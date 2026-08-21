# Net Class & Bus Detection — Implementation Plan

Feature set for the KiCad MCP server: measure per-net trace widths from the routed
PCB, detect buses (SPI/I2C/I2S/…) by net name and qualify them against shared ICs,
and create KiCad net classes from measured/confirmed settings — with the user
verifying every bus and choosing widths/via sizes from values already used in the
project.

**Module layout** — `kicad_pcb_tool.py` is already ~3,200 lines; the analysis
phases extend it:
- **`kicad_pcb_tool.py`** — Phases 1–6 and 8 (parsers, inventory, bus detection,
  net classes, cost model, audits): parser/audit-shaped code that reuses its
  `SexprParser`, caches, and write discipline in place. Also owns all
  reference-based copy/template functionality (layout/route/flip/property-
  position templates, groups, sibling-instance listing) used for propagating a
  hand-finished instance's placement/routing/flip state onto its siblings.

Everything is exposed through `kicad_mcp_server.py` following the existing
`self.tools[name] = {description, inputSchema, handler}` + `_tool_*` wrapper
pattern (handlers import from whichever module owns the function).

> **REMOVED (2026-08-08):** the autorouter engine described throughout the
> "Phase 7" sections below (`kicad_router_tool.py`, `kicad_router_accel.py`,
> `kicad_route_viewer.py`, `kicad_optimizer_tool.py`, and every MCP tool they
> backed — `route_kicad_board`, `route_kicad_nets`, `unroute_kicad_nets`,
> `get_kicad_drc_constraints`, `get_kicad_ratsnest`, `list_kicad_zones`,
> `create_kicad_plane`, `modify_kicad_plane`, `propose_kicad_plane`,
> `audit_kicad_plane_islands`, `audit_kicad_crosstalk`,
> `benchmark_kicad_autoroute`, `open_kicad_route_viewer`, `decide_kicad_route`,
> `get_kicad_route_session`, `optimize_kicad_board`,
> `remove_kicad_stitching_vias`, `run_kicad_stitching_pass`, and
> `get_kicad_system_resources`) — has been deleted from this codebase along
> with `docs/mcp-tools/11-autorouter.md` and its test files under `tests/`.
> The "Phase 7" history below is kept as an archival record of that removed
> work; it no longer describes anything present in the repo. The net-class/bus
> tooling (Phases 1–6, 8) and the copy/template tooling above are unaffected
> and remain fully supported.

---

## Status snapshot — read this first (updated 2026-07-29)

For whoever (human or AI) picks this up next: this snapshot used to carry a
full session-by-session history (2026-07-21 through 2026-07-28). It has been
trimmed (2026-07-29, user request) now that every item in that history has
its own dedicated "LANDED" anchor section elsewhere in this file — go read
the phase's own anchor for what shipped, tool/test counts at that landing,
and residuals; don't rely on this snapshot for that detail.

- **Landed & coordinator-verified**: Phases 1–9 and 7.1 through 7.22 in
  full — every one has a "### 7.x — LANDED" (or "## Phase N — LANDED")
  anchor in place below with its full write-up. Current state: 94 MCP tools
  registered, 513-test suite green (7 pre-existing failures are real-board
  drift from the user's own continued hand-routing — see the ⭐ findings
  section below for detail — not caused by any landed feature).
  Plus a large-board-with-a-handful-of-unrouted-connections test fixture
  (`generate_large_board_few_unrouted`, `tests/test_large_board_few_
  unrouted.py`, 2026-07-29 user request) — see its own module docstring, not
  a numbered phase. Also: `route_board(optimize=True)` now wires the 7.6
  optimizer into the one-command orchestrator (LANDED 2026-07-30, see the
  follow-up under the 7.6 anchor) — 7.6 itself is NOT a pending item, only
  this wiring was.
- The ⭐ **Real-board routing findings & the hand-routed baseline** section
  right after this one is kept in full (not trimmed) — it's the acceptance
  narrative for the routing-capability arc (7.3b through M7) and several
  root-cause diagnoses future work still depends on, not a redundant log.
- **Next work when resumed (updated 2026-07-30).** The user's 2026-07-29
  priority queue is now fully drained: M6 item 17(c) was the last item and
  is **DEFERRED** (user decision 2026-07-30 — it depends on Phase 7.13's
  decision/recording mechanism, and 7.13 is itself deprioritized; see item
  17(c)'s note and the "Pre-route stack-up gate" note for detail). Phase
  7.13 (impedance-matched traces) remains explicitly deprioritized — do not
  start it without being asked. Still open, low priority: the small 7.3b
  "any same-net copper" termination bit, and reassessing whether 7.22's
  `bus_first_direct_corridor_mm` default should flip now that 7.6 is wired
  into `route_board` (see 7.22's anchor's honest tradeoff note).
  **Phase 7.23 (zone-territory soft routing) and 7.24 (direct-line fast
  path) LANDED 2026-07-30** — see their anchors below. 7.23 closes the
  "zone-shape editing" gap this bullet used to flag as the from-scratch
  board's dominant blocker; 7.24 is a pure speed win for the trivial-hop
  case. `kilnCtl_AutoRouteTest` has also been stripped-and-rerouted once
  already (416→318 unrouted, committed) as a capability test, independent
  of these two phases landing.
  **STANDING POLICY CHANGE (user, 2026-07-30): the byte-identical-when-
  flag-off parity discipline that gated every phase through 7.24 is
  RELAXED.** The user cares more about routing quality (completion %,
  board score) and speed than preserving exact output geometry for
  already-passing connections — new work does not need a parity test
  proving default-off is byte-identical, and defaults MAY change emitted
  geometry when it's an improvement. This does NOT relax the separate
  auditability rationale behind `allow_zone_soft_route`/
  `allow_hand_copper_ripup` staying opt-in (a human should still review
  what a `write=True` touches before trusting it) — keep those opt-in
  regardless. Still non-negotiable: `_self_check` exact-clearance
  correctness, determinism across worker counts, and a green test suite
  (parity tests specifically may be dropped/relaxed, not the whole suite).
  **Phase 7.25 (cross-layer direct-route-first) queued next** — see its
  anchor below.
- Verify claims against the code (`kicad_pcb_tool.py`, `kicad_mcp_server.py`,
  `tests/`) rather than trusting this snapshot if they disagree — and then fix
  this file.

## Phase 7.25 — Cross-layer direct-route-first (via-drop extension, user-requested 2026-07-30, IN PROGRESS)

**Motivation:** 7.24's tier 0 explicitly skips any connection whose two
endpoints don't share a common layer ("no via-drop heuristic here" was the
deliberate 7.24 scope cut). On a from-scratch board a lot of connections ARE
cross-layer (that's most of the point of vias), so this is the next-biggest
easy win for the same reason 7.24 was: cheap geometry beats a grid+A* search
whenever it's legal, and now that byte-identical parity is no longer a
constraint (see the status-snapshot policy change above), this can be added
more freely than 7.24 was.

**Design — a second, slightly richer tier-0b, tried after 7.24's same-layer
tier fails/is skipped, before the grid/A* pipeline:**
- For a cross-layer connection, try: an escape via near the `from` endpoint
  (drop straight down/up from `from_xy` to the layer nearest `to_xy`'s home
  layer) + a same-layer direct/L-bend leg to `to_xy` on that layer; then the
  symmetric candidate anchored at the `to` endpoint instead. Two candidates,
  each reusing 7.24's existing straight/L-bend leg-building and `_self_check`
  discipline for the trace portion, plus a via clearance/placement check
  (reuse whatever via-placement clearance logic `_finalize_core`/`_self_check`
  already apply elsewhere — do not reinvent via clearance math).
- Same acceptance bar as 7.24: full netclass clearance against every real
  hard obstacle, first candidate that passes wins, a miss falls through to
  the existing pipeline unchanged (never makes a connection worse).
- Opt-in flag, but per the relaxed policy this does NOT need a byte-identical
  parity proof — a straightforward default-off-is-safe smoke test is enough
  (prove the tier isn't silently mangling something when off), no need for
  the exhaustive monkeypatch-style parity test 7.23/7.24 required. Still
  needs: `_self_check` correctness tests, a determinism check, and a green
  suite.
- Consider (implementer's judgment, not mandated) whether this should
  default to `True` on `route_board` specifically given the relaxed policy —
  if so, note the reasoning in this file when landing.

**Scope note:** builds directly on 7.24's helpers (`_direct_route_layer`,
`_route_direct_first`'s candidate/self-check pattern) — moderate complexity,
Sonnet-class subagent, worktree-isolated, coordinator reviews before
merging, same process as 7.23/7.24.

---

## How to work this plan (living document — keep it current)

**This file is the source of truth for what's left to do, and must be edited as work
lands — not left to drift.** On every unit of work:

0. **Plan edits are owned by the coordinating session, never by implementation
   subagents.** Delegations must tell the subagent not to touch this file; the
   coordinator reviews each subagent report and applies the plan updates itself
   (this is deliberate — it forces a review step between "agent says done" and
   "plan says done").

1. **When an item is completed, delete it from this plan** (the phase step, its row in
   the MCP tool summary table, and its entry in the build order). Do not leave a
   "done ✓" marker — remove it, so what remains in this file is always exactly the
   work still outstanding.
2. If a whole phase is finished, delete the phase section too. When only the docs
   step of a phase remains, keep just that.
3. If implementation reveals the plan was wrong (a signature changes, an approach is
   replaced, a new edge case appears), **update the affected text in the same commit**
   so the plan never describes code that no longer matches.
4. Keep the three cross-references in sync whenever you remove or change an item: the
   **phase section**, the **MCP tool summary table**, and the **Suggested build
   order**. An item removed from one must be removed from all three.
5. Record any deviation the user approved (different weights, renamed tool, dropped
   feature) by editing the relevant section, not by appending notes at the bottom.

When every phase and its build-order entry are gone, the only thing that should
remain is whatever the team wants to keep as reference (e.g. section 0 and the
`pcb_settings.json` schema) — or delete the file entirely if it's fully captured in
the docs pages it told you to write.

---

## 0. What the board file actually gives us (verified against `kiln.kicad_pcb`)

Trace segment (1,609 present):
```
(segment
    (start 160.417059 99.432375)
    (end   160.417059 99.791986)
    (width 0.3)
    (layer "F.Cu")
    (net "GND_Main")          ; nets are referenced BY NAME here, not by index
    (uuid "026c...")
)
```

Via (298 present):
```
(via
    (at 56.75 127.75)
    (size 12)  (drill 7)
    (layers "F.Cu" "B.Cu")
    (net "")                  ; free/unconnected vias carry an empty net name
    (uuid "29fd...")
)
```

Net classes live in **`kiln.kicad_pro`** (JSON), not the board:
```json
"net_settings": {
  "classes": [ { "name": "Default", "track_width": 0.2, "via_diameter": 0.6,
                 "via_drill": 0.3, "clearance": 0.2, ... } ],
  "netclass_patterns": [],          // list of { "pattern": <regex>, "netclass": <name> }
  "netclass_assignments": null
}
```

Net → IC membership comes from the **`.net` netlist** (already parsed by
`_parse_nets` → `{name, nodes:[{ref,pin}]}`), which is how we qualify a bus as
"all these nets touch the same IC."

### Key facts that shape the design
- **Nets by name in segments/vias** → no net-index table to cross-reference; group
  segments directly by their `net` string. Simplest possible path.
- **`(width …)` is not unique to segments** — silkscreen `gr_line`, footprint
  graphics, etc. also have `width`. Width parsing MUST be scoped to `(segment …)`
  and `(via …)` nodes only, via the s-expr tree, never a flat regex. (A naive
  `grep '(width'` over this board returns `width 0`, `0.05`, `0.1`… which are mostly
  graphics, not copper.)
- **Free vias** (`net ""`) exist and some are oversized (size 12 / drill 7 — likely
  stitching/mounting artifacts). Exclude empty-net vias from per-net stats and flag
  them separately.
- **`.kicad_pro` is JSON** → edit with `json.load`/`json.dump`, not the s-expr
  surgery used for the board. Preserve key order and indentation to keep git diffs
  clean (`json.dump(..., indent=2)` matches KiCad's format; verify against a real
  save).
- Reuse existing infra: `_resolve_project_path`, the mtime/size parse caches,
  `SexprParser`, `_check_not_locked_by_editor`, dry-run `write=False` convention.

---

## Phase 7 — Python autorouter (grid A* with rip-up, layer-purpose aware)

Goal: route unrouted (or user-selected) nets **entirely in Python** — pure stdlib,
same zero-dependency posture — writing standard `(segment)`/`(via)` blocks into the
board file with the existing dry-run/write/lock-file discipline. Everything the
router needs (obstacles, clearances, costs, corridors, layer purposes) is computed
in Python from files already parsed by earlier phases; the MCP caller only picks
nets, reviews previews, and confirms writes.

### 7.3 Router core — two-stage: global route, then detailed route

`route_nets(project_path, nets=None, write=False, allow_while_open=False)` → tool
`route_kicad_nets` (nets=None → all unrouted). The classic industrial split,
because it is also what makes AI-in-the-loop (7.7) possible: **global routing**
makes the discrete, explainable choices (which layer, which corridor, roughly which
path); **detailed routing** turns each choice into exact geometry. All Python.

**7.3a Global routing (coarse, whole-board).** On the `global_grid_mm` grid
(default 2 mm — a few thousand cells, fast even in pure Python):
- Build a per-layer **capacity map**: each coarse cell knows how many more traces
  fit through it (cell width minus existing copper, / (trace width + clearance)).
- For every unrouted connection, find 1–3 *candidate* coarse paths (A* with
  k-shortest variation: best path, then best path avoiding the first's most
  congested cell, etc.), each scored with the full cost model — layer-purpose
  multipliers, corridor discount, via count, congestion vs. capacity, plane
  opportunities (7.5.4).
- Output per connection: ranked candidate list `{layers, coarse path, est. cost,
  congestion risk}`. Bus bundles are globally routed **as one unit** (shared
  candidate corridors, capacity debited for the whole bundle width) — this is
  where "keep the bundle together" actually gets decided.
- **This is the decision surface**: ties/near-ties here (and plane trade-offs)
  are exactly what gets escalated to the AI in 7.7 rather than silently taken.

**7.3b Detailed routing (fine, windowed).** Per connection, in global-stage order:
1. **Obstacle window.** Rasterize only the connection's bbox +
   `search_window_margin_mm` (doubling on failure, up to whole board) at `grid_mm`:
   segments/arcs/vias (Phase 1), pads (`_parse_footprint_pads`; through-hole blocks
   all layers), `Edge.Cuts`, keepout zones (incl. the board's `antenna` zone) and
   board-local keepouts. Obstacles inflate by *their* net's clearance (netclass,
   else `clearance_fallback_mm`, seedable from JLCPCB.kicad_dru.txt). Same-net
   copper is free (and a valid termination — reaching any same-net copper completes
   the connection, not just the target pad). Windowing keeps per-connection A* in
   the tens of thousands of cells instead of millions — the difference between
   seconds and hours in pure Python.
2. **Pad escape.** Pads rarely sit on-grid: each connection endpoint gets an exact
   off-grid stub from the pad anchor to the nearest legal grid point, chosen along
   the pad's escape directions (away from the component body, respecting neighbor
   pad clearance) — the standard fix for A* failing right at a dense pin field
   (this board's MAX31856 channels and the Nano header). Stub + path are emitted
   together, so the copper is exact even though the search is gridded.
3. **A\* search** over (x, y, layer) *within the window*, constrained to the
   global stage's chosen corridor (leaving it costs `off_corridor`): straight/45°
   moves cost `step` x layer-purpose multiplier; turns add `direction_change`;
   layer changes add `via` and need via-sized clearance on both layers; octile
   heuristic. Plane moves per 7.5.4.
4. **Rip-up & reroute (negotiated congestion) — LANDED 2026-07-23 (see stage-2
   anchor).** PathFinder-style on failure:
   raise `congestion` on contested cells, rip only **autorouter-owned** copper
   plus the failed path's blockers among them, re-run from the global stage for
   the ripped set (their corridor choice may change), up to
   `max_ripup_iterations`. Obstacle windows update **incrementally** on rip-up
   (clear the ripped cells) — never a full rebuild mid-run. Human-routed copper
   is never ripped: a net blocked by it fails with the blocker named (or becomes
   a 7.7 `conflict_yield` decision when another routable option exists).
5. **Self-check, then emit.** Before any write, a Python clearance pass verifies
   every proposed segment/via against *all* copper (proposed + existing) at
   netclass clearances — the router proving its own work instead of leaving it
   to KiCad DRC after the fact; violations demote the path back to step 4.
   Then: grid path + stubs → simplified collinear/45° polyline → `(segment)`
   blocks (netclass width) and `(via)` blocks (netclass size/drill), appended
   with the same top-level surgery as `create_group`; uuids recorded in
   `autorouter_owned`. `write=False` preview: per-net length, vias, layers,
   est. Phase 6 cost, SVG, failures with reasons.

**Stage 2 LANDED 2026-07-23** (anchor): steps 1–3, 5, emit, and
`unroute_nets` landed in `kicad_router_tool.py`; `route_kicad_nets` +
`unroute_kicad_nets` registered (79 tools); 7 tests in
`tests/test_detailed_route.py`; the 7.11 `kicad-cli pcb drc`
baseline-vs-post acceptance gate is wired in (auto-skips when kicad-cli
absent). Obstacle window rasterizes bbox + `search_window_margin_mm` at
`grid_mm` (0.2) with segments/arcs/vias, pads (through-hole blocks all
layers), Edge.Cuts, zone fills; **obstacle inflation resolves clearance from
the Default net-class (0.2), never the bare merged DRC 0.0** (per the 7.11
note). Zone clearance is **halo-aware** — precise pour-edge distance
(window-clipped), validated by a hard case: `3.3v_Safty` first emitted copper
skimming a GND pour that produced 7 real kicad-cli violations while the
self-check wrongly passed; the zone model was fixed to fail that connection
(needs plane routing) with NEW=0. Fine A* is integer-milli-cost `(x,y,layer)`,
octile heuristic, deterministic frontier; self-check clears every proposed
segment/via against ALL copper before write; emit is `create_group`-style
top-level surgery with `_format_at_number`, uuids recorded per-net in
board-local `autorouter_owned`.

**Measured on a scratch kiln copy:** routed `/SaftyProcessor/Current3`
(C52.1→R89.1) = 1.7257 mm, 0 vias, B.Cu, 5 segments; self-check clean;
missing connections 39→38 (net went unrouted→routed); kicad-cli DRC
223→223 (NEW=0); `unroute_nets` removed all 5 segments, connectivity back to 39.

**Step 4 rip-up & reroute LANDED 2026-07-23** (PathFinder negotiated
congestion): `_Obst` gained an `owner` field (None = human/board copper, never
rippable; int = autorouter connection id); `_FineWindow` obstacles are
ref-counted so rip-up clears **only** the ripped copper's cells incrementally
(no mid-run full rebuild); `_fine_astar` takes a soft board-global congestion
field escalated on contested cells; `route_nets`' worklist loop rips only
rippable copper on the failed path, names the blockers, self-checks the freed
route, escalates congestion, re-queues ripped nets corridor-free (their path
may change), bounded by `autorouter.max_ripup_iterations` with a `displaced_by`
guard for termination. Reports `ripup_active:true` + `ripup` stats. **Measured
(synthetic GND-wall congestion board):** NETF rips NETG, takes the near gap
(13.81 mm); NETG re-routes to the far gap (82.52 mm — corridor choice changed);
both self-check clean, kicad-cli DRC NEW=0; two write/unroute cycles
byte-identical (deterministic); a net blocked only by solid GND copper fails
with `nearest_blocker.net=="GND"` and GND intact (human copper never ripped).
2 tests added (146 suite total). **Rip-up residuals (accepted, in-code):**
incremental window patching is within the failing net's window (each ripped net
rebuilds its own per-connection window — there is no full-board window, so the
"no full rebuild" contract still holds); congestion cell mapping is
nearest-node between global/window grids (≤½-cell off when unaligned — fine for
a soft field).

**Still open in 7.3b (do NOT treat 7.3b as closed):** (plane-aware routing,
7.12 neck-down, and direction-aware pad escape were the other open items here
at the time this was written — all landed since, as 7.5.4, 7.12, and 7.3d, see
their anchors) termination is on the `to` point (not "any same-net copper");
window doubling is **capped at 60 mm span / 400k-node budget**, not
whole-board (a whole-kiln 0.2 mm 4-layer raster ~2.3M×4 nodes is infeasible in
pure Python — lift with numpy/accel, M5).

### 7.4 What makes it better than a naive maze router
- **Corridor-guided buses** (Phase 5 reuse): bundles stay bundled by cost shaping.
- **Layer-purpose costs** (7.2): respects the board's own layer designations.
- **Layer directions + home-layer thrift** (7.3c): alternating preferred axes
  make crossings board-wide solvable; per-net home layers with cheap short
  jumps keep each net on as few layers as the costs justify.
- **Netclass-aware geometry**: width/clearance/via per net class, not one global.
- **Owned-copper rip-up**: incremental and safe around hand routing by
  construction.
- **Post-route verification**: after write, re-run connectivity (step 2) to prove
  each routed connection is now joined, and re-run Phase 6 cost + Phase 4c
  conformance on the new copper; report before/after cost.
- **SVG preview export** (stdlib string-building, no deps): `write=False` can also
  emit a per-layer SVG of proposed paths to eyeball before committing.
- **Plane-aware** (7.5) and **globally iterative** (7.6) — the router is one move
  inside a cost-driven optimization loop, not a single greedy pass.
- Honest scope: diff-pair coupled routing and length matching remain out of scope
  until the core is proven on this board's remaining ratsnest.

### 7.5 Power/ground plane engine (use, create, move zones)

The board already has six zones (`mainGnd` on F/B/In1.Cu, `safty_gnd` at priority 1,
`main12v`, `main3.3`, `3.3v_safty` on In2.Cu, and an `antenna` zone) with
`island_removal_mode 0` — islands allowed — so plane handling is not optional for
this board; it's how its power distribution actually works. All plane costs live in
`pcb_settings.json` under `plane` (see 6.1).

**7.5.1 Zone parser — LANDED 2026-07-23** (anchor; Sonnet agent,
coordinator-verified). `_parse_zones(board_path)` + `_parse_zones_cached`
(`_zone_cache` keyed by mtime,size) in `kicad_router_tool.py`, per zone: `net`,
`layers` (KiCad 9 multi-layer — returned as a LIST), `uuid`, `name`,
`priority`, `hatch`/`connect_pads`/`min_thickness`/`fill`,
`island_removal_mode`, outline `polygon` points, and `filled_polygon` blocks
when present. Exposed as tool **`list_kicad_zones`** (81 tools). 7 tests in
`tests/test_zones.py`. Measured on kiln — the six known zones parse exactly:
mainGnd (GND_Main, [F.Cu,B.Cu,In1.Cu], prio 0), safty_gnd (GND_Safty,
[F/B/In1.Cu], prio 1), antenna (no net, all 4 Cu, prio 0), 3.3v_safty
(3.3v_Safty, [In2.Cu], prio 2), main3.3 (3.3V_Main, [In2.Cu], prio 3), main12v
(12V_Main, [In2.Cu], prio 4); every zone `island_removal_mode 0`.
**Stopgap supersession — deviation recorded:** the stopgap *parser*
`_parse_zone_fills` was deleted and replaced by a thin per-net fill index that
sources from `_parse_zones_cached` (the authoritative model). `_FillRaster` was
**retained** (not deleted as the original text said) — it is a generic scanline
polygon rasterizer, not a zone parser, and is reused by the connectivity model
and the detailed router's obstacle model. `get_ratsnest` on kiln still returns
**39 missing connections** (regression guard verified), so the false-split fix
is preserved. Still-not-done here (7.5.2+): thermal-spoke / real fill
estimation / island semantics.

**7.5.2 + 7.5.3 — LANDED 2026-07-23** (anchor; Sonnet agent,
coordinator-verified). Fill model + islands + `audit_plane_islands` →
tool **`audit_kicad_plane_islands`** (82 tools) in `kicad_router_tool.py`.
**7.5.2 fill:** uses KiCad's own `filled_polygon` blocks per (zone uuid, layer)
as authoritative components (each block is already one connected component);
when absent, estimates by rasterizing the outline at `grid_mm`, subtracting
higher-priority-zone cells and clearance-inflated foreign copper, then 8-conn
flood-fill (`_FillRaster.from_cells`). Every layer labeled
`fill_source: "kicad" | "estimated"` — kiln reports `"kicad"` throughout
(verified). **7.5.3 islands:** attachments = same-net pads (thermal-gap
tolerance) + same-net vias inside a component; most-attachments component =
mainland, rest = islands; mainland cost 0, island `island_base/N`, 0
attachments → `orphan_island`; warns below `island_min_attachments_warn`. A
mode-1 (`island_removal_mode`) zone reports non-mainland components as
`will_be_removed` and never costs/stitches them (synthetic-tested; kiln is all
mode 0). Each costed island carries a `suggested_stitching_via` (nearest
boundary-pair to mainland + projected cost) — position only, no placement
(that's 7.5.6). 6 tests in `tests/test_plane_islands.py`. **Measured on kiln
(a real finding to hand-check against KiCad's zone-fill view): 31 costed
islands, total island cost ≈ 1912.30, and 1 ORPHAN island (0 attachments) on
`safty_gnd` F.Cu.** mainGnd F.Cu does NOT form a single mainland — 22 island
components, 14 of them single-attachment (flagged). `get_ratsnest`=39 holds.
Estimation-path limits (kiln never hits them): higher-priority subtraction uses
the raw outline not a recursive fill; track segments approximated as sampled
circles — reasonable approximations, not bugs.

The full 7.5.2/7.5.3 spec is kept below as reference for 7.5.4+ consumers:

**7.5.2 Fill model.** Authoritative fills are KiCad's ("Fill All Zones", `B`) — we
never fabricate `filled_polygon` blocks. For costing, use the file's
`filled_polygon` when present; else **estimate**: rasterize the outline at router
grid, subtract clearance-inflated foreign copper/holes, honor zone `priority`
(higher-priority zone wins overlap — exactly the mainGnd/safty_gnd split). Every
plane result is labeled `fill_source: "kicad" | "estimated"`, and any write that
changes zones tells the user to refill in KiCad before trusting DRC.

**7.5.3 Islands & attachment-point costing.** Connected-component analysis on the
per-layer fill raster. For each component: `attachments` = same-net pads
(thermal/solid per `connect_pads`) + same-net vias landing inside it. The component
containing the most attachments is the *mainland*; every other component is an
*island*. Cost, exactly as specified:
- mainland copper: `plane_step` per mm (very low — planes are nearly free),
- island with N≥1 attachments: surcharge `island_base / N` added to the board
  score (an island reached many ways is nearly as good as mainland; a
  single-thread island stays expensive),
- 0 attachments: `orphan_island` (dead copper — effectively forbidden; the audit
  flags it for removal or stitching).
Tool: `audit_kicad_plane_islands` — per zone/layer: component count, area, each
island's attachment list and current cost, plus the cheapest stitching-via
positions that would lower it (see 7.6 move (d)).

**7.5.4 Plane-aware routing — LANDED 2026-07-23** (anchor; Sonnet agent,
coordinator-verified). Wired into `_fine_astar`, `_route_to_emit`, and
`route_nets`/`_route_core` in `kicad_router_tool.py`. `route_nets` builds a
per-net plane model once (`_plane_components_for`, over `_zone_fill_index_cached`
+ `_component_attachments`): per layer, the net's own fill split into components
with a cost factor (mainland 1.0, island `island_base/attachments`, orphan
`orphan_island`). `_route_core` threads `plane_layers` (this net's model, or
`None` for non-plane nets) + `goal_planes` into every `_fine_astar` call; in the
search, a move onto the net's own fill costs `plane_step × factor` (not the
normal step/layer/direction cost), a via onto fill adds `attachment_via`, and
`is_goal` accepts any node inside a `goal_planes` component. `_route_to_emit`
drops segments whose both endpoints ride the fill (plane traversal emits no
copper — only the via(s) + real stubs are written). **Signal-net parity is by
construction:** every plane branch is gated on `plane_layers`/`goal_planes is
not None`, which is `None` for any net that doesn't own a zone (verified —
Current3 unchanged at 1.7257 mm / 0 vias / B.Cu).

**Correctness fix made during the work:** `goal_planes` is restricted to
components whose `layer ∈ goal_layers` — an unrestricted (X/Y-only) match let a
search terminate on the wrong physical layer with ZERO copper emitted, silently
"solving" a cross-layer connection without dropping the needed via.

**Measured (synthetic board — no fast naturally-failing-then-fixed kiln
candidate found in budget; kiln plane nets either already route or fail on
dense-copper `unreachable_in_window`, an open pad-escape/neck-down issue):** net
`PWR`, B.Cu whole-board zone, B.Cu pad → F.Cu pad 16 mm away routes with 1 via
at the F.Cu pad, `length_mm=0` (plane-riding copper not emitted), self-check
clean, kicad-cli DRC NEW=0, unconnected 1→0. 8 tests in
`tests/test_plane_routing.py`; full suite 173 green; `get_ratsnest`=39 holds.

**Residual (accepted, documented in-code):** `_fine_astar`'s distance-only
heuristic (pre-existing) is not admissible for a plane-discounted state, so the
router returns a valid / deterministic / DRC-safe path but not always the
global cost optimum. (The other two residuals noted at 7.5.4's original
landing — the estimated-fill fallback not feeding the plane model, and the
pipeline report's stale `not_implemented` label — were fixed alongside 7.5.5,
see its anchor below.)

### 7.8 Acceleration tiers — numpy and GPU

> **LANDED 2026-07-24 (numpy tier + multi-core), coordinator-verified — with a
> premise correction.** Shipped in `kicad_router_accel.py` (`fine_wavefront`, a
> byte-identical numpy integer-field wavefront) behind `_resolve_backend` /
> `_fine_search`; multi-core independent-connection routing in
> `_run_independent_routes` (`autorouter.cpu.workers`, 0=auto=cores−1), workers
> compute only and the parent commits in canonical owner order so the board is
> bit-identical for ANY worker count. Parity gate `tests/test_backend_parity.py`
> (5 constructions: plain, cross-layer via, plane bypass, plane termination,
> unreachable) asserts numpy == cpu geometry byte-for-byte. numpy is a HARD
> dependency (accel imports it at load; the parity test errors, not skips, if
> absent). 195-test suite green.
>
> **⚠️ Premise correction (important — supersedes the "7.8 fixes the fine-grid
> limit" hypothesis above at §"USER DIRECTION 2026-07-24"):** the numpy per-window
> wavefront is a full-window Jacobi/Bellman-Ford relaxation — O(window_cells ×
> sweeps), cost-INsensitive to path length — so it is *strictly slower* than the
> output-sensitive cpu A* on the router's many small windows. It does **not** make
> a large FINE-grid window tractable; relaxing a 400k-cell window many times is
> worse, not better, than A* refusing it. Hence **`"auto"` resolves to `cpu`**
> (A*), and numpy stays as the parity oracle / large-batch-field tier. **The real,
> delivered 7.8 payoff is multi-core parallelism across independent connections,
> not the numpy backend.** Consequently `unreachable_in_window` on kiln's dense
> pin fields is STILL OPEN: beating 8552.276 needs the fine-grid *pathfinding*
> revisit (a smarter search/window strategy — e.g. fine grid only near pads,
> hierarchical windows, or a plane-aware admissible heuristic), which is the next
> Opus-permitted task, NOT more numpy. Batched whole-board wavefronts (where the
> vectorization actually pays off) remain the GPU/M5 story below.

_Design vision (numpy + GPU, all three tiers; quality-identical by construction;
sized for boards far larger than kiln):_

Three interchangeable backends behind one interface, selected by
`autorouter.acceleration` (`"auto"` probes best-available at startup). **All
three tiers are committed deliverables** — the GPU tier is not contingent on
kiln needing it, because kiln (~1.6k segments, 4 layers) is the *small* end of
what this must handle; the design targets are set by boards 10–100x larger.

| Tier | Needs | Role |
|------|-------|------|
| `cpu` | stdlib only | always works — the reference implementation every other backend must match |
| `numpy` | `numpy` (optional) | vectorized rasterization, clearance checks, wavefronts |
| `gpu` | `cupy` (CUDA) or `torch` (CUDA/DirectML on Windows) | batch parallelism + big-board scale on top of the numpy code path |

**What actually maps to the GPU** (and what doesn't):
- **Serial A\* does not.** Branchy, sequential, tiny frontier — a GPU sits idle.
  The GPU tier therefore swaps the detailed-search inner loop for **wavefront
  cost-field relaxation** (Lee/Bellman-Ford stencil iteration): every cell
  relaxes from its neighbors each sweep until the field converges — a textbook
  GPU stencil kernel. Bonus beyond raw speed: the converged field is a *complete*
  distance field, which yields k-alternate paths and congestion estimates for
  free (feeding 7.3a candidates and 7.7 options), where A* gives one path.
- **Batching is the real win**: relax many connections' cost fields as one
  batched tensor (global stage: all candidate evaluations at once; rip-up
  retries: the whole ripped set together).
- Also GPU/numpy-friendly: obstacle-map rasterization + clearance inflation
  (morphological dilation), the 7.3b-step-5 self-check (pairwise
  segment-distance as array ops), 7.5.2 fill estimation and island
  connected-component labeling.
- **Not worth it**: s-expr parsing, emit/serialization, session bookkeeping —
  I/O-bound, stay in plain Python forever.

**Quality is non-negotiable — and provably preserved:**
- **Convergence, not iteration caps.** Relaxation run to fixpoint is exactly
  Dijkstra-optimal; every "GPU-fast but approximate" shortcut (fixed sweep
  counts, early exit on "good enough", coarsened fields standing in for fine
  ones) is **forbidden**. The GPU is allowed to be slower than theoretically
  possible; it is not allowed to find a different route than `cpu` would.
- **Integer cost fields.** All backend arithmetic uses integer milli-cost units
  (weights from the JSON quantized once at model build). This makes cpu, numpy,
  and gpu fields **bit-identical** — no float summation-order divergence, no
  fp32-vs-fp64 drift — so the deterministic tie-break (lexicographic on
  (cost, y, x, layer)) selects the same path on every backend, always.
- **Parity suite as the gate.** CI routes a fixed net set on `cpu` vs each
  installed backend and asserts identical paths (not just identical costs); a
  backend that can't pass doesn't ship.

**Big-board engineering (do not assume kiln's size):**
- **Memory before speed.** A full fine-grid field is ~(board_area/grid²) x
  layers x 4 bytes — fine at kiln scale (~14 MB), but a 500x500 mm 8-layer
  board at 0.1 mm is ~8 GB: naive whole-board fields die first on memory, not
  time. Hence: per-connection **windowed fields** remain the unit of work on
  every backend (7.3b windows, not whole-board arrays); `gpu.memory_budget_mb`
  (auto-detected free VRAM by default) sizes batches, and batches **tile** —
  windows stream through the budget in chunks, never all-at-once.
- **Hierarchical global routing.** One coarse level stops scaling too: 7.3a
  becomes multi-level on large boards (coarsen until the top level is ~10k
  cells, route, then refine level by level within the parent's corridor —
  standard multilevel global routing). Level count auto-derives from board
  area; kiln naturally collapses to today's single level.
- **Sparse obstacle storage.** Whole-board rasters are held sparsely
  (dict-of-tiles on cpu, per-tile arrays on numpy/gpu); dense arrays exist only
  inside active windows/batches.
- **Scale benchmarks, not vibes.** The acceptance suite includes *synthetic*
  stress boards (generated: dense BGA-style fanout fields, 10x and 100x kiln
  ratsnest, 8+ layers) with per-tier runtime/memory budgets — since no real
  board in this repo can exercise big-board behavior, the tests must
  manufacture one.
- **VRAM overflow falls back, never fails.** The memory planner estimates each
  batch's footprint *before* dispatch and tiles down as far as batch = one
  window; if a single window still exceeds the budget (a huge whole-board
  fallback window on a giant design), **that work item drops to the numpy/cpu
  tier** and the run continues — per-item fallback, not whole-run abort
  (`gpu.oom_fallback`). Runtime allocator OOMs (fragmentation, another app
  claiming VRAM mid-run) are caught the same way: retry at half batch, then
  demote the item. Every demotion is counted in the run report, so "the GPU
  helped 90% of this board" is visible rather than silent.

**Multi-core CPU (stdlib `multiprocessing` — no new deps):**
- `cpu.workers` (auto: cores − 1) parallelizes the *within-iteration* work that
  is independent by construction: detailed-route searches for connections whose
  windows don't overlap (routed in waves — overlapping windows serialize into
  the next wave, so no two workers ever contend for the same cells),
  rasterization tiles, clearance self-checks, fill/island labeling. Windows
  requires spawn-safe code: picklable work items, pool created lazily inside
  the router module, no fork assumptions.
- **Determinism survives parallelism**: workers only *compute* (window →
  path/field); all state commits (congestion updates, owned-copper bookkeeping)
  happen in the parent, in canonical connection order, so the result is
  bit-identical for any worker count — same parity discipline as the backends.
- The numpy and gpu tiers reuse the same wave decomposition (numpy: workers
  across windows; gpu: waves become batches), so parallel structure is designed
  once.

**Hybrid CPU+GPU — use both at once (`acceleration: "auto"`/"hybrid"):**
Backends are not either/or. Each wave's work items go into one queue drained by
**two executors concurrently**: the GPU executor pulls batchable
field-relaxation items sized to its VRAM budget; the CPU pool pulls everything
else *plus overflow* — work-stealing, so neither side idles while the other has
a backlog. This is the payoff of the parity discipline: since every backend
produces bit-identical integer fields and all commits happen parent-side in
canonical order, **which executor computed an item cannot affect the result** —
scheduling is free to be opportunistic without any determinism or quality cost.
Explicit `"cpu"`/`"gpu"` settings remain for benchmarking and debugging.

**The memory planner — probe the machine it's running on, every run:**
`probe_system_resources()` (exposed as diagnostic tool
`get_kicad_system_resources`) reads the actual hardware **at the start of every
routing/optimization run** — free (not installed) system RAM via stdlib
`ctypes` (`GlobalMemoryStatusEx` on Windows, `/proc/meminfo` elsewhere), core
count via `os.cpu_count()`, VRAM via the backend's own API
(`cupy.cuda.Device.mem_info` / torch equivalent, `nvidia-smi` as fallback,
"no GPU" as a normal answer). The planner then derives every concurrency knob
left on auto: worker count capped by `ram_budget` / per-window footprint,
replica count by `ram_budget` / model size, GPU batch by free-VRAM minus
reserve. **No hardware number is ever hard-coded, cached across runs, or
stored in either JSON** — a run on a different PC (or the same PC under
different load) plans itself from scratch; the JSONs only carry *budget
overrides* a user chose, never probed values. Budgets are re-checked at each
session chunk (free memory changes while other apps run); the probed numbers
and derived budgets go in the run report and session log, so a slow run is
diagnosable ("batches were tiny because only 1.1 GB VRAM was free").

**Example probe — the dev machine, 2026-07-21** (illustration of what the
probe returns and why hybrid matters; **not constants** — every run re-probes
whatever machine it's on):

| Resource | Value |
|----------|-------|
| System RAM | 128 GB installed, **111 GB free** |
| CPU | Ryzen 9 3900XT — 12 cores / 24 threads |
| GPU | GTX 1650 — **4 GB VRAM, only 2.4 GB free** at probe (desktop holds the rest), CUDA compute 7.5 (cupy/torch-CUDA capable) |

This profile *inverts* the naive assumption: the CPU side (24 threads, RAM
enough for even a 100x-kiln model times many replicas) dwarfs the GPU (entry
Turing, ~2 GB usable). Hybrid on this box means: GPU as a batch co-processor
for global-stage candidate sweeps that fit ~2 GB; detailed waves and portfolio
replicas mostly on the CPU pool. On a different box (say 16 GB RAM + 24 GB
RTX) the same auto-probing flips the load the other way — which is exactly why
budgets must come from probing at run start, never from constants tuned to any
one machine, this one included.

**Portfolio parallelism — separate iterations racing for quality
(`cpu.replicas`):** run K **independent optimizer replicas** on separate cores,
each with its own seed (fixed list: `seed`, `seed+1`, …), its own net ordering
and SA temperature trajectory, each exploring a *different local minimum* of the
same cost landscape. At every `replica_sync` point (chunk end): compare board
scores, keep the best, restart the losers from the best state with fresh seeds
(go-with-the-winners). Best-of-K strictly dominates any single run of the same
total compute when the landscape is multi-modal — which rip-up routing is.
Interplay with the rest:
- **Reproducible**: winner selected by (score, replica index) tie-break; the
  seed list and sync history land in the session/decision log, so a portfolio
  run replays like any other.
- **7.7 decisions are made once, globally**: a pending AI decision pauses all
  replicas and the answer binds all of them — replicas explore *routing*
  variation, not strategy variation, so the decision budget doesn't multiply
  by K.
- Memory bound: each replica holds an in-memory model diff, so `replicas`
  auto-caps by available RAM on big boards (same planner as the GPU budget).

**Dependency policy — REVISED 2026-07-24 (user decision): `numpy` is a HARD
REQUIRED dependency**, listed uncommented in `requirements-mcp.txt`. The router
does NOT carry a "cpu fallback when numpy is absent" runtime path — numpy is a
trivial pip install, so working around its absence is wasted complexity; a
missing `import numpy` is a hard install error, which is correct. The pure-Python
`cpu` search survives ONLY as the test-time **parity oracle** the numpy tier is
proven against (and an explicit `acceleration:"cpu"` selection for that test) —
not as a graceful-degradation fallback. `cupy`/`torch` (GPU) remain optional
(commented-out) since a CUDA GPU genuinely may be absent. The run report still
names the backend, batch sizes, worker/replica counts, demotion counts, and
peak memory used.

### 7.10 Warm start — an existing board as the starting point

Out of the box the router only *adds* copper: existing routing is fixed obstacle
("human copper is never ripped"). That safety default would make "improve my
already-routed board" impossible — the optimizer could never touch the very
routing it's supposed to improve. Two explicit, opt-in mechanisms fix that:

**7.10.1 Adopting the current board's routing.**
`adopt_kicad_routing(project_path, nets=None | [...], write=False)` → tool
`adopt_kicad_routing`: moves existing copper (whole board, per net, or per
confirmed bus) into `autorouter_owned` in the board-local JSON — from then on the
optimizer treats it as **mutable starting solution** rather than fixed obstacle,
so 7.6 starts from `S0` = the board as routed and improves it: reroute a
meandering trace, replace GND traces with plane attachments (7.5.4), pull a bus
member back into its corridor.
- Adoption is **explicit and enumerated** — never automatic, never a side effect;
  the dry-run lists exactly which uuids change ownership, and the choice is
  recorded (nets, uuids, date) in the board-local JSON.
- **Un-adopt** (`nets` + `revert=True`) removes still-unmodified uuids from the
  owned list — copper the optimizer already replaced stays owned (its original is
  gone; see backup below).
- **Backup before first mutation.** The first `write=True` of any session that
  modifies *adopted* copper first copies the board file to
  `kiln-backups/<board>-<timestamp>.kicad_pcb` (the project's existing backup
  dir) and records the path in the session — adopted-copper optimization is the
  one case where "undo" can't be reconstructed from `autorouter_owned` alone,
  because the originals were human work.
- Hand-made zones stay under the 7.5.5 rule (proposals only) even when adopted
  traces on the same nets are mutable.

**7.10.2 Seeding from a different board file.**
`seed_kicad_routing_from_board(project_path, source_board, nets=None,
write=False)` → tool `seed_kicad_routing_from_board`: warm-start from an earlier
revision or a sibling design's `.kicad_pcb`:
- Match nets **by name** between boards; for each matched net, compare endpoint
  pad positions (same refs + pads within tolerance). Where endpoints still line
  up, copy the source geometry verbatim (fresh uuids, marked `autorouter_owned`);
  where they don't, **degrade gracefully**: the source net's routing is not
  copied but its coarse path is handed to 7.3a as a **prior** — the global stage
  seeds its candidate corridors from where the old board ran that net, so even a
  board whose components all moved still inherits the old board's routing
  *intent*, then re-details it cleanly.
- Report per net: `copied` / `used_as_prior` / `no_match`, with counts — nothing
  silently dropped.
- The source board is opened read-only through the same parsers (it's just
  another `.kicad_pcb`); it is never written.

Both paths feed the same optimizer: after adoption/seeding, `optimize_kicad_board`
runs exactly as in 7.6 — the only difference is what `S0` contains and which
copper is legal to change. The decision log (7.7) notes seeded/adopted origins on
moves that modify them, so the final review shows "replaced adopted trace
(was hand-routed, backed up)" distinctly from "rerouted own copper".

### 7.13 Impedance-matched traces & matched sets

Now planned (was "out of scope until the core is proven"; still gated on 7.3b
landing first). Applies to DIFF_PAIR candidates (Phase 3 structural detector),
buses tagged impedance-critical (Phase 9), and explicit user sets.
- **`impedance_profiles` in `pcb_settings.json`**: named profiles
  `{target_ohms, per-layer {width, gap}, tolerance_mm}` plus `assignments`
  (net-set → profile). Widths/gaps come from the user's stackup/field-solver —
  **we enforce the geometry the user specifies; we never compute impedance.**
- **Coupled routing:** a pair/set routes as one search — 7.3a already treats
  bundles as one capacity unit; 7.3b routes the P/N pair simultaneously as two
  parallel traces at profile width/gap, layer changes as a via pair.
  Uncoupled stretches exist only for pad escape and are reported as
  `uncoupled_mm`, never hidden.
- **Length matching:** within a matched set, after routing, serpentine/trombone
  meanders are inserted in the slack member (inside its own corridor,
  self-checked against 7.11) until lengths agree within `tolerance_mm`; report
  final per-member lengths + residual mismatch. The meander emitter is a
  shared helper (DDR-class buses reuse it).
- **Stack-up gate:** an impedance-critical net with no assigned profile trips
  the same "wait until impedance control / stack-up is set up?" question as
  Phase 9's critical-length gate — one code path, one recorded answer.

### 7.16 Benchmark harness — other people's boards vs. the autorouter

**LANDED 2026-07-24** (anchor; Sonnet agent, coordinator-verified).
`benchmark_autoroute(source_board, mode="complete_only"|"strip_and_reroute",
effort=…)` → tool `benchmark_kicad_autoroute` (83 tools) in
`kicad_router_tool.py` — a thin orchestrator over `get_ratsnest` + `route_board`
+ `get_trace_cost` + the kicad-cli DRC gate; source board NEVER written
(scratch copy only, asserted). Result dict: `human`
(score/board_totals/unrouted/layer-lengths), `auto`
(routed/failed/completion_pct/added length+vias/post-score), `drc`
(baseline/post/`new_violation_count`), `comparison`
(`human_score_total`/`post_score_total`/`delta_total`/`matched_or_beat_human`/
`verdict` — first-class), `route_report`, `runtime_seconds`. 6 fast tests on a
synthetic project + 1 real-kiln test gated behind `KICAD_BENCHMARK_REAL=1`
(the parallel default run does not deselect `slow`, so the real run is env-gated
to stay out of the 2m24s suite). **Measured on real kiln (`complete_only`):
human 8552.276 vs auto post 8568.267 — auto is WORSE by +15.99, 3/39 routed
(7.69%), 1 NEW DRC violation** (a `3.3V_Main` surface stub 0.269 mm from the
`mainGnd` zone — the exact "plane nets as surface stubs" defect). Runtime ~27
min for 39 connections (35 fail fast on `window_too_large`). `strip_and_reroute`
verified on the synthetic fixture only — a from-zero real-kiln reroute is
impractically slow until the window-budget fix lands. **This is now the
acceptance gate for router work: the goal is `matched_or_beat_human: true`.**

Measure the router against real human routing, not just synthetic stress.
`benchmark_autoroute(source_board, mode)` → tool `benchmark_kicad_autoroute`
(scratch copies only; the source board is never written):
- Modes: `strip_and_reroute` (delete all non-zone copper, keep zones +
  placement, route from zero) and `complete_only` (route only what's
  unrouted).
- Metrics vs. the human original: completion %, total copper length, via
  count, Phase 6 board score (same weights both sides), per-layer utilization,
  DRC violation count (7.11 gate), runtime.
- Corpus: `benchmarks/boards/` (gitignored except a manifest recording each
  board's source + license) — openly-licensed KiCad boards (KiCad demos,
  open-hardware projects).
- Once 7.3b lands, router milestone acceptance gains corpus targets ("≥N%
  completion, DRC-clean") and the hand-vs-auto comparison tables join the
  M4/M5 reports.

### 7.17 One command to route the board (CLI **and** MCP, one implementation)

**Minimal version LANDED 2026-07-23** (anchor): `route_board(project_path,
nets=None, write=False, effort="balanced", allow_while_open=False)` in
`kicad_router_tool.py` — a thin orchestrator (no duplicated routing logic):
Stage 0 `get_ratsnest` for the unrouted-before report, then `route_nets` (which
already runs ratsnest→`global_route`→detailed A*+rip-up) over the unrouted/
`nets`-selected connections, rolled into one report (`unrouted_before`,
`routed`/`failed`, `total_routed_length_mm`, `vias_emitted`, `ripup` stats,
per-connection list, and a `pipeline` block that honestly marks
plane_aware_routing / whole_board_optimization / stitching as
`not_implemented (M4)`). `effort` maps to rip-up only for now (quick=0,
balanced=config default, best=20) — stated in the report `notes`. Registered as
MCP tool **`route_kicad_board`** (80 tools) and as a **CLI**: `python
kicad_router_tool.py route <project> [--write] [--nets ...]
[--effort quick|balanced|best] [--json]` (+ an `unroute` subcommand), both thin
skins over the one `route_board` function. 6 tests in
`tests/test_route_board.py` (dry-run leaves the board byte-identical; write
routes + connectivity drops + reversible via unroute; CLI smoke; effort
validation; pipeline-hooks-not-faked guard). **Measured on scratch kiln:**
`route_board(nets=['/SaftyProcessor/Current3'])` → routed 1, 1.7257 mm, B.Cu,
0 vias; `/MainControler/MOSI` correctly **fails** (needs a plane via-drop —
M4). **Remaining (the signature does not change as these land):** wire planes
(7.5, M4), whole-board optimize + effort presets + decision auto-pick (7.6,
M4), stitching (7.5.6, M4); and a docs row on `11-autorouter.md`.

**Requirement:** there must be a single "route the board" command a user can run
either from the command line or as one MCP tool call — the whole Flow B pipeline
behind one entry point, not a sequence the caller has to orchestrate by hand.

- **One function, two front-ends.** A single `route_board(project_path, ...)` in
  `kicad_router_tool.py` runs the end-to-end pipeline: resolve prereqs (net
  classes / confirmed buses if present — else route with defaults and say so),
  `build_connectivity`/ratsnest → global route (7.3a) → detailed route (7.3b)
  over all unrouted (or `nets=`-selected) connections → (when available)
  plane-aware routing (7.5) and whole-board optimization (7.6) → stitching pass
  last (7.5.6). It is a thin orchestrator over the existing functions — **no
  routing logic is duplicated in it**; the CLI and MCP tool are both skins on
  this one call, exactly like the "one session mechanism, not two" discipline in
  7.6.
- **MCP tool `route_kicad_board`** (function `route_board`). Rides the same
  resumable session mechanism as `optimize_kicad_board` (chunk/resume/`awaiting_decision`),
  so a big board's full route survives tool timeouts; a small board completes in
  one call. `write=False` (preview: per-net length/vias/layers, board score,
  failures, SVG) is the default; `write=True` is the explicit confirmed apply.
- **CLI entry point** in `kicad_router_tool.py`'s `__main__` (same pattern as
  `kicad_pcb_tool.py`'s existing CLI):
  `python kicad_router_tool.py route <project_path> [--write] [--nets ...]
  [--effort quick|balanced|best] [--open-viewer]`. Dry-run by default; prints the
  same report the MCP preview returns; `--write` applies after the preview. The
  CLI drives the session loop to completion in-process and auto-answers 7.7
  decision pauses with the optimizer's best-scored default (a headless CLI run
  has no interactive AI in the loop — it records each auto-pick in the decision
  log exactly as a `defer` would), so a scripted/CI route is one shell command.
- **Honest scope by milestone:** a **minimal `route_board` ships with M3** wrapping
  just ratsnest→global→detailed (the pieces that exist) — already a usable
  one-command router for pour-free nets; it **grows** to include planes (M4),
  optimization + effort presets + decision auto-pick (M4), and acceleration
  (M5) as those land, without changing its signature or the two front-ends. The
  build-order item records which stages are wired in at each milestone.
- Documented on `docs/mcp-tools/11-autorouter.md` (the route→review→write
  workflow) **and** in README/CLAUDE.md's "Common Tasks" as the headline
  "route the board" command.

---

## Implementation strategy — subagents

Work phases as sub-tasks delegated to subagents, keeping plan/decisions in the main
session (which also owns all user-facing verification questions):

- **Router core & geometry (Phase 7.3), plane engine (7.5), optimizer +
  decision protocol (7.6/7.7)** — the algorithm-heavy code: delegate to an
  **Opus** subagent with the relevant plan section pasted in whole; require it to
  run against `kiln.kicad_pcb` (a scratch copy for anything that writes) and
  report measured numbers (routed lengths, island counts, before/after board
  score, global-stage runtimes), not just code. 7.3a, 7.3b,
  7.5, and 7.6/7.7 are separate delegations, each landed and reviewed before the
  next; 7.7's delegation must include the scripted-decider test harness.
- **Parsers, inventory, settings plumbing (Phase 7.5.1)** —
  pattern-following work with clear specs: **Sonnet** subagents, one phase each, in
  dependency order; verify each lands green before starting a dependent.
- **numpy backend (7.8)** — mechanical vectorization of a proven cpu
  implementation with the parity suite as the acceptance gate: **Sonnet**. The
  **GPU tier** goes to **Opus**: it owns the batching/tiling/VRAM-budget design
  and the synthetic big-board benchmark suite, and must report parity results +
  runtime/memory numbers at 10x and 100x kiln scale, not just working code.
- **Docs (docs page, README, CLAUDE.md updates)** — **Haiku** subagent once code is
  merged, with the final tool list as input.
- **7.13 impedance/matched sets and the 7.14 optimizer move + pause-the-user
  protocol** — algorithm-heavy, ride the router core: **Opus**, after their
  build-order prerequisites.
- Always: subagent output reviewed in the main session against this plan; each
  completed delegation removes its items from this file per "How to work this plan".

---

## MCP tool summary (new group: "net classes & buses")

Registered-and-landed rows are removed from this table per "How to work this
plan". `route_kicad_nets`/`route_kicad_board`/`optimize_kicad_board`/
`get_kicad_route_session`/`decide_kicad_route` are all landed and gone from
this table now (rip-up, plane-aware routing, and neck-down all landed too —
this note previously listed them as pending, which had gone stale; see the
7.3b/7.5.4/7.12/7.6/7.7 anchors).

| Tool | Function | Writes? |
|------|----------|---------|
| `get_kicad_system_resources` | `probe_system_resources` | no |
| `adopt_kicad_routing` | `adopt_routing` | **yes (board_local.json)** |
| `seed_kicad_routing_from_board` | `seed_routing_from_board` | **yes (board + board_local.json)** |

Each registered in `self.tools` with `inputSchema` + a `_tool_*` handler, exactly
like the existing entries.

### Documentation updates (still owed; M1 + M2 passes landed 2026-07-21 —
`docs/mcp-tools/10-netclasses-and-buses.md` covers all 11 group-10 tools with
the bus-qualification, corridor-area, and cost-model/`pcb_settings.json`
explainers. **M3 docs pass LANDED 2026-07-23:** all 7 formerly-undocumented
tools now have rows — `get_kicad_board_layers`, `get_kicad_ratsnest`,
`get_kicad_drc_constraints`, `route_kicad_nets`, `unroute_kicad_nets` on the
new `docs/mcp-tools/11-autorouter.md` (Group 11: Autorouter & Detailed
Routing), and `detect_kicad_critical_nets` + `detect_kicad_connectors` added
to page 10; README + CLAUDE.md synced to **79 tools / 11 groups**. The
autorouter page honestly marks rip-up, plane-aware routing, and neck-down as
planned-not-implemented. **Docs sync LANDED 2026-07-23 (Haiku):**
`route_kicad_board` (7.17, + CLI), `list_kicad_zones` (7.5.1), and
`audit_kicad_plane_islands` (7.5.2/7.5.3) now have full rows on
`11-autorouter.md`; README + CLAUDE.md synced to **82 tools / 11 groups**
(CLAUDE.md gained "route the board" + zone/island Common-Tasks entries).
**Docs sync LANDED 2026-07-27 (Haiku, three passes):** `benchmark_kicad_autoroute`
(7.16), `open_kicad_route_viewer` (7.9), `propose_kicad_plane`/
`create_kicad_plane`/`modify_kicad_plane` (7.5.5), and a Phase 7.12 neck-down
mention on the `route_kicad_nets`/`route_kicad_board` sections now all have
coverage on `11-autorouter.md`; a stale-claims sweep also fixed two
long-drifted "NOT YET IMPLEMENTED" mentions (rip-up & reroute, plane-aware
routing) that had landed weeks earlier without a docs update; `optimize_kicad_
board`/`get_kicad_route_session` (7.6) and `decide_kicad_route` (7.7) also now
documented (the latter two passes needed a small in-place fix each time,
since a docs pass for 7.6 that merges just before 7.7 lands goes stale
immediately — the "Known Limitation: 7.7 not implemented" note had to be
replaced right after landing); `run_kicad_stitching_pass`/`remove_kicad_
stitching_vias` (7.5.6) also documented — that docs subagent's worktree
branched from a very stale point (before several intervening docs passes),
so its diff would have reintroduced/conflicted with content already on main;
the coordinator applied just its two new tool sections by hand instead of
merging the branch (a pattern worth repeating if a future docs subagent's
diff looks unexpectedly large — check `git show <commit> --stat` against
what the task actually asked for before merging). README synced to **92
tools**. **CLAUDE.md tool-count bump still owed** (coordinator has it staged
locally in the parent repo at 92 — needs the user's own commit; 7.15 added no
new tool so the count doesn't need to move again yet). **Remaining docs
debt:** none currently known for landed tools; future Phase 7 tools add rows
as they land)
- Extend `docs/mcp-tools/10-netclasses-and-buses.md` (or the autorouter page,
  as fits) as each remaining tool in the summary table above lands (same
  per-tool format).
- Keep the tool count in README and CLAUDE.md in sync as Phase 7 tools land,
  and document `<board>.board_local.json` (gitignored per-board state —
  disposable, and how the autorouter uses it) when Phase 7.1
  introduces it.
- Autorouter gets its own docs page `docs/mcp-tools/11-autorouter.md`: pipeline,
  cost model incl. layer-purpose multipliers, rip-up rules ("only autorouter-owned
  copper"), failure reporting, and the route→review→write workflow.
- Autorouter docs page also covers the viewer (`kicad_route_viewer.py`, the
  progress-event JSONL format, cancel flag) and the acceleration tiers + parity
  guarantee.

---

## Interaction flows (how a session uses these)

**Flow A — net classes from the routed board:**
1. `detect_kicad_buses` → list of qualified candidates.
2. For each candidate, **AskUserQuestion**: confirm bus type / membership / drop
   spurious nets / name the class.
2b. (optional) `measure_kicad_bus_corridor_area` on the confirmed bus → per-IC
   corridor areas, to inform width/spacing choices below.
3. `propose_kicad_netclass` on the confirmed nets → proposed width/via + the
   project's used-value menu.
4. **AskUserQuestion**: pick track width and via size from the presented,
   previously-used values (or override).
5. `create_kicad_netclass(write=False)` → review JSON diff → `write=True`.
6. `audit_kicad_netclass_conformance` → confirm routed copper matches, list any
   nets needing a re-route to conform.

**Flow B — routing/optimizing a board:**
1. Prereqs once per board: net classes exist (Flow A), buses confirmed (cached in
   board-local JSON), `pcb_settings.json` tuned if desired.
2. Starting point: nothing (route from scratch), `adopt_kicad_routing` (improve
   the board as routed), and/or `seed_kicad_routing_from_board` (carry over an
   earlier revision).
3. **Session-start questions (AskUserQuestion, answers recorded in the
   board-local session):** effort level (7.15 — three presets); the Phase 9
   critical-length / 7.13 missing-profile stack-up gate ("pause until
   impedance control / stack-up is set up?") for any tripped nets; pin-swap
   consent + exclusions (7.14, validated loudly); and, per area containing
   existing stitching vias, whether to remove them before routing (7.5.6).
4. `get_kicad_ratsnest` → what's unrouted; `open_kicad_route_viewer` to watch.
5. **The one command:** `route_kicad_board` (MCP) or `python
   kicad_router_tool.py route <project>` (CLI) runs steps 4–6 of this flow end
   to end (7.17). Under the hood it is `optimize_kicad_board` (or plain
   `route_kicad_nets` for a quick single pass) — chunk by chunk; answer
   `awaiting_decision` pauses via `decide_kicad_route`;
   plane proposals touching hand-made zones and pin-swap proposals go to the
   **user**, not the AI. Iterates until the 7.15 plateau rule fires. (The CLI
   auto-picks decision pauses and runs headless.)
6. Stitching pass runs last (7.5.6), then: review the dry-run diff, per-net
   costs, decision log, SVG/viewer →
   `write=True` (backup taken automatically if adopted copper changed).
7. In KiCad: refill zones (`B`), run DRC — the authoritative check (the run
   already self-gated on `kicad-cli pcb drc`, 7.11). Iterate from
   step 5 if wanted; `unroute_kicad_nets` undoes any autorouter copper.

---

## Edge cases & correctness notes
- **Width scoping**: only `(segment)`/`(via)` copper — assert layer endswith `.Cu`;
  ignore `Edge.Cuts`, silk, fab.
- **Arcs**: KiCad routes curved traces as `(arc …)` with a `width`; include them in
  per-net width stats (length via arc geometry or chord fallback), else a net's
  width picture is incomplete.
- **Empty-net vias/segments**: exclude from per-net stats; surface a
  `free_copper` count so oversized stray vias (size 12/drill 7 here) are visible.
- **Zero-width (`width 0`)**: treat as "inherit from netclass" per KiCad semantics;
  don't report as a literal 0 mm trace.
- **Net name casing**: board uses mixed case and hierarchical paths
  (`/MainControler/SDA`, `GND_Main`) — normalize for role matching but preserve the
  original name for patterns/writes.
- **`.kicad_pro` write safety**: back up / diff before write; a malformed
  `net_settings` block breaks the project open. Round-trip test on a copy first.
- **Idempotency**: `create_netclass` must refuse or update-in-place on an existing
  class name rather than appending a duplicate.
- **Netlist staleness**: the `.net` file is a schematic export and can lag the
  board. Everything that leans on it (bus detection 3c, cap audit 8, corridor
  roles 5) must first cross-check net names against the board's own copper/pad
  nets and **warn with the mismatch list** when they disagree — a stale netlist
  silently mis-qualifying a bus is worse than a refused run. The router itself
  uses board-file pad nets (ground truth) and is immune.
- **`island_removal_mode` matters**: kiln's zones use mode 0 (islands kept), which
  the 7.5.3 cost model assumes. A zone with mode 1 (KiCad deletes islands on
  refill) must not have estimated islands costed/stitched — its islands won't
  survive a refill; the fill model reads the mode per zone and reports islands on
  such zones as `will_be_removed` instead.
- **KiCad format tolerance**: this repo has v9-era files edited under KiCad 10;
  parsers must skip unknown s-expr tokens instead of failing, and every writer
  emits only constructs already present in the target file (copy-the-native-shape
  rule, as `create_kicad_plane` already does for fill settings). Known hardening
  gap (found by M0's kicad-cli tests): older boards reference nets by numeric
  index (`(net 1 "name")`) where kiln uses name-only (`(net "name")`);
  `_parse_tracks` reads `entry[1]` verbatim and would misread the index form as
  the net name — harden if the tools ever target non-kiln boards.
- **Coordinate formatting on emit**: new segments/vias/zone points use the same
  number formatting as `apply_layout_changes` (`_format_at_number`, ≤6 decimals,
  no trailing zeros) so diffs stay minimal and KiCad re-saves don't rewrite them.

## Build order — five shippable milestones

Phase 7 alone is ~10x the effort of Phases 1–6; without cut points the useful
early tools would sit unreleased behind the router. Each milestone below is
independently shippable (tools registered, docs row added, plan items deleted per
"How to work this plan") before the next begins.

**M0 — Test infrastructure — DONE 2026-07-21.** `tests/`: conftest fixtures
(`kiln_project_path`, `scratch_board`), golden parser tests, writer round-trip
harness (`create_group`/`delete_group`; extend to other writers as they gain
tests), synthetic generator (N-layer stacks, net table, `scale=`, dense
fanout-field mode), `write_synthetic_project` (board + companion
`.kicad_pro`/`.net` — netlist-based tools incl. `detect_buses` run on
synthetic-only projects), and `kicad-cli pcb drc` acceptance tests (KiCad
10.0.4 loads generated boards; auto-skip if kicad-cli absent). 35 tests
passing under `mykicadMcp\.venv`. Only non-automated bit: a literal pcbnew-GUI
screenshot — the kicad-cli DRC load exercises the same board reader.
**Parallel by default (added 2026-07-23):** `pytest.ini` sets `addopts = -n auto`
(pytest-xdist — see `requirements-dev.txt`); the suite is parallel-safe (per-test
tmp/scratch dirs, per-worker-process parse caches), ~2m40s vs ~8m serial on 24
cores. Override with `-n0` for serial/debugging.

**M1 — Net classes end-to-end (Flow A works) — DONE 2026-07-21** (code:
Phases 1, 2, 3, 6-stubbed, 4 — see their anchors; docs pass landed:
`docs/mcp-tools/10-netclasses-and-buses.md`, README + CLAUDE.md at 70 tools /
10 groups, `pcb_settings.json` documented as committed policy).

**M2 — Analysis suite — DONE 2026-07-21** (Phases 5 + 8, the Phase 6 deviation
unstub, and the M2 docs pass all landed — see the phase anchors;
`docs/mcp-tools/10-netclasses-and-buses.md` covers all 11 group-10 tools,
README + CLAUDE.md synced at 72 tools).

**M3 — Router MVP (routes real nets, single pass)** (step 10, Phase 7.1/7.2,
landed 2026-07-21 — see their anchors; remaining:):
11. Phase 7.3 stage 1 (ratsnest/connectivity), 7.3a (global routing), and 7.3b
    **core** (obstacle windows + pad escape + fine A* + self-check +
    emit/unroute + the 7.11 kicad-cli acceptance gate) all LANDED — see the
    7.3 stage-1/stage-2 and 7.3a anchors; `route_kicad_nets`/`unroute_kicad_nets`
    registered, integer milli-cost quantization done. **Step 4 rip-up & reroute
    LANDED 2026-07-23** (negotiated congestion, owner-tagged obstacles,
    incremental window clears, deterministic, human copper never ripped — see
    the stage-2 anchor). 7.12 neck-down and 7.3d direction-aware pad escape
    both landed 2026-07-27 (see their anchors; 7.3d's default stays `false`
    pending a real-board benchmark comparison + sign-off before ever flipping
    it). Whole-board windowing (the 60 mm/400k-node cap) **LANDED 2026-07-28**
    — see the M5 anchor. **Remaining to close 7.3b:** "any same-net copper"
    termination (small, bounded, not yet scheduled — unrelated to the M5
    windowing work, which did not touch this).
    Plane-aware via-drops through pours landed as 7.5.4 (see its anchor).
11h. **[HEADLINE] Phase 7.17 minimal `route_board` — LANDED 2026-07-23** (see
    the 7.17 anchor): the one-command router (MCP tool `route_kicad_board` +
    `python kicad_router_tool.py route <project>` CLI), a thin orchestrator over
    ratsnest→global→detailed, `write=False` default, 6 tests, measured on kiln.
    It grows to add planes (M4), optimization/effort/decision auto-pick (M4),
    and accel (M5) without changing its signature. Still owed: a docs row on
    `11-autorouter.md`.
12. Phase 7.9 viewer — **LANDED 2026-07-27** (see its anchor).

**M4 — Planes + whole-board optimization:**
13. Phase 7.5 plane engine — ALL of it, including 7.5.5 writers and 7.5.6
    stitching, **FULLY LANDED** (see the 7.5.1, 7.5.2/7.5.3, 7.5.4, 7.5.5, and
    7.5.6 anchors; kiln: 31 islands, 1 orphan on safty_gnd F.Cu; plane moves in
    the detailed A*, signal parity by construction; both 7.5.4 residuals wired
    in with 7.5.5; stitching's ordering-contract dependency on a "7.6 stopping
    rule" satisfied once 7.6/7.7 landed). Nothing remains in this item.
14. Phase 7.6/7.7/7.15 optimizer + decision protocol + effort/plateau —
    **ALL LANDED 2026-07-27** (see their anchors; `greedy` and `sa` accept
    policies, sessions/resume, `awaiting_decision`/`decide_kicad_route`,
    `decision_log` auditability, the scripted-decider harness, effort presets,
    and plateau-based stopping all implemented and tested). **Remaining:**
    viewer's cancel flag + decision banner, and portfolio replicas
    (`cpu.replicas`, still schema-only/unread) — both still open, low
    priority.
15. Phase 7.10 warm start — adoption with 7.6 (ownership flag + backup rule);
    cross-board seeding after it (feeds 7.3a priors). Acceptance: adopt kiln's
    routing on a scratch copy, optimize, verify backup exists and the diff only
    touches owned copper.

**M5 — Acceleration:**
16. Phase 7.8 numpy tier + multi-core — **LANDED 2026-07-24** (see its anchor;
    premise-corrected: multi-core across independent connections is the
    delivered win, numpy is the parity oracle, not the fine-grid lever).
    **Whole-board windowing + GPU tier LANDED 2026-07-28** (Opus subagent,
    worktree-isolated, coordinator-reviewed: full diff read plus an
    independent from-scratch full-suite run, not just a report review — see
    the M5 anchor below). **Remaining:** hybrid scheduling (once both
    executors exist, hybrid vs cpu-only parity proves executor assignment
    can't change results) and driving `torch` as a second GPU array module
    (currently detected/named only, not driven — see the M5 anchor for why)
    — both low priority, no user-facing gap.

### M5 — Whole-board windowing + GPU tier — LANDED 2026-07-28 (anchor)

**Whole-board lazy window tier** (closes the 7.3b/M5 windowing residual):
the 60 mm/400k-node cap was never a memory limit — `_FineWindow.build`
rasterizes obstacle→cells at cost O(total inflated obstacle area / grid²)
regardless of what the search then explores, so a board-spanning plane fill
at a fine grid over a wide window dominated before A* took its first step
(naively raising the constants "blows pure-Python runtime", per the
2026-07-24 finding). Fix: `_ObstacleIndex` (uniform-grid spatial index over
obstacles, each inserted into every bucket its bbox padded by its own reach
overlaps — same one-bucket-complete argument `_ZoneEdgeGrid` already uses,
generalized to per-obstacle reach) + `_LazyBlockedSet` (memoized per-cell
membership, drop-in for the eager blocked-cell sets) + `_FineWindow(...,
lazy=True)`. Build cost becomes O(obstacles); search cost is output-sensitive
A* again. `_route_wide_lazy` is a new last-resort tier reached from both
`unreachable_in_window` and `window_too_large`, deliberately ordered AFTER
the existing hierarchical tier (a connection either tier already routes
stays byte-identical) and going through the same `_finalize_core` self-check/
emit path as every other tier — no parallel code path. `_MAX_LAZY_WINDOW_NODES
= 4_000_000` (an order of magnitude above the eager cap, since nothing is
rasterized up front; still coarsens via `_choose_grid` on a genuinely huge
board). Verified byte-for-byte parity between lazy and eager blocked sets
(`tests/test_lazy_window.py`, 14 tests). **Measured on the real kiln board:**
0 connections changed (15/16 before and after) — the one remaining failure
(`Net-(U6-BIAS)`) is confirmed a genuine `GND_Safty` zone-fill enclosure
(0.0 mm clearance to a zone, not a window-size effect), matching item 10's
flood-fill re-diagnosis; the new tier honestly reports this rather than
forcing a route. The actual windowing failure mode (a legal path existing
only via a long off-corridor detour no capped window or hierarchical chunk-
chain can see) is proven instead on a dedicated synthetic case
(`test_wide_lazy_tier_rescues_window_too_large`) where every `_route_attempts`
ladder rung is proved (in-test) to miss the only legal 95mm-offset detour,
and the new tier finds it.

**GPU tier** (closes 7.8's deferred piece): `fine_wavefront` now takes an
`xp` array-module parameter so numpy and CUDA (`cupy`) drive the identical
kernel — parity is structural (integer milli-cost arithmetic throughout, no
float divergence possible), not a second implementation to keep in sync.
New in `kicad_router_accel.py`: `probe_gpu()` (fresh every call, never
cached/written to JSON; `nvidia-smi` fallback for reporting when no array
module is importable), `estimate_window_device_bytes`, `gpu_memory_budget_
bytes` (0 = auto-probe free VRAM, reserving 25%/min 128MB headroom),
`plan_batches`/`resolve_batch_limit` (memory-planned streaming batches, not
a fused multi-window kernel — batching is a correctness/memory-discipline
concern here, not a speed one), and `run_windows` (the demotion-ladder
executor: no device → demote all; oversized item → demote that item; runtime
OOM → retry at half batch size, then demote the individual item; every
demotion counted in the report). New MCP tool `get_kicad_system_resources`
(92→93 tools) reports live hardware (never cached) so a slow/CPU-only run is
explainable. **Scope-down, stated plainly in-code:** `cupy` is driven;
`torch` is detected and named but not driven (not a numpy drop-in for this
kernel's `rint`/one-arg `where`/`minimum(out=)` — a real semantic risk with
no acceptance gate needing it, recorded as a residual). **No GPU hardware
(cupy/torch) is installed in this environment** — the box has a CUDA GPU
(confirmed via `nvidia-smi`) but the tier itself has never executed on real
device memory; parity and OOM-demotion are verified via a simulated device
in `tests/test_gpu_tier.py` (22 tests) instead, including one test running
the real wavefront through a mid-search OOM and confirming the demoted
result matches the cpu A* exactly. `tests/test_bigboard_scale.py` (7 tests)
proves the planner declares 10x/100x-kiln-scale windows oversized and
demotes rather than crashing (memory-only gates, no timing, per the standing
"don't measure speedups" directive).

Full suite: 317→361 passed, same 7 pre-existing board-drift failures
unaffected (coordinator ran the suite independently against the merged tree,
not just trusting the subagent's numbers). Board state at merge time: kiln
score 11765.700 (drifted from the plan's earlier-quoted 8552.276 — not
touched by this work, no write occurred).

**M6 — Routing intelligence (added 2026-07-21 at user request):**
17. Phase 9 residuals — (a) and (b) LANDED 2026-07-23 (see the Phase 9 anchor):
    the 4 placeholder tests in `tests/test_critical_nets.py` are implemented
    (with `generate_critical_nets_board`/`write_critical_nets_project` helpers
    in `tests/synthetic_board.py` giving XTAL-by-ref, XTAL-by-footprint-token,
    switch-node-by-size, and switch-node-requires-IC-pin real coverage — the
    XTAL path is no longer dead code), and the switch-node size proxy is fixed
    to build the bbox from each pad's full rotated rectangle (`position ±
    size/2`) instead of pad centers — kiln's SRP1038C L1 now measures
    3.55×12.45 mm and yields 2 switch_node nets (`Net-(IC1-SW)`/`Net-(IC1-IND)`),
    total critical nets 13→15. Known residual (out of scope, flagged): small
    rectangular-pad inductors (kiln L2/L3, `L_7.3x7.3`) still undershoot their
    real courtyard since no courtyard graphics are parsed — a true courtyard
    parser would close this. **(c)** the Flow B session-start stack-up-gate
    question (the tool already reports `stack_up_gate` per net) — **DEFERRED
    2026-07-30 (user decision)**: depends on Phase 7.13's decision/recording
    mechanism, and 7.13 itself is deprioritized/not started; see the note
    under "Pre-route stack-up gate" a few sections up. Not to be picked up
    standalone without the user raising it again.
18. Phase 7.13 impedance-matched sets (coupled pair routing + length-matching
    meanders + profiles/assignments) — after 7.3b; Opus.
19. Phase 7.14 connector pin-swap advisor — **FULLY LANDED 2026-07-27** (see
    the 7.14 anchor: detection, the optimizer swap move, and the
    pause-and-ask-the-user protocol are all in). Nothing remains in this item.
20. Phase 7.16 benchmark harness (`benchmark_kicad_autoroute`) — **LANDED
    2026-07-24** (see anchor; kiln complete_only: human 8552.276 vs auto
    8568.267, 3/39, `matched_or_beat_human:false` — the acceptance gate). The
    openly-licensed corpus (`benchmarks/boards/`) is still TODO; kiln itself is
    the working benchmark for now.
21. Optional Phase 5 refinements recorded in its anchor: per-station polyline
    centerline (S-shaped bundles read slightly high today) and
    equidistant-trunk splitting.

**M7 — Fill/via engineering, route-search speed, crosstalk avoidance (added
2026-07-28 at user request) — FULLY LANDED 2026-07-28, all three items:**
22. Phase 7.18 multi-layer plane fill & via-mediated connectivity — **LANDED**
    (see its anchor: 7.18.1 attachment-choice ranking behind
    `plane.multilayer_attachment_choice`, 7.18.2 cross-layer continuity audit,
    7.18.3 return-path-aware via placement behind `plane.return_path_bonus`).
    Nothing remains in this item.
23. Phase 7.19 lightweight route cost estimation — **LANDED** (see its
    anchor: `_GoalDistanceField` heuristic behind
    `autorouter.goal_field_heuristic`, candidate pre-ranking + fallback
    behind `autorouter.candidate_fallback`). Nothing remains in this item.
24. Phase 7.20 adjacent-layer parallel-trace (crosstalk) avoidance —
    **LANDED** (see its anchor: `crosstalk` block in `pcb_settings.json`,
    new `audit_kicad_crosstalk` tool). Nothing remains in this item.

**Every milestone:** docs for its tools (`docs/mcp-tools/10-…`/`11-…`), README +
CLAUDE.md tool count/group sync, `.gitignore`/requirements entries when that
milestone introduces the file — not one big docs push at the end (the "Docs"
items in the documentation-updates section are consumed milestone by milestone).
