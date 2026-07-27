"""Tests for Phase 7.3d: direction-aware pad escape (`_FineWindow.nearest_free`'s
optional `toward_xy` bias, gated behind `autorouter.pad_escape_direction_aware`).

`nearest_free` is called unconditionally for every connection's start AND goal
cell (see its two call sites in `_route_one` and the two/three in
`_route_hierarchical`), so this is NOT a pure addition like 7.5.5/7.9/7.12 -
changing its tie-break changes board-wide routing geometry. That is why the
setting defaults to False and why the parity requirement below (flag OFF must
reproduce today's exact output) is the load-bearing assertion in this file,
not just the positive "it changes something when ON" case.

The dense-pin-field scenario is built by writing directly into `blocked_track`
(rather than through real `_Obst` pad geometry) so the winning ring's free/
blocked pattern is exact and grid-index-precise - the obstacle-geometry-to-
cell conversion itself is already covered by `test_hierarchical_route.py`'s
wall-gap fixture; this file isolates `nearest_free`'s ring-search + tie-break
logic on its own.
"""

from __future__ import annotations

import kicad_router_tool as rt

_LAYERS = ["F.Cu"]
_LAYER_TYPES = {"F.Cu": "signal"}


def _window() -> rt._FineWindow:
    # grid=1.0 world units, spanning [-5, 5] on both axes -> cell_of(0, 0)
    # lands exactly on the center cell (5, 5), so ring math below is exact.
    win = rt._FineWindow(-5.0, -5.0, 5.0, 5.0, 1.0, _LAYERS, _LAYER_TYPES, "TARGET")
    return win


def _dense_pin_field_window() -> rt._FineWindow:
    """A pad at world (0, 0) (grid cell (5, 5)) whose own node and every
    ring-1 neighbor are blocked EXCEPT two: the west edge cell (4, 5) - the
    pure-nearest candidate (Euclidean distance = 1 grid unit) - and the
    north-east corner cell (6, 6) - farther (distance = sqrt(2) grid units)
    but the only free ring-1 candidate whose direction from the pad has any
    positive component toward a connection partner sitting due east. This is
    the "several tightly packed pads on one side, open ring on the other"
    fixture the 7.3d spec calls for, at the grid-index level."""
    win = _window()
    blocked = win.blocked_track["F.Cu"]
    cx, cy = win.cell_of(0.0, 0.0)
    assert (cx, cy) == (5, 5)
    blocked.add((cx, cy))  # the pad's own node
    for iy in range(cy - 1, cy + 2):
        for ix in range(cx - 1, cx + 2):
            if (ix, iy) == (cx, cy):
                continue
            if (ix, iy) in {(cx - 1, cy), (cx + 1, cy + 1)}:
                continue  # leave the west edge + NE corner free
            blocked.add((ix, iy))
    return win


def test_flag_off_picks_pure_nearest_possibly_wrong_side():
    """With no `toward_xy` (the default `pad_escape_direction_aware: false`
    behavior), the ring-1 tie-break is pure Euclidean distance - it picks the
    west edge cell (dist=1.0), even though the connection's other endpoint
    (passed as `toward_xy` in the next test) sits due east."""
    win = _dense_pin_field_window()
    cx, cy = win.cell_of(0.0, 0.0)
    result = win.nearest_free(0.0, 0.0, _LAYERS)
    assert result == (cx - 1, cy)  # the nearer, "wrong side" (west) node


def test_flag_on_biases_toward_the_other_endpoint():
    """With `toward_xy` pointing due east (where this connection is actually
    headed), the same window/pad instead lands on the NE corner node - the
    only free ring-1 candidate with a positive dot product toward that
    direction - even though it is farther away in raw Euclidean distance than
    the west node picked above."""
    win = _dense_pin_field_window()
    cx, cy = win.cell_of(0.0, 0.0)
    result = win.nearest_free(0.0, 0.0, _LAYERS, toward_xy=(50.0, 0.0))
    assert result == (cx + 1, cy + 1)  # the farther but direction-aligned node
    assert result != (cx - 1, cy)  # must differ from the flag-off pick above


def test_flag_on_with_single_candidate_matches_flag_off():
    """The spec's explicit carve-out: when the winning ring offers only ONE
    free candidate (the common uncongested-board case), the result is
    unchanged regardless of `toward_xy` - there is nothing to bias between."""
    win = _window()
    blocked = win.blocked_track["F.Cu"]
    cx, cy = win.cell_of(0.0, 0.0)
    blocked.add((cx, cy))
    # Block every ring-1 cell except one, so only a single candidate exists.
    for iy in range(cy - 1, cy + 2):
        for ix in range(cx - 1, cx + 2):
            if (ix, iy) in {(cx, cy), (cx - 1, cy)}:
                continue
            blocked.add((ix, iy))
    off = win.nearest_free(0.0, 0.0, _LAYERS)
    on = win.nearest_free(0.0, 0.0, _LAYERS, toward_xy=(50.0, 0.0))
    assert off == on == (cx - 1, cy)


def test_toward_xy_none_is_byte_identical_to_pre_7_3d_signature():
    """Parity guard: calling `nearest_free` exactly as every pre-7.3d call
    site does (no `toward_xy` argument at all) must still work and must not
    consult direction in any way - covered structurally by the two tests
    above using the keyword explicitly, this test just pins the positional/
    default-arg call shape callers already use (see `_route_one`,
    `_route_hierarchical`, and `test_hierarchical_route.py`)."""
    win = _dense_pin_field_window()
    cx, cy = win.cell_of(0.0, 0.0)
    assert win.nearest_free(0.0, 0.0, _LAYERS, max_ring=6) == (cx - 1, cy)
