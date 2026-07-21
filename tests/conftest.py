"""Shared pytest fixtures for the mykicadMcp test suite.

`kiln_project_path` points at the REAL kilnCtl project directory (read-only
use only - golden-file tests against the actual board). `scratch_board` copies
the real board/project/netlist files into `tmp_path` so that any test which
writes to a `.kicad_pcb`/`.kicad_pro` never touches the real files.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

# `kicad_pcb_tool.py` lives one directory up from `tests/` and is not part of
# an installed package, so make sure it's importable regardless of how pytest
# was invoked (from `mykicadMcp/`, from the repo root, or via `pytest tests/`).
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# The real kilnCtl KiCad project lives one directory above mykicadMcp/.
_KILN_PROJECT_DIR = _REPO_ROOT.parent


@pytest.fixture(scope="session")
def kiln_project_path() -> Path:
    """Path to the real kilnCtl project directory (contains kiln.kicad_pcb).

    Read-only fixture: tests using this directly must never write through it.
    Use `scratch_board` for anything that mutates board/project files.
    """
    board = _KILN_PROJECT_DIR / "kiln.kicad_pcb"
    if not board.exists():
        pytest.skip(f"Real kiln board not found at {board}; skipping golden tests.")
    return _KILN_PROJECT_DIR


@pytest.fixture
def scratch_board(tmp_path: Path, kiln_project_path: Path) -> Path:
    """Copy kiln.kicad_pcb (+ kiln.kicad_pro + kiln.net, when present) into a
    fresh tmp_path directory and return that directory's path.

    Writer/round-trip tests should always operate on this copy, never on the
    real project files under `kiln_project_path`.
    """
    names = ["kiln.kicad_pcb", "kiln.kicad_pro", "kiln.net"]
    copied_any = False
    for name in names:
        src = kiln_project_path / name
        if src.exists():
            shutil.copy2(src, tmp_path / name)
            copied_any = True
    if not copied_any:
        pytest.skip("No kiln board/project/netlist files found to copy into scratch dir.")
    return tmp_path
