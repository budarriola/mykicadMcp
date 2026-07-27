"""Phase 7.5.6 - the plane stitching pass (`run_kicad_stitching_pass`) and its
undo (`remove_kicad_stitching_vias`).

The island-rescue/return-path/general tests reuse `scratch_board` (a copy of
the real kiln project) - like `test_optimizer.py`'s move-(d)/(e)/(f) tests,
this board already has real pours, real islands, real critical/high-speed
nets and real power/ground planes, which a from-scratch synthetic fixture
would have to fake piece by piece. The removal tests build small synthetic
boards where exact via counts/positions matter more than realism.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import kicad_optimizer_tool as o
import kicad_pcb_tool as k
import kicad_router_tool as r

from synthetic_board import write_multidrop_spi_project


def _set_stitching(project: Path, **overrides) -> None:
    path = Path(project) / "pcb_settings.json"
    config = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    config.setdefault("stitching", {}).update(overrides)
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


# --- island rescue -----------------------------------------------------------


def test_island_rescue_reduces_plane_island_cost(scratch_board: Path) -> None:
    project = Path(scratch_board)
    before = r.audit_plane_islands(project)["summary"]["total_island_cost"]

    result = o.run_stitching_pass(project, write=True)

    assert result["enabled"] is True
    assert len(result["island_rescue"]) > 0, "kiln's pours have costed islands to rescue"
    assert result["placed_count"] >= len(result["island_rescue"])

    after = r.audit_plane_islands(project)["summary"]["total_island_cost"]
    assert after < before, "attaching every costed island must cut the board's total island cost"

    owned = k.load_board_local(project)["data"]["autorouter_owned"]
    rescue_uuids = {
        rec["uuid"] for rec in owned["records"]
        if rec.get("stitching") and rec.get("net") in {e["net"] for e in result["island_rescue"]}
    }
    assert rescue_uuids, "island-rescue vias must be recorded as autorouter-owned + tagged"


def test_island_rescue_dry_run_previews_without_writing(scratch_board: Path) -> None:
    project = Path(scratch_board)
    board_path, _, _ = k._resolve_project_path(project)
    before_bytes = board_path.read_bytes()

    result = o.run_stitching_pass(project, write=False)

    assert result["write"] is False
    assert result["placed_count"] == 0
    assert result["planned_count"] > 0
    assert board_path.read_bytes() == before_bytes


# --- return-path stitching ---------------------------------------------------


def test_return_path_stitching_targets_critical_nets(scratch_board: Path) -> None:
    project = Path(scratch_board)
    critical_names = {rec["net"] for rec in k.classify_critical_nets(project)["critical_nets"]}

    result = o.run_stitching_pass(project, write=False)

    assert result["return_path"], "kiln has classified critical nets near power/ground pours"
    for entry in result["return_path"]:
        assert entry["near_net"] in critical_names
        assert entry["kind"] == "return_path"


# --- general stitching --------------------------------------------------------


def test_general_stitching_respects_target_spacing(scratch_board: Path) -> None:
    project = Path(scratch_board)
    _set_stitching(project, target_spacing_mm=8.0)

    result = o.run_stitching_pass(project, write=False)
    assert result["general"], "kiln's ground/power pours have room for general stitching"

    by_net_layer: dict[tuple[str, str], list[tuple[float, float]]] = {}
    for entry in result["general"]:
        by_net_layer.setdefault((entry["net"], entry["layer"]), []).append((entry["x"], entry["y"]))

    for points in by_net_layer.values():
        for i, (x1, y1) in enumerate(points):
            for x2, y2 in points[i + 1:]:
                assert math.hypot(x1 - x2, y1 - y2) >= 8.0 - 1e-6


def test_stitching_disabled_is_a_noop(scratch_board: Path) -> None:
    project = Path(scratch_board)
    board_path, _, _ = k._resolve_project_path(project)
    before_bytes = board_path.read_bytes()
    _set_stitching(project, enabled=False)

    result = o.run_stitching_pass(project, write=True)

    assert result["enabled"] is False
    assert result["planned_count"] == 0
    assert result["placed_count"] == 0
    assert result["island_rescue"] == []
    assert result["return_path"] == []
    assert result["general"] == []
    assert board_path.read_bytes() == before_bytes


# --- remove_kicad_stitching_vias ---------------------------------------------


def _small_routed_project(tmp_path: Path) -> Path:
    write_multidrop_spi_project(tmp_path, destinations=1, route=True)
    return tmp_path


def test_remove_only_deletes_stitching_tagged_autorouter_vias(tmp_path: Path) -> None:
    project = _small_routed_project(tmp_path)

    # a plain routing via (untagged) - must survive.
    routing = o._place_stitching_via(project, "/SPI/SCK", 10.0, 10.0)
    # a real stitching-pass via (tagged) - must be removed.
    stitching = o._place_stitching_via(project, "/SPI/SCK", 20.0, 20.0, stitching=True)

    preview = o.remove_stitching_vias(project, write=False)
    assert preview["candidates"] == 1
    assert preview["removed_uuids"] == [stitching["uuid"]]

    applied = o.remove_stitching_vias(project, write=True)
    assert applied["removed"] == 1
    assert applied["removed_uuids"] == [stitching["uuid"]]

    owned = k.load_board_local(project)["data"]["autorouter_owned"]
    assert routing["uuid"] in owned["vias"]
    assert stitching["uuid"] not in owned["vias"]


def test_remove_area_filter_scopes_to_the_given_rect(tmp_path: Path) -> None:
    project = _small_routed_project(tmp_path)

    inside = o._place_stitching_via(project, "/SPI/SCK", 15.0, 15.0, stitching=True)
    outside = o._place_stitching_via(project, "/SPI/SCK", 90.0, 90.0, stitching=True)

    area = {"x_min": 10.0, "x_max": 20.0, "y_min": 10.0, "y_max": 20.0}
    result = o.remove_stitching_vias(project, area=area, write=True)

    assert result["removed_uuids"] == [inside["uuid"]]
    owned = k.load_board_local(project)["data"]["autorouter_owned"]
    assert outside["uuid"] in owned["vias"]
    assert inside["uuid"] not in owned["vias"]


def test_include_foreign_lists_but_never_deletes(tmp_path: Path) -> None:
    project = _small_routed_project(tmp_path)
    board_path, _, _ = k._resolve_project_path(project)

    # a hand-placed free via (net == "") - never autorouter_owned, never a
    # candidate for the tagged-stitching deletion path.
    text = k._read_text(board_path)
    free_via_uuid = "11111111-2222-3333-4444-555555555555"
    block = r._via_block({"x": 50.0, "y": 50.0}, "", 1.0, 0.6, "F.Cu", "B.Cu", free_via_uuid)
    board_path.write_text(k._append_top_level_block(text, block), encoding="utf-8", newline="")
    k._invalidate_board_cache(board_path)
    before_bytes = board_path.read_bytes()

    result = o.remove_stitching_vias(project, include_foreign=True, write=False)

    assert result["removed"] == 0
    assert result["written"] is False
    foreign_uuids = {f["uuid"] for f in result["foreign_vias"]}
    assert free_via_uuid in foreign_uuids
    listed = next(f for f in result["foreign_vias"] if f["uuid"] == free_via_uuid)
    assert listed["free"] is True
    assert board_path.read_bytes() == before_bytes, "include_foreign must only list, never delete"


def test_dry_run_never_touches_the_board(tmp_path: Path) -> None:
    project = _small_routed_project(tmp_path)
    board_path, _, _ = k._resolve_project_path(project)
    o._place_stitching_via(project, "/SPI/SCK", 12.0, 12.0, stitching=True)
    before_bytes = board_path.read_bytes()

    result = o.remove_stitching_vias(project, write=False)

    assert result["candidates"] == 1
    assert result["written"] is False
    assert board_path.read_bytes() == before_bytes
