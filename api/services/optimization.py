"""
Fuel-stop optimization (greedy look-ahead with nearest-cheaper rule).

Problem: given candidate stations ordered by route position, choose
the subset to buy fuel at and the quantity at each, respecting a
500-mile maximum range (50 gal tank at 10 MPG) and starting full.

Public API:
    optimize_fuel_stops(stations, total_distance_miles) -> OptimizationResult

Assumptions (documented in README):
    A1: Vehicle starts with a full tank (50 gal).
    A2: Tank capacity = 50 gal (500-mile range at 10 MPG).
    A3: Fuel can be purchased in any quantity.
    A4: Uniform 10 MPG efficiency.
    A8: No purchase required at destination; leftover fuel is not refunded.

Algorithm:
    At each decision point:
      1. If dest reachable with current fuel -> buy nothing, stop.
      2. If a cheaper station is within full-tank range -> buy just enough
         to reach the NEAREST such cheaper station.
      3. Otherwise (current is cheapest in range) -> fill tank, drive to
         the CHEAPEST reachable station.
    If no station is reachable and destination is out of range -> INFEASIBLE.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

DEFAULT_TANK_GALLONS = 50.0
DEFAULT_MPG = 10.0
EPS = 1e-6


@dataclass
class FuelPurchase:
    station_opis_id: str
    station_name: str
    station_city: str
    station_state: str
    station_lat: float
    station_lon: float
    route_position_miles: float
    price_per_gallon: float
    gallons_purchased: float
    cost_usd: float
    fuel_before_gallons: float
    fuel_after_gallons: float
    distance_since_prev_purchase_miles: float


@dataclass
class OptimizationResult:
    feasible: bool
    purchases: list[FuelPurchase]
    total_cost_usd: float
    total_gallons_purchased: float
    ending_fuel_gallons: float
    max_leg_miles: float
    reason: str | None = None
    warnings: list[str] = field(default_factory=list)


def _infeasible(reason: str) -> OptimizationResult:
    return OptimizationResult(
        feasible=False,
        purchases=[],
        total_cost_usd=0.0,
        total_gallons_purchased=0.0,
        ending_fuel_gallons=0.0,
        max_leg_miles=0.0,
        reason=reason,
    )


def _make_purchase(
    station: Any,
    pos: float,
    gallons: float,
    price: float,
    fuel_before: float,
    last_purchase_pos: float,
) -> FuelPurchase:
    return FuelPurchase(
        station_opis_id=station.opis_id,
        station_name=station.name,
        station_city=station.city,
        station_state=station.state,
        station_lat=station.lat,
        station_lon=station.lon,
        route_position_miles=round(pos, 2),
        price_per_gallon=round(price, 4),
        gallons_purchased=round(gallons, 4),
        cost_usd=round(gallons * price, 2),
        fuel_before_gallons=round(fuel_before, 4),
        fuel_after_gallons=round(fuel_before + gallons, 4),
        distance_since_prev_purchase_miles=round(pos - last_purchase_pos, 2),
    )


def optimize_fuel_stops(
    stations: Iterable[Any],
    total_distance_miles: float,
    tank_gallons: float = DEFAULT_TANK_GALLONS,
    mpg: float = DEFAULT_MPG,
) -> OptimizationResult:
    """
    Returns a feasible OptimizationResult with purchases, or an infeasible
    result with a clear reason. Never raises on infeasibility.
    """
    if total_distance_miles <= 0:
        return _infeasible("Route distance must be positive.")
    if tank_gallons <= 0 or mpg <= 0:
        return _infeasible("Tank capacity and MPG must be positive.")

    max_range = tank_gallons * mpg
    stations = sorted(
        [s for s in stations if 0 < s.distance_along_route_miles < total_distance_miles],
        key=lambda s: s.distance_along_route_miles,
    )

    fuel = tank_gallons  # A1: start full
    pos = 0.0
    current_price = float("inf")  # at origin we cannot buy
    current_station: Any = None
    purchases: list[FuelPurchase] = []
    last_purchase_pos = 0.0

    safety_limit = len(stations) + 5
    for _ in range(safety_limit):
        dist_to_dest = total_distance_miles - pos

        # Case 1: destination reachable with current fuel
        if dist_to_dest <= fuel * mpg + EPS:
            break

        # Stations reachable if we fill the tank here
        reachable = [
            s
            for s in stations
            if s.distance_along_route_miles > pos + EPS
            and s.distance_along_route_miles - pos <= max_range + EPS
        ]

        # No station reachable; check if dest itself is reachable
        if not reachable:
            if dist_to_dest <= max_range + EPS:
                needed = dist_to_dest / mpg
                buy = max(0.0, needed - fuel)
                if buy > EPS:
                    if current_station is None:
                        return _infeasible(
                            "Cannot purchase fuel before reaching the first station."
                        )
                    purchases.append(
                        _make_purchase(
                            current_station, pos, buy, current_price,
                            fuel, last_purchase_pos,
                        )
                    )
                    last_purchase_pos = pos
                fuel = 0.0
                pos = total_distance_miles
                break
            return _infeasible(
                f"No station reachable from mile {pos:.1f}; cannot complete route."
            )

        # Case 2: nearest cheaper station within reach
        nearest_cheaper = next(
            (s for s in reachable if s.retail_price < current_price), None
        )
        if nearest_cheaper is not None:
            needed = (nearest_cheaper.distance_along_route_miles - pos) / mpg
            buy = max(0.0, needed - fuel)
            if buy > EPS:
                if current_station is None:
                    return _infeasible(
                        "Cannot purchase fuel before reaching the first station."
                    )
                purchases.append(
                    _make_purchase(
                        current_station, pos, buy, current_price,
                        fuel, last_purchase_pos,
                    )
                )
                last_purchase_pos = pos
                fuel += buy
            fuel -= needed
            pos = nearest_cheaper.distance_along_route_miles
            current_price = nearest_cheaper.retail_price
            current_station = nearest_cheaper
            continue

        # Case 3: no cheaper station. Either reach dest with one tank,
        # or fill up and drive to the cheapest reachable station.
        if dist_to_dest <= max_range + EPS:
            needed = dist_to_dest / mpg
            buy = max(0.0, needed - fuel)
            if buy > EPS:
                if current_station is None:
                    return _infeasible(
                        "Cannot purchase fuel before reaching the first station."
                    )
                purchases.append(
                    _make_purchase(
                        current_station, pos, buy, current_price,
                        fuel, last_purchase_pos,
                    )
                )
                last_purchase_pos = pos
            fuel = 0.0
            pos = total_distance_miles
            break

        if fuel < tank_gallons - EPS:
            buy = tank_gallons - fuel
            if current_station is None:
                return _infeasible(
                    "Cannot purchase fuel before reaching the first station."
                )
            purchases.append(
                _make_purchase(
                    current_station, pos, buy, current_price,
                    fuel, last_purchase_pos,
                )
            )
            last_purchase_pos = pos
            fuel = tank_gallons

        cheapest = min(reachable, key=lambda s: s.retail_price)
        dist_to_cheapest = cheapest.distance_along_route_miles - pos
        fuel -= dist_to_cheapest / mpg
        pos = cheapest.distance_along_route_miles
        current_price = cheapest.retail_price
        current_station = cheapest
    else:
        return _infeasible("Iteration limit exceeded; algorithm failed to converge.")

    # Post-hoc validation
    total_cost = sum(p.cost_usd for p in purchases)
    total_gallons = sum(p.gallons_purchased for p in purchases)

    positions = [0.0] + [p.route_position_miles for p in purchases] + [total_distance_miles]
    legs = [positions[i + 1] - positions[i] for i in range(len(positions) - 1)]
    max_leg = max(legs) if legs else 0.0

    warnings: list[str] = []
    if max_leg > max_range + EPS:
        warnings.append(
            f"Leg of {max_leg:.1f} miles exceeds max range of {max_range:.0f}."
        )
    if fuel < -EPS:
        warnings.append(f"Ending fuel is negative ({fuel:.4f}); result unreliable.")

    return OptimizationResult(
        feasible=True,
        purchases=purchases,
        total_cost_usd=round(total_cost, 2),
        total_gallons_purchased=round(total_gallons, 4),
        ending_fuel_gallons=round(max(0.0, fuel), 4),
        max_leg_miles=round(max_leg, 1),
        warnings=warnings,
    )