"""Tests for the M5 whole-board lazy window tier (`_ObstacleIndex`,
`_LazyBlockedSet`, `_FineWindow(lazy=True)`, `_route_wide_lazy`).

The tier's whole correctness story is ONE claim: a lazily-evaluated window's
blocked sets are EQUAL, cell for cell, to what the eager obstacle->cell
rasterization would have produced - it is the same predicate evaluated in the
other loop order. Everything else (bigger windows are now affordable, a
whole-board search can find a detour the capped ladder cannot) follows from that
without any change to the cost model, the A*, or the self-check.

So these tests prove, in order:

1. PARITY of the blocked sets themselves, cell for cell, over a mixed obstacle
   scenario (segment / point / edge / via-transparent zone / foreign-layer
   copper) - the load-bearing test.
2. PARITY of the resulting A* geometry (same path, byte for byte) from a lazy
   vs an eager window.
3. PARITY under MUTATION (`add_obstacle` / `remove_obstacle`, the step-4 rip-up
   path) - a lazy window must stay equal to the eager one after copper is added
   and removed again.
4. CAPABILITY: `_route_wide_lazy` routes a connection whose only legal path
   leaves EVERY `_route_attempts` rung's window (a long detour "the wrong way"
   round a wall) - the failure mode the 60mm span cap created, which neither the
   ladder (span-capped) nor the hierarchical tier (chunks follow the coarse
   path, which does not go that way) can reach.
5. GATING: the tier never engages for a connection that already routes.
6. DETERMINISM: repeated wide-lazy routes are identical.
"""

from __future__ import annotations

import kicad_router_tool as rt
from synthetic_board import write_critical_nets_project


_TRACK_WIDTH = 0.2
_CLEARANCE = 0.2
_VIA_DIAMETER = 0.6
_TRACK_HALF = _TRACK_WIDTH / 2.0
_VIA_RADIUS = _VIA_DIAMETER / 2.0
_ROUTABLE_LAYERS = ["F.Cu", "B.Cu"]
_LAYER_TYPES = {"F.Cu": "signal", "B.Cu": "signal"}
_RULES = {"clearance": _CLEARANCE, "edge_clearance": _CLEARANCE,
          "track_width": _TRACK_WIDTH, "via_diameter": _VIA_DIAMETER, "via_drill": 0.3}


def _mixed_obstacles() -> list:
    """One of every obstacle shape the window model distinguishes, so the parity
    test exercises every branch of `obstacle_cells` / `_lazy_cell_blocked`."""
    zone_pts = [(6.0, 1.0), (11.0, 1.0), (11.0, 6.0), (6.0, 6.0)]
    return [
        # plain two-layer track segment
        rt._Obst("seg", "OTHER", frozenset(_ROUTABLE_LAYERS), 0.25, 3.0, -2.0, 3.0, 8.0),
        # single-layer segment (must block F.Cu only)
        rt._Obst("seg", "OTHER", frozenset(["F.Cu"]), 0.3, 0.0, 4.0, 14.0, 4.0),
        # point obstacle (via / pad)
        rt._Obst("pt", "OTHER", frozenset(_ROUTABLE_LAYERS), 0.4, 8.5, 9.0, 8.5, 9.0,
                 is_pad=True),
        # board edge (uses edge_clearance, and is NOT same-net-exempt)
        rt._Obst("edge", "", frozenset(_ROUTABLE_LAYERS), 0.05, -1.0, -3.0, 16.0, -3.0,
                 is_edge=True),
        # via-transparent power plane: blocks TRACKS on its layer, never vias
        rt._Obst("zone", "GND", frozenset(["B.Cu"]), 0.0, 6.0, 1.0, 6.0, 1.0,
                 raster=rt._FillRaster(zone_pts), pts=zone_pts, via_transparent=True),
        # same-net copper (must be free in BOTH windows)
        rt._Obst("seg", "TARGET", frozenset(_ROUTABLE_LAYERS), 0.25, 1.0, 9.0, 13.0, 9.0),
    ]


def _both_windows(grid: float = 0.2, obstacles=None):
    """An eager and a lazy `_FineWindow` over the same region/obstacles."""
    obstacles = _mixed_obstacles() if obstacles is None else obstacles
    args = (-1.0, -4.0, 16.0, 11.0, grid, _ROUTABLE_LAYERS, _LAYER_TYPES, "TARGET")
    eager = rt._FineWindow(*args)
    lazy = rt._FineWindow(*args, lazy=True)
    for w in (eager, lazy):
        w.build(obstacles, _TRACK_HALF, _VIA_RADIUS, _CLEARANCE, _CLEARANCE)
    return eager, lazy, obstacles


def _assert_windows_agree(eager: rt._FineWindow, lazy: rt._FineWindow) -> None:
    """Every cell of every layer (plus the via set) must agree exactly."""
    assert (eager.cols, eager.rows) == (lazy.cols, lazy.rows)
    mismatches: list[tuple] = []
    blocked_seen = {"track": 0, "via": 0}
    for iy in range(eager.rows):
        for ix in range(eager.cols):
            cell = (ix, iy)
            for layer in _ROUTABLE_LAYERS:
                e = cell in eager.blocked_track[layer]
                l = cell in lazy.blocked_track[layer]
                if e != l:
                    mismatches.append((cell, layer, e, l))
                blocked_seen["track"] += 1 if e else 0
            ev = cell in eager.blocked_via
            lv = cell in lazy.blocked_via
            if ev != lv:
                mismatches.append((cell, "VIA", ev, lv))
            blocked_seen["via"] += 1 if ev else 0
    assert not mismatches, f"lazy/eager blocked-set mismatch: {mismatches[:8]}"
    # Guard against a vacuous pass (both windows empty of obstacles).
    assert blocked_seen["track"] > 0 and blocked_seen["via"] > 0, blocked_seen


def test_lazy_blocked_sets_match_eager_cell_for_cell():
    """(1) The load-bearing parity test: same predicate, other loop order."""
    eager, lazy, _ = _both_windows()
    _assert_windows_agree(eager, lazy)


def test_lazy_zone_is_via_transparent_like_eager():
    """The plane anti-pad model must survive the loop inversion: cells inside the
    via-transparent GND pour are track-blocked on B.Cu but NOT via-blocked."""
    _eager, lazy, _ = _both_windows()
    # Inside the zone polygon AND clear of every other obstacle's reach, so the
    # zone is the only thing that could block this cell.
    inside = lazy.cell_of(9.5, 2.0)
    assert inside in lazy.blocked_track["B.Cu"]
    assert inside not in lazy.blocked_via
    assert inside not in lazy.blocked_track["F.Cu"]


def test_lazy_same_net_copper_is_free():
    """Same-net copper is free in a lazy window exactly as in an eager one."""
    _eager, lazy, _ = _both_windows()
    on_own_track = lazy.cell_of(7.0, 9.0)
    assert on_own_track not in lazy.blocked_track["F.Cu"]


def test_lazy_iteration_materializes_the_same_set():
    """`_LazyBlockedSet` iteration (what the numpy backend uses to build dense
    arrays) must enumerate exactly the eager set - otherwise cpu/numpy parity
    would silently break on a lazy window."""
    eager, lazy, _ = _both_windows()
    for layer in _ROUTABLE_LAYERS:
        assert set(lazy.blocked_track[layer]) == eager.blocked_track[layer]
    assert set(lazy.blocked_via) == eager.blocked_via


def test_lazy_and_eager_astar_find_the_same_path():
    """(2) Identical blocked sets => identical geometry, not merely equal cost."""
    eager, lazy, _ = _both_windows()
    paths = []
    for win in (eager, lazy):
        s = win.nearest_free(0.0, 0.0, _ROUTABLE_LAYERS, max_ring=max(win.cols, win.rows))
        g = win.nearest_free(14.0, 7.0, _ROUTABLE_LAYERS, max_ring=max(win.cols, win.rows))
        paths.append(rt._fine_astar(win, "signal", rt._Weights({}, 1.0), {}, {},
                                    s, _ROUTABLE_LAYERS, g, set(_ROUTABLE_LAYERS),
                                    None, None))
    assert paths[0] is not None, "fixture must be routable for this to mean anything"
    assert paths[0] == paths[1]


def test_backend_parity_holds_on_a_lazy_window():
    """7.8's parity gate must still hold on a LAZY window: the numpy wavefront
    materializes the blocked sets by iterating them, so it sees exactly the same
    obstacle model the cpu A* queries cell by cell, and must return the same
    path byte for byte."""
    _eager, lazy, _ = _both_windows()
    s = lazy.nearest_free(0.0, 0.0, _ROUTABLE_LAYERS, max_ring=max(lazy.cols, lazy.rows))
    g = lazy.nearest_free(14.0, 7.0, _ROUTABLE_LAYERS, max_ring=max(lazy.cols, lazy.rows))
    args = (lazy, "signal", rt._Weights({}, 1.0), {}, {}, s, _ROUTABLE_LAYERS,
            g, set(_ROUTABLE_LAYERS), None, None)
    cpu_path = rt._fine_search("cpu", *args)
    numpy_path = rt._fine_search("numpy", *args)
    assert cpu_path is not None
    assert cpu_path == numpy_path


def test_lazy_add_and_remove_obstacle_matches_eager():
    """(3) The step-4 rip-up path: a lazy window must track incremental
    add/remove of copper exactly as the ref-counted eager window does."""
    eager, lazy, _ = _both_windows()
    extra = rt._Obst("seg", "RIPPABLE", frozenset(_ROUTABLE_LAYERS), 0.3,
                     12.0, -2.0, 12.0, 8.0, owner=7)
    for w in (eager, lazy):
        w.add_obstacle(extra)
    _assert_windows_agree(eager, lazy)
    # the added copper must actually have changed something (non-vacuous)
    assert lazy.cell_of(12.0, 3.0) in lazy.blocked_track["F.Cu"]
    for w in (eager, lazy):
        w.remove_obstacle(extra)
    _assert_windows_agree(eager, lazy)
    assert lazy.cell_of(12.0, 3.0) not in lazy.blocked_track["F.Cu"]


def test_lazy_build_does_not_materialize_cells():
    """The reason the cap can be lifted at all: building a lazy window costs
    O(obstacles), not O(window area) - nothing is evaluated until asked."""
    _eager, lazy, _ = _both_windows()
    assert lazy._index is not None
    assert lazy.blocked_via._cache == {}
    assert all(lazy.blocked_track[l]._cache == {} for l in _ROUTABLE_LAYERS)
    lazy.cell_of(0, 0) in lazy.blocked_via  # noqa: B015 - force one evaluation
    assert len(lazy.blocked_via._cache) == 1


# --------------------------------------------------------------------------- #
# (4) The capability test: a detour that leaves EVERY ladder rung's window.
#
# A long wall at x=40 spans the whole board except for a gap far to the NORTH
# (y in [95, 105]). The connection runs (5, 0) -> (75, 0): a 70mm airline, so
# every `_route_attempts` rung's window is centred on y=0 and reaches at most
# `_MAX_WINDOW_SPAN_MM` (60mm) of margin - i.e. up to y=60, never as far as the
# gap at y>=95. The ladder is therefore unreachable at every rung no matter how
# fine or coarse the grid, and the coarse global path does not go north either.
# A whole-board lazy window contains the gap and finds the detour.
# --------------------------------------------------------------------------- #

_WIDE_BOARD_BBOX = (-10.0, -20.0, 90.0, 130.0)
_WIDE_FROM = (5.0, 0.0)
_WIDE_TO = (75.0, 0.0)
_GAP_LO, _GAP_HI = 95.0, 105.0


def _wide_obstacles() -> list:
    return [
        rt._Obst("seg", "WALL", frozenset(_ROUTABLE_LAYERS), 0.5, 40.0, -60.0, 40.0, _GAP_LO),
        rt._Obst("seg", "WALL", frozenset(_ROUTABLE_LAYERS), 0.5, 40.0, _GAP_HI, 40.0, 170.0),
    ]


def _wide_ctx() -> dict:
    return {
        "board_bbox": _WIDE_BOARD_BBOX, "board_min": (_WIDE_BOARD_BBOX[0], _WIDE_BOARD_BBOX[1]),
        "routable_layers": _ROUTABLE_LAYERS, "routable_set": set(_ROUTABLE_LAYERS),
        "layer_types": _LAYER_TYPES, "grid": 1.0, "min_grid_mm": 0.25, "max_grid_mm": 1.0,
        "base_margin": 8.0, "rules": _RULES, "track_half": _TRACK_HALF,
        "via_radius": _VIA_RADIUS, "weights": rt._Weights({}, 1.0), "layer_purpose": {},
        "directions": {}, "backend": "cpu", "plane_step": 0.0, "attachment_via_cost": 0.0,
        "max_window_nodes": rt._MAX_WINDOW_NODES, "coarse_grid": 2.0, "coarse_min": (0.0, 0.0),
        "tw": {}, "neck_down": {"enabled": False},
    }


def _wide_conn() -> dict:
    return {"net": "TARGET",
            "from": {"x": _WIDE_FROM[0], "y": _WIDE_FROM[1], "layers": _ROUTABLE_LAYERS},
            "to": {"x": _WIDE_TO[0], "y": _WIDE_TO[1], "layers": _ROUTABLE_LAYERS}}


def test_every_ladder_rung_misses_the_far_detour():
    """Fixture sanity: prove the ordinary span-capped ladder genuinely cannot
    reach the gap, so the capability test below is not measuring nothing."""
    attempts = rt._route_attempts(_WIDE_FROM, _WIDE_TO, _WIDE_BOARD_BBOX, 1.0, 0.25,
                                  1.0, 8.0, 2, rt._MAX_WINDOW_NODES)
    assert attempts
    obstacles = _wide_obstacles()
    for margin, grid in attempts:
        minx = max(min(_WIDE_FROM[0], _WIDE_TO[0]) - margin, _WIDE_BOARD_BBOX[0] - 1.0)
        miny = max(min(_WIDE_FROM[1], _WIDE_TO[1]) - margin, _WIDE_BOARD_BBOX[1] - 1.0)
        maxx = min(max(_WIDE_FROM[0], _WIDE_TO[0]) + margin, _WIDE_BOARD_BBOX[2] + 1.0)
        maxy = min(max(_WIDE_FROM[1], _WIDE_TO[1]) + margin, _WIDE_BOARD_BBOX[3] + 1.0)
        assert maxy < _GAP_LO, f"rung margin={margin} unexpectedly reaches the gap"
        win = rt._FineWindow(minx, miny, maxx, maxy, grid, _ROUTABLE_LAYERS,
                             _LAYER_TYPES, "TARGET")
        win.build(obstacles, _TRACK_HALF, _VIA_RADIUS, _CLEARANCE, _CLEARANCE)
        ring = max(win.cols, win.rows)
        s = win.nearest_free(*_WIDE_FROM, _ROUTABLE_LAYERS, max_ring=ring)
        g = win.nearest_free(*_WIDE_TO, _ROUTABLE_LAYERS, max_ring=ring)
        path = rt._fine_astar(win, "signal", rt._Weights({}, 1.0), {}, {}, s,
                              _ROUTABLE_LAYERS, g, set(_ROUTABLE_LAYERS), None, None)
        assert path is None, f"rung (margin={margin}, grid={grid}) unexpectedly routed"


def test_wide_lazy_routes_the_far_detour():
    """(4) The headline: the whole-board lazy window finds the northern detour
    the span-capped ladder provably cannot see."""
    got = rt._route_wide_lazy(_wide_ctx(), _wide_conn(), "TARGET", "signal",
                              _WIDE_FROM, _WIDE_TO, _ROUTABLE_LAYERS,
                              set(_ROUTABLE_LAYERS), _wide_obstacles(), {}, None, None, None)
    assert got is not None, "whole-board lazy window failed to find the detour"
    rec, segments, _vias, win = got
    assert segments
    assert rec["routed"] is True
    # The window really is whole-board, i.e. wider than the span the ordinary
    # ladder is capped at - that cap is exactly what this tier lifts.
    span = max(_WIDE_BOARD_BBOX[2] - _WIDE_BOARD_BBOX[0],
               _WIDE_BOARD_BBOX[3] - _WIDE_BOARD_BBOX[1])
    assert span > rt._MAX_WINDOW_SPAN_MM
    assert rec["wide_lazy_window"]["rows"] * win.grid >= span - 2 * win.grid
    # It really did go north through the gap, not through the wall.
    assert max(max(s["y1"], s["y2"]) for s in segments) > _GAP_LO
    assert win.grid <= 1.0


def test_wide_lazy_is_deterministic():
    """(6) Same inputs, byte-identical geometry, every call."""
    runs = [rt._route_wide_lazy(_wide_ctx(), _wide_conn(), "TARGET", "signal",
                                _WIDE_FROM, _WIDE_TO, _ROUTABLE_LAYERS,
                                set(_ROUTABLE_LAYERS), _wide_obstacles(), {}, None, None, None)
            for _ in range(2)]
    assert runs[0] is not None and runs[1] is not None
    assert runs[0][1] == runs[1][1]
    assert runs[0][2] == runs[1][2]


def test_wide_lazy_returns_none_when_truly_sealed():
    """Honesty gate: a topologically SEALED endpoint (no gap anywhere) must still
    fail. A bigger window proves impossibility faster; it does not invent a path.
    This is the kiln case (see NETCLASS_PLAN.md item 10)."""
    sealed = [rt._Obst("seg", "WALL", frozenset(_ROUTABLE_LAYERS), 0.5,
                       40.0, -60.0, 40.0, 170.0)]
    got = rt._route_wide_lazy(_wide_ctx(), _wide_conn(), "TARGET", "signal",
                              _WIDE_FROM, _WIDE_TO, _ROUTABLE_LAYERS,
                              set(_ROUTABLE_LAYERS), sealed, {}, None, None, None)
    assert got is None


def test_wide_lazy_never_engages_for_a_routable_connection(tmp_path, monkeypatch):
    """(5) Gating: strictly a last resort. Every connection on a normal project
    routes without the tier ever being called - so no existing route can change."""
    components = [
        {"ref": "A1", "footprint": "synthetic:PAD", "x": 0.0, "y": 0.0,
         "pads": [(1, 0.0, 0.0, 0.3, 0.3, "LINK")]},
        {"ref": "A2", "footprint": "synthetic:PAD", "x": 5.0, "y": 0.0,
         "pads": [(1, 0.0, 0.0, 0.3, 0.3, "LINK")]},
    ]
    paths = write_critical_nets_project(tmp_path, "synthetic", components)

    calls = {"n": 0}
    real = rt._route_wide_lazy

    def counting(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(rt, "_route_wide_lazy", counting)
    # Force the in-process serial path: a monkeypatch on this process's module
    # object is invisible to a multiprocessing worker (same reason as the
    # hierarchical tier's gating test).
    monkeypatch.setattr(rt, "_resolve_workers", lambda settings: 1)

    res = rt.route_nets(str(paths["project"]), write=False)
    routed = [c for c in res.get("connections", []) if c.get("routed")]
    assert routed, "fixture project produced no routed connections"
    assert calls["n"] == 0, "wide-lazy tier engaged for an ordinarily-routable board"
