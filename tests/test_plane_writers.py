r"""Tests for Phase 7.5.5 (creating and moving planes) - the plane WRITERS:
`kicad_router_tool.propose_plane` / `create_plane` / `modify_plane`, MCP
tools `propose_kicad_plane` / `create_kicad_plane` / `modify_kicad_plane`.

Synthetic board (same hand-written-zone-text approach as
`tests/test_plane_islands.py`, since `synthetic_board.py` has no
zone-authoring helper): two footprints on net GND (F.Cu), one on net
3.3V_RAIL (F.Cu, no zone yet — name deliberately matches the default
``power_net_patterns``' r"3\.3[Vv]" regex so it exercises the power-layer-
preference path), plus an Edge.Cuts outline (so propose_plane's
board-bbox clip has real margin to work with, not just the copper bbox) and
one HAND-MADE zone on GND to exercise the modify_plane ownership refusal.

Plus one read-only kiln smoke test for propose_kicad_plane (safe against the
real committed board - it never writes).
"""

from __future__ import annotations

from pathlib import Path

import pytest

import kicad_pcb_tool as pcb
import kicad_router_tool as router
from tests.synthetic_board import _HEADER_TEMPLATE, _footprint_block, _layer_stack_lines, _net_table

_FOOTER = ")\n"

_HAND_ZONE_UUID = "hand-gnd-zone-0001"


def _hand_zone_block() -> str:
    return f"""    (zone
        (net "GND")
        (net_name "GND")
        (layer "F.Cu")
        (uuid "{_HAND_ZONE_UUID}")
        (name "handGnd")
        (priority 0)
        (hatch edge 0.5)
        (connect_pads
            (clearance 0.3)
        )
        (min_thickness 0.25)
        (fill yes
            (thermal_gap 0.5)
            (thermal_bridge_width 0.5)
            (smoothing fillet)
            (radius 0.1)
            (island_removal_mode 0)
        )
        (polygon
            (pts (xy -5 -5) (xy 25 -5) (xy 25 15) (xy -5 15))
        )
        (filled_polygon
            (layer "F.Cu")
            (pts (xy -5 -5) (xy 25 -5) (xy 25 15) (xy -5 15))
        )
    )
"""


def _edge_cuts_block() -> str:
    return """    (gr_rect
        (start -20 -20)
        (end 50 30)
        (layer "Edge.Cuts")
        (uuid "edge-outline-0001")
    )
"""


def _write_board(path: Path) -> Path:
    header = _HEADER_TEMPLATE.format(layer_lines=_layer_stack_lines(2))
    net_names = ["GND", "SIG1", "SIG2", "3.3V_RAIL", "SIG3"]
    parts = [header, _net_table(net_names), _edge_cuts_block()]
    # R1 pad1 (GND) at (-0.75, 0) - inside the hand-made GND zone.
    parts.append(_footprint_block("R1", "10k", 0.0, 0.0, "synth-fp-r1", "GND", "SIG1"))
    # R2 pad1 (GND) at (19.25, 10) - also inside the hand-made GND zone.
    parts.append(_footprint_block("R2", "10k", 20.0, 10.0, "synth-fp-r2", "GND", "SIG2"))
    # R3 pad1 (3.3V_RAIL) at (9.25, -10) - a net with NO zone yet.
    parts.append(_footprint_block("R3", "10k", 10.0, -10.0, "synth-fp-r3", "3.3V_RAIL", "SIG3"))
    parts.append(_hand_zone_block())
    parts.append(_FOOTER)
    path.write_text("".join(parts), encoding="utf-8")
    return path


@pytest.fixture
def board_dir(tmp_path: Path) -> Path:
    _write_board(tmp_path / "synthboard.kicad_pcb")
    return tmp_path


# --------------------------------------------------------------------------- #
# propose_plane (read-only)
# --------------------------------------------------------------------------- #

def test_propose_plane_returns_sane_outline_and_cost_delta(board_dir: Path):
    res = router.propose_plane(str(board_dir), "3.3V_RAIL", "F.Cu")
    assert res["net"] == "3.3V_RAIL"
    assert res["net_kind"] == "power"  # matches default power_net_patterns' r"3\.3[Vv]"
    assert res["layer"] == "F.Cu"

    outline = res["outline"]
    assert len(outline) == 4
    xs = [p["x"] for p in outline]
    ys = [p["y"] for p in outline]
    assert max(xs) - min(xs) > 0
    assert max(ys) - min(ys) > 0
    assert res["outline_area_mm2"] > 0

    # The single 3.3V_RAIL pad (9.25, -10) must fall inside the proposed outline.
    assert min(xs) <= 9.25 <= max(xs)
    assert min(ys) <= -10 <= max(ys)

    assert res["component_count"] >= 1
    assert "cost_delta" in res
    assert "estimate" in res
    # 3.3V_RAIL has no routed copper -> current routing cost is exactly 0.
    assert res["current_routing_cost"] == 0.0


def test_propose_plane_auto_picks_layer_when_omitted(board_dir: Path):
    res = router.propose_plane(str(board_dir), "GND")
    assert res["layer"] in ("F.Cu", "B.Cu")


def test_propose_plane_raises_for_net_with_no_pads_on_layer(board_dir: Path):
    with pytest.raises(ValueError):
        router.propose_plane(str(board_dir), "SIG1", "B.Cu")  # SIG1 pad is F.Cu only


def test_kiln_propose_plane_smoke(kiln_project_path: Path):
    """Read-only smoke test against the real committed kiln board: propose a
    plane for a real power net and confirm the result shape, without ever
    writing (propose_plane takes no write parameter - it can't touch disk)."""
    res = router.propose_plane(str(kiln_project_path), "GND_Main", "F.Cu")
    assert res["net"] == "GND_Main"
    assert res["net_kind"] == "power"
    assert res["layer"] == "F.Cu"
    assert len(res["outline"]) == 4
    assert res["outline_area_mm2"] > 0
    assert "cost_delta" in res


# --------------------------------------------------------------------------- #
# create_plane
# --------------------------------------------------------------------------- #

def test_create_plane_dry_run_does_not_touch_board(board_dir: Path):
    board = board_dir / "synthboard.kicad_pcb"
    before = board.read_bytes()

    res = router.create_plane(str(board_dir), "3.3V_RAIL", "F.Cu", write=False)
    assert res["write"] is False
    assert res["written"] is False
    assert "(zone" in res["block"]
    assert res["net"] == "3.3V_RAIL"
    assert res["layer"] == "F.Cu"

    after = board.read_bytes()
    assert before == after


def test_create_plane_write_roundtrips_and_records_ownership(board_dir: Path):
    res = router.create_plane(str(board_dir), "3.3V_RAIL", "F.Cu", write=True)
    assert res["written"] is True
    new_uuid = res["uuid"]

    # The written (zone ...) block must round-trip through the board's own
    # zone parser (`_parse_zones_cached`), matching what create_plane reported.
    zones = router._parse_zones_cached(board_dir / "synthboard.kicad_pcb")
    written = next((z for z in zones if z["uuid"] == new_uuid), None)
    assert written is not None
    assert written["net"] == "3.3V_RAIL"
    assert "F.Cu" in written["layers"]
    assert len(written["polygon"]) >= 3

    # Ownership round-trips into board-local autorouter_owned.zones.
    state = pcb.load_board_local(str(board_dir))
    owned_zones = state["data"].get("autorouter_owned", {}).get("zones", [])
    assert new_uuid in owned_zones

    # Fill-setting shape was copied from the board's existing (hand-made)
    # zone template, not invented from nothing.
    assert written["min_thickness"] is not None
    assert written["hatch"] is not None


def test_create_plane_copies_fill_shape_from_existing_zone(board_dir: Path):
    res = router.create_plane(str(board_dir), "3.3V_RAIL", "F.Cu", write=False)
    # The hand-made GND zone uses hatch "edge 0.5" / clearance 0.3 / min_thickness
    # 0.25 - the new zone's block must carry the same shape verbatim.
    assert "(hatch edge 0.5)" in res["block"]
    assert "(clearance 0.3)" in res["block"]
    assert "(min_thickness 0.25)" in res["block"]


# --------------------------------------------------------------------------- #
# modify_plane
# --------------------------------------------------------------------------- #

def test_modify_plane_refuses_hand_made_zone(board_dir: Path):
    with pytest.raises(ValueError):
        router.modify_plane(
            str(board_dir), _HAND_ZONE_UUID,
            new_outline=[{"x": -5, "y": -5}, {"x": 30, "y": -5}, {"x": 30, "y": 20}, {"x": -5, "y": 20}],
            write=False,
        )


def test_modify_plane_dry_run_does_not_touch_board(board_dir: Path):
    create_res = router.create_plane(str(board_dir), "3.3V_RAIL", "F.Cu", write=True)
    board = board_dir / "synthboard.kicad_pcb"
    before = board.read_bytes()

    res = router.modify_plane(
        str(board_dir), create_res["uuid"], priority=2, write=False,
    )
    assert res["write"] is False
    assert res["written"] is False
    assert "(priority 2)" in res["block"]

    after = board.read_bytes()
    assert before == after


def test_modify_plane_succeeds_on_owned_zone(board_dir: Path):
    create_res = router.create_plane(str(board_dir), "3.3V_RAIL", "F.Cu", write=True)
    new_outline = [{"x": 0.0, "y": -15.0}, {"x": 20.0, "y": -15.0},
                   {"x": 20.0, "y": -5.0}, {"x": 0.0, "y": -5.0}]

    res = router.modify_plane(
        str(board_dir), create_res["uuid"], new_outline=new_outline, priority=3, write=True,
    )
    assert res["written"] is True

    zones = router._parse_zones_cached(board_dir / "synthboard.kicad_pcb")
    modified = next(z for z in zones if z["uuid"] == create_res["uuid"])
    assert modified["priority"] == 3
    assert len(modified["polygon"]) == 4
    got_pts = {(round(x, 4), round(y, 4)) for x, y in modified["polygon"]}
    want_pts = {(p["x"], p["y"]) for p in new_outline}
    assert got_pts == want_pts


def test_modify_plane_requires_outline_or_priority(board_dir: Path):
    create_res = router.create_plane(str(board_dir), "3.3V_RAIL", "F.Cu", write=True)
    with pytest.raises(ValueError):
        router.modify_plane(str(board_dir), create_res["uuid"], write=False)
