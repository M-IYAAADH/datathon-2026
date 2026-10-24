#!/usr/bin/env python3
"""Create or refresh data/catalog.csv.

  .venv/bin/python scripts/build_data_catalog.py --init       # create from scripts/data_audit/specs.py (refuses to overwrite)
  .venv/bin/python scripts/build_data_catalog.py --refresh    # recompute rows, columns, sha256, size_bytes; keep every other column
  .venv/bin/python scripts/build_data_catalog.py --add-new    # append specs whose dataset_id is not yet in the catalogue

Offline. Never modifies a data file. --deep hashes the large private files byte by byte (slow).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from data_audit.catalog import compute, init_catalog, refresh_catalog  # noqa: E402
from data_audit.common import CATALOG_COLUMNS, ROOT, read_catalog, write_catalog  # noqa: E402
from data_audit.specs import DATASETS  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--init", action="store_true")
    g.add_argument("--refresh", action="store_true")
    g.add_argument("--add-new", action="store_true")
    ap.add_argument("--force", action="store_true", help="with --init: overwrite an existing catalogue")
    ap.add_argument("--deep", action="store_true")
    args = ap.parse_args(argv)
    cat = ROOT / "data" / "catalog.csv"
    if args.init:
        if cat.exists() and not args.force:
            print(f"{cat} exists; use --refresh, or --init --force to rebuild from specs (loses manual edits)", file=sys.stderr)
            return 2
        rows = init_catalog(DATASETS, args.deep)
        write_catalog(rows)
        print(f"wrote {len(rows)} datasets to {cat.relative_to(ROOT)}")
    elif args.refresh:
        rows, changes = refresh_catalog(DATASETS, args.deep)
        write_catalog(rows)
        print("\n".join(changes) if changes else "no changes")
    else:
        rows = read_catalog()
        have = {r["dataset_id"] for r in rows}
        new = [s for s in DATASETS if s["dataset_id"] not in have]
        rows += init_catalog(new, args.deep)
        write_catalog(rows)
        print(f"added {len(new)}: {', '.join(s['dataset_id'] for s in new)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
