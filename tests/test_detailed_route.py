"""Tests for Phase 7.3b detailed (fine, windowed) routing.

Covers, on a SCRATCH copy of the real kiln board (never the real files):
  1. Rule resolution never trusts a bare-0 clearance (7.11 anchor's note): the
     resolved clearance comes from the Default net-class, > 0.
  2. Preview routes a genuinely-unrouted kiln connection with a passing
     self-check (Python clearance proof before any write).
  3. Determinism: two preview runs of the same connection are byte-identical.
  4. Write actually joins the connection - the board's missing-connection count
     drops and the net reports `routed` (island_count 1).
  5. `unroute_nets` deletes exactly the autorouter-owned copper it wrote and
     restores the prior connectivity.
  6. Acceptance gate (7.11 -> 7.3b): kicad-cli pcb drc on the written scratch
     board introduces NO new violations vs. the pre-route baseline (auto-skips
     when kicad-cli is absent, like the M0 harness).
  7. Both tools are registered on the MCP server.

The router is slow on this plane-heavy board (per-connection windowed A* in pure
Python), so every test routes ONE specific connection (via `connections=[...]`),
never the whole board.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

import kicad_pcb_tool as pcb
import kicad_router_tool as router

# Candidate genuinely-unrouted kiln connections that route on a single layer
# through a pour-free channel (no plane engine needed). The first that previews
# routed + self-check-clean is used; kept as a short list so a board tweak that
# routes one of them by hand still leaves the tests a target.
_CANDIDATE_NETS = ["/SaftyProcessor/Current3", "3.3V_Main"]


def _find_kicad_cli() -> str | None:
    on_path = shutil.which("kicad-cli") or shutil.which("kicad-cli.exe")
    if on_path:
        return on_path
    candidates = list(Path("C:/Program Files/KiCad").glob("*/bin/kicad-cli.exe"))
    candidates += list(Path("C:/Program Files (x86)/KiCad").glob("*/bin/kicad-cli.exe"))
    for cand in candidates:
        if cand.exists():
            return str(cand)
    return None


_KICAD_CLI = _find_kicad_cli()


def _pick_connection(project_path) -> dict:
    """The first candidate net whose single connection previews routed with a
    passing self-check. Skips the whole module if none route (board changed)."""
    rats = router.get_ratsnest(project_path)
    by_net = {c["net"]: c for c in rats["connections"]}
    for net in _CANDIDATE_NETS:
        conn = by_net.get(net)
        if conn is None:
            continue
        res = router.route_nets(project_path, connections=[conn], write=False)
        rec = res["connections"][0]
        if rec["routed"] and rec["self_check"]["passed"]:
            return conn
    pytest.skip("No candidate kiln connection routed cleanly (board changed?).")


# --------------------------------------------------------------------------- #
# Rule resolution
# --------------------------------------------------------------------------- #

def test_clearance_never_bare_zero(scratch_board: Path) -> None:
    settings = pcb.load_pcb_settings(scratch_board)["config"]
    rules = router._resolve_route_rules(scratch_board, settings)
    # kiln's merged DRC clearance is a bare board-rule 0.0; the router must NOT
    # use it - it resolves from the Default net-class instead (0.2 mm).
    assert rules["clearance"] > 0.0
    assert rules["clearance_source"] in ("default_netclass", "merged_drc")
    assert rules["track_width"] > 0.0
    assert rules["via_diameter"] > 0.0


# --------------------------------------------------------------------------- #
# Preview + self-check + determinism
# --------------------------------------------------------------------------- #

def test_preview_routes_with_passing_self_check(scratch_board: Path) -> None:
    conn = _pick_connection(scratch_board)
    res = router.route_nets(scratch_board, connections=[conn], write=False)
    assert res["written"] is False
    rec = res["connections"][0]
    assert rec["routed"] is True
    assert rec["self_check"]["passed"] is True
    assert rec["self_check"]["violation_count"] == 0
    assert rec["length_mm"] > 0.0
    assert rec["via_count"] >= 0
    assert rec["layers"], "a routed connection reports the layers it used"
    # nothing was written in a preview run
    assert res["summary"]["connections_routed"] == 1


def test_preview_is_deterministic(scratch_board: Path) -> None:
    conn = _pick_connection(scratch_board)
    a = router.route_nets(scratch_board, connections=[conn], write=False)
    b = router.route_nets(scratch_board, connections=[conn], write=False)
    assert json.dumps(a["connections"], sort_keys=True) == json.dumps(b["connections"], sort_keys=True)


# --------------------------------------------------------------------------- #
# Write joins connectivity; unroute restores it
# --------------------------------------------------------------------------- #

def test_write_joins_connectivity_and_unroute_restores(scratch_board: Path) -> None:
    conn = _pick_connection(scratch_board)
    net = conn["net"]
    before = router.get_ratsnest(scratch_board)["summary"]["total_connections"]

    res = router.route_nets(scratch_board, connections=[conn], write=True)
    assert res["written"] is True
    assert res["summary"]["segments_emitted"] >= 1

    after = router.get_ratsnest(scratch_board)
    assert after["summary"]["total_connections"] == before - 1
    assert after["per_net"][net]["status"] == "routed"
    assert after["per_net"][net]["missing_connections"] == 0

    # board-local ownership recorded for the net
    owned = pcb.load_board_local(scratch_board)["data"]["autorouter_owned"]
    assert len(owned["segments"]) == res["summary"]["segments_emitted"]
    assert any(r["net"] == net for r in owned["records"])

    # unroute removes exactly what was written and restores connectivity
    un = router.unroute_nets(scratch_board, nets=[net], write=True)
    assert un["written"] is True
    assert un["removed"] == res["summary"]["segments_emitted"] + res["summary"]["vias_emitted"]
    restored = router.get_ratsnest(scratch_board)["summary"]["total_connections"]
    assert restored == before
    owned_after = pcb.load_board_local(scratch_board)["data"]["autorouter_owned"]
    assert owned_after["segments"] == []
    assert owned_after["vias"] == []


def test_unroute_preview_touches_nothing(scratch_board: Path) -> None:
    conn = _pick_connection(scratch_board)
    net = conn["net"]
    router.route_nets(scratch_board, connections=[conn], write=True)
    board_bytes = (scratch_board / "kiln.kicad_pcb").read_bytes()
    preview = router.unroute_nets(scratch_board, nets=[net], write=False)
    assert preview["written"] is False
    assert preview["candidates"] >= 1
    assert preview["removed"] == 0
    # board file unchanged by a preview
    assert (scratch_board / "kiln.kicad_pcb").read_bytes() == board_bytes


# --------------------------------------------------------------------------- #
# Acceptance gate: kicad-cli DRC introduces no new violations
# --------------------------------------------------------------------------- #

def _drc_violations(cli: str, board: Path, report: Path) -> list[dict]:
    subprocess.run(
        [cli, "pcb", "drc", "--format", "json", "--severity-all", str(board), "-o", str(report)],
        capture_output=True, text=True, timeout=120,
    )
    return json.loads(report.read_text(encoding="utf-8")).get("violations", [])


def _violation_sig(v: dict) -> tuple:
    return (
        v.get("type"),
        v.get("severity"),
        tuple(sorted(item.get("description", "") for item in v.get("items", []))),
    )


@pytest.mark.skipif(_KICAD_CLI is None, reason="kicad-cli not found; acceptance gate skipped")
def test_kicad_cli_drc_no_new_violations(scratch_board: Path) -> None:
    board = scratch_board / "kiln.kicad_pcb"
    conn = _pick_connection(scratch_board)

    baseline = _drc_violations(_KICAD_CLI, board, scratch_board / "drc_base.json")
    res = router.route_nets(scratch_board, connections=[conn], write=True)
    assert res["written"] is True

    post = _drc_violations(_KICAD_CLI, board, scratch_board / "drc_post.json")

    # multiset difference: a post violation is NEW only if it isn't matched by a
    # baseline one of the same signature.
    remaining: dict[tuple, int] = {}
    for v in baseline:
        remaining[_violation_sig(v)] = remaining.get(_violation_sig(v), 0) + 1
    new_violations = []
    for v in post:
        sig = _violation_sig(v)
        if remaining.get(sig, 0) > 0:
            remaining[sig] -= 1
        else:
            new_violations.append(v)

    assert not new_violations, (
        f"routing introduced {len(new_violations)} new DRC violation(s): "
        f"{[_violation_sig(v) for v in new_violations[:5]]}"
    )


# --------------------------------------------------------------------------- #
# Tool registration
# --------------------------------------------------------------------------- #

def test_router_tools_registered() -> None:
    from kicad_mcp_server import KiCadMcpServer

    tools = KiCadMcpServer().tools
    assert "route_kicad_nets" in tools
    assert "unroute_kicad_nets" in tools
    for name in ("route_kicad_nets", "unroute_kicad_nets"):
        assert callable(tools[name]["handler"])
        assert "project_path" in tools[name]["inputSchema"]["properties"]


# --------------------------------------------------------------------------- #
# Step 4 - rip-up & reroute (negotiated congestion), on a synthetic congestion
# board (the real kiln board has no natural single-lane rip-up scenario cheap
# enough to route in pure Python, so these use a small forced-congestion board).
#
# Board model: a horizontal GND "wall" at y=15 on BOTH copper layers with 1.4 mm
# gaps; routing is pinned to F.Cu (allowed_layers) so the wall is a true planar
# barrier a via cannot bypass. Nets cross top<->bottom only through a gap.
# --------------------------------------------------------------------------- #

from synthetic_board import (  # noqa: E402
    _HEADER_TEMPLATE, _layer_stack_lines, _net_table, _synthetic_kicad_pro_text,
)

_HDR2 = _HEADER_TEMPLATE.format(layer_lines=_layer_stack_lines(2))


def _pad_block(ref: str, x: float, y: float, net: str, uid: str) -> str:
    return (f'    (footprint "synthetic:PAD"\n        (layer "F.Cu")\n        (uuid "{uid}")\n'
            f'        (at {x} {y})\n'
            f'        (property "Reference" "{ref}" (at 0 -1) (layer "F.SilkS"))\n'
            f'        (property "Value" "P" (at 0 1) (layer "F.Fab"))\n'
            f'        (pad "1" smd rect (at 0 0) (size 0.6 0.6) (layers "F.Cu" "F.Paste" "F.Mask") (net "{net}"))\n'
            f'    )\n')


def _seg_block(x1: float, y1: float, x2: float, y2: float, layer: str, net: str, uid: str) -> str:
    return (f'    (segment\n        (start {x1} {y1})\n        (end {x2} {y2})\n'
            f'        (width 0.2)\n        (layer "{layer}")\n        (net "{net}")\n        (uuid "{uid}")\n    )\n')


def _wall_with_gaps(y: float, x0: float, x1: float, gap_centers: list[float], halfgap: float) -> list[str]:
    """GND wall segments on F.Cu + B.Cu spanning [x0, x1] at height `y`, leaving a
    `2*halfgap`-wide opening centred on each gap center."""
    segs: list[str] = []
    x = x0
    for i, gc in enumerate(sorted(gap_centers)):
        if gc - halfgap > x:
            segs.append(_seg_block(x, y, gc - halfgap, y, "F.Cu", "GND", f"wall{i}fF"))
            segs.append(_seg_block(x, y, gc - halfgap, y, "B.Cu", "GND", f"wall{i}fB"))
        x = gc + halfgap
    if x < x1:
        segs.append(_seg_block(x, y, x1, y, "F.Cu", "GND", "wallzF"))
        segs.append(_seg_block(x, y, x1, y, "B.Cu", "GND", "wallzB"))
    return segs


def _write_congestion_project(directory, pads, walls, nets, overrides=None):
    """Write a minimal single-layer-routed congestion project (board + pro +
    board_local priorities + pcb_settings pinning routing to F.Cu)."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    parts = [_HDR2, _net_table(nets + ["GND"])]
    for i, (ref, x, y, net) in enumerate(pads):
        parts.append(_pad_block(ref, x, y, net, f"pad{i:04d}"))
    parts.extend(walls)
    parts.append(")\n")
    (directory / "cong.kicad_pcb").write_text("".join(parts), encoding="utf-8")
    (directory / "cong.kicad_pro").write_text(_synthetic_kicad_pro_text(), encoding="utf-8")
    if overrides:
        pcb.save_board_local(directory, {"version": 1, "net_overrides": overrides})
    (directory / "pcb_settings.json").write_text(
        json.dumps({"autorouter": {"allowed_layers": ["F.Cu"]}}), encoding="utf-8")
    pcb._invalidate_board_cache(directory / "cong.kicad_pcb")
    return directory


def _by_net(res):
    return {c["net"]: c for c in res["connections"]}


def test_ripup_reroutes_earlier_autorouter_connection(tmp_path: Path) -> None:
    """NETF cannot route without rip-up (its only gap is taken by the earlier,
    higher-priority NETG and its second gap is beyond window reach); with rip-up
    NETF rips NETG, takes the gap, and NETG RE-ROUTES to the far gap - both end
    routed. Proves step 4 rips autorouter-owned copper and re-routes the ripped
    set (corridor choice changed)."""
    pads = [("G1", 10, 5, "NETG"), ("G2", 60, 25, "NETG"),
            ("F1", 11, 10, "NETF"), ("F2", 9, 20, "NETF")]
    walls = _wall_with_gaps(15.0, 0.0, 80.0, [10.0, 72.0], 0.7)
    proj = _write_congestion_project(
        tmp_path / "rip", pads, walls, ["NETG", "NETF"],
        overrides={"NETG": {"priority": 10}, "NETF": {"priority": 0}})

    res = router.route_nets(proj, write=False)
    assert res["ripup_active"] is True
    conns = _by_net(res)

    # Without rip-up NETF has no route; with rip-up BOTH route.
    assert conns["NETG"]["routed"] is True
    assert conns["NETF"]["routed"] is True
    assert res["summary"]["connections_routed"] == 2

    # Rip-up actually happened: NETF ripped an earlier connection to place itself.
    rip = res["ripup"]
    assert rip["iterations"] >= 1
    assert rip["connections_ripped"] >= 1
    assert rip["congestion_escalations"] >= 1
    assert conns["NETF"].get("ripped_to_place"), "NETF records the owner(s) it ripped"

    # NETG's re-route uses the FAR gap (x~72): its corridor choice changed, so it
    # is a genuinely different (longer) path than a straight NETG-first route.
    assert conns["NETG"]["length_mm"] > conns["NETF"]["length_mm"]
    assert conns["NETG"]["self_check"]["passed"] is True
    assert conns["NETF"]["self_check"]["passed"] is True

    # Determinism: a second preview is byte-identical.
    res2 = router.route_nets(proj, write=False)
    assert json.dumps(res["connections"], sort_keys=True) == json.dumps(res2["connections"], sort_keys=True)

    # Write path: both nets' copper is emitted and recorded as autorouter-owned.
    wr = router.route_nets(proj, write=True)
    assert wr["written"] is True
    assert wr["summary"]["connections_routed"] == 2
    owned = pcb.load_board_local(proj)["data"]["autorouter_owned"]
    assert len(owned["segments"]) == wr["summary"]["segments_emitted"]
    owned_nets = {r["net"] for r in owned["records"]}
    assert {"NETG", "NETF"} <= owned_nets

    # Emitted geometry is deterministic (uuid-stripped) across write runs.
    import re
    def _geo(txt):
        return sorted(re.findall(
            r"\(segment\s+\(start ([\-\d.]+) ([\-\d.]+)\)\s+\(end ([\-\d.]+) ([\-\d.]+)\)", txt))
    geo1 = _geo((proj / "cong.kicad_pcb").read_text(encoding="utf-8"))
    router.unroute_nets(proj, write=True)
    router.route_nets(proj, write=True)
    geo2 = _geo((proj / "cong.kicad_pcb").read_text(encoding="utf-8"))
    assert geo1 == geo2 and len(geo1) >= 2


def test_human_copper_is_never_ripped(tmp_path: Path) -> None:
    """A net blocked SOLELY by human/board copper (a solid GND wall with no gap)
    FAILS with the blocker named - the router never rips human copper to make
    room, and performs zero rip-up iterations."""
    pads = [("HH1", 20, 5, "NETHH"), ("HH2", 20, 25, "NETHH")]
    walls = (_wall_with_gaps(15.0, 0.0, 40.0, [], 0.7)  # solid F.Cu+B.Cu wall, no gaps
             )
    proj = _write_congestion_project(tmp_path / "human", pads, walls, ["NETHH"])

    gnd_before = (proj / "cong.kicad_pcb").read_text(encoding="utf-8").count('(net "GND")')

    res = router.route_nets(proj, write=True)  # write=True proves nothing human is deleted
    conns = _by_net(res)
    rec = conns["NETHH"]

    assert rec["routed"] is False
    assert rec["failure"]["reason"] == "unreachable_in_window"
    # the blocker is NAMED and it is the human GND wall.
    assert rec["failure"]["nearest_blocker"]["net"] == "GND"
    # NOTHING was ripped (human copper is not rippable), no rip-up iterations.
    assert res["ripup"]["iterations"] == 0
    assert res["ripup"]["connections_ripped"] == 0
    assert res["summary"]["connections_routed"] == 0
    # the human GND copper is still fully present on the board.
    gnd_after = (proj / "cong.kicad_pcb").read_text(encoding="utf-8").count('(net "GND")')
    assert gnd_after == gnd_before
