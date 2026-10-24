# Ferry stop to Census island crosswalk: MISSING

Expected file: `data/reference/crosswalk/ferry_stop_census_island.csv` (ChatGPT is building it). Suggested columns:
`stop_id` (from `data/processed/ferry/stops.csv`), `census_island_code`, `census_island_name`, `match_method`, `reviewed_by`, `note`.
`stops.csv.official_island_code` is blank for all 199 stops today. When the file lands, add its checksum to `data/catalog.csv`
(`scripts/build_data_catalog.py --refresh`) and the audit's join check will use it.
