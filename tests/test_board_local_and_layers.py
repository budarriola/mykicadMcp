"""Tests for Phase 7.1 (board-local state JSON + confirmed-bus reuse) and
Phase 7.2 (board layer purposes, net-kind classification, layer_penalty in
`get_trace_cost`).

Everything that WRITES a `.board_local.json` does so only inside pytest tmp
dirs (synthetic projects) - never in the real kilnCtl project directory.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import kicad_pcb_tool as k
from synthetic_board import (
    generate_synthetic_board,
    write_multidrop_spi_project,
    write_synthetic_project,
)


# --- Phase 7.1: load/save round-trip ----------------------------------------


def test_board_local_missing_file_is_empty_state(tmp_path: Path) -> None:
    write_synthetic_project(tmp_path, component_count=2)
    state = k.load_board_local(tmp_path)
    assert state["loaded_from_file"] is False
    assert state["data"] == {}
    # File is named after the board stem, next to the board file.
    assert state["board_local_path"] == str(tmp_path / "synthetic.board_local.json")
    assert not Path(state["board_local_path"]).exists()  # loading never creates it


def test_board_local_round_trip_preserves_unknown_keys(tmp_path: Path) -> None:
    write_synthetic_project(tmp_path, component_count=2)
    data = {
        "version": 1,
        "autorouter_owned": {"segments": ["uuid-a", "uuid-b"], "vias": ["uuid-v"]},
        "keepouts": [{"layer": "F.Cu", "rect": [0, 0, 10, 10], "note": "antenna area"}],
        "net_overrides": {"NET_1_A": {"priority": 10, "layers": ["F.Cu"]}},
        "confirmed_buses": [],
        "last_route_session": {"routed": ["NET_1_A"], "failed": [], "grid_mm": 0.2},
        # Unknown keys (e.g. from a newer tool version) must survive verbatim.
        "future_tool_state": {"nested": {"deep": [1, 2, 3]}, "flag": True},
    }
    saved = k.save_board_local(tmp_path, data)
    assert saved["written"] is True
    assert Path(saved["board_local_path"]).name == "synthetic.board_local.json"

    state = k.load_board_local(tmp_path)
    assert state["loaded_from_file"] is True
    assert state["data"] == data
    assert state["data"]["future_tool_state"] == {"nested": {"deep": [1, 2, 3]}, "flag": True}
    # The returned data is a copy - mutating it must not alias the caller's dict.
    state["data"]["autorouter_owned"]["segments"].append("uuid-c")
    assert data["autorouter_owned"]["segments"] == ["uuid-a", "uuid-b"]


def test_board_local_rejects_non_dict(tmp_path: Path) -> None:
    write_synthetic_project(tmp_path, component_count=1)
    (tmp_path / "synthetic.board_local.json").write_text("[1, 2, 3]\n", encoding="utf-8")
    with pytest.raises(ValueError):
        k.load_board_local(tmp_path)
    with pytest.raises(ValueError):
        k.save_board_local(tmp_path, ["not", "a", "dict"])  # type: ignore[arg-type]


# --- Phase 7.1: confirmed-bus reuse in detect_buses -------------------------


def test_detect_buses_reuses_cached_confirmations(tmp_path: Path) -> None:
    write_multidrop_spi_project(tmp_path, destinations=2)

    first = k.detect_buses(tmp_path)
    spi = next(c for c in first["candidates"] if c["bus_type"] == "SPI")
    # Nothing recorded yet: every candidate unconfirmed.
    assert all(c["confirmed"] is False for c in first["candidates"])
    assert first["confirmed_count"] == 0

    recorded = k.record_confirmed_bus(tmp_path, spi, name="SPI_Main")
    assert recorded["entry"]["bus_type"] == "SPI"
    assert recorded["entry"]["name"] == "SPI_Main"
    assert recorded["confirmed_bus_count"] == 1
    assert Path(recorded["board_local_path"]).exists()

    second = k.detect_buses(tmp_path)
    spi_again = next(c for c in second["candidates"] if c["bus_type"] == "SPI")
    assert spi_again["confirmed"] is True
    assert spi_again["confirmed_on"] == recorded["entry"]["confirmed_on"]
    assert spi_again["confirmed_name"] == "SPI_Main"
    assert second["confirmed_count"] == 1
    # Every OTHER candidate still needs user confirmation.
    others = [c for c in second["candidates"] if c is not spi_again]
    assert all(c["confirmed"] is False for c in others)

    # Re-recording the same bus replaces, never duplicates.
    rerecorded = k.record_confirmed_bus(tmp_path, spi, name="SPI_Renamed")
    assert rerecorded["replaced_existing"] is True
    assert rerecorded["confirmed_bus_count"] == 1


# --- Phase 7.2: board layer parsing -----------------------------------------


def test_parse_board_layers_kiln_golden(kiln_project_path: Path) -> None:
    result = k.get_board_layers(kiln_project_path)
    assert result["copper_layer_count"] == 4
    by_name = {layer["name"]: layer for layer in result["layers"]}
    assert by_name["F.Cu"]["type"] == "signal"
    assert by_name["B.Cu"]["type"] == "signal"
    assert by_name["In1.Cu"]["type"] == "power"
    assert by_name["In2.Cu"]["type"] == "power"
    assert by_name["In1.Cu"]["user_name"] == "In1.Cu_GND"
    assert by_name["In2.Cu"]["user_name"] == "In2.Cu_power"
    # File order is KiCad's physical stack order, front to back.
    assert [layer["name"] for layer in result["layers"]] == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
    assert result["type_counts"] == {"signal": 2, "power": 2}


def test_parse_board_layers_synthetic_n_layer(tmp_path: Path) -> None:
    board_path = tmp_path / "six.kicad_pcb"
    board_path.write_text(generate_synthetic_board(component_count=1, layers=6), encoding="utf-8")
    layers = k._parse_board_layers(board_path)
    # 6-layer stack: F.Cu + In1..In4 (alternating signal/power, power on even
    # inner index per _layer_stack_lines) + B.Cu.
    assert [(l["name"], l["type"]) for l in layers] == [
        ("F.Cu", "signal"),
        ("In1.Cu", "signal"),
        ("In2.Cu", "power"),
        ("In3.Cu", "signal"),
        ("In4.Cu", "power"),
        ("B.Cu", "signal"),
    ]
    # Non-copper layers (silk, mask, ...) never appear.
    assert all(l["name"].endswith(".Cu") for l in layers)


# --- Phase 7.2: net-kind classification -------------------------------------


@pytest.mark.parametrize(
    ("net_name", "expected"),
    [
        ("GND_Main", "power"),
        ("/Power/GND", "power"),  # anchored ^GND must still catch the basename
        ("+5V", "power"),
        ("12V_Main", "power"),
        ("/Regulators/3.3V", "power"),
        ("VCC_IO", "power"),
        ("/MainControler/MOSI", "signal"),
        ("/MainControler/CLK", "signal"),
        ("NET_1_A", "signal"),
    ],
)
def test_net_kind_pattern_matching(net_name: str, expected: str) -> None:
    assert k._net_kind(net_name) == expected


def test_net_kind_netclass_indicates_power() -> None:
    # Name alone says signal, but the assigned netclass marks it a supply net.
    assert k._net_kind("/Sheet/RAIL_A", netclass="Power") == "power"
    assert k._net_kind("/Sheet/RAIL_A", netclass="Default") == "signal"


# --- Phase 7.2: layer_penalty in get_trace_cost -----------------------------


def _write_two_net_layer_board(directory: Path) -> None:
    """Synthetic 4-layer project (In2.Cu is a power layer) with two identical
    1.5 mm signal-net segments: NET_1_A on F.Cu (signal layer) and NET_2_A on
    In2.Cu (power layer). Same geometry, only the layer differs."""
    write_synthetic_project(directory, component_count=2, layers=4, route=False)
    board_path = directory / "synthetic.kicad_pcb"
    text = board_path.read_text(encoding="utf-8")
    segments = (
        '    (segment (start 4.25 10) (end 5.75 10) (width 0.2) (layer "F.Cu") (net "NET_1_A") (uuid "seg-sig"))\n'
        '    (segment (start 9.25 10) (end 10.75 10) (width 0.2) (layer "In2.Cu") (net "NET_2_A") (uuid "seg-pwr"))\n'
    )
    assert text.endswith(")\n")
    board_path.write_text(text[: -2] + segments + ")\n", encoding="utf-8")


def test_layer_penalty_signal_net_on_power_layer(tmp_path: Path) -> None:
    _write_two_net_layer_board(tmp_path)

    on_signal_layer = k.get_trace_cost(tmp_path, "NET_1_A")
    assert on_signal_layer["net_kind"] == "signal"
    assert on_signal_layer["cost"]["layer_penalty"] == 0.0
    assert on_signal_layer["metrics"]["layer_lengths_mm"] == {"F.Cu": 1.5}

    on_power_layer = k.get_trace_cost(tmp_path, "NET_2_A")
    assert on_power_layer["net_kind"] == "signal"
    # 1.5 mm x (layer_purpose.signal.power 4.0 - 1) x weights.length_mm 1.0
    assert on_power_layer["cost"]["layer_penalty"] == pytest.approx(4.5)
    assert on_power_layer["metrics"]["layer_lengths_mm"] == {"In2.Cu": 1.5}
    # The penalty is its own breakdown term AND included in the total.
    assert on_power_layer["cost"]["total"] == pytest.approx(
        on_power_layer["cost"]["length"] + on_power_layer["cost"]["layer_penalty"]
    )
    assert on_power_layer["cost"]["total"] > on_signal_layer["cost"]["total"]


def test_layer_penalty_in_board_totals_and_weights_used(tmp_path: Path) -> None:
    _write_two_net_layer_board(tmp_path)
    ranked = k.get_trace_cost(tmp_path)
    assert ranked["board_totals"]["layer_penalty"] == pytest.approx(4.5)
    assert ranked["board_totals"]["total"] == pytest.approx(
        ranked["board_totals"]["length"] + ranked["board_totals"]["layer_penalty"]
    )
    lp = ranked["weights_used"]["layer_purpose"]
    assert lp["signal"]["power"] == 4.0
    assert lp["power_net_patterns"]  # self-describing result


def test_real_kiln_project_dir_gets_no_board_local_file(kiln_project_path: Path) -> None:
    """Reading tools (detect_buses, get_trace_cost) must never create the
    board-local state file in the real project directory."""
    real_state = kiln_project_path / "kiln.board_local.json"
    existed_before = real_state.exists()
    k.detect_buses(kiln_project_path)
    k.get_trace_cost(kiln_project_path, "/MainControler/MOSI")
    assert real_state.exists() == existed_before
