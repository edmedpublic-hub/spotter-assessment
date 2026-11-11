"""
M2 diagnostic: profile the fuel CSV and geocode all records via the
US Census batch geocoder. Zero cost, no API key.

Run from the project root:
    python scripts/profile_and_geocode.py
"""

from __future__ import annotations

import csv
import io
import json
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any

import requests

CSV_PATH = Path("fuel-prices-for-be-assessment.csv")
OUTPUT_DIR = Path("data")
CENSUS_BATCH_URL = (
    "https://geocoding.geo.census.gov/geocoder/locations/addressbatch"
)
BENCHMARK = "Public_AR_Current"
REQUEST_TIMEOUT_SECONDS = 300

RESPONSE_COLUMNS = [
    "opis_id",
    "input_address",
    "match_indicator",
    "match_type",
    "matched_address",
    "coords",
    "tiger_line_id",
    "tiger_side",
]


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip())


def load_and_deduplicate(csv_path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for raw in reader:
            rows.append(
                {
                    "opis_id": _clean(raw.get("OPIS Truckstop ID", "")),
                    "name": _clean(raw.get("Truckstop Name", "")),
                    "street": _clean(raw.get("Address", "")),
                    "city": _clean(raw.get("City", "")),
                    "state": _clean(raw.get("State", "")),
                    "rack_id": _clean(raw.get("Rack ID", "")),
                    "retail_price": _clean(raw.get("Retail Price", "")),
                }
            )

    by_id: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_id.setdefault(row["opis_id"], []).append(row)

    deduped: list[dict[str, Any]] = []
    duplicates: list[dict[str, Any]] = []
    for opis_id, group in by_id.items():
        if len(group) == 1:
            deduped.append(group[0])
            continue
        canonical = max(group, key=lambda r: len(r["name"]))
        prices = sorted({r["retail_price"] for r in group})
        duplicates.append(
            {
                "opis_id": opis_id,
                "count": len(group),
                "names": [r["name"] for r in group],
                "prices": prices,
                "chosen": canonical["name"],
            }
        )
        deduped.append(canonical)

    report = {
        "rows_read": len(rows),
        "rows_after_dedup": len(deduped),
        "duplicate_groups": len(duplicates),
        "duplicate_examples": duplicates[:10],
        "missing_price_count": sum(1 for r in deduped if not r["retail_price"]),
    }
    return deduped, report


def write_census_batch(records: list[dict[str, Any]], path: Path) -> None:
    """
    Census batch format: Unique ID, Street address, City, State, ZIP.
    No header. ZIP is unavailable in the CSV, so the field is left empty.
    """
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        for rec in records:
            writer.writerow(
                [rec["opis_id"], rec["street"], rec["city"], rec["state"], ""]
            )


def submit_to_census(batch_path: Path, raw_path: Path) -> bytes:
    with batch_path.open("rb") as f:
        files = {"addressFile": (batch_path.name, f, "text/csv")}
        data = {"benchmark": BENCHMARK}
        response = requests.post(
            CENSUS_BATCH_URL,
            files=files,
            data=data,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    response.raise_for_status()
    raw_path.write_bytes(response.content)
    return response.content


def parse_census_response(content: bytes) -> list[dict[str, str]]:
    """
    Parse the Census CSV response leniently. Do NOT filter by column count:
    unmatched records may have fewer than 8 fields, and dropping them
    silently distorts the match rate.
    """
    text = content.decode("utf-8-sig")
    reader = csv.reader(io.StringIO(text))
    parsed: list[dict[str, str]] = []
    for row in reader:
        if not row or not row[0].strip():
            continue
        padded = row + [""] * (len(RESPONSE_COLUMNS) - len(row))
        record = dict(zip(RESPONSE_COLUMNS, padded[: len(RESPONSE_COLUMNS)]))
        record["_column_count"] = str(len(row))
        parsed.append(record)
    return parsed


def summarise(
    results: list[dict[str, str]], records_submitted: int
) -> dict[str, Any]:
    indicator_counts = Counter(
        (r["match_indicator"].strip() or "<blank>") for r in results
    )
    matched = [r for r in results if r["match_indicator"].strip().lower() == "match"]
    unmatched = [r for r in results if r not in matched]
    column_counts = Counter(r["_column_count"] for r in results)

    return {
        "records_submitted_to_census": records_submitted,
        "response_rows_parsed": len(results),
        "response_looks_truncated": len(results) < records_submitted,
        "matched": len(matched),
        "unmatched": len(unmatched),
        "match_rate_pct": round(100 * len(matched) / records_submitted, 2)
        if records_submitted
        else 0.0,
        "match_indicator_distribution": dict(indicator_counts),
        "response_column_count_distribution": dict(column_counts),
        "unmatched_examples": [
            {
                "opis_id": r["opis_id"],
                "input_address": r["input_address"],
                "match_indicator": r["match_indicator"],
            }
            for r in unmatched[:10]
        ],
    }


def main() -> None:
    OUTPUT_DIR.mkdir(exist_ok=True)

    if not CSV_PATH.exists():
        raise SystemExit(
            f"CSV not found at {CSV_PATH.resolve()}. "
            "Place fuel-prices-for-be-assessment.csv in the project root."
        )

    deduped, profile = load_and_deduplicate(CSV_PATH)
    batch_path = OUTPUT_DIR / "census_batch_input.csv"
    raw_path = OUTPUT_DIR / "census_raw_response.csv"
    write_census_batch(deduped, batch_path)

    started = time.perf_counter()
    print(f"Submitting {len(deduped)} records to Census batch geocoder...")
    content = submit_to_census(batch_path, raw_path)
    elapsed = time.perf_counter() - started

    results = parse_census_response(content)
    summary = summarise(results, records_submitted=len(deduped))

    by_id = {r["opis_id"]: r for r in results}
    geocoded: list[dict[str, Any]] = []
    for rec in deduped:
        match = by_id.get(rec["opis_id"], {})
        coords = match.get("coords", "")
        lon = lat = None
        if coords:
            parts = coords.split(",")
            if len(parts) == 2:
                try:
                    lon, lat = float(parts[0]), float(parts[1])
                except ValueError:
                    lon = lat = None
        geocoded.append(
            {
                **rec,
                "lon": lon,
                "lat": lat,
                "census_match_indicator": match.get("match_indicator", ""),
                "census_match_type": match.get("match_type", ""),
                "census_matched_address": match.get("matched_address", ""),
            }
        )

    report = {
        "profile": profile,
        "geocoding": {
            **summary,
            "census_request_seconds": round(elapsed, 2),
            "benchmark": BENCHMARK,
        },
    }

    (OUTPUT_DIR / "geocoded_stations.json").write_text(
        json.dumps(geocoded, indent=2), encoding="utf-8"
    )
    (OUTPUT_DIR / "m2_data_quality_report.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )

    print(json.dumps(report["geocoding"], indent=2))


if __name__ == "__main__":
    main()