# Group 11: Autorouter & Detailed Routing

[< Back to README.md](../../README.md)

Phase 7.3 windowed A* detailed routing (fine-grained exact copper generation) and its supporting
infrastructure: ratsnest calculation, layer/constraint querying, and undo (unrouting). This group
covers the implemented core of the autorouter pipeline as it exists today, with honest documentation
of what is NOT yet implemented (rip-up/negotiation, plane-aware routing, neck-down). The routing
workflow is: **ratsnest** (find unrouted connections) → **global route** (7.3a, decide layers/corridors)
→ **detail route** (7.3b core, this group) → **self-check** (before write) → **emit**.

## `get_kicad_ratsnest`

List every unrouted connection (missing conductor) on the board. Connectivity is computed using
union-find over each net's pads + existing routed copper (segments/arcs/vias) AND its filled zone
polygons, using the board file's own pad nets as ground truth (immune to `.net` netlist staleness).
Two items join when they share a copper layer and their copper overlaps within tolerance; a via
joins all layers it spans; a pad or trace over a same-net plane fill joins the fill (including
across thermal gaps).

For each net with ≥ 2 separate copper islands, the missing connections are reported as the
**Minimum Spanning Tree** (MST) decomposition over those islands — exactly one connection per
island pair, no cycles. Each connection reports the net, `from`/`to` island representatives
(nearest pad refs or copper/zone uuid), airline distance in mm, and the layers each side lives on.

Connections are ordered **most-constrained-first** (by `net_overrides.priority` from the board-local
JSON, descending, then shortest airline first) — the same order the detailed router will consume
them. Summary includes fully-routed net count (≥2 pads, single island), unrouted nets, single-pad
nets (no connections possible), and free-copper nets (copper without attached pads).

**Read-only; pass `nets` to restrict to specific net names.**

**Args:** `project_path`, `nets` (optional array of net names; omit for whole board)

**Example output (excerpt):**
```json
{
  "board_path": "path/to/kiln.kicad_pcb",
  "copper_layers": ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"],
  "summary": {
    "total_connections": 87,
    "total_airline_mm": 2341.5,
    "fully_routed_net_count": 52,
    "unrouted_net_count": 19,
    "single_pad_net_count": 3,
    "free_copper_net_count": 0
  },
  "unrouted_nets": ["/MainControler/CLK", "/Power/VBUS", ...],
  "connections": [
    {
      "net": "/MainControler/CLK",
      "priority": 10.0,
      "from": {"ref": "U4", "pad": "5", "layers": ["F.Cu"]},
      "to": {"ref": "U5", "pad": "2", "layers": ["F.Cu"]},
      "airline_length_mm": 12.34,
      "from_layers": ["F.Cu"],
      "to_layers": ["F.Cu"]
    }
  ]
}
```

## `get_kicad_board_layers`

Parse the board file's copper stack (the top-level `(layers ...)` block) into a structured list
of copper layers in physical stack order (front to back). Each layer carries its KiCad-designated
purpose — `signal`, `power`, `mixed`, or `jumper` — which the trace-cost layer multipliers and
the Phase 7 router's cost model key off.

**Read-only; no parameters beyond project_path.**

**Args:** `project_path`

**Example output:**
```json
{
  "board_path": "path/to/kiln.kicad_pcb",
  "copper_layer_count": 4,
  "type_counts": {
    "signal": 2,
    "power": 2
  },
  "layers": [
    {
      "name": "F.Cu",
      "ordinal": 0,
      "type": "signal",
      "user_name": "Front"
    },
    {
      "name": "In1.Cu",
      "ordinal": 1,
      "type": "power",
      "user_name": "Power1"
    },
    {
      "name": "In2.Cu",
      "ordinal": 2,
      "type": "power",
      "user_name": "Power2"
    },
    {
      "name": "B.Cu",
      "ordinal": 3,
      "type": "signal",
      "user_name": "Back"
    }
  ]
}
```

## `get_kicad_drc_constraints`

Resolve the project's design-rule constraints into one merged table, in precedence order (highest
to lowest):
1. **`.kicad_dru` file** (custom rule file, e.g. JLCPCB.kicad_dru.txt)
2. **`.kicad_pro`** net-class and board design-settings rules
3. **`pcb_settings.json` autorouter fallback** (e.g. `clearance_fallback_mm`)

Parses `(rule ...)` constraint blocks for clearance, track_width, via diameter/drill/annular,
hole-to-hole, and edge clearance. Only evaluates offline-evaluable conditions (netclass, layer,
net name); rules whose conditions depend on pairwise predicates (e.g. `B.Type`, `B.Net`) are
listed in `unsupported_rules` with the reason — never silently ignored.

Each resolved constraint carries its value plus a `sources` list tracing which rule/board setting
supplied it, so results are fully auditable. The raw `net_classes` and `board_rules` maps are
also returned for per-net resolution. This is the single resolver every geometric router stage
(obstacle inflation, emit widths, self-check) consumes.

**Read-only; cached by `.kicad_dru` and `.kicad_pro` mtime/size.**

**Args:** `project_path`

**Example output (excerpt):**
```json
{
  "board_path": "path/to/kiln.kicad_pcb",
  "dru_file": "path/to/JLCPCB.kicad_dru.txt",
  "constraints": {
    "clearance": {
      "value": 0.2,
      "sources": [
        {"type": "dru_rule", "rule_name": "MinClearance", "layer": null},
        {"type": "netclass", "netclass": "Default", "key": "clearance"}
      ]
    },
    "track_width": {
      "value": 0.25,
      "sources": [
        {"type": "dru_rule", "rule_name": "MinTrackWidth", "layer": "F.Cu"}
      ]
    }
  },
  "net_classes": {
    "Default": {"clearance": 0.2, "track_width": 0.25},
    "Power": {"clearance": 0.3, "track_width": 0.5}
  },
  "unsupported_rules": [
    {"name": "PairwiseClearance", "condition": "A.Type==\"Track\" && B.Type==\"Via\"",
     "reason": "Unsupported predicate: B.Type"}
  ]
}
```

## `route_kicad_nets`

**Phase 7.3b — Detailed (fine, windowed) Autorouter**

Route unrouted connections from `get_kicad_ratsnest` into exact copper geometry. For each
connection (in priority-desc/airline-asc order from ratsnest):

1. **Obstacle window** — Rasterize only a bounding box around the connection + `search_window_margin_mm`,
   doubling up to the whole board on A* failure.
2. **Pad escape** — Find the nearest legal grid node to each endpoint pad and emit a stub from
   the pad's center.
3. **Fine A\*** — Integer-milli-cost search over (x, y, layer) softly constrained to the global
   stage's corridor (from Phase 7.3a if available).
4. **Self-check** — Prove every proposed segment/via against ALL copper at netclass clearance BEFORE
   any write. Clearance always resolves from the Default net-class (never a bare 0).
5. **Emit** — Append simplified `(segment)`/`(via)` blocks via top-level surgery; record their
   uuids in board-local `autorouter_owned` so `unroute_kicad_nets` can undo them.

Newly emitted copper becomes an obstacle for later connections in the same run, so multiple routed
nets in one call stay DRC-clean against each other.

**NOT YET IMPLEMENTED (Planned):**
- Step 4's PathFinder negotiated-congestion **rip-up & reroute** — connections that cannot fit in
  their window without ripping existing autorouter copper currently FAIL with their nearest blocker
  named. The parameter `max_ripup_iterations` is accepted and reported but only window-doubling retry
  is active.
- **Plane-aware routing** — On plane-filled layers (power/ground pours), only pour-free channels
  route today. Full plane-aware routing (via drops through pours, plane-aware A\*) is a later phase.
- **Neck-down** (7.12) — Not yet applied.

**write=false** (default) returns a full preview — per connection: `routed` flag, `length_mm`, via
count, layers used, est. Phase-6 cost, self-check result, and failures with reasons — WITHOUT
touching the board. **Always preview first.**

**Args:** `project_path`, `nets` (optional array; omit to route all unrouted connections), `connections`
(optional explicit connection list from `get_ratsnest`), `write` (default false), `allow_while_open`
(default false), `max_ripup_iterations` (accepted but window-doubling retry only)

**Example output (excerpt — single routed connection):**
```json
{
  "board_path": "path/to/kiln.kicad_pcb",
  "grid_mm": 0.2,
  "write": false,
  "written": false,
  "ripup_active": false,
  "rules": {
    "track_width": 0.25,
    "via_diameter": 0.8,
    "via_drill": 0.4,
    "clearance": 0.2,
    "edge_clearance": 0.3
  },
  "connections": [
    {
      "net": "/MainControler/CLK",
      "net_kind": "signal",
      "from_point": {"x": 100.5, "y": 50.25},
      "to_point": {"x": 115.75, "y": 50.25},
      "airline_length_mm": 15.25,
      "home_layer": "F.Cu",
      "routed": true,
      "length_mm": 16.5,
      "via_count": 0,
      "layers": ["F.Cu"],
      "segment_count": 3,
      "window_margin_mm": 8.0,
      "est_phase6_cost": 16.5,
      "self_check": {"passed": true, "violation_count": 0}
    }
  ],
  "summary": {
    "total_connections": 87,
    "connections_routed": 82,
    "connections_failed": 5,
    "segments_emitted": 0,
    "vias_emitted": 0,
    "total_length_mm": 0.0
  }
}
```

## `unroute_kicad_nets`

**The Undo for `route_kicad_nets`**

Delete autorouter-owned copper (segments and vias) recorded in the board-local `autorouter_owned`
state. Human-routed copper is NEVER touched — only segments/vias that the autorouter itself emitted
are candidates for removal. Pass `nets` to restrict deletion to specific net names; omit to remove
all autorouter-owned copper.

**write=false** (default) previews the uuids that would be removed without touching the board.

**Read-only** when `write=false`; **destructive** when `write=true` (only removes autorouter-owned copper, never human copper).

**Args:** `project_path`, `nets` (optional array; omit to remove all autorouter-owned copper), `write` (default false), `allow_while_open` (default false)

**Example output:**
```json
{
  "board_path": "path/to/kiln.kicad_pcb",
  "write": false,
  "written": false,
  "nets": null,
  "candidates": 152,
  "removed": 0,
  "removed_uuids": [
    "12345678-1234-1234-1234-123456789abc",
    "87654321-4321-4321-4321-abcdef123456",
    ...
  ]
}
```

## `route_kicad_board`

**Phase 7.17 — The Headline Board Router**

The one-command orchestrator to route an entire board. Thin wrapper that calls the routing
pipeline stages (ratsnest → global → detailed) in sequence and rolls their results into a
single comprehensive report. No routing logic of its own — it calls existing functions
(`get_kicad_ratsnest`, `route_kicad_nets`) and synthesizes their outputs.

`effort` controls rip-up aggressiveness only (for now):
- `"quick"` — single pass, no rip-up (`max_ripup_iterations=0`)
- `"balanced"` — default strategy (KiCad's pcb_settings config)
- `"best"` — aggressive rip-up (`max_ripup_iterations=20`)

Higher efforts become more meaningful when Phase 7.6 (whole-board optimizer) lands; that is
documented honestly in the report's `notes`.

**NOT YET IMPLEMENTED (Marked as M4 Hooks):**
- Plane-aware routing (Phase 7.5)
- Whole-board optimization (Phase 7.6)
- Stitching pass (Phase 7.5.6)

The report's `pipeline` block lists each stage with its status, transparently marking not-yet-wired
stages so callers know what's actually running.

**write=false** (default) previews the full result without touching the board; `write=true` emits
copper and records ownership for undo.

**CLI usage:** `python kicad_router_tool.py route <project> [--write] [--nets ...] [--effort quick|balanced|best]`

**Args:** `project_path`, `nets` (optional array of net names; omit to route all unrouted),
`write` (default false), `effort` (default "balanced"), `allow_while_open` (default false)

**Example output (excerpt):**
```json
{
  "command": "route_board",
  "board_path": "path/to/kiln.kicad_pcb",
  "effort": "balanced",
  "write": false,
  "written": false,
  "unrouted_before": 87,
  "unrouted_nets_before": ["/MainControler/CLK", "/Power/VBUS", ...],
  "airline_before_mm": 2341.5,
  "routed": 82,
  "failed": 5,
  "total_routed_length_mm": 2450.75,
  "vias_emitted": 142,
  "ripup": {
    "iterations": 3,
    "connections_ripped": 12,
    "congestion_escalations": 2
  },
  "pipeline": {
    "ratsnest": "done",
    "global_route": "done",
    "detailed_route": "done",
    "rip_up": "active",
    "plane_aware_routing": "not_implemented (Phase 7.5, M4)",
    "whole_board_optimization": "not_implemented (Phase 7.6, M4)",
    "stitching": "not_implemented (Phase 7.5.6, M4)"
  },
  "notes": [
    "Minimal route_board (Phase 7.17): ratsnest -> global -> detailed only; planes/optimizer/stitching are M4 TODO hooks and do not run yet.",
    "effort currently maps only to rip-up aggressiveness (quick=0, balanced=config default, best=20)."
  ]
}
```

## `list_kicad_zones`

**Phase 7.5.1 — Zone Inspection (Read-Only)**

Parse every board-level copper zone and keepout zone (no footprint-nested zones; those are pad
keepouts and not planes). Returns the zone name, net, copper-layer list (always an array; KiCad 9
multi-layer zones are native on this board, e.g. `mainGnd` spans `F.Cu`, `In1.Cu`, `B.Cu`),
uuid, priority, hatch settings, `connect_pads` mode/clearance, `min_thickness`, fill settings
(including `island_removal_mode` — every zone on this board allows islands, so downstream
costing must not assume single-component fills), the zone's outline `polygon`, and `filled_polygon`
blocks when present (never fabricated — that is Phase 7.5.2's job; the filled data is the model
input only).

**Keepout zones** (no net) and copper zones (net-owning) are both listed. A multi-layer zone
contributes one entry with `layers` as a list; its outline polygon is shared across all layers.

**Read-only; no parameters beyond project_path.**

**Args:** `project_path`

**Example output (excerpt — single zone):**
```json
{
  "board_path": "path/to/kiln.kicad_pcb",
  "zone_count": 6,
  "zones": [
    {
      "uuid": "12345678-abcd-1234-abcd-123456789abc",
      "name": "mainGnd",
      "net": "GND_Main",
      "layers": ["F.Cu", "In1.Cu", "B.Cu"],
      "priority": 0,
      "hatch": {"style": "edge", "pitch": 0.5},
      "connect_pads": {"mode": "solid", "clearance": 0.2},
      "min_thickness": 0.2,
      "fill": {
        "enabled": true,
        "island_removal_mode": 0,
        "smoothing": "none"
      },
      "island_removal_mode": 0,
      "keepout": null,
      "polygon": [
        {"x": 10.0, "y": 20.0},
        {"x": 100.0, "y": 20.0},
        {"x": 100.0, "y": 80.0},
        {"x": 10.0, "y": 80.0}
      ],
      "filled_polygon": [
        {
          "layer": "F.Cu",
          "pts": [
            {"x": 10.2, "y": 20.2},
            {"x": 15.5, "y": 20.2},
            ...
          ]
        },
        {
          "layer": "In1.Cu",
          "pts": [...]
        }
      ]
    }
  ]
}
```

## `audit_kicad_plane_islands`

**Phase 7.5.2/7.5.3 — Fill Model & Island Costing (Read-Only)**

Comprehensive fill and island analysis per net-owning zone/layer. For every zone carrying a net
(keepout/no-net zones excluded):

1. **Fill Source** — `"kicad"` when the zone carries real `filled_polygon` data from a KiCad
   board file; `"estimated"` when the zone is missing filled data (never filled in KiCad, or a
   synthetic board — the outline is rasterized at the router grid, higher-priority zones and
   foreign-net copper are subtracted, and what remains is flood-filled into connected components).

2. **Components** — Per zone/layer: the mainland (largest/most attachments), islands (secondary
   components), orphans (zero attachments), and `will_be_removed` (when `island_removal_mode == 1`,
   meaning KiCad deletes them on refill — never costed or offered stitching).

3. **Attachments** — Per component: same-net pads reaching it (thermal or solid `connect_pads` both
   bridge via the same contact-reach tolerance used for thermal-relief gaps) plus same-net vias
   landing on that layer inside the component. Attachment count drives island costing.

4. **Costing** — Per island: cost = `island_base / attachment_count`. Orphans cost `orphan_island`
   (fixed penalty for zero attachments). Mainland costs 0. Islands below `island_min_attachments_warn`
   are flagged. For every costed island, `suggested_stitching_via` proposes the nearest via position
   to the mainland component with the new attachment count and projected cost after stitching.

5. **Warnings** — Zones/islands with low attachment counts are listed for review.

Plane settings (`island_base`, `orphan_island`, `island_min_attachments_warn`) and autorouter grid
are read from `pcb_settings.json` or defaults.

**Read-only; no parameters beyond project_path.**

**Args:** `project_path`

**Example output (excerpt):**
```json
{
  "board_path": "path/to/kiln.kicad_pcb",
  "plane_settings": {
    "plane_step": 0.05,
    "island_base": 40.0,
    "orphan_island": 1000.0,
    "island_min_attachments_warn": 2
  },
  "zones": [
    {
      "uuid": "12345678-abcd-1234-abcd-123456789abc",
      "name": "mainGnd",
      "net": "GND_Main",
      "priority": 0,
      "island_removal_mode": 0,
      "layers": [
        {
          "layer": "F.Cu",
          "fill_source": "kicad",
          "component_count": 3,
          "components": [
            {
              "role": "mainland",
              "attachment_count": 47,
              "attachments": [
                {
                  "kind": "pad",
                  "reference": "U1",
                  "pad": "1",
                  "position": {"x": 50.5, "y": 60.25}
                },
                {
                  "kind": "via",
                  "uuid": "via-uuid-1",
                  "position": {"x": 55.0, "y": 65.0}
                }
              ],
              "area_mm2": 150.25,
              "cost": 0.0,
              "warn": false
            },
            {
              "role": "island",
              "attachment_count": 3,
              "attachments": [...],
              "area_mm2": 8.5,
              "cost": 13.3333,
              "warn": false,
              "suggested_stitching_via": {
                "position": {"x": 72.1, "y": 58.3},
                "nearest_mainland_point": {"x": 70.5, "y": 60.0},
                "distance_to_mainland_mm": 2.8,
                "projected_attachment_count": 4,
                "projected_cost": 10.0
              }
            },
            {
              "role": "orphan",
              "attachment_count": 0,
              "attachments": [],
              "area_mm2": 1.2,
              "cost": 1000.0,
              "warn": true,
              "suggested_stitching_via": {
                "position": {"x": 15.0, "y": 35.0},
                "nearest_mainland_point": {"x": 18.5, "y": 35.0},
                "distance_to_mainland_mm": 3.5,
                "projected_attachment_count": 1,
                "projected_cost": 40.0
              }
            }
          ]
        }
      ]
    }
  ],
  "summary": {
    "island_count": 12,
    "orphan_island_count": 2,
    "total_island_cost": 187.45,
    "warnings": [
      {
        "zone": "mainGnd",
        "layer": "F.Cu",
        "attachment_count": 1,
        "role": "island"
      }
    ]
  }
}
```

## `open_kicad_route_viewer`

**Phase 7.9 — Live Route-Progress Viewer**

Spawn a detached tkinter subprocess (`kicad_route_viewer.py`) that tails the JSONL event stream
(`<board>.route_progress.jsonl`, written by `route_kicad_nets`/`route_kicad_board` and gated by
`autorouter.progress.events`) to display a live board view with progress bars and copper updates
as routing happens. The viewer is fully decoupled by construction: the router only ever appends to
the event file, never talks back to the viewer, so the viewer can be opened, closed, or restarted
without touching or blocking an in-flight route.

**Observational-only failure mode:** If tkinter is unavailable (headless CI/container), this
returns `{"launched": False, "reason": "tkinter is not available..."}` instead of raising. The MCP
server keeps running headless even though the viewer cannot launch.

The viewer's **"Stop after this iteration"** button writes a cancel flag into the board-local JSON
state, which `route_kicad_nets` polls between connections — the safe cancel path a headless MCP
session would otherwise lack.

Also auto-launched internally by `route_kicad_nets` / `route_kicad_board` when
`autorouter.progress.open_viewer` is true in `pcb_settings.json`.

**Args:** `project_path` (required), `board` (optional; explicit `.kicad_pcb` path if it differs
from the project)

**Example output:**
```json
{
  "launched": true,
  "pid": 12345,
  "board_path": "path/to/kiln.kicad_pcb",
  "viewer_script": "path/to/kicad_route_viewer.py"
}
```

**Example output (tkinter unavailable):**
```json
{
  "launched": false,
  "reason": "tkinter is not available in this Python environment; the route viewer is observational-only and cannot run headless."
}
```

## `benchmark_kicad_autoroute`

**Phase 7.16 — Benchmark Harness**

Score the autorouter against a human-routed board (your stated north star: "as well or better than
my hand-routed board", judged by the Phase-6 `get_kicad_trace_cost` board score). Never writes the
source board — it copies the entire project (board, `.kicad_pro`, `.net`, and `pcb_settings.json`/
board-local state when present) into a fresh scratch directory before measuring or routing.

Two modes:

- **`mode="complete_only"`** (default, primary acceptance metric) — Measures the human board's
  score and unrouted-connection count on the untouched scratch copy, then runs `route_board(write=True)`
  to route only what the human left unrouted. Reports completion %, copper length/vias added,
  post-route board score, and kicad-cli DRC delta (baseline vs post, new-violation count; auto-skipped
  if kicad-cli is unavailable).

- **`mode="strip_and_reroute"`** — Deletes ALL non-zone copper from the scratch copy (every
  top-level segment/via/arc; zones/footprints/edge-cuts are untouched), reroutes the whole board
  from zero, and compares the rerouted board to the HUMAN ORIGINAL (measured before stripping) on
  completion %, total length, via count, Phase-6 board score (identical weights both sides),
  per-layer copper utilization, DRC violation count, and runtime.

Returns a hand-vs-auto comparison dict. **First-class pass/fail fields** to check: `comparison.matched_or_beat_human`
(bool) and `comparison.verdict` (str).

**Caveat:** Full-kiln runs are slow (detailed A* is pure-Python); prefer a small test project for
quick checks.

**Args:** `source_board` (required; project path, never written), `mode` (default "complete_only"),
`effort` (default "balanced"; one of "quick", "balanced", "best"), `scratch_dir` (optional; omit
for a fresh `tempfile.mkdtemp()`)

**Example output (excerpt — `mode="complete_only"`):**
```json
{
  "command": "benchmark_autoroute",
  "mode": "complete_only",
  "source_board": "path/to/kiln.kicad_pcb",
  "scratch_board": "path/to/scratch/kiln.kicad_pcb",
  "scratch_owned": true,
  "human_score": {
    "total": 2450.75,
    "board_layers": {...},
    "unrouted_connections": 5
  },
  "post_score": {
    "total": 2380.5,
    "board_layers": {...},
    "unrouted_connections": 0
  },
  "comparison": {
    "matched_or_beat_human": true,
    "verdict": "PASS: autorouter matched/beat human board on Phase-6 score"
  },
  "route_report": {...}
}
```

## `optimize_kicad_board`

**Phase 7.6 — Iterative Whole-Board Optimization**

Optimize the routed board by minimizing a single composite score:

    S = SUM net trace costs (from `get_kicad_trace_cost` board total, including 7.2 layer-purpose
                             penalties and Phase 5 deviation terms)
      + SUM plane island costs (from `audit_kicad_plane_islands` summary)
      + optimizer.unrouted_penalty × unrouted-connection count (from `get_kicad_ratsnest`)

Every cost contributor is defined in `pcb_settings.json`, so "best" means exactly what that JSON
declares. The optimizer is a thin orchestrator over existing tools (`route_kicad_nets`,
`unroute_kicad_nets`, `propose_kicad_plane`, `create_kicad_plane`, `modify_kicad_plane`,
`get_kicad_ratsnest`, `get_kicad_trace_cost`, `audit_kicad_plane_islands`) and duplicates no
routing, scoring, or write logic.

**Sessions & Resumability:**
One MCP call runs a BOUNDED chunk (`max_iterations_per_call` iterations or `max_seconds` of wall
clock, whichever binds first) and returns `{session_id, state, score_curve, moves, ...}` with
`state` in `running | converged | budget_exhausted`. Pass `session_id` back to resume exactly
where it stopped (RNG state and all loop state checkpoint to the board-local JSON, so a session
survives an MCP restart and is inspectable via `get_kicad_route_session`).

**Six Move Types:**
Each iteration ranks nets by cost contribution (routed nets at their trace cost; unrouted nets at
penalty), takes `optimizer.worst_k` worst contributors, and generates up to six candidate moves:
1. **Rip-up+reroute** — Delete and re-emit a net's copper in a perturbed order.
2. **Bundle reroute** — Re-emit a whole bus bundle on its Phase 5 corridor (if available).
3. **Layer swap** — Move a net to a different layer (layer-purpose driven).
4. **Add stitching via** — Place a via to connect a plane island to the mainland.
5. **Create plane** — Add a new copper-pour zone for a power net if its cost is lower than routed copper.
6. **Modify plane** — Resize/reprioritize an existing autorouter-created zone.

Every candidate is scored on its own private copy of the board; acceptance follows
`optimizer.accept` policy: `greedy` (strict improvements only) or `sa` (simulated annealing, worse
moves accepted with probability exp(−ΔS/T), T *= `sa_cooling` each iteration). A `seed` makes
runs fully reproducible.

**Write Behavior:**
- **`write=false`** (default) — NEVER touches the real board on any call. ALL iteration happens on
  a private scratch copy of the project (`<project>.board_local.json` checkpoint survives MCP restart).
- **`write=true`** — Applies the session's FINAL accepted state (copper, vias, and zones together, as one consistent snapshot) onto the real board and records all ownership in `autorouter_owned`. **REFUSES** if the session is still `running` (no final state yet) or if the real board changed since the session started. `unroute_kicad_nets` still undoes all of it. Refill zones (Fill All Zones) and re-run DRC in KiCad after `write=true`.

**Safety:**
Human-routed copper and the six hand-made board zones (`mainGnd`, `safty_gnd`, `antenna`,
`main3.3`, `main12v`, `3.3v_safty`) are read-only inputs throughout — the moves reuse
`unroute_kicad_nets`/`modify_kicad_plane`, whose ownership guards (recording in `autorouter_owned`)
prevent mutation of non-autorouter copper/zones. The optimizer never bypasses these.

**Known Limitation:**
Phase 7.7 (AI-in-the-loop decision pause for near-tied candidate moves) is NOT implemented — there
is no `awaiting_decision` state and `optimizer.ai_decisions` is never read. Where 7.7 would pause,
this optimizer simply takes the best-scored candidate.

**Args:** `project_path`, `session_id` (optional; omit to start a new session), `max_iterations_per_call`
(default 3), `max_seconds` (optional; omit for iterations-only bounding), `seed` (optional; overrides
`optimizer.seed`), `accept` (optional; "greedy" or "sa"; overrides `optimizer.accept`), `max_iterations`
(optional; total SESSION budget; defaults to `optimizer.max_iterations`), `time_budget_s` (optional;
total SESSION time budget; defaults to `optimizer.time_budget_s`), `write` (default false), `allow_while_open`
(default false)

**Example output (excerpt):**
```json
{
  "command": "optimize_board",
  "board_path": "path/to/kiln.kicad_pcb",
  "write": false,
  "written": false,
  "session_id": "d9c4e1f5-7a8b-4c3d-9e2f-1a6b5c8d3e9f",
  "state": "running",
  "iteration": 12,
  "max_iterations": 50,
  "elapsed_s": 3.847,
  "time_budget_s": 60.0,
  "seed": 42,
  "accept": "sa",
  "temperature": 0.082543,
  "initial_score": {
    "total": 2580.5,
    "trace_cost": 2450.75,
    "plane_island_cost": 129.75,
    "unrouted_penalty": 0.0
  },
  "current_score": {
    "total": 2510.3,
    "trace_cost": 2410.5,
    "plane_island_cost": 99.8,
    "unrouted_penalty": 0.0
  },
  "best_score": {
    "total": 2510.3,
    "trace_cost": 2410.5,
    "plane_island_cost": 99.8,
    "unrouted_penalty": 0.0
  },
  "score_curve": [2580.5, 2575.2, 2570.1, 2560.8, 2550.3, 2545.1, 2540.2, 2530.5, 2525.3, 2520.1, 2515.8, 2510.3],
  "moves": [
    {
      "iteration": 1,
      "type": "rip_up_reroute",
      "accepted": true,
      "net": "/MainControler/CLK",
      "score_before": 2580.5,
      "score_after": 2575.2,
      "score_delta": -5.3,
      "reason": "improvement"
    },
    {
      "iteration": 2,
      "type": "layer_swap",
      "accepted": false,
      "net": "/Power/VBUS",
      "score_before": 2575.2,
      "score_after": 2578.1,
      "score_delta": 2.9,
      "reason": "rejected by greedy/sa policy"
    }
  ],
  "moves_accepted": 8,
  "moves_rejected": 4,
  "scratch_dir": "C:\\Temp\\kiln_scratch_d9c4e1f5",
  "score_delta": -70.2,
  "diff": {
    "segments_added": 12,
    "segments_removed": 18,
    "vias_added": 3,
    "vias_removed": 5,
    "zones_added": 1,
    "zones_modified": 2
  },
  "notes": [
    "Phase 7.7 (AI-in-the-loop decisions) is not implemented: this session state machine has three states and never reads optimizer.ai_decisions."
  ]
}
```

## `get_kicad_route_session`

**Phase 7.6 — READ-ONLY Session Status Reporter**

Inspect the state of an `optimize_kicad_board` session without advancing it by a single iteration.
Returns the session's state (`running | converged | budget_exhausted`), iteration count vs. budget,
elapsed time, seed, acceptance policy, simulated-annealing temperature, initial/current/best scores,
the per-iteration score curve, the full move log (what move type was tried, whether it was accepted,
and why), and the scratch board path.

**Read-only; omit `session_id` to report the board's most recently touched session.** Returns
`{"found": false, ...}` rather than raising when there is nothing to report, so a caller can poll
a board that has never been optimized.

**Args:** `project_path`, `session_id` (optional; omit for the most recently touched session)

**Example output (excerpt):**
```json
{
  "command": "get_route_session",
  "found": true,
  "session_id": "d9c4e1f5-7a8b-4c3d-9e2f-1a6b5c8d3e9f",
  "state": "converged",
  "iteration": 27,
  "max_iterations": 50,
  "elapsed_s": 8.523,
  "time_budget_s": 60.0,
  "seed": 42,
  "accept": "sa",
  "temperature": 0.001234,
  "initial_score": {
    "total": 2580.5,
    "trace_cost": 2450.75,
    "plane_island_cost": 129.75,
    "unrouted_penalty": 0.0
  },
  "current_score": {
    "total": 2502.1,
    "trace_cost": 2410.2,
    "plane_island_cost": 91.9,
    "unrouted_penalty": 0.0
  },
  "best_score": {
    "total": 2502.1,
    "trace_cost": 2410.2,
    "plane_island_cost": 91.9,
    "unrouted_penalty": 0.0
  },
  "score_curve": [2580.5, 2575.2, 2570.1, 2560.8, 2550.3, 2545.1, 2540.2, 2530.5, 2525.3, 2520.1, 2515.8, 2510.3, 2505.7, 2503.2, 2502.1, 2502.1, 2502.1, 2502.1, 2502.1, 2502.1, 2502.1, 2502.1, 2502.1, 2502.1, 2502.1, 2502.1, 2502.1],
  "moves": [
    {
      "iteration": 1,
      "type": "rip_up_reroute",
      "accepted": true,
      "net": "/MainControler/CLK",
      "score_before": 2580.5,
      "score_after": 2575.2,
      "score_delta": -5.3,
      "reason": "improvement"
    },
    {
      "iteration": 2,
      "type": "layer_swap",
      "accepted": false,
      "net": "/Power/VBUS",
      "score_before": 2575.2,
      "score_after": 2578.1,
      "score_delta": 2.9,
      "reason": "rejected by sa policy (prob 0.04)"
    },
    {
      "iteration": 14,
      "type": "create_plane",
      "accepted": true,
      "net": "/Power/GND_Main",
      "score_before": 2510.3,
      "score_after": 2505.7,
      "score_delta": -4.6,
      "reason": "improvement"
    }
  ],
  "moves_accepted": 18,
  "moves_rejected": 9,
  "scratch_dir": "C:\\Temp\\kiln_scratch_d9c4e1f5",
  "known_sessions": ["d9c4e1f5-7a8b-4c3d-9e2f-1a6b5c8d3e9f", "a1b2c3d4-e5f6-7a8b-9c0d-1e2f3a4b5c6d"]
}
```

**Example output (no session yet):**
```json
{
  "command": "get_route_session",
  "found": false,
  "session_id": null,
  "known_sessions": []
}
```

---

## Autorouter Architecture & Cost Model

### Routing Pipeline (Phases 7.3a–7.3b, Core Implemented Today)

1. **Connectivity & Ratsnest** (Phase 7.1/7.2) — `get_kicad_ratsnest` computes union-find islands per
   net and the MST spanning connections.
2. **Global Route** (Phase 7.3a, stubbed in this interface) — Decides which layers and coarse
   corridors each connection should use (not exposed yet).
3. **Detailed Route** (Phase 7.3b core, `route_kicad_nets`) — Fine A* in per-connection obstacle
   windows, emitting exact segments/vias.
4. **Self-Check** — Before any write, prove every segment/via against all copper at netclass
   clearance.
5. **Emit & Record** — Write copper to the board file and track ownership in `board-local.json`
   `autorouter_owned`.

### Cost Model & Layer Purpose

The A* cost function includes:
- **Base cost** — grid step distance (1 orthogonal, √2 diagonal).
- **Layer-purpose multiplier** — from `layer_purpose` config; signal-on-power layers cost more than
  signal-on-signal (the 7.2 layer-purpose concept).
- **Off-direction penalty** — when moving against a layer's preferred axis (7.3c); motivates
  preferential layer usage.
- **Via cost** — per-via base weight × type multiplier (through/microvia/blind).
- **Away-from-home** — penalty for dwelling on layers other than the net's home layer.
- **Off-corridor** — penalty for bus-bundle nets that wander outside their Phase-5 detected corridor.
- **Direction-change penalty** — penalizes turns, preferring straight lines.
- **Congestion** — scaled occupancy cost (never hard-forbidden, weights decide).

All weights are converted to integer milli-units at model build time, so all A* comparisons use
deterministic integer arithmetic (no floating-point tie-breaks).

### Ownership & Undo

When `route_kicad_nets` writes copper (`write=true`), each emitted segment/via is assigned a UUID
and recorded in the board-local `board-local.json` file under `autorouter_owned`. This record:
- Survives across multiple routing runs (additive).
- Is the ONLY source of truth for which copper the autorouter owns (vs. human-routed).
- Enables `unroute_kicad_nets` to undo by removing only those uuids, never touching human copper.

### Workflow: Preview → Review → Write

1. **Call `route_kicad_nets(..., write=false)`** — Get a full per-connection preview: routed/length/vias/layers/cost/failures, without touching the board. Always do this first.
2. **Review the connections** — Check for unexpected paths, failures, or layer choices.
3. **Call `route_kicad_nets(..., write=true)`** — Emit the copper and update board-local ownership.
4. **If needed, `unroute_kicad_nets(..., write=true)`** — Rip up and retry (e.g. with different settings).

### Board-Local State

The board-local metadata file (`<board>.board_local.json`, `.gitignored` and disposable) holds:
```json
{
  "version": 1,
  "autorouter_owned": {
    "segments": ["uuid-1", "uuid-2", ...],
    "vias": ["uuid-3", ...],
    "records": [
      {"uuid": "uuid-1", "net": "/MainControler/CLK", "kind": "segment"},
      ...
    ]
  },
  "net_overrides": {
    "/MainControler/CLK": {"priority": 10.0},
    ...
  }
}
```

This file is **gitignored** (disposable) because it's derived from the board state and routing
runs. It's used only for undo (`unroute_kicad_nets` reads `autorouter_owned` to know what to
delete) and priority ordering (ratsnest orders connections by `net_overrides.priority`).

### Failure Modes & Self-Check Violations

When a connection cannot be routed:
- **`window_too_large`** — The search window exceeds the node budget; the connection's endpoints
  are too far apart or the board is too congested to fit in available memory.
- **`unreachable_in_window`** — A\* failed to find a path even after window-doubling to the whole board.
  The report includes the `nearest_blocker` (the obstacle or copper closest to the goal).
- **`self_check_failed`** — A\* found a path, but the proposed copper failed the netclass-clearance
  proof before write (never written in this case). The report includes the first ≤8 violations
  and a total count. This indicates a bug (A\* should not produce violations) and usually means a
  grid/clearance mismatch in the window's obstacle model vs. the final clearance geometry.

### Known Limitations (Honest Documentation)

1. **Plane-aware routing NOT implemented** — Power/ground pours are treated as obstacles, and only
   pour-free channels (spaces between plane polygons) route. Via drops and plane-split routing are
   later phases.
2. **Rip-up & negotiate NOT implemented** — `max_ripup_iterations` is a stub. When a connection
   cannot fit without removing existing autorouter copper, it fails (never rips). Negotiated
   congestion re-costing and PathFinder-style netlist negotiation are planned.
3. **Neck-down NOT implemented** — Neck-down (trace width reduction for dense fanout, 7.12) is
   a later phase.
4. **Simplified pad escape** — Lands on the nearest free grid node, not a pad-direction-aware exact
   stub (a minor detail, but documented honestly).
5. **Termination on connection's `to` point** — Not "any same-net copper" and not a connection hub;
   exact per-connection routing, which is correct for tree-style nets.

### Tuning & Settings (pcb_settings.json)

The autorouter consumes settings from `pcb_settings.json` under the `autorouter` key:
```json
{
  "autorouter": {
    "grid_mm": 0.2,
    "search_window_margin_mm": 8.0,
    "max_ripup_iterations": 5,
    "allowed_layers": ["F.Cu", "B.Cu", "In1.Cu", "In2.Cu"],
    "clearance_fallback_mm": 0.2,
    "cost": {
      "step": 1.0,
      "via": 5.0,
      "away_from_home_per_mm": 0.5,
      "off_corridor": 1.0,
      "direction_change": 0.1,
      "off_direction": 3.0,
      "congestion": 1.0
    }
  }
}
```

---

## References

- **mykicadMcp/NETCLASS_PLAN.md** — Design document and roadmap (Phases 1–9, including planned
  autorouter stages not yet implemented).
- **get_kicad_trace_cost** (Group 10) — Scores routed copper and applies critical-net multipliers
  post-routing.
- **detect_kicad_critical_nets** (Group 10) — Classifies high-speed/critical nets so the cost model
  and future router stages prioritize them.
