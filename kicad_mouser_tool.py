"""Look up manufacturer specs for a component from Mouser's official Search API.

Requires a free Mouser API key (MOUSER_API_KEY in the repo-root .env - see
.env.example) - Mouser's product-page HTML is actively bot-blocked for
scripted clients, so this deliberately does not fall back to scraping it;
a missing key is a hard stop with a message asking for one, not a silent
degrade. No bs4/requests dependency - just the stdlib.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from kicad_pcb_tool import audit_capacitor_voltages, audit_schematic_integrity, get_schematic_part, list_schematic_parts

# Repo-root .env/.mouser_cache.json (both gitignored) - see .env.example for
# the expected key name.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_ENV_FILE = _REPO_ROOT / ".env"
_CACHE_FILE = _REPO_ROOT / ".mouser_cache.json"
_CACHE_TTL_SECONDS = 3600  # one hour, per the "same part within the same hour" requirement

_MOUSER_API_BASE = "https://api.mouser.com/api/v1"

# Mouser's free-tier Search API caps calls per minute ("TooManyRequests" /
# "MaxCallPerMinute") - a bulk lookup across a whole schematic's worth of
# parts routinely bursts past that, so requests are both spaced out and
# retried with a backoff on that specific error instead of failing outright.
_RATE_LIMIT_BACKOFF_SECONDS = 20.0
_RATE_LIMIT_MAX_RETRIES = 4
_BULK_REQUEST_DELAY_SECONDS = 1.5


def _load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _get_mouser_api_key() -> str | None:
    key = os.environ.get("MOUSER_API_KEY")
    if key:
        return key.strip()
    return _load_env_file(_ENV_FILE).get("MOUSER_API_KEY") or None


def _require_mouser_api_key() -> str:
    key = _get_mouser_api_key()
    if not key:
        raise RuntimeError(
            "No Mouser API key configured. Please provide your Mouser API key - either set "
            "MOUSER_API_KEY in the repo-root .env file (copy .env.example and fill it in) or "
            "export it as an environment variable. This tool does not fall back to scraping "
            "Mouser's website, since that gets blocked by their bot protection."
        )
    return key


# ---------------------------------------------------------------------------
# Per-part result cache (JSON file, gitignored) - a bulk lookup across the
# whole schematic is expensive (rate-limited, ~1.5s/part minimum) and this
# project's parts don't change every minute, so a fresh-within-the-hour
# lookup for the same part is served from disk instead of hitting the API
# again. Keyed by host+path (ignoring the `?qs=...` tracking query string, so
# two properties pointing at "the same" product with different tracking
# params still share one cache entry) rather than the full URL.
# ---------------------------------------------------------------------------


def _cache_key_for_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    return f"{(parsed.hostname or '').lower()}{parsed.path}".rstrip("/")


def _load_mouser_cache() -> dict[str, Any]:
    try:
        return json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_mouser_cache(cache: dict[str, Any]) -> None:
    try:
        _CACHE_FILE.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")
    except OSError:
        pass


def _get_cached_mouser_result(url: str) -> dict[str, Any] | None:
    entry = _load_mouser_cache().get(_cache_key_for_url(url))
    if not entry:
        return None
    if time.time() - entry.get("cached_at", 0) > _CACHE_TTL_SECONDS:
        return None
    result = dict(entry.get("result") or {})
    result["url"] = url
    result["cache_hit"] = True
    return result


def _store_mouser_cache_result(url: str, result: dict[str, Any]) -> None:
    cache = _load_mouser_cache()
    cache[_cache_key_for_url(url)] = {"cached_at": time.time(), "url": url, "result": result}
    _save_mouser_cache(cache)


def _validate_mouser_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Unsupported URL scheme: {parsed.scheme!r}")
    host = (parsed.hostname or "").lower()
    if "mouser" not in host.split("."):
        raise ValueError(f"Refusing to look up a non-Mouser host: {parsed.hostname!r}")
    return url


def _extract_mpn_hint_from_url(url: str) -> str | None:
    """Mouser product URLs look like .../ProductDetail/<Manufacturer>/<PartNumber>
    - the last path segment is normally the manufacturer part number, which
    makes a good Search API keyword even before any lookup has happened.
    """
    path = urllib.parse.urlparse(url).path
    segments = [urllib.parse.unquote(s) for s in path.split("/") if s]
    return segments[-1] if segments else None


def find_all_mouser_urls(properties: dict[str, str]) -> dict[str, str]:
    """Find all Mouser product links in a schematic part's properties,
    indexed by field name. Returns a dict of {field_name: url} with primary
    and alternate Mouser part numbers. Supports multiple Mouser fields
    (Mouser, Mouser Part Number, Mouser Part Number Alt, etc) and prefers
    /ProductDetail/ links over datasheet PDFs since they identify a specific part.
    """
    urls: dict[str, str] = {}
    product_detail_urls: dict[str, str] = {}

    # Check all properties and extract Mouser URLs
    for key, value in properties.items():
        if not value or "mouser" not in value.lower():
            continue
        host = urllib.parse.urlparse(value).hostname or ""
        if "mouser" not in host.lower():
            continue

        # Prefer /ProductDetail/ URLs as they identify a specific part
        if "/productdetail/" in value.lower():
            product_detail_urls[key] = value
        else:
            urls[key] = value

    # Combine with ProductDetail URLs taking precedence
    result = {**urls, **product_detail_urls}
    return result


def list_component_mouser_urls(project_path: str | Path, reference: str) -> dict[str, Any]:
    """List all Mouser URLs (primary and alternates) for a component by
    reference designator. Returns both product detail links and raw properties,
    along with metadata about which are primary vs. alternates.
    """
    component = get_schematic_part(project_path, reference)
    properties = component.get("properties", {})

    all_urls = find_all_mouser_urls(properties)
    primary_url = find_mouser_url(properties)

    urls_with_metadata = []
    for field_name in sorted(all_urls.keys()):
        url = all_urls[field_name]
        is_primary = url == primary_url
        mpn_hint = _extract_mpn_hint_from_url(url)

        urls_with_metadata.append(
            {
                "field_name": field_name,
                "url": url,
                "is_primary": is_primary,
                "mpn_hint": mpn_hint,
            }
        )

    return {
        "reference": reference,
        "value": component.get("value", ""),
        "footprint": component.get("footprint", ""),
        "primary_url": primary_url,
        "alternate_count": len(urls_with_metadata) - 1 if primary_url else len(urls_with_metadata),
        "urls": urls_with_metadata,
    }


def find_mouser_url(properties: dict[str, str]) -> str | None:
    """Pick the best Mouser link out of a schematic part's properties (as
    returned by get_kicad_schematic_part). Prefers /ProductDetail/ links
    (product page URLs whose last path segment is a usable search hint) over
    plain datasheet PDFs, and prioritizes fields in this order:
    1. Mouser Part Number (primary product link)
    2. Mouser (alternate field name)
    3. Mouser Part Number Alt (alternate product link)
    4. Any other Mouser URL found
    """
    all_urls = find_all_mouser_urls(properties)
    if not all_urls:
        return None

    # Priority order for field names
    priority_fields = [
        "Mouser Part Number",
        "Mouser",
        "Mouser Part Number Alt",
    ]

    # Check priority fields first
    for field in priority_fields:
        if field in all_urls:
            return all_urls[field]

    # If no priority field matched, prefer ProductDetail links over datasheets
    for key, url in sorted(all_urls.items()):
        if "/productdetail/" in url.lower():
            return url

    # Fall back to any Mouser URL found
    return next(iter(all_urls.values())) if all_urls else None


# ---------------------------------------------------------------------------
# Mouser Search API
# ---------------------------------------------------------------------------


def _mouser_api_request(path: str, api_key: str, body: dict[str, Any], timeout: float = 15.0) -> dict[str, Any]:
    url = f"{_MOUSER_API_BASE}{path}?apiKey={urllib.parse.quote(api_key)}"
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    for attempt in range(_RATE_LIMIT_MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            if exc.code == 403 and "toomanyrequests" in error_body.lower() and attempt < _RATE_LIMIT_MAX_RETRIES:
                time.sleep(_RATE_LIMIT_BACKOFF_SECONDS)
                continue
            raise RuntimeError(f"Mouser API returned HTTP {exc.code} for {path}: {error_body[:300]}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Could not reach Mouser API: {exc.reason}") from exc
    raise RuntimeError(f"Mouser API rate limit ('calls per minute') not cleared after {_RATE_LIMIT_MAX_RETRIES} retries for {path}")


def _mouser_api_search_keyword(keyword: str, api_key: str, records: int = 5) -> list[dict[str, Any]]:
    body = {
        "SearchByKeywordRequest": {
            "keyword": keyword,
            "records": records,
            "startingRecord": 0,
            "searchOptions": "",
            "searchWithYourSignUpLanguage": "",
        }
    }
    data = _mouser_api_request("/search/keyword", api_key, body)
    errors = (data or {}).get("Errors") or []
    if errors:
        messages = "; ".join(str(e.get("Message", e)) for e in errors)
        raise RuntimeError(f"Mouser API error: {messages}")
    return ((data or {}).get("SearchResults") or {}).get("Parts") or []


def _pick_best_part_match(parts: list[dict[str, Any]], mpn_hint: str | None) -> dict[str, Any] | None:
    """Keyword search can return several parts (different packaging/tape-and-reel
    variants, similar part numbers, etc) - prefer the one whose own manufacturer
    part number matches the hint exactly (ignoring case/punctuation), else just
    take the top-ranked result.
    """
    if not parts:
        return None
    if mpn_hint:
        target = re.sub(r"[^a-z0-9]", "", mpn_hint.lower())
        for part in parts:
            candidate = re.sub(r"[^a-z0-9]", "", str(part.get("ManufacturerPartNumber", "")).lower())
            if candidate and candidate == target:
                return part
    return parts[0]


_DESC_CAPACITANCE_RE = re.compile(r"\b\d+(?:\.\d+)?\s*(?:pF|nF|uF|µF|mF)\b", re.IGNORECASE)
_DESC_VOLTAGE_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s*V(?:DC)?\b(?![a-zA-Z])")
_DESC_RESISTANCE_RE = re.compile(r"\b\d+(?:\.\d+)?\s*[kKmM]?\s*OHM\b", re.IGNORECASE)


def _parse_description_specs(description: str | None) -> dict[str, str]:
    """The Mouser Search API's `ProductAttributes` list is often sparse (just
    packaging/pack-qty, no electrical parametrics) for a given search result,
    but its free-text `Description` field usually still carries them, e.g.
    "Multilayer Ceramic Capacitors MLCC - SMD/SMT 50V 1800pF X7R 0402 5%" or
    "Thick Film Resistors - SMD 10K OHM 1%". Parsed as a lower-priority
    fallback source under the same field names used everywhere else.
    """
    if not description:
        return {}
    normalized = re.sub(r"\s+", " ", description).strip()
    specs: dict[str, str] = {}
    cap_match = _DESC_CAPACITANCE_RE.search(normalized)
    if cap_match:
        specs["Capacitance"] = cap_match.group(0)
    voltage_match = _DESC_VOLTAGE_RE.search(normalized)
    if voltage_match:
        specs["Voltage Rating"] = voltage_match.group(0)
    resistance_match = _DESC_RESISTANCE_RE.search(normalized)
    if resistance_match:
        specs["Resistance"] = resistance_match.group(0)
    return specs


# Standard EIA/IEC imperial case codes.
_EIA_IMPERIAL_CODES = frozenset(
    {
        "008004", "01005", "0201", "0402", "0603", "0805", "1008", "1111",
        "1206", "1210", "1218", "1806", "1812", "1825", "2010", "2512", "2515", "2920",
    }
)


def _extract_package_code(value: str | None) -> str | None:
    """Pull the imperial size code out of a Case/Package spec value, e.g.
    "0402 (1005 Metric)" -> "0402". Mouser lists the imperial code first with
    the metric equivalent trailing in parentheses, so the first token that
    matches a known EIA code is the imperial one.
    """
    if not value:
        return None
    for token in re.findall(r"\d{4,6}", value):
        if token in _EIA_IMPERIAL_CODES:
            return token
    return None


def _extract_package_code_from_mpn(mpn: str | None) -> str | None:
    """Series-prefix manufacturer part numbers often encode the imperial case
    size right after the prefix with no separator (Vishay "RCA0402...", KEMET
    "C0402...", Yageo "RC0402...", Murata "GRM0402...") - scan every digit run
    in the MPN for an embedded known EIA code as a last-resort package guess
    when nothing more explicit (a Case/Package spec or the description text)
    mentions it.
    """
    if not mpn:
        return None
    for run in re.findall(r"\d+", mpn):
        for length in (6, 5, 4):
            if len(run) < length:
                continue
            for start in range(len(run) - length + 1):
                token = run[start : start + length]
                if token in _EIA_IMPERIAL_CODES:
                    return token
    return None


def _specs_from_mouser_api_part(part: dict[str, Any]) -> dict[str, str]:
    specs: dict[str, str] = {}
    for attr in part.get("ProductAttributes") or []:
        name = attr.get("AttributeName")
        value = attr.get("AttributeValue")
        if name and value:
            specs[str(name).strip()] = str(value).strip()
    for key, value in _parse_description_specs(part.get("Description")).items():
        specs.setdefault(key, value)
    return specs


def _normalize_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", key.lower()).strip()


def _lookup_spec(specs: dict[str, str], *aliases: str) -> str | None:
    normalized = {_normalize_key(k): v for k, v in specs.items()}
    for alias in aliases:
        value = normalized.get(_normalize_key(alias))
        if value:
            return value
    return None


def _looks_like(value: str | None, *needles: str) -> bool:
    if not value:
        return False
    lowered = value.lower()
    return any(needle in lowered for needle in needles)


def _parse_price(price_text: Any) -> float | None:
    match = re.search(r"[\d.]+", str(price_text or ""))
    return float(match.group(0)) if match else None


def _price_breaks_from_part(part: dict[str, Any]) -> list[dict[str, Any]]:
    breaks: list[dict[str, Any]] = []
    for price_break in part.get("PriceBreaks") or []:
        quantity = price_break.get("Quantity")
        unit_price = _parse_price(price_break.get("Price"))
        if quantity is None or unit_price is None:
            continue
        breaks.append({"quantity": int(quantity), "unit_price": unit_price, "currency": price_break.get("Currency") or "USD"})
    breaks.sort(key=lambda entry: entry["quantity"])
    return breaks


def _unit_price_for_quantity(price_breaks: list[dict[str, Any]], quantity: int) -> dict[str, Any] | None:
    """Pick the price-break tier that applies when buying `quantity` units -
    Mouser's pricing model is that each listed tier's unit price holds from
    that quantity up to (but not including) the next tier, so this is the
    highest tier at or below `quantity`. Buying fewer than the lowest listed
    tier still costs at least that tier's unit price (there's no cheaper
    option), so the lowest tier is used as the floor.
    """
    if not price_breaks:
        return None
    applicable = price_breaks[0]
    for tier in price_breaks:
        if tier["quantity"] <= quantity:
            applicable = tier
        else:
            break
    return applicable


def _interpret_part(part: dict[str, Any]) -> dict[str, Any]:
    """Turn one Mouser API part record into the tool's field-level result.
    `Category` (Mouser's own part category, e.g. "Ceramic Capacitors" /
    "Chip Resistor - Surface Mount") is used as extra signal for detected_type
    and for telling MLCC/SMT construction apart from other cap/resistor types
    when the more specific attribute isn't present.
    """
    mpn = part.get("ManufacturerPartNumber")
    manufacturer = part.get("Manufacturer")
    category = part.get("Category")
    specs = _specs_from_mouser_api_part(part)

    capacitance = _lookup_spec(specs, "Capacitance")
    voltage_rating = _lookup_spec(
        specs, "Voltage Rating", "Voltage - Rated", "Rated Voltage", "Voltage Rating - DC", "Voltage Rating DC"
    )
    resistance = _lookup_spec(specs, "Resistance", "Resistance (Ohms)")
    package = _lookup_spec(specs, "Case/Package", "Package/Case", "Case / Package", "Package / Case", "Size/Dimension")
    capacitor_construction = _lookup_spec(specs, "Capacitor Type", "Dielectric", "Construction")
    mounting_style = _lookup_spec(specs, "Mounting Style", "Termination Style", "Product Type")

    if capacitance is not None or _looks_like(category, "capacitor"):
        detected_type = "capacitor"
    elif resistance is not None or _looks_like(category, "resistor"):
        detected_type = "resistor"
    elif specs or category:
        detected_type = "other"
    else:
        detected_type = "unknown"

    result: dict[str, Any] = {
        "manufacturer_part_number": mpn or "unknown",
        "manufacturer": manufacturer or "unknown",
        "mouser_part_number": part.get("MouserPartNumber") or "unknown",
        "category": category or "unknown",
        "availability": part.get("Availability") or "unknown",
        "availability_in_stock": part.get("AvailabilityInStock"),
        "lifecycle_status": part.get("LifecycleStatus") or None,
        "product_detail_url": part.get("ProductDetailUrl") or "",
        "price_breaks": _price_breaks_from_part(part),
        "detected_type": detected_type,
        "capacitance": "unsupported",
        "voltage_rating": "unsupported",
        "resistance": "unsupported",
        "package_size_inch": "unsupported",
        "raw_specifications": specs,
    }

    if detected_type == "capacitor":
        result["capacitance"] = capacitance or "unknown"
        result["voltage_rating"] = voltage_rating or "unknown"
        construction_signal = " ".join(s for s in (capacitor_construction, category) if s)
        if construction_signal and not _looks_like(construction_signal, "ceramic", "mlcc"):
            result["package_size_inch"] = "unsupported"
        else:
            code = _extract_package_code(package) or _extract_package_code_from_mpn(mpn)
            result["package_size_inch"] = code or "unknown"
    elif detected_type == "resistor":
        result["resistance"] = resistance or "unknown"
        mounting_signal = " ".join(s for s in (mounting_style, category) if s)
        if mounting_signal and not _looks_like(mounting_signal, "surface mount", "smd", "smt", "chip"):
            result["package_size_inch"] = "unsupported"
        else:
            code = _extract_package_code(package) or _extract_package_code_from_mpn(mpn)
            result["package_size_inch"] = code or "unknown"
    elif detected_type == "unknown":
        result["capacitance"] = "unknown"
        result["voltage_rating"] = "unknown"
        result["resistance"] = "unknown"
        result["package_size_inch"] = "unknown"
    # detected_type == "other": every field stays "unsupported" (set above)

    return result


def lookup_mouser_part(url: str) -> dict[str, Any]:
    """Look up a Mouser product's manufacturer part number, stock/lifecycle
    status, and type-specific electrical specs via Mouser's official Search
    API. Pass a Mouser product link, e.g. one found in a schematic part's
    `Datasheet`/`Mouser Part Number`/`Mouser Price/Stock` property via
    get_kicad_schematic_part (find_mouser_url picks the best one out of a
    part's properties). Only ever talks to mouser.* hosts.

    Requires MOUSER_API_KEY (repo-root .env - see .env.example); raises with
    a clear message asking for one if it's not configured, rather than
    falling back to scraping the product page (which Mouser's bot protection
    blocks for a meaningful fraction of scripted requests).

    `detected_type` is "capacitor", "resistor", "other" (confidently neither -
    an inductor, diode, connector, etc), or "unknown" (couldn't tell at all).
    Each of capacitance/voltage_rating/resistance/package_size_inch is then
    either a scraped value, "unsupported" (doesn't apply to this part's
    detected type - e.g. resistance on a capacitor, or a package code on a
    through-hole/electrolytic part that doesn't use 0402-style imperial
    sizing), or "unknown" (should apply but couldn't be found/parsed).
    `raw_specifications` carries every spec this lookup found under Mouser's
    own field names, in case the mapping above misses one. `availability`/
    `lifecycle_status` support the out-of-stock/NRND report.

    Results are cached to disk (.mouser_cache.json, gitignored) per part for
    one hour - a repeat lookup for the same part within that window is served
    from the cache (`cache_hit: true`) instead of calling the API again.
    """
    _validate_mouser_url(url)
    cached = _get_cached_mouser_result(url)
    if cached is not None:
        return cached

    api_key = _require_mouser_api_key()
    mpn_hint = _extract_mpn_hint_from_url(url)

    parts = _mouser_api_search_keyword(mpn_hint or url, api_key)
    part = _pick_best_part_match(parts, mpn_hint)
    if part is None:
        raise RuntimeError(f"Mouser API returned no matching parts for {url!r} (keyword {mpn_hint!r})")

    result = _interpret_part(part)
    _store_mouser_cache_result(url, result)
    result = dict(result)
    result["url"] = url
    result["cache_hit"] = False
    return result


def bulk_list_component_mouser_urls(project_path: str | Path, references: list[str] | None = None) -> dict[str, Any]:
    """List all Mouser URLs for many schematic parts in one call - useful for
    auditing which parts have alternates, or to spot parts missing Mouser links
    entirely. Defaults to one representative reference per unique part from
    list_schematic_parts; pass `references` for a specific subset.
    """
    representative_to_group: dict[str, dict[str, Any]] = {}
    if references is None:
        parts = list_schematic_parts(project_path)["parts"]
        references = []
        for part in parts:
            if not part["references"]:
                continue
            representative = part["references"][0]
            references.append(representative)
            representative_to_group[representative] = part

    results: list[dict[str, Any]] = []
    no_mouser_link: list[dict[str, Any]] = []

    for reference in references:
        try:
            component_mouser_info = list_component_mouser_urls(project_path, reference)
        except KeyError as exc:
            no_mouser_link.append(
                {
                    "reference": reference,
                    "error": str(exc),
                }
            )
            continue

        if not component_mouser_info["urls"]:
            group = representative_to_group.get(reference)
            all_refs = group["references"] if group else [reference]
            quantity = group["quantity"] if group else 1

            no_mouser_link.append(
                {
                    "reference": reference,
                    "all_references": all_refs,
                    "quantity": quantity,
                    "value": component_mouser_info["value"],
                    "reason": "no Mouser link found",
                }
            )
            continue

        group = representative_to_group.get(reference)
        all_refs = group["references"] if group else [reference]
        quantity = group["quantity"] if group else 1

        results.append(
            {
                "reference": reference,
                "all_references": all_refs,
                "quantity": quantity,
                "value": component_mouser_info["value"],
                "footprint": component_mouser_info["footprint"],
                "primary_url": component_mouser_info["primary_url"],
                "alternate_count": component_mouser_info["alternate_count"],
                "urls": component_mouser_info["urls"],
            }
        )

    return {
        "checked_count": len(references),
        "with_mouser_link_count": len(results),
        "with_alternates_count": sum(1 for r in results if r["alternate_count"] > 0),
        "no_mouser_link_count": len(no_mouser_link),
        "results": results,
        "no_mouser_link": no_mouser_link,
    }


def bulk_lookup_mouser_parts(project_path: str | Path, references: list[str] | None = None) -> dict[str, Any]:
    """Look up Mouser data for many schematic parts in one call instead of one
    tool round-trip per part - the batch-speed entry point for auditing an
    entire schematic's worth of parts (MPN verification, stock/NRND report).

    Defaults to one representative reference per unique part from
    list_schematic_parts (i.e. every distinct Value+Footprint group once, not
    every individual placed instance); pass `references` to look up a
    specific subset instead. Each part missing a discoverable Mouser link, or
    whose lookup fails for any reason (no API match, transient API error), is
    reported under `skipped`/`errors` rather than aborting the whole batch.
    """
    representative_to_group: dict[str, dict[str, Any]] = {}
    if references is None:
        parts = list_schematic_parts(project_path)["parts"]
        references = []
        for part in parts:
            if not part["references"]:
                continue
            representative = part["references"][0]
            references.append(representative)
            representative_to_group[representative] = part

    results: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    made_api_call = False
    for reference in references:
        try:
            component = get_schematic_part(project_path, reference)
        except KeyError as exc:
            errors.append({"reference": reference, "error": str(exc)})
            continue

        group = representative_to_group.get(reference)
        all_references = group["references"] if group else [reference]
        quantity = group["quantity"] if group else 1

        mouser_url = find_mouser_url(component.get("properties", {}))
        if not mouser_url:
            skipped.append(
                {
                    "reference": reference,
                    "all_references": all_references,
                    "quantity": quantity,
                    "value": component.get("value", ""),
                    "reason": "no Mouser link in properties",
                }
            )
            continue

        if _get_cached_mouser_result(mouser_url) is None:
            if made_api_call:
                time.sleep(_BULK_REQUEST_DELAY_SECONDS)  # stay under Mouser's per-minute call cap
            made_api_call = True

        try:
            mouser = lookup_mouser_part(mouser_url)
        except Exception as exc:
            errors.append(
                {
                    "reference": reference,
                    "all_references": all_references,
                    "quantity": quantity,
                    "value": component.get("value", ""),
                    "mouser_url": mouser_url,
                    "error": str(exc),
                }
            )
            continue

        results.append(
            {
                "reference": reference,
                "all_references": all_references,
                "quantity": quantity,
                "value": component.get("value", ""),
                "footprint": component.get("footprint", ""),
                "schematic_properties": component.get("properties", {}),
                "mouser_url": mouser_url,
                "mouser": mouser,
            }
        )

    return {
        "requested_count": len(references),
        "found_count": len(results),
        "skipped_count": len(skipped),
        "error_count": len(errors),
        "results": results,
        "skipped": skipped,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# Alternate-link optimization - given a component with more than one
# candidate Mouser link (e.g. "Mouser Price/Stock" plus a second-source
# "Mouser Part Number Alt"), rank them by live stock/pricing data instead of
# find_mouser_url's static field-name order, so the link actually best suited
# to ordering this board is surfaced as the recommended one.
# ---------------------------------------------------------------------------


def _quantity_needed_for_reference(project_path: str | Path, reference: str) -> int:
    """How many of this part the board actually uses - the Value+Footprint
    group's total quantity from list_schematic_parts, not just 1 instance.
    """
    lowered = reference.strip().upper()
    for group in list_schematic_parts(project_path)["parts"]:
        if any(ref.strip().upper() == lowered for ref in group["references"]):
            return group["quantity"]
    return 1


def _rank_mouser_candidates(candidates: list[dict[str, Any]], quantity_needed: int) -> list[dict[str, Any]]:
    """Order candidate Mouser links for one component by, in priority order:
    1. In stock for at least `quantity_needed` units (the board's actual need).
    2. Sold with a qty-1 price break (not reel-only/bulk-minimum pricing),
       then the greatest number in stock.
    3. Cheapest unit price at `quantity_needed` (falls back to the lowest
       listed tier if the board doesn't need enough to reach any tier).
    Each candidate dict must have a `mouser` key holding a lookup_mouser_part
    result. Returns a new list, annotated with the fields the ranking used
    and sorted best-first.
    """
    annotated: list[dict[str, Any]] = []
    for candidate in candidates:
        mouser = candidate["mouser"]
        price_breaks = mouser.get("price_breaks") or []
        in_stock_count = _availability_in_stock_count(mouser)
        meets_quantity = in_stock_count is not None and in_stock_count >= quantity_needed
        has_qty_one_price = bool(price_breaks) and price_breaks[0]["quantity"] == 1
        tier = _unit_price_for_quantity(price_breaks, quantity_needed)
        unit_price = tier["unit_price"] if tier else None

        annotated.append(
            {
                **candidate,
                "in_stock_count": in_stock_count,
                "meets_quantity": meets_quantity,
                "has_qty_one_price": has_qty_one_price,
                "unit_price_at_quantity": unit_price,
                "currency": tier["currency"] if tier else None,
            }
        )

    def sort_key(entry: dict[str, Any]) -> tuple[int, int, int, float]:
        return (
            0 if entry["meets_quantity"] else 1,
            0 if entry["has_qty_one_price"] else 1,
            -(entry["in_stock_count"] or 0),
            entry["unit_price_at_quantity"] if entry["unit_price_at_quantity"] is not None else float("inf"),
        )

    ranked = sorted(annotated, key=sort_key)
    for i, entry in enumerate(ranked):
        entry["rank"] = i + 1
    return ranked


def optimize_component_mouser_alternates(
    project_path: str | Path, reference: str, quantity_needed: int | None = None
) -> dict[str, Any]:
    """Rank a component's candidate Mouser links (its "Mouser"/"Mouser Price/Stock"/
    "Mouser Part Number Alt"/etc properties) by live stock and pricing instead of
    find_mouser_url's static field-name preference order, and recommend which one
    to treat as primary for ordering this board.

    Ranking priority: (1) in stock for at least the board's required quantity,
    (2) sold with a qty-1 price break (over reel-only/bulk-minimum pricing), with
    ties broken by greatest quantity in stock, (3) cheapest unit price at the
    required quantity. Components with only one candidate link still get looked
    up so an out-of-stock/no-price-break link is flagged rather than assumed fine.

    `quantity_needed` defaults to how many of this part the schematic actually
    places (its Value+Footprint group's total across all references) rather than
    1, since a single-board order needs to cover every instance. REQUIRES
    MOUSER_API_KEY.
    """
    component = get_schematic_part(project_path, reference)
    properties = component.get("properties", {})
    all_urls = find_all_mouser_urls(properties)
    if not all_urls:
        raise KeyError(f"No Mouser URLs found for {reference}")

    if quantity_needed is None:
        quantity_needed = _quantity_needed_for_reference(project_path, reference)

    current_primary_url = find_mouser_url(properties)

    candidates: list[dict[str, Any]] = []
    made_api_call = False
    for field_name in sorted(all_urls.keys()):
        url = all_urls[field_name]
        if _get_cached_mouser_result(url) is None:
            if made_api_call:
                time.sleep(_BULK_REQUEST_DELAY_SECONDS)  # stay under Mouser's per-minute call cap
            made_api_call = True
        mouser = lookup_mouser_part(url)
        candidates.append({"field_name": field_name, "url": url, "mouser": mouser})

    ranked = _rank_mouser_candidates(candidates, quantity_needed)
    recommended = ranked[0]

    return {
        "reference": reference,
        "value": component.get("value", ""),
        "quantity_needed": quantity_needed,
        "current_primary_url": current_primary_url,
        "recommended_field": recommended["field_name"],
        "recommended_url": recommended["url"],
        "recommendation_changed": recommended["url"] != current_primary_url,
        "candidates": ranked,
    }


def bulk_optimize_component_mouser_alternates(
    project_path: str | Path,
    references: list[str] | None = None,
    only_with_alternates: bool = True,
) -> dict[str, Any]:
    """Batch version of optimize_component_mouser_alternates across the
    schematic's unique parts (or a given subset). Defaults to skipping parts
    with only a single candidate Mouser link (`only_with_alternates=True`) so
    the (rate-limited) API budget is spent on parts where there's actually a
    choice to make; set it False to also validate single-link parts' stock.

    Returns `changed` - components whose live-ranked recommendation differs
    from what find_mouser_url's static field-priority order would have picked
    - as the actionable list of parts worth re-pointing at a better link.
    REQUIRES MOUSER_API_KEY.
    """
    representative_to_group: dict[str, dict[str, Any]] = {}
    if references is None:
        parts = list_schematic_parts(project_path)["parts"]
        references = []
        for part in parts:
            if not part["references"]:
                continue
            representative = part["references"][0]
            references.append(representative)
            representative_to_group[representative] = part

    results: list[dict[str, Any]] = []
    changed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for reference in references:
        try:
            component = get_schematic_part(project_path, reference)
        except KeyError as exc:
            errors.append({"reference": reference, "error": str(exc)})
            continue

        group = representative_to_group.get(reference)
        all_references = group["references"] if group else [reference]
        quantity = group["quantity"] if group else 1

        all_urls = find_all_mouser_urls(component.get("properties", {}))
        if not all_urls:
            skipped.append(
                {"reference": reference, "all_references": all_references, "value": component.get("value", ""), "reason": "no Mouser link in properties"}
            )
            continue
        if only_with_alternates and len(all_urls) < 2:
            skipped.append(
                {"reference": reference, "all_references": all_references, "value": component.get("value", ""), "reason": "only one candidate link"}
            )
            continue

        try:
            result = optimize_component_mouser_alternates(project_path, reference, quantity_needed=quantity)
        except Exception as exc:
            errors.append({"reference": reference, "all_references": all_references, "value": component.get("value", ""), "error": str(exc)})
            continue

        result["all_references"] = all_references
        results.append(result)
        if result["recommendation_changed"]:
            changed.append(result)

    return {
        "requested_count": len(references),
        "evaluated_count": len(results),
        "changed_count": len(changed),
        "skipped_count": len(skipped),
        "error_count": len(errors),
        "results": results,
        "changed": changed,
        "skipped": skipped,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# Buy list - how many of each part to actually order, given price-break
# economics (buying past the required quantity can lower the total bill) and
# a floor of extra units for very cheap parts (not worth a second order over
# a handful of cents).
# ---------------------------------------------------------------------------

# "Under $0.05 at our quantity" -> buy 10 extra; "under $0.10" -> buy 5 extra.
# Ranges are mutually exclusive (checked narrowest-first) - a part isn't both.
_CHEAP_PART_PADDING = (
    (0.05, 10),
    (0.10, 5),
)


def _optimize_buy_quantity(price_breaks: list[dict[str, Any]], quantity_needed: int) -> dict[str, Any]:
    """Decide how many units to actually buy for one line item, starting from
    `quantity_needed` (the board's requirement):

    1. Cheap-part padding: if the unit price at `quantity_needed` is under
       $0.05, add 10 extra units; under $0.10, add 5 extra. Based on the
       unit price at the required quantity, not any padded/upgraded quantity.
    2. Price-break upgrade: compare the total cost of buying at the padded
       quantity against buying at every higher price-break tier's own
       minimum quantity - if any higher tier's total bill (tier quantity x
       tier unit price) is cheaper, buy that tier's quantity instead. This
       never buys *below* the padded quantity, only more if it's a net
       savings on the total bill (not just a lower unit price).

    Returns buy_quantity/unit_price/currency/line_cost for the chosen point,
    plus extra_units (over quantity_needed) and human-readable reasons.
    Falls back to the padded quantity, unpriced, if the part has no
    price-break data at all.
    """
    base_tier = _unit_price_for_quantity(price_breaks, quantity_needed)
    base_unit_price = base_tier["unit_price"] if base_tier else None

    padding = 0
    padding_reason = None
    if base_unit_price is not None:
        for threshold, extra in _CHEAP_PART_PADDING:
            if base_unit_price < threshold:
                padding = extra
                padding_reason = f"unit price ${base_unit_price:.4f} at required qty {quantity_needed} is under ${threshold:.2f} - padded by {extra}"
                break
    padded_qty = quantity_needed + padding

    options: list[dict[str, Any]] = []
    padded_tier = _unit_price_for_quantity(price_breaks, padded_qty)
    if padded_tier:
        options.append({"quantity": padded_qty, "unit_price": padded_tier["unit_price"], "currency": padded_tier["currency"]})
    for tier in price_breaks:
        if tier["quantity"] > padded_qty:
            options.append({"quantity": tier["quantity"], "unit_price": tier["unit_price"], "currency": tier["currency"]})

    if not options:
        return {
            "buy_quantity": padded_qty,
            "unit_price": None,
            "currency": None,
            "line_cost": None,
            "extra_units": padded_qty - quantity_needed,
            "padding_reason": padding_reason,
            "price_break_upgrade": False,
            "price_break_reason": None,
        }

    for option in options:
        option["line_cost"] = round(option["quantity"] * option["unit_price"], 4)

    best = min(options, key=lambda o: (o["line_cost"], o["quantity"]))
    price_break_upgrade = best["quantity"] > padded_qty
    price_break_reason = None
    if price_break_upgrade:
        padded_cost = next((o["line_cost"] for o in options if o["quantity"] == padded_qty), None)
        price_break_reason = (
            f"buying {best['quantity']} at ${best['unit_price']:.4f} (${best['line_cost']:.2f} total) is cheaper "
            f"than {padded_qty} at ${padded_cost:.2f} total" if padded_cost is not None else
            f"buying {best['quantity']} at ${best['unit_price']:.4f} totals less than the padded quantity"
        )

    return {
        "buy_quantity": best["quantity"],
        "unit_price": best["unit_price"],
        "currency": best["currency"],
        "line_cost": best["line_cost"],
        "extra_units": best["quantity"] - quantity_needed,
        "padding_reason": padding_reason,
        "price_break_upgrade": price_break_upgrade,
        "price_break_reason": price_break_reason,
    }


def generate_mouser_buy_list(
    project_path: str | Path,
    buy_list_path: str | Path | None = None,
    references: list[str] | None = None,
) -> dict[str, Any]:
    """Build an orderable buy list across the schematic's unique parts (or a
    given subset): the best Mouser link for each part (live stock/price
    ranked via optimize_component_mouser_alternates, same as
    bulk_optimize_kicad_mouser_alternates) and how many units to actually buy
    per _optimize_buy_quantity - the board's required quantity, padded for
    very cheap parts (10 extra under $0.05/unit, 5 extra under $0.10/unit),
    then bumped further if a higher price-break tier's total cost undercuts
    that padded quantity's total cost. Writes a Markdown table to
    `buy_list_path` (defaults to 'buy_list.md' at the project root) with the
    link, quantities, per-line cost, and the reason behind any extra units,
    plus a grand total. REQUIRES MOUSER_API_KEY.
    """
    schematic_dir = Path(list_schematic_parts(project_path)["schematic_dir"])
    output_path = Path(buy_list_path) if buy_list_path is not None else schematic_dir / "buy_list.md"

    optimized = bulk_optimize_component_mouser_alternates(project_path, references=references, only_with_alternates=False)

    buy_lines: list[dict[str, Any]] = []
    unpriced: list[dict[str, Any]] = []
    total_by_currency: dict[str, float] = {}

    for entry in optimized["results"]:
        recommended = entry["candidates"][0]
        mouser = recommended["mouser"]
        quantity_needed = entry["quantity_needed"]
        buy = _optimize_buy_quantity(mouser.get("price_breaks") or [], quantity_needed)

        row = {
            "reference": entry["reference"],
            "all_references": entry["all_references"],
            "value": entry["value"],
            "quantity_needed": quantity_needed,
            "buy_quantity": buy["buy_quantity"],
            "extra_units": buy["extra_units"],
            "padding_reason": buy["padding_reason"],
            "price_break_upgrade": buy["price_break_upgrade"],
            "price_break_reason": buy["price_break_reason"],
            "unit_price": buy["unit_price"],
            "currency": buy["currency"],
            "line_cost": buy["line_cost"],
            "manufacturer_part_number": mouser.get("manufacturer_part_number"),
            "mouser_url": recommended["url"],
        }
        if buy["unit_price"] is None:
            unpriced.append(row)
        else:
            buy_lines.append(row)
            total_by_currency[buy["currency"]] = round(total_by_currency.get(buy["currency"], 0.0) + buy["line_cost"], 4)

    lines = [
        "# Mouser Buy List",
        "",
        f"Covers {len(buy_lines) + len(unpriced)} of {optimized['requested_count']} unique parts "
        f"({optimized['skipped_count']} had no Mouser link, {optimized['error_count']} failed to look up).",
        "",
    ]
    if buy_lines:
        totals_text = ", ".join(f"{currency} {total:,.2f}" for currency, total in sorted(total_by_currency.items()))
        lines.append(f"**Estimated total: {totals_text}**, from {len(buy_lines)} priced line items.")
        if unpriced:
            lines.append(f"({len(unpriced)} part(s) excluded - no Mouser price-break data; see below.)")
        lines.append("")
        lines.append("| Reference(s) | Value | Qty Needed | Buy Qty | Extra | Why | Unit Price | Line Cost | MPN | Mouser Link |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|")
        for row in sorted(buy_lines, key=lambda r: r["line_cost"], reverse=True):
            refs = ", ".join(row["all_references"])
            reasons = [r for r in (row["padding_reason"], row["price_break_reason"]) if r]
            why = "; ".join(reasons) if reasons else "-"
            lines.append(
                f"| {refs} | {row['value']} | {row['quantity_needed']} | {row['buy_quantity']} | {row['extra_units']} | "
                f"{why} | {row['currency']} {row['unit_price']:.4f} | {row['currency']} {row['line_cost']:.2f} | "
                f"{row['manufacturer_part_number']} | {row['mouser_url']} |"
            )
    else:
        lines.append("No priced line items.")
    if unpriced:
        lines += ["", "## Excluded from cost total (no price-break data)", ""]
        lines.append("| Reference(s) | Value | Qty Needed | Buy Qty | Mouser Link |")
        lines.append("|---|---|---|---|---|")
        for row in unpriced:
            refs = ", ".join(row["all_references"])
            lines.append(f"| {refs} | {row['value']} | {row['quantity_needed']} | {row['buy_quantity']} | {row['mouser_url']} |")
    if optimized["skipped"]:
        lines += ["", "## No Mouser Link", ""]
        lines.append("| Reference(s) | Value | Reason |")
        lines.append("|---|---|---|")
        for row in optimized["skipped"]:
            refs = ", ".join(row.get("all_references") or [row["reference"]])
            lines.append(f"| {refs} | {row.get('value', '')} | {row.get('reason', '')} |")
    if optimized["errors"]:
        lines += ["", "## Lookup Errors", ""]
        lines.append("| Reference(s) | Value | Error |")
        lines.append("|---|---|---|")
        for row in optimized["errors"]:
            refs = ", ".join(row.get("all_references") or [row["reference"]])
            lines.append(f"| {refs} | {row.get('value', '')} | {row.get('error', '')} |")
    lines += [
        "",
        "---",
        "Note: extra units are padded for very cheap parts (10 extra under $0.05/unit, 5 extra under $0.10/unit) "
        "and bumped further only when a higher Mouser price-break tier's total cost undercuts the padded "
        "quantity's total cost.",
        "",
    ]

    output_path.write_text("\n".join(lines), encoding="utf-8")

    return {
        "buy_list_path": str(output_path),
        "total_by_currency": total_by_currency,
        "priced_line_count": len(buy_lines),
        "unpriced_count": len(unpriced),
        "buy_lines": buy_lines,
        "unpriced": unpriced,
        "no_mouser_link": optimized["skipped"],
        "errors": optimized["errors"],
    }


def _canonical_mpn_from_properties(properties: dict[str, str]) -> str | None:
    for key, value in properties.items():
        if re.sub(r"[^a-z0-9]", "", key.lower()) == "manufacturerpartnumber" and value:
            return value
    return None


def _normalize_mpn_for_compare(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def audit_manufacturer_part_numbers(project_path: str | Path, references: list[str] | None = None) -> dict[str, Any]:
    """Cross-check each schematic part's `Manufacturer_Part_Number` property
    against the manufacturer part number Mouser's Search API actually returns
    for that part's own Mouser link - catches typos, copy-paste errors, or a
    stale value left over from swapping which exact part a symbol points to.
    Run normalize_manufacturer_part_number_properties first so parts that
    only carry the MPN under a differently-named property (PROD_ID, MPN,
    etc.) get picked up here too, instead of showing up as "missing".

    Parts with no Mouser link at all, or whose Mouser lookup failed, aren't
    compared (nothing to compare against) - see `skipped`/`errors`, carried
    through unchanged from bulk_lookup_mouser_parts.
    """
    bulk = bulk_lookup_mouser_parts(project_path, references=references)

    matched: list[dict[str, Any]] = []
    mismatched: list[dict[str, Any]] = []
    missing_schematic_mpn: list[dict[str, Any]] = []

    for entry in bulk["results"]:
        schematic_mpn = _canonical_mpn_from_properties(entry["schematic_properties"])
        mouser_mpn = entry["mouser"].get("manufacturer_part_number")
        mouser_mpn = None if mouser_mpn in (None, "unknown") else mouser_mpn
        if mouser_mpn is None:
            continue  # Mouser lookup didn't identify an MPN either - nothing to compare

        row = {
            "reference": entry["reference"],
            "all_references": entry["all_references"],
            "value": entry["value"],
            "schematic_manufacturer_part_number": schematic_mpn,
            "mouser_manufacturer_part_number": mouser_mpn,
            "mouser_url": entry["mouser_url"],
        }
        if schematic_mpn is None:
            missing_schematic_mpn.append(row)
        elif _normalize_mpn_for_compare(schematic_mpn) == _normalize_mpn_for_compare(mouser_mpn):
            matched.append(row)
        else:
            mismatched.append(row)

    return {
        "checked_count": len(bulk["results"]),
        "matched_count": len(matched),
        "mismatched_count": len(mismatched),
        "missing_schematic_mpn_count": len(missing_schematic_mpn),
        "skipped_count": bulk["skipped_count"],
        "error_count": bulk["error_count"],
        "matched": matched,
        "mismatched": mismatched,
        "missing_schematic_mpn": missing_schematic_mpn,
        "skipped": bulk["skipped"],
        "errors": bulk["errors"],
    }


_LIFECYCLE_FLAG_MARKERS = (
    "nrnd",
    "not recommended",
    "obsolete",
    "eol",
    "end of life",
    "discontinued",
    "last time buy",
)


def _availability_in_stock_count(mouser_result: dict[str, Any]) -> int | None:
    raw = mouser_result.get("availability_in_stock")
    if raw in (None, ""):
        return None
    try:
        return int(re.sub(r"[^\d]", "", str(raw)) or "0")
    except ValueError:
        return None


def generate_mouser_stock_report(
    project_path: str | Path,
    report_path: str | Path | None = None,
    references: list[str] | None = None,
) -> dict[str, Any]:
    """Run bulk_lookup_mouser_parts across the schematic's unique parts (or a
    given subset) and write a Markdown report listing a BOM cost estimate
    (one board's worth, using Mouser's own quantity-break pricing) plus every
    part Mouser currently shows as out of stock or lifecycle-flagged (Not
    Recommended for New Designs, obsolete, discontinued, etc), so those can
    be addressed before fabrication/ordering. Defaults to
    `mouser_stock_report.md` at the project root.

    Mouser's API only sometimes populates `LifecycleStatus` - a part with no
    status isn't necessarily healthy, it just means Mouser didn't report
    anything either way, and is left out of `not_recommended` accordingly.
    Parts with no Mouser link, a failed lookup, or no price-break data at all
    are excluded from the cost total and listed separately so the total's
    coverage is clear rather than silently understated.
    """
    schematic_dir = Path(list_schematic_parts(project_path)["schematic_dir"])
    output_path = Path(report_path) if report_path is not None else schematic_dir / "mouser_stock_report.md"

    bulk = bulk_lookup_mouser_parts(project_path, references=references)

    out_of_stock: list[dict[str, Any]] = []
    not_recommended: list[dict[str, Any]] = []
    bom_lines: list[dict[str, Any]] = []
    bom_unpriced: list[dict[str, Any]] = []
    bom_total_by_currency: dict[str, float] = {}

    for entry in bulk["results"]:
        mouser = entry["mouser"]
        row = {
            "reference": entry["reference"],
            "all_references": entry["all_references"],
            "quantity": entry["quantity"],
            "value": entry["value"],
            "manufacturer_part_number": mouser.get("manufacturer_part_number"),
            "manufacturer": mouser.get("manufacturer"),
            "availability": mouser.get("availability"),
            "lifecycle_status": mouser.get("lifecycle_status"),
            "mouser_url": entry["mouser_url"],
        }
        in_stock_count = _availability_in_stock_count(mouser)
        if in_stock_count is not None and in_stock_count <= 0:
            out_of_stock.append(row)
        if mouser.get("lifecycle_status") and _looks_like(mouser.get("lifecycle_status"), *_LIFECYCLE_FLAG_MARKERS):
            not_recommended.append(row)

        tier = _unit_price_for_quantity(mouser.get("price_breaks") or [], entry["quantity"])
        if tier is None:
            bom_unpriced.append(row)
            continue
        line_cost = round(tier["unit_price"] * entry["quantity"], 4)
        bom_total_by_currency[tier["currency"]] = round(bom_total_by_currency.get(tier["currency"], 0.0) + line_cost, 4)
        bom_lines.append(
            {
                **row,
                "unit_price": tier["unit_price"],
                "price_break_quantity": tier["quantity"],
                "currency": tier["currency"],
                "line_cost": line_cost,
            }
        )

    lines = [
        "# Mouser Stock & Lifecycle Report",
        "",
        f"Checked {bulk['found_count']} of {bulk['requested_count']} unique parts "
        f"({bulk['skipped_count']} had no Mouser link, {bulk['error_count']} failed to look up).",
        "",
        "## Bill of Materials Cost Estimate",
        "",
    ]
    if bom_lines:
        totals_text = ", ".join(f"{currency} {total:,.2f}" for currency, total in sorted(bom_total_by_currency.items()))
        lines.append(f"**Estimated total (one board): {totals_text}**, from {len(bom_lines)} priced line items.")
        if bom_unpriced:
            lines.append(f"({len(bom_unpriced)} part(s) excluded - no Mouser price-break data; see below.)")
        lines.append("")
        lines.append("| Reference(s) | Value | Qty | Unit Price | Line Cost | MPN | Mouser Link |")
        lines.append("|---|---|---|---|---|---|---|")
        for row in sorted(bom_lines, key=lambda r: r["line_cost"], reverse=True):
            refs = ", ".join(row["all_references"])
            lines.append(
                f"| {refs} | {row['value']} | {row['quantity']} | {row['currency']} {row['unit_price']:.4f} | "
                f"{row['currency']} {row['line_cost']:.2f} | {row['manufacturer_part_number']} | {row['mouser_url']} |"
            )
    else:
        lines.append("No priced line items.")
    if bom_unpriced:
        lines += ["", "### Excluded from cost total (no price-break data)", ""]
        lines.append("| Reference(s) | Value | Qty | Mouser Link |")
        lines.append("|---|---|---|---|")
        for row in bom_unpriced:
            refs = ", ".join(row["all_references"])
            lines.append(f"| {refs} | {row['value']} | {row['quantity']} | {row['mouser_url']} |")
    lines += ["", "## Out of Stock", ""]
    if out_of_stock:
        lines.append("| Reference(s) | Value | MPN | Manufacturer | Availability | Mouser Link |")
        lines.append("|---|---|---|---|---|---|")
        for row in out_of_stock:
            refs = ", ".join(row["all_references"])
            lines.append(
                f"| {refs} | {row['value']} | {row['manufacturer_part_number']} | {row['manufacturer']} | "
                f"{row['availability']} | {row['mouser_url']} |"
            )
    else:
        lines.append("None found.")
    lines += ["", "## Not Recommended for New Designs / Obsolete / Discontinued", ""]
    if not_recommended:
        lines.append("| Reference(s) | Value | MPN | Manufacturer | Lifecycle Status | Mouser Link |")
        lines.append("|---|---|---|---|---|---|")
        for row in not_recommended:
            refs = ", ".join(row["all_references"])
            lines.append(
                f"| {refs} | {row['value']} | {row['manufacturer_part_number']} | {row['manufacturer']} | "
                f"{row['lifecycle_status']} | {row['mouser_url']} |"
            )
    else:
        lines.append("None found.")
    lines += [
        "",
        "---",
        "Note: Mouser's Search API only sometimes populates lifecycle status; a part not listed here "
        "isn't guaranteed current, it just wasn't flagged by Mouser at the time of this report.",
        "",
    ]

    output_path.write_text("\n".join(lines), encoding="utf-8")

    return {
        "report_path": str(output_path),
        "checked_count": bulk["found_count"],
        "bom_total_by_currency": bom_total_by_currency,
        "bom_priced_line_count": len(bom_lines),
        "bom_unpriced_count": len(bom_unpriced),
        "bom_lines": bom_lines,
        "bom_unpriced": bom_unpriced,
        "out_of_stock_count": len(out_of_stock),
        "not_recommended_count": len(not_recommended),
        "out_of_stock": out_of_stock,
        "not_recommended": not_recommended,
        "skipped": bulk["skipped"],
        "errors": bulk["errors"],
    }


# ---------------------------------------------------------------------------
# Schematic health check - cross-checking every part's stated Value/Footprint
# against what its linked Mouser product actually is, confirming at least one
# candidate link can supply the build, and rolling that up with the
# schematic-only integrity/voltage checks into one pre-fab pass.
# ---------------------------------------------------------------------------

_RESISTANCE_UNIT_MULTIPLIERS = {"r": 1.0, "k": 1e3, "m": 1e6, "meg": 1e6, "g": 1e9}
_CAPACITANCE_UNIT_MULTIPLIERS = {"f": 1.0, "p": 1e-12, "n": 1e-9, "u": 1e-6, "m": 1e-3}
_OHM_WORD_RE = re.compile(r"(?i)ohms?")
_FARAD_WORD_RE = re.compile(r"(?i)farads?")
_TRAILING_TOLERANCE_RE = re.compile(r"±?\d+(?:\.\d+)?%.*$")


def _lookup_unit_multiplier(unit: str, table: dict[str, float]) -> float | None:
    unit = unit.lower()
    if not unit:
        return 1.0
    if unit in table:
        return table[unit]
    return table.get(unit[0])


def _parse_engineering_value(text: str | None, table: dict[str, float], word_re: re.Pattern[str]) -> float | None:
    """Parse a nominal component value ("10k", "4k7", "100nF", "1800 pF",
    "0.1uF", "10 kOhm") into a base-unit float (ohms or farads), so
    schematic Value-field text and Mouser's spec text can be compared
    numerically instead of tripping over formatting differences. Supports
    KiCad's "unit letter as decimal point" shorthand ("4k7" == 4.7k, "2R2" ==
    2.2 ohms). Returns None if nothing recognizable is found - callers must
    treat that as "couldn't verify", never as a mismatch or a zero.
    """
    if not text:
        return None
    cleaned = re.sub(r"[,\s]", "", text)
    cleaned = word_re.sub("", cleaned)
    cleaned = cleaned.replace("Ω", "R").replace("µ", "u")
    cleaned = _TRAILING_TOLERANCE_RE.sub("", cleaned)
    if not cleaned:
        return None

    shorthand = re.match(r"^(\d+)([a-zA-Z]+)(\d+)?$", cleaned)
    if shorthand:
        whole, unit, frac = shorthand.group(1), shorthand.group(2), shorthand.group(3)
        mult = _lookup_unit_multiplier(unit, table)
        if mult is not None:
            try:
                return float(f"{whole}.{frac}" if frac else whole) * mult
            except ValueError:
                pass

    standard = re.match(r"^(\d+(?:\.\d+)?)([a-zA-Z]*)$", cleaned)
    if standard:
        number, unit = standard.group(1), standard.group(2)
        mult = _lookup_unit_multiplier(unit, table)
        if mult is not None:
            try:
                return float(number) * mult
            except ValueError:
                pass
    return None


def _parse_resistance(text: str | None) -> float | None:
    return _parse_engineering_value(text, _RESISTANCE_UNIT_MULTIPLIERS, _OHM_WORD_RE)


def _parse_capacitance(text: str | None) -> float | None:
    return _parse_engineering_value(text, _CAPACITANCE_UNIT_MULTIPLIERS, _FARAD_WORD_RE)


def _extract_package_code_from_footprint(footprint: str | None) -> str | None:
    """Pull the imperial EIA size code straight out of a KiCad footprint
    name, e.g. "Resistor_SMD:R_0402_1005Metric" -> "0402" - the same EIA
    code table _extract_package_code reads Mouser's Case/Package spec
    through, so both sides of the package comparison speak the same
    vocabulary.
    """
    if not footprint:
        return None
    for token in re.findall(r"\d{4,6}", footprint):
        if token in _EIA_IMPERIAL_CODES:
            return token
    return None


def audit_component_specs_against_mouser(
    project_path: str | Path,
    references: list[str] | None = None,
    value_tolerance_pct: float = 1.0,
) -> dict[str, Any]:
    """Cross-check every unique schematic part's Value/Footprint/
    Manufacturer_Part_Number against what Mouser's Search API actually
    returns for that part's own linked Mouser product - catches a Mouser
    link that resolves fine but points at the wrong part (wrong
    resistance/capacitance, wrong package, or a stale/typo'd MPN). This is
    the exact bug class behind the R96/R103 stale-link issue noted in
    todo.md (link pointed at a 47.5kOhm part for a 154k resistor) - run this
    to find any remaining instances of it instead of spotting them by hand.

    Three independent checks per part, each reported "match" / "mismatch" /
    "not_verifiable" (Mouser or the schematic didn't provide enough to
    compare - never a silent pass):
    - manufacturer_part_number: schematic's Manufacturer_Part_Number property
      vs Mouser's MPN. Run normalize_manufacturer_part_number_properties
      first so parts that only carry the MPN under a differently-named
      property are picked up here too.
    - package: EIA imperial size code parsed out of the schematic Footprint
      name vs Mouser's package_size_inch (only meaningful for parts Mouser
      could identify as a chip resistor/ceramic capacitor; anything else is
      "not_verifiable" for this field, not "match").
    - value: nominal resistance/capacitance parsed out of the schematic
      Value field vs Mouser's resistance/capacitance spec, compared
      numerically within `value_tolerance_pct` percent so formatting
      differences ("10k" vs "10 kOhm") don't read as mismatches. Only
      resistors/capacitors (by Mouser's own detected_type) get this check.

    Parts with no Mouser link, or whose lookup fails, are excluded from
    `results` (see skipped/errors, carried through from
    bulk_lookup_mouser_parts). REQUIRES MOUSER_API_KEY.
    """
    bulk = bulk_lookup_mouser_parts(project_path, references=references)

    rows: list[dict[str, Any]] = []
    mismatched: list[dict[str, Any]] = []

    for entry in bulk["results"]:
        mouser = entry["mouser"]

        schematic_mpn = _canonical_mpn_from_properties(entry["schematic_properties"])
        mouser_mpn = mouser.get("manufacturer_part_number")
        mouser_mpn = None if mouser_mpn in (None, "unknown") else mouser_mpn
        if mouser_mpn is None or schematic_mpn is None:
            mpn_status = "not_verifiable"
        elif _normalize_mpn_for_compare(schematic_mpn) == _normalize_mpn_for_compare(mouser_mpn):
            mpn_status = "match"
        else:
            mpn_status = "mismatch"

        mouser_package = mouser.get("package_size_inch")
        schematic_package = _extract_package_code_from_footprint(entry.get("footprint"))
        if mouser_package in (None, "unknown", "unsupported") or schematic_package is None:
            package_status = "not_verifiable"
        elif schematic_package == mouser_package:
            package_status = "match"
        else:
            package_status = "mismatch"

        detected_type = mouser.get("detected_type")
        schematic_numeric = mouser_numeric = None
        mouser_stated = None
        if detected_type == "resistor" and mouser.get("resistance") not in (None, "unknown", "unsupported"):
            mouser_stated = mouser.get("resistance")
            schematic_numeric = _parse_resistance(entry.get("value"))
            mouser_numeric = _parse_resistance(mouser_stated)
        elif detected_type == "capacitor" and mouser.get("capacitance") not in (None, "unknown", "unsupported"):
            mouser_stated = mouser.get("capacitance")
            schematic_numeric = _parse_capacitance(entry.get("value"))
            mouser_numeric = _parse_capacitance(mouser_stated)

        if schematic_numeric is None or mouser_numeric is None:
            value_status = "not_verifiable"
        elif mouser_numeric == 0:
            value_status = "not_verifiable"
        else:
            diff_pct = abs(schematic_numeric - mouser_numeric) / mouser_numeric * 100
            value_status = "match" if diff_pct <= value_tolerance_pct else "mismatch"

        row = {
            "reference": entry["reference"],
            "all_references": entry["all_references"],
            "value": entry["value"],
            "footprint": entry.get("footprint"),
            "mouser_url": entry["mouser_url"],
            "manufacturer_part_number": {"status": mpn_status, "schematic": schematic_mpn, "mouser": mouser_mpn},
            "package": {"status": package_status, "schematic": schematic_package, "mouser": mouser_package},
            "value_check": {
                "status": value_status,
                "schematic_numeric": schematic_numeric,
                "mouser_numeric": mouser_numeric,
                "mouser_stated": mouser_stated,
            },
        }
        rows.append(row)
        if "mismatch" in (mpn_status, package_status, value_status):
            mismatched.append(row)

    return {
        "checked_count": len(rows),
        "mismatched_count": len(mismatched),
        "clean_count": len(rows) - len(mismatched),
        "results": rows,
        "mismatched": mismatched,
        "skipped_count": bulk["skipped_count"],
        "error_count": bulk["error_count"],
        "skipped": bulk["skipped"],
        "errors": bulk["errors"],
    }


def audit_stock_sufficiency(
    project_path: str | Path,
    references: list[str] | None = None,
    board_quantity: int = 1,
) -> dict[str, Any]:
    """Check that at least one candidate Mouser link for every unique
    schematic part (not just its current primary link - every "Mouser"/
    "Mouser Price/Stock"/"Mouser Part Number Alt"/etc field) is in stock for
    enough units to build `board_quantity` board(s), via the same live-data
    ranking optimize_component_mouser_alternates uses. A part whose primary
    link is out of stock but has a working alternate is NOT flagged;
    `insufficient` only lists parts where no candidate link covers the need.

    `board_quantity` multiplies each part's own schematic-placed count (from
    list_schematic_parts) - defaults to 1 board. REQUIRES MOUSER_API_KEY.
    """
    representative_to_group: dict[str, dict[str, Any]] = {}
    if references is None:
        parts = list_schematic_parts(project_path)["parts"]
        references = []
        for part in parts:
            if not part["references"]:
                continue
            representative = part["references"][0]
            references.append(representative)
            representative_to_group[representative] = part

    results: list[dict[str, Any]] = []
    insufficient: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for reference in references:
        try:
            component = get_schematic_part(project_path, reference)
        except KeyError as exc:
            errors.append({"reference": reference, "error": str(exc)})
            continue

        group = representative_to_group.get(reference)
        all_references = group["references"] if group else [reference]
        needed = (group["quantity"] if group else 1) * board_quantity

        if not find_all_mouser_urls(component.get("properties", {})):
            skipped.append(
                {"reference": reference, "all_references": all_references, "value": component.get("value", ""), "reason": "no Mouser link in properties"}
            )
            continue

        try:
            result = optimize_component_mouser_alternates(project_path, reference, quantity_needed=needed)
        except Exception as exc:
            errors.append({"reference": reference, "all_references": all_references, "error": str(exc)})
            continue

        best = result["candidates"][0]
        candidate_summaries = [
            {
                "field_name": c["field_name"],
                "url": c["url"],
                "rank": c["rank"],
                "in_stock_count": c["in_stock_count"],
                "meets_quantity": c["meets_quantity"],
                "has_qty_one_price": c["has_qty_one_price"],
                "unit_price_at_quantity": c["unit_price_at_quantity"],
                "currency": c["currency"],
            }
            for c in result["candidates"]
        ]
        row = {
            "reference": reference,
            "all_references": all_references,
            "value": result["value"],
            "quantity_needed": needed,
            "meets_quantity": best["meets_quantity"],
            "best_candidate_in_stock": best["in_stock_count"],
            "best_candidate_url": best["url"],
            "candidates": candidate_summaries,
        }
        results.append(row)
        if not best["meets_quantity"]:
            insufficient.append(row)

    return {
        "board_quantity": board_quantity,
        "checked_count": len(results),
        "insufficient_count": len(insufficient),
        "insufficient": insufficient,
        "results": results,
        "skipped_count": len(skipped),
        "error_count": len(errors),
        "skipped": skipped,
        "errors": errors,
    }


def audit_schematic_health(
    project_path: str | Path,
    default_capacitor_voltage: str | float,
    references: list[str] | None = None,
    board_quantity: int = 1,
    value_tolerance_pct: float = 1.0,
    report_path: str | Path | None = None,
) -> dict[str, Any]:
    """One-call pre-fab/pre-order sanity pass across the whole schematic,
    combining every check this project has for catching schematic errors
    before parts get ordered or the board gets sent to fab:

    1. audit_schematic_integrity (schematic-only, instant) - duplicate
       reference designators, symbols missing a Value or Footprint.
    2. audit_capacitor_voltages - every capacitor either states its own
       voltage rating or is assumed to use `default_capacitor_voltage`.
       There is no universally-correct default for this project - ASK THE
       USER what voltage rating this design assumes for capacitors that
       don't state one before calling this (Power.kicad_sch/
       Regulators.kicad_sch's rail voltages are a reasonable thing to bring
       up in that conversation, but the actual answer is a project decision,
       not something to guess).
    3. audit_component_specs_against_mouser - each part's Value/Footprint/
       Manufacturer_Part_Number matches what its linked Mouser product
       actually is (catches stale/wrong links like the R96/R103 issue in
       todo.md).
    4. audit_stock_sufficiency - at least one of each part's candidate
       Mouser links is in stock for `board_quantity` board(s) worth.

    Steps 3-4 REQUIRE MOUSER_API_KEY and are the slow, rate-limited part of
    this call - a full ~40-60 unique part schematic can take a couple of
    minutes. Writes a Markdown summary to `report_path` (defaults to
    `schematic_health_report.md` at the project root); full structured
    results are also returned as JSON.
    """
    schematic_dir = Path(list_schematic_parts(project_path)["schematic_dir"])
    output_path = Path(report_path) if report_path is not None else schematic_dir / "schematic_health_report.md"

    integrity = audit_schematic_integrity(project_path)
    voltages = audit_capacitor_voltages(project_path, default_voltage=default_capacitor_voltage)
    voltage_mismatches = [v for v in voltages["with_voltage"] if v["status"] == "differs_from_default"]
    specs = audit_component_specs_against_mouser(project_path, references=references, value_tolerance_pct=value_tolerance_pct)
    stock = audit_stock_sufficiency(project_path, references=references, board_quantity=board_quantity)

    total_issue_count = (
        integrity["duplicate_reference_count"]
        + integrity["missing_value_count"]
        + integrity["missing_footprint_count"]
        + voltages["missing_voltage_count"]
        + len(voltage_mismatches)
        + specs["mismatched_count"]
        + stock["insufficient_count"]
    )

    lines = [
        "# Schematic Health Report",
        "",
        f"**{total_issue_count} issue(s) found.**",
        "",
        "## 1. Schematic Integrity",
        "",
        f"- Duplicate reference designators: {integrity['duplicate_reference_count']}",
        f"- Missing Value field: {integrity['missing_value_count']}",
        f"- Missing Footprint field: {integrity['missing_footprint_count']}",
        "",
    ]
    if integrity["duplicate_references"]:
        lines.append("| Reference | Instance Count | Values | Sheets |")
        lines.append("|---|---|---|---|")
        for row in integrity["duplicate_references"]:
            lines.append(f"| {row['reference']} | {row['instance_count']} | {', '.join(row['values'])} | {', '.join(row['sheetfiles'])} |")
        lines.append("")
    for label, key in (("Missing Value", "missing_value"), ("Missing Footprint", "missing_footprint")):
        rows = integrity[key]
        if rows:
            lines.append(f"### {label}")
            lines.append("")
            lines.append("| Reference | Value | Footprint | Sheet |")
            lines.append("|---|---|---|---|")
            for row in rows:
                lines.append(f"| {row['reference']} | {row['value']} | {row['footprint']} | {row['sheetfile']} |")
            lines.append("")

    lines += [
        "## 2. Capacitor Voltage Ratings",
        "",
        f"Default assumed voltage: **{default_capacitor_voltage}**",
        f"- Missing a stated voltage (assumed default): {voltages['missing_voltage_count']}",
        f"- States a voltage that differs from the default: {len(voltage_mismatches)}",
        "",
    ]
    if voltage_mismatches:
        lines.append("| References | Value | Stated Voltage |")
        lines.append("|---|---|---|")
        for row in voltage_mismatches:
            lines.append(f"| {', '.join(row['references'])} | {row['value']} | {row['stated_voltage']} |")
        lines.append("")

    lines += [
        "## 3. Part Specs vs. Mouser Link",
        "",
        f"Checked {specs['checked_count']} part(s), {specs['mismatched_count']} mismatch(es) "
        f"({specs['skipped_count']} had no Mouser link, {specs['error_count']} failed to look up).",
        "",
    ]
    if specs["mismatched"]:
        lines.append("| References | Value | MPN | Package | Value Check | Mouser Link |")
        lines.append("|---|---|---|---|---|---|")
        for row in specs["mismatched"]:
            lines.append(
                f"| {', '.join(row['all_references'])} | {row['value']} | {row['manufacturer_part_number']['status']} "
                f"| {row['package']['status']} | {row['value_check']['status']} | {row['mouser_url']} |"
            )
        lines.append("")
    else:
        lines.append("None found.")
        lines.append("")

    lines += [
        "## 4. Stock Sufficiency",
        "",
        f"Board quantity: {stock['board_quantity']}. Checked {stock['checked_count']} part(s), "
        f"{stock['insufficient_count']} without any candidate link that covers the need.",
        "",
    ]
    if stock["insufficient"]:
        lines.append("| References | Value | Needed | Best Candidate In Stock | Best Candidate Link |")
        lines.append("|---|---|---|---|---|")
        for row in stock["insufficient"]:
            lines.append(
                f"| {', '.join(row['all_references'])} | {row['value']} | {row['quantity_needed']} | "
                f"{row['best_candidate_in_stock']} | {row['best_candidate_url']} |"
            )
        lines.append("")
    else:
        lines.append("None found.")
        lines.append("")

    output_path.write_text("\n".join(lines), encoding="utf-8")

    return {
        "report_path": str(output_path),
        "total_issue_count": total_issue_count,
        "integrity": integrity,
        "capacitor_voltages": voltages,
        "capacitor_voltage_mismatches": voltage_mismatches,
        "component_specs": specs,
        "stock_sufficiency": stock,
    }
