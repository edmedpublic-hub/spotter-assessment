"""
Routing service: single OSRM call per request.

Public API:
    get_route(origin_lat, origin_lon, dest_lat, dest_lon) -> RouteResult

    RouteResult has:
        distance_miles: float
        duration_seconds: float
        geometry: list[tuple[float, float]]  # (lon, lat) as OSRM returns
        source: "osrm"
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import requests

OSRM_BASE_URL = "https://router.project-osrm.org"
OSRM_ROUTE_PATH = "/route/v1/driving"
REQUEST_TIMEOUT_SECONDS = 30
METERS_PER_MILE = 1609.344


class RoutingError(Exception):
    """Raised when OSRM cannot produce a route."""


@dataclass
class RouteResult:
    distance_miles: float
    duration_seconds: float
    geometry: list[tuple[float, float]] = field(default_factory=list)
    source: str = "osrm"


def get_route(
    origin_lat: float, origin_lon: float, dest_lat: float, dest_lon: float
) -> RouteResult:
    """
    Fetch a driving route between two coordinate pairs via OSRM.

    Makes exactly one HTTP call. Raises RoutingError on any failure
    (network, HTTP error, OSRM code != "Ok", missing routes/geometry).
    """
    coords = f"{origin_lon},{origin_lat};{dest_lon},{dest_lat}"
    url = f"{OSRM_BASE_URL}{OSRM_ROUTE_PATH}/{coords}"
    params = {
        "overview": "full",
        "geometries": "geojson",
        "alternatives": "false",
        "steps": "false",
    }

    try:
        response = requests.get(url, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise RoutingError(f"OSRM request failed: {exc}") from exc

    try:
        data: dict[str, Any] = response.json()
    except ValueError as exc:
        raise RoutingError(f"OSRM returned non-JSON response: {exc}") from exc

    code = data.get("code")
    if code != "Ok":
        raise RoutingError(
            f"OSRM did not return a route (code={code!r}, "
            f"message={data.get('message')!r})."
        )

    routes = data.get("routes") or []
    if not routes:
        raise RoutingError("OSRM returned no routes.")

    route = routes[0]
    distance_m = route.get("distance")
    duration_s = route.get("duration")
    geometry = route.get("geometry") or {}
    coordinates = geometry.get("coordinates") or []

    if distance_m is None or duration_s is None:
        raise RoutingError("OSRM response missing distance or duration.")
    if not coordinates:
        raise RoutingError("OSRM response missing route geometry.")
    if not math.isfinite(float(distance_m)) or float(distance_m) <= 0:
        raise RoutingError(f"OSRM returned invalid distance: {distance_m!r}.")

    return RouteResult(
        distance_miles=float(distance_m) / METERS_PER_MILE,
        duration_seconds=float(duration_s),
        geometry=[(float(c[0]), float(c[1])) for c in coordinates],
        source="osrm",
    )