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

## How to work this plan (living document — keep it current)

**This file is the source of truth for what's left to do, and must be edited as work
lands — not left to drift.** On every unit of work:

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

Via (311 present):
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

## Phase 1 — Trace/via geometry parser (foundation)

**New in `kicad_pcb_tool.py`:**

- `_parse_tracks(board_path) -> {"segments":[...], "vias":[...], "arcs":[...]}`
  Walk the s-expr tree for `segment`, `via`, and `gr_arc`-style `arc` copper nodes.
  Each segment: `{net, width, layer, start, end, length, uuid}` (length =
  Euclidean start→end; arcs approximated by chord + flagged `is_arc`). Each via:
  `{net, size, drill, layers, at, uuid}`.
- `_parse_tracks_cached(board_path)` — same `(mtime, size)` cache pattern as
  `_parse_board_components_cached`; add a `_track_cache` dict and invalidate it in
  `_invalidate_board_cache`.

**Public function + MCP tool:**

`get_net_track_widths(project_path, net=None) -> {...}`  → tool
`get_kicad_net_track_widths`

Per net, aggregate its copper:
```json
{
  "net": "/MainControler/MOSI",
  "segment_count": 45,
  "total_length_mm": 62.3,
  "widths": { "0.2": 40, "0.3": 5 },       // width -> segment count
  "dominant_width": 0.2,                     // length-weighted mode
  "min_width": 0.2, "max_width": 0.3,
  "layers": ["F.Cu", "B.Cu"],
  "via_sizes": { "0.6/0.3": 3 },             // size/drill -> count
  "is_uniform": false                        // more than one width present
}
```
`net=None` returns every routed net (sorted by name); `net="X"` returns one.
`is_uniform=false` is the signal that a net's routing disagrees with a single
width — the hook for later schematic/net-class reconciliation.

**Why length-weighted dominant width:** a net can have a few short stubs at a
different width; the width that matters for "what is this net actually routed at"
is the one carrying most of the copper length, not the most frequent tiny segment.

---

## Phase 2 — Project width/via inventory (menu of "previously used" values)

`get_project_track_inventory(project_path) -> {...}`  → tool
`get_kicad_track_inventory`

Board-wide, copper-only:
```json
{
  "track_widths": [ {"width": 0.2, "segment_count": 566, "length_mm": 1830, "nets": 34},
                    {"width": 0.3, "segment_count": 795, ...}, ... ],
  "via_sizes":    [ {"size": 0.6, "drill": 0.3, "count": 300},
                    {"size": 12,  "drill": 7,  "count": 11, "warning": "free/oversized"} ],
  "existing_netclasses": [ {"name":"Default","track_width":0.2,"via_diameter":0.6,...} ],
  "free_via_count": 11
}
```
This is the exact palette presented to the user in Phase 4 when asking which
width/via to standardize a class on — *only values already in the design*, sorted by
usage, so the answer is a pick-from-list, not free entry. `existing_netclasses` is
read from `kiln.kicad_pro`.

---

## Phase 3 — Bus detection & IC qualification

### 3a. Bus signal dictionary
`_BUS_SIGNATURES` — token sets matched against the **net basename** (last `/`-segment,
uppercased, stripped of a trailing index and separators). A bus type fires only when
its **required** roles are all present among nets sharing a group key.

| Bus  | Required role tokens | Optional |
|------|----------------------|----------|
| I2C  | SDA, SCL | — |
| SPI  | (MOSI\|SDO\|COPI), (MISO\|SDI\|CIPO), (SCK\|SCLK\|CLK) | CS/SS/nCS/CSn (0..n) |
| QSPI | SCK/SCLK, (IO0..IO3 \| DQ0..DQ3), CS | — |
| I2S  | (WS\|LRCLK\|FS), (BCLK\|SCK\|BCK), (SD\|SDIN\|SDOUT\|DIN\|DOUT) | MCLK |
| UART | (TX\|TXD), (RX\|RXD) | RTS, CTS, DTR |
| CAN  | (CANH\|CAN_H), (CANL\|CAN_L) | — |
| USB  | (D+\|DP\|DPLUS), (D-\|DM\|DMINUS) | VBUS, ID |
| SWD  | SWDIO, SWCLK | nRST/RESET |
| JTAG | TCK, TMS, TDI, TDO | nTRST |
| RS485/RS422 | A, B (qualified by transceiver IC) | Z, Y |
| Diff pair | `<base>_P`/`<base>_N`, `<base>+`/`<base>-`, `<base>P`/`<base>N` | — |
| Parallel bus | `<base>0..<base>n` (≥4 contiguous, e.g. A0..A15 / D0..D7) | — |

Ship 3a as data so new buses are one dict entry. Roles are regexes over the
normalized basename; keep a small alias table (CLK↔SCLK↔SCK, CS↔SS↔NSS, etc.).

### 3b. Candidate grouping
`detect_buses(project_path) -> {...}`  → tool `detect_kicad_buses`

1. Parse nets from `.net`. Normalize each net's basename and record its role hits.
2. Group candidate nets by **shared hierarchical prefix** (the path before the
   basename, e.g. `/MainControler/`) — signals on one bus almost always share a
   sheet prefix. Also try grouping by shared connected-IC as a fallback for flat
   designs.
3. Within a group, if a bus type's required roles are all covered, emit a candidate.

### 3c. IC qualification (the "same IC" requirement)
For each candidate, intersect the **component refs** on every member net (from the
netlist nodes), keeping only ICs (ref prefix `U`/`IC`/`Q`-transceiver; configurable).
- **Qualified**: a common IC is on all/most member nets → strong candidate.
  Record `common_ics`, and per-net which pins of that IC each touches.
- **Weak**: required roles present but no shared IC (e.g. bus fans out to a
  connector only) → still reported, flagged `qualified: false` with the reason.

Candidate shape:
```json
{
  "bus_type": "SPI",
  "confidence": "high",
  "group_prefix": "/MainControler/",
  "nets": [ {"net":".../MOSI","role":"MOSI","width_summary":{...from Phase 1...}},
            {"net":".../MISO","role":"MISO", ...}, ... ],
  "common_ics": ["U2"],                 // shared across all member nets
  "qualified": true,
  "member_widths": {"0.2": 4},          // union of dominant widths across the bus
  "suggested_class_name": "SPI_MainControler"
}
```
**Never auto-apply.** `detect_buses` returns candidates only; the caller presents
each to the user (`AskUserQuestion`) to confirm bus type, membership, and name
before anything is created (Phase 4).

---

## Phase 4 — Net class creation from PCB settings

### 4a. Propose settings from what's routed
`propose_netclass_from_nets(project_path, nets, name) -> {...}`  → tool
`propose_kicad_netclass`

Given a confirmed net list (from a bus or hand-picked), pull each net's Phase-1
width summary + via sizes and derive a proposed class:
- `track_width`: length-weighted dominant width across all member nets.
- `via_diameter`/`via_drill`: dominant via on those nets (fallback: Default class).
- `clearance`: inherit from Default unless overridden.
- Report `conflicts` when member nets are routed at differing widths, so the user
  chooses rather than the tool silently averaging.

Returns the proposed class **plus** the Phase-2 inventory menus so the caller can
ask the user "use 0.2 (used on 566 segs) / 0.3 (795 segs) / other?" and
"via 0.6/0.3 (×300)?". This is where the **AskUserQuestion** interaction happens —
options pre-filled from real project usage.

### 4b. Write the class
`create_netclass(project_path, name, settings, net_patterns, write=False,
allow_while_open=False) -> {...}`  → tool `create_kicad_netclass`

Edits **`kiln.kicad_pro`** JSON:
1. Append a class object to `net_settings.classes` (all keys KiCad expects — copy the
   Default object's shape, override `name`/`track_width`/`via_*`/`clearance`).
   Refuse on duplicate name.
2. Add `net_settings.netclass_patterns` entries mapping each member net to the class.
   Prefer one exact-name pattern per net (`^/MainControler/MOSI$`); optionally a
   single collapsed regex when the user opts in.
3. `write=False` (default) returns the full before/after diff of the JSON blocks and
   the resulting class; `write=True` saves.
4. Reuse `_check_not_locked_by_editor` — but note the lock is on the **board**; also
   check the `.kicad_pro` isn't held open, and warn that KiCad reloads net classes
   only on project reopen.

> KiCad applies net classes to routed copper only when you re-run the router or
> "Update from netclass"; creating the class changes the *rules*, not existing
> track widths. Tool docstrings must say this so the user isn't surprised the
> board looks unchanged.

### 4c. Optional: reconciliation report (the "compare to schematic" payoff)
`audit_netclass_conformance(project_path) -> {...}`  → tool
`audit_kicad_netclass_conformance`

For each net assigned to a class (via `netclass_patterns`), compare the class's
`track_width`/`via` to the net's **actual** routed dominant width (Phase 1). Emit
mismatches: "net X is in class SPI (0.2 mm) but 5 segments are routed at 0.3 mm."
This is the concrete artifact that lets net-class intent be checked against the
real PCB — the stated end goal.

---

## Phase 5 — Inter-trace ("area between the traces") measurement per bus

Goal: for each detected/confirmed bus, measure the board area the bundle's traces
enclose — the routing corridor between them — and do it **per destination IC** so a
bus that spurs off to several ICs is measured as the sub-bundles that actually run
together, not as one inflated envelope.

`measure_bus_corridor_areas(project_path, bus)` → tool
`measure_kicad_bus_corridor_area`
(input: a bus candidate object from `detect_buses`, or an explicit `{nets, hub_ic}`).

### 5.1 Why naïve approaches fail here
- **One convex hull of all bus copper** balloons the moment a bus fans out: a chip
  select spurring to a far IC drags the hull across empty board, and the "area
  between the traces" becomes mostly area between *unrelated* traces.
- Bus nets are **shared** (SCK/MOSI/MISO reach every slave) and **dedicated**
  (CS1→U2, CS2→U3). The physically-meaningful corridor near U2 is
  {SCK,MOSI,MISO,CS1} *in the stretch that runs to U2* — not the whole length of the
  shared nets.

So the problem is two-fold: **(a) group nets per destination IC**, and **(b) clip
each shared net's copper to the stretch belonging to that destination.**

### 5.2 Design — "anchor-and-corridor" per-IC bundles (recommended)

**Step A — Roles from qualification (reuse Phase 3c).**
- `hub_ic` = the IC common to all bus nets (SPI master, I2C controller, …).
- `destinations` = every other IC on any bus net.
- For each destination `D`: `bundle_nets(D)` = bus nets that connect to `D`.
- Tag each bus net `dedicated` (ICs == {hub, D} only → unambiguously D's, e.g. a CS)
  or `shared` (reaches hub + ≥2 destinations, e.g. SCK/MOSI).
- Degenerate cases handled explicitly: **single destination** → one bundle, no
  clipping; **no hub / weak bus** → one un-grouped hull with `grouped: false`.

**Step B — Anchor geometry from pads (reuse `_parse_footprint_pads`).**
- `hub_pt` = centroid of the hub IC's pads that sit on the bus nets.
- `dest_pt(D)` = centroid of D's pads on `bundle_nets(D)`.
- These give each bundle a physical *axis* `hub_pt → dest_pt(D)` to clip against.

**Step C — Assign/clip shared copper to the nearest destination.**
Each shared net's copper is split among destinations so a stretch is counted for the
bundle it physically runs with:
- For every copper segment of a shared net, assign it to the destination `D` whose
  axis `hub_pt→dest_pt(D)` the segment's midpoint is closest to (perpendicular
  distance), **and** that lies within a band of the bus's own pitch (see 5.4) of at
  least one already-assigned trace of that bundle — so a shared trace only joins a
  bundle where it actually runs alongside it, not where it has peeled away.
- Dedicated nets' copper is assigned wholesale to their destination.
- Segments that match no bundle within the band → reported as `unassigned` (the
  fan-out/transition copper) so nothing is silently dropped.

**Step D — Corridor area per bundle.** Two methods; report both, recommend the
first:
1. **`corridor` (recommended, literal "between the traces"):** build the bundle's
   centerline (ordered stations from `hub_pt` to `dest_pt(D)` — project each
   segment midpoint onto the axis, sort, resample at a fixed step). At each station
   take the perpendicular spread = distance from the leftmost to the rightmost
   bundle trace. Area ≈ Σ(spread × step). This is the true inter-trace corridor and
   is insensitive to bends (measured along the local axis, not a global hull).
2. **`hull` (sanity bound):** convex-hull area of the bundle's clipped copper
   (Andrew's monotone chain + shoelace). Always ≥ corridor area; the ratio flags
   how much the bundle bends or fans.

### 5.3 Output shape
```json
{
  "bus_type": "SPI",
  "hub_ic": "U1",
  "bundles": [
    { "destination_ic": "U2",
      "nets": [{"net":".../SCK","role":"shared"}, {"net":".../CS1","role":"dedicated"}, ...],
      "trace_count": 4,
      "axis": {"from":[x,y],"to":[x,y]}, "length_mm": 18.4,
      "corridor_area_mm2": 9.7, "hull_area_mm2": 12.1, "bend_ratio": 1.25,
      "mean_spacing_mm": 0.53, "layers": ["F.Cu"] },
    { "destination_ic": "U3", ... }
  ],
  "sum_of_bundle_areas_mm2": 27.9,   // shared copper counted once per bundle it serves
  "union_hull_area_mm2": 41.2,       // whole-bus envelope, for reference
  "unassigned_segment_count": 6,     // fan-out/transition copper not in any bundle
  "grouped": true,
  "warnings": ["net .../MISO has copper on 2 layers; corridor computed per layer then summed"]
}
```
Report **both** `sum_of_bundle_areas` and `union_hull` so double-counting of shared
copper (which is real — one physical SCK trace serves several bundles) is explicit
rather than hidden.

### 5.4 Details & correctness
- **Per layer**: compute corridors per copper layer, then sum — a bus that jumps
  F.Cu↔B.Cu shouldn't have its two layers' spread conflated. Vias mark the hops.
- **Pitch / band width**: default clip band = a few × the bus's dominant trace width
  (from Phase 1) or its median inter-trace spacing; expose as a parameter with a
  sensible default. Document that too-tight a band leaves shared copper
  `unassigned`, too-wide over-claims.
- **Only ≥2 traces**: a bundle needs at least two traces to have an area; a
  single-net "bundle" returns `corridor_area = 0` with just a length.
- **Geometry is pure stdlib** (convex hull, shoelace, point-segment distance,
  projection) — no `shapely`/`numpy`, matching the module's zero-extra-dependency
  posture (`requirements-mcp.txt` is just `mcp` + optional `kicad-python`). If
  robustness ever demands it, `shapely` can be an *optional* import behind a
  fallback, exactly like `kicad-python` gates the live-IPC tools.
- **Reuses**: Phase 1 `_parse_tracks_cached` for copper, `_parse_footprint_pads`
  for anchors, Phase 3c qualification for hub/destination roles. No new board
  parsing pass.
- Read-only; nothing is written.

### 5.5 Where it plugs into the flow
After a bus is confirmed (interaction flow step 2), `measure_kicad_bus_corridor_area`
gives the per-IC corridor areas — useful on its own (routing density, coupling area,
board-space budgeting) and as an input when deciding a bus's net-class width/spacing
in Phase 4.

---

## Phase 6 — Trace cost model + PCB settings JSON

Goal: score each routed trace with a single **cost** number built from (a) its
copper length, (b) how far it strays from its bus, and (c) its via count — with all
weights and knobs read from a project **settings JSON** so the model is tunable
without code changes. Read-only over the board; the only write is creating/seeding
the settings file.

### 6.1 The settings JSON (`pcb_settings.json`)

Lives in the **project directory** next to `kiln.kicad_pro` (so it's per-project and
versioned in git), resolved from the same directory `_resolve_project_path` already
finds. Absent file → in-code defaults are used, so every tool works out of the box;
the file only *overrides*. Centralizes tunables that today are scattered across the
plan (Phase 5 clip band, Phase 3 IC-prefix rules) so there's one place to tune board
policy.

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
  "optimizer": {                        // Phase 7.6: iterative whole-board optimization
    "max_iterations": 20,
    "time_budget_s": 300,
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

- `load_pcb_settings(project_path)` — deep-merge file over defaults; validate
  weights are non-negative numbers; return the effective config plus which keys came
  from the file vs defaults.
- `init_pcb_settings(project_path, write=False, allow_while_open=False)` — write the
  fully-populated default file (dry-run first, refuse to clobber an existing one
  unless `overwrite=True`). Plain JSON `json.dump(indent=2)`; it's our own file, not
  KiCad's, so no s-expr surgery and no board-lock concern (but still don't stomp a
  user-edited file silently).

### 6.2 Cost computation

`get_trace_cost(project_path, net=None) -> {...}` → tool `get_kicad_trace_cost`.

Per net (reusing Phase 1 copper, Phase 5 bundle geometry, and `_parse_footprint_pads`
for the hub→dest axis):

- **length_mm** — total copper length of the net (sum of segment/arc lengths, per
  Phase 1). `length_cost = w.length_mm * length_mm`.
- **via_count** — vias on the net, each scaled by its type via `via_weights`.
  `via_cost = w.via * Σ via_type_weight`.
- **deviation** — only defined when the net belongs to a detected bus bundle
  (Phase 5). Depending on `deviation.metric`:
  - `mean_perp_distance` / `max_perp_distance`: mean/max perpendicular distance of
    the net's segment midpoints from its bundle **centerline** → `deviation_cost =
    w.deviation_mm * value`.
  - `excess_length`: `ratio = actual_length / direct_distance(hub_pad→dest_pad) - 1`
    → `excess_cost = w.excess_length * max(ratio, 0)`.
  - Nets with no bundle use `non_bus_deviation` (default 0) and are flagged
    `on_bus: false`, so the model degrades cleanly to length+vias for point-to-point
    nets rather than guessing a reference.
- **layer_span** — count of copper layers the net occupies beyond its first;
  `span_cost = w.layer_span * (layers_used - 1)`. Prices multi-layer sprawl
  (see 7.3c — jumps that return to the home layer add vias, not span).
- **total** = sum of the enabled cost terms.

`net=None` → ranked list of every routed net, worst-cost first, plus board totals and
the `weights_used` block (so a result is self-describing / reproducible).

Output shape:
```json
{
  "net": "/MainControler/MOSI",
  "on_bus": true,
  "bundle": {"bus_type": "SPI", "destination_ic": "U2"},
  "metrics": { "length_mm": 62.3, "direct_mm": 41.0, "excess_length_ratio": 0.52,
               "via_count": 3, "via_types": {"through": 3},
               "mean_deviation_mm": 0.8, "max_deviation_mm": 2.1 },
  "cost":    { "length": 62.3, "vias": 15.0, "deviation": 1.6, "total": 78.9 },
  "weights_used": { "length_mm": 1.0, "via": 5.0, "deviation_mm": 2.0, ... }
}
```

### 6.3 Details & correctness
- **Deviation needs a bundle**, which needs Phase 5 → the deviation terms depend
  on Phases 1, 5 (and 3 for roles). Per the build order, Phase 6 ships in M1
  *before* Phase 5 with deviation reported as `on_bus: false` for every net;
  M2's Phase 5 unstubs it — the length/via terms are useful on day one. Shared bus nets are measured against the bundle centerline of
  each destination they serve; report per-destination deviation, and roll up to the
  net with the `metric`'s aggregate (max across destinations for `max_perp`, length-
  weighted mean for `mean_perp`).
- **Direct distance** for `excess_length` uses hub/dest **pad** positions
  (`_parse_footprint_pads`), not net-name geometry, so it's the true physical
  endpoints.
- **Weights are policy, not truth** — every cost result echoes `weights_used`, and
  the units are documented (mm, count) so scores are comparable only within one
  settings version. Bumping `version` signals an incomparable re-weighting.
- **All arithmetic pure stdlib**; reuses cached parses — no new board pass.
- Read-only except `init_pcb_settings` (writes `pcb_settings.json` only).

### 6.4 Flow
`get_kicad_trace_cost` (net=None) surfaces the worst-routed nets (long, via-heavy, or
straying from their bus) — a routing-quality triage list, and a natural companion to
Phase 5's corridor areas and Phase 4's net-class decisions. Tune the trade-offs by
editing `pcb_settings.json` (seed it with `init_kicad_pcb_settings`).

---

## Phase 7 — Python autorouter (grid A* with rip-up, layer-purpose aware)

Goal: route unrouted (or user-selected) nets **entirely in Python** — pure stdlib,
same zero-dependency posture — writing standard `(segment)`/`(via)` blocks into the
board file with the existing dry-run/write/lock-file discipline. Everything the
router needs (obstacles, clearances, costs, corridors, layer purposes) is computed
in Python from files already parsed by earlier phases; the MCP caller only picks
nets, reviews previews, and confirms writes.

### 7.1 Board-local state JSON (`<board>.board_local.json`, NOT in git)

Companion to `pcb_settings.json`, with the opposite contract:
- **`pcb_settings.json`** = shareable *policy* (weights, multipliers) → committed.
- **`<board>.board_local.json`** (e.g. `kiln.board_local.json`, next to the board
  file) = *state of this working board* → **gitignored**. The plan includes adding
  `/*.board_local.json` to the kilnCtl repo's `.gitignore` (and a note in the
  README that the file is disposable).

Contents (all optional, tools create/extend it as they run):
```json
{
  "version": 1,
  "autorouter_owned": { "segments": ["uuid", ...], "vias": ["uuid", ...] },
      // every uuid the autorouter has ever written -> rip-up/undo only ever
      // touches copper the router itself created, never human routing
  "keepouts": [ {"layer": "F.Cu", "rect": [x1, y1, x2, y2], "note": "antenna area"} ],
  "net_overrides": { "/MainControler/MOSI": {"priority": 10, "layers": ["F.Cu"]} },
  "confirmed_buses": [ { "bus_type": "SPI", "nets": [...], "hub_ic": "U1",
                         "name": "SPI_MainControler", "confirmed_on": "2026-07-21" } ],
      // Phase 3 user verifications cached here so re-runs don't re-ask
  "last_route_session": { "routed": [...], "failed": [...], "grid_mm": 0.2 }
}
```
`load_board_local` / `save_board_local` helpers mirror `load_pcb_settings` (deep
merge over `{}`, tolerant of a missing file). Caching **confirmed bus
verifications** here also retro-improves Phases 3–5: detection re-runs present only
*new* candidates and reuse prior confirmations.

### 7.2 Layer purposes from the board file

The board's `(layers ...)` block already types every layer — verified on kiln:
`F.Cu`/`B.Cu` are `signal`, `In1.Cu`/`In2.Cu` are `power` (KiCad also allows
`mixed` and `jumper`). New parser `_parse_board_layers(board_path)` (cached) returns
`{name, ordinal, type, user_name}` per copper layer.

Cost integration: each net gets a `net_kind` (`power` if its name matches
`layer_purpose.power_net_patterns` or its netclass says so; else `signal`), and
every grid step on a layer is multiplied by
`layer_purpose[net_kind][layer_type]` from `pcb_settings.json`. So routing a signal
across `In1.Cu` (a power plane) costs 4x, a power net is happiest on a `power`
layer, `mixed` is mildly penalized for everyone, and `jumper` layers are usable but
discouraged for continuous routing. Unknown/`user` layers are simply not routable.
The same multiplier table is also reported by `get_kicad_trace_cost` (Phase 6) as a
per-net `layer_penalty`, so *existing* routing that violates layer purpose shows up
in the cost triage too.

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
4. **Rip-up & reroute (negotiated congestion).** PathFinder-style on failure:
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

Companion tools: `unroute_kicad_nets` (delete autorouter-owned copper for given
nets — the undo), and `get_kicad_ratsnest` (list unrouted connections with airline
lengths — ships first, also useful standalone). Ratsnest/ordering as before:
union-find connectivity over pads+copper, MST decomposition of multi-pad nets,
most-constrained first, `net_overrides.priority` wins.

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

**7.5.1 Zone parser.** `_parse_zones(board_path)` (cached like the others): per
zone — `net`, `layers` (KiCad 9 multi-layer zones — present on this board), `uuid`,
`name`, `priority`, `hatch`/`connect_pads`/`min_thickness`/`fill` settings, outline
`polygon` points, and `filled_polygon` blocks when present. Exposed as
`list_kicad_zones`.

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

**7.5.4 Plane-aware routing.** For a net that owns a zone (GND_Main, 12V_Main, …),
A* gains plane moves: a connection may complete by dropping a via into fill
(`attachment_via` + `via` cost), then traversing fill cells at `plane_step` (island
cells at `plane_step` x current island factor, so paths prefer the mainland but may
buy into an island — and the via that does so *becomes* an attachment, cheapening
that island for every later path in the same run). This is why kiln's GND/12V nets
should mostly stop being traces at all.

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

---

## Phase 8 — Net-aware capacitor voltage check (schematic audit extension)

Goal: extend the schematic-check family with a check that reads **net names as
voltage labels** and verifies each capacitor's voltage rating against the voltage
it actually sits across — rating vs. |V(net_a) − V(net_b)| — instead of only
checking that a rating is written in the Value field (which is all the existing
`audit_capacitor_voltages` does). Knobs live in `pcb_settings.json` under
`schematic_checks.cap_voltage` (see 6.1).

`audit_capacitor_net_voltages(project_path)` → tool
`audit_kicad_capacitor_net_voltages`. Read-only. Sits beside (does not replace)
`audit_capacitor_voltages`, reusing its `_extract_voltage`/`_coerce_voltage`
helpers and `C<n>` reference convention.

### 8.1 Inferring a net's voltage from its name
Order of precedence, applied to the net's **basename** (last `/` segment):
1. **Explicit override** in `net_voltages` (exact, case-insensitive match) — for
   names that carry no number (`VBUS`, `AREF`).
2. **GND rule (as specified):** name contains any `gnd_tokens` token
   (case-insensitive substring) → **0 V**. Covers `GND_Main`, `GND_Safty`, `AGND`…
3. **Labeled voltage:** reuse the `_VOLTAGE_RE` pattern against the name —
   `12V_Main` → 12.0, `3.3v_Safty` → 3.3, `+5V` → 5.0. Also accept the `3V3`/`1V8`
   digit-V-digit convention (`(\d+)[vV](\d+)` → 3.3, 1.8). Multiple voltage tokens
   in one name → take the largest and flag the row `ambiguous_label`.
4. Otherwise → **unlabeled** (no voltage known).

Each inferred voltage is reported with its `source`
(`override | gnd | label | none`) so a wrong guess is visible, never silent.

### 8.2 The check, per capacitor
Capacitors from `list_schematic_parts` (`C<n>` refs); each instance's two nets from
the netlist (`_parse_nets` membership — same source Phase 3 uses). Per cap:

- **applied_v** = |V(net_a) − V(net_b)| when both nets resolve — the cap between
  12 V and 3.3 V rails sees 8.7 V, not 12; a decoupler `12V_Main`↔`GND_Main` sees
  12. This differential form is why the check needs *both* nets, per the request.
- **rated_v** = `_extract_voltage` on the Value field, else
  `default_cap_rating`, else unknown.
- **Verdict** (`derating_min_ratio` = 2.0 default):
  - `under_rated` — rated_v < applied_v (hard fail, e.g. 6.3 V cap on a 12 V rail),
  - `under_derated` — rated_v < ratio x applied_v (works, but violates derating
    policy — the common ceramic-DC-bias trap),
  - `ok` — rated_v ≥ ratio x applied_v,
  - `unknown_rating` — cap on a voltage-labeled net with no rating anywhere
    (this is precisely the cap worth chasing; listed first after failures),
  - `one_net_unlabeled` — one side resolved, other unknown: reported
    informationally with `assumed_applied_v` = the resolved side vs. 0 V
    (right for the overwhelmingly common rail↔signal decoupler case, and
    labeled as an assumption),
  - `no_labeled_nets` — neither side resolves; skipped from scoring, counted.
- Edge cases: caps with ≠2 pins (arrays/4-terminal) flagged `unsupported_pins`
  and skipped; DNP caps excluded (reusing list_schematic_parts' DNP handling);
  net names matching both a gnd token and a voltage label (`GND_5V_RTN`) → GND
  wins (precedence above) and the row is flagged `ambiguous_label`.

Output: rows sorted worst-first (`under_rated`, `unknown_rating`,
`under_derated`, …), each with reference, value, rated_v, both nets with their
inferred voltage + source, applied_v, required_min (= ratio x applied), verdict —
plus summary counts and the settings used (self-describing like Phase 6).

### 8.3 Fit with the rest of the plan
- Pure reuse: netlist parsing (Phase 0 infra), `_extract_voltage` (existing),
  settings loader (6.1). No new file parsing.
- The net-name voltage inference (8.1) is deliberately a standalone helper
  (`_infer_net_voltage`) — Phase 7.2's `power_net_patterns` says *whether* a net
  is power; this says *what voltage* — and the autorouter/plane phases may adopt
  it later (e.g. warning when zones of different inferred voltages overlap), so
  it must not be buried inside the audit function.
- Docs: covered in `docs/mcp-tools/10-netclasses-and-buses.md`'s audit section
  (or the existing schematic-data page `02-schematic-data.md` if that reads more
  naturally at write-time — implementer's call, note it in the doc commit).

---

## Implementation strategy — subagents

Work phases as sub-tasks delegated to subagents, keeping plan/decisions in the main
session (which also owns all user-facing verification questions):

- **Router core & geometry (Phases 5, 7.3), plane engine (7.5), optimizer +
  decision protocol (7.6/7.7)** — the algorithm-heavy code: delegate to an
  **Opus** subagent with the relevant plan section pasted in whole; require it to
  run against `kiln.kicad_pcb` (a scratch copy for anything that writes) and
  report measured numbers (corridor areas, routed lengths, island counts,
  before/after board score, global-stage runtimes), not just code. 7.3a, 7.3b,
  7.5, and 7.6/7.7 are separate delegations, each landed and reviewed before the
  next; 7.7's delegation must include the scripted-decider test harness.
- **Parsers, inventory, settings plumbing (Phases 1, 2, 6.1, 7.1, 7.2, 7.5.1)** —
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
- **Test infrastructure (M0)** — pytest fixtures and golden files: **Sonnet**;
  the synthetic board generator too (it reuses the emit helpers and its output
  must open in KiCad — acceptance is a screenshot of a generated board loaded
  in pcbnew without errors).
- **Bus signatures dictionary (3a)** and **net-voltage cap audit (Phase 8)** —
  well-specified, reuse-heavy work: Sonnet. Phase 8's acceptance test: run it on
  kiln and hand-check a few rows (a `12V_Main`↔`GND_Main` decoupler, a cap
  between two labeled rails, an unlabeled-net cap) against the schematic.
- Always: subagent output reviewed in the main session against this plan; each
  completed delegation removes its items from this file per "How to work this plan".

---

## MCP tool summary (new group: "net classes & buses")

| Tool | Function | Writes? |
|------|----------|---------|
| `get_kicad_net_track_widths` | `get_net_track_widths` | no |
| `get_kicad_track_inventory` | `get_project_track_inventory` | no |
| `detect_kicad_buses` | `detect_buses` | no |
| `measure_kicad_bus_corridor_area` | `measure_bus_corridor_areas` | no |
| `get_kicad_trace_cost` | `get_trace_cost` | no |
| `get_kicad_pcb_settings` | `load_pcb_settings` | no |
| `init_kicad_pcb_settings` | `init_pcb_settings` | **yes (pcb_settings.json)** |
| `propose_kicad_netclass` | `propose_netclass_from_nets` | no |
| `create_kicad_netclass` | `create_netclass` | **yes (.kicad_pro)** |
| `audit_kicad_netclass_conformance` | `audit_netclass_conformance` | no |
| `get_kicad_ratsnest` | `get_ratsnest` | no |
| `route_kicad_nets` | `route_nets` | **yes (board + board_local.json)** |
| `unroute_kicad_nets` | `unroute_nets` | **yes (board + board_local.json)** |
| `list_kicad_zones` | `list_zones` | no |
| `audit_kicad_plane_islands` | `audit_plane_islands` | no |
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
| `audit_kicad_capacitor_net_voltages` | `audit_capacitor_net_voltages` | no |

Each registered in `self.tools` with `inputSchema` + a `_tool_*` handler, exactly
like the existing entries.

### Documentation updates (required — part of this feature)
- **New** `docs/mcp-tools/10-netclasses-and-buses.md` — one section per tool above
  (purpose, inputs, output shape, example call/response), plus a short "how bus
  corridor area is computed / how spurs are grouped per IC" explainer so the
  measurement is reproducible by a reader, plus a "trace cost model & the
  `pcb_settings.json` schema" section (every weight/knob, its units, defaults, and a
  worked cost example).
- **`README.md`** — add the new tool group to the tool listing/count, a short
  "Net classes & bus analysis" blurb, and link the new docs page. (README is
  currently open in the editor.)
- **`CLAUDE.md`** — bump the "exposes 61 tools across 9 groups" line to the new
  count/group, add the new group to the group list and the
  `docs/mcp-tools/` reference, and note `NETCLASS_PLAN.md` as the design doc.
- Keep the tool count in README and CLAUDE.md in sync with the new tools added
  (Phases 1–7), and document **both** config files: `pcb_settings.json`
  (committed policy) and `<board>.board_local.json` (gitignored per-board state —
  document that it's disposable and how the autorouter uses it).
- Autorouter gets its own docs page `docs/mcp-tools/11-autorouter.md`: pipeline,
  cost model incl. layer-purpose multipliers, rip-up rules ("only autorouter-owned
  copper"), failure reporting, and the route→review→write workflow.
- Add `/*.board_local.json` and `/*.route_progress.jsonl` to the kilnCtl repo's
  `.gitignore` (with a comment, matching that file's existing style).
- `requirements-mcp.txt`: add commented-out optional `numpy`/`cupy`/`torch`
  entries in the `kicad-python` style, explaining what each tier accelerates
  (7.8) and that nothing requires them.
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
3. `get_kicad_ratsnest` → what's unrouted; `open_kicad_route_viewer` to watch.
4. `optimize_kicad_board` (or plain `route_kicad_nets` for a quick single pass) —
   chunk by chunk; answer `awaiting_decision` pauses via `decide_kicad_route`;
   plane proposals touching hand-made zones go to the **user**, not the AI.
5. Converged → review the dry-run diff, per-net costs, decision log, SVG/viewer →
   `write=True` (backup taken automatically if adopted copper changed).
6. In KiCad: refill zones (`B`), run DRC — the authoritative check. Iterate from
   step 4 if wanted; `unroute_kicad_nets` undoes any autorouter copper.

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
  rule, as `create_kicad_plane` already does for fill settings).
- **Coordinate formatting on emit**: new segments/vias/zone points use the same
  number formatting as `apply_layout_changes` (`_format_at_number`, ≤6 decimals,
  no trailing zeros) so diffs stay minimal and KiCad re-saves don't rewrite them.

## Build order — five shippable milestones

Phase 7 alone is ~10x the effort of Phases 1–6; without cut points the useful
early tools would sit unreleased behind the router. Each milestone below is
independently shippable (tools registered, docs row added, plan items deleted per
"How to work this plan") before the next begins.

**M0 — Test infrastructure** (small, first, owned like any deliverable):
1. `tests/` with pytest: golden-file tests for every parser against
   `kiln.kicad_pcb` snapshots; a scratch-board fixture (copy kiln to tmp, run
   writers there — no test ever touches the real board); round-trip harness
   (write → reparse → KiCad-shape assertions).
2. **Synthetic board generator** (required by 7.8's benchmarks, referenced
   nowhere else until now — it's a real deliverable): parameterized
   `.kicad_pcb` writer producing dense fanout fields, N-layer stacks, 10x/100x
   kiln-scale ratsnest. Lives in `tests/`, reuses the emit helpers.

**M1 — Net classes end-to-end (Flow A works):**
3. Phase 1 track parser + `get_kicad_net_track_widths`.
4. Phase 2 inventory.
5. Phase 3 bus detection + IC qualification.
6. Phase 6 settings JSON (`init/load_pcb_settings`) + `get_kicad_trace_cost`
   (deviation terms stubbed `on_bus: false` until M2 lands Phase 5).
7. Phase 4a/4b propose/create netclass (the `.kicad_pro` writer; dry-run first),
   then 4c conformance audit.

**M2 — Analysis suite:**
8. Phase 5 corridor areas (unstubs Phase 6's deviation terms).
9. Phase 8 net-aware cap voltage audit (independent; any time after step 6).

**M3 — Router MVP (routes real nets, single pass):**
10. Phase 7.1/7.2 board-local JSON + layer-purpose parser (also wires the
    layer-penalty report into Phase 6 output).
11. Phase 7.3 — ratsnest/connectivity first (`get_kicad_ratsnest` ships as soon
    as it works), then 7.3a global routing (candidate lists reviewable on their
    own — also the 7.7 decision surface), then 7.3b detailed: windows + pad
    escape + A* + rip-up + self-check + emit/unroute. Integer milli-cost
    quantization and the memory planner land here (cpu tier is the reference
    everything else must match); multi-core waves land here too (the wave
    decomposition is shared by all tiers; parity suite runs workers=1 vs N).
12. Phase 7.9 viewer — developed against a recorded JSONL event file as soon as
    7.3b emits events.

**M4 — Planes + whole-board optimization:**
13. Phase 7.5 plane engine — zone parser/`list_kicad_zones`, then fill estimation
    + `audit_kicad_plane_islands` (validated against KiCad's own fills on the six
    existing zones), then plane-aware routing, then propose/create/modify
    (writers last).
14. Phase 7.6 optimizer + 7.7 decision protocol — greedy first, SA once greedy is
    trusted; sessions/resume before decisions (7.7 rides the session mechanism);
    7.7 verified with a scripted decider (canned answers) before a live AI sits
    in the loop. Viewer gains the cancel flag + decision banner. Portfolio
    replicas land here (a session feature).
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

**Every milestone:** docs for its tools (`docs/mcp-tools/10-…`/`11-…`), README +
CLAUDE.md tool count/group sync, `.gitignore`/requirements entries when that
milestone introduces the file — not one big docs push at the end (the "Docs"
items in the documentation-updates section are consumed milestone by milestone).
