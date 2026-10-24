# Harbour and jetty reference

Reference points for harbours, jetties, piers and ferry terminals in AA, ADh, K, V and Greater Malé,
and the (private) method for comparing them with stop locations from the cleaned FollowMe GPS data.

**Status of the GPS part.** Part A (the public reference) is built. Parts B and C (stay points, clusters,
matching) are implemented and tested on synthetic data but have **not** been run on real data: when this was
written the FollowMe national run was still in progress (`failed_run.json` present, no `run_manifest.json`
or `regional_run_manifest.json`). `scripts/build_stop_candidates.py` refuses to run until both manifests are
`"status": "complete"` with every validation value true and no `failed_run.json`,
`regional_failed_run.json` or `.run.lock` exists. Partial outputs are never read.

## Sources, licences and what was rejected

| Source | Use | Licence / terms | Retrieved | Features |
| --- | --- | --- | --- | --- |
| OpenStreetMap via the **Geofabrik Maldives extract** `maldives-latest.osm.pbf` (3,655,188 bytes, SHA-256 `90d0bce2…e95276`, Last-Modified 2026-10-23) | Used | ODbL 1.0. Contains data © OpenStreetMap contributors. | 2026-10-24 | 2,874 OSM objects in the study bbox + margin, parsed to 2,933 rows (route ways/relations also yield start/end points) |
| OneMap ArcGIS service directory and per-service metadata (`services7.arcgis.com/yvCbn3q8PPtPLZIM`) | Probed only | OneMap [Data Usage License](https://readme.onemap.mv/pdf/Data%20Usage%20License.pdf) | 2026-10-24 | 0 harbour features: the public layers are bathymetry, lagoon, reef, ProtectedArea, ControlPoints (survey marks), PlotLocator (Gulhifalhu lots), Ihthisaas (council boundaries) and the island layer. None is a harbour, jetty or port layer. The island layer is used only for island assignment. |

Rejected or unused:

- **Overpass API.** The plan was one bounded query. `overpass.private.coffee` and `overpass.kumi.systems` timed out or
  returned dispatcher errors even on a one-node query (2026-10-24), and `overpass-api.de` was not used because its
  `robots.txt` disallows `/api/`. The Geofabrik extract is the same OSM data and one polite download. The Overpass query
  builder and collector (`--source overpass`) remain for a later retry; nothing was hammered.
- **HDX** (`package_search` for Maldives harbour/jetty/port): 0 datasets.
- **Ferry PDFs / `stops.csv`:** island-level stops only; no jetty names and no coordinates. Used for matching, not as a coordinate source.
- **Census coordinates, island polygons, island centroids:** not harbours; never used as terminal locations.
- **MTCC, Ministry of Transport, Ports Limited:** no machine-readable public terminal list with coordinates was found; nothing scraped.
- No Google Maps, paid APIs or generated coordinates.

Raw downloads are unchanged in `data/reference/harbours/raw/` and parsed as untrusted data (type-checked fields, malformed elements
skipped, geometry never executed). Per-source URL, query text, retrieval date, SHA-256, licence and counts are in
`data/reference/harbours/provenance.json`. **Attribution to carry with any derived product:** "Contains data © OpenStreetMap
contributors, available under the Open Database Licence (ODbL) 1.0." Derived databases must stay ODbL-compatible.

## Outputs (Part A, public, no GPS content)

- `data/reference/harbours/harbour_reference.geojson` and `.csv`: one row per source feature (2,933).
- `harbour_reference_review.csv`: 2,154 non-low-confidence or terminal-class rows with `review_status` (default `unreviewed`) and `reviewer_note` to fill in.
- `coverage.json`, `provenance.json`.

Columns: `harbour_ref_id` (`hr-<source>-<type>-<id>[-start|-end]`, a function of source and source id only),
`harbour_group_id`, `group_size`, `source`, `source_feature_id`, `feature_class`, `name`, `name_alt`
(name:en, name:dv, alt_name, official_name, int_name), `operator`, `longitude`/`latitude` (representative point on the
geometry, never an area centroid), `geometry_type`, `island_id`/`island_name`/`island_atoll`, `island_assignment`,
`island_distance_m`, `study_area_code`, `inside_study_area`, `ferry_stop_match`/`ferry_stop_id`/`ferry_stop_match_method`,
`confidence`, `review_status`, `tags_json` (all OSM tags). The GeoJSON geometry is the original geometry.

### Features by class (study area code of the assigned island)

| Class | Total | K | AA | ADh | V | Greater_Male | unassigned |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| pier | 2,163 | 1,219 | 299 | 440 | 67 | 18 | 120 |
| breakwater | 537 | 232 | 187 | 48 | 36 | 25 | 9 |
| ferry_route_endpoint (evidence only) | 110 | 55 | 17 | 9 | 0 | 20 | 9 |
| ferry_terminal | 41 | 15 | 6 | 5 | 1 | 14 | 0 |
| marina | 38 | 8 | 8 | 9 | 1 | 10 | 2 |
| port_area | 17 | 10 | 1 | 1 | 0 | 3 | 2 |
| ferry_stop_position | 10 | 7 | 1 | 2 | 0 | 0 | 0 |
| quay | 8 | 0 | 0 | 6 | 0 | 0 | 2 |
| harbour | 3 | 2 | 0 | 0 | 0 | 1 | 0 |
| dock | 2 | 1 | 0 | 0 | 0 | 0 | 1 |
| seamark_mooring | 2 | 2 | 0 | 0 | 0 | 0 | 0 |
| ferry_platform | 1 | 0 | 1 | 0 | 0 | 0 | 0 |
| seamark_small_craft_facility | 1 | 0 | 1 | 0 | 0 | 0 | 0 |

2,810 of 2,933 rows have their representative point inside the study mask. Most piers are resort jetties; OSM `marina` is
widely used in this area for island harbours (for example `AA. Ukulhas`).

## Rules

**Island assignment (never by centroid).** A feature's representative point inside an OneMap island polygon gives
`within_polygon` (distance 0; 317 rows). Otherwise the nearest island polygon boundary within **500 m**, measured from the full
feature geometry in UTM 43N metres, gives `nearest_shoreline` with `island_distance_m` (2,471 rows). Beyond 500 m no island is
assigned (145 rows, 22 of them inside the mask). `study_area_code` is from the island's OneMap atoll (`Greater_Male` = the three
Malé-city features, matching the mask file).

**Ferry-stop match.** `island_name` (assigned island equals the stop's island, atoll agrees; the Malé-area stops that carry no atoll accept
`Greater_Male` or `K`), `island_alias` (table in config; `Villimalé` → `Vilin'gili`), `feature_name_token` (name contains the stop
island and does not contradict the assigned island; never for route endpoints, whose names describe journeys).

**Confidence** reflects tagging completeness, not verification. `high`: a named terminal-class feature (ferry terminal, platform, stop position)
or a named harbour/port/marina/dock with an island within polygon or ≤100 m. `low`: route endpoints, features with no island, unnamed
breakwater/quay. `medium`: everything else. For instance "Maafannu Sahara Bus Stop" is tagged `amenity=ferry_terminal` in OSM and scores high;
review before relying on it.

**De-duplication.** Single-linkage groups within **150 m**: both named with similarity ≥0.8 (difflib ratio on normalised names), or exactly one
unnamed, or both unnamed from *different* sources. Unnamed same-source neighbours never chain, otherwise resort jetty networks merge into one
group. Every source row is kept; only `harbour_group_id` is shared. With one OSM source today, groups are within-source clusters (2,672 groups, largest 43).

## Coverage

Of 35 inhabited ("Residential Island") study islands, **34 have at least one reference feature** and **31 have a terminal-grade feature** (any class except route endpoints and breakwaters).
Of 37 ferry-schedule stops in the study atolls (including the Malé-area stops with no atoll prefix), **34 match any feature, 31 a terminal-grade feature**.

- No feature at all: **ADh Mandhoo**.
- Only breakwaters: **ADh Dhidhdhoo, V Felidhoo, V Keyodhoo**.
- Ferry stops without a terminal-grade feature: ADh Dhidhdhoo, ADh Mandhoo, V Felidhoo, V Keyodhoo, **K Himmafushi** (only the Malé–Himmafushi route endpoint, which falls on Malé), **Gulhifalhu** (no feature).

Absence here means OSM has not mapped it, not that no jetty exists.

## Greater Malé resolution

Separate features exist for each, from OSM only. No coordinate was invented.

- **Malé:** `Hulu Male' Ferry Terminal` (way 302137560; this OSM name is on the Malé side of the Hulhulé link), `Heinveru Ferry Terminal (public ferry to other atolls)`, `Jetty #5`, `Jetty #6`, `Jetty #7` (ferry terminals, including `MLE Airport Employees Ferry (Jetty #5)`), `Male Airport Jetty` (tagged marina), five further unnamed ferry-terminal rows, the harbours `Male Commercial Harbor`, `Male North Harbour`, `Male South Harbour`, `Male SW Harbor`, `MPL Dock`, `Male Customs Dock`. The island-level `Malé` stop matches all of them.
- **Hulhumalé:** `Hulhumalé Ferry Terminal` (way 302137545), `Hulhumale International Terminal` (port area), an unnamed harbour and ferry terminal.
- **Villimalé (= Vilin'gili in the Malé-city layer):** three unnamed marinas and an unnamed ferry terminal, plus the `Malé to Villimalé Ferry` route endpoints. The alias is an inference from spelling (`data/manual/ferry/stop_name_aliases.csv` already treats `villimale` = `vilimale`); it needs local confirmation.
- **Airport / Hulhulé:** the island layer assigns `Jetty № 7`–`№ 10` (piers) and the stop position `Ferry to Male` to Hulhule, a Kaafu industrial island, so they do not match the Malé stop (on purpose). `Jetty #7` (Malé side) and `Jetty № 7` (Hulhulé side) share a number in OSM; do not merge them without local confirmation.
- **Thilafushi:** three unnamed piers only; no named terminal or harbour polygon.
- **Gulhifalhu:** nothing in OSM or OneMap (OneMap has only a plot-locator layer for it, and the island layer has no Gulhifalhu feature). Unresolved.

## Part B/C method (private, not yet run on real data)

`scripts/build_stop_candidates.py`, settings in `config/harbour_stops.json`.

1. Input is `gps_region_clean.parquet` restricted to `usable_for_trajectory` and a trajectory ID, joined to `movement_segments.parquet`. An observation can only extend a stay when the preceding observation in the same trajectory is linked by an **accepted** transition; any other pair, and every trajectory start, is a break. Nothing is interpolated and `speed_raw` is not used.
2. **Stay points** are anchored at the first fix: every later fix within `radius_m` (default 50 m) and the window spans at least `dwell_seconds` (default 600 s) with at least 2 fixes. Defaults are **provisional**. The run writes `stop_sensitivity.csv` over radius 30/50/100 m × dwell 300/600/1200 s; inspect it and the dwell distribution before fixing defaults.
3. **Clusters:** deterministic DBSCAN in UTM metres, `eps_m` 75, `min_samples` 3 stay points, points processed in (lat, lon, stay id) order.
4. **Statistics:** stop count, distinct devices and days, dwell p10/p50/p90, 24-hour start profile (clock timezone is unconfirmed), centroid, spread p50/p90/max.
5. **Disclosure:** `meets_disclosure_minimum` needs ≥5 distinct devices **and** ≥3 distinct days (configurable). All outputs stay in `data/private/followme/`; the matches CSV carries counts, never device keys.
6. **Classification** by cluster centroid: `insufficient` (below the minimum), else `matched_reference` (nearest non-endpoint reference geometry ≤150 m), else `near_island_no_reference` (inside or ≤250 m from an island polygon), else `offshore` (anchorage, mooring, fishing ground or resort mooring; **not assumed to be a terminal**).
7. `harbour_reference_without_gps_cluster.csv` lists reference features with no nearby cluster. That reflects GPS coverage, not that the feature is unused.

Private outputs: `stop_points.parquet`, `stop_clusters.parquet`, `stop_sensitivity.csv`, `harbour_candidate_matches.csv`, `harbour_reference_without_gps_cluster.csv`, `stop_run_manifest.json`.

**A GPS cluster never confirms a terminal.** Do not describe devices as ferries or stops as passenger services. `confirmed` may only be entered by a
human in `review_status` of `harbour_candidate_matches.csv` / `harbour_reference_review.csv`.

## Review checklist

1. Is the feature a public passenger landing, a resort or private jetty, a cargo or industrial quay, a fishing or boat harbour, or a breakwater?
2. Does the name or operator tag agree with the ferry PDFs for that island? Are two features the same jetty (`harbour_group_id`)?
3. Does the satellite imagery or local knowledge confirm a structure at the point? (No imagery was used here.)
4. Is the island assignment right near reclamation (Hulhumalé, Hulhulé, Thilafushi, Gulhifalhu)?
5. For a GPS cluster: does it sit on the reference feature or shoreline? Is it dominated by one device (see `distinct_devices`)? Could it be a home, workplace, resort mooring or anchorage?
6. Record `review_status` ∈ {`unreviewed`, `confirmed`, `rejected`, `needs_local_check`} and a `reviewer_note`.

## Rerun

```bash
.venv/bin/python scripts/collect_harbour_sources.py          # network; saves raw files unchanged
.venv/bin/python scripts/build_harbour_reference.py          # offline, deterministic
.venv/bin/python scripts/build_stop_candidates.py            # only once both FollowMe manifests are complete
.venv/bin/python -m pytest tests/test_harbour_reference.py tests/test_stop_candidates.py -q
```
