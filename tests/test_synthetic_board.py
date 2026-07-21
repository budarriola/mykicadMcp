"""Smoke test: the synthetic board generator (tests/synthetic_board.py) emits
text the repo's own kicad_pcb_tool parser can read back. See synthetic_board.py
module docstring for what is still TODO on the generator itself.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import kicad_pcb_tool as k

from synthetic_board import (
    generate_fanout_field_board,
    generate_synthetic_board,
    write_fanout_field_board,
    write_synthetic_board,
)


def test_generated_board_parses_with_repo_parser(tmp_path: Path) -> None:
    board_path = write_synthetic_board(tmp_path / "synthetic.kicad_pcb", component_count=6)
    components = k._parse_board_components(board_path)
    assert len(components) == 6
    refs = {c["reference"] for c in components}
    assert refs == {f"R{i}" for i in range(1, 7)}


def test_generated_board_pads_parse(tmp_path: Path) -> None:
    board_path = write_synthetic_board(tmp_path / "synthetic.kicad_pcb", component_count=3)
    pads = k._parse_footprint_pads(board_path)
    assert len(pads) == 3
    for fp in pads.values():
        assert len(fp["pads"]) == 2


def test_unrouted_mode_emits_no_segments(tmp_path: Path) -> None:
    text = generate_synthetic_board(component_count=4, route=False)
    assert "(segment" not in text
    board_path = tmp_path / "unrouted.kicad_pcb"
    board_path.write_text(text, encoding="utf-8")
    components = k._parse_board_components(board_path)
    assert len(components) == 4


def test_default_two_layer_stack_unchanged(tmp_path: Path) -> None:
    text = generate_synthetic_board(component_count=2)
    assert '(0 "F.Cu" signal)' in text
    assert '(31 "B.Cu" signal)' in text
    assert "In1.Cu" not in text
    board_path = write_synthetic_board(tmp_path / "two_layer.kicad_pcb", component_count=2)
    assert len(k._parse_board_components(board_path)) == 2


def test_n_layer_stack_emits_inner_layers_with_types(tmp_path: Path) -> None:
    text = generate_synthetic_board(component_count=2, layers=6)
    for i in range(1, 5):
        assert f'"In{i}.Cu"' in text
    assert '(1 "In1.Cu" signal)' in text
    assert '(2 "In2.Cu" power)' in text
    assert '(3 "In3.Cu" signal)' in text
    assert '(4 "In4.Cu" power)' in text
    # Board must still parse cleanly with the repo's own component parser.
    board_path = write_synthetic_board(tmp_path / "six_layer.kicad_pcb", component_count=5, layers=6)
    components = k._parse_board_components(board_path)
    assert len(components) == 5


def test_net_table_emitted_for_all_component_nets(tmp_path: Path) -> None:
    text = generate_synthetic_board(component_count=3)
    assert '(net 0 "")' in text
    assert '(net 1 "NET_1_A")' in text
    assert '(net 6 "NET_3_B")' in text
    board_path = write_synthetic_board(tmp_path / "nettable.kicad_pcb", component_count=3)
    pads = k._parse_footprint_pads(board_path)
    assert len(pads) == 3


def test_scale_multiplies_component_count(tmp_path: Path) -> None:
    board_path = write_synthetic_board(
        tmp_path / "scaled.kicad_pcb", component_count=26, scale=10, route=False
    )
    components = k._parse_board_components(board_path)
    # 10x kiln-scale ratsnest: many unrouted components, no segments.
    assert len(components) == 260
    text = board_path.read_text(encoding="utf-8")
    assert "(segment" not in text


def test_segment_and_pad_nets_are_referenced_by_name_not_index(tmp_path: Path) -> None:
    """Regression test: verified against the real kiln.kicad_pcb (KiCad 10)
    that pads/segments/vias reference nets BY NAME ONLY - `(net "X")`, never
    `(net <index> "X")`. The index form used to break both
    `kicad_pcb_tool._parse_tracks` (reads entry[1] as the net name verbatim,
    so it would silently read the index as the name) and `kicad-cli pcb drc`
    (refuses to load the file at all). The board-level `(net N "name")` table
    is the only place a numeric index legitimately appears.
    """
    text = generate_synthetic_board(component_count=2)
    assert '(net "NET_1_A")' in text
    assert '(net "NET_1_B")' in text
    # The board-level `(net N "name")` table legitimately has index+name; only
    # per-pad/segment references must be name-only, checked below via the
    # parser itself (which reads segment nets as literal `entry[1]` - an index
    # there would be silently misread as a net named "1").
    board_path = write_synthetic_board(tmp_path / "byname.kicad_pcb", component_count=2)
    tracks = k._parse_tracks(board_path)
    net_names = {seg["net"] for seg in tracks["segments"]}
    assert net_names == {"NET_1_A", "NET_2_A"}


# --- Dense fanout-field mode -------------------------------------------------


def test_fanout_field_board_parses_with_correct_pad_counts(tmp_path: Path) -> None:
    board_path = write_fanout_field_board(
        tmp_path / "fanout.kicad_pcb", component_count=3, pads_per_component=32
    )
    components = k._parse_board_components(board_path)
    assert len(components) == 3
    assert {c["reference"] for c in components} == {"U1", "U2", "U3"}

    pads = k._parse_footprint_pads(board_path)
    assert len(pads) == 3
    for fp in pads.values():
        assert len(fp["pads"]) == 32

    # Fanout fields are always unrouted (ratsnest benchmark input).
    text = board_path.read_text(encoding="utf-8")
    assert "(segment" not in text


@pytest.mark.parametrize("pad_count", [16, 48])
def test_fanout_field_pad_count_range(tmp_path: Path, pad_count: int) -> None:
    board_path = write_fanout_field_board(
        tmp_path / f"fanout_{pad_count}.kicad_pcb", component_count=2, pads_per_component=pad_count
    )
    pads = k._parse_footprint_pads(board_path)
    for fp in pads.values():
        assert len(fp["pads"]) == pad_count


def test_fanout_field_shares_bus_nets_across_components(tmp_path: Path) -> None:
    """Each pad position is a shared bus net across every component - the
    real ratsnest scenario 7.8's router benchmarks need (unlike the simple
    two-pad mode, where every net is isolated to one component).
    """
    board_path = write_fanout_field_board(
        tmp_path / "fanout_bus.kicad_pcb", component_count=4, pads_per_component=16
    )
    pads = k._parse_footprint_pads(board_path)
    nets_by_pad_number: dict[str, set[str]] = {}
    for fp in pads.values():
        for pad in fp["pads"]:
            nets_by_pad_number.setdefault(pad["number"], set()).add(pad["net"])
    assert len(nets_by_pad_number) == 16
    for pad_number, nets in nets_by_pad_number.items():
        # exactly one shared net name per pad slot, used by all 4 components
        assert nets == {f"FANOUT_{pad_number}"}


def test_fanout_field_rejects_invalid_counts() -> None:
    with pytest.raises(ValueError):
        generate_fanout_field_board(component_count=0)
    with pytest.raises(ValueError):
        generate_fanout_field_board(pads_per_component=0)
