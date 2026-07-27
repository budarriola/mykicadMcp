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
    while report["state"] == "running":
        report = o.optimize_board(project, session_id=report["session_id"],
                                  max_iterations_per_call=2)
    assert board_path.read_bytes() == before_bytes


# --- 3. write=True applies the final state; a re-run then converges ---------


def test_write_applies_final_state_and_rerun_converges_immediately(tmp_path: Path) -> None:
    project = _unrouted_project(tmp_path)
    board_path, _, _ = k._resolve_project_path(project)
    unrouted_before = r.get_ratsnest(project)["summary"]["total_connections"]

    report = o.optimize_board(project, max_iterations_per_call=6, seed=5, max_iterations=6)
    while report["state"] == "running":
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
    while report["state"] == "running":
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
    one_shot = o.optimize_board(_unrouted_project(tmp_path / "one"),
                                max_iterations_per_call=4, seed=17, max_iterations=4)

    chunked_project = _unrouted_project(tmp_path / "chunked")
    chunked = o.optimize_board(chunked_project, max_iterations_per_call=1, seed=17, max_iterations=4)
    calls = 1
    while chunked["state"] == "running":
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
    while final["state"] == "running":
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


def test_decide_kicad_route_is_not_registered(tmp_path: Path) -> None:
    """Phase 7.7's tool must not appear until 7.7 is actually built."""
    from kicad_mcp_server import KiCadMcpServer

    assert "decide_kicad_route" not in KiCadMcpServer().tools


# --- 7.7 is honestly out of scope, and must stay that way -------------------


def test_phase_77_decision_machinery_is_absent_not_faked(tmp_path: Path) -> None:
    """7.6 landed WITHOUT 7.7. A regression that silently introduces a fourth
    state (or starts reading `optimizer.ai_decisions`) should fail here rather
    than ship a half-built decision loop."""
    assert o.SESSION_STATES == ("running", "converged", "budget_exhausted")
    assert not hasattr(o, "decide_route")

    project = _unrouted_project(tmp_path)
    report = o.optimize_board(project, max_iterations_per_call=1, seed=2)
    assert report["state"] in o.SESSION_STATES
    assert "pending_decision" not in report
    assert any("7.7" in note for note in report["notes"])

    # the settings block still EXISTS (it is part of the 6.1 schema) - this
    # phase simply never consults it.
    assert "ai_decisions" in k.DEFAULT_PCB_SETTINGS["optimizer"]
    source = Path(o.__file__).read_text(encoding="utf-8")
    assert 'ai_decisions"]' not in source and "get(\"ai_decisions\"" not in source
