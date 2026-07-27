"""Phase 7.6 - iterative whole-board optimization (`optimize_kicad_board`).

The "make the best board" loop. One number decides everything:

    S = SUM net trace cost        (Phase 6 `get_trace_cost` board total, which
                                   already carries the 7.2 layer-purpose
                                   penalties and the Phase 5 deviation term)
      + SUM plane island cost     (7.5.3, `audit_plane_islands` summary)
      + unrouted_penalty x unrouted_connection_count   (`get_ratsnest`)

Every term is already defined in `pcb_settings.json`, so "best" is exactly what
the JSON says it is - the optimizer invents no cost of its own. It is a THIN
orchestrator over the existing tools in `kicad_router_tool` / `kicad_pcb_tool`
(route/unroute, propose/create/modify plane, ratsnest, trace cost, island
audit); it duplicates no routing, scoring or writing logic. The one piece of
new board surgery it owns is the bare stitching via (`_place_stitching_via`),
because nothing else on the board ever emits a via that is not part of a route.

WHY A SCRATCH COPY RATHER THAN A PARALLEL IN-MEMORY BOARD MODEL
---------------------------------------------------------------
The plan asks for iteration on "an in-memory board model - the file is
untouched until the final confirmed write". The REQUIREMENT there is that the
user's real board is never touched until they confirm; "in-memory" is the
plan's shorthand for how to get that, not the goal itself. Building a genuine
second representation of zones+tracks+vias would fork every writer in this
codebase (`route_nets`, `unroute_nets`, `create_plane`, `modify_plane` all do
uuid-anchored s-expr surgery on the FILE) into a file path and a memory path -
two implementations of the same semantics, the exact duplication this plan
avoids everywhere else, and a second place for a determinism bug to hide.

So the model here is a PRIVATE SCRATCH COPY of the whole project (the pattern
`benchmark_autoroute` already uses via `_copy_project_to_scratch`): the session
owns a temp directory holding board + `.kicad_pro` + `.net` + `pcb_settings.json`
+ board-local state, and every iteration mutates THAT. The real board file is
opened read-only exactly once, at session creation, to be copied. `write=True`
copies the scratch board's final accepted state back over the real board and
merges the scratch's `autorouter_owned` into the real board-local state. The
user-visible contract is identical to the plan's; the implementation reuses
every existing writer unchanged, including their safety guards.

Those guards are why human copper is safe here without the optimizer
re-implementing a single check: `unroute_nets` only ever deletes uuids recorded
in `autorouter_owned` (a hand-routed track is not in that list, so a rip-up
move against a human-routed net is a no-op), and `modify_plane` REFUSES any
zone uuid not in `autorouter_owned.zones` (the six hand-made kiln zones can
never be auto-mutated). The optimizer never bypasses either; it calls them with
their defaults, so `allow_hand_copper_ripup` stays off on every move.

SESSIONS, NOT MARATHONS
-----------------------
One MCP call must never run the whole optimization. `optimize_board` runs a
BOUNDED chunk (`max_iterations_per_call` iterations or `max_seconds` wall
clock, whichever binds first) and returns `{session_id, state, score_curve,
...}` with `state` in `running | converged | budget_exhausted`. Everything
needed to resume - RNG state, iteration counter, SA temperature, cumulative
elapsed time, score curve, move history, the scratch directory path - is
checkpointed into the board-local JSON under `optimizer_sessions`, so a session
survives an MCP restart and is inspectable read-only via `get_route_session`.

Because ALL loop state (including the RNG) round-trips through that checkpoint,
a chunked run and a single big-budget run take byte-identical decisions: the
call boundary is not an input to any decision. (The one exception is the wall
clock - a run stopped by `max_seconds` obviously diverges from one that wasn't.
Tests pin this by bounding on iterations only.)

Phase 7.7 (AI-in-the-loop decisions: the `awaiting_decision` state,
`decide_kicad_route`, and the `optimizer.ai_decisions` settings block) is
DELIBERATELY NOT IMPLEMENTED here - it is a separate landing. This module has
three session states, not four, and never reads `optimizer.ai_decisions`. Where
7.7 would pause on a near-tie between candidate moves, this optimizer simply
takes the best-scored candidate.
"""

from __future__ import annotations

import json
import math
import random
import shutil
import tempfile
import time
import uuid as _uuid
from pathlib import Path
from typing import Any

import kicad_pcb_tool as _pcb
import kicad_router_tool as _r

# The three session states this phase implements. 7.7's `awaiting_decision` is
# intentionally absent (see module docstring) rather than declared-and-unused,
# so a caller cannot be told a state exists that nothing can ever produce.
SESSION_STATES = ("running", "converged", "budget_exhausted")

# Hard cap on how many candidate moves one iteration evaluates. Each candidate
# costs a full board copy + reroute + rescore, so this is the knob that keeps a
# single iteration inside a tool-call budget. It is deliberately NOT a
# `pcb_settings.json` field: it is an implementation cost bound, not design
# policy (unlike `worst_k`, which changes WHICH parts of the board get looked
# at and therefore belongs in the settings file).
_MAX_CANDIDATES_PER_ITERATION = 6

# Multiplier applied to a net's CURRENT dominant-layer purpose weight when the
# layer-swap move (c) reroutes it. Large enough that the reroute genuinely
# prefers a different layer type, finite so a net with nowhere else to go still
# routes (the "the weights decide, the router never hard-forbids" rule from
# 7.3c) rather than failing and costing an unrouted-connection penalty.
_LAYER_SWAP_PENALTY = 6.0


# --------------------------------------------------------------------------- #
# Board score - the single number the whole loop optimizes
# --------------------------------------------------------------------------- #

def score_board(project_path: str | Path) -> dict[str, Any]:
    """Score a board with the Phase 7.6 objective (see module docstring).

    Read-only. Returns the total plus every component term, so a score is
    self-explaining in the report - a caller can see whether a move paid off in
    copper, in planes, or in completing a connection.
    """
    settings = _pcb.load_pcb_settings(project_path)["config"]
    unrouted_penalty = float(settings.get("optimizer", {}).get("unrouted_penalty", 500.0))

    trace = _pcb.get_trace_cost(project_path)
    trace_total = float(trace["board_totals"]["total"])

    # Zone/plane costing degrades to zero rather than failing the whole score:
    # a board with no zones at all is a perfectly valid input.
    try:
        planes = _r.audit_plane_islands(project_path)
        plane_total = float(planes["summary"]["total_island_cost"])
    except Exception:  # pragma: no cover - defensive
        plane_total = 0.0

    rats = _r.get_ratsnest(project_path)
    unrouted = int(rats["summary"]["total_connections"])

    total = trace_total + plane_total + unrouted_penalty * unrouted
    return {
        "total": round(total, 4),
        "trace_cost": round(trace_total, 4),
        "plane_cost": round(plane_total, 4),
        "unrouted_count": unrouted,
        "unrouted_penalty": unrouted_penalty,
        "unrouted_cost": round(unrouted_penalty * unrouted, 4),
    }


def _ranked_nets(project_path: str | Path) -> list[dict[str, Any]]:
    """Every net that contributes to `S`, worst-contribution-first.

    Two kinds of contributor, both ranked on the SAME scale because both are
    terms of the same objective:
      - routed nets, at their `get_trace_cost` total;
      - UNROUTED nets, at `unrouted_penalty x their missing-connection count`.
    Including the unrouted ones matters: on a board that is not yet fully
    routed they are by far the largest cost contribution (the penalty dwarfs
    any copper), so ranking only routed nets would send `worst_k` chasing
    rounding noise while the real cost sat untouched. A rip-up+reroute move
    against an unrouted net degrades naturally into "route it" - `unroute_nets`
    removes nothing, `route_nets` does the work.

    The secondary sort on net NAME keeps equal-cost nets in a stable order -
    otherwise "the worst k nets" would depend on parse order and a run would
    stop being reproducible from its seed alone.
    """
    settings = _pcb.load_pcb_settings(project_path)["config"]
    unrouted_penalty = float(settings.get("optimizer", {}).get("unrouted_penalty", 500.0))

    trace = _pcb.get_trace_cost(project_path)
    nets = [dict(n) for n in trace["nets"]]
    by_name = {n["net"]: n for n in nets}

    missing: dict[str, int] = {}
    for conn in _r.get_ratsnest(project_path).get("connections", []):
        missing[conn["net"]] = missing.get(conn["net"], 0) + 1
    power_patterns = settings.get("layer_purpose", {}).get("power_net_patterns", [])
    for net_name, count in missing.items():
        penalty = unrouted_penalty * count
        entry = by_name.get(net_name)
        if entry is None:
            entry = {
                "net": net_name,
                "net_kind": _pcb._net_kind(net_name, power_net_patterns=power_patterns),
                "on_bus": False,
                "metrics": {"length_mm": 0.0, "via_count": 0, "layers_used": 0,
                            "layer_lengths_mm": {}},
                "cost": {"total": 0.0},
            }
            nets.append(entry)
        entry["unrouted_connections"] = count
        entry["contribution"] = float(entry["cost"]["total"]) + penalty

    for entry in nets:
        entry.setdefault("unrouted_connections", 0)
        entry.setdefault("contribution", float(entry["cost"]["total"]))

    nets.sort(key=lambda n: (-n["contribution"], n["net"]))
    return nets


# --------------------------------------------------------------------------- #
# Session persistence - board-local JSON, so a session outlives the process
# --------------------------------------------------------------------------- #

def _rng_state_to_json(rng: random.Random) -> list[Any]:
    """`random.Random.getstate()` is a tuple containing a 625-int tuple; JSON
    has no tuples, so store it as nested lists and rebuild on load. Storing the
    RNG STATE (not just the seed) is what makes a resumed session take exactly
    the decisions the unbroken run would have."""
    version, internal, gauss = rng.getstate()
    return [version, list(internal), gauss]


def _rng_from_json(state: list[Any]) -> random.Random:
    rng = random.Random()
    version, internal, gauss = state
    rng.setstate((version, tuple(internal), gauss))
    return rng


def _board_fingerprint(project_path: str | Path) -> dict[str, Any]:
    """Cheap identity of the real board file (size + mtime + first/last bytes
    length is overkill; size+mtime is what every cache in this codebase already
    trusts for invalidation). Recorded at session creation and re-checked
    before `write=True`, so a session cannot silently overwrite a board the
    user edited in KiCad while the optimizer was mid-run."""
    board_path, _, _ = _pcb._resolve_project_path(project_path)
    stat = board_path.stat()
    return {"path": str(board_path), "size": stat.st_size, "mtime": stat.st_mtime}


def _load_sessions(project_path: str | Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return `(board_local_data, optimizer_sessions_block)`. The caller
    mutates the block and saves the whole `data` back, so unknown keys written
    by other tools survive (the load-modify-save discipline `load_board_local`
    documents)."""
    data = _pcb.load_board_local(project_path)["data"]
    data.setdefault("version", 1)
    sessions = data.setdefault("optimizer_sessions", {})
    return data, sessions


def get_route_session(project_path: str | Path, session_id: str | None = None) -> dict[str, Any]:
    """READ-ONLY report of an optimizer session (MCP tool
    `get_kicad_route_session`): its state, iteration count, score curve, move
    history and budgets, WITHOUT advancing it by a single iteration.

    `session_id=None` reports the most recently touched session on this board
    (`last_optimizer_session`). Returns `{"found": false, ...}` rather than
    raising when there is nothing to report, so a caller can poll a board that
    has never been optimized.
    """
    data, sessions = _load_sessions(project_path)
    wanted = session_id or data.get("last_optimizer_session")
    if not wanted or wanted not in sessions:
        return {
            "command": "get_route_session",
            "found": False,
            "session_id": wanted,
            "known_sessions": sorted(sessions.keys()),
        }
    return {
        "command": "get_route_session",
        "found": True,
        **_session_report(sessions[wanted]),
        "known_sessions": sorted(sessions.keys()),
    }


def _session_report(session: dict[str, Any]) -> dict[str, Any]:
    """The public projection of a session record - everything a caller needs to
    decide "resume, write, or abandon", and nothing internal (the RNG state
    blob and the scratch board fingerprint stay out of the report; they are
    machine state, not information)."""
    return {
        "session_id": session["session_id"],
        "state": session["state"],
        "iteration": session["iteration"],
        "max_iterations": session["max_iterations"],
        "elapsed_s": round(session["elapsed_s"], 3),
        "time_budget_s": session["time_budget_s"],
        "seed": session["seed"],
        "accept": session["accept"],
        "temperature": round(session["temperature"], 6),
        "initial_score": session["initial_score"],
        "current_score": session["current_score"],
        "best_score": session["best_score"],
        "score_curve": list(session["score_curve"]),
        "moves": list(session["moves"]),
        "moves_accepted": sum(1 for m in session["moves"] if m["accepted"]),
        "moves_rejected": sum(1 for m in session["moves"] if not m["accepted"]),
        "scratch_dir": session["scratch_dir"],
        "applied": session.get("applied", False),
        "stop_reason": session.get("stop_reason"),
    }


# --------------------------------------------------------------------------- #
# Stitching-via writer - the one piece of board surgery this module owns
# --------------------------------------------------------------------------- #

def _place_stitching_via(project_path: str | Path, net: str, x: float, y: float) -> dict[str, Any]:
    """Append ONE bare `(via ...)` on `net` at `(x, y)`, spanning the full
    copper stack, and record its uuid in board-local `autorouter_owned` so
    `unroute_nets` can undo it exactly like any routed via.

    This is new surgery only because every existing via emitter places vias as
    part of a ROUTE (`route_nets` emits them inside a connection's geometry); a
    stitching via belongs to no connection. It deliberately reuses the same
    `_via_block` serializer, the same `_resolve_route_rules` geometry, and the
    same `_append_top_level_block` insertion `route_nets` uses, so the emitted
    text is indistinguishable from a routed via apart from being standalone.

    NOTE (honest scope): this places the via where `audit_plane_islands`
    already suggests (`suggested_stitching_via.position`, the island's nearest
    point to the mainland). It does NOT run the router's `_self_check`
    clearance proof, because the suggested point is by construction inside the
    island's own same-net pour - the caller (the optimizer's move (d)) only
    ever passes such a point, and the result is reported as needing a KiCad
    refill + DRC pass like every other write in this codebase.
    """
    board_path, _, _ = _pcb._resolve_project_path(project_path)
    settings = _pcb.load_pcb_settings(project_path)["config"]
    rules = _r._resolve_route_rules(project_path, settings)
    all_cu = [layer["name"] for layer in _pcb._parse_board_layers_cached(board_path)
              if layer["type"] in ("signal", "power", "mixed", "jumper")]
    if len(all_cu) < 2:
        raise ValueError("A stitching via needs at least two copper layers")

    via_uuid = str(_uuid.uuid4())
    block = _r._via_block({"x": x, "y": y}, net, rules["via_diameter"], rules["via_drill"],
                          all_cu[0], all_cu[-1], via_uuid)
    text = _pcb._read_text(board_path)
    with board_path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(_pcb._append_top_level_block(text, block))
    _pcb._invalidate_board_cache(board_path)

    data = _pcb.load_board_local(project_path)["data"]
    data.setdefault("version", 1)
    owned = data.setdefault("autorouter_owned", {})
    owned.setdefault("vias", []).append(via_uuid)
    owned.setdefault("records", []).append({"uuid": via_uuid, "net": net, "kind": "via"})
    _pcb.save_board_local(project_path, data)
    return {"uuid": via_uuid, "net": net, "x": x, "y": y}


# --------------------------------------------------------------------------- #
# The six move types
#
# Every applier takes the TRIAL project directory (already a private copy) and
# mutates it in place with `write=True` calls to the existing writers, then
# returns a detail dict (or raises/returns None when the move turns out to be
# inapplicable on this board - a candidate that cannot apply is simply dropped,
# never faked into a zero-delta "success").
# --------------------------------------------------------------------------- #

def _reroute_nets_in_order(project: Path, nets: list[str], max_ripup: int | None) -> dict[str, Any]:
    """Rip up the autorouter-owned copper of `nets`, then re-route them ONE
    CALL AT A TIME in the given order. Routing them individually (rather than
    in one `route_nets(nets=[...])` call) is what makes the ORDER matter: each
    completed route becomes an obstacle for the next, so a perturbed order is a
    genuinely different search, which is exactly the "new order" perturbation
    the plan asks move (a) for.

    `unroute_nets` only removes uuids in `autorouter_owned`, so a human-routed
    net passed here loses nothing and gains nothing - the move degrades to a
    no-op instead of touching hand copper.
    """
    undo = _r.unroute_nets(project, nets=nets, write=True)
    routed = 0
    failed = 0
    for net in nets:
        res = _r.route_nets(project, nets=[net], write=True, max_ripup_iterations=max_ripup)
        summary = res.get("summary", {})
        routed += int(summary.get("connections_routed", 0))
        failed += int(summary.get("connections_failed", 0))
    return {"ripped_uuids": undo["removed"], "routed": routed, "failed": failed}


def _apply_ripup_reroute(project: Path, params: dict[str, Any]) -> dict[str, Any]:
    """Move (a): rip up + reroute one (or two) of the worst nets in a perturbed
    order, at a perturbed rip-up aggressiveness."""
    stats = _reroute_nets_in_order(project, list(params["nets"]), params["max_ripup"])
    return {"nets": list(params["nets"]), "max_ripup": params["max_ripup"], **stats}


def _apply_reroute_bundle(project: Path, params: dict[str, Any]) -> dict[str, Any]:
    """Move (b): rip up a whole Phase-5 bus bundle and reroute its members
    TOGETHER in one `route_nets` call.

    This is corridor-aware for free and without new machinery: `route_nets`
    runs the global stage, which calls `_collect_bundles` and applies the
    `off_corridor` cost to bundle members, so members routed in one call are
    pulled onto the bundle's corridor. Routing them in one call (unlike move
    (a), which routes one net per call to perturb the order) is precisely what
    lets the global stage see them as a bundle.
    """
    nets = list(params["nets"])
    undo = _r.unroute_nets(project, nets=nets, write=True)
    res = _r.route_nets(project, nets=nets, write=True, max_ripup_iterations=params["max_ripup"])
    summary = res.get("summary", {})
    return {
        "bundle_id": params["bundle_id"],
        "nets": nets,
        "ripped_uuids": undo["removed"],
        "routed": int(summary.get("connections_routed", 0)),
        "failed": int(summary.get("connections_failed", 0)),
    }


def _apply_swap_layer(project: Path, params: dict[str, Any]) -> dict[str, Any]:
    """Move (c): reroute a net with its CURRENT dominant layer's purpose
    weight temporarily penalized, so the router's existing layer-purpose logic
    pushes it onto a different layer type.

    There is no per-net home-layer override anywhere in the router (the home
    layer is DERIVED, by `_dominant_layer`, from a preliminary route), so the
    honest way to express "try this net on another layer" in this cost model is
    to reweight the layer purposes the router already consults - which is what
    "layer-purpose driven" means in the plan. The reweight is written into the
    TRIAL copy's own `pcb_settings.json`, applies to exactly one reroute, and
    is reverted before the trial is scored, so the candidate is always scored
    with the project's real weights. A trial directory is discarded after
    scoring either way, so the perturbed file can never escape into the session.
    """
    settings_path = Path(project) / "pcb_settings.json"
    original = settings_path.read_text(encoding="utf-8") if settings_path.exists() else None
    config = _pcb.load_pcb_settings(project)["config"]
    purpose = config.setdefault("layer_purpose", {})
    kind_map = purpose.setdefault(params["net_kind"], {})
    current = kind_map.get(params["layer_type"])
    kind_map[params["layer_type"]] = (float(current) if isinstance(current, (int, float))
                                      and not isinstance(current, bool) else 1.0) * _LAYER_SWAP_PENALTY
    settings_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    try:
        stats = _reroute_nets_in_order(project, [params["net"]], params["max_ripup"])
    finally:
        # Restore BEFORE scoring - the candidate must be judged by the
        # project's real cost model, never by the perturbed one.
        if original is None:
            settings_path.unlink(missing_ok=True)
        else:
            settings_path.write_text(original, encoding="utf-8")
    return {
        "net": params["net"],
        "from_layer": params["from_layer"],
        "penalized_layer_type": params["layer_type"],
        **stats,
    }


def _apply_add_stitching_via(project: Path, params: dict[str, Any]) -> dict[str, Any]:
    """Move (d): drop one stitching via at the position `audit_plane_islands`
    already computed for this island. The island's cost is `island_base / N`,
    so a via that lands inside it takes N to N+1 and directly cheapens the
    board score - no refill needed for the estimate, because the 7.5.2
    attachment model counts same-net vias landing in the component."""
    placed = _place_stitching_via(project, params["net"], params["x"], params["y"])
    return {
        "zone": params["zone"],
        "layer": params["layer"],
        "projected_cost": params["projected_cost"],
        **placed,
    }


def _apply_create_plane(project: Path, params: dict[str, Any]) -> dict[str, Any]:
    """Move (e): pour a plane for a net whose current routed copper costs more
    than the plane would (`propose_plane`'s `cost_delta` < 0). Straight reuse of
    `create_plane`, including its `autorouter_owned.zones` bookkeeping - which
    is also what makes the new zone (and ONLY the new zone) eligible for move
    (f) later."""
    res = _r.create_plane(project, params["net"], layer=params["layer"], write=True)
    return {
        "net": params["net"],
        "layer": res["layer"],
        "zone_uuid": res["uuid"],
        "zone_name": res["name"],
        "projected_cost_delta": params["cost_delta"],
    }


def _apply_modify_plane(project: Path, params: dict[str, Any]) -> dict[str, Any]:
    """Move (f): grow/shrink an AUTOROUTER-OWNED zone outline.

    The perturbation is a uniform scale about the outline's centroid by an
    RNG-drawn factor - a deliberately simple geometric probe, not a cost-model-
    derived reshaping (the cost model scores a FILLED outcome; deriving the
    optimal outline analytically is a different, much larger problem). The
    scoring loop is what decides whether the probe was worth keeping.

    `modify_plane` refuses any zone uuid not in `autorouter_owned.zones`, so
    the six hand-made kiln zones can never reach this path; the optimizer does
    not re-check that itself, it just never bypasses the writer.
    """
    res = _r.modify_plane(project, params["uuid"], new_outline=params["outline"], write=True)
    return {
        "zone_uuid": params["uuid"],
        "scale": params["scale"],
        "point_count": len(params["outline"]),
        "written": res["written"],
    }


_MOVE_APPLIERS = {
    "ripup_reroute": _apply_ripup_reroute,
    "reroute_bundle": _apply_reroute_bundle,
    "swap_layer": _apply_swap_layer,
    "add_stitching_via": _apply_add_stitching_via,
    "create_plane": _apply_create_plane,
    "modify_plane": _apply_modify_plane,
}


# --------------------------------------------------------------------------- #
# Candidate generation
#
# Determinism contract: this function consumes `rng` in a FIXED order, and
# every list it iterates is sorted, so the candidate list for a given board
# state + RNG state is reproducible. That is what makes `seed` alone enough to
# replay a whole run.
# --------------------------------------------------------------------------- #

def _candidate_ripup_reroute(project: Path, worst: list[dict[str, Any]],
                             rng: random.Random) -> dict[str, Any] | None:
    if not worst:
        return None
    primary = worst[rng.randrange(len(worst))]["net"]
    nets = [primary]
    others = [n["net"] for n in worst if n["net"] != primary]
    if others and rng.random() < 0.5:
        nets.append(others[rng.randrange(len(others))])
    rng.shuffle(nets)
    max_ripup = [0, 4, 20][rng.randrange(3)]
    return {"type": "ripup_reroute", "params": {"nets": nets, "max_ripup": max_ripup},
            "summary": f"rip-up + reroute {', '.join(nets)} (max_ripup={max_ripup})"}


def _candidate_reroute_bundle(project: Path, worst: list[dict[str, Any]],
                              rng: random.Random) -> dict[str, Any] | None:
    bundles = _r._collect_bundles(project)   # already sorted by id
    if not bundles:
        return None
    bundle = bundles[rng.randrange(len(bundles))]
    max_ripup = [4, 20][rng.randrange(2)]
    return {"type": "reroute_bundle",
            "params": {"bundle_id": bundle["id"], "nets": list(bundle["member_nets"]),
                       "max_ripup": max_ripup},
            "summary": f"reroute bus bundle {bundle['id']} ({len(bundle['member_nets'])} nets) on its corridor"}


def _candidate_swap_layer(project: Path, worst: list[dict[str, Any]],
                          rng: random.Random) -> dict[str, Any] | None:
    board_path, _, _ = _pcb._resolve_project_path(project)
    layer_types = {layer["name"]: layer["type"]
                   for layer in _pcb._parse_board_layers_cached(board_path)}
    # Only nets with copper on a layer whose PURPOSE we can reason about are
    # swappable; an unknown/user layer type has no layer-purpose weight to
    # penalize, so "swap away from it" would be a meaningless instruction.
    eligible = []
    for entry in worst:
        lengths = entry["metrics"].get("layer_lengths_mm", {})
        if not lengths:
            continue
        dominant = max(sorted(lengths.items()), key=lambda kv: kv[1])[0]
        ltype = layer_types.get(dominant)
        if ltype in ("signal", "power", "mixed"):
            eligible.append((entry, dominant, ltype))
    if not eligible:
        return None
    entry, dominant, ltype = eligible[rng.randrange(len(eligible))]
    return {"type": "swap_layer",
            "params": {"net": entry["net"], "net_kind": entry["net_kind"],
                       "from_layer": dominant, "layer_type": ltype, "max_ripup": 4},
            "summary": f"swap {entry['net']} off {dominant} (layer type {ltype})"}


def _candidate_add_stitching_via(project: Path, worst: list[dict[str, Any]],
                                 rng: random.Random) -> dict[str, Any] | None:
    try:
        audit = _r.audit_plane_islands(project)
    except Exception:  # pragma: no cover - defensive
        return None
    options: list[dict[str, Any]] = []
    for zone in audit["zones"]:
        for layer in zone["layers"]:
            for comp in layer["components"]:
                suggestion = comp.get("suggested_stitching_via")
                if comp["role"] != "island" or not suggestion:
                    continue
                options.append({
                    "zone": zone["name"], "net": zone["net"], "layer": layer["layer"],
                    "x": suggestion["position"]["x"], "y": suggestion["position"]["y"],
                    "projected_cost": suggestion["projected_cost"],
                    "current_cost": comp["cost"],
                })
    if not options:
        return None
    # Worst-first: the island whose cost the via would cut hardest.
    options.sort(key=lambda o: (-(o["current_cost"] - o["projected_cost"]), o["zone"], o["layer"], o["x"], o["y"]))
    pick = options[rng.randrange(min(len(options), 3))]
    return {"type": "add_stitching_via", "params": pick,
            "summary": f"stitch island in {pick['zone']} ({pick['layer']}) at ({pick['x']}, {pick['y']})"}


def _candidate_create_plane(project: Path, worst: list[dict[str, Any]],
                            rng: random.Random) -> dict[str, Any] | None:
    """Only power nets, and only when `propose_plane` prices the plane BELOW
    the net's current routed copper (`cost_delta < 0`) - the plan's own gate
    ("a power net whose trace cost exceeds create_plane cost + projected plane
    cost"), which `propose_plane` already computes for us."""
    candidates = [n for n in worst if n["net_kind"] == "power"]
    if not candidates:
        return None
    for entry in candidates:   # worst-first; take the first that prices well
        try:
            proposal = _r.propose_plane(project, entry["net"])
        except Exception:
            continue
        if float(proposal.get("cost_delta", 0.0)) >= 0.0:
            continue
        return {"type": "create_plane",
                "params": {"net": entry["net"], "layer": proposal["layer"],
                           "cost_delta": proposal["cost_delta"]},
                "summary": f"create plane for {entry['net']} on {proposal['layer']} "
                           f"(projected delta {proposal['cost_delta']})"}
    return None


def _candidate_modify_plane(project: Path, worst: list[dict[str, Any]],
                            rng: random.Random) -> dict[str, Any] | None:
    data = _pcb.load_board_local(project)["data"]
    owned_zones = sorted((data.get("autorouter_owned", {}) or {}).get("zones", []) or [])
    if not owned_zones:
        return None
    board_path, _, _ = _pcb._resolve_project_path(project)
    zones = {z["uuid"]: z for z in _r._parse_zones_cached(board_path)}
    eligible = [uid for uid in owned_zones if uid in zones and len(zones[uid].get("polygon", []) or []) >= 3]
    if not eligible:
        return None
    uid = eligible[rng.randrange(len(eligible))]
    pts = [(float(p[0]), float(p[1])) for p in zones[uid]["polygon"]]
    scale = [0.9, 0.95, 1.05, 1.1][rng.randrange(4)]
    cx = sum(p[0] for p in pts) / len(pts)
    cy = sum(p[1] for p in pts) / len(pts)
    outline = [{"x": round(cx + (p[0] - cx) * scale, 4), "y": round(cy + (p[1] - cy) * scale, 4)}
               for p in pts]
    return {"type": "modify_plane",
            "params": {"uuid": uid, "outline": outline, "scale": scale},
            "summary": f"rescale autorouter zone {uid[:8]} by {scale}"}


# Fixed order = fixed RNG consumption order = reproducible runs.
_CANDIDATE_GENERATORS = (
    ("ripup_reroute", _candidate_ripup_reroute),
    ("reroute_bundle", _candidate_reroute_bundle),
    ("swap_layer", _candidate_swap_layer),
    ("add_stitching_via", _candidate_add_stitching_via),
    ("create_plane", _candidate_create_plane),
    ("modify_plane", _candidate_modify_plane),
)


def _generate_candidates(project: Path, worst: list[dict[str, Any]],
                         rng: random.Random) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for _name, generator in _CANDIDATE_GENERATORS:
        if len(candidates) >= _MAX_CANDIDATES_PER_ITERATION:
            break
        try:
            candidate = generator(project, worst, rng)
        except Exception as exc:   # a generator that cannot inspect this board
            candidate = None       # simply offers nothing - never a fake move.
            del exc
        if candidate:
            candidates.append(candidate)
    return candidates


# --------------------------------------------------------------------------- #
# The loop
# --------------------------------------------------------------------------- #

def _scratch_snapshot(src: Path, dst: Path) -> Path:
    """Copy a whole project directory (board + .kicad_pro + .net +
    pcb_settings.json + board_local.json) - the unit of "board state" for the
    optimizer. Small by construction: `_copy_project_to_scratch` only ever put
    those files there.

    The parse caches in both tool modules are keyed by board PATH (validated on
    mtime+size). Writing new content under a path that has been parsed before -
    which is exactly what promoting a trial over the scratch does - is the one
    situation where that validation could be fooled, so the caches for the
    destination board are dropped explicitly rather than left to a timestamp
    comparison. A stale parse here would corrupt a score, and a corrupted score
    is a silently wrong board.
    """
    if dst.exists():
        shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(src, dst)
    try:
        board_path, _, _ = _pcb._resolve_project_path(dst)
    except Exception:  # pragma: no cover - a dir with no board can't be stale
        return dst
    _pcb._invalidate_board_cache(board_path)
    for cache in (_r._zone_cache, _r._zone_fill_index_cache, _r._drc_constraints_cache):
        cache.pop(str(board_path), None)
    return dst


def _evaluate_candidate(scratch: Path, trial_root: Path, candidate: dict[str, Any],
                        iteration: int, index: int) -> dict[str, Any]:
    """Apply one candidate to a private snapshot of the current scratch state
    and score it. A candidate that raises is REPORTED as failed with its error
    rather than crashing the run - the loop simply doesn't pick it. (Routing is
    allowed to fail; that is information, not a bug.)"""
    # Iteration number in the path so no two trials ever share a directory -
    # a reused path is the only way a board-keyed parse cache could go stale.
    trial = _scratch_snapshot(scratch, trial_root / f"i{iteration}_cand{index}")
    record: dict[str, Any] = {"type": candidate["type"], "summary": candidate["summary"],
                              "trial_dir": str(trial)}
    try:
        record["detail"] = _MOVE_APPLIERS[candidate["type"]](trial, candidate["params"])
        record["score"] = score_board(trial)
        record["applicable"] = True
    except Exception as exc:
        record["applicable"] = False
        record["error"] = f"{type(exc).__name__}: {exc}"
    return record


def _accept(policy: str, delta: float, temperature: float, rng: random.Random) -> tuple[bool, str]:
    """`greedy`: only strict improvements. `sa`: improvements always, worse
    moves with probability exp(-delta/T). Returns (accepted, reason) so the
    move log records WHY - an SA run's history is unreadable otherwise."""
    if delta < 0:
        return True, "improvement"
    if policy != "sa":
        return False, "greedy_rejects_non_improvement"
    if temperature <= 0:
        return False, "sa_temperature_exhausted"
    probability = math.exp(-delta / temperature)
    if rng.random() < probability:
        return True, f"sa_accepted_worse (p={probability:.4f})"
    return False, f"sa_rejected_worse (p={probability:.4f})"


def _new_session(project_path: str | Path, config: dict[str, Any],
                 seed: int | None, accept: str | None,
                 max_iterations: int | None, time_budget_s: float | None) -> dict[str, Any]:
    optimizer = config.get("optimizer", {})
    resolved_seed = int(optimizer.get("seed", 1)) if seed is None else int(seed)
    resolved_accept = str(accept or optimizer.get("accept", "greedy")).lower()
    if resolved_accept not in ("greedy", "sa"):
        raise ValueError(f"accept must be 'greedy' or 'sa'; got {resolved_accept!r}")

    scratch = Path(tempfile.mkdtemp(prefix="kicad_optimize_"))
    _r._copy_project_to_scratch(project_path, scratch)
    initial = score_board(scratch)
    rng = random.Random(resolved_seed)
    return {
        "session_id": str(_uuid.uuid4()),
        "state": "running",
        "created": time.time(),
        "project_path": str(project_path),
        "board_fingerprint": _board_fingerprint(project_path),
        "scratch_dir": str(scratch),
        "seed": resolved_seed,
        "accept": resolved_accept,
        "rng_state": _rng_state_to_json(rng),
        "iteration": 0,
        "elapsed_s": 0.0,
        "max_iterations": int(optimizer.get("max_iterations", 20)) if max_iterations is None else int(max_iterations),
        "time_budget_s": float(optimizer.get("time_budget_s", 300)) if time_budget_s is None else float(time_budget_s),
        "worst_k": int(optimizer.get("worst_k", 5)),
        "convergence_delta": float(optimizer.get("convergence_delta", 0.5)),
        "temperature": float(optimizer.get("sa_initial_temp", 50.0)),
        "sa_cooling": float(optimizer.get("sa_cooling", 0.9)),
        "initial_score": initial,
        "current_score": initial,
        "best_score": initial,
        "score_curve": [initial["total"]],
        "moves": [],
        "applied": False,
        "stop_reason": None,
    }


def optimize_board(
    project_path: str | Path,
    session_id: str | None = None,
    max_iterations_per_call: int = 3,
    max_seconds: float | None = None,
    seed: int | None = None,
    accept: str | None = None,
    max_iterations: int | None = None,
    time_budget_s: float | None = None,
    write: bool = False,
    allow_while_open: bool = False,
) -> dict[str, Any]:
    """Phase 7.6 - run a BOUNDED chunk of whole-board optimization.

    Omit `session_id` to start a new session (which snapshots the project into
    a private scratch directory and scores it); pass one to resume an existing
    session exactly where it stopped. Each call runs at most
    `max_iterations_per_call` iterations or `max_seconds` of wall clock,
    whichever binds first, and returns the session's state:

      `running`           - budget for THIS call is spent, more work remains.
      `converged`         - the best available move improved the score by less
                            than `optimizer.convergence_delta` (or no move
                            improved it at all).
      `budget_exhausted`  - the SESSION's `max_iterations` / `time_budget_s`
                            ran out first.

    Each iteration ranks every cost contributor worst-first (routed nets at
    their trace cost, unrouted nets at `unrouted_penalty` x their missing
    connections), takes `optimizer.worst_k` of them, generates up to six
    candidate moves (rip-up+reroute, bundle
    reroute, layer swap, stitching via, create plane, modify plane), scores
    every candidate on its OWN private copy of the current board state, and
    accepts per `optimizer.accept` (`greedy` = strict improvements only; `sa` =
    simulated annealing, worse moves accepted with probability exp(-dS/T),
    T *= `sa_cooling` each iteration). `seed` makes the whole run reproducible.

    `write=False` (the default) NEVER touches the real board - not on the first
    call, not on the last. `write=True` applies the session's final accepted
    board state (copper, vias and zones together, as one consistent state - not
    a replay of individual moves) onto the real board and merges the scratch's
    `autorouter_owned` bookkeeping into the real board-local state, so
    `unroute_nets` still undoes every piece of it. It refuses if the session is
    still `running` (there is no "final state" yet) or if the real board file
    changed since the session started. As with every writer here, KiCad must
    refill zones and re-run DRC afterward.

    Phase 7.7 (AI-in-the-loop decisions) is not implemented: there is no
    `awaiting_decision` state and `optimizer.ai_decisions` is never read. Where
    7.7 would pause on a near-tie, this optimizer takes the best-scored move.
    """
    config = _pcb.load_pcb_settings(project_path)["config"]
    data, sessions = _load_sessions(project_path)

    if session_id is None:
        session = _new_session(project_path, config, seed, accept, max_iterations, time_budget_s)
        sessions[session["session_id"]] = session
    else:
        if session_id not in sessions:
            raise KeyError(f"No optimizer session {session_id!r} on this board")
        session = sessions[session_id]

    notes: list[str] = []
    if session["state"] == "running" and not session["applied"]:
        _run_chunk(session, max_iterations_per_call, max_seconds, notes)

    written = False
    write_skipped_reason = None
    if write:
        written, write_skipped_reason = _apply_session(session, project_path, allow_while_open, data)

    # ONE save, after everything (including the apply) has mutated `data` -
    # `data` was loaded before the write, so saving it from two places would
    # let the second save clobber the first's `autorouter_owned`.
    data["last_optimizer_session"] = session["session_id"]
    _pcb.save_board_local(project_path, data)

    report = {
        "command": "optimize_board",
        "board_path": _board_fingerprint(project_path)["path"],
        "write": write,
        "written": written,
        **_session_report(session),
        "score_delta": round(session["current_score"]["total"] - session["initial_score"]["total"], 4),
        "diff": _session_diff(session),
        "notes": notes + [
            "Phase 7.7 (AI-in-the-loop decisions) is not implemented: this session "
            "state machine has three states and never reads optimizer.ai_decisions.",
        ],
    }
    if write_skipped_reason:
        report["write_skipped_reason"] = write_skipped_reason
    if written:
        report["refill_required_note"] = (
            "Copper/zones were applied but zones are NOT filled - run Fill All Zones "
            "and re-run DRC in KiCad before fabricating."
        )
    return report


def _run_chunk(session: dict[str, Any], max_iterations_per_call: int,
               max_seconds: float | None, notes: list[str]) -> None:
    """One bounded chunk of the loop, mutating `session` in place. Everything
    it changes (RNG state, iteration, temperature, elapsed, curve, moves,
    scratch contents) is exactly the state a resume needs, which is why a
    chunked run and an unbroken run make identical decisions."""
    scratch = Path(session["scratch_dir"])
    if not scratch.exists():
        raise FileNotFoundError(
            f"Session {session['session_id']}'s scratch board {scratch} is gone "
            "(temp dir reaped?) - start a new session")
    rng = _rng_from_json(session["rng_state"])
    trial_root = scratch.parent / f"{scratch.name}_trials"
    trial_root.mkdir(exist_ok=True)

    started = time.monotonic()
    done_this_call = 0
    while True:
        if done_this_call >= max_iterations_per_call:
            session["stop_reason"] = "call_budget"
            break
        if max_seconds is not None and time.monotonic() - started >= max_seconds:
            session["stop_reason"] = "call_time_budget"
            break
        if session["iteration"] >= session["max_iterations"]:
            session["state"] = "budget_exhausted"
            session["stop_reason"] = "max_iterations"
            break
        if session["elapsed_s"] >= session["time_budget_s"]:
            session["state"] = "budget_exhausted"
            session["stop_reason"] = "time_budget_s"
            break

        iteration_started = time.monotonic()
        worst = _ranked_nets(scratch)[: session["worst_k"]]
        candidates = _generate_candidates(scratch, worst, rng)
        evaluated = [_evaluate_candidate(scratch, trial_root, c, session["iteration"] + 1, i)
                     for i, c in enumerate(candidates)]
        usable = [e for e in evaluated if e["applicable"]]
        # Best-scored candidate wins outright. 7.7 would pause here when the
        # top two are within `min_score_spread`; out of scope for this landing.
        usable.sort(key=lambda e: (e["score"]["total"], e["type"], e["summary"]))

        session["iteration"] += 1
        done_this_call += 1
        current_total = session["current_score"]["total"]

        if not usable:
            session["moves"].append({
                "iteration": session["iteration"], "type": None, "accepted": False,
                "reason": "no_applicable_candidate", "score_before": current_total,
                "score_after": current_total, "delta": 0.0,
            })
            session["score_curve"].append(current_total)
            session["state"] = "converged"
            session["stop_reason"] = "no_applicable_candidate"
            _finish_iteration(session, rng, iteration_started)
            break

        best = usable[0]
        delta = round(best["score"]["total"] - current_total, 6)
        accepted, reason = _accept(session["accept"], delta, session["temperature"], rng)

        if accepted:
            # Promote the winning trial to BE the scratch state. Copying the
            # trial over the scratch (rather than replaying the move on the
            # scratch) is what guarantees the accepted state is exactly the
            # state that was scored.
            _scratch_snapshot(Path(best["trial_dir"]), scratch)
            session["current_score"] = best["score"]
            if best["score"]["total"] < session["best_score"]["total"]:
                session["best_score"] = best["score"]

        session["moves"].append({
            "iteration": session["iteration"],
            "type": best["type"],
            "summary": best["summary"],
            "detail": best.get("detail"),
            "accepted": accepted,
            "reason": reason,
            "score_before": current_total,
            "score_after": session["current_score"]["total"],
            "delta": round(session["current_score"]["total"] - current_total, 6),
            "candidates_evaluated": len(evaluated),
            "candidates_applicable": len(usable),
        })
        session["score_curve"].append(session["current_score"]["total"])
        shutil.rmtree(trial_root, ignore_errors=True)
        trial_root.mkdir(exist_ok=True)

        # Converged when the best available move cannot buy `convergence_delta`
        # worth of score. Under `greedy` a rejected move means nothing improved,
        # which is the same condition - so both policies stop here honestly.
        improvement = current_total - session["current_score"]["total"]
        if improvement < session["convergence_delta"]:
            session["state"] = "converged"
            session["stop_reason"] = "convergence_delta"
            _finish_iteration(session, rng, iteration_started)
            break
        _finish_iteration(session, rng, iteration_started)

    shutil.rmtree(trial_root, ignore_errors=True)
    session["rng_state"] = _rng_state_to_json(rng)
    if session["state"] == "running" and session["iteration"] >= session["max_iterations"]:
        session["state"] = "budget_exhausted"
        session["stop_reason"] = "max_iterations"
    notes.append(f"Ran {done_this_call} iteration(s) this call; state={session['state']}.")


def _finish_iteration(session: dict[str, Any], rng: random.Random, iteration_started: float) -> None:
    """Per-iteration bookkeeping that must happen on EVERY exit path (including
    the two that break out of the loop), or a resumed session would replay with
    a stale temperature/clock."""
    session["elapsed_s"] += time.monotonic() - iteration_started
    if session["accept"] == "sa":
        session["temperature"] *= session["sa_cooling"]
    session["rng_state"] = _rng_state_to_json(rng)


def _session_diff(session: dict[str, Any]) -> dict[str, Any]:
    """The dry-run diff: what `write=True` WOULD change on the real board,
    measured as the difference between the scratch state and the board the
    session started from - copper length, via/zone counts, and the score terms.
    Cheap because both sides are already-parsed boards."""
    scratch = Path(session["scratch_dir"])
    if not scratch.exists():
        return {"available": False, "reason": "scratch board is gone"}
    board_path, _, _ = _pcb._resolve_project_path(scratch)
    tracks = _pcb._parse_tracks_cached(board_path)
    owned = (_pcb.load_board_local(scratch)["data"].get("autorouter_owned", {}) or {})
    return {
        "available": True,
        "scratch_board": str(board_path),
        "segments_on_board": len(tracks["segments"]) + len(tracks["arcs"]),
        "vias_on_board": len(tracks["vias"]),
        "autorouter_owned_segments": len(owned.get("segments", []) or []),
        "autorouter_owned_vias": len(owned.get("vias", []) or []),
        "autorouter_owned_zones": len(owned.get("zones", []) or []),
        "score_from": session["initial_score"],
        "score_to": session["current_score"],
    }


def _apply_session(session: dict[str, Any], project_path: str | Path,
                   allow_while_open: bool, data: dict[str, Any]) -> tuple[bool, str | None]:
    """Copy the session's final accepted board state onto the REAL board.

    Deliberately a whole-file copy of the scratch board rather than a replay of
    the move list: the scratch board IS the state that was scored, so copying
    it is the only way the written board is provably the board the report
    describes. The scratch's `autorouter_owned` (which grew as moves routed and
    poured) replaces the real one, keeping every applied piece undoable through
    the ordinary `unroute_nets` path.

    `data` is the caller's live board-local dict: the ownership merge is
    written INTO it rather than saved here, so `optimize_board` performs ONE
    save of everything (session checkpoint + ownership) at the end. Saving from
    both places would let the caller's older snapshot clobber this one.
    """
    if session["state"] == "running":
        return False, "session is still running - resume until converged/budget_exhausted before writing"
    if session.get("applied"):
        return False, "session was already applied"
    scratch = Path(session["scratch_dir"])
    if not scratch.exists():
        return False, "scratch board is gone - nothing to apply"

    current = _board_fingerprint(project_path)
    recorded = session["board_fingerprint"]
    if current["size"] != recorded["size"] or current["mtime"] != recorded["mtime"]:
        return False, ("the real board changed since this session started "
                       "(size/mtime differ) - re-run the optimizer against the current board")

    real_board, _, _ = _pcb._resolve_project_path(project_path)
    _pcb._check_not_locked_by_editor(real_board, allow_while_open)
    scratch_board, _, _ = _pcb._resolve_project_path(scratch)
    shutil.copy2(scratch_board, real_board)
    _pcb._invalidate_board_cache(real_board)

    scratch_owned = (_pcb.load_board_local(scratch)["data"].get("autorouter_owned", {}) or {})
    data.setdefault("version", 1)
    data["autorouter_owned"] = scratch_owned

    session["applied"] = True
    session["board_fingerprint"] = _board_fingerprint(project_path)
    return True, None
