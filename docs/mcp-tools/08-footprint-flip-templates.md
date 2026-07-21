Group 8: Footprint Flip Templates
====================================

[< Back to README.md](../../README.md)

Copying a correctly front/back-flipped footprint's full flip state (mirrored
silkscreen/fab graphics, swapped F./B. layer names, `justify mirror` text flags, adjusted pad
angles - everything KiCad's own Flip command produces) onto a sibling that still needs the
same treatment - e.g. a template channel has some support parts deliberately flipped to the
back to save front-side space, and other channel instances don't yet.

## `diff_kicad_flip_template`
Dry-run: find which members of `target_reference`'s hierarchical group sit on the wrong copper
side (front/back) compared to their matching member (by `symbol_uuid`) in
`template_reference`'s group. Rotation mismatches between a matched pair are reported under
`skipped` rather than attempted. Returns `changes`; nothing is written - pass to
`apply_kicad_flip_template`.
**Args:** `project_path`, `template_reference`, `target_reference`

## `apply_kicad_flip_template`
Flip every part of `target_references`' hierarchical groups that needs it to match
`template_reference`'s group's front/back layer split, by **cloning** the template member's
already-correctly-flipped footprint block onto the target footprint, while keeping the
target's own identity: its uuid, schematic path/sheetname/sheetfile, board position, and
(matched by pad number) its own net names. Used instead of hand-deriving a flip transform - a
text property's stored rotation does not transform under mirroring by one fixed rule, so the
only trustworthy source for "what does a correctly-flipped instance of this footprint look
like" is an instance KiCad itself already flipped. `template_reference`'s group must already
contain one for every role that needs flipping.
**Args:** `project_path`, `template_reference`, `target_references`, `write` (default false),
`allow_while_open` (default false)
