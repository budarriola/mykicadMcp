#!/usr/bin/env python3
"""MCP server for inspecting and editing KiCad projects, over stdio or HTTP."""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, cast

LOG_PATH = Path(__file__).resolve().with_name("kicad_mcp_server.log")
SUPPORTED_PROTOCOL_VERSIONS = {"2024-11-05", "2025-03-26"}


def log_message(message: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {message}"
    try:
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except Exception:
        pass


try:
    from kicad_pcb_tool import (
    align_component_pin,
    align_components_to_anchor,
    apply_flip_template,
    apply_layout_changes,
    apply_layout_template,
    apply_property_position_changes,
    apply_property_position_template,
    audit_capacitor_net_voltages,
    classify_critical_nets,
    audit_capacitor_voltages,
    audit_netclass_conformance,
    audit_schematic_integrity,
    classify_group_by_anchor_pin,
    create_group,
    create_netclass,
    delete_group,
    detect_connectors,
    diff_flip_template,
    diff_layout_by_role,
    diff_layout_template,
    diff_property_position_template,
    estimate_footprint_radius,
    find_components_by_net,
    find_components_by_pin_connection,
    find_layout_collisions,
    get_board_layers,
    get_component,
    get_component_connections,
    get_footprint_pads,
    get_hierarchical_group,
    get_net,
    get_net_track_widths,
    detect_buses,
    get_pin_position,
    get_project_track_inventory,
    get_property_position,
    get_schematic_part,
    get_trace_cost,
    init_pcb_settings,
    inspect_project,
    list_components,
    list_groups,
    list_hierarchical_templates,
    list_nets,
    list_schematic_parts,
    list_sibling_instances,
    load_pcb_settings,
    measure_bus_corridor_areas,
    match_group_members_by_role,
    move_group,
    normalize_manufacturer_part_number_properties,
    nudge_to_clear,
    pin_distance,
    propose_netclass_from_nets,
    search_component_by_reference,
    set_schematic_property,
    suggest_component_placement,
    )
except Exception as exc:  # pragma: no cover - import safety
    log_message(f"Failed to import KiCad parser module: {exc}")
    traceback.print_exc(file=sys.stderr)
    raise


try:
    from kicad_router_tool import (
        audit_crosstalk,
        audit_plane_islands,
        benchmark_autoroute,
        create_plane,
        get_drc_constraints,
        get_ratsnest,
        list_zones,
        modify_plane,
        open_route_viewer,
        propose_plane,
        route_board,
        route_nets,
        unroute_nets,
    )
except Exception as exc:  # pragma: no cover - import safety
    log_message(f"Failed to import KiCad router module: {exc}")
    traceback.print_exc(file=sys.stderr)
    raise


try:
    from kicad_optimizer_tool import (
        decide_route,
        get_route_session,
        optimize_board,
        remove_stitching_vias,
        run_stitching_pass,
    )
except Exception as exc:  # pragma: no cover - import safety
    log_message(f"Failed to import KiCad optimizer module: {exc}")
    traceback.print_exc(file=sys.stderr)
    raise


try:
    from kicad_ipc_tool import (
        clear_live_highlight,
        find_live_layout_collisions,
        get_ipc_status,
        get_live_bounding_box,
        get_live_selection,
        highlight_live_components,
    )
    _IPC_AVAILABLE = True
except Exception as exc:  # pragma: no cover - optional dependency
    log_message(f"KiCad IPC tools unavailable (is kicad-python installed? {exc})")
    _IPC_AVAILABLE = False


try:
    from kicad_mouser_tool import (
        audit_component_specs_against_mouser,
        audit_manufacturer_part_numbers,
        audit_schematic_health,
        audit_stock_sufficiency,
        bulk_list_component_mouser_urls,
        bulk_lookup_mouser_parts,
        bulk_optimize_component_mouser_alternates,
        generate_mouser_buy_list,
        generate_mouser_stock_report,
        list_component_mouser_urls,
        lookup_mouser_part,
        optimize_component_mouser_alternates,
    )
except Exception as exc:  # pragma: no cover - import safety
    log_message(f"Failed to import Mouser lookup module: {exc}")
    traceback.print_exc(file=sys.stderr)
    raise


log_message("KiCad MCP server module imported successfully")


class KiCadMcpServer:
    def __init__(self) -> None:
        self.tools: dict[str, dict[str, Any]] = {
            "inspect_kicad_project": {
                "description": "Inspect a KiCad project directory or board file and return a summary.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {
                            "type": "string",
                            "description": "Path to a KiCad project directory, .kicad_pcb file, or .kicad_pro file.",
                        }
                    },
                    "required": ["project_path"],
                },
                "handler": self._tool_inspect_project,
            },
            "list_kicad_components": {
                "description": "List components from a KiCad PCB file.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string"},
                        "limit": {"type": "integer", "default": 50},
                    },
                    "required": ["project_path"],
                },
                "handler": self._tool_list_components,
            },
            "get_kicad_component": {
                "description": "Get a specific component by its reference/designator.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string"},
                        "reference": {"type": "string"},
                    },
                    "required": ["project_path", "reference"],
                },
                "handler": self._tool_get_component,
            },
            "get_kicad_component_connections": {
                "description": "Get the net connections for a specific component reference.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string"},
                        "reference": {"type": "string"},
                    },
                    "required": ["project_path", "reference"],
                },
                "handler": self._tool_get_component_connections,
            },
            "get_kicad_net": {
                "description": "Get details for a specific net name from the KiCad netlist.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string"},
                        "net_name": {"type": "string"},
                    },
                    "required": ["project_path", "net_name"],
                },
                "handler": self._tool_get_net,
            },
            "find_kicad_components_by_net": {
                "description": "Find all components connected to a specific net.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string"},
                        "net_name": {"type": "string"},
                    },
                    "required": ["project_path", "net_name"],
                },
                "handler": self._tool_find_components_by_net,
            },
            "find_kicad_components_by_pin_connection": {
                "description": "Find components that connect to a specific pin on a given component reference.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string"},
                        "reference": {"type": "string"},
                        "pin": {"type": "string"},
                    },
                    "required": ["project_path", "reference", "pin"],
                },
                "handler": self._tool_find_components_by_pin_connection,
            },
            "suggest_kicad_component_placement": {
                "description": "Suggest component placement positions based on connection grouping and rotation hints.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string"},
                        "reference": {"type": "string"},
                        "group_size": {"type": "integer", "default": 4},
                        "spacing": {"type": "number", "default": 10.0},
                        "rotation": {"type": "number", "default": 0.0},
                    },
                    "required": ["project_path", "reference"],
                },
                "handler": self._tool_suggest_component_placement,
            },
            "list_kicad_schematic_parts": {
                "description": (
                    "Get the unique parts list straight from the .kicad_sch files (all sheets, "
                    "following every hierarchical instance) instead of the exported kiln.csv BOM, "
                    "which can go stale. Groups every placed symbol by Value + Footprint - the same "
                    "grouping KiCad's own BOM exporter uses - and returns one row per unique part "
                    "with its quantity and every reference designator that shares it. Use this first, "
                    "then pass one of a row's `references` to get_kicad_schematic_part for that part's "
                    "full property set."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string", "description": "KiCad project directory, .kicad_pro, .kicad_pcb, or .kicad_sch path."},
                    },
                    "required": ["project_path"],
                },
                "handler": self._tool_list_schematic_parts,
            },
            "get_kicad_schematic_part": {
                "description": (
                    "Get every property KiCad stores on one placed schematic symbol by reference "
                    "designator (e.g. a reference from list_kicad_schematic_parts' `references`) - "
                    "Value, Footprint, Datasheet, Manufacturer_Name/Manufacturer_Part_Number, multiple "
                    "Mouser fields (Mouser, Mouser Part Number, Mouser Part Number Alt, etc), Sim.* "
                    "fields, whatever that symbol carries - plus its pin list and which schematic "
                    "sheet file/instance it was placed on."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string", "description": "KiCad project directory, .kicad_pro, .kicad_pcb, or .kicad_sch path."},
                        "reference": {"type": "string"},
                    },
                    "required": ["project_path", "reference"],
                },
                "handler": self._tool_get_schematic_part,
            },
            "audit_kicad_capacitor_voltages": {
                "description": (
                    "Check every unique capacitor value in the schematic (identified by the 'C<n>' "
                    "reference-designator convention) for a voltage rating written into its Value field, "
                    "e.g. '47uF 16V' vs. plain '0.1uf' - the common schematic convention where every cap "
                    "is assumed to use one project-wide default voltage unless its Value overrides it. "
                    "Pass `default_voltage` (e.g. '16V' or 16) to also split parts that do state a "
                    "voltage into ones that just redundantly restate the default vs. ones that genuinely "
                    "differ from it; omit it to only split has/missing a voltage indication. Can only see "
                    "what's written in the Value text, so 'missing_voltage' means 'assumed default', not "
                    "'verified against the real part'."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string", "description": "KiCad project directory, .kicad_pro, .kicad_pcb, or .kicad_sch path."},
                        "default_voltage": {"type": ["string", "number"], "description": "Project's default capacitor voltage rating, e.g. '16V' or 16."},
                    },
                    "required": ["project_path"],
                },
                "handler": self._tool_audit_capacitor_voltages,
            },
            "audit_kicad_capacitor_net_voltages": {
                "description": (
                    "Net-aware capacitor voltage check (Phase 8) - extends audit_kicad_capacitor_voltages "
                    "(which only checks a rating is *written* in the Value field) by reading the netlist to "
                    "find the two nets each capacitor ('C<n>' reference convention) actually sits across, "
                    "inferring each net's voltage from its name (override in pcb_settings.json's "
                    "schematic_checks.cap_voltage.net_voltages > a gnd_tokens substring match -> 0V > a "
                    "labeled-voltage pattern like '12V_Main'/'3V3' > unlabeled), and comparing the rating "
                    "against the *applied* differential voltage |V(net_a) - V(net_b)| rather than assuming "
                    "one rail vs ground. Verdicts per capacitor, worst-first: under_rated (rated < applied, "
                    "hard fail), unknown_rating (labeled nets but no rating anywhere - worth chasing), "
                    "under_derated (rated < derating_min_ratio x applied - the ceramic-DC-bias trap), ok, "
                    "one_net_unlabeled (informational, assumed_applied_v = resolved side vs 0V), "
                    "no_labeled_nets (skipped, counted), unsupported_pins (not exactly 2 netlist pins - "
                    "arrays/4-terminal caps - skipped). DNP capacitors excluded. Cross-checks netlist net "
                    "names against the board's own pad nets and reports stale_netlist_warnings on mismatch. "
                    "Read-only; settings actually used are echoed back in settings_used for reproducibility."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string", "description": "KiCad project directory, .kicad_pro, .kicad_pcb, or .kicad_sch path."},
                    },
                    "required": ["project_path"],
                },
                "handler": self._tool_audit_capacitor_net_voltages,
            },
            "detect_kicad_critical_nets": {
                "description": (
                    "Classify high-speed / critical nets so the cost model and router shorten exactly "
                    "these first. Sources, each row reporting its reason: (1) bus_frequency - member nets "
                    "of qualified detect_kicad_buses candidates (plus CLK-token nets) mapped to a typical "
                    "frequency via pcb_settings.json high_speed.bus_frequencies_mhz; (2) xtal - nets "
                    "touching a crystal/resonator (ref Y*/X* or crystal/resonator/osc footprint tokens), "
                    "always critical with the highest length weight; (3) switch_node - the inductor-to-IC "
                    "net of a switching supply (L* component larger than switch_node.min_inductor_mm both "
                    "axes). Per net: {critical, reason, frequency_mhz, l_crit_mm, straight_line_mm, "
                    "multiplier, stack_up_gate} where L_crit = c x velocity_fraction x (rise_fraction/f) / 6 "
                    "and stack_up_gate=true means straight-line length >= critical_fraction x L_crit - the "
                    "session should ask the user whether to pause routing until impedance control / "
                    "stack-up is configured. The resolved l_crit_table is included so every number is "
                    "inspectable. get_kicad_trace_cost applies the multipliers to classified nets' length "
                    "costs automatically. Read-only. Knobs: pcb_settings.json high_speed + switch_node."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string", "description": "KiCad project directory, .kicad_pro, .kicad_pcb, or .kicad_sch path."},
                    },
                    "required": ["project_path"],
                },
                "handler": self._tool_detect_critical_nets,
            },
            "detect_kicad_connectors": {
                "description": (
                    "Phase 7.14 (detection only, read-only): scan the board's own footprints/pads for "
                    "connector candidates. A footprint qualifies when EITHER its reference starts with "
                    "one of ref_prefixes (default from pcb_settings.json pin_swap.ref_prefixes, itself "
                    "J/P/CN/X) OR its footprint/library name contains a connector token (conn, header, "
                    "connector, socket, terminal - case-insensitive substring), catching connectors under "
                    "a non-standard ref. Per candidate: ref, footprint, pin_count, matched_by "
                    "(ref_prefix and/or footprint_token), and pins - one entry per pad with its pad "
                    "number and the net attached right now, read straight from the board file's own pad "
                    "(net ...) entries (ground truth, independent of any stale .net export). NEVER "
                    "guesses swappability - which pins could safely trade places is the user's call, not "
                    "this tool's. Never writes anything."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string", "description": "KiCad project directory, .kicad_pro, .kicad_pcb, or .kicad_sch path."},
                        "ref_prefixes": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Override the ref-prefix candidate list (default: pcb_settings.json pin_swap.ref_prefixes, itself J/P/CN/X).",
                        },
                    },
                    "required": ["project_path"],
                },
                "handler": self._tool_detect_connectors,
            },
            "lookup_mouser_part": {
                "description": (
                    "Look up a Mouser product via Mouser's official Search API and extract its "
                    "manufacturer part number, stock/lifecycle status, plus type-specific electrical "
                    "specs. Pass a Mouser product URL, e.g. one found in a schematic part's "
                    "'Mouser Part Number', 'Mouser', 'Mouser Part Number Alt', or 'Datasheet' property "
                    "via get_kicad_schematic_part (supports multiple Mouser fields). Detects capacitor "
                    "vs resistor vs other: capacitors get capacitance + voltage_rating, resistors get "
                    "resistance, and either an MLCC capacitor or SMT resistor additionally gets "
                    "package_size_inch (e.g. '0402', '0805'). Fields that don't apply to the detected "
                    "type come back 'unsupported'; fields that should apply but couldn't be found come "
                    "back 'unknown'. `raw_specifications` has every spec Mouser listed, in case the "
                    "field-name mapping misses one. REQUIRES MOUSER_API_KEY (repo-root .env) - raises "
                    "a clear error asking for one if missing; does not fall back to scraping the site. "
                    "For more than a couple of parts, use bulk_lookup_mouser_parts instead - it's a "
                    "single round trip."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "Mouser product page URL."},
                    },
                    "required": ["url"],
                },
                "handler": self._tool_lookup_mouser_part,
            },
            "bulk_lookup_mouser_parts": {
                "description": (
                    "Look up Mouser data (MPN, stock, lifecycle status, electrical specs) for many "
                    "schematic parts in one call instead of one round trip per part - the fast path for "
                    "auditing a whole schematic. Defaults to one representative reference per unique part "
                    "from list_kicad_schematic_parts; pass `references` for a specific subset instead. "
                    "Parts with no discoverable Mouser link, or whose lookup fails, are reported under "
                    "`skipped`/`errors` rather than aborting the batch. REQUIRES MOUSER_API_KEY."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string", "description": "KiCad project directory, .kicad_pro, .kicad_pcb, or .kicad_sch path."},
                        "references": {"type": "array", "items": {"type": "string"}, "description": "Specific reference designators to look up instead of every unique part."},
                    },
                    "required": ["project_path"],
                },
                "handler": self._tool_bulk_lookup_mouser_parts,
            },
            "normalize_kicad_manufacturer_part_number_properties": {
                "description": (
                    "Find schematic symbols that carry a manufacturer-part-number-shaped property under "
                    "some other name (e.g. 'PROD_ID', 'MPN', 'Part Number') but don't already have the "
                    "project's canonical 'Manufacturer_Part_Number' property, and rename that property key "
                    "to the canonical name (value untouched). Only renames when exactly one alias "
                    "candidate is present on a symbol lacking the canonical key; symbols with more than "
                    "one candidate come back under `ambiguous` instead of being guessed at. Defaults to "
                    "write=false (dry run) - inspect `changes`/`ambiguous`, then call again with "
                    "write=true to actually edit the .kicad_sch files."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string", "description": "KiCad project directory, .kicad_pro, .kicad_pcb, or .kicad_sch path."},
                        "write": {"type": "boolean", "default": False},
                        "allow_while_open": {"type": "boolean", "default": False, "description": "Skip the check that refuses to write while KiCad has a sheet open for editing."},
                    },
                    "required": ["project_path"],
                },
                "handler": self._tool_normalize_manufacturer_part_number_properties,
            },
            "audit_kicad_manufacturer_part_numbers": {
                "description": (
                    "Cross-check each schematic part's 'Manufacturer_Part_Number' property against the "
                    "manufacturer part number Mouser's Search API actually returns for that part's own "
                    "Mouser link - catches typos, copy-paste errors, or a stale value left over from "
                    "swapping which exact part a symbol points to. Run "
                    "normalize_kicad_manufacturer_part_number_properties (with write=true) first so parts "
                    "that only carry the MPN under a differently-named property get picked up here too. "
                    "REQUIRES MOUSER_API_KEY."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string", "description": "KiCad project directory, .kicad_pro, .kicad_pcb, or .kicad_sch path."},
                        "references": {"type": "array", "items": {"type": "string"}, "description": "Specific reference designators to check instead of every unique part."},
                    },
                    "required": ["project_path"],
                },
                "handler": self._tool_audit_manufacturer_part_numbers,
            },
            "generate_kicad_mouser_stock_report": {
                "description": (
                    "Run a bulk Mouser lookup across the schematic's unique parts and write a Markdown "
                    "report with: a BOM cost estimate for one board (using Mouser's own quantity-break "
                    "pricing against how many of each part the schematic actually uses, with unpriced "
                    "parts listed separately rather than silently excluded from the total), and every "
                    "part Mouser currently shows as out of stock or lifecycle-flagged (Not Recommended "
                    "for New Designs, obsolete, discontinued, etc), so those can be addressed before "
                    "fabrication/ordering. Defaults to writing 'mouser_stock_report.md' at the project "
                    "root. REQUIRES MOUSER_API_KEY."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string", "description": "KiCad project directory, .kicad_pro, .kicad_pcb, or .kicad_sch path."},
                        "report_path": {"type": "string", "description": "Output file path; defaults to mouser_stock_report.md at the project root."},
                        "references": {"type": "array", "items": {"type": "string"}, "description": "Specific reference designators to check instead of every unique part."},
                    },
                    "required": ["project_path"],
                },
                "handler": self._tool_generate_mouser_stock_report,
            },
            "list_kicad_component_mouser_urls": {
                "description": (
                    "Get all available Mouser URLs (primary and alternates) for a single component by "
                    "reference designator. Useful for spotting components with multiple Mouser sources, "
                    "alternates, or stale/incorrect links. Returns field names alongside URLs so you "
                    "know which property each link came from (e.g. 'Mouser Part Number', "
                    "'Mouser Part Number Alt', 'Datasheet', etc). Supports multiple Mouser fields with "
                    "automatic prioritization."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string", "description": "KiCad project directory, .kicad_pro, .kicad_pcb, or .kicad_sch path."},
                        "reference": {"type": "string"},
                    },
                    "required": ["project_path", "reference"],
                },
                "handler": self._tool_list_component_mouser_urls,
            },
            "bulk_list_kicad_component_mouser_urls": {
                "description": (
                    "List all Mouser URLs for many schematic parts in one call - an audit of which parts "
                    "have alternates, which are missing Mouser links, and which fields each link came from. "
                    "Defaults to one representative reference per unique part from list_kicad_schematic_parts; "
                    "pass `references` for a specific subset instead. Useful before bulk_lookup_mouser_parts "
                    "to spot parts with stale or multiple links that should be cleaned up first."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string", "description": "KiCad project directory, .kicad_pro, .kicad_pcb, or .kicad_sch path."},
                        "references": {"type": "array", "items": {"type": "string"}, "description": "Specific reference designators to check instead of every unique part."},
                    },
                    "required": ["project_path"],
                },
                "handler": self._tool_bulk_list_component_mouser_urls,
            },
            "optimize_kicad_mouser_alternates": {
                "description": (
                    "Rank a component's candidate Mouser links (its 'Mouser', 'Mouser Price/Stock', "
                    "'Mouser Part Number Alt', etc properties) by live stock and pricing instead of the "
                    "static field-name order find_mouser_url normally uses, and recommend which one to "
                    "treat as primary. Priority order: #1 in stock for at least the board's required "
                    "quantity, #2 sold with a qty-1 price break (over reel-only/bulk-minimum pricing) - "
                    "ties broken by greatest quantity in stock, #3 cheapest unit price at the required "
                    "quantity. `quantity_needed` defaults to how many this part's Value+Footprint group "
                    "actually places on the board. REQUIRES MOUSER_API_KEY."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string", "description": "KiCad project directory, .kicad_pro, .kicad_pcb, or .kicad_sch path."},
                        "reference": {"type": "string"},
                        "quantity_needed": {"type": "integer", "description": "Override the board quantity to optimize for; defaults to the part's actual placed count."},
                    },
                    "required": ["project_path", "reference"],
                },
                "handler": self._tool_optimize_component_mouser_alternates,
            },
            "bulk_optimize_kicad_mouser_alternates": {
                "description": (
                    "Batch version of optimize_kicad_mouser_alternates across the schematic's unique "
                    "parts (or a given subset) - the actionable follow-up to "
                    "bulk_list_kicad_component_mouser_urls once it's shown which parts have more than one "
                    "candidate Mouser link. Defaults to skipping parts with only a single candidate link "
                    "(only_with_alternates=true) to spend the rate-limited API budget on parts where "
                    "there's an actual choice; set it false to also flag single-link parts that turn out "
                    "out-of-stock or lack price-break data. Returns `changed` - parts whose live-ranked "
                    "recommendation differs from the static field-priority pick - as the list worth "
                    "re-pointing at a better link. REQUIRES MOUSER_API_KEY."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string", "description": "KiCad project directory, .kicad_pro, .kicad_pcb, or .kicad_sch path."},
                        "references": {"type": "array", "items": {"type": "string"}, "description": "Specific reference designators to check instead of every unique part."},
                        "only_with_alternates": {"type": "boolean", "default": True, "description": "Skip parts with only one candidate Mouser link."},
                    },
                    "required": ["project_path"],
                },
                "handler": self._tool_bulk_optimize_component_mouser_alternates,
            },
            "generate_kicad_mouser_buy_list": {
                "description": (
                    "Build an orderable buy list across the schematic's unique parts (or a given "
                    "subset): the best Mouser link per part (live stock/price ranked the same way as "
                    "bulk_optimize_kicad_mouser_alternates) and how many units to actually buy. Buy "
                    "quantity starts at the board's required quantity, then: parts under $0.05/unit get "
                    "10 extra, parts under $0.10/unit get 5 extra, and on top of that any part is bumped "
                    "further whenever a higher Mouser price-break tier's total cost is cheaper overall "
                    "than the padded quantity's total cost (never bumped below the padded quantity). "
                    "Writes a Markdown table to `buy_list_path` (defaults to 'buy_list.md' at the project "
                    "root) with links, quantities, per-line cost, the reason behind any extra units, and "
                    "a grand total. REQUIRES MOUSER_API_KEY."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string", "description": "KiCad project directory, .kicad_pro, .kicad_pcb, or .kicad_sch path."},
                        "buy_list_path": {"type": "string", "description": "Output file path; defaults to buy_list.md at the project root."},
                        "references": {"type": "array", "items": {"type": "string"}, "description": "Specific reference designators to include instead of every unique part."},
                    },
                    "required": ["project_path"],
                },
                "handler": self._tool_generate_mouser_buy_list,
            },
            "audit_kicad_schematic_integrity": {
                "description": (
                    "Cheap, netlist-independent sanity checks across every placed schematic symbol "
                    "(power symbols excluded): duplicate reference designators (two distinct placed "
                    "instances annotated with the exact same reference - a real KiCad ERC 'duplicate "
                    "reference' error, not just two instances of the same hierarchical block, which get "
                    "their own distinct references), and symbols missing a Value or Footprint altogether. "
                    "Pure text/structure checks - no Mouser data or API key needed, so this is a fast "
                    "first pass before the slower Mouser-backed audits."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string", "description": "KiCad project directory, .kicad_pro, .kicad_pcb, or .kicad_sch path."},
                    },
                    "required": ["project_path"],
                },
                "handler": self._tool_audit_schematic_integrity,
            },
            "audit_kicad_component_specs": {
                "description": (
                    "Cross-check every unique schematic part's Value/Footprint/Manufacturer_Part_Number "
                    "against what Mouser's Search API actually returns for that part's own linked Mouser "
                    "product - catches a link that resolves fine but points at the wrong part (wrong "
                    "resistance/capacitance, wrong package, or a stale/typo'd MPN). This is the exact bug "
                    "class behind the R96/R103 stale-link issue in todo.md (link pointed at a 47.5kOhm "
                    "part for a 154k resistor). Three independent checks per part, each reported "
                    "'match'/'mismatch'/'not_verifiable' (never a silent pass when data is missing): "
                    "manufacturer_part_number (vs Mouser's MPN - run "
                    "normalize_kicad_manufacturer_part_number_properties with write=true first so parts "
                    "carrying the MPN under a differently-named property are picked up), package (EIA "
                    "imperial size code parsed from the Footprint name vs Mouser's package_size_inch, only "
                    "meaningful for chip resistors/ceramic capacitors), and value (nominal "
                    "resistance/capacitance parsed from the Value field vs Mouser's spec, compared "
                    "numerically within `value_tolerance_pct` percent so '10k' vs '10 kOhm' formatting "
                    "differences don't read as mismatches - resistors/capacitors only). REQUIRES "
                    "MOUSER_API_KEY."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string", "description": "KiCad project directory, .kicad_pro, .kicad_pcb, or .kicad_sch path."},
                        "references": {"type": "array", "items": {"type": "string"}, "description": "Specific reference designators to check instead of every unique part."},
                        "value_tolerance_pct": {"type": "number", "default": 1.0, "description": "Allowed percent difference between the schematic's stated value and Mouser's spec before flagging a mismatch."},
                    },
                    "required": ["project_path"],
                },
                "handler": self._tool_audit_component_specs,
            },
            "audit_kicad_stock_sufficiency": {
                "description": (
                    "Check that at least one candidate Mouser link for every unique schematic part (not "
                    "just its current primary link - every 'Mouser'/'Mouser Price/Stock'/'Mouser Part "
                    "Number Alt'/etc field) is in stock for enough units to build `board_quantity` "
                    "board(s), via the same live-data ranking optimize_kicad_mouser_alternates uses. A "
                    "part whose primary link is out of stock but has a working alternate is NOT flagged - "
                    "`insufficient` only lists parts where no candidate link covers the need. "
                    "`board_quantity` multiplies each part's own schematic-placed count; defaults to 1 "
                    "board. REQUIRES MOUSER_API_KEY."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string", "description": "KiCad project directory, .kicad_pro, .kicad_pcb, or .kicad_sch path."},
                        "references": {"type": "array", "items": {"type": "string"}, "description": "Specific reference designators to check instead of every unique part."},
                        "board_quantity": {"type": "integer", "default": 1, "description": "How many boards' worth to check stock for."},
                    },
                    "required": ["project_path"],
                },
                "handler": self._tool_audit_stock_sufficiency,
            },
            "audit_kicad_schematic_health": {
                "description": (
                    "One-call pre-fab/pre-order sanity pass across the whole schematic, combining every "
                    "error check this project has: (1) audit_kicad_schematic_integrity - duplicate "
                    "reference designators, missing Value/Footprint; (2) audit_kicad_capacitor_voltages - "
                    "every capacitor either states its own voltage or is assumed to use "
                    "`default_capacitor_voltage`; (3) audit_kicad_component_specs - each part's "
                    "Value/Footprint/MPN matches its linked Mouser product; (4) audit_kicad_stock_sufficiency "
                    "- at least one candidate link per part covers `board_quantity` board(s). There is no "
                    "universally-correct default capacitor voltage for this project - ASK THE USER what "
                    "voltage rating this design assumes for capacitors that don't state one before calling "
                    "this (Power.kicad_sch/Regulators.kicad_sch's rail voltages are a reasonable thing to "
                    "bring up in that conversation, but the actual answer is a project decision). Steps 3-4 "
                    "REQUIRE MOUSER_API_KEY and are the slow, rate-limited part of this call. Writes a "
                    "Markdown summary to `report_path` (defaults to 'schematic_health_report.md' at the "
                    "project root); full structured results are also returned as JSON."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string", "description": "KiCad project directory, .kicad_pro, .kicad_pcb, or .kicad_sch path."},
                        "default_capacitor_voltage": {"type": ["string", "number"], "description": "Project's default capacitor voltage rating (e.g. '16V' or 16) for caps that don't state their own - ask the user for this, don't guess."},
                        "references": {"type": "array", "items": {"type": "string"}, "description": "Specific reference designators to check instead of every unique part (applies to the Mouser-backed steps)."},
                        "board_quantity": {"type": "integer", "default": 1, "description": "How many boards' worth to check stock sufficiency for."},
                        "value_tolerance_pct": {"type": "number", "default": 1.0, "description": "Allowed percent difference between a schematic value and Mouser's spec before flagging a mismatch."},
                        "report_path": {"type": "string", "description": "Output Markdown file path; defaults to schematic_health_report.md at the project root."},
                    },
                    "required": ["project_path", "default_capacitor_voltage"],
                },
                "handler": self._tool_audit_schematic_health,
            },
            "set_kicad_schematic_property": {
                "description": (
                    "Set one property on a schematic symbol by reference designator - updates it in "
                    "place if already present (matched case-insensitively), otherwise inserts it as a "
                    "new hidden field styled/positioned like the symbol's existing 'Datasheet' property "
                    "(KiCad's own convention for supplementary metadata fields such as distributor "
                    "links), anchored right after it. Defaults to write=false (dry run) - inspect "
                    "`change`, then call again with write=true to actually edit the .kicad_sch file."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string", "description": "KiCad project directory, .kicad_pro, .kicad_pcb, or .kicad_sch path."},
                        "reference": {"type": "string"},
                        "property_name": {"type": "string"},
                        "value": {"type": "string"},
                        "write": {"type": "boolean", "default": False},
                        "allow_while_open": {"type": "boolean", "default": False, "description": "Skip the check that refuses to write while KiCad has a sheet open for editing."},
                    },
                    "required": ["project_path", "reference", "property_name", "value"],
                },
                "handler": self._tool_set_schematic_property,
            },
            "list_kicad_nets": {
                "description": "List nets from the KiCad netlist.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string"},
                    },
                    "required": ["project_path"],
                },
                "handler": self._tool_list_nets,
            },
            "get_kicad_net_track_widths": {
                "description": (
                    "Per-net aggregate of routed copper trace widths and via sizes, measured "
                    "directly from the PCB's own segment/via/arc geometry (not netclass intent). "
                    "Omit net_name to get every routed net (sorted by name); pass net_name for one "
                    "net. is_uniform=false flags a net routed at more than one width."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string"},
                        "net_name": {"type": "string"},
                    },
                    "required": ["project_path"],
                },
                "handler": self._tool_get_net_track_widths,
            },
            "detect_kicad_buses": {
                "description": (
                    "Read-only bus detection over the schematic netlist (I2C, SPI, QSPI, I2S, UART, "
                    "CAN, USB, SWD, JTAG). Groups nets by shared hierarchical prefix (e.g. "
                    "/MainControler/), matches each group against a bus signal signature table, and "
                    "for every match emits a candidate with per-net width_summary (from "
                    "get_kicad_net_track_widths), common_ics (the IC shared across all member nets - "
                    "the bus master/hub), qualified (true when a common IC spans the whole bus), and a "
                    "suggested_class_name. Never writes or applies anything - candidates only, for the "
                    "caller to confirm with the user before Phase 4 creates any net class. Also cross-"
                    "checks netlist net names against the board's own pad nets and reports mismatches "
                    "in stale_netlist_warnings (the .net export can lag the board). Candidates the user "
                    "confirmed on a previous run (cached in the gitignored <board>.board_local.json's "
                    "confirmed_buses) come back marked confirmed:true with confirmed_on/confirmed_name, "
                    "so only unconfirmed candidates need to be presented again."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string"},
                        "ic_ref_prefixes": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Reference-designator prefixes treated as ICs for qualification. Default ['U', 'IC', 'Q'].",
                        },
                    },
                    "required": ["project_path"],
                },
                "handler": self._tool_detect_buses,
            },
            "measure_kicad_bus_corridor_area": {
                "description": (
                    "Measure the routing-corridor area a bus bundle's traces enclose (the 'area "
                    "between the traces'), PER destination IC - so a bus that fans out to several ICs "
                    "is measured as the sub-bundles that actually run together, not one inflated "
                    "envelope. Pass EITHER a detect_kicad_buses candidate object (bus=) or an explicit "
                    "net list (nets=[...], optional hub_ic). Reuses the qualification roles, pad-centroid "
                    "anchors, and copper parse of the earlier phases. Each bundle reports corridor_area_mm2 "
                    "(station-resampled perpendicular spread along the hub->dest axis, bend-insensitive), "
                    "hull_area_mm2 (convex-hull sanity bound), bend_ratio, mean_spacing_mm, and its nets "
                    "with shared/dedicated roles. Also returns sum_of_bundle_areas_mm2 (shared copper "
                    "counted once per bundle it serves), union_hull_area_mm2 (whole-bus envelope), and "
                    "unassigned_segment_count (fan-out/transition copper). Degenerate: single destination "
                    "-> one bundle no clipping; no hub / no destination IC -> grouped:false. Read-only."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string"},
                        "bus": {"type": "object", "description": "A detect_kicad_buses candidate object (its nets/common_ics/bus_type)."},
                        "nets": {"type": "array", "items": {"type": "string"}, "description": "Explicit bus net names (alternative to bus=)."},
                        "hub_ic": {"type": "string", "description": "Override the hub/master IC reference (e.g. 'U4')."},
                        "bus_type": {"type": "string", "description": "Optional label (e.g. 'SPI') when passing nets= directly."},
                        "clip_band_mult": {"type": "number", "description": "Shared-copper clip band = this x dominant trace width. Default from corridor.clip_band_mult (3.0)."},
                        "station_step": {"type": "number", "description": "Corridor station resample step in mm. Default = dominant trace width."},
                        "ic_ref_prefixes": {"type": "array", "items": {"type": "string"}, "description": "Reference prefixes treated as ICs. Default ['U','IC','Q']."},
                    },
                    "required": ["project_path"],
                },
                "handler": self._tool_measure_bus_corridor_area,
            },
            "get_kicad_track_inventory": {
                "description": (
                    "Board-wide, copper-only inventory of every track width and via size actually "
                    "used on the PCB, plus the netclasses already defined in the project file - the "
                    "menu of previously-used values to offer a user instead of free-entry numbers. "
                    "Flags free/oversized via buckets and reports free_via_count."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string"},
                    },
                    "required": ["project_path"],
                },
                "handler": self._tool_get_track_inventory,
            },
            "get_kicad_pcb_settings": {
                "description": (
                    "Load pcb_settings.json from the project directory (next to <name>.kicad_pro) "
                    "deep-merged over the in-code defaults (trace-cost weights, corridor/bus-detection/"
                    "layer-purpose/autorouter/plane/schematic-check/optimizer knobs). A missing file is "
                    "not an error - every tool that reads settings works on pure defaults out of the "
                    "box. Returns the effective config plus keys_from_file / keys_from_defaults so a "
                    "caller can tell what's customized vs. stock. Raises if a weight in trace_cost is "
                    "negative or non-numeric."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string"},
                    },
                    "required": ["project_path"],
                },
                "handler": self._tool_get_pcb_settings,
            },
            "init_kicad_pcb_settings": {
                "description": (
                    "Write the fully-populated default pcb_settings.json into the project directory. "
                    "Plain JSON (json.dump(indent=2)) - our own file, not KiCad's. Defaults to write=false "
                    "(dry run), returning the would-be file content without touching disk. write=true "
                    "refuses to clobber an existing file unless overwrite=true."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string"},
                        "write": {"type": "boolean", "default": False},
                        "overwrite": {"type": "boolean", "default": False, "description": "Allow replacing an existing pcb_settings.json."},
                    },
                    "required": ["project_path"],
                },
                "handler": self._tool_init_pcb_settings,
            },
            "get_kicad_trace_cost": {
                "description": (
                    "Score routed copper with the trace-cost model: length cost (copper length x "
                    "weights.length_mm), via cost (via count x weights.via x via_weights.through - every "
                    "via on this board is through-hole), layer_span cost ((layers_used - 1) x "
                    "weights.layer_span), a deviation cost for nets on detected bus bundles (per "
                    "trace_cost.deviation.metric/reference; non-bundle nets get "
                    "trace_cost.non_bus_deviation, default 0), and a layer_penalty: each net is "
                    "classified power/signal (net_kind, via layer_purpose.power_net_patterns), its "
                    "copper broken down per layer, and copper sitting on a wrong-purpose layer (per the "
                    "board's own (layers ...) types x the layer_purpose multiplier table) is charged "
                    "length_on_layer x (multiplier - 1) x weights.length_mm extra - existing layer-"
                    "purpose violations show up in cost triage. Omit net to get every routed net ranked "
                    "worst-cost-first plus board totals and the weights_used actually applied."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string"},
                        "net": {"type": "string", "description": "Return cost for just this net; omit for every routed net, ranked worst-first."},
                    },
                    "required": ["project_path"],
                },
                "handler": self._tool_get_trace_cost,
            },
            "get_kicad_board_layers": {
                "description": (
                    "Parse the board file's own top-level (layers ...) block into the copper stack: "
                    "one {name, ordinal, type, user_name} per copper (*.Cu) layer, in physical stack "
                    "order (front to back). type is KiCad's own layer designation - signal | power | "
                    "mixed | jumper - the layer purposes the trace-cost layer_penalty and the Phase 7 "
                    "router cost model key off (e.g. on kiln: F.Cu/B.Cu signal, In1.Cu/In2.Cu power). "
                    "Read-only; also returns copper_layer_count and per-type counts."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string"},
                    },
                    "required": ["project_path"],
                },
                "handler": self._tool_get_board_layers,
            },
            "get_kicad_ratsnest": {
                "description": (
                    "List every unrouted connection (missing ratsnest line) on the board. "
                    "Connectivity is computed with union-find over each net's pads + existing copper "
                    "(segments/arcs/vias) AND its filled zone polygons, using the board file's own pad "
                    "nets as ground truth (immune to .net netlist staleness): two items join when they "
                    "share a copper layer and their copper overlaps within tolerance (a via joins the "
                    "layers it spans; a pad/trace over a same-net plane fill joins the fill, incl. across "
                    "thermal gaps). For each net with >= 2 connected islands the missing connections are "
                    "the MST decomposition over its islands (no cycles), each reporting net, from/to "
                    "island representative (nearest pad refs or copper/zone uuid), airline_length_mm, and "
                    "the layers each side lives on. Connections are ordered most-constrained-first: by "
                    "net_overrides.priority (from the board-local JSON) descending, then shortest airline "
                    "first. Summary gives total connections, total airline mm, fully-routed net count, and "
                    "the unrouted-net list; single-pad and free-copper (net \"\") nets are counted "
                    "separately. Read-only; pass nets to restrict to specific net names."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string"},
                        "nets": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Restrict to these net names; omit for the whole board.",
                        },
                    },
                    "required": ["project_path"],
                },
                "handler": self._tool_get_ratsnest,
            },
            "list_kicad_zones": {
                "description": (
                    "Phase 7.5.1 zone parser: list every BOARD-LEVEL zone (copper pour or keepout) - "
                    "footprint-nested pad-keepout zones are excluded, only plane/board zones are "
                    "returned. Per zone: net (empty for keepouts/no-net zones), layers (a LIST - KiCad 9 "
                    "multi-layer zones are native on this board, e.g. mainGnd spans F.Cu/B.Cu/In1.Cu), "
                    "uuid, name, priority (higher wins overlap), hatch (style/pitch), connect_pads "
                    "(mode/clearance), min_thickness, fill settings (thermal_gap, thermal_bridge_width, "
                    "smoothing, radius, and island_removal_mode - read directly, since mode 1 changes "
                    "island-removal semantics the cost model must respect), the outline polygon, and "
                    "filled_polygon blocks (per layer) WHEN PRESENT - never fabricated; a zone that "
                    "hasn't been filled in KiCad simply has none. On kiln this finds six zones: mainGnd "
                    "(GND_Main, F/B/In1.Cu), safty_gnd (GND_Safty, priority 1), main12v, main3.3, "
                    "3.3v_safty (all In2.Cu), and an unnamed-net antenna keepout. Read-only; cached on "
                    "board mtime/size. Supersedes the Phase 7.3 connectivity-only zone-fill stopgap."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string"},
                    },
                    "required": ["project_path"],
                },
                "handler": self._tool_list_zones,
            },
            "audit_kicad_plane_islands": {
                "description": (
                    "Phase 7.5.2 (fill model) + 7.5.3 (islands & attachment-point costing): per "
                    "net-owning board zone/layer, report fill_source ('kicad' when the zone carries "
                    "real filled_polygon data - true for all six kiln zones - else 'estimated': the "
                    "outline rasterized at autorouter.grid_mm, with higher-priority-zone overlap and "
                    "clearance-inflated foreign copper subtracted, split into connected components), "
                    "component_count, and per component its role (mainland - the component with the "
                    "most same-net pad/via attachments; island; orphan - 0 attachments, dead copper; "
                    "or will_be_removed when the zone's island_removal_mode is 1, since KiCad deletes "
                    "such islands on refill and they are never costed/stitched), its attachment list, "
                    "area_mm2, current cost (island_base/attachment_count, orphan_island at 0 "
                    "attachments, 0.0 for the mainland - all from pcb_settings.json's plane block), a "
                    "warn flag below island_min_attachments_warn, and for costed islands the cheapest "
                    "stitching-via position found (nearest point to the mainland component) with its "
                    "projected new attachment count and cost. Keepout/no-net zones are excluded. "
                    "Phase 7.18.2 adds a CROSS-layer view under the top-level cross_layer key (and "
                    "summary.weakly_coupled_layer_pairs): for every net owning fill on more than one "
                    "copper layer, each unordered layer pair reports bonding_via_count (same-net vias "
                    "that span BOTH layers and land inside real fill copper on both - a through via "
                    "bonds every layer between its named ends), bonding_pad_count (same-net through-"
                    "hole pads doing the same; context only, a stitching pass does not place pads), "
                    "stack_adjacent, and weakly_coupled when the bonding-VIA count is below "
                    "island_min_attachments_warn - two pours that are one netlist node but are "
                    "electrically thin between them. Read-only; does not place anything - "
                    "run_kicad_stitching_pass (Phase 7.5.6) is the writer that fixes both the "
                    "same-layer islands and the cross-layer gaps this reports."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string"},
                    },
                    "required": ["project_path"],
                },
                "handler": self._tool_audit_plane_islands,
            },
            "audit_kicad_crosstalk": {
                "description": (
                    "Phase 7.20: measure adjacent-layer PARALLEL TRACE RUNS (broadside crosstalk "
                    "risk) on the board's existing copper. For every pair of track segments on "
                    "STACK-ADJACENT copper layers (consecutive in the board's own layer stack - a "
                    "non-adjacent pair has a reference plane / enough dielectric between it, so it "
                    "is not reported) whose nets differ, the TRUE overlap length is measured by "
                    "marching along the segment, so a crossing contributes only the crossing width "
                    "while a broadside run contributes its full coupled length. Runs at or above "
                    "crosstalk.min_parallel_run_mm, within crosstalk.min_spacing_mm edge-to-edge in "
                    "XY, are reported. BOTH sides of the same-bus exemption are returned: "
                    "'violations' (penalised) and 'exempt_runs' (nets sharing a confirmed_buses "
                    "entry or a detect_buses candidate - running parallel is the POINT of a bus, so "
                    "they cost nothing against each other, though they still pay against "
                    "third-party nets). Reporting the exempt set is deliberate: a false exemption "
                    "silently hides real crosstalk risk, and is invisible unless shown. Read-only. "
                    "Optional overrides for each threshold; adjacent_layer_pairs overrides the "
                    "stack adjacency itself for a stack-up what-if (e.g. kiln carries tracks only "
                    "on F.Cu/B.Cu, three apart in its 4-layer stack, so its REAL adjacency has zero "
                    "track-vs-track exposure - asking what the same routing would cost on a 2-layer "
                    "stack-up is a genuine design question). The routing-TIME counterpart is the "
                    "crosstalk cost term in route_kicad_board/route_kicad_nets, off by default "
                    "(crosstalk.adjacent_layer_penalty_per_mm = 0.0)."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string"},
                        "min_spacing_mm": {"type": "number"},
                        "min_parallel_run_mm": {"type": "number"},
                        "penalty_per_mm": {"type": "number"},
                        "same_bus_exempt": {"type": "boolean"},
                        "adjacent_layer_pairs": {
                            "type": "array",
                            "items": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                    "required": ["project_path"],
                },
                "handler": self._tool_audit_crosstalk,
            },
            "get_kicad_drc_constraints": {
                "description": (
                    "Resolve the project's DRC constraints into one merged table, in precedence order: "
                    "custom .kicad_dru rules file (e.g. JLCPCB.kicad_dru.txt) > .kicad_pro net-class and "
                    "board design-settings rules > pcb_settings.json autorouter.clearance_fallback_mm. "
                    "Parses (rule ...) constraints - clearance, track_width, via diameter/drill/annular, "
                    "hole-to-hole, edge clearance - with condition expressions limited to offline-evaluable "
                    "predicates; rules whose conditions cannot be evaluated offline (pairwise B.Type/B.Net "
                    "etc.) are listed in unsupported_rules with the reason, never silently ignored. Each "
                    "resolved constraint carries its value plus a sources list tracing which rule/board "
                    "setting supplied it (with the rule's layer where given). Also returns the raw "
                    "net_classes and board_rules maps for per-net resolution. This is the single resolver "
                    "every geometric router stage consumes (obstacle inflation, emit widths, self-check). "
                    "Read-only; cached on .kicad_dru and .kicad_pro mtime/size."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string"},
                    },
                    "required": ["project_path"],
                },
                "handler": self._tool_get_drc_constraints,
            },
            "get_kicad_system_resources": {
                "description": (
                    "Phase 7.8 memory planner probe - the hardware this box actually has RIGHT NOW: "
                    "core count, FREE (not installed) system RAM, and GPU/VRAM. Probed fresh on every "
                    "call and never cached or written to any JSON, so a run on a different machine (or "
                    "the same machine under different load - a desktop compositor holds VRAM) plans "
                    "itself from scratch; the JSONs carry only budget OVERRIDES a user chose. The GPU "
                    "block reports availability, the CUDA array module in use (cupy; torch is detected "
                    "and named but not driven), free/total VRAM, the probe source (cupy | nvidia-smi | "
                    "none), and - when unavailable - the REASON, so 'a GPU is present but no array "
                    "module is installed' is distinguishable from 'no GPU'. Use it to explain a slow "
                    "or CPU-only routing run ('batches were tiny because only 1.1 GB VRAM was free'). "
                    "Read-only; takes no arguments and touches no project files."
                ),
                "inputSchema": {"type": "object", "properties": {}},
                "handler": self._tool_get_system_resources,
            },
            "route_kicad_board": {
                "description": (
                    "Phase 7.17 - the ONE command to route the board (also a CLI: "
                    "`python kicad_router_tool.py route <project> [--write] [--nets ...] [--effort ...]`). "
                    "A thin orchestrator over the ratsnest -> global (7.3a) -> detailed (7.3b, incl. rip-up) "
                    "pipeline (it duplicates no routing logic - route_kicad_nets already runs that whole "
                    "pipeline). Reports what was unrouted before, then routed/failed counts, routed length, "
                    "vias, and rip-up stats. `effort` (quick|balanced|best) currently tunes rip-up "
                    "aggressiveness (quick=single pass; balanced=config default; best=aggressive) and, when "
                    "optimize=true, is passed straight through to the optimizer's identically-named preset. "
                    "optimize=false (default) is byte-for-byte the pre-wiring behavior; optimize=true adds a "
                    "4th stage after detailed routing (and after the write, if any): a fresh "
                    "optimize_kicad_board session driven chunk by chunk until it reports converged or "
                    "budget_exhausted, reported under the result's `optimizer` key and in "
                    "pipeline.whole_board_optimization. If the optimizer hits awaiting_decision (a 7.7 AI "
                    "near-tie or a MANDATORY 7.14 pin-swap pause) route_board STOPS and surfaces the pending "
                    "decision - it never answers it for you; the session stays open for "
                    "get_kicad_route_session / decide_kicad_route. effort=best's 8-hour optimizer budget is "
                    "clamped to a synchronous-call-sized cap (reported in notes). Plane-aware routing (7.5) "
                    "and stitching (7.5.6) are still declared TODO pipeline hooks (M4), not yet "
                    "wired - the report's `pipeline` block says so. write=false (default) previews without "
                    "touching the board (including the optimizer stage, which is scratch-only); reversible via "
                    "unroute_kicad_nets. allow_hand_copper_ripup (default false) opts in to letting "
                    "rip-up remove hand-routed track/via copper (never footprint pads, zone fills, or "
                    "Edge.Cuts) when it is the actual blocker - a per-call opt-in, not a persisted "
                    "setting, since it is materially more destructive than ripping the autorouter's own "
                    "prior placements (the default behavior). Every piece ripped is reported in "
                    "human_copper_ripped (uuid/net/kind/layer/geometry) - review that BEFORE trusting "
                    "write=true."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string"},
                        "nets": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Restrict routing to these net names; omit for every unrouted connection.",
                        },
                        "write": {"type": "boolean", "default": False},
                        "effort": {
                            "type": "string",
                            "enum": ["quick", "balanced", "best"],
                            "default": "balanced",
                        },
                        "allow_while_open": {"type": "boolean", "default": False},
                        "allow_hand_copper_ripup": {
                            "type": "boolean",
                            "default": False,
                            "description": (
                                "Opt in (per-call, default off) to letting rip-up remove hand-routed "
                                "track/via copper when it is the blocker. Never removes pads, zone "
                                "fills, or Edge.Cuts even when true. See human_copper_ripped in the "
                                "report for exactly what was/would be removed."
                            ),
                        },
                        "optimize": {
                            "type": "boolean",
                            "default": False,
                            "description": (
                                "Opt in (per-call, default off) to running the Phase 7.6 whole-board "
                                "optimizer after detailed routing, driven to converged/budget_exhausted. "
                                "An awaiting_decision pause is surfaced under `optimizer`, never "
                                "auto-answered. Nothing is persisted unless write is also true."
                            ),
                        },
                    },
                    "required": ["project_path"],
                },
                "handler": self._tool_route_board,
            },
            "route_kicad_nets": {
                "description": (
                    "Phase 7.3b detailed (fine, windowed) autorouter: route unrouted connections into "
                    "exact copper. For each connection (from get_kicad_ratsnest, filtered by nets, or a "
                    "supplied connections list, in priority-desc/airline-asc order) it builds a per-connection "
                    "obstacle window (bbox + search_window_margin_mm, doubling up to the whole board on "
                    "failure), pad-escapes both endpoints to the nearest legal grid node, runs an "
                    "integer-milli-cost A* over (x,y,layer) softly constrained to the global stage's corridor "
                    "(layer-purpose/off-direction/away-from-home/via costs), then SELF-CHECKS every proposed "
                    "segment/via against ALL copper at netclass clearance BEFORE any write. Clearance resolves "
                    "from the Default net-class (never a bare merged 0). Emits simplified (segment)/(via) blocks "
                    "via top-level surgery and records their uuids in board-local autorouter_owned (undo with "
                    "unroute_kicad_nets). Note: on plane-filled layers only pour-free channels route (full "
                    "plane-aware routing and PathFinder rip-up are later phases; window-doubling retry only). "
                    "write=false (default) previews per-connection routed/length/vias/layers/self-check/failures "
                    "without touching the board - always preview first. Writes ONLY to the given project's board."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string"},
                        "nets": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Restrict routing to these net names; omit for every unrouted connection.",
                        },
                        "write": {"type": "boolean", "default": False},
                        "allow_while_open": {"type": "boolean", "default": False},
                        "max_ripup_iterations": {"type": "integer"},
                        "allow_hand_copper_ripup": {
                            "type": "boolean",
                            "default": False,
                            "description": (
                                "Opt in (per-call, default off) to letting rip-up remove hand-routed "
                                "track/via copper (never pads/zone fills/Edge.Cuts) when it is the "
                                "blocker. See the result's human_copper_ripped list for exactly what "
                                "was/would be removed."
                            ),
                        },
                    },
                    "required": ["project_path"],
                },
                "handler": self._tool_route_nets,
            },
            "unroute_kicad_nets": {
                "description": (
                    "Delete autorouter-owned copper (the undo for route_kicad_nets). Removes only "
                    "segments/vias recorded in board-local autorouter_owned - human-routed copper is never "
                    "touched. Pass nets to restrict to specific net names; omit to remove all autorouter-owned "
                    "copper. write=false (default) previews the uuids that would be removed without touching "
                    "the board."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string"},
                        "nets": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Restrict deletion to these net names; omit to remove all autorouter-owned copper.",
                        },
                        "write": {"type": "boolean", "default": False},
                        "allow_while_open": {"type": "boolean", "default": False},
                    },
                    "required": ["project_path"],
                },
                "handler": self._tool_unroute_nets,
            },
            "open_kicad_route_viewer": {
                "description": (
                    "Phase 7.9 - open the live route-progress viewer: a tkinter window that redraws the "
                    "board's copper and progress bars as route_kicad_nets/route_kicad_board run, tailing "
                    "the JSONL event stream those tools append to (<board>.route_progress.jsonl, gated by "
                    "pcb_settings.json's autorouter.progress.events). Spawns a DETACHED "
                    "`python kicad_route_viewer.py <board_path>` subprocess - decoupled by construction: "
                    "the router never talks to the viewer directly, only ever appends to that file, so the "
                    "viewer can be closed/reopened mid-run, survives server restarts (it replays the file "
                    "to catch up), and can never block or crash a route. Its 'Stop after this iteration' "
                    "button writes a cancel flag into the board-local JSON that route_kicad_nets polls "
                    "between connections - the safe cancel path a headless MCP session otherwise lacks. "
                    "Also auto-launched by route_kicad_nets/route_kicad_board when "
                    "autorouter.progress.open_viewer is true. Observational only: a viewer bug or a missing "
                    "tkinter (headless CI/container) can never corrupt a route or crash this server - if "
                    "tkinter is unavailable this reports launched=false with a reason instead of raising."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string"},
                        "board": {
                            "type": "string",
                            "description": "Explicit .kicad_pcb path to view, if it differs from project_path's board.",
                        },
                    },
                    "required": ["project_path"],
                },
                "handler": self._tool_open_route_viewer,
            },
            "propose_kicad_plane": {
                "description": (
                    "Phase 7.5.5 - propose a candidate copper-pour plane for `net` on `layer` "
                    "(read-only; never writes; no ownership restriction, unlike create/modify - it's a "
                    "suggestion, even against a hand-made zone/net). Candidate outline: a grid-based "
                    "coverage region of the net's own pads/vias on the layer (bounding box inflated by "
                    "pad/via reach plus a margin, clipped to the board's Edge.Cuts extents). Omit layer "
                    "to auto-pick a copper layer whose layer_purpose TYPE matches the net's own kind "
                    "(7.2 - power nets prefer power-type layers). Runs the same 7.5.2 estimation "
                    "pipeline audit_kicad_plane_islands uses (minus higher-priority zones and "
                    "clearance-inflated foreign copper) to report components (mainland/island/orphan, "
                    "7.5.3 costing) and a cost_delta vs the net's current routed trace cost "
                    "(get_kicad_trace_cost) - negative means the plane looks cheaper. Simplified "
                    "estimate, not a re-route simulation."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string"},
                        "net": {"type": "string"},
                        "layer": {
                            "type": "string",
                            "description": "Copper layer to propose on; omit to auto-pick by net kind / layer purpose.",
                        },
                    },
                    "required": ["project_path", "net"],
                },
                "handler": self._tool_propose_plane,
            },
            "create_kicad_plane": {
                "description": (
                    "Phase 7.5.5 - create a new copper-pour zone for `net` on `layer` from "
                    "propose_kicad_plane's candidate outline, copying an existing board zone's "
                    "fill-setting shape (hatch, connect_pads clearance, min_thickness, thermal gap, "
                    "smoothing) so it looks native - the create_kicad_netclass 'copy Default's shape' "
                    "pattern, applied to zones. write=false (default) returns the exact (zone ...) text "
                    "block that would be appended, without touching the board. write=true appends it "
                    "(uuid-anchored top-level surgery) and records the new zone's uuid in board-local "
                    "autorouter_owned.zones - the only thing that makes it eligible for a later "
                    "modify_kicad_plane call. IMPORTANT: writing the zone does not fill it with copper - "
                    "refill zones (Fill All Zones) and re-run DRC in KiCad afterward."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string"},
                        "net": {"type": "string"},
                        "layer": {
                            "type": "string",
                            "description": "Copper layer to create the zone on; omit to auto-pick by net kind / layer purpose.",
                        },
                        "name": {"type": "string", "description": "Zone name; defaults to autorouter_<net>_<layer>."},
                        "priority": {"type": "integer", "default": 0},
                        "write": {"type": "boolean", "default": False},
                        "allow_while_open": {"type": "boolean", "default": False},
                    },
                    "required": ["project_path", "net"],
                },
                "handler": self._tool_create_plane,
            },
            "modify_kicad_plane": {
                "description": (
                    "Phase 7.5.5 - move/grow/shrink and/or reprioritize an EXISTING zone by uuid, via "
                    "uuid-anchored s-expr surgery (same discipline as delete_kicad_group/"
                    "unroute_kicad_nets - only the (zone ...) block's polygon/priority is spliced, never "
                    "a full file re-serialize). REFUSES (raises, never silently proceeds) when uuid is "
                    "not recorded in board-local autorouter_owned.zones - this tool may ONLY move/resize "
                    "a zone create_kicad_plane itself created, never one of the six hand-made kiln zones "
                    "(mainGnd, safty_gnd, antenna, main3.3, main12v, 3.3v_safty); those can only be "
                    "*proposed* for change via propose_kicad_plane for a human to apply by hand in KiCad. "
                    "At least one of new_outline/priority must be given. write=false (default) previews "
                    "the new zone block text without touching the board; write=true applies it and "
                    "reminds the caller that KiCad must refill zones + re-run DRC afterward."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string"},
                        "uuid": {"type": "string"},
                        "new_outline": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {"x": {"type": "number"}, "y": {"type": "number"}},
                                "required": ["x", "y"],
                            },
                            "description": "New polygon outline (>=3 points) to replace the zone's (polygon (pts ...)).",
                        },
                        "priority": {"type": "integer"},
                        "write": {"type": "boolean", "default": False},
                        "allow_while_open": {"type": "boolean", "default": False},
                    },
                    "required": ["project_path", "uuid"],
                },
                "handler": self._tool_modify_plane,
            },
            "benchmark_kicad_autoroute": {
                "description": (
                    "Phase 7.16 - score route_board against a human-routed board (the user's north star: "
                    "'as well or better than my hand-routed board', judged by the get_kicad_trace_cost board "
                    "score). NEVER writes source_board - it is only ever copied (board + .kicad_pro + .net + "
                    "pcb_settings.json/board-local state, when present) into a fresh scratch directory before "
                    "anything measures or routes. mode='complete_only' (default, primary acceptance metric): "
                    "measures the HUMAN board's score/unrouted-connection count on the untouched scratch copy, "
                    "then route_board(write=True) routes only what the human left unrouted, then reports "
                    "completion %, copper length/vias added, the post-route board score, and the kicad-cli DRC "
                    "delta (baseline vs post new-violation count; auto-skipped, never failed, when kicad-cli "
                    "isn't on this machine). mode='strip_and_reroute': deletes ALL non-zone copper from the "
                    "scratch copy (every top-level segment/via/arc - zones/footprints/edge-cuts untouched), "
                    "reroutes the whole board from zero, and compares to the HUMAN ORIGINAL (measured before "
                    "stripping) on completion %, length, vias, Phase 6 board score (identical weights both "
                    "sides), per-layer utilization, DRC violation count, and runtime. Returns a hand-vs-auto "
                    "comparison dict; comparison.matched_or_beat_human (bool) and comparison.verdict (str) are "
                    "the first-class pass/fail fields. Full-kiln runs are slow (route_board detailed A* is "
                    "pure-Python) - prefer a small project for quick checks."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "source_board": {"type": "string", "description": "Project path/directory to benchmark - never written."},
                        "mode": {
                            "type": "string",
                            "enum": ["complete_only", "strip_and_reroute"],
                            "default": "complete_only",
                        },
                        "effort": {
                            "type": "string",
                            "enum": ["quick", "balanced", "best"],
                            "default": "balanced",
                        },
                        "scratch_dir": {
                            "type": "string",
                            "description": "Optional explicit scratch directory; omit for a fresh tempfile.mkdtemp().",
                        },
                    },
                    "required": ["source_board"],
                },
                "handler": self._tool_benchmark_autoroute,
            },
            "optimize_kicad_board": {
                "description": (
                    "Phase 7.6/7.15 - iterative whole-board optimization. Optimizes ONE number: "
                    "S = sum of net trace costs (get_kicad_trace_cost's board total, incl. the 7.2 "
                    "layer-purpose penalties) + sum of plane island costs (audit_kicad_plane_islands) + "
                    "optimizer.unrouted_penalty x unrouted-connection count (get_kicad_ratsnest) - every "
                    "term already defined in pcb_settings.json, so 'best' is exactly what the JSON says "
                    "it is. Each iteration ranks nets by cost contribution (routed nets at their trace "
                    "cost, unrouted nets at their penalty), takes optimizer.worst_k, and generates up to "
                    "six candidate moves: rip-up+reroute in a perturbed order, reroute a whole bus bundle "
                    "on its Phase 5 corridor, swap a net's layer (layer-purpose driven), add a stitching "
                    "via to a plane island, create a plane for a power net that prices below its copper, "
                    "and resize an autorouter-owned zone. Every candidate is scored on its own private "
                    "copy of the board; acceptance follows optimizer.accept ('greedy' = strict "
                    "improvements only; 'sa' = simulated annealing, worse moves accepted with probability "
                    "exp(-dS/T), T *= sa_cooling per iteration). `seed` makes a run fully reproducible. "
                    "RESUMABLE, never a marathon: one call runs at most max_iterations_per_call "
                    "iterations or max_seconds of wall clock and returns {session_id, state, "
                    "score_curve, moves, diff}, with state in "
                    "running|converged|budget_exhausted|awaiting_decision (converged now also fires "
                    "on Phase 7.15's plateau rule - trailing-window mean improvement rate below "
                    "optimizer.plateau_slope_ratio x its reference rate, distinguishable from the "
                    "convergence_delta floor via stop_reason - alongside the effort preset "
                    "quick|balanced|best, see the effort parameter); pass "
                    "the session_id back to continue exactly where it stopped (RNG state and all loop "
                    "state checkpoint to the board-local JSON, so a session survives an MCP restart and "
                    "is inspectable via get_kicad_route_session). ALL iteration happens on a private "
                    "scratch copy of the project - write=false (the default) never touches the real "
                    "board on any call. write=true applies the session's FINAL accepted state (copper, "
                    "vias and zones together) onto the real board and records it all in "
                    "autorouter_owned so unroute_kicad_nets still undoes it; it refuses while the "
                    "session is still running or if the real board changed since the session started. "
                    "Refill zones + re-run DRC in KiCad after write=true. Human-routed copper and the "
                    "hand-made zones are read-only inputs throughout - the moves reuse "
                    "unroute_kicad_nets/modify_kicad_plane, whose own ownership guards they never "
                    "bypass. Phase 7.7 (AI in the loop): when the top two candidate moves are within "
                    "optimizer.ai_decisions.min_score_spread - i.e. the cost model genuinely cannot "
                    "separate them - the call returns state 'awaiting_decision' with a pending_decision "
                    "holding 2-4 pre-scored options (id, one-line summary, score delta); answer it with "
                    "decide_kicad_route, or simply call this tool again to defer to the best-scored "
                    "option. Clear winners are auto-taken exactly as before, max_pauses_per_run caps the "
                    "escalation budget, and ai_decisions.enabled:false disables pausing entirely. Every "
                    "committed move - auto or AI-decided - is appended to decision_log for audit/replay. "
                    "Phase 7.14 (pin-swap advisor, OFF unless pin_swap.enabled is true in "
                    "pcb_settings.json): a seventh 'move' that this tool can never apply. It looks for "
                    "two SIGNAL nets on different pins of one detect_kicad_connectors connector "
                    "(power/ground pins are excluded outright) whose swap would score better, prices "
                    "each candidate as a controlled A/B on two disposable board copies - both arms "
                    "strip and reroute the same two nets, the swap arm additionally trades the two "
                    "pads' nets - and if the gain reaches pin_swap.min_gain returns state "
                    "'awaiting_decision' with decision_type 'pin_swap'. That pause is MANDATORY: "
                    "unlike every other decision type it is not gated by ai_decisions (not by "
                    "min_score_spread, not by max_pauses_per_run, not by the decision_types "
                    "allowlist), because the tool cannot apply a pin swap even when it is a clear "
                    "winner - only YOU can, by editing the schematic and re-exporting the netlist. "
                    "Answer opt1 to decline or opt2 to report you made the change, after which the "
                    "session re-syncs its board against the real board's new pad-net assignment and "
                    "continues. Sub-min_gain swaps are recorded in pin_swap_reports and never "
                    "proposed. The schematic and the real .net file are NEVER written on any path, "
                    "and write=true additionally refuses outright if the session's board and the "
                    "real board disagree about any pad's net."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string"},
                        "session_id": {
                            "type": "string",
                            "description": "Resume this session; omit to start a new one.",
                        },
                        "max_iterations_per_call": {"type": "integer", "default": 3},
                        "max_seconds": {
                            "type": "number",
                            "description": "Wall-clock bound for THIS call; omit for iterations-only bounding.",
                        },
                        "seed": {"type": "integer", "description": "Overrides optimizer.seed for a new session."},
                        "accept": {"type": "string", "enum": ["greedy", "sa"]},
                        "max_iterations": {
                            "type": "integer",
                            "description": "Total SESSION iteration budget; defaults to optimizer.max_iterations.",
                        },
                        "time_budget_s": {
                            "type": "number",
                            "description": "Total SESSION time budget; defaults to optimizer.time_budget_s.",
                        },
                        "effort": {
                            "type": "string",
                            "enum": ["quick", "balanced", "best"],
                            "description": (
                                "Phase 7.15 - new-session-only preset bundling the other optimizer knobs "
                                "(an explicit accept/max_iterations/time_budget_s above still wins over "
                                "it): quick=max_iterations 5 + greedy; balanced (default)=today's "
                                "optimizer.* settings unchanged; best=simulated annealing + an 8h time "
                                "budget. Defaults to optimizer.effort in pcb_settings.json ('balanced')."
                            ),
                        },
                        "pin_swap_exclusions": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Phase 7.14 - connector refs (e.g. ['J2','J7']) the pin-swap "
                                "advisor must never propose a swap on. Resolved against "
                                "detect_kicad_connectors at session creation; an unresolved name "
                                "RAISES and lists the board's detected refs rather than being "
                                "silently dropped. New-session only; ignored unless "
                                "pin_swap.enabled is true in pcb_settings.json."
                            ),
                        },
                        "write": {"type": "boolean", "default": False},
                        "allow_while_open": {"type": "boolean", "default": False},
                    },
                    "required": ["project_path"],
                },
                "handler": self._tool_optimize_board,
            },
            "get_kicad_route_session": {
                "description": (
                    "Phase 7.6 - READ-ONLY report of an optimize_kicad_board session: its state "
                    "(running|converged|budget_exhausted), iteration count vs budget, elapsed time, seed, "
                    "acceptance policy, SA temperature, initial/current/best score, the per-iteration "
                    "score curve, the full move log (what was tried, whether it was accepted and why), "
                    "and the scratch board it is iterating on. Does NOT advance the session by a single "
                    "iteration - use optimize_kicad_board for that. Omit session_id to report the most "
                    "recently touched session on this board; returns found:false (never raises) when the "
                    "board has no optimizer session yet."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string"},
                        "session_id": {
                            "type": "string",
                            "description": "Omit for the board's most recently touched session.",
                        },
                    },
                    "required": ["project_path"],
                },
                "handler": self._tool_get_route_session,
            },
            "decide_kicad_route": {
                "description": (
                    "Phase 7.7 - answer an optimize_kicad_board session that is paused in the "
                    "'awaiting_decision' state. The pending_decision is a CLOSED list of 2-4 candidate "
                    "moves the optimizer already applied and scored on private board copies; you pick "
                    "one by its option id (e.g. 'opt2'), or pass the literal 'defer' to take the "
                    "optimizer's own best-scored default. You cannot introduce a move of your own - "
                    "`rationale` is free text that is RECORDED (in decision_log) and never executed. "
                    "Decisions fire only where the cost model cannot separate the options (score spread "
                    "under optimizer.ai_decisions.min_score_spread), which is exactly where judgement "
                    "the arithmetic does not have - EMI, serviceability, 'that jumper layer is for "
                    "rework wires', future hand-rework - beats the numbers. Applies the chosen candidate "
                    "and returns the session to 'running'; it runs NO further iterations, so call "
                    "optimize_kicad_board with the same session_id afterwards to continue. Raises if the "
                    "session is not awaiting a decision or the decision_id does not match the pending "
                    "one. Never touches the real board - like the rest of the optimizer it works on the "
                    "session's private scratch copy until optimize_kicad_board is called with write=true. "
                    "Phase 7.14: a pending_decision with decision_type 'pin_swap' is answered here too, "
                    "but it is ADVISORY and commits nothing - opt1 declines the proposed connector pin "
                    "swap, opt2 reports that the HUMAN made the change in the schematic and re-exported "
                    "the netlist, whereupon the session re-syncs its board against the real board's "
                    "current pad-net assignment (adopting it, never deciding it) and reports what "
                    "changed under `resync`. Answering opt2 without having made the change is harmless: "
                    "the re-sync finds nothing to adopt and says so. This is a question for the USER, "
                    "not for you - relay it rather than answering it on their behalf."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string"},
                        "session_id": {"type": "string", "description": "The paused session."},
                        "decision_id": {
                            "type": "string",
                            "description": "Must equal pending_decision.decision_id - a stale answer is refused.",
                        },
                        "choice": {
                            "type": "string",
                            "description": "An option id from pending_decision.options, or 'defer'.",
                        },
                        "rationale": {
                            "type": "string",
                            "description": "Why - recorded in decision_log, never executed.",
                        },
                    },
                    "required": ["project_path", "session_id", "decision_id", "choice"],
                },
                "handler": self._tool_decide_route,
            },
            "run_kicad_stitching_pass": {
                "description": (
                    "Phase 7.5.6 - the plane stitching pass. Run this LAST, once routing and plane "
                    "creation/moves have converged (after route_kicad_board / optimize_kicad_board), "
                    "never mid-routing - a stitching via placed early just becomes an obstacle for "
                    "routing that comes after it. Three ordered steps (skipped entirely, reporting "
                    "enabled:false, when stitching.enabled is false): (1) island rescue - one via per "
                    "costed island/orphan audit_kicad_plane_islands already reports, at its own "
                    "suggested_stitching_via.position; (2) return-path stitching - vias near detect_kicad_"
                    "critical_nets' routed copper, placed on the same-layer power/ground PLANE at "
                    "stitching.near_high_speed_pitch_mm spacing within stitching.near_high_speed_mm of "
                    "the trace, wherever the candidate point actually lands inside that plane's own drawn "
                    "outline; (3) general stitching - a grid fill of each power/ground plane's outline "
                    "toward stitching.target_spacing_mm. Every via is autorouter_owned AND tagged "
                    "stitching:true - distinct from an ordinary routing via, a hand-placed via, and the "
                    "optimizer's own untagged move-(d) stitching via - so remove_kicad_stitching_vias can "
                    "target exactly these. write=false (default) previews the full plan (every via's "
                    "net/zone/layer/position, and for island rescue its projected cost) without touching "
                    "the board; write=true places every planned via for real. Refill zones + re-run DRC "
                    "in KiCad afterward, same as every other copper writer here. SESSION CONVENTION "
                    "(documented, not enforced): before routing or optimizing in an area that already "
                    "contains stitching vias (owned or foreign), ask the user whether to remove them "
                    "first via remove_kicad_stitching_vias - removed stitching copper is simply re-placed "
                    "by the next run of this pass, so nothing is lost by asking."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string"},
                        "write": {"type": "boolean", "default": False},
                    },
                    "required": ["project_path"],
                },
                "handler": self._tool_run_stitching_pass,
            },
            "remove_kicad_stitching_vias": {
                "description": (
                    "Undo for run_kicad_stitching_pass. Deletes ONLY autorouter-owned vias tagged "
                    "stitching:true in board-local autorouter_owned - never an ordinary routing via, "
                    "never a hand-placed via, and never the optimize_kicad_board optimizer's own untagged "
                    "move-(d) stitching via (that one belongs to an optimizer session, not this pass's "
                    "bookkeeping). area restricts the deletion to a region: a rect {x_min, x_max, y_min, "
                    "y_max} (any bound omittable) or a polygon {points: [[x, y], ...]}; omit for the whole "
                    "board. write=false (default) previews the uuids that would be removed without "
                    "touching the board. include_foreign=true additionally LISTS (never deletes) every "
                    "OTHER via in the resolved area this tool does not own, using this codebase's existing "
                    "free/oversized via heuristic (net=='' means an unconnected stitching/mounting via; "
                    "more than 3x the Default netclass via diameter means oversized) - the same "
                    "get_kicad_track_inventory characterization, applied here so a real board's already-"
                    "present freestanding vias (kiln has 3 known free/oversized ones) surface for a human "
                    "to confirm removal one at a time. This is NOT a full connectivity trace. SESSION "
                    "CONVENTION (documented, not enforced): before routing or optimizing in an area "
                    "containing stitching vias (owned or foreign), the calling session should ask the "
                    "user whether to remove them first - the same kind of contract as route_kicad_nets' "
                    "allow_hand_copper_ripup opt-in; this tool performs the deletion asked of it, the "
                    "'ask first' step is the calling session's own responsibility."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string"},
                        "area": {
                            "type": "object",
                            "description": (
                                "Omit for the whole board. Rect: {x_min, x_max, y_min, y_max} (any bound "
                                "omittable). Polygon: {points: [[x, y], ...]}."
                            ),
                        },
                        "write": {"type": "boolean", "default": False},
                        "include_foreign": {"type": "boolean", "default": False},
                        "allow_while_open": {"type": "boolean", "default": False},
                    },
                    "required": ["project_path"],
                },
                "handler": self._tool_remove_stitching_vias,
            },
            "propose_kicad_netclass": {
                "description": (
                    "Propose a net-class definition from a confirmed net list (a detect_kicad_buses "
                    "candidate's members, or hand-picked nets): track_width is the length-weighted "
                    "dominant width across member nets, via_diameter/via_drill is the most-used via size "
                    "on those nets (falls back to the project's Default class), clearance is inherited "
                    "from Default. Reports conflicts when member nets differ in routed width, so the "
                    "user chooses rather than the tool silently averaging. Also returns the project-wide "
                    "track/via inventory so a caller can offer previously-used values as menu options."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string"},
                        "nets": {"type": "array", "items": {"type": "string"}, "description": "Exact net names, e.g. [\"/MainControler/MOSI\", \"/MainControler/MISO\"]."},
                        "name": {"type": "string", "description": "Proposed net class name, e.g. \"SPI_MainControler\"."},
                    },
                    "required": ["project_path", "nets", "name"],
                },
                "handler": self._tool_propose_netclass,
            },
            "create_kicad_netclass": {
                "description": (
                    "Create a KiCad net class by editing <project>.kicad_pro JSON: appends a class to "
                    "net_settings.classes (copying the Default class's full key shape, overriding name/"
                    "track_width/via_diameter/via_drill/clearance from settings), and adds one exact, "
                    "regex-escaped, anchored pattern (^<net>$) per net in net_patterns to "
                    "net_settings.netclass_patterns. Refuses if the class name already exists. Defaults "
                    "to write=false (dry run) returning a before/after diff of the affected JSON blocks; "
                    "write=true saves with KiCad's own indent=2/sort_keys formatting. IMPORTANT: KiCad "
                    "only reloads net classes when the project is reopened, and creating a class changes "
                    "only the rules - it does not resize any already-routed copper."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string"},
                        "name": {"type": "string"},
                        "settings": {
                            "type": "object",
                            "description": "Overrides for the new class: track_width, via_diameter, via_drill, clearance (mm). Unset keys copy Default's value.",
                            "properties": {
                                "track_width": {"type": "number"},
                                "via_diameter": {"type": "number"},
                                "via_drill": {"type": "number"},
                                "clearance": {"type": "number"},
                            },
                        },
                        "net_patterns": {"type": "array", "items": {"type": "string"}, "description": "Exact net names to assign to this class, one anchored pattern per net."},
                        "write": {"type": "boolean", "default": False},
                        "allow_while_open": {"type": "boolean", "default": False, "description": "Skip the check that refuses to write while KiCad has the board or project open."},
                    },
                    "required": ["project_path", "name", "settings", "net_patterns"],
                },
                "handler": self._tool_create_netclass,
            },
            "audit_kicad_netclass_conformance": {
                "description": (
                    "For every routed net, resolve its assigned net class via <project>.kicad_pro's "
                    "netclass_patterns (first regex match wins, same precedence as KiCad; unmatched nets "
                    "fall back to Default), then compare that class's track_width/via_diameter/via_drill "
                    "against the net's actual routed dominant values (get_kicad_net_track_widths). "
                    "Reports per-net mismatches, e.g. 'net is in class SPI (0.2 mm) but routed at 0.3 "
                    "mm.' Read-only."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string"},
                    },
                    "required": ["project_path"],
                },
                "handler": self._tool_audit_netclass_conformance,
            },
            "search_kicad_component": {
                "description": "Search for a component by reference designator and return its line numbers in the PCB file. Use this to efficiently locate component sections without reading the entire file.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string"},
                        "reference": {"type": "string"},
                    },
                    "required": ["project_path", "reference"],
                },
                "handler": self._tool_search_component,
            },
            "get_kicad_hierarchical_group": {
                "description": (
                    "Given a component reference, return every other footprint that belongs to the same "
                    "hierarchical-sheet instance (e.g. all the parts of one relay channel, or one thermocouple "
                    "channel), matched via the schematic path rather than board position. Use this before trying "
                    "to reorganize a repeated sub-circuit's layout, to find its true member list without "
                    "guessing from proximity."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string"},
                        "reference": {"type": "string"},
                        "verbose": {"type": "boolean", "default": False, "description": "Include full KiCad properties (Datasheet, Mouser part numbers, Sim.* fields) per component. Leave false unless you actually need them - it's the largest cost in the response."},
                    },
                    "required": ["project_path", "reference"],
                },
                "handler": self._tool_get_hierarchical_group,
            },
            "list_kicad_sibling_instances": {
                "description": (
                    "Given a component reference, find every other instance of the same hierarchical schematic "
                    "sheet (e.g. given one relay channel or one thermocouple channel, list the other channels "
                    "stamped from the same template page), with each sibling's own anchor reference/position."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string"},
                        "reference": {"type": "string"},
                    },
                    "required": ["project_path", "reference"],
                },
                "handler": self._tool_list_sibling_instances,
            },
            "diff_kicad_layout_template": {
                "description": (
                    "Dry-run: compute where every sibling of target_reference's hierarchical group should move "
                    "to match the relative layout (offsets AND rotations) of template_reference's group. "
                    "Rotates the whole offset pattern to account for a difference in the two anchors' own "
                    "rotation. Returns a list of changes; nothing is written. Use this to preview before calling "
                    "apply_kicad_layout_template."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string"},
                        "template_reference": {"type": "string", "description": "Reference of the anchor component in the known-good template group (e.g. a locked relay or chip)."},
                        "target_reference": {"type": "string", "description": "Reference of the anchor component in the group to be repositioned."},
                    },
                    "required": ["project_path", "template_reference", "target_reference"],
                },
                "handler": self._tool_diff_layout_template,
            },
            "apply_kicad_layout_template": {
                "description": (
                    "Reposition every sibling group's components to match template_reference's layout, one call "
                    "per target anchor listed in target_references. Defaults to write=false (dry run) - always "
                    "call it that way first and review apply_result/diffs before calling again with write=true "
                    "to actually modify the board file."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string"},
                        "template_reference": {"type": "string"},
                        "target_references": {"type": "array", "items": {"type": "string"}},
                        "write": {"type": "boolean", "default": False},
                        "allow_while_open": {"type": "boolean", "default": False, "description": "Skip the check that refuses to write while KiCad has this board open for editing (see get_kicad_ipc_status)."},
                    },
                    "required": ["project_path", "template_reference", "target_references"],
                },
                "handler": self._tool_apply_layout_template,
            },
            "apply_kicad_layout_changes": {
                "description": (
                    "Low-level: apply an explicit list of {reference or uuid, new_position:{x,y,rotation}} changes "
                    "(as returned in diff_kicad_layout_template's `changes`, or written by hand) to the board file. "
                    "Defaults to write=false to preview; call again with write=true to commit."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string"},
                        "changes": {"type": "array", "items": {"type": "object"}},
                        "write": {"type": "boolean", "default": False},
                        "allow_while_open": {"type": "boolean", "default": False, "description": "Skip the check that refuses to write while KiCad has this board open for editing (see get_kicad_ipc_status)."},
                    },
                    "required": ["project_path", "changes"],
                },
                "handler": self._tool_apply_layout_changes,
            },
            "list_kicad_hierarchical_templates": {
                "description": (
                    "Board-wide overview, in one call, of every schematic sheet stamped out more than once "
                    "(one row per repeated sheet file, with every instance's member references and whether it's "
                    "the fully-locked reference layout). Run this FIRST on any 'make these repeated sub-circuits "
                    "consistent' task instead of exploring with search/grep - it replaces the manual discovery "
                    "work of figuring out which components belong together and which instance is the reference."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string"},
                    },
                    "required": ["project_path"],
                },
                "handler": self._tool_list_hierarchical_templates,
            },
            "move_kicad_group": {
                "description": (
                    "Rigid-body move: shift every member of a component's hierarchical group together, keeping "
                    "their layout relative to each other. Use this (instead of diff/apply_kicad_layout_template) "
                    "when there's no separate known-good template to copy from - e.g. relocating an "
                    "already-correct cluster elsewhere on the board, or nudging one channel to clear a routing "
                    "conflict. Give dx/dy as a plain offset, or `to: {x, y}` to move the anchor to an absolute "
                    "position. Defaults to write=false to preview."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string"},
                        "reference": {"type": "string", "description": "Any component reference in the group to move."},
                        "dx": {"type": "number", "default": 0.0},
                        "dy": {"type": "number", "default": 0.0},
                        "drotation": {"type": "number", "default": 0.0, "description": "Additional rotation (degrees) applied to every member and the anchor."},
                        "to": {"type": "object", "properties": {"x": {"type": "number"}, "y": {"type": "number"}}, "description": "Move the anchor to this absolute position instead of using dx/dy."},
                        "write": {"type": "boolean", "default": False},
                        "allow_while_open": {"type": "boolean", "default": False, "description": "Skip the check that refuses to write while KiCad has this board open for editing (see get_kicad_ipc_status)."},
                    },
                    "required": ["project_path", "reference"],
                },
                "handler": self._tool_move_group,
            },
            "list_kicad_groups": {
                "description": (
                    "List every top-level PCB group already on the board (KiCad's Ctrl+G grouping construct, "
                    "which lets a cluster of footprints be selected/moved as one unit). Each member uuid is "
                    "resolved back to its reference designator. Use this before create_kicad_group to check "
                    "whether components are already grouped, or to find a group's exact name/uuid before "
                    "deleting it."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string"},
                    },
                    "required": ["project_path"],
                },
                "handler": self._tool_list_groups,
            },
            "create_kicad_group": {
                "description": (
                    "Create a new named PCB group containing the given footprint references, so they "
                    "select/move together as one unit in the KiCad GUI - the same construct KiCad itself "
                    "writes for Ctrl+G. Typical flow: get_kicad_hierarchical_group to find a sub-circuit's "
                    "members, then create_kicad_group to group them (e.g. group every part of one thermocouple "
                    "or relay channel). Raises if a reference isn't found on the board, or already belongs to "
                    "another group. Defaults to write=false (dry run) - always preview first, then call again "
                    "with write=true to save."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string"},
                        "name": {"type": "string", "description": "Group name shown in the KiCad GUI (can be empty string, matching KiCad's default for GUI-created groups)."},
                        "references": {"type": "array", "items": {"type": "string"}, "description": "Footprint reference designators to include, e.g. [\"R1\", \"C4\", \"U2\"]."},
                        "write": {"type": "boolean", "default": False},
                        "allow_while_open": {"type": "boolean", "default": False, "description": "Skip the check that refuses to write while KiCad has this board open for editing (see get_kicad_ipc_status)."},
                    },
                    "required": ["project_path", "name", "references"],
                },
                "handler": self._tool_create_group,
            },
            "get_kicad_footprint_pads": {
                "description": (
                    "Get every pad of a footprint - number, net (read straight off the board file's own pad "
                    "entries, not the schematic pin numbering), and absolute board position. Use this whenever "
                    "a placement decision depends on exactly where a pin is (e.g. an IC's pins), not just where "
                    "the footprint's origin is."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string"},
                        "reference": {"type": "string"},
                    },
                    "required": ["project_path", "reference"],
                },
                "handler": self._tool_get_footprint_pads,
            },
            "get_kicad_pin_position": {
                "description": "Look up one pad's net and absolute board position by reference + pin number.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string"},
                        "reference": {"type": "string"},
                        "pin": {"type": "string"},
                    },
                    "required": ["project_path", "reference", "pin"],
                },
                "handler": self._tool_get_pin_position,
            },
            "get_kicad_pin_distance": {
                "description": (
                    "Euclidean distance between two specific pads. Use to check a placement's quality "
                    "before/after - e.g. confirming a bypass cap's pad ended up closer to the IC pin it bypasses."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string"},
                        "reference_a": {"type": "string"},
                        "pin_a": {"type": "string"},
                        "reference_b": {"type": "string"},
                        "pin_b": {"type": "string"},
                    },
                    "required": ["project_path", "reference_a", "pin_a", "reference_b", "pin_b"],
                },
                "handler": self._tool_pin_distance,
            },
            "align_kicad_component_pin": {
                "description": (
                    "Rigid-move a component (translate, and optionally rotate first) so that one of its pads "
                    "ends up exactly at a given absolute board position. Core primitive for datasheet-guided "
                    "placement: point a passive's pad at the IC pin/pad it needs to reach instead of eyeballing "
                    "footprint-origin offsets. Defaults to write=false (dry run) - review `change`, then call "
                    "again with write=true to commit."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string"},
                        "reference": {"type": "string"},
                        "pin": {"type": "string"},
                        "target": {"type": "object", "properties": {"x": {"type": "number"}, "y": {"type": "number"}}, "required": ["x", "y"]},
                        "rotation": {"type": "number", "description": "Optional new footprint rotation (degrees), applied before the translate."},
                        "write": {"type": "boolean", "default": False},
                        "allow_while_open": {"type": "boolean", "default": False, "description": "Skip the check that refuses to write while KiCad has this board open for editing (see get_kicad_ipc_status)."},
                    },
                    "required": ["project_path", "reference", "pin", "target"],
                },
                "handler": self._tool_align_component_pin,
            },
            "align_kicad_components_to_anchor": {
                "description": (
                    "Batch-place support components relative to one anchor's pins - e.g. arrange every "
                    "capacitor/resistor/inductor around a regulator IC to mirror a datasheet layout guide. Each "
                    "alignments entry: {reference, pin, anchor_pin, offset:{dx,dy} (default 0,0), rotation "
                    "(optional degrees)}. Target = anchor_pin's absolute pad position + offset; `reference`'s "
                    "`pin` pad is placed there. Defaults to write=false (dry run) - review `results`, then call "
                    "again with write=true to commit all of them."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string"},
                        "anchor_reference": {"type": "string"},
                        "alignments": {"type": "array", "items": {"type": "object"}},
                        "write": {"type": "boolean", "default": False},
                        "allow_while_open": {"type": "boolean", "default": False, "description": "Skip the check that refuses to write while KiCad has this board open for editing (see get_kicad_ipc_status)."},
                    },
                    "required": ["project_path", "anchor_reference", "alignments"],
                },
                "handler": self._tool_align_components_to_anchor,
            },
            "classify_kicad_group_by_anchor_pin": {
                "description": (
                    "For every other member of a hierarchical group, find which of the anchor's own pads it "
                    "shares a net with - i.e. its electrical role (VIN cap, feedback divider resistor, etc.), "
                    "read straight off board nets. Automatic version of hand-building a 'which part goes with "
                    "which IC pin' table - usually you want match_kicad_group_members_by_role or "
                    "diff_kicad_layout_by_role instead of calling this directly."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string"},
                        "anchor_reference": {"type": "string"},
                    },
                    "required": ["project_path", "anchor_reference"],
                },
                "handler": self._tool_classify_group_by_anchor_pin,
            },
            "match_kicad_group_members_by_role": {
                "description": (
                    "Match components between two hierarchical groups by which anchor pin they connect to, "
                    "instead of KiCad's symbol_uuid (which only works between instances of the *same* schematic "
                    "sheet). Works even when the two groups are on entirely different sheet files, as long as "
                    "their anchors share a compatible pinout - e.g. two independently-drawn but functionally "
                    "analogous regulator circuits. Ties (more than one same-footprint candidate on either side) "
                    "are broken by matching component value; anything still tied comes back under `ambiguous` "
                    "instead of being guessed - pass `overrides` ({template_reference: target_reference}) to "
                    "force those once you've eyeballed which is which."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string"},
                        "template_reference": {"type": "string"},
                        "target_reference": {"type": "string"},
                        "overrides": {"type": "object", "description": "{template_reference: target_reference} forced pairings for ambiguous ties."},
                    },
                    "required": ["project_path", "template_reference", "target_reference"],
                },
                "handler": self._tool_match_group_members_by_role,
            },
            "diff_kicad_layout_by_role": {
                "description": (
                    "Like apply_kicad_layout_template's diff step, but for two hierarchical groups on *different* "
                    "schematic sheets - matches members by anchor-pin role (match_kicad_group_members_by_role) "
                    "instead of shared symbol_uuid, then carries the template group's relative layout (offsets + "
                    "rotations) over onto the target anchor's own position. Check `ambiguous`/`template_unmatched`/"
                    "`target_unmatched` before trusting `changes` is complete. `changes` is ready to hand straight "
                    "to apply_kicad_layout_changes."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string"},
                        "template_reference": {"type": "string"},
                        "target_reference": {"type": "string"},
                        "overrides": {"type": "object", "description": "{template_reference: target_reference} forced pairings for ambiguous ties."},
                    },
                    "required": ["project_path", "template_reference", "target_reference"],
                },
                "handler": self._tool_diff_layout_by_role,
            },
            "estimate_kicad_footprint_radius": {
                "description": (
                    "Best-effort collision-check radius (mm) for a footprint: a known-good manual override for "
                    "packages where pad span badly underestimates body size (electrolytic cans, connectors), else "
                    "a size parsed out of a standard KiCad SMD footprint name, else a pad-bounding-box estimate, "
                    "else a conservative 2.0mm default."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string"},
                        "reference": {"type": "string"},
                    },
                    "required": ["project_path", "reference"],
                },
                "handler": self._tool_estimate_footprint_radius,
            },
            "find_kicad_layout_collisions": {
                "description": (
                    "Collision-check a set of footprints (typically one hierarchical group's members) both "
                    "against each other and against any *other* board component nearby - catching e.g. a group's "
                    "inductor ending up on top of an unrelated connector from a different subsystem. Uses "
                    "estimate_kicad_footprint_radius for every part's envelope, so no radius table needs to be "
                    "built by the caller. Read-only."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string"},
                        "references": {"type": "array", "items": {"type": "string"}},
                        "extra_search_radius": {"type": "number", "default": 25.0, "description": "mm; how far to look for outside obstacles."},
                        "margin": {"type": "number", "default": 0.4, "description": "mm; required clearance between envelopes."},
                    },
                    "required": ["project_path", "references"],
                },
                "handler": self._tool_find_layout_collisions,
            },
            "nudge_kicad_to_clear": {
                "description": (
                    "Move a component the minimum distance needed to clear a collision, searching outward in a "
                    "ring from its *current* position so it stays as close as possible to wherever it already "
                    "was (usually intentional) rather than being fully re-placed. Obstacles default to every "
                    "other board component within search_radius mm; pass avoid_references for an explicit list "
                    "instead. Defaults to write=false (dry run) - review `new_position`, then call again with "
                    "write=true to commit."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string"},
                        "reference": {"type": "string"},
                        "avoid_references": {"type": "array", "items": {"type": "string"}},
                        "search_radius": {"type": "number", "default": 25.0},
                        "margin": {"type": "number", "default": 0.4},
                        "max_search_radius": {"type": "number", "default": 20.0},
                        "write": {"type": "boolean", "default": False},
                        "allow_while_open": {"type": "boolean", "default": False, "description": "Skip the check that refuses to write while KiCad has this board open for editing (see get_kicad_ipc_status)."},
                    },
                    "required": ["project_path", "reference"],
                },
                "handler": self._tool_nudge_to_clear,
            },
            "delete_kicad_group": {
                "description": (
                    "Delete a top-level PCB group by name or uuid - only the grouping is removed, member "
                    "footprints are untouched. Give group_uuid when multiple groups share a name (common for "
                    "unnamed \"\" groups); name alone must match exactly one group. Defaults to write=false "
                    "(dry run) - preview the matched group, then call again with write=true to save."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string"},
                        "name": {"type": "string"},
                        "group_uuid": {"type": "string"},
                        "write": {"type": "boolean", "default": False},
                        "allow_while_open": {"type": "boolean", "default": False, "description": "Skip the check that refuses to write while KiCad has this board open for editing (see get_kicad_ipc_status)."},
                    },
                    "required": ["project_path"],
                },
                "handler": self._tool_delete_group,
            },
            "get_kicad_property_position": {
                "description": (
                    "Get a footprint's child text property's own local (at x y rotation) and layer - e.g. exactly "
                    "where the 'Reference' designator text sits on the silkscreen, relative to the footprint's own "
                    "origin. Different from get_kicad_component's position, which is the footprint's own placement, "
                    "not its label's offset. Use this to read a known-good instance's label placement before "
                    "copying it with diff_kicad_property_position_template."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string"},
                        "reference": {"type": "string"},
                        "property_name": {"type": "string", "default": "Reference"},
                    },
                    "required": ["project_path", "reference"],
                },
                "handler": self._tool_get_property_position,
            },
            "diff_kicad_property_position_template": {
                "description": (
                    "Dry-run: the silkscreen-label analogue of diff_kicad_layout_template. Compute which text-"
                    "property offsets (default 'Reference', pass e.g. 'Value' for others) in target_reference's "
                    "hierarchical group need to change to match template_reference's group - use this after a "
                    "reference instance's labels have been hand-decluttered to avoid overlaps, and you want to "
                    "copy that exact treatment onto sibling instances (e.g. other repeated channels). Matches "
                    "members by symbol_uuid like diff_kicad_layout_template. Any matched pair whose own footprint "
                    "rotation differs is reported under `skipped` rather than guessed at, since a label offset's "
                    "rotation does not transform under a simple linear rule. Returns `changes`; nothing is written "
                    "- pass `changes` to apply_kicad_property_position_changes, or use "
                    "apply_kicad_property_position_template to do both in one call."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string"},
                        "template_reference": {"type": "string"},
                        "target_reference": {"type": "string"},
                        "property_name": {"type": "string", "default": "Reference"},
                    },
                    "required": ["project_path", "template_reference", "target_reference"],
                },
                "handler": self._tool_diff_property_position_template,
            },
            "apply_kicad_property_position_changes": {
                "description": (
                    "Low-level: apply an explicit list of {reference or uuid, property, new_at:{x,y,rotation}} "
                    "changes (as returned in diff_kicad_property_position_template's `changes`, or written by "
                    "hand) to the matching child property's (at ...) line inside each footprint's block. Defaults "
                    "to write=false to preview; call again with write=true to commit."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string"},
                        "changes": {"type": "array", "items": {"type": "object"}},
                        "write": {"type": "boolean", "default": False},
                        "allow_while_open": {"type": "boolean", "default": False, "description": "Skip the check that refuses to write while KiCad has this board open for editing (see get_kicad_ipc_status)."},
                    },
                    "required": ["project_path", "changes"],
                },
                "handler": self._tool_apply_property_position_changes,
            },
            "apply_kicad_property_position_template": {
                "description": (
                    "Copy template_reference's group's text-property label offsets (default 'Reference') onto "
                    "every group in target_references, one call per target - the silkscreen-label analogue of "
                    "apply_kicad_layout_template. Example: after hand-decluttering U7's 'Reference' silkscreen "
                    "labels to stop them overlapping, apply_kicad_property_position_template(project_path, 'U7', "
                    "['U8','U9','U6']) copies that exact same label placement onto the matching component in each "
                    "sibling channel. Defaults to write=false (dry run) - review diffs/apply_result, then call "
                    "again with write=true to commit."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string"},
                        "template_reference": {"type": "string"},
                        "target_references": {"type": "array", "items": {"type": "string"}},
                        "property_name": {"type": "string", "default": "Reference"},
                        "write": {"type": "boolean", "default": False},
                        "allow_while_open": {"type": "boolean", "default": False, "description": "Skip the check that refuses to write while KiCad has this board open for editing (see get_kicad_ipc_status)."},
                    },
                    "required": ["project_path", "template_reference", "target_references"],
                },
                "handler": self._tool_apply_property_position_template,
            },
            "diff_kicad_flip_template": {
                "description": (
                    "Dry-run: find which members of target_reference's hierarchical group sit on the wrong copper "
                    "side (front/back) compared to their matching member (by symbol_uuid) in template_reference's "
                    "group - e.g. the template channel has some support parts deliberately flipped to the back to "
                    "save front-side space, and this target channel doesn't yet. Rotation mismatches between a "
                    "matched pair are reported under `skipped` rather than attempted. Returns `changes`; nothing "
                    "is written - pass to apply_kicad_flip_template."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string"},
                        "template_reference": {"type": "string"},
                        "target_reference": {"type": "string"},
                    },
                    "required": ["project_path", "template_reference", "target_reference"],
                },
                "handler": self._tool_diff_flip_template,
            },
            "apply_kicad_flip_template": {
                "description": (
                    "Flip every part of target_references' hierarchical groups that needs it to match "
                    "template_reference's group's front/back layer split, by CLONING the template member's "
                    "already-correctly-flipped footprint block (mirrored silkscreen/fab graphics, swapped F./B. "
                    "layer names, 'justify mirror' text flags, adjusted pad angles - everything KiCad's own Flip "
                    "command produces) onto the target footprint, while keeping the target's own identity: its "
                    "uuid, schematic path/sheetname/sheetfile, board position, and (matched by pad number) its own "
                    "net names. Use this instead of hand-deriving a flip transform - a text property's stored "
                    "rotation does not transform under mirroring by one fixed rule, so the only trustworthy source "
                    "for 'what does a correctly-flipped instance of this footprint look like' is an instance KiCad "
                    "itself already flipped. template_reference's group must already contain one for every role "
                    "that needs flipping. Defaults to write=false (dry run) - inspect flipped/failed, then call "
                    "again with write=true to commit."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string"},
                        "template_reference": {"type": "string"},
                        "target_references": {"type": "array", "items": {"type": "string"}},
                        "write": {"type": "boolean", "default": False},
                        "allow_while_open": {"type": "boolean", "default": False, "description": "Skip the check that refuses to write while KiCad has this board open for editing (see get_kicad_ipc_status)."},
                    },
                    "required": ["project_path", "template_reference", "target_references"],
                },
                "handler": self._tool_apply_flip_template,
            },
        }
        if _IPC_AVAILABLE:
            self.tools.update(self._ipc_tools())

    def _ipc_tools(self) -> dict[str, dict[str, Any]]:
        """Tools that talk to a *running* KiCad instance over the IPC API
        (kicad-python), instead of parsing kiln.kicad_pcb on disk. Require
        KiCad to be open with this board loaded and Preferences > Plugins >
        'Enable IPC API' turned on - every one of these fails fast with a
        clear message if that's not the case. None of them take a
        project_path; they operate on whatever board KiCad currently has
        open.
        """
        return {
            "get_kicad_ipc_status": {
                "description": (
                    "Check whether KiCad's IPC API is reachable right now, and report the "
                    "connected KiCad version and which board (if any) is open. Call this first "
                    "when any other get_kicad_live_*/find_kicad_live_*/*_kicad_live_* tool fails, "
                    "to tell 'KiCad isn't running/API disabled' apart from 'component not found'."
                ),
                "inputSchema": {"type": "object", "properties": {}},
                "handler": self._tool_get_ipc_status,
            },
            "get_kicad_live_bounding_box": {
                "description": (
                    "Real KiCad-computed bounding box (mm) for a footprint, straight from KiCad's "
                    "own geometry engine - accounts for actual pad/silkscreen/courtyard shapes and "
                    "rotation exactly, unlike estimate_kicad_footprint_radius's circle-from-name "
                    "heuristic. Use for oddly-shaped parts (connectors, electrolytic cans, relays) "
                    "where the heuristic is least trustworthy."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "reference": {"type": "string"},
                        "include_text": {"type": "boolean", "default": False, "description": "Include the footprint's reference/value silkscreen text in the box."},
                    },
                    "required": ["reference"],
                },
                "handler": self._tool_get_live_bounding_box,
            },
            "find_kicad_live_layout_collisions": {
                "description": (
                    "Live-board analogue of find_kicad_layout_collisions: same internal (among "
                    "references) + external (nearby obstacles) collision check, but using KiCad's "
                    "own bounding boxes instead of the file tool's circular-radius estimate - more "
                    "accurate for oblong parts. Read-only."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "references": {"type": "array", "items": {"type": "string"}},
                        "extra_search_radius": {"type": "number", "default": 25.0, "description": "mm; how far to look for outside obstacles."},
                        "margin": {"type": "number", "default": 0.4, "description": "mm; required clearance between boxes."},
                    },
                    "required": ["references"],
                },
                "handler": self._tool_find_live_layout_collisions,
            },
            "highlight_kicad_live_components": {
                "description": (
                    "Select the given component references in the live KiCad PCB editor window, "
                    "replacing whatever's currently selected - so a human reviewing an agent's "
                    "proposed change can see exactly which footprints it's about to touch before "
                    "any write happens. Purely visual; writes still go through the write=true "
                    "file-based tools."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {"references": {"type": "array", "items": {"type": "string"}}},
                    "required": ["references"],
                },
                "handler": self._tool_highlight_live_components,
            },
            "clear_kicad_live_highlight": {
                "description": "Clear the current selection in the live KiCad PCB editor window.",
                "inputSchema": {"type": "object", "properties": {}},
                "handler": self._tool_clear_live_highlight,
            },
            "get_kicad_live_selection": {
                "description": (
                    "Read back whatever is currently selected in the live KiCad PCB editor - e.g. "
                    "so a person can point at a component by hand in the GUI instead of typing its "
                    "reference designator for a follow-up tool call."
                ),
                "inputSchema": {"type": "object", "properties": {}},
                "handler": self._tool_get_live_selection,
            },
        }

    def _tool_inspect_project(self, args: dict[str, Any]) -> dict[str, Any]:
        return inspect_project(args["project_path"])

    def _tool_list_components(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        return list_components(args["project_path"], limit=int(args.get("limit", 50)))

    def _tool_get_component(self, args: dict[str, Any]) -> dict[str, Any]:
        return get_component(args["project_path"], args["reference"])

    def _tool_get_component_connections(self, args: dict[str, Any]) -> dict[str, Any]:
        return get_component_connections(args["project_path"], args["reference"])

    def _tool_get_net(self, args: dict[str, Any]) -> dict[str, Any]:
        return get_net(args["project_path"], args["net_name"])

    def _tool_get_net_track_widths(self, args: dict[str, Any]) -> dict[str, Any]:
        return get_net_track_widths(args["project_path"], args.get("net_name"))

    def _tool_detect_buses(self, args: dict[str, Any]) -> dict[str, Any]:
        return detect_buses(args["project_path"], args.get("ic_ref_prefixes"))

    def _tool_get_track_inventory(self, args: dict[str, Any]) -> dict[str, Any]:
        return get_project_track_inventory(args["project_path"])

    def _tool_measure_bus_corridor_area(self, args: dict[str, Any]) -> dict[str, Any]:
        return measure_bus_corridor_areas(
            args["project_path"],
            bus=args.get("bus"),
            nets=args.get("nets"),
            hub_ic=args.get("hub_ic"),
            bus_type=args.get("bus_type"),
            clip_band_mult=args.get("clip_band_mult"),
            station_step=args.get("station_step"),
            ic_ref_prefixes=args.get("ic_ref_prefixes"),
        )

    def _tool_get_pcb_settings(self, args: dict[str, Any]) -> dict[str, Any]:
        return load_pcb_settings(args["project_path"])

    def _tool_init_pcb_settings(self, args: dict[str, Any]) -> dict[str, Any]:
        return init_pcb_settings(
            args["project_path"],
            write=bool(args.get("write", False)),
            overwrite=bool(args.get("overwrite", False)),
        )

    def _tool_get_trace_cost(self, args: dict[str, Any]) -> dict[str, Any]:
        return get_trace_cost(args["project_path"], args.get("net"))

    def _tool_get_board_layers(self, args: dict[str, Any]) -> dict[str, Any]:
        return get_board_layers(args["project_path"])

    def _tool_get_ratsnest(self, args: dict[str, Any]) -> dict[str, Any]:
        nets = args.get("nets")
        return get_ratsnest(args["project_path"], list(nets) if nets else None)

    def _tool_list_zones(self, args: dict[str, Any]) -> dict[str, Any]:
        return list_zones(args["project_path"])

    def _tool_audit_plane_islands(self, args: dict[str, Any]) -> dict[str, Any]:
        return audit_plane_islands(args["project_path"])

    def _tool_audit_crosstalk(self, args: dict[str, Any]) -> dict[str, Any]:
        pairs = args.get("adjacent_layer_pairs")
        return audit_crosstalk(
            args["project_path"],
            min_spacing_mm=args.get("min_spacing_mm"),
            min_parallel_run_mm=args.get("min_parallel_run_mm"),
            penalty_per_mm=args.get("penalty_per_mm"),
            same_bus_exempt=args.get("same_bus_exempt"),
            adjacent_layer_pairs=([tuple(p) for p in pairs] if pairs else None),
        )

    def _tool_get_drc_constraints(self, args: dict[str, Any]) -> dict[str, Any]:
        return get_drc_constraints(args["project_path"])

    def _tool_get_system_resources(self, args: dict[str, Any]) -> dict[str, Any]:
        import kicad_router_accel as _accel
        return _accel.probe_system_resources()

    def _tool_route_board(self, args: dict[str, Any]) -> dict[str, Any]:
        nets = args.get("nets")
        return route_board(
            args["project_path"],
            list(nets) if nets else None,
            write=bool(args.get("write", False)),
            effort=str(args.get("effort", "balanced")),
            allow_while_open=bool(args.get("allow_while_open", False)),
            allow_hand_copper_ripup=bool(args.get("allow_hand_copper_ripup", False)),
            optimize=bool(args.get("optimize", False)),
        )

    def _tool_route_nets(self, args: dict[str, Any]) -> dict[str, Any]:
        nets = args.get("nets")
        mri = args.get("max_ripup_iterations")
        return route_nets(
            args["project_path"],
            list(nets) if nets else None,
            write=bool(args.get("write", False)),
            allow_while_open=bool(args.get("allow_while_open", False)),
            max_ripup_iterations=int(mri) if mri is not None else None,
            allow_hand_copper_ripup=bool(args.get("allow_hand_copper_ripup", False)),
        )

    def _tool_unroute_nets(self, args: dict[str, Any]) -> dict[str, Any]:
        nets = args.get("nets")
        return unroute_nets(
            args["project_path"],
            list(nets) if nets else None,
            write=bool(args.get("write", False)),
            allow_while_open=bool(args.get("allow_while_open", False)),
        )

    def _tool_open_route_viewer(self, args: dict[str, Any]) -> dict[str, Any]:
        return open_route_viewer(args["project_path"], board=args.get("board"))

    def _tool_propose_plane(self, args: dict[str, Any]) -> dict[str, Any]:
        return propose_plane(args["project_path"], args["net"], args.get("layer"))

    def _tool_create_plane(self, args: dict[str, Any]) -> dict[str, Any]:
        return create_plane(
            args["project_path"],
            args["net"],
            layer=args.get("layer"),
            name=args.get("name"),
            priority=int(args.get("priority", 0)),
            write=bool(args.get("write", False)),
            allow_while_open=bool(args.get("allow_while_open", False)),
        )

    def _tool_modify_plane(self, args: dict[str, Any]) -> dict[str, Any]:
        priority = args.get("priority")
        return modify_plane(
            args["project_path"],
            args["uuid"],
            new_outline=args.get("new_outline"),
            priority=int(priority) if priority is not None else None,
            write=bool(args.get("write", False)),
            allow_while_open=bool(args.get("allow_while_open", False)),
        )

    def _tool_benchmark_autoroute(self, args: dict[str, Any]) -> dict[str, Any]:
        return benchmark_autoroute(
            args["source_board"],
            mode=str(args.get("mode", "complete_only")),
            effort=str(args.get("effort", "balanced")),
            scratch_dir=args.get("scratch_dir"),
        )

    def _tool_optimize_board(self, args: dict[str, Any]) -> dict[str, Any]:
        max_seconds = args.get("max_seconds")
        max_iterations = args.get("max_iterations")
        time_budget_s = args.get("time_budget_s")
        seed = args.get("seed")
        return optimize_board(
            args["project_path"],
            session_id=args.get("session_id"),
            max_iterations_per_call=int(args.get("max_iterations_per_call", 3)),
            max_seconds=float(max_seconds) if max_seconds is not None else None,
            seed=int(seed) if seed is not None else None,
            accept=args.get("accept"),
            max_iterations=int(max_iterations) if max_iterations is not None else None,
            time_budget_s=float(time_budget_s) if time_budget_s is not None else None,
            effort=args.get("effort"),
            pin_swap_exclusions=args.get("pin_swap_exclusions"),
            write=bool(args.get("write", False)),
            allow_while_open=bool(args.get("allow_while_open", False)),
        )

    def _tool_get_route_session(self, args: dict[str, Any]) -> dict[str, Any]:
        return get_route_session(args["project_path"], session_id=args.get("session_id"))

    def _tool_decide_route(self, args: dict[str, Any]) -> dict[str, Any]:
        return decide_route(
            args["project_path"],
            args["session_id"],
            args["decision_id"],
            args["choice"],
            rationale=args.get("rationale"),
        )

    def _tool_run_stitching_pass(self, args: dict[str, Any]) -> dict[str, Any]:
        return run_stitching_pass(args["project_path"], write=bool(args.get("write", False)))

    def _tool_remove_stitching_vias(self, args: dict[str, Any]) -> dict[str, Any]:
        return remove_stitching_vias(
            args["project_path"],
            area=args.get("area"),
            write=bool(args.get("write", False)),
            include_foreign=bool(args.get("include_foreign", False)),
            allow_while_open=bool(args.get("allow_while_open", False)),
        )

    def _tool_propose_netclass(self, args: dict[str, Any]) -> dict[str, Any]:
        return propose_netclass_from_nets(args["project_path"], list(args["nets"]), args["name"])

    def _tool_create_netclass(self, args: dict[str, Any]) -> dict[str, Any]:
        return create_netclass(
            args["project_path"],
            args["name"],
            dict(args.get("settings") or {}),
            list(args["net_patterns"]),
            write=bool(args.get("write", False)),
            allow_while_open=bool(args.get("allow_while_open", False)),
        )

    def _tool_audit_netclass_conformance(self, args: dict[str, Any]) -> dict[str, Any]:
        return audit_netclass_conformance(args["project_path"])

    def _tool_find_components_by_net(self, args: dict[str, Any]) -> dict[str, Any]:
        return find_components_by_net(args["project_path"], args["net_name"])

    def _tool_find_components_by_pin_connection(self, args: dict[str, Any]) -> dict[str, Any]:
        return find_components_by_pin_connection(
            args["project_path"],
            args["reference"],
            args["pin"],
        )

    def _tool_suggest_component_placement(self, args: dict[str, Any]) -> dict[str, Any]:
        return suggest_component_placement(
            args["project_path"],
            args["reference"],
            group_size=int(args.get("group_size", 4)),
            spacing=float(args.get("spacing", 10.0)),
            rotation=float(args.get("rotation", 0.0)),
        )

    def _tool_list_schematic_parts(self, args: dict[str, Any]) -> dict[str, Any]:
        return list_schematic_parts(args["project_path"])

    def _tool_get_schematic_part(self, args: dict[str, Any]) -> dict[str, Any]:
        return get_schematic_part(args["project_path"], args["reference"])

    def _tool_audit_capacitor_voltages(self, args: dict[str, Any]) -> dict[str, Any]:
        return audit_capacitor_voltages(args["project_path"], default_voltage=args.get("default_voltage"))

    def _tool_audit_capacitor_net_voltages(self, args: dict[str, Any]) -> dict[str, Any]:
        return audit_capacitor_net_voltages(args["project_path"])

    def _tool_detect_critical_nets(self, args: dict[str, Any]) -> dict[str, Any]:
        return classify_critical_nets(args["project_path"])

    def _tool_detect_connectors(self, args: dict[str, Any]) -> dict[str, Any]:
        return detect_connectors(args["project_path"], ref_prefixes=args.get("ref_prefixes"))

    def _tool_lookup_mouser_part(self, args: dict[str, Any]) -> dict[str, Any]:
        return lookup_mouser_part(args["url"])

    def _tool_bulk_lookup_mouser_parts(self, args: dict[str, Any]) -> dict[str, Any]:
        return bulk_lookup_mouser_parts(args["project_path"], references=args.get("references"))

    def _tool_normalize_manufacturer_part_number_properties(self, args: dict[str, Any]) -> dict[str, Any]:
        return normalize_manufacturer_part_number_properties(
            args["project_path"],
            write=bool(args.get("write", False)),
            allow_while_open=bool(args.get("allow_while_open", False)),
        )

    def _tool_audit_manufacturer_part_numbers(self, args: dict[str, Any]) -> dict[str, Any]:
        return audit_manufacturer_part_numbers(args["project_path"], references=args.get("references"))

    def _tool_generate_mouser_stock_report(self, args: dict[str, Any]) -> dict[str, Any]:
        return generate_mouser_stock_report(
            args["project_path"],
            report_path=args.get("report_path"),
            references=args.get("references"),
        )

    def _tool_list_component_mouser_urls(self, args: dict[str, Any]) -> dict[str, Any]:
        return list_component_mouser_urls(args["project_path"], args["reference"])

    def _tool_bulk_list_component_mouser_urls(self, args: dict[str, Any]) -> dict[str, Any]:
        return bulk_list_component_mouser_urls(args["project_path"], references=args.get("references"))

    def _tool_optimize_component_mouser_alternates(self, args: dict[str, Any]) -> dict[str, Any]:
        return optimize_component_mouser_alternates(
            args["project_path"],
            args["reference"],
            quantity_needed=args.get("quantity_needed"),
        )

    def _tool_bulk_optimize_component_mouser_alternates(self, args: dict[str, Any]) -> dict[str, Any]:
        return bulk_optimize_component_mouser_alternates(
            args["project_path"],
            references=args.get("references"),
            only_with_alternates=bool(args.get("only_with_alternates", True)),
        )

    def _tool_generate_mouser_buy_list(self, args: dict[str, Any]) -> dict[str, Any]:
        return generate_mouser_buy_list(
            args["project_path"],
            buy_list_path=args.get("buy_list_path"),
            references=args.get("references"),
        )

    def _tool_audit_schematic_integrity(self, args: dict[str, Any]) -> dict[str, Any]:
        return audit_schematic_integrity(args["project_path"])

    def _tool_audit_component_specs(self, args: dict[str, Any]) -> dict[str, Any]:
        return audit_component_specs_against_mouser(
            args["project_path"],
            references=args.get("references"),
            value_tolerance_pct=float(args.get("value_tolerance_pct", 1.0)),
        )

    def _tool_audit_stock_sufficiency(self, args: dict[str, Any]) -> dict[str, Any]:
        return audit_stock_sufficiency(
            args["project_path"],
            references=args.get("references"),
            board_quantity=int(args.get("board_quantity", 1)),
        )

    def _tool_audit_schematic_health(self, args: dict[str, Any]) -> dict[str, Any]:
        return audit_schematic_health(
            args["project_path"],
            args["default_capacitor_voltage"],
            references=args.get("references"),
            board_quantity=int(args.get("board_quantity", 1)),
            value_tolerance_pct=float(args.get("value_tolerance_pct", 1.0)),
            report_path=args.get("report_path"),
        )

    def _tool_set_schematic_property(self, args: dict[str, Any]) -> dict[str, Any]:
        return set_schematic_property(
            args["project_path"],
            args["reference"],
            args["property_name"],
            args["value"],
            write=bool(args.get("write", False)),
            allow_while_open=bool(args.get("allow_while_open", False)),
        )

    def _tool_list_nets(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        return list_nets(args["project_path"])

    def _tool_search_component(self, args: dict[str, Any]) -> dict[str, Any]:
        return search_component_by_reference(args["project_path"], args["reference"])

    def _tool_get_hierarchical_group(self, args: dict[str, Any]) -> dict[str, Any]:
        return get_hierarchical_group(args["project_path"], args["reference"], verbose=bool(args.get("verbose", False)))

    def _tool_list_sibling_instances(self, args: dict[str, Any]) -> dict[str, Any]:
        return list_sibling_instances(args["project_path"], args["reference"])

    def _tool_diff_layout_template(self, args: dict[str, Any]) -> dict[str, Any]:
        return diff_layout_template(args["project_path"], args["template_reference"], args["target_reference"])

    def _tool_apply_layout_template(self, args: dict[str, Any]) -> dict[str, Any]:
        return apply_layout_template(
            args["project_path"],
            args["template_reference"],
            list(args["target_references"]),
            write=bool(args.get("write", False)),
            allow_while_open=bool(args.get("allow_while_open", False)),
        )

    def _tool_apply_layout_changes(self, args: dict[str, Any]) -> dict[str, Any]:
        return apply_layout_changes(
            args["project_path"],
            list(args["changes"]),
            write=bool(args.get("write", False)),
            allow_while_open=bool(args.get("allow_while_open", False)),
        )

    def _tool_list_hierarchical_templates(self, args: dict[str, Any]) -> dict[str, Any]:
        return list_hierarchical_templates(args["project_path"])

    def _tool_move_group(self, args: dict[str, Any]) -> dict[str, Any]:
        return move_group(
            args["project_path"],
            args["reference"],
            dx=float(args.get("dx", 0.0)),
            dy=float(args.get("dy", 0.0)),
            drotation=float(args.get("drotation", 0.0)),
            to=args.get("to"),
            write=bool(args.get("write", False)),
            allow_while_open=bool(args.get("allow_while_open", False)),
        )

    def _tool_list_groups(self, args: dict[str, Any]) -> dict[str, Any]:
        return list_groups(args["project_path"])

    def _tool_create_group(self, args: dict[str, Any]) -> dict[str, Any]:
        return create_group(
            args["project_path"],
            args["name"],
            list(args["references"]),
            write=bool(args.get("write", False)),
            allow_while_open=bool(args.get("allow_while_open", False)),
        )

    def _tool_get_footprint_pads(self, args: dict[str, Any]) -> dict[str, Any]:
        return get_footprint_pads(args["project_path"], args["reference"])

    def _tool_get_pin_position(self, args: dict[str, Any]) -> dict[str, Any]:
        return get_pin_position(args["project_path"], args["reference"], args["pin"])

    def _tool_pin_distance(self, args: dict[str, Any]) -> dict[str, Any]:
        return pin_distance(
            args["project_path"],
            args["reference_a"],
            args["pin_a"],
            args["reference_b"],
            args["pin_b"],
        )

    def _tool_align_component_pin(self, args: dict[str, Any]) -> dict[str, Any]:
        return align_component_pin(
            args["project_path"],
            args["reference"],
            args["pin"],
            args["target"],
            rotation=args.get("rotation"),
            write=bool(args.get("write", False)),
            allow_while_open=bool(args.get("allow_while_open", False)),
        )

    def _tool_align_components_to_anchor(self, args: dict[str, Any]) -> dict[str, Any]:
        return align_components_to_anchor(
            args["project_path"],
            args["anchor_reference"],
            list(args["alignments"]),
            write=bool(args.get("write", False)),
            allow_while_open=bool(args.get("allow_while_open", False)),
        )

    def _tool_classify_group_by_anchor_pin(self, args: dict[str, Any]) -> dict[str, Any]:
        return classify_group_by_anchor_pin(args["project_path"], args["anchor_reference"])

    def _tool_match_group_members_by_role(self, args: dict[str, Any]) -> dict[str, Any]:
        return match_group_members_by_role(
            args["project_path"],
            args["template_reference"],
            args["target_reference"],
            overrides=args.get("overrides"),
        )

    def _tool_diff_layout_by_role(self, args: dict[str, Any]) -> dict[str, Any]:
        return diff_layout_by_role(
            args["project_path"],
            args["template_reference"],
            args["target_reference"],
            overrides=args.get("overrides"),
        )

    def _tool_estimate_footprint_radius(self, args: dict[str, Any]) -> dict[str, Any]:
        return {"reference": args["reference"], "radius": estimate_footprint_radius(args["project_path"], args["reference"])}

    def _tool_find_layout_collisions(self, args: dict[str, Any]) -> dict[str, Any]:
        return find_layout_collisions(
            args["project_path"],
            list(args["references"]),
            extra_search_radius=float(args.get("extra_search_radius", 25.0)),
            margin=float(args.get("margin", 0.4)),
        )

    def _tool_nudge_to_clear(self, args: dict[str, Any]) -> dict[str, Any]:
        return nudge_to_clear(
            args["project_path"],
            args["reference"],
            avoid_references=args.get("avoid_references"),
            search_radius=float(args.get("search_radius", 25.0)),
            margin=float(args.get("margin", 0.4)),
            max_search_radius=float(args.get("max_search_radius", 20.0)),
            write=bool(args.get("write", False)),
            allow_while_open=bool(args.get("allow_while_open", False)),
        )

    def _tool_delete_group(self, args: dict[str, Any]) -> dict[str, Any]:
        return delete_group(
            args["project_path"],
            name=args.get("name"),
            group_uuid=args.get("group_uuid"),
            write=bool(args.get("write", False)),
            allow_while_open=bool(args.get("allow_while_open", False)),
        )

    def _tool_get_property_position(self, args: dict[str, Any]) -> dict[str, Any]:
        return get_property_position(
            args["project_path"],
            args["reference"],
            property_name=args.get("property_name", "Reference"),
        )

    def _tool_diff_property_position_template(self, args: dict[str, Any]) -> dict[str, Any]:
        return diff_property_position_template(
            args["project_path"],
            args["template_reference"],
            args["target_reference"],
            property_name=args.get("property_name", "Reference"),
        )

    def _tool_apply_property_position_changes(self, args: dict[str, Any]) -> dict[str, Any]:
        return apply_property_position_changes(
            args["project_path"],
            list(args["changes"]),
            write=bool(args.get("write", False)),
            allow_while_open=bool(args.get("allow_while_open", False)),
        )

    def _tool_apply_property_position_template(self, args: dict[str, Any]) -> dict[str, Any]:
        return apply_property_position_template(
            args["project_path"],
            args["template_reference"],
            list(args["target_references"]),
            property_name=args.get("property_name", "Reference"),
            write=bool(args.get("write", False)),
            allow_while_open=bool(args.get("allow_while_open", False)),
        )

    def _tool_diff_flip_template(self, args: dict[str, Any]) -> dict[str, Any]:
        return diff_flip_template(args["project_path"], args["template_reference"], args["target_reference"])

    def _tool_apply_flip_template(self, args: dict[str, Any]) -> dict[str, Any]:
        return apply_flip_template(
            args["project_path"],
            args["template_reference"],
            list(args["target_references"]),
            write=bool(args.get("write", False)),
            allow_while_open=bool(args.get("allow_while_open", False)),
        )

    def _tool_get_ipc_status(self, args: dict[str, Any]) -> dict[str, Any]:
        return get_ipc_status()

    def _tool_get_live_bounding_box(self, args: dict[str, Any]) -> dict[str, Any]:
        return get_live_bounding_box(args["reference"], include_text=bool(args.get("include_text", False)))

    def _tool_find_live_layout_collisions(self, args: dict[str, Any]) -> dict[str, Any]:
        return find_live_layout_collisions(
            list(args["references"]),
            extra_search_radius=float(args.get("extra_search_radius", 25.0)),
            margin=float(args.get("margin", 0.4)),
        )

    def _tool_highlight_live_components(self, args: dict[str, Any]) -> dict[str, Any]:
        return highlight_live_components(list(args["references"]))

    def _tool_clear_live_highlight(self, args: dict[str, Any]) -> dict[str, Any]:
        return clear_live_highlight()

    def _tool_get_live_selection(self, args: dict[str, Any]) -> dict[str, Any]:
        return get_live_selection()

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        method = message.get("method")
        if method is None:
            return None

        if method == "initialize":
            params = message.get("params") or {}
            requested_version = params.get("protocolVersion")
            protocol_version = requested_version if requested_version in SUPPORTED_PROTOCOL_VERSIONS else "2024-11-05"
            return {
                "jsonrpc": "2.0",
                "id": message.get("id"),
                "result": {
                    "protocolVersion": protocol_version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "kiln-kicad-mcp", "version": "1.0.0"},
                },
            }

        if method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": message.get("id"),
                "result": {
                    "tools": [
                        {
                            "name": name,
                            "description": info["description"],
                            "inputSchema": info["inputSchema"],
                        }
                        for name, info in self.tools.items()
                    ]
                },
            }

        if method == "tools/call":
            params = message.get("params", {})
            tool_name = params.get("name")
            arguments = params.get("arguments", {}) or {}
            tool = self.tools.get(tool_name)
            if not tool:
                return {
                    "jsonrpc": "2.0",
                    "id": message.get("id"),
                    "error": {"code": -1, "message": f"Unknown tool: {tool_name}"},
                }
            try:
                result = tool["handler"](arguments)
            except Exception as exc:  # pragma: no cover - runtime safety
                return {
                    "jsonrpc": "2.0",
                    "id": message.get("id"),
                    "error": {"code": -2, "message": str(exc)},
                }
            return {
                "jsonrpc": "2.0",
                "id": message.get("id"),
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(result, indent=2, ensure_ascii=False),
                        }
                    ],
                    "isError": False,
                },
            }

        if method == "ping":
            return {"jsonrpc": "2.0", "id": message.get("id"), "result": {"ok": True}}

        return {
            "jsonrpc": "2.0",
            "id": message.get("id"),
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }


def _read_message() -> dict[str, Any] | None:
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            log_message("[kicad-mcp] stdin closed before message")
            return None
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line.decode("utf-8"))
        except Exception as exc:
            log_message(f"[kicad-mcp] invalid JSON: {exc}")
            raise


class MCPHTTPRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        return

    def _send_json(self, response: dict[str, Any], status: int = 200) -> None:
        payload = json.dumps(response, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        self.wfile.write(payload)
        self.wfile.flush()

    def do_POST(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_error(411, "Content-Length required")
            return

        body = self.rfile.read(length)
        try:
            message = json.loads(body.decode("utf-8"))
        except Exception as exc:
            self.send_error(400, f"Invalid JSON: {exc}")
            return

        method = message.get("method")
        if method == "notifications/initialized":
            self.send_response(202)
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            return

        server = cast("KicadMcpHTTPServer", self.server)
        response = server.kicad_mcp.handle(message)
        if response is None:
            self.send_response(204)
            self.end_headers()
            return
        self._send_json(response)

    def do_GET(self) -> None:
        accept_header = self.headers.get("Accept", "")
        if "text/event-stream" in accept_header.lower():
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                while True:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    time.sleep(15)
            except BrokenPipeError:
                return
            except ConnectionResetError:
                return
        else:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
            self.wfile.flush()


class KicadMcpHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], RequestHandlerClass: type[BaseHTTPRequestHandler], kicad_mcp: KiCadMcpServer) -> None:
        super().__init__(server_address, RequestHandlerClass)
        self.kicad_mcp = kicad_mcp


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="KiCad MCP server supporting stdio and HTTP transports")
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio", help="Transport to use for MCP communication")
    parser.add_argument("--host", default="127.0.0.1", help="HTTP host to bind when using HTTP transport")
    parser.add_argument("--port", type=int, default=8765, help="HTTP port to bind when using HTTP transport")
    return parser.parse_args()


def _write_message(message: dict[str, Any] | None) -> None:
    if message is None:
        return
    payload = json.dumps(message, ensure_ascii=False).encode("utf-8") + b"\n"
    sys.stdout.buffer.write(payload)
    sys.stdout.buffer.flush()


def main() -> None:
    log_message("[kicad-mcp] starting")
    server = KiCadMcpServer()
    log_message("[kicad-mcp] server initialized")
    args = parse_args()

    if args.transport == "http":
        http_server = KicadMcpHTTPServer((args.host, args.port), MCPHTTPRequestHandler, server)
        log_message(f"[kicad-mcp] HTTP transport listening on http://{args.host}:{args.port}")
        print(f"HTTP server listening on http://{args.host}:{args.port}", flush=True)
        try:
            http_server.serve_forever()
        except KeyboardInterrupt:
            log_message("[kicad-mcp] HTTP server shutting down")
            http_server.server_close()
    else:
        while True:
            try:
                message = _read_message()
                if message is None:
                    log_message("[kicad-mcp] exiting because input ended")
                    break
                if message.get("method") == "notifications/initialized":
                    log_message("[kicad-mcp] ignoring notifications/initialized")
                    continue
                response = server.handle(message)
                _write_message(response)
            except Exception as exc:
                log_message(f"[kicad-mcp] unhandled error: {exc}")
                traceback.print_exc(file=sys.stderr)
                break


if __name__ == "__main__":
    main()
