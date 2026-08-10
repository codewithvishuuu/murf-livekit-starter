"""Healthcare facility lookup for the Aarogya Sahayak voice agent.

Queries the OpenStreetMap Overpass API for healthcare facilities in an
Indian district (government health centres, PHCs, CHCs, hospitals, clinics,
dispensaries, sub-centres). The data comes from community-maintained
OpenStreetMap and is never government-verified; the module never invents
facilities, and every failure produces a truthful spoken fallback string.

The module is deliberately failure-tolerant: all errors (timeouts, network
failures, rate limits, invalid responses) are logged and surfaced as a
natural fallback message so the voice conversation can always continue.

Typed lookups run in two light phases: first on the specific (rare-key) type
tags, then topped up by client-side name classification from a broad
hospital/clinic/centre query. No server-side name-regex scans are used, which
public Overpass mirrors time out on for large districts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
from typing import Any

import httpx

logger = logging.getLogger("health_facilities")

OVERPASS_PRIMARY_URL = "https://overpass-api.de/api/interpreter"
OVERPASS_BACKUP_URL = "https://overpass.kumi.systems/api/interpreter"

_USER_AGENT = "AarogyaSahayakVoiceAgent/1.0 (healthcare facility lookup)"

# The Overpass server applies this same budget server-side ([timeout:...]).
OVERPASS_QUERY_TIMEOUT_S = 20
# How many elements Overpass is asked to return. Tag-only filters are cheap,
# and the extra headroom lets client-side type classification and the
# government/private split find enough facilities to report.
OVERPASS_RESULT_LIMIT = 60
# Per-request client timeout (tunable via HEALTH_FACILITIES_TIMEOUT_S).
_DEFAULT_REQUEST_TIMEOUT_S = 10.0
# Total budget across the primary attempt, the backup retry, and parsing.
_RETRY_OVERHEAD_S = 10.0

MAX_RESULTS = 5
# How long a successful lookup stays cached, to avoid hammering Overpass.
CACHE_TTL_S = 300.0


class OverpassUnavailableError(Exception):
    """Raised once the query cannot run (transport, server, or shape errors)."""


def request_timeout_s() -> float:
    try:
        return max(
            1.0,
            float(
                os.environ.get(
                    "HEALTH_FACILITIES_TIMEOUT_S", _DEFAULT_REQUEST_TIMEOUT_S
                )
            ),
        )
    except ValueError:
        return _DEFAULT_REQUEST_TIMEOUT_S


def lookup_total_timeout_s() -> float:
    """Upper bound for the whole lookup (both phases and both endpoints)."""
    return request_timeout_s() * 3 + _RETRY_OVERHEAD_S


# ---------------------------------------------------------------------------
# Facility type handling
# ---------------------------------------------------------------------------

# Canonical types the agent can ask for, mapped to OSM tag selectors.
# Only rare-key tag filters are sent to Overpass (cheap for the server);
# name-based classification happens client-side in Python.
FACILITY_TYPES: dict[str, tuple[tuple[str, str], ...]] = {
    "hospital": (
        ("amenity", "hospital"),
        ("healthcare", "hospital"),
    ),
    "phc": (
        ("health_amenity_type", "PHC"),
        ("health_facility:type", "PHC"),
    ),
    "chc": (
        ("health_amenity_type", "CHC"),
        ("health_facility:type", "CHC"),
    ),
    "clinic": (
        ("amenity", "clinic"),
        ("healthcare", "clinic"),
    ),
    "sub-centre": (
        ("health_amenity_type", "SC"),
        ("health_facility:type", "sub_centre"),
    ),
    "dispensary": (
        ("health_facility:type", "dispensary"),
        ("health_amenity_type", "dispensary"),
    ),
}

_TYPE_ALIASES = {
    "government hospital": "hospital",
    "govt hospital": "hospital",
    "private hospital": "hospital",
    "primary health centre": "phc",
    "primary health center": "phc",
    "community health centre": "chc",
    "community health center": "chc",
    "sub centre": "sub-centre",
    "sub center": "sub-centre",
    "sub-center": "sub-centre",
    "subcentre": "sub-centre",
    "subcenter": "sub-centre",
}

_GOVERNMENT_KEYWORDS = {
    "government",
    "govt",
    "public",
    "sarkari",
    "सरकारी",
}

_SUPPORTED_TYPES_LABEL = "hospital, PHC, CHC, clinic, sub-centre, and dispensary"


def normalize_facility_type(value: str | None) -> tuple[str, bool] | None:
    """Map a user-friendly type to ``(canonical_type, government_only)``.

    Returns ``None`` for unsupported types so the caller can fall back to a
    polite "unsupported type" message.
    """
    if not value:
        return None
    raw = value.strip().lower().replace("_", " ").replace("-", " ")
    raw = " ".join(raw.split())
    government_only = any(word in raw for word in _GOVERNMENT_KEYWORDS)
    for word in _GOVERNMENT_KEYWORDS:
        raw = raw.replace(f"{word} ", "")
        raw = raw.replace(f" {word}", "")
    raw = raw.strip()
    if raw in _TYPE_ALIASES:
        raw = _TYPE_ALIASES[raw]
    if raw in FACILITY_TYPES:
        return raw, government_only
    return None


# ---------------------------------------------------------------------------
# Overpass queries
# ---------------------------------------------------------------------------

_BROAD_LINES = [
    'nwr(area.d)["amenity"="hospital"];',
    'nwr(area.d)["healthcare"="hospital"];',
    'nwr(area.d)["amenity"="clinic"];',
    'nwr(area.d)["healthcare"="clinic"];',
    'nwr(area.d)["healthcare"="centre"];',
]


def _query_for(district: str, lines: list[str]) -> str:
    district_quoted = json.dumps(district, ensure_ascii=False)
    union = "\n  ".join(lines)
    return (
        f"[out:json][timeout:{OVERPASS_QUERY_TIMEOUT_S}];\n"
        f'area["name"={district_quoted}]["boundary"~"administrative"]->.d;\n'
        f"(\n  {union}\n);\n"
        f"out center {OVERPASS_RESULT_LIMIT};"
    )


def build_query(district: str, facility_type: str | None) -> str:
    """District-level Overpass query limited to the type's rare-key tags."""
    lines: list[str] = []
    selectors = FACILITY_TYPES.get(facility_type) if facility_type else None
    if selectors:
        lines.extend(
            f'nwr(area.d)["{key}"={json.dumps(value, ensure_ascii=False)}];'
            for key, value in selectors
        )
    else:
        lines.extend(_BROAD_LINES)
    return _query_for(district, lines)


def build_broad_query(district: str) -> str:
    """District-level Overpass query covering all facility kinds."""
    return _query_for(district, list(_BROAD_LINES))


# ---------------------------------------------------------------------------
# HTTP layer (primary endpoint, then one retry on the backup mirror)
# ---------------------------------------------------------------------------


async def _do_request(url: str, query: str) -> dict[str, Any]:
    """POST one Overpass query and return the parsed JSON.

    Raises ``httpx.HTTPError`` for transport/status failures and
    ``ValueError`` for unparseable JSON.
    """
    async with httpx.AsyncClient(
        timeout=request_timeout_s(),
        headers={"User-Agent": _USER_AGENT},
        follow_redirects=True,
    ) as client:
        response = await client.post(url, data={"data": query})
        response.raise_for_status()
        return response.json()


async def _post_overpass(query: str) -> dict[str, Any]:
    """Query Overpass with one retry against the backup mirror.

    Raises ``OverpassUnavailableError`` when both endpoints fail.
    """
    last_error: Exception | None = None
    for url in (OVERPASS_PRIMARY_URL, OVERPASS_BACKUP_URL):
        try:
            return await _do_request(url, query)
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("overpass request failed (endpoint=%s): %s", url, exc)
            last_error = exc
    raise OverpassUnavailableError(f"overpass unavailable: {last_error}")


async def _query(query: str) -> tuple[list[dict[str, Any]], str | None]:
    """Run one query; return ``(elements, timestamp_osm_base)``.

    Raises ``OverpassUnavailableError`` for transport failures and for
    responses whose shape is not the expected JSON element list.
    """
    response = await _post_overpass(query)
    if not isinstance(response, dict) or not isinstance(response.get("elements"), list):
        raise OverpassUnavailableError("invalid overpass response structure")
    osm3s = response.get("osm3s") if isinstance(response.get("osm3s"), dict) else {}
    raw_timestamp = osm3s.get("timestamp_osm_base")
    timestamp = str(raw_timestamp).strip() if raw_timestamp else None
    return response["elements"], timestamp


# ---------------------------------------------------------------------------
# Result selection and formatting
# ---------------------------------------------------------------------------

_NAME_TYPE_PATTERNS = (
    (r"\bphc\b|primary health cent", "PHC"),
    (r"\bchc\b|community health cent", "CHC"),
    (r"\bdispensary\b", "dispensary"),
    (r"sub[ -]?center|sub[ -]?centre", "sub-centre"),
)

_GOVERNMENT_NAME_PATTERN = re.compile(
    r"\b(govt|government|sarkari|civil hospital|district hospital|esi)\b",
    re.IGNORECASE,
)


def _facility_label(tags: dict[str, str], default: str) -> str:
    kind = (
        tags.get("health_amenity_type")
        or tags.get("health_facility:type")
        or tags.get("healthcare")
        or tags.get("amenity")
        or ""
    )
    lowered = kind.lower()
    if lowered in ("phc", "bphc"):
        return "PHC"
    if lowered == "chc":
        return "CHC"
    if lowered == "sc":
        return "sub-centre"
    if "dispensary" in lowered:
        return "dispensary"
    if "sub centre" in lowered or "sub-center" in lowered:
        return "sub-centre"
    name = tags.get("name", "").lower()
    for pattern, label in _NAME_TYPE_PATTERNS:
        if re.search(pattern, name):
            return label
    return kind or default


def _is_government(tags: dict[str, str]) -> bool:
    operator_type = (tags.get("operator:type") or "").lower()
    if any(word in operator_type for word in ("government", "govt", "public")):
        return True
    operator = (tags.get("operator") or "").lower()
    if any(
        word in operator for word in ("government", "govt", "public", "state health")
    ):
        return True
    name = tags.get("name", "")
    return bool(_GOVERNMENT_NAME_PATTERN.search(name))


def _locality(tags: dict[str, str]) -> str:
    for key in (
        "addr:subdistrict",
        "addr:block",
        "addr:place",
        "addr:village",
        "addr:city",
    ):
        if tags.get(key):
            return tags[key]
    return ""


def _phone(tags: dict[str, str]) -> str:
    return tags.get("contact:phone") or tags.get("phone") or ""


def _select_facilities(
    elements: list[dict[str, Any]],
    facility_type: str | None,
    government_only: bool,
) -> list[dict[str, Any]]:
    """Keep named, on-topic, de-duplicated facilities (max ``MAX_RESULTS``)."""
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for element in elements:
        tags = element.get("tags") or {}
        name = tags.get("name", "").strip()
        if not name:
            continue
        label = _facility_label(tags, facility_type or "healthcare facility")
        if facility_type and label.lower() != facility_type:
            continue
        if government_only and not _is_government(tags):
            continue
        dedup_key = name.lower()
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        selected.append(element)
        if len(selected) >= MAX_RESULTS:
            break
    return selected


def _merge_facilities(
    primary: list[dict[str, Any]],
    more: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Append ``more`` entries whose names were not already selected."""
    seen = {fac["tags"]["name"].lower() for fac in primary}
    for element in more:
        name = element["tags"]["name"].lower()
        if name in seen:
            continue
        seen.add(name)
        primary.append(element)
        if len(primary) >= MAX_RESULTS:
            break
    return primary


def _format_result(
    facilities: list[dict[str, Any]],
    district: str,
    location: str | None,
    timestamp: str | None,
) -> str:
    """Turn selected Overpass elements into a spoken-friendly summary string."""
    if not facilities:
        return (
            "I couldn't find any matching healthcare facilities for this "
            "district. Public mapping data may not cover this area yet, so "
            "please confirm with your District Health Office or a nearby "
            "government hospital."
        )

    rendered = []
    for element in facilities:
        tags = element["tags"]
        name = tags.get("name", "").strip()
        label = _facility_label(tags, "healthcare facility")
        parts = [f"{name} — type: {label}"]
        if _is_government(tags):
            parts.append("operator: government")
        elif tags.get("operator:type"):
            parts.append(f"operator: {tags['operator:type'].lower().replace('_', ' ')}")
        locality = _locality(tags)
        if locality:
            parts.append(f"locality: {locality}")
        phone = _phone(tags)
        if phone:
            parts.append(f"phone: {phone}")
        rendered.append(", ".join(parts))

    prefix = f"Found {len(rendered)} healthcare facilit" + (
        "ies" if len(rendered) > 1 else "y"
    )
    scope = f" in {location}" if location else ""
    header = f"{prefix}{scope} in {district}."
    numbered = "\n".join(
        f"{i}. {facility}" for i, facility in enumerate(rendered, start=1)
    )
    if timestamp:
        freshness = f"The facility data was last refreshed on {timestamp}."
    else:
        freshness = (
            "This information comes from community-maintained public mapping "
            "data and may not be fully up to date. Please confirm with the "
            "facility before travelling."
        )
    return f"{header}\n{numbered}\n{freshness}"


# ---------------------------------------------------------------------------
# Lightweight cache (avoids repeated identical requests within a few minutes)
# ---------------------------------------------------------------------------

_cache_lock = threading.Lock()
_cache: dict[tuple[str, str | None, str | None, bool], tuple[float, str]] = {}


def _cached_search(key: tuple[str, str | None, str | None, bool]) -> str | None:
    with _cache_lock:
        entry = _cache.get(key)
        if entry and entry[0] > time.monotonic():
            return entry[1]
        if entry:
            del _cache[key]
    return None


def _cache_put(key: tuple[str, str | None, str | None, bool], result: str) -> None:
    with _cache_lock:
        _cache[key] = (time.monotonic() + CACHE_TTL_S, result)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def _lookup_facilities(
    district: str,
    facility_type: str,
    government_only: bool,
) -> tuple[list[dict[str, Any]], str | None]:
    """Typed lookups: type-tag query first, name-classified top-up second."""
    elements, timestamp = await _query(build_query(district, facility_type))
    facilities = _select_facilities(elements, facility_type, government_only)
    if len(facilities) >= MAX_RESULTS:
        return facilities, timestamp
    try:
        elements, broad_timestamp = await _query(build_broad_query(district))
    except (OverpassUnavailableError, asyncio.TimeoutError):
        if facilities:
            return facilities, timestamp
        raise
    more = _select_facilities(elements, facility_type, government_only)
    return _merge_facilities(facilities, more), broad_timestamp or timestamp


async def search_health_facilities(
    district: str,
    location: str | None = None,
    facility_type: str | None = None,
) -> str:
    """Return a spoken summary of healthcare facilities in ``district``.

    Never raises and never invents facilities: every failure mode returns a
    truthful fallback message.
    """
    district = (district or "").strip()
    if not district:
        return "I need a district name to look up healthcare facilities. Which district are you in?"

    normalized = normalize_facility_type(facility_type)
    if facility_type and normalized is None:
        return (
            f"{facility_type} is not a supported facility type. I can look up "
            f"{_SUPPORTED_TYPES_LABEL}."
        )
    canonical_type, government_only = normalized if normalized else (None, False)

    key = (district.lower(), location, canonical_type or "any", government_only)
    cached = _cached_search(key)
    if cached is not None:
        logger.info("health facility lookup served from cache (district=%s)", district)
        return cached

    try:
        if canonical_type:
            facilities, timestamp = await asyncio.wait_for(
                _lookup_facilities(district, canonical_type, government_only),
                timeout=lookup_total_timeout_s(),
            )
        else:
            elements, timestamp = await asyncio.wait_for(
                _query(build_broad_query(district)),
                timeout=lookup_total_timeout_s(),
            )
            facilities = _select_facilities(elements, None, government_only)
    except (OverpassUnavailableError, asyncio.TimeoutError) as exc:
        logger.warning(
            "health facility lookup unavailable (district=%s): %s", district, exc
        )
        return (
            f"I'm sorry, I couldn't look up healthcare facilities in {district} "
            f"right now. The facility data service is temporarily unavailable. "
            f"Please try again in a few minutes."
        )

    result = _format_result(facilities, district, location, timestamp)
    _cache_put(key, result)
    return result
