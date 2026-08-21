# Router phase history & board findings

Completed autorouter phases and the measured board findings behind them,
moved out of `NETCLASS_PLAN.md` so that plan carries only open work. Nothing
here is pending: these sections are kept because later phases and the open
build order still cite their measurements, schemas and rejected
alternatives.

Landed-phase sections retain their original "LANDED"/"anchor" headings so
existing cross-references from the plan still resolve by title.

## Phase 7.24 — Direct-line fast path — LANDED 2026-07-30 (Sonnet subagent, worktree-isolated, coordinator independently re-ran the new test file and merged after review)

**Motivation:** the from-scratch benchmark above showed a lot of the router's
time and complexity goes into building a full grid window + running A* for
connections that are, geometrically, trivial — the sampled failures earlier
in this session were mostly 1.5–2 mm airline hops. User request: before doing
any of that, just try drawing a straight line (or a simple one-bend
Manhattan path) directly between the two pad points and see if it's already
legal — much cheaper than a grid+A* search, and should be a big win on the
common easy case.

**Design (opt-in, new tier 0, runs BEFORE the global stage / grid pipeline):**
- New flag, default `False` (same ask-every-call convention as
  `allow_hand_copper_ripup`/`allow_zone_soft_route`) — call it
  `direct_route_first`. Default off means every existing test and caller
  gets byte-identical behavior; this is a pure speed feature, not a
  correctness one, and changing the DEFAULT geometry of thousands of already-
  passing connections would break this codebase's parity guarantees across
  every prior landing.
- For each connection, before spending anything on `_choose_grid`/`_FineWindow`/
  `_fine_search`: try (a) a single same-layer straight segment between
  `from_xy`/`to_xy`, then if that fails (b) the two orientations of an
  L-shaped one-bend Manhattan path (via one intermediate corner). Test each
  candidate with the SAME exact-clearance self-check every other tier uses
  (`_self_check` against the real hard obstacle set — pads/tracks/vias/edges/
  zones at full netclass clearance; no via-transparency or zone-soft leniency
  here, this tier is about SPEED on already-legal geometry, not about finding
  new corridors). No grid, no A*, no window build — just geometric distance
  tests, so this should be one to two orders of magnitude cheaper per
  connection than the existing pipeline.
- On a pass, accept immediately — same emit path as any other tier, this
  connection never touches the rest of `_route_attempts`. On failure (direct
  line and both L-bends all collide with something), fall through to the
  existing pipeline completely unchanged — this tier only ever short-circuits
  the EASY case, it never gives up on a connection the existing pipeline
  would have solved.
- Cross-layer connections (different `home_layer` between endpoints) are out
  of scope for the straight-line/L-bend geometry itself (no via placement
  logic here) — skip tier 0 for those and fall through immediately, don't
  try to invent a via-drop heuristic in the same pass.

**Correctness gates (same bar as every other tier):** default off is
byte-identical (parity test); an accepted direct/L-bend route must pass the
exact same `_self_check` clearance test as any other emitted copper (no
special-cased leniency — if it wouldn't pass the existing self-check, it
doesn't get accepted here either); determinism across worker counts; full
suite stays green. This tier is pure speed/ordering, so unlike 7.23 there is
no need for a kicad-cli round-trip — a straight legal trace at full clearance
is just as legal as one the grid pipeline would have found, nothing about it
is speculative.

**Landed as designed**, no deviations from spec. New opt-in flag
`direct_route_first` (default `False`) threaded through `route_nets`/
`route_kicad_nets` and `route_board`/`route_kicad_board` (signatures,
docstrings, `ctx` dict, CLI `--direct-route-first`, both MCP schemas/
handlers, `_cli_print_route_report`). Core: `_direct_route_layer` (picks the
tier's single layer, or `None` to skip cross-layer connections entirely) and
`_route_direct_first` (the 3 ordered candidates — straight, then both L-bend
Manhattan corners — each proven against the same `_self_check` every other
tier uses), hooked into `_route_one_candidate` before any grid/window code
runs. Reports `direct_route_first_count` in the result summary. 0 new MCP
tools. 13 new tests (`tests/test_direct_route.py`): open-straight accept,
blocked-straight/open-L-bend accept, fully-blocked falls through identical
to flag-off, cross-layer skips the tier entirely, `_direct_route_layer` unit
tests, parity (flag off never calls the tier, byte-identical to explicit
`False`), determinism across `workers=1` vs `4`, and CLI/MCP plumbing.
Suite 529→542 passed (both counts independently confirmed from completed
run output, not assumed), same 7 pre-existing real-kiln-board-drift
failures, 9 skipped — zero regressions. Coordinator independently re-ran
the new test file (13/13 passed) before merging (`e6dd63e`, fast-forward).

**Process note:** an early background test run in the implementing
subagent's worktree was flagged mid-session as possibly deadlocked (an
8-second CPU-time sample showed no progress) and its process was killed:
the subagent's own follow-up run completed normally, so that read was most
likely a false positive from sampling during pytest-xdist's slower startup/
collection phase rather than a real hang — worth remembering before killing
a long-running test process on this suite again (it legitimately takes
place 800s+ single pass).

---

## ⭐ From-scratch full-board benchmark on `kilnCtl_AutoRouteTest` (2026-07-30)

A separate scratch clone of the project (`kilnCtl_AutoRouteTest`, sibling
directory to this repo, same commit history) exists specifically as a
from-scratch routing testbed: placement and all six zone pours are present
but almost nothing is routed yet (`get_kicad_ratsnest` at the start of this
session: 278 unrouted connections / 139 unrouted nets / 36 fully-routed nets
— contrast with the real `kiln.kicad_pcb`, which the 2026-07-27 audit found
had only 1 unrouted connection left after the user's own hand-routing).
This is a materially different regime than everything the routing-capability
arc above was tuned and measured against.

**Result: `route_kicad_board(write=True, effort=best)` routed only 5 of 278
connections (3.677 mm copper, 3 vias).** Tried both `balanced` and `best`
effort, before and after a `kicad-cli pcb drc --refill-zones --save-board`
refresh of the zone fills (in case stale fills were the cause) — none of
these moved the needle (5-6/278 every time; effort tunes rip-up
aggressiveness, and there was almost nothing rippable since almost nothing
routed on the first pass). This was NOT the `window_too_large` /
`unreachable_in_window`-from-budget-limits problem the 2026-07-24 findings
above describe and already fixed (adaptive grid, zone-distance perf, the
finer-grid ladder, plane-via anti-pads) — those fixes are all still active
and did not help here.

**Root cause, sampled directly from the failure records (`kicad_router_tool.py`
self-check / A* obstacle model, not a new bug):** the overwhelming majority of
failures are short (1.5–2 mm airline) SIGNAL-net hops whose pads sit
immediately adjacent to a filled GND/power zone with **zero rippable
obstacle in the way** —
- `unreachable_in_window` (464→468 of the failures): `nearest_blocker` is a
  zone (e.g. `GND_Main` on F.Cu) at `distance_mm: 0.0`, at every grid
  resolution down to 0.05 mm, within the full 60 mm window margin.
- `self_check_failed` (78–82 of the failures): A* does find a path, but it
  skims the zone closer than the net class clearance (0.3 mm); the violation
  record has `"against_kind": "zone"`, `"owner": null` — i.e. NOT rip-up
  eligible (rip-up demotion, phase 9's item, only ever applies to
  autorouter-owned or hand-routed TRACK/VIA copper, never to a zone
  polygon — zones can only be *reshaped* via `propose_kicad_plane` /
  `modify_kicad_plane`, and only for autorouter-owned zones, never the six
  hand-made kiln zones).

In other words: on a board with no pre-existing hand-routed corridors, most
signal pads under/beside a filled plane have no legal escape channel at
all, and nothing in the pipeline (adaptive grid, hierarchical windowing,
rip-up, plane-via anti-pads) can create one, because none of those tools
edit a zone's own polygon. This is exactly the gap the 2026-07-27 real-board
audit flagged as "a structurally different, bigger feature (zone-shape
editing, not track/via rip-up) ... not started" for the single remaining
`U6-BIAS` net on the real board — this session's from-scratch run shows
that gap is the DOMINANT failure mode (not a rare residual) whenever a
board hasn't already had its corridors carved out by hand. **Confirmed
harmless changes kept on `kilnCtl_AutoRouteTest`:** the zone refill (fresh,
authoritative fills; `kiln.kicad_pcb.prerefill.bak` left alongside as a
restore point) and the 5-connection `write=True` route both landed with
**zero new DRC violations** (kicad-cli reported 27 violations before and
after, unconnected-item count dropped 276→275→ further after the route).

**If pursued, the shape of the fix** would be a new capability — call it
zone keepout carving / auto-notching: when a signal net's only legal path
requires touching a foreign plane's territory, cut a small clearance notch
into that plane's polygon (bounded, reversible via the zone's own
`autorouter_owned` tracking the same way `create_kicad_plane`/
`modify_kicad_plane` already do) rather than only ever treating the fill as
a hard obstacle. This is scoped as a NEW item, not a fix to any existing
phase; per the plan's standing rule, do not start it without the user's
go-ahead.

## Phase 7.23 — Zone-territory soft routing + kicad-cli-verified refill — LANDED 2026-07-30 (Opus subagent, worktree-isolated, coordinator independently re-ran both the new test file and the full suite before merging)

**Problem this fixes** (see the finding above): most `unreachable_in_window` /
`self_check_failed` failures on a from-scratch board are a signal-net pad
with a filled foreign GND/power zone at `distance_mm: 0.0` and `owner: null`
— nothing rippable, because the *zone's own fill polygon* is the obstacle,
not any track/via. Real KiCad workflow doesn't have this problem: when you
draw a trace and refill, the fill algorithm recomputes clearance around
**every** copper item present at fill time, including the trace you just
drew — the pour was never a fixed shape the trace has to dodge, it is
supposed to yield. Our router has been treating a zone's *stale, pre-trace*
fill as a permanent hard obstacle, which is backwards for this case (it is
correctly a hard obstacle for a normal `unreachable_in_window` search against
*other tracks/vias/pads*, which is why nothing here changes for those).

**Design — do not hand-roll KiCad's fill/clearance algorithm; use kicad-cli as
the authority (the ENABLER already proven 2026-07-24), the same way
`refill_zones_with_kicad` already does for plane-via writes:**

1. New **last-resort tier**, gated strictly behind full `_route_attempts`
   ladder + hierarchical-tier exhaustion (same gating discipline as Phase
   7.13's hierarchical tier — never engages for a connection that already
   routes today, so default-off behavior is byte-identical). Only fires when
   every remaining obstacle in the failure is a **foreign zone fill**
   (`against_kind == "zone"`, `owner is None`) — any hard obstacle (pad,
   track, via, Edge.Cuts) still fails normally, no change.
2. For those connections, re-run the window search with that specific zone's
   fill polygon treated as *non-blocking* for this net (still keep the
   zone's own **outline** as courtesy-only info, not a hard bound — KiCad's
   refill works within the outline anyway) — same clearance/self-check logic
   otherwise, so the candidate still must clear all *other* real copper.
3. Collect every such speculative candidate across the whole `route_board`
   call; do **NOT** trust the in-Python self-check for the zone-clearance
   term. Instead, on a **scratch copy** of the board: write all candidates,
   run `refill_zones_with_kicad` (already implemented,
   `kicad_router_tool.py:9480`), then read back the refilled zone polygons
   and confirm each candidate's copper actually has clearance from the new
   fill (a cheap geometry check now that the fill is authoritative, not
   estimated).
4. Only candidates that pass step 3 are eligible to write to the real board;
   any that don't are rejected and stay `failed` (reason
   `zone_soft_route_rejected`, with the real clearance gap reported) — never
   silently accepted on the Python-only self-check.
5. New per-call opt-in flag, default `False`: `allow_zone_soft_route` (same
   convention as `allow_hand_copper_ripup` — this is materially more
   speculative than ordinary routing, so ask every call, not a persisted
   setting). Reports `zone_soft_routed` (uuids/nets) on the result, the same
   audit-before-`write=True` pattern the hand-copper-ripup flag uses. Auto-
   skips (reports `reason: "kicad-cli not found"`) when kicad-cli isn't on
   the machine, exactly like `refill_zones_with_kicad` already does.

**Correctness gates (non-negotiable, same as every prior phase):** flag-off
is byte-identical to today (proven by a parity test); a connection this tier
accepts must show **zero new DRC violations** on a real `kicad-cli pcb drc`
run of the resulting board (measured, not assumed); determinism across
worker counts; full existing suite stays green. **Do not** attempt to model
KiCad's thermal-relief/clearance fill math in pure Python as an alternative —
that was tried implicitly by the existing estimated-fill fallback and is
exactly the source of this bug; kicad-cli is the ground truth here.

**Landed as designed**, with a few deviations the implementer noted (all more
conservative than the spec, none loosening a correctness gate): the tier runs
after the whole-board lazy tier too (strictly later, not just after the
hierarchical tier); eligibility is decided by re-checking the candidate's
copper against the full obstacle set rather than trusting the failure
record's single `nearest_blocker` (no reliable full blocker list exists on a
failure record); verification deliberately does NOT apply the `_self_check`
`via_transparent` anti-pad exemption (a real post-refill anti-pad is real
geometry, exempting it would assume the thing being proven); a write
containing any zone-soft copper always refills the real board regardless of
the `refill_zones` flag (the whole premise is that the fill must catch up).
New per-call opt-in flag `allow_zone_soft_route` (default `False`, same
ask-every-call convention as `allow_hand_copper_ripup`) on both
`route_nets`/`route_kicad_nets` and `route_board`/`route_kicad_board` (CLI +
MCP schemas); reports `zone_soft_routed` (accepted, audit shape mirrors
`human_copper_ripped`) and `zone_soft_route` (attempt/accept/reject
accounting) on the result; refusals stay `failed` with
`reason: "zone_soft_route_rejected"` and the measured `clearance_gap_mm`.
0 new MCP tools (params added to two existing tools). 18 new tests
(`tests/test_zone_soft_route.py`, synthetic fixtures, incl. a real
kicad-cli DRC-delta test proving zero new violations on accepted copper, and
three parity tests proving flag-off is byte-identical, incl. one that
monkeypatches the new tier to raise so the default path provably never
reaches it). Suite 513→531 passed, same 7 pre-existing real-kiln-board-drift
failures (zone/ratsnest counts), 7 skipped — both the new file and the full
suite independently re-run by the coordinator, not just taken on the
subagent's word.

**Residuals, honestly recorded:** verification measures at netclass
clearance, so a zone configured with a tighter clearance override could see
a candidate rejected that KiCad's own DRC would actually pass — conservative
direction only (false-reject, never false-accept). Candidates are only
produced for `candidate_index == 0`, so an alternate 7.19.2 candidate-
fallback path never gets one. Up to 6 kicad-cli refill rounds
(`_ZONE_SOFT_MAX_VERIFY_ROUNDS`) when candidates keep rejecting each other;
non-convergence rejects rather than accepts.

**Not yet re-benchmarked against `kilnCtl_AutoRouteTest`** (the from-scratch
board that surfaced this bug) with the new flag turned on — that's the
natural next verification step if the user wants to see how much of the
273/278-unrouted gap this closes, but has not been run yet.

---

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

## Phase 7.22 — Bus-first direct routing pass — LANDED 2026-07-29 (anchor)

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

**LANDED 2026-07-29** (Opus subagent, worktree-isolated, branched after 7.21
merged; coordinator-reviewed: full diff read directly plus an independent
full-suite run on the merged tree). Ordering: `autorouter.bus_first` (default
**True** — this IS the deliverable, per the user's directive) sorts every
bus-member net (from `_bus_member_nets`, flattening the SAME
`_crosstalk_bus_groups` resolution 7.20 already computes — resolved once and
shared with the crosstalk block, so the worklist and the cost model can
never disagree about what a bus is) strictly before every non-member net,
with the existing `(-priority, airline_length_mm, net)` key preserved
verbatim as the tie-break inside and after that group. Self-inerting: a
bus-less board has an empty member set and takes the literal pre-7.22 sort.

**Directness — the investigation resolved the spec's open design question
decisively, not just "picked one":** option (a) is not merely insufficient,
it is structurally impossible — `deviation_mm` (Phase 6) has **zero**
references anywhere in `kicad_router_tool.py`; it exists only as a post-hoc
scoring metric in `kicad_pcb_tool.py`'s `get_trace_cost`, never something the
router's A* is steered by. The actual empty-board bowing comes from
`_direction_factor`'s `off_direction` term (against-axis runs cost 2×, 45°
diagonals are always neutral) — measured on a bus fixture with NO copper on
the board at all, a 12.0 mm airline routed as 16.971 mm (exactly 12·√2).
Landed as option (b): `_straight_line_corridor` (new) + `autorouter.
bus_first_direct_corridor_mm` (default **0.0**, off) — a corridor around the
straight pad-to-pad line, reusing the existing `off_corridor` weight (no new
cost term, `_build_fine_cost`/`_build_cost_arrays`/numpy/GPU tiers
untouched), swapped in at the FIRST routing attempt only (`use_corridor`
already scopes this — every rip-up re-route runs corridor-free, so a
ripped-up bus net re-routes normally, not pinned back to its airline).
Measured at `bus_first_direct_corridor_mm: 0.6` on a 1-destination fixture:
58.148 mm/4 vias → exactly 55.000 mm/0 vias, and faster (1.36s → 0.48s).

**Honest tradeoff on record (why the directness knob ships OFF, unlike
ordering):** on a denser 3-destination fixture the same knob improves
geometry (ratio 1.1082 → 1.0547, vias 18 → 8) but 3 of 12 connections stop
routing (`unreachable_in_window`, blocked by another bus net's now-dead-
straight copper ~1.0 mm away — a `same_net_via_blocker` against its own pad,
correctly refused per the just-landed 7.21), rip-up spent 3 iterations/7
rips without recovering them, and runtime went 21s → 214s. This is exactly
the deferred-conflict tradeoff the user's directive accepted ("ripped up and
optimized later" implies Phase 7.6 is the eventual reconciler, not yet
built) — the subagent deliberately did not add anything to mitigate it, since
that would be 7.6's job, not 7.22's. Reassess whether to flip this knob's
default once Phase 7.6 exists.

**Point 3 verified directly, not just claimed:** `_straight_line_corridor`
is a pure function of the connection's own two endpoints only — no obstacle
list, no placement state, no congestion field, no reservation — and
`test_direct_corridor_ignores_already_placed_copper` asserts this by
scanning the function body. No conflict-avoidance/lookahead logic exists
anywhere in the landed diff.

**Honest residual:** `_route_hierarchical` (the rare last-resort tier) is
deliberately not wired for the directness knob, mirroring 7.12's neck-down
precedent — a bus net that only routes via that fallback lands on the coarse
global-stage path, not its airline.

20 new tests (`tests/test_bus_first_routing.py`): both tie-break directions,
both bus-membership sources, and inertness proven as a full byte-identical
geometry digest (not just net order) when there are no detected/confirmed
buses, plus a monkeypatch proving `_straight_line_corridor` is never even
called at the shipped default. Full suite: 481→501 passed, same 7
pre-existing kiln board-drift failures, unaffected (confirmed by the
coordinator's own independent run on the merged tree).

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

**Deliberately DEFERRED 2026-07-30 (user decision):** this item was designed
to share its decision/recording mechanism with Phase 7.13's missing-profile
case, but 7.13 (impedance-matched traces) is itself explicitly deprioritized
and not started — building 17(c) standalone now would mean inventing a
throwaway mechanism now and a real one later, or coupling this item to
machinery that doesn't exist. Coordinator asked; user chose to leave this
fully deferred until 7.13 is actually taken up, rather than build a
standalone minimal gate ahead of it. No code work should be attempted here
without the user raising it again (ideally alongside 7.13 itself).

---



---

## Phase 7 landed subsections

Moved out of `NETCLASS_PLAN.md`'s Phase 7 for the same reason as the
sections above: no work remains in them, but later phases cite their
schemas and measurements.

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

**Follow-up LANDED 2026-07-30** (Opus subagent, worktree-isolated,
coordinator-reviewed: full diff read directly plus an independent full-suite
run on the merged tree — user request, coordinator-clarified after correcting
an earlier mis-scoped priority question, since 7.6 itself was already done
and this was the actual open item near it). `route_board` gained `optimize:
bool = False`. Left off (the default), the call is verbatim pre-wiring —
`test_route_board_pipeline_hooks_declared_not_faked`'s default-path assertion
passes unchanged and was extended, not loosened, to also cover the
`optimize=True` path. When `optimize=True`, `_run_optimizer_stage` starts a
fresh `optimize_board` session AFTER detailed routing (and after the write,
if any) and drives it chunk-by-chunk (`_ROUTE_BOARD_OPTIMIZE_CHUNK=5`) until
non-`running`; `awaiting_decision` (a 7.7 AI near-tie or a MANDATORY 7.14
pin-swap pause) STOPS the loop rather than resuming (a plain resume
auto-defers the pending decision — the loop's `while` condition structurally
excludes that state, and the write step below is separately gated on only
`("converged", "budget_exhausted")`), surfacing `pending_decision` under the
report's new `optimizer` key and leaving the session open for
`get_kicad_route_session`/`decide_kicad_route`. `effort` passes straight
through to `optimize_board`'s identically-named preset.

**Judgment call on record:** `effort="best"`'s 8-hour `time_budget_s` (7.15)
is capped at `_ROUTE_BOARD_OPTIMIZE_TIME_CAP_S = 900.0` (15 min) for a
`route_board` call specifically — reasoning written in-code next to the
constant: `optimize_kicad_board` is chunked/resumable so 8 hours is fine
there, but `route_board` is one synchronous call every caller (MCP tool,
CLI, `benchmark_autoroute`'s timed loop) would otherwise block on for a
working day. The cap only ever LOWERS a budget (`min(resolved, cap)`) —
`quick`/`balanced` (300s default) are unaffected — and a capped run says so
explicitly in `notes`, naming the session id so the full-length run can be
resumed directly via `optimize_kicad_board`. Bounded-and-loud, not
unbounded-and-silent.

`pipeline["whole_board_optimization"]` stays the exact pinned
`"not_implemented (Phase 7.6, M4)"` string when `optimize` is omitted/False;
otherwise `_optimizer_pipeline_status` distinguishes `"done: converged
(...)"` / `"done: budget_exhausted (...)"` / `"paused: awaiting_decision
(...)"` explicitly (never a single generic "done"). MCP schema
(`route_kicad_board`'s `optimize` param) and CLI (`--optimize` on `route`)
both updated to match; `docs/mcp-tools/11-autorouter.md` updated (whole-board
optimization removed from the "NOT YET IMPLEMENTED" list; stitching
remains). 12 new tests (`tests/test_route_board_optimize.py`), including
fixtures pinning that `awaiting_decision` is never silently auto-resolved
(reusing 7.7's `min_score_spread: 1e9` pause harness) and that `write=False`
leaves the real board byte-identical. Full suite: 501→513 passed, same 7
pre-existing kiln board-drift failures, unaffected (confirmed by the
coordinator's own independent run on the merged tree).

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

