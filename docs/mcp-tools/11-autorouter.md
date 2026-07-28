# Group 11: Autorouter & Detailed Routing

[< Back to README.md](../../README.md)

Phase 7.3 windowed A* detailed routing (fine-grained exact copper generation) and its supporting
infrastructure: ratsnest calculation, layer/constraint querying, and undo (unrouting). This group
covers the implemented core of the autorouter pipeline as it exists today, with honest documentation
of active features and remaining planned work. The routing
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

stretch of copper at that endpoint is automatically narrowed to fit the pad. The neck width is
sized as a fraction of the class width, capped at the pad's smaller copper dimension, and floored
at the board's DRC minimum track width. Enabled by default via `pcb_settings.json`:
`neck_down: {enabled: true, max_width_vs_pad: 1.0, min_length_mm: 0.5, max_length_mm: 3.0}`.
The narrowed segment is self-checked at its true (narrow) width, so no conformance violations are
raised for a genuine neck. One residual: the Phase-5.x hierarchical last-resort routing tier (fallback
when detailed A* exhausts all windows) does not apply neck-down; connections that only route via
hierarchical placement will not emit necks (rare in practice).

**ACTIVE FEATURES (Phases 7.3b+):**
- **Step 4 negotiated-congestion rip-up & reroute** — When a connection cannot fit in its window
  without removing existing autorouter copper, the router rips ONLY autorouter-owned copper
  from the failed path (never human-routed copper by default), reschedules the ripped connections,
  and re-attempts them. Bounded by `pcb_settings.json`'s `max_ripup_iterations`. Off-by-default
  ripup of hand-routed copper is gated by separate `allow_hand_copper_ripup` flag.
- **Plane-aware routing (Phase 7.5.4, Partial)** — For power/ground nets that own a filled zone,
  the router recognizes moves onto the net's own plane fill as low-cost traversal and can terminate
  on any point already inside that net's own fill (not just the exact `to` point). On-plane moves
  cost plane-step × island-factor instead of normal trace cost. Known limitation: the A* heuristic
  is distance-only (pre-existing), so it is not cost-optimal for plane-discounted routes. Signal
  nets are unaffected (plane-aware gates are behind `is not None` checks, ensuring parity).

**write=false** (default) returns a full preview — per connection: `routed` flag, `length_mm`, via
count, layers used, est. Phase-6 cost, self-check result, and failures with reasons — WITHOUT
touching the board. **Always preview first.**

**Args:** `project_path`, `nets` (optional array; omit to route all unrouted connections), `connections`
(optional explicit connection list from `get_ratsnest`), `write` (default false), `allow_while_open`
(default false), `max_ripup_iterations` (default from pcb_settings.json; bounds rip-up iterations)

**Example output (excerpt — single routed connection):**
```json
{
  "board_path": "path/to/kiln.kicad_pcb",
  "grid_mm": 0.2,
  "write": false,
  "written": false,
  "ripup_active": true,
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
- Whole-board optimization (Phase 7.6)
- Stitching pass (Phase 7.5.6)

Plane-aware routing (Phase 7.5.4) is ACTIVE for power/ground nets; see `route_kicad_nets` for details.

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
    "plane_aware_routing": "partial (Phase 7.5.4 landed for power nets; heuristic not cost-optimal)",
    "whole_board_optimization": "not_implemented (Phase 7.6, M4)",
    "stitching": "not_implemented (Phase 7.5.6, M4)"
  },
  "notes": [
    "Minimal route_board (Phase 7.17): ratsnest -> global -> detailed only; optimizer/stitching are M4 TODO hooks and do not run yet.",
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

## `propose_kicad_plane`

**Phase 7.5.5 — Copper-Pour Plane Proposal (Read-Only)**

Propose a candidate copper-pour plane outline for a net on a specified or auto-picked copper
layer. The proposal runs read-only and has no ownership restriction — it's a suggestion for
human review, even if the net already owns one of the six hand-made kiln zones. Intended as the
first step: propose a plane, review the cost-delta estimate, then use `create_kicad_plane` to
commit if it looks good.

**Candidate Outline Computation:**
1. Collect all pads and vias on the net that touch the target layer.
2. Compute their bounding box, inflated by each pad/via's own reach (copper radius) plus a
   fixed margin (configurable in `pcb_settings.json` as `plane.propose_outline_margin_mm`,
   default 1.0 mm).
3. Clip to the board's `Edge.Cuts` extents.
4. Rasterize the outline at the autorouter grid and estimate components (mainland/island/orphan)
   using the same 7.5.2/7.5.3 pipeline as `audit_kicad_plane_islands` (subtracting
   higher-priority zones and clearance-inflated foreign copper).

**Cost Delta Estimation:**
The proposal computes `cost_delta` by projecting the plane's ongoing island cost (via 7.5.3
costing: islands cost `island_base / attachment_count`, orphans cost a fixed `orphan_island`
penalty) plus a one-time `create_plane` cost, then subtracting the net's current routed trace
cost (from `get_kicad_trace_cost`; 0.0 if unrouted). Negative `cost_delta` means the plane
looks cheaper than the net's current copper. This is a **simplified estimate, not a re-route
simulation** — it does not model what a signal net's copper would look like if removed and
rerouted after the plane exists.

**Layer Auto-Pick Behavior:**
If `layer` is omitted, the tool picks a copper layer whose `layer_purpose` TYPE (signal/power)
matches the net's own kind (7.2 classification), tie-broken by stack order (preferring front).
Pass an explicit `layer` if you want to propose on a specific layer regardless of its purpose.

**Read-only; no parameters beyond project_path, net, and optional layer.**

**Args:** `project_path`, `net`, `layer` (optional; omit to auto-pick by net kind / layer
purpose)

**Example output (excerpt):**
```json
{
  "board_path": "path/to/kiln.kicad_pcb",
  "net": "/Power/GND_Main",
  "net_kind": "power",
  "layer": "In1.Cu",
  "outline": [
    {"x": 10.5, "y": 20.25},
    {"x": 95.75, "y": 20.25},
    {"x": 95.75, "y": 80.5},
    {"x": 10.5, "y": 80.5}
  ],
  "outline_area_mm2": 5782.44,
  "component_count": 2,
  "components": [
    {
      "role": "mainland",
      "attachment_count": 52,
      "attachments": [
        {"kind": "pad", "reference": "U1", "pad": "1", "position": {"x": 50.5, "y": 60.25}},
        {"kind": "via", "uuid": "via-uuid-1", "position": {"x": 55.0, "y": 65.0}}
      ],
      "area_mm2": 5700.5,
      "cost": 0.0
    },
    {
      "role": "island",
      "attachment_count": 2,
      "attachments": [...],
      "area_mm2": 82.0,
      "cost": 20.0
    }
  ],
  "estimate": {
    "island_count": 1,
    "orphan_count": 0,
    "total_island_cost": 20.0,
    "create_plane_cost": 15.0,
    "projected_plane_cost": 35.0
  },
  "current_routing_cost": 45.5,
  "cost_delta": -10.5,
  "note": "cost_delta = (create_plane one-time cost + projected ongoing island cost) minus the net's CURRENT routed trace cost (0.0 if unrouted); negative means the plane looks cheaper than the net's current copper. Simplified estimate, not a re-route simulation."
}
```

## `create_kicad_plane`

**Phase 7.5.5 — Create a New Copper-Pour Zone**

Create a new copper-pour zone for a net on a specified or auto-picked copper layer, using
`propose_kicad_plane`'s candidate outline as the starting geometry. The zone's fill-setting
shape (hatch style, `connect_pads` clearance, `min_thickness`, thermal gap, smoothing) is copied
from an existing board zone using `_zone_template_shape` — the same "inherit a reference zone's
appearance" pattern `create_kicad_netclass` uses for the Default net-class, applied here to
zones. This ensures the new zone looks native and consistent with the board's existing zones.

**Dry-Run vs Write Behavior:**
- **`write=false`** (default) — Returns a full preview: the exact `(zone ...)` s-expr text block
  that WOULD be appended, the uuid that will be assigned, and the outline, without touching
  the board file.
- **`write=true`** — Appends the zone block via uuid-anchored top-level surgery (same targeted
  text edit as `create_kicad_group`), assigns a UUID, records the new zone's uuid in the
  board-local `autorouter_owned.zones` list — the ONLY thing that makes it eligible for a later
  `modify_kicad_plane` call. **IMPORTANT: The zone outline is written to the board, but KiCad
  does NOT compute its fill immediately.** You MUST refill zones (Fill All Zones) and re-run
  DRC in KiCad after `write=true` to see the copper.

**Ownership & Eligibility:**
Zones created by this tool are recorded in board-local `autorouter_owned.zones` and can only be
modified by `modify_kicad_plane` — never by hand in KiCad (though KiCad will not stop you). The
six hand-made kiln zones (mainGnd, safty_gnd, antenna, main3.3, main12v, 3.3v_safty) are never
in `autorouter_owned`, so they can only be *proposed* for change via `propose_kicad_plane` — a
human must review and apply changes to hand-made zones in KiCad.

**Args:** `project_path`, `net`, `layer` (optional; omit to auto-pick by net kind / layer
purpose), `name` (optional; defaults to `autorouter_<net>_<layer>`), `priority` (optional,
default 0), `write` (default false), `allow_while_open` (default false)

**Example output (excerpt):**
```json
{
  "board_path": "path/to/kiln.kicad_pcb",
  "write": false,
  "written": false,
  "net": "/Power/GND_Main",
  "layer": "In1.Cu",
  "name": "autorouter_/Power/GND_Main_In1.Cu",
  "uuid": "550e8400-e29b-41d4-a716-446655440000",
  "priority": 0,
  "outline": [
    {"x": 10.5, "y": 20.25},
    {"x": 95.75, "y": 20.25},
    {"x": 95.75, "y": 80.5},
    {"x": 10.5, "y": 80.5}
  ],
  "proposal": {...},
  "block": "(zone\n\t(net \"GND_Main\")\n\t(net_name \"/Power/GND_Main\")\n\t...\n\t(uuid \"550e8400-e29b-41d4-a716-446655440000\")\n)",
  "refill_required_note": "The zone outline is written but NOT filled - refill zones (Fill All Zones) and re-run DRC in KiCad after write=true."
}
```

## `modify_kicad_plane`

**Phase 7.5.5 — Resize/Reprioritize an Autorouter-Created Zone**

Move, grow, shrink, and/or reprioritize an existing zone by uuid, via uuid-anchored s-expr
surgery. Only the `(polygon (pts ...))` block and/or `(priority N)` line are spliced inside the
enclosing `(zone ...)` block — never a full board file re-serialize. Shares the same targeted
edit discipline as `delete_kicad_group` and `unroute_kicad_nets`.

**Ownership Restriction (Critical):**
This tool **REFUSES** (raises `ValueError`, never silently proceeds) if the zone uuid is not
recorded in board-local `autorouter_owned.zones`. This tool may ONLY move/resize a zone that
`create_kicad_plane` itself created. The six hand-made kiln zones (mainGnd, safty_gnd, antenna,
main3.3, main12v, 3.3v_safty) are never in `autorouter_owned`, so they can only be *proposed*
for change via `propose_kicad_plane` for a human to apply by hand in KiCad. This restriction
ensures human-authored zones are never auto-mutated.

**Parameters:**
- `new_outline` — A list of `{x, y}` dicts or `(x, y)` tuples with at least 3 points, replacing
  the zone's `(polygon (pts ...))` block. Omit if you're only changing priority.
- `priority` — An integer to replace or add the zone's `(priority N)` line. Omit if you're only
  changing the outline.
- **At least one of `new_outline`/`priority` must be given.**

**Dry-Run vs Write Behavior:**
- **`write=false`** (default) — Previews the new zone block text (with modified polygon and/or
  priority) without touching the board.
- **`write=true`** — Applies the changes to the board file and invalidates caches. **IMPORTANT:
  As with `create_kicad_plane`, KiCad must refill zones (Fill All Zones) and re-run DRC
  afterward** to reflect the modified outline in copper.

**Args:** `project_path`, `uuid` (required; must be in `autorouter_owned.zones`), `new_outline`
(optional), `priority` (optional), `write` (default false), `allow_while_open` (default false)

**Example output (excerpt — outline change):**
```json
{
  "board_path": "path/to/kiln.kicad_pcb",
  "write": false,
  "written": false,
  "uuid": "550e8400-e29b-41d4-a716-446655440000",
  "net": "/Power/GND_Main",
  "layers": ["In1.Cu"],
  "new_outline": [
    {"x": 12.0, "y": 22.0},
    {"x": 94.0, "y": 22.0},
    {"x": 94.0, "y": 78.5},
    {"x": 12.0, "y": 78.5}
  ],
  "new_priority": null,
  "block": "(zone\n\t(net \"GND_Main\")\n\t...\n\t(polygon\n\t\t(pts\n\t\t\t(xy 12.0 22.0)(xy 94.0 22.0)(xy 94.0 78.5)(xy 12.0 78.5)\n\t\t)\n\t)\n\t...\n)",
  "refill_required_note": "Zone outline/priority is changed on disk only if write=true - KiCad must refill zones (Fill All Zones) and re-run DRC before this is reflected in copper."
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

**Phase 7.7 — AI-in-the-loop decisions:**
When the top two candidate moves for an iteration score within
`optimizer.ai_decisions.min_score_spread` of each other — i.e. the cost model genuinely cannot
separate them — the call returns `state: "awaiting_decision"` instead of auto-accepting. The
report's `pending_decision` carries a closed list of 2–4 already-applied, already-scored options
(`id`, one-line `summary`, `score_delta`); answer with `decide_kicad_route(session_id, decision_id,
choice, rationale)` (an option id or the literal `"defer"`), or simply call `optimize_kicad_board`
again to defer to the best-scored option automatically. A clear winner (score spread at or above
`min_score_spread`) is still auto-taken exactly as before; `ai_decisions.enabled: false` disables
pausing entirely and `max_pauses_per_run` caps how many pauses one session can spend. Every
committed move — auto-accepted or AI-decided — is appended to a `decision_log` (visible via
`get_kicad_route_session`) for audit and replay. Per-option SVG previews are not built (nothing in
this codebase renders a board to SVG yet); the numbers and summary are what the decision is made on.

**Phase 7.15 — Effort presets & plateau-based stopping:**
`effort` (`"quick" | "balanced" | "best"`, new-session only) bundles the other optimizer knobs
into one choice — `quick` = `max_iterations: 5` + greedy; `balanced` (default) = today's
`optimizer.*` settings, unchanged; `best` = simulated annealing + an 8-hour ("overnight")
`time_budget_s`. An explicit `accept`/`max_iterations`/`time_budget_s` argument still wins over
whatever the preset would set. Alongside the existing `convergence_delta` floor (which stops a run
whose single latest move barely improved the score), the **plateau rule** also converges a run
whose *pace* of genuine improvement has slowed: once `optimizer.plateau_window` productive
(score-lowering) moves have landed, the trailing-window mean improvement rate is compared to the
reference rate (the first `plateau_window` such moves); falling below
`optimizer.plateau_slope_ratio` × reference converges the session with `stop_reason: "plateau"`
(vs. `"convergence_delta"` for the floor). Both rates are reported on every call via
`plateau_reference_rate`/`plateau_trailing_rate`, so "why did it stop" (or "how close is it") is
inspectable mid-run, not just at the end. `cpu.replicas` (the "replicas" language for quick/best in
the original design) is not wired to anything in this codebase yet, so no preset sets it.

**Phase 7.14 — Connector pin-swap advisor (off by default, `pin_swap.enabled: false`):**
A seventh "move" this tool can never apply on its own. It looks for two SIGNAL nets (power/ground
pins excluded) on different pins of one `detect_kicad_connectors` connector whose swap would score
better, prices each candidate as a controlled A/B on two disposable board+netlist copies (both
arms strip and reroute the same two nets; the swap arm additionally trades the two pads' nets via
trial-only s-expr surgery — never the real project), and when the gain clears `pin_swap.min_gain`
board-score points returns `state: "awaiting_decision"` with `decision_type: "pin_swap"`. **This
pause is MANDATORY**, unlike every other decision type — it is never gated by `ai_decisions`
(not `min_score_spread`, not `max_pauses_per_run`, not the `decision_types` allowlist), because the
tool cannot realize a pin swap itself; only a human editing the schematic and re-exporting the
netlist can. Answer via `decide_kicad_route`: `opt1` declines, `opt2` reports the change was made,
triggering a re-sync that *adopts* (never decides) the real board's new pad-net assignment onto the
session and reroutes only autorouter-owned copper on affected nets (hand copper on an affected net
is reported, not touched). Sub-`min_gain` swaps are recorded in `pin_swap_reports` but never
proposed. `pin_swap_exclusions` (new-session only) lists connector refs the advisor must never
touch — an unresolved ref name raises rather than being silently dropped. **The schematic and the
real `.net` file are never written by this tool on any path**, and `write=true` additionally
refuses outright if the session's board and the real board disagree about any pad's net, for any
reason.

**Args:** `project_path`, `session_id` (optional; omit to start a new session), `max_iterations_per_call`
(default 3), `max_seconds` (optional; omit for iterations-only bounding), `seed` (optional; overrides
`optimizer.seed`), `accept` (optional; "greedy" or "sa"; overrides `optimizer.accept`), `max_iterations`
(optional; total SESSION budget; defaults to `optimizer.max_iterations`), `time_budget_s` (optional;
total SESSION time budget; defaults to `optimizer.time_budget_s`), `effort` (optional; "quick",
"balanced", or "best"; new-session only), `pin_swap_exclusions` (optional array of connector refs;
new-session only), `write` (default false), `allow_while_open`
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
Returns the session's state (`running | converged | budget_exhausted | awaiting_decision`),
iteration count vs. budget, elapsed time, seed, acceptance policy, simulated-annealing temperature,
initial/current/best scores, the per-iteration score curve, the full move log (what move type was
tried, whether it was accepted, and why), the Phase 7.7 `decision_log` (every auto or AI-decided
move, for audit/replay), and the scratch board path.

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

## `decide_kicad_route`

**Phase 7.7 — Answer a Paused Optimizer Session**

Answers an `optimize_kicad_board` session that returned `state: "awaiting_decision"`. The pending
decision is a CLOSED list of 2–4 candidate moves the optimizer already applied and scored on
private board copies (`pending_decision.options`, each with an `id`, a one-line `summary`, and a
`score_delta`) — pick one by its `id` (e.g. `"opt2"`), or pass the literal `"defer"` to take the
optimizer's own best-scored default. You cannot introduce a move of your own; `rationale` is free
text that is recorded in `decision_log` and never executed.

A decision only fires where the cost model genuinely cannot separate the options (score spread
under `optimizer.ai_decisions.min_score_spread`) — exactly where judgment (EMI, serviceability,
"that jumper layer is for rework wires," future hand-rework) beats arithmetic; clear winners are
auto-taken without ever pausing.

**This call runs NO further iterations** — it only resolves the pause and returns the session to
`running` (or to `converged`, if the move it just committed was the one that stopped buying
`convergence_delta`). Call `optimize_kicad_board` with the same `session_id` afterward to continue.
Raises if the session is not `awaiting_decision` or if `decision_id` doesn't match the pending one
(a stale answer is refused). Never touches the real board — like the rest of the optimizer it works
on the session's private scratch copy until `optimize_kicad_board` is called with `write=true`.

**Phase 7.14 — answering a `pin_swap` decision:** a pending decision with `decision_type:
"pin_swap"` is answered here too, but it is advisory and commits nothing (no trial is ever
promoted). `opt1` declines the proposed connector pin swap; `opt2` reports that you made the
change in the schematic and re-exported the netlist, which triggers a re-sync — the session adopts
the real board's current pad-net assignment onto its scratch copy (never the reverse) and reports
what changed, plus any hand-routed copper on an affected net that needs redoing by hand, under
`resync` in the response. Answering `opt2` without having actually made the change is harmless: the
re-sync finds nothing to adopt and says so. This tool never writes the schematic or the real `.net`
file on this or any other path — relay the question to the human who can make the schematic edit,
don't answer it on their behalf.

**Args:** `project_path`, `session_id`, `decision_id` (must equal `pending_decision.decision_id`),
`choice` (an option id or `"defer"`), `rationale` (optional; recorded, never executed)

**Example output (excerpt):**
```json
{
  "command": "decide_route",
  "decision_id": "d9c4e1f5-i15",
  "choice": "opt2",
  "resolved_choice": "opt2",
  "rationale": "opt1 reroutes through the connector keep-out area we reserved for rework",
  "state": "running",
  "iteration": 15,
  "pending_decision": null
}
```

## `run_kicad_stitching_pass`

**Phase 7.5.6 — The Plane Stitching Pass**

Fill power/ground planes with stitching vias after routing and plane creation have converged. This
tool runs **LAST**, never mid-routing — a stitching via placed early becomes a congestion obstacle
for subsequent routing and defeats the purpose of the pass. The pass consists of three ordered steps,
each reusing existing read-only analysis tools rather than inventing new geometry:

1. **Island rescue** — Place one via per costed island/orphan that `audit_kicad_plane_islands`
   already reports, at its own `suggested_stitching_via.position`. This is the same target-selection
   logic the optimizer's move (d) uses, applied to EVERY island in a single sweep rather than one
   per iteration.

2. **Return-path stitching** — Place vias near high-speed/critical nets (from `classify_kicad_critical_nets`)
   on the same-layer power/ground PLANE (not the signal net's own layer). Vias are spaced
   `stitching.near_high_speed_pitch_mm` apart, placed within `stitching.near_high_speed_mm` of the
   routed trace, wherever a candidate point actually lands inside that plane's drawn outline.

3. **General stitching** — Grid-fill each power/ground plane's outline toward `stitching.target_spacing_mm`,
   skipping any grid point already covered by a step 1 or 2 via (deduplicating at the spacing).

Every via is placed via `_place_stitching_via(..., stitching=True)`, marking it as both `autorouter_owned`
(undoable via `unroute_kicad_nets`) AND tagged `"stitching": True` in the board-local record, so
`remove_kicad_stitching_vias` can target exactly these vias — never an ordinary routing via, never
a hand-placed via, and never the optimizer's own untagged move-(d) island-rescue via (which belongs
to an optimizer session, not this pass's bookkeeping).

**write=false** (default) previews the full plan (every via's net/zone/layer/position, and for island
rescue its projected cost change) without touching the board. **write=true** places every planned
via for real and additionally returns each one's uuid. After `write=true`, refill zones (Fill All
Zones) and re-run DRC in KiCad to reflect the vias in copper, the same as every other copper writer
here.

**SESSION CONVENTION (documented, not enforced):** Before routing or optimizing in an area that
already contains stitching vias (owned or foreign), the calling session should ask the user whether
to remove them first via `remove_kicad_stitching_vias` — removed stitching copper is simply
re-placed by the next run of this pass, so nothing is lost by asking. This is the same kind of
session-level contract as `route_kicad_nets`'s `allow_hand_copper_ripup` opt-in.

**Stitching Configuration** — Settings come from `pcb_settings.json` under the `stitching` block
(or defaults):
- `enabled` (default true) — Gate the entire pass; when false, this tool returns early and reports
  `enabled: false` with empty plans.
- `target_spacing_mm` (default 5.0) — Target grid spacing for general stitching.
- `near_high_speed_mm` (default 1.0) — Offset distance (both sides of trace) for return-path vias
  near critical nets.
- `near_high_speed_pitch_mm` (default 2.0) — Spacing between return-path vias along a critical-net
  trace.

**Args:** `project_path`, `write` (default false)

**Example output (excerpt):**
```json
{
  "board_path": "path/to/kiln.kicad_pcb",
  "write": false,
  "enabled": true,
  "planned_count": 47,
  "placed_count": 0,
  "island_rescue": [
    {
      "kind": "island_rescue",
      "net": "GND_Main",
      "zone": "mainGnd",
      "layer": "F.Cu",
      "x": 72.1,
      "y": 58.3,
      "current_cost": 13.3333,
      "projected_cost": 10.0
    }
  ],
  "return_path": [
    {
      "kind": "return_path",
      "net": "GND_Main",
      "zone": "mainGnd",
      "layer": "In1.Cu",
      "x": 55.25,
      "y": 62.5,
      "near_net": "/MainControler/CLK"
    }
  ],
  "general": [
    {
      "kind": "general",
      "net": "GND_Main",
      "zone": "mainGnd",
      "layer": "F.Cu",
      "x": 50.0,
      "y": 55.0
    }
  ]
}
```

When `write=true`, each entry in the `placed` array additionally carries its uuid.

## `remove_kicad_stitching_vias`

**Phase 7.5.6 — Undo for the Plane Stitching Pass**

Undo for `run_kicad_stitching_pass`. Deletes ONLY autorouter-owned vias tagged `"stitching": True`
in the board-local `autorouter_owned` records — never an ordinary routing via, never a hand-placed
via, and never the optimizer's own untagged move-(d) island-rescue stitching via (which belongs to
an optimizer session, not this pass's bookkeeping).

**Area Scoping** — `area` restricts deletion to a region; omit for the whole board. Two formats:
- **Rect:** `{"x_min": ..., "x_max": ..., "y_min": ..., "y_max": ...}` (any bound may be omitted
  for an open side)
- **Polygon:** `{"points": [[x0, y0], [x1, y1], ...]}`

**write=false** (default) previews the uuids that would be removed without touching the board.

**include_foreign=true** additionally LISTS (never deletes) every OTHER via in the resolved area
that this tool does not own, using this codebase's existing free/oversized via heuristic
(net=='' is an unconnected stitching/mounting via; more than 3× the Default netclass via diameter
is oversized). This is the same `get_kicad_track_inventory` characterization, applied here so a
real board's already-present freestanding vias can surface for a human to review one at a time.
**This is NOT a full connectivity trace** — it does not prove a same-net via has no track soldered
to it; it is a cheap first-pass heuristic only.

**SESSION CONVENTION (documented, not enforced):** Before routing or optimizing in an area containing
stitching vias (owned or foreign), the calling session should ask the user whether to remove them
first — the same kind of contract as `route_kicad_nets`'s `allow_hand_copper_ripup` opt-in. This
tool performs the deletion requested of it; the "ask first" step is the calling session's own
responsibility.

**Args:** `project_path`, `area` (optional; omit for whole board), `write` (default false),
`include_foreign` (default false), `allow_while_open` (default false)

**Example output (excerpt):**
```json
{
  "board_path": "path/to/kiln.kicad_pcb",
  "area": null,
  "write": false,
  "written": false,
  "candidates": 47,
  "removed": 0,
  "removed_uuids": [
    "12345678-abcd-1234-abcd-123456789abc",
    "87654321-dcba-4321-dcba-fedcba123456",
    ...
  ],
  "include_foreign": false,
  "foreign_vias": []
}
```

With `include_foreign=true`, the `foreign_vias` array carries vias this tool does not own:
```json
{
  "foreign_vias": [
    {
      "uuid": "foreign-via-uuid",
      "net": "",
      "x": 60.0,
      "y": 65.0,
      "size": 0.8,
      "drill": 0.4,
      "free": true,
      "oversized": false
    }
  ]
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

1. **Plane-aware routing is partial (Phase 7.5.4)** — Power/ground nets that own a filled zone route
   with on-plane cost discounts and termination relaxation; signal nets are unaffected. The A*
   heuristic is distance-only (pre-existing), so plane-aware routes are not cost-optimal — a plane
   route and a normal-cost route both reaching the goal may have different costs. Full plane-split
   routing and plane drop-via pathfinding are later phases.
2. **Rip-up uses integer-milli decision logic** — When a connection fails, the router rips only
   autorouter-owned copper in its path. Human-routed copper (`owner is None`) is never ripped by
   default; a separate opt-in flag `allow_hand_copper_ripup` can gate ripping of hand-routed tracks/arcs.
   Anti-thrash logic and deterministic canonical ordering ensure deterministic, repeatable results.
3. **Hierarchical fallback tier has no neck-down** — The Phase 5.x hierarchical last-resort routing
   tier (fallback when detailed A* exhausts all windows) routes without applying Phase 7.12 neck-down;
   this is rare in practice.
4. **Simplified pad escape** — Lands on the nearest free grid node, not a pad-direction-aware exact
   stub (a minor detail, but documented honestly).
5. **Termination on connection's `to` point** — Not "any same-net copper" and not a connection hub;
   exact per-connection routing (relaxed only for plane nets landing on their own fill), which is
   correct for tree-style nets.

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
