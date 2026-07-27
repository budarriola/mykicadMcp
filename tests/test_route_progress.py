"""Phase 7.9 - live route-progress viewer.

Covers the pieces the spec calls out as testable WITHOUT a real display:
  1. JSONL event emission shape/ordering from a real `route_nets` call
     against a scratch copy of the kiln board (header -> connection(s) ->
     run_complete), plus the `pcb_settings.json` gate that turns it off.
  2. The cancel-flag path actually stopping `route_nets` mid-run (between
     connections, never mid-search) and the stale-flag reset at call start.
  3. `kicad_route_viewer`'s pure data layer: JSONL parsing/replay
     (`iter_events`/`replay_state`/`ProgressState`), `_rgb_string_to_hex`,
     and `_load_kicad_layer_colors`'s theme-file + embedded-fallback paths -
     none of this touches `tkinter`.
  4. `open_kicad_route_viewer` / `open_route_viewer`'s graceful-failure
     message when tkinter/DISPLAY is unavailable, and its MCP registration.

No test here ever needs a real Tk mainloop - `RouteViewerApp` itself is
exercised only "by inspection" per the module's own testing note.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import kicad_pcb_tool as pcb
import kicad_router_tool as router
import kicad_route_viewer as viewer

# Same strategy as test_detailed_route.py's `_pick_connection` (a hardcoded
# net-name shortlist), generalized to search the live ratsnest instead: the
# kiln board's exact set of already-routed vs. still-unrouted connections
# drifts as the real board is edited (test_route_board.py's ROUTABLE_NET-based
# tests currently fail for exactly this reason - pre-existing board drift,
# not a router bug), so pick whichever of the first few unrouted connections
# genuinely previews clean right now, rather than trusting a fixed net name.
_MAX_CANDIDATES_TRIED = 12


def _pick_connection(project_path) -> dict:
    rats = router.get_ratsnest(project_path)
    for conn in rats["connections"][:_MAX_CANDIDATES_TRIED]:
        res = router.route_nets(project_path, connections=[conn], write=False)
        rec = res["connections"][0]
        if rec["routed"] and rec["self_check"]["passed"]:
            return conn
    pytest.skip("No candidate kiln connection routed cleanly (board changed?).")


def _progress_file(scratch_board: Path) -> Path:
    return scratch_board / "kiln.route_progress.jsonl"


def _read_events(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# --------------------------------------------------------------------------- #
# 1. JSONL emission shape/ordering
# --------------------------------------------------------------------------- #

def test_progress_jsonl_header_then_connection_then_complete(scratch_board):
    conn = _pick_connection(scratch_board)
    rep = router.route_nets(scratch_board, connections=[conn], write=False)

    assert "progress_path" in rep
    prog_path = Path(rep["progress_path"])
    assert prog_path == _progress_file(scratch_board)
    events = _read_events(prog_path)
    assert events, "expected at least a header + run_complete event"

    assert events[0]["event"] == "header"
    header = events[0]
    assert "session" in header and "geometry" in header and "colors" in header
    assert header["session"]["total_connections"] == 1
    assert set(header["geometry"].keys()) == {"segments", "vias", "zones"}
    # decision-protocol hook is explicitly present but empty - no 7.6/7.7 yet.
    assert header["decision_protocol"] is None

    assert events[-1]["event"] == "run_complete"
    assert events[-1]["total_connections"] == 1

    connection_events = [e for e in events if e["event"] == "connection"]
    assert len(connection_events) == 1
    ce = connection_events[0]
    assert ce["net"] == conn["net"]
    assert ce["routed"] is True
    assert ce["connection_index"] == 1 and ce["total_connections"] == 1
    assert "changed" in ce and "added" in ce["changed"] and "removed" in ce["changed"]
    # Usually non-empty; can legitimately be empty for a connection that
    # routes entirely across its own net's copper-fill plane (7.5.4 - no
    # copper is emitted for plane traversal, only a via/stub when one is
    # needed at all), so this only asserts the SHAPE of each item when any
    # are present, not that the list is non-empty.
    for item in ce["changed"]["added"]:
        assert item["kind"] in ("segment", "via")
        assert item["uuid"]


def test_progress_file_reset_each_call_not_accumulated(scratch_board):
    conn = _pick_connection(scratch_board)
    router.route_nets(scratch_board, connections=[conn], write=False)
    first_events = _read_events(_progress_file(scratch_board))

    router.route_nets(scratch_board, connections=[conn], write=False)
    second_events = _read_events(_progress_file(scratch_board))

    # Same shape both times (truncated/reset at the START of each call) -
    # not doubled.
    assert len(second_events) == len(first_events)


def test_progress_disabled_via_pcb_settings(scratch_board):
    conn = _pick_connection(scratch_board)
    # `_pick_connection` itself ran a progress-enabled preview call - remove
    # that leftover file so this test only judges what THIS (disabled) call
    # does with it.
    _progress_file(scratch_board).unlink(missing_ok=True)
    settings_path = scratch_board / "pcb_settings.json"
    settings_path.write_text(
        json.dumps({"autorouter": {"progress": {"events": False}}}), encoding="utf-8"
    )
    rep = router.route_nets(scratch_board, connections=[conn], write=False)
    assert "progress_path" not in rep
    assert not _progress_file(scratch_board).exists()


# --------------------------------------------------------------------------- #
# 2. Cancel-flag path
# --------------------------------------------------------------------------- #

def test_cancel_flag_stops_route_nets_before_any_connection(scratch_board, monkeypatch):
    conn = _pick_connection(scratch_board)
    # Force this connection through the SERIAL worklist (`while pending:`) -
    # the loop the cancel flag is actually wired into - rather than letting
    # the 7.8b speculative parallel pre-pass commit it first (which it often
    # does for a single well-isolated connection, never touching `pending`
    # at all). Speculation is an execution-order optimization, not part of
    # what the spec asks the cancel flag to gate, so this is the honest way
    # to exercise the real check deterministically.
    monkeypatch.setattr(router, "_run_independent_routes", lambda ctx, items, workers: {})
    # Simulate the viewer's "Stop after this iteration" button having already
    # been clicked once this run is underway: the per-connection loop's check
    # (never mid-search) must see it true on its very first poll and stop
    # cleanly, leaving the one candidate connection reported as not attempted.
    monkeypatch.setattr(router, "_route_cancel_requested", lambda project_path: True)

    rep = router.route_nets(scratch_board, connections=[conn], write=False)

    assert rep["cancelled"] is True
    assert rep["summary"]["connections_routed"] == 0
    assert len(rep["connections"]) == 1
    assert rep["connections"][0]["cancelled"] is True
    assert rep["connections"][0]["failure"]["reason"] == "cancelled_before_attempt"

    events = _read_events(_progress_file(scratch_board))
    assert any(e["event"] == "cancelled" for e in events)
    assert events[-1]["event"] == "run_complete"
    assert events[-1]["cancelled"] is True


def test_stale_cancel_flag_reset_at_call_start(scratch_board):
    conn = _pick_connection(scratch_board)
    # Write a stale flag as if a PREVIOUS run's cancel request never got
    # cleaned up - the NEXT call must not inherit it.
    state = pcb.load_board_local(scratch_board)
    data = state["data"]
    data["route_cancel_requested"] = True
    pcb.save_board_local(scratch_board, data)

    rep = router.route_nets(scratch_board, connections=[conn], write=False)

    assert rep["cancelled"] is False
    assert rep["summary"]["connections_routed"] == 1
    # the flag itself is cleared, not left dangling for a third call.
    assert pcb.load_board_local(scratch_board)["data"].get("route_cancel_requested") is False


def test_request_cancel_writes_board_local_flag(tmp_path):
    project_dir = tmp_path
    (project_dir / "board.kicad_pcb").write_text(
        '(kicad_pcb (version 20221018) (generator test))', encoding="utf-8"
    )
    path = viewer.request_cancel(project_dir / "board.kicad_pcb")
    assert Path(path).exists()
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    assert data["route_cancel_requested"] is True


# --------------------------------------------------------------------------- #
# 3. Pure data layer: JSONL replay + layer-color resolution
# --------------------------------------------------------------------------- #

def test_rgb_string_to_hex_parses_and_drops_alpha():
    assert viewer._rgb_string_to_hex("rgb(200, 52, 52)") == "#c83434"
    assert viewer._rgb_string_to_hex("rgba(0, 255, 0, 0.5)") == "#00ff00"
    assert viewer._rgb_string_to_hex("#abcdef") == "#abcdef"
    assert viewer._rgb_string_to_hex("not-a-color") is None
    assert viewer._rgb_string_to_hex(None) is None


def test_load_kicad_layer_colors_falls_back_when_no_kicad_config(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))  # empty dir - no kicad/ subfolder at all
    colors = viewer._load_kicad_layer_colors("auto")
    assert colors == viewer.DEFAULT_LAYER_COLORS


def test_load_kicad_layer_colors_resolves_theme_file(tmp_path, monkeypatch):
    version_dir = tmp_path / "kicad" / "9.0"
    (version_dir / "colors").mkdir(parents=True)
    (version_dir / "pcbnew.json").write_text(
        json.dumps({"appearance": {"color_theme": "my_theme"}}), encoding="utf-8"
    )
    (version_dir / "colors" / "my_theme.json").write_text(
        json.dumps({
            "meta": {"name": "my_theme"},
            "board": {
                "copper": {"f": "rgb(10, 20, 30)", "b": "rgb(1, 2, 3)"},
                "via_through": "rgb(255, 0, 0)",
                "background": "rgb(0, 0, 0)",
            },
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("APPDATA", str(tmp_path))

    colors = viewer._load_kicad_layer_colors("auto")
    assert colors["F.Cu"] == "#0a141e"
    assert colors["B.Cu"] == "#010203"
    assert colors["via_through"] == "#ff0000"
    # a layer the theme doesn't name falls back to the embedded default.
    assert colors["In1.Cu"] == viewer.DEFAULT_LAYER_COLORS["In1.Cu"]


def test_load_kicad_layer_colors_picks_newest_version_dir(tmp_path, monkeypatch):
    for ver in ("7.0", "8.0", "9.0"):
        (tmp_path / "kicad" / ver).mkdir(parents=True)
    (tmp_path / "kicad" / "9.0" / "pcbnew.json").write_text(
        json.dumps({"appearance": {"color_theme": "_builtin_default"}}), encoding="utf-8"
    )
    monkeypatch.setenv("APPDATA", str(tmp_path))
    # _builtin_default has no theme file on disk -> falls back cleanly, no crash.
    colors = viewer._load_kicad_layer_colors("auto")
    assert colors == viewer.DEFAULT_LAYER_COLORS


def test_iter_events_skips_malformed_and_blank_lines(tmp_path):
    p = tmp_path / "events.jsonl"
    p.write_text('{"event": "header"}\n\nnot json\n{"event": "run_complete"}\n', encoding="utf-8")
    events = viewer.iter_events(p)
    assert [e["event"] for e in events] == ["header", "run_complete"]


def test_iter_events_missing_file_returns_empty(tmp_path):
    assert viewer.iter_events(tmp_path / "nope.jsonl") == []


def test_replay_state_adds_and_removes_by_uuid():
    events = [
        {
            "event": "header", "session": {"total_connections": 2},
            "colors": {"F.Cu": "#111111"},
            "geometry": {
                "segments": [{"uuid": "existing-seg", "net": "GND", "layer": "F.Cu",
                              "width": 0.25, "start": {"x": 0, "y": 0}, "end": {"x": 1, "y": 1}}],
                "vias": [], "zones": [],
            },
            "decision_protocol": None,
        },
        {
            "event": "connection", "connection_index": 1, "total_connections": 2,
            "iteration": 0, "net": "NET1", "routed": True, "score": 1.5,
            "changed": {"added": [
                {"kind": "segment", "uuid": "0:seg:0", "net": "NET1", "layer": "F.Cu",
                 "width": 0.25, "start": {"x": 0, "y": 0}, "end": {"x": 2, "y": 2}},
            ], "removed": []},
        },
        {
            "event": "connection", "connection_index": 2, "total_connections": 2,
            "iteration": 1, "net": "NET2", "routed": True, "score": 3.0,
            "changed": {"added": [
                {"kind": "via", "uuid": "1:via:0", "net": "NET2", "size": 0.6, "drill": 0.3,
                 "at": {"x": 5, "y": 5}},
            ], "removed": ["0:seg:0"]},
        },
        {"event": "run_complete", "cancelled": False},
    ]
    state = viewer.replay_state(events)

    assert "existing-seg" in state.segments        # initial geometry snapshot survives
    assert "0:seg:0" not in state.segments          # added then ripped away
    assert "1:via:0" in state.vias
    assert state.connections_done == 2
    assert state.total_connections == 2
    assert state.score_history == [1.5, 3.0]
    assert state.done is True
    assert state.cancelled is False
    assert state.colors["F.Cu"] == "#111111"

    summary = state.as_summary()
    assert summary["segment_count"] == 1
    assert summary["via_count"] == 1


def test_replay_state_cancelled_event():
    events = [
        {"event": "header", "session": {"total_connections": 5}, "geometry": {}},
        {"event": "cancelled", "connections_done": 1, "total_connections": 5},
        {"event": "run_complete", "cancelled": True},
    ]
    state = viewer.replay_state(events)
    assert state.cancelled is True
    assert state.done is True


# --------------------------------------------------------------------------- #
# 4. Graceful degradation + MCP registration
# --------------------------------------------------------------------------- #

def test_open_route_viewer_reports_reason_when_tk_unavailable(scratch_board, monkeypatch):
    monkeypatch.setattr(router, "_tk_available", lambda: False)
    result = router.open_route_viewer(scratch_board)
    assert result["launched"] is False
    assert "tkinter" in result["reason"].lower()


def test_open_route_viewer_missing_viewer_script(scratch_board, monkeypatch, tmp_path):
    # tkinter IS available in this test env - force the "script not found"
    # branch instead, another non-raising failure path.
    monkeypatch.setattr(router, "_tk_available", lambda: True)
    fake_module_file = tmp_path / "kicad_router_tool.py"
    fake_module_file.write_text("", encoding="utf-8")
    monkeypatch.setattr(router, "__file__", str(fake_module_file))
    result = router.open_route_viewer(scratch_board)
    assert result["launched"] is False
    assert "viewer script not found" in result["reason"]


def test_open_kicad_route_viewer_tool_registered():
    from kicad_mcp_server import KiCadMcpServer

    tools = KiCadMcpServer().tools
    assert "open_kicad_route_viewer" in tools
    entry = tools["open_kicad_route_viewer"]
    assert callable(entry["handler"])
    assert "project_path" in entry["inputSchema"]["properties"]
    assert "board" in entry["inputSchema"]["properties"]


def test_open_kicad_route_viewer_handler_graceful_without_tk(scratch_board, monkeypatch):
    import kicad_mcp_server as server

    monkeypatch.setattr(server, "open_route_viewer", lambda project_path, board=None: {
        "launched": False, "reason": "tkinter is not available in this Python environment; ..."
    })
    srv = server.KiCadMcpServer()
    result = srv.tools["open_kicad_route_viewer"]["handler"]({"project_path": str(scratch_board)})
    assert result["launched"] is False
    assert "reason" in result
