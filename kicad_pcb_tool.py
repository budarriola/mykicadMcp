
from __future__ import annotations

import argparse
import json
import math
import re
import uuid as _uuid
from pathlib import Path
from typing import Any


class SexprParser:
    def __init__(self, text: str) -> None:
        self.tokens = self._tokenize(text)
        self.index = 0

    def _tokenize(self, text: str) -> list[str]:
        tokens: list[str] = []
        i = 0
        while i < len(text):
            char = text[i]
            if char.isspace():
                i += 1
                continue
            if char == ';':
                while i < len(text) and text[i] != '\n':
                    i += 1
                continue
            if char in "()":
                tokens.append(char)
                i += 1
                continue
            if char == '"':
                j = i + 1
                buf: list[str] = []
                while j < len(text):
                    if text[j] == '\\' and j + 1 < len(text):
                        buf.append(text[j + 1])
                        j += 2
                        continue
                    if text[j] == '"':
                        break
                    buf.append(text[j])
                    j += 1
                tokens.append("".join(buf))
                i = j + 1
                continue

            j = i
            while j < len(text) and not text[j].isspace() and text[j] not in "()":
                j += 1
            tokens.append(text[i:j])
            i = j

        return tokens

    def parse(self) -> Any:
        value, self.index = self._parse_value(self.index)
        return value

    def _parse_value(self, index: int) -> tuple[Any, int]:
        if index >= len(self.tokens):
            raise ValueError("Unexpected end of input")
        token = self.tokens[index]
        if token == '(':
            items: list[Any] = []
            index += 1
            while index < len(self.tokens) and self.tokens[index] != ')':
                item, index = self._parse_value(index)
                items.append(item)
            if index >= len(self.tokens):
                raise ValueError("Unterminated list")
            return items, index + 1
        if token == ')':
            raise ValueError("Unexpected ')' token")
        return token, index + 1


def _resolve_project_path(project_path: str | Path) -> tuple[Path, Path, Path]:
    path = Path(project_path).expanduser().resolve()
    if path.is_dir():
        # KiCad periodically writes "_autosave-<name>.kicad_pcb"/".kicad_pro" next
        # to the real project files whenever it has the project open - and
        # "_autosave-..." sorts before the real name alphabetically. Excluding
        # them here is required, not just tidy: picking one up by accident means
        # every read/write in this module silently operates on a stale snapshot
        # of the board instead of the real file.
        real_pcbs = sorted(p for p in path.glob("*.kicad_pcb") if not p.name.startswith("_autosave-"))
        real_pros = sorted(p for p in path.glob("*.kicad_pro") if not p.name.startswith("_autosave-"))
        board_candidates = [
            path / f"{path.name}.kicad_pcb",
            *real_pcbs,
            path / f"{path.name}.kicad_pro",
            *real_pros,
        ]
        for candidate in board_candidates:
            if candidate.exists() and candidate.suffix.lower() == ".kicad_pcb":
                board_path = candidate
                break
        else:
            raise FileNotFoundError(f"No KiCad PCB file found in {path}")
        project_file = next((candidate for candidate in board_candidates if candidate.exists() and candidate.suffix.lower() == ".kicad_pro"), path / f"{path.name}.kicad_pro")
        netlist_path = path / f"{board_path.stem}.net"
    else:
        if path.suffix.lower() == ".kicad_pro":
            board_path = path.with_suffix(".kicad_pcb")
            project_file = path
            netlist_path = board_path.with_suffix(".net")
        elif path.suffix.lower() == ".kicad_pcb":
            board_path = path
            project_file = path.with_suffix(".kicad_pro")
            netlist_path = path.with_suffix(".net")
        else:
            raise ValueError(f"Unsupported KiCad path: {path}")

    if not board_path.exists():
        raise FileNotFoundError(f"PCB file not found: {board_path}")
    if not netlist_path.exists():
        netlist_path = board_path.with_suffix(".net")
    return board_path, project_file, netlist_path


def _read_text(path: Path) -> str:
    # newline="" preserves the file's original line endings verbatim (KiCad
    # board files are typically CRLF); universal-newline translation would
    # silently rewrite every line ending on the next write-back.
    with path.open("r", encoding="utf-8", errors="ignore", newline="") as handle:
        return handle.read()


# Parsing a multi-megabyte board file with the s-expression tokenizer above
# takes several hundred ms; a single diff/apply call can trigger it a dozen
# times (once per hierarchical group involved). Cache by (mtime, size) so
# repeat calls within one process - the common case for the MCP server, which
# is a long-lived process across a whole session - reuse the parse until the
# file actually changes on disk, however it changed (our own writes, KiCad,
# manual edits). Keyed by resolved path so different callers sharing a process
# share the cache too.
_board_component_cache: dict[str, tuple[float, int, list[dict[str, Any]]]] = {}
_net_cache: dict[str, tuple[float, int, list[dict[str, Any]]]] = {}
_pad_cache: dict[str, tuple[float, int, dict[str, dict[str, Any]]]] = {}


def _invalidate_board_cache(board_path: Path) -> None:
    _board_component_cache.pop(str(board_path), None)
    _pad_cache.pop(str(board_path), None)


def _kicad_lock_path(board_path: Path) -> Path:
    return board_path.with_name(f"~{board_path.name}.lck")


def _check_not_locked_by_editor(board_path: Path, allow_while_open: bool) -> None:
    """KiCad drops a `~<name>.lck` file next to a board while it's open in an
    editor, and removes it on a normal close - but it never watches the file
    for outside changes, so writing here while KiCad has the board open is a
    data-loss trap in both directions: if the GUI has unsaved edits, the next
    Ctrl+S there silently overwrites what this write is about to make; if it
    doesn't, this write instead gets silently discarded the next time the GUI
    saves its own still-stale in-memory copy. Whichever side saves last wins,
    with no warning either way.

    Refuses by default. `allow_while_open=True` is an explicit opt-out for a
    caller who has already confirmed there's nothing pending in the GUI (or
    who knows the lock file is stale, e.g. left over from a crashed session).
    """
    if allow_while_open:
        return
    lock_path = _kicad_lock_path(board_path)
    if not lock_path.exists():
        return
    raise RuntimeError(
        f"{board_path.name} appears to be open in a KiCad editor right now (lock file "
        f"{lock_path.name} present). Writing directly to the file while KiCad has it open "
        "risks silently losing this change or the GUI's own unsaved edits, whichever side "
        "saves last. Close the board in KiCad first, or pass allow_while_open=True once "
        "you've confirmed there's nothing pending in the GUI."
    )


def _parse_board_components_cached(board_path: Path) -> list[dict[str, Any]]:
    stat = board_path.stat()
    key = str(board_path)
    cached = _board_component_cache.get(key)
    if cached is not None and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
        return cached[2]
    components = _parse_board_components(board_path)
    _board_component_cache[key] = (stat.st_mtime, stat.st_size, components)
    return components


def _parse_nets_cached(netlist_path: Path) -> list[dict[str, Any]]:
    if not netlist_path.exists():
        return []
    stat = netlist_path.stat()
    key = str(netlist_path)
    cached = _net_cache.get(key)
    if cached is not None and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
        return cached[2]
    nets = _parse_nets(netlist_path)
    _net_cache[key] = (stat.st_mtime, stat.st_size, nets)
    return nets


def _parse_board_components(board_path: Path) -> list[dict[str, Any]]:
    board_text = _read_text(board_path)
    parser = SexprParser(board_text)
    root = parser.parse()
    components: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, list):
            if node and node[0] == "footprint":
                properties: dict[str, str] = {}
                position: dict[str, float] | None = None
                footprint_name = ""
                footprint_uuid = ""
                locked = False
                path_str = ""
                sheetname = ""
                sheetfile = ""
                if len(node) > 1 and isinstance(node[1], str):
                    footprint_name = node[1]
                for entry in node[1:]:
                    if not (isinstance(entry, list) and entry):
                        continue
                    tag = entry[0]
                    if tag == "locked":
                        locked = True
                    elif tag == "property" and len(entry) >= 3:
                        key = entry[1]
                        value = entry[2] if len(entry) > 2 else ""
                        if isinstance(key, str):
                            properties[key] = str(value)
                    elif tag == "at":
                        values = [float(token) for token in entry[1:] if isinstance(token, str) and _is_number(token)]
                        if len(values) >= 2:
                            position = {"x": values[0], "y": values[1], "rotation": values[2] if len(values) >= 3 else 0.0}
                    elif tag == "layer" and len(entry) >= 2:
                        layer_name = entry[1] if isinstance(entry[1], str) else ""
                        properties.setdefault("layer", str(layer_name))
                    elif tag == "uuid" and len(entry) >= 2 and isinstance(entry[1], str):
                        footprint_uuid = entry[1]
                    elif tag == "path" and len(entry) >= 2 and isinstance(entry[1], str):
                        path_str = entry[1]
                    elif tag == "sheetname" and len(entry) >= 2 and isinstance(entry[1], str):
                        sheetname = entry[1]
                    elif tag == "sheetfile" and len(entry) >= 2 and isinstance(entry[1], str):
                        sheetfile = entry[1]
                if properties.get("Reference") or footprint_name:
                    path_segments = [seg for seg in path_str.split("/") if seg]
                    components.append(
                        {
                            "reference": properties.get("Reference", ""),
                            "value": properties.get("Value", ""),
                            "footprint": footprint_name,
                            "position": position or {"x": 0.0, "y": 0.0, "rotation": 0.0},
                            "properties": properties,
                            "uuid": footprint_uuid,
                            "locked": locked,
                            "path": path_str,
                            "sheet_instance": "/".join(path_segments[:-1]),
                            "symbol_uuid": path_segments[-1] if path_segments else "",
                            "sheetname": sheetname,
                            "sheetfile": sheetfile,
                        }
                    )
            for child in node:
                walk(child)

    walk(root)
    return components


def _parse_footprint_pads(board_path: Path) -> dict[str, dict[str, Any]]:
    """Extract every footprint's pads (number, net, absolute board position) keyed
    by footprint uuid. Pad nets come straight from the board file's own `(net ..)`
    entries on each pad - the ground truth for what's actually connected where,
    independent of the separate schematic-derived .net file - so this is reliable
    even for footprints whose schematic pin numbering doesn't obviously match pad
    numbering. Absolute pad position accounts for the footprint's own `(at x y
    rotation)` placement on the board (pad coordinates in the file are relative to
    that, unrotated).
    """
    board_text = _read_text(board_path)
    root = SexprParser(board_text).parse()
    footprints: dict[str, dict[str, Any]] = {}

    def walk(node: Any) -> None:
        if isinstance(node, list):
            if node and node[0] == "footprint":
                fp_uuid = ""
                reference = ""
                fp_pos = {"x": 0.0, "y": 0.0, "rotation": 0.0}
                pads: list[dict[str, Any]] = []
                for entry in node[1:]:
                    if not (isinstance(entry, list) and entry):
                        continue
                    tag = entry[0]
                    if tag == "uuid" and len(entry) >= 2 and isinstance(entry[1], str):
                        fp_uuid = entry[1]
                    elif tag == "at":
                        values = [float(t) for t in entry[1:] if isinstance(t, str) and _is_number(t)]
                        if len(values) >= 2:
                            fp_pos = {"x": values[0], "y": values[1], "rotation": values[2] if len(values) >= 3 else 0.0}
                    elif tag == "property" and len(entry) >= 3 and entry[1] == "Reference":
                        reference = str(entry[2])
                    elif tag == "pad":
                        pad_number = entry[1] if len(entry) > 1 and isinstance(entry[1], str) else ""
                        pad_local = {"x": 0.0, "y": 0.0, "rotation": 0.0}
                        pad_net = ""
                        pad_layers: list[str] = []
                        pad_pintype = ""
                        for pentry in entry[1:]:
                            if not (isinstance(pentry, list) and pentry):
                                continue
                            ptag = pentry[0]
                            if ptag == "at":
                                pvalues = [float(t) for t in pentry[1:] if isinstance(t, str) and _is_number(t)]
                                if len(pvalues) >= 2:
                                    pad_local = {"x": pvalues[0], "y": pvalues[1], "rotation": pvalues[2] if len(pvalues) >= 3 else 0.0}
                            elif ptag == "net" and len(pentry) >= 2:
                                pad_net = str(pentry[-1])
                            elif ptag == "layers":
                                pad_layers = [str(x) for x in pentry[1:] if isinstance(x, str)]
                            elif ptag == "pintype" and len(pentry) >= 2:
                                pad_pintype = str(pentry[1])
                        pads.append(
                            {
                                "number": pad_number,
                                "net": pad_net,
                                "pintype": pad_pintype,
                                "layers": pad_layers,
                                "local_position": pad_local,
                            }
                        )
                if fp_uuid:
                    for pad in pads:
                        dx, dy = _rotate_point(pad["local_position"]["x"], pad["local_position"]["y"], fp_pos["rotation"])
                        pad["position"] = {"x": round(fp_pos["x"] + dx, 6), "y": round(fp_pos["y"] + dy, 6)}
                        pad["rotation"] = round((pad["local_position"]["rotation"] + fp_pos["rotation"]) % 360, 6)
                    footprints[fp_uuid] = {
                        "uuid": fp_uuid,
                        "reference": reference,
                        "position": fp_pos,
                        "pads": pads,
                    }
            for child in node:
                walk(child)

    walk(root)
    return footprints


def _parse_footprint_pads_cached(board_path: Path) -> dict[str, dict[str, Any]]:
    stat = board_path.stat()
    key = str(board_path)
    cached = _pad_cache.get(key)
    if cached is not None and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
        return cached[2]
    pads = _parse_footprint_pads(board_path)
    _pad_cache[key] = (stat.st_mtime, stat.st_size, pads)
    return pads


def _parse_nets(netlist_path: Path) -> list[dict[str, Any]]:
    if not netlist_path.exists():
        return []
    parser = SexprParser(_read_text(netlist_path))
    root = parser.parse()
    nets: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, list):
            if node and node[0] == "net":
                name = ""
                nodes: list[dict[str, str]] = []
                for entry in node[1:]:
                    if isinstance(entry, list) and entry and entry[0] == "name" and len(entry) >= 2:
                        name = str(entry[1])
                    elif isinstance(entry, list) and entry and entry[0] == "node":
                        ref = ""
                        pin = ""
                        for field in entry[1:]:
                            if isinstance(field, list) and field and field[0] == "ref" and len(field) >= 2:
                                ref = str(field[1])
                            elif isinstance(field, list) and field and field[0] == "pin" and len(field) >= 2:
                                pin = str(field[1])
                        if ref:
                            nodes.append({"ref": ref, "pin": pin})
                nets.append({"name": name or f"net_{len(nets) + 1}", "nodes": nodes})
            for child in node:
                walk(child)

    walk(root)
    return nets


def _build_net_maps(nets: list[dict[str, Any]]) -> tuple[dict[str, list[dict[str, str]]], dict[str, list[dict[str, str]]]]:
    refs: dict[str, list[dict[str, str]]] = {}
    net_map: dict[str, list[dict[str, str]]] = {}
    for net in nets:
        net_name = net.get("name", "")
        nodes = net.get("nodes", [])
        net_map[net_name] = nodes
        for node in nodes:
            ref = node.get("ref", "")
            if not ref:
                continue
            refs.setdefault(ref, []).append({"net": net_name, "pin": node.get("pin", "")})
    return refs, net_map


def _attach_net_info_to_components(components: list[dict[str, Any]], nets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Builds new dicts rather than mutating in place: `components` may be the
    # cached list shared across calls (and, under the HTTP transport, across
    # threads), so mutating it here would leak net info into unrelated
    # callers and race under concurrent requests.
    ref_to_nets, _ = _build_net_maps(nets)
    result: list[dict[str, Any]] = []
    for component in components:
        ref = component.get("reference", "")
        memberships = ref_to_nets.get(ref, [])
        result.append({**component, "nets": memberships, "net_names": [entry["net"] for entry in memberships]})
    return result


def _is_number(token: str) -> bool:
    try:
        float(token)
        return True
    except ValueError:
        return False


def inspect_project(project_path: str | Path) -> dict[str, Any]:
    board_path, project_file, netlist_path = _resolve_project_path(project_path)
    components = _parse_board_components_cached(board_path)
    nets = _parse_nets_cached(netlist_path)
    components = _attach_net_info_to_components(components, nets)
    refs_to_nets, net_map = _build_net_maps(nets)
    return {
        "project_path": str(project_path),
        "board_path": str(board_path),
        "project_file": str(project_file),
        "netlist_path": str(netlist_path),
        "component_count": len(components),
        "net_count": len(nets),
        "components": components[:25],
        "nets": nets[:25],
        "component_net_membership": refs_to_nets,
        "net_map": net_map,
    }


def _find_component(components: list[dict[str, Any]], reference: str) -> dict[str, Any] | None:
    lower_reference = reference.strip().upper()
    for component in components:
        if component.get("reference", "").strip().upper() == lower_reference:
            return component
    return None


def _get_net_details(nets: list[dict[str, Any]], net_name: str) -> dict[str, Any]:
    for net in nets:
        if net.get("name", "") == net_name:
            return net
    raise KeyError(f"Net {net_name} not found")


def list_components(project_path: str | Path, limit: int = 50) -> list[dict[str, Any]]:
    board_path, _, netlist_path = _resolve_project_path(project_path)
    components = _parse_board_components_cached(board_path)
    nets = _parse_nets_cached(netlist_path)
    components = _attach_net_info_to_components(components, nets)
    return components[:limit]


def get_component(project_path: str | Path, reference: str) -> dict[str, Any]:
    board_path, _, netlist_path = _resolve_project_path(project_path)
    components = _parse_board_components_cached(board_path)
    nets = _parse_nets_cached(netlist_path)
    components = _attach_net_info_to_components(components, nets)
    component = _find_component(components, reference)
    if component is None:
        raise KeyError(f"Component {reference} not found")
    return component


def get_component_connections(project_path: str | Path, reference: str) -> dict[str, Any]:
    board_path, _, netlist_path = _resolve_project_path(project_path)
    components = _parse_board_components_cached(board_path)
    nets = _parse_nets_cached(netlist_path)
    components = _attach_net_info_to_components(components, nets)
    component = _find_component(components, reference)
    if component is None:
        raise KeyError(f"Component {reference} not found")

    connections: list[dict[str, Any]] = []
    connected_refs: set[str] = set()
    for membership in component.get("nets", []):
        net_name = membership.get("net", "")
        net = _get_net_details(nets, net_name)
        nodes = net.get("nodes", [])
        others = [node for node in nodes if node.get("ref", "").strip().upper() != reference.strip().upper()]
        for node in others:
            connected_refs.add(node.get("ref", ""))
        connections.append(
            {
                "net": net_name,
                "pin": membership.get("pin", ""),
                "nodes": nodes,
                "connected_components": others,
            }
        )

    return {
        "component": component,
        "connections": connections,
        "connected_references": sorted(connected_refs),
    }


def find_components_by_pin_connection(project_path: str | Path, reference: str, pin: str) -> dict[str, Any]:
    board_path, _, netlist_path = _resolve_project_path(project_path)
    components = _parse_board_components_cached(board_path)
    nets = _parse_nets_cached(netlist_path)
    component = _find_component(components, reference)
    if component is None:
        raise KeyError(f"Component {reference} not found")

    match_memberships = [
        membership for membership in component.get("nets", []) if str(membership.get("pin", "")).strip() == str(pin).strip()
    ]
    if not match_memberships:
        raise KeyError(f"Pin {pin} not found on component {reference}")

    connected_refs: set[str] = set()
    matches: list[dict[str, Any]] = []
    for membership in match_memberships:
        net_name = membership.get("net", "")
        net = _get_net_details(nets, net_name)
        nodes = net.get("nodes", [])
        others = [node for node in nodes if node.get("ref", "").strip().upper() != reference.strip().upper()]
        for node in others:
            connected_refs.add(node.get("ref", ""))
        matches.append({
            "net": net_name,
            "pin": membership.get("pin", ""),
            "connected_components": others,
            "node_count": len(nodes),
        })

    return {
        "component": component,
        "pin": pin,
        "connections": matches,
        "connected_references": sorted(connected_refs),
    }


def suggest_component_placement(project_path: str | Path, reference: str, group_size: int = 4, spacing: float = 10.0, rotation: float = 0.0) -> dict[str, Any]:
    board_path, _, netlist_path = _resolve_project_path(project_path)
    components = _parse_board_components_cached(board_path)
    nets = _parse_nets_cached(netlist_path)
    component = _find_component(components, reference)
    if component is None:
        raise KeyError(f"Component {reference} not found")

    connections = get_component_connections(project_path, reference)
    connected_refs = connections.get("connected_references", [])
    grouped: list[list[str]] = []
    for index in range(0, len(connected_refs), max(1, group_size)):
        grouped.append(sorted(connected_refs[index:index + max(1, group_size)]))

    placement: list[dict[str, Any]] = []
    for group_index, group in enumerate(grouped):
        for offset_index, other_ref in enumerate(group):
            placement.append({
                "reference": other_ref,
                "group": group_index + 1,
                "position": {
                    "x": round((offset_index - (len(group) - 1) / 2) * spacing, 3),
                    "y": round(group_index * spacing * 1.5, 3),
                    "rotation": rotation,
                },
            })

    return {
        "component": component,
        "connected_references": connected_refs,
        "groups": grouped,
        "suggested_positions": placement,
        "spacing": spacing,
        "rotation": rotation,
    }


def _find_component_ci(components: list[dict[str, Any]], reference: str) -> dict[str, Any]:
    component = _find_component(components, reference)
    if component is None:
        raise KeyError(f"Component {reference} not found")
    return component


_SLIM_KEYS = ("reference", "value", "footprint", "position", "uuid", "locked", "symbol_uuid", "sheetname")


def _slim(component: dict[str, Any], verbose: bool = False) -> dict[str, Any]:
    # Drops the `properties`/`path` blob (Datasheet URLs, Mouser part numbers,
    # Sim.* fields, KiCad filter strings, etc.) that every footprint carries -
    # irrelevant to layout work but the single biggest cost in a group/diff
    # response's size. Pass verbose=True on the rare call where you actually
    # need a component's full KiCad properties.
    if verbose:
        return component
    return {key: component[key] for key in _SLIM_KEYS if key in component}


def get_hierarchical_group(project_path: str | Path, reference: str, verbose: bool = False) -> dict[str, Any]:
    """Return every footprint that shares the same hierarchical-sheet instance as `reference`.

    KiCad stores each footprint's schematic origin as a `path` of the form
    ".../<sheet-instance-uuid>/<symbol-uuid>". The final segment (`symbol_uuid`)
    identifies *which* symbol on the template schematic page this footprint came
    from (shared across every instance of a repeated hierarchical sheet, e.g. one
    per relay channel or thermocouple channel); everything before it identifies
    *which physical instance* (channel) the footprint belongs to. Grouping by that
    prefix is what lets you reliably find "all the parts that belong with K1" or
    "all the parts that belong with U8" without guessing from board position.
    """
    board_path, _, _ = _resolve_project_path(project_path)
    components = _parse_board_components_cached(board_path)
    anchor = _find_component_ci(components, reference)
    instance = anchor.get("sheet_instance", "")
    if not instance:
        raise ValueError(
            f"Component {reference} has no hierarchical sheet path info "
            "(not placed from a hierarchical sheet, or path missing)"
        )
    members = [c for c in components if c.get("sheet_instance") == instance]
    return {
        "anchor": _slim(anchor, verbose),
        "sheet_instance": instance,
        "sheetname": anchor.get("sheetname", ""),
        "sheetfile": anchor.get("sheetfile", ""),
        "member_count": len(members),
        "members": [_slim(m, verbose) for m in members],
    }


def list_sibling_instances(project_path: str | Path, reference: str) -> dict[str, Any]:
    """Find every other hierarchical-sheet instance that reuses the same template
    page as `reference`'s sheet (e.g. given one relay channel or one thermocouple
    channel, find the other channels stamped from the same schematic sheet)."""
    board_path, _, _ = _resolve_project_path(project_path)
    components = _parse_board_components_cached(board_path)
    anchor = _find_component_ci(components, reference)
    sheetfile = anchor.get("sheetfile", "")
    if not sheetfile:
        raise ValueError(f"Component {reference} has no sheetfile info")

    by_instance: dict[str, list[dict[str, Any]]] = {}
    for component in components:
        if component.get("sheetfile") != sheetfile:
            continue
        instance = component.get("sheet_instance", "")
        if not instance:
            continue
        by_instance.setdefault(instance, []).append(component)

    siblings = []
    for instance, members in by_instance.items():
        anchor_in_group = next((m for m in members if m.get("symbol_uuid") == anchor.get("symbol_uuid")), None)
        siblings.append(
            {
                "sheet_instance": instance,
                "sheetname": members[0].get("sheetname", "") if members else "",
                "anchor_reference": anchor_in_group.get("reference") if anchor_in_group else None,
                "anchor_position": anchor_in_group.get("position") if anchor_in_group else None,
                "anchor_locked": anchor_in_group.get("locked") if anchor_in_group else None,
                "member_count": len(members),
            }
        )
    return {
        "reference": reference,
        "sheetfile": sheetfile,
        "instance_count": len(siblings),
        "instances": siblings,
    }


def _rotate_point(dx: float, dy: float, degrees: float) -> tuple[float, float]:
    """Rotate an offset by `degrees` using KiCad's footprint-angle convention.

    Empirically verified against the board (a naive clockwise/Y-down assumption
    came out mirrored on a 90-degree case): increasing a footprint's `at` angle
    turns it counter-clockwise on screen, so a positive `degrees` here is applied
    counter-clockwise as well.
    """
    theta = math.radians(degrees)
    cos_t, sin_t = math.cos(theta), math.sin(theta)
    return dx * cos_t + dy * sin_t, -dx * sin_t + dy * cos_t


def diff_layout_template(project_path: str | Path, template_reference: str, target_reference: str) -> dict[str, Any]:
    """Compute where every sibling of `target_reference`'s group should move to
    in order to match the relative layout (offsets *and* rotations) of
    `template_reference`'s group.

    Matching between the two groups is done by `symbol_uuid` (the shared
    schematic-symbol identity), not by reference name or board proximity, so it
    can't accidentally pick up an unrelated component that merely happens to sit
    nearby (this is the mistake that bit us doing this by hand: two decoupling
    caps from a different sheet were sitting right next to a locked relay and
    got mistaken for its support components).

    If the target anchor's own rotation differs from the template anchor's
    rotation, every member offset (and every member's own rotation) is rotated
    by that same delta, so the whole sub-layout is carried over rigidly rather
    than naively copying raw deltas.

    Returns a dry-run diff; nothing is written. Pass the `changes` list to
    `apply_layout_changes(..., write=True)` to actually apply it.
    """
    template_group = get_hierarchical_group(project_path, template_reference)
    target_group = get_hierarchical_group(project_path, target_reference)

    t_anchor = template_group["anchor"]
    x_anchor = target_group["anchor"]
    delta_rot = (x_anchor["position"]["rotation"] - t_anchor["position"]["rotation"]) % 360

    target_by_role = {m["symbol_uuid"]: m for m in target_group["members"]}

    changes: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for member in template_group["members"]:
        role = member["symbol_uuid"]
        if role == t_anchor["symbol_uuid"]:
            continue  # the anchor itself is never moved
        target_member = target_by_role.get(role)
        if target_member is None:
            skipped.append({"template_reference": member["reference"], "reason": "no matching symbol_uuid in target group"})
            continue

        dx = member["position"]["x"] - t_anchor["position"]["x"]
        dy = member["position"]["y"] - t_anchor["position"]["y"]
        ndx, ndy = _rotate_point(dx, dy, delta_rot)
        new_x = round(x_anchor["position"]["x"] + ndx, 6)
        new_y = round(x_anchor["position"]["y"] + ndy, 6)
        new_rotation = round((member["position"]["rotation"] + delta_rot) % 360, 6)

        changes.append(
            {
                "reference": target_member["reference"],
                "uuid": target_member["uuid"],
                "template_role_reference": member["reference"],
                "old_position": target_member["position"],
                "new_position": {"x": new_x, "y": new_y, "rotation": new_rotation},
            }
        )

    return {
        "template_reference": template_reference,
        "target_reference": target_reference,
        "template_sheet_instance": template_group["sheet_instance"],
        "target_sheet_instance": target_group["sheet_instance"],
        "delta_rotation": delta_rot,
        "change_count": len(changes),
        "changes": changes,
        "skipped": skipped,
    }


def _format_at_number(value: float) -> str:
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return text


def apply_layout_changes(
    project_path: str | Path,
    changes: list[dict[str, Any]],
    write: bool = False,
    allow_while_open: bool = False,
) -> dict[str, Any]:
    """Apply a list of {uuid or reference, new_position:{x,y,rotation}} changes
    (as produced by `diff_layout_template`/`move_group`, or written by hand) to
    the board file's `(at ...)` line for each footprint.

    Either `uuid` or `reference` identifies the footprint - `reference` saves a
    round trip when you already know the designator (e.g. "move R33 to...")
    but haven't looked up its uuid; it's resolved against a single cached
    board parse. Matching for the actual file edit is always by uuid
    underneath (unique per footprint instance in the PCB), and only the very
    next `(at ...)` line after that uuid is touched, which is always the
    footprint's own placement line (property `at` fields come later in the
    block) - so this can't accidentally rewrite an unrelated coordinate
    elsewhere in the file.

    write=False (the default) validates every change resolves to exactly one
    location and returns a preview without touching the file - always run this
    first. Pass write=True once you've reviewed the preview to actually save.

    Refuses to write while KiCad has the board open for editing (a `~<name>.lck`
    file present next to it) unless `allow_while_open=True` - see
    `_check_not_locked_by_editor` for why.
    """
    board_path, _, _ = _resolve_project_path(project_path)
    text = _read_text(board_path)
    applied: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    ref_to_uuid: dict[str, str] | None = None

    for change in changes:
        uuid = change.get("uuid")
        new_pos = change.get("new_position", {})
        if not uuid:
            reference = change.get("reference")
            if reference:
                if ref_to_uuid is None:
                    ref_to_uuid = {
                        c["reference"].strip().upper(): c["uuid"]
                        for c in _parse_board_components_cached(board_path)
                        if c.get("reference") and c.get("uuid")
                    }
                uuid = ref_to_uuid.get(reference.strip().upper())
        if not uuid:
            missing.append({**change, "reason": "no uuid supplied and reference did not resolve to one"})
            continue
        uuid_marker = f'(uuid "{uuid}")'
        idx = text.find(uuid_marker)
        if idx == -1:
            missing.append({**change, "reason": "uuid not found in board file"})
            continue
        if text.find(uuid_marker, idx + 1) != -1:
            missing.append({**change, "reason": "uuid is not unique in board file"})
            continue
        at_idx = text.find("(at ", idx)
        if at_idx == -1:
            missing.append({**change, "reason": "no (at ...) line found after uuid"})
            continue
        end_idx = text.find(")", at_idx)
        old_at = text[at_idx : end_idx + 1]

        x = _format_at_number(new_pos["x"])
        y = _format_at_number(new_pos["y"])
        rotation = new_pos.get("rotation", 0.0) or 0.0
        new_at = f"(at {x} {y} {_format_at_number(rotation)})" if rotation else f"(at {x} {y})"

        applied.append({"reference": change.get("reference"), "uuid": uuid, "old_at": old_at, "new_at": new_at})
        if write:
            text = text[:at_idx] + new_at + text[end_idx + 1 :]

    if write and applied:
        _check_not_locked_by_editor(board_path, allow_while_open)
        with board_path.open("w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        _invalidate_board_cache(board_path)

    return {
        "board_path": str(board_path),
        "write": write,
        "applied_count": len(applied),
        "missing_count": len(missing),
        "applied": applied,
        "missing": missing,
    }


def apply_layout_template(
    project_path: str | Path,
    template_reference: str,
    target_references: list[str],
    write: bool = False,
    allow_while_open: bool = False,
) -> dict[str, Any]:
    """Convenience wrapper: diff `template_reference`'s group against each of
    `target_references` in turn, then apply every resulting change in one pass.

    Example: apply_layout_template(project_path, "U7", ["U8", "U9", "U6"]) will
    reposition every capacitor/resistor/ferrite-bead/connector belonging to U8,
    U9 and U6's thermocouple channels to match U7's (locked) reference layout,
    rotating each group's offsets to account for that channel's own rotation.

    Defaults to a dry run (write=False); inspect `diffs` and `apply_result`
    first, then call again with write=True to commit.
    """
    diffs = []
    all_changes: list[dict[str, Any]] = []
    for target_reference in target_references:
        diff = diff_layout_template(project_path, template_reference, target_reference)
        diffs.append(diff)
        all_changes.extend(diff["changes"])

    apply_result = apply_layout_changes(project_path, all_changes, write=write, allow_while_open=allow_while_open)
    return {
        "template_reference": template_reference,
        "target_references": target_references,
        "diffs": diffs,
        "apply_result": apply_result,
    }


def move_group(
    project_path: str | Path,
    reference: str,
    dx: float = 0.0,
    dy: float = 0.0,
    drotation: float = 0.0,
    to: dict[str, float] | None = None,
    write: bool = False,
    allow_while_open: bool = False,
) -> dict[str, Any]:
    """Rigid-body move: shift every member of `reference`'s hierarchical group
    together, preserving their layout relative to each other.

    Use this instead of `diff_layout_template` when there's no separate
    "known-good" template group to copy from - e.g. relocating an
    already-correct cluster somewhere else on the board, or nudging one
    channel a few mm to clear a routing conflict, without touching how its own
    members are arranged relative to each other.

    Give either (dx, dy) - a plain offset applied to every member - or
    `to={"x":.., "y":..}` to move the anchor to an absolute position (the
    offset is derived from that automatically). `drotation` additionally
    rotates every member's position *and* its own facing around the anchor,
    for e.g. flipping a channel's orientation as part of the move.

    Defaults to a dry run (write=False); inspect `changes` first, then call
    again with write=True to commit.
    """
    group = get_hierarchical_group(project_path, reference)
    anchor = group["anchor"]
    anchor_pos = anchor["position"]

    if to is not None:
        dx = to["x"] - anchor_pos["x"]
        dy = to["y"] - anchor_pos["y"]

    changes: list[dict[str, Any]] = []
    for member in group["members"]:
        pos = member["position"]
        if member["uuid"] == anchor["uuid"]:
            new_x, new_y = pos["x"] + dx, pos["y"] + dy
        else:
            rel_dx = pos["x"] - anchor_pos["x"]
            rel_dy = pos["y"] - anchor_pos["y"]
            if drotation:
                rel_dx, rel_dy = _rotate_point(rel_dx, rel_dy, drotation)
            new_x = anchor_pos["x"] + dx + rel_dx
            new_y = anchor_pos["y"] + dy + rel_dy
        new_rotation = (pos["rotation"] + drotation) % 360 if drotation else pos["rotation"]
        changes.append(
            {
                "reference": member["reference"],
                "uuid": member["uuid"],
                "old_position": pos,
                "new_position": {"x": round(new_x, 6), "y": round(new_y, 6), "rotation": round(new_rotation, 6)},
            }
        )

    apply_result = apply_layout_changes(project_path, changes, write=write, allow_while_open=allow_while_open)
    return {
        "reference": reference,
        "sheet_instance": group["sheet_instance"],
        "dx": dx,
        "dy": dy,
        "drotation": drotation,
        "change_count": len(changes),
        "changes": changes,
        "apply_result": apply_result,
    }


def get_footprint_pads(project_path: str | Path, reference: str) -> dict[str, Any]:
    """Return every pad of `reference`'s footprint - number, net (read straight off
    the board file's own pad `(net ..)` entries, not the schematic pin numbering),
    and absolute board position. This is the pin-level companion to `get_component`
    (which only knows the footprint's own origin/rotation, not where its individual
    pads land) - use it whenever a placement decision depends on where a specific
    pin actually is, e.g. lining a bypass cap's pad up with the IC pin it bypasses.
    """
    board_path, _, _ = _resolve_project_path(project_path)
    footprints = _parse_footprint_pads_cached(board_path)
    lowered = reference.strip().upper()
    for fp in footprints.values():
        if fp["reference"].strip().upper() == lowered:
            return fp
    raise KeyError(f"Component {reference} not found")


def get_pin_position(project_path: str | Path, reference: str, pin: str) -> dict[str, Any]:
    """Look up a single pad's net and absolute board position by reference + pin
    number. Thin convenience wrapper over `get_footprint_pads` for the common case
    of "where exactly is pin N of this part."
    """
    fp = get_footprint_pads(project_path, reference)
    target_pin = str(pin).strip()
    for pad in fp["pads"]:
        if str(pad["number"]).strip() == target_pin:
            return {
                "reference": fp["reference"],
                "pin": pad["number"],
                "net": pad["net"],
                "position": pad["position"],
                "rotation": pad["rotation"],
            }
    raise KeyError(f"Pin {pin} not found on component {reference}")


def pin_distance(project_path: str | Path, reference_a: str, pin_a: str, reference_b: str, pin_b: str) -> dict[str, Any]:
    """Euclidean distance between two specific pads. Use this to check a
    placement's quality before/after - e.g. confirming a decoupling cap's pad
    actually ended up closer to the IC pin it bypasses.
    """
    a = get_pin_position(project_path, reference_a, pin_a)
    b = get_pin_position(project_path, reference_b, pin_b)
    dx = a["position"]["x"] - b["position"]["x"]
    dy = a["position"]["y"] - b["position"]["y"]
    return {
        "a": a,
        "b": b,
        "distance": round(math.hypot(dx, dy), 6),
    }


def align_component_pin(
    project_path: str | Path,
    reference: str,
    pin: str,
    target: dict[str, float],
    rotation: float | None = None,
    write: bool = False,
    allow_while_open: bool = False,
) -> dict[str, Any]:
    """Rigid-move `reference` (translate, and optionally rotate first) so that its
    pad `pin` ends up sitting exactly at absolute board position `target`
    ({"x":.., "y":..}).

    This is the core primitive for datasheet-guided placement: point the pad of a
    passive that's on a given net at the IC pin/pad it needs to reach, rather than
    eyeballing footprint-origin offsets by hand. `rotation` (degrees) sets the
    footprint's orientation before the translation is computed, so both "spin this
    part to face the pin" and "then land the pad on the pin" happen in one call.
    Leave `rotation` unset to keep the part's current orientation.

    Defaults to a dry run (write=False) - inspect `change` first, then call again
    with write=True to commit.
    """
    board_path, _, _ = _resolve_project_path(project_path)
    components = _parse_board_components_cached(board_path)
    component = _find_component_ci(components, reference)
    footprints = _parse_footprint_pads_cached(board_path)
    fp = footprints.get(component["uuid"])
    if fp is None:
        raise KeyError(f"No pad data found for {reference}")
    target_pin = str(pin).strip()
    pad = next((p for p in fp["pads"] if str(p["number"]).strip() == target_pin), None)
    if pad is None:
        raise KeyError(f"Pin {pin} not found on component {reference}")

    old_pos = component["position"]
    new_rotation = rotation if rotation is not None else old_pos["rotation"]
    local = pad["local_position"]
    dx, dy = _rotate_point(local["x"], local["y"], new_rotation)
    new_x = target["x"] - dx
    new_y = target["y"] - dy

    change = {
        "reference": component["reference"],
        "uuid": component["uuid"],
        "old_position": old_pos,
        "new_position": {"x": round(new_x, 6), "y": round(new_y, 6), "rotation": round(new_rotation % 360, 6)},
    }
    apply_result = apply_layout_changes(project_path, [change], write=write, allow_while_open=allow_while_open)
    return {
        "reference": component["reference"],
        "pin": pad["number"],
        "net": pad["net"],
        "target": target,
        "change": change,
        "apply_result": apply_result,
    }


def align_components_to_anchor(
    project_path: str | Path,
    anchor_reference: str,
    alignments: list[dict[str, Any]],
    write: bool = False,
    allow_while_open: bool = False,
) -> dict[str, Any]:
    """Batch version of `align_component_pin`, targeted relative to one anchor's
    pins - the natural shape for "arrange these support parts around this IC to
    match a datasheet layout guide."

    Each entry in `alignments`: {"reference", "pin", "anchor_pin",
    "offset": {"dx":.., "dy":..} (default 0,0), "rotation" (optional degrees)}.
    For each, the target point is anchor_pin's absolute pad position plus offset;
    `reference`'s own `pin` pad is placed there (see `align_component_pin` for
    exactly how). `offset` is what encodes "how far out from the pin, and in which
    direction" - e.g. a bypass cap sits offset a couple mm along the pin's edge of
    the package rather than stacked exactly on top of the pin pad.

    Defaults to a dry run (write=False) - review `results` first, then call again
    with write=True to commit all of them.
    """
    anchor_fp = get_footprint_pads(project_path, anchor_reference)
    anchor_pads = {str(p["number"]).strip(): p for p in anchor_fp["pads"]}

    results: list[dict[str, Any]] = []
    for item in alignments:
        anchor_pin = str(item["anchor_pin"]).strip()
        anchor_pad = anchor_pads.get(anchor_pin)
        if anchor_pad is None:
            results.append({**item, "error": f"anchor pin {anchor_pin} not found on {anchor_reference}"})
            continue
        offset = item.get("offset") or {"dx": 0.0, "dy": 0.0}
        target = {
            "x": anchor_pad["position"]["x"] + offset.get("dx", 0.0),
            "y": anchor_pad["position"]["y"] + offset.get("dy", 0.0),
        }
        try:
            result = align_component_pin(
                project_path,
                item["reference"],
                item["pin"],
                target,
                rotation=item.get("rotation"),
                write=write,
                allow_while_open=allow_while_open,
            )
        except KeyError as exc:
            results.append({**item, "error": str(exc)})
            continue
        results.append(result)

    return {
        "anchor_reference": anchor_reference,
        "write": write,
        "result_count": len(results),
        "results": results,
    }


def _detect_newline(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _append_top_level_block(text: str, block: str) -> str:
    """Insert `block` (a fully-formed top-level s-expression, written with `\\n`
    line separators and no trailing newline) as the last child of the root
    `(kicad_pcb ...)` list, immediately before the file's closing `)`.

    Board files this size (tens of thousands of lines) can't be safely handled
    by parse-mutate-reserialize: re-emitting the whole tree from the parsed
    form would rewrite formatting/whitespace on every unrelated line and blow
    up the diff. This does uuid/text-anchored surgery instead, touching only
    the few lines right before the closing paren - the same approach
    `apply_layout_changes` uses for `(at ...)` edits.

    Reuses whatever line ending the file already has (KiCad board files are
    typically CRLF) - mixing endings would itself show up as a whole-file
    diff the next time KiCad or git normalizes it.
    """
    newline = _detect_newline(text)
    has_trailing_newline = text.endswith(newline)
    body = text[: -len(newline)] if has_trailing_newline else text
    if not body.endswith(")"):
        raise ValueError("Board file does not appear to end with a closing parenthesis")
    line_start = body.rfind(newline, 0, len(body) - 1) + len(newline)
    if body[line_start:] != ")":
        raise ValueError("Could not locate the board's top-level closing parenthesis on its own line")
    block_with_newlines = block.replace("\n", newline)
    new_body = body[:line_start] + block_with_newlines + newline + body[line_start:]
    return new_body + (newline if has_trailing_newline else "")


def list_groups(project_path: str | Path) -> dict[str, Any]:
    """List every top-level PCB group already on the board (KiCad's
    `(group "name" (uuid ..) (members ..))` construct - the thing the GUI's
    Ctrl+G "Group" command writes, letting a cluster of footprints be
    selected/moved as one unit). Each member uuid is resolved back to its
    footprint reference where possible.
    """
    board_path, _, _ = _resolve_project_path(project_path)
    text = _read_text(board_path)
    root = SexprParser(text).parse()
    uuid_to_ref = {
        c["uuid"]: c["reference"]
        for c in _parse_board_components_cached(board_path)
        if c.get("uuid") and c.get("reference")
    }

    groups: list[dict[str, Any]] = []
    for node in root:
        if not (isinstance(node, list) and node and node[0] == "group"):
            continue
        name = node[1] if len(node) > 1 and isinstance(node[1], str) else ""
        group_uuid = ""
        member_uuids: list[str] = []
        for entry in node[1:]:
            if not (isinstance(entry, list) and entry):
                continue
            if entry[0] == "uuid" and len(entry) >= 2 and isinstance(entry[1], str):
                group_uuid = entry[1]
            elif entry[0] == "members":
                member_uuids = [m for m in entry[1:] if isinstance(m, str)]
        members = [{"uuid": u, "reference": uuid_to_ref.get(u, "")} for u in member_uuids]
        groups.append({"name": name, "uuid": group_uuid, "member_count": len(members), "members": members})

    return {"board_path": str(board_path), "group_count": len(groups), "groups": groups}


def create_group(
    project_path: str | Path,
    name: str,
    references: list[str],
    write: bool = False,
    allow_while_open: bool = False,
) -> dict[str, Any]:
    """Create a new named PCB group containing the given footprints, so they
    select/move together as one unit in the KiCad GUI - the same board-level
    construct KiCad itself writes for Ctrl+G. Typical use: after identifying a
    hierarchical sub-circuit's members with `get_hierarchical_group`, group
    them so the layout stays intact when someone drags one part of it.

    `references` are footprint designators (e.g. ["R1", "C4", "U2"]), resolved
    to footprint uuids from the cached board parse. Raises if any reference
    isn't found, or if a reference already belongs to another group - KiCad
    groups don't nest/overlap, and silently double-adding would leave the
    board in a state the GUI can't cleanly represent.

    Defaults to a dry run (write=False) - inspect the result, then call again
    with write=True to actually save.
    """
    board_path, _, _ = _resolve_project_path(project_path)
    components = _parse_board_components_cached(board_path)

    resolved: list[dict[str, str]] = []
    missing: list[str] = []
    for reference in references:
        component = _find_component(components, reference)
        if component is None or not component.get("uuid"):
            missing.append(reference)
            continue
        resolved.append({"reference": component["reference"], "uuid": component["uuid"]})
    if missing:
        raise KeyError(f"Component(s) not found or missing uuid: {', '.join(missing)}")

    existing = list_groups(project_path)
    already_grouped = {
        member["reference"]: group["name"] or group["uuid"]
        for group in existing["groups"]
        for member in group["members"]
        if member["reference"]
    }
    conflicts = {r["reference"]: already_grouped[r["reference"]] for r in resolved if r["reference"] in already_grouped}
    if conflicts:
        raise ValueError(f"Already a member of another group: {conflicts}")

    group_uuid = str(_uuid.uuid4())
    member_uuids = sorted(r["uuid"] for r in resolved)
    member_lines = [
        " ".join(f'"{u}"' for u in member_uuids[i : i + 2]) for i in range(0, len(member_uuids), 2)
    ]
    members_block = "\n\t\t\t".join(member_lines)
    block = f'\t(group "{name}"\n\t\t(uuid "{group_uuid}")\n\t\t(members {members_block}\n\t\t)\n\t)'

    result = {
        "board_path": str(board_path),
        "write": write,
        "name": name,
        "group_uuid": group_uuid,
        "member_count": len(resolved),
        "members": resolved,
    }
    if write:
        _check_not_locked_by_editor(board_path, allow_while_open)
        text = _read_text(board_path)
        new_text = _append_top_level_block(text, block)
        with board_path.open("w", encoding="utf-8", newline="") as handle:
            handle.write(new_text)
        _invalidate_board_cache(board_path)
    return result


def delete_group(
    project_path: str | Path,
    name: str | None = None,
    group_uuid: str | None = None,
    write: bool = False,
    allow_while_open: bool = False,
) -> dict[str, Any]:
    """Delete a top-level PCB group by name or uuid (member footprints are
    untouched - only the grouping is removed). Give `group_uuid` when two
    groups share a name (e.g. multiple unnamed `""` groups); otherwise `name`
    is matched against `list_groups` and must be unique.

    Defaults to a dry run (write=False) - inspect the match, then call again
    with write=True to actually save.
    """
    if not name and not group_uuid:
        raise ValueError("Provide name or group_uuid")

    board_path, _, _ = _resolve_project_path(project_path)
    groups = list_groups(project_path)["groups"]
    if group_uuid:
        matches = [g for g in groups if g["uuid"] == group_uuid]
    else:
        matches = [g for g in groups if g["name"] == name]
    if not matches:
        raise KeyError(f"No group found matching name={name!r} group_uuid={group_uuid!r}")
    if len(matches) > 1:
        raise ValueError(
            f"{len(matches)} groups match name={name!r} - disambiguate with group_uuid "
            f"(candidates: {[g['uuid'] for g in matches]})"
        )
    target = matches[0]

    text = _read_text(board_path)
    uuid_marker = f'(uuid "{target["uuid"]}")'
    uuid_idx = text.find(uuid_marker)
    if uuid_idx == -1:
        raise ValueError("Group uuid not found in board file text (board changed since list_groups was read)")
    group_start = text.rfind("(group", 0, uuid_idx)
    if group_start == -1:
        raise ValueError("Could not locate the enclosing (group ...) block")

    depth = 0
    end_idx = None
    for i in range(group_start, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                end_idx = i + 1
                break
    if end_idx is None:
        raise ValueError("Unbalanced parentheses while locating end of (group ...) block")

    newline = _detect_newline(text)
    line_start = text.rfind(newline, 0, group_start) + len(newline)
    line_end = text.find(newline, end_idx)
    line_end = line_end + len(newline) if line_end != -1 else len(text)

    result = {"board_path": str(board_path), "write": write, "deleted_group": target}
    if write:
        _check_not_locked_by_editor(board_path, allow_while_open)
        new_text = text[:line_start] + text[line_end:]
        with board_path.open("w", encoding="utf-8", newline="") as handle:
            handle.write(new_text)
        _invalidate_board_cache(board_path)
    return result


def list_hierarchical_templates(project_path: str | Path) -> dict[str, Any]:
    """One-shot board-wide overview of every schematic sheet that's stamped out
    more than once (relay channels, thermocouple channels, etc): which file,
    how many instances, and each instance's member references + anchor lock
    state. Replaces the exploratory grepping/reading otherwise needed to
    discover which components belong together and which instance (if any) is
    the locked reference layout to copy from - run this first on any "make
    these repeated sub-circuits consistent" task.
    """
    board_path, _, _ = _resolve_project_path(project_path)
    components = _parse_board_components_cached(board_path)

    by_sheetfile: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for component in components:
        sheetfile = component.get("sheetfile", "")
        instance = component.get("sheet_instance", "")
        if not sheetfile or not instance:
            continue
        by_sheetfile.setdefault(sheetfile, {}).setdefault(instance, []).append(component)

    templates: list[dict[str, Any]] = []
    for sheetfile, instances in by_sheetfile.items():
        if len(instances) < 2:
            continue  # only one instance - nothing to reconcile against
        instance_rows = []
        for instance, members in instances.items():
            locked_refs = sorted(m["reference"] for m in members if m.get("locked") and m.get("reference"))
            instance_rows.append(
                {
                    "sheet_instance": instance,
                    "sheetname": members[0].get("sheetname", ""),
                    "member_count": len(members),
                    "member_references": sorted(m["reference"] for m in members if m.get("reference")),
                    "locked_count": len(locked_refs),
                    "fully_locked": len(locked_refs) == len(members),
                    "locked_references": locked_refs,
                }
            )
        instance_rows.sort(key=lambda row: row["sheetname"])
        templates.append({"sheetfile": sheetfile, "instance_count": len(instance_rows), "instances": instance_rows})

    templates.sort(key=lambda t: t["sheetfile"])
    return {"template_count": len(templates), "templates": templates}


def classify_group_by_anchor_pin(project_path: str | Path, anchor_reference: str) -> dict[str, Any]:
    """For every other member of `anchor_reference`'s hierarchical group, find
    which of the anchor's own pads it shares a net with - i.e. what electrical
    role it plays (VIN cap, feedback divider resistor, etc.), read straight off
    board nets instead of eyeballing a schematic. This is the automatic version
    of manually building a "which part goes with which IC pin" table by hand.

    Each member's `signature` (sorted tuple of anchor pin numbers it touches)
    plus its footprint is normally enough to identify its role; components with
    an empty signature aren't electrically tied to any anchor pin at all (e.g.
    a TVS diode across VIN that only touches the anchor indirectly via another
    part), which `match_group_members_by_role` handles as its own case.
    """
    anchor_pads = get_footprint_pads(project_path, anchor_reference)
    net_to_anchor_pins: dict[str, list[str]] = {}
    for pad in anchor_pads["pads"]:
        net_to_anchor_pins.setdefault(pad["net"], []).append(pad["number"])

    group = get_hierarchical_group(project_path, anchor_reference)
    anchor_uuid = group["anchor"]["uuid"]

    members: list[dict[str, Any]] = []
    for member in group["members"]:
        if member["uuid"] == anchor_uuid:
            continue
        comp = get_component(project_path, member["reference"])
        matches: list[dict[str, Any]] = []
        seen_pins: set[str] = set()
        for membership in comp.get("nets", []):
            net = membership.get("net", "")
            for anchor_pin in net_to_anchor_pins.get(net, []):
                if anchor_pin in seen_pins:
                    continue
                seen_pins.add(anchor_pin)
                matches.append({"anchor_pin": anchor_pin, "net": net, "own_pin": membership.get("pin", "")})
        matches.sort(key=lambda m: m["anchor_pin"])
        members.append(
            {
                "reference": member["reference"],
                "footprint": member.get("footprint", ""),
                "value": member.get("value", ""),
                "position": member.get("position"),
                "anchor_pin_matches": matches,
                "signature": tuple(m["anchor_pin"] for m in matches),
            }
        )

    return {
        "anchor_reference": anchor_reference,
        "sheet_instance": group["sheet_instance"],
        "sheetname": group["sheetname"],
        "members": members,
    }


def _normalize_value(value: str) -> str:
    return "".join(str(value).split()).lower()


def match_group_members_by_role(
    project_path: str | Path,
    template_reference: str,
    target_reference: str,
    overrides: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Match components between two hierarchical groups by which anchor pin
    they connect to, instead of KiCad's own symbol_uuid identity
    (`diff_layout_template`/`apply_layout_template`'s matching key) - that only
    works between instances of the *same* schematic sheet. This works even
    when the two groups come from entirely different sheet files (e.g. two
    independently drawn but functionally analogous regulator circuits), as
    long as their anchors share a compatible pinout.

    Matching key is (footprint, sorted anchor pins connected to). When more
    than one member on either side shares a key, ties are broken by matching
    identical component `value` (e.g. distinguishing a feedback divider's two
    same-footprint resistors by their differing resistance). Anything still
    tied after that is reported under `ambiguous` rather than guessed - pass
    an explicit `overrides` dict ({template_reference: target_reference}) to
    force those pairings once you've eyeballed which is which.
    """
    template = classify_group_by_anchor_pin(project_path, template_reference)
    target = classify_group_by_anchor_pin(project_path, target_reference)
    overrides = overrides or {}

    def bucket(classified: dict[str, Any]) -> dict[tuple[str, tuple], list[dict[str, Any]]]:
        buckets: dict[tuple[str, tuple], list[dict[str, Any]]] = {}
        for m in classified["members"]:
            buckets.setdefault((m["footprint"], m["signature"]), []).append(m)
        return buckets

    template_buckets = bucket(template)
    target_buckets = bucket(target)

    matched: list[dict[str, Any]] = []
    ambiguous: list[dict[str, Any]] = []
    template_unmatched: list[str] = []

    for key, t_members in template_buckets.items():
        candidates = list(target_buckets.get(key, []))
        remaining_t: list[dict[str, Any]] = []
        for tm in t_members:
            forced_ref = overrides.get(tm["reference"])
            forced = next((xm for xm in candidates if xm["reference"] == forced_ref), None) if forced_ref else None
            if forced:
                matched.append(
                    {
                        "template_reference": tm["reference"],
                        "target_reference": forced["reference"],
                        "footprint": key[0],
                        "signature": key[1],
                        "resolved_by": "override",
                    }
                )
                candidates.remove(forced)
            else:
                remaining_t.append(tm)

        if not remaining_t:
            continue
        if len(remaining_t) == 1 and len(candidates) == 1:
            matched.append(
                {
                    "template_reference": remaining_t[0]["reference"],
                    "target_reference": candidates[0]["reference"],
                    "footprint": key[0],
                    "signature": key[1],
                    "resolved_by": "unique_key",
                }
            )
            continue
        if not candidates:
            template_unmatched.extend(m["reference"] for m in remaining_t)
            continue

        candidates_by_value: dict[str, list[dict[str, Any]]] = {}
        for xm in candidates:
            candidates_by_value.setdefault(_normalize_value(xm["value"]), []).append(xm)
        used_target_refs: set[str] = set()
        leftover_t: list[dict[str, Any]] = []
        for tm in remaining_t:
            value_candidates = [
                xm for xm in candidates_by_value.get(_normalize_value(tm["value"]), [])
                if xm["reference"] not in used_target_refs
            ]
            if len(value_candidates) == 1:
                matched.append(
                    {
                        "template_reference": tm["reference"],
                        "target_reference": value_candidates[0]["reference"],
                        "footprint": key[0],
                        "signature": key[1],
                        "resolved_by": "value_match",
                    }
                )
                used_target_refs.add(value_candidates[0]["reference"])
            else:
                leftover_t.append(tm)

        leftover_x = [xm for xm in candidates if xm["reference"] not in used_target_refs]
        if leftover_t or leftover_x:
            ambiguous.append(
                {
                    "footprint": key[0],
                    "signature": key[1],
                    "template_candidates": [m["reference"] for m in leftover_t],
                    "target_candidates": [m["reference"] for m in leftover_x],
                }
            )

    matched_target_refs = {m["target_reference"] for m in matched}
    ambiguous_target_refs = {ref for entry in ambiguous for ref in entry["target_candidates"]}
    target_unmatched = [
        m["reference"]
        for members in target_buckets.values()
        for m in members
        if m["reference"] not in matched_target_refs and m["reference"] not in ambiguous_target_refs
    ]

    return {
        "template_reference": template_reference,
        "target_reference": target_reference,
        "matched": matched,
        "ambiguous": ambiguous,
        "template_unmatched": template_unmatched,
        "target_unmatched": target_unmatched,
    }


def diff_layout_by_role(
    project_path: str | Path,
    template_reference: str,
    target_reference: str,
    overrides: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Like `diff_layout_template`, but for two hierarchical groups that live on
    *different* schematic sheets, so there's no shared symbol_uuid to match on
    - members are matched by which anchor pin they connect to instead (see
    `match_group_members_by_role`), then the same rigid offset+rotation
    transform `diff_layout_template` uses carries the template group's
    relative layout over onto the target anchor's own position/rotation.

    Check `ambiguous`/`template_unmatched`/`target_unmatched` before trusting
    `changes` is complete - anything listed there was deliberately left out
    rather than guessed; resolve it by passing `overrides`
    ({template_reference: target_reference}) and calling again. `changes` is
    ready to hand straight to `apply_layout_changes`.
    """
    match = match_group_members_by_role(project_path, template_reference, target_reference, overrides=overrides)

    template_group = get_hierarchical_group(project_path, template_reference)
    target_group = get_hierarchical_group(project_path, target_reference)
    t_anchor = template_group["anchor"]
    x_anchor = target_group["anchor"]
    delta_rot = (x_anchor["position"]["rotation"] - t_anchor["position"]["rotation"]) % 360

    template_members_by_ref = {m["reference"]: m for m in template_group["members"]}
    target_members_by_ref = {m["reference"]: m for m in target_group["members"]}

    changes: list[dict[str, Any]] = []
    for pair in match["matched"]:
        t_member = template_members_by_ref[pair["template_reference"]]
        x_member = target_members_by_ref[pair["target_reference"]]
        dx = t_member["position"]["x"] - t_anchor["position"]["x"]
        dy = t_member["position"]["y"] - t_anchor["position"]["y"]
        ndx, ndy = _rotate_point(dx, dy, delta_rot)
        new_x = round(x_anchor["position"]["x"] + ndx, 6)
        new_y = round(x_anchor["position"]["y"] + ndy, 6)
        new_rotation = round((t_member["position"]["rotation"] + delta_rot) % 360, 6)
        changes.append(
            {
                "reference": x_member["reference"],
                "uuid": x_member["uuid"],
                "template_role_reference": t_member["reference"],
                "old_position": x_member["position"],
                "new_position": {"x": new_x, "y": new_y, "rotation": new_rotation},
            }
        )

    return {
        "template_reference": template_reference,
        "target_reference": target_reference,
        "delta_rotation": delta_rot,
        "change_count": len(changes),
        "changes": changes,
        "ambiguous": match["ambiguous"],
        "template_unmatched": match["template_unmatched"],
        "target_unmatched": match["target_unmatched"],
    }


def _extract_property_at(block: str, property_name: str) -> tuple[dict[str, float], str]:
    """Pull a child text property's own local `(at x y [rotation])` and `(layer
    ..)` out of one footprint's raw block text (e.g. where the "Reference"
    designator sits on the silkscreen, as opposed to the footprint's own
    position - see `get_property_position`)."""
    pattern = re.compile(r'\(property "%s"[^\n]*\n\s*\(at ([^)]+)\)\s*\n\s*\(layer "([^"]+)"\)' % re.escape(property_name))
    match = pattern.search(block)
    if not match:
        raise KeyError(f'Property "{property_name}" not found')
    parts = match.group(1).split()
    at = {"x": float(parts[0]), "y": float(parts[1]), "rotation": float(parts[2]) if len(parts) > 2 else 0.0}
    return at, match.group(2)


def get_property_position(project_path: str | Path, reference: str, property_name: str = "Reference") -> dict[str, Any]:
    """Return a footprint's child text property's own local `(at x y rotation)`
    and layer - e.g. exactly where the "Reference" designator text sits on the
    silkscreen, relative to the footprint's own origin.

    This is a different number from the footprint's own position/rotation
    (`get_component`): two components at the same board location and
    orientation can still have wildly different label offsets, because the
    label was hand-dragged to dodge a neighbour's silkscreen. Use this to read
    a known-good "reference" instance's label placement before copying it with
    `diff_property_position_template`.
    """
    board_path, _, _ = _resolve_project_path(project_path)
    component = _find_component_ci(_parse_board_components_cached(board_path), reference)
    block_text = _read_text(board_path)
    start, end = _footprint_block_span(block_text, component["uuid"])
    at, layer = _extract_property_at(block_text[start:end], property_name)
    return {"reference": component["reference"], "uuid": component["uuid"], "property": property_name, "at": at, "layer": layer}


def diff_property_position_template(
    project_path: str | Path,
    template_reference: str,
    target_reference: str,
    property_name: str = "Reference",
) -> dict[str, Any]:
    """Compute silkscreen-label (or other child-property text) offset changes
    needed to make every sibling of `target_reference`'s hierarchical group
    match `template_reference`'s group - the property-offset analogue of
    `diff_layout_template`, for when what needs to be copied is *where a text
    label sits on a footprint* rather than the footprint's own position.

    Matches members by symbol_uuid, exactly like `diff_layout_template`.
    Unlike `diff_layout_template`, offsets are copied verbatim rather than
    rotated by the template/target anchor rotation delta: a property `at`
    value's rotation component does not transform under mirroring/rotation by
    a fixed linear rule (verified by hand while flipping footprints to the
    back layer - two properties starting from an identical `at` value came out
    with different rotations after a real KiCad flip, depending on
    hidden/unlocked flags on the property). So this only trusts a verbatim
    copy, and requires the matched pair's own footprint rotation to already be
    identical - anything else is reported in `skipped` rather than guessed.
    """
    template_group = get_hierarchical_group(project_path, template_reference)
    target_group = get_hierarchical_group(project_path, target_reference)
    board_path, _, _ = _resolve_project_path(project_path)
    text = _read_text(board_path)
    target_by_role = {m["symbol_uuid"]: m for m in target_group["members"]}

    changes: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for member in template_group["members"]:
        role = member["symbol_uuid"]
        target_member = target_by_role.get(role)
        if target_member is None:
            skipped.append({"template_reference": member["reference"], "reason": "no matching symbol_uuid in target group"})
            continue
        if round(member["position"]["rotation"] % 360, 3) != round(target_member["position"]["rotation"] % 360, 3):
            skipped.append(
                {
                    "template_reference": member["reference"],
                    "target_reference": target_member["reference"],
                    "reason": (
                        f"footprint rotation differs ({member['position']['rotation']} vs "
                        f"{target_member['position']['rotation']}) - text offset isn't guaranteed to transfer"
                    ),
                }
            )
            continue
        try:
            t_start, t_end = _footprint_block_span(text, member["uuid"])
            template_at, _template_layer = _extract_property_at(text[t_start:t_end], property_name)
        except (KeyError, ValueError):
            skipped.append({"template_reference": member["reference"], "reason": f'template has no "{property_name}" property'})
            continue
        try:
            x_start, x_end = _footprint_block_span(text, target_member["uuid"])
            target_at, _target_layer = _extract_property_at(text[x_start:x_end], property_name)
        except (KeyError, ValueError):
            skipped.append({"target_reference": target_member["reference"], "reason": f'target has no "{property_name}" property'})
            continue
        if template_at == target_at:
            continue
        changes.append(
            {
                "reference": target_member["reference"],
                "uuid": target_member["uuid"],
                "property": property_name,
                "template_role_reference": member["reference"],
                "old_at": target_at,
                "new_at": template_at,
            }
        )

    return {
        "template_reference": template_reference,
        "target_reference": target_reference,
        "property": property_name,
        "change_count": len(changes),
        "changes": changes,
        "skipped": skipped,
    }


def apply_property_position_changes(
    project_path: str | Path,
    changes: list[dict[str, Any]],
    write: bool = False,
    allow_while_open: bool = False,
) -> dict[str, Any]:
    """Apply a list of {uuid or reference, property, new_at:{x,y,rotation}}
    changes (as produced by `diff_property_position_template`) to the matching
    child property's `(at ...)` line inside each footprint's block - the
    property-offset analogue of `apply_layout_changes`.

    Scoped per-change to the named property within that specific footprint's
    own block (found via its uuid, bounded to the next top-level footprint),
    so it can never touch an unrelated property or a same-named property on a
    different footprint.

    write=False (the default) validates and returns a preview without
    touching the file - always run this first, then call again with
    write=True to commit.
    """
    board_path, _, _ = _resolve_project_path(project_path)
    text = _read_text(board_path)
    applied: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    ref_to_uuid: dict[str, str] | None = None

    for change in changes:
        uuid = change.get("uuid")
        property_name = change.get("property", "Reference")
        new_at = change.get("new_at", {})
        if not uuid:
            reference = change.get("reference")
            if reference:
                if ref_to_uuid is None:
                    ref_to_uuid = {
                        c["reference"].strip().upper(): c["uuid"]
                        for c in _parse_board_components_cached(board_path)
                        if c.get("reference") and c.get("uuid")
                    }
                uuid = ref_to_uuid.get(reference.strip().upper())
        if not uuid:
            missing.append({**change, "reason": "no uuid supplied and reference did not resolve to one"})
            continue

        uuid_marker = f'(uuid "{uuid}")'
        idx = text.find(uuid_marker)
        if idx == -1:
            missing.append({**change, "reason": "uuid not found in board file"})
            continue
        if text.find(uuid_marker, idx + 1) != -1:
            missing.append({**change, "reason": "uuid is not unique in board file"})
            continue
        next_fp = text.find("\n\t(footprint ", idx)
        scope_end = next_fp if next_fp != -1 else len(text)
        prop_marker = f'(property "{property_name}"'
        prop_idx = text.find(prop_marker, idx, scope_end)
        if prop_idx == -1:
            missing.append({**change, "reason": f'property "{property_name}" not found on this footprint'})
            continue
        at_idx = text.find("(at ", prop_idx, scope_end)
        if at_idx == -1:
            missing.append({**change, "reason": f'no (at ...) line found after property "{property_name}"'})
            continue
        end_idx = text.find(")", at_idx)
        old_at = text[at_idx : end_idx + 1]

        x = _format_at_number(new_at["x"])
        y = _format_at_number(new_at["y"])
        rotation = new_at.get("rotation", 0.0) or 0.0
        new_at_line = f"(at {x} {y} {_format_at_number(rotation)})" if rotation else f"(at {x} {y})"

        applied.append({"reference": change.get("reference"), "uuid": uuid, "property": property_name, "old_at": old_at, "new_at": new_at_line})
        if write:
            text = text[:at_idx] + new_at_line + text[end_idx + 1 :]

    if write and applied:
        _check_not_locked_by_editor(board_path, allow_while_open)
        with board_path.open("w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        _invalidate_board_cache(board_path)

    return {
        "board_path": str(board_path),
        "write": write,
        "applied_count": len(applied),
        "missing_count": len(missing),
        "applied": applied,
        "missing": missing,
    }


def apply_property_position_template(
    project_path: str | Path,
    template_reference: str,
    target_references: list[str],
    property_name: str = "Reference",
    write: bool = False,
    allow_while_open: bool = False,
) -> dict[str, Any]:
    """Convenience wrapper: diff `template_reference`'s group's `property_name`
    label offsets against each of `target_references` in turn, then apply
    every resulting change in one pass - the property-offset analogue of
    `apply_layout_template`.

    Example: apply_property_position_template(project_path, "U7", ["U8", "U9",
    "U6"]) copies every hand-decluttered "Reference" silkscreen label position
    in U7's group onto the matching component in U8/U9/U6's groups.

    Defaults to a dry run (write=False); inspect `diffs` and `apply_result`
    first, then call again with write=True to commit.
    """
    diffs = []
    all_changes: list[dict[str, Any]] = []
    for target_reference in target_references:
        diff = diff_property_position_template(project_path, template_reference, target_reference, property_name=property_name)
        diffs.append(diff)
        all_changes.extend(diff["changes"])

    apply_result = apply_property_position_changes(project_path, all_changes, write=write, allow_while_open=allow_while_open)
    return {
        "template_reference": template_reference,
        "target_references": target_references,
        "property": property_name,
        "diffs": diffs,
        "apply_result": apply_result,
    }


def _footprint_block_span(text: str, footprint_uuid: str) -> tuple[int, int]:
    """Bracket-matched (start, end) character span of the top-level `(footprint
    ...)` block that owns `footprint_uuid`, honoring quoted strings (so a
    stray "(" or ")" inside a descr/datasheet string can't desync the depth
    count). Used by the flip-template tools and the property-position tools
    above, which need a whole footprint's block rather than a single `(at
    ...)` line.
    """
    marker = f'(uuid "{footprint_uuid}")'
    upos = text.find(marker)
    if upos == -1:
        raise KeyError(f"uuid not found in board file: {footprint_uuid}")
    if text.find(marker, upos + 1) != -1:
        raise ValueError(f"uuid is not unique in board file: {footprint_uuid}")
    start = text.rfind("\t(footprint ", 0, upos)
    if start == -1:
        raise ValueError(f"could not find enclosing (footprint ...) for uuid {footprint_uuid}")
    n = len(text)
    i = start
    depth = 0
    in_str = False
    while i < n:
        c = text[i]
        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    return start, i + 1
        i += 1
    raise ValueError(f"unbalanced parentheses while scanning footprint block for {footprint_uuid}")


def _all_uuids_in_block(block: str) -> list[str]:
    return re.findall(r'\(uuid "([0-9a-fA-F-]{8}-[0-9a-fA-F-]{4}-[0-9a-fA-F-]{4}-[0-9a-fA-F-]{4}-[0-9a-fA-F-]{12})"\)', block)


def _footprint_block_meta(block: str) -> dict[str, Any]:
    ref_m = re.search(r'\(property "Reference" "([^"]*)"', block)
    path_m = re.search(r'\(path "([^"]*)"\)', block)
    sheetname_m = re.search(r'\(sheetname "([^"]*)"\)', block)
    sheetfile_m = re.search(r'\(sheetfile "([^"]*)"\)', block)
    at_m = re.search(r"\n\t\t\(at ([^)]+)\)", block)
    layer_m = re.search(r'\n\t\t\(layer "([^"]+)"\)', block)
    pads = dict(re.findall(r'\(pad "([^"]+)"[^\n]*\n(?:[^\n]*\n)*?\s*\(net "([^"]*)"\)', block))
    return {
        "reference": ref_m.group(1) if ref_m else "",
        "path": path_m.group(1) if path_m else "",
        "sheetname": sheetname_m.group(1) if sheetname_m else "",
        "sheetfile": sheetfile_m.group(1) if sheetfile_m else "",
        "at_top": at_m.group(1) if at_m else "",
        "layer": layer_m.group(1) if layer_m else "",
        "pads": pads,
    }


def diff_flip_template(project_path: str | Path, template_reference: str, target_reference: str) -> dict[str, Any]:
    """Find which members of `target_reference`'s hierarchical group sit on the
    wrong copper side compared to their matching (by symbol_uuid) member in
    `template_reference`'s group - e.g. the template channel has 4 support
    parts deliberately flipped to the back to save front-side space, but this
    target channel still has all of them on the front.

    Read-only; pass `changes` to `apply_flip_template` to actually flip them.
    Rotation mismatches between a matched pair are reported under `skipped`
    rather than attempted - see `apply_flip_template` for why that transform
    isn't safe to guess at.
    """
    template_group = get_hierarchical_group(project_path, template_reference, verbose=True)
    target_group = get_hierarchical_group(project_path, target_reference, verbose=True)
    t_anchor = template_group["anchor"]
    target_by_role = {m["symbol_uuid"]: m for m in target_group["members"]}

    changes: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for member in template_group["members"]:
        role = member["symbol_uuid"]
        if role == t_anchor["symbol_uuid"]:
            continue
        target_member = target_by_role.get(role)
        if target_member is None:
            skipped.append({"template_reference": member["reference"], "reason": "no matching symbol_uuid in target group"})
            continue
        template_layer = member.get("properties", {}).get("layer", "")
        target_layer = target_member.get("properties", {}).get("layer", "")
        if template_layer == target_layer:
            continue
        if round(member["position"]["rotation"] % 360, 3) != round(target_member["position"]["rotation"] % 360, 3):
            skipped.append(
                {
                    "template_reference": member["reference"],
                    "target_reference": target_member["reference"],
                    "reason": (
                        f"footprint rotation differs ({member['position']['rotation']} vs "
                        f"{target_member['position']['rotation']}) - flip template won't transfer cleanly"
                    ),
                }
            )
            continue
        changes.append(
            {
                "reference": target_member["reference"],
                "uuid": target_member["uuid"],
                "template_role_reference": member["reference"],
                "template_uuid": member["uuid"],
                "from_layer": target_layer,
                "to_layer": template_layer,
            }
        )

    return {
        "template_reference": template_reference,
        "target_reference": target_reference,
        "change_count": len(changes),
        "changes": changes,
        "skipped": skipped,
    }


def apply_flip_template(
    project_path: str | Path,
    template_reference: str,
    target_references: list[str],
    write: bool = False,
    allow_while_open: bool = False,
) -> dict[str, Any]:
    """Flip every part of `target_references`' hierarchical groups that needs it
    to match `template_reference`'s group's front/back layer split (see
    `diff_flip_template`), by cloning the template member's already-correctly-
    flipped footprint block onto the target footprint - mirrored
    silkscreen/fab graphics, swapped F./B. layer names, `justify mirror` text
    flags, and adjusted pad angles, i.e. everything KiCad's own Flip command
    produces - while keeping the target's own identity: its uuid, schematic
    path/sheetname/sheetfile, board position, and (matched by pad number) its
    own net names.

    This exists because deriving a front/back flip transform per-field from
    scratch is genuinely unsafe to hand-roll: a text property's stored
    rotation does not transform under mirroring by one fixed rule (verified by
    hand - two child properties starting from an identical `at` value came out
    with different final rotations after a real KiCad flip, depending on
    hidden/unlocked flags), so the only trustworthy source of truth for "what
    does a correctly-flipped instance of this footprint look like" is an
    instance KiCad itself already flipped. `template_reference`'s group must
    already contain one for every role that needs flipping - this clones a
    real flip, it does not compute one.

    Defaults to a dry run (write=False) - inspect `flipped`/`failed`, then
    call again with write=True to commit.
    """
    board_path, _, _ = _resolve_project_path(project_path)

    diffs = []
    all_changes: list[dict[str, Any]] = []
    for target_reference in target_references:
        diff = diff_flip_template(project_path, template_reference, target_reference)
        diffs.append(diff)
        all_changes.extend(diff["changes"])

    text = _read_text(board_path)
    prepared: list[tuple[int, int, str, dict[str, Any]]] = []
    failed: list[dict[str, Any]] = []

    for change in all_changes:
        try:
            t_start, t_end = _footprint_block_span(text, change["template_uuid"])
            x_start, x_end = _footprint_block_span(text, change["uuid"])
        except (KeyError, ValueError) as exc:
            failed.append({**change, "reason": str(exc)})
            continue

        template_block = text[t_start:t_end]
        target_block = text[x_start:x_end]
        tmpl_uuids = _all_uuids_in_block(template_block)
        tgt_uuids = _all_uuids_in_block(target_block)
        if len(tmpl_uuids) != len(tgt_uuids):
            failed.append(
                {
                    **change,
                    "reason": (
                        f"child element count differs (template {len(tmpl_uuids)} vs target {len(tgt_uuids)}) "
                        "- not the same library footprint"
                    ),
                }
            )
            continue

        tmpl_meta = _footprint_block_meta(template_block)
        tgt_meta = _footprint_block_meta(target_block)

        new_block = template_block
        for tu, gu in zip(tmpl_uuids, tgt_uuids):
            new_block = new_block.replace(f'(uuid "{tu}")', f'(uuid "{gu}")', 1)
        new_block = new_block.replace(f'"Reference" "{tmpl_meta["reference"]}"', f'"Reference" "{tgt_meta["reference"]}"', 1)
        new_block = new_block.replace(f'(path "{tmpl_meta["path"]}")', f'(path "{tgt_meta["path"]}")', 1)
        new_block = new_block.replace(f'(sheetname "{tmpl_meta["sheetname"]}")', f'(sheetname "{tgt_meta["sheetname"]}")', 1)
        new_block = new_block.replace(f'(sheetfile "{tmpl_meta["sheetfile"]}")', f'(sheetfile "{tgt_meta["sheetfile"]}")', 1)

        tmpl_xy = tmpl_meta["at_top"].split()
        tgt_xy = tgt_meta["at_top"].split()
        tmpl_rot = tmpl_xy[2] if len(tmpl_xy) > 2 else None
        tgt_rot = tgt_xy[2] if len(tgt_xy) > 2 else None
        if tmpl_rot != tgt_rot:
            failed.append({**change, "reason": f"rotation mismatch at apply time (template {tmpl_rot!r} vs target {tgt_rot!r})"})
            continue
        new_block = new_block.replace(f'\n\t\t(at {tmpl_meta["at_top"]})', f'\n\t\t(at {" ".join(tgt_xy)})', 1)

        ok = True
        for padnum, tmpl_net in tmpl_meta["pads"].items():
            tgt_net = tgt_meta["pads"].get(padnum)
            if tgt_net is None:
                failed.append({**change, "reason": f"target has no pad {padnum} to carry a net from"})
                ok = False
                break
            pad_pattern = re.compile(r'(\(pad "%s"[^\n]*\n(?:[^\n]*\n)*?\s*\(net )"%s"' % (re.escape(padnum), re.escape(tmpl_net)))
            new_block, subcount = pad_pattern.subn(lambda m: m.group(1) + f'"{tgt_net}"', new_block, count=1)
            if subcount != 1:
                failed.append({**change, "reason": f"could not locate net field for pad {padnum} in cloned block"})
                ok = False
                break
        if not ok:
            continue

        prepared.append((x_start, x_end, new_block, change))

    prepared.sort(key=lambda item: item[0], reverse=True)
    if write:
        for x_start, x_end, new_block, _change in prepared:
            text = text[:x_start] + new_block + text[x_end:]
        if prepared:
            _check_not_locked_by_editor(board_path, allow_while_open)
            with board_path.open("w", encoding="utf-8", newline="") as handle:
                handle.write(text)
            _invalidate_board_cache(board_path)

    flipped = [
        {
            "reference": change["reference"],
            "uuid": change["uuid"],
            "from_layer": change["from_layer"],
            "to_layer": change["to_layer"],
            "template_role_reference": change["template_role_reference"],
        }
        for _x_start, _x_end, _new_block, change in prepared
    ]

    return {
        "board_path": str(board_path),
        "write": write,
        "template_reference": template_reference,
        "target_references": target_references,
        "diffs": diffs,
        "flipped_count": len(flipped),
        "flipped": flipped,
        "failed_count": len(failed),
        "failed": failed,
    }


# Empirically-corrected envelope radii (mm) for footprints whose real package
# body is significantly bigger than their pad span would suggest - e.g. an
# electrolytic can's leads sit close together but the body towers over them.
# Checked first in `estimate_footprint_radius`, before falling back to a
# package-code-derived or pad-derived estimate.
_FOOTPRINT_RADIUS_OVERRIDES: dict[str, float] = {
    "SamacSys_Parts:CAPAE1360X1450N": 7.3,  # 470uF electrolytic can, ~13.6x14.5mm body
    "SamacSys_Parts:1935161": 5.2,  # screw-terminal connector
    "SamacSys_Parts:SS12FP": 1.6,
    "SamacSys_Parts:SOP65P640X120-21N": 3.5,  # LT8631 FE-20 body
    "Inductor_SMD:L_Bourns_SRP1038C_10.0x10.0mm": 5.2,
    "Inductor_SMD:L_7.3x7.3_H4.5": 3.7,
}

# Standard KiCad SMD footprint names encode body size as two 2-digit groups in
# tenths of a mm immediately before a literal "Metric" suffix (e.g.
# "..._1206_3216Metric" -> 3.2mm x 1.6mm). "Metric" must be required, not
# optional: names like "R_0402_1005Metric" also contain an unrelated imperial
# package code ("0402") earlier in the string that a looser pattern matches
# first, silently returning the wrong (much smaller) dimensions.
_PACKAGE_CODE_RE = re.compile(r"(\d{2})(\d{2})Metric")


def estimate_footprint_radius(project_path: str | Path, reference: str) -> float:
    """Best-effort collision-check radius (mm) for a footprint: a known-good
    manual override if we have one, else half the larger dimension parsed out
    of a standard KiCad SMD footprint name, else half the pad bounding-box
    diagonal (with a buffer), else a conservative 2.0mm default. Centralizes
    the radius table that used to get re-typed by hand in every one-off
    placement script.
    """
    footprint_name = get_component(project_path, reference)["footprint"]
    if footprint_name in _FOOTPRINT_RADIUS_OVERRIDES:
        return _FOOTPRINT_RADIUS_OVERRIDES[footprint_name]

    match = _PACKAGE_CODE_RE.search(footprint_name)
    if match:
        w, h = int(match.group(1)) / 10.0, int(match.group(2)) / 10.0
        if 0.2 <= w <= 25 and 0.2 <= h <= 25:
            return round(max(w, h) / 2.0 * 1.15, 3)

    try:
        pads = get_footprint_pads(project_path, reference)["pads"]
        if pads:
            radius = max(math.hypot(p["local_position"]["x"], p["local_position"]["y"]) for p in pads)
            if radius > 0:
                return round(radius * 1.2, 3)
    except KeyError:
        pass
    return 2.0


def find_layout_collisions(
    project_path: str | Path,
    references: list[str],
    extra_search_radius: float = 25.0,
    margin: float = 0.4,
) -> dict[str, Any]:
    """Collision-check a set of footprints (typically one hierarchical group's
    members) both against each other and against any *other* board component
    within `extra_search_radius` mm of any of them - catching e.g. a group's
    inductor ending up on top of an unrelated connector from a different
    subsystem, which a same-group-only check would miss. Uses
    `estimate_footprint_radius` for every part's envelope, so no per-footprint
    radius table needs to be built by the caller. Read-only.
    """
    ref_set = {r.strip().upper() for r in references}
    items = [
        {
            "reference": ref,
            "position": get_component(project_path, ref)["position"],
            "radius": estimate_footprint_radius(project_path, ref),
        }
        for ref in references
    ]

    obstacles: list[dict[str, Any]] = []
    for comp in list_components(project_path, limit=5000):
        ref = comp.get("reference", "")
        if not ref or ref.strip().upper() in ref_set:
            continue
        pos = comp["position"]
        if any(math.hypot(pos["x"] - it["position"]["x"], pos["y"] - it["position"]["y"]) <= extra_search_radius for it in items):
            obstacles.append({"reference": ref, "position": pos, "radius": estimate_footprint_radius(project_path, ref)})

    collisions: list[dict[str, Any]] = []
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            a, b = items[i], items[j]
            dist = math.hypot(a["position"]["x"] - b["position"]["x"], a["position"]["y"] - b["position"]["y"])
            need = a["radius"] + b["radius"] + margin
            if dist < need:
                collisions.append({"a": a["reference"], "b": b["reference"], "distance": round(dist, 3), "required": round(need, 3), "kind": "internal"})
        for o in obstacles:
            a = items[i]
            dist = math.hypot(a["position"]["x"] - o["position"]["x"], a["position"]["y"] - o["position"]["y"])
            need = a["radius"] + o["radius"] + margin
            if dist < need:
                collisions.append({"a": a["reference"], "b": o["reference"], "distance": round(dist, 3), "required": round(need, 3), "kind": "external"})

    return {
        "references": references,
        "obstacle_count": len(obstacles),
        "obstacles_checked": [o["reference"] for o in obstacles],
        "collision_count": len(collisions),
        "collisions": collisions,
    }


def nudge_to_clear(
    project_path: str | Path,
    reference: str,
    avoid_references: list[str] | None = None,
    search_radius: float = 25.0,
    margin: float = 0.4,
    max_search_radius: float = 20.0,
    step: float = 0.2,
    write: bool = False,
    allow_while_open: bool = False,
) -> dict[str, Any]:
    """Move `reference` the minimum distance needed to clear a collision,
    searching outward in a ring pattern from its *current* position so it
    stays as close as possible to wherever it already was - its position is
    usually already intentional (e.g. near the pin it's electrically tied to)
    and only needs a small nudge to stop overlapping a neighbour, rather than
    a full re-placement.

    Obstacles default to every other board component within `search_radius`mm
    of `reference`'s current position (radii from `estimate_footprint_radius`);
    pass `avoid_references` to check against an explicit list instead (e.g. a
    group's own just-placed members plus any nearby foreign parts you already
    found with `find_layout_collisions`).

    Defaults to a dry run (write=False) - inspect `new_position`, then call
    again with write=True to commit.
    """
    comp = get_component(project_path, reference)
    cx, cy = comp["position"]["x"], comp["position"]["y"]
    radius = estimate_footprint_radius(project_path, reference)

    if avoid_references:
        obstacle_refs = [r for r in avoid_references if r.strip().upper() != reference.strip().upper()]
    else:
        obstacle_refs = [
            c["reference"]
            for c in list_components(project_path, limit=5000)
            if c.get("reference")
            and c["reference"].strip().upper() != reference.strip().upper()
            and math.hypot(c["position"]["x"] - cx, c["position"]["y"] - cy) <= search_radius
        ]

    obstacles = [
        {"reference": r, "position": get_component(project_path, r)["position"], "radius": estimate_footprint_radius(project_path, r)}
        for r in obstacle_refs
    ]

    def free(x: float, y: float) -> bool:
        return all(
            math.hypot(x - o["position"]["x"], y - o["position"]["y"]) >= radius + o["radius"] + margin
            for o in obstacles
        )

    if free(cx, cy):
        new_x, new_y = cx, cy
    else:
        found = None
        r = step
        while r <= max_search_radius and found is None:
            n = max(8, int(2 * math.pi * r / step))
            for i in range(n):
                ang = 2 * math.pi * i / n
                x, y = cx + r * math.cos(ang), cy + r * math.sin(ang)
                if free(x, y):
                    found = (x, y)
                    break
            r += step
        if found is None:
            raise RuntimeError(f"No free spot found for {reference} within {max_search_radius}mm")
        new_x, new_y = found

    change = {
        "reference": reference,
        "uuid": comp["uuid"],
        "old_position": comp["position"],
        "new_position": {"x": round(new_x, 4), "y": round(new_y, 4), "rotation": comp["position"]["rotation"]},
    }
    apply_result = apply_layout_changes(project_path, [change], write=write, allow_while_open=allow_while_open)
    return {
        "reference": reference,
        "radius": radius,
        "obstacles_checked": len(obstacles),
        "moved": (round(new_x, 4), round(new_y, 4)) != (round(cx, 4), round(cy, 4)),
        "new_position": change["new_position"],
        "apply_result": apply_result,
    }


def find_components_by_net(project_path: str | Path, net_name: str) -> dict[str, Any]:
    board_path, _, netlist_path = _resolve_project_path(project_path)
    components = _parse_board_components_cached(board_path)
    nets = _parse_nets_cached(netlist_path)
    components = _attach_net_info_to_components(components, nets)
    net = _get_net_details(nets, net_name)
    refs = [node.get("ref", "") for node in net.get("nodes", []) if node.get("ref", "")]
    matched = [component for component in components if component.get("reference", "") in refs]
    return {
        "net": net,
        "component_references": refs,
        "components": matched,
        "component_count": len(matched),
    }


def get_net(project_path: str | Path, net_name: str) -> dict[str, Any]:
    _, _, netlist_path = _resolve_project_path(project_path)
    nets = _parse_nets_cached(netlist_path)
    net = _get_net_details(nets, net_name)
    return {
        "net": net,
        "component_references": [node.get("ref", "") for node in net.get("nodes", [])],
        "component_count": len(net.get("nodes", [])),
    }


def list_nets(project_path: str | Path) -> list[dict[str, Any]]:
    _, _, netlist_path = _resolve_project_path(project_path)
    return _parse_nets_cached(netlist_path)


def get_component_info(board_path: str | Path) -> dict[str, Any]:
    return inspect_project(board_path)


def search_component_by_reference(project_path: str | Path, reference: str) -> dict[str, Any]:
    """Search for a component by reference designator and return line numbers in the PCB file.

    Returns the line number where the footprint section starts and a preview of the section.
    """
    board_path, _, _ = _resolve_project_path(project_path)
    board_text = _read_text(board_path)
    lines = board_text.split('\n')

    search_ref = reference.strip().upper()
    results: list[dict[str, Any]] = []

    # Search for property "Reference" lines that match
    for line_num, line in enumerate(lines, 1):
        if 'property "Reference"' in line and search_ref in line.upper():
            # Find the footprint section start (work backwards)
            footprint_start = None
            for i in range(line_num - 1, max(0, line_num - 50), -1):
                if lines[i - 1].strip().startswith('(footprint'):
                    footprint_start = i
                    break

            # Find the footprint section end (work forwards)
            footprint_end = None
            paren_count = 0
            if footprint_start:
                in_section = False
                for i in range(footprint_start - 1, len(lines)):
                    for char in lines[i]:
                        if char == '(':
                            paren_count += 1
                            in_section = True
                        elif char == ')':
                            paren_count -= 1
                            if in_section and paren_count == 0:
                                footprint_end = i + 1
                                break
                    if footprint_end:
                        break

            results.append({
                "reference": reference,
                "reference_line": line_num,
                "section_start": footprint_start,
                "section_end": footprint_end,
                "preview_lines": {
                    "start": max(1, (footprint_start or line_num) - 1),
                    "end": min(len(lines), (footprint_end or line_num) + 1),
                }
            })

    if not results:
        raise KeyError(f"Component {reference} not found in PCB file")

    return {
        "project_path": str(project_path),
        "board_path": str(board_path),
        "search_reference": reference,
        "matches": results,
        "match_count": len(results),
        "total_lines": len(lines),
    }


def _resolve_schematic_dir(project_path: str | Path) -> Path:
    """Resolve a project directory containing `.kicad_sch` files. Deliberately
    independent of `_resolve_project_path` (which requires a `.kicad_pcb` to
    exist) - schematic parsing shouldn't fail just because the board hasn't
    been laid out yet.
    """
    path = Path(project_path).expanduser().resolve()
    if path.is_dir():
        return path
    if path.suffix.lower() in {".kicad_sch", ".kicad_pcb", ".kicad_pro"}:
        return path.parent
    raise ValueError(f"Unsupported KiCad path: {path}")


def _list_schematic_files(directory: Path) -> list[Path]:
    return sorted(
        p for p in directory.glob("*.kicad_sch")
        if not p.name.startswith("_autosave-") and not p.name.startswith("~")
    )


def _root_schematic_path(directory: Path) -> Path | None:
    """Root schematic filename always matches the KiCad project name (e.g.
    kiln.kicad_pro -> kiln.kicad_sch), mirroring how `_resolve_project_path`
    locates the board file.
    """
    for pattern in ("*.kicad_pro", "*.kicad_pcb"):
        candidates = sorted(p for p in directory.glob(pattern) if not p.name.startswith("_autosave-"))
        for candidate in candidates:
            root = directory / f"{candidate.stem}.kicad_sch"
            if root.exists():
                return root
    return None


def _parse_schematic_sheet_files(sch_path: Path) -> list[str]:
    """Every `Sheetfile` property named on a `(sheet ...)` block in this file -
    i.e. which other .kicad_sch files this one instantiates as a sub-sheet.
    """
    text = _read_text(sch_path)
    root = SexprParser(text).parse()
    sheetfiles: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, list):
            if node and node[0] == "sheet":
                for entry in node[1:]:
                    if (
                        isinstance(entry, list) and len(entry) >= 3
                        and entry[0] == "property" and entry[1] == "Sheetfile"
                        and isinstance(entry[2], str)
                    ):
                        sheetfiles.append(entry[2])
            for child in node:
                walk(child)

    walk(root)
    return sheetfiles


def _reachable_schematic_files(directory: Path) -> list[Path]:
    """Only the .kicad_sch files actually reachable via `(sheet ...)` blocks
    starting from the project's root schematic - deliberately NOT every
    .kicad_sch file sitting in the directory. KiCad projects routinely
    accumulate orphaned sheet files (leftover "untitled.kicad_sch" scratch
    sheets, disconnected subsystem pages from an earlier design iteration)
    that were never wired into the design via a (sheet ...) instance;
    including those would silently inflate BOM quantities and can introduce
    phantom duplicate reference designators for parts that were never
    actually placed on the real design.
    """
    root_path = _root_schematic_path(directory)
    if root_path is None:
        return _list_schematic_files(directory)  # no identifiable project file - best effort

    visited: dict[str, Path] = {}
    stack = [root_path]
    while stack:
        current = stack.pop()
        if current.name in visited or not current.exists():
            continue
        visited[current.name] = current
        for sheetfile in _parse_schematic_sheet_files(current):
            stack.append(directory / sheetfile)
    return sorted(visited.values(), key=lambda p: p.name)


_REF_RE = re.compile(r"^([A-Za-z_]*)(\d+)?$")


def _reference_sort_key(reference: str) -> tuple[str, int, str]:
    match = _REF_RE.match(reference or "")
    if not match:
        return (reference or "", -1, reference or "")
    prefix, digits = match.group(1), match.group(2)
    return (prefix, int(digits) if digits else -1, reference)


def _parse_schematic_instances(entry: list[Any]) -> list[dict[str, Any]]:
    """Flatten a symbol's `(instances (project "name" (path "..." (reference "X") (unit N)) ...))`
    block. Each `path` entry is one physical placement of the symbol - a
    hierarchical sheet stamped out more than once (e.g. Thermocouple.kicad_sch
    used for 5 channels) yields one path/reference per stamped-out instance,
    all sharing the same underlying symbol definition and properties.
    """
    instances: list[dict[str, Any]] = []
    for project_entry in entry[1:]:
        if not (isinstance(project_entry, list) and project_entry and project_entry[0] == "project"):
            continue
        project_name = project_entry[1] if len(project_entry) > 1 and isinstance(project_entry[1], str) else ""
        for path_entry in project_entry[2:]:
            if not (isinstance(path_entry, list) and path_entry and path_entry[0] == "path"):
                continue
            path_str = path_entry[1] if len(path_entry) > 1 and isinstance(path_entry[1], str) else ""
            reference = ""
            path_unit = 1
            for field in path_entry[2:]:
                if not (isinstance(field, list) and field):
                    continue
                if field[0] == "reference" and len(field) > 1 and isinstance(field[1], str):
                    reference = field[1]
                elif field[0] == "unit" and len(field) > 1 and _is_number(str(field[1])):
                    path_unit = int(float(field[1]))
            if reference:
                instances.append({"project": project_name, "path": path_str, "reference": reference, "unit": path_unit})
    return instances


def _parse_one_schematic_symbol(node: list[Any]) -> dict[str, Any]:
    lib_id = ""
    symbol_uuid = ""
    unit = 1
    dnp = False
    in_bom = True
    on_board = True
    properties: dict[str, str] = {}
    pins: list[str] = []
    instances: list[dict[str, Any]] = []

    for entry in node[1:]:
        if not (isinstance(entry, list) and entry):
            continue
        tag = entry[0]
        if tag == "lib_id" and len(entry) >= 2 and isinstance(entry[1], str):
            lib_id = entry[1]
        elif tag == "uuid" and len(entry) >= 2 and isinstance(entry[1], str):
            symbol_uuid = entry[1]
        elif tag == "unit" and len(entry) >= 2 and _is_number(str(entry[1])):
            unit = int(float(entry[1]))
        elif tag == "dnp" and len(entry) >= 2:
            dnp = entry[1] == "yes"
        elif tag == "in_bom" and len(entry) >= 2:
            in_bom = entry[1] == "yes"
        elif tag == "on_board" and len(entry) >= 2:
            on_board = entry[1] == "yes"
        elif tag == "property" and len(entry) >= 3 and isinstance(entry[1], str):
            properties[entry[1]] = str(entry[2])
        elif tag == "pin" and len(entry) >= 2 and isinstance(entry[1], str):
            pins.append(entry[1])
        elif tag == "instances":
            instances.extend(_parse_schematic_instances(entry))

    return {
        "lib_id": lib_id,
        "symbol_uuid": symbol_uuid,
        "unit": unit,
        "dnp": dnp,
        "in_bom": in_bom,
        "on_board": on_board,
        "properties": properties,
        "pins": pins,
        "instances": instances,
    }


def _parse_schematic_symbols(sch_path: Path) -> list[dict[str, Any]]:
    """Parse every *placed* symbol instance out of one `.kicad_sch` file - i.e.
    `(symbol ...)` blocks that carry both a `lib_id` and an `instances` block.
    That combination is what distinguishes an actual placed component from the
    `(symbol ...)` unit/graphic sub-blocks nested inside the file's
    `lib_symbols` library cache, which have neither.
    """
    text = _read_text(sch_path)
    root = SexprParser(text).parse()
    symbols: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, list):
            if node and node[0] == "symbol":
                has_lib_id = any(isinstance(e, list) and e and e[0] == "lib_id" for e in node[1:])
                has_instances = any(isinstance(e, list) and e and e[0] == "instances" for e in node[1:])
                if has_lib_id and has_instances:
                    symbols.append(_parse_one_schematic_symbol(node))
                    return
            for child in node:
                walk(child)

    walk(root)
    return symbols


_schematic_symbol_cache: dict[str, tuple[float, int, list[dict[str, Any]]]] = {}


def _parse_schematic_symbols_cached(sch_path: Path) -> list[dict[str, Any]]:
    stat = sch_path.stat()
    key = str(sch_path)
    cached = _schematic_symbol_cache.get(key)
    if cached is not None and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
        return cached[2]
    symbols = _parse_schematic_symbols(sch_path)
    _schematic_symbol_cache[key] = (stat.st_mtime, stat.st_size, symbols)
    return symbols


def _flatten_schematic_components(directory: Path) -> list[dict[str, Any]]:
    """One row per placed reference designator across every `.kicad_sch` file
    reachable from the project's root sheet (see _reachable_schematic_files -
    NOT every .kicad_sch file in `directory`), expanding each symbol's
    `instances` block (a symbol drawn once on a hierarchical sheet like
    Thermocouple.kicad_sch becomes one row per channel it's stamped out into,
    e.g. U6/U7/U8/U9).
    """
    components: list[dict[str, Any]] = []
    for sch_path in _reachable_schematic_files(directory):
        for symbol in _parse_schematic_symbols_cached(sch_path):
            properties = symbol["properties"]
            for instance in symbol["instances"]:
                components.append(
                    {
                        "reference": instance["reference"],
                        "unit": instance["unit"],
                        "value": properties.get("Value", ""),
                        "footprint": properties.get("Footprint", ""),
                        "lib_id": symbol["lib_id"],
                        "dnp": symbol["dnp"],
                        "in_bom": symbol["in_bom"],
                        "on_board": symbol["on_board"],
                        "properties": properties,
                        "pins": symbol["pins"],
                        "symbol_uuid": symbol["symbol_uuid"],
                        "sheetfile": sch_path.name,
                        "sheet_path": instance["path"],
                        "project": instance["project"],
                    }
                )
    components.sort(key=lambda c: _reference_sort_key(c["reference"]))
    return components


def list_schematic_parts(project_path: str | Path) -> dict[str, Any]:
    """Group every placed schematic symbol instance (across all `.kicad_sch`
    files in the project) into unique BOM-style part rows, grouped by
    Value + Footprint - the same grouping KiCad's own BOM exporter uses to
    produce kiln.csv. Each row lists every reference designator that shares
    it and a total quantity. Use this instead of trusting kiln.csv when the
    exported BOM might be stale relative to the live schematic, or to find
    the reference designators for a part before calling get_schematic_part.
    """
    directory = _resolve_schematic_dir(project_path)
    components = _flatten_schematic_components(directory)

    groups: dict[tuple[str, str], dict[str, Any]] = {}
    order: list[tuple[str, str]] = []
    for component in components:
        # Power symbols (lib_id "power:GND", "power:VPP", etc, reference "#PWR..")
        # are schematic-only net markers, not orderable parts - KiCad's own BOM
        # exporter excludes them from kiln.csv the same way, by the "#" reference
        # prefix it auto-assigns them and never lets the user rename.
        if component["lib_id"].startswith("power:") or component["reference"].startswith("#"):
            continue
        key = (component["value"], component["footprint"])
        group = groups.get(key)
        if group is None:
            properties = component["properties"]
            group = {
                "value": component["value"],
                "footprint": component["footprint"],
                "lib_id": component["lib_id"],
                "description": properties.get("Description", ""),
                "datasheet": properties.get("Datasheet", ""),
                "manufacturer": properties.get("Manufacturer_Name", ""),
                "manufacturer_part_number": properties.get("Manufacturer_Part_Number", ""),
                "references": [],
                "dnp_references": [],
            }
            groups[key] = group
            order.append(key)
        group["references"].append(component["reference"])
        if component["dnp"]:
            group["dnp_references"].append(component["reference"])

    parts: list[dict[str, Any]] = []
    for key in order:
        group = groups[key]
        group["references"].sort(key=_reference_sort_key)
        group["dnp_references"].sort(key=_reference_sort_key)
        group["quantity"] = len(group["references"])
        if not group["dnp_references"]:
            del group["dnp_references"]
        parts.append(group)

    parts.sort(key=lambda p: _reference_sort_key(p["references"][0]) if p["references"] else ("", -1, ""))

    return {
        "schematic_dir": str(directory),
        "component_count": len(components),
        "unique_part_count": len(parts),
        "parts": parts,
    }


def get_schematic_part(project_path: str | Path, reference: str) -> dict[str, Any]:
    """Look up one placed schematic symbol by reference designator (e.g. a
    reference returned in list_schematic_parts' `references`) and return every
    property KiCad stores on it - Value, Footprint, Datasheet, Manufacturer_*,
    Mouser fields, Sim.* fields, whatever the symbol carries - plus its pin
    list and which schematic sheet file/instance it was placed on.
    """
    directory = _resolve_schematic_dir(project_path)
    components = _flatten_schematic_components(directory)
    lowered = reference.strip().upper()
    for component in components:
        if component["reference"].strip().upper() == lowered:
            return component
    raise KeyError(f"Schematic symbol {reference} not found")


_CAPACITOR_REF_RE = re.compile(r"^C\d+$")
_VOLTAGE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*[vV](?![a-zA-Z])")


def _extract_voltage(value: str) -> tuple[str, float] | None:
    """Pull a voltage rating out of a capacitor Value string, e.g. "47uF 16V" ->
    ("16V", 16.0). Requires the number to be immediately followed by v/V not
    itself followed by another letter, so it can't mistake the "V" hiding
    inside an unrelated unit or word for a rating; there is no unit prefix
    (m/u/n/p) between the digits and the "V" for volts the way there is for
    farads, so this is unambiguous for capacitor values in practice.
    """
    match = _VOLTAGE_RE.search(value or "")
    if not match:
        return None
    return match.group(0).strip(), float(match.group(1))


def _coerce_voltage(voltage: str | float | None) -> float | None:
    if voltage is None:
        return None
    if isinstance(voltage, (int, float)):
        return float(voltage)
    extracted = _extract_voltage(str(voltage))
    if extracted:
        return extracted[1]
    try:
        return float(voltage)
    except ValueError:
        return None


def audit_capacitor_voltages(project_path: str | Path, default_voltage: str | float | None = None) -> dict[str, Any]:
    """Check every unique capacitor value in the schematic for a voltage rating
    written into its Value field (e.g. "47uF 16V" vs. plain "0.1uf") - the
    common schematic convention where every cap is assumed to use one
    project-wide default voltage rating unless its Value overrides it.
    Capacitors are identified by KiCad's own "C<n>" reference-designator
    convention (reused from list_schematic_parts' grouping, so results share
    its Value+Footprint grouping and reference lists).

    Pass `default_voltage` (e.g. "16V", "16", or 16) to also split entries that
    *do* state a voltage into ones that just redundantly restate the default
    vs. ones that genuinely differ from it - the case this convention exists
    to flag. Without it, entries are only split into has/missing a voltage
    indication.

    This can only see what's written in the Value field text - it has no way
    to know a part's *actual* voltage rating beyond that, so "missing_voltage"
    means "assumed to be the default", not "verified against the real part".
    """
    default_numeric = _coerce_voltage(default_voltage)

    parts = list_schematic_parts(project_path)["parts"]
    capacitors = [p for p in parts if p["references"] and all(_CAPACITOR_REF_RE.match(r) for r in p["references"])]

    with_voltage: list[dict[str, Any]] = []
    missing_voltage: list[dict[str, Any]] = []

    for cap in capacitors:
        row = {
            "value": cap["value"],
            "footprint": cap["footprint"],
            "lib_id": cap["lib_id"],
            "quantity": cap["quantity"],
            "references": cap["references"],
        }
        extracted = _extract_voltage(cap["value"])
        if extracted is None:
            row["status"] = "missing_voltage"
            missing_voltage.append(row)
            continue

        voltage_text, voltage_numeric = extracted
        row["stated_voltage"] = voltage_text
        row["stated_voltage_numeric"] = voltage_numeric
        if default_numeric is not None:
            row["status"] = "matches_default" if voltage_numeric == default_numeric else "differs_from_default"
        else:
            row["status"] = "has_voltage"
        with_voltage.append(row)

    return {
        "default_voltage": default_voltage,
        "default_voltage_numeric": default_numeric,
        "capacitor_part_count": len(capacitors),
        "capacitor_instance_count": sum(c["quantity"] for c in capacitors),
        "missing_voltage_count": len(missing_voltage),
        "with_voltage_count": len(with_voltage),
        "missing_voltage": missing_voltage,
        "with_voltage": with_voltage,
    }


def audit_schematic_integrity(project_path: str | Path) -> dict[str, Any]:
    """Cheap, netlist-independent sanity checks across every placed schematic
    symbol (power symbols and other "#"-prefixed auto-references excluded,
    same as list_schematic_parts): duplicate reference designators (two
    distinct placed instances annotated with the exact same reference - a
    real KiCad ERC "duplicate reference" error, not just two instances of the
    same hierarchical block which get their own distinct references), and
    symbols missing a Value or Footprint altogether. Pure text/structure
    checks - no Mouser data involved, so this always works even without an
    API key and is a fast first pass before the slower Mouser-backed audits.
    """
    directory = _resolve_schematic_dir(project_path)
    components = [
        c for c in _flatten_schematic_components(directory)
        if not c["lib_id"].startswith("power:") and not c["reference"].startswith("#")
    ]

    by_reference: dict[str, list[dict[str, Any]]] = {}
    for component in components:
        by_reference.setdefault(component["reference"].strip().upper(), []).append(component)

    duplicate_references: list[dict[str, Any]] = []
    for reference, instances in sorted(by_reference.items()):
        if len(instances) > 1:
            duplicate_references.append(
                {
                    "reference": reference,
                    "instance_count": len(instances),
                    "values": [i["value"] for i in instances],
                    "sheetfiles": [i["sheetfile"] for i in instances],
                }
            )

    missing_value: list[dict[str, Any]] = []
    missing_footprint: list[dict[str, Any]] = []
    for component in components:
        row = {
            "reference": component["reference"],
            "value": component["value"],
            "footprint": component["footprint"],
            "sheetfile": component["sheetfile"],
            "dnp": component["dnp"],
        }
        if not component["value"].strip():
            missing_value.append(row)
        if not component["footprint"].strip():
            missing_footprint.append(row)

    return {
        "component_count": len(components),
        "duplicate_reference_count": len(duplicate_references),
        "missing_value_count": len(missing_value),
        "missing_footprint_count": len(missing_footprint),
        "duplicate_references": duplicate_references,
        "missing_value": missing_value,
        "missing_footprint": missing_footprint,
    }


def _normalize_property_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", key.lower())


def _lookup_property_ci(properties: dict[str, str], target_key: str) -> str | None:
    target_norm = _normalize_property_key(target_key)
    for key, value in properties.items():
        if _normalize_property_key(key) == target_norm:
            return value
    return None


def _invalidate_schematic_cache(sch_path: Path) -> None:
    _schematic_symbol_cache.pop(str(sch_path), None)


_MANUFACTURER_PART_NUMBER_KEY = "Manufacturer_Part_Number"

# Field names seen in the wild (this project and generally) that mean "the
# manufacturer's own part number" but aren't spelled the same as the project's
# established canonical property name.
_MPN_ALIAS_KEYS = frozenset(
    _normalize_property_key(alias)
    for alias in (
        "MPN",
        "Mfr Part Number",
        "Mfr. Part Number",
        "Mfr_Part_Number",
        "Mfr Part No",
        "Mfr Part No.",
        "Manufacturer Part No",
        "Manufacturer Part No.",
        "Manufacturer Part Num",
        "ManufacturerPartNumber",
        "Part Number",
        "Part_Number",
        "PartNumber",
        "PROD_ID",
        "Product ID",
        "Vendor Part Number",
        "Distributor Part Number",
    )
)


def normalize_manufacturer_part_number_properties(
    project_path: str | Path,
    write: bool = False,
    allow_while_open: bool = False,
) -> dict[str, Any]:
    """Find schematic symbols that carry a manufacturer-part-number-shaped
    property under some other name (e.g. "PROD_ID", "MPN", "Part Number") but
    don't already have the project's canonical "Manufacturer_Part_Number"
    property, and rename that property key to the canonical name - text-only
    edit, the value itself is left untouched.

    Only renames when exactly one alias candidate is present on a symbol that
    lacks the canonical key already; a symbol with more than one candidate
    (ambiguous which one is the real MPN) is reported under `ambiguous`
    instead of guessed at.

    Defaults to a dry run (write=False) - inspect `changes`/`ambiguous`, then
    call again with write=True to actually edit the .kicad_sch files. Refuses
    to write to a sheet KiCad currently has open unless allow_while_open=True
    (see _check_not_locked_by_editor).
    """
    directory = _resolve_schematic_dir(project_path)

    changes: list[dict[str, Any]] = []
    ambiguous: list[dict[str, Any]] = []

    for sch_path in _list_schematic_files(directory):
        for symbol in _parse_schematic_symbols_cached(sch_path):
            properties = symbol["properties"]
            if _lookup_property_ci(properties, _MANUFACTURER_PART_NUMBER_KEY) is not None:
                continue
            candidates = [
                (key, value) for key, value in properties.items()
                if _normalize_property_key(key) in _MPN_ALIAS_KEYS
            ]
            if not candidates:
                continue
            references = sorted(
                {instance["reference"] for instance in symbol["instances"] if instance.get("reference")},
                key=_reference_sort_key,
            )
            if len(candidates) > 1:
                ambiguous.append(
                    {
                        "sheetfile": sch_path.name,
                        "references": references,
                        "candidate_keys": [key for key, _ in candidates],
                    }
                )
                continue
            old_key, value = candidates[0]
            changes.append(
                {
                    "sheetfile": sch_path.name,
                    "symbol_uuid": symbol["symbol_uuid"],
                    "references": references,
                    "old_key": old_key,
                    "new_key": _MANUFACTURER_PART_NUMBER_KEY,
                    "value": value,
                }
            )

    changes_by_file: dict[str, list[dict[str, Any]]] = {}
    for change in changes:
        changes_by_file.setdefault(change["sheetfile"], []).append(change)

    if write:
        for sheetfile in changes_by_file:
            _check_not_locked_by_editor(directory / sheetfile, allow_while_open)

    applied: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []

    for sheetfile, file_changes in changes_by_file.items():
        sch_path = directory / sheetfile
        text = _read_text(sch_path)
        file_modified = False
        for change in file_changes:
            uuid_marker = f'(uuid "{change["symbol_uuid"]}")'
            uuid_idx = text.find(uuid_marker)
            if uuid_idx == -1:
                missing.append({**change, "reason": "symbol uuid not found in file"})
                continue
            old_property_marker = f'(property "{change["old_key"]}" '
            property_idx = text.find(old_property_marker, uuid_idx)
            if property_idx == -1:
                missing.append({**change, "reason": "property not found after symbol uuid"})
                continue
            applied.append(change)
            if write:
                new_property_marker = f'(property "{change["new_key"]}" '
                end = property_idx + len(old_property_marker)
                text = text[:property_idx] + new_property_marker + text[end:]
                file_modified = True
        if write and file_modified:
            with sch_path.open("w", encoding="utf-8", newline="") as handle:
                handle.write(text)
            _invalidate_schematic_cache(sch_path)

    return {
        "schematic_dir": str(directory),
        "write": write,
        "change_count": len(changes),
        "changes": changes,
        "ambiguous_count": len(ambiguous),
        "ambiguous": ambiguous,
        "applied_count": len(applied),
        "missing_count": len(missing),
        "applied": applied,
        "missing": missing,
    }


def _find_matching_paren(text: str, open_idx: int) -> int:
    """`open_idx` must point at a `(`; returns the index of its matching `)`."""
    depth = 0
    for i in range(open_idx, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    raise ValueError("Unbalanced parentheses while scanning for a matching ')'")


def _escape_sexpr_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def set_schematic_property(
    project_path: str | Path,
    reference: str,
    property_name: str,
    value: str,
    write: bool = False,
    allow_while_open: bool = False,
) -> dict[str, Any]:
    """Set one property on a schematic symbol by reference designator -
    updates it in place if already present (matched case-insensitively, e.g.
    a call for "Mouser" will still find and update an existing "MOUSER"),
    otherwise inserts it as a new hidden field styled and positioned like the
    symbol's existing "Datasheet" property (KiCad's own convention for
    supplementary metadata fields such as distributor links), anchored right
    after it in the file.

    Defaults to a dry run (write=False) - inspect `change`, then call again
    with write=True to actually edit the .kicad_sch file. Refuses to write to
    a sheet KiCad currently has open unless allow_while_open=True.
    """
    directory = _resolve_schematic_dir(project_path)
    components = _flatten_schematic_components(directory)
    lowered_reference = reference.strip().upper()
    component = next((c for c in components if c["reference"].strip().upper() == lowered_reference), None)
    if component is None:
        raise KeyError(f"Schematic symbol {reference} not found")

    sch_path = directory / component["sheetfile"]
    text = _read_text(sch_path)
    uuid_marker = f'(uuid "{component["symbol_uuid"]}")'
    uuid_idx = text.find(uuid_marker)
    if uuid_idx == -1:
        raise ValueError(f"Symbol uuid not found in {component['sheetfile']} (file changed since parse?)")

    existing_key = next(
        (key for key in component["properties"] if _normalize_property_key(key) == _normalize_property_key(property_name)),
        None,
    )

    if existing_key is not None:
        marker = f'(property "{existing_key}" "'
        marker_idx = text.find(marker, uuid_idx)
        if marker_idx == -1:
            raise ValueError(f"Property {existing_key!r} not found in file text after symbol uuid")
        value_start = marker_idx + len(marker)
        value_end = value_start
        while value_end < len(text) and not (text[value_end] == '"' and text[value_end - 1] != "\\"):
            value_end += 1
        old_value = text[value_start:value_end]
        change = {
            "sheetfile": component["sheetfile"],
            "reference": reference,
            "property": existing_key,
            "action": "updated",
            "old_value": old_value,
            "new_value": value,
        }
        if write:
            _check_not_locked_by_editor(sch_path, allow_while_open)
            new_text = text[:value_start] + _escape_sexpr_string(value) + text[value_end:]
            with sch_path.open("w", encoding="utf-8", newline="") as handle:
                handle.write(new_text)
            _invalidate_schematic_cache(sch_path)
        return {"write": write, "change": change}

    anchor_marker = '(property "Datasheet" "'
    anchor_open_idx = text.find(anchor_marker, uuid_idx)
    if anchor_open_idx == -1:
        raise ValueError(
            f"No 'Datasheet' property found on {reference} to anchor a new {property_name!r} property after"
        )
    anchor_close_idx = _find_matching_paren(text, anchor_open_idx)
    anchor_block = text[anchor_open_idx : anchor_close_idx + 1]
    at_match = re.search(r"\(at [^)]*\)", anchor_block)
    at_clause = at_match.group(0) if at_match else "(at 0 0 0)"

    newline = _detect_newline(text)
    new_block = (
        f'\n\t\t(property "{property_name}" "{_escape_sexpr_string(value)}"\n'
        f"\t\t\t{at_clause}\n"
        f"\t\t\t(hide yes)\n"
        f"\t\t\t(show_name no)\n"
        f"\t\t\t(do_not_autoplace no)\n"
        f"\t\t\t(effects\n"
        f"\t\t\t\t(font\n"
        f"\t\t\t\t\t(size 1.27 1.27)\n"
        f"\t\t\t\t)\n"
        f"\t\t\t)\n"
        f"\t\t)"
    ).replace("\n", newline)

    change = {
        "sheetfile": component["sheetfile"],
        "reference": reference,
        "property": property_name,
        "action": "inserted",
        "old_value": None,
        "new_value": value,
    }
    if write:
        _check_not_locked_by_editor(sch_path, allow_while_open)
        new_text = text[: anchor_close_idx + 1] + new_block + text[anchor_close_idx + 1 :]
        with sch_path.open("w", encoding="utf-8", newline="") as handle:
            handle.write(new_text)
        _invalidate_schematic_cache(sch_path)
    return {"write": write, "change": change}


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect KiCad board files")
    parser.add_argument("project_path", help="Path to a KiCad project directory, .kicad_pcb or .kicad_pro file")
    parser.add_argument("--json", action="store_true", help="Emit JSON output")
    args = parser.parse_args()

    result = inspect_project(args.project_path)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"PCB: {result['board_path']}")
        print(f"Components: {result['component_count']}")
        print(f"Nets: {result['net_count']}")


if __name__ == "__main__":
    main()
