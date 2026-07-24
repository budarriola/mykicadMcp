"""Phase 7.17 - `route_board`, the one command to route the board (CLI + MCP).

These tests exercise the thin orchestrator directly and through its CLI skin,
against a SCRATCH copy of the real kiln board (never the real files). The
routable target is `/SaftyProcessor/Current3` (C52.1->R89.1), the same
pour-free connection the 7.3b detailed-router tests use - it routes without a
plane via-drop, which the minimal router does not yet do.
"""

from __future__ import annotations

import kicad_router_tool as r

ROUTABLE_NET = "/SaftyProcessor/Current3"


def _missing_count(project_path) -> int:
    return r.get_ratsnest(project_path)["summary"]["total_connections"]


def test_route_board_dry_run_preview_does_not_touch_board(scratch_board):
    board = scratch_board / "kiln.kicad_pcb"
    before_bytes = board.read_bytes()

    rep = r.route_board(scratch_board, nets=[ROUTABLE_NET], write=False, effort="balanced")

    # report shape
    assert rep["command"] == "route_board"
    assert rep["write"] is False and rep["written"] is False
    assert rep["unrouted_before"] == 1
    assert rep["routed"] == 1 and rep["failed"] == 0
    assert rep["total_routed_length_mm"] > 0
    conn = rep["connections"][0]
    assert conn["net"] == ROUTABLE_NET and conn["routed"] is True

    # dry-run must not modify the board file at all.
    assert board.read_bytes() == before_bytes


def test_route_board_write_routes_and_is_reversible(scratch_board):
    board = scratch_board / "kiln.kicad_pcb"
    before_missing = _missing_count(scratch_board)
    before_bytes = board.read_bytes()

    rep = r.route_board(scratch_board, nets=[ROUTABLE_NET], write=True, effort="balanced")
    assert rep["written"] is True
    assert rep["routed"] == 1

    # connectivity improved: at least one fewer missing connection, and the
    # board file actually changed.
    after_missing = _missing_count(scratch_board)
    assert after_missing == before_missing - 1
    assert board.read_bytes() != before_bytes

    # reversible: unroute restores connectivity and the board.
    r.unroute_nets(scratch_board, nets=[ROUTABLE_NET], write=True)
    assert _missing_count(scratch_board) == before_missing


def test_route_board_cli_dry_run_smoke(scratch_board, capsys):
    # In-process argv (no shell => no MSYS leading-slash path mangling).
    rc = r.main(["route", str(scratch_board), "--nets", ROUTABLE_NET, "--effort", "quick"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "route_board" in out
    assert "dry-run" in out           # no --write => preview only
    # the CLI must not have written anything.
    assert "written=False" in out


def test_route_board_invalid_effort_raises(scratch_board):
    import pytest
    with pytest.raises(ValueError):
        r.route_board(scratch_board, nets=[ROUTABLE_NET], effort="ludicrous")


def test_route_board_pipeline_hooks_declared_not_faked(scratch_board):
    # Guard: planes/optimizer/stitching must be honestly reported as not wired,
    # so a future regression that silently "enables" them without implementing
    # them is caught.
    rep = r.route_board(scratch_board, nets=[ROUTABLE_NET], write=False)
    pipe = rep["pipeline"]
    assert pipe["ratsnest"] == "done"
    assert pipe["global_route"] == "done"
    assert pipe["detailed_route"] == "done"
    for hook in ("plane_aware_routing", "whole_board_optimization", "stitching"):
        assert pipe[hook].startswith("not_implemented")


def test_route_board_effort_quick_disables_ripup(scratch_board):
    rep = r.route_board(scratch_board, nets=[ROUTABLE_NET], write=False, effort="quick")
    assert rep["pipeline"]["rip_up"].startswith("disabled")
    assert rep["ripup"]["max_ripup_iterations"] == 0
