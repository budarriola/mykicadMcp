# Group 1: Project Inspection & PCB/Netlist

[< Back to README.md](../../README.md)

Read-only queries against `kiln.kicad_pcb` and its netlist. Backed by `kicad_pcb_tool.py`,
which parses the board file directly - no KiCad runtime required. The board is parsed once
and cached in-process, invalidated automatically if the file changes on disk.

## `inspect_kicad_project`
Inspect a KiCad project directory or board file and return a summary (component/net counts,
board metadata).
**Args:** `project_path`

## `list_kicad_components`
List components from a KiCad PCB file.
**Args:** `project_path`, `limit` (default 50)

## `get_kicad_component`
Get a specific component by its reference designator.
**Args:** `project_path`, `reference`

## `get_kicad_component_connections`
Get the net connections for a specific component reference.
**Args:** `project_path`, `reference`

## `list_kicad_nets`
List every net in the netlist.
**Args:** `project_path`

## `get_kicad_net`
Get details (connected pins) for one net by name.
**Args:** `project_path`, `net_name`

## `find_kicad_components_by_net`
Find every component connected to a specific net.
**Args:** `project_path`, `net_name`

## `find_kicad_components_by_pin_connection`
Find components that connect to a specific pin on a given component reference - i.e. "what's
on the other end of this wire."
**Args:** `project_path`, `reference`, `pin`

## `search_kicad_component`
Find a component's line numbers in the raw `.kicad_pcb` file by reference designator, to
locate its section without reading the entire (multi-megabyte) file.
**Args:** `project_path`, `reference`

## `get_kicad_footprint_pads`
Get every pad of a footprint - number, net (read straight off the board file's own pad
entries, not the schematic pin numbering), and absolute board position. Use whenever a
placement decision depends on exactly where a pin is, not just the footprint's origin.
**Args:** `project_path`, `reference`

## `get_kicad_pin_position`
Look up one pad's net and absolute board position by reference + pin number.
**Args:** `project_path`, `reference`, `pin`

## `get_kicad_pin_distance`
Euclidean distance (mm) between two specific pads - useful for checking a placement's quality
before/after, e.g. confirming a bypass cap's pad ended up closer to the IC pin it bypasses.
**Args:** `project_path`, `reference_a`, `pin_a`, `reference_b`, `pin_b`
