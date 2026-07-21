Group 2: Schematic Data & Property Maintenance
===============================================

[< Back to README.md](../../README.md)

Reading and editing the properties KiCad stores on schematic symbols (Value, Footprint,
Datasheet, Manufacturer_Name/Manufacturer_Part_Number, Mouser fields, Sim.* fields), read
straight from the `.kicad_sch` files across every sheet - preferred over trusting the
exported `kiln.csv` BOM, which can go stale relative to the live schematic.

## `list_kicad_schematic_parts`
Get the unique parts list straight from the schematic (all sheets, following every
hierarchical instance). Groups every placed symbol by Value + Footprint - the same grouping
KiCad's own BOM exporter uses - and returns one row per unique part with its quantity and
every reference designator that shares it. Start here, then pass one of a row's `references`
to `get_kicad_schematic_part` for that part's full property set.
**Args:** `project_path`

## `get_kicad_schematic_part`
Get every property KiCad stores on one placed schematic symbol by reference designator, plus
its pin list and which schematic sheet file/instance it was placed on.
**Args:** `project_path`, `reference`

## `set_kicad_schematic_property`
Set one property on a schematic symbol by reference designator - updates it in place if
already present (matched case-insensitively), otherwise inserts it as a new hidden field
styled/positioned like the symbol's existing `Datasheet` property, anchored right after it.
**Args:** `project_path`, `reference`, `property_name`, `value`, `write` (default false),
`allow_while_open` (default false)

## `audit_kicad_schematic_integrity`
Cheap, netlist-independent sanity checks across every placed schematic symbol (power symbols
excluded): duplicate reference designators (two distinct placed instances annotated with the
exact same reference - a real KiCad ERC "duplicate reference" error, not just two instances of
the same hierarchical block, which get their own distinct references), and symbols missing a
Value or Footprint altogether. Pure text/structure checks - no Mouser data or API key needed,
so this is a fast first pass before the slower Mouser-backed audits below, or before
`audit_kicad_schematic_health` ([03-mouser-sourcing.md](03-mouser-sourcing.md)).
**Args:** `project_path`

## `audit_kicad_capacitor_voltages`
Check every unique capacitor value (identified by the `C<n>` reference-designator convention)
for a voltage rating written into its Value field, e.g. `47uF 16V` vs. plain `0.1uf` - the
common convention where every cap is assumed to use one project-wide default voltage unless
its Value overrides it. Pass `default_voltage` to also split parts that state a voltage into
"redundantly restates the default" vs. "genuinely differs"; omit it to only split
has/missing a voltage indication. Can only see what's written in the Value text, so
`missing_voltage` means "assumed default," not "verified against the real part."
**Args:** `project_path`, `default_voltage` (optional, e.g. `"16V"` or `16`)

## `normalize_kicad_manufacturer_part_number_properties`
Find schematic symbols that carry a manufacturer-part-number-shaped property under some other
name (`PROD_ID`, `MPN`, `Part Number`, etc) but don't already have the project's canonical
`Manufacturer_Part_Number` property, and rename that property key to the canonical name (value
untouched). Only renames when exactly one alias candidate is present on a symbol lacking the
canonical key; symbols with more than one candidate come back under `ambiguous` instead of
being guessed at.
**Args:** `project_path`, `write` (default false), `allow_while_open` (default false)

## `audit_kicad_manufacturer_part_numbers`
Cross-check each schematic part's `Manufacturer_Part_Number` property against the manufacturer
part number Mouser's Search API actually returns for that part's own Mouser link - catches
typos, copy-paste errors, or a stale value left over from swapping which exact part a symbol
points to. Run `normalize_kicad_manufacturer_part_number_properties` (with `write: true`)
first so parts that only carry the MPN under a differently-named property get picked up here
too. REQUIRES `MOUSER_API_KEY` (cross-checks against live Mouser data - see
[03-mouser-sourcing.md](03-mouser-sourcing.md)).
**Args:** `project_path`, `references` (optional subset)

## `audit_kicad_component_specs`
Broader version of `audit_kicad_manufacturer_part_numbers` above: cross-checks a part's
Value/Footprint *and* MPN against its linked Mouser product, not just the MPN - catches a link
that resolves fine but points at the wrong part entirely (wrong resistance/capacitance, wrong
package, or a stale/typo'd MPN). This is the exact bug class behind the R96/R103 stale-link
issue noted in `todo.md` (link pointed at a 47.5kΩ part for a 154k resistor). Three independent
checks per part, each reported `"match"` / `"mismatch"` / `"not_verifiable"` (never a silent
pass when data is missing): `manufacturer_part_number` (same comparison as above), `package`
(EIA imperial size code parsed from the Footprint name vs. Mouser's `package_size_inch` - chip
resistors/ceramic capacitors only), and `value` (nominal resistance/capacitance parsed from the
Value field vs. Mouser's spec, compared numerically within `value_tolerance_pct` percent so
`"10k"` vs. `"10 kOhm"` formatting differences don't read as mismatches - resistors/capacitors
only).
**Args:** `project_path`, `references` (optional subset), `value_tolerance_pct` (default `1.0`)
· REQUIRES `MOUSER_API_KEY`
