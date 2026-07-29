"""Phase 7.8 acceleration backends for the fine detailed router.

This module holds the **numpy (CPU-vectorized) tier** of the detailed windowed
search. It is a mechanical vectorization of the pure-Python cpu reference
(`kicad_router_tool._fine_astar`): instead of a serial A* over
`(cx, cy, layer, dir)` states, it relaxes an **integer milli-cost field** over
the whole window with a Lee / Bellman-Ford stencil until the sub-field that the
reconstruction can reach is at fixpoint (which is Dijkstra-optimal — no fixed
sweep cap, no "good enough" early exit).

Why the two backends are bit-identical (the 7.8 parity guarantee):
  * Both cost every move through the SAME model
    (`kicad_router_tool._build_fine_cost`), whose arithmetic is integer
    milli-cost (`_Weights.q` quantizes once). There is no float summation-order
    divergence to make the fields differ.
  * The optimal integer cost of every state is unique, so the cpu dict field and
    this numpy array field agree bit-for-bit on every state.
  * Both reconstruct with the SAME deterministic, field-only backtrace
    (`kicad_router_tool._fine_backtrace`) — a pure function of the field — so the
    emitted geometry (path + vias) is identical, not merely equal in cost.

numpy is a hard runtime dependency of the router; `import numpy` at module load
is intentional (a missing numpy is an install error, not a runtime fallback).
"""

from __future__ import annotations

import math
import os
import shutil
import subprocess
from typing import Any

import numpy as np

# Direction-code layout for the state axis: codes 0..7 are the eight planar
# headings (index into `_MOVES`), code 8 is "no heading" (dir == -1, a start
# state or a state reached only by via hops before any planar move).
_NO_DIR = 8
_N_DIR = 9


def _shift_from(field: "Any", dx: int, dy: int, inf: int, xp: Any = np) -> "Any":
    """`out[iy, ix, ...] = field[iy - dy, ix - dx, ...]`, out-of-window = `inf`.

    i.e. the value carried by the *source* cell of a planar move in direction
    (dx, dy) into (ix, iy). Pure array slicing (no wraparound), so every border
    move correctly reads `inf` from beyond the window edge."""
    out = xp.full_like(field, inf)
    R, C = field.shape[0], field.shape[1]
    ys, ye = max(0, dy), min(R, R + dy)
    xs, xe = max(0, dx), min(C, C + dx)
    if ys >= ye or xs >= xe:
        return out
    out[ys:ye, xs:xe] = field[ys - dy:ye - dy, xs - dx:xe - dx]
    return out


def _leave_one_out_min(planar_dir_min: "Any", exclude: int, inf: int, xp: Any = np) -> "Any":
    """min over the 8 planar direction-codes EXCLUDING `exclude`, given the
    (R, C, L, 8) array. Uses a prefix/suffix min so it is O(1) array ops."""
    # planar_dir_min: (..., 8). Return (...) = min over axis -1 skipping `exclude`.
    pre = xp.full(planar_dir_min.shape[:-1], inf, dtype=planar_dir_min.dtype)
    if exclude > 0:
        pre = planar_dir_min[..., :exclude].min(axis=-1)
    suf = xp.full(planar_dir_min.shape[:-1], inf, dtype=planar_dir_min.dtype)
    if exclude < 7:
        suf = planar_dir_min[..., exclude + 1:].min(axis=-1)
    return xp.minimum(pre, suf)


def _build_cost_arrays(
    win: Any, model: dict[str, Any], weights: Any, layer_purpose: dict[str, Any],
    directions: dict[str, Any], net_kind: str, home_layer: str | None,
    corridor_cells: "set[tuple[int, int]] | None",
    congestion: "dict[tuple[int, int, str], int] | None",
    plane_layers: "dict[str, list[dict[str, Any]]] | None",
    plane_step: float, attachment_via_cost: float,
    goal_cell: "tuple[int, int]", inf: int,
) -> "tuple[np.ndarray, np.ndarray, np.ndarray]":
    """Vectorized integer milli-cost fields, matching `_build_fine_cost`'s scalar
    `planar`/`via` closures bit-for-bit:

      * `Q0[iy, ix, l, di]` — planar move di INTO (ix,iy,layer l) with NO turn.
      * `Q1[iy, ix, l, di]` — same, WITH a direction change (turn) charged.
      * `Vq[iy, ix, l]`     — via hop landing on layer l at (ix,iy).

    `inf` marks impassable (a track-blocked target off the goal cell; a
    via-blocked cell). Every float sub-expression is formed in the SAME order as
    the scalar model so `np.rint(x*1000)` == `round(x*1000)` element-for-element.
    """
    import kicad_router_tool as rt

    R, C, L = win.rows, win.cols, len(win.layers)
    layers = win.layers
    g = win.grid
    gx, gy = goal_cell
    lp_kind = layer_purpose.get(net_kind, {})
    layer_types = win.layer_types

    # Per-direction distance units / mm (Python floats, identical to scalar).
    du = np.array([rt._SQRT2 if (dx and dy) else 1.0 for (dx, dy) in rt._MOVES], dtype=np.float64)  # (8,)
    dist_mm = du * g  # (8,)

    # Per-(layer, dir) direction factor and per-layer purpose factor.
    dirfac = np.ones((L, 8), dtype=np.float64)
    lp = np.ones(L, dtype=np.float64)
    off_home = np.zeros(L, dtype=np.float64)  # 1.0 where the away-from-home term applies
    for li_, layer in enumerate(layers):
        lp[li_] = float(lp_kind.get(layer_types[layer], 1.0))
        ld = directions.get(layer)
        for di, (dx, dy) in enumerate(rt._MOVES):
            dirfac[li_, di] = rt._direction_factor(weights, ld, dx, dy)
        if home_layer is not None and layer != home_layer:
            off_home[li_] = 1.0

    # Plane factor per (iy, ix, layer): NaN where the net's own fill is absent.
    pf = np.full((R, C, L), np.nan, dtype=np.float64)
    if plane_layers:
        plane_factor = model["plane_factor"]
        for li_, layer in enumerate(layers):
            if layer not in plane_layers:
                continue
            for iy in range(R):
                for ix in range(C):
                    v = plane_factor(ix, iy, layer)
                    if v is not None:
                        pf[iy, ix, li_] = v
    has_pf = ~np.isnan(pf)  # (R, C, L)

    # -- base (pre-turn, pre-corridor) cost, per (iy, ix, l, di) -------------- #
    # Normal branch:  base = ((step*du)*lp)*dirfac ; away extra = away_per_mm*dist_mm on off-home layers.
    step = weights.step
    step_du = step * du                                  # (8,)
    base_normal = (step_du[None, :] * lp[:, None]) * dirfac  # (L, 8)
    base_normal = np.broadcast_to(base_normal, (R, C, L, 8)).copy()
    away = (weights.away_from_home_per_mm * dist_mm)[None, :] * off_home[:, None]  # (L, 8)
    away = np.broadcast_to(away, (R, C, L, 8)).copy()

    # Plane branch:  base = ((step*du)*plane_step)*pf ; no away term.
    if plane_layers:
        base_plane = (step_du * plane_step)[None, None, None, :] * pf[..., None]  # (R,C,L,8)
        base = np.where(has_pf[..., None], base_plane, base_normal)
        away = np.where(has_pf[..., None], 0.0, away)
    else:
        base = base_normal

    # Corridor extra: off_corridor*dist_mm where the TARGET cell is off-corridor.
    corridor_extra = np.zeros((R, C, L, 8), dtype=np.float64)
    if corridor_cells is not None:
        off = np.ones((R, C), dtype=bool)
        for (cx, cy) in corridor_cells:
            if 0 <= cx < C and 0 <= cy < R:
                off[cy, cx] = False
        corridor_extra = off[:, :, None, None] * (weights.off_corridor * dist_mm)[None, None, None, :]

    # 7.20 crosstalk: per-mm surcharge on a planar move landing on a flagged
    # cell/layer. Built ONLY when the term is live - when `crosstalk_cells` is
    # None (the default) not a single array op is added here, which is what
    # makes an untuned project byte-identical to pre-7.20 (mirrors the scalar
    # `planar` closure's `xt_cells is not None` branch exactly).
    xt_cells = model.get("crosstalk_cells")
    if xt_cells:
        xt_pen = float(model.get("crosstalk_penalty", 0.0) or 0.0)
        xt_flag = np.zeros((R, C, L), dtype=bool)
        for layer, cs in xt_cells.items():
            if layer not in layer_types:
                continue
            li_ = layers.index(layer)
            for (cx, cy) in cs:
                if 0 <= cx < C and 0 <= cy < R:
                    xt_flag[cy, cx, li_] = True
        crosstalk_extra = xt_flag[..., None] * (xt_pen * dist_mm)[None, None, None, :]
        # Same summand ORDER as the scalar model: base + away + corridor + xt.
        s0 = base + away + corridor_extra + crosstalk_extra
    else:
        # S0 = base + away + corridor (same float add order as the scalar model).
        s0 = base + away + corridor_extra
    s1 = s0 + weights.direction_change

    q0 = np.rint(s0 * 1000.0).astype(np.int64)
    q1 = np.rint(s1 * 1000.0).astype(np.int64)

    # Congestion (per cell/layer) is added AFTER quantization, like the scalar.
    if congestion:
        cong = np.zeros((R, C, L), dtype=np.int64)
        for (cx, cy, layer), val in congestion.items():
            if layer in win.layer_types and 0 <= cx < C and 0 <= cy < R:
                cong[cy, cx, layers.index(layer)] += int(val)
        q0 = q0 + cong[..., None]
        q1 = q1 + cong[..., None]

    # Blocked planar targets -> inf (except the goal cell, which is always
    # enterable, matching the scalar `planar`'s goal exception).
    blocked = np.zeros((R, C, L), dtype=bool)
    for li_, layer in enumerate(layers):
        for (cx, cy) in win.blocked_track[layer]:
            if 0 <= cx < C and 0 <= cy < R:
                blocked[cy, cx, li_] = True
    if 0 <= gx < C and 0 <= gy < R:
        blocked[gy, gx, :] = False
    q0 = np.where(blocked[..., None], inf, q0)
    q1 = np.where(blocked[..., None], inf, q1)

    # -- via cost per (iy, ix, l) landing on layer l ------------------------- #
    via_base = np.full((R, C, L), weights.via * weights.through_via, dtype=np.float64)
    if plane_layers:
        if model.get("multilayer_attachment"):
            # 7.18.1: attachment surcharge scaled by the landed component's
            # island factor - same expression/order as the scalar `via`.
            via_base = np.where(
                has_pf, via_base + attachment_via_cost * np.nan_to_num(pf, nan=0.0), via_base)
        else:
            via_base = np.where(has_pf, via_base + attachment_via_cost, via_base)
    rp_bonus = float(model.get("return_path_bonus", 0.0) or 0.0)
    if rp_bonus:
        # 7.18.3: same per-(cell, layer) predicate the scalar model uses, then
        # the same discount + floor, so both backends stay bit-identical.
        #
        # M5 HOST/DEVICE DISCIPLINE: this whole function is HOST-side numpy by
        # design (see `fine_wavefront` - only the finished q0/q1/vq integer
        # fields cross to the device via `xp.asarray`). `rp_near` is a Python
        # closure over `_FillRaster.covers`, so it could not run on a device
        # anyway; building `near` here with plain `np` is therefore the
        # correct placement, not an oversight - the discount is baked into
        # `vq` before anything is transferred, and the gpu tier sees an
        # already-discounted integer field identical to the numpy tier's.
        near = np.zeros((R, C, L), dtype=bool)
        rp_near = model["return_path_near"]
        for li_, layer in enumerate(layers):
            for iy in range(R):
                for ix in range(C):
                    if rp_near(ix, iy, layer):
                        near[iy, ix, li_] = True
        floor = int(model.get("min_via_milli", 1))
        vq = np.where(
            near,
            np.maximum(np.rint((via_base - rp_bonus) * 1000.0).astype(np.int64), floor),
            np.rint(via_base * 1000.0).astype(np.int64),
        )
    else:
        vq = np.rint(via_base * 1000.0).astype(np.int64)
    if congestion:
        vq = vq + cong
    via_blocked = np.zeros((R, C), dtype=bool)
    for (cx, cy) in win.blocked_via:
        if 0 <= cx < C and 0 <= cy < R:
            via_blocked[cy, cx] = True
    vq = np.where(via_blocked[:, :, None], inf, vq)

    return q0, q1, vq


def fine_wavefront(
    win: Any, net_kind: str, weights: Any, layer_purpose: dict[str, Any],
    directions: dict[str, Any], start_cell: "tuple[int, int]",
    start_layers: "list[str]", goal_cell: "tuple[int, int]",
    goal_layers: "set[str]", home_layer: "str | None",
    corridor_cells: "set[tuple[int, int]] | None",
    congestion: "dict[tuple[int, int, str], int] | None" = None,
    plane_layers: "dict[str, list[dict[str, Any]]] | None" = None,
    goal_planes: "dict[str, list[dict[str, Any]]] | None" = None,
    plane_step: float = 0.0, attachment_via_cost: float = 0.0,
    # Phase 7.18 cost-model options, kept in the SAME positional slots
    # `_fine_astar` uses so `_fine_search`'s positional passthrough hands both
    # backends identical arguments. They must stay ahead of `xp`, which callers
    # only ever pass by keyword (see `_fine_search`'s gpu branch).
    multilayer_attachment: bool = False,
    return_path: "dict[str, Any] | None" = None,
    # Phase 7.19.1. Accepted so `_fine_search`'s positional passthrough hands
    # every backend the same argument list, but DELIBERATELY NOT FORWARDED to
    # `_build_fine_cost`: this tier is a full Bellman-Ford relaxation of the
    # whole window, so it has no frontier for a heuristic to order and would pay
    # for the backward Dijkstra without expanding one fewer cell. Everything the
    # wavefront still needs a heuristic FOR (the goal-state pick below, and the
    # shared backtrace) is pinned to the octile tie-break, which is exactly what
    # makes this tier's output provably independent of the flag - and therefore
    # the fixed point that cpu-with-field and cpu-without-field both match.
    goal_field: bool = False,
    # Phase 7.20 crosstalk. UNLIKE `goal_field` above, this one IS forwarded to
    # `_build_fine_cost`: it is a genuine cost-model term (it changes what a move
    # COSTS, not merely how the frontier is ordered), so a tier that ignored it
    # would compute a different field and break parity. It keeps the same
    # positional slot `_fine_astar` uses, ahead of `xp`.
    crosstalk: "dict[str, Any] | None" = None,
    xp: Any = None,
) -> "list[tuple[int, int, str]] | None":
    """numpy/GPU detailed search — drop-in for `kicad_router_tool._fine_astar`.

    Relaxes the integer milli-cost field to (reconstruction-)fixpoint, then hands
    the field to the shared deterministic backtrace. Returns the same
    `[(cx,cy,layer), ...]` path (or None) the cpu A* returns, byte-for-byte.

    `xp` selects the array module for the RELAXATION LOOP: `numpy` (default) or a
    CUDA array module (`cupy`) for the GPU tier. Everything that needs Python-level
    iteration — the cost-array construction, the plane/goal masks, the backtrace —
    stays on the HOST in numpy, and only the integer fields cross to the device.
    That split is deliberate: the loop is where the sweeps are, and host RAM on
    this class of machine dwarfs VRAM (see NETCLASS_PLAN.md's probe table), so the
    device should hold the iterated fields and nothing else.

    Parity is structural, not incidental: the arithmetic is integer milli-cost
    throughout (`_Weights.q` quantizes once on the host), so there is no float
    summation-order or fp32/fp64 divergence for a different array module to
    introduce — the device field is bit-identical to the numpy field, and the
    shared deterministic backtrace therefore reconstructs identical geometry."""
    import kicad_router_tool as rt
    if xp is None:
        xp = np

    inf = rt._FINE_INF
    model = rt._build_fine_cost(
        win, net_kind, weights, layer_purpose, directions, home_layer,
        corridor_cells, congestion, plane_layers, goal_planes, plane_step,
        attachment_via_cost, goal_cell, goal_layers,
        multilayer_attachment, return_path, False, crosstalk)
    li = model["li"]
    # 7.19.1: pinned octile tie-break (see the `goal_field` parameter comment).
    heuristic = model["tiebreak_heuristic"]
    layers = win.layers
    R, C, L = win.rows, win.cols, len(layers)

    # Start states — identical selection rule to `_fine_astar`.
    start_states = [(start_cell[0], start_cell[1], l, -1) for l in start_layers
                    if start_cell not in win.blocked_track.get(l, set())]
    if not start_states:
        start_states = [(start_cell[0], start_cell[1], start_layers[0], -1)] if start_layers else []
    if not start_states:
        return None

    q0, q1, vq = _build_cost_arrays(
        win, model, weights, layer_purpose, directions, net_kind, home_layer,
        corridor_cells, congestion, plane_layers, plane_step,
        attachment_via_cost, goal_cell, inf)

    # Field over (iy, ix, layer, dir-code); start states at 0. Built on the host
    # (scalar assignment per start state) then moved to the device once.
    field_h = np.full((R, C, L, _N_DIR), inf, dtype=np.int64)
    for (sx, sy, l, _d) in start_states:
        field_h[sy, sx, li[l], _NO_DIR] = 0

    # Goal mask over (iy, ix, layer): the exact goal cell on a goal layer, plus
    # any plane-goal component cell (relaxed termination for plane nets).
    goal_cl = np.zeros((R, C, L), dtype=bool)
    gx, gy = goal_cell
    if 0 <= gx < C and 0 <= gy < R:
        for l in goal_layers:
            if l in li:
                goal_cl[gy, gx, li[l]] = True
    if goal_planes:
        for layer, comps in goal_planes.items():
            if layer not in li:
                continue
            li_ = li[layer]
            for iy in range(R):
                for ix in range(C):
                    nx, ny = win.node_xy(ix, iy)
                    if any(c["raster"].covers(nx, ny, 0.0) for c in comps):
                        goal_cl[iy, ix, li_] = True
    if not goal_cl.any():
        return None

    # ---- host -> device: only the integer fields the loop actually iterates -- #
    if xp is np:
        field = field_h
        goal_mask = np.broadcast_to(goal_cl[..., None], (R, C, L, _N_DIR))
    else:
        field = xp.asarray(field_h)
        q0, q1, vq = xp.asarray(q0), xp.asarray(q1), xp.asarray(vq)
        goal_mask = xp.broadcast_to(xp.asarray(goal_cl)[..., None], (R, C, L, _N_DIR))

    moves = rt._MOVES
    max_sweeps = 2 * (R + C) + 64
    goal_cost = inf
    for _sweep in range(max_sweeps):
        prev = field
        new = field.copy()
        # -- planar relaxation: for each target heading di, gather the source --
        for di, (dx, dy) in enumerate(moves):
            src = _shift_from(field, dx, dy, inf, xp)          # (R,C,L,9)
            src_planar = src[..., :8]                          # (R,C,L,8)
            # no-turn sources: same heading di, or the no-heading code.
            noturn = xp.minimum(src[..., di], src[..., _NO_DIR])
            # turn sources: any planar heading != di.
            turnm = _leave_one_out_min(src_planar, di, inf, xp)
            cand = xp.minimum(noturn + q0[..., di], turnm + q1[..., di])
            xp.minimum(new[..., di], cand, out=new[..., di])
        # -- via relaxation: land on layer l from any other layer, same dir ----
        # min over source layers for each dir-code, leave-one-out on the target l.
        # field: (R,C,L,9). For target layer l: min over ol!=l of field[:,:,ol,:].
        if L >= 2:
            # prefix/suffix min over the layer axis.
            pre = xp.full((R, C, L, _N_DIR), inf, dtype=np.int64)
            suf = xp.full((R, C, L, _N_DIR), inf, dtype=np.int64)
            acc = xp.full((R, C, _N_DIR), inf, dtype=np.int64)
            for l in range(L):
                pre[:, :, l, :] = acc
                acc = xp.minimum(acc, field[:, :, l, :])
            acc = xp.full((R, C, _N_DIR), inf, dtype=np.int64)
            for l in range(L - 1, -1, -1):
                suf[:, :, l, :] = acc
                acc = xp.minimum(acc, field[:, :, l, :])
            other_layer_min = xp.minimum(pre, suf)             # (R,C,L,9)
            via_cand = other_layer_min + vq[..., None]         # (R,C,L,9)
            xp.minimum(new, via_cand, out=new)

        field = new
        # Reachable-goal cost so far.
        gvals = xp.where(goal_mask, field, inf)
        gc = int(gvals.min())
        goal_cost = gc
        changed = field != prev
        if gc < inf:
            # Only states with cost <= goal_cost can appear on a reconstruction;
            # once none of THOSE changed this sweep, the field the backtrace
            # needs is at fixpoint (Dijkstra-optimal) — stop.
            if not bool((changed & (field <= gc)).any()):
                break
        else:
            if not bool(changed.any()):
                return None  # fully converged with no goal reached: unreachable

    if goal_cost >= inf:
        return None

    # ---- device -> host: the backtrace is scalar/Python, so it runs on the ---- #
    # host field. Integer fields transfer exactly, so this is lossless.
    if xp is not np:
        field = xp.asnumpy(field) if hasattr(xp, "asnumpy") else np.asarray(field.get())
        goal_mask = np.broadcast_to(goal_cl[..., None], (R, C, L, _N_DIR))

    # Pick the goal STATE the cpu A* would have popped first: min heap key
    # (f = cost + h, cost, cx, cy, layer_index, dir).  h == 0 at the exact goal
    # cell so this reduces to (cost, ...) there; general for plane goals.
    gys, gxs, gls, gds = np.where((field <= goal_cost) & goal_mask)
    best_key = None
    goal_state = None
    for iy, ix, l, dc in zip(gys.tolist(), gxs.tolist(), gls.tolist(), gds.tolist()):
        cost = int(field[iy, ix, l, dc])
        d = -1 if dc == _NO_DIR else dc
        key = (cost + heuristic(ix, iy), cost, ix, iy, l, d)
        if best_key is None or key < best_key:
            best_key, goal_state = key, (ix, iy, layers[l], d)
    if goal_state is None:
        return None

    def cost_get(state: "tuple[int, int, str, int]") -> "int | None":
        cx, cy, layer, d = state
        if layer not in li or not (0 <= cx < C and 0 <= cy < R):
            return None
        dc = _NO_DIR if d == -1 else d
        v = int(field[cy, cx, li[layer], dc])
        return None if v >= inf else v

    return rt._fine_backtrace(win, model, cost_get, goal_state, start_states)


# =========================================================================== #
# Phase 7.8 GPU TIER - batching, VRAM budgeting, and per-item OOM fallback.
#
# Scope and honest limits (read before extending):
#
# * The GPU runs the SAME wavefront kernel as the numpy tier, via `fine_wavefront
#   (..., xp=<cuda array module>)`. There is no second algorithm to keep in sync,
#   which is what makes tier parity structural rather than something to re-prove
#   per kernel: the arithmetic is integer milli-cost end to end, so no array
#   module can introduce float divergence, and the shared deterministic backtrace
#   turns an identical field into identical geometry.
# * `cupy` is the supported CUDA array module. `torch` is DETECTED and reported,
#   but not driven: torch is not a numpy drop-in for the ops this kernel uses
#   (`rint`, one-argument `where`, `minimum(..., out=)`), so it would need a
#   separate adapter with its own semantics to get wrong - real risk, and none of
#   the parity/OOM gates this tier is accepted on need it. Recorded as a residual
#   rather than half-built.
# * Batching is implemented as MEMORY-PLANNED STREAMING (`plan_batches` /
#   `run_windows`): work items are grouped so each group fits the VRAM budget and
#   groups are processed in order, never all-at-once, with per-item demotion when
#   even a single item does not fit. A single FUSED multi-window kernel (one
#   tensor spanning several windows) is NOT implemented - it is purely a speed
#   optimization, and measuring speedups is explicitly out of scope here, whereas
#   the memory discipline and the demotion path are correctness-visible.
# =========================================================================== #


class GpuUnavailable(RuntimeError):
    """No usable CUDA array module (the normal answer on a machine without one)."""


class GpuOutOfMemory(RuntimeError):
    """A work item does not fit the VRAM budget, or the device allocator failed
    mid-run. Always recoverable: the caller demotes the item to numpy/cpu."""


# Bytes of DEVICE memory the wavefront holds per (row x col x layer) element.
# The loop keeps several (R, C, L, 9) int64 fields live at once (`field`, `new`,
# the shifted source, the prefix/suffix layer minima, the via candidate, the
# goal/changed masks) plus the two (R, C, L, 8) cost arrays and the (R, C, L) via
# cost. Counted generously on purpose: over-estimating makes the planner tile
# SMALLER, which is safe; under-estimating causes the runtime OOM this budget
# exists to avoid. Runtime OOM is still caught and demoted, so this constant is a
# first line of defence, not the only one.
_DEVICE_FIELDS_9 = 8      # concurrent (R, C, L, 9) int64 arrays
_DEVICE_FIELDS_8 = 2      # concurrent (R, C, L, 8) int64 arrays (q0, q1)
_DEVICE_FIELDS_1 = 1      # concurrent (R, C, L)    int64 arrays (vq)
_BYTES_PER_INT64 = 8


def estimate_window_device_bytes(rows: int, cols: int, layers: int) -> int:
    """Device footprint the wavefront needs for one window. See the constants
    above for why this deliberately over-counts."""
    per_element = _BYTES_PER_INT64 * (_DEVICE_FIELDS_9 * _N_DIR
                                      + _DEVICE_FIELDS_8 * 8
                                      + _DEVICE_FIELDS_1)
    return max(1, rows) * max(1, cols) * max(1, layers) * per_element


def _probe_vram_nvidia_smi() -> "tuple[int, int, str] | None":
    """(free_mb, total_mb, name) from `nvidia-smi`, or None. Used only for
    REPORTING when no array module is importable - knowing a GPU exists but is
    unusable is a much better diagnostic than a bare "no GPU"."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        out = subprocess.run(
            [exe, "--query-gpu=memory.free,memory.total,name",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    parts = [p.strip() for p in out.stdout.strip().splitlines()[0].split(",")]
    if len(parts) < 3:
        return None
    try:
        return int(float(parts[0])), int(float(parts[1])), parts[2]
    except ValueError:
        return None


def probe_gpu() -> dict[str, Any]:
    """Probe the ACTUAL machine for a usable CUDA array module and its free VRAM.

    Never raises and never caches: "no GPU" is a normal answer, and a run on a
    different box (or the same box under different load - a desktop compositor
    holds VRAM) must plan itself from scratch. Nothing here is ever written to
    either JSON; the JSONs carry only budget OVERRIDES a user chose."""
    info: dict[str, Any] = {
        "available": False, "module": None, "name": None,
        "free_mb": 0, "total_mb": 0, "source": "none", "reason": None,
    }
    try:
        import cupy  # type: ignore
    except Exception as exc:
        info["reason"] = f"cupy not importable ({type(exc).__name__})"
    else:  # pragma: no cover - depends on the box having cupy
        try:
            dev = cupy.cuda.Device()
            free_b, total_b = dev.mem_info
            info.update({"available": True, "module": "cupy",
                         "free_mb": int(free_b // (1024 * 1024)),
                         "total_mb": int(total_b // (1024 * 1024)),
                         "source": "cupy", "reason": None})
            try:
                props = cupy.cuda.runtime.getDeviceProperties(dev.id)
                nm = props.get("name")
                info["name"] = nm.decode() if isinstance(nm, bytes) else str(nm)
            except Exception:
                pass
            return info
        except Exception as exc:
            info["reason"] = f"cupy present but device unusable ({type(exc).__name__})"

    try:
        import torch  # type: ignore
    except Exception:
        pass
    else:  # pragma: no cover - depends on the box having torch
        try:
            if torch.cuda.is_available():
                info["reason"] = ("a CUDA torch is installed but this tier drives "
                                  "cupy only (see the module comment); install cupy "
                                  "to enable the GPU tier")
                info["name"] = torch.cuda.get_device_name(0)
        except Exception:
            pass

    smi = _probe_vram_nvidia_smi()
    if smi is not None:
        free_mb, total_mb, name = smi
        info.update({"name": name, "free_mb": free_mb, "total_mb": total_mb,
                     "source": "nvidia-smi"})
        if info["reason"] is None:
            info["reason"] = "GPU present but no CUDA array module importable"
    return info


def gpu_array_module(probe: "dict[str, Any] | None" = None) -> Any:
    """The CUDA array module to pass as `xp`, or raise `GpuUnavailable`."""
    probe = probe_gpu() if probe is None else probe
    if not probe.get("available") or probe.get("module") != "cupy":
        raise GpuUnavailable(probe.get("reason") or "no CUDA array module")
    import cupy  # type: ignore  # pragma: no cover - depends on the box
    return cupy  # pragma: no cover


def gpu_memory_budget_bytes(settings: dict[str, Any],
                            probe: "dict[str, Any] | None" = None) -> int:
    """Resolve `autorouter.gpu.memory_budget_mb` to a byte budget.

    0 (the default) = AUTO: probe free VRAM now and reserve a slice of it for the
    driver/other apps rather than claiming the lot. An explicit value is taken as
    given - that is the whole point of an override."""
    gcfg = (settings.get("autorouter", {}) or {}).get("gpu", {}) or {}
    mb = int(gcfg.get("memory_budget_mb", 0) or 0)
    if mb > 0:
        return mb * 1024 * 1024
    probe = probe_gpu() if probe is None else probe
    free_mb = int(probe.get("free_mb", 0) or 0)
    if free_mb <= 0:
        return 0
    # Reserve 25% (min 128 MB) of what is free for the allocator's own
    # fragmentation headroom and whatever else is drawing on the desktop.
    usable = max(0, free_mb - max(128, free_mb // 4))
    return usable * 1024 * 1024


def plan_batches(item_bytes: "list[int]", budget_bytes: int,
                 max_batch: "int | None" = None) -> "tuple[list[list[int]], list[int]]":
    """Group work items into batches that each fit `budget_bytes`.

    Returns `(batches, oversized)` where `batches` is a list of index lists (in
    the ORIGINAL order - batching never reorders work, so it cannot perturb any
    downstream canonical-order commit) and `oversized` lists the indices whose
    single-item footprint exceeds the budget on its own. Those are the items the
    caller must demote to numpy/cpu: tiling down as far as batch = one window is
    exactly what the plan calls for, and this is the point past which tiling
    cannot help.

    A zero/negative budget means "nothing fits" - every item is oversized, which
    is the correct answer when there is no usable VRAM at all."""
    batches: list[list[int]] = []
    oversized: list[int] = []
    cur: list[int] = []
    cur_bytes = 0
    for i, nb in enumerate(item_bytes):
        if budget_bytes <= 0 or nb > budget_bytes:
            oversized.append(i)
            continue
        if cur and (cur_bytes + nb > budget_bytes
                    or (max_batch is not None and len(cur) >= max_batch)):
            batches.append(cur)
            cur, cur_bytes = [], 0
        cur.append(i)
        cur_bytes += nb
    if cur:
        batches.append(cur)
    return batches, oversized


def resolve_batch_limit(settings: dict[str, Any]) -> "int | None":
    """`autorouter.gpu.batch`: "auto" (or 0/absent) = let the memory budget
    decide; a positive integer caps items per batch on top of that."""
    gcfg = (settings.get("autorouter", {}) or {}).get("gpu", {}) or {}
    raw = gcfg.get("batch", "auto")
    if isinstance(raw, str):
        return None
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def oom_fallback_enabled(settings: dict[str, Any]) -> bool:
    gcfg = (settings.get("autorouter", {}) or {}).get("gpu", {}) or {}
    return bool(gcfg.get("oom_fallback", True))


def _is_oom(exc: BaseException) -> bool:
    """Is this a device out-of-memory condition (as opposed to a real bug)?

    Matched structurally (by name/message) rather than by importing cupy's
    exception type, so this works with a simulated device in tests and does not
    make cupy a hard import."""
    if isinstance(exc, (GpuOutOfMemory, MemoryError)):
        return True
    name = type(exc).__name__.lower()
    return "outofmemory" in name or "out of memory" in str(exc).lower()


def run_windows(
    items: "list[dict[str, Any]]", settings: dict[str, Any],
    gpu_call: Any, fallback_call: Any,
    probe: "dict[str, Any] | None" = None,
) -> "tuple[list[Any], dict[str, Any]]":
    """Stream `items` through the GPU tier, demoting per item as needed.

    `items` are dicts carrying at least `rows`/`cols`/`layers` (for the footprint
    estimate) and whatever payload the two callables need. `gpu_call(item, xp)`
    computes on the device; `fallback_call(item)` is the numpy/cpu reference. The
    return is `(results_in_input_order, report)`.

    Demotion rules - none of them ever abort the run:
      * no usable device            -> every item falls back;
      * item alone exceeds budget   -> that item falls back (tiling bottoms out);
      * allocator OOM at runtime    -> retry the batch at HALF size, then demote
                                       the individual item.
    Every demotion is COUNTED in `report`, so "the GPU helped 90% of this board"
    is visible rather than silent. With `gpu.oom_fallback` false, an OOM is raised
    instead of demoted (a debugging/benchmarking choice, never the default).

    Determinism: results are returned in INPUT order and each item is computed by
    a pure function of its own payload, so which executor ran an item cannot
    affect the answer - the same discipline the cpu/numpy tiers already keep."""
    report: dict[str, Any] = {
        "backend": "gpu", "items": len(items), "on_gpu": 0,
        "demoted_no_device": 0, "demoted_oversized": 0, "demoted_oom": 0,
        "batches": 0, "budget_mb": 0, "device": None, "reason": None,
    }
    probe = probe_gpu() if probe is None else probe
    report["device"] = probe.get("name")
    results: list[Any] = [None] * len(items)

    try:
        xp = gpu_array_module(probe)
    except GpuUnavailable as exc:
        report["reason"] = str(exc)
        report["demoted_no_device"] = len(items)
        for i, it in enumerate(items):
            results[i] = fallback_call(it)
        return results, report

    budget = gpu_memory_budget_bytes(settings, probe)
    report["budget_mb"] = budget // (1024 * 1024)
    sizes = [estimate_window_device_bytes(it.get("rows", 0), it.get("cols", 0),
                                          it.get("layers", 1)) for it in items]
    batches, oversized = plan_batches(sizes, budget, resolve_batch_limit(settings))
    report["batches"] = len(batches)
    allow_fallback = oom_fallback_enabled(settings)

    for i in oversized:
        if not allow_fallback:
            raise GpuOutOfMemory(
                f"window {i} needs {sizes[i]} bytes, budget is {budget} bytes")
        report["demoted_oversized"] += 1
        results[i] = fallback_call(items[i])

    pending = list(batches)
    while pending:
        batch = pending.pop(0)
        try:
            for i in batch:
                results[i] = gpu_call(items[i], xp)
                report["on_gpu"] += 1
        except Exception as exc:
            if not (_is_oom(exc) and allow_fallback):
                raise
            # Undo this batch's partial credit, then retry at half size; a batch
            # of one that still OOMs is demoted item by item.
            report["on_gpu"] -= sum(1 for i in batch if results[i] is not None)
            for i in batch:
                results[i] = None
            if len(batch) > 1:
                mid = len(batch) // 2
                pending.insert(0, batch[mid:])
                pending.insert(0, batch[:mid])
                report["batches"] += 1
            else:
                report["demoted_oom"] += 1
                results[batch[0]] = fallback_call(items[batch[0]])
    return results, report


def probe_system_resources() -> dict[str, Any]:
    """Hardware the planner actually has THIS run: free (not installed) RAM, core
    count, and GPU/VRAM. Probed fresh every call, never cached and never written
    to either JSON - a run on a different box, or the same box under different
    load, plans itself from scratch (NETCLASS_PLAN.md 7.8, "probe the machine it
    is running on, every run"). Reported in the run report so a slow run is
    diagnosable ("batches were tiny because only 1.1 GB VRAM was free")."""
    info: dict[str, Any] = {
        "cpu_count": os.cpu_count() or 1,
        "ram_total_mb": 0, "ram_free_mb": 0, "ram_source": "unavailable",
        "gpu": probe_gpu(),
    }
    try:
        if os.name == "nt":
            import ctypes

            class _MemStatus(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong),
                            ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            st = _MemStatus()
            st.dwLength = ctypes.sizeof(_MemStatus)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
                info["ram_total_mb"] = int(st.ullTotalPhys // (1024 * 1024))
                info["ram_free_mb"] = int(st.ullAvailPhys // (1024 * 1024))
                info["ram_source"] = "GlobalMemoryStatusEx"
        else:
            with open("/proc/meminfo", "r", encoding="utf-8") as fh:
                vals: dict[str, int] = {}
                for line in fh:
                    k, _, rest = line.partition(":")
                    parts = rest.split()
                    if parts and parts[0].isdigit():
                        vals[k.strip()] = int(parts[0])
            if vals:
                info["ram_total_mb"] = vals.get("MemTotal", 0) // 1024
                info["ram_free_mb"] = vals.get("MemAvailable",
                                               vals.get("MemFree", 0)) // 1024
                info["ram_source"] = "/proc/meminfo"
    except Exception:
        pass  # "unknown" is a normal answer; never fail a routing run over a probe
    return info
