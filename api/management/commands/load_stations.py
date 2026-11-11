"""
Load geocoded stations from data/stations_enriched.csv into the Station table.

Usage:
    python manage.py load_stations
    python manage.py load_stations --keep    # do not delete existing rows
"""

from __future__ import annotations

import csv
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from api.models import Station


class Command(BaseCommand):
    help = "Load geocoded stations from data/stations_enriched.csv into the database."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--path",
            default="data/stations_enriched.csv",
            help="Path to the enriched CSV, relative to BASE_DIR.",
        )
        parser.add_argument(
            "--keep",
            action="store_true",
            help="Do not delete existing Station rows before loading.",
        )

    def handle(self, *args, **options) -> None:
        path = Path(options["path"])
        if not path.is_absolute():
            path = Path(settings.BASE_DIR) / path
        if not path.exists():
            raise CommandError(f"CSV not found: {path}")

        self.stdout.write(f"Reading {path}")

        to_create: list[Station] = []
        rows_read = 0
        skipped_missing_coords = 0
        skipped_bad_coords = 0
        skipped_bad_price = 0

        with path.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows_read += 1
                lat_raw = (row.get("lat") or "").strip()
                lon_raw = (row.get("lon") or "").strip()
                if not lat_raw or not lon_raw:
                    skipped_missing_coords += 1
                    continue
                try:
                    lat = float(lat_raw)
                    lon = float(lon_raw)
                except ValueError:
                    skipped_bad_coords += 1
                    continue
                price_raw = (row.get("retail_price") or "").strip()
                try:
                    price = Decimal(price_raw)
                except (InvalidOperation, ValueError):
                    skipped_bad_price += 1
                    continue

                to_create.append(
                    Station(
                        opis_id=(row.get("opis_id") or "").strip(),
                        name=(row.get("name") or "").strip(),
                        city=(row.get("city") or "").strip(),
                        state=(row.get("state") or "").strip(),
                        lat=lat,
                        lon=lon,
                        retail_price=price,
                    )
                )

        self.stdout.write(f"Rows read: {rows_read}")
        self.stdout.write(f"Skipped (missing coords): {skipped_missing_coords}")
        self.stdout.write(f"Skipped (bad coords): {skipped_bad_coords}")
        self.stdout.write(f"Skipped (bad price): {skipped_bad_price}")
        self.stdout.write(f"To load: {len(to_create)}")

        with transaction.atomic():
            if not options["keep"]:
                deleted, _ = Station.objects.all().delete()
                self.stdout.write(f"Deleted existing rows: {deleted}")
            Station.objects.bulk_create(
                to_create, batch_size=500, ignore_conflicts=False
            )

        total = Station.objects.count()
        sample = Station.objects.order_by("opis_id")[:3]
        self.stdout.write(self.style.SUCCESS(f"Successfully loaded {total} stations"))
        self.stdout.write("First 3 rows:")
        for s in sample:
            self.stdout.write(
                f"  {s.opis_id}  {s.name}  {s.city}, {s.state}  "
                f"{s.lat},{s.lon}  ${s.retail_price}"
            )