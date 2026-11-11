"""
City + state → (lat, lon) lookup against the 2026 US Census Gazetteer.

Pure Python: no Django, no network, no external dependencies beyond the
already-downloaded Gazetteer text file. Loads lazily on first call and
caches the result in module scope.

Public API:
    lookup(city, state) -> (lat, lon)
    Raises UnknownCityError if the (city, state) pair is not in the Gazetteer.

    parse_city_state(origin_string) -> (city, state)
    Raises InvalidLocationError if the string cannot be parsed as
    "City, ST" or "City, StateName".
"""

from __future__ import annotations

import csv
import re
import threading
from pathlib import Path

from django.conf import settings

# LSAD suffixes in the Census Gazetteer NAME column, e.g. "Tomah city".
# Longest first so multi-word designators match before their substrings.
_LSAD_SUFFIXES = sorted(
    [
        "consolidated government (balance)",
        "metropolitan government (balance)",
        "unified government (balance)",
        "urban county (balance)",
        "city (balance)",
        "city and borough",
        "city and county",
        "consolidated government",
        "metropolitan government",
        "unified government",
        "urban county",
        "municipality",
        "borough",
        "village",
        "town",
        "cdp",
        "city",
        "county",
        "(balance)",
    ],
    key=len,
    reverse=True,
)

# Full state name -> USPS abbreviation, so "Kansas City, Missouri" works too.
_STATE_NAMES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE",
    "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ",
    "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR",
    "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "district of columbia": "DC",
}

_VALID_STATE_ABBRS = set(_STATE_NAMES.values())

# Gazetteer file: same one used during M2 ingestion.
GAZETTEER_PATH = Path(settings.BASE_DIR) / "data" / "2026_Gaz_place_national.txt"

_lock = threading.Lock()
_cache: dict[tuple[str, str], tuple[float, float]] | None = None


class UnknownCityError(ValueError):
    """The (city, state) pair does not exist in the Gazetteer."""


class InvalidLocationError(ValueError):
    """The input string could not be parsed as 'City, ST'."""


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def _strip_lsad(name: str) -> str:
    name = name.strip()
    lower = name.lower()
    for suffix in _LSAD_SUFFIXES:
        if lower.endswith(" " + suffix):
            return name[: -(len(suffix) + 1)].strip()
    return name


def _build_cache() -> dict[tuple[str, str], tuple[float, float]]:
    if not GAZETTEER_PATH.exists():
        raise FileNotFoundError(
            f"Gazetteer file missing: {GAZETTEER_PATH}. "
            "Run scripts/enrich_with_gazetteer.py to download it."
        )
    cache: dict[tuple[str, str], tuple[float, float]] = {}
    with GAZETTEER_PATH.open("r", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f, delimiter="|")
        reader.fieldnames = [c.strip() for c in reader.fieldnames or []]
        for row in reader:
            state = (row.get("USPS") or "").strip()
            raw_name = (row.get("NAME") or "").strip()
            lat_raw = (row.get("INTPTLAT") or "").strip()
            lon_raw = (row.get("INTPTLONG") or "").strip()
            if not state or not raw_name or not lat_raw or not lon_raw:
                continue
            try:
                lat = float(lat_raw)
                lon = float(lon_raw)
            except ValueError:
                continue
            clean = _strip_lsad(raw_name)
            if not clean:
                continue
            key = (_norm(clean), _norm(state))
            cache.setdefault(key, (lat, lon))
    return cache


def _get_cache() -> dict[tuple[str, str], tuple[float, float]]:
    global _cache
    if _cache is None:
        with _lock:
            if _cache is None:
                _cache = _build_cache()
    return _cache


def lookup(city: str, state: str) -> tuple[float, float]:
    """Return (lat, lon) for the given city and USPS two-letter state."""
    city_n = _norm(city)
    state_n = _norm(state)
    if not city_n or not state_n:
        raise InvalidLocationError("City and state are both required.")
    coords = _get_cache().get((city_n, state_n))
    if coords is None:
        raise UnknownCityError(
            f"City not found in Gazetteer: {city!r}, {state!r}"
        )
    return coords


def parse_city_state(value: str) -> tuple[str, str]:
    """
    Parse 'City, ST' or 'City, StateName' or 'City ST'. Returns (city, state_abbr).

    Rules:
      - Exactly one comma separates city from state.
      - State portion must be a two-letter USPS code or a full state name.
      - Leading/trailing whitespace is stripped.
    """
    if not value or not isinstance(value, str):
        raise InvalidLocationError("Location must be a non-empty string.")
    text = value.strip()
    if "," not in text:
        raise InvalidLocationError(
            f"Location must be in the form 'City, ST': got {value!r}"
        )
    city_part, _, state_part = text.rpartition(",")
    city = city_part.strip()
    state_raw = state_part.strip()
    if not city:
        raise InvalidLocationError(f"City is empty in {value!r}")
    if not state_raw:
        raise InvalidLocationError(f"State is empty in {value!r}")

    if len(state_raw) == 2 and state_raw.upper() in _VALID_STATE_ABBRS:
        return city, state_raw.upper()

    full = _STATE_NAMES.get(state_raw.lower())
    if full is not None:
        return city, full

    raise InvalidLocationError(
        f"Unrecognised US state {state_raw!r}. "
        "Use a two-letter code (e.g. 'MO') or a full state name."
    )