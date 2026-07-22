# Group 10: Net Classes & Buses

[< Back to README.md](../../README.md)

Read-only inspection and structured planning for trace-width assignment and bus routing.
This group supports the PCB pre-routing workflow: detecting buses from schematic net-name patterns,
proposing net classes from measured routing data, and costing out copper routes to guide
optimization. Central to the workflow is `pcb_settings.json` - a shareable, committed JSON policy
file (next to `<project>.kicad_pro`) that holds trace-cost weights, bus-detection parameters,
and layer/plane/autorouter knobs. Every tool works out-of-the-box on pure defaults if this file
is absent; it's not required, only useful when teams want to standardize per-project constraints.

## `get_kicad_net_track_widths`

Per-net aggregate of routed copper trace widths and via sizes, measured directly from the PCB's
own segment/arc/via geometry (not netclass intent). Omit `net_name` to get every routed net
(sorted alphabetically); pass `net_name` for one net. `is_uniform` flags false when a net is
routed at more than one width. Segments/arcs with a width of 0 (meaning "inherit from netclass"
in KiCad) are counted in `segment_count`/`total_length_mm` but bucketed under the `"inherit"`
key in `widths`, and excluded from `dominant_width`/`min_width`/`max_width` (which describe only
explicit widths). `zero_width_segment_count` reports how much of that net has no explicit width.

**Args:** `project_path`, `net_name` (optional - omit for all nets)

**Example output (single net):**
```json
{
  "net": "/MainControler/CLK",
  "segment_count": 18,
  "total_length_mm": 90.264,
  "widths": {"0.3": 18},
  "layers": ["F.Cu"],
  "via_sizes": {},
  "dominant_width": "0.3",
  "min_width": "0.3",
  "max_width": "0.3",
  "is_uniform": true
}
```

## `detect_kicad_buses`

Read-only bus detection over the schematic netlist. Groups nets by shared hierarchical prefix
(e.g. `/MainControler/`); for flat/prefix-less nets, falls back to shared-connected-IC grouping.
Matches each group against built-in bus signal signatures (I2C, SPI, QSPI, I2S, UART, CAN, USB,
SWD, JTAG) by normalized net-name roles, and for every match with all required roles emits a
candidate bus with per-net width summary (from `get_kicad_net_track_widths`), common ICs, and a
suggested class name.

**Never writes or applies anything** - candidates only, for the caller to confirm with the user
before creating any net class. Also cross-checks netlist net names against the board's own pad
nets and reports mismatches in `stale_netlist_warnings` (the `.net` export can lag the board).

**Bus qualification:** A candidate is `qualified: true` when all its member nets' **most common
connected IC** shares a single, canonical IC reference (e.g., a single U<n> designator across
all I2C nets means all nets attach to the same I2C controller). Buses with multiple IC
candidates or no common IC reference are qualified `false` (medium/low confidence).

**Structural detectors** (beyond name-based signatures): DIFF_PAIR detection for nets ending in
`_P` / `_N` pairs within a group, and PARALLEL bus detection for numbered groups of ≥4
contiguous nets (e.g. A[0..15] address lines).

**Args:** `project_path`, `ic_ref_prefixes` (optional list of IC reference designator prefixes;
default `["U", "IC", "Q"]`)

**Example output (excerpt - first 3 candidates):**
```json
{
  "candidates": [
    {
      "bus_type": "I2C",
      "confidence": "high",
      "group_prefix": "/MainControler/",
      "nets": [
        {
          "net": "/MainControler/SDA",
          "role": "SDA",
          "width_summary": {"net": "/MainControler/SDA", "dominant_width": "0.3", ...}
        },
        {
          "net": "/MainControler/SCL",
          "role": "SCL",
          "width_summary": {...}
        }
      ],
      "common_ics": ["U1"],
      "qualified": true,
      "qualification_reason": "single IC reference across all member nets",
      "member_widths": {"0.3": 2},
      "suggested_class_name": "I2C_MainControler",
      "required_roles_matched": ["SCL", "SDA"],
      "required_roles_needed": 2
    },
    {
      "bus_type": "SPI",
      "confidence": "high",
      "group_prefix": "/MainControler/",
      "nets": [...],
      "qualified": true,
      "suggested_class_name": "SPI_MainControler",
      ...
    }
  ],
  "stale_netlist_warnings": []
}
```

## `get_kicad_track_inventory`

Board-wide inventory of every trace width and via size actually routed on the PCB (copper only),
plus the netclasses already defined in the project file - the "menu" of previously-used values
to offer a user interface instead of free-entry numbers. Also flags free/oversized via buckets
and reports `free_via_count` (vias marked as free vias in KiCad's via settings).

**Args:** `project_path`

**Example output:**
```json
{
  "track_widths": ["0.2", "0.25", "0.3", "0.4"],
  "via_sizes": [
    {"diameter": "0.8", "drill": "0.4"},
    {"diameter": "1.0", "drill": "0.5"}
  ],
  "existing_netclasses": [
    {
      "name": "Default",
      "track_width": 0.25,
      "via_diameter": 0.8,
      "via_drill": 0.4,
      "clearance": 0.2
    },
    {
      "name": "Power",
      "track_width": 0.5,
      ...
    }
  ],
  "free_via_count": 12,
  "oversized_via_warnings": []
}
```

## `get_kicad_pcb_settings`

Load `pcb_settings.json` from the project directory (next to `<name>.kicad_pro`) and deep-merge
it over in-code defaults. A missing file is **not an error** - every tool that reads settings
works on pure defaults out of the box. Returns the effective config plus `keys_from_file` /
`keys_from_defaults` so a caller can tell what's customized vs. stock. Raises if a weight in
`trace_cost` is negative or non-numeric.

**Args:** `project_path`

**Example output:**
```json
{
  "config": {
    "version": 1,
    "trace_cost": {
      "weights": {
        "length_mm": 1.0,
        "via": 5.0,
        "deviation_mm": 2.0,
        "excess_length": 10.0,
        "layer_span": 8.0
      },
      "via_weights": {"through": 1.0, "microvia": 0.5, "blind_buried": 1.5},
      "non_bus_deviation": 0.0
    },
    ...
  },
  "keys_from_file": [],
  "keys_from_defaults": ["version", "trace_cost", "corridor", "bus_detection", ...]
}
```

## `init_kicad_pcb_settings`

Write the fully-populated default `pcb_settings.json` into the project directory (next to
`<name>.kicad_pro`). Plain JSON, our own file, not KiCad's native project settings.
Defaults to `write: false` (dry run), returning the would-be file content without touching
disk. `write: true` refuses to clobber an existing file unless `overwrite: true`.

**Args:** `project_path`, `write` (default false), `overwrite` (default false)

## `get_kicad_trace_cost`

Score routed copper with the trace-cost model (see "Trace Cost Model" section below).
Computes length cost (copper length × `weights.length_mm`), via cost (via count ×
`weights.via` × via type multiplier), layer-span cost ((layers used - 1) ×
`weights.layer_span`), and deviation cost (for nets belonging to a qualified detected bus).

Nets on a bus bundle report `on_bus: true` plus a bundle object containing `bus_type`, `hub_ic`,
`destinations`, `role` (shared or dedicated), and `deviation` (metric used: mean_perp_distance,
max_perp_distance, or excess_length; value in mm; weight applied per metric). Shared nets roll up
across all bundles they serve. Non-bus nets report `on_bus: false`, `bundle: null`, and a deviation
cost equal to `trace_cost.non_bus_deviation` (default 0.0).

Omit `net` to get every routed net ranked worst-cost-first plus board totals and the
`weights_used` block actually applied (so a result is self-describing/reproducible even as
`pcb_settings.json` changes).

**Args:** `project_path`, `net` (optional - omit for all nets, ranked worst-first)

**Example output (single net):**
```json
{
  "net": "/MainControler/CLK",
  "on_bus": false,
  "bundle": null,
  "metrics": {
    "length_mm": 90.264,
    "via_count": 0,
    "via_types": {},
    "layers_used": 1
  },
  "cost": {
    "length": 90.264,
    "vias": 0.0,
    "deviation": 0.0,
    "layer_span": 0.0,
    "total": 90.264
  },
  "weights_used": {...}
}
```

## `propose_kicad_netclass`

Propose a net-class definition from a confirmed net list (e.g., a `detect_kicad_buses` candidate's
members, or hand-picked nets). Computes `track_width` as the length-weighted dominant width
across member nets, `via_diameter` / `via_drill` as the most-used via size on those nets (falls
back to the project's Default class), and inherits `clearance` from Default.

Reports conflicts when member nets differ in routed width, so the user chooses rather than the
tool silently averaging. Also returns the project-wide track/via inventory so a caller can offer
previously-used values as menu options.

**Args:** `project_path`, `nets` (exact net names array), `name` (proposed class name, e.g.
`"SPI_MainControler"`)

**Example output:**
```json
{
  "proposed_class": {
    "name": "SPI_MainControler",
    "track_width": 0.3,
    "via_diameter": 0.8,
    "via_drill": 0.4,
    "clearance": 0.2
  },
  "width_conflicts": [],
  "via_conflicts": [],
  "track_inventory": ["0.2", "0.25", "0.3", "0.4"],
  "via_inventory": [...]
}
```

## `create_kicad_netclass`

Create a KiCad net class by editing `<project>.kicad_pro` JSON: appends a class to
`net_settings.classes` (copying the Default class's full key shape, overriding
name/track_width/via_diameter/via_drill/clearance from settings), and adds one exact,
regex-escaped, anchored pattern (`^<net>$`) per net in `net_patterns` to
`net_settings.netclass_patterns`.

Refuses if the class name already exists. Defaults to `write: false` (dry run) returning a
before/after diff of the affected JSON blocks; `write: true` saves with KiCad's own
indent=2/sort_keys formatting.

**IMPORTANT:** KiCad only reloads net classes when the project is reopened, and creating a
class changes only the rules - it does not resize any already-routed copper.

**Args:** `project_path`, `name`, `settings` (object with optional
`track_width`/`via_diameter`/`via_drill`/`clearance` overrides), `net_patterns` (array of exact
net names to assign), `write` (default false), `allow_while_open` (default false)

## `audit_kicad_netclass_conformance`

For every routed net, resolve its assigned net class via `<project>.kicad_pro`'s
`netclass_patterns` (first regex match wins, same precedence as KiCad; unmatched nets fall back
to Default), then compare that class's `track_width` / `via_diameter` / `via_drill` against the
net's actual routed dominant values (`get_kicad_net_track_widths`).

Reports per-net mismatches, e.g. `"net is in class SPI (0.2 mm) but routed at 0.3 mm."` Read-only.

**Args:** `project_path`

**Example output:**
```json
{
  "total_nets": 154,
  "conformant_nets": 148,
  "mismatches": [
    {
      "net": "/Power/VBUS",
      "assigned_class": "Power",
      "assigned_track_width": 0.5,
      "routed_dominant_width": "0.3",
      "assigned_via_diameter": 1.0,
      "routed_dominant_via": "0.8/0.4",
      "issues": ["track_width mismatch", "via_diameter mismatch"]
    }
  ]
}
```

## `measure_kicad_bus_corridor_area`

Measures the routing-corridor area enclosed by a bus's traces, per destination IC. Input is either
a bus candidate object from `detect_kicad_buses`, or explicit nets + hub_ic. Output reports the
literal area between the bundle centerlines (stations resampled along the axis, perpendicular
spread × step) plus a convex-hull sanity bound, layers, bend ratio, and per-bundle segment counts.

**How corridor area is computed:** The hub (source IC) and destination IC pad centroids define an
axis. Dedicated nets (e.g. a chip-select signal) assign wholesale to their destination. Shared nets
(SCK/MOSI/MISO on SPI) are clipped per segment to the nearest destination axis, gated by projection
within the hub→destination span extended by a clip band (`clip_band_mult × dominant_trace_width`,
default clip_band_mult 3.0) or proximity to the bundle's dedicated copper. Unmatched fan-out
segments are counted in `unassigned_segment_count` and never silently dropped. Degenerate cases: a
single-destination bus → one bundle, no clipping; no hub or no on-board destinations → `grouped:false`
with just the union hull.

**Args:** `project_path`, `bus` (bus candidate from detect_kicad_buses, optional),
`nets` (explicit net names array), `hub_ic` (hub IC reference), `bus_type` (string, e.g. "SPI"),
`clip_band_mult`, `station_step` (resampling distance, mm), `ic_ref_prefixes`

**Example output (multi-destination SPI bus):**
```json
{
  "bus_type": "SPI",
  "hub_ic": "U4",
  "grouped": true,
  "bundles": [
    {
      "destination_ic": "U7",
      "role": "primary_slave",
      "corridor_area_mm2": 137.0,
      "hull_area_mm2": 485.2,
      "bend_ratio": 1.08,
      "mean_spacing_mm": 0.45,
      "layers": ["F.Cu"],
      "axis_length_mm": 125.3,
      "segment_count": 45,
      "unassigned_segment_count": 0
    },
    {
      "destination_ic": "U8",
      "role": "secondary_slave",
      "corridor_area_mm2": 157.4,
      "hull_area_mm2": 612.1,
      ...
    }
  ],
  "sum_of_bundle_areas_mm2": 313.1,
  "union_hull_area_mm2": 1706.6,
  "clip_band_mm": 0.9,
  "warnings": []
}
```

**Real kiln examples:**
- **I2C /MainControler/** (hub U4 → U5, single destination): corridor 152.1 mm², hull 485.2 mm²
- **SPI /MainControler/** (hub U4 → U7/U8/U9 multi-drop): 7 unassigned fan-out segments; bundles 137.0 / 157.4 / 18.7 mm²; sum_of_bundle_areas 313.1 vs union_hull 1706.6 mm²
- **SPI /SaftyProcessor/**: `grouped:false` (slaves off-board); union hull only

## `audit_kicad_capacitor_net_voltages`

Audits capacitor voltage ratings against the actual voltages they sit across. Reads net names as
voltage labels (e.g., `12V_Main`, `3.3v_Safty`, `+5V`, `3V3`, `1V8`) and cross-checks each
capacitor's rating (from the schematic Value field or explicit `net_voltages` override in
pcb_settings.json) against the applied voltage `|V(net_a) − V(net_b)|`.

**Net voltage inference precedence** (each reported with source: override | gnd | label | none):
1. Explicit `net_voltages` override from pcb_settings.json
2. GND token — any net containing `gnd_tokens` (case-insensitive, default: ["gnd", "ground", "vss"]) → 0 V
3. Labeled voltage in the net name — `12V_Main` → 12.0 V, `3.3v_Safty` → 3.3 V, `+5V` → 5.0 V, `3V3` / `1V8` convention
4. If multiple voltage tokens found, use the largest + flag `ambiguous_label`
5. GND beats label: `GND_5V_RTN` → 0 V, flagged ambiguous_label

**Verdicts, sorted worst-first:**
- `under_rated` — capacitor's rated voltage < applied voltage (critical failure)
- `unknown_rating` — both nets labeled, applied voltage resolved, but rating missing from schematic or pcb_settings.json (the nets worth chasing)
- `under_derated` — rated voltage < `derating_min_ratio × applied_voltage` (default ratio 2.0), e.g. a 10 V cap across 5 V with 2.0× ratio is under-derated
- `ok` — rating meets derating minimum
- `one_net_unlabeled` — one side resolved, the other assumed 0 V (reported with `assumed_applied_v`)
- `no_labeled_nets` — skipped from scoring (both nets unrecognized)

DNP caps excluded. Output rows carry reference, value, rated_v, rated_v_source, both nets with
inferred voltage + source, applied_v, required_min, and verdict; plus summary counts, settings used,
and stale_netlist_warnings.

**Args:** `project_path`

**Example output (kiln project, 68 caps):**
```json
{
  "audit_summary": {
    "total_caps": 68,
    "verdicts": {
      "under_rated": 0,
      "unknown_rating": 24,
      "under_derated": 0,
      "ok": 3,
      "one_net_unlabeled": 31,
      "no_labeled_nets": 10
    }
  },
  "capacitors": [
    {
      "reference": "C9",
      "value": "470uf",
      "rated_v": null,
      "rated_v_source": "none",
      "net_a": "12V_Main",
      "net_a_voltage": 12.0,
      "net_a_voltage_source": "label",
      "net_b": "GND_Main",
      "net_b_voltage": 0.0,
      "net_b_voltage_source": "gnd",
      "applied_v": 12.0,
      "required_min_v": 24.0,
      "verdict": "unknown_rating"
    }
  ],
  "settings_used": {
    "derating_min_ratio": 2.0,
    "gnd_tokens": ["gnd", "ground", "vss"],
    "net_voltages": {},
    "default_cap_rating": null
  },
  "stale_netlist_warnings": []
}
```

---

## How Bus Detection & IC Qualification Works

Bus detection is a two-phase process:

**Phase 1: Signature Matching**
1. Group nets by hierarchical prefix (e.g. `/MainControler/`), or by shared connected IC for flat nets.
2. Within each group, match nets against built-in bus signatures by normalized role names:
   - **I2C:** SDA + SCL (e.g., any net named `SDA`, `scl`, `/sheet/I2C_SDA` → role "SDA")
   - **SPI:** MOSI + MISO + CLK (optional CS)
   - **QSPI:** CLK + IO0–IO3 + CS
   - **I2S:** WS + BCLK + SD (optional MCLK)
   - **UART:** TX + RX (optional RTS/CTS/DTR)
   - **CAN:** CANH + CANL
   - **USB:** DP + DM (optional VBUS/ID)
   - **SWD:** SWDIO + SWCLK (optional NRST)
   - **JTAG:** TCK + TMS + TDI + TDO (optional NTRST)
3. Optional roles (like SPI's CS) are attached if present but don't gate detection.
4. Structural detectors fire after named signatures: DIFF_PAIR (nets ending in `_P`/`_N`), and
   PARALLEL buses (numbered groups ≥4 contiguous, like A0–A15).

**Phase 2: IC Qualification**
A candidate is `qualified: true` when the **most common IC connected to all member nets** is a
single, canonical reference. For example:
- An I2C bus where all 2 nets attach to the same U1 (MCU) → qualified
- An SPI bus where MOSI/MISO/CLK attach to U1, but CS attaches to U2 → falls back to "all-but-one
  fan-out tolerance" (qualified only if the minority IC is ignored)
- No common IC or multiple disparate ICs → qualified false (low confidence candidate)

Qualification guides user confidence: high-confidence buses (qualified + all required roles
present) are automatically suggested for net class creation; low-confidence matches (e.g.
RS485's generic A/B naming) are suppressed unless qualified.

---

## Trace Cost Model & pcb_settings.json

The trace-cost model scores every routed net on four axes: **length** (total copper length in mm),
**vias** (via count × via weight), **layer span** (how many layers a net uses), and **deviation**
(current stubbed until bus-corridor measurement lands).

### Schema (DEFAULT_PCB_SETTINGS)

```json
{
  "version": 1,
  "trace_cost": {
    "weights": {
      "length_mm": 1.0,
      "via": 5.0,
      "deviation_mm": 2.0,
      "excess_length": 10.0,
      "layer_span": 8.0
    },
    "via_weights": {
      "through": 1.0,
      "microvia": 0.5,
      "blind_buried": 1.5
    },
    "non_bus_deviation": 0.0
  },
  "corridor": { "clip_band_mult": 3.0 },
  "bus_detection": {
    "ic_ref_prefixes": ["U", "IC"],
    "extra_signatures": {}
  },
  ...
}
```

### Units and Interpretation

- **length_mm weight** (default 1.0): cost per mm of routed copper. Penalizes unnecessarily long
  traces (e.g. backtracking, poor fanout placement).
- **via weight** (default 5.0): multiplier per via instance. Each via is charged this base weight
  × its `via_weights` type multiplier (through=1.0, microvia=0.5, blind_buried=1.5).
- **layer_span weight** (default 8.0): cost × (layers_used - 1). Penalizes multi-layer routes
  more than single-layer.
- **deviation_mm weight** (default 2.0): cost per mm of mean or max perpendicular distance from
  the bundle centerline. Applies to nets belonging to a qualified detected bus. Non-bus nets skip
  this term.
- **excess_length weight** (default 10.0): cost per mm of length overrun relative to the
  hub→destination straight-line distance (shared nets clipped per destination segment).
- **non_bus_deviation** (default 0.0): fallback cost for nets not on a detected bus. Set to 0.0 to
  ignore deviation entirely for non-bus nets, or increase if you want to penalize stray traces.

### Worked Example

For `/MainControler/CLK` (actual kiln project data):
- Routed length: 90.264 mm
- Via count: 0
- Layers used: 1 (F.Cu only)
- Bus: SPI, on_bus=true, role=shared, deviation metric=mean_perp_distance, value=0.25 mm

**Cost calculation:**
```
length_cost = 1.0 × 90.264 = 90.264
via_cost = 5.0 × 0 × 1.0 = 0.0
layer_span_cost = 8.0 × (1 - 1) = 0.0
deviation_cost = 2.0 × 0.25 = 0.5 (bus bundle deviation)
total = 90.764
```

**Board-wide impact (kiln project):**
- **Without deviation term:** total 5584.4
- **With deviation term (live):** total 5628.8 (+44.41, primarily SPI /MainControler/ nets on shared bundles)
- **Exception:** SPI /MainControler/ CS3 net reaches only the hub (no destination), correctly sits on no bundle

### Sharing & Committing pcb_settings.json

`pcb_settings.json` is a shareable policy file, intentionally kept next to `<project>.kicad_pro`
for easy version control and team distribution. By committing it to your repo, you enforce
consistent trace-width/via/clearance standards across all team members' layouts. Missing file
= pure defaults work fine; it's only needed if you want to override.

### Real Measured Baseline (kiln project)

- **Routed nets:** 154 nets with copper
- **Segments:** 1,609 trace segments/arcs
- **Vias:** 298 total (295 net-attached, 3 free/empty-net)
- **Dominant width:** 0.3 mm (most common trace width)
- **Bus candidates:** 3 qualified buses detected (I2C_MainControler, SPI_MainControler, SPI_SaftyProcessor)
- **Bus corridor areas:** I2C single-dest 152.1 mm²; SPI multi-drop 313.1 sum vs 1706.6 union hull; SaftyProcessor grouped=false (off-board slaves)
- **Trace cost before/after deviation term:** 5584.4 → 5628.8 (+44.41)
- **Capacitor audit:** 68 caps — 0 under_rated, 24 unknown_rating (missing Value/rating in schematic), 0 under_derated, 3 ok, 31 one_net_unlabeled, 10 no_labeled_nets

---

## References

- **mykicadMcp/NETCLASS_PLAN.md** — Design document and implementation roadmap for this feature set
  (Phases 1–6.3).
