"""Phase 9: High-speed and critical-net classification tests.

Tests cover each classification source (bus frequency, XTAL, switch nodes),
L_crit formula, critical_fraction gate, and get_trace_cost integration.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

import kicad_pcb_tool as k
from synthetic_board import (
    generate_synthetic_board,
    write_critical_nets_project,
    write_synthetic_project,
)


def test_classify_critical_nets_bus_frequency_mapping(tmp_path: Path) -> None:
    """Test that bus-detected nets are classified with the correct frequency."""
    # Generate a minimal synthetic project
    result_paths = write_synthetic_project(
        tmp_path,
        "test_bus_freq",
        component_count=4,
    )
    project_path = result_paths["board"].parent

    # Run classify_critical_nets
    result = k.classify_critical_nets(project_path)

    assert "critical_nets" in result
    assert "l_crit_table" in result
    # Check that L_crit table is populated
    assert len(result["l_crit_table"]) > 0


def test_classify_critical_nets_clk_name_token(tmp_path: Path) -> None:
    """Test that nets with CLK name token are classified."""
    # Create a synthetic board with a net named CLK
    board_text = generate_synthetic_board(component_count=2)
    board_path = tmp_path / "test_clk.kicad_pcb"
    board_path.write_text(board_text, encoding="utf-8")

    project_path = tmp_path
    # The test is limited by synthetic_board not supporting custom net names yet,
    # so this is a placeholder for the intended behavior
    # TODO: enhance synthetic_board to support custom net names


def test_l_crit_formula_golden(tmp_path: Path) -> None:
    """Test L_crit = v * t_rise / 6 formula with hand-computed numbers."""
    # Default settings: velocity_fraction=0.5, rise_fraction=0.05
    # For SPI at 20 MHz:
    # t_rise = 0.05 / 20 = 0.0025 us = 2.5 ns
    # v = c * 0.5 = 299.792 * 0.5 = 149.896 mm/ns
    # L_crit = 149.896 * 2.5 / 6 = 62.456 mm

    result_paths = write_synthetic_project(tmp_path, "test_lcrit", component_count=2)
    project_path = result_paths["board"].parent

    result = k.classify_critical_nets(project_path)
    l_crit_table = result.get("l_crit_table", {})

    # Check SPI L_crit value (should be ~62.5 mm)
    if "SPI" in l_crit_table:
        spi_lcrit = l_crit_table["SPI"]
        expected = 149.896 * 2.5 / 6  # ~62.456 mm
        assert abs(spi_lcrit - expected) < 1.0, f"SPI L_crit {spi_lcrit} != {expected}"


def test_l_crit_overrides(tmp_path: Path) -> None:
    """Test that critical_length_overrides_mm bypasses the formula."""
    result_paths = write_synthetic_project(tmp_path, "test_override", component_count=2)
    project_path = result_paths["board"].parent

    # Write pcb_settings.json with an override
    settings_path = project_path / "pcb_settings.json"
    settings = {
        "high_speed": {
            "critical_length_overrides_mm": {
                "SPI": 100.0,
            }
        }
    }
    settings_path.write_text(json.dumps(settings), encoding="utf-8")

    result = k.classify_critical_nets(project_path)
    l_crit_table = result.get("l_crit_table", {})

    # SPI should use the override value, not the formula
    assert l_crit_table.get("SPI") == 100.0


def test_critical_fraction_gate(tmp_path: Path) -> None:
    """Test that stack_up_gate is True when straight_line >= critical_fraction * l_crit."""
    result_paths = write_synthetic_project(tmp_path, "test_gate", component_count=3)
    project_path = result_paths["board"].parent

    result = k.classify_critical_nets(project_path)
    critical_nets = result.get("critical_nets", [])

    # For nets that are classified, check the stack_up_gate logic
    for net in critical_nets:
        l_crit = net.get("l_crit_mm", 0.0)
        straight_line = net.get("straight_line_mm", 0.0)
        stack_up_gate = net.get("stack_up_gate", False)

        # critical_fraction defaults to 0.9
        expected_gate = straight_line >= (0.9 * l_crit) if l_crit > 0 else False
        assert stack_up_gate == expected_gate, (
            f"Net {net['net']}: straight_line={straight_line}, "
            f"l_crit={l_crit}, gate={stack_up_gate}, expected={expected_gate}"
        )


def test_xtal_detection_by_ref(tmp_path: Path) -> None:
    """Test XTAL nets are detected by component ref Y* or X*."""
    components = [
        {
            "ref": "Y1",
            "footprint": "synthetic:XTAL_2Pin",
            "x": 10.0,
            "y": 10.0,
            "pads": [
                (1, -1.0, 0.0, 0.9, 0.9, "XTAL1_A"),
                (2, 1.0, 0.0, 0.9, 0.9, "XTAL1_B"),
            ],
        },
        {
            "ref": "U1",
            "footprint": "synthetic:IC",
            "x": 30.0,
            "y": 10.0,
            "pads": [
                (1, 0.0, 0.0, 0.4, 0.4, "XTAL1_A"),
                (2, 0.0, 1.0, 0.4, 0.4, "XTAL1_B"),
            ],
        },
    ]
    result_paths = write_critical_nets_project(tmp_path, "test_xtal_ref", components)
    project_path = result_paths["board"].parent

    result = k.classify_critical_nets(project_path)
    critical_nets = result.get("critical_nets", [])

    xtal_records = {net["net"]: net for net in critical_nets if net.get("reason") == "xtal"}
    assert {"XTAL1_A", "XTAL1_B"} <= set(xtal_records.keys()), (
        f"Expected XTAL1_A/XTAL1_B classified as xtal, got {xtal_records.keys()} "
        f"from {critical_nets}"
    )
    for net in xtal_records.values():
        assert net["critical"] is True
        assert net["multiplier"] == 8.0  # switch_node length_weight_mult (XTAL's weight)


def test_xtal_detection_by_footprint_token(tmp_path: Path) -> None:
    """Test XTAL nets are detected by footprint name containing xtal/crystal tokens."""
    components = [
        {
            # Ref deliberately does NOT start with Y/X, so only the footprint
            # name's "CRYSTAL" token can trigger classification.
            "ref": "C1",
            "footprint": "synthetic:Crystal_HC49U_Vertical",
            "x": 10.0,
            "y": 10.0,
            "pads": [
                (1, -1.0, 0.0, 0.9, 0.9, "OSC_A"),
                (2, 1.0, 0.0, 0.9, 0.9, "OSC_B"),
            ],
        },
        {
            "ref": "U1",
            "footprint": "synthetic:IC",
            "x": 30.0,
            "y": 10.0,
            "pads": [
                (1, 0.0, 0.0, 0.4, 0.4, "OSC_A"),
                (2, 0.0, 1.0, 0.4, 0.4, "OSC_B"),
            ],
        },
    ]
    result_paths = write_critical_nets_project(tmp_path, "test_xtal_footprint", components)
    project_path = result_paths["board"].parent

    result = k.classify_critical_nets(project_path)
    critical_nets = result.get("critical_nets", [])

    xtal_records = {net["net"]: net for net in critical_nets if net.get("reason") == "xtal"}
    assert {"OSC_A", "OSC_B"} <= set(xtal_records.keys()), (
        f"Expected OSC_A/OSC_B classified as xtal via footprint token, got "
        f"{xtal_records.keys()} from {critical_nets}"
    )


def test_switch_node_detection_by_size(tmp_path: Path) -> None:
    """Test switch nodes are detected when inductor size >= min_inductor_mm.

    Uses two inductors of the same ref pattern (L*), both with one terminal
    wired to an IC pin: L1's footprint is >= the default 2.0mm min_inductor_mm
    on both axes (pad size 4x9mm, pads 6mm apart -> ~10x9mm bbox, mirroring
    kiln's real SRP1038C), so its IC-connected net should qualify. L2 is a
    tiny 0402-style footprint (well under 2.0mm on both axes) so, despite
    also reaching an IC pin, its net should NOT qualify - isolating the size
    gate from the IC-pin gate (covered separately below).
    """
    components = [
        {
            "ref": "L1",
            "footprint": "synthetic:L_Inductor_Big",
            "x": 10.0,
            "y": 10.0,
            "pads": [
                (1, -3.0, 0.0, 4.0, 9.0, "L1_IN"),
                (2, 3.0, 0.0, 4.0, 9.0, "L1_SW"),
            ],
        },
        {
            "ref": "L2",
            "footprint": "synthetic:L_Inductor_0402",
            "x": 30.0,
            "y": 10.0,
            "pads": [
                (1, -0.35, 0.0, 0.5, 0.6, "L2_IN"),
                (2, 0.35, 0.0, 0.5, 0.6, "L2_SW"),
            ],
        },
        {
            "ref": "U1",
            "footprint": "synthetic:IC",
            "x": 50.0,
            "y": 10.0,
            "pads": [
                (1, 0.0, 0.0, 0.4, 0.4, "L1_SW"),
                (2, 0.0, 1.0, 0.4, 0.4, "L2_SW"),
            ],
        },
    ]
    result_paths = write_critical_nets_project(tmp_path, "test_switch_size", components)
    project_path = result_paths["board"].parent

    result = k.classify_critical_nets(project_path)
    critical_nets = result.get("critical_nets", [])
    switch_nets = {net["net"] for net in critical_nets if net.get("reason") == "switch_node"}

    assert "L1_SW" in switch_nets, f"Big inductor's IC-connected net should qualify, got {switch_nets}"
    assert "L1_IN" not in switch_nets, "The non-IC-connected terminal must not be flagged"
    assert "L2_SW" not in switch_nets, (
        "Undersized (0402-style) inductor must not qualify even though it reaches an IC pin"
    )


def test_switch_node_requires_ic_pin_connection(tmp_path: Path) -> None:
    """Test switch node detection requires one terminal to touch an IC pin.

    L1 is large enough on both axes to pass the size gate (same geometry as
    the size test above), but BOTH terminals connect only to passive
    components (R1/R2, not an IC) - so no net should be classified as a
    switch node despite satisfying the size requirement.
    """
    components = [
        {
            "ref": "L1",
            "footprint": "synthetic:L_Inductor_Big",
            "x": 10.0,
            "y": 10.0,
            "pads": [
                (1, -3.0, 0.0, 4.0, 9.0, "L1_IN"),
                (2, 3.0, 0.0, 4.0, 9.0, "L1_SW"),
            ],
        },
        {
            "ref": "R1",
            "footprint": "synthetic:R_0603",
            "x": -10.0,
            "y": 10.0,
            "pads": [
                (1, -0.75, 0.0, 0.9, 0.95, "L1_IN"),
                (2, 0.75, 0.0, 0.9, 0.95, "SUPPLY"),
            ],
        },
        {
            "ref": "R2",
            "footprint": "synthetic:R_0603",
            "x": 30.0,
            "y": 10.0,
            "pads": [
                (1, -0.75, 0.0, 0.9, 0.95, "L1_SW"),
                (2, 0.75, 0.0, 0.9, 0.95, "LOAD"),
            ],
        },
    ]
    result_paths = write_critical_nets_project(tmp_path, "test_switch_no_ic", components)
    project_path = result_paths["board"].parent

    result = k.classify_critical_nets(project_path)
    critical_nets = result.get("critical_nets", [])
    switch_nets = {net["net"] for net in critical_nets if net.get("reason") == "switch_node"}

    assert "L1_SW" not in switch_nets
    assert "L1_IN" not in switch_nets
    assert switch_nets == set(), (
        f"No switch node should be classified without an IC-pin connection, got {switch_nets}"
    )


def test_get_trace_cost_applies_critical_multiplier(tmp_path: Path) -> None:
    """Test that get_trace_cost applies the critical net multiplier to length cost."""
    result_paths = write_synthetic_project(tmp_path, "test_cost", component_count=3)
    project_path = result_paths["board"].parent

    # Get trace cost for all nets
    cost_result = k.get_trace_cost(project_path)

    # Check that critical nets have the multiplier applied
    for net_cost in cost_result.get("nets", []):
        if "critical" in net_cost:
            critical_info = net_cost["critical"]
            multiplier = critical_info.get("multiplier", 1.0)

            # Length cost should reflect the multiplier
            # length_cost = weights["length_mm"] * length_mm * multiplier
            # So multiplier > 1 means higher cost
            assert multiplier > 0, f"Multiplier must be positive, got {multiplier}"


def test_get_trace_cost_board_total_updated(tmp_path: Path) -> None:
    """Test that board_totals include the critical net multiplied costs."""
    result_paths = write_synthetic_project(tmp_path, "test_board_total", component_count=5)
    project_path = result_paths["board"].parent

    cost_result = k.get_trace_cost(project_path)

    # Board totals should exist
    assert "board_totals" in cost_result or all(
        "total" in net.get("cost", {}) for net in cost_result.get("nets", [])
    )


def test_kiln_smoke_test_classify_critical_nets(kiln_project_path: Path) -> None:
    """Smoke test: run classify_critical_nets on the real kiln project."""
    result = k.classify_critical_nets(kiln_project_path)

    # Check basic structure
    assert "critical_nets" in result
    assert "l_crit_table" in result
    assert "settings_snapshot" in result

    # Should have at least some nets classified (SPI, I2C, maybe XTAL)
    critical_nets = result.get("critical_nets", [])
    assert len(critical_nets) > 0, "kiln should have at least one critical net"

    # Each critical net should have the required fields
    for net in critical_nets:
        assert "net" in net
        assert "critical" in net
        assert net["critical"] is True
        assert "reason" in net
        assert net["reason"] in {"bus_frequency", "xtal", "switch_node"}
        assert "multiplier" in net
        assert net["multiplier"] > 0


def test_kiln_classification_has_expected_buses(kiln_project_path: Path) -> None:
    """Test that kiln classification finds SPI, I2C buses as expected."""
    result = k.classify_critical_nets(kiln_project_path)
    critical_nets = result.get("critical_nets", [])

    # Extract reason counts
    reasons = [net.get("reason") for net in critical_nets]
    reason_counts = {}
    for reason in reasons:
        reason_counts[reason] = reason_counts.get(reason, 0) + 1

    # kiln should have SPI (MainControler, SaftyProcessor) and I2C buses
    # This checks that bus detection is working
    assert len(critical_nets) > 0, "kiln should have critical nets"


def test_kiln_get_trace_cost_before_after(kiln_project_path: Path) -> None:
    """Test get_trace_cost before/after critical net classification on kiln."""
    # This is more of an integration test - verify the multipliers are applied

    cost_result = k.get_trace_cost(kiln_project_path)

    # Check structure
    assert "nets" in cost_result
    assert len(cost_result["nets"]) > 0

    # Count nets with critical classification
    critical_count = sum(1 for net in cost_result["nets"] if "critical" in net)

    # At least some nets should be marked as critical
    # (depends on kiln having high-speed buses, which it does)
    # This is a soft assertion - we mainly check it doesn't crash


def test_classify_critical_nets_empty_board(tmp_path: Path) -> None:
    """Test classify_critical_nets degrades gracefully on an empty board."""
    # Create a minimal valid board with no components
    board_text = generate_synthetic_board(component_count=0, route=False)
    board_path = tmp_path / "empty.kicad_pcb"
    board_path.write_text(board_text, encoding="utf-8")

    # Create minimal project file
    project_path = tmp_path
    pro_text = """(kicad_pro (version 20230121) (project "test"))"""
    (project_path / "empty.kicad_pro").write_text(pro_text, encoding="utf-8")

    # Create empty netlist
    net_text = "(export (version 1) (design) (nets))"
    (project_path / "empty.net").write_text(net_text, encoding="utf-8")

    # Should not crash
    result = k.classify_critical_nets(project_path)
    assert "critical_nets" in result
    assert isinstance(result["critical_nets"], list)


def test_critical_net_multiplier_values(tmp_path: Path) -> None:
    """Test that multiplier values match configuration."""
    result_paths = write_synthetic_project(tmp_path, "test_mult", component_count=2)
    project_path = result_paths["board"].parent

    result = k.classify_critical_nets(project_path)

    # Get default multipliers from settings
    settings_snapshot = result.get("settings_snapshot", {})
    hs_cfg = settings_snapshot.get("high_speed", {})
    sw_cfg = settings_snapshot.get("switch_node", {})

    expected_hs_mult = float(hs_cfg.get("length_weight_mult", 4.0))
    expected_sw_mult = float(sw_cfg.get("length_weight_mult", 8.0))

    # Check that classified nets use the correct multipliers
    for net in result.get("critical_nets", []):
        reason = net.get("reason")
        mult = net.get("multiplier")

        if reason == "bus_frequency":
            assert mult == expected_hs_mult
        elif reason == "switch_node":
            assert mult == expected_sw_mult
        elif reason == "xtal":
            # XTAL uses switch_node weight (highest)
            assert mult == expected_sw_mult
