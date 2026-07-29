"""Tests for Phase 7.22 - bus-first direct routing pass.

The user's ask, verbatim: "when routing start with the busses in the most
direct line, they can be riped up and optimized later."

Four things are load-bearing here, and each gets its own assertion so a
regression names itself:

1. ORDERING. Every bus-member net sorts STRICTLY before every non-member in
   `route_nets`' worklist, and the pre-7.22 key survives verbatim behind that
   split - user `net_overrides.priority` still orders within each group, and
   shortest-airline-then-name still settles the rest. Both directions are
   asserted: a priority bump cannot lift a non-bus net above a bus net, and a
   bus net with no priority still beats a non-bus net that has one.

2. INERTNESS, which matters exactly as much as (1). On a board with no
   detected/confirmed bus the member set is empty and the sort takes the
   literal pre-7.22 key - proved by digesting the whole `route_nets` result
   against a `bus_first: false` run, not merely by eyeballing the net order.
   The directness knob has its own, stronger inertness proof: at the default
   `bus_first_direct_corridor_mm: 0.0` `_straight_line_corridor` is never
   CALLED at all (monkeypatched to raise), so it cannot be a zero-weighted
   term that happens to cancel.

3. THE DIRECTNESS KNOB ACTUALLY BITES. On a totally empty board the stock
   router still bows a bus net off its airline - not from congestion (there is
   none) but from the layer-direction preference: `_direction_factor` charges
   `off_direction` (2.0x) for a straight run against a layer's preferred axis
   while 45-degree diagonals are always neutral, so a pure against-axis run is
   cheaper as a diagonal V (ratio sqrt(2)) or as a via hop to the other layer.
   `test_direct_corridor_straightens_an_against_axis_bus_net` pins the
   measured before/after on exactly that case.

4. SCOPE. The straight-line corridor applies to the FIRST pass only. Rip-up
   re-routes call `_route_one` with `use_corridor=False`, and that path must
   never reach the 7.22 branch (`test_ripup_reroute_never_uses_the_direct_
   corridor`) - the phase deliberately adds NO conflict avoidance or
   lookahead; conflicts are reconciled later by rip-up and Phase 7.6.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import kicad_pcb_tool as pcb
import kicad_router_tool as router

from synthetic_board import write_multidrop_spi_project, write_synthetic_project


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _ladder(directory: Path, unrouted: int = 6):
    """A resistor-ladder board with `unrouted` real ratsnest connections and -
    verified by `test_ladder_fixture_has_no_detectable_bus` below - no bus of
    its own, so every bus in these tests is one the test itself declares."""
    return write_synthetic_project(directory, mode="ladder_partial",
                                   component_count=12, unrouted_count=unrouted)


def _confirm_bus(directory: Path, nets: list[str]) -> None:
    """Declare `nets` a confirmed bus in the board-local JSON - the
    authoritative one of `_crosstalk_bus_groups`' two sources."""
    pcb.save_board_local(directory, {
        "version": 1,
        "confirmed_buses": [{"bus_type": "SPI", "name": "TESTBUS", "nets": nets}],
    })


def _priorities(directory: Path, overrides: dict[str, float], buses: list[str] | None = None) -> None:
    data: dict = {"version": 1,
                  "net_overrides": {n: {"priority": p} for n, p in overrides.items()}}
    if buses:
        data["confirmed_buses"] = [{"bus_type": "SPI", "name": "TESTBUS", "nets": buses}]
    pcb.save_board_local(directory, data)


def _settings_file(directory: Path, autorouter: dict) -> None:
    (Path(directory) / "pcb_settings.json").write_text(
        json.dumps({"autorouter": autorouter}), encoding="utf-8")


def _order(project) -> list[str]:
    """The worklist order, as observed through `route_nets`' output - the
    result's `connections` are assembled in canonical owner order, and owner
    ids ARE the sorted worklist indices."""
    return [c["net"] for c in router.route_nets(project, write=False)["connections"]]


def _pre_722_order(project) -> list[str]:
    """The literal pre-7.22 sort key, recomputed here independently of the
    implementation so the inertness tests compare against the OLD behaviour
    rather than against the new code agreeing with itself."""
    conns = router.get_ratsnest(project)["connections"]
    return [c["net"] for c in sorted(conns, key=lambda c: (-float(c.get("priority", 0.0)),
                                                           float(c.get("airline_length_mm", 0.0)),
                                                           c.get("net", "")))]


def _digest(result: dict) -> str:
    """A stable digest of everything `route_nets` decided - geometry included."""
    return json.dumps(result["connections"], sort_keys=True, default=str)


# --------------------------------------------------------------------------- #
# 0. fixture sanity
# --------------------------------------------------------------------------- #

def test_ladder_fixture_has_no_detectable_bus(tmp_path):
    """Everything below depends on the ladder board being bus-FREE until a
    test declares a bus on it. If `detect_buses` ever starts qualifying
    `CHAIN_*`, the ordering tests would silently stop testing what they claim."""
    p = _ladder(tmp_path)
    settings = pcb.load_pcb_settings(p["project"])
    assert router._crosstalk_bus_groups(p["project"], settings) == []


# --------------------------------------------------------------------------- #
# 1. shipped defaults
# --------------------------------------------------------------------------- #

def test_default_settings_ship_bus_first_on_and_directness_off():
    """Ordering IS the deliverable, so it ships on; the directness knob moves
    geometry on every bus net it touches, so it ships off (the 7.18/7.19/7.20
    convention)."""
    autor = pcb.DEFAULT_PCB_SETTINGS["autorouter"]
    assert autor["bus_first"] is True
    assert autor["bus_first_direct_corridor_mm"] == 0.0


# --------------------------------------------------------------------------- #
# 2. `_bus_member_nets` - the membership flattening
# --------------------------------------------------------------------------- #

def test_bus_member_nets_unions_every_group():
    groups = [frozenset({"A", "B"}), frozenset({"B", "C"})]
    assert router._bus_member_nets(groups) == frozenset({"A", "B", "C"})


def test_bus_member_nets_of_no_groups_is_empty():
    """The inert case: no groups in, nothing promoted out. This is also the
    shape `_crosstalk_bus_groups` degrades to when either source is
    unreadable, which is what makes a lookup failure demote nets out of the
    early slot instead of silently promoting them into it."""
    assert router._bus_member_nets([]) == frozenset()


# --------------------------------------------------------------------------- #
# 3. ordering
# --------------------------------------------------------------------------- #

def test_bus_nets_route_before_non_bus_nets(tmp_path):
    p = _ladder(tmp_path)
    _confirm_bus(tmp_path, ["CHAIN_9", "CHAIN_11"])
    order = _order(p["project"])
    # every airline on this fixture is 3.5 mm and no net has a priority, so
    # both groups fall through to the name tie-break (string order, hence
    # CHAIN_10 before CHAIN_6 and CHAIN_11 before CHAIN_9).
    assert order == ["CHAIN_11", "CHAIN_9",
                     "CHAIN_10", "CHAIN_6", "CHAIN_7", "CHAIN_8"]


def test_bus_group_is_internally_ordered_by_user_priority(tmp_path):
    """Within the bus group the pre-7.22 key still applies verbatim: a higher
    `net_overrides.priority` bus net routes before a lower one, and the
    airline/name tie-break settles the rest."""
    p = _ladder(tmp_path)
    _priorities(tmp_path, {"CHAIN_11": 5.0}, buses=["CHAIN_9", "CHAIN_11"])
    order = _order(p["project"])
    assert order[:2] == ["CHAIN_11", "CHAIN_9"]


def test_user_priority_cannot_promote_a_non_bus_net_above_a_bus_net(tmp_path):
    """The bus split is the PRIMARY key: a non-member with a large explicit
    priority still routes after every member, including members with none."""
    p = _ladder(tmp_path)
    _priorities(tmp_path, {"CHAIN_6": 99.0}, buses=["CHAIN_9", "CHAIN_11"])
    order = _order(p["project"])
    assert set(order[:2]) == {"CHAIN_9", "CHAIN_11"}
    assert order[2] == "CHAIN_6"           # priority still leads the non-bus group
    assert order[3:] == ["CHAIN_10", "CHAIN_7", "CHAIN_8"]


def test_airline_then_name_tiebreak_survives_inside_both_groups(tmp_path):
    """Same priority throughout: shortest airline first, name last - inside the
    bus group and inside the non-bus group alike. Airlines are supplied on the
    connection records (which is where the sort reads them from) so the fixture
    does not have to be re-geometried to produce distinct lengths."""
    p = _ladder(tmp_path)
    _confirm_bus(tmp_path, ["CHAIN_9", "CHAIN_11"])
    conns = router.get_ratsnest(p["project"])["connections"]
    lengths = {"CHAIN_6": 9.0, "CHAIN_7": 1.0, "CHAIN_8": 5.0,
               "CHAIN_9": 8.0, "CHAIN_10": 3.0, "CHAIN_11": 2.0}
    for c in conns:
        c["airline_length_mm"] = lengths[c["net"]]
    res = router.route_nets(p["project"], connections=conns, write=False)
    assert [c["net"] for c in res["connections"]] == [
        "CHAIN_11", "CHAIN_9",                              # bus, by airline
        "CHAIN_7", "CHAIN_10", "CHAIN_8", "CHAIN_6",        # non-bus, by airline
    ]


def test_detect_buses_candidates_also_win_the_early_slot(tmp_path):
    """The other of `_crosstalk_bus_groups`' two sources: a board with a
    structurally-detected (never user-confirmed) SPI bus still routes it
    first."""
    p = write_multidrop_spi_project(tmp_path, destinations=1, route=False)
    settings = pcb.load_pcb_settings(p["project"])
    groups = router._crosstalk_bus_groups(p["project"], settings)
    assert groups, "fixture must have a DETECTED bus, not a confirmed one"
    assert router._bus_member_nets(groups) >= {"/SPI/SCK", "/SPI/MOSI", "/SPI/MISO"}


# --------------------------------------------------------------------------- #
# 4. inertness - the half of this phase that must never regress
# --------------------------------------------------------------------------- #

def test_worklist_is_pre_722_order_when_the_board_has_no_bus(tmp_path):
    p = _ladder(tmp_path)
    assert _order(p["project"]) == _pre_722_order(p["project"])


def test_bus_less_board_routes_byte_identically_with_bus_first_off(tmp_path):
    """Stronger than net order: the ENTIRE result - every segment, via, length
    and self-check - is identical with the feature on and off, because with no
    bus on the board the new sort key is the old sort key."""
    p = _ladder(tmp_path)
    on = router.route_nets(p["project"], write=False)
    _settings_file(tmp_path, {"bus_first": False})
    off = router.route_nets(p["project"], write=False)
    assert _digest(on) == _digest(off)


def test_bus_first_false_restores_the_pre_722_order_even_with_a_bus(tmp_path):
    """The escape hatch: a board that DOES have a bus can still be pinned to
    the exact pre-7.22 order."""
    p = _ladder(tmp_path)
    _confirm_bus(tmp_path, ["CHAIN_9", "CHAIN_11"])
    _settings_file(tmp_path, {"bus_first": False})
    assert _order(p["project"]) == _pre_722_order(p["project"])


def test_straight_line_corridor_is_never_called_at_the_default(tmp_path, monkeypatch):
    """The directness term is SKIPPED at its default, not weighted to zero -
    the 7.20 `crosstalk is None` convention. Proved by making the helper fatal:
    a default-settings run on a board that HAS a bus must still complete.

    `cpu.workers: 1` is required for the monkeypatch to be observable at all -
    7.8b's speculative pass otherwise routes in spawned PROCESSES, which never
    see a parent-process patch. `_run_independent_routes` runs the identical
    `_route_one(ctx, conn, base_obstacles, {})` per owner in-process at
    `workers <= 1`, so this changes execution only, never the algorithm."""
    p = write_multidrop_spi_project(tmp_path, destinations=1, route=False)
    _settings_file(tmp_path, {"cpu": {"workers": 1}})

    def _boom(*a, **kw):  # pragma: no cover - the point is that it never runs
        raise AssertionError("_straight_line_corridor called at default settings")

    monkeypatch.setattr(router, "_straight_line_corridor", _boom)
    res = router.route_nets(p["project"], write=False)
    assert res["summary"]["connections_routed"] >= 1


def test_explicit_zero_corridor_is_identical_to_the_default(tmp_path):
    """Writing the knob out as 0.0 must reproduce the default run exactly -
    i.e. 0.0 really is the untouched arithmetic and not a near-miss."""
    p = write_multidrop_spi_project(tmp_path, destinations=1, route=False)
    default = router.route_nets(p["project"], write=False)
    _settings_file(tmp_path, {"bus_first_direct_corridor_mm": 0.0})
    explicit = router.route_nets(p["project"], write=False)
    assert _digest(default) == _digest(explicit)


# --------------------------------------------------------------------------- #
# 5. `_straight_line_corridor` itself
# --------------------------------------------------------------------------- #

def _window(grid: float = 0.2) -> "router._FineWindow":
    layers = ["F.Cu", "B.Cu"]
    win = router._FineWindow(0.0, 0.0, 20.0, 20.0, grid, layers,
                             {n: "signal" for n in layers}, "SIG")
    win.build([], 0.1, 0.3, 0.2, 0.2)
    return win


def test_straight_line_corridor_zero_width_is_none():
    assert router._straight_line_corridor(_window(), (2.0, 2.0), (18.0, 2.0), 0.0) is None


def test_straight_line_corridor_hugs_the_line_and_covers_both_ends():
    win = _window()
    cells = router._straight_line_corridor(win, (2.0, 10.0), (18.0, 10.0), 0.6)
    assert win.cell_of(2.0, 10.0) in cells
    assert win.cell_of(18.0, 10.0) in cells
    assert win.cell_of(10.0, 10.0) in cells      # mid-line
    assert win.cell_of(10.0, 16.0) not in cells  # 6 mm off the line
    # every cell is within the stamped half-width (square stamp, so the bound
    # is the Chebyshev radius `ceil(half_width/grid)` cells about the line)
    rr = 3  # ceil(0.6 / 0.2)
    for (ix, iy) in cells:
        assert abs(iy - win.cell_of(2.0, 10.0)[1]) <= rr


def test_straight_line_corridor_is_connected_along_a_diagonal():
    """Half-cell sampling: consecutive stamps must overlap so the corridor is a
    tube, not a dotted line (a dotted corridor would make the discount
    meaningless on a diagonal run)."""
    win = _window()
    cells = router._straight_line_corridor(win, (2.0, 2.0), (18.0, 18.0), 0.2)
    xs = sorted({ix for (ix, _iy) in cells})
    assert xs == list(range(xs[0], xs[-1] + 1)), "corridor has a column gap"


# --------------------------------------------------------------------------- #
# 6. the knob bites (the actual Phase 7.22 deliverable, opt-in)
# --------------------------------------------------------------------------- #

def test_direct_corridor_straightens_an_against_axis_bus_net(tmp_path):
    """MEASURED EVIDENCE for the design decision recorded in NETCLASS_PLAN.md:
    the pre-7.22 machinery is NOT already direct on an empty board. This board
    has no copper at all, so nothing here is congestion - the bowing is
    `_direction_factor`'s `off_direction` charge plus via costs. With the knob
    on, at least one bus net that previously detoured routes at exactly its
    airline length with no vias, and the board total gets no worse."""
    def run(corridor_mm: float, directory: Path):
        p = write_multidrop_spi_project(directory, destinations=1, route=False)
        if corridor_mm:
            _settings_file(directory, {"bus_first_direct_corridor_mm": corridor_mm})
        res = router.route_nets(p["project"], write=False)
        return {c["net"]: c for c in res["connections"] if c["routed"]}

    off = run(0.0, tmp_path / "off")
    on = run(0.6, tmp_path / "on")
    assert set(off) == set(on)

    def ratio(recs):
        return (sum(c["length_mm"] for c in recs.values())
                / sum(c["airline_length_mm"] for c in recs.values()))

    # the specific against-axis net the stock router bows: it detours AND pays
    # vias with the knob off, and lands exactly on its airline with it on.
    assert off["/SPI/SCK"]["length_mm"] > off["/SPI/SCK"]["airline_length_mm"] + 1.0
    assert off["/SPI/SCK"]["via_count"] > 0
    assert on["/SPI/SCK"]["length_mm"] == pytest.approx(
        on["/SPI/SCK"]["airline_length_mm"], abs=1e-3)
    assert on["/SPI/SCK"]["via_count"] == 0
    # and it is not a local win paid for elsewhere on the board
    assert ratio(on) < ratio(off)
    for rec in on.values():
        assert rec["self_check"]["passed"] is True


# --------------------------------------------------------------------------- #
# 7. scope - first pass only, and NO conflict avoidance
# --------------------------------------------------------------------------- #

def test_ripup_reroute_never_uses_the_direct_corridor(tmp_path, monkeypatch):
    """Rip-up re-routes are corridor-free (`use_corridor=False`), which is what
    scopes 7.22 to the FIRST pass. Asserted structurally: with the helper made
    fatal, a `use_corridor=False` call on a bus net still completes."""
    p = write_multidrop_spi_project(tmp_path, destinations=1, route=False)
    # workers:1 for the same reason as the test above - a patch in this process
    # is invisible to a spawned pool worker.
    _settings_file(tmp_path, {"bus_first_direct_corridor_mm": 0.6,
                              "cpu": {"workers": 1}})

    calls: list[int] = []
    real = router._straight_line_corridor
    ctx_probe: dict = {}
    real_route_one = router._route_one

    def _counting(*a, **kw):
        calls.append(1)
        return real(*a, **kw)

    def _capture(ctx, conn, obstacles, congestion, use_corridor=True):
        ctx_probe.setdefault("ctx", ctx)
        ctx_probe.setdefault("conn", conn)
        ctx_probe.setdefault("obstacles", obstacles)
        return real_route_one(ctx, conn, obstacles, congestion, use_corridor)

    monkeypatch.setattr(router, "_straight_line_corridor", _counting)
    monkeypatch.setattr(router, "_route_one", _capture)
    router.route_nets(p["project"], write=False)
    assert calls, "knob is on; the first pass must use it"

    # ...and the corridor-free path (what every rip-up re-route uses) adds none.
    calls.clear()
    real_route_one(ctx_probe["ctx"], ctx_probe["conn"],
                   ctx_probe["obstacles"], {}, use_corridor=False)
    assert calls == [], "a corridor-free (rip-up) re-route must not use 7.22"


def test_direct_corridor_ignores_already_placed_copper(tmp_path):
    """The explicit non-goal (user: "they can be riped up and optimized
    later"): the straight-line corridor is a pure function of the connection's
    own two endpoints and the window grid. It takes no obstacle list, no
    placement state and no congestion field, so it CANNOT be steering around
    other nets - there is no lookahead or reservation anywhere in this phase."""
    import inspect
    sig = inspect.signature(router._straight_line_corridor)
    assert list(sig.parameters) == ["win", "from_xy", "to_xy", "half_width_mm"]
    # scan the CODE, not the docstring (which discusses these very concepts).
    src = inspect.getsource(router._straight_line_corridor)
    body = src.split('"""')[2]
    for forbidden in ("obstacle", "congestion", "placement", "reserve", "blocked",
                      "lookahead", "active_"):
        assert forbidden not in body.lower(), forbidden
