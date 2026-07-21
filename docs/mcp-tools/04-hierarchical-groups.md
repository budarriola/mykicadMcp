Group 4: Hierarchical Groups & Sibling Discovery
==================================================

[< Back to README.md](../../README.md)

This project has several hierarchical schematic sheets stamped out multiple times on the
board (relay outputs, thermocouple inputs, current-sense channels, regulators, etc). These
tools find which footprints belong to one instance of a repeated sub-circuit, and match
members across different instances/sheets - the discovery step that precedes the layout,
label-position, and flip tools in Groups 5/7/8.

## `list_kicad_hierarchical_templates`
Board-wide overview, in one call, of every schematic sheet stamped out more than once (one row
per repeated sheet file, with every instance's member references and whether it's the
fully-locked reference layout). Run this **first** on any "make these repeated sub-circuits
consistent" task instead of exploring with search/grep - it replaces the manual discovery work
of figuring out which components belong together and which instance is the reference.
**Args:** `project_path`

## `get_kicad_hierarchical_group`
Given a component reference, return every other footprint that belongs to the same
hierarchical-sheet instance (e.g. all the parts of one relay channel), matched via the
schematic path rather than board position. Use before reorganizing a repeated sub-circuit's
layout, to get its true member list without guessing from proximity.
**Args:** `project_path`, `reference`, `verbose` (default false - only include full KiCad
properties like Datasheet/Mouser/Sim.* fields if actually needed; it's the largest cost in the
response)

## `list_kicad_sibling_instances`
Given a component reference, find every other instance of the same hierarchical schematic
sheet (e.g. given one relay channel, list the other channels stamped from the same template
page), with each sibling's own anchor reference/position.
**Args:** `project_path`, `reference`

## `classify_kicad_group_by_anchor_pin`
For every other member of a hierarchical group, find which of the anchor's own pads it shares
a net with - i.e. its electrical role (VIN cap, feedback divider resistor, etc), read straight
off board nets. Automatic version of hand-building a "which part goes with which IC pin"
table - usually you want `match_kicad_group_members_by_role` or `diff_kicad_layout_by_role`
instead of calling this directly.
**Args:** `project_path`, `anchor_reference`

## `match_kicad_group_members_by_role`
Match components between two hierarchical groups by which anchor pin they connect to, instead
of KiCad's `symbol_uuid` (which only works between instances of the *same* schematic sheet).
Works even when the two groups are on entirely different sheet files, as long as their anchors
share a compatible pinout - e.g. two independently-drawn but functionally analogous regulator
circuits. Ties (more than one same-footprint candidate on either side) are broken by matching
component value; anything still tied comes back under `ambiguous` instead of being guessed -
pass `overrides` to force those once you've eyeballed which is which.
**Args:** `project_path`, `template_reference`, `target_reference`,
`overrides` (optional `{template_reference: target_reference}` map for ambiguous ties)
