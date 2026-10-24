# App data build

`scripts/build_app_data.py` writes the five JSON files the journey-planner app loads from
`app/public/data/`: `stops.json`, `legs.json`, `config.json`, `census.json`, `meta.json`, plus two map
layers, `islands.geojson` and `atolls.geojson`.
Types match `app/src/router/model.ts` in the app repo (two additive fields: `config.buildVersion`,
`census[*].disabilityEligible`).

## Command

```bash
python3 scripts/build_app_data.py --out <app_repo>/app/public/data --build-version 2026-10-24T17:00+05:00 \
  [--atolls AA,ADH,K,V,MLE]
```

Standard library only. `--build-version` (or env `APP_DATA_BUILD_VERSION`) is written identically into
`config.json` and `meta.json`; the default is a fixed string, never the wall clock, so the same inputs give
byte-identical output. Files are written to a temp dir and then moved into place. The script prints counts
and every dropped stop, leg and service with its reason.

Tests: `python3 -m pytest tests/test_app_data.py` (needs pytest only).

## Inputs

| Input | Used for |
|---|---|
| `data/processed/ferry/{routing_eligibility,trips,legs,services,routes,stops}.csv` | Official legs. Only `routing_eligible = 1` trips whose `source_id` is a 2026 MTCC table (`cfn_2026_all`, `cfn_2026_zone6`, `speed_2026_thaa_laamu`). Historical timetables are never used. |
| `data/derived/vessels/corridor_services.json` | Inferred legs (Aug 2025 aggregates), `timezone_confirmed`, `speed_unit_confirmed`, `clock_correction.observations_corrected_in_scope`. `dates_seen` is never copied. |
| `per_island/*.csv` | Census 2022 values, island coordinates and `LD####` keys. |
| `data/reference/followme/onemap_study_islands.geojson` | `islands.geojson` (379 OneMap footprints, FCODE = Census `LD####`). |
| `data/reference/followme/study_area.geojson` | `atolls.geojson` (convex hull per study atoll). |

Nothing under `data/private/` is read.

## Rules

- **Region** (`--atolls`, default `AA,ADH,K,V,MLE`, the five FollowMe study atolls): only stops whose atoll
  is in the set are kept, and only legs whose both ends are kept stops. Census entries follow the stops.
  `meta.region = {atolls, name: "Central Maldives study region (AA, ADh, K, V, Greater Malé)"}` (additive).
  Stop ids are assigned before the cut, so an id keeps its atoll suffix when the bare name occurs in
  another atoll nationally (`GURAIDHOO-K`, because TH Guraidhoo exists). The report lists MTCC legs
  dropped by the cut per route, and any trip cut mid-trip.
- **islands.geojson**: OneMap polygons, coordinates rounded to 5 decimals, consecutive duplicate points
  removed, rings closed, rings with fewer than 4 points dropped. Properties `{code (FCODE), name, atoll
  (AA/ADH/K/V/MLE), category, isStop}`; `isStop` is true when `code` equals a kept stop's `censusKey`.
  Compact JSON; top-level `attribution` (OneMap, layer island_20240509, OneMap Data Usage License).
- **atolls.geojson**: one Polygon per `study_area.geojson` feature, the convex hull (monotone chain, pure
  Python) of all its vertices rounded to 5 decimals. The source geometry is island land, not a lagoon rim,
  so the hull is a display outline only, not a legal or reef boundary. Properties `{code (AA, ADh, K, V,
  Greater_Male), name, pcode, method}`; `attribution` credits COD-AB v02 (CC BY-IGO) and OneMap.
- **Times**: integer minutes from 00:00 of the service day, Maldives time, no clock shifting. MTCC
  `departure_seconds`/`arrival_seconds` already include the day offset, so values of 1440+ pass through.
  Inferred times are kept monotonic from the service's first departure: a time more than 60 min before it is
  the next day (+1440), and a range whose max is before its min wraps midnight.
- **tripId**: MTCC `trip_id` (without `trp_`); inferred `service_id`. Legs with one tripId are one boat.
- **Stops**: ferry stop → Census by exact (atoll, ASCII-folded name) match plus a hand-checked alias table
  (Dhangethi→Dhagethi, Kunburudhoo→Kuburudhoo, Hangnaameedhoo→Hangnameedhoo, Malé→Maale,
  Villimalé→Vilingili, Hulhumalé→Hulhumaale). Never substring matching (Kolamaafushi is not Maafushi;
  GA Villingili is a different island and is not aliased). Stops without a Census match are dropped with
  their legs and listed in the report.
- **Inferred legs** (disclosure minimum, from config): a service must be `regular`, with
  `distinct_devices >= 3` and `days_seen >= 8`. A stop with `n_observed < 8` is treated as passed
  (the boat rides straight on). Consecutive kept stops form a leg: dep = [p10, p90] at stop i,
  arr = [p10, p90] at stop i+1 (the next stop's departure range when no arrival was observed).
  `evidence.devices` = service-level `distinct_devices` (an upper bound for a segment);
  `evidence.days` = min(days_seen, n_observed at both ends). `days` = weekdays with at least one observation.
- **Validity**: MTCC validFrom is the date in the source file name (no printed effective dates), validTo
  2026-12-31 (assumption). Inferred legs: 2026-01-01..2026-12-31, projecting the August 2025 pattern.
- **meta**: `fixture false`, `evidencePeriod "2025-08"`, `timezoneStatus`/`speedUnitStatus` from the
  source flags (both `unconfirmed`), `clockCorrections` from the source (0), `timetableYear 2026`.

## Output (build of 2026-10-24, default region)

34 stops (AA 8, ADH 10, K 9, MLE 2, V 5), 120 legs (89 MTCC, 31 inferred), 34 Census entries.
islands.geojson: 379 features, 34 with `isStop`, 1,368,888 bytes. atolls.geojson: 5 features, 6,162 bytes.

Without the region cut the bundle had 143 stops from 20 atolls and 342 legs (311 MTCC, 31 inferred).
The cut removes 109 stops and 222 MTCC legs (routes 101-118, 201-207, 402-405, 504, S506, S507,
601-608, C609: all outside the region). Kept MTCC routes are 301-310, whole; no trip is cut mid-trip.
All 31 inferred legs are kept. 26 used ferry stops have no Census match (all outside the region).
12 inferred services are not regular and are dropped; 8 low-support stops are skipped; 5 legs use the
next stop's departure as arrival.

## Limits

- Timezone and speed unit of the vessel data are unconfirmed; many corridor departures cluster at
  01:00-02:40 local. Times are not shifted.
- Inferred legs describe August 2025 behaviour, not a timetable.
- Per-segment device counts are not available from aggregate outputs; `devices` is the service total.
- MTCC validity dates are assumptions.
- Unmatched stops (for example GA Villingili, L Gan, R Ungoofaaru) need a human-checked alias before
  their legs can be included.
