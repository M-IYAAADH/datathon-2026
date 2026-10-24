# data/inbox/

Drop new files from teammates here. Nothing in this folder is trusted or used by the app.

1. Put the file here with a note on who made it, how, and when (a `<file>.provenance.txt` next to it is enough).
2. Add a row to `data/catalog.csv` (status `in_progress`, verification_status `UNVERIFIED`).
3. Run `.venv/bin/python scripts/audit_data.py`, then move the file to `data/reference/`, `data/processed/` or `data/derived/`.

Files here are gitignored except this README. Private data (device keys, per-device tracks) must never be dropped here: use `data/private/`.
