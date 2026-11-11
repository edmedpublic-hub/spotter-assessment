"""
Route API view.

POST /api/v1/route/
    Body: {"origin": "Kansas City, MO", "destination": "St. Louis, MO"}
    Response (200): full route + fuel plan (see README).

Makes exactly one external API call per request (OSRM).
"""

from __future__ import annotations

import time

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from api.models import Station
from api.serializers import RouteRequestSerializer
from api.services.city_lookup import (
    InvalidLocationError,
    UnknownCityError,
    lookup,
    parse_city_state,
)
from api.services.matching import match_stations_to_route
from api.services.optimization import optimize_fuel_stops
from api.services.routing import RoutingError, get_route


class RouteView(APIView):
    """Compute a driving route and optimal fuel-stop plan."""

    def post(self, request):
        t_start = time.perf_counter()

        serializer = RouteRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {"error": "invalid_request", "detail": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )

        origin_raw = serializer.validated_data["origin"]
        destination_raw = serializer.validated_data["destination"]

        try:
            origin_city, origin_state = parse_city_state(origin_raw)
            destination_city, destination_state = parse_city_state(destination_raw)
        except InvalidLocationError as exc:
            return Response(
                {"error": "invalid_location", "detail": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            origin_lat, origin_lon = lookup(origin_city, origin_state)
            dest_lat, dest_lon = lookup(destination_city, destination_state)
        except (UnknownCityError, InvalidLocationError) as exc:
            return Response(
                {"error": "unknown_city", "detail": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if (origin_lat, origin_lon) == (dest_lat, dest_lon):
            return Response(
                {
                    "error": "invalid_request",
                    "detail": "Origin and destination resolve to the same location.",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            route = get_route(origin_lat, origin_lon, dest_lat, dest_lon)
        except RoutingError as exc:
            return Response(
                {"error": "routing_failed", "detail": str(exc)},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        stations = list(Station.objects.all())
        candidates, match_meta = match_stations_to_route(route.geometry, stations)

        optimization = optimize_fuel_stops(candidates, route.distance_miles)

        elapsed = time.perf_counter() - t_start

        purchases = [
            {
                "station": {
                    "opis_id": p.station_opis_id,
                    "name": p.station_name,
                    "city": p.station_city,
                    "state": p.station_state,
                    "lat": p.station_lat,
                    "lon": p.station_lon,
                },
                "route_position_miles": p.route_position_miles,
                "distance_since_prev_purchase_miles": (
                    p.distance_since_prev_purchase_miles
                ),
                "price_per_gallon": p.price_per_gallon,
                "gallons_purchased": p.gallons_purchased,
                "cost_usd": p.cost_usd,
                "fuel_before_gallons": p.fuel_before_gallons,
                "fuel_after_gallons": p.fuel_after_gallons,
            }
            for p in optimization.purchases
        ]

        response_body = {
            "feasible": optimization.feasible,
            "origin": {
                "input": origin_raw,
                "city": origin_city,
                "state": origin_state,
                "lat": origin_lat,
                "lon": origin_lon,
            },
            "destination": {
                "input": destination_raw,
                "city": destination_city,
                "state": destination_state,
                "lat": dest_lat,
                "lon": dest_lon,
            },
            "route": {
                "distance_miles": round(route.distance_miles, 1),
                "duration_seconds": round(route.duration_seconds, 0),
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[lon, lat] for lon, lat in route.geometry],
                },
                "source": route.source,
            },
            "fuel_plan": {
                "vehicle": {
                    "tank_gallons": 50.0,
                    "max_range_miles": 500.0,
                    "mpg": 10.0,
                    "starting_fuel_gallons": 50.0,
                },
                "purchases": purchases,
                "total_gallons_purchased": optimization.total_gallons_purchased,
                "total_cost_usd": optimization.total_cost_usd,
                "ending_fuel_gallons": optimization.ending_fuel_gallons,
                "max_leg_miles": optimization.max_leg_miles,
            },
            "meta": {
                "stations_in_corridor": match_meta["stations_within_corridor"],
                "corridor_miles": match_meta["corridor_miles"],
                "warnings": optimization.warnings,
                "processing_seconds": round(elapsed, 3),
                "external_api_calls": 1,
                "assumptions": [
                    "Vehicle starts with a full 50-gallon tank.",
                    "Fuel efficiency is 10 MPG.",
                    "Station coordinates are city-level approximations "
                    "from the 2026 US Census Gazetteer, not forecourt locations.",
                    "Stations are route-proximate candidates, not verified "
                    "operational stations.",
                    "Prices are a static snapshot from the employer CSV; "
                    "not live data.",
                ],
            },
        }

        if not optimization.feasible:
            response_body["error"] = "infeasible"
            response_body["reason"] = optimization.reason

        return Response(response_body, status=status.HTTP_200_OK)