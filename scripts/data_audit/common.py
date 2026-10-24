"""Shared helpers: paths, row/column counting, checksums, catalogue I/O."""
from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

CATALOG_COLUMNS = [
    "dataset_id", "name", "path", "description", "owner", "source", "retrieval_date", "licence",
    "rows", "columns", "key_columns", "joins_to", "date_range", "private", "status",
    "verification_status", "verified_by", "verified_on", "known_issues", "used_by",
    "blockers", "next_action", "sha256", "size_bytes",
]
VERIFICATION_STATUSES = ["UNVERIFIED", "AUTO_CHECKED", "LIVE_CHECKED", "NEEDS_ORGANISER", "REJECTED"]
DATASET_STATUSES = ["final", "in_progress", "deprecated"]
# Files above this size are fingerprinted by (relative path, size) unless --deep is given.
LIGHT_HASH_BYTES = 256 * 1024 * 1024
MISSING_MARK = "MISSING"


def resolve(rel: str, root: Path = ROOT) -> Path:
    return (root / rel).resolve()


def split_paths(path_field: str) -> list[str]:
    return [p.strip() for p in path_field.split("|") if p.strip()]


def read_csv(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def csv_header(path: Path) -> list[str]:
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return next(csv.reader(fh), [])


def count_csv_lines(path: Path) -> int:
    """Data rows of a CSV by newline count; for files too big to parse (raw FollowMe)."""
    n = 0
    last = b"\n"
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 24):
            n += chunk.count(b"\n")
            last = chunk[-1:]
    if last != b"\n":
        n += 1
    return max(n - 1, 0)


def _parquet_shape(path: Path) -> tuple[int, int]:
    import duckdb

    con = duckdb.connect()
    p = str(path).replace("'", "''")
    rows = con.execute(f"select count(*) from read_parquet('{p}')").fetchone()[0]
    cols = len(con.execute(f"describe select * from read_parquet('{p}')").fetchall())
    con.close()
    return rows, cols


def file_shape(path: Path) -> tuple[int | None, int | None]:
    """(rows, columns) for a single file; None where the concept does not apply."""
    ext = path.suffix.lower()
    if ext == ".csv":
        if path.stat().st_size > LIGHT_HASH_BYTES:
            hdr = csv_header(path)
            return count_csv_lines(path), len(hdr)
        with open(path, newline="", encoding="utf-8-sig") as fh:
            rd = csv.reader(fh)
            hdr = next(rd, [])
            return sum(1 for _ in rd), len(hdr)
    if ext == ".parquet":
        return _parquet_shape(path)
    if ext in (".geojson", ".json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None, None
        if isinstance(data, dict) and isinstance(data.get("features"), list):
            feats = data["features"]
            props = feats[0].get("properties", {}) if feats else {}
            return len(feats), len(props)
        if isinstance(data, dict) and isinstance(data.get("services"), list):
            return len(data["services"]), None
        if isinstance(data, list):
            return len(data), None
        return 1, len(data) if isinstance(data, dict) else None
    return None, None


def iter_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    return sorted(p for p in path.rglob("*") if p.is_file() and not p.name.startswith(".") and "__pycache__" not in p.parts)


def dir_shape(path: Path, exts=(".csv", ".parquet")) -> tuple[int | None, int | None]:
    """Sum of rows over tabular files in a directory; columns only if every file has the same header."""
    total, headers, seen = 0, set(), False
    for f in iter_files(path):
        if f.suffix.lower() in exts:
            r, c = file_shape(f)
            if r is not None:
                total += r
                seen = True
                headers.add(tuple(csv_header(f)) if f.suffix.lower() == ".csv" else c)
    return (total if seen else None), (len(next(iter(headers))) if len(headers) == 1 and isinstance(next(iter(headers)), tuple) else (next(iter(headers)) if len(headers) == 1 else None))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 22):
            h.update(chunk)
    return h.hexdigest()


def fingerprint(paths: list[Path], base: Path = ROOT, deep: bool = False, secret: bool = False) -> tuple[str, int]:
    """Deterministic checksum for one or more files/directories and the total size in bytes.

    A single small file gives its plain SHA-256. Several files, a directory, or anything larger than
    LIGHT_HASH_BYTES gives a manifest hash (``manifest:`` prefix; ``size-manifest:`` when contents are not read).
    Secrets are never read (``secret-not-hashed``).
    """
    files = [f for p in paths for f in iter_files(p)]
    total = sum(f.stat().st_size for f in files)
    if secret:
        return "secret-not-hashed", total
    if len(files) == 1 and paths[0].is_file() and (deep or total <= LIGHT_HASH_BYTES):
        return sha256_file(files[0]), total
    light = not deep and total > LIGHT_HASH_BYTES
    lines = []
    for f in sorted(files):
        try:
            rel = f.relative_to(base)
        except ValueError:
            rel = f
        lines.append(f"{rel}\t{f.stat().st_size}" + ("" if light else f"\t{sha256_file(f)}"))
    digest = hashlib.sha256("\n".join(lines).encode()).hexdigest()
    return ("size-manifest:" if light else "manifest:") + digest, total


def read_catalog(path: Path | None = None) -> list[dict]:
    path = path or ROOT / "data" / "catalog.csv"
    return read_csv(path) if path.exists() else []


def write_catalog(rows: list[dict], path: Path | None = None) -> None:
    path = path or ROOT / "data" / "catalog.csv"
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=CATALOG_COLUMNS, extrasaction="ignore", lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in CATALOG_COLUMNS})


def norm_name(s: str) -> str:
    """Strict name key: accents stripped, case folded, letters only. No fuzzy matching."""
    import unicodedata

    s = unicodedata.normalize("NFKD", s or "")
    return "".join(ch for ch in s.lower() if ch.isalpha())
