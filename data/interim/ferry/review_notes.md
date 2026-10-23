# Review notes (ferry extraction)

Reviewer: AI agent (Claude), 2026-10-23. **No item here has been verified by a human.**

Structured records of every decision live in `data/manual/ferry/`:

| File | What it records |
|---|---|
| `reviewed_pages.csv` | Pages whose rendered image was compared with the extracted rows, with findings. |
| `block_annotations.csv` | AI readings of Dhivehi headings and footnotes, calendar-status decisions, language conflicts. Status `needs_human_confirmation` unless only an extraction artefact was involved. |
| `stop_name_aliases.csv` | 24 spelling variants mapped to a canonical name, each justified by the same page or another version. |
| `stop_identity_overrides.csv` | Greater Malé names kept without an atoll prefix. |
| `source_metadata.csv` | Operator evidence (text vs logo) and source notes. |

## How the review was done

1. Every page was rendered (`renders/`, regenerate with `--render`). Representative pages of each layout,
   and every page that raised an issue, were inspected at 85–110 dpi and compared cell by cell with
   `parsed_rows.csv`.
2. Dhivehi was read from the rendered image, not from the text layer, whose Thaana is glyph-reordered.
   Readings are paraphrases by an AI agent. They are used only to *raise* or *explain* issues; they never
   fill a weekday flag, time or island.
3. Automatic Dhivehi/English weekday checks raised three conflicts. Two were confirmed on the page (CFN 2026
   route 102; the Gulhifalhi cargo block). One was a text-layer artefact (CTN 2024 p32) and is recorded as
   such.

## Items a Dhivehi reader should confirm first

- CFN 2026 p41/p42 (and CTN 2024 p41/p42): "in one week … / in the other week …" alternation.
- CFN 2026 p43, p44 and CTN 2024 p43, p44: fortnightly Saturday/Thursday trips to Dh. Kudahuvadhoo via Th. Buruni.
- CTN 2024 p8: alternating Saturday/Sunday assignment between routes 108 and 110.
- CFN 2026 p32 rows r02/r03 and CTN 2022 p9 row r06: Dhivehi island names differ from the English.
- Gulhifalhu sheet: the Dhivehi Friday note is an image (text layer holds filler glyphs).
- Zone 6: all Dhivehi is an image.
