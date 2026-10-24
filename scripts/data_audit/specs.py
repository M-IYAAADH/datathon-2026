"""Hand-written metadata for every catalogued dataset.

`build_data_catalog.py --init` turns these into data/catalog.csv and fills rows, columns, checksum and size from
the files. Provenance is copied from the repo's own provenance files; anything not recorded there is UNKNOWN.
Verification fields start as UNVERIFIED (or NEEDS_ORGANISER where the meaning depends on an open organiser answer);
only scripts/audit_data.py --update-catalog or a human changes them afterwards.
"""
from __future__ import annotations

U = "UNKNOWN"
OD = "n/a (derived in this repo)"
GEN = "2026-10-24"  # all generated outputs in the repo were produced on this date
ORG = "NEEDS_ORGANISER"


def D(dataset_id, name, path, description, owner, source=OD, retrieval_date=OD, licence="n/a (derived)",
      key_columns="", joins_to="", date_range="", private="no", status="final", verification_status="UNVERIFIED",
      known_issues="", used_by="", blockers="", next_action="", count_rows=True, secret=False, expect="present",
      date_col="", sibling=False):
    return dict(dataset_id=dataset_id, name=name, path=path, description=description, owner=owner, source=source,
                retrieval_date=retrieval_date, licence=licence, key_columns=key_columns, joins_to=joins_to,
                date_range=date_range, private=private, status=status, verification_status=verification_status,
                known_issues=known_issues, used_by=used_by, blockers=blockers, next_action=next_action,
                count_rows=count_rows, secret=secret, expect=expect, date_col=date_col, sibling=sibling)


FERRY = "scripts/extract_ferry_schedules.py (scripts/ferry_pipeline/)"
FERRY_SRC = "MTCC ferry timetable PDFs (11 files; URL not recorded)"
FERRY_LIC = U + " (published MTCC/operator PDFs; licence not recorded)"
FERRY_RET = U + " (PDF creation dates 2022-05-09 to 2026-08-27 in sources.csv; no retrieval date recorded)"
FERRY_USE = "journey planner routing; scripts/build_alif_male_subset.py; scripts/build_vessel_services.py (inferred_vs_mtcc)"
VES = "scripts/build_vessel_services.py"
VES_USE = "journey planner (inferred services layer)"
VES_BLOCK = "Organiser answers: timezone, speed unit, device vs vessel (data/organiser_answers.md Q1, Q2, Q3)"
FM = "scripts/run_followme_pipeline.py"
FM_SRC = "FollowMe GPS CSVs for August 2025, supplied by the organisers (../FollowMe_august_2025/)"
FM_LIC = U + " (organiser-supplied; terms not recorded)"

DATASETS = [
    # ---- Census and geography -------------------------------------------------------------------------
    D("census_2022_per_island", "Census 2022 per island (209 files)", "per_island",
      "One CSV per island with a single 2022 row: population by sex, age bands, disability, elderly living alone, island_code, atoll_code, coordinates. 209 files include one 'Non admin island' row per atoll.",
      "UNKNOWN (committed in repo history 600b217; producer not recorded)", source=U + " (README says '2022 census extract'; no URL)",
      retrieval_date=U, licence=U, key_columns="island_code", joins_to="island_code (master join key); LD#### FCODE = island_code",
      date_range="reference_year 2022", known_issues="Source, retrieval date and licence not recorded. Names differ from other datasets (Maale, Hulhumaale; some rows carry an atoll prefix such as 'AA.Maalhos'). 19 of the 209 files are per-atoll 'Non admin island' aggregates, not real islands.",
      used_by="journey planner population/vulnerability context; island_code join for everything", next_action="Ask whoever added per_island/ for the Census source and licence", count_rows=True, status="final"),
    D("onemap_island_footprints", "OneMap island footprints (379 islands in scope)", "data/reference/followme/onemap_study_islands.geojson",
      "OneMap island polygons for AA, ADh, K, V and Greater Malé (379 features); FCODE LD#### equals the Census island code.",
      "scripts/prepare_followme_geography.py", source="OneMap (Geomatics Department, Ministry of Infrastructure, Housing and Urban Development), layer island_20240509",
      retrieval_date="2026-10-24", licence="OneMap Data Usage License (https://readme.onemap.mv/pdf/Data%20Usage%20License.pdf)",
      key_columns="FCODE", joins_to="island_code (FCODE LD#### = Census island_code)", date_range="layer dated 2024-05-09",
      known_issues="Only the 5 study atolls (not all 209 Census islands). 2025 shorelines and post-2024 reclamation not verified. Male area has 3 'MLE' polygons (see audit check 4).",
      used_by="FollowMe regional selection; scripts/build_vessel_services.py; scripts/collect_weather.py; audit check 4", status="final"),
    D("ocha_cod_ab_boundaries", "OCHA COD-AB Maldives administrative boundaries (raw zip)", "data/reference/followme/mdv_admin_boundaries.geojson.zip",
      "Government of Maldives / OCHA COD-AB v02 administrative boundaries, as downloaded.", "downloaded by scripts/prepare_followme_geography.py",
      source="https://data.humdata.org/dataset/cod-ab-mdv", retrieval_date="2026-10-24", licence="CC BY-IGO",
      joins_to="atoll polygons (no island_code)", known_issues="10 MB third-party raw download: gitignored; SHA-256 pinned in geography_provenance.json.",
      used_by="scripts/prepare_followme_geography.py", count_rows=False, status="final"),
    D("study_area_polygon", "Study area polygon (AA, ADh, K, V, Greater Malé)", "data/reference/followme/study_area.geojson",
      "Union of reviewed COD-AB polygons supplemented by all 379 OneMap footprints; defines the FollowMe regional selection.",
      "scripts/prepare_followme_geography.py", source="OCHA COD-AB v02 + OneMap", retrieval_date="2026-10-24", licence="CC BY-IGO; OneMap Data Usage License",
      known_issues="No legal marine boundary for Greater Malé is asserted.", used_by="scripts/build_followme_region.py; scripts/collect_weather.py", status="final"),
    D("study_island_coverage", "Study island coverage table", "data/reference/followme/study_island_coverage.csv",
      "Per OneMap island: which study atoll it belongs to and whether COD-AB covers it.", "scripts/prepare_followme_geography.py",
      source="derived from OneMap + COD-AB", retrieval_date="2026-10-24", licence="CC BY-IGO; OneMap Data Usage License",
      key_columns="FCODE", joins_to="island_code (FCODE)", used_by="docs/followme_geography.md; audit check 3", status="final"),
    D("geography_metadata", "Geography source metadata and provenance", "data/reference/followme/geography_provenance.json|data/reference/followme/hdx_metadata.json|data/reference/followme/onemap_atoll_values.json|data/reference/followme/onemap_island_layer_metadata.json|data/reference/followme/onemap_query.json",
      "Provenance, hashes and raw service metadata for the polygon layers.", "scripts/prepare_followme_geography.py", source="HDX and OneMap services",
      retrieval_date="2026-10-24", licence="CC BY-IGO; OneMap Data Usage License", count_rows=False, status="final", used_by="docs/followme_geography.md"),
    # ---- Health facilities and terminals -------------------------------------------------------------
    D("health_facilities", "Health facilities (corrected version)", "data/reference/health/health_facilities.csv",
      "47 facilities across Malé, AA, ADh, K and V after the Gemini audit plus a second pass that moved coordinates outside the island polygon to the polygon centroid.",
      "AI-assisted: Gemini audit, then a second-pass polygon-centroid fix (tool and script not recorded)", source="OpenStreetMap / MoH HEMS (per row source_name)",
      retrieval_date="2026-10-24 (per row retrieval_date)", licence="ODbL 1.0 (per row licence)", key_columns="facility_name", joins_to="island + atoll_code (names; no island_code column)",
      known_issues="6 rows UNVERIFIED (Mathiveri Eesa Clinic, Maamigili Clinic, Advance Diagnostic Center, AMDC Clinic, Atoll Clinic, Life Wellness Clinic). 39 of 47 coordinates are island-level only; coordinate_quality says LOCATED-BY-ISLAND-ONLY, not island_centroid, for the 4 rows moved to polygon centroids. No island_code column. Script that produced this file is not in the repo.",
      used_by="journey planner health-facility layer (planned)", blockers="Six UNVERIFIED rows; no island_code column", next_action="Verify the six UNVERIFIED rows; add island_code via the crosswalk", status="in_progress"),
    D("health_facilities_gemini_audited", "Health facilities, Gemini-audited copy (archive only)", "data/archive/health_facilities_gemini_audited/health_facilities_gemini_audited.csv",
      "Untouched Gemini-audited version; differs from the corrected file in latitude, longitude, original_value and change_note for 4 rows.",
      "Gemini (AI tool; prompt and date not recorded)", source="OpenStreetMap / MoH HEMS (per row)", retrieval_date="2026-10-24 (per row)", licence="ODbL 1.0 (per row)",
      key_columns="facility_name", joins_to="island + atoll_code (names)", status="deprecated",
      known_issues="Archive-only. Byte-identical copy also sits in the parent folder (../health_facilities_audited.csv). 4 coordinates fall outside their island polygon (fixed in health_facilities.csv).",
      used_by="nothing (archive)", next_action="Keep; do not use"),
    D("maritime_terminals_gemini_audited", "Maritime terminals, Gemini-audited (archive only)", "data/archive/maritime_terminals_gemini_audited/maritime_terminals_gemini_audited.csv",
      "48 ferry terminal rows audited by Gemini against OSM way/node IDs; copied from ../maritime_terminals_audited.csv.",
      "Gemini (AI tool; prompt and date not recorded)", source="OpenStreetMap (per row)", retrieval_date="2026-10-24 (per row)", licence="ODbL 1.0 (per row)",
      key_columns="terminal_name", joins_to="island + atoll_code (names)", status="deprecated",
      known_issues="Archive-only. File has a UTF-8 BOM. atoll_code uses 'Male City'. Provenance beyond row columns is UNKNOWN. Not yet checked against OSM by this repo.",
      used_by="nothing (archive)", next_action="Keep; do not use"),
    # ---- Harbours / jetties (deprecated) -------------------------------------------------------------
    D("harbour_reference", "Harbour and jetty reference (OSM, unreviewed)", "data/reference/harbours/harbour_reference.csv|data/reference/harbours/harbour_reference.geojson|data/reference/harbours/harbour_reference_review.csv|data/reference/harbours/provenance.json|data/reference/harbours/coverage.json",
      "2,933 OSM harbour/jetty/terminal features assigned to OneMap islands and matched to ferry stops. Unreviewed; GPS clusters are candidates only.",
      "scripts/build_harbour_reference.py", source="OpenStreetMap Geofabrik Maldives extract (osm_geofabrik_maldives.osm.pbf)", retrieval_date="2026-10-24",
      licence="ODbL 1.0", key_columns="harbour_ref_id", joins_to="island_id (LD####), ferry_stop_id", status="deprecated", verification_status="UNVERIFIED",
      known_issues="Marked deprecated by the team lead's instruction, but scripts/build_vessel_services.py still reads harbour_reference.csv for stop detection; do not delete. Files are not tracked-safe to move.",
      used_by="scripts/build_vessel_services.py (jetty selection); scripts/build_stop_candidates.py", next_action="Decide replacement before any change to vessel services", count_rows=True),
    D("jetty_verification", "Jetty verification review", "data/manual/harbours/jetty_verification.csv",
      "Manual review rows for jetty points.", U + " (reviewer or tool not recorded)", source="manual review of harbour_reference", retrieval_date=U, licence=U,
      key_columns="harbour_ref_id", status="deprecated", known_issues="Producer and review date UNKNOWN. Deprecated with the harbour files.", used_by="scripts/build_vessel_services.py"),
    D("harbour_raw_sources", "Harbour raw downloads (OSM PBF, OneMap service metadata)", "data/reference/harbours/raw",
      "Raw OSM Geofabrik PBF and OneMap service directory/metadata as retrieved.", "scripts/collect_harbour_sources.py",
      source="https://download.geofabrik.de/asia/maldives-latest.osm.pbf; OneMap service directory", retrieval_date="2026-10-24T05:35:23Z", licence="ODbL 1.0; OneMap Data Usage License",
      status="deprecated", known_issues="PBF (3.7 MB) is gitignored. SHA-256 in data/reference/harbours/raw/retrieval.json.", used_by="scripts/build_harbour_reference.py", count_rows=False),
    # ---- Weather -------------------------------------------------------------------------------------
    D("weather_hourly", "Weather August 2025, hourly wind and waves", "data/reference/weather/weather_hourly.parquet",
      "45 query points x 744 hours (2025-08-01 00:00 to 2025-08-31 23:00 UTC): wind, gusts, direction, pressure, precipitation, wave height/direction/period, swell. Modelled, not observed.",
      "scripts/collect_weather.py (config/weather.json)", source="Open-Meteo Historical Weather API (ECMWF IFS) and Marine API (NOAA GFS Wave)", retrieval_date="2026-10-24 (see provenance.json retrieval_timestamps_utc)",
      licence="CC BY 4.0 (free API tier: non-commercial use only)", key_columns="point_id, time_utc", joins_to="point_id -> weather_points; island names via weather_points.onemap_fcodes",
      date_range="2025-08-01T00:00Z .. 2025-08-31T23:00Z", known_issues="time_local starts 1 Aug 05:00 local, so the first five local hours of 1 Aug are missing. Non-commercial API terms. Island points are island-area proxies.",
      used_by="journey planner weather exposure; scripts/collect_weather.py", date_col="time_utc", status="final"),
    D("weather_points", "Weather query points", "data/reference/weather/weather_points.csv", "45 query points: 35 sea grid, 7 island endpoints, 3 corridor midpoints, with returned cells.",
      "scripts/collect_weather.py", source="derived from OneMap polygons", retrieval_date="2026-10-24", licence="OneMap Data Usage License", key_columns="point_id", joins_to="onemap_fcodes (LD####)", used_by="weather_hourly", status="final"),
    D("weather_daily_summary", "Weather daily summary", "data/reference/weather/daily_summary.csv", "Per point per local day maxima/means and hours above thresholds.",
      "scripts/collect_weather.py", source="derived from weather_hourly", retrieval_date=OD, licence="CC BY 4.0", key_columns="point_id, date_local", joins_to="point_id", date_range="2025-08-01 .. 2025-09-01 local (32 local dates)",
      used_by="journey planner weather exposure", status="final"),
    D("weather_provenance", "Weather provenance and validation", "data/reference/weather/provenance.json|data/reference/weather/validation.json",
      "Requests, retrieval timestamps, returned model fields, licence and the pipeline's own validation results.", "scripts/collect_weather.py", source="Open-Meteo", retrieval_date="2026-10-24", licence="CC BY 4.0", count_rows=False, status="final", used_by="docs/weather_data.md"),
    D("weather_raw", "Weather raw API responses", "data/reference/weather/raw", "Request and response JSON for every Open-Meteo call (incl. failed attempts).", "scripts/collect_weather.py", source="Open-Meteo", retrieval_date="2026-10-24", licence="CC BY 4.0",
      known_issues="Includes empty files from failed attempts (0 bytes). Each file under 0.2 MB.", count_rows=False, status="final", used_by="weather_hourly lineage"),
    # ---- Ferry (MTCC) --------------------------------------------------------------------------------
    D("ferry_source_pdfs", "MTCC ferry timetable PDFs (11 files)", "data/raw/ferry_pdfs", "The 11 source PDFs. Gitignored (*.pdf) and absent from this checkout (expected in the repo root or data/raw/ferry_pdfs/), so extraction cannot be rerun here.",
      "MTCC / operators", source=FERRY_SRC, retrieval_date=FERRY_RET, licence=FERRY_LIC, status="in_progress", expect="missing",
      known_issues="MISSING locally. File names and SHA-256 are in data/processed/ferry/sources.csv.", blockers="Original PDFs not available to verify extraction against", next_action="Place the 11 PDFs in the repo root (or data/raw/) and rerun extract_ferry_schedules.py --validate-only", count_rows=False),
    D("ferry_trips", "Ferry trips (all versions)", "data/processed/ferry/trips.csv", "430 trips across 11 sources and 148 routes, all versions. Use only routing_eligible = 1 (via routing_eligibility.csv): 100 trips, latest CFN 2026 (cfn_2026_all) plus CFN Zone 6 and two Thaa/Laamu speed routes. CTN 2022 and CTN 2024 are historical/duplicate.",
      FERRY, source=FERRY_SRC, retrieval_date=FERRY_RET, licence=FERRY_LIC, key_columns="trip_id", joins_to="origin_stop_id/destination_stop_id -> stops.stop_id (no island_code yet)", date_range="effective 2022-05-15 (CTN 2022) .. 2026-08-24; most sources print no date",
      known_issues="Published schedules, not evidence trips ran. Only 5 documents print an effective date. Timezone Asia/Male is an assumption. 73 open issues in extraction_issues.csv.", used_by=FERRY_USE,
      blockers="Stop-to-Census crosswalk MISSING; currency of CFN 2026 unconfirmed", next_action="Join stops to Census once the crosswalk lands", status="final"),
    D("ferry_legs", "Ferry legs", "data/processed/ferry/legs.csv", "1,366 legs: departure stop/time to arrival stop/time.", FERRY, source=FERRY_SRC, retrieval_date=FERRY_RET, licence=FERRY_LIC, key_columns="leg_id", joins_to="trip_id, stop_id", used_by=FERRY_USE),
    D("ferry_stop_times", "Ferry stop times", "data/processed/ferry/stop_times.csv", "1,796 per-stop arrival/departure times per trip (seconds from start of service day).", FERRY, source=FERRY_SRC, retrieval_date=FERRY_RET, licence=FERRY_LIC, key_columns="trip_id, stop_sequence", joins_to="trip_id, stop_id", used_by=FERRY_USE),
    D("ferry_stops", "Ferry stops (199)", "data/processed/ferry/stops.csv", "199 stop identities with atoll abbreviation; official_island_code and coordinates are blank for all.", FERRY, source=FERRY_SRC, retrieval_date=FERRY_RET, licence=FERRY_LIC,
      key_columns="stop_id", joins_to="official_island_code (blank) -> Census island_code", known_issues="official_island_code blank for all 199 stops: no join to Census. 8 stops atoll_unresolved, 13 have no atoll.", used_by=FERRY_USE,
      blockers="Crosswalk to Census island codes MISSING", next_action="Wait for ChatGPT crosswalk, then fill official_island_code"),
    D("ferry_routes", "Ferry routes", "data/processed/ferry/routes.csv", "148 route records.", FERRY, source=FERRY_SRC, retrieval_date=FERRY_RET, licence=FERRY_LIC, key_columns="route_id", used_by=FERRY_USE),
    D("ferry_services", "Ferry services (calendars)", "data/processed/ferry/services.csv", "180 service calendars: weekday flags and calendar_status per block.", FERRY, source=FERRY_SRC, retrieval_date=FERRY_RET, licence=FERRY_LIC, key_columns="service_id", used_by=FERRY_USE),
    D("ferry_schedule_versions", "Ferry schedule versions", "data/processed/ferry/schedule_versions.csv", "180 block-level version decisions (preferred_candidate / duplicate / historical / unresolved).", FERRY, source=FERRY_SRC, retrieval_date=FERRY_RET, licence=FERRY_LIC, key_columns="version_record_id", used_by="deciding which MTCC version the app uses"),
    D("ferry_routing_eligibility", "Ferry routing eligibility", "data/processed/ferry/routing_eligibility.csv", "Per trip flags; routing_eligible = 1 marks the 100 trips the app should use.", FERRY, source=FERRY_SRC, retrieval_date=FERRY_RET, licence=FERRY_LIC, key_columns="trip_id", joins_to="trips.trip_id", used_by=FERRY_USE),
    D("ferry_sources", "Ferry source documents", "data/processed/ferry/sources.csv", "11 PDFs with SHA-256, pages, printed effective dates and date evidence.", FERRY, source=FERRY_SRC, retrieval_date=FERRY_RET, licence=FERRY_LIC, key_columns="source_id",
      date_range="printed effective dates: 2022-05-15, 2026-03-21, 2026-08-24; others blank", known_issues="source_url blank for every row: where the PDFs were downloaded is UNKNOWN.", used_by="audit check 5/7"),
    D("ferry_stop_aliases", "Ferry stop aliases", "data/processed/ferry/stop_aliases.csv", "808 raw stop spellings mapped to stop_id.", FERRY, source=FERRY_SRC, retrieval_date=FERRY_RET, licence=FERRY_LIC, key_columns="alias_id", joins_to="stop_id"),
    D("ferry_extraction_issues", "Ferry extraction issues", "data/processed/ferry/extraction_issues.csv", "137 recorded issues (11 errors, 61 warnings, 65 info); 73 open.", FERRY, source=FERRY_SRC, retrieval_date=FERRY_RET, licence=FERRY_LIC, key_columns="issue_id", used_by="docs/ferry_extraction_report.md"),
    D("ferry_frequency", "Ferry frequency windows (Vilimalé etc.)", "data/processed/ferry/frequency_windows.csv|data/processed/ferry/frequency_eligibility.csv", "25 headway windows; none are routable.", FERRY, source=FERRY_SRC, retrieval_date=FERRY_RET, licence=FERRY_LIC, key_columns="frequency_id"),
    D("ferry_subsets_alif_male", "Alif atolls to Malé subset", "data/processed/ferry/subsets", "Direct and one-transfer journeys between AA/ADh islands and Malé/Hulhumalé. Transfers are derived, not published.", "scripts/build_alif_male_subset.py", source="derived from data/processed/ferry", retrieval_date=OD, licence=FERRY_LIC,
      known_issues="Minimum transfer time and landing jetty unknown (notes column).", used_by="journey planner corridor demo", status="in_progress"),
    D("ferry_interim_evidence", "Ferry extraction evidence", "data/interim/ferry", "Raw text, parsed rows/blocks, coverage, version diffs, validation report.", FERRY, source=FERRY_SRC, retrieval_date=FERRY_RET, licence=FERRY_LIC, count_rows=False, used_by="audit check 7 (version_diffs.csv)"),
    D("ferry_manual_decisions", "Ferry reviewed decisions", "data/manual/ferry", "Aliases, overrides, page-review log and Dhivehi footnote readings.", "human + AI agent review (per file)", source=FERRY_SRC, retrieval_date=FERRY_RET, licence=FERRY_LIC,
      known_issues="Dhivehi readings were made by an AI agent and are marked needs_human_confirmation.", count_rows=False, used_by=FERRY),
    # ---- Vessel services (shareable) -----------------------------------------------------------------
    D("vessel_corridor_services", "Inferred vessel services, corridor (app JSON)", "data/derived/vessels/corridor_services.json", "32 inferred services for Mathiveri, Bodufolhudhoo, Ukulhas, Rasdhoo, Malé, Thoddoo, Maafushi; every service inferred: true.",
      VES, source="FollowMe August 2025 (private), jetty reference, clock offsets", retrieval_date=OD, licence=FM_LIC, key_columns="service_id", joins_to="stops[].island_code (LD####)", date_range="2025-08-01 .. 2025-08-31",
      verification_status=ORG, status="in_progress", known_issues="Times read as Asia/Male without confirmation; speed unit knots unconfirmed; thresholds provisional; a device is not a vessel.", used_by=VES_USE, blockers=VES_BLOCK, next_action="Re-run after organiser answers"),
    D("vessel_service_signatures", "Inferred service signatures", "data/derived/vessels/service_signatures.csv", "19 ordered-stop signatures with class mix, days seen and device counts.", VES, source="FollowMe August 2025 (private)", licence=FM_LIC, key_columns="signature_id", joins_to="stop_island_codes (LD####)", date_range="2025-08-01 .. 2025-08-31", verification_status=ORG, status="in_progress", used_by=VES_USE, blockers=VES_BLOCK),
    D("vessel_service_departures", "Inferred service departures", "data/derived/vessels/service_departures.csv", "Per service and stop: median and p10-p90 time, days seen, weekday counts.", VES, source="FollowMe August 2025 (private)", licence=FM_LIC, key_columns="signature_id, service_id, stop_order", joins_to="island_code (LD####)", date_range="2025-08-01 .. 2025-08-31", verification_status=ORG, status="in_progress", used_by=VES_USE, blockers=VES_BLOCK),
    D("vessel_service_edges", "Inferred service edges", "data/derived/vessels/service_edges.csv", "Island-pair and class level services per observed day, first/last departures, duration.", VES, source="FollowMe August 2025 (private)", licence=FM_LIC, key_columns="origin_code, destination_code, class", joins_to="island_code (LD####)", date_range="2025-08-01 .. 2025-08-31", verification_status=ORG, status="in_progress", used_by=VES_USE, blockers=VES_BLOCK),
    D("vessel_service_days", "Inferred service days", "data/derived/vessels/service_days.csv", "Signature x date observed/not_observed with coverage flag.", VES, source="FollowMe August 2025 (private)", licence=FM_LIC, key_columns="signature_id, date", date_range="2025-08-01 .. 2025-08-31", verification_status=ORG, status="in_progress", known_issues="not_observed with coverage_flag=limited carries almost no information.", used_by="weather matching; " + VES_USE, blockers=VES_BLOCK),
    D("vessel_inferred_vs_mtcc", "Inferred services vs published MTCC departures", "data/derived/vessels/inferred_vs_mtcc.csv", "Published MTCC departures beside inferred services on the same island pairs, with wait to the next published departure.", VES, source="MTCC extraction + FollowMe-derived services", licence=FM_LIC, key_columns="origin_code, destination_code, source, ref_id", joins_to="island_code (LD####)", verification_status=ORG, status="in_progress", used_by=VES_USE, blockers=VES_BLOCK),
    D("vessel_jetty_selection", "Jetty rows used or skipped", "data/derived/vessels/jetty_selection.csv", "Every harbour reference row considered for the vessel stops, with decision and reason.", VES, source="harbour_reference (OSM, unreviewed)", licence="ODbL 1.0", key_columns="harbour_ref_id", joins_to="island_code (LD####)", status="in_progress", used_by=VES_USE, known_issues="Depends on the deprecated harbour_reference."),
    D("vessel_service_summary", "Vessel service run summary", "data/derived/vessels/service_summary.json", "Parameters, counts, disclosure rule, suppression counts, time_basis, clock correction, flags timezone_confirmed and speed_unit_confirmed.", VES, source="FollowMe August 2025 (private)", licence=FM_LIC, count_rows=False, verification_status=ORG, status="in_progress", used_by="audit check 6"),
    D("vessel_hour_profiles", "Departure hour profile and night services", "data/derived/vessels/departure_hour_profile.csv|data/derived/vessels/night_services_2200_0500.csv", "Information only: trip departures by hour (raw vs corrected) and services whose first-stop median is 22:00-05:00.", VES, source="FollowMe August 2025 (private)", licence=FM_LIC, verification_status=ORG, status="in_progress", known_issues="Never evidence of a wrong clock.", used_by="information only", blockers=VES_BLOCK),
    D("vessel_clock_offsets", "Device clock offset distributions", "data/derived/vessels/clock", "National distribution of database_time - collection_time, by network and day; clock class counts. 0 device-days corrected.", "scripts/build_clock_offsets.py", source="FollowMe August 2025 (private)", licence=FM_LIC, count_rows=False, verification_status=ORG, status="in_progress", used_by=VES, blockers="Organiser answer on timezone (Q1)"),
    # ---- FollowMe private ----------------------------------------------------------------------------
    D("followme_raw_august_2025", "FollowMe raw CSVs, August 2025 (outside repo)", "../FollowMe_august_2025", "31 daily CSVs (1.csv .. 31.csv, 1.8 GB, 17,462,749 rows) with raw IMEI. Untouched originals; never copy into the repo.",
      "organisers", source=FM_SRC, retrieval_date=U + " (files dated 2026-10-14 on disk)", licence=FM_LIC, private="yes", key_columns="poll_id", date_range="2025-08-01 .. 2025-08-31 (device_date)",
      known_issues="Contains raw IMEI. Fingerprinted by file sizes only (1.8 GB); per-file SHA-256 and row counts are in data/private/followme/input_manifest.csv. Sits outside the repo (parent .gitignore, parent is not a git repo).",
      used_by=FM, verification_status=ORG, sibling=True, next_action="Keep outside the repo", status="final"),
    D("followme_gps_clean", "FollowMe cleaned, national", "data/private/followme/gps_clean.parquet", "17,233,072 canonical observations with pseudonymous device_key, after dedup and quarantine.", FM, source=FM_SRC, retrieval_date=U, licence=FM_LIC, private="yes", key_columns="observation_id, device_key", joins_to="trajectory_id; no island_code",
      date_range="2025-08-01 .. 2025-08-31", verification_status=ORG, known_issues="timezone_status unconfirmed; speed unit unconfirmed (speed_kmh derived).", used_by="scripts/build_clock_offsets.py; scripts/build_followme_region.py", blockers="Timezone, speed unit (Q1, Q2)", date_col="collection_time", status="final"),
    D("followme_gps_region_clean", "FollowMe cleaned, regional (AA, ADh, K, V, Greater Malé)", "data/private/followme/gps_region_clean.parquet", "10,392,355 observations in complete trajectory segments intersecting the study area.", "scripts/build_followme_region.py", source=FM_SRC, retrieval_date=U, licence=FM_LIC, private="yes", key_columns="observation_id, device_key", joins_to="segment_study_area_codes (atoll codes)",
      date_range="2025-08-01 .. 2025-08-31", verification_status=ORG, used_by=VES, blockers="Timezone, speed unit (Q1, Q2)", date_col="collection_time", status="final"),
    D("followme_trajectories", "FollowMe trajectory and movement segments", "data/private/followme/trajectory_segments.parquet|data/private/followme/movement_segments.parquet|data/private/followme/regional_trajectory_segments.parquet|data/private/followme/regional_movement_intersections.parquet", "National and regional per-device trajectories and movement segments.", "scripts/run_followme_pipeline.py; scripts/build_followme_region.py", source=FM_SRC, retrieval_date=U, licence=FM_LIC, private="yes", verification_status=ORG, count_rows=False, used_by=VES),
    D("followme_support_tables", "FollowMe quarantine, lineage, geography, poll-id audit", "data/private/followme/gps_quarantine.parquet|data/private/followme/observation_lineage.parquet|data/private/followme/gps_geography.parquet|data/private/followme/poll_id_audit.parquet", "32,869 quarantined rows, 17,462,749-row lineage, per-observation geography.", FM, source=FM_SRC, retrieval_date=U, licence=FM_LIC, private="yes", count_rows=False, used_by="audit"),
    D("followme_clock_tables", "FollowMe clock offset tables (per device)", "data/private/followme/clock_device_day.parquet|data/private/followme/clock_devices.parquet|data/private/followme/clock_device_difference.parquet|data/private/followme/clock_observations.parquet", "Per device-day clock class and corrected time column.", "scripts/build_clock_offsets.py", source="derived from followme_gps_clean", retrieval_date=OD, licence=FM_LIC, private="yes", verification_status=ORG, count_rows=False, used_by=VES, blockers="Timezone (Q1)"),
    D("followme_vessel_tables", "Inferred vessel classes, trips, journeys (private)", "data/private/followme/vessel_classes.parquet|data/private/followme/vessel_trips.parquet|data/private/followme/vessel_journeys.parquet", "Per-device classes, 1,305 trips and 1,137 journeys with device keys.", VES, source="followme_gps_region_clean", retrieval_date=OD, licence=FM_LIC, private="yes", key_columns="device_key", verification_status=ORG, status="in_progress", count_rows=False,
      known_issues="Per-device tracks: must never leave data/private/.", used_by="shareable aggregates in data/derived/vessels", blockers=VES_BLOCK),
    D("followme_reports_manifests", "FollowMe reports, manifests and audits", "data/private/followme/run_manifest.json|data/private/followme/regional_run_manifest.json|data/private/followme/validation_results.json|data/private/followme/cleaning_report.md|data/private/followme/regional_cleaning_report.md|data/private/followme/input_manifest.csv", "Run manifests, validation results and cleaning reports.", FM, source=FM_SRC, retrieval_date=U, licence=FM_LIC, private="yes", count_rows=False, used_by="audit check 5; scripts/build_vessel_services.py precondition"),
    D("followme_staging", "FollowMe staging (1.4 GB)", "data/private/followme/staging", "Pipeline intermediate files.", FM, source=FM_SRC, retrieval_date=U, licence=FM_LIC, private="yes", count_rows=False, status="in_progress", known_issues="Regenerable. Size changed during this audit session (1.9 GB to 2.7 GB): something else is writing here, so its checksum is expected to drift. Not hashed byte-by-byte unless --deep."),
    D("followme_sample", "FollowMe 2,000-row sample run", "data/private/followme_sample", "Test run with --sample-rows 2000.", FM, source=FM_SRC, retrieval_date=U, licence=FM_LIC, private="yes", count_rows=False, status="in_progress"),
    D("followme_geography_review", "FollowMe geography review files", "data/private/followme_geography_review", "Working files from the geography review.", "scripts/prepare_followme_geography.py", source="OneMap + COD-AB", retrieval_date="2026-10-24", licence="CC BY-IGO; OneMap", private="yes", count_rows=False, status="in_progress"),
    D("followme_device_secret", "FollowMe pseudonymisation secret", "data/private/followme.secret", "Secret used to derive device_key; reuse it for stable keys. Never read, hashed or shared.", FM, source=OD, licence="n/a", private="yes", count_rows=False, secret=True, known_issues="Secret: only existence and size are recorded."),
    D("followme_legacy_pipeline_out", "Legacy FollowMe boat-filter output (outside repo)", "../FollowMe_pipeline/out", "Earlier prototype output: 31 daily parquet files of hashed-device pings plus device_class.csv. Logic is reused by the vessel pipeline.",
      "../FollowMe_pipeline/boat_filter.py (author not recorded)", source=FM_SRC, retrieval_date=U, licence=FM_LIC, private="yes", count_rows=False, sibling=True, status="in_progress",
      known_issues="Per-device tracks (salted-hash dev ids) live outside data/private/, in a sibling folder outside the repo. Salt policy per README_boat_filter.md (IMEI_SALT env). Not a repo dataset.", used_by="docs/vessel_pipeline_notes.md (logic reference)"),
    # ---- Organiser answers and MISSING ---------------------------------------------------------------
    D("organiser_answers", "Organiser answers log", "data/organiser_answers.md", "Open questions for the organisers (timezone, speed unit, device vs vessel, MTCC shift, transfer rules). All OPEN.", "team (this file)", source="n/a", retrieval_date=OD, licence="n/a", status="in_progress", count_rows=False, blockers="Waiting on organisers", next_action="Send questions; record answers with date and who answered"),
    D("ferry_stop_census_crosswalk", "Ferry stop to Census island crosswalk", "data/reference/crosswalk/ferry_stop_census_island.csv", "Maps each ferry stop_id to a Census island_code. ChatGPT is building it.", "ChatGPT (in progress)", source=U, retrieval_date=U, licence=U, status="in_progress", expect="missing",
      key_columns="stop_id, census_island_code", joins_to="stops.stop_id -> island_code", known_issues="MISSING.", blockers="Not delivered", next_action="Chase; verify with audit check 3 when it lands", count_rows=False),
    D("route_exposure", "Route exposure data", "data/reference/exposure/route_exposure.csv", "Per-route exposure to open sea and weather. Not started in this repo.", U, source=U, retrieval_date=U, licence=U, status="in_progress", expect="missing", known_issues="MISSING.", blockers="Not delivered; no owner recorded", next_action="Assign an owner", count_rows=False),
    D("weather_warning_format", "Official weather warning format", "data/reference/weather/warning_format.md", "Format/thresholds of official Maldives Meteorological Service warnings. Not obtained.", U, source=U, retrieval_date=U, licence=U, status="in_progress", expect="missing", known_issues="MISSING.", blockers="Not delivered; weather thresholds in config/weather.json are provisional", next_action="Ask organisers or MMS for the format", count_rows=False),
]
