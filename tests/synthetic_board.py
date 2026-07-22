"""Parameterized minimal `.kicad_pcb` writer for router/benchmark tests.

# TODO (M0 - remaining work):
#   - Acceptance: `tests/test_kicad_cli_acceptance.py` covers "KiCad itself
#     accepts a generated board" via `kicad-cli pcb drc` (skipped if kicad-cli
#     can't be found anywhere on the machine). A full "opens in the pcbnew GUI"
#     screenshot is still not automated (no headless GUI driver here), but the
#     DRC-load check exercises the same board-file parser pcbnew itself uses,
#     which is the meaningful part of that acceptance criterion.
#
# Done in this pass:
#   - Fixed a real format bug: segments/pads previously emitted
#     `(net <index> "<name>")`. Verified against the real `kiln.kicad_pcb`
#     (KiCad 10 format) that pads/segments/vias reference nets BY NAME ONLY
#     (`(net "<name>")`, no index) - the board-level `(net N "name")` table is
#     the only place an index appears. The old index-in-segment form also
#     silently broke `kicad_pcb_tool._parse_tracks` (which reads `entry[1]`
#     as the net name verbatim) and made `kicad-cli pcb drc` refuse to load
#     the file outright ("Failed to load board"). All emitters now match the
#     verified real shape.
#   - Dense fanout-field generation mode (`generate_fanout_field_board`):
#     many-pad (16-48+, BGA/QFP-grid-style) footprints, several placed in a
#     row, with shared bus-style nets (`FANOUT_<n>`) wiring the same pad
#     position across every component - an unrouted ratsnest field for
#     7.8's router/benchmark tests. Always unrouted by design (no segments).
#   - Companion file generation (`write_synthetic_project`): writes a matching
#     `.kicad_pro` (JSON, `net_settings.classes` Default block shaped like
#     kiln.kicad_pro's real one) and a `.net` netlist in the exact shape
#     `kicad_pcb_tool._parse_nets` reads (verified against kiln.net: an
#     `(export ... (nets (net (name "X") (node (ref "R1") (pin "1")) ...))))`
#     tree - `code`/`class` on the net and `value`/`footprint` on components
#     are included for realism but aren't required by the parser). Supports
#     both the "simple" two-pad-per-component board and the new "fanout" mode,
#     with node refs/pins matching the board's own footprint refs/pad numbers
#     1:1 so netlist-based tools (list_nets, get_component_connections,
#     detect_buses) run cleanly against a synthetic-only project directory.

Reuses only the s-expression text shapes the repo's own parser
(`kicad_pcb_tool._parse_board_components` / `_parse_footprint_pads` /
`_parse_nets`) expects - no KiCad runtime, no external deps.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

_HEADER_TEMPLATE = """(kicad_pcb
    (version 20221018)
    (generator "synthetic_board.py")
    (general
        (thickness 1.6)
    )
    (paper "A4")
    (layers
{layer_lines}
    )
    (setup
        (pad_to_mask_clearance 0)
    )
"""

_FOOTER = ")\n"


def _layer_stack_lines(layers: int) -> str:
    """Build the `(layers ...)` block entries for an N-layer copper stack.

    layers=2 -> F.Cu (0), B.Cu (31) only (unchanged 2-layer default).
    layers>2 -> F.Cu (0), In1.Cu..In(N-2).Cu (1..N-2, alternating
    signal/power), B.Cu (31), matching how KiCad numbers inner layers.
    """
    if layers < 2:
        raise ValueError("layers must be >= 2")
    lines = ['        (0 "F.Cu" signal)']
    inner_count = layers - 2
    for i in range(1, inner_count + 1):
        layer_type = "power" if i % 2 == 0 else "signal"
        lines.append(f'        ({i} "In{i}.Cu" {layer_type})')
    lines.append('        (31 "B.Cu" signal)')
    return "\n".join(lines)


def _net_table(net_names: list[str]) -> str:
    """Board-level `(net N "name")` table; net 0 is always the implicit
    unconnected/no-net entry KiCad itself always emits.

    Note: this table is a convenience index only - the real `kiln.kicad_pcb`
    (KiCad 10) doesn't emit one at all, and every pad/segment/via reference
    below is by NAME, never by this index. Kept because existing tests key
    off its presence and it's harmless (kicad-cli tolerates the extra block).
    """
    lines = ['    (net 0 "")']
    for idx, name in enumerate(net_names, start=1):
        lines.append(f'    (net {idx} "{name}")')
    return "\n".join(lines) + "\n"


def _footprint_block(ref: str, value: str, x: float, y: float, uuid: str, net_a: str, net_b: str) -> str:
    return f"""    (footprint "synthetic:R_0603"
        (layer "F.Cu")
        (uuid "{uuid}")
        (at {x} {y})
        (property "Reference" "{ref}" (at 0 -1.5) (layer "F.SilkS"))
        (property "Value" "{value}" (at 0 1.5) (layer "F.Fab"))
        (pad "1" smd rect (at -0.75 0) (size 0.9 0.95) (layers "F.Cu" "F.Paste" "F.Mask") (net "{net_a}"))
        (pad "2" smd rect (at 0.75 0) (size 0.9 0.95) (layers "F.Cu" "F.Paste" "F.Mask") (net "{net_b}"))
    )
"""


def _segment_block(x1: float, y1: float, x2: float, y2: float, width: float, layer: str, net: str, uuid: str) -> str:
    return f"""    (segment
        (start {x1} {y1})
        (end {x2} {y2})
        (width {width})
        (layer "{layer}")
        (net "{net}")
        (uuid "{uuid}")
    )
"""


def generate_synthetic_board(
    component_count: int = 10,
    spacing_mm: float = 5.0,
    track_width: float = 0.2,
    route: bool = True,
    layers: int = 2,
    scale: float = 1.0,
) -> str:
    """Build a minimal but parser-valid `.kicad_pcb` text with
    `round(component_count * scale)` two-pad footprints (R1..Rn), each on its
    own net pair, optionally connected by a single straight F.Cu segment
    (route=True).

    `layers` controls the copper stack size (2 = F.Cu/B.Cu only; >2 adds
    In1.Cu..In(N-2).Cu with alternating signal/power types). `scale` is a
    convenience multiplier on `component_count` for generating 10x/100x
    kiln-sized ratsnest boards (e.g. component_count=26, scale=10 -> 260
    components, matching the real board's ~259-component golden count) -
    combine with route=False for an unrouted ratsnest benchmark.

    See also `generate_fanout_field_board` for dense many-pad footprints, and
    `write_synthetic_project` for a full board + `.kicad_pro` + `.net` project.
    """
    n = round(component_count * scale)
    header = _HEADER_TEMPLATE.format(layer_lines=_layer_stack_lines(layers))
    net_names: list[str] = []
    for i in range(1, n + 1):
        net_names.append(f"NET_{i}_A")
        net_names.append(f"NET_{i}_B")

    parts = [header, _net_table(net_names)]
    for i in range(1, n + 1):
        ref = f"R{i}"
        net_a = f"NET_{i}_A"
        net_b = f"NET_{i}_B"
        x = i * spacing_mm
        y = 10.0
        fp_uuid = f"synth-fp-{i:06d}"
        parts.append(_footprint_block(ref, "10k", x, y, fp_uuid, net_a, net_b))
        if route:
            seg_uuid = f"synth-seg-{i:06d}"
            parts.append(_segment_block(x - 0.75, y, x + 0.75, y, track_width, "F.Cu", net_a, seg_uuid))
    parts.append(_FOOTER)
    return "".join(parts)


def write_synthetic_board(path: Path, **kwargs) -> Path:
    path.write_text(generate_synthetic_board(**kwargs), encoding="utf-8")
    return path


# --- Dense fanout-field mode (Phase 7.8 router/benchmark fixtures) ---------


def _dense_grid_dims(pad_count: int) -> tuple[int, int]:
    """Roughly-square (rows, cols) grid with rows*cols >= pad_count, trimmed
    to exactly pad_count cells (row-major, last row may be partial)."""
    if pad_count < 1:
        raise ValueError("pad_count must be >= 1")
    cols = math.ceil(math.sqrt(pad_count))
    rows = math.ceil(pad_count / cols)
    return rows, cols


def _dense_footprint_block(
    ref: str,
    value: str,
    x: float,
    y: float,
    uuid: str,
    pad_count: int,
    pad_nets: list[str],
    pitch: float = 1.0,
) -> str:
    """A many-pad footprint (BGA/QFP-grid-style): `pad_count` square SMD pads
    arranged on a roughly-square grid at `pitch` mm spacing, numbered 1..N
    row-major. `pad_nets[i]` is the net name for pad number i+1.
    """
    if len(pad_nets) != pad_count:
        raise ValueError(f"pad_nets must have exactly {pad_count} entries, got {len(pad_nets)}")
    rows, cols = _dense_grid_dims(pad_count)
    pad_size = round(pitch * 0.6, 4)
    x_off = (cols - 1) * pitch / 2.0
    y_off = (rows - 1) * pitch / 2.0
    label_y = y_off + pitch

    lines = [
        f'    (footprint "synthetic:DENSE_{pad_count}"',
        '        (layer "F.Cu")',
        f'        (uuid "{uuid}")',
        f'        (at {x} {y})',
        f'        (property "Reference" "{ref}" (at 0 -{label_y}) (layer "F.SilkS"))',
        f'        (property "Value" "{value}" (at 0 {label_y}) (layer "F.Fab"))',
    ]
    idx = 0
    for r in range(rows):
        for c in range(cols):
            if idx >= pad_count:
                break
            px = round(c * pitch - x_off, 4)
            py = round(r * pitch - y_off, 4)
            pad_num = str(idx + 1)
            net_name = pad_nets[idx]
            lines.append(
                f'        (pad "{pad_num}" smd rect (at {px} {py}) (size {pad_size} {pad_size}) '
                f'(layers "F.Cu" "F.Paste" "F.Mask") (net "{net_name}"))'
            )
            idx += 1
    lines.append("    )\n")
    return "\n".join(lines)


def generate_fanout_field_board(
    component_count: int = 4,
    pads_per_component: int = 32,
    spacing_mm: float = 25.0,
    layers: int = 2,
) -> str:
    """Dense fanout-field board for router/benchmark tests (Phase 7.8's
    global/detailed routing needs many-pad components with real unrouted
    connectivity between them, not just isolated two-pad pairs).

    `component_count` many-pad footprints (BGA/QFP-grid-style,
    `pads_per_component` pads each - default 32, within the requested
    16-48 range) are placed `spacing_mm` apart in a row. Pad position `p` on
    every component shares one net `FANOUT_p` (a bus, like real shared
    address/data/control lines fanning out to several ICs), so with
    `component_count >= 2` every net has >= 2 nodes - a genuine multi-point
    ratsnest, not a single isolated pad pair.

    Always unrouted (no `(segment ...)` emitted): this mode exists to feed
    the router/benchmark suite a realistic dense unrouted field, not to be a
    routed golden fixture.
    """
    if component_count < 1:
        raise ValueError("component_count must be >= 1")
    if pads_per_component < 1:
        raise ValueError("pads_per_component must be >= 1")

    header = _HEADER_TEMPLATE.format(layer_lines=_layer_stack_lines(layers))
    net_names = [f"FANOUT_{p}" for p in range(1, pads_per_component + 1)]

    parts = [header, _net_table(net_names)]
    for c in range(1, component_count + 1):
        ref = f"U{c}"
        x = c * spacing_mm
        y = 50.0
        fp_uuid = f"synth-fanout-fp-{c:06d}"
        parts.append(_dense_footprint_block(ref, "DENSE", x, y, fp_uuid, pads_per_component, net_names))
    parts.append(_FOOTER)
    return "".join(parts)


def write_fanout_field_board(path: Path, **kwargs) -> Path:
    path.write_text(generate_fanout_field_board(**kwargs), encoding="utf-8")
    return path


# --- Companion .kicad_pro / .net generation --------------------------------


def _synthetic_kicad_pro_text() -> str:
    """Minimal `.kicad_pro` JSON with a `net_settings.classes` Default block
    shaped exactly like the real `kiln.kicad_pro`'s (same keys/types - see
    `get_project_track_inventory`'s `existing_netclasses` reader, which only
    needs `net_settings.classes` to exist and be a list of dicts with a
    `name` and numeric netclass fields).
    """
    data = {
        "net_settings": {
            "classes": [
                {
                    "bus_width": 12,
                    "clearance": 0.2,
                    "diff_pair_gap": 0.25,
                    "diff_pair_via_gap": 0.25,
                    "diff_pair_width": 0.2,
                    "line_style": 0,
                    "microvia_diameter": 0.3,
                    "microvia_drill": 0.1,
                    "name": "Default",
                    "pcb_color": "rgba(0, 0, 0, 0.000)",
                    "priority": 2147483647,
                    "schematic_color": "rgba(0, 0, 0, 0.000)",
                    "track_width": 0.2,
                    "tuning_profile": "",
                    "via_diameter": 0.6,
                    "via_drill": 0.3,
                    "wire_width": 6,
                }
            ],
            "net_colors": None,
            "netclass_assignments": None,
            "netclass_patterns": [],
        }
    }
    return json.dumps(data, indent=2) + "\n"


def _netlist_net_block(code: int, name: str, node_refs_pins: list[tuple[str, str]]) -> list[str]:
    lines = [f'    (net (code "{code}") (name "{name}") (class "Default")']
    for ref, pin in node_refs_pins:
        lines.append(f'      (node (ref "{ref}") (pin "{pin}"))')
    lines.append("    )")
    return lines


def _synthetic_netlist_text_simple(component_count: int) -> str:
    """`.net` text matching `generate_synthetic_board`'s two-pad R<n>
    footprints: each resistor's A/B pads are their own isolated single-node
    net, exactly mirroring the board's own (unshared) pad connectivity.
    """
    parts = [
        "(export",
        '  (version "E")',
        "  (design",
        '    (source "synthetic.kicad_sch")',
        '    (date "2026-07-21")',
        '    (tool "synthetic_board.py")',
        "  )",
        "  (components",
    ]
    for i in range(1, component_count + 1):
        parts.append(f'    (comp (ref "R{i}") (value "10k") (footprint "synthetic:R_0603"))')
    parts.append("  )")
    parts.append("  (nets")
    code = 1
    for i in range(1, component_count + 1):
        parts.extend(_netlist_net_block(code, f"NET_{i}_A", [(f"R{i}", "1")]))
        code += 1
        parts.extend(_netlist_net_block(code, f"NET_{i}_B", [(f"R{i}", "2")]))
        code += 1
    parts.append("  )")
    parts.append(")")
    return "\n".join(parts) + "\n"


def _synthetic_netlist_text_fanout(component_count: int, pads_per_component: int) -> str:
    """`.net` text matching `generate_fanout_field_board`: each pad position
    `p` is one net (`FANOUT_p`) with one node per component - mirroring the
    board's own shared-bus pad connectivity, so `get_component_connections`
    on any U<n> reports every other component sharing a fanout net.
    """
    parts = [
        "(export",
        '  (version "E")',
        "  (design",
        '    (source "synthetic.kicad_sch")',
        '    (date "2026-07-21")',
        '    (tool "synthetic_board.py")',
        "  )",
        "  (components",
    ]
    for c in range(1, component_count + 1):
        parts.append(f'    (comp (ref "U{c}") (value "DENSE") (footprint "synthetic:DENSE_{pads_per_component}"))')
    parts.append("  )")
    parts.append("  (nets")
    for p in range(1, pads_per_component + 1):
        node_refs_pins = [(f"U{c}", str(p)) for c in range(1, component_count + 1)]
        parts.extend(_netlist_net_block(p, f"FANOUT_{p}", node_refs_pins))
    parts.append("  )")
    parts.append(")")
    return "\n".join(parts) + "\n"


# --- Multi-drop SPI bus mode (Phase 5 corridor / Phase 6 deviation fixtures) -


def _ic_footprint_block(ref: str, x: float, y: float, uuid: str, pads: list[tuple[str, float, float, str]]) -> str:
    """A multi-pad IC-style footprint at board position (x,y). `pads` is a list
    of (pad_number, local_x, local_y, net_name). Uses a 'U'-style ref so the
    bus-qualification IC filter (U/IC/Q prefixes) treats it as an IC."""
    lines = [
        '    (footprint "synthetic:IC"',
        '        (layer "F.Cu")',
        f'        (uuid "{uuid}")',
        f'        (at {x} {y})',
        f'        (property "Reference" "{ref}" (at 0 -3) (layer "F.SilkS"))',
        f'        (property "Value" "IC" (at 0 3) (layer "F.Fab"))',
    ]
    for num, px, py, net in pads:
        lines.append(
            f'        (pad "{num}" smd rect (at {px} {py}) (size 0.4 0.4) '
            f'(layers "F.Cu" "F.Paste" "F.Mask") (net "{net}"))'
        )
    lines.append("    )\n")
    return "\n".join(lines)


def _multidrop_spi_layout(destinations: int, prefix: str) -> dict:
    """Geometry for a hub (U1) + `destinations` slave ICs multi-drop SPI bus.
    Shared nets SCK/MOSI/MISO reach the hub + every destination; each CS<i> is
    dedicated (hub + one destination). Returns component pad maps, the routed
    segment list, and the netlist node map - all sharing net names by value."""
    shared = [f"{prefix}SCK", f"{prefix}MOSI", f"{prefix}MISO"]
    # per-shared-net trunk y offset, so the 3 shared traces form a parallel bundle
    trunk_y = {shared[0]: -1.0, shared[1]: 0.0, shared[2]: 1.0}

    # Hub U1 at (0,0): one pad per shared net + one per CS.
    hub_pads: list[tuple[str, float, float, str]] = []
    pin = 1
    for net in shared:
        hub_pads.append((str(pin), 0.0, trunk_y[net], net))
        pin += 1
    cs_nets = []
    for d in range(destinations):
        cs = f"{prefix}CS{d}"
        cs_nets.append(cs)
        hub_pads.append((str(pin), 0.0, 3.0 + d, cs))
        pin += 1

    components = {"U1": {"at": (0.0, 0.0), "pads": hub_pads}}
    segments: list[tuple[float, float, float, float, str, str]] = []  # x1,y1,x2,y2,net,layer
    netlist_nodes: dict[str, list[tuple[str, str]]] = {net: [("U1", str(i + 1))] for i, net in enumerate(shared + cs_nets)}

    branch_x = 30.0
    # trunk: shared nets run straight from hub to the branch point.
    for net in shared:
        segments.append((0.0, trunk_y[net], branch_x, trunk_y[net], net, "F.Cu"))

    for d in range(destinations):
        ref = f"U{d + 2}"
        # destinations fan out in y so their hub->dest axes are distinct.
        dest_y = -12.0 + d * 24.0 / max(1, destinations - 1) if destinations > 1 else 0.0
        dest_x = 55.0
        cs = f"{prefix}CS{d}"
        dpads: list[tuple[str, float, float, str]] = []
        pin = 1
        for net in shared:
            dpads.append((str(pin), 0.0, trunk_y[net], net))
            pin += 1
            # branch: shared net from the trunk branch point to this destination.
            segments.append((branch_x, trunk_y[net], dest_x, dest_y + trunk_y[net], net, "F.Cu"))
            netlist_nodes[net].append((ref, str(pin - 1)))
        dpads.append((str(pin), 0.0, 2.0, cs))
        # dedicated CS: straight hub -> destination.
        segments.append((0.0, 3.0 + d, dest_x, dest_y + 2.0, cs, "F.Cu"))
        netlist_nodes[cs].append((ref, str(pin)))
        components[ref] = {"at": (dest_x, dest_y), "pads": dpads}

    return {"components": components, "segments": segments, "netlist_nodes": netlist_nodes,
            "shared": shared, "cs_nets": cs_nets}


def generate_multidrop_spi_board(destinations: int = 2, prefix: str = "/SPI/", route: bool = True,
                                 deviate_net: str | None = None) -> tuple[str, dict]:
    """Build a parser-valid `.kicad_pcb` for a hub + `destinations`-slave SPI bus.
    Returns (board_text, layout). `deviate_net` (a full net name) adds a lateral
    jog to that net's trunk so it bows off the bundle centerline - the Phase 6
    deviation-term fixture. `route=False` emits the footprints but no copper."""
    layout = _multidrop_spi_layout(destinations, prefix)
    header = _HEADER_TEMPLATE.format(layer_lines=_layer_stack_lines(2))
    all_nets = layout["shared"] + layout["cs_nets"]
    parts = [header, _net_table(all_nets)]
    for i, (ref, comp) in enumerate(layout["components"].items()):
        parts.append(_ic_footprint_block(ref, comp["at"][0], comp["at"][1], f"synth-ic-{i:04d}", comp["pads"]))
    if route:
        for j, (x1, y1, x2, y2, net, layer) in enumerate(layout["segments"]):
            if net == deviate_net and abs(y1 - y2) < 1e-9:
                # replace a straight trunk with a bowed jog that pulls the net
                # well away from the bundle centerline (SCK is the outermost/
                # most-negative trace, so jog further negative to raise deviation).
                midx = (x1 + x2) / 2.0
                parts.append(_segment_block(x1, y1, midx, y1 - 8.0, 0.2, layer, net, f"synth-seg-{j:04d}a"))
                parts.append(_segment_block(midx, y1 - 8.0, x2, y2, 0.2, layer, net, f"synth-seg-{j:04d}b"))
            else:
                parts.append(_segment_block(x1, y1, x2, y2, 0.2, layer, net, f"synth-seg-{j:04d}"))
    parts.append(_FOOTER)
    return "".join(parts), layout


def _multidrop_netlist_text(layout: dict) -> str:
    parts = [
        "(export", '  (version "E")', "  (design", '    (source "synthetic.kicad_sch")',
        '    (date "2026-07-21")', '    (tool "synthetic_board.py")', "  )", "  (components",
    ]
    refs = sorted(layout["components"].keys())
    for ref in refs:
        parts.append(f'    (comp (ref "{ref}") (value "IC") (footprint "synthetic:IC"))')
    parts.append("  )")
    parts.append("  (nets")
    for code, (net, nodes) in enumerate(layout["netlist_nodes"].items(), start=1):
        parts.extend(_netlist_net_block(code, net, nodes))
    parts.append("  )")
    parts.append(")")
    return "\n".join(parts) + "\n"


def write_multidrop_spi_project(
    directory: Path,
    project_name: str = "spibus",
    destinations: int = 2,
    prefix: str = "/SPI/",
    route: bool = True,
    deviate_net: str | None = None,
) -> dict[str, Path]:
    """Write a full synthetic multi-drop SPI project (board + `.kicad_pro` +
    `.net`) into `directory`. See `generate_multidrop_spi_board`."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    board_path = directory / f"{project_name}.kicad_pcb"
    project_path = directory / f"{project_name}.kicad_pro"
    netlist_path = directory / f"{project_name}.net"
    board_text, layout = generate_multidrop_spi_board(destinations, prefix, route, deviate_net)
    board_path.write_text(board_text, encoding="utf-8")
    project_path.write_text(_synthetic_kicad_pro_text(), encoding="utf-8")
    netlist_path.write_text(_multidrop_netlist_text(layout), encoding="utf-8")
    return {"board": board_path, "project": project_path, "netlist": netlist_path, "layout": layout}


def write_synthetic_project(
    directory: Path,
    project_name: str = "synthetic",
    mode: str = "simple",
    component_count: int = 10,
    spacing_mm: float | None = None,
    track_width: float = 0.2,
    route: bool = True,
    layers: int = 2,
    scale: float = 1.0,
    pads_per_component: int = 32,
) -> dict[str, Path]:
    """Write a full synthetic KiCad project - board + `.kicad_pro` + `.net` -
    into `directory`, all named `project_name` (e.g. `synthetic.kicad_pcb`,
    `synthetic.kicad_pro`, `synthetic.net`) so `_resolve_project_path` finds
    all three exactly the way it finds kiln's own files.

    `mode="simple"` (default) uses `generate_synthetic_board` (two-pad R<n>
    footprints; `component_count`/`spacing_mm`/`track_width`/`route`/`layers`/
    `scale` are forwarded to it) with a matching isolated-net `.net` file.

    `mode="fanout"` uses `generate_fanout_field_board` (`component_count`
    many-pad U<n> footprints, `pads_per_component` pads each, `spacing_mm`/
    `layers` forwarded) with a matching shared-bus `.net` file.

    Returns `{"board": ..., "project": ..., "netlist": ...}` paths.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    board_path = directory / f"{project_name}.kicad_pcb"
    project_path = directory / f"{project_name}.kicad_pro"
    netlist_path = directory / f"{project_name}.net"

    if mode == "simple":
        write_synthetic_board(
            board_path,
            component_count=component_count,
            spacing_mm=spacing_mm if spacing_mm is not None else 5.0,
            track_width=track_width,
            route=route,
            layers=layers,
            scale=scale,
        )
        n = round(component_count * scale)
        netlist_text = _synthetic_netlist_text_simple(n)
    elif mode == "fanout":
        write_fanout_field_board(
            board_path,
            component_count=component_count,
            pads_per_component=pads_per_component,
            spacing_mm=spacing_mm if spacing_mm is not None else 25.0,
            layers=layers,
        )
        netlist_text = _synthetic_netlist_text_fanout(component_count, pads_per_component)
    else:
        raise ValueError(f"Unknown mode: {mode!r} (expected 'simple' or 'fanout')")

    project_path.write_text(_synthetic_kicad_pro_text(), encoding="utf-8")
    netlist_path.write_text(netlist_text, encoding="utf-8")
    return {"board": board_path, "project": project_path, "netlist": netlist_path}
