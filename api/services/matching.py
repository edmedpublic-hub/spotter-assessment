"""
Route-to-station geographic matching.

Given a route polyline (from OSRM) and candidate stations, return the
subset whose perpendicular distance to the route is within a corridor
threshold. Each returned station has:
  - distance_to_route_miles: perpendicular offset (may be 0-10 miles)
  - distance_along_route_miles: cumulative route position (0 = origin)

Performance: stations outside the route's lat/lon bounding box (padded
by corridor) are excluded before the point-to-segment loop. On typical
intercity routes this reduces work by 10-100x.

Coordinate system: equirectangular projection at the route's mean
latitude. Valid within the Continental US (error < 0.1%); documented
in README.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable

import numpy as np

EARTH_RADIUS_M = 6_371_000.0
METERS_PER_MILE = 1609.344
DEFAULT_CORRIDOR_MILES = 10.0


@dataclass
class MatchedStation:
    opis_id: str
    name: str
    city: str
    state: str
    lat: float
    lon: float
    retail_price: float
    distance_to_route_miles: float
    distance_along_route_miles: float


def match_stations_to_route(
    route_geometry: list[tuple[float, float]],
    stations: Iterable[Any],
    corridor_miles: float = DEFAULT_CORRIDOR_MILES,
) -> tuple[list[MatchedStation], dict[str, Any]]:
    if len(route_geometry) < 2:
        raise ValueError("Route geometry must have at least 2 points.")
    if corridor_miles <= 0:
        raise ValueError("corridor_miles must be positive.")

    lons = np.array([p[0] for p in route_geometry], dtype=np.float64)
    lats = np.array([p[1] for p in route_geometry], dtype=np.float64)
    lat_mean = float(np.mean(lats))
    cos_lat = math.cos(math.radians(lat_mean))

    route_x = np.radians(lons) * EARTH_RADIUS_M * cos_lat
    route_y = np.radians(lats) * EARTH_RADIUS_M
    route_xy = np.column_stack([route_x, route_y])

    seg_deltas = np.diff(route_xy, axis=0)
    seg_lens = np.linalg.norm(seg_deltas, axis=1)
    cum_dist = np.concatenate([[0.0], np.cumsum(seg_lens)])
    total_route_m = float(cum_dist[-1])

    station_list = list(stations)
    if not station_list:
        return [], {
            "corridor_miles": corridor_miles,
            "total_route_miles": round(total_route_m / METERS_PER_MILE, 2),
            "stations_considered": 0,
            "stations_within_corridor": 0,
        }

    all_lats = np.array([s.lat for s in station_list], dtype=np.float64)
    all_lons = np.array([s.lon for s in station_list], dtype=np.float64)

    # Bounding-box pre-filter. Padding accounts for the corridor width.
    lat_pad = corridor_miles / 69.0
    lon_pad = corridor_miles / (69.0 * max(cos_lat, 0.1))
    bbox_mask = (
        (all_lats >= lats.min() - lat_pad)
        & (all_lats <= lats.max() + lat_pad)
        & (all_lons >= lons.min() - lon_pad)
        & (all_lons <= lons.max() + lon_pad)
    )
    keep_idx = np.where(bbox_mask)[0]
    filtered_stations = [station_list[int(i)] for i in keep_idx]
    n_st = len(filtered_stations)

    if n_st == 0:
        return [], {
            "corridor_miles": corridor_miles,
            "total_route_miles": round(total_route_m / METERS_PER_MILE, 2),
            "stations_considered": len(station_list),
            "stations_within_corridor": 0,
        }

    st_lats = all_lats[keep_idx]
    st_lons = all_lons[keep_idx]
    stations_xy = np.column_stack(
        [
            np.radians(st_lons) * EARTH_RADIUS_M * cos_lat,
            np.radians(st_lats) * EARTH_RADIUS_M,
        ]
    )

    min_dist = np.full(n_st, np.inf, dtype=np.float64)
    min_idx = np.full(n_st, -1, dtype=np.int64)
    min_t = np.zeros(n_st, dtype=np.float64)

    eps = 1e-9
    for i in range(len(route_xy) - 1):
        a = route_xy[i]
        ab = route_xy[i + 1] - a
        ab_len_sq = float(ab[0] * ab[0] + ab[1] * ab[1])
        if ab_len_sq < eps:
            continue
        ap_x = stations_xy[:, 0] - a[0]
        ap_y = stations_xy[:, 1] - a[1]
        t = (ap_x * ab[0] + ap_y * ab[1]) / ab_len_sq
        np.clip(t, 0.0, 1.0, out=t)
        dx = ap_x - t * ab[0]
        dy = ap_y - t * ab[1]
        d = np.sqrt(dx * dx + dy * dy)
        better = d < min_dist
        if np.any(better):
            min_dist[better] = d[better]
            min_idx[better] = i
            min_t[better] = t[better]

    min_dist_miles = min_dist / METERS_PER_MILE
    within = min_dist_miles <= corridor_miles

    candidates: list[MatchedStation] = []
    for j in np.where(within)[0]:
        st = filtered_stations[j]
        i = int(min_idx[j])
        along_m = float(cum_dist[i] + min_t[j] * seg_lens[i])
        candidates.append(
            MatchedStation(
                opis_id=st.opis_id,
                name=st.name,
                city=st.city,
                state=st.state,
                lat=st.lat,
                lon=st.lon,
                retail_price=float(st.retail_price),
                distance_to_route_miles=float(min_dist_miles[j]),
                distance_along_route_miles=along_m / METERS_PER_MILE,
            )
        )

    candidates.sort(key=lambda c: c.distance_along_route_miles)

    metadata = {
        "corridor_miles": corridor_miles,
        "total_route_miles": round(total_route_m / METERS_PER_MILE, 2),
        "stations_considered": len(station_list),
        "stations_after_bbox": n_st,
        "stations_within_corridor": len(candidates),
    }
    return candidates, metadata