"""Tests for Phase 7.5.2 (fill model) + 7.5.3 (islands & attachment-point
costing) - `kicad_router_tool.audit_plane_islands` / MCP tool
`audit_kicad_plane_islands`.

Golden coverage against the real kiln board (fill_source must be "kicad" on
every zone/layer - the six real zones all carry `filled_polygon` data) plus
two synthetic boards built with raw `(zone ...)` s-expr blocks (there is no
zone-authoring helper yet in `synthetic_board.py`, so these compose the
existing header/footprint/net-table helpers with hand-written zone text that
matches exactly what `kicad_router_tool._parse_zones` expects):

  - a mode-0 zone with a mainland component (1 pad attachment) and a second,
    far-away, unattached component -> must cost as an `orphan_island`.
  - a mode-1 (`island_removal_mode 1`) zone with a mainland component and a
    second component that DOES have a pad attachment -> must still report
    `will_be_removed` (not costed as an island), per the edge-case note that
    KiCad deletes islands on refill under mode 1 regardless of attachments.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import kicad_router_tool as router
from tests.synthetic_board import _HEADER_TEMPLATE, _footprint_block, _layer_stack_lines, _net_table

_FOOTER = ")\n"


def _zone_block(
    net: str,
    layer: str,
    uuid: str,
    priority: int,
    island_removal_mode: int,
    outline_pts: list[tuple[float, float]],
    filled_polys: list[list[tuple[float, float]]],
) -> str:
    def _pts_block(pts: list[tuple[float, float]]) -> str:
        return " ".join(f"(xy {x} {y})" for x, y in pts)

    filled = "\n".join(
        f'        (filled_polygon\n'
        f'            (layer "{layer}")\n'
        f'            (pts {_pts_block(pts)})\n'
        f'        )'
        for pts in filled_polys
    )
    return f"""    (zone
        (net "{net}")
        (net_name "{net}")
        (layer "{layer}")
        (uuid "{uuid}")
        (name "")
        (priority {priority})
        (connect_pads
            (clearance 0.2)
        )
        (min_thickness 0.2)
        (fill yes
            (thermal_gap 0.5)
            (thermal_bridge_width 0.5)
            (island_removal_mode {island_removal_mode})
        )
        (polygon
            (pts {_pts_block(outline_pts)})
        )
{filled}
    )
"""


def _write_island_board(path: Path) -> Path:
    """Two F.Cu zones on a 2-layer synthetic board:

    - `GND` (mode 0): mainland square (-5,-5)-(5,5) attached by R1 pad 1
      (net GND); a second, far-away, unattached square (20,20)-(25,25) ->
      must audit as an orphan island (0 attachments).
    - `GND2` (mode 1 / island_removal_mode 1): mainland square (50,-5)-(60,5)
      attached by R2 pad 1; a second square (70,-5)-(75,5) attached by R3 pad
      1 (net GND2) -> has a real attachment, but mode 1 means it must be
      reported `will_be_removed`, never costed as a plain island.
    """
    header = _HEADER_TEMPLATE.format(layer_lines=_layer_stack_lines(2))
    net_names = ["GND", "SIG", "GND2", "SIG2", "SIG3"]
    parts = [header, _net_table(net_names)]

    # R1: pad 1 (GND) at (-0.75, 0) - inside zone1 mainland square.
    parts.append(_footprint_block("R1", "10k", 0.0, 0.0, "synth-fp-r1", "GND", "SIG"))
    # R2: pad 1 (GND2) at (54.25, 0) - inside zone2 mainland square.
    parts.append(_footprint_block("R2", "10k", 55.0, 0.0, "synth-fp-r2", "GND2", "SIG2"))
    # R3: pad 1 (GND2) at (71.25, 0) - inside zone2's second (mode-1) component.
    parts.append(_footprint_block("R3", "10k", 72.0, 0.0, "synth-fp-r3", "GND2", "SIG3"))

    parts.append(_zone_block(
        net="GND", layer="F.Cu", uuid="synth-zone-gnd", priority=0, island_removal_mode=0,
        outline_pts=[(-5, -5), (25, -5), (25, 25), (-5, 25)],
        filled_polys=[
            [(-5, -5), (5, -5), (5, 5), (-5, 5)],
            [(20, 20), (25, 20), (25, 25), (20, 25)],
        ],
    ))
    parts.append(_zone_block(
        net="GND2", layer="F.Cu", uuid="synth-zone-gnd2", priority=0, island_removal_mode=1,
        outline_pts=[(50, -5), (75, -5), (75, 5), (50, 5)],
        filled_polys=[
            [(50, -5), (60, -5), (60, 5), (50, 5)],
            [(70, -5), (75, -5), (75, 5), (70, 5)],
        ],
    ))
    parts.append(_FOOTER)
    path.write_text("".join(parts), encoding="utf-8")
    return path


@pytest.fixture
def island_board_dir(tmp_path: Path) -> Path:
    _write_island_board(tmp_path / "synthboard.kicad_pcb")
    return tmp_path


def _zones_by_net(res: dict) -> dict:
    return {z["net"]: z for z in res["zones"]}


# --------------------------------------------------------------------------- #
# Golden: real kiln board
# --------------------------------------------------------------------------- #

def test_kiln_fill_source_is_kicad_everywhere(kiln_project_path: Path):
    """All six kiln zones carry real `filled_polygon` data, so every
    net-owning zone/layer must report fill_source 'kicad', never 'estimated'."""
    res = router.audit_plane_islands(str(kiln_project_path))
    assert res["zones"], "expected net-owning zones on kiln"
    for zone in res["zones"]:
        for layer_report in zone["layers"]:
            assert layer_report["fill_source"] == "kicad"


def test_kiln_zones_report_components_and_attachments(kiln_project_path: Path):
    res = router.audit_plane_islands(str(kiln_project_path))
    zones = _zones_by_net(res)
    expected_nets = {"GND_Main", "GND_Safty", "12V_Main", "3.3V_Main", "3.3v_Safty"}
    assert expected_nets <= set(zones)

    for net in expected_nets:
        zone = zones[net]
        assert zone["layers"], f"{net} zone reports no copper layers"
        for layer_report in zone["layers"]:
            comps = layer_report["components"]
            assert layer_report["component_count"] == len(comps)
            if not comps:
                continue
            mainlands = [c for c in comps if c["role"] == "mainland"]
            assert len(mainlands) == 1
            # The mainland must hold the most attachments of any component.
            top = max(c["attachment_count"] for c in comps)
            assert mainlands[0]["attachment_count"] == top


def test_kiln_ratsnest_unaffected_by_island_audit(kiln_project_path: Path):
    """Regression guard: the new fill/island model must not perturb the
    long-standing 39-missing-connections ratsnest result."""
    res = router.get_ratsnest(str(kiln_project_path))
    assert res["summary"]["total_connections"] == 39


# --------------------------------------------------------------------------- #
# Synthetic: orphan island + island_removal_mode 1
# --------------------------------------------------------------------------- #

def test_synthetic_orphan_island_costed(island_board_dir: Path):
    res = router.audit_plane_islands(str(island_board_dir))
    zones = _zones_by_net(res)
    gnd = zones["GND"]
    f_cu = next(l for l in gnd["layers"] if l["layer"] == "F.Cu")
    assert f_cu["fill_source"] == "kicad"
    assert f_cu["component_count"] == 2

    comps = {c["role"]: c for c in f_cu["components"]}
    assert "mainland" in comps
    assert comps["mainland"]["attachment_count"] == 1
    assert comps["mainland"]["cost"] == 0.0

    assert "orphan" in comps
    orphan = comps["orphan"]
    assert orphan["attachment_count"] == 0
    assert orphan["cost"] == pytest.approx(1000.0)  # default plane.orphan_island
    # An orphan still gets a stitching-via suggestion pointing at the mainland.
    assert orphan["suggested_stitching_via"] is not None
    assert orphan["suggested_stitching_via"]["projected_attachment_count"] == 1


def test_synthetic_island_removal_mode_1_reports_will_be_removed(island_board_dir: Path):
    res = router.audit_plane_islands(str(island_board_dir))
    zones = _zones_by_net(res)
    gnd2 = zones["GND2"]
    assert gnd2["island_removal_mode"] == 1
    f_cu = next(l for l in gnd2["layers"] if l["layer"] == "F.Cu")
    assert f_cu["component_count"] == 2

    roles = [c["role"] for c in f_cu["components"]]
    assert "mainland" in roles
    assert "will_be_removed" in roles
    # Never "island" or "orphan" under mode 1, even though this component DOES
    # have a real pad attachment (R3 pad 1).
    assert "island" not in roles
    assert "orphan" not in roles

    removed = next(c for c in f_cu["components"] if c["role"] == "will_be_removed")
    assert removed["attachment_count"] == 1  # R3 pad 1 lands inside it
    assert removed["cost"] is None
    assert "suggested_stitching_via" not in removed


def test_synthetic_island_cost_matches_plane_settings_formula(island_board_dir: Path):
    res = router.audit_plane_islands(str(island_board_dir))
    plane = res["plane_settings"]
    zones = _zones_by_net(res)
    orphan = next(
        c for c in next(l for l in zones["GND"]["layers"] if l["layer"] == "F.Cu")["components"]
        if c["role"] == "orphan"
    )
    assert orphan["cost"] == pytest.approx(plane["orphan_island"])
