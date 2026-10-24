# data/raw/

Untouched third-party originals belong here. Nothing has been moved in yet: the existing pipelines read their
raw files from the places below, and moving them would break `config/` and the scripts. `data/catalog.csv` records
each one with its path, checksum and provenance.

| Raw original | Where it is now | Notes |
| --- | --- | --- |
| FollowMe CSVs 1-31 Aug 2025 (private) | `../FollowMe_august_2025/` (outside the repo) | never copy into the repo |
| MTCC ferry PDFs | not present (gitignored `*.pdf`; expected in the repo root) | MISSING locally; SHA-256 in `data/processed/ferry/sources.csv` |
| OSM Maldives extract | `data/reference/harbours/raw/osm_geofabrik_maldives.osm.pbf` | gitignored, 3.7 MB |
| OCHA COD-AB boundaries | `data/reference/followme/mdv_admin_boundaries.geojson.zip` | gitignored, 10 MB |
| Open-Meteo API responses | `data/reference/weather/raw/` | request and response per query |
| OneMap service metadata | `data/reference/harbours/raw/onemap_service_metadata/` | |
