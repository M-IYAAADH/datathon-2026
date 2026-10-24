# Vessel pipeline notes (what was found, what was reused)

**Not to be confused with** the FollowMe *cleaning* package `scripts/followme_pipeline/`.

## Where it lives

The vessel pipeline is the sibling folder `../FollowMe_pipeline/` (outside this repository, not under version control here):

| File | Role |
| --- | --- |
| `boat_filter.py` (DT-51) | cleans raw daily CSVs, hashes IMEIs, flags land/sea pings, classifies each device for the month as `boat`, `land`, `stationary` or `uncertain` |
| `boat_types.py` | second pass: speed sub-type from the 90th percentile of sea speed, plus a map |
| `landmask.py`, `islands_bbox.csv` | raster land mask from 901 OSM island bounding boxes (+150 m buffer) |
| `README_boat_filter.md` | method and August 2025 results |

## What it expects

Raw daily CSVs (`1.csv` … `31.csv`) with `imei, device_date, latitude, longitude, speed, heading`, or a single parquet with the
same columns. It runs its **own** cleaning (duplicates, bad coordinates), hashes IMEIs with a salted SHA-256 (`IMEI_SALT`,
default `datathon2026`) and writes to its own `out/`. Libraries: pandas, numpy, matplotlib.

## How it classifies

| Item | Rule in the original |
| --- | --- |
| Speed field | raw `speed`, declared to be **knots** (checked by the authors: consecutive-ping distance ÷ time ≈ 1.85 × reported speed) |
| Moving | `speed >= 3` kn (hard-coded `MOVING_KN`) |
| Sea | outside the OSM-bbox land mask |
| Sea run | consecutive moving sea pings, gaps < 15 min (hard-coded `GAP_S`); displacement measured straight-line |
| `boat` | a sea run ≥ 2 km AND ≥ 40 % of moving pings at sea |
| `land` | < 15 % of moving pings at sea AND no sea run ≥ 1 km |
| `stationary` | never moved ≥ 3 kn |
| speed sub-type | p90 sea speed ≥ 18 kn **speedboat**; < 14 kn **slow vessel (dhoni/ferry/cargo)**; 14–18 kn **mid-speed** |
| confidence | `low` if the device made a ≥ 2 km sea run on only one day, else `high` |

## What the task description assumed but the code does not do

The task brief says the pipeline "treats vessels that follow the same route regularly as ferries". **It does not.** There is no
route, stop, jetty, schedule or ferry logic anywhere in `FollowMe_pipeline/`; the word "ferry" appears only in the sub-type label
"slow vessel (dhoni/ferry/cargo)". Only the slow/fast classification was reusable. Regular-service logic (stops, visit sequences,
signatures, departure clustering) is new code in `scripts/vessel_pipeline/`, built to the written specification. If a different
folder holds a route-regularity implementation, point me at it.

## Hard-coded or undocumented in the original

- thresholds `MOVING_KN = 3`, `GAP_S = 900`, 2 km / 1 km / 40 % / 15 % rules, 18 / 14 kn bands, 150 m land buffer;
- the speed unit (knots) is asserted, not configurable, and **unconfirmed** by the organisers;
- timestamp = `device_date`, assumed local, no timezone handling; wrong-day rows dropped by `device_date.dt.day != file day`;
- the land mask is bounding boxes, so a boat close to shore can count as land (documented by the authors);
- `boat_types.py` takes a population-weighted mean of daily p90 values; `p90` uses moving sea pings only.

## What was changed for this repository (adaptation, not a rebuild)

| Original | Here |
| --- | --- |
| reads raw CSVs and cleans them again | **reads the cleaned outputs** (`gps_region_clean.parquet`, `movement_segments.parquet`, regional and national trajectory segments); the raw data is not processed a second time. Only `usable_for_trajectory` observations and accepted transitions are used; rejected transitions and trajectory splits are never bridged; no interpolation |
| raw `speed` (knots) | speed **derived from accepted movement segments** (km/h ÷ 1.852); the raw field is used only for the unit check |
| OSM bbox land mask | OneMap island polygons; "land" = inside a polygon shrunk by 50 m (so boats at a jetty are not land); estimated on a deterministic sample of 400 observations per device; **flag only**, never a filter |
| 2 km straight-line sea run | ≥ 2 km of path length over consecutive accepted moving segments (displacement needs a separate join) |
| boat / land / stationary / uncertain | `slow_vessel` / `fast_vessel` / `mixed` (p90 14–18 kn) / `unclassified`; the label is a review hint and filters nothing |
| thresholds in code | all in `config/vessel_services.json`, marked provisional; `speed_unit_confirmed: false`, `timezone_confirmed: false` |
| IMEI hash with `IMEI_SALT` | the cleaning run's HMAC `device_key`; no raw identifier is ever read |

Original code in `../FollowMe_pipeline/` is untouched.
