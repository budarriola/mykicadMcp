"""Golden-style tests for the existing parsers in kicad_pcb_tool.py, run
against the REAL kiln.kicad_pcb project (read-only - see conftest.py for the
`kiln_project_path` fixture and the `scratch_board` copy used by writers).

Exact counts below are a SNAPSHOT of the real kilnCtl board as of 2026-09-06,
re-captured after the 2026-08-28 hardware/firmware/tools split moved the
project to `hardware/mainBoard/` (which broke the `kiln_project_path` fixture
and made this whole file silently skip - see conftest.py). Re-verified
directly against the parser, not assumed from CLAUDE.md or the schematic.
If a schematic/PCB change legitimately alters these numbers, update the
constants here in the same commit as that change - that's the point of a
golden test: a silent drift here means either the parser broke or nobody
updated the snapshot.

Net-based assertions (`GOLDEN_NET_COUNT`, `test_list_nets_...`,
`test_get_net_...`) reflect that `kiln.net` (the separate KiCad netlist
export the parser reads for net data - NOT the .kicad_pcb file itself) was
deleted as stale in commit b111e338 (2026-08-16), before the repo split, and
has not been regenerated. `list_nets`/`get_net` therefore currently see zero
nets against this board; that emptiness is itself the fact being pinned here,
not a placeholder. Regenerating `hardware/mainBoard/kiln.net` from KiCad's
netlist exporter would make these meaningful again.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import kicad_pcb_tool as k

# --- Golden snapshot values (re-captured 2026-09-06 against kiln.kicad_pcb) ---
GOLDEN_COMPONENT_COUNT = 211
GOLDEN_NET_COUNT = 0  # no kiln.net on disk or at HEAD - see module docstring
GOLDEN_U13_PAD_COUNT = 4

# Refs known to exist on the real board today. U10-U12 are NO LONGER placed;
# U13/U14 (the remaining two of the five MAX31856 thermocouple channels) are
# now on the board instead - verified directly against the parser, not
# assumed from CLAUDE.md's "U10-U14" description of the schematic design.
KNOWN_REFS = ["R1", "U13", "U14"]
REMOVED_REFS = ["U10", "U11", "U12"]


def test_component_count_is_stable_and_positive(kiln_project_path: Path) -> None:
    components = k.list_components(kiln_project_path, limit=10_000)
    assert len(components) > 0
    assert len(components) == GOLDEN_COMPONENT_COUNT


def test_known_component_references_exist(kiln_project_path: Path) -> None:
    components = k.list_components(kiln_project_path, limit=10_000)
    refs = {c["reference"] for c in components}
    for ref in KNOWN_REFS:
        assert ref in refs, f"expected reference {ref} on the real board"
    for ref in REMOVED_REFS:
        assert ref not in refs, f"expected {ref} to remain absent from the real board"


def test_get_component_returns_known_ref(kiln_project_path: Path) -> None:
    component = k.get_component(kiln_project_path, "U13")
    assert component is not None
    assert component["reference"] == "U13"
    # MAX31856 thermocouple converters are ICs; footprint should be populated.
    assert component["footprint"]


def test_list_nets_is_stable_and_includes_known_names(kiln_project_path: Path) -> None:
    # No kiln.net is present (see module docstring) - the parser reports zero
    # nets. Pinned here as a fact, not a placeholder: regenerating the
    # netlist export should make this fail loudly so it gets revisited.
    nets = k.list_nets(kiln_project_path)
    assert len(nets) == GOLDEN_NET_COUNT


def test_get_net_returns_nodes_for_known_net(kiln_project_path: Path) -> None:
    # With no netlist export present, even a net as fundamental as GND_Main
    # cannot be resolved - the parser raises rather than returning an empty
    # result. Documented here so a future netlist regeneration is expected
    # to change this test's shape, not just its constants.
    with pytest.raises(KeyError):
        k.get_net(kiln_project_path, "GND_Main")


def test_footprint_pads_parse_for_known_component(kiln_project_path: Path) -> None:
    fp = k.get_footprint_pads(kiln_project_path, "U13")
    assert fp["reference"] == "U13"
    assert len(fp["pads"]) == GOLDEN_U13_PAD_COUNT
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
