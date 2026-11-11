markdown
# Spotter AI — Backend Django Engineer Assessment

A REST API that computes a driving route between two US locations and returns an
optimal fuel-stop plan that respects a **500-mile maximum range** and **10 MPG**
fuel efficiency, minimising total fuel expenditure using the employer-supplied
fuel-price CSV.

Built with **Django 6.1**, **Django REST Framework 3.18**, **Python 3.12**,
**OSRM** (free routing), and the **2026 US Census Gazetteer** (city coordinates).

---

## 1. What it does

**Single endpoint:** `POST /api/v1/route/`

**Request:**
```json
{
  "origin": "Kansas City, MO",
  "destination": "Los Angeles, CA"
}
Response (abridged):

json
{
  "feasible": true,
  "origin":      { "input": "Kansas City, MO", "lat": 39.125, "lon": -94.550 },
  "destination": { "input": "Los Angeles, CA", "lat": 34.052, "lon": -118.244 },
  "route": {
    "distance_miles": 1622.2,
    "duration_seconds": 57700,
    "geometry": { "type": "LineString", "coordinates": [[-94.55, 39.13], "..."] },
    "source": "osrm"
  },
  "fuel_plan": {
    "vehicle": { "tank_gallons": 50.0, "max_range_miles": 500.0, "mpg": 10.0 },
    "purchases": [
      {
        "station": { "opis_id": "94", "name": "...", "city": "Bunker Hill", "state": "KS",
                     "lat": 38.87, "lon": -98.70 },
        "route_position_miles": 235.14,
        "price_per_gallon": 2.999,
        "gallons_purchased": 23.51,
        "cost_usd": 70.51
      }
    ],
    "total_gallons_purchased": 112.215,
    "total_cost_usd": 353.92,
    "max_leg_miles": 394.2
  },
  "meta": {
    "processing_seconds": 2.1,
    "external_api_calls": 1,
    "warnings": [],
    "assumptions": ["..."]
  }
}
The route.geometry is a GeoJSON LineString renderable by any map library
(Leaflet, Mapbox, Google Maps, MapLibre, deck.gl).

2. Quick start
Prerequisites
Python 3.12+ (Django 6.1 requires it)

Windows PowerShell (or any POSIX shell — commands below show PowerShell)

Setup
powershell
# 1. Create and activate a virtual environment
python -m venv venv
.\venv\Scripts\Activate.ps1

# 2. Install dependencies
pip install -r requirements.txt

# 3. Place fuel-prices-for-be-assessment.csv at the repo root (employer-supplied)

# 4. Prepare and load data
python manage.py migrate
python scripts/profile_and_geocode.py    # one-time diagnostic (~40s, Census batch)
python scripts/enrich_with_gazetteer.py
python manage.py load_stations

# 5. Run
python manage.py runserver
Then POST:

powershell
curl -X POST http://127.0.0.1:8000/api/v1/route/ `
     -H "Content-Type: application/json" `
     -d '{\"origin\":\"Kansas City, MO\",\"destination\":\"Los Angeles, CA\"}'
Run the tests
powershell
python manage.py test api -v 2
21 tests — algorithm, parser, and API integration — under 1 second.

3. Architecture
text
POST /api/v1/route/
        |
        v
+---------------------------------------+
| api.views.RouteView                   |  <- DRF view, orchestrates
| api.serializers.RouteRequestSerializer|  <- input validation
+------------------+--------------------+
                   |
                   v
+---------------------------------------+
| api.services.city_lookup              |  <- "City, ST" -> (lat, lon)
|                                       |     via 2026 US Census Gazetteer
+------------------+--------------------+
                   |
                   v
+---------------------------------------+
| api.services.routing                  |  <- ONE OSRM call per request
|                                       |     returns distance, duration,
|                                       |     GeoJSON LineString
+------------------+--------------------+
                   |
                   v
+---------------------------------------+
| api.services.matching                 |  <- 6,207 stations x ~3,600 segments
|                                       |     NumPy point-to-segment,
|                                       |     10-mile corridor filter,
|                                       |     route-relative positions
+------------------+--------------------+
                   |
                   v
+---------------------------------------+
| api.services.optimization             |  <- greedy look-ahead,
|                                       |     nearest-cheaper rule,
|                                       |     500-mile reachability,
|                                       |     gallon & cost calculations
+---------------------------------------+
Layering principle: each service is a pure Python module with no Django
dependency — unit-testable without a database, a web server, or a network call.

4. Fuel-stop optimisation — algorithm
Objective
Minimise total fuel expenditure while respecting 500-mile maximum range
(50-gallon tank at 10 MPG), starting with a full tank.

Approach: greedy look-ahead with nearest-cheaper rule
At each decision point (origin, or a station where we stopped):

If destination is reachable with current fuel -> buy nothing, done.

If a cheaper station exists within full-tank range -> buy exactly
enough to reach the nearest cheaper station. Buying more expensive
fuel than necessary is never beneficial.

Otherwise (current station is the cheapest available for the
foreseeable future) -> fill the tank, drive to the cheapest reachable
station.

If no station is reachable and destination is out of range -> return a
structured infeasible response. We never fabricate a route.

Correctness
Under the stated assumptions — continuous fuel purchase, no fixed cost per
stop, no price uncertainty — this greedy rule is known to be optimal for
the continuous-fuel gas-station problem (the "nearest cheaper station" property
means buying more at a more expensive station is never advantageous).

Honest limitation
The algorithm may recommend multiple small purchases in quick succession, e.g.
0.82 gal at mile 243 then 13.1 gal at mile 375. This is mathematically
cost-optimal but not driver-realistic. A driver would merge the small
purchases, paying a few cents more but making fewer stops. The employer's
requirement was cost-minimisation, not stop-count minimisation; we chose
provable optimality over heuristic realism.

Complexity
Candidate stations after corridor filter: typically 30-150 per route
(already sorted by route position).

Each iteration selects from within-range stations: O(n).

Worst-case iterations: O(n). Total: O(n^2) where n ~ 100 -> sub-millisecond.

5. Geographic data pipeline
The dataset problem
The employer-supplied fuel-prices-for-be-assessment.csv has 8,151 rows
and columns:

text
OPIS Truckstop ID, Truckstop Name, Address, City, State, Rack ID, Retail Price
It has no latitude or longitude columns. The Address field contains
highway-exit descriptions ("I-44, EXIT 283 & US-69"), not street addresses.

The solution — Path A
Deduplicate by OPIS Truckstop ID (8151 -> 6,738 unique stations).
Duplicate rows with the same ID and divergent prices are merged: the record
with the most descriptive name is kept.

US Census batch geocoder was tried first. It resolved only 7.82%
(527/6738) of addresses — Census has known limits on commercial addresses
due to Title 13 restrictions, and highway-exit descriptions are outside its
grammar.

2026 US Census Gazetteer Places was used instead. Each station's
City, State is joined against the Gazetteer's 32,134 places to obtain
city-centroid coordinates. Match rate: 92.12% (6,207/6,738).

531 excluded records — unincorporated US communities (not in the
Gazetteer by design) and Canadian records (out of scope). Excluded, not
fabricated.

**Pipeline note.** `scripts/profile_and_geocode.py` is a one-time diagnostic
that documents the Census batch geocoder's limitations (7.82% match rate,
preserved as evidence of why we pivoted). It produces
`data/geocoded_stations.json` — the deduplicated station records.
`scripts/enrich_with_gazetteer.py` then joins those records against the
Gazetteer and writes `data/stations_enriched.csv`. Both scripts run during
initial setup; neither runs per API request.

Honest limitation
Coordinates are city-level approximations, not forecourt locations. A
station on a highway exit 4 miles outside its listed town has up to 4 miles of
positional error. The API labels stations as "route-proximate candidates",
not verified operational locations. The 10-mile corridor matches this
precision.

6. API reference
POST /api/v1/route/
Request body
json
{
  "origin": "Kansas City, MO",
  "destination": "Los Angeles, CA"
}
origin and destination are required strings.

Format: "City, ST" (two-letter USPS) or "City, Full State Name".

Origin and destination must be different.

Response — 200 OK (feasible)
See the example in section 1.

Response — 200 OK (infeasible)
When no station sequence can bridge the 500-mile constraint:

json
{
  "feasible": false,
  "error": "infeasible",
  "reason": "No station reachable from mile 210.0; cannot complete route."
}
Response — 400 Bad Request
Invalid input format or unknown city:

json
{ "error": "invalid_location", "detail": "Location must be in the form 'City, ST': got 'not a location'" }
json
{ "error": "unknown_city", "detail": "City not found in Gazetteer: 'NotARealCityName', 'MO'" }
json
{ "error": "invalid_request", "detail": { "origin": ["This field is required."] } }
Response — 502 Bad Gateway
External routing provider failed:

json
{ "error": "routing_failed", "detail": "OSRM request failed: <reason>" }
7. External API usage
Provider	Calls per request	Purpose
OSRM (router.project-osrm.org)	1 (uncached)	Route + geometry
US Census Gazetteer	0 at request time (file cached on disk)	City coordinates
The employer's target was 1 routing call per request, 2-3 acceptable.
We make exactly 1, and no per-station routing calls.

Per-request timing observed during development (KC -> LA, 1,622 mi):

Phase	Time
OSRM route fetch	~1.5-2.0 s (network)
DB load (6,207 stations)	~0.10-0.26 s
Matcher (6,207 x 3,500 segments, with bbox pre-filter)	~0.20 s
Optimiser	< 0.01 s
Total wall-clock	~2.1 s
8. Assumptions register
#	Assumption	Rationale
A1	Vehicle starts with a full 50-gallon tank	Employer specifies 500-mile range; a full tank is the most defensible baseline.
A2	Tank capacity = 50 gallons (500 mi / 10 MPG)	Derived from A1.
A3	Fuel may be purchased continuously (any quantity, no full-tank requirement)	Standard continuous-fuel formulation; allows true cost minimisation.
A4	Fuel efficiency is a uniform 10 MPG	Employer requirement.
A5	Prices are the static snapshot from the employer CSV — not live	Dataset contains no timestamp column.
A6	Diesel is the assumed fuel type (OPIS truckstop context)	Dataset has no fuel-type column.
A7	Stations within 10 miles of the route polyline are candidates	Corridor width trade-off: wide enough for city-centroid precision, narrow enough to exclude outliers.
A8	No fuel purchase required at the destination; leftover fuel is not refunded	Employer asks for "money spent on fuel"; we track purchases only.
A9	Duplicate OPIS ID rows represent the same station; the most descriptive name is kept	Verified in the CSV — duplicate IDs always share city/state.
9. What's deferred / out of scope
Response caching. Identical requests hit OSRM again. Not required by the
employer; documented as a future improvement.

Real-time price updates. Prices are static; the schema supports an
updated_at field for future ingestion from a live OPIS feed.

Forecourt-accurate station coordinates. Would require a paid geocoder
(TomTom, HERE, Mapbox); free options were evaluated and either failed on
commercial addresses (Census) or exceeded the free-tier limits for bulk
ingestion (OpenCage: 2,500/day vs 6,738 records).

Saint -> St. normalisation for ~50-150 recoverable Gazetteer matches.
Deliberately skipped: marginal improvement, no impact on routing quality
(6,207 stations is dense enough for any intercity route).

10. Repository layout
text
spotter-assessment/
+-- api/
|   +-- management/
|   |   +-- commands/
|   |       +-- load_stations.py       # Ingest stations_enriched.csv -> DB
|   +-- migrations/
|   |   +-- 0001_initial.py
|   +-- services/
|   |   +-- city_lookup.py             # City + state -> (lat, lon)
|   |   +-- matching.py                # Route <-> station corridor filter
|   |   +-- optimization.py            # Greedy fuel-stop algorithm
|   |   +-- routing.py                 # One OSRM call per request
|   +-- models.py                      # Station model
|   +-- serializers.py                 # RouteRequestSerializer
|   +-- tests.py                       # 21 tests
|   +-- urls.py                        # /api/v1/route/
|   +-- views.py                       # RouteView
+-- data/                              # (gitignored) runtime data
|   +-- 2026_Gaz_place_national.txt    # Census Gazetteer
|   +-- geocoded_stations.json         # M2 diagnostic output
|   +-- stations_enriched.csv          # Ingestion input
+-- scripts/
|   +-- enrich_with_gazetteer.py       # Download Gazetteer + join stations
|   +-- profile_and_geocode.py         # Ingestion diagnostic; produces geocoded_stations.json
+-- spotter_project/
|   +-- settings.py
|   +-- urls.py
+-- fuel-prices-for-be-assessment.csv  # (employer-supplied; not redistributed)
+-- manage.py
+-- requirements.txt
+-- README.md
11. Testing
powershell
python manage.py test api -v 2
21 tests across three tiers:

TestOptimization (8 tests): short route -> no purchase; single stop;
multiple stops; infeasible route; cheaper station preference; 500-mile
boundary; zero-distance rejection; cost arithmetic.

TestCityLookup (7 tests): parsing with abbreviation, full name,
invalid state, missing comma, empty string; known-city lookup; unknown-city
rejection.

TestRouteAPI (6 tests): happy path (OSRM mocked), missing field
responses, invalid format, origin=destination, upstream routing failure.

All tests pass in < 1 second — no network, no flakiness.

12. Reproducibility
The full pipeline is reproducible from a clean checkout:

pip install -r requirements.txt

Place fuel-prices-for-be-assessment.csv at the project root.

python manage.py migrate

python scripts/enrich_with_gazetteer.py — downloads the 2026 Census
Gazetteer (~1.2 MB) once and writes data/stations_enriched.csv.

python manage.py load_stations — bulk-loads 6,207 rows.

python manage.py runserver.

The Gazetteer download is the only external network dependency during setup
and it is idempotent (skipped if the file is already present).

text



