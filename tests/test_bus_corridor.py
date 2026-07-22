"""Tests for Phase 5 (`measure_bus_corridor_areas`) and the Phase 6 deviation
unstub in `get_trace_cost`.

Synthetic multi-drop SPI projects (hub U1 + N slave ICs) come from
`synthetic_board.write_multidrop_spi_project`; a couple of golden checks also
run against the real kiln board via the `kiln_project_path` fixture.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import kicad_pcb_tool as k

from synthetic_board import write_multidrop_spi_project, write_synthetic_project


# --- Phase 5: corridor measurement ----------------------------------------


def test_multidrop_spi_has_per_destination_bundles_with_positive_corridor(tmp_path: Path) -> None:
    write_multidrop_spi_project(tmp_path, destinations=2)
    cand = next(c for c in k.detect_buses(tmp_path)["candidates"] if c["bus_type"] == "SPI" and c["qualified"])
    m = k.measure_bus_corridor_areas(tmp_path, bus=cand)

    assert m["grouped"] is True
    assert m["hub_ic"] == "U1"
    # one bundle per destination IC
    assert {b["destination_ic"] for b in m["bundles"]} == {"U2", "U3"}
    for b in m["bundles"]:
        assert b["trace_count"] >= 2
        assert b["corridor_area_mm2"] > 0
        # hull is always an upper bound on the corridor area
        assert b["hull_area_mm2"] >= b["corridor_area_mm2"] - 1e-6
    # unassigned count is always reported (an int, never dropped silently)
    assert isinstance(m["unassigned_segment_count"], int)
    # shared copper is counted once per bundle it serves -> sum can exceed hull
    assert m["sum_of_bundle_areas_mm2"] > 0
    assert m["union_hull_area_mm2"] > 0


def test_shared_nets_are_clipped_into_each_destination_bundle(tmp_path: Path) -> None:
    write_multidrop_spi_project(tmp_path, destinations=2)
    cand = next(c for c in k.detect_buses(tmp_path)["candidates"] if c["bus_type"] == "SPI" and c["qualified"])
    m = k.measure_bus_corridor_areas(tmp_path, bus=cand)

    for b in m["bundles"]:
        roles = {n["net"].rsplit("/", 1)[-1]: n["role"] for n in b["nets"]}
        # the three shared nets appear (clipped) in every destination bundle
        assert roles.get("SCK") == "shared"
        assert roles.get("MOSI") == "shared"
        assert roles.get("MISO") == "shared"
        # exactly one dedicated CS per bundle
        dedicated = [net for net, role in roles.items() if role == "dedicated"]
        assert len(dedicated) == 1 and dedicated[0].startswith("CS")


def test_single_destination_is_one_bundle_no_clipping(tmp_path: Path) -> None:
    write_multidrop_spi_project(tmp_path, destinations=1)
    cand = next(c for c in k.detect_buses(tmp_path)["candidates"] if c["bus_type"] == "SPI" and c["qualified"])
    m = k.measure_bus_corridor_areas(tmp_path, bus=cand)

    assert m["grouped"] is True
    assert len(m["bundles"]) == 1
    b = m["bundles"][0]
    assert b["destination_ic"] == "U2"
    # single destination -> nothing is clipped away, no unassigned copper
    assert m["unassigned_segment_count"] == 0
    assert b["corridor_area_mm2"] > 0


def test_no_hub_reports_grouped_false(tmp_path: Path) -> None:
    # Two isolated resistor nets share no common IC -> no hub can be found.
    write_synthetic_project(tmp_path, project_name="synth", component_count=3)
    m = k.measure_bus_corridor_areas(tmp_path, nets=["NET_1_A", "NET_2_A"])
    assert m["grouped"] is False
    assert m["bundles"] == []
    assert m["hub_ic"] is None
    assert m["union_hull_area_mm2"] >= 0


def test_explicit_nets_and_hub_ic_input(tmp_path: Path) -> None:
    res = write_multidrop_spi_project(tmp_path, destinations=2)
    nets = list(res["layout"]["netlist_nodes"].keys())
    m = k.measure_bus_corridor_areas(tmp_path, nets=nets, hub_ic="U1", bus_type="SPI")
    assert m["grouped"] is True
    assert m["hub_ic"] == "U1"
    assert m["bus_type"] == "SPI"
    assert len(m["bundles"]) == 2


def test_unrouted_bus_has_zero_corridor_area(tmp_path: Path) -> None:
    write_multidrop_spi_project(tmp_path, destinations=2, route=False)
    m = k.measure_bus_corridor_areas(tmp_path, nets=["/SPI/SCK", "/SPI/MOSI", "/SPI/MISO", "/SPI/CS0", "/SPI/CS1"], hub_ic="U1")
    # no copper -> corridors and hull collapse to zero, nothing crashes
    assert m["sum_of_bundle_areas_mm2"] == 0
    for b in m["bundles"]:
        assert b["corridor_area_mm2"] == 0


def test_clip_band_mult_is_respected(tmp_path: Path) -> None:
    write_multidrop_spi_project(tmp_path, destinations=2)
    m = k.measure_bus_corridor_areas(tmp_path, nets=["/SPI/SCK", "/SPI/MOSI", "/SPI/MISO", "/SPI/CS0", "/SPI/CS1"], hub_ic="U1", clip_band_mult=5.0)
    # band = 5 x dominant width (0.2) = 1.0 mm
    assert m["clip_band_mm"] == pytest.approx(1.0, abs=1e-6)


# --- Phase 6: deviation-term unstub ---------------------------------------


def test_deviating_net_costs_more_than_straight(tmp_path: Path) -> None:
    straight_dir = tmp_path / "straight"
    bowed_dir = tmp_path / "bowed"
    write_multidrop_spi_project(straight_dir, project_name="spi", destinations=1)
    write_multidrop_spi_project(bowed_dir, project_name="spi", destinations=1, deviate_net="/SPI/SCK")

    straight = k.get_trace_cost(straight_dir, "/SPI/SCK")
    bowed = k.get_trace_cost(bowed_dir, "/SPI/SCK")

    assert straight["on_bus"] is True
    assert bowed["on_bus"] is True
    # the bowed net deviates further from the bundle centerline -> higher
    # deviation term and higher total cost
    assert bowed["cost"]["deviation"] > straight["cost"]["deviation"]
    assert bowed["bundle"]["deviation_value"] > straight["bundle"]["deviation_value"]


def test_non_bus_net_keeps_stub_behavior(tmp_path: Path) -> None:
    # A plain isolated-net project has no detectable bus: every net stays off-bus.
    write_synthetic_project(tmp_path, project_name="synth", component_count=4)
    r = k.get_trace_cost(tmp_path, "NET_1_A")
    assert r["on_bus"] is False
    assert r["bundle"] is None
    assert r["cost"]["deviation"] == 0.0  # == trace_cost.non_bus_deviation default


def test_bus_net_is_flagged_on_bus_with_bundle_object(tmp_path: Path) -> None:
    write_multidrop_spi_project(tmp_path, destinations=2)
    r = k.get_trace_cost(tmp_path, "/SPI/SCK")
    assert r["on_bus"] is True
    b = r["bundle"]
    assert b is not None
    assert b["bus_type"] == "SPI"
    assert b["hub_ic"] == "U1"
    assert b["role"] == "shared"
    # a shared net is measured against every destination bundle it serves
    assert set(b["destinations"]) == {"U2", "U3"}


def test_dedicated_cs_is_on_bus_single_destination(tmp_path: Path) -> None:
    write_multidrop_spi_project(tmp_path, destinations=2)
    r = k.get_trace_cost(tmp_path, "/SPI/CS0")
    assert r["on_bus"] is True
    assert r["bundle"]["role"] == "dedicated"
    assert r["bundle"]["destinations"] == ["U2"]


# --- Real-board golden checks (read-only) ---------------------------------


def test_real_board_three_qualified_buses_measure(kiln_project_path: Path) -> None:
    cands = [c for c in k.detect_buses(kiln_project_path)["candidates"] if c["qualified"]]
    by = {(c["bus_type"], c["group_prefix"]): c for c in cands}
    assert ("I2C", "/MainControler/") in by
    assert ("SPI", "/MainControler/") in by
    assert ("SPI", "/SaftyProcessor/") in by

    # I2C /MainControler/ : U4 hub, single destination U5 -> one bundle
    i2c = k.measure_bus_corridor_areas(kiln_project_path, bus=by[("I2C", "/MainControler/")])
    assert i2c["hub_ic"] == "U4"
    assert i2c["grouped"] is True
    assert len(i2c["bundles"]) == 1

    # SPI /MainControler/ : U4 hub -> U7/U8/U9 multi-drop
    spi = k.measure_bus_corridor_areas(kiln_project_path, bus=by[("SPI", "/MainControler/")])
    assert spi["hub_ic"] == "U4"
    assert {b["destination_ic"] for b in spi["bundles"]} == {"U7", "U8", "U9"}
    assert spi["sum_of_bundle_areas_mm2"] > 0

    # SPI /SaftyProcessor/ : U6 hub with no destination IC -> grouped:false
    safety = k.measure_bus_corridor_areas(kiln_project_path, bus=by[("SPI", "/SaftyProcessor/")])
    assert safety["grouped"] is False


def test_real_board_spi_nets_have_active_deviation_term(kiln_project_path: Path) -> None:
    r = k.get_trace_cost(kiln_project_path, "/MainControler/MOSI")
    assert r["on_bus"] is True
    assert r["cost"]["deviation"] > 0
    assert r["bundle"]["hub_ic"] == "U4"
    # CS3 reaches only the hub (no destination IC) -> not on any bundle
    cs3 = k.get_trace_cost(kiln_project_path, "/MainControler/CS3")
    assert cs3["on_bus"] is False
