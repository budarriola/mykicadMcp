"""KiCad IPC API tools - talk to a *running* KiCad instance (via kicad-python /
`kipy`) instead of parsing the board file on disk.

This is a separate, complementary tool set from kicad_pcb_tool.py, not a
replacement for it:

- kicad_pcb_tool.py works headless (no KiCad process needed), does uuid/text-
  anchored surgery on the .kicad_pcb file to keep git diffs minimal, and is
  the right choice for all layout/placement automation (move, align, group,
  template-copy, etc).
- This module only does things that require a live KiCad session: reading
  KiCad's own computed geometry (real bounding boxes, not a heuristic), and
  driving the GUI's selection so a human can see what an agent is about to
  touch before anything is written.

Every function here requires KiCad to be running with the target board open,
and Preferences > Plugins > "Enable IPC API" turned on - without that,
KiCad refuses the connection and every call below raises KiCadNotConnected.
"""

from __future__ import annotations

import math
from typing import Any

from kipy.board import Board
from kipy.errors import ApiError
from kipy.geometry import Box2
from kipy.kicad import KiCad
from kipy.util.units import from_mm, to_mm


class KiCadNotConnected(RuntimeError):
    """KiCad isn't reachable over the IPC API right now."""


def _connect(timeout_ms: int = 2000) -> KiCad:
    try:
        kicad = KiCad(timeout_ms=timeout_ms)
        kicad.ping()
    except Exception as exc:
        raise KiCadNotConnected(
            "Could not reach KiCad over the IPC API. Make sure KiCad is running with this "
            "project's board open, and Preferences > Plugins > 'Enable IPC API' is turned on."
        ) from exc
    return kicad


def _get_board(kicad: KiCad) -> Board:
    try:
        return kicad.get_board()
    except ApiError as exc:
        raise KiCadNotConnected(f"KiCad is reachable but no PCB is currently open: {exc}") from exc


def _reference_of(footprint: Any) -> str:
    try:
        return footprint.reference_field.text.value
    except Exception:
        return ""


def _find_footprint(board: Board, reference: str) -> Any:
    lowered = reference.strip().upper()
    for fp in board.get_footprints():
        if _reference_of(fp).strip().upper() == lowered:
            return fp
    raise KeyError(f"Component {reference} not found on the live board")


def _box_to_dict(box: Box2 | None) -> dict[str, float] | None:
    if box is None:
        return None
    return {
        "x": round(to_mm(box.pos.x), 6),
        "y": round(to_mm(box.pos.y), 6),
        "width": round(to_mm(box.size.x), 6),
        "height": round(to_mm(box.size.y), 6),
    }


def _box_extents(box: Box2) -> tuple[float, float, float, float]:
    x0, x1 = sorted((box.pos.x, box.pos.x + box.size.x))
    y0, y1 = sorted((box.pos.y, box.pos.y + box.size.y))
    return x0, y0, x1, y1


def _boxes_overlap(a: Box2, b: Box2, margin_nm: float) -> bool:
    ax0, ay0, ax1, ay1 = _box_extents(a)
    bx0, by0, bx1, by1 = _box_extents(b)
    return not (ax1 + margin_nm < bx0 or bx1 + margin_nm < ax0 or ay1 + margin_nm < by0 or by1 + margin_nm < ay0)


def get_ipc_status() -> dict[str, Any]:
    """Check whether KiCad's IPC API is reachable right now, and report basic
    version/board info. Every other tool in this module needs this to succeed
    first - call it before assuming a failure elsewhere means something else
    is wrong.
    """
    try:
        kicad = _connect()
    except KiCadNotConnected as exc:
        return {"connected": False, "board_open": False, "error": str(exc)}
    version = kicad.get_version()
    try:
        board = kicad.get_board()
        board_name, board_open = board.name, True
    except ApiError:
        board_name, board_open = None, False
    return {
        "connected": True,
        "kicad_version": repr(version),
        "board_open": board_open,
        "board_name": board_name,
    }


def get_live_bounding_box(reference: str, include_text: bool = False) -> dict[str, Any]:
    """Real KiCad-computed bounding box (mm) for a footprint, straight from
    KiCad's own geometry engine - accounts for actual pad/silkscreen/courtyard
    shapes and rotation exactly, unlike the file-based
    estimate_kicad_footprint_radius heuristic (which approximates every
    footprint as a circle from its footprint name or pad spread). Use this
    when a placement decision needs the real envelope of an oddly-shaped part
    (connector, electrolytic can, relay).
    """
    kicad = _connect()
    board = _get_board(kicad)
    fp = _find_footprint(board, reference)
    box = board.get_item_bounding_box(fp, include_text=include_text)
    return {
        "reference": reference,
        "include_text": include_text,
        "bounding_box_mm": _box_to_dict(box),
    }


def find_live_layout_collisions(
    references: list[str],
    extra_search_radius: float = 25.0,
    margin: float = 0.4,
) -> dict[str, Any]:
    """Live-board analogue of find_kicad_layout_collisions: same internal
    (among `references`) + external (against nearby obstacles within
    `extra_search_radius` mm) collision check, but using KiCad's own computed
    bounding box for each footprint instead of the file tool's circular-radius
    estimate. Catches collisions the radius heuristic misses or over-reports
    on oblong parts. Read-only.
    """
    kicad = _connect()
    board = _get_board(kicad)

    by_ref: dict[str, Any] = {}
    for fp in board.get_footprints():
        ref = _reference_of(fp)
        if ref:
            by_ref[ref.strip().upper()] = fp

    ref_set = {r.strip().upper() for r in references}
    missing = [r for r in references if r.strip().upper() not in by_ref]
    if missing:
        raise KeyError(f"Component(s) not found on live board: {', '.join(missing)}")

    target_fps = [by_ref[r.strip().upper()] for r in references]
    search_radius_nm = from_mm(extra_search_radius)

    def dist_nm(a: Any, b: Any) -> float:
        return math.hypot(a.x - b.x, a.y - b.y)

    obstacle_fps = [
        fp
        for ref_u, fp in by_ref.items()
        if ref_u not in ref_set and any(dist_nm(fp.position, t.position) <= search_radius_nm for t in target_fps)
    ]

    def info(fp: Any) -> dict[str, Any]:
        return {
            "reference": _reference_of(fp),
            "position_mm": {"x": round(to_mm(fp.position.x), 6), "y": round(to_mm(fp.position.y), 6)},
            "box": board.get_item_bounding_box(fp),
        }

    targets = [info(fp) for fp in target_fps]
    obstacles = [info(fp) for fp in obstacle_fps]
    margin_nm = from_mm(margin)

    collisions: list[dict[str, Any]] = []
    for i in range(len(targets)):
        for j in range(i + 1, len(targets)):
            a, b = targets[i], targets[j]
            if a["box"] and b["box"] and _boxes_overlap(a["box"], b["box"], margin_nm):
                collisions.append({"a": a["reference"], "b": b["reference"], "kind": "internal"})
        for o in obstacles:
            a = targets[i]
            if a["box"] and o["box"] and _boxes_overlap(a["box"], o["box"], margin_nm):
                collisions.append({"a": a["reference"], "b": o["reference"], "kind": "external"})

    return {
        "references": references,
        "obstacle_count": len(obstacles),
        "obstacles_checked": [o["reference"] for o in obstacles],
        "collision_count": len(collisions),
        "collisions": collisions,
    }


def highlight_live_components(references: list[str]) -> dict[str, Any]:
    """Select the given component references in the live KiCad PCB editor
    window, replacing whatever's currently selected - so a human reviewing an
    agent's proposed change can see exactly which footprints it's about to
    touch before any write happens (writes still go through the file-based
    tools in kicad_pcb_tool.py; this is purely visual).
    """
    kicad = _connect()
    board = _get_board(kicad)
    board.clear_selection()
    fps = [_find_footprint(board, r) for r in references]
    selection = board.add_to_selection(fps)
    return {"references": references, "selected_count": len(selection)}


def clear_live_highlight() -> dict[str, Any]:
    """Clear the current selection in the live KiCad PCB editor window."""
    kicad = _connect()
    board = _get_board(kicad)
    board.clear_selection()
    return {"cleared": True}


def get_live_selection() -> dict[str, Any]:
    """Read back whatever is currently selected in the live KiCad PCB editor -
    e.g. so a person can point at a component by hand in the GUI instead of
    typing its reference designator for a follow-up tool call.
    """
    kicad = _connect()
    board = _get_board(kicad)
    selection = board.get_selection()
    references = sorted({ref for item in selection if (ref := _reference_of(item))})
    return {"item_count": len(selection), "references": references}
