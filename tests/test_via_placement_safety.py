"""Phase 7.21 - via placement safety: no via-in-pad, no via-via overlap.

User-reported bug from a real routed board: vias landing inside pads, and vias
overlapping other vias. Root cause was the "same-net copper is free" exemption,
which is CORRECT for tracks but was applied to vias as well, at all three
parity-critical sites (`_FineWindow.obstacle_cells`, its lazy per-cell mirror,
and the pre-write `_self_check` gate).

Two rules, deliberately gated differently:
  - VIA-IN-PAD is opt-in: `autorouter.allow_via_in_pad` (default false) - a via
    may not land in ANY pad, same-net or foreign, unless the user turns it on
    (via-in-pad is a real but niche manufacturing technique).
  - VIA-VIA OVERLAP is unconditional - two drilled holes are never valid, so
    there is deliberately no knob.

SCOPE GUARD: every "tracks are unaffected" assertion below is load-bearing. A
route legitimately runs alongside / lands on its own net's copper, and that
must keep working byte-for-byte; the fix touches the via reach only.
"""

from __future__ import annotations

import json
from pathlib import Path

import kicad_pcb_tool as pcb
import kicad_router_tool as router
from synthetic_board import _synthetic_kicad_pro_text

_LAYERS = ["F.Cu", "B.Cu"]
_LAYER_TYPES = {"F.Cu": "signal", "B.Cu": "signal"}
_NET = "SIG"
_RULES = {"track_width": 0.2, "clearance": 0.2, "edge_clearance": 0.2}
_TRACK_HALF = 0.1
_VIA_RADIUS = 0.3


def _pad(net: str, x: float = 5.0, y: float = 5.0, half: float = 0.5) -> router._Obst:
    return router._Obst("pt", net, frozenset(_LAYERS), half, x, y, x, y, is_pad=True)


def _via(net: str, x: float = 5.0, y: float = 5.0, half: float = 0.3,
         owner: int | None = None) -> router._Obst:
    return router._Obst("pt", net, frozenset(_LAYERS), half, x, y, x, y, owner=owner)


def _seg(net: str, y: float = 5.0) -> router._Obst:
    return router._Obst("seg", net, frozenset(["F.Cu"]), 0.1, 1.0, y, 9.0, y)


def _built(obstacles: list[router._Obst], *, allow_via_in_pad: bool = False,
           lazy: bool = False) -> router._FineWindow:
    win = router._FineWindow(0.0, 0.0, 10.0, 10.0, 0.5, _LAYERS, _LAYER_TYPES, _NET,
                             lazy=lazy, allow_via_in_pad=allow_via_in_pad)
    win.build(obstacles, _TRACK_HALF, _VIA_RADIUS, _RULES["clearance"], _RULES["edge_clearance"])
    return win


def _track_cells(win: router._FineWindow) -> set:
    """Every track-blocked cell across all layers (materialized, so this works
    for the lazy window's `_LazyBlockedSet` view too)."""
    out: set = set()
    for layer in _LAYERS:
        out |= {(ix, iy) for ix in range(win.cols) for iy in range(win.rows)
                if (ix, iy) in win.blocked_track[layer]}
    return out


def _via_cells(win: router._FineWindow) -> set:
    return {(ix, iy) for ix in range(win.cols) for iy in range(win.rows)
            if (ix, iy) in win.blocked_via}


# --------------------------------------------------------------------------- #
# Fix A - via-in-pad, gated by `autorouter.allow_via_in_pad` (default False)
# --------------------------------------------------------------------------- #

def test_same_net_pad_blocks_vias_by_default() -> None:
    win = _built([_pad(_NET)])
    assert _via_cells(win), "a same-net pad must block vias by default (no via-in-pad)"
    # SCOPE GUARD: it must NOT have gained any track-blocking - same-net copper
    # stays fully permeable to the net's own trace.
    assert _track_cells(win) == set()


def test_same_net_pad_is_via_permeable_when_opted_in() -> None:
    win = _built([_pad(_NET)], allow_via_in_pad=True)
    # `allow_via_in_pad: true` restores the pre-7.21 behavior exactly: the
    # same-net pad blocks nothing at all.
    assert _via_cells(win) == set()
    assert _track_cells(win) == set()


def test_foreign_pad_blocks_both_regardless_of_the_flag() -> None:
    for allow in (False, True):
        win = _built([_pad("OTHER")], allow_via_in_pad=allow)
        assert _via_cells(win), f"a foreign pad always blocks vias (allow={allow})"
        assert _track_cells(win), f"a foreign pad always blocks tracks (allow={allow})"


# --------------------------------------------------------------------------- #
# Fix B - via-on-via, UNCONDITIONAL (no knob)
# --------------------------------------------------------------------------- #

def test_same_net_existing_via_blocks_vias_unconditionally() -> None:
    for allow in (False, True):
        win = _built([_via(_NET)], allow_via_in_pad=allow)
        assert _via_cells(win), (
            f"two vias may never overlap, even same-net (allow_via_in_pad={allow})")
        # SCOPE GUARD: still free for the net's own TRACK.
        assert _track_cells(win) == set()


def test_same_net_segment_and_zone_stay_completely_free() -> None:
    """The exemption is unchanged for everything that is not a pad or a via."""
    for allow in (False, True):
        win = _built([_seg(_NET)], allow_via_in_pad=allow)
        assert _via_cells(win) == set()
        assert _track_cells(win) == set()


# --------------------------------------------------------------------------- #
# Eager/lazy parity - the invariant `tests/test_lazy_window.py` guards, re-run
# over the obstacle shapes 7.21 actually changed.
# --------------------------------------------------------------------------- #

def test_eager_lazy_parity_over_same_net_via_blockers() -> None:
    obstacles = [_pad(_NET, 3.0, 3.0), _via(_NET, 7.0, 7.0), _seg(_NET, 5.0),
                 _pad("OTHER", 2.0, 8.0)]
    for allow in (False, True):
        eager = _built(obstacles, allow_via_in_pad=allow)
        lazy = _built(obstacles, allow_via_in_pad=allow, lazy=True)
        assert _via_cells(eager) == _via_cells(lazy), f"via-cell parity (allow={allow})"
        for layer in _LAYERS:
            e = {(ix, iy) for ix in range(eager.cols) for iy in range(eager.rows)
                 if (ix, iy) in eager.blocked_track[layer]}
            l = {(ix, iy) for ix in range(lazy.cols) for iy in range(lazy.rows)
                 if (ix, iy) in lazy.blocked_track[layer]}
            assert e == l, f"track-cell parity on {layer} (allow={allow})"


def test_incremental_add_matches_bulk_build_for_same_net_via_blockers() -> None:
    """`add_obstacle` (rip-up's incremental path) and `build` must agree - they
    share `obstacle_cells`, and this pins that they still do post-7.21."""
    ob = _pad(_NET)
    bulk = _built([ob])
    incr = router._FineWindow(0.0, 0.0, 10.0, 10.0, 0.5, _LAYERS, _LAYER_TYPES, _NET)
    incr.build([], _TRACK_HALF, _VIA_RADIUS, _RULES["clearance"], _RULES["edge_clearance"])
    incr.add_obstacle(ob)
    assert _via_cells(incr) == _via_cells(bulk)
    # ...and removing it again leaves the window clean (ref-count symmetry).
    incr.remove_obstacle(ob)
    assert _via_cells(incr) == set()


# --------------------------------------------------------------------------- #
# `_self_check` - the pre-write DRC gate that let the bug reach the board
# --------------------------------------------------------------------------- #

def test_self_check_flags_via_in_same_net_pad_by_default() -> None:
    viol = router._self_check(_NET, [], [{"x": 5.0, "y": 5.0}], [_pad(_NET)], _RULES, _VIA_RADIUS)
    assert [v["kind"] for v in viol] == ["via"]
    assert viol[0]["against_is_pad"] is True
    assert viol[0]["against_net"] == _NET


def test_self_check_allows_via_in_same_net_pad_when_opted_in() -> None:
    viol = router._self_check(_NET, [], [{"x": 5.0, "y": 5.0}], [_pad(_NET)], _RULES,
                              _VIA_RADIUS, True)
    assert viol == []


def test_self_check_flags_via_on_same_net_via_with_or_without_the_flag() -> None:
    for allow in (False, True):
        viol = router._self_check(_NET, [], [{"x": 5.0, "y": 5.0}], [_via(_NET)], _RULES,
                                  _VIA_RADIUS, allow)
        assert [v["kind"] for v in viol] == ["via"], f"allow_via_in_pad={allow}"
        assert viol[0]["against_is_pad"] is False


def test_self_check_segment_over_own_pad_stays_clean() -> None:
    """SCOPE GUARD, the important one: a route landing ON its own endpoint pad
    is the normal case and must stay violation-free in BOTH modes."""
    seg = [{"x1": 1.0, "y1": 5.0, "x2": 9.0, "y2": 5.0, "layer": "F.Cu"}]
    for allow in (False, True):
        assert router._self_check(_NET, seg, [], [_pad(_NET)], _RULES, _VIA_RADIUS, allow) == []
        assert router._self_check(_NET, seg, [], [_via(_NET)], _RULES, _VIA_RADIUS, allow) == []
        assert router._self_check(_NET, seg, [], [_seg(_NET)], _RULES, _VIA_RADIUS, allow) == []


def test_self_check_via_clear_of_the_pad_is_fine() -> None:
    """The rule is geometric, not categorical: a via far enough from the same-net
    pad passes even with the default (strict) setting."""
    assert router._self_check(_NET, [], [{"x": 9.5, "y": 9.5}], [_pad(_NET)],
                              _RULES, _VIA_RADIUS) == []


# --------------------------------------------------------------------------- #
# The prefilter must not drop the obstacles the fix depends on
# --------------------------------------------------------------------------- #

def test_prefilter_keeps_same_net_via_blockers_and_still_drops_same_net_tracks() -> None:
    obstacles = [_pad(_NET, 5.0, 5.0), _via(_NET, 5.0, 6.0), _seg(_NET, 5.0)]
    kept = router._prefilter_window_obstacles(
        obstacles, _NET, (1.0, 5.0), (9.0, 5.0), (0.0, 0.0, 10.0, 10.0), 0.5,
        [(2.0, 0.5)], _TRACK_HALF, _VIA_RADIUS, 0.2, 0.2)
    kinds = [(o.kind, o.is_pad) for o in kept]
    assert ("pt", True) in kinds and ("pt", False) in kinds
    assert ("seg", False) not in kinds, "same-net tracks are still dropped wholesale"

    # opted in, the same-net PAD goes back to being dropped; the via never is.
    kept_opt = router._prefilter_window_obstacles(
        obstacles, _NET, (1.0, 5.0), (9.0, 5.0), (0.0, 0.0, 10.0, 10.0), 0.5,
        [(2.0, 0.5)], _TRACK_HALF, _VIA_RADIUS, 0.2, 0.2, True)
    kinds_opt = [(o.kind, o.is_pad) for o in kept_opt]
    assert ("pt", True) not in kinds_opt
    assert ("pt", False) in kinds_opt


# --------------------------------------------------------------------------- #
# `_nearest_blocker` diagnostic
# --------------------------------------------------------------------------- #

def test_nearest_blocker_reports_same_net_via_blocker_additively() -> None:
    win = _built([])
    own_pad = _pad(_NET, 5.0, 5.0)
    foreign = _pad("OTHER", 5.5, 5.0)
    rec = router._nearest_blocker(win, [own_pad, foreign], _NET, (5.0, 5.0))
    # The primary pick stays the FOREIGN obstacle even though the net's own pad
    # is nearer (it is the goal itself, distance 0) - the pre-7.21 diagnostic.
    assert rec["net"] == "OTHER" and rec["same_net"] is False
    # ...with the same-net via-blocker surfaced alongside it.
    assert rec["same_net_via_blocker"]["net"] == _NET
    assert rec["same_net_via_blocker"]["is_pad"] is True


def test_nearest_blocker_promotes_same_net_blocker_when_nothing_foreign() -> None:
    win = _built([])
    rec = router._nearest_blocker(win, [_pad(_NET, 5.0, 5.0)], _NET, (5.0, 5.0))
    assert rec["net"] == _NET and rec["same_net"] is True
    # ...and a same-net obstacle that does NOT block vias is still no blocker.
    assert router._nearest_blocker(win, [_seg(_NET)], _NET, (5.0, 5.0)) is None


# --------------------------------------------------------------------------- #
# End-to-end on synthetic boards: the invariants the user's real board violated
#
# All the boards below are a narrow 4x20 mm strip with the endpoint pads on
# F.Cu and a solid GND wall across F.Cu at y=10, so the connection MUST change
# layer to get across. The strip is narrow enough that a via has very little
# room, which is exactly the condition under which the old "same-net copper is
# free" exemption made the pads (and any existing same-net via) look like the
# cheapest place to drop it. Verified against the PRE-FIX code: it placed vias
# at (2.0, 4.0) and (2.0, 16.0) - dead centre of both endpoint pads.
# --------------------------------------------------------------------------- #

_HDR = """(kicad_pcb
    (version 20221018)
    (generator "test_via_placement_safety")
    (general (thickness 1.6))
    (paper "A4")
    (layers
        (0 "F.Cu" signal)
        (31 "B.Cu" signal)
    )
    (setup (pad_to_mask_clearance 0))
"""

_PAD_HALF = 0.2      # pads are emitted 0.4 mm square
_VIA_HALF = 0.3      # Default net-class via_diameter 0.6 mm


def _pad_fp(ref: str, x: float, y: float, net: str, uid: str, size: float = 0.4) -> str:
    return (f'    (footprint "synthetic:PAD"\n        (layer "F.Cu")\n        (uuid "{uid}")\n'
            f'        (at {x} {y})\n'
            f'        (property "Reference" "{ref}" (at 0 -2) (layer "F.SilkS"))\n'
            f'        (property "Value" "P" (at 0 2) (layer "F.Fab"))\n'
            f'        (pad "1" smd rect (at 0 0) (size {size} {size}) '
            f'(layers "F.Cu" "F.Paste" "F.Mask") (net "{net}"))\n    )\n')


def _wall(x1: float, y1: float, x2: float, y2: float, layer: str, uid: str) -> str:
    return (f'    (segment\n        (start {x1} {y1})\n        (end {x2} {y2})\n'
            f'        (width 1.0)\n        (layer "{layer}")\n        (net "GND")\n'
            f'        (uuid "{uid}")\n    )\n')


def _existing_via(x: float, y: float, net: str, uid: str) -> str:
    return (f'    (via (at {x} {y}) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") '
            f'(net "{net}") (uuid "{uid}"))\n')


def _strip_project(directory: Path, pads: list[tuple[str, float, float]],
                   extra: str = "", allow_via_in_pad: bool | None = None) -> Path:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    parts = [_HDR, '    (net 0 "")\n    (net 1 "SIGNET")\n    (net 2 "GND")\n',
             '    (gr_rect (start 0 0) (end 4 20) (layer "Edge.Cuts") (width 0.1) '
             '(uuid "vps-edge"))\n']
    for i, (ref, x, y) in enumerate(pads):
        parts.append(_pad_fp(ref, x, y, "SIGNET", f"vps-fp-{i}"))
    parts.append(_wall(0.2, 10.0, 3.8, 10.0, "F.Cu", "vps-wall-f"))
    if extra:
        parts.append(extra)
    parts.append(")\n")
    (directory / "vps.kicad_pcb").write_text("".join(parts), encoding="utf-8")
    (directory / "vps.kicad_pro").write_text(_synthetic_kicad_pro_text(), encoding="utf-8")
    autor: dict = {"cpu": {"workers": 1}}
    if allow_via_in_pad is not None:
        autor["allow_via_in_pad"] = allow_via_in_pad
    (directory / "pcb_settings.json").write_text(
        json.dumps({"autorouter": autor}), encoding="utf-8")
    pcb._invalidate_board_cache(directory / "vps.kicad_pcb")
    return directory


def _conn(x: float) -> dict:
    return {"net": "SIGNET", "from_point": {"x": x, "y": 4.0}, "to_point": {"x": x, "y": 16.0},
            "airline_length_mm": 12.0, "from_layers": ["F.Cu"], "to_layers": ["F.Cu"]}


def _placed_vias(directory: Path) -> list[tuple[float, float, float]]:
    tracks = pcb._parse_tracks_cached(directory / "vps.kicad_pcb")
    return [(v["at"]["x"], v["at"]["y"], v.get("size", 0.6) / 2.0) for v in tracks["vias"]]


def _assert_via_placement_is_sane(directory: Path,
                                  pads: list[tuple[str, float, float]]) -> list[tuple]:
    vias = _placed_vias(directory)
    assert vias, "the F.Cu wall forces a layer change, so vias must exist"
    for (vx, vy, vr) in vias:
        for (_ref, px, py) in pads:
            assert (vx - px) ** 2 + (vy - py) ** 2 > (vr + _PAD_HALF) ** 2, (
                f"via at ({vx}, {vy}) lands inside the pad at ({px}, {py})")
    for i, (ax, ay, ar) in enumerate(vias):
        for (bx, by, br) in vias[i + 1:]:
            assert (ax - bx) ** 2 + (ay - by) ** 2 > (ar + br) ** 2, (
                f"vias at ({ax}, {ay}) and ({bx}, {by}) overlap")
    return vias


def test_routed_board_never_drops_a_via_into_a_pad_or_onto_an_existing_via(tmp_path) -> None:
    """One connection, plus a pre-existing SAME-NET via sitting exactly where an
    unobstructed run of this board puts its via. Pre-fix the router shoved both
    of its own vias into the endpoint pads instead; now it must find real
    copper-free positions clear of the pads AND of the existing via."""
    pads = [("P1", 2.0, 4.0), ("P2", 2.0, 16.0)]
    proj = _strip_project(tmp_path / "inv", pads,
                          extra=_existing_via(1.4, 4.6, "SIGNET", "vps-existing"))
    res = router.route_nets(proj, connections=[_conn(2.0)], write=True)
    rec = res["connections"][0]
    assert rec["routed"] is True and rec["self_check"]["passed"] is True
    vias = _assert_via_placement_is_sane(proj, pads)
    # the pre-existing via is still there and was NOT built on top of.
    assert any(abs(v[0] - 1.4) < 1e-6 and abs(v[1] - 4.6) < 1e-6 for v in vias)
    assert len(vias) >= 3, "the pre-existing via plus this connection's own two"


def test_two_same_net_connections_place_vias_clear_of_each_other_and_of_pads(tmp_path) -> None:
    """Two connections on the SAME net, 1.2 mm apart in a 4 mm-wide strip - so
    their vias compete for the same scarce free copper and the same-net
    exemption offers no protection. Pre-fix, connection 2 parked both of its
    vias dead centre in pads P3/P4 (measured via-to-pad distance: 0.0 mm)."""
    pads = [("P1", 1.4, 4.0), ("P2", 1.4, 16.0), ("P3", 2.6, 4.0), ("P4", 2.6, 16.0)]
    proj = _strip_project(tmp_path / "pair", pads)
    res = router.route_nets(proj, connections=[_conn(1.4), _conn(2.6)], write=True)
    for rec in res["connections"]:
        if rec["routed"]:
            assert rec["self_check"]["passed"] is True
    vias = _assert_via_placement_is_sane(proj, pads)
    assert len(vias) >= 4, "both connections had to cross the wall"


def test_via_forced_into_same_net_pad_is_rejected_by_default_accepted_when_opted_in(
        tmp_path) -> None:
    """The gate itself: the goal pad P2 is fenced in by a GND box on F.Cu whose
    interior is too small to hold a via anywhere except inside the pad. Default
    (`allow_via_in_pad: false`) must REFUSE the connection rather than silently
    drilling into the pad; `true` must route the identical board."""
    bx, by, half = 2.0, 16.0, 1.2
    fence = (_wall(bx - half, by - half, bx + half, by - half, "F.Cu", "vps-box-t")
             + _wall(bx - half, by + half, bx + half, by + half, "F.Cu", "vps-box-b")
             + _wall(bx - half, by - half, bx - half, by + half, "F.Cu", "vps-box-l")
             + _wall(bx + half, by - half, bx + half, by + half, "F.Cu", "vps-box-r"))
    pads = [("P1", 2.0, 4.0), ("P2", bx, by)]

    strict = router.route_nets(
        _strip_project(tmp_path / "strict", pads, extra=fence, allow_via_in_pad=False),
        connections=[_conn(2.0)], write=False)["connections"][0]
    opted = router.route_nets(
        _strip_project(tmp_path / "opted", pads, extra=fence, allow_via_in_pad=True),
        connections=[_conn(2.0)], write=False)["connections"][0]

    assert strict["routed"] is False, (
        "a via whose only legal spot is inside a same-net pad must be rejected "
        "when allow_via_in_pad is off")
    assert opted["routed"] is True and opted["self_check"]["passed"] is True
    assert opted["via_count"] >= 1


def test_flag_is_read_from_pcb_settings_and_defaults_to_safe(tmp_path) -> None:
    pads = [("P1", 2.0, 4.0), ("P2", 2.0, 16.0)]
    for flag in (False, True):
        proj = _strip_project(tmp_path / f"flag{int(flag)}", pads, allow_via_in_pad=flag)
        settings = pcb.load_pcb_settings(proj)["config"]
        assert settings["autorouter"]["allow_via_in_pad"] is flag
        res = router.route_nets(proj, connections=[_conn(2.0)], write=False)
        assert res["connections"][0]["self_check"]["passed"] is True
    # the shipped default is the SAFE one.
    assert pcb.DEFAULT_PCB_SETTINGS["autorouter"]["allow_via_in_pad"] is False


