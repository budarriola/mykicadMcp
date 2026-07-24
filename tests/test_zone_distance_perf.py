"""Tests for the zone-edge spatial index (`_ZoneEdgeGrid`) that replaces the
old O(cells x zone_edges) linear scan in `_FineWindow.obstacle_cells`.

`_min_dist_to_edges_ref` is the untouched original linear-scan implementation,
kept as the correctness oracle. `_ZoneEdgeGrid` buckets a window's
already-clipped zone edges into cells sized to the window's own clearance
reach so each query point only scans its own bucket - see the class
docstring in kicad_router_tool.py for the exactness argument.

These tests build a REAL `_FineWindow` over a genuine zone-heavy area of the
kiln board (never writing to it - `scratch_board`/`kiln_project_path` are
read/copy-only) and assert:

  1. Parity: for a real zone obstacle's clipped edges, `_ZoneEdgeGrid.min_dist`
     agrees with the linear-scan reference at every query point on a fine
     sample grid across the window (both "on the fill" and "off the fill"
     points), AND the full `obstacle_cells(...)` output (via_cells,
     track_cells - the actual thing routing decisions read) is byte-identical
     whether `_ZoneEdgeGrid` or the reference scan backs it.
  2. Determinism: two `route_nets` preview runs of the same real connection
     produce identical emitted geometry (proves the new indexing path has no
     hidden nondeterminism - dict/set iteration order, etc.).

Kept fast: both tests operate on a single connection's window, never a
full-board route.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import kicad_pcb_tool as pcb
import kicad_router_tool as router

# Real zone-heavy kiln connections (ground/power rails riding over the big
# copper pours) - the exact scenario the perf fix targets.
_ZONE_HEAVY_NETS = ["GND_Main", "12V_Main", "3.3V_Main"]


def _real_window_and_zone_obstacles(board_path: Path) -> tuple[router._FineWindow, list]:
    """Build one real `_FineWindow` (same construction `route_nets` uses) over
    a genuine zone-heavy kiln connection's bbox, and return it plus the
    zone-kind obstacles active in it (net != this window's net, so they are
    NOT skipped as same-net-free)."""
    project_path = board_path
    board_path, _project_file, _ = pcb._resolve_project_path(project_path)
    settings = pcb.load_pcb_settings(project_path)["config"]
    rules = router._resolve_route_rules(project_path, settings)
    autor = settings.get("autorouter", {})
    grid = float(autor.get("grid_mm", 0.2)) or 0.2
    max_grid_mm = float(autor.get("max_grid_mm", 1.0)) or 1.0
    margin = float(autor.get("search_window_margin_mm", 8.0)) or 8.0

    all_layers = pcb._parse_board_layers_cached(board_path)
    all_cu = [l["name"] for l in all_layers] or ["F.Cu", "B.Cu"]
    routable_types = {"signal", "power", "mixed", "jumper"}
    layer_types: dict[str, str] = {}
    routable_layers: list[str] = []
    for l in all_layers:
        if l["type"] not in routable_types:
            continue
        routable_layers.append(l["name"])
        layer_types[l["name"]] = l["type"]
    routable_set = set(routable_layers)

    obstacles = router._collect_obstacles(board_path, routable_set, all_cu, rules["edge_clearance"])
    board_bbox = router._board_bbox(board_path)

    rats = router.get_ratsnest(project_path)
    by_net: dict[str, list[dict]] = {}
    for c in rats["connections"]:
        by_net.setdefault(c["net"], []).append(c)

    for net in _ZONE_HEAVY_NETS:
        conns = by_net.get(net)
        if not conns:
            continue
        conn = conns[0]
        from_xy, to_xy = router._conn_endpoints(conn)
        minx = max(min(from_xy[0], to_xy[0]) - margin, board_bbox[0] - grid)
        miny = max(min(from_xy[1], to_xy[1]) - margin, board_bbox[1] - grid)
        maxx = min(max(from_xy[0], to_xy[0]) + margin, board_bbox[2] + grid)
        maxy = min(max(from_xy[1], to_xy[1]) + margin, board_bbox[3] + grid)
        win_grid = router._choose_grid(maxx - minx, maxy - miny, len(routable_layers),
                                       grid, max_grid_mm, router._MAX_WINDOW_NODES)
        win = router._FineWindow(minx, miny, maxx, maxy, win_grid, routable_layers,
                                 layer_types, net)
        if win.cols * win.rows * max(1, len(routable_layers)) > router._MAX_WINDOW_NODES:
            continue
        track_half = rules["track_width"] / 2.0
        via_radius = rules["via_diameter"] / 2.0
        win._track_half = track_half
        win._via_radius = via_radius
        win._clearance = rules["clearance"]
        win._edge_clearance = rules["edge_clearance"]
        zone_obs = [ob for ob in obstacles if ob.kind == "zone" and ob.net != net]
        if zone_obs:
            return win, zone_obs
    pytest.skip("No zone-heavy kiln connection with active zone obstacles found (board changed?).")


class _RefZoneEdgeGrid:
    """Drop-in stand-in for `_ZoneEdgeGrid` backed by the untouched linear
    scan (`_min_dist_to_edges_ref`) instead of the bucketed index - used to
    get the "before" answer for the exact same `obstacle_cells` code path."""

    __slots__ = ("edges",)

    def __init__(self, edges, reach):  # noqa: ARG002 - reach unused by the reference
        self.edges = edges

    def min_dist(self, px: float, py: float) -> float:
        return router._min_dist_to_edges_ref(px, py, self.edges)


def test_zone_edge_grid_matches_reference_obstacle_cells(kiln_project_path: Path) -> None:
    win, zone_obs = _real_window_and_zone_obstacles(kiln_project_path)
    assert zone_obs, "expected at least one zone obstacle in the sampled window"

    for ob in zone_obs[:5]:  # a handful of real zone fills is plenty of edges
        fast = win.obstacle_cells(ob)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(router, "_ZoneEdgeGrid", _RefZoneEdgeGrid)
            ref = win.obstacle_cells(ob)
        assert fast == ref, f"obstacle_cells mismatch for zone net={ob.net!r}"


def test_zone_edge_grid_min_dist_matches_reference_on_sample_points(kiln_project_path: Path) -> None:
    """Direct unit-level parity of the index itself (not just its
    integration): sample query points across the window on a fine grid
    (finer than the window's own node spacing, so some points sit exactly on
    fill and some sit strictly between edges) and require the fast index's
    threshold comparisons agree with the reference at every clearance
    threshold the router actually uses (`track_reach`, `via_reach`)."""
    win, zone_obs = _real_window_and_zone_obstacles(kiln_project_path)

    margin = router._FINE_CELL_MARGIN_FRAC * win.grid
    wminx = win.minx - win.grid
    wminy = win.miny - win.grid
    wmaxx = win.minx + (win.cols - 1) * win.grid + win.grid
    wmaxy = win.miny + (win.rows - 1) * win.grid + win.grid
    clearance = win._clearance

    # Not every zone obstacle in the window's neighborhood necessarily has an
    # edge clipped into THIS window (some are only caught by the coarser
    # bbox+reach prefilter in `obstacle_cells`); use the first one that does,
    # so the test exercises real, non-trivial edge data.
    zedges = None
    for ob in zone_obs:
        assert ob.pts
        track_reach = win._track_half + clearance + ob.half + margin
        via_reach = win._via_radius + clearance + ob.half + margin
        big = max(track_reach, via_reach)
        candidate = router._clip_polygon_edges(ob.pts, wminx - big, wminy - big, wmaxx + big, wmaxy + big)
        if candidate:
            zedges = candidate
            break
    assert zedges, "expected at least one real zone polygon with edges near this window"
    grid_index = router._ZoneEdgeGrid(zedges, big)

    # Sample on a finer pitch than the window grid so points fall strictly
    # between grid nodes too, not only exactly on them.
    n_samples = 25
    checked_blocked = checked_clear = 0
    for i in range(n_samples):
        for j in range(n_samples):
            px = wminx + (wmaxx - wminx) * i / (n_samples - 1)
            py = wminy + (wmaxy - wminy) * j / (n_samples - 1)
            fast = grid_index.min_dist(px, py)
            ref = router._min_dist_to_edges_ref(px, py, zedges)
            assert (fast < track_reach) == (ref < track_reach), (px, py, fast, ref, track_reach)
            assert (fast < via_reach) == (ref < via_reach), (px, py, fast, ref, via_reach)
            if ref < track_reach:
                checked_blocked += 1
            else:
                checked_clear += 1
    # sanity: the sample actually exercised both branches (a real zone edge
    # window has both near-fill and far-from-fill points), otherwise the
    # parity check above would be vacuous.
    assert checked_blocked > 0
    assert checked_clear > 0


# --------------------------------------------------------------------------- #
# Determinism
# --------------------------------------------------------------------------- #

_DETERMINISM_CANDIDATES = ["GND_Main", "/SaftyProcessor/Current3", "3.3V_Main"]


def test_route_preview_is_deterministic_across_runs(scratch_board: Path) -> None:
    rats = router.get_ratsnest(scratch_board)
    by_net = {c["net"]: c for c in rats["connections"]}
    conn = None
    for net in _DETERMINISM_CANDIDATES:
        if net in by_net:
            conn = by_net[net]
            break
    if conn is None:
        pytest.skip("No candidate connection found (board changed?).")

    res1 = router.route_nets(scratch_board, connections=[conn], write=False)
    res2 = router.route_nets(scratch_board, connections=[conn], write=False)
    rec1, rec2 = res1["connections"][0], res2["connections"][0]
    assert rec1 == rec2
