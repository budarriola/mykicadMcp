
from __future__ import annotations

import argparse
import copy
import json
import math
import re
import uuid as _uuid
from datetime import date as _date
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
_track_cache: dict[str, tuple[float, int, dict[str, list[dict[str, Any]]]]] = {}
_board_layers_cache: dict[str, tuple[float, int, list[dict[str, Any]]]] = {}


def _invalidate_board_cache(board_path: Path) -> None:
    _board_component_cache.pop(str(board_path), None)
    _pad_cache.pop(str(board_path), None)
    _track_cache.pop(str(board_path), None)
    _board_layers_cache.pop(str(board_path), None)


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
                        # (pad "<num>" <type> <shape> ...): entry[2] is the pad type
                        # (smd | thru_hole | np_thru_hole | connect) - needed by the
                        # router to know a through-hole pad spans every copper layer.
                        pad_type = entry[2] if len(entry) > 2 and isinstance(entry[2], str) else ""
                        pad_local = {"x": 0.0, "y": 0.0, "rotation": 0.0}
                        pad_net = ""
                        pad_layers: list[str] = []
                        pad_pintype = ""
                        pad_size = {"x": 0.0, "y": 0.0}
                        for pentry in entry[1:]:
                            if not (isinstance(pentry, list) and pentry):
                                continue
                            ptag = pentry[0]
                            if ptag == "at":
                                pvalues = [float(t) for t in pentry[1:] if isinstance(t, str) and _is_number(t)]
                                if len(pvalues) >= 2:
                                    pad_local = {"x": pvalues[0], "y": pvalues[1], "rotation": pvalues[2] if len(pvalues) >= 3 else 0.0}
                            elif ptag == "size":
                                svalues = [float(t) for t in pentry[1:] if isinstance(t, str) and _is_number(t)]
                                if len(svalues) >= 2:
                                    pad_size = {"x": svalues[0], "y": svalues[1]}
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
                                "type": pad_type,
                                "pintype": pad_pintype,
                                "layers": pad_layers,
                                "size": pad_size,
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


def _parse_tracks(board_path: Path) -> dict[str, list[dict[str, Any]]]:
    """Parse copper geometry - `(segment ...)`, `(via ...)`, `(arc ...)` - directly
    from the s-expr tree. Width parsing MUST be scoped to these node types only:
    `(width ...)` also appears on silkscreen `gr_line`/footprint graphics, which a
    flat regex would wrongly count as copper. Every segment/arc is additionally
    asserted to sit on a `*.Cu` layer, and every via is asserted to span at least
    one `*.Cu` layer, before being accepted.

    Arc length is the straight-line start->end chord, not the true arc length -
    documented approximation (flagged `is_arc: True`); the board currently has no
    copper arcs, only edge-cuts/graphic arcs, which are excluded by the layer check.

    Vias with `net == ""` are free/unconnected (stitching, mounting) vias - callers
    that build per-net stats must skip them explicitly; this parser returns them
    as-is so board-wide inventories can still count/flag them.
    """
    board_text = _read_text(board_path)
    root = SexprParser(board_text).parse()
    segments: list[dict[str, Any]] = []
    vias: list[dict[str, Any]] = []
    arcs: list[dict[str, Any]] = []

    def _point(entry: list[Any]) -> dict[str, float] | None:
        values = [float(t) for t in entry[1:] if isinstance(t, str) and _is_number(t)]
        if len(values) >= 2:
            return {"x": values[0], "y": values[1]}
        return None

    def walk(node: Any) -> None:
        if isinstance(node, list):
            tag0 = node[0] if node else None
            if tag0 in ("segment", "arc"):
                start = end = None
                width = 0.0
                layer = ""
                net = ""
                node_uuid = ""
                for entry in node[1:]:
                    if not (isinstance(entry, list) and entry):
                        continue
                    tag = entry[0]
                    if tag == "start":
                        start = _point(entry)
                    elif tag == "end":
                        end = _point(entry)
                    elif tag == "width" and len(entry) >= 2 and isinstance(entry[1], str) and _is_number(entry[1]):
                        width = float(entry[1])
                    elif tag == "layer" and len(entry) >= 2 and isinstance(entry[1], str):
                        layer = entry[1]
                    elif tag == "net" and len(entry) >= 2 and isinstance(entry[1], str):
                        net = entry[1]
                    elif tag == "uuid" and len(entry) >= 2 and isinstance(entry[1], str):
                        node_uuid = entry[1]
                if layer.endswith(".Cu") and start is not None and end is not None:
                    length = math.hypot(end["x"] - start["x"], end["y"] - start["y"])
                    record = {
                        "net": net,
                        "width": width,
                        "layer": layer,
                        "start": start,
                        "end": end,
                        "length": round(length, 6),
                        "uuid": node_uuid,
                    }
                    if tag0 == "arc":
                        record["is_arc"] = True
                        arcs.append(record)
                    else:
                        segments.append(record)
            elif tag0 == "via":
                at = None
                size = 0.0
                drill = 0.0
                via_layers: list[str] = []
                net = ""
                node_uuid = ""
                for entry in node[1:]:
                    if not (isinstance(entry, list) and entry):
                        continue
                    tag = entry[0]
                    if tag == "at":
                        at = _point(entry)
                    elif tag == "size" and len(entry) >= 2 and isinstance(entry[1], str) and _is_number(entry[1]):
                        size = float(entry[1])
                    elif tag == "drill" and len(entry) >= 2 and isinstance(entry[1], str) and _is_number(entry[1]):
                        drill = float(entry[1])
                    elif tag == "layers":
                        via_layers = [str(x) for x in entry[1:] if isinstance(x, str)]
                    elif tag == "net" and len(entry) >= 2 and isinstance(entry[1], str):
                        net = entry[1]
                    elif tag == "uuid" and len(entry) >= 2 and isinstance(entry[1], str):
                        node_uuid = entry[1]
                if at is not None and any(layer_name.endswith(".Cu") for layer_name in via_layers):
                    vias.append(
                        {
                            "net": net,
                            "size": size,
                            "drill": drill,
                            "layers": via_layers,
                            "at": at,
                            "uuid": node_uuid,
                        }
                    )
            for child in node:
                walk(child)

    walk(root)
    return {"segments": segments, "vias": vias, "arcs": arcs}


def _parse_tracks_cached(board_path: Path) -> dict[str, list[dict[str, Any]]]:
    stat = board_path.stat()
    key = str(board_path)
    cached = _track_cache.get(key)
    if cached is not None and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
        return cached[2]
    tracks = _parse_tracks(board_path)
    _track_cache[key] = (stat.st_mtime, stat.st_size, tracks)
    return tracks


def _format_mm(value: float) -> str:
    """Render a mm value the way the plan's example keys look ("0.2", "12"), not
    Python's default float repr - trims trailing zeros/decimal point."""
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return text if text else "0"


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


def get_net_track_widths(project_path: str | Path, net: str | None = None) -> dict[str, Any]:
    """Per-net aggregate of routed copper (segments + arcs) and vias, from the
    board file's own geometry - not the netclass/schematic intent. `net=None`
    returns every routed net (sorted by name); `net="X"` returns just that net.

    Empty-net ("") copper is excluded - those are free/unconnected vias, not a
    real net. A segment/arc `width` of 0 means "inherit from netclass" in KiCad,
    not a literal 0 mm trace - such copper is counted in `segment_count`/
    `total_length_mm` but bucketed under the `"inherit"` key in `widths` (and
    excluded from `dominant_width`/`min_width`/`max_width`, which describe only
    explicit widths) with `zero_width_segment_count` reporting how much of that
    net has no explicit width.
    """
    board_path, _, _ = _resolve_project_path(project_path)
    tracks = _parse_tracks_cached(board_path)
    copper = tracks["segments"] + tracks["arcs"]

    per_net: dict[str, dict[str, Any]] = {}

    def bucket_for(net_name: str) -> dict[str, Any]:
        return per_net.setdefault(
            net_name,
            {
                "net": net_name,
                "segment_count": 0,
                "total_length_mm": 0.0,
                "widths": {},
                "layers": set(),
                "via_sizes": {},
                "_width_length": {},
                "_zero_width_count": 0,
            },
        )

    for seg in copper:
        net_name = seg["net"]
        if not net_name:
            continue
        entry = bucket_for(net_name)
        entry["segment_count"] += 1
        entry["total_length_mm"] += seg["length"]
        entry["layers"].add(seg["layer"])
        width = seg["width"]
        if width <= 0:
            entry["_zero_width_count"] += 1
            key = "inherit"
        else:
            key = _format_mm(width)
            entry["_width_length"][width] = entry["_width_length"].get(width, 0.0) + seg["length"]
        entry["widths"][key] = entry["widths"].get(key, 0) + 1

    for via in tracks["vias"]:
        net_name = via["net"]
        if not net_name:
            continue
        entry = bucket_for(net_name)
        key = f"{_format_mm(via['size'])}/{_format_mm(via['drill'])}"
        entry["via_sizes"][key] = entry["via_sizes"].get(key, 0) + 1

    results: dict[str, dict[str, Any]] = {}
    for net_name, entry in per_net.items():
        width_length = entry.pop("_width_length")
        zero_count = entry.pop("_zero_width_count")
        if width_length:
            dominant_width = max(width_length.items(), key=lambda kv: kv[1])[0]
            min_width = min(width_length)
            max_width = max(width_length)
        else:
            dominant_width = None
            min_width = None
            max_width = None
        entry["dominant_width"] = dominant_width
        entry["min_width"] = min_width
        entry["max_width"] = max_width
        entry["layers"] = sorted(entry["layers"])
        entry["total_length_mm"] = round(entry["total_length_mm"], 3)
        entry["is_uniform"] = len(entry["widths"]) <= 1
        if zero_count:
            entry["zero_width_segment_count"] = zero_count
        results[net_name] = entry

    if net is not None:
        if net not in results:
            raise KeyError(f"Net {net!r} has no routed copper on the board")
        return results[net]

    return {"net_count": len(results), "nets": [results[name] for name in sorted(results)]}


_BUS_SIGNATURES: dict[str, dict[str, Any]] = {
    # Each bus type: "required" roles must ALL have >=1 matching net in a group
    # for the bus to fire; "optional" roles are attached if present (e.g. CS
    # lines) but don't gate detection. Alias sets are checked against both the
    # net's full normalized basename and its basename with a trailing index
    # stripped (so CS0/CS1/... and CS all match the "CS" role).
    "I2C": {"required": {"SDA": {"SDA"}, "SCL": {"SCL"}}, "optional": {}},
    "SPI": {
        "required": {
            "MOSI": {"MOSI", "SDO", "COPI"},
            "MISO": {"MISO", "SDI", "CIPO"},
            "CLK": {"SCK", "SCLK", "CLK"},
        },
        "optional": {"CS": {"CS", "SS", "NSS", "NCS", "CSN", "SSN"}},
    },
    "QSPI": {
        "required": {
            "CLK": {"SCK", "SCLK"},
            # IO/DQ roles carry their index, so match on the FULL basename here.
            "IO": {"IO0", "IO1", "IO2", "IO3", "DQ0", "DQ1", "DQ2", "DQ3"},
            "CS": {"CS", "SS", "NSS", "NCS"},
        },
        "optional": {},
    },
    "I2S": {
        "required": {
            "WS": {"WS", "LRCLK", "FS"},
            "BCLK": {"BCLK", "SCK", "BCK"},
            "SD": {"SD", "SDIN", "SDOUT", "DIN", "DOUT"},
        },
        "optional": {"MCLK": {"MCLK"}},
    },
    "UART": {
        "required": {"TX": {"TX", "TXD"}, "RX": {"RX", "RXD"}},
        "optional": {"RTS": {"RTS"}, "CTS": {"CTS"}, "DTR": {"DTR"}},
    },
    "CAN": {
        "required": {"CANH": {"CANH", "CAN_H"}, "CANL": {"CANL", "CAN_L"}},
        "optional": {},
    },
    "USB": {
        "required": {"DP": {"DP", "D+", "DPLUS"}, "DM": {"DM", "D-", "DMINUS"}},
        "optional": {"VBUS": {"VBUS"}, "ID": {"ID"}},
    },
    "SWD": {
        "required": {"SWDIO": {"SWDIO"}, "SWCLK": {"SWCLK"}},
        "optional": {"NRST": {"NRST", "RESET", "RST"}},
    },
    "JTAG": {
        "required": {"TCK": {"TCK"}, "TMS": {"TMS"}, "TDI": {"TDI"}, "TDO": {"TDO"}},
        "optional": {"NTRST": {"NTRST"}},
    },
    # RS485/RS422: single-letter A/B (optional Z/Y) roles are wildly
    # false-positive-prone on any board (any net literally named "A"/"B"
    # matches), so this signature is NOT reported like the others - see
    # `suppress_unqualified` below and its use in `detect_buses`. `basename_only`
    # means the role match is against the net's exact basename only, never the
    # index-stripped form, so it doesn't collide with parallel-bus roles like
    # address line "A0".."A15" (whose base_no_index is also "A").
    "RS485": {
        "required": {"A": {"A", "RS485_A", "RS485A", "RS485+"}, "B": {"B", "RS485_B", "RS485B", "RS485-"}},
        "optional": {"Z": {"Z", "RS485_Z"}, "Y": {"Y", "RS485_Y"}},
        "basename_only": True,
        "suppress_unqualified": True,
    },
}

# Structural (non-role) detectors, applied per hierarchical group in addition to
# the signature table above. Parallel-bus index detection reuses the
# `base_no_index`/`index` already computed per net by `_split_trailing_index`
# (below) rather than a second regex - `_NET_INDEX_RE` there is the same shape
# a dedicated "parallel index" regex would be.
_DIFF_PAIR_RE = re.compile(r"^(?P<base>.+?)[_]?[PN]$")

_NET_INDEX_RE = re.compile(r"^(.*?)[_\-]?(\d+)$")


def _normalize_net_basename(net_name: str) -> str:
    """Last `/`-segment of a hierarchical net name, uppercased. Net-name casing
    is inconsistent across the board (`/MainControler/SDA`, `GND_Main`) so role
    matching normalizes to uppercase, but callers must keep using the original
    `net_name` for anything that writes or patterns against the netlist."""
    base = net_name.rsplit("/", 1)[-1] if net_name else net_name
    return base.strip().upper()


def _net_group_prefix(net_name: str) -> str:
    """Hierarchical path before the basename, e.g. '/MainControler/MOSI' ->
    '/MainControler/'. Nets with no '/' get the flat-design sentinel ''."""
    if "/" not in net_name:
        return ""
    return net_name.rsplit("/", 1)[0] + "/"


def _split_trailing_index(basename: str) -> tuple[str, int | None]:
    match = _NET_INDEX_RE.match(basename)
    if match and match.group(1):
        return match.group(1), int(match.group(2))
    return basename, None


def _role_matches(basename: str, base_no_index: str, alias_set: set[str]) -> bool:
    return basename in alias_set or base_no_index in alias_set


def _ic_like_ref(ref: str, ic_prefixes: tuple[str, ...]) -> bool:
    ref = ref.strip().upper()
    return any(ref.startswith(prefix) for prefix in ic_prefixes)


def _bus_qualification(
    member_nets: list[dict[str, Any]],
    net_map: dict[str, list[dict[str, str]]],
    ic_prefixes: tuple[str, ...],
) -> dict[str, Any]:
    """3c: intersect component refs across every member net, keeping only
    IC-like refs. A common IC on all (or all-but-one, for a fanned-out net
    like a shared CS bank) member nets makes the candidate `qualified`."""
    ref_sets: list[set[str]] = []
    for member in member_nets:
        nodes = net_map.get(member["net"], [])
        refs = {n.get("ref", "").strip().upper() for n in nodes if _ic_like_ref(n.get("ref", ""), ic_prefixes)}
        if refs:
            ref_sets.append(refs)

    if not ref_sets:
        return {"common_ics": [], "qualified": False, "reason": "no IC-like refs on any member net"}

    intersection = set.intersection(*ref_sets) if ref_sets else set()
    if intersection:
        return {"common_ics": sorted(intersection), "qualified": True, "reason": None}

    # Weak: no IC touches every member net - check "all but one" (fan-out where
    # one net, e.g. a lone CS, skips the common IC because it's on a connector).
    counts: dict[str, int] = {}
    for refs in ref_sets:
        for ref in refs:
            counts[ref] = counts.get(ref, 0) + 1
    if counts:
        best_ref, best_count = max(counts.items(), key=lambda kv: kv[1])
        if best_count >= max(1, len(ref_sets) - 1) and len(ref_sets) > 1:
            return {
                "common_ics": [best_ref],
                "qualified": False,
                "reason": f"{best_ref} touches {best_count}/{len(ref_sets)} member nets, not all - reported as weak",
            }
    return {
        "common_ics": [],
        "qualified": False,
        "reason": "required roles present but no IC is common across member nets (bus fans out to a connector or similar)",
    }


def _find_diff_pairs(
    group_nets: list[dict[str, Any]],
    claimed_nets: set[str],
) -> list[dict[str, Any]]:
    """Structural (non-signature) diff-pair detector for one hierarchical group:
    a base qualifies once BOTH polarities of `<base>_P`/`<base>_N`, `<base>+`/
    `<base>-`, or `<base>P`/`<base>N` are present among this group's nets. Nets
    already claimed by a named `_BUS_SIGNATURES` match in the same group are
    skipped (`claimed_nets`) so e.g. USB D+/D- stays USB, not also DIFF_PAIR.
    Requiring both polarities to be present is what keeps this from firing on
    every net that happens to end in P or N.
    """
    pairs: dict[str, dict[str, dict[str, Any]]] = {}
    for info in group_nets:
        if info["net"] in claimed_nets:
            continue
        basename = info["basename"]
        if basename.endswith("+") and len(basename) > 1:
            pairs.setdefault(basename[:-1], {})["P"] = info
            continue
        if basename.endswith("-") and len(basename) > 1:
            pairs.setdefault(basename[:-1], {})["N"] = info
            continue
        match = _DIFF_PAIR_RE.match(basename)
        if match and match.group("base"):
            pairs.setdefault(match.group("base"), {})[basename[-1]] = info

    results: list[dict[str, Any]] = []
    for base, roles in pairs.items():
        if base and "P" in roles and "N" in roles and roles["P"]["net"] != roles["N"]["net"]:
            results.append({"base": base, "P": roles["P"], "N": roles["N"]})
    return results


def _find_parallel_buses(
    group_nets: list[dict[str, Any]],
    claimed_nets: set[str],
) -> list[dict[str, Any]]:
    """Structural (non-signature) parallel-bus detector for one hierarchical
    group: a base qualifies when its group carries >=4 nets sharing that base
    with contiguous numeric suffixes 0..n (D0..D7, A0..A15 - `base_no_index`/
    `index` already computed per net by `_split_trailing_index`). A gap in the
    sequence disqualifies the whole base rather than reporting a partial run.
    Nets already claimed (named bus signature or a diff pair) in the same group
    are skipped so e.g. QSPI IO0..IO3 stays QSPI, not also PARALLEL.
    """
    by_base: dict[str, dict[int, dict[str, Any]]] = {}
    for info in group_nets:
        if info["net"] in claimed_nets or info["index"] is None:
            continue
        by_base.setdefault(info["base_no_index"], {})[info["index"]] = info

    results: list[dict[str, Any]] = []
    for base, indexed in by_base.items():
        if not base or 0 not in indexed or len(indexed) < 4:
            continue
        indices = sorted(indexed.keys())
        if indices != list(range(len(indices))):
            continue  # gap in the sequence - not a contiguous 0..n run
        results.append({"base": base, "indices": indices, "members": [indexed[i] for i in indices]})
    return results


def detect_buses(
    project_path: str | Path,
    ic_ref_prefixes: list[str] | None = None,
) -> dict[str, Any]:
    """Read-only bus detection over the schematic netlist (Phase 3). Groups nets
    by shared hierarchical prefix (falling back to shared-connected-IC grouping
    for prefix-less/flat nets), matches each group against `_BUS_SIGNATURES`,
    and for every match that has all required roles emits a candidate with
    Phase-1 `get_net_track_widths` width data and Phase-3c IC qualification.

    NEVER writes or applies anything - candidates only, for a caller to confirm
    with the user before Phase 4 creates any net class.

    Netlist staleness: the `.net` file is a schematic export and can lag the
    board. Net names are cross-checked against the board's own pad nets (the
    ground truth); mismatches are reported in `stale_netlist_warnings` rather
    than refusing the run.

    Confirmed-bus reuse (Phase 7.1): candidates matching a `confirmed_buses`
    entry in `<board>.board_local.json` (same bus_type + identical net set,
    written earlier via `record_confirmed_bus`) are marked `confirmed: true`
    with their `confirmed_on` date (and `confirmed_name` when recorded), so a
    caller only needs to present unconfirmed candidates to the user. Reading
    that state file keeps this function read-only.
    """
    board_path, _, netlist_path = _resolve_project_path(project_path)
    ic_prefixes = tuple(p.strip().upper() for p in (ic_ref_prefixes or ["U", "IC", "Q"]))

    nets = _parse_nets_cached(netlist_path)
    _, net_map = _build_net_maps(nets)

    # Netlist staleness guard: compare netlist net names against the board's
    # own pad nets (ground truth, independent of the schematic export).
    footprints = _parse_footprint_pads_cached(board_path)
    board_net_names: set[str] = set()
    for fp in footprints.values():
        for pad in fp.get("pads", []):
            pad_net = pad.get("net", "")
            if pad_net:
                board_net_names.add(pad_net)
    netlist_net_names = {n.get("name", "") for n in nets if n.get("name")}
    stale_netlist_warnings: list[str] = []
    only_in_netlist = sorted(netlist_net_names - board_net_names)
    only_on_board = sorted(board_net_names - netlist_net_names)
    if only_in_netlist:
        stale_netlist_warnings.append(
            f"{len(only_in_netlist)} net(s) in the .net export have no matching pad net on the board "
            f"(netlist may be stale - re-export from the schematic): {only_in_netlist[:20]}"
        )
    if only_on_board:
        stale_netlist_warnings.append(
            f"{len(only_on_board)} net(s) on the board's pads are absent from the .net export: {only_on_board[:20]}"
        )

    # Precompute per-net normalized basename/index/prefix.
    net_info: list[dict[str, Any]] = []
    for net in nets:
        name = net.get("name", "")
        if not name:
            continue
        basename = _normalize_net_basename(name)
        base_no_index, index = _split_trailing_index(basename)
        net_info.append(
            {
                "net": name,
                "basename": basename,
                "base_no_index": base_no_index,
                "index": index,
                "prefix": _net_group_prefix(name),
            }
        )

    # Group by hierarchical prefix; nets with no '/' (prefix == "") fall back to
    # shared-connected-IC grouping below.
    groups: dict[str, list[dict[str, Any]]] = {}
    flat_nets: list[dict[str, Any]] = []
    for info in net_info:
        if info["prefix"]:
            groups.setdefault(info["prefix"], []).append(info)
        else:
            flat_nets.append(info)

    if flat_nets:
        # Fallback: bucket flat nets by the IC ref(s) they share.
        nets_by_ref: dict[str, list[dict[str, Any]]] = {}
        for info in flat_nets:
            for node in net_map.get(info["net"], []):
                ref = node.get("ref", "")
                if _ic_like_ref(ref, ic_prefixes):
                    nets_by_ref.setdefault(ref.strip().upper(), []).append(info)
        for ref, members in nets_by_ref.items():
            if len(members) >= 2:
                groups.setdefault(f"IC:{ref}/", []).extend(members)

    width_data = get_net_track_widths(project_path)
    width_by_net = {entry["net"]: entry for entry in width_data.get("nets", [])}

    candidates: list[dict[str, Any]] = []
    for prefix, group_nets in groups.items():
        sheet_name = prefix.strip("/").split("/")[-1] if prefix and not prefix.startswith("IC:") else prefix.replace("IC:", "").strip("/")
        for bus_type, signature in _BUS_SIGNATURES.items():
            # `basename_only` (RS485) disables the index-stripped fallback so a
            # single-letter role can't accidentally absorb an indexed parallel-
            # bus net (e.g. address line "A0" whose base_no_index is also "A").
            basename_only = bool(signature.get("basename_only"))
            matched_roles: dict[str, list[tuple[dict[str, Any], str]]] = {}
            for role_name, alias_set in signature["required"].items():
                role_hits = []
                for info in group_nets:
                    base_for_match = info["basename"] if basename_only else info["base_no_index"]
                    if _role_matches(info["basename"], base_for_match, alias_set):
                        tag = role_name if info["index"] is None or role_name == "IO" else f"{role_name}{info['index']}"
                        role_hits.append((info, tag))
                if role_hits:
                    matched_roles[role_name] = role_hits
            if len(matched_roles) < len(signature["required"]):
                continue  # not all required roles present in this group

            member_entries: list[dict[str, Any]] = []
            seen_nets: set[str] = set()
            for role_name in signature["required"]:
                for info, tag in matched_roles[role_name]:
                    if info["net"] in seen_nets:
                        continue
                    seen_nets.add(info["net"])
                    member_entries.append({"net": info["net"], "role": tag, "width_summary": width_by_net.get(info["net"])})

            for role_name, alias_set in signature.get("optional", {}).items():
                for info in group_nets:
                    if info["net"] in seen_nets:
                        continue
                    base_for_match = info["basename"] if basename_only else info["base_no_index"]
                    if _role_matches(info["basename"], base_for_match, alias_set):
                        tag = role_name if info["index"] is None else f"{role_name}{info['index']}"
                        seen_nets.add(info["net"])
                        member_entries.append({"net": info["net"], "role": tag, "width_summary": width_by_net.get(info["net"])})

            qualification = _bus_qualification(member_entries, net_map, ic_prefixes)

            if signature.get("suppress_unqualified") and not qualification["qualified"]:
                # RS485/RS422: A/B (and Z/Y) are too generic to report without a
                # confirmed common transceiver IC - suppress rather than emit a
                # "low confidence" candidate that is really just noise.
                continue

            member_widths: dict[str, int] = {}
            for member in member_entries:
                summary = member.get("width_summary")
                if summary and summary.get("dominant_width"):
                    member_widths[summary["dominant_width"]] = member_widths.get(summary["dominant_width"], 0) + 1

            required_role_count = len(signature["required"])
            optional_hit_count = len(member_entries) - sum(len(v) for v in matched_roles.values())
            confidence = "high" if qualification["qualified"] else ("medium" if optional_hit_count > 0 else "low")

            candidates.append(
                {
                    "bus_type": bus_type,
                    "confidence": confidence,
                    "group_prefix": prefix,
                    "nets": member_entries,
                    "common_ics": qualification["common_ics"],
                    "qualified": qualification["qualified"],
                    "qualification_reason": qualification["reason"],
                    "member_widths": member_widths,
                    "suggested_class_name": f"{bus_type}_{sheet_name}" if sheet_name else bus_type,
                    "required_roles_matched": sorted(matched_roles.keys()),
                    "required_roles_needed": required_role_count,
                }
            )

        # Structural detectors (no role table): diff pairs and parallel buses.
        # Both skip nets already claimed by a named-signature candidate above in
        # this same group (e.g. USB D+/D- stays USB, QSPI IO0..IO3 stays QSPI).
        group_claimed_nets: set[str] = {
            member["net"] for cand in candidates if cand["group_prefix"] == prefix for member in cand["nets"]
        }

        for diff_pair in _find_diff_pairs(group_nets, group_claimed_nets):
            member_entries = [
                {"net": diff_pair["P"]["net"], "role": "P", "width_summary": width_by_net.get(diff_pair["P"]["net"])},
                {"net": diff_pair["N"]["net"], "role": "N", "width_summary": width_by_net.get(diff_pair["N"]["net"])},
            ]
            qualification = _bus_qualification(member_entries, net_map, ic_prefixes)
            member_widths = {}
            for member in member_entries:
                summary = member.get("width_summary")
                if summary and summary.get("dominant_width"):
                    member_widths[summary["dominant_width"]] = member_widths.get(summary["dominant_width"], 0) + 1
            base = diff_pair["base"]
            candidates.append(
                {
                    "bus_type": "DIFF_PAIR",
                    "confidence": "high" if qualification["qualified"] else "low",
                    "group_prefix": prefix,
                    "nets": member_entries,
                    "common_ics": qualification["common_ics"],
                    "qualified": qualification["qualified"],
                    "qualification_reason": qualification["reason"],
                    "member_widths": member_widths,
                    "suggested_class_name": f"DIFF_PAIR_{base}_{sheet_name}" if sheet_name else f"DIFF_PAIR_{base}",
                    "required_roles_matched": ["N", "P"],
                    "required_roles_needed": 2,
                }
            )
            group_claimed_nets.update(m["net"] for m in member_entries)

        for parallel_bus in _find_parallel_buses(group_nets, group_claimed_nets):
            member_entries = [
                {"net": m["net"], "role": str(m["index"]), "width_summary": width_by_net.get(m["net"])}
                for m in parallel_bus["members"]
            ]
            qualification = _bus_qualification(member_entries, net_map, ic_prefixes)
            member_widths = {}
            for member in member_entries:
                summary = member.get("width_summary")
                if summary and summary.get("dominant_width"):
                    member_widths[summary["dominant_width"]] = member_widths.get(summary["dominant_width"], 0) + 1
            base = parallel_bus["base"]
            candidates.append(
                {
                    "bus_type": "PARALLEL",
                    "confidence": "high" if qualification["qualified"] else "low",
                    "group_prefix": prefix,
                    "nets": member_entries,
                    "common_ics": qualification["common_ics"],
                    "qualified": qualification["qualified"],
                    "qualification_reason": qualification["reason"],
                    "member_widths": member_widths,
                    "suggested_class_name": f"PARALLEL_{base}_{sheet_name}" if sheet_name else f"PARALLEL_{base}",
                    "required_roles_matched": [str(i) for i in parallel_bus["indices"]],
                    "required_roles_needed": len(parallel_bus["indices"]),
                }
            )
            group_claimed_nets.update(m["net"] for m in member_entries)

    # Phase 7.1: reuse cached user confirmations from the board-local JSON so
    # re-runs mark already-confirmed buses and only NEW candidates need the
    # user's attention. Matching is exact (bus_type + identical net set) - a
    # changed membership is a different bus and must be re-confirmed. Any
    # problem reading the state file degrades to "nothing confirmed".
    confirmed_lookup: dict[tuple[str, frozenset[str]], dict[str, Any]] = {}
    try:
        for entry in load_board_local(project_path)["data"].get("confirmed_buses", []):
            if isinstance(entry, dict) and entry.get("nets"):
                key = (str(entry.get("bus_type", "")), frozenset(str(n) for n in entry["nets"]))
                confirmed_lookup[key] = entry
    except Exception:  # pragma: no cover - defensive; unreadable state file
        confirmed_lookup = {}
    for cand in candidates:
        entry = confirmed_lookup.get((cand["bus_type"], frozenset(m["net"] for m in cand["nets"])))
        cand["confirmed"] = entry is not None
        if entry is not None:
            cand["confirmed_on"] = entry.get("confirmed_on")
            if entry.get("name"):
                cand["confirmed_name"] = entry["name"]

    return {
        "project_path": str(project_path),
        "candidate_count": len(candidates),
        "candidates": candidates,
        "confirmed_count": sum(1 for c in candidates if c["confirmed"]),
        "stale_netlist_warnings": stale_netlist_warnings,
        "ic_ref_prefixes_used": list(ic_prefixes),
    }


def get_project_track_inventory(project_path: str | Path) -> dict[str, Any]:
    """Board-wide, copper-only inventory of every track width and via size in use,
    plus the netclasses already defined in `<project>.kicad_pro` - the exact
    "menu of previously used values" a caller should present to a user instead of
    free-entry width/via numbers. `free_via_count` is vias with `net == ""`
    (unconnected stitching/mounting vias); a via-size bucket is flagged
    `"free/oversized"` when every via at that size/drill is a free via.
    """
    board_path, project_file, _ = _resolve_project_path(project_path)
    tracks = _parse_tracks_cached(board_path)
    copper = tracks["segments"] + tracks["arcs"]

    width_stats: dict[float, dict[str, Any]] = {}
    zero_width_count = 0
    for seg in copper:
        width = seg["width"]
        if width <= 0:
            zero_width_count += 1
            continue
        bucket = width_stats.setdefault(
            width, {"width": width, "segment_count": 0, "length_mm": 0.0, "_nets": set()}
        )
        bucket["segment_count"] += 1
        bucket["length_mm"] += seg["length"]
        if seg["net"]:
            bucket["_nets"].add(seg["net"])

    track_widths = []
    for bucket in width_stats.values():
        track_widths.append(
            {
                "width": bucket["width"],
                "segment_count": bucket["segment_count"],
                "length_mm": round(bucket["length_mm"], 3),
                "nets": len(bucket["_nets"]),
            }
        )
    track_widths.sort(key=lambda entry: entry["segment_count"], reverse=True)

    via_groups: dict[tuple[float, float], dict[str, Any]] = {}
    free_via_count = 0
    for via in tracks["vias"]:
        is_free = via["net"] == ""
        if is_free:
            free_via_count += 1
        key = (via["size"], via["drill"])
        group = via_groups.setdefault(key, {"size": via["size"], "drill": via["drill"], "count": 0, "_free": 0})
        group["count"] += 1
        if is_free:
            group["_free"] += 1

    via_sizes = []
    for group in via_groups.values():
        entry = {"size": group["size"], "drill": group["drill"], "count": group["count"]}
        via_sizes.append(entry)
    via_sizes.sort(key=lambda entry: entry["count"], reverse=True)

    existing_netclasses: list[dict[str, Any]] = []
    if project_file.exists():
        try:
            pro_data = json.loads(project_file.read_text(encoding="utf-8"))
            existing_netclasses = pro_data.get("net_settings", {}).get("classes", [])
        except (json.JSONDecodeError, OSError):
            existing_netclasses = []

    # Flag a via bucket "free/oversized" if it contains any free (net=="") via -
    # a size otherwise unused by real routing is a stitching/mounting artifact
    # worth calling out even when a few connected vias happen to share that size
    # - or if it's well above the Default netclass via diameter (a literal
    # oversized via), whichever signal fires first.
    default_via_diameter = next(
        (float(c["via_diameter"]) for c in existing_netclasses if c.get("name") == "Default" and _is_number(str(c.get("via_diameter", "")))),
        None,
    )
    for entry in via_sizes:
        group = via_groups[(entry["size"], entry["drill"])]
        is_free_bucket = group["_free"] > 0
        is_oversized = default_via_diameter is not None and entry["size"] > default_via_diameter * 3
        if is_free_bucket or is_oversized:
            entry["warning"] = "free/oversized"

    result: dict[str, Any] = {
        "track_widths": track_widths,
        "via_sizes": via_sizes,
        "existing_netclasses": existing_netclasses,
        "free_via_count": free_via_count,
    }
    if zero_width_count:
        result["zero_width_segment_count"] = zero_width_count
        result["note"] = (
            "Segments/arcs with width 0 inherit their width from the assigned "
            "netclass and are excluded from track_widths."
        )
    return result


# --- Phase 5: inter-trace ("area between the traces") corridor measurement ---


def _convex_hull_area(points: list[tuple[float, float]]) -> float:
    """Area of the convex hull of `points` (Andrew's monotone chain + shoelace).
    Pure stdlib. Returns 0.0 for fewer than 3 distinct points (no area)."""
    pts = sorted(set((round(px, 6), round(py, 6)) for px, py in points))
    if len(pts) < 3:
        return 0.0

    def cross(o: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> float:
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: list[tuple[float, float]] = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper: list[tuple[float, float]] = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    hull = lower[:-1] + upper[:-1]
    if len(hull) < 3:
        return 0.0
    area = 0.0
    n = len(hull)
    for i in range(n):
        x1, y1 = hull[i]
        x2, y2 = hull[(i + 1) % n]
        area += x1 * y2 - x2 * y1
    return abs(area) / 2.0


def _perp_distance_to_axis(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
    """Perpendicular distance of point (px,py) from the infinite line through
    (ax,ay)->(bx,by). Degenerate (zero-length) axis falls back to point distance."""
    dx = bx - ax
    dy = by - ay
    length = math.hypot(dx, dy)
    if length < 1e-9:
        return math.hypot(px - ax, py - ay)
    return abs((px - ax) * dy - (py - ay) * dx) / length


def _ic_set_for_net(net_name: str, net_map: dict[str, list[dict[str, str]]], ic_prefixes: tuple[str, ...]) -> set[str]:
    return {n.get("ref", "").strip().upper() for n in net_map.get(net_name, []) if _ic_like_ref(n.get("ref", ""), ic_prefixes)}


def _resolve_bus_spec(bus: Any, nets: Any, hub_ic: Any, bus_type: Any) -> dict[str, Any]:
    """Normalize either a detect_buses candidate object (`bus`) or explicit
    `nets`/`hub_ic` into {nets: [str], hub_hint, common_ics, bus_type, roles}."""
    if bus is not None:
        if not isinstance(bus, dict):
            raise ValueError("bus must be a detect_buses candidate object (dict)")
        raw = bus.get("nets", [])
        net_names = [(n["net"] if isinstance(n, dict) else str(n)) for n in raw]
        return {
            "nets": net_names,
            "hub_hint": hub_ic,
            "common_ics": [str(c).strip().upper() for c in bus.get("common_ics", []) or []],
            "bus_type": bus_type or bus.get("bus_type"),
        }
    if nets is None:
        raise ValueError("provide either `bus` (a detect_buses candidate) or `nets` (a list of net names)")
    net_names = [(n["net"] if isinstance(n, dict) else str(n)) for n in nets]
    return {
        "nets": net_names,
        "hub_hint": hub_ic,
        "common_ics": [],
        "bus_type": bus_type,
    }


def _compute_bus_bundles(
    project_path: str | Path,
    bus: Any = None,
    nets: Any = None,
    hub_ic: Any = None,
    bus_type: Any = None,
    clip_band_mult: float | None = None,
    station_step: float | None = None,
    ic_ref_prefixes: list[str] | None = None,
) -> dict[str, Any]:
    """Core of Phase 5. Builds the anchor-and-corridor per-destination-IC bundles
    and returns rich internal geometry (axis, centerline offset, per-net projected
    segments) that both `measure_bus_corridor_areas` (public JSON) and
    `get_trace_cost` (deviation term) consume. See `measure_bus_corridor_areas`
    for the design/output description. Read-only."""
    board_path, _, netlist_path = _resolve_project_path(project_path)
    ic_prefixes = tuple(p.strip().upper() for p in (ic_ref_prefixes or ["U", "IC", "Q"]))

    spec = _resolve_bus_spec(bus, nets, hub_ic, bus_type)
    bus_nets = spec["nets"]
    warnings: list[str] = []

    settings = load_pcb_settings(project_path)["config"]
    band_mult = float(clip_band_mult) if clip_band_mult is not None else float(settings.get("corridor", {}).get("clip_band_mult", 3.0))

    netlist = _parse_nets_cached(netlist_path)
    refs_map, net_map = _build_net_maps(netlist)
    # Board-wide net participation per ref (the master/hub touches the most nets)
    # - used only to break hub-selection ties among equally-common ICs.
    design_net_count: dict[str, int] = {}
    for ref, entries in refs_map.items():
        design_net_count[ref.strip().upper()] = design_net_count.get(ref.strip().upper(), 0) + len({e["net"] for e in entries})

    # Per-net IC membership and dominant width.
    width_data = get_net_track_widths(project_path)
    width_by_net = {e["net"]: e for e in width_data.get("nets", [])}
    dom_widths: dict[float, int] = {}
    for net_name in bus_nets:
        dw = (width_by_net.get(net_name) or {}).get("dominant_width")
        if dw:
            dom_widths[dw] = dom_widths.get(dw, 0) + 1
    dominant_width = max(dom_widths.items(), key=lambda kv: kv[1])[0] if dom_widths else 0.2
    band = band_mult * dominant_width

    net_ics = {net_name: _ic_set_for_net(net_name, net_map, ic_prefixes) for net_name in bus_nets}

    # --- Hub selection (Step A) ---
    hub = None
    if spec["hub_hint"]:
        hub = str(spec["hub_hint"]).strip().upper()
    else:
        candidate_ics = spec["common_ics"]
        if not candidate_ics:
            # intersection of IC sets over member nets
            ic_sets = [s for s in net_ics.values() if s]
            candidate_ics = sorted(set.intersection(*ic_sets)) if ic_sets else []
        if candidate_ics:
            # pick the IC on the most member nets; tie-broken by the ref that
            # touches the most nets board-wide (the master/MCU), then by name.
            def hub_key(ic: str) -> tuple[int, int, str]:
                member_hits = sum(1 for s in net_ics.values() if ic in s)
                return (-member_hits, -design_net_count.get(ic, 0), ic)
            hub = min(candidate_ics, key=hub_key)

    all_ics: set[str] = set()
    for s in net_ics.values():
        all_ics |= s
    destinations = sorted(all_ics - ({hub} if hub else set()))

    # Segments grouped by net (copper only; empty-net excluded), from the cache.
    tracks = _parse_tracks_cached(board_path)
    segs_by_net: dict[str, list[dict[str, Any]]] = {}
    for seg in tracks["segments"] + tracks["arcs"]:
        if seg["net"] in net_ics:
            segs_by_net.setdefault(seg["net"], []).append(seg)

    # union hull over all bus copper (all layers pooled), for the reference envelope.
    union_points: list[tuple[float, float]] = []
    for seg_list in segs_by_net.values():
        for seg in seg_list:
            union_points.append((seg["start"]["x"], seg["start"]["y"]))
            union_points.append((seg["end"]["x"], seg["end"]["y"]))
    union_hull_area = _convex_hull_area(union_points)

    # Degenerate: no hub, or no destination IC on the board -> one un-grouped hull.
    if hub is None or not destinations:
        if hub is None:
            warnings.append("no hub IC common to the bus nets; reporting one un-grouped hull (grouped:false)")
        else:
            warnings.append(f"hub {hub} has no destination IC on any bus net; reporting one un-grouped hull (grouped:false)")
        return {
            "bus_type": spec["bus_type"],
            "hub_ic": hub,
            "bundles": [],
            "sum_of_bundle_areas_mm2": 0.0,
            "union_hull_area_mm2": round(union_hull_area, 4),
            "unassigned_segment_count": sum(len(v) for v in segs_by_net.values()),
            "grouped": False,
            "warnings": warnings,
            "clip_band_mm": round(band, 4),
        }

    # Pad anchors (Step B).
    footprints = _parse_footprint_pads_cached(board_path)
    pads_by_ref: dict[str, list[dict[str, Any]]] = {}
    for fp in footprints.values():
        ref = fp.get("reference", "").strip().upper()
        if ref:
            pads_by_ref.setdefault(ref, []).extend(fp.get("pads", []))

    def centroid_on_nets(ref: str, allowed_nets: set[str]) -> tuple[float, float] | None:
        xs, ys = [], []
        for pad in pads_by_ref.get(ref, []):
            if pad.get("net") in allowed_nets:
                xs.append(pad["position"]["x"])
                ys.append(pad["position"]["y"])
        if not xs:
            return None
        return (sum(xs) / len(xs), sum(ys) / len(ys))

    bus_net_set = set(bus_nets)
    hub_pt = centroid_on_nets(hub, bus_net_set)

    # Per-net role and bundle membership.
    roles: dict[str, str] = {}
    bundle_nets: dict[str, list[str]] = {D: [] for D in destinations}
    for net_name in bus_nets:
        dests_on_net = net_ics[net_name] - {hub}
        if not dests_on_net:
            roles[net_name] = "hub_only"
        elif len(dests_on_net) == 1:
            roles[net_name] = "dedicated"
        else:
            roles[net_name] = "shared"
        for D in dests_on_net:
            if D in bundle_nets:
                bundle_nets[D].append(net_name)

    dest_pt: dict[str, tuple[float, float] | None] = {}
    for D in destinations:
        dest_pt[D] = centroid_on_nets(D, set(bundle_nets[D]))

    single_destination = len(destinations) == 1

    # --- Step C: assign/clip copper to bundles ---
    def midpoint(seg: dict[str, Any]) -> tuple[float, float]:
        return ((seg["start"]["x"] + seg["end"]["x"]) / 2.0, (seg["start"]["y"] + seg["end"]["y"]) / 2.0)

    assigned: dict[str, list[tuple[str, dict[str, Any]]]] = {D: [] for D in destinations}
    unassigned_count = 0

    # Precompute each bundle's axis unit vector for projection/perp tests.
    axis_geom: dict[str, tuple[float, float, float, float, float]] = {}  # D -> (hx,hy,ux,uy,len)
    for D in destinations:
        dp = dest_pt.get(D)
        if hub_pt is not None and dp is not None:
            axl = math.hypot(dp[0] - hub_pt[0], dp[1] - hub_pt[1])
            if axl > 1e-9:
                axis_geom[D] = (hub_pt[0], hub_pt[1], (dp[0] - hub_pt[0]) / axl, (dp[1] - hub_pt[1]) / axl, axl)

    # Dedicated + (single-destination) copper is assigned wholesale (seeds).
    dedicated_by_dest: dict[str, list[dict[str, Any]]] = {D: [] for D in destinations}
    shared_queue: list[tuple[str, dict[str, Any]]] = []
    for net_name, seg_list in segs_by_net.items():
        role = roles.get(net_name, "hub_only")
        if role == "hub_only":
            unassigned_count += len(seg_list)
            continue
        if role == "dedicated" or single_destination:
            D = next(iter(net_ics[net_name] - {hub})) if role == "dedicated" else destinations[0]
            if D in assigned:
                for seg in seg_list:
                    assigned[D].append((net_name, seg))
                    dedicated_by_dest[D].append(seg)
            else:
                unassigned_count += len(seg_list)
            continue
        for seg in seg_list:
            shared_queue.append((net_name, seg))

    # Shared copper: assign each segment to the destination bundle whose axis its
    # midpoint runs closest to (perpendicular distance), gated so it only joins a
    # bundle where it physically runs with it - either its projection lies within
    # the hub->dest span (extended by the pitch band), OR it sits within `band` of
    # that bundle's dedicated copper. Segments matching no bundle are `unassigned`
    # (the fan-out/transition copper), so nothing is silently dropped. (This is a
    # per-segment nearest-axis + span/band gate rather than the plan's iterative
    # "band of an already-assigned trace" propagation: contiguous-segment
    # propagation pulled whole shared traces into a single bundle and defeated the
    # per-destination clip on bowed real traces - see report.)
    for net_name, seg in shared_queue:
        mx, my = midpoint(seg)
        best_D = None
        best_perp = float("inf")
        for D in bundle_nets:
            if net_name not in bundle_nets[D] or D not in axis_geom:
                continue
            hx, hy, ux, uy, axl = axis_geom[D]
            t = (mx - hx) * ux + (my - hy) * uy
            perp = _perp_distance_to_axis(mx, my, hx, hy, hx + ux * axl, hy + uy * axl)
            in_span = -band <= t <= axl + band
            near_dedicated = any(
                aseg["layer"] == seg["layer"]
                and math.hypot(mx - midpoint(aseg)[0], my - midpoint(aseg)[1]) <= band
                for aseg in dedicated_by_dest[D]
            )
            if (in_span or near_dedicated) and perp < best_perp:
                best_perp = perp
                best_D = D
        if best_D is not None:
            assigned[best_D].append((net_name, seg))
        else:
            unassigned_count += 1

    # --- Step D: corridor + hull area per bundle ---
    bundles_out: list[dict[str, Any]] = []
    sum_bundle_area = 0.0
    for D in destinations:
        members = assigned[D]
        dp = dest_pt.get(D)
        distinct_nets = sorted({n for n, _ in members})
        layers = sorted({seg["layer"] for _, seg in members})
        if hub_pt is None or dp is None:
            axis_len = 0.0
        else:
            axis_len = math.hypot(dp[0] - hub_pt[0], dp[1] - hub_pt[1])

        corridor_area = 0.0
        hull_area = 0.0
        centerline_s = 0.0
        net_segs: dict[str, list[dict[str, float]]] = {}

        if hub_pt is not None and dp is not None and axis_len > 1e-9:
            ux = (dp[0] - hub_pt[0]) / axis_len
            uy = (dp[1] - hub_pt[1]) / axis_len
            vx, vy = -uy, ux
            # station step: fine enough but bounded to keep it cheap on long axes.
            step = station_step if station_step else (dominant_width or 0.2)
            if step <= 0:
                step = 0.2
            if axis_len / step > 2000:
                step = axis_len / 2000.0

            # project every assigned segment; accumulate per-net midpoint stats.
            proj_by_layer: dict[str, list[tuple[str, float, float, float, float]]] = {}
            all_s: list[tuple[float, float]] = []  # (s, length) for centerline
            for net_name, seg in members:
                p1, p2 = seg["start"], seg["end"]
                t1 = (p1["x"] - hub_pt[0]) * ux + (p1["y"] - hub_pt[1]) * uy
                s1 = (p1["x"] - hub_pt[0]) * vx + (p1["y"] - hub_pt[1]) * vy
                t2 = (p2["x"] - hub_pt[0]) * ux + (p2["y"] - hub_pt[1]) * uy
                s2 = (p2["x"] - hub_pt[0]) * vx + (p2["y"] - hub_pt[1]) * vy
                proj_by_layer.setdefault(seg["layer"], []).append((net_name, t1, s1, t2, s2))
                mid_s = (s1 + s2) / 2.0
                seg_len = seg["length"]
                all_s.append((mid_s, seg_len))
                net_segs.setdefault(net_name, []).append({"s": mid_s, "length": seg_len})

            total_len = sum(l for _, l in all_s)
            centerline_s = (sum(s * l for s, l in all_s) / total_len) if total_len > 0 else 0.0

            raw_points = [(seg["start"]["x"], seg["start"]["y"]) for _, seg in members]
            raw_points += [(seg["end"]["x"], seg["end"]["y"]) for _, seg in members]
            hull_area = _convex_hull_area(raw_points)

            # corridor area: midpoint-rule stations along the axis, perpendicular
            # spread across >=2 DISTINCT bundle nets at each station.
            if len(distinct_nets) >= 2:
                nslabs = max(1, int(math.ceil(axis_len / step)))
                for layer_proj in proj_by_layer.values():
                    for i in range(nslabs):
                        ti = (i + 0.5) * step
                        by_net_s: dict[str, list[float]] = {}
                        for net_name, t1, s1, t2, s2 in layer_proj:
                            lo, hi = (t1, t2) if t1 <= t2 else (t2, t1)
                            if lo - 1e-9 <= ti <= hi + 1e-9:
                                if abs(t2 - t1) < 1e-9:
                                    sv = (s1 + s2) / 2.0
                                else:
                                    frac = (ti - t1) / (t2 - t1)
                                    sv = s1 + frac * (s2 - s1)
                                by_net_s.setdefault(net_name, []).append(sv)
                        if len(by_net_s) >= 2:
                            flat = [v for vs in by_net_s.values() for v in vs]
                            corridor_area += (max(flat) - min(flat)) * step

        trace_count = len(distinct_nets)
        mean_spacing = (corridor_area / axis_len / (trace_count - 1)) if (axis_len > 0 and trace_count > 1 and corridor_area > 0) else 0.0
        bend_ratio = round(hull_area / corridor_area, 4) if corridor_area > 1e-9 else None
        sum_bundle_area += corridor_area

        bundles_out.append(
            {
                "destination_ic": D,
                "nets": [{"net": n, "role": roles.get(n, "shared")} for n in distinct_nets],
                "trace_count": trace_count,
                "axis": {
                    "from": [round(hub_pt[0], 4), round(hub_pt[1], 4)] if hub_pt else None,
                    "to": [round(dp[0], 4), round(dp[1], 4)] if dp else None,
                },
                "length_mm": round(axis_len, 4),
                "corridor_area_mm2": round(corridor_area, 4),
                "hull_area_mm2": round(hull_area, 4),
                "bend_ratio": bend_ratio,
                "mean_spacing_mm": round(mean_spacing, 4),
                "layers": layers,
                # internal geometry for the Phase 6 deviation term (stripped by
                # measure_bus_corridor_areas before returning public JSON):
                "_hub_pt": hub_pt,
                "_dest_pt": dp,
                "_centerline_s": centerline_s,
                "_net_segs": net_segs,
                "_axis_len": axis_len,
            }
        )

    if union_hull_area and sum_bundle_area > union_hull_area * 1.001:
        warnings.append(
            "sum_of_bundle_areas exceeds union_hull (shared copper is counted once per bundle it serves - this is expected, not an error)"
        )
    multi_layer = sorted({seg["layer"] for v in segs_by_net.values() for seg in v})
    if len(multi_layer) > 1:
        warnings.append(f"bus copper spans {len(multi_layer)} layers {multi_layer}; corridor computed per layer then summed")

    return {
        "bus_type": spec["bus_type"],
        "hub_ic": hub,
        "bundles": bundles_out,
        "sum_of_bundle_areas_mm2": round(sum_bundle_area, 4),
        "union_hull_area_mm2": round(union_hull_area, 4),
        "unassigned_segment_count": unassigned_count,
        "grouped": True,
        "warnings": warnings,
        "clip_band_mm": round(band, 4),
    }


def measure_bus_corridor_areas(
    project_path: str | Path,
    bus: Any = None,
    nets: Any = None,
    hub_ic: Any = None,
    bus_type: Any = None,
    clip_band_mult: float | None = None,
    station_step: float | None = None,
    ic_ref_prefixes: list[str] | None = None,
) -> dict[str, Any]:
    """Phase 5: measure the routing-corridor area a bus bundle's traces enclose -
    the "area between the traces" - PER destination IC, so a bus that fans out to
    several ICs is measured as the sub-bundles that actually run together, not as
    one inflated envelope.

    Input is EITHER a `detect_buses` candidate object (`bus=`) or an explicit net
    list (`nets=[...]`, optionally `hub_ic=`). Design ("anchor-and-corridor"):
      - Roles (reuse Phase 3c): `hub_ic` is the IC common to all bus nets;
        `destinations` are the other ICs; each bus net is `dedicated` (touches
        only {hub, D}) or `shared` (hub + >=2 destinations).
      - Anchors (reuse `_parse_footprint_pads`): hub/destination pad centroids on
        the bus nets give each bundle an axis hub->dest to clip against.
      - Clip (Step C): each shared net's copper is assigned to the nearest
        destination axis, within a band (`clip_band_mult` x dominant trace width,
        default from `corridor.clip_band_mult`) of an already-assigned trace of
        that bundle; segments matching no bundle are reported as `unassigned`.
      - Area (Step D): `corridor_area` = station-resampled perpendicular spread
        along the axis (the literal inter-trace corridor, bend-insensitive);
        `hull_area` = convex hull of the bundle's clipped copper (sanity bound,
        always >= corridor); `bend_ratio` = hull/corridor. Computed per copper
        layer then summed. A bundle needs >=2 distinct nets for a nonzero area.

    Reports both `sum_of_bundle_areas_mm2` (shared copper counted once per bundle
    it serves) and `union_hull_area_mm2` (whole-bus envelope) so shared-copper
    double counting is explicit. Degenerate cases: single destination -> one
    bundle, no clipping; no hub / no destination IC -> one un-grouped hull with
    `grouped:false`. Pure stdlib geometry; read-only - nothing is written."""
    result = _compute_bus_bundles(
        project_path,
        bus=bus,
        nets=nets,
        hub_ic=hub_ic,
        bus_type=bus_type,
        clip_band_mult=clip_band_mult,
        station_step=station_step,
        ic_ref_prefixes=ic_ref_prefixes,
    )
    # Strip the internal geometry keys (used only by get_trace_cost's deviation term).
    for bundle in result.get("bundles", []):
        for key in ("_hub_pt", "_dest_pt", "_centerline_s", "_net_segs", "_axis_len"):
            bundle.pop(key, None)
    result["project_path"] = str(project_path)
    return result


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


# Digit-V-digit convention (3V3 -> 3.3, 1V8 -> 1.8) tried before the plain
# `_VOLTAGE_RE`-shaped alternative at each scan position, in one combined
# regex, so a single "3V3" token is captured once by the more specific
# branch instead of also partially matching the generic branch ("3V" -> 3.0)
# and manufacturing a spurious second, lower candidate / false ambiguity.
_NET_VOLTAGE_TOKEN_RE = re.compile(r"(\d+)[vV](\d+)|(\d+(?:\.\d+)?)\s*[vV](?![a-zA-Z])")


def _infer_net_voltage(
    net_name: str,
    net_voltages: dict[str, Any] | None = None,
    gnd_tokens: list[str] | None = None,
) -> dict[str, Any]:
    """Infer a net's voltage from its name (Phase 8.1). A standalone helper -
    deliberately NOT buried inside the capacitor audit below - because later
    phases (e.g. plane/zone voltage-mismatch warnings) are expected to reuse
    it: `power_net_patterns` (Phase 7.2) says *whether* a net is power, this
    says *what voltage*.

    Applied to the net's basename (last '/'-segment, uppercased - the same
    normalization `_normalize_net_basename` uses for role matching elsewhere
    in this module). Precedence, first match wins:
      1. `net_voltages` explicit override (exact, case-insensitive match) -
         for names that carry no number at all (VBUS, AREF).
      2. GND rule: basename contains any `gnd_tokens` token (case-insensitive
         substring) -> 0.0 V. Covers GND_Main, GND_Safty, AGND... Beats a
         label match on the same name (see 4 below).
      3. Labeled voltage: `_VOLTAGE_RE` (12V_Main -> 12.0, 3.3v_Safty -> 3.3,
         +5V -> 5.0) plus the 3V3/1V8 digit-V-digit convention. Multiple
         distinct voltage tokens in one name -> the largest wins and
         `ambiguous_label` is flagged.
      4. Otherwise -> unlabeled (voltage None, source "none").

    A net matching both a GND token and a voltage label (`GND_5V_RTN`) has
    GND win per (2), but the row is still flagged `ambiguous_label` so the
    collision stays visible instead of silently picking a side.

    Returns `{"net", "basename", "voltage": float | None,
    "source": "override" | "gnd" | "label" | "none", "ambiguous_label": bool}`
    so a wrong guess is visible, never silent.
    """
    net_voltages = net_voltages or {}
    gnd_tokens = gnd_tokens or []
    basename = _normalize_net_basename(net_name)

    overrides = {str(key).strip().upper(): value for key, value in net_voltages.items()}
    if basename in overrides:
        return {
            "net": net_name,
            "basename": basename,
            "voltage": _coerce_voltage(overrides[basename]),
            "source": "override",
            "ambiguous_label": False,
        }

    is_gnd = any(str(token).strip().upper() in basename for token in gnd_tokens if str(token).strip())

    candidates: list[float] = []
    for match in _NET_VOLTAGE_TOKEN_RE.finditer(basename):
        if match.group(1) is not None:
            candidates.append(float(f"{match.group(1)}.{match.group(2)}"))
        else:
            candidates.append(float(match.group(3)))
    label_voltage = max(candidates) if candidates else None

    if is_gnd:
        return {
            "net": net_name,
            "basename": basename,
            "voltage": 0.0,
            "source": "gnd",
            "ambiguous_label": label_voltage is not None,
        }

    if label_voltage is not None:
        return {
            "net": net_name,
            "basename": basename,
            "voltage": label_voltage,
            "source": "label",
            "ambiguous_label": len(set(candidates)) > 1,
        }

    return {"net": net_name, "basename": basename, "voltage": None, "source": "none", "ambiguous_label": False}


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


_CAP_VERDICT_ORDER = [
    "under_rated",
    "unknown_rating",
    "under_derated",
    "ok",
    "one_net_unlabeled",
    "no_labeled_nets",
    "unsupported_pins",
]


def audit_capacitor_net_voltages(project_path: str | Path) -> dict[str, Any]:
    """Phase 8: net-aware capacitor voltage check. Extends
    `audit_capacitor_voltages` (which only checks that a rating is *written*
    into the Value field) by reading the netlist to find the two nets each
    capacitor instance (`C<n>` reference convention, same as
    `audit_capacitor_voltages`/`list_schematic_parts`) actually sits across,
    inferring each net's voltage from its name (`_infer_net_voltage`, 8.1),
    and comparing the capacitor's rated voltage against the *applied*
    differential voltage |V(net_a) - V(net_b)| - the cap between a 12V and a
    3.3V rail sees 8.7V, not 12V; a decoupler 12V_Main<->GND_Main sees 12V.

    Knobs come from `pcb_settings.json`'s `schematic_checks.cap_voltage`
    (`derating_min_ratio`, `gnd_tokens`, `net_voltages`, `default_cap_rating`)
    via `load_pcb_settings` - Phase 6.1's schema, Phase 8.1/8.2 of
    NETCLASS_PLAN.md.

    Capacitor instances are read directly off the flattened schematic symbol
    list (the same data `list_schematic_parts` groups by Value+Footprint),
    filtered to the `C<n>` reference convention and with DNP instances
    excluded - per-instance rather than per-group, because net topology is a
    per-instance property: two capacitors that share a Value+Footprint can
    still sit on completely different net pairs.

    Per (non-DNP) capacitor instance:
      - `unsupported_pins` - the netlist doesn't show exactly 2 pins for this
        reference (arrays/4-terminal caps, or a reference missing from the
        netlist altogether) - skipped from scoring.
      - Both nets resolve to a voltage -> `applied_v` = |Va - Vb|, `rated_v`
        from `_extract_voltage` on Value else `default_cap_rating`:
          - `unknown_rating` - no rating anywhere (the cap worth chasing
            first - listed right after hard failures),
          - `under_rated`  - rated_v < applied_v (hard fail),
          - `under_derated` - rated_v < derating_min_ratio * applied_v (works,
            but violates derating policy),
          - `ok` - rated_v >= derating_min_ratio * applied_v.
      - Exactly one net resolves -> `one_net_unlabeled`, informational, with
        `assumed_applied_v` = the resolved side vs. 0V (the common
        rail<->signal decoupler case, labeled as an assumption).
      - Neither net resolves -> `no_labeled_nets`, skipped from scoring but
        counted.
    A net matching both a GND token and a voltage label (`GND_5V_RTN`) flags
    the row `ambiguous_label` (see `_infer_net_voltage`).

    Read-only. Netlist staleness: the `.net` file is a schematic export and
    can lag the board, so netlist net names are cross-checked against the
    board's own pad nets (ground truth) and mismatches are reported in
    `stale_netlist_warnings` - the same guard `detect_buses` uses - rather
    than silently mis-scoring caps against a stale rail name.

    Rows are sorted worst-first (`under_rated`, `unknown_rating`,
    `under_derated`, `ok`, `one_net_unlabeled`, `no_labeled_nets`,
    `unsupported_pins`), each reference, value, rated_v, both nets with their
    inferred voltage + source, applied_v, required_min (= ratio x applied),
    and verdict - plus summary counts and the settings actually used
    (self-describing, like Phase 6's `weights_used`).
    """
    board_path, _, netlist_path = _resolve_project_path(project_path)
    directory = _resolve_schematic_dir(project_path)

    settings_result = load_pcb_settings(project_path)
    cap_cfg = settings_result["config"]["schematic_checks"]["cap_voltage"]
    ratio = float(cap_cfg.get("derating_min_ratio", 2.0))
    gnd_tokens = list(cap_cfg.get("gnd_tokens", []))
    net_voltages = dict(cap_cfg.get("net_voltages", {}))
    default_cap_rating = cap_cfg.get("default_cap_rating")
    default_rating_numeric = _coerce_voltage(default_cap_rating)

    nets = _parse_nets_cached(netlist_path)
    ref_to_nets, _ = _build_net_maps(nets)

    # Netlist staleness guard (same pattern as detect_buses): cross-check
    # netlist net names against the board's own pad nets (ground truth,
    # independent of the schematic export) and warn on mismatch rather than
    # refusing the run.
    footprints = _parse_footprint_pads_cached(board_path)
    board_net_names: set[str] = set()
    for fp in footprints.values():
        for pad in fp.get("pads", []):
            pad_net = pad.get("net", "")
            if pad_net:
                board_net_names.add(pad_net)
    netlist_net_names = {n.get("name", "") for n in nets if n.get("name")}
    stale_netlist_warnings: list[str] = []
    only_in_netlist = sorted(netlist_net_names - board_net_names)
    only_on_board = sorted(board_net_names - netlist_net_names)
    if only_in_netlist:
        stale_netlist_warnings.append(
            f"{len(only_in_netlist)} net(s) in the .net export have no matching pad net on the board "
            f"(netlist may be stale - re-export from the schematic): {only_in_netlist[:20]}"
        )
    if only_on_board:
        stale_netlist_warnings.append(
            f"{len(only_on_board)} net(s) on the board's pads are absent from the .net export: {only_on_board[:20]}"
        )

    components = [
        c
        for c in _flatten_schematic_components(directory)
        if not c["lib_id"].startswith("power:")
        and not c["reference"].startswith("#")
        and _CAPACITOR_REF_RE.match(c["reference"])
        and not c["dnp"]
    ]
    components.sort(key=lambda c: _reference_sort_key(c["reference"]))

    def _net_row(net_name: str) -> dict[str, Any]:
        info = _infer_net_voltage(net_name, net_voltages, gnd_tokens)
        return {
            "name": net_name,
            "voltage": info["voltage"],
            "source": info["source"],
            "ambiguous_label": info["ambiguous_label"],
        }

    rows: list[dict[str, Any]] = []
    counts: dict[str, int] = {}

    for component in components:
        ref = component["reference"]
        value = component["value"]
        nodes = ref_to_nets.get(ref, [])

        extracted = _extract_voltage(value)
        if extracted is not None:
            rated_v_numeric = extracted[1]
            rated_v_source = "value"
        elif default_rating_numeric is not None:
            rated_v_numeric = default_rating_numeric
            rated_v_source = "default"
        else:
            rated_v_numeric = None
            rated_v_source = "unknown"

        if len(nodes) != 2:
            verdict = "unsupported_pins"
            row = {
                "reference": ref,
                "value": value,
                "rated_v": rated_v_numeric,
                "rated_v_source": rated_v_source,
                "nets": [_net_row(node.get("net", "")) for node in nodes],
                "applied_v": None,
                "required_min": None,
                "ambiguous_label": False,
                "verdict": verdict,
                "note": f"expected 2 netlist pins for a capacitor, found {len(nodes)}",
            }
            rows.append(row)
            counts[verdict] = counts.get(verdict, 0) + 1
            continue

        net_a = _net_row(nodes[0].get("net", ""))
        net_b = _net_row(nodes[1].get("net", ""))
        ambiguous_label = bool(net_a["ambiguous_label"] or net_b["ambiguous_label"])
        va, vb = net_a["voltage"], net_b["voltage"]

        row = {
            "reference": ref,
            "value": value,
            "rated_v": rated_v_numeric,
            "rated_v_source": rated_v_source,
            "nets": [net_a, net_b],
            "ambiguous_label": ambiguous_label,
        }

        if va is not None and vb is not None:
            applied_v = abs(va - vb)
            required_min = ratio * applied_v
            row["applied_v"] = applied_v
            row["required_min"] = required_min
            if rated_v_numeric is None:
                verdict = "unknown_rating"
            elif rated_v_numeric < applied_v:
                verdict = "under_rated"
            elif rated_v_numeric < required_min:
                verdict = "under_derated"
            else:
                verdict = "ok"
        elif va is not None or vb is not None:
            resolved = va if va is not None else vb
            row["applied_v"] = None
            row["required_min"] = None
            row["assumed_applied_v"] = resolved
            verdict = "one_net_unlabeled"
        else:
            row["applied_v"] = None
            row["required_min"] = None
            verdict = "no_labeled_nets"

        row["verdict"] = verdict
        rows.append(row)
        counts[verdict] = counts.get(verdict, 0) + 1

    order_index = {verdict: i for i, verdict in enumerate(_CAP_VERDICT_ORDER)}
    rows.sort(key=lambda r: (order_index.get(r["verdict"], len(_CAP_VERDICT_ORDER)), _reference_sort_key(r["reference"])))

    summary = {verdict: counts.get(verdict, 0) for verdict in _CAP_VERDICT_ORDER}
    summary["total"] = len(rows)

    return {
        "schematic_dir": str(directory),
        "netlist_path": str(netlist_path),
        "capacitor_count": len(rows),
        "summary": summary,
        "settings_used": {
            "settings_path": settings_result["settings_path"],
            "loaded_from_file": settings_result["loaded_from_file"],
            "derating_min_ratio": ratio,
            "gnd_tokens": gnd_tokens,
            "net_voltages": net_voltages,
            "default_cap_rating": default_cap_rating,
        },
        "stale_netlist_warnings": stale_netlist_warnings,
        "capacitors": rows,
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


DEFAULT_PCB_SETTINGS: dict[str, Any] = {
    "version": 1,
    "trace_cost": {
        "weights": {
            "length_mm": 1.0,
            "via": 5.0,
            "deviation_mm": 2.0,
            "excess_length": 10.0,
            "layer_span": 8.0,
        },
        "deviation": {
            "metric": "mean_perp_distance",
            "reference": "bus_centerline",
        },
        "via_weights": {"through": 1.0, "microvia": 0.5, "blind_buried": 1.5},
        "non_bus_deviation": 0.0,
    },
    "corridor": {"clip_band_mult": 3.0},
    "bus_detection": {"ic_ref_prefixes": ["U", "IC"], "extra_signatures": {}},
    "layer_purpose": {
        "signal": {"signal": 1.0, "mixed": 1.2, "power": 4.0, "jumper": 2.0},
        "power": {"signal": 2.0, "mixed": 1.2, "power": 1.0, "jumper": 3.0},
        "power_net_patterns": ["^GND", r"^\+?\d+\.?\d*[Vv]", "VCC", "VDD", "12[Vv]", r"3\.3[Vv]", "5[Vv]"],
    },
    "autorouter": {
        "grid_mm": 0.2,
        "global_grid_mm": 2.0,
        "search_window_margin_mm": 8.0,
        "clearance_fallback_mm": 0.2,
        "cost": {
            "step": 1.0,
            "via": 25.0,
            "direction_change": 2.0,
            "congestion": 8.0,
            "off_corridor": 4.0,
            "off_direction": 2.0,
            "away_from_home_per_mm": 0.5,
        },
        "layer_directions": "auto",
        "max_ripup_iterations": 5,
        "allowed_layers": [],
        "acceleration": "auto",
        "gpu": {"memory_budget_mb": 0, "batch": "auto", "oom_fallback": True},
        "cpu": {"workers": 0, "ram_budget_mb": 0, "replicas": "auto", "replica_sync": "chunk_end"},
        "progress": {"events": True, "open_viewer": False, "color_theme": "auto"},
    },
    "plane": {
        "plane_step": 0.05,
        "attachment_via": 8.0,
        "island_base": 40.0,
        "orphan_island": 1000.0,
        "island_min_attachments_warn": 2,
        "create_plane": 15.0,
        "modify_plane": 5.0,
    },
    "schematic_checks": {
        "cap_voltage": {
            "derating_min_ratio": 2.0,
            "gnd_tokens": ["GND", "AGND", "DGND", "PGND", "VSS"],
            "net_voltages": {},
            "default_cap_rating": None,
        }
    },
    "optimizer": {
        "max_iterations": 20,
        "time_budget_s": 300,
        "worst_k": 5,
        "unrouted_penalty": 500.0,
        "accept": "greedy",
        "sa_initial_temp": 50.0,
        "sa_cooling": 0.9,
        "convergence_delta": 0.5,
        "seed": 1,
        "ai_decisions": {
            "enabled": True,
            "min_score_spread": 5.0,
            "max_pauses_per_run": 12,
            "decision_types": [
                "bundle_layer",
                "plane_proposal",
                "conflict_yield",
                "stitching_budget",
                "sa_large_move",
                "give_up_net",
            ],
        },
    },
    "high_speed": {
        "bus_frequencies_mhz": {
            "SPI": 20,
            "QSPI": 80,
            "I2C": 0.4,
            "I2S": 12,
            "UART": 1,
            "CAN": 1,
            "USB": 480,
            "MIPI": 1000,
            "DDR": 800,
            "SWD": 4,
            "JTAG": 10,
            "CLK": 25,
        },
        "velocity_fraction": 0.5,
        "rise_fraction": 0.05,
        "critical_length_overrides_mm": {},
        "critical_fraction": 0.9,
        "length_weight_mult": 4.0,
    },
    "switch_node": {
        "min_inductor_mm": 2.0,
        "length_weight_mult": 8.0,
    },
}


def _pcb_settings_path(project_path: str | Path) -> Path:
    board_path, _, _ = _resolve_project_path(project_path)
    return board_path.parent / "pcb_settings.json"


def _deep_merge_settings(defaults: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge `overrides` over `defaults`, dict-by-dict; any
    non-dict value (including lists) in `overrides` replaces the default
    wholesale rather than being element-merged."""
    result = copy.deepcopy(defaults)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge_settings(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _validate_pcb_settings_weights(config: dict[str, Any]) -> None:
    """Every weight in `trace_cost` (base weights and `via_weights`) must be a
    non-negative number - a negative weight would make the cost model reward
    the very thing it's supposed to penalize, and a non-numeric weight breaks
    every downstream arithmetic op silently instead of failing loudly here."""
    trace_cost = config.get("trace_cost", {})
    weights = trace_cost.get("weights", {})
    for key, value in weights.items():
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
            raise ValueError(f"trace_cost.weights.{key} must be a non-negative number, got {value!r}")
    via_weights = trace_cost.get("via_weights", {})
    for key, value in via_weights.items():
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
            raise ValueError(f"trace_cost.via_weights.{key} must be a non-negative number, got {value!r}")
    non_bus_deviation = trace_cost.get("non_bus_deviation", 0.0)
    if not isinstance(non_bus_deviation, (int, float)) or isinstance(non_bus_deviation, bool) or non_bus_deviation < 0:
        raise ValueError(f"trace_cost.non_bus_deviation must be a non-negative number, got {non_bus_deviation!r}")


def load_pcb_settings(project_path: str | Path) -> dict[str, Any]:
    """Load `pcb_settings.json` from the project directory (next to
    `<name>.kicad_pro`) and deep-merge it over the in-code defaults (Phase
    6.1's schema - trace-cost weights, corridor/bus-detection/layer-purpose/
    autorouter/plane/schematic-check/optimizer knobs). A missing file is not
    an error: every tool that reads settings works out of the box on pure
    defaults. `trace_cost` weights are validated non-negative after the
    merge; a bad file raises rather than silently producing nonsense costs.

    Returns the effective config plus which top-level keys came from the file
    (even a partial override) vs. untouched defaults, so a caller can tell
    "this project has customized X" from "X is stock"."""
    settings_path = _pcb_settings_path(project_path)
    file_data: dict[str, Any] = {}
    loaded_from_file = False
    if settings_path.exists():
        try:
            file_data = json.loads(settings_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise ValueError(f"Failed to parse {settings_path}: {exc}") from exc
        if not isinstance(file_data, dict):
            raise ValueError(f"{settings_path} must contain a JSON object at the top level")
        loaded_from_file = True

    merged = _deep_merge_settings(DEFAULT_PCB_SETTINGS, file_data)
    _validate_pcb_settings_weights(merged)

    keys_from_file = sorted(file_data.keys())
    keys_from_defaults = sorted(k for k in DEFAULT_PCB_SETTINGS.keys() if k not in file_data)

    return {
        "settings_path": str(settings_path),
        "loaded_from_file": loaded_from_file,
        "config": merged,
        "keys_from_file": keys_from_file,
        "keys_from_defaults": keys_from_defaults,
    }


def init_pcb_settings(project_path: str | Path, write: bool = False, overwrite: bool = False) -> dict[str, Any]:
    """Write the fully-populated default `pcb_settings.json` (Phase 6.1's
    schema, verbatim) into the project directory. Plain JSON
    (`json.dump(indent=2)`) - it's our own file, not KiCad's, so no s-expr
    surgery and no board-lock concern.

    Defaults to a dry run (write=False) that returns the would-be file
    content without touching disk. `write=True` refuses to clobber an
    existing file unless `overwrite=True` - seeding is meant to give a
    project its first settings file, not silently discard one a user has
    already tuned.
    """
    settings_path = _pcb_settings_path(project_path)
    content = json.dumps(DEFAULT_PCB_SETTINGS, indent=2) + "\n"
    already_exists = settings_path.exists()

    result: dict[str, Any] = {
        "settings_path": str(settings_path),
        "write": write,
        "already_exists": already_exists,
        "content": content,
        "written": False,
    }
    if write:
        if already_exists and not overwrite:
            raise FileExistsError(
                f"{settings_path} already exists; pass overwrite=True to replace it "
                "(dry-run content is available in `content` if you want to compare first)."
            )
        with settings_path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        result["written"] = True
    return result


# --- Phase 7.1: board-local state JSON (<board>.board_local.json) -----------
#
# Companion to pcb_settings.json with the opposite contract: pcb_settings.json
# is shareable POLICY (weights, multipliers) and is committed; the board-local
# file is the STATE of this working board (autorouter-owned copper uuids,
# keepouts, per-net overrides, cached user bus confirmations, last route
# session) and is gitignored - disposable, machine-local.


def _board_local_path(project_path: str | Path) -> Path:
    board_path, _, _ = _resolve_project_path(project_path)
    return board_path.with_name(f"{board_path.stem}.board_local.json")


def load_board_local(project_path: str | Path) -> dict[str, Any]:
    """Load `<board_stem>.board_local.json` from next to the board file,
    mirroring `load_pcb_settings` but deep-merged over `{}` - there are no
    in-code defaults for per-board state, so the effective data is simply the
    file's contents (deep-copied). A missing file is not an error: every
    caller works from an empty state out of the box.

    Schema-tolerant by design: the known keys are `version`,
    `autorouter_owned` ({segments, vias} uuid lists), `keepouts`,
    `net_overrides`, `confirmed_buses`, `last_route_session` - but unknown
    keys are preserved verbatim so newer/older tool versions can share the
    file without destroying each other's state.
    """
    state_path = _board_local_path(project_path)
    file_data: dict[str, Any] = {}
    loaded_from_file = False
    if state_path.exists():
        try:
            file_data = json.loads(state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise ValueError(f"Failed to parse {state_path}: {exc}") from exc
        if not isinstance(file_data, dict):
            raise ValueError(f"{state_path} must contain a JSON object at the top level")
        loaded_from_file = True

    return {
        "board_local_path": str(state_path),
        "loaded_from_file": loaded_from_file,
        "data": _deep_merge_settings({}, file_data),
    }


def save_board_local(project_path: str | Path, data: dict[str, Any]) -> dict[str, Any]:
    """Write `data` verbatim to `<board_stem>.board_local.json` (plain JSON,
    indent=2, LF newlines - our own file, not KiCad's, so no s-expr surgery
    and no board-lock concern). Callers that update a single key should
    load-modify-save so unknown keys written by other tools survive.
    """
    if not isinstance(data, dict):
        raise ValueError(f"board-local data must be a dict, got {type(data).__name__}")
    state_path = _board_local_path(project_path)
    content = json.dumps(data, indent=2) + "\n"
    with state_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)
    return {"board_local_path": str(state_path), "written": True}


def record_confirmed_bus(project_path: str | Path, candidate: dict[str, Any], name: str | None = None) -> dict[str, Any]:
    """Cache a user-confirmed bus in the board-local JSON's `confirmed_buses`
    list so `detect_buses` re-runs mark it `confirmed:true` and only present
    genuinely NEW candidates for confirmation. `candidate` is a
    `detect_buses` candidate object (its `nets` entries may be member dicts
    or plain net-name strings). An existing entry with the same bus_type and
    net set is replaced (re-confirming updates the name/date), never
    duplicated.
    """
    member_nets: list[str] = []
    for member in candidate.get("nets", []):
        member_nets.append(member["net"] if isinstance(member, dict) else str(member))
    if not member_nets:
        raise ValueError("candidate has no nets to confirm")
    bus_type = str(candidate.get("bus_type", "")) or "BUS"
    common_ics = candidate.get("common_ics") or []
    entry = {
        "bus_type": bus_type,
        "nets": sorted(member_nets),
        "hub_ic": common_ics[0] if common_ics else None,
        "name": name or candidate.get("suggested_class_name") or bus_type,
        "confirmed_on": _date.today().isoformat(),
    }

    state = load_board_local(project_path)
    data = state["data"]
    data.setdefault("version", 1)
    confirmed: list[Any] = data.setdefault("confirmed_buses", [])
    net_set = frozenset(entry["nets"])
    replaced = False
    for i, existing in enumerate(confirmed):
        if (
            isinstance(existing, dict)
            and str(existing.get("bus_type", "")) == bus_type
            and frozenset(str(n) for n in existing.get("nets", [])) == net_set
        ):
            confirmed[i] = entry
            replaced = True
            break
    if not replaced:
        confirmed.append(entry)
    save_board_local(project_path, data)
    return {
        "board_local_path": state["board_local_path"],
        "entry": entry,
        "replaced_existing": replaced,
        "confirmed_bus_count": len(confirmed),
    }


# --- Phase 7.2: layer purposes from the board file --------------------------


def _parse_board_layers(board_path: str | Path) -> list[dict[str, Any]]:
    """Parse the board's own top-level `(layers ...)` block into the copper
    stack: one `{name, ordinal, type, user_name}` per copper (`*.Cu`) layer,
    in file order (which is KiCad's physical stack order, front to back -
    the ordinals themselves are NOT stack-ordered in KiCad 9/10 files).
    `type` is KiCad's own designation: signal | power | mixed | jumper (a
    `user`-typed copper layer would be reported too, but gets no cost
    treatment anywhere - unknown/user types are a router no-go, not a cost).
    """
    text = _read_text(Path(board_path))
    open_idx = text.find("(layers")
    if open_idx == -1:
        return []
    close_idx = _find_matching_paren(text, open_idx)
    block = SexprParser(text[open_idx : close_idx + 1]).parse()

    layers: list[dict[str, Any]] = []
    for entry in block[1:] if isinstance(block, list) else []:
        if not isinstance(entry, list) or len(entry) < 3:
            continue
        try:
            ordinal = int(str(entry[0]))
        except ValueError:
            continue
        layer_name = str(entry[1])
        if not layer_name.endswith(".Cu"):
            continue
        layers.append(
            {
                "name": layer_name,
                "ordinal": ordinal,
                "type": str(entry[2]),
                "user_name": str(entry[3]) if len(entry) >= 4 else None,
            }
        )
    return layers


def _parse_board_layers_cached(board_path: Path) -> list[dict[str, Any]]:
    stat = board_path.stat()
    key = str(board_path)
    cached = _board_layers_cache.get(key)
    if cached is not None and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
        return cached[2]
    layers = _parse_board_layers(board_path)
    _board_layers_cache[key] = (stat.st_mtime, stat.st_size, layers)
    return layers


def get_board_layers(project_path: str | Path) -> dict[str, Any]:
    """Public wrapper over `_parse_board_layers_cached` for the MCP tool:
    the board's copper stack with each layer's KiCad-designated purpose."""
    board_path, _, _ = _resolve_project_path(project_path)
    layers = _parse_board_layers_cached(board_path)
    type_counts: dict[str, int] = {}
    for layer in layers:
        type_counts[layer["type"]] = type_counts.get(layer["type"], 0) + 1
    return {
        "board_path": str(board_path),
        "copper_layer_count": len(layers),
        "layers": layers,
        "type_counts": type_counts,
    }


def _net_kind(
    net_name: str,
    netclass: str | None = None,
    power_net_patterns: list[str] | None = None,
) -> str:
    """Classify a net as "power" or "signal" for layer-purpose costing.

    "power" when the net's name matches any regex in
    `layer_purpose.power_net_patterns` (from pcb_settings; defaults used when
    None is passed) - patterns are tried against both the full hierarchical
    name and its last path segment, so anchored patterns like `^GND` still
    catch `/Power/GND` - or when the net's netclass name itself indicates a
    supply class (power/pwr/gnd/ground/supply, case-insensitive).
    """
    patterns = (
        power_net_patterns
        if power_net_patterns is not None
        else DEFAULT_PCB_SETTINGS["layer_purpose"]["power_net_patterns"]
    )
    basename = net_name.rsplit("/", 1)[-1]
    for pattern in patterns:
        try:
            rx = re.compile(str(pattern))
        except re.error:
            continue
        if rx.search(net_name) or rx.search(basename):
            return "power"
    if netclass and re.search(r"power|pwr|gnd|ground|supply", netclass, re.IGNORECASE):
        return "power"
    return "signal"


def classify_critical_nets(project_path: str | Path) -> dict[str, Any]:
    """Phase 9: High-speed and critical-net classification.

    Classifies nets as critical based on three sources:
    1. **Bus/net-name high-speed table**: Bus types from `detect_buses` (SPI,
       QSPI, etc.) and name tokens (CLK, MCLK, etc.) map to a typical frequency
       via `high_speed.bus_frequencies_mhz` settings.
    2. **XTAL nets**: Nets touching a crystal/resonator (ref Y*/X* or
       footprint/lib name containing crystal/resonator/osc tokens) are always
       critical with the highest length weight.
    3. **Switching-supply inductors**: An L* component whose footprint courtyard
       exceeds `switch_node.min_inductor_mm` on both axes with one terminal net
       reaching an IC pin -> that terminal net is a switch node with an 8x length
       weight multiplier.

    For each critical net, computes:
    - `critical: true` marker
    - `reason`: one of "bus_frequency", "xtal", or "switch_node"
    - `frequency_mhz`: the classified frequency
    - `l_crit_mm`: critical length = v * t_rise / 6 (physics formula)
    - `straight_line_mm`: distance between net's connection points
    - `stack_up_gate`: whether straight_line >= 0.9 * l_crit
    - `multiplier`: per-net length-weight multiplier for `get_trace_cost`

    Returns a dict with:
    - `critical_nets`: list of classification records (one per critical net)
    - `l_crit_table`: resolved L_crit values per bus type
    - `settings_snapshot`: high_speed + switch_node config used
    """
    settings = load_pcb_settings(project_path)["config"]
    high_speed_cfg = settings.get("high_speed", {})
    switch_node_cfg = settings.get("switch_node", {})

    bus_frequencies_mhz = high_speed_cfg.get("bus_frequencies_mhz", {})
    velocity_fraction = float(high_speed_cfg.get("velocity_fraction", 0.5))
    rise_fraction = float(high_speed_cfg.get("rise_fraction", 0.05))
    critical_length_overrides_mm = high_speed_cfg.get("critical_length_overrides_mm", {})
    critical_fraction = float(high_speed_cfg.get("critical_fraction", 0.9))
    hs_length_weight = float(high_speed_cfg.get("length_weight_mult", 4.0))

    min_inductor_mm = float(switch_node_cfg.get("min_inductor_mm", 2.0))
    switch_length_weight = float(switch_node_cfg.get("length_weight_mult", 8.0))

    # Speed of light in vacuum [mm/ns]
    C_LIGHT_MM_PER_NS = 299.792
    # Compute v = c * velocity_fraction [mm/ns]
    velocity = C_LIGHT_MM_PER_NS * velocity_fraction

    board_path, _, netlist_path = _resolve_project_path(project_path)

    # Parse board components and nets
    components = _parse_board_components_cached(board_path)
    nets = _parse_nets_cached(netlist_path)
    footprints = _parse_footprint_pads_cached(board_path)
    _, net_map = _build_net_maps(nets)

    # Build a map: component reference -> component info
    comp_by_ref = {c.get("reference", ""): c for c in components}

    # Compute L_crit values per bus type (physics formula)
    # L_crit = v * t_rise / 6, where t_rise = rise_fraction / f
    # -> L_crit = v * (rise_fraction / f) / 6
    l_crit_table: dict[str, float] = {}
    for bus_type, freq_mhz in bus_frequencies_mhz.items():
        if bus_type in critical_length_overrides_mm:
            l_crit_table[bus_type] = float(critical_length_overrides_mm[bus_type])
        elif freq_mhz > 0:
            t_rise = rise_fraction / freq_mhz  # in microseconds
            l_crit = velocity * (t_rise * 1000) / 6  # convert to ns, compute in mm
            l_crit_table[bus_type] = round(l_crit, 3)
        else:
            l_crit_table[bus_type] = 0.0

    # Detect buses to find high-speed bus nets
    try:
        detected_buses = detect_buses(project_path)
    except Exception:
        detected_buses = {"candidates": []}

    # Build set of nets on high-speed buses
    bus_nets_by_type: dict[str, set[str]] = {}
    for cand in detected_buses.get("candidates", []):
        if not cand.get("qualified"):
            continue
        bus_type = cand.get("bus_type", "")
        if bus_type in bus_frequencies_mhz:
            members = cand.get("members", [])
            for member in members:
                net_name = member.get("net", "")
                if net_name:
                    bus_nets_by_type.setdefault(bus_type, set()).add(net_name)

    # Detect XTAL nets: component refs Y*/X* or footprint names with xtal/resonator/osc tokens
    xtal_nets: set[str] = set()
    xtal_tokens = {"XTAL", "CRYSTAL", "RESONATOR", "OSC"}
    for comp in components:
        ref = comp.get("reference", "").upper()
        footprint = comp.get("footprint", "").upper()
        if ref.startswith(("Y", "X")) or any(token in footprint for token in xtal_tokens):
            # Get all nets connected to this component
            nets_for_comp = net_map.get(ref, [])
            for node in nets_for_comp:
                net_name = node.get("net", "")
                if net_name:
                    xtal_nets.add(net_name)

    # Detect switch nodes: inductors whose footprint size >= min_inductor_mm on both axes
    # and which have one terminal net touching an IC pin
    switch_node_nets: set[str] = set()
    for comp in components:
        ref = comp.get("reference", "").upper()
        if not ref.startswith("L"):
            continue
        # Get footprint from component's uuid
        fp_uuid = comp.get("uuid", "")
        if not fp_uuid:
            continue
        fp = footprints.get(fp_uuid)
        if not fp:
            continue

        # Check footprint courtyard size: both x and y must be >= min_inductor_mm
        # For now, use pad positions as a proxy for footprint size
        pads = fp.get("pads", [])
        if len(pads) < 2:
            continue

        # Compute bounding box from pad positions
        if pads:
            xs = [p.get("position", {}).get("x", 0) for p in pads]
            ys = [p.get("position", {}).get("y", 0) for p in pads]
            bbox_width = max(xs) - min(xs) if xs else 0
            bbox_height = max(ys) - min(ys) if ys else 0

            # Both axes must exceed min_inductor_mm
            if bbox_width >= min_inductor_mm and bbox_height >= min_inductor_mm:
                # Check if any pad's net connects to an IC pin
                for pad in pads:
                    pad_net = pad.get("net", "")
                    if pad_net and pad_net in net_map:
                        # Check if this net touches any IC pins
                        for node in net_map[pad_net]:
                            node_ref = node.get("ref", "").upper()
                            if node_ref.startswith(("U", "IC", "Q")):
                                switch_node_nets.add(pad_net)

    # Build classification records
    critical_records: list[dict[str, Any]] = []
    seen_nets: set[str] = set()

    # Add bus nets
    for bus_type, nets_set in bus_nets_by_type.items():
        freq_mhz = bus_frequencies_mhz.get(bus_type, 0)
        l_crit = l_crit_table.get(bus_type, 0.0)
        for net_name in nets_set:
            if net_name not in seen_nets:
                # Estimate straight-line distance (placeholder for now)
                straight_line = _estimate_net_straight_line(net_name, net_map, comp_by_ref)
                stack_up_gate = straight_line >= (critical_fraction * l_crit) if l_crit > 0 else False
                critical_records.append({
                    "net": net_name,
                    "critical": True,
                    "reason": "bus_frequency",
                    "frequency_mhz": freq_mhz,
                    "l_crit_mm": l_crit,
                    "straight_line_mm": straight_line,
                    "stack_up_gate": stack_up_gate,
                    "multiplier": hs_length_weight,
                })
                seen_nets.add(net_name)

    # Add XTAL nets
    for net_name in xtal_nets:
        if net_name not in seen_nets:
            freq_mhz = bus_frequencies_mhz.get("CLK", 25)
            l_crit = l_crit_table.get("CLK", 0.0)
            straight_line = _estimate_net_straight_line(net_name, net_map, comp_by_ref)
            stack_up_gate = straight_line >= (critical_fraction * l_crit) if l_crit > 0 else False
            critical_records.append({
                "net": net_name,
                "critical": True,
                "reason": "xtal",
                "frequency_mhz": freq_mhz,
                "l_crit_mm": l_crit,
                "straight_line_mm": straight_line,
                "stack_up_gate": stack_up_gate,
                "multiplier": switch_length_weight,  # XTAL uses highest weight
            })
            seen_nets.add(net_name)

    # Add switch nodes
    for net_name in switch_node_nets:
        if net_name not in seen_nets:
            critical_records.append({
                "net": net_name,
                "critical": True,
                "reason": "switch_node",
                "frequency_mhz": 0,  # Not frequency-based
                "l_crit_mm": 0.0,
                "straight_line_mm": 0.0,
                "stack_up_gate": False,
                "multiplier": switch_length_weight,
            })
            seen_nets.add(net_name)

    return {
        "critical_nets": critical_records,
        "l_crit_table": l_crit_table,
        "settings_snapshot": {
            "high_speed": dict(high_speed_cfg),
            "switch_node": dict(switch_node_cfg),
        },
    }


def _estimate_net_straight_line(
    net_name: str,
    net_map: dict[str, list[dict[str, str]]],
    comp_by_ref: dict[str, dict[str, Any]],
) -> float:
    """Estimate the straight-line distance between connection points of a net.

    Uses the positions of components connected to this net. If only one or zero
    components connected, returns 0. If multiple, returns distance between the
    two most-distant connection points.
    """
    nodes = net_map.get(net_name, [])
    positions: list[tuple[float, float]] = []

    for node in nodes:
        ref = node.get("ref", "")
        if ref:
            comp = comp_by_ref.get(ref)
            if comp:
                pos = comp.get("position", {})
                x = pos.get("x", 0.0)
                y = pos.get("y", 0.0)
                positions.append((x, y))

    if len(positions) < 2:
        return 0.0

    # Find max distance between any two positions
    max_dist = 0.0
    for i in range(len(positions)):
        for j in range(i + 1, len(positions)):
            x1, y1 = positions[i]
            x2, y2 = positions[j]
            dist = math.sqrt((x2 - x1) ** 2 + (y2 - y1) ** 2)
            max_dist = max(max_dist, dist)

    return round(max_dist, 3)


def get_trace_cost(project_path: str | Path, net: str | None = None) -> dict[str, Any]:
    """Score routed copper with the Phase 6.2 cost model: length cost (copper
    length x `weights.length_mm`), via cost (via count x `weights.via` x the
    via's `via_weights` type multiplier - every via on this board is
    through-hole, so `via_weights.through` applies uniformly), and layer_span
    cost ((layers_used - 1) x `weights.layer_span`).

    Deviation term (Phase 5 unstub): when a net belongs to a detected, qualified
    bus bundle (via `detect_buses` + `measure_bus_corridor_areas`), the deviation
    cost is added per `trace_cost.deviation.metric`:
      - `mean_perp_distance` / `max_perp_distance` = mean/max perpendicular
        distance of the net's segment midpoints from the bundle centerline
        (`deviation.reference`: `bus_centerline` = the bundle's mean offset, or
        `straight_line` = the hub->dest pad axis); cost = `weights.deviation_mm`
        x value.
      - `excess_length` = `weights.excess_length` x max(actual_length /
        direct(hub->dest pad axis) - 1, 0).
    A net on several bundles (a shared SCK/MOSI) is measured against each and
    rolled up with the metric's aggregate (max for max_perp/excess_length,
    length-weighted mean for mean_perp). Such nets report `on_bus:true` plus a
    `bundle` object; non-bundle nets keep the stub behavior (`on_bus:false`,
    `bundle:null`, deviation = `trace_cost.non_bus_deviation`, default 0.0).

    Layer-purpose penalty (Phase 7.2): each net gets a `net_kind` ("power"
    when its name matches `layer_purpose.power_net_patterns`, else "signal"),
    its copper length is broken down per layer, and `layer_penalty` = sum over
    layers of length_on_layer x (`layer_purpose[net_kind][layer_type]` - 1) x
    `weights.length_mm` - the EXTRA cost existing copper incurs for sitting on
    wrong-purpose layers (a signal net crossing a `power` inner layer at the
    default 4.0 multiplier pays 3x length weight extra there; copper on the
    right-purpose layer, multiplier 1.0, adds nothing). Layers of unknown or
    `user` type get no penalty here - they're a router concern, not a cost.

    `net=None` returns every routed net ranked worst-cost-first, plus board
    totals and the `weights_used` block actually applied (so a result is
    self-describing/reproducible even as `pcb_settings.json` changes).
    """
    settings = load_pcb_settings(project_path)["config"]
    trace_cost_cfg = settings["trace_cost"]
    weights = trace_cost_cfg["weights"]
    via_weights = trace_cost_cfg["via_weights"]
    non_bus_deviation = float(trace_cost_cfg.get("non_bus_deviation", 0.0))
    through_via_weight = float(via_weights.get("through", 1.0))
    deviation_cfg = trace_cost_cfg.get("deviation", {})
    metric = deviation_cfg.get("metric", "mean_perp_distance")
    reference = deviation_cfg.get("reference", "bus_centerline")

    # Phase 7.2: layer purposes + per-net per-layer copper breakdown.
    board_path, _, _ = _resolve_project_path(project_path)
    layer_purpose_cfg = settings.get("layer_purpose", {})
    power_patterns = layer_purpose_cfg.get("power_net_patterns", [])
    layer_types = {layer["name"]: layer["type"] for layer in _parse_board_layers_cached(board_path)}
    tracks = _parse_tracks_cached(board_path)
    layer_lengths_by_net: dict[str, dict[str, float]] = {}
    for seg in tracks["segments"] + tracks["arcs"]:
        seg_net = seg["net"]
        if not seg_net:
            continue
        per_layer = layer_lengths_by_net.setdefault(seg_net, {})
        per_layer[seg["layer"]] = per_layer.get(seg["layer"], 0.0) + seg["length"]

    # Build net -> bundle memberships from every qualified detected bus. Failure
    # to detect (e.g. no netlist) degrades cleanly to the non-bus behavior.
    net_membership: dict[str, list[dict[str, Any]]] = {}
    try:
        detected = detect_buses(project_path)
    except Exception:  # pragma: no cover - defensive
        detected = {"candidates": []}
    for cand in detected.get("candidates", []):
        if not cand.get("qualified"):
            continue
        try:
            binfo = _compute_bus_bundles(project_path, bus=cand)
        except Exception:  # pragma: no cover - defensive
            continue
        if not binfo.get("grouped"):
            continue
        for bundle in binfo["bundles"]:
            role_by_net = {n["net"]: n["role"] for n in bundle["nets"]}
            for net_name, segs in bundle.get("_net_segs", {}).items():
                net_membership.setdefault(net_name, []).append(
                    {
                        "bus_type": binfo["bus_type"],
                        "hub_ic": binfo["hub_ic"],
                        "destination_ic": bundle["destination_ic"],
                        "role": role_by_net.get(net_name, "shared"),
                        "segs": segs,
                        "centerline_s": bundle["_centerline_s"],
                        "axis_len": bundle["_axis_len"],
                    }
                )

    width_data = get_net_track_widths(project_path)
    entries = width_data["nets"]

    # Phase 9: Load critical nets classification and build a lookup by net name
    critical_by_net: dict[str, dict[str, Any]] = {}
    try:
        critical_result = classify_critical_nets(project_path)
        for rec in critical_result.get("critical_nets", []):
            critical_by_net[rec["net"]] = rec
    except Exception:  # pragma: no cover - defensive
        pass

    def _deviation(net_name: str, length_mm: float) -> tuple[float, dict[str, Any] | None]:
        memberships = net_membership.get(net_name)
        if not memberships:
            return non_bus_deviation, None

        per_bundle: list[dict[str, Any]] = []
        for m in memberships:
            center = m["centerline_s"] if reference == "bus_centerline" else 0.0
            perp_vals = [(abs(s["s"] - center), s["length"]) for s in m["segs"]]
            net_len = sum(l for _, l in perp_vals) or 1.0
            b_mean = sum(v * l for v, l in perp_vals) / net_len
            b_max = max((v for v, _ in perp_vals), default=0.0)
            axl = m["axis_len"]
            excess = max(length_mm / axl - 1.0, 0.0) if axl > 1e-9 else 0.0
            per_bundle.append(
                {"destination_ic": m["destination_ic"], "mean": b_mean, "max": b_max, "excess": excess, "net_len": net_len}
            )

        if metric == "excess_length":
            value = max(b["excess"] for b in per_bundle)
            cost = weights["excess_length"] * value
        elif metric == "max_perp_distance":
            value = max(b["max"] for b in per_bundle)
            cost = weights["deviation_mm"] * value
        else:  # mean_perp_distance (default)
            wsum = sum(b["net_len"] for b in per_bundle) or 1.0
            value = sum(b["mean"] * b["net_len"] for b in per_bundle) / wsum
            cost = weights["deviation_mm"] * value

        roles = {m["role"] for m in memberships}
        bundle_obj = {
            "bus_type": memberships[0]["bus_type"],
            "hub_ic": memberships[0]["hub_ic"],
            "destinations": [m["destination_ic"] for m in memberships],
            "role": "shared" if "shared" in roles or len(memberships) > 1 else next(iter(roles)),
            "metric": metric,
            "reference": reference,
            "deviation_value": round(value, 4),
        }
        return cost, bundle_obj

    def build(entry: dict[str, Any]) -> dict[str, Any]:
        via_count = sum(entry.get("via_sizes", {}).values())
        length_mm = float(entry.get("total_length_mm", 0.0))
        layers_used = len(entry.get("layers", []) or [])

        # Phase 9: Apply critical net length multiplier if this net is critical
        net_name = entry["net"]
        critical_info = critical_by_net.get(net_name)
        length_multiplier = 1.0
        if critical_info:
            length_multiplier = float(critical_info.get("multiplier", 1.0))

        length_cost = weights["length_mm"] * length_mm * length_multiplier
        via_cost = weights["via"] * via_count * through_via_weight
        span_cost = weights["layer_span"] * max(0, layers_used - 1)
        deviation_cost, bundle_obj = _deviation(net_name, length_mm)

        # Phase 7.2 layer-purpose penalty: extra cost of copper on
        # wrong-purpose layers. Unknown/user layer types get no penalty.
        net_kind = _net_kind(net_name, power_net_patterns=power_patterns)
        kind_multipliers = layer_purpose_cfg.get(net_kind, {})
        per_layer = layer_lengths_by_net.get(net_name, {})
        layer_penalty = 0.0
        for layer_name, layer_len in per_layer.items():
            multiplier = kind_multipliers.get(layer_types.get(layer_name))
            if isinstance(multiplier, (int, float)) and not isinstance(multiplier, bool):
                layer_penalty += layer_len * (float(multiplier) - 1.0) * weights["length_mm"]

        total = length_cost + via_cost + span_cost + deviation_cost + layer_penalty

        result = {
            "net": net_name,
            "net_kind": net_kind,
            "on_bus": bundle_obj is not None,
            "bundle": bundle_obj,
            "metrics": {
                "length_mm": round(length_mm, 3),
                "via_count": via_count,
                "via_types": {"through": via_count} if via_count else {},
                "layers_used": layers_used,
                "layer_lengths_mm": {name: round(val, 3) for name, val in sorted(per_layer.items())},
            },
            "cost": {
                "length": round(length_cost, 3),
                "vias": round(via_cost, 3),
                "deviation": round(deviation_cost, 3),
                "layer_span": round(span_cost, 3),
                "layer_penalty": round(layer_penalty, 3),
                "total": round(total, 3),
            },
        }
        # Phase 9: Add critical net info if applicable
        if critical_info:
            result["critical"] = {
                "reason": critical_info.get("reason", ""),
                "multiplier": length_multiplier,
            }
        return result

    weights_used = {
        "length_mm": weights["length_mm"],
        "via": weights["via"],
        "deviation_mm": weights["deviation_mm"],
        "excess_length": weights["excess_length"],
        "layer_span": weights["layer_span"],
        "via_weights": dict(via_weights),
        "non_bus_deviation": non_bus_deviation,
        "layer_purpose": {
            "signal": dict(layer_purpose_cfg.get("signal", {})),
            "power": dict(layer_purpose_cfg.get("power", {})),
            "power_net_patterns": list(power_patterns),
        },
    }

    if net is not None:
        match = next((e for e in entries if e["net"] == net), None)
        if match is None:
            raise KeyError(f"Net {net!r} has no routed copper on the board")
        result = build(match)
        result["weights_used"] = weights_used
        return result

    ranked = [build(e) for e in entries]
    ranked.sort(key=lambda r: r["cost"]["total"], reverse=True)
    board_totals = {
        "length": round(sum(r["cost"]["length"] for r in ranked), 3),
        "vias": round(sum(r["cost"]["vias"] for r in ranked), 3),
        "deviation": round(sum(r["cost"]["deviation"] for r in ranked), 3),
        "layer_span": round(sum(r["cost"]["layer_span"] for r in ranked), 3),
        "layer_penalty": round(sum(r["cost"]["layer_penalty"] for r in ranked), 3),
        "total": round(sum(r["cost"]["total"] for r in ranked), 3),
    }
    return {
        "net_count": len(ranked),
        "nets": ranked,
        "board_totals": board_totals,
        "weights_used": weights_used,
    }


def _default_netclass(project_file: Path) -> dict[str, Any] | None:
    if not project_file.exists():
        return None
    try:
        pro_data = json.loads(project_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    classes = pro_data.get("net_settings", {}).get("classes", [])
    return next((c for c in classes if c.get("name") == "Default"), None)


def propose_netclass_from_nets(project_path: str | Path, nets: list[str], name: str) -> dict[str, Any]:
    """Propose a net-class definition (Phase 4a) from a confirmed net list (a
    bus candidate's members, or a hand-picked set) by pulling each net's
    Phase-1 width summary and deriving:
    - `track_width`: length-weighted dominant width across all member nets
      (each net's own dominant width weighted by that net's total routed
      length, so a long net's width dominates a short stub's).
    - `via_diameter`/`via_drill`: the most-used via size/drill across member
      nets; falls back to the project's `Default` netclass if no member net
      has any vias.
    - `clearance`: inherited from `Default` (this tool never invents one).

    Reports `conflicts` - member nets whose own dominant width differs from
    the proposed `track_width` - so the caller surfaces a choice to the user
    instead of silently averaging away real routing differences. Also
    includes the Phase-2 project-wide inventory so a caller can offer
    previously-used widths/vias as menu options (`AskUserQuestion`) rather
    than free-entry numbers.

    Nets with no routed copper are reported in `missing_nets` and excluded
    from the width/via aggregation (they simply have nothing to contribute).
    """
    board_path, project_file, _ = _resolve_project_path(project_path)

    width_by_net: dict[str, dict[str, Any]] = {}
    missing_nets: list[str] = []
    for member in nets:
        try:
            width_by_net[member] = get_net_track_widths(project_path, member)
        except KeyError:
            missing_nets.append(member)

    width_length_totals: dict[str, float] = {}
    via_counts: dict[str, int] = {}
    member_dominant_widths: dict[str, str | None] = {}
    for member, summary in width_by_net.items():
        dominant_width = summary.get("dominant_width")
        member_dominant_widths[member] = dominant_width
        if dominant_width is not None:
            length = float(summary.get("total_length_mm", 0.0))
            width_length_totals[dominant_width] = width_length_totals.get(dominant_width, 0.0) + length
        for via_key, count in (summary.get("via_sizes") or {}).items():
            via_counts[via_key] = via_counts.get(via_key, 0) + count

    proposed_width_key = None
    if width_length_totals:
        proposed_width_key = max(width_length_totals.items(), key=lambda kv: kv[1])[0]

    proposed_via_key = None
    if via_counts:
        proposed_via_key = max(via_counts.items(), key=lambda kv: kv[1])[0]

    default_class = _default_netclass(project_file)

    track_width: float | None
    if proposed_width_key is not None:
        track_width = float(proposed_width_key)
    elif default_class is not None and _is_number(str(default_class.get("track_width", ""))):
        track_width = float(default_class["track_width"])
    else:
        track_width = None

    if proposed_via_key is not None:
        via_size_str, via_drill_str = proposed_via_key.split("/", 1)
        via_diameter, via_drill = float(via_size_str), float(via_drill_str)
    elif default_class is not None:
        via_diameter = float(default_class.get("via_diameter", 0.0)) if _is_number(str(default_class.get("via_diameter", ""))) else None
        via_drill = float(default_class.get("via_drill", 0.0)) if _is_number(str(default_class.get("via_drill", ""))) else None
    else:
        via_diameter = via_drill = None

    clearance = float(default_class["clearance"]) if default_class is not None and _is_number(str(default_class.get("clearance", ""))) else None

    conflicts = [
        {"net": member, "dominant_width": dominant_width}
        for member, dominant_width in member_dominant_widths.items()
        if dominant_width is not None and proposed_width_key is not None and dominant_width != proposed_width_key
    ]

    inventory = get_project_track_inventory(project_path)

    return {
        "name": name,
        "nets": nets,
        "missing_nets": missing_nets,
        "proposed_settings": {
            "track_width": track_width,
            "via_diameter": via_diameter,
            "via_drill": via_drill,
            "clearance": clearance,
        },
        "conflicts": conflicts,
        "member_dominant_widths": member_dominant_widths,
        "via_counts_by_size": via_counts,
        "default_class_used_as_fallback": default_class is not None and (proposed_width_key is None or proposed_via_key is None),
        "inventory": inventory,
    }


def create_netclass(
    project_path: str | Path,
    name: str,
    settings: dict[str, Any],
    net_patterns: list[str],
    write: bool = False,
    allow_while_open: bool = False,
) -> dict[str, Any]:
    """Create a KiCad net class (Phase 4b) by editing `<project>.kicad_pro`
    JSON: appends a class object to `net_settings.classes` (copying the
    `Default` class's full key shape, then overriding `name`/`track_width`/
    `via_diameter`/`via_drill`/`clearance` from `settings` - every other key
    Default carries, e.g. `bus_width`/`diff_pair_*`/`microvia_*`/colors, is
    preserved verbatim so the new class looks native to KiCad), and adds one
    exact, regex-escaped, anchored pattern (`^<net>$`) per net in
    `net_patterns` to `net_settings.netclass_patterns`. Refuses if a class
    named `name` already exists (idempotent by refusal, never appends a
    duplicate).

    Defaults to a dry run (write=False) that returns a before/after diff of
    the affected `classes`/`netclass_patterns` JSON blocks plus the new class
    object, without touching disk. `write=True` saves with
    `json.dump(..., indent=2, sort_keys=True)` - verified byte-for-byte
    against a full round-trip of this project's own `.kicad_pro` (KiCad
    itself serializes net-settings objects with alphabetically-sorted keys),
    so the diff stays minimal on KiCad's next own save.

    IMPORTANT: KiCad only reloads net classes when the project is reopened -
    this write does not affect anything in a currently-open KiCad session
    until it reopens the project. It also only changes the *rules*: existing
    routed copper keeps whatever width/vias it already has until the net is
    re-routed or "Update Tracks/Vias from Netclass" is run in KiCad; creating
    a class does not retroactively resize any trace.

    Checks both the board file's and the project file's KiCad editor lock
    (`~<name>.lck`) before writing - the board's own lock is the well-known
    one, but a caller with the project open for net-class editing should not
    be silently overwritten either.
    """
    board_path, project_file, _ = _resolve_project_path(project_path)
    if not project_file.exists():
        raise FileNotFoundError(f"Project file not found: {project_file}")

    pro_data = json.loads(project_file.read_text(encoding="utf-8"))
    net_settings = pro_data.setdefault("net_settings", {})
    classes = net_settings.setdefault("classes", [])
    patterns = net_settings.setdefault("netclass_patterns", [])

    if any(c.get("name") == name for c in classes):
        raise ValueError(f"Netclass {name!r} already exists in {project_file.name} - refusing to append a duplicate")

    default_class = next((c for c in classes if c.get("name") == "Default"), None)
    if default_class is None:
        raise ValueError(f"No 'Default' netclass found in {project_file.name}'s net_settings.classes to copy shape from")

    new_class = copy.deepcopy(default_class)
    new_class["name"] = name
    for key in ("track_width", "via_diameter", "via_drill", "clearance"):
        if key in settings and settings[key] is not None:
            new_class[key] = settings[key]

    new_patterns = [{"pattern": f"^{re.escape(member)}$", "netclass": name} for member in net_patterns]

    before = {
        "net_settings.classes": copy.deepcopy(classes),
        "net_settings.netclass_patterns": copy.deepcopy(patterns),
    }
    updated_classes = classes + [new_class]
    updated_patterns = patterns + new_patterns
    after = {
        "net_settings.classes": updated_classes,
        "net_settings.netclass_patterns": updated_patterns,
    }

    result: dict[str, Any] = {
        "project_file": str(project_file),
        "write": write,
        "written": False,
        "name": name,
        "new_class": new_class,
        "new_patterns": new_patterns,
        "diff": {"before": before, "after": after},
    }

    if write:
        _check_not_locked_by_editor(board_path, allow_while_open)
        _check_not_locked_by_editor(project_file, allow_while_open)
        net_settings["classes"] = updated_classes
        net_settings["netclass_patterns"] = updated_patterns
        new_text = json.dumps(pro_data, indent=2, sort_keys=True) + "\n"
        with project_file.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(new_text)
        result["written"] = True

    return result


def audit_netclass_conformance(project_path: str | Path) -> dict[str, Any]:
    """Reconciliation report (Phase 4c): for each routed net, resolve which
    net class it's assigned to via `<project>.kicad_pro`'s
    `net_settings.netclass_patterns` (regex patterns, first match wins - the
    same precedence KiCad itself uses), falling back to `Default` for any net
    matched by no pattern. Compares that class's `track_width`/
    `via_diameter`/`via_drill` against the net's *actual* routed dominant
    values (Phase 1's `get_net_track_widths`) and reports a `mismatches` list
    per net, e.g. "net is in class SPI (0.2 mm) but routed at 0.3 mm."

    Read-only. A pattern referencing a netclass name absent from
    `net_settings.classes` is reported as a row-level `error` instead of a
    silent skip - a dangling pattern is itself a project-file defect worth
    surfacing.
    """
    board_path, project_file, _ = _resolve_project_path(project_path)
    pro_data: dict[str, Any] = {}
    if project_file.exists():
        try:
            pro_data = json.loads(project_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pro_data = {}

    net_settings = pro_data.get("net_settings", {})
    classes_by_name = {c.get("name"): c for c in net_settings.get("classes", [])}
    compiled_patterns: list[tuple[re.Pattern[str], str]] = []
    for entry in net_settings.get("netclass_patterns", []) or []:
        pattern_text = entry.get("pattern", "")
        netclass_name = entry.get("netclass", "")
        try:
            compiled_patterns.append((re.compile(pattern_text), netclass_name))
        except re.error:
            continue

    width_data = get_net_track_widths(project_path)

    rows: list[dict[str, Any]] = []
    mismatch_count = 0
    for entry in width_data["nets"]:
        net_name = entry["net"]
        assigned_class_name = "Default"
        for regex, netclass_name in compiled_patterns:
            if regex.search(net_name):
                assigned_class_name = netclass_name
                break

        class_def = classes_by_name.get(assigned_class_name)
        if class_def is None:
            rows.append(
                {
                    "net": net_name,
                    "assigned_class": assigned_class_name,
                    "error": (
                        f"netclass {assigned_class_name!r} is referenced by netclass_patterns but not "
                        "defined in net_settings.classes"
                    ),
                    "conforms": False,
                }
            )
            mismatch_count += 1
            continue

        actual_width: float | None = None
        dominant_width = entry.get("dominant_width")
        if dominant_width not in (None, "inherit") and _is_number(str(dominant_width)):
            actual_width = float(dominant_width)

        actual_via_diameter: float | None = None
        actual_via_drill: float | None = None
        via_sizes = entry.get("via_sizes") or {}
        if via_sizes:
            best_via_key = max(via_sizes.items(), key=lambda kv: kv[1])[0]
            via_size_str, via_drill_str = best_via_key.split("/", 1)
            actual_via_diameter = float(via_size_str)
            actual_via_drill = float(via_drill_str)

        class_width = float(class_def.get("track_width", 0)) if _is_number(str(class_def.get("track_width", ""))) else None
        class_via_diameter = float(class_def.get("via_diameter", 0)) if _is_number(str(class_def.get("via_diameter", ""))) else None
        class_via_drill = float(class_def.get("via_drill", 0)) if _is_number(str(class_def.get("via_drill", ""))) else None

        mismatches: list[str] = []
        if actual_width is not None and class_width is not None and abs(actual_width - class_width) > 1e-6:
            mismatches.append(
                f"track_width: class {assigned_class_name!r} specifies {class_width} mm but "
                f"{entry.get('segment_count', 0)} segment(s) are routed at {actual_width} mm"
            )
        if actual_via_diameter is not None and class_via_diameter is not None and abs(actual_via_diameter - class_via_diameter) > 1e-6:
            mismatches.append(
                f"via_diameter: class {assigned_class_name!r} specifies {class_via_diameter} mm but "
                f"the net's dominant via is {actual_via_diameter} mm"
            )
        if actual_via_drill is not None and class_via_drill is not None and abs(actual_via_drill - class_via_drill) > 1e-6:
            mismatches.append(
                f"via_drill: class {assigned_class_name!r} specifies {class_via_drill} mm but "
                f"the net's dominant via drill is {actual_via_drill} mm"
            )

        if mismatches:
            mismatch_count += 1
        rows.append(
            {
                "net": net_name,
                "assigned_class": assigned_class_name,
                "class_track_width": class_width,
                "actual_dominant_width": actual_width,
                "class_via_diameter": class_via_diameter,
                "actual_via_diameter": actual_via_diameter,
                "class_via_drill": class_via_drill,
                "actual_via_drill": actual_via_drill,
                "mismatches": mismatches,
                "conforms": not mismatches,
            }
        )

    return {
        "project_file": str(project_file),
        "net_count": len(rows),
        "mismatch_count": mismatch_count,
        "rows": rows,
    }


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
