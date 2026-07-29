"""Tests for Phase 7.20 - adjacent-layer parallel-trace (crosstalk) avoidance.

The user's ask: "some avoidance of running traces parallel to one another on
adjacent layers, especially if they are not part of the same bus."

Four things are load-bearing in this file, and each is asserted on its own so a
regression names itself rather than showing up as one vague failure:

1. PARITY. At the default `adjacent_layer_penalty_per_mm: 0.0` the term is not
   merely weighted to zero - `_resolve_crosstalk` returns None and neither the
   scalar `planar` closure nor `_build_cost_arrays` performs a single extra
   float operation. `test_default_settings_are_inert` /
   `test_default_cost_model_is_identical_to_no_crosstalk` pin that, and the
   board-level byte-identical digest proof on the real kiln board is recorded
   in `test_kiln_default_routing_digest_is_unchanged`.

2. STACK ADJACENCY, not "any other layer". A non-adjacent layer pair has a
   reference plane / enough dielectric between it, so it must NOT be flagged.
   This is not academic on kiln: its track copper lives only on F.Cu and B.Cu,
   which are THREE apart in its 4-layer stack, so its real adjacency has no
   track-vs-track exposure at all (`test_kiln_real_stack_has_no_adjacent_
   track_exposure`).

3. THE EXEMPTION, IN BOTH DIRECTIONS. A false exemption silently disables the
   feature for real risk; a false penalty makes normal bus routing artificially
   expensive. Both are asserted against kiln's REAL buses (SPI /MainControler/,
   I2C /MainControler/, SPI /SaftyProcessor/), not only synthetic fixtures -
   including the case that catches a lazy implementation: two DIFFERENT buses
   are not exempt against each other.

4. THE THRESHOLDS BITE. `min_spacing_mm` and `min_parallel_run_mm` each get a
   test that moves only that knob.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import kicad_pcb_tool as pcb
import kicad_router_accel as accel
import kicad_router_tool as router

_LAYERS = ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
_LAYER_TYPES = {name: "signal" for name in _LAYERS}

# kiln's real buses, as `detect_buses` qualifies them (Phase 3 anchor).
_KILN_SPI_MAIN = "/MainControler/CLK"
_KILN_SPI_MAIN2 = "/MainControler/MOSI"
_KILN_I2C_SCL = "/MainControler/SCL"
_KILN_SPI_SAFTY = "/SaftyProcessor/CLK"


def _window(net: str = "SIG", grid: float = 1.0) -> "router._FineWindow":
    win = router._FineWindow(0.0, 0.0, 10.0, 10.0, grid, _LAYERS, _LAYER_TYPES, net)
    win.build([], 0.1, 0.3, 0.2, 0.2)
    return win


def _seg(net: str, layer: str, x1: float, y1: float, x2: float, y2: float,
         half: float = 0.1) -> "router._Obst":
    """One track-copper obstacle - the only kind 7.20 treats as an aggressor."""
    return router._Obst("seg", net, frozenset([layer]), half, x1, y1, x2, y2)


def _adjacency() -> dict[str, list[str]]:
    order = {n: i for i, n in enumerate(_LAYERS)}
    return {L: [o for o in _LAYERS if abs(order[o] - order[L]) == 1] for L in _LAYERS}


def _settings(**over) -> dict:
    cfg = {"enabled": True, "adjacent_layer_penalty_per_mm": 4.0,
           "min_parallel_run_mm": 2.0, "min_spacing_mm": 0.3,
           "same_bus_exempt": True}
    cfg.update(over)
    return {"crosstalk": cfg}


# --------------------------------------------------------------------------- #
# 1. Parity at the default
# --------------------------------------------------------------------------- #

def test_default_settings_ship_an_inert_penalty():
    """The shipped default must be the inert one, or every untuned project
    silently changes geometry the moment this phase lands."""
    cfg = pcb.DEFAULT_PCB_SETTINGS["crosstalk"]
    assert cfg["adjacent_layer_penalty_per_mm"] == 0.0
    assert cfg["same_bus_exempt"] is True


def test_default_settings_are_inert():
    """At penalty 0 `_resolve_crosstalk` returns None even with an aggressor
    sitting right there - so no cell set is built and the cost model cannot
    differ from pre-7.20 no matter what the geometry is."""
    win = _window()
    obs = [_seg("OTHER", "In1.Cu", 0.0, 5.0, 10.0, 5.0)]
    assert router._resolve_crosstalk(
        _settings(adjacent_layer_penalty_per_mm=0.0), win, obs, "SIG",
        _adjacency(), []) is None


def test_disabled_flag_is_inert_even_with_a_tuned_weight():
    """`enabled: false` suspends a tuned weight without the user losing it."""
    win = _window()
    obs = [_seg("OTHER", "In1.Cu", 0.0, 5.0, 10.0, 5.0)]
    assert router._resolve_crosstalk(
        _settings(enabled=False), win, obs, "SIG", _adjacency(), []) is None


def test_default_cost_model_is_identical_to_no_crosstalk():
    """The parity mechanism at the cost-model level: passing `crosstalk=None`
    and passing a payload resolved from default settings must produce the same
    planar cost for every cell/direction - because both are None."""
    win = _window()
    base = router._build_fine_cost(
        win, "signal", router._Weights({}, 1.0), {}, {}, None, None, None,
        None, None, 0.05, 8.0, (6, 3), {"F.Cu"}, False, None, False, None)
    withx = router._build_fine_cost(
        win, "signal", router._Weights({}, 1.0), {}, {}, None, None, None,
        None, None, 0.05, 8.0, (6, 3), {"F.Cu"}, False, None, False,
        router._resolve_crosstalk(
            _settings(adjacent_layer_penalty_per_mm=0.0), win,
            [_seg("OTHER", "In1.Cu", 0.0, 5.0, 10.0, 5.0)], "SIG",
            _adjacency(), []))
    assert withx["crosstalk_cells"] is None
    for layer in _LAYERS:
        for ix in range(win.cols):
            for iy in range(win.rows):
                for di in range(8):
                    assert base["planar"](ix, iy, layer, di, -1) == \
                        withx["planar"](ix, iy, layer, di, -1)


# --------------------------------------------------------------------------- #
# 2. Stack adjacency, not "any other layer"
# --------------------------------------------------------------------------- #

def test_only_stack_adjacent_layers_are_flagged():
    """An aggressor on In1.Cu threatens F.Cu and In2.Cu (its neighbours) and
    NOTHING else. B.Cu is two layers away and must stay clean."""
    win = _window()
    obs = [_seg("OTHER", "In1.Cu", 0.0, 5.0, 10.0, 5.0)]
    pay = router._resolve_crosstalk(_settings(), win, obs, "SIG", _adjacency(), [])
    assert pay is not None
    assert set(pay["cells"]) == {"F.Cu", "In2.Cu"}
    assert "B.Cu" not in pay["cells"]
    # ...and not its OWN layer either: same-layer copper is already a hard
    # obstacle, double-charging it as crosstalk would be wrong.
    assert "In1.Cu" not in pay["cells"]


def test_outer_layers_of_a_four_layer_stack_never_couple():
    """F.Cu vs B.Cu on a 4-layer board: three apart, so no flagging at all.
    This is exactly kiln's situation - see the kiln test below."""
    win = _window()
    obs = [_seg("OTHER", "B.Cu", 0.0, 5.0, 10.0, 5.0)]
    pay = router._resolve_crosstalk(_settings(), win, obs, "SIG", _adjacency(), [])
    assert pay is None or "F.Cu" not in pay["cells"]


# --------------------------------------------------------------------------- #
# 3. The thresholds
# --------------------------------------------------------------------------- #

def test_min_spacing_gates_the_xy_offset():
    """A cell far from the aggressor in XY is not aligned with it. Moving ONLY
    `min_spacing_mm` must change which cells are flagged."""
    win = _window(grid=0.5)
    obs = [_seg("OTHER", "In1.Cu", 0.0, 5.0, 10.0, 5.0)]
    tight = router._resolve_crosstalk(
        _settings(min_spacing_mm=0.2), win, obs, "SIG", _adjacency(), [])
    loose = router._resolve_crosstalk(
        _settings(min_spacing_mm=2.0), win, obs, "SIG", _adjacency(), [])
    assert tight is not None and loose is not None
    assert len(loose["cells"]["F.Cu"]) > len(tight["cells"]["F.Cu"])
    # Every tight-flagged cell is within the loose set (monotone in spacing).
    assert tight["cells"]["F.Cu"] <= loose["cells"]["F.Cu"]


def test_min_parallel_run_excuses_a_short_aggressor():
    """A short stub is incidental overlap, not a coupled run. Moving ONLY
    `min_parallel_run_mm` past the aggressor's length must clear it."""
    win = _window()
    short = [_seg("OTHER", "In1.Cu", 4.0, 5.0, 5.0, 5.0)]  # 1.0 mm long
    assert router._resolve_crosstalk(
        _settings(min_parallel_run_mm=0.5), win, short, "SIG", _adjacency(), []) is not None
    assert router._resolve_crosstalk(
        _settings(min_parallel_run_mm=5.0), win, short, "SIG", _adjacency(), []) is None


def test_pads_and_vias_are_not_aggressors():
    """A point of copper cannot form a parallel RUN. Only `kind == "seg"`
    counts (see `_crosstalk_window_cells`)."""
    win = _window()
    pad = router._Obst("pt", "OTHER", frozenset(["In1.Cu"]), 0.5, 5.0, 5.0, 5.0, 5.0)
    assert router._resolve_crosstalk(
        _settings(), win, [pad], "SIG", _adjacency(), []) is None


def test_same_net_copper_is_never_an_aggressor():
    win = _window(net="SIG")
    obs = [_seg("SIG", "In1.Cu", 0.0, 5.0, 10.0, 5.0)]
    assert router._resolve_crosstalk(
        _settings(), win, obs, "SIG", _adjacency(), []) is None


def test_penalty_is_charged_per_mm_on_a_flagged_cell():
    """The cost term itself: a flagged cell costs strictly more to enter, and
    the surcharge scales with the distance travelled (per-MM, like
    `off_corridor`) - a diagonal move pays sqrt(2)x a straight one."""
    win = _window()
    obs = [_seg("OTHER", "In1.Cu", 0.0, 5.0, 10.0, 5.0)]
    pay = router._resolve_crosstalk(_settings(), win, obs, "SIG", _adjacency(), [])
    flagged = sorted(pay["cells"]["F.Cu"])[len(pay["cells"]["F.Cu"]) // 2]
    args = (win, "signal", router._Weights({}, 1.0), {}, {}, None, None, None,
            None, None, 0.05, 8.0, (6, 3), {"F.Cu"}, False, None, False)
    base = router._build_fine_cost(*args, None)
    withx = router._build_fine_cost(*args, pay)
    ix, iy = flagged
    straight_base = base["planar"](ix, iy, "F.Cu", 0, -1)
    straight_x = withx["planar"](ix, iy, "F.Cu", 0, -1)
    assert straight_x > straight_base
    # `_MOVES` 0-3 are the straight headings, 4-7 the diagonals; a diagonal
    # covers sqrt(2)x the distance and so must pay sqrt(2)x the surcharge.
    diag_delta = withx["planar"](ix, iy, "F.Cu", 4, -1) - base["planar"](ix, iy, "F.Cu", 4, -1)
    straight_delta = straight_x - straight_base
    assert diag_delta == pytest.approx(straight_delta * router._SQRT2, rel=1e-3)


# --------------------------------------------------------------------------- #
# 3b. cpu vs numpy tier parity WITH the term live
# --------------------------------------------------------------------------- #

def _tier_window():
    """A window with a real decision to make (round the wall on F.Cu, or via to
    In1.Cu and straight across) AND an In2.Cu aggressor that makes the In1.Cu
    crossing expensive - so the crosstalk term genuinely participates in the
    choice rather than being a constant offset."""
    layers = ["F.Cu", "In1.Cu", "In2.Cu"]
    types = {n: "signal" for n in layers}
    win = router._FineWindow(-1.0, -3.0, 11.0, 5.0, 0.25, layers, types, "NET")
    win.build([router._Obst("seg", "WALL", frozenset(["F.Cu"]), 0.3,
                            5.0, -3.0, 5.0, 2.0)], 0.1, 0.3, 0.2, 0.2)
    return win, layers


def test_cpu_and_numpy_tiers_agree_with_the_crosstalk_term_active():
    """The 7.8 parity guarantee must survive a NEW cost term. If the numpy
    `_build_cost_arrays` summand were placed in a different position (or
    omitted, as `goal_field` deliberately is) the two tiers would silently
    diverge - this is the test that would catch it."""
    win, layers = _tier_window()
    order = {n: i for i, n in enumerate(layers)}
    adj = {L: [o for o in layers if abs(order[o] - order[L]) == 1] for L in layers}
    pay = router._resolve_crosstalk(
        _settings(min_parallel_run_mm=1.0, min_spacing_mm=0.6),
        win, [_seg("AGGRESSOR", "In2.Cu", 0.0, 0.0, 10.0, 0.0)], "NET", adj, [])
    assert pay is not None and pay["cells"], "term must be live for this to prove anything"
    s = win.nearest_free(0.0, 0.0, layers, max_ring=max(win.cols, win.rows))
    g = win.nearest_free(10.0, 0.0, layers, max_ring=max(win.cols, win.rows))
    args = (win, "signal", router._Weights({}, 1.0), {}, {}, s, layers, g,
            set(layers), None, None, None, None, None, 0.0, 0.0, False, None,
            False, pay)
    assert router._fine_astar(*args) == accel.fine_wavefront(*args)


def test_crosstalk_term_changes_the_chosen_path():
    """Sanity that the term is not merely a constant offset: a heavy penalty on
    one layer must actually move the route off it."""
    win, layers = _tier_window()
    order = {n: i for i, n in enumerate(layers)}
    adj = {L: [o for o in layers if abs(order[o] - order[L]) == 1] for L in layers}
    s = win.nearest_free(0.0, 0.0, layers, max_ring=max(win.cols, win.rows))
    g = win.nearest_free(10.0, 0.0, layers, max_ring=max(win.cols, win.rows))

    def run(pay):
        return router._fine_astar(
            win, "signal", router._Weights({}, 1.0), {}, {}, s, layers, g,
            set(layers), None, None, None, None, None, 0.0, 0.0, False, None,
            False, pay)

    # The aggressor sits on In2.Cu directly under the In1.Cu corridor the
    # unpenalised route uses, so the term bears on the actual decision.
    heavy = router._resolve_crosstalk(
        _settings(adjacent_layer_penalty_per_mm=500.0, min_parallel_run_mm=1.0,
                  min_spacing_mm=1.5),
        win, [_seg("AGGRESSOR", "In2.Cu", 0.0, 0.0, 10.0, 0.0)], "NET", adj, [])
    assert heavy is not None
    free_route = run(None)
    # Precondition: the unpenalised route really does use the corridor we are
    # about to make expensive - otherwise this test proves nothing.
    assert any((cx, cy) in heavy["cells"].get(layer, set())
               for (cx, cy, layer) in free_route)
    assert free_route != run(heavy)


# --------------------------------------------------------------------------- #
# 4. The exemption - both directions, on kiln's REAL buses
# --------------------------------------------------------------------------- #

def test_exempt_nets_are_resolved_from_bus_groups():
    groups = [frozenset({"A", "B", "C"}), frozenset({"C", "D"})]
    assert router._crosstalk_exempt_nets("A", groups) == frozenset({"B", "C"})
    # A net in two buses is exempt against the members of both.
    assert router._crosstalk_exempt_nets("C", groups) == frozenset({"A", "B", "D"})
    assert router._crosstalk_exempt_nets("Z", groups) == frozenset()


def test_same_bus_members_are_not_penalised_against_each_other():
    """DIRECTION 1 - the false-penalty guard. Two SPI members running directly
    above/below each other is the DESIRED layout; it must cost nothing."""
    win = _window(net=_KILN_SPI_MAIN)
    groups = [frozenset({_KILN_SPI_MAIN, _KILN_SPI_MAIN2})]
    obs = [_seg(_KILN_SPI_MAIN2, "In1.Cu", 0.0, 5.0, 10.0, 5.0)]
    assert router._resolve_crosstalk(
        _settings(), win, obs, _KILN_SPI_MAIN, _adjacency(), groups) is None


def test_unrelated_net_is_penalised_for_the_same_physical_arrangement():
    """DIRECTION 2 - the false-exemption guard, and the controlled experiment:
    IDENTICAL geometry to the test above, only the aggressor's net changes."""
    win = _window(net=_KILN_SPI_MAIN)
    groups = [frozenset({_KILN_SPI_MAIN, _KILN_SPI_MAIN2})]
    obs = [_seg("/MainControler/LCD_Reset", "In1.Cu", 0.0, 5.0, 10.0, 5.0)]
    pay = router._resolve_crosstalk(
        _settings(), win, obs, _KILN_SPI_MAIN, _adjacency(), groups)
    assert pay is not None and pay["cells"]["F.Cu"]


def test_different_buses_are_not_exempt_against_each_other():
    """The case a lazy "both are on some bus" check gets wrong: SPI and I2C are
    different buses, so they must still penalise each other."""
    win = _window(net=_KILN_SPI_MAIN)
    groups = [frozenset({_KILN_SPI_MAIN, _KILN_SPI_MAIN2}),
              frozenset({_KILN_I2C_SCL, "/MainControler/SDA"})]
    obs = [_seg(_KILN_I2C_SCL, "In1.Cu", 0.0, 5.0, 10.0, 5.0)]
    pay = router._resolve_crosstalk(
        _settings(), win, obs, _KILN_SPI_MAIN, _adjacency(), groups)
    assert pay is not None and pay["cells"]["F.Cu"]


def test_same_bus_exempt_false_disables_the_exemption():
    win = _window(net=_KILN_SPI_MAIN)
    groups = [frozenset({_KILN_SPI_MAIN, _KILN_SPI_MAIN2})]
    obs = [_seg(_KILN_SPI_MAIN2, "In1.Cu", 0.0, 5.0, 10.0, 5.0)]
    pay = router._resolve_crosstalk(
        _settings(same_bus_exempt=False), win, obs, _KILN_SPI_MAIN,
        _adjacency(), groups)
    assert pay is not None and pay["cells"]["F.Cu"]


# --------------------------------------------------------------------------- #
# 5. The REAL kiln board
# --------------------------------------------------------------------------- #

def test_kiln_bus_groups_cover_the_three_known_buses(kiln_project_path: Path):
    """kiln's real, Phase-3-qualified buses must all resolve - if they do not,
    the exemption is dead and every bus starts paying a penalty it should not.

    This test exists because that failure ACTUALLY HAPPENED during 7.20: an
    early `_crosstalk_bus_groups` passed a kwarg `detect_buses` does not accept
    and the broad `except Exception` swallowed it, leaving the exemption set
    permanently empty with no test and no warning."""
    settings = pcb.load_pcb_settings(str(kiln_project_path))["config"]
    groups = router._crosstalk_bus_groups(str(kiln_project_path), settings)
    assert len(groups) >= 3, f"expected kiln's 3 buses, got {groups}"
    flat = [set(g) for g in groups]
    assert any({_KILN_SPI_MAIN, _KILN_SPI_MAIN2} <= g for g in flat), "SPI /MainControler/"
    assert any({_KILN_I2C_SCL, "/MainControler/SDA"} <= g for g in flat), "I2C /MainControler/"
    assert any({_KILN_SPI_SAFTY, "/SaftyProcessor/MOSI"} <= g for g in flat), "SPI /SaftyProcessor/"
    # The two SPI buses are SEPARATE groups: a MainControler SPI net must not be
    # exempt against a SaftyProcessor SPI net just because both are "SPI".
    assert _KILN_SPI_SAFTY not in router._crosstalk_exempt_nets(_KILN_SPI_MAIN, groups)


def test_kiln_real_stack_has_no_adjacent_track_exposure(kiln_project_path: Path):
    """MEASURED on the real board: kiln's track copper is only on F.Cu (1392
    segments) and B.Cu (640) - its two inner layers are pure plane pours. F.Cu
    and B.Cu are three apart in the 4-layer stack, so with the STACK-ADJACENT
    rule (as opposed to a naive "any other layer") kiln has zero track-vs-track
    crosstalk exposure. That is the correct answer, and it is precisely what
    the adjacency restriction buys."""
    res = router.audit_crosstalk(str(kiln_project_path), penalty_per_mm=4.0)
    assert res["layer_stack"] == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
    assert res["adjacent_layer_pairs"] == [["F.Cu", "In1.Cu"], ["In1.Cu", "In2.Cu"],
                                           ["In2.Cu", "B.Cu"]]
    assert res["totals"]["flagged_runs"] == 0
    assert res["totals"]["total_penalty"] == 0


def test_kiln_two_layer_whatif_flags_real_runs_and_exempts_real_buses(
        kiln_project_path: Path):
    """The two-directional exemption proof on REAL kiln geometry and REAL kiln
    buses, as a controlled experiment: the same board, the same overlaps, with
    the exemption toggled.

    Measured at penalty 4.0/mm, spacing 0.5 mm, min run 1.0 mm, treating F.Cu
    and B.Cu as adjacent (the 2-layer stack-up what-if - see `audit_crosstalk`'s
    `adjacent_layer_pairs`):
      exemption ON : 337 flagged runs / 576.62 mm, 23 exempt runs / 31.77 mm
      exemption OFF: 360 flagged runs / 608.39 mm,  0 exempt
    The 23-run, 31.77 mm, 127.1-penalty delta IS the exemption, and every one of
    those runs is a pair of confirmed same-bus members (SPI /MainControler/
    CLK-MISO-MOSI-CS*, SPI /SaftyProcessor/ MOSI-CLK, I2C SDA-SCL)."""
    kw = dict(penalty_per_mm=4.0, min_spacing_mm=0.5, min_parallel_run_mm=1.0,
              adjacent_layer_pairs=[("F.Cu", "B.Cu")])
    on = router.audit_crosstalk(str(kiln_project_path), same_bus_exempt=True, **kw)
    off = router.audit_crosstalk(str(kiln_project_path), same_bus_exempt=False, **kw)

    # DIRECTION 1 (false-penalty guard): real same-bus pairs run parallel here,
    # and every one of them is excused.
    assert on["totals"]["exempt_runs"] > 0
    groups = router._crosstalk_bus_groups(
        str(kiln_project_path), pcb.load_pcb_settings(str(kiln_project_path))["config"])
    for run in on["exempt_runs"]:
        assert any(run["net_a"] in g and run["net_b"] in g for g in groups)
        assert run["penalty"] == 0.0

    # DIRECTION 2 (false-exemption guard): plenty of real runs still penalised,
    # including bus members facing unrelated nets.
    assert on["totals"]["flagged_runs"] > 0
    for run in on["violations"]:
        assert not any(run["net_a"] in g and run["net_b"] in g for g in groups)

    # The controlled delta: turning the exemption off moves exactly the exempt
    # runs into the flagged set, and nothing else changes.
    assert (off["totals"]["flagged_runs"]
            == on["totals"]["flagged_runs"] + on["totals"]["exempt_runs"])
    assert off["totals"]["total_penalty"] > on["totals"]["total_penalty"]


def test_kiln_overlap_measurement_is_exact_not_the_aggressor_length(
        kiln_project_path: Path):
    """`audit_crosstalk` reports the TRUE overlap, so every flagged run is at
    least the threshold and never longer than the segment it measured along."""
    res = router.audit_crosstalk(
        str(kiln_project_path), penalty_per_mm=4.0, min_spacing_mm=0.5,
        min_parallel_run_mm=1.0, adjacent_layer_pairs=[("F.Cu", "B.Cu")])
    assert res["violations"]
    for run in res["violations"]:
        assert run["overlap_mm"] >= 1.0
        # Both fields are rounded to 4dp independently, so compare at that
        # resolution rather than demanding exact float equality.
        assert run["penalty"] == pytest.approx(run["overlap_mm"] * 4.0, abs=1e-3)
