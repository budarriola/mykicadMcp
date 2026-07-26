"""Plane-via anti-pad model (2026-07-24): a foreign POWER/GND plane fill yields
an anti-pad to a via crossing it, so it blocks same-layer TRACKS but never VIAS.
This is what lets a signal net via through a plane (the unlock for kiln's
cross-layer control bus - see NETCLASS_PLAN.md plane-via findings).

Two levels of proof, mirroring the two places the model lives:
  1. `_FineWindow` obstacle build: a `via_transparent` zone leaves `blocked_via`
     empty while filling `blocked_track` on its layer; a NON-transparent zone
     (signal fill) blocks both.
  2. `_self_check`: a via over a `via_transparent` plane is NOT a violation;
     over a non-transparent obstacle it IS. Tracks are checked either way.
"""

from __future__ import annotations

import kicad_router_tool as router


def _rect_zone(net: str, layer: str, x0: float, y0: float, x1: float, y1: float,
               via_transparent: bool) -> router._Obst:
    pts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    raster = router._FillRaster(pts)
    return router._Obst("zone", net, frozenset([layer]), 0.0, x0, y0, x0, y0,
                        raster=raster, pts=pts, via_transparent=via_transparent)


def _window(layers, layer_types):
    win = router._FineWindow(0.0, 0.0, 10.0, 10.0, 1.0, layers, layer_types, "SIG")
    return win


def test_transparent_plane_blocks_tracks_not_vias() -> None:
    layers = ["F.Cu", "In1.Cu", "B.Cu"]
    lt = {"F.Cu": "signal", "In1.Cu": "power", "B.Cu": "signal"}
    win = _window(layers, lt)
    plane = _rect_zone("GND", "In1.Cu", -5.0, -5.0, 15.0, 15.0, via_transparent=True)
    win.build([plane], 0.1, 0.3, 0.2, 0.2)
    # the plane fills In1.Cu track-blocking...
    assert len(win.blocked_track["In1.Cu"]) > 0
    # ...but blocks NO vias (a via crossing it gets an anti-pad).
    assert win.blocked_via == set()
    # and it does not touch the OTHER layers' track sets (it is In1.Cu only).
    assert win.blocked_track["F.Cu"] == set()
    assert win.blocked_track["B.Cu"] == set()


def test_signal_fill_blocks_vias_too() -> None:
    layers = ["F.Cu", "In1.Cu", "B.Cu"]
    lt = {"F.Cu": "signal", "In1.Cu": "signal", "B.Cu": "signal"}
    win = _window(layers, lt)
    fill = _rect_zone("DATA", "In1.Cu", -5.0, -5.0, 15.0, 15.0, via_transparent=False)
    win.build([fill], 0.1, 0.3, 0.2, 0.2)
    # a non-transparent (signal) fill blocks vias AND tracks - the pre-existing
    # behaviour, unchanged for anything that is not a power/gnd plane.
    assert len(win.blocked_via) > 0
    assert len(win.blocked_track["In1.Cu"]) > 0


def test_self_check_via_over_plane_is_clean() -> None:
    rules = {"track_width": 0.2, "clearance": 0.2, "edge_clearance": 0.2}
    plane = _rect_zone("GND", "In1.Cu", -5.0, -5.0, 15.0, 15.0, via_transparent=True)
    via = [{"x": 5.0, "y": 5.0}]
    # a via sitting squarely in the plane is NOT a violation (anti-pad yields).
    assert router._self_check("SIG", [], via, [plane], rules, 0.3) == []


def test_self_check_via_over_signal_fill_violates() -> None:
    rules = {"track_width": 0.2, "clearance": 0.2, "edge_clearance": 0.2}
    fill = _rect_zone("DATA", "In1.Cu", -5.0, -5.0, 15.0, 15.0, via_transparent=False)
    via = [{"x": 5.0, "y": 5.0}]
    viol = router._self_check("SIG", [], via, [fill], rules, 0.3)
    assert any(v["kind"] == "via" for v in viol)


def test_self_check_track_in_plane_still_violates() -> None:
    """The plane still blocks a same-layer TRACK - via-transparency is via-only."""
    rules = {"track_width": 0.2, "clearance": 0.2, "edge_clearance": 0.2}
    plane = _rect_zone("GND", "In1.Cu", -5.0, -5.0, 15.0, 15.0, via_transparent=True)
    seg = [{"x1": 1.0, "y1": 5.0, "x2": 9.0, "y2": 5.0, "layer": "In1.Cu"}]
    viol = router._self_check("SIG", seg, [], [plane], rules, 0.3)
    assert any(v["kind"] == "segment" for v in viol)
