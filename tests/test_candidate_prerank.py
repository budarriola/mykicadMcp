"""Phase 7.19.2 — cheap pre-ranking of the global stage's alternate candidates.

WHAT THE CODE ACTUALLY DID BEFORE THIS PHASE (checked, because the phase sketch
described something else): 7.3a's `_make_candidates` produces 1–3 ranked coarse
candidates per connection, and detailed routing consumed candidate **0 and only
candidate 0** — `_corridor_from_global` and `_hier_world_waypoints` both indexed
`["candidates"][0]` unconditionally. There was no "try them in order until one
succeeds" loop to make cheaper. `test_pre_phase_flow_used_only_candidate_zero`
pins that as a fact rather than a recollection.

So the phase delivers two coupled things, both behind
`autorouter.candidate_fallback.enabled` (default off ⇒ byte-identical):
  * the fallback that makes alternates reachable at all, and
  * the cheap gate — `_prerank_candidates`, which decides from the coarse cost
    alone (no grid, no window, no A*) whether an alternate is worth a full
    windowed search.

The gate is the deliverable, so it is tested directly and exhaustively, and the
control flow is tested by recording exactly which candidate indices ever reach
detailed routing.
"""

from __future__ import annotations

import kicad_router_tool as router


def _cand(est, layers_seq, cx=0):
    """A minimal candidate record in the shape `_make_candidates` emits."""
    return {"est_cost_milli": est,
            "coarse_path": [[cx + i, 0, l] for i, l in enumerate(layers_seq)]}


def _gconn(*cands):
    return {"candidates": list(cands)}


# --------------------------------------------------------------------------- #
# The gate itself
# --------------------------------------------------------------------------- #

def test_layer_change_counting():
    assert router._candidate_layer_changes(_cand(0, ["F.Cu"] * 5)) == 0
    assert router._candidate_layer_changes(_cand(0, ["F.Cu", "F.Cu", "B.Cu"])) == 1
    assert router._candidate_layer_changes(
        _cand(0, ["F.Cu", "B.Cu", "F.Cu", "B.Cu"])) == 3
    assert router._candidate_layer_changes({"coarse_path": []}) == 0
    assert router._candidate_layer_changes({}) == 0


def test_prerank_is_coarse_cost_plus_a_fixed_via_constant():
    g = _gconn(_cand(1000, ["F.Cu"] * 4),
               _cand(1000, ["F.Cu", "B.Cu", "B.Cu", "F.Cu"]))
    ranked = router._prerank_candidates(g, {"via_penalty_milli": 7, "max_cost_ratio": 99})
    assert ranked[0]["prerank_milli"] == 1000          # 0 layer changes
    assert ranked[1]["layer_changes"] == 2
    assert ranked[1]["prerank_milli"] == 1000 + 2 * 7  # priced purely by the constant


def test_prerank_never_reorders_candidates():
    """The gate decides how far down the list is worth trying — never what is
    tried FIRST. Reordering would change which candidate gets emitted on a
    connection that succeeds today, which is exactly what must not happen."""
    g = _gconn(_cand(9000, ["F.Cu"]), _cand(10, ["F.Cu"]), _cand(20, ["F.Cu"]))
    ranked = router._prerank_candidates(g, {"max_cost_ratio": 99})
    assert [r["index"] for r in ranked] == [0, 1, 2]
    assert ranked[0]["est_cost_milli"] == 9000


def test_candidate_zero_is_always_worth():
    g = _gconn(_cand(10_000_000, ["F.Cu", "B.Cu"] * 20))
    ranked = router._prerank_candidates(g, {"max_cost_ratio": 0.0})
    assert ranked[0]["worth"] is True


def test_expensive_alternate_is_gated_out():
    g = _gconn(_cand(1000, ["F.Cu"]), _cand(1200, ["F.Cu"]), _cand(5000, ["F.Cu"]))
    ranked = router._prerank_candidates(g, {"max_cost_ratio": 1.35})
    assert [r["worth"] for r in ranked] == [True, True, False]


def test_via_constant_alone_can_gate_a_candidate_out():
    """Two candidates with the SAME coarse cost, one of which needs four layer
    changes: the per-layer-change constant is what separates them, which is the
    whole reason the estimate is not just `est_cost_milli`."""
    g = _gconn(_cand(1000, ["F.Cu"]),
               _cand(1000, ["F.Cu", "B.Cu", "F.Cu", "B.Cu", "F.Cu"]))
    loose = router._prerank_candidates(g, {"via_penalty_milli": 0, "max_cost_ratio": 1.1})
    assert loose[1]["worth"] is True
    tight = router._prerank_candidates(g, {"via_penalty_milli": 100, "max_cost_ratio": 1.1})
    assert tight[1]["worth"] is False


def test_max_candidates_caps_the_list():
    g = _gconn(_cand(1000, ["F.Cu"]), _cand(1001, ["F.Cu"]), _cand(1002, ["F.Cu"]))
    ranked = router._prerank_candidates(g, {"max_cost_ratio": 99, "max_candidates": 2})
    assert [r["worth"] for r in ranked] == [True, True, False]


def test_slack_admits_a_marginal_candidate():
    g = _gconn(_cand(1000, ["F.Cu"]), _cand(1400, ["F.Cu"]))
    assert router._prerank_candidates(g, {"max_cost_ratio": 1.35})[1]["worth"] is False
    assert router._prerank_candidates(
        g, {"max_cost_ratio": 1.35, "slack_milli": 100})[1]["worth"] is True


def test_prerank_handles_missing_and_empty_input():
    assert router._prerank_candidates(None, {}) == []
    assert router._prerank_candidates({}, {}) == []
    assert router._prerank_candidates({"candidates": []}, None) == []


def test_prerank_does_no_routing_work():
    """The estimate must be pure arithmetic over data 7.3a already emitted. If
    it ever grew a window or a search, this would fire."""
    calls = []
    real = router._FineWindow
    try:
        router._FineWindow = lambda *a, **k: calls.append(a)  # type: ignore[assignment]
        g = _gconn(_cand(1000, ["F.Cu"]), _cand(1200, ["F.Cu", "B.Cu"]))
        router._prerank_candidates(g, {})
    finally:
        router._FineWindow = real  # type: ignore[assignment]
    assert calls == []


# --------------------------------------------------------------------------- #
# Control flow: which candidates ever reach a detailed search
# --------------------------------------------------------------------------- #

def _tracking_ctx(monkeypatch, gconn, succeed_at):
    """Replace the per-candidate detailed router with a recorder, so the test
    observes exactly which candidate indices paid for a windowed A*."""
    tried: list[int] = []

    def fake(ctx, conn, obstacles, congestion, use_corridor=True, candidate_index=0):
        tried.append(candidate_index)
        routed = candidate_index == succeed_at
        return {"routed": routed, "net": conn["net"], "segments": [], "vias": [],
                "rec": {"net": conn["net"], "routed": routed}, "win": None}

    monkeypatch.setattr(router, "_route_one_candidate", fake)
    conn = {"net": "N",
            "from_point": {"x": 1.0, "y": 2.0}, "to_point": {"x": 3.0, "y": 4.0}}
    key = ("N", 1.0, 2.0, 3.0, 4.0)
    return tried, conn, {"global_by_key": {key: gconn}}


def test_pre_phase_flow_used_only_candidate_zero(monkeypatch):
    """With the feature absent from settings (the default), a FAILING connection
    still only ever detail-routes candidate 0 — the pre-7.19.2 behaviour."""
    g = _gconn(_cand(1000, ["F.Cu"]), _cand(1100, ["F.Cu"]), _cand(1200, ["F.Cu"]))
    tried, conn, ctx = _tracking_ctx(monkeypatch, g, succeed_at=99)
    out = router._route_one(ctx, conn, [], {})
    assert tried == [0]
    assert out["routed"] is False


def test_success_on_candidate_zero_never_touches_an_alternate(monkeypatch):
    """The byte-identity guarantee in executable form: when the top candidate
    works, the fallback is not merely harmless — it does not run."""
    g = _gconn(_cand(1000, ["F.Cu"]), _cand(1100, ["F.Cu"]))
    tried, conn, ctx = _tracking_ctx(monkeypatch, g, succeed_at=0)
    ctx["candidate_fallback"] = {"enabled": True}
    out = router._route_one(ctx, conn, [], {})
    assert tried == [0]
    assert out["routed"] is True
    assert "candidate_index" not in out["rec"]


def test_fallback_reaches_a_viable_alternate(monkeypatch):
    g = _gconn(_cand(1000, ["F.Cu"]), _cand(1100, ["F.Cu"]))
    tried, conn, ctx = _tracking_ctx(monkeypatch, g, succeed_at=1)
    ctx["candidate_fallback"] = {"enabled": True}
    out = router._route_one(ctx, conn, [], {})
    assert tried == [0, 1]
    assert out["routed"] is True
    assert out["rec"]["candidate_index"] == 1


def test_gate_skips_a_doomed_alternates_detailed_search(monkeypatch):
    """THE deliverable: candidate 2 is far too expensive by the cheap estimate,
    so its windowed A* never runs even though candidates 0 and 1 both failed."""
    g = _gconn(_cand(1000, ["F.Cu"]), _cand(1100, ["F.Cu"]), _cand(99_000, ["F.Cu"]))
    tried, conn, ctx = _tracking_ctx(monkeypatch, g, succeed_at=99)
    ctx["candidate_fallback"] = {"enabled": True, "max_cost_ratio": 1.35}
    out = router._route_one(ctx, conn, [], {})
    assert tried == [0, 1], "the over-budget candidate must not be detail-routed"
    assert out["rec"]["candidates_prerank_skipped"] == [2]


def test_gate_can_skip_every_alternate(monkeypatch):
    g = _gconn(_cand(1000, ["F.Cu"]), _cand(50_000, ["F.Cu"]), _cand(99_000, ["F.Cu"]))
    tried, conn, ctx = _tracking_ctx(monkeypatch, g, succeed_at=99)
    ctx["candidate_fallback"] = {"enabled": True, "max_cost_ratio": 1.35}
    router._route_one(ctx, conn, [], {})
    assert tried == [0]


def test_no_corridor_means_no_fallback(monkeypatch):
    """`use_corridor=False` calls (the rip-up free-path probe) have no corridor
    to vary, so trying another candidate would be pure wasted search."""
    g = _gconn(_cand(1000, ["F.Cu"]), _cand(1100, ["F.Cu"]))
    tried, conn, ctx = _tracking_ctx(monkeypatch, g, succeed_at=1)
    ctx["candidate_fallback"] = {"enabled": True}
    router._route_one(ctx, conn, [], {}, use_corridor=False)
    assert tried == [0]


# --------------------------------------------------------------------------- #
# The candidate index really does select a different corridor
# --------------------------------------------------------------------------- #

def test_corridor_and_waypoints_follow_the_selected_candidate():
    win = router._FineWindow(0.0, 0.0, 20.0, 20.0, 1.0, ["F.Cu"],
                             {"F.Cu": "signal"}, "N")
    win.build([], 0.1, 0.3, 0.2, 0.2)
    g = _gconn({"est_cost_milli": 10,
                "coarse_path": [[0, 0, "F.Cu"], [1, 0, "F.Cu"], [2, 0, "F.Cu"]]},
               {"est_cost_milli": 12,
                "coarse_path": [[0, 8, "F.Cu"], [1, 8, "F.Cu"], [2, 8, "F.Cu"]]})
    c0 = router._corridor_from_global(win, g, 2.0, (0.0, 0.0), 0)
    c1 = router._corridor_from_global(win, g, 2.0, (0.0, 0.0), 1)
    assert c0 and c1 and c0 != c1

    w0 = router._hier_world_waypoints(g, 2.0, (0.0, 0.0), (0.0, 0.0), (5.0, 0.0), 0)
    w1 = router._hier_world_waypoints(g, 2.0, (0.0, 0.0), (0.0, 0.0), (5.0, 0.0), 1)
    assert w0 != w1
    # Out-of-range index degrades to "no corridor", never to candidate 0.
    assert router._corridor_from_global(win, g, 2.0, (0.0, 0.0), 5) is None
    assert router._hier_world_waypoints(g, 2.0, (0.0, 0.0), (0.0, 0.0), (5.0, 0.0), 5) is None
