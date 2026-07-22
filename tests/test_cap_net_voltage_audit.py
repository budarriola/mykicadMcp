"""Tests for Phase 8's net-aware capacitor voltage audit:
`kicad_pcb_tool._infer_net_voltage` (8.1's standalone net-name voltage
inference helper) and `kicad_pcb_tool.audit_capacitor_net_voltages` (8.2's
per-capacitor check), registered as the `audit_kicad_capacitor_net_voltages`
MCP tool.
"""

from __future__ import annotations

import json
from pathlib import Path

import kicad_pcb_tool as k

from synthetic_cap_schematic import write_synthetic_cap_project

_DEFAULT_GND_TOKENS = ["GND", "AGND", "DGND", "PGND", "VSS"]


# --- 8.1: _infer_net_voltage precedence -------------------------------------


def test_infer_net_voltage_override_beats_gnd_and_label() -> None:
    # "GND_5V_RTN" would normally resolve via the GND rule (contains "GND"),
    # but an explicit override for the exact basename must win outright.
    info = k._infer_net_voltage("GND_5V_RTN", {"GND_5V_RTN": 9.9}, _DEFAULT_GND_TOKENS)
    assert info["voltage"] == 9.9
    assert info["source"] == "override"
    assert info["ambiguous_label"] is False


def test_infer_net_voltage_override_is_case_insensitive() -> None:
    info = k._infer_net_voltage("vbus", {"VBUS": 5.0}, [])
    assert info["voltage"] == 5.0
    assert info["source"] == "override"


def test_infer_net_voltage_gnd_beats_label_and_flags_ambiguous() -> None:
    # No override present: GND_5V_RTN contains both a gnd token ("GND") and a
    # voltage label ("5V") - GND must win (0.0V) but the collision must still
    # be surfaced via ambiguous_label.
    info = k._infer_net_voltage("GND_5V_RTN", {}, _DEFAULT_GND_TOKENS)
    assert info["voltage"] == 0.0
    assert info["source"] == "gnd"
    assert info["ambiguous_label"] is True


def test_infer_net_voltage_plain_gnd() -> None:
    for name in ("GND_Main", "GND_Safty", "AGND", "/MainControler/DGND"):
        info = k._infer_net_voltage(name, {}, _DEFAULT_GND_TOKENS)
        assert info["voltage"] == 0.0, name
        assert info["source"] == "gnd", name
        assert info["ambiguous_label"] is False, name


def test_infer_net_voltage_labeled_and_digit_v_digit_convention() -> None:
    cases = {
        "12V_Main": 12.0,
        "3.3v_Safty": 3.3,
        "+5V": 5.0,
        "3V3_Safety": 3.3,
        "1V8_Core": 1.8,
    }
    for name, expected in cases.items():
        info = k._infer_net_voltage(name, {}, [])
        assert info["voltage"] == expected, name
        assert info["source"] == "label", name
        assert info["ambiguous_label"] is False, name


def test_infer_net_voltage_ambiguous_multi_token_takes_largest() -> None:
    info = k._infer_net_voltage("12V_TO_5V_CONV", {}, [])
    assert info["voltage"] == 12.0
    assert info["source"] == "label"
    assert info["ambiguous_label"] is True


def test_infer_net_voltage_unlabeled() -> None:
    info = k._infer_net_voltage("SIG_UNLABELED", {}, _DEFAULT_GND_TOKENS)
    assert info["voltage"] is None
    assert info["source"] == "none"
    assert info["ambiguous_label"] is False


# --- 8.2: audit_capacitor_net_voltages ---------------------------------------


_CAPACITORS = [
    # under_rated: 6.3V cap sees a full 12V differential.
    {"ref": "C1", "value": "10uF 6.3V", "nets": ["12V_Main", "GND_Main"]},
    # under_derated: 16V rated >= 12V applied but < 2x derating minimum (24V).
    {"ref": "C2", "value": "10uF 16V", "nets": ["12V_Main", "GND_Main"]},
    # ok: 25V rated >= 2x12V = 24V required minimum.
    {"ref": "C3", "value": "10uF 25V", "nets": ["12V_Main", "GND_Main"]},
    # unknown_rating: both nets resolve (3.3V), Value states no voltage, and
    # no default_cap_rating is configured.
    {"ref": "C4", "value": "0.1uF", "nets": ["3V3", "GND_Main"]},
    # one_net_unlabeled: 3V3_A resolves, SIG_UNLABELED does not.
    {"ref": "C5", "value": "0.1uF 50V", "nets": ["3V3_A", "SIG_UNLABELED"]},
    # no_labeled_nets: neither side resolves.
    {"ref": "C6", "value": "0.1uF 50V", "nets": ["SIG_A", "SIG_B"]},
    # DNP: would otherwise be under_rated, but must be excluded entirely.
    {"ref": "C7", "value": "10uF 6.3V", "nets": ["12V_Main", "GND_Main"], "dnp": True},
    # unsupported_pins: 3 netlist pins, not 2 (array/4-terminal style cap).
    {"ref": "C8", "value": "10uF 25V", "nets": ["12V_Main", "GND_Main", "EXTRA"]},
]


def _build_project(tmp_path: Path, capacitors=None, settings: dict | None = None) -> Path:
    directory = tmp_path / "capproj"
    write_synthetic_cap_project(directory, "capproj", capacitors if capacitors is not None else _CAPACITORS)
    if settings is not None:
        (directory / "pcb_settings.json").write_text(json.dumps(settings), encoding="utf-8")
    return directory


def test_audit_capacitor_net_voltages_verdicts(tmp_path: Path) -> None:
    directory = _build_project(tmp_path)
    result = k.audit_capacitor_net_voltages(directory)

    by_ref = {row["reference"]: row for row in result["capacitors"]}

    # DNP capacitor must not appear at all.
    assert "C7" not in by_ref
    assert result["capacitor_count"] == 7

    assert by_ref["C1"]["verdict"] == "under_rated"
    assert by_ref["C1"]["applied_v"] == 12.0
    assert by_ref["C1"]["rated_v"] == 6.3

    assert by_ref["C2"]["verdict"] == "under_derated"
    assert by_ref["C2"]["applied_v"] == 12.0
    assert by_ref["C2"]["required_min"] == 24.0
    assert by_ref["C2"]["rated_v"] == 16.0

    assert by_ref["C3"]["verdict"] == "ok"
    assert by_ref["C3"]["applied_v"] == 12.0
    assert by_ref["C3"]["rated_v"] == 25.0

    assert by_ref["C4"]["verdict"] == "unknown_rating"
    assert by_ref["C4"]["applied_v"] == 3.3
    assert by_ref["C4"]["rated_v"] is None

    assert by_ref["C5"]["verdict"] == "one_net_unlabeled"
    assert by_ref["C5"]["assumed_applied_v"] == 3.3
    assert by_ref["C5"]["applied_v"] is None

    assert by_ref["C6"]["verdict"] == "no_labeled_nets"
    assert by_ref["C6"]["applied_v"] is None

    assert by_ref["C8"]["verdict"] == "unsupported_pins"
    assert by_ref["C8"]["applied_v"] is None

    summary = result["summary"]
    assert summary["under_rated"] == 1
    assert summary["under_derated"] == 1
    assert summary["ok"] == 1
    assert summary["unknown_rating"] == 1
    assert summary["one_net_unlabeled"] == 1
    assert summary["no_labeled_nets"] == 1
    assert summary["unsupported_pins"] == 1
    assert summary["total"] == 7

    # Settings actually used must be echoed back (self-describing, no
    # pcb_settings.json present here so these are the in-code defaults).
    assert result["settings_used"]["derating_min_ratio"] == 2.0
    assert result["settings_used"]["loaded_from_file"] is False

    # No board/netlist mismatch since the synthetic project's pads and .net
    # nodes agree 1:1 by construction.
    assert result["stale_netlist_warnings"] == []


def test_audit_capacitor_net_voltages_sort_order(tmp_path: Path) -> None:
    directory = _build_project(tmp_path)
    result = k.audit_capacitor_net_voltages(directory)
    verdicts_in_order = [row["verdict"] for row in result["capacitors"]]
    expected_order = [
        "under_rated",
        "unknown_rating",
        "under_derated",
        "ok",
        "one_net_unlabeled",
        "no_labeled_nets",
        "unsupported_pins",
    ]
    # Every verdict present must appear in this relative order (worst-first).
    seen_positions = [expected_order.index(v) for v in verdicts_in_order]
    assert seen_positions == sorted(seen_positions)


def test_audit_capacitor_net_voltages_net_voltages_override(tmp_path: Path) -> None:
    # VBUS carries no number in its name, so it only resolves via an explicit
    # net_voltages override in pcb_settings.json's schematic_checks.cap_voltage.
    capacitors = [
        {"ref": "C1", "value": "10uF 4V", "nets": ["VBUS", "GND_Main"]},
    ]
    settings = {"schematic_checks": {"cap_voltage": {"net_voltages": {"VBUS": 5.0}}}}
    directory = _build_project(tmp_path, capacitors=capacitors, settings=settings)
    result = k.audit_capacitor_net_voltages(directory)

    assert result["settings_used"]["loaded_from_file"] is True
    assert result["settings_used"]["net_voltages"] == {"VBUS": 5.0}

    row = result["capacitors"][0]
    vbus_net = next(n for n in row["nets"] if n["name"] == "VBUS")
    assert vbus_net["source"] == "override"
    assert vbus_net["voltage"] == 5.0
    assert row["applied_v"] == 5.0
    # rated 4V < applied 5V -> hard fail.
    assert row["verdict"] == "under_rated"


def test_audit_capacitor_net_voltages_default_cap_rating(tmp_path: Path) -> None:
    # A cap with no stated voltage on labeled nets falls back to
    # default_cap_rating instead of unknown_rating when one is configured.
    capacitors = [
        {"ref": "C1", "value": "0.1uF", "nets": ["12V_Main", "GND_Main"]},
    ]
    settings = {"schematic_checks": {"cap_voltage": {"default_cap_rating": 25.0}}}
    directory = _build_project(tmp_path, capacitors=capacitors, settings=settings)
    result = k.audit_capacitor_net_voltages(directory)

    row = result["capacitors"][0]
    assert row["rated_v"] == 25.0
    assert row["rated_v_source"] == "default"
    assert row["applied_v"] == 12.0
    assert row["verdict"] == "ok"  # 25 >= 2*12


def test_audit_capacitor_net_voltages_derating_ratio_override(tmp_path: Path) -> None:
    capacitors = [
        {"ref": "C1", "value": "10uF 16V", "nets": ["12V_Main", "GND_Main"]},
    ]
    # With ratio=1.0, rated (16) >= applied (12) is enough to be "ok" -
    # normally (ratio=2.0) this same cap is under_derated (see C2 above).
    settings = {"schematic_checks": {"cap_voltage": {"derating_min_ratio": 1.0}}}
    directory = _build_project(tmp_path, capacitors=capacitors, settings=settings)
    result = k.audit_capacitor_net_voltages(directory)
    row = result["capacitors"][0]
    assert row["verdict"] == "ok"
    assert row["required_min"] == 12.0
