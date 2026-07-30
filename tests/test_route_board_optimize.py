"""Phase 7.6-into-7.17 wiring - `route_board(optimize=True)` drives the
whole-board optimizer as an OPT-IN pipeline stage.

The discipline these tests pin, in order of importance:

  1. `optimize=False` (the default) is indistinguishable from `route_board`
     before the flag existed - same `pipeline` strings, same notes, and the two
     new report keys inert (`optimize=False`, `optimizer=None`).
  2. `optimize=True` really runs an `optimize_board` session and reports its
     real session id and terminal state.
  3. An `awaiting_decision` pause is SURFACED, never silently resolved - the
     session is left genuinely paused and resumable.
  4. `route_board`'s `write` is still the only thing that persists anything: a
     `write=False, optimize=True` call leaves the real board byte-identical.
  5. `effort` maps straight through to the optimizer's identically-named
     preset, with `best`'s 8-hour budget clamped by `route_board`'s own cap.

The fixture is the small synthetic multi-drop SPI project the 7.6/7.7 tests
already use (a handful of optimizer iterations runs in seconds); the kiln board
is far too big to optimize inside a test.
"""

from __future__ import annotations

import json
from pathlib import Path

import kicad_optimizer_tool as o
import kicad_router_tool as r

from synthetic_board import write_multidrop_spi_project

_LEGACY_HOOK = "not_implemented (Phase 7.6, M4)"


def _project(directory: Path, destinations: int = 1, **settings) -> Path:
    """An unrouted synthetic project (real ratsnest, no copper), optionally with
    `optimizer.*` settings written into its `pcb_settings.json`."""
    write_multidrop_spi_project(directory, destinations=destinations, route=False)
    if settings:
        _set_optimizer(directory, **settings)
    return directory


def _set_optimizer(project: Path, **overrides) -> None:
    path = Path(project) / "pcb_settings.json"
    config = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    config.setdefault("optimizer", {}).update(overrides)
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


def _board_bytes(project: Path) -> bytes:
    return (Path(project) / "spibus.kicad_pcb").read_bytes()


# --- 1. default OFF is exactly the old behavior -----------------------------


def test_optimize_defaults_off_and_is_byte_for_byte_the_old_report(tmp_path: Path) -> None:
    project = _project(tmp_path)

    default = r.route_board(project, write=False)
    explicit_off = r.route_board(project, write=False, optimize=False)

    for rep in (default, explicit_off):
        assert rep["optimize"] is False
        assert rep["optimizer"] is None
        # the exact string the pinned 7.17 guard asserts on - unchanged.
        assert rep["pipeline"]["whole_board_optimization"] == _LEGACY_HOOK
        assert rep["pipeline"]["stitching"].startswith("not_implemented")
        assert any("planes/optimizer/stitching are M4 TODO hooks" in n for n in rep["notes"])
        assert not any("optimizer" in n and "session" in n for n in rep["notes"])

    # omitting the flag and passing it False are the same call.
    assert default["pipeline"] == explicit_off["pipeline"]
    assert default["notes"] == explicit_off["notes"]


def test_optimize_off_starts_no_optimizer_session(tmp_path: Path) -> None:
    """The strongest form of "inert unless opted in": the default path must not
    create an optimizer session at all."""
    project = _project(tmp_path)
    r.route_board(project, write=False)
    session = o.get_route_session(project)
    assert session["found"] is False
    assert session["known_sessions"] == []


# --- 2. opt-in actually runs the optimizer ----------------------------------


def test_optimize_true_runs_a_real_session_to_a_terminal_state(tmp_path: Path) -> None:
    project = _project(tmp_path, seed=7, max_iterations=3)

    rep = r.route_board(project, write=False, optimize=True)

    assert rep["optimize"] is True
    opt = rep["optimizer"]
    assert opt is not None
    assert opt["command"] == "optimize_board"
    # a REAL session id: the board's own session store knows it.
    assert opt["session_id"] in o.get_route_session(project)["known_sessions"]
    # driven out of `running`, exactly as specced.
    assert opt["state"] in ("converged", "budget_exhausted")
    assert opt["state"] != "running"
    assert opt["iteration"] >= 1

    hook = rep["pipeline"]["whole_board_optimization"]
    assert hook.startswith("done: ")
    assert opt["state"] in hook          # converged vs budget_exhausted, distinguished
    assert opt["session_id"] in hook
    assert not hook.startswith("not_implemented")
    # the other hooks are untouched by this stage.
    assert rep["pipeline"]["stitching"].startswith("not_implemented")


def test_optimize_true_with_write_persists_and_reports_written(tmp_path: Path) -> None:
    project = _project(tmp_path, seed=7, max_iterations=2)
    before = _board_bytes(project)

    rep = r.route_board(project, write=True, optimize=True)

    assert rep["written"] is True
    opt = rep["optimizer"]
    assert opt["state"] in ("converged", "budget_exhausted")
    assert opt["written"] is True
    assert _board_bytes(project) != before


# --- 3. a pause is surfaced, never silently answered ------------------------


def _always_pause(project: Path) -> None:
    """The 7.7 tests' own pause harness: a huge `min_score_spread` makes every
    multi-candidate iteration a "near tie", so the session pauses on demand."""
    path = Path(project) / "pcb_settings.json"
    config = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    config.setdefault("optimizer", {}).setdefault("ai_decisions", {}).update(
        {"enabled": True, "min_score_spread": 1e9})
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


def test_awaiting_decision_is_surfaced_and_never_auto_resolved(tmp_path: Path) -> None:
    project = _project(tmp_path, destinations=2, seed=7, max_iterations=6)
    _always_pause(project)

    rep = r.route_board(project, write=False, optimize=True)

    opt = rep["optimizer"]
    assert opt["state"] == "awaiting_decision", (
        f"fixture never produced a near-tie to decide (ended {opt['state']})")

    # the pause is fully surfaced in route_board's own report...
    pending = opt["pending_decision"]
    assert pending and pending["decision_id"]
    assert 2 <= len(pending["options"]) <= 4
    hook = rep["pipeline"]["whole_board_optimization"]
    assert hook.startswith("paused: awaiting_decision")
    assert pending["decision_id"] in hook
    assert "NOT auto-resolved" in hook
    assert any("did NOT answer it" in n for n in rep["notes"])

    # ...and NOT answered: nothing in the decision log resolved it, and the
    # session is still genuinely paused when read back independently.
    assert not any(e["decision_id"] == pending["decision_id"] for e in opt["decision_log"])
    reread = o.get_route_session(project, opt["session_id"])
    assert reread["state"] == "awaiting_decision"
    assert reread["pending_decision"]["decision_id"] == pending["decision_id"]

    # left resumable: a human can still answer it the ordinary way afterwards.
    answered = o.decide_route(project, opt["session_id"], pending["decision_id"],
                              pending["options"][0]["id"], rationale="test")
    assert answered["state"] in ("running", "converged", "budget_exhausted")


def test_paused_session_with_write_true_still_writes_nothing_from_the_optimizer(tmp_path: Path) -> None:
    """`write=True` must not become a back door that resumes (and so auto-defers)
    a paused session in order to have something to apply."""
    project = _project(tmp_path, destinations=2, seed=7, max_iterations=6)
    _always_pause(project)

    rep = r.route_board(project, write=True, optimize=True)
    opt = rep["optimizer"]
    assert opt["state"] == "awaiting_decision"
    assert opt["written"] is False
    assert not any(e["decision_id"] == opt["pending_decision"]["decision_id"]
                   for e in opt["decision_log"])
    assert any("were NOT written" in n for n in rep["notes"])


# --- 4. write=False persists nothing ----------------------------------------


def test_write_false_with_optimize_true_leaves_the_real_board_byte_identical(tmp_path: Path) -> None:
    project = _project(tmp_path, seed=7, max_iterations=3)
    before = _board_bytes(project)

    rep = r.route_board(project, write=False, optimize=True)

    assert rep["write"] is False and rep["written"] is False
    opt = rep["optimizer"]
    assert opt["state"] in ("converged", "budget_exhausted")
    assert opt["write"] is False and opt["written"] is False
    # the optimizer moved copper around on its own scratch copy...
    assert opt["moves"], "the stage must actually have done work for this to prove anything"
    # ...and the real board is untouched, to the byte.
    assert _board_bytes(project) == before


# --- 5. effort maps straight through (and `best` is capped) -----------------


def test_effort_passes_straight_through_to_the_optimizer_preset(tmp_path: Path) -> None:
    project = _project(tmp_path, seed=7)

    rep = r.route_board(project, write=False, optimize=True, effort="quick")

    opt = rep["optimizer"]
    assert opt["effort"] == "quick"
    # the optimizer's OWN `quick` preset, not a second vocabulary invented here.
    assert opt["accept"] == o._EFFORT_PRESETS["quick"]["accept"]
    assert opt["max_iterations"] == o._EFFORT_PRESETS["quick"]["max_iterations"]
    assert rep["effort"] == "quick"
    assert any("passed straight through to the optimizer" in n for n in rep["notes"])


def test_effort_best_is_capped_by_route_boards_own_bound_and_says_so(tmp_path: Path) -> None:
    """`best` means an 8-hour optimizer budget (7.15). One synchronous
    `route_board` call must not inherit that silently - it is clamped, and the
    clamp is reported. `max_iterations=1` keeps the test itself quick."""
    project = _project(tmp_path, seed=7, max_iterations=1)

    rep = r.route_board(project, write=False, optimize=True, effort="best")

    opt = rep["optimizer"]
    assert opt["effort"] == "best"
    assert opt["accept"] == o._EFFORT_PRESETS["best"]["accept"]   # "sa", from the preset
    assert o._EFFORT_PRESETS["best"]["time_budget_s"] == 8.0 * 3600.0
    assert opt["time_budget_s"] == r._ROUTE_BOARD_OPTIMIZE_TIME_CAP_S
    assert opt["time_budget_s"] < o._EFFORT_PRESETS["best"]["time_budget_s"]
    assert any("time budget capped" in n and opt["session_id"] in n for n in rep["notes"])


def test_quick_and_balanced_budgets_are_not_clamped(tmp_path: Path) -> None:
    """The cap only ever LOWERS a budget, so the ordinary efforts are
    unaffected - they behave exactly as optimize_kicad_board would."""
    project = _project(tmp_path, seed=7, max_iterations=1, time_budget_s=42.0)

    rep = r.route_board(project, write=False, optimize=True, effort="balanced")

    assert rep["optimizer"]["time_budget_s"] == 42.0
    assert not any("time budget capped" in n for n in rep["notes"])


# --- 6. the surfaces stay in sync (MCP schema + CLI) ------------------------


def test_cli_exposes_optimize_flag_and_defaults_it_off(tmp_path: Path, capsys) -> None:
    project = _project(tmp_path)

    rc = r.main(["route", str(project), "--effort", "quick"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "optimizer:" not in out          # default off => no optimizer line

    rc = r.main(["route", str(project), "--effort", "quick", "--optimize"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "optimizer: state=" in out
    assert "written=False" in out


def test_mcp_schema_declares_optimize(tmp_path: Path) -> None:
    from kicad_mcp_server import KiCadMcpServer

    server = KiCadMcpServer()
    schema = server.tools["route_kicad_board"]["inputSchema"]["properties"]
    assert schema["optimize"]["type"] == "boolean"
    assert schema["optimize"]["default"] is False
    assert "auto-answered" in schema["optimize"]["description"]

    # and the handler really forwards it (default off, opt-in on).
    project = _project(tmp_path, seed=7, max_iterations=1)
    handler = server.tools["route_kicad_board"]["handler"]
    assert handler({"project_path": str(project)})["optimizer"] is None
    forwarded = handler({"project_path": str(project), "optimize": True})
    assert forwarded["optimizer"] is not None
    assert forwarded["optimizer"]["state"] in ("converged", "budget_exhausted", "awaiting_decision")
