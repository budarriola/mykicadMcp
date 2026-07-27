"""Tests for the opt-in, default-OFF `allow_hand_copper_ripup` flag (NETCLASS_PLAN
item 10): when True, the worklist's Step 4 rip-up loop (`route_nets`) may ALSO
rip owner-is-None obstacles, IF AND ONLY IF they are hand-routed track/arc
segments or vias (never a footprint pad, a zone fill, or Edge.Cuts - see
`_is_hand_copper_obstacle`). Default False means every caller that doesn't pass
the flag gets byte-identical behavior to before this feature existed.

Two fault-injection strategies, matching the existing `test_ripup_selfcheck.py`
style:

  1. A REAL sealed-corridor board (`_write_wall_board`): a tiny 20x20mm board
     whose only path between two endpoints is physically blocked by hand-routed
     copper on EVERY routable layer, spanning the full board (so there is no
     detour within the window/board bounds - the same "topologically sealed"
     shape the real kiln board's 6 long nets were diagnosed with). Exercises
     the `unreachable_in_window` Step-4 branch end-to-end with real geometry,
     including the write-path uuid-block-delete.

  2. `_self_check` monkeypatching (borrowed from `test_ripup_selfcheck.py`) to
     exercise the `self_check_failed` Step-4 branch and the pad/zone exclusion
     precisely, without needing to engineer real coarse-vs-exact clearance
     mismatches or giant pad/zone geometries.
"""

from __future__ import annotations

import json
from pathlib import Path

import kicad_router_tool as router
from synthetic_board import _synthetic_kicad_pro_text
from synthetic_board import write_synthetic_project


_WALL_NET = "WALLNET"
_TEST_NET = "TEST"


def _wall_board_text() -> str:
    """20x20mm board (Edge.Cuts rect), 2-layer (F.Cu/B.Cu). A 2mm-wide
    hand-routed "wall" segment spans the FULL board height on EACH routable
    layer at x=10 - between x=2 (a connection's `from_point`) and x=18 (its
    `to_point`), so there is no way to route straight across, around (capped
    by Edge.Cuts), or via-bypass (both layers sealed) without ripping the
    wall. Same shape as the real kiln board's topologically-sealed long nets
    (see NETCLASS_PLAN.md item 10 diagnosis)."""
    return """(kicad_pcb
    (version 20221018)
    (generator "test_hand_copper_ripup.py")
    (general
        (thickness 1.6)
    )
    (paper "A4")
    (layers
        (0 "F.Cu" signal)
        (31 "B.Cu" signal)
    )
    (setup
        (pad_to_mask_clearance 0)
    )
    (net 0 "")
    (net 1 "WALLNET")
    (net 2 "TEST")
    (gr_rect
        (start 0 0)
        (end 20 20)
        (layer "Edge.Cuts")
        (width 0.1)
        (uuid "wall-board-edge-0001")
    )
    (segment
        (start 10 0)
        (end 10 20)
        (width 2.0)
        (layer "F.Cu")
        (net "WALLNET")
        (uuid "wall-seg-fcu-0001")
    )
    (segment
        (start 10 0)
        (end 10 20)
        (width 2.0)
        (layer "B.Cu")
        (net "WALLNET")
        (uuid "wall-seg-bcu-0001")
    )
)
"""


def _write_wall_board(tmp_path: Path) -> Path:
    directory = tmp_path
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "wall.kicad_pcb").write_text(_wall_board_text(), encoding="utf-8")
    (directory / "wall.kicad_pro").write_text(_synthetic_kicad_pro_text(), encoding="utf-8")
    # Force serial (workers=1) so both the speculative pass and the serial
    # worklist run in THIS process - deterministic, no pool overhead for a
    # 1-connection test.
    (directory / "pcb_settings.json").write_text(
        json.dumps({"autorouter": {"cpu": {"workers": 1}}}), encoding="utf-8")
    return directory


def _wall_connections() -> list[dict]:
    return [
        {"net": _TEST_NET, "from_point": {"x": 2.0, "y": 10.0}, "to_point": {"x": 18.0, "y": 10.0},
         "airline_length_mm": 16.0, "from_layers": ["F.Cu"], "to_layers": ["F.Cu"]},
    ]


def _wall_uuids(board_dir: Path) -> set[str]:
    text = (board_dir / "wall.kicad_pcb").read_text(encoding="utf-8")
    return {u for u in ("wall-seg-fcu-0001", "wall-seg-bcu-0001") if f'(uuid "{u}")' in text}


# --------------------------------------------------------------------------- #
# (a) flag OFF: hand copper is NEVER touched, even as the obvious sole blocker.
# --------------------------------------------------------------------------- #

def test_flag_off_never_rips_hand_copper_net_stays_failed(tmp_path) -> None:
    board_dir = _write_wall_board(tmp_path)
    before = (board_dir / "wall.kicad_pcb").read_text(encoding="utf-8")

    res = router.route_nets(board_dir, connections=_wall_connections(), write=False)

    by_net = {c["net"]: c for c in res["connections"]}
    assert by_net[_TEST_NET]["routed"] is False
    assert by_net[_TEST_NET]["failure"]["reason"] == "unreachable_in_window"
    assert res["human_copper_ripped"] == []
    assert res["summary"]["human_copper_ripped_count"] == 0
    assert res["allow_hand_copper_ripup"] is False
    # Board text byte-identical (write=False is always a no-op, but assert it
    # explicitly here since this is the flag's core promise).
    assert (board_dir / "wall.kicad_pcb").read_text(encoding="utf-8") == before

    # Also true with write=True: still nothing to write (nothing routed), and
    # the wall is still fully intact either way.
    res_w = router.route_nets(board_dir, connections=_wall_connections(), write=True)
    assert res_w["written"] is False
    assert _wall_uuids(board_dir) == {"wall-seg-fcu-0001", "wall-seg-bcu-0001"}


# --------------------------------------------------------------------------- #
# (b) flag ON: rips the hand-copper blocker via the `unreachable_in_window`
#     branch and routes; the removal is correctly reported.
# --------------------------------------------------------------------------- #

def test_flag_on_rips_hand_track_blocker_and_routes(tmp_path) -> None:
    board_dir = _write_wall_board(tmp_path)

    res = router.route_nets(board_dir, connections=_wall_connections(), write=False,
                            allow_hand_copper_ripup=True)

    by_net = {c["net"]: c for c in res["connections"]}
    assert by_net[_TEST_NET]["routed"] is True, by_net[_TEST_NET]
    assert by_net[_TEST_NET]["self_check"]["passed"] is True
    assert res["allow_hand_copper_ripup"] is True

    ripped = res["human_copper_ripped"]
    assert len(ripped) >= 1
    assert res["summary"]["human_copper_ripped_count"] == len(ripped)
    for r in ripped:
        assert r["kind"] == "segment"
        assert r["net"] == _WALL_NET
        assert r["uuid"] in ("wall-seg-fcu-0001", "wall-seg-bcu-0001")
        assert r["ripped_for_net"] == _TEST_NET
    # The F.Cu wall piece (the one actually on the routed path) must be among
    # the ripped set - the connection routed on F.Cu only (from_layers/
    # to_layers pin it there), so only the F.Cu wall piece could possibly be
    # "on the path"; the B.Cu twin is never touched (never on the path).
    ripped_uuids = {r["uuid"] for r in ripped}
    assert "wall-seg-fcu-0001" in ripped_uuids
    assert "wall-seg-bcu-0001" not in ripped_uuids

    # Per-connection record also carries the audit trail.
    assert by_net[_TEST_NET].get("human_copper_ripped") == ripped

    # write=False (preview): board text untouched.
    assert _wall_uuids(board_dir) == {"wall-seg-fcu-0001", "wall-seg-bcu-0001"}


def test_flag_on_write_true_removes_reported_hand_copper_and_stays_consistent(tmp_path) -> None:
    board_dir = _write_wall_board(tmp_path)

    res = router.route_nets(board_dir, connections=_wall_connections(), write=True,
                            allow_hand_copper_ripup=True)

    assert res["written"] is True
    ripped = res["human_copper_ripped"]
    assert len(ripped) == 1
    assert res["summary"]["human_copper_removed_from_board"] == 1

    remaining = _wall_uuids(board_dir)
    ripped_uuid = ripped[0]["uuid"]
    assert ripped_uuid not in remaining          # the ripped piece is gone from the board TEXT
    assert remaining == {"wall-seg-fcu-0001", "wall-seg-bcu-0001"} - {ripped_uuid}  # the other one survives

    # The new TEST-net copper was actually written.
    text = (board_dir / "wall.kicad_pcb").read_text(encoding="utf-8")
    assert f'(net "{_TEST_NET}")' in text


# --------------------------------------------------------------------------- #
# (c) flag ON still correctly refuses to rip a pad or a zone fill - out of
#     scope obstacle kinds are never added to the rippable set. Uses the same
#     `_self_check` fault-injection strategy as `test_ripup_selfcheck.py`
#     (precise, deterministic - avoids needing to engineer real pad/zone
#     geometry that happens to produce a coarse-vs-exact mismatch).
# --------------------------------------------------------------------------- #

def _simple_two_conn_project(tmp_path: Path) -> Path:
    write_synthetic_project(tmp_path, project_name="ripup2", mode="simple",
                            component_count=2, route=False, layers=2)
    (tmp_path / "pcb_settings.json").write_text(
        json.dumps({"autorouter": {"cpu": {"workers": 1}}}), encoding="utf-8")
    return tmp_path


def _two_conns() -> list[dict]:
    return [
        {"net": "A", "from_point": {"x": 5.0, "y": 20.0}, "to_point": {"x": 15.0, "y": 20.0},
         "airline_length_mm": 10.0, "from_layers": ["F.Cu"], "to_layers": ["F.Cu"]},
        {"net": "B", "from_point": {"x": 5.0, "y": 30.0}, "to_point": {"x": 15.0, "y": 30.0},
         "airline_length_mm": 10.0, "from_layers": ["F.Cu"], "to_layers": ["F.Cu"]},
    ]


def _fake_owner_none_violation(against_kind: str, is_pad: bool) -> dict:
    return {"kind": "segment", "layer": "F.Cu", "against_net": "SOMENET",
            "against_kind": against_kind, "required_mm": 0.5, "owner": None,
            "against_is_pad": is_pad, "obstacle_uuid": "fake-uuid-not-in-pool",
            "hand_copper": against_kind in ("seg", "pt") and not is_pad}


def _patch_self_check_for_b(monkeypatch, violation: dict, fail_calls: int = 2):
    """Same call-numbering discipline as `test_ripup_selfcheck.py`'s helper:
    net B's calls #2..#(1+fail_calls) are faked to report `violation`; every
    other call runs the real `_self_check`."""
    real_self_check = router._self_check
    counts = {"B": 0}

    def fake(net, segments, vias, obstacles, rules, via_radius):
        if net == "B":
            counts["B"] += 1
            if 2 <= counts["B"] <= 1 + fail_calls:
                return [violation]
        return real_self_check(net, segments, vias, obstacles, rules, via_radius)

    monkeypatch.setattr(router, "_self_check", fake)
    return counts


def test_pad_violation_never_rippable_even_with_flag_on(tmp_path, monkeypatch) -> None:
    proj = _simple_two_conn_project(tmp_path)
    _patch_self_check_for_b(monkeypatch, _fake_owner_none_violation("pt", is_pad=True))

    res = router.route_nets(proj, connections=_two_conns(), write=False,
                            allow_hand_copper_ripup=True)

    by_net = {c["net"]: c for c in res["connections"]}
    assert by_net["B"]["routed"] is False
    assert by_net["B"]["failure"]["reason"] == "self_check_failed"
    assert res["human_copper_ripped"] == []
    assert res["ripup"]["connections_ripped"] == 0


def test_zone_violation_never_rippable_even_with_flag_on(tmp_path, monkeypatch) -> None:
    proj = _simple_two_conn_project(tmp_path)
    _patch_self_check_for_b(monkeypatch, _fake_owner_none_violation("zone", is_pad=False))

    res = router.route_nets(proj, connections=_two_conns(), write=False,
                            allow_hand_copper_ripup=True)

    by_net = {c["net"]: c for c in res["connections"]}
    assert by_net["B"]["routed"] is False
    assert by_net["B"]["failure"]["reason"] == "self_check_failed"
    assert res["human_copper_ripped"] == []
    assert res["ripup"]["connections_ripped"] == 0


def test_edge_violation_never_rippable_even_with_flag_on(tmp_path, monkeypatch) -> None:
    proj = _simple_two_conn_project(tmp_path)
    _patch_self_check_for_b(monkeypatch, _fake_owner_none_violation("edge", is_pad=False))

    res = router.route_nets(proj, connections=_two_conns(), write=False,
                            allow_hand_copper_ripup=True)

    by_net = {c["net"]: c for c in res["connections"]}
    assert by_net["B"]["routed"] is False
    assert by_net["B"]["failure"]["reason"] == "self_check_failed"
    assert res["human_copper_ripped"] == []


# --------------------------------------------------------------------------- #
# (e) determinism: repeated dry-run calls reproduce byte-identical output.
# --------------------------------------------------------------------------- #

def test_hand_copper_ripup_is_deterministic(tmp_path) -> None:
    board_dir1 = _write_wall_board(tmp_path / "run1")
    board_dir2 = _write_wall_board(tmp_path / "run2")

    res1 = router.route_nets(board_dir1, connections=_wall_connections(), write=False,
                             allow_hand_copper_ripup=True)
    res2 = router.route_nets(board_dir2, connections=_wall_connections(), write=False,
                             allow_hand_copper_ripup=True)

    dump1 = json.dumps(sorted(res1["connections"], key=lambda c: c["net"]), sort_keys=True)
    dump2 = json.dumps(sorted(res2["connections"], key=lambda c: c["net"]), sort_keys=True)
    assert dump1 == dump2
    assert (json.dumps(res1["human_copper_ripped"], sort_keys=True)
            == json.dumps(res2["human_copper_ripped"], sort_keys=True))

    # And again, same board dir a second time (in-process re-run) - a ripped
    # piece can never be ripped a SECOND time (it is removed from the board on
    # write, and the pool guard also prevents re-selecting an already-ripped
    # uuid within a single run - see `_commit_hand_copper_rip`).
    res3 = router.route_nets(board_dir1, connections=_wall_connections(), write=False,
                             allow_hand_copper_ripup=True)
    dump3 = json.dumps(sorted(res3["connections"], key=lambda c: c["net"]), sort_keys=True)
    assert dump1 == dump3
