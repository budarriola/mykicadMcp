"""M0 acceptance test #3: prove KiCad itself (not just our own parser) accepts
a generated synthetic board.

There's no headless "open in pcbnew" driver available here, so this uses
`kicad-cli pcb drc` as the substitute acceptance check the plan calls for:
DRC has to fully load and parse the board file through KiCad's own board
reader before it can run any checks at all, so a clean run (not a "Failed to
load board" error) is proof the file is genuinely well-formed KiCad-10-shape
`.kicad_pcb`, not just something our own lenient s-expr parser tolerates.

Skipped outright if kicad-cli can't be found anywhere on this machine (PATH,
then the standard Windows KiCad install locations) - this is a machine
capability check, not a pytest requirement.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from synthetic_board import write_fanout_field_board, write_synthetic_board


def _find_kicad_cli() -> str | None:
    on_path = shutil.which("kicad-cli") or shutil.which("kicad-cli.exe")
    if on_path:
        return on_path
    # Not on PATH here, but KiCad 10 is installed - fall back to the standard
    # Windows install locations rather than skipping a check that's actually
    # runnable on this machine.
    candidates = list(Path("C:/Program Files/KiCad").glob("*/bin/kicad-cli.exe"))
    candidates += list(Path("C:/Program Files (x86)/KiCad").glob("*/bin/kicad-cli.exe"))
    import os

    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        candidates += list(Path(local_appdata, "Programs", "KiCad").glob("*/bin/kicad-cli.exe"))
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return None


_KICAD_CLI = _find_kicad_cli()

pytestmark = pytest.mark.skipif(
    _KICAD_CLI is None,
    reason="kicad-cli not found on PATH or in standard KiCad install locations",
)


def _run_drc(board_path: Path, report_path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            _KICAD_CLI,
            "pcb",
            "drc",
            "--format",
            "json",
            "--severity-all",
            str(board_path),
            "-o",
            str(report_path),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_kicad_cli_accepts_generated_simple_board(tmp_path: Path) -> None:
    board_path = write_synthetic_board(tmp_path / "synthetic.kicad_pcb", component_count=6)
    report_path = tmp_path / "drc.json"

    result = _run_drc(board_path, report_path)

    assert result.returncode == 0, (
        f"kicad-cli failed to load the generated board (stderr: {result.stderr!r})"
    )
    assert "Failed to load board" not in result.stderr
    assert report_path.exists()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert "violations" in report
    assert report.get("kicad_version")


def test_kicad_cli_accepts_generated_fanout_field_board(tmp_path: Path) -> None:
    board_path = write_fanout_field_board(
        tmp_path / "fanout.kicad_pcb", component_count=3, pads_per_component=32
    )
    report_path = tmp_path / "drc_fanout.json"

    result = _run_drc(board_path, report_path)

    assert result.returncode == 0, (
        f"kicad-cli failed to load the generated fanout board (stderr: {result.stderr!r})"
    )
    assert "Failed to load board" not in result.stderr
    assert report_path.exists()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert "violations" in report


def test_kicad_cli_accepts_n_layer_board(tmp_path: Path) -> None:
    board_path = write_synthetic_board(
        tmp_path / "sixlayer.kicad_pcb", component_count=4, layers=6
    )
    report_path = tmp_path / "drc_6layer.json"

    result = _run_drc(board_path, report_path)

    assert result.returncode == 0, (
        f"kicad-cli failed to load the 6-layer generated board (stderr: {result.stderr!r})"
    )
    assert "Failed to load board" not in result.stderr
