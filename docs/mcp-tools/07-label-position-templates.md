Group 7: Silkscreen Label Position Templates
===============================================

[< Back to README.md](../../README.md)

The silkscreen-label analogue of Group 5's layout templating: copying a footprint's
hand-decluttered text-property offset (typically its `Reference` designator, but any property
like `Value` works) onto sibling instances instead of re-decluttering each one by hand.

## `get_kicad_property_position`
Get a footprint's child text property's own local `(at x y rotation)` and layer - e.g. exactly
where the `Reference` designator text sits on the silkscreen, relative to the footprint's own
origin. Different from `get_kicad_component`'s position (Group 1), which is the footprint's
own placement, not its label's offset. Use to read a known-good instance's label placement
before copying it with `diff_kicad_property_position_template`.
**Args:** `project_path`, `reference`, `property_name` (default `"Reference"`)

## `diff_kicad_property_position_template`
Dry-run: compute which text-property offsets in `target_reference`'s hierarchical group need
to change to match `template_reference`'s group - use after a reference instance's labels have
been hand-decluttered to avoid overlaps, and you want to copy that exact treatment onto sibling
instances. Matches members by `symbol_uuid` like `diff_kicad_layout_template`. Any matched pair
whose own footprint rotation differs is reported under `skipped` rather than guessed at, since
a label offset's rotation does not transform under a simple linear rule. Returns `changes`;
nothing is written - pass to `apply_kicad_property_position_changes`, or use
`apply_kicad_property_position_template` to do both in one call.
**Args:** `project_path`, `template_reference`, `target_reference`,
`property_name` (default `"Reference"`)

## `apply_kicad_property_position_changes`
Low-level: apply an explicit list of `{reference or uuid, property, new_at: {x, y, rotation}}`
changes (as returned in `diff_kicad_property_position_template`'s `changes`, or written by
hand) to the matching child property's `(at ...)` line inside each footprint's block.
**Args:** `project_path`, `changes`, `write` (default false), `allow_while_open` (default false)

## `apply_kicad_property_position_template`
Copy `template_reference`'s group's text-property label offsets (default `"Reference"`) onto
every group in `target_references`, one call per target. Example: after hand-decluttering U7's
`Reference` silkscreen labels to stop them overlapping,
`apply_kicad_property_position_template(project_path, "U7", ["U8","U9","U6"])` copies that
exact same label placement onto the matching component in each sibling channel.
**Args:** `project_path`, `template_reference`, `target_references`,
`property_name` (default `"Reference"`), `write` (default false), `allow_while_open` (default
false)
