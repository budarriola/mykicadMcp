"""Phase 7.14 - the pin-swap advisor: the optimizer's seventh move, and the
only one this codebase can never apply.

The fixture is a deliberately rigged four-pin connector. `J1` sits on the left
with its two SIGNAL pins at opposite ends of the connector body, and each
signal net's only other pad is on the FAR side of the board, diagonally
opposite its own connector pin. Routing them as wired costs two long diagonals;
routing them swapped costs two short horizontals. The swap is therefore worth a
large, easily-measured number of board-score points, which is what lets these
tests pin the threshold behaviour exactly rather than hoping the router
produces a difference.

`J1`'s other two pins carry GND and +3V3, and their remote pads are placed so
that swapping THEM would look attractive to the same estimator - which is the
only way to prove the power/ground exclusion is doing work rather than being
vacuously satisfied.

The safety rule these tests exist to defend: this tool never edits a schematic,
never edits the real `.net` file, and never changes which net a real pad
belongs to. Everything else here is detail.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import kicad_optimizer_tool as o
import kicad_pcb_tool as k

from synthetic_board import write_critical_nets_project


# --- fixture ---------------------------------------------------------------

# Pads carry ABSOLUTE-relevant offsets from their footprint origin; the two
# signal pins are 40 mm apart so the diagonal-vs-horizontal difference is far
# larger than any routing noise.
_COMPONENTS = [
    {"ref": "J1", "footprint": "Connector_JST:JST_XH_B4B-XH-AM_1x04", "x": 10.0, "y": 20.0,
     "pads": [("1", 0.0, -20.0, 1.0, 1.0, "SIG_A"),     # abs (10, 0)
              ("2", 0.0, 20.0, 1.0, 1.0, "SIG_B"),      # abs (10, 40)
              ("3", 2.0, -10.0, 1.0, 1.0, "GND"),       # abs (12, 10)
              ("4", 2.0, 10.0, 1.0, 1.0, "+3V3")]},     # abs (12, 30)
    # SIG_A's far pad is level with J1 pin 2, and SIG_B's with J1 pin 1: as
    # wired, both nets cross the board diagonally.
    {"ref": "U1", "footprint": "synthetic:SOT", "x": 60.0, "y": 40.0,
     "pads": [("1", 0.0, 0.0, 1.0, 1.0, "SIG_A")]},
    {"ref": "U2", "footprint": "synthetic:SOT", "x": 60.0, "y": 0.0,
     "pads": [("1", 0.0, 0.0, 1.0, 1.0, "SIG_B")]},
    # The same crossed arrangement for the supply pins - so a swap of pins 3/4
    # would score just as attractively if `_net_kind` did not veto it.
    {"ref": "U3", "footprint": "synthetic:SOT", "x": 62.0, "y": 40.0,
     "pads": [("1", 0.0, 0.0, 1.0, 1.0, "GND")]},
    {"ref": "U4", "footprint": "synthetic:SOT", "x": 62.0, "y": 0.0,
     "pads": [("1", 0.0, 0.0, 1.0, 1.0, "+3V3")]},
]


def _pin_swap_project(directory: Path, **pin_swap) -> Path:
    """The rigged board plus a `pcb_settings.json` carrying `pin_swap`
    overrides. Also writes a stub `.kicad_sch`: several tests assert this tool
    never touches a schematic, and an assertion about a file that does not
    exist proves nothing."""
    directory = Path(directory)
    write_critical_nets_project(directory, "pinswap", _COMPONENTS)
    (directory / "pinswap.kicad_sch").write_text(
        '(kicad_sch (version 20231120) (generator "test"))\n', encoding="utf-8")
    if pin_swap:
        (directory / "pcb_settings.json").write_text(
            json.dumps({"pin_swap": pin_swap}, indent=2) + "\n", encoding="utf-8")
    return directory


def _no_ai_pauses(project: Path) -> None:
    """Make the 7.7 spread gate incapable of pausing: disabled outright AND a
    zero spread threshold. Any pause a test still sees after this is a pin-swap
    pause, escalated on its own authority."""
    path = Path(project) / "pcb_settings.json"
    config = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    config.setdefault("optimizer", {})["ai_decisions"] = {
        "enabled": False, "min_score_spread": 0.0, "max_pauses_per_run": 0,
        "decision_types": [],
    }
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


def _run_to_pin_swap_pause(project: Path, limit: int = 6, **kwargs) -> dict:
    report = o.optimize_board(project, max_iterations_per_call=1, seed=3, **kwargs)
    calls = 0
    while report["state"] == "running" and calls < limit:
        report = o.optimize_board(project, session_id=report["session_id"],
                                  max_iterations_per_call=1)
        calls += 1
    assert report["state"] == "awaiting_decision", (
        f"expected a pin-swap pause, got {report['state']}")
    assert report["pending_decision"]["decision_type"] == "pin_swap"
    return report


# --- 1. a genuine swap is found, priced, and ALWAYS escalated ---------------


def test_genuine_swap_pauses_regardless_of_score_spread(tmp_path: Path) -> None:
    """The headline behavioural difference from every other decision type.

    The 7.7 gate pauses only on a NEAR-TIE (spread under `min_score_spread`)
    and auto-takes a clear winner. This swap is the opposite of a near-tie -
    it is worth tens of points - and the AI gate here is switched off entirely
    with a zero threshold besides. It must pause anyway, because a clear
    winner is precisely the case only a human can act on: the tool cannot edit
    the schematic that would realize it.
    """
    project = _pin_swap_project(tmp_path, enabled=True)
    _no_ai_pauses(project)

    report = _run_to_pin_swap_pause(project)
    pending = report["pending_decision"]
    swap = pending["pin_swap"]

    assert swap["ref"] == "J1"
    assert {swap["pad_a"], swap["pad_b"]} == {"1", "2"}
    assert {swap["net_a"], swap["net_b"]} == {"SIG_A", "SIG_B"}
    # The gain is the A/B difference, and it comfortably clears the threshold.
    assert swap["gain"] >= swap["min_gain"] == 25.0
    assert swap["swap_score"]["total"] < swap["baseline_score"]["total"]
    # The pause happened on a spread far WIDER than the 7.7 threshold would
    # ever escalate - that is the whole point of this test.
    assert pending["score_spread"] > k.DEFAULT_PCB_SETTINGS["optimizer"]["ai_decisions"]["min_score_spread"]
    assert pending["min_score_spread"] is None

    # Two answerable options, decline first, and NEITHER carries a trial
    # directory to promote: there is no applied move behind a pin swap.
    assert [opt["id"] for opt in pending["options"]] == ["opt1", "opt2"]
    assert pending["default_choice"] == "opt1"
    assert [opt["type"] for opt in pending["options"]] == ["pin_swap_decline", "pin_swap_applied"]
    assert all(opt["trial_dir"] is None for opt in pending["options"])
    assert pending["pending_dir"] is None

    # `pauses_used` budgets the 7.7 AI escalations; a pin swap must not spend it.
    assert report["pauses_used"] == 0


def test_pin_swap_pause_does_not_advance_the_iteration_or_the_rng(tmp_path: Path) -> None:
    """A pin-swap pause interrupts an iteration before any copper candidate is
    generated, so declining it must leave the session in exactly the state the
    gate found it in - the interrupted iteration then runs unchanged."""
    project = _pin_swap_project(tmp_path, enabled=True)
    _no_ai_pauses(project)

    report = _run_to_pin_swap_pause(project)
    before = {"iteration": report["iteration"], "score_curve": list(report["score_curve"]),
              "temperature": report["temperature"], "moves": len(report["moves"])}
    pending = report["pending_decision"]

    answered = o.decide_route(project, report["session_id"], pending["decision_id"],
                              "opt1", rationale="test declines")
    assert answered["state"] == "running"
    assert answered["iteration"] == before["iteration"]
    assert answered["score_curve"] == before["score_curve"]
    assert answered["temperature"] == before["temperature"]
    assert len(answered["moves"]) == before["moves"]

    # Logged as a decision, but never as an accepted move - the tool applied
    # nothing, and the log must not claim otherwise.
    entry = answered["decision"]
    assert entry["decision_type"] == "pin_swap"
    assert entry["accepted"] is False
    assert entry["delta"] == 0.0
    assert entry["resolved_choice"] == "opt1"


def test_declined_swap_is_not_proposed_again(tmp_path: Path) -> None:
    """An answered pair goes into the `pin_swap_examined` ledger, so neither
    answer can wedge the session in a loop of the same question."""
    project = _pin_swap_project(tmp_path, enabled=True)
    _no_ai_pauses(project)

    report = _run_to_pin_swap_pause(project)
    key = report["pending_decision"]["pin_swap"]["key"]
    o.decide_route(project, report["session_id"], report["pending_decision"]["decision_id"], "opt1")

    report = o.optimize_board(project, session_id=report["session_id"], max_iterations_per_call=3)
    if report["state"] == "awaiting_decision":
        assert report["pending_decision"].get("pin_swap", {}).get("key") != key


def test_resume_without_answering_defers_to_decline(tmp_path: Path) -> None:
    """The timeout-to-defer path must never invent a schematic edit on the
    user's behalf, which is why `decline` is the default option."""
    project = _pin_swap_project(tmp_path, enabled=True)
    _no_ai_pauses(project)

    report = _run_to_pin_swap_pause(project)
    session_id = report["session_id"]
    report = o.optimize_board(project, session_id=session_id, max_iterations_per_call=1)

    entry = next(e for e in report["decision_log"] if e["decision_type"] == "pin_swap")
    assert entry["resolved_choice"] == "opt1"
    assert entry["auto"] is True
    assert entry["accepted"] is False


# --- 2. sub-threshold swaps are reported, never proposed --------------------


def test_sub_min_gain_swap_is_reported_but_never_escalated(tmp_path: Path) -> None:
    """"Sub-threshold swaps are reported, not proposed" is only true if the
    report is somewhere a caller can read it, so this asserts both halves: the
    priced candidate IS visible with its real gain, and the session never
    reaches `awaiting_decision` for it."""
    project = _pin_swap_project(tmp_path, enabled=True, min_gain=1.0e6)
    _no_ai_pauses(project)

    report = o.optimize_board(project, max_iterations_per_call=2, seed=3)
    while report["state"] == "running":
        report = o.optimize_board(project, session_id=report["session_id"],
                                  max_iterations_per_call=2)

    assert report["state"] != "awaiting_decision"
    assert not any(e["decision_type"] == "pin_swap" for e in report["decision_log"])

    reports = report["pin_swap_reports"]
    entry = next(r for r in reports if r["key"] == "J1:1<->2")
    assert entry["priced"] is True
    assert entry["gain"] > 0                     # the swap really is an improvement
    assert entry["gain"] < entry["min_gain"]     # just not by the threshold's standard
    assert entry["proposed"] is False


# --- 3. power/ground pins are never swap candidates -------------------------


def test_power_and_ground_pins_are_excluded_from_candidates(tmp_path: Path) -> None:
    """J1's GND/+3V3 pins are crossed exactly the way its signal pins are, so a
    purely geometric candidate generator would happily offer them. Only the
    `_net_kind` veto keeps them out."""
    project = _pin_swap_project(tmp_path, enabled=True)
    cfg = o._pin_swap_config(k.load_pcb_settings(project)["config"])

    # Sanity: the connector really does expose all four pins, so the exclusion
    # below is about net kind and not about detection missing the pads.
    detected = k.detect_connectors(project)["candidates"]
    assert [p["pad"] for p in detected[0]["pins"]] == ["1", "2", "3", "4"]

    pins = o._swappable_connector_pins(project, cfg, [])
    assert sorted(p["pad"] for p in pins) == ["1", "2"]
    assert {p["net"] for p in pins} == {"SIG_A", "SIG_B"}

    pairs = o._pin_swap_pairs(project, cfg, [])
    supply = {"GND", "+3V3"}
    assert pairs, "the signal pair should still be offered"
    assert not any(supply & {p["net_a"], p["net_b"]} for p in pairs)
    assert not any({"3", "4"} & {p["pad_a"], p["pad_b"]} for p in pairs)


def test_excluded_connector_yields_no_candidates_and_a_typo_raises(tmp_path: Path) -> None:
    """The interaction contract's loud-abort rule: an exclusion that does not
    resolve must raise and name the board's real connectors, never be dropped -
    a silently-ignored typo leaves a connector the user meant to protect wide
    open."""
    project = _pin_swap_project(tmp_path, enabled=True)
    cfg = o._pin_swap_config(k.load_pcb_settings(project)["config"])

    assert o._pin_swap_pairs(project, cfg, ["J1"]) == []
    assert o._pin_swap_pairs(project, cfg, ["j1"]) == []   # case-insensitive

    with pytest.raises(ValueError, match="J1"):
        o.optimize_board(project, max_iterations_per_call=1, pin_swap_exclusions=["J9"])

    report = o.optimize_board(project, max_iterations_per_call=1, seed=3,
                              pin_swap_exclusions=["j1"])
    assert report["pin_swap_exclusions"] == ["J1"]        # canonical board casing
    assert report["state"] != "awaiting_decision"
    assert report["pin_swap_reports"] == []


# --- 4. off by default, and provably inert when off -------------------------


def test_disabled_by_default_generates_no_pin_swap_at_all(tmp_path: Path) -> None:
    """No `pin_swap` block at all: the schema default is `enabled: false`, and
    the advisor must not so much as look at the board."""
    project = _pin_swap_project(tmp_path)
    assert k.load_pcb_settings(project)["config"]["pin_swap"]["enabled"] is False
    # Silence the ORDINARY 7.7 spread gate, so the `awaiting_decision`
    # assertion below is about the pin-swap advisor and nothing else.
    _no_ai_pauses(project)

    report = o.optimize_board(project, max_iterations_per_call=3, seed=3)
    while report["state"] == "running":
        report = o.optimize_board(project, session_id=report["session_id"],
                                  max_iterations_per_call=3)

    assert report["pin_swap"]["enabled"] is False
    assert report["pin_swap_reports"] == []
    assert not any(e["decision_type"] == "pin_swap" for e in report["decision_log"])
    assert report["state"] != "awaiting_decision"


def test_advisor_perturbs_no_copper_decision(tmp_path: Path) -> None:
    """The determinism/parity guarantee, tested the only way it can be tested
    from inside the new code: two sessions on the SAME board with the SAME
    seed, one with the advisor off and one with it on but its threshold set
    unreachably high (so it prices swaps every iteration and proposes none),
    must produce identical score curves and identical move lists.

    That holds only if the advisor consumes no RNG and mutates no board state,
    which is exactly the property a `pin_swap.enabled: false` project needs in
    order to behave byte-identically to the pre-7.14 optimizer.
    """
    off = _pin_swap_project(tmp_path / "off")
    on = _pin_swap_project(tmp_path / "on", enabled=True, min_gain=1.0e6)

    def run(project: Path) -> dict:
        report = o.optimize_board(project, max_iterations_per_call=2, seed=11)
        while report["state"] == "running":
            report = o.optimize_board(project, session_id=report["session_id"],
                                      max_iterations_per_call=2)
        return report

    a, b = run(off), run(on)
    assert a["score_curve"] == b["score_curve"]
    assert [(m["type"], m["summary"], m["accepted"]) for m in a["moves"] if m["type"]] == \
           [(m["type"], m["summary"], m["accepted"]) for m in b["moves"] if m["type"]]
    # ... and the advisor really did run on the "on" side, so the parity above
    # is not vacuous.
    assert b["pin_swap_reports"] and b["pin_swap_reports"][0]["priced"] is True
    assert a["pin_swap_reports"] == []


# --- 5. the hard safety rule ------------------------------------------------


def _project_digest(project: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes()
            for p in sorted(Path(project).iterdir()) if p.suffix in (".kicad_sch", ".net")}


def test_never_writes_the_schematic_or_the_real_netlist_even_with_write_true(tmp_path: Path) -> None:
    """The whole feature's safety rule, asserted against the strongest case:
    a session that found a swap, was told to take it, and was then asked to
    write. The schematic bytes, the `.net` bytes, and the real board's entire
    pad-net map must all come out unchanged - the swap is realizable only by
    the human, and `write=True` carries copper, never connectivity."""
    project = _pin_swap_project(tmp_path, enabled=True)
    _no_ai_pauses(project)

    before_files = _project_digest(project)
    before_pads = o._board_pad_net_map(project)

    report = _run_to_pin_swap_pause(project)
    pending = report["pending_decision"]

    # Answer "I applied it" WITHOUT having applied it - the most adversarial
    # answer available, and the one most likely to tempt a tool into helping.
    answered = o.decide_route(project, report["session_id"], pending["decision_id"],
                              "opt2", rationale="claims to have swapped it")
    assert answered["resync"]["resynced"] is False
    assert answered["resync"]["changed_pads"] == []

    report = o.optimize_board(project, session_id=report["session_id"],
                              max_iterations_per_call=6)
    while report["state"] in ("running", "awaiting_decision"):
        report = o.optimize_board(project, session_id=report["session_id"],
                                  max_iterations_per_call=6)
    o.optimize_board(project, session_id=report["session_id"], write=True)

    assert _project_digest(project) == before_files
    assert o._board_pad_net_map(project) == before_pads


def test_write_refuses_when_pad_nets_diverge(tmp_path: Path) -> None:
    """The backstop that makes "never silently reassigns a pad" a CHECKED
    property rather than a promise about code paths: if the session's board and
    the real board disagree about any pad's net, nothing is written."""
    project = _pin_swap_project(tmp_path)
    # The session has to reach a TERMINAL state before the divergence is
    # forged: resuming a paused session promotes a trial over the scratch,
    # which would quietly overwrite the forgery and test nothing.
    _no_ai_pauses(project)
    report = o.optimize_board(project, max_iterations_per_call=2, seed=3)
    while report["state"] == "running":
        report = o.optimize_board(project, session_id=report["session_id"],
                                  max_iterations_per_call=2)
    assert report["state"] in ("converged", "budget_exhausted")

    # Forge the divergence directly on the session's scratch copy - the exact
    # state a promoted pin-swap trial would have produced had one ever escaped.
    scratch = Path(report["scratch_dir"])
    o._trial_swap_pad_nets(scratch, "J1", "1", "2")

    written = o.optimize_board(project, session_id=report["session_id"], write=True)
    assert written["written"] is False
    assert "REFUSING TO WRITE" in written["write_skipped_reason"]
    assert "J1.1" in written["write_skipped_reason"]


def test_trial_swap_leaves_the_source_project_untouched(tmp_path: Path) -> None:
    """The swap surgery itself, in isolation: applied to a copy, it changes
    that copy's board AND netlist coherently and the original not at all."""
    project = _pin_swap_project(tmp_path / "src", enabled=True)
    before = _project_digest(project)
    before_board = (project / "pinswap.kicad_pcb").read_bytes()

    trial = o._scratch_snapshot(project, tmp_path / "trial")
    detail = o._trial_swap_pad_nets(trial, "J1", "1", "2")

    assert detail["netlist_swapped"] is True
    assert o._board_pad_net_map(trial)["J1.1"] == "SIG_B"
    assert o._board_pad_net_map(trial)["J1.2"] == "SIG_A"
    # Board and netlist agree afterwards - the trial is a coherent project, not
    # a board with a lie in it.
    assert o._netlist_pad_mismatches(trial) == []

    assert _project_digest(project) == before
    assert (project / "pinswap.kicad_pcb").read_bytes() == before_board


# --- 6. netlist re-sync -----------------------------------------------------


def test_resync_adopts_a_changed_pad_assignment_and_rederives_the_ratsnest(tmp_path: Path) -> None:
    """The plan's "the session re-syncs (netlist-staleness check) and
    continues", end to end.

    The human's schematic edit + netlist re-export + 'Update PCB from
    Schematic' is stood in for by applying the swap to the REAL project - which
    is exactly what those three steps produce. The session must then notice,
    adopt the new assignment (never invent one), reroute what the change
    invalidated, and carry on.
    """
    project = _pin_swap_project(tmp_path, enabled=True)
    _no_ai_pauses(project)

    report = _run_to_pin_swap_pause(project)
    pending = report["pending_decision"]
    scratch = Path(report["scratch_dir"])
    assert o._board_pad_net_map(scratch)["J1.1"] == "SIG_A"

    # The user does the thing only the user can do.
    o._trial_swap_pad_nets(project, "J1", "1", "2")
    assert o._board_pad_net_map(project)["J1.1"] == "SIG_B"

    answered = o.decide_route(project, report["session_id"], pending["decision_id"],
                              "opt2", rationale="done in the schematic and re-exported")
    resync = answered["resync"]

    assert resync["resynced"] is True
    assert [c["pad"] for c in resync["changed_pads"]] == ["J1.1", "J1.2"]
    assert {c["to_net"] for c in resync["changed_pads"]} == {"SIG_A", "SIG_B"}
    assert resync["rerouted_nets"] == ["SIG_A", "SIG_B"]
    # The re-export was consistent, so no pad-level staleness is reported.
    assert resync["real_netlist_pad_mismatches"] == []

    # The session's board now agrees with the real board about every pad, and
    # its ratsnest/score were re-derived against the NEW assignment.
    assert o._board_pad_net_map(scratch) == o._board_pad_net_map(project)
    assert answered["state"] == "running"
    assert answered["current_score"]["total"] < report["current_score"]["total"]

    # Having adopted the user's own change, the session may still write - the
    # creation-time fingerprint was refreshed rather than left to refuse
    # forever a change the session itself asked for.
    while answered["state"] in ("running", "awaiting_decision"):
        answered = o.optimize_board(project, session_id=report["session_id"],
                                    max_iterations_per_call=4)
    written = o.optimize_board(project, session_id=report["session_id"], write=True)
    assert written["written"] is True
    assert o._board_pad_net_map(project)["J1.1"] == "SIG_B"   # still the USER's assignment


def test_pad_level_staleness_sees_what_the_name_level_guard_cannot(tmp_path: Path) -> None:
    """A pin swap changes no net NAME, so the existing name-set staleness
    guards are blind to it. This is the finer check that is not."""
    project = _pin_swap_project(tmp_path)
    assert o._netlist_pad_mismatches(project) == []

    # Swap on the BOARD only - the state a user is in after updating the PCB
    # from a schematic whose netlist they forgot to re-export.
    board = project / "pinswap.kicad_pcb"
    text = board.read_text(encoding="utf-8")
    fp = o._footprint_span_by_ref(text, "J1")
    board.write_text(o._swap_spans(text, o._pad_net_span(text, *fp, "1"),
                                   o._pad_net_span(text, *fp, "2")), encoding="utf-8")
    k._invalidate_board_cache(board)

    mismatches = o._netlist_pad_mismatches(project)
    assert [m["pad"] for m in mismatches] == ["J1.1", "J1.2"]
    assert mismatches[0] == {"pad": "J1.1", "board_net": "SIG_B", "netlist_net": "SIG_A"}
