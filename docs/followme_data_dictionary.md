# FollowMe private output data dictionary

All paths below are under `data/private/followme/` (or an explicitly selected private output).
Null is missing/unavailable; it is never replaced with zero. Times are naive Parquet timestamps in
source clock semantics. `collection_date` means the source-clock date, not a confirmed UTC date.
Hashes/IDs are strings. Flags are Parquet lists of explicit strings, not bit fields.

| File | Row meaning and fields |
| --- | --- |
| `input_manifest.csv` | One source inventory entry. `source_path` relative to selected input root; ZIP members use `archive::member`. SHA-256, byte size, kind, processed/excluded status, encoding, delimiter, JSON columns/schema differences, processed row count, parsed min/max collection/database time, timestamp and row parse-failure counts. Identical file aliases name `duplicate_of` and contribute no processed rows. Non-CSV containers/metadata have inapplicable fields blank. |
| `gps_clean.parquet` | One retained canonical device/time/location observation. See fields below. |
| `observation_lineage.parquet` | One row per processed input row: nullable canonical `observation_id`, `duplicate_group_id` (only for groups with more than one source row, including quarantines), `source_file`, `source_row`, `source_line`, original string `poll_id` (database row ID, never IMEI), terminal outcome/reason, group size and ancillary conflict. Quarantined rows have null canonical FK. |
| `gps_quarantine.parquet` | One excluded source row: candidate ID, pseudonymous device key if available, parsed times/coordinates where possible, source references, exclusion reason and quality flags. No original device identifier or raw record text. |
| `movement_segments.parquet` | One adjacent canonical observation pair per device, including rejected pairs. Endpoint IDs, device key, from/to times, elapsed seconds, WGS84 distance metres, derived km/h, flags, `transition_accepted`. Review flags can coexist with acceptance (e.g. standstill heading). |
| `trajectory_segments.parquet` | One continuity component, including insufficient singletons. Trajectory/device IDs, start/end, observation count, observed span, sum of accepted intervals, distance, median/max internal sampling gap, entry split reasons, insufficient-for-movement status and a pointer to geography in regional outputs. Duration sums do not establish monitoring. |
| `device_day_coverage.csv` | One device and parsed date, including dates with quarantines only. Input rows, canonical/usable/flagged/actionably flagged/quarantined/duplicate counts; first/last, observed span; within-day median/p90/max gaps; long gaps; all/reconstructable segment counts; median/p90/max reporting lag; sparse rule result and coverage caveat. Metadata-unconfirmed flags count as flagged but not actionable flags. |
| `data_quality_summary.csv` | Metric, count, named denominator, denominator count, rate and overlap caveat. Terminal outcomes reconcile; issue counts overlap. Movement metrics divide by adjacent pair count; sparse days divide by device-day count; other row metrics divide by processed input rows. |
| `cleaning_issues.csv` | Unresolved metadata, schema and processing issues without device identifiers. Severity, count (where applicable), interpretation. |
| `deduplication_audit.csv` | Source-row terminal outcome/reason counts. Source copies in quarantined groups stay quarantined, not duplicate outcomes. |
| `poll_id_audit.parquet` | Repeated string poll ID, source-row count and distinct raw-content count. Private database row identifiers; not a device-key mapping. |
| `observation_flag_counts.csv` | Retained observation counts per flag; flags overlap. |
| `gap_threshold_sensitivity.csv` | 300/900/1800-second thresholds, adjacent pair population, all/same-day long gaps, selected default. Other discontinuity rules are unchanged. |
| `run_manifest.json` | Completion status, UTC execution timestamps (not GPS time conversion), code/config/input signatures and hashes, full settings, dependency versions, sample/full scope, aggregate results, logical data fingerprints, validation and privacy/region status. |
| `validation_results.json` | Named boolean checks; every check must pass. |
| `cleaning_report.md` | Private run-specific coverage, exclusions, thresholds, uncertainty and downstream advice. |
| `staging/observations/*.parquet` | Private pseudonymised source rows: raw timestamps/coordinates/speed/heading, source references, string poll ID, categorical network/cell/signal and structural parse status. Original IMEI removed before writing. |
| `staging/cache.json` | Stage-cache integrity/signature and source row-count metadata; contains no original-ID-to-key mapping. |
| `staging/documentation/` | Private copies of source documentation identified by content hash. |
| `failed_run.json` | Present only after a failed run; outputs must be treated as incomplete until a subsequent successful run removes it. |

## Canonical observations

| Field | Type/meaning |
| --- | --- |
| `observation_id` | SHA-256 canonical device/time/numeric-location group ID; unique. |
| `device_key` | HMAC-SHA256 of the exact original identifier string using the private secret; potentially sensitive. |
| `collection_time` | Parsed device_date, naive, used for movement ordering. |
| `database_time` | Parsed timestamp, naive, nullable; ingestion clock. |
| `timezone_status` | `unconfirmed_naive` or explicit `source_local_naive:<timezone>`. |
| `latitude`, `longitude` | Double degrees, finite and in global ranges. Review extent does not filter them. |
| `speed_raw` | Original string; blank becomes null. |
| `speed_kmh` | Double, null without confirmed/configured conversion or valid nonnegative numeric speed. |
| `speed_unit_status` | `unconfirmed` or `configured:<unit>`. |
| `heading_raw`, `heading` | Original string and finite parsed number. Range only assessed under confirmed configured convention. |
| `network` | String; documented 1=Dhiraagu, 2=Ooredoo. |
| `cellid` | Categorical string preserving leading zeros. |
| `gsm_level` | Raw string with unconfirmed signal units. |
| `reporting_lag_seconds` | Database minus collection seconds only with compatible configured clock semantics; otherwise null. |
| `quality_flags` | List of observation-level issues and uncertainty, including raw-field, duplicate, cluster and isolated-jump flags. |
| `usable_for_trajectory` | False for retained isolated jumps; unresolved simultaneous locations are quarantined rather than present here. |
| `trajectory_id` | Foreign key to complete national continuity segment; null for unusable observations. |

Invalid observation reasons are separate from suspicious observation flags and movement flags.
`invalid_observation` quarantines are explained by their specific quality flags. A suspicious segment
does not by itself invalidate both endpoint observations. See [method](followme_cleaning.md) for
exact representative priority, barrier rules, sensitivity and uncertainty.


## Required regional deliverables

| File | Fields and meaning |
| --- | --- |
| `gps_region_clean.parquet` | Every member observation of each selected national trajectory, including outside-boundary members. National fields plus `inside_study_area`, `point_study_area_codes`, `segment_study_area_codes`, segment-level `enters_region` and `leaves_region`. No partial segments. |
| `regional_trajectory_segments.parquet` | Selected national segment summaries plus inside/outside observation counts, intersecting accepted-pair counts, both-outside endpoint intersection count, study-area bit mask/codes, entry/exit flags and complete geometry status. |
| `regional_coverage_summary.csv` | Unique union total, per-collection-date totals and per-area complete-component totals. Observation/device/trajectory counts, inside/outside observations and date span. Area rows overlap and must not be added together. |
| `gps_geography.parquet` | Only national observations covered by the mask: observation/trajectory ID, study-area bit mask and codes. Observations absent from this tag table are outside the mask. A null trajectory ID remains possible for an inside but unusable isolated jump. |
| `regional_movement_intersections.parquet` | Accepted national pairs intersecting the mask: endpoint IDs, trajectory ID, study-area mask/codes, entry/exit and both-endpoints-outside flags. No rejected pair is connected. |
| `regional_run_manifest.json` | Regional config/code/reference hashes, bound national input signature/fingerprints, coverage counts, validation and deterministic regional data fingerprints. |
| `regional_cleaning_issues.csv` | Explicit unresolved historical shoreline, source boundary and Greater Malé functional-boundary uncertainty. No device identifiers. |
| `regional_cleaning_report.md` | Geographic sources, all-island coverage, exact whole-component selection, entry/exit rules, totals and uncertainty. |

Bits follow the ordered `study_area_code` features in the hashed GeoJSON; consume the explicit code
arrays rather than hardcoding integer values. Boundary points are covered. Line intersection is a
selection geometry between accepted endpoint fixes, not an artificial GPS observation or a confirmed
sailed path. Whole regional components preserve outside points and retain their national IDs.
