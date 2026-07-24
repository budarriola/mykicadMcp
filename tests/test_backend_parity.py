"""Phase 7.8 correctness gate: the numpy wavefront backend
(`kicad_router_accel.fine_wavefront`) must return BYTE-IDENTICAL geometry to the
cpu A* reference (`kicad_router_tool._fine_astar`) on every window.

This is a CORRECTNESS test, not a speed test (per the standing directive we do
NOT benchmark or confirm speedups). numpy is a hard dependency: if it is missing
this test errors at import, which is the intended "install error, not runtime
fallback" behaviour.

The 7.8 parity guarantee (see kicad_router_accel's module docstring): both
backends cost every move through the same integer milli-cost model
(`_build_fine_cost`) and reconstruct with the same deterministic field-only
backtrace (`_fine_backtrace`), so the emitted path + vias are identical, not
merely equal in cost. These constructions reuse the tiny synthetic windows from
test_plane_routing.py that isolate each mechanic: plain travel, a cross-layer
via detour, a plane-bypass around a wall, and early plane termination.
"""

from __future__ import annotations

import kicad_router_tool as router
import kicad_router_accel as accel  # hard dep: import error here == numpy missing


_LT1 = {"F.Cu": "signal"}
_LT2 = {"F.Cu": "signal", "B.Cu": "signal"}


def _open_window(layers, layer_types, cols, rows, grid=1.0, net="PWR"):
    win = router._FineWindow(0.0, 0.0, (cols - 1) * grid, (rows - 1) * grid,
                             grid, layers, layer_types, net)
    win.build([], 0.1, 0.3, 0.2, 0.2)
    return win


def _rect_raster(x0, y0, x1, y1):
    return router._FillRaster([(x0, y0), (x1, y0), (x1, y1), (x0, y1)])


def _weights():
    return router._Weights({}, 1.0)


def _assert_parity(win, *args, **kwargs):
    cpu = router._fine_astar(win, *args, **kwargs)
    npw = accel.fine_wavefront(win, *args, **kwargs)
    assert cpu == npw, f"backend divergence:\n cpu={cpu}\n numpy={npw}"
    return cpu


def test_parity_plain_line():
    win = _open_window(["F.Cu"], _LT1, cols=11, rows=1)
    path = _assert_parity(win, "power", _weights(), {}, {}, (0, 0), ["F.Cu"],
                          (10, 0), {"F.Cu"}, None, None, None)
    assert path is not None and len(path) == 11


def test_parity_cross_layer_via():
    win = _open_window(["F.Cu", "B.Cu"], _LT2, cols=9, rows=5)
    path = _assert_parity(win, "signal", _weights(), {}, {}, (0, 0), ["F.Cu"],
                          (8, 4), {"B.Cu"}, None, None, None)
    assert path is not None
    assert len(router._path_via_nodes(path)) >= 1


def test_parity_plane_bypass_wall():
    layers = ["F.Cu", "B.Cu"]
    win = router._FineWindow(0.0, 0.0, 10.0, 1.0, 1.0, layers, _LT2, "PWR")
    wall0 = router._Obst("seg", "OBS", frozenset(["F.Cu"]), 0.4, 2.0, 0.0, 8.0, 0.0, owner=None)
    wall1 = router._Obst("seg", "OBS", frozenset(["F.Cu"]), 0.4, 2.0, 1.0, 8.0, 1.0, owner=None)
    win.build([wall0, wall1], 0.1, 0.3, 0.2, 0.2)
    raster = _rect_raster(-2.0, -2.0, 12.0, 3.0)
    plane_layers = {"B.Cu": [{"raster": raster, "factor": 1.0}]}
    path = _assert_parity(win, "power", _weights(), {}, {}, (0, 0), ["F.Cu"],
                          (10, 0), {"F.Cu"}, None, None, None, plane_layers,
                          None, 0.05, 8.0)
    assert path is not None
    assert "B.Cu" in {l for (_x, _y, l) in path}


def test_parity_plane_termination():
    win = _open_window(["F.Cu"], _LT1, cols=11, rows=1)
    raster = _rect_raster(4.9, -2.0, 20.0, 2.0)
    plane_layers = {"F.Cu": [{"raster": raster, "factor": 1.0}]}
    goal_planes = {"F.Cu": [{"raster": raster, "factor": 1.0}]}
    path = _assert_parity(win, "power", _weights(), {}, {}, (0, 0), ["F.Cu"],
                          (10, 0), {"F.Cu"}, None, None, None, plane_layers,
                          goal_planes, 0.05, 8.0)
    assert path is not None and path[-1] == (5, 0, "F.Cu")


def test_parity_unreachable_returns_none_both():
    """A fully via/track-blocked interior with no goal reach: both backends must
    agree on None (genuine unreachable, not a backend artifact)."""
    win = router._FineWindow(0.0, 0.0, 4.0, 1.0, 1.0, ["F.Cu"], _LT1, "PWR")
    # Wall the whole interior on the single layer so the goal is unreachable.
    wall = router._Obst("seg", "OBS", frozenset(["F.Cu"]), 4.0, 1.0, 0.0, 1.0, 1.0, owner=None)
    win.build([wall], 0.1, 0.3, 0.2, 0.2)
    cpu = router._fine_astar(win, "signal", _weights(), {}, {}, (0, 0), ["F.Cu"],
                             (4, 0), {"F.Cu"}, None, None, None)
    npw = accel.fine_wavefront(win, "signal", _weights(), {}, {}, (0, 0), ["F.Cu"],
                               (4, 0), {"F.Cu"}, None, None, None)
    assert cpu == npw
