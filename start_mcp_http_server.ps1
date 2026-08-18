# Activate venv and start KiCad MCP server in HTTP mode
$VenvScript = Join-Path $PSScriptRoot '.venv\Scripts\Activate.ps1'
if (-not (Test-Path $VenvScript)) {
    Write-Error "Virtual environment not found at $VenvScript. Run setup.ps1 first."
    exit 1
}
& $VenvScript
# port 8765 is taken by kilnctrl's link_hub (tools/PcTools/src/kilnctrl/link_hub.py HUB_PORT) -- do not reuse it
python $PSScriptRoot\kicad_mcp_server.py --transport http --port 8766
