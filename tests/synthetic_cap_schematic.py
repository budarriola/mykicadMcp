"""Minimal synthetic schematic+netlist+board generator for Phase 8's
net-aware capacitor voltage audit (`audit_capacitor_net_voltages` in
`kicad_pcb_tool.py`).

`synthetic_board.py` already covers `.kicad_pcb`/`.kicad_pro`/`.net` shapes
for netlist-only tools, but nothing in the test suite yet exercises the
`.kicad_sch` reader (`_flatten_schematic_components` / `list_schematic_parts`
/ `audit_capacitor_voltages`), which Phase 8 also depends on for each cap's
Value/Footprint/DNP flag. This module writes a tiny, flat (no sub-sheets)
schematic alongside a matching board and netlist so
`_resolve_schematic_dir`/`_reachable_schematic_files` (schematic side) and
`_resolve_project_path`/`_parse_nets_cached` (board/netlist side) both
resolve against one synthetic project directory.

Reuses only the s-expression text shapes the repo's own parsers expect
(`_parse_one_schematic_symbol`, `_parse_nets`, `_parse_footprint_pads`) - no
KiCad runtime, no external deps.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from synthetic_board import _synthetic_kicad_pro_text, _net_table, _netlist_net_block

_SCH_HEADER = """(kicad_sch
    (version 20231120)
    (generator "synthetic_cap_schematic.py")
    (uuid "synthetic-root-uuid")
    (paper "A4")
"""

_SCH_FOOTER = ")\n"


def _cap_symbol_block(
    ref: str,
    value: str,
    footprint: str,
    dnp: bool,
    pin_count: int,
    index: int,
) -> str:
    """A placed `(symbol ...)` block satisfying `_parse_one_schematic_symbol`
    (needs both `lib_id` and `instances` to be recognized as a placed
    component by `_parse_schematic_symbols`). `pin_count` pins are emitted
    (2 for a normal capacitor; a different count exercises the
    `unsupported_pins` edge case)."""
    pins = "\n".join(f'        (pin "{p}" (uuid "synthetic-cap-pin-{index:04d}-{p}"))' for p in range(1, pin_count + 1))
    dnp_text = "yes" if dnp else "no"
    return f"""    (symbol
        (lib_id "Device:C")
        (at {index * 10.0} 50.0 0)
        (unit 1)
        (in_bom yes)
        (on_board yes)
        (dnp {dnp_text})
        (uuid "synthetic-cap-symbol-{index:04d}")
        (property "Reference" "{ref}" (at {index * 10.0} 48.5 0))
        (property "Value" "{value}" (at {index * 10.0} 51.5 0))
        (property "Footprint" "{footprint}" (at {index * 10.0} 53.0 0))
{pins}
        (instances
            (project "synthetic"
                (path "/synthetic-root-uuid"
                    (reference "{ref}")
                    (unit 1)
                )
            )
        )
    )
"""


def _cap_footprint_block(ref: str, footprint: str, x: float, index: int, nets: list[str]) -> str:
    """A board-side footprint with one pad per net in `nets` (pad numbers
    1..N matching the netlist node pins `write_synthetic_cap_project` emits),
    so the board's own pad nets agree with the `.net` export (no staleness
    warning) unless a test deliberately diverges them."""
    lines = [
        f'    (footprint "{footprint}"',
        '        (layer "F.Cu")',
        f'        (uuid "synthetic-cap-fp-{index:04d}")',
        f'        (at {x} 50.0)',
        f'        (property "Reference" "{ref}" (at 0 -1.5) (layer "F.SilkS"))',
    ]
    for i, net in enumerate(nets, start=1):
        px = -0.75 + (i - 1) * 1.5
        lines.append(
            f'        (pad "{i}" smd rect (at {px} 0) (size 0.9 0.95) '
            f'(layers "F.Cu" "F.Paste" "F.Mask") (net "{net}"))'
        )
    lines.append("    )\n")
    return "\n".join(lines)


def write_synthetic_cap_project(
    directory: Path,
    project_name: str,
    capacitors: list[dict[str, Any]],
) -> dict[str, Path]:
    """Write a full synthetic project - `.kicad_sch` + `.kicad_pcb` +
    `.kicad_pro` + `.net` - all named `project_name`, containing one
    capacitor symbol per entry in `capacitors`.

    Each entry: `{"ref": "C1", "value": "0.1uF 16V", "footprint": "cap:C_0603",
    "nets": ["12V_Main", "GND_Main"], "dnp": False}`. `nets` length controls
    pin count (2 for a normal capacitor; use e.g. 3 or 4 nets to exercise the
    `unsupported_pins` verdict; a `nets` list with fewer than 2 entries is
    also accepted, e.g. `[]` to simulate a reference absent from the
    netlist).

    Board pad nets always match the netlist nodes 1:1 (no staleness by
    construction) - the caller can still hand-craft a mismatch by mutating
    the returned board text/file directly if a staleness-warning test is
    ever added.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    board_path = directory / f"{project_name}.kicad_pcb"
    project_path = directory / f"{project_name}.kicad_pro"
    netlist_path = directory / f"{project_name}.net"
    sch_path = directory / f"{project_name}.kicad_sch"

    # --- schematic ---
    sch_parts = [_SCH_HEADER]
    for i, cap in enumerate(capacitors, start=1):
        nets = cap.get("nets", [])
        pin_count = max(len(nets), 1)  # a symbol needs at least one pin block
        sch_parts.append(
            _cap_symbol_block(
                cap["ref"],
                cap.get("value", ""),
                cap.get("footprint", "cap:C_0603"),
                bool(cap.get("dnp", False)),
                pin_count,
                i,
            )
        )
    sch_parts.append(_SCH_FOOTER)
    sch_path.write_text("".join(sch_parts), encoding="utf-8")

    # --- board (all net names across every cap, deduped, in the net table) ---
    all_nets: list[str] = []
    seen: set[str] = set()
    for cap in capacitors:
        for net in cap.get("nets", []):
            if net not in seen:
                seen.add(net)
                all_nets.append(net)

    board_parts = [
        "(kicad_pcb\n    (version 20221018)\n    (generator \"synthetic_cap_schematic.py\")\n"
        "    (general\n        (thickness 1.6)\n    )\n    (paper \"A4\")\n"
        "    (layers\n        (0 \"F.Cu\" signal)\n        (31 \"B.Cu\" signal)\n    )\n"
        "    (setup\n        (pad_to_mask_clearance 0)\n    )\n",
        _net_table(all_nets),
    ]
    for i, cap in enumerate(capacitors, start=1):
        nets = cap.get("nets", [])
        if not nets:
            continue  # nothing to place on the board for a cap absent from the netlist
        x = i * 5.0
        board_parts.append(
            _cap_footprint_block(cap["ref"], cap.get("footprint", "cap:C_0603"), x, i, nets)
        )
    board_parts.append(")\n")
    board_path.write_text("".join(board_parts), encoding="utf-8")

    # --- project + netlist ---
    project_path.write_text(_synthetic_kicad_pro_text(), encoding="utf-8")

    net_parts = [
        "(export",
        '  (version "E")',
        "  (design",
        '    (source "synthetic.kicad_sch")',
        '    (date "2026-07-21")',
        '    (tool "synthetic_cap_schematic.py")',
        "  )",
        "  (components",
    ]
    for cap in capacitors:
        net_parts.append(f'    (comp (ref "{cap["ref"]}") (value "{cap.get("value", "")}") (footprint "{cap.get("footprint", "cap:C_0603")}"))')
    net_parts.append("  )")
    net_parts.append("  (nets")
    # group by net name so a net shared by two caps' pins ends up as one <net> block
    net_nodes: dict[str, list[tuple[str, str]]] = {}
    for cap in capacitors:
        for pin_idx, net in enumerate(cap.get("nets", []), start=1):
            net_nodes.setdefault(net, []).append((cap["ref"], str(pin_idx)))
    for code, (net, nodes) in enumerate(net_nodes.items(), start=1):
        net_parts.extend(_netlist_net_block(code, net, nodes))
    net_parts.append("  )")
    net_parts.append(")")
    netlist_path.write_text("\n".join(net_parts) + "\n", encoding="utf-8")

    return {"board": board_path, "project": project_path, "netlist": netlist_path, "schematic": sch_path}
