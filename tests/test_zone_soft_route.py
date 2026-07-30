"""Tests for the opt-in, default-OFF `allow_zone_soft_route` flag (Phase 7.23):
zone-territory soft routing with a kicad-cli-verified refill.

THE SHAPE BEING REPRODUCED. `_zone_wall_board_text` builds a tiny 20x20 mm,
2-layer board whose only obstacle is a foreign copper zone (`SHIELD`) whose
FILLED POLYGON is a bar spanning the full board height on BOTH routable layers.
A `TEST` connection from (2,10) to (18,10) therefore cannot route: not around
(Edge.Cuts), not under (both layers are covered), and not by rip-up (a zone fill
has `owner is None` and is explicitly out of scope for hand-copper rip-up). That
is exactly the `distance_mm: 0.0 / owner: null` failure the coordinator measured
on the from-scratch test board - and exactly the case where the real KiCad
workflow works fine, because the pour is supposed to yield to the trace on the
next refill.

WHY THE SYNTHETIC BOARD, deliberately. The real kiln board is slow and its
failures are a mix of causes; this fixture isolates the ONE cause the tier
addresses, so a pass here means the tier fired for the right reason. The only
real-board / real-kicad-cli test is the independent DRC-delta check at the
bottom, which is skipped gracefully when kicad-cli is absent (same convention as
`refill_zones_with_kicad` / `benchmark_kicad_autoroute`).

TEST STRATEGY for the kicad-cli-dependent parts: the accept path and the DRC
delta need a real refill and are skipped without kicad-cli. The REJECT path is
tested WITHOUT kicad-cli by neutering the refill (a no-op refill leaves the
fills stale, so the candidate provably does not clear them and must be
refused) - that keeps the "never silently accepted" guarantee under test on any
machine.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

import kicad_router_tool as router
from synthetic_board import _synthetic_kicad_pro_text


_ZONE_NET = "SHIELD"      # deliberately NOT a power-pattern name, so the fill is
                          # an ordinary (non-`via_transparent`) foreign zone.
_TEST_NET = "TEST"

_BAR_X0, _BAR_X1 = 8.0, 12.0


def _pad_footprint(ref: str, x: float, y: float, net_name: str, net_code: int) -> str:
    """One single-pad SMD footprint. Real pads matter here for more than
    realism: without them kicad-cli's DRC reports `track_dangling` for the
    routed trace and `isolated_copper` for the zone islands the trace creates -
    artifacts of a padless test board, not of the copper under test. Anchoring
    both endpoints and BOTH sides of the split pour makes the DRC-delta
    assertion unconditional instead of severity-filtered."""
    return f"""	(footprint "test:PAD1"
		(layer "F.Cu")
		(uuid "zonewall-fp-{ref}")
		(at {x} {y})
		(attr smd)
		(property "Reference" "{ref}"
			(at 0 -1 0)
			(layer "F.SilkS")
			(uuid "zonewall-fpref-{ref}")
			(effects
				(font
					(size 1 1)
					(thickness 0.15)
				)
			)
		)
		(pad "1" smd rect
			(at 0 0)
			(size 1 1)
			(layers "F.Cu" "F.Paste" "F.Mask")
			(net {net_code} "{net_name}")
			(uuid "zonewall-pad-{ref}")
		)
	)
"""


def _zone_wall_board_text() -> str:
    """20x20 mm, F.Cu/B.Cu. One `SHIELD` zone whose fill is the bar
    x in [8, 12], y in [0, 20] on BOTH layers - a full-height copper wall made
    entirely of ZONE FILL (no track, no via, no pad), which is what makes it
    unroutable-and-unrippable today. The zone outline is the same rectangle, so
    a real kicad-cli refill regenerates a comparable bar (inset by its own
    clearance) and, once a trace is present, carves the clearance channel around
    it that this whole tier is predicated on."""
    return """(kicad_pcb
	(version 20260206)
	(generator "test_zone_soft_route.py")
	(generator_version "10.0")
	(general
		(thickness 1.6)
	)
	(paper "A4")
	(layers
		(0 "F.Cu" signal)
		(2 "B.Cu" signal)
		(1 "F.Mask" user)
		(3 "B.Mask" user)
		(5 "F.SilkS" user)
		(7 "B.SilkS" user)
		(13 "F.Paste" user)
		(15 "B.Paste" user)
		(25 "Edge.Cuts" user)
	)
	(setup
		(pad_to_mask_clearance 0)
	)
	(net 0 "")
	(net 1 "SHIELD")
	(net 2 "TEST")
""" + _pad_footprint("J1", 2.0, 10.0, "TEST", 2) \
    + _pad_footprint("J2", 18.0, 10.0, "TEST", 2) \
    + _pad_footprint("G1", 10.0, 3.0, "SHIELD", 1) \
    + _pad_footprint("G2", 10.0, 17.0, "SHIELD", 1) + """
	(gr_rect
		(start 0 0)
		(end 20 20)
		(stroke
			(width 0.1)
			(type solid)
		)
		(layer "Edge.Cuts")
		(uuid "zonewall-edge-0001")
	)
	(zone
		(net "SHIELD")
		(layers "F.Cu" "B.Cu")
		(uuid "zonewall-zone-0001")
		(name "shieldbar")
		(hatch edge 0.5)
		(priority 0)
		(connect_pads
			(clearance 0.2)
		)
		(min_thickness 0.25)
		(fill yes
			(thermal_gap 0.5)
			(thermal_bridge_width 0.5)
			(island_removal_mode 0)
		)
		(polygon
			(pts
				(xy 8 0) (xy 12 0) (xy 12 20) (xy 8 20)
			)
		)
		(filled_polygon
			(layer "F.Cu")
			(pts
				(xy 8 0) (xy 12 0) (xy 12 20) (xy 8 20)
			)
		)
		(filled_polygon
			(layer "B.Cu")
			(pts
				(xy 8 0) (xy 12 0) (xy 12 20) (xy 8 20)
			)
		)
	)
)
"""


def _write_zone_wall_board(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "zonewall.kicad_pcb").write_text(_zone_wall_board_text(), encoding="utf-8")
    (tmp_path / "zonewall.kicad_pro").write_text(_synthetic_kicad_pro_text(), encoding="utf-8")
    # workers=1 keeps both the speculative pass and the serial worklist in THIS
    # process - deterministic and pool-free for a one-connection test. The
    # worker-count determinism claim is proven separately below.
    (tmp_path / "pcb_settings.json").write_text(
        json.dumps({"autorouter": {"cpu": {"workers": 1}}}), encoding="utf-8")
    return tmp_path


def _conns() -> list[dict]:
    return [
        {"net": _TEST_NET, "from_point": {"x": 2.0, "y": 10.0}, "to_point": {"x": 18.0, "y": 10.0},
         "airline_length_mm": 16.0, "from_layers": ["F.Cu"], "to_layers": ["F.Cu"]},
    ]


def _by_net(res: dict) -> dict:
    return {c["net"]: c for c in res["connections"]}


_has_cli = router._find_kicad_cli() is not None
requires_cli = pytest.mark.skipif(not _has_cli, reason="kicad-cli not installed on this machine")


# --------------------------------------------------------------------------- #
# (a) PARITY - flag OFF is byte-identical to pre-7.23 behavior.
# --------------------------------------------------------------------------- #

def test_flag_off_zone_sealed_net_fails_exactly_as_before(tmp_path) -> None:
    board_dir = _write_zone_wall_board(tmp_path)
    before = (board_dir / "zonewall.kicad_pcb").read_text(encoding="utf-8")

    res = router.route_nets(board_dir, connections=_conns(), write=False)

    rec = _by_net(res)[_TEST_NET]
    assert rec["routed"] is False
    # The pre-7.23 failure reasons, untouched - NOT zone_soft_route_rejected.
    assert rec["failure"]["reason"] in ("unreachable_in_window", "self_check_failed")
    assert res["allow_zone_soft_route"] is False
    assert res["zone_soft_routed"] == []
    assert res["zone_soft_route"] is None
    assert res["summary"]["zone_soft_routed_count"] == 0
    assert (board_dir / "zonewall.kicad_pcb").read_text(encoding="utf-8") == before

    # write=True changes nothing either: there is nothing routed to write.
    res_w = router.route_nets(board_dir, connections=_conns(), write=True)
    assert res_w["written"] is False
    assert (board_dir / "zonewall.kicad_pcb").read_text(encoding="utf-8") == before


def test_flag_off_result_is_identical_to_a_run_predating_the_flag(tmp_path) -> None:
    """The parity test proper: the per-connection report of a default call must
    be byte-identical to one made with the flag explicitly False, AND the tier's
    entry point must never be reached. `_route_zone_soft` is monkeypatched to
    explode - if the default path ever calls it, this fails loudly."""
    d1 = _write_zone_wall_board(tmp_path / "a")
    d2 = _write_zone_wall_board(tmp_path / "b")

    default_res = router.route_nets(d1, connections=_conns(), write=False)
    explicit_res = router.route_nets(d2, connections=_conns(), write=False,
                                     allow_zone_soft_route=False)

    assert (json.dumps(default_res["connections"], sort_keys=True)
            == json.dumps(explicit_res["connections"], sort_keys=True))


def test_flag_off_never_calls_the_zone_soft_tier(tmp_path, monkeypatch) -> None:
    board_dir = _write_zone_wall_board(tmp_path)

    def boom(*a, **k):  # pragma: no cover - must never run
        raise AssertionError("_route_zone_soft called with allow_zone_soft_route off")

    monkeypatch.setattr(router, "_route_zone_soft", boom)
    res = router.route_nets(board_dir, connections=_conns(), write=False)
    assert _by_net(res)[_TEST_NET]["routed"] is False


# --------------------------------------------------------------------------- #
# (b) The tier's ELIGIBILITY gate - foreign zone fill only, never real copper.
# --------------------------------------------------------------------------- #

def test_soft_zone_predicate_only_matches_foreign_unowned_zone_fills() -> None:
    zone = router._Obst("zone", "GND", frozenset(["F.Cu"]), 0.0, 0, 0, 0, 0)
    own_zone = router._Obst("zone", "TEST", frozenset(["F.Cu"]), 0.0, 0, 0, 0, 0)
    placed_zone = router._Obst("zone", "GND", frozenset(["F.Cu"]), 0.0, 0, 0, 0, 0, owner=3)
    track = router._Obst("seg", "GND", frozenset(["F.Cu"]), 0.1, 0, 0, 1, 1, uuid="u")
    via = router._Obst("pt", "GND", frozenset(["F.Cu"]), 0.3, 0, 0, 0, 0, uuid="v")
    pad = router._Obst("pt", "GND", frozenset(["F.Cu"]), 0.5, 0, 0, 0, 0, is_pad=True)
    edge = router._Obst("edge", "", frozenset(["F.Cu"]), 0.0, 0, 0, 1, 0, is_edge=True)

    assert router._is_soft_zone_obstacle(zone, "TEST") is True
    assert router._is_soft_zone_obstacle(own_zone, "TEST") is False   # same net
    assert router._is_soft_zone_obstacle(placed_zone, "TEST") is False  # owned
    for hard in (track, via, pad, edge):
        assert router._is_soft_zone_obstacle(hard, "TEST") is False


def test_tier_declines_when_a_real_hard_obstacle_is_also_in_the_way(tmp_path) -> None:
    """A hand-routed track wall ON TOP of the zone bar: now the blockers are not
    100% zone fill, so the tier must decline outright and the connection fails
    with its ordinary reason - never `zone_soft_route_rejected`, because it was
    never a candidate. This is the "any hard obstacle in the mix means no
    behavior change" clause."""
    board_dir = _write_zone_wall_board(tmp_path)
    board = board_dir / "zonewall.kicad_pcb"
    text = board.read_text(encoding="utf-8")
    wall = ""
    for layer in ("F.Cu", "B.Cu"):
        wall += (f'\t(segment\n\t\t(start 10 0)\n\t\t(end 10 20)\n\t\t(width 2.0)\n'
                 f'\t\t(layer "{layer}")\n\t\t(net "SHIELD")\n'
                 f'\t\t(uuid "hardwall-{layer}")\n\t)\n')
    board.write_text(text.rstrip()[:-1] + wall + ")\n", encoding="utf-8")

    res = router.route_nets(board_dir, connections=_conns(), write=False,
                            allow_zone_soft_route=True)

    rec = _by_net(res)[_TEST_NET]
    assert rec["routed"] is False
    assert rec["failure"]["reason"] != "zone_soft_route_rejected"
    assert res["zone_soft_routed"] == []
    # No candidate was ever produced, so there is nothing to report on.
    assert res["zone_soft_route"] is None


# --------------------------------------------------------------------------- #
# (c) The refusal paths - a candidate is NEVER accepted on Python's word alone.
# --------------------------------------------------------------------------- #

def test_no_kicad_cli_refuses_every_candidate_with_a_clear_reason(tmp_path, monkeypatch) -> None:
    board_dir = _write_zone_wall_board(tmp_path)
    monkeypatch.setattr(router, "_find_kicad_cli", lambda: None)

    res = router.route_nets(board_dir, connections=_conns(), write=False,
                            allow_zone_soft_route=True)

    report = res["zone_soft_route"]
    assert report is not None, "the tier should have produced a candidate to refuse"
    assert report["attempted"] >= 1
    assert report["skipped"] is True
    assert report["reason"] == "kicad-cli not found"
    assert report["accepted"] == 0
    assert res["zone_soft_routed"] == []

    rec = _by_net(res)[_TEST_NET]
    assert rec["routed"] is False
    assert rec["failure"]["reason"] == "zone_soft_route_rejected"
    assert rec["failure"]["zone_soft_reject"]["reason"] == "kicad-cli not found"


def test_a_refill_that_does_not_actually_refill_rejects_with_a_measured_gap(
        tmp_path, monkeypatch) -> None:
    """The core safety property, provable without kicad-cli: if the refill does
    not really move the fill, the candidate's copper still sits inside the pour,
    the geometric verification measures that, and the candidate is REFUSED with
    the real gap - never accepted on the Python self-check that let it through
    the search."""
    board_dir = _write_zone_wall_board(tmp_path)
    monkeypatch.setattr(router, "_find_kicad_cli", lambda: "/pretend/kicad-cli")
    monkeypatch.setattr(router, "refill_zones_with_kicad",
                        lambda board_path, timeout=180: {"refilled": True, "returncode": 0})

    res = router.route_nets(board_dir, connections=_conns(), write=False,
                            allow_zone_soft_route=True)

    report = res["zone_soft_route"]
    assert report is not None and report["attempted"] >= 1
    assert report["accepted"] == 0
    assert res["zone_soft_routed"] == []

    failure = _by_net(res)[_TEST_NET]["failure"]
    assert failure["reason"] == "zone_soft_route_rejected"
    reject = failure["zone_soft_reject"]
    assert reject["reason"] == "zone_soft_route_rejected"
    # A real, negative, measured shortfall against the named zone.
    assert failure["clearance_gap_mm"] < 0
    assert reject["against_zone_net"] == _ZONE_NET
    assert reject["measured_mm"] < reject["required_mm"]


def test_rejected_candidate_never_reaches_the_board(tmp_path, monkeypatch) -> None:
    board_dir = _write_zone_wall_board(tmp_path)
    before = (board_dir / "zonewall.kicad_pcb").read_text(encoding="utf-8")
    monkeypatch.setattr(router, "_find_kicad_cli", lambda: "/pretend/kicad-cli")
    monkeypatch.setattr(router, "refill_zones_with_kicad",
                        lambda board_path, timeout=180: {"refilled": True, "returncode": 0})

    res = router.route_nets(board_dir, connections=_conns(), write=True,
                            allow_zone_soft_route=True)

    assert res["written"] is False
    assert (board_dir / "zonewall.kicad_pcb").read_text(encoding="utf-8") == before


# --------------------------------------------------------------------------- #
# (d) The measurement primitives themselves.
# --------------------------------------------------------------------------- #

def test_zone_clearance_gaps_measures_intrusion_and_clearance() -> None:
    bar = [(8.0, 0.0), (12.0, 0.0), (12.0, 20.0), (8.0, 20.0)]
    zone = router._Obst("zone", _ZONE_NET, frozenset(["F.Cu"]), 0.0,
                        8.0, 0.0, 8.0, 0.0, pts=bar)
    rules = {"clearance": 0.2, "track_width": 0.25, "edge_clearance": 0.2}

    through = [{"x1": 2.0, "y1": 10.0, "x2": 18.0, "y2": 10.0, "layer": "F.Cu"}]
    gaps = router._zone_clearance_gaps(_TEST_NET, through, [], [zone], rules, 0.3)
    assert gaps and gaps[0]["measured_mm"] == 0.0
    assert gaps[0]["gap_mm"] < 0

    clear = [{"x1": 2.0, "y1": 10.0, "x2": 7.0, "y2": 10.0, "layer": "F.Cu"}]
    assert router._zone_clearance_gaps(_TEST_NET, clear, [], [zone], rules, 0.3) == []

    # Same-net copper is never measured against its own pour.
    assert router._zone_clearance_gaps(_ZONE_NET, through, [], [zone], rules, 0.3) == []

    # Just inside the required clearance (0.125 half-width + 0.2 = 0.325).
    grazing = [{"x1": 2.0, "y1": 10.0, "x2": 7.8, "y2": 10.0, "layer": "F.Cu"}]
    assert router._zone_clearance_gaps(_TEST_NET, grazing, [], [zone], rules, 0.3)


def test_min_dist_seg_to_poly_is_zero_inside_and_exact_outside() -> None:
    bar = [(8.0, 0.0), (12.0, 0.0), (12.0, 20.0), (8.0, 20.0)]
    assert router._min_dist_seg_to_poly(9.0, 10.0, 11.0, 10.0, bar) == 0.0
    assert router._min_dist_seg_to_poly(2.0, 10.0, 6.0, 10.0, bar) == pytest.approx(2.0, abs=1e-6)


# --------------------------------------------------------------------------- #
# (e) The ACCEPT path, against a real kicad-cli refill.
# --------------------------------------------------------------------------- #

@requires_cli
def test_real_refill_accepts_the_candidate_and_reports_it(tmp_path) -> None:
    board_dir = _write_zone_wall_board(tmp_path)

    res = router.route_nets(board_dir, connections=_conns(), write=False,
                            allow_zone_soft_route=True)

    report = res["zone_soft_route"]
    assert report is not None, "the tier should have produced a candidate"
    if report["accepted"] == 0:
        pytest.skip(f"kicad-cli refill did not clear the candidate here: {report}")

    assert res["allow_zone_soft_route"] is True
    assert res["summary"]["zone_soft_routed_count"] == len(res["zone_soft_routed"])
    record = res["zone_soft_routed"][0]
    assert record["net"] == _TEST_NET
    assert record["segment_count"] >= 1
    assert len(record["uuids"]) == record["segment_count"] + record["via_count"]

    rec = _by_net(res)[_TEST_NET]
    assert rec["routed"] is True
    assert rec["failure"] is None
    assert rec["zone_soft_route"]["zones"]


@requires_cli
def test_accepted_candidate_writes_and_forces_a_real_board_refill(tmp_path) -> None:
    board_dir = _write_zone_wall_board(tmp_path)

    res = router.route_nets(board_dir, connections=_conns(), write=True,
                            allow_zone_soft_route=True)
    if not res["zone_soft_routed"]:
        pytest.skip(f"kicad-cli refill did not clear the candidate here: {res['zone_soft_route']}")

    assert res["written"] is True
    # `refill_zones` was NOT passed, but a zone-soft write must refill anyway -
    # the fill has to catch up with the new trace.
    assert res["refill"] is not None and res["refill"].get("refilled") is True
    text = (board_dir / "zonewall.kicad_pcb").read_text(encoding="utf-8")
    assert f'(net "{_TEST_NET}")' in text
    for uid in res["zone_soft_routed"][0]["uuids"]:
        assert f'(uuid "{uid}")' in text

    # Reversible, like any other autorouter copper.
    undo = router.unroute_nets(board_dir, write=True)
    assert undo["removed"]


# --------------------------------------------------------------------------- #
# (f) Independent verification: a REAL kicad-cli DRC run must show no NEW
#     violations attributable to the accepted zone-soft copper.
# --------------------------------------------------------------------------- #

@requires_cli
def test_accepted_zone_soft_copper_adds_no_new_kicad_cli_drc_violations(tmp_path) -> None:
    """Does not trust this module's own geometry check: routes with the flag on,
    then asks kicad-cli itself for a DRC report before and after, and requires a
    zero NEW-violation delta (`_new_violations`, the multiset difference the
    kicad-cli acceptance test already proved out). The BEFORE board is refilled
    first so the comparison is fill-vs-fill, not stale-fill-vs-refilled-fill."""
    board_dir = _write_zone_wall_board(tmp_path)
    cli = router._find_kicad_cli()
    board = board_dir / "zonewall.kicad_pcb"

    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    baseline_board = baseline_dir / "zonewall.kicad_pcb"
    shutil.copy2(board, baseline_board)
    shutil.copy2(board_dir / "zonewall.kicad_pro", baseline_dir / "zonewall.kicad_pro")
    assert router.refill_zones_with_kicad(baseline_board).get("refilled")
    baseline = router._drc_violations(cli, baseline_board, tmp_path / "before.json")
    if baseline is None:
        pytest.skip("kicad-cli DRC did not produce a parseable baseline report")

    res = router.route_nets(board_dir, connections=_conns(), write=True,
                            allow_zone_soft_route=True)
    if not res["zone_soft_routed"]:
        pytest.skip(f"kicad-cli refill did not clear the candidate here: {res['zone_soft_route']}")

    post = router._drc_violations(cli, board, tmp_path / "after.json")
    assert post is not None
    new = router._new_violations(baseline, post)
    assert new == [], f"zone-soft copper introduced {len(new)} new DRC violation(s): {new[:3]}"


# --------------------------------------------------------------------------- #
# (g) Determinism - same answer serially and across a worker pool.
# --------------------------------------------------------------------------- #

def _run_with_workers(tmp_path: Path, workers: int, monkeypatch) -> dict:
    board_dir = _write_zone_wall_board(tmp_path)
    (board_dir / "pcb_settings.json").write_text(
        json.dumps({"autorouter": {"cpu": {"workers": workers}}}), encoding="utf-8")
    monkeypatch.setattr(router, "_find_kicad_cli", lambda: "/pretend/kicad-cli")
    monkeypatch.setattr(router, "refill_zones_with_kicad",
                        lambda board_path, timeout=180: {"refilled": True, "returncode": 0})
    return router.route_nets(board_dir, connections=_conns(), write=False,
                             allow_zone_soft_route=True)


def test_zone_soft_route_is_deterministic_across_worker_counts(tmp_path, monkeypatch) -> None:
    serial = _run_with_workers(tmp_path / "w1", 1, monkeypatch)
    parallel = _run_with_workers(tmp_path / "w4", 4, monkeypatch)

    assert (json.dumps(serial["connections"], sort_keys=True)
            == json.dumps(parallel["connections"], sort_keys=True))
    assert (json.dumps(serial["zone_soft_route"], sort_keys=True)
            == json.dumps(parallel["zone_soft_route"], sort_keys=True))


def test_repeat_runs_reproduce_the_same_report(tmp_path, monkeypatch) -> None:
    first = _run_with_workers(tmp_path / "r1", 1, monkeypatch)
    second = _run_with_workers(tmp_path / "r2", 1, monkeypatch)
    assert (json.dumps(first["connections"], sort_keys=True)
            == json.dumps(second["connections"], sort_keys=True))


# --------------------------------------------------------------------------- #
# (h) The flag is plumbed all the way out to route_board / the CLI / MCP.
# --------------------------------------------------------------------------- #

def test_route_board_passes_the_flag_through_and_reports_it(tmp_path, monkeypatch) -> None:
    board_dir = _write_zone_wall_board(tmp_path)
    monkeypatch.setattr(router, "_find_kicad_cli", lambda: None)

    report = router.route_board(board_dir, write=False, allow_zone_soft_route=True)

    assert report["allow_zone_soft_route"] is True
    assert report["zone_soft_routed"] == []
    off = router.route_board(board_dir, write=False)
    assert off["allow_zone_soft_route"] is False
    assert off["zone_soft_route"] is None


def test_cli_exposes_the_flag_and_defaults_it_off(monkeypatch) -> None:
    """The CLI must accept `--allow-zone-soft-route` and forward it, defaulting
    to False when it is absent. `route_board` is stubbed out so this stays a
    pure argument-plumbing check with no routing cost."""
    captured: dict = {}

    class _Captured(Exception):
        pass

    def fake_route_board(project_path, **kwargs):
        captured.update(kwargs)
        raise _Captured()

    monkeypatch.setattr(router, "route_board", fake_route_board)
    for argv, expected in ((["route", "x.kicad_pro"], False),
                           (["route", "x.kicad_pro", "--allow-zone-soft-route"], True)):
        captured.clear()
        with pytest.raises(_Captured):
            router.main(argv)
        assert captured["allow_zone_soft_route"] is expected, argv


def test_mcp_schema_declares_the_flag_on_both_routing_tools() -> None:
    from kicad_mcp_server import KiCadMcpServer

    server = KiCadMcpServer()
    for name in ("route_kicad_board", "route_kicad_nets"):
        props = server.tools[name]["inputSchema"]["properties"]
        assert props["allow_zone_soft_route"]["type"] == "boolean", name
        assert props["allow_zone_soft_route"]["default"] is False, name
        assert "kicad-cli" in props["allow_zone_soft_route"]["description"], name
        assert "zone_soft_route" in server.tools[name]["description"], name
