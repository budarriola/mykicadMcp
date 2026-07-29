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

## Status snapshot — read this first (updated 2026-07-29)

For whoever (human or AI) picks this up next: this snapshot used to carry a
full session-by-session history (2026-07-21 through 2026-07-28). It has been
trimmed (2026-07-29, user request) now that every item in that history has
its own dedicated "LANDED" anchor section elsewhere in this file — go read
the phase's own anchor for what shipped, tool/test counts at that landing,
and residuals; don't rely on this snapshot for that detail.

- **Landed & coordinator-verified**: Phases 1–9 and 7.1 through 7.20 in
  full — every one has a "### 7.x — LANDED" (or "## Phase N — LANDED")
  anchor in place below with its full write-up. Current state: 94 MCP tools
  registered, 476-test suite green (7 pre-existing failures are real-board
  drift from the user's own continued hand-routing — see the ⭐ findings
  section below for detail — not caused by any landed feature).
  Plus a large-board-with-a-handful-of-unrouted-connections test fixture
  (`generate_large_board_few_unrouted`, `tests/test_large_board_few_
  unrouted.py`, 2026-07-29 user request) — see its own module docstring, not
  a numbered phase.
- The ⭐ **Real-board routing findings & the hand-routed baseline** section
  right after this one is kept in full (not trimmed) — it's the acceptance
  narrative for the routing-capability arc (7.3b through M7) and several
  root-cause diagnoses future work still depends on, not a redundant log.
- **Next work when resumed (updated 2026-07-29 — M7 fully landed: 7.18,
  7.19, 7.20 all done; Phase 7.21 via placement safety also LANDED same day,
  see their anchors).** User priority set 2026-07-29, in order: **(1) Phase
  7.22 bus-first direct routing pass — spec'd, NOT yet started, do this
  next** (see its section right after the 7.21 anchor — queued behind 7.21 to
  avoid two concurrent worktree agents editing the same hot `route_nets`
  functions; that's no longer a concern now that 7.21 is merged); (2) Phase
  7.6 whole-board optimization; (3) M6 item 17(c) Flow B stack-up-gate
  question. Phase 7.13 (impedance-matched traces) remains explicitly
  deprioritized by the user — do not start it without being asked. Still
  open, low priority: the small 7.3b "any same-net copper" termination bit.
- Verify claims against the code (`kicad_pcb_tool.py`, `kicad_mcp_server.py`,
  `tests/`) rather than trusting this snapshot if they disagree — and then fix
  this file.

## ⭐ Real-board routing findings & the hand-routed baseline (2026-07-24)

> **⭐⭐ ROOT-CAUSE DATA for `unreachable_in_window` (measured 2026-07-24, by
> tapping `_fine_search` on the real board — this SUPERSEDES the earlier guess
> that "a finer grid cannot create a corridor that does not exist").** Two
> distinct, quantified causes — NOT a single window-size limit:
>
> 1. **Sealed pad escape at the 0.2 mm grid (FIXED, partially).** Many short
>    connections (MOSI 0.19 mm airline, IC2-FB 0.69 mm, several 3.3V ~2 mm) failed
>    because at 0.2 mm NO grid node lands in the sub-0.2 mm channel between
>    clearance-inflated neighbours — measured: IC2-FB start had **1** reachable
>    node, 3.3v_Safty **3**. Hand routing threads these off-grid. **Fix landed:
>    the `_route_attempts` grid-refinement ladder** (finer grids down to
>    `min_grid_mm`=0.05 at a tight window, on failure only, attempt-1 preserved so
>    all passing routes stay byte-identical). Proven: MOSI now routes at 0.05 mm
>    (full board 4→5). Widening the margin — the OLD retry — never helped these
>    (the block is LOCAL to the pad, not a lack of room).
>
> 2. **⭐ THE BIG LEVER — vias can't cross the inner/bottom PLANES (FIXED
>    2026-07-24).** Measured for CLK at 0.05 mm: F.Cu is 8825/18904 nodes free, but
>    **In1.Cu & In2.Cu are ~96% solid copper and B.Cu ~97%** (GND/power plane
>    pours), and only **104/18904 nodes were via-able** — the obstacle model
>    treated a plane fill as solid copper blocking the via everywhere, so every
>    cross-layer signal net (CLK, CS0/1/2, MISO, SCL, SDA, DataToSafty/FromSafty,
>    Fault) that must go F.Cu→B.Cu was `unreachable`. In reality a signal via
>    punches an **anti-pad** through a GND/power plane (how the hand board crosses
>    layers). **Fix landed: the plane-via anti-pad model** — a foreign power/gnd
>    ZONE fill is tagged `via_transparent` (`_collect_obstacles`), so `_Obst` /
>    `_FineWindow.obstacle_cells` skip VIA-blocking for it (still block same-layer
>    tracks) and `_self_check` skips via-vs-plane clearance. Unit-tested in
>    `tests/test_plane_via.py` (5 tests). **Writes must refill:** the
>    `refill_zones_with_kicad(board_path)` helper (`kicad-cli pcb drc
>    --refill-zones --save-board`, auto-skips without cli) is wired to
>    `route_nets(..., refill_zones=True)` — opt-in so byte-exact/no-via tests stay
>    untouched, but REQUIRED for a DRC-clean written board so KiCad cuts the real
>    anti-pad around each plane-crossing via.
>
>    **COMBINED RESULT (finer grid + plane-via): full-board completion 4 → 10
>    routed** (preview, `route_nets` on kiln, 2026-07-24). CLK/MOSI now route at
>    the coarse 0.2 mm grid (plane-via alone), 3.3v_Safty at 0.1 mm (needs both).
>    Remaining: 6 `self_check_failed` (plane-via route found but skims real copper
>    at every grid — needs rip-up demotion, a known TODO, NOT a finer grid: that
>    was measured to not help and only add latency), + long inter-module nets
>    (item 3) + a few genuinely-sealed pads (SDA, IC2-FB, U6-BIAS: pad ringed by
>    hand copper, ≤3 reachable nodes — need rip-up of hand copper, which we never
>    do).
>
> 3. **Long inter-module nets (40–113 mm: DataToSafty, Fault, thermoFault, estop,
>    saftyRelay, 5V/12V spans) remain `unreachable`** at the coarse grid their huge
>    windows force — the genuine large-window case, deferred to hierarchical
>    windowing / multilevel global routing (7.8 GPU/M5 batched fields), NOT the
>    per-window numpy wavefront (which is slower there, see the 7.8 anchor).

> **ENABLER found 2026-07-24 (user suggestion, PROVEN): KiCad can fill zones and
> run DRC headlessly — use it as the authority instead of estimating.**
> `kicad-cli pcb drc --refill-zones --save-board <board>` (KiCad 10.0.4 on this
> box) recomputes ALL zone fills and saves the board, no GUI/IPC session — tested
> on a scratch kiln copy (fills rewritten, board saved). KiCad's DRC also
> reported **"Found 39 unconnected items" = our `get_ratsnest` 39** (independent
> validation of our connectivity model). KiCad's bundled Python
> (`.../KiCad/10.0/bin/python.exe`) also has `pcbnew` 10.0.4 with `ZONE_FILLER`
> for programmatic fills. **How this helps (wire it in — bounded Sonnet task,
> AFTER the 7.8 agent frees `kicad_router_tool.py`):**
> - Add a `refill_zones_with_kicad(board_path)` helper (invoke the CLI; auto-skip
>   if kicad-cli absent) so the plane-aware router gets AUTHORITATIVE fills after
>   placing plane vias — directly fixes the plane-stub / `len=0` defects (the
>   router currently reasons over estimated/stale fills). Supersedes much of the
>   §7.5.2 "estimated fill" fallback for the real-board path.
> - Use KiCad's "unconnected items" count as an authoritative completion metric in
>   the benchmark (cross-check vs `get_ratsnest`).
> - The §7.11 DRC gate should run WITH `--refill-zones` so plane-through-fill
>   connections are checked on real fills, not stale ones.
>
> **More kicad-cli capabilities worth wiring in (KiCad 10.0.4, all verified
> headless 2026-07-24):**
> - **`sch export netlist` regenerates the netlist FROM the schematic** (verified
>   on `kiln.kicad_sch`) — this can RETIRE the netlist-staleness guards that
>   `detect_buses` (3c), `audit_capacitor_net_voltages` (8), corridor roles (5),
>   and `classify_critical_nets` (9) all carry: instead of cross-checking a
>   possibly-stale `.net` against board pads and warning, just regenerate the
>   fresh netlist on demand. Highest-leverage after refill (touches the most
>   tools). Formats: kicadsexpr (default), kicadxml, spice, ….
> - **`pcb export ipcd356`** — the board's real net→pad connectivity as a standard
>   IPC-D-356 netlist (verified): an INDEPENDENT connectivity oracle to validate
>   `build_connectivity`/`get_ratsnest` against (already corroborated: KiCad DRC's
>   "39 unconnected" == our ratsnest 39).
> - **`pcb export svg`** (per-layer) — route/board visualization for `write=False`
>   previews and an alternative/complement to the 7.9 tkinter viewer.
> - **`sch erc`** — authoritative Electrical Rules Check; complements the Phase 8
>   schematic audits. **`pcb export stats`/`pos`** — quick board metrics /
>   component placement.
> These are bounded Sonnet tasks (each an MCP tool + `kicad-cli` shell-out with
> auto-skip when absent), queued behind the in-flight 7.8 work to avoid
> `kicad_router_tool.py` edit collisions.

> **REQUIRED CONSTRAINT (user, 2026-07-24) — LANDED 2026-07-24: filled zones are
> used AS ROUTABLE PLANES only for POWER/GROUND nets** (3.3V, 5V, 3V3, GND, 12V,
> VCC, VDD, and the like — `_net_kind(net)=="power"`), never for signal nets. The
> gate is one early return in `_plane_components_for` (kicad_router_tool.py):
> `if _pcb._net_kind(net, None, power_patterns) != "power": return None`, so a
> signal net that owns a fill gets NO plane moves and routes as ordinary copper.
> Tested by `test_signal_net_fill_is_not_used_as_plane` (signal fill → emitted
> length ≈ full airline, the inverse of the GND plane case) plus the GND-renamed
> full-pipeline plane tests; parity/white-box tests pass plane args explicitly so
> are unaffected. 196-test suite green.
>
> **Deliberate divergence from the originally-planned location (do NOT "fix" this
> back):** the gate was placed at the PLANE-ROUTER consumer (`_plane_components_for`),
> NOT at `_zone_fill_index_cached` as first sketched. Gating the shared fill index
> itself would ALSO strip signal-net fills from OBSTACLE collection — and a signal
> net's copper pour must still block other nets (else the router threads copper
> straight through it → DRC violations). A signal fill must remain an obstacle
> while never being a routable plane FOR ITSELF; only the plane-router consumer
> distinguishes those, so that is where the gate belongs. Bonus: connectivity/
> ratsnest is untouched, so `get_ratsnest`=39 is preserved by construction (kiln's
> five net-owning zones — GND_Main, GND_Safty, 12V_Main, 3.3V_Main, 3.3v_Safty —
> are all power nets anyway). If a genuinely-power net's name doesn't match on some
> board, extend `power_net_patterns`, don't loosen the gate.


> **User decision 2026-07-24:** the no-Opus-subagent rule is **lifted for the
> deep routing-capability / whole-board-optimizer work** (the `unreachable_in_window`
> pathfinding gap and Phase 7.6) — that is genuinely Opus-class and won't fall
> out of Sonnet patches. Bounded quality fixes still go to Sonnet.


First `route_board(write=True)` run on the REAL kiln board. The user **reverted
it** — the output was not usable. What we learned (this is now the top driver of
router work):

- **THE GOAL (user-stated):** the autorouter must do **as well or better than the
  user's hand-routed board**, judged by the Phase 6 board score, *ignoring the
  nets they have not routed yet.* **Baseline to beat — hand-routed kiln
  `get_trace_cost` board total = 8552.28** (length 5851.9, vias 1475.0,
  deviation 44.4, layer_span 568.0, layer_penalty 612.9; 39 connections still
  unrouted by hand). The scoring tool already exists (`get_kicad_trace_cost`) —
  no new tool needed to measure the baseline.
- **TIMELINE EXPECTATION (user, 2026-07-24): do NOT expect routing to match hand
  quality until everything else is done.** `matched_or_beat_human` is the
  END-STATE finish line after the full pipeline lands (7.8 acceleration → the
  7.6 whole-board optimizer → geometry/plane-via cleanup → …), NOT a per-step
  gate. Judge each intermediate piece on its own incremental merit — completion
  %, parity, DRC-clean, speed — and let the score converge toward 8552.28 as the
  pipeline completes. Don't over-optimize any single step to beat the baseline
  prematurely.
- **STANDING DIRECTIVE (user, 2026-07-24): use multicore/multiprocessing for
  heavy tasks WHENEVER POSSIBLE.** BFS/search is slow serially; this box is
  24-thread + 111 GB RAM ≫ the 2 GB GPU, so CPU parallelism is the dominant
  lever. The ~39 board connections are largely independent → route them in
  parallel across cores (a BFS *within* one window is sequential; many run
  concurrently). Also parallelizable: rasterization tiles, clearance
  self-checks, fill/island labeling. Use stdlib `multiprocessing`; workers only
  COMPUTE, all state commits happen in the parent in canonical order so results
  are bit-identical for any worker count (workers=1 vs N is a parity test). This
  is the 7.8 "Multi-core CPU" piece and has been folded into the in-flight 7.8
  agent's scope (alongside numpy); `autorouter.cpu.workers` (0=auto=cores−1).
  Apply the same parallel-where-independent instinct to any future heavy tool.
  **Do NOT measure/confirm speedups (user, 2026-07-24):** apply perf
  optimizations on engineering judgment; skip before/after timing runs and the
  ~10-min speed benchmarks. This does NOT relax correctness — parity
  (cpu-vs-numpy, workers=1-vs-N), full suite green, and determinism are
  CORRECTNESS gates and stay. Just stop spending cycles proving things are
  faster.
- **Result was 3 of 39 routed, and even those 3 were bad.** Three concrete
  defects, in priority order:
  1. **`window_too_large` fails 35/39.** The pure-Python detailed A* window cap
     (`_MAX_WINDOW_SPAN_MM=60` / `_MAX_WINDOW_NODES=400k` at 0.2 mm grid) can't
     reach any connection spanning more than ~55 mm — i.e. every real long haul
     (SPI bus, power rails). **This is the #1 blocker: without it the router is
     useless on a real board.** Fix path: adaptive/coarser detailed grid for
     long connections (fine only near pads), and/or the M5 numpy/accel tier.
     Not a naive cap bump — that blows pure-Python runtime.
  2. **Emitted geometry is degenerate.** `/SaftyProcessor/Current3` (1.5 mm
     straight airline) came out as a **5-segment grid-snapped squiggle**. Needs
     exact pad-anchor termination + collinear/45° simplification, and probably a
     larger grid with exact stubs rather than 0.2 mm gridding of the whole path.
     (Ties to the open 7.3b residuals: "pad escape lands on nearest free node,
     not direction-aware"; "termination is on the `to` point.")
  3. **Plane nets routed as surface stubs with 0 vias.** `3.3V_Main`/`GND_Main`
     are In2.Cu-plane nets — their pads should **drop a via into the plane**, but
     the router laid a pointless tiny surface trace. 7.5.4's plane-via path is
     not being *preferred* (the documented non-admissible-heuristic gap) — plane
     nets must favor via-to-plane over a surface hop.
- **Measurement loop — DONE:** Phase 7.16 `benchmark_kicad_autoroute` LANDED
  2026-07-24 (see its anchor). It CONFIRMED the numbers on kiln: human 8552.276
  vs auto 8568.267 (**worse by +15.99, 3/39 routed, 1 new DRC violation** = the
  plane-stub defect). `comparison.matched_or_beat_human` is the acceptance gate
  for every router change from here.
- **Adaptive grid LANDED 2026-07-24, and it EXPOSED the real #1 blocker.**
  `_choose_grid`/`_window_node_count` + `autorouter.max_grid_mm` (1.0) in
  `kicad_router_tool.py`: per-connection grid coarsens (up to 1.0 mm) so the
  window fits the 400k-node budget — **39/39 kiln connections now fit** (grids
  ~0.26–0.48 mm for the long nets), short connections unchanged (byte-identical),
  187-test suite green, self-check unchanged (grid-independent), determinism
  held. BUT: **fitting the node budget did NOT make kiln routable** — the agent
  could not finish a single real kiln A* route in a practical time budget. Root
  cause (pre-existing, now the bottleneck): **`_FineWindow.obstacle_cells` /
  `_min_dist_to_edges` is O(cells × zone_edges)** — every grid cell does exact
  edge-distance tests against the big zone-pour polygons, which dominates
  runtime in kiln's zone-heavy areas (~mins per connection; the 27-min benchmark
  only finished because 35/39 fail-fast). Adaptive grid converted those
  fast-failures into slow routes, so `route_board` on the real board would now
  HANG rather than fail fast until this is fixed. **So do NOT re-run route_board
  on the real board yet.**
- **Zone-distance perf fix LANDED 2026-07-24** (Sonnet, coordinator-verified):
  `_FineWindow` now spatially buckets zone/obstacle edges so per-cell distance
  is sub-linear in `zone_edges`, with `_min_dist_to_edges_ref` kept as the
  parity reference (exact, not approximate — results byte-identical).
  **Verified: a zone-heavy `GND_Main` connection that previously would not
  finish now routes in 9.7 s** on a scratch copy. This unblocks real routing.
- **Real benchmark now COMPLETES (2026-07-24, ~10 min):
  `benchmark_kicad_autoroute(kiln, complete_only, quick)` = 4/39 (10.26%),
  post score 8568.267 vs human 8552.276 (still +15.99 worse),
  `matched_or_beat_human:false`, 1 new DRC violation.** So the infra work
  (perf + adaptive grid) fixed HANGING/`window_too_large` but only unlocked
  ONE more connection. **The 35 failures are now `unreachable_in_window`** — A*
  runs fast but genuinely can't thread pad-to-pad through the existing
  hand-routed copper + pours (verified on CLK/MISO/SDA/12V/5V/IC2-FB). This is
  real routing difficulty, not a budget artifact.
- **⚠️ USER DIRECTION 2026-07-24 — Phase 7.8 acceleration was the PREREQUISITE;
  it landed 2026-07-24, but PARTIALLY DISPROVED its own premise (see the 7.8
  LANDED anchor).** The hypothesis was: `unreachable_in_window` is a pure-Python
  limit (A* can't afford a FINE grid over a LARGE window → `_choose_grid`
  coarsens to ~0.3–0.5 mm → a coarse grid can't thread kiln's dense pin fields →
  unreachable), so numpy would make large fine-grid windows tractable and remove
  the coarsening. **What we found:** the numpy per-window wavefront relaxes the
  WHOLE window many sweeps (cost-insensitive), which is *slower* than
  output-sensitive A* on these windows — it does NOT make a 400k-cell fine
  window tractable. numpy's real value is batched whole-board fields (GPU/M5),
  and 7.8's delivered win on this box is **multi-core across independent
  connections**. So the fine-grid `unreachable_in_window` limit is **still open**
  and needs a smarter SEARCH/WINDOW strategy (fine grid only near pads,
  hierarchical windows, plane-aware admissible heuristic), not more vectorization
  — that is the next Opus-permitted task.
- **Reprioritization (updated per user):** (1) ✅ 7.16 benchmark, (2) ✅ adaptive
  grid, (3) ✅ zone-distance perf, (4) ✅ Phase 7.8 (numpy parity tier +
  multi-core independent-connection routing; parity suite is the gate) — LANDED
  2026-07-24, premise-corrected (numpy backend is NOT the fine-grid lever;
  multi-core is the delivered win). (5) ✅ **Routing-capability revisit — LANDED
  2026-07-24: the `_route_attempts` finer-grid ladder (sealed pad escapes) + the
  plane-via anti-pad model (cross-layer signal nets via through planes). Full-board
  completion 4 → 10 routed.** (See the ROOT-CAUSE DATA block above for the measured
  mechanism and residuals.) (7) ✅ **Speculative-parallel routing + feasibility screen — LANDED 2026-07-24
  (Sonnet subagent, coordinator-verified).** The old parallel phase only covered
  spatially-INDEPENDENT connections (rare on a dense board → effectively serial,
  which is why routing "took forever" once the fine-grid ladder made failing
  searches expensive). Now `_run_independent_routes` routes EVERY connection
  concurrently against the BASE board (`_worker_route_speculative`, workers via a
  light `_obstacle_recipe` that rebuilds obstacles locally instead of pickling
  them — pickling `base_obstacles` was profiled as the ~20s/worker dominant cost),
  the parent commits in canonical owner order self-checking each against
  already-committed copper, and only genuine cross-connection CONFLICTS fall back
  to the serial rip-up worklist. `_feasibility_screen` (coarse 1 mm BFS) only
  ORDERS pool submission (never gates — proven by test). **Verified: 3.71× speedup
  at 8 workers (878s→236s serial→parallel; default is auto=cores−1≈23, faster
  still); routed count held at 10; `connections` JSON BIT-IDENTICAL across
  workers=1 vs 8 on the REAL kiln board (the conflict-requeue path, not just the
  synthetic independent case); 205-test suite green (+4 in `test_parallel_route.py`,
  incl. one proving the screen never rejects a routable net).** Determinism
  invariant: workers COMPUTE pure functions against base-only; the speculative
  pass runs for ALL worker counts incl. 1 (so `workers` is a pure execution
  detail, not an algorithm switch — an earlier draft that gated it behind
  `workers>1` produced non-identical geometry and was caught/fixed).
  (8) ✅ **Window-build rasterization cache — LANDED 2026-07-24 (Sonnet subagent,
  coordinator-verified).** Profiling the parallel run showed `_FineWindow.build`
  → `obstacle_cells` was ~38% of per-connection time (re-rasterizing board-
  spanning plane fills on EVERY `_route_attempts` ladder rung). Now
  `_prefilter_window_obstacles` filters obstacles to the connection's max-margin
  window bound ONCE, and `_build_zone_edge_cache` builds each zone's
  `_ZoneEdgeGrid`/clipped edges once per connection (reused across rungs). Both
  proven byte-identical (safe because the bound is a superset of every rung's
  window/reach). Build cumtime −12% on the 6-net sample; 205-test suite green.
  **Test-infra note (coordinator, 2026-07-24):** the kiln GOLDEN tests now run
  against a git-HEAD **committed board snapshot** (`tests/conftest.py`
  `kiln_project_path`, CRLF-normalized), NOT the live working-tree board — the
  user edits `kiln.kicad_pcb` in KiCad continuously, which drifts the 6-zone/
  39-missing invariants; pinning to the committed board keeps the golden tests
  stable without regenerating ref stats (do NOT regenerate until the user asks;
  `KILN_USE_LIVE_BOARD=1` overrides). LATENT BUG FOUND (not fixed, out of scope):
  `unroute_nets` corrupts an **LF**-line-ending board (unbalanced parens) — never
  triggers from KiCad's CRLF output, but worth a line-ending-agnostic fix someday.
  (9) ✅ **Rip-up demotion for `self_check_failed` — LANDED 2026-07-26 (Sonnet
  subagent, coordinator-verified, `c98bed2`).** `_self_check` now tags every
  violation with `owner` (None = existing/human board copper — zone, pad, edge,
  hand track, never rippable; int = the autorouter connection id owning the
  colliding copper — rippable). `_route_one`'s self-check-failed branch keeps
  the found `path` and full `violations` list instead of discarding them. The
  worklist's Step-4 rip-up loop gained a parallel branch: when a self-check
  failure's violations are against rippable copper, it rips those owners'
  placements and re-`_finalize_core`s the SAME path (no re-search needed — it
  was already geometrically fine except for the named conflicts) against the
  reduced obstacle set; a skim whose violations are ALL against non-rippable
  copper (`owner is None` everywhere) is untouched and correctly stays a hard
  failure, same as before. 3 new tests (`tests/test_ripup_selfcheck.py`) fault-
  inject a synthetic self-check failure to prove: a rippable-owner skim gets
  demoted and both nets end up routed; an `owner=None` skim stays hard-failed;
  repeated runs are byte-identical. 154 passed/55 skipped (was 151/55) — no
  regressions. **Measured against the live board today: only 2 unrouted
  connections remain** (the board has been routed further since the 6-net
  measurement that motivated this item) — `3.3V_Main` (self_check_failed
  against an unconnected pad, `owner` always None for pads, correctly stays
  terminal) and `Net-(U6-BIAS)` (`unreachable_in_window` against the
  `GND_Safty` zone fill, unrelated to this item). Board score unchanged
  (`get_trace_cost` total 10367.891) since neither remaining failure was
  eligible for demotion — this item's value will show up once more
  self-check-failed nets exist to demote (e.g. from a future net whose
  hierarchical-tier result, item 10 below, skims).
  (10) ✅ **Hierarchical / multilevel windowing tier — LANDED 2026-07-26
  (Sonnet subagent, coordinator-verified, `ef7a71c`).** `_route_hierarchical`
  (+ `_hier_world_waypoints`, `kicad_router_tool.py`) chains small fine-grid
  `_FineWindow`s (span `_HIER_CHUNK_SPAN_MM`=8mm, margin
  `_HIER_WINDOW_MARGIN_MM`=3mm) along the global stage's own coarse path
  (decimated every ~8mm), stitches each leg's absolute-mm segments/vias, and
  runs `_self_check` ONCE end-to-end (seams get the same exact-clearance
  guarantee as any other route, no self-check changes needed). Gated strictly
  behind full `_route_attempts` ladder exhaustion — a connection that already
  routes today never reaches this tier and stays byte-identical. Intentionally
  NOT wired into rip-up (a hierarchical result is terminal, like
  `self_check_failed` was before item 9). 6 new tests
  (`tests/test_hierarchical_route.py`) prove it against a SYNTHETIC wall-with-
  a-narrow-gap scenario (every `_route_attempts` rung's coarsened grid
  provably misses the gap, a small fine-grid sub-window finds it) — chosen
  because the real board's 6 long nets turned out NOT to be an instance of
  this problem (see below). Also fixed (bonus): `tests/conftest.py`
  `kiln_project_path` assumed a fixed one-level nesting above `mykicadMcp/`,
  which silently broke (skipping 55 golden tests) under an agent-worktree
  checkout; now walks upward for the `kiln.kicad_pro` marker. 151→211
  passed, 55→1 skipped.
  **Re-diagnosis of the 6 long nets (BFS flood-fill over the real obstacle
  grid, not just A* failure): they are NOT a "channel exists but the grid
  missed it" case.** They sit in genuinely isolated copper islands — e.g.
  `/SaftyProcessor/saftyRelay`'s source pad has only ~21–154 total reachable
  `(x, y, layer)` states in the ENTIRE board, at ANY grid resolution,
  including via-hops through via-transparent GND pours (confirmed for a
  second net, `5V_Main`, as well). This is a hard topological enclosure — no
  legal channel exists at all — which no pathfinding-only tier (windowed,
  hierarchical, or otherwise) can fix; it needs zone rip-up/neck-down of hand
  copper, same as the "genuinely-sealed pads" item below. **So this landing
  is a real, tested, general-purpose capability (useful for any FUTURE net
  whose failure mode actually is grid-resolution-driven) but does not itself
  unblock the current 6 nets** — board score and routed-count unchanged
  (8552.276 / no new copper on the real board).
  (11) ✅ **Opt-in hand-copper rip-up — LANDED 2026-07-27 (Sonnet subagent,
  coordinator-verified, `5f13c53`), per explicit user authorization.**
  `allow_hand_copper_ripup: bool = False` (per-call argument on `route_nets`/
  `route_board`, plus CLI `--allow-hand-copper-ripup` and both MCP tool
  schemas — deliberately NOT a persisted `pcb_settings.json` field, since
  ripping a human's own routed copper is materially more destructive than
  the autorouter-copper-only rip-up in item 9, so permission is asked fresh
  every call, same convention as `write=`). `_Obst` gained `is_pad`/`uuid`;
  `_is_hand_copper_obstacle` (`kicad_router_tool.py` ~4082) scopes eligibility
  to hand-routed TRACK/ARC segments and VIAS ONLY — footprint pads, zone
  fills, and Edge.Cuts are structurally excluded and stay hard blockers
  regardless of the flag. Both Step-4 rip-up branches (`unreachable_in_window`
  and `self_check_failed`, ~5700-5830) offer a live `hand_copper_pool` when
  the flag is set; a ripped piece is removed from the pool immediately
  (anti-double-rip guard) and recorded in `human_copper_ripped` (uuid, net,
  kind, layer, geometry) on both the connection record and the top-level
  result/`summary.human_copper_ripped_count` — the audit trail to review
  before ever setting `write=true` for real. `write=True` deletes exactly
  those uuids from the board text via the same `_delete_blocks_by_uuid`
  surgery `unroute_nets` already uses. A latent correctness gap the flag
  exposed was fixed in the same landing: the 7.8b speculative pass previously
  treated every failure as terminal on the reasoning that rip-up only frees
  autorouter copper it never saw — false once hand copper (already in the
  base obstacle set) is rippable; now falls through to the serial worklist
  instead when the flag is on and the pool is non-empty. 7 new tests
  (`tests/test_hand_copper_ripup.py`) on a synthetic board cover flag-off
  no-touch, flag-on rip+route, write=True actually removing the board text,
  pad/zone/edge exclusion, and determinism. 208 passed/7 skipped with the
  flag off — byte-identical to before this landing (the 7 pre-existing
  failures below are unrelated board drift, not caused by this feature).
  **Real-board dry-run audit (`write=False`, git-HEAD committed snapshot):**
  the board has been hand-routed substantially further since the "6 sealed
  nets" diagnosis (item 10) was written — only **1** connection is unrouted
  today, `Net-(U6-BIAS)`, blocked by the `GND_Safty` **zone fill** (not hand
  track/via copper) — exactly the case this flag correctly still cannot help
  and honestly reports as still blocked. `human_copper_ripped_count: 0` on
  the real board today; the feature is real, tested, and working, it simply
  has nothing left to demonstrate on since the user's own hand-routing closed
  the rest.
  **BOARD-DRIFT NOTE (coordinator, 2026-07-27): the golden reference stats
  (6 zones / 39 missing connections) are now stale.** The user committed
  `d2f19fc "progress"` mid-session (2026-07-26 23:11) — substantial further
  hand-routing/re-pouring (6→14 zones, 39→16 missing connections, file
  112k→130k lines since the `1aa5abe` commit those numbers were pinned to).
  7 golden tests now fail against current HEAD (`test_kiln_finds_six_known_
  zones`, `test_kiln_ratsnest_still_39_missing_connections_after_zone_port`,
  and others downstream of those counts) — confirmed identical on unmodified
  pre-item-11 code, so this is board drift, not a regression from any of
  items 9-11. Per the standing rule ("do NOT re-baseline until the user
  explicitly asks"), the reference stats have NOT been touched — ask the user
  whether to re-baseline to the new committed board before doing so.
  **NOW: nothing left in the routing-completion arc** — the one remaining
  real-board gap (`U6-BIAS` vs. a zone fill) is a structurally different,
  bigger feature (zone-shape editing, not track/via rip-up) than anything
  scoped so far; not started, needs explicit scoping if pursued. Otherwise:
  re-baseline the golden stats (pending user go-ahead, see above), then
  quality/score work vs 8552.276. `benchmark` is the standing gate
  throughout.
  (12) ✅ **Phase 7.9 live progress viewer — LANDED 2026-07-27** (Sonnet
  subagent, coordinator-verified, worktree-isolated, merged clean). See the
  7.9 anchor for the full landing detail and spec deviations. 84 tools,
  226-test suite green (208→226, same 7 pre-existing board-drift failures
  before and after, unaffected). Docs debt closed same day (Haiku pass).
  (13) ✅ **Phase 7.5.5 plane writers — LANDED 2026-07-27** (Sonnet subagent,
  worktree-isolated; coordinator found and fixed one test-fixture bug before
  merging — see the 7.5.5 anchor). Also lands the two small 7.5.4 residuals
  (estimated-fill wired into the plane router; `pipeline.plane_aware_routing`
  relabeled `"partial"`). 84→87 tools, 219-test suite green (208→219, same 7
  pre-existing board-drift failures, unaffected). Merge required one
  mechanical conflict resolution in `kicad_mcp_server.py` against the
  already-merged 7.9 viewer branch (both added tool registrations at the same
  insertion points — no logic conflict, both sets of tools kept). Docs debt
  (three plane tools + README/CLAUDE.md count) closed same day (Haiku pass,
  see the docs-sync note below); CLAUDE.md itself still needs the user's own
  commit in the parent repo (coordinator does not auto-commit there).
  (14) ✅ **Phase 7.12 neck-down — LANDED 2026-07-27** (Sonnet subagent,
  worktree-isolated, merged fast-forward — no conflict with the concurrently-
  landed 7.5.5). See its anchor for the full write-up. 87 tools (unchanged —
  no new MCP tool), 237→243-test suite green, same 7 pre-existing board-drift
  failures. Honest residual: the hierarchical last-resort routing tier is
  deliberately not wired for neck-down (documented in-code and in the anchor).
  (15) ✅ **Phase 7.3d direction-aware pad escape — LANDED 2026-07-27** (Sonnet
  subagent, worktree-isolated, merged fast-forward). See its anchor for the
  full write-up. 87 tools (unchanged), 243→247-test suite green, same 7
  pre-existing board-drift failures. Deliberately scoped as a new
  default-`false` setting rather than a plain behavior change, since
  `nearest_free` is called unconditionally for every routed connection today —
  flag off is byte-identical parity, proven both by dedicated tests and the
  unchanged full-suite results. Flipping the default to `true` needs a
  real-board `benchmark_kicad_autoroute` before/after comparison and the
  user's sign-off first — not done, tracked as a follow-up.
  (16) Session housekeeping: also fixed two stale `docs/mcp-tools/
  11-autorouter.md` claims that had drifted behind the code for a while —
  rip-up & reroute and plane-aware routing were still documented as "NOT YET
  IMPLEMENTED" despite landing 2026-07-23/2026-07-24 respectively (Haiku docs
  pass, coordinator-merged with one mechanical conflict against the
  concurrent 7.12-mention docs pass — same paragraphs, no logic conflict).
  (17) ✅ **Phase 7.6 whole-board optimizer CORE — LANDED 2026-07-27** (Opus
  subagent — explicit standing authorization for this specific work per the
  2026-07-24 user decision below; worktree-isolated; coordinator did a full
  independent code read-through plus its own from-scratch test runs, not just
  a report review, given this is the largest single delegation in the plan so
  far). See the 7.6 anchor for the full write-up: all six move types
  implemented (five as specced, one — layer swap — honestly reinterpreted
  since no per-net home-layer override exists in the router), greedy + SA
  acceptance, resumable checkpointed sessions with RNG-state round-tripping so
  chunked and one-shot runs decide identically. 89 tools (was 87). 28 new
  tests, full suite 247→275 passed, same 7 pre-existing board-drift failures,
  7 skipped. Human copper and the six hand-made zones are safe by construction
  (the optimizer never bypasses `unroute_nets`'/`modify_plane`'s own ownership
  guards, adds no new check of its own). **Deliberately deferred to a
  follow-up delegation:** Phase 7.7 in full (the AI-decision-pause protocol,
  `decide_kicad_route`, `awaiting_decision` state) — a test guards that
  `decide_kicad_route` stays unregistered rather than half-built. Also this
  session's stale-cross-reference sweep found and fixed several other places
  in this file that still described rip-up/plane-aware-routing/7.5.5/7.8 as
  pending when they had already landed (the MCP tool summary table and build
  order items 12-16) — a reminder that "How to work this plan"'s
  cross-reference-sync rule needs to be followed more consistently going
  forward, not just applied to the newest landing.
  (18) ✅ **Phase 7.7 AI-in-the-loop decision protocol — LANDED 2026-07-27**
  (Opus subagent, same standing authorization as 7.6, worktree-isolated,
  independently re-verified by the coordinator). See the 7.7 anchor for the
  full write-up: `awaiting_decision` state, `decide_kicad_route`, the
  scripted-decider test harness the original plan called for, and a genuine
  correctness fix the implementer's own tests caught before landing (a
  decision-resolving move skipped the convergence check the auto-accept path
  already ran). 89→90 tools, 275→284 passed, same 7 pre-existing failures.
  **This closes out the 7.6/7.7 delegation pair the plan called for** — SVG
  option previews and a dedicated replay executor are honest, documented
  gaps, not required for the core mechanism to work. Unblocks 7.5.6 stitching
  and 7.14's pin-swap pause protocol, both previously gated on this landing
  (see their respective sections/build-order items).
  (19) ✅ **Phase 7.5.6 plane stitching pass — LANDED 2026-07-27** (Sonnet
  subagent, worktree-isolated, merged fast-forward). See its anchor for the
  full write-up: `run_kicad_stitching_pass` (island rescue, return-path,
  general stitching, in the specced order) + `remove_kicad_stitching_vias`
  (scoped deletion, `include_foreign` listing), reusing `_place_stitching_via`
  from the 7.6 landing via a new backward-compatible `stitching=True` flag.
  90→92 tools, 284→293 passed, same 7 pre-existing board-drift failures.
  **This closes out Phase 7.5 entirely** (see build-order item 13). Unblocks
  nothing further by itself, but was itself the last piece 7.14's pin-swap
  protocol and further M4 work were waiting on alongside 7.6/7.7.
  (20) ✅ **Phase 7.15 effort presets + plateau stopping — LANDED 2026-07-27**
  (Sonnet subagent, worktree-isolated, merged fast-forward). See its anchor
  for the full write-up: `optimizer.effort` (quick/balanced/best) bundling
  the other optimizer knobs with a three-deep precedence, and the plateau
  rule running alongside `convergence_delta` rather than replacing it. Honest
  scope-down: `cpu.replicas` stays unwired since nothing in this codebase
  reads it. 92 tools (unchanged), 293→303 passed, same 7 pre-existing
  failures. **This closes out build-order item 14 (7.6/7.7/7.15) entirely** —
  only the viewer's cancel/decision UI and portfolio replicas remain as
  low-priority residuals there.
  (21) ✅ **Phase 7.14 connector pin-swap advisor — LANDED 2026-07-27** (Opus
  subagent, worktree-isolated, coordinator did a full independent code
  read-through given the safety-criticality of "never edit the schematic/
  netlist" — not just a report review). See its anchor for the full
  write-up: a seventh optimizer "move" that is never applied by this tool,
  priced as a controlled A/B via trial-only pad-net swaps on scratch board+
  netlist copies, escalated as a MANDATORY (not `ai_decisions`-gated) pause
  when it clears `pin_swap.min_gain`, with a re-sync path that adopts (never
  decides) the real board's post-edit pad assignment, and a defense-in-depth
  safety gate refusing any `write=True` whose scratch/real pad-net maps
  disagree for any reason. 92 tools (unchanged), 303→317 passed, same 7
  pre-existing failures. **This closes out essentially all of Phase 7's
  originally-scoped feature list.**
  (22) ✅ **M5 whole-board windowing + GPU tier — LANDED 2026-07-28** (Opus
  subagent, worktree-isolated, coordinator-reviewed: full diff read plus an
  independent full-suite run against the merged tree — see the M5 anchor).
  93 tools (was 92), 317→361 passed, same 7 pre-existing failures.
  (23) ✅ **Viewer auto-close — LANDED 2026-07-28** (coordinator-implemented,
  user request): an unattended/config-driven viewer launch now closes itself
  after `run_complete`; an explicit `open_kicad_route_viewer` call never does
  — see the 7.9 anchor. 5 new tests, 361→365 passed.
  (24) ✅ **M7 opened at user request (three new phases: 7.18 fill/via
  engineering, 7.19 lightweight route-cost estimation, 7.20 adjacent-layer
  crosstalk avoidance), and Phase 7.18 LANDED 2026-07-28** (Opus subagent,
  worktree-isolated, coordinator-reviewed — see its anchor for the full
  write-up, including a nontrivial integration pass against the just-merged
  M5 work). 365→398 passed, same 7 pre-existing failures.
  (25) ✅ **Phase 7.19 lightweight route cost estimation LANDED 2026-07-28**
  (Opus subagent, worktree-isolated, coordinator-reviewed — full diff read,
  independent full-suite run, AND an independent wall-clock spot-check on
  the real kiln board; see its anchor for the honest wall-clock finding).
  398→435 passed, same 7 pre-existing failures.
  (26) ✅ **Phase 7.20 adjacent-layer crosstalk avoidance LANDED 2026-07-28**
  (Opus subagent, worktree-isolated, coordinator-reviewed — cost-model
  integration read directly, independent full-suite run on the merged tree;
  see its anchor). 435→457 passed, same 7 pre-existing failures. **This
  closes milestone M7 in full** — all three phases the user requested this
  session (7.18, 7.19, 7.20) are landed. **The only clearly-open item
  remaining anywhere in the plan is Phase 7.13 impedance-matched traces**
  (spec'd, not started) **and assorted low-priority residuals** (viewer
  cancel-flag/decision banner, portfolio replicas, hybrid GPU/CPU scheduling,
  driving `torch` as a second GPU array module, the small 7.3b "any same-net
  copper" termination bit, and real-hardware GPU verification — no
  cupy/torch installed in this environment).

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

### 7.3d — LANDED 2026-07-27 (reference anchor; no work remains here)

Implemented in `kicad_router_tool.py` (Sonnet subagent, worktree-isolated,
coordinator-reviewed, merged fast-forward). `_FineWindow.nearest_free` gained
an optional `toward_xy` parameter: when the winning ring has more than one
free-layer candidate, the tie-break is biased toward the candidate whose
vector from `(x, y)` has the largest dot product with the direction toward
`toward_xy` (the connection's other endpoint) instead of pure Euclidean
distance — "escape toward where you're going," not "escape to the closest
open spot." Wired into both call sites (`_route_one`'s `s_cell`/`g_cell`,
`_route_hierarchical`'s per-leg `s_cell`/`g_cell`) via a new picklable
`ctx["pad_escape_direction_aware"]` bool, gating whether `toward_xy` is passed
at all. **Strict parity, as specced:** `toward_xy=None` (every call site when
`autorouter.pad_escape_direction_aware` — new knob, default `false` — is off)
and any ring with only one free candidate reproduce the exact pre-7.3d code
path (the `biased` list is `None` and simply never built). 4 tests in
`tests/test_pad_escape_direction_aware.py`, exercising `nearest_free` directly
against hand-built `blocked_track` fixtures (isolating the ring-search/
tie-break logic from obstacle-geometry conversion, which other tests already
cover): flag off picks the pure-nearest "wrong side" node; flag on picks the
farther but direction-aligned node instead; a single-candidate ring is
unaffected by the flag either way; and the no-`toward_xy`-argument call shape
every real call site uses is pinned. 87 tools (unchanged), full suite
243→247 passed, same 7 pre-existing board-drift failures, 7 skipped.

**Still open, by design:** the flag remains `false` — flipping the DEFAULT to
`true` requires a deliberate before/after `benchmark_kicad_autoroute`
comparison on the real board plus the user's sign-off (this cannot be judged
by parity alone, unlike 7.12), and has not been done. Track that as a
follow-up if/when pursued.

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

**Residual (accepted, documented in-code):** `_fine_astar`'s distance-only
heuristic (pre-existing) is not admissible for a plane-discounted state, so the
router returns a valid / deterministic / DRC-safe path but not always the
global cost optimum. (The other two residuals noted at 7.5.4's original
landing — the estimated-fill fallback not feeding the plane model, and the
pipeline report's stale `not_implemented` label — were fixed alongside 7.5.5,
see its anchor below.)

### 7.5.5 — LANDED 2026-07-27 (reference anchor; no work remains here)

`propose_plane`/`create_plane`/`modify_plane` implemented in
`kicad_router_tool.py`, registered as `propose_kicad_plane`/`create_kicad_plane`/
`modify_kicad_plane` (Sonnet subagent, worktree-isolated, coordinator-reviewed
and merged with one mechanical conflict resolution — both this branch and the
already-merged 7.9 viewer branch added tool registrations at the same
insertion points in `kicad_mcp_server.py`; resolved by keeping both, no logic
conflict). `propose_plane(net, layer=None)` is read-only (no ownership
restriction — a suggestion even against a hand-made zone/net): grid-based
candidate outline from the net's own pad/via bounding box (inflated by
reach + margin, clipped to `Edge.Cuts`), auto-picks a layer by `layer_purpose`
type matching the net's kind (7.2) when `layer` is omitted, runs the same
7.5.2/7.5.3 estimation pipeline `audit_plane_islands` uses for
mainland/island/orphan costing, and returns a `cost_delta` vs. the net's
current routed trace cost. `create_plane(..., write=)` writes a native-looking
`(zone ...)` block (fill-setting shape copied from an existing board zone via
`_zone_template_shape`, the same "copy an existing definition's shape" idea
`create_netclass` uses) and records the new uuid in board-local
`autorouter_owned.zones`. `modify_plane(uuid, new_outline=, priority=, write=)`
does uuid-anchored polygon/priority s-expr surgery (same discipline as
`delete_group`/`unroute_nets`) and **refuses** (raises, never silently
proceeds) on any uuid not in `autorouter_owned.zones` — the six hand-made kiln
zones (mainGnd, safty_gnd, antenna, main3.3, main12v, 3.3v_safty) can only ever
be *proposed* for change via the read-only `propose_plane`, never auto-mutated.
Also lands, in the same commit, the two 7.5.4 residuals noted above:
`_plane_fill_index_with_estimated` wires the 7.5.2 estimated-fill fallback into
`route_nets`'s plane model (previously only KiCad-authoritative `filled_polygon`
zones were routable planes), and `route_board`'s `pipeline` report now says
`plane_aware_routing: "partial"` instead of `"not_implemented"`.

84→87 tools. 11 tests in `tests/test_plane_writers.py` (synthetic board +
one read-only kiln smoke test for `propose_plane` against `GND_Main`).
**Coordinator fix applied post-landing:** the subagent's own
`test_propose_plane_returns_sane_outline_and_cost_delta` used a synthetic net
named `PWR3V3`, which does not actually match any default
`power_net_patterns` regex (`3\.3[Vv]` requires a literal decimal point,
`PWR3V3` has none) — the test was asserting `net_kind == "power"` against a
net the code correctly classified as `"signal"`. Renamed the synthetic net to
`3.3V_RAIL` (which does match) throughout the test file rather than loosening
the assertion or the production regex — the regex and code were correct, the
test fixture was wrong. Full suite green after the fix: 219 passed / 7
pre-existing board-drift failures (unrelated, unchanged) / 7 skipped.

### 7.5.6 — LANDED 2026-07-27 (reference anchor; no work remains here)

Implemented in `kicad_optimizer_tool.py` (Sonnet subagent, worktree-isolated,
coordinator-reviewed, merged fast-forward). `run_stitching_pass`/MCP tool
`run_kicad_stitching_pass` runs the three specced steps in order — island
rescue (one via per costed island/orphan `audit_plane_islands` already
reports, at its own `suggested_stitching_via.position`), return-path
stitching (vias near `classify_critical_nets`' routed copper, on the
same-layer power/ground plane, at `stitching.near_high_speed_pitch_mm`
spacing within `stitching.near_high_speed_mm` of the trace, filtered to
points landing inside the plane's own drawn outline), and general stitching
(a grid fill of each power/ground plane toward `stitching.target_spacing_mm`,
capped per zone/layer by an internal engineering bound, not a settings
field). `_place_stitching_via` (the bare-via writer 7.6's move (d) already
introduced) gained an optional `stitching=True` flag: every pre-existing
caller (the optimizer's own move (d)) omits it and is byte-identical to
before, while this pass's vias get tagged `"stitching": True` in the
board-local record so `remove_stitching_vias`/MCP `remove_kicad_stitching_
vias` can target exactly them — never an ordinary routing via, a hand-placed
via, or the optimizer's own untagged move-(d) via (a deliberate scope line:
that one belongs to an `optimize_kicad_board` session, not this pass's
bookkeeping). `remove_kicad_stitching_vias(area=None, write=False,
include_foreign=False)` restricts to a rect/polygon area; `include_foreign`
lists (never deletes) other freestanding same-net vias using the codebase's
existing free/oversized-via heuristic (`get_kicad_track_inventory`'s
characterization, reused rather than reinvented).

**Spec deviation, honest and narrow:** the return-path/general-stitching
containment test uses each plane zone's DRAWN outline as a proxy for its
actual fill area, rather than re-running the full 7.5.2 fill/flood-fill
estimation per candidate point — a reasonable simplification since most of a
real pour's outline area is its mainland (island rescue, which needs the
precise per-island decomposition, uses the exact `audit_plane_islands` model
instead and isn't affected by this).

**Interaction rule (session contract) — documented, not enforced in code, by
design:** the plan calls for the calling session to ask the user before
routing/optimizing in an area with existing stitching vias. This is not
something a Python function can verify about its caller's future intent, so
it's documented in both tools' MCP descriptions as a convention (the same
treatment `allow_hand_copper_ripup` already gets) rather than built as
enforcement machinery — removed stitching copper is simply re-placed by the
next pass anyway, so nothing is lost by asking.

New `stitching` block in `DEFAULT_PCB_SETTINGS`: `{target_spacing_mm: 5.0,
near_high_speed_mm: 1.0, near_high_speed_pitch_mm: 2.0, enabled: true}`.
90→92 tools. 9 tests in `tests/test_stitching.py`. Full suite 284→293
passed, same 7 pre-existing board-drift failures, 7 skipped.

### 7.6 CORE — LANDED 2026-07-27 (reference anchor; 7.7's decision layer landed too, see its own anchor below)

Implemented in new module `kicad_optimizer_tool.py` (1075 lines; Opus subagent
— explicit standing authorization per the 2026-07-24 user decision recorded
below, worktree-isolated, coordinator-reviewed including independent re-runs
of the full suite and the new test file, merged fast-forward). `score_board`
computes exactly the specced `S` (`get_trace_cost` board total + `audit_plane_
islands` total island cost + `unrouted_penalty × unrouted_count` from
`get_ratsnest`); `_ranked_nets` ranks every cost contributor worst-first
(routed nets at trace cost, unrouted nets at their penalty — the plan's
"rank nets/planes by cost contribution" step). Each iteration generates up to
`_MAX_CANDIDATES_PER_ITERATION` (6, an implementation cost bound, not a
settings field) candidates across all six move types, scores each on its own
trial, and accepts the best per `optimizer.accept` (`greedy` strict-improvement
only, or `sa` with `exp(-ΔS/T)` / `T *= sa_cooling`).

**Architecture deviation from the original text, approved as the sane reading
of the actual requirement:** "in-memory board model" is implemented as a
**private scratch copy of the whole project** (the same pattern
`benchmark_autoroute` already uses), not a parallel in-memory zones/tracks/
vias representation — building the latter would fork every existing
uuid-anchored s-expr writer (`route_nets`, `unroute_nets`, `create_plane`,
`modify_plane`) into a file path and a memory path, duplicating exactly the
logic this whole plan has avoided duplicating everywhere else. The real board
file is opened read-only once, at session creation (to copy it), and is never
touched again until an explicit `write=True`; every move calls the SAME
writers real routing/plane calls use, so human-copper safety and the
six-hand-made-zones restriction come for free from `unroute_nets`'s
`autorouter_owned`-only deletion and `modify_plane`'s ownership refusal — the
optimizer adds no new safety check of its own, it simply never bypasses the
existing ones.

**Move-by-move status — five fully implemented as specced, one narrowed with
the gap documented in-code:**
- (a) rip-up + reroute, (b) bundle reroute (genuinely corridor-aware — members
  route in ONE `route_nets` call so the global stage's existing `off_corridor`
  cost applies), (e) create plane, (f) modify plane (zone-outline perturbation
  is a centroid scale, not a cost-derived reshape — deriving an optimal
  outline analytically is out of scope) are full, direct reuse of already-
  landed tools.
- (c) layer swap: there is no per-net home-layer override anywhere in the
  router (home layer is *derived* by `_dominant_layer`), so "swap a routed
  net's layer" is expressed by temporarily multiplying the net's current
  dominant-layer-type purpose weight ×6 in the TRIAL's own
  `pcb_settings.json`, rerouting, then restoring the real weights before
  scoring (a test asserts the restore) — an honest reinterpretation of
  "layer-purpose driven," not a shortcut.
- (d) stitching via: required the ONE genuinely new piece of board surgery in
  this landing, `_place_stitching_via` (a bare via outside any route — nothing
  else on the board ever emits one), reusing the router's own via-block
  serializer/ownership recording. Placed exactly where `audit_plane_islands`
  already computes `suggested_stitching_via`; does not re-run `_self_check`
  clearance proof (the suggested point is by construction inside the island's
  own same-net pour), same "needs a KiCad refill + DRC pass" caveat as every
  other writer here.

**Sessions:** `optimize_board` runs a bounded chunk (`max_iterations_per_call`
or `max_seconds`, whichever binds first) and returns `{session_id, state,
score_curve, moves, diff}` — **three states, not the specced four**
(`running`/`converged`/`budget_exhausted`; `awaiting_decision` is 7.7's, not
built — see below). ALL loop state, including the RNG state itself, round-
trips through the board-local JSON checkpoint (`optimizer_sessions`), which is
what makes a chunked run and a single big-budget run decide identically (the
call boundary is not an input to any decision — proven by a dedicated test).
`get_kicad_route_session` reports a session read-only without advancing it.
`write=True` copies the scratch board's final accepted state onto the real
board (not a replay of individual moves — the scratch board IS the state that
was scored) and merges its `autorouter_owned` into the real board-local state;
it refuses while `running` or if the real board's fingerprint (size+mtime)
changed since the session started.

**Deliberately out of scope, tracked for a follow-up delegation (per the
plan's own stated strategy — "7.6/7.7 are separate delegations, each landed
and reviewed before the next"):** Phase 7.7 in full — the `awaiting_decision`
state, `decide_kicad_route`, and `optimizer.ai_decisions` (present in the
settings schema, never read). A test explicitly guards that `decide_kicad_
route` stays unregistered rather than half-built. Also NOT done: the "`route_
kicad_nets` rides the same session mechanism" aspiration from the original
text — `route_nets` still completes in one call with its own chunking-free
worklist; unifying it with the optimizer's session mechanism was not
attempted and is not required for 7.6 to function.

89 tools (was 87 — `optimize_kicad_board`, `get_kicad_route_session`). 28 new
tests in `tests/test_optimizer.py` (score curve non-increasing under greedy;
dry-run byte-identical board across a whole session; write applies final
state + immediate re-convergence; determinism from seed incl. chunked-vs-
one-call equivalence; session reporting at every state; human copper and
hand-made zones untouched on a fixture that has both; all six moves
independently reachable; the 7.7-absence guard). Full suite (independently
re-run by the coordinator, not just trusted from the subagent report):
247→275 passed, same 7 pre-existing board-drift failures, 7 skipped.

**Residuals noted by the implementer, accepted:** scratch temp directories are
never reaped (deliberate — a session must survive to be resumed later — but
they accumulate under the OS temp dir across sessions); each candidate's
`route_nets` call spawns its own worker pool (nested-parallelism is harmless
today — full-suite runtime was unchanged — but is arguably the wrong level for
future tuning); `route_board`'s `pipeline.whole_board_optimization` still
reports `"not_implemented (Phase 7.6, M4)"`, which is literally true (7.6 is
its own tool, not wired into that orchestrator) but reads as more stale than
it is now — left alone since an existing test
(`test_route_board_pipeline_hooks_declared_not_faked`) asserts that exact
string and changing it is a docs/plan judgment call, not a code one.

### 7.7 — LANDED 2026-07-27 (reference anchor; no work remains here)

Implemented directly on top of the 7.6 core in `kicad_optimizer_tool.py` (Opus
subagent, same standing authorization as 7.6, worktree-isolated, coordinator
did a full independent code read-through plus its own from-scratch test runs
rather than trusting the report alone). `SESSION_STATES` gained the fourth
state, `awaiting_decision`. `_pause_check` is the gate: a pause fires only
when `ai_decisions.enabled` is true, at least 2 applicable candidates exist,
the winning candidate's own decision type (`_decision_type_for`, mapping the
six move types onto the plan's six `decision_types`, with `give_up_net`/
`sa_large_move` detected from the situation rather than the move type — see
its code comment) is in the `decision_types` allowlist, `max_pauses_per_run`
hasn't been spent, and — a correctness addition beyond the original text —
the leading candidate would actually improve the score under `greedy` (else
every option is about to be rejected regardless of choice, so a converged
board would otherwise escalate a meaningless menu every iteration). On a
pause, `pending_decision` carries 2–4 already-applied, already-scored options
(id, one-line summary, score delta) whose trial directories are copied into a
stable location so a pause survives an MCP restart. `decide_route`/MCP tool
`decide_kicad_route(session_id, decision_id, choice, rationale)` applies the
chosen option (or `"defer"` for the optimizer's own best-scored default),
appends to `decision_log`, and returns the session to `running` (or straight
to `converged`, if the committed move was the one that stopped buying
`convergence_delta` — see the correctness note below) — it runs no further
iterations itself. Resuming an `awaiting_decision` session via
`optimize_kicad_board` without answering first auto-resolves it as `defer`,
so an abandoned session still converges. Every committed move — auto or
AI-decided — appends one self-contained entry to `decision_log`.

**Correctness fix caught by the implementer's own tests (not a spec
deviation, a genuine bug the tests found before landing):** resolving a pause
originally always returned to `running`, skipping the convergence check the
auto-accept path already ran — so a run whose *last* move happened to be
escalated diverged from an identical unescalated run (reporting
`budget_exhausted` where the other correctly reported `converged`).
`_resolve_pending` now runs the exact same `improvement < convergence_delta`
test, so `decide_route` can itself return `converged`.

**Parity, the load-bearing guarantee (three independent proofs in one test):**
an `ai_decisions.enabled: false` run, a `min_score_spread: 0.0` run (nothing
can ever be close enough to pause), and a run forced to pause on every
eligible iteration but always answered `defer` all produce byte-identical
move sequences and score curves — proving `defer` really is the 7.6 rule,
not an approximation of it.

**Scoped down, documented honestly:**
- **Per-option SVG snippets — not built.** Nothing in this codebase renders a
  board to SVG; adding that would mean new export machinery (and a
  `kicad-cli` dependency) on the critical path of every pause. The `svg`
  field is present on each option and always `None`; the numbers + one-line
  summary are what a decision is actually made on.
- **A dedicated replay executor — not built,** per the task's own scoping
  guidance. `decision_log` entries are self-contained (options, per-option
  scores, choice, resolved choice, rationale, auto flag, accept outcome), so
  seed + log is sufficient for a human (or a future tool) to reconstruct why
  a run came out the way it did; building the executor itself is future work.
- **Two 7.6 tests that existed specifically to assert 7.7's absence were
  replaced** (one asserted `decide_kicad_route` was unregistered; one grepped
  the source for `ai_decisions` never being read) — correctly so, since those
  conditions are no longer true; a new registration test and a settings-block
  test (asserting the 6.1 key names are read, not invented) took their place.
- Five existing resume loops widened from `while state == "running"` to
  `while state in ("running", "awaiting_decision")` — not a relaxation, since
  resuming a pause auto-defers per spec, so those tests still assert the same
  outcome, just correctly stepping past a state that can now legitimately
  occur mid-loop.

89→90 tools (`decide_kicad_route`). 10 new tests in `tests/test_optimizer.py`
(37 total in that file now), full suite 275→284 passed, same 7 pre-existing
board-drift failures, 7 skipped (coordinator independently re-ran both the
optimizer file and the full suite, not just reviewed the report).

**Residual (noted by the implementer, accepted):** `write=True` called on an
`awaiting_decision` session silently auto-defers, runs a chunk, and then
writes, rather than refusing outright — consistent with the general
resume-auto-defers-a-pause semantics, but a caller expecting a hard refusal
on an unresolved decision gets a write instead. Worth revisiting if it proves
surprising in practice; not blocking, since the underlying safety guarantees
(dry-run-by-default, board-fingerprint check) are unaffected.

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

### 7.9 — LANDED 2026-07-27 (reference anchor; no work remains here)

Implemented: `kicad_route_viewer.py` (new, 589 lines — pure JSONL replay/diff
layer (`iter_events`, `ProgressState`/`replay_state`) importable and testable
without `tkinter`; `_load_kicad_layer_colors()` theme resolution (KiCad
`pcbnew.json` → `colors/*.json` → embedded `DEFAULT_LAYER_COLORS` fallback);
`RouteViewerApp` — Canvas board redraw, two progress bars, score sparkline,
pan/zoom, "Stop after this iteration" cancel button; `main()` CLI entry point,
degrades to a clear stderr message when `tkinter` is unavailable). Wired into
`kicad_router_tool.py`: `route_nets` now truncates `<board>.route_progress.jsonl`
at the start of each call and appends `header`/`connection`/`cancelled`/
`run_complete` JSONL events (gated by `autorouter.progress.events`); the
per-connection loop polls a `route_cancel_requested` board-local flag between
connections (not mid-search) for a clean stop, reporting unattempted
connections honestly as `cancelled_before_attempt` rather than a routing
failure; `open_route_viewer()` spawns the detached viewer subprocess (also
auto-launched when `autorouter.progress.open_viewer` is set). New MCP tool
`open_kicad_route_viewer` registered in `kicad_mcp_server.py` (83→84 tools).
`.gitignore` gained `*.board_local.json` (previously missing entirely) and
`*.route_progress.jsonl`. 18 new tests in `tests/test_route_progress.py`
(event shape/ordering, reset-not-accumulated, disable-via-settings, cancel
mid-run + stale-flag reset, color parsing/theme-resolution/fallback, JSONL
parse/replay, graceful tkinter-unavailable degradation) — 208→226 passed, same
7 pre-existing board-drift failures before and after (unaffected).

**Spec deviations (Sonnet subagent, coordinator-reviewed):**
- Per-connection progress events use a run-local id (`f"{owner}:seg:{i}"` /
  `f"{owner}:via:{i}"`), not the final board uuid — `route_nets` only assigns
  real board uuids once, in a single batch, at the very end of the function.
- `score` in each connection event is a cumulative-routed-length proxy, not a
  full `get_trace_cost` board score (too expensive to compute per connection);
  documented as a placeholder the sparkline can still plot meaningfully.
- Cancel-flag polling only gates the serial rip-up worklist, not the 7.8b
  speculative parallel pre-pass — most connections resolve there and never
  reach the serial loop, so the serial loop is where a mid-run stop is
  actually reachable.
- The viewer's redraw-on-poll re-derives full state from the JSONL file each
  tick rather than true incremental Tk item diffing (the file is small enough
  that this is cheap) — an intentional simplicity/cost tradeoff, not a bug.
- `decision_protocol` is a left-in `None` hook in the header event and
  `ProgressState` (no 7.6/7.7 optimizer/decision protocol exists yet to feed
  it).

**Auto-close added 2026-07-28 (coordinator-implemented, user request):** a
viewer window auto-LAUNCHED by `autorouter.progress.open_viewer` (the
unattended/config-driven case — a session isn't necessarily sitting there
watching) now closes itself ~4s after `run_complete`, via a `--auto-close` CLI
flag `open_route_viewer(..., auto_close=True)` passes only from its internal
`route_nets` call site. The explicit `open_kicad_route_viewer` MCP tool call
(a human/session deliberately asking to watch) always passes `auto_close=
False` and is unaffected — that window stays up for review. 5 new tests in
`tests/test_route_progress.py` (flag plumbing both directions, the internal
call site, CLI parsing); full suite still green.

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

### 7.12 — LANDED 2026-07-27 (reference anchor; no work remains here)

Implemented in `kicad_router_tool.py` (routing side) and `kicad_pcb_tool.py`
(`audit_netclass_conformance` acceptance side); Sonnet subagent, worktree-
isolated, coordinator-reviewed and merged (fast-forward, no conflicts against
the concurrently-landed 7.5.5). `_neck_targets_for_conn` identifies a `from`/
`to` connection endpoint as a pad whose smaller copper dimension the net-class
width would overrun by more than `neck_down.max_width_vs_pad`, computing
`neck_width = min(class_width, max_width_vs_pad × pad's smaller dimension)`
floored at the board's `min_track_width` DRC rule. `_apply_neck_endpoint`
re-tags the final stretch of already-emitted copper at that endpoint
(splitting the boundary segment if the cut falls mid-segment) to the narrower
width, clamped into `[min_length_mm, max_length_mm]` with a floor at
`_NECK_ESCAPE_RING_CELLS × grid` (reusing the existing pad-escape reach
constant rather than inventing a new one). Wired into `_finalize_core`
(called from the normal `_route_one` ladder, its rip-up re-finalize calls, and
the speculative parallel pass — all three now pass `conn` through). `_self_check`
now prices any segment carrying its own `"width"` key at THAT width rather than
the net's uniform `rules["track_width"]` — DRC-true neck pricing. **Strict
parity guarantee, tested both directions:** a segment with no `"width"` key
(every segment from every other landed feature) is byte-identical to pre-7.12
behavior, and `neck_down.enabled: false` restores it exactly even for
connections that would otherwise get a neck.

`audit_netclass_conformance` no longer unconditionally flags a net whose
dominant width differs from its class: each individual offending segment is
checked (`_neck_conformant_segment`) and the mismatch is suppressed only when
EVERY offending segment is a genuine neck (terminates within reach of a pad on
the net, at that pad's exact justified width, within the configured length
bounds) — a merely-narrow segment failing any of those checks still flags the
mismatch (this is per-segment, not per-net: one real violation mixed with
legitimate necks still fails the net). Accepted rows report `neck_segments:
<count>`.

**Honest residual (documented in-code, deliberate scope-down):** the Phase
7.3b hierarchical last-resort tier (`_route_hierarchical`, only reached once
the full `_route_attempts` ladder has failed every rung) runs its own inline
`_route_to_emit`/`_self_check` and is NOT wired for neck-down — a connection
that only routes via that rare fallback emits at full class width even onto a
small pad. Scoped out deliberately to avoid touching that tier's own from-
scratch self-check/emit path and risking its landed seam-safety guarantee for
a corner this phase does not require.

Knobs `neck_down`: `{enabled: true, max_width_vs_pad: 1.0, min_length_mm: 0.5,
max_length_mm: 3.0}` (added to `DEFAULT_PCB_SETTINGS`). 87 tools (unchanged —
no new MCP tool, routing/audit behavior + schema only). 6 tests in
`tests/test_neck_down.py`: genuine-neck emission (width/length/termination
checked), enabled-vs-disabled byte-identical parity for a pad that needs no
neck, `_self_check` pricing proof (a neighbor obstacle that clips at the wide
width but clears at the narrow width), and `audit_netclass_conformance`
accepting a genuine neck while still flagging a wrong-width and a too-long
fake neck (plus the `enabled: false` strict-mode restoration). Full suite:
237→243 passed, same 7 pre-existing board-drift failures, 7 skipped.

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

### 7.14 — LANDED 2026-07-27 (reference anchor; no work remains here)

**Detection LANDED 2026-07-23** (anchor): `detect_connectors(project_path,
ref_prefixes=None)` → tool `detect_kicad_connectors` (read-only) in
`kicad_pcb_tool.py`, plus `validate_connector_exclusions(...)` (loud-abort on
an unresolved exclusion name). 14 tests in `tests/test_connectors.py`.
Measured on kiln: 24 J-prefixed connectors (J1–J25, J22 absent); J2 the only
one matched by both signals.

**The pin-swap advisor itself LANDED 2026-07-27** (Opus subagent — standing
authorization per the 2026-07-24 user decision and the plan's own
delegation-strategy note that this rides the router core; worktree-isolated;
coordinator did a full independent code read-through plus its own
from-scratch test runs given the safety-criticality of this feature).
Implemented in `kicad_optimizer_tool.py` as a **seventh "move" this tool can
never apply** — categorically different from the other six, which are all
copper moves promotable by `_commit_choice`. A pin swap changes which NET
OWNS WHICH PAD, whose source of truth is the schematic; this tool never
writes the schematic or the real `.net` file, full stop.

**How a swap is priced without touching the real netlist:** on a disposable
trial copy (the same scratch pattern every other move uses), the swap is made
REAL rather than simulated — the two pads' own `(net ...)` s-expressions in
the trial board, and the matching `(node ...)` blocks in the trial `.net`
copy, are swapped verbatim (format-preserving text-span exchange, not a
rewrite) via new trial-only helpers (`_trial_swap_pad_nets` and its s-expr
span-finding support). This keeps `route_nets`'s `_self_check` clearance
model, `get_ratsnest`, and `get_trace_cost` all operating on genuinely
coherent data — a deliberate rejection of the alternative (synthetic
`route_nets(connections=...)` endpoints impersonating a pad that net doesn't
actually own), which would have broken self-check's trust that routed copper
belongs to the net whose pad it touches.

**Controlled A/B, not "current score vs swapped score":** each candidate pair
is priced on two sibling trials that differ in exactly one respect — both
strip the two nets' copper (even hand-routed copper, safe only because
neither trial is ever promoted — see below) and reroute them as-is
(`baseline`) or with the two pads' nets traded (`swap`); `gain = baseline -
swap` is therefore attributable to the swap alone, not to rerouting noise. A
cheap airline-distance estimate (`_pin_swap_pairs`) ranks all candidate pairs
first so only the most promising few (`_MAX_PIN_SWAP_TRIALS`, default 2) ever
reach a full two-trial reroute — a real connector's full pair count would
make pricing everything impractical.

**The pause is MANDATORY, not `ai_decisions`-gated:** `_pin_swap_gate` runs
once per iteration, before any copper candidate is generated, and — unlike
every other decision type — is not filtered by `min_score_spread`,
`max_pauses_per_run`, or the `decision_types` allowlist (`"pin_swap"` is
deliberately absent from that allowlist for this reason). A swap clearing
`pin_swap.min_gain` always escalates, because "clear winner" and "cannot be
applied by this tool" are simultaneously true. Sub-threshold swaps are
recorded in `pin_swap_reports` (visible) but never proposed as a decision.
`pin_swap.enabled` (default `false`) gates the whole feature and is checked
before a single unit of RNG is consumed, so a session that never touches this
knob is provably byte-identical to a pre-7.14 session.

**Re-sync after the human answers** (`_resync_pad_nets`): answering `opt2`
("I made the change") diffs the REAL board's current pad-net map against the
scratch's and adopts (never decides) the real board's assignment onto the
scratch, pad by pad, for ANY divergence found — not just the one pair
proposed, so a second manual edit the user made isn't silently ignored. Only
autorouter-owned copper on affected nets is rerouted (through `unroute_nets`,
whose ownership guard is never bypassed); hand copper on an affected net is
left alone and reported in `hand_copper_nets` for the user to redo. Answering
`opt2` without actually having made the change is harmless — the diff finds
nothing and says so. A pad-level staleness check (`_netlist_pad_mismatches`)
is used here rather than the existing name-set staleness guards in
`detect_buses`/`classify_critical_nets`, because a pin swap changes no net
NAME at all — only which pad a name sits on — which a name-set comparison is
structurally blind to.

**Defense in depth:** `_apply_session` gained a hard safety gate — before ANY
`write=True`, the scratch board's pad-net map is compared against the real
board's, and the write is refused outright on any divergence at all, for any
reason, not just one this feature could plausibly cause. This asserts the
"never silently change which net a pad belongs to" property directly rather
than trusting that no code path ever promotes a swap trial into the scratch.

92 tools (unchanged — no new MCP tool; `optimize_kicad_board` gained a
`pin_swap_exclusions` parameter; `decide_kicad_route` gained a dedicated
answer path for `decision_type: "pin_swap"` that commits nothing). 14 new
tests in `tests/test_pin_swap.py`. Full suite 303→317 passed, same 7
pre-existing board-drift failures, 7 skipped.

Knobs `pin_swap`: `{enabled: false, min_gain: 25.0, ref_prefixes:
["J","P","CN","X"]}` — unchanged from the already-landed detection schema, no
new keys needed.

### 7.15 — LANDED 2026-07-27 (reference anchor; no work remains here)

Implemented in `kicad_optimizer_tool.py` (Sonnet subagent, worktree-isolated,
coordinator-reviewed, merged fast-forward). `optimizer.effort`
(`"quick"|"balanced"|"best"`) bundles the other optimizer knobs via
`_EFFORT_PRESETS` + `_resolve_effort_knobs`, with a three-deep precedence:
explicit call-time argument > effort preset (quick/best only) > bare
`optimizer.*` config value (what `"balanced"`, the default, always resolves
to — verbatim pre-7.15 behavior for any project that never touches `effort`).
`quick` = `max_iterations: 5`, `accept: "greedy"`; `best` = `accept: "sa"`,
`time_budget_s`: 8 hours ("overnight," a ceiling a session still checkpoints
through and can converge or be stopped well before, not a promise to run
that long).

**Honest scope-down:** the plan's "replicas 1"/"replicas max" language for
quick/best refers to `autorouter.cpu.replicas`, which is schema-only —
nothing in this codebase reads it yet (confirmed by grep) — so neither preset
sets it; inventing wiring for a knob nothing consumes was correctly declined.

The plateau rule (`_plateau_check`) runs alongside, not instead of, the
existing `convergence_delta` floor: tracks `productive_improvements` (accepted
moves that genuinely lowered the score — an SA-accepted worse move is
excluded, same as a rejected move); reference rate = mean of the first
`plateau_window` such moves; converges when the trailing-window mean falls
below `plateau_slope_ratio × reference`. A session with fewer than
`plateau_window` productive moves so far cannot fire the rule at all (nothing
to compare against yet) — `convergence_delta` remains the only thing that can
stop it early in that phase, exactly as specced. `stop_reason` distinguishes
`"convergence_delta"` from `"plateau"`; both rates are reported on every
session via `get_kicad_route_session`, not just at the moment of stopping, so
"why did it stop" (or "how close is it") is inspectable mid-run.

New knobs in `DEFAULT_PCB_SETTINGS["optimizer"]`: `"effort": "balanced"`,
`"plateau_window": 3`, `"plateau_slope_ratio": 0.1`. 92 tools (unchanged — no
new MCP tool; `optimize_kicad_board` gained the `effort` parameter plus new
session-state fields). 10 new tests in `tests/test_optimizer.py` (47 total in
that file). Full suite 293→303 passed, same 7 pre-existing board-drift
failures, 7 skipped.

**Not built (out of the original spec's scope, never claimed otherwise):**
the "session asks the user via AskUserQuestion at start" UX — that is a
session/client-side interaction convention, not something this Python
function can perform; `effort` is exposed as a plain parameter for a calling
session to surface however it chooses.

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

## Phase 7.18 — Multi-layer plane fill & via-mediated connectivity — LANDED 2026-07-28 (anchor)

(Opus subagent, worktree-isolated, coordinator-reviewed: read the merge
conflict resolution directly and ran the full suite independently on the
integrated tree — see below for why integration was nontrivial.)

**7.18.1 Multi-layer attachment choice.** `_build_fine_cost`/
`_build_cost_arrays` now score EVERY covering plane component at a candidate
cell (not just the first found) and pick by the same cost model everything
else uses; the attachment-via surcharge is scaled by the landed component's
island factor (`attachment_via_cost × factor`) instead of a flat charge
regardless of what it lands on — that scaling is what actually changes
kiln's decisions (the min-vs-first part alone was nearly a no-op on this
board). **Deviation from spec, justified:** landed behind a new
`plane.multilayer_attachment_choice` knob (default `false`), not knob-free as
originally scoped — the parity requirement ("untuned project byte-identical
to before") and the fact that this change provably moves geometry can only
both hold behind a flag, same treatment 7.3d gave `pad_escape_direction_
aware`. Measured on kiln: real ranking decisions exist (2,984 of 9,964
multi-layer-covered points on GND_Main have different island factors across
layers); flag ON changes emitted copper (11 blocks vs 8 on a 4-net probe) and
trades ~10 points of board score for landing vias on healthier copper — the
Phase 6 score doesn't price plane health, so this is a real, honest,
opt-in trade-off, not a strict improvement.

**7.18.2 Cross-layer fill continuity audit.** `audit_kicad_plane_islands`
gained `cross_layer` + `summary.weakly_coupled_layer_pairs`: per net owning
fill on multiple copper layers, each stack-adjacent layer pair reports
`bonding_via_count` (same-net vias whose electrical span covers both layers
AND lands in real fill on both), `bonding_pad_count`, and a `weakly_coupled`
flag below `island_min_attachments_warn`. Read-only, additive — no new
writer (`run_kicad_stitching_pass` already fixes what this flags). Kiln
reports **zero** weakly-coupled pairs (GND_Main 251 bonding vias/pair,
GND_Safty 109, 12v_Safty 5) — the gap is now visible, this board just
doesn't have one.

**7.18.3 Return-path-aware via placement for signal nets.** New
`plane.return_path_bonus` weight (default `0.0` — byte-identical parity
proven by digest match against pre-7.18 routing) discounts a signal net's
via cost when it lands within `stitching.near_high_speed_mm` of its own
reference plane on a stack-adjacent layer. **"The net's own reference
plane"** (left open by the original spec) is resolved by pad-vote: candidates
are power-kind fill-owning nets (preferring `gnd_token` names), winner is
whichever candidate's fill covers the most of the signal net's own pads —
deliberately not a "biggest ground pour" heuristic, since that would
mis-reference every net in kiln's isolated `/SaftyProcessor/` ground domain.
The 2026-07-24 REQUIRED CONSTRAINT (signal nets never treated as a routable
plane) is untouched — this only discounts VIA PLACEMENT cost, never routes a
signal net through fill. Measured: 192/222 signal nets resolve a reference
(115 MainControler → GND_Main, 77 SaftyProcessor → GND_Safty, exactly along
the schematic sheets); of the board's 167 existing hand-routed signal vias,
153 (97%) already land where the term would discount them — validating the
term encodes real good practice, not an arbitrary preference.

**Integration note (why this merge was nontrivial):** this delegation's
worktree was created before the M5 windowing/GPU tier (see its anchor) was
merged into `main`, and both pieces of work modified the exact same hot
functions (`fine_wavefront`, `_build_cost_arrays`, `_fine_search` and its call
sites). The subagent rebased/merged `main` in and resolved five conflicts by
combining both sides (never picking one) — notably, M5's brand-new
`_route_wide_lazy` tier auto-merged WITHOUT a conflict marker but would have
silently used the wrong (pre-7.18) cost model; this was caught and fixed
explicitly so a connection rescued by that tier prices identically to a
ladder-routed one. Three-backend (cpu/numpy/gpu) parity holds with each new
flag on and off. Full suite: 365→398 passed (33 new tests), same 7
pre-existing board-drift failures, confirmed by the coordinator's own
independent run on the final integrated tree.

## Phase 7.19 — Lightweight route cost estimation — LANDED 2026-07-28 (anchor)

(Opus subagent, worktree-isolated, coordinator-reviewed: full diff read,
independent full-suite run, and independent spot-measurement of the
heuristic's effect on the real kiln board.)

**7.19.1 — `_GoalDistanceField`** (`autorouter.goal_field_heuristic`, default
`false`): a lazily-expanded backward Dijkstra wavefront over a RELAXATION of
the fine search (state collapsed to bare `(cell)`, a cell is enterable if
unblocked on ANY routable layer, every move cost floored, via moves free) —
admissible and consistent by construction, since every relaxation adds edges
and never raises a cost. Replaces plain octile distance as the fine A*'s
heuristic. **Deliberate deviation from the original sketch, verified
necessary:** NOT built on 7.3a's coarse `_CoarseModel` capacity/congestion
map as originally proposed — that model is a capacity/congestion map, not a
fine-cost lower bound (a coarse "capacity 0" cell says nothing about fine
passability through the rest of the cell, and its congestion term adds cost
the fine model may not charge), so a field built on it can OVERSTATE true
fine cost, i.e. be inadmissible, which would silently change the returned
path. Built directly from the fine window's own obstacle model instead, so
admissibility is a property of the construction, not a hope. Byte-identical
routing is guaranteed by pinning `_fine_backtrace`'s tie-break to plain
octile regardless of which heuristic drove the search (reconstruction is a
pure function of the optimal cost field), and by draining every state at
`f <= C*` rather than stopping at the first goal pop, so the set of tight
predecessors the backtrace sees is identical for any admissible heuristic.
A second, smaller finding along the way: plain octile itself is marginally
inadmissible (it floors the whole-distance conversion once, where the true
cost is a sum of independently rounded per-move costs) — harmless for
tie-breaking, pinned as a test, and the reason the new heuristic is used
alone rather than `max(octile, field)`.

**7.19.2 — cheap candidate pre-ranking** (`autorouter.candidate_fallback`,
default off). **The plan's premise was wrong, and the subagent verified this
against the code before building anything:** detailed routing was never
trying 7.3a's ranked candidates in order — `_corridor_from_global` and
`_hier_world_waypoints` both indexed candidate `[0]` unconditionally, so
candidates 1/2 were computed by the global stage and thrown away outright.
There was no wasteful "try them all" loop to gate; there was a discarded
resource and NO fallback at all. This phase built both: a fallback tier
(retry the whole detailed-routing ladder along candidate 1's, then 2's,
corridor when candidate 0 fails outright) and `_prerank_candidates`, the
actual cheap-estimate deliverable (coarse cost + a fixed per-layer-change
constant, no grid/window/search) that decides whether a lower-ranked
candidate is worth a full windowed A* at all. A connection whose candidate 0
already succeeds never reaches any of this — byte-identical by construction.

**Measured:** full suite 398→435 passed (37 new tests), same 7 pre-existing
board-drift failures. Parity proven both by the construction argument above
and by tests. **Wall-clock, reported honestly (both by the subagent and by
the coordinator's own independent spot-check):** the field mechanism is
real and demonstrable — proving an unreachable net infeasible took 33
field-expansions vs. 2,344 full A* expansions before the legacy search gave
up (coordinator's own measurement, unroute-and-reroute on a scratch kiln
copy) — but a decisive whole-board wall-clock win on KILN SPECIFICALLY was
not observed (subagent: 485.3s off vs. 484.7s on for one full-board run;
coordinator: consistent with this on smaller spot-checks). Kiln's own
currently-unrouted set is dominated by the topologically-isolated nets M5
already diagnosed (no legal channel exists at any resolution — a bigger
search proves that faster, it does not create a route), and its
already-routed signal nets route fast enough today that search isn't the
bottleneck. The mechanism's value is real (faster failure/congestion-detour
detection, proven by construction) but this specific board doesn't showcase
a dramatic end-to-end speedup; a board with more mid-difficulty congested
routing (neither trivially easy nor topologically impossible) would show it
more.

## Phase 7.20 — Adjacent-layer parallel-trace (crosstalk) avoidance — LANDED 2026-07-28 (anchor)

(Opus subagent, worktree-isolated, coordinator-reviewed: read the cost-model
integration directly and ran the full suite independently on the merged
tree. **This closes milestone M7 in full** — all three user-requested
phases (7.18, 7.19, 7.20) are now landed.)

New `pcb_settings.json` block `crosstalk`: `{enabled: true,
adjacent_layer_penalty_per_mm: 0.0, min_spacing_mm: 0.3, min_parallel_run_mm:
2.0, same_bus_exempt: true}`. Inert by construction at the default
(`penalty_per_mm` 0.0) — `_resolve_crosstalk` returns `None` rather than a
zero-weighted payload, and both `_build_fine_cost` (cpu) and
`_build_cost_arrays` (numpy/gpu) branch on `crosstalk is None` to execute the
exact pre-7.20 arithmetic, not `+0.0`. During detailed A*, a planar move
landing within `min_spacing_mm` of a *different*, non-bus-exempt net's track
copper (this run's own placements + already-routed board copper) on a
stack-adjacent copper layer accrues `adjacent_layer_penalty_per_mm × dist_mm`
— priced in the same position/shape as the existing `off_corridor`/
`away_from_home_per_mm` terms, so the surcharge scales with run length
without the A* state needing to remember any length itself, and both
backends stay bit-identical by construction (same summand order).
`min_parallel_run_mm` is applied to the AGGRESSOR SEGMENT'S OWN LENGTH at
routing time (the only reading a per-cell term can express — the true
"how long do these two paths stay aligned" doesn't exist until the path
does); the exact overlap-length semantics live in the new read-only
`audit_kicad_crosstalk` tool, which measures real emitted geometry.

**Same-bus exemption** draws on both `confirmed_buses` (board-local JSON)
and `detect_buses` candidates, so a board that hasn't been walked through
Flow A still gets sane defaults rather than the feature being actively
harmful on first use. Failure direction is deliberate: any problem reading
either exemption source degrades to FEWER exemptions (more nets penalized),
never more — the safe direction for a crosstalk check, since a false
exemption silently hides real risk while a false penalty is merely
conservative. `audit_kicad_crosstalk` reports both `violations` and
`exempt_runs` explicitly, so a false exemption would be visible in the
output rather than invisible.

**Honest real-kiln finding:** kiln's own stack-up has **zero track-vs-track
adjacency** — F.Cu and B.Cu (its only two track layers) are three layers
apart with two plane layers (In1.Cu, In2.Cu) between them, so the term has
nothing to flag on this board's real geometry as routed. `audit_kicad_
crosstalk`'s `adjacent_layer_pairs` parameter exists for exactly this case —
a stack-up what-if ("what would this same routing cost on a 2-layer board
where F.Cu/B.Cu ARE adjacent?") against the same real geometry, which is how
the feature was actually exercised/validated on kiln. The exemption logic
itself is verified with kiln's real confirmed/detectable buses (SPI
`/MainControler/`, I2C `/MainControler/`, SPI `/SaftyProcessor/`), not only
synthetic fixtures.

New MCP tool `audit_kicad_crosstalk` (94 tools, was 92). 22 new tests in
`tests/test_crosstalk.py`. Full suite: 435→457 passed, same 7 pre-existing
board-drift failures unaffected (confirmed by the coordinator's own
independent run on the merged tree — an 8th failure the subagent saw
mid-session was confirmed a load-induced flake from too many concurrent
route calls competing for cores, not a regression, and passes cleanly in
isolation).

---

## Phase 7.21 — Via placement safety: no via-in-pad, no via-via overlap — LANDED 2026-07-29 (anchor)

**User-reported bug (real board observation): vias are landing inside pads and
overlapping other vias.** Root-caused by reading the code directly (not
guessed): the "same-net copper is free" exemption exists at THREE places that
must stay parity-mirrored —

1. `_FineWindow.obstacle_cells` (bulk build), `kicad_router_tool.py:4386`:
   `if ob.net == self.net and not ob.is_edge: return via_cells, track_cells`
2. The lazy per-cell mirror, `_lazy_build`/`_window_rejects` family starting
   `kicad_router_tool.py:4499` (must stay byte-identical to (1) per
   `tests/test_lazy_window.py`'s cell-for-cell parity assertion).
3. The final pre-write DRC gate `_self_check`, `kicad_router_tool.py:5554`:
   `if ob.net == net and not ob.is_edge: continue` — this is why the bug
   reaches the board unflagged: the one function whose docstring promises to
   "prove every proposed segment/via against ALL foreign copper" silently
   never checks a via against same-net pads or same-net vias at all.

This exemption is CORRECT for tracks (a route legitimately runs alongside/
touches its own net's existing copper — that's the whole point of "same-net
copper is free"). It is WRONG for vias specifically, in two distinct ways
that need two distinct fixes:

- **Via-in-pad**: a via must never land inside ANY footprint pad (`is_pad`),
  same-net or foreign, UNLESS the user has opted in — this is a deliberate
  manufacturing technique (filled/plated via-in-pad), not something the
  router should do implicitly. New `pcb_settings.json` flag:
  `autorouter.allow_via_in_pad` (default `false`). When `false` (default),
  pads block vias regardless of net — i.e. drop the `ob.is_pad` carve-out
  from the same-net exemption specifically for the via-radius check (tracks
  keep the existing same-net-free behavior unchanged). When `true`, restore
  today's behavior (same-net pads stay via-permeable) — an explicit opt-in,
  not a default change.
- **Via-via overlap**: two vias must never overlap, even same-net,
  UNCONDITIONALLY — no config gate. Two overlapping drilled holes is never
  physically valid, unlike via-in-pad which is a real (if niche) technique.
  This applies at all three sites above: a same-net EXISTING via (`ob.kind ==
  "pt"`, not `is_pad`) must still block new via placement even though it's
  exempted for tracks.

**Scope constraint — do not touch the track-vs-same-net-copper behavior.**
The fix must only change how the exemption applies to the VIA reach/check
(`via_cells` / the via branch of `_self_check`), never to `track_cells` / the
segment branch — same-net tracks staying permeable is intentional and
covered by existing tests (e.g. a route touching its own endpoint pad's
copper must keep working). Any test that currently exercises "same-net
track over same-net pad/via" must keep passing unchanged.

**Also required**: `_nearest_blocker` (`kicad_router_tool.py:5590`) has the
same same-net skip — decide whether a via-blocked-by-own-net-pad failure
should now report that pad as the blocker (probably yes, for a useful
`unreachable_in_window` diagnostic) rather than silently skipping it.

**LANDED 2026-07-29** (Opus subagent, worktree-isolated, coordinator-reviewed:
full diff read directly against the spec above plus an independent full-suite
run on the merged tree, not just a report review). `_same_net_blocks_via(ob,
allow_via_in_pad)` (new, `kicad_router_tool.py` ~3959) is the single source of
truth both fixes route through: a same-net PAD blocks vias unless
`allow_via_in_pad` is set; a same-net EXISTING VIA blocks vias unconditionally
(no knob). All three parity-critical sites updated (`_FineWindow.obstacle_cells`
+ its `__init__`, the lazy mirror `_lazy_build`/`_lazy_cell_blocked`/
`add_obstacle`, and `_self_check`, which gained an `allow_via_in_pad` arg and
now runs its via loop against same-net via-blockers while skipping the segment
loop for them — tracks stay untouched). Also fixed: `_prefilter_window_obstacles`
(would have silently dropped same-net via-blockers before the window ever saw
them, defeating the fix upstream) and `_nearest_blocker`. New setting
`autorouter.allow_via_in_pad: false` in `DEFAULT_PCB_SETTINGS`
(`kicad_pcb_tool.py`). Threaded as `ctx["allow_via_in_pad"]` through
`_route_one_candidate`, `_route_wide_lazy`, `_route_hierarchical`, and
`route_nets`, same picklable-bool pattern as 7.3d/7.19. 19 new tests
(`tests/test_via_placement_safety.py`), including end-to-end synthetic-board
reproductions of the exact reported bug (pre-fix: vias placed dead-centre in
both endpoint pads; a second connection's vias landing at 0.0 mm pad
distance). Full suite: 457→476 passed, same 7 pre-existing kiln board-drift
failures, unaffected (confirmed by the coordinator's own independent run on
the merged tree, not just the subagent's report).

**Judgment call on record (coordinator reviewed and accepted):**
`_nearest_blocker` does NOT merge the same-net via-blocker into the primary
ranking — the spec's "probably yes" turned out to break
`test_human_copper_is_never_ripped` (a connection's own goal pad is a
same-net via-blocker at ~0 distance by construction, so merging would bury
the real foreign-copper blocker on nearly every failure). Implemented
additively instead: the primary pick stays byte-identical to pre-7.21
(nearest FOREIGN obstacle), with a same-net via-blocker surfaced alongside
under a new `same_net_via_blocker` key, promoted to primary only when there
is no foreign obstacle at all. `_crosstalk_window_cells` and
`_feasibility_screen` were deliberately left untouched (verified track-only /
scheduling-heuristic-only, not via-relevant).

**Honest residual**: the via-on-via rule is proven at unit level (window +
`_self_check`) and behaviorally (the router steps around a pre-existing
same-net via end-to-end), but NOT covered by a test forcing two NEWLY-placed
vias to collide — congestion cost already separates them even on pre-fix
code, so such a test would have been green-on-broken; none was shipped rather
than fake one.

---

## Phase 7.22 — Bus-first direct routing pass — SPEC'd 2026-07-29, not yet started

**User directive (2026-07-29), verbatim intent:** "when routing start with the
busses in the most direct line, they can be riped up and optimized later."
Two distinct changes, both in `route_nets`'s worklist, not the global-route
(7.3a) stage:

1. **Ordering — bus nets go first.** Today `conns` is sorted purely by
   `(-priority, airline_length_mm, net)` (`kicad_router_tool.py:8124` — user
   `net_overrides.priority` only, default 0 for everyone, so bus membership
   currently has NO effect on routing order). Reuse `_crosstalk_bus_groups`
   (`kicad_router_tool.py:5852`, landed in 7.20) as the bus-membership
   source — same `confirmed_buses` (board-local JSON) + `detect_buses`
   candidates, same safe-degradation direction (a lookup failure should
   degrade to treating a net as NOT a bus member, i.e. fewer nets get the
   early slot, never silently more — the same "fail toward the conservative
   side" convention 7.20 already established for this exact data source, not
   a new invented policy). Give every bus-member net a synthetic priority
   boost ABOVE any `net_overrides.priority` value seen today (or thread it as
   a distinct primary sort key ahead of the existing one) so buses are always
   routed first, before ordinary signal/power nets, with the existing
   shortest-airline tie-break preserved within and after that group.
2. **Directness for that first pass.** While routing the bus-priority group,
   bias the detailed A* toward the most direct (least deviation from the
   straight pad-to-pad line) path rather than the board's general congestion-
   avoidance behavior — the point is to lay buses down close to their airline
   while the board is still empty, not to have them thread around copper that
   doesn't exist yet. Needs a design decision (pick one, don't invent a third
   without checking the existing knobs first): (a) reuse the existing
   `deviation_mm` trace-cost weight (Phase 6) and Phase 5 bundle-corridor
   machinery, which already biases a bus toward its own centerline — verify
   whether that alone is sufficient before adding a new knob; or (b) if not,
   add a scoped bonus/knob (following the 7.18/7.19/7.20 "inert at default"
   convention: `{}`/`None` at default must reproduce the exact pre-7.22
   arithmetic) that only applies during this first bus-priority pass.
3. **Conflicts are explicitly OK and deferred, not prevented.** The user's own
   framing — "they can be ripped up and optimized later" — means this phase
   must NOT try to make the first bus pass conflict-free with nets that
   haven't routed yet. The existing rip-up worklist (7.3b step 4) and the
   Phase 7.6 whole-board optimizer are the reconciliation mechanisms; do not
   add new speculative lookahead or conflict-avoidance logic here — that
   would defeat the "route buses directly now, fix it up later" intent and
   duplicate work 7.6 already owns.

**Sequencing note (coordinator, 2026-07-29):** spec'd but intentionally
queued behind 7.21 — both would touch `kicad_router_tool.py`'s core
`route_nets` worklist/window machinery, and 7.21 is already in flight with a
subagent as of this writing. Land and merge 7.21 first to avoid two
concurrent worktree edits colliding in the same hot functions, then take up
7.22 before Phase 7.6 (per the same 2026-07-29 priority ordering — this
directly affects what geometry Phase 7.6 will have to optimize on top of, so
it belongs before, not after, that work).

**Not yet started** — spec only.

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
    parser would close this. **Still open: (c)** the Flow B session-start
    stack-up-gate question (the tool already reports `stack_up_gate` per net).
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
