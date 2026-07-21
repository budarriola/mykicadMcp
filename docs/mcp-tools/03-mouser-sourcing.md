Group 3: Mouser Sourcing & BOM
===============================

[< Back to README.md](../../README.md)

Backed by `kicad_mouser_tool.py`. Tools that call Mouser's official Search API REQUIRE
`MOUSER_API_KEY` (set in the repo-root `.env` - copy `.env.example`); they raise a clear error
asking for one rather than falling back to scraping the product page (Mouser's bot protection
blocks a meaningful fraction of scripted page fetches). Live results are cached to disk
(`.mouser_cache.json`, gitignored) for one hour, so a repeat lookup for the same part within
that window is served from cache instead of hitting the API again. Bulk calls automatically
space out requests to stay under Mouser's free-tier per-minute call cap.

## `lookup_mouser_part`
Look up a Mouser product via the Search API and extract its manufacturer part number,
stock/lifecycle status, and type-specific electrical specs. Pass a Mouser product URL, e.g.
one found via `get_kicad_schematic_part`'s `Mouser`/`Mouser Part Number`/`Mouser Price/Stock`
properties. Detects capacitor vs. resistor vs. other: capacitors get `capacitance` +
`voltage_rating`, resistors get `resistance`, and either an MLCC capacitor or SMT resistor
additionally gets `package_size_inch` (e.g. `"0402"`). Fields that don't apply to the detected
type come back `"unsupported"`; fields that should apply but couldn't be found come back
`"unknown"`. `raw_specifications` has every spec Mouser listed, for when the field-name
mapping misses one. For more than a couple of parts, use `bulk_lookup_mouser_parts` instead.
**Args:** `url` · REQUIRES `MOUSER_API_KEY`

## `bulk_lookup_mouser_parts`
Look up Mouser data for many schematic parts in one call instead of one round trip per part -
the fast path for auditing a whole schematic. Defaults to one representative reference per
unique part from `list_kicad_schematic_parts`; pass `references` for a specific subset. Parts
with no discoverable Mouser link, or whose lookup fails, are reported under `skipped`/`errors`
rather than aborting the batch.
**Args:** `project_path`, `references` (optional) · REQUIRES `MOUSER_API_KEY`

## `list_kicad_component_mouser_urls`
Get all available Mouser URLs (primary and alternates) for a single component by reference
designator - no API calls, just reads schematic properties. Useful for spotting components
with multiple Mouser sources/alternates or stale/incorrect links; reports which property each
link came from (`Mouser`, `Mouser Part Number`, `Mouser Part Number Alt`, `Mouser Price/Stock`,
etc) and which one `find_mouser_url`'s static field-priority order currently treats as primary.
**Args:** `project_path`, `reference`

## `bulk_list_kicad_component_mouser_urls`
List all Mouser URLs for many schematic parts in one call - an audit of which parts have
alternates, which are missing Mouser links entirely, and which fields each link came from. No
API calls. Useful before `bulk_lookup_mouser_parts` to spot parts with stale or multiple links
that should be cleaned up first, and before `bulk_optimize_kicad_mouser_alternates` to see
which parts actually have more than one candidate link worth ranking.
**Args:** `project_path`, `references` (optional)

## `optimize_kicad_mouser_alternates`
Rank a component's candidate Mouser links by live stock and pricing instead of the static
field-name order `find_mouser_url` normally uses, and recommend which one to treat as primary.
Priority order: **#1** in stock for at least the board's required quantity; **#2** sold with a
qty-1 price break (over reel-only/bulk-minimum pricing), ties broken by greatest quantity in
stock; **#3** cheapest unit price at the required quantity. `quantity_needed` defaults to how
many of this part the schematic's Value+Footprint group actually places on the board.
**Args:** `project_path`, `reference`, `quantity_needed` (optional override) ·
REQUIRES `MOUSER_API_KEY`

## `bulk_optimize_kicad_mouser_alternates`
Batch version of `optimize_kicad_mouser_alternates` across the schematic's unique parts (or a
subset). Defaults to skipping parts with only one candidate Mouser link
(`only_with_alternates: true`) to spend the rate-limited API budget on parts where there's an
actual choice; set it `false` to also validate single-link parts' stock. Returns `changed` -
parts whose live-ranked recommendation differs from the static field-priority pick - as the
actionable list of parts worth re-pointing at a better link.
**Args:** `project_path`, `references` (optional), `only_with_alternates` (default true) ·
REQUIRES `MOUSER_API_KEY`

## `audit_kicad_stock_sufficiency`
Check that at least one candidate Mouser link for every unique schematic part (not just its
current primary link - every `Mouser`/`Mouser Price/Stock`/`Mouser Part Number Alt`/etc field)
is in stock for enough units to build `board_quantity` board(s), via the same live-data ranking
`optimize_kicad_mouser_alternates` uses. A part whose primary link is out of stock but has a
working alternate is **not** flagged - `insufficient` only lists parts where no candidate link
covers the need. `board_quantity` multiplies each part's own schematic-placed count; defaults
to 1 board.
**Args:** `project_path`, `references` (optional), `board_quantity` (default `1`) ·
REQUIRES `MOUSER_API_KEY`

## `generate_kicad_mouser_stock_report`
Run a bulk Mouser lookup across the schematic's unique parts and write a Markdown report with:
a BOM cost estimate for one board (using Mouser's own quantity-break pricing against how many
of each part the schematic actually uses, with unpriced parts listed separately rather than
silently excluded from the total), and every part Mouser currently shows as out of stock or
lifecycle-flagged (Not Recommended for New Designs, obsolete, discontinued, etc). Defaults to
writing `mouser_stock_report.md` at the project root.
**Args:** `project_path`, `report_path` (optional), `references` (optional) ·
REQUIRES `MOUSER_API_KEY`

## `generate_kicad_mouser_buy_list`
Build an orderable buy list across the schematic's unique parts (or a subset): the best Mouser
link per part (live stock/price ranked, same as `bulk_optimize_kicad_mouser_alternates`) and
how many units to actually buy. Buy quantity starts at the board's required quantity, then:
- parts under **$0.05/unit** get **10 extra**,
- parts under **$0.10/unit** get **5 extra**,
- and on top of that, any part is bumped further whenever a higher Mouser price-break tier's
  **total cost** is cheaper overall than the padded quantity's total cost (never bumped below
  the padded quantity).

Writes a Markdown table to `buy_list_path` (defaults to `buy_list.md` at the project root)
with links, quantities, per-line cost, the reason behind any extra units, and a grand total.
**Args:** `project_path`, `buy_list_path` (optional), `references` (optional) ·
REQUIRES `MOUSER_API_KEY`

## `audit_kicad_schematic_health`
One-call pre-fab/pre-order sanity pass across the whole schematic - the tool to reach for when
asked to "check the schematic for errors." Combines every check this project has:
1. `audit_kicad_schematic_integrity` ([02-schematic-data.md](02-schematic-data.md)) - duplicate
   reference designators, missing Value/Footprint.
2. `audit_kicad_capacitor_voltages` ([02-schematic-data.md](02-schematic-data.md)) - every
   capacitor either states its own voltage or is assumed to use `default_capacitor_voltage`.
3. `audit_kicad_component_specs` ([02-schematic-data.md](02-schematic-data.md)) - each part's
   Value/Footprint/MPN matches its linked Mouser product.
4. `audit_kicad_stock_sufficiency` (above) - at least one candidate link per part covers
   `board_quantity` board(s).

There is no universally-correct default capacitor voltage for this project - **ask the user**
what voltage rating this design assumes for capacitors that don't state one before calling
this (`Power.kicad_sch`/`Regulators.kicad_sch`'s rail voltages are a reasonable thing to bring
up in that conversation, but the actual answer is a project decision, not something to guess).
Steps 3-4 are the slow, rate-limited part of this call - a full ~40-60 unique part schematic
can take a couple of minutes. Writes a Markdown summary to `report_path` (defaults to
`schematic_health_report.md` at the project root); full structured results (every finding from
all four checks) are also returned as JSON.
**Args:** `project_path`, `default_capacitor_voltage` (required, e.g. `"16V"` or `16` - ask the
user, don't guess), `references` (optional, applies to steps 3-4), `board_quantity` (default
`1`), `value_tolerance_pct` (default `1.0`), `report_path` (optional) ·
REQUIRES `MOUSER_API_KEY`
