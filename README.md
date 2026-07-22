# KiCad AI Tooling

This workspace includes a lightweight KiCad inspector that reads the schematic, PCB, and
netlist files directly, without requiring a full KiCad runtime. It's exposed as a local MCP
server (`kicad_mcp_server.py`) so any MCP-capable AI assistant (Claude Code, VS Code, etc.) can
query and edit the board.

## Files

- [kicad_pcb_tool.py](kicad_pcb_tool.py) - parses `.kicad_pcb` layout and netlist data into
  structured JSON-friendly output. No KiCad runtime required.
- [kicad_mouser_tool.py](kicad_mouser_tool.py) - Mouser Search API sourcing/stock/pricing
  lookups. Requires a free `MOUSER_API_KEY` in the repo-root `.env` (see `.env.example`).
- [kicad_ipc_tool.py](kicad_ipc_tool.py) - talks to a *running* KiCad instance over its IPC API
  (via `kicad-python`) instead of parsing files on disk; backs the live tools in Group 9 below.
- [kicad_mcp_server.py](kicad_mcp_server.py) - the MCP server itself; registers all 72 tools and
  serves them over stdio or HTTP transport.
- [requirements-mcp.txt](requirements-mcp.txt) - Python dependencies (`mcp>=1.0.0` required;
  `kicad-python>=0.7.0` optional - enables the live IPC tools, otherwise they're just left
  unregistered).
- [.vscode/mcp.json](.vscode/mcp.json) - example MCP client config (HTTP transport).
- [docs/mcp-tools/](docs/mcp-tools/) - full per-tool reference, one file per group (see below).

## Setup

1. Ensure Python 3 is installed and in `PATH`.
2. Activate the virtual environment:
   ```powershell
   .venv\Scripts\Activate.ps1
   ```
3. Install dependencies (if needed):
   ```powershell
   pip install -r requirements-mcp.txt
   ```
4. Run the server:
   ```powershell
   # stdio (default) - point your MCP client's command at this
   python kicad_mcp_server.py

   # HTTP - matches the example in .vscode/mcp.json
   python kicad_mcp_server.py --transport http --port 8765
   ```
5. Point your MCP client at it, e.g. `.vscode/mcp.json`:
   ```json
   {
     "servers": {
       "kiln-kicad": { "type": "http", "url": "http://127.0.0.1:8765" }
     }
   }
   ```
6. For the live IPC tools (Group 9): in KiCad, enable **Preferences > Plugins > Enable IPC
   API**, then open `kiln.kicad_pcb` and keep it open while using those tools.

## Example prompts

- "Inspect the KiCad project in this workspace"
- "List the components on the PCB"
- "Show me component R1 and its connections"
- "Provide details for net /MainControler/CLK"
- "Make the thermocouple channel layouts match the locked reference instance"

## Tool reference

Most tools take a `project_path` (a KiCad project directory, `.kicad_pro`, `.kicad_pcb`, or
`.kicad_sch` path - any of these resolve to the same project). Anything that edits a file
defaults to `write: false` (a dry run that returns a preview) - inspect the result, then call
again with `write: true` to actually save. Board-editing tools also accept
`allow_while_open: false`, which by default refuses to write while KiCad has the board open for
editing (to avoid racing the GUI's own unsaved state); see
[05-layout-and-placement.md](docs/mcp-tools/05-layout-and-placement.md) for detail on that guard.

### Groups

| Group | File | What it covers |
|---|---|---|
| 1. Project Inspection & PCB/Netlist | [01-inspection-and-netlist.md](docs/mcp-tools/01-inspection-and-netlist.md) | Read-only board/netlist queries: components, nets, pads, pin positions - no KiCad runtime required. |
| 2. Schematic Data & Property Maintenance | [02-schematic-data.md](docs/mcp-tools/02-schematic-data.md) | Reading/writing schematic symbol properties (Value, Footprint, Manufacturer_Part_Number, etc), integrity/capacitor-voltage/part-spec audits. |
| 3. Mouser Sourcing & BOM | [03-mouser-sourcing.md](docs/mcp-tools/03-mouser-sourcing.md) | Mouser link discovery, live stock/price lookups, alternate-link ranking, stock sufficiency, stock/lifecycle reports, buy-list generation, and the one-call schematic health check. Most of these REQUIRE `MOUSER_API_KEY`. |
| 4. Hierarchical Groups & Sibling Discovery | [04-hierarchical-groups.md](docs/mcp-tools/04-hierarchical-groups.md) | Finding which footprints belong to one repeated sub-circuit instance, and matching members across sheets/instances. |
| 5. Layout & Placement | [05-layout-and-placement.md](docs/mcp-tools/05-layout-and-placement.md) | Moving/aligning footprints, copying a known-good instance's layout onto siblings, collision checks. |
| 6. PCB Groups (Ctrl+G) | [06-pcb-groups.md](docs/mcp-tools/06-pcb-groups.md) | Creating/listing/deleting the board-file grouping construct KiCad's GUI uses for "select as one unit." |
| 7. Silkscreen Label Position Templates | [07-label-position-templates.md](docs/mcp-tools/07-label-position-templates.md) | Copying a hand-decluttered text property's (Reference/Value label) offset onto sibling instances. |
| 8. Footprint Flip Templates | [08-footprint-flip-templates.md](docs/mcp-tools/08-footprint-flip-templates.md) | Copying a correctly front/back-flipped footprint's full flip state onto siblings that need the same treatment. |
| 9. Live KiCad IPC Tools | [09-live-ipc-tools.md](docs/mcp-tools/09-live-ipc-tools.md) | Tools that talk to a *running* KiCad instance (real geometry, GUI selection/highlighting) instead of parsing files on disk. |
| 10. Net Classes & Buses | [10-netclasses-and-buses.md](docs/mcp-tools/10-netclasses-and-buses.md) | Bus detection from schematic net names, net class proposal/creation, trace-cost scoring (with live deviation measurement), bus corridor-area measurement, capacitor voltage auditing, and pcb_settings.json management for routing policy. |

### Picking the right group

- Just need to read board/schematic data? Start with **Group 1** (PCB/netlist) or **Group 2**
  (schematic symbol properties) depending on whether you need footprint/net facts or the
  properties KiCad stores on the schematic symbol itself (Datasheet, Mouser fields, MPN, etc).
- Working on parts sourcing, stock, pricing, or a buy list? **Group 3**.
- Checking the schematic for errors before ordering parts or sending the board to fab? Start
  with `audit_kicad_schematic_health` in **Group 3** - it runs Group 2's integrity/voltage/spec
  audits and Group 3's stock-sufficiency check in one call and writes a Markdown summary.
- Making a repeated sub-circuit (relay channel, thermocouple input, regulator block, etc)
  consistent across instances? Start with **Group 4** to find/match members, then **Group 5**
  (position), **Group 7** (label offsets), and/or **Group 8** (front/back flip) depending on
  what needs copying.
- Want a human reviewing a change to see it happen live, or want to point at a component in the
  GUI instead of typing its reference? **Group 9** - but it needs KiCad open with the IPC API
  enabled (Preferences > Plugins > Enable IPC API); everything else works from the files on disk
  alone.

## Reorganizing a repeated sub-circuit's layout

This project has several hierarchical sheets that get stamped out multiple times on the board
(relay outputs, thermocouple inputs, current-sense channels, regulators, etc). When one instance
has a known-good layout (often marked `locked` in the PCB) and the others need to match it, use
the layout-template tools instead of hand-computing offsets:

0. `list_kicad_hierarchical_templates()` - **start here.** One call, no reference needed,
   returns every repeated schematic sheet on the board with each instance's member references
   and whether it's the fully-locked reference layout. This replaces grepping/reading the file
   to figure out what belongs together and which instance to copy from - do this before
   anything else on a "make these repeated sub-circuits consistent" task.
1. `get_kicad_hierarchical_group(reference=<anchor of the good layout>)` - lists every footprint
   that belongs with that instance, matched via the schematic `path` rather than board
   proximity. This avoids accidentally grabbing an unrelated component that just happens to sit
   nearby on the board. Returns a trimmed view by default (position/uuid/locked/footprint only,
   no Datasheet URLs or Mouser part numbers); pass `verbose: true` only if you actually need full
   KiCad properties.
2. `list_kicad_sibling_instances(reference=<same anchor>)` - lists every other instance of the
   same schematic sheet (the other channels), with each one's own anchor reference and position.
3. `diff_kicad_layout_template(template_reference=<good anchor>, target_reference=<channel to fix>)`
   - dry-run preview of where every matching component in the target channel should move to
   reproduce the template's relative layout. If the target channel's anchor has a different
   rotation than the template's, the whole offset pattern is rotated to match (so components
   don't end up mirrored or on the wrong side).
4. `apply_kicad_layout_template(template_reference=..., target_references=[...], write=false)` to
   preview across every target channel at once, then call again with `write=true` to actually
   save. Always dry-run first.

Matching between template and target is done by the footprint's schematic symbol identity, not
by reference name or physical distance - two components on different schematic sheets that
merely sit close together on the board will never be confused for one another.

## Relocating an already-correct cluster (no template needed)

If there's no separate known-good layout to copy from - you just need to move a whole group
somewhere else on the board, or nudge it a few mm to clear a routing conflict - use
`move_kicad_group(reference=<any member>, dx=, dy=)` (or `to: {x, y}` for an absolute anchor
position, `drotation` to rotate the whole group in place). It moves every member of that
component's hierarchical group together, preserving their layout relative to each other.
Defaults to `write=false`; dry-run first.

## Creating/managing PCB groups (the Ctrl+G GUI construct)

Don't confuse this with the *hierarchical* group above - `get_kicad_hierarchical_group` finds a
sub-circuit's members via the schematic path, purely for computing layout diffs; it's a query,
not a board-file construct. A **PCB group** is the actual `(group "name" (uuid ..) (members ..))`
block KiCad writes to the board file, which is what makes a cluster of footprints select/move
together as one unit in the GUI. The two are independent - a hierarchical group's members aren't
grouped in the PCB sense until you explicitly `create_kicad_group` them.

Typical flow for "make each instance of this repeated sub-circuit its own group":
1. `list_kicad_hierarchical_templates()` or `get_kicad_hierarchical_group(reference=...)` to
   find each instance's member references.
2. `list_kicad_groups()` to check nothing you're about to group is already in another group
   (KiCad groups don't nest/overlap).
3. `create_kicad_group(name=..., references=[...], write=false)` per instance to preview, then
   `write=true` to save. Raises if a reference isn't found on the board, or if it's already a
   member of an existing group.
4. `delete_kicad_group(name=... or group_uuid=..., write=false/true)` to undo - only the grouping
   is removed, member footprints are untouched. Pass `group_uuid` when several groups share a
   name (KiCad's own GUI-created groups are usually named `""`).

Like the layout tools, `create_kicad_group`/`delete_kicad_group` do targeted text surgery keyed
off footprint/group uuids rather than a full parse-mutate-reserialize - safe on a multi-megabyte
board file, and preserves the file's existing CRLF/LF line endings so the diff stays limited to
the lines that actually changed.

## Making one-off edits without a lookup round trip

`apply_kicad_layout_changes` and the `changes` it takes from
`diff_kicad_layout_template`/`move_kicad_group` accept either `uuid` or `reference` per change -
if you already know a component's designator, you can write `{"reference": "R33", "new_position":
{...}}` directly instead of first calling `get_kicad_component` to look up its uuid.

## Notes

- The parser reads the KiCad PCB and netlist files directly, so it does not require a full KiCad
  runtime.
- The board file is parsed once and cached in-process (invalidated automatically if the file
  changes on disk, by anyone or anything - not just this tool), so repeated calls within one
  session don't re-parse a multi-megabyte board file each time.
- There's no `run DRC and get violations` tool - `kicad-python` (as of 0.7.1) exposes DRC *rule
  configuration* but not a way to trigger a check and read back the violation list, so that
  capability doesn't exist yet on this stack.

## Live tools (KiCad IPC API)

Most tools (`kicad_pcb_tool.py`) parse `kiln.kicad_pcb` directly and never need KiCad to be
running. A second, smaller set of tools (`kicad_ipc_tool.py`) instead talks to a *running* KiCad
instance over its IPC API, via the `kicad-python` package - for the handful of things only a live
KiCad session can answer:

- **get_kicad_live_bounding_box** - KiCad's own computed bounding box for a footprint (real
  pad/silkscreen/courtyard geometry and exact rotation), instead of
  `estimate_kicad_footprint_radius`'s circle-from-footprint-name heuristic.
- **find_kicad_live_layout_collisions** - the same collision check as
  `find_kicad_layout_collisions`, but built on real bounding boxes instead of radii - more
  accurate for oblong parts (connectors, electrolytic cans, relays).
- **highlight_kicad_live_components** / **clear_kicad_live_highlight** - select components in the
  live PCB editor so a human can see what an agent is about to change before any write happens.
  Purely visual - writes still go through the `write=true` file-based tools.
- **get_kicad_live_selection** - read back whatever's currently selected in the GUI, so a person
  can point at a component by hand instead of typing its reference.
- **get_kicad_ipc_status** - checks connectivity; call this first if any live tool fails, to tell
  "KiCad isn't reachable" apart from "component not found".
