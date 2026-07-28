#!/usr/bin/env python3
"""Phase 7.9 - live route-progress viewer (NETCLASS_PLAN.md).

Runs as its OWN process, spawned (detached) by `kicad_router_tool.open_route_
viewer` / the `open_kicad_route_viewer` MCP tool, or launched manually:

    python kicad_route_viewer.py <board_path>

It tails `<board_stem>.route_progress.jsonl` (written by `route_nets`/
`route_board`, gated by `autorouter.progress.events`) and redraws a `tk.Canvas`
board view plus progress chrome as events arrive. Decoupled by construction:
the router only ever APPENDS to that file - this process never talks back to
the router, so it can be opened, closed, restarted, or crash without touching
(or blocking) an in-flight route. The one exception is the cancel flag: the
"Stop after this iteration" button writes `route_cancel_requested` into
`<board>.board_local.json` (via `kicad_pcb_tool.save_board_local`), which
`route_nets`'s per-connection loop polls between connections.

DESIGN NOTE (testability): every function below that does NOT need a live Tk
display - JSONL parsing/replay, incremental diff/state computation, KiCad
layer-color resolution (`_load_kicad_layer_colors`), the cancel-flag writer -
is a plain, pure-ish function with no `tkinter` import at call time, so the
test suite (headless CI) can exercise all of it without a display. Only
`RouteViewerApp` touches real Tk objects, and `tkinter` itself is imported
defensively (`TK_AVAILABLE`) so importing this module at all never requires a
display - `kicad_router_tool.py` imports `_load_kicad_layer_colors` from here
unconditionally.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

try:
    import tkinter as tk
    from tkinter import ttk
    TK_AVAILABLE = True
except Exception:  # pragma: no cover - exercised via TK_AVAILABLE=False paths
    TK_AVAILABLE = False


# =========================================================================== #
# KiCad layer-color resolution ("auto" theme, JSON theme files, embedded
# fallback). Pure functions - no tkinter, no board access - so the router can
# import this at module load time regardless of Tk availability, and tests can
# exercise every branch headlessly.
# =========================================================================== #

# Embedded fallback palette - KiCad's stock default colors, baked in as
# constants. NEEDED (not just a convenience) because the active theme on a
# fresh/default KiCad install is `_builtin_default`, which has NO theme JSON
# file on disk at all - "read the config" alone cannot resolve it - and this
# also covers machines with no KiCad config whatsoever (never installed, or a
# recording replayed on a different machine).
DEFAULT_LAYER_COLORS: dict[str, str] = {
    "F.Cu": "#c83434",
    "B.Cu": "#3434c8",
    "In1.Cu": "#c8c834",
    "In2.Cu": "#34c8c8",
    "In3.Cu": "#c834c8",
    "In4.Cu": "#4ba82f",
    "In5.Cu": "#a84b2f",
    "In6.Cu": "#2f4ba8",
    "via_through": "#c2c200",
    "via_micro": "#c25e00",
    "via_blind_buried": "#c200a0",
    "background": "#131318",
    "edge_cuts": "#ffff00",
    "ratsnest": "#a0a0a0",
}
for _n in range(7, 31):
    DEFAULT_LAYER_COLORS.setdefault(f"In{_n}.Cu", "#808080")
del _n


def _rgb_string_to_hex(value: str) -> str | None:
    """Parse a KiCad theme color string - `"rgb(200, 52, 52)"` or
    `"rgba(200, 52, 52, 0.8)"` - into a Tk-compatible `#rrggbb` hex string.
    Alpha is DROPPED (Canvas items have no per-item alpha channel). Returns
    None for anything unparseable rather than raising - a malformed/unexpected
    theme value should fall back to the embedded default, not crash the
    viewer.
    """
    if not isinstance(value, str):
        return None
    m = re.match(r"\s*rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*(?:,\s*[\d.]+\s*)?\)\s*$", value)
    if not m:
        # Already a hex string?
        if re.match(r"^#[0-9a-fA-F]{6}$", value.strip()):
            return value.strip().lower()
        return None
    r, g, b = (max(0, min(255, int(float(x)))) for x in m.groups())
    return f"#{r:02x}{g:02x}{b:02x}"


# board.<key> -> our layer-name key
_THEME_COPPER_KEYS = {"f": "F.Cu", "b": "B.Cu"}
for _n in range(1, 31):
    _THEME_COPPER_KEYS[f"in{_n}"] = f"In{_n}.Cu"
del _n

_THEME_MISC_KEYS = {
    "via_through": "via_through",
    "via_micro": "via_micro",
    "via_blind_buried": "via_blind_buried",
    "background": "background",
    "edge_cuts": "edge_cuts",
    "ratsnest": "ratsnest",
}


def _kicad_config_root() -> Path | None:
    """`%APPDATA%/kicad` (Windows) - the root under which each installed
    version keeps its own `<ver>/` config directory. Reads `APPDATA` from the
    environment (not a hardcoded path) so tests can point this at a scratch
    directory. Returns None when the env var is unset or the directory does
    not exist (no KiCad config on this machine)."""
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    root = Path(appdata) / "kicad"
    return root if root.is_dir() else None


def _newest_kicad_version_dir(root: Path) -> Path | None:
    """Pick the newest `<ver>/` directory under the kicad config root, sorted
    by parsed version tuple (falls back to lexical sort for unparseable
    names) - the most recently installed/used KiCad version's settings are
    what a running KiCad session actually uses."""
    candidates = [d for d in root.iterdir() if d.is_dir()]
    if not candidates:
        return None

    def _key(d: Path) -> tuple[Any, ...]:
        parts = re.findall(r"\d+", d.name)
        if parts:
            return (0, tuple(int(p) for p in parts))
        return (-1, (d.name,))

    candidates.sort(key=_key)
    return candidates[-1]


def _resolve_theme_name(version_dir: Path, color_theme: str) -> str | None:
    """`color_theme == "auto"`: read `pcbnew.json` -> `appearance.color_theme`.
    Anything else: treat `color_theme` itself as the literal theme name (an
    explicit override some future caller may want) - either way this returns
    the NAME to look up in `colors/*.json`, not resolved colors yet."""
    if color_theme and color_theme.lower() != "auto":
        return color_theme
    pcbnew_json = version_dir / "pcbnew.json"
    if not pcbnew_json.exists():
        return None
    try:
        data = json.loads(pcbnew_json.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data.get("appearance", {}).get("color_theme")


def _find_theme_file(version_dir: Path, theme_name: str | None) -> Path | None:
    """Match `theme_name` against `colors/*.json` by `meta.name` first, then by
    filename stem - a theme is commonly saved as `<slug>.json` with a
    human-readable `meta.name` inside. `_builtin_default` (the theme with no
    file on disk) correctly returns None here, falling through to the
    embedded fallback palette."""
    if not theme_name:
        return None
    colors_dir = version_dir / "colors"
    if not colors_dir.is_dir():
        return None
    stem_match = colors_dir / f"{theme_name}.json"
    if stem_match.exists():
        return stem_match
    for candidate in colors_dir.glob("*.json"):
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if data.get("meta", {}).get("name") == theme_name:
            return candidate
    return None


def _load_kicad_layer_colors(color_theme: str = "auto") -> dict[str, Any]:
    """Resolve the user's actual KiCad layer colors, driven by
    `autorouter.progress.color_theme`:

      1. "auto": find the newest version dir under `%APPDATA%/kicad/<ver>/`,
         read `pcbnew.json` -> `appearance.color_theme`, match that theme in
         `colors/*.json` by `meta.name`/filename, parse `board.copper.f` /
         `.in1..in30` / `.b`, `board.via_through`, `board.background`,
         `board.edge_cuts`, `board.ratsnest` (`"rgb(...)"` strings) to hex.
      2. Embedded fallback (`DEFAULT_LAYER_COLORS`) for anything not found -
         a fresh/default install's `_builtin_default` theme has no file at
         all, and a machine with no KiCad config falls back entirely.

    Any layer a theme doesn't explicitly name falls back per-layer to the
    embedded palette (never a total failure just because one key is missing).
    The result is meant to be baked into the progress JSONL header event so
    the viewer stays a dumb renderer - a recorded event file replays with the
    colors it was recorded with, without touching this function again.
    """
    colors = dict(DEFAULT_LAYER_COLORS)
    theme_file: Path | None = None
    root = _kicad_config_root()
    if root is not None:
        version_dir = _newest_kicad_version_dir(root)
        if version_dir is not None:
            theme_name = _resolve_theme_name(version_dir, color_theme)
            theme_file = _find_theme_file(version_dir, theme_name)
    if theme_file is not None:
        try:
            theme = json.loads(theme_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            theme = {}
        board = theme.get("board", {}) or {}
        copper = board.get("copper", {}) or {}
        for board_key, layer_name in _THEME_COPPER_KEYS.items():
            hexval = _rgb_string_to_hex(copper.get(board_key))
            if hexval:
                colors[layer_name] = hexval
        for board_key, out_key in _THEME_MISC_KEYS.items():
            hexval = _rgb_string_to_hex(board.get(board_key))
            if hexval:
                colors[out_key] = hexval
    return colors


# =========================================================================== #
# JSONL event tailing/replay - pure data layer, no Tk.
# =========================================================================== #

def iter_events(path: str | Path) -> list[dict[str, Any]]:
    """Parse every well-formed JSON line in `path`. Malformed/blank lines are
    skipped (a viewer reading a file mid-write by the router may see a
    partial final line) rather than raising - tailing must be robust to a
    torn last line."""
    p = Path(path)
    if not p.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


class ProgressState:
    """Incremental, replayable state built by applying progress events in
    order - the pure "diff computation" the viewer's Canvas layer draws from.
    Kept uuid-keyed (`segments`/`vias` dicts) so redraw stays O(change): a
    caller can diff `before`/`after` snapshots of this state to know exactly
    which canvas items to delete/create, instead of rebuilding the whole
    board every event."""

    def __init__(self) -> None:
        self.session: dict[str, Any] = {}
        self.board_path: str | None = None
        self.colors: dict[str, Any] = dict(DEFAULT_LAYER_COLORS)
        self.zones: list[dict[str, Any]] = []
        self.segments: dict[str, dict[str, Any]] = {}
        self.vias: dict[str, dict[str, Any]] = {}
        self.score_history: list[float] = []
        self.connections_done = 0
        self.total_connections = 0
        self.iteration = 0
        self.cancelled = False
        self.done = False
        self.decision_protocol: dict[str, Any] | None = None
        self.last_event: dict[str, Any] | None = None

    def _apply_changed(self, changed: dict[str, Any] | None) -> None:
        if not changed:
            return
        for item in changed.get("added", []) or []:
            uid = item.get("uuid")
            if not uid:
                continue
            bucket = self.vias if item.get("kind") == "via" else self.segments
            bucket[uid] = item
        for item in changed.get("removed", []) or []:
            uid = item.get("uuid") if isinstance(item, dict) else item
            if not uid:
                continue
            self.segments.pop(uid, None)
            self.vias.pop(uid, None)

    def apply(self, event: dict[str, Any]) -> None:
        self.last_event = event
        kind = event.get("event")
        if kind == "header":
            self.session = event.get("session", {}) or {}
            self.board_path = event.get("board_path")
            colors = event.get("colors") or {}
            if colors:
                self.colors = colors
            geometry = event.get("geometry") or {}
            self.zones = geometry.get("zones", []) or []
            for s in geometry.get("segments", []) or []:
                if s.get("uuid"):
                    self.segments[s["uuid"]] = {**s, "kind": "segment"}
            for v in geometry.get("vias", []) or []:
                if v.get("uuid"):
                    self.vias[v["uuid"]] = {**v, "kind": "via"}
            self.total_connections = self.session.get("total_connections", 0)
            self.decision_protocol = event.get("decision_protocol")
        elif kind == "connection":
            self.connections_done = event.get("connection_index", self.connections_done)
            self.total_connections = event.get("total_connections", self.total_connections)
            self.iteration = event.get("iteration", self.iteration)
            if event.get("score") is not None:
                self.score_history.append(event["score"])
            self._apply_changed(event.get("changed"))
        elif kind == "cancelled":
            self.cancelled = True
        elif kind == "run_complete":
            self.done = True
            self.cancelled = self.cancelled or bool(event.get("cancelled"))

    def replay(self, events: list[dict[str, Any]]) -> "ProgressState":
        for event in events:
            self.apply(event)
        return self

    def as_summary(self) -> dict[str, Any]:
        return {
            "board_path": self.board_path,
            "connections_done": self.connections_done,
            "total_connections": self.total_connections,
            "iteration": self.iteration,
            "segment_count": len(self.segments),
            "via_count": len(self.vias),
            "score_history": list(self.score_history),
            "cancelled": self.cancelled,
            "done": self.done,
        }


def replay_state(events: list[dict[str, Any]]) -> ProgressState:
    """Convenience wrapper: build a fresh `ProgressState` from a full event
    list - what a viewer does on (re)open/catch-up, and what tests exercise
    without ever touching Tk."""
    return ProgressState().replay(events)


# =========================================================================== #
# Cancel-flag writer - shared by the viewer's button and the CLI/tests.
# =========================================================================== #

def request_cancel(project_path: str | Path) -> str:
    """Write `route_cancel_requested = True` into `<board>.board_local.json` -
    the flag `route_nets`'s per-connection loop polls between connections.
    Uses `kicad_pcb_tool.load_board_local`/`save_board_local` directly (this
    module has no board-local schema of its own) so the flag lives in the
    SAME per-board state file the rest of the autorouter already trusts."""
    import kicad_pcb_tool as _pcb  # local import: keep this module importable standalone
    state = _pcb.load_board_local(project_path)
    data = state["data"]
    data["route_cancel_requested"] = True
    _pcb.save_board_local(project_path, data)
    return state["board_local_path"]


# =========================================================================== #
# Tk chrome - only touched when TK_AVAILABLE. Thin by design (per the spec's
# testing note): the pure state/diff layer above is what's unit-tested; this
# class is kept simple enough to be obviously correct by inspection.
# =========================================================================== #

_POLL_MS = 250
_AUTO_CLOSE_DELAY_MS = 4000


class RouteViewerApp:
    """The live viewer window: a redrawing `tk.Canvas` board view (segments as
    width-scaled lines, vias/pads as ovals, zone outlines as polygons) plus
    progress chrome (two progress bars, a best-score sparkline, a backend/
    session-state line, and an "awaiting_decision" banner hook for the future
    7.6/7.7 optimizer - not implemented yet, no decision protocol exists).
    Zoom/pan via `canvas.scale`/click-drag. Incremental by uuid: each event
    only deletes/creates the canvas items it changed.
    """

    def __init__(self, board_path: str, auto_close: bool = False) -> None:
        self.board_path = Path(board_path)
        self.project_path = self.board_path
        self.progress_path = self.board_path.with_name(f"{self.board_path.stem}.route_progress.jsonl")
        self.state = ProgressState()
        self._last_size = 0
        self._item_ids: dict[str, int] = {}          # geometry uuid -> canvas item id
        self._layer_visible: dict[str, "tk.BooleanVar"] = {}
        # Only ever True for a viewer THIS run auto-launched via
        # `autorouter.progress.open_viewer` (route_nets/route_board pass
        # `--auto-close`) - not the user explicitly asking to watch via the
        # `open_kicad_route_viewer` MCP tool, which always leaves the window up
        # so the user can review the final board at their own pace.
        self.auto_close = bool(auto_close)
        self._close_scheduled = False

        self.root = tk.Tk()
        self.root.title(f"Route progress - {self.board_path.name}")
        self.root.geometry("1000x700")

        chrome = ttk.Frame(self.root)
        chrome.pack(side=tk.TOP, fill=tk.X)

        self.conn_progress = ttk.Progressbar(chrome, mode="determinate", length=220)
        self.conn_progress.pack(side=tk.LEFT, padx=4, pady=4)
        self.iter_progress = ttk.Progressbar(chrome, mode="determinate", length=220)
        self.iter_progress.pack(side=tk.LEFT, padx=4, pady=4)

        self.status_var = tk.StringVar(value="waiting for progress events...")
        ttk.Label(chrome, textvariable=self.status_var).pack(side=tk.LEFT, padx=8)

        self.banner_var = tk.StringVar(value="")  # 7.6/7.7 "awaiting_decision" hook - unused today
        ttk.Label(chrome, textvariable=self.banner_var, foreground="#a06000").pack(side=tk.LEFT, padx=8)

        cancel_btn = ttk.Button(chrome, text="Stop after this iteration", command=self._on_cancel)
        cancel_btn.pack(side=tk.RIGHT, padx=4, pady=4)

        self.score_canvas = tk.Canvas(self.root, height=40, bg="#1a1a1a", highlightthickness=0)
        self.score_canvas.pack(side=tk.TOP, fill=tk.X)

        self.canvas = tk.Canvas(self.root, bg=self.state.colors.get("background", "#131318"),
                                highlightthickness=0)
        self.canvas.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self.canvas.bind("<ButtonPress-1>", self._pan_start)
        self.canvas.bind("<B1-Motion>", self._pan_move)
        self.canvas.bind("<MouseWheel>", self._zoom)

        self._pan_origin = (0, 0)

    # -- cancel ------------------------------------------------------------ #
    def _on_cancel(self) -> None:
        try:
            request_cancel(self.project_path)
            self.status_var.set("stop requested - will halt after the current connection")
        except Exception as exc:  # pragma: no cover - UI feedback only
            self.status_var.set(f"failed to write cancel flag: {exc}")

    # -- pan/zoom ------------------------------------------------------------ #
    def _pan_start(self, event: "tk.Event") -> None:
        self.canvas.scan_mark(event.x, event.y)

    def _pan_move(self, event: "tk.Event") -> None:
        self.canvas.scan_dragto(event.x, event.y, gain=1)

    def _zoom(self, event: "tk.Event") -> None:
        factor = 1.1 if event.delta > 0 else (1 / 1.1)
        self.canvas.scale("all", event.x, event.y, factor, factor)

    # -- redraw -------------------------------------------------------------- #
    def _mm_to_px(self, x: float, y: float) -> tuple[float, float]:
        return x * 4.0, y * 4.0

    def _draw_segment(self, uid: str, item: dict[str, Any]) -> None:
        start, end = item.get("start"), item.get("end")
        if not start or not end:
            return
        x1, y1 = self._mm_to_px(start["x"], start["y"])
        x2, y2 = self._mm_to_px(end["x"], end["y"])
        color = self.state.colors.get(item.get("layer", ""), "#ffffff")
        width = max(1.0, float(item.get("width") or 0.2) * 4.0)
        old = self._item_ids.pop(uid, None)
        if old is not None:
            self.canvas.delete(old)
        self._item_ids[uid] = self.canvas.create_line(x1, y1, x2, y2, fill=color, width=width)

    def _draw_via(self, uid: str, item: dict[str, Any]) -> None:
        at = item.get("at")
        if not at:
            return
        x, y = self._mm_to_px(at["x"], at["y"])
        r = max(1.0, float(item.get("size") or 0.6) * 2.0)
        color = self.state.colors.get("via_through", "#c2c200")
        old = self._item_ids.pop(uid, None)
        if old is not None:
            self.canvas.delete(old)
        self._item_ids[uid] = self.canvas.create_oval(x - r, y - r, x + r, y + r, fill=color, outline="")

    def _remove_geometry(self, uid: str) -> None:
        old = self._item_ids.pop(uid, None)
        if old is not None:
            self.canvas.delete(old)

    def _apply_event(self, event: dict[str, Any]) -> None:
        before_segments = set(self.state.segments)
        before_vias = set(self.state.vias)
        self.state.apply(event)

        if event.get("event") == "header":
            self.canvas.configure(bg=self.state.colors.get("background", "#131318"))
            for uid, seg in self.state.segments.items():
                self._draw_segment(uid, seg)
            for uid, via in self.state.vias.items():
                self._draw_via(uid, via)
        else:
            removed = before_segments - set(self.state.segments)
            removed |= before_vias - set(self.state.vias)
            for uid in removed:
                self._remove_geometry(uid)
            for uid in set(self.state.segments) - before_segments:
                self._draw_segment(uid, self.state.segments[uid])
            for uid in set(self.state.vias) - before_vias:
                self._draw_via(uid, self.state.vias[uid])

        total = max(1, self.state.total_connections)
        self.conn_progress["maximum"] = total
        self.conn_progress["value"] = self.state.connections_done
        self.iter_progress["maximum"] = max(1, self.state.iteration + 1)
        self.iter_progress["value"] = self.state.iteration
        backend = self.state.session.get("backend", "?")
        self.status_var.set(
            f"{self.state.connections_done}/{self.state.total_connections} connections - "
            f"iter {self.state.iteration} - backend={backend}"
            + (" - CANCELLED" if self.state.cancelled else "")
            + (" - done" if self.state.done else "")
        )
        self._draw_score_sparkline()

        if self.state.done and self.auto_close and not self._close_scheduled:
            # A short grace period so an auto-launched window doesn't just
            # flash and vanish if the user happens to be glancing at it - but
            # this run wasn't started by the user watching, so it closes
            # itself rather than sitting there unattended after completion.
            self._close_scheduled = True
            self.status_var.set(self.status_var.get() + " - closing")
            self.root.after(_AUTO_CLOSE_DELAY_MS, self.root.destroy)

    def _draw_score_sparkline(self) -> None:
        self.score_canvas.delete("all")
        history = self.state.score_history[-200:]
        if len(history) < 2:
            return
        w = int(self.score_canvas.winfo_width() or 400)
        h = 40
        lo, hi = min(history), max(history)
        span = (hi - lo) or 1.0
        points: list[float] = []
        for i, val in enumerate(history):
            x = i / (len(history) - 1) * w
            y = h - ((val - lo) / span) * h
            points.extend([x, y])
        self.score_canvas.create_line(*points, fill="#4bd0ff", width=1.5)

    # -- tailing -------------------------------------------------------------- #
    def _poll(self) -> None:
        try:
            size = self.progress_path.stat().st_size if self.progress_path.exists() else 0
        except OSError:
            size = self._last_size
        if size != self._last_size:
            events = iter_events(self.progress_path)
            # Re-derive from scratch each poll (cheap - JSONL files here are
            # small) rather than tracking a byte offset, so the viewer is
            # correct even if it opened mid-run (replays the whole file).
            new_state = ProgressState().replay(events)
            # Diff old vs new by re-applying only from a fresh state each
            # time keeps this simple and correct at the cost of redrawing
            # geometry that survived unchanged - acceptable at this file size.
            self.state = ProgressState()
            self._item_ids.clear()
            self.canvas.delete("all")
            for event in events:
                self._apply_event(event)
            self._last_size = size
        self.root.after(_POLL_MS, self._poll)

    def run(self) -> None:
        self.root.after(0, self._poll)
        self.root.mainloop()


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    auto_close = "--auto-close" in argv
    positional = [a for a in argv if a != "--auto-close"]
    if not positional:
        print("usage: kicad_route_viewer.py <board_path> [--auto-close]", file=sys.stderr)
        return 2
    board_path = positional[0]
    if not TK_AVAILABLE:
        print(
            "tkinter is not available in this Python environment; cannot open "
            "the route viewer (it is observational-only - routing itself is "
            "unaffected).",
            file=sys.stderr,
        )
        return 1
    app = RouteViewerApp(board_path, auto_close=auto_close)
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
