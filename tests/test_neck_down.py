"""Tests for Phase 7.12 "neck-down: wide nets onto small pads".

Covers, per NETCLASS_PLAN.md's exact spec:
  1. A wide net-class connection landing on a small pad gets a genuinely
     narrower neck stretch at that endpoint (width, length bounds), while the
     rest of the trace stays at full class width.
  2. Parity: a connection whose pads are already wide enough emits IDENTICAL
     geometry with `neck_down.enabled: true` (default) vs `false`.
  3. `_self_check` prices a neck at its TRUE (narrow) width, not the class
     width - a precisely constructed neighbor obstacle that would clip at the
     wide width but clears at the narrow width.
  4. `audit_netclass_conformance` accepts a genuine neck (not flagged) but
     still flags a merely-narrow segment that doesn't satisfy neck geometry
     (wrong width for its pad, or too long) - both directions, plus the
     `neck_down.enabled: false` case restoring the strict pre-7.12 behavior.

All boards are synthetic (`tests/synthetic_board.py`'s explicit-component
mode), built with a custom `.kicad_pro` Default net-class (`track_width`
deliberately WIDE - 1.0 mm, like a real power-rail class) so the pad-vs-class
mismatch is easy to control precisely, unlike kiln's real board (whose
pads/classes don't naturally trigger neck-down).
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

import kicad_pcb_tool as pcb
import kicad_router_tool as router
from synthetic_board import generate_critical_nets_board, _segment_block as _syn_segment_block

_CLASS_TRACK_WIDTH = 1.0  # deliberately wide, like a power net-class


def _write_project(
    directory: Path,
    name: str,
    components: list[dict],
    extra_segments: list[tuple[str, float, float, float, float, float, str]] | None = None,
    min_track_width: float = 0.0,
    neck_settings: dict | None = None,
) -> Path:
    """Write a minimal synthetic project (board + `.kicad_pro`, no `.net` -
    none of the functions under test need one) into `directory`.

    `extra_segments` is `[(net, x1, y1, x2, y2, width, layer), ...]` - hand-
    authored copper spliced into the board text (for the audit tests, which
    need exact pre-existing segment geometry rather than router output).
    `min_track_width` sets `board.design_settings.rules.min_track_width` (the
    "7.11 minimum" neck floor); 0 leaves the rule unset. `neck_settings`, when
    given, writes `pcb_settings.json` with `{"neck_down": neck_settings}`.
    """
    directory.mkdir(parents=True, exist_ok=True)
    board_text, _ = generate_critical_nets_board(components, layers=2)
    if extra_segments:
        pieces = "".join(
            _syn_segment_block(x1, y1, x2, y2, width, layer, net, f"synth-neckseg-{i:04d}")
            for i, (net, x1, y1, x2, y2, width, layer) in enumerate(extra_segments)
        )
        assert board_text.endswith(")\n")
        board_text = board_text[: -len(")\n")] + pieces + ")\n"
    (directory / f"{name}.kicad_pcb").write_text(board_text, encoding="utf-8")

    pro: dict = {
        "net_settings": {
            "classes": [
                {
                    "name": "Default",
                    "clearance": 0.2,
                    "track_width": _CLASS_TRACK_WIDTH,
                    "via_diameter": 0.6,
                    "via_drill": 0.3,
                    "diff_pair_width": 0.2,
                    "diff_pair_gap": 0.25,
                    "microvia_diameter": 0.3,
                    "microvia_drill": 0.1,
                }
            ],
            "net_colors": None,
            "netclass_assignments": None,
            "netclass_patterns": [],
        }
    }
    if min_track_width:
        pro["board"] = {"design_settings": {"rules": {"min_track_width": min_track_width}}}
    (directory / f"{name}.kicad_pro").write_text(json.dumps(pro, indent=2), encoding="utf-8")

    if neck_settings is not None:
        (directory / "pcb_settings.json").write_text(
            json.dumps({"neck_down": neck_settings}), encoding="utf-8")
    return directory


def _pwr_components(small_size: float = 0.3, big_size: float = 1.0, net: str = "PWR") -> list[dict]:
    return [
        {
            "ref": "U1", "footprint": "synthetic:IC", "x": 0.0, "y": 0.0,
            "pads": [("1", 0.0, 0.0, small_size, small_size, net)],
        },
        {
            "ref": "U2", "footprint": "synthetic:IC", "x": 10.0, "y": 0.0,
            "pads": [("1", 0.0, 0.0, big_size, big_size, net)],
        },
    ]


# --------------------------------------------------------------------------- #
# 1. Genuine neck: wide class width landing on a small pad
# --------------------------------------------------------------------------- #

def test_neck_emitted_for_small_pad(tmp_path: Path) -> None:
    project = _write_project(tmp_path, "neck1", _pwr_components())
    conn = router.get_ratsnest(project)["connections"][0]
    assert conn["net"] == "PWR"

    res = router.route_nets(project, connections=[conn], write=True)
    assert res["written"] is True

    board_path, _, _ = pcb._resolve_project_path(project)
    tracks = pcb._parse_tracks_cached(board_path)
    segs = [s for s in tracks["segments"] if s["net"] == "PWR"]
    assert len(segs) == 2, "expected exactly a neck segment + one full-class-width segment"

    neck = min(segs, key=lambda s: s["width"])
    rest = max(segs, key=lambda s: s["width"])
    assert neck["width"] == pytest.approx(0.3, abs=1e-6)   # min(1.0, 1.0 * 0.3)
    assert rest["width"] == pytest.approx(_CLASS_TRACK_WIDTH, abs=1e-6)

    # neck length within the configured [min_length_mm, max_length_mm] bounds.
    assert 0.5 - 1e-6 <= neck["length"] <= 3.0 + 1e-6

    # the neck touches the small pad at (0, 0).
    near_origin = min(
        math.hypot(neck["start"]["x"], neck["start"]["y"]),
        math.hypot(neck["end"]["x"], neck["end"]["y"]),
    )
    assert near_origin < 1e-6

    # total routed length is still ~10 mm end to end (neck + rest, unbroken).
    assert neck["length"] + rest["length"] == pytest.approx(10.0, abs=1e-3)


# --------------------------------------------------------------------------- #
# 2. Parity: pads already wide enough -> identical geometry enabled vs disabled
# --------------------------------------------------------------------------- #

def test_no_neck_needed_is_byte_identical_enabled_vs_disabled(tmp_path: Path) -> None:
    components = _pwr_components(small_size=1.0, big_size=1.0, net="SIG")  # both pads big

    dir_enabled = _write_project(tmp_path / "enabled", "neck2", components)
    dir_disabled = _write_project(tmp_path / "disabled", "neck2", components,
                                  neck_settings={"enabled": False})

    conn_a = router.get_ratsnest(dir_enabled)["connections"][0]
    conn_b = router.get_ratsnest(dir_disabled)["connections"][0]

    res_a = router.route_nets(dir_enabled, connections=[conn_a], write=True)
    res_b = router.route_nets(dir_disabled, connections=[conn_b], write=True)
    assert res_a["written"] and res_b["written"]

    board_a, _, _ = pcb._resolve_project_path(dir_enabled)
    board_b, _, _ = pcb._resolve_project_path(dir_disabled)
    segs_a = [s for s in pcb._parse_tracks_cached(board_a)["segments"] if s["net"] == "SIG"]
    segs_b = [s for s in pcb._parse_tracks_cached(board_b)["segments"] if s["net"] == "SIG"]

    def _norm(segs):
        return sorted(
            (round(s["start"]["x"], 6), round(s["start"]["y"], 6),
             round(s["end"]["x"], 6), round(s["end"]["y"], 6),
             round(s["width"], 6), s["layer"])
            for s in segs
        )

    assert _norm(segs_a) == _norm(segs_b)
    # every segment routed at the uniform class width - no neck was applied.
    assert all(s["width"] == pytest.approx(_CLASS_TRACK_WIDTH, abs=1e-6) for s in segs_a)


# --------------------------------------------------------------------------- #
# 3. Self-check prices a neck at its TRUE (narrow) width, not the class width
# --------------------------------------------------------------------------- #

def test_self_check_prices_neck_at_true_width() -> None:
    rules = {"track_width": _CLASS_TRACK_WIDTH, "clearance": 0.2, "edge_clearance": 0.2}
    via_radius = 0.15

    # A foreign obstacle 0.6 mm away (perpendicular), same x-range as our
    # candidate segment - parallel copper on the same layer, different net.
    obstacle = router._Obst(
        "seg", "OTHER", frozenset({"F.Cu"}), 0.1,   # half = 0.2mm track / 2
        0.0, 0.6, 1.2, 0.6,
    )

    wide_segment = [{"x1": 0.0, "y1": 0.0, "x2": 1.2, "y2": 0.0, "layer": "F.Cu",
                     "width": _CLASS_TRACK_WIDTH}]
    narrow_segment = [{"x1": 0.0, "y1": 0.0, "x2": 1.2, "y2": 0.0, "layer": "F.Cu",
                       "width": 0.3}]

    wide_violations = router._self_check("PWR", wide_segment, [], [obstacle], rules, via_radius)
    narrow_violations = router._self_check("PWR", narrow_segment, [], [obstacle], rules, via_radius)

    assert wide_violations, "at the wide class width this segment clips the neighbor"
    assert not narrow_violations, "at the true narrow neck width this segment clears"


def test_self_check_falls_back_to_class_width_without_explicit_width() -> None:
    """A segment with NO `"width"` key (every segment from every other landed
    feature) must price at `rules["track_width"]` - byte-identical to
    pre-7.12 behavior."""
    rules = {"track_width": _CLASS_TRACK_WIDTH, "clearance": 0.2, "edge_clearance": 0.2}
    via_radius = 0.15
    obstacle = router._Obst("seg", "OTHER", frozenset({"F.Cu"}), 0.1, 0.0, 0.6, 1.2, 0.6)
    plain_segment = [{"x1": 0.0, "y1": 0.0, "x2": 1.2, "y2": 0.0, "layer": "F.Cu"}]
    violations = router._self_check("PWR", plain_segment, [], [obstacle], rules, via_radius)
    assert violations, "no explicit width -> priced at the wide class width, same as before 7.12"


# --------------------------------------------------------------------------- #
# 4. audit_netclass_conformance: accept a genuine neck, still flag a fake one
# --------------------------------------------------------------------------- #

def _audit_components() -> list[dict]:
    return [
        {"ref": "U1", "footprint": "synthetic:IC", "x": 0.0, "y": 0.0,
         "pads": [("1", 0.0, 0.0, 0.3, 0.3, "PWR_GOOD")]},
        {"ref": "U2", "footprint": "synthetic:IC", "x": 10.0, "y": 0.0,
         "pads": [("1", 0.0, 0.0, 1.0, 1.0, "PWR_GOOD")]},
        {"ref": "U3", "footprint": "synthetic:IC", "x": 20.0, "y": 0.0,
         "pads": [("1", 0.0, 0.0, 0.3, 0.3, "PWR_BAD_WIDTH")]},
        {"ref": "U4", "footprint": "synthetic:IC", "x": 30.0, "y": 0.0,
         "pads": [("1", 0.0, 0.0, 1.0, 1.0, "PWR_BAD_WIDTH")]},
        {"ref": "U5", "footprint": "synthetic:IC", "x": 40.0, "y": 0.0,
         "pads": [("1", 0.0, 0.0, 0.3, 0.3, "PWR_BAD_LEN")]},
        {"ref": "U6", "footprint": "synthetic:IC", "x": 50.0, "y": 0.0,
         "pads": [("1", 0.0, 0.0, 1.0, 1.0, "PWR_BAD_LEN")]},
    ]


def _audit_extra_segments() -> list[tuple[str, float, float, float, float, float, str]]:
    return [
        # Genuine neck: length 1.0mm (in [0.5, 3.0]), width == min(1.0, 1.0*0.3) == 0.3,
        # terminates exactly on U1's pad (0, 0).
        ("PWR_GOOD", 0.0, 0.0, 1.0, 0.0, 0.3, "F.Cu"),
        # Wrong width for its pad (0.25, not the justified 0.3) - real violation.
        ("PWR_BAD_WIDTH", 20.0, 0.0, 21.5, 0.0, 0.25, "F.Cu"),
        # Correct neck width (0.3) but too long (5mm > max_length_mm 3.0) - real violation.
        ("PWR_BAD_LEN", 40.0, 0.0, 45.0, 0.0, 0.3, "F.Cu"),
    ]


def test_audit_accepts_genuine_neck_but_flags_fake_necks(tmp_path: Path) -> None:
    project = _write_project(
        tmp_path, "auditneck", _audit_components(), extra_segments=_audit_extra_segments())

    result = pcb.audit_netclass_conformance(project)
    rows = {r["net"]: r for r in result["rows"]}

    good = rows["PWR_GOOD"]
    assert good["conforms"] is True
    assert not good["mismatches"]
    assert good.get("neck_segments") == 1

    bad_width = rows["PWR_BAD_WIDTH"]
    assert bad_width["conforms"] is False
    assert any("track_width" in m for m in bad_width["mismatches"])
    assert not bad_width.get("neck_segments")

    bad_len = rows["PWR_BAD_LEN"]
    assert bad_len["conforms"] is False
    assert any("track_width" in m for m in bad_len["mismatches"])
    assert not bad_len.get("neck_segments")


def test_audit_neck_down_disabled_restores_strict_behavior(tmp_path: Path) -> None:
    """With `neck_down.enabled: false`, the genuine-neck net from the test
    above is flagged too - exactly the pre-7.12 strict behavior, proving the
    acceptance logic is fully gated behind the setting."""
    project = _write_project(
        tmp_path, "auditneckoff", _audit_components(), extra_segments=_audit_extra_segments(),
        neck_settings={"enabled": False})

    result = pcb.audit_netclass_conformance(project)
    rows = {r["net"]: r for r in result["rows"]}

    good = rows["PWR_GOOD"]
    assert good["conforms"] is False
    assert any("track_width" in m for m in good["mismatches"])
    assert not good.get("neck_segments")
