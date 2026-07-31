"""Tests for the opt-in, default-OFF `direct_route_first` flag (Phase 7.24):
the tier-0 direct-line / one-bend Manhattan fast path.

THE SHAPES BEING TESTED, each a tiny synthetic board (mirrors
`test_zone_soft_route.py`'s style: purpose-built minimal `.kicad_pcb` text
rather than the generic `synthetic_board` generator, so obstacle geometry can
be placed exactly where a specific candidate needs to hit or miss it):

  * `_open_board_text`   - nothing between the two TEST pads: the straight
    segment must be accepted, and its geometry must literally BE the direct
    line (no grid, no detour).
  * `_diagonal_wall_board_text` - a short foreign-net obstacle sitting
    exactly on the direct diagonal line between two TEST pads, but nowhere
    near either one-bend Manhattan corner: the straight candidate must be
    rejected and the first L-bend (corner at `(from.x, to.y)`) must be
    accepted instead.
  * `_full_wall_board_text` - a foreign-net wall spanning the full bbox of
    the two TEST pads (blocking the straight line AND both L-bend corners),
    with open board on either side of the wall past the pads' bbox: tier 0
    must decline every candidate and fall through to the ordinary
    `_route_attempts`/whole-board-lazy pipeline, which still solves it - and
    the flag-on run must be byte-identical to a flag-off run on the same
    board (this tier can only ever turn an easy case into a cheaper route,
    never change what a hard case resolves to).
  * `_cross_layer_board_text` - a TEST connection whose two pads are on
    different copper layers (no common routable layer): tier 0 must be
    skipped entirely (never even called), falling straight through.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import kicad_router_tool as router
from synthetic_board import _synthetic_kicad_pro_text


_TEST_NET = "TEST"
_BLOCK_NET = "BLOCK"


def _pad_footprint(ref: str, x: float, y: float, net_name: str, net_code: int,
                   layer: str = "F.Cu") -> str:
    """One single-pad SMD footprint (same shape as test_zone_soft_route.py's,
    proven to satisfy kicad-cli's DRC without dangling-track/isolated-copper
    noise)."""
    return f"""\t(footprint "test:PAD1"
\t\t(layer "{layer}")
\t\t(uuid "directroute-fp-{ref}")
\t\t(at {x} {y})
\t\t(attr smd)
\t\t(property "Reference" "{ref}"
\t\t\t(at 0 -1 0)
\t\t\t(layer "F.SilkS")
\t\t\t(uuid "directroute-fpref-{ref}")
\t\t\t(effects
\t\t\t\t(font
\t\t\t\t\t(size 1 1)
\t\t\t\t\t(thickness 0.15)
\t\t\t\t)
\t\t\t)
\t\t)
\t\t(pad "1" smd rect
\t\t\t(at 0 0)
\t\t\t(size 1 1)
\t\t\t(layers "{layer}" "F.Paste" "F.Mask")
\t\t\t(net {net_code} "{net_name}")
\t\t\t(uuid "directroute-pad-{ref}")
\t\t)
\t)
"""


def _track_block(x1: float, y1: float, x2: float, y2: float, layer: str,
                 net: str, uid: str, width: float = 1.0) -> str:
    return (f'\t(segment\n\t\t(start {x1} {y1})\n\t\t(end {x2} {y2})\n'
            f'\t\t(width {width})\n\t\t(layer "{layer}")\n'
            f'\t\t(net "{net}")\n\t\t(uuid "{uid}")\n\t)\n')


def _board_header(size: float) -> str:
    return f"""(kicad_pcb
\t(version 20260206)
\t(generator "test_direct_route.py")
\t(generator_version "10.0")
\t(general
\t\t(thickness 1.6)
\t)
\t(paper "A4")
\t(layers
\t\t(0 "F.Cu" signal)
\t\t(2 "B.Cu" signal)
\t\t(1 "F.Mask" user)
\t\t(3 "B.Mask" user)
\t\t(5 "F.SilkS" user)
\t\t(7 "B.SilkS" user)
\t\t(13 "F.Paste" user)
\t\t(15 "B.Paste" user)
\t\t(25 "Edge.Cuts" user)
\t)
\t(setup
\t\t(pad_to_mask_clearance 0)
\t)
\t(net 0 "")
\t(net 1 "{_TEST_NET}")
\t(net 2 "{_BLOCK_NET}")
"""


def _edge_rect(size: float) -> str:
    return f"""\t(gr_rect
\t\t(start 0 0)
\t\t(end {size} {size})
\t\t(stroke
\t\t\t(width 0.1)
\t\t\t(type solid)
\t\t)
\t\t(layer "Edge.Cuts")
\t\t(uuid "directroute-edge-0001")
\t)
"""


# --------------------------------------------------------------------------- #
# Board builders
# --------------------------------------------------------------------------- #

_OPEN_FROM = (2.0, 10.0)
_OPEN_TO = (18.0, 10.0)


def _open_board_text() -> str:
    """20x20 mm board, two TEST pads, nothing else in the way at all."""
    return (_board_header(20)
            + _pad_footprint("J1", _OPEN_FROM[0], _OPEN_FROM[1], _TEST_NET, 1)
            + _pad_footprint("J2", _OPEN_TO[0], _OPEN_TO[1], _TEST_NET, 1)
            + _edge_rect(20) + ")\n")


_DIAG_FROM = (2.0, 4.0)
_DIAG_TO = (18.0, 16.0)


def _diagonal_wall_board_text() -> str:
    """20x20 mm board, TEST pads at a genuine diagonal (differing x AND y, so
    the straight candidate and the two L-bends are all geometrically
    distinct). A short BLOCK-net track sits exactly on the straight diagonal
    line's midpoint (10, 10) but nowhere near either L-bend's Manhattan
    corridor (corner1 = (2, 16): legs at x=2 and y=16; corner2 = (18, 4):
    legs at y=4 and x=18 - the obstacle at y=10, x in [9, 11] is >= 5 mm from
    every one of those four legs), so only the straight candidate is
    rejected."""
    wall = _track_block(9.0, 10.0, 11.0, 10.0, "F.Cu", _BLOCK_NET, "directroute-wall-diag", width=1.0)
    return (_board_header(20)
            + _pad_footprint("J1", _DIAG_FROM[0], _DIAG_FROM[1], _TEST_NET, 1)
            + _pad_footprint("J2", _DIAG_TO[0], _DIAG_TO[1], _TEST_NET, 1)
            + wall + _edge_rect(20) + ")\n")


_FULLWALL_FROM = (10.0, 15.0)
_FULLWALL_TO = (30.0, 25.0)


def _full_wall_board_text() -> str:
    """40x40 mm board. TEST pads at (10, 15) and (30, 25) - bbox x in
    [10, 30], y in [15, 25]. A BLOCK-net wall runs vertically at x=20 from
    y=12 to y=38, which crosses the straight diagonal (at y=20) AND both
    L-bend corners' horizontal legs (corner1's y=25 leg, corner2's y=15 leg -
    both inside [12, 38]) - every tier-0 candidate collides with it. The wall
    stops well short of the board edges (y in [0, 12] and [38, 40] are open),
    so the ordinary `_route_attempts`/whole-board-lazy pipeline can still
    route around it; this board exists to prove tier 0 declining changes
    NOTHING about that outcome."""
    wall = _track_block(20.0, 12.0, 20.0, 38.0, "F.Cu", _BLOCK_NET, "directroute-wall-full", width=1.0)
    wall += _track_block(20.0, 12.0, 20.0, 38.0, "B.Cu", _BLOCK_NET, "directroute-wall-full-b", width=1.0)
    return (_board_header(40)
            + _pad_footprint("J1", _FULLWALL_FROM[0], _FULLWALL_FROM[1], _TEST_NET, 1)
            + _pad_footprint("J2", _FULLWALL_TO[0], _FULLWALL_TO[1], _TEST_NET, 1)
            + wall + _edge_rect(40) + ")\n")


_XLAYER_FROM = (2.0, 10.0)
_XLAYER_TO = (18.0, 10.0)


def _cross_layer_board_text() -> str:
    """20x20 mm board, TEST pads on OPPOSITE layers (J1 on F.Cu, J2 on B.Cu)
    with nothing else in the way - geometrically the straight line would be
    trivially open if it were same-layer, but it is not: this is exactly the
    case tier 0 must refuse to touch (no via-drop heuristic in this phase)."""
    return (_board_header(20)
            + _pad_footprint("J1", _XLAYER_FROM[0], _XLAYER_FROM[1], _TEST_NET, 1, layer="F.Cu")
            + _pad_footprint("J2", _XLAYER_TO[0], _XLAYER_TO[1], _TEST_NET, 1, layer="B.Cu")
            + _edge_rect(20) + ")\n")


def _write_board(tmp_path: Path, text: str, stem: str = "directroute",
                 workers: int = 1) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / f"{stem}.kicad_pcb").write_text(text, encoding="utf-8")
    (tmp_path / f"{stem}.kicad_pro").write_text(_synthetic_kicad_pro_text(), encoding="utf-8")
    (tmp_path / "pcb_settings.json").write_text(
        json.dumps({"autorouter": {"cpu": {"workers": workers}}}), encoding="utf-8")
    return tmp_path


def _conn(from_xy: tuple[float, float], to_xy: tuple[float, float],
         from_layers: list[str] | None = None, to_layers: list[str] | None = None) -> list[dict]:
    return [{
        "net": _TEST_NET,
        "from_point": {"x": from_xy[0], "y": from_xy[1]},
        "to_point": {"x": to_xy[0], "y": to_xy[1]},
        "airline_length_mm": ((to_xy[0] - from_xy[0]) ** 2 + (to_xy[1] - from_xy[1]) ** 2) ** 0.5,
        "from_layers": from_layers or ["F.Cu"],
        "to_layers": to_layers or ["F.Cu"],
    }]


def _by_net(res: dict) -> dict:
    return {c["net"]: c for c in res["connections"]}


# --------------------------------------------------------------------------- #
# (a) PARITY - flag OFF is byte-identical to pre-7.24 behavior.
# --------------------------------------------------------------------------- #

def test_flag_off_never_calls_the_direct_route_tier(tmp_path, monkeypatch) -> None:
    board_dir = _write_board(tmp_path, _open_board_text())

    def boom(*a, **k):  # pragma: no cover - must never run
        raise AssertionError("_route_direct_first called with direct_route_first off")

    monkeypatch.setattr(router, "_route_direct_first", boom)
    res = router.route_nets(board_dir, connections=_conn(_OPEN_FROM, _OPEN_TO), write=False)
    # Still routes fine via the ordinary pipeline - the monkeypatch only
    # proves the NEW tier's entry point was never reached.
    assert _by_net(res)[_TEST_NET]["routed"] is True


def test_flag_off_result_is_identical_to_explicit_false(tmp_path) -> None:
    d1 = _write_board(tmp_path / "a", _open_board_text())
    d2 = _write_board(tmp_path / "b", _open_board_text())

    default_res = router.route_nets(d1, connections=_conn(_OPEN_FROM, _OPEN_TO), write=False)
    explicit_res = router.route_nets(d2, connections=_conn(_OPEN_FROM, _OPEN_TO), write=False,
                                     direct_route_first=False)

    assert (json.dumps(default_res["connections"], sort_keys=True)
            == json.dumps(explicit_res["connections"], sort_keys=True))
    assert default_res["direct_route_first"] is False
    assert default_res["summary"]["direct_route_first_count"] == 0


# --------------------------------------------------------------------------- #
# (b) Straight-line ACCEPT: clearly-open case.
# --------------------------------------------------------------------------- #

def test_open_straight_line_is_accepted_and_geometry_is_the_direct_segment(tmp_path) -> None:
    board_dir = _write_board(tmp_path, _open_board_text())

    res = router.route_nets(board_dir, connections=_conn(_OPEN_FROM, _OPEN_TO), write=False,
                            direct_route_first=True)

    rec = _by_net(res)[_TEST_NET]
    assert rec["routed"] is True
    assert rec["failure"] is None
    assert rec["direct_route_first"] == {"kind": "straight"}
    assert rec["via_count"] == 0
    assert rec["segment_count"] == 1
    assert res["summary"]["direct_route_first_count"] == 1

    segments = res.get("segments") or []
    # `route_nets` doesn't echo raw segments at top level on every build; fall
    # back to writing and reading the board back if this build omits it.
    if not segments:
        w = router.route_nets(board_dir, connections=_conn(_OPEN_FROM, _OPEN_TO), write=True,
                              direct_route_first=True)
        assert w["written"] is True
        text = (board_dir / "directroute.kicad_pcb").read_text(encoding="utf-8")
        assert f"(start {_OPEN_FROM[0]} {_OPEN_FROM[1]})" in text or "(start 2 10)" in text
        router.unroute_nets(board_dir, write=True)


# --------------------------------------------------------------------------- #
# (c) Straight blocked, L-bend open: ACCEPT via the bend.
# --------------------------------------------------------------------------- #

def test_blocked_straight_falls_back_to_the_open_l_bend(tmp_path) -> None:
    board_dir = _write_board(tmp_path, _diagonal_wall_board_text())

    # Sanity: the flag-off run must fail on this connection at tier level (or
    # at least never claim the straight-line geometry) - proven indirectly
    # below via the direct_route_first record. First prove the tier-0 run:
    res = router.route_nets(board_dir, connections=_conn(_DIAG_FROM, _DIAG_TO), write=False,
                            direct_route_first=True)

    rec = _by_net(res)[_TEST_NET]
    assert rec["routed"] is True
    assert rec["failure"] is None
    # The straight candidate must have been rejected - the accepted geometry
    # is the FIRST l-bend (corner at (from.x, to.y)), not the direct line.
    assert rec["direct_route_first"] == {"kind": "l_bend_from_x_to_y"}
    assert rec["segment_count"] == 2
    assert rec["via_count"] == 0
    # Length is the L-bend's Manhattan length (16 + 12 = 28 mm), NOT the
    # diagonal's hypotenuse (~20 mm) - proves the accepted geometry really is
    # the bend, not some other path.
    assert rec["length_mm"] == pytest.approx(28.0, abs=1e-3)


def test_diagonal_wall_straight_candidate_self_check_fails_directly() -> None:
    """Unit-level proof that the obstacle really does sit on the straight
    line: `_self_check` must reject the direct segment outright."""
    rules = {"clearance": 0.2, "track_width": 0.2, "edge_clearance": 0.2}
    wall = router._Obst("seg", _BLOCK_NET, frozenset(["F.Cu"]), 0.5,
                        9.0, 10.0, 11.0, 10.0, uuid="w")
    straight = [{"x1": _DIAG_FROM[0], "y1": _DIAG_FROM[1],
                "x2": _DIAG_TO[0], "y2": _DIAG_TO[1], "layer": "F.Cu"}]
    violations = router._self_check(_TEST_NET, straight, [], [wall], rules, 0.3, False)
    assert violations, "the wall must block the direct diagonal segment"


# --------------------------------------------------------------------------- #
# (d) Fully blocked: falls through, existing pipeline still solves it,
#     identical to a flag-off run.
# --------------------------------------------------------------------------- #

def test_fully_blocked_falls_through_and_matches_flag_off(tmp_path) -> None:
    d_on = _write_board(tmp_path / "on", _full_wall_board_text())
    d_off = _write_board(tmp_path / "off", _full_wall_board_text())

    res_on = router.route_nets(d_on, connections=_conn(_FULLWALL_FROM, _FULLWALL_TO), write=False,
                               direct_route_first=True)
    res_off = router.route_nets(d_off, connections=_conn(_FULLWALL_FROM, _FULLWALL_TO), write=False,
                                direct_route_first=False)

    rec_on = _by_net(res_on)[_TEST_NET]
    rec_off = _by_net(res_off)[_TEST_NET]
    # Tier 0 must have declined every candidate for this connection.
    assert "direct_route_first" not in rec_on or rec_on.get("direct_route_first") is None
    assert res_on["summary"]["direct_route_first_count"] == 0
    # The existing pipeline still solves it (this board was built so a detour
    # around the wall's open ends exists) - and produces the EXACT SAME
    # result whether the flag is on or off, since tier 0 never touched it.
    assert rec_on["routed"] is True
    assert (json.dumps(res_on["connections"], sort_keys=True)
            == json.dumps(res_off["connections"], sort_keys=True))


def test_full_wall_blocks_all_three_tier0_candidates_directly() -> None:
    """Unit-level proof of the board's own geometry claim: `_self_check`
    rejects the straight line AND both L-bends against the wall obstacle."""
    rules = {"clearance": 0.2, "track_width": 0.2, "edge_clearance": 0.2}
    wall_f = router._Obst("seg", _BLOCK_NET, frozenset(["F.Cu"]), 0.5,
                          20.0, 12.0, 20.0, 38.0, uuid="wf")
    wall_b = router._Obst("seg", _BLOCK_NET, frozenset(["B.Cu"]), 0.5,
                          20.0, 12.0, 20.0, 38.0, uuid="wb")
    obstacles = [wall_f, wall_b]
    fx, fy = _FULLWALL_FROM
    tx, ty = _FULLWALL_TO
    candidates = [
        [(fx, fy), (tx, ty)],
        [(fx, fy), (fx, ty), (tx, ty)],
        [(fx, fy), (tx, fy), (tx, ty)],
    ]
    for pts in candidates:
        segs = [{"x1": pts[i][0], "y1": pts[i][1], "x2": pts[i + 1][0], "y2": pts[i + 1][1],
                "layer": "F.Cu"} for i in range(len(pts) - 1)]
        violations = router._self_check(_TEST_NET, segs, [], obstacles, rules, 0.3, False)
        assert violations, f"candidate {pts} should be blocked by the full wall"


# --------------------------------------------------------------------------- #
# (e) Cross-layer connection: tier 0 skipped entirely.
# --------------------------------------------------------------------------- #

def test_cross_layer_connection_skips_the_tier_entirely(tmp_path, monkeypatch) -> None:
    board_dir = _write_board(tmp_path, _cross_layer_board_text())

    def boom(*a, **k):  # pragma: no cover - must never run
        raise AssertionError("_route_direct_first called on a cross-layer connection")

    monkeypatch.setattr(router, "_route_direct_first", boom)
    res = router.route_nets(
        board_dir,
        connections=_conn(_XLAYER_FROM, _XLAYER_TO, from_layers=["F.Cu"], to_layers=["B.Cu"]),
        write=False, direct_route_first=True)

    rec = _by_net(res)[_TEST_NET]
    assert res["summary"]["direct_route_first_count"] == 0
    # Whatever the ordinary (via-capable) pipeline decides, it must not carry
    # a tier-0 record - the tier was never entered for this connection.
    assert rec.get("direct_route_first") is None


def test_direct_route_layer_returns_none_when_layers_dont_overlap() -> None:
    assert router._direct_route_layer(["F.Cu"], {"B.Cu"}, None, ["F.Cu", "B.Cu"]) is None
    assert router._direct_route_layer(["F.Cu"], {"F.Cu"}, None, ["F.Cu", "B.Cu"]) == "F.Cu"
    # Prefers home_layer when it's one of the common layers.
    assert router._direct_route_layer(["F.Cu", "B.Cu"], {"F.Cu", "B.Cu"}, "B.Cu",
                                      ["F.Cu", "B.Cu"]) == "B.Cu"
    # Falls back to the first routable-order common layer otherwise.
    assert router._direct_route_layer(["F.Cu", "B.Cu"], {"F.Cu", "B.Cu"}, None,
                                      ["F.Cu", "B.Cu"]) == "F.Cu"


# --------------------------------------------------------------------------- #
# (f) Determinism - same answer serially and across a worker pool.
# --------------------------------------------------------------------------- #

def _run_with_workers(tmp_path: Path, workers: int, board_text: str,
                      from_xy: tuple[float, float], to_xy: tuple[float, float]) -> dict:
    board_dir = _write_board(tmp_path, board_text, workers=workers)
    return router.route_nets(board_dir, connections=_conn(from_xy, to_xy), write=False,
                             direct_route_first=True)


def test_direct_route_first_is_deterministic_across_worker_counts(tmp_path) -> None:
    serial = _run_with_workers(tmp_path / "w1", 1, _diagonal_wall_board_text(),
                               _DIAG_FROM, _DIAG_TO)
    parallel = _run_with_workers(tmp_path / "w4", 4, _diagonal_wall_board_text(),
                                 _DIAG_FROM, _DIAG_TO)
    assert (json.dumps(serial["connections"], sort_keys=True)
            == json.dumps(parallel["connections"], sort_keys=True))


# --------------------------------------------------------------------------- #
# (g) The flag is plumbed all the way out to route_board / the CLI / MCP.
# --------------------------------------------------------------------------- #

def test_route_board_passes_the_flag_through_and_reports_it(tmp_path) -> None:
    board_dir = _write_board(tmp_path, _open_board_text())

    report = router.route_board(board_dir, write=False, direct_route_first=True)
    assert report["direct_route_first"] is True
    assert report["direct_route_first_count"] == 1

    off = router.route_board(board_dir, write=False)
    assert off["direct_route_first"] is False
    assert off["direct_route_first_count"] == 0


def test_cli_exposes_the_flag_and_defaults_it_off(monkeypatch) -> None:
    captured: dict = {}

    class _Captured(Exception):
        pass

    def fake_route_board(project_path, **kwargs):
        captured.update(kwargs)
        raise _Captured()

    monkeypatch.setattr(router, "route_board", fake_route_board)
    for argv, expected in ((["route", "x.kicad_pro"], False),
                           (["route", "x.kicad_pro", "--direct-route-first"], True)):
        captured.clear()
        with pytest.raises(_Captured):
            router.main(argv)
        assert captured["direct_route_first"] is expected, argv


def test_mcp_schema_declares_the_flag_on_both_routing_tools() -> None:
    from kicad_mcp_server import KiCadMcpServer

    server = KiCadMcpServer()
    for name in ("route_kicad_board", "route_kicad_nets"):
        props = server.tools[name]["inputSchema"]["properties"]
        assert props["direct_route_first"]["type"] == "boolean", name
        assert props["direct_route_first"]["default"] is False, name
        assert "tier-0" in props["direct_route_first"]["description"], name
        assert "direct_route_first" in server.tools[name]["description"], name
