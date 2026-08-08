Group 5: Layout & Placement
=============================

[< Back to README.md](../../README.md)

Moving, aligning, and collision-checking footprints on `kiln.kicad_pcb`. Every tool that
writes defaults to `write: false` (a dry run returning a preview) - always call it that way
first, review the result, then call again with `write: true` to actually modify the board
file. Board-editing tools also default `allow_while_open: false`, which refuses to write while
KiCad has the board open for editing (see `get_kicad_ipc_status` in
[09-live-ipc-tools.md](09-live-ipc-tools.md)) to avoid racing the GUI's own unsaved state; set
it `true` to skip that check. Writes do targeted text surgery keyed off footprint/group uuids
rather than a full parse-mutate-reserialize, so they're safe on a multi-megabyte board file and
preserve the file's existing line endings.

## `diff_kicad_layout_template`
Dry-run: compute where every sibling of `target_reference`'s hierarchical group should move to
match the relative layout (offsets *and* rotations) of `template_reference`'s group. Rotates
the whole offset pattern to account for a difference in the two anchors' own rotation. Returns
`changes`; nothing is written. Preview before `apply_kicad_layout_template`.
**Args:** `project_path`, `template_reference`, `target_reference`

## `apply_kicad_layout_template`
Reposition every sibling group's components to match `template_reference`'s layout, one call
per target anchor listed in `target_references`.
**Args:** `project_path`, `template_reference`, `target_references`, `write` (default false),
`allow_while_open` (default false)

## `apply_kicad_layout_changes`
Low-level: apply an explicit list of `{reference or uuid, new_position: {x, y, rotation}}`
changes (as returned in `diff_kicad_layout_template`'s `changes`, or written by hand) to the
board file. Accepts either `uuid` or `reference` per change, so you can write
`{"reference": "R33", "new_position": {...}}` directly without first looking up its uuid.
**Args:** `project_path`, `changes`, `write` (default false), `allow_while_open` (default false)

## `diff_kicad_layout_by_role`
Like `apply_kicad_layout_template`'s diff step, but for two hierarchical groups on *different*
schematic sheets - matches members by anchor-pin role (`match_kicad_group_members_by_role`,
see [04-hierarchical-groups.md](04-hierarchical-groups.md)) instead of shared `symbol_uuid`,
then carries the template group's relative layout over onto the target anchor's own position.
Check `ambiguous`/`template_unmatched`/`target_unmatched` before trusting `changes` is
complete. `changes` is ready to hand straight to `apply_kicad_layout_changes`.
**Args:** `project_path`, `template_reference`, `target_reference`, `overrides` (optional)

## `move_kicad_group`
Rigid-body move: shift every member of a component's hierarchical group together, keeping
their layout relative to each other. Use this (instead of diff/apply layout template) when
there's no separate known-good template to copy from - e.g. relocating an already-correct
cluster elsewhere on the board, or nudging one channel to clear a routing conflict. Give
`dx`/`dy` as a plain offset, or `to: {x, y}` to move the anchor to an absolute position.
**Args:** `project_path`, `reference` (any member of the group), `dx` (default 0.0),
`dy` (default 0.0), `drotation` (default 0.0), `to` (optional `{x, y}`), `write` (default
false), `allow_while_open` (default false)

## `suggest_kicad_component_placement`
Suggest component placement positions based on connection grouping and rotation hints.
**Args:** `project_path`, `reference`, `group_size` (default 4), `spacing` (default 10.0),
`rotation` (default 0.0)

## `align_kicad_component_pin`
Rigid-move a component (translate, and optionally rotate first) so that one of its pads ends
up exactly at a given absolute board position. Core primitive for datasheet-guided placement:
point a passive's pad at the IC pin/pad it needs to reach instead of eyeballing
footprint-origin offsets.
**Args:** `project_path`, `reference`, `pin`, `target` (`{x, y}`), `rotation` (optional degrees,
applied before the translate), `write` (default false), `allow_while_open` (default false)

## `align_kicad_components_to_anchor`
Batch-place support components relative to one anchor's pins - e.g. arrange every
capacitor/resistor/inductor around a regulator IC to mirror a datasheet layout guide. Each
`alignments` entry: `{reference, pin, anchor_pin, offset: {dx, dy} (default 0,0), rotation
(optional)}`. Target = `anchor_pin`'s absolute pad position + offset; `reference`'s `pin` pad
is placed there.
**Args:** `project_path`, `anchor_reference`, `alignments`, `write` (default false),
`allow_while_open` (default false)

## `estimate_kicad_footprint_radius`
Best-effort collision-check radius (mm) for a footprint: a known-good manual override for
packages where pad span badly underestimates body size (electrolytic cans, connectors), else a
size parsed out of a standard KiCad SMD footprint name, else a pad-bounding-box estimate, else
a conservative 2.0mm default.
**Args:** `project_path`, `reference`

## `find_kicad_layout_collisions`
Collision-check a set of footprints (typically one hierarchical group's members) both against
each other and against any *other* board component nearby - catching e.g. a group's inductor
ending up on top of an unrelated connector from a different subsystem. Uses
`estimate_kicad_footprint_radius` for every part's envelope, so no radius table needs to be
built by the caller. Read-only.
**Args:** `project_path`, `references`, `extra_search_radius` (default 25.0mm - how far to look
for outside obstacles), `margin` (default 0.4mm - required clearance between envelopes)

## `diff_kicad_route_template`
Dry-run: find the copper (`segment`/`via`, any layer) belonging to `template_reference`'s
hierarchical group's own **per-instance** nets - nets fully contained within that group, like
`Net-(D12-A)` - and compute where each piece would land on `target_reference`'s group, using the
same rigid translate+rotate transform as `diff_kicad_layout_template`. The net is renamed via the
group's `symbol_uuid` role map (template's `Net-(D12-A)` becomes target's `Net-(D16-A)`). Shared
rails (`GND_Safty`, `12V_Main`, ...) that also reach components outside the group are
deliberately **out of scope** - a shared rail's routing is a board-wide decision, not one a
single channel's template gets to make. Three result buckets: `to_add` (ready for
`apply_kicad_route_template`), `already_present` (a matching segment/via already sits there -
skipped, repeat calls stay idempotent), `unmapped_net` (the mapped net isn't one of the target's
own local nets - usually a real schematic difference between the two channels, not just a naming
coincidence). Nothing is written.
**Args:** `project_path`, `template_reference`, `target_reference`, `tolerance_mm` (default
0.05mm - how close an existing target segment/via must be to count as `already_present`)

## `apply_kicad_route_template`
Clone `template_reference`'s hierarchical group's own routed copper (see
`diff_kicad_route_template`) onto every group in `target_references`, as brand-new `segment`/
`via` blocks with fresh uuids - transformed geometry and remapped net name; nothing else is
copied from the template, since these are new copper objects rather than edits to an existing
footprint. This **adds** copper; it never removes or reroutes anything already on the target
(see `already_present`) and never touches a net it can't confidently map (see `unmapped_net`) -
so a partially hand-routed channel keeps what it has, and a real schematic difference between
channels doesn't get silently forced through. Run `unroute_kicad_nets`/
`remove_kicad_stitching_vias` first if the goal is to *replace* existing copper rather than add
alongside it.
**Args:** `project_path`, `template_reference`, `target_references`, `write` (default false),
`allow_while_open` (default false)

## `nudge_kicad_to_clear`
Move a component the minimum distance needed to clear a collision, searching outward in a ring
from its *current* position so it stays as close as possible to wherever it already was
(usually intentional) rather than being fully re-placed. Obstacles default to every other board
component within `search_radius` mm; pass `avoid_references` for an explicit list instead.
**Args:** `project_path`, `reference`, `avoid_references` (optional), `search_radius` (default
25.0), `margin` (default 0.4), `max_search_radius` (default 20.0), `write` (default false),
`allow_while_open` (default false)
