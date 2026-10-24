You are working in our Maldives Datathon 2026 repository (`datathon-2026/`). Build a documented, reproducible harbour/jetty reference for the study area, then compare it with stop locations derived from the cleaned FollowMe GPS data.

Read any AGENTS.md/CLAUDE.md, the README, `docs/followme_cleaning.md`, `docs/followme_geography.md` and `docs/followme_data_dictionary.md` before editing. Reuse the existing conventions: Python + DuckDB + Parquet, versioned JSON config in `config/`, scripts in `scripts/`, tests in `tests/`, `.venv/bin/python`. Implement and run the work, not just a plan.

## Context

We are building an inter-island mobility and connectivity system. Study area: Alif Alif (AA), Alif Dhaalu (ADh), Kaafu (K), Vaavu (V) and Greater Malé.

Already available:
- `data/reference/followme/study_area.geojson`: reviewed study-area mask (OCHA COD-AB v02 ADM1, unioned with all 379 OneMap island footprints). Provenance is in `geography_provenance.json`.
- `data/reference/followme/onemap_study_islands.geojson`: OneMap island polygons, including uninhabited, resort and reclaimed islands.
- Ferry PDFs, extracted schedules (`scripts/ferry_pipeline/`, `data/interim/ferry/` or similar) and a Census island code lookup. Use their stop and island names for matching.
- Private, pseudonymised FollowMe outputs in `data/private/followme/`: `gps_clean.parquet`, `movement_segments.parquet`, `trajectory_segments.parquet`, `gps_region_clean.parquet`, `regional_trajectory_segments.parquet`, plus `run_manifest.json` and `regional_run_manifest.json`.

Census coordinates are island reference points, not harbours. Island polygons and centroids are not terminals.

## 0. Preconditions

Check that `data/private/followme/run_manifest.json` and `regional_run_manifest.json` both have `"status": "complete"`, that every validation value is true, and that no `failed_run.json`, `regional_failed_run.json` or `.run.lock` exists. If the GPS outputs are not complete, do Part A (external references) first, and do Part B only once they are valid. Never consume partial outputs.

## Part A: External harbour/jetty references

Collect candidate harbour, jetty, pier, ferry-terminal and marina features covering the study area. Use the study-area bounds with a small, documented margin.

Sources, in order of preference:
1. **OpenStreetMap.** Use the Overpass API (one bounded query with polite timeouts and no hammering) or a Geofabrik Maldives extract. Capture at least:
   - `amenity=ferry_terminal`
   - `public_transport=station|platform|stop_position` with `ferry=yes`
   - `man_made=pier|breakwater|quay`
   - `harbour=*`, `seamark:type=harbour|mooring|small_craft_facility`
   - `leisure=marina|slipway`, `waterway=dock`
   - `landuse=harbour|port`, `industrial=port`
   - `route=ferry`: extract route way endpoints as possible terminal evidence, not as terminals themselves.

   Keep nodes, ways and relations. Store way/relation geometry plus a representative point. Keep `name`, `name:dv`, `name:en`, `operator` and all tags.
2. **OneMap (readme.onemap.mv / Geomatics Department).** Find out whether public ArcGIS REST layers exist for harbours, jetties or ports. The previous session queried the island layer; see `onemap_query.json` and `onemap_island_layer_metadata.json` for the endpoint pattern. Download only public layers, recording the service URL, layer metadata and licence (Data Usage License PDF).
3. **Other public sources, only where the terms allow:** HDX datasets, official Ministry of Transport, MTCC or Ports Limited terminal lists, and the ferry PDFs' named terminals. Do not scrape sites whose terms or robots.txt forbid it. Do not use paid APIs, Google Maps scraping or AI-generated coordinates.

Explicitly resolve the Greater Malé terminals as separate features where the sources support it: Malé jetties and ferry terminals (including Villimalé and Hulhumalé ferry terminals, and the airport/Hulhulé jetties) and the Thilafushi/Gulhifalhu industrial harbours. Do not invent a coordinate that no source provides.

For every source, record the following in `data/reference/harbours/provenance.json`: URL, query text, retrieval date, licence/attribution (OSM requires ODbL attribution), file SHA-256 and feature counts. Keep raw downloads unchanged in their own directory under `data/reference/harbours/raw/`, and parse them as untrusted data.

Produce `data/reference/harbours/harbour_reference.geojson` and a matching `.csv`, with one row per source feature:
- `harbour_ref_id`: stable, derived from the source plus its source ID.
- `source`, `source_feature_id`, `feature_class`, `name`, `name_alt`, `tags_json`.
- Representative `longitude`/`latitude` and the original geometry.
- `island_id`/`island_name`: by spatial join to the OneMap island polygons, using a documented nearest-shoreline distance when the feature is offshore. Never assign by centroid.
- `study_area_code`, `inside_study_area`.
- `ferry_stop_match`: the matched ferry-schedule stop name and method, if any.
- `confidence`: documented rule.
- `review_status`: default `unreviewed`.

De-duplicate across sources without discarding anything. Group features within a documented distance and name similarity into `harbour_group_id`, and keep every source row.

Report coverage: how many inhabited study islands and ferry-schedule stops have at least one reference feature, and list those that have none.

## Part B: GPS-derived stop candidates (private)

Use only `usable_for_trajectory` observations and accepted transitions. Speed units are unconfirmed, so use derived displacement and time, not `speed_raw`.

1. **Detect stay points per trajectory.** These are consecutive observations staying within a configurable radius for at least a configurable dwell time. Inspect distributions before choosing the defaults, and test sensitivity across a few radius/dwell values. Never bridge rejected transitions or trajectory splits. Do not interpolate.
2. **Cluster stay points across devices and days.** For example, use DBSCAN with a haversine metric or an equivalent deterministic method with documented parameters.
3. **Write per-cluster statistics:** stop count, distinct devices, distinct days, dwell quantiles, hour-of-day profile, and centroid plus spread.
4. **Apply disclosure rules.** Device-level stays stay private in `data/private/followme/stop_points.parquet`. Cluster outputs (`data/private/followme/stop_clusters.parquet`) are also private by default. Mark which clusters meet a configurable minimum of distinct devices and days for a possible aggregate summary. Never export device keys or tracks outside `data/private/`.

## Part C: Match candidates to references

For each stop cluster, find the nearest `harbour_reference` features and the nearest OneMap island shoreline. Classify each cluster as one of:
- `matched_reference`: within a documented distance of a reference feature.
- `near_island_no_reference`: a possible unmapped jetty to review.
- `offshore`: an anchorage, mooring, fishing ground or resort mooring. Do not assume it is a terminal.
- `insufficient`: too few devices or days.

Also list reference features with no nearby GPS stop cluster. That reflects coverage, not proof the feature is unused.

Write `data/private/followme/harbour_candidate_matches.csv` and a short `docs/harbour_reference.md` covering sources, licences, rules, thresholds, coverage gaps and the review checklist.

**A GPS cluster alone never makes a confirmed terminal.** A terminal is `confirmed` only through an explicit human review step. Provide a review CSV with `review_status` and `reviewer_note` columns to fill in. Do not claim that devices are ferries, that stops are passenger services, or that clusters are official terminals.

## Tests and validation

Add tests for:
- The Overpass/OneMap parsers, using small fixture files with no network access in tests.
- Stable `harbour_ref_id`s.
- Cross-source de-duplication that keeps every source row.
- Island assignment by polygon or shoreline distance, never by centroid.
- Stay-point detection not bridging trajectory splits or rejected transitions.
- Deterministic clustering.
- Disclosure thresholds.
- Correct classification of matched, offshore and unmatched clusters.

Make sure no device key or raw identifier appears outside `data/private/`, and that reruns with identical inputs and config give identical outputs.

Run the existing tests (`.venv/bin/python -m pytest -q`) and the new ones, and update the README with the exact commands. Do not commit, push or publish anything unless asked.

## Final report

Report:
1. Sources used, with retrieval dates, licences and feature counts, plus any sources rejected and why.
2. Reference features by class and study area. Coverage of inhabited islands and ferry stops, and which have none.
3. The Malé terminal resolution.
4. Stop-detection parameters and sensitivity results, and cluster counts by classification.
5. Unresolved items needing organiser or local confirmation.
6. Output paths, test results and rerun commands.
