"""Tests for rip-up demotion of a `self_check_failed` failure (a "plane-skim":
a path clears the coarse A* obstacle model but fails the exact geometric
clearance check in `_finalize_core`).

Background: the worklist's Step 4 rip-up loop (`route_nets`, ~line 5300) used
to demote ONLY `unreachable_in_window` failures back to rip-up-and-reroute.
`self_check_failed` was a hard, terminal failure even when the collision the
exact check found was against another AUTOROUTER-PLACED (hence rippable)
connection, rather than a filled zone/plane/pad/edge/hand-track (never
rippable). `_self_check` (kicad_router_tool.py) now tags every violation with
`owner` (None for non-rippable board copper, an int connection id for
rippable autorouter copper - see its docstring), and the worklist's rip-up
step reads that to rip precisely the owning connection(s) and re-finalize the
SAME already-found path (no fresh search needed - the path was geometrically
fine except for the specific flagged conflicts).

Fault-injection strategy: rather than trying to engineer real board geometry
that naturally produces a coarse-vs-exact clearance mismatch (fragile, grid-
dependent), these tests monkeypatch `_self_check` to force a self_check_failed
outcome for one connection ("B") with a controlled `owner` on the violation,
and verify the worklist responds exactly as the mechanism promises:
  (a) owner points at an already-placed connection -> demoted, ripped, B routes.
  (b) owner is None (a non-rippable obstacle) -> stays a hard failure, no rip.
  (c) determinism: (a) reproduces byte-identically across repeated runs.
"""

from __future__ import annotations

import json
from pathlib import Path

import kicad_router_tool as router
from synthetic_board import write_synthetic_project


def _write_project(tmp_path: Path) -> Path:
    write_synthetic_project(tmp_path, project_name="ripup", mode="simple",
                            component_count=2, route=False, layers=2)
    # Force serial (workers=1) so `_run_independent_routes` falls back to the
    # in-process loop (see its docstring) - the speculative pass then also runs
    # in THIS process, where the `_self_check` monkeypatch below applies. A real
    # worker-pool run would pickle `_route_one` into a subprocess the test
    # process's monkeypatch cannot reach.
    (tmp_path / "pcb_settings.json").write_text(
        json.dumps({"autorouter": {"cpu": {"workers": 1}}}), encoding="utf-8")
    return tmp_path


def _connections() -> list[dict]:
    # Two independent straight single-layer connections, well clear of the
    # synthetic board's own R1/R2 footprint pads (at y=10) so neither collides
    # with real board copper. Same priority/airline -> tie-broken by net name
    # ("A" < "B"), so canonical order is deterministic: A = owner 0, B = owner 1.
    return [
        {"net": "A", "from_point": {"x": 5.0, "y": 20.0}, "to_point": {"x": 15.0, "y": 20.0},
         "airline_length_mm": 10.0, "from_layers": ["F.Cu"], "to_layers": ["F.Cu"]},
        {"net": "B", "from_point": {"x": 5.0, "y": 30.0}, "to_point": {"x": 15.0, "y": 30.0},
         "airline_length_mm": 10.0, "from_layers": ["F.Cu"], "to_layers": ["F.Cu"]},
    ]


def _fake_violation(owner: int | None) -> dict:
    return {"kind": "segment", "layer": "F.Cu", "against_net": "A",
            "against_kind": "seg", "required_mm": 0.5, "owner": owner}


def _patch_self_check_for_b(monkeypatch, owner_to_blame: int | None, fail_calls: int):
    """Force self-checks #2..#(1+fail_calls) of net "B" to report a single
    violation blaming `owner_to_blame`; every other call (net "A", B's call #1,
    or B past that window) runs the REAL `_self_check` unmodified.

    Call #1 for "B" is `_route_one`'s OWN internal ladder self-check (inside
    `_finalize_core`), run during the 7.8b SPECULATIVE pass against base-only
    obstacles - it must stay real (routed=True, clean) so the speculative
    result is "routed" and reaches the parent's commit-time self-check at all;
    faking call #1 would make the speculative worker itself report
    `routed: False`, which the parent treats as TERMINAL (never requeued - see
    `route_nets`'s determinism comment on the speculative pass), so B would
    never reach the serial worklist / rip-up step this test exercises.

    `fail_calls=2` therefore fakes calls #2 (the speculative-pass commit
    self-check, which bumps B into the serial worklist without placing or
    failing it - see the "falls into pending" comment in `route_nets`) and #3
    (the first serial-pass `_finalize_core` self-check, which produces the
    `self_check_failed` the rip-up step must handle). The rip-up step's own
    re-finalize is call #4 and runs for real."""
    real_self_check = router._self_check
    counts = {"B": 0}

    def fake(net, segments, vias, obstacles, rules, via_radius, *args, **kwargs):
        if net == "B":
            counts["B"] += 1
            if 2 <= counts["B"] <= 1 + fail_calls:
                return [_fake_violation(owner_to_blame)]
        return real_self_check(net, segments, vias, obstacles, rules, via_radius, *args, **kwargs)

    monkeypatch.setattr(router, "_self_check", fake)
    return counts


def test_self_check_failed_demoted_to_ripup_against_rippable_copper(tmp_path, monkeypatch) -> None:
    proj = _write_project(tmp_path)
    counts = _patch_self_check_for_b(monkeypatch, owner_to_blame=0, fail_calls=2)

    res = router.route_nets(proj, connections=_connections(), write=False)

    by_net = {c["net"]: c for c in res["connections"]}
    assert by_net["B"]["routed"] is True, by_net["B"]
    assert by_net["B"]["self_check"]["passed"] is True
    assert by_net["B"].get("ripped_to_place") == [0]
    # A was ripped to make room, then successfully re-routed (re-queued).
    assert by_net["A"]["routed"] is True
    assert counts["B"] >= 3  # speculative ladder + commit-check + serial ladder
    ripup = res["ripup"]
    assert ripup["connections_ripped"] >= 1
    assert ripup["iterations"] >= 1


def test_self_check_failed_against_non_rippable_copper_stays_hard_failure(tmp_path, monkeypatch) -> None:
    proj = _write_project(tmp_path)
    counts = _patch_self_check_for_b(monkeypatch, owner_to_blame=None, fail_calls=2)

    res = router.route_nets(proj, connections=_connections(), write=False)

    by_net = {c["net"]: c for c in res["connections"]}
    assert by_net["B"]["routed"] is False
    assert by_net["B"]["failure"]["reason"] == "self_check_failed"
    assert "ripped_to_place" not in by_net["B"]
    # A is untouched - never ripped, since the only violation had no owner.
    assert by_net["A"]["routed"] is True
    assert "ripped_to_place" not in by_net["A"]
    assert res["ripup"]["connections_ripped"] == 0
    assert counts["B"] >= 3  # speculative ladder + commit-check + serial ladder


def test_self_check_ripup_demotion_is_deterministic(tmp_path, monkeypatch) -> None:
    proj = _write_project(tmp_path)

    _patch_self_check_for_b(monkeypatch, owner_to_blame=0, fail_calls=2)
    res1 = router.route_nets(proj, connections=_connections(), write=False)

    _patch_self_check_for_b(monkeypatch, owner_to_blame=0, fail_calls=2)
    res2 = router.route_nets(proj, connections=_connections(), write=False)

    dump1 = json.dumps(sorted(res1["connections"], key=lambda c: c["net"]), sort_keys=True)
    dump2 = json.dumps(sorted(res2["connections"], key=lambda c: c["net"]), sort_keys=True)
    assert dump1 == dump2
    assert res1["ripup"] == res2["ripup"]
