# Net Class & Bus Detection — Implementation Plan

Feature set for the KiCad MCP server: measure per-net trace widths from the routed
PCB, detect buses (SPI/I2C/I2S/…) by net name and qualify them against shared ICs,
and create KiCad net classes from measured/confirmed settings — with the user
verifying every bus and choosing widths/via sizes from values already used in the
project.

**Module layout** — `kicad_pcb_tool.py` is already ~3,200 lines; the analysis
phases extend it, but the autorouter does not get stuffed in on top:
- **`kicad_pcb_tool.py`** — Phases 1–6 and 8 (parsers, inventory, bus detection,
  net classes, cost model, audits): parser/audit-shaped code that reuses its
  `SexprParser`, caches, and write discipline in place.
- **`kicad_router_tool.py`** (new) — Phase 7 core: ratsnest, global/detailed
  routing, plane engine, optimizer, sessions, warm start. Imports the parsers
  and helpers from `kicad_pcb_tool` — no duplicated parsing.
- **`kicad_router_accel.py`** (new) — 7.8 backends (cpu/numpy/gpu kernels,
  memory planner, hybrid scheduler) behind the one backend interface.
- **`kicad_route_viewer.py`** (new) — 7.9 tkinter viewer, runs as its own
  process; knows only the JSONL event format.

Everything is exposed through `kicad_mcp_server.py` following the existing
`self.tools[name] = {description, inputSchema, handler}` + `_tool_*` wrapper
pattern (handlers import from whichever module owns the function).

---

## Status snapshot — read this first (updated 2026-07-23)

For whoever (human or AI) picks this up next:

- **Landed & verified on the real board**: Phases 1–6 complete — 1, 2, 3
  (named buses + DIFF_PAIR/PARALLEL/RS485 structural detectors), 4, 5
  (corridor areas), and 6 (deviation term live, fed by Phase 5 bundles).
  M0 and M1 are fully done (docs page `10-netclasses-and-buses.md` — note:
  verify tool counts by instantiating `KiCadMcpServer`, not by grepping; a
  grep-count once came back wrong).
  **82 MCP tools registered; full 173-test pytest suite green** (fixtures,
  golden parser tests, writer round-trip, synthetic board/project
  + multi-drop SPI generators, kicad-cli acceptance, corridor/deviation,
  ratsnest, global-route, detailed-route, plane-routing, route-board,
  zone-parser, plane-island, DRC-constraint, critical-net, connector tests;
  `pytest.ini`
  registers the `slow` marker used by the kiln global-route smoke). Each
  landed phase is collapsed to a short "LANDED" anchor section in
  place, kept because later phases reference it. All subagent work above was
  coordinator-reviewed and folded in before this snapshot.
- **Phase 8 landed 2026-07-21** (coordinator-verified: 72 tools, 60-test suite
  green, kiln audit hand-checked — see its anchor; notable real finding: C9's
  schematic Value field lost its voltage rating, flagged `unknown_rating`).
- **M2 is closed** (2026-07-21) and **M3 step 10 (Phases 7.1 + 7.2) landed**
  same day, coordinator-verified: 73 tools, 79-test suite green, kiln board
  cost total now 6241.7 with the `layer_penalty` term (see the 7.1/7.2
  anchors).
- **7.3 stage 1 (connectivity + `get_kicad_ratsnest`) landed 2026-07-21**,
  coordinator-verified: 74 tools, 87-test suite green, kiln = 39 missing
  connections / 25 unrouted nets, hand-checked genuine (see the stage-1
  anchor in 7.3, incl. the approved zone-fill stopgap 7.5 must retire).
- **Plan extended 2026-07-21 at user request** (of the items planned then,
  7.11 and Phase 9 have since landed — see the next bullet; still pending):
  7.3c adjacent-layer crossing preference; 7.5.6 stitching pass
  (always last) + `remove_kicad_stitching_vias` + ask-before-routing rule;
  7.12 neck-down; 7.13 impedance-matched/matched-length sets; 7.14 connector
  pin-swap advisor (consent-gated, user makes the sch change, loud exclusion
  validation); 7.15 effort presets + plateau stopping; 7.16 benchmark harness
  vs hand-routed boards;
  new `pcb_settings.json` blocks; milestone **M6** + M3/M4 amendments;
  Flow B session-start questions. The "hull ballooning / shared-vs-dedicated"
  concern re-raised at planning time is already mitigated by landed Phase 5
  (see its anchor; kiln SPI: bundles 313 vs naive hull 1707 mm²) — residual
  approximations are M6 item 21.
- **Session 2026-07-22 (3-way Sonnet burst, all coordinator-reviewed and
  folded in): 7.11 landed, Phase 9 landed, 7.3a CLOSED.** See the 7.11 and
  Phase 9 anchors for what landed and their consumer notes/residuals. The
  previously-unreviewed `tests/test_global_route.py` was audited and
  hardened by the coordinator: tests 2 (k-alternates) and 3 (bundle
  capacity) were vacuous — assertions behind `if` guards that never fired
  (the bundle test exercised ZERO connections: a fully-routed board has no
  ratsnest, and a fully-unrouted bus has no Phase 5 corridor geometry, so
  bundles only exist on a PARTIALLY routed bus). Both are now hard
  assertions; test 3 uses a strip-alternate-segments fixture
  (`_strip_alternate_segments`) that measurably produces bundle
  `SPI:U1->U2`. Remaining softness (accepted): test 1 doesn't verify the
  crossing resolved onto different layers/corridors, only that both
  connections route.
- **7.3a is closed** (all three closure items done: coordinator cost-formula
  review 2026-07-21; tests reviewed+hardened and kiln acceptance write-up
  delivered 2026-07-22 — `docs/acceptance/7.3a-kiln-global-route.md`, 7
  sections, all measured). Machinery inventory: `infer_layer_directions`,
  `_CoarseModel`, `_Weights` (integer milli-cost), `_astar`/
  `_make_candidates` (k-shortest), `_collect_bundles` (Phase 5 reuse),
  `global_route(project_path, nets=None, connections=None)`,
  `_plane_opportunity_score` stub → 7.5.4. Kiln: 39/39 routed, 4 near-ties,
  corridor choice agrees exactly with the Phase 5 golden bundles.
  **Measured findings that feed 7.3b/7.5.4 (from the acceptance report —
  read it before starting 7.3b):**
  - Warm ≈ cold (128 vs 137 s): there is no result cache and A* itself is
    the floor — the foreign-plane `_FULL_CELL_MILLI` degeneration diagnosis
    stands (fix via 7.3b windowing and/or excluding foreign-plane cells);
    runs are byte-identical (determinism confirmed on the real board).
  - 99.40% of kiln `total_est_cost_milli` (628.8M) is `_FULL_CELL_MILLI`
    penalty; only 0.60% is real routing cost. Weight balance is 7.5.4's job.
  - Bundle shared-corridor double-count inflates the total +5.68% (35.7M),
    not a flat 7× — `net_to_bundle`'s `setdefault` claims shared SPI nets
    for the alphabetically-first bundle (U7 4×, I2C 2×, U8/U9 1×). State in
    the eventual `route_kicad_nets` tool docs.
  - Direction inference: F.Cu 52.77% V / B.Cu 54.52% V — both under the 60%
    threshold, both correctly `None` (the lean is V, weaker than earlier
    guesses). Recommendation on record: don't drop the global threshold to a
    near-coin-flip 52%; prefer a per-layer confidence/margin rule when 7.3c
    tuning happens.
  - Home layers: signal nets 100% intuitive (F.Cu/B.Cu); power nets pick the
    dedicated planes only 2/19 times because via cost dominates short
    (<5 mm) hops — a genuine 7.5.4 design input, not a defect.
  - Congestion truth: F.Cu worst (127/350 used cells over capacity; the
    SPI/I2C hub fan-out cell committed 12× its 2-slot capacity);
    In1/In2.Cu barely touched.
  - Cost-formula review notes still open for 7.3b: (a) heuristic
    admissibility is config-conditional (`off_direction ≥ 1`, layer-purpose
    multipliers ≥ per-kind minimum) — add a docstring line with 7.3b;
    (b) bundle endpoints search all-layer start/goal sets, skipping the
    entry-via cost — 7.3b's exact geometry corrects it.
- **Session 2026-07-23 (coordinator-reviewed): 7.3b core landed, 7.14 detection
  landed, M6 item 17 (a)+(b) landed, M3 docs pass landed, then 7.3b step-4
  rip-up landed.** See the 7.3b stage-2 anchor (incl. its rip-up sub-anchor),
  the 7.14 anchor, and Phase 9 anchor for what landed + residuals. Full suite
  146 green, 79 tools, all coordinator-verified against the tree (concurrent
  edits to `kicad_mcp_server.py`/`kicad_pcb_tool.py` integrated cleanly).
  Then (coordinator-implemented directly, agents being rate-limited):
  **§7.17 one-command `route_board` minimal version LANDED** — MCP tool
  `route_kicad_board` + `python kicad_router_tool.py route` CLI, 80 tools, 6
  new tests, measured on kiln (Current3 routes 1.7257 mm; MOSI correctly fails
  pending M4 planes). See §7.17 anchor, build-order 11h, Flow B step 5.
  Then **Phase 7.5.1 zone parser + `list_kicad_zones` LANDED** (Sonnet agent,
  coordinator-verified): 81 tools, 159-test suite green, six kiln zones parsed
  (mainGnd multi-layer F/B/In1.Cu, safty_gnd, antenna, 3 In2.Cu planes), the
  7.3 stopgap parser retired, and the `get_ratsnest`=39 guard holds. See the
  7.5.1 anchor. Then **Phase 7.5.2 fill + 7.5.3 `audit_kicad_plane_islands`
  LANDED** (Sonnet, coordinator-verified): 82 tools, 165-test suite green,
  kiln fill_source all "kicad", 31 costed islands / 1 orphan on safty_gnd F.Cu,
  39-guard holds. See the 7.5.2/7.5.3 anchor. Then **Phase 7.5.4 plane-aware
  routing LANDED** (Sonnet, coordinator-verified): plane moves in the detailed
  A* for zone-owning nets, 82 tools, full 173-test suite green (my own
  `-p no:randomly` run — the agent's transient "172" did not reproduce),
  signal-net parity confirmed (Current3 unchanged), 39-guard holds. See the
  7.5.4 anchor for the residuals (estimated-fill path not wired; heuristic not
  cost-optimal for plane states; kiln proof is synthetic).
- **Next work when resumed:** (1) **Phase 7.5.5 propose/create/modify planes**
  (`propose_kicad_plane`/`create_kicad_plane`/`modify_kicad_plane` — the plane
  WRITERS, dry-run/write/lock discipline, uuid-anchored s-expr surgery, only
  autorouter-owned zones mutable), then **7.5.6 stitching pass** +
  `remove_kicad_stitching_vias` + its ask-before-routing rule (last in M4).
  (2) Two small 7.5.4 residuals: wire the 7.5.2 estimated-fill fallback into the
  plane router, and relabel `route_board`/`route_kicad_nets`
  `pipeline.plane_aware_routing` from `not_implemented` to `partial`. (3) A docs
  row for
  `route_kicad_board` on `11-autorouter.md` (README/CLAUDE bump 79→81). (3)
  Finish 7.3b's remaining bits: 7.12 neck-down, direction-aware pad escape,
  whole-board windowing (numpy/accel, M5). Then step 12 (viewer). Still open:
  M6 item 17 (c) Flow B stack-up-gate question, and 7.14's optimizer pin-swap
  move + pause-the-user protocol (both wait on 7.6).
- **Nothing has been committed** in either repo as of this snapshot — review the
  working tree before assuming git history matches this file.
- Verify claims against the code (`kicad_pcb_tool.py`, `kicad_mcp_server.py`,
  `tests/`) rather than trusting this snapshot if they disagree — and then fix
  this file.

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

## Phases 1 & 2 — LANDED 2026-07-21 (reference anchor; no work remains here)

Implemented in `kicad_pcb_tool.py` and registered in `kicad_mcp_server.py`:
- `_parse_tracks` / `_parse_tracks_cached` (`_track_cache`, invalidated in
  `_invalidate_board_cache`) — segments/vias/arcs, `.Cu`-scoped, per the shapes
  formerly specced here.
- `get_net_track_widths(project_path, net=None)` → tool
  `get_kicad_net_track_widths` — the per-net width summary
  (length-weighted `dominant_width`, `widths` map, `via_sizes`, `is_uniform`)
  that later phases call the "Phase 1 width summary". Width-0 segments bucket
  under `"inherit"` (KiCad "use netclass" semantics) with a
  `zero_width_segment_count`.
- `get_project_track_inventory(project_path)` → tool
  `get_kicad_track_inventory` — the Phase-2 "previously used values" menu for
  Phase 4's pick-from-list questions, incl. `existing_netclasses` from
  `kiln.kicad_pro`, free/oversized via warnings, `free_via_count`.

Verified on kiln: 1,609 segments, 298 vias, 0 arcs, 154 routed nets; dominant
width 0.3 mm (795 segs / 95 nets); vias 0.6/0.3 (×293) plus 5 oversized 12/7
(3 free).

---

## Phase 3 — Bus detection & IC qualification — LANDED 2026-07-21 (reference anchor; no work remains here)

Landed in `kicad_pcb_tool.py` + registered as tool `detect_kicad_buses`:
`_BUS_SIGNATURES` (I2C, SPI, QSPI, I2S, UART, CAN, USB, SWD, JTAG with role
alias sets), `detect_buses(project_path, ic_ref_prefixes=None)` (hierarchical-
prefix grouping with shared-IC fallback, per-net `width_summary` from
`get_net_track_widths`, candidate shape per the original spec incl.
`suggested_class_name`), 3c IC qualification (`common_ics` intersection over
U/IC/Q refs; all-but-one tolerance for fan-out; `qualified:false` + reason
otherwise), and the netlist-staleness guard (`stale_netlist_warnings`, both
directions vs. board pad nets). Read-only; never auto-applies — caller confirms
each candidate with the user (`AskUserQuestion`) before Phase 4 creates
anything. Verified on kiln: 3 qualified candidates — I2C `/MainControler/`
(SDA/SCL, 0.2 mm, common_ics U4+U5), SPI `/MainControler/` (MOSI/MISO/CLK/
CS0–CS3, 0.3 mm, hub U4), SPI `/SaftyProcessor/` (hub U6);
`stale_netlist_warnings` empty (netlist current).

Structural detectors also landed 2026-07-21: `_find_diff_pairs`
(`<base>_P/_N`, `+`/`-`, `P`/`N` — both polarities required) → `DIFF_PAIR`;
`_find_parallel_buses` (≥4 nets, contiguous `0..n`, gap disqualifies) →
`PARALLEL`; RS485/RS422 in `_BUS_SIGNATURES` (`A`/`B` required, `Z`/`Y`
optional, `basename_only` to dodge A0..A15 collisions, and
`suppress_unqualified` — dropped entirely without a common transceiver IC).
Named signatures claim nets before structural detectors run, so USB D+/D-
stays USB and QSPI IO0..IO3 stays QSPI (verified with synthetic netlists).
On kiln: still exactly the 3 named candidates, zero structural candidates —
correct for this board. Known honest limitation: KiCad auto-generated
pin-derived names like `Net-(U6-T+)`/`Net-(U6-T-)` (thermocouple leads) end in
parens, outside the three specified suffix forms, so such per-IC diff pairs go
undetected; extend the suffix forms if that ever matters.

---

## Phase 4 — LANDED 2026-07-21 (reference anchor; no work remains here)

`propose_netclass_from_nets` / `create_netclass` / `audit_netclass_conformance`
implemented in `kicad_pcb_tool.py`, registered as `propose_kicad_netclass`,
`create_kicad_netclass` (writes `.kicad_pro`; dry-run diff default; refuses
duplicate names; docstring warns classes don't retroactively resize copper and
reload on project reopen), and `audit_kicad_netclass_conformance`. The
AskUserQuestion width/via pick-from-inventory interaction happens in the
session, per Flow A. Verified: byte-identical `.kicad_pro` serialization
round-trip; SPI_MainControler proposal 0.3/0.6/0.3 clearance 0.2, zero
conflicts; write=True round-trip on a temp copy; conformance clean for the 7
SPI nets (128/154 nets mismatch Default — expected until classes are assigned).

---

## Phase 5 — LANDED 2026-07-21 (reference anchor; no code work remains here)

`measure_bus_corridor_areas` (+ `_compute_bus_bundles`, `_convex_hull_area`,
`_perp_distance_to_axis`, `_ic_set_for_net`, `_resolve_bus_spec`) implemented in
`kicad_pcb_tool.py` after `get_project_track_inventory`; registered as tool
`measure_kicad_bus_corridor_area`. Accepts a `detect_buses` candidate or explicit
`{nets, hub_ic}`; anchor-and-corridor per-destination-IC bundles (hub/dest pad
centroids as axes; dedicated nets wholesale, shared nets clipped per
destination); per-layer corridor + convex-hull areas per the original 5.3 output
shape (plus a `clip_band_mm` transparency field); degenerate cases (single
destination → no clipping; no hub → `grouped:false` un-grouped hull); pure
stdlib; read-only. `_compute_bus_bundles` also returns internal geometry keys
(`_hub_pt`/`_dest_pt`/`_centerline_s`/`_net_segs`/`_axis_len`) consumed by the
Phase 6 deviation term — **Phase 7.3a's corridor reuse should consume the same
bundle geometry**. Knob: `corridor.clip_band_mult` (band = mult × dominant
width). 13 tests in `tests/test_bus_corridor.py` incl. a synthetic multi-drop
SPI generator (`tests/synthetic_board.py: write_multidrop_spi_project`).

**Spec deviations recorded (approved at review, 2026-07-21):**
- **Step C assignment** is per-segment nearest-destination-axis gated by
  (projection within the hub→dest span extended by the band) OR (within band of
  the bundle's dedicated copper) — NOT the originally specced "band of an
  already-assigned trace" chaining, which on real bowed traces chained whole
  shared trunks into one bundle (67/74 segments unassigned, bend ratios
  150–700). The band guards span-extension/dedicated-proximity, not
  trace-to-trace chaining.
- **Hub tiebreak** when `common_ics` has several (I2C gave [U4,U5]): most
  member nets, then most board-wide net participation, then name → U4.
- **"≥2 traces" enforced as ≥2 distinct nets**, at bundle level and per
  station, so one meandering net can't inflate a corridor.
- Known limits: an equidistant shared trunk lands in one bundle (ties → first
  destination), visible via `unassigned_segment_count` and the
  `sum_of_bundle_areas` vs `union_hull` gap; arcs use the chord approximation
  (kiln has no copper arcs). Optional M6 refinements (build-order item 21):
  per-station polyline centerline + equidistant-trunk splitting.

Verified on kiln: I2C /MainControler/ = single-destination degenerate (U4→U5,
corridor 152.1 mm²); SPI /MainControler/ = true multi-drop (U4→U7/U8/U9,
bundles 137.0/157.4/18.7 mm², 7 unassigned fan-out segments, sum 313.1 vs
union hull 1706.6); SPI /SaftyProcessor/ correctly degrades to
`grouped:false` (its slaves are off-board).

---

## Phase 6 — LANDED 2026-07-21, deviation term unstubbed same day (schema below kept as reference)

`DEFAULT_PCB_SETTINGS` + `load_pcb_settings` (deep-merge over defaults,
non-negative weight validation, file-vs-default key report) +
`init_pcb_settings` (dry-run/overwrite-guarded seeding) + `get_trace_cost`
(length/via/layer_span terms) are implemented in `kicad_pcb_tool.py`
and registered as `get_kicad_pcb_settings`, `init_kicad_pcb_settings`,
`get_kicad_trace_cost`. The deviation term is live: bundle memberships come
from every qualified `detect_buses` candidate via Phase 5's
`_compute_bus_bundles`; `mean_perp_distance`/`max_perp_distance`/
`excess_length` per `deviation.metric` + `reference`; shared nets roll up
across bundles (max for max_perp, length-weighted mean for mean_perp; **max
also for excess_length** — unspecified in the original spec, chosen at review —
with `direct` = bundle-axis length between hub/dest pad centroids, not a
single pad pair). Bus nets report `on_bus:true` + a `bundle` object; the
`bus_centerline` reference approximates the centerline as a straight line at
the bundle's mean perpendicular offset (S-shaped bundles read slightly high).
Verified on kiln: 154 nets ranked; board total 5584.4 → 5628.8 with the
deviation term (44.41 board-wide); SPI /MainControler/ nets all `on_bus:true`
except CS3 (reaches only the hub — correctly on no bundle). Worst nets remain
GND_Main 520.1, GND_Safty 240.8, 12V_Main 211.9 — via-heavy power/ground
nets, the Phase 7.5 plane motivation made measurable.

The `pcb_settings.json` schema below is **kept as reference** — Phases 5, 7,
and 8 read their knobs (`corridor`, `layer_purpose`, `autorouter`, `plane`,
`optimizer`, `schematic_checks`) from this file, and `DEFAULT_PCB_SETTINGS`
mirrors it. The file lives in the project directory next to `kiln.kicad_pro`,
committed; absent file → defaults.

```json
{
  "version": 1,
  "trace_cost": {
    "weights": {
      "length_mm":     1.0,   // cost per mm of routed copper
      "via":           5.0,   // cost per via on the net
      "deviation_mm":  2.0,   // cost per mm of mean lateral deviation from the bus centerline
      "excess_length": 10.0,  // cost per unit of detour ratio (actual/direct - 1)
      "layer_span":    8.0    // cost per layer beyond the net's first (home) layer - prices
                              // multi-layer sprawl; a short jump that returns home adds vias
                              // but no span, a genuine transfer adds span (7.3c)
    },
    "deviation": {
      "metric":    "mean_perp_distance",  // "mean_perp_distance" | "max_perp_distance" | "excess_length"
      "reference": "bus_centerline"       // "bus_centerline" (Phase 5) | "straight_line" (hub->dest pad axis)
    },
    "via_weights": { "through": 1.0, "microvia": 0.5, "blind_buried": 1.5 },  // multiplies base "via" weight
    "non_bus_deviation": 0.0            // deviation cost applied to nets not on any detected bus (usually 0)
  },
  "corridor":      { "clip_band_mult": 3.0 },     // Phase 5 knob, centralized here
  "bus_detection": { "ic_ref_prefixes": ["U", "IC"], "extra_signatures": {} },  // Phase 3 overrides
  "layer_purpose": {                    // Phase 7: cost multipliers, net_kind x layer_type
    // layer types come from the board's own (layers ...) block: signal|power|mixed|jumper|user
    "signal": { "signal": 1.0, "mixed": 1.2, "power": 4.0, "jumper": 2.0 },
    "power":  { "signal": 2.0, "mixed": 1.2, "power": 1.0, "jumper": 3.0 },
    "power_net_patterns": ["^GND", "^\\+?\\d+\\.?\\d*[Vv]", "VCC", "VDD", "12[Vv]", "3\\.3[Vv]", "5[Vv]"]
  },
  "autorouter": {                       // Phase 7 knobs (policy; per-board state lives in the board-local JSON)
    "grid_mm": 0.2,                     // detailed-routing grid
    "global_grid_mm": 2.0,              // coarse grid for the global-routing stage (7.3a)
    "search_window_margin_mm": 8.0,     // detailed A* runs in the connection bbox + this margin,
                                        // doubling on failure up to the whole board
    "clearance_fallback_mm": 0.2,       // used when no netclass/DRU clearance applies
    "cost": { "step": 1.0, "via": 25.0, "direction_change": 2.0,
              "congestion": 8.0, "off_corridor": 4.0,
              "off_direction": 2.0,          // 7.3c: multiplier on steps against the layer's
                                             // preferred axis (45 deg moves are neutral)
              "away_from_home_per_mm": 0.5 },// 7.3c: per-mm surcharge on any layer that isn't
                                             // the net's home layer -> short jumps stay cheap,
                                             // long stays get priced into a real transfer
    "layer_directions": "auto",              // 7.3c: "auto" = infer each copper layer's preferred
                                             // axis from the board's existing segments; or an
                                             // explicit map {"F.Cu": "h", "B.Cu": "v", ...}
    "max_ripup_iterations": 5,
    "allowed_layers": [],               // empty = every copper layer the board defines
    "acceleration": "auto",             // 7.8: "auto" (= hybrid cpu+gpu when both available) |
                                        //      "hybrid" | "cpu" | "numpy" | "gpu"
    "gpu": { "memory_budget_mb": 0,     // 0 = auto: probe FREE VRAM at run start (not card total)
             "batch": "auto",           // connections relaxed per batch; "auto" sizes from budget
             "oom_fallback": true },    // work that can't fit VRAM even untiled drops to numpy/cpu
    "cpu": { "workers": 0,              // multiprocessing pool size; 0 = auto (cores - 1, min 1)
             "ram_budget_mb": 0,        // 0 = auto: probe free system RAM, keep a reserve;
                                        // caps workers x window memory and replica count
             "replicas": "auto",        // 7.8 portfolio: parallel independent optimizer replicas
                                        // ("auto" = min(workers, 4); 1 disables)
             "replica_sync": "chunk_end" }, // when replicas compare scores / losers restart from best
    "progress": { "events": true,       // 7.9: emit JSONL progress events for the viewer
                  "open_viewer": false, // auto-launch the tkinter viewer on route/optimize
                  "color_theme": "auto" } // "auto" = the user's active KiCad theme; or a theme
                                          // name from KiCad's colors/ dir; or "builtin"
  },
  "plane": {                            // Phase 7.5: power/ground plane (zone) costs
    "plane_step": 0.05,                 // per-mm cost through healthy plane copper (vs 1.0 for a trace)
    "attachment_via": 8.0,              // cost to enter/leave a plane through a via
    "island_base": 40.0,                // island surcharge numerator:
                                        //   island_cost = island_base / attachment_count
                                        //   (more attachment points -> cheaper; 1 attachment -> full 40)
    "orphan_island": 1000.0,            // island with 0 attachments (dead copper) - effectively forbidden
    "island_min_attachments_warn": 2,   // audit warns below this even when routable
    "create_plane": 15.0,               // optimizer's flat cost to add a new zone (discourages zone spam)
    "modify_plane": 5.0                 //          ... to move/resize an existing zone outline
  },
  "schematic_checks": {                 // Phase 8: net-aware schematic audits
    "cap_voltage": {
      "derating_min_ratio": 2.0,        // rating must be >= ratio x applied voltage (ceramic derating)
      "gnd_tokens": ["GND", "AGND", "DGND", "PGND", "VSS"],   // net name containing one -> 0 V
      "net_voltages": {},               // explicit overrides for unlabeled names, e.g. {"VBUS": 5.0, "AREF": 3.3}
      "default_cap_rating": null        // fallback rating for caps whose Value states none (same
                                        // convention as audit_capacitor_voltages' default_voltage)
    }
  },
  "high_speed": {                       // Phase 9: high-speed classification & critical lengths
    "bus_frequencies_mhz": { "SPI": 20, "QSPI": 80, "I2C": 0.4, "I2S": 12,
                             "UART": 1, "CAN": 1, "USB": 480, "MIPI": 1000,
                             "DDR": 800, "SWD": 4, "JTAG": 10, "CLK": 25 },
    "velocity_fraction": 0.5,           // signal speed as a fraction of c (mid-FR4)
    "rise_fraction": 0.05,              // t_rise estimated as this fraction of the bit period
    "critical_length_overrides_mm": {}, // per-bus-type L_crit overrides, wins over the formula
    "critical_fraction": 0.9,           // straight-line >= this x L_crit -> stack-up gate question
    "length_weight_mult": 4.0           // per-mm cost multiplier for classified fast nets
  },
  "switch_node": {                      // Phase 9: switching-supply inductor detection
    "min_inductor_mm": 2.0,             // courtyard/footprint edge above this (both axes) qualifies
    "length_weight_mult": 8.0           // per-mm cost multiplier on the SW-node net
  },
  "neck_down": {                        // Phase 7.12: wide nets onto small pads
    "enabled": true,
    "max_width_vs_pad": 1.0,            // neck when class width > this x pad's smaller dimension
    "min_length_mm": 0.5, "max_length_mm": 3.0
  },
  "stitching": {                        // Phase 7.5.6: plane stitching pass (always last)
    "enabled": true,
    "target_spacing_mm": 5.0,           // general plane stitching pitch
    "near_high_speed_mm": 1.0,          // return-path vias placed within this of a fast trace
    "near_high_speed_pitch_mm": 2.0
  },
  "pin_swap": {                         // Phase 7.14: connector pin-swap advisor (consent-gated)
    "enabled": false,
    "min_gain": 25.0,                   // board-score gain that pauses the run to ask the USER
    "ref_prefixes": ["J", "P", "CN", "X"]
  },
  "impedance_profiles": {               // Phase 7.13: user-specified geometry, never computed
    "profiles": {},                     // e.g. {"usb90": {"target_ohms": 90, "layers": {"F.Cu": {"width": 0.2, "gap": 0.15}}, "tolerance_mm": 0.5}}
    "assignments": {}                   // net-set / bus name -> profile name
  },
  "optimizer": {                        // Phase 7.6: iterative whole-board optimization
    "max_iterations": 20,
    "time_budget_s": 300,
    "effort": "balanced",               // 7.15: "quick" | "balanced" | "best" preset (session asks the user)
    "plateau_window": 3,                // 7.15: iterations in the rate windows
    "plateau_slope_ratio": 0.1,         // 7.15: stop when trailing rate < ratio x initial rate
    "worst_k": 5,                       // nets re-examined per iteration
    "unrouted_penalty": 500.0,          // added to board score per still-unrouted connection
    "accept": "greedy",                 // "greedy" | "sa" (simulated annealing)
    "sa_initial_temp": 50.0, "sa_cooling": 0.9,
    "convergence_delta": 0.5,           // stop when an iteration improves less than this
    "seed": 1,                          // deterministic run-to-run for reproducibility
    "ai_decisions": {                   // Phase 7.7: AI-in-the-loop decision points
      "enabled": true,
      "min_score_spread": 5.0,          // pause only when best vs runner-up option differ less than this
                                        // (clear winners are auto-picked; the AI sees genuine trade-offs)
      "max_pauses_per_run": 12,         // budget; past it the optimizer auto-picks best-scored
      "decision_types": ["bundle_layer", "plane_proposal", "conflict_yield",
                         "stitching_budget", "sa_large_move", "give_up_net"]
    }
  }
}
```

---

## Phase 7 — Python autorouter (grid A* with rip-up, layer-purpose aware)

Goal: route unrouted (or user-selected) nets **entirely in Python** — pure stdlib,
same zero-dependency posture — writing standard `(segment)`/`(via)` blocks into the
board file with the existing dry-run/write/lock-file discipline. Everything the
router needs (obstacles, clearances, costs, corridors, layer purposes) is computed
in Python from files already parsed by earlier phases; the MCP caller only picks
nets, reviews previews, and confirms writes.

### 7.1 — LANDED 2026-07-21 (anchor). Board-local state JSON (`<board>.board_local.json`, NOT in git)

`_board_local_path` / `load_board_local` / `save_board_local` +
`record_confirmed_bus` in `kicad_pcb_tool.py` (after `init_pcb_settings`).
Contract as designed: `pcb_settings.json` = committed shareable policy;
`<board_stem>.board_local.json` next to the board = gitignored per-board state
(both `.gitignore` entries verified present; README "disposable" note still
owed with the M3 docs). Schema (all keys optional, unknown keys preserved;
tools create/extend as they run): `version`, `autorouter_owned`
`{segments, vias}` (every uuid the router ever wrote — rip-up/undo only ever
touches these, never human copper), `keepouts`, `net_overrides`
(`{priority, layers}` per net), `confirmed_buses`
(`{bus_type, nets, hub_ic, name, confirmed_on}` — user verifications cached so
re-runs don't re-ask), `last_route_session`. `detect_buses` marks candidates
`confirmed:true/false` (+`confirmed_on`/`confirmed_name`) by matching
bus_type + exact net set, result gains `confirmed_count`; membership changes
require re-confirmation (by design). Deviations approved at review:
`save_board_local` writes verbatim (load-modify-save contract), no merge;
`load_board_local` returns `{board_local_path, loaded_from_file, data}`.

### 7.2 — LANDED 2026-07-21 (anchor). Layer purposes from the board file

`_parse_board_layers(_cached)` (new `_board_layers_cache`, invalidated with
the others) → per-copper-layer `{name, ordinal, type, user_name}` **in file
order** (the true stack order — kiln ordinals 0/4/6/2 are not stack-ordered);
public `get_board_layers` → tool `get_kicad_board_layers` (73 tools total).
Kiln golden: F.Cu/B.Cu `signal`, In1.Cu/In2.Cu `power`. `_net_kind(net_name,
netclass=None, power_net_patterns=None)` — patterns tried against full name
AND post-`/` basename (anchored `^GND` must catch `/Power/GND`); netclass
check is token-based (power/pwr/gnd/ground/supply) but `get_trace_cost`
currently classifies by name only (`_parse_nets` doesn't capture the `.net`
`(class ...)` field). **Router cost integration (for 7.3): every grid step on
a layer is multiplied by `layer_purpose[net_kind][layer_type]`** — signal
across a power plane 4x, `mixed` mildly penalized, `jumper` usable but
discouraged for continuous routing, unknown/`user` layers not routable.
`get_trace_cost` already reports per net: `net_kind`,
`metrics.layer_lengths_mm`, and `cost.layer_penalty` = Σ length_on_layer ×
(multiplier − 1) × `w.length_mm` (segments+arcs only; vias have no dwell
length; multipliers < 1 would discount — per spec, not clamped), included in
net totals / `board_totals` / `weights_used`. Kiln: board total 5628.8 →
6241.7 (`layer_penalty` 612.9 board-wide; 10 power / 144 signal nets; worst:
GND_Main +182.1, 12V_Main +138.9, GND_Safty +117.8 — all power nets on signal
layers: the Phase 7.5 plane motivation, now visible in triage).

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
self-check clearance failures are hard failures, not demoted back to rip-up;
incremental window patching is within the failing net's window (each ripped net
rebuilds its own per-connection window — there is no full-board window, so the
"no full rebuild" contract still holds); congestion cell mapping is
nearest-node between global/window grids (≤½-cell off when unaligned — fine for
a soft field).

**Still open in 7.3b (do NOT treat 7.3b as closed):** **plane-aware routing
(7.5.4)** — plane-net via-drops through pours currently *fail* rather than emit
DRC-violating copper (needs Phase 7.5 zone model, M4); **7.12 neck-down** (not
applied); pad escape lands on nearest free node (not direction-aware);
termination is on the `to` point (not "any same-net copper"); window doubling
is **capped at 60 mm span / 400k-node budget**, not whole-board (a whole-kiln
0.2 mm 4-layer raster ~2.3M×4 nodes is infeasible in pure Python — lift with
numpy/accel, M5). Demoting self-check failures back into the rip-up loop is
also open.

**Stage 1 LANDED 2026-07-21** (anchor): `kicad_router_tool.py` exists with
`build_connectivity` (union-find islands per net) + `get_ratsnest` → tool
`get_kicad_ratsnest` (74 tools). Contact rule: two items join when they share
a copper layer and come within `reach_a + reach_b + 0.02 mm` (reach = half
copper width; pad = half its larger dimension — deliberately generous, since
false splits are the failure mode and over-merge stays within one net);
through-hole pads span all copper layers; vias span the layers physically
between their endpoints (stack order from `_parse_board_layers`). Airlines/MST
edges are 2-D layer-agnostic between island terminal points, like KiCad.
Ordering: `net_overrides.priority` desc, then shortest-airline-first.
`_parse_footprint_pads` gained pad `size`/`type` keys (additive). **Scope
addition approved at review:** minimal read-only `_parse_zone_fills` +
scanline `_FillRaster` live in the router module because plane nets connect
through filled pours (without them: 211 phantom connections; with: 39) —
**7.5 must supersede and delete these** in favor of its real zone model.
Verified on kiln: 39 missing connections / 726.2 mm airline / 149 nets fully
routed / 25 unrouted / 62 single-pad; hand-checked shortest connections are
genuinely unrouted (e.g. MOSI→R28.1 needs a via drop; 3.3V_Main pads await
plane vias to In2.Cu). Perf: cold ≈ 9 s (three full board parses), warm
≈ 0.2 s; per-net connectivity is O(n²) pairwise — add a spatial index before
big-board work (7.8). 8 tests in `tests/test_ratsnest.py`.

**7.3c Layer directions & layer-thrift (jumps) — whole-board ease by cost
shaping.** Two disciplines every seasoned router uses, both implemented as cost
terms (so they fall out of the same A*/wavefront search, price into the same
board score, and obey the same parity rules — no special-case path logic):

- **Preferred direction per layer.** Alternating axes (H on one signal layer, V
  on the next) is what makes crossing conflicts globally solvable: two nets that
  must cross do so on different axes/layers instead of fighting for one channel.
  KiCad does not store per-layer directions in the board file, so
  `layer_directions: "auto"` **infers** each copper layer's axis from the
  board's own existing segments (length-weighted angle histogram → H / V /
  none-dominant; layers with too little copper alternate against their
  neighbors; power-type layers get no preference — planes don't care).
  The inferred map is reported in the run report and overridable in the JSON.
  In search, a step against the layer's axis costs `step x off_direction`
  (45° moves neutral); the global stage's capacity map counts directional
  capacity the same way, so corridor candidates already respect the pattern.
- **Home layer + jumps.** Each net gets a **home layer** (chosen by the global
  stage: the layer where most of its corridor wants to live, biased by layer
  purpose and direction). Search then prices layers asymmetrically: every mm on
  a non-home layer adds `away_from_home_per_mm` on top of normal costs. The
  emergent behavior is exactly the requested one: when a trace hits a blockage,
  a **short jump** — via, a few mm on another layer (a `jumper`-type layer where
  one exists; that is their purpose, and the 7.2 multiplier already favors them
  for short hops over continuous routing), via back home — stays cheap, while
  *staying* on the away layer accumulates surcharge until a genuine **layer
  transfer** (re-homing, paying `layer_span` in the Phase 6 score) becomes the
  honestly-cheaper choice. The router never hard-forbids either; the weights
  decide, per the "respect cost" requirement.
- **Trade-offs are measured, not assumed.** Per-net results (route preview and
  `get_kicad_trace_cost` alike) report `home_layer`, `layers_used`,
  `jump_count` (over-and-back excursions), `away_mm`, `off_direction_mm`, and —
  when the search had a viable single-layer or transfer alternative — the
  scored delta between chosen and runner-up, so "two jumps beat a B.Cu
  transfer by 11.3 here" is inspectable. Near-ties surface through the
  existing 7.7 `bundle_layer`/`conflict_yield` decisions rather than a new
  decision type. Phase 6's `layer_span` weight makes multi-layer sprawl visible
  on *existing* boards too, before the router ever runs.
- **Crossing pairs prefer adjacent layers.** When the global stage resolves a
  crossing conflict by putting the two nets on different layers, candidate
  scoring biases toward an **adjacent** copper-layer pair (the vias involved
  span fewer layers and the return-path discontinuity at the crossing is
  smaller). A pairing bias in crossing-conflict resolution, not a hard rule —
  `layer_span` and via-span costs already price distant pairs; this breaks the
  tie toward adjacency when costs are otherwise close.

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

**Residuals (accepted, documented in-code):** (a) only KiCad-filled zones feed
the plane model — the 7.5.2 "estimated" fill fallback is NOT wired into routing
yet; (b) `_fine_astar`'s distance-only heuristic (pre-existing) is not
admissible for a plane-discounted state, so the router returns a valid /
deterministic / DRC-safe path but not always the global cost optimum; (c) the
`route_board`/`route_kicad_nets` `pipeline` report still labels
`plane_aware_routing: not_implemented` — update that string to "partial" now
that it is wired.

**7.5.5 Creating and moving planes.** Same dry-run/write/lock discipline as every
writer; zone outlines are uuid-anchored s-expr surgery like `delete_group`:
- `propose_kicad_plane(net, layer)` — candidate outline: grid-based coverage region
  of the net's pads/vias on that layer (rectilinear hull, simplified, clipped to
  `Edge.Cuts`, minus higher-priority zones), preferring layers whose type matches
  the net kind (7.2 — power nets onto `power` layers). Returns outline, estimated
  fill/islands/attachments, and the **cost delta** vs. current routing.
- `create_kicad_plane(..., write=)` — writes the `(zone ...)` block, copying
  fill-setting shape (hatch, connect_pads clearance, min_thickness, thermal gap,
  smoothing) from the board's existing zones so new zones look native.
- `modify_kicad_plane(uuid, new_outline | priority, write=)` — move/grow/shrink an
  existing zone by replacing its polygon points; refuses on zones it can't
  re-locate; warns that refill + DRC in KiCad is required after.
- Board-local JSON records `autorouter_owned.zones` uuids — the optimizer may only
  move/delete zones it created, never the six hand-made ones (they can only be
  *proposed* for change, for the user to confirm).

### 7.5.6 Plane stitching pass (always runs LAST) + stitching-via management

**Ordering contract: stitching is the final copper pass.** Only after all trace
routing and plane creation/moves have converged (7.6's stopping rule fires)
does a dedicated stitching pass place vias, in this order:
1. **Island rescue** — attach would-be islands/stubs found by 7.5.3, at the
   cheapest attachment positions it already computes.
2. **Return-path stitching** — on power/ground planes, stitching vias near
   high-speed traces (Phase 9 classification): target pitch
   `stitching.near_high_speed_pitch_mm`, placed within
   `stitching.near_high_speed_mm` of the trace wherever fill + DRC clearances
   (7.11) allow.
3. **General stitching** to `stitching.target_spacing_mm` where the budget
   allows (7.7's `stitching_budget` decision type prices this).

Every stitching via is `autorouter_owned` **and tagged `stitching: true`** in
the board-local JSON — distinct from routing vias, so management tools can
target exactly them.

`remove_kicad_stitching_vias(project_path, area=None, write=False)` → tool:
delete all stitching vias, or only those inside a given rect/polygon `area`.
Deletes only autorouter-owned stitching vias; `include_foreign: true` *lists*
(never auto-deletes) other freestanding same-net vias for the user to confirm
one by one — kiln's 3 free vias would surface here.

**Interaction rule (session contract):** before routing or optimizing in an
area that contains stitching vias (owned or foreign), the session must ask the
user whether to remove them first (AskUserQuestion with count + area); the
answer is recorded per area in the board-local session so one run asks once,
and removed stitching is re-placed by the final stitching pass anyway.

Knobs `stitching`: `{target_spacing_mm: 5.0, near_high_speed_mm: 1.0,
near_high_speed_pitch_mm: 2.0, enabled: true}`.

### 7.6 Iterative whole-board optimization (`optimize_kicad_board`)

The "make the best board" loop. Board score
`S = Σ net trace cost (Phase 6, incl. layer-purpose penalties) + Σ plane costs
(7.5.3) + unrouted_penalty x unrouted_count` — one number, every term already
defined in `pcb_settings.json`, so "best" is exactly what the JSON says it is.

**Loop** (knobs under `optimizer` in 6.1): all iteration happens on an **in-memory
board model** — the file is untouched until the final confirmed write.
1. Build model (copper, planes, ratsnest), score `S0`.
2. Each iteration: rank nets/planes by cost contribution; take the `worst_k` and
   generate candidate **moves**:
   (a) rip-up + reroute a worst net (new order / perturbed congestion costs),
   (b) reroute a bus bundle together along its Phase 5 corridor,
   (c) swap a routed net's layer assignment (layer-purpose driven),
   (d) add stitching vias to an island (each directly cheapens `island_base / N`),
   (e) `propose_kicad_plane` for a power net whose trace cost exceeds
       `create_plane` + projected plane cost,
   (f) move/resize an **autorouter-owned** zone outline.
3. Score each candidate; accept per `accept` policy — `greedy` (only improvements)
   or `sa` (simulated annealing: worse moves accepted with probability
   `exp(-ΔS/T)`, `T *= sa_cooling` per iteration — escapes local minima like
   "every GND trace individually cheap but a plane would beat them all").
4. Stop on `max_iterations`, `time_budget_s`, or improvement < `convergence_delta`.
5. Result: per-iteration score curve, final `S`, the full move list, and a
   dry-run diff of every board change. `write=True` applies the best state only
   (segments/vias/zones), records everything in `autorouter_owned`, and reminds:
   refill zones + run DRC in KiCad.

Rip-up/move constraints are inherited from 7.3/7.5: human copper and the six
hand-made zones are read-only inputs; `seed` makes runs reproducible; the session
(score curve, best-S, owned uuids) persists in the board-local JSON so a later run
resumes instead of thrashing.

**Sessions, not marathons.** One MCP call must never run the whole optimization —
it would blow tool timeouts and give the caller no control. `optimize_kicad_board`
is **resumable**: each call runs a bounded chunk (N iterations or a decision pause,
whichever first) and returns
`{session_id, state: "running" | "awaiting_decision" | "converged" | "budget_exhausted",
score_curve, pending_decision?}`. Session state (RNG state, iteration, in-memory
model diff, decision log) checkpoints to the board-local JSON, so a session
survives MCP restarts and is inspectable via `get_kicad_route_session`. The final
`write=True` still only happens on an explicit confirmed call. `route_kicad_nets`
rides the **same session mechanism** whenever a job exceeds one call's budget (a
big board's full ratsnest) — a plain small route completes in one call and never
mentions sessions, but chunking/resume/cancel is one implementation, not two.

### 7.7 AI-in-the-loop routing decisions (high-level, between designated options)

The optimizer escalates **strategic** choices to the AI through the MCP
call/return cycle; the AI never draws geometry. Contract:

- **Options are always machine-generated and pre-scored.** A decision is a closed
  list of 2–4 candidates, each fully specified and already priced by the cost
  model (7.3a candidates, 7.5.5 plane proposals, …). The AI picks one — by id —
  or answers `defer` (optimizer takes its best-scored default). Free-form input
  is limited to a `rationale` string, which is *recorded, never executed*.
- **Only genuine trade-offs pause the run.** A decision fires only when the score
  spread between best and runner-up is under `ai_decisions.min_score_spread` —
  i.e. the cost model can't distinguish the options well, which is exactly where
  judgment (EMI, serviceability, future rework, "that jumper layer is for rework
  wires") beats arithmetic. Clear winners are auto-taken; `max_pauses_per_run`
  caps the budget, after which everything auto-picks.
- **Decision types** (the `decision_types` allowlist in 6.1):
  - `bundle_layer` — which layer/corridor a bus bundle takes (7.3a candidates:
    "SPI bundle: F.Cu direct through dense region vs B.Cu detour, +6.2 cost,
    -2 vias").
  - `plane_proposal` — create/resize a plane: candidate outlines with projected
    cost deltas and island forecasts (7.5.5).
  - `conflict_yield` — two nets want one channel: which yields and takes its
    alternative candidate.
  - `stitching_budget` — how many stitching vias an island gets (options: counts
    with resulting `island_base / N` costs).
  - `sa_large_move` — an annealing move above a size threshold (e.g. rip a whole
    bundle) wants confirmation before proceeding.
  - `give_up_net` — a net keeps failing: leave unrouted for hand-routing vs.
    accept an expensive route (both shown with numbers).
- **Mechanics.** `optimize_kicad_board` returns `state: "awaiting_decision"` with
  one pending decision (options, scores, per-option SVG snippet, a one-line
  machine-generated summary each). The AI answers via
  `decide_kicad_route(session_id, decision_id, choice, rationale)`; the session
  resumes to the next chunk. Undecided sessions time out to `defer` on the next
  resume, so an abandoned session still converges.
- **Auditability.** Every decision (options, scores, choice, rationale, whether
  auto or AI) appends to `decision_log` in the board-local JSON. A run can be
  **replayed** from the log (same seed + same answers → same board), which keeps
  the optimizer debuggable even with a nondeterministic decision-maker in the
  loop; the final report includes the log so the human review of `write=True`
  sees *why* the board came out this way.
- **Where the human stays.** The AI decides *between designated options*; the
  human still holds the write gate (dry-run preview + `write=True`), the
  hand-made-zone confirmations (7.5.5), and `pcb_settings.json` itself — the AI
  cannot reweight the cost model mid-run.

### 7.8 Acceleration tiers — numpy and GPU (all three shipped; quality-identical
by construction; sized for boards far larger than kiln)

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

`numpy`/`cupy`/`torch` stay **commented-out** in `requirements-mcp.txt` exactly
like `kicad-python` — absent, everything still runs on `cpu`; the run report
names the backend, batch sizes, worker/replica counts, demotion counts, and
peak memory used.

### 7.9 Live progress viewer (tkinter — stdlib, vector-drawn)

Show the board evolving while the router/optimizer works: a redrawing board view
plus progress bars. `tkinter` ships with CPython on Windows — still zero added
dependencies.

**Architecture: separate viewer process, file-based event stream.** The MCP
server must never host a GUI thread (Tk wants the main thread; the server may be
headless or restart mid-session). Instead:
- The router appends **JSONL progress events** (when `autorouter.progress.events`)
  to `<board>.route_progress.jsonl` next to the board-local JSON (gitignored by
  the same `.gitignore` change, and pruned at session start): session/board
  geometry snapshot first, then per-event `iteration`, `connection done/total`,
  `score`, `changed` (added/removed segment+via geometry by uuid), pending
  7.7 decisions.
- `open_kicad_route_viewer` (also auto-launched when `progress.open_viewer`)
  spawns a **detached** `python kicad_route_viewer.py <board>` subprocess that
  tails the file. Decoupled by construction: the viewer can be closed/reopened
  mid-run, survives server restarts (replays the file to catch up), and never
  blocks or crashes the router — the router only ever appends to a file.
- **Cancel support**: a "Stop after this iteration" button writes a flag into the
  board-local session; the optimizer checks it between chunks — a clean, safe
  cancel path that a headless MCP session otherwise lacks.

**Rendering: vector, straight onto `tk.Canvas`** — no SVG rasterization, no image
files. Canvas *is* a retained-mode vector surface: segments as width-scaled
lines, vias/pads as ovals, zone outlines as polygons, ratsnest as thin dashed
lines; per-layer colors with layer-visibility checkboxes; zoom/pan via
`canvas.scale`. Incremental by uuid — each event only deletes/creates the changed
items, so redraw stays O(change), not O(board). (The "changing image of the
board", done as live vector drawing rather than image swapping.)

**Layer colors = the user's KiCad colors.** The viewer renders with the same
palette the PCB editor shows, so the picture reads instantly. Helper
`_load_kicad_layer_colors()` resolves, in order (driven by
`autorouter.progress.color_theme`):
1. **The active KiCad theme** (`"auto"`): newest version dir under
   `%APPDATA%/kicad/<ver>/` (10.0 on this machine), read `pcbnew.json` →
   `appearance.color_theme`, then match that theme in `colors/*.json` by
   `meta.name`/filename. Theme JSON layout (verified locally): `board.copper.f`
   / `.in1` / `.in2` / `.b` as `"rgb(200, 52, 52)"`-style strings (F.Cu red,
   In1 green, B.Cu blue on this machine), plus `board.via_through`,
   `board.background`, `board.edge_cuts`, `board.ratsnest` — parsed to Tk hex
   (`#c83434`); `rgba(...)` alpha is dropped (Canvas has no per-item alpha).
2. **Embedded fallback palette**: KiCad's stock default colors baked into the
   viewer as constants. Not optional-nice-to-have: the active theme on this
   machine is `_builtin_default`, which is compiled into KiCad and has **no
   theme file**, so "read the config" alone can't work — auto falls back to the
   embedded palette whenever the named theme has no file (also covers machines
   without any KiCad config).
Copper layer keys map `f/in1..in30/b` → `F.Cu`/`In1.Cu`…`B.Cu`; layers a theme
doesn't name fall back per-layer to the embedded palette. The resolved palette
is written into the JSONL header event, so the viewer stays a dumb renderer
(no KiCad-config knowledge in the GUI process) and a recorded event file replays
with the colors it was recorded with.

**Chrome**: two `ttk.Progressbar`s (connections routed this iteration; iterations
this run), best-score readout with a small score-curve sparkline (Canvas
polyline), backend + session-state line, and a banner when the session is
`awaiting_decision` showing the pending 7.7 options read-only (answering stays in
the MCP conversation, where the decision log lives).

Failure honesty: the viewer is *observational only* — if tkinter is unavailable
(headless CI), `open_kicad_route_viewer` reports that and everything else works;
viewer bugs can't corrupt a route because the viewer never writes anything except
the cancel flag.

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

### 7.11 — LANDED 2026-07-22 (reference anchor; the kicad-cli acceptance gate moves to 7.3b)

`get_drc_constraints(project_path)` (cached) → tool `get_kicad_drc_constraints`
(registered, 12/12 tests in `tests/test_drc_constraints.py`), merging in
precedence order: `.kicad_dru` rules > `.kicad_pro` net-class/board rules >
`autorouter.clearance_fallback_mm`. Unsupported conditions reported per rule
in `unsupported_rules`, never dropped. Landing fixes (coordinator-reviewed):
`.kicad_dru` is a flat sequence of top-level forms, not one wrapping s-expr —
the parser walks all forms; DRU values carry unit suffixes (`0.15mm`) →
`_parse_dru_length_mm`; `#`-comments stripped quote-aware, scoped to DRU
parsing only; cache invalidates on BOTH `.kicad_dru` and `.kicad_pro`
mtime/size (coordinator fix at review).

**Notes for 7.3b (the first geometric consumer):**
- On kiln, `clearance` CANNOT be sourced from the DRU at all: every
  clearance-typed rule in JLCPCB.kicad_dru.txt conditions on `B.Type`/`B.Net`
  (inherently pairwise), so all 9 land in `unsupported_rules` and clearance
  resolves from the `.kicad_pro` board rule (0.0 on kiln!) → fallback logic /
  netclass clearance matters; obstacle inflation must not trust a bare 0.
- Merged `constraints` values are last-wins per constraint type (e.g. kiln
  track_width resolves to the inner-layer 0.09 rule); the per-rule `layer` and
  full `sources` chain are preserved — 7.3b should resolve per-layer/per-net
  from `sources` + `net_classes`, not lean on the single merged value.

**Acceptance gate (moves to 7.3b):** every routing/optimization acceptance run
ends with `kicad-cli pcb drc` on the written scratch board; new violations vs.
the pre-route baseline fail the run (extends the M0 kicad-cli harness into the
router path).

### 7.12 Neck-down: wide nets onto small pads

Power net classes route wide (e.g. 1 mm) but must land on 0.3 mm IC pins/balls.
Per connection endpoint: if the netclass/target width >
`neck_down.max_width_vs_pad` × the pad's smaller copper dimension, emit a
**neck** — the final stretch before the pad routes at `neck_width` =
min(class width, pad-fit width), never below the 7.11 minimum, for a length
between `neck_down.min_length_mm` and `max_length_mm` (at least the pad-escape
distance out of the pin field). The 7.3b pad-escape stub is the natural home —
stubs are already emitted separately from the gridded path. The self-check
prices the neck at its true width (DRC-true), and Phase 4c
`audit_netclass_conformance` must learn to accept necks: a segment narrower
than its class is conformant when it terminates on a pad within neck length
and matches the neck rules — otherwise still flagged. Knobs `neck_down`:
`{enabled: true, max_width_vs_pad: 1.0, min_length_mm: 0.5, max_length_mm: 3.0}`.

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

### 7.14 Connector detection & pin-swap advisor

**Detection LANDED 2026-07-23** (anchor): `detect_connectors(project_path,
ref_prefixes=None)` → tool `detect_kicad_connectors` (read-only, 79 tools) in
`kicad_pcb_tool.py`, plus the `pin_swap` block in `DEFAULT_PCB_SETTINGS`
(`{enabled:false, min_gain:25.0, ref_prefixes:["J","P","CN","X"]}`). Candidates
match by EITHER ref prefix OR footprint/library connector token (`conn`,
`header`, `connector`, `socket`, `terminal`), reported per-candidate via
`matched_by`; returns `ref`, `footprint`, `pin_count`, `pins` (pad+net from the
board's own pad nets). Never guesses swappability, never writes. The
exclusion-validation helper also landed: `validate_connector_exclusions(...)`
raises `ValueError` listing unresolved names AND the full detected-ref list
(case-insensitive resolve) — the loud-abort contract below, callable from the
session layer. 14 tests in `tests/test_connectors.py`. Measured on kiln: 24
J-prefixed connectors (J1–J25, J22 absent); J2 the only one matched by both
signals (`Connector_JST:JST_XH_B10B-XH-AM_1x10`); no P/CN/X or non-J
connector-token footprints present.

**Interaction contract (before optimization) — STILL TO BUILD (needs 7.6):**
present detected connectors and
ask (a) whether the optimizer may consider pin swaps at all, (b) which
connectors are off-limits, (c) optional per-connector swappable pin groups
(default: signal pins swappable within one connector; power/ground pins —
Phase 9/`_net_kind` classification — excluded). Exclusion validation is done
(the landed helper above); the session-layer prompt that calls it is not.

**Optimizer move (7.6 family):** a swap = re-terminating two nets' ratsnest
endpoints on that connector; scored like any move. **The tool never edits the
schematic.** If the best swap gains ≥ `pin_swap.min_gain` board-score points,
the session pauses (`awaiting_decision`, decision type `pin_swap`) and asks the
**user** — not the 7.7 AI — to make the change in the schematic and re-export
the netlist; the session re-syncs (netlist-staleness check) and continues.
Sub-threshold swaps are reported, not proposed. Knobs `pin_swap`:
`{enabled: false, min_gain: 25.0, ref_prefixes: ["J","P","CN","X"]}` (off by
default — consent-gated anyway).

### 7.15 Effort control & plateau-based stopping

- **Effort question at session start:** the session asks the user
  (AskUserQuestion) how much effort to spend, three defaults mapping to
  `optimizer.effort` presets — **quick** (one pass + cheap cleanup:
  max_iterations 5, replicas 1, greedy), **balanced** (today's defaults),
  **best** (SA on, replicas max, hours-scale time budget — "overnight") — plus
  free-form override of explicit knobs.
- **Plateau stopping (primary rule; the user-requested "iterate until the pace
  of improvement slows dramatically"):** track per-iteration board-score
  improvement; reference rate = mean of the first `plateau_window` productive
  iterations; **stop when the trailing-window mean rate <
  `plateau_slope_ratio` × reference** (defaults: window 3, ratio 0.1 — pace
  fell to a tenth), or on the hard budgets. Score curve + both rates land in
  the session log and viewer so "why did it stop" is inspectable.
  `convergence_delta` survives only as a floor for degenerate curves.
- Knobs under `optimizer`: `"effort": "balanced"`, `"plateau_window": 3`,
  `"plateau_slope_ratio": 0.1`.

### 7.16 Benchmark harness — other people's boards vs. the autorouter

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

## Phase 8 — LANDED 2026-07-21 (reference anchor; only its M2 docs item remains, in the build order)

`_infer_net_voltage(net_name, net_voltages, gnd_tokens)` (standalone helper
right after `_coerce_voltage` — **kept standalone deliberately**: Phase 7.2's
`power_net_patterns` says *whether* a net is power, this says *what voltage*,
and the autorouter/plane phases may adopt it, e.g. warning when zones of
different inferred voltages overlap) + `audit_capacitor_net_voltages` in
`kicad_pcb_tool.py`; registered as tool `audit_kicad_capacitor_net_voltages`
(72 tools total). Precedence override → gnd → label → none with per-net
`source`; 3V3/1V8 convention; GND-beats-label + `ambiguous_label`; verdicts
`under_rated`/`unknown_rating`/`under_derated`/`ok`/`one_net_unlabeled` (with
`assumed_applied_v`)/`no_labeled_nets`/`unsupported_pins`, sorted worst-first;
netlist-staleness cross-check as in `detect_buses`; knobs from
`schematic_checks.cap_voltage`. 12 tests in `tests/test_cap_net_voltage_audit.py`
+ new synthetic cap-schematic generator `tests/synthetic_cap_schematic.py`.

**Spec deviations recorded (approved at review, 2026-07-21):**
- Iterates `_flatten_schematic_components` (with `_CAPACITOR_REF_RE` + DNP
  exclusion) instead of `list_schematic_parts`' grouped output — the check is
  per-instance and grouping by Value+Footprint loses per-instance nets.
- `_VOLTAGE_RE` and digit-V-digit are combined into one alternation regex
  (digit-V-digit first) so `3V3` matches once instead of spuriously flagging
  `ambiguous_label`.
- Rows gain `rated_v_source` (`value`/`default`/`unknown`).

Verified on kiln: 68 caps — 0 under_rated, 24 unknown_rating, 0 under_derated,
3 ok, 31 one_net_unlabeled, 10 no_labeled_nets; `stale_netlist_warnings` empty.
Hand-checked: C9 (470 µF bulk cap, `12V_Main`↔`GND_Main`) → `unknown_rating`
is a **real schematic finding** — its live Value field is just `"470uf"`; the
stale `.net` cached value still says `"470uf 50v"` and the MPN (UCM1H471MNJ1MS)
is a 50 V part, so the schematic Value lost its rating at some point. C13
(regulator bootstrap cap across BST/SW) correctly `no_labeled_nets`. No cap on
kiln sits across two non-ground labeled rails (that path is synthetic-tested
only). Known limit: the staleness guard cross-checks net *names* only — it
cannot catch component-*value* staleness like C9's.

---

## Phase 9 — LANDED 2026-07-22 (reference anchor; residual test/heuristic items live in M6 item 17)

`classify_critical_nets` → tool `detect_kicad_critical_nets` (registered),
per the original spec: bus/net-name high-speed table via
`high_speed.bus_frequencies_mhz`, XTAL nets (ref `Y*`/`X*` or
crystal/resonator/osc footprint tokens, highest weight), switch-node inductor
nets, `L_crit = v × t_rise / 6` with the resolved table in the result, and
`get_trace_cost` length-multiplier integration. 16/16 tests in
`tests/test_critical_nets.py`. The kiln zero-result defect was two dict-key
bugs (both coordinator-reviewed 2026-07-22): bus candidates key members as
`"nets"` not `"members"`, and the XTAL block read the net-name-keyed map where
the ref-keyed `refs_to_nets` was needed (XTAL detection was dead for every
board).

Verified on kiln: 13 critical nets, all `bus_frequency` — 7 SPI
/MainControler/ (62.5 mm L_crit; CLK/CS0/CS1/MISO/MOSI trip the stack-up gate
at 81–108 mm straight-line), 2 I2C (L_crit 3.1 m — never gated), 4 SPI
/SaftyProcessor/ (28.8 mm, under gate). XTAL: 0 hits and correctly so — kiln
has no crystal part (the Nano's oscillator is on-module; verified across all
schematics + 259 board components). Switch node: 0 hits. Kiln
`get_trace_cost` board total 6241.7 → 8389.0 (the 13 nets' length costs ×4;
only the length term scales, as specced).

**Pre-route stack-up gate (still to build, with Flow B session-start
questions):** for every critical net with `stack_up_gate: true`, the session
asks whether to pause until impedance control / stack-up is configured (same
gate + recorded answer as 7.13's missing-profile case; stored in the
board-local JSON). The tool already computes and reports the flag.

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
- **Progress viewer (7.9)** — self-contained, spec'd, and decoupled from the
  router by the event-file contract: **Sonnet**, developed against a recorded
  JSONL event file rather than a live run.
- **numpy backend (7.8)** — mechanical vectorization of a proven cpu
  implementation with the parity suite as the acceptance gate: **Sonnet**. The
  **GPU tier** goes to **Opus**: it owns the batching/tiling/VRAM-budget design
  and the synthetic big-board benchmark suite, and must report parity results +
  runtime/memory numbers at 10x and 100x kiln scale, not just working code.
- **Docs (docs page, README, CLAUDE.md updates)** — **Haiku** subagent once code is
  merged, with the final tool list as input.
- **7.16 benchmark harness, 7.12 neck-down, 7.14 connector detection** —
  well-specified, reuse-heavy: **Sonnet** delegations. 7.14's acceptance must
  include the exclusion-validation error path.
- **7.13 impedance/matched sets and the 7.14 optimizer move + pause-the-user
  protocol** — algorithm-heavy, ride the router core: **Opus**, after their
  build-order prerequisites.
- Always: subagent output reviewed in the main session against this plan; each
  completed delegation removes its items from this file per "How to work this plan".

---

## MCP tool summary (new group: "net classes & buses")

Registered-and-landed rows are removed from this table per "How to work this
plan"; `detect_kicad_connectors` (7.14 detection) landed 2026-07-23 and is
gone. `route_kicad_nets`/`unroute_kicad_nets` are registered but `route_nets`
is still partial (no rip-up/plane/neck-down — see the 7.3b stage-2 anchor), so
they stay listed until 7.3b closes.

| Tool | Function | Writes? |
|------|----------|---------|
| `route_kicad_board` (7.17 minimal LANDED; also a CLI entry; planes/optimize/stitching pending) | `route_board` | **yes (board + board_local.json)** |
| `route_kicad_nets` (core landed; rip-up/plane/neck-down pending) | `route_nets` | **yes (board + board_local.json)** |
| `unroute_kicad_nets` (landed) | `unroute_nets` | **yes (board + board_local.json)** |
| `propose_kicad_plane` | `propose_plane` | no |
| `create_kicad_plane` | `create_plane` | **yes (board + board_local.json)** |
| `modify_kicad_plane` | `modify_plane` | **yes (board + board_local.json)** |
| `optimize_kicad_board` | `optimize_board` | **yes (board + board_local.json)** |
| `decide_kicad_route` | `decide_route` | **yes (board_local.json session)** |
| `get_kicad_route_session` | `get_route_session` | no |
| `open_kicad_route_viewer` | `open_route_viewer` | no (spawns viewer process) |
| `get_kicad_system_resources` | `probe_system_resources` | no |
| `adopt_kicad_routing` | `adopt_routing` | **yes (board_local.json)** |
| `seed_kicad_routing_from_board` | `seed_routing_from_board` | **yes (board + board_local.json)** |
| `remove_kicad_stitching_vias` | `remove_stitching_vias` | **yes (board + board_local.json)** |
| `benchmark_kicad_autoroute` | `benchmark_autoroute` | no (scratch copies + report files) |

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
**Remaining docs debt:** the `route_kicad_nets`/`route_kicad_board` pages get
revised as 7.5.4 plane-aware routing and 7.12 neck-down land (they change what
routes vs. fails), and future Phase 7 tools add rows as they land)
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
    the stage-2 anchor). **Remaining to close 7.3b:** 7.12
    neck-down on the pad-escape stub, direction-aware pad escape, "any same-net
    copper" termination, demoting self-check failures into the rip-up loop, and
    lifting the 60 mm / 400k-node window cap toward
    whole-board (needs the memory planner + numpy/multi-core waves — those slip
    to M5's accel work; the cpu tier remains the reference everything else must
    match). Plane-aware via-drops through pours need M4's 7.5 zone model.
11h. **[HEADLINE] Phase 7.17 minimal `route_board` — LANDED 2026-07-23** (see
    the 7.17 anchor): the one-command router (MCP tool `route_kicad_board` +
    `python kicad_router_tool.py route <project>` CLI), a thin orchestrator over
    ratsnest→global→detailed, `write=False` default, 6 tests, measured on kiln.
    It grows to add planes (M4), optimization/effort/decision auto-pick (M4),
    and accel (M5) without changing its signature. Still owed: a docs row on
    `11-autorouter.md`.
12. Phase 7.9 viewer — developed against a recorded JSONL event file as soon as
    7.3b emits events.

**M4 — Planes + whole-board optimization:**
13. Phase 7.5 plane engine — zone parser/`list_kicad_zones` + 7.5.2 fill +
    7.5.3 `audit_kicad_plane_islands` + 7.5.4 plane-aware routing all **LANDED
    2026-07-23** (see the 7.5.1, 7.5.2/7.5.3, and 7.5.4 anchors; kiln: 31
    islands, 1 orphan on safty_gnd F.Cu; plane moves in the detailed A*, signal
    parity by construction). **Remaining:** 7.5.5 propose/create/modify
    (writers last), then the 7.5.6 stitching pass +
    `remove_kicad_stitching_vias` + its ask-before-routing interaction rule
    (stitching is last in the run order AND last in this milestone). Plus the
    7.5.4 residuals: wire the estimated-fill fallback into routing, and relabel
    the `route_board`/`route_kicad_nets` `pipeline.plane_aware_routing` from
    `not_implemented` to `partial`.
14. Phase 7.6 optimizer + 7.7 decision protocol — greedy first, SA once greedy is
    trusted; sessions/resume before decisions (7.7 rides the session mechanism);
    7.7 verified with a scripted decider (canned answers) before a live AI sits
    in the loop. **7.15 effort presets + plateau stopping land with 7.6** (they
    are its stopping/budget layer). Viewer gains the cancel flag + decision
    banner. Portfolio replicas land here (a session feature).
15. Phase 7.10 warm start — adoption with 7.6 (ownership flag + backup rule);
    cross-board seeding after it (feeds 7.3a priors). Acceptance: adopt kiln's
    routing on a scratch copy, optimize, verify backup exists and the diff only
    touches owned copper.

**M5 — Acceleration:**
16. Phase 7.8 numpy tier (parity suite is the gate), then the GPU tier —
    committed, gated on parity + the M0 synthetic big-board benchmarks
    (runtime *and* memory budgets); OOM-fallback acceptance = a forced-tiny-VRAM
    run completes via demotion, not crash. Hybrid scheduling last, once both
    executors exist (hybrid vs cpu-only parity proves executor assignment can't
    change results).

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
    parser would close this. **Still open: (c)** the Flow B session-start
    stack-up-gate question (the tool already reports `stack_up_gate` per net).
18. Phase 7.13 impedance-matched sets (coupled pair routing + length-matching
    meanders + profiles/assignments) — after 7.3b; Opus.
19. Phase 7.14 connector pin-swap advisor — **detection LANDED 2026-07-23**
    (`detect_kicad_connectors` + `validate_connector_exclusions`, 14 tests; see
    the 7.14 anchor). Remaining: the optimizer swap move + pause-and-ask-the-user
    protocol (after 7.6) and the session-layer exclusion prompt that calls the
    landed validator.
20. Phase 7.16 benchmark harness + corpus (`benchmark_kicad_autoroute`) —
    after 7.3b for `strip_and_reroute`; corpus targets attach to M4/M5
    acceptance.
21. Optional Phase 5 refinements recorded in its anchor: per-station polyline
    centerline (S-shaped bundles read slightly high today) and
    equidistant-trunk splitting.

**Every milestone:** docs for its tools (`docs/mcp-tools/10-…`/`11-…`), README +
CLAUDE.md tool count/group sync, `.gitignore`/requirements entries when that
milestone introduces the file — not one big docs push at the end (the "Docs"
items in the documentation-updates section are consumed milestone by milestone).
