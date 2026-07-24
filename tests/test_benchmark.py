"""Phase 7.16 benchmark harness - `benchmark_autoroute` / MCP tool
`benchmark_kicad_autoroute` (83 tools).

Every test here works on SCRATCH copies only: `benchmark_autoroute` itself
copies `source_board` into a fresh scratch directory before it measures or
routes anything, so `source_board` (a synthetic project under `tmp_path`, or
the real kiln board for the opt-in slow test) is never written. That guard is
asserted directly, not just assumed.

Fast synthetic fixtures only in the always-run tests: `route_board`'s pure
detailed A* is slow on anything kiln-sized, so both `complete_only` and
`strip_and_reroute` here run against a tiny synthetic multi-drop SPI project
(`synthetic_board.write_multidrop_spi_project`, 2 components, 4 nets) -
seconds, not minutes. The real-kiln measurement is a separate
`@pytest.mark.slow` test gated behind an env var (see its docstring for why
`@pytest.mark.slow` alone is not enough here).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import kicad_pcb_tool as pcb
import kicad_router_tool as router

from synthetic_board import write_multidrop_spi_project


def _strip_one_net(board: Path, net: str) -> int:
    """Delete the copper for a single net (simulates 'the human left this one
    unrouted'), using the same uuid-block-deletion surgery `unroute_nets`
    uses. Returns the number of blocks removed."""
    tracks = pcb._parse_tracks(board)
    uuids = {
        t["uuid"]
        for t in tracks["segments"] + tracks["vias"] + tracks["arcs"]
        if t["net"] == net and t.get("uuid")
    }
    text = pcb._read_text(board)
    text, removed = router._delete_blocks_by_uuid(text, uuids)
    with board.open("w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    pcb._invalidate_board_cache(board)
    return removed


@pytest.fixture
def spi_project(tmp_path: Path) -> Path:
    """A small, fully-routed multi-drop SPI project (hub U1 + 1 destination,
    4 nets: SCK/MOSI/MISO shared + one dedicated CS0) in its own directory."""
    write_multidrop_spi_project(tmp_path, destinations=1, route=True)
    return tmp_path


# --------------------------------------------------------------------------- #
# complete_only
# --------------------------------------------------------------------------- #


def test_complete_only_reports_human_and_post_scores(spi_project: Path) -> None:
    board = spi_project / "spibus.kicad_pcb"
    # Leave one net unrouted, like the real kiln's 39 human-unrouted connections.
    removed = _strip_one_net(board, "/SPI/CS0")
    assert removed >= 1

    before_bytes = board.read_bytes()

    result = router.benchmark_autoroute(spi_project, mode="complete_only")

    assert result["command"] == "benchmark_autoroute"
    assert result["mode"] == "complete_only"
    # source board is untouched: the scratch copy is a DIFFERENT path.
    assert result["scratch_board"] != result["source_board"]
    assert board.read_bytes() == before_bytes

    human = result["human"]
    assert human["unrouted_connections_before"] == 1
    assert "total" in human["score"]

    auto = result["auto"]
    assert auto["routed"] == 1
    assert auto["failed"] == 0
    assert auto["completion_pct"] == 100.0
    assert auto["total_routed_length_mm"] > 0
    assert "total" in auto["score"]

    comparison = result["comparison"]
    assert comparison["human_score_total"] == human["score"]["total"]
    assert comparison["post_score_total"] == auto["score"]["total"]
    assert isinstance(comparison["matched_or_beat_human"], bool)
    assert "verdict" in comparison

    drc = result["drc"]
    assert set(drc) == {"baseline", "post", "new_violation_count", "new_violations"}


def test_complete_only_source_board_never_written(spi_project: Path) -> None:
    board = spi_project / "spibus.kicad_pcb"
    _strip_one_net(board, "/SPI/CS0")
    before_bytes = board.read_bytes()
    before_mtime = board.stat().st_mtime

    router.benchmark_autoroute(spi_project, mode="complete_only", effort="quick")

    assert board.read_bytes() == before_bytes
    assert board.stat().st_mtime == before_mtime


def test_complete_only_invalid_mode_raises(spi_project: Path) -> None:
    with pytest.raises(ValueError):
        router.benchmark_autoroute(spi_project, mode="not_a_mode")


# --------------------------------------------------------------------------- #
# strip_and_reroute
# --------------------------------------------------------------------------- #


def test_strip_and_reroute_returns_hand_vs_auto_comparison(spi_project: Path) -> None:
    board = spi_project / "spibus.kicad_pcb"
    before_bytes = board.read_bytes()

    result = router.benchmark_autoroute(spi_project, mode="strip_and_reroute")

    assert result["mode"] == "strip_and_reroute"
    assert board.read_bytes() == before_bytes  # source untouched

    strip = result["strip"]
    assert strip["removed"] > 0  # all pre-existing copper was stripped

    human = result["human"]
    assert human["score"]["total"] > 0  # the ORIGINAL human board had real copper
    assert human["layer_lengths_mm"]

    auto = result["auto"]
    assert auto["total_connections_needed"] > 0
    assert auto["routed"] + auto["failed"] == auto["total_connections_needed"]
    assert auto["score"]["total"] >= 0

    comparison = result["comparison"]
    assert comparison["human_score_total"] == human["score"]["total"]
    assert comparison["post_score_total"] == auto["score"]["total"]
    assert comparison["delta_total"] == round(
        comparison["post_score_total"] - comparison["human_score_total"], 3
    )


def test_strip_and_reroute_actually_deletes_copper_before_routing(spi_project: Path) -> None:
    board = spi_project / "spibus.kicad_pcb"
    original_length = pcb.get_trace_cost(spi_project)["board_totals"]["length"]
    assert original_length > 0

    result = router.benchmark_autoroute(spi_project, mode="strip_and_reroute")

    # the scratch copy was fully stripped then rerouted from zero - its
    # emitted length need not match the human original at all (different
    # geometry), but the strip step must report removing every original
    # segment/via/arc.
    assert result["strip"]["candidates"] == result["strip"]["removed"]


# --------------------------------------------------------------------------- #
# MCP registration
# --------------------------------------------------------------------------- #


def test_benchmark_tool_registered() -> None:
    import kicad_mcp_server as server

    srv = server.KiCadMcpServer()
    assert "benchmark_kicad_autoroute" in srv.tools
    schema = srv.tools["benchmark_kicad_autoroute"]["inputSchema"]
    assert "source_board" in schema["properties"]
    assert schema["properties"]["mode"]["enum"] == ["complete_only", "strip_and_reroute"]


# --------------------------------------------------------------------------- #
# Real kiln baseline (opt-in only - see docstring)
# --------------------------------------------------------------------------- #


@pytest.mark.slow
def test_kiln_complete_only_baseline(kiln_project_path: Path) -> None:
    """Runs `complete_only` on the REAL kiln board (via a scratch copy -
    kiln.kicad_pcb itself is still never written) and records the numbers
    against the documented hand-routed baseline (NETCLASS_PLAN.md's "Real-board
    routing findings" section: board total 8552.28, 39 connections unrouted by
    hand).

    IMPORTANT: `route_board` doing detailed A* over kiln's 39 unrouted
    connections took minutes in the first real run (documented in the plan:
    3/39 routed, 35 `window_too_large` failures, each retried via window-
    doubling before giving up). `@pytest.mark.slow` alone does NOT deselect
    this test in the default suite (`pytest.ini`'s `addopts` has no `-m`
    filter), so it is ALSO gated behind an explicit env var
    (`KICAD_BENCHMARK_REAL=1`) and skipped otherwise - the default `pytest -q`
    run stays fast. Run explicitly with:
        KICAD_BENCHMARK_REAL=1 pytest -m slow tests/test_benchmark.py -q
    """
    if os.environ.get("KICAD_BENCHMARK_REAL") != "1":
        pytest.skip(
            "Real-kiln benchmark opt-in only (minutes-scale route_board run); "
            "set KICAD_BENCHMARK_REAL=1 to run it."
        )

    board = kiln_project_path / "kiln.kicad_pcb"
    before_bytes = board.read_bytes()

    result = router.benchmark_autoroute(kiln_project_path, mode="complete_only", effort="quick")

    # the real board must never be written, no matter how long routing took.
    assert board.read_bytes() == before_bytes

    human_total = result["human"]["score"]["total"]
    print(
        f"\n[kiln benchmark] human_total={human_total} "
        f"unrouted_before={result['human']['unrouted_connections_before']} "
        f"post_total={result['auto']['score']['total']} "
        f"completion_pct={result['auto']['completion_pct']} "
        f"routed={result['auto']['routed']} failed={result['auto']['failed']} "
        f"runtime_s={result['runtime_seconds']} "
        f"verdict={result['comparison']['verdict']}"
    )
    assert result["human"]["unrouted_connections_before"] > 0
