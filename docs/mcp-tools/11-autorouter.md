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
