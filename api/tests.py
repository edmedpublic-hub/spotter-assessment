"""
Test suite for the route-and-fuel API.

Three tiers:
  - TestOptimization: pure algorithm tests (no DB, no network).
  - TestCityLookup: input parsing and Gazetteer lookup.
  - TestRouteAPI: HTTP-level integration with OSRM mocked.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APITestCase

from api.models import Station
from api.services.city_lookup import (
    InvalidLocationError,
    UnknownCityError,
    lookup,
    parse_city_state,
)
from api.services.matching import MatchedStation
from api.services.optimization import optimize_fuel_stops
from api.services.routing import RouteResult

GAZETTEER_PATH = Path(settings.BASE_DIR) / "data" / "2026_Gaz_place_national.txt"


def _station(pos: float, price: float, opis_id: str = "x") -> MatchedStation:
    return MatchedStation(
        opis_id=opis_id,
        name=f"Station {opis_id}",
        city="Test",
        state="MO",
        lat=39.0,
        lon=-94.0,
        retail_price=price,
        distance_to_route_miles=0.5,
        distance_along_route_miles=pos,
    )


class TestOptimization(TestCase):
    """Algorithm tests. Each corresponds to an employer-stated constraint."""

    def test_short_route_no_purchases_needed(self):
        # Full tank covers 500 mi; a 300-mi route needs nothing.
        result = optimize_fuel_stops([_station(100, 3.0)], 300.0)
        self.assertTrue(result.feasible)
        self.assertEqual(len(result.purchases), 0)
        self.assertEqual(result.total_cost_usd, 0)

    def test_single_stop_required(self):
        # 700-mi route, one station at mile 400: must stop.
        result = optimize_fuel_stops([_station(400, 3.0)], 700.0)
        self.assertTrue(result.feasible)
        self.assertEqual(len(result.purchases), 1)
        self.assertLessEqual(result.max_leg_miles, 500)

    def test_multiple_stops(self):
        # 1500-mi route, stations every 200 mi: multiple stops, all legs legal.
        stations = [_station(float(m), 3.0) for m in range(200, 1400, 200)]
        result = optimize_fuel_stops(stations, 1500.0)
        self.assertTrue(result.feasible)
        self.assertGreaterEqual(len(result.purchases), 2)
        self.assertLessEqual(result.max_leg_miles, 500)

    def test_infeasible_when_no_station_reachable(self):
        # 1000-mi route, no stations: must fail clearly, not fabricate.
        result = optimize_fuel_stops([], 1000.0)
        self.assertFalse(result.feasible)
        self.assertIsNotNone(result.reason)

    def test_cheaper_station_is_preferred(self):
        # 600-mi route, cheap station at 400 and expensive at 300.
        # Cheaper station should be used; expensive one only if unavoidable.
        stations = [
            _station(300, 4.0, "expensive"),
            _station(400, 2.5, "cheap"),
        ]
        result = optimize_fuel_stops(stations, 600.0)
        self.assertTrue(result.feasible)
        purchased_ids = [p.station_opis_id for p in result.purchases]
        self.assertIn("cheap", purchased_ids)

    def test_station_exactly_at_max_range_is_reachable(self):
        # 900-mi route, station exactly at 500 mi: must be treated as reachable (≤).
        result = optimize_fuel_stops([_station(500, 3.0)], 900.0)
        self.assertTrue(result.feasible)
        self.assertLessEqual(result.max_leg_miles, 500.0)

    def test_zero_distance_route_is_infeasible(self):
        result = optimize_fuel_stops([_station(100, 3.0)], 0.0)
        self.assertFalse(result.feasible)

    def test_total_cost_equals_sum_of_purchases(self):
        # Arithmetic identity: total_cost = Σ gallon × price at each stop.
        stations = [_station(float(m), 3.0 + i * 0.1) for i, m in enumerate(range(200, 1400, 200))]
        result = optimize_fuel_stops(stations, 1500.0)
        self.assertTrue(result.feasible)
        computed = sum(p.cost_usd for p in result.purchases)
        self.assertAlmostEqual(computed, result.total_cost_usd, places=2)


class TestCityLookup(TestCase):
    """Input parsing and Gazetteer lookup."""

    def test_parse_simple(self):
        city, state = parse_city_state("Kansas City, MO")
        self.assertEqual(city, "Kansas City")
        self.assertEqual(state, "MO")

    def test_parse_full_state_name(self):
        _, state = parse_city_state("Kansas City, Missouri")
        self.assertEqual(state, "MO")

    def test_parse_no_comma_raises(self):
        with self.assertRaises(InvalidLocationError):
            parse_city_state("Kansas City MO")

    def test_parse_invalid_state_raises(self):
        with self.assertRaises(InvalidLocationError):
            parse_city_state("Kansas City, ZZ")

    def test_parse_empty_raises(self):
        with self.assertRaises(InvalidLocationError):
            parse_city_state("")

    def test_lookup_known_city(self):
        if not GAZETTEER_PATH.exists():
            self.skipTest("Gazetteer file not present.")
        lat, lon = lookup("Kansas City", "MO")
        self.assertGreater(lat, 38.0)
        self.assertLess(lat, 40.0)
        self.assertGreater(lon, -96.0)
        self.assertLess(lon, -93.0)

    def test_lookup_unknown_city_raises(self):
        if not GAZETTEER_PATH.exists():
            self.skipTest("Gazetteer file not present.")
        with self.assertRaises(UnknownCityError):
            lookup("NotARealCityName", "MO")


def _fake_route_250_mi() -> RouteResult:
    """Deterministic fake route: KC → STL, 250 miles."""
    return RouteResult(
        distance_miles=250.0,
        duration_seconds=4 * 3600,
        geometry=[(-94.55, 39.13), (-92.00, 38.90), (-90.24, 38.64)],
        source="osrm",
    )


class TestRouteAPI(APITestCase):
    """HTTP-level tests. OSRM is mocked to keep tests deterministic."""

    def setUp(self):
        Station.objects.create(
            opis_id="api-test-1",
            name="API Test Station",
            city="Test",
            state="MO",
            lat=38.90,
            lon=-92.00,
            retail_price=Decimal("3.00000000"),
        )

    @patch("api.views.get_route")
    def test_valid_request_returns_200(self, mock_route):
        mock_route.return_value = _fake_route_250_mi()
        response = self.client.post(
            "/api/v1/route/",
            {"origin": "Kansas City, MO", "destination": "St. Louis, MO"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["feasible"])
        self.assertEqual(data["route"]["distance_miles"], 250.0)
        self.assertEqual(data["meta"]["external_api_calls"], 1)

    def test_missing_origin_returns_400(self):
        response = self.client.post(
            "/api/v1/route/",
            {"destination": "St. Louis, MO"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_missing_destination_returns_400(self):
        response = self.client.post(
            "/api/v1/route/",
            {"origin": "Kansas City, MO"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_invalid_origin_format_returns_400(self):
        response = self.client.post(
            "/api/v1/route/",
            {"origin": "not a location", "destination": "St. Louis, MO"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_origin_equals_destination_returns_400(self):
        response = self.client.post(
            "/api/v1/route/",
            {"origin": "Kansas City, MO", "destination": "Kansas City, MO"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    @patch("api.views.get_route")
    def test_routing_failure_returns_502(self, mock_route):
        from api.services.routing import RoutingError
        mock_route.side_effect = RoutingError("Simulated OSRM failure")
        response = self.client.post(
            "/api/v1/route/",
            {"origin": "Kansas City, MO", "destination": "St. Louis, MO"},
            format="json",
        )
        self.assertEqual(response.status_code, 502)
# Create your tests here.
