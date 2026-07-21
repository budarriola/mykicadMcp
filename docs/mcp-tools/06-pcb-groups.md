Group 6: PCB Groups (Ctrl+G)
==============================

[< Back to README.md](../../README.md)

Don't confuse a **PCB group** with the *hierarchical* group from
[04-hierarchical-groups.md](04-hierarchical-groups.md): `get_kicad_hierarchical_group` finds a
sub-circuit's members via the schematic path, purely for computing layout diffs - it's a
query, not a board-file construct. A PCB group is the actual
`(group "name" (uuid ..) (members ..))` block KiCad writes to the board file, which is what
makes a cluster of footprints select/move together as one unit in the GUI. The two are
independent - a hierarchical group's members aren't grouped in the PCB sense until you
explicitly `create_kicad_group` them.

Typical flow for "make each instance of a repeated sub-circuit its own group":
1. `list_kicad_hierarchical_templates()` or `get_kicad_hierarchical_group(reference=...)` to
   find each instance's member references.
2. `list_kicad_groups()` to check nothing you're about to group is already in another group
   (KiCad groups don't nest/overlap).
3. `create_kicad_group(name=..., references=[...], write=false)` per instance to preview, then
   `write=true` to save.
4. `delete_kicad_group(name=... or group_uuid=..., write=false/true)` to undo.

## `list_kicad_groups`
List every top-level PCB group already on the board. Each member uuid is resolved back to its
reference designator. Use before `create_kicad_group` to check whether components are already
grouped, or to find a group's exact name/uuid before deleting it.
**Args:** `project_path`

## `create_kicad_group`
Create a new named PCB group containing the given footprint references, so they select/move
together as one unit in the KiCad GUI - the same construct KiCad itself writes for Ctrl+G.
Raises if a reference isn't found on the board, or already belongs to another group.
**Args:** `project_path`, `name` (can be `""`, matching KiCad's default for GUI-created
groups), `references`, `write` (default false), `allow_while_open` (default false)

## `delete_kicad_group`
Delete a top-level PCB group by name or uuid - only the grouping is removed, member footprints
are untouched. Give `group_uuid` when multiple groups share a name (common for unnamed `""`
groups); `name` alone must match exactly one group.
**Args:** `project_path`, `name` (optional), `group_uuid` (optional), `write` (default false),
`allow_while_open` (default false)
