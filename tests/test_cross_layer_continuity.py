"""Tests for Phase 7.18.2 - cross-layer fill continuity in
`audit_plane_islands` (MCP tool `audit_kicad_plane_islands`).

7.5.3 already answers "is this island bonded to the rest of its OWN layer's
pour". 7.18.2 adds the other axis: two same-net pours on two DIFFERENT layers
are one node in the netlist, but if nothing (or almost nothing) vias between
them, everything referencing one reaches the other through that one via. This
is read-only - `run_kicad_stitching_pass` (7.5.6) is the writer that fixes what
gets flagged here - so there is no parity gate on this file; the assertions are
about the new `cross_layer` report being correct and about the pre-existing
keys of the audit result being untouched.

The synthetic board below is built the same way `test_plane_islands.py` builds
its fixtures (raw `(zone ...)` s-expr text over `synthetic_board.py`'s header/
footprint/net-table helpers), extended with hand-written `(via ...)` blocks so
the bonding-via count is exact and known rather than inferred.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import kicad_router_tool as router
from tests.synthetic_board import _HEADER_TEMPLATE, _footprint_block, _layer_stack_lines, _net_table
from tests.test_plane_islands import _write_island_board, _zone_block

_FOOTER = ")\n"
_SQUARE = [(-5.0, -5.0), (5.0, -5.0), (5.0, 5.0), (-5.0, 5.0)]


def _via_block(x: float, y: float, net: str, uuid: str,
               top: str = "F.Cu", bottom: str = "B.Cu") -> str:
    return (f'    (via\n'
            f'        (at {x} {y})\n'
            f'        (size 0.6)\n'
            f'        (drill 0.3)\n'
            f'        (layers "{top}" "{bottom}")\n'
            f'        (net "{net}")\n'
            f'        (uuid "{uuid}")\n'
            f'    )\n')


def _write_two_layer_gnd_board(path: Path, via_positions: list[tuple[float, float]]) -> Path:
    """A GND pour on BOTH F.Cu and B.Cu over the same square, bonded by exactly
    `len(via_positions)` GND vias (each landing inside both pours)."""
    header = _HEADER_TEMPLATE.format(layer_lines=_layer_stack_lines(2))
    parts = [header, _net_table(["GND", "SIG"])]
    parts.append(_footprint_block("R1", "10k", 0.0, 0.0, "synth-fp-r1", "GND", "SIG"))
    for layer in ("F.Cu", "B.Cu"):
        parts.append(_zone_block(
            net="GND", layer=layer, uuid=f"synth-zone-gnd-{layer}", priority=0,
            island_removal_mode=0, outline_pts=_SQUARE, filled_polys=[_SQUARE],
        ))
    for i, (x, y) in enumerate(via_positions):
        parts.append(_via_block(x, y, "GND", f"synth-via-{i}"))
    parts.append(_FOOTER)
    path.write_text("".join(parts), encoding="utf-8")
    return path


@pytest.fixture
def unbonded_board(tmp_path: Path) -> Path:
    _write_two_layer_gnd_board(tmp_path / "synthboard.kicad_pcb", [])
    return tmp_path


@pytest.fixture
def one_via_board(tmp_path: Path) -> Path:
    _write_two_layer_gnd_board(tmp_path / "synthboard.kicad_pcb", [(0.0, 2.0)])
    return tmp_path


@pytest.fixture
def single_layer_board(tmp_path: Path) -> Path:
    """`test_plane_islands.py`'s own F.Cu-only fixture board, rebuilt here
    (a fixture is not importable across test modules) so the "a net on one
    layer has no pair to reason about" case is proven against exactly the
    board the 7.5.3 tests already use."""
    _write_island_board(tmp_path / "synthboard.kicad_pcb")
    return tmp_path


@pytest.fixture
def bonded_board(tmp_path: Path) -> Path:
    _write_two_layer_gnd_board(
        tmp_path / "synthboard.kicad_pcb", [(0.0, 2.0), (2.0, -2.0), (-2.0, -2.0)])
    return tmp_path


def _pair(res: dict, net: str) -> dict:
    entry = next(e for e in res["cross_layer"] if e["net"] == net)
    assert len(entry["layer_pairs"]) == 1
    return entry["layer_pairs"][0]


# --------------------------------------------------------------------------- #
# The new cross-layer report
# --------------------------------------------------------------------------- #

def test_two_pours_with_no_bonding_via_are_flagged(unbonded_board: Path) -> None:
    """The worst case: same net, two layers, ZERO vias between them. The
    netlist calls this one node; electrically the two pours only meet through
    whatever pad happens to span both."""
    res = router.audit_plane_islands(str(unbonded_board))
    pair = _pair(res, "GND")
    assert pair["layers"] == ["F.Cu", "B.Cu"]
    assert pair["bonding_via_count"] == 0
    assert pair["weakly_coupled"] is True
    assert res["summary"]["weakly_coupled_layer_pairs"] == [
        {"net": "GND", "layers": ["F.Cu", "B.Cu"],
         "bonding_via_count": 0, "bonding_pad_count": pair["bonding_pad_count"]}
    ]


def test_one_bonding_via_is_still_weak(one_via_board: Path) -> None:
    """One via clears "connected" but not the `island_min_attachments_warn`
    convention (default 2) the same-layer model already uses - a single via is
    the whole return path between the two pours."""
    res = router.audit_plane_islands(str(one_via_board))
    pair = _pair(res, "GND")
    assert pair["bonding_via_count"] == 1
    assert pair["weakly_coupled"] is True
    assert res["plane_settings"]["island_min_attachments_warn"] == 2


def test_enough_bonding_vias_clears_the_flag(bonded_board: Path) -> None:
    res = router.audit_plane_islands(str(bonded_board))
    pair = _pair(res, "GND")
    assert pair["bonding_via_count"] == 3
    assert pair["weakly_coupled"] is False
    assert res["summary"]["weakly_coupled_layer_pairs"] == []
    assert [v["position"] for v in pair["bonding_vias"]] == [
        {"x": 0.0, "y": 2.0}, {"x": 2.0, "y": -2.0}, {"x": -2.0, "y": -2.0}]


def test_via_outside_the_fill_does_not_count(tmp_path: Path) -> None:
    """A same-net via that spans both layers but lands OUTSIDE the pours bonds
    nothing - counting it would be the exact false-clear this audit exists to
    prevent."""
    _write_two_layer_gnd_board(tmp_path / "synthboard.kicad_pcb",
                               [(0.0, 2.0), (40.0, 40.0), (41.0, 41.0)])
    res = router.audit_plane_islands(str(tmp_path))
    pair = _pair(res, "GND")
    assert pair["bonding_via_count"] == 1
    assert pair["weakly_coupled"] is True


def test_single_layer_net_gets_no_cross_layer_entry(single_layer_board: Path) -> None:
    """`test_plane_islands.py`'s fixture pours GND and GND2 on F.Cu only:
    there is no second layer, so there is no pair to reason about and the net
    must not appear in the cross-layer report at all."""
    res = router.audit_plane_islands(str(single_layer_board))
    assert res["cross_layer"] == []
    assert res["summary"]["weakly_coupled_layer_pairs"] == []


def test_existing_audit_shape_is_unchanged(single_layer_board: Path) -> None:
    """7.18.2 is additive: every key 7.5.2/7.5.3 consumers already read must
    still be present and unmoved (`run_stitching_pass` step 1 walks
    zones->layers->components->suggested_stitching_via)."""
    res = router.audit_plane_islands(str(single_layer_board))
    assert {"board_path", "plane_settings", "zones", "summary"} <= set(res)
    assert {"island_count", "orphan_island_count", "total_island_cost",
            "warnings"} <= set(res["summary"])
    zone = res["zones"][0]
    assert {"uuid", "name", "net", "priority", "island_removal_mode", "layers"} <= set(zone)
    assert {"layer", "fill_source", "component_count", "components"} <= set(zone["layers"][0])


# --------------------------------------------------------------------------- #
# The real board
# --------------------------------------------------------------------------- #

def test_kiln_cross_layer_report_is_measured(kiln_project_path: Path) -> None:
    """MEASURED on kiln: GND_Main, GND_Safty and 12v_Safty each pour on
    F.Cu + In1.Cu + B.Cu. Every layer pair is bonded well past the warn
    threshold (GND_Main 251 bonding vias per pair, GND_Safty 109, 12v_Safty 5),
    so this board reports ZERO weakly-coupled pairs - the audit is honest about
    finding nothing wrong here. It is asserted as a REGRESSION gate: if a
    future edit strands a pour on its own layer, this goes non-empty.

    Note the `stack_adjacent` flag: In1.Cu/B.Cu is NOT adjacent on this
    4-layer stack (In2.Cu sits between them), which is why it is reported
    rather than assumed."""
    res = router.audit_plane_islands(str(kiln_project_path))
    by_net = {e["net"]: e for e in res["cross_layer"]}
    assert {"GND_Main", "GND_Safty"} <= set(by_net)
    for net in ("GND_Main", "GND_Safty"):
        entry = by_net[net]
        assert len(entry["layers"]) >= 2
        assert entry["layer_pairs"]
        for pair in entry["layer_pairs"]:
            assert pair["bonding_via_count"] >= 2
            assert pair["weakly_coupled"] is False
    assert res["summary"]["weakly_coupled_layer_pairs"] == []

    fcu_in1 = next(p for p in by_net["GND_Main"]["layer_pairs"]
                   if p["layers"] == ["F.Cu", "In1.Cu"])
    assert fcu_in1["stack_adjacent"] is True
