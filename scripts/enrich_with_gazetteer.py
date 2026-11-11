"""
M2b: join deduplicated station records to the 2026 U.S. Census Gazetteer
Places file using normalised City + State. Zero API calls, zero cost.

Input:  data/geocoded_stations.json   (produced by profile_and_geocode.py)
Output: data/stations_enriched.csv    (input for Django model in M5)

Run from the project root:
    python scripts/enrich_with_gazetteer.py
"""

from __future__ import annotations

import csv
import json
import re
import zipfile
from pathlib import Path
from typing import Any

import requests

DATA_DIR = Path("data")
INPUT_JSON = DATA_DIR / "geocoded_stations.json"
OUTPUT_CSV = DATA_DIR / "stations_enriched.csv"
GAZETTEER_ZIP_URL = (
    "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/"
    "2026_Gazetteer/2026_Gaz_place_national.zip"
)
GAZETTEER_ZIP_PATH = DATA_DIR / "2026_Gaz_place_national.zip"
GAZETTEER_TXT_PATH = DATA_DIR / "2026_Gaz_place_national.txt"

# LSAD type designators appended by the Census to place names.
# Longest first so that multi-word designators are tried before their
# substrings (e.g. "city and county" before "county").
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


def _norm(value: str) -> str:
    """Normalise a city/state string for matching: lowercase, alphanumeric only."""
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def _strip_lsad(name: str) -> str:
    """Remove the trailing LSAD designator, e.g. 'Tomah city' -> 'Tomah'."""
    name = name.strip()
    lower = name.lower()
    for suffix in _LSAD_SUFFIXES:
        if lower.endswith(" " + suffix):
            return name[: -(len(suffix) + 1)].strip()
    return name


def download_gazetteer() -> None:
    if GAZETTEER_TXT_PATH.exists():
        print(f"Gazetteer already extracted at {GAZETTEER_TXT_PATH}")
        return

    print(f"Downloading Gazetteer from {GAZETTEER_ZIP_URL}")
    response = requests.get(GAZETTEER_ZIP_URL, timeout=120)
    response.raise_for_status()
    GAZETTEER_ZIP_PATH.write_bytes(response.content)

    print(f"Extracting {GAZETTEER_ZIP_PATH}")
    with zipfile.ZipFile(GAZETTEER_ZIP_PATH) as zf:
        names = zf.namelist()
        txt_names = [n for n in names if n.lower().endswith(".txt")]
        if not txt_names:
            raise RuntimeError(f"No .txt file in zip. Contents: {names}")
        zf.extract(txt_names[0], DATA_DIR)
        extracted = DATA_DIR / txt_names[0]
        if extracted != GAZETTEER_TXT_PATH:
            extracted.replace(GAZETTEER_TXT_PATH)
    print(f"Gazetteer ready at {GAZETTEER_TXT_PATH}")


def load_gazetteer() -> dict[str, tuple[float, float]]:
    """
    Return a dict keyed by (normalised city, normalised state) -> (lat, lon).
    The Census NAME column includes an LSAD type designator
    (e.g. 'Tomah city', 'Gila Bend town', 'Big Cabin CDP'), which must be
    stripped before joining to plain City values from the station CSV.
    """
    lookup: dict[str, tuple[float, float]] = {}
    sample_names: list[str] = []
    with GAZETTEER_TXT_PATH.open("r", encoding="utf-8", errors="replace") as f:
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
            clean_name = _strip_lsad(raw_name)
            if not clean_name:
                continue
            if len(sample_names) < 5:
                sample_names.append(f"{raw_name!r} -> {clean_name!r}")
            key = f"{_norm(clean_name)}|{_norm(state)}"
            lookup.setdefault(key, (lat, lon))
    print(f"Loaded {len(lookup)} gazetteer places")
    print("Sample raw NAME -> stripped NAME:")
    for s in sample_names:
        print(f"  {s}")
    return lookup


def load_stations() -> list[dict[str, Any]]:
    if not INPUT_JSON.exists():
        raise SystemExit(
            f"Input not found: {INPUT_JSON.resolve()}. "
            "Run scripts/profile_and_geocode.py first."
        )
    return json.loads(INPUT_JSON.read_text(encoding="utf-8"))


def join_stations(
    stations: list[dict[str, Any]], gazetteer: dict[str, tuple[float, float]]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    matched = 0
    unmatched_examples: list[dict[str, str]] = []

    for rec in stations:
        city = rec.get("city", "")
        state = rec.get("state", "")
        key = f"{_norm(city)}|{_norm(state)}"
        coords = gazetteer.get(key)
        if coords:
            lat, lon = coords
            matched += 1
        else:
            lat = lon = None
            if len(unmatched_examples) < 10:
                unmatched_examples.append(
                    {"opis_id": rec.get("opis_id", ""), "city": city, "state": state}
                )
        enriched.append(
            {
                "opis_id": rec.get("opis_id", ""),
                "name": rec.get("name", ""),
                "city": city,
                "state": state,
                "lat": lat,
                "lon": lon,
                "retail_price": rec.get("retail_price", ""),
            }
        )

    report = {
        "total_stations": len(stations),
        "matched_to_gazetteer": matched,
        "unmatched": len(stations) - matched,
        "match_rate_pct": round(100 * matched / len(stations), 2)
        if stations
        else 0.0,
        "unmatched_examples": unmatched_examples,
    }
    return enriched, report


def main() -> None:
    DATA_DIR.mkdir(exist_ok=True)

    download_gazetteer()
    gazetteer = load_gazetteer()
    stations = load_stations()

    enriched, report = join_stations(stations, gazetteer)

    with OUTPUT_CSV.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "opis_id",
                "name",
                "city",
                "state",
                "lat",
                "lon",
                "retail_price",
            ],
        )
        writer.writeheader()
        writer.writerows(enriched)

    print(json.dumps(report, indent=2))
    print(f"\nWrote {OUTPUT_CSV} ({len(enriched)} rows)")


if __name__ == "__main__":
    main()