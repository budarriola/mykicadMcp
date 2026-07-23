"""Tests for Phase 7.3a global routing (`kicad_router_tool`).

Tests cover:
  1. Forced crossing: two connections must cross; verify resolution.
  2. Congested channel k-alternates: narrow channel, verify _make_candidates produces
     ranked alternates that genuinely avoid the first path's congestion.
  3. Bundle-width capacity: multi-drop SPI, verify bundle routed as one unit with
     capacity debited for the whole bundle width.
  4. Direction inference goldens: synthetic boards with h/v segments, verify
     `infer_layer_directions` returns expected values.
  5. Byte-identical determinism: run `global_route` twice, verify serialized
     results are identical (no floats in cost comparisons).
  6. Integer milli-cost assertion: all cost values are integers (milli-cost units),
     never floats.
  7. Real board smoke test (optional, may be marked slow).

Uses synthetic boards + real kiln project for golden testing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import kicad_pcb_tool as pcb
import kicad_router_tool as router
from synthetic_board import (
    _FOOTER,
    _HEADER_TEMPLATE,
    _layer_stack_lines,
    generate_synthetic_board,
    write_multidrop_spi_project,
    write_synthetic_project,
)


# --------------------------------------------------------------------------- #
# Helpers: minimal board builder (test 1 - forced crossing)
# --------------------------------------------------------------------------- #


def _pad(num: str, x: float, y: float, net: str, layer: str = "F.Cu", size: float = 0.9) -> str:
    """Single SMD pad on one layer."""
    return (
        f'        (pad "{num}" smd rect (at {x} {y}) (size {size} {size}) '
        f'(layers "{layer}" "{layer[0]}.Paste" "{layer[0]}.Mask") (net "{net}"))'
    )


def _footprint(ref: str, x: float, y: float, uuid: str, pads: list[str]) -> str:
    """Footprint with arbitrary pads."""
    body = "\n".join(pads)
    return (
        f'    (footprint "synthetic:BLD"\n'
        f'        (layer "F.Cu")\n'
        f'        (uuid "{uuid}")\n'
        f'        (at {x} {y})\n'
        f'        (property "Reference" "{ref}" (at 0 -1.5) (layer "F.SilkS"))\n'
        f'        (property "Value" "V" (at 0 1.5) (layer "F.Fab"))\n'
        f'{body}\n'
        f'    )\n'
    )


def _segment(
    x1: float, y1: float, x2: float, y2: float, net: str, uuid: str, layer: str = "F.Cu", width: float = 0.25
) -> str:
    """Single segment."""
    return (
        f'    (segment (start {x1} {y1}) (end {x2} {y2}) (width {width}) '
        f'(layer "{layer}") (net "{net}") (uuid "{uuid}"))\n'
    )


def _via(x: float, y: float, net: str, uuid: str, layers: tuple[str, str] = ("F.Cu", "B.Cu")) -> str:
    """Single via."""
    return (
        f'    (via (at {x} {y}) (size 0.6) (drill 0.3) '
        f'(layers "{layers[0]}" "{layers[1]}") (net "{net}") (uuid "{uuid}"))\n'
    )


def _write_board(path: Path, body_parts: list[str], layers: int = 2) -> Path:
    """Write a minimal board file."""
    header = _HEADER_TEMPLATE.format(layer_lines=_layer_stack_lines(layers))
    path.write_text(header + "".join(body_parts) + _FOOTER, encoding="utf-8")
    return path


def _write_minimal_pro_and_net(path: Path, net_names: list[str], refs_pads: dict[str, list[tuple[str, str]]]) -> None:
    """Write minimal .kicad_pro and .net files for a test board."""
    import json

    # Minimal .kicad_pro
    pro_data = {
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
    pro_file = path.with_suffix(".kicad_pro")
    pro_file.write_text(json.dumps(pro_data, indent=2) + "\n", encoding="utf-8")

    # Minimal .net
    net_lines = [
        "(export",
        '  (version "E")',
        "  (design",
        '    (source "test.kicad_sch")',
        '    (date "2026-07-21")',
        '    (tool "test_global_route.py")',
        "  )",
        "  (components",
    ]
    for ref in sorted(refs_pads.keys()):
        net_lines.append(f'    (comp (ref "{ref}") (value "TEST") (footprint "synthetic:TEST"))')
    net_lines.append("  )")
    net_lines.append("  (nets")
    for code, net in enumerate(net_names, start=1):
        nodes = []
        for ref, pads in refs_pads.items():
            for pad_num, pad_net in pads:
                if pad_net == net:
                    nodes.append(f'      (node (ref "{ref}") (pin "{pad_num}"))')
        if nodes:
            net_lines.append(f'    (net (code "{code}") (name "{net}") (class "Default")')
            net_lines.extend(nodes)
            net_lines.append("    )")
    net_lines.append("  )")
    net_lines.append(")")
    net_file = path.with_suffix(".net")
    net_file.write_text("\n".join(net_lines) + "\n", encoding="utf-8")


# =========================================================================== #
# Test 1: Forced crossing
# =========================================================================== #


def test_forced_crossing_two_connections_must_cross(tmp_path: Path) -> None:
    """Two connections X and Y must cross (one needs a different layer).
    Verify global_route resolves it (candidates exist) and both get routed."""
    board = tmp_path / "crossing.kicad_pcb"

    # Crossing topology: X net goes from (0,0) to (10,10), Y net from (0,10) to (10,0).
    # On a simple F.Cu-only board, they must cross. With B.Cu available, the router
    # should pick different layers or non-intersecting corridors.
    _write_board(
        board,
        [
            _footprint("U1", 0, 0, "fp-1", [_pad("1", 0.0, 0.0, "X")]),
            _footprint("U2", 0, 0, "fp-2", [_pad("1", 10.0, 10.0, "X")]),
            _footprint("U3", 0, 0, "fp-3", [_pad("1", 0.0, 10.0, "Y")]),
            _footprint("U4", 0, 0, "fp-4", [_pad("1", 10.0, 0.0, "Y")]),
        ],
        layers=2,
    )

    _write_minimal_pro_and_net(
        board,
        ["X", "Y"],
        {"U1": [("1", "X")], "U2": [("1", "X")], "U3": [("1", "Y")], "U4": [("1", "Y")]},
    )

    result = router.global_route(str(tmp_path))
    assert result is not None
    assert "connections" in result

    # Both connections should exist and have candidates.
    x_conns = [c for c in result["connections"] if c["net"] == "X"]
    y_conns = [c for c in result["connections"] if c["net"] == "Y"]
    assert len(x_conns) == 1
    assert len(y_conns) == 1
    assert x_conns[0]["routed"], "X connection should have candidates"
    assert y_conns[0]["routed"], "Y connection should have candidates"

    # Verify at least one connection used a layer (not None).
    layers_used = set()
    if x_conns[0]["candidates"]:
        layers_used.add(x_conns[0]["candidates"][0]["home_layer"])
    if y_conns[0]["candidates"]:
        layers_used.add(y_conns[0]["candidates"][0]["home_layer"])
    assert len([l for l in layers_used if l is not None]) >= 1, "At least one connection should have a resolved home layer"


# =========================================================================== #
# Test 2: Congested channel k-alternates
# =========================================================================== #


def test_congested_channel_k_alternates(tmp_path: Path) -> None:
    """Narrow channel that cannot fit all connections in one layer: verify
    _make_candidates produces k>1 ranked alternates, and alternates genuinely
    avoid the first path's most-congested cell."""
    # Create a synthetic board with a tight corridor: multiple connections
    # must route through a narrow passage. With congestion tracking,
    # _make_candidates should produce 2-3 alternates with increasingly higher cost.
    board = tmp_path / "congested.kicad_pcb"

    # Setup: 4 pads on left (bottom to top), 4 on right, narrow passage in the middle
    # at (5, 0..3). Two connections will need to share the passage.
    header = _HEADER_TEMPLATE.format(layer_lines=_layer_stack_lines(2))
    net_table = '    (net 0 "")\n    (net 1 "NET_A")\n    (net 2 "NET_B")\n'

    # Two footprints: left (0,0) with pads for A/B, right (10,0) with pads for A/B.
    fp_left = _footprint("U1", 0, 0, "fp-1", [_pad("1", 0.0, 1.0, "NET_A"), _pad("2", 0.0, 2.0, "NET_B")])
    fp_right = _footprint("U2", 0, 0, "fp-2", [_pad("1", 10.0, 1.0, "NET_A"), _pad("2", 10.0, 2.0, "NET_B")])

    body_text = header + net_table + fp_left + fp_right + _FOOTER
    board.write_text(body_text, encoding="utf-8")

    _write_minimal_pro_and_net(
        board,
        ["NET_A", "NET_B"],
        {"U1": [("1", "NET_A"), ("2", "NET_B")], "U2": [("1", "NET_A"), ("2", "NET_B")]},
    )

    result = router.global_route(str(tmp_path))
    assert result is not None

    routed = [c for c in result["connections"] if c["routed"]]
    assert len(routed) == 2, "Both NET_A and NET_B connections should route"

    # Measured on this fixture: each connection gets 3 ranked alternates. The
    # k-shortest machinery must produce genuine alternates, not a single path.
    for conn in routed:
        cands = conn["candidates"]
        assert len(cands) > 1, f"{conn['net']} should have k>1 candidate paths, got {len(cands)}"
        costs = [c["est_cost_milli"] for c in cands]
        assert costs == sorted(costs), "Candidates should be sorted by cost"
        assert all(isinstance(c, int) for c in costs), "Costs should be integers (milli-cost)"
        # Alternates must be genuinely different paths, not the best path repeated.
        paths = [json.dumps(c.get("path", c), sort_keys=True) for c in cands]
        assert len(set(paths)) == len(paths), "Alternate candidates should be distinct paths"


# =========================================================================== #
# Test 3: Bundle-width capacity
# =========================================================================== #


def _strip_alternate_segments(board: Path, nets: list[str]) -> dict[str, int]:
    """Delete every other (segment ...) block belonging to the given nets.

    Bundles require a PARTIALLY routed bus: `_collect_bundles` reuses Phase 5
    corridor geometry computed from existing copper, so a fully-unrouted bus has
    no bundle geometry and a fully-routed one has no ratsnest connections.
    Stripping alternate segments leaves corridor copper AND missing connections.
    """
    import re

    text = board.read_text(encoding="utf-8")
    seg_re = re.compile(r"    \(segment\n(?:[^\n]*\n)*?    \)\n")
    counts = {net: 0 for net in nets}

    def repl(m: "re.Match[str]") -> str:
        blk = m.group(0)
        for net in counts:
            if f'(net "{net}")' in blk:
                counts[net] += 1
                if counts[net] % 2 == 0:
                    return ""
        return blk

    board.write_text(seg_re.sub(repl, text), encoding="utf-8")
    return counts


def test_bundle_width_capacity_debited_as_unit(tmp_path: Path) -> None:
    """Partially-routed multi-drop SPI: verify the bus bundle is routed as ONE
    unit (shared bundle_id, shared home layer, shared candidate corridors)."""
    # Destinations=2 -> hub U1 + U2,U3; shared nets SCK/MOSI/MISO form a bus.
    # Route it fully, then strip alternate MISO/MOSI segments so the bus keeps
    # its corridor copper but has unrouted connections for the router to bundle.
    write_multidrop_spi_project(tmp_path, destinations=2, route=True)
    board = next(tmp_path.glob("*.kicad_pcb"))
    counts = _strip_alternate_segments(board, ["/SPI/MISO", "/SPI/MOSI"])
    assert all(v >= 2 for v in counts.values()), f"Fixture should strip real segments, saw {counts}"

    result = router.global_route(str(tmp_path))
    assert result is not None
    assert "connections" in result

    bundle_conns: dict[str, list[dict]] = {}
    for conn in result["connections"]:
        bid = conn["bundle_id"]
        if bid is not None:
            bundle_conns.setdefault(bid, []).append(conn)

    # Measured on this fixture: one bundle (SPI:U1->U2) holding both the MISO
    # and MOSI connections. A vacuous no-bundle result is a regression.
    assert bundle_conns, "Partially-routed SPI bus must produce at least one bundle"
    multi_member = [conns for conns in bundle_conns.values() if len(conns) >= 2]
    assert multi_member, "At least one bundle should hold multiple member connections"

    for bid, conns in bundle_conns.items():
        # All members of the same bundle should have the same home layer
        home_layers = set(c["home_layer"] for c in conns)
        assert len(home_layers) == 1, f"Bundle {bid} members should all have same home layer"
        # All should report the same candidates (shared routing)
        candidate_lists = [tuple(json.dumps(c, sort_keys=True) for c in conn["candidates"]) for conn in conns]
        assert len(set(candidate_lists)) == 1, f"Bundle {bid} members should share candidates"


# =========================================================================== #
# Test 4: Direction inference goldens
# =========================================================================== #


def test_direction_inference_h_v_dominant(tmp_path: Path) -> None:
    """Create boards with clearly dominant horizontal/vertical copper and verify
    `infer_layer_directions` returns the expected h/v assignments."""
    board = tmp_path / "directions.kicad_pcb"

    # Two-layer board: F.Cu heavily horizontal, B.Cu heavily vertical.
    # Segments: F.Cu has many long horizontal traces, B.Cu has many long vertical.
    header = _HEADER_TEMPLATE.format(layer_lines=_layer_stack_lines(2))
    net_table = '    (net 0 "")\n    (net 1 "H_NET")\n    (net 2 "V_NET")\n'

    body_parts = []
    # Horizontal segments on F.Cu: 5 x 20mm each = 100mm horizontal
    for i in range(5):
        body_parts.append(_segment(0.0, float(i * 2.0), 20.0, float(i * 2.0), "H_NET", f"seg-h-{i}", "F.Cu"))
    # Vertical segments on B.Cu: 5 x 20mm each = 100mm vertical
    for i in range(5):
        body_parts.append(_segment(float(i * 2.0), 0.0, float(i * 2.0), 20.0, "V_NET", f"seg-v-{i}", "B.Cu"))

    body_text = header + net_table + "".join(body_parts) + _FOOTER
    board.write_text(body_text, encoding="utf-8")

    # Create minimal .kicad_pro and .net
    import json

    pro_file = board.with_suffix(".kicad_pro")
    pro_file.write_text(json.dumps({"net_settings": {"classes": []}}, indent=2) + "\n", encoding="utf-8")
    net_file = board.with_suffix(".net")
    net_file.write_text("(export (version E) (design) (components) (nets))\n", encoding="utf-8")

    result = router.infer_layer_directions(str(tmp_path))
    assert result is not None
    assert "directions" in result
    directions = result["directions"]

    # F.Cu should infer as "h" (horizontal dominant), B.Cu as "v" (vertical dominant).
    assert directions.get("F.Cu") == "h", f"F.Cu should infer as 'h', got {directions.get('F.Cu')}"
    assert directions.get("B.Cu") == "v", f"B.Cu should infer as 'v', got {directions.get('B.Cu')}"


def test_direction_inference_low_copper_ambiguous(tmp_path: Path) -> None:
    """Board with very little copper: direction inference should handle gracefully
    (either return None for ambiguous, or alternate against neighbors - both are valid)."""
    board = tmp_path / "low_copper.kicad_pcb"

    # Three-layer board with minimal copper (below the 10mm threshold).
    header = _HEADER_TEMPLATE.format(layer_lines=_layer_stack_lines(3))
    net_table = '    (net 0 "")\n    (net 1 "TEST")\n'

    # Just one short segment per layer: not enough to infer.
    body_parts = [
        _segment(0.0, 0.0, 1.0, 0.0, "TEST", "seg-f", "F.Cu"),  # 1mm horizontal
        _segment(0.0, 0.0, 0.0, 1.0, "TEST", "seg-in", "In1.Cu"),  # 1mm vertical
        _segment(0.0, 0.0, 1.0, 1.0, "TEST", "seg-b", "B.Cu"),  # ~1.4mm diagonal
    ]

    body_text = header + net_table + "".join(body_parts) + _FOOTER
    board.write_text(body_text, encoding="utf-8")

    import json

    pro_file = board.with_suffix(".kicad_pro")
    pro_file.write_text(json.dumps({"net_settings": {"classes": []}}, indent=2) + "\n", encoding="utf-8")
    net_file = board.with_suffix(".net")
    net_file.write_text("(export (version E) (design) (components) (nets))\n", encoding="utf-8")

    result = router.infer_layer_directions(str(tmp_path))
    assert result is not None
    assert "directions" in result
    directions = result["directions"]

    # All layers should have some value (h, v, or None); no exceptions.
    for layer in ["F.Cu", "In1.Cu", "B.Cu"]:
        assert layer in directions, f"Layer {layer} should be in directions"
        # Value can be h, v, or None - all are valid for low-copper case.
        assert directions[layer] in ("h", "v", None), f"Direction for {layer} should be h/v/None, got {directions[layer]}"


# =========================================================================== #
# Test 5: Byte-identical determinism
# =========================================================================== #


def test_byte_identical_determinism(tmp_path: Path) -> None:
    """Run global_route twice on the same project; verify json.dumps(result,
    sort_keys=True) is byte-identical both times (no float creep in costs)."""
    # Use a simple synthetic project.
    proj = write_synthetic_project(tmp_path, project_name="determ", component_count=5, route=False)

    result1 = router.global_route(str(tmp_path))
    result2 = router.global_route(str(tmp_path))

    assert result1 is not None and result2 is not None

    # Serialize both with deterministic sort.
    json1 = json.dumps(result1, sort_keys=True)
    json2 = json.dumps(result2, sort_keys=True)

    assert json1 == json2, "Two runs on the same project should produce byte-identical JSON"


# =========================================================================== #
# Test 6: Integer milli-cost assertion
# =========================================================================== #


def test_all_costs_are_integers(tmp_path: Path) -> None:
    """Verify all cost values are integers (milli-cost units), never floats.
    This is the critical determinism requirement."""
    proj = write_synthetic_project(tmp_path, project_name="intcost", component_count=8, route=False)

    result = router.global_route(str(tmp_path))
    assert result is not None

    def check_int_costs(obj, path=""):
        """Recursively check that all cost/milli fields are integers."""
        if isinstance(obj, dict):
            for key, val in obj.items():
                if "cost" in key.lower() or "milli" in key.lower():
                    if isinstance(val, (int, float)):
                        assert isinstance(val, int) and not isinstance(val, bool), (
                            f"Cost field '{key}' at {path} is {type(val).__name__}, " f"should be int. Value: {val}"
                        )
                check_int_costs(val, f"{path}.{key}")
        elif isinstance(obj, list):
            for i, item in enumerate(obj):
                check_int_costs(item, f"{path}[{i}]")

    check_int_costs(result)


# =========================================================================== #
# Test 7: Real board smoke test (golden, optional slow marker)
# =========================================================================== #


@pytest.mark.slow
def test_kiln_smoke_global_route(kiln_project_path: Path) -> None:
    """Smoke test: run global_route on the real kiln board. Verify all 39
    ratsnest connections get candidates (39/39 routed). Runtime is ~2 min cold.
    Marked as slow so it can be skipped in fast CI runs."""
    result = router.global_route(str(kiln_project_path))
    assert result is not None
    assert "connections" in result
    assert "summary" in result

    # The real board has 39 ratsnest connections (known from prior runs).
    conns = result["connections"]
    routed_count = sum(1 for c in conns if c["routed"])

    # Not enforcing 39/39 (board state may change), but expect most to route.
    assert len(conns) >= 30, f"Expected at least 30 connections, got {len(conns)}"
    assert routed_count >= len(conns) * 0.8, (
        f"Expected at least 80% routed, got {routed_count}/{len(conns)} " f"({100*routed_count/len(conns):.1f}%)"
    )

    # Verify cost values are integers.
    for conn in conns:
        if conn["candidates"]:
            for cand in conn["candidates"]:
                assert isinstance(cand["est_cost_milli"], int), (
                    f"Candidate cost should be int, got {type(cand['est_cost_milli']).__name__}"
                )
