# Inferred vessel service datasets

Built by `scripts/build_vessel_services.py` from the cleaned FollowMe outputs. Settings: `config/vessel_services.json`
(every threshold is **provisional**). Background on the reused slow/fast logic: [vessel_pipeline_notes.md](vessel_pipeline_notes.md).

## Approved wording

Labels are **inferred class** and **inferred service**. Say "observed device movements consistent with …".

| Situation | Label |
| --- | --- |
| regular, fast-vessel | inferred speedboat service |
| regular, slow-vessel | inferred slow ferry/dhoni service |
| slow vessel that does not repeat | inferred supply/other |
| anything else | inferred vessel movements (class unclear) |

Never write confirmed ferry, official timetable, passenger service, specific vessel, "cancelled" or a single exact time.
Use ranges ("usually departs Mathiveri 07:10-07:30, seen on 22 of 31 days"). Missing GPS is not non-operation; "not observed"
is not "cancelled".

## Inputs and preconditions

`clock_device_day.parquet` (from `scripts/build_clock_offsets.py`, run it first), `gps_region_clean.parquet`, `movement_segments.parquet` (plus the regional/national trajectory summaries) in
`data/private/followme/`. Both run manifests must be `complete` with all validation values true and no `failed_run.json`,
`regional_failed_run.json` or `.run.lock`, else the script exits. Only `usable_for_trajectory` observations are used. A break exists
at every trajectory start and wherever the preceding observation is not linked by an accepted transition; nothing is bridged or
interpolated. The raw CSVs are not read.

Stop location (`stops.stop_location_mode` in `config/vessel_services.json`, recorded in `service_summary.json` under `stop_location` and as a
`stop_location_mode` column in the shareable CSVs; override with `--stop-location-mode`):

- `island_shoreline` (default): a stop is at the island. Every island uses a band of `radius_m` (150 m) either side of its OneMap shoreline.
  `data/reference/harbours/` is not read at all, jetty verification is ignored, and the full scope is every `Residential Island` (the
  "islands with a qualifying jetty" extension applies only in jetty mode). Outputs say "at Island X", never a named jetty; trip tables
  hold null jetty ids and `jetty_selection.csv` is not written.
- `jetty` (kept for later): stops within `radius_m` of a verified jetty point (`harbour_reference.csv`: numeric OSM id and
  `confidence = high`, or VERIFIED/CORRECTED rows when a verification file exists); islands with no usable jetty use the shoreline band,
  flagged `no_jetty_point`. `jetty_selection.csv` lists every row used or skipped.

An island centroid is never a stop location. Island names and codes (`LD####`, equal to the Census island code) are the OneMap ones in every output.

## Method

1. **Stops:** a run of consecutive observations of one trajectory inside the stop zone (the 150 m shoreline band in the default mode, `radius_m` of a jetty in jetty mode) for at
   least `dwell_seconds` (120). A gap over `stop_gap_flag_seconds` (300) inside a stop marks it `unconfirmed`; it is flagged, never filled.
   Arrival/departure are the first/last in-zone observation (the true departure can be later by up to one ping interval).
   A stop at the start/end of a trajectory has an unobserved arrival/departure and is not used for that time.
2. **Visits:** per trajectory, stops in time order, repeated consecutive stops at one island collapsed.
3. **Journeys and direction:** a visit sequence is split where the direction of travel relative to Malé reverses (distance of
   the island's anchor (centroid-type point, direction ordering only) to Malé's). `toward_male` / `away_from_male`.
4. **Signatures:** ordered island codes + direction. A shorter signature maps to a longer canonical route (own support >= 8 days) when it is
   an ordered subsequence omitting at most one stop; the omitted stop is counted (`skipped_stop_share`).
5. **Classes:** per device from speed derived from accepted segments: `fast_vessel` p90 >= 18 kn, `slow_vessel` p90 < 14 kn, `mixed` in
   between; `unclassified` without a >= 2 km moving path or when < 40 % of moving observations are off land. Evidence and confidence in
   `vessel_classes.parquet`. A land-device flag (>= 50 % of sampled observations on island interior, 50 m from shore) is kept as a column.
   Classes are review hints and never filter rows.
6. **Regular service:** journeys that start at the canonical first stop are clustered by departure time of day (window 15 min, most distinct
   days first). A cluster seen on >= 8 distinct days (of 31) is regular; the rest are `not_regular_*`. Grouping is by time, not device.
7. **Disclosure:** a row needs >= 3 distinct devices **and** >= 5 distinct days, else it is not exported (counts in
   `service_summary.json`). Per-date device counts under 3 print as `<3`.

## Device clock offsets

Approved wording: **"times corrected for a measured clock offset on some devices, pending organiser confirmation"**. Use it only
when `clock_correction.observations_corrected_in_scope` is above zero; otherwise say that no correction was applied.
The organisers have not confirmed the timezone of either time field.

**Measure** (`scripts/build_clock_offsets.py`, cleaned `gps_clean.parquet` only, no raw CSVs). For every retained observation the
difference is `database_time - collection_time` in seconds. `reporting_lag_seconds` exists in `gps_clean.parquet` but is NULL for every
row while the timezone is unconfirmed, so the difference is computed from the two timestamps. Outputs in `data/derived/vessels/clock/`:
`difference_histogram_15min.csv`, `difference_by_network.csv` (1 Dhiraagu, 2 Ooredoo, 0 other, unexpected codes),
`difference_by_day.csv`, `device_median_distribution.csv`, `clock_class_counts.csv`, `observation_hour_profile.csv`,
`clock_offsets_summary.json`. Per-device detail stays in `data/private/followme/` (`clock_device_difference.parquet`).

**Classify** (per device-day, thresholds in `config/clock_offset.json`, all provisional). Device-day = one device on one
`collection_time` date; the unit is the median difference and the share of observations within 15 minutes of that median.

| Class | Rule |
| --- | --- |
| `local` | median difference between -60 s and 900 s (near 0 plus normal reporting lag), whatever the tail |
| `offset_5h` | at least 20 observations, median within 1800 s of 18000 s, and at least 80 % of observations within 900 s of the median |
| `other` | at least 20 observations, a consistent difference (same 80 % rule) that is not local and not near 5 h |
| `unknown` | fewer than 20 observations, or a non-local median with a wide spread (delayed or buffered upload, not a clock offset) |

A device has a stable class when at least 3 device-days were measured and at least 90 % agree. Unknown device-days of a stable
device inherit its class (`class_reason` says so); a device that switches class keeps each day's own measured class.

**Correct.** `clock_observations.parquet` (private) carries `collection_time` (never overwritten), `collection_time_local_corrected`
and `clock_class` per observation. Only `offset_5h` device-days are shifted (+18000 s). `build_vessel_services.py --time-basis
corrected` (default) uses the corrected time for every departure, arrival and time-of-day cluster; `raw` uses `collection_time`.
Journeys of `unknown` and `other` devices stay in the private trip and journey tables and in `service_edges.csv`, but are not used to
form regular services by default (`--clock-exclude`, default in config; `--clock-exclude ""` keeps all). Every output states the
time basis (`time_basis` column or metadata), `clock_correction` (method, evidence summary, counts corrected and unchanged) and
`clock_exclusion` (excluded classes and journeys dropped). Hour-of-day profiles (`observation_hour_profile.csv`,
`departure_hour_profile.csv`) and `night_services_2200_0500.csv` are for information only and never decide a correction.

**Evidence, and what it does not show.** Validation does not assume when boats operate: (a) stability of each device's daily median
across days, (b) hour-of-day profiles before and after, shown without judging any hour, (c) a weak comparison with MTCC departures on
the corridor, where private boats need not match official ferries. A fleet-wide offset, or a device whose `database_time` shares its
clock error, gives a difference near 0 and cannot be detected this way. Results for the August 2025 data are in the clock outputs
above and in the project report.

## Outputs

Private (`data/private/followme/`, device keys allowed, git-ignored): `clock_device_day.parquet`, `clock_devices.parquet`, `clock_observations.parquet`, `clock_device_difference.parquet`, `vessel_classes.parquet`, `vessel_trips.parquet` (one row per trip
between two consecutive stops), `vessel_journeys.parquet` (journey, canonical route, service category).

Shareable (`data/derived/vessels/`, aggregates only):

| File | Content |
| --- | --- |
| `service_signatures.csv` | ordered stops, direction, class mix, days seen of 31, distinct devices, skipped-stop shares |
| `service_departures.csv` | per service and stop: median and p10-p90 time, days seen, weekday counts (`Mon:4/4`), weekdays not observed, regular flag, unconfirmed-origin share |
| `service_edges.csv` | island pair and class: services per observed day, first/last departure ranges, duration, days seen, weekday pattern, coverage notes |
| `service_days.csv` | signature x date: `observed`/`not_observed`, devices seen, coverage flag (`good`/`limited`) and reason |
| `corridor_services.json` | demo corridor services for the web app, every service `inferred: true`, with `timezone_assumed`, `speed_unit_status`, provenance |
| `inferred_vs_mtcc.csv` | published MTCC departures beside inferred services on the same island pairs, with the wait to the next published departure |
| `jetty_selection.csv` | jetty mode only: every jetty row used or skipped, and why (absent in `island_shoreline` mode) |
| `service_summary.json` | parameters, counts, thresholds, sensitivity results, disclosure rule and suppression counts, speed-unit check, `time_basis`, `clock_correction`, `clock_exclusion` |
| `departure_hour_profile.csv`, `night_services_2200_0500.csv` | information only: trip departures by hour (raw vs corrected) and services whose first-stop median is 22:00-05:00, with days seen and distinct devices |
| `clock/` | national clock-difference distribution, by network and day, class counts (see "Device clock offsets") |

`service_days.csv` coverage is `good` only if regional active devices that day were >= 70 % of the monthly median and >= 2 devices were
seen near the first stop. Where it is `limited`, `not_observed` carries almost no information (for weather matching).

## Assumptions and limits

- **Timezone unconfirmed.** `collection_time` (device_date) and `database_time` are naive timestamps; the organisers have not
  confirmed the timezone of either. Times are read as local Maldives time (Asia/Male, UTC+5), with a +5 h correction only for
  device-days whose measured clock difference clearly shows that offset (see "Device clock offsets"); `timezone_confirmed: false`.
  An unusual hour of day is never evidence of a wrong clock: private speedboats may run at any hour, night included. Treat every
  time as provisional until the organisers answer.
- **Speed unit unconfirmed.** Derived km/h divided by raw speed has a median of about 1.9, consistent with knots (1.852); `speed_unit_confirmed: false`.
- Trajectories split at gaps over 15 minutes, so a journey is only found when the whole trip, stop to stop, was continuously tracked.
  Slow or sparsely reporting boats are under-represented.
- Dense ports (Malé) and radius/dwell choices change counts (see `sensitivity` in the summary). A boat manoeuvring or anchored near an island, or a device carried along the shore, can
  make a false stop (the land-device flag is a hint only); speed is not used for stop detection.
- In `island_shoreline` mode the stop location is the island, not a landing point: it cannot tell two landings on one island apart, and a vessel passing within the band for the dwell time is a stop. In `jetty` mode, `no_jetty_point` islands use the shoreline band and the OSM jetty reference is unreviewed.
- With the 3-device minimum, a service run by one or two tracked devices is not exported.
- A device is not a vessel; one vessel can carry several devices or none.
