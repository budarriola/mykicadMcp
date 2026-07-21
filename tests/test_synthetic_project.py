"""Acceptance tests for `write_synthetic_project` (M0 companion-file
generation): a synthetic board plus a matching `.kicad_pro` and `.net` file,
so netlist-based tools that call `_resolve_project_path` on a bare directory
work against a synthetic-only project - not just the real kiln project.
"""

from __future__ import annotations

import json
from pathlib import Path

import kicad_pcb_tool as k

from synthetic_board import write_synthetic_project


def test_write_synthetic_project_creates_all_three_files(tmp_path: Path) -> None:
    paths = write_synthetic_project(tmp_path, project_name="synth", component_count=5)
    assert paths["board"].exists()
    assert paths["project"].exists()
    assert paths["netlist"].exists()
    assert paths["board"].name == "synth.kicad_pcb"
    assert paths["project"].name == "synth.kicad_pro"
    assert paths["netlist"].name == "synth.net"


def test_synthetic_kicad_pro_has_default_netclass_shaped_like_kiln(tmp_path: Path) -> None:
    paths = write_synthetic_project(tmp_path, project_name="synth", component_count=3)
    data = json.loads(paths["project"].read_text(encoding="utf-8"))
    classes = data["net_settings"]["classes"]
    assert len(classes) == 1
    default = classes[0]
    assert default["name"] == "Default"
    # Same keys/shape `get_project_track_inventory` and Phase 4 expect to
    # find on kiln.kicad_pro's own Default class.
    for key in ("track_width", "via_diameter", "via_drill", "clearance"):
        assert key in default
        assert isinstance(default[key], (int, float))


def test_resolve_project_path_finds_synthetic_project_directory(tmp_path: Path) -> None:
    write_synthetic_project(tmp_path, project_name="synth", component_count=4)
    board_path, project_file, netlist_path = k._resolve_project_path(tmp_path)
    assert board_path.name == "synth.kicad_pcb"
    assert project_file.name == "synth.kicad_pro"
    assert netlist_path.name == "synth.net"
    assert netlist_path.exists()


def test_list_nets_runs_against_synthetic_project(tmp_path: Path) -> None:
    write_synthetic_project(tmp_path, project_name="synth", component_count=4)
    nets = k.list_nets(tmp_path)
    # 4 components x 2 nets each (A/B), all isolated single-node nets.
    assert len(nets) == 8
    names = {n["name"] for n in nets}
    assert names == {f"NET_{i}_{side}" for i in range(1, 5) for side in ("A", "B")}


def test_get_component_connections_runs_against_synthetic_project(tmp_path: Path) -> None:
    write_synthetic_project(tmp_path, project_name="synth", component_count=3)
    result = k.get_component_connections(tmp_path, "R1")
    assert result["component"]["reference"] == "R1"
    assert len(result["connections"]) == 2  # NET_1_A, NET_1_B


def test_detect_buses_runs_against_synthetic_project_without_error(tmp_path: Path) -> None:
    write_synthetic_project(tmp_path, project_name="synth", component_count=6)
    result = k.detect_buses(tmp_path)
    assert "candidates" in result
    assert "stale_netlist_warnings" in result


def test_fanout_mode_netlist_reports_shared_nets_across_components(tmp_path: Path) -> None:
    write_synthetic_project(
        tmp_path,
        project_name="synthfan",
        mode="fanout",
        component_count=3,
        pads_per_component=16,
    )
    nets = k.list_nets(tmp_path)
    assert len(nets) == 16
    for net in nets:
        assert len(net["nodes"]) == 3
        assert {node["ref"] for node in net["nodes"]} == {"U1", "U2", "U3"}


def test_fanout_mode_get_component_connections_sees_all_peers(tmp_path: Path) -> None:
    write_synthetic_project(
        tmp_path,
        project_name="synthfan",
        mode="fanout",
        component_count=4,
        pads_per_component=16,
    )
    result = k.get_component_connections(tmp_path, "U1")
    assert result["connected_references"] == ["U2", "U3", "U4"]


def test_invalid_mode_raises(tmp_path: Path) -> None:
    import pytest

    with pytest.raises(ValueError):
        write_synthetic_project(tmp_path, mode="bogus")
