"""Tests for the hierarchical (multi-window) last-resort routing tier
(`_route_hierarchical` / `_hier_world_waypoints` in `kicad_router_tool.py`).

This tier only engages after `_route_attempts`'s ENTIRE ladder (every margin/
grid rung, up to the whole-board `_MAX_WINDOW_SPAN_MM` cap) has already failed
for a connection. Two things need proving:

1. GATING: it must never run for a connection that already routes on the
   ordinary ladder - verified against the real synthetic project fixture with
   a plain, trivially-routable connection, by monkeypatching
   `_route_hierarchical` with a call-counting wrapper and asserting it is
   never invoked while every connection still routes.

2. MECHANICS (stitching / end-to-end self-check / determinism): exercised
   directly against `_route_hierarchical`, using a small hand-built obstacle
   scenario - a "wall" with a narrow gap positioned and sized so that every
   `_route_attempts` rung (tested independently below, with the node budget
   patched down to force early grid coarsening) misses the gap, while a
   single small fine-grid window centered on the gap finds it easily. This
   reproduces, in miniature and under full control, the exact "coarse rung
   steps over a real but narrow channel" mechanism the tier exists to work
   around (see the class-level comment on `_route_hierarchical` in
   `kicad_router_tool.py`).
"""

from __future__ import annotations

import copy

import kicad_router_tool as rt
from synthetic_board import write_critical_nets_project


# --------------------------------------------------------------------------- #
# Shared "wall with a narrow gap" fixture for the mechanics tests.
#
# Two thick "WALL"-net segments form a near-vertical barrier at x=15 spanning
# y in (-1000, 1000) except for a gap in y in [11.05, 12.95]. At the node
# budget patched into these tests (3000), every `_route_attempts` rung for a
# (0,0)->(30,0) connection is forced onto a grid coarse enough (>=0.7355mm)
# that NO grid node lands inside that 1.9mm-wide gap (verified below) - so the
# ladder is unreachable_in_window at every rung, even though a real DRC-legal
# path exists (it must detour up through the gap around y=12). A small
# fine-grid (0.2mm) window centered on the gap has plenty of nodes inside it
# and finds the detour trivially - this is what `_route_hierarchical` is for.
# --------------------------------------------------------------------------- #

_TRACK_WIDTH = 0.2
_CLEARANCE = 0.2
_VIA_DIAMETER = 0.6
_VIA_DRILL = 0.3
_TRACK_HALF = _TRACK_WIDTH / 2.0
_VIA_RADIUS = _VIA_DIAMETER / 2.0
_WALL_HALF = 0.3
_GAP_LO, _GAP_HI = 11.05, 12.95  # y-range of the passable slot in the wall
_ROUTABLE_LAYERS = ["F.Cu", "B.Cu"]
_ROUTABLE_SET = set(_ROUTABLE_LAYERS)
_LAYER_TYPES = {"F.Cu": "signal", "B.Cu": "signal"}
_RULES = {"clearance": _CLEARANCE, "edge_clearance": _CLEARANCE,
          "track_width": _TRACK_WIDTH, "via_diameter": _VIA_DIAMETER, "via_drill": _VIA_DRILL}
_FROM_XY = (0.0, 0.0)
_TO_XY = (30.0, 0.0)
# Node budget small enough that every `_route_attempts` rung for this 30mm
# connection is forced to coarsen well past the 1.9mm gap width (see the
# module docstring above); independently confirmed not to affect a leg-sized
# hierarchical sub-window (~14mm span), which always searches at the full
# base grid regardless of this budget.
_SHRUNK_BUDGET = 3000


def _wall_obstacles() -> list:
    return [
        rt._Obst("seg", "WALL", frozenset(_ROUTABLE_LAYERS), _WALL_HALF, 15, -1000, 15, _GAP_LO),
        rt._Obst("seg", "WALL", frozenset(_ROUTABLE_LAYERS), _WALL_HALF, 15, _GAP_HI, 15, 1000),
    ]


def _hier_ctx() -> dict:
    return {
        "coarse_grid": 1.0, "coarse_min": (0.0, 0.0),
        "routable_layers": _ROUTABLE_LAYERS, "routable_set": _ROUTABLE_SET,
        "layer_types": _LAYER_TYPES, "board_bbox": (-1000.0, -1000.0, 1000.0, 1000.0),
        "grid": 0.2, "rules": _RULES, "track_half": _TRACK_HALF, "via_radius": _VIA_RADIUS,
        "weights": rt._Weights({}, 1.0), "layer_purpose": {}, "directions": {}, "backend": "cpu",
        "plane_step": 0.0, "attachment_via_cost": 0.0, "max_window_nodes": rt._MAX_WINDOW_NODES,
        "tw": {},
    }


def _detour_gconn() -> dict:
    """A fabricated global-stage result whose coarse_path arcs up through the
    gap at x~15 (world coords, coarse_grid=1.0, coarse_min=(0,0) so a coarse
    cell's world point is simply cell+0.5) - exactly the shape the real
    coarse (2mm) global stage would hand `_route_hierarchical` for a
    connection that must detour around an obstacle."""
    world_path = [(0, 0), (5, 2), (10, 6), (12, 10), (15, 12), (18, 10),
                  (20, 6), (25, 2), (30, 0)]
    coarse_path = [[round(x - 0.5), round(y - 0.5), "F.Cu"] for (x, y) in world_path]
    return {"candidates": [{"coarse_path": coarse_path}]}


def test_wall_gap_defeats_every_route_attempts_rung():
    """Sanity-check the fixture itself: with the node budget patched down,
    every ordinary `_route_attempts` (margin, grid) rung for the (0,0)->
    (30,0) connection fails to find a path through the wall - confirming the
    scenario genuinely needs the hierarchical tier (not just a weak test)."""
    board_bbox = (-1000.0, -1000.0, 1000.0, 1000.0)
    attempts = rt._route_attempts(_FROM_XY, _TO_XY, board_bbox, 0.2, 0.05, 1.0, 8.0, 2, _SHRUNK_BUDGET)
    assert attempts, "expected at least one ladder rung"
    obstacles = _wall_obstacles()
    for margin, grid in attempts:
        minx = _FROM_XY[0] - margin
        maxx = _TO_XY[0] + margin
        miny = -margin
        maxy = margin
        win = rt._FineWindow(minx, miny, maxx, maxy, grid, _ROUTABLE_LAYERS, _LAYER_TYPES, "TARGET")
        win.build(obstacles, _TRACK_HALF, _VIA_RADIUS, _CLEARANCE, _CLEARANCE)
        s_cell = win.nearest_free(*_FROM_XY, _ROUTABLE_LAYERS, max_ring=max(win.cols, win.rows))
        g_cell = win.nearest_free(*_TO_XY, _ROUTABLE_LAYERS, max_ring=max(win.cols, win.rows))
        path = rt._fine_astar(win, "signal", rt._Weights({}, 1.0), {}, {}, s_cell, _ROUTABLE_LAYERS,
                              g_cell, _ROUTABLE_SET, None, None)
        assert path is None, f"expected rung (margin={margin}, grid={grid}) to fail, but it routed"


def test_hierarchical_routes_where_the_full_ladder_cannot():
    """The core positive case: `_route_hierarchical` succeeds via a chained
    sequence of small fine-grid sub-windows even though the fixture above
    proves the ordinary ladder cannot solve the same connection at any rung."""
    obstacles = _wall_obstacles()
    ctx = _hier_ctx()
    gconn = _detour_gconn()
    result = rt._route_hierarchical(
        ctx, "TARGET", "signal", _FROM_XY, _TO_XY, _ROUTABLE_LAYERS, _ROUTABLE_SET,
        obstacles, gconn, None, None, None)
    assert result is not None, "hierarchical tier should have found the detour through the gap"
    rec, segments, vias = result
    assert rec["routed"] is True
    assert rec["hierarchical"] is True
    # The detour is decimated into several legs (chunk span ~8mm over a path
    # much longer than the 30mm airline) - a single leg would just be attempt
    # 1 again, so >=2 legs is what actually exercises stitching.
    assert rec["hierarchical_legs"] >= 2
    assert segments, "expected at least one emitted segment"


def test_hierarchical_stitched_result_self_checks_clean_end_to_end():
    """The concatenated segments/vias from every leg, run through the SAME
    `_self_check` any other connection's route is proven against, must show
    zero violations - including at the seams between legs (same-net copper is
    always free to a later leg, so legs never falsely violate each other)."""
    obstacles = _wall_obstacles()
    ctx = _hier_ctx()
    gconn = _detour_gconn()
    result = rt._route_hierarchical(
        ctx, "TARGET", "signal", _FROM_XY, _TO_XY, _ROUTABLE_LAYERS, _ROUTABLE_SET,
        obstacles, gconn, None, None, None)
    assert result is not None
    rec, segments, vias = result
    assert rec["self_check"] == {"passed": True, "violation_count": 0}
    violations = rt._self_check("TARGET", segments, vias, obstacles, _RULES, _VIA_RADIUS)
    assert violations == []


def test_hierarchical_is_deterministic():
    """Repeated calls with identical inputs produce byte-identical geometry -
    a pure function of (from_xy, to_xy, coarse path, obstacle/layer state),
    with no iteration-order or randomness dependence."""
    obstacles = _wall_obstacles()
    ctx = _hier_ctx()
    gconn = _detour_gconn()
    results = []
    for _ in range(3):
        result = rt._route_hierarchical(
            ctx, "TARGET", "signal", _FROM_XY, _TO_XY, _ROUTABLE_LAYERS, _ROUTABLE_SET,
            copy.deepcopy(obstacles), copy.deepcopy(gconn), None, None, None)
        assert result is not None
        results.append(result)
    first_rec, first_segs, first_vias = results[0]
    for rec, segs, vias in results[1:]:
        assert rec == first_rec
        assert segs == first_segs
        assert vias == first_vias


def test_hierarchical_returns_none_without_a_coarse_path():
    """No coarse path to chain sub-windows along (e.g. the global stage never
    produced one for this connection) -> the tier declines cleanly rather
    than guessing; `_route_one` falls through to its ordinary
    `unreachable_in_window` report."""
    obstacles = _wall_obstacles()
    ctx = _hier_ctx()
    assert rt._route_hierarchical(
        ctx, "TARGET", "signal", _FROM_XY, _TO_XY, _ROUTABLE_LAYERS, _ROUTABLE_SET,
        obstacles, None, None, None, None) is None
    assert rt._route_hierarchical(
        ctx, "TARGET", "signal", _FROM_XY, _TO_XY, _ROUTABLE_LAYERS, _ROUTABLE_SET,
        obstacles, {"candidates": []}, None, None, None) is None
    assert rt._route_hierarchical(
        ctx, "TARGET", "signal", _FROM_XY, _TO_XY, _ROUTABLE_LAYERS, _ROUTABLE_SET,
        obstacles, {"candidates": [{"coarse_path": []}]}, None, None, None) is None


def test_hierarchical_tier_never_engages_for_an_already_routing_connection(tmp_path, monkeypatch):
    """GATING: on a plain synthetic project where every connection routes on
    the ordinary ladder's first attempt, `_route_hierarchical` must never be
    called at all - proving the new tier is strictly a last resort and never
    perturbs a connection that already succeeds (byte-identical-parity
    guarantee for every net that isn't exhausting the ladder)."""
    # Two components, one pad each, sharing a single net "LINK" - a plain,
    # obstacle-free, short two-node connection with nothing nearby to block
    # it, so it must route on attempt 1.
    components = [
        {"ref": "A1", "footprint": "synthetic:PAD", "x": 0.0, "y": 0.0,
         "pads": [(1, 0.0, 0.0, 0.3, 0.3, "LINK")]},
        {"ref": "A2", "footprint": "synthetic:PAD", "x": 5.0, "y": 0.0,
         "pads": [(1, 0.0, 0.0, 0.3, 0.3, "LINK")]},
    ]
    paths = write_critical_nets_project(tmp_path, "synthetic", components)

    calls = []
    original = rt._route_hierarchical

    def counting_wrapper(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(rt, "_route_hierarchical", counting_wrapper)
    # Force the in-process serial path: a monkeypatch on this process's module
    # object is invisible to a multiprocessing worker (a fresh, unpatched
    # import in its own process), so the call-counting assertion below would
    # be meaningless under the parallel pool.
    monkeypatch.setattr(rt, "_resolve_workers", lambda settings: 1)

    result = rt.route_nets(str(paths["project"]), write=False)
    assert result["connections"], "expected at least one connection to route"
    for conn in result["connections"]:
        assert conn.get("routed") is True, conn.get("failure")
        assert conn.get("hierarchical") is None
    assert calls == [], "hierarchical tier must not run for connections that route on the normal ladder"
