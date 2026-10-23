# Ferry timetable extraction report

Status as of 2026-10-23. Target planning date: **24 October 2026**.

## 1. Bottom line

- All 11 PDFs (152 pages, 180 timetable blocks) were extracted from their text layers with character
  coordinates. No OCR was needed or used. Every visible line on every page was mapped to a record or
  recorded as a footer or note; no block is partially extracted.
- **100 trips (433 legs) on 48 routes are routing-ready by default** (`routing_eligible = 1`): 44 routes from
  the CFN 2026 all-atolls file, 2 from CFN Zone 6 (608, C609), and 2 Thaa/Laamu speed routes (S506, S507).
- "Routing-ready" means *latest available published version, internally consistent, explicit weekdays,
  resolved stops*. It does **not** mean the service is confirmed to operate on 24 October 2026. Only five
  documents print an effective date (CTN 2022, the RTL boxes, and three Malé-area sheets dated 2026), and no
  document states an end date. CFN 2026's "1 August 2026" comes from its file name and PDF metadata only.
- Malé-area services (Thilafushi, Gulhifalhu, Vilimalé) are extracted but **not routable**: they publish
  departures or headways only, with no arrival times, durations or (for Vilimalé) direction.
- 137 issues are recorded: 11 errors, 61 warnings, 65 info. 73 remain open for a human or operator answer
  (section 9).

## 2. Source inventory

SHA-256 values are in `sources.csv` (first 12 shown). "Date evidence" separates printed dates from
file-name dates. Every PDF has a working text layer.

| source_id | File | Pages | Title printed | Operator | Printed effective date | Other date evidence | Layout |
|---|---|---|---|---|---|---|---|
| ctn_2022_k_aa_adh_v | CTN-Ferry-Schedule-Effective-15.05.2022-K.And-Aa.-Adh.V.pdf (`2067e1665c76`) | 10 | MTCC FERRY SCHEDULE | MTCC (text) | 2022-05-15, every page | PDF created 2022-05-09 | 4-column legs, routes 301–310 |
| ctn_2024_all_atolls | CTN-Ferry-–-All-Atolls-30.04.pdf (`552ac67daa21`) | 54 | Ferry Schedule | MTCC (logo) | none | file name "30.04" (no year); PDF created 2024-04-28 | 4-column legs, routes 101–607 |
| cfn_2026_all | CFN-All-ferry-Schedule-1-August-2026.pdf (`5e0bd191cff7`) | 54 | Ferry Schedule | MTCC (logo) | none | file name "1 August 2026"; PDF created 2026-07-28 | 4-column legs, routes 101–607 |
| cfn_2026_zone6 | Zone-6-CFN-Ferry-Schedule-2026.pdf (`a6a8e94ce4d6`) | 6 | Speed Ferry Schedule | MTCC (logo) | none | file name "2026"; PDF created 2026-05-11 | 4-column legs with "Trip n" boxes, routes 608–S610 |
| speed_2026_meemu | 28.03.2026-Meemu-Speed.pdf (`5cd2e9e4fde8`) | 2 | Ferry Schedule | MTCC (logo) | none | file name "28.03.2026"; PDF created 2026-03-27 | 4-column legs, S401–S402 |
| speed_2026_thaa_laamu | 28.03.2026-Thaa-Laamu-Speed.pdf (`728d49e0392a`) | 2 | Ferry Schedule | MTCC (logo) | none | as above | 4-column legs, S506–S509 |
| rtl_zone1 | RTL-Ferry-Zone-1.pdf (`e5666ac32722`) | 19 | FERRY SCHEDULES | not named (unnamed logo) | per box: 2022-05-28 … 2022-09-11 | none | departure-first boxes, 12-hour clock, 24 routes |
| mtcc_gulhifalhu_2026_v1 | Gulhifalhu-Ferry-Schedule-2026-V1.pdf (`91b1600de146`) | 1 | Ferry Schedule | MTCC (logo) | 2026-03-21 | PDF created 2026-03-23 | two independent departure lists |
| mtcc_thilafushi_2026_v2 | Thilafushi-Ferry-Schedule-2026-V2.pdf (`9c1f3990ef49`) | 2 | Ferry Schedule | MTCC (logo) | 2026-08-24 (page 2 only) | PDF created 2026-08-27 | two independent departure lists |
| mtcc_cargo_2026_v4 | Cargo-Ferry-Schedule-2026-V4.pdf (`c3aa5981dd00`) | 1 | Ferry Schedule | MTCC (logo) | none | file name "V4"; PDF created 2026-08-11 | three cargo departure lists |
| mtcc_vilimale_2026_v1 | Vili-male-Ferry-Schedule-2026-V1.pdf (`bd31eba24a76`) | 3 | Ferry Schedule | MTCC (logo) | 2026-03-21 (page 3 only) | PDF created 2026-03-23 | headway windows |

Network labels (CTN, CFN, RTL Zone 1, "speed") come from file names only; the documents do not print them.

## 3. Method

1. **Inspection.** Every PDF was inventoried (hash, pages, metadata, text availability). Pages were
   rendered and representative pages of each layout compared visually with the extracted rows (log in
   `data/manual/ferry/reviewed_pages.csv`: 31 pages across all 11 files, including the page behind every
   anomaly in section 7).
2. **Character-level extraction** (`scripts/ferry_pipeline/pdftext.py`). Plain text loses column
   relationships and splits times (`8:` + `20`) and merges cells across columns, so every character is placed
   by its coordinates, grouped into visual lines and gap-separated tokens. White text counts as visible only
   when it lies on a non-white fill: CTN 2024 p44 carries an invisible stale day heading, and the RTL file
   carries invisible "AM"/"A"/"M" fragments beside island names. Both are ignored and reported.
3. **Layout parsers** (`parsers.py`):
   - *4-column* (CTN/CFN/speed): `arrival time | arrival island | departure island | departure time`; each row
     becomes departure island @ departure time → arrival island @ arrival time. The name split is taken from
     the header positions, and times by side of the page. Day headings start new blocks; they are never carried
     into a Dhivehi-only sub-block (CFN/CTN p44) or a new route.
   - *RTL*: `island | departure | island | arrival` with 12-hour times; column bounds from header words.
   - *Departure lists*: each `FROM X` column is an independent list. Adjacent left/right times are **not**
     paired as departure/arrival.
   - *Frequency*: start/end/headway windows, kept as windows.
4. **Build** (`build.py`): stop identity, services, trip segmentation, day offsets, versions, eligibility.
5. **Validation** (`validate.py`): 77 checks over the written CSVs (section 8).

### Interpretation rules applied

| Topic | Rule |
|---|---|
| Trip segmentation | Same trip while each row departs where the previous row arrived. Split at a printed "Trip n" label change, a reversal (A→B then B→A), a discontinuity (flagged), or a stop-over of 120+ minutes. No vessel or through-service is inferred. |
| Midnight | Offset +1 only when a time falls from ≥20:00 to ≤05:00 *within one published sequence*. Applied to the Thilafushi 00:15 departures (after 23:15 in the same column). Any other backwards time is left as printed and raised as an error. |
| Frequency past midnight | Vilimalé windows after 00:00 are anchored to the preceding service day because the table runs 05:30→05:30; flagged as needing confirmation. |
| AM/PM | 12 AM → 00, 12 PM → 12. "18:00 PM" (RTL R1V2) kept as 18:00 and flagged. |
| Weekdays | Parsed per block from the English heading; Dhivehi headings are checked by consonant skeleton and any disagreement leaves the disputed days blank. |
| Stop identity | Atoll from a printed cell prefix or route-title endpoint, else inferred from a single-atoll route title, else cross-route match; otherwise unresolved. Same-named islands in different atolls stay separate. Reviewed spelling aliases only (24 entries, each with a reason); no fuzzy matching. |
| Dhivehi | Extracted Thaana is glyph-reordered (word-final vowel signs move to the front, some glyphs drop). It is kept raw and never reconstructed. Zone 6 and one Gulhifalhu note are images (text layer holds filler glyphs). Dhivehi footnotes were read from the rendered pages by an AI agent; those readings are recorded in `block_annotations.csv` as `needs_human_confirmation`. |
| Cargo | Extracted as `cargo_ferry` with `passenger_eligibility = not_established`; never routable. |

## 4. Version handling

Versions were compared using route code plus ordered stops, days and times. Route-code matches with less
than 50% island overlap would not count as the same service; none occurred. Differences for every compared
pair are in `data/interim/ferry/version_diff_report.md` (74 comparisons).

| Family | Decision |
|---|---|
| CTN 2022 → CTN 2024 → CFN 2026 (routes 101–607) | CFN 2026 blocks are `preferred_candidate`. Ordering evidence is the printed 2022 date, then file-name dates and PDF creation dates (2024-04-28, 2026-07-28). Older blocks are `duplicate` where the published content is identical (42) and `historical` otherwise (33). |
| Changes 2024 → 2026 | Days changed on 102 (Tue → "Thursday", but the 2026 Dhivehi still says Tuesday; see §7), 108 (+Sunday) and 110 (Sun/Mon/Wed → Sat/Sun/Tue/Thu). Times changed on 107, 113, 201, 203, 401, 404, and on 307–310 vs 2022. CFN 2026 adds Hulhumalé legs to routes 303 and 305–309. |
| CFN Zone 6 | 608 and C609 preferred. **S609 (p3 vs p4) and S610 (p5 vs p6)**: the same route code and days appear on two pages with different trip lists (some trips identical) → both pages `unresolved`. |
| Speed vs CFN | CFN 401/S401 (Sat 07:00 Dhiggaru→Maduvvari), CFN 403/S402 (Wed 07:00 Kolhufushi→Naalaafushi) and CFN 503/S508 (Wed 15:00 Gan→Mundoo) publish identical crossings on a shared weekday. Neither replacement nor coexistence is demonstrable → all six blocks `unresolved`. |
| RTL Zone 1 (2022) | All 24 boxes `unresolved`. There is no later RTL edition to compare against. Later MTCC routes serve the same Haa Alif/Haa Dhaalu/Shaviyani islands with different codes and times (36 overlaps listed), so replacement cannot be shown either. Current validity unknown. |
| Malé-area sheets | Single versions → `preferred_candidate` (Thilafushi V2, Gulhifalhu V1, Vilimalé V1, Cargo V4). |

No network was replaced wholesale, old and new versions were not unioned, and `effective_to` is never
asserted.

## 5. Coverage

| Source | Pages | Blocks | extracted | duplicate | unresolved |
|---|---|---|---|---|---|
| cfn_2026_all | 54 | 60 | 50 | – | 10 |
| ctn_2024_all_atolls | 54 | 60 | 19 | 39 | 2 |
| ctn_2022_k_aa_adh_v | 10 | 15 | 12 | 3 | – |
| rtl_zone1 | 19 | 24 | – | – | 24 |
| cfn_2026_zone6 | 6 | 6 | 2 | – | 4 |
| speed_2026_meemu | 2 | 2 | – | – | 2 |
| speed_2026_thaa_laamu | 2 | 4 | 3 | – | 1 |
| mtcc_thilafushi_2026_v2 | 2 | 2 | 2 | – | – |
| mtcc_gulhifalhu_2026_v1 | 1 | 1 | 1 | – | – |
| mtcc_cargo_2026_v4 | 1 | 3 | 2 | – | 1 |
| mtcc_vilimale_2026_v1 | 3 | 3 | 3 | – | – |
| **Total** | **152** | **180** | **94** | **42** | **44** |

No page is `non-schedule` and none is `partially_extracted`. `extracted` refers to extraction completeness:
historical CTN blocks are fully extracted but are not routable. `unresolved` = extracted, but the version or
calendar is unresolved. Per-block detail: `data/interim/ferry/coverage.csv`.

## 6. Output files

| File | Rows |
|---|---|
| sources.csv | 11 |
| stops.csv | 199 |
| stop_aliases.csv | 808 |
| routes.csv | 148 |
| services.csv | 180 |
| trips.csv | 430 |
| legs.csv | 1,366 |
| stop_times.csv | 1,796 |
| frequency_windows.csv | 26 |
| schedule_versions.csv | 180 |
| extraction_issues.csv | 137 |
| routing_eligibility.csv | 430 |
| frequency_eligibility.csv (additional) | 26 |

## 7. Source anomalies found (kept as printed, flagged, excluded where they affect routing)

| Where | Finding |
|---|---|
| CFN 2026 p2, route 102 | English "Thursday", Dhivehi އަންގާރަ (Tuesday); confirmed visually. Tuesday and Thursday left blank. |
| Cargo V4, Gulhifalhi block | English "Sunday, Tuesday & Thursday", Dhivehi ends with ބުދ (Wednesday). Wednesday and Thursday left blank. |
| CFN 2026 p32, route 307 | Row "Male’ 11:45 → Male’ 12:15". The Dhivehi cells read ހުޅުމާލެ (Hulhumalé) beside the r02 arrival and the r03 departure. |
| CTN 2022 p9, route 309 | Last row departs "Male’", but its Dhivehi cell reads މާފުށި (Maafushi), the previous row's arrival. |
| CTN 2024 p7, route 107 | Row "Thuraakunu 16:40 → Thuraakunu 16:55". |
| CTN 2024 p19, route 201 | Arrival column (Villingilli, Kooddoo, Maamendhoo, Dhevvadhoo, Thinadhoo) does not match the departures; it appears copied from a Gaafu route. Rows not joined; these five arrival stops are atoll-unresolved. |
| CTN 2024 p36, route 401 | Return departures 14:30 and 15:30 are earlier than the preceding arrivals (15:25, 16:00). CFN 2026 corrects them. |
| Meemu S402 | Row "Mulah 14:55 → Muli 15:00" sits between 16:50 and 17:05. |
| Thaa/Laamu S509 | Departs Ki’nbidhoo 12:20 after arriving there at 12:25. |
| RTL R1V2 | Arrival printed "18:00 PM". |
| Vilimalé Saturday & holidays | Gap 17:20–17:30 between windows. |
| CFN 2026 routes 406/407 (and CTN 2024) | Alternating weeks Sat/Mon/Wed and Sun/Tue/Thu, with no anchor week. |
| CFN/CTN 501, 502, 505 and CTN 108/110 | Dhivehi footnotes describe fortnightly substitutions with no anchor week. |
| CFN 2026 route 605 | Title prints "GA. THINADHOO" while 601/602/606/607 print "GDH. THINADHOO"; kept as separate stops and flagged. |
| CTN 2024 p36 | Route title prefix "TM." (others print "M."). |

## 8. Validation and tests

`python scripts/extract_ferry_schedules.py` ends with **VALIDATION: pass (77 checks, 0 failed)**. The checks
cover:

- primary-key uniqueness and foreign keys
- controlled vocabularies
- source PDF hashes, so the originals are verified unchanged
- time format, offset and seconds arithmetic
- arrival after departure (or error-flagged and excluded)
- monotonic times within a trip, leg-to-leg stop continuity, and no unsplit reversal
- stop_times derived exactly from legs, with no invented origin arrivals or destination departures
- weekday values; blank flags for unanchored alternation; `effective_to` never asserted
- positive headways, a suspended prayer break without a headway, `exact_times = 0`, and no trips created from frequency windows
- cargo and departure-only exclusions
- eligibility flag consistency; only preferred versions routable
- no duplicate routable departure across blocks
- no coordinates, codes, jetties or fare columns
- traceability to source row/block
- coverage of every page and block

The no-duplicate check found the CFN/speed overlaps described in §4; they are now `unresolved`.

`python -m pytest tests -q`: 33 tests pass. They cover normalisation (AM/PM, ranges, Dhivehi skeletons,
prefixes, midnight steps) and parsers on the real PDFs: orientation, split spans, merged spans, offset
lines, hidden text, RTL fragments, independent departure columns and the prayer break. End-to-end tests
check the validation pass, the 00:15 → offset 1 / 87,300 s rule, departure-only and cargo exclusion,
frequency non-expansion, reversal splits, trip labels, discontinuities, alternating weeks, version
statuses, atoll separation and the exclusion of rows with source errors.

The run is deterministic: two consecutive runs produce byte-identical CSVs.

## 9. Outstanding items for human review

Open issues: 11 errors, 61 warnings and 1 info (the image-only Gulhifalhu Dhivehi note) in
`extraction_issues.csv` (`resolution_status = open`). They group as:

1. **Operator confirmation of current validity** for every source; effective dates for CTN 2024, CFN 2026,
   Zone 6, both speed files and Cargo V4 (8 `date_ambiguity` issues).
2. **Alternation anchors** for 406/407 and the fortnightly 501/502/505 (and CTN 108/110) patterns
   (15 `operating_day_ambiguity`).
3. **Language conflicts**: route 102 days, Gulhifalhi cargo days, and the island names in CFN 307 and CTN 309.
4. **Zone 6 S609/S610** page conflicts; **CFN vs speed** overlaps (401/S401, 403/S402, 503/S508);
   **RTL Zone 1** status.
5. **Stop identity**: 8 atoll-unresolved stops (Gadhdhoo, Vaadhoo and Faresmaathodaa on route 606 between
   GA and GDH; the five copied names on CTN 2024 route 201). The GA/GDH Thinadhoo prefix also needs checking.
6. **Dhivehi readings by the AI agent** (footnotes on CFN p43/p44 and CTN p8/p43/p44, alternation headings)
   are recorded in `data/manual/ferry/block_annotations.csv` and must be confirmed by a Dhivehi reader.
7. **Vilimalé** direction/terminals, per-direction headways, post-midnight anchoring and the 17:20–17:30 gap.

## 10. Fields that need another source

- Arrival times and crossing durations for Thilafushi, Gulhifalhu and cargo departures.
- Terminals, direction and per-direction headway for Vilimalé.
- Jetty/terminal names for every stop, including which Malé terminal each route uses.
- Official island and atoll codes, coordinates (authoritative island register).
- Effective/withdrawal dates; public-holiday dates for the Vilimalé holiday schedule.
- Fares, vessel IDs, capacities, boarding cut-offs, and real-time or operated-trip status: none are published
  in these PDFs and none were added.
- Confirmation that "speed" services carry passengers on the same basis as regular ferries (that
  classification comes from the file name).

## 11. Reproducibility and audit trail

- Offline: PyMuPDF only; no OCR, network or model calls. Source PDFs are unchanged (hashes are re-checked by
  the validator).
- Reviewed decisions live in `data/manual/ferry/`: `stop_name_aliases.csv`, `stop_identity_overrides.csv`,
  `block_annotations.csv`, `source_metadata.csv` and `reviewed_pages.csv`. Each row gives the source
  reference, the reason and a review label. All reviews were done by an AI agent; none is labelled as
  human-verified.
- Evidence lives in `data/interim/ferry/` (see the data dictionary).
