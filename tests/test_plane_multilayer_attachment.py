"""Tests for Phase 7.18.1 - multi-layer plane attachment CHOICE.

What changed: `_build_fine_cost` used to (a) take the FIRST of this net's own
fill components covering a cell as "the" component there, and (b) charge the
same flat `plane.attachment_via` for a via landing on the plane no matter WHICH
component it landed on. With `plane.multilayer_attachment_choice: true` it (a)
takes the cheapest covering component and (b) scales the attachment surcharge by
that component's island factor - which is what turns the A*'s existing per-layer
expansion into a real RANKING across every layer a power net owns fill on
(mainland on In1.Cu genuinely out-prices a one-attachment island on F.Cu at the
same x/y, where before both cost exactly 8.0).

PARITY is the load-bearing assertion here, exactly as in
`test_pad_escape_direction_aware.py`: this is a change to an EXISTING decision
point that every plane-owning connection passes through, so the flag defaults
False and the flag-OFF cost model must reproduce the pre-7.18 numbers term for
term. `test_flag_off_via_cost_is_the_pre_7_18_formula` /
`test_flag_off_plane_factor_is_first_found` are that proof at the cost-model
level; the board-level proof (identical emitted-geometry digest on the real
kiln board, HEAD vs this tree, at default settings) was run out of band and is
recorded in `test_kiln_multilayer_attachment_is_not_vacuous`'s docstring.
"""

from __future__ import annotations

from pathlib import Path

import kicad_pcb_tool as pcb
import kicad_router_tool as router

_LAYER_TYPES_2 = {"F.Cu": "signal", "B.Cu": "signal"}
_ATTACH = 8.0
_PLANE_STEP = 0.05


def _window(cols: int = 6, rows: int = 6) -> "router._FineWindow":
    win = router._FineWindow(0.0, 0.0, float(cols - 1), float(rows - 1), 1.0,
                             ["F.Cu", "B.Cu"], _LAYER_TYPES_2, "GND")
    win.build([], 0.1, 0.3, 0.2, 0.2)
    return win


def _rect(x0: float, y0: float, x1: float, y1: float) -> "router._FillRaster":
    return router._FillRaster([(x0, y0), (x1, y0), (x1, y1), (x0, y1)])


def _model(win, plane_layers, *, multilayer: bool):
    return router._build_fine_cost(
        win, "power", router._Weights({}, 1.0), {}, {}, None, None, None,
        plane_layers, None, _PLANE_STEP, _ATTACH, (0, 0), {"F.Cu"},
        multilayer, None)


def _overlapping_layer() -> dict:
    """One layer carrying TWO same-net components that overlap: the first in
    `_plane_components_for` order is the mainland (factor 1.0), the second is a
    heavily-attached island whose factor (40/80 = 0.5) is actually CHEAPER."""
    return {"F.Cu": [{"raster": _rect(0.0, 0.0, 5.0, 5.0), "factor": 1.0},
                     {"raster": _rect(0.0, 0.0, 5.0, 5.0), "factor": 0.5}],
            "B.Cu": []}


def _two_layer_planes() -> dict:
    """The 7.18.1 scenario proper: the SAME net owns fill on both layers over
    the same area, mainland-quality on B.Cu and a weak island on F.Cu."""
    return {"F.Cu": [{"raster": _rect(0.0, 0.0, 5.0, 5.0), "factor": 40.0}],
            "B.Cu": [{"raster": _rect(0.0, 0.0, 5.0, 5.0), "factor": 1.0}]}


# --------------------------------------------------------------------------- #
# Parity: flag OFF must reproduce the pre-7.18 cost model exactly.
# --------------------------------------------------------------------------- #

def test_default_settings_keep_the_flag_off() -> None:
    """An untuned project must not opt into this: the byte-identical guarantee
    is what the default is for."""
    assert pcb.DEFAULT_PCB_SETTINGS["plane"]["multilayer_attachment_choice"] is False


def test_flag_off_plane_factor_is_first_found() -> None:
    win = _window()
    m = _model(win, _overlapping_layer(), multilayer=False)
    # Pre-7.18 behavior: the FIRST covering component wins, even though the
    # second one is cheaper.
    assert m["plane_factor"](2, 2, "F.Cu") == 1.0


def test_flag_off_via_cost_is_the_pre_7_18_formula() -> None:
    """Flag OFF: a via landing on the plane pays the flat attachment surcharge,
    identical whether it lands on a factor-1.0 mainland or a factor-40 island."""
    win = _window()
    w = router._Weights({}, 1.0)
    m = _model(win, _two_layer_planes(), multilayer=False)
    expected = w.q(w.via * w.through_via + _ATTACH)
    assert m["via"](2, 2, "F.Cu") == expected
    assert m["via"](2, 2, "B.Cu") == expected
    # ... and the two layers are therefore INDISTINGUISHABLE to the search,
    # which is exactly the gap 7.18.1 closes.
    assert m["via"](2, 2, "F.Cu") == m["via"](2, 2, "B.Cu")


def test_flag_off_off_plane_via_cost_is_untouched() -> None:
    """A cell with no plane copper prices identically either way - proving the
    change cannot leak into a signal net's cost model."""
    win = _window()
    w = router._Weights({}, 1.0)
    plain = w.q(w.via * w.through_via)
    for flag in (False, True):
        m = _model(win, None, multilayer=flag)
        assert m["via"](2, 2, "F.Cu") == plain


# --------------------------------------------------------------------------- #
# Flag ON: best-component choice + island-quality-scaled attachment.
# --------------------------------------------------------------------------- #

def test_flag_on_plane_factor_takes_the_cheapest_covering_component() -> None:
    win = _window()
    m = _model(win, _overlapping_layer(), multilayer=True)
    assert m["plane_factor"](2, 2, "F.Cu") == 0.5


def test_flag_on_via_cost_scales_with_island_quality() -> None:
    win = _window()
    w = router._Weights({}, 1.0)
    m = _model(win, _two_layer_planes(), multilayer=True)
    base = w.via * w.through_via
    assert m["via"](2, 2, "B.Cu") == w.q(base + _ATTACH * 1.0)
    assert m["via"](2, 2, "F.Cu") == w.q(base + _ATTACH * 40.0)
    # The ranking now exists: the healthy layer is strictly cheaper to land on.
    assert m["via"](2, 2, "B.Cu") < m["via"](2, 2, "F.Cu")


def test_flag_on_mainland_only_board_prices_identically() -> None:
    """A net owning exactly one healthy pour per layer (factor 1.0 everywhere)
    is priced the same either way - the flag only bites where island quality
    actually differs, so turning it on is not a blanket cost inflation."""
    win = _window()
    planes = {"F.Cu": [{"raster": _rect(0.0, 0.0, 5.0, 5.0), "factor": 1.0}],
              "B.Cu": [{"raster": _rect(0.0, 0.0, 5.0, 5.0), "factor": 1.0}]}
    off = _model(win, planes, multilayer=False)
    on = _model(win, planes, multilayer=True)
    for layer in ("F.Cu", "B.Cu"):
        for ix in range(4):
            assert off["via"](ix, 2, layer) == on["via"](ix, 2, layer)
            assert off["plane_factor"](ix, 2, layer) == on["plane_factor"](ix, 2, layer)


def _three_layer_window() -> "router._FineWindow":
    layers = ["F.Cu", "B.Cu", "In1.Cu"]
    types = {name: "signal" for name in layers}
    win = router._FineWindow(0.0, 0.0, 7.0, 7.0, 1.0, layers, types, "GND")
    win.build([], 0.1, 0.3, 0.2, 0.2)
    return win


def _astar_attachment_choice(*, multilayer: bool):
    """The net starts on F.Cu (no fill there) and owns fill on BOTH inner
    layers over the same area: a healthy mainland on In1.Cu and a
    one-attachment island on B.Cu. Reaching either plane completes the
    connection, so the ONLY decision the search makes is which layer to attach
    to - the exact decision point 7.18.1 changes."""
    win = _three_layer_window()
    island = {"raster": _rect(0.0, 0.0, 7.0, 7.0), "factor": 40.0}
    mainland = {"raster": _rect(0.0, 0.0, 7.0, 7.0), "factor": 1.0}
    planes = {"B.Cu": [island], "In1.Cu": [mainland]}
    return router._fine_astar(
        win, "power", router._Weights({}, 1.0), {}, {}, (0, 3), ["F.Cu"],
        # Empty `goal_layers`: the ONLY way to finish is to attach to one of
        # the two plane layers, so the search cannot dodge the decision by
        # walking to the goal cell on its start layer.
        (6, 3), set(), None, None, None,
        plane_layers=planes, goal_planes=planes,
        plane_step=_PLANE_STEP, attachment_via_cost=_ATTACH,
        multilayer_attachment=multilayer)


def test_flag_off_astar_cannot_tell_the_two_plane_layers_apart() -> None:
    """Pre-7.18: both attachments cost a flat 8.0, so the search is left with a
    pure tie and falls through to its deterministic layer-index tie-break -
    landing on the WEAK island purely because B.Cu sorts before In1.Cu."""
    path = _astar_attachment_choice(multilayer=False)
    assert path is not None
    assert path[-1][2] == "B.Cu"


def test_flag_on_astar_attaches_to_the_healthier_layer() -> None:
    """With 7.18.1 the same search prices the island attachment at 8.0 x 40 and
    the mainland at 8.0 x 1, so it lands on In1.Cu instead - the ranking, not
    the tie-break, now decides."""
    path = _astar_attachment_choice(multilayer=True)
    assert path is not None
    assert path[-1][2] == "In1.Cu"
    assert path != _astar_attachment_choice(multilayer=False)


def test_both_backends_agree_with_the_flag_on() -> None:
    """The 7.8 bit-identical-backends guarantee must survive 7.18.1: the numpy
    wavefront builds its via-cost array from the same model, so the scaled
    attachment surcharge has to reproduce the cpu A*'s decision exactly."""
    win_args = ("power", router._Weights({}, 1.0), {}, {}, (0, 3), ["F.Cu"],
                (6, 3), set(), None, None, None)
    island = {"raster": _rect(0.0, 0.0, 7.0, 7.0), "factor": 40.0}
    mainland = {"raster": _rect(0.0, 0.0, 7.0, 7.0), "factor": 1.0}
    planes = {"B.Cu": [island], "In1.Cu": [mainland]}
    for flag in (False, True):
        paths = [
            router._fine_search(backend, _three_layer_window(), *win_args,
                                planes, planes, _PLANE_STEP, _ATTACH, flag, None)
            for backend in ("cpu", "numpy")
        ]
        assert paths[0] == paths[1], f"backend divergence at flag={flag}"


# --------------------------------------------------------------------------- #
# The real board: is any of this non-vacuous on kiln?
# --------------------------------------------------------------------------- #

def test_kiln_multilayer_attachment_is_not_vacuous(kiln_project_path: Path) -> None:
    """MEASURED on the committed kiln board: GND_Main and GND_Safty each own
    fill on THREE copper layers (F.Cu/B.Cu/In1.Cu) with genuinely different
    island factors per layer, so 7.18.1 has real ranking decisions to make here
    - it is not a feature with no board to act on.

    Board-level parity (run out of band, HEAD vs this tree, `route_nets(nets=
    [/SaftyProcessor/Current3, GND_Main, GND_Safty, /MainControler/CLK],
    write=True)` on a copy of the live board, every emitted (segment)/(via)
    block digested in file order with uuids masked): identical digest
    e2d49f3e2374147d236a511d99a0966eafd3dd7fdb0b752d56d34e7a8e68afc8 and an
    identical `get_trace_cost` board total of 11848.149 at default settings.
    """
    board_path, _, _ = pcb._resolve_project_path(str(kiln_project_path))
    settings = pcb.load_pcb_settings(str(kiln_project_path))["config"]
    power_patterns = settings.get("layer_purpose", {}).get("power_net_patterns", [])
    index = router._plane_fill_index_with_estimated(board_path, 0.2, 0.2)

    multilayer_nets = {
        net: sorted({e["layer"] for e in entries})
        for net, entries in index.items()
        if len({e["layer"] for e in entries}) > 1
    }
    assert multilayer_nets, "kiln must have at least one multi-layer plane net"

    footprints = pcb._parse_footprint_pads_cached(board_path)
    tracks = pcb._parse_tracks_cached(board_path)
    pads_by_net = router._group_pads_by_net(footprints)
    all_cu = [lyr["name"] for lyr in pcb._parse_board_layers_cached(board_path)]
    stack = {name: i for i, name in enumerate(all_cu)}

    # A ranking decision exists wherever a net's fill covers ONE (x, y) on more
    # than one layer with DIFFERENT island factors - that is the point at which
    # pre-7.18's flat surcharge could not tell the layers apart. Sweep a coarse
    # board-wide grid and stop at the first one found (this is an existence
    # proof, not a census - the full 1 mm census measured 2984 such points for
    # GND_Main and 434 for GND_Safty on the live board).
    minx, miny, maxx, maxy = router._board_bbox(board_path)
    found: tuple[str, float, float, dict] | None = None
    for net in sorted(multilayer_nets):
        comps = router._compute_plane_components_for(
            net, index, power_patterns, footprints, tracks, pads_by_net,
            stack, all_cu, set(all_cu), 40.0, 1000.0)
        if not comps or len(comps) < 2:
            continue
        y = miny
        while y < maxy and found is None:
            x = minx
            while x < maxx:
                here: dict[str, float] = {}
                for layer, cs in comps.items():
                    for c in cs:
                        if c["raster"].covers(x, y, 0.0):
                            here[layer] = c["factor"]
                            break
                if len(here) > 1 and len({round(v, 6) for v in here.values()}) > 1:
                    found = (net, x, y, here)
                    break
                x += 2.0
            y += 2.0
        if found:
            break
    assert found is not None, (
        "no point on kiln has one net's fill on two layers at different island "
        "quality - 7.18.1 would be vacuous on this board")
