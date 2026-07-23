"""Tests for Phase 7.14 connector detection (`detect_connectors` /
`detect_kicad_connectors`) and its exclusion-validation helper
(`validate_connector_exclusions`).

Detection only - the optimizer pin-swap move (7.6 family) is out of scope
here; these tests only cover the read-only scan and the exclusion-list
validation helper that a later interaction step would call.

Uses a tiny purpose-built synthetic board (full control over ref/footprint
name/pads) plus the real kiln board for the golden "does it find kiln's real
connectors" check. Reuses the repo's synthetic layer-stack helper so the
boards parse identically to how KiCad-generated ones do.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import kicad_pcb_tool as pcb
from synthetic_board import _layer_stack_lines, _HEADER_TEMPLATE, _FOOTER


# --------------------------------------------------------------------------- #
# Minimal board builder (custom ref + custom footprint/library name per part)
# --------------------------------------------------------------------------- #

def _pad(num: str, x: float, y: float, net: str, layer: str = "F.Cu", size: float = 0.9) -> str:
    return (f'        (pad "{num}" smd rect (at {x} {y}) (size {size} {size}) '
            f'(layers "{layer}" "{layer[0]}.Paste" "{layer[0]}.Mask") (net "{net}"))')


def _footprint(ref: str, fp_name: str, x: float, y: float, uuid: str, pads: list[str]) -> str:
    body = "\n".join(pads)
    return (f'    (footprint "{fp_name}"\n'
            f'        (layer "F.Cu")\n'
            f'        (uuid "{uuid}")\n'
            f'        (at {x} {y})\n'
            f'        (property "Reference" "{ref}" (at 0 -1.5) (layer "F.SilkS"))\n'
            f'        (property "Value" "V" (at 0 1.5) (layer "F.Fab"))\n'
            f'{body}\n'
            f'    )\n')


def _write_board(path: Path, body_parts: list[str], layers: int = 2) -> Path:
    header = _HEADER_TEMPLATE.format(layer_lines=_layer_stack_lines(layers))
    path.write_text(header + "".join(body_parts) + _FOOTER, encoding="utf-8")
    return path


def _build_mixed_board(tmp_path: Path) -> Path:
    """One board with four footprints exercising every detection path:
      - J1: ref-prefix match only (plain footprint name, no connector token)
      - K1: footprint-token match only (JST connector under a non-standard
        ref outside the default J/P/CN/X set) - the "still caught" case
      - J2: BOTH signals (ref prefix AND a connector-token footprint name)
      - R1: neither signal - must NOT be detected
    """
    board = tmp_path / "connectors.kicad_pcb"
    _write_board(board, [
        _footprint("J1", "synthetic:HDR_2P", 0, 0, "fp-j1", [
            _pad("1", -0.75, 0, "NET_A"),
            _pad("2", 0.75, 0, "NET_B"),
        ]),
        _footprint("K1", "Connector_JST:JST_XH_B3B-XH-A_1x03", 10, 0, "fp-k1", [
            _pad("1", -1.0, 0, "NET_C"),
            _pad("2", 0.0, 0, "NET_D"),
            _pad("3", 1.0, 0, "NET_E"),
        ]),
        _footprint("J2", "Connector_JST:JST_XH_B2B-XH-A_1x02", 20, 0, "fp-j2", [
            _pad("1", -0.5, 0, "NET_F"),
            _pad("2", 0.5, 0, "NET_G"),
        ]),
        _footprint("R1", "synthetic:R_0603", 30, 0, "fp-r1", [
            _pad("1", -0.75, 0, "NET_H"),
            _pad("2", 0.75, 0, "NET_H"),
        ]),
    ])
    return board


# --------------------------------------------------------------------------- #
# Golden test: real kiln board
# --------------------------------------------------------------------------- #

def test_kiln_detects_real_connectors(kiln_project_path: Path) -> None:
    """Golden test against the real board: 24 J-prefixed connectors, with J2
    (the JST header) matched by both signals since it's both J-prefixed AND
    carries a connector-token footprint name."""
    result = pcb.detect_connectors(kiln_project_path)

    assert result["ref_prefixes_used"] == ["J", "P", "CN", "X"]
    assert result["candidate_count"] == 24
    assert len(result["candidates"]) == 24

    refs = [c["ref"] for c in result["candidates"]]
    assert refs == sorted(refs), "candidates should be sorted by ref"
    assert all(ref.upper().startswith("J") for ref in refs), "kiln's connectors are all J-prefixed"

    by_ref = {c["ref"]: c for c in result["candidates"]}
    assert "J2" in by_ref
    j2 = by_ref["J2"]
    assert set(j2["matched_by"]) == {"ref_prefix", "footprint_token"}
    assert "JST" in j2["footprint"]
    assert j2["pin_count"] == 11
    assert len(j2["pins"]) == 11
    # Every pin entry has a pad number and a net (possibly empty for unconnected).
    for pin in j2["pins"]:
        assert "pad" in pin and "net" in pin

    # A handful of the smaller 2-pin headers, sanity-checked by pin count.
    assert by_ref["J1"]["pin_count"] == 2
    assert by_ref["J13"]["pin_count"] == 6


# --------------------------------------------------------------------------- #
# Synthetic board: each detection path in isolation
# --------------------------------------------------------------------------- #

def test_synthetic_ref_prefix_only_match(tmp_path: Path) -> None:
    board = _build_mixed_board(tmp_path)
    result = pcb.detect_connectors(board)
    by_ref = {c["ref"]: c for c in result["candidates"]}

    assert "J1" in by_ref
    assert by_ref["J1"]["matched_by"] == ["ref_prefix"]
    assert by_ref["J1"]["pin_count"] == 2
    assert {p["pad"] for p in by_ref["J1"]["pins"]} == {"1", "2"}
    assert {p["net"] for p in by_ref["J1"]["pins"]} == {"NET_A", "NET_B"}


def test_synthetic_non_standard_ref_caught_by_footprint_token(tmp_path: Path) -> None:
    """K1 has a JST connector footprint but a ref outside the default
    J/P/CN/X prefix set - it must still be detected via the footprint-token
    signal alone (the 'still caught' acceptance case)."""
    board = _build_mixed_board(tmp_path)
    result = pcb.detect_connectors(board)
    by_ref = {c["ref"]: c for c in result["candidates"]}

    assert "K1" in by_ref
    assert by_ref["K1"]["matched_by"] == ["footprint_token"]
    assert by_ref["K1"]["pin_count"] == 3
    assert {p["net"] for p in by_ref["K1"]["pins"]} == {"NET_C", "NET_D", "NET_E"}


def test_synthetic_both_signals_reported(tmp_path: Path) -> None:
    board = _build_mixed_board(tmp_path)
    result = pcb.detect_connectors(board)
    by_ref = {c["ref"]: c for c in result["candidates"]}

    assert set(by_ref["J2"]["matched_by"]) == {"ref_prefix", "footprint_token"}


def test_synthetic_non_connector_excluded(tmp_path: Path) -> None:
    """R1 matches neither signal and must not appear as a candidate."""
    board = _build_mixed_board(tmp_path)
    result = pcb.detect_connectors(board)
    refs = [c["ref"] for c in result["candidates"]]

    assert "R1" not in refs
    assert result["candidate_count"] == 3  # J1, K1, J2 only


def test_synthetic_candidates_sorted_by_ref(tmp_path: Path) -> None:
    board = _build_mixed_board(tmp_path)
    result = pcb.detect_connectors(board)
    refs = [c["ref"] for c in result["candidates"]]
    assert refs == sorted(refs)


def test_explicit_ref_prefixes_override_widens_matches(tmp_path: Path) -> None:
    """Passing ref_prefixes=["K"] makes K1 ALSO match by ref_prefix (on top
    of its existing footprint_token match), and excludes J1 (no longer
    ref-matched, no connector-token footprint)."""
    board = _build_mixed_board(tmp_path)
    result = pcb.detect_connectors(board, ref_prefixes=["K"])
    by_ref = {c["ref"]: c for c in result["candidates"]}

    assert "J1" not in by_ref
    assert "K1" in by_ref
    assert set(by_ref["K1"]["matched_by"]) == {"ref_prefix", "footprint_token"}
    # J2 still detected via its footprint token even though "J" is no longer
    # a configured ref prefix.
    assert "J2" in by_ref
    assert by_ref["J2"]["matched_by"] == ["footprint_token"]


def test_pcb_settings_pin_swap_ref_prefixes_respected(tmp_path: Path) -> None:
    """pcb_settings.json pin_swap.ref_prefixes is honored when the caller
    doesn't pass an explicit override."""
    board = _build_mixed_board(tmp_path)
    settings_path = tmp_path / "connectors.kicad_pro"
    settings_path.write_text(json.dumps({"project": "connectors"}), encoding="utf-8")
    (tmp_path / "pcb_settings.json").write_text(
        json.dumps({"pin_swap": {"ref_prefixes": ["K"]}}), encoding="utf-8"
    )

    result = pcb.detect_connectors(board)
    assert result["ref_prefixes_used"] == ["K"]
    by_ref = {c["ref"]: c for c in result["candidates"]}
    assert "J1" not in by_ref
    assert set(by_ref["K1"]["matched_by"]) == {"ref_prefix", "footprint_token"}


def test_empty_board_no_candidates(tmp_path: Path) -> None:
    board = tmp_path / "empty.kicad_pcb"
    _write_board(board, [])
    result = pcb.detect_connectors(board)
    assert result["candidate_count"] == 0
    assert result["candidates"] == []


# --------------------------------------------------------------------------- #
# Exclusion-validation helper (Phase 7.14 interaction contract)
# --------------------------------------------------------------------------- #

def test_validate_exclusions_happy_path_case_insensitive(tmp_path: Path) -> None:
    board = _build_mixed_board(tmp_path)
    result = pcb.validate_connector_exclusions(board, exclusions=["j1", "k1"])
    assert result["resolved_exclusions"] == ["J1", "K1"]
    assert set(result["detected_refs"]) == {"J1", "K1", "J2"}


def test_validate_exclusions_unresolved_name_raises_loudly(tmp_path: Path) -> None:
    """An exclusion that doesn't resolve to a detected connector must abort
    loudly (ValueError) and the error must list every detected ref - no
    silent typo drop."""
    board = _build_mixed_board(tmp_path)
    with pytest.raises(ValueError) as excinfo:
        pcb.validate_connector_exclusions(board, exclusions=["J1", "J99"])

    message = str(excinfo.value)
    assert "J99" in message
    # Every detected ref must be shown so the user can spot their typo.
    assert "J1" in message
    assert "K1" in message
    assert "J2" in message


def test_validate_exclusions_mixed_valid_and_invalid_lists_both(tmp_path: Path) -> None:
    board = _build_mixed_board(tmp_path)
    with pytest.raises(ValueError) as excinfo:
        pcb.validate_connector_exclusions(board, exclusions=["J1", "NOPE", "ALSO_NOPE"])
    message = str(excinfo.value)
    assert "NOPE" in message
    assert "ALSO_NOPE" in message


def test_validate_exclusions_empty_list_ok(tmp_path: Path) -> None:
    board = _build_mixed_board(tmp_path)
    result = pcb.validate_connector_exclusions(board, exclusions=[])
    assert result["resolved_exclusions"] == []
    assert set(result["detected_refs"]) == {"J1", "K1", "J2"}


def test_kiln_validate_exclusions_real_refs(kiln_project_path: Path) -> None:
    result = pcb.validate_connector_exclusions(kiln_project_path, exclusions=["j2", "j13"])
    assert result["resolved_exclusions"] == ["J2", "J13"]
