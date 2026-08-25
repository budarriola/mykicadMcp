"""Taxonomy for the KiCad MCP server's search facade: groups, synonyms, recipes.

Same shape as ``kilnctrl.mcp_facade`` and ``kilnsim.mcp_facade`` -- see either
module's header for why this table lives beside the server instead of inside
it. The vocabulary here is different again: kilnctrl and kilnsim each answer
to one running board, but this server answers to a KiCad *project* -- a
schematic, a PCB file, and (for the ``_live_`` tools) a KiCad GUI process that
may or may not currently be open. That third state is why ``get_kicad_ipc_status``
earns a place in ``KEEP`` alongside ``inspect_kicad_project``: it is the one
call that tells the caller whether the live-editor tools are even reachable
before it wastes a round trip finding out the hard way.

Unlike the other two servers, almost none of this tool set groups itself by a
simple leading prefix -- the naming convention is ``verb_kicad_noun`` (e.g.
``list_kicad_components``, ``audit_kicad_capacitor_voltages``), so the verb
that varies sits *before* the part of the name that says what group a tool
belongs in. ``GROUP_PREFIXES`` still catches the cases where verb+noun happen
to share a stable prefix (``bulk_`` is always a Mouser tool; ``detect_kicad_``
is always a bus/netclass tool), but the bulk of the mapping had to move into
``GROUP_OVERRIDES`` on purpose -- that table is not a sign the prefix scheme
failed, it is what this naming convention requires.
"""

from __future__ import annotations

#: Longest match wins. Most of these prefixes exist because a whole verb+noun
#: family happens to share one (every "bulk_" tool is a Mouser tool; every
#: "detect_kicad_" tool is a bus/netclass tool) -- not because the naming
#: convention groups cleanly in general. See GROUP_OVERRIDES for the rest.
GROUP_PREFIXES = (
    # Schematic-health and sourcing audits share the "audit_kicad_" spelling;
    # the four that are really about Mouser data (not schematic correctness)
    # are pulled back out below.
    ("audit_kicad_", "audit"),
    # Every "bulk_*" tool exists to save Mouser round trips.
    ("bulk_", "mouser"),
    ("generate_kicad_mouser_", "mouser"),
    # Bus/connector/critical-net classification -- the netclass workflow's
    # read side.
    ("detect_kicad_", "netclass"),
    # Live-KiCad-GUI tools, split across two verb spellings.
    ("get_kicad_live_", "live"),
    ("highlight_kicad_live_", "live"),
    ("clear_kicad_live_", "live"),
    # Template diff/apply pairs. Route (copper) is kept separate from layout
    # (position/rotation/label-offset) even though both use the same
    # diff/apply verb pair, because "clone this part's placement" and "clone
    # this part's routing" are different questions with different risk (the
    # route ones create new copper).
    ("diff_kicad_route_", "route"),
    ("apply_kicad_route_", "route"),
    ("diff_kicad_flip_", "layout"),
    ("apply_kicad_flip_", "layout"),
    ("diff_kicad_layout_", "layout"),
    ("apply_kicad_layout_", "layout"),
    ("diff_kicad_property_position_", "layout"),
    ("apply_kicad_property_position_", "layout"),
    ("align_kicad_", "layout"),
    # Schematic-symbol data and edits (not the exported BOM, not the PCB).
    ("get_kicad_schematic_", "schematic"),
    ("set_kicad_schematic_property", "schematic"),
    ("list_kicad_schematic_parts", "schematic"),
    # Component/pin/net lookups -- the read-only inspection core.
    ("find_kicad_components_by_", "inspect"),
    ("get_kicad_pin_", "inspect"),
    ("get_kicad_component", "inspect"),
    ("get_kicad_net", "net"),
    ("list_kicad_nets", "net"),
)

GROUP_OVERRIDES = {
    # These four are spelled "audit_kicad_*" like the schematic-health checks
    # above, but what they actually check is Mouser/sourcing data, so they
    # belong with the rest of the sourcing workflow instead.
    "audit_kicad_component_specs": "mouser",
    "audit_kicad_duplicate_mouser_links": "mouser",
    "audit_kicad_manufacturer_part_numbers": "mouser",
    "audit_kicad_stock_sufficiency": "mouser",
    # Netclass conformance reads back what the netclass-proposal workflow
    # wrote, so it travels with that group rather than the generic audits.
    "audit_kicad_netclass_conformance": "netclass",
    # Rest of the Mouser/sourcing/datasheet workflow -- no shared prefix with
    # the "bulk_"/"generate_kicad_mouser_" tools above, so each needs its own
    # line here.
    "fetch_kicad_datasheet": "mouser",
    "list_kicad_component_mouser_urls": "mouser",
    "lookup_mouser_part": "mouser",
    "optimize_kicad_mouser_alternates": "mouser",
    # Property-renaming helper for the manufacturer-part-number field: it
    # exists to feed the sourcing tools a clean MPN, so it is a sourcing tool
    # in intent even though it edits the schematic.
    "normalize_kicad_manufacturer_part_number_properties": "mouser",
    # Hierarchical-sheet-instance tools ("this part's siblings across every
    # copy of the same sub-sheet") -- no shared prefix, since the verb varies
    # (get/list/classify/match) while the shared word ("hierarchical",
    # "sibling", "group", "role", "anchor") sits in the middle of the name.
    "classify_kicad_group_by_anchor_pin": "hier",
    "get_kicad_hierarchical_group": "hier",
    "list_kicad_hierarchical_templates": "hier",
    "list_kicad_sibling_instances": "hier",
    "match_kicad_group_members_by_role": "hier",
    # Single-component/collision placement tools that don't share a prefix
    # with the diff/apply template family above, but are the same "move
    # things on the board" job.
    "estimate_kicad_footprint_radius": "layout",
    "find_kicad_layout_collisions": "layout",
    "get_kicad_property_position": "layout",
    "move_kicad_group": "layout",
    "nudge_kicad_to_clear": "layout",
    "set_kicad_component_position": "layout",
    "suggest_kicad_component_placement": "layout",
    # Copper-cloning counterpart to apply_kicad_route_template, for a single
    # part instead of a whole hierarchical group.
    "copy_kicad_component_routing": "route",
    # KiCad's Ctrl+G grouping construct -- a different "group" than the
    # hierarchical-sheet-instance sense above, which is exactly why it needs
    # its own facade group rather than sharing the "hier" name.
    "create_kicad_group": "group",
    "delete_kicad_group": "group",
    "list_kicad_groups": "group",
    # Live-KiCad-GUI tools with no shared prefix left after the ones above.
    "find_kicad_live_layout_collisions": "live",
    "get_kicad_ipc_status": "live",
    # Netclass/trace-cost/pcb_settings.json plumbing with no shared prefix.
    "create_kicad_netclass": "netclass",
    "get_kicad_pcb_settings": "netclass",
    "get_kicad_trace_cost": "netclass",
    "init_kicad_pcb_settings": "netclass",
    "measure_kicad_bus_corridor_area": "netclass",
    "propose_kicad_netclass": "netclass",
    # Board-wide copper inventory travels with the net tools it summarizes,
    # not with "inspect" (which is about components) or "layout".
    "get_kicad_track_inventory": "net",
    # Inspection-core tools whose names don't share a prefix with the
    # find_/get_kicad_pin_/get_kicad_component group above.
    "get_kicad_board_layers": "inspect",
    "get_kicad_footprint_pads": "inspect",
    "inspect_kicad_project": "inspect",
    "list_kicad_components": "inspect",
    "search_kicad_component": "inspect",
    # Process control for the MCP server itself -- not a KiCad-data tool at
    # all, so it gets a one-tool group rather than being wedged into "live".
    "restart_kicad_mcp_server": "system",
}

#: Extra search tokens for tools whose names hide what they are for. Kept to
#: the tools where the name alone would send a query to the wrong place --
#: most tool names here are already descriptive enough that KEYWORDS would
#: just restate them.
KEYWORDS = {
    "inspect_kicad_project": ("overview", "summary", "what", "board", "status"),
    # "what is connected to U10" is the single most common question asked of
    # this server, and without these it lost to the live-IPC status tool: the
    # tool's own name says "connections", never "connected" or "wired".
    "get_kicad_component_connections": ("connected", "wired", "goes", "attached", "hooked"),
    "audit_kicad_capacitor_voltages": ("rating", "written", "value", "field", "voltage"),
    "audit_kicad_capacitor_net_voltages": ("rated", "high", "enough", "derating", "net"),
    "audit_kicad_schematic_health": ("everything", "prefab", "preorder", "sanity", "onecall"),
    "audit_kicad_schematic_integrity": ("duplicate", "reference", "designator", "orphan"),
    "audit_kicad_symbol_pin_names": ("datasheet", "pdf", "mismatch", "wrong", "pinout"),
    "audit_kicad_stock_sufficiency": ("enough", "out", "of", "shortage"),
    "audit_kicad_duplicate_mouser_links": ("same", "part", "twice", "conflict"),
    "audit_kicad_manufacturer_part_numbers": ("mpn", "wrong", "mismatch"),
    "audit_kicad_component_specs": ("electrical", "wrong", "mismatch", "spec"),
    "generate_kicad_mouser_buy_list": ("order", "purchase", "cart", "checkout"),
    "generate_kicad_mouser_stock_report": ("cost", "estimate", "markdown", "report"),
    "lookup_mouser_part": ("stock", "price", "buy", "lifecycle"),
    "bulk_lookup_mouser_parts": ("stock", "price", "buy", "all", "everything"),
    "optimize_kicad_mouser_alternates": ("cheapest", "best", "rank", "alternate"),
    "fetch_kicad_datasheet": ("download", "pdf", "url"),
    "bulk_fetch_kicad_datasheets": ("download", "pdf", "all", "everything"),
    "detect_kicad_buses": ("i2c", "spi", "qspi", "i2s", "uart", "can", "usb", "swd", "jtag"),
    "detect_kicad_connectors": ("header", "pin", "socket"),
    "detect_kicad_critical_nets": ("high", "speed", "shorten", "cost"),
    "propose_kicad_netclass": ("width", "clearance", "recommend"),
    "create_kicad_netclass": ("net_settings", "class", "write"),
    "audit_kicad_netclass_conformance": ("mismatch", "wrong", "class", "actual"),
    "get_kicad_trace_cost": ("why", "long", "score", "length", "via", "autorouter"),
    "measure_kicad_bus_corridor_area": ("width", "area", "between", "traces"),
    "get_kicad_ipc_status": ("connected", "reachable", "open", "running"),
    "highlight_kicad_live_components": ("select", "point", "show", "human"),
    "get_kicad_live_bounding_box": ("real", "actual", "geometry", "size"),
    "find_kicad_layout_collisions": ("overlap", "clash", "too", "close"),
    "find_kicad_live_layout_collisions": ("overlap", "clash", "too", "close", "live"),
    "nudge_kicad_to_clear": ("bump", "unstick", "minimum", "move"),
    "suggest_kicad_component_placement": ("where", "recommend", "auto"),
    "estimate_kicad_footprint_radius": ("body", "size", "override", "package"),
    "get_kicad_hierarchical_group": ("sheet", "instance", "channel", "sibling"),
    "list_kicad_sibling_instances": ("copy", "repeated", "duplicate", "channel"),
    "restart_kicad_mcp_server": ("reload", "code", "change", "file", "edited"),
    "get_kicad_pcb_settings": ("config", "weights", "corridor"),
    "init_kicad_pcb_settings": ("default", "create", "write"),
}

#: Query word -> tokens to also score against. One-way, the same as the other
#: two facades: expanding a colloquial word toward the tool vocabulary helps,
#: but the reverse (every "bom" hit dragging down every unrelated query) would
#: not.
SYNONYMS = {
    "footprint": ("inspect", "layout"),
    "pad": ("inspect", "footprint"),
    "package": ("footprint", "radius"),
    "stackup": ("board", "layers"),
    "layer": ("board", "layers"),
    "drc": ("audit", "integrity", "health"),
    "rules": ("audit", "netclass"),
    "bom": ("schematic", "parts", "mouser"),
    "stock": ("mouser",),
    "price": ("mouser",),
    "cost": ("mouser", "trace"),
    "buy": ("mouser", "generate"),
    "order": ("mouser", "buy_list"),
    "purchase": ("mouser", "buy_list"),
    "vendor": ("mouser",),
    "mpn": ("mouser", "manufacturer"),
    "datasheet": ("mouser", "fetch"),
    "pdf": ("datasheet",),
    "ratsnest": ("net", "route"),
    "trace": ("route", "net"),
    "track": ("route", "net"),
    "via": ("route", "net", "trace_cost"),
    "zone": ("netclass", "pour"),
    "pour": ("zone", "copper"),
    "copper": ("route", "zone", "trace"),
    "silkscreen": ("property_position", "layout"),
    "label": ("property_position", "schematic"),
    "refdes": ("component", "reference"),
    "designator": ("component", "reference"),
    "class": ("netclass",),
    "corridor": ("netclass", "bus"),
    "bus": ("netclass", "detect"),
    "connector": ("netclass", "detect"),
    "route": ("layout", "route", "autorouter"),
    "router": ("route",),
    "capacitor": ("audit", "voltage"),
    "voltage": ("audit", "capacitor"),
    "collision": ("layout", "collisions"),
    "overlap": ("layout", "collisions"),
    "sheet": ("hier", "schematic"),
    "channel": ("hier", "sibling"),
    "duplicate": ("audit", "sibling", "hier"),
    "gui": ("live", "ipc"),
    "select": ("live", "highlight"),
    "highlight": ("live",),
    "move": ("layout", "position"),
    "flip": ("layout", "flip_template"),
    "mirror": ("layout", "flip_template"),
    "template": ("layout", "route", "diff", "apply"),
}

#: The sequences someone actually runs against this project, spelled out so
#: the first call of a session does not have to be a search. Every one of
#: these is paste-ready against the real main-board project path.
RECIPES = """\
  what is on the board:  kicad_call(name="inspect_kicad_project", args={"project_path":"hardware/mainBoard/kiln.kicad_pro"})
  find a component:      kicad_call(name="get_kicad_component", args={"project_path":"hardware/mainBoard/kiln.kicad_pro","reference":"U10"})
  its net connections:   kicad_call(name="get_kicad_component_connections", args={"project_path":"hardware/mainBoard/kiln.kicad_pro","reference":"U10"})
  cap voltage ratings:   kicad_call(name="audit_kicad_capacitor_voltages", args={"project_path":"hardware/mainBoard/kiln.kicad_pro"})
  net-aware cap check:   kicad_call(name="audit_kicad_capacitor_net_voltages", args={"project_path":"hardware/mainBoard/kiln.kicad_pro"})
  check Mouser stock:    kicad_call(name="bulk_lookup_mouser_parts", args={"project_path":"hardware/mainBoard/kiln.kicad_pro"})
  one part's price:      kicad_call(name="lookup_mouser_part", args={"mouser_part_number":"<str>"})
  before any *_live_* call: kicad_call(name="get_kicad_ipc_status")
  score a net's routing: kicad_call(name="get_kicad_trace_cost", args={"project_path":"hardware/mainBoard/kiln.kicad_pro","net":"<str>"})
  pre-fab sanity pass:   kicad_batch(calls=[{"name":"audit_kicad_schematic_health","args":{"project_path":"hardware/mainBoard/kiln.kicad_pro"}},{"name":"audit_kicad_stock_sufficiency","args":{"project_path":"hardware/mainBoard/kiln.kicad_pro"}}])
  detect the I2C/SPI buses: kicad_call(name="detect_kicad_buses", args={"project_path":"hardware/mainBoard/kiln.kicad_pro"})

Note: routing itself (route_kicad_board, list_zones, audit_plane_islands) is a
separate CLI, kicad_router_tool.py -- not wired into this MCP server, so it is
not reachable through kicad_call/kicad_batch at all. See CLAUDE.md's "Route
the Board" section for the CLI invocation.
"""

#: Stays directly published, schema and all. `inspect_kicad_project` is the
#: unconditional first call on a fresh project, the same role `connect` plays
#: for kilnctrl. `get_kicad_ipc_status` earns the second slot for a reason
#: neither of the other two servers has: about a third of this tool set (every
#: `*_live_*` tool) only works while a KiCad GUI process is open with that
#: project loaded, and there is no way to tell from the tool name alone
#: whether that is true right now -- paying a search round trip just to
#: discover "IPC not reachable" is exactly the overhead KEEP exists to avoid.
KEEP = ("inspect_kicad_project", "get_kicad_ipc_status")

TITLE = "KiCad project data (schematic + PCB) for kilnCtl's boards, plus live-editor and Mouser sourcing tools"
LABEL = "KiCad project"
PREFIX = "kicad_"

#: 8765 is kilnctrl's link_hub, 8766 is this server's own long-standing port.
DEFAULT_PORT = 8766
