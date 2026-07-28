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
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
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


def _plane_fill_index_with_estimated(
    board_path: Path, grid_mm: float, clearance_mm: float,
) -> dict[str, list[dict[str, Any]]]:
    """Extends `_zone_fill_index_cached` (KiCad `filled_polygon` blocks only)
    with the Phase 7.5.2 "estimated" fallback for zone/layer pairs that have
    not been filled in KiCad yet, so plane-aware routing (7.5.4) can use an
    unfilled-but-still-poured net as a routable plane too - previously a
    documented residual ("only KiCad-filled zones feed the plane model").
    Reuses the exact rasterize/subtract-foreign-copper/flood-fill estimation
    `audit_plane_islands` already performs (`_estimate_layer_components`), so
    the two stay consistent with each other.

    Same return shape as `_zone_fill_index_cached`
    (`{net: [{layer, pts, uuid, name, raster}]}`), plus every entry (kicad AND
    estimated) now also carries `area_mm2` and `fill_source` - `area_mm2` so
    callers never need to fall back to `_polygon_area_mm2(pts)` when `pts` is
    `None` (an estimated component has no polygon, only a raster)."""
    zones = _parse_zones_cached(board_path)
    kicad_fills = _zone_fill_index_cached(board_path)
    footprints = _pcb._parse_footprint_pads_cached(board_path)
    tracks = _pcb._parse_tracks_cached(board_path)
    layers_info = _pcb._parse_board_layers_cached(board_path)
    all_cu = [lyr["name"] for lyr in layers_info] or ["F.Cu", "B.Cu"]
    stack_order = {name: i for i, name in enumerate(all_cu)}

    kicad_by_zone_layer: dict[tuple[str, str], list[dict[str, Any]]] = {}
    fills: dict[str, list[dict[str, Any]]] = {}
    for net_name, entries in kicad_fills.items():
        for e in entries:
            kicad_by_zone_layer.setdefault((e["uuid"], e["layer"]), []).append(e)
            out = dict(e)
            out["area_mm2"] = _polygon_area_mm2(e["pts"])
            out["fill_source"] = "kicad"
            fills.setdefault(net_name, []).append(out)

    for zone in zones:
        net = zone.get("net", "")
        if not net:
            continue
        for layer in [l for l in zone.get("layers", []) if l.endswith(".Cu")]:
            if kicad_by_zone_layer.get((zone["uuid"], layer)):
                continue  # already covered by the kicad-authoritative branch above
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
            for comp in comps:
                fills.setdefault(net, []).append({
                    "layer": layer,
                    "pts": comp["pts"],
                    "uuid": zone.get("uuid", ""),
                    "name": zone.get("name", ""),
                    "raster": comp["raster"],
                    "area_mm2": comp["area_mm2"],
                    "fill_source": "estimated",
                })
    return fills


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
    # Phase 7.18.2: every fill component this pass builds, keyed (net, layer),
    # so the cross-layer continuity view below can ask "does this via/pad land
    # in real copper on BOTH layers?" against the same components the
    # same-layer island model just costed - one fill decomposition, two views.
    comps_by_net_layer: dict[tuple[str, str], list[dict[str, Any]]] = {}
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

            comps_by_net_layer.setdefault((net, layer), []).extend(comps)

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

    cross_layer, weak_pairs = _cross_layer_continuity(
        comps_by_net_layer, pads_by_net, tracks, stack_order, all_cu, warn_below)

    return {
        "board_path": str(board_path),
        "plane_settings": {
            "plane_step": plane_step,
            "island_base": island_base,
            "orphan_island": orphan_island,
            "island_min_attachments_warn": warn_below,
        },
        "zones": zone_reports,
        "cross_layer": cross_layer,
        "summary": {
            "island_count": total_islands,
            "orphan_island_count": total_orphans,
            "total_island_cost": round(total_cost, 4),
            "warnings": warnings,
            "weakly_coupled_layer_pairs": weak_pairs,
        },
    }


def _cross_layer_continuity(
    comps_by_net_layer: dict[tuple[str, str], list[dict[str, Any]]],
    pads_by_net: dict[str, list[dict[str, Any]]],
    tracks: dict[str, list[dict[str, Any]]],
    stack_order: dict[str, int], all_cu: list[str], warn_below: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Phase 7.18.2 - the CROSS-layer view of plane continuity.

    7.5.3's island model answers "is this island connected to the rest of its
    OWN layer's pour". It says nothing about the other axis: two pours of the
    same net on two different layers are nominally the same node in the
    netlist, but if only one via bonds them, everything referencing one of them
    reaches the other through that single via's inductance/resistance - a real
    electrical weakness the schematic cannot show. This makes it visible.

    For each net owning fill on more than one copper layer, every unordered
    LAYER PAIR gets a count of BONDING VIAS: same-net vias whose electrical
    span (`_via_layer_set` - a through via bonds every layer between its named
    ends, not just the two named) covers both layers AND which physically land
    inside real fill copper on BOTH layers. Same-net through-hole PADS that do
    the same are counted separately (`bonding_pad_count`): they genuinely bond
    the layers too, so hiding them would over-warn, but they are not something
    a stitching pass places, so they do not clear the flag.

    A pair with fewer than `plane.island_min_attachments_warn` bonding VIAS is
    flagged `weakly_coupled` - the same knob and the same "how many attachment
    points are enough" convention the same-layer model already uses.

    READ-ONLY, and no new tool: `run_kicad_stitching_pass` (7.5.6) is already
    the writer that fixes exactly this, so this closes the loop by making the
    gap visible rather than adding a second way to place copper."""

    def _lands_in_fill(net: str, layer: str, x: float, y: float, reach: float) -> bool:
        for comp in comps_by_net_layer.get((net, layer), []):
            if comp["raster"].covers(x, y, reach):
                return True
        return False

    nets_layers: dict[str, set[str]] = {}
    for (net, layer) in comps_by_net_layer:
        nets_layers.setdefault(net, set()).add(layer)

    reports: list[dict[str, Any]] = []
    weak: list[dict[str, Any]] = []
    for net in sorted(nets_layers):
        layers = sorted(nets_layers[net], key=lambda l: stack_order.get(l, 0))
        if len(layers) < 2:
            continue
        pairs: list[dict[str, Any]] = []
        for i, la in enumerate(layers):
            for lb in layers[i + 1:]:
                vias = []
                for via in tracks.get("vias", []):
                    if via.get("net") != net:
                        continue
                    span = _via_layer_set(via, stack_order, all_cu)
                    if la not in span or lb not in span:
                        continue
                    at = via["at"]
                    reach = float(via.get("size", 0.6)) / 2.0
                    if (_lands_in_fill(net, la, at["x"], at["y"], reach)
                            and _lands_in_fill(net, lb, at["x"], at["y"], reach)):
                        vias.append({
                            "uuid": via.get("uuid", ""),
                            "position": {"x": round(at["x"], 4), "y": round(at["y"], 4)},
                        })
                pad_count = 0
                for pad in pads_by_net.get(net, []):
                    span = _pad_layer_set(pad, all_cu)
                    if la not in span or lb not in span:
                        continue
                    pos = pad["position"]
                    reach = _pad_reach(pad)
                    if (_lands_in_fill(net, la, pos["x"], pos["y"], reach)
                            and _lands_in_fill(net, lb, pos["x"], pos["y"], reach)):
                        pad_count += 1
                weakly = len(vias) < warn_below
                rec = {
                    "layers": [la, lb],
                    "stack_adjacent": abs(stack_order.get(la, 0) - stack_order.get(lb, 0)) == 1,
                    "bonding_via_count": len(vias),
                    "bonding_vias": vias[:8],
                    "bonding_pad_count": pad_count,
                    "weakly_coupled": weakly,
                }
                pairs.append(rec)
                if weakly:
                    weak.append({"net": net, "layers": [la, lb],
                                 "bonding_via_count": len(vias),
                                 "bonding_pad_count": pad_count})
        reports.append({
            "net": net, "layers": layers, "layer_pairs": pairs,
            "weakly_coupled_pair_count": sum(1 for p in pairs if p["weakly_coupled"]),
        })
    return reports, weak


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

    PHASE 7.18.2 adds the CROSS-layer view alongside the per-zone one, under
    the top-level `cross_layer` key (plus `summary.weakly_coupled_layer_pairs`):
    for every net owning fill on more than one copper layer, each unordered
    layer pair reports how many same-net vias actually BOND those two pours
    (span both layers AND land in real fill copper on both), how many same-net
    through-hole pads do (context - they bond too, but a stitching pass does
    not place them), whether the two layers are stack-adjacent, and a
    `weakly_coupled` flag when the bonding-VIA count is below `plane.island_
    min_attachments_warn`. Two pours that are one netlist node but are joined
    by a single via are electrically thin between them; `run_kicad_stitching_
    pass` (7.5.6) is the existing writer that fixes what this flags.
    """
    board_path, _, _ = _pcb._resolve_project_path(project_path)
    settings = _pcb.load_pcb_settings(project_path)["config"]
    return _zone_island_model(board_path, settings)


# =========================================================================== #
# Phase 7.5.5 - Creating and moving planes (the plane WRITERS)
#
# Same dry-run/write/lock discipline as every other writer (create_netclass,
# create_group, route_nets): zone outlines are uuid-anchored s-expr surgery,
# same as delete_group/_delete_blocks_by_uuid. `propose_plane` is read-only
# (never writes, no ownership restriction - it's a suggestion for a human to
# review, even against a hand-made zone/net); `create_plane`/`modify_plane`
# write, and `modify_plane` refuses on any zone uuid not recorded in
# board-local `autorouter_owned.zones` - i.e. it may only move/resize a zone
# THIS tool created, never one of the six hand-made kiln zones (mainGnd,
# safty_gnd, antenna, main3.3, main12v, 3.3v_safty), which can only ever be
# *proposed* for change.
# =========================================================================== #

def _zone_template_shape(zones: list[dict[str, Any]]) -> dict[str, Any]:
    """Pick a representative net-owning (non-keepout) board zone's
    fill-setting SHAPE - hatch, connect_pads clearance, min_thickness, fill
    block (thermal_gap, thermal_bridge_width, smoothing, radius,
    island_removal_mode) - to copy onto a newly created zone so it looks
    native, the same "copy an existing definition's shape" idea
    `create_netclass` uses for the Default netclass. Falls back to sane
    KiCad-typical defaults only if the board has no net-owning zone at all."""
    template = next((z for z in zones if z.get("net") and not z.get("keepout")), None)
    if template is None:
        return {
            "hatch": {"style": "edge", "pitch": 0.5},
            "connect_pads": {"mode": None, "clearance": 0.2},
            "min_thickness": 0.2,
            "fill": {
                "enabled": True, "thermal_gap": 0.5, "thermal_bridge_width": 0.5,
                "smoothing": "fillet", "radius": 0.1, "island_removal_mode": 0,
            },
        }
    return {
        "hatch": template.get("hatch") or {"style": "edge", "pitch": 0.5},
        "connect_pads": template.get("connect_pads") or {"mode": None, "clearance": 0.2},
        "min_thickness": (
            template["min_thickness"] if template.get("min_thickness") is not None else 0.2
        ),
        "fill": dict(template.get("fill") or {"enabled": True, "island_removal_mode": 0}),
    }


def _zone_block(
    net: str, layer: str, name: str, uid: str,
    outline: list[tuple[float, float]], shape: dict[str, Any], priority: int = 0,
) -> str:
    """Serialize one board-level `(zone ...)` text block in the exact shape
    KiCad itself writes (1-tab top-level indent, 2-tab children - matching
    `_segment_block`/`_via_block`/`create_group`'s `(group ...)` block), from
    a candidate outline plus a fill-setting SHAPE copied from an existing
    zone (`_zone_template_shape`)."""
    hatch = shape.get("hatch") or {"style": "edge", "pitch": 0.5}
    cp = shape.get("connect_pads") or {}
    clearance = cp.get("clearance")
    if clearance is None:
        clearance = 0.2
    min_thickness = shape.get("min_thickness")
    if min_thickness is None:
        min_thickness = 0.2
    fill = shape.get("fill") or {}

    lines = ['\t(zone', f'\t\t(net "{net}")', f'\t\t(layers "{layer}")',
              f'\t\t(uuid "{uid}")', f'\t\t(name "{name}")']
    if priority:
        lines.append(f'\t\t(priority {int(priority)})')
    lines.append(f'\t\t(hatch {hatch.get("style", "edge")} {_fmt(hatch.get("pitch", 0.5) or 0.5)})')
    mode = cp.get("mode")
    lines.append(f'\t\t(connect_pads {mode}' if mode else '\t\t(connect_pads')
    lines.append(f'\t\t\t(clearance {_fmt(clearance)})')
    lines.append('\t\t)')
    lines.append(f'\t\t(min_thickness {_fmt(min_thickness)})')
    lines.append(f'\t\t(fill {"yes" if fill.get("enabled", True) else "no"}')
    if fill.get("thermal_gap") is not None:
        lines.append(f'\t\t\t(thermal_gap {_fmt(fill["thermal_gap"])})')
    if fill.get("thermal_bridge_width") is not None:
        lines.append(f'\t\t\t(thermal_bridge_width {_fmt(fill["thermal_bridge_width"])})')
    if fill.get("smoothing"):
        lines.append(f'\t\t\t(smoothing {fill["smoothing"]})')
    if fill.get("radius") is not None:
        lines.append(f'\t\t\t(radius {_fmt(fill["radius"])})')
    irm = fill.get("island_removal_mode", 0)
    lines.append(f'\t\t\t(island_removal_mode {int(irm) if irm is not None else 0})')
    lines.append('\t\t)')
    lines.append('\t\t(polygon')
    lines.append('\t\t\t(pts')
    pts_str = " ".join(f'(xy {_fmt(x)} {_fmt(y)})' for x, y in outline)
    lines.append(f'\t\t\t\t{pts_str}')
    lines.append('\t\t\t)')
    lines.append('\t\t)')
    lines.append('\t)')
    return "\n".join(lines)


def propose_plane(
    project_path: str | Path, net: str, layer: str | None = None,
) -> dict[str, Any]:
    """Phase 7.5.5 - propose a candidate copper-pour plane for `net` on
    `layer` (MCP tool `propose_kicad_plane`). READ-ONLY: never writes,
    has no ownership restriction (unlike `create_plane`/`modify_plane`) -
    the result is a suggestion for a human to review, even against a net
    that already owns one of the six hand-made zones.

    Candidate outline: a grid-based coverage region of the net's own pads
    and vias on `layer` - the rectilinear hull (here: their bounding box,
    inflated by each pad/via's own reach plus a fixed margin, "simplified"
    per the spec) clipped to the board's Edge.Cuts extents. `layer` may be
    omitted: it is then auto-picked from the board's copper layers,
    preferring a layer whose `layer_purpose` TYPE matches the net's OWN
    kind (7.2 - a power net prefers a `power`-type layer, a signal net a
    `signal`-type one), tie-broken by stack order.

    The outline is then run through the exact 7.5.2 estimation pipeline
    `audit_plane_islands` uses (`_estimate_layer_components`, minus any
    HIGHER-priority zone already on `layer` and minus clearance-inflated
    foreign (other-net) copper) to report `components` (mainland/island/
    orphan, same 7.5.3 costing) and an `estimate` block, plus a **cost
    delta** vs the net's CURRENT routed trace cost (`get_trace_cost`; 0.0
    if the net has no routed copper yet): `projected_plane_cost` (the
    optimizer's flat `create_plane` cost plus the projected ongoing island
    cost) minus `current_routing_cost`. This is a simplified ESTIMATE, not
    a re-route simulation - it does not model what a signal net's cost
    would become if left as ordinary copper after the plane existed;
    negative `cost_delta` means the plane looks cheaper than the net's
    current copper.

    Raises if `net` has no pads/vias at all on the resolved layer, or if
    the computed outline collapses to zero area after clipping to the
    board outline.
    """
    board_path, _, _ = _pcb._resolve_project_path(project_path)
    settings = _pcb.load_pcb_settings(project_path)["config"]
    layer_purpose = settings.get("layer_purpose", {})
    power_patterns = layer_purpose.get("power_net_patterns", [])
    plane_cfg = settings.get("plane", {}) or {}
    autor = settings.get("autorouter", {}) or {}
    grid_mm = float(autor.get("grid_mm", 0.2)) or 0.2
    clearance_mm = float(autor.get("clearance_fallback_mm", 0.2))
    margin_mm = float(plane_cfg.get("propose_outline_margin_mm", 1.0))

    footprints = _pcb._parse_footprint_pads_cached(board_path)
    tracks = _pcb._parse_tracks_cached(board_path)
    layers_info = _pcb._parse_board_layers_cached(board_path)
    all_cu = [l["name"] for l in layers_info] or ["F.Cu", "B.Cu"]
    stack_order = {name: i for i, name in enumerate(all_cu)}
    pads_by_net = _group_pads_by_net(footprints)

    net_kind = _pcb._net_kind(net, None, power_patterns)

    if layer is None:
        layer_types = {l["name"]: l["type"] for l in layers_info}
        preferred_type = "power" if net_kind == "power" else "signal"
        candidates = [l for l in all_cu if l.endswith(".Cu")]
        if not candidates:
            raise ValueError("Board has no copper layers to propose a plane on")
        candidates.sort(key=lambda name: (0 if layer_types.get(name) == preferred_type else 1,
                                          stack_order.get(name, 0)))
        layer = candidates[0]
    elif layer not in all_cu:
        raise ValueError(f"Layer {layer!r} not found on board (copper layers: {all_cu})")

    points: list[tuple[float, float, float]] = []
    for pad in pads_by_net.get(net, []):
        if layer not in _pad_layer_set(pad, all_cu):
            continue
        pos = pad["position"]
        points.append((pos["x"], pos["y"], _pad_reach(pad)))
    for via in tracks.get("vias", []):
        if via.get("net") != net:
            continue
        if layer not in _via_layer_set(via, stack_order, all_cu):
            continue
        at = via["at"]
        points.append((at["x"], at["y"], float(via.get("size", 0.6)) / 2.0))
    if not points:
        raise ValueError(
            f"Net {net!r} has no pads or vias on layer {layer!r} to build a plane "
            "coverage region from"
        )

    minx = min(p[0] - p[2] for p in points) - margin_mm
    maxx = max(p[0] + p[2] for p in points) + margin_mm
    miny = min(p[1] - p[2] for p in points) - margin_mm
    maxy = max(p[1] + p[2] for p in points) + margin_mm

    bbminx, bbminy, bbmaxx, bbmaxy = _board_bbox(board_path)
    minx, miny = max(minx, bbminx), max(miny, bbminy)
    maxx, maxy = min(maxx, bbmaxx), min(maxy, bbmaxy)
    if minx >= maxx or miny >= maxy:
        raise ValueError(
            "Computed plane outline collapsed to zero area after clipping to the "
            "board's Edge.Cuts extents"
        )

    outline = [(minx, miny), (maxx, miny), (maxx, maxy), (minx, maxy)]

    zones = _parse_zones_cached(board_path)
    higher_polys = [
        z["polygon"] for z in zones
        if layer in z.get("layers", [])
        and z.get("priority", 0) > 0
        and len(z.get("polygon") or []) >= 3
    ]
    foreign_items = _foreign_copper_items(layer, net, footprints, tracks, stack_order, all_cu, clearance_mm)
    comps = _estimate_layer_components({"polygon": outline}, layer, higher_polys, foreign_items, grid_mm)

    island_base = float(plane_cfg.get("island_base", 40.0))
    orphan_island = float(plane_cfg.get("orphan_island", 1000.0))

    comp_records = []
    for comp in comps:
        attachments = _component_attachments(comp, layer, net, pads_by_net, tracks, stack_order, all_cu)
        comp_records.append({
            "comp": comp, "attachments": attachments,
            "attachment_count": len(attachments), "area_mm2": comp["area_mm2"],
        })
    comp_records.sort(key=lambda r: (-r["attachment_count"], -r["area_mm2"]))

    out_components: list[dict[str, Any]] = []
    total_island_cost = 0.0
    island_count = 0
    orphan_count = 0
    for idx, rec in enumerate(comp_records):
        n = rec["attachment_count"]
        if idx == 0:
            role, cost = "mainland", 0.0
        elif n == 0:
            role, cost = "orphan", orphan_island
            orphan_count += 1
        else:
            role, cost = "island", island_base / n
            island_count += 1
        if idx != 0:
            total_island_cost += cost
        out_components.append({
            "role": role, "attachment_count": n, "attachments": rec["attachments"],
            "area_mm2": round(rec["area_mm2"], 4), "cost": round(cost, 4),
        })

    create_plane_cost = float(settings.get("optimizer", {}).get("create_plane", 15.0))
    projected_plane_cost = round(create_plane_cost + total_island_cost, 4)

    try:
        current = _pcb.get_trace_cost(project_path, net=net)
        current_total = float(current["cost"]["total"])
    except KeyError:
        current_total = 0.0

    return {
        "board_path": str(board_path),
        "net": net,
        "net_kind": net_kind,
        "layer": layer,
        "outline": [{"x": round(x, 4), "y": round(y, 4)} for x, y in outline],
        "outline_area_mm2": round(_polygon_area_mm2(outline), 4),
        "component_count": len(out_components),
        "components": out_components,
        "estimate": {
            "island_count": island_count,
            "orphan_count": orphan_count,
            "total_island_cost": round(total_island_cost, 4),
            "create_plane_cost": create_plane_cost,
            "projected_plane_cost": projected_plane_cost,
        },
        "current_routing_cost": round(current_total, 4),
        "cost_delta": round(projected_plane_cost - current_total, 4),
        "note": (
            "cost_delta = (create_plane one-time cost + projected ongoing island "
            "cost) minus the net's CURRENT routed trace cost (0.0 if unrouted); "
            "negative means the plane looks cheaper than the net's current copper. "
            "Simplified estimate, not a re-route simulation."
        ),
    }


def create_plane(
    project_path: str | Path,
    net: str,
    layer: str | None = None,
    name: str | None = None,
    priority: int = 0,
    write: bool = False,
    allow_while_open: bool = False,
) -> dict[str, Any]:
    """Phase 7.5.5 - create a new copper-pour zone for `net` on `layer` (MCP
    tool `create_kicad_plane`) from `propose_plane`'s candidate outline,
    copying the board's existing zone fill-setting SHAPE (hatch,
    connect_pads clearance, min_thickness, thermal gap, smoothing) via
    `_zone_template_shape` so the new zone looks native - the same
    "copy an existing definition's shape" pattern `create_netclass` uses
    for the Default netclass, applied here to zones.

    Same dry-run/write/lock discipline as every writer in this module:
    write=False (default) returns the exact `(zone ...)` text block that
    WOULD be appended, without touching the board. write=True appends it
    (uuid-anchored top-level block, same `_append_top_level_block` surgery
    `create_group` uses) and records the new zone's uuid in board-local
    `autorouter_owned.zones` - the only thing that makes it eligible for a
    later `modify_plane` call (the six hand-made kiln zones are never in
    that list, so they can never be auto-mutated).

    IMPORTANT: writing the zone block does not fill it with copper - KiCad
    only computes `filled_polygon` data on its own next "Fill All Zones" /
    save-triggered refill. Refill + re-run DRC in KiCad after write=True.
    """
    board_path, _, _ = _pcb._resolve_project_path(project_path)
    proposal = propose_plane(project_path, net, layer)
    layer = proposal["layer"]
    outline = [(p["x"], p["y"]) for p in proposal["outline"]]

    zones = _parse_zones_cached(board_path)
    shape = _zone_template_shape(zones)
    zone_uuid = str(_uuid.uuid4())
    zone_name = name or f"autorouter_{net.replace('/', '_')}_{layer}"
    block = _zone_block(net, layer, zone_name, zone_uuid, outline, shape, priority)

    result: dict[str, Any] = {
        "board_path": str(board_path),
        "write": write,
        "written": False,
        "net": net,
        "layer": layer,
        "name": zone_name,
        "uuid": zone_uuid,
        "priority": priority,
        "outline": proposal["outline"],
        "proposal": proposal,
        "block": block,
        "refill_required_note": (
            "The zone outline is written but NOT filled - refill zones (Fill All "
            "Zones) and re-run DRC in KiCad after write=True."
        ),
    }
    if write:
        _pcb._check_not_locked_by_editor(board_path, allow_while_open)
        text = _pcb._read_text(board_path)
        new_text = _pcb._append_top_level_block(text, block)
        with board_path.open("w", encoding="utf-8", newline="") as handle:
            handle.write(new_text)
        _pcb._invalidate_board_cache(board_path)

        state = _pcb.load_board_local(project_path)
        data = state["data"]
        data.setdefault("version", 1)
        owned = data.setdefault("autorouter_owned", {})
        owned.setdefault("zones", [])
        owned["zones"].append(zone_uuid)
        _pcb.save_board_local(project_path, data)
        result["written"] = True
    return result


def modify_plane(
    project_path: str | Path,
    uuid: str,
    new_outline: list[dict[str, float]] | list[tuple[float, float]] | None = None,
    priority: int | None = None,
    write: bool = False,
    allow_while_open: bool = False,
) -> dict[str, Any]:
    """Phase 7.5.5 - move/grow/shrink and/or reprioritize an EXISTING zone
    (MCP tool `modify_kicad_plane`) by replacing its `(polygon (pts ...))`
    block and/or its `(priority N)` line, via the same uuid-anchored s-expr
    surgery `delete_group`/`unroute_nets` use (locate the enclosing `(zone
    ...)` block by its uuid, splice only inside it - never a full
    re-serialize of the board file).

    REFUSES (raises `ValueError`, never silently proceeds) when `uuid` is
    not recorded in board-local `autorouter_owned.zones` - this tool may
    ONLY move/resize a zone `create_plane` itself created, never one of the
    six hand-made kiln zones (mainGnd, safty_gnd, antenna, main3.3, main12v,
    3.3v_safty). Those can only be *proposed* for change via `propose_plane`
    (read-only) for a human to review and apply by hand in KiCad.

    At least one of `new_outline`/`priority` must be given. `new_outline` is
    a list of `{x, y}` dicts or `(x, y)` tuples with at least 3 points.

    write=False (default) previews the new zone block text without
    touching the board. write=True applies it and reminds the caller that
    KiCad must refill zones + re-run DRC afterward - same as `create_plane`.
    """
    if new_outline is None and priority is None:
        raise ValueError("Provide new_outline and/or priority - nothing to modify")

    board_path, _, _ = _pcb._resolve_project_path(project_path)
    state = _pcb.load_board_local(project_path)
    data = state["data"]
    owned_zones = set((data.get("autorouter_owned", {}) or {}).get("zones", []) or [])
    if uuid not in owned_zones:
        raise ValueError(
            f"Zone {uuid!r} is not autorouter-owned (not in board-local "
            "autorouter_owned.zones) - modify_plane refuses to mutate a hand-made "
            "zone; use propose_plane to suggest a change for the user to review "
            "and apply in KiCad instead."
        )

    zones = _parse_zones_cached(board_path)
    target = next((z for z in zones if z["uuid"] == uuid), None)
    if target is None:
        raise KeyError(f"Zone uuid {uuid!r} not found on the board (board may have changed since last parse)")

    text = _pcb._read_text(board_path)
    marker = f'(uuid "{uuid}")'
    uidx = text.find(marker)
    if uidx == -1:
        raise ValueError("Zone uuid not found in board file text (board changed since last parse)")
    zone_start = text.rfind("(zone", 0, uidx)
    if zone_start == -1:
        raise ValueError("Could not locate the enclosing (zone ...) block")

    depth = 0
    zone_end = None
    for i in range(zone_start, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                zone_end = i + 1
                break
    if zone_end is None:
        raise ValueError("Unbalanced parentheses while locating end of (zone ...) block")

    zone_text = text[zone_start:zone_end]
    new_zone_text = zone_text

    new_outline_pts: list[tuple[float, float]] | None = None
    if new_outline is not None:
        new_outline_pts = [
            (float(p["x"]), float(p["y"])) if isinstance(p, dict) else (float(p[0]), float(p[1]))
            for p in new_outline
        ]
        if len(new_outline_pts) < 3:
            raise ValueError("new_outline must have at least 3 points")
        pts_str = " ".join(f'(xy {_fmt(x)} {_fmt(y)})' for x, y in new_outline_pts)
        poly_match = re.search(r"\(polygon\s*\(pts\b.*?\)\s*\)", new_zone_text, re.DOTALL)
        if poly_match is None:
            raise ValueError("Zone has no (polygon (pts ...)) block to replace")
        new_poly_block = f'(polygon\n\t\t\t(pts\n\t\t\t\t{pts_str}\n\t\t\t)\n\t\t)'
        new_zone_text = new_zone_text[: poly_match.start()] + new_poly_block + new_zone_text[poly_match.end():]

    if priority is not None:
        pr_match = re.search(r"\(priority\s+-?\d+\)", new_zone_text)
        if pr_match is not None:
            new_zone_text = (
                new_zone_text[: pr_match.start()] + f'(priority {int(priority)})' + new_zone_text[pr_match.end():]
            )
        else:
            name_match = re.search(r'\(name "[^"]*"\)\n?', new_zone_text)
            if name_match is not None:
                idx = name_match.end()
                new_zone_text = new_zone_text[:idx] + f'\t\t(priority {int(priority)})\n' + new_zone_text[idx:]
            else:
                new_zone_text = new_zone_text.replace(
                    "(zone", f"(zone\n\t\t(priority {int(priority)})", 1,
                )

    result: dict[str, Any] = {
        "board_path": str(board_path),
        "write": write,
        "written": False,
        "uuid": uuid,
        "net": target.get("net"),
        "layers": target.get("layers"),
        "new_outline": [{"x": x, "y": y} for x, y in new_outline_pts] if new_outline_pts else None,
        "new_priority": priority,
        "block": new_zone_text,
        "refill_required_note": (
            "Zone outline/priority is changed on disk only if write=True - KiCad "
            "must refill zones (Fill All Zones) and re-run DRC before this is "
            "reflected in copper."
        ),
    }
    if write:
        _pcb._check_not_locked_by_editor(board_path, allow_while_open)
        new_text = text[:zone_start] + new_zone_text + text[zone_end:]
        with board_path.open("w", encoding="utf-8", newline="") as handle:
            handle.write(new_text)
        _pcb._invalidate_board_cache(board_path)
        result["written"] = True
    return result


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
# NOTE (7.8): the numpy tier vectorizes the whole-window field and COULD afford a
# much larger fine-grid window before `_choose_grid` coarsens. We deliberately do
# NOT raise the budget for numpy: (1) the budget selects the grid, so an equal
# budget is what keeps cpu and numpy route-level bit-identical (parity); (2) it
# was measured NOT to help on kiln - 35/39 unrouted connections are walled by
# foreign copper at clearance and have NO DRC-legal corridor at any grid/window
# (cost-free BFS at 0.2 mm over a 60 mm-margin window connects only the same 4/39
# as the coarse grid). Kept documented here rather than shipped as a non-improving
# behavior change.


def _resolve_backend(settings: dict[str, Any]) -> str:
    """Resolve `autorouter.acceleration` to a concrete backend.

    numpy is a hard dependency (the accel module imports it unconditionally and
    the parity test exercises it every run), but it is NOT the default per-window
    pathfinder: the numpy wavefront relaxes the WHOLE window for many sweeps
    (Jacobi / Bellman-Ford), whereas the cpu A* is output-sensitive and only
    expands toward the goal. The detailed router runs many small windows, where
    A* is decisively faster, so "auto" resolves to "cpu". Choose "numpy"
    explicitly for the vectorized field backend (parity oracle / large-window
    experiments).

    `"gpu"` selects the CUDA tier (same wavefront kernel, device arrays). It is
    only ever chosen EXPLICITLY: a missing GPU is expected on most machines and
    must degrade silently to the numpy/cpu tiers rather than error, which the
    dispatch below does per call - so asking for `"gpu"` on a box without one
    still routes, just on the CPU."""
    accel = str(settings.get("autorouter", {}).get("acceleration", "auto")).lower()
    if accel in ("cpu", "numpy", "gpu"):
        return accel
    return "cpu"  # "auto"/"hybrid"/anything else -> output-sensitive A*


# Counters for GPU-tier demotions over the current process's routing work, so a
# run report can say how much of the board the GPU actually carried (7.8: "every
# demotion is counted, so 'the GPU helped 90% of this board' is visible rather
# than silent"). Reset by `gpu_tier_report()`.
_GPU_TIER_STATS: dict[str, Any] = {"on_gpu": 0, "demoted_no_device": 0,
                                   "demoted_oversized": 0, "demoted_oom": 0,
                                   "reason": None, "device": None}


def gpu_tier_report(reset: bool = False) -> dict[str, Any]:
    """Snapshot (optionally reset) the GPU tier's per-item demotion counters."""
    snap = dict(_GPU_TIER_STATS)
    if reset:
        _GPU_TIER_STATS.update({"on_gpu": 0, "demoted_no_device": 0,
                                "demoted_oversized": 0, "demoted_oom": 0,
                                "reason": None, "device": None})
    return snap


def _fine_search(backend: str, win: "_FineWindow", *args: Any, **kwargs: Any):
    """Dispatch one windowed detailed search to the selected backend. All three
    backends return byte-identical geometry (7.8 parity); `cpu` is the reference
    pure-Python A*, `numpy` the vectorized integer-field wavefront, `gpu` that
    same wavefront with device arrays.

    The GPU path is memory-planned and demotes rather than fails: if there is no
    usable CUDA array module, or this window's estimated device footprint exceeds
    `autorouter.gpu.memory_budget_mb` (0 = auto-probe free VRAM), or the allocator
    OOMs mid-search, the window falls back to the numpy tier and the demotion is
    counted in `_GPU_TIER_STATS`. Since every tier is bit-identical, a demotion
    changes only WHERE the work ran, never the answer."""
    # Popped unconditionally: it is dispatch metadata for the gpu tier, never an
    # argument of the search itself, so cpu/numpy must not see it.
    settings = kwargs.pop("_settings", None) or {}
    if backend == "gpu":
        import kicad_router_accel as _accel
        item = {"rows": win.rows, "cols": win.cols, "layers": len(win.layers)}
        results, report = _accel.run_windows(
            [item], settings,
            gpu_call=lambda _it, xp: _accel.fine_wavefront(win, *args, xp=xp, **kwargs),
            fallback_call=lambda _it: _accel.fine_wavefront(win, *args, **kwargs))
        for k in ("on_gpu", "demoted_no_device", "demoted_oversized", "demoted_oom"):
            _GPU_TIER_STATS[k] += report.get(k, 0)
        _GPU_TIER_STATS["reason"] = report.get("reason") or _GPU_TIER_STATS["reason"]
        _GPU_TIER_STATS["device"] = report.get("device") or _GPU_TIER_STATS["device"]
        return results[0]
    if backend == "numpy":
        import kicad_router_accel as _accel
        return _accel.fine_wavefront(win, *args, **kwargs)
    return _fine_astar(win, *args, **kwargs)

# Deterministic grid-growth factor used by `_choose_grid`'s refinement loop
# (see its docstring). Kept well below 2x so the chosen grid tracks the node
# budget closely instead of overshooting to a much coarser resolution than
# necessary.
_GRID_GROWTH_FACTOR = 1.05
_GRID_CHOICE_MAX_STEPS = 200


def _window_node_count(span_x: float, span_y: float, grid: float, n_layers: int) -> int:
    """Node count `_FineWindow` would allocate for a window of this span at
    this grid - mirrors `_FineWindow.__init__`'s `cols`/`rows` formula exactly
    (including its `ceil(...) + 1` and `max(2, ...)` floors) so the estimate
    used to CHOOSE a grid always matches what gets built."""
    cols = max(2, int(math.ceil(span_x / grid)) + 1)
    rows = max(2, int(math.ceil(span_y / grid)) + 1)
    return cols * rows * max(1, n_layers)


def _choose_grid(span_x: float, span_y: float, n_layers: int,
                  base_grid: float, max_grid: float, budget: int) -> float:
    """Adaptive detailed-grid selection (7.9 anchor): the coarsest-as-needed,
    fine-as-possible `grid_mm` for ONE connection's window, as a pure,
    deterministic function of its span/layer-count/budget only.

    - SHORT connections (window fits the budget at `base_grid`, the board's
      `autorouter.grid_mm`) get `base_grid` back UNCHANGED - this is what
      keeps every existing short-connection route byte-identical: the window
      building / A* / self-check code downstream never learns the grid was
      "chosen" adaptively when nothing needed to change.
    - LONG connections that would blow the node budget at `base_grid` are
      coarsened just enough to fit: an analytic lower bound
      (`sqrt(span_x * span_y * n_layers / budget)`), then refined UPWARD in
      small deterministic `_GRID_GROWTH_FACTOR` steps against the EXACT
      `_window_node_count` formula (the analytic bound can undershoot because
      of the `ceil(...) + 1` node-count floor at small col/row counts), never
      finer than `base_grid` and never coarser than `max_grid`
      (`autorouter.max_grid_mm`).
    - If even `max_grid` cannot fit the budget, `max_grid` is returned anyway;
      the caller's existing `_MAX_WINDOW_NODES` check (run against whatever
      grid comes back) is what turns that into the `window_too_large` failure
      - grid selection itself never fails.

    Same span/budget in => same grid out, always: no randomness, no
    board-global state, no iteration-order dependence.
    """
    base_grid = max(base_grid, 1e-6)
    max_grid = max(max_grid, base_grid)
    if _window_node_count(span_x, span_y, base_grid, n_layers) <= budget:
        return base_grid
    needed = math.sqrt(max(span_x, 1e-9) * max(span_y, 1e-9) * max(1, n_layers) / budget)
    grid = min(max(needed, base_grid), max_grid)
    steps = 0
    while (grid < max_grid
           and _window_node_count(span_x, span_y, grid, n_layers) > budget
           and steps < _GRID_CHOICE_MAX_STEPS):
        grid = min(grid * _GRID_GROWTH_FACTOR, max_grid)
        steps += 1
    return grid


# Tight window (mm) used by the escape-refinement attempts so the FINEST grid
# fits a small window even when the base-margin window is too big for it - a
# clearance-sealed pad breakout in a dense pin field needs a fine grid, not room.
_ESCAPE_MARGIN_MM = 3.0


def _route_attempts(
    from_xy: tuple[float, float], to_xy: tuple[float, float],
    board_bbox: tuple[float, float, float, float], base_grid: float,
    min_grid: float, max_grid: float, base_margin: float, n_layers: int,
    budget: int,
) -> list[tuple[float, float]]:
    """Ordered `(margin_mm, grid_mm)` attempts for one connection's windowed
    detailed search.

    The FIRST attempt is EXACTLY the legacy `(base_margin, adaptive-grid)` pair,
    so every connection that already routes on attempt 1 is byte-identical - the
    whole ladder below it only runs on FAILURE. The ladder interleaves two
    orthogonal escapes from an `unreachable_in_window`:
      * FINER grids (down to `min_grid`) - the real fix for a pad escape sealed
        off by clearance inflation in a dense pin field: at 0.2 mm no grid node
        lands in the sub-0.2 mm channel a hand route threads; a 0.1/0.05 mm grid
        puts a node there. This is why widening the margin alone (the old loop)
        never helped these - the block is LOCAL to the pad, not a lack of room.
      * WIDER margins - the classic detour-room escape for a genuinely boxed
        route, unchanged from before.
    Every candidate whose node*layer count would exceed `budget` is dropped, so
    a finer grid is only ever tried at a margin small enough to afford it (hence
    the tight `_ESCAPE_MARGIN_MM` attempts, which let `min_grid` fit even when
    the base-margin window cannot). Pure function of its inputs; deterministic."""
    min_grid = max(min_grid, 1e-6)
    base_grid = max(base_grid, min_grid)
    max_grid = max(max_grid, base_grid)

    def span(margin: float) -> tuple[float, float]:
        minx = max(min(from_xy[0], to_xy[0]) - margin, board_bbox[0] - base_grid)
        miny = max(min(from_xy[1], to_xy[1]) - margin, board_bbox[1] - base_grid)
        maxx = min(max(from_xy[0], to_xy[0]) + margin, board_bbox[2] + base_grid)
        maxy = min(max(from_xy[1], to_xy[1]) + margin, board_bbox[3] + base_grid)
        return maxx - minx, maxy - miny

    # margin schedule: base, 2x, 4x, ... up to the cap (the legacy doubling).
    margins = [base_margin]
    while margins[-1] < _MAX_WINDOW_SPAN_MM:
        margins.append(min(margins[-1] * 2.0, _MAX_WINDOW_SPAN_MM))

    attempts: list[tuple[float, float]] = []

    def adaptive_at(margin: float) -> tuple[float, float]:
        sx, sy = span(margin)
        return (round(margin, 6), round(_choose_grid(sx, sy, n_layers, base_grid, max_grid, budget), 6))

    def fine_at(margin: float) -> list[tuple[float, float]]:
        """Grids FINER than base, down to `min_grid`, that fit `budget` at this
        margin's span. Returns [] when none fit (e.g. a long net whose window is
        large even at the tight margin) - so a fine grid is only ever tried where
        it is cheap, and a failed fine search never explores a huge window."""
        sx, sy = span(margin)
        out: list[tuple[float, float]] = []
        g = base_grid
        while g > min_grid + 1e-9:
            g = max(g / 2.0, min_grid)
            if _window_node_count(sx, sy, g, n_layers) <= budget:
                out.append((round(margin, 6), round(g, 6)))
            else:
                break
        return out

    # 1) legacy attempt 1 first (parity): base margin, adaptive grid.
    attempts.append(adaptive_at(base_margin))
    # 2) FINE-grid escapes at a TIGHT window - the dense-pad-breakout workhorse,
    #    cheap because the window is small (and auto-skipped for long nets whose
    #    window can't be small: fine_at returns [] when nothing fits the budget).
    attempts.extend(fine_at(_ESCAPE_MARGIN_MM))
    # 3) the classic wider-margin COARSE ladder (detour room for a boxed route).
    for m in margins[1:]:
        attempts.append(adaptive_at(m))

    seen: set[tuple[float, float]] = set()
    ordered: list[tuple[float, float]] = []
    for a in attempts:
        if a not in seen:
            seen.add(a)
            ordered.append(a)
    return ordered


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
                 "raster", "pts", "minx", "miny", "maxx", "maxy", "is_edge", "owner",
                 "via_transparent", "is_pad", "uuid")

    def __init__(self, kind: str, net: str, layers: frozenset[str], half: float,
                 x1: float, y1: float, x2: float, y2: float,
                 raster: "_FillRaster | None" = None, is_edge: bool = False,
                 pts: list[tuple[float, float]] | None = None,
                 owner: int | None = None, via_transparent: bool = False,
                 is_pad: bool = False, uuid: str = "") -> None:
        self.kind = kind      # "seg" | "pt" | "zone" | "edge"
        self.net = net
        self.layers = layers
        self.half = half
        # is_pad: True only for a footprint-pad "pt" obstacle (as opposed to a
        # via, which is also kind "pt"). Needed to keep pads OUT of the
        # hand-copper rip-up scope (a pad is a component pin, not routed
        # copper - see `_is_hand_copper_obstacle`). uuid: the board-file
        # `(uuid ...)` of the underlying (segment)/(arc)/(via) block, when
        # known - lets a rippable hand-copper obstacle be identified back to
        # its exact board block for `_delete_blocks_by_uuid` removal. Empty
        # for zone/edge/pad obstacles and for autorouter-emitted obstacles
        # (those are ripped by owner id, not uuid; see `_obstacles_from_emit`).
        self.is_pad = is_pad
        self.uuid = uuid
        # via_transparent: a foreign POWER/GND plane fill yields an ANTI-PAD around
        # a via that crosses it, so it does NOT block a via by itself (it still
        # blocks a same-layer TRACK). Set only for power/gnd zone fills - the
        # physical reality that lets a signal net via through a plane, and the
        # unlock for kiln's cross-layer control bus (see the plane-via findings in
        # NETCLASS_PLAN.md). A real anti-pad is cut by KiCad on zone refill after
        # the via is written, so writes MUST refill for DRC-clean output.
        self.via_transparent = via_transparent
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
                       edge_clearance: float,
                       power_patterns: list[str] | None = None) -> list["_Obst"]:
    """Every copper item on the board as an `_Obst` (segments/arcs, vias, pads,
    foreign zone fills) plus Edge.Cuts segments. Built once per board; the
    per-connection window filters this list by bbox.

    A foreign POWER/GND zone fill is tagged `via_transparent` (per
    `power_patterns`): it blocks same-layer tracks but yields an anti-pad to a via
    crossing it, so a signal net can via through a plane (see NETCLASS_PLAN.md
    plane-via findings)."""
    tracks = _pcb._parse_tracks_cached(board_path)
    footprints = _pcb._parse_footprint_pads_cached(board_path)
    fills = _zone_fill_index_cached(board_path)
    stack = {name: i for i, name in enumerate(all_cu)}
    obs: list[_Obst] = []

    for seg in tracks["segments"] + tracks["arcs"]:
        if seg["layer"] not in routable:
            continue
        obs.append(_Obst("seg", seg["net"], frozenset([seg["layer"]]), seg["width"] / 2.0,
                         seg["start"]["x"], seg["start"]["y"], seg["end"]["x"], seg["end"]["y"],
                         uuid=seg.get("uuid", "")))
    for via in tracks["vias"]:
        at = via["at"]
        layers = _via_layer_set(via, stack, all_cu)
        layers = frozenset(l for l in layers if l in routable) or frozenset(
            l for l in via.get("layers", []) if l in routable)
        obs.append(_Obst("pt", via["net"], layers, via.get("size", 0.6) / 2.0,
                         at["x"], at["y"], at["x"], at["y"], uuid=via.get("uuid", "")))
    for fp in footprints.values():
        for pad in fp["pads"]:
            layers = frozenset(l for l in _pad_layer_set(pad, all_cu) if l in routable)
            if not layers:
                continue
            pos = pad["position"]
            obs.append(_Obst("pt", pad.get("net", ""), layers, _pad_reach(pad),
                             pos["x"], pos["y"], pos["x"], pos["y"], is_pad=True))
    for net_name, fill_list in fills.items():
        is_plane = _pcb._net_kind(net_name, None, power_patterns) == "power"
        for zf in fill_list:
            if zf["layer"] not in routable:
                continue
            obs.append(_Obst("zone", net_name, frozenset([zf["layer"]]), 0.0,
                             zf["pts"][0][0], zf["pts"][0][1], zf["pts"][0][0], zf["pts"][0][1],
                             raster=zf.get("raster"), pts=zf["pts"],
                             via_transparent=is_plane))
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


def _min_dist_to_edges_ref(px: float, py: float,
                           edges: list[tuple[float, float, float, float]]) -> float:
    """Reference (linear-scan) min point-to-edge distance. O(len(edges)) per
    call; kept byte-for-byte as the correctness oracle for
    `_ZoneEdgeGrid.min_dist` - see test_zone_distance_perf.py."""
    best = math.inf
    for (x1, y1, x2, y2) in edges:
        d = _dist_point_segment(px, py, x1, y1, x2, y2)
        if d < best:
            best = d
    return best


# Back-compat alias (kept in case anything outside this module imports the
# old name directly); the hot path below uses `_ZoneEdgeGrid` instead.
_min_dist_to_edges = _min_dist_to_edges_ref


class _ZoneEdgeGrid:
    """Uniform-grid spatial index over a window's already-clipped zone edges.

    `obstacle_cells` already restricts `zedges` to the edges whose bbox
    intersects the window+halo (`_clip_polygon_edges`), but a zone fill can
    still contribute hundreds of nearby edges (thermal-relief cutouts,
    serpentine pour boundaries) and every grid cell in the window used to be
    tested against every one of them - O(cells x zone_edges). This buckets
    those edges into cells of side `reach` (the largest distance threshold any
    query in this window will ever compare against) so a query point only
    needs to scan the single bucket its own cell falls in.

    Correctness argument (why one bucket is always enough, not neighbors too):
    each edge is inserted into every bucket that its bounding box, padded by
    `reach` on all sides, overlaps. If the true distance from query point P to
    edge E is <= reach, then P lies within E's bbox padded by `reach` (the
    closest point of E to P is inside E's bbox, and P is within `reach` of
    it). P's own bucket cell therefore overlaps that padded bbox (they share
    the point P), so E was necessarily inserted into P's bucket during
    construction. Hence any edge that could be <= reach from P is present in
    P's single bucket - no neighbor-bucket scan is needed. Edges farther than
    `reach` may or may not appear in a bucket (an over-approximation is fine);
    they are still tested exactly if present, so distances returned are exact
    whenever they are < reach, which is all obstacle_cells ever compares
    against."""

    __slots__ = ("cell", "inv", "minx", "miny", "buckets")

    def __init__(self, edges: list[tuple[float, float, float, float]], reach: float) -> None:
        cell = max(reach, 1e-6)
        self.cell = cell
        self.inv = 1.0 / cell
        if edges:
            self.minx = min(min(x1, x2) for (x1, y1, x2, y2) in edges)
            self.miny = min(min(y1, y2) for (x1, y1, x2, y2) in edges)
        else:
            self.minx = self.miny = 0.0
        buckets: dict[tuple[int, int], list[tuple[float, float, float, float]]] = {}
        inv = self.inv
        minx, miny = self.minx, self.miny
        for (x1, y1, x2, y2) in edges:
            bx0 = int(math.floor((min(x1, x2) - cell - minx) * inv))
            bx1 = int(math.floor((max(x1, x2) + cell - minx) * inv))
            by0 = int(math.floor((min(y1, y2) - cell - miny) * inv))
            by1 = int(math.floor((max(y1, y2) + cell - miny) * inv))
            for by in range(by0, by1 + 1):
                for bx in range(bx0, bx1 + 1):
                    buckets.setdefault((bx, by), []).append((x1, y1, x2, y2))
        self.buckets = buckets

    def min_dist(self, px: float, py: float) -> float:
        bx = int(math.floor((px - self.minx) * self.inv))
        by = int(math.floor((py - self.miny) * self.inv))
        edges = self.buckets.get((bx, by))
        if not edges:
            return math.inf
        best = math.inf
        for (x1, y1, x2, y2) in edges:
            d = _dist_point_segment(px, py, x1, y1, x2, y2)
            if d < best:
                best = d
        return best


class _ObstacleIndex:
    """Uniform-grid spatial index over `_Obst`s — the cell->obstacle direction.

    The eager `_FineWindow.build` walks obstacle->cells (rasterizing every
    obstacle's inflated bbox into the window), which costs O(total inflated
    obstacle area / grid^2) REGARDLESS of what the search then looks at. A
    board-spanning plane fill at a fine grid over a wide window therefore
    dominates, which is exactly what `_MAX_WINDOW_NODES` was capping. This index
    inverts the loop so a LAZY window can answer "is this one cell blocked?"
    without ever materializing the rest — making window build cost independent
    of window size and search cost output-sensitive again (see `_LazyBlockedSet`).

    Correctness (the same argument `_ZoneEdgeGrid` documents, restated for
    obstacles): each obstacle is inserted into every bucket its bounding box
    PADDED BY ITS OWN `reach` overlaps. If a query point P is within `reach` of
    obstacle O, then P lies inside O's padded bbox; P's own bucket cell contains
    P, so that cell overlaps the padded bbox and O was necessarily inserted
    there. One bucket lookup is therefore complete — no neighbour scan. Note the
    argument never constrains the BUCKET SIZE (it is a pure performance knob) and
    allows a DIFFERENT `reach` per obstacle, unlike `_ZoneEdgeGrid`'s single
    window-wide reach.

    Obstacles farther than `reach` may also appear in a bucket (a harmless
    over-approximation); every candidate is still tested exactly by the caller.
    Query order is insertion order, so results are deterministic."""

    __slots__ = ("cell", "inv", "minx", "miny", "buckets", "reach_by_id", "removed")

    def __init__(self, entries: "list[tuple[_Obst, float]]", cell: float,
                 origin: tuple[float, float]) -> None:
        self.cell = max(cell, 1e-6)
        self.inv = 1.0 / self.cell
        self.minx, self.miny = origin
        self.buckets: dict[tuple[int, int], list[_Obst]] = {}
        self.reach_by_id: dict[int, float] = {}
        # ids of obstacles removed after construction (step-4 rip-up). Filtered
        # at query time so a removal never has to rebuild the buckets.
        self.removed: set[int] = set()
        for ob, reach in entries:
            self.add(ob, reach)

    def add(self, ob: "_Obst", reach: float) -> None:
        self.removed.discard(id(ob))
        if id(ob) in self.reach_by_id:
            return  # already indexed at its (identical) reach
        self.reach_by_id[id(ob)] = reach
        inv, minx, miny = self.inv, self.minx, self.miny
        bx0 = int(math.floor((ob.minx - reach - minx) * inv))
        bx1 = int(math.floor((ob.maxx + reach - minx) * inv))
        by0 = int(math.floor((ob.miny - reach - miny) * inv))
        by1 = int(math.floor((ob.maxy + reach - miny) * inv))
        buckets = self.buckets
        for by in range(by0, by1 + 1):
            for bx in range(bx0, bx1 + 1):
                buckets.setdefault((bx, by), []).append(ob)

    def remove(self, ob: "_Obst") -> None:
        self.removed.add(id(ob))

    def query(self, px: float, py: float) -> "list[_Obst]":
        bx = int(math.floor((px - self.minx) * self.inv))
        by = int(math.floor((py - self.miny) * self.inv))
        obs = self.buckets.get((bx, by))
        if not obs:
            return []
        if not self.removed:
            return obs
        rm = self.removed
        return [ob for ob in obs if id(ob) not in rm]


class _LazyBlockedSet:
    """Set-like view of ONE `_FineWindow` blocked layer (or its via layer) whose
    membership is computed per cell on demand and memoized.

    Implements exactly the read surface the searches use — `cell in s` (both A*
    backends, `nearest_free`) and iteration (`kicad_router_accel` materializing
    numpy arrays) — so a lazy window is a drop-in for an eagerly-rasterized one.
    Membership is decided by the SAME predicate `_FineWindow.obstacle_cells`
    applies, just evaluated cell-first, which is what makes a lazy window's
    blocked sets EQUAL an eager window's cell for cell (proven directly in
    `tests/test_lazy_window.py`) — the parity guarantee this tier rests on.

    Iterating forces a full cols x rows evaluation (i.e. forfeits the laziness);
    that is deliberate and only happens on the numpy backend, which needs dense
    arrays anyway. The cpu A* never iterates."""

    __slots__ = ("_win", "_layer", "_cache")

    def __init__(self, win: "_FineWindow", layer: str | None) -> None:
        self._win = win
        self._layer = layer  # None => the via set
        self._cache: dict[tuple[int, int], bool] = {}

    def __contains__(self, cell: object) -> bool:
        c = self._cache.get(cell)  # type: ignore[arg-type]
        if c is None:
            c = self._win._lazy_cell_blocked(cell, self._layer)  # type: ignore[arg-type]
            self._cache[cell] = c  # type: ignore[index]
        return c

    def __iter__(self):
        win = self._win
        for iy in range(win.rows):
            for ix in range(win.cols):
                if (ix, iy) in self:
                    yield (ix, iy)

    def __len__(self) -> int:
        return sum(1 for _ in self)

    def __eq__(self, other: object) -> bool:
        """Compare EQUAL to the plain `set` an eager window would have built.

        Without this, `lazy_window.blocked_via == set()` would silently be False
        by object identity - a subtle trap for any future caller that reaches for
        a set comparison. Materializes, like iteration does."""
        if isinstance(other, (set, frozenset)):
            return set(self) == set(other)
        if isinstance(other, _LazyBlockedSet):
            return set(self) == set(other)
        return NotImplemented

    __hash__ = None  # type: ignore[assignment]  # mutable view, like `set`

    def invalidate(self) -> None:
        self._cache.clear()


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
                 "_clearance", "_edge_clearance", "_zone_cache",
                 "_lazy", "_index", "_zgrids")

    def __init__(self, minx: float, miny: float, maxx: float, maxy: float, grid: float,
                 layers: list[str], layer_types: dict[str, str], net: str,
                 lazy: bool = False) -> None:
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
        # LAZY mode (M5 whole-board windowing): `build` indexes obstacles instead
        # of rasterizing them and the two blocked views below are replaced by
        # `_LazyBlockedSet`s that decide membership per cell on demand. This
        # decouples build cost from window AREA, which is what lets a window span
        # the whole board at a fine grid (see `_route_wide_lazy`). Default False
        # so every existing window is byte-identical eager behaviour.
        self._lazy = bool(lazy)
        self._index: _ObstacleIndex | None = None
        self._zgrids: dict[int, _ZoneEdgeGrid] = {}
        self.blocked_track: dict[str, Any] = (
            {l: _LazyBlockedSet(self, l) for l in layers} if self._lazy
            else {l: set() for l in layers})
        self.blocked_via: Any = _LazyBlockedSet(self, None) if self._lazy else set()
        self._track_cnt: dict[str, dict[tuple[int, int], int]] = {l: {} for l in layers}
        self._via_cnt: dict[tuple[int, int], int] = {}
        self._track_half = 0.0
        self._via_radius = 0.0
        self._clearance = 0.0
        self._edge_clearance = 0.0
        # Optional {id(ob): _ZoneEdgeGrid} set by `_route_one` before `build`,
        # precomputed ONCE per connection at a bbox+reach bound wide enough to
        # cover every ladder rung (see `_build_zone_edge_cache`). None for any
        # window built outside that ladder (e.g. incremental rip-up), which
        # keeps building its own per-call zgrid exactly as before.
        self._zone_cache = None

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
        # A via_transparent obstacle (a power/gnd plane) yields an anti-pad to a
        # crossing via, so it blocks TRACKS but never VIAS.
        block_via = not ob.via_transparent
        ob_layers = [l for l in ob.layers if l in self.blocked_track]
        for l in ob_layers:
            track_cells.setdefault(l, set())
        is_zone = ob.kind == "zone" and bool(ob.pts)
        zgrid: _ZoneEdgeGrid | None = None
        if is_zone:
            cached = self._zone_cache.get(id(ob)) if self._zone_cache is not None else None
            if cached is not None:
                # Reuse the per-connection grid built once at a bbox+reach
                # bound that upper-bounds every ladder rung (`_build_zone_edge_
                # cache`): it was built with a reach R >= this call's `big`, and
                # `_ZoneEdgeGrid.min_dist` is exact for any query threshold <=
                # the reach it was built with, so this is identical to building
                # fresh here - just skips the re-clip + re-bucket every rung.
                zgrid = cached
            else:
                zedges = _clip_polygon_edges(
                    ob.pts, wminx - big, wminy - big, wmaxx + big, wmaxy + big)
                # `big` bounds every distance this window will ever compare a zone
                # edge against below (via_reach, track_reach <= big), so a grid
                # bucketed at that size lets each cell test only its own bucket -
                # see `_ZoneEdgeGrid` for the exactness argument.
                zgrid = _ZoneEdgeGrid(zedges, big)
        for iy in range(iy0, iy1 + 1):
            for ix in range(ix0, ix1 + 1):
                px, py = self.node_xy(ix, iy)
                if is_zone:
                    assert ob.raster is not None
                    inside = ob.raster.covers(px, py, 0.0)
                    dmin = 0.0 if inside else zgrid.min_dist(px, py)
                    if block_via and dmin < via_reach:
                        via_cells.add((ix, iy))
                    if dmin < track_reach:
                        for l in ob_layers:
                            track_cells[l].add((ix, iy))
                    continue
                if block_via and ob.point_within(px, py, via_reach):
                    via_cells.add((ix, iy))
                if ob.point_within(px, py, track_reach):
                    for l in ob_layers:
                        track_cells[l].add((ix, iy))
        return via_cells, track_cells

    # ---------------- lazy (on-demand) obstacle evaluation ------------------ #
    #
    # Everything below mirrors `obstacle_cells` EXACTLY, evaluated cell-first
    # instead of obstacle-first. Any divergence would be a parity bug, so the
    # branch structure is kept deliberately parallel to that method and
    # `tests/test_lazy_window.py` asserts set equality cell-for-cell.

    def _ob_reach(self, ob: "_Obst") -> tuple[float, float, float]:
        """(track_reach, via_reach, big) for one obstacle — the same three
        quantities `obstacle_cells` computes."""
        cl = self._edge_clearance if ob.is_edge else self._clearance
        margin = self.grid * _FINE_CELL_MARGIN_FRAC
        track_reach = self._track_half + cl + ob.half + margin
        via_reach = self._via_radius + cl + ob.half + margin
        return track_reach, via_reach, max(track_reach, via_reach)

    def _window_rejects(self, ob: "_Obst") -> bool:
        """`obstacle_cells`'s window-level bbox early-out, verbatim."""
        g = self.grid
        wminx = self.minx - g
        wminy = self.miny - g
        wmaxx = self.minx + (self.cols - 1) * g + g
        wmaxy = self.miny + (self.rows - 1) * g + g
        reach = (max(self._track_half, self._via_radius)
                 + max(self._clearance, self._edge_clearance) + ob.half
                 + g * _FINE_CELL_MARGIN_FRAC)
        return (ob.maxx < wminx - reach or ob.minx > wmaxx + reach
                or ob.maxy < wminy - reach or ob.miny > wmaxy + reach)

    def _zgrid_for(self, ob: "_Obst", big: float) -> "_ZoneEdgeGrid":
        cached = self._zone_cache.get(id(ob)) if self._zone_cache is not None else None
        if cached is not None:
            return cached
        zg = self._zgrids.get(id(ob))
        if zg is None:
            g = self.grid
            wminx, wminy = self.minx - g, self.miny - g
            wmaxx = self.minx + (self.cols - 1) * g + g
            wmaxy = self.miny + (self.rows - 1) * g + g
            zedges = _clip_polygon_edges(
                ob.pts, wminx - big, wminy - big, wmaxx + big, wmaxy + big)
            zg = _ZoneEdgeGrid(zedges, big)
            self._zgrids[id(ob)] = zg
        return zg

    def _lazy_build(self, obstacles: list["_Obst"]) -> None:
        """Index the obstacles this window could possibly be blocked by, instead
        of rasterizing them. Cost is O(obstacles x buckets-each-spans), which is
        independent of the fine grid — the whole point of the tier."""
        entries: list[tuple[_Obst, float]] = []
        for ob in obstacles:
            if ob.net == self.net and not ob.is_edge:
                continue  # same-net copper is free (obstacle_cells' first line)
            if self._window_rejects(ob):
                continue
            entries.append((ob, self._ob_reach(ob)[2]))
        # Bucket side: a few fine cells wide, floored so a tiny grid does not
        # explode the bucket count on a board-spanning obstacle. Perf only —
        # correctness is independent of this value (see `_ObstacleIndex`).
        cell = max(self.grid * 8.0, 2.0)
        self._index = _ObstacleIndex(entries, cell, (self.minx, self.miny))
        for l in self.layers:
            self.blocked_track[l].invalidate()
        self.blocked_via.invalidate()

    def _lazy_cell_blocked(self, cell: tuple[int, int], layer: str | None) -> bool:
        """Is grid node `cell` blocked for a track on `layer` (or for a VIA when
        `layer is None`)? The cell-first form of `obstacle_cells`' inner loop."""
        idx = self._index
        if idx is None:
            return False
        ix, iy = cell
        px, py = self.node_xy(ix, iy)
        for ob in idx.query(px, py):
            if layer is None:
                # `obstacle_cells` adds via cells WITHOUT consulting ob.layers.
                if ob.via_transparent:
                    continue
            elif layer not in ob.layers:
                continue
            track_reach, via_reach, big = self._ob_reach(ob)
            need = via_reach if layer is None else track_reach
            if ob.kind == "zone" and ob.pts:
                assert ob.raster is not None
                dmin = 0.0 if ob.raster.covers(px, py, 0.0) else self._zgrid_for(ob, big).min_dist(px, py)
                if dmin < need:
                    return True
            elif ob.point_within(px, py, need):
                return True
        return False

    def _lazy_invalidate(self) -> None:
        for l in self.layers:
            self.blocked_track[l].invalidate()
        self.blocked_via.invalidate()

    def add_obstacle(self, ob: "_Obst") -> None:
        if self._lazy:
            if not (ob.net == self.net and not ob.is_edge) and not self._window_rejects(ob):
                if self._index is None:
                    self._index = _ObstacleIndex([], max(self.grid * 8.0, 2.0),
                                                 (self.minx, self.miny))
                self._index.add(ob, self._ob_reach(ob)[2])
            self._lazy_invalidate()
            return
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
        if self._lazy:
            if self._index is not None:
                self._index.remove(ob)
            self._lazy_invalidate()
            return
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
        if self._lazy:
            self._lazy_build(obstacles)
            return
        for ob in obstacles:
            self.add_obstacle(ob)

    def nearest_free(self, x: float, y: float, layers: list[str], max_ring: int = 6,
                     toward_xy: tuple[float, float] | None = None
                     ) -> tuple[int, int] | None:
        """Nearest grid node (spiral out) not track-blocked on at least one of
        `layers` - the pad-escape landing node.

        `toward_xy` (Phase 7.3d, `autorouter.pad_escape_direction_aware`): the
        OTHER endpoint of the connection this escape belongs to (`to_xy` when
        escaping the start pad, `from_xy` when escaping the goal pad). When
        given AND the winning ring has more than one free-layer candidate, the
        pure-nearest tie-break below is replaced by a directional one: pick
        the candidate whose vector from `(x, y)` has the largest dot product
        with the unit direction toward `toward_xy` - i.e. "escape toward where
        you're going" instead of "escape to the closest open spot," which on a
        dense pin field can land on the far side of the pad and force the fine
        A* to route back around it. Passing None (the default) - or a ring
        with only one free candidate, the common uncongested-board case -
        reproduces the pre-7.3d behavior exactly; callers gate `toward_xy` on
        the settings flag so leaving it off is byte-identical to today."""
        cx, cy = self.cell_of(x, y)
        for ring in range(max_ring + 1):
            best: tuple[float, tuple[int, int]] | None = None
            biased: list[tuple[int, int]] | None = [] if toward_xy is not None else None
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
                        if biased is not None:
                            biased.append((ix, iy))
            if best is not None:
                if biased is not None and len(biased) > 1:
                    tvx, tvy = toward_xy[0] - x, toward_xy[1] - y
                    tlen = math.hypot(tvx, tvy)
                    if tlen > 1e-9:
                        tvx, tvy = tvx / tlen, tvy / tlen

                        def _toward_score(cell: tuple[int, int]) -> float:
                            nx, ny = self.node_xy(*cell)
                            return (nx - x) * tvx + (ny - y) * tvy

                        return max(biased, key=_toward_score)
                return best[1]
        return None


# --------------------------------------------------------------------------- #
# Fine detailed search - shared integer-milli cost model + deterministic
# field backtrace (used identically by the cpu A*, the numpy wavefront tier,
# and the reconstruction, so every backend is bit-identical by construction).
# --------------------------------------------------------------------------- #

# Sentinel "unreachable" cost for the integer cost field (well below int64 max
# so accumulating a few relaxation sweeps onto it can never overflow or wrap).
_FINE_INF = 1 << 60

# Phase 7.18.3: hard floor (integer milli) on a return-path-DISCOUNTED via, so
# an aggressively-tuned `plane.return_path_bonus` can never make a layer change
# free or negative. Only ever consulted on the discounted branch of `via`.
_MIN_VIA_MILLI = 1

# Phase 7.19.1 DIAGNOSTIC counters for the most recent `_fine_astar` call:
# `expansions` = A* states expanded, `field_expansions` = cells the backward
# goal-distance wavefront settled. Written unconditionally, read only by tests
# and reports - nothing in the router branches on them, so they cannot affect
# determinism (and are meaningless under the multi-process pool, where each
# worker has its own copy).
_FINE_SEARCH_STATS: dict[str, int] = {"expansions": 0, "field_expansions": 0}


# --------------------------------------------------------------------------- #
# Phase 7.19.1 - obstacle-aware goal-distance field (the fine A*'s heuristic).
# --------------------------------------------------------------------------- #

class _GoalDistanceField:
    """Backward (goal-rooted) cost-to-go LOWER BOUND for the fine detailed A*.

    WHAT IT IS. A Dijkstra wavefront run BACKWARD from the goal over a
    deliberately RELAXED version of the very problem the forward A* solves:

      * state space collapsed from (cell, layer, heading) to (cell) - the layer
        and the incoming heading are simply dropped;
      * a cell is passable if it is unblocked on ANY routable layer (the real
        search must be unblocked on the layer it is actually travelling, which
        is a strictly stronger requirement);
      * every move costs `floor(dist_units x unit_milli)`, where `unit_milli` is
        a hard floor on what one distance-unit of ANY planar move can cost (see
        `unit_floor_milli` below);
      * via moves cost 0 (a layer change does not move the cell, so it cannot
        change this field's value at all).

    Because every one of those is a relaxation - more edges, never-larger edge
    costs - the resulting distance is <= the true optimal fine cost from the same
    cell to the goal. That makes it ADMISSIBLE. It is also CONSISTENT: it is a
    shortest-path metric on a graph whose edges are a superset of the real moves'
    projections with costs <= the real ones, so `h(a) <= cost(a->b) + h(b)` holds
    for every real planar move, and for a via move `h` does not change at all
    while the move costs >= 0.

    WHY IT BEATS OCTILE. Plain octile distance is this same field with all
    obstacles deleted, so this field DOMINATES octile everywhere (obstacles can
    only lengthen a shortest path, never shorten it) - and dominates it a lot
    exactly where octile is worst: a goal behind a wall, in a pocket, or reachable
    only the long way round. Cells the field proves unreachable get `_FINE_INF`,
    which prunes them outright.

    WHY NOT THE 7.3a COARSE GRID (deviation from the phase's original sketch,
    recorded deliberately). `_CoarseModel`'s per-cell values are a CAPACITY /
    CONGESTION model, not a lower bound on fine cost: a coarse cell is "capacity
    0" when a foreign zone-fill covers its CENTRE, which says nothing about
    whether the fine grid can pass through the rest of that 2 mm cell, and its
    `congestion` term ADDS cost the fine model may not charge. A field built on
    those weights can therefore OVERSTATE the true fine cost - i.e. be
    inadmissible - and inadmissibility here is not a small error: it silently
    changes which path is returned. The relaxation above is built from the
    window's OWN blocked sets (literally the obstacle model the fine A* enforces)
    so admissibility is a property of the construction rather than a hope. See
    the phase report for the worked argument.

    LAZINESS. The wavefront is expanded on demand (`value()` settles just far
    enough to answer), so a search that terminates early never pays for the rest
    of the window. Settled values are final (ordinary Dijkstra), so repeated
    queries are O(1) once settled."""

    __slots__ = ("_win", "_dist", "_heap", "_settled", "_unit", "_diag",
                 "_straight", "_layers", "_goal_cells", "_goal_set",
                 "_exhausted", "expansions")

    def __init__(self, win: "_FineWindow", goal_cells: "list[tuple[int, int]]",
                 unit_milli: int) -> None:
        self._win = win
        self._layers = list(win.layers)
        self._unit = max(0, int(unit_milli))
        # Per-edge FLOOR (never a round-half-up) so a summed field can never
        # creep above the true cost through rounding - see `unit_floor_milli`.
        self._straight = self._unit
        self._diag = int(math.floor(self._unit * _SQRT2))
        self._dist: dict[tuple[int, int], int] = {}
        self._settled: set[tuple[int, int]] = set()
        self._heap: list[tuple[int, int, int]] = []
        self._goal_cells = list(goal_cells)
        self._goal_set = set(self._goal_cells)
        self._exhausted = False
        self.expansions = 0
        for (gx, gy) in self._goal_cells:
            if win.in_bounds(gx, gy) and self._dist.get((gx, gy), 1) != 0:
                self._dist[(gx, gy)] = 0
                heapq.heappush(self._heap, (0, gx, gy))

    def _enterable(self, ix: int, iy: int) -> bool:
        """Can a planar move LAND here? Unblocked on at least one routable layer
        (the relaxation - the real move must be unblocked on the layer it is
        actually travelling), or the goal cell, which `planar` always lets a
        move enter.

        Note the asymmetry this encodes, and why it matters: `planar` gates the
        DESTINATION of a move, never its source, so a blocked cell still has a
        finite cost-to-go (a route may start on one - `nearest_free` can hand
        the search a blocked start node) but may never be travelled THROUGH.
        The backward wavefront therefore tests enterability on the cell it is
        expanding FROM, and lets any neighbour be relaxed as a source."""
        if (ix, iy) in self._goal_set:
            return True
        for layer in self._layers:
            blocked = self._win.blocked_track.get(layer)
            if blocked is None or (ix, iy) not in blocked:
                return True
        return False

    def value(self, ix: int, iy: int) -> int:
        """Lower bound on the cost of any legal fine route from (ix, iy) to the
        goal. `_FINE_INF` when the relaxed problem itself has no route (which
        proves the real one has none either)."""
        if (ix, iy) in self._settled:
            return self._dist[(ix, iy)]
        while self._heap:
            d, cx, cy = heapq.heappop(self._heap)
            if (cx, cy) in self._settled:
                continue
            if d != self._dist.get((cx, cy)):
                continue
            self._settled.add((cx, cy))
            self.expansions += 1
            # Relax BEFORE answering: the wavefront is resumable, so a cell must
            # never be settled without its neighbours having been offered - an
            # early return here would silently truncate the field on the next
            # query and report reachable cells as unreachable.
            if self._enterable(cx, cy):
                for (dx, dy) in _MOVES:
                    nx, ny = cx + dx, cy + dy
                    if not self._win.in_bounds(nx, ny):
                        continue
                    if (nx, ny) in self._settled:
                        continue
                    nd = d + (self._diag if (dx and dy) else self._straight)
                    if nd < self._dist.get((nx, ny), _FINE_INF):
                        self._dist[(nx, ny)] = nd
                        heapq.heappush(self._heap, (nd, nx, ny))
            if (cx, cy) == (ix, iy):
                return d
        self._exhausted = True
        self._settled.add((ix, iy))
        if (ix, iy) not in self._dist:
            self._dist[(ix, iy)] = _FINE_INF
        return self._dist[(ix, iy)]


def unit_floor_milli(weights: _Weights, layer_purpose: dict[str, Any],
                     net_kind: str, layer_types: dict[str, str],
                     layers: "list[str]") -> int:
    """A hard integer-milli FLOOR on what one distance-unit of any planar move
    can cost in `_build_fine_cost`'s `planar`, for the NON-plane branch.

    `planar` charges `q(step x dist_units x layer_purpose x direction_factor +
    extras)` where `q` is round-half-even, `direction_factor >= min(1,
    off_direction)` and every `extra` (away-from-home, off-corridor, turn) and
    the post-quantization congestion overlay are non-negative. Flooring the
    per-unit term (instead of rounding it, as the octile heuristic does) makes
    the bound safe against `q`'s rounding in BOTH directions: `round(y) >=
    floor(y) >= floor(floor(x) x du)` for `y = x x du`.

    Deliberately NOT used for a plane-owning net: there `planar` can take the
    `plane_step x island_factor` branch, which an aggressive `plane.step` can
    price BELOW this floor. `_build_fine_cost` gates the field off entirely for
    those nets rather than weakening the floor for every net."""
    lp_kind = layer_purpose.get(net_kind, {})
    min_lp = min([float(lp_kind.get(layer_types[l], 1.0)) for l in layers] or [1.0])
    dir_min = min(1.0, float(weights.off_direction))
    per_unit = float(weights.step) * min_lp * dir_min
    return max(0, int(math.floor(per_unit * 1000.0)))


def _build_fine_cost(
    win: "_FineWindow", net_kind: str, weights: _Weights,
    layer_purpose: dict[str, Any], directions: dict[str, Any],
    home_layer: str | None, corridor_cells: set[tuple[int, int]] | None,
    congestion: dict[tuple[int, int, str], int] | None,
    plane_layers: dict[str, list[dict[str, Any]]] | None,
    goal_planes: dict[str, list[dict[str, Any]]] | None,
    plane_step: float, attachment_via_cost: float,
    goal_cell: tuple[int, int], goal_layers: set[str],
    multilayer_attachment: bool = False,
    return_path: dict[str, Any] | None = None,
    goal_field: bool = False,
) -> dict[str, Any]:
    """The ONE integer-milli cost model for the fine detailed search.

    Returns a dict of pure closures (`planar`, `via`, `heuristic`, `is_goal`,
    `plane_factor`, plus `li`/`step_milli_per_unit`). The cpu A* (`_fine_astar`),
    the deterministic backtrace (`_fine_backtrace`), and the numpy wavefront
    (`kicad_router_accel.fine_wavefront`) ALL cost moves through this single
    source of truth, so their integer cost fields are bit-identical - which is
    what makes the deterministic reconstruction pick the same path on every
    backend (7.8 parity).

    PHASE 7.18.1 (`multilayer_attachment`, from `plane.multilayer_attachment_
    choice`, default False) changes TWO things at the plane-attachment decision
    point, and nothing else:
      * `plane_factor` returns the BEST (minimum) island factor among ALL of
        this net's own fill components covering the cell on that layer, instead
        of the FIRST one found in `_plane_components_for`'s (-attachments,
        -area) order. On a board where a net owns several overlapping zones on
        one layer (kiln's GND_Main/GND_Safty each own three), the first-found
        component is not necessarily the cheapest one.
      * the attachment surcharge a via pays to land on the plane is SCALED by
        that component's island factor (`attachment_via_cost x factor`) rather
        than being the same flat 8.0 whether it lands on the mainland or on a
        one-attachment island. This is what makes the A*'s existing per-layer
        expansion an actual RANKING across every layer the net owns fill on:
        dropping onto In1.Cu's mainland now genuinely out-prices dropping onto
        F.Cu's weakly-attached island at the same (x, y).
    Mainland factor is 1.0, so a net owning exactly one healthy pour per layer
    prices identically either way; the flag still defaults False because
    islands/overlaps do move geometry.

    PHASE 7.18.3 (`return_path`, non-None only when `plane.return_path_bonus`
    > 0 AND the net being routed is a signal net) DISCOUNTS a via's cost by
    `bonus` when the via lands on a layer that is STACK-ADJACENT to a layer
    carrying this net's own reference-plane copper within `near_mm` of the via
    position. It is a via-placement preference ONLY: no planar move, no
    obstacle, no goal test consults it, and the signal net is never routed
    through the reference plane's fill (the 2026-07-24 REQUIRED CONSTRAINT gate
    in `_plane_components_for` is untouched - `plane_layers` is still None for
    every signal net, so every plane-traversal branch above stays False)."""
    g = win.grid
    lp_kind = layer_purpose.get(net_kind, {})
    layers = win.layers
    li = {name: i for i, name in enumerate(layers)}
    layer_types = win.layer_types
    min_lp = min([float(lp_kind.get(layer_types[l], 1.0)) for l in layers] or [1.0])
    step_milli_per_unit = weights.q(weights.step * min_lp)
    gx, gy = goal_cell
    cong = congestion or None
    plane = plane_layers or None
    plane_goal = goal_planes or None
    _pf_cache: dict[tuple[int, int, str], float | None] = {}

    def plane_factor(ix: int, iy: int, layer: str) -> float | None:
        if plane is None:
            return None
        key = (ix, iy, layer)
        if key in _pf_cache:
            return _pf_cache[key]
        val: float | None = None
        comps = plane.get(layer)
        if comps:
            nx, ny = win.node_xy(ix, iy)
            for c in comps:
                if c["raster"].covers(nx, ny, 0.0):
                    if not multilayer_attachment:
                        val = c["factor"]
                        break
                    # 7.18.1: keep scanning - a later component (a same-net
                    # zone overlapping this one) can be the cheaper attachment.
                    f = c["factor"]
                    if val is None or f < val:
                        val = f
        _pf_cache[key] = val
        return val

    # 7.18.3 return-path proximity, precomputed per (cell, layer). None when
    # the feature is off (`return_path is None`), which is the default and
    # keeps `via` byte-identical to pre-7.18.
    _rp_cache: dict[tuple[int, int, str], bool] = {}
    rp_bonus = float((return_path or {}).get("bonus", 0.0))
    rp_near_mm = float((return_path or {}).get("near_mm", 0.0))
    rp_by_layer: dict[str, list[Any]] = (return_path or {}).get("adjacent_rasters", {})

    def return_path_near(ix: int, iy: int, layer: str) -> bool:
        """True when this net's own reference-plane copper sits within
        `near_mm` of (ix, iy) on a layer STACK-ADJACENT to `layer` - i.e. a via
        landing here has a short, low-inductance return path available. Purely
        a via-cost preference (see the class docstring); never a permission."""
        if not rp_by_layer:
            return False
        rasters = rp_by_layer.get(layer)
        if not rasters:
            return False
        key = (ix, iy, layer)
        hit = _rp_cache.get(key)
        if hit is None:
            nx, ny = win.node_xy(ix, iy)
            hit = any(r.covers(nx, ny, rp_near_mm) for r in rasters)
            _rp_cache[key] = hit
        return hit

    def octile_heuristic(cx: int, cy: int) -> int:
        ax, ay = abs(cx - gx), abs(cy - gy)
        octile = (ax + ay) + (_SQRT2 - 2.0) * min(ax, ay)
        return int(math.floor(octile * step_milli_per_unit))

    # ---- 7.19.1 heuristic selection ------------------------------------- #
    # `tiebreak_heuristic` is ALWAYS octile. It is the ordering `_fine_backtrace`
    # (and the numpy tier's goal-state pick) uses to choose among tight optimal
    # predecessors, and pinning it makes reconstruction a pure function of the
    # optimal cost field - i.e. INDEPENDENT of which heuristic drove the search.
    # That pin is what turns "a tighter heuristic explores less" into "a tighter
    # heuristic explores less and returns the identical bytes".
    #
    # The FIELD is gated off for a plane-owning net: `planar`'s plane branch can
    # undercut `unit_floor_milli` (see its docstring), which would make the field
    # inadmissible - and an inadmissible heuristic changes the answer, silently.
    # Signal nets (every net with `plane_layers is None`, the overwhelming
    # majority) get the field; plane nets keep plain octile.
    use_field = bool(goal_field) and plane is None and plane_goal is None
    field: "_GoalDistanceField | None" = None
    if use_field:
        field = _GoalDistanceField(
            win, [goal_cell],
            unit_floor_milli(weights, layer_purpose, net_kind, layer_types, layers))

    if field is None:
        heuristic = octile_heuristic
    else:
        _f = field

        def heuristic(cx: int, cy: int) -> int:  # type: ignore[misc]
            # The FIELD ALONE - deliberately NOT `max(octile, field)`.
            #
            # `max` looks free (the max of two admissible heuristics is
            # admissible) but is not, because OCTILE IS NOT ACTUALLY ADMISSIBLE
            # HERE: it floors `octile_units x step_milli_per_unit` once for the
            # whole distance, while the real cost is a SUM of independently
            # `round`-ed per-move costs, so on some cells it overstates the true
            # optimum by a milli or two. (`tests/test_goal_field_heuristic.py::
            # test_octile_heuristic_is_marginally_inadmissible` pins that as a
            # measured property of today's code, not a claim.) It is harmless
            # for its two remaining jobs - ordering the legacy frontier and
            # breaking reconstruction ties deterministically - but folding it in
            # here would import the defect into the bound the drain relies on.
            #
            # The price is that on a wide-open straight run the field can sit a
            # milli or two BELOW octile. That costs a handful of extra
            # expansions in exactly the case where the search is already
            # trivial, and buys a heuristic whose admissibility is provable.
            return _f.value(cx, cy)

    def planar(ncx: int, ncy: int, layer: str, di: int, prev_d: int) -> int | None:
        """Integer milli-cost of the planar move in direction `di` INTO
        (ncx,ncy,layer) arriving from a state whose incoming heading was
        `prev_d` (-1 = none). None => the target is a hard obstacle."""
        if (ncx, ncy) in win.blocked_track[layer] and not (ncx == gx and ncy == gy):
            return None
        dx, dy = _MOVES[di]
        dist_units = _SQRT2 if (dx and dy) else 1.0
        dist_mm = dist_units * g
        extra = 0.0
        pf = plane_factor(ncx, ncy, layer)
        if pf is not None:
            base = weights.step * dist_units * plane_step * pf
        else:
            base = weights.step * dist_units * float(lp_kind.get(layer_types[layer], 1.0))
            base *= _direction_factor(weights, directions.get(layer), dx, dy)
            if home_layer is not None and layer != home_layer:
                extra += weights.away_from_home_per_mm * dist_mm
        if corridor_cells is not None and (ncx, ncy) not in corridor_cells:
            extra += weights.off_corridor * dist_mm
        if prev_d != -1 and di != prev_d:
            extra += weights.direction_change
        move_milli = weights.q(base + extra)
        if cong is not None:
            move_milli += cong.get((ncx, ncy, layer), 0)
        return move_milli

    def via(ix: int, iy: int, to_layer: str) -> int | None:
        """Integer milli-cost of a via hop landing on `to_layer` at (ix,iy).
        None => a via cannot be placed at this cell (via-blocked)."""
        if (ix, iy) in win.blocked_via:
            return None
        via_base = weights.via * weights.through_via
        pf = plane_factor(ix, iy, to_layer)
        if pf is not None:
            # 7.18.1: price the attachment by the component actually landed on
            # (mainland factor 1.0 == the historical flat surcharge).
            via_base += attachment_via_cost * pf if multilayer_attachment else attachment_via_cost
        if rp_bonus and return_path_near(ix, iy, to_layer):
            # 7.18.3: discount, floored so a via never becomes free/negative
            # (a zero-cost via would let the search thrash layers for nothing).
            # The floor is applied ONLY on the discounted branch, so an untuned
            # project's via cost is untouched even if it configured via=0.
            move_milli = max(weights.q(via_base - rp_bonus), _MIN_VIA_MILLI)
        else:
            move_milli = weights.q(via_base)
        if cong is not None:
            move_milli += cong.get((ix, iy, to_layer), 0)
        return move_milli

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

    return {
        "planar": planar, "via": via, "heuristic": heuristic, "is_goal": is_goal,
        # 7.19.1: the canonical, heuristic-INDEPENDENT reconstruction ordering.
        "tiebreak_heuristic": octile_heuristic,
        "goal_field": field,
        "plane_factor": plane_factor, "li": li,
        "step_milli_per_unit": step_milli_per_unit, "goal_cell": goal_cell,
        "goal_layers": goal_layers,
        # 7.18: exposed so the numpy backend builds bit-identical cost arrays.
        "return_path_near": return_path_near,
        "return_path_bonus": rp_bonus,
        "multilayer_attachment": multilayer_attachment,
        "min_via_milli": _MIN_VIA_MILLI,
    }


def _fine_backtrace(
    win: "_FineWindow", model: dict[str, Any],
    cost_get: "Callable[[tuple[int, int, str, int]], int | None]",
    goal_state: tuple[int, int, str, int],
    start_states: list[tuple[int, int, str, int]],
) -> list[tuple[int, int, str]]:
    """Deterministic path reconstruction from the OPTIMAL integer cost field.

    `cost_get(state)` returns the field cost of a `(cx,cy,layer,dir)` state
    (None if unreached). Walking backward from `goal_state`, at each step it
    picks - among every predecessor that TIGHTLY explains the current state's
    optimal cost (`cost_get(pred) + edge_cost(pred->cur) == cost_get(cur)`) -
    the one the cpu A* heap would have committed first: min key
    `(cost(pred) + heuristic(pred), cost(pred), px, py, layer_index, pred_dir)`.

    This is a pure function of the (bit-identical) field, so the cpu dict field
    and the numpy array field reconstruct byte-identical geometry - the 7.8
    parity guarantee. Returns the list of `(cx,cy,layer)` nodes (consecutive
    duplicate cells from via hops collapsed), same shape `_route_to_emit`
    consumes."""
    planar = model["planar"]
    via = model["via"]
    # 7.19.1: ALWAYS the octile tie-break, never the (possibly field-informed)
    # search heuristic - reconstruction must be a pure function of the optimal
    # cost field, so that a better-informed search returns identical geometry.
    heuristic = model.get("tiebreak_heuristic") or model["heuristic"]
    li = model["li"]
    layers = win.layers
    start_set = set(start_states)

    rev: list[tuple[int, int, str]] = []
    cur: tuple[int, int, str, int] | None = goal_state
    seen: set[tuple[int, int, str, int]] = set()
    while cur is not None:
        cx, cy, layer, d = cur
        if not rev or rev[-1] != (cx, cy, layer):
            rev.append((cx, cy, layer))
        if cur in start_set or cur in seen:
            break
        seen.add(cur)
        cur_cost = cost_get(cur)
        best_key: tuple[int, ...] | None = None
        best_pred: tuple[int, int, str, int] | None = None
        # Planar predecessor: `cur` was entered by a planar move whose heading
        # equals cur's stored `d` (so the source cell is fixed); the incoming
        # heading of that source (pd) is unknown, enumerate it.
        if d != -1:
            dx, dy = _MOVES[d]
            px, py = cx - dx, cy - dy
            if win.in_bounds(px, py):
                for pd in range(-1, 8):
                    pv = cost_get((px, py, layer, pd))
                    if pv is None:
                        continue
                    mc = planar(cx, cy, layer, d, pd)
                    if mc is None or pv + mc != cur_cost:
                        continue
                    key = (pv + heuristic(px, py), pv, px, py, li[layer], pd)
                    if best_key is None or key < best_key:
                        best_key, best_pred = key, (px, py, layer, pd)
        # Via predecessor: entered by a via onto `layer`, heading preserved.
        mc_via = via(cx, cy, layer)
        if mc_via is not None:
            for other in layers:
                if other == layer:
                    continue
                pv = cost_get((cx, cy, other, d))
                if pv is None or pv + mc_via != cur_cost:
                    continue
                key = (pv + heuristic(cx, cy), pv, cx, cy, li[other], d)
                if best_key is None or key < best_key:
                    best_key, best_pred = key, (cx, cy, other, d)
        cur = best_pred
    rev.reverse()
    return rev


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
    multilayer_attachment: bool = False,
    return_path: dict[str, Any] | None = None,
    goal_field: bool = False,
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
    and the search is byte-identical to the pre-7.5.4 behaviour (parity).

    PHASE 7.19.1 (`goal_field`, from `autorouter.goal_field_heuristic`): replaces
    the octile heuristic with `_GoalDistanceField` - an obstacle-aware backward
    Dijkstra lower bound that DOMINATES octile, so the search expands strictly
    less of the window for the identical answer. Two things make "the identical
    answer" a proof rather than an observation:

      1. `_fine_backtrace` and the numpy tier's goal-state pick are pinned to the
         OCTILE tie-break (`model["tiebreak_heuristic"]`), so reconstruction is a
         pure function of the optimal cost field, not of the search order.
      2. The loop below does not stop at the first goal pop when the field is in
         use; it DRAINS every state whose `f <= C*` (`C*` = the optimal cost, the
         first goal's `g`). With a consistent heuristic those are exactly the
         states A* pops with their OPTIMAL `g`, and every predecessor that can
         tightly explain an optimal path satisfies `g* + h <= g* + h* = C*` for
         any admissible `h`. So the set of tight predecessors the backtrace sees
         is the same set for ANY admissible/consistent heuristic - including the
         plain octile one, i.e. today's.

    The drain is affordable precisely because the field dominates octile: the
    drained set `{g* + h_field <= C*}` is a SUBSET of `{g* + h_octile <= C*}`,
    which is what today's search already expands (minus its `f == C*` fringe)."""
    model = _build_fine_cost(
        win, net_kind, weights, layer_purpose, directions, home_layer,
        corridor_cells, congestion, plane_layers, goal_planes, plane_step,
        attachment_via_cost, goal_cell, goal_layers,
        multilayer_attachment, return_path, goal_field)
    planar = model["planar"]
    via = model["via"]
    heuristic = model["heuristic"]
    tiebreak_h = model["tiebreak_heuristic"]
    # Drain mode is coupled to the field being ACTUALLY in use (it is gated off
    # for plane-owning nets inside `_build_fine_cost`), so a run without the
    # field executes the legacy break-on-first-goal loop unchanged.
    drain = model.get("goal_field") is not None
    # Reset the diagnostic counters up front so an EARLY return (blocked start,
    # or the field proving the goal unreachable) reports this call's real zero
    # rather than the previous call's leftovers.
    _FINE_SEARCH_STATS["expansions"] = 0
    _FINE_SEARCH_STATS["field_expansions"] = 0
    is_goal = model["is_goal"]
    li = model["li"]
    layers = win.layers

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
    heap: list[tuple[int, int, int, int, int, int]] = []
    for (sx, sy, l, d) in start_states:
        st = (sx, sy, l, d)
        h0 = heuristic(sx, sy)
        if drain and h0 >= _FINE_INF:
            # 7.19.1: the goal-distance field is a RELAXATION of the real
            # problem, so "no route in the relaxation" proves "no route at all".
            # The legacy search discovers that only by exhausting the window.
            continue
        best_g[st] = 0
        heapq.heappush(heap, (h0, 0, sx, sy, li[l], d))
    if drain and not heap:
        _FINE_SEARCH_STATS["field_expansions"] = model["goal_field"].expansions
        return None

    expansions = 0
    goal_state: tuple[int, int, str, int] | None = None
    # 7.19.1 drain bookkeeping (all inert when `drain` is False).
    limit: int | None = None
    goal_states: list[tuple[int, int, str, int]] = []
    while heap:
        f, gc, cx, cy, layer_i, d = heapq.heappop(heap)
        if limit is not None and f > limit:
            break  # nothing at f <= C* is left: the drained field is complete
        layer = layers[layer_i]
        st = (cx, cy, layer, d)
        if gc != best_g.get(st):
            continue
        if is_goal(cx, cy, layer):
            if not drain:
                goal_state = st
                break
            # Drain mode: record every optimal-cost goal state and keep going
            # until the f <= C* frontier is exhausted. Goal states are never
            # EXPANDED (matching the legacy loop, which stops here).
            if limit is None:
                limit = gc  # first goal popped: f == g == C*
            goal_states.append(st)
            continue
        expansions += 1
        if expansions > _FINE_ASTAR_MAX_EXPANSIONS:
            if limit is not None:
                # A goal was already proved optimal; stop draining rather than
                # discard it. Only reachable on a pathological window - the
                # drained set is a subset of what the legacy search expands.
                break
            return None

        for di, (dx, dy) in enumerate(_MOVES):
            ncx, ncy = cx + dx, cy + dy
            if not win.in_bounds(ncx, ncy):
                continue
            move_milli = planar(ncx, ncy, layer, di, d)
            if move_milli is None:
                continue
            ng = gc + move_milli
            nst = (ncx, ncy, layer, di)
            if nst not in best_g or ng < best_g[nst]:
                h = heuristic(ncx, ncy)
                if h >= _FINE_INF:
                    continue  # the relaxation proves the goal is unreachable here
                nf = ng + h
                if limit is not None and nf > limit:
                    continue  # provably off every optimal path (h admissible)
                best_g[nst] = ng
                heapq.heappush(heap, (nf, ng, ncx, ncy, layer_i, di))

        # via moves - layer change at the same node; needs a clear via cell.
        for other in layers:
            if other == layer:
                continue
            move_milli = via(cx, cy, other)
            if move_milli is None:
                break  # via-blocked at this cell: no via to ANY layer from here
            ng = gc + move_milli
            nst = (cx, cy, other, d)
            if nst not in best_g or ng < best_g[nst]:
                h = heuristic(cx, cy)
                if h >= _FINE_INF:
                    continue
                nf = ng + h
                if limit is not None and nf > limit:
                    continue
                best_g[nst] = ng
                heapq.heappush(heap, (nf, ng, cx, cy, li[other], d))

    # Diagnostic only (7.19.1 gate): how much of the window the last search had
    # to look at. Never read by the router itself - see `_FINE_SEARCH_STATS`.
    _FINE_SEARCH_STATS["expansions"] = expansions
    _FINE_SEARCH_STATS["field_expansions"] = (
        model["goal_field"].expansions if model.get("goal_field") is not None else 0)
    if drain and goal_states:
        # Pick the goal state by the SAME canonical rule the numpy tier uses -
        # the octile tie-break over the optimal-cost goal states - so cpu and
        # numpy select identically no matter which heuristic drove the search.
        goal_state = min(
            goal_states,
            key=lambda s: (best_g[s] + tiebreak_h(s[0], s[1]), best_g[s],
                           s[0], s[1], li[s[2]], s[3]))
    if goal_state is None:
        return None
    return _fine_backtrace(win, model, best_g.get, goal_state, start_states)


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

def _is_hand_copper_obstacle(ob: "_Obst") -> bool:
    """True for an obstacle that is human-placed, ripup-eligible copper: a
    hand-routed track/arc segment or via (owner is None - never autorouter-
    placed) with a known board uuid. Explicitly EXCLUDES footprint pads
    (`is_pad`), zone fills (`kind == "zone"`), and Edge.Cuts (`kind ==
    "edge"`) - those are never rip-up candidates even with
    `allow_hand_copper_ripup=True` (see `route_nets` docstring / NETCLASS_PLAN
    item 10 scoping notes)."""
    return (ob.owner is None and ob.kind in ("seg", "pt") and not ob.is_pad
            and not ob.is_edge and bool(ob.uuid))


def _self_check(
    net: str, segments: list[dict[str, Any]], vias: list[dict[str, Any]],
    obstacles: list["_Obst"], rules: dict[str, Any], via_radius: float,
) -> list[dict[str, Any]]:
    """Prove every proposed segment/via against ALL foreign copper at netclass
    clearance (edge-to-edge >= clearance). Returns a list of violation records;
    empty means the route is DRC-safe to emit. Same-net obstacles are skipped
    (a route legitimately touches its own endpoints' copper).

    Each violation carries `owner`: None for existing/human board copper (never
    rippable) or the integer connection id that owns the colliding AUTOROUTER-
    placed copper (rippable). This is what lets a caller (the worklist's rip-up
    step) tell a plane-skim against a filled zone/pad/hand-track (owner is None,
    correctly terminal) apart from a skim against another already-placed
    connection's own copper (owner is an id, demotable to rip-up).

    Per-segment width (Phase 7.12 neck-down): a segment carrying its own
    `"width"` key (a neck, narrower than the net's class width) is priced at
    THAT width, not the net's uniform `rules["track_width"]` - this is what
    makes the self-check DRC-true for a neck (pricing it at the wide class
    width would be an over-generous, wrong pass; pricing it at some other
    width would be simply wrong). A segment with no `"width"` key - every
    segment from every OTHER landed feature - falls back to
    `rules["track_width"]`, byte-identical to pre-7.12 behavior."""
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
            seg_half = s.get("width", rules["track_width"]) / 2.0
            need = seg_half + cl + ob.half - 1e-6
            if ob.seg_within(s["x1"], s["y1"], s["x2"], s["y2"], need):
                violations.append({"kind": "segment", "layer": s["layer"],
                                   "against_net": ob.net, "against_kind": ob.kind,
                                   "required_mm": round(need, 4), "owner": ob.owner,
                                   "against_is_pad": ob.is_pad,
                                   "obstacle_uuid": ob.uuid,
                                   "hand_copper": _is_hand_copper_obstacle(ob)})
        if ob.via_transparent:
            # a power/gnd plane yields an anti-pad to a crossing via - the plane
            # copper is cut back around the via on KiCad refill, so a via is NOT a
            # clearance violation against it (it still blocks tracks, checked
            # above). Skip the via checks for this obstacle.
            continue
        for v in vias:
            # a through via touches every routable layer; check against every
            # foreign obstacle regardless of the obstacle's own layer set.
            need = via_radius + cl + ob.half - 1e-6
            if ob.point_within(v["x"], v["y"], need):
                violations.append({"kind": "via", "against_net": ob.net,
                                   "against_kind": ob.kind, "required_mm": round(need, 4),
                                   "owner": ob.owner,
                                   "against_is_pad": ob.is_pad,
                                   "obstacle_uuid": ob.uuid,
                                   "hand_copper": _is_hand_copper_obstacle(ob)})
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
                          coarse_grid: float, coarse_min: tuple[float, float],
                          candidate_index: int = 0) -> set[tuple[int, int]] | None:
    """Fine window nodes within one coarse cell of the global stage's chosen
    coarse path - the soft corridor the detailed search is discounted to stay
    inside (leaving it costs off_corridor).

    `candidate_index` selects WHICH of 7.3a's ranked candidates to follow. It is
    0 for every call the router makes today; Phase 7.19.2's fallback tier is the
    only thing that ever passes anything else, and only after candidate 0 has
    already failed outright."""
    if not global_conn or not global_conn.get("candidates"):
        return None
    cands = global_conn["candidates"]
    if candidate_index >= len(cands):
        return None
    coarse_path = cands[candidate_index].get("coarse_path") or []
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


def _compute_plane_components_for(
    net: str, plane_fill_index: dict[str, list[dict[str, Any]]],
    power_patterns: list[str], plane_footprints: dict[str, Any],
    plane_tracks: dict[str, list[dict[str, Any]]],
    plane_pads_by_net: dict[str, list[dict[str, Any]]],
    plane_stack_order: dict[str, int], all_cu: list[str], routable_set: set[str],
    island_base: float, orphan_island_cost: float,
    cache: dict[str, dict[str, list[dict[str, Any]]]] | None = None,
) -> dict[str, list[dict[str, Any]]] | None:
    """Module-level twin of `route_nets`'s `_plane_components_for` closure - same
    body, parameterized so a multiprocessing WORKER can recompute a net's plane
    components locally (from board-derived inputs it rebuilds itself) instead of
    receiving a pre-built dict of `_FillRaster`-bearing objects through pickle,
    which is what made the pool initializer's `dump()` the dominant cost of the
    old parallel path (see 7.8b profiling notes). Pure function of its inputs;
    identical output to the parent's closure for the same board state."""
    if net not in plane_fill_index:
        return None
    if _pcb._net_kind(net, None, power_patterns) != "power":
        return None
    if cache is not None:
        cached = cache.get(net)
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
                comp_like, layer, net, plane_pads_by_net, plane_tracks,
                plane_stack_order, all_cu,
            )
            area = e["area_mm2"] if "area_mm2" in e else _polygon_area_mm2(e["pts"])
            recs.append((e, len(attachments), area))
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
    if cache is not None:
        cache[net] = result
    return result


def _reference_plane_rasters(
    signal_nets: list[str], plane_fill_index: dict[str, list[dict[str, Any]]],
    pads_by_net: dict[str, list[dict[str, Any]]], power_patterns: list[str],
    gnd_tokens: list[str], all_cu: list[str], routable_set: set[str],
    near_mm: float,
) -> dict[str, dict[str, Any]]:
    """Phase 7.18.3 - resolve each signal net's OWN reference plane and pre-slice
    that plane's fill rasters by STACK-ADJACENT layer.

    HOW "the net's own reference plane" is decided (documented choice; this is
    the part 7.18.3 left open):

    1. Candidate planes are the nets that OWN FILL and are power-kind by the
       existing `_net_kind`/`power_net_patterns` machinery - no second notion of
       "is this a power net" is introduced. Among those, GROUND nets (name
       contains one of `schematic_checks.cap_voltage.gnd_tokens`: GND, AGND,
       DGND, PGND, VSS) are preferred, because a return path is a ground return;
       only if the board has no ground pour at all do the remaining power pours
       become candidates.
    2. The winner is chosen by PAD VOTE: for each of the signal net's own pads,
       whichever candidate's fill actually covers that pad's location (any
       layer, within the pad's own contact reach) gets a vote. The candidate
       with the most votes wins; ties break on net name, so the result is
       deterministic and independent of dict/file order.
    3. A net whose pads sit over no pour at all gets NO reference plane and no
       bonus - guessing a plane it is nowhere near would be worse than
       abstaining.

    Rule 2 is what makes this correct on a board with SEVERAL isolated ground
    domains (kiln has GND_Main and GND_Safty, each pouring on F.Cu/B.Cu/In1.Cu):
    a safety-domain signal's pads sit inside the GND_Safty pour, so GND_Safty -
    not the larger GND_Main - becomes its reference, and its vias are pulled
    toward the plane that is genuinely its return path.

    Returns `{signal_net: {"net", "bonus"(filled by caller), "near_mm",
    "adjacent_rasters": {layer: [raster, ...]}}}`, where `adjacent_rasters[L]`
    holds the reference plane's fill rasters on the layers immediately above/
    below L in the board's copper stack - so a via landing on L is asked only
    about the layers it is actually referenced against."""
    cands = [n for n in sorted(plane_fill_index)
             if _pcb._net_kind(n, None, power_patterns) == "power"]
    tokens = [t.upper() for t in (gnd_tokens or [])]
    gnds = [n for n in cands if any(t in n.upper() for t in tokens)]
    if gnds:
        cands = gnds
    if not cands:
        return {}

    # net -> layer -> [raster], for the candidate planes only.
    by_net_layer: dict[str, dict[str, list[Any]]] = {}
    for n in cands:
        per_layer: dict[str, list[Any]] = {}
        for e in plane_fill_index[n]:
            per_layer.setdefault(e["layer"], []).append(e["raster"])
        by_net_layer[n] = per_layer

    stack = {name: i for i, name in enumerate(all_cu)}
    adjacent_of = {
        L: [o for o in all_cu if abs(stack.get(o, -99) - stack.get(L, 99)) == 1]
        for L in routable_set
    }

    out: dict[str, dict[str, Any]] = {}
    for net in signal_nets:
        votes: dict[str, int] = {}
        for pad in pads_by_net.get(net, []):
            pos = pad["position"]
            reach = _pad_reach(pad)
            for n in cands:
                if any(r.covers(pos["x"], pos["y"], reach)
                       for rasters in by_net_layer[n].values() for r in rasters):
                    votes[n] = votes.get(n, 0) + 1
        if not votes:
            continue
        ref = min(sorted(votes), key=lambda n: (-votes[n], n))
        adj: dict[str, list[Any]] = {}
        for L, others in adjacent_of.items():
            rasters = [r for o in others for r in by_net_layer[ref].get(o, [])]
            if rasters:
                adj[L] = rasters
        if not adj:
            continue
        out[net] = {"net": ref, "near_mm": near_mm, "adjacent_rasters": adj}
    return out


class _LazyPlaneByNet:
    """Worker-side stand-in for the parent's `plane_by_net` dict: same `.get(net)`
    interface `_route_one` uses, but computes (and memoizes) each net's plane
    components on first access from board-derived recipe pieces the worker
    rebuilt itself in `_worker_init`, instead of receiving the whole dict (with
    its `_FillRaster` payloads) through the pool initializer's pickle. Bit-
    identical values to the parent's dict for the same board state - a pure
    function of the same inputs, just evaluated lazily and process-locally."""

    __slots__ = ("fill_index", "power_patterns", "footprints", "tracks",
                 "pads_by_net", "stack_order", "all_cu", "routable_set",
                 "island_base", "orphan_island_cost", "_cache")

    def __init__(self, fill_index: dict[str, list[dict[str, Any]]],
                 power_patterns: list[str], footprints: dict[str, Any],
                 tracks: dict[str, list[dict[str, Any]]],
                 pads_by_net: dict[str, list[dict[str, Any]]],
                 stack_order: dict[str, int], all_cu: list[str],
                 routable_set: set[str], island_base: float,
                 orphan_island_cost: float) -> None:
        self.fill_index = fill_index
        self.power_patterns = power_patterns
        self.footprints = footprints
        self.tracks = tracks
        self.pads_by_net = pads_by_net
        self.stack_order = stack_order
        self.all_cu = all_cu
        self.routable_set = routable_set
        self.island_base = island_base
        self.orphan_island_cost = orphan_island_cost
        self._cache: dict[str, dict[str, list[dict[str, Any]]]] = {}

    def get(self, net: str, default: Any = None) -> Any:
        result = _compute_plane_components_for(
            net, self.fill_index, self.power_patterns, self.footprints,
            self.tracks, self.pads_by_net, self.stack_order, self.all_cu,
            self.routable_set, self.island_base, self.orphan_island_cost,
            self._cache)
        return result if result is not None else default


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


# --------------------------------------------------------------------------- #
# 7.8 multi-core: the per-connection detailed search extracted as a stateless,
# picklable unit of work. `ctx` bundles every immutable routing input (obstacles,
# weights, rules, layer info, plane components, backend...); `_route_one` reads
# ONLY from `ctx` + its explicit arguments and mutates nothing shared, so it runs
# identically in the parent or in a spawned worker process. All state commits
# (placement, congestion, owned-copper bookkeeping) stay in the parent, in
# canonical order — so the result is bit-identical for any worker count.
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# Phase 7.12: neck-down (wide net-class copper landing on a small pad)
# --------------------------------------------------------------------------- #

# Matches `_FineWindow.nearest_free`'s default `max_ring` (6 cells) - the
# existing "how far a pad escape reaches out of a dense pin field" constant,
# reused rather than inventing a new one (per NETCLASS_PLAN 7.12's "at least
# the pad-escape distance out of the pin field" rule). At a connection's own
# search grid this gives `6 * grid` mm as the practical floor under the
# configured `neck_down.min_length_mm`.
_NECK_ESCAPE_RING_CELLS = 6


def _neck_targets_for_conn(ctx: dict[str, Any], conn: dict[str, Any] | None) -> dict[str, float]:
    """{'from': neck_width, 'to': neck_width} for each connection endpoint that
    is a PAD whose smaller copper dimension the net-class width would overrun.
    Empty when `neck_down.enabled` is false, `conn` carries no pad-identified
    endpoint (e.g. terminates on existing copper, not a bare pad), or the
    class width already fits the pad (the common case - most connections
    return {} here, which is the parity guarantee: no `"width"` key is ever
    added to their segments).

    neck_width = min(class_width, max_width_vs_pad * pad's smaller dimension),
    floored at the board's `min_track_width` DRC rule (never emit copper
    narrower than that floor regardless of how small the pad is) - the exact
    Phase 7.12 spec formula."""
    if not conn:
        return {}
    neck_cfg = ctx.get("neck_cfg")
    if not neck_cfg or not neck_cfg.get("enabled", True):
        return {}
    pad_sizes: dict[tuple[str, str], tuple[float, float]] = ctx.get("pad_size_by_ref_num") or {}
    if not pad_sizes:
        return {}
    class_width = float(ctx["rules"]["track_width"])
    max_ratio = float(neck_cfg.get("max_width_vs_pad", 1.0))
    floor_w = float(ctx.get("neck_min_width", 0.0) or 0.0)
    out: dict[str, float] = {}
    for side in ("from", "to"):
        end = conn.get(side)
        if not end or end.get("kind") != "pad":
            continue
        size = pad_sizes.get((end.get("ref", ""), end.get("pad", "")))
        if not size:
            continue
        sx, sy = size
        pad_small = min(sx, sy) if (sx and sy) else (sx or sy)
        if pad_small <= 0 or class_width <= max_ratio * pad_small + 1e-9:
            continue  # already fits this pad - no neck, byte-identical segments
        neck_w = min(class_width, max_ratio * pad_small)
        neck_w = max(neck_w, floor_w)
        if neck_w >= class_width - 1e-9:
            continue
        out[side] = neck_w
    return out


def _split_segment_at(seg: dict[str, Any], dist_from_start: float
                      ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Split `seg` at `dist_from_start` mm from (x1,y1) towards (x2,y2), same
    layer on both pieces. Returns (near_part, far_part); either is None when
    the split point falls at/beyond an endpoint (the whole segment is
    "near", or none of it is)."""
    total = math.hypot(seg["x2"] - seg["x1"], seg["y2"] - seg["y1"])
    if total <= _EMIT_EPS_MM or dist_from_start >= total - _EMIT_EPS_MM:
        return dict(seg), None
    if dist_from_start <= _EMIT_EPS_MM:
        return None, dict(seg)
    t = dist_from_start / total
    mx = seg["x1"] + (seg["x2"] - seg["x1"]) * t
    my = seg["y1"] + (seg["y2"] - seg["y1"]) * t
    near = {"x1": seg["x1"], "y1": seg["y1"], "x2": mx, "y2": my, "layer": seg["layer"]}
    far = {"x1": mx, "y1": my, "x2": seg["x2"], "y2": seg["y2"], "layer": seg["layer"]}
    return near, far


def _apply_neck_endpoint(segments: list[dict[str, Any]], from_start: bool,
                         target_len: float, neck_width: float) -> list[dict[str, Any]]:
    """Tag the leading (`from_start=True`, the pad at `segments[0]`'s
    (x1,y1)) or trailing (`from_start=False`, the pad at `segments[-1]`'s
    (x2,y2)) stretch of `segments` - in path order, the "final stretch before
    the pad" the 7.12 spec calls for - with `width=neck_width`, up to
    `target_len` mm, splitting the boundary segment if the cut falls
    mid-segment. Segments beyond the neck are returned untouched (no
    `"width"` key - they keep emitting at the net's uniform class width
    exactly as before this feature). Segment order and each segment's own
    (x1,y1)->(x2,y2) direction are always preserved - only NEW dicts are
    produced (via `dict(seg)`/`_split_segment_at`), the input list/dicts are
    never mutated.

    Never consumes past the midpoint of the whole path: if both endpoints
    need a neck on a connection shorter than the sum of both neck lengths,
    each side is capped to at most half the total length so the two necks
    can never overlap or invert (a documented residual for pathologically
    short necked connections - see NETCLASS_PLAN 7.12 notes)."""
    if not segments:
        return segments
    total_len = sum(math.hypot(s["x2"] - s["x1"], s["y2"] - s["y1"]) for s in segments)
    tlen = min(target_len, total_len / 2.0)
    if tlen <= _EMIT_EPS_MM:
        return segments
    n = len(segments)
    index_order = range(n) if from_start else range(n - 1, -1, -1)
    replacements: dict[int, list[dict[str, Any]]] = {}
    consumed = 0.0
    for idx in index_order:
        remaining = tlen - consumed
        if remaining <= _EMIT_EPS_MM:
            break
        seg = segments[idx]
        seg_len = math.hypot(seg["x2"] - seg["x1"], seg["y2"] - seg["y1"])
        if seg_len <= remaining + _EMIT_EPS_MM:
            # whole segment falls within the neck stretch.
            necked = dict(seg)
            necked["width"] = neck_width
            replacements[idx] = [necked]
            consumed += seg_len
            continue
        # boundary segment: split so only the pad-adjacent piece necks down.
        if from_start:
            # pad is at (x1,y1); the near piece (0..remaining) is pad-adjacent.
            near, far = _split_segment_at(seg, remaining)
            pieces: list[dict[str, Any]] = []
            if near is not None:
                near["width"] = neck_width
                pieces.append(near)
            if far is not None:
                pieces.append(far)
        else:
            # pad is at (x2,y2); the far piece (cut..end) is pad-adjacent.
            cut = seg_len - remaining
            near, far = _split_segment_at(seg, cut)
            pieces = []
            if near is not None:
                pieces.append(near)
            if far is not None:
                far["width"] = neck_width
                pieces.append(far)
        replacements[idx] = pieces
        consumed = tlen
        break
    if not replacements:
        return segments
    out: list[dict[str, Any]] = []
    for idx, seg in enumerate(segments):
        out.extend(replacements.get(idx, [seg]))
    return out


def _finalize_core(
    ctx: dict[str, Any], net: str, win: "_FineWindow",
    path: list[tuple[int, int, str]], from_xy: tuple[float, float],
    to_xy: tuple[float, float], active_obstacles: list["_Obst"], margin: float,
    plane_layers: dict[str, list[dict[str, Any]]] | None,
    conn: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Fine path -> (rec-updates, segments, vias, violations); rec-updates None
    when the exact self-check rejects the path. Stateless (reads only `ctx`).

    `conn` (the ratsnest connection this path serves - optional, defaults to
    None) is ONLY used for Phase 7.12 neck-down: when it identifies a `from`/
    `to` endpoint as a pad whose smaller dimension the class width would
    overrun (`_neck_targets_for_conn`), the final stretch of copper at that
    endpoint is re-tagged to a narrower `width` BEFORE `_self_check` runs, so
    the self-check prices the neck at its true (narrow) width, never the
    class width. `conn=None` (or a connection needing no neck) leaves
    `segments` untouched - byte-identical to pre-7.12 behavior."""
    rules = ctx["rules"]
    routable_layers = ctx["routable_layers"]
    tw = ctx["tw"]
    segments, vias = _route_to_emit(win, path, from_xy, to_xy, plane_layers)
    neck_targets = _neck_targets_for_conn(ctx, conn)
    if neck_targets:
        neck_cfg = ctx["neck_cfg"]
        min_len = float(neck_cfg.get("min_length_mm", 0.5))
        max_len = float(neck_cfg.get("max_length_mm", 3.0))
        floor_len = _NECK_ESCAPE_RING_CELLS * win.grid
        target_len = min(max(min_len, floor_len), max_len)
        if "from" in neck_targets:
            segments = _apply_neck_endpoint(segments, True, target_len, neck_targets["from"])
        if "to" in neck_targets:
            segments = _apply_neck_endpoint(segments, False, target_len, neck_targets["to"])
    violations = _self_check(net, segments, vias, active_obstacles, rules, ctx["via_radius"])
    if violations:
        return None, segments, vias, violations
    length = sum(math.hypot(s["x2"] - s["x1"], s["y2"] - s["y1"]) for s in segments)
    layers_used = sorted({s["layer"] for s in segments},
                         key=lambda l: routable_layers.index(l) if l in routable_layers else 999)
    est_cost = (length * float(tw.get("length_mm", 1.0)) + len(vias) * float(tw.get("via", 5.0)))
    rec_updates = {
        "routed": True, "length_mm": round(length, 4), "via_count": len(vias),
        "layers": layers_used, "segment_count": len(segments),
        "window_margin_mm": margin, "grid_mm": win.grid,
        "est_phase6_cost": round(est_cost, 4),
        "self_check": {"passed": True, "violation_count": 0},
    }
    return rec_updates, segments, vias, []


def _max_ladder_window_bound(
    from_xy: tuple[float, float], to_xy: tuple[float, float],
    board_bbox: tuple[float, float, float, float], ctx_grid: float,
    attempts: list[tuple[float, float]],
) -> tuple[float, float, float, float, float]:
    """(wminx, wminy, wmaxx, wmaxy, max_grid): the reach-padded window bound at
    the LARGEST (margin, grid) combination reachable across every ladder rung
    in `attempts` - a single conservative bound both `_prefilter_window_
    obstacles` and `_build_zone_edge_cache` build their per-connection,
    reused-across-rungs structures against.

    Safe upper bound because every rung shares the same connection (net,
    track_half, via_radius, clearance, edge_clearance) and only its (margin,
    grid) differ:
      * the window span `_route_one` builds from `margin` is monotonically
        non-shrinking as `margin` grows (larger request pushes minx/miny down
        and maxx/maxy up; the board-bbox clamp only floors/ceils it, never
        un-does that), so the span at `max(margin for margin,_ in attempts)`
        is a superset of every rung's actual span;
      * `obstacle_cells` further pads that span by `grid * _FINE_CELL_MARGIN_
        FRAC` plus the shared clearance/half terms - using `max(grid for _,
        grid in attempts)` for that pad only widens the probe further, so a
        test against this bound is at least as generous as any individual
        rung's smaller pad and possibly-smaller span."""
    max_margin = max(m for (m, _g) in attempts)
    max_grid = max(g for (_m, g) in attempts)
    minx = max(min(from_xy[0], to_xy[0]) - max_margin, board_bbox[0] - ctx_grid)
    miny = max(min(from_xy[1], to_xy[1]) - max_margin, board_bbox[1] - ctx_grid)
    maxx = min(max(from_xy[0], to_xy[0]) + max_margin, board_bbox[2] + ctx_grid)
    maxy = min(max(from_xy[1], to_xy[1]) + max_margin, board_bbox[3] + ctx_grid)
    return (minx - max_grid, miny - max_grid, maxx + max_grid, maxy + max_grid, max_grid)


def _prefilter_window_obstacles(
    obstacles: list["_Obst"], net: str, from_xy: tuple[float, float],
    to_xy: tuple[float, float], board_bbox: tuple[float, float, float, float],
    ctx_grid: float, attempts: list[tuple[float, float]],
    track_half: float, via_radius: float, clearance: float, edge_clearance: float,
) -> list["_Obst"]:
    """Subset of `obstacles` that COULD be kept by at least one ladder rung's
    `_FineWindow.build` (i.e. NOT bbox-rejected by `obstacle_cells`'s early-out,
    see line ~3507), evaluated once against the UNION of every rung's window
    (`_max_ladder_window_bound`) instead of per-rung.

    A same-net non-edge obstacle is unconditionally free at every rung (the
    first line of `obstacle_cells`), so it is dropped here regardless of bbox."""
    if not attempts:
        return obstacles
    wminx, wminy, wmaxx, wmaxy, max_grid = _max_ladder_window_bound(
        from_xy, to_xy, board_bbox, ctx_grid, attempts)
    base_reach = max(track_half, via_radius) + max(clearance, edge_clearance) + max_grid * _FINE_CELL_MARGIN_FRAC
    out: list[_Obst] = []
    for ob in obstacles:
        if ob.net == net and not ob.is_edge:
            continue  # same-net copper is free at every rung, unconditionally
        reach = base_reach + ob.half
        if (ob.maxx < wminx - reach or ob.minx > wmaxx + reach
                or ob.maxy < wminy - reach or ob.miny > wmaxy + reach):
            continue
        out.append(ob)
    return out


def _build_zone_edge_cache(
    obstacles: list["_Obst"], from_xy: tuple[float, float], to_xy: tuple[float, float],
    board_bbox: tuple[float, float, float, float], ctx_grid: float,
    attempts: list[tuple[float, float]],
    track_half: float, via_radius: float, clearance: float, edge_clearance: float,
) -> dict[int, "_ZoneEdgeGrid"]:
    """{id(zone obstacle): _ZoneEdgeGrid}, built ONCE per connection at the same
    `_max_ladder_window_bound` used for the obstacle prefilter, so every rung's
    `_FineWindow.obstacle_cells` can reuse it instead of re-clipping the zone's
    polygon edges (`_clip_polygon_edges`) and re-bucketing them (`_ZoneEdgeGrid.
    __init__`) on every rung's build - a board-spanning plane fill's edge list is
    the same edges regardless of which rung is asking.

    Exact for every rung: `_ZoneEdgeGrid.min_dist`'s correctness argument is
    generic in the `reach` used both to build (bucket size + edge-insertion
    padding) and to query (the threshold a caller compares its result against) -
    it only requires the query threshold be `<= reach`. Building here with
    `reach = big` computed from `max_grid` (see `_max_ladder_window_bound`) and
    the connection's constant track_half/via_radius/clearance/edge_clearance
    gives a `big` that upper-bounds every individual rung's own (smaller-grid)
    `big`, so a rung's actual query threshold (its own track_reach/via_reach)
    is always `<= this cache's reach` - the same generosity argument used for
    the window bbox: the padded bbox this cache clips edges against is a
    superset of every rung's own (smaller) padded bbox, so no relevant edge is
    missed by clipping once at the bound instead of per rung."""
    if not attempts:
        return {}
    wminx, wminy, wmaxx, wmaxy, max_grid = _max_ladder_window_bound(
        from_xy, to_xy, board_bbox, ctx_grid, attempts)
    margin_term = max_grid * _FINE_CELL_MARGIN_FRAC
    cache: dict[int, _ZoneEdgeGrid] = {}
    for ob in obstacles:
        if ob.kind != "zone" or not ob.pts:
            continue
        cl = edge_clearance if ob.is_edge else clearance  # always `clearance` for zones (is_edge False)
        track_reach = track_half + cl + ob.half + margin_term
        via_reach = via_radius + cl + ob.half + margin_term
        big = max(track_reach, via_reach)
        zedges = _clip_polygon_edges(ob.pts, wminx - big, wminy - big, wmaxx + big, wmaxy + big)
        cache[id(ob)] = _ZoneEdgeGrid(zedges, big)
    return cache


# --------------------------------------------------------------------------- #
# Phase 7.19.2 - cheap pre-ranking of the global stage's alternate candidates.
# --------------------------------------------------------------------------- #

# Milli-cost charged per LAYER CHANGE along a coarse candidate, on top of its own
# `est_cost_milli`, when deciding whether the candidate is worth detail-routing.
# Deliberately a fixed constant and NOT a re-derivation of the real via cost: the
# whole point of the pre-rank is that it costs nothing to compute (no grid, no
# window, no search), so it must not go looking anything up. Overridable via
# `autorouter.candidate_fallback.via_penalty_milli`.
_PRERANK_VIA_PENALTY_MILLI = 25_000


def _candidate_layer_changes(candidate: dict[str, Any]) -> int:
    """Layer changes along a coarse candidate path - a pure count over data
    7.3a already emitted (`coarse_path` is `[[cx, cy, layer], ...]`)."""
    path = candidate.get("coarse_path") or []
    changes = 0
    prev: str | None = None
    for entry in path:
        layer = entry[2] if len(entry) > 2 else None
        if prev is not None and layer != prev:
            changes += 1
        prev = layer
    return changes


def _prerank_candidates(gconn: dict[str, Any] | None,
                        cfg: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Rank 7.3a's candidates by a CHEAP estimate and mark which are worth ever
    detail-routing. No fine grid, no window, no A* - just the coarse cost 7.3a
    already computed plus a fixed per-layer-change constant.

    Returns one record per candidate IN THE ORIGINAL ORDER (candidate 0 stays
    candidate 0 - this never re-orders what the router tries first, it only
    decides how far down the list is worth trying at all):
    `{index, est_cost_milli, layer_changes, prerank_milli, worth}`.

    `worth` is False when `prerank_milli` exceeds
    `top_prerank x max_cost_ratio + slack_milli`, i.e. when the candidate is so
    much more expensive than the best one that paying a full windowed A* to find
    out is not justified even as a fallback. Candidate 0 is ALWAYS worth (it is
    what the router routes today, and the ratio is measured against it)."""
    cands = (gconn or {}).get("candidates") or []
    cfg = cfg or {}
    via_pen = int(cfg.get("via_penalty_milli", _PRERANK_VIA_PENALTY_MILLI))
    ratio = float(cfg.get("max_cost_ratio", 1.35))
    slack = int(cfg.get("slack_milli", 0))
    max_c = int(cfg.get("max_candidates", 3))
    out: list[dict[str, Any]] = []
    for i, c in enumerate(cands):
        changes = _candidate_layer_changes(c)
        est = int(c.get("est_cost_milli", 0))
        out.append({"index": i, "est_cost_milli": est, "layer_changes": changes,
                    "prerank_milli": est + via_pen * changes, "worth": True})
    if not out:
        return out
    budget = int(out[0]["prerank_milli"] * ratio) + slack
    for rec in out:
        if rec["index"] == 0:
            continue
        rec["worth"] = rec["index"] < max_c and rec["prerank_milli"] <= budget
    return out


def _route_one(
    ctx: dict[str, Any], conn: dict[str, Any], active_obstacles: list["_Obst"],
    congestion: dict[tuple[int, int, str], int], use_corridor: bool = True,
) -> dict[str, Any]:
    """Detailed routing for ONE connection, including Phase 7.19.2's alternate-
    candidate fallback.

    THE FLOW THIS REPLACES (verified against the code, not assumed): 7.3a emits
    1-3 ranked coarse candidates per connection, and detailed routing used
    candidate 0 and ONLY candidate 0 - `_corridor_from_global` and
    `_hier_world_waypoints` both indexed `[0]` unconditionally, so candidates 1
    and 2 were computed by the global stage and then thrown away. There was
    therefore no "try them in order until one succeeds" loop to make cheaper;
    there was a discarded resource and no fallback at all.

    What this adds, all of it behind `autorouter.candidate_fallback.enabled`
    (default False, so an untuned project stays byte-identical):
      * a FALLBACK - if candidate 0 fails outright, retry the whole ladder along
        candidate 1's (then 2's) corridor and hierarchical waypoints;
      * the CHEAP GATE that is the actual phase deliverable - `_prerank_candidates`
        decides from the coarse cost alone whether a lower-ranked candidate is
        worth a full windowed A*, so a hopeless alternate is skipped without ever
        being searched.

    A connection whose candidate 0 succeeds never reaches any of this, which is
    what keeps "the top candidate already works" byte-identical - the fallback
    can only ever turn a failure into a route, never move a success."""
    raw_cfg = ctx.get("candidate_fallback")
    cfg = raw_cfg if isinstance(raw_cfg, dict) else {}
    if not cfg.get("enabled"):
        return _route_one_candidate(ctx, conn, active_obstacles, congestion,
                                    use_corridor, 0)
    out = _route_one_candidate(ctx, conn, active_obstacles, congestion,
                               use_corridor, 0)
    if out["routed"] or not use_corridor:
        return out
    net = conn["net"]
    from_xy, to_xy = _conn_endpoints(conn)
    gkey = (net, round(from_xy[0], 3), round(from_xy[1], 3),
            round(to_xy[0], 3), round(to_xy[1], 3))
    ranked = _prerank_candidates(ctx["global_by_key"].get(gkey), cfg)
    skipped: list[int] = []
    for rec in ranked[1:]:
        if not rec["worth"]:
            skipped.append(rec["index"])
            continue
        alt = _route_one_candidate(ctx, conn, active_obstacles, congestion,
                                   use_corridor, rec["index"])
        if alt["routed"]:
            alt["rec"]["candidate_index"] = rec["index"]
            alt["rec"]["candidates_prerank_skipped"] = skipped
            return alt
    if skipped:
        out["rec"]["candidates_prerank_skipped"] = skipped
    return out


def _route_one_candidate(
    ctx: dict[str, Any], conn: dict[str, Any], active_obstacles: list["_Obst"],
    congestion: dict[tuple[int, int, str], int], use_corridor: bool = True,
    candidate_index: int = 0,
) -> dict[str, Any]:
    """Window-doubling detailed search + self-check for ONE connection against a
    GIVEN obstacle set + congestion field. Pure function of (ctx, conn,
    active_obstacles, congestion, use_corridor) — emits nothing, mutates nothing
    shared. The parent worklist owns placement/rip-up. Returns the `out` record
    (including the last `_FineWindow` for in-place rip-up)."""
    net = conn["net"]
    net_kind = _pcb._net_kind(net, None, ctx["power_patterns"])
    from_xy, to_xy = _conn_endpoints(conn)
    gkey = (net, round(from_xy[0], 3), round(from_xy[1], 3),
            round(to_xy[0], 3), round(to_xy[1], 3))
    gconn = ctx["global_by_key"].get(gkey)
    home_layer = gconn.get("home_layer") if gconn else None
    routable_layers = ctx["routable_layers"]
    routable_set = ctx["routable_set"]
    grid = ctx["grid"]
    backend = ctx["backend"]
    weights = ctx["weights"]
    # Phase 7.3d: the toward_xy bias below is only ever computed if this is
    # True (settings default False) - see `nearest_free`'s docstring.
    pad_escape_aware = bool(ctx.get("pad_escape_direction_aware", False))
    from_item_layers = (conn.get("from") or {}).get("layers") or conn.get("from_layers") or routable_layers
    to_item_layers = (conn.get("to") or {}).get("layers") or conn.get("to_layers") or routable_layers
    start_layers = [l for l in from_item_layers if l in routable_set] or routable_layers
    goal_layers = set(l for l in to_item_layers if l in routable_set) or set(routable_layers)

    plane_layers = ctx["plane_by_net"].get(net)
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

    board_bbox = ctx["board_bbox"]
    board_min = ctx["board_min"]
    layer_types = ctx["layer_types"]
    rules = ctx["rules"]
    track_half = ctx["track_half"]
    via_radius = ctx["via_radius"]
    max_grid_mm = ctx["max_grid_mm"]
    max_window_nodes = ctx["max_window_nodes"]
    layer_purpose = ctx["layer_purpose"]
    directions = ctx["directions"]
    coarse_grid = ctx["coarse_grid"]
    coarse_min = ctx["coarse_min"]
    plane_step = ctx["plane_step"]
    attachment_via_cost = ctx["attachment_via_cost"]
    # Phase 7.18: both default-off. `ml_attach` re-ranks the plane attachment
    # (7.18.1); `return_path` is non-None ONLY for a signal net when
    # `plane.return_path_bonus` > 0 (7.18.3) - `plane_layers` is None for every
    # signal net (the 2026-07-24 power-net gate), so the two never overlap.
    ml_attach = bool(ctx.get("multilayer_attachment", False))
    # Phase 7.19.1 obstacle-aware goal-distance heuristic (default off).
    goal_field = bool(ctx.get("goal_field_heuristic", False))
    return_path = (ctx.get("return_path_by_net") or {}).get(net)

    # Ordered (margin, grid) attempts: attempt 1 is the legacy (base_margin,
    # adaptive-grid) pair (so any connection that already routes on it is
    # byte-identical), then the on-FAILURE ladder of finer grids + wider margins.
    attempts = _route_attempts(from_xy, to_xy, board_bbox, grid, ctx["min_grid_mm"],
                               max_grid_mm, ctx["base_margin"], len(routable_layers),
                               max_window_nodes)
    # Pre-filter once to the obstacles ANY ladder rung could possibly keep (see
    # `_prefilter_window_obstacles`), instead of re-testing the full board-wide
    # obstacle list (~2800 items) against every rung's bbox reject inside
    # `obstacle_cells`. Only `win.build` below uses this subset; `_finalize_core`'s
    # exact self-check and `_nearest_blocker`'s global nearest-obstacle search
    # keep using the untouched `active_obstacles`.
    window_obstacles = _prefilter_window_obstacles(
        active_obstacles, net, from_xy, to_xy, board_bbox, grid, attempts,
        track_half, via_radius, rules["clearance"], rules["edge_clearance"])
    # Per-connection zone-edge-grid cache (see `_build_zone_edge_cache`): built
    # LAZILY, only once attempt 1 (the legacy, byte-identical-parity rung) has
    # failed and the ladder is actually going to run more than one rung -
    # building it eagerly for every connection would size it to the WORST-CASE
    # ladder rung (a 60 mm margin) even for the common case that routes on
    # attempt 1 alone, which measured as a net LOSS (the eager build's own cost
    # exceeded what it saved). Once built (from `attempts[1:]`, the sub-ladder
    # that actually still runs), it is reused by every remaining rung's window.
    zone_cache: dict[int, "_ZoneEdgeGrid"] | None = None
    win: _FineWindow | None = None
    margin = ctx["base_margin"]
    any_built = False
    for attempt_idx, (margin, win_grid) in enumerate(attempts):
        if attempt_idx == 1 and zone_cache is None:
            zone_cache = _build_zone_edge_cache(
                window_obstacles, from_xy, to_xy, board_bbox, grid, attempts[1:],
                track_half, via_radius, rules["clearance"], rules["edge_clearance"])
        minx = max(min(from_xy[0], to_xy[0]) - margin, board_bbox[0] - grid)
        miny = max(min(from_xy[1], to_xy[1]) - margin, board_bbox[1] - grid)
        maxx = min(max(from_xy[0], to_xy[0]) + margin, board_bbox[2] + grid)
        maxy = min(max(from_xy[1], to_xy[1]) + margin, board_bbox[3] + grid)
        win = _FineWindow(minx, miny, maxx, maxy, win_grid, routable_layers, layer_types, net)
        if win.cols * win.rows * max(1, len(routable_layers)) > max_window_nodes:
            win = None
            continue  # over budget at this (margin, grid) - a tighter/coarser one may fit
        any_built = True
        win._zone_cache = zone_cache
        win.build(window_obstacles, track_half, via_radius, rules["clearance"], rules["edge_clearance"])
        s_cell = win.nearest_free(from_xy[0], from_xy[1], start_layers,
                                  toward_xy=to_xy if pad_escape_aware else None) or win.cell_of(*from_xy)
        g_cell = win.nearest_free(to_xy[0], to_xy[1], list(goal_layers),
                                  toward_xy=from_xy if pad_escape_aware else None) or win.cell_of(*to_xy)
        corridor = (_corridor_from_global(win, gconn, coarse_grid, coarse_min,
                                          candidate_index) if use_corridor else None)
        win_cong = _project_congestion(win, congestion, board_min[0], board_min[1], grid)
        out.update({"win": win, "s_cell": s_cell, "g_cell": g_cell,
                    "corridor": corridor, "margin": margin,
                    "active_obstacles": active_obstacles})

        path = _fine_search(backend, win, net_kind, weights, layer_purpose, directions,
                            s_cell, start_layers, g_cell, goal_layers,
                            home_layer, corridor, win_cong,
                            plane_layers, goal_planes, plane_step, attachment_via_cost,
                            ml_attach, return_path, goal_field,
                            _settings=ctx.get("gpu_settings"))
        if path is None:
            continue  # unreachable at this (margin, grid) - try the next ladder rung

        rec_updates, segments, vias, violations = _finalize_core(
            ctx, net, win, path, from_xy, to_xy, active_obstacles, margin, plane_layers, conn)
        if rec_updates is None:
            # A path cleared the A* obstacle model but not the exact clearance
            # pass (a plane-skim). NOT unconditionally terminal: `out["path"]`
            # and the FULL `violations` list (each carrying `owner` - see
            # `_self_check`) are kept so the worklist's rip-up step (Step 4,
            # ~line 5300) can attempt demotion - ripping precisely the placed
            # connections that own the colliding copper and re-finalizing this
            # SAME path against the reduced obstacle set. A skim whose
            # violations are all against non-rippable copper (owner is None:
            # a filled zone/plane, a pad, an edge, hand-routed copper) cannot be
            # helped by rip-up and correctly stays terminal there.
            result_rec["self_check"] = {"passed": False, "violations": violations[:8],
                                        "violation_count": len(violations)}
            result_rec["failure"] = {"reason": "self_check_failed",
                                     "detail": "proposed copper clears the A* obstacle model "
                                               "but not the exact clearance pass (plane-skim); "
                                               "demoted to rip-up when the skim is against "
                                               "rippable autorouter-placed copper"}
            out["path"] = path
            out["violations"] = violations
            return out
        result_rec.update(rec_updates)
        out.update({"routed": True, "segments": segments, "vias": vias})
        return out

    if any_built:
        # Every `_route_attempts` rung failed (finest grid included) - LAST RESORT
        # TIER 1: chain small fine-grid sub-windows along the global stage's own
        # coarse path (see `_route_hierarchical`'s docstring for why this can
        # succeed where a single wide-margin window cannot). Strictly gated behind
        # total ladder exhaustion, so a connection that already routes today never
        # reaches this call and is byte-identical to before this tier existed.
        hier = _route_hierarchical(ctx, net, net_kind, from_xy, to_xy, start_layers,
                                   goal_layers, active_obstacles, gconn, home_layer,
                                   plane_layers, goal_planes, candidate_index)
        if hier is not None:
            rec_updates, segments, vias = hier
            result_rec.update(rec_updates)
            out.update({"routed": True, "segments": segments, "vias": vias})
            return out

    # LAST RESORT TIER 2 (M5 whole-board windowing): ONE lazily-evaluated window
    # over the WHOLE board. See `_route_wide_lazy`. Deliberately ordered AFTER the
    # hierarchical tier so every connection either tier already routes keeps its
    # exact current geometry; this one only ever converts a hard failure into a
    # route. Reached from BOTH failure paths - `unreachable_in_window` AND
    # `window_too_large` (a lazy window has no up-front rasterization cost, so the
    # eager node budget that produced `window_too_large` simply does not apply).
    wide = _route_wide_lazy(ctx, conn, net, net_kind, from_xy, to_xy, start_layers,
                            goal_layers, active_obstacles, congestion, home_layer,
                            plane_layers, goal_planes)
    if wide is not None:
        wrec, wsegments, wvias, wwin = wide
        result_rec.update(wrec)
        out.update({"routed": True, "segments": wsegments, "vias": wvias, "win": wwin})
        return out

    if not any_built:
        # every attempted window exceeded the node budget - the pathological
        # large window (nothing was ever searched).
        result_rec["failure"] = {"reason": "window_too_large",
                                 "detail": "window exceeds node budget at every attempted grid",
                                 "window_margin_mm": margin,
                                 "grid_mm": attempts[0][1] if attempts else None}
        out["win"] = None
        return out

    # unreachable within every attempted window/grid (finest grid included).
    blocker = _nearest_blocker(win, active_obstacles, net, to_xy) if win is not None else None
    result_rec["failure"] = {"reason": "unreachable_in_window",
                             "nearest_blocker": blocker, "window_margin_mm": margin}
    return out


# --------------------------------------------------------------------------- #
# Whole-board lazy window tier (M5 - lifting the 60 mm / 400k-node cap).
#
# WHY THE OLD CAP EXISTED, AND WHY IT CAN NOW GO: `_FineWindow.build` rasterizes
# obstacle -> cells, so its cost is O(total inflated obstacle area / grid^2) no
# matter what the search then looks at. A board-spanning plane fill at 0.2 mm over
# a 60 mm window is millions of cell tests before A* takes its first step, which
# is what `_MAX_WINDOW_SPAN_MM` / `_MAX_WINDOW_NODES` were really capping - build
# cost, not memory (the blocked sets were always sparse). Naively raising the
# constants therefore "blows pure-Python runtime", exactly as the plan recorded.
#
# The fix is to stop building the window at all: `_FineWindow(..., lazy=True)`
# indexes the obstacles (`_ObstacleIndex`) and decides blocked-ness per cell on
# demand (`_LazyBlockedSet`), so cost becomes O(cells A* actually expands), i.e.
# output-sensitive like A* itself. A whole-board window then costs no more to
# build than a small one, and the search pays only for what it explores.
#
# WHAT THIS BUYS (and what it does not): it fixes the failure mode where the only
# legal path leaves the capped window - a long detour "the wrong way" around an
# obstruction, which no ladder rung can see (span-capped) and which chunk-chaining
# along the coarse path cannot find either (the coarse path does not go that way).
# It does NOT help a pad that is topologically SEALED: if no legal channel exists
# at any resolution, a bigger window just proves it faster. Kiln's remaining
# failures are of that second kind (see NETCLASS_PLAN.md item 10's flood-fill
# re-diagnosis), so this tier is a real, tested capability that does not by itself
# change kiln's routed count - reported honestly rather than tuned to look good.
# --------------------------------------------------------------------------- #

# Node budget for the LAZY whole-board window. An order of magnitude above the
# eager `_MAX_WINDOW_NODES` because nothing is materialized up front: this only
# picks the grid (via `_choose_grid`) so that a genuinely huge board still
# coarsens rather than asking A* to expand an absurd state space. Kiln's whole
# board at the default 0.2 mm on 4 layers sits well inside it, so kiln keeps its
# full fine resolution over the entire board.
_MAX_LAZY_WINDOW_NODES = 4_000_000


def _route_wide_lazy(
    ctx: dict[str, Any], conn: dict[str, Any], net: str, net_kind: str,
    from_xy: tuple[float, float], to_xy: tuple[float, float],
    start_layers: list[str], goal_layers: set[str],
    active_obstacles: list["_Obst"], congestion: dict[tuple[int, int, str], int],
    home_layer: str | None,
    plane_layers: dict[str, list[dict[str, Any]]] | None,
    goal_planes: dict[str, list[dict[str, Any]]] | None,
) -> "tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], _FineWindow] | None":
    """One WHOLE-BOARD lazily-evaluated `_FineWindow` search — the M5 lift of the
    60 mm span / 400k-node cap. See the block comment above for the cost argument.

    Unlike the hierarchical tier this one goes through the ordinary
    `_finalize_core`, so a route it finds gets the same exact-clearance
    self-check, neck-down (7.12), emit path, and rip-up demotion eligibility as
    any ladder-routed connection — there is no second code path to keep in sync.

    Returns None (never a partial result) when the whole-board search is still
    unreachable or its geometry fails the exact self-check; the caller then
    reports the ordinary failure it would have reported without this tier.

    Determinism: a pure function of its inputs — a fixed window (the board bbox),
    a grid chosen by the same deterministic `_choose_grid`, and the same
    deterministic A*/backtrace every other tier uses."""
    board_bbox = ctx["board_bbox"]
    board_min = ctx["board_min"]
    routable_layers = ctx["routable_layers"]
    layer_types = ctx["layer_types"]
    grid = ctx["grid"]
    rules = ctx["rules"]
    track_half = ctx["track_half"]
    via_radius = ctx["via_radius"]
    backend = ctx["backend"]
    pad_escape_aware = bool(ctx.get("pad_escape_direction_aware", False))
    # Phase 7.18: identical reads to `_route_one`/`_route_hierarchical`. This
    # tier goes through the same `_finalize_core`, so it must also cost a move
    # the same way - a connection rescued here has to be priced by the same
    # cost model as one the ordinary ladder routes, or the two tiers would
    # disagree about the same board.
    ml_attach = bool(ctx.get("multilayer_attachment", False))
    # Phase 7.19.1 obstacle-aware goal-distance heuristic (default off).
    goal_field = bool(ctx.get("goal_field_heuristic", False))
    return_path = (ctx.get("return_path_by_net") or {}).get(net)

    minx = board_bbox[0] - grid
    miny = board_bbox[1] - grid
    maxx = board_bbox[2] + grid
    maxy = board_bbox[3] + grid
    span_x, span_y = maxx - minx, maxy - miny
    if span_x <= 0 or span_y <= 0:
        return None
    n_layers = max(1, len(routable_layers))
    win_grid = _choose_grid(span_x, span_y, n_layers, grid, ctx["max_grid_mm"],
                            _MAX_LAZY_WINDOW_NODES)

    win = _FineWindow(minx, miny, maxx, maxy, win_grid, routable_layers,
                      layer_types, net, lazy=True)
    if win.cols * win.rows * n_layers > _MAX_LAZY_WINDOW_NODES:
        return None  # even `max_grid_mm` cannot fit a board this big - give up
    # No `_prefilter_window_obstacles` here on purpose: the window IS the board,
    # so every obstacle is in scope and `_lazy_build` applies the same same-net /
    # bbox filters itself while indexing.
    win.build(active_obstacles, track_half, via_radius,
              rules["clearance"], rules["edge_clearance"])

    s_cell = win.nearest_free(from_xy[0], from_xy[1], start_layers,
                              toward_xy=to_xy if pad_escape_aware else None) or win.cell_of(*from_xy)
    g_cell = win.nearest_free(to_xy[0], to_xy[1], list(goal_layers),
                              toward_xy=from_xy if pad_escape_aware else None) or win.cell_of(*to_xy)
    win_cong = _project_congestion(win, congestion, board_min[0], board_min[1], grid)
    # Corridor is deliberately NOT applied: the whole point of this tier is to
    # find a path the global stage's corridor never contemplated (a long detour
    # the other way round an obstruction).
    path = _fine_search(backend, win, net_kind, ctx["weights"], ctx["layer_purpose"],
                        ctx["directions"], s_cell, start_layers, g_cell, goal_layers,
                        home_layer, None, win_cong, plane_layers, goal_planes,
                        ctx["plane_step"], ctx["attachment_via_cost"],
                        ml_attach, return_path, goal_field,
                        _settings=ctx.get("gpu_settings"))
    if path is None:
        return None

    rec_updates, segments, vias, _violations = _finalize_core(
        ctx, net, win, path, from_xy, to_xy, active_obstacles,
        max(span_x, span_y), plane_layers, conn)
    if rec_updates is None:
        # The whole-board path skims real copper. Terminal for this tier (same
        # convention as `_route_hierarchical`): fall back to the caller's ordinary
        # failure report rather than emitting geometry the self-check rejected.
        return None
    rec_updates = dict(rec_updates)
    rec_updates["wide_lazy_window"] = {"grid_mm": round(win_grid, 6),
                                       "cols": win.cols, "rows": win.rows,
                                       "nodes": win.cols * win.rows * n_layers}
    return rec_updates, segments, vias, win


# --------------------------------------------------------------------------- #
# Hierarchical (multi-window) last-resort tier.
#
# Empirical root cause (kiln board, confirmed against the committed snapshot):
# 6 long-haul nets (40-113mm span) exhaust the ENTIRE `_route_attempts` ladder,
# failing even at its widest rung (margin=`_MAX_WINDOW_SPAN_MM`). Every one
# fails with the goal-adjacent obstacle 0.0-1.9mm away (tight quarters, not an
# open board area) - `_choose_grid`'s node-budget math forces those wide-margin
# rungs onto a grid coarse enough to step OVER the real, sub-2mm channel a
# route needs to thread between hand-routed copper/pours near the endpoint.
# It is a local-channel-width problem at large scale, not a lack of detour
# room (see NETCLASS_PLAN.md).
#
# Fix: don't search one huge window at a coarse grid. Chain many SMALL
# fine-grid windows along the connection's own already-computed coarse path
# (`gconn["candidates"][0]["coarse_path"]`) - each sub-window's span is small
# enough (`_HIER_CHUNK_SPAN_MM`) to afford the board's FINEST configured grid
# at the same node budget, so it can find the tight channel the coarse rungs
# stepped over.
# --------------------------------------------------------------------------- #

# Deterministic coarse-path decimation stride (mm): consecutive hierarchical
# waypoints are spaced roughly this far apart along the global stage's winning
# coarse path. Chosen so a sub-window's bbox (this span + 2x the margin below)
# stays comfortably inside `_MAX_WINDOW_NODES` at the FINEST grid even on a
# 4-layer board (a few thousand nodes, not hundreds of thousands) - the whole
# point is affording the fine grid the coarse ladder's wide rungs cannot.
_HIER_CHUNK_SPAN_MM = 8.0
# Small, fixed bbox pad per sub-window. Kept tight (not doubled/escalated like
# the main ladder) because a sub-window's endpoints are already only
# `_HIER_CHUNK_SPAN_MM` apart on a path the coarse stage already proved is
# geometrically plausible - room isn't the problem this tier exists to solve.
_HIER_WINDOW_MARGIN_MM = 3.0


def _hier_world_waypoints(
    gconn: dict[str, Any] | None, coarse_grid: float, coarse_min: tuple[float, float],
    from_xy: tuple[float, float], to_xy: tuple[float, float],
    candidate_index: int = 0,
) -> list[tuple[float, float, str]] | None:
    """Deterministically decimate the global stage's winning coarse path (a
    (cx, cy, layer) cell sequence at `coarse_grid` resolution, see
    `_corridor_from_global`) into a sequence of WORLD (x, y, layer) waypoints
    spaced ~`_HIER_CHUNK_SPAN_MM` apart along the path, always anchored at the
    connection's exact `from_xy`/`to_xy`. A pure, deterministic distance-
    accumulation walk over an already-deterministic input (same coarse_path
    and span in => same waypoints out, always - no iteration-order or
    randomness dependence). Returns None when there is no coarse path to chain
    sub-windows along (nothing for this tier to do)."""
    if not gconn or not gconn.get("candidates"):
        return None
    cands = gconn["candidates"]
    if candidate_index >= len(cands):
        return None
    coarse_path = cands[candidate_index].get("coarse_path") or []
    if not coarse_path:
        return None
    cmnx, cmny = coarse_min
    world_pts: list[tuple[float, float, str]] = []
    for entry in coarse_path:
        cx, cy, layer = entry[0], entry[1], entry[2]
        wx = cmnx + (cx + 0.5) * coarse_grid
        wy = cmny + (cy + 0.5) * coarse_grid
        world_pts.append((wx, wy, layer))
    if not world_pts:
        return None

    waypoints: list[tuple[float, float, str]] = [(from_xy[0], from_xy[1], world_pts[0][2])]
    acc = 0.0
    last_xy = (from_xy[0], from_xy[1])
    n = len(world_pts)
    for i, (x, y, layer) in enumerate(world_pts):
        acc += math.hypot(x - last_xy[0], y - last_xy[1])
        last_xy = (x, y)
        if acc >= _HIER_CHUNK_SPAN_MM and i != n - 1:
            waypoints.append((x, y, layer))
            acc = 0.0
    waypoints.append((to_xy[0], to_xy[1], world_pts[-1][2]))

    # Drop degenerate (near-zero-length) legs so every sub-window has a
    # genuine, non-zero span.
    deduped: list[tuple[float, float, str]] = [waypoints[0]]
    for wx, wy, layer in waypoints[1:]:
        px, py, _pl = deduped[-1]
        if math.hypot(wx - px, wy - py) > _EMIT_EPS_MM:
            deduped.append((wx, wy, layer))
    return deduped if len(deduped) >= 2 else None


def _route_hierarchical(
    ctx: dict[str, Any], net: str, net_kind: str,
    from_xy: tuple[float, float], to_xy: tuple[float, float],
    start_layers: list[str], goal_layers: set[str],
    active_obstacles: list["_Obst"], gconn: dict[str, Any] | None,
    home_layer: str | None,
    plane_layers: dict[str, list[dict[str, Any]]] | None,
    goal_planes: dict[str, list[dict[str, Any]]] | None,
    candidate_index: int = 0,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]] | None:
    """Last-resort tier: chain small fine-grid `_FineWindow`s along the global
    stage's own coarse path, ONLY called after the full `_route_attempts`
    ladder has already failed every rung (see the call site in `_route_one`).

    Stitching (no coordinate transform needed): `_route_to_emit` already emits
    segment/via records in ABSOLUTE board mm (`win.node_xy` folds in the
    window's own origin: `self.minx + ix*self.grid`), so successive sub-
    windows' emitted geometry concatenates directly.

    Self-check (seam-safe end-to-end): `_self_check`/the cost math below run
    ONCE against the FULL concatenated segment/via list from every leg, using
    `active_obstacles` - the same full board-global obstacle list a normal
    single-window connection is proven against - so a seam between two legs
    gets exactly the same exact-clearance guarantee any other connection's
    route gets, with zero changes to the self-check machinery itself. A
    leg's own same-net copper is free to a later leg (the ordinary same-net
    exemption in `_Obst`/`obstacle_cells`), so legs never falsely block each
    other.

    Rip-up: intentionally NOT wired in this landing - a hierarchical result is
    terminal (routed, or hard-failed back to the ordinary `unreachable_in_
    window` report), exactly how a `self_check_failed` result is treated
    today. Left for a future landing (see NETCLASS_PLAN.md).

    Neck-down (7.12): also NOT wired here - this tier runs its own inline
    `_route_to_emit`/`_self_check` rather than `_finalize_core`, and this
    rare last-resort path (only reached once the whole `_route_attempts`
    ladder has failed) is deliberately left out of neck-down's scope rather
    than risking this tier's own seam-safety self-check guarantee. A
    connection that only routes via this tier emits at full class width even
    onto a small pad - see `route_nets`'s docstring HONEST RESIDUAL note.

    Determinism: a pure function of (from_xy, to_xy, the coarse path, the
    given obstacle/layer state) - the same decimation walk, the same per-leg
    A* (already deterministic), the same concatenation order every call.

    Returns None (never a partial/half-stitched result) when: there is no
    coarse path to chain along, any leg is unreachable even at the finest
    grid, or the end-to-end self-check rejects the stitched geometry."""
    coarse_grid = ctx["coarse_grid"]
    coarse_min = ctx["coarse_min"]
    waypoints = _hier_world_waypoints(gconn, coarse_grid, coarse_min, from_xy, to_xy,
                                      candidate_index)
    if waypoints is None:
        return None

    routable_layers = ctx["routable_layers"]
    routable_set = ctx["routable_set"]
    layer_types = ctx["layer_types"]
    board_bbox = ctx["board_bbox"]
    grid = ctx["grid"]
    rules = ctx["rules"]
    track_half = ctx["track_half"]
    via_radius = ctx["via_radius"]
    weights = ctx["weights"]
    layer_purpose = ctx["layer_purpose"]
    directions = ctx["directions"]
    backend = ctx["backend"]
    plane_step = ctx["plane_step"]
    attachment_via_cost = ctx["attachment_via_cost"]
    max_window_nodes = ctx["max_window_nodes"]
    # Phase 7.3d: per-leg "other endpoint" bias (settings default False) -
    # see `nearest_free`'s docstring and `_route_one`'s identical read.
    pad_escape_aware = bool(ctx.get("pad_escape_direction_aware", False))
    # Phase 7.18: identical reads to `_route_one`, so this last-resort tier
    # costs a leg exactly the way the ordinary tier costs a window.
    ml_attach = bool(ctx.get("multilayer_attachment", False))
    # Phase 7.19.1 obstacle-aware goal-distance heuristic (default off).
    goal_field = bool(ctx.get("goal_field_heuristic", False))
    return_path = (ctx.get("return_path_by_net") or {}).get(net)

    all_segments: list[dict[str, Any]] = []
    all_vias: list[dict[str, Any]] = []
    cur_layers = list(start_layers)
    n_legs = len(waypoints) - 1

    for leg in range(n_legs):
        leg_from = (waypoints[leg][0], waypoints[leg][1])
        leg_to = (waypoints[leg + 1][0], waypoints[leg + 1][1])
        leg_path_layer = waypoints[leg + 1][2]
        is_last = leg == n_legs - 1

        if is_last:
            leg_goal_layers = goal_layers
            leg_goal_planes = goal_planes
        else:
            leg_goal_layers = ({leg_path_layer} if leg_path_layer in routable_set
                               else set(routable_layers))
            leg_goal_planes = None

        minx = max(min(leg_from[0], leg_to[0]) - _HIER_WINDOW_MARGIN_MM, board_bbox[0] - grid)
        miny = max(min(leg_from[1], leg_to[1]) - _HIER_WINDOW_MARGIN_MM, board_bbox[1] - grid)
        maxx = min(max(leg_from[0], leg_to[0]) + _HIER_WINDOW_MARGIN_MM, board_bbox[2] + grid)
        maxy = min(max(leg_from[1], leg_to[1]) + _HIER_WINDOW_MARGIN_MM, board_bbox[3] + grid)
        win = _FineWindow(minx, miny, maxx, maxy, grid, routable_layers, layer_types, net)
        if win.cols * win.rows * max(1, len(routable_layers)) > max_window_nodes:
            return None  # a chunk still too big for the node budget - bail, no partial emit

        leg_obstacles = _prefilter_window_obstacles(
            active_obstacles, net, leg_from, leg_to, board_bbox, grid,
            [(_HIER_WINDOW_MARGIN_MM, grid)], track_half, via_radius,
            rules["clearance"], rules["edge_clearance"])
        win.build(leg_obstacles, track_half, via_radius, rules["clearance"], rules["edge_clearance"])

        # A generous escape ring (not the pad-escape default of 6 = 1.2mm):
        # an INTERMEDIATE waypoint is a decimated coarse-path point, not a
        # pad, and can land deep inside a plane pour (tracks near-fully
        # blocked there, see the class docstring) with no free cell within a
        # tight ring - a large ring still finds the nearest real channel
        # cheaply (the sub-window itself is small).
        ring = max(win.cols, win.rows)
        s_cell = win.nearest_free(leg_from[0], leg_from[1], cur_layers, max_ring=ring,
                                  toward_xy=leg_to if pad_escape_aware else None) or win.cell_of(*leg_from)
        g_cell = win.nearest_free(leg_to[0], leg_to[1], list(leg_goal_layers), max_ring=ring,
                                  toward_xy=leg_from if pad_escape_aware else None) or win.cell_of(*leg_to)
        path = _fine_search(backend, win, net_kind, weights, layer_purpose, directions,
                            s_cell, cur_layers, g_cell, leg_goal_layers,
                            home_layer, None, None, plane_layers, leg_goal_planes,
                            plane_step, attachment_via_cost, ml_attach, return_path,
                            goal_field,
                            _settings=ctx.get("gpu_settings"))
        if path is None and not is_last and leg_goal_layers != set(routable_layers):
            # Intermediate waypoint only: the coarse stage's layer preference
            # there isn't binding, only its (x, y) location is - retry with any
            # routable layer before giving up on this leg.
            leg_goal_layers = set(routable_layers)
            g_cell = win.nearest_free(leg_to[0], leg_to[1], list(leg_goal_layers), max_ring=ring,
                                      toward_xy=leg_from if pad_escape_aware else None) or win.cell_of(*leg_to)
            path = _fine_search(backend, win, net_kind, weights, layer_purpose, directions,
                                s_cell, cur_layers, g_cell, leg_goal_layers,
                                home_layer, None, None, plane_layers, leg_goal_planes,
                                plane_step, attachment_via_cost, ml_attach, return_path,
                                goal_field,
                                _settings=ctx.get("gpu_settings"))
        if path is None:
            return None  # this leg is unreachable even at the finest grid - terminal

        segs, vias = _route_to_emit(win, path, leg_from, leg_to, plane_layers)
        all_segments.extend(segs)
        all_vias.extend(vias)
        cur_layers = [path[-1][2]]

    if not all_segments and not all_vias:
        return None
    violations = _self_check(net, all_segments, all_vias, active_obstacles, rules, via_radius)
    if violations:
        return None  # stitched result fails the end-to-end exact-clearance pass

    length = sum(math.hypot(s["x2"] - s["x1"], s["y2"] - s["y1"]) for s in all_segments)
    layers_used = sorted({s["layer"] for s in all_segments},
                         key=lambda l: routable_layers.index(l) if l in routable_layers else 999)
    tw = ctx["tw"]
    est_cost = (length * float(tw.get("length_mm", 1.0)) + len(all_vias) * float(tw.get("via", 5.0)))
    rec_updates = {
        "routed": True, "length_mm": round(length, 4), "via_count": len(all_vias),
        "layers": layers_used, "segment_count": len(all_segments),
        "window_margin_mm": _HIER_WINDOW_MARGIN_MM, "grid_mm": grid,
        "est_phase6_cost": round(est_cost, 4),
        "self_check": {"passed": True, "violation_count": 0},
        "hierarchical": True, "hierarchical_legs": n_legs,
    }
    return rec_updates, all_segments, all_vias


# Per-process context for the multiprocessing pool (set once by the initializer,
# read-only thereafter). Workers only COMPUTE window -> path; never commit.
_WORKER_CTX: dict[str, Any] | None = None


def _worker_init(ctx: dict[str, Any]) -> None:
    """Pool initializer: runs ONCE per worker process at startup (not per task).

    `ctx` here is the LIGHT variant `_run_independent_routes` builds - it omits
    `base_obstacles` and `plane_by_net` (the two keys whose pickled payload used
    to dominate pool start-up cost: thousands of `_Obst`/`_FillRaster` objects)
    and instead carries a small `_obstacle_recipe` of picklable primitives (a
    board path string + the layer/clearance/pattern inputs `_collect_obstacles`
    needs). Each worker rebuilds its OWN `base_obstacles` and a lazy
    `plane_by_net` (`_LazyPlaneByNet`) from that recipe, via the SAME pure
    functions the parent used to build them - bit-identical content, just
    computed locally instead of shipped through `pickle.dump`, which is what
    made the old initializer slow (the obstacle list serialized at ~20s per
    worker on the real kiln board; recomputing it from the cached board parse
    is a small fraction of that)."""
    global _WORKER_CTX
    ctx = dict(ctx)
    recipe = ctx.pop("_obstacle_recipe", None)
    if "base_obstacles" not in ctx and recipe is not None:
        (board_path_str, routable_set, all_cu, edge_clearance, power_patterns,
         plane_grid_mm, plane_clearance_mm) = recipe
        board_path = Path(board_path_str)
        ctx["base_obstacles"] = _collect_obstacles(
            board_path, routable_set, all_cu, edge_clearance, power_patterns)
        if "plane_by_net" not in ctx:
            plane_fill_index = _plane_fill_index_with_estimated(
                board_path, plane_grid_mm, plane_clearance_mm)
            plane_footprints = _pcb._parse_footprint_pads_cached(board_path)
            plane_tracks = _pcb._parse_tracks_cached(board_path)
            plane_pads_by_net = _group_pads_by_net(plane_footprints)
            plane_stack_order = {name: i for i, name in enumerate(all_cu)}
            ctx["plane_by_net"] = _LazyPlaneByNet(
                plane_fill_index, power_patterns, plane_footprints, plane_tracks,
                plane_pads_by_net, plane_stack_order, all_cu, routable_set,
                ctx.get("plane_island_base", 40.0),
                ctx.get("plane_orphan_island_cost", 1000.0))
    _WORKER_CTX = ctx


def _worker_route_speculative(item: tuple[int, dict[str, Any]]) -> tuple[int, dict[str, Any]]:
    """Route one connection against the BASE obstacles only (no placements yet,
    empty congestion, corridor bias on) - the speculative-parallel-pass unit.
    Returns (owner, picklable-out) with the heavy `_FineWindow` stripped. This
    is run for EVERY connection now (not just spatially-independent ones): it
    is a pure function of (ctx, conn, base_obstacles, {}), so its result for a
    given owner is identical regardless of which worker computes it or how many
    workers exist. The parent (never this function) decides, in canonical
    order, whether a result can be committed as-is or must be re-routed
    serially because it conflicts with an earlier commit from this same pass -
    that decision, not this computation, is where determinism is enforced."""
    owner, conn = item
    ctx = _WORKER_CTX
    assert ctx is not None
    out = _route_one(ctx, conn, ctx["base_obstacles"], {}, use_corridor=True)
    return owner, {"routed": out["routed"], "net": out["net"],
                   "segments": out["segments"], "vias": out["vias"], "rec": out["rec"]}


def _resolve_workers(settings: dict[str, Any]) -> int:
    """Resolve `autorouter.cpu.workers`: 0 (or <0) = auto = `cpu_count - 1`
    (>=1). A value of 1 forces the serial reference path."""
    import os
    cfg = settings.get("autorouter", {}).get("cpu", {}) or {}
    raw = cfg.get("workers", 0)
    try:
        raw = int(raw)
    except (TypeError, ValueError):
        raw = 0
    if raw >= 1:
        return raw
    return max(1, (os.cpu_count() or 1) - 1)


def _run_independent_routes(
    ctx: dict[str, Any], items: list[tuple[int, dict[str, Any]]], workers: int,
) -> dict[int, dict[str, Any]]:
    """Speculatively route every connection in `items` against the BASE board
    (no other connections' copper), in parallel across processes when
    `workers > 1`. Falls back to in-process serial on a single worker/item or
    any pool error. Result is keyed by owner id and independent of worker
    count - each entry is a pure function of (ctx, conn, base_obstacles, {})
    (see `_worker_route_speculative`), so it is bit-identical for any worker
    count or submission order. The CALLER (`route_nets`) is what enforces
    determinism: it walks results in canonical owner order and commits only
    when a result's copper self-checks clean against everything already
    committed this pass, re-queuing any conflict into the serial worklist -
    so parallel EXECUTION order never leaks into the committed board.

    The pool initializer receives a LIGHT ctx (an `_obstacle_recipe` in place
    of `base_obstacles`/`plane_by_net`) so each worker rebuilds its own copy of
    the board-derived obstacle/plane state locally instead of having it shipped
    through `pickle` - see `_worker_init`. This is what makes a wide worker
    pool actually pay off: profiling showed pickling `base_obstacles` (every
    copper item + zone-fill raster on the board) was, by a wide margin, the
    dominant cost of the old (narrower) parallel phase, dwarfing the actual
    routing compute it was meant to parallelize."""
    results: dict[int, dict[str, Any]] = {}
    if workers <= 1 or len(items) <= 1:
        for owner, conn in items:
            out = _route_one(ctx, conn, ctx["base_obstacles"], {}, use_corridor=True)
            results[owner] = {"routed": out["routed"], "net": out["net"],
                              "segments": out["segments"], "vias": out["vias"], "rec": out["rec"]}
        return results
    try:
        worker_ctx = dict(ctx)
        board_path_str = worker_ctx.pop("_board_path", None)
        if board_path_str is not None:
            worker_ctx.pop("base_obstacles", None)
            worker_ctx.pop("plane_by_net", None)
            worker_ctx["_obstacle_recipe"] = (
                board_path_str, worker_ctx["routable_set"], worker_ctx["all_cu"],
                worker_ctx["rules"]["edge_clearance"], worker_ctx["power_patterns"],
                worker_ctx.get("plane_grid_mm", 0.2), worker_ctx.get("plane_clearance_mm", 0.2))
        from concurrent.futures import ProcessPoolExecutor
        with ProcessPoolExecutor(max_workers=min(workers, len(items)),
                                 initializer=_worker_init, initargs=(worker_ctx,)) as ex:
            for owner, res in ex.map(_worker_route_speculative, items):
                results[owner] = res
    except Exception:
        # Any spawn/pickle/executor failure: fall back to the serial reference
        # path (identical results), so a worker problem never fails the run.
        results.clear()
        for owner, conn in items:
            out = _route_one(ctx, conn, ctx["base_obstacles"], {}, use_corridor=True)
            results[owner] = {"routed": out["routed"], "net": out["net"],
                              "segments": out["segments"], "vias": out["vias"], "rec": out["rec"]}
    return results


def _feasibility_screen(ctx: dict[str, Any], conn: dict[str, Any],
                        obstacles: list["_Obst"], screen_grid: float = 1.0,
                        node_cap: int = 600) -> int:
    """Cheap coarse-grid BFS estimate of a connection's routing difficulty.

    Used ONLY to ORDER work submitted to the parallel pool (easy connections
    first) - it is a heuristic for SCHEDULING, never a gate: a connection this
    screen scores as hard (returns `node_cap`) still gets the full
    `_route_attempts` ladder later, exactly like every other connection. The
    model is deliberately much weaker than the real router: a single merged
    layer (a cell is impassable only when every routable layer's obstacle
    footprint covers it - a via can always change layer, so this is the most
    optimistic view), no clearance geometry beyond a coarse bbox reject, no
    cost weights - unweighted 8-connected BFS at a coarse (1 mm default) grid
    over just the connection's base-margin window. This makes screening every
    connection cost a small fraction of routing even a single one, so it can
    run serially over the whole worklist before the parallel pass without
    itself becoming a bottleneck."""
    from_xy, to_xy = _conn_endpoints(conn)
    margin = ctx["base_margin"]
    board_bbox = ctx["board_bbox"]
    grid = max(screen_grid, ctx["grid"])
    minx = max(min(from_xy[0], to_xy[0]) - margin, board_bbox[0] - grid)
    miny = max(min(from_xy[1], to_xy[1]) - margin, board_bbox[1] - grid)
    maxx = min(max(from_xy[0], to_xy[0]) + margin, board_bbox[2] + grid)
    maxy = min(max(from_xy[1], to_xy[1]) + margin, board_bbox[3] + grid)
    cols = max(2, int(math.ceil((maxx - minx) / grid)) + 1)
    rows = max(2, int(math.ceil((maxy - miny) / grid)) + 1)
    net = conn.get("net")
    blocked: set[tuple[int, int]] = set()
    reach_pad = grid
    for ob in obstacles:
        if ob.net == net and not ob.is_edge:
            continue
        if (ob.maxx < minx - reach_pad or ob.minx > maxx + reach_pad
                or ob.maxy < miny - reach_pad or ob.miny > maxy + reach_pad):
            continue
        ix0 = max(0, int((ob.minx - reach_pad - minx) / grid))
        ix1 = min(cols - 1, int((ob.maxx + reach_pad - minx) / grid) + 1)
        iy0 = max(0, int((ob.miny - reach_pad - miny) / grid))
        iy1 = min(rows - 1, int((ob.maxy + reach_pad - miny) / grid) + 1)
        for iy in range(iy0, iy1 + 1):
            for ix in range(ix0, ix1 + 1):
                blocked.add((ix, iy))

    def cell_of(x: float, y: float) -> tuple[int, int]:
        ix = min(max(int(round((x - minx) / grid)), 0), cols - 1)
        iy = min(max(int(round((y - miny) / grid)), 0), rows - 1)
        return ix, iy

    start, goal = cell_of(*from_xy), cell_of(*to_xy)
    if start == goal:
        return 0
    from collections import deque as _dq
    q: "_dq[tuple[int, int]]" = _dq([start])
    seen = {start}
    expansions = 0
    while q and expansions < node_cap:
        cx, cy = q.popleft()
        expansions += 1
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)):
            nx, ny = cx + dx, cy + dy
            if not (0 <= nx < cols and 0 <= ny < rows):
                continue
            if (nx, ny) in seen or (nx, ny) in blocked:
                continue
            if (nx, ny) == goal:
                return expansions
            seen.add((nx, ny))
            q.append((nx, ny))
    return node_cap


# =========================================================================== #
# Phase 7.9 - live progress viewer: JSONL event stream + cancel-flag support.
#
# Companion stream to `<board>.board_local.json`, same "disposable, machine-
# local, gitignored" contract: `<board_stem>.route_progress.jsonl` next to the
# board file. Append-only while `route_nets`/`route_board` run; TRUNCATED at
# the start of each call (never accumulates across runs, so a stale file from
# a previous session can't be misread as live). A separate process
# (`kicad_route_viewer.py`) tails this file - the router never talks to the
# viewer directly, it only ever appends here. See NETCLASS_PLAN.md Phase 7.9.
#
# `_load_kicad_layer_colors` lives in `kicad_route_viewer.py` (so the color-
# resolution logic is testable/importable without pulling in tkinter) but is
# ALSO needed here to bake the resolved palette into the header event (the
# viewer stays a dumb renderer - no KiCad-config knowledge in the GUI
# process, and a recorded event file replays with the colors it was recorded
# with). Imported defensively: a broken/missing viewer module must never take
# down routing, only progress coloring.
try:
    from kicad_route_viewer import _load_kicad_layer_colors as _viewer_load_colors
except Exception:
    def _viewer_load_colors(color_theme: str = "auto") -> dict[str, Any]:
        return {}


def _tk_available() -> bool:
    """Whether this Python environment can import tkinter at all (headless CI/
    containers frequently cannot). The viewer is observational-only, so a
    missing tkinter must degrade to a clear reported reason, never a crash -
    see `open_route_viewer`."""
    try:
        import tkinter  # noqa: F401
    except Exception:
        return False
    return True


def _progress_path(project_path: str | Path) -> Path:
    board_path, _, _ = _pcb._resolve_project_path(project_path)
    return board_path.with_name(f"{board_path.stem}.route_progress.jsonl")


def _progress_reset(project_path: str | Path) -> Path:
    """Truncate (or create) the progress file at the START of a route call -
    each run's stream starts clean, so a viewer that (re)opens mid-run never
    replays a previous run's events."""
    path = _progress_path(project_path)
    try:
        path.write_text("", encoding="utf-8", newline="\n")
    except OSError:
        pass
    return path


def _progress_append(path: Path | None, event: dict[str, Any]) -> None:
    """Append one JSONL event. Best-effort: a progress-file write failure
    (disk full, permissions) must never fail the actual route."""
    if path is None:
        return
    try:
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(event) + "\n")
    except OSError:
        pass


def _board_geometry_snapshot(board_path: Path) -> dict[str, Any]:
    """Existing copper (segments/vias) + zone outlines, for the viewer's
    initial draw before any progress event arrives. Reuses the same cached
    parsers the rest of the router already uses - no new board-parsing logic
    is introduced for this feature."""
    tracks = _pcb._parse_tracks_cached(board_path)
    segments = [
        {
            "uuid": s.get("uuid") or "", "net": s.get("net", ""), "layer": s.get("layer", ""),
            "width": s.get("width", 0.0), "start": s.get("start"), "end": s.get("end"),
        }
        for s in tracks.get("segments", []) if s.get("uuid")
    ]
    vias = [
        {
            "uuid": v.get("uuid") or "", "net": v.get("net", ""),
            "size": v.get("size", 0.0), "drill": v.get("drill", 0.0),
            "layers": v.get("layers", []), "at": v.get("at"),
        }
        for v in tracks.get("vias", []) if v.get("uuid")
    ]
    zones: list[dict[str, Any]] = []
    try:
        zones = list_zones(str(board_path)).get("zones", [])
    except Exception:
        zones = []
    return {"segments": segments, "vias": vias, "zones": zones}


def _progress_header_event(
    project_path: str | Path, board_path: Path, settings: dict[str, Any], session: dict[str, Any],
) -> dict[str, Any]:
    progress_cfg = (settings.get("autorouter", {}) or {}).get("progress", {}) or {}
    color_theme = str(progress_cfg.get("color_theme", "auto"))
    try:
        colors = _viewer_load_colors(color_theme)
    except Exception:
        colors = {}
    return {
        "event": "header",
        "ts": time.time(),
        "session": session,
        "board_path": str(board_path),
        "colors": colors,
        "geometry": _board_geometry_snapshot(board_path),
        # No 7.6/7.7 optimizer/decision protocol exists yet - this is a
        # left-in hook so a future decision event slots in without a schema
        # change (see the viewer's "awaiting_decision" banner hook).
        "decision_protocol": None,
    }


def _reset_route_cancel_flag(project_path: str | Path) -> None:
    """Clear a stale `route_cancel_requested` flag at the START of a run - a
    stop request only ever applies to the run that receives it, never
    silently cancels the NEXT call too."""
    try:
        state = _pcb.load_board_local(project_path)
        data = state["data"]
        if data.get("route_cancel_requested"):
            data["route_cancel_requested"] = False
            _pcb.save_board_local(project_path, data)
    except Exception:
        pass


def _route_cancel_requested(project_path: str | Path) -> bool:
    """Poll the board-local cancel flag the viewer's 'Stop after this
    iteration' button writes. Best-effort: a read failure must never abort a
    route that would otherwise have succeeded."""
    try:
        return bool(_pcb.load_board_local(project_path)["data"].get("route_cancel_requested"))
    except Exception:
        return False


def open_route_viewer(
    project_path: str | Path, board: str | None = None, auto_close: bool = False,
) -> dict[str, Any]:
    """Phase 7.9 - spawn the detached `kicad_route_viewer.py <board_path>`
    process that tails `<board>.route_progress.jsonl`. Decoupled by
    construction: the router only ever appends to that file, so the viewer
    can be opened, closed, or crash without touching (or blocking) routing.
    Also called internally by `route_nets`/`route_board` when
    `autorouter.progress.open_viewer` is true (with `auto_close=True` - see
    below).

    `auto_close`: when True, the spawned viewer closes itself a few seconds
    after it sees `run_complete` in the event stream. This is for the
    UNATTENDED case - a session/pipeline auto-launched the viewer via the
    settings knob, nobody necessarily asked to sit and watch it, so it should
    not linger after the route is done. The explicit `open_kicad_route_viewer`
    MCP tool call (a human/session deliberately asking to watch) always
    passes `auto_close=False` (the default) so that window stays up for
    review at the viewer's own pace, same as before this existed.

    Observational-only failure honesty: if tkinter is unavailable (headless
    CI/container), this returns `{"launched": False, "reason": ...}` instead
    of raising - the MCP server must keep running headless even though the
    viewer cannot.
    """
    if not _tk_available():
        return {
            "launched": False,
            "reason": (
                "tkinter is not available in this Python environment; the route "
                "viewer is observational-only and cannot run headless."
            ),
        }
    board_path, _, _ = _pcb._resolve_project_path(project_path)
    if board:
        board_path = Path(board)
    viewer_script = Path(__file__).resolve().with_name("kicad_route_viewer.py")
    if not viewer_script.exists():
        return {"launched": False, "reason": f"viewer script not found: {viewer_script}"}
    popen_kwargs: dict[str, Any] = {
        "cwd": str(viewer_script.parent),
        "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        popen_kwargs["start_new_session"] = True
    argv = [sys.executable or "python", str(viewer_script), str(board_path)]
    if auto_close:
        argv.append("--auto-close")
    try:
        proc = subprocess.Popen(argv, **popen_kwargs)
    except OSError as exc:
        return {"launched": False, "reason": f"failed to launch viewer subprocess: {exc}"}
    return {
        "launched": True, "pid": proc.pid, "board_path": str(board_path),
        "viewer_script": str(viewer_script), "auto_close": auto_close,
    }


def route_nets(
    project_path: str | Path,
    nets: list[str] | None = None,
    connections: list[dict[str, Any]] | None = None,
    write: bool = False,
    allow_while_open: bool = False,
    max_ripup_iterations: int | None = None,
    refill_zones: bool = False,
    allow_hand_copper_ripup: bool = False,
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

    ADAPTIVE DETAILED GRID (see `_choose_grid`): each connection's window is
    built at the coarsest-as-needed grid that keeps `cols * rows * n_layers`
    within `_MAX_WINDOW_NODES` - `autorouter.grid_mm` (fine, 0.2 mm default)
    when the window already fits, coarsening deterministically (a pure
    function of span/layers/budget) up to `autorouter.max_grid_mm` (1.0 mm
    default) only when it doesn't. This is what lets long-haul connections
    (a bus, a power rail spanning most of the board) route at all instead of
    failing `window_too_large` outright; short connections are unaffected -
    same grid, same geometry as before. The chosen grid is reported per
    connection as `grid_mm`. Coarsening the SEARCH grid never weakens safety:
    `_self_check` proves the emitted geometry against all copper at exact
    netclass clearance regardless of what grid found the path, so a coarse
    path that skims an obstacle still fails self-check and is never emitted.

    STEP 4 (rip-up & reroute, negotiated congestion) IS ACTIVE. When a
    connection cannot route in its window (A* found no path at all), the
    window's obstacle cells are cleared INCREMENTALLY of the autorouter-owned
    copper on the freed path (never a full rebuild), the blocking autorouter
    connections are RIPPED (human/board copper is NEVER ripped - a net blocked
    solely by human copper fails with the blocker named), a `congestion` cost is
    escalated on the contested cells, and the ripped connections are re-queued
    to re-route (their corridor choice may change) - bounded by
    `max_ripup_iterations`. A self-check failure (proposed copper clears the A*
    obstacle model but not the exact clearance pass - a plane-skim) is ALSO
    demoted through the same rip-up step when the skim's violations are against
    rippable autorouter-placed copper: the offending placements are ripped and
    the SAME found path (not a fresh search - it was already geometrically fine
    except for those specific conflicts) is re-finalized/re-self-checked against
    the reduced obstacle set. A plane-skim against non-rippable copper (a filled
    zone/plane, a pad, an edge, hand-routed copper - `owner is None` on every
    violation) cannot be helped by rip-up and correctly stays a hard failure. A
    displaced net does not immediately rip the net that displaced it
    (anti-thrash), and every decision is integer-milli / canonically ordered, so
    a given input routes identically run to run. The result reports
    `ripup_active: true` plus per-run rip-up stats (`ripup_iterations`,
    `connections_ripped`, `congestion_escalations`).

    `allow_hand_copper_ripup` (default False - NETCLASS_PLAN item 10): when
    True, the SAME two Step-4 branches above ALSO consider owner-is-None
    obstacles rippable, IF AND ONLY IF they are hand-routed TRACK/ARC
    segments or VIAS (`_is_hand_copper_obstacle` - never a footprint pad,
    never a zone fill, never Edge.Cuts; those stay hard blockers exactly as
    today regardless of this flag). This is a PER-CALL argument, not a
    persisted `pcb_settings.json` field - deliberately: ripping a human's own
    routed copper is a materially more destructive action than ripping the
    autorouter's own prior placements (the everyday case Step 4 already
    handles), so "explicit permission" is asked fresh on every invocation
    that wants it, the same way `write=True` is never inherited from a
    settings file either. Default False means every existing caller/test that
    does not pass this flag gets byte-identical behavior to before this
    feature landed - human copper is NEVER touched. Each hand-copper piece
    that gets ripped is reported once, with its board uuid/net/kind/layer/
    geometry, in `human_copper_ripped` on both the owning connection's record
    and the top-level result (and `summary.human_copper_ripped_count`) - the
    audit trail a caller MUST review before ever trusting `write=True` against
    a real board. A ripped hand-copper obstacle is removed from the shared
    obstacle pool the moment it is ripped, so it can never be ripped a second
    time in the same run (the natural anti-thrash/determinism guard for
    unowned copper, which - unlike an autorouter connection id - has no
    "net that displaced it" to protect against re-ripping). write=True
    additionally deletes the exact ripped (segment)/(via) blocks from the
    board text via the same uuid-block-delete surgery `unroute_nets` already
    uses for autorouter-owned copper, so the write stays DRC-consistent with
    what was reported.

    Still simplified vs. the full spec (documented honestly): pad escape lands
    on the nearest free grid node rather than a pad-direction-aware exact stub.

    PHASE 7.12 (neck-down) IS ACTIVE for every connection routed through the
    normal `_finalize_core` path (the ordinary ladder in `_route_one`, its
    rip-up re-finalize calls, and the speculative parallel pass - all three
    call `_finalize_core` with the connection, see `_neck_targets_for_conn`):
    when a `from`/`to` endpoint is identified as a pad (`_item_id`'s `kind ==
    "pad"`) whose smaller copper dimension the net-class width would overrun
    by more than `neck_down.max_width_vs_pad`, the final stretch of copper at
    that endpoint (`_apply_neck_endpoint`) is emitted at a narrower
    `neck_width` instead - self-checked at that true narrow width, never the
    wide class width. `neck_down.enabled: false` (or an endpoint whose class
    width already fits its pad - the common case) leaves every segment
    without a `"width"` key, byte-identical to pre-7.12 emission.
    HONEST RESIDUAL: the Phase-5.x hierarchical last-resort tier
    (`_route_hierarchical`, only reached after the full `_route_attempts`
    ladder has failed every rung) does its own `_route_to_emit`/`_self_check`
    inline and is NOT wired for neck-down - a connection that only routes via
    that rare fallback tier lands at full class width even onto a small pad.
    Scoped out deliberately rather than touching that tier's own from-scratch
    self-check/emit path and risking its landed seam-safety guarantees for a
    corner this phase does not require.

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
    backend = _resolve_backend(settings)

    # Phase 7.9 live progress viewer: reset the JSONL event stream and any
    # stale cancel flag at the START of this call (never accumulate across
    # runs; a stop request only ever applies to the run that receives it).
    progress_cfg = autor.get("progress", {}) or {}
    progress_enabled = bool(progress_cfg.get("events", True))
    progress_path = _progress_reset(project_path) if progress_enabled else None
    _reset_route_cancel_flag(project_path)
    _progress_session = {
        "session_id": _uuid.uuid4().hex, "started": time.time(), "backend": backend,
        "command": "route_nets",
    }
    _progress_conns_done = 0
    cancelled = False
    # The node budget is INTENTIONALLY the same for both backends: it selects the
    # detailed grid (`_choose_grid`), so an identical budget is what guarantees
    # cpu and numpy pick the same grid and thus route-level bit-identical geometry
    # (7.8 parity). NOTE: a larger WINDOW (wider margin) at the same grid was
    # confirmed NOT to help kiln - most unrouted connections are a pad escape
    # sealed by clearance-inflated foreign copper LOCAL to the pad, not a lack of
    # room. The fix is a FINER grid (`min_grid_mm`), which places a node in the
    # sub-0.2 mm channel a hand route threads; the `_route_attempts` ladder tries
    # those on failure. See _MAX_WINDOW_NODES / `_route_attempts`.
    max_window_nodes = _MAX_WINDOW_NODES
    grid = float(autor.get("grid_mm", 0.2)) or 0.2
    max_grid_mm = float(autor.get("max_grid_mm", 1.0)) or 1.0
    if max_grid_mm < grid:
        max_grid_mm = grid
    min_grid_mm = float(autor.get("min_grid_mm", 0.05)) or 0.05
    if min_grid_mm > grid:      # a "finer" floor can never be coarser than base
        min_grid_mm = grid
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

    obstacles = _collect_obstacles(board_path, routable_set, all_cu, rules["edge_clearance"],
                                   power_patterns)
    board_bbox = _board_bbox(board_path)

    # -- 7.5.4 plane-aware routing: per-net own-fill components + costs ------ #
    plane_cfg = settings.get("plane", {}) or {}
    plane_step = float(plane_cfg.get("plane_step", 0.05))
    attachment_via_cost = float(plane_cfg.get("attachment_via", 8.0))
    island_base = float(plane_cfg.get("island_base", 40.0))
    orphan_island_cost = float(plane_cfg.get("orphan_island", 1000.0))
    # Phase 7.18.1 / 7.18.3 - both OFF by default (see DEFAULT_PCB_SETTINGS).
    ml_attach = bool(plane_cfg.get("multilayer_attachment_choice", False))
    return_path_bonus = float(plane_cfg.get("return_path_bonus", 0.0) or 0.0)
    plane_grid_mm = float(autor.get("grid_mm", 0.2)) or 0.2
    plane_clearance_mm = float(autor.get("clearance_fallback_mm", 0.2))
    plane_fill_index = _plane_fill_index_with_estimated(board_path, plane_grid_mm, plane_clearance_mm)
    _plane_footprints = _pcb._parse_footprint_pads_cached(board_path)
    _plane_tracks = _pcb._parse_tracks_cached(board_path)
    _plane_pads_by_net = _group_pads_by_net(_plane_footprints)
    _plane_stack_order = {name: i for i, name in enumerate(all_cu)}
    _plane_components_cache: dict[str, dict[str, list[dict[str, Any]]]] = {}

    # -- 7.12 neck-down: per-(ref, pad number) copper size, reusing the same
    #    footprint parse `_plane_footprints` already loaded above (no extra
    #    board read). Keyed exactly like `_item_id`'s pad identity (`ref`/
    #    `pad`), which is what a ratsnest connection's `from`/`to` carries. ---- #
    neck_cfg = settings.get("neck_down", {}) or {}
    pad_size_by_ref_num: dict[tuple[str, str], tuple[float, float]] = {}
    for fp in _plane_footprints.values():
        ref = fp.get("reference", "")
        for pad in fp["pads"]:
            size = pad.get("size") or {}
            sx = float(size.get("x", 0.0) or 0.0)
            sy = float(size.get("y", 0.0) or 0.0)
            pad_size_by_ref_num[(ref, pad.get("number", ""))] = (sx, sy)
    neck_min_width = 0.0
    if neck_cfg.get("enabled", True):
        try:
            neck_min_width = float(
                get_drc_constraints(project_path)["constraints"].get("track_width", {}).get("value") or 0.0)
        except Exception:
            neck_min_width = 0.0

    def _plane_components_for(net: str) -> dict[str, list[dict[str, Any]]] | None:
        """This net's own fill, per routable layer, as `[{"raster", "factor"}]`
        components (7.5.3 model: mainland factor 1.0, island `island_base /
        attachment_count`, orphan `orphan_island`) - None when `net` does not
        own a zone (i.e. is not a key in the fill index) at all. Both
        `_parse_zones_cached`-sourced (KiCad-filled, `fill_source: "kicad"`)
        AND the Phase 7.5.2 estimation fallback (`fill_source: "estimated"`,
        for a zone/layer KiCad has not filled yet) are considered - see
        `_plane_fill_index_with_estimated` (residual (a) of 7.5.4 now wired).

        POWER-NET GATE (user, 2026-07-24): filled zones are used for plane moves
        ONLY for power/ground nets (`_net_kind == "power"`: GND, 3V3/3.3V, 5V,
        12V, VCC/VDD, ... per `power_net_patterns`). A signal net that happens to
        own a fill gets no plane moves and routes as ordinary copper - a filled
        zone is never treated as routable plane for a signal net."""
        if net not in plane_fill_index:
            return None
        if _pcb._net_kind(net, None, power_patterns) != "power":
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
                area = e["area_mm2"] if "area_mm2" in e else _polygon_area_mm2(e["pts"])
                recs.append((e, len(attachments), area))
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

    # -- 7.8 multi-core: bundle every immutable routing input into a picklable
    #    context so ONE stateless search function serves both the serial worklist
    #    and the spawned workers (see module-level `_route_one`). --------------- #
    plane_by_net = {c["net"]: _plane_components_for(c["net"]) for c in conns}
    # Phase 7.18.3: reference-plane slices, built ONLY when the bonus is tuned
    # on (default 0.0 -> `{}`, nothing computed, nothing pickled to workers,
    # and `_build_fine_cost`'s `via` takes its untouched pre-7.18 branch).
    return_path_by_net: dict[str, dict[str, Any]] = {}
    if return_path_bonus > 0.0:
        rp_near_mm = float((settings.get("stitching", {}) or {}).get("near_high_speed_mm", 1.0))
        gnd_tokens = ((settings.get("schematic_checks", {}) or {})
                      .get("cap_voltage", {}) or {}).get("gnd_tokens", []) or []
        signal_nets = sorted({c["net"] for c in conns if plane_by_net.get(c["net"]) is None})
        return_path_by_net = _reference_plane_rasters(
            signal_nets, plane_fill_index, _plane_pads_by_net, power_patterns,
            gnd_tokens, all_cu, routable_set, rp_near_mm)
        for rec in return_path_by_net.values():
            rec["bonus"] = return_path_bonus
    ctx: dict[str, Any] = {
        "power_patterns": power_patterns, "routable_layers": routable_layers,
        "routable_set": routable_set, "layer_types": layer_types, "grid": grid,
        "max_grid_mm": max_grid_mm, "min_grid_mm": min_grid_mm,
        "max_window_nodes": max_window_nodes,
        "base_margin": base_margin, "board_bbox": board_bbox, "board_min": board_min,
        "coarse_grid": coarse_grid, "coarse_min": coarse_min, "backend": backend,
        # 7.8 GPU tier knobs (`memory_budget_mb` / `batch` / `oom_fallback`),
        # carried as a small picklable slice of `settings` rather than the whole
        # thing, so a spawned worker can plan its own VRAM budget identically.
        "gpu_settings": {"autorouter": {"gpu": dict(autor.get("gpu", {}) or {})}},
        "plane_step": plane_step, "attachment_via_cost": attachment_via_cost,
        "weights": weights, "layer_purpose": layer_purpose, "directions": directions,
        "track_half": track_half, "via_radius": via_radius, "rules": rules,
        "global_by_key": global_by_key, "tw": tw, "plane_by_net": plane_by_net,
        "base_obstacles": obstacles,
        "multilayer_attachment": ml_attach,
        # Phase 7.19.1: `autorouter.goal_field_heuristic`. Default False keeps
        # the legacy octile-heuristic, break-on-first-goal A* byte-for-byte.
        "goal_field_heuristic": bool(autor.get("goal_field_heuristic", False)),
        # Phase 7.19.2: `autorouter.candidate_fallback`. Default (absent, or
        # `enabled: false`) means detailed routing uses coarse candidate 0 only,
        # exactly as before this phase existed. Small picklable primitives, so
        # a spawned worker gates identically to the parent.
        "candidate_fallback": dict(autor.get("candidate_fallback", {}) or {}),
        "return_path_by_net": return_path_by_net,
        # Phase 7.12 neck-down: config + per-(ref, pad) copper size + the
        # board's min_track_width DRC floor. Small, picklable primitives -
        # shipped to workers with the rest of `ctx` unchanged.
        "neck_cfg": neck_cfg, "pad_size_by_ref_num": pad_size_by_ref_num,
        "neck_min_width": neck_min_width,
        # Phase 7.3d direction-aware pad escape: default False - a picklable
        # bool, read once per connection in `_route_one`/`_route_hierarchical`
        # rather than re-reading `settings` per call. See `nearest_free`'s
        # `toward_xy` doc and NETCLASS_PLAN.md's 7.3d section for why this is
        # gated behind a flag instead of a plain new-default addition.
        "pad_escape_direction_aware": bool(autor.get("pad_escape_direction_aware", False)),
        # Picklable "recipe" fields used ONLY to let a worker process rebuild
        # `base_obstacles`/`plane_by_net` locally instead of receiving them
        # through pickle (see `_worker_init`) - never read by `_route_one`.
        "_board_path": str(board_path), "all_cu": all_cu,
        "plane_island_base": island_base, "plane_orphan_island_cost": orphan_island_cost,
        "plane_grid_mm": plane_grid_mm, "plane_clearance_mm": plane_clearance_mm,
    }

    def _route_core(conn: dict[str, Any], owner: int, use_corridor: bool = True) -> dict[str, Any]:
        """Serial-path wrapper: route one connection against the CURRENT
        placements + shared congestion, via the stateless module-level
        `_route_one`. Byte-identical to the pre-7.8 in-lined search."""
        return _route_one(ctx, conn, active_obstacles_for(owner), congestion, use_corridor)

    # -- negotiated-congestion worklist -------------------------------------- #
    from collections import deque

    owner_conns = list(conns)                       # index == owner id (canonical)
    n_conns = len(owner_conns)
    if progress_enabled:
        _progress_session["total_connections"] = n_conns
        _progress_append(progress_path, _progress_header_event(
            project_path, board_path, settings, _progress_session))
        if bool(progress_cfg.get("open_viewer", False)):
            try:
                # auto_close=True: this launch is config-driven, not a user
                # explicitly asking to watch (that's the separate
                # `open_kicad_route_viewer` MCP tool, which never auto-closes)
                # - see `open_route_viewer`'s docstring.
                open_route_viewer(project_path, auto_close=True)
            except Exception:
                pass
    placements: dict[int, dict[str, Any]] = {}      # owner -> {segments, vias, rec, net, obstacles}
    failures: dict[int, dict[str, Any]] = {}        # owner -> failed record
    congestion: dict[tuple[int, int, str], int] = {}
    congestion_bump = max(1, weights.q(weights.congestion))
    displaced_by: dict[int, int] = {}               # ripped owner -> displacing owner (anti-thrash)
    rerouted: set[int] = set()                       # owners that have been ripped (re-route corridor-free)
    ripup_iterations = 0
    connections_ripped = 0
    congestion_escalations = 0
    # allow_hand_copper_ripup bookkeeping (default-off feature; see docstring).
    # `hand_copper_pool` is the live set of still-rippable hand-copper _Obst
    # objects, keyed by board uuid - it starts as every eligible obstacle and
    # SHRINKS as pieces get ripped, which is what guarantees a given piece can
    # never be ripped twice in one run (no uuid stays in the pool after its
    # first rip). `human_copper_ripped` accumulates the audit-trail record for
    # every piece actually ripped this run, in rip order.
    hand_copper_pool: dict[str, _Obst] = {}
    if allow_hand_copper_ripup:
        hand_copper_pool = {ob.uuid: ob for ob in obstacles if _is_hand_copper_obstacle(ob)}
    human_copper_ripped: list[dict[str, Any]] = []

    def _hand_copper_record(ob: "_Obst", ripped_for_net: str) -> dict[str, Any]:
        return {
            "uuid": ob.uuid,
            "net": ob.net,
            "kind": "via" if ob.kind == "pt" else "segment",
            "layers": sorted(ob.layers),
            "x1": round(ob.x1, 4), "y1": round(ob.y1, 4),
            "x2": round(ob.x2, 4), "y2": round(ob.y2, 4),
            "ripped_for_net": ripped_for_net,
        }

    def _commit_hand_copper_rip(uuids: set[str], ripped_for_net: str) -> list[dict[str, Any]]:
        """Remove each ripped hand-copper piece from BOTH the shared board-wide
        `obstacles` list (so later connections in this run see it as gone,
        exactly like a written change would be) and `hand_copper_pool` (so it
        cannot be selected again - the anti-double-rip guard). Returns the
        audit records, in canonical (sorted uuid) order for determinism."""
        recs: list[dict[str, Any]] = []
        for uid in sorted(uuids):
            ob = hand_copper_pool.pop(uid, None)
            if ob is None:
                continue
            try:
                obstacles.remove(ob)
            except ValueError:
                pass
            recs.append(_hand_copper_record(ob, ripped_for_net))
        return recs

    def active_obstacles_for(owner: int) -> list[_Obst]:
        act = list(obstacles)
        for oid, pl in placements.items():
            if oid == owner:
                continue
            act.extend(pl["obstacles"])
        return act

    def _place(owner: int, net: str, segments: list[dict[str, Any]],
               vias: list[dict[str, Any]], rec: dict[str, Any],
               ripped_local_geometry: list[dict[str, Any]] | None = None) -> None:
        placements[owner] = {
            "segments": segments, "vias": vias, "rec": rec, "net": net,
            "obstacles": _obstacles_from_emit(net, segments, vias, track_half,
                                              via_radius, routable_layers, owner),
        }
        failures.pop(owner, None)
        if progress_enabled:
            _emit_connection_progress(owner, net, True, segments, vias, ripped_local_geometry)

    # Phase 7.9: per-connection progress event, called from every commit site
    # above (`_place`) and every terminal-failure site below. `owner` doubles
    # as a stable LOCAL id for this run's segments/vias (`f"{owner}:seg:{i}"` /
    # `f"{owner}:via:{i}"`) - HONEST SIMPLIFICATION: the real board uuid for
    # written copper is only assigned once, in a single batch, at the very end
    # of this function (see `emit_segments`/`emit_vias` below), so a
    # per-connection event necessarily uses a progress-stream-local id rather
    # than the eventual board uuid; the viewer only needs a stable id to
    # incrementally add/remove canvas items by, not the final board identity.
    def _local_geometry(owner: int, segments: list[dict[str, Any]],
                        vias: list[dict[str, Any]]) -> list[dict[str, Any]]:
        items = []
        for i, s in enumerate(segments):
            items.append({"kind": "segment", "uuid": f"{owner}:seg:{i}", "net": s.get("net"),
                         "layer": s.get("layer"), "width": s.get("width"),
                         "start": s.get("start"), "end": s.get("end")})
        for i, v in enumerate(vias):
            items.append({"kind": "via", "uuid": f"{owner}:via:{i}", "net": v.get("net"),
                         "size": v.get("size"), "drill": v.get("drill"), "at": v.get("at")})
        return items

    def _emit_connection_progress(owner: int, net: str, routed: bool,
                                   segments: list[dict[str, Any]] | None,
                                   vias: list[dict[str, Any]] | None,
                                   ripped_local_geometry: list[dict[str, Any]] | None) -> None:
        nonlocal _progress_conns_done
        _progress_conns_done += 1
        added = _local_geometry(owner, segments or [], vias or [])
        _progress_append(progress_path, {
            "event": "connection",
            "ts": time.time(),
            "connection_index": _progress_conns_done,
            "total_connections": n_conns,
            "owner": owner,
            "net": net,
            "routed": routed,
            "iteration": ripup_iterations,
            "score": round(sum(pl["rec"].get("length_mm", 0.0) for pl in placements.values()), 4),
            "changed": {"added": added, "removed": ripped_local_geometry or []},
            "decision_protocol": None,
        })

    # -- 7.8b speculative parallel pass: route EVERY connection concurrently -- #
    # against the BASE board (no placements, no congestion), across processes,
    # then COMMIT in canonical order in the parent. Unlike the old Phase A
    # (restricted to spatially-independent connections, which are the rare
    # case on a dense board), this covers the whole worklist - the point being
    # that most connections' A* search (the 10-19 s cost for a failing one) can
    # run in parallel across cores, with only genuine cross-connection
    # conflicts falling back to the serial rip-up worklist below.
    #
    # Determinism argument (worker count AND submission order independent):
    #   - Each `_worker_route_speculative` result is a pure function of
    #     (ctx, conn, base_obstacles, {}) - see that function's docstring - so
    #     a given owner's speculative result is identical no matter which
    #     worker computes it or in what order the pool processes the batch.
    #   - The PARENT commits strictly in ascending canonical owner order,
    #     self-checking each routed result against `active_obstacles_for(owner)`
    #     (base + everything already committed THIS pass) before placing it.
    #     That self-check and the commit loop are ordinary serial Python -
    #     same order, same result, regardless of worker count.
    #   - A speculative "not routed" is TERMINAL, never requeued: obstacles are
    #     strictly additive (`active_obstacles_for` only ever adds placement
    #     copper on top of the same base set, and no placement is
    #     `via_transparent`), so a search that already failed against the
    #     MINIMAL obstacle set (base only) is provably unreachable against any
    #     superset too, for the same attempt ladder/corridor bias - and rip-up
    #     cannot help either, since rip-up only removes AUTOROUTER-PLACED
    #     copper, which the speculative pass never saw in the first place (its
    #     view is already "as if every other net were ripped"). So this exactly
    #     matches what the plain (non-rip-up) first serial attempt at this
    #     owner would find - recording it now is not a regression.
    #   - A speculative "routed" that FAILS the commit-time self-check means
    #     its copper collides with another connection ALSO computed against
    #     the base-only view (a genuine cross-connection conflict that the
    #     speculative pass, by construction, could not see) - THIS is requeued
    #     into the serial worklist, which re-routes it against the true
    #     current placements and can rip-up if needed, identically to how a
    #     serial-only run would have handled the second-arriving conflict.
    #
    # The cheap feasibility screen (`_feasibility_screen`) only reorders which
    # owner is SUBMITTED to the pool first (easy-looking ones first) - commit
    # order below is always ascending canonical order, so this ordering has no
    # effect on the result, only on how quickly a worker becomes free.
    workers = _resolve_workers(settings)
    speculative: dict[int, dict[str, Any]] = {}
    if n_conns > 0:
        # IMPORTANT for determinism: this speculative pass runs for ANY worker
        # count, including 1 - `_run_independent_routes` internally falls back
        # to an in-process serial loop when `workers <= 1`, calling the exact
        # same `_route_one(ctx, conn, base_obstacles, {})` per owner that a
        # pool worker would. That is what makes `workers` purely an EXECUTION
        # detail: the ALGORITHM (speculative-against-base, canonical-order
        # commit with self-check, conflict -> serial requeue) is identical for
        # every worker count, so its output is too. Gating this block behind
        # `workers > 1` (an earlier draft did) would make `workers=1` fall
        # through to the OLD pure-serial algorithm below with NO speculative
        # pass at all - a genuinely different computation (every connection
        # searched against the true current placements instead of base-only),
        # which is NOT guaranteed to reproduce the same geometry. Verified this
        # the hard way: that gating produced non-identical `connections` JSON
        # between workers=1 and workers=8 on the real kiln board.
        submit_order = sorted(range(n_conns),
                              key=lambda i: (_feasibility_screen(ctx, owner_conns[i], obstacles), i))
        items = [(owner, owner_conns[owner]) for owner in submit_order]
        speculative = _run_independent_routes(ctx, items, workers)

    for owner in range(n_conns):
        res = speculative.get(owner)
        if res is None:
            continue
        if not res["routed"]:
            # allow_hand_copper_ripup exception to the "speculative failure is
            # terminal" rule (see the big comment above this loop): that rule's
            # argument is "rip-up only removes AUTOROUTER-PLACED copper, which
            # the speculative pass never saw in the first place" - true when
            # hand copper is never rippable, but WRONG once it can be: the
            # speculative pass runs against the BASE obstacle set, which DOES
            # include hand copper, and hand-copper rip-up (Step 4) can free
            # exactly that. So when the flag is on and there is still
            # something in the hand-copper pool to try, don't finalize this as
            # a terminal failure - fall through to `pending` instead, where
            # the serial worklist re-searches for real and Step 4 gets its
            # normal chance. (When the flag is off, or the pool is already
            # empty, this is a no-op and behavior is unchanged.)
            if not (allow_hand_copper_ripup and hand_copper_pool):
                failures[owner] = res["rec"]
                if progress_enabled:
                    _emit_connection_progress(owner, res["net"], False, None, None, None)
            continue
        violations = _self_check(res["net"], res["segments"], res["vias"],
                                 active_obstacles_for(owner), rules, via_radius)
        if not violations:
            _place(owner, res["net"], res["segments"], res["vias"], res["rec"])
        # else: leaves owner neither placed nor failed -> falls into `pending`
        # below, which reroutes it against the true current placements.

    done_speculatively = set(placements.keys()) | set(failures.keys())
    pending: "deque[int]" = deque(i for i in range(n_conns) if i not in done_speculatively)
    while pending:
        # Phase 7.9 cancel support: the viewer's "Stop after this iteration"
        # button writes `route_cancel_requested` into board-local state;
        # checked between connections (never mid-search) so a cancelled run
        # always stops on a clean, self-consistent boundary and returns
        # whatever is routed so far, marked cancelled below.
        if _route_cancel_requested(project_path):
            cancelled = True
            if progress_enabled:
                _progress_append(progress_path, {
                    "event": "cancelled", "ts": time.time(),
                    "connections_done": _progress_conns_done, "total_connections": n_conns,
                })
            break
        owner = pending.popleft()
        core = _route_core(owner_conns[owner], owner, use_corridor=owner not in rerouted)
        if core["routed"]:
            _place(owner, core["net"], core["segments"], core["vias"], core["rec"])
            continue

        # Step 4: attempt rip-up for an A*-unreachable failure OR a self-check
        # failure whose violations are against RIPPABLE autorouter-placed
        # copper (window-budget failures, and self-check failures against
        # non-rippable copper, stay hard). Never rip when nothing is placed.
        failure = core["rec"].get("failure") or {}
        reason = failure.get("reason")
        did_rip = False
        if (reason == "unreachable_in_window" and core["win"] is not None
                and (placements or (allow_hand_copper_ripup and hand_copper_pool))
                and ripup_iterations < max_ripup_iterations):
            win = core["win"]
            # Anti-thrash: a net does not rip the net that just displaced it.
            protect = {displaced_by[owner]} if owner in displaced_by else set()
            rippable = [ob for oid, pl in placements.items() if oid not in protect
                        for ob in pl["obstacles"]]
            # allow_hand_copper_ripup (default off): the whole still-rippable
            # hand-copper pool is also offered to this window - geometry-gated
            # exactly like every other obstacle (`obstacle_cells`/`remove_
            # obstacle` bbox-reject anything far outside the window), so this
            # is cheap even though the pool is board-wide.
            hand_candidates: list[_Obst] = list(hand_copper_pool.values()) if allow_hand_copper_ripup else []
            # Incrementally clear the rippable autorouter copper (+ hand copper,
            # when enabled) from THIS window (no full rebuild) and re-search.
            for ob in rippable:
                win.remove_obstacle(ob)
            for ob in hand_candidates:
                win.remove_obstacle(ob)
            win_cong = _project_congestion(win, congestion, board_min[0], board_min[1], grid)
            free_path = _fine_search(backend, win, core["net_kind"], weights, layer_purpose, directions,
                                     core["s_cell"], core["start_layers"], core["g_cell"],
                                     core["goal_layers"], core["home_layer"], core["corridor"], win_cong,
                                     core["plane_layers"], core["goal_planes"],
                                     plane_step, attachment_via_cost,
                                     ml_attach, (return_path_by_net or {}).get(core["net"]),
                                     bool(ctx.get("goal_field_heuristic", False)),
                                     _settings=ctx.get("gpu_settings"))
            if free_path is not None:
                via_nodes = _path_via_nodes(free_path)
                blockers: set[int] = set()
                for oid, pl in placements.items():
                    if oid in protect:
                        continue
                    if any(_obstacle_on_path(win, ob, free_path, via_nodes) for ob in pl["obstacles"]):
                        blockers.add(oid)
                hand_blocker_objs: set[_Obst] = set()
                if hand_candidates:
                    for ob in hand_candidates:
                        if _obstacle_on_path(win, ob, free_path, via_nodes):
                            hand_blocker_objs.add(ob)
                if blockers or hand_blocker_objs:
                    # Place THIS connection on the freed path; self-check against
                    # human copper + the placements we are KEEPING (non-blockers),
                    # minus any hand-copper obstacle(s) being ripped.
                    keep_obs = [ob for ob in obstacles if ob not in hand_blocker_objs]
                    for oid, pl in placements.items():
                        if oid not in blockers:
                            keep_obs.extend(pl["obstacles"])
                    rec_updates, segments, vias, violations = _finalize_core(
                        ctx, core["net"], win, free_path, core["from_xy"], core["to_xy"],
                        keep_obs, core.get("margin", base_margin), core["plane_layers"],
                        owner_conns[owner])
                    if rec_updates is not None:
                        ripup_iterations += 1
                        congestion_escalations += _raise_path_congestion(
                            congestion, win, free_path, board_min[0], board_min[1],
                            grid, congestion_bump)
                        ripped_geometry: list[dict[str, Any]] = []
                        for b in sorted(blockers):
                            if progress_enabled and b in placements:
                                ripped_geometry.extend(
                                    _local_geometry(b, placements[b]["segments"], placements[b]["vias"]))
                            placements.pop(b, None)
                            displaced_by[b] = owner
                            rerouted.add(b)
                            connections_ripped += 1
                        rec = dict(core["rec"])
                        rec.update(rec_updates)
                        rec["ripped_to_place"] = sorted(blockers)
                        if hand_blocker_objs:
                            hand_recs = _commit_hand_copper_rip(
                                {ob.uuid for ob in hand_blocker_objs}, core["net"])
                            human_copper_ripped.extend(hand_recs)
                            rec["human_copper_ripped"] = hand_recs
                            ripped_geometry.extend(
                                {"kind": r["kind"], "uuid": r["uuid"], "net": r["net"]} for r in hand_recs)
                        _place(owner, core["net"], segments, vias, rec, ripped_geometry)
                        # re-queue the ripped connections (canonical order).
                        pending = deque(sorted(set(pending) | blockers))
                        did_rip = True

        elif (reason == "self_check_failed" and core.get("path") is not None
                and core["win"] is not None
                and (placements or (allow_hand_copper_ripup and core.get("violations")))
                and ripup_iterations < max_ripup_iterations):
            # A path was FOUND (it cleared the A* obstacle model) but the exact
            # `_finalize_core` self-check rejected it - a plane-skim. Unlike the
            # unreachable case, we already have the failing path AND (via
            # `core["violations"]`, each carrying `owner` - see `_self_check`)
            # exactly which obstacle each violation is against, so there is no
            # need to re-search: rip the OWNING placed connection(s) of every
            # RIPPABLE violation (owner is not None) and re-finalize the SAME
            # path against the reduced obstacle set. A violation with
            # `owner is None` (filled zone/plane, pad, edge, hand copper) can
            # never be freed this way UNLESS `allow_hand_copper_ripup` is set
            # AND the violation is specifically against hand-routed track/via
            # copper (`v["hand_copper"]` - see `_is_hand_copper_obstacle`; a
            # violation against a pad, zone, or edge is never eligible even
            # then) - if every violation is against non-rippable copper,
            # `blockers`/`hand_blockers` are both empty and this connection
            # correctly stays a hard failure below.
            win = core["win"]
            protect = {displaced_by[owner]} if owner in displaced_by else set()
            all_violations = core.get("violations") or []
            blockers = {v["owner"] for v in all_violations
                       if v.get("owner") is not None} - protect
            hand_blockers: set[str] = set()
            if allow_hand_copper_ripup:
                hand_blockers = {v["obstacle_uuid"] for v in all_violations
                                 if v.get("hand_copper") and v.get("obstacle_uuid")
                                 and v["obstacle_uuid"] in hand_copper_pool}
            if blockers or hand_blockers:
                hand_blocker_objs = {hand_copper_pool[uid] for uid in hand_blockers}
                keep_obs = [ob for ob in obstacles if ob not in hand_blocker_objs]
                for oid, pl in placements.items():
                    if oid not in blockers:
                        keep_obs.extend(pl["obstacles"])
                rec_updates, segments, vias, violations = _finalize_core(
                    ctx, core["net"], win, core["path"], core["from_xy"], core["to_xy"],
                    keep_obs, core.get("margin", base_margin), core["plane_layers"],
                    owner_conns[owner])
                if rec_updates is not None:
                    ripup_iterations += 1
                    congestion_escalations += _raise_path_congestion(
                        congestion, win, core["path"], board_min[0], board_min[1],
                        grid, congestion_bump)
                    ripped_geometry: list[dict[str, Any]] = []
                    for b in sorted(blockers):
                        if progress_enabled and b in placements:
                            ripped_geometry.extend(
                                _local_geometry(b, placements[b]["segments"], placements[b]["vias"]))
                        placements.pop(b, None)
                        displaced_by[b] = owner
                        rerouted.add(b)
                        connections_ripped += 1
                    rec = dict(core["rec"])
                    rec.update(rec_updates)
                    rec["ripped_to_place"] = sorted(blockers)
                    if hand_blockers:
                        hand_recs = _commit_hand_copper_rip(hand_blockers, core["net"])
                        human_copper_ripped.extend(hand_recs)
                        rec["human_copper_ripped"] = hand_recs
                        ripped_geometry.extend(
                            {"kind": r["kind"], "uuid": r["uuid"], "net": r["net"]} for r in hand_recs)
                    _place(owner, core["net"], segments, vias, rec, ripped_geometry)
                    # re-queue the ripped connections (canonical order).
                    pending = deque(sorted(set(pending) | blockers))
                    did_rip = True

        if not did_rip:
            failures[owner] = core["rec"]
            if progress_enabled:
                _emit_connection_progress(owner, core["net"], False, None, None, None)

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
        elif owner in failures:
            out_conns.append(failures[owner])
        else:
            # Phase 7.9: `cancelled` left this owner in neither map - the
            # per-connection loop stopped before reaching it. Reported
            # honestly as "not attempted" rather than a routing failure.
            out_conns.append({
                "net": owner_conns[owner].get("net"), "routed": False,
                "length_mm": 0.0, "cancelled": True,
                "failure": {"reason": "cancelled_before_attempt"},
            })

    # ---- emit (write) -------------------------------------------------------
    written = False
    owned_added = {"segments": [], "vias": []}
    hand_copper_removed = 0
    if write and (emit_segments or emit_vias or human_copper_ripped):
        _pcb._check_not_locked_by_editor(board_path, allow_while_open)
        blocks: list[str] = []
        records: list[dict[str, Any]] = []
        for (net, s, uid) in emit_segments:
            # Phase 7.12: a segment carrying its own "width" (a neck) wins
            # over the net's uniform class width; absence of the key (every
            # segment from every other feature) is byte-identical to before.
            blocks.append(_segment_block(s, net, s.get("width", rules["track_width"]), uid))
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
        # allow_hand_copper_ripup write path: physically remove every ripped
        # hand-copper (segment)/(via) block from the board TEXT, same uuid-
        # block-delete surgery `unroute_nets` uses for autorouter-owned copper
        # (`_delete_blocks_by_uuid`) - so a write stays DRC-consistent with
        # what was reported. No-op (0 uuids) when the flag was off or nothing
        # was ripped.
        if human_copper_ripped:
            hand_uuids = {r["uuid"] for r in human_copper_ripped}
            text, hand_copper_removed = _delete_blocks_by_uuid(text, hand_uuids)
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

    # Plane-crossing vias/traces need KiCad to cut the real anti-pad/clearance
    # void in the pours (the `via_transparent` model assumes it). Opt-in so byte-
    # exact tests and the no-via case stay untouched; auto-skips without kicad-cli.
    refill = None
    if written and refill_zones:
        refill = refill_zones_with_kicad(board_path)

    if progress_enabled:
        _progress_append(progress_path, {
            "event": "run_complete", "ts": time.time(), "cancelled": cancelled,
            "connections_routed": routed_count, "total_connections": n_conns,
            "written": written,
        })

    result = {
        "board_path": str(board_path),
        "grid_mm": grid,
        "write": write,
        "written": written,
        "cancelled": cancelled,
        "refill": refill,
        "rules": rules,
        "max_ripup_iterations": max_ripup_iterations,
        "ripup_active": True,
        "allow_hand_copper_ripup": allow_hand_copper_ripup,
        "ripup": {
            "iterations": ripup_iterations,
            "connections_ripped": connections_ripped,
            "congestion_escalations": congestion_escalations,
            "max_ripup_iterations": max_ripup_iterations,
        },
        # The human-copper audit trail (NETCLASS_PLAN item 10): empty unless
        # `allow_hand_copper_ripup=True` AND a hand-routed track/via was
        # actually ripped to complete a route. `write=False` (default) reports
        # what WOULD be removed without touching the board; `write=True`
        # additionally deletes exactly these uuids from the board text (see
        # `hand_copper_removed` below, which counts the blocks that were
        # actually found and deleted - it can be < len(human_copper_ripped)
        # only if the board text changed out from under this call, same
        # honesty convention as `unroute_nets`'s `removed` vs `candidates`).
        "human_copper_ripped": human_copper_ripped,
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
            "human_copper_ripped_count": len(human_copper_ripped),
            "human_copper_removed_from_board": hand_copper_removed,
        },
    }
    if progress_enabled:
        result["progress_path"] = str(progress_path)
    return result


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
    allow_hand_copper_ripup: bool = False,
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

    `allow_hand_copper_ripup` (default False) is passed straight through to
    `route_nets` - see its docstring. Left off, this call is byte-identical to
    before the flag existed: human-placed copper is never a rip-up candidate.
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
        allow_hand_copper_ripup=allow_hand_copper_ripup,
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
        "allow_hand_copper_ripup": allow_hand_copper_ripup,
        "human_copper_ripped": detailed.get("human_copper_ripped", []),
        "connections": detailed.get("connections", []),
        "detailed_result": detailed,   # full route_nets result for callers who want it
        "pipeline": {
            "ratsnest": "done",
            "global_route": "done",
            "detailed_route": "done",
            "rip_up": "disabled (effort=quick)" if max_ripup == 0 else "active",
            "plane_aware_routing": "partial (Phase 7.5.4 landed for power nets; heuristic not cost-optimal)",
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


# =========================================================================== #
# Phase 7.16 - Benchmark harness: score the autorouter against a human-routed
# board, honestly.
#
# `benchmark_autoroute(source_board, mode)` NEVER writes `source_board` - every
# measurement and every route happens on a fresh scratch copy
# (`_copy_project_to_scratch`). It is a thin orchestrator over the existing
# tools (`get_ratsnest`, `route_board`, `get_trace_cost`) plus the kicad-cli
# DRC gate already proven out in `tests/test_detailed_route.py` - no routing
# or scoring logic is duplicated here.
#
# Two modes:
#   complete_only    - measure the HUMAN board's score/unrouted count on the
#                       untouched scratch copy, then route_board(write=True)
#                       only what's missing, then report completion/added
#                       copper/vias/post-score/DRC delta.
#   strip_and_reroute - delete ALL non-zone copper (every top-level
#                       segment/via/arc; zones/footprints/edge-cuts are
#                       untouched) from the scratch copy, route_board from
#                       zero, and compare to the HUMAN ORIGINAL (measured
#                       before stripping, same project => identical weights)
#                       on completion/length/vias/score/per-layer/DRC/runtime.
# =========================================================================== #

_BENCHMARK_MODES = ("complete_only", "strip_and_reroute")


def _find_kicad_cli() -> str | None:
    """Locate kicad-cli (PATH, then standard Windows install locations) -
    same discovery logic as `tests/test_kicad_cli_acceptance.py` /
    `tests/test_detailed_route.py`, duplicated here (not imported from tests)
    so this module has no test-tree dependency."""
    on_path = shutil.which("kicad-cli") or shutil.which("kicad-cli.exe")
    if on_path:
        return on_path
    candidates = list(Path("C:/Program Files/KiCad").glob("*/bin/kicad-cli.exe"))
    candidates += list(Path("C:/Program Files (x86)/KiCad").glob("*/bin/kicad-cli.exe"))
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        candidates += list(Path(local_appdata, "Programs", "KiCad").glob("*/bin/kicad-cli.exe"))
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return None


def refill_zones_with_kicad(board_path: str | Path, timeout: int = 180) -> dict[str, Any]:
    """Recompute ALL zone fills authoritatively with KiCad and save the board -
    `kicad-cli pcb drc --refill-zones --save-board` (the CLI refills as a side
    effect of DRC and writes the board back). This is REQUIRED after a write that
    places plane-crossing vias or traces near a plane: KiCad cuts the real
    anti-pad / clearance void around the new copper so the board is DRC-clean (the
    router's `via_transparent` model assumes this refill happens - see the
    plane-via findings in NETCLASS_PLAN.md). Auto-SKIPS (never raises) when
    kicad-cli is absent, so a box without KiCad still routes (the raw board then
    carries fills that overlap the new vias until a manual refill)."""
    board_path = Path(board_path)
    cli = _find_kicad_cli()
    if cli is None:
        return {"refilled": False, "reason": "kicad-cli not found"}
    try:
        with tempfile.TemporaryDirectory() as td:
            report = Path(td) / "refill_drc.json"
            proc = subprocess.run(
                [cli, "pcb", "drc", "--refill-zones", "--save-board",
                 "--format", "json", "-o", str(report), str(board_path)],
                capture_output=True, text=True, timeout=timeout,
            )
        _pcb._invalidate_board_cache(board_path)
        return {"refilled": proc.returncode == 0, "returncode": proc.returncode}
    except Exception as exc:  # pragma: no cover - defensive (broken cli, timeout)
        return {"refilled": False, "reason": str(exc)}


def _drc_violations(cli: str, board_path: Path, report_path: Path) -> list[dict[str, Any]] | None:
    try:
        subprocess.run(
            [cli, "pcb", "drc", "--format", "json", "--severity-all", str(board_path), "-o", str(report_path)],
            capture_output=True, text=True, timeout=180,
        )
        return json.loads(report_path.read_text(encoding="utf-8")).get("violations", [])
    except Exception:  # pragma: no cover - defensive (missing/broken cli, timeout)
        return None


def _violation_sig(v: dict[str, Any]) -> tuple:
    return (
        v.get("type"),
        v.get("severity"),
        tuple(sorted(item.get("description", "") for item in v.get("items", []))),
    )


def _new_violations(baseline: list[dict[str, Any]], post: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Multiset difference: a post violation is NEW only if it isn't matched by
    a baseline one of the same signature (type/severity/item descriptions) -
    the same logic `test_kicad_cli_drc_no_new_violations` proved out."""
    remaining: dict[tuple, int] = {}
    for v in baseline:
        sig = _violation_sig(v)
        remaining[sig] = remaining.get(sig, 0) + 1
    new: list[dict[str, Any]] = []
    for v in post:
        sig = _violation_sig(v)
        if remaining.get(sig, 0) > 0:
            remaining[sig] -= 1
        else:
            new.append(v)
    return new


def _drc_report(board_path: Path, report_dir: Path, tag: str) -> dict[str, Any]:
    """Run the kicad-cli DRC gate, auto-skipping (not failing) when kicad-cli
    isn't found anywhere on this machine."""
    cli = _find_kicad_cli()
    if cli is None:
        return {"available": False, "violations": None, "violation_count": None}
    violations = _drc_violations(cli, board_path, report_dir / f"drc_{tag}.json")
    if violations is None:
        return {"available": False, "violations": None, "violation_count": None}
    return {"available": True, "violations": violations, "violation_count": len(violations)}


def _copy_project_to_scratch(source_project: str | Path, scratch_dir: str | Path) -> Path:
    """Copy the board/.kicad_pro/.net (+ pcb_settings.json / board_local.json,
    when present) that `source_project` resolves to into `scratch_dir`, under
    their original filenames. `source_project` is NEVER written - every file
    handle this opens on it is read-only (`shutil.copy2`)."""
    board_path, project_file, netlist_path = _pcb._resolve_project_path(source_project)
    scratch_dir = Path(scratch_dir)
    scratch_dir.mkdir(parents=True, exist_ok=True)
    candidates = [
        board_path,
        project_file,
        netlist_path,
        board_path.parent / "pcb_settings.json",
        board_path.with_name(f"{board_path.stem}.board_local.json"),
    ]
    for src in candidates:
        if src.exists():
            shutil.copy2(src, scratch_dir / src.name)
    return scratch_dir


def _layer_lengths_mm(project_path: str | Path) -> dict[str, float]:
    """Copper length per layer (mm) - the per-layer utilization metric."""
    board_path, _, _ = _pcb._resolve_project_path(project_path)
    tracks = _pcb._parse_tracks_cached(board_path)
    lengths: dict[str, float] = {}
    for seg in tracks["segments"] + tracks["arcs"]:
        lengths[seg["layer"]] = lengths.get(seg["layer"], 0.0) + seg["length"]
    return {name: round(val, 3) for name, val in sorted(lengths.items())}


def _strip_non_zone_copper(project_path: str | Path, write: bool = True) -> dict[str, Any]:
    """Delete every top-level `(segment ...)`/`(via ...)`/`(arc ...)` block -
    i.e. ALL non-zone copper. Zone fill polygons live inside their own
    `(zone ...)` block, not as separate top-level segment/via/arc entries, so
    this is a complete "strip everything except the pours" - footprints,
    zones, and edge-cuts are never touched. `write=False` previews the count
    without touching the board (mirrors `unroute_nets`' preview discipline)."""
    board_path, _, _ = _pcb._resolve_project_path(project_path)
    tracks = _pcb._parse_tracks_cached(board_path)
    uuids = {t["uuid"] for t in tracks["segments"] + tracks["vias"] + tracks["arcs"] if t.get("uuid")}
    removed = 0
    if write and uuids:
        text = _pcb._read_text(board_path)
        text, removed = _delete_blocks_by_uuid(text, uuids)
        with board_path.open("w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        _pcb._invalidate_board_cache(board_path)
    return {"board_path": str(board_path), "candidates": len(uuids), "removed": removed, "written": write}


def _score_comparison(human_total: float, post_total: float, label: str) -> dict[str, Any]:
    delta_total = round(post_total - human_total, 3)
    beat = delta_total <= 0
    verdict = (
        f"{label}: auto matched/beat human (post {post_total} <= human {human_total})"
        if beat
        else f"{label}: auto WORSE than human by {delta_total} (post {post_total} > human {human_total})"
    )
    return {
        "human_score_total": human_total,
        "post_score_total": post_total,
        "delta_total": delta_total,
        "matched_or_beat_human": beat,
        "verdict": verdict,
    }


def _drc_comparison(baseline_drc: dict[str, Any], post_drc: dict[str, Any]) -> dict[str, Any]:
    new_violation_count = None
    new_violations = None
    if baseline_drc["available"] and post_drc["available"]:
        new_violations = _new_violations(baseline_drc["violations"], post_drc["violations"])
        new_violation_count = len(new_violations)
    return {
        "baseline": {"available": baseline_drc["available"], "violation_count": baseline_drc["violation_count"]},
        "post": {"available": post_drc["available"], "violation_count": post_drc["violation_count"]},
        "new_violation_count": new_violation_count,
        "new_violations": new_violations[:10] if new_violations else new_violations,
    }


def _benchmark_complete_only(scratch_path: Path, effort: str) -> dict[str, Any]:
    board_path, _, _ = _pcb._resolve_project_path(scratch_path)

    # HUMAN measurement: the untouched scratch copy, before auto touches it.
    human_rats = get_ratsnest(scratch_path)
    human_score = _pcb.get_trace_cost(scratch_path)["board_totals"]
    human_layers = _layer_lengths_mm(scratch_path)
    unrouted_before = human_rats["summary"]["total_connections"]
    baseline_drc = _drc_report(board_path, scratch_path, "baseline")

    t0 = time.perf_counter()
    route_report = route_board(scratch_path, write=True, effort=effort)
    runtime_seconds = round(time.perf_counter() - t0, 3)

    post_score = _pcb.get_trace_cost(scratch_path)["board_totals"]
    post_layers = _layer_lengths_mm(scratch_path)
    post_rats = get_ratsnest(scratch_path)
    post_drc = _drc_report(board_path, scratch_path, "post")

    routed_count = route_report["routed"]
    completion_pct = round(100.0 * routed_count / unrouted_before, 2) if unrouted_before else 100.0

    return {
        "effort": effort,
        "runtime_seconds": runtime_seconds,
        "human": {
            "score": human_score,
            "unrouted_connections_before": unrouted_before,
            "airline_mm_before": human_rats["summary"]["total_airline_mm"],
            "layer_lengths_mm": human_layers,
        },
        "auto": {
            "routed": routed_count,
            "failed": route_report["failed"],
            "completion_pct": completion_pct,
            "total_routed_length_mm": route_report["total_routed_length_mm"],
            "vias_emitted": route_report["vias_emitted"],
            "unrouted_connections_after": post_rats["summary"]["total_connections"],
            "score": post_score,
            "layer_lengths_mm": post_layers,
        },
        "drc": _drc_comparison(baseline_drc, post_drc),
        "comparison": _score_comparison(human_score["total"], post_score["total"], "complete_only"),
        "route_report": route_report,
        "notes": [
            "complete_only measures the human score BEFORE auto touches anything, "
            "then routes only the connections the human left unrouted; the post "
            "score therefore covers the human's own copper PLUS whatever the "
            "autorouter added on top - a 'did the combined board get worse' "
            "comparison, not a from-scratch replay (see strip_and_reroute for that).",
        ],
    }


def _benchmark_strip_and_reroute(scratch_path: Path, effort: str) -> dict[str, Any]:
    board_path, _, _ = _pcb._resolve_project_path(scratch_path)

    # HUMAN ORIGINAL measurement, taken before any stripping.
    human_score = _pcb.get_trace_cost(scratch_path)["board_totals"]
    human_layers = _layer_lengths_mm(scratch_path)
    baseline_drc = _drc_report(board_path, scratch_path, "baseline")

    strip_report = _strip_non_zone_copper(scratch_path, write=True)
    stripped_rats = get_ratsnest(scratch_path)
    total_connections_needed = stripped_rats["summary"]["total_connections"]

    t0 = time.perf_counter()
    route_report = route_board(scratch_path, write=True, effort=effort)
    runtime_seconds = round(time.perf_counter() - t0, 3)

    post_score = _pcb.get_trace_cost(scratch_path)["board_totals"]
    post_layers = _layer_lengths_mm(scratch_path)
    post_rats = get_ratsnest(scratch_path)
    post_drc = _drc_report(board_path, scratch_path, "post")

    routed_count = route_report["routed"]
    completion_pct = (
        round(100.0 * routed_count / total_connections_needed, 2) if total_connections_needed else 100.0
    )

    return {
        "effort": effort,
        "runtime_seconds": runtime_seconds,
        "strip": strip_report,
        "human": {
            "score": human_score,
            "layer_lengths_mm": human_layers,
        },
        "auto": {
            "routed": routed_count,
            "failed": route_report["failed"],
            "total_connections_needed": total_connections_needed,
            "completion_pct": completion_pct,
            "total_routed_length_mm": route_report["total_routed_length_mm"],
            "vias_emitted": route_report["vias_emitted"],
            "unrouted_connections_after": post_rats["summary"]["total_connections"],
            "score": post_score,
            "layer_lengths_mm": post_layers,
        },
        "drc": _drc_comparison(baseline_drc, post_drc),
        "comparison": _score_comparison(human_score["total"], post_score["total"], "strip_and_reroute"),
        "route_report": route_report,
        "notes": [
            "strip_and_reroute deletes ALL non-zone copper (every top-level "
            "segment/via/arc) then routes the whole board from zero - a "
            "from-scratch replay compared against the human original "
            "(measured before stripping, same project/pcb_settings.json so "
            "the Phase 6 weights are identical on both sides).",
        ],
    }


def benchmark_autoroute(
    source_board: str | Path,
    mode: str = "complete_only",
    effort: str = "balanced",
    scratch_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Phase 7.16 - the benchmark harness: score `route_board` against a
    human-routed board (the user-stated north star: "as well or better than
    my hand-routed board", judged by the `get_trace_cost` board score).

    `source_board` is NEVER written - it is only ever read through
    `_copy_project_to_scratch`, which copies the whole project (board +
    `.kicad_pro` + `.net` + `pcb_settings.json`/board-local state, when
    present) into a fresh scratch directory (a new `tempfile.mkdtemp()` unless
    `scratch_dir` is given) before anything measures or routes.

    `mode="complete_only"` (default, the primary acceptance metric): the HUMAN
    board's score/unrouted-connection count is measured on the untouched
    scratch copy, then `route_board(write=True)` routes only what the human
    left unrouted; reports completion %, copper length/vias added, the
    post-route board score, and the kicad-cli DRC delta (baseline vs post,
    new-violation count - auto-skipped, never failed, when kicad-cli isn't on
    this machine).

    `mode="strip_and_reroute"`: deletes ALL non-zone copper from the scratch
    copy (every top-level segment/via/arc - zones/footprints/edge-cuts are
    untouched), reroutes the whole board from zero, and compares the
    rerouted board to the HUMAN ORIGINAL (measured before stripping) on
    completion %, total length, via count, Phase 6 board score (identical
    weights both sides - same project), per-layer copper utilization, DRC
    violation count, and runtime.

    Returns a hand-vs-auto comparison dict; `comparison.matched_or_beat_human`
    (bool) and `comparison.verdict` (str) are the first-class pass/fail
    fields callers should check first.
    """
    mode = (mode or "complete_only").lower()
    if mode not in _BENCHMARK_MODES:
        raise ValueError(f"mode must be one of {_BENCHMARK_MODES}; got {mode!r}")

    source_board_path, _, _ = _pcb._resolve_project_path(source_board)
    owns_scratch = scratch_dir is None
    scratch_path = Path(scratch_dir) if scratch_dir else Path(tempfile.mkdtemp(prefix="kicad_benchmark_"))
    _copy_project_to_scratch(source_board, scratch_path)
    board_path, _, _ = _pcb._resolve_project_path(scratch_path)

    if mode == "complete_only":
        result = _benchmark_complete_only(scratch_path, effort=effort)
    else:
        result = _benchmark_strip_and_reroute(scratch_path, effort=effort)

    result["command"] = "benchmark_autoroute"
    result["mode"] = mode
    result["source_board"] = str(source_board_path)
    result["scratch_board"] = str(board_path)
    result["scratch_owned"] = owns_scratch
    return result


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
    hand_ripped = report.get("human_copper_ripped") or []
    if hand_ripped:
        print(f"  HAND COPPER RIPPED ({len(hand_ripped)}, allow_hand_copper_ripup=True):")
        for r in hand_ripped:
            print(f"    - {r['kind']} uuid={r['uuid']} net={r['net']} "
                  f"layers={r['layers']} for net={r['ripped_for_net']}")
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
    p_route.add_argument("--allow-hand-copper-ripup", action="store_true",
                         help="opt in (default off) to letting rip-up remove hand-routed "
                              "track/via copper (never pads/zones/edges) when it is the "
                              "blocker; reported in the report's human_copper_ripped list")
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
            allow_hand_copper_ripup=args.allow_hand_copper_ripup,
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
