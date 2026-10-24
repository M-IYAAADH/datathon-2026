# Data consolidation: work state (stopped by the user mid-task)

Written 2026-10-24. This records where the data catalogue and audit work stands. It is not a finished deliverable.
Nothing was committed or pushed. No source data was deleted, overwritten or moved. No network calls were made.

## Done

- **Read** the README and all the docs named in the brief. There is no AGENTS.md or CLAUDE.md.
- **Inventory** of the repo and the sibling folders (`../FollowMe_august_2025`, `../FollowMe_pipeline`, the two `*_audited.csv` files in the parent folder).
- **Layout additions only** (no moves):
  - New folders with pointer READMEs: `data/raw/`, `data/inbox/`, `data/archive/`, `data/reference/crosswalk/`.
  - Copied `../maritime_terminals_audited.csv` to `data/archive/maritime_terminals_gemini_audited/maritime_terminals_gemini_audited.csv`. The original is untouched.
  - `.gitignore` now ignores the OSM `.pbf` (3.7 MB), the COD-AB zip (10 MB) and `data/inbox/*` (except its README).
  - `per_island/`, `data/reference/*`, `data/derived/*` and `data/private/*` stay where they are, because scripts and config point at them.
- **Catalogue**: `data/catalog.csv` has 60 datasets, 4 of them MISSING. Built by `scripts/build_data_catalog.py` from `scripts/data_audit/specs.py`.
  - Modes: `--init`, `--refresh`, `--add-new`.
  - Columns added beyond the brief: `columns`, `date_range`, `blockers`, `next_action`, `sha256`, `size_bytes`.
- **Organiser log**: `data/organiser_answers.md` has Q1-Q8, all OPEN.
- **Audit**: `scripts/audit_data.py` and `scripts/data_audit/{common,specs,catalog,checks,status}.py`, with settings in `config/data_audit.json`.
  - It runs checks 0-9 in about 25 s and writes `data/audit_report.md`, `data/audit_results.json` and `docs/DATA_STATUS.md`.
  - `--status-only`, `--update-catalog`, `--live` and `--deep` are implemented.
  - Check 9 (live OSM) was **not run**, so nothing is LIVE_CHECKED.

## Last audit run

Passed: 0 (catalogue schema), 4 (coordinates), 5 (time ranges), 6 (vessel services), 7 (ferry timetables).
Failed: 1, 2, 3, 8. Check 9 skipped.

| Check | Result | Reading |
| --- | --- | --- |
| 1 files and checksums | FAIL | `followme_staging` checksum drifted. Another session is running `build_vessel_services.py`, which writes there. Not a data problem. Also flagged the 4 MISSING datasets. |
| 2 privacy | FAIL | **False positive**: a 15-digit number in `data/reference/weather/provenance.json` was flagged as an IMEI. It is not one. Needs a tighter pattern, for example an allowlist for that file. The other privacy checks (nothing under `data/private/` is tracked, `data/private/` is gitignored, no `device_key` columns outside it) found nothing. |
| 3 joins | FAIL | The failure is real: `official_island_code` is blank for all 199 ferry stops and the crosswalk is MISSING. Census `island_code` is unique (209). All 7 vessel island codes match Census code and name. 41 ferry stops and 4 health rows do not map. The health rows are Hangnaameedhoo, Kunburudhoo, Dhangethi and Villimale. Many unmapped stops carry an apostrophe in the name (`Dha’ngethi`); the cause is not yet investigated. |
| 4 coordinates | PASS | 36 Census points and 43 health rows are within 1 km of their polygons. 4 health rows were unchecked because their island did not map. 154 Census islands have no polygon in the layer. |
| 7 ferry | PASS | 100 app trips, all from `cfn_2026_all`, `cfn_2026_zone6` and `speed_2026_thaa_laamu`. The 301 arrival at Rasdhoo (10:55) and the 303 departure from Rasdhoo (11:00) are both present. They are two different routes. 3 trips elsewhere in the data have non-monotonic times, none in the app set. Route 303 differs between 2022/2024 and 2026 only by added Hulhumalé legs. |
| 8 health | FAIL | 6 UNVERIFIED rows listed. The 4 coordinate-fixed rows are labelled `LOCATED-BY-ISLAND-ONLY`, not `island_centroid`. |

## Not done

- Fix the check-2 IMEI false positive, then investigate the apostrophe name mapping in check 3.
- Run `build_data_catalog.py --refresh` once the other session's vessel run finishes. The vessel files and staging will have changed.
- Write `data/README.md` (one section per dataset plus a join diagram).
- Write `docs/data_verification.md` and `data/verification_checklist.csv`, including the known-unknowns section.
- Write the tests (catalogue schema, audit checks on fixtures, privacy check) and run the full suite.
- Update `README.md` with the audit commands, and write `docs/ai_use.md`.
- Produce the final report, including the before/after tree. The "before" directory list is in the scratchpad: `/private/tmp/claude-501/-Users-iyaadh-Documents-Datathon-Goats/b2556794-36a7-490c-9605-8586aec73a28/scratchpad/tree_before.txt`.
- Run `--update-catalog` only after the failures above are resolved or accepted. No row has been set to AUTO_CHECKED yet.

## Things the user should know

- `harbour_reference.csv` and `jetty_verification.csv` are marked deprecated as asked, but `scripts/build_vessel_services.py` still reads them, so they must not be deleted.
- The Gemini-audited health file is byte-identical to `../health_facilities_audited.csv`.
- Salted-hash per-device tracks exist outside the repo, in `../FollowMe_pipeline/out/pings`. The parent folder is not a git repo and has its own `.gitignore`.
- The ferry PDFs are not in this checkout, so extraction cannot be re-verified here.
- Provenance is UNKNOWN for the Census per-island files (source, date, licence) and for the ferry PDFs (source URL, retrieval date, licence).
