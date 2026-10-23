# Datathon datasets

## Per-island census (`per_island/`)

209 CSV files, one per island (plus one "non-admin islands" row per atoll). Each file holds a single
2022 row (`reference_year`) of aggregate counts: population by sex, age bands (0–17, 18–64, 65+),
disability counts, elderly living alone, plus `island_code`, `atoll_code` and coordinates. The
`residence_basis` is `usual_residence`. File names are `{island_code:05d}_{atoll}_{island}.csv`.

Note: the census `island_code` and coordinates come from this census extract. The ferry tables do not
yet link to them, so ferry `stops.csv` keeps those fields blank.

The source PDFs for the ferry extraction are not committed. Place them in the repository root to rerun it.

## Ferry timetables

Published Maldives ferry timetables (the PDFs in this folder) extracted into CSVs for multi-leg journey
planning, connectivity analysis and disruption simulation. These are **published schedules**, not evidence
that trips operated, and not confirmation that a schedule is current.

- Outputs: `data/processed/ferry/*.csv`
- Column definitions and controlled values: [docs/ferry_data_dictionary.md](docs/ferry_data_dictionary.md)
- Method, coverage, version decisions and open issues: [docs/ferry_extraction_report.md](docs/ferry_extraction_report.md)
- Reviewed decisions (aliases, Dhivehi readings, calendar annotations): `data/manual/ferry/`
- Audit evidence (raw text, coordinate rows, coverage, version diffs, validation report): `data/interim/ferry/`

### Rerun

Python 3.12, offline, no API keys. The source PDFs are read, never modified.

```bash
pip install -r requirements.txt

# extract all PDFs, write CSVs + interim evidence, then validate (exit code 1 if any check fails)
python scripts/extract_ferry_schedules.py

# validate existing CSVs only
python scripts/extract_ferry_schedules.py --validate-only

# also re-render every page to PNG for visual review (data/interim/ferry/renders/)
python scripts/extract_ferry_schedules.py --render

# tests (parsers, normalisation, end-to-end policy checks; ~4 minutes)
python -m pytest tests -q
```

On Windows consoles that are not UTF-8, prefix Python with `-X utf8` (for example
`python -X utf8 scripts/extract_ferry_schedules.py`).

### Alif atolls (AA, ADh) ↔ Malé subset

```bash
python scripts/build_alif_male_subset.py   # reads data/processed/ferry, run after the extraction
```

Writes `data/processed/ferry/subsets/`:

- `alif_male_direct.csv`: every published trip that carries you between an AA/ADh island and Malé or
  Hulhumalé without changing ferry. Includes all versions, with `version_status`, `routing_eligible` and
  `exclusion_reasons`. Filter `routing_eligible = 1` for the current-candidate timetable. `direction` is
  `to_male` or `from_male`.
- `alif_male_one_transfer.csv`: journeys with one change, built only from routing-eligible trips. The
  connections are **derived**, not published: same island, shared weekday, onward ferry not earlier than
  arrival. Minimum transfer time and landing jetty are unknown (see the `notes` column).

### Using the data for routing

Join `trips` to `routing_eligibility` and keep `routing_eligible = 1`. Everything else stays in the
tables for analysis but is excluded by default (historical/duplicate/unresolved versions, departure-only
lists, cargo, unanchored alternating weeks, unresolved stop identities, rows with source errors).
Times are clock times in the declared project timezone **Asia/Male** (an assumption; the PDFs do not
state a timezone), with `*_day_offset` and `*_seconds` measured from the start of the service day.
