Group 8: Footprint Flip Templates
====================================

[< Back to README.md](../../README.md)

Copying a correctly front/back-flipped footprint's full flip state (mirrored
silkscreen/fab graphics, swapped F./B. layer names, `justify mirror` text flags, adjusted pad
angles - everything KiCad's own Flip command produces) onto a sibling that still needs the
same treatment - e.g. a template channel has some support parts deliberately flipped to the
back to save front-side space, and other channel instances don't yet.

## `diff_kicad_flip_template`
Dry-run: find which members of `target_reference`'s hierarchical group either sit on the wrong
copper side (front/back) or have mismatched per-pad rotation, compared to their matching member
(by `symbol_uuid`) in `template_reference`'s group. The pad-rotation check catches a case the
layer check alone misses: a non-square SMD pad (screw terminal, jack, anything rectangular/
elongated) can have an extra local rotation baked into its own `pad ... (at x y <angle>)` line
in one instance but not another, even though both instances sit on the same layer with the same
overall footprint rotation - invisible to `diff_kicad_layout_template`/`get_kicad_component`
(both only report the footprint's own `at`, not per-pad angles), but visibly wrong in the 2D
editor since it flips which way the pad's long axis points. Compared mod 180, not mod 360 -
rect/roundrect/oval/circle pads are point-symmetric, so a 180°-apart pair (e.g. 0 vs 180) draws
identically and is correctly *not* flagged; only a mismatch that survives mod 180 is real.
Rotation mismatches between a matched pair are reported under `skipped` rather than attempted.
Returns `changes`; nothing is written - pass to `apply_kicad_flip_template`.
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

## Workflow: make sibling channels' layout match a reference

Common request: "I arranged the parts around U8 the way I want, make the other CurrentSense
channels (U7/U9) match." One `template_reference` (the already-correct instance) plus a list of
`target_references` (the siblings), always in this order:

1. `get_kicad_hierarchical_group` on the reference anchor (e.g. `U8`) - confirms its member list.
2. `list_kicad_sibling_instances` on the reference anchor - finds every other instance of the
   same stamped sheet (e.g. `U7`, `U9`) and their own anchor references.
3. `diff_kicad_layout_template` / `apply_kicad_layout_template` (dry-run first, then
   `write: true`) - copies position *and* rotation for every matched-by-`symbol_uuid` member.
   See [05-layout-and-placement.md](05-layout-and-placement.md).
4. `diff_kicad_flip_template` / `apply_kicad_flip_template` (dry-run first, then `write: true`)
   - catches what step 3 can't: front/back layer differences *and* the same-layer per-pad
     rotation quirk described above. Run this even when you don't expect any flips - the
     pad-geometry check is cheap and easy to forget when the visible symptom (front/back layer)
     doesn't apply.
5. Re-run both diffs one more time after applying - `change_count: 0` on both means the siblings
   now match the reference exactly. Don't just trust a clean apply result; a `skipped` rotation
   mismatch in step 3 or 4 means a target's footprint rotation itself differs from the reference
   and needs a look before it's safe to force through.
6. Once placement/flip are settled, `diff_kicad_route_template` / `apply_kicad_route_template`
   (dry-run first, then `write: true`) - clones the reference channel's own hand-routed copper
   (per-instance nets only; shared rails are out of scope) onto each sibling using the same
   transform. See [05-layout-and-placement.md](05-layout-and-placement.md). Do this step last,
   after 3-5 have already settled each target's final position/rotation/layer - the route
   template's transform is derived from the *current* anchor positions, so routing first and
   repositioning after would leave the cloned copper pointing at where the parts used to be.

This is the exact sequence that surfaced the pad-rotation bug `diff_kicad_flip_template` now
catches: layout and flip diffs both came back clean by the old (layer-only) check, but the board
still looked wrong because the actual defect - one instance's jack connector had its pads
individually rotated 90° off from the other two - lived one level below what either diff
compared.
