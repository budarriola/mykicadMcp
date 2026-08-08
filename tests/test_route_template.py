"""Tests for `diff_route_template`/`apply_route_template` (Group 5 route
templates - cloning a reference hierarchical channel's own hand-routed
copper onto its sibling instances, the routing counterpart to
`diff_layout_template`/`apply_layout_template`).

Board fixture: two hierarchical-group instances (U1/D1 = template,
U2/D2 = target) plus one unrelated component D3 tied to a shared GND net
that also reaches D1 and D2 - this is what exercises the "only per-instance
nets get cloned" scoping rule (`_local_group_nets`), the actual reason this
tool exists instead of "just copy every segment near the template anchor."
"""

from __future__ import annotations

from pathlib import Path

import kicad_pcb_tool as pcb
from synthetic_board import _FOOTER, _HEADER_TEMPLATE, _layer_stack_lines

t = "\t"  # real kiln.kicad_pcb format: tab-indented, footprint/segment/via
# blocks each with their own (uuid ...) - required by _footprint_block_span
# and friends, unlike the space-indented boards synthetic_board.py's other
# helpers build.


def _pad(num: str, x: float, y: float, net: str, uuid: str) -> str:
    return (
        f'{t}{t}(pad "{num}" smd rect\n'
        f'{t}{t}{t}(at {x} {y})\n'
        f'{t}{t}{t}(size 1 1)\n'
        f'{t}{t}{t}(layers "F.Cu" "F.Paste" "F.Mask")\n'
        f'{t}{t}{t}(net "{net}")\n'
        f'{t}{t}{t}(uuid "{uuid}")\n'
        f'{t}{t})'
    )


def _footprint(ref: str, x: float, y: float, uuid: str, pads: list[str], hier: tuple[str, str] | None = None) -> str:
    body = "\n".join(pads)
    hier_lines = ""
    if hier is not None:
        symbol_uuid, sheet_instance = hier
        hier_lines = (
            f'{t}{t}(path "/root-sheet-uuid/{sheet_instance}/{symbol_uuid}")\n'
            f'{t}{t}(sheetname "/Test/")\n'
            f'{t}{t}(sheetfile "test.kicad_sch")\n'
        )
    return (
        f'{t}(footprint "synthetic:PART"\n'
        f'{t}{t}(layer "F.Cu")\n'
        f'{t}{t}(uuid "{uuid}")\n'
        f'{t}{t}(at {x} {y})\n'
        f'{t}{t}(property "Reference" "{ref}" (at 0 -1.5) (layer "F.SilkS"))\n'
        f'{t}{t}(property "Value" "V" (at 0 1.5) (layer "F.Fab"))\n'
        f'{body}\n'
        f'{hier_lines}'
        f'{t})\n'
    )


def _segment(x1: float, y1: float, x2: float, y2: float, net: str, uuid: str, layer: str = "F.Cu", width: float = 0.25) -> str:
    return (
        f'{t}(segment\n'
        f'{t}{t}(start {x1} {y1})\n'
        f'{t}{t}(end {x2} {y2})\n'
        f'{t}{t}(width {width})\n'
        f'{t}{t}(layer "{layer}")\n'
        f'{t}{t}(net "{net}")\n'
        f'{t}{t}(uuid "{uuid}")\n'
        f'{t})\n'
    )


def _net_block(code: int, name: str, nodes: list[tuple[str, str]]) -> list[str]:
    lines = [f'    (net (code "{code}") (name "{name}") (class "Default")']
    for ref, pin in nodes:
        lines.append(f'      (node (ref "{ref}") (pin "{pin}"))')
    lines.append("    )")
    return lines


def _build_project(tmp_path: Path, target_has_own_net: bool = True) -> Path:
    """U1 (anchor) + D1 (member) = template group; U2 (anchor) + D2 (member) =
    target group; D3 = unrelated, shares GND with D1/D2. D1/D2 pin 1 is each
    on its own per-instance net (Net-(D1-A)/Net-(D2-A)); pin 2 is the shared
    GND net.

    Two routed segments on the template side: one on Net-(D1-A) (must be
    cloned), one on GND (must NOT be cloned - it's not a per-instance net).

    `target_has_own_net=False` drops D2's own net entirely (both its pads on
    GND) - the `unmapped_net` case, where D1's per-instance net has no real
    counterpart on the target's schematic.
    """
    anchor_symbol = "sym-anchor-0001"
    diode_symbol = "sym-diode-0001"

    d2_net_a = "Net-(D2-A)" if target_has_own_net else "GND"

    parts = [
        _footprint("U1", 0.0, 0.0, "fp-u1", [_pad("1", 0, 0, "", "pad-u1-1")], hier=(anchor_symbol, "inst-template")),
        _footprint(
            "D1", 10.0, 0.0, "fp-d1",
            [_pad("1", -1, 0, "Net-(D1-A)", "pad-d1-1"), _pad("2", 1, 0, "GND", "pad-d1-2")],
            hier=(diode_symbol, "inst-template"),
        ),
        _footprint("U2", 0.0, 30.0, "fp-u2", [_pad("1", 0, 0, "", "pad-u2-1")], hier=(anchor_symbol, "inst-target")),
        _footprint(
            "D2", 10.0, 30.0, "fp-d2",
            [_pad("1", -1, 0, d2_net_a, "pad-d2-1"), _pad("2", 1, 0, "GND", "pad-d2-2")],
            hier=(diode_symbol, "inst-target"),
        ),
        _footprint("D3", 50.0, 50.0, "fp-d3", [_pad("1", 0, 0, "GND", "pad-d3-1")]),
        _segment(9.0, 0.0, 9.0, 2.0, "Net-(D1-A)", "seg-net-a"),
        _segment(11.0, 0.0, 11.0, 2.0, "GND", "seg-gnd"),
    ]
    header = _HEADER_TEMPLATE.format(layer_lines=_layer_stack_lines(2))
    board_path = tmp_path / "test.kicad_pcb"
    board_path.write_text(header + "".join(parts) + _FOOTER, encoding="utf-8")

    net_lines = [
        "(export",
        '  (version "E")',
        "  (design",
        '    (source "test.kicad_sch")',
        '    (date "2026-08-07")',
        '    (tool "test_route_template.py")',
        "  )",
        "  (components",
        '    (comp (ref "U1") (value "V") (footprint "synthetic:PART"))',
        '    (comp (ref "D1") (value "V") (footprint "synthetic:PART"))',
        '    (comp (ref "U2") (value "V") (footprint "synthetic:PART"))',
        '    (comp (ref "D2") (value "V") (footprint "synthetic:PART"))',
        '    (comp (ref "D3") (value "V") (footprint "synthetic:PART"))',
        "  )",
        "  (nets",
    ]
    code = 1
    net_lines += _net_block(code, "Net-(D1-A)", [("D1", "1")])
    code += 1
    if target_has_own_net:
        net_lines += _net_block(code, "Net-(D2-A)", [("D2", "1")])
        code += 1
    gnd_nodes = [("D1", "2"), ("D2", "2"), ("D3", "1")]
    net_lines += _net_block(code, "GND", gnd_nodes)
    net_lines += ["  )", ")"]
    (tmp_path / "test.net").write_text("\n".join(net_lines) + "\n", encoding="utf-8")

    return board_path


def test_local_group_nets_excludes_shared_rail(tmp_path):
    board = _build_project(tmp_path)
    group = pcb.get_hierarchical_group(board, "U1")
    local = pcb._local_group_nets(board, group)
    assert set(local.keys()) == {"Net-(D1-A)"}


def test_diff_route_template_clones_only_local_net(tmp_path):
    board = _build_project(tmp_path)
    result = pcb.diff_route_template(board, "U1", "U2")
    assert result["to_add_count"] == 1
    change = result["to_add"][0]
    assert change["kind"] == "segment"
    assert change["template_net"] == "Net-(D1-A)"
    assert change["net"] == "Net-(D2-A)"
    # translated by the U1->U2 anchor offset (dx=0, dy=30, no rotation)
    assert change["start"] == {"x": 9.0, "y": 30.0}
    assert change["end"] == {"x": 9.0, "y": 32.0}
    assert result["unmapped_net_count"] == 0
    assert result["already_present_count"] == 0


def test_diff_route_template_reports_unmapped_net(tmp_path):
    board = _build_project(tmp_path, target_has_own_net=False)
    result = pcb.diff_route_template(board, "U1", "U2")
    assert result["to_add_count"] == 0
    assert result["unmapped_net_count"] == 1
    assert result["unmapped_net"][0]["template_net"] == "Net-(D1-A)"
    assert result["unmapped_net"][0]["mapped_net"] == "Net-(D2-A)"


def test_apply_route_template_writes_copper_and_is_idempotent(tmp_path):
    board = _build_project(tmp_path)
    result = pcb.apply_route_template(board, "U1", ["U2"], write=True)
    assert result["added_count"] == 1

    tracks = pcb._parse_tracks(board)
    matches = [s for s in tracks["segments"] if s["net"] == "Net-(D2-A)"]
    assert len(matches) == 1
    assert matches[0]["start"] == {"x": 9.0, "y": 30.0}
    assert matches[0]["end"] == {"x": 9.0, "y": 32.0}

    # GND never got cloned onto the target - only its own D2 pads' copper (none
    # routed in this fixture), never the shared-rail segment from the template.
    gnd_on_target_area = [
        s for s in tracks["segments"]
        if s["net"] == "GND" and 25.0 <= s["start"]["y"] <= 35.0
    ]
    assert gnd_on_target_area == []

    # Re-running now reports the same copper as already_present, not to_add.
    after = pcb.diff_route_template(board, "U1", "U2")
    assert after["to_add_count"] == 0
    assert after["already_present_count"] == 1
