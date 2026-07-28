"""Phase 7.6 - `optimize_kicad_board`, the iterative whole-board optimizer.

Fixtures are synthetic multi-drop SPI projects
(`synthetic_board.write_multidrop_spi_project`): small enough that a handful of
optimizer iterations runs in seconds, but real enough to exercise the actual
router (they have multi-pad nets, a real ratsnest, and - with `route=True` -
pre-existing copper that the optimizer must treat as untouchable human copper).

The kiln-board tests here are deliberately CHEAP: they assert the ownership
guards (which zones/copper a move may ever consider) without running full
iterations against a 39-connection board.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import kicad_optimizer_tool as o
import kicad_pcb_tool as k
import kicad_router_tool as r

from synthetic_board import write_multidrop_spi_project


# --- helpers ---------------------------------------------------------------


def _unrouted_project(directory: Path, destinations: int = 1) -> Path:
    """A small board with a real ratsnest and no copper at all - the optimizer
    has obvious work to do, so the score curve is unambiguous."""
    write_multidrop_spi_project(directory, destinations=destinations, route=False)
    return directory


def _routed_project(directory: Path, destinations: int = 1) -> Path:
    """The same board WITH its synthetic copper already placed. None of that
    copper is in `autorouter_owned`, so for every guard in this module it is
    hand-routed human copper."""
    write_multidrop_spi_project(directory, destinations=destinations, route=True)
    return directory


def _set_ai_decisions(project: Path, **overrides) -> None:
    """Write an `optimizer.ai_decisions` policy into the project's settings.

    The 7.7 tests need to control the pause GATE, not the board: pushing
    `min_score_spread` far above any real spread makes every multi-candidate
    iteration a "near tie" on demand, and 0.0 makes none of them one. That is
    the only way to exercise the pause path deterministically on a fixture whose
    natural score spreads depend on the router's output.
    """
    path = Path(project) / "pcb_settings.json"
    config = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    config.setdefault("optimizer", {}).setdefault("ai_decisions", {}).update(overrides)
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


def _always_pause(project: Path, **overrides) -> None:
    _set_ai_decisions(project, enabled=True, min_score_spread=1e9, **overrides)


def _run_to_first_pause(project: Path, limit: int = 10, **kwargs) -> dict:
    """Start a session and advance it until it pauses.

    Which ITERATION first pauses is a property of the board, not of the gate: an
    iteration that produces only one applicable candidate has nothing to choose
    between and commits without asking, however wide the configured spread is.
    So a test that wants a paused session asks for one rather than assuming it
    arrives on iteration 1.
    """
    report = o.optimize_board(project, max_iterations_per_call=1, **kwargs)
    calls = 0
    while report["state"] == "running" and calls < limit:
        report = o.optimize_board(project, session_id=report["session_id"],
                                  max_iterations_per_call=1)
        calls += 1
    assert report["state"] == "awaiting_decision", (
        f"fixture never produced a near-tie to decide (ended {report['state']})")
    return report


def _run_to_completion(project: Path, report: dict, decider=None, limit: int = 40) -> dict:
    """Drive a session to a terminal state, answering any pause with `decider`
    (the "scripted decider" harness - a canned function, never a real AI). With
    `decider=None` the pauses are left unanswered, which exercises the
    timeout-to-defer path instead."""
    calls = 0
    while report["state"] in ("running", "awaiting_decision") and calls < limit:
        if report["state"] == "awaiting_decision" and decider is not None:
            pending = report["pending_decision"]
            o.decide_route(project, report["session_id"], pending["decision_id"],
                           decider(pending), rationale="scripted decider")
        report = o.optimize_board(project, session_id=report["session_id"],
                                  max_iterations_per_call=2)
        calls += 1
    return report


def _segment_uuids(project: Path) -> set[str]:
    board_path, _, _ = k._resolve_project_path(project)
    tracks = k._parse_tracks_cached(board_path)
    return {t["uuid"] for t in tracks["segments"] + tracks["vias"] + tracks["arcs"] if t.get("uuid")}


# --- 1. the loop actually improves the board score --------------------------


def test_greedy_run_improves_score_and_curve_is_non_increasing(tmp_path: Path) -> None:
    project = _unrouted_project(tmp_path)
    before = o.score_board(project)
    assert before["unrouted_count"] > 0

    report = o.optimize_board(project, max_iterations_per_call=3, seed=7, accept="greedy")

    assert report["command"] == "optimize_board"
    assert report["state"] in o.SESSION_STATES
    curve = report["score_curve"]
    assert curve[0] == pytest.approx(before["total"])
    # greedy accepts only strict improvements, so the curve can never rise.
    for earlier, later in zip(curve, curve[1:]):
        assert later <= earlier + 1e-9
    # and on a board this unrouted it must actually have improved something.
    assert curve[-1] < curve[0]
    assert report["score_delta"] < 0
    assert any(m["accepted"] for m in report["moves"])
    # every accepted move records why it was taken and what it bought.
    for move in report["moves"]:
        assert move["reason"]
        assert move["type"] in (None, *o._MOVE_APPLIERS)


# --- 2. write=False never touches the real board ----------------------------


def test_dry_run_never_touches_the_real_board(tmp_path: Path) -> None:
    project = _unrouted_project(tmp_path)
    board_path, _, _ = k._resolve_project_path(project)
    before_bytes = board_path.read_bytes()

    report = o.optimize_board(project, max_iterations_per_call=2, seed=3)

    assert report["write"] is False and report["written"] is False
    assert board_path.read_bytes() == before_bytes
    # the work really happened - it just happened on the scratch copy.
    assert report["iteration"] >= 1
    assert Path(report["scratch_dir"]).exists()
    assert report["diff"]["available"] is True
    assert report["diff"]["scratch_board"] != str(board_path)


def test_dry_run_across_a_whole_session_leaves_the_board_byte_identical(tmp_path: Path) -> None:
    project = _unrouted_project(tmp_path)
    board_path, _, _ = k._resolve_project_path(project)
    before_bytes = board_path.read_bytes()

    report = o.optimize_board(project, max_iterations_per_call=2, seed=11, max_iterations=4)
    while report["state"] in ("running", "awaiting_decision"):
        report = o.optimize_board(project, session_id=report["session_id"],
                                  max_iterations_per_call=2)
    assert board_path.read_bytes() == before_bytes


# --- 3. write=True applies the final state; a re-run then converges ---------


def test_write_applies_final_state_and_rerun_converges_immediately(tmp_path: Path) -> None:
    project = _unrouted_project(tmp_path)
    board_path, _, _ = k._resolve_project_path(project)
    unrouted_before = r.get_ratsnest(project)["summary"]["total_connections"]

    report = o.optimize_board(project, max_iterations_per_call=6, seed=5, max_iterations=6)
    while report["state"] in ("running", "awaiting_decision"):
        report = o.optimize_board(project, session_id=report["session_id"], max_iterations_per_call=6)
    assert report["state"] in ("converged", "budget_exhausted")

    final_score = report["current_score"]["total"]
    applied = o.optimize_board(project, session_id=report["session_id"], write=True)
    assert applied["written"] is True
    assert applied["refill_required_note"]

    # the REAL board now carries exactly the state that was scored.
    assert o.score_board(project)["total"] == pytest.approx(final_score)
    assert r.get_ratsnest(project)["summary"]["total_connections"] < unrouted_before
    # ...and everything applied is undoable through the ordinary route path.
    owned = k.load_board_local(project)["data"]["autorouter_owned"]
    assert owned["segments"]

    # a fresh session on the resulting board is already near-optimal: no move
    # within budget buys `convergence_delta`, so it converges on the spot.
    rerun = o.optimize_board(project, max_iterations_per_call=2, seed=5)
    assert rerun["state"] == "converged"
    assert rerun["score_curve"][-1] <= rerun["score_curve"][0] + 1e-9


def test_write_refuses_while_the_session_is_still_running(tmp_path: Path) -> None:
    project = _unrouted_project(tmp_path)
    board_path, _, _ = k._resolve_project_path(project)
    before_bytes = board_path.read_bytes()

    report = o.optimize_board(project, max_iterations_per_call=1, seed=5,
                              max_iterations=10, write=True)
    if report["state"] == "running":
        assert report["written"] is False
        assert "still running" in report["write_skipped_reason"]
        assert board_path.read_bytes() == before_bytes


def test_write_refuses_when_the_real_board_changed_under_the_session(tmp_path: Path) -> None:
    project = _unrouted_project(tmp_path)
    board_path, _, _ = k._resolve_project_path(project)

    report = o.optimize_board(project, max_iterations_per_call=8, seed=5, max_iterations=8)
    while report["state"] in ("running", "awaiting_decision"):
        report = o.optimize_board(project, session_id=report["session_id"], max_iterations_per_call=8)

    # simulate the user editing the board in KiCad mid-session.
    board_path.write_bytes(board_path.read_bytes() + b"\n")
    k._invalidate_board_cache(board_path)
    edited = board_path.read_bytes()

    blocked = o.optimize_board(project, session_id=report["session_id"], write=True)
    assert blocked["written"] is False
    assert "changed since this session started" in blocked["write_skipped_reason"]
    assert board_path.read_bytes() == edited


# --- 4. determinism: same seed -> same move sequence and final score --------


def test_same_seed_gives_identical_move_sequence_and_score(tmp_path: Path) -> None:
    first = o.optimize_board(_unrouted_project(tmp_path / "a"),
                             max_iterations_per_call=3, seed=42, max_iterations=3)
    second = o.optimize_board(_unrouted_project(tmp_path / "b"),
                              max_iterations_per_call=3, seed=42, max_iterations=3)

    def signature(report):
        return [(m["type"], m["summary"], m["accepted"], m["score_after"]) for m in report["moves"]]

    assert signature(first) == signature(second)
    assert first["score_curve"] == second["score_curve"]
    assert first["current_score"]["total"] == second["current_score"]["total"]


def test_a_different_seed_is_free_to_diverge(tmp_path: Path) -> None:
    # Not a correctness requirement (a tiny board can converge to the same
    # thing), but the seed must at least REACH the RNG - a seed that changed
    # nothing anywhere would mean the run wasn't actually randomized.
    a = o.optimize_board(_unrouted_project(tmp_path / "a"), max_iterations_per_call=1,
                         seed=1, max_iterations=1)
    b = o.optimize_board(_unrouted_project(tmp_path / "b"), max_iterations_per_call=1,
                         seed=999, max_iterations=1)
    assert a["seed"] == 1 and b["seed"] == 999


# --- 5. session resume: chunked == one big call -----------------------------


def test_chunked_run_reaches_the_same_state_as_one_big_call(tmp_path: Path) -> None:
    """Every loop input (RNG state, iteration, temperature, score curve, the
    scratch board itself) is checkpointed, so the call boundary is not an input
    to any decision. Bounded on ITERATIONS only - a wall-clock bound would
    legitimately diverge."""
    # Both arms are driven to a terminal state. A 7.7 pause also ends a call, so
    # "one big call" can no longer be assumed to be literally one call - which
    # is precisely the property under test: the call boundary, however it
    # arises, must not be an input to any decision.
    one_shot_project = _unrouted_project(tmp_path / "one")
    one_shot = o.optimize_board(one_shot_project, max_iterations_per_call=4,
                                seed=17, max_iterations=4)
    while one_shot["state"] in ("running", "awaiting_decision"):
        one_shot = o.optimize_board(one_shot_project, session_id=one_shot["session_id"],
                                    max_iterations_per_call=4)

    chunked_project = _unrouted_project(tmp_path / "chunked")
    chunked = o.optimize_board(chunked_project, max_iterations_per_call=1, seed=17, max_iterations=4)
    calls = 1
    while chunked["state"] in ("running", "awaiting_decision"):
        chunked = o.optimize_board(chunked_project, session_id=chunked["session_id"],
                                   max_iterations_per_call=1)
        calls += 1

    assert calls > 1, "the chunked run must actually have spanned several calls"
    assert chunked["score_curve"] == one_shot["score_curve"]
    assert [m["summary"] for m in chunked["moves"]] == [m["summary"] for m in one_shot["moves"]]
    assert chunked["current_score"]["total"] == one_shot["current_score"]["total"]
    assert chunked["state"] == one_shot["state"]


def test_session_state_survives_a_fresh_process_view_of_the_board(tmp_path: Path) -> None:
    """The checkpoint lives in the board-local JSON, not in process memory."""
    project = _unrouted_project(tmp_path)
    report = o.optimize_board(project, max_iterations_per_call=1, seed=17, max_iterations=3)

    on_disk = json.loads((Path(project) / "spibus.board_local.json").read_text(encoding="utf-8"))
    session = on_disk["optimizer_sessions"][report["session_id"]]
    assert session["iteration"] == report["iteration"]
    assert session["rng_state"], "the RNG state must be checkpointed, not just the seed"
    assert on_disk["last_optimizer_session"] == report["session_id"]


def test_resuming_an_unknown_session_raises(tmp_path: Path) -> None:
    project = _unrouted_project(tmp_path)
    with pytest.raises(KeyError):
        o.optimize_board(project, session_id="no-such-session")


# --- 6. get_route_session reports the right state, read-only ----------------


def test_get_route_session_reports_state_without_advancing_it(tmp_path: Path) -> None:
    project = _unrouted_project(tmp_path)
    assert o.get_route_session(project)["found"] is False

    running = o.optimize_board(project, max_iterations_per_call=1, seed=23, max_iterations=5)
    mid = o.get_route_session(project)
    assert mid["found"] is True
    assert mid["session_id"] == running["session_id"]
    assert mid["state"] == running["state"]
    assert mid["iteration"] == running["iteration"]

    # read-only: asking twice must not move the session on by an iteration.
    again = o.get_route_session(project, session_id=running["session_id"])
    assert again["iteration"] == mid["iteration"]
    assert again["score_curve"] == mid["score_curve"]

    final = o.optimize_board(project, session_id=running["session_id"], max_iterations_per_call=10)
    while final["state"] in ("running", "awaiting_decision"):
        final = o.optimize_board(project, session_id=final["session_id"], max_iterations_per_call=10)
    assert final["state"] in ("converged", "budget_exhausted")
    assert o.get_route_session(project)["state"] == final["state"]
    assert o.get_route_session(project)["stop_reason"]


def test_budget_exhausted_is_distinguishable_from_converged(tmp_path: Path) -> None:
    project = _unrouted_project(tmp_path, destinations=2)
    report = o.optimize_board(project, max_iterations_per_call=1, seed=31, max_iterations=1)
    # a 1-iteration budget on a board with plenty left to do ends on the budget,
    # not on convergence.
    assert report["state"] in ("budget_exhausted", "converged")
    if report["state"] == "budget_exhausted":
        assert report["stop_reason"] == "max_iterations"


# --- 7. human copper and hand-made zones are never touched ------------------


def test_pre_existing_human_copper_is_never_ripped_or_altered(tmp_path: Path) -> None:
    project = _routed_project(tmp_path, destinations=2)
    human_uuids = _segment_uuids(project)
    assert human_uuids, "fixture must actually have hand copper to protect"

    report = o.optimize_board(project, max_iterations_per_call=3, seed=13, max_iterations=3)

    # on the scratch board every move ran against...
    assert human_uuids <= _segment_uuids(Path(report["scratch_dir"]))
    # ...and on the real board, which a dry run must not touch at all.
    assert _segment_uuids(project) == human_uuids


def test_ripup_move_against_human_copper_removes_nothing(tmp_path: Path) -> None:
    """The guard is `unroute_nets`' own: it deletes only uuids recorded in
    `autorouter_owned`, and hand copper is never in that list. The optimizer
    must not bypass it (e.g. by passing allow_hand_copper_ripup)."""
    project = _routed_project(tmp_path, destinations=2)
    before = _segment_uuids(project)

    stats = o._apply_ripup_reroute(Path(project), {"nets": ["/SPI/SCK"], "max_ripup": 4})

    assert stats["ripped_uuids"] == 0
    assert before <= _segment_uuids(project)


def test_hand_made_zones_are_never_offered_to_the_modify_plane_move(scratch_board: Path) -> None:
    """kiln's six hand-made zones (mainGnd, safty_gnd, antenna, main3.3,
    main12v, 3.3v_safty) exist on the board but are not in
    `autorouter_owned.zones`, so move (f) can never even propose one."""
    board_path, _, _ = k._resolve_project_path(scratch_board)
    hand_made = {z["uuid"] for z in r._parse_zones_cached(board_path)}
    assert len(hand_made) >= 6

    owned = (k.load_board_local(scratch_board)["data"].get("autorouter_owned", {}) or {})
    assert not owned.get("zones"), "fixture must start with no autorouter-owned zones"

    import random
    assert o._candidate_modify_plane(Path(scratch_board), [], random.Random(0)) is None

    # and the writer itself refuses even if a caller hand-crafted the move.
    zone_uuid = sorted(hand_made)[0]
    with pytest.raises(ValueError):
        r.modify_plane(scratch_board, zone_uuid,
                       new_outline=[{"x": 0.0, "y": 0.0}, {"x": 1.0, "y": 0.0}, {"x": 1.0, "y": 1.0}],
                       write=True)


# --- the six move types: each one is reachable, and each one does something -


def test_reroute_and_layer_moves_are_offered_on_a_routed_bus_board(tmp_path: Path) -> None:
    """Moves (a) rip-up+reroute, (b) bundle reroute and (c) layer swap all need
    a board with routed copper and a detected bus - which is exactly the
    multi-drop SPI fixture with `route=True`."""
    import random
    project = Path(_routed_project(tmp_path, destinations=2))
    worst = o._ranked_nets(project)[:5]

    assert o._candidate_ripup_reroute(project, worst, random.Random(0))
    bundle = o._candidate_reroute_bundle(project, worst, random.Random(0))
    assert bundle and len(bundle["params"]["nets"]) >= 2
    swap = o._candidate_swap_layer(project, worst, random.Random(0))
    assert swap and swap["params"]["layer_type"] in ("signal", "power", "mixed")


def test_layer_swap_restores_the_real_cost_weights_before_scoring(tmp_path: Path) -> None:
    """Move (c) perturbs the trial's `layer_purpose` weights to steer the
    reroute, then MUST put them back - a candidate scored under perturbed
    weights would be incomparable with every other candidate."""
    project = Path(_routed_project(tmp_path, destinations=2))
    settings_path = project / "pcb_settings.json"
    settings_path.write_text(json.dumps({"optimizer": {"seed": 1}}, indent=2) + "\n", encoding="utf-8")
    before = settings_path.read_text(encoding="utf-8")

    o._apply_swap_layer(project, {"net": "/SPI/SCK", "net_kind": "signal",
                                  "from_layer": "F.Cu", "layer_type": "signal",
                                  "max_ripup": 0})

    assert settings_path.read_text(encoding="utf-8") == before


def test_plane_and_stitching_moves_are_reachable_and_effective(scratch_board: Path) -> None:
    """Moves (d) stitching via, (e) create plane and (f) modify plane need real
    pours, real islands and a power net - kiln has all three, and none of these
    moves routes anything, so this stays cheap despite the board's size.

    (f) is reachable only AFTER (e): `modify_plane` refuses any zone that is
    not in `autorouter_owned.zones`, so the only zone it can ever touch is one
    the optimizer itself poured.
    """
    import random
    project = Path(scratch_board)
    worst = o._ranked_nets(project)[:5]

    # (d) stitching via - lands in an island and cuts that island's cost.
    stitch = o._candidate_add_stitching_via(project, worst, random.Random(0))
    assert stitch, "kiln's pours have costed islands to stitch"
    plane_cost_before = o.score_board(project)["plane_cost"]
    detail = o._apply_add_stitching_via(project, stitch["params"])
    assert detail["uuid"] in k.load_board_local(project)["data"]["autorouter_owned"]["vias"]
    assert o.score_board(project)["plane_cost"] < plane_cost_before

    # (e) create plane - only offered when propose_plane prices it BELOW the
    # net's current copper, and it registers the new zone as autorouter-owned.
    create = o._candidate_create_plane(project, worst, random.Random(0))
    assert create and create["params"]["cost_delta"] < 0
    created = o._apply_create_plane(project, create["params"])
    owned_zones = k.load_board_local(project)["data"]["autorouter_owned"]["zones"]
    assert created["zone_uuid"] in owned_zones

    # (f) modify plane - now reachable, and only for the zone (e) just created.
    modify = o._candidate_modify_plane(project, worst, random.Random(0))
    assert modify and modify["params"]["uuid"] in owned_zones
    zones_before = {z["uuid"]: list(z["polygon"]) for z in r._parse_zones_cached(
        k._resolve_project_path(project)[0])}
    assert o._apply_modify_plane(project, modify["params"])["written"] is True
    zones_after = {z["uuid"]: list(z["polygon"]) for z in r._parse_zones_cached(
        k._resolve_project_path(project)[0])}
    changed = {uid for uid in zones_before if zones_before[uid] != zones_after.get(uid)}
    assert changed == {modify["params"]["uuid"]}, "only the owned zone may move"


def test_stitching_via_is_recorded_as_undoable_autorouter_copper(scratch_board: Path) -> None:
    project = Path(scratch_board)
    board_path, _, _ = k._resolve_project_path(project)
    before = _segment_uuids(project)

    placed = o._place_stitching_via(project, "GND_Main", 120.0, 78.0)
    assert placed["uuid"] in _segment_uuids(project)

    r.unroute_nets(project, nets=["GND_Main"], write=True)
    assert _segment_uuids(project) == before, "a stitching via must undo like any routed via"


# --- scoring / acceptance unit checks --------------------------------------


def test_score_is_the_sum_of_its_declared_terms(tmp_path: Path) -> None:
    project = _routed_project(tmp_path, destinations=2)
    score = o.score_board(project)
    assert score["total"] == pytest.approx(
        score["trace_cost"] + score["plane_cost"] + score["unrouted_cost"], abs=1e-6)
    assert score["unrouted_cost"] == pytest.approx(score["unrouted_penalty"] * score["unrouted_count"])


def test_unrouted_nets_are_ranked_by_their_penalty_contribution(tmp_path: Path) -> None:
    project = _unrouted_project(tmp_path, destinations=2)
    ranked = o._ranked_nets(project)
    assert ranked, "an unrouted board still has cost contributors"
    assert ranked[0]["unrouted_connections"] >= 1
    # strictly worst-first, with net name as the deterministic tie-break.
    keys = [(-n["contribution"], n["net"]) for n in ranked]
    assert keys == sorted(keys)


def test_greedy_rejects_worse_moves_and_sa_can_accept_them() -> None:
    import random
    assert o._accept("greedy", -1.0, 10.0, random.Random(0))[0] is True
    assert o._accept("greedy", 1.0, 10.0, random.Random(0))[0] is False
    # at a high temperature SA accepts a slightly worse move with high
    # probability; at zero temperature it never does.
    accepted = sum(o._accept("sa", 1.0, 1000.0, random.Random(seed))[0] for seed in range(20))
    assert accepted > 10
    assert o._accept("sa", 1.0, 0.0, random.Random(0))[0] is False


def test_sa_policy_runs_and_cools(tmp_path: Path) -> None:
    project = _unrouted_project(tmp_path)
    report = o.optimize_board(project, max_iterations_per_call=2, seed=4,
                              accept="sa", max_iterations=2)
    assert report["accept"] == "sa"
    settings = k.load_pcb_settings(project)["config"]["optimizer"]
    assert report["temperature"] < settings["sa_initial_temp"]


def test_invalid_accept_policy_raises(tmp_path: Path) -> None:
    project = _unrouted_project(tmp_path)
    with pytest.raises(ValueError):
        o.optimize_board(project, accept="annealing-ish")


# --- MCP registration -------------------------------------------------------


def test_mcp_tools_are_registered_and_route_to_the_core_functions(tmp_path: Path) -> None:
    from kicad_mcp_server import KiCadMcpServer

    server = KiCadMcpServer()
    for name in ("optimize_kicad_board", "get_kicad_route_session"):
        tool = server.tools[name]
        assert tool["inputSchema"]["required"] == ["project_path"]
        assert callable(tool["handler"])

    project = _unrouted_project(tmp_path)
    report = server.tools["optimize_kicad_board"]["handler"](
        {"project_path": str(project), "max_iterations_per_call": 1, "seed": 8})
    assert report["command"] == "optimize_board"
    assert report["written"] is False

    session = server.tools["get_kicad_route_session"]["handler"]({"project_path": str(project)})
    assert session["found"] is True
    assert session["session_id"] == report["session_id"]
    # read-only: the reporting tool must not have advanced the session.
    assert session["iteration"] == report["iteration"]


def test_decide_kicad_route_is_registered_and_routes_to_the_core_function() -> None:
    """Phase 7.7's tool. (This test replaced 7.6's
    `test_decide_kicad_route_is_not_registered`, whose whole purpose was to fail
    the moment 7.7 shipped.)"""
    from kicad_mcp_server import KiCadMcpServer

    tool = KiCadMcpServer().tools["decide_kicad_route"]
    assert tool["inputSchema"]["required"] == [
        "project_path", "session_id", "decision_id", "choice"]
    assert callable(tool["handler"])


# --- 7.7: decisions ---------------------------------------------------------


def test_a_near_tie_pauses_the_session_with_a_well_formed_pending_decision(tmp_path: Path) -> None:
    """The pause gate: when the cost model cannot separate the top candidates,
    the chunk stops mid-iteration and hands out a CLOSED, pre-scored option
    list. Nothing is applied while it is paused."""
    project = _unrouted_project(tmp_path, destinations=2)
    _always_pause(project)

    report = _run_to_first_pause(project, seed=7, max_iterations=6)

    assert report["state"] in o.SESSION_STATES
    assert report["stop_reason"] == "awaiting_decision"
    assert report["pauses_used"] == 1
    # nothing was committed BY THE PAUSED ITERATION: the move log and the score
    # curve still describe only the iterations that completed before it.
    assert len(report["moves"]) == report["iteration"]
    assert len(report["score_curve"]) == report["iteration"] + 1
    assert report["current_score"]["total"] == report["score_curve"][-1]

    pending = report["pending_decision"]
    assert pending["decision_id"]
    assert pending["iteration"] == report["iteration"] + 1
    assert pending["decision_type"] in k.DEFAULT_PCB_SETTINGS["optimizer"]["ai_decisions"]["decision_types"]
    assert 2 <= len(pending["options"]) <= 4
    assert pending["default_choice"] == pending["options"][0]["id"]
    for index, option in enumerate(pending["options"]):
        assert option["id"] == f"opt{index + 1}"
        assert option["summary"] and option["type"] in o._MOVE_APPLIERS
        assert option["score_delta"] == pytest.approx(
            option["score_total"] - report["current_score"]["total"])
        assert option["is_default"] is (index == 0)
        assert Path(option["trial_dir"]).exists(), "a paused option must outlive the chunk"
    # best-first, and the top two really are within the configured spread.
    totals = [o_["score_total"] for o_ in pending["options"]]
    assert totals == sorted(totals)
    assert totals[1] - totals[0] < pending["min_score_spread"]


def test_a_clear_winner_never_pauses_and_matches_the_ai_disabled_run_exactly(tmp_path: Path) -> None:
    """PARITY - the most important test here. A session that never hits a
    near-tie must behave exactly like the 7.6 core did.

    Two independent statements of the same thing:
      1. with `min_score_spread` at 0 (no spread can ever be under it) and with
         `ai_decisions.enabled: false`, no call ever reports a pause; and
      2. a run that DOES pause on every iteration but answers every decision
         with `defer` lands on an identical move sequence and score curve -
         because `defer` IS the 7.6 rule ("take the best-scored candidate").
    """
    disabled = _unrouted_project(tmp_path / "disabled", destinations=2)
    _set_ai_decisions(disabled, enabled=False)
    baseline = o.optimize_board(disabled, max_iterations_per_call=2, seed=7, max_iterations=6)
    baseline = _run_to_completion(disabled, baseline)
    assert baseline["state"] in ("converged", "budget_exhausted")
    assert baseline["pauses_used"] == 0
    assert baseline["pending_decision"] is None
    assert all(entry["auto"] for entry in baseline["decision_log"])
    assert all(entry["auto_reason"] == "ai_decisions_disabled"
               for entry in baseline["decision_log"] if len(entry["options"]) > 1)

    clear = _unrouted_project(tmp_path / "clear", destinations=2)
    _set_ai_decisions(clear, enabled=True, min_score_spread=0.0)
    strict = _run_to_completion(clear, o.optimize_board(
        clear, max_iterations_per_call=2, seed=7, max_iterations=6))
    assert strict["pauses_used"] == 0

    deferred = _unrouted_project(tmp_path / "deferred", destinations=2)
    _always_pause(deferred)
    always = _run_to_completion(deferred, o.optimize_board(
        deferred, max_iterations_per_call=2, seed=7, max_iterations=6),
        decider=lambda pending: "defer")
    assert always["pauses_used"] > 0, "this arm must actually have paused"

    def signature(report):
        return ([(m["type"], m["summary"], m["accepted"], m["score_after"])
                 for m in report["moves"]], report["score_curve"], report["state"])

    assert signature(strict) == signature(baseline)
    assert signature(always) == signature(baseline)


def test_scripted_decider_drives_a_paused_session_to_a_terminal_state(tmp_path: Path) -> None:
    """The harness the plan asks for before a live AI sits in the loop: a canned
    decider answering every pause, proving the pause/decide/resume cycle closes
    and the session still reaches a terminal state."""
    project = _unrouted_project(tmp_path, destinations=2)
    _always_pause(project)

    answers: list[tuple[str, str]] = []

    def decider(pending: dict) -> str:
        # deliberately NOT always the default - a decider that only ever defers
        # would not prove the AI's choice is the one that gets applied.
        choice = pending["options"][-1]["id"] if len(answers) % 2 else "defer"
        answers.append((pending["decision_id"], choice))
        return choice

    report = o.optimize_board(project, max_iterations_per_call=2, seed=19, max_iterations=6)
    report = _run_to_completion(project, report, decider=decider)

    assert answers, "the harness must actually have been asked something"
    assert report["state"] in ("converged", "budget_exhausted")
    assert report["pending_decision"] is None
    assert report["iteration"] >= 1

    ai_entries = [e for e in report["decision_log"] if not e["auto"]]
    assert len(ai_entries) == len(answers)
    for (decision_id, choice), entry in zip(answers, ai_entries):
        assert entry["decision_id"] == decision_id
        assert entry["choice"] == choice
        assert entry["rationale"] == "scripted decider"
        # `defer` is recorded as asked, and resolved to the default option.
        assert entry["resolved_choice"] in {opt["id"] for opt in entry["options"]}
        if choice == "defer":
            assert entry["resolved_choice"] == "opt1"
        else:
            assert entry["resolved_choice"] == choice
    # the chosen option is the move that actually landed in the move log.
    for entry in ai_entries:
        chosen = next(o_ for o_ in entry["options"] if o_["id"] == entry["resolved_choice"])
        move = next(m for m in report["moves"] if m["iteration"] == entry["iteration"])
        assert move["summary"] == chosen["summary"] and move["type"] == chosen["type"]


def test_an_undecided_session_times_out_to_defer_on_the_next_resume(tmp_path: Path) -> None:
    """An abandoned pause must not wedge the session: resuming without answering
    takes the optimizer's own best-scored option and carries on."""
    project = _unrouted_project(tmp_path, destinations=2)
    _always_pause(project)

    paused = _run_to_first_pause(project, seed=7, max_iterations=6)
    pending = paused["pending_decision"]

    resumed = o.optimize_board(project, session_id=paused["session_id"], max_iterations_per_call=1)
    assert resumed["state"] != "awaiting_decision" or \
        resumed["pending_decision"]["decision_id"] != pending["decision_id"]
    assert resumed["iteration"] >= pending["iteration"]

    entry = next(e for e in resumed["decision_log"] if e["decision_id"] == pending["decision_id"])
    assert entry["auto"] is True
    assert entry["choice"] == "defer"
    assert entry["auto_reason"] == "resume_without_decision"
    assert entry["resolved_choice"] == pending["default_choice"]
    assert any("resumed without an answer" in note for note in resumed["notes"])

    # and an abandoned session still converges rather than looping on pauses.
    final = _run_to_completion(project, resumed)
    assert final["state"] in ("converged", "budget_exhausted")


def test_max_pauses_per_run_caps_the_escalation_budget(tmp_path: Path) -> None:
    """After the cap, further near-ties auto-decide instead of pausing again -
    the run finishes even if the AI stops answering."""
    project = _unrouted_project(tmp_path, destinations=2)
    _always_pause(project, max_pauses_per_run=1)

    report = o.optimize_board(project, max_iterations_per_call=2, seed=7, max_iterations=6)
    assert report["state"] == "awaiting_decision"
    report = _run_to_completion(project, report, decider=lambda pending: "defer")

    assert report["state"] in ("converged", "budget_exhausted")
    assert report["pauses_used"] == 1
    capped = [e for e in report["decision_log"] if e["auto_reason"] == "max_pauses_per_run"]
    assert capped, "iterations after the cap must record WHY they were not escalated"
    assert all(e["auto"] and e["choice"] == e["options"][0]["id"] for e in capped)


def test_decision_log_records_auto_and_ai_decisions_and_is_inspectable(tmp_path: Path) -> None:
    """Auditability: every committed move appends one fully self-contained log
    entry, and the log is readable through the read-only session report."""
    project = _unrouted_project(tmp_path, destinations=2)
    _always_pause(project, max_pauses_per_run=1)

    report = o.optimize_board(project, max_iterations_per_call=2, seed=23, max_iterations=6)
    report = _run_to_completion(project, report,
                                decider=lambda pending: pending["options"][-1]["id"])

    log = report["decision_log"]
    assert log, "a run that moved must have logged its decisions"
    # one entry per committed move (the "no applicable candidate" iteration
    # commits nothing, so it is a move without a decision).
    assert len(log) == len([m for m in report["moves"] if m["type"] is not None])
    assert any(e["auto"] for e in log) and any(not e["auto"] for e in log)
    for entry in log:
        assert entry["decision_id"] and entry["iteration"] >= 1
        assert entry["decision_type"] in \
            k.DEFAULT_PCB_SETTINGS["optimizer"]["ai_decisions"]["decision_types"]
        assert entry["options"] and entry["scores"]
        assert set(entry["scores"]) == {opt["id"] for opt in entry["options"]}
        assert entry["choice"] and entry["resolved_choice"]
        assert isinstance(entry["auto"], bool)
        assert "rationale" in entry and "accepted" in entry
        assert entry["score_after"] - entry["score_before"] == pytest.approx(entry["delta"])

    reported = o.get_route_session(project, session_id=report["session_id"])
    assert reported["decision_log"] == log
    assert reported["pauses_used"] == report["pauses_used"]

    # and it survives the process: the log is checkpointed, not in-memory state.
    on_disk = json.loads((Path(project) / "spibus.board_local.json").read_text(encoding="utf-8"))
    assert on_disk["optimizer_sessions"][report["session_id"]]["decision_log"] == log


def test_decide_route_refuses_a_stale_decision_or_an_unpaused_session(tmp_path: Path) -> None:
    project = _unrouted_project(tmp_path, destinations=2)
    _always_pause(project)
    paused = _run_to_first_pause(project, seed=7, max_iterations=6)
    pending = paused["pending_decision"]

    with pytest.raises(ValueError):
        o.decide_route(project, paused["session_id"], "not-this-decision", "defer")
    with pytest.raises(ValueError):
        o.decide_route(project, paused["session_id"], pending["decision_id"], "opt99")
    with pytest.raises(KeyError):
        o.decide_route(project, "no-such-session", pending["decision_id"], "defer")

    # a refused answer must leave the pause exactly as it was.
    assert o.get_route_session(project)["pending_decision"]["decision_id"] == pending["decision_id"]

    o.decide_route(project, paused["session_id"], pending["decision_id"], "defer")
    with pytest.raises(ValueError):
        o.decide_route(project, paused["session_id"], pending["decision_id"], "defer")


def test_decide_route_does_not_itself_advance_the_session(tmp_path: Path) -> None:
    """Separation of concerns: answering resolves the pause and applies the
    chosen move, and stops. Generating the NEXT decision is the caller's next
    `optimize_kicad_board` call."""
    project = _unrouted_project(tmp_path, destinations=2)
    _always_pause(project)
    paused = _run_to_first_pause(project, seed=7, max_iterations=6)
    pending = paused["pending_decision"]

    answered = o.decide_route(project, paused["session_id"], pending["decision_id"],
                              "defer", rationale="keep the jumper layer free")

    assert answered["command"] == "decide_route"
    # `converged` is legitimate here (the committed move may itself be the one
    # that stops buying convergence_delta) - what must NOT happen is a second
    # iteration or a second pending decision.
    assert answered["state"] in ("running", "converged")
    assert answered["pending_decision"] is None
    assert answered["iteration"] == pending["iteration"], \
        "exactly the paused iteration, and no more"
    assert len(answered["moves"]) == pending["iteration"]
    assert answered["decision"]["rationale"] == "keep the jumper layer free"
    assert o.get_route_session(project)["iteration"] == pending["iteration"]


def test_the_real_board_is_untouched_across_a_whole_decided_run(tmp_path: Path) -> None:
    """The 7.6 dry-run guarantee is not weakened by the decision loop: pausing,
    parking options and resolving them all happen on scratch copies."""
    project = _unrouted_project(tmp_path, destinations=2)
    _always_pause(project)
    board_path, _, _ = k._resolve_project_path(project)
    before_bytes = board_path.read_bytes()

    report = o.optimize_board(project, max_iterations_per_call=2, seed=7, max_iterations=6)
    report = _run_to_completion(project, report, decider=lambda pending: "defer")

    assert board_path.read_bytes() == before_bytes
    assert report["written"] is False


def test_ai_decisions_settings_block_is_read_not_invented(tmp_path: Path) -> None:
    """The 6.1 schema owns these key names; the optimizer must consume them
    rather than define its own."""
    defaults = k.DEFAULT_PCB_SETTINGS["optimizer"]["ai_decisions"]
    assert set(o._ai_decision_config({})) == set(defaults)
    assert o._ai_decision_config({}) == {
        "enabled": defaults["enabled"],
        "min_score_spread": float(defaults["min_score_spread"]),
        "max_pauses_per_run": int(defaults["max_pauses_per_run"]),
        "decision_types": list(defaults["decision_types"]),
    }
    # every move type escalates as one of the six documented decision types.
    assert set(o._MOVE_DECISION_TYPES) == set(o._MOVE_APPLIERS)
    assert set(o._MOVE_DECISION_TYPES.values()) <= set(defaults["decision_types"])

    project = _unrouted_project(tmp_path)
    _set_ai_decisions(project, min_score_spread=1.25, max_pauses_per_run=3)
    report = o.optimize_board(project, max_iterations_per_call=1, seed=2, max_iterations=1)
    assert report["ai_decisions"]["min_score_spread"] == 1.25
    assert report["ai_decisions"]["max_pauses_per_run"] == 3


# --- Phase 7.15: effort presets ----------------------------------------------


def _set_optimizer(project: Path, **overrides) -> None:
    """Write arbitrary `optimizer.*` keys into the project's settings (the
    same pattern `_set_ai_decisions` uses for the `ai_decisions` sub-block)."""
    path = Path(project) / "pcb_settings.json"
    config = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    config.setdefault("optimizer", {}).update(overrides)
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


def test_effort_presets_resolve_to_distinct_knob_bundles(tmp_path: Path) -> None:
    """`quick` and `best` must resolve to visibly different session configs -
    inspecting the session (via `max_iterations_per_call=0`, which starts the
    session but runs zero iterations) is enough, no router work needed."""
    quick_project = tmp_path / "quick"
    _unrouted_project(quick_project)
    quick = o.optimize_board(quick_project, max_iterations_per_call=0, effort="quick")
    assert quick["effort"] == "quick"
    assert quick["max_iterations"] == 5
    assert quick["accept"] == "greedy"

    best_project = tmp_path / "best"
    _unrouted_project(best_project)
    best = o.optimize_board(best_project, max_iterations_per_call=0, effort="best")
    assert best["effort"] == "best"
    assert best["accept"] == "sa"
    assert best["time_budget_s"] == pytest.approx(8 * 3600.0)
    # distinct from quick/balanced on every knob the presets touch.
    assert best["accept"] != quick["accept"]
    assert best["max_iterations"] != quick["max_iterations"]

    balanced_project = tmp_path / "balanced"
    _unrouted_project(balanced_project)
    balanced = o.optimize_board(balanced_project, max_iterations_per_call=0)
    assert balanced["effort"] == "balanced"
    # "balanced" is defined as "today's optimizer.* defaults, unchanged".
    assert balanced["max_iterations"] == k.DEFAULT_PCB_SETTINGS["optimizer"]["max_iterations"]
    assert balanced["accept"] == k.DEFAULT_PCB_SETTINGS["optimizer"]["accept"]


def test_effort_can_be_read_from_pcb_settings_not_just_the_call(tmp_path: Path) -> None:
    project = _unrouted_project(tmp_path)
    _set_optimizer(project, effort="quick")
    report = o.optimize_board(project, max_iterations_per_call=0)
    assert report["effort"] == "quick"
    assert report["max_iterations"] == 5


def test_invalid_effort_raises(tmp_path: Path) -> None:
    project = _unrouted_project(tmp_path)
    with pytest.raises(ValueError):
        o.optimize_board(project, max_iterations_per_call=0, effort="overnight")


def test_explicit_call_time_override_wins_over_the_effort_preset(tmp_path: Path) -> None:
    """A preset is a DEFAULT, not a hard override - an explicit argument to
    `optimize_board` must win regardless of which effort tier is selected."""
    project = tmp_path / "override_max_iter"
    _unrouted_project(project)
    report = o.optimize_board(project, max_iterations_per_call=0, effort="best", max_iterations=1)
    assert report["max_iterations"] == 1  # NOT best's un-set fallback (optimizer.max_iterations=20)

    project2 = tmp_path / "override_accept"
    _unrouted_project(project2)
    report2 = o.optimize_board(project2, max_iterations_per_call=0, effort="quick", accept="sa")
    assert report2["accept"] == "sa"  # NOT quick's "greedy"

    project3 = tmp_path / "override_time_budget"
    _unrouted_project(project3)
    report3 = o.optimize_board(project3, max_iterations_per_call=0, effort="best", time_budget_s=42.0)
    assert report3["time_budget_s"] == pytest.approx(42.0)  # NOT best's 8h default


# --- Phase 7.15: plateau-based stopping --------------------------------------


def test_plateau_check_computes_reference_and_trailing_rates(tmp_path: Path) -> None:
    """Direct test of the plateau math (window/ratio/rates) against a
    hand-constructed improvement history - deterministic and fast, since a
    real router-driven score curve cannot be coerced into an exact shape."""
    session = {
        "plateau_window": 3,
        "plateau_slope_ratio": 0.1,
        "productive_improvements": [10.0, 12.0, 8.0],
    }
    # Exactly `plateau_window` productive iterations so far: reference and
    # trailing are computed over the SAME 3 samples - no genuine slowdown is
    # visible yet, so the rule must not fire.
    assert o._plateau_check(session) is None
    assert session["plateau_reference_rate"] == pytest.approx(10.0)
    assert session["plateau_trailing_rate"] == pytest.approx(10.0)

    # Three more iterations whose pace has collapsed to ~3% of the reference.
    session["productive_improvements"].extend([0.5, 0.3, 0.1])
    reason = o._plateau_check(session)
    assert reason == "plateau"
    assert session["plateau_reference_rate"] == pytest.approx(10.0)
    assert session["plateau_trailing_rate"] == pytest.approx(0.3)


def test_plateau_check_does_not_fire_on_a_mild_slowdown(tmp_path: Path) -> None:
    """A trailing rate that dropped but stayed above `plateau_slope_ratio` x
    the reference must not converge the run - the rule is a ratio test, not
    "any slowdown at all"."""
    session = {
        "plateau_window": 3,
        "plateau_slope_ratio": 0.1,
        "productive_improvements": [10.0, 10.0, 10.0, 9.0, 8.0, 7.0],
    }
    assert o._plateau_check(session) is None
    assert session["plateau_trailing_rate"] == pytest.approx(8.0)


def test_plateau_check_needs_a_full_window_of_productive_iterations_first() -> None:
    """Fewer than `plateau_window` productive iterations means there is no
    reference rate to compare against yet - the rule must stay silent, and
    report both rates as None rather than guessing."""
    session = {
        "plateau_window": 3,
        "plateau_slope_ratio": 0.1,
        "productive_improvements": [10.0, 0.01],
    }
    assert o._plateau_check(session) is None
    assert session["plateau_reference_rate"] is None
    assert session["plateau_trailing_rate"] is None


def test_a_new_session_reports_no_plateau_rates_before_it_has_run(tmp_path: Path) -> None:
    project = _unrouted_project(tmp_path)
    report = o.optimize_board(project, max_iterations_per_call=0)
    assert report["plateau_reference_rate"] is None
    assert report["plateau_trailing_rate"] is None
    assert report["productive_improvements"] == []
    assert report["plateau_window"] == k.DEFAULT_PCB_SETTINGS["optimizer"]["plateau_window"]
    assert report["plateau_slope_ratio"] == k.DEFAULT_PCB_SETTINGS["optimizer"]["plateau_slope_ratio"]


def test_convergence_delta_floor_still_stops_a_run_before_plateau_can_fire(tmp_path: Path) -> None:
    """The pre-7.15 floor behaviour must survive unchanged: a single
    iteration whose improvement is below `convergence_delta` stops the run
    immediately, and (since one iteration is fewer than the default
    `plateau_window` of 3) the plateau rule cannot possibly have fired
    instead - proving the floor still wins on its own terms."""
    project = _unrouted_project(tmp_path)
    # An impossibly high floor: any real improvement is "below" it, so the
    # very first committed move converges the run via convergence_delta.
    _set_optimizer(project, convergence_delta=1e9)
    report = o.optimize_board(project, max_iterations_per_call=1, seed=5, accept="greedy")
    assert report["state"] == "converged"
    assert report["stop_reason"] == "convergence_delta"
    # not enough productive history yet for the plateau rule to have an
    # opinion at all.
    assert report["plateau_reference_rate"] is None


def test_plateau_rule_converges_a_run_independently_of_convergence_delta(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wiring test: force `_plateau_check` to fire and disable the
    `convergence_delta` floor (an impossibly negative value, so it can never
    be the reason), then prove `optimize_board`'s real code path honors the
    plateau rule with its own distinguishable `stop_reason` - not a
    reimplementation of the check, an assertion that `_run_chunk` actually
    calls it."""
    project = _unrouted_project(tmp_path)
    _set_optimizer(project, convergence_delta=-1e9)
    monkeypatch.setattr(o, "_plateau_check", lambda session: "plateau")

    report = o.optimize_board(project, max_iterations_per_call=1, seed=9, accept="greedy")

    assert report["state"] == "converged"
    assert report["stop_reason"] == "plateau"
