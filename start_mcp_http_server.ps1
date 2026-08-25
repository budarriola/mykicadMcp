# Start the KiCad MCP server over HTTP on 127.0.0.1:8766.
#
# Kept as the standalone entry point for anyone using this submodule on its own.
# Inside the kilnCtl parent repo, prefer
#
#     tools\PcTools\scripts\mcp_servers.ps1 start|stop|restart|status -Server kicad
#
# which drives this server and the two PcTools servers through the same
# /health and /shutdown routes, and which is what the editor's status-bar
# buttons call. This script is the same launch with none of that around it.
#
# Port 8766, not 8765: 8765 is kilnCtl's link_hub (tools/PcTools/src/kilnctrl/
# link_hub.py HUB_PORT). 8767 and 8768 are the kilnctrl and kilnsim servers.

$ErrorActionPreference = "Stop"

$Python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $Python)) {
    Write-Error "Virtual environment not found at $Python. Run setup.ps1 first."
    exit 1
}

# The interpreter directly, rather than Activate.ps1 then a bare `python`:
# activation mutates this shell for whatever runs after it, and picking the
# venv's python by path cannot select the wrong one.
& $Python (Join-Path $PSScriptRoot 'kicad_mcp_server.py') --transport http --port 8766
