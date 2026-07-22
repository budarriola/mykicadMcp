#!/usr/bin/env python3
"""Phase 7 autorouter core for the KiCad MCP server.

This is the new module the NETCLASS_PLAN "module layout" note calls for: the
autorouter does not get stuffed on top of the already ~3,200-line
`kicad_pcb_tool.py`. Phase 7 core lives here - starting with the connectivity
model and the ratsnest, growing into global routing (7.3a), detailed routing
(7.3b), the plane engine, optimizer, and sessions.

Everything parses through `kicad_pcb_tool`'s existing cached parsers - no
duplicated s-expr parsing:
  * `_parse_footprint_pads_cached`  - pad ref/number, absolute position, size,
    type (through-hole vs SMD), and per-pad copper layers (ground truth for
    what's connected where, immune to `.net` staleness).
  * `_parse_tracks_cached`          - segments / arcs / vias (`.Cu`-scoped).
  * `_parse_board_layers_cached`    - the copper stack in physical order (used
    to expand a via's layer span).
  * `load_board_local`              - per-board state incl. `net_overrides`
    (`{priority, layers}` per net) that biases ratsnest ordering.

Design of the connectivity model (`build_connectivity`) and the contact
tolerance is documented at those functions - false splits (declaring routed
copper unrouted) are the failure mode this stage guards against.
"""

from __future__ import annotations

import heapq
import json
import math
from pathlib import Path
from typing import Any

import kicad_pcb_tool as _pcb

# Connectivity-only zone-fill cache, keyed by board path (mtime,size) - mirrors
# the parse caches in kicad_pcb_tool. Retired when Phase 7.5's zone engine lands.
_zone_fill_cache: dict[str, tuple[float, int, dict[str, list[dict[str, Any]]]]] = {}


# --------------------------------------------------------------------------- #
# Geometry helpers (pure stdlib, 2-D; layer membership handled separately)
# --------------------------------------------------------------------------- #

def _dist_point_point(ax: float, ay: float, bx: float, by: float) -> float:
    return math.hypot(ax - bx, ay - by)


def _dist_point_segment(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
    """Shortest distance from point P to the finite segment A-B."""
    dx = bx - ax
    dy = by - ay
    seg_len2 = dx * dx + dy * dy
    if seg_len2 <= 1e-18:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / seg_len2
    if t < 0.0:
        t = 0.0
    elif t > 1.0:
        t = 1.0
    cx = ax + t * dx
    cy = ay + t * dy
    return math.hypot(px - cx, py - cy)


def _point_in_poly(px: float, py: float, poly: list[tuple[float, float]]) -> bool:
    """Even-odd ray-cast point-in-polygon test (KiCad stores a zone fill as a
    single outline ring, holes folded in via zero-width bridges, so even-odd is
    the right rule)."""
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if ((yi > py) != (yj > py)) and (px < (xj - xi) * (py - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


class _FillRaster:
    """Scanline rasterization of a zone fill polygon into a set of occupied
    cells, for O(1)-ish "is this point on the fill (within reach)?" tests.

    A zone fill on this board can carry thousands of vertices (a thermal-relief
    cutout around every pad), so a per-pair `_point_in_poly` over the raw ring
    is the connectivity model's hot spot. Rasterizing once (scanline, O(rows x
    edges)) and testing membership turns it into cheap set lookups. `cell` is
    the raster pitch; `covers` treats a point as on the fill when any occupied
    cell lies within `reach` of it, so a pad sitting in a thermal gap still
    reads as connected across the gap to the fill's spoke copper."""

    __slots__ = ("cell", "minx", "miny", "maxx", "maxy", "cells")

    def __init__(self, pts: list[tuple[float, float]], cell: float = 0.2) -> None:
        self.cell = cell
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        self.minx, self.miny = min(xs), min(ys)
        self.maxx, self.maxy = max(xs), max(ys)
        self.cells: set[tuple[int, int]] = set()
        n = len(pts)
        nrows = int((self.maxy - self.miny) / cell) + 1
        for row in range(nrows):
            y = self.miny + (row + 0.5) * cell
            crossings: list[float] = []
            for i in range(n):
                x1, y1 = pts[i]
                x2, y2 = pts[(i + 1) % n]
                if (y1 <= y < y2) or (y2 <= y < y1):
                    crossings.append(x1 + (y - y1) / (y2 - y1) * (x2 - x1))
            crossings.sort()
            for k in range(0, len(crossings) - 1, 2):
                c0 = int((crossings[k] - self.minx) / cell)
                c1 = int((crossings[k + 1] - self.minx) / cell)
                for col in range(c0, c1 + 1):
                    self.cells.add((col, row))

    def covers(self, px: float, py: float, reach: float) -> bool:
        r = reach + self.cell
        if px < self.minx - r or px > self.maxx + r or py < self.miny - r or py > self.maxy + r:
            return False
        c0 = int((px - r - self.minx) / self.cell)
        c1 = int((px + r - self.minx) / self.cell)
        r0 = int((py - r - self.miny) / self.cell)
        r1 = int((py + r - self.miny) / self.cell)
        cells = self.cells
        for row in range(r0, r1 + 1):
            for col in range(c0, c1 + 1):
                if (col, row) in cells:
                    cx = self.minx + (col + 0.5) * self.cell
                    cy = self.miny + (row + 0.5) * self.cell
                    if math.hypot(px - cx, py - cy) <= r:
                        return True
        return False


def _dist_point_poly(px: float, py: float, poly: list[tuple[float, float]]) -> float:
    """0.0 if the point is inside the polygon, else distance to its nearest
    edge. Lets a pad/via that sits in a zone's thermal-relief gap still register
    as connected: its copper reach bridges the small gap to the fill's spoke."""
    if _point_in_poly(px, py, poly):
        return 0.0
    best = math.inf
    n = len(poly)
    j = n - 1
    for i in range(n):
        d = _dist_point_segment(px, py, poly[i][0], poly[i][1], poly[j][0], poly[j][1])
        if d < best:
            best = d
        j = i
    return best


def _dist_segment_segment(
    a1x: float, a1y: float, a2x: float, a2y: float,
    b1x: float, b1y: float, b2x: float, b2y: float,
) -> float:
    """Shortest distance between two finite segments A1-A2 and B1-B2.

    If they intersect the distance is 0; otherwise it is the smallest of the
    four endpoint-to-opposite-segment distances (the classic non-intersecting
    case reduces to one endpoint being closest)."""
    # Intersection test (proper or touching) -> distance 0.
    d1x, d1y = a2x - a1x, a2y - a1y
    d2x, d2y = b2x - b1x, b2y - b1y
    denom = d1x * d2y - d1y * d2x
    if abs(denom) > 1e-12:
        t = ((b1x - a1x) * d2y - (b1y - a1y) * d2x) / denom
        u = ((b1x - a1x) * d1y - (b1y - a1y) * d1x) / denom
        if -1e-9 <= t <= 1.0 + 1e-9 and -1e-9 <= u <= 1.0 + 1e-9:
            return 0.0
    return min(
        _dist_point_segment(a1x, a1y, b1x, b1y, b2x, b2y),
        _dist_point_segment(a2x, a2y, b1x, b1y, b2x, b2y),
        _dist_point_segment(b1x, b1y, a1x, a1y, a2x, a2y),
        _dist_point_segment(b2x, b2y, a1x, a1y, a2x, a2y),
    )


# --------------------------------------------------------------------------- #
# Union-Find
# --------------------------------------------------------------------------- #

class _UnionFind:
    def __init__(self, n: int) -> None:
        self.parent = list(range(n))
        self.rank = [0] * n

    def find(self, i: int) -> int:
        root = i
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[i] != root:
            self.parent[i], i = root, self.parent[i]
        return root

    def union(self, i: int, j: int) -> None:
        ri, rj = self.find(i), self.find(j)
        if ri == rj:
            return
        if self.rank[ri] < self.rank[rj]:
            ri, rj = rj, ri
        self.parent[rj] = ri
        if self.rank[ri] == self.rank[rj]:
            self.rank[ri] += 1


# --------------------------------------------------------------------------- #
# Connectivity model
# --------------------------------------------------------------------------- #

# Absolute epsilon (mm) added to every contact reach so an exactly-terminated
# trace (endpoint at a pad/via center, distance ~0) and near-coincident segment
# endpoints always join despite float noise / minimal coordinate rounding.
_CONTACT_EPS_MM = 0.02


def _via_layer_set(via: dict[str, Any], stack_order: dict[str, int], all_cu: list[str]) -> frozenset[str]:
    """Copper layers a via electrically spans. A KiCad through via listed as
    ("F.Cu" "B.Cu") joins EVERY copper layer physically between them in the
    stack, not just the two named - so a segment on an inner layer is joined by
    a through via passing through it. Blind/buried vias name their real span.
    Falls back to the named `.Cu` layers if the stack order is unknown."""
    cu = [lyr for lyr in via.get("layers", []) if lyr.endswith(".Cu")]
    idxs = [stack_order[lyr] for lyr in cu if lyr in stack_order]
    if len(idxs) >= 2:
        lo, hi = min(idxs), max(idxs)
        return frozenset(lyr for lyr in all_cu if lo <= stack_order[lyr] <= hi)
    return frozenset(cu)


def _pad_layer_set(pad: dict[str, Any], all_cu: list[str]) -> frozenset[str]:
    """Copper layers a pad reaches. Through-hole pads (layers list contains
    the `*.Cu` wildcard, or pad type is a thru_hole variant) reach every copper
    layer; SMD pads reach only the specific `.Cu` layers named on them."""
    layers = pad.get("layers", [])
    pad_type = str(pad.get("type", ""))
    if any(lyr == "*.Cu" for lyr in layers) or "thru" in pad_type:
        return frozenset(all_cu)
    return frozenset(lyr for lyr in layers if lyr.endswith(".Cu"))


def _pad_reach(pad: dict[str, Any]) -> float:
    """Contact reach (mm) of a pad: half its LARGER dimension, so a trace that
    terminates anywhere within the pad's copper footprint (not merely dead on
    the anchor) still registers as touching. Erring generous here is deliberate
    - the failure mode this stage guards against is FALSE SPLITS (a routed net
    reported unrouted), which come from too-tight contact, not too-loose. Any
    resulting over-merge is confined to a single net and only ever JOINS copper
    KiCad also treats as one, so it cannot invent phantom ratsnest lines."""
    size = pad.get("size") or {}
    sx = float(size.get("x", 0.0) or 0.0)
    sy = float(size.get("y", 0.0) or 0.0)
    return max(sx, sy) / 2.0 if (sx or sy) else 0.1


class _Item:
    """One connectivity node: a pad, segment, arc, via, or zone-fill polygon,
    reduced to a 2-D shape (point, segment, or polygon), a copper-layer set, and
    a contact reach."""

    __slots__ = ("kind", "layers", "reach", "is_seg", "is_poly",
                 "x1", "y1", "x2", "y2", "poly", "bbox", "raster", "ref")

    def __init__(self, kind: str, layers: frozenset[str], reach: float,
                 x1: float, y1: float, x2: float, y2: float, ref: dict[str, Any],
                 poly: list[tuple[float, float]] | None = None,
                 raster: "_FillRaster | None" = None) -> None:
        self.kind = kind          # "pad" | "segment" | "arc" | "via" | "zone"
        self.layers = layers
        self.reach = reach
        self.is_seg = (kind in ("segment", "arc"))
        self.is_poly = (kind == "zone")
        self.x1, self.y1 = x1, y1
        self.x2, self.y2 = x2, y2  # == (x1,y1) for point items
        self.poly = poly or []
        self.raster = raster if raster is not None else (_FillRaster(poly) if poly else None)
        self.bbox = (
            (min(p[0] for p in poly), min(p[1] for p in poly),
             max(p[0] for p in poly), max(p[1] for p in poly))
            if poly else (x1, y1, x2, y2)
        )
        self.ref = ref            # original dict, for reporting representatives

    def points(self) -> list[tuple[float, float]]:
        if self.is_poly:
            # Airline nearest-pair only needs representative boundary points;
            # subsample big fills (some are 1000s of vertices) to keep it cheap.
            poly = self.poly
            if len(poly) > 64:
                step = len(poly) // 64
                return poly[::step]
            return poly
        if self.is_seg:
            return [(self.x1, self.y1), (self.x2, self.y2)]
        return [(self.x1, self.y1)]


def _dist_poly_item(poly_item: "_Item", other: "_Item") -> float:
    """Minimum distance between a zone-fill polygon and a point/segment item
    (0.0 when the item's geometry falls inside the fill). Cheap bbox reject
    first: if the other item is farther than its own reach from the fill's
    bounding box, they cannot touch."""
    minx, miny, maxx, maxy = poly_item.bbox
    margin = other.reach + _CONTACT_EPS_MM
    ox1, oy1, ox2, oy2 = other.x1, other.y1, other.x2, other.y2
    if (max(ox1, ox2) < minx - margin or min(ox1, ox2) > maxx + margin
            or max(oy1, oy2) < miny - margin or min(oy1, oy2) > maxy + margin):
        return math.inf
    raster = poly_item.raster
    assert raster is not None
    if other.is_poly:
        # zone vs zone (same net, same layer island): touch if any subsampled
        # vertex of one lands on the other's fill. Cross-layer fills never share
        # a layer and are excluded upstream by the shared-layer test.
        for vx, vy in other.points():
            if raster.covers(vx, vy, _CONTACT_EPS_MM):
                return 0.0
        return math.inf
    # point / segment: on the fill (within reach) at any endpoint -> touch.
    reach = other.reach + _CONTACT_EPS_MM
    for px, py in other.points():
        if raster.covers(px, py, reach):
            return 0.0
    return math.inf


def _min_distance(a: "_Item", b: "_Item") -> float:
    """Minimum 2-D distance between two items' geometries."""
    if a.is_poly:
        return _dist_poly_item(a, b)
    if b.is_poly:
        return _dist_poly_item(b, a)
    if a.is_seg and b.is_seg:
        return _dist_segment_segment(a.x1, a.y1, a.x2, a.y2, b.x1, b.y1, b.x2, b.y2)
    if a.is_seg and not b.is_seg:
        return _dist_point_segment(b.x1, b.y1, a.x1, a.y1, a.x2, a.y2)
    if b.is_seg and not a.is_seg:
        return _dist_point_segment(a.x1, a.y1, b.x1, b.y1, b.x2, b.y2)
    return _dist_point_point(a.x1, a.y1, b.x1, b.y1)


def _touches(a: "_Item", b: "_Item") -> bool:
    """Two items are electrically joined when they share at least one copper
    layer AND their geometries come within (reach_a + reach_b + eps).

    Reach is half the copper width of each item (pad half-extent, track/arc
    half-width, via radius; a zone fill has reach 0 - its polygon IS the copper
    edge, the margin comes from the other item), so the criterion is "their
    copper overlaps within tolerance on a common layer" - the physical
    definition of a connection. Segment<->segment shared endpoints, T-junctions
    (endpoint on a body), pad terminations, via drops, and a pad/via/trace
    landing on a same-net zone fill (incl. across a thermal-relief gap) all
    reduce to this one test."""
    if a.layers.isdisjoint(b.layers):
        return False
    return _min_distance(a, b) <= (a.reach + b.reach + _CONTACT_EPS_MM)


def _parse_zone_fills(board_path: Path) -> dict[str, list[dict[str, Any]]]:
    """Parse copper-pour zone FILLS (the `filled_polygon` blocks) per net, so
    the connectivity model can join pads/vias/traces that connect through a
    ground/power plane rather than through a trace - without which every plane
    net (GND_Main, 12V_Main, ...) false-splits into dozens of phantom islands.

    Returns `{net_name: [{layer, pts:[(x,y),...], uuid, name}]}` for every
    `filled_polygon` on a `.Cu` layer belonging to a zone that carries a net.
    Keepout zones (no net) and non-copper fills are skipped. This is a minimal,
    read-only, connectivity-only reader; the fuller Phase 7.5 zone engine
    (`_parse_zones`/`list_kicad_zones`, fill estimation, islands) supersedes it
    later - this function should be retired into that one when 7.5 lands.
    """
    text = _pcb._read_text(board_path)
    root = _pcb.SexprParser(text).parse()
    fills: dict[str, list[dict[str, Any]]] = {}

    def _pts(node: Any) -> list[tuple[float, float]]:
        pts: list[tuple[float, float]] = []
        for entry in node[1:]:
            if isinstance(entry, list) and entry and entry[0] == "xy" and len(entry) >= 3:
                try:
                    pts.append((float(entry[1]), float(entry[2])))
                except (TypeError, ValueError):
                    continue
        return pts

    def walk(node: Any) -> None:
        if isinstance(node, list):
            if node and node[0] == "zone":
                net_name = ""
                zone_uuid = ""
                zone_name = ""
                zone_fills: list[dict[str, Any]] = []
                for entry in node[1:]:
                    if not (isinstance(entry, list) and entry):
                        continue
                    tag = entry[0]
                    if tag == "net":
                        # (net "GND_Main") or (net 5 "GND_Main") - name is last.
                        if len(entry) >= 2 and isinstance(entry[-1], str):
                            net_name = entry[-1]
                    elif tag == "uuid" and len(entry) >= 2:
                        zone_uuid = str(entry[1])
                    elif tag == "name" and len(entry) >= 2:
                        zone_name = str(entry[1])
                    elif tag == "filled_polygon":
                        layer = ""
                        pts: list[tuple[float, float]] = []
                        for fentry in entry[1:]:
                            if not (isinstance(fentry, list) and fentry):
                                continue
                            if fentry[0] == "layer" and len(fentry) >= 2:
                                layer = str(fentry[1])
                            elif fentry[0] == "pts":
                                pts = _pts(fentry)
                        if layer.endswith(".Cu") and len(pts) >= 3:
                            zone_fills.append({"layer": layer, "pts": pts})
                if net_name and zone_fills:
                    for zf in zone_fills:
                        zf["uuid"] = zone_uuid
                        zf["name"] = zone_name
                    fills.setdefault(net_name, []).extend(zone_fills)
                return  # zones don't nest
            for child in node:
                walk(child)

    walk(root)
    return fills


def _parse_zone_fills_cached(board_path: Path) -> dict[str, list[dict[str, Any]]]:
    stat = board_path.stat()
    key = str(board_path)
    cached = _zone_fill_cache.get(key)
    if cached is not None and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
        return cached[2]
    fills = _parse_zone_fills(board_path)
    # Rasterize each fill once here (cached with the parse) - rasterization is
    # the connectivity model's most expensive step, so it must not repeat per
    # call or per net.
    for fill_list in fills.values():
        for zf in fill_list:
            zf["raster"] = _FillRaster(zf["pts"])
    _zone_fill_cache[key] = (stat.st_mtime, stat.st_size, fills)
    return fills


def _build_net_items(
    net: str,
    pads: list[dict[str, Any]],
    tracks: dict[str, list[dict[str, Any]]],
    zone_fills: list[dict[str, Any]],
    stack_order: dict[str, int],
    all_cu: list[str],
) -> list["_Item"]:
    items: list[_Item] = []
    for pad in pads:
        pos = pad["position"]
        items.append(_Item(
            "pad", _pad_layer_set(pad, all_cu), _pad_reach(pad),
            pos["x"], pos["y"], pos["x"], pos["y"], pad,
        ))
    for seg in tracks["segments"]:
        if seg["net"] != net:
            continue
        items.append(_Item(
            "segment", frozenset([seg["layer"]]), seg["width"] / 2.0,
            seg["start"]["x"], seg["start"]["y"], seg["end"]["x"], seg["end"]["y"], seg,
        ))
    for arc in tracks["arcs"]:
        if arc["net"] != net:
            continue
        items.append(_Item(
            "arc", frozenset([arc["layer"]]), arc["width"] / 2.0,
            arc["start"]["x"], arc["start"]["y"], arc["end"]["x"], arc["end"]["y"], arc,
        ))
    for via in tracks["vias"]:
        if via["net"] != net:
            continue
        at = via["at"]
        items.append(_Item(
            "via", _via_layer_set(via, stack_order, all_cu), via["size"] / 2.0,
            at["x"], at["y"], at["x"], at["y"], via,
        ))
    for zf in zone_fills:
        pts = zf["pts"]
        items.append(_Item(
            "zone", frozenset([zf["layer"]]), 0.0,
            pts[0][0], pts[0][1], pts[0][0], pts[0][1], zf, poly=pts,
            raster=zf.get("raster"),
        ))
    return items


def _item_id(item: "_Item") -> dict[str, Any]:
    """A compact, JSON-safe identity for an island representative item."""
    ref = item.ref
    if item.kind == "pad":
        return {
            "kind": "pad",
            "ref": ref.get("reference") or ref.get("ref") or "",
            "pad": ref.get("number", ""),
            "position": {"x": round(item.x1, 4), "y": round(item.y1, 4)},
            "layers": sorted(item.layers),
        }
    if item.kind == "zone":
        return {
            "kind": "zone",
            "uuid": ref.get("uuid", ""),
            "name": ref.get("name", ""),
            "layers": sorted(item.layers),
        }
    return {
        "kind": item.kind,
        "uuid": ref.get("uuid", ""),
        "layers": sorted(item.layers),
    }


def build_connectivity(project_path: str | Path) -> dict[str, Any]:
    """Union-find connectivity over pads + copper, per net, using board-file
    pad nets as ground truth (immune to `.net` staleness).

    For each net the nodes are its pads (with reachable copper layers;
    through-hole pads span every copper layer) plus its existing copper
    (segments / arcs / vias). Two nodes are unioned when `_touches` holds -
    they share a copper layer and their geometries come within the summed
    contact reach. A via unions the layers it spans at its position, so
    cross-layer copper joined only through a via still lands in one island.

    Returns, per net, its `islands` (each a list of member `_Item`s) plus small
    counters. Empty-net (`net ""`) copper is excluded - callers surface it via
    `free_copper`. Reused by `get_ratsnest` and, later, the router and its
    post-route verification.
    """
    board_path, _, _ = _pcb._resolve_project_path(project_path)
    footprints = _pcb._parse_footprint_pads_cached(board_path)
    tracks = _pcb._parse_tracks_cached(board_path)
    layers = _pcb._parse_board_layers_cached(board_path)
    zone_fills = _parse_zone_fills_cached(board_path)

    all_cu = [lyr["name"] for lyr in layers]
    if not all_cu:
        # Format tolerance: a board with no parseable (layers ...) copper stack
        # (e.g. an unusual export) still routes on whatever layers appear on the
        # copper itself; fall back to the layers named on segments/vias/pads.
        seen: list[str] = []
        for seg in tracks["segments"] + tracks["arcs"]:
            if seg["layer"] not in seen:
                seen.append(seg["layer"])
        all_cu = seen or ["F.Cu", "B.Cu"]
    stack_order = {name: i for i, name in enumerate(all_cu)}

    # Group pads by net (board pad nets = ground truth).
    pads_by_net: dict[str, list[dict[str, Any]]] = {}
    for fp in footprints.values():
        ref = fp.get("reference", "")
        for pad in fp["pads"]:
            net = pad.get("net", "")
            if not net:
                continue
            enriched = dict(pad)
            enriched["reference"] = ref
            pads_by_net.setdefault(net, []).append(enriched)

    # Every net that has any pad OR any copper (excluding empty-net copper).
    net_names: set[str] = set(pads_by_net)
    for group in ("segments", "arcs", "vias"):
        for rec in tracks[group]:
            if rec["net"]:
                net_names.add(rec["net"])
    net_names |= set(zone_fills)

    free_copper = {
        "segments": sum(1 for s in tracks["segments"] if not s["net"]),
        "arcs": sum(1 for a in tracks["arcs"] if not a["net"]),
        "vias": sum(1 for v in tracks["vias"] if not v["net"]),
    }

    nets_out: dict[str, dict[str, Any]] = {}
    for net in net_names:
        items = _build_net_items(net, pads_by_net.get(net, []), tracks,
                                 zone_fills.get(net, []), stack_order, all_cu)
        n = len(items)
        uf = _UnionFind(n)
        # O(n^2) pairwise contact within the net; net node counts are small.
        for i in range(n):
            ai = items[i]
            for j in range(i + 1, n):
                if _touches(ai, items[j]):
                    uf.union(i, j)
        groups: dict[int, list[_Item]] = {}
        for i in range(n):
            groups.setdefault(uf.find(i), []).append(items[i])
        islands = list(groups.values())
        nets_out[net] = {
            "islands": islands,
            "pad_count": sum(1 for it in items if it.kind == "pad"),
            "copper_count": sum(1 for it in items if it.kind != "pad"),
            "island_count": len(islands),
        }

    return {
        "board_path": str(board_path),
        "copper_layers": all_cu,
        "nets": nets_out,
        "free_copper": free_copper,
    }


# --------------------------------------------------------------------------- #
# Ratsnest (MST decomposition over islands)
# --------------------------------------------------------------------------- #

def _nearest_pair(island_a: list["_Item"], island_b: list["_Item"]) -> tuple[float, "_Item", "_Item", tuple[float, float], tuple[float, float]]:
    """Minimum airline (2-D) distance between two islands, plus the two items
    and the exact endpoints that realize it. Airlines are measured between the
    islands' terminal points: pad centers, via centers, and segment/arc
    endpoints - the copper the router would actually reach for."""
    best = math.inf
    best_a: _Item = island_a[0]
    best_b: _Item = island_b[0]
    best_pa: tuple[float, float] = (island_a[0].x1, island_a[0].y1)
    best_pb: tuple[float, float] = (island_b[0].x1, island_b[0].y1)
    pts_a = [(p, it) for it in island_a for p in it.points()]
    pts_b = [(p, it) for it in island_b for p in it.points()]
    for pa, ia in pts_a:
        for pb, ib in pts_b:
            d = _dist_point_point(pa[0], pa[1], pb[0], pb[1])
            if d < best:
                best, best_a, best_b, best_pa, best_pb = d, ia, ib, pa, pb
    return best, best_a, best_b, best_pa, best_pb


def _island_layers(island: list["_Item"]) -> list[str]:
    layers: set[str] = set()
    for it in island:
        layers |= it.layers
    return sorted(layers)


def _mst_connections(net: str, islands: list[list["_Item"]]) -> list[dict[str, Any]]:
    """Prim MST over islands (edge weight = nearest-pair airline). Yields
    exactly island_count - 1 connections and never a cycle - the missing
    ratsnest lines for the net."""
    n = len(islands)
    if n < 2:
        return []
    # Precompute the nearest-pair edge for every island pair once.
    edge: dict[tuple[int, int], tuple[float, Any, Any, Any, Any]] = {}
    for i in range(n):
        for j in range(i + 1, n):
            edge[(i, j)] = _nearest_pair(islands[i], islands[j])

    def get_edge(i: int, j: int):
        return edge[(i, j)] if i < j else edge[(j, i)]

    in_tree = {0}
    connections: list[dict[str, Any]] = []
    while len(in_tree) < n:
        best = None  # (weight, i, j)
        for i in in_tree:
            for j in range(n):
                if j in in_tree:
                    continue
                w = get_edge(i, j)[0]
                if best is None or w < best[0]:
                    best = (w, i, j)
        assert best is not None
        _, i, j = best
        dist, ia, ib, pa, pb = get_edge(i, j)
        # Representatives: report the from side on the tree, to side newly added.
        # pa/ia sit on island i, pb/ib on island j; keep from/to and their exact
        # realizing points aligned so the router (7.3a) has real endpoint coords,
        # not just the midpoint.
        if i < j:
            from_item, to_item, from_pt, to_pt = ia, ib, pa, pb
        else:
            from_item, to_item, from_pt, to_pt = ib, ia, pb, pa
        connections.append({
            "net": net,
            "airline_length_mm": round(dist, 4),
            "from": _item_id(from_item),
            "to": _item_id(to_item),
            "from_layers": _island_layers(islands[i]),
            "to_layers": _island_layers(islands[j]),
            "from_point": {"x": round(from_pt[0], 4), "y": round(from_pt[1], 4)},
            "to_point": {"x": round(to_pt[0], 4), "y": round(to_pt[1], 4)},
            "midpoint": {"x": round((pa[0] + pb[0]) / 2.0, 4), "y": round((pa[1] + pb[1]) / 2.0, 4)},
        })
        in_tree.add(j)
    return connections


def get_ratsnest(project_path: str | Path, nets: list[str] | None = None) -> dict[str, Any]:
    """List every unrouted connection (missing ratsnest line) on the board.

    Connectivity comes from `build_connectivity` (union-find over pads+copper,
    board pad nets as ground truth). For each net with >= 2 islands the missing
    connections are the MST decomposition over its islands (edge weight = the
    min pad/copper-to-pad/copper airline between islands), giving exactly one
    connection per still-separate island and no cycles.

    Ordering (for the future router - most-constrained first): connections are
    sorted by `net_overrides.priority` DESCENDING (higher priority routes
    first, "priority wins" per the plan), then by airline length ASCENDING
    (shortest = most constrained / least routing freedom, routed first). A net
    with no override has priority 0.

    Summary reports total connections, total airline mm, fully-routed net count
    (>= 2 pads, single island), and the unrouted-net list. Single-pad nets and
    free-copper (`net ""`) copper are handled explicitly: no connections, and
    counted separately.
    """
    conn = build_connectivity(project_path)
    nets_data = conn["nets"]

    # Per-net priority from board-local net_overrides (higher = route earlier).
    board_local = _pcb.load_board_local(project_path)
    overrides = board_local["data"].get("net_overrides", {}) or {}

    def priority_of(net: str) -> float:
        ov = overrides.get(net)
        if isinstance(ov, dict) and "priority" in ov:
            try:
                return float(ov["priority"])
            except (TypeError, ValueError):
                return 0.0
        return 0.0

    wanted = set(nets) if nets else None

    all_connections: list[dict[str, Any]] = []
    fully_routed: list[str] = []
    unrouted: list[str] = []
    single_pad: list[str] = []
    free_copper_nets: list[str] = []
    per_net: dict[str, dict[str, Any]] = {}

    for net, data in nets_data.items():
        if wanted is not None and net not in wanted:
            continue
        pad_count = data["pad_count"]
        islands = data["islands"]
        if pad_count == 0:
            # Copper on a real net but no pads reach it - a free/floating copper
            # net. No pads to ratsnest between; surfaced separately.
            free_copper_nets.append(net)
            per_net[net] = {"pad_count": 0, "island_count": data["island_count"],
                            "missing_connections": 0, "status": "free_copper"}
            continue
        if pad_count == 1:
            single_pad.append(net)
            per_net[net] = {"pad_count": 1, "island_count": data["island_count"],
                            "missing_connections": 0, "status": "single_pad"}
            continue

        connections = _mst_connections(net, islands)
        prio = priority_of(net)
        for c in connections:
            c["priority"] = prio
        all_connections.extend(connections)
        if connections:
            unrouted.append(net)
            status = "unrouted"
        else:
            fully_routed.append(net)
            status = "routed"
        per_net[net] = {
            "pad_count": pad_count,
            "island_count": data["island_count"],
            "missing_connections": len(connections),
            "airline_mm": round(sum(c["airline_length_mm"] for c in connections), 4),
            "status": status,
            "priority": prio,
        }

    # Most-constrained-first ordering: priority desc, then airline asc.
    all_connections.sort(key=lambda c: (-c["priority"], c["airline_length_mm"]))

    total_airline = round(sum(c["airline_length_mm"] for c in all_connections), 4)
    return {
        "board_path": conn["board_path"],
        "copper_layers": conn["copper_layers"],
        "summary": {
            "total_connections": len(all_connections),
            "total_airline_mm": total_airline,
            "fully_routed_net_count": len(fully_routed),
            "unrouted_net_count": len(unrouted),
            "single_pad_net_count": len(single_pad),
            "free_copper_net_count": len(free_copper_nets),
            "free_copper_items": conn["free_copper"],
        },
        "unrouted_nets": sorted(unrouted),
        "single_pad_nets": sorted(single_pad),
        "free_copper_nets": sorted(free_copper_nets),
        "connections": all_connections,
        "per_net": per_net,
    }


# =========================================================================== #
# Phase 7.3a - Global (coarse) routing + 7.3c layer directions / home layers
#
# The coarse stage makes the discrete, explainable choices (which layer, which
# corridor, roughly which path) that 7.3b later turns into exact geometry and
# that 7.7 escalates when they are near-ties. It is deliberately built on the
# same cached parsers and the same connectivity/ratsnest output as stage 1.
#
# INTEGER MILLI-COST (build-order step 11): every weight is quantized to
# integer milli-units (x1000) once at model build (`_Weights`), and ALL cost
# comparisons - A* priority, k-shortest, near-tie detection - run in integer
# milli-cost with a deterministic lexicographic tie-break. No floats enter a
# comparison, so two runs on identical inputs produce byte-identical JSON.
#
# COST TERMS (per grid move, all from `autorouter.cost` in pcb_settings.json):
#   step move (to neighbour cell on layer L, net kind K):
#       base   = step * dist_units            (dist_units: 1 straight, sqrt2 diag)
#       lp     = layer_purpose[K][type(L)]    (7.2: signal-on-power = 4x, etc.)
#       dirf   = off_direction  if the move runs straight against L's preferred
#                axis (7.3c); 1.0 for with-axis or 45-deg diagonal moves
#       congestion += congestion / max(remaining_capacity, small)   (or a large
#                finite penalty when a cell is full / a foreign plane cell -
#                never +inf: the weights decide, the router never hard-forbids)
#       away   += away_from_home_per_mm * dist_mm   when L != the net's home layer
#       off_corr += off_corridor * dist_mm   when a bus net leaves its Phase-5
#                corridor cells
#       turn   += direction_change           when the planar heading changes
#     move_milli = round( (base*lp*dirf + away + off_corr + turn)*1000 )
#                  + congestion_milli
#   via move (layer change at the same cell): via * via_weights.through, plus the
#     target cell's congestion; no dwell length so no away/off-direction term.
# =========================================================================== #

# Finite (never infinite) penalty for stepping into a full / foreign-plane cell,
# in milli-cost. Large enough to route around almost anything, small enough that
# a genuinely walled-in net still completes at high cost rather than failing -
# "the weights decide" (7.3c), the router never hard-forbids a move.
_FULL_CELL_MILLI = 5_000_000

# A* expansion safety cap per search (coarse grids are small; this only guards
# against a pathological blow-up, and a hit is reported as a routing failure).
_ASTAR_MAX_EXPANSIONS = 800_000

# 8-connected moves, in a fixed (deterministic) order. dir_index is the tuple's
# position; diagonal when both components are non-zero.
_MOVES: tuple[tuple[int, int], ...] = (
    (1, 0), (-1, 0), (0, 1), (0, -1),
    (1, 1), (1, -1), (-1, 1), (-1, -1),
)
_SQRT2 = math.sqrt(2.0)


class _Weights:
    """Cost weights from `autorouter.cost`, plus the milli-cost quantizer. Kept
    as one object so the quantization convention lives in exactly one place."""

    __slots__ = ("step", "via", "direction_change", "congestion", "off_corridor",
                 "off_direction", "away_from_home_per_mm", "through_via")

    def __init__(self, cost: dict[str, Any], through_via: float) -> None:
        self.step = float(cost.get("step", 1.0))
        self.via = float(cost.get("via", 25.0))
        self.direction_change = float(cost.get("direction_change", 2.0))
        self.congestion = float(cost.get("congestion", 8.0))
        self.off_corridor = float(cost.get("off_corridor", 4.0))
        self.off_direction = float(cost.get("off_direction", 2.0))
        self.away_from_home_per_mm = float(cost.get("away_from_home_per_mm", 0.5))
        self.through_via = float(through_via)

    @staticmethod
    def q(value: float) -> int:
        """Quantize a mm/unit cost to integer milli-cost (round-half-to-even is
        Python's default and is deterministic)."""
        return int(round(value * 1000.0))


# --------------------------------------------------------------------------- #
# Board bounding box (Edge.Cuts, else copper)
# --------------------------------------------------------------------------- #

def _edge_cuts_bbox(board_path: Path) -> tuple[float, float, float, float] | None:
    """Bounding box of every graphic on the `Edge.Cuts` layer (gr_line/gr_rect/
    gr_poly/gr_circle/gr_arc), by pooling all of their coordinate points. Board
    outline is the natural routing extent; None if the board has no Edge.Cuts."""
    text = _pcb._read_text(board_path)
    root = _pcb.SexprParser(text).parse()
    xs: list[float] = []
    ys: list[float] = []

    def _is_num(tok: Any) -> bool:
        return isinstance(tok, str) and _pcb._is_number(tok)

    def _collect_points(node: list[Any]) -> None:
        for entry in node[1:]:
            if not isinstance(entry, list) or not entry:
                continue
            tag = entry[0]
            if tag in ("start", "end", "center", "mid", "xy"):
                nums = [float(t) for t in entry[1:] if _is_num(t)]
                if len(nums) >= 2:
                    xs.append(nums[0])
                    ys.append(nums[1])
            elif tag == "pts":
                for sub in entry[1:]:
                    if isinstance(sub, list) and sub and sub[0] == "xy":
                        nums = [float(t) for t in sub[1:] if _is_num(t)]
                        if len(nums) >= 2:
                            xs.append(nums[0])
                            ys.append(nums[1])

    def _on_edge_cuts(node: list[Any]) -> bool:
        for entry in node[1:]:
            if isinstance(entry, list) and len(entry) >= 2 and entry[0] == "layer" and entry[1] == "Edge.Cuts":
                return True
        return False

    def walk(node: Any) -> None:
        if isinstance(node, list):
            tag0 = node[0] if node else None
            if isinstance(tag0, str) and tag0.startswith("gr_") and _on_edge_cuts(node):
                _collect_points(node)
            for child in node:
                walk(child)

    walk(root)
    if not xs or not ys:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def _copper_bbox(board_path: Path) -> tuple[float, float, float, float] | None:
    """Fallback board extent: bounding box over all copper (segment/arc/via
    endpoints and pad centres)."""
    tracks = _pcb._parse_tracks_cached(board_path)
    footprints = _pcb._parse_footprint_pads_cached(board_path)
    xs: list[float] = []
    ys: list[float] = []
    for seg in tracks["segments"] + tracks["arcs"]:
        xs += [seg["start"]["x"], seg["end"]["x"]]
        ys += [seg["start"]["y"], seg["end"]["y"]]
    for via in tracks["vias"]:
        xs.append(via["at"]["x"])
        ys.append(via["at"]["y"])
    for fp in footprints.values():
        for pad in fp["pads"]:
            xs.append(pad["position"]["x"])
            ys.append(pad["position"]["y"])
    if not xs or not ys:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def _board_bbox(board_path: Path) -> tuple[float, float, float, float]:
    bbox = _edge_cuts_bbox(board_path)
    if bbox is None:
        bbox = _copper_bbox(board_path)
    if bbox is None:
        return (0.0, 0.0, 1.0, 1.0)
    return bbox


# --------------------------------------------------------------------------- #
# 7.3c - Layer direction inference
# --------------------------------------------------------------------------- #

# Acute-angle deadzone (deg) around 45: a segment whose acute angle to the
# horizontal axis lands here is a diagonal and votes for neither H nor V.
_DIAG_LO = 30.0
_DIAG_HI = 60.0
# A layer with less than this much classified (non-diagonal) copper length is
# "too little copper" to infer from, and instead alternates against neighbours.
_MIN_INFER_LEN_MM = 10.0
# Fraction of classified length one axis must exceed to be declared dominant.
_DOMINANCE_FRAC = 0.60


def infer_layer_directions(project_path: str | Path, settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """Resolve each copper layer's preferred routing axis - "h", "v", or None
    (7.3c). Overridable wholesale by `autorouter.layer_directions` in
    pcb_settings; the string "auto" (default) means infer from the board:

      1. Length-weighted acute-angle histogram of the layer's existing segments
         -> "h" if horizontal length dominates (>= 60%), "v" if vertical does,
         else None. 45-deg diagonals (acute angle in 30..60) vote for neither.
      2. Power-type layers get NO preference (None) - planes don't route on an
         axis (source "power").
      3. Signal/mixed/jumper layers with too little copper (< 10 mm classified)
         to infer from ALTERNATE against their nearest already-resolved neighbour
         in stack order (H next to V next to H ...), which is what makes crossing
         conflicts globally solvable (source "alternation").

    Returns `{directions: {layer: "h"|"v"|None}, detail: {layer: {...}},
    source: "auto"|"override"}`; the resolved `directions` map is reported in
    every global-route result.
    """
    board_path, _, _ = _pcb._resolve_project_path(project_path)
    if settings is None:
        settings = _pcb.load_pcb_settings(project_path)["config"]
    layers = _pcb._parse_board_layers_cached(board_path)
    override = settings.get("autorouter", {}).get("layer_directions", "auto")

    # Explicit override map -> apply verbatim over every copper layer.
    if isinstance(override, dict):
        directions: dict[str, Any] = {}
        detail: dict[str, Any] = {}
        for lyr in layers:
            name = lyr["name"]
            val = override.get(name)
            val = val if val in ("h", "v", None) else None
            directions[name] = val
            detail[name] = {"direction": val, "source": "override", "type": lyr["type"]}
        return {"directions": directions, "detail": detail, "source": "override"}

    tracks = _pcb._parse_tracks_cached(board_path)
    # Length-weighted H/V accumulation per layer.
    hv: dict[str, list[float]] = {lyr["name"]: [0.0, 0.0] for lyr in layers}  # [h_len, v_len]
    for seg in tracks["segments"] + tracks["arcs"]:
        name = seg["layer"]
        if name not in hv:
            continue
        dx = seg["end"]["x"] - seg["start"]["x"]
        dy = seg["end"]["y"] - seg["start"]["y"]
        length = seg["length"]
        if length <= 1e-9:
            continue
        acute = math.degrees(math.atan2(abs(dy), abs(dx)))  # 0 = horizontal, 90 = vertical
        if acute < _DIAG_LO:
            hv[name][0] += length
        elif acute > _DIAG_HI:
            hv[name][1] += length

    directions = {}
    detail = {}
    needs_alt: list[str] = []
    for lyr in layers:
        name = lyr["name"]
        h_len, v_len = hv[name]
        total = h_len + v_len
        if lyr["type"] == "power":
            directions[name] = None
            detail[name] = {"direction": None, "source": "power", "type": lyr["type"],
                            "h_len_mm": round(h_len, 4), "v_len_mm": round(v_len, 4)}
            continue
        if total < _MIN_INFER_LEN_MM:
            directions[name] = None  # provisional; resolved by alternation below
            detail[name] = {"direction": None, "source": "alternation", "type": lyr["type"],
                            "h_len_mm": round(h_len, 4), "v_len_mm": round(v_len, 4)}
            needs_alt.append(name)
            continue
        if h_len >= _DOMINANCE_FRAC * total:
            resolved = "h"
        elif v_len >= _DOMINANCE_FRAC * total:
            resolved = "v"
        else:
            resolved = None
        directions[name] = resolved
        detail[name] = {"direction": resolved, "source": "inferred", "type": lyr["type"],
                        "h_len_mm": round(h_len, 4), "v_len_mm": round(v_len, 4)}

    # Alternation pass (stack order): each under-copper signal layer takes the
    # axis opposite its nearest already-resolved neighbour; seed "h" if none.
    order = [lyr["name"] for lyr in layers]
    opp = {"h": "v", "v": "h"}
    for name in needs_alt:
        idx = order.index(name)
        neighbour_dir = None
        for dist in range(1, len(order)):
            for j in (idx - dist, idx + dist):
                if 0 <= j < len(order):
                    d = directions.get(order[j])
                    if d in ("h", "v"):
                        neighbour_dir = d
                        break
            if neighbour_dir is not None:
                break
        directions[name] = opp[neighbour_dir] if neighbour_dir else "h"
        detail[name]["direction"] = directions[name]

    return {"directions": directions, "detail": detail, "source": "auto"}


# --------------------------------------------------------------------------- #
# Coarse capacity model
# --------------------------------------------------------------------------- #

class _CoarseModel:
    """Per-layer coarse capacity grid over the board bbox at `global_grid_mm`.

    A cell's capacity is how many more traces fit across it: floor((cell_width -
    existing copper crossing it) / (trace_width + clearance)), floored at 0.
    Existing copper is segments/arcs (width added once per crossed cell), vias
    and pads (their diameter at their cell), and zone fills (a filled plane cell
    is capacity 0 for FOREIGN nets - own-net plane cells stay routable). Only
    routable copper layers (signal/power/mixed/jumper types) get a grid; unknown
    /user-typed copper is excluded from routing entirely.
    """

    def __init__(self, board_path: Path, settings: dict[str, Any]) -> None:
        self.grid_mm = float(settings.get("autorouter", {}).get("global_grid_mm", 2.0)) or 2.0
        clearance = float(settings.get("autorouter", {}).get("clearance_fallback_mm", 0.2))
        default_nc = _pcb._default_netclass(board_path.parent / (board_path.stem + ".kicad_pro"))
        track_w = float(default_nc.get("track_width", 0.2)) if default_nc else 0.2
        self.pitch = (track_w + clearance) or 0.4

        self.minx, self.miny, self.maxx, self.maxy = _board_bbox(board_path)
        g = self.grid_mm
        self.cols = max(1, int(math.ceil((self.maxx - self.minx) / g)) + 1)
        self.rows = max(1, int(math.ceil((self.maxy - self.miny) / g)) + 1)
        self.base_slots = max(1, int(math.floor(g / self.pitch)))

        # Routable layers in stack order (exclude user/unknown-typed copper).
        allowed = settings.get("autorouter", {}).get("allowed_layers", []) or []
        all_layers = _pcb._parse_board_layers_cached(board_path)
        routable_types = {"signal", "power", "mixed", "jumper"}
        self.layer_types: dict[str, str] = {}
        self.layers: list[str] = []
        for lyr in all_layers:
            if lyr["type"] not in routable_types:
                continue
            if allowed and lyr["name"] not in allowed:
                continue
            self.layers.append(lyr["name"])
            self.layer_types[lyr["name"]] = lyr["type"]
        if not self.layers:  # degenerate board: route on whatever copper appears
            self.layers = [l["name"] for l in all_layers] or ["F.Cu", "B.Cu"]
            for name in self.layers:
                self.layer_types.setdefault(name, "signal")
        self.layer_index = {name: i for i, name in enumerate(self.layers)}

        # Occupancy (mm of copper across each cell) and plane cells (by net).
        self._occ: dict[tuple[str, int, int], float] = {}
        self._plane: dict[tuple[str, int, int], set[str]] = {}
        self._committed: dict[tuple[str, int, int], int] = {}
        self._build_occupancy(board_path)

    # -- cell <-> coordinate helpers ---------------------------------------- #
    def cell_of(self, x: float, y: float) -> tuple[int, int]:
        cx = int((x - self.minx) / self.grid_mm)
        cy = int((y - self.miny) / self.grid_mm)
        return (min(max(cx, 0), self.cols - 1), min(max(cy, 0), self.rows - 1))

    def cell_center(self, cx: int, cy: int) -> tuple[float, float]:
        return (self.minx + (cx + 0.5) * self.grid_mm, self.miny + (cy + 0.5) * self.grid_mm)

    def in_bounds(self, cx: int, cy: int) -> bool:
        return 0 <= cx < self.cols and 0 <= cy < self.rows

    # -- occupancy build ---------------------------------------------------- #
    def _add_seg_occ(self, layer: str, x1: float, y1: float, x2: float, y2: float, width: float) -> None:
        if layer not in self.layer_types:
            return
        length = math.hypot(x2 - x1, y2 - y1)
        nsamp = max(2, int(length / (self.grid_mm * 0.5)) + 1)
        touched: set[tuple[int, int]] = set()
        for i in range(nsamp + 1):
            t = i / nsamp
            touched.add(self.cell_of(x1 + t * (x2 - x1), y1 + t * (y2 - y1)))
        for (cx, cy) in touched:
            self._occ[(layer, cx, cy)] = self._occ.get((layer, cx, cy), 0.0) + width

    def _build_occupancy(self, board_path: Path) -> None:
        tracks = _pcb._parse_tracks_cached(board_path)
        for seg in tracks["segments"] + tracks["arcs"]:
            self._add_seg_occ(seg["layer"], seg["start"]["x"], seg["start"]["y"],
                              seg["end"]["x"], seg["end"]["y"], seg["width"])
        # vias: occupy every routable layer they span at their cell.
        stack = {name: i for i, name in enumerate(self.layers)}
        for via in tracks["vias"]:
            cx, cy = self.cell_of(via["at"]["x"], via["at"]["y"])
            cu = [l for l in via.get("layers", []) if l in self.layer_index]
            spanned = _via_layer_set(via, stack, self.layers) if len(cu) >= 2 else frozenset(cu)
            for layer in spanned:
                if layer in self.layer_types:
                    self._occ[(layer, cx, cy)] = self._occ.get((layer, cx, cy), 0.0) + via.get("size", 0.6)
        # pads: occupy every routable layer they reach at their cell.
        footprints = _pcb._parse_footprint_pads_cached(board_path)
        for fp in footprints.values():
            for pad in fp["pads"]:
                cx, cy = self.cell_of(pad["position"]["x"], pad["position"]["y"])
                reach = 2.0 * _pad_reach(pad)  # full larger dimension
                for layer in _pad_layer_set(pad, self.layers):
                    if layer in self.layer_types:
                        self._occ[(layer, cx, cy)] = self._occ.get((layer, cx, cy), 0.0) + reach
        # zone fills: mark coarse cells whose centre falls on the fill (per net).
        fills = _parse_zone_fills_cached(board_path)
        for net_name, fill_list in fills.items():
            for zf in fill_list:
                layer = zf["layer"]
                if layer not in self.layer_types:
                    continue
                raster = zf.get("raster")
                if raster is None:
                    continue
                cx0, cy0 = self.cell_of(raster.minx, raster.miny)
                cx1, cy1 = self.cell_of(raster.maxx, raster.maxy)
                for cy in range(cy0, cy1 + 1):
                    for cx in range(cx0, cx1 + 1):
                        px, py = self.cell_center(cx, cy)
                        if raster.covers(px, py, 0.0):
                            self._plane.setdefault((layer, cx, cy), set()).add(net_name)

    # -- capacity queries --------------------------------------------------- #
    def initial_capacity(self, net: str, layer: str, cx: int, cy: int) -> int:
        key = (layer, cx, cy)
        planes = self._plane.get(key)
        if planes is not None and net not in planes:
            return 0  # foreign plane cell - no room for this net's trace
        occ = self._occ.get(key, 0.0)
        free = self.grid_mm - min(occ, self.grid_mm)
        return max(0, int(math.floor(free / self.pitch)))

    def remaining(self, net: str, layer: str, cx: int, cy: int) -> int:
        return self.initial_capacity(net, layer, cx, cy) - self._committed.get((layer, cx, cy), 0)

    def commit(self, layer: str, cx: int, cy: int, width_factor: int) -> None:
        self._committed[(layer, cx, cy)] = self._committed.get((layer, cx, cy), 0) + width_factor


def _plane_opportunity_score(model: "_CoarseModel", net: str, layer: str, cx: int, cy: int) -> int:
    """HOOK for 7.5.4 plane-aware routing (NOT in scope for 7.3a): a net that
    owns a zone should be able to complete by dropping into fill and traversing
    plane cells cheaply. That belongs to the Phase 7.5 plane engine, which has
    the real fill/island/attachment model. Here it is a deliberate no-op so the
    call site exists and 7.5.4 has one clearly-named place to fill in. Always 0.
    """
    return 0


# --------------------------------------------------------------------------- #
# Coarse A* over (cell, layer)
# --------------------------------------------------------------------------- #

def _direction_factor(w: _Weights, layer_dir: Any, dx: int, dy: int) -> float:
    """off_direction multiplier for a planar move: a straight move against the
    layer's preferred axis costs `off_direction`; with-axis and 45-deg diagonal
    moves are neutral (1.0)."""
    if layer_dir not in ("h", "v") or (dx != 0 and dy != 0):
        return 1.0  # no preference, or a diagonal - neutral
    if layer_dir == "h":
        return w.off_direction if dx == 0 else 1.0  # vertical straight = against
    return w.off_direction if dy == 0 else 1.0       # layer_dir == "v"


def _astar(
    model: _CoarseModel,
    net: str,
    net_kind: str,
    weights: _Weights,
    layer_purpose: dict[str, Any],
    directions: dict[str, Any],
    start_cell: tuple[int, int],
    start_layers: list[str],
    goal_cell: tuple[int, int],
    goal_layers: set[str],
    home_layer: str | None,
    corridor_cells: set[tuple[int, int]] | None,
    blocked_cells: set[tuple[str, int, int]],
) -> list[tuple[int, int, str]] | None:
    """Integer-milli-cost A* over (cx, cy, layer) with an octile heuristic.

    State carries the incoming planar heading (dir_index; -1 at start/after a
    via) so the turn penalty is a proper edge cost. Returns the coarse path as
    an ordered list of (cx, cy, layer) cells, or None if unreachable within the
    expansion cap. Deterministic: the frontier is ordered lexicographically by
    (f_milli, g_milli, cx, cy, layer_index, dir_index).
    """
    g = model.grid_mm
    lp_kind = layer_purpose.get(net_kind, {})
    min_lp = min([float(lp_kind.get(model.layer_types[l], 1.0)) for l in model.layers] or [1.0])
    step_milli_per_unit = weights.q(weights.step * min_lp)
    gx, gy = goal_cell

    def heuristic(cx: int, cy: int) -> int:
        ax, ay = abs(cx - gx), abs(cy - gy)
        octile = (ax + ay) + (_SQRT2 - 2.0) * min(ax, ay)
        return int(math.floor(octile * step_milli_per_unit))

    li = model.layer_index

    def move_congestion_milli(layer: str, cx: int, cy: int) -> int:
        if (layer, cx, cy) in blocked_cells:
            return _FULL_CELL_MILLI
        rem = model.remaining(net, layer, cx, cy)
        if rem <= 0:
            return _FULL_CELL_MILLI
        return weights.q(weights.congestion / rem)

    start_states: list[tuple[int, int, str, int]] = []
    for layer in start_layers:
        start_states.append((start_cell[0], start_cell[1], layer, -1))

    # best_g keyed by full state (cell, layer, dir) since turn cost depends on dir.
    best_g: dict[tuple[int, int, str, int], int] = {}
    came: dict[tuple[int, int, str, int], tuple[int, int, str, int] | None] = {}
    heap: list[tuple[int, int, int, int, int, int]] = []
    for (sx, sy, layer, d) in start_states:
        st = (sx, sy, layer, d)
        best_g[st] = 0
        came[st] = None
        heapq.heappush(heap, (heuristic(sx, sy), 0, sx, sy, li[layer], d))

    def is_goal(cx: int, cy: int, layer: str) -> bool:
        return cx == gx and cy == gy and layer in goal_layers

    expansions = 0
    goal_state: tuple[int, int, str, int] | None = None
    while heap:
        f, gcost, cx, cy, layer_i, d = heapq.heappop(heap)
        layer = model.layers[layer_i]
        st = (cx, cy, layer, d)
        if gcost != best_g.get(st, None):
            continue  # stale heap entry
        if is_goal(cx, cy, layer):
            goal_state = st
            break
        expansions += 1
        if expansions > _ASTAR_MAX_EXPANSIONS:
            return None

        # planar moves
        for di, (dx, dy) in enumerate(_MOVES):
            ncx, ncy = cx + dx, cy + dy
            if not model.in_bounds(ncx, ncy):
                continue
            dist_units = _SQRT2 if (dx and dy) else 1.0
            dist_mm = dist_units * g
            base = weights.step * dist_units * float(lp_kind.get(model.layer_types[layer], 1.0))
            base *= _direction_factor(weights, directions.get(layer), dx, dy)
            extra = 0.0
            if home_layer is not None and layer != home_layer:
                extra += weights.away_from_home_per_mm * dist_mm
            if corridor_cells is not None and (ncx, ncy) not in corridor_cells:
                extra += weights.off_corridor * dist_mm
            if d != -1 and di != d:
                extra += weights.direction_change
            move_milli = weights.q(base + extra) + move_congestion_milli(layer, ncx, ncy)
            ng = gcost + move_milli
            nst = (ncx, ncy, layer, di)
            if (nst not in best_g) or ng < best_g[nst]:
                best_g[nst] = ng
                came[nst] = st
                heapq.heappush(heap, (ng + heuristic(ncx, ncy), ng, ncx, ncy, layer_i, di))

        # via moves (layer change at the same cell), heading preserved.
        for other in model.layers:
            if other == layer:
                continue
            move_milli = weights.q(weights.via * weights.through_via) + move_congestion_milli(other, cx, cy)
            ng = gcost + move_milli
            nst = (cx, cy, other, d)
            if (nst not in best_g) or ng < best_g[nst]:
                best_g[nst] = ng
                came[nst] = st
                heapq.heappush(heap, (ng + heuristic(cx, cy), ng, cx, cy, li[other], d))

    if goal_state is None:
        return None

    # reconstruct (cell, layer) path, collapsing repeated-cell via hops.
    rev: list[tuple[int, int, str]] = []
    cur: tuple[int, int, str, int] | None = goal_state
    while cur is not None:
        cx, cy, layer, _d = cur
        if not rev or rev[-1] != (cx, cy, layer):
            rev.append((cx, cy, layer))
        cur = came[cur]
    rev.reverse()
    return rev


# --------------------------------------------------------------------------- #
# Path scoring / summarisation
# --------------------------------------------------------------------------- #

def _path_cost_milli(
    model: _CoarseModel, net: str, net_kind: str, weights: _Weights,
    layer_purpose: dict[str, Any], directions: dict[str, Any],
    path: list[tuple[int, int, str]], home_layer: str | None,
    corridor_cells: set[tuple[int, int]] | None,
) -> int:
    """Re-score a coarse path in integer milli-cost with the same terms A* used,
    against the CURRENT remaining capacity (so a candidate reflects congestion
    debited by earlier-committed connections). Used to price k-alternates and
    reused bundle members consistently."""
    g = model.grid_mm
    lp_kind = layer_purpose.get(net_kind, {})
    total = 0
    prev_dir = -1
    for i in range(1, len(path)):
        pcx, pcy, player = path[i - 1]
        cx, cy, layer = path[i]
        if (cx, cy) == (pcx, pcy) and layer != player:
            total += weights.q(weights.via * weights.through_via)
            rem = model.remaining(net, layer, cx, cy)
            total += _FULL_CELL_MILLI if rem <= 0 else weights.q(weights.congestion / rem)
            continue
        dx = 1 if cx > pcx else (-1 if cx < pcx else 0)
        dy = 1 if cy > pcy else (-1 if cy < pcy else 0)
        di = _MOVES.index((dx, dy)) if (dx, dy) in _MOVES else -1
        dist_units = _SQRT2 if (dx and dy) else 1.0
        dist_mm = dist_units * g
        base = weights.step * dist_units * float(lp_kind.get(model.layer_types[layer], 1.0))
        base *= _direction_factor(weights, directions.get(layer), dx, dy)
        extra = 0.0
        if home_layer is not None and layer != home_layer:
            extra += weights.away_from_home_per_mm * dist_mm
        if corridor_cells is not None and (cx, cy) not in corridor_cells:
            extra += weights.off_corridor * dist_mm
        if prev_dir != -1 and di != -1 and di != prev_dir:
            extra += weights.direction_change
        total += weights.q(base + extra)
        rem = model.remaining(net, layer, cx, cy)
        total += _FULL_CELL_MILLI if rem <= 0 else weights.q(weights.congestion / rem)
        prev_dir = di if di != -1 else prev_dir
    return total


def _dominant_layer(path: list[tuple[int, int, str]], net_kind: str,
                    model: _CoarseModel, layer_purpose: dict[str, Any]) -> str:
    """Home layer = the layer the path spends the most cells on, biased toward
    the lower-cost (more purpose-appropriate) layer for the net kind, then the
    stack order - all deterministic tie-breaks."""
    length_on: dict[str, int] = {}
    for _cx, _cy, layer in path:
        length_on[layer] = length_on.get(layer, 0) + 1
    lp_kind = layer_purpose.get(net_kind, {})

    def key(layer: str) -> tuple[int, float, int]:
        return (-length_on[layer], float(lp_kind.get(model.layer_types[layer], 1.0)),
                model.layer_index.get(layer, 999))

    return min(length_on, key=key)


def _congestion_risk(model: _CoarseModel, net: str, path: list[tuple[int, int, str]]) -> float:
    """Fraction of the path's cells whose remaining capacity is <= 1 (a proxy
    for "how tight is this route" that 7.3b / 7.7 can rank on)."""
    if len(path) <= 1:
        return 0.0
    tight = sum(1 for (cx, cy, layer) in path if model.remaining(net, layer, cx, cy) <= 1)
    return round(tight / len(path), 4)


def _most_congested_interior(model: _CoarseModel, net: str, path: list[tuple[int, int, str]],
                             already: set[tuple[str, int, int]]) -> tuple[str, int, int] | None:
    """The interior path cell with the least remaining capacity (deterministic
    lexicographic tie-break), for k-shortest diversification - excludes the two
    endpoints and anything already blocked."""
    best: tuple[str, int, int] | None = None
    best_key: tuple[int, int, int, str] | None = None
    for (cx, cy, layer) in path[1:-1]:
        key3 = (layer, cx, cy)
        if key3 in already:
            continue
        rem = model.remaining(net, layer, cx, cy)
        k = (rem, cx, cy, layer)
        if best_key is None or k < best_key:
            best_key = k
            best = key3
    return best


def _cells_to_json(path: list[tuple[int, int, str]]) -> list[list[Any]]:
    return [[cx, cy, layer] for (cx, cy, layer) in path]


def _make_candidates(
    model: _CoarseModel, net: str, net_kind: str, weights: _Weights,
    layer_purpose: dict[str, Any], directions: dict[str, Any],
    start_cell: tuple[int, int], start_layers: list[str],
    goal_cell: tuple[int, int], goal_layers: set[str],
    corridor_cells: set[tuple[int, int]] | None,
) -> tuple[list[dict[str, Any]], str | None]:
    """1-3 ranked candidate coarse paths for one connection/bundle. First an
    un-homed A* picks the home layer (the layer its natural best path favours);
    then homed A* produces the best path and up to two k-alternates, each formed
    by blocking the previous path's most-congested interior cell. Returns
    (candidates, home_layer)."""
    prelim = _astar(model, net, net_kind, weights, layer_purpose, directions,
                    start_cell, start_layers, goal_cell, goal_layers,
                    home_layer=None, corridor_cells=corridor_cells, blocked_cells=set())
    if prelim is None:
        return [], None
    home_layer = _dominant_layer(prelim, net_kind, model, layer_purpose)

    candidates: list[dict[str, Any]] = []
    seen: set[tuple[tuple[int, int, str], ...]] = set()
    blocked: set[tuple[str, int, int]] = set()
    for _k in range(3):
        path = _astar(model, net, net_kind, weights, layer_purpose, directions,
                      start_cell, start_layers, goal_cell, goal_layers,
                      home_layer=home_layer, corridor_cells=corridor_cells,
                      blocked_cells=blocked)
        if path is None:
            break
        sig = tuple(path)
        if sig in seen:
            break
        seen.add(sig)
        est = _path_cost_milli(model, net, net_kind, weights, layer_purpose, directions,
                               path, home_layer, corridor_cells)
        layers_used = sorted({layer for _cx, _cy, layer in path},
                             key=lambda l: model.layer_index.get(l, 999))
        on_corridor = None
        if corridor_cells is not None:
            on = sum(1 for (cx, cy, _l) in path if (cx, cy) in corridor_cells)
            on_corridor = round(on / len(path), 4)
        candidates.append({
            "layers": layers_used,
            "coarse_path": _cells_to_json(path),
            "est_cost_milli": int(est),
            "congestion_risk": _congestion_risk(model, net, path),
            "home_layer": home_layer,
            "on_corridor": on_corridor,
        })
        nxt = _most_congested_interior(model, net, path, blocked)
        if nxt is None:
            break
        blocked.add(nxt)

    candidates.sort(key=lambda c: (c["est_cost_milli"], c["layers"], c["coarse_path"]))
    return candidates, home_layer


# --------------------------------------------------------------------------- #
# Bus bundle geometry (Phase 5 reuse)
# --------------------------------------------------------------------------- #

def _collect_bundles(project_path: str | Path) -> list[dict[str, Any]]:
    """Every confirmed/qualified bus bundle's routing geometry, from Phase 5's
    `_compute_bus_bundles` (via `detect_buses` for membership). One entry per
    (bus candidate, destination IC) with its member nets, hub/dest points, and
    distinct-trace count (the capacity width factor). Degrades cleanly to [] if
    detection fails (e.g. no netlist)."""
    bundles: list[dict[str, Any]] = []
    try:
        detected = _pcb.detect_buses(project_path)
    except Exception:
        return []
    for cand in detected.get("candidates", []):
        if not (cand.get("qualified") or cand.get("confirmed")):
            continue
        try:
            binfo = _pcb._compute_bus_bundles(project_path, bus=cand)
        except Exception:
            continue
        if not binfo.get("grouped"):
            continue
        for bundle in binfo["bundles"]:
            hub_pt = bundle.get("_hub_pt")
            dest_pt = bundle.get("_dest_pt")
            if not hub_pt or not dest_pt:
                continue
            member_nets = sorted(bundle.get("_net_segs", {}).keys())
            if not member_nets:
                continue
            bundles.append({
                "id": f"{binfo['bus_type']}:{binfo['hub_ic']}->{bundle['destination_ic']}",
                "bus_type": binfo["bus_type"],
                "hub_ic": binfo["hub_ic"],
                "destination_ic": bundle["destination_ic"],
                "member_nets": member_nets,
                "hub_pt": (float(hub_pt[0]), float(hub_pt[1])),
                "dest_pt": (float(dest_pt[0]), float(dest_pt[1])),
                "trace_count": max(1, int(bundle.get("trace_count", len(member_nets)))),
                "layers": list(bundle.get("layers", [])),
            })
    bundles.sort(key=lambda b: b["id"])
    return bundles


def _bundle_corridor_cells(model: _CoarseModel, hub_pt: tuple[float, float],
                           dest_pt: tuple[float, float]) -> set[tuple[int, int]]:
    """Coarse cells lying within one grid pitch of the hub->dest axis - the
    Phase-5 corridor a bus bundle is discounted to stay inside."""
    cells: set[tuple[int, int]] = set()
    length = math.hypot(dest_pt[0] - hub_pt[0], dest_pt[1] - hub_pt[1])
    nsamp = max(2, int(length / (model.grid_mm * 0.5)) + 1)
    for i in range(nsamp + 1):
        t = i / nsamp
        x = hub_pt[0] + t * (dest_pt[0] - hub_pt[0])
        y = hub_pt[1] + t * (dest_pt[1] - hub_pt[1])
        ccx, ccy = model.cell_of(x, y)
        for ax in (-1, 0, 1):
            for ay in (-1, 0, 1):
                if model.in_bounds(ccx + ax, ccy + ay):
                    cells.add((ccx + ax, ccy + ay))
    return cells


# --------------------------------------------------------------------------- #
# Global route (public internal API for 7.3b)
# --------------------------------------------------------------------------- #

def _conn_endpoints(conn: dict[str, Any]) -> tuple[tuple[float, float], tuple[float, float]]:
    def pt(side: str) -> tuple[float, float]:
        p = conn.get(f"{side}_point")
        if isinstance(p, dict) and "x" in p:
            return (float(p["x"]), float(p["y"]))
        item = conn.get(side, {})
        pos = item.get("position") if isinstance(item, dict) else None
        if isinstance(pos, dict) and "x" in pos:
            return (float(pos["x"]), float(pos["y"]))
        mid = conn.get("midpoint", {})
        return (float(mid.get("x", 0.0)), float(mid.get("y", 0.0)))
    return pt("from"), pt("to")


def global_route(
    project_path: str | Path,
    nets: list[str] | None = None,
    connections: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Phase 7.3a global (coarse) routing.

    For every unrouted connection (from `get_ratsnest`, filtered by `nets`, or a
    caller-supplied `connections` list) produce 1-3 ranked candidate coarse
    paths on the `global_grid_mm` capacity grid, each scored with the full
    integer-milli-cost model (layer-purpose multipliers, 7.3c off-direction and
    away-from-home terms, congestion vs. remaining capacity, and off-corridor for
    bus nets). Capacity is debited as connections commit, in the SAME canonical
    order as the ratsnest (priority desc, then airline asc), so later candidates
    see earlier congestion.

    Bus bundles (Phase 5 `_compute_bus_bundles` geometry via `detect_buses`) are
    routed AS ONE UNIT: the first member connection reached routes the shared
    hub->dest corridor, capacity is debited for the whole bundle width
    (trace_count x pitch), and every member connection reports that shared
    corridor's candidates and home layer.

    This is the 7.7 decision surface: a connection whose best two candidates are
    within `optimizer.ai_decisions.min_score_spread` (in milli) is flagged
    `near_tie: true` so 7.7 can later escalate it (no pausing here).

    Returns a JSON-friendly dict: the resolved layer-direction map, per-connection
    ranked candidates + chosen home layer + near_tie flag, the bundle groupings
    used, and a summary (total est cost, per-layer utilisation, counts).
    """
    board_path, _, _ = _pcb._resolve_project_path(project_path)
    settings = _pcb.load_pcb_settings(project_path)["config"]
    autor = settings.get("autorouter", {})
    weights = _Weights(autor.get("cost", {}),
                       float(settings.get("trace_cost", {}).get("via_weights", {}).get("through", 1.0)))
    layer_purpose = settings.get("layer_purpose", {})
    power_patterns = layer_purpose.get("power_net_patterns", [])

    dir_info = infer_layer_directions(project_path, settings=settings)
    directions = dir_info["directions"]

    model = _CoarseModel(board_path, settings)

    if connections is None:
        rats = get_ratsnest(project_path, nets=nets)
        conns = rats["connections"]
    else:
        conns = list(connections)
        if nets is not None:
            wanted = set(nets)
            conns = [c for c in conns if c["net"] in wanted]
    # Canonical commit order: priority desc, then airline asc (ratsnest order).
    conns = sorted(conns, key=lambda c: (-float(c.get("priority", 0.0)),
                                         float(c.get("airline_length_mm", 0.0)),
                                         c.get("net", "")))

    # Bundle membership: net -> bundle (first by sorted bundle id).
    bundles = _collect_bundles(project_path)
    net_to_bundle: dict[str, dict[str, Any]] = {}
    for b in bundles:
        for n in b["member_nets"]:
            net_to_bundle.setdefault(n, b)

    min_spread_milli = weights.q(float(settings.get("optimizer", {})
                                       .get("ai_decisions", {}).get("min_score_spread", 5.0)))

    routed_bundles: dict[str, dict[str, Any]] = {}  # id -> {candidates, home_layer}
    bundles_used: set[str] = set()
    out_conns: list[dict[str, Any]] = []
    near_tie_count = 0

    def routable_layers_for(layer_names: list[str]) -> list[str]:
        r = [l for l in layer_names if l in model.layer_index]
        return r if r else list(model.layers)

    for conn in conns:
        net = conn["net"]
        net_kind = _pcb._net_kind(net, None, power_patterns)
        from_xy, to_xy = _conn_endpoints(conn)
        bundle = net_to_bundle.get(net)

        if bundle is not None:
            bid = bundle["id"]
            if bid not in routed_bundles:
                # Route the bundle as a unit along its hub->dest corridor.
                corridor = _bundle_corridor_cells(model, bundle["hub_pt"], bundle["dest_pt"])
                s_cell = model.cell_of(*bundle["hub_pt"])
                g_cell = model.cell_of(*bundle["dest_pt"])
                cands, home = _make_candidates(
                    model, net, net_kind, weights, layer_purpose, directions,
                    s_cell, list(model.layers), g_cell, set(model.layers), corridor)
                # Debit the WHOLE bundle width along the chosen (best) path.
                if cands:
                    for (cx, cy, layer) in [tuple(c) for c in cands[0]["coarse_path"]]:
                        model.commit(layer, cx, cy, bundle["trace_count"])
                routed_bundles[bid] = {"candidates": cands, "home_layer": home,
                                       "corridor": corridor}
                bundles_used.add(bid)
            shared = routed_bundles[bid]
            candidates = [dict(c) for c in shared["candidates"]]
            home_layer = shared["home_layer"]
            on_corridor = candidates[0]["on_corridor"] if candidates else None
        else:
            s_cell = model.cell_of(*from_xy)
            g_cell = model.cell_of(*to_xy)
            candidates, home_layer = _make_candidates(
                model, net, net_kind, weights, layer_purpose, directions,
                s_cell, routable_layers_for(conn.get("from_layers", [])),
                g_cell, set(routable_layers_for(conn.get("to_layers", []))),
                corridor_cells=None)
            if candidates:
                for (cx, cy, layer) in [tuple(c) for c in candidates[0]["coarse_path"]]:
                    model.commit(layer, cx, cy, 1)
            on_corridor = None

        near_tie = False
        if len(candidates) >= 2:
            spread = candidates[1]["est_cost_milli"] - candidates[0]["est_cost_milli"]
            if spread < min_spread_milli:
                near_tie = True
        if near_tie:
            near_tie_count += 1

        out_conns.append({
            "net": net,
            "net_kind": net_kind,
            "priority": float(conn.get("priority", 0.0)),
            "airline_length_mm": conn.get("airline_length_mm"),
            "from_point": {"x": round(from_xy[0], 4), "y": round(from_xy[1], 4)},
            "to_point": {"x": round(to_xy[0], 4), "y": round(to_xy[1], 4)},
            "bundle_id": bundle["id"] if bundle is not None else None,
            "home_layer": home_layer,
            "on_corridor": on_corridor,
            "near_tie": near_tie,
            "routed": bool(candidates),
            "candidates": candidates,
        })

    # Per-layer utilisation from committed debits.
    util: dict[str, dict[str, Any]] = {}
    layer_debit: dict[str, int] = {l: 0 for l in model.layers}
    layer_cells: dict[str, set[tuple[int, int]]] = {l: set() for l in model.layers}
    for (layer, cx, cy), deb in model._committed.items():
        if layer in layer_debit:
            layer_debit[layer] += deb
            layer_cells[layer].add((cx, cy))
    for layer in model.layers:
        util[layer] = {
            "type": model.layer_types[layer],
            "preferred_direction": directions.get(layer),
            "cells_used": len(layer_cells[layer]),
            "total_trace_slots_debited": layer_debit[layer],
        }

    total_est = sum(c["candidates"][0]["est_cost_milli"] for c in out_conns if c["candidates"])
    routed_count = sum(1 for c in out_conns if c["routed"])

    return {
        "board_path": str(board_path),
        "global_grid_mm": model.grid_mm,
        "grid_dims": {"cols": model.cols, "rows": model.rows},
        "bbox": {"minx": round(model.minx, 4), "miny": round(model.miny, 4),
                 "maxx": round(model.maxx, 4), "maxy": round(model.maxy, 4)},
        "trace_pitch_mm": round(model.pitch, 4),
        "base_slots_per_cell": model.base_slots,
        "routable_layers": list(model.layers),
        "inferred_directions": directions,
        "layer_direction_detail": dir_info["detail"],
        "layer_direction_source": dir_info["source"],
        "connections": out_conns,
        "bundles_used": [b for b in bundles if b["id"] in bundles_used],
        "summary": {
            "total_connections": len(out_conns),
            "connections_routed": routed_count,
            "connections_failed": len(out_conns) - routed_count,
            "total_est_cost_milli": int(total_est),
            "near_tie_count": near_tie_count,
            "bundle_groupings_used": sorted(bundles_used),
            "per_layer_utilization": util,
            "inferred_directions": directions,
        },
    }


# =========================================================================== #
# Phase 7.11 - DRC constraints (rules + board settings) resolver
#
# Merges design-rule constraints from three sources in precedence order:
#   1. .kicad_dru rules (custom rule file) - highest priority
#   2. .kicad_pro net_settings.classes and board rules
#   3. pcb_settings autorouter.clearance_fallback_mm (fallback) - lowest
#
# Only evaluates offline-evaluable conditions (netclass, layer, net name).
# Unsupported conditions are reported, never silently ignored.
# =========================================================================== #

_drc_constraints_cache: dict[str, tuple[float, int, dict[str, Any]]] = {}


def _parse_dru_file(dru_path: Path) -> dict[str, Any]:
    """Parse a .kicad_dru (design rule) file and extract rule definitions.

    Returns {
        'rules': [
            {
                'name': str,
                'layer': str or None,
                'condition': str or None,
                'constraints': {constraint_type: {...}, ...}
            }
        ],
        'parse_error': str or None
    }
    """
    if not dru_path.exists():
        return {'rules': [], 'parse_error': None}

    try:
        text = _pcb._read_text(dru_path)
    except Exception as e:
        return {'rules': [], 'parse_error': str(e)}

    parser = _pcb.SexprParser(text)
    try:
        root = parser.parse()
    except Exception as e:
        return {'rules': [], 'parse_error': str(e)}

    rules: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, list) and node and node[0] == 'rule':
            rule_dict: dict[str, Any] = {}
            rule_name = ''
            rule_layer = None
            rule_condition = None
            constraints: dict[str, Any] = {}

            if len(node) > 1 and isinstance(node[1], str):
                rule_name = node[1]

            for entry in node[1:]:
                if not (isinstance(entry, list) and entry):
                    continue
                tag = entry[0]

                if tag == 'layer' and len(entry) >= 2:
                    rule_layer = str(entry[1])
                elif tag == 'condition' and len(entry) >= 2:
                    rule_condition = str(entry[1])
                elif tag == 'constraint' and len(entry) >= 2:
                    constraint_type = str(entry[1])
                    constraint_data: dict[str, float | None] = {}
                    for centry in entry[2:]:
                        if isinstance(centry, list) and len(centry) >= 2:
                            ctype = centry[0]
                            if ctype in ('min', 'max', 'opt') and len(centry) >= 2:
                                try:
                                    val = float(centry[1])
                                    constraint_data[ctype] = val
                                except (ValueError, TypeError):
                                    pass
                    if constraint_data:
                        constraints[constraint_type] = constraint_data

            if rule_name:
                rules.append({
                    'name': rule_name,
                    'layer': rule_layer,
                    'condition': rule_condition,
                    'constraints': constraints
                })

    walk(root)
    return {'rules': rules, 'parse_error': None}


def _parse_kicad_pro_constraints(project_file: Path) -> dict[str, Any]:
    """Extract constraints from .kicad_pro: net_settings.classes and board rules.

    Returns {
        'net_classes': {name: {clearance, track_width, via_diameter, via_drill, ...}},
        'board_rules': {min_clearance, min_track_width, min_via_diameter, ...},
        'parse_error': str or None
    }
    """
    if not project_file.exists():
        return {'net_classes': {}, 'board_rules': {}, 'parse_error': None}

    try:
        pro_data = json.loads(project_file.read_text(encoding='utf-8'))
    except Exception as e:
        return {'net_classes': {}, 'board_rules': {}, 'parse_error': str(e)}

    net_classes: dict[str, dict[str, Any]] = {}
    board_rules: dict[str, Any] = {}

    # Extract net_settings.classes
    classes = pro_data.get('board', {}).get('design_settings', {}).get('net_settings', {}).get('classes', [])
    if isinstance(classes, list):
        for nc in classes:
            if isinstance(nc, dict) and 'name' in nc:
                nc_name = nc['name']
                nc_dict = {}
                for key in ('clearance', 'track_width', 'via_diameter', 'via_drill',
                           'diff_pair_width', 'diff_pair_gap', 'microvia_diameter', 'microvia_drill'):
                    if key in nc:
                        try:
                            nc_dict[key] = float(nc[key])
                        except (ValueError, TypeError):
                            pass
                if nc_dict:
                    net_classes[nc_name] = nc_dict

    # Extract board rules
    rules = pro_data.get('board', {}).get('design_settings', {}).get('rules', {})
    if isinstance(rules, dict):
        for key in ('min_clearance', 'min_track_width', 'min_via_diameter', 'min_via_annular_width',
                   'min_hole_clearance', 'min_hole_to_hole', 'min_copper_edge_clearance'):
            if key in rules:
                try:
                    board_rules[key] = float(rules[key])
                except (ValueError, TypeError):
                    pass

    return {'net_classes': net_classes, 'board_rules': board_rules, 'parse_error': None}


def _dru_to_constraint_type(key: str) -> str | None:
    """Map .kicad_pro board rule keys to DRU constraint types."""
    mapping = {
        'min_clearance': 'clearance',
        'min_track_width': 'track_width',
        'min_via_diameter': 'via_diameter',
        'min_via_annular_width': 'annular_width',
        'min_hole_clearance': 'hole_clearance',
        'min_hole_to_hole': 'hole_to_hole',
        'min_copper_edge_clearance': 'edge_clearance',
    }
    return mapping.get(key)


def get_drc_constraints(project_path: str | Path) -> dict[str, Any]:
    """Resolve all DRC constraints for a KiCad project.

    Merges design-rule constraints from (in precedence order):
    1. .kicad_dru rules (custom rule file) - highest priority
    2. .kicad_pro net_settings.classes and design_settings.rules
    3. pcb_settings autorouter.clearance_fallback_mm (fallback) - lowest

    Only evaluates offline-evaluable conditions (netclass, layer, net name).
    Unsupported conditions are reported in `unsupported_rules`, never silently
    ignored. Cached by file mtime/size.

    API: get_drc_constraints(project_path: str | Path) -> dict

    Returns {
        'board_path': str - resolved PCB file path,
        'dru_file': str | None - path to .kicad_dru file used (or None),
        'constraints': dict - merged constraints with precedence tracing:
            {constraint_type_str: {
                'value': float | None,
                'sources': [{'type': str, 'key': str, ...}, ...]
            }},
        'net_classes': dict - net class definitions extracted from .kicad_pro,
        'board_rules': dict - board design rules from .kicad_pro,
        'unsupported_rules': list - rules with conditions we cannot evaluate:
            [{'name': str, 'condition': str, 'reason': str}, ...],
        'cache_info': dict - caching metadata (path, mtime, size)
    }
    """
    board_path, project_file, _ = _pcb._resolve_project_path(project_path)

    # Determine the .kicad_dru file path
    dru_path: Path | None = None

    # First try project-name.kicad_dru
    dru_candidates = sorted(board_path.parent.glob('*.kicad_dru*'))
    if dru_candidates:
        dru_path = dru_candidates[0]

    # Caching: use dru_path (or board_path if no dru) mtime/size
    cache_key = str(dru_path) if dru_path else str(board_path)

    try:
        stat = (dru_path if dru_path else board_path).stat()
    except OSError:
        stat = None

    if stat and cache_key in _drc_constraints_cache:
        cached = _drc_constraints_cache[cache_key]
        if cached[0] == stat.st_mtime and cached[1] == stat.st_size:
            return cached[2]

    # Parse .kicad_dru rules
    dru_data = _parse_dru_file(dru_path) if dru_path else {'rules': [], 'parse_error': None}
    dru_rules = dru_data.get('rules', [])

    # Parse .kicad_pro constraints
    pro_data = _parse_kicad_pro_constraints(project_file)
    net_classes = pro_data.get('net_classes', {})
    board_rules = pro_data.get('board_rules', {})

    # Get fallback clearance from pcb_settings
    settings = _pcb.load_pcb_settings(project_path)
    fallback_clearance = float(settings['config'].get('autorouter', {}).get('clearance_fallback_mm', 0.2))

    # Identify unsupported rules (those with non-evaluable conditions)
    unsupported: list[dict[str, Any]] = []
    for rule in dru_rules:
        condition = rule.get('condition')
        if condition:
            # Check if condition contains unsupported predicates
            # Offline-evaluable: A.Type, A.Net == 'name', A.Net != B.Net, A.isPlated, layer
            # Unsupported: B.Type, B.Net, object-pair predicates, specific pad properties
            unsupported_patterns = [
                'B.Type',      # pair predicates
                'B.Net',
                'B.Layer',
                'B.isPlated',
                'A.Pad_Type',  # specific pad property
            ]
            for pattern in unsupported_patterns:
                if pattern in condition:
                    unsupported.append({
                        'name': rule.get('name', ''),
                        'condition': condition,
                        'reason': f'Unsupported predicate: {pattern}'
                    })
                    break

    # Build the resolved constraints dict
    # Structure: {constraint_type: {value: float, sources: [...]}}
    resolved: dict[str, dict[str, Any]] = {}

    # Priority 3 (lowest): Add board rules
    for key, val in board_rules.items():
        constraint_type = _dru_to_constraint_type(key)
        if constraint_type:
            if constraint_type not in resolved:
                resolved[constraint_type] = {'value': val, 'sources': []}
            else:
                resolved[constraint_type]['value'] = val
            resolved[constraint_type]['sources'].append({
                'type': 'board_rule',
                'key': key
            })

    # Priority 2: Add net class constraints
    for nc_name, nc_dict in net_classes.items():
        for key, val in nc_dict.items():
            constraint_type = _dru_to_constraint_type(key) or key
            if constraint_type not in resolved:
                resolved[constraint_type] = {'value': val, 'sources': []}
            else:
                resolved[constraint_type]['value'] = val
            resolved[constraint_type]['sources'].append({
                'type': 'netclass',
                'netclass': nc_name,
                'key': key
            })

    # Priority 1 (highest): Add DRU rules
    unsupported_names = {u.get('name') for u in unsupported}
    for rule in dru_rules:
        if rule.get('name') in unsupported_names:
            continue  # Skip unsupported rules

        layer = rule.get('layer')
        for ctype, cdata in rule.get('constraints', {}).items():
            min_val = cdata.get('min')
            if min_val is not None:
                if ctype not in resolved:
                    resolved[ctype] = {'value': min_val, 'sources': []}
                else:
                    resolved[ctype]['value'] = min_val
                resolved[ctype]['sources'].append({
                    'type': 'dru_rule',
                    'rule_name': rule.get('name', ''),
                    'layer': layer,
                    'constraint_type': ctype
                })

    # Add fallback clearance only if not already set
    if 'clearance' not in resolved or resolved['clearance'].get('value') is None:
        resolved['clearance'] = {
            'value': fallback_clearance,
            'sources': [{'type': 'fallback', 'default': fallback_clearance}]
        }

    result = {
        'board_path': str(board_path),
        'dru_file': str(dru_path) if dru_path else None,
        'constraints': resolved,
        'net_classes': net_classes,
        'board_rules': board_rules,
        'unsupported_rules': unsupported,
        'cache_info': {
            'path': cache_key,
            'mtime': stat.st_mtime if stat else 0.0,
            'size': stat.st_size if stat else 0
        }
    }

    # Cache the result
    if stat:
        _drc_constraints_cache[cache_key] = (stat.st_mtime, stat.st_size, result)

    return result
