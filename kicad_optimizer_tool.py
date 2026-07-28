"""Phase 7.6/7.7 - iterative whole-board optimization (`optimize_kicad_board`).

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

PHASE 7.7 - AI IN THE LOOP, BETWEEN DESIGNATED OPTIONS ONLY
-----------------------------------------------------------
The optimizer escalates a choice to the AI ONLY where its own arithmetic cannot
separate the options: when the score spread between the best and runner-up
candidate is under `optimizer.ai_decisions.min_score_spread`. That is the whole
gate, and it is deliberately narrow - a clear winner is still auto-taken, so a
run's behaviour is UNCHANGED from the 7.6 core everywhere except on a genuine
near-tie. `ai_decisions.enabled: false` restores the 7.6 behaviour exactly.

On a near-tie the chunk stops mid-iteration with `state: "awaiting_decision"`
and a `pending_decision` holding a CLOSED list of 2-4 already-applied,
already-scored candidates. The AI answers with an option id (or `"defer"`) via
`decide_route` / MCP `decide_kicad_route`; free-form input is confined to a
`rationale` string that is recorded and never executed. Nothing is applied while
the session is paused, and the AI can never introduce a move the optimizer did
not itself generate and price.

Because a paused option must survive an MCP restart like everything else, the
options' trial directories are copied out of the throwaway trial root into a
stable `_pending` directory recorded in the checkpoint; resolving the decision
promotes the chosen one over the scratch, exactly as auto-acceptance does.

An abandoned pause cannot wedge a session: calling `optimize_board` again on an
`awaiting_decision` session resolves the pause as `defer` (the optimizer's own
best-scored option) and carries on, so an undecided session still converges.

Auditability is the point of the whole mechanism, so EVERY committed move -
auto-accepted or AI-decided - appends one entry to `decision_log` carrying the
options, their scores, the choice, the rationale and the `auto` flag. That log
plus `seed` is enough to replay a run (same seed + same answers -> same board);
a dedicated replay executor is not built here (see the report's TODO).

PHASE 7.14 - THE PIN-SWAP ADVISOR, THE ONE MOVE THIS TOOL CANNOT MAKE
---------------------------------------------------------------------
A seventh move type joins the six above with one categorical difference: it is
never applied. A pin swap changes which NET OWNS WHICH PAD, and that lives in
the schematic - which this tool never edits, by design and by plan. So the
advisor prices a swap on disposable copies, and when one is worth
`pin_swap.min_gain` board-score points it PAUSES and asks the human to make the
change, rather than making it. That pause is mandatory rather than
spread-gated: a clear winner is exactly the case that must be escalated, since
"clear winner" and "cannot be applied by this tool" are both true at once. Off
by default (`pin_swap.enabled: false`), and provably inert when off - the gate
returns before consuming a single unit of RNG. See the Phase 7.14 section
further down for the full design argument.
"""

from __future__ import annotations

import json
import math
import random
import re
import shutil
import tempfile
import time
import uuid as _uuid
from pathlib import Path
from typing import Any

import kicad_pcb_tool as _pcb
import kicad_router_tool as _r

SESSION_STATES = ("running", "converged", "budget_exhausted", "awaiting_decision")

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

# How many options a pending decision may carry. The plan fixes this at "a
# closed list of 2-4 candidates": fewer than two is not a decision, and more
# than four stops being a judgement call and starts being a search the AI is
# worse at than the cost model.
_MIN_DECISION_OPTIONS = 2
_MAX_DECISION_OPTIONS = 4

# Which of the plan's six `decision_types` each move type escalates as. The
# move types are the optimizer's internal vocabulary; `decision_types` in
# `pcb_settings.json` is the USER-facing allowlist, so the two have to be
# mapped rather than conflated - a user who wants "never ask me about planes"
# removes `plane_proposal` and both plane moves stop pausing.
#
# `swap_layer` maps to `bundle_layer` because it is the same class of question
# the plan describes there ("which layer/corridor this copper takes"), just for
# a single net rather than a whole bundle; there is no separate net-layer type
# in the plan's list and inventing one would put a name in the allowlist that
# the settings schema does not document.
_MOVE_DECISION_TYPES = {
    "ripup_reroute": "conflict_yield",
    "reroute_bundle": "bundle_layer",
    "swap_layer": "bundle_layer",
    "add_stitching_via": "stitching_budget",
    "create_plane": "plane_proposal",
    "modify_plane": "plane_proposal",
}

# The remaining two decision types are properties of the SITUATION, not of the
# move type, so they are detected from the winning candidate's own result and
# override the static map above.
#
# `give_up_net`: the leading candidate still leaves connections unrouted, so the
# real question is "hand-route this or buy an expensive route", which is exactly
# the plan's give_up_net.
# `sa_large_move`: an annealing move that rips this many nets or more is the
# "rip a whole bundle" case the plan wants confirmed before proceeding.
_SA_LARGE_MOVE_NETS = 2


def _ai_decision_config(config: dict[str, Any]) -> dict[str, Any]:
    """Resolve the `optimizer.ai_decisions` block, falling back to the 6.1
    schema defaults. Snapshotted into the session at creation so a run's pause
    policy cannot change under it mid-session - the same treatment `worst_k` and
    `convergence_delta` already get."""
    defaults = _pcb.DEFAULT_PCB_SETTINGS["optimizer"]["ai_decisions"]
    block = (config.get("optimizer", {}) or {}).get("ai_decisions", {}) or {}
    return {
        "enabled": bool(block.get("enabled", defaults["enabled"])),
        "min_score_spread": float(block.get("min_score_spread", defaults["min_score_spread"])),
        "max_pauses_per_run": int(block.get("max_pauses_per_run", defaults["max_pauses_per_run"])),
        "decision_types": list(block.get("decision_types", defaults["decision_types"])),
    }


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
        "effort": session.get("effort", "balanced"),
        "accept": session["accept"],
        "temperature": round(session["temperature"], 6),
        # Phase 7.15: the plateau rule's own view of "why did it stop" (or
        # "how close is it to stopping") - both rates are None until the
        # session has `plateau_window` productive moves to reference.
        "plateau_window": session.get("plateau_window"),
        "plateau_slope_ratio": session.get("plateau_slope_ratio"),
        "plateau_reference_rate": session.get("plateau_reference_rate"),
        "plateau_trailing_rate": session.get("plateau_trailing_rate"),
        "productive_improvements": list(session.get("productive_improvements", [])),
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
        # 7.7. `pending_decision` is None except in the `awaiting_decision`
        # state; it is reported unconditionally so a caller can test the field
        # rather than having to know which states carry it.
        "pending_decision": session.get("pending_decision"),
        "decision_log": list(session.get("decision_log", [])),
        "pauses_used": session.get("pauses_used", 0),
        "ai_decisions": dict(session.get("ai_decisions", {})),
        # 7.14. `pin_swap_reports` carries EVERY pair the advisor priced,
        # including the sub-`min_gain` ones that were never proposed - "reported,
        # not proposed" is only true if the report is somewhere a caller can
        # read it, and this is that place.
        "pin_swap": dict(session.get("pin_swap", {})),
        "pin_swap_exclusions": list(session.get("pin_swap_exclusions", [])),
        "pin_swap_reports": list(session.get("pin_swap_reports", [])),
    }


def _migrate_session(session: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Bring a checkpoint written by an older core up to the current shape.

    Sessions outlive the process AND the code version - a board-local JSON
    written before this landing is a perfectly legitimate thing to resume, and
    it must not crash on a missing key. Resolving `ai_decisions`/`effort`/the
    plateau knobs from the CURRENT settings here is the only honest option:
    an older session never recorded a policy to preserve.
    """
    session.setdefault("pending_decision", None)
    session.setdefault("decision_log", [])
    session.setdefault("pauses_used", 0)
    if not session.get("ai_decisions"):
        session["ai_decisions"] = _ai_decision_config(config)
    # Phase 7.15: a pre-7.15 session has no plateau bookkeeping and no
    # productive-move history to backfill (the moves it already made were
    # never scored against a reference rate), so it resumes with an EMPTY
    # `productive_improvements` - the plateau rule simply needs
    # `plateau_window` more productive iterations from here before it can
    # fire, same as a brand new session. That is a correct, not a degraded,
    # resumption: the rule's contract is "don't fire without enough data."
    optimizer = config.get("optimizer", {})
    session.setdefault("effort", str(optimizer.get("effort", "balanced")))
    session.setdefault("plateau_window", int(optimizer.get("plateau_window", 3)))
    session.setdefault("plateau_slope_ratio", float(optimizer.get("plateau_slope_ratio", 0.1)))
    session.setdefault("productive_improvements", [])
    session.setdefault("plateau_reference_rate", None)
    session.setdefault("plateau_trailing_rate", None)
    # Phase 7.14: a pre-7.14 session never had a pin-swap policy, and resolving
    # one from the CURRENT settings is the only honest option (same reasoning as
    # `ai_decisions` above). A resumed session whose settings still say
    # `enabled: false` - the default - therefore behaves exactly as it did.
    if not session.get("pin_swap"):
        session["pin_swap"] = _pin_swap_config(config)
    session.setdefault("pin_swap_exclusions", [])
    session.setdefault("pin_swap_examined", [])
    session.setdefault("pin_swap_reports", [])
    return session


def _plateau_check(session: dict[str, Any]) -> str | None:
    """Phase 7.15 - the plateau-based stopping rule, alongside (not instead
    of) the existing `convergence_delta` floor.

    Reference rate = mean of the first `plateau_window` PRODUCTIVE
    improvements (accepted moves that actually lowered the score - see
    `_commit_choice`); trailing rate = mean of the most recent
    `plateau_window` productive improvements. Fires when the trailing rate has
    fallen below `plateau_slope_ratio` x the reference rate - i.e. the pace of
    genuine improvement has slowed to a fraction of its initial pace.

    Both rates are written back onto the session on EVERY call (even when the
    rule does not fire, and even before there is enough data to compute them)
    so `get_route_session`'s report always shows the current pace, not just
    the pace at the moment the run stopped - "why did it stop" has to be
    inspectable mid-run too, per the plan.

    Returns a `stop_reason` string when the rule fires, else None. A session
    that has not yet made `plateau_window` productive moves cannot have a
    reference rate at all, so it returns None unconditionally - there is
    nothing to compare the trailing rate against yet, and the existing
    `convergence_delta` floor is the only thing that can stop such a session
    early.
    """
    window = session["plateau_window"]
    improvements = session["productive_improvements"]
    if len(improvements) < window:
        session["plateau_reference_rate"] = None
        session["plateau_trailing_rate"] = None
        return None

    reference = sum(improvements[:window]) / window
    trailing = sum(improvements[-window:]) / window
    session["plateau_reference_rate"] = round(reference, 6)
    session["plateau_trailing_rate"] = round(trailing, 6)

    # A non-positive reference rate is degenerate (the first window's moves
    # were, on average, non-improving despite being individually accepted -
    # only possible under `sa`, which can accept a worse move). There is no
    # meaningful "pace" to have slowed from, so leave stopping to the
    # `convergence_delta` floor rather than divide-by-zero or fire on noise.
    if reference <= 0:
        return None
    if trailing < session["plateau_slope_ratio"] * reference:
        return "plateau"
    return None


# --------------------------------------------------------------------------- #
# Stitching-via writer - the one piece of board surgery this module owns
# --------------------------------------------------------------------------- #

def _place_stitching_via(
    project_path: str | Path, net: str, x: float, y: float, stitching: bool = False,
) -> dict[str, Any]:
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

    `stitching` (default False - unchanged behavior for every pre-existing
    caller, i.e. the optimizer's own move (d)) tags the board-local record
    `"stitching": True` when set. This is Phase 7.5.6's distinguishing mark:
    `run_stitching_pass` is the only caller that ever passes `stitching=True`,
    so `remove_stitching_vias` (MCP `remove_kicad_stitching_vias`) can target
    exactly the vias IT placed without ever touching an ordinary routing via
    or the optimizer's own island-cost move. The key is omitted entirely (not
    even written as `False`) when unset, so every existing reader of
    `autorouter_owned["records"]` - which only ever reads `uuid`/`net` - sees
    byte-identical records to before this change.
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
    record: dict[str, Any] = {"uuid": via_uuid, "net": net, "kind": "via"}
    if stitching:
        record["stitching"] = True
        record["x"] = x
        record["y"] = y
    owned.setdefault("records", []).append(record)
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


# =========================================================================== #
# Phase 7.14 - the pin-swap ADVISOR: the seventh move, and the only one this
# module can never apply
#
# WHY IT IS NOT IN `_MOVE_APPLIERS`
# ---------------------------------
# The six moves above are all COPPER moves: they change where copper runs, and
# `_commit_choice` promotes a winning trial directory over the scratch because
# the scratch board is, by construction, a thing this tool is allowed to write.
# A pin swap is not a copper change at all - it changes WHICH NET OWNS WHICH
# PHYSICAL PAD, and the source of truth for that is the schematic (plus the
# `.net` export KiCad derives from it). The plan is categorical that this tool
# NEVER edits the schematic and never edits the real netlist, so a pin swap is
# something only a human can realize. Wiring it into `_MOVE_APPLIERS` would
# make it promotable by `_commit_choice`, and a promoted swap would ride the
# scratch board straight into the real board on `write=True` - silently
# reassigning a real pad's net, the one outcome this whole feature must make
# impossible. So the pin swap lives entirely OUTSIDE the candidate/commit
# machinery: it is generated, priced, and ESCALATED, never committed.
#
# That is also why its pause is mandatory rather than spread-gated (see
# `_pin_swap_gate`): every other decision type is "which of these applied moves
# do I keep", and a clear winner needs no human. A pin swap has no applied move
# to keep - the ONLY way it can ever happen is a human editing the schematic -
# so a clear winner is precisely the case that MUST be escalated. It follows
# that the `ai_decisions` policy does not gate it either: `min_score_spread`,
# `max_pauses_per_run` and the `decision_types` allowlist all describe when to
# ask the AI to arbitrate between the optimizer's own options, and this is not
# that question. (`"pin_swap"` is deliberately absent from
# `DEFAULT_PCB_SETTINGS["optimizer"]["ai_decisions"]["decision_types"]` for the
# same reason - it is not an AI decision type. `pin_swap.enabled`, off by
# default, is its consent gate, exactly as the plan specifies.)
#
# HOW A HYPOTHETICAL SWAP IS PRICED WITHOUT TOUCHING THE NETLIST
# --------------------------------------------------------------
# On a DISPOSABLE trial copy (the `_scratch_snapshot` pattern every other
# candidate already uses), the swap is made REAL rather than simulated: the two
# pads' own `(net ...)` s-expressions in the trial's `.kicad_pcb` are swapped
# verbatim, and the matching two `(node ...)` blocks in the trial's `.net` copy
# are swapped with them. The trial is then a perfectly coherent board - pad
# nets, netlist and copper all agree - so `route_nets`, its `_self_check`
# clearance proof, `get_ratsnest` and `get_trace_cost` all run on genuine data
# with their normal trust model intact. Nothing here fakes pad identity, which
# is what would have broken self-check's clearance model had the swap been
# expressed as synthetic `route_nets(connections=...)` endpoints instead: a net
# routed to a pad it does not own is, to self-check, copper shorting a foreign
# pad, and the trial would fail for a reason that has nothing to do with the
# swap's merit.
#
# The real project is never opened for writing on any path here, and the trial
# is thrown away whatever the answer - the swap's ONLY output is a number and a
# question.
#
# WHY THE MEASUREMENT IS AN A/B, NOT "CURRENT SCORE VS SWAPPED SCORE"
# -------------------------------------------------------------------
# Scoring the swap trial against the session's current score would measure two
# things at once: the swap, and the fact that the two nets got rerouted by the
# autorouter (on a hand-routed board, usually a large loss that has nothing to
# do with pin assignment). So each pair is priced as a controlled A/B on two
# sibling trials that differ in exactly one respect: `baseline` strips both
# nets' copper and reroutes them as they are; `swap` strips the same copper,
# swaps the two pads, and reroutes. `gain = baseline_total - swap_total` is
# therefore attributable to the swap alone, and is what `pin_swap.min_gain`
# is compared against.
# =========================================================================== #

# Cost bounds, not design policy (same reasoning as
# `_MAX_CANDIDATES_PER_ITERATION`): a connector with N swappable signal pins
# offers N*(N-1)/2 pairs, and every pair that reaches a trial costs two full
# reroute+rescore passes. The cheap airline estimate below ranks them all;
# only the top few are ever routed.
_MAX_PIN_SWAP_PAIRS_ESTIMATED = 60
_MAX_PIN_SWAP_TRIALS = 2


def _pin_swap_config(config: dict[str, Any]) -> dict[str, Any]:
    """Resolve the `pin_swap` block against the 7.14 schema defaults, and
    snapshot it into the session at creation for the same reason
    `_ai_decision_config` is snapshotted: a run's consent gate and threshold
    must not change under it half way through."""
    defaults = _pcb.DEFAULT_PCB_SETTINGS["pin_swap"]
    block = config.get("pin_swap", {}) or {}
    return {
        "enabled": bool(block.get("enabled", defaults["enabled"])),
        "min_gain": float(block.get("min_gain", defaults["min_gain"])),
        "ref_prefixes": list(block.get("ref_prefixes", defaults["ref_prefixes"])),
    }


# --- pad/netlist identity: the two maps every safety check here compares ---- #

def _board_pad_net_map(project_path: str | Path) -> dict[str, str]:
    """`"<REF>.<PAD>" -> net name`, straight from the BOARD's own pad `(net
    ...)` entries - the same ground truth `build_connectivity` and
    `detect_connectors` already trust over the `.net` export.

    This map is the object the whole feature's safety rule is stated in: "never
    silently change which net a real pad belongs to" is exactly "this map, for
    the real board, is not changed by anything this tool writes", which
    `_apply_session` now asserts before every write.
    """
    board_path, _, _ = _pcb._resolve_project_path(project_path)
    pads = _pcb._parse_footprint_pads_cached(board_path)
    out: dict[str, str] = {}
    for fp in pads.values():
        ref = fp.get("reference", "")
        if not ref:
            continue
        for pad in fp.get("pads", []):
            number = pad.get("number", "")
            if number:
                out[f"{ref}.{number}"] = pad.get("net", "")
    return out


def _netlist_pad_net_map(project_path: str | Path) -> dict[str, str]:
    """The same `"<REF>.<PIN>" -> net name` map as `_board_pad_net_map`, but
    from the `.net` schematic export. Empty when the project has no `.net`
    file, which is a legitimate state (several synthetic fixtures) rather than
    an error - callers treat "no netlist" as "nothing to compare against"."""
    _, _, netlist_path = _pcb._resolve_project_path(project_path)
    out: dict[str, str] = {}
    for net in _pcb._parse_nets_cached(netlist_path):
        name = net.get("name", "")
        for node in net.get("nodes", []):
            ref, pin = node.get("ref", ""), node.get("pin", "")
            if ref and pin:
                out[f"{ref}.{pin}"] = name
    return out


def _netlist_pad_mismatches(project_path: str | Path) -> list[dict[str, Any]]:
    """PAD-LEVEL netlist staleness, sorted for reproducibility.

    `detect_buses`/`classify_critical_nets` already carry a staleness guard,
    but theirs compares net NAME SETS - and a pin swap changes no net name at
    all, only which pad each name sits on, so a name-set comparison is blind to
    exactly the edit this phase asks the user to make. This is the same guard
    one level finer: for every `(ref, pad)` present in BOTH the board and the
    `.net` export, report the ones whose net disagrees. Pads present in only
    one of the two are not reported here - that is the name/DNP-level drift the
    existing guards already cover, and flagging it again would bury the two
    rows a pin swap actually produces.
    """
    board_map = _board_pad_net_map(project_path)
    netlist_map = _netlist_pad_net_map(project_path)
    return [
        {"pad": key, "board_net": board_map[key], "netlist_net": netlist_map[key]}
        for key in sorted(set(board_map) & set(netlist_map))
        if board_map[key] != netlist_map[key]
    ]


# --- trial-only s-expression surgery ---------------------------------------- #
#
# Everything below writes a board/netlist file. Every caller passes a private
# trial directory produced by `_scratch_snapshot`; nothing here is ever called
# with the real project path, and `_apply_session`'s pad-map assertion is the
# backstop that makes that a checked property rather than a promise.

def _block_end(text: str, open_idx: int) -> int:
    """Index just past the `)` matching the `(` at `open_idx`, ignoring parens
    inside quoted strings (a footprint `descr`/`datasheet` string routinely
    contains one, and a naive depth count desyncs on it - the same hazard
    `_pcb._footprint_block_span` documents)."""
    depth = 0
    i = open_idx
    in_str = False
    while i < len(text):
        c = text[i]
        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise ValueError(f"unbalanced parentheses from offset {open_idx}")


def _child_spans(text: str, start: int, end: int) -> list[tuple[int, int]]:
    """Spans of the DIRECT children of the block spanning `[start, end)`. Used
    instead of a substring search so that, say, a `(net ...)` belonging to a
    nested block can never be mistaken for the pad's own."""
    spans: list[tuple[int, int]] = []
    i = start + 1
    in_str = False
    while i < end - 1:
        c = text[i]
        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            i += 1
            continue
        if c == "(":
            child_end = _block_end(text, i)
            spans.append((i, child_end))
            i = child_end
            continue
        i += 1
    return spans


def _footprint_span_by_ref(text: str, reference: str) -> tuple[int, int]:
    """Span of the `(footprint ...)` block whose Reference property is
    `reference`. Deliberately indentation-agnostic (unlike
    `_pcb._footprint_block_span`, which anchors on a literal tab): the
    synthetic fixtures this feature is tested on indent with spaces, and a
    swap that silently found nothing on a space-indented board would make the
    tests pass for the wrong reason."""
    search = 0
    while True:
        idx = text.find("(footprint ", search)
        if idx == -1:
            raise KeyError(f"no (footprint ...) block with Reference {reference!r}")
        end = _block_end(text, idx)
        match = re.search(r'\(property\s+"Reference"\s+"([^"]*)"', text[idx:end])
        if match and match.group(1) == reference:
            return idx, end
        search = end


def _pad_net_span(text: str, fp_start: int, fp_end: int, pad_number: str) -> tuple[int, int]:
    """Span of the `(net ...)` child of pad `pad_number` inside the footprint
    block `[fp_start, fp_end)`. Raises when the pad has no `(net ...)` at all -
    an unconnected pad is not swappable and must not be silently skipped, since
    a swap that quietly moved only one of the two nets would corrupt the trial.
    """
    for pad_start, pad_end in _child_spans(text, fp_start, fp_end):
        head = text[pad_start:pad_start + 40]
        match = re.match(r'\(pad\s+"([^"]*)"', head)
        if not match or match.group(1) != pad_number:
            continue
        for child_start, child_end in _child_spans(text, pad_start, pad_end):
            if re.match(r"\(net[\s)]", text[child_start:child_start + 5]):
                return child_start, child_end
        raise KeyError(f"pad {pad_number!r} has no (net ...) entry - not swappable")
    raise KeyError(f"no pad {pad_number!r} in this footprint")


def _swap_spans(text: str, a: tuple[int, int], b: tuple[int, int]) -> str:
    """Exchange two non-overlapping substrings VERBATIM.

    Swapping the text rather than rewriting it is what makes this format-proof:
    a pad's net entry is `(net "NAME")` on some boards and `(net 7 "NAME")` on
    others (KiCad emits both shapes, and `_parse_footprint_pads` reads the last
    token either way). Exchanging the two entries preserves whichever shape the
    file uses, and carries each net's board-level index along with its name -
    no index table lookup, no format assumption, nothing to get wrong.
    """
    first, second = sorted([a, b])
    if first[1] > second[0]:
        raise ValueError("cannot swap overlapping spans")
    return (text[:first[0]] + text[second[0]:second[1]] + text[first[1]:second[0]]
            + text[first[0]:first[1]] + text[second[1]:])


def _netlist_node_span(text: str, reference: str, pin: str) -> tuple[int, int] | None:
    """Span of the `(node (ref "<reference>") (pin "<pin>") ...)` block in a
    `.net` export, or None when the export does not mention that pin (a
    perfectly ordinary state for a fixture with no netlist, and for a board
    pad the schematic does not drive)."""
    search = 0
    while True:
        idx = text.find("(node", search)
        if idx == -1:
            return None
        end = _block_end(text, idx)
        block = text[idx:end]
        ref_m = re.search(r'\(ref\s+"([^"]*)"\)', block)
        pin_m = re.search(r'\(pin\s+"([^"]*)"\)', block)
        if ref_m and pin_m and ref_m.group(1) == reference and pin_m.group(1) == pin:
            return idx, end
        search = end


def _trial_swap_pad_nets(trial: Path, reference: str, pad_a: str, pad_b: str) -> dict[str, Any]:
    """TRIAL-ONLY: exchange the nets of two pads on one connector, in both the
    trial board and (when present) the trial `.net` copy.

    The board side swaps the pads' own `(net ...)` entries; the netlist side
    swaps the two `(node ...)` blocks, which moves each pin - together with any
    `pinfunction`/`pintype` that belongs to it - under the other net's name.
    The result is exactly the project the user would have after making the
    change in the schematic and re-exporting, which is the point: the trial has
    to be the thing being proposed, not an approximation of it.

    NEVER call this on a real project directory. Its only callers snapshot the
    scratch first, and the snapshot is deleted whatever the user answers.
    """
    board_path, _, netlist_path = _pcb._resolve_project_path(trial)
    text = _pcb._read_text(board_path)
    fp_start, fp_end = _footprint_span_by_ref(text, reference)
    span_a = _pad_net_span(text, fp_start, fp_end, pad_a)
    span_b = _pad_net_span(text, fp_start, fp_end, pad_b)
    with board_path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(_swap_spans(text, span_a, span_b))
    _pcb._invalidate_board_cache(board_path)

    netlist_swapped = False
    if netlist_path.exists():
        net_text = _pcb._read_text(netlist_path)
        node_a = _netlist_node_span(net_text, reference, pad_a)
        node_b = _netlist_node_span(net_text, reference, pad_b)
        if node_a and node_b:
            with netlist_path.open("w", encoding="utf-8", newline="") as handle:
                handle.write(_swap_spans(net_text, node_a, node_b))
            # The netlist parse cache is mtime+size keyed and a swap preserves
            # size exactly (the same two blocks, exchanged), so the cache MUST
            # be dropped by hand - a same-size same-second rewrite is precisely
            # the case that validation cannot see.
            _pcb._net_cache.pop(str(netlist_path), None)
            netlist_swapped = True
    return {"reference": reference, "pads": [pad_a, pad_b], "netlist_swapped": netlist_swapped}


def _trial_strip_net_copper(trial: Path, nets: list[str]) -> int:
    """TRIAL-ONLY: delete ALL copper on `nets`, hand-routed or not, and forget
    any ownership records for it.

    `unroute_nets` deliberately cannot do this - it only ever removes
    autorouter-owned uuids, which is exactly the guard that keeps human copper
    safe everywhere else in this module, and nothing here weakens it. But the
    A/B measurement needs both sides to start from "these two nets have no
    copper": on a hand-routed board the baseline arm would otherwise keep the
    human's copper while the swap arm could not (the human's copper runs to the
    pads the swap just reassigned, so it is not merely suboptimal, it is
    wrong), and the resulting "gain" would be measuring the human, not the
    swap.

    Ripping hand copper is safe here for one structural reason only: a pin-swap
    trial is NEVER promoted over the scratch. The directory this writes to is
    deleted whatever the user answers, so no copper it removes can reach the
    session's board, let alone the real one.
    """
    board_path, _, _ = _pcb._resolve_project_path(trial)
    tracks = _pcb._parse_tracks_cached(board_path)
    wanted = set(nets)
    uuids = {
        item["uuid"]
        for group in ("segments", "arcs", "vias")
        for item in tracks[group]
        if item.get("net") in wanted and item.get("uuid")
    }
    if not uuids:
        return 0
    text, removed = _r._delete_blocks_by_uuid(_pcb._read_text(board_path), uuids)
    with board_path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    _pcb._invalidate_board_cache(board_path)

    data = _pcb.load_board_local(trial)["data"]
    owned = data.get("autorouter_owned", {}) or {}
    for key in ("segments", "vias", "arcs"):
        if owned.get(key):
            owned[key] = [u for u in owned[key] if u not in uuids]
    if owned.get("records"):
        owned["records"] = [rec for rec in owned["records"] if rec.get("uuid") not in uuids]
    _pcb.save_board_local(trial, data)
    return removed


# --- candidate pairs -------------------------------------------------------- #

def _swappable_connector_pins(project: Path, cfg: dict[str, Any],
                              exclusions: list[str]) -> list[dict[str, Any]]:
    """Every connector pin this session may consider trading, in
    `detect_connectors`'s own sorted order (ranking is the caller's job).

    Three filters, all from the plan's own interaction contract:
      - the connector must be one `detect_connectors` found under
        `pin_swap.ref_prefixes` (or by footprint token - detection's own
        either-signal rule, reused rather than re-derived);
      - the connector must not be in `exclusions` (validated loudly at session
        creation via `validate_connector_exclusions`, so an unresolved name
        cannot silently leave a connector the user meant to protect eligible);
      - the pin's net must be a SIGNAL net. Power/ground pins are excluded
        outright via `_net_kind` - the same Phase 9 classification the rest of
        this codebase uses - because a connector's supply pins are fixed by the
        mating part's pinout, not by what routes prettily, and "the optimizer
        suggested moving your ground pin" is advice no one should be given.
    """
    settings = _pcb.load_pcb_settings(project)["config"]
    power_patterns = settings.get("layer_purpose", {}).get("power_net_patterns", [])
    excluded = {str(name).strip().upper() for name in exclusions}

    detected = _pcb.detect_connectors(project, ref_prefixes=cfg["ref_prefixes"])
    pins: list[dict[str, Any]] = []
    board_path, _, _ = _pcb._resolve_project_path(project)
    positions = {
        f"{fp.get('reference', '')}.{pad.get('number', '')}": pad.get("position", {"x": 0.0, "y": 0.0})
        for fp in _pcb._parse_footprint_pads_cached(board_path).values()
        for pad in fp.get("pads", [])
    }
    for candidate in detected["candidates"]:
        ref = candidate["ref"]
        if ref.upper() in excluded:
            continue
        for pin in candidate["pins"]:
            net = pin.get("net", "")
            if not net:
                continue
            if _pcb._net_kind(net, power_net_patterns=power_patterns) != "signal":
                continue
            pins.append({
                "ref": ref, "pad": pin["pad"], "net": net,
                "position": positions.get(f"{ref}.{pin['pad']}", {"x": 0.0, "y": 0.0}),
            })
    return pins


def _net_anchor_points(project: Path) -> dict[str, list[dict[str, Any]]]:
    """Every net's pad positions (with the owning ref, so the estimate can
    exclude the connector's own pads), for the cheap airline estimate below."""
    board_path, _, _ = _pcb._resolve_project_path(project)
    anchors: dict[str, list[dict[str, Any]]] = {}
    for fp in _pcb._parse_footprint_pads_cached(board_path).values():
        ref = fp.get("reference", "")
        for pad in fp.get("pads", []):
            net = pad.get("net", "")
            if net:
                anchors.setdefault(net, []).append({
                    "x": float(pad.get("position", {}).get("x", 0.0)),
                    "y": float(pad.get("position", {}).get("y", 0.0)),
                    "ref": ref, "pad": pad.get("number", ""),
                })
    return anchors


def _pin_swap_pairs(project: Path, cfg: dict[str, Any],
                    exclusions: list[str]) -> list[dict[str, Any]]:
    """Rank candidate pin pairs by a cheap airline ESTIMATE, best-first.

    The estimate is deliberately crude and is never reported as the gain: for
    each pin, the distance from its pad to the centroid of the REST of its net
    (its pads elsewhere on the board). A pair is promising when the two nets
    would each end up closer to their own destinations after trading pins.
    Its only job is to decide which handful of pairs is worth the two full
    reroute trials that produce the real, routed number - a real board's
    10-pin connector offers 45 pairs and routing all of them per iteration is
    not a budget anyone has.

    Consumes no RNG and iterates only sorted lists: with `pin_swap.enabled`
    false this function is never called at all, and when it is called it cannot
    perturb the seed-replayability of the six copper moves.
    """
    pins = _swappable_connector_pins(project, cfg, exclusions)
    anchors = _net_anchor_points(project)

    def remote_centroid(net: str, ref: str) -> tuple[float, float] | None:
        # "The rest of the net" excludes every pad on the connector itself:
        # measuring to a point that moves with the swap would make the estimate
        # compare the pair against itself.
        remote = [p for p in anchors.get(net, []) if p["ref"] != ref]
        if not remote:
            return None
        return (sum(p["x"] for p in remote) / len(remote),
                sum(p["y"] for p in remote) / len(remote))

    by_ref: dict[str, list[dict[str, Any]]] = {}
    for pin in pins:
        by_ref.setdefault(pin["ref"], []).append(pin)

    pairs: list[dict[str, Any]] = []
    for ref in sorted(by_ref):
        group = sorted(by_ref[ref], key=lambda p: (p["net"], _pcb._pad_sort_key(p["pad"])))
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                if a["net"] == b["net"]:
                    continue   # trading two pins of the SAME net changes nothing
                ca, cb = remote_centroid(a["net"], ref), remote_centroid(b["net"], ref)
                if ca is None or cb is None:
                    continue   # a net that only touches this connector has no
                               # destination to be closer to - nothing to estimate
                ax, ay = a["position"]["x"], a["position"]["y"]
                bx, by = b["position"]["x"], b["position"]["y"]
                own = math.hypot(ax - ca[0], ay - ca[1]) + math.hypot(bx - cb[0], by - cb[1])
                swapped = math.hypot(ax - cb[0], ay - cb[1]) + math.hypot(bx - ca[0], by - ca[1])
                pairs.append({
                    "key": f"{ref}:{a['pad']}<->{b['pad']}",
                    "ref": ref,
                    "pad_a": a["pad"], "net_a": a["net"],
                    "pad_b": b["pad"], "net_b": b["net"],
                    "estimated_gain_mm": round(own - swapped, 4),
                })
    # Deterministic: estimate desc, then the pair key, which is unique per pair.
    pairs.sort(key=lambda p: (-p["estimated_gain_mm"], p["key"]))
    return pairs[:_MAX_PIN_SWAP_PAIRS_ESTIMATED]


# --- pricing ---------------------------------------------------------------- #

def _score_pin_swap(scratch: Path, trial_root: Path, pair: dict[str, Any],
                    iteration: int, index: int) -> dict[str, Any]:
    """Price ONE candidate pair as the controlled A/B described in this
    section's header, then delete both arms.

    Returns the pair enriched with `baseline_score`/`swap_score`/`gain`, or
    with `priced: False` plus the error when either arm cannot be built (a
    board the swap surgery cannot express, a route that raises). An unpriceable
    pair is reported, never guessed at.
    """
    record: dict[str, Any] = {**pair, "priced": False}
    nets = [pair["net_a"], pair["net_b"]]
    baseline_dir = trial_root / f"i{iteration}_pinswap{index}_base"
    swap_dir = trial_root / f"i{iteration}_pinswap{index}_swap"
    try:
        baseline = _scratch_snapshot(scratch, baseline_dir)
        _trial_strip_net_copper(baseline, nets)
        base_stats = _reroute_nets_in_order(baseline, nets, max_ripup=4)
        record["baseline_score"] = score_board(baseline)

        swap = _scratch_snapshot(scratch, swap_dir)
        _trial_strip_net_copper(swap, nets)
        record["swap_detail"] = _trial_swap_pad_nets(swap, pair["ref"], pair["pad_a"], pair["pad_b"])
        swap_stats = _reroute_nets_in_order(swap, nets, max_ripup=4)
        record["swap_score"] = score_board(swap)

        record["gain"] = round(record["baseline_score"]["total"] - record["swap_score"]["total"], 6)
        record["baseline_routing"] = base_stats
        record["swap_routing"] = swap_stats
        record["priced"] = True
    except Exception as exc:
        record["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        # Both arms go, always. A pin-swap trial is the one trial in this
        # module that must never survive its own evaluation - see the section
        # header on why it can never be promoted.
        shutil.rmtree(baseline_dir, ignore_errors=True)
        shutil.rmtree(swap_dir, ignore_errors=True)
    return record


def _pin_swap_gate(session: dict[str, Any], scratch: Path, trial_root: Path,
                   iteration: int) -> dict[str, Any] | None:
    """The mandatory-escalation gate, run once per iteration BEFORE any copper
    candidate is generated.

    Returns a `pending_decision` when some unexamined pair clears
    `pin_swap.min_gain`, else None (with every pair it priced recorded on the
    session's `pin_swap_reports`, so a sub-threshold swap is VISIBLE - the
    plan's "sub-threshold swaps are reported, not proposed" - without ever
    becoming an actionable decision).

    Runs before candidate generation and consumes no RNG, so an
    `enabled: false` session (the default) reaches `_generate_candidates` with
    the RNG in exactly the state the pre-7.14 code left it in. That is the
    whole parity argument, and it is why the `enabled` test is the very first
    line here.
    """
    cfg = session["pin_swap"]
    if not cfg["enabled"]:
        return None

    already = set(session.get("pin_swap_examined", []))
    pairs = [p for p in _pin_swap_pairs(scratch, cfg, session.get("pin_swap_exclusions", []))
             if p["key"] not in already]
    # Only pairs the estimate likes are worth routing; a pair whose airline
    # estimate is non-positive cannot plausibly clear a positive `min_gain`,
    # and pricing it would spend two reroutes to say so.
    promising = [p for p in pairs if p["estimated_gain_mm"] > 0][:_MAX_PIN_SWAP_TRIALS]
    if not promising:
        return None

    priced = [_score_pin_swap(scratch, trial_root, pair, iteration, index)
              for index, pair in enumerate(promising)]
    for record in priced:
        session["pin_swap_examined"].append(record["key"])
        session["pin_swap_reports"].append({
            "iteration": iteration,
            "key": record["key"],
            "ref": record["ref"],
            "pads": [record["pad_a"], record["pad_b"]],
            "nets": [record["net_a"], record["net_b"]],
            "estimated_gain_mm": record["estimated_gain_mm"],
            "priced": record["priced"],
            "gain": record.get("gain"),
            "min_gain": cfg["min_gain"],
            "proposed": bool(record["priced"] and record.get("gain", 0.0) >= cfg["min_gain"]),
            "error": record.get("error"),
        })

    winners = [r for r in priced if r["priced"] and r["gain"] >= cfg["min_gain"]]
    if not winners:
        return None
    best = max(winners, key=lambda r: (r["gain"], r["key"]))

    # The option list is NOT a menu of applied moves (there are none - see the
    # section header). It is the two things a human can actually answer.
    # "Decline" is first, and therefore the default, because both `defer` and a
    # resume without an answer resolve to the default: neither may ever assume
    # a schematic edit happened on the user's behalf.
    options = [
        {
            "id": "opt1",
            "type": "pin_swap_decline",
            "summary": (f"Leave {best['ref']} pins {best['pad_a']}/{best['pad_b']} as they are "
                        f"({best['net_a']} / {best['net_b']}) and keep optimizing."),
            "is_default": True,
            "score": best["baseline_score"],
            "score_total": best["baseline_score"]["total"],
            "score_delta": 0.0,
            "trial_dir": None,
            "detail": None,
            "svg": None,
        },
        {
            "id": "opt2",
            "type": "pin_swap_applied",
            "summary": (f"I swapped {best['ref']} pins {best['pad_a']} and {best['pad_b']} "
                        f"({best['net_a']} <-> {best['net_b']}) in the schematic and re-exported "
                        "the netlist - re-sync and continue."),
            "is_default": False,
            "score": best["swap_score"],
            "score_total": best["swap_score"]["total"],
            "score_delta": round(-best["gain"], 6),
            "trial_dir": None,
            "detail": best.get("swap_detail"),
            "svg": None,
        },
    ]
    return {
        "decision_id": f"{session['session_id'][:8]}-i{iteration}-pinswap",
        "iteration": iteration,
        "decision_type": "pin_swap",
        "default_choice": "opt1",
        "options": options,
        # No `pending_dir`: there is nothing to park. The other decision types
        # carry applied trial directories that must survive an MCP restart;
        # this one carries a question and two numbers, both already in the
        # checkpoint.
        "pending_dir": None,
        "candidates_evaluated": len(priced),
        "current_score": session["current_score"]["total"],
        "score_spread": round(best["gain"], 6),
        "min_score_spread": None,
        "pin_swap": {
            "key": best["key"],
            "ref": best["ref"],
            "pad_a": best["pad_a"], "net_a": best["net_a"],
            "pad_b": best["pad_b"], "net_b": best["net_b"],
            "gain": round(best["gain"], 6),
            "min_gain": cfg["min_gain"],
            "baseline_score": best["baseline_score"],
            "swap_score": best["swap_score"],
            "estimated_gain_mm": best["estimated_gain_mm"],
            "measurement": (
                "Controlled A/B on two disposable copies of the session's scratch board: both "
                "arms strip these two nets' copper and reroute them with the same router; the "
                "swap arm additionally trades the two pads' nets. `gain` is therefore "
                "attributable to the swap alone. NOTHING was applied - this tool never edits a "
                "schematic or a netlist, so only you can realize this change."
            ),
            "instructions": (
                f"To take it: in the schematic, swap which net lands on {best['ref']} pin "
                f"{best['pad_a']} and pin {best['pad_b']}, re-export the netlist, update the PCB "
                "from the schematic in KiCad, then answer this decision with opt2 to re-sync "
                "the session against the new pad assignment. Answer opt1 to decline."
            ),
        },
    }


def _resolve_pin_swap_pending(session: dict[str, Any], choice: str, rationale: str | None,
                              auto: bool, auto_reason: str | None) -> dict[str, Any]:
    """Answer a `pin_swap` pause. Commits NO move - there is none to commit -
    so the iteration counter, RNG, SA temperature and score curve are all left
    exactly as the pause found them, and the very next chunk runs the copper
    iteration that the gate interrupted, bit for bit as it would have.

    The pair is recorded in `pin_swap_examined` by the gate itself, so neither
    answer can loop: a declined swap is not re-proposed, and an applied one is
    already reality by the time the session sees it again.
    """
    pending = session["pending_decision"]
    options = {o["id"]: o for o in pending["options"]}
    resolved = pending["default_choice"] if choice == "defer" else choice
    if resolved not in options:
        raise ValueError(
            f"choice {choice!r} is not one of this decision's options "
            f"({', '.join(sorted(options))}) or the literal 'defer'")

    resync: dict[str, Any] | None = None
    if options[resolved]["type"] == "pin_swap_applied":
        resync = _resync_pad_nets(session, pending["pin_swap"])

    entry = {
        **_log_entry(pending["decision_id"], pending["iteration"], "pin_swap",
                     pending["options"], choice, resolved, rationale, auto, auto_reason),
        # A pin-swap decision is advisory: `accepted` is False on BOTH answers
        # because this tool applied nothing either way, and a log that claimed
        # otherwise would misreport the one thing this feature promises.
        "accepted": False,
        "accept_reason": "pin_swap_advisory_never_applied_by_this_tool",
        "score_before": session["current_score"]["total"],
        "score_after": session["current_score"]["total"],
        "delta": 0.0,
        "pin_swap": pending["pin_swap"],
        "resync": resync,
    }
    session["decision_log"].append(entry)
    session["pending_decision"] = None
    session["state"] = "running"
    session["stop_reason"] = None
    return {"resolved_choice": resolved, "improvement": 0.0, "entry": entry, "resync": resync}


def _resync_pad_nets(session: dict[str, Any], swap: dict[str, Any]) -> dict[str, Any]:
    """Re-sync the session's scratch board against the REAL project's current
    pad-net assignment, after the user reports making the schematic change.

    This is the plan's "the session re-syncs (netlist-staleness check) and
    continues", done at pad level because that is the only level a pin swap is
    visible at - it changes no net NAME, so the existing name-set staleness
    guards in `detect_buses`/`classify_critical_nets` cannot see it (see
    `_netlist_pad_mismatches`).

    Three things happen, in order:
      1. The real board's pad-net map is diffed against the scratch's. Every
         pad that now disagrees is copied FROM THE REAL BOARD onto the scratch.
         The direction matters: the new assignment is the user's, imported into
         KiCad from their own schematic - this function never decides what a
         pad's net should be, it only adopts what the real board already says.
      2. Any net touched by that diff is rerouted on the scratch, because
         copper that ran to a pad which has changed nets is no longer valid
         connectivity and the session's score has to describe a board that is
         actually connected. Only AUTOROUTER-OWNED copper is replaced (the
         reroute goes through `unroute_nets`, whose ownership guard is never
         bypassed here either); hand copper on an affected net is reported in
         `hand_copper_nets` for the user to redo in KiCad, not deleted.
      3. The real project's own board-vs-`.net` pad mismatches are reported.
         A user who edited the schematic but forgot to re-export (or to update
         the PCB from it) gets told so here rather than optimizing a board that
         disagrees with its own netlist.

    When nothing changed, this reports `resynced: False` with the reason and
    changes nothing at all - "the user answered 'applied' before actually
    applying it" must not silently corrupt the session.
    """
    project_path = session["project_path"]
    scratch = Path(session["scratch_dir"])
    real_map = _board_pad_net_map(project_path)
    scratch_map = _board_pad_net_map(scratch)

    changed = sorted(
        (
            {"pad": key, "from_net": scratch_map[key], "to_net": real_map[key]}
            for key in set(real_map) & set(scratch_map)
            if real_map[key] != scratch_map[key]
        ),
        key=lambda c: c["pad"],
    )
    netlist_mismatches = _netlist_pad_mismatches(project_path)
    expected = f"{swap['ref']}.{swap['pad_a']}"

    result: dict[str, Any] = {
        "expected_swap": swap["key"],
        "changed_pads": changed,
        "real_netlist_pad_mismatches": netlist_mismatches,
        "rerouted_nets": [],
        "resynced": False,
    }
    if not changed:
        result["reason"] = (
            f"The real board's pad-net assignment is unchanged (pad {expected} still reads "
            f"{real_map.get(expected, '<absent>')!r}), so there is nothing to re-sync. If you made "
            "the schematic edit, run 'Update PCB from Schematic' in KiCad so the board's pads "
            "carry the new nets, then start a new session against that board."
        )
        return result

    # Adopt the real board's assignment pad by pad. Two pads whose nets simply
    # traded places are the expected shape, but this deliberately handles ANY
    # difference the same way: the real board is the authority, and a session
    # that only understood the one edit it proposed would silently ignore a
    # second change the user made while they were in there.
    board_path, _, _ = _pcb._resolve_project_path(scratch)
    text = _pcb._read_text(board_path)
    for change in changed:
        ref, _, pad = change["pad"].rpartition(".")
        fp_start, fp_end = _footprint_span_by_ref(text, ref)
        net_start, net_end = _pad_net_span(text, fp_start, fp_end, pad)
        entry = text[net_start:net_end]
        # Reuse the existing entry's own shape, replacing only the trailing
        # name token, so a `(net 7 "OLD")` board keeps its index form. The index
        # is now wrong for this pad, but nothing in the routing/scoring path
        # reads it (every reader takes the last token as the name) and inventing
        # an index here would mean maintaining the board's net table - real
        # surgery this scratch-side adoption does not need.
        text = text[:net_start] + re.sub(r'"[^"]*"\s*\)$', f'"{change["to_net"]}")', entry) + text[net_end:]
    with board_path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    _pcb._invalidate_board_cache(board_path)

    affected = sorted({c["from_net"] for c in changed if c["from_net"]}
                      | {c["to_net"] for c in changed if c["to_net"]})
    # `_reroute_nets_in_order` goes through `unroute_nets`, so ONLY
    # autorouter-owned copper on those nets is replaced. Human copper on an
    # affected net is left exactly where it is and REPORTED instead: it is now
    # wired to a pad that belongs to a different net, but deleting a human's
    # copper is not this module's call to make anywhere else and a pin swap is
    # not the place to start. The user has to redo it in KiCad, which is the
    # same place they made the swap.
    _reroute_nets_in_order(scratch, affected, max_ripup=4)
    owned = _pcb.load_board_local(scratch)["data"].get("autorouter_owned", {}) or {}
    owned_uuids = set(owned.get("segments", []) or []) | set(owned.get("vias", []) or [])
    affected_set = set(affected)
    tracks = _pcb._parse_tracks_cached(board_path)
    result["hand_copper_nets"] = sorted({
        item["net"] for group in ("segments", "arcs", "vias") for item in tracks[group]
        if item.get("net") in affected_set and item.get("uuid") not in owned_uuids
    })

    session["current_score"] = score_board(scratch)
    session["score_curve"].append(session["current_score"]["total"])
    if session["current_score"]["total"] < session["best_score"]["total"]:
        session["best_score"] = session["current_score"]
    # The write guard compares the real board against the fingerprint taken at
    # session creation, and the user has just deliberately changed that board.
    # Re-taking it here is what lets an acknowledged, re-synced session still
    # write; leaving it stale would refuse every future write for a change the
    # session itself asked the user to make.
    session["board_fingerprint"] = _board_fingerprint(project_path)
    result["rerouted_nets"] = affected
    result["resynced"] = True
    return result


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


# --------------------------------------------------------------------------- #
# Phase 7.7 - decisions
#
# The gate, the option list, and the ONE place a chosen candidate is committed.
# Auto-acceptance and AI-acceptance go through the same commit, which is what
# makes the parity claim checkable rather than asserted: an auto-decided
# iteration executes literally the same code as before, plus a log append.
# --------------------------------------------------------------------------- #

def _decision_type_for(session: dict[str, Any], best: dict[str, Any]) -> str:
    """Classify the decision the leading candidate represents (see
    `_MOVE_DECISION_TYPES` for why the situation-derived types win)."""
    detail = best.get("detail") or {}
    if int(detail.get("failed", 0) or 0) > 0:
        return "give_up_net"
    if session["accept"] == "sa" and len(detail.get("nets", []) or []) >= _SA_LARGE_MOVE_NETS:
        return "sa_large_move"
    return _MOVE_DECISION_TYPES[best["type"]]


def _option_records(usable: list[dict[str, Any]], current_total: float) -> list[dict[str, Any]]:
    """Project the scored candidates into the option list a decision (and every
    `decision_log` entry) is made of.

    `usable` arrives already sorted best-first, so `opt1` is ALWAYS the
    optimizer's own default - which is what `defer` resolves to and what the
    7.6 core would have taken unconditionally.
    """
    options = []
    for index, record in enumerate(usable[:_MAX_DECISION_OPTIONS], start=1):
        options.append({
            "id": f"opt{index}",
            "type": record["type"],
            "summary": record["summary"],
            "score": record["score"],
            "score_total": record["score"]["total"],
            "score_delta": round(record["score"]["total"] - current_total, 6),
            "spread_from_best": round(record["score"]["total"] - usable[0]["score"]["total"], 6),
            "is_default": index == 1,
            "trial_dir": record["trial_dir"],
            "detail": record.get("detail"),
            # Per-option SVG previews are specified in the plan but not built
            # here: nothing in this codebase renders a board to SVG, so it would
            # mean new export machinery (and a KiCad CLI dependency) on the
            # critical path of every paused iteration. The numbers plus the
            # one-line summary are what the decision is actually made on.
            "svg": None,
        })
    return options


def _log_entry(decision_id: str, iteration: int, decision_type: str,
               options: list[dict[str, Any]], choice: str, resolved: str,
               rationale: str | None, auto: bool, auto_reason: str | None) -> dict[str, Any]:
    """One `decision_log` row. Deliberately self-contained (the options and
    their scores are copied in, not referenced) so the log alone is enough to
    understand - or replay - why the board came out the way it did."""
    return {
        "decision_id": decision_id,
        "iteration": iteration,
        "decision_type": decision_type,
        "options": [{"id": o["id"], "type": o["type"], "summary": o["summary"],
                     "score_total": o["score_total"], "score_delta": o["score_delta"],
                     "is_default": o["is_default"]} for o in options],
        "scores": {o["id"]: o["score_total"] for o in options},
        "choice": choice,
        "resolved_choice": resolved,
        "rationale": rationale,
        "auto": auto,
        "auto_reason": auto_reason,
    }


def _pause_check(session: dict[str, Any], options: list[dict[str, Any]],
                 decision_type: str) -> str | None:
    """Should this iteration pause for the AI? Returns None to pause, or the
    reason it is auto-decided instead (which goes straight into the log, so a
    run's history says WHY each move was never escalated).

    Consumes no RNG and reads no board: whether a pause happens can never
    perturb the run's decisions, only defer them.
    """
    policy = session["ai_decisions"]
    if not policy["enabled"]:
        return "ai_decisions_disabled"
    if len(options) < _MIN_DECISION_OPTIONS:
        return "single_applicable_candidate"
    if decision_type not in policy["decision_types"]:
        return f"decision_type_not_enabled:{decision_type}"
    if session["pauses_used"] >= policy["max_pauses_per_run"]:
        return "max_pauses_per_run"
    # A decision whose outcome cannot change the board is not a decision. Under
    # `greedy` every option is at least as expensive as the leader, so if the
    # LEADER does not improve the score, every option is rejected whichever one
    # is picked - and a converged board (where nothing improves any more) would
    # otherwise escalate a menu of six moves that are all about to be thrown
    # away. Under `sa` a worse move can genuinely be accepted, so the choice
    # still matters there.
    if session["accept"] != "sa" and options[0]["score_delta"] >= 0:
        return "no_improving_candidate"
    spread = options[1]["score_total"] - options[0]["score_total"]
    if spread >= policy["min_score_spread"]:
        return f"clear_winner:spread={round(spread, 6)}"
    return None


def _park_pending_options(session: dict[str, Any], options: list[dict[str, Any]]) -> None:
    """Copy the option trials out of the per-chunk trial root into a stable
    `_pending` directory beside the scratch, and repoint the options at it.

    The trial root is wiped at the end of every chunk (and by the next
    iteration), but a pause has to survive until the AI answers - possibly after
    an MCP restart. Copying is cheap here: a project snapshot is board +
    .kicad_pro + .net + two small JSONs, and there are at most four of them.
    """
    scratch = Path(session["scratch_dir"])
    pending_root = scratch.parent / f"{scratch.name}_pending"
    shutil.rmtree(pending_root, ignore_errors=True)
    pending_root.mkdir(parents=True, exist_ok=True)
    for option in options:
        option["trial_dir"] = str(_scratch_snapshot(Path(option["trial_dir"]),
                                                    pending_root / option["id"]))


def _commit_choice(session: dict[str, Any], chosen: dict[str, Any],
                   options: list[dict[str, Any]], rng: random.Random,
                   log: dict[str, Any], evaluated_count: int) -> float:
    """Apply one chosen candidate to the session and record it. The single
    place a move is ever committed, whether it was auto-taken or AI-chosen.

    `chosen` carries a `trial_dir` that has ALREADY been applied and scored;
    promoting that directory over the scratch (rather than replaying the move)
    is what guarantees the accepted state is exactly the state that was scored.
    Returns the score improvement, which the caller tests for convergence.
    """
    session["iteration"] += 1
    current_total = session["current_score"]["total"]
    delta = round(chosen["score_total"] - current_total, 6)
    accepted, reason = _accept(session["accept"], delta, session["temperature"], rng)

    if accepted:
        _scratch_snapshot(Path(chosen["trial_dir"]), Path(session["scratch_dir"]))
        session["current_score"] = chosen["score"]
        if chosen["score"]["total"] < session["best_score"]["total"]:
            session["best_score"] = chosen["score"]
        # Phase 7.15: only an ACCEPTED move that genuinely lowered the score
        # counts as "productive" for the plateau rate - an SA-accepted worse
        # move (negative improvement) says nothing about the pace of genuine
        # improvement, so it is excluded exactly like a rejected move.
        productive = current_total - session["current_score"]["total"]
        if productive > 0:
            session["productive_improvements"].append(round(productive, 6))

    session["moves"].append({
        "iteration": session["iteration"],
        "type": chosen["type"],
        "summary": chosen["summary"],
        "detail": chosen.get("detail"),
        "accepted": accepted,
        "reason": reason,
        "score_before": current_total,
        "score_after": session["current_score"]["total"],
        "delta": round(session["current_score"]["total"] - current_total, 6),
        "candidates_evaluated": evaluated_count,
        "candidates_applicable": len(options),
    })
    session["score_curve"].append(session["current_score"]["total"])
    session["decision_log"].append({
        **log,
        "accepted": accepted,
        "accept_reason": reason,
        "score_before": current_total,
        "score_after": session["current_score"]["total"],
        "delta": round(session["current_score"]["total"] - current_total, 6),
    })
    return current_total - session["current_score"]["total"]


def _resolve_pending(session: dict[str, Any], choice: str, rationale: str | None,
                     auto: bool, auto_reason: str | None) -> dict[str, Any]:
    """Turn an `awaiting_decision` session back into a `running` one by
    committing the chosen option. Shared by `decide_route` (an explicit answer)
    and by `optimize_board`'s timeout-to-defer on resume.

    Deliberately does NOT run further iterations: resolving a pause and doing
    more work are separate calls, so the caller always sees the consequence of
    its own decision before the next one is generated.
    """
    pending = session["pending_decision"]
    # 7.14: a pin-swap pause is answered on a separate path because it has no
    # applied candidate to commit - there is no trial directory to promote, no
    # score to move, and no move to log as accepted. Routing it through
    # `_commit_choice` below would promote a board carrying a pad-net
    # reassignment this tool is never allowed to make.
    if pending.get("decision_type") == "pin_swap":
        return _resolve_pin_swap_pending(session, choice, rationale, auto, auto_reason)

    options = {o["id"]: o for o in pending["options"]}
    resolved = pending["default_choice"] if choice == "defer" else choice
    if resolved not in options:
        raise ValueError(
            f"choice {choice!r} is not one of this decision's options "
            f"({', '.join(sorted(options))}) or the literal 'defer'")

    chosen = options[resolved]
    rng = _rng_from_json(session["rng_state"])
    improvement = _commit_choice(
        session, chosen, pending["options"], rng,
        _log_entry(pending["decision_id"], pending["iteration"], pending["decision_type"],
                   pending["options"], choice, resolved, rationale, auto, auto_reason),
        pending["candidates_evaluated"])

    if session["accept"] == "sa":
        session["temperature"] *= session["sa_cooling"]
    session["rng_state"] = _rng_state_to_json(rng)
    session["pending_decision"] = None
    # The SAME convergence/plateau tests the auto path applies to the move it
    # just committed. Skipping them here would be a parity break, not a
    # simplification: a run whose final move happened to be escalated would
    # report `budget_exhausted` on the next resume where the identical
    # unescalated run reported `converged`.
    plateau_reason = _plateau_check(session)
    if improvement < session["convergence_delta"]:
        session["state"] = "converged"
        session["stop_reason"] = "convergence_delta"
    elif plateau_reason:
        session["state"] = "converged"
        session["stop_reason"] = plateau_reason
    else:
        session["state"] = "running"
        session["stop_reason"] = None
    shutil.rmtree(Path(pending["pending_dir"]), ignore_errors=True)
    return {"resolved_choice": resolved, "improvement": round(improvement, 6),
            "entry": session["decision_log"][-1]}


def decide_route(project_path: str | Path, session_id: str, decision_id: str,
                 choice: str, rationale: str | None = None) -> dict[str, Any]:
    """Phase 7.7 - answer a paused optimizer session (MCP `decide_kicad_route`).

    `choice` is one of the pending decision's option ids, or the literal
    `"defer"` meaning "take the optimizer's own best-scored default". The
    chosen candidate is applied, the decision (options, scores, choice,
    rationale) is appended to `decision_log`, and the session returns to
    `running` - or to `converged`, if the move it just committed was the one
    that stopped buying `convergence_delta`.

    This call runs NO further iterations - resume with `optimize_board(...,
    session_id=...)` afterwards. `rationale` is recorded and never executed.

    Phase 7.14 - a `pin_swap` decision is answered here too, but it commits
    nothing: `opt1` declines the proposed connector pin swap, `opt2` reports
    that you made the change in the schematic and re-exported the netlist, at
    which point the session RE-SYNCS its scratch board against the real board's
    current pad-net assignment (adopting it, never deciding it) and reports the
    result under `resync`. Answering `opt2` without having made the change is
    harmless: the re-sync finds nothing to adopt and says so. Either way the
    pair is not proposed again, and this tool has still written neither the
    schematic nor the real `.net` file.
    """
    config = _pcb.load_pcb_settings(project_path)["config"]
    data, sessions = _load_sessions(project_path)
    if session_id not in sessions:
        raise KeyError(f"No optimizer session {session_id!r} on this board")
    session = _migrate_session(sessions[session_id], config)

    if session["state"] != "awaiting_decision" or not session.get("pending_decision"):
        raise ValueError(
            f"session {session_id} is {session['state']!r}, not 'awaiting_decision' - "
            "there is no decision to answer")
    pending = session["pending_decision"]
    if decision_id != pending["decision_id"]:
        raise ValueError(
            f"decision_id {decision_id!r} does not match this session's pending decision "
            f"{pending['decision_id']!r}")

    outcome = _resolve_pending(session, str(choice), rationale, auto=False, auto_reason=None)
    data["last_optimizer_session"] = session["session_id"]
    _pcb.save_board_local(project_path, data)
    return {
        "command": "decide_route",
        "decision_id": decision_id,
        "choice": choice,
        "resolved_choice": outcome["resolved_choice"],
        "rationale": rationale,
        "decision": outcome["entry"],
        # Present (non-None) only for a `pin_swap` decision answered `opt2`.
        "resync": outcome.get("resync"),
        **_session_report(session),
        "notes": ["Decision recorded and applied; call optimize_kicad_board with this "
                  "session_id to continue optimizing."],
    }


## Phase 7.15: effort presets ------------------------------------------------
#
# `optimizer.effort` bundles the OTHER optimizer knobs into one question ("how
# much do you want to spend?") instead of asking a caller to tune six numbers
# individually. "balanced" is deliberately NOT hardcoded below: it means
# "whatever `optimizer.*` already says" - today's pre-7.15 behaviour, verbatim,
# for every project that never touches `effort` at all. "quick"/"best" DO carry
# their own numbers because the plan gives them concrete values (5 iterations/
# greedy for quick; SA + an overnight time budget for best) that are meant to
# win over the generic per-knob config, the same way a call-time argument wins
# over the preset. The precedence is therefore three deep: call-time argument
# > effort preset (quick/best only) > `optimizer.*` config value (which is
# what "balanced" resolves to, and what a knob outside a preset's bundle
# always resolves to).
#
# HONEST SCOPE-DOWN: `autorouter.cpu.replicas` (the knob the plan's "quick:
# replicas 1" / "best: replicas max" language refers to) is defined in
# `pcb_settings.json` but is not read by anything in this module, the router,
# or anywhere else in the codebase today (grep confirms - see NETCLASS_PLAN.md
# 7.15/7.9 notes on the CPU parallelism knobs being schema-only pending M4
# wiring). Presets below therefore do NOT set `replicas`: doing so would be
# decorative, and this module doesn't invent wiring for a knob nothing
# consumes. This is noted in `optimize_board`'s docstring too.
_EFFORT_PRESETS: dict[str, dict[str, Any]] = {
    "quick": {
        "max_iterations": 5,
        "accept": "greedy",
    },
    "best": {
        "accept": "sa",
        # "Hours-scale... overnight" (plan's words) - 8 hours is a full
        # unattended overnight run without assuming a multi-day session; a
        # session still checkpoints every chunk, so this is a ceiling the run
        # can converge or be stopped well before, not a promise to run 8h.
        "time_budget_s": 8.0 * 3600.0,
    },
}


def _resolve_effort_knobs(optimizer: dict[str, Any], effort: str,
                          accept: str | None, max_iterations: int | None,
                          time_budget_s: float | None) -> dict[str, Any]:
    """Resolve `max_iterations`/`accept`/`time_budget_s` through the
    call-time-arg > effort-preset > config precedence described above.
    `worst_k`/`sa_initial_temp`/`sa_cooling`/`convergence_delta` are NOT part
    of any effort bundle (the plan does not mention them for any tier), so
    they are always resolved straight from `optimizer.*` regardless of
    `effort` - unchanged from pre-7.15 behaviour."""
    preset = _EFFORT_PRESETS.get(effort, {})
    return {
        "max_iterations": (int(max_iterations) if max_iterations is not None
                          else int(preset.get("max_iterations", optimizer.get("max_iterations", 20)))),
        "accept": str(accept or preset.get("accept", optimizer.get("accept", "greedy"))).lower(),
        "time_budget_s": (float(time_budget_s) if time_budget_s is not None
                         else float(preset.get("time_budget_s", optimizer.get("time_budget_s", 300)))),
    }


def _new_session(project_path: str | Path, config: dict[str, Any],
                 seed: int | None, accept: str | None,
                 max_iterations: int | None, time_budget_s: float | None,
                 effort: str | None = None,
                 pin_swap_exclusions: list[str] | None = None) -> dict[str, Any]:
    optimizer = config.get("optimizer", {})
    resolved_seed = int(optimizer.get("seed", 1)) if seed is None else int(seed)
    resolved_effort = str(effort or optimizer.get("effort", "balanced")).lower()
    if resolved_effort not in ("quick", "balanced", "best"):
        raise ValueError(f"effort must be 'quick', 'balanced' or 'best'; got {resolved_effort!r}")
    knobs = _resolve_effort_knobs(optimizer, resolved_effort, accept, max_iterations, time_budget_s)
    resolved_accept = knobs["accept"]
    if resolved_accept not in ("greedy", "sa"):
        raise ValueError(f"accept must be 'greedy' or 'sa'; got {resolved_accept!r}")

    # Phase 7.14: validate the connector exclusion list against the REAL board
    # before anything else happens. `validate_connector_exclusions` raises on an
    # unresolved name and lists every detected ref - the plan's loud-abort
    # contract, and the reason it exists: a typo'd exclusion that was quietly
    # dropped would leave a connector the user meant to protect eligible for a
    # swap proposal. Only worth the scan when the feature is actually on.
    pin_swap = _pin_swap_config(config)
    resolved_exclusions: list[str] = []
    if pin_swap["enabled"] and pin_swap_exclusions:
        resolved_exclusions = _pcb.validate_connector_exclusions(
            project_path, list(pin_swap_exclusions),
            ref_prefixes=pin_swap["ref_prefixes"])["resolved_exclusions"]

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
        "effort": resolved_effort,
        "accept": resolved_accept,
        "rng_state": _rng_state_to_json(rng),
        "iteration": 0,
        "elapsed_s": 0.0,
        "max_iterations": int(knobs["max_iterations"]),
        "time_budget_s": float(knobs["time_budget_s"]),
        "worst_k": int(optimizer.get("worst_k", 5)),
        "convergence_delta": float(optimizer.get("convergence_delta", 0.5)),
        "temperature": float(optimizer.get("sa_initial_temp", 50.0)),
        "sa_cooling": float(optimizer.get("sa_cooling", 0.9)),
        # Phase 7.15: plateau-stopping bookkeeping. `productive_improvements`
        # only ever records ACCEPTED moves that genuinely lowered the score
        # (see `_commit_choice`) - a rejected/no-op iteration says nothing
        # about the pace of genuine improvement, so it is excluded from both
        # the reference and trailing rate, per the plan.
        "plateau_window": int(optimizer.get("plateau_window", 3)),
        "plateau_slope_ratio": float(optimizer.get("plateau_slope_ratio", 0.1)),
        "productive_improvements": [],
        "plateau_reference_rate": None,
        "plateau_trailing_rate": None,
        "initial_score": initial,
        "current_score": initial,
        "best_score": initial,
        "score_curve": [initial["total"]],
        "moves": [],
        "applied": False,
        "stop_reason": None,
        "ai_decisions": _ai_decision_config(config),
        # Phase 7.14: `pin_swap_examined` is the "ask once" ledger - a pair the
        # advisor has already priced and put to the user is never re-proposed,
        # whichever way they answered, so neither a decline nor an applied swap
        # can wedge the session in a loop of the same question.
        "pin_swap": pin_swap,
        "pin_swap_exclusions": resolved_exclusions,
        "pin_swap_examined": [],
        "pin_swap_reports": [],
        "pending_decision": None,
        "decision_log": [],
        "pauses_used": 0,
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
    effort: str | None = None,
    pin_swap_exclusions: list[str] | None = None,
    write: bool = False,
    allow_while_open: bool = False,
) -> dict[str, Any]:
    """Phase 7.6/7.15 - run a BOUNDED chunk of whole-board optimization.

    Omit `session_id` to start a new session (which snapshots the project into
    a private scratch directory and scores it); pass one to resume an existing
    session exactly where it stopped. Each call runs at most
    `max_iterations_per_call` iterations or `max_seconds` of wall clock,
    whichever binds first, and returns the session's state:

      `running`           - budget for THIS call is spent, more work remains.
      `converged`         - the best available move improved the score by less
                            than `optimizer.convergence_delta` (a single
                            degenerate iteration - the floor), OR (7.15) the
                            trailing-window mean improvement rate has fallen
                            below `plateau_slope_ratio` x its reference rate
                            (the pace of genuine improvement has slowed to a
                            fraction of its initial pace - `stop_reason`
                            distinguishes the two: `"convergence_delta"` vs.
                            `"plateau"`).
      `budget_exhausted`  - the SESSION's `max_iterations` / `time_budget_s`
                            ran out first.
      `awaiting_decision` - (7.7) the top two candidates are within
                            `optimizer.ai_decisions.min_score_spread`, so the
                            cost model cannot separate them; `pending_decision`
                            carries the option list. Answer with
                            `decide_kicad_route`, or just call this tool again
                            to defer to the best-scored option. (7.14) OR the
                            pin-swap advisor found a connector pin swap worth
                            `pin_swap.min_gain` board-score points. THAT pause
                            is mandatory and is not gated by `ai_decisions` at
                            all: the tool can never apply a pin swap itself -
                            only a schematic edit + netlist re-export can - so
                            a clear winner is exactly the case that must be
                            escalated. Answer `opt1` to decline, or `opt2` to
                            report that you made the change (the session then
                            re-syncs against the board's new pad-net
                            assignment and continues). Off unless
                            `pin_swap.enabled` is true (default false).

    Each iteration ranks every cost contributor worst-first (routed nets at
    their trace cost, unrouted nets at `unrouted_penalty` x their missing
    connections), takes `optimizer.worst_k` of them, generates up to six
    candidate moves (rip-up+reroute, bundle
    reroute, layer swap, stitching via, create plane, modify plane), scores
    every candidate on its OWN private copy of the current board state, and
    accepts per `optimizer.accept` (`greedy` = strict improvements only; `sa` =
    simulated annealing, worse moves accepted with probability exp(-dS/T),
    T *= `sa_cooling` each iteration). `seed` makes the whole run reproducible.

    `effort` (7.15, new session only - a resumed session keeps whatever effort
    it started with) is `"quick" | "balanced" | "best"`, each a DEFAULT bundle
    of the OTHER knobs (an explicit `accept`/`max_iterations`/`time_budget_s`
    argument here still wins over the preset): `quick` = one pass + cheap
    cleanup (`max_iterations=5`, `accept="greedy"`); `balanced` (the default)
    = today's `optimizer.*` settings, unchanged; `best` = `accept="sa"` and an
    8-hour ("overnight") `time_budget_s`. Omit to read `optimizer.effort` from
    `pcb_settings.json` (also `"balanced"` by default). NOTE: the plan's
    "replicas" knob for quick/best (`autorouter.cpu.replicas`) is not
    consumed by anything in this codebase yet, so no preset here sets it -
    see `_EFFORT_PRESETS`'s docstring-comment for the honest scope-down.

    `write=False` (the default) NEVER touches the real board - not on the first
    call, not on the last. `write=True` applies the session's final accepted
    board state (copper, vias and zones together, as one consistent state - not
    a replay of individual moves) onto the real board and merges the scratch's
    `autorouter_owned` bookkeeping into the real board-local state, so
    `unroute_nets` still undoes every piece of it. It refuses if the session is
    still `running` or `awaiting_decision` (there is no "final state" yet) or if
    the real board file changed since the session started. As with every writer
    here, KiCad must refill zones and re-run DRC afterward.

    `pin_swap_exclusions` (7.14, new session only) names connectors the pin-swap
    advisor must never propose a swap on. Every name is resolved against
    `detect_kicad_connectors` at session creation and an unresolved one RAISES,
    listing the board's detected refs - a typo must not silently leave a
    connector unprotected. Ignored when `pin_swap.enabled` is false.

    HARD GUARANTEE (7.14): no path through this function - including
    `write=True` - ever writes the schematic, writes the real `.net` file, or
    changes which net a real pad belongs to. `write=True` asserts the last of
    those explicitly by comparing the scratch board's pad-net map against the
    real board's before copying anything, and refuses rather than writing a
    board whose pad assignment it does not recognize.
    """
    config = _pcb.load_pcb_settings(project_path)["config"]
    data, sessions = _load_sessions(project_path)

    if session_id is None:
        session = _new_session(project_path, config, seed, accept, max_iterations,
                               time_budget_s, effort, pin_swap_exclusions)
        sessions[session["session_id"]] = session
    else:
        if session_id not in sessions:
            raise KeyError(f"No optimizer session {session_id!r} on this board")
        session = _migrate_session(sessions[session_id], config)

    notes: list[str] = []
    # Timeout-to-defer: an abandoned pause must never wedge a session, so a
    # plain resume answers the outstanding decision with the optimizer's own
    # best-scored option and carries on. `decide_route` is the way to answer it
    # with anything ELSE - not the way to answer it at all.
    if session["state"] == "awaiting_decision" and not session["applied"]:
        pending = session["pending_decision"]
        outcome = _resolve_pending(session, "defer", None, auto=True,
                                   auto_reason="resume_without_decision")
        notes.append(f"Decision {pending['decision_id']} was resumed without an answer; "
                     f"deferred to {outcome['resolved_choice']} ({pending['default_choice']} "
                     "was the optimizer's default).")

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
        "notes": notes,
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

        # 7.14: the pin-swap advisor runs FIRST, before a single unit of RNG is
        # consumed, and pauses unconditionally on a swap worth `min_gain` - see
        # the Phase 7.14 section header for why a clear winner is exactly the
        # case that must be escalated rather than auto-taken. With
        # `pin_swap.enabled` false (the default) `_pin_swap_gate` returns on its
        # first line, so everything below is reached in the identical state the
        # pre-7.14 code reached it in.
        try:
            pending_swap = _pin_swap_gate(session, scratch, trial_root, session["iteration"] + 1)
        except Exception as exc:
            # An advisory feature must never be able to kill a routing session.
            # Recorded rather than swallowed, though - "the advisor could not
            # run on this board" is information, and a silent None here would
            # be indistinguishable from "there was nothing to propose".
            pending_swap = None
            session["pin_swap_reports"].append({
                "iteration": session["iteration"] + 1, "key": None, "priced": False,
                "proposed": False, "error": f"{type(exc).__name__}: {exc}",
            })
        if pending_swap is not None:
            session["pending_decision"] = pending_swap
            session["state"] = "awaiting_decision"
            session["stop_reason"] = "awaiting_decision"
            # `pauses_used` is NOT incremented: it budgets the 7.7 AI escalations
            # (`max_pauses_per_run`), and a pin swap is a question for the human
            # that no budget may suppress. Nor is the iteration counter advanced
            # or the temperature cooled - no move was made, and the interrupted
            # iteration runs unchanged on the next chunk.
            session["elapsed_s"] += time.monotonic() - iteration_started
            session["rng_state"] = _rng_state_to_json(rng)
            swap = pending_swap["pin_swap"]
            notes.append(
                f"Paused for pin-swap decision {pending_swap['decision_id']}: swapping "
                f"{swap['ref']} pins {swap['pad_a']}/{swap['pad_b']} would gain "
                f"{swap['gain']} board-score points (>= min_gain {swap['min_gain']}). This tool "
                "CANNOT make that change - only a schematic edit + netlist re-export can. Answer "
                "with decide_kicad_route (opt1 = decline, opt2 = I made the change)."
            )
            break

        worst = _ranked_nets(scratch)[: session["worst_k"]]
        candidates = _generate_candidates(scratch, worst, rng)
        evaluated = [_evaluate_candidate(scratch, trial_root, c, session["iteration"] + 1, i)
                     for i, c in enumerate(candidates)]
        usable = [e for e in evaluated if e["applicable"]]
        usable.sort(key=lambda e: (e["score"]["total"], e["type"], e["summary"]))
        current_total = session["current_score"]["total"]

        if not usable:
            session["iteration"] += 1
            done_this_call += 1
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

        # 7.7: the ONE branch point this phase adds. Everything below the pause
        # check is byte-for-byte the 7.6 behaviour, reached whenever
        # `_pause_check` declines to escalate.
        options = _option_records(usable, current_total)
        decision_id = f"{session['session_id'][:8]}-i{session['iteration'] + 1}"
        decision_type = _decision_type_for(session, usable[0])
        auto_reason = _pause_check(session, options, decision_type)

        if auto_reason is None:
            _park_pending_options(session, options)
            session["pending_decision"] = {
                "decision_id": decision_id,
                "iteration": session["iteration"] + 1,
                "decision_type": decision_type,
                "default_choice": options[0]["id"],
                "score_spread": options[1]["score_total"] - options[0]["score_total"],
                "min_score_spread": session["ai_decisions"]["min_score_spread"],
                "current_score": current_total,
                "candidates_evaluated": len(evaluated),
                "pending_dir": str(Path(scratch.parent) / f"{scratch.name}_pending"),
                "options": options,
            }
            session["pauses_used"] += 1
            session["state"] = "awaiting_decision"
            session["stop_reason"] = "awaiting_decision"
            # Only the timing half of `_finish_iteration`: the iteration has NOT
            # completed, so cooling the temperature or advancing the counter here
            # would make a paused run diverge from an unpaused one.
            session["elapsed_s"] += time.monotonic() - iteration_started
            session["rng_state"] = _rng_state_to_json(rng)
            notes.append(f"Paused for decision {decision_id} ({decision_type}); "
                         "answer with decide_kicad_route, or call optimize_kicad_board "
                         "again to defer to the best-scored option.")
            break

        done_this_call += 1
        improvement = _commit_choice(
            session, options[0], options, rng,
            _log_entry(decision_id, session["iteration"] + 1, decision_type, options,
                       options[0]["id"], options[0]["id"], None, True, auto_reason),
            len(evaluated))
        shutil.rmtree(trial_root, ignore_errors=True)
        trial_root.mkdir(exist_ok=True)

        # Converged when the best available move cannot buy `convergence_delta`
        # worth of score. Under `greedy` a rejected move means nothing improved,
        # which is the same condition - so both policies stop here honestly.
        # `convergence_delta` is checked FIRST and unconditionally (it is a
        # floor for a single degenerate iteration); the Phase 7.15 plateau
        # rule is evaluated whenever that floor didn't already stop the run,
        # so a genuinely-slowed-but-still-above-floor pace can also converge.
        plateau_reason = _plateau_check(session)
        if improvement < session["convergence_delta"]:
            session["state"] = "converged"
            session["stop_reason"] = "convergence_delta"
            _finish_iteration(session, rng, iteration_started)
            break
        if plateau_reason:
            session["state"] = "converged"
            session["stop_reason"] = plateau_reason
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
    if session["state"] in ("running", "awaiting_decision"):
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

    # Phase 7.14 SAFETY GATE. The scratch board is copied over the real board
    # wholesale, so any difference in PAD NETS between the two would be a
    # silent netlist edit - precisely the thing this tool must never do, and
    # the one failure mode a pin-swap feature could plausibly introduce (a
    # trial board escaping into the scratch). This asserts the property
    # directly rather than trusting that no code path promotes a swap trial:
    # the two maps must be identical, or nothing is written.
    #
    # It is deliberately not limited to connector pads or to sessions that ran
    # the advisor - ANY divergence, from any cause, is a reason to refuse.
    real_pad_nets = _board_pad_net_map(project_path)
    scratch_pad_nets = _board_pad_net_map(scratch)
    if real_pad_nets != scratch_pad_nets:
        differing = sorted(
            key for key in set(real_pad_nets) | set(scratch_pad_nets)
            if real_pad_nets.get(key) != scratch_pad_nets.get(key)
        )
        return False, (
            "REFUSING TO WRITE: the session's board disagrees with the real board about which "
            f"net {len(differing)} pad(s) belong to ({differing[:10]}). This tool never changes a "
            "pad's net - only a schematic edit + netlist re-export may - so a divergence here "
            "means the session state is not safe to apply. Start a new session against the "
            "current board."
        )

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


# =========================================================================== #
# Phase 7.5.6 - the plane stitching pass (`run_stitching_pass`, MCP
# `run_kicad_stitching_pass`) and its undo (`remove_stitching_vias`, MCP
# `remove_kicad_stitching_vias`)
#
# The plan's own ordering contract: stitching is the FINAL copper pass, run
# only after routing and plane creation/moves have converged - it is not one
# of the 7.6 optimizer's per-iteration candidate moves (move (d) already
# covers a single opportunistic island-rescue via inside the score loop; this
# is a dedicated, one-shot sweep run once the board is otherwise settled).
#
# Three ordered steps, each reusing an already-landed read/analysis tool
# rather than inventing new geometry:
#   1. Island rescue        - `audit_plane_islands`'s own `suggested_stitching_
#                              via` per costed island/orphan (exact reuse of
#                              the optimizer's move (d) targeting, but applied
#                              to EVERY island in one pass, not one per
#                              iteration).
#   2. Return-path stitching- vias near `classify_critical_nets` nets, on the
#                              power/ground PLANE (not the signal net's own
#                              layer), at `stitching.near_high_speed_pitch_mm`
#                              within `stitching.near_high_speed_mm` of the
#                              trace.
#   3. General stitching     - a grid fill of the remaining plane area toward
#                              `stitching.target_spacing_mm`.
#
# Every via this pass places goes through `_place_stitching_via(..., stitching
# =True)`, so it is `autorouter_owned` AND carries the `"stitching": True` tag
# `remove_stitching_vias` filters on - never touching an ordinary routing via,
# a hand-placed via, or the optimizer's own untagged move-(d) vias.
#
# SESSION-CONTRACT NOTE (not enforced here - see the tool docstrings below):
# the plan asks that before routing/optimizing in an area containing
# stitching vias (owned or foreign), the calling AI session ask the user
# whether to remove them first. That is a convention for the SESSION to
# follow (the same kind of documented-not-enforced contract as `allow_hand_
# copper_ripup` on `route_nets`/`route_board`), not something a Python
# function can verify about its caller's future intentions - removed
# stitching copper is simply re-placed by the next `run_stitching_pass`
# anyway, so nothing is lost by asking.
# =========================================================================== #

# Implementation cost bound (same idea as `_MAX_CANDIDATES_PER_ITERATION`): the
# most general-stitching vias one pass will place per zone/layer. A dense
# ground pour's interior area can be huge, so an unbounded grid fill could
# place thousands of vias in one call; this is an engineering guard against
# that, not a `pcb_settings.json` design knob.
_MAX_GENERAL_STITCHING_VIAS_PER_ZONE_LAYER = 60


def _net_owning_zone_polygons(project_path: str | Path) -> list[dict[str, Any]]:
    """Board-level, net-owning, non-keepout zones with a real (>=3-point)
    outline - the same filter `_zone_island_model` applies before treating a
    zone as costable. Returns the zones as parsed (`uuid`/`name`/`net`/
    `layers`/`polygon`/...), for containment tests against the zone's own
    DRAWN outline (not the per-island fill decomposition step 1 already
    handles precisely via `suggested_stitching_via`) - a documented scope
    simplification for steps 2/3: most of a real pour's outline area IS its
    mainland, so testing against the outline is a reasonable proxy without
    re-running the full 7.5.2 fill/flood-fill estimation for every candidate
    point."""
    board_path, _, _ = _pcb._resolve_project_path(project_path)
    zones = _r._parse_zones_cached(board_path)
    return [
        z for z in zones
        if z.get("net") and not z.get("keepout") and len(z.get("polygon") or []) >= 3
    ]


def _is_power_ground_net(net_name: str, settings: dict[str, Any]) -> bool:
    """A stitching via belongs on the RETURN-PATH plane, not the signal net's
    own layer - reuses the exact `layer_purpose.power_net_patterns` regex list
    `classify_critical_nets`/`propose_plane` already use to recognize a
    power/ground net by name, rather than inventing a second pattern list."""
    patterns = settings.get("layer_purpose", {}).get("power_net_patterns", [])
    return any(re.search(pat, net_name) for pat in patterns)


def _offset_points_along_segment(
    seg: dict[str, Any], pitch_mm: float, offset_mm: float,
) -> list[tuple[float, float]]:
    """Candidate return-path stitching-via positions along one routed track
    segment: points spaced `pitch_mm` apart along the centerline, each pushed
    `offset_mm` to BOTH sides (perpendicular to the trace direction). The
    caller filters these by plane-outline containment, so a point that lands
    off the plane (or on the wrong side, off-board, etc.) is simply dropped -
    never faked into a placement."""
    if pitch_mm <= 0:
        return []
    x0, y0 = seg["start"]["x"], seg["start"]["y"]
    x1, y1 = seg["end"]["x"], seg["end"]["y"]
    length = math.hypot(x1 - x0, y1 - y0)
    if length <= 0:
        return []
    ux, uy = (x1 - x0) / length, (y1 - y0) / length
    perp_x, perp_y = -uy, ux  # 90-degree rotation of the trace direction

    points: list[tuple[float, float]] = []
    steps = max(1, int(length // pitch_mm) + 1)
    for i in range(steps + 1):
        t = min(i * pitch_mm, length)
        cx, cy = x0 + ux * t, y0 + uy * t
        for sign in (1.0, -1.0):
            points.append((cx + perp_x * offset_mm * sign, cy + perp_y * offset_mm * sign))
    return points


def run_stitching_pass(project_path: str | Path, write: bool = False) -> dict[str, Any]:
    """Phase 7.5.6 - the plane stitching pass (MCP tool `run_kicad_stitching_
    pass`). Intended to run LAST, once routing and plane creation/moves have
    converged (after `route_kicad_board`/`optimize_kicad_board`), never mid-
    routing - a stitching via placed early would just become an obstacle/
    congestion source for routing that comes after it.

    Three ordered steps (`stitching.enabled: false` makes this a no-op, still
    reporting `enabled: false` and empty plans):
    1. Island rescue - one via per costed island/orphan `audit_kicad_plane_
       islands` already reports, at its own `suggested_stitching_via.position`
       (exactly the optimizer's move (d) target, applied to every island in
       one sweep rather than one per iteration).
    2. Return-path stitching - vias near `classify_kicad_critical_nets`
       nets' OWN routed copper, placed on same-layer power/ground PLANES
       (`stitching.near_high_speed_pitch_mm` apart, within `stitching.
       near_high_speed_mm` of the trace), wherever a candidate point actually
       lands inside that plane's own drawn outline.
    3. General stitching - a grid fill of each power/ground plane's outline
       toward `stitching.target_spacing_mm`, skipping any point already
       covered by a step 1/2 via at that spacing.

    Every via is placed via `_place_stitching_via(..., stitching=True)`: it is
    `autorouter_owned` (undoable via `unroute_kicad_nets`) AND tagged
    `"stitching": True` in the board-local record, so `remove_kicad_
    stitching_vias` can target exactly the vias this pass placed - never an
    ordinary routing via, a hand-placed via, or the optimizer's own untracked
    move-(d) via (which is not tagged, by design - see `_place_stitching_via`).

    write=False (default) previews the full plan (every via's net/zone/layer/
    position and, for island rescue, its projected cost) without touching the
    board. write=True places every planned via for real and additionally
    returns each one's uuid; refill zones + re-run DRC in KiCad afterward,
    same as every other copper writer here.

    SESSION CONVENTION (documented, not enforced): before routing or
    optimizing in an area that already contains stitching vias (owned or
    foreign), the calling session should ask the user whether to remove them
    first (see `remove_kicad_stitching_vias`) - this function cannot know
    what the session plans to do next, so it does not attempt to gate on it;
    removed stitching copper is simply re-placed by the next run of this pass.
    """
    settings = _pcb.load_pcb_settings(project_path)["config"]
    st_cfg = settings.get("stitching", {}) or {}
    enabled = bool(st_cfg.get("enabled", True))
    target_spacing_mm = float(st_cfg.get("target_spacing_mm", 5.0))
    near_hs_mm = float(st_cfg.get("near_high_speed_mm", 1.0))
    near_hs_pitch_mm = float(st_cfg.get("near_high_speed_pitch_mm", 2.0))

    board_path, _, _ = _pcb._resolve_project_path(project_path)
    result: dict[str, Any] = {
        "board_path": str(board_path),
        "write": write,
        "enabled": enabled,
        "island_rescue": [],
        "return_path": [],
        "general": [],
        "planned_count": 0,
        "placed_count": 0,
    }
    if not enabled:
        return result

    planned: list[dict[str, Any]] = []
    # (net, layer) -> positions already claimed this pass, so step 3 (and step
    # 2's own pitch spacing) never stacks a via on top of one step 1/2 already
    # placed there.
    claimed: dict[tuple[str, str], list[tuple[float, float]]] = {}

    def _claim(net: str, layer: str, x: float, y: float, min_sep: float) -> bool:
        key = (net, layer)
        existing = claimed.setdefault(key, [])
        if any(math.hypot(x - ex, y - ey) < min_sep for ex, ey in existing):
            return False
        existing.append((x, y))
        return True

    # --- Step 1: island rescue --------------------------------------------
    audit = _r.audit_plane_islands(project_path)
    for zone in audit["zones"]:
        for layer in zone["layers"]:
            for comp in layer["components"]:
                suggestion = comp.get("suggested_stitching_via")
                if comp["role"] not in ("island", "orphan") or not suggestion:
                    continue
                x, y = suggestion["position"]["x"], suggestion["position"]["y"]
                _claim(zone["net"], layer["layer"], x, y, 1e-6)
                entry = {
                    "kind": "island_rescue",
                    "net": zone["net"],
                    "zone": zone["name"],
                    "layer": layer["layer"],
                    "x": x, "y": y,
                    "current_cost": comp["cost"],
                    "projected_cost": suggestion["projected_cost"],
                }
                planned.append(entry)
                result["island_rescue"].append(entry)

    # --- Step 2: return-path stitching near critical/high-speed nets ------
    zones = _net_owning_zone_polygons(project_path)
    power_zones = [z for z in zones if _is_power_ground_net(z["net"], settings)]

    try:
        critical = _pcb.classify_critical_nets(project_path)
    except Exception:  # pragma: no cover - defensive, matches other callers
        critical = {"critical_nets": []}
    critical_net_names = sorted({rec["net"] for rec in critical.get("critical_nets", [])})

    tracks = _pcb._parse_tracks_cached(board_path)
    segments_by_net: dict[str, list[dict[str, Any]]] = {}
    for seg in tracks["segments"]:
        if seg.get("net"):
            segments_by_net.setdefault(seg["net"], []).append(seg)

    for net_name in critical_net_names:
        for seg in segments_by_net.get(net_name, []):
            layer = seg["layer"]
            for zone in power_zones:
                if layer not in zone.get("layers", []):
                    continue
                for (cx, cy) in _offset_points_along_segment(seg, near_hs_pitch_mm, near_hs_mm):
                    if not _r._point_in_poly(cx, cy, zone["polygon"]):
                        continue
                    if not _claim(zone["net"], layer, cx, cy, near_hs_pitch_mm * 0.9):
                        continue
                    entry = {
                        "kind": "return_path",
                        "net": zone["net"],
                        "zone": zone["name"],
                        "layer": layer,
                        "x": round(cx, 4), "y": round(cy, 4),
                        "near_net": net_name,
                    }
                    planned.append(entry)
                    result["return_path"].append(entry)

    # --- Step 3: general stitching toward target_spacing_mm ---------------
    if target_spacing_mm > 0:
        for zone in power_zones:
            poly = zone["polygon"]
            xs = [p[0] for p in poly]
            ys = [p[1] for p in poly]
            minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
            for layer in zone.get("layers", []):
                placed_here = 0
                y = miny
                while y <= maxy and placed_here < _MAX_GENERAL_STITCHING_VIAS_PER_ZONE_LAYER:
                    x = minx
                    while x <= maxx and placed_here < _MAX_GENERAL_STITCHING_VIAS_PER_ZONE_LAYER:
                        if _r._point_in_poly(x, y, poly) and _claim(
                            zone["net"], layer, x, y, target_spacing_mm,
                        ):
                            entry = {
                                "kind": "general",
                                "net": zone["net"],
                                "zone": zone["name"],
                                "layer": layer,
                                "x": round(x, 4), "y": round(y, 4),
                            }
                            planned.append(entry)
                            result["general"].append(entry)
                            placed_here += 1
                        x += target_spacing_mm
                    y += target_spacing_mm

    result["planned_count"] = len(planned)
    if write:
        placed_records = []
        for entry in planned:
            placed = _place_stitching_via(project_path, entry["net"], entry["x"], entry["y"], stitching=True)
            placed_records.append({**entry, "uuid": placed["uuid"]})
        result["placed"] = placed_records
        result["placed_count"] = len(placed_records)
    return result


def _point_in_area(x: float, y: float, area: dict[str, Any] | None) -> bool:
    """`area` is either omitted (everywhere matches), a rect `{"x_min",
    "x_max", "y_min", "y_max"}` (any bound may be omitted for an open side),
    or a polygon `{"points": [[x, y], ...]}` tested with the same even-odd
    `_point_in_poly` the router's own island/zone containment tests use."""
    if area is None:
        return True
    if "points" in area:
        poly = [(float(p[0]), float(p[1])) for p in area["points"]]
        return _r._point_in_poly(x, y, poly)
    xmin = float(area.get("x_min", -math.inf))
    xmax = float(area.get("x_max", math.inf))
    ymin = float(area.get("y_min", -math.inf))
    ymax = float(area.get("y_max", math.inf))
    return xmin <= x <= xmax and ymin <= y <= ymax


def remove_stitching_vias(
    project_path: str | Path,
    area: dict[str, Any] | None = None,
    write: bool = False,
    include_foreign: bool = False,
    allow_while_open: bool = False,
) -> dict[str, Any]:
    """Undo for `run_stitching_pass` (MCP tool `remove_kicad_stitching_vias`).
    Deletes ONLY autorouter-owned vias tagged `"stitching": True` in the
    board-local `autorouter_owned["records"]` - never an ordinary routing via,
    never a hand-placed via, and never the optimizer's own untagged move-(d)
    stitching via (a deliberate scope line: that one is scored and tracked by
    an `optimize_kicad_board` session, not this pass's bookkeeping).

    `area` restricts the deletion to a region: a rect `{"x_min", "x_max",
    "y_min", "y_max"}` (any bound omittable) or a polygon `{"points": [[x,
    y], ...]}`; omit for the whole board. write=False (default) previews the
    uuids that would be removed without touching the board.

    `include_foreign=True` additionally LISTS (never deletes) every OTHER via
    in the resolved area that this tool does not own - reusing this
    codebase's existing `get_project_track_inventory` notion of a "free"
    via (`net == ""`, an unconnected stitching/mounting via) plus an
    "oversized" flag (more than 3x the Default netclass via diameter) so a
    real board's already-present freestanding vias (kiln has 3 known free/
    oversized ones) surface for a human to confirm removal one at a time,
    exactly as those tools already characterize them. This is NOT a full
    connectivity trace (it does not prove a same-net via has no track
    soldered to it) - it is the same free/oversized heuristic this codebase
    already uses elsewhere, applied here as an honest, cheap first pass.

    SESSION CONVENTION (documented, not enforced): the plan asks that before
    routing or optimizing in an area containing stitching vias (owned or
    foreign), the calling AI session ask the user whether to remove them
    first - the same kind of session-level contract as `route_kicad_nets`'s
    `allow_hand_copper_ripup` opt-in. This function does the deletion asked
    of it; the "ask first" step is the calling session's responsibility, not
    something enforceable from inside a single tool call.
    """
    board_path, _, _ = _pcb._resolve_project_path(project_path)
    state = _pcb.load_board_local(project_path)
    data = state["data"]
    owned = data.get("autorouter_owned", {}) or {}
    records = owned.get("records", []) or []

    stitching_records = [
        rec for rec in records
        if rec.get("kind") == "via" and rec.get("stitching")
        and _point_in_area(rec.get("x", 0.0), rec.get("y", 0.0), area)
    ]
    remove_uuids = {rec["uuid"] for rec in stitching_records}

    removed = 0
    written = False
    if write and remove_uuids:
        _pcb._check_not_locked_by_editor(board_path, allow_while_open)
        text = _pcb._read_text(board_path)
        text, removed = _r._delete_blocks_by_uuid(text, remove_uuids)
        with board_path.open("w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        _pcb._invalidate_board_cache(board_path)
        via_set = set(owned.get("vias", []) or [])
        owned["vias"] = [u for u in via_set if u not in remove_uuids]
        owned["records"] = [rec for rec in records if rec["uuid"] not in remove_uuids]
        _pcb.save_board_local(project_path, data)
        written = True

    foreign_vias: list[dict[str, Any]] = []
    if include_foreign:
        tracks = _pcb._parse_tracks_cached(board_path)
        owned_via_uuids = set(owned.get("vias", []) or [])
        project_cfg = _pcb.load_pcb_settings(project_path)["config"]
        board_path2, project_file, _ = _pcb._resolve_project_path(project_path)
        existing_netclasses: list[dict[str, Any]] = []
        if project_file.exists():
            try:
                pro_data = json.loads(project_file.read_text(encoding="utf-8"))
                existing_netclasses = pro_data.get("net_settings", {}).get("classes", [])
            except (json.JSONDecodeError, OSError):
                existing_netclasses = []
        default_via_diameter = next(
            (float(c["via_diameter"]) for c in existing_netclasses
             if c.get("name") == "Default" and _pcb._is_number(str(c.get("via_diameter", "")))),
            None,
        )
        for via in tracks["vias"]:
            if via["uuid"] in owned_via_uuids:
                continue
            x, y = via["at"]["x"], via["at"]["y"]
            if not _point_in_area(x, y, area):
                continue
            is_free = via["net"] == ""
            is_oversized = default_via_diameter is not None and via["size"] > default_via_diameter * 3
            foreign_vias.append({
                "uuid": via["uuid"], "net": via["net"], "x": x, "y": y,
                "size": via["size"], "drill": via["drill"],
                "free": is_free, "oversized": is_oversized,
            })

    return {
        "board_path": str(board_path),
        "area": area,
        "write": write,
        "written": written,
        "candidates": len(remove_uuids),
        "removed": removed,
        "removed_uuids": sorted(remove_uuids),
        "include_foreign": include_foreign,
        "foreign_vias": foreign_vias,
    }
