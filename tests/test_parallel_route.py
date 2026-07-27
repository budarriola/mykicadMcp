"""Tests for the 7.8b speculative-parallel routing pass + cheap feasibility
screen (`kicad_router_tool._run_independent_routes` generalized to route every
connection concurrently against the base board, `_feasibility_screen`).

Uses a small synthetic multi-net open board (`synthetic_board.write_synthetic_project`,
`mode="simple"`, `route=False`) - several two-pad components, each its own
isolated net pair, unrouted - so every connection routes trivially and
independently, which is exactly the shape that exercises the parallel commit
path (most connections clean-commit straight out of the speculative pass).

Covers:
  1. Determinism across worker counts: `autorouter.cpu.workers` forced to 1
     (serial fallback) vs the default (auto = cpu_count-1, parallel) produce
     byte-identical `connections` JSON.
  2. The cheap feasibility screen is a heuristic ONLY: forcing it to always
     report the worst ("hardest") score for every connection must not change
     which connections route, nor their geometry - it may only affect the
     order connections are SUBMITTED to the worker pool.
  3. `_feasibility_screen` runs standalone (no obstacles) without raising and
     returns an int in `[0, node_cap]`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import kicad_pcb_tool as pcb
import kicad_router_tool as router
from synthetic_board import write_multidrop_spi_project


def _write_project(tmp_path: Path, workers: int | None = None) -> Path:
    write_multidrop_spi_project(tmp_path, project_name="spibus", destinations=4, route=False)
    settings: dict = {}
    if workers is not None:
        settings = {"autorouter": {"cpu": {"workers": workers}}}
    (tmp_path / "pcb_settings.json").write_text(json.dumps(settings), encoding="utf-8")
    pcb._invalidate_board_cache(tmp_path / "spibus.kicad_pcb")
    return tmp_path


def _dump(res: dict) -> str:
    return json.dumps(sorted(res["connections"], key=lambda c: c["net"]), sort_keys=True)


def test_determinism_across_worker_counts(tmp_path: Path) -> None:
    proj_serial = _write_project(tmp_path / "serial", workers=1)
    proj_parallel = _write_project(tmp_path / "parallel", workers=0)  # 0 = auto

    res_serial = router.route_nets(proj_serial, write=False)
    res_parallel = router.route_nets(proj_parallel, write=False)

    assert res_serial["summary"]["connections_routed"] >= 1
    assert _dump(res_serial) == _dump(res_parallel)

    # And two parallel runs of the same project are identical to each other.
    res_parallel2 = router.route_nets(proj_parallel, write=False)
    assert _dump(res_parallel) == _dump(res_parallel2)


def test_determinism_across_different_worker_pool_sizes(tmp_path: Path) -> None:
    proj_two = _write_project(tmp_path / "w2", workers=2)
    proj_many = _write_project(tmp_path / "wmany", workers=8)

    res_two = router.route_nets(proj_two, write=False)
    res_many = router.route_nets(proj_many, write=False)
    assert _dump(res_two) == _dump(res_many)


def test_screen_is_heuristic_only_forcing_worst_score_does_not_regress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    proj = _write_project(tmp_path, workers=0)
    baseline = router.route_nets(proj, write=False)
    assert baseline["summary"]["connections_routed"] >= 1

    # Force the screen to report every connection as maximally "hard" - if the
    # screen were ever used as a gate (rather than pure submission-order
    # heuristic) this would starve/skip connections. It must not change the
    # routed set or geometry at all.
    real_screen = router._feasibility_screen

    def _worst_case_screen(ctx, conn, obstacles, screen_grid=1.0, node_cap=600):
        return node_cap

    monkeypatch.setattr(router, "_feasibility_screen", _worst_case_screen)
    forced = router.route_nets(proj, write=False)
    monkeypatch.setattr(router, "_feasibility_screen", real_screen)

    assert _dump(forced) == _dump(baseline)


def test_feasibility_screen_standalone_returns_bounded_int(kiln_project_path: Path) -> None:
    rats = router.get_ratsnest(kiln_project_path)
    conns = rats["connections"]  # get_ratsnest only ever lists MISSING connections
    if not conns:
        pytest.skip("no unrouted kiln connection available to screen")
    conn = conns[0]

    board_path, _, _ = pcb._resolve_project_path(kiln_project_path)
    settings = pcb.load_pcb_settings(kiln_project_path)["config"]
    autor = settings.get("autorouter", {})
    board_bbox = router._board_bbox(board_path)
    ctx = {
        "base_margin": float(autor.get("search_window_margin_mm", 8.0)) or 8.0,
        "board_bbox": board_bbox,
        "grid": float(autor.get("grid_mm", 0.2)) or 0.2,
    }
    score = router._feasibility_screen(ctx, conn, [])
    assert isinstance(score, int)
    assert 0 <= score <= 600
