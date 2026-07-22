"""Tests for the Phase 7.3 connectivity model and `get_ratsnest`
(`kicad_router_tool`).

Uses tiny purpose-built synthetic boards (full control over pads/copper/vias so
each net's island decomposition is known exactly) plus the real kiln board for
the no-new-files-in-project-root guard. Reuses the repo's synthetic layer-stack
helper so the boards parse identically to how KiCad-generated ones do.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import kicad_pcb_tool as pcb
import kicad_router_tool as router
from synthetic_board import _layer_stack_lines, _HEADER_TEMPLATE, _FOOTER


# --------------------------------------------------------------------------- #
# Minimal board builder (pads on chosen layers/nets/sizes, segments, vias)
# --------------------------------------------------------------------------- #

def _pad(num: str, x: float, y: float, net: str, layer: str = "F.Cu", size: float = 0.9,
         thru: bool = False) -> str:
    if thru:
        kind = "thru_hole"
        layers = '"*.Cu" "*.Mask"'
        drill = " (drill 0.4)"
    else:
        kind = "smd"
        layers = f'"{layer}" "{layer[0]}.Paste" "{layer[0]}.Mask"'
        drill = ""
    return (f'        (pad "{num}" {kind} rect (at {x} {y}) (size {size} {size}){drill} '
            f'(layers {layers}) (net "{net}"))')


def _footprint(ref: str, x: float, y: float, uuid: str, pads: list[str]) -> str:
    body = "\n".join(pads)
    return (f'    (footprint "synthetic:BLD"\n'
            f'        (layer "F.Cu")\n'
            f'        (uuid "{uuid}")\n'
            f'        (at {x} {y})\n'
            f'        (property "Reference" "{ref}" (at 0 -1.5) (layer "F.SilkS"))\n'
            f'        (property "Value" "V" (at 0 1.5) (layer "F.Fab"))\n'
            f'{body}\n'
            f'    )\n')


def _segment(x1: float, y1: float, x2: float, y2: float, net: str, uuid: str,
             layer: str = "F.Cu", width: float = 0.25) -> str:
    return (f'    (segment (start {x1} {y1}) (end {x2} {y2}) (width {width}) '
            f'(layer "{layer}") (net "{net}") (uuid "{uuid}"))\n')


def _via(x: float, y: float, net: str, uuid: str, layers: tuple[str, str] = ("F.Cu", "B.Cu"),
         size: float = 0.6, drill: float = 0.3) -> str:
    return (f'    (via (at {x} {y}) (size {size}) (drill {drill}) '
            f'(layers "{layers[0]}" "{layers[1]}") (net "{net}") (uuid "{uuid}"))\n')


def _write_board(path: Path, body_parts: list[str], layers: int = 2) -> Path:
    header = _HEADER_TEMPLATE.format(layer_lines=_layer_stack_lines(layers))
    path.write_text(header + "".join(body_parts) + _FOOTER, encoding="utf-8")
    return path


def _rats(board_path: Path, **kw):
    return router.get_ratsnest(str(board_path), **kw)


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #

def test_half_routed_net_one_connection(tmp_path: Path):
    """3 collinear pads on one net, P1-P2 joined by a trace, P3 left free:
    exactly 2 islands -> 1 missing connection, airline == the P2->P3 gap."""
    board = tmp_path / "half.kicad_pcb"
    _write_board(board, [
        _footprint("R1", 0, 0, "fp-1", [
            _pad("1", 10.0, 10.0, "HALF"),
            _pad("2", 20.0, 10.0, "HALF"),
            _pad("3", 30.0, 10.0, "HALF"),
        ]),
        _segment(10.0, 10.0, 20.0, 10.0, "HALF", "seg-1"),  # joins pad1<->pad2
    ])
    res = _rats(board)
    conns = [c for c in res["connections"] if c["net"] == "HALF"]
    assert len(conns) == 1
    # nearest island points: {P1,P2,trace} to {P3} => P2(20,10)->P3(30,10) = 10mm
    assert conns[0]["airline_length_mm"] == pytest.approx(10.0, abs=1e-3)
    assert res["per_net"]["HALF"]["island_count"] == 2
    assert "HALF" in res["unrouted_nets"]


def test_fully_routed_net_zero_connections(tmp_path: Path):
    """Same 3 pads, fully daisy-chained P1-P2-P3: one island, 0 connections."""
    board = tmp_path / "full.kicad_pcb"
    _write_board(board, [
        _footprint("R1", 0, 0, "fp-1", [
            _pad("1", 10.0, 10.0, "FULL"),
            _pad("2", 20.0, 10.0, "FULL"),
            _pad("3", 30.0, 10.0, "FULL"),
        ]),
        _segment(10.0, 10.0, 20.0, 10.0, "FULL", "seg-1"),
        _segment(20.0, 10.0, 30.0, 10.0, "FULL", "seg-2"),
    ])
    res = _rats(board)
    assert [c for c in res["connections"] if c["net"] == "FULL"] == []
    assert res["per_net"]["FULL"]["island_count"] == 1
    assert res["per_net"]["FULL"]["status"] == "routed"
    assert "FULL" not in res["unrouted_nets"]
    assert res["summary"]["fully_routed_net_count"] >= 1


def test_unrouted_three_pad_net_two_mst_connections_no_cycle(tmp_path: Path):
    """3 pads, no copper: 3 islands -> exactly 2 MST connections (a tree, not
    3 = a cycle), spanning all three pads."""
    board = tmp_path / "three.kicad_pcb"
    _write_board(board, [
        _footprint("R1", 0, 0, "fp-1", [_pad("1", 0.0, 0.0, "TRI")]),
        _footprint("R2", 0, 0, "fp-2", [_pad("1", 10.0, 0.0, "TRI")]),
        _footprint("R3", 0, 0, "fp-3", [_pad("1", 0.0, 10.0, "TRI")]),
    ])
    res = _rats(board)
    conns = [c for c in res["connections"] if c["net"] == "TRI"]
    assert len(conns) == 2  # 3 islands - 1, no cycle
    refs = set()
    for c in conns:
        refs.add(c["from"]["ref"])
        refs.add(c["to"]["ref"])
    assert refs == {"R1", "R2", "R3"}  # the tree spans every pad


def test_via_joined_cross_layer_one_island(tmp_path: Path):
    """F.Cu pad -> F.Cu trace -> via(F/B) -> B.Cu trace -> B.Cu pad: the via
    ties the two layers, so the whole net is a single island (0 connections)."""
    board = tmp_path / "via.kicad_pcb"
    _write_board(board, [
        _footprint("U1", 0, 0, "fp-1", [_pad("1", 0.0, 0.0, "XL", layer="F.Cu")]),
        _footprint("U2", 0, 0, "fp-2", [_pad("1", 10.0, 0.0, "XL", layer="B.Cu")]),
        _segment(0.0, 0.0, 5.0, 0.0, "XL", "seg-f", layer="F.Cu"),
        _via(5.0, 0.0, "XL", "via-1", layers=("F.Cu", "B.Cu")),
        _segment(5.0, 0.0, 10.0, 0.0, "XL", "seg-b", layer="B.Cu"),
    ])
    res = _rats(board)
    assert res["per_net"]["XL"]["island_count"] == 1
    assert [c for c in res["connections"] if c["net"] == "XL"] == []

    # And WITHOUT the via the two layers must NOT merge (guards the via logic):
    board2 = tmp_path / "novia.kicad_pcb"
    _write_board(board2, [
        _footprint("U1", 0, 0, "fp-1", [_pad("1", 0.0, 0.0, "XL", layer="F.Cu")]),
        _footprint("U2", 0, 0, "fp-2", [_pad("1", 10.0, 0.0, "XL", layer="B.Cu")]),
        _segment(0.0, 0.0, 5.0, 0.0, "XL", "seg-f", layer="F.Cu"),
        _segment(5.0, 0.0, 10.0, 0.0, "XL", "seg-b", layer="B.Cu"),
    ])
    res2 = _rats(board2)
    assert res2["per_net"]["XL"]["island_count"] == 2


def test_net_overrides_priority_reorders(tmp_path: Path):
    """Two unrouted nets: SHORT (5mm airline) and LONG (40mm). Default ordering
    is shortest-first (SHORT leads). A board-local net_overrides.priority on
    LONG must pull it ahead - priority wins over airline length."""
    board = tmp_path / "prio.kicad_pcb"
    _write_board(board, [
        _footprint("A1", 0, 0, "fp-a1", [_pad("1", 0.0, 0.0, "SHORT")]),
        _footprint("A2", 0, 0, "fp-a2", [_pad("1", 5.0, 0.0, "SHORT")]),
        _footprint("B1", 0, 0, "fp-b1", [_pad("1", 0.0, 20.0, "LONG")]),
        _footprint("B2", 0, 0, "fp-b2", [_pad("1", 40.0, 20.0, "LONG")]),
    ])
    # Default: SHORT (5mm) sorts before LONG (40mm).
    default_first = _rats(board)["connections"][0]["net"]
    assert default_first == "SHORT"

    # Give LONG a higher priority via the board-local JSON.
    pcb.save_board_local(str(board), {"version": 1, "net_overrides": {"LONG": {"priority": 10}}})
    # Cache is keyed by board mtime/size; the JSON is a separate file, but be safe.
    res = _rats(board)
    assert res["connections"][0]["net"] == "LONG"
    assert res["connections"][0]["priority"] == 10.0


def test_single_pad_and_free_copper_counted_separately(tmp_path: Path):
    """A one-pad net contributes 0 connections and is counted as single_pad;
    empty-net copper is free_copper, never ratsnested."""
    board = tmp_path / "solo.kicad_pcb"
    _write_board(board, [
        _footprint("R1", 0, 0, "fp-1", [_pad("1", 0.0, 0.0, "SOLO")]),
        _via(50.0, 50.0, "", "freevia-1"),  # empty-net free via
    ])
    res = _rats(board)
    assert "SOLO" in res["single_pad_nets"]
    assert res["per_net"]["SOLO"]["missing_connections"] == 0
    assert res["summary"]["free_copper_items"]["vias"] == 1
    assert res["summary"]["total_connections"] == 0


def test_no_new_files_in_project_root(kiln_project_path: Path):
    """get_ratsnest is read-only: running it on the real project must not
    create kiln.board_local.json or any other file in the project root."""
    before = set(os.listdir(kiln_project_path))
    res = router.get_ratsnest(str(kiln_project_path))
    after = set(os.listdir(kiln_project_path))
    assert before == after, f"new files appeared: {after - before}"
    assert "kiln.board_local.json" not in after
    # Sanity: it actually ran and produced a plausible (not hundreds) result.
    assert res["summary"]["total_connections"] < 100
    assert res["summary"]["fully_routed_net_count"] > 0


def test_kiln_plane_nets_connect_through_fill(kiln_project_path: Path):
    """Regression for the false-split failure mode: on the real board GND_Safty
    connects through its zone fill (0 missing connections), NOT the dozens of
    phantom island-per-pad connections a fill-blind model produces."""
    res = router.get_ratsnest(str(kiln_project_path), nets=["GND_Safty"])
    gnd = [c for c in res["connections"] if c["net"] == "GND_Safty"]
    assert len(gnd) == 0
