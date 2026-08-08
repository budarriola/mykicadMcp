"""Regression test for the pad-geometry-mismatch detection added to
`diff_flip_template`/`apply_flip_template`.

Real-world bug this covers: kiln.kicad_pcb's three CurrentSense channel
instances (U8/U7/U9) all had a screw-terminal/jack connector (SJ2-35813B-
SMT-TR) on F.Cu with identical footprint-level position/rotation, but one
instance's individual rect pads each carried an extra 90-degree local
rotation baked into their own `(at x y <angle>)` line and the other two
didn't - invisible to a position/layer diff (same layer, same footprint
`at` rotation), but visibly wrong on the board since it flips which way
each elongated pad's long axis points. `diff_flip_template` used to gate
entirely on `template_layer != target_layer`, so it never saw this. It now
also compares per-pad local rotation (mod 180, since rect/roundrect/oval/
circle pads are point-symmetric and a 180-apart pair draws identically -
only a non-180 mismatch is a real visual bug).
"""

from __future__ import annotations

from pathlib import Path

import kicad_pcb_tool as pcb
from synthetic_board import _FOOTER, _HEADER_TEMPLATE, _layer_stack_lines


t = "\t"  # real kiln.kicad_pcb (and _footprint_block_span/_footprint_block_meta's
# regexes) use tab indentation, not spaces - unlike the space-indented boards
# synthetic_board.py's other helpers build (those never go through
# _footprint_block_span, so it never mattered before this test).


def _pad(
    num: str,
    x: float,
    y: float,
    net: str,
    uuid: str,
    angle: float | None = None,
    size: tuple[float, float] = (1.5, 2.75),
) -> str:
    # Multi-line, with its own (uuid ...), matching real KiCad's per-pad shape
    # (see e.g. kiln.kicad_pcb's own J12/J13 pads) - apply_flip_template's
    # uuid-remapping and net-field regex both require this, not the
    # single-line shape other synthetic_board.py fixtures use (those never
    # go through apply_flip_template/_footprint_block_span).
    at = f"(at {x} {y} {angle})" if angle else f"(at {x} {y})"
    return (
        f'{t}{t}(pad "{num}" smd rect\n'
        f'{t}{t}{t}{at}\n'
        f'{t}{t}{t}(size {size[0]} {size[1]})\n'
        f'{t}{t}{t}(layers "F.Cu" "F.Paste" "F.Mask")\n'
        f'{t}{t}{t}(net "{net}")\n'
        f'{t}{t}{t}(uuid "{uuid}")\n'
        f'{t}{t})'
    )


def _footprint(
    ref: str,
    x: float,
    y: float,
    rot: float,
    uuid: str,
    symbol_uuid: str,
    sheet_instance: str,
    pads: list[str],
) -> str:
    body = "\n".join(pads)
    at = f"(at {x} {y} {rot})" if rot else f"(at {x} {y})"
    return (
        f'{t}(footprint "synthetic:JACK"\n'
        f'{t}{t}(layer "F.Cu")\n'
        f'{t}{t}(uuid "{uuid}")\n'
        f'{t}{t}{at}\n'
        f'{t}{t}(property "Reference" "{ref}" (at 0 -3) (layer "F.SilkS"))\n'
        f'{t}{t}(property "Value" "JACK" (at 0 3) (layer "F.Fab"))\n'
        f'{body}\n'
        f'{t}{t}(path "/root-sheet-uuid/{sheet_instance}/{symbol_uuid}")\n'
        f'{t}{t}(sheetname "/Test/")\n'
        f'{t}{t}(sheetfile "test.kicad_sch")\n'
        f'{t})\n'
    )


def _anchor(ref: str, x: float, y: float, uuid: str, symbol_uuid: str, sheet_instance: str) -> str:
    return (
        f'{t}(footprint "synthetic:ANCHOR"\n'
        f'{t}{t}(layer "F.Cu")\n'
        f'{t}{t}(uuid "{uuid}")\n'
        f'{t}{t}(at {x} {y})\n'
        f'{t}{t}(property "Reference" "{ref}" (at 0 -1.5) (layer "F.SilkS"))\n'
        f'{t}{t}(property "Value" "ANCHOR" (at 0 1.5) (layer "F.Fab"))\n'
        f'{t}{t}(pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Paste" "F.Mask") (net ""))\n'
        f'{t}{t}(path "/root-sheet-uuid/{sheet_instance}/{symbol_uuid}")\n'
        f'{t}{t}(sheetname "/Test/")\n'
        f'{t}{t}(sheetfile "test.kicad_sch")\n'
        f'{t})\n'
    )


def _write_board(path: Path, parts: list[str]) -> Path:
    header = _HEADER_TEMPLATE.format(layer_lines=_layer_stack_lines(2))
    path.write_text(header + "".join(parts) + _FOOTER, encoding="utf-8")
    return path


def _build_board(tmp_path: Path, target_pad_angle: float | None, target_footprint_rot: float = 90.0) -> Path:
    """Two hierarchical-group instances (template role = U-anchor + J-jack):
    template group (U1/J1) has the jack's rect pads at local angle 90; target
    group (U2/J2) has whatever `target_pad_angle` says, both at F.Cu with the
    same footprint-level rotation (90) as the template.
    """
    anchor_symbol_uuid = "sym-anchor-0001"
    jack_symbol_uuid = "sym-jack-0001"
    parts = [
        _anchor("U1", 0.0, 0.0, "fp-u1", anchor_symbol_uuid, "inst-template"),
        _footprint(
            "J1", 5.0, 0.0, 90.0, "fp-j1", jack_symbol_uuid, "inst-template",
            [
                _pad("1", -2.5, 3.425, "GND", "pad-j1-1", angle=90.0),
                _pad("2", 4.3, 3.325, "TIP", "pad-j1-2", angle=90.0),
            ],
        ),
        _anchor("U2", 20.0, 0.0, "fp-u2", anchor_symbol_uuid, "inst-target"),
        _footprint(
            "J2", 25.0, 0.0, target_footprint_rot, "fp-j2", jack_symbol_uuid, "inst-target",
            [
                _pad("1", -2.5, 3.425, "GND2", "pad-j2-1", angle=target_pad_angle),
                _pad("2", 4.3, 3.325, "TIP2", "pad-j2-2", angle=target_pad_angle),
            ],
        ),
    ]
    return _write_board(tmp_path / "test.kicad_pcb", parts)


def test_diff_flip_template_catches_same_layer_pad_rotation_mismatch(tmp_path):
    """Target's jack pads have no local rotation (0) vs template's 90 - a real
    90-degree-off pad on an elongated rect shape - same layer both sides, so the
    old layer-only check would have missed this entirely."""
    board = _build_board(tmp_path, target_pad_angle=None)
    result = pcb.diff_flip_template(board, "U1", "U2")
    assert result["change_count"] == 1
    change = result["changes"][0]
    assert change["reference"] == "J2"
    assert change["from_layer"] == change["to_layer"] == "F.Cu"
    assert change["pad_geometry_mismatch"] is True


def test_diff_flip_template_ignores_180_apart_pad_rotation(tmp_path):
    """Target's pads are 180 degrees off from the template's (90 vs 270) - for
    a plain rect pad that's visually identical (point-symmetric), so this must
    NOT be flagged, even though the raw stored angle differs."""
    board = _build_board(tmp_path, target_pad_angle=270.0)
    result = pcb.diff_flip_template(board, "U1", "U2")
    assert result["change_count"] == 0
    assert result["changes"] == []


def test_diff_flip_template_matching_pads_no_change(tmp_path):
    """Target's pads already match the template (both 90) - nothing to do."""
    board = _build_board(tmp_path, target_pad_angle=90.0)
    result = pcb.diff_flip_template(board, "U1", "U2")
    assert result["change_count"] == 0


def test_apply_flip_template_clones_pad_geometry(tmp_path):
    """The detected mismatch actually gets fixed by apply_flip_template, and
    the target keeps its own identity (uuid, net names, position)."""
    board = _build_board(tmp_path, target_pad_angle=None)
    result = pcb.apply_flip_template(board, "U1", ["U2"], write=True)
    assert result["flipped_count"] == 1
    assert result["failed_count"] == 0

    after = pcb.diff_flip_template(board, "U1", "U2")
    assert after["change_count"] == 0

    pads = pcb.get_footprint_pads(board, "J2")["pads"]
    by_num = {p["number"]: p for p in pads}
    assert round(by_num["1"]["local_position"]["rotation"] % 180, 3) == 90.0
    # Net names and uuid stay the target's own, not cloned from the template.
    assert by_num["1"]["net"] == "GND2"
    assert by_num["2"]["net"] == "TIP2"
