"""Build and refresh data/catalog.csv from specs.py and the files on disk."""
from __future__ import annotations

from .common import (CATALOG_COLUMNS, MISSING_MARK, ROOT, dir_shape, file_shape, fingerprint, read_catalog, resolve,
                     split_paths, write_catalog)


def _date_range(path, col: str) -> str:
    import duckdb

    con = duckdb.connect()
    con.execute("SET TimeZone='UTC'")
    p = str(path).replace("'", "''")
    lo, hi = con.execute(f"select min({col})::varchar, max({col})::varchar from read_parquet('{p}')").fetchone()
    con.close()
    return f"{lo} .. {hi}"


def compute(row: dict, spec: dict | None = None, deep: bool = False) -> dict:
    """Computed columns for one catalogue row: rows, columns, sha256, size_bytes (+ date_range if a date_col is set)."""
    spec = spec or {}
    paths = [resolve(p) for p in split_paths(row["path"])]
    present = [p for p in paths if p.exists()]
    out = {"rows": "", "columns": "", "sha256": MISSING_MARK, "size_bytes": ""}
    if not present or len(present) != len(paths):
        return out
    out["sha256"], out["size_bytes"] = fingerprint(present, ROOT, deep=deep, secret=bool(spec.get("secret")))
    if spec.get("count_rows", row.get("rows", "") != ""):
        first = present[0]
        r, c = (dir_shape(first) if first.is_dir() else file_shape(first))
        out["rows"] = "" if r is None else r
        out["columns"] = "" if c is None else c
    if spec.get("date_col") and present[0].suffix == ".parquet":
        out["date_range"] = _date_range(present[0], spec["date_col"])
    return out


def init_catalog(specs: list[dict], deep: bool = False) -> list[dict]:
    rows = []
    for s in specs:
        row = {c: s.get(c, "") for c in CATALOG_COLUMNS}
        row["verified_by"] = row["verified_on"] = ""
        row.update(compute(row, s, deep))
        if s["expect"] == "missing" and row["sha256"] == MISSING_MARK:
            row["rows"] = row["columns"] = row["size_bytes"] = ""
        rows.append(row)
    return rows


def refresh_catalog(specs: list[dict], deep: bool = False) -> tuple[list[dict], list[str]]:
    """Recompute rows/columns/sha256/size/date_range in place. Human-edited fields are never touched."""
    spec_by_id = {s["dataset_id"]: s for s in specs}
    rows = read_catalog()
    changes = []
    for row in rows:
        before = {k: row.get(k, "") for k in ("rows", "columns", "sha256", "size_bytes", "date_range")}
        spec = dict(spec_by_id.get(row["dataset_id"], {}))
        spec["count_rows"] = row.get("rows", "") != "" or spec.get("count_rows", False)
        new = compute(row, spec, deep)
        for k, v in new.items():
            row[k] = str(v)
        for k, v in before.items():
            if str(v) != str(row.get(k, "")):
                changes.append(f"{row['dataset_id']}.{k}: {v} -> {row.get(k, '')}")
    return rows, changes
