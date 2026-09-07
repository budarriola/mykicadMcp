# Net Class & Bus Detection — Implementation Plan

Feature set for the KiCad MCP server: measure per-net trace widths from the routed
PCB, detect buses (SPI/I2C/I2S/…) by net name and qualify them against shared ICs,
and create KiCad net classes from measured/confirmed settings — with the user
verifying every bus and choosing widths/via sizes from values already used in the
project.

**Module layout** — all of it lives in **`kicad_pcb_tool.py`** (parsers,
inventory, bus detection, net classes, cost model, audits: parser/audit-shaped
code that reuses its `SexprParser`, caches, and write discipline in place).
Also owns all reference-based copy/template functionality (layout/route/flip/
property-position templates, groups, sibling-instance listing) used for
propagating a hand-finished instance's placement/routing/flip state onto its
siblings.

Everything is exposed through `kicad_mcp_server.py` following the existing
`self.tools[name] = {description, inputSchema, handler}` + `_tool_*` wrapper
pattern.

**History:** an in-repo Python autorouter (grid A* with rip-up, plane-aware
routing, GPU/numpy acceleration, benchmark harness, warm-start/adoption,
impedance-matched routing — everything this file used to call "Phase 7" and
"7.x") was built out, then removed. `kicad_router_tool.py`,
`kicad_router_accel.py`, `kicad_route_viewer.py`, `kicad_optimizer_tool.py`,
`docs/mcp-tools/11-autorouter.md`, and every MCP tool they backed
(`route_kicad_board`, `route_kicad_nets`, `unroute_kicad_nets`,
`optimize_kicad_board`, `decide_kicad_route`, `get_kicad_route_session`,
`benchmark_kicad_autoroute`, `open_kicad_route_viewer`,
`get_kicad_system_resources`, `adopt_kicad_routing`,
`seed_kicad_routing_from_board`, `list_kicad_zones`,
`audit_kicad_plane_islands`, `create_kicad_plane`/`modify_kicad_plane`/
`propose_kicad_plane`, `audit_kicad_crosstalk`, `run_kicad_stitching_pass`/
`remove_kicad_stitching_vias`, and the rest) were deleted in `mykicadMcp`
commit `403fad7` ("remove autorouter engine, keep reference-copy/template
tools"). What remains from that era is the reference-copy family —
`copy_kicad_component_routing`, `apply_kicad_route_template`,
`diff_kicad_route_template` — which replicates routing already drawn on one
instance of a repeated block onto its siblings, not an autorouter. There is
no in-repo router today; route by hand in KiCad. The net-class/bus tooling
below is unaffected by the removal and remains fully supported.

---

## Status snapshot — read this first

Net class & bus detection (this file's actual subject) is landed and stable:
detection, corridor-area measurement, trace-cost scoring, netclass proposal/
creation, and conformance auditing all exist as real tools (see `kicad_facade.py`'s
"netclass" group). Nothing in that area is currently pending. What follows is
kept as background/reference (section 0, the working agreement, the netclass
flow, and correctness notes) — verify any claim against the code
(`kicad_pcb_tool.py`, `kicad_mcp_server.py`, `tests/`) before trusting this
snapshot if they disagree, and fix this file when they do.

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

## Interaction flow (how a session uses these)

**Net classes from the routed board:**
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
  board. Everything that leans on it (bus detection, cap audit, corridor
  roles) must first cross-check net names against the board's own copper/pad
  nets and **warn with the mismatch list** when they disagree — a stale netlist
  silently mis-qualifying a bus is worse than a refused run.
- **`island_removal_mode` matters**: kiln's zones use mode 0 (islands kept). There
  is no in-repo tool that costs/stitches islands today (that lived in the removed
  autorouter); this is noted here only because it affects how zone-related net
  data should be interpreted if that capability is ever rebuilt.
- **KiCad format tolerance**: this repo has v9-era files edited under KiCad 10;
  parsers must skip unknown s-expr tokens instead of failing, and every writer
  emits only constructs already present in the target file (copy-the-native-shape
  rule). Known hardening gap: older boards reference nets by numeric index
  (`(net 1 "name")`) where kiln uses name-only (`(net "name")`); `_parse_tracks`
  reads `entry[1]` verbatim and would misread the index form as the net name —
  harden if the tools ever target non-kiln boards.
- **Coordinate formatting on emit**: new segments/vias/zone points use the same
  number formatting as `apply_layout_changes` (`_format_at_number`, ≤6 decimals,
  no trailing zeros) so diffs stay minimal and KiCad re-saves don't rewrite them.

---

## Build order (historical)

**M0 — Test infrastructure — DONE.** `tests/`: conftest fixtures
(`kiln_project_path`, `scratch_board`), golden parser tests, writer round-trip
harness, synthetic generator (N-layer stacks, net table, dense fanout-field
mode), `write_synthetic_project` (board + companion `.kicad_pro`/`.net`), and
`kicad-cli pcb drc` acceptance tests (auto-skip if kicad-cli absent).
**Parallel by default:** `pytest.ini` sets `addopts = -n auto` (pytest-xdist);
the suite is parallel-safe (per-test tmp/scratch dirs, per-worker-process
parse caches).

**M1 — Net classes end-to-end — DONE.** Detection, proposal, creation, and
conformance auditing landed; docs at `docs/mcp-tools/10-netclasses-and-buses.md`.

**M2 — Analysis suite — DONE.** Cost model, corridor-area measurement, and
critical-net/connector classification landed; `docs/mcp-tools/
10-netclasses-and-buses.md` covers the full netclass/bus group.
