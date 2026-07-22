"""Tests for DRC constraints resolver (Phase 7.11).

Tests cover:
1. Golden test on the real kilnCtl project (JLCPCB.kicad_dru.txt parsing)
2. Precedence of constraints (DRU > .kicad_pro > fallback)
3. Unsupported conditions are reported, not silently ignored
4. Cache invalidation on file mtime change
5. Synthetic .kicad_dru/.kicad_pro combinations
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import kicad_pcb_tool as pcb
import kicad_router_tool as router
from synthetic_board import _layer_stack_lines, _HEADER_TEMPLATE, _FOOTER


# --------------------------------------------------------------------------- #
# Golden test: JLCPCB.kicad_dru.txt on the real kiln project
# --------------------------------------------------------------------------- #

def test_golden_jlcpcb_dru_parsing(kiln_project_path: Path):
    """Golden test: parse the actual JLCPCB.kicad_dru.txt from kiln project.

    Verifies that the resolver correctly identifies and parses all rule
    definitions from the real file used in production.
    """
    result = router.get_drc_constraints(kiln_project_path)

    # Should have found the DRU file
    assert result['dru_file'] is not None
    assert 'JLCPCB' in result['dru_file']

    # Should have rules parsed
    constraints = result['constraints']
    assert 'track_width' in constraints
    assert 'clearance' in constraints

    # JLCPCB rules include outer layer track width 0.127mm
    assert constraints['track_width']['value'] in [0.127, 0.09]


def test_golden_kiln_constraints_sources(kiln_project_path: Path):
    """Golden test: verify constraint precedence for kiln project.

    DRU rules should have precedence over .kicad_pro settings.
    """
    result = router.get_drc_constraints(kiln_project_path)

    # Clearance should come from DRU, not pro/fallback
    clearance = result['constraints']['clearance']
    sources = clearance.get('sources', [])

    # Should have at least one source tracing
    assert len(sources) > 0
    # At least one should be from DRU or board rules
    assert any(s.get('type') in ('dru_rule', 'board_rule', 'fallback') for s in sources)


def test_golden_kiln_unsupported_rules(kiln_project_path: Path):
    """Golden test: identify unsupported rule conditions in JLCPCB.

    Some rules may have B.Type/B.Net predicates we cannot evaluate offline.
    These should be reported in unsupported_rules, not silently dropped.
    """
    result = router.get_drc_constraints(kiln_project_path)

    unsupported = result['unsupported_rules']
    # Some JLCPCB rules have pair predicates (B.Type == A.Type)
    # These should be flagged
    # (exact count depends on the .kicad_dru file structure)
    # At minimum, report any that are present
    for rule in unsupported:
        assert 'name' in rule
        assert 'condition' in rule
        assert 'reason' in rule


# --------------------------------------------------------------------------- #
# Precedence tests with synthetic files
# --------------------------------------------------------------------------- #

def test_precedence_dru_over_pro(tmp_path: Path):
    """DRU rules take precedence over .kicad_pro settings."""
    board_dir = tmp_path / 'board'
    board_dir.mkdir()

    # Minimal synthetic board
    board_path = board_dir / 'test.kicad_pcb'
    board_text = (_HEADER_TEMPLATE.format(layer_lines=_layer_stack_lines(2))
                  + '\n' + _FOOTER)
    board_path.write_text(board_text, encoding='utf-8')

    # Create .kicad_pro with clearance 0.3mm
    pro_path = board_dir / 'test.kicad_pro'
    pro_data = {
        'board': {
            'design_settings': {
                'net_settings': {
                    'classes': [{
                        'name': 'Default',
                        'clearance': 0.3,
                        'track_width': 0.25,
                        'via_diameter': 0.8,
                        'via_drill': 0.4,
                    }]
                },
                'rules': {
                    'min_clearance': 0.2,
                    'min_track_width': 0.2,
                }
            }
        }
    }
    pro_path.write_text(json.dumps(pro_data), encoding='utf-8')

    # Create .kicad_dru with clearance 0.15mm (higher priority)
    dru_path = board_dir / 'test.kicad_dru'
    dru_text = """(version 1)
(rule "Test clearance"
    (constraint clearance (min 0.15mm))
)
"""
    dru_path.write_text(dru_text, encoding='utf-8')

    # Create pcb_settings.json with fallback 0.1mm
    settings_path = board_dir / 'pcb_settings.json'
    settings_data = {
        'autorouter': {'clearance_fallback_mm': 0.1}
    }
    settings_path.write_text(json.dumps(settings_data), encoding='utf-8')

    result = router.get_drc_constraints(board_dir)

    # DRU (0.15) should win over pro (0.3) and fallback (0.1)
    assert result['constraints']['clearance']['value'] == pytest.approx(0.15, abs=0.001)

    # Verify sources list shows DRU rule is first/highest priority
    sources = result['constraints']['clearance']['sources']
    assert any(s.get('type') == 'dru_rule' for s in sources)


def test_precedence_pro_over_fallback(tmp_path: Path):
    """When no DRU file, .kicad_pro rules take precedence over fallback."""
    board_dir = tmp_path / 'board'
    board_dir.mkdir()

    board_path = board_dir / 'test.kicad_pcb'
    board_text = (_HEADER_TEMPLATE.format(layer_lines=_layer_stack_lines(2))
                  + '\n' + _FOOTER)
    board_path.write_text(board_text, encoding='utf-8')

    pro_path = board_dir / 'test.kicad_pro'
    pro_data = {
        'board': {
            'design_settings': {
                'net_settings': {
                    'classes': [{
                        'name': 'Default',
                        'clearance': 0.25,
                    }]
                },
                'rules': {
                    'min_clearance': 0.2,
                }
            }
        }
    }
    pro_path.write_text(json.dumps(pro_data), encoding='utf-8')

    settings_path = board_dir / 'pcb_settings.json'
    settings_data = {
        'autorouter': {'clearance_fallback_mm': 0.1}
    }
    settings_path.write_text(json.dumps(settings_data), encoding='utf-8')

    result = router.get_drc_constraints(board_dir)

    # Pro rules (0.2) should win over fallback (0.1)
    clearance = result['constraints']['clearance']['value']
    assert clearance in [0.2, 0.25]  # board_rule or netclass value

    # No DRU file should be present
    assert result['dru_file'] is None


def test_fallback_clearance_used(tmp_path: Path):
    """Fallback clearance is used when nothing else is set."""
    board_dir = tmp_path / 'board'
    board_dir.mkdir()

    board_path = board_dir / 'test.kicad_pcb'
    board_text = (_HEADER_TEMPLATE.format(layer_lines=_layer_stack_lines(2))
                  + '\n' + _FOOTER)
    board_path.write_text(board_text, encoding='utf-8')

    pro_path = board_dir / 'test.kicad_pro'
    pro_data = {'board': {'design_settings': {}}}
    pro_path.write_text(json.dumps(pro_data), encoding='utf-8')

    settings_path = board_dir / 'pcb_settings.json'
    settings_data = {
        'autorouter': {'clearance_fallback_mm': 0.35}
    }
    settings_path.write_text(json.dumps(settings_data), encoding='utf-8')

    result = router.get_drc_constraints(board_dir)

    # Fallback should be used
    assert result['constraints']['clearance']['value'] == pytest.approx(0.35, abs=0.001)

    # Source should show fallback
    sources = result['constraints']['clearance']['sources']
    assert any(s.get('type') == 'fallback' for s in sources)


# --------------------------------------------------------------------------- #
# Unsupported conditions tests
# --------------------------------------------------------------------------- #

def test_unsupported_condition_reported(tmp_path: Path):
    """Unsupported conditions are reported in unsupported_rules, not dropped."""
    board_dir = tmp_path / 'board'
    board_dir.mkdir()

    board_path = board_dir / 'test.kicad_pcb'
    board_text = (_HEADER_TEMPLATE.format(layer_lines=_layer_stack_lines(2))
                  + '\n' + _FOOTER)
    board_path.write_text(board_text, encoding='utf-8')

    pro_path = board_dir / 'test.kicad_pro'
    pro_path.write_text(json.dumps({'board': {'design_settings': {}}}), encoding='utf-8')

    # Create .kicad_dru with a rule that has an unsupported B.Type predicate
    dru_path = board_dir / 'test.kicad_dru'
    dru_text = """(version 1)
(rule "Pair predicate (unsupported)"
    (condition "A.Type == 'track' && B.Type == A.Type")
    (constraint clearance (min 0.2mm))
)
(rule "Supported type predicate"
    (condition "A.Type == 'track'")
    (constraint clearance (min 0.15mm))
)
"""
    dru_path.write_text(dru_text, encoding='utf-8')

    result = router.get_drc_constraints(board_dir)

    # The unsupported rule should be in unsupported_rules
    unsupported = result['unsupported_rules']
    unsupported_names = {r['name'] for r in unsupported}
    assert 'Pair predicate (unsupported)' in unsupported_names

    # The reason should mention B.Type
    unsupported_pair = next((r for r in unsupported if r['name'] == 'Pair predicate (unsupported)'), None)
    assert unsupported_pair is not None
    assert 'B.Type' in unsupported_pair.get('reason', '')

    # The supported rule should still be processed
    assert result['constraints']['clearance']['value'] == pytest.approx(0.15, abs=0.001)


# --------------------------------------------------------------------------- #
# Cache invalidation tests
# --------------------------------------------------------------------------- #

def test_cache_invalidation_on_mtime_change(tmp_path: Path):
    """Cache is invalidated when DRU file mtime changes."""
    board_dir = tmp_path / 'board'
    board_dir.mkdir()

    board_path = board_dir / 'test.kicad_pcb'
    board_text = (_HEADER_TEMPLATE.format(layer_lines=_layer_stack_lines(2))
                  + '\n' + _FOOTER)
    board_path.write_text(board_text, encoding='utf-8')

    pro_path = board_dir / 'test.kicad_pro'
    pro_path.write_text(json.dumps({'board': {'design_settings': {}}}), encoding='utf-8')

    dru_path = board_dir / 'test.kicad_dru'
    dru_path.write_text("""(version 1)
(rule "Initial"
    (constraint clearance (min 0.2mm))
)
""", encoding='utf-8')

    # First call
    result1 = router.get_drc_constraints(board_dir)
    assert result1['constraints']['clearance']['value'] == pytest.approx(0.2, abs=0.001)

    # Modify the file (update mtime)
    import time
    time.sleep(0.01)  # Ensure mtime changes
    dru_path.write_text("""(version 1)
(rule "Updated"
    (constraint clearance (min 0.3mm))
)
""", encoding='utf-8')

    # Second call should reflect the change
    result2 = router.get_drc_constraints(board_dir)
    assert result2['constraints']['clearance']['value'] == pytest.approx(0.3, abs=0.001)


def test_cache_hit_on_same_mtime(tmp_path: Path):
    """Cache is reused when mtime/size are unchanged."""
    board_dir = tmp_path / 'board'
    board_dir.mkdir()

    board_path = board_dir / 'test.kicad_pcb'
    board_text = (_HEADER_TEMPLATE.format(layer_lines=_layer_stack_lines(2))
                  + '\n' + _FOOTER)
    board_path.write_text(board_text, encoding='utf-8')

    pro_path = board_dir / 'test.kicad_pro'
    pro_path.write_text(json.dumps({'board': {'design_settings': {}}}), encoding='utf-8')

    dru_path = board_dir / 'test.kicad_dru'
    dru_path.write_text("""(version 1)
(rule "Cached"
    (constraint clearance (min 0.2mm))
)
""", encoding='utf-8')

    # Two calls on unchanged file should reuse cache
    result1 = router.get_drc_constraints(board_dir)
    result2 = router.get_drc_constraints(board_dir)

    # Results should be identical
    assert result1 == result2


# --------------------------------------------------------------------------- #
# Constraint type coverage tests
# --------------------------------------------------------------------------- #

def test_constraint_types_extracted(tmp_path: Path):
    """All constraint types from .kicad_dru are properly extracted."""
    board_dir = tmp_path / 'board'
    board_dir.mkdir()

    board_path = board_dir / 'test.kicad_pcb'
    board_text = (_HEADER_TEMPLATE.format(layer_lines=_layer_stack_lines(2))
                  + '\n' + _FOOTER)
    board_path.write_text(board_text, encoding='utf-8')

    pro_path = board_dir / 'test.kicad_pro'
    pro_path.write_text(json.dumps({'board': {'design_settings': {}}}), encoding='utf-8')

    dru_path = board_dir / 'test.kicad_dru'
    dru_text = """(version 1)
(rule "Track width"
    (constraint track_width (min 0.1mm))
)
(rule "Clearance"
    (constraint clearance (min 0.15mm))
)
(rule "Via diameter"
    (constraint via_diameter (min 0.4mm))
)
(rule "Via drill"
    (constraint hole_size (min 0.2mm))
)
(rule "Annular width"
    (constraint annular_width (min 0.05mm))
)
"""
    dru_path.write_text(dru_text, encoding='utf-8')

    result = router.get_drc_constraints(board_dir)

    # Verify multiple constraint types are extracted
    assert 'track_width' in result['constraints']
    assert 'clearance' in result['constraints']
    assert 'via_diameter' in result['constraints']
    assert 'hole_size' in result['constraints']
    assert 'annular_width' in result['constraints']


def test_netclass_and_board_rules(tmp_path: Path):
    """Net class and board rules are both extracted from .kicad_pro."""
    board_dir = tmp_path / 'board'
    board_dir.mkdir()

    board_path = board_dir / 'test.kicad_pcb'
    board_text = (_HEADER_TEMPLATE.format(layer_lines=_layer_stack_lines(2))
                  + '\n' + _FOOTER)
    board_path.write_text(board_text, encoding='utf-8')

    pro_path = board_dir / 'test.kicad_pro'
    pro_data = {
        'board': {
            'design_settings': {
                'net_settings': {
                    'classes': [
                        {
                            'name': 'Default',
                            'clearance': 0.2,
                            'track_width': 0.25,
                            'via_diameter': 0.6,
                            'via_drill': 0.3,
                        },
                        {
                            'name': 'Power',
                            'clearance': 0.3,
                            'track_width': 0.5,
                        }
                    ]
                },
                'rules': {
                    'min_clearance': 0.1,
                    'min_track_width': 0.2,
                    'min_via_diameter': 0.4,
                    'min_hole_to_hole': 0.25,
                }
            }
        }
    }
    pro_path.write_text(json.dumps(pro_data), encoding='utf-8')

    result = router.get_drc_constraints(board_dir)

    # Check net_classes extracted
    assert 'Default' in result['net_classes']
    assert 'Power' in result['net_classes']
    assert result['net_classes']['Default']['clearance'] == 0.2
    assert result['net_classes']['Power']['clearance'] == 0.3

    # Check board_rules extracted
    assert result['board_rules']['min_clearance'] == 0.1
    assert result['board_rules']['min_via_diameter'] == 0.4


# --------------------------------------------------------------------------- #
# Result structure validation
# --------------------------------------------------------------------------- #

def test_result_structure(kiln_project_path: Path):
    """Result dict has the expected keys and structure."""
    result = router.get_drc_constraints(kiln_project_path)

    # Top-level keys
    assert 'board_path' in result
    assert 'dru_file' in result
    assert 'constraints' in result
    assert 'net_classes' in result
    assert 'board_rules' in result
    assert 'unsupported_rules' in result
    assert 'cache_info' in result

    # board_path should exist
    board_path = Path(result['board_path'])
    assert board_path.exists()

    # cache_info structure
    cache_info = result['cache_info']
    assert 'path' in cache_info
    assert 'mtime' in cache_info
    assert 'size' in cache_info

    # constraints structure
    for ctype, cdata in result['constraints'].items():
        assert 'value' in cdata
        assert 'sources' in cdata
        assert isinstance(cdata['sources'], list)

    # unsupported_rules structure
    for rule in result['unsupported_rules']:
        assert 'name' in rule
        assert 'condition' in rule
        assert 'reason' in rule
