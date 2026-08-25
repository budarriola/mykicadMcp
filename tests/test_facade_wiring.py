"""Tests for the KiCad MCP server's search-facade wiring.

`tests/test_mcpkit_facade.py` (in the parent `tools/PcTools` repo) already
covers the registry internals (`collapse`/`collapse_table`, search scoring,
argument coercion, suggestion ranking) against a small synthetic tool set --
this file does not repeat any of that. What is specific to `mykicadMcp` is
the *wiring*: that `KiCadMcpServer.__init__` actually calls `collapse_table`
with `kicad_facade`'s tables and ends up with the right six-tool surface,
that every withdrawn tool is still reachable through the registry, that the
taxonomy in `kicad_facade.py` covers every real tool name (so a newly added
tool cannot silently fall into a junk group), and that the hand-rolled
JSON-RPC `handle()` method serializes facade results as plain text instead
of double-encoding them (the regression this whole change was fixing).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

import kicad_facade
from kicad_mcp_server import KiCadMcpServer, DEFAULT_HTTP_PORT, parse_args

_REPO_ROOT = Path(__file__).resolve().parent.parent
_KILN_PRO = None
for _candidate in (_REPO_ROOT, *_REPO_ROOT.parents):
    _p = _candidate / "hardware" / "mainBoard" / "kiln.kicad_pro"
    if _p.exists():
        _KILN_PRO = _p
        break

_FACADE_NAMES = {"inspect_kicad_project", "get_kicad_ipc_status",
                  "kicad_help", "kicad_find", "kicad_describe", "kicad_call", "kicad_batch"}


@pytest.fixture(scope="module")
def server() -> KiCadMcpServer:
    return KiCadMcpServer()


def test_published_tools_are_exactly_facade_plus_keep(server: KiCadMcpServer) -> None:
    expected = _FACADE_NAMES | set(kicad_facade.KEEP)
    assert set(server.tools) == expected


def test_registry_still_holds_withdrawn_tools(server: KiCadMcpServer) -> None:
    sample = (
        "audit_kicad_capacitor_voltages",
        "get_kicad_component_connections",
        "list_kicad_nets",
    )
    for name in sample:
        assert name in server.registry.by_name, name
        # None of these should still be directly published.
        assert name not in server.tools
    assert len(server.registry.entries) > 50


def test_taxonomy_covers_every_registered_tool(server: KiCadMcpServer) -> None:
    """Every tool the registry knows about must land in a group that the
    taxonomy actually names -- otherwise a future tool could silently fall
    into `derive_group`'s fallback ("split on first underscore" or "misc")
    without anyone noticing."""
    known_groups = set(kicad_facade.GROUP_OVERRIDES.values())
    known_groups.update(group for _prefix, group in kicad_facade.GROUP_PREFIXES)
    stray = [
        (entry.name, entry.group)
        for entry in server.registry.entries
        if entry.group not in known_groups
    ]
    assert not stray, f"tools landed outside the known taxonomy groups: {stray}"


def test_initialize_instructions_name_kicad_find(server: KiCadMcpServer) -> None:
    result = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert result is not None
    assert "kicad_find" in result["result"]["instructions"]


def test_tools_list_is_six_plus_keep_with_valid_schemas(server: KiCadMcpServer) -> None:
    result = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tools = result["result"]["tools"]
    names = {t["name"] for t in tools}
    assert names == _FACADE_NAMES | set(kicad_facade.KEEP)
    for tool in tools:
        assert tool["description"], tool["name"]
        assert tool["inputSchema"].get("type") == "object", tool["name"]


def test_kicad_find_returns_plain_text_not_json_encoded(server: KiCadMcpServer) -> None:
    result = server.handle({
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "kicad_find", "arguments": {"query": "capacitor voltage"}},
    })
    text = result["result"]["content"][0]["text"]
    # A JSON-double-encoded string would start with a literal `"` (and often
    # contain escaped `\n`); plain text never does.
    assert not text.startswith('"')


def test_kicad_call_dispatches_a_real_readonly_tool(server: KiCadMcpServer) -> None:
    if _KILN_PRO is None:
        pytest.skip("hardware/mainBoard/kiln.kicad_pro not found relative to repo root")
    result = server.handle({
        "jsonrpc": "2.0", "id": 4, "method": "tools/call",
        "params": {
            "name": "kicad_call",
            "arguments": {
                "name": "inspect_kicad_project",
                "args": {"project_path": str(_KILN_PRO)},
            },
        },
    })
    text = result["result"]["content"][0]["text"]
    assert "error: no tool named" not in text
    assert text.strip()


def test_kicad_call_misspelled_name_suggests_closest(server: KiCadMcpServer) -> None:
    result = server.handle({
        "jsonrpc": "2.0", "id": 5, "method": "tools/call",
        "params": {
            "name": "kicad_call",
            "arguments": {"name": "list_kicad_componants"},
        },
    })
    text = result["result"]["content"][0]["text"]
    assert text.startswith("error:")
    assert "Closest:" in text


def test_parse_args_defaults_to_http_8766(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["kicad_mcp_server.py"])
    args = parse_args()
    assert args.transport == "http"
    assert args.port == 8766
    assert args.port == DEFAULT_HTTP_PORT
