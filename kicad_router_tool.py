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
import re
import uuid as _uuid
from pathlib import Path
from typing import Any

import kicad_pcb_tool as _pcb

# Phase 7.5.1 zone model cache, keyed by board path (mtime,size) - mirrors the
# parse caches in kicad_pcb_tool (self-invalidating: a stat mismatch on lookup
# re-parses, so no explicit hook into `_pcb._invalidate_board_cache` is
# needed - same discipline the 7.3-stage-1 stopgap this supersedes used).
_zone_cache: dict[str, tuple[float, int, list[dict[str, Any]]]] = {}
# Derived per-net fill index (rasterized), built from `_parse_zones_cached`.
# Rasterization is the expensive step, so it is cached separately from the
# plain zone parse (`list_kicad_zones` never needs rasters).
_zone_fill_index_cache: dict[str, tuple[float, int, dict[str, list[dict[str, Any]]]]] = {}


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

    @classmethod
    def from_cells(cls, cells: set[tuple[int, int]], cell: float, minx: float, miny: float) -> "_FillRaster":
        """Build a raster directly from an already-computed occupied-cell set
        (7.5.2 estimation path: connected components found by grid flood-fill
        rather than by rasterizing a polygon ring)."""
        obj = cls.__new__(cls)
        obj.cell = cell
        obj.cells = set(cells)
        cols = [c for c, _ in obj.cells]
        rows = [r for _, r in obj.cells]
        obj.minx = minx
        obj.miny = miny
        obj.maxx = minx + (max(cols) + 1) * cell
        obj.maxy = miny + (max(rows) + 1) * cell
        return obj

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


def _parse_zones(board_path: Path) -> list[dict[str, Any]]:
    """Phase 7.5.1 zone model: every top-level `(zone ...)` block on the board
    (copper pours AND keepouts) - net, `layers` (always a LIST; KiCad 9
    multi-layer zones are native on this board, e.g. mainGnd spans F/B/In1.Cu),
    uuid, name, priority, hatch, connect_pads, min_thickness, fill settings
    (incl. `island_removal_mode` - every zone on this board allows islands, so
    downstream costing must not assume single-component fills), the outline
    `polygon`, and `filled_polygon` blocks WHEN PRESENT (never fabricated -
    that is 7.5.2's job, not this parser's).

    Only BOARD-level zones are returned - the footprint library on this board
    (RaspberryPi Pico) nests several per-pad keepout `(zone ...)` blocks inside
    `(footprint ...)`; those are pad-keepout regions, not planes, so `walk`
    does not descend into footprints. Unknown/future tokens are skipped
    (v9-vs-v10 tolerance).
    """
    text = _pcb._read_text(board_path)
    root = _pcb.SexprParser(text).parse()
    zones: list[dict[str, Any]] = []

    def _pts(node: Any) -> list[tuple[float, float]]:
        pts: list[tuple[float, float]] = []
        for entry in node[1:]:
            if isinstance(entry, list) and entry and entry[0] == "xy" and len(entry) >= 3:
                try:
                    pts.append((float(entry[1]), float(entry[2])))
                except (TypeError, ValueError):
                    continue
        return pts

    def _num(token: Any) -> float | None:
        try:
            return float(token)
        except (TypeError, ValueError):
            return None

    def walk(node: Any) -> None:
        if not isinstance(node, list) or not node:
            return
        tag0 = node[0]
        if tag0 == "footprint":
            return  # pad-keepout zones live here; not board-level planes.
        if tag0 == "zone":
            zone: dict[str, Any] = {
                "net": "",
                "layers": [],
                "uuid": "",
                "name": "",
                "priority": 0,
                "hatch": None,
                "connect_pads": None,
                "min_thickness": None,
                "fill": {},
                "island_removal_mode": None,
                "keepout": None,
                "polygon": [],
                "filled_polygon": [],
            }
            for entry in node[1:]:
                if not (isinstance(entry, list) and entry):
                    continue
                tag = entry[0]
                if tag == "net":
                    # (net "GND_Main") or (net 5 "GND_Main") - name is last.
                    for e in entry[1:]:
                        if isinstance(e, str):
                            zone["net"] = e
                elif tag == "layer" and len(entry) >= 2:
                    zone["layers"] = [str(entry[1])]
                elif tag == "layers":
                    zone["layers"] = [str(e) for e in entry[1:] if isinstance(e, str)]
                elif tag == "uuid" and len(entry) >= 2:
                    zone["uuid"] = str(entry[1])
                elif tag == "name" and len(entry) >= 2:
                    zone["name"] = str(entry[1])
                elif tag == "priority" and len(entry) >= 2:
                    n = _num(entry[1])
                    if n is not None:
                        zone["priority"] = int(n)
                elif tag == "hatch" and len(entry) >= 3:
                    zone["hatch"] = {"style": str(entry[1]), "pitch": _num(entry[2])}
                elif tag == "connect_pads":
                    mode = None
                    clearance = None
                    for e in entry[1:]:
                        if isinstance(e, str):
                            mode = e
                        elif isinstance(e, list) and e and e[0] == "clearance" and len(e) >= 2:
                            clearance = _num(e[1])
                    zone["connect_pads"] = {"mode": mode, "clearance": clearance}
                elif tag == "min_thickness" and len(entry) >= 2:
                    zone["min_thickness"] = _num(entry[1])
                elif tag == "keepout":
                    ko: dict[str, Any] = {}
                    for e in entry[1:]:
                        if isinstance(e, list) and len(e) >= 2 and isinstance(e[0], str):
                            ko[e[0]] = str(e[1])
                    zone["keepout"] = ko
                elif tag == "fill":
                    fill: dict[str, Any] = {}
                    # (fill yes ...) / (fill no ...) / (fill ...) - bare atoms
                    # right after the tag (before the first sub-list) are the
                    # enabled flag; keepout zones often omit it entirely.
                    for e in entry[1:]:
                        if isinstance(e, str):
                            fill["enabled"] = (e == "yes")
                        elif isinstance(e, list) and e and isinstance(e[0], str):
                            key = e[0]
                            n = _num(e[1]) if len(e) >= 2 else None
                            fill[key] = n if n is not None else (e[1] if len(e) >= 2 else True)
                    zone["fill"] = fill
                    if "island_removal_mode" in fill:
                        irm = fill["island_removal_mode"]
                        zone["island_removal_mode"] = int(irm) if irm is not None else None
                elif tag == "polygon":
                    for e in entry[1:]:
                        if isinstance(e, list) and e and e[0] == "pts":
                            zone["polygon"] = _pts(e)
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
                    if layer and len(pts) >= 3:
                        zone["filled_polygon"].append({"layer": layer, "pts": pts})
                # unknown tokens (island_area_filled, hatch min/max lengths,
                # attribute, etc.) are skipped gracefully - v9-vs-v10 tolerance.
            zones.append(zone)
            return  # zones don't nest
        for child in node:
            walk(child)

    walk(root)
    return zones


def _parse_zones_cached(board_path: Path) -> list[dict[str, Any]]:
    stat = board_path.stat()
    key = str(board_path)
    cached = _zone_cache.get(key)
    if cached is not None and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
        return cached[2]
    zones = _parse_zones(board_path)
    _zone_cache[key] = (stat.st_mtime, stat.st_size, zones)
    return zones


def _zone_fill_index_cached(board_path: Path) -> dict[str, list[dict[str, Any]]]:
    """Per-net copper-fill index derived from `_parse_zones_cached`, for the
    connectivity model / router occupancy grid / obstacle collection - the
    only three consumers that need per-net `.Cu`-layer fill geometry rather
    than the full zone record. Rasterized and cached once per board
    (mtime,size); a same-net multi-layer zone contributes one entry per
    `filled_polygon` block (one board layer's pour can also be split into
    several disjoint filled_polygon islands - each becomes its own entry).
    Keepout zones (no net) and non-copper fills are excluded.

    Returns `{net_name: [{layer, pts:[(x,y),...], uuid, name, raster}]}`.
    """
    stat = board_path.stat()
    key = str(board_path)
    cached = _zone_fill_index_cache.get(key)
    if cached is not None and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
        return cached[2]
    zones = _parse_zones_cached(board_path)
    fills: dict[str, list[dict[str, Any]]] = {}
    for zone in zones:
        net_name = zone.get("net", "")
        if not net_name:
            continue
        for fp in zone.get("filled_polygon", []):
            layer = fp.get("layer", "")
            pts = fp.get("pts", [])
            if not layer.endswith(".Cu") or len(pts) < 3:
                continue
            fills.setdefault(net_name, []).append({
                "layer": layer,
                "pts": pts,
                "uuid": zone.get("uuid", ""),
                "name": zone.get("name", ""),
                "raster": _FillRaster(pts),
            })
    _zone_fill_index_cache[key] = (stat.st_mtime, stat.st_size, fills)
    return fills


def list_zones(project_path: str | Path) -> dict[str, Any]:
    """Public wrapper over `_parse_zones_cached` for the MCP tool
    `list_kicad_zones`: every board-level zone (copper pour or keepout) with
    its net, `layers` list, uuid, name, priority, hatch, connect_pads,
    min_thickness, fill settings (incl. `island_removal_mode`), outline
    `polygon`, and `filled_polygon` blocks when present. Read-only; polygon
    point lists are returned as `{x, y}` dicts (JSON-safe) rather than tuples.
    """
    board_path, _, _ = _pcb._resolve_project_path(project_path)
    zones = _parse_zones_cached(board_path)

    def _xy(pts: list[tuple[float, float]]) -> list[dict[str, float]]:
        return [{"x": p[0], "y": p[1]} for p in pts]

    out_zones: list[dict[str, Any]] = []
    for z in zones:
        out_zones.append({
            "uuid": z["uuid"],
            "name": z["name"],
            "net": z["net"],
            "layers": list(z["layers"]),
            "priority": z["priority"],
            "hatch": z["hatch"],
            "connect_pads": z["connect_pads"],
            "min_thickness": z["min_thickness"],
            "fill": z["fill"],
            "island_removal_mode": z["island_removal_mode"],
            "keepout": z["keepout"],
            "polygon": _xy(z["polygon"]),
            "filled_polygon": [
                {"layer": fp["layer"], "pts": _xy(fp["pts"])} for fp in z["filled_polygon"]
            ],
        })
    return {
        "board_path": str(board_path),
        "zone_count": len(out_zones),
        "zones": out_zones,
    }


# =========================================================================== #
# Phase 7.5.2 (fill model) + 7.5.3 (islands & attachment-point costing)
#
# Fill model: KiCad's own `filled_polygon` blocks (from `_zone_fill_index_cached`,
# grouped per (zone uuid, layer) - each block is already one connected component,
# since KiCad itself splits a disjoint pour into separate `filled_polygon`
# entries) are authoritative and used verbatim when present -> `fill_source:
# "kicad"`. When a zone/layer has none (never filled in KiCad, or a synthetic
# board), the fill is ESTIMATED: the outline is rasterized at the router grid
# (`autorouter.grid_mm`), cells inside a higher-priority zone's outline are
# subtracted (priority wins the overlap), cells within a clearance-inflated
# reach of foreign-net copper are subtracted, and what remains is split into
# connected components by an 8-connected flood fill -> `fill_source:
# "estimated"`. Every component is then attached: same-net pads (reaching the
# component's fill within their contact reach - the same thermal-gap-bridging
# tolerance `_FillRaster.covers` already uses elsewhere) and same-net vias
# landing on the layer. The component with the most attachments is the
# mainland; every other component is an island, costed per `pcb_settings.json`
# `plane` knobs - except under `island_removal_mode == 1`, where KiCad deletes
# islands on refill, so they are reported `will_be_removed` and never costed.
#
# Known estimation-accuracy limits (documented, not hidden): the estimate uses
# higher-priority zones' OUTLINE polygons for subtraction rather than their own
# recursively-estimated fills (avoids unbounded recursion; a slight
# over-estimate of the lower-priority zone's area near a shared boundary), and
# approximates track segments as a chain of sampled circles rather than an
# exact stadium shape. Kiln itself never exercises this path - all six real
# zones carry `filled_polygon` data, so `fill_source` is "kicad" for every
# kiln zone/layer - the estimator only matters for a zone that has not yet
# been filled in KiCad (or synthetic test boards).
# =========================================================================== #

def _polygon_area_mm2(pts: list[tuple[float, float]]) -> float:
    """Shoelace polygon area (absolute value), mm^2."""
    n = len(pts)
    if n < 3:
        return 0.0
    area = 0.0
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        area += x1 * y2 - x2 * y1
    return abs(area) / 2.0


def _subsample_points(pts: list[tuple[float, float]], max_pts: int = 150) -> list[tuple[float, float]]:
    """Evenly-spaced subsample of a point list down to at most `max_pts` - a
    fill boundary/interior can carry thousands of points (thermal reliefs);
    nearest-point-pair search only needs a representative sample."""
    n = len(pts)
    if n <= max_pts or n == 0:
        return pts
    step = n / max_pts
    return [pts[int(i * step)] for i in range(max_pts)]


def _nearest_point_pair(
    pts_a: list[tuple[float, float]], pts_b: list[tuple[float, float]]
) -> tuple[float, tuple[float, float], tuple[float, float]]:
    """Minimum distance (mm) between any point of `pts_a` and any of `pts_b`,
    plus the realizing pair (point on A, point on B)."""
    best = math.inf
    best_a = pts_a[0]
    best_b = pts_b[0]
    for pa in pts_a:
        for pb in pts_b:
            d = math.hypot(pa[0] - pb[0], pa[1] - pb[1])
            if d < best:
                best, best_a, best_b = d, pa, pb
    return best, best_a, best_b


def _component_boundary_points(comp: dict[str, Any]) -> list[tuple[float, float]]:
    """Representative point sample for a fill component: its own outline
    points when KiCad-sourced (`pts`), else the centers of its occupied raster
    cells when estimated - either way, subsampled for cheap nearest-pair
    search."""
    pts = comp.get("pts")
    if pts:
        return _subsample_points(pts, 150)
    raster: _FillRaster = comp["raster"]
    cell_pts = [
        (raster.minx + (c + 0.5) * raster.cell, raster.miny + (r + 0.5) * raster.cell)
        for (c, r) in raster.cells
    ]
    return _subsample_points(cell_pts, 150)


def _group_pads_by_net(footprints: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
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
    return pads_by_net


def _foreign_copper_items(
    layer: str,
    net: str,
    footprints: dict[str, Any],
    tracks: dict[str, list[dict[str, Any]]],
    stack_order: dict[str, int],
    all_cu: list[str],
    clearance_mm: float,
) -> list[tuple[float, float, float]]:
    """Circles `(x, y, clearance-inflated radius)` approximating OTHER-net
    copper on `layer` - subtracted from a zone's estimated fill (7.5.2: "honor
    zone priority... subtract clearance-inflated foreign copper/holes"). Track
    segments/arcs are approximated as a chain of sampled circles rather than an
    exact stadium shape (an estimation-accuracy limit, not a correctness bug -
    see the module-level note above)."""
    items: list[tuple[float, float, float]] = []
    for fp in footprints.values():
        for pad in fp["pads"]:
            pad_net = pad.get("net", "")
            if not pad_net or pad_net == net:
                continue
            if layer not in _pad_layer_set(pad, all_cu):
                continue
            pos = pad["position"]
            items.append((pos["x"], pos["y"], _pad_reach(pad) + clearance_mm))
    for seg in tracks.get("segments", []) + tracks.get("arcs", []):
        seg_net = seg.get("net", "")
        if not seg_net or seg_net == net or seg.get("layer") != layer:
            continue
        x1, y1 = seg["start"]["x"], seg["start"]["y"]
        x2, y2 = seg["end"]["x"], seg["end"]["y"]
        length = math.hypot(x2 - x1, y2 - y1)
        n = max(1, int(length / 0.3))
        r = float(seg.get("width", 0.2)) / 2.0 + clearance_mm
        for i in range(n + 1):
            t = (i / n) if n else 0.0
            items.append((x1 + t * (x2 - x1), y1 + t * (y2 - y1), r))
    for via in tracks.get("vias", []):
        via_net = via.get("net", "")
        if not via_net or via_net == net:
            continue
        if layer not in _via_layer_set(via, stack_order, all_cu):
            continue
        at = via["at"]
        items.append((at["x"], at["y"], float(via.get("size", 0.6)) / 2.0 + clearance_mm))
    return items


def _estimate_layer_components(
    zone: dict[str, Any],
    layer: str,
    higher_priority_polys: list[list[tuple[float, float]]],
    foreign_items: list[tuple[float, float, float]],
    grid_mm: float,
) -> list[dict[str, Any]]:
    """7.5.2 estimation path for one zone/layer with no `filled_polygon`:
    rasterize the outline at `grid_mm`, subtract cells covered by a
    higher-priority zone's outline or lying within a foreign-copper item's
    inflated radius, then split what remains into connected components (8-
    connected flood fill). Each component becomes one `{pts: None, raster,
    area_mm2}` record, matching the shape of a KiCad-sourced component."""
    poly = zone.get("polygon") or []
    if len(poly) < 3:
        return []
    minx = min(p[0] for p in poly)
    maxx = max(p[0] for p in poly)
    miny = min(p[1] for p in poly)
    maxy = max(p[1] for p in poly)
    cols = max(1, int((maxx - minx) / grid_mm) + 1)
    rows = max(1, int((maxy - miny) / grid_mm) + 1)

    occupied: set[tuple[int, int]] = set()
    for row in range(rows):
        y = miny + (row + 0.5) * grid_mm
        for col in range(cols):
            x = minx + (col + 0.5) * grid_mm
            if not _point_in_poly(x, y, poly):
                continue
            if any(_point_in_poly(x, y, hp) for hp in higher_priority_polys):
                continue
            blocked = False
            for (fx, fy, fr) in foreign_items:
                if math.hypot(x - fx, y - fy) <= fr:
                    blocked = True
                    break
            if blocked:
                continue
            occupied.add((col, row))
    if not occupied:
        return []

    remaining = set(occupied)
    comps: list[dict[str, Any]] = []
    while remaining:
        seed = next(iter(remaining))
        remaining.discard(seed)
        comp_cells = {seed}
        stack = [seed]
        while stack:
            cx, cy = stack.pop()
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    if dx == 0 and dy == 0:
                        continue
                    nb = (cx + dx, cy + dy)
                    if nb in remaining:
                        remaining.discard(nb)
                        comp_cells.add(nb)
                        stack.append(nb)
        raster = _FillRaster.from_cells(comp_cells, grid_mm, minx, miny)
        comps.append({"pts": None, "raster": raster, "area_mm2": len(comp_cells) * grid_mm * grid_mm})
    return comps


def _component_attachments(
    comp: dict[str, Any],
    layer: str,
    net: str,
    pads_by_net: dict[str, list[dict[str, Any]]],
    tracks: dict[str, list[dict[str, Any]]],
    stack_order: dict[str, int],
    all_cu: list[str],
) -> list[dict[str, Any]]:
    """7.5.3 attachments for one fill component: same-net pads reaching it
    (thermal or solid `connect_pads` both bridge via the same contact-reach
    tolerance `_FillRaster.covers` already uses for thermal-relief gaps) plus
    same-net vias landing on `layer` inside it."""
    raster: _FillRaster = comp["raster"]
    attachments: list[dict[str, Any]] = []
    for pad in pads_by_net.get(net, []):
        if layer not in _pad_layer_set(pad, all_cu):
            continue
        pos = pad["position"]
        reach = _pad_reach(pad)
        if raster.covers(pos["x"], pos["y"], reach):
            attachments.append({
                "kind": "pad",
                "reference": pad.get("reference", ""),
                "pad": pad.get("number", ""),
                "position": {"x": round(pos["x"], 4), "y": round(pos["y"], 4)},
            })
    for via in tracks.get("vias", []):
        if via.get("net") != net:
            continue
        if layer not in _via_layer_set(via, stack_order, all_cu):
            continue
        at = via["at"]
        reach = float(via.get("size", 0.6)) / 2.0
        if raster.covers(at["x"], at["y"], reach):
            attachments.append({
                "kind": "via",
                "uuid": via.get("uuid", ""),
                "position": {"x": round(at["x"], 4), "y": round(at["y"], 4)},
            })
    return attachments


def _zone_island_model(board_path: Path, settings: dict[str, Any]) -> dict[str, Any]:
    """Build the full 7.5.2/7.5.3 fill + island + costing model for every
    board-level, net-owning zone (keepouts have no net, so no attachments to
    cost, and are excluded). Returns the structure `audit_plane_islands`
    reports directly."""
    zones = _parse_zones_cached(board_path)
    kicad_fills = _zone_fill_index_cached(board_path)
    footprints = _pcb._parse_footprint_pads_cached(board_path)
    tracks = _pcb._parse_tracks_cached(board_path)
    layers_info = _pcb._parse_board_layers_cached(board_path)
    all_cu = [lyr["name"] for lyr in layers_info] or ["F.Cu", "B.Cu"]
    stack_order = {name: i for i, name in enumerate(all_cu)}
    pads_by_net = _group_pads_by_net(footprints)

    plane_cfg = settings.get("plane", {}) or {}
    plane_step = float(plane_cfg.get("plane_step", 0.05))
    island_base = float(plane_cfg.get("island_base", 40.0))
    orphan_island = float(plane_cfg.get("orphan_island", 1000.0))
    warn_below = int(plane_cfg.get("island_min_attachments_warn", 2))
    autorouter_cfg = settings.get("autorouter", {}) or {}
    grid_mm = float(autorouter_cfg.get("grid_mm", 0.2)) or 0.2
    clearance_mm = float(autorouter_cfg.get("clearance_fallback_mm", 0.2))

    kicad_by_zone_layer: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for entries in kicad_fills.values():
        for e in entries:
            kicad_by_zone_layer.setdefault((e["uuid"], e["layer"]), []).append(e)

    zone_reports: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    total_islands = 0
    total_orphans = 0
    total_cost = 0.0

    for zone in zones:
        net = zone.get("net", "")
        if not net:
            continue  # keepouts / unnamed-net zones carry no attachments
        island_removal_mode = zone.get("island_removal_mode")
        will_remove = (island_removal_mode == 1)
        z_layers = [l for l in zone.get("layers", []) if l.endswith(".Cu")]
        layer_reports: list[dict[str, Any]] = []

        for layer in z_layers:
            entries = kicad_by_zone_layer.get((zone["uuid"], layer), [])
            if entries:
                fill_source = "kicad"
                comps = [
                    {"pts": e["pts"], "raster": e["raster"], "area_mm2": _polygon_area_mm2(e["pts"])}
                    for e in entries
                ]
            else:
                fill_source = "estimated"
                higher_polys = [
                    z2["polygon"] for z2 in zones
                    if z2 is not zone
                    and layer in z2.get("layers", [])
                    and z2.get("priority", 0) > zone.get("priority", 0)
                    and len(z2.get("polygon", []) or []) >= 3
                ]
                foreign_items = _foreign_copper_items(
                    layer, net, footprints, tracks, stack_order, all_cu, clearance_mm,
                )
                comps = _estimate_layer_components(zone, layer, higher_polys, foreign_items, grid_mm)

            if not comps:
                layer_reports.append({
                    "layer": layer, "fill_source": fill_source,
                    "component_count": 0, "components": [],
                })
                continue

            comp_records = []
            for comp in comps:
                attachments = _component_attachments(
                    comp, layer, net, pads_by_net, tracks, stack_order, all_cu,
                )
                comp_records.append({
                    "comp": comp,
                    "attachments": attachments,
                    "attachment_count": len(attachments),
                    "area_mm2": comp["area_mm2"],
                })
            # Mainland = most attachments; ties broken by larger area, then by
            # original (file/estimation) order for determinism.
            comp_records.sort(key=lambda r: (-r["attachment_count"], -r["area_mm2"]))
            mainland_rec = comp_records[0]

            out_comps: list[dict[str, Any]] = []
            for idx, rec in enumerate(comp_records):
                attach_list = rec["attachments"]
                area = round(rec["area_mm2"], 4)
                if idx == 0:
                    out_comps.append({
                        "role": "mainland",
                        "attachment_count": rec["attachment_count"],
                        "attachments": attach_list,
                        "area_mm2": area,
                        "cost": 0.0,
                        "warn": False,
                    })
                    continue
                if will_remove:
                    out_comps.append({
                        "role": "will_be_removed",
                        "attachment_count": rec["attachment_count"],
                        "attachments": attach_list,
                        "area_mm2": area,
                        "cost": None,
                        "warn": False,
                        "note": (
                            "island_removal_mode 1: KiCad deletes this island on "
                            "refill; not costed or stitched."
                        ),
                    })
                    continue
                n = rec["attachment_count"]
                if n == 0:
                    cost = orphan_island
                    role = "orphan"
                    total_orphans += 1
                else:
                    cost = island_base / n
                    role = "island"
                total_islands += 1
                total_cost += cost
                warn = n < warn_below
                if warn:
                    warnings.append({
                        "zone": zone["name"], "layer": layer,
                        "attachment_count": n, "role": role,
                    })
                suggestion = None
                isl_pts = _component_boundary_points(rec["comp"])
                main_pts = _component_boundary_points(mainland_rec["comp"])
                if isl_pts and main_pts:
                    dist, pa, pb = _nearest_point_pair(isl_pts, main_pts)
                    new_n = n + 1
                    suggestion = {
                        "position": {"x": round(pa[0], 4), "y": round(pa[1], 4)},
                        "nearest_mainland_point": {"x": round(pb[0], 4), "y": round(pb[1], 4)},
                        "distance_to_mainland_mm": round(dist, 4),
                        "projected_attachment_count": new_n,
                        "projected_cost": round(island_base / new_n, 4),
                    }
                out_comps.append({
                    "role": role,
                    "attachment_count": n,
                    "attachments": attach_list,
                    "area_mm2": area,
                    "cost": round(cost, 4),
                    "warn": warn,
                    "suggested_stitching_via": suggestion,
                })

            layer_reports.append({
                "layer": layer,
                "fill_source": fill_source,
                "component_count": len(comp_records),
                "components": out_comps,
            })

        zone_reports.append({
            "uuid": zone["uuid"],
            "name": zone["name"],
            "net": net,
            "priority": zone.get("priority", 0),
            "island_removal_mode": island_removal_mode,
            "layers": layer_reports,
        })

    return {
        "board_path": str(board_path),
        "plane_settings": {
            "plane_step": plane_step,
            "island_base": island_base,
            "orphan_island": orphan_island,
            "island_min_attachments_warn": warn_below,
        },
        "zones": zone_reports,
        "summary": {
            "island_count": total_islands,
            "orphan_island_count": total_orphans,
            "total_island_cost": round(total_cost, 4),
            "warnings": warnings,
        },
    }


def audit_plane_islands(project_path: str | Path) -> dict[str, Any]:
    """Public wrapper for the MCP tool `audit_kicad_plane_islands` (Phase
    7.5.2 fill model + 7.5.3 island/attachment costing). Read-only.

    Per net-owning zone/layer: `fill_source` ("kicad" when the zone carries
    real `filled_polygon` data, else "estimated"), component count, and per
    component: role (`mainland` | `island` | `orphan` | `will_be_removed`),
    attachment list (same-net pads/vias landing in it), area, current cost
    (`island_base / attachment_count`, or `orphan_island` at 0 attachments, or
    0.0 for the mainland), a warn flag below `island_min_attachments_warn`, and
    for costed islands the cheapest stitching-via position found (nearest
    point pair to the mainland component) with its projected new cost. Zones
    with `island_removal_mode 1` report islands as `will_be_removed` instead -
    they don't survive a KiCad refill, so they are never costed or offered a
    stitching suggestion (per the NETCLASS_PLAN edge-case note). Keepout /
    no-net zones carry no attachments and are excluded.
    """
    board_path, _, _ = _pcb._resolve_project_path(project_path)
    settings = _pcb.load_pcb_settings(project_path)["config"]
    return _zone_island_model(board_path, settings)


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
    zone_fills = _zone_fill_index_cached(board_path)

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
        fills = _zone_fill_index_cached(board_path)
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

# KiCad .kicad_dru constraint values are numbers with an optional unit
# suffix (mm, mil, in, um/µm); a bare number is already mm. All resolved
# constraint values in this module are in mm.
_DRU_UNIT_TO_MM = {
    'mm': 1.0,
    'mil': 0.0254,
    'in': 25.4,
    'um': 0.001,
    'µm': 0.001,
}
_DRU_NUMBER_RE = re.compile(r'^([+-]?\d*\.?\d+)\s*([a-zA-Zµ]*)$')


def _parse_dru_length_mm(token: Any) -> float | None:
    """Parse a DRU numeric length token (e.g. '0.15mm', '6.3mm', '1mm') to mm.

    Returns None if the token isn't a recognizable number (rather than
    raising), so callers can skip malformed constraint values instead of
    dropping the whole rule.
    """
    if not isinstance(token, str):
        return None
    match = _DRU_NUMBER_RE.match(token.strip())
    if not match:
        return None
    try:
        value = float(match.group(1))
    except ValueError:
        return None
    unit = match.group(2).lower()
    if not unit:
        return value
    factor = _DRU_UNIT_TO_MM.get(unit)
    if factor is None:
        return value  # unknown unit suffix; treat the number as already mm
    return value * factor


def _strip_dru_comments(text: str) -> str:
    """Strip '#'-to-end-of-line comments from a .kicad_dru file's text.

    KiCad's custom design-rule syntax uses '#' (not the ';' the generic
    SexprParser treats as a comment marker elsewhere) for line comments,
    including commenting out entire rules or individual clauses mid-rule
    (see JLCPCB.kicad_dru.txt). Done here rather than in the shared
    SexprParser, which other parsers still rely on '#' being an ordinary
    token character for (none currently do, but this keeps the change
    scoped to DRU parsing). Quote-aware so a literal '#' inside a string
    literal is left alone.
    """
    out_lines: list[str] = []
    for line in text.splitlines():
        in_string = False
        cut = len(line)
        i = 0
        while i < len(line):
            ch = line[i]
            if ch == '\\' and in_string:
                i += 2
                continue
            if ch == '"':
                in_string = not in_string
            elif ch == '#' and not in_string:
                cut = i
                break
            i += 1
        out_lines.append(line[:cut])
    return '\n'.join(out_lines)


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

    text = _strip_dru_comments(text)
    parser = _pcb.SexprParser(text)

    # Unlike `.kicad_pcb`/`.kicad_sch`, a `.kicad_dru` file is NOT a single
    # sexpr wrapping the whole file - it's a flat sequence of top-level forms:
    # `(version 1)` followed by one `(rule ...)` per rule. `SexprParser.parse()`
    # only consumes the first form, so walk `_parse_value` across the token
    # stream here to collect every top-level form.
    try:
        top_level_forms: list[Any] = []
        idx = 0
        num_tokens = len(parser.tokens)
        while idx < num_tokens:
            value, idx = parser._parse_value(idx)
            top_level_forms.append(value)
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
                                val = _parse_dru_length_mm(centry[1])
                                if val is not None:
                                    constraint_data[ctype] = val
                    if constraint_data:
                        constraints[constraint_type] = constraint_data

            if rule_name:
                rules.append({
                    'name': rule_name,
                    'layer': rule_layer,
                    'condition': rule_condition,
                    'constraints': constraints
                })

    for form in top_level_forms:
        walk(form)
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

    # The resolved result also depends on .kicad_pro (net classes / board
    # rules), so its mtime/size participates in invalidation too.
    try:
        pro_stat = project_file.stat() if project_file else None
    except OSError:
        pro_stat = None
    pro_key = (pro_stat.st_mtime, pro_stat.st_size) if pro_stat else None

    if stat and cache_key in _drc_constraints_cache:
        cached = _drc_constraints_cache[cache_key]
        if cached[0] == stat.st_mtime and cached[1] == stat.st_size and cached[2] == pro_key:
            return cached[3]

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
        _drc_constraints_cache[cache_key] = (stat.st_mtime, stat.st_size, pro_key, result)

    return result


# =========================================================================== #
# Phase 7.3b - Detailed (fine, windowed) routing
#
# Turns the 7.3a global corridor choice into exact copper. Per connection, in
# the SAME global-stage order (priority desc, airline asc):
#   1. Obstacle window   - rasterize only the connection bbox + margin at grid_mm
#   2. Pad escape        - exact off-grid stub from the endpoint to the nearest
#                          legal grid node
#   3. Fine A*           - integer-milli-cost (cx, cy, layer) search in the window,
#                          softly constrained to the global corridor
#   4. Rip-up & reroute  - STUBBED for this landing (see route_nets docstring):
#                          on failure the window doubles up to the whole board;
#                          a still-blocked net fails with its nearest blocker
#                          named. No PathFinder negotiated congestion / no
#                          ripping of already-placed copper yet.
#   5. Self-check + emit - a Python clearance pass proves every proposed
#                          segment/via against ALL copper at netclass clearance
#                          BEFORE any write; then simplified (segment)/(via)
#                          blocks are appended with create_group-style top-level
#                          surgery and their uuids recorded in board-local
#                          autorouter_owned.
#
# Clearance discipline (7.11 anchor's "Notes for 7.3b"): clearance is NEVER read
# from the single merged DRC value (0.0 on kiln - a bare board rule). It resolves
# from the Default net-class clearance, else the merged DRC value only when > 0,
# else autorouter.clearance_fallback_mm - obstacle inflation never trusts a 0.
# =========================================================================== #

# Nudge added to every A* obstacle-inflation radius: half a grid diagonal, so a
# foreign edge threading between two grid nodes is still marked blocked. This
# makes the fine A* over-block relative to the exact self-check (step 5) - the
# safe direction: any path A* finds clears the self-check, never the reverse.
_FINE_CELL_MARGIN_FRAC = 0.7072  # ~ 1/sqrt(2)

_FINE_ASTAR_MAX_EXPANSIONS = 1_500_000
_EMIT_EPS_MM = 1e-6

# Hard cap on a single connection's obstacle-window span (mm). The spec's
# "double up to the whole board" is infeasible at a 0.2 mm fine grid in pure
# Python (a whole-kiln window is ~2.3M nodes x 4 layers); windowing exists
# precisely to keep per-connection A* in the tens of thousands of cells. A
# connection that cannot route within this span fails fast (blocker named)
# rather than melting into a whole-board rasterization. Raise for larger boards
# once a spatial index / native backend lands (7.8 / GPU).
_MAX_WINDOW_SPAN_MM = 60.0
# Guard against a pathological window: if node*layer count exceeds this, the
# window is refused (reported as a failure) instead of built.
_MAX_WINDOW_NODES = 400_000


def _resolve_route_rules(project_path: str | Path, settings: dict[str, Any]) -> dict[str, Any]:
    """Resolve the width / clearance / via geometry the router emits and checks
    against, honoring the 7.11 anchor's rule that clearance must not come from a
    bare merged 0. Precedence: Default net-class (`.kicad_pro`) > merged DRC
    constraint (only when > 0) > `autorouter.clearance_fallback_mm`.

    kiln has a single Default net-class (clearance 0.2, track 0.2, via 0.6/0.3)
    and no per-net classes in `get_drc_constraints().net_classes`, so the values
    are board-uniform here; the resolver is written to prefer a matching
    net-class when one exists so multi-class boards resolve per-net later.
    """
    board_path, project_file, _ = _pcb._resolve_project_path(project_path)
    drc = get_drc_constraints(project_path)
    default_nc = _pcb._default_netclass(project_file) or {}
    autor = settings.get("autorouter", {})
    fallback = float(autor.get("clearance_fallback_mm", 0.2)) or 0.2

    clearance: float | None = None
    src = "fallback"
    nc_cl = float(default_nc.get("clearance", 0.0) or 0.0)
    if nc_cl > 0:
        clearance, src = nc_cl, "default_netclass"
    if clearance is None:
        merged = drc["constraints"].get("clearance", {}).get("value")
        try:
            merged_f = float(merged) if merged is not None else 0.0
        except (TypeError, ValueError):
            merged_f = 0.0
        if merged_f > 0:
            clearance, src = merged_f, "merged_drc"
    if clearance is None or clearance <= 0:
        clearance, src = fallback, "fallback"

    width = float(default_nc.get("track_width", 0.0) or 0.0) or 0.2
    via_d = float(default_nc.get("via_diameter", 0.0) or 0.0) or 0.6
    via_dr = float(default_nc.get("via_drill", 0.0) or 0.0) or 0.3
    edge_cl = float(drc["board_rules"].get("min_copper_edge_clearance", 0.0) or 0.0)
    if edge_cl <= 0:
        edge_cl = clearance
    return {
        "clearance": clearance,
        "clearance_source": src,
        "track_width": width,
        "via_diameter": via_d,
        "via_drill": via_dr,
        "edge_clearance": edge_cl,
    }


# --------------------------------------------------------------------------- #
# Obstacle collection (all copper + edges + keepouts), built once per board
# --------------------------------------------------------------------------- #

class _Obst:
    """A copper (or edge / keepout) obstacle reduced to geometry, the copper
    layers it occupies, a half-width, and its owning net. Same-net obstacles are
    skipped by the caller (same-net copper is free)."""

    __slots__ = ("kind", "net", "layers", "half", "x1", "y1", "x2", "y2",
                 "raster", "pts", "minx", "miny", "maxx", "maxy", "is_edge", "owner")

    def __init__(self, kind: str, net: str, layers: frozenset[str], half: float,
                 x1: float, y1: float, x2: float, y2: float,
                 raster: "_FillRaster | None" = None, is_edge: bool = False,
                 pts: list[tuple[float, float]] | None = None,
                 owner: int | None = None) -> None:
        self.kind = kind      # "seg" | "pt" | "zone" | "edge"
        self.net = net
        self.layers = layers
        self.half = half
        # owner: None for existing/human board copper (NEVER ripped); an integer
        # connection id for autorouter-placed copper (rippable in step 4).
        self.owner = owner
        self.x1, self.y1, self.x2, self.y2 = x1, y1, x2, y2
        self.raster = raster
        self.pts = pts or []   # zone fill polygon (for PRECISE edge distance)
        self.is_edge = is_edge
        if raster is not None:
            self.minx, self.miny, self.maxx, self.maxy = raster.minx, raster.miny, raster.maxx, raster.maxy
        else:
            self.minx, self.maxx = min(x1, x2), max(x1, x2)
            self.miny, self.maxy = min(y1, y2), max(y1, y2)

    def center_dist(self, px: float, py: float) -> float:
        """Distance from a point to this obstacle's copper centerline geometry
        (0 inside a zone fill). Reporting only - clearance decisions use the
        halo-aware `point_within` / `seg_within`, which account for a zone's
        fill EDGE, not just its interior."""
        if self.kind == "zone":
            assert self.raster is not None
            return 0.0 if self.raster.covers(px, py, 0.0) else math.inf
        if self.kind in ("seg", "edge"):
            return _dist_point_segment(px, py, self.x1, self.y1, self.x2, self.y2)
        return _dist_point_point(px, py, self.x1, self.y1)

    def _zone_within(self, px: float, py: float, need: float) -> bool:
        """A point is within `need` of the fill's copper edge. Fast raster reject
        first (the raster over-estimates by ~one cell, so a raster miss is a
        guaranteed true miss); only near the boundary is the exact polygon-edge
        distance computed - so the clearance the router enforces matches KiCad's
        own, not the coarse raster (which would false-positive on a legal skim)."""
        assert self.raster is not None
        if not self.raster.covers(px, py, need):
            return False  # conservatively-generous reject -> definitely clear
        return _dist_point_poly(px, py, self.pts) < need if self.pts else True

    def point_within(self, px: float, py: float, need: float) -> bool:
        """True when a point comes within `need` of this obstacle's COPPER EDGE.
        For a zone this accounts for the fill EDGE (its clearance halo), not just
        its interior - the fix for copper skimming a plane edge that an
        interior-only test misses (kicad-cli flags it, we must too)."""
        if self.kind == "zone":
            return self._zone_within(px, py, need)
        if self.kind in ("seg", "edge"):
            return _dist_point_segment(px, py, self.x1, self.y1, self.x2, self.y2) < need
        return _dist_point_point(px, py, self.x1, self.y1) < need

    def seg_within(self, ax: float, ay: float, bx: float, by: float, need: float) -> bool:
        """True when a finite segment A-B comes within `need` of this obstacle's
        copper edge (zone: any sampled point within `need` of the fill edge)."""
        if self.kind == "zone":
            length = math.hypot(bx - ax, by - ay)
            nsamp = max(2, int(length / 0.1) + 1)
            for i in range(nsamp + 1):
                t = i / nsamp
                if self._zone_within(ax + t * (bx - ax), ay + t * (by - ay), need):
                    return True
            return False
        if self.kind in ("seg", "edge"):
            return _dist_segment_segment(ax, ay, bx, by, self.x1, self.y1, self.x2, self.y2) < need
        return _dist_point_segment(self.x1, self.y1, ax, ay, bx, by) < need


def _edge_cut_segments(board_path: Path) -> list[tuple[float, float, float, float]]:
    """Edge.Cuts geometry as line segments: gr_line as one segment, gr_rect as
    its four sides. gr_poly points as consecutive segments. gr_arc/gr_circle are
    approximated by their bounding rectangle (documented coarse-ness - interior
    routes on this board are never edge-bound)."""
    text = _pcb._read_text(board_path)
    root = _pcb.SexprParser(text).parse()
    segs: list[tuple[float, float, float, float]] = []

    def _num(tok: Any) -> bool:
        return isinstance(tok, str) and _pcb._is_number(tok)

    def _on_edge(node: list[Any]) -> bool:
        for e in node[1:]:
            if isinstance(e, list) and len(e) >= 2 and e[0] == "layer" and e[1] == "Edge.Cuts":
                return True
        return False

    def _pt(node: list[Any], tag: str) -> tuple[float, float] | None:
        for e in node[1:]:
            if isinstance(e, list) and e and e[0] == tag:
                nums = [float(t) for t in e[1:] if _num(t)]
                if len(nums) >= 2:
                    return (nums[0], nums[1])
        return None

    def walk(node: Any) -> None:
        if isinstance(node, list):
            tag0 = node[0] if node else None
            if isinstance(tag0, str) and tag0.startswith("gr_") and _on_edge(node):
                if tag0 == "gr_line":
                    s, e = _pt(node, "start"), _pt(node, "end")
                    if s and e:
                        segs.append((s[0], s[1], e[0], e[1]))
                elif tag0 == "gr_rect":
                    s, e = _pt(node, "start"), _pt(node, "end")
                    if s and e:
                        x0, y0, x1, y1 = s[0], s[1], e[0], e[1]
                        segs.extend([(x0, y0, x1, y0), (x1, y0, x1, y1),
                                     (x1, y1, x0, y1), (x0, y1, x0, y0)])
                else:
                    # gr_poly / gr_arc / gr_circle: pool xy points, connect them.
                    pts: list[tuple[float, float]] = []
                    for e in node[1:]:
                        if isinstance(e, list) and e and e[0] == "pts":
                            for sub in e[1:]:
                                if isinstance(sub, list) and sub and sub[0] == "xy":
                                    nums = [float(t) for t in sub[1:] if _num(t)]
                                    if len(nums) >= 2:
                                        pts.append((nums[0], nums[1]))
                    for i in range(len(pts) - 1):
                        segs.append((pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1]))
            for child in node:
                walk(child)

    walk(root)
    return segs


def _collect_obstacles(board_path: Path, routable: set[str], all_cu: list[str],
                       edge_clearance: float) -> list["_Obst"]:
    """Every copper item on the board as an `_Obst` (segments/arcs, vias, pads,
    foreign zone fills) plus Edge.Cuts segments. Built once per board; the
    per-connection window filters this list by bbox."""
    tracks = _pcb._parse_tracks_cached(board_path)
    footprints = _pcb._parse_footprint_pads_cached(board_path)
    fills = _zone_fill_index_cached(board_path)
    stack = {name: i for i, name in enumerate(all_cu)}
    obs: list[_Obst] = []

    for seg in tracks["segments"] + tracks["arcs"]:
        if seg["layer"] not in routable:
            continue
        obs.append(_Obst("seg", seg["net"], frozenset([seg["layer"]]), seg["width"] / 2.0,
                         seg["start"]["x"], seg["start"]["y"], seg["end"]["x"], seg["end"]["y"]))
    for via in tracks["vias"]:
        at = via["at"]
        layers = _via_layer_set(via, stack, all_cu)
        layers = frozenset(l for l in layers if l in routable) or frozenset(
            l for l in via.get("layers", []) if l in routable)
        obs.append(_Obst("pt", via["net"], layers, via.get("size", 0.6) / 2.0,
                         at["x"], at["y"], at["x"], at["y"]))
    for fp in footprints.values():
        for pad in fp["pads"]:
            layers = frozenset(l for l in _pad_layer_set(pad, all_cu) if l in routable)
            if not layers:
                continue
            pos = pad["position"]
            obs.append(_Obst("pt", pad.get("net", ""), layers, _pad_reach(pad),
                             pos["x"], pos["y"], pos["x"], pos["y"]))
    for net_name, fill_list in fills.items():
        for zf in fill_list:
            if zf["layer"] not in routable:
                continue
            obs.append(_Obst("zone", net_name, frozenset([zf["layer"]]), 0.0,
                             zf["pts"][0][0], zf["pts"][0][1], zf["pts"][0][0], zf["pts"][0][1],
                             raster=zf.get("raster"), pts=zf["pts"]))
    for (x1, y1, x2, y2) in _edge_cut_segments(board_path):
        obs.append(_Obst("edge", "", frozenset(all_cu), 0.0, x1, y1, x2, y2, is_edge=True))
    return obs


def _clip_polygon_edges(pts: list[tuple[float, float]], bx0: float, by0: float,
                        bx1: float, by1: float) -> list[tuple[float, float, float, float]]:
    """Edges of a closed polygon whose segment bbox intersects the query box -
    lets a node near a board-spanning fill measure only the fill edges that pass
    through its window instead of the whole ring (the build hot-spot)."""
    edges: list[tuple[float, float, float, float]] = []
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        if max(x1, x2) < bx0 or min(x1, x2) > bx1 or max(y1, y2) < by0 or min(y1, y2) > by1:
            continue
        edges.append((x1, y1, x2, y2))
    return edges


def _min_dist_to_edges(px: float, py: float,
                       edges: list[tuple[float, float, float, float]]) -> float:
    best = math.inf
    for (x1, y1, x2, y2) in edges:
        d = _dist_point_segment(px, py, x1, y1, x2, y2)
        if d < best:
            best = d
    return best


# --------------------------------------------------------------------------- #
# Windowed obstacle raster + fine grid model
# --------------------------------------------------------------------------- #

class _FineWindow:
    """Fine (grid_mm) routing window over one connection's bbox + margin.

    Grid NODES sit at (minx + ix*grid, miny + iy*grid). `blocked_track[layer]`
    is the set of nodes a `track_half`-wide trace of net `net` cannot occupy;
    `blocked_via` is the set of nodes a via cannot occupy (foreign copper on any
    layer within the via's radius). Same-net obstacles are excluded (free)."""

    __slots__ = ("grid", "minx", "miny", "cols", "rows", "layers", "layer_types",
                 "blocked_track", "blocked_via", "net",
                 "_track_cnt", "_via_cnt", "_track_half", "_via_radius",
                 "_clearance", "_edge_clearance")

    def __init__(self, minx: float, miny: float, maxx: float, maxy: float, grid: float,
                 layers: list[str], layer_types: dict[str, str], net: str) -> None:
        self.grid = grid
        self.minx, self.miny = minx, miny
        self.cols = max(2, int(math.ceil((maxx - minx) / grid)) + 1)
        self.rows = max(2, int(math.ceil((maxy - miny) / grid)) + 1)
        self.layers = layers
        self.layer_types = layer_types
        self.net = net
        # `blocked_*` are the sets A* reads; `_*_cnt` are the per-cell reference
        # counts backing them, so an obstacle can be added OR removed
        # incrementally (a cell stays blocked while any obstacle still reaches
        # it). This is what makes step-4 rip-up clear ONLY the ripped copper's
        # cells without a full window rebuild.
        self.blocked_track: dict[str, set[tuple[int, int]]] = {l: set() for l in layers}
        self.blocked_via: set[tuple[int, int]] = set()
        self._track_cnt: dict[str, dict[tuple[int, int], int]] = {l: {} for l in layers}
        self._via_cnt: dict[tuple[int, int], int] = {}
        self._track_half = 0.0
        self._via_radius = 0.0
        self._clearance = 0.0
        self._edge_clearance = 0.0

    def node_xy(self, ix: int, iy: int) -> tuple[float, float]:
        return (self.minx + ix * self.grid, self.miny + iy * self.grid)

    def cell_of(self, x: float, y: float) -> tuple[int, int]:
        ix = int(round((x - self.minx) / self.grid))
        iy = int(round((y - self.miny) / self.grid))
        return (min(max(ix, 0), self.cols - 1), min(max(iy, 0), self.rows - 1))

    def in_bounds(self, ix: int, iy: int) -> bool:
        return 0 <= ix < self.cols and 0 <= iy < self.rows

    def obstacle_cells(self, ob: "_Obst") -> tuple[set[tuple[int, int]], dict[str, set[tuple[int, int]]]]:
        """The window cells this obstacle blocks: (via_cells, {layer: track_cells}).

        Pure geometry (uses the window's stored track/via/clearance params) so it
        is identical whether called during the bulk build, an incremental add, an
        incremental remove, or the rip-up on-path blocker test - a single source
        of truth for "which cells does this copper occupy"."""
        g = self.grid
        track_half = self._track_half
        via_radius = self._via_radius
        clearance = self._clearance
        edge_clearance = self._edge_clearance
        margin = g * _FINE_CELL_MARGIN_FRAC
        wminx = self.minx - g
        wminy = self.miny - g
        wmaxx = self.minx + (self.cols - 1) * g + g
        wmaxy = self.miny + (self.rows - 1) * g + g
        via_cells: set[tuple[int, int]] = set()
        track_cells: dict[str, set[tuple[int, int]]] = {}
        if ob.net == self.net and not ob.is_edge:
            return via_cells, track_cells  # same-net copper is free
        reach = max(track_half, via_radius) + max(clearance, edge_clearance) + ob.half + margin
        if (ob.maxx < wminx - reach or ob.minx > wmaxx + reach
                or ob.maxy < wminy - reach or ob.miny > wmaxy + reach):
            return via_cells, track_cells
        cl = edge_clearance if ob.is_edge else clearance
        track_reach = track_half + cl + ob.half + margin
        via_reach = via_radius + cl + ob.half + margin
        big = max(track_reach, via_reach)
        ix0 = max(0, int(math.floor((ob.minx - big - self.minx) / g)))
        ix1 = min(self.cols - 1, int(math.ceil((ob.maxx + big - self.minx) / g)))
        iy0 = max(0, int(math.floor((ob.miny - big - self.miny) / g)))
        iy1 = min(self.rows - 1, int(math.ceil((ob.maxy + big - self.miny) / g)))
        if ix0 > ix1 or iy0 > iy1:
            return via_cells, track_cells
        ob_layers = [l for l in ob.layers if l in self.blocked_track]
        for l in ob_layers:
            track_cells.setdefault(l, set())
        zedges: list[tuple[float, float, float, float]] | None = None
        if ob.kind == "zone" and ob.pts:
            zedges = _clip_polygon_edges(
                ob.pts, wminx - big, wminy - big, wmaxx + big, wmaxy + big)
        for iy in range(iy0, iy1 + 1):
            for ix in range(ix0, ix1 + 1):
                px, py = self.node_xy(ix, iy)
                if zedges is not None:
                    assert ob.raster is not None
                    inside = ob.raster.covers(px, py, 0.0)
                    dmin = 0.0 if inside else _min_dist_to_edges(px, py, zedges)
                    if dmin < via_reach:
                        via_cells.add((ix, iy))
                    if dmin < track_reach:
                        for l in ob_layers:
                            track_cells[l].add((ix, iy))
                    continue
                if ob.point_within(px, py, via_reach):
                    via_cells.add((ix, iy))
                if ob.point_within(px, py, track_reach):
                    for l in ob_layers:
                        track_cells[l].add((ix, iy))
        return via_cells, track_cells

    def add_obstacle(self, ob: "_Obst") -> None:
        via_cells, track_cells = self.obstacle_cells(ob)
        vc = self._via_cnt
        for cell in via_cells:
            n = vc.get(cell, 0)
            vc[cell] = n + 1
            if n == 0:
                self.blocked_via.add(cell)
        for layer, cells in track_cells.items():
            cnt = self._track_cnt[layer]
            blk = self.blocked_track[layer]
            for cell in cells:
                n = cnt.get(cell, 0)
                cnt[cell] = n + 1
                if n == 0:
                    blk.add(cell)

    def remove_obstacle(self, ob: "_Obst") -> None:
        """Incrementally clear an obstacle's cells (decrement ref counts; a cell
        leaves the blocked set only when no other obstacle still reaches it)."""
        via_cells, track_cells = self.obstacle_cells(ob)
        vc = self._via_cnt
        for cell in via_cells:
            n = vc.get(cell, 0)
            if n <= 1:
                vc.pop(cell, None)
                self.blocked_via.discard(cell)
            else:
                vc[cell] = n - 1
        for layer, cells in track_cells.items():
            cnt = self._track_cnt[layer]
            blk = self.blocked_track[layer]
            for cell in cells:
                n = cnt.get(cell, 0)
                if n <= 1:
                    cnt.pop(cell, None)
                    blk.discard(cell)
                else:
                    cnt[cell] = n - 1

    def build(self, obstacles: list["_Obst"], track_half: float, via_radius: float,
              clearance: float, edge_clearance: float) -> None:
        self._track_half = track_half
        self._via_radius = via_radius
        self._clearance = clearance
        self._edge_clearance = edge_clearance
        for ob in obstacles:
            self.add_obstacle(ob)

    def nearest_free(self, x: float, y: float, layers: list[str], max_ring: int = 6
                     ) -> tuple[int, int] | None:
        """Nearest grid node (spiral out) not track-blocked on at least one of
        `layers` - the pad-escape landing node."""
        cx, cy = self.cell_of(x, y)
        for ring in range(max_ring + 1):
            best: tuple[float, tuple[int, int]] | None = None
            for iy in range(cy - ring, cy + ring + 1):
                for ix in range(cx - ring, cx + ring + 1):
                    if max(abs(ix - cx), abs(iy - cy)) != ring:
                        continue
                    if not self.in_bounds(ix, iy):
                        continue
                    if any((ix, iy) not in self.blocked_track[l] for l in layers):
                        nx, ny = self.node_xy(ix, iy)
                        d = (nx - x) ** 2 + (ny - y) ** 2
                        if best is None or d < best[0]:
                            best = (d, (ix, iy))
            if best is not None:
                return best[1]
        return None


# --------------------------------------------------------------------------- #
# Fine A* over (cx, cy, layer)
# --------------------------------------------------------------------------- #

def _fine_astar(
    win: _FineWindow,
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
    congestion: dict[tuple[int, int, str], int] | None = None,
    plane_layers: dict[str, list[dict[str, Any]]] | None = None,
    goal_planes: dict[str, list[dict[str, Any]]] | None = None,
    plane_step: float = 0.0,
    attachment_via_cost: float = 0.0,
) -> list[tuple[int, int, str]] | None:
    """Integer-milli-cost A* over fine (cx, cy, layer) nodes with an octile
    heuristic, mirroring the 7.3a coarse A* cost model (step x layer-purpose x
    off-direction, turn = direction_change, via = via x through_via, away-from-
    home, soft off_corridor). Blocked nodes are impassable (a DRC obstacle, not a
    congestion penalty). Deterministic frontier order.

    `congestion` is the step-4 negotiated-congestion overlay: an integer-milli
    penalty added when a move ENTERS a contested (window-local) cell/layer. It is
    a soft cost (never impassable), so a net still routes through a congested
    cell if it must, but is nudged onto an alternate when one exists - which is
    what makes rip-up negotiation converge instead of thrash. Empty/None => the
    search is byte-identical to the pre-step-4 behaviour.

    `plane_layers`/`goal_planes`/`plane_step`/`attachment_via_cost` are the
    7.5.4 plane-aware-routing overlay, ONLY populated by the caller for a net
    that owns a zone (a zone whose `net` is this net - see `_route_core`).
    `plane_layers` is `{layer: [{"raster", "factor"}, ...]}` - this net's own
    fill components on each layer it covers, `factor` being 1.0 for the
    mainland (most-attached component) and `island_base/attachment_count` (or
    `orphan_island`) for an island, per the 7.5.3 costing model. A move whose
    destination node lies on one of these components costs `plane_step x
    factor` per mm INSTEAD of the normal step/layer-purpose/direction/
    away-from-home cost (off_corridor and turn cost still apply - soft, so they
    never block a plane shortcut, only nudge it); a layer-change (via) move
    landing on a plane component adds `attachment_via_cost` on top of the usual
    via cost (the cost to enter/leave the plane). `goal_planes` is
    `{layer: [{"raster", ...}, ...]}` - the SAME-layer components that already
    cover the connection's exact `to` point (within a `grid`-mm tolerance) at
    `_route_core` build time; reaching ANY node on one of THOSE specific
    components (mainland or island) completes the connection, because that
    copper is already electrically the goal's own island (7.3b's `to`-point-
    only termination relaxed for plane nets - see `is_goal` below). When both
    are None (every signal-net call, and any plane-net call whose goal does not
    already touch its own fill), every branch below that checks them is False
    and the search is byte-identical to the pre-7.5.4 behaviour (parity)."""
    cong = congestion or None
    plane = plane_layers or None
    plane_goal = goal_planes or None
    _plane_factor_cache: dict[tuple[int, int, str], float | None] = {}

    def _plane_factor(ix: int, iy: int, layer: str) -> float | None:
        """The (mainland=1.0 / island / orphan) cost factor for node (ix, iy)
        on `layer`, from THIS net's own plane fill - None when the net has no
        plane, `layer` carries none of its fill, or the node isn't on it."""
        if plane is None:
            return None
        key = (ix, iy, layer)
        if key in _plane_factor_cache:
            return _plane_factor_cache[key]
        val: float | None = None
        comps = plane.get(layer)
        if comps:
            nx, ny = win.node_xy(ix, iy)
            for c in comps:
                if c["raster"].covers(nx, ny, 0.0):
                    val = c["factor"]
                    break
        _plane_factor_cache[key] = val
        return val
    g = win.grid
    lp_kind = layer_purpose.get(net_kind, {})
    layers = win.layers
    li = {name: i for i, name in enumerate(layers)}
    min_lp = min([float(lp_kind.get(win.layer_types[l], 1.0)) for l in layers] or [1.0])
    step_milli_per_unit = weights.q(weights.step * min_lp)
    gx, gy = goal_cell

    def heuristic(cx: int, cy: int) -> int:
        ax, ay = abs(cx - gx), abs(cy - gy)
        octile = (ax + ay) + (_SQRT2 - 2.0) * min(ax, ay)
        return int(math.floor(octile * step_milli_per_unit))

    start_states = [(start_cell[0], start_cell[1], l, -1) for l in start_layers
                    if start_cell not in win.blocked_track.get(l, set())]
    if not start_states:
        # start node itself is blocked on every start layer; allow it anyway on
        # the first start layer (pad escape already picked it), so the search can
        # leave the pad. It will still be self-checked before emit.
        start_states = [(start_cell[0], start_cell[1], start_layers[0], -1)] if start_layers else []
    if not start_states:
        return None

    best_g: dict[tuple[int, int, str, int], int] = {}
    came: dict[tuple[int, int, str, int], tuple[int, int, str, int] | None] = {}
    heap: list[tuple[int, int, int, int, int, int]] = []
    for (sx, sy, l, d) in start_states:
        st = (sx, sy, l, d)
        best_g[st] = 0
        came[st] = None
        heapq.heappush(heap, (heuristic(sx, sy), 0, sx, sy, li[l], d))

    def is_goal(cx: int, cy: int, layer: str) -> bool:
        if cx == gx and cy == gy and layer in goal_layers:
            return True
        if plane_goal is not None:
            comps = plane_goal.get(layer)
            if comps:
                nx, ny = win.node_xy(cx, cy)
                for c in comps:
                    if c["raster"].covers(nx, ny, 0.0):
                        return True
        return False

    expansions = 0
    goal_state: tuple[int, int, str, int] | None = None
    while heap:
        f, gc, cx, cy, layer_i, d = heapq.heappop(heap)
        layer = layers[layer_i]
        st = (cx, cy, layer, d)
        if gc != best_g.get(st):
            continue
        if is_goal(cx, cy, layer):
            goal_state = st
            break
        expansions += 1
        if expansions > _FINE_ASTAR_MAX_EXPANSIONS:
            return None

        for di, (dx, dy) in enumerate(_MOVES):
            ncx, ncy = cx + dx, cy + dy
            if not win.in_bounds(ncx, ncy):
                continue
            if (ncx, ncy) in win.blocked_track[layer] and not (ncx == gx and ncy == gy):
                continue
            dist_units = _SQRT2 if (dx and dy) else 1.0
            dist_mm = dist_units * g
            extra = 0.0
            move_plane_factor = _plane_factor(ncx, ncy, layer)
            if move_plane_factor is not None:
                # Plane traversal (7.5.4): riding the net's own fill costs
                # plane_step x island-factor per mm INSTEAD of the normal
                # step/layer-purpose/direction/away-from-home cost - the plane
                # is nearly free copper, not a trace. off_corridor/turn still
                # apply (soft nudges, never block a shortcut through the fill).
                base = weights.step * dist_units * plane_step * move_plane_factor
            else:
                base = weights.step * dist_units * float(lp_kind.get(win.layer_types[layer], 1.0))
                base *= _direction_factor(weights, directions.get(layer), dx, dy)
                if home_layer is not None and layer != home_layer:
                    extra += weights.away_from_home_per_mm * dist_mm
            if corridor_cells is not None and (ncx, ncy) not in corridor_cells:
                extra += weights.off_corridor * dist_mm
            if d != -1 and di != d:
                extra += weights.direction_change
            move_milli = weights.q(base + extra)
            if cong is not None:
                move_milli += cong.get((ncx, ncy, layer), 0)
            ng = gc + move_milli
            nst = (ncx, ncy, layer, di)
            if nst not in best_g or ng < best_g[nst]:
                best_g[nst] = ng
                came[nst] = st
                heapq.heappush(heap, (ng + heuristic(ncx, ncy), ng, ncx, ncy, layer_i, di))

        # via moves - layer change at the same node; needs a clear via cell.
        if (cx, cy) not in win.blocked_via:
            for other in layers:
                if other == layer:
                    continue
                via_base = weights.via * weights.through_via
                if _plane_factor(cx, cy, other) is not None:
                    # Attachment via (7.5.4): entering/leaving this net's own
                    # plane fill through a via costs the usual via PLUS the
                    # flat attachment_via surcharge.
                    via_base += attachment_via_cost
                move_milli = weights.q(via_base)
                if cong is not None:
                    move_milli += cong.get((cx, cy, other), 0)
                ng = gc + move_milli
                nst = (cx, cy, other, d)
                if nst not in best_g or ng < best_g[nst]:
                    best_g[nst] = ng
                    came[nst] = st
                    heapq.heappush(heap, (ng + heuristic(cx, cy), ng, cx, cy, li[other], d))

    if goal_state is None:
        return None
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
# Path -> world polyline -> simplified (segment)/(via) emit
# --------------------------------------------------------------------------- #

def _collinear(ax: float, ay: float, bx: float, by: float, cx: float, cy: float) -> bool:
    cross = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
    return abs(cross) <= 1e-7


def _simplify_polyline(pts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Drop interior points collinear with their neighbours (collapses straight
    and 45-degree runs into single spans). Endpoints are always kept."""
    if len(pts) <= 2:
        return list(pts)
    out = [pts[0]]
    for i in range(1, len(pts) - 1):
        if not _collinear(out[-1][0], out[-1][1], pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1]):
            out.append(pts[i])
    out.append(pts[-1])
    return out


def _route_to_emit(
    win: _FineWindow, path: list[tuple[int, int, str]],
    from_xy: tuple[float, float], to_xy: tuple[float, float],
    plane_layers: dict[str, list[dict[str, Any]]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Turn a fine A* (cx, cy, layer) path plus its exact off-grid endpoints into
    emit-ready segment and via records. Pad-escape stubs join the exact endpoints
    to the first/last grid nodes; per-layer runs are simplified to collinear
    spans; each layer transition becomes a through via at the shared node.

    `plane_layers` (7.5.4, only set for a net that owns a zone) drops any
    segment whose BOTH endpoints already lie on this net's own fill (any
    component - mainland or island): plane traversal rides existing copper and
    emits nothing, only the via(s) that drop onto/off the plane are real new
    copper. A segment with only ONE endpoint on the fill is a genuine lead-in/
    lead-out stub and is kept."""
    # world points with layers: exact from-point, grid nodes, exact to-point.
    world: list[tuple[float, float, str]] = []
    first_layer = path[0][2]
    world.append((from_xy[0], from_xy[1], first_layer))
    for (ix, iy, layer) in path:
        nx, ny = win.node_xy(ix, iy)
        world.append((nx, ny, layer))
    last_layer = path[-1][2]
    world.append((to_xy[0], to_xy[1], last_layer))

    # split into per-layer runs, emitting a via at each layer change.
    segments: list[dict[str, Any]] = []
    vias: list[dict[str, Any]] = []
    run: list[tuple[float, float]] = [(world[0][0], world[0][1])]
    run_layer = world[0][2]
    for i in range(1, len(world)):
        x, y, layer = world[i]
        if layer != run_layer:
            # via at the previous node (== current node coords for a via hop).
            vx, vy, _ = world[i - 1]
            simp = _simplify_polyline(_dedup(run))
            for k in range(len(simp) - 1):
                segments.append({"x1": simp[k][0], "y1": simp[k][1],
                                 "x2": simp[k + 1][0], "y2": simp[k + 1][1], "layer": run_layer})
            vias.append({"x": vx, "y": vy})
            run = [(vx, vy)]
            run_layer = layer
        run.append((x, y))
    simp = _simplify_polyline(_dedup(run))
    for k in range(len(simp) - 1):
        segments.append({"x1": simp[k][0], "y1": simp[k][1],
                         "x2": simp[k + 1][0], "y2": simp[k + 1][1], "layer": run_layer})
    # drop zero-length segments (can appear at a via node).
    segments = [s for s in segments
                if math.hypot(s["x2"] - s["x1"], s["y2"] - s["y1"]) > _EMIT_EPS_MM]
    if plane_layers:
        def _on_own_plane(px: float, py: float, layer: str) -> bool:
            comps = plane_layers.get(layer)
            if not comps:
                return False
            return any(c["raster"].covers(px, py, 0.0) for c in comps)

        segments = [s for s in segments
                    if not (_on_own_plane(s["x1"], s["y1"], s["layer"])
                            and _on_own_plane(s["x2"], s["y2"], s["layer"]))]
    return segments, vias


def _dedup(pts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for p in pts:
        if not out or math.hypot(p[0] - out[-1][0], p[1] - out[-1][1]) > _EMIT_EPS_MM:
            out.append(p)
    return out


# --------------------------------------------------------------------------- #
# Self-check (Python clearance pass before any write)
# --------------------------------------------------------------------------- #

def _self_check(
    net: str, segments: list[dict[str, Any]], vias: list[dict[str, Any]],
    obstacles: list["_Obst"], rules: dict[str, Any], via_radius: float,
) -> list[dict[str, Any]]:
    """Prove every proposed segment/via against ALL foreign copper at netclass
    clearance (edge-to-edge >= clearance). Returns a list of violation records;
    empty means the route is DRC-safe to emit. Same-net obstacles are skipped
    (a route legitimately touches its own endpoints' copper)."""
    track_half = rules["track_width"] / 2.0
    clearance = rules["clearance"]
    edge_cl = rules["edge_clearance"]
    violations: list[dict[str, Any]] = []
    for ob in obstacles:
        if ob.net == net and not ob.is_edge:
            continue
        cl = edge_cl if ob.is_edge else clearance
        ob_layers = ob.layers
        for s in segments:
            if s["layer"] not in ob_layers:
                continue
            need = track_half + cl + ob.half - 1e-6
            if ob.seg_within(s["x1"], s["y1"], s["x2"], s["y2"], need):
                violations.append({"kind": "segment", "layer": s["layer"],
                                   "against_net": ob.net, "against_kind": ob.kind,
                                   "required_mm": round(need, 4)})
        for v in vias:
            # a through via touches every routable layer; check against every
            # foreign obstacle regardless of the obstacle's own layer set.
            need = via_radius + cl + ob.half - 1e-6
            if ob.point_within(v["x"], v["y"], need):
                violations.append({"kind": "via", "against_net": ob.net,
                                   "against_kind": ob.kind, "required_mm": round(need, 4)})
    return violations


def _nearest_blocker(win: _FineWindow, obstacles: list["_Obst"], net: str,
                     goal_xy: tuple[float, float]) -> dict[str, Any] | None:
    """The foreign obstacle nearest the (blocked) goal - named in a failure so a
    net that cannot route says WHAT is in the way (human copper especially)."""
    best: tuple[float, _Obst] | None = None
    for ob in obstacles:
        if ob.net == net and not ob.is_edge:
            continue
        d = ob.center_dist(goal_xy[0], goal_xy[1])
        if best is None or d < best[0]:
            best = (d, ob)
    if best is None:
        return None
    ob = best[1]
    return {"net": ob.net or "(edge/keepout)", "kind": ob.kind,
            "distance_mm": round(best[0], 4), "layers": sorted(ob.layers)}


# --------------------------------------------------------------------------- #
# Board surgery: emit / delete autorouter copper
# --------------------------------------------------------------------------- #

def _fmt(v: float) -> str:
    return _pcb._format_at_number(round(v, 6))


def _segment_block(s: dict[str, Any], net: str, width: float, uid: str) -> str:
    return (f'\t(segment\n\t\t(start {_fmt(s["x1"])} {_fmt(s["y1"])})\n'
            f'\t\t(end {_fmt(s["x2"])} {_fmt(s["y2"])})\n'
            f'\t\t(width {_fmt(width)})\n\t\t(layer "{s["layer"]}")\n'
            f'\t\t(net "{net}")\n\t\t(uuid "{uid}")\n\t)')


def _via_block(v: dict[str, Any], net: str, size: float, drill: float,
               top: str, bottom: str, uid: str) -> str:
    return (f'\t(via\n\t\t(at {_fmt(v["x"])} {_fmt(v["y"])})\n'
            f'\t\t(size {_fmt(size)})\n\t\t(drill {_fmt(drill)})\n'
            f'\t\t(layers "{top}" "{bottom}")\n\t\t(net "{net}")\n\t\t(uuid "{uid}")\n\t)')


def _delete_blocks_by_uuid(text: str, uuids: set[str]) -> tuple[str, int]:
    """Delete the enclosing (segment ...)/(via ...)/(arc ...) block for each
    uuid, by uuid/text-anchored surgery (same discipline as delete_group)."""
    removed = 0
    for uid in uuids:
        marker = f'(uuid "{uid}")'
        uidx = text.find(marker)
        if uidx == -1:
            continue
        # find the enclosing block open paren (segment/via/arc) before the uuid.
        start = -1
        for token in ("(segment", "(via", "(arc"):
            p = text.rfind(token, 0, uidx)
            if p > start:
                start = p
        if start == -1:
            continue
        depth = 0
        end = None
        for i in range(start, len(text)):
            if text[i] == "(":
                depth += 1
            elif text[i] == ")":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end is None:
            continue
        line_start = text.rfind("\n", 0, start)
        line_start = 0 if line_start == -1 else line_start
        seg_end = end + 1
        if seg_end < len(text) and text[seg_end] == "\n":
            seg_end += 1
        text = text[:line_start] + text[seg_end:]
        removed += 1
    return text, removed


# --------------------------------------------------------------------------- #
# Public: route_nets / unroute_nets
# --------------------------------------------------------------------------- #

def _corridor_from_global(win: _FineWindow, global_conn: dict[str, Any] | None,
                          coarse_grid: float, coarse_min: tuple[float, float]) -> set[tuple[int, int]] | None:
    """Fine window nodes within one coarse cell of the global stage's chosen
    coarse path - the soft corridor the detailed search is discounted to stay
    inside (leaving it costs off_corridor)."""
    if not global_conn or not global_conn.get("candidates"):
        return None
    coarse_path = global_conn["candidates"][0].get("coarse_path") or []
    if not coarse_path:
        return None
    radius = coarse_grid
    cells: set[tuple[int, int]] = set()
    cmnx, cmny = coarse_min
    for entry in coarse_path:
        ccx, ccy = entry[0], entry[1]
        wx = cmnx + (ccx + 0.5) * coarse_grid
        wy = cmny + (ccy + 0.5) * coarse_grid
        cix, ciy = win.cell_of(wx, wy)
        rr = int(math.ceil(radius / win.grid))
        for iy in range(ciy - rr, ciy + rr + 1):
            for ix in range(cix - rr, cix + rr + 1):
                if win.in_bounds(ix, iy):
                    cells.add((ix, iy))
    return cells or None


# --------------------------------------------------------------------------- #
# Step 4 - rip-up & reroute (PathFinder-style negotiated congestion) helpers
# --------------------------------------------------------------------------- #

def _project_congestion(win: _FineWindow, congestion: dict[tuple[int, int, str], int],
                        gminx: float, gminy: float, grid: float
                        ) -> dict[tuple[int, int, str], int] | None:
    """Map the board-global congestion field onto this window's LOCAL cells.

    Congestion is accumulated in board-absolute fine cells (so it is shared
    across per-connection windows whose local origins differ); before an A* run
    it is projected onto the window's own (ix, iy, layer) grid. Nearest-node
    mapping is exact when the window origin is grid-aligned to the board origin
    and otherwise off by at most half a cell - a soft-cost field, so approximate
    alignment is acceptable (documented)."""
    if not congestion:
        return None
    out: dict[tuple[int, int, str], int] = {}
    for (gix, giy, layer), val in congestion.items():
        if layer not in win.layer_types:
            continue
        wx = gminx + gix * grid
        wy = gminy + giy * grid
        ix = int(round((wx - win.minx) / grid))
        iy = int(round((wy - win.miny) / grid))
        if win.in_bounds(ix, iy):
            key = (ix, iy, layer)
            out[key] = out.get(key, 0) + int(val)
    return out or None


def _path_via_nodes(path: list[tuple[int, int, str]]) -> set[tuple[int, int]]:
    """The (ix, iy) cells where `path` changes layer (a via drop)."""
    vias: set[tuple[int, int]] = set()
    for i in range(1, len(path)):
        a, b = path[i - 1], path[i]
        if (a[0], a[1]) == (b[0], b[1]) and a[2] != b[2]:
            vias.add((a[0], a[1]))
    return vias


def _obstacle_on_path(win: _FineWindow, ob: "_Obst", path: list[tuple[int, int, str]],
                      via_nodes: set[tuple[int, int]]) -> bool:
    """True when this obstacle's copper occupies a cell the A* `path` uses - i.e.
    removing it is what freed the path. Used to name the exact rip set: only
    owners actually blocking the freed path are ripped, never merely-nearby
    copper."""
    via_cells, track_cells = win.obstacle_cells(ob)
    for (ix, iy, layer) in path:
        if (ix, iy) in track_cells.get(layer, ()):
            return True
    if via_cells:
        for (ix, iy) in via_nodes:
            if (ix, iy) in via_cells:
                return True
    return False


def _raise_path_congestion(congestion: dict[tuple[int, int, str], int], win: _FineWindow,
                           path: list[tuple[int, int, str]], gminx: float, gminy: float,
                           grid: float, bump_milli: int) -> int:
    """Escalate (raise) the negotiated-congestion cost on every board-global cell
    the newly-placed (displacing) route occupies, so the ripped nets re-route
    AROUND this contested corridor instead of straight back into it - the
    mechanism that makes rip-up converge. Returns the number of cells escalated."""
    bumped = 0
    for (ix, iy, layer) in path:
        wx, wy = win.node_xy(ix, iy)
        gix = int(round((wx - gminx) / grid))
        giy = int(round((wy - gminy) / grid))
        key = (gix, giy, layer)
        congestion[key] = congestion.get(key, 0) + bump_milli
        bumped += 1
    return bumped


def _obstacles_from_emit(net: str, segments: list[dict[str, Any]], vias: list[dict[str, Any]],
                         track_half: float, via_radius: float, routable_layers: list[str],
                         owner: int) -> list["_Obst"]:
    """The autorouter copper of one placed connection, as owner-tagged obstacles
    (so a later connection sees it, and step 4 can rip exactly this owner)."""
    obs: list[_Obst] = []
    for s in segments:
        obs.append(_Obst("seg", net, frozenset([s["layer"]]), track_half,
                         s["x1"], s["y1"], s["x2"], s["y2"], owner=owner))
    for v in vias:
        obs.append(_Obst("pt", net, frozenset(routable_layers), via_radius,
                         v["x"], v["y"], v["x"], v["y"], owner=owner))
    return obs


def route_nets(
    project_path: str | Path,
    nets: list[str] | None = None,
    connections: list[dict[str, Any]] | None = None,
    write: bool = False,
    allow_while_open: bool = False,
    max_ripup_iterations: int | None = None,
) -> dict[str, Any]:
    """Phase 7.3b detailed (fine, windowed) routing.

    For every unrouted connection (from `get_ratsnest`, filtered by `nets`, or a
    caller-supplied `connections` list) route exact copper in a per-connection
    obstacle window, in the SAME canonical order as the global stage (priority
    desc, airline asc). Each connection: build an obstacle window (bbox +
    `search_window_margin_mm`, doubling up to the whole board on failure); pad-
    escape both endpoints to the nearest legal grid node; run the fine A* softly
    constrained to the global stage's corridor; SELF-CHECK every proposed
    segment/via against all copper at netclass clearance BEFORE any write; then
    (write=True) append simplified `(segment)`/`(via)` blocks with create_group-
    style top-level surgery, recording their uuids in board-local
    `autorouter_owned` (per-net) so `unroute_nets` can undo them.

    Newly emitted copper becomes an obstacle for later connections in the same
    run (so two routed nets in one call stay DRC-clean against each other).

    STEP 4 (rip-up & reroute, negotiated congestion) IS ACTIVE. When a
    connection cannot route in its window, the window's obstacle cells are
    cleared INCREMENTALLY of the autorouter-owned copper on the freed path (never
    a full rebuild), the blocking autorouter connections are RIPPED (human/board
    copper is NEVER ripped - a net blocked solely by human copper fails with the
    blocker named), a `congestion` cost is escalated on the contested cells, and
    the ripped connections are re-queued to re-route (their corridor choice may
    change) - bounded by `max_ripup_iterations`. A displaced net does not
    immediately rip the net that displaced it (anti-thrash), and every decision
    is integer-milli / canonically ordered, so a given input routes identically
    run to run. The result reports `ripup_active: true` plus per-run rip-up stats
    (`ripup_iterations`, `connections_ripped`, `congestion_escalations`).

    Still simplified vs. the full spec (documented honestly): a self-check
    failure (proposed copper clears the A* obstacle model but not the exact
    clearance pass - the plane-skim case) is a hard failure, not demoted back to
    rip-up; pad escape lands on the nearest free grid node rather than a pad-
    direction-aware exact stub; neck-down (7.12) is not applied.

    PHASE 7.5.4 (plane-aware routing) IS ACTIVE for any net that owns a zone
    (a zone whose `net` matches - see `_plane_components_for`): a move whose
    destination lies on that net's own fill costs `plane.plane_step x island-
    factor` per mm instead of the normal trace cost (mainland factor 1.0,
    island `island_base / attachment_count`, orphan `orphan_island` - the
    7.5.3 model, `_component_attachments`); a via landing on the fill adds
    `plane.attachment_via`; and termination relaxes from "only the exact `to`
    grid point" to "any node of the net's own fill on a layer the goal's own
    item already reaches" (`_route_core`'s `goal_planes`, restricted to
    `layer in goal_layers` - see its comment for why the cross-layer case is
    deliberately NOT relaxed the same way). Plane traversal emits no copper
    (`_route_to_emit` drops any segment riding entirely on the net's own
    fill) - only the via(s) and real lead-in/lead-out stubs are written.
    Signal nets (plane_layers stays None) are provably unaffected - every new
    branch is gated behind an `is not None` check. HONEST LIMITATION found
    while testing this (see `tests/test_plane_routing.py`): the A* heuristic
    is distance-only (pre-existing, not changed here), so it is not
    admissible for a plane-discounted state; `_fine_astar` still returns a
    valid, deterministic, DRC-safe path, just not always the cost-global-
    optimum when a plane route and a normal-cost route both reach the goal -
    a plane-aware heuristic is out of this phase's scope.

    write=False (default) returns a full preview - per connection: routed flag,
    length_mm, via count, layers used, est. Phase-6 cost, self-check result, and
    failures with reasons - without touching the board. Always preview first.
    """
    board_path, project_file, _ = _pcb._resolve_project_path(project_path)
    settings = _pcb.load_pcb_settings(project_path)["config"]
    autor = settings.get("autorouter", {})
    grid = float(autor.get("grid_mm", 0.2)) or 0.2
    base_margin = float(autor.get("search_window_margin_mm", 8.0)) or 8.0
    if max_ripup_iterations is None:
        max_ripup_iterations = int(autor.get("max_ripup_iterations", 5))

    rules = _resolve_route_rules(project_path, settings)
    track_half = rules["track_width"] / 2.0
    via_radius = rules["via_diameter"] / 2.0

    weights = _Weights(autor.get("cost", {}),
                       float(settings.get("trace_cost", {}).get("via_weights", {}).get("through", 1.0)))
    layer_purpose = settings.get("layer_purpose", {})
    power_patterns = layer_purpose.get("power_net_patterns", [])
    directions = infer_layer_directions(project_path, settings=settings)["directions"]

    # routable layer set (mirror _CoarseModel's rule).
    all_layers = _pcb._parse_board_layers_cached(board_path)
    all_cu = [l["name"] for l in all_layers] or ["F.Cu", "B.Cu"]
    routable_types = {"signal", "power", "mixed", "jumper"}
    allowed = autor.get("allowed_layers", []) or []
    layer_types: dict[str, str] = {}
    routable_layers: list[str] = []
    for l in all_layers:
        if l["type"] not in routable_types:
            continue
        if allowed and l["name"] not in allowed:
            continue
        routable_layers.append(l["name"])
        layer_types[l["name"]] = l["type"]
    if not routable_layers:
        routable_layers = all_cu
        for name in routable_layers:
            layer_types.setdefault(name, "signal")
    routable_set = set(routable_layers)

    obstacles = _collect_obstacles(board_path, routable_set, all_cu, rules["edge_clearance"])
    board_bbox = _board_bbox(board_path)

    # -- 7.5.4 plane-aware routing: per-net own-fill components + costs ------ #
    plane_cfg = settings.get("plane", {}) or {}
    plane_step = float(plane_cfg.get("plane_step", 0.05))
    attachment_via_cost = float(plane_cfg.get("attachment_via", 8.0))
    island_base = float(plane_cfg.get("island_base", 40.0))
    orphan_island_cost = float(plane_cfg.get("orphan_island", 1000.0))
    plane_fill_index = _zone_fill_index_cached(board_path)
    _plane_footprints = _pcb._parse_footprint_pads_cached(board_path)
    _plane_tracks = _pcb._parse_tracks_cached(board_path)
    _plane_pads_by_net = _group_pads_by_net(_plane_footprints)
    _plane_stack_order = {name: i for i, name in enumerate(all_cu)}
    _plane_components_cache: dict[str, dict[str, list[dict[str, Any]]]] = {}

    def _plane_components_for(net: str) -> dict[str, list[dict[str, Any]]] | None:
        """This net's own fill, per routable layer, as `[{"raster", "factor"}]`
        components (7.5.3 model: mainland factor 1.0, island `island_base /
        attachment_count`, orphan `orphan_island`) - None when `net` does not
        own a zone (i.e. is not a key in the fill index) at all. Computed once
        per net and cached; only `_parse_zones_cached`-sourced (KiCad-filled)
        components are considered - a net whose zone has not been filled yet
        (no `filled_polygon`) gets no plane moves (documented partial: the
        7.5.2 "estimated" island fallback is NOT wired into routing)."""
        if net not in plane_fill_index:
            return None
        cached = _plane_components_cache.get(net)
        if cached is not None:
            return cached
        by_layer: dict[str, list[dict[str, Any]]] = {}
        for e in plane_fill_index[net]:
            if e["layer"] in routable_set:
                by_layer.setdefault(e["layer"], []).append(e)
        result: dict[str, list[dict[str, Any]]] = {}
        for layer, entries in sorted(by_layer.items()):
            recs = []
            for e in entries:
                comp_like = {"raster": e["raster"], "pts": e["pts"]}
                attachments = _component_attachments(
                    comp_like, layer, net, _plane_pads_by_net, _plane_tracks,
                    _plane_stack_order, all_cu,
                )
                recs.append((e, len(attachments), _polygon_area_mm2(e["pts"])))
            # mainland = most attachments (ties: larger area, then file order).
            recs.sort(key=lambda r: (-r[1], -r[2]))
            comps: list[dict[str, Any]] = []
            for idx, (e, n, _area) in enumerate(recs):
                if idx == 0:
                    factor = 1.0
                elif n == 0:
                    factor = orphan_island_cost
                else:
                    factor = island_base / n
                comps.append({"raster": e["raster"], "factor": factor})
            result[layer] = comps
        _plane_components_cache[net] = result
        return result

    # connections to route.
    if connections is None:
        rats = get_ratsnest(project_path, nets=nets)
        conns = rats["connections"]
    else:
        conns = list(connections)
        if nets is not None:
            wanted = set(nets)
            conns = [c for c in conns if c.get("net") in wanted]
    conns = sorted(conns, key=lambda c: (-float(c.get("priority", 0.0)),
                                         float(c.get("airline_length_mm", 0.0)),
                                         c.get("net", "")))

    # global stage (for home layer + corridor), routed on the same connections.
    global_by_key: dict[tuple[Any, ...], dict[str, Any]] = {}
    coarse_grid = 2.0
    coarse_min = (board_bbox[0], board_bbox[1])
    try:
        gr = global_route(project_path, connections=conns)
        coarse_grid = float(gr.get("global_grid_mm", 2.0))
        coarse_min = (gr["bbox"]["minx"], gr["bbox"]["miny"])
        for oc in gr["connections"]:
            key = (oc["net"], round(oc["from_point"]["x"], 3), round(oc["from_point"]["y"], 3),
                   round(oc["to_point"]["x"], 3), round(oc["to_point"]["y"], 3))
            global_by_key[key] = oc
    except Exception:
        pass

    tw = settings.get("trace_cost", {}).get("weights", {})
    board_min = (board_bbox[0], board_bbox[1])

    def _finalize(net: str, win: _FineWindow, path: list[tuple[int, int, str]],
                  from_xy: tuple[float, float], to_xy: tuple[float, float],
                  active_obstacles: list[_Obst], margin: float,
                  plane_layers: dict[str, list[dict[str, Any]]] | None = None,
                  ) -> tuple[dict[str, Any] | None, list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        """Turn a fine A* path into (rec-updates, segments, vias, violations).
        rec-updates is None when the exact self-check rejects the path."""
        segments, vias = _route_to_emit(win, path, from_xy, to_xy, plane_layers)
        violations = _self_check(net, segments, vias, active_obstacles, rules, via_radius)
        if violations:
            return None, segments, vias, violations
        length = sum(math.hypot(s["x2"] - s["x1"], s["y2"] - s["y1"]) for s in segments)
        layers_used = sorted({s["layer"] for s in segments},
                             key=lambda l: routable_layers.index(l) if l in routable_layers else 999)
        est_cost = (length * float(tw.get("length_mm", 1.0)) + len(vias) * float(tw.get("via", 5.0)))
        rec_updates = {
            "routed": True, "length_mm": round(length, 4), "via_count": len(vias),
            "layers": layers_used, "segment_count": len(segments),
            "window_margin_mm": margin, "est_phase6_cost": round(est_cost, 4),
            "self_check": {"passed": True, "violation_count": 0},
        }
        return rec_updates, segments, vias, []

    def _route_core(conn: dict[str, Any], owner: int, use_corridor: bool = True) -> dict[str, Any]:
        """Window-doubling fine A* + self-check for ONE connection against the
        current placements (and the shared congestion field). Emits nothing; the
        outer worklist owns placement/rip-up. Returns the record plus the last
        window and search parameters so step 4 can rip-up in-place.

        `use_corridor=False` drops the global-stage corridor bias - used when a
        RIPPED net re-routes, so its corridor choice is free to change (per spec)
        instead of being pulled back toward the gap it just lost."""
        net = conn["net"]
        net_kind = _pcb._net_kind(net, None, power_patterns)
        from_xy, to_xy = _conn_endpoints(conn)
        gkey = (net, round(from_xy[0], 3), round(from_xy[1], 3),
                round(to_xy[0], 3), round(to_xy[1], 3))
        gconn = global_by_key.get(gkey)
        home_layer = gconn.get("home_layer") if gconn else None
        # Start/goal layers are the PRECISE contact-item layers (the copper that
        # actually lives at from_point/to_point), not the whole island's layer
        # set - otherwise the emitted trace can land on a layer the endpoint
        # copper never reaches and float. Fall back to island, then all routable.
        from_item_layers = (conn.get("from") or {}).get("layers") or conn.get("from_layers") or routable_layers
        to_item_layers = (conn.get("to") or {}).get("layers") or conn.get("to_layers") or routable_layers
        start_layers = [l for l in from_item_layers if l in routable_set] or routable_layers
        goal_layers = set(l for l in to_item_layers if l in routable_set) or set(routable_layers)

        # 7.5.4 plane-aware routing: only for a net that owns a zone. `to_xy`
        # is tested against each own-fill component (grid-mm tolerance, since
        # the goal is an off-grid pad/via position) - a hit means that exact
        # component is ALREADY the goal's own copper, so reaching it anywhere
        # completes the connection (see `_fine_astar`'s `is_goal`). Restricted
        # to `layer in goal_layers`: the goal's OWN item must already be
        # reachable on THAT plane layer (e.g. a same-layer pad/via, or a via
        # that spans the plane layer, or the goal being another zone
        # component of the same net) - this is what makes the relaxation
        # electrically sound. It deliberately does NOT fire when the plane
        # layer differs from every layer the goal's real copper occupies
        # (e.g. a pad on B.Cu only, plane on In2.Cu only): reaching the plane
        # there is not the same copper as the pad, and completing the
        # connection still requires an actual via landing AT the goal's own
        # cell - i.e. that "pad awaits a plane via" case is served by the
        # cost model (cheap plane travel + attachment_via costing) only, not
        # by this termination relaxation (documented partial - see the
        # `route_nets` docstring / NETCLASS_PLAN 7.5.4 anchor).
        plane_layers = _plane_components_for(net)
        goal_planes: dict[str, list[dict[str, Any]]] | None = None
        if plane_layers:
            goal_planes = {}
            for layer, comps in plane_layers.items():
                if layer not in goal_layers:
                    continue
                hits = [c for c in comps if c["raster"].covers(to_xy[0], to_xy[1], grid)]
                if hits:
                    goal_planes[layer] = hits
            if not goal_planes:
                goal_planes = None

        result_rec: dict[str, Any] = {
            "net": net, "net_kind": net_kind,
            "from_point": {"x": round(from_xy[0], 4), "y": round(from_xy[1], 4)},
            "to_point": {"x": round(to_xy[0], 4), "y": round(to_xy[1], 4)},
            "airline_length_mm": conn.get("airline_length_mm"),
            "home_layer": home_layer, "routed": False,
            "length_mm": 0.0, "via_count": 0, "layers": [],
            "self_check": None, "failure": None,
        }
        out: dict[str, Any] = {
            "routed": False, "net": net, "net_kind": net_kind, "rec": result_rec,
            "segments": [], "vias": [], "win": None, "from_xy": from_xy, "to_xy": to_xy,
            "s_cell": None, "g_cell": None, "start_layers": start_layers,
            "goal_layers": goal_layers, "home_layer": home_layer, "corridor": None,
            "plane_layers": plane_layers, "goal_planes": goal_planes,
        }

        active_obstacles = active_obstacles_for(owner)
        margin = base_margin
        win: _FineWindow | None = None
        for _attempt in range(4):  # window doubling
            minx = max(min(from_xy[0], to_xy[0]) - margin, board_bbox[0] - grid)
            miny = max(min(from_xy[1], to_xy[1]) - margin, board_bbox[1] - grid)
            maxx = min(max(from_xy[0], to_xy[0]) + margin, board_bbox[2] + grid)
            maxy = min(max(from_xy[1], to_xy[1]) + margin, board_bbox[3] + grid)
            win = _FineWindow(minx, miny, maxx, maxy, grid, routable_layers, layer_types, net)
            if win.cols * win.rows * max(1, len(routable_layers)) > _MAX_WINDOW_NODES:
                result_rec["failure"] = {"reason": "window_too_large",
                                         "detail": f"window {win.cols}x{win.rows} exceeds node budget",
                                         "window_margin_mm": margin}
                out["win"] = None
                return out
            win.build(active_obstacles, track_half, via_radius, rules["clearance"], rules["edge_clearance"])
            s_cell = win.nearest_free(from_xy[0], from_xy[1], start_layers) or win.cell_of(*from_xy)
            g_cell = win.nearest_free(to_xy[0], to_xy[1], list(goal_layers)) or win.cell_of(*to_xy)
            corridor = _corridor_from_global(win, gconn, coarse_grid, coarse_min) if use_corridor else None
            win_cong = _project_congestion(win, congestion, board_min[0], board_min[1], grid)
            out.update({"win": win, "s_cell": s_cell, "g_cell": g_cell,
                        "corridor": corridor, "margin": margin,
                        "active_obstacles": active_obstacles})

            path = _fine_astar(win, net_kind, weights, layer_purpose, directions,
                               s_cell, start_layers, g_cell, goal_layers,
                               home_layer, corridor, win_cong,
                               plane_layers, goal_planes, plane_step, attachment_via_cost)
            if path is None:
                if margin >= _MAX_WINDOW_SPAN_MM:
                    break
                margin = min(margin * 2.0, _MAX_WINDOW_SPAN_MM)
                continue

            rec_updates, segments, vias, violations = _finalize(
                net, win, path, from_xy, to_xy, active_obstacles, margin, plane_layers)
            if rec_updates is None:
                result_rec["self_check"] = {"passed": False, "violations": violations[:8],
                                            "violation_count": len(violations)}
                result_rec["failure"] = {"reason": "self_check_failed",
                                         "detail": "proposed copper clears the A* obstacle model "
                                                   "but not the exact clearance pass (plane-skim); "
                                                   "not demoted to rip-up"}
                return out
            result_rec.update(rec_updates)
            out.update({"routed": True, "segments": segments, "vias": vias})
            return out

        # unreachable within the (doubled) window.
        blocker = _nearest_blocker(win, active_obstacles, net, to_xy) if win is not None else None
        result_rec["failure"] = {"reason": "unreachable_in_window",
                                 "nearest_blocker": blocker, "window_margin_mm": margin}
        return out

    # -- negotiated-congestion worklist -------------------------------------- #
    from collections import deque

    owner_conns = list(conns)                       # index == owner id (canonical)
    n_conns = len(owner_conns)
    placements: dict[int, dict[str, Any]] = {}      # owner -> {segments, vias, rec, net, obstacles}
    failures: dict[int, dict[str, Any]] = {}        # owner -> failed record
    congestion: dict[tuple[int, int, str], int] = {}
    congestion_bump = max(1, weights.q(weights.congestion))
    displaced_by: dict[int, int] = {}               # ripped owner -> displacing owner (anti-thrash)
    rerouted: set[int] = set()                       # owners that have been ripped (re-route corridor-free)
    ripup_iterations = 0
    connections_ripped = 0
    congestion_escalations = 0

    def active_obstacles_for(owner: int) -> list[_Obst]:
        act = list(obstacles)
        for oid, pl in placements.items():
            if oid == owner:
                continue
            act.extend(pl["obstacles"])
        return act

    def _place(owner: int, net: str, segments: list[dict[str, Any]],
               vias: list[dict[str, Any]], rec: dict[str, Any]) -> None:
        placements[owner] = {
            "segments": segments, "vias": vias, "rec": rec, "net": net,
            "obstacles": _obstacles_from_emit(net, segments, vias, track_half,
                                              via_radius, routable_layers, owner),
        }
        failures.pop(owner, None)

    pending: "deque[int]" = deque(range(n_conns))
    while pending:
        owner = pending.popleft()
        core = _route_core(owner_conns[owner], owner, use_corridor=owner not in rerouted)
        if core["routed"]:
            _place(owner, core["net"], core["segments"], core["vias"], core["rec"])
            continue

        # Step 4: attempt rip-up ONLY for an A*-unreachable failure (self-check /
        # window-budget failures are hard). Never rip when nothing is placed.
        failure = core["rec"].get("failure") or {}
        did_rip = False
        if (failure.get("reason") == "unreachable_in_window" and core["win"] is not None
                and placements and ripup_iterations < max_ripup_iterations):
            win = core["win"]
            # Anti-thrash: a net does not rip the net that just displaced it.
            protect = {displaced_by[owner]} if owner in displaced_by else set()
            rippable = [ob for oid, pl in placements.items() if oid not in protect
                        for ob in pl["obstacles"]]
            # Incrementally clear the rippable autorouter copper from THIS window
            # (no full rebuild) and re-search.
            for ob in rippable:
                win.remove_obstacle(ob)
            win_cong = _project_congestion(win, congestion, board_min[0], board_min[1], grid)
            free_path = _fine_astar(win, core["net_kind"], weights, layer_purpose, directions,
                                    core["s_cell"], core["start_layers"], core["g_cell"],
                                    core["goal_layers"], core["home_layer"], core["corridor"], win_cong,
                                    core["plane_layers"], core["goal_planes"],
                                    plane_step, attachment_via_cost)
            if free_path is not None:
                via_nodes = _path_via_nodes(free_path)
                blockers: set[int] = set()
                for oid, pl in placements.items():
                    if oid in protect:
                        continue
                    if any(_obstacle_on_path(win, ob, free_path, via_nodes) for ob in pl["obstacles"]):
                        blockers.add(oid)
                if blockers:
                    # Place THIS connection on the freed path; self-check against
                    # human copper + the placements we are KEEPING (non-blockers).
                    keep_obs = list(obstacles)
                    for oid, pl in placements.items():
                        if oid not in blockers:
                            keep_obs.extend(pl["obstacles"])
                    rec_updates, segments, vias, violations = _finalize(
                        core["net"], win, free_path, core["from_xy"], core["to_xy"],
                        keep_obs, core.get("margin", base_margin), core["plane_layers"])
                    if rec_updates is not None:
                        ripup_iterations += 1
                        congestion_escalations += _raise_path_congestion(
                            congestion, win, free_path, board_min[0], board_min[1],
                            grid, congestion_bump)
                        for b in sorted(blockers):
                            placements.pop(b, None)
                            displaced_by[b] = owner
                            rerouted.add(b)
                            connections_ripped += 1
                        rec = dict(core["rec"])
                        rec.update(rec_updates)
                        rec["ripped_to_place"] = sorted(blockers)
                        _place(owner, core["net"], segments, vias, rec)
                        # re-queue the ripped connections (canonical order).
                        pending = deque(sorted(set(pending) | blockers))
                        did_rip = True

        if not did_rip:
            failures[owner] = core["rec"]

    # Assemble outputs in canonical owner order.
    out_conns: list[dict[str, Any]] = []
    emit_segments: list[tuple[str, dict[str, Any], str]] = []  # (net, seg, uuid)
    emit_vias: list[tuple[str, dict[str, Any], str]] = []
    routed_count = 0
    for owner in range(n_conns):
        pl = placements.get(owner)
        if pl is not None:
            out_conns.append(pl["rec"])
            routed_count += 1
            for s in pl["segments"]:
                emit_segments.append((pl["net"], s, str(_uuid.uuid4())))
            for v in pl["vias"]:
                emit_vias.append((pl["net"], v, str(_uuid.uuid4())))
        else:
            out_conns.append(failures[owner])

    # ---- emit (write) -------------------------------------------------------
    written = False
    owned_added = {"segments": [], "vias": []}
    if write and (emit_segments or emit_vias):
        _pcb._check_not_locked_by_editor(board_path, allow_while_open)
        blocks: list[str] = []
        records: list[dict[str, Any]] = []
        for (net, s, uid) in emit_segments:
            blocks.append(_segment_block(s, net, rules["track_width"], uid))
            records.append({"uuid": uid, "net": net, "kind": "segment"})
            owned_added["segments"].append(uid)
        for (net, v, uid) in emit_vias:
            top, bottom = routable_layers[0], routable_layers[-1]
            # through via spans the full copper stack, not just routable subset.
            if all_cu:
                top, bottom = all_cu[0], all_cu[-1]
            blocks.append(_via_block(v, net, rules["via_diameter"], rules["via_drill"], top, bottom, uid))
            records.append({"uuid": uid, "net": net, "kind": "via"})
            owned_added["vias"].append(uid)
        text = _pcb._read_text(board_path)
        newline = _pcb._detect_newline(text)
        for block in blocks:
            text = _pcb._append_top_level_block(text, block)
        with board_path.open("w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        _pcb._invalidate_board_cache(board_path)

        # record ownership in board-local state (per-net, for unroute).
        state = _pcb.load_board_local(project_path)
        data = state["data"]
        data.setdefault("version", 1)
        owned = data.setdefault("autorouter_owned", {})
        owned.setdefault("segments", [])
        owned.setdefault("vias", [])
        owned.setdefault("records", [])
        owned["segments"].extend(owned_added["segments"])
        owned["vias"].extend(owned_added["vias"])
        owned["records"].extend(records)
        _pcb.save_board_local(project_path, data)
        written = True

    return {
        "board_path": str(board_path),
        "grid_mm": grid,
        "write": write,
        "written": written,
        "rules": rules,
        "max_ripup_iterations": max_ripup_iterations,
        "ripup_active": True,
        "ripup": {
            "iterations": ripup_iterations,
            "connections_ripped": connections_ripped,
            "congestion_escalations": congestion_escalations,
            "max_ripup_iterations": max_ripup_iterations,
        },
        "connections": out_conns,
        "summary": {
            "total_connections": len(out_conns),
            "connections_routed": routed_count,
            "connections_failed": len(out_conns) - routed_count,
            "segments_emitted": len(emit_segments),
            "vias_emitted": len(emit_vias),
            "total_length_mm": round(sum(c["length_mm"] for c in out_conns), 4),
            "ripup_iterations": ripup_iterations,
            "connections_ripped": connections_ripped,
            "congestion_escalations": congestion_escalations,
        },
    }


def unroute_nets(
    project_path: str | Path,
    nets: list[str] | None = None,
    write: bool = False,
    allow_while_open: bool = False,
) -> dict[str, Any]:
    """Delete autorouter-owned copper (the undo for `route_nets`). Removes only
    segments/vias recorded in board-local `autorouter_owned` - human-routed
    copper is never touched. `nets` restricts the deletion to those nets; omit to
    remove all autorouter-owned copper. write=False previews the uuids that would
    be removed without touching the board.
    """
    board_path, _, _ = _pcb._resolve_project_path(project_path)
    state = _pcb.load_board_local(project_path)
    data = state["data"]
    owned = data.get("autorouter_owned", {}) or {}
    records = owned.get("records", []) or []
    wanted = set(nets) if nets else None

    to_remove: list[dict[str, Any]] = []
    seg_set = set(owned.get("segments", []) or [])
    via_set = set(owned.get("vias", []) or [])
    if records:
        for rec in records:
            if wanted is None or rec.get("net") in wanted:
                to_remove.append(rec)
    else:
        # no per-record map (older state): fall back to the flat uuid lists.
        for uid in seg_set:
            to_remove.append({"uuid": uid, "net": None, "kind": "segment"})
        for uid in via_set:
            to_remove.append({"uuid": uid, "net": None, "kind": "via"})

    remove_uuids = {r["uuid"] for r in to_remove}
    removed = 0
    written = False
    if write and remove_uuids:
        _pcb._check_not_locked_by_editor(board_path, allow_while_open)
        text = _pcb._read_text(board_path)
        text, removed = _delete_blocks_by_uuid(text, remove_uuids)
        with board_path.open("w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        _pcb._invalidate_board_cache(board_path)
        # prune board-local ownership.
        owned["segments"] = [u for u in seg_set if u not in remove_uuids]
        owned["vias"] = [u for u in via_set if u not in remove_uuids]
        owned["records"] = [r for r in records if r["uuid"] not in remove_uuids]
        _pcb.save_board_local(project_path, data)
        written = True

    return {
        "board_path": str(board_path),
        "write": write,
        "written": written,
        "nets": sorted(wanted) if wanted else None,
        "candidates": len(remove_uuids),
        "removed": removed,
        "removed_uuids": sorted(remove_uuids),
    }


# =========================================================================== #
# Phase 7.17 - One command to route the board (CLI AND MCP, one implementation)
#
# `route_board` is a THIN orchestrator over the functions above - it duplicates
# no routing logic. `route_nets` already runs the full ratsnest -> global (7.3a)
# -> detailed (7.3b, incl. rip-up) pipeline for the unrouted connections, so the
# minimal one-command router is that call plus an effort->rip-up mapping and a
# consolidated, human/MCP-friendly report. The MCP tool `route_kicad_board` and
# the `python kicad_router_tool.py route ...` CLI are both skins over this one
# function (one implementation, not two - the same discipline as the sessions).
#
# Planes (7.5), whole-board optimization (7.6), and stitching (7.5.6) do not
# exist yet: they are declared as explicit TODO pipeline hooks below so they
# slot in at M4 WITHOUT changing this signature. Nothing is faked.
# =========================================================================== #

# effort preset -> max_ripup_iterations. Today effort only tunes rip-up
# aggressiveness; it gains meaning (SA, replicas, plateau stopping) when the
# 7.6 optimizer lands. quick = single pass, no rip-up; balanced = config
# default; best = aggressive rip-up.
_EFFORT_RIPUP: dict[str, int | None] = {"quick": 0, "balanced": None, "best": 20}


def route_board(
    project_path: str | Path,
    nets: list[str] | None = None,
    write: bool = False,
    effort: str = "balanced",
    allow_while_open: bool = False,
) -> dict[str, Any]:
    """Phase 7.17 - the ONE command to route the board (CLI + MCP).

    Runs the end-to-end minimal routing pipeline behind a single call so a
    caller does not have to orchestrate ratsnest -> global -> detailed by hand:

      1. `get_ratsnest`  - report what is unrouted BEFORE (informational).
      2. `route_nets`    - which itself runs ratsnest -> `global_route` (7.3a) ->
                           detailed windowed A* (7.3b) with rip-up, over every
                           unrouted (or `nets`-selected) connection.

    This is a thin orchestrator - it calls the existing functions and rolls their
    results into one report; it contains no routing logic of its own.

    `effort` maps to rip-up aggressiveness only, for now: "quick" (single pass,
    no rip-up), "balanced" (pcb_settings default), "best" (aggressive rip-up).
    Higher efforts become meaningfully different when the 7.6 optimizer lands;
    that is stated honestly in the report's `notes`.

    Plane-aware routing (7.5), whole-board optimization (7.6), and the stitching
    pass (7.5.6) are NOT wired yet - the report's `pipeline` block marks them as
    the M4 TODO hooks they are; the signature will not change when they land.

    `write=False` (default) previews without touching the board; `write=True` is
    the explicit apply. Reversible with `unroute_nets` / `unroute_kicad_nets`.
    """
    effort = (effort or "balanced").lower()
    if effort not in _EFFORT_RIPUP:
        raise ValueError(
            f"effort must be one of {sorted(_EFFORT_RIPUP)}; got {effort!r}")
    max_ripup = _EFFORT_RIPUP[effort]

    # Stage 0 - what is unrouted before (reporting only; route_nets recomputes).
    before = get_ratsnest(project_path, nets=nets)
    before_summary = before.get("summary", {})
    unrouted_before = before_summary.get("total_connections", 0)

    # Stages 1-3 - ratsnest -> global -> detailed, all inside route_nets.
    detailed = route_nets(
        project_path,
        nets=nets,
        write=write,
        allow_while_open=allow_while_open,
        max_ripup_iterations=max_ripup,
    )
    d_summary = detailed.get("summary", {})

    return {
        "command": "route_board",
        "board_path": detailed.get("board_path"),
        "effort": effort,
        "write": write,
        "written": detailed.get("written", False),
        "unrouted_before": unrouted_before,
        "unrouted_nets_before": before.get("unrouted_nets", []),
        "airline_before_mm": before_summary.get("total_airline_mm"),
        "routed": d_summary.get("connections_routed", 0),
        "failed": d_summary.get("connections_failed", 0),
        "total_routed_length_mm": d_summary.get("total_length_mm", 0.0),
        "vias_emitted": d_summary.get("vias_emitted", 0),
        "ripup": detailed.get("ripup", {}),
        "connections": detailed.get("connections", []),
        "detailed_result": detailed,   # full route_nets result for callers who want it
        "pipeline": {
            "ratsnest": "done",
            "global_route": "done",
            "detailed_route": "done",
            "rip_up": "disabled (effort=quick)" if max_ripup == 0 else "active",
            "plane_aware_routing": "not_implemented (Phase 7.5, M4)",
            "whole_board_optimization": "not_implemented (Phase 7.6, M4)",
            "stitching": "not_implemented (Phase 7.5.6, M4)",
        },
        "notes": [
            "Minimal route_board (Phase 7.17): ratsnest -> global -> detailed only; "
            "planes/optimizer/stitching are M4 TODO hooks and do not run yet.",
            "effort currently maps only to rip-up aggressiveness "
            "(quick=0, balanced=config default, best=20).",
        ],
    }


# --------------------------------------------------------------------------- #
# CLI - a thin skin over route_board (and unroute), so the board can be routed
# from the command line with one command:
#     python kicad_router_tool.py route <project> [--write] [--nets ...] [--effort ...]
#     python kicad_router_tool.py unroute <project> [--write] [--nets ...]
# Dry-run by default; --write applies after printing the preview.
# --------------------------------------------------------------------------- #

def _cli_print_route_report(report: dict[str, Any]) -> None:
    print(f"route_board  board={report.get('board_path')}")
    print(f"  effort={report['effort']}  write={report['write']}  "
          f"written={report['written']}")
    print(f"  unrouted before: {report['unrouted_before']} connection(s), "
          f"airline {report.get('airline_before_mm')} mm")
    print(f"  routed: {report['routed']}   failed: {report['failed']}   "
          f"routed length: {report['total_routed_length_mm']} mm   "
          f"vias: {report['vias_emitted']}")
    rip = report.get("ripup", {})
    if rip:
        print(f"  rip-up: iterations={rip.get('iterations')} "
              f"ripped={rip.get('connections_ripped')} "
              f"escalations={rip.get('congestion_escalations')}")
    for c in report.get("connections", []):
        if c.get("routed"):
            print(f"    [OK]   {c['net']}: {c.get('length_mm')} mm, "
                  f"{c.get('via_count')} via(s), layers {c.get('layers')}")
        else:
            reason = (c.get("failure") or {}).get("reason", "unknown")
            print(f"    [FAIL] {c['net']}: {reason}")
    pipe = report.get("pipeline", {})
    todo = [k for k, v in pipe.items() if str(v).startswith("not_implemented")]
    if todo:
        print(f"  not-yet-wired (M4): {', '.join(todo)}")
    if not report["write"]:
        print("  (dry-run - re-run with --write to apply)")


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="kicad_router_tool.py",
        description="Route a KiCad board from the command line (Phase 7.17).")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_route = sub.add_parser("route", help="route the board (the one command)")
    p_route.add_argument("project_path", help="path to the .kicad_pro/.kicad_pcb project")
    p_route.add_argument("--write", action="store_true",
                         help="apply the routing (default: dry-run preview only)")
    p_route.add_argument("--nets", nargs="+", metavar="NET",
                         help="restrict to these net names (default: every unrouted net)")
    p_route.add_argument("--effort", choices=sorted(_EFFORT_RIPUP), default="balanced")
    p_route.add_argument("--allow-while-open", action="store_true",
                         help="route even if the board looks open in an editor")
    p_route.add_argument("--json", action="store_true", help="print the raw JSON report")

    p_unroute = sub.add_parser("unroute", help="delete autorouter-owned copper (undo)")
    p_unroute.add_argument("project_path")
    p_unroute.add_argument("--write", action="store_true")
    p_unroute.add_argument("--nets", nargs="+", metavar="NET")
    p_unroute.add_argument("--allow-while-open", action="store_true")

    args = parser.parse_args(argv)

    if args.cmd == "route":
        report = route_board(
            args.project_path,
            nets=args.nets,
            write=args.write,
            effort=args.effort,
            allow_while_open=args.allow_while_open,
        )
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            _cli_print_route_report(report)
        return 0

    if args.cmd == "unroute":
        report = unroute_nets(
            args.project_path,
            nets=args.nets,
            write=args.write,
            allow_while_open=args.allow_while_open,
        )
        print(json.dumps(report, indent=2))
        return 0

    parser.error(f"unknown command {args.cmd!r}")
    return 2


if __name__ == "__main__":
    import sys
    raise SystemExit(main(sys.argv[1:]))
