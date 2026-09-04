"""Collapse a flat MCP tool surface into a five-tool search/batch facade.

The shape, and why it is this shape
-----------------------------------
Two constraints pull in opposite directions.

*Token cost* says expose as few tool schemas as possible: a server that
publishes 200 tools spends 40-60k tokens of every context window on schemas the
model will not use. That argues for one generic ``call(name, args)`` tool.

*Reliability* says the opposite. A generic dispatcher is one more layer for a
model to get wrong -- it has to guess a tool name it has never seen a schema
for, then guess that tool's arguments. Benchmarks of MCP-versus-CLI put the
completion gap on complex tasks at roughly 28 points, and indirection is where
that gap comes from.

So every affordance here exists to pay the token saving back in reliability:

* :func:`find` never returns a bare name. It returns a rendered signature and a
  ready-to-paste ``call`` example, so the next step needs no invention.
* ``call`` treats a wrong name as a *search query*, not an error -- an unknown
  name comes back with ranked suggestions instead of a failure.
* ``call`` treats a wrong argument as a *schema request* -- the error carries
  the parameter list, so the retry has what it needs.
* Arguments are coerced, not rejected: ``"3"`` for an int, ``"true"`` for a
  bool, a JSON string for an object. The CLI's forgiveness is the point.

Output is shaped for a reader, not a parser: one line per tool, no wrapper
objects, nulls and empty fields omitted. A 200-tool index costs about the same
as three raw schemas.
"""

from __future__ import annotations

import inspect
import json
import math
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Iterable, Optional, Sequence

#: Rendered instead of the JSON Schema spelling. Every character here is paid
#: for once per tool in every ``find`` result, so they are kept to three or four.
_TYPE_NAMES = {
    "integer": "int",
    "number": "num",
    "string": "str",
    "boolean": "bool",
    "object": "obj",
    "array": "arr",
    "null": "null",
}

_WORD_RE = re.compile(r"[a-z0-9]+")

#: Dropped from a *query* before scoring (never from the index -- a tool named
#: `get_all_*` still indexes "all"). Queries here are written as questions --
#: "what is connected to U10", "how do I flash the pico" -- and these carry no
#: signal while doing real damage: a single curated keyword like "what" would
#: otherwise score at full keyword weight and beat the tool the question is
#: actually about. Kept deliberately short; anything domain-bearing stays.
_QUERY_STOPWORDS = frozenset({
    "a", "an", "and", "any", "are", "as", "at", "be", "by", "can", "do", "does",
    "for", "from", "get", "how", "i", "if", "in", "is", "it", "its", "me", "my",
    "of", "on", "or", "should", "so", "that", "the", "then", "there", "this",
    "to", "was", "what", "when", "where", "which", "why", "will", "with", "you",
})


def _tokens(text: str) -> "list[str]":
    return _WORD_RE.findall((text or "").lower())


# ---------------------------------------------------------------------------
# schema rendering
# ---------------------------------------------------------------------------
def _resolve(schema: dict, root: dict) -> dict:
    """Follow a single ``$ref`` into the schema's ``$defs``.

    Pydantic emits a ``$ref`` for any parameter typed with a model or enum. One
    hop is enough: these servers have no nested model parameters, and a loop
    guard is cheaper than a full resolver we would have to maintain.
    """
    seen = 0
    while isinstance(schema, dict) and "$ref" in schema and seen < 8:
        ref = schema["$ref"]
        if not isinstance(ref, str) or not ref.startswith("#/$defs/"):
            break
        schema = (root.get("$defs") or {}).get(ref.split("/")[-1], {})
        seen += 1
    return schema if isinstance(schema, dict) else {}


def _type_names(schema: dict, root: dict) -> "list[str]":
    """JSON-Schema type keywords for one parameter, ``null`` stripped.

    ``Optional[int]`` becomes ``anyOf: [integer, null]``; the ``null`` arm says
    nothing a reader does not already learn from the parameter being optional,
    so it is dropped unless it is the only arm.
    """
    schema = _resolve(schema, root)
    out: "list[str]" = []
    for arm in (schema.get("anyOf") or schema.get("oneOf") or []):
        out.extend(_type_names(arm, root))
    raw = schema.get("type")
    if isinstance(raw, str):
        out.append(raw)
    elif isinstance(raw, list):
        out.extend(t for t in raw if isinstance(t, str))
    if "enum" in schema and not out:
        out.append("string")
    deduped = [t for i, t in enumerate(out) if t not in out[:i]]
    non_null = [t for t in deduped if t != "null"]
    return non_null or deduped


def _render_type(schema: dict, root: dict) -> str:
    names = _type_names(schema, root)
    if not names:
        return "any"
    return "|".join(_TYPE_NAMES.get(n, n) for n in names)


def _enum_values(schema: dict, root: dict) -> "list[Any]":
    schema = _resolve(schema, root)
    if "enum" in schema:
        return list(schema["enum"])
    for arm in (schema.get("anyOf") or schema.get("oneOf") or []):
        values = _enum_values(arm, root)
        if values:
            return values
    return []


def _fmt_default(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return repr(value)
    if value is None:
        return "null"
    return json.dumps(value, separators=(",", ":"))


def render_signature(schema: dict, *, max_params: int = 8) -> str:
    """``(channel:int, offset_c:num)`` -- required first, defaults shown.

    Truncated past ``max_params`` with a ``+N`` marker: the point of a signature
    in a search hit is to confirm the tool is the right one, and ``describe``
    is one call away when the full parameter list actually matters.
    """
    schema = schema or {}
    props = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    ordered = [n for n in props if n in required] + [n for n in props if n not in required]
    parts: "list[str]" = []
    for name in ordered[:max_params]:
        spec = props[name] or {}
        rendered = f"{name}:{_render_type(spec, schema)}"
        if name not in required and "default" in spec:
            rendered += f"={_fmt_default(spec['default'])}"
        parts.append(rendered)
    if len(ordered) > max_params:
        parts.append(f"+{len(ordered) - max_params}")
    return "(" + ", ".join(parts) + ")"


def _summarize(doc: str, limit: int = 130) -> str:
    """First sentence of a docstring, collapsed onto one line."""
    text = " ".join((doc or "").split())
    if not text:
        return ""
    match = re.search(r"(?<=[.!?])\s", text)
    if match and match.start() + 1 <= limit:
        text = text[: match.start() + 1]
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "..."
    return text


def _example_call(prefix: str, name: str, schema: dict) -> str:
    """A paste-ready ``call`` line built from the tool's own required params.

    Placeholders are the rendered type, not a plausible value: a wrong-looking
    ``<int>`` gets replaced, whereas a plausible-looking ``0`` gets sent.
    """
    schema = schema or {}
    props = schema.get("properties") or {}
    required = [n for n in props if n in set(schema.get("required") or [])]
    args = {n: f"<{_render_type(props[n] or {}, schema)}>" for n in required[:4]}
    body = json.dumps(args, separators=(",", ":")) if args else "{}"
    return f'{prefix}call(name="{name}", args={body})'


# ---------------------------------------------------------------------------
# staleness / freshness
# ---------------------------------------------------------------------------
# These servers are long-running HTTP processes an editor button starts and
# stops (tools/PcTools/scripts/mcp_servers.ps1) -- a source edit made while one
# is up does not reach it until the next restart. That has twice cost a real
# debugging session: a fix landed on disk, the server kept answering with the
# old in-memory code, and the still-present symptom read as "the fix didn't
# work" rather than "the process serving it is stale". A snapshot taken once
# at startup, re-checked cheaply against the same file list, makes the process
# say so itself instead.
_STALE_EXCLUDE_DIRS = frozenset({
    "__pycache__", ".venv", "venv", ".git", "logs", "node_modules", ".pytest_cache",
})
_STALE_SOURCE_EXTS = (".py", ".html", ".css", ".js")


def _iter_source_files(root: str, *, exclude_dirs: "Iterable[str]" = _STALE_EXCLUDE_DIRS,
                       extensions: "Sequence[str]" = _STALE_SOURCE_EXTS) -> "Iterable[str]":
    exclude = set(exclude_dirs)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in exclude]
        for filename in filenames:
            if extensions and not filename.endswith(tuple(extensions)):
                continue
            yield os.path.join(dirpath, filename)


def _git_head_short(root: str) -> str:
    """``git rev-parse --short HEAD`` at ``root``, with a graceful fallback --
    a server whose /health someone is staring at mid-incident must never
    crash on this."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=root,
            capture_output=True, text=True, timeout=5,
        )
    except Exception:  # noqa: BLE001 - git missing/unavailable is not fatal here
        return "unknown (git unavailable)"
    if out.returncode != 0 or not out.stdout.strip():
        return "unknown (git unavailable)"
    return out.stdout.strip()


@dataclass
class SourceSnapshot:
    """What the server's own source tree looked like the moment it started
    serving -- what staleness is measured against for the life of the process."""

    root: str
    started_at: float
    commit: str
    #: path -> mtime at snapshot time. Cached once at startup; re-stat'd on
    #: demand at check time rather than re-walking the tree on every call.
    files: "dict[str, float]" = field(default_factory=dict)

    def started_at_human(self) -> str:
        return datetime.fromtimestamp(self.started_at).strftime("%Y-%m-%d %H:%M:%S")


def take_snapshot(root: str, *, mtime_provider: "Optional[Callable[[str], float]]" = None,
                  commit: "Optional[str]" = None) -> SourceSnapshot:
    """Record the newest-mtime baseline for ``root`` at server startup.

    ``mtime_provider`` defaults to ``os.path.getmtime`` and exists so tests
    can inject a fake clock instead of depending on real filesystem timing.
    """
    getmtime = mtime_provider or os.path.getmtime
    files: "dict[str, float]" = {}
    for path in _iter_source_files(root):
        try:
            files[path] = getmtime(path)
        except OSError:
            continue
    return SourceSnapshot(
        root=root, started_at=time.time(),
        commit=commit if commit is not None else _git_head_short(root),
        files=files,
    )


def check_staleness(snapshot: SourceSnapshot, *,
                    mtime_provider: "Optional[Callable[[str], float]]" = None) -> "tuple[bool, int]":
    """Re-stat the snapshot's cached file list. Returns ``(stale, changed_count)``.

    A file is "changed" if its current mtime is newer than what was recorded
    at snapshot time, or if it has been deleted out from under the snapshot
    (a rename/move counts as both a deletion and, for the new path, a file
    the snapshot never knew about -- either way the server's in-memory code no
    longer matches what's on disk). Nothing here rescans the tree for files
    added *since* startup: a brand-new file cannot itself be why an existing
    process's behaviour is stale, since it never imported it.
    """
    getmtime = mtime_provider or os.path.getmtime
    changed = 0
    for path, recorded_mtime in snapshot.files.items():
        try:
            current_mtime = getmtime(path)
        except OSError:
            changed += 1  # deleted (or otherwise unreadable) since startup
            continue
        if current_mtime > recorded_mtime:
            changed += 1
    return changed > 0, changed


def freshness_line(snapshot: "Optional[SourceSnapshot]", *,
                   mtime_provider: "Optional[Callable[[str], float]]" = None) -> str:
    """The one line surfaced in ``*_help()`` output -- loud when stale, quiet
    when not, so freshness is checkable at a glance either way."""
    if snapshot is None:
        return ""
    stale, changed = check_staleness(snapshot, mtime_provider=mtime_provider)
    started = snapshot.started_at_human()
    if stale:
        plural = "" if changed == 1 else "s"
        return (
            f"SERVER CODE IS STALE: {changed} file{plural} changed since this process "
            f"started (started {started}, at commit {snapshot.commit}). "
            "Restart via mcp_servers.ps1 restart."
        )
    return f"server code fresh: started {started}, at commit {snapshot.commit}"


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------
@dataclass
class ToolEntry:
    """One tool that was registered on the server but is no longer published."""

    name: str
    group: str
    doc: str
    schema: dict
    fn: Callable[..., Any]
    keywords: "tuple[str, ...]" = ()
    summary: str = ""
    guarded: bool = False
    #: Search-index tokens, weighted by where they came from. Built once at
    #: collapse time -- ``find`` runs on every model turn and must not tokenize
    #: 200 docstrings to answer.
    index: "dict[str, float]" = field(default_factory=dict)

    def signature(self) -> str:
        return render_signature(self.schema)

    def brief(self) -> str:
        mark = "!" if self.guarded else ""
        head = f"{self.name}{self.signature()} [{self.group}]{mark}"
        return f"{head} {self.summary}".rstrip()


def _build_index(entry: ToolEntry) -> "dict[str, float]":
    weights: "dict[str, float]" = {}

    def add(text: str, weight: float) -> None:
        for token in _tokens(text):
            weights[token] = max(weights.get(token, 0.0), weight)

    add(entry.name, 4.0)
    add(entry.group, 3.0)
    add(" ".join(entry.keywords), 3.0)
    add(entry.summary, 1.5)
    add(" ".join((entry.schema.get("properties") or {}).keys()), 1.0)
    add(entry.doc, 0.6)
    return weights


class ToolRegistry:
    """The collapsed tool surface, plus the search that reaches back into it."""

    #: A hit must score at least this fraction of the top hit to be returned.
    #: Tuned by hand against the two servers' real tool sets: 0.25 keeps the
    #: genuine near-misses (which is what makes a typo recoverable) and drops
    #: the single-common-token tail.
    RELEVANCE_FLOOR = 0.25

    def __init__(self, prefix: str, entries: "list[ToolEntry]",
                 synonyms: "Optional[dict[str, Sequence[str]]]" = None):
        self.prefix = prefix
        self.entries = entries
        self.by_name = {e.name: e for e in entries}
        self.synonyms = {k.lower(): tuple(v) for k, v in (synonyms or {}).items()}
        self.groups: "dict[str, list[ToolEntry]]" = {}
        for entry in entries:
            self.groups.setdefault(entry.group, []).append(entry)
        # Inverse document frequency over the same weighted index `search`
        # scores against, so a query token like "read" (in 30 tools) cannot
        # outrank "autotune" (in 4).
        total = max(len(entries), 1)
        counts: "dict[str, int]" = {}
        for entry in entries:
            for token in entry.index:
                counts[token] = counts.get(token, 0) + 1
        self._idf = {token: math.log(1.0 + total / count) for token, count in counts.items()}
        self._default_idf = math.log(1.0 + total)
        #: Set by ``collapse``/``collapse_table`` when a ``source_root`` is
        #: given; ``None`` means "freshness not tracked" (e.g. in tests that
        #: build a registry directly), and ``facade_help`` treats that as
        #: nothing to say rather than an error.
        self.freshness: "Optional[SourceSnapshot]" = None

    # -- search ------------------------------------------------------------
    def _expand(self, query: str) -> "list[str]":
        """Query tokens, stopwords dropped and synonyms folded in.

        Stopwords go before synonym expansion, and the whole query survives if
        it turns out to be nothing but stopwords -- a search for "how to" is
        better answered badly than not at all.
        """
        tokens = _tokens(query)
        meaningful = [t for t in tokens if t not in _QUERY_STOPWORDS]
        out: "list[str]" = []
        for token in (meaningful or tokens):
            out.append(token)
            out.extend(self.synonyms.get(token, ()))
        return [t for i, t in enumerate(out) if t not in out[:i]]

    def score(self, entry: ToolEntry, query: str, tokens: "Sequence[str]") -> float:
        if not tokens:
            return 0.0
        score = 0.0
        for token in tokens:
            idf = self._idf.get(token, self._default_idf)
            weight = entry.index.get(token)
            if weight is not None:
                score += weight * idf
                continue
            # Prefix hit: "therm" should still find thermo_read. Worth half an
            # exact token so it never outranks a real match. Both sides must be
            # four characters or more -- otherwise every English stopword in a
            # docstring ("the") prefix-matches a real query term ("thermo").
            if len(token) < 4:
                continue
            for indexed, indexed_weight in entry.index.items():
                if len(indexed) < 4:
                    continue
                if indexed.startswith(token) or token.startswith(indexed):
                    score += 0.5 * indexed_weight * idf
                    break
        flat = query.strip().lower()
        if flat == entry.name.lower():
            score += 1000.0
        elif flat and flat in entry.name.lower():
            score += 40.0
        return score

    def search(self, query: str, *, group: Optional[str] = None, limit: int = 12) -> "list[ToolEntry]":
        pool = self.entries
        if group:
            wanted = group.strip().lower()
            pool = [e for e in pool if e.group == wanted]
            if not pool:
                return []
        if not (query or "").strip():
            # An empty query is a browse, not a search: return the group in a
            # stable order rather than an arbitrary zero-score slice.
            return sorted(pool, key=lambda e: e.name)[:limit]
        tokens = self._expand(query)
        scored = [(self.score(e, query, tokens), e) for e in pool]
        scored = [pair for pair in scored if pair[0] > 0]
        if not scored:
            return []
        scored.sort(key=lambda pair: (-pair[0], pair[1].name))
        # Relevance floor. Without it a query's long tail of one-weak-token
        # matches pads every result to `limit`, and a padded list reads as
        # "these are all plausible" when only the first two are.
        floor = scored[0][0] * self.RELEVANCE_FLOOR
        return [entry for score, entry in scored[:limit] if score >= floor]

    def suggest(self, name: str, limit: int = 6) -> "list[str]":
        """Ranked near-misses for a name that did not resolve."""
        return [e.name for e in self.search(name.replace("_", " "), limit=limit)]

    # -- invocation --------------------------------------------------------
    def coerce(self, entry: ToolEntry, args: "dict[str, Any]") -> "dict[str, Any]":
        """Best-effort JSON-type repair, so a stringly-typed call still lands."""
        props = entry.schema.get("properties") or {}
        out: "dict[str, Any]" = {}
        for key, value in args.items():
            spec = props.get(key)
            out[key] = value if spec is None else _coerce_value(value, _type_names(spec, entry.schema))
        return out

    def invoke(self, name: str, args: "dict[str, Any]") -> str:
        """Dispatch one tool, answering every failure with what to try next."""
        entry = self.by_name.get(name)
        if entry is None:
            suggestions = self.suggest(name)
            hint = ", ".join(suggestions) if suggestions else "(no close matches)"
            query = name.replace("_", " ")
            return (
                f"error: no tool named {name!r}. Closest: {hint}\n"
                f'Run {self.prefix}find(query="{query}") for signatures.'
            )
        props = entry.schema.get("properties") or {}
        missing = [n for n in (entry.schema.get("required") or []) if n not in args]
        unknown = [k for k in args if k not in props]
        if missing or unknown:
            problems = []
            if missing:
                problems.append("missing required: " + ", ".join(missing))
            if unknown:
                problems.append("unknown: " + ", ".join(unknown))
            return (
                f"error: bad arguments for {name}: " + "; ".join(problems) + "\n"
                f"signature: {name}{entry.signature()}"
            )
        try:
            coerced = self.coerce(entry, args)
        except (TypeError, ValueError) as exc:
            return (
                f"error: could not coerce arguments for {name}: {exc}\n"
                f"signature: {name}{entry.signature()}"
            )
        result = entry.fn(**coerced)
        if inspect.isawaitable(result):  # pragma: no cover - no async tools today
            raise TypeError(f"{name} is async; the facade only dispatches sync tools")
        return result if isinstance(result, str) else json.dumps(result, default=str)


_TRUE = {"true", "1", "yes", "on", "high"}
_FALSE = {"false", "0", "no", "off", "low"}


def _coerce_value(value: Any, types: "Sequence[str]") -> Any:
    if not types or value is None:
        return value
    if "boolean" in types and isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in _TRUE:
            return True
        if lowered in _FALSE:
            return False
    if "integer" in types and isinstance(value, str) and value.strip():
        # base 0, so "0x1f" -- how every register address in this project is
        # written -- survives the trip through a JSON string.
        return int(value.strip(), 0)
    if "integer" in types and isinstance(value, float) and value.is_integer():
        return int(value)
    if "number" in types and "integer" not in types and isinstance(value, str) and value.strip():
        return float(value.strip())
    if ("object" in types or "array" in types) and isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith(("{", "[")):
            return json.loads(stripped)
    if "string" in types and isinstance(value, (int, float)) and not isinstance(value, bool):
        if "integer" not in types and "number" not in types:
            return str(value)
    return value


# ---------------------------------------------------------------------------
# grouping
# ---------------------------------------------------------------------------
def derive_group(name: str, prefixes: "Sequence[tuple[str, str]]", overrides: "dict[str, str]") -> str:
    """Longest-prefix match, with an exact-name override table on top."""
    if name in overrides:
        return overrides[name]
    best = ""
    group = ""
    for prefix, candidate in prefixes:
        if name.startswith(prefix) and len(prefix) > len(best):
            best, group = prefix, candidate
    if group:
        return group
    return name.split("_", 1)[0] if "_" in name else "misc"


# ---------------------------------------------------------------------------
# collapse
# ---------------------------------------------------------------------------
_INSTRUCTIONS = """\
{title}

This server publishes {facade} tools, not its full surface: {count} hardware
tools live behind them so their schemas do not have to be loaded up front.

ALWAYS start with {p}help() or {p}find(query=...) -- never conclude a capability
is missing because you cannot see a tool for it. Everything the {label} hardware
can do is reachable through {p}call and {p}batch.

  {p}help()                        groups, counts, the common recipes
  {p}find(query="thermocouple")    ranked signatures + a paste-ready call
  {p}describe(names=["a","b"])     full schemas for named tools
  {p}call(name="x", args={{...}})    invoke one
  {p}batch(calls=[{{...}}, ...])     invoke several in one round trip

{p}batch is the preferred form for any sequence of two or more hardware
operations: one round trip instead of N, and it stops at the first failure so a
half-applied sequence is visible rather than silent.

A wrong guess is not fatal -- {p}call answers an unknown name with ranked
suggestions, and a bad argument with the tool's signature.
"""


def collapse(
    mcp: Any,
    *,
    prefix: str,
    label: str,
    title: str,
    group_prefixes: "Sequence[tuple[str, str]]" = (),
    group_overrides: "Optional[dict[str, str]]" = None,
    keywords: "Optional[dict[str, Sequence[str]]]" = None,
    synonyms: "Optional[dict[str, Sequence[str]]]" = None,
    keep: "Sequence[str]" = (),
    recipes: str = "",
    source_root: "Optional[str]" = None,
) -> ToolRegistry:
    """Withdraw every registered tool from the wire and publish the facade.

    ``source_root`` -- when given, a startup :class:`SourceSnapshot` of that
    directory is taken and attached to the registry, and ``{prefix}help()``
    prepends a freshness line (loud when the running process has drifted from
    the source on disk, quiet when it hasn't). Omit it for servers that don't
    need this (or in tests building a registry directly).

    Called once, after the module's ``@_tool()`` definitions have run. The tool
    functions themselves are untouched -- they stay importable module globals
    (the unit tests call them directly) and stay reachable through
    :meth:`ToolRegistry.invoke`. Only their *advertisement* is withdrawn.

    ``keep`` names tools that stay directly published. Keep it short: each one
    costs its full schema in every context window. It earns its place only if
    the model needs it before it has any reason to search -- ``connect`` is the
    example, since nothing else works until the link is up.
    """
    manager = mcp._tool_manager
    published = dict(manager._tools)
    overrides = dict(group_overrides or {})
    keyword_map = {k: tuple(v) for k, v in (keywords or {}).items()}

    entries: "list[ToolEntry]" = []
    for name, tool in published.items():
        doc = tool.description or ""
        schema = dict(tool.parameters or {})
        entry = ToolEntry(
            name=name,
            group=derive_group(name, group_prefixes, overrides),
            doc=doc,
            schema=schema,
            fn=tool.fn,
            keywords=keyword_map.get(name, ()),
            summary=_summarize(doc),
            guarded="confirm" in (schema.get("properties") or {}),
        )
        entry.index = _build_index(entry)
        entries.append(entry)

    registry = ToolRegistry(prefix, entries, dict(synonyms or {}))
    if source_root is not None:
        registry.freshness = take_snapshot(source_root)

    kept = sorted(set(keep) & set(published))
    for name in published:
        if name not in kept:
            manager.remove_tool(name)

    _register_facade(mcp, registry, label=label, recipes=recipes, kept=kept)

    # `instructions` is read-only on MCPServer but a plain attribute one level
    # down; this is the only channel that reaches the client before it has
    # called anything, which is exactly when "search, don't assume" has to land.
    mcp._lowlevel_server.instructions = _INSTRUCTIONS.format(
        title=title,
        facade=5 + len(kept),
        count=len(entries),
        p=prefix,
        label=label,
    )
    return registry


# ---------------------------------------------------------------------------
# the facade, as plain functions
# ---------------------------------------------------------------------------
# Two servers with completely different plumbing publish these: the PcTools
# servers go through MCPServer's decorator (which builds a schema by
# introspecting a typed wrapper), while mykicadMcp hand-rolls its own JSON-RPC
# loop over a table of {description, inputSchema, handler} dicts. So the five
# implementations take a registry as their first argument and know nothing
# about either -- `collapse` and `collapse_table` are the two adapters.


def facade_help(registry: "ToolRegistry", *, label: str, recipes: str, kept: "Sequence[str]",
                topic: "Optional[str]" = None) -> str:
    prefix = registry.prefix
    fresh_line = freshness_line(registry.freshness)
    if topic:
        group = topic.strip().lower()
        entries = registry.groups.get(group)
        if not entries:
            known = ", ".join(sorted(registry.groups))
            return f"error: no group {topic!r}. Groups: {known}"
        body = "\n".join(e.brief() for e in sorted(entries, key=lambda e: e.name))
        prefix_lines = f"{fresh_line}\n\n" if fresh_line else ""
        return f"{prefix_lines}group {group} ({len(entries)} tools)\n{body}"

    rows = sorted(registry.groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    table = "\n".join(
        f"  {name:<14} {len(items):>3}  "
        + ", ".join(sorted(e.name for e in items)[:4])
        + (" ..." if len(items) > 4 else "")
        for name, items in rows
    )
    parts = []
    if fresh_line:
        parts += [fresh_line, ""]
    parts += [
        f"{label}: {len(registry.entries)} tools in {len(registry.groups)} groups, reached "
        f"via {prefix}find / {prefix}call / {prefix}batch. A trailing '!' marks a tool that "
        f"takes a confirm flag.",
        "",
        "GROUPS (name, count, sample)",
        table,
    ]
    if kept:
        parts += ["", "Published directly (no lookup needed): " + ", ".join(kept)]
    if recipes:
        parts += ["", "RECIPES", recipes.rstrip()]
    parts += [
        "",
        f'NEXT: {prefix}help(topic="<group>") for a full group listing, or '
        f'{prefix}find(query="<what you want to do>").',
    ]
    return "\n".join(parts)


def facade_find(registry: "ToolRegistry", *, query: str = "", group: "Optional[str]" = None,
                limit: int = 12, detail: str = "brief") -> str:
    prefix = registry.prefix
    if limit < 1:
        return "error: limit must be >= 1"
    hits = registry.search(query, group=group, limit=min(limit, 50))
    if not hits:
        known = ", ".join(sorted(registry.groups))
        scope = f" in group {group!r}" if group else ""
        return (
            f"no match for {query!r}{scope}\n"
            f"Groups: {known}\n"
            f"Try a broader word, or {prefix}help()."
        )
    if detail == "full":
        return "\n\n".join(_describe_entry(e, prefix) for e in hits)
    body = "\n".join(e.brief() for e in hits)
    return f"{body}\n\nnext: {_example_call(prefix, hits[0].name, hits[0].schema)}"


def facade_describe(registry: "ToolRegistry", *, names: Any, doc_chars: int = 1200) -> str:
    prefix = registry.prefix
    wanted = _as_name_list(names)
    if not wanted:
        sample = registry.entries[0].name if registry.entries else "<tool>"
        return f'error: names is empty. Example: {prefix}describe(names=["{sample}"])'
    blocks = []
    for name in wanted[:25]:
        entry = registry.by_name.get(name)
        if entry is None:
            hint = ", ".join(registry.suggest(name)) or "(no close matches)"
            blocks.append(f"{name}: error: unknown. Closest: {hint}")
        else:
            blocks.append(_describe_entry(entry, prefix, doc_chars=doc_chars))
    return "\n\n".join(blocks)


def facade_call(registry: "ToolRegistry", *, name: str, args: Any = None) -> str:
    try:
        parsed = _as_args(args)
    except ValueError as exc:
        return f"error: {exc}"
    return registry.invoke(name, parsed)


def facade_batch(registry: "ToolRegistry", *, calls: Any, stop_on_error: bool = True) -> str:
    prefix = registry.prefix
    sample = registry.entries[0].name if registry.entries else "<tool>"
    try:
        steps = _as_calls(calls)
    except ValueError as exc:
        return (
            f"error: {exc}\n"
            f'expected: {prefix}batch(calls=[{{"name":"{sample}","args":{{}}}}, ...])'
        )
    if not steps:
        return "error: calls is empty"
    out: "list[str]" = []
    failed = 0
    for index, (name, args) in enumerate(steps, start=1):
        result = registry.invoke(name, args)
        bad = result.lstrip().startswith("error:")
        failed += bad
        out.append(f"[{index}] {'ERR' if bad else 'ok'} {name}\n{result}".rstrip())
        if bad and stop_on_error:
            skipped = len(steps) - index
            out.append(f"[stopped at step {index} of {len(steps)}; {skipped} not run]")
            out.append(f"-- {index - failed} ok, {failed} failed, {skipped} skipped")
            return "\n".join(out)
    out.append(f"-- {len(steps) - failed} ok, {failed} failed")
    return "\n".join(out)


def facade_descriptions(prefix: str, label: str) -> "dict[str, str]":
    """The five tool descriptions.

    These are the *only* schemas a client ever sees for a collapsed server, so
    their wording is load-bearing: it is the entire basis on which a model
    decides to search rather than to give up. Built from ``label``/``prefix``
    rather than written per server so all three read identically.
    """
    return {
        "help": (
            f"START HERE for anything involving the {label}. Lists every tool group behind "
            f"this server's search facade, with counts and the common multi-step recipes. "
            f"Pass `topic` (a group name) to list that group in full."
        ),
        "find": (
            f"Search the {label} tool set by intent, name, or domain noun; returns ranked "
            f'signatures. detail="full" adds each hit\'s full documentation. An empty query '
            f"with a group lists that whole group. Every hit is directly callable via "
            f"{prefix}call."
        ),
        "describe": (
            f"Full schema and documentation for one or more {label} tools, in a single round "
            f"trip. `names` is a list of tool names (a comma-separated string, or a single "
            f"bare name, also works)."
        ),
        "call": (
            f"Invoke one {label} tool by name. `args` is that tool's argument object (omit it "
            f"for a no-argument tool). An unknown name comes back with ranked suggestions and "
            f"a bad argument with the tool's signature, so a first guess is safe to make."
        ),
        "batch": (
            f"Invoke several {label} tools in one round trip -- the preferred form for any "
            f'sequence of two or more operations. `calls` is a list of {{"name": ..., "args": '
            f'{{...}}}} objects, run in order. Stops at the first failure unless '
            f"stop_on_error=false."
        ),
    }


#: JSON Schemas for the five facade tools, for servers that publish a schema
#: table directly instead of having one introspected from a Python signature.
#: `args` and `calls` are deliberately untyped (`describe`'s `names` too): they
#: carry another tool's arbitrary argument object, and a stricter schema here
#: would reject valid payloads for no benefit -- the real validation happens in
#: `ToolRegistry.invoke` against the target tool's own schema.
FACADE_SCHEMAS: "dict[str, dict]" = {
    "help": {
        "type": "object",
        "properties": {"topic": {"type": "string", "description": "Group name to list in full."}},
    },
    "find": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What you are trying to do, in plain words."},
            "group": {"type": "string", "description": "Restrict to one group."},
            "limit": {"type": "integer", "default": 12},
            "detail": {"type": "string", "enum": ["brief", "full"], "default": "brief"},
        },
    },
    "describe": {
        "type": "object",
        "properties": {
            "names": {"description": "Tool name, list of names, or comma-separated string."},
            "doc_chars": {"type": "integer", "default": 1200},
        },
        "required": ["names"],
    },
    "call": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Exact tool name."},
            "args": {"description": "That tool's argument object. Omit for a no-argument tool."},
        },
        "required": ["name"],
    },
    "batch": {
        "type": "object",
        "properties": {
            "calls": {"description": 'List of {"name": ..., "args": {...}} objects, run in order.'},
            "stop_on_error": {"type": "boolean", "default": True},
        },
        "required": ["calls"],
    },
}


def _register_facade(mcp: Any, registry: ToolRegistry, *, label: str, recipes: str,
                     kept: "Sequence[str]") -> None:
    """Publish the five facade tools on an ``MCPServer``.

    The wrappers below exist only to give the decorator a typed signature to
    build a schema from; every one of them immediately delegates to the plain
    ``facade_*`` function above, which is what ``collapse_table`` also calls.
    """
    prefix = registry.prefix
    described = facade_descriptions(prefix, label)

    @mcp.tool(name=f"{prefix}help", description=described["help"])
    def _help(topic: Optional[str] = None) -> str:
        return facade_help(registry, label=label, recipes=recipes, kept=kept, topic=topic)

    @mcp.tool(name=f"{prefix}find", description=described["find"])
    def _find(query: str = "", group: Optional[str] = None, limit: int = 12,
              detail: str = "brief") -> str:
        return facade_find(registry, query=query, group=group, limit=limit, detail=detail)

    @mcp.tool(name=f"{prefix}describe", description=described["describe"])
    def _describe(names: Any, doc_chars: int = 1200) -> str:
        return facade_describe(registry, names=names, doc_chars=doc_chars)

    @mcp.tool(name=f"{prefix}call", description=described["call"])
    def _call(name: str, args: Any = None) -> str:
        return facade_call(registry, name=name, args=args)

    @mcp.tool(name=f"{prefix}batch", description=described["batch"])
    def _batch(calls: Any, stop_on_error: bool = True) -> str:
        return facade_batch(registry, calls=calls, stop_on_error=stop_on_error)


def collapse_table(
    tools: "dict[str, dict]",
    *,
    prefix: str,
    label: str,
    title: str,
    group_prefixes: "Sequence[tuple[str, str]]" = (),
    group_overrides: "Optional[dict[str, str]]" = None,
    keywords: "Optional[dict[str, Sequence[str]]]" = None,
    synonyms: "Optional[dict[str, Sequence[str]]]" = None,
    keep: "Sequence[str]" = (),
    recipes: str = "",
    handler_key: str = "handler",
    source_root: "Optional[str]" = None,
) -> "tuple[dict[str, dict], ToolRegistry, str]":
    """:func:`collapse` for a server that keeps its tools in a plain dict.

    ``tools`` maps a tool name to ``{"description": str, "inputSchema": dict,
    handler_key: callable}``, where the handler takes a single arguments dict --
    the shape ``mykicadMcp``'s hand-rolled JSON-RPC server uses. Returns the
    replacement table, the registry the facade searches, and the ``instructions``
    string to hand back on ``initialize``.

    ``source_root`` -- see :func:`collapse`: when given, a startup
    :class:`SourceSnapshot` is attached to the registry and surfaced in
    ``{prefix}help()``.

    Nothing is mutated: the caller assigns the returned table over its own.
    """
    overrides = dict(group_overrides or {})
    keyword_map = {k: tuple(v) for k, v in (keywords or {}).items()}

    entries: "list[ToolEntry]" = []
    for name, info in tools.items():
        doc = info.get("description") or ""
        schema = dict(info.get("inputSchema") or {})
        handler = info[handler_key]
        entry = ToolEntry(
            name=name,
            group=derive_group(name, group_prefixes, overrides),
            doc=doc,
            schema=schema,
            # ToolRegistry.invoke calls fn(**kwargs); these handlers take one
            # positional arguments dict, so bind the shape difference here
            # rather than teaching the registry about two calling conventions.
            fn=(lambda _handler: lambda **kwargs: _handler(kwargs))(handler),
            keywords=keyword_map.get(name, ()),
            summary=_summarize(doc),
            guarded="confirm" in (schema.get("properties") or {}),
        )
        entry.index = _build_index(entry)
        entries.append(entry)

    registry = ToolRegistry(prefix, entries, dict(synonyms or {}))
    if source_root is not None:
        registry.freshness = take_snapshot(source_root)
    kept = sorted(set(keep) & set(tools))
    described = facade_descriptions(prefix, label)

    def _wrap(fn: "Callable[..., str]") -> "Callable[[dict], str]":
        # The server JSON-encodes whatever a handler returns; these return
        # ready-to-read text, so a raw string is exactly right.
        return lambda arguments: fn(**(arguments or {}))

    table: "dict[str, dict]" = {name: tools[name] for name in kept}
    facade = {
        "help": _wrap(lambda **kw: facade_help(registry, label=label, recipes=recipes,
                                               kept=kept, **kw)),
        "find": _wrap(lambda **kw: facade_find(registry, **kw)),
        "describe": _wrap(lambda **kw: facade_describe(registry, **kw)),
        "call": _wrap(lambda **kw: facade_call(registry, **kw)),
        "batch": _wrap(lambda **kw: facade_batch(registry, **kw)),
    }
    for short, handler in facade.items():
        table[f"{prefix}{short}"] = {
            "description": described[short],
            "inputSchema": FACADE_SCHEMAS[short],
            handler_key: handler,
        }

    instructions = _INSTRUCTIONS.format(
        title=title, facade=5 + len(kept), count=len(entries), p=prefix, label=label,
    )
    return table, registry, instructions


def _describe_entry(entry: ToolEntry, prefix: str, doc_chars: int = 1200) -> str:
    schema = entry.schema or {}
    props = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    lines = [f"{entry.name}{entry.signature()}  [{entry.group}]"]
    for name in props:
        spec = props[name] or {}
        bits = [f"  {name}: {_render_type(spec, schema)}",
                "required" if name in required else "optional"]
        if "default" in spec:
            bits.append(f"default={_fmt_default(spec['default'])}")
        enum = _enum_values(spec, schema)
        if enum:
            bits.append("one of " + "|".join(str(v) for v in enum[:12]))
        if spec.get("description"):
            bits.append(_summarize(spec["description"], 90))
        lines.append(" -- ".join(bits))
    doc = " ".join((entry.doc or "").split())
    if doc:
        lines.append("  " + (doc[: doc_chars - 3] + "..." if len(doc) > doc_chars else doc))
    lines.append("  " + _example_call(prefix, entry.name, schema))
    return "\n".join(lines)


def _as_name_list(value: Any) -> "list[str]":
    if value is None:
        return []
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("["):
            try:
                return _as_name_list(json.loads(stripped))
            except json.JSONDecodeError:
                return []
        return [part.strip() for part in stripped.split(",") if part.strip()]
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return []


def _as_args(value: Any) -> "dict[str, Any]":
    if value is None or value == "":
        return {}
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"args is not a JSON object: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError("args must be an object, not " + type(parsed).__name__)
        return parsed
    raise ValueError("args must be an object, not " + type(value).__name__)


def _as_calls(value: Any) -> "list[tuple[str, dict]]":
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"calls is not JSON: {exc}") from exc
    if isinstance(value, dict):
        value = [value]  # a single step, un-wrapped -- accept it rather than fail
    if not isinstance(value, (list, tuple)):
        raise ValueError("calls must be a list of {name, args} objects")
    steps: "list[tuple[str, dict]]" = []
    for index, item in enumerate(value, start=1):
        if isinstance(item, str):
            steps.append((item, {}))
            continue
        if not isinstance(item, dict):
            raise ValueError(f"step {index} is not an object")
        name = item.get("name") or item.get("tool")
        if not name:
            raise ValueError(f"step {index} has no 'name'")
        steps.append((str(name), _as_args(item.get("args") or item.get("arguments"))))
    return steps
