# Stop-location change: state at pause (2026-10-24)

## Done
- `stops.stop_location_mode` added to `config/vessel_services.json` (`island_shoreline` default, `jetty` kept). Validated in `vessel_pipeline/config.py`.
- `geo.py`: `uses_shoreline()`, `build_sites(..., mode)`. In shoreline mode every island is a shoreline band, the harbour reference is not read, and full scope = Residential Islands only.
- `build_vessel_services.py`: `--stop-location-mode`, `--out-dir` (comparison runs; skips private tables). Jetty ids are null in `vessel_trips.parquet`. `jetty_selection.csv` is not written (a stale copy is deleted in shoreline mode). `stop_location` block and `stop_location_mode` column added to outputs. Wording is "from a stop at Island X".
- `build.py`: the `stop_location` field on stops, wording, and the no_jetty note is suppressed in shoreline mode.
- Tests added or fixed (`tests/test_vessel_services.py`, 22 pass). Docs updated in `docs/vessel_datasets.md`.
- **Corridor rerun finished** (shoreline, 150 m, 120 s, time basis corrected, full 3x3 sensitivity). Outputs are in `data/derived/vessels/`; private trips are in `data/private/followme/`.

## Not done
- **Full study area run**: `python scripts/build_vessel_services.py --scope full --use-cache`. It has not been run, and the full-scope island list changes in shoreline mode.
- Reasons for appeared/disappeared services are only partly attributed (see below).
- `README.md` and `docs/vessel_pipeline_notes.md` were not touched.

## Corridor results (jetty+corrected -> shoreline+corrected, 150 m / 120 s)
Stops 30,522 -> 45,603. Trips 1,305 -> 1,693. Services 32 -> 41. Regular 23 -> 29. Signatures 19 -> 14.
The earlier raw-time jetty run had 34 services and 1,362 trips. Its files were overwritten and the snapshot lost, so those two numbers come only from the earlier summary.
The clock correction changed 0 timestamps and excluded 0 journeys, so all differences come from the stop mode.
Sensitivity grids: `service_summary.json` (shoreline). The jetty grid is in
`/private/tmp/claude-501/-Users-iyaadh-Documents-Datathon-Goats/c067521e-04b2-4fd9-900a-82a5efa5ca02/scratchpad/jetty_corrected_corridor/service_summary.json`.
Shoreline gives more stops and trips at every setting, with the biggest gap at 100 m (e.g. 100 m/120 s: 23,822 -> 45,307 stops).

Services (matched by route, direction and first-stop median within 30 min): 27 matched, none changed regular status.
- Appeared (14): extra Maafushi<->Malé, Malé>Rasdhoo>Thoddoo/Ukulhas variants, Mathiveri>Ukulhas>Malé, Thoddoo>Malé 02:05.
- Disappeared (5): Mathiveri>Ukulhas 07:54, Rasdhoo>Thoddoo 12:39, Rasdhoo>Ukulhas 12:38, Malé>Thoddoo 05:17 and 11:05. All were non-regular.
- Likely reason (not fully verified): an earlier or later stop on the route is now detected, so the same service shows with a longer route (Malé>Thoddoo became Malé>Rasdhoo>Thoddoo; Mathiveri>Ukulhas became Mathiveri>Ukulhas>Malé).
- Trips per island rose at every island, most at Malé (329 -> 486 origins), Thoddoo and Rasdhoo.
- Maafushi services appeared because the band catches stops that the jetty radius missed.
- Caveat: the shoreline band can count vessels passing or anchored near an island, and 32 land devices are still flagged but not filtered.
- Several appeared services have only 3 devices (the disclosure minimum).
