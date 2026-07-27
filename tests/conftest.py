"""Shared pytest fixtures for the mykicadMcp test suite.

`kiln_project_path` points at a COMMITTED (git HEAD) snapshot of the kilnCtl
board, materialized once per session into a temp dir - NOT the live working-tree
board. The golden-file tests assert fixed reference stats (6 zones, 39 missing
connections, ...); pinning them to the committed board keeps them stable while
the user actively edits `kiln.kicad_pcb` in KiCad (a live board with WIP zones/
routes would spuriously fail these invariants). Set `KILN_USE_LIVE_BOARD=1` to
override and use the live working-tree board instead. `scratch_board` copies the
(committed) board/project/netlist files into `tmp_path` so writer tests never
touch the real files.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

# `kicad_pcb_tool.py` lives one directory up from `tests/` and is not part of
# an installed package, so make sure it's importable regardless of how pytest
# was invoked (from `mykicadMcp/`, from the repo root, or via `pytest tests/`).
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# The real kilnCtl KiCad project normally lives one directory above mykicadMcp/.
# When mykicadMcp is checked out as an isolated agent worktree (e.g.
# `mykicadMcp/.claude/worktrees/agent-<id>/`), the submodule's own repo root is
# nested several levels below the real kilnCtl checkout instead of exactly one
# level below it. Walk upward looking for the kilnCtl project marker
# (`kiln.kicad_pro`, which only ever lives at the real project root) instead of
# assuming a fixed nesting depth, so this resolves correctly in both layouts.
def _find_kiln_project_dir(start: Path) -> Path:
    for candidate in (start, *start.parents):
        if (candidate / "kiln.kicad_pro").exists():
            return candidate
    return start.parent  # fallback: preserve old (non-worktree) behavior


_KILN_PROJECT_DIR = _find_kiln_project_dir(_REPO_ROOT)


# Board/project files pinned to the git-committed version; everything else the
# golden tests need (JLCPCB.kicad_dru.txt, fp-lib-table, ...) is copied from the
# live dir as-is (those don't drift with a user's board zone/route edits).
_COMMITTED_FILES = ("kiln.kicad_pcb", "kiln.kicad_prl", "kiln.kicad_pro", "kiln.net")


def _to_crlf(data: bytes) -> bytes:
    """Normalize to CRLF - `git show` yields the LF-normalized blob, but a real
    KiCad board on Windows (and what `git checkout` materializes here) is CRLF.
    Some write paths (e.g. unroute block removal) are CRLF-sensitive, so the
    reference snapshot must match the real on-disk line endings."""
    return data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")


def _materialize_committed_board(dest: Path) -> bool:
    """Populate `dest` with a COMMITTED (git HEAD) snapshot of the board: aux
    project files copied from the live dir, then the board/project files
    overwritten with their git HEAD versions (CRLF-normalized). Returns True if
    the committed .kicad_pcb was extracted. Pure read of the repo (`git show`);
    never mutates the working tree or the live board."""
    for f in _KILN_PROJECT_DIR.iterdir():
        if f.is_file() and not f.name.startswith("_autosave-"):
            try:
                shutil.copy2(f, dest / f.name)
            except OSError:
                pass
    got_board = False
    for name in _COMMITTED_FILES:
        try:
            res = subprocess.run(
                ["git", "-C", str(_KILN_PROJECT_DIR), "show", f"HEAD:{name}"],
                capture_output=True, timeout=60,
            )
        except Exception:
            continue
        if res.returncode == 0 and res.stdout:
            (dest / name).write_bytes(_to_crlf(res.stdout))
            if name == "kiln.kicad_pcb":
                got_board = True
    return got_board


@pytest.fixture(scope="session")
def kiln_project_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Directory holding a COMMITTED (git HEAD) snapshot of the kiln board.

    Materialized once per session so the golden tests run against a stable,
    version-controlled board even while the live `kiln.kicad_pcb` is being edited
    in KiCad. Read-only fixture: tests using this directly must never write
    through it. Use `scratch_board` for anything that mutates board/project files.
    Falls back to the live working-tree directory when `KILN_USE_LIVE_BOARD=1` or
    when the committed board can't be extracted (e.g. not a git checkout)."""
    live_board = _KILN_PROJECT_DIR / "kiln.kicad_pcb"
    if os.environ.get("KILN_USE_LIVE_BOARD") == "1":
        if not live_board.exists():
            pytest.skip(f"Live kiln board not found at {live_board}.")
        return _KILN_PROJECT_DIR
    snapshot = tmp_path_factory.mktemp("kiln_committed")
    if _materialize_committed_board(snapshot):
        return snapshot
    # Fallback: no git snapshot available -> use the live board if present.
    if not live_board.exists():
        pytest.skip("Neither a committed nor a live kiln board is available.")
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
