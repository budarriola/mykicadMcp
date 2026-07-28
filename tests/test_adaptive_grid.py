"""Tests for the adaptive detailed-grid selection (`_choose_grid` /
`_MAX_WINDOW_NODES` interaction in `_route_core`, kicad_router_tool.py).

Covers:
  1. `_choose_grid` unit behaviour: short spans return the base (fine) grid
     UNCHANGED; long spans coarsen just enough to fit the node budget, capped
     at `max_grid`; grid selection never fails (the caller's node check does).
  2. A synthetic long connection that FAILS `window_too_large` at the fixed
     0.2 mm base grid now ROUTES with the adaptive grid, with a passing
     self-check and a `grid_mm` between the base and the configured max.
  3. A short synthetic connection still uses the fine base grid and reports
     the SAME grid_mm / geometry it always has (regression guard).
  4. Determinism: two preview runs of the long connection are byte-identical.
"""

from __future__ import annotations

import json
from pathlib import Path

import kicad_pcb_tool as pcb
import kicad_router_tool as router

from synthetic_board import _HEADER_TEMPLATE, _layer_stack_lines, _net_table, _synthetic_kicad_pro_text

_HDR2 = _HEADER_TEMPLATE.format(layer_lines=_layer_stack_lines(2))


def _pad_block(ref: str, x: float, y: float, net: str, uid: str) -> str:
    return (f'    (footprint "synthetic:PAD"\n        (layer "F.Cu")\n        (uuid "{uid}")\n'
            f'        (at {x} {y})\n'
            f'        (property "Reference" "{ref}" (at 0 -1) (layer "F.SilkS"))\n'
            f'        (property "Value" "P" (at 0 1) (layer "F.Fab"))\n'
            f'        (pad "1" smd rect (at 0 0) (size 0.6 0.6) (layers "F.Cu" "F.Paste" "F.Mask") (net "{net}"))\n'
            f'    )\n')


def _write_project(directory: Path, pads: list[tuple[str, float, float, str]],
                    nets: list[str], settings_overrides: dict | None = None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    parts = [_HDR2, _net_table(nets)]
    for i, (ref, x, y, net) in enumerate(pads):
        parts.append(_pad_block(ref, x, y, net, f"pad{i:04d}"))
    parts.append(")\n")
    (directory / "adap.kicad_pcb").write_text("".join(parts), encoding="utf-8")
    (directory / "adap.kicad_pro").write_text(_synthetic_kicad_pro_text(), encoding="utf-8")
    settings = {"autorouter": {"allowed_layers": ["F.Cu"]}}
    if settings_overrides:
        settings["autorouter"].update(settings_overrides)
    (directory / "pcb_settings.json").write_text(json.dumps(settings), encoding="utf-8")
    pcb._invalidate_board_cache(directory / "adap.kicad_pcb")
    return directory


def _by_net(res):
    return {c["net"]: c for c in res["connections"]}


# --------------------------------------------------------------------------- #
# Unit: _choose_grid
# --------------------------------------------------------------------------- #

def test_choose_grid_short_span_returns_base_unchanged() -> None:
    # A small window (well within budget at the base grid) must come back
    # bit-for-bit as the base grid - the guarantee that short-connection
    # geometry is untouched by this change.
    grid = router._choose_grid(span_x=10.0, span_y=10.0, n_layers=2,
                               base_grid=0.2, max_grid=1.0, budget=400_000)
    assert grid == 0.2


def test_choose_grid_long_span_coarsens_within_cap() -> None:
    grid = router._choose_grid(span_x=166.0, span_y=166.0, n_layers=1,
                               base_grid=0.2, max_grid=1.0, budget=400_000)
    assert 0.2 < grid <= 1.0
    assert router._window_node_count(166.0, 166.0, grid, 1) <= 400_000


def test_choose_grid_pathological_span_clamps_to_max_grid() -> None:
    # Even the coarsest allowed grid can't fit this budget; _choose_grid still
    # returns max_grid (never fails) - the caller's node-budget check is what
    # turns an over-budget result into `window_too_large`.
    grid = router._choose_grid(span_x=10_000.0, span_y=10_000.0, n_layers=4,
                               base_grid=0.2, max_grid=1.0, budget=400_000)
    assert grid == 1.0


def test_choose_grid_is_a_pure_function() -> None:
    a = router._choose_grid(83.0, 47.0, 2, 0.2, 1.0, 400_000)
    b = router._choose_grid(83.0, 47.0, 2, 0.2, 1.0, 400_000)
    assert a == b


# --------------------------------------------------------------------------- #
# Integration: long connection fails at fixed grid, routes with adaptive grid
# --------------------------------------------------------------------------- #

def test_long_connection_window_too_large_at_fixed_grid(tmp_path: Path, monkeypatch) -> None:
    """Sanity-check the FAILURE this fix addresses: forcing `max_grid_mm` down
    to the base grid (i.e. disabling adaptation) reproduces `window_too_large`
    on a connection whose window is too large at the fine 0.2 mm grid.

    The M5 whole-board LAZY window tier (`_route_wide_lazy`) now rescues exactly
    this case - a lazy window has no up-front rasterization cost, so the eager
    node budget that produced `window_too_large` does not apply to it. That is a
    deliberate capability, and it is asserted in the companion test below; here
    the tier is disabled so the ORIGINAL adaptive-grid failure mode is still
    covered on its own terms."""
    monkeypatch.setattr(router, "_route_wide_lazy", lambda *a, **k: None)
    monkeypatch.setattr(router, "_resolve_workers", lambda settings: 1)
    pads = [("L1", 5.0, 5.0, "LONG"), ("L2", 155.0, 155.0, "LONG")]
    proj = _write_project(tmp_path / "nogrid", pads, ["LONG"],
                          settings_overrides={"max_grid_mm": 0.2})
    res = router.route_nets(proj, write=False)
    rec = _by_net(res)["LONG"]
    assert rec["routed"] is False
    assert rec["failure"]["reason"] == "window_too_large"


def test_wide_lazy_tier_rescues_window_too_large(tmp_path: Path) -> None:
    """The M5 lift, end to end through `route_nets`: the SAME fixed-grid project
    that reports `window_too_large` with the tier disabled (above) now routes,
    and reports the whole-board lazy window it used."""
    pads = [("L1", 5.0, 5.0, "LONG"), ("L2", 155.0, 155.0, "LONG")]
    proj = _write_project(tmp_path / "widelazy", pads, ["LONG"],
                          settings_overrides={"max_grid_mm": 0.2})
    res = router.route_nets(proj, write=False)
    rec = _by_net(res)["LONG"]
    assert rec["routed"] is True, rec.get("failure")
    assert rec["self_check"]["passed"] is True
    assert rec["self_check"]["violation_count"] == 0
    assert rec["wide_lazy_window"]["grid_mm"] == 0.2


def test_long_connection_routes_with_adaptive_grid(tmp_path: Path) -> None:
    pads = [("L1", 5.0, 5.0, "LONG"), ("L2", 155.0, 155.0, "LONG")]
    proj = _write_project(tmp_path / "long", pads, ["LONG"])

    res = router.route_nets(proj, write=False)
    rec = _by_net(res)["LONG"]
    assert rec["routed"] is True, rec.get("failure")
    assert rec["self_check"]["passed"] is True
    assert rec["self_check"]["violation_count"] == 0
    # the chosen grid is coarser than the base 0.2 mm fine grid, within the
    # configured max_grid_mm (default 1.0).
    assert 0.2 < rec["grid_mm"] <= 1.0

    # write path also succeeds and stays self-check clean.
    wr = router.route_nets(proj, write=True)
    wrec = _by_net(wr)["LONG"]
    assert wrec["routed"] is True
    assert wrec["self_check"]["passed"] is True


def test_long_connection_determinism(tmp_path: Path) -> None:
    pads = [("L1", 5.0, 5.0, "LONG"), ("L2", 155.0, 155.0, "LONG")]
    proj = _write_project(tmp_path / "long_det", pads, ["LONG"])
    a = router.route_nets(proj, write=False)
    b = router.route_nets(proj, write=False)
    assert json.dumps(a["connections"], sort_keys=True) == json.dumps(b["connections"], sort_keys=True)


# --------------------------------------------------------------------------- #
# Regression: short connection keeps the fine base grid
# --------------------------------------------------------------------------- #

def test_short_connection_keeps_fine_base_grid(tmp_path: Path) -> None:
    pads = [("S1", 5.0, 5.0, "SHORT"), ("S2", 8.0, 5.0, "SHORT")]
    proj = _write_project(tmp_path / "short", pads, ["SHORT"])

    res = router.route_nets(proj, write=False)
    rec = _by_net(res)["SHORT"]
    assert rec["routed"] is True
    assert rec["grid_mm"] == 0.2

    # unaffected by a much larger max_grid_mm - short connections never
    # coarsen regardless of the cap.
    proj2 = _write_project(tmp_path / "short2", pads, ["SHORT"],
                           settings_overrides={"max_grid_mm": 5.0})
    res2 = router.route_nets(proj2, write=False)
    rec2 = _by_net(res2)["SHORT"]
    assert rec2["grid_mm"] == 0.2
    assert rec2["length_mm"] == rec["length_mm"]
    assert rec2["via_count"] == rec["via_count"]
