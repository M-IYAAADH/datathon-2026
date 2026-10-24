#!/usr/bin/env python3
"""Regenerate data/DATA_STATUS.md, data/data_status.csv and the per-dataset READMEs under data/{ready,not_ready,archive}/.

  .venv/bin/python scripts/update_data_status.py            # run checks, rewrite the status board and READMEs
  .venv/bin/python scripts/update_data_status.py --check    # run checks and print results only

Inputs: data/status_manifest.csv (edit this to change a status, blocker or next action) and data/catalog.csv
(provenance: source, retrieval date, licence, rows). Checks are offline and read-only: file exists, row count,
no empty key columns, island codes join to the Census list. Nothing here sets LIVE_CHECKED. Private files are only
counted (rows, bytes); no values are read out of them.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
FOLDER = {"READY": "ready", "NOT READY": "not_ready", "ARCHIVE": "archive"}


def read_csv(p: Path) -> list[dict]:
    with open(p, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def census_codes() -> set[int]:
    return {int(read_csv(p)[0]["island_code"]) for p in sorted((ROOT / "per_island").glob("*.csv"))}


def norm(s: str) -> str:
    s = re.sub(r"^[A-Za-z]{1,4}\.", "", (s or "").strip())
    return re.sub(r"[^a-z]", "", s.lower().replace("aa", "a").replace("ee", "i").replace("oo", "u"))


# ---------------------------------------------------------------- checks: each returns (ok, [messages])
def check_census(codes: set[int]):
    files = sorted((ROOT / "per_island").glob("*.csv"))
    rows = [read_csv(p) for p in files]
    empty = sum(1 for r in rows if len(r) != 1 or not r[0]["island_code"])
    uniq = len({r[0]["island_code"] for r in rows if r})
    ok = len(files) == 209 and not empty and uniq == len(files)
    return ok, [f"{len(files)} files, {uniq} distinct island_code, {empty} empty/multi-row files; this is the master list ({len(codes)} codes)"]


def check_polygons(codes: set[int]):
    import json as j
    feats = j.load(open(DATA / "reference/followme/onemap_study_islands.geojson", encoding="utf-8"))["features"]
    fc = [f["properties"].get("FCODE") or "" for f in feats]
    empty = fc.count("")
    ints = {int(c[2:]) for c in fc if re.fullmatch(r"LD\d{4}", c)}
    dup = len(fc) - len(set(fc))
    ok = len(feats) > 0 and not empty and not dup and bool(ints & codes)
    return ok, [f"{len(feats)} OneMap features, {empty} empty FCODE, {dup} duplicate FCODE",
                f"{len(ints & codes)}/{len(ints)} FCODE join to Census island_code (the rest are islands with no Census row, such as uninhabited or resort islands); {len(codes - ints)} of {len(codes)} Census islands have no polygon here"]


def check_ferry(codes: set[int]):
    d = DATA / "processed/ferry"
    stops, trips, elig = read_csv(d / "stops.csv"), read_csv(d / "trips.csv"), read_csv(d / "routing_eligibility.csv")
    empty = sum(1 for r in stops if not r["stop_id"])
    coded = sum(1 for r in stops if r["official_island_code"])
    app = [r for r in elig if r.get("routing_eligible") == "1"]
    ok = bool(stops) and bool(trips) and not empty and bool(app)
    return ok, [f"{len(stops)} stops ({empty} empty stop_id), {len(trips)} trips, {len(app)} routing_eligible rows",
                f"island_code join: {coded}/{len(stops)} stops carry official_island_code (crosswalk missing, task D2); keys pass, Census join NOT available"]


def check_health(codes: set[int]):
    rows = read_csv(DATA / "reference/health/health_facilities.csv")
    cen = {}
    for p in sorted((ROOT / "per_island").glob("*.csv")):
        r = read_csv(p)[0]
        cen[(r["atoll_code"].lower(), norm(r["island_name"]))] = int(r["island_code"])
    empty = sum(1 for r in rows if not (r["facility_name"] and r["latitude"] and r["longitude"]))
    atoll_map = {"mle": "mle", "male city": "mle"}
    hit = [r for r in rows if (atoll_map.get(r["atoll_code"].lower(), r["atoll_code"].lower()), norm(r["island"])) in cen]
    unv = sum(1 for r in rows if r["verification_status"] == "UNVERIFIED")
    ok = bool(rows) and not empty
    return ok, [f"{len(rows)} rows, {empty} with empty name/lat/lon, {unv} UNVERIFIED rows",
                f"island join by name+atoll (no island_code column): {len(hit)}/{len(rows)} rows matched to Census; the rest need the crosswalk (D2)"]


def check_weather(codes: set[int]):
    import duckdb
    p = str(DATA / "reference/weather/weather_hourly.parquet")
    con = duckdb.connect()
    n, pts, bad = con.execute(f"select count(*), count(distinct point_id), count(*) filter (where point_id is null or point_id='') from read_parquet('{p}')").fetchone()
    per = con.execute(f"select min(c), max(c) from (select count(*) c from read_parquet('{p}') group by point_id)").fetchone()
    pts_csv = {r["point_id"] for r in read_csv(DATA / "reference/weather/weather_points.csv")}
    ids = {r[0] for r in con.execute(f"select distinct point_id from read_parquet('{p}')").fetchall()}
    ok = per == (744, 744) and not bad and ids <= pts_csv
    return ok, [f"{n} hourly rows, {pts} points, {per[0]}-{per[1]} hours per point (need 744), {bad} empty point_id; all point_id in weather_points.csv: {ids <= pts_csv}",
                "No island_code column: joins by point_id -> weather_points.onemap_fcodes"]


def check_followme(codes: set[int]):
    import duckdb
    base = DATA / "private/followme"
    con = duckdb.connect()
    out, ok = [], True
    for name in ("gps_clean", "gps_region_clean", "trajectory_segments"):
        p = base / f"{name}.parquet"
        if not p.exists():
            ok = False
            out.append(f"{name}: MISSING")
            continue
        out.append(f"{name}: {con.execute(f'select count(*) from read_parquet(\'{p}\')').fetchone()[0]:,} rows, {p.stat().st_size / 1e6:,.0f} MB (private, counts only)")
    return ok, out


CHECKS = {"census_2022_per_island": check_census, "island_polygons": check_polygons, "ferry_timetables_2026": check_ferry,
          "health_facilities": check_health, "weather_2025_08": check_weather, "followme_cleaned": check_followme}


def run_checks() -> dict[str, dict]:
    codes = census_codes()
    res = {}
    for name, fn in CHECKS.items():
        try:
            ok, msgs = fn(codes)
        except Exception as exc:  # a crashing check is a failed check
            ok, msgs = False, [f"check crashed: {type(exc).__name__}: {exc}"]
        res[name] = {"ok": ok, "messages": msgs}
    return res


# ---------------------------------------------------------------- board
def build_rows(manifest: list[dict], cat: dict[str, dict], res: dict[str, dict]) -> list[dict]:
    rows = []
    for m in manifest:
        ids = [i for i in m["catalog_ids"].split("|") if i]
        entries = [cat[i] for i in ids if i in cat]
        paths = sorted({p for e in entries for p in e["path"].split("|") if p})
        exists = [p for p in paths if (ROOT / p).exists()]
        missing_ids = [e["dataset_id"] for e in entries if e["sha256"] == "MISSING" or not all((ROOT / p).exists() for p in e["path"].split("|") if not p.startswith(".."))]
        status = m["status"]
        cat_ver = {e["verification_status"] for e in entries}
        if m["verification_override"]:
            ver = m["verification_override"]
        elif m["name"] in res:
            ver = "AUTO_CHECKED" if res[m["name"]]["ok"] else "UNVERIFIED"
            if "NEEDS_ORGANISER" in cat_ver:
                ver = "NEEDS_ORGANISER"
        elif "NEEDS_ORGANISER" in cat_ver or m["name"] in ("vessel_services_inferred", "organiser_answers"):
            ver = "NEEDS_ORGANISER"
        else:
            ver = "UNVERIFIED"
        if "LIVE_CHECKED" in cat_ver and m["name"] not in res:
            ver = "LIVE_CHECKED"
        rows.append({"name": m["name"], "path": "; ".join(sorted({"/".join(p.split("/")[:3]) for p in paths})) or "(not built)",
                     "status": status, "verification": ver, "private": m["private"], "owner": "", "blocks": m["blocks"], "next_action": m["next_action"],
                     "tasks": m["task_ids"].replace("|", " "), "missing_files": ", ".join(missing_ids), "_ids": ids, "_paths": paths})
    return rows


def render_md(rows: list[dict], res: dict[str, dict]) -> str:
    L = ["# Data status board", "", "Generated by `.venv/bin/python scripts/update_data_status.py` from `data/status_manifest.csv` and `data/catalog.csv`. Do not edit by hand: edit the manifest and rerun.",
         "Detailed per-file catalogue: [`catalog.csv`](catalog.csv), audit: [`audit_report.md`](audit_report.md), tasks: [`../docs/TASKS.md`](../docs/TASKS.md).", ""]
    cnt = {s: sum(1 for r in rows if r["status"] == s) for s in ("READY", "NOT READY", "ARCHIVE", "NEEDS_CHECK")}
    L += ["READY " + str(cnt["READY"]) + ", NOT READY " + str(cnt["NOT READY"]) + ", ARCHIVE " + str(cnt["ARCHIVE"]) + ", NEEDS_CHECK " + str(cnt["NEEDS_CHECK"]) + ".",
          "Verification: UNVERIFIED (existence only), AUTO_CHECKED (this script's checks passed), LIVE_CHECKED (checked against the live source; never set automatically), NEEDS_ORGANISER (waiting on an organiser answer).", "",
          "Datasets stay at their original paths because scripts, tests and `config/` read them there; `data/{ready,not_ready,archive}/<name>/README.md` describe each one and point to its files. Only the archived Gemini copies were physically moved.", "",
          "| name | status | verification | private | path | owner | what blocks it | next action | tasks |", "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for r in rows:
        L.append("| " + " | ".join([f"`{r['name']}`", r["status"], r["verification"], r["private"], f"`{r['path']}`" if r["path"] != "(not built)" else r["path"], r["owner"], r["blocks"].replace("|", "/"), r["next_action"].replace("|", "/"), r["tasks"]]) + " |")
    L += ["", "## Checks on READY datasets", ""]
    for name, r in res.items():
        L.append(f"- **{name}**: {'PASS' if r['ok'] else 'FAIL'}")
        L += [f"  - {m}" for m in r["messages"]]
    L += ["", "## What blocks the planner", "",
          "1. **Vessel services** are inferred with the old timing and stop logic; they need the clock-offset rebuild and island-shoreline stops (D1).",
          "2. **No ferry stop to Census island_code crosswalk** (D2). `stops.official_island_code` is blank for all stops, so timetables cannot join to population or health data.",
          "3. **2026 timetable and the Rasdhoo 10:55 arrive / 11:00 depart connection** are not yet confirmed with MTCC or the organisers (D3, Q5, Q6).",
          "4. **No `build_app_data.py`**, so there is no app-ready JSON yet (D4).",
          "5. **Organiser answers** on timezone, speed unit, device = vessel, shift limits and transfer rules are all OPEN (D5, Q1-Q8).",
          "6. Route exposure (D8) and the weather-warning format (D9) do not exist; weather is not yet joined to FollowMe days (D10).", ""]
    return "\n".join(L)


def readme(r: dict, cat: dict[str, dict], res: dict[str, dict]) -> str:
    ents = [cat[i] for i in r["_ids"] if i in cat]
    f = lambda k: "; ".join(dict.fromkeys(e[k] for e in ents if e[k])) or "UNKNOWN"
    rows_ = ", ".join(f"{e['dataset_id']}: {e['rows']}" for e in ents if e["rows"] and e["rows"] != "UNKNOWN") or "n/a"
    L = [f"# {r['name']}", "", f"Status: **{r['status']}** · verification: **{r['verification']}** · private: {r['private']}", "",
         "_Generated by `scripts/update_data_status.py`. Unless the folder holds the files itself, the data stays at the paths below (scripts and tests read them there); this page documents them._", ""]
    if ents:
        L += ["## What it is", ""] + [f"- `{e['dataset_id']}`: {e['description']}" for e in ents] + [""]
    L += ["## Where the files are", ""] + ([f"- `{p}`" for p in r["_paths"]] or ["- (nothing built yet)"]) + [""]
    L += ["## Provenance", "", f"- Source: {f('source')}", f"- Retrieval date: {f('retrieval_date')}", f"- Licence: {f('licence')}", f"- Row counts: {rows_}",
          f"- Key columns: {f('key_columns')}", f"- Joins: {f('joins_to')}", ""]
    issues = [e["known_issues"] for e in ents if e["known_issues"]]
    L += ["## Known issues", ""] + ([f"- {i}" for i in issues] or ["- none recorded"]) + [""]
    if r["name"] in res:
        L += ["## Latest automatic check", ""] + [f"- {'PASS' if res[r['name']]['ok'] else 'FAIL'}"] + [f"- {m}" for m in res[r["name"]]["messages"]] + [""]
    L += ["## To make it READY" if r["status"] != "READY" else "## Open items", "", f"- Blocked by: {r['blocks'] or 'nothing recorded'}", f"- Next action: {r['next_action'] or 'none'}", f"- Tasks: {r['tasks'] or 'none'} (see docs/TASKS.md)", ""]
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true", help="run checks and print results; write nothing")
    a = ap.parse_args()
    manifest = read_csv(DATA / "status_manifest.csv")
    cat = {r["dataset_id"]: r for r in read_csv(DATA / "catalog.csv")}
    res = run_checks()
    for n, r in res.items():
        print(("PASS" if r["ok"] else "FAIL"), n, *r["messages"], sep="\n  ")
    if a.check:
        return 0 if all(r["ok"] for r in res.values()) else 1
    rows = build_rows(manifest, cat, res)
    (DATA / "DATA_STATUS.md").write_text(render_md(rows, res), encoding="utf-8")
    cols = ["name", "path", "status", "verification", "private", "owner", "blocks", "next_action", "tasks"]
    with open(DATA / "data_status.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    for r in rows:
        if r["status"] in FOLDER and r["private"] == "no":
            d = DATA / FOLDER[r["status"]] / r["name"]
            d.mkdir(parents=True, exist_ok=True)
            (d / "README.md").write_text(readme(r, cat, res), encoding="utf-8")
    print(f"wrote data/DATA_STATUS.md, data/data_status.csv and {sum(1 for r in rows if r['status'] in FOLDER and r['private'] == 'no')} dataset READMEs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
