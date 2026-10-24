# Data task board

Claim a task by telling the team lead its ID and filling in **Owner** below in your own branch. Status board for the datasets: [data/DATA_STATUS.md](../data/DATA_STATUS.md). Open organiser questions (Q1-Q8): [data/organiser_answers.md](../data/organiser_answers.md).

## How to claim a task

1. Tell the team lead which ID you took.
2. Create your own branch from `data/organise-ready-not-ready`: `git switch data/organise-ready-not-ready && git switch -c data/<ID>-<yourname>` (for example `data/D2-aisha`).
3. Work only in the folders that task names. Do not edit another person's claimed task.
4. Open a pull request into `data/organise-ready-not-ready` (not `main`).
5. Before asking for review: set the task's Status here, rerun `.venv/bin/python scripts/update_data_status.py`, and commit the regenerated `data/DATA_STATUS.md` and `data/data_status.csv`.

Never commit anything under `data/private/`, `device_key` or raw IMEI values, `.env` or `*.secret` files, raw third-party PDFs, raw FollowMe CSVs, or files over 50 MB.

## Tasks

| ID | Task | Type | Dataset | Priority | Done when | Owner | Status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| D1 | Rebuild vessel services with the clock-offset correction and island-shoreline stops | Build | `vessel_services_inferred` (`data/derived/vessels/`, `config/vessel_services.json`) | Blocks planner | `scripts/build_vessel_services.py --scope corridor` runs with the correction and shoreline stops; `tests/test_vessel_services.py` passes; `data/derived/vessels/` regenerated; status moves to READY or the remaining blockers are listed | | Open |
| D2 | Ferry-stop to Census island crosswalk: run the ChatGPT prompt, review the output, import it | Collect / Verify | `ferry_stop_census_crosswalk` (`data/reference/crosswalk/`) | Blocks planner | `data/reference/crosswalk/ferry_stop_census_island.csv` exists; every one of the 199 stops has a reviewed `census_island_code` or an explicit "no Census island" reason; `stops.official_island_code` filled; `scripts/audit_data.py` check 3 passes | | Open |
| D3 | Confirm the app uses the 2026 timetable; confirm the Rasdhoo 10:55 arrive / 11:00 depart connection (Route 301 to 303) and the Friday gaps | Verify | `ferry_timetables_2026` (`data/processed/ferry/`) | Blocks planner | Written confirmation (or a recorded organiser/MTCC answer to Q5 and Q6) that `cfn_2026_all` is in force and the connection is designed; Friday gaps listed in `docs/ferry_extraction_report.md` as real or extraction errors | | Open |
| D4 | Write `scripts/build_app_data.py` producing app-ready JSON (stops, legs, config, census, meta) | Build | `ferry_timetables_2026`, `census_2022_per_island`, `health_facilities` | Blocks planner | Script runs offline, writes the five JSON files to one output folder, uses only READY datasets, and a test checks the output schema and that no private column is present | | Open |
| D5 | Ask the organisers: device timezone, speed unit, one device = one vessel, how far MTCC can shift a departure, transfer rules (Q1-Q5; also Q6-Q8 if possible) | Ask organisers | `organiser_answers` | Needed soon | Each of Q1-Q5 has an answer, who gave it, the date and where, recorded in `data/organiser_answers.md`; dependent files re-run | | Open |
| D6 | Live-check OSM nodes for hospital rows with source-provided coordinates | Verify | `health_facilities` | Needed soon | Each row with `coordinate_quality` = source-provided is opened on OSM and `verification_status` is set to LIVE_CHECKED (with `verified_url`) or corrected | | Open |
| D7 | Resolve the six UNVERIFIED health rows | Verify | `health_facilities` | Optional | Rows for Mathiveri Eesa Clinic, Maamigili Clinic, Advance Diagnostic Center, AMDC Clinic, Atoll Clinic and Life Wellness Clinic are verified with a source URL or removed | | Open |
| D8 | Route exposure: classify each corridor leg as open water or sheltered lagoon | Build | `route_exposure` (`data/reference/exposure/route_exposure.csv`) | Needed soon | One row per corridor leg with class, method and source; reviewed by a second person | | Open |
| D9 | Weather-warning format from the Maldives Meteorological Service | Collect | `weather_warning_format` | Optional | Issuer, thresholds and how a warning maps to a route or atoll are written in `data/reference/weather/warning_format.md` with source and retrieval date; `config/weather.json` thresholds compared | | Open |
| D10 | Join weather to FollowMe days and `service_days` | Build | `weather_followme_join` (`weather_2025_08`, `vessel_services_inferred`) | Needed soon | A shareable aggregate (no per-device rows) joins weather by day and point; `docs/weather_data.md` explains the join; depends on the timezone answer (Q1) | | Open |
| D11 | Data audit script: existence, checksums, join integrity, privacy check | Build | all | Needed soon | `scripts/audit_data.py` already exists. Remaining: fix the check-2 IMEI false positive on `weather/provenance.json`, investigate the apostrophe stop-name mismatches in check 3, rerun and commit `data/audit_report.md` | | Open |
| D12 | Data section of the methods note and the AI-use record | Clean | all | Needed soon | Methods note lists each dataset used, its source, licence and limits; AI-use record names the Gemini audit, the ChatGPT crosswalk and Claude work, with what was checked by a human; Census source and licence filled in (Q8) | | Open |
