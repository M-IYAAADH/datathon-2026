# FollowMe GPS cleaning

This pipeline prepares observations and continuity segments for later harbour and crossing analysis.
It does not identify passenger boats, operated ferry trips, recurring services or official timetables.
All generated data, including pseudonymised trajectories and device-day statistics, are private.
No data is uploaded. Dependency installation is the only step requiring network access.
DuckDB handles Parquet directly; PyArrow is not a runtime dependency.

## Source and repository inventory

The supplied source is `../FollowMe_august_2025/`, containing daily CSV files and
`Summary of the available data.txt`. The configuration selects this explicit source root. CSV headers
must include `imei`, `device_date`, `latitude` and `longitude`. Census and timetable files elsewhere in
the repository are not searched as GPS inputs. `input_manifest.csv` inventories every candidate CSV,
ZIP and accompanying text/JSON/Markdown metadata, including excluded identical-file aliases.
SHA-256 hashes are checked again at the end. Original source files are never modified or moved.

No applicable AGENTS.md was present when implemented. Existing Python scripts/tests and pinned
`requirements.txt` conventions are reused. Census coordinates in `per_island/` represent island
reference points, not polygons or harbours. The published ferry timetable timezone assumption is
not evidence of FollowMe timezone semantics. No suitable boundary geometry existed locally. The required regional stage uses reviewed public
Government of Maldives/OCHA and OneMap references; see followme_geography.md.

## Install and run

From the repository root, using Python 3.12:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt

# Generate a random private local secret once, then run a bounded first-records sample per file.
.venv/bin/python scripts/run_followme_pipeline.py --init-secret \
  --sample-rows 2000 --output data/private/followme_sample

# Process every record from all selected CSVs.
.venv/bin/python scripts/run_followme_pipeline.py

# Repeat full cleaning and compare every deterministic data output with the completed run.
# Input and staged-file hashes are rechecked. Computations and exports are rerun.
.venv/bin/python scripts/run_followme_pipeline.py --reuse-staging --verify-repeat

# Synthetic GPS edge cases; the separate ferry workflow has its own tests.
.venv/bin/python -m pytest tests/test_followme_pipeline.py -q
```

The generated secret is `data/private/followme.secret`, mode 600. Back it up privately: losing or
changing it changes all device keys and observation/trajectory IDs. Alternatively set
`FOLLOWME_HMAC_SECRET` through an existing private environment/secret manager; it must contain at
least 32 UTF-8 bytes. Do not put a literal secret on a command line or commit it. The local secret file
stores random binary bytes; an environment value is interpreted literally as UTF-8, not as hex/base64.
There is no original-ID-to-key export. In-memory mappings are discarded after pseudonymisation.

`--input PATH` selects a directory, CSV or ZIP. `--output PATH` must be ignored by Git and outside the
source tree. Keep it under `data/private/`. Only create a new secret with `--init-secret` when one is
absent. Configuration is `config/followme.json`; use `--config PATH` to select a reviewed alternative.
Do not run two processes into the same output directory. An exclusive `.run.lock` prevents this.
If a process is killed, inspect before removing a stale lock. `failed_run.json` invalidates consumption
of partially overwritten outputs; a prior complete manifest alone does not establish the latest run
succeeded. Run to completion again before use.

## Safe extraction and ingestion

ZIPs are fully validated before extraction into ignored private staging. Absolute/drive paths,
traversal, symlinks, duplicate member names and configured member/expanded-byte limits are rejected.
Archive contents are never executed. Identical SHA-256 CSV content is processed once, even when
both an archive and its extracted copy exist. Excluded aliases are explicit in the manifest and do
not add rows to the processed population. Archive and documentation entries are inventory items,
not additional CSV populations. ZIP-extracted files are rechecked as well as original ZIP hashes.

CSV is read with Python's strict streaming reader into bounded CSV spools, one source file at a time, then into Parquet. IDs remain
strings, including leading zeros. The default is UTF-8 with BOM handling, with BOM-based UTF-16/32
and a recorded Windows-1252 fallback. Delimiters are detected from comma, semicolon, tab or pipe.
A recoverable wrong-width record is quarantined, without exporting its raw line. Unrecoverable
quoting or encoding errors fail the run rather than guessing row boundaries or silently dropping data.
Source row is the 1-based CSV record number, including the header as row 1; source line is the
physical starting line, so quoted multiline records remain traceable. An empty physical record is an
input row and is quarantined. Raw timestamp/speed/heading/coordinate strings remain in private
`staging/observations/`. Blank fields become null; other missing-value sentinels are unconfirmed.

DuckDB performs typed parsing, grouping, sorting and diagnostics in 32 deterministic device
buckets, with a 1 GB cap and private spill directory. Every device keeps its complete archive history
in one bucket. Poll-ID reuse and shared-coordinate clusters are audited globally before partitioning.
Narrow ranking tables avoid carrying raw fields through duplicate sorts. Bucket validation precedes
a deterministic merge; the merged observation IDs receive an additional national uniqueness check,
run in 16 observation-ID prefix slices so it fits in memory. Sorted bucket names follow device_key
order, so device-ordered outputs are concatenated in bucket order without a global spilling sort;
lineage and quarantine are sorted one source file at a time. After a successful merge,
`staging/partition_state.json` stores the bucket statistics and merged-file hashes, so a failure in
later steps can rerun with `--reuse-staging` without repeating the bucket stage (only when inputs,
configuration and bucket-stage code are unchanged and every merged file is byte-identical). PyProj.Geod uses the WGS84 ellipsoid, returning metres. There are no AI calls,
paid APIs, external services or inference dependencies. Code/config/input hashes, dependency
versions and run times are recorded in the run manifest. Staging cache reuse checks both source
and staged-file hashes, sample size and a secret-dependent verifier; it does not skip cleaning.

## Time, coordinates and ancillary values

Only explicitly configured timestamp formats are parsed (initially ISO calendar dates with space or
T separator and optional fractional seconds). Slash dates ambiguous between day/month order are
flagged and unusable. Offset/Z timestamps require source-specific handling; this version flags and
quarantines such inconsistencies rather than silently dropping offsets. `device_date` orders movement.
`timestamp` describes database ingestion. Unknown source timezone leaves naive timestamps and
`timezone_status=unconfirmed_naive`. Even a known timezone does not cause implicit UTC conversion:
configured timestamps remain local naive values, labelled with the source timezone.

Reporting lag is null until both `source_timezone` and `timestamp_semantics_compatible=true` are
explicitly established. With compatible semantics, database minus collection time gives seconds;
negative and greater-than-one-hour lag are flags, not corrections. No assumption from timetable
source material is imported into GPS clocks.

Missing, non-finite or globally invalid coordinates, absent device/time and the explicit 0/0
placeholder are quarantined. The broad Maldives review box is latitude [-2, 9], longitude [70, 76].
Points outside it remain retained and flagged when otherwise valid. Coordinates are never swapped.
Exact shared-coordinate clusters with at least 1,000 input observations across 5 devices receive a
review flag; real busy moorings can meet this rule. This is not automatic evidence of a bad fix.

Speed is retained as `speed_raw`; numeric missing/invalid/negative values are flagged. `speed_kmh`
is null while units are unconfirmed. The only supported explicit conversions are km/h, knots ×1.852
and m/s ×3.6. Numerical maxima never determine units. Heading numbers and raw strings are retained;
no bearing convention is declared unless configured. Zero numeric speed and coordinate displacements
of at most 10 m flag heading as unreliable at standstill; heading never establishes movement.
Network codes remain categorical strings (1 Dhiraagu, 2 Ooredoo); unexpected values are flags.
Cell ID remains a string, and GSM level remains raw, without a dBm interpretation. Missing network,
cell or signal data does not invalidate a usable location.

## Observation decisions and duplicates

The observation key is SHA-256 of canonical JSON containing device_key, parsed collection time and
numeric latitude/longitude. It excludes database poll ID and ancillary fields. The secret itself is
never part of this content. All identifiers depend on the pseudonymisation key, so equivalence is
specified for the same inputs, code, config and secret. No guarantee of identity stability is made
across changed keys or changed canonicalisation versions.

Every processed source row ends in exactly one outcome:

1. Quarantined: unusable required fields/coordinates or unresolved simultaneous locations.
2. Retained: one deterministic representative of a usable device/time/location group.
3. Duplicate: another row in that retained group.

All source rows in a simultaneous-location conflict are quarantined, including copies within that
conflict. They are not also counted as absorbed duplicates. Repeated poll IDs are audited globally,
with counts of differing raw observation content. Poll ID is never a location deduplication key.
Repeated conflicting poll IDs retain differing usable observations and receive a conflict flag.

Within a device/time/location group, choose most valid/populated ancillary fields, then earliest
parseable database timestamp, lexicographically smallest full-record hash, source path and source
row. Ancillary completeness counts parsed database time, nonnegative finite speed, finite heading,
recognised network and populated cell/signal. Multiple ancillary hashes flag conflicts and all rows
remain in lineage. Duplicate reasons prioritise an exact raw-record copy, repeated identical poll-ID
content, ancillary disagreement, then another poll ID at the same device/time/location. Grouping is
exact, not fuzzy spatial or temporal matching. Raw record hashes include poll ID; content hashes
exclude it. Formatting differences may be raw-content conflicts even when canonical values agree.

## Movement, sampling and trajectory segmentation

Adjacent canonical observations are sorted by device and collection time. Every pair records elapsed
seconds, ellipsoidal displacement and coordinate-derived speed, including rejected pairs. Nonpositive
time, invalid displacement, speed above 200 km/h, unresolved quarantine barriers, gaps greater than
15 minutes and collection-date changes prevent continuity. Suspicious transitions do not remove both
endpoints. A middle fix B is marked unusable for trajectory reconstruction when A→B and B→C both
exceed the review speed, each interval is at most 15 minutes, A and C are within 500 m, and no date
or quarantine barrier is crossed. B remains a retained canonical observation with an isolated-jump
flag. There is no new A→C transition or interpolation across B.

The 200 km/h rule is a conservative review threshold with unknown device categories, not a validated
vessel limit. The initial 15-minute continuity rule is provisional. Inspect measured per-device/day
median/p90/max gaps and `gap_threshold_sensitivity.csv` before changing it. This table compares 5,
15 and 30 minutes on the same nationally cleaned adjacent pairs; other split rules still apply.
Small gaps remain intact if no other discontinuity rule is triggered. Cross-midnight adjacent pairs
are retained diagnostically but always split trajectories, even across a short gap.

Quarantined rows with known device and parsed time create barriers between clean neighbours.
Rows without an orderable device/time cannot be positioned: they are explicitly reported as an
uncertainty rather than assigned artificial times. A single usable observation creates a singleton
segment marked insufficient for movement; this preserves traceability without implying a journey.
Trajectory ID is a hash of device_key and its first retained observation ID. Membership is explicit
in `gps_clean.trajectory_id`; unusable isolated fixes have null membership.

Device-day statistics include quarantined and duplicate counts when their device and date can be
parsed. Unidentified/undated quarantines are counted separately. Observation counts, flagged counts
and usable counts overlap by design and are not a terminal partition. Sparse means fewer than 10
canonical observations, an observed span below 600 seconds, or median within-day sampling gap above
900 seconds (or absent). Observed span and accepted interval sums are not monitored duration. No
fleet fraction is calculated without a fleet denominator. Missing GPS does not imply cancellation.

## Scope, downstream use and validation

National cleaning precedes geographic selection. The end-to-end command runs the required regional stage after validated national outputs exist.
Reviewed OCHA/OneMap geometry covers all five areas and 379 island features, including unnamed
and uninhabited islands. Complete components are selected when a member point or accepted
endpoint-to-endpoint line intersects the mask. Outside observations are retained; entry and exit
flags are recorded. National `region_status=geography_in_regional_outputs` directs consumers to
the regional outputs rather than claiming the national table was spatially filtered. See the exact
rule, reference hashes and uncertainty in followme_geography.md and regional_run_manifest.json.

For harbour/crossing work, use usable observations and accepted transitions within trajectory IDs;
review sparse days and flags first. Do not connect across rejected pairs or date boundaries. GPS
sampling and unknown device assignments constrain any recurring-pattern analysis; multiple collection
days permit investigation but do not prove a recurring passenger service. A device is not guaranteed
to remain attached to one physical vessel. No official timetable is inferred.

Validation checks mutually exclusive row reconciliation, observation uniqueness, movement and
membership foreign keys, positive accepted elapsed time, date splits, source hashes, key formats,
identifier column absence and actual original-ID string value absence in private Parquet exports.
Raw source identifiers are re-read only into memory for that value comparison and then discarded.
No raw records are emitted in exception logs. Tests use only synthetic data. `--verify-repeat`
compares SHA-256 fingerprints of deterministic logical Parquet data and diagnostic CSVs, independently
of run times, and confirms unchanged code/config/input/secret-dependent signatures. Never use outputs
from a failed validation. No private outputs are automatically disclosure-cleared for publication.


## Separate stages and public reference preparation

The end-to-end entrypoint always runs both required stages. For diagnostics or resuming only the
regional stage after completed national cleaning:

```bash
.venv/bin/python scripts/clean_followme.py --reuse-staging
.venv/bin/python scripts/build_followme_region.py
.venv/bin/python scripts/build_followme_region.py --verify-repeat

# Rebuild the reviewed geometry from bundled, archived public source snapshots, offline.
.venv/bin/python scripts/prepare_followme_geography.py
```

The national-only command deliberately does not complete the regional workflow; prefer the
end-to-end entrypoint for deliverables. Regional settings and reviewed geometry are separately
hashed. National fingerprints exclude regional outputs, so each stage's repeat check verifies its
own deterministic datasets. The regional manifest binds the national fingerprint set and input
signature, plus the geometry/config/code hashes. Missing or changed reviewed geometry is a hard
failure. Never consume regional results while regional_failed_run.json exists.
