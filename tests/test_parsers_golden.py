"""Golden-style tests for the existing parsers in kicad_pcb_tool.py, run
against the REAL kiln.kicad_pcb project (read-only - see conftest.py for the
`kiln_project_path` fixture and the `scratch_board` copy used by writers).

Exact counts below are a SNAPSHOT of the real kilnCtl board as of 2026-07-21.
If a schematic/PCB change legitimately alters these numbers, update the
constants here in the same commit as that change - that's the point of a
golden test: a silent drift here means either the parser broke or nobody
updated the snapshot.
"""

from __future__ import annotations

from pathlib import Path

import kicad_pcb_tool as k

# --- Golden snapshot values (captured 2026-07-21 against kiln.kicad_pcb) ---
GOLDEN_COMPONENT_COUNT = 259
GOLDEN_NET_COUNT = 236
GOLDEN_U10_PAD_COUNT = 5

# Refs known to exist on the real board today. Note: only U1-U12 are
# currently placed on the PCB (U13/U14 from the 5-channel thermocouple
# schematic are not yet on the board) - verified directly against the parser,
# not assumed from the schematic.
KNOWN_REFS = ["R1", "U10", "U11", "U12"]


def test_component_count_is_stable_and_positive(kiln_project_path: Path) -> None:
    components = k.list_components(kiln_project_path, limit=10_000)
    assert len(components) > 0
    assert len(components) == GOLDEN_COMPONENT_COUNT


def test_known_component_references_exist(kiln_project_path: Path) -> None:
    components = k.list_components(kiln_project_path, limit=10_000)
    refs = {c["reference"] for c in components}
    for ref in KNOWN_REFS:
        assert ref in refs, f"expected reference {ref} on the real board"


def test_get_component_returns_known_ref(kiln_project_path: Path) -> None:
    component = k.get_component(kiln_project_path, "U10")
    assert component is not None
    assert component["reference"] == "U10"
    # MAX31856 thermocouple converters are ICs; footprint should be populated.
    assert component["footprint"]


def test_list_nets_is_stable_and_includes_known_names(kiln_project_path: Path) -> None:
    nets = k.list_nets(kiln_project_path)
    assert len(nets) > 0
    assert len(nets) == GOLDEN_NET_COUNT
    names = {n["name"] for n in nets}
    assert "GND_Main" in names


def test_get_net_returns_nodes_for_known_net(kiln_project_path: Path) -> None:
    result = k.get_net(kiln_project_path, "GND_Main")
    assert result["component_count"] > 0
    refs = set(result["component_references"])
    assert refs  # at least one component sits on GND_Main


def test_footprint_pads_parse_for_known_component(kiln_project_path: Path) -> None:
    fp = k.get_footprint_pads(kiln_project_path, "U10")
    assert fp["reference"] == "U10"
    assert len(fp["pads"]) == GOLDEN_U10_PAD_COUNT
    for pad in fp["pads"]:
        assert "number" in pad
        assert "position" in pad
        assert "x" in pad["position"] and "y" in pad["position"]


def test_component_connections_for_known_component(kiln_project_path: Path) -> None:
    result = k.get_component_connections(kiln_project_path, "R1")
    assert result is not None


def test_inspect_project_shape(kiln_project_path: Path) -> None:
    summary = k.inspect_project(kiln_project_path)
    assert summary["component_count"] == GOLDEN_COMPONENT_COUNT
    assert summary["net_count"] == GOLDEN_NET_COUNT
    assert "components" in summary and "nets" in summary
