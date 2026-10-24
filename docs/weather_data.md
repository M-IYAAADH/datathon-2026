# August 2025 hourly wind and wave reference

Config version: `1.0.0`. 45 points; 33,480 point-hours; 744 hours per point. 0 failed point/source pairs. Duplicate point-hours: 0. Diagnostic outliers: 0.

## Sources, models and attribution

[Weather data by Open-Meteo.com](https://open-meteo.com/), using ECMWF IFS historical analysis for atmospheric variables and NOAA NCEP GFS Wave archived forecasts for marine variables. Modelled history is not an observed sea state. The requested models are explicit; model/dataset names actually reported in each response (or their absence) and all returned metadata are in `provenance.json`. Do not interpret a requested model identifier as independent API confirmation.

- [Historical Weather API](https://open-meteo.com/en/docs/historical-weather-api): `https://archive-api.open-meteo.com/v1/archive`, `models=ecmwf_ifs`, about 9 km. IFS was chosen because Open-Meteo's documented ERA5 variable matrix does not provide gusts.
- [Marine Weather API](https://open-meteo.com/en/docs/marine-weather-api): `https://marine-api.open-meteo.com/v1/marine`, `models=ncep_gfswave025`, documented 0.25° (about 25 km), hourly, since June 2024. The identifier is listed in [Open-Meteo's official marine model inventory](https://github.com/open-meteo/open-data#marine-wave-models). These are archived model forecasts, not marine reanalysis; the response does not identify the contributing run/lead time.
- [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) and [Open-Meteo licence](https://open-meteo.com/en/licence). The [free API terms](https://open-meteo.com/en/terms) restrict service use to non-commercial activity (600 calls/minute, 5,000/hour, 10,000/day; multi-location/long requests can count as multiple calls). Credit Open-Meteo, ECMWF and NOAA NCEP. Changes here: point selection, joining, local-time derivation and daily aggregation; source values are not adjusted.
- Island coordinates inherit [OneMap's data usage licence](https://readme.onemap.mv/pdf/Data%20Usage%20License.pdf); study-area geometry inherits Government of Maldives/OCHA COD-AB CC BY-IGO attribution. See [geography review](followme_geography.md).

## Query points and returned cells

Inside the study union bounding box, retain points outside all 379 OneMap island footprints (sea proxy), or within 5000 m of a study island footprint. This is a footprint-based sea proxy, not a surveyed coastline or reef mask. Study-area polygon membership is recorded separately; sea in the bounding box outside the administrative union is retained for exposure context.

35 grid points, 7 island-area endpoints/additional islands, and 3 corridor midpoints. Grid origin is 0°/0°, spacing 0.25°, clipped to the unexpanded study union bounds. Sea means outside the supplied OneMap footprints, including possible lagoon/reef water; no bathymetry or global land mask was fetched. All points lying on a study-island footprint are at distance zero and therefore satisfy the configured 5 km rule. `study_area_codes` is empty for bounding-box context points outside the administrative union; no legal marine boundary is inferred.

Centroid of each OneMap polygon in EPSG:32643, transformed to EPSG:4326; use representative_point in the same projection if centroid lies outside polygon. Endpoints are island-area proxies, not surveyed harbours. Midpoints use the WGS84 geodesic between consecutive endpoints, not claimed sailed paths.

The corridor is Malé–Rasdhoo–Ukulhas–Mathiveri, with all four endpoints and three consecutive-leg midpoints, plus Thoddoo, Maafushi and Hulhumalé. Exact names and atolls must match one OneMap polygon. `weather_points.csv` records FCODEs, coordinates, inclusion reason, footprint land status, distance in UTM 43N metres, administrative memberships, returned cells and per-source status. Input geometry SHA-256s are pinned in the config and provenance.

`cell_selection=sea` is explicit for both sources. Returned coordinates are retained separately for weather and marine, in both the points table and every hourly row. Points may share a returned grid cell, so they are not independent samples. Land endpoints remain island-area proxies even when snapped offshore. An all-null marine response at a land endpoint is labelled `all_null_land_point` in the points table; it is not replaced by a guessed offshore observation.

## Time and units

Request: `start_date=2025-08-01`, `end_date=2025-08-31`, `timezone=GMT`, `timeformat=iso8601`; response UTC offset must be zero. The 744-hour window is 2025-08-01 00:00 UTC through 2025-08-31 23:00 UTC inclusive. `time_utc` is Parquet TIMESTAMPTZ. `time_local` is a local wall-clock TIMESTAMP derived in Python with `ZoneInfo('Indian/Maldives')`, with a separate `local_timezone` column; UTC+5, no DST. It runs from 1 August 05:00 through 1 September 04:00 local. Thus it does not contain the first five local hours of 1 August. No extra dates were collected; confirm GPS timezone before joining.

DuckDB may display TIMESTAMPTZ in the reader's session timezone. Use `SET TimeZone='UTC'` to display `time_utc` in UTC, or `time_utc AT TIME ZONE 'UTC'` for a UTC wall-clock value. The stored timestamp represents a UTC instant regardless of display settings.

`wind_speed_unit=kmh` is explicitly sent to both endpoints; `precipitation_unit=mm`, `length_unit=metric`, and atmospheric `elevation=nan` (disable elevation downscaling) are also explicit where applicable. Parser rejects incompatible units, except that `undefined` is retained for explicitly all-null unsupported series. All `hourly_units` returned by the API are recorded per location in provenance, including time. Wind and wave directions indicate where they come from (degrees clockwise from north). Precipitation is the preceding-hour sum; wave height is significant wave height, not an individual maximum wave.

## Coverage and nulls

| Variable | Unit | Non-null hours | Null hours | Minimum | Maximum |
| --- | --- | ---: | ---: | ---: | ---: |
| wind_speed_10m | km/h | 33480 | 0 | 0.5 | 47.5 |
| wind_gusts_10m | km/h | 33480 | 0 | 4.0 | 62.3 |
| wind_direction_10m | ° | 33480 | 0 | 1.0 | 360.0 |
| precipitation | mm | 33480 | 0 | 0.0 | 8.6 |
| surface_pressure | hPa | 33480 | 0 | 1006.0 | 1014.5 |
| wave_height | m | 33480 | 0 | 0.52 | 2.02 |
| wave_direction | ° | 33480 | 0 | 100.0 | 300.0 |
| wave_period | s | 33480 | 0 | 3.5 | 19.0 |
| wind_wave_height | m | 33480 | 0 | 0.0 | 1.82 |
| swell_wave_height | m | 33480 | 0 | 0.2 | 1.6 |
| swell_wave_period | s | 33480 | 0 | 6.25 | 17.25 |

Expected rows and actual source coverage are different checks. Per-point source time-array and non-null counts are in `validation.json` under `source_point_coverage`. The complete point-hour scaffold preserves failed/missing hours as NULL. No gaps are interpolated, forward-filled or replaced with zero. Source flags distinguish `ok`, `partial_nulls`, `all_null`, `hour_missing` and `request_failed`. A source-level `partial_nulls` flag describes that location's entire response; individual variable nulls remain directly visible. `weather_raw_file`/`marine_raw_file` trace each row to its original response; batch `point_ids` and `location_id` map the response to its query point.

- weather: 41 unique returned cells; statuses: {"ok": 45}.
- marine: 35 unique returned cells; statuses: {"ok": 45}.

Failed point/source requests:

None.

## Daily summaries and highest days

`daily_summary.csv` groups by **Maldives local day**, with counts of observed wind/gust/wave hours. The first day has 19 hours and the 1 September spillover has 5; all intervening days have 24. `hours_in_window` and per-variable observed-hour columns expose incomplete days. Threshold counts use strict `>` and count only observed hours; an entirely missing variable produces NULL, not zero. Thresholds in `config/weather.json` are provisional review thresholds, **not safety limits**: wind 30 km/h, gust 45 km/h, significant wave height 2 m.

Rankings below use the maximum over the 17 query points covered by the reviewed study-area polygons per local August day (not an area-weighted mean). The other 28 bounding-box context points remain in the dataset and daily summaries; their all-point rankings are retained separately as `top_days_bbox_context` in validation. Repeated returned cells cannot alter the maximum. Ties sort by date. Partial boundary days use available hours only; 1 September is excluded.

### Five highest wind days

| Local date | Maximum (km/h) |
| --- | ---: |
| 2025-08-28 | 47.5 |
| 2025-08-05 | 39.8 |
| 2025-08-27 | 39.7 |
| 2025-08-06 | 38.9 |
| 2025-08-01 | 37.1 |

### Five highest wave days

| Local date | Maximum (m) |
| --- | ---: |
| 2025-08-01 | 1.92 |
| 2025-08-05 | 1.88 |
| 2025-08-02 | 1.86 |
| 2025-08-04 | 1.84 |
| 2025-08-06 | 1.82 |

## Files, cache and validation

All dataset files are under `data/reference/weather/`: `weather_hourly.parquet`, `weather_points.csv`, `daily_summary.csv`, `provenance.json`, `validation.json`, and `raw/`. Raw response bodies are saved byte-for-byte, including error responses, with HTTP headers and request receipts. Receipts include UTC retrieval/completion timestamps, exact URL/parameters, attempts and SHA-256. Provenance records raw and output row counts/hashes, every returned unit, full returned location metadata and model fields if reported. `validation.json` contains point-hour counts, non-null ranges, failures and every outlier. Outliers are retained unchanged.

Collection diagnostics are retained in the same raw cache: initial sandbox DNS failures, multi-coordinate elevation parameter errors, an unsupported `gfs_wave` model alias, and ERA5-Ocean responses whose three component series were all null with unit `undefined`. These are not sources for the final table. `requests` in provenance identifies final-source batches, while `unused_cached_requests` records earlier requests separately, including their original parameters, timestamps and reasons.

Five coordinates per request, serial requests, 3 seconds minimum between calls, 90 second timeout, at most four attempts per request per online run, 5/10/20 second exponential retry backoff and `Retry-After` honoured for rate limiting. Permanent 4xx errors are not retried. Successful cache entries are immutable and reused; failed bodies are retained when an online rerun retries. Offline mode never invokes curl. JSON is treated as data: no evaluation, shell interpolation, response-directed URLs or executable content. Arrays, units, times, finite numeric types, location IDs and coordinates are validated. A malformed batch is reported as failed and remains null in outputs.

```bash
.venv/bin/python scripts/collect_weather.py --config config/weather.json
# Exact offline rebuild from this cache:
.venv/bin/python scripts/collect_weather.py --config config/weather.json --offline
# Offline fixture tests:
.venv/bin/python -m pytest tests/test_weather.py -q
```

Run commands from `datathon-2026/`. This collector never invokes FollowMe or writes its outputs. Existing dependencies suffice (Python + DuckDB + Shapely + pyproj); no new package or API key is required. Identical config/geography/cache inputs produce byte-identical primary outputs and deterministic documentation/validation. Provenance uses stored retrieval timestamps, not the rebuild clock.

## Known limits and team questions

- Model analysis, reanalysis or archived forecasts are not an observed sea state, actual disruption, or proof that a vessel sailed. Wind/wave cells are coarse around atolls, reefs, channels and sheltered lagoons; the 0.25° query grid cannot improve the approximately 25 km marine model resolution. Source API preprocessing may itself resample model data; this collector does no gap filling.
- Polygon snapshot was downloaded in October 2026; historical August 2025 shorelines/reclamation are not independently confirmed. Island-area points are not actual jetties; geodesic midpoints do not define navigation routes. No route/GPS matching or safety classification is performed.
- Confirm whether the GPS window is defined in UTC or Maldives local time. A full local-August join would additionally need 31 July 19:00–23:00 UTC, outside this requested collection.
- Ask organisers/crew which review thresholds and vessel classes are meaningful, and which harbours/sailed routes should replace island proxies for later exposure matching.
- Confirm whether bounding-box context sea points outside the administrative union should enter later aggregate analysis, and whether approximately 25 km archived wave forecasts are sufficient. Organisers should confirm that archived GFS forecasts are appropriate for retrospective exposure; ERA5-Ocean at this endpoint lacked component variables.
- Confirm that datathon use and any subsequent reuse meet Open-Meteo's non-commercial free-tier terms; retain attribution if derived results are shared.
