"""Tests for the Phase 7.5.1 zone parser + `list_kicad_zones`
(`kicad_router_tool`).

Golden tests against the real kiln board: it carries the six known board-level
zones (mainGnd multi-layer, safty_gnd, main12v, main3.3, 3.3v_safty, antenna)
plus a regression guard that the zone-model port of `get_ratsnest` still
yields the known 39 missing connections.
"""

from __future__ import annotations

from pathlib import Path

import kicad_router_tool as router


def _zones_by_name(res: dict) -> dict:
    return {z["name"]: z for z in res["zones"]}


def test_kiln_finds_six_known_zones(kiln_project_path: Path):
    res = router.list_zones(str(kiln_project_path))
    assert res["zone_count"] == 6
    names = {z["name"] for z in res["zones"]}
    assert names == {
        "mainGnd", "safty_gnd", "main12v", "main3.3", "3.3v_safty", "antenna",
    }


def test_kiln_main_gnd_is_multi_layer_with_net_and_priority(kiln_project_path: Path):
    res = router.list_zones(str(kiln_project_path))
    zones = _zones_by_name(res)

    main_gnd = zones["mainGnd"]
    assert main_gnd["net"] == "GND_Main"
    assert set(main_gnd["layers"]) == {"F.Cu", "B.Cu", "In1.Cu"}
    assert main_gnd["priority"] == 0
    assert len(main_gnd["polygon"]) >= 3
    assert len(main_gnd["filled_polygon"]) > 0

    safty_gnd = zones["safty_gnd"]
    assert safty_gnd["net"] == "GND_Safty"
    assert set(safty_gnd["layers"]) == {"F.Cu", "B.Cu", "In1.Cu"}
    assert safty_gnd["priority"] == 1


def test_kiln_single_layer_power_zones(kiln_project_path: Path):
    res = router.list_zones(str(kiln_project_path))
    zones = _zones_by_name(res)

    main12v = zones["main12v"]
    assert main12v["net"] == "12V_Main"
    assert main12v["layers"] == ["In2.Cu"]
    assert main12v["priority"] == 4

    main33 = zones["main3.3"]
    assert main33["net"] == "3.3V_Main"
    assert main33["layers"] == ["In2.Cu"]
    assert main33["priority"] == 3

    safty33 = zones["3.3v_safty"]
    assert safty33["net"] == "3.3v_Safty"
    assert safty33["layers"] == ["In2.Cu"]
    assert safty33["priority"] == 2


def test_kiln_antenna_zone_is_keepout_multi_layer_no_net(kiln_project_path: Path):
    res = router.list_zones(str(kiln_project_path))
    antenna = _zones_by_name(res)["antenna"]
    assert antenna["net"] == ""
    assert set(antenna["layers"]) == {"F.Cu", "B.Cu", "In1.Cu", "In2.Cu"}
    assert antenna["keepout"] is not None
    # A keepout zone is never filled - no fabricated filled_polygon blocks.
    assert antenna["filled_polygon"] == []


def test_kiln_zones_read_island_removal_mode(kiln_project_path: Path):
    """kiln zones all use fill.island_removal_mode 0 (islands allowed) - the
    edge-case note that matters for later cost-model phases; this stage only
    has to actually read it, not act on it."""
    res = router.list_zones(str(kiln_project_path))
    for z in res["zones"]:
        assert z["island_removal_mode"] == 0
        assert z["fill"].get("island_removal_mode") == 0


def test_kiln_footprint_nested_pad_keepouts_excluded(kiln_project_path: Path):
    """The RaspberryPi Pico footprint library nests several per-pad keepout
    `(zone ...)` blocks; these are not board-level planes and must not appear
    in `list_kicad_zones` (that's why zone_count == 6, not 6 + N)."""
    res = router.list_zones(str(kiln_project_path))
    names = {z["name"] for z in res["zones"]}
    assert not any("Pad Keep Out" in n for n in names)


def test_kiln_ratsnest_still_39_missing_connections_after_zone_port(kiln_project_path: Path):
    """Regression guard: porting `get_ratsnest`/`build_connectivity`'s zone-fill
    connectivity onto the real `_parse_zones` model must not change kiln's
    verified pre-existing result of 39 missing connections."""
    res = router.get_ratsnest(str(kiln_project_path))
    assert res["summary"]["total_connections"] == 39
