Group 9: Live KiCad IPC Tools
================================

[< Back to README.md](../../README.md)

Backed by `kicad_ipc_tool.py` (the `kicad-python` package). Unlike every other group, these
talk to a **running** KiCad instance over its IPC API instead of parsing `kiln.kicad_pcb` on
disk - for the handful of things only a live session can answer (real geometry, GUI
selection). None of them take `project_path`; they operate on whatever board KiCad currently
has open. Every one fails fast with a clear message if KiCad isn't reachable.

Setup:
1. `pip install -r requirements-mcp.txt` (installs `kicad-python`; the server still starts
   fine without it, it just won't register this group's tools).
2. In KiCad: **Preferences > Plugins > Enable IPC API**.
3. Open `kiln.kicad_pcb` in KiCad and keep it open while using these tools.

There's no "run DRC and get violations" tool here - `kicad-python` (as of 0.7.1) exposes DRC
*rule configuration* but not a way to trigger a check and read back the violation list.

## `get_kicad_ipc_status`
Check whether KiCad's IPC API is reachable right now, and report the connected KiCad version
and which board (if any) is open. Call this first when any other live tool fails, to tell
"KiCad isn't running/API disabled" apart from "component not found."
**Args:** none

## `get_kicad_live_bounding_box`
Real KiCad-computed bounding box (mm) for a footprint, straight from KiCad's own geometry
engine - accounts for actual pad/silkscreen/courtyard shapes and rotation exactly, unlike
`estimate_kicad_footprint_radius`'s circle-from-name heuristic (Group 5). Use for oddly-shaped
parts (connectors, electrolytic cans, relays) where the heuristic is least trustworthy.
**Args:** `reference`, `include_text` (default false - include the footprint's
reference/value silkscreen text in the box)

## `find_kicad_live_layout_collisions`
Live-board analogue of `find_kicad_layout_collisions` (Group 5): same internal (among
references) + external (nearby obstacles) collision check, but using KiCad's own bounding
boxes instead of the file tool's circular-radius estimate - more accurate for oblong parts.
Read-only.
**Args:** `references`, `extra_search_radius` (default 25.0mm), `margin` (default 0.4mm)

## `highlight_kicad_live_components`
Select the given component references in the live KiCad PCB editor window, replacing whatever
is currently selected - so a human reviewing an agent's proposed change can see exactly which
footprints it's about to touch before any write happens. Purely visual; writes still go through
the `write: true` file-based tools in the other groups.
**Args:** `references`

## `clear_kicad_live_highlight`
Clear the current selection in the live KiCad PCB editor window.
**Args:** none

## `get_kicad_live_selection`
Read back whatever is currently selected in the live KiCad PCB editor - so a person can point
at a component by hand in the GUI instead of typing its reference designator for a follow-up
tool call.
**Args:** none
