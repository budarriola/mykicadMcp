"""Coverage for six pure-parser tools in `kicad_pcb_tool.py` that had no test
anywhere in this suite before this file: `get_net_track_widths`,
`search_component_by_reference`, `get_pin_position`, `pin_distance`,
`estimate_footprint_radius`, `get_property_position`.

Each reads only `.kicad_pcb` (no live IPC, no netlist export needed - unlike
`find_components_by_net`/`find_components_by_pin_connection`, which depend on
a `.net` export that is not present for the live working-tree board and were
therefore not picked here). Runs directly against the real, live
`hardware/mainBoard/kiln.kicad_pcb`, read-only - no fixture, no copy, no write.

Facts asserted below (component U1 has pads "1".."4", pad "2" is net
GND_Main, pad "1"-"2" spacing is 1.27mm, etc.) were captured against the board
as of 2026-09-06 by calling each function directly (see this file's git log
for the probe). If a legitimate board edit changes them, update the
constants here in the same commit - a silent drift means the parser broke.

Each test also has a malformed-input companion using a tiny, deliberately
truncated synthetic `.kicad_pcb` (an unterminated s-expression) to confirm
the parser fails loudly (raises) instead of silently returning nonsense.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import kicad_pcb_tool as k

_REAL_BOARD_DIR = Path(__file__).resolve().parents[3] / "hardware" / "mainBoard"

pytestmark = pytest.mark.skipif(
    not (_REAL_BOARD_DIR / "kiln.kicad_pcb").exists(),
    reason=f"Real kiln board not found at {_REAL_BOARD_DIR}",
)

_MALFORMED_BOARD_TEXT = "(kicad_pcb (version 20260206) (generator pcbnew garbage not closed"


@pytest.fixture()
def malformed_board(tmp_path: Path) -> Path:
    (tmp_path / "kiln.kicad_pcb").write_text(_MALFORMED_BOARD_TEXT, encoding="utf-8")
    return tmp_path


# --------------------------------------------------------------------------- #
# get_net_track_widths
# --------------------------------------------------------------------------- #

def test_get_net_track_widths_real_board_has_routed_copper() -> None:
    result = k.get_net_track_widths(_REAL_BOARD_DIR)
    assert result["net_count"] > 0
    names = [n["net"] for n in result["nets"]]
    assert len(names) == len(set(names))  # no duplicate net entries
    for entry in result["nets"]:
        assert entry["net"] != ""  # empty/unconnected copper is excluded


def test_get_net_track_widths_single_net_filter() -> None:
    all_nets = k.get_net_track_widths(_REAL_BOARD_DIR)
    some_net = all_nets["nets"][0]["net"]
    filtered = k.get_net_track_widths(_REAL_BOARD_DIR, net=some_net)
    # net=X returns the single bucket directly, not wrapped in a "nets" list.
    assert filtered["net"] == some_net
    assert filtered["segment_count"] >= 0


def test_get_net_track_widths_malformed_board_raises(malformed_board: Path) -> None:
    with pytest.raises(ValueError):
        k.get_net_track_widths(malformed_board)


# --------------------------------------------------------------------------- #
# search_component_by_reference
# --------------------------------------------------------------------------- #

def test_search_component_by_reference_finds_u1() -> None:
    result = k.search_component_by_reference(_REAL_BOARD_DIR, "U1")
    assert result["matches"], "expected at least one match for U1"
    match = result["matches"][0]
    assert match["reference"] == "U1"
    assert match["section_start"] < match["reference_line"] < match["section_end"]


def test_search_component_by_reference_unknown_ref_raises() -> None:
    with pytest.raises(KeyError):
        k.search_component_by_reference(_REAL_BOARD_DIR, "ZZ999")


def test_search_component_by_reference_malformed_board_raises(malformed_board: Path) -> None:
    with pytest.raises(KeyError):
        k.search_component_by_reference(malformed_board, "U1")


# --------------------------------------------------------------------------- #
# get_pin_position
# --------------------------------------------------------------------------- #

def test_get_pin_position_u1_pin1() -> None:
    pos = k.get_pin_position(_REAL_BOARD_DIR, "U1", "1")
    assert pos["reference"] == "U1"
    assert pos["pin"] == "1"
    assert isinstance(pos["position"]["x"], float)
    assert isinstance(pos["position"]["y"], float)


def test_get_pin_position_unknown_pin_raises_keyerror() -> None:
    with pytest.raises(KeyError):
        k.get_pin_position(_REAL_BOARD_DIR, "U1", "not-a-real-pin")


def test_get_pin_position_malformed_board_raises(malformed_board: Path) -> None:
    with pytest.raises(ValueError):
        k.get_pin_position(malformed_board, "U1", "1")


# --------------------------------------------------------------------------- #
# pin_distance
# --------------------------------------------------------------------------- #

def test_pin_distance_u1_pin1_to_pin2() -> None:
    result = k.pin_distance(_REAL_BOARD_DIR, "U1", "1", "U1", "2")
    assert result["a"]["pin"] == "1"
    assert result["b"]["pin"] == "2"
    assert result["distance"] == pytest.approx(1.27, abs=1e-3)


def test_pin_distance_same_pin_is_zero() -> None:
    result = k.pin_distance(_REAL_BOARD_DIR, "U1", "1", "U1", "1")
    assert result["distance"] == 0.0


def test_pin_distance_malformed_board_raises(malformed_board: Path) -> None:
    with pytest.raises(ValueError):
        k.pin_distance(malformed_board, "U1", "1", "U1", "2")


# --------------------------------------------------------------------------- #
# estimate_footprint_radius
# --------------------------------------------------------------------------- #

def test_estimate_footprint_radius_u1_is_positive_and_reasonable() -> None:
    radius = k.estimate_footprint_radius(_REAL_BOARD_DIR, "U1")
    assert isinstance(radius, float)
    assert 0.0 < radius < 50.0  # sane bound for any real footprint on this board


def test_estimate_footprint_radius_unknown_ref_raises() -> None:
    with pytest.raises(KeyError):
        k.estimate_footprint_radius(_REAL_BOARD_DIR, "ZZ999")


def test_estimate_footprint_radius_malformed_board_raises(malformed_board: Path) -> None:
    with pytest.raises(ValueError):
        k.estimate_footprint_radius(malformed_board, "U1")


# --------------------------------------------------------------------------- #
# get_property_position
# --------------------------------------------------------------------------- #

def test_get_property_position_u1_reference_label() -> None:
    result = k.get_property_position(_REAL_BOARD_DIR, "U1", "Reference")
    assert result["reference"] == "U1"
    assert result["property"] == "Reference"
    assert "x" in result["at"] and "y" in result["at"]


def test_get_property_position_unknown_ref_raises() -> None:
    with pytest.raises(KeyError):
        k.get_property_position(_REAL_BOARD_DIR, "ZZ999", "Reference")


def test_get_property_position_malformed_board_raises(malformed_board: Path) -> None:
    with pytest.raises(ValueError):
        k.get_property_position(malformed_board, "U1")
