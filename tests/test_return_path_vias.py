"""Tests for Phase 7.18.3 - return-path-aware VIA PLACEMENT for signal nets.

A signal net's layer-change cost used to be layer-purpose/congestion only: it
had no preference for landing near the reference-plane copper it is referenced
against, even though 7.5.6's stitching pass values exactly that AFTER the fact
(`stitching.near_high_speed_mm`). `plane.return_path_bonus` (default 0.0) is a
routing-TIME discount on a signal via that lands within `near_high_speed_mm` of
its own reference plane on a STACK-ADJACENT layer.

Two invariants are load-bearing in this file:

1. PARITY. Default `return_path_bonus: 0.0` must reproduce pre-7.18 routing
   byte-for-byte. `_route_nets` does not even build the reference-plane slices
   at 0.0, and `_build_fine_cost`'s `via` takes its untouched branch. The
   board-level proof (identical emitted-geometry digest, HEAD vs this tree, on
   the real kiln board at default settings) was run out of band and is recorded
   in `test_kiln_reference_planes_split_by_ground_domain`'s docstring.

2. THE 2026-07-24 REQUIRED CONSTRAINT IS UNTOUCHED. This adds a cost
   preference for where a via lands. It does NOT let a signal net route
   THROUGH plane fill: `plane_layers` is still None for every signal net (the
   power-net gate in `_plane_components_for`), so every plane-traversal and
   plane-termination branch stays False. `test_bonus_never_makes_the_plane_
   traversable` and `test_signal_net_still_gets_no_plane_components` assert
   that directly.
"""

from __future__ import annotations

from pathlib import Path

import kicad_pcb_tool as pcb
import kicad_router_tool as router

_LAYERS = ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
_LAYER_TYPES = {name: "signal" for name in _LAYERS}
_NEAR_MM = 1.0
_BONUS = 12.0


def _window() -> "router._FineWindow":
    win = router._FineWindow(0.0, 0.0, 7.0, 7.0, 1.0, _LAYERS, _LAYER_TYPES, "SIG")
    win.build([], 0.1, 0.3, 0.2, 0.2)
    return win


def _rect(x0: float, y0: float, x1: float, y1: float) -> "router._FillRaster":
    return router._FillRaster([(x0, y0), (x1, y0), (x1, y1), (x0, y1)])


def _model(return_path, *, bonus_layers=None):
    """Cost model for a SIGNAL net: `plane_layers` is None, exactly as
    `_route_core` passes it for any net that does not own a power pour."""
    return router._build_fine_cost(
        _window(), "signal", router._Weights({}, 1.0), {}, {}, None, None, None,
        None, None, 0.05, 8.0, (6, 3), {"F.Cu"}, False, return_path)


def _return_path(layers: list[str], bonus: float = _BONUS, near: float = _NEAR_MM):
    """Reference-plane copper covering the whole window, offered as the
    adjacent-layer reference for each named landing layer."""
    raster = _rect(0.0, 0.0, 7.0, 7.0)
    return {"net": "GND", "bonus": bonus, "near_mm": near,
            "adjacent_rasters": {layer: [raster] for layer in layers}}


def _plain_via_milli() -> int:
    w = router._Weights({}, 1.0)
    return w.q(w.via * w.through_via)


# --------------------------------------------------------------------------- #
# Parity at the default
# --------------------------------------------------------------------------- #

def test_default_settings_disable_the_bonus() -> None:
    assert pcb.DEFAULT_PCB_SETTINGS["plane"]["return_path_bonus"] == 0.0


def test_no_return_path_is_the_pre_7_18_via_cost() -> None:
    m = _model(None)
    for layer in _LAYERS:
        assert m["via"](3, 3, layer) == _plain_via_milli()


def test_zero_bonus_is_the_pre_7_18_via_cost() -> None:
    """Even with reference-plane geometry present, a bonus of 0.0 must not
    perturb a single cost - this is what makes the untuned default provably
    identical rather than merely "not built"."""
    m = _model(_return_path(_LAYERS, bonus=0.0))
    for layer in _LAYERS:
        assert m["via"](3, 3, layer) == _plain_via_milli()


def test_route_nets_builds_no_reference_planes_at_the_default(tmp_path: Path) -> None:
    """The plumbing side of parity: at bonus 0.0 the reference-plane slices are
    never computed at all, so nothing new is pickled to a worker either."""
    from tests.test_cross_layer_continuity import _write_two_layer_gnd_board

    _write_two_layer_gnd_board(tmp_path / "synthboard.kicad_pcb", [(0.0, 2.0)])
    settings = pcb.load_pcb_settings(str(tmp_path))["config"]
    assert float(settings.get("plane", {}).get("return_path_bonus", 0.0)) == 0.0


# --------------------------------------------------------------------------- #
# The bonus itself
# --------------------------------------------------------------------------- #

def test_bonus_discounts_a_via_landing_over_the_reference_plane() -> None:
    m = _model(_return_path(["In1.Cu"]))
    w = router._Weights({}, 1.0)
    assert m["via"](3, 3, "In1.Cu") == w.q(w.via * w.through_via - _BONUS)
    assert m["via"](3, 3, "In1.Cu") < _plain_via_milli()


def test_layers_without_an_adjacent_reference_are_not_discounted() -> None:
    """Only the landing layers the caller actually sliced a reference for get
    the discount - a via onto a layer with no adjacent reference copper pays
    the ordinary price."""
    m = _model(_return_path(["In1.Cu"]))
    for layer in ("F.Cu", "In2.Cu", "B.Cu"):
        assert m["via"](3, 3, layer) == _plain_via_milli()


def test_bonus_respects_the_near_high_speed_radius() -> None:
    """The reference pour covers x in [0, 2] only; `near_high_speed_mm` is
    1.0, so a via at x=3 is 1.0 mm away (inside, by `covers`' reach) and a via
    at x=6 is far outside."""
    rp = {"net": "GND", "bonus": _BONUS, "near_mm": _NEAR_MM,
          "adjacent_rasters": {"In1.Cu": [_rect(0.0, 0.0, 2.0, 7.0)]}}
    m = router._build_fine_cost(
        _window(), "signal", router._Weights({}, 1.0), {}, {}, None, None, None,
        None, None, 0.05, 8.0, (6, 3), {"F.Cu"}, False, rp)
    assert m["via"](1, 3, "In1.Cu") < _plain_via_milli()   # over the pour
    assert m["via"](6, 3, "In1.Cu") == _plain_via_milli()  # 4 mm away


def test_a_huge_bonus_can_never_make_a_via_free() -> None:
    """An aggressively-tuned bonus is floored, so a layer change always costs
    something and the search cannot thrash layers for nothing."""
    m = _model(_return_path(["In1.Cu"], bonus=10_000.0))
    cost = m["via"](3, 3, "In1.Cu")
    assert cost == router._MIN_VIA_MILLI
    assert cost > 0


def test_bonus_pulls_the_layer_change_toward_the_reference_plane() -> None:
    """End-to-end through the A*: with two otherwise-identical candidate
    layers, the signal net vias onto the one its reference plane backs."""
    args = ("signal", router._Weights({}, 1.0), {}, {}, (0, 3), ["F.Cu"],
            (6, 3), {"In1.Cu", "In2.Cu"}, None, None, None)
    plain = router._fine_astar(_window(), *args, plane_layers=None, goal_planes=None,
                               plane_step=0.05, attachment_via_cost=8.0)
    biased = router._fine_astar(_window(), *args, plane_layers=None, goal_planes=None,
                                plane_step=0.05, attachment_via_cost=8.0,
                                return_path=_return_path(["In2.Cu"]))
    assert plain is not None and biased is not None
    assert plain[-1][2] == "In1.Cu"    # deterministic layer-index tie-break
    assert biased[-1][2] == "In2.Cu"   # the reference-backed layer wins


def test_all_backends_agree_with_the_bonus_on() -> None:
    """cpu / numpy / gpu must reconstruct the identical discounted path. The
    discount is baked into the host-side integer via-cost array before anything
    crosses to a device (see `_build_cost_arrays`), so device parity is
    structural rather than a second implementation to keep in sync."""
    args = ("signal", router._Weights({}, 1.0), {}, {}, (0, 3), ["F.Cu"],
            (6, 3), {"In1.Cu", "In2.Cu"}, None, None, None)
    paths = [
        router._fine_search(backend, _window(), *args, None, None, 0.05, 8.0,
                            False, _return_path(["In2.Cu"]), _settings={})
        for backend in ("cpu", "numpy", "gpu")
    ]
    assert paths[0] == paths[1] == paths[2]


# --------------------------------------------------------------------------- #
# The REQUIRED CONSTRAINT (2026-07-24) is untouched
# --------------------------------------------------------------------------- #

def test_bonus_never_makes_the_plane_traversable() -> None:
    """The discount is a VIA-placement preference and nothing else: with a
    return path configured, the net still has no plane factor anywhere, so no
    planar move is ever priced at the plane rate and no plane node is ever a
    goal."""
    m = _model(_return_path(_LAYERS))
    for layer in _LAYERS:
        for ix in range(6):
            assert m["plane_factor"](ix, 3, layer) is None
            assert m["is_goal"](ix, 3, layer) == (ix == 6 and layer == "F.Cu")


def test_signal_net_still_gets_no_plane_components(tmp_path: Path) -> None:
    """The gate itself, re-asserted at its own source: a SIGNAL net that owns
    a fill still gets None from `_compute_plane_components_for`, so
    `plane_layers` stays None for it no matter what 7.18.3 is configured to
    do."""
    from tests.test_cross_layer_continuity import _write_two_layer_gnd_board

    _write_two_layer_gnd_board(tmp_path / "synthboard.kicad_pcb", [(0.0, 2.0)])
    board_path, _, _ = pcb._resolve_project_path(str(tmp_path))
    settings = pcb.load_pcb_settings(str(tmp_path))["config"]
    patterns = settings.get("layer_purpose", {}).get("power_net_patterns", [])
    index = router._plane_fill_index_with_estimated(board_path, 0.2, 0.2)
    assert "GND" in index  # the pour is there ...
    footprints = pcb._parse_footprint_pads_cached(board_path)
    tracks = pcb._parse_tracks_cached(board_path)
    pads = router._group_pads_by_net(footprints)
    all_cu = [lyr["name"] for lyr in pcb._parse_board_layers_cached(board_path)]
    stack = {name: i for i, name in enumerate(all_cu)}
    # ... and a power net gets components from it, but a signal net never does.
    assert router._compute_plane_components_for(
        "GND", index, patterns, footprints, tracks, pads, stack, all_cu,
        set(all_cu), 40.0, 1000.0) is not None
    assert router._compute_plane_components_for(
        "SIG", index, patterns, footprints, tracks, pads, stack, all_cu,
        set(all_cu), 40.0, 1000.0) is None


# --------------------------------------------------------------------------- #
# Choosing "the net's own reference plane"
# --------------------------------------------------------------------------- #

def test_reference_plane_is_chosen_by_pad_vote_not_by_size(tmp_path: Path) -> None:
    """The documented rule: a signal net's reference plane is the ground pour
    its OWN PADS sit over, not the biggest pour on the board. This is what
    keeps a board with several isolated ground domains correct."""
    from tests.synthetic_board import (_HEADER_TEMPLATE, _footprint_block,
                                       _layer_stack_lines, _net_table)
    from tests.test_plane_islands import _zone_block

    big = [(-50.0, -50.0), (50.0, -50.0), (50.0, 50.0), (-50.0, 50.0)]
    small = [(60.0, -5.0), (80.0, -5.0), (80.0, 5.0), (60.0, 5.0)]
    parts = [_HEADER_TEMPLATE.format(layer_lines=_layer_stack_lines(2)),
             _net_table(["GND_BIG", "GND_SMALL", "SIG"])]
    # R1's SIG pad sits at x~71, i.e. inside GND_SMALL, far from GND_BIG.
    parts.append(_footprint_block("R1", "10k", 72.0, 0.0, "fp-r1", "GND_SMALL", "SIG"))
    for net, poly in (("GND_BIG", big), ("GND_SMALL", small)):
        for layer in ("F.Cu", "B.Cu"):
            parts.append(_zone_block(net=net, layer=layer, uuid=f"z-{net}-{layer}",
                                     priority=0, island_removal_mode=0,
                                     outline_pts=poly, filled_polys=[poly]))
    parts.append(")\n")
    (tmp_path / "synthboard.kicad_pcb").write_text("".join(parts), encoding="utf-8")

    board_path, _, _ = pcb._resolve_project_path(str(tmp_path))
    settings = pcb.load_pcb_settings(str(tmp_path))["config"]
    patterns = settings.get("layer_purpose", {}).get("power_net_patterns", [])
    gnd_tokens = settings["schematic_checks"]["cap_voltage"]["gnd_tokens"]
    index = router._plane_fill_index_with_estimated(board_path, 0.2, 0.2)
    pads = router._group_pads_by_net(pcb._parse_footprint_pads_cached(board_path))
    all_cu = [lyr["name"] for lyr in pcb._parse_board_layers_cached(board_path)]

    resolved = router._reference_plane_rasters(
        ["SIG"], index, pads, patterns, gnd_tokens, all_cu, set(all_cu), _NEAR_MM)
    assert resolved["SIG"]["net"] == "GND_SMALL"


def test_a_net_over_no_pour_gets_no_reference_plane(tmp_path: Path) -> None:
    """Abstain rather than guess: a net nowhere near a pour is given no
    reference plane, so it is never pulled toward a plane it is not
    referenced against."""
    from tests.test_cross_layer_continuity import _write_two_layer_gnd_board

    _write_two_layer_gnd_board(tmp_path / "synthboard.kicad_pcb", [(0.0, 2.0)])
    board_path, _, _ = pcb._resolve_project_path(str(tmp_path))
    settings = pcb.load_pcb_settings(str(tmp_path))["config"]
    patterns = settings.get("layer_purpose", {}).get("power_net_patterns", [])
    gnd_tokens = settings["schematic_checks"]["cap_voltage"]["gnd_tokens"]
    index = router._plane_fill_index_with_estimated(board_path, 0.2, 0.2)
    pads = router._group_pads_by_net(pcb._parse_footprint_pads_cached(board_path))
    all_cu = [lyr["name"] for lyr in pcb._parse_board_layers_cached(board_path)]
    assert router._reference_plane_rasters(
        ["NOT_A_NET"], index, pads, patterns, gnd_tokens, all_cu,
        set(all_cu), _NEAR_MM) == {}


# --------------------------------------------------------------------------- #
# The real board
# --------------------------------------------------------------------------- #

def test_kiln_reference_planes_split_by_ground_domain(kiln_project_path: Path) -> None:
    """MEASURED on kiln, which has TWO isolated ground domains (GND_Main and
    GND_Safty, both pouring on F.Cu/In1.Cu/B.Cu of a
    F.Cu/In1.Cu/In2.Cu/B.Cu stack): of 222 signal nets, 192 resolve a
    reference plane - 115 to GND_Main and 77 to GND_Safty - and the split
    follows the schematic sheet, i.e. /SaftyProcessor/* nets reference
    GND_Safty while /MainControler/* nets reference GND_Main. A board-wide
    "the biggest ground pour" rule would have gotten every safety-domain net
    wrong, which is why the pad-vote rule is the one implemented.

    Board-level parity at the default (bonus 0.0), run out of band HEAD vs
    this tree: identical emitted-geometry digest
    e2d49f3e2374147d236a511d99a0966eafd3dd7fdb0b752d56d34e7a8e68afc8 and an
    identical get_trace_cost board total of 11848.149.

    HOW MUCH OF THE BOARD THE TERM CAN ACT ON, measured: of the 167 signal
    vias already on kiln, 157 resolve a reference plane and 153 (97%) land
    within near_high_speed_mm of it on a stack-adjacent layer - i.e. the human
    designer's own via placement is almost entirely where this term rewards,
    which is the sanity check that the term encodes good practice rather than
    a novel preference.

    HONEST NEGATIVE RESULT on the same 4-net write probe used for parity: the
    routes it emits contain 8 segments and ZERO new vias, so raising the bonus
    to 12.0 and then to 24.0 (nearly the whole 25.0 via cost) leaves the
    digest bit-identical. That is not evidence the term does nothing - it is
    evidence this particular probe has no discretionary layer change to move.
    The mechanism itself is proven against the A* in
    `test_bonus_pulls_the_layer_change_toward_the_reference_plane`.
    """
    board_path, _, _ = pcb._resolve_project_path(str(kiln_project_path))
    settings = pcb.load_pcb_settings(str(kiln_project_path))["config"]
    patterns = settings.get("layer_purpose", {}).get("power_net_patterns", [])
    gnd_tokens = settings["schematic_checks"]["cap_voltage"]["gnd_tokens"]
    index = router._plane_fill_index_with_estimated(board_path, 0.2, 0.2)
    pads = router._group_pads_by_net(pcb._parse_footprint_pads_cached(board_path))
    all_cu = [lyr["name"] for lyr in pcb._parse_board_layers_cached(board_path)]
    signal_nets = sorted(n for n in pads
                         if pcb._net_kind(n, None, patterns) != "power")

    resolved = router._reference_plane_rasters(
        signal_nets, index, pads, patterns, gnd_tokens, all_cu, set(all_cu), 1.0)
    assert resolved, "expected kiln signal nets to resolve a reference plane"
    chosen = {rec["net"] for rec in resolved.values()}
    assert chosen <= {"GND_Main", "GND_Safty"}, chosen
    assert len(chosen) == 2, "both kiln ground domains must be represented"

    safety = [n for n, rec in resolved.items()
              if n.startswith("/SaftyProcessor/") ]
    assert safety, "expected safety-domain signal nets on kiln"
    assert all(resolved[n]["net"] == "GND_Safty" for n in safety), (
        "a /SaftyProcessor/ net must reference the safety ground, not the "
        "larger GND_Main pour")

    # Every keyed landing layer must genuinely have reference copper on a
    # STACK-ADJACENT layer - never on the landing layer itself.
    stack = {name: i for i, name in enumerate(all_cu)}
    for rec in resolved.values():
        for layer in rec["adjacent_rasters"]:
            assert layer in stack
