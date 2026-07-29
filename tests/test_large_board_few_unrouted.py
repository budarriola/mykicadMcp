"""A LARGE synthetic board (kiln-scale component count) with only a HANDFUL
of unrouted connections, added to the test set per user request (2026-07-29).

The existing `test_bigboard_scale.py` suite only exercises geometric/memory
primitives (window shape, GPU batch planning) in isolation - none of it
builds an actual `.kicad_pcb` + project and runs it through the real
`get_ratsnest` / `route_board` pipeline at scale. This file closes that gap
with a real (synthetic) large board via `generate_large_board_few_unrouted`
(`write_synthetic_project(mode="ladder_partial")`): a resistor-ladder series
chain of many components (kiln-scale count), pre-routed except a small,
known number of links - the same "large but mostly done" shape most real
sessions actually work against, rather than an all-unrouted stress board
(which `test_bigboard_scale.py` and `generate_fanout_field_board` already
cover). See that generator's docstring for why a dense many-pad-per-component
topology was tried first and rejected (it packs parallel already-routed
copper right next to the deliberately-unrouted links, turning "a handful of
easy connections" into real dense-board congestion).
"""

from __future__ import annotations

from pathlib import Path

import kicad_pcb_tool as k
import kicad_router_tool as r

from synthetic_board import write_synthetic_project

# 300 components matches kiln's real ~259-component scale (see
# generate_synthetic_board's docstring for the same reference point).
_COMPONENT_COUNT = 300
_UNROUTED_COUNT = 4


def _make_large_board(tmp_path: Path) -> Path:
    paths = write_synthetic_project(
        tmp_path, project_name="bigfew", mode="ladder_partial",
        component_count=_COMPONENT_COUNT, unrouted_count=_UNROUTED_COUNT,
    )
    return paths["project"].parent


def test_large_board_has_expected_component_and_connection_counts(tmp_path: Path) -> None:
    project_dir = _make_large_board(tmp_path)
    components = k._parse_board_components(project_dir / "bigfew.kicad_pcb")
    assert len(components) == _COMPONENT_COUNT

    rats = r.get_ratsnest(project_dir)
    # One deliberate gap per still-unrouted chain link = exactly one 2-pad
    # MST connection each - a true handful, independent of component_count.
    assert rats["summary"]["total_connections"] == _UNROUTED_COUNT

    unrouted_nets = {c["net"] for c in rats["connections"]}
    n_links = _COMPONENT_COUNT - 1
    first_unrouted_link = n_links - _UNROUTED_COUNT + 1
    expected_nets = {f"CHAIN_{i}" for i in range(first_unrouted_link, n_links + 1)}
    assert unrouted_nets == expected_nets

    # An early link is already fully routed (single island, no missing
    # connection) - confirms the "mostly done" shape, not just the count.
    assert "CHAIN_1" not in unrouted_nets


def test_large_board_route_board_completes_and_routes_the_handful(tmp_path: Path) -> None:
    project_dir = _make_large_board(tmp_path)
    rep = r.route_board(project_dir, write=True, effort="quick")
    assert rep["unrouted_before"] == _UNROUTED_COUNT
    assert rep["routed"] == _UNROUTED_COUNT
    assert rep["failed"] == 0

    # written to the actual board file, and nothing left unrouted after.
    rats_after = r.get_ratsnest(project_dir)
    assert rats_after["summary"]["total_connections"] == 0
