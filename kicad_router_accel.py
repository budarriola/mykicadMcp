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
from typing import Any

import numpy as np

# Direction-code layout for the state axis: codes 0..7 are the eight planar
# headings (index into `_MOVES`), code 8 is "no heading" (dir == -1, a start
# state or a state reached only by via hops before any planar move).
_NO_DIR = 8
_N_DIR = 9


def _shift_from(field: np.ndarray, dx: int, dy: int, inf: int) -> np.ndarray:
    """`out[iy, ix, ...] = field[iy - dy, ix - dx, ...]`, out-of-window = `inf`.

    i.e. the value carried by the *source* cell of a planar move in direction
    (dx, dy) into (ix, iy). Pure array slicing (no wraparound), so every border
    move correctly reads `inf` from beyond the window edge."""
    out = np.full_like(field, inf)
    R, C = field.shape[0], field.shape[1]
    ys, ye = max(0, dy), min(R, R + dy)
    xs, xe = max(0, dx), min(C, C + dx)
    if ys >= ye or xs >= xe:
        return out
    out[ys:ye, xs:xe] = field[ys - dy:ye - dy, xs - dx:xe - dx]
    return out


def _leave_one_out_min(planar_dir_min: np.ndarray, exclude: int, inf: int) -> np.ndarray:
    """min over the 8 planar direction-codes EXCLUDING `exclude`, given the
    (R, C, L, 8) array. Uses a prefix/suffix min so it is O(1) array ops."""
    # planar_dir_min: (..., 8). Return (...) = min over axis -1 skipping `exclude`.
    pre = np.full(planar_dir_min.shape[:-1], inf, dtype=planar_dir_min.dtype)
    if exclude > 0:
        pre = planar_dir_min[..., :exclude].min(axis=-1)
    suf = np.full(planar_dir_min.shape[:-1], inf, dtype=planar_dir_min.dtype)
    if exclude < 7:
        suf = planar_dir_min[..., exclude + 1:].min(axis=-1)
    return np.minimum(pre, suf)


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

    # S0 = base + away + corridor  (same float add order as the scalar model).
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
    multilayer_attachment: bool = False,
    return_path: "dict[str, Any] | None" = None,
) -> "list[tuple[int, int, str]] | None":
    """numpy detailed search — drop-in for `kicad_router_tool._fine_astar`.

    Relaxes the integer milli-cost field to (reconstruction-)fixpoint, then hands
    the field to the shared deterministic backtrace. Returns the same
    `[(cx,cy,layer), ...]` path (or None) the cpu A* returns, byte-for-byte."""
    import kicad_router_tool as rt

    inf = rt._FINE_INF
    model = rt._build_fine_cost(
        win, net_kind, weights, layer_purpose, directions, home_layer,
        corridor_cells, congestion, plane_layers, goal_planes, plane_step,
        attachment_via_cost, goal_cell, goal_layers,
        multilayer_attachment, return_path)
    li = model["li"]
    heuristic = model["heuristic"]
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

    # Field over (iy, ix, layer, dir-code); start states at 0.
    field = np.full((R, C, L, _N_DIR), inf, dtype=np.int64)
    for (sx, sy, l, _d) in start_states:
        field[sy, sx, li[l], _NO_DIR] = 0

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
    goal_mask = np.broadcast_to(goal_cl[..., None], (R, C, L, _N_DIR))
    if not goal_cl.any():
        return None

    moves = rt._MOVES
    max_sweeps = 2 * (R + C) + 64
    goal_cost = inf
    for _sweep in range(max_sweeps):
        prev = field
        new = field.copy()
        # -- planar relaxation: for each target heading di, gather the source --
        for di, (dx, dy) in enumerate(moves):
            src = _shift_from(field, dx, dy, inf)              # (R,C,L,9)
            src_planar = src[..., :8]                          # (R,C,L,8)
            # no-turn sources: same heading di, or the no-heading code.
            noturn = np.minimum(src[..., di], src[..., _NO_DIR])
            # turn sources: any planar heading != di.
            turnm = _leave_one_out_min(src_planar, di, inf)
            cand = np.minimum(noturn + q0[..., di], turnm + q1[..., di])
            np.minimum(new[..., di], cand, out=new[..., di])
        # -- via relaxation: land on layer l from any other layer, same dir ----
        # min over source layers for each dir-code, leave-one-out on the target l.
        # field: (R,C,L,9). For target layer l: min over ol!=l of field[:,:,ol,:].
        if L >= 2:
            # prefix/suffix min over the layer axis.
            pre = np.full((R, C, L, _N_DIR), inf, dtype=np.int64)
            suf = np.full((R, C, L, _N_DIR), inf, dtype=np.int64)
            acc = np.full((R, C, _N_DIR), inf, dtype=np.int64)
            for l in range(L):
                pre[:, :, l, :] = acc
                acc = np.minimum(acc, field[:, :, l, :])
            acc = np.full((R, C, _N_DIR), inf, dtype=np.int64)
            for l in range(L - 1, -1, -1):
                suf[:, :, l, :] = acc
                acc = np.minimum(acc, field[:, :, l, :])
            other_layer_min = np.minimum(pre, suf)             # (R,C,L,9)
            via_cand = other_layer_min + vq[..., None]         # (R,C,L,9)
            np.minimum(new, via_cand, out=new)

        field = new
        # Reachable-goal cost so far.
        gvals = np.where(goal_mask, field, inf)
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
