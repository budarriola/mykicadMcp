"""Phase 7.19.1 — obstacle-aware goal-distance heuristic for the fine A*.

The deliverable is "the same route, found by looking at less of the window", so
this file proves BOTH halves and treats the first as the hard gate:

  * ADMISSIBILITY, machine-checked rather than argued. `_true_cost_to_go`
    computes, by exhaustive backward Bellman-Ford over the FULL
    `(cell, layer, heading)` state space using `_build_fine_cost`'s own
    `planar`/`via` closures, the exact optimal remaining cost of every state.
    The field must never exceed it, on every cell of every fixture — a wall, a
    U-shaped pocket, a sealed pocket, a two-layer window, and a window with a
    congestion overlay and an off-corridor penalty active.
  * BYTE-IDENTITY. Field-on and field-off must return the *same list of
    `(cell, layer)` nodes*, not merely paths of equal cost — and the numpy
    backend, which ignores the flag entirely, must return that same list too.
    That three-way agreement is the whole parity argument in executable form:
    numpy is provably flag-independent (it never consults the search heuristic
    and its goal pick is pinned to the octile tie-break), so pinning cpu to it
    with the flag both ways pins cpu to itself.
  * DOMINANCE / EXPANSION REDUCTION. The field must be >= octile everywhere
    (else it cannot help) and must actually cut A* expansions on a window whose
    obstacles make octile badly misleading.
"""

from __future__ import annotations

import kicad_router_accel as accel
import kicad_router_tool as router


_LT1 = {"F.Cu": "signal"}
_LT2 = {"F.Cu": "signal", "B.Cu": "signal"}


def _weights():
    return router._Weights({}, 1.0)


def _win(layers, layer_types, cols, rows, obstacles=(), grid=1.0, net="SIG"):
    w = router._FineWindow(0.0, 0.0, (cols - 1) * grid, (rows - 1) * grid,
                           grid, layers, layer_types, net)
    w.build(list(obstacles), 0.1, 0.3, 0.2, 0.2)
    return w


def _wall(layer, x0, y0, x1, y1, half=0.4):
    return router._Obst("seg", "OBS", frozenset([layer]), half,
                        x0, y0, x1, y1, owner=None)


# --------------------------------------------------------------------------- #
# Exhaustive reference: the TRUE optimal cost-to-go of every state.
# --------------------------------------------------------------------------- #

def _true_cost_to_go(win, model):
    """`{(ix, iy, layer, dir): optimal remaining milli-cost}` by backward
    Bellman-Ford over the exact edge set `_fine_astar` walks forward.

    Deliberately brute force (relax until fixpoint) so it shares no code with
    the thing under test beyond the cost closures themselves — the point is an
    independent oracle, not a fast one. Only tractable on tiny fixtures."""
    planar, via, is_goal = model["planar"], model["via"], model["is_goal"]
    layers = win.layers
    INF = router._FINE_INF
    dist: dict[tuple[int, int, str, int], int] = {}
    states = [(ix, iy, l, d)
              for iy in range(win.rows) for ix in range(win.cols)
              for l in layers for d in range(-1, 8)]
    for st in states:
        dist[st] = 0 if is_goal(st[0], st[1], st[2]) else INF
    changed = True
    while changed:
        changed = False
        for (ix, iy, layer, d) in states:
            cur = dist[(ix, iy, layer, d)]
            if cur == 0:
                continue
            best = cur
            for di, (dx, dy) in enumerate(router._MOVES):
                nx, ny = ix + dx, iy + dy
                if not win.in_bounds(nx, ny):
                    continue
                mc = planar(nx, ny, layer, di, d)
                if mc is None:
                    continue
                nxt = dist[(nx, ny, layer, di)]
                if nxt < INF and mc + nxt < best:
                    best = mc + nxt
            for other in layers:
                if other == layer:
                    continue
                mc = via(ix, iy, other)
                if mc is None:
                    break
                nxt = dist[(ix, iy, other, d)]
                if nxt < INF and mc + nxt < best:
                    best = mc + nxt
            if best < cur:
                dist[(ix, iy, layer, d)] = best
                changed = True
    return dist


def _model(win, *, congestion=None, corridor=None, net_kind="signal",
           goal=(0, 0), goal_layers=None, goal_field=True):
    return router._build_fine_cost(
        win, net_kind, _weights(), {}, {}, None, corridor, congestion,
        None, None, 0.0, 0.0, goal, goal_layers or {win.layers[0]},
        False, None, goal_field)


def _assert_admissible(win, goal, goal_layers=None, **kw):
    """The field never overstates the true optimal remaining cost."""
    model = _model(win, goal=goal, goal_layers=goal_layers, **kw)
    field = model["goal_field"]
    assert field is not None, "field should be active for a non-plane net"
    truth = _true_cost_to_go(win, model)
    heuristic = model["heuristic"]
    checked = 0
    for iy in range(win.rows):
        for ix in range(win.cols):
            best = min(truth[(ix, iy, l, d)]
                       for l in win.layers for d in range(-1, 8))
            # Check the heuristic the search actually uses (max(octile, field)),
            # not just the raw field - the max is what has to be admissible.
            h = heuristic(ix, iy)
            assert h <= best, (
                f"INADMISSIBLE at ({ix},{iy}): h={h} > true={best}")
            assert field.value(ix, iy) <= best
            checked += 1
    assert checked == win.rows * win.cols
    return model, field, truth


# --------------------------------------------------------------------------- #
# Admissibility
# --------------------------------------------------------------------------- #

def test_admissible_open_window():
    win = _win(["F.Cu"], _LT1, 9, 7)
    _assert_admissible(win, (8, 6))


def test_admissible_around_a_wall():
    # A wall across the middle with a single gap at the bottom: octile is badly
    # wrong here, which is exactly the case the field is for.
    win = _win(["F.Cu"], _LT1, 11, 9, obstacles=[_wall("F.Cu", 5.0, 0.0, 5.0, 6.0)])
    _assert_admissible(win, (10, 0))


def test_admissible_u_shaped_pocket():
    obs = [_wall("F.Cu", 3.0, 0.0, 3.0, 6.0),
           _wall("F.Cu", 3.0, 6.0, 8.0, 6.0),
           _wall("F.Cu", 8.0, 0.0, 8.0, 6.0)]
    win = _win(["F.Cu"], _LT1, 12, 10, obstacles=obs)
    _assert_admissible(win, (11, 9))


def test_admissible_two_layers_via_detour():
    win = _win(["F.Cu", "B.Cu"], _LT2, 9, 5,
               obstacles=[_wall("F.Cu", 4.0, 0.0, 4.0, 4.0)])
    _assert_admissible(win, (8, 4), goal_layers={"B.Cu"})


def test_admissible_with_congestion_and_corridor_overlays():
    """Both overlays only ADD cost, so the field (which ignores them) must stay
    a lower bound — asserted, not assumed."""
    win = _win(["F.Cu"], _LT1, 9, 7,
               obstacles=[_wall("F.Cu", 4.0, 0.0, 4.0, 4.0)])
    cong = {(ix, iy, "F.Cu"): 900 for ix in range(9) for iy in range(7)
            if (ix + iy) % 3 == 0}
    corridor = {(ix, iy) for ix in range(9) for iy in range(7) if iy <= 1}
    _assert_admissible(win, (8, 6), congestion=cong, corridor=corridor)


def test_field_is_infinite_for_a_sealed_pocket():
    """A goal walled off on every layer: the relaxation proves unreachability,
    which prunes the whole search instead of exploring the window."""
    obs = [_wall("F.Cu", 2.0, 0.0, 2.0, 8.0),
           _wall("F.Cu", 2.0, 0.0, 8.0, 0.0),
           _wall("F.Cu", 2.0, 8.0, 8.0, 8.0),
           _wall("F.Cu", 8.0, 0.0, 8.0, 8.0)]
    win = _win(["F.Cu"], _LT1, 11, 9, obstacles=obs)
    model = _model(win, goal=(5, 4))
    field = model["goal_field"]
    assert field.value(0, 0) == router._FINE_INF
    assert model["heuristic"](0, 0) == router._FINE_INF


# --------------------------------------------------------------------------- #
# Dominance over octile
# --------------------------------------------------------------------------- #

# Per-edge flooring (see `unit_floor_milli`) makes the field a MORE conservative
# rounder than octile's single floor-of-the-whole-distance, so on a clear run the
# field can sit a milli or two under octile. That is a rounding artefact, not an
# information deficit; anything beyond it would be a real regression.
_ROUNDING_SLACK_MILLI = 4


def test_field_dominates_octile_up_to_rounding():
    win = _win(["F.Cu"], _LT1, 11, 9, obstacles=[_wall("F.Cu", 5.0, 0.0, 5.0, 6.0)])
    model = _model(win, goal=(10, 0))
    heuristic, octile = model["heuristic"], model["tiebreak_heuristic"]
    strictly_better = 0
    for iy in range(win.rows):
        for ix in range(win.cols):
            h, o = heuristic(ix, iy), octile(ix, iy)
            if h == router._FINE_INF:
                strictly_better += 1
                continue
            assert h >= o - _ROUNDING_SLACK_MILLI, (
                f"field materially below octile at ({ix},{iy}): {h} < {o}")
            if h > o:
                strictly_better += 1
    assert strictly_better > 0, "the wall must make the field strictly tighter"


def test_octile_heuristic_is_marginally_inadmissible():
    """A MEASURED property of the pre-existing octile heuristic, pinned here
    because it is the reason `heuristic` is the field alone rather than
    `max(octile, field)`.

    `floor(octile_units x step_milli_per_unit)` floors ONCE for a whole
    multi-move distance, whereas the true cost is a sum of independently
    `round`-ed per-move costs, so octile can come out a milli above the true
    optimum. Harmless for its two remaining jobs (ordering the legacy frontier,
    breaking reconstruction ties) since neither needs a bound - but it must not
    be folded into the bound the 7.19.1 drain relies on."""
    win = _win(["F.Cu"], _LT1, 9, 7)
    model = _model(win, goal=(8, 6))
    truth = _true_cost_to_go(win, model)
    octile = model["tiebreak_heuristic"]
    overstatements = [
        (ix, iy, octile(ix, iy),
         min(truth[(ix, iy, l, d)] for l in win.layers for d in range(-1, 8)))
        for iy in range(win.rows) for ix in range(win.cols)]
    bad = [t for t in overstatements if t[2] > t[3]]
    assert bad, ("octile no longer overstates anywhere - if the quantizer "
                 "changed, re-derive whether `heuristic` may include it again")
    # ...and the overstatement is only ever a rounding-scale one.
    assert all(h - true <= _ROUNDING_SLACK_MILLI for _x, _y, h, true in bad)
    # The field, on the same window, overstates NOWHERE.
    field = model["goal_field"]
    for ix, iy, _h, true in overstatements:
        assert field.value(ix, iy) <= true


def test_tiebreak_heuristic_is_octile_regardless_of_flag():
    """The reconstruction ordering must NOT move with the search heuristic —
    that pin is what makes the parity argument a proof rather than luck."""
    win = _win(["F.Cu"], _LT1, 9, 7)
    on = _model(win, goal=(8, 6), goal_field=True)
    off = _model(win, goal=(8, 6), goal_field=False)
    assert off["goal_field"] is None
    for iy in range(win.rows):
        for ix in range(win.cols):
            assert on["tiebreak_heuristic"](ix, iy) == off["tiebreak_heuristic"](ix, iy)
            assert off["heuristic"](ix, iy) == off["tiebreak_heuristic"](ix, iy)


def test_field_is_disabled_for_a_plane_owning_net():
    """`planar`'s plane branch can undercut `unit_floor_milli`, so the field is
    gated off there rather than shipped inadmissible."""
    win = _win(["F.Cu", "B.Cu"], _LT2, 9, 5, net="PWR")
    raster = router._FillRaster([(0.0, 0.0), (8.0, 0.0), (8.0, 4.0), (0.0, 4.0)])
    planes = {"B.Cu": [{"raster": raster, "factor": 1.0}]}
    model = router._build_fine_cost(
        win, "power", _weights(), {}, {}, None, None, None,
        planes, None, 0.05, 8.0, (8, 4), {"F.Cu"}, False, None, True)
    assert model["goal_field"] is None
    assert model["heuristic"] is model["tiebreak_heuristic"]


# --------------------------------------------------------------------------- #
# Byte-identity: field-on == field-off == numpy
# --------------------------------------------------------------------------- #

def _three_way(win, *args, **kwargs):
    off = router._fine_astar(win, *args, goal_field=False, **kwargs)
    on = router._fine_astar(win, *args, goal_field=True, **kwargs)
    npw = accel.fine_wavefront(win, *args, **kwargs)
    npw_on = accel.fine_wavefront(win, *args, goal_field=True, **kwargs)
    assert on == off, f"field changed the route:\n off={off}\n on ={on}"
    assert npw == off, f"numpy diverged from cpu:\n cpu={off}\n numpy={npw}"
    assert npw_on == npw, "numpy must be completely unaffected by the flag"
    return off


def test_identical_plain_line():
    win = _win(["F.Cu"], _LT1, 11, 1)
    path = _three_way(win, "signal", _weights(), {}, {}, (0, 0), ["F.Cu"],
                      (10, 0), {"F.Cu"}, None, None, None)
    assert path is not None and len(path) == 11


def test_identical_around_a_wall():
    win = _win(["F.Cu"], _LT1, 11, 9, obstacles=[_wall("F.Cu", 5.0, 0.0, 5.0, 6.0)])
    path = _three_way(win, "signal", _weights(), {}, {}, (0, 8), ["F.Cu"],
                      (10, 8), {"F.Cu"}, None, None, None)
    assert path is not None


def test_identical_u_shaped_pocket():
    obs = [_wall("F.Cu", 3.0, 0.0, 3.0, 6.0),
           _wall("F.Cu", 3.0, 6.0, 8.0, 6.0),
           _wall("F.Cu", 8.0, 0.0, 8.0, 6.0)]
    win = _win(["F.Cu"], _LT1, 12, 10, obstacles=obs)
    path = _three_way(win, "signal", _weights(), {}, {}, (0, 0), ["F.Cu"],
                      (11, 9), {"F.Cu"}, None, None, None)
    assert path is not None


def test_identical_cross_layer_via():
    win = _win(["F.Cu", "B.Cu"], _LT2, 9, 5)
    path = _three_way(win, "signal", _weights(), {}, {}, (0, 0), ["F.Cu"],
                      (8, 4), {"B.Cu"}, None, None, None)
    assert path is not None
    assert len(router._path_via_nodes(path)) >= 1


def test_identical_with_congestion_overlay():
    win = _win(["F.Cu", "B.Cu"], _LT2, 11, 7,
               obstacles=[_wall("F.Cu", 5.0, 0.0, 5.0, 4.0)])
    cong = {(ix, iy, "F.Cu"): 700 for ix in range(11) for iy in range(7)
            if (ix * 7 + iy * 3) % 5 == 0}
    path = _three_way(win, "signal", _weights(), {}, {}, (0, 6), ["F.Cu"],
                      (10, 6), {"B.Cu"}, None, None, cong)
    assert path is not None


def test_identical_with_corridor_and_home_layer():
    win = _win(["F.Cu", "B.Cu"], _LT2, 11, 7)
    corridor = {(ix, iy) for ix in range(11) for iy in range(7) if iy >= 3}
    path = _three_way(win, "signal", _weights(), {}, {}, (0, 6), ["F.Cu"],
                      (10, 3), {"F.Cu"}, "F.Cu", corridor, None)
    assert path is not None


def test_identical_unreachable_returns_none_both_ways():
    obs = [_wall("F.Cu", 2.0, 0.0, 2.0, 8.0),
           _wall("F.Cu", 2.0, 0.0, 8.0, 0.0),
           _wall("F.Cu", 2.0, 8.0, 8.0, 8.0),
           _wall("F.Cu", 8.0, 0.0, 8.0, 8.0)]
    win = _win(["F.Cu"], _LT1, 11, 9, obstacles=obs)
    assert _three_way(win, "signal", _weights(), {}, {}, (0, 0), ["F.Cu"],
                      (5, 4), {"F.Cu"}, None, None, None) is None


# --------------------------------------------------------------------------- #
# The actual deliverable: less of the window looked at.
# --------------------------------------------------------------------------- #

def test_field_cuts_expansions_on_a_misleading_window():
    """A goal directly across a long wall whose only gap is at the far end, so
    octile points the search straight into copper for thousands of cells.

    Work is counted HONESTLY: A* expansions PLUS every cell the backward
    wavefront settled. The field's own search is 2D (one value per cell) where
    the search it prunes is 4D (cell x layer x heading), which is the whole
    economic argument for the technique — asserted here, not asserted away."""
    obs = [_wall("F.Cu", 20.0, 0.0, 20.0, 24.0)]
    win = _win(["F.Cu"], _LT1, 41, 31, obstacles=obs)
    args = ("signal", _weights(), {}, {}, (2, 2), ["F.Cu"],
            (38, 2), {"F.Cu"}, None, None, None)

    off = router._fine_astar(win, *args, goal_field=False)
    base = router._FINE_SEARCH_STATS["expansions"]
    on = router._fine_astar(win, *args, goal_field=True)
    with_field = router._FINE_SEARCH_STATS["expansions"]
    field_cells = router._FINE_SEARCH_STATS["field_expansions"]

    assert on == off, "the reduction must not have cost us the route"
    assert with_field < base, f"no A* reduction: {base} -> {with_field}"
    assert with_field + field_cells < base, (
        f"combined work not reduced: {base} -> {with_field}+{field_cells}")


def test_field_cost_scales_with_layers_but_the_field_does_not():
    """The saving GROWS with layer count: the A* state space multiplies by the
    layer count, the 2D field does not move at all."""
    obs = [_wall("F.Cu", 20.0, 0.0, 20.0, 24.0), _wall("B.Cu", 20.0, 0.0, 20.0, 24.0)]
    win = _win(["F.Cu", "B.Cu"], _LT2, 41, 31, obstacles=obs)
    args = ("signal", _weights(), {}, {}, (2, 2), ["F.Cu"],
            (38, 2), {"F.Cu"}, None, None, None)
    off = router._fine_astar(win, *args, goal_field=False)
    base = router._FINE_SEARCH_STATS["expansions"]
    on = router._fine_astar(win, *args, goal_field=True)
    total = (router._FINE_SEARCH_STATS["expansions"]
             + router._FINE_SEARCH_STATS["field_expansions"])
    assert on == off
    assert total < base


def test_unreachable_goal_is_pruned_without_searching():
    """The strongest case. An `_FINE_INF` field value is a PROOF of
    unreachability (the field is a relaxation), so the search can answer None
    without expanding a single state, where the legacy search must exhaust the
    whole window to reach the same answer."""
    obs = [_wall("F.Cu", 10.0, 6.0, 10.0, 24.0),
           _wall("F.Cu", 10.0, 6.0, 30.0, 6.0),
           _wall("F.Cu", 10.0, 24.0, 30.0, 24.0),
           _wall("F.Cu", 30.0, 6.0, 30.0, 24.0)]
    win = _win(["F.Cu"], _LT1, 41, 31, obstacles=obs)
    args = ("signal", _weights(), {}, {}, (1, 1), ["F.Cu"],
            (20, 15), {"F.Cu"}, None, None, None)
    assert router._fine_astar(win, *args, goal_field=False) is None
    base = router._FINE_SEARCH_STATS["expansions"]
    assert base > 1000, "fixture must be one the legacy search really has to sweep"
    assert router._fine_astar(win, *args, goal_field=True) is None
    assert router._FINE_SEARCH_STATS["expansions"] == 0
    assert router._FINE_SEARCH_STATS["field_expansions"] < base
