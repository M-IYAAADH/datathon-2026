# Ferry timetable data dictionary

All files are UTF-8 CSV with a header row, written to `data/processed/ferry/` by
`scripts/extract_ferry_schedules.py`. Quoting is minimal RFC 4180 (fields containing commas, quotes or
newlines are quoted).

## Conventions

| Topic | Rule |
|---|---|
| Blank | **Unknown or not published.** Never zero, never "none". |
| Dates | ISO `YYYY-MM-DD`. `effective_to` is always blank: no document states an end date, and a newer file is not proof of one. |
| Clock times | `HH:MM:SS` as printed (RTL 12-hour values converted to 24-hour). `*_raw` columns keep the printed text. |
| Service day | `*_day_offset` is 0 for the service day of the heading, 1 for the following calendar day. `*_seconds` = offset × 86400 + clock seconds. Example: 00:15 after 23:15 in one published sequence → offset 1, 87300 s. |
| Timezone | Asia/Male, declared as a project assumption (the PDFs state none). |
| IDs | Deterministic, derived from source, page, block and row. They are internal identifiers, **not official codes**. |
| Traceability | `source_id` + `source_page` + block (`…:pPP:bN`) + row (`…:rNN`) identify the printed cell. Coordinates of every row are in `data/interim/ferry/parsed_rows.csv`. |
| Review labels | `auto_extracted` (rules only), `auto_extracted_ai_page_review` (page compared with its rendered image by an AI agent), `needs_human_review` (an error/warning is attached). Nothing is labelled human-verified. |

### ID patterns

| ID | Pattern | Example |
|---|---|---|
| block | `{source_id}:p{page:02}:b{n}` | `cfn_2026_all:p28:b2` |
| source row | `{block}:r{n:02}` (departure lists add column letter) | `mtcc_thilafushi_2026_v2:p01:b1:r14b` |
| stop | `stp_{atoll}_{namekey}`, `stp_noatoll_{namekey}`, `stp_unres_{candidate atolls}_{namekey}` | `stp_ha_uligamu` |
| route | `{namespace}:{published code}` or `mtcc_male:{title slug}` | `cfn:101`, `rtl_zone1:S1C1` |
| service | `svc_{block}` | `svc_cfn_2026_all:p01:b1` |
| trip | `trp_{block}:t{n:02}` | `trp_cfn_2026_all:p01:b1:t02` |
| leg | `leg_{source row}` | `leg_cfn_2026_all:p01:b1:r06` |
| frequency | `frq_{source row}` | `frq_mtcc_vilimale_2026_v1:p02:b1:r03` |
| version | `ver_{block}` | `ver_ctn_2024_all_atolls:p01:b1` |

Route namespaces: `ctn` (CTN 2022 and CTN 2024 files), `cfn` (CFN 2026 and Zone-6 CFN files), `speed`
(Meemu and Thaa/Laamu speed files), `rtl_zone1`, `mtcc_male`. Namespaces come from file names; the
documents do not print network names.

## sources.csv

One row per PDF.

| Column | Meaning |
|---|---|
| source_id | Stable ID (see `scripts/ferry_pipeline/config.py`). |
| filename, sha256, page_count | File identity; the validator re-hashes the PDFs. |
| document_title | Most common page title printed on the document. |
| operator_name | Only where established (text or visible logo); evidence in `notes`. Blank for RTL. |
| network_name | Label taken from the file name (evidence in `notes`). |
| effective_from | A date **printed** in the document and applicable to the whole document; blank otherwise. |
| effective_to | Always blank. |
| date_evidence | Printed dates, file-name date text (marked unverified) and PDF creation date. |
| source_url | Blank (not available). |
| extraction_method | Parser used. No OCR was used for any value. |
| notes | Operator/network/version-order evidence and caveats. |

## stops.csv

Island-level stops. No ferry terminal or jetty is named in any source.

| Column | Meaning |
|---|---|
| island_name_en | Most common printed spelling (reviewed aliases excluded). |
| island_name_dv | Blank. The PDFs' Thaana text is glyph-reordered on extraction; it is not reconstructed. Raw strings: `data/interim/ferry/dhivehi_raw_strings.csv`. |
| atoll_abbreviation | Atoll prefix as printed in the sources (HA, HDH, SH, …). Blank for Greater Malé names and unresolved stops. |
| official_island_code, official_atoll_code, jetty_name, latitude, longitude | Blank: no authoritative lookup was supplied. |
| identity_status | See below. |
| notes | How the atoll was established; terminal caveat. |

`identity_status`:

| Value | Meaning |
|---|---|
| atoll_explicit | At least one source prints the prefix with this name (cell such as `Ha. Uligamu`, or a route-title endpoint). |
| atoll_inferred_route_title | Never printed with a prefix, but appears (as both departure and arrival) on a route whose two title endpoints share one atoll. |
| atoll_inferred_cross_route | Only candidate atoll (from the route title) in which the name is otherwise established. |
| no_atoll_prefix_reviewed | Greater Malé names (Malé, Hulhumalé, Vilimalé, Thilafushi, Gulhifalhu) that sources never prefix; reviewed in `data/manual/ferry/stop_identity_overrides.csv`. |
| atoll_unresolved | Atoll not established; trips touching it are excluded from routing. |

Identically named islands in different atolls are separate stops (e.g. `stp_r_maduvvari`, `stp_m_maduvvari`).

## stop_aliases.csv

One row per printed spelling × stop × source page.

| Column | Meaning |
|---|---|
| raw_name | Cell text exactly as extracted (prefix included). |
| raw_atoll | Prefix printed in that cell, else blank. |
| match_method | `name_key_exact` (case/apostrophe-insensitive) or `reviewed_alias(variant->canonical)`, plus the atoll method. No fuzzy matching. |
| review_status | `auto_rule`, `alias_ai_agent_reviewed` (from `stop_name_aliases.csv`), `unresolved_needs_human_review`. |

## routes.csv

| Column | Meaning |
|---|---|
| published_route_code | Code as printed (blank for Malé-area sheets, which print none). |
| published_route_name | Title from the latest source; title variants listed in `notes`. |
| transport_type | `ferry`, `speed_ferry` (page title "Speed Ferry Schedule", or file name only — see notes), `cargo_ferry`. |
| passenger_eligibility | `presumed_public_ferry` (public ferry timetable; passenger carriage not stated) or `not_established` (cargo). |

## services.csv

One row per timetable block (a day heading with its rows).

| Column | Meaning |
|---|---|
| monday … sunday | `1` operates, `0` not listed in an explicit heading, blank unknown/disputed. |
| holiday_rule | Text when the heading names holidays; holiday dates are never invented. |
| effective_from | Date printed on that page/box; blank when not printed there. |
| days_raw_en, days_raw_dv | Headings as extracted (Dhivehi unreconstructed). |
| calendar_status | See below. Only `explicit_days` counts as resolved for routing. |

`calendar_status`: `explicit_days`; `explicit_days_plus_unlisted_holidays` (Vilimalé Saturday & holidays);
`language_conflict` (English and Dhivehi disagree — disputed days blank); `alternating_weeks_unanchored`
(different days in alternate weeks, no anchor week — all days blank); `fortnightly_unanchored`;
`explicit_days_with_unanchored_exception` (days printed, but a footnote changes some days in alternate
weeks); `unknown_days`.

## trips.csv

| Column | Meaning |
|---|---|
| source_trip_label | Label printed in the source (e.g. `Trip 1`); blank if none. Zone-6 "Trip n" boxes hold an out-and-back pair, so one label maps to two trips. |
| direction_label | `title_order (a -> b)` / `reverse_title_order (b -> a)` relative to the printed route title, `not_determined`, or `from X` for departure lists. |
| schedule_type | `scheduled` (timed legs) or `departure_only` (independent departure list; no arrival). |
| notes | Why the trip was split from the preceding rows. |

Trip segmentation: consecutive rows are one trip while the next row departs from the previous arrival
island. A new trip starts at a printed trip label change, a direction reversal (A→B then B→A), a
discontinuity (flagged), or a stop-over of 120+ minutes. No through-service or vessel assignment is
inferred across a split.

## legs.csv

The primary record: one published crossing row.

| Column | Meaning |
|---|---|
| departure_time_raw, arrival_time_raw | Printed text (e.g. `6:00`, `05:45AM`). |
| departure_time … arrival_seconds | Normalised values (see conventions). Blank when not published. |
| duration_minutes | Arrival − departure when both are known and arrival is later; else blank. |
| notes | Extraction notes (e.g. invisible text fragment removed, time printed off its row). |

Impossible values are kept as printed and recorded in `extraction_issues`, never repaired.

## stop_times.csv

Derived from legs. Stop *k*'s arrival comes from the incoming leg and its departure from the outgoing leg.
The origin's arrival and the destination's departure are blank (not published). Repeated visits get
separate `stop_sequence` values. `time_status`: `published_arrival_and_departure`,
`published_departure_only`, `published_arrival_only`, `unknown`. `source_row_ids` lists the rows used.

## frequency_windows.csv

Headway windows (Vilimalé only). Not expanded into departures.

| Column | Meaning |
|---|---|
| direction_scope | `unspecified` — the table names no direction or terminals. |
| from_stop_id, to_stop_id | Blank (terminals not printed). |
| start_/end_day_offset | Windows after midnight are anchored to the previous service day by table order (05:30 → 05:30); flagged for review. |
| headway_seconds | From "15 MIN", "1 HR". Blank for a suspended window. |
| operation_status | `operating` or `suspended` (Friday prayer break). |
| exact_times | Always 0: no exact departures are published. |

## schedule_versions.csv

One row per block.

| Column | Meaning |
|---|---|
| schedule_fingerprint | Hash of weekdays + ordered (from, departure, to, arrival) rows. Equal fingerprints = identical published content. |
| selection_status | `preferred_candidate` (latest available version of that route; **not** a confirmation of current service), `historical` (superseded by a later version of the same route code with ≥50% island overlap), `duplicate` (identical to the later version), `unresolved` (conflict or no basis to choose). |
| comparison_source_id | Source(s) compared against. |
| decision_reason | Rule and evidence applied. |
| review_status | `rule_based_needs_human_confirmation`. |

## extraction_issues.csv

| Column | Meaning |
|---|---|
| severity | `error` (record excluded from routing), `warning` (needs review; may exclude), `info` (documented interpretation). |
| issue_type | `time_inconsistency`, `source_row_error`, `language_conflict`, `operating_day_ambiguity`, `version_conflict`, `stop_identity`, `trip_segmentation`, `midnight_interpretation`, `am_pm_ambiguity`, `date_ambiguity`, `direction_unspecified`, `frequency_gap`, `departure_only_timing`, `dhivehi_unreadable`, `language_check_incomplete`, `extraction_artifact`, `unparsed_content`, `operator_unconfirmed`. |
| record_type, record_id | Affected record (`leg`, `trip`, `service`, `stop`, `route`, `frequency_window`, `page`, `source`). |
| raw_value | Printed value(s) involved. |
| resolution_status | `open` (needs a human/operator answer) or `documented_no_action` (interpretation recorded). |

## routing_eligibility.csv

One row per trip. `routing_eligible = 1` only when all five flags are 1 and no error is attached to the
trip's legs.

| Flag | 1 when |
|---|---|
| passenger_eligible | Route is not cargo. |
| timing_complete | Scheduled trip with departure and arrival for every leg, arrival after departure, monotonic times. |
| calendar_resolved | Service `calendar_status = explicit_days`. |
| stop_identity_resolved | No stop on the trip has `atoll_unresolved`. |
| version_resolved | Block is `preferred_candidate`. |

`exclusion_reasons` lists the failing conditions (e.g. `version_historical;calendar_language_conflict`).

## frequency_eligibility.csv (additional)

One row per frequency window: `direction_resolved`, `stops_resolved`, `calendar_resolved`,
`version_resolved`, `headway_model_eligible` (currently 0 for all: direction and terminals are not stated).

## Interim evidence (`data/interim/ferry/`)

| File | Content |
|---|---|
| raw_text/*.txt | Plain PyMuPDF text per page (audit only; parsing uses coordinates). |
| parsed_blocks.csv, parsed_rows.csv | Every block and row with y-position, cells, raw Dhivehi and parser notes. |
| unmapped_text.csv | Hidden text, footers, page notes and any visible line not mapped to a record. |
| dhivehi_raw_strings.csv | Raw extracted Thaana (row cells, headings, notes) for human review. |
| coverage.csv | Status of every page/block: extracted, duplicate, non-schedule, partially_extracted, unresolved. |
| version_diffs.csv, version_diff_report.md | Differences for every compared pair of versions/overlaps. |
| service_notes.csv | Calendar notes per service. |
| run_summary.json, validation_report.json | Counts and the 77 validation checks. |
| renders/*.png | Page images (regenerate with `--render`). |
