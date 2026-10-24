"""Audit checks. Each returns a dict: id, title, status (pass/fail/warn/skip), datasets, summary, details, data."""
from __future__ import annotations

import csv
import json
import re
import subprocess
from collections import Counter, defaultdict
from pathlib import Path

from .common import (MISSING_MARK, ROOT, VERIFICATION_STATUSES, DATASET_STATUSES, CATALOG_COLUMNS, dir_shape, file_shape,
                     fingerprint, iter_files, norm_name, read_csv, resolve, split_paths)


def result(cid, title, status, datasets, summary, details=None, data=None):
    return {"id": cid, "title": title, "status": status, "datasets": sorted(set(datasets)), "summary": summary,
            "details": details or [], "data": data or {}}


def load_config(root: Path = ROOT) -> dict:
    return json.loads((root / "config" / "data_audit.json").read_text())


def _rel(root, p):
    try:
        return str(Path(p).resolve().relative_to(root))
    except ValueError:
        return str(p)


# ---------------------------------------------------------------- catalogue schema + check 1
def check_catalog_schema(cat, root=ROOT, **_):
    errs = []
    if cat and list(cat[0].keys()) != CATALOG_COLUMNS:
        errs.append("header differs from CATALOG_COLUMNS")
    ids = Counter(r["dataset_id"] for r in cat)
    errs += [f"duplicate dataset_id {i}" for i, n in ids.items() if n > 1]
    for r in cat:
        d = r["dataset_id"]
        if r["verification_status"] not in VERIFICATION_STATUSES:
            errs.append(f"{d}: bad verification_status {r['verification_status']!r}")
        if r["status"] not in DATASET_STATUSES:
            errs.append(f"{d}: bad status {r['status']!r}")
        if r["private"] not in ("yes", "no"):
            errs.append(f"{d}: private must be yes/no")
        if r["private"] == "yes" and not (r["path"].startswith("data/private/") or r["path"].startswith("../")):
            errs.append(f"{d}: private dataset outside data/private/ and the sibling folders")
        if r["verification_status"] == "LIVE_CHECKED" and not (r["verified_by"] and r["verified_on"]):
            errs.append(f"{d}: LIVE_CHECKED needs verified_by and verified_on")
        if r["verification_status"] in ("AUTO_CHECKED", "LIVE_CHECKED") and not r["verified_on"]:
            errs.append(f"{d}: {r['verification_status']} needs verified_on")
    return result("0", "Catalogue schema", "fail" if errs else "pass", [], f"{len(cat)} rows, {len(errs)} schema problems", errs)


def check_files(cat, root=ROOT, deep=False, **_):
    fails, notes, ok = [], [], []
    for r in cat:
        d = r["dataset_id"]
        paths = [resolve(p, root) for p in split_paths(r["path"])]
        exists = [p.exists() for p in paths]
        if r["sha256"] == MISSING_MARK:
            if any(exists):
                fails.append(f"{d}: catalogued MISSING but {r['path']} exists; refresh the catalogue")
            else:
                notes.append(f"{d}: MISSING (as catalogued)")
            continue
        if not all(exists):
            fails.append(f"{d}: file not found: {r['path']}")
            continue
        sha, size = fingerprint(paths, root, deep=deep, secret=r["sha256"] == "secret-not-hashed")
        if sha != r["sha256"]:
            if sha.startswith("size-manifest:") != r["sha256"].startswith("size-manifest:") and not deep:
                notes.append(f"{d}: checksum kind differs (use --deep consistently)")
            else:
                fails.append(f"{d}: checksum changed since catalogued ({'size-manifest' if 'size-manifest' in sha else 'content'}); review, then build_data_catalog.py --refresh")
            continue
        if r["rows"] != "":
            first = paths[0]
            rows, cols = dir_shape(first) if first.is_dir() else file_shape(first)
            if rows != int(r["rows"]) or (r["columns"] != "" and cols != int(r["columns"])):
                fails.append(f"{d}: rows/columns {rows}/{cols} != catalogue {r['rows']}/{r['columns']}")
                continue
        ok.append(d)
    # every file under data/ and per_island/ (except private/archive-of-itself) must be covered by a catalogue path
    covered = [resolve(p, root) for r in cat for p in split_paths(r["path"])]
    unc = []
    for base in (root / "data", root / "per_island"):
        for f in iter_files(base):
            if "private" in f.relative_to(root).parts[:2] or f.name in ("catalog.csv", "README.md", "audit_report.md", "audit_results.json", "verification_checklist.csv") or f.name == ".DS_Store":
                continue
            if not any(f == c or c in f.parents for c in covered):
                unc.append(_rel(root, f))
    if unc:
        fails.append(f"{len(unc)} files under data/ are not covered by any catalogue path, e.g. {unc[:5]}")
    status = "fail" if fails else "pass"
    return result("1", "Existence, checksum, row/column counts", status, ok if not fails else ok,
                  f"{len(ok)} datasets verified, {len(fails)} failures, {len(notes)} notes", fails + notes,
                  {"verified": ok, "uncatalogued_files": unc})


# ---------------------------------------------------------------- check 2 privacy
def _git(root, *args):
    try:
        out = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=60)
        return out.returncode, out.stdout
    except (OSError, subprocess.SubprocessError):
        return 127, ""


def check_privacy(cat, root=ROOT, cfg=None, **_):
    cfg = cfg or load_config(root)
    bad, notes = [], []
    forbidden = [c.lower() for c in cfg["private_forbidden_columns"]]
    imei = re.compile(cfg["imei_pattern"])
    skip_dirs = {".venv", ".git", "__pycache__", ".pytest_cache", "private"}
    scanned = 0
    for f in sorted(root.rglob("*")):
        rel = f.relative_to(root)
        if not f.is_file() or skip_dirs & set(rel.parts) or rel.parts[0] == "tests":
            continue
        ext = f.suffix.lower()
        if ext in (".csv", ".tsv"):
            scanned += 1
            with open(f, newline="", encoding="utf-8-sig", errors="replace") as fh:
                hdr = [h.lower() for h in next(csv.reader(fh), [])]
            hit = [c for c in forbidden if c in hdr]
            if hit:
                bad.append(f"{rel}: column(s) {hit}")
        elif ext == ".parquet":
            scanned += 1
            import duckdb
            p = str(f).replace("'", "''")
            cols = [x[0].lower() for x in duckdb.connect().execute(f"describe select * from read_parquet('{p}')").fetchall()]
            hit = [c for c in forbidden if c in cols]
            if hit:
                bad.append(f"{rel}: column(s) {hit}")
        if ext in (".csv", ".json", ".geojson", ".md", ".txt") and f.stat().st_size < 50_000_000 and rel.parts[0] != "docs":
            scanned += 1
            text = f.read_text(encoding="utf-8", errors="replace")
            if "device_key" in text.lower() and ext in (".json", ".geojson") and "device_key" in text:
                if re.search(r'"device_key"\s*:', text):
                    bad.append(f"{rel}: JSON holds a device_key value")
            for m in imei.finditer(text):
                s = m.group(0)
                if f.suffix == ".csv" and re.search(r"(latitude|longitude|hash|sha|id)", text[max(0, m.start() - 40):m.start()].lower()):
                    continue
                bad.append(f"{rel}: 15-digit number {s[:4]}… (possible IMEI)")
                break
    gi = (root / ".gitignore").read_text() if (root / ".gitignore").exists() else ""
    rc, _o = _git(root, "check-ignore", "-q", "data/private/followme/x.parquet")
    if rc == 127:
        ignored = any(l.strip() in ("data/private/", "data/private", "/data/private/") for l in gi.splitlines())
        notes.append("git unavailable: gitignore checked by text only")
    else:
        ignored = rc == 0
    if not ignored:
        bad.append("data/private/ is NOT gitignored")
    rc, out = _git(root, "ls-files", "data/private")
    if rc == 0 and out.strip():
        bad.append(f"git tracks {len(out.split())} files under data/private/")
    # large third-party raw files tracked or not ignored
    big = []
    thr = cfg["large_raw_bytes"]
    for f in sorted((root / "data").rglob("*")):
        if not f.is_file() or "private" in f.relative_to(root).parts:
            continue
        p = "/" + str(f.relative_to(root)).replace("\\", "/")
        is_raw = any(m in p for m in cfg["raw_path_markers"]) or f.suffix.lower() in cfg["raw_extensions"]
        if is_raw and f.stat().st_size > thr:
            rc, _o = _git(root, "check-ignore", "-q", str(f.relative_to(root)))
            if rc == 1:
                rc2, tracked = _git(root, "ls-files", "--error-unmatch", str(f.relative_to(root)))
                big.append(f"{f.relative_to(root)} ({f.stat().st_size/1e6:.1f} MB) {'TRACKED' if rc2 == 0 else 'not ignored'}")
            elif rc == 127:
                notes.append("git unavailable: raw-file ignore check skipped")
                break
    bad += [f"large third-party raw file not gitignored: {b}" for b in big]
    sib = root.parent / "FollowMe_pipeline" / "out" / "pings"
    if sib.exists():
        notes.append(f"NOTE: per-device (salted-hash) tracks exist OUTSIDE the repo at {sib.parent.parent.name}/out/pings (sibling folder, not covered by this repo's .gitignore; the parent folder is not a git repo)")
    return result("2", "Privacy", "fail" if bad else "pass", [r["dataset_id"] for r in cat if r["private"] == "no" and r["status"] != "deprecated"] if not bad else [],
                  f"{scanned} files scanned outside data/private/, {len(bad)} problems", bad + notes)


# ---------------------------------------------------------------- shared lookups for checks 3/4
def load_census(root):
    rows = []
    for f in sorted((root / "per_island").glob("*.csv")):
        rows += read_csv(f)
    return rows


def census_key(r, cfg):
    name = r["island_name"].split(".", 1)[-1] if "." in r["island_name"] else r["island_name"]
    n = norm_name(name)
    return cfg["name_aliases"].get(n, n)


def census_index(root, cfg):
    census = load_census(root)
    idx = defaultdict(list)
    for r in census:
        if "non admin" in r["island_name"].lower():
            continue
        idx[(r["atoll_code"].lower(), census_key(r, cfg))].append(r["island_code"])
    by_name = defaultdict(list)
    for (a, n), codes in idx.items():
        by_name[n] += [(a, c) for c in codes]
    return census, idx, by_name


def map_name(atoll, name, idx, by_name, cfg):
    n = norm_name(name)
    n = cfg["name_aliases"].get(n, n)
    a = cfg["atoll_aliases"].get(atoll.lower(), atoll.lower()) if atoll else ""
    if a and (a, n) in idx:
        return idx[(a, n)], "atoll+name"
    if n in by_name and (not a or len({x[0] for x in by_name[n]}) == 1 and a == "mle"):
        return [c for _, c in by_name[n]], "name only"
    return [], "unmapped"


def check_joins(cat, root=ROOT, cfg=None, **_):
    cfg = cfg or load_config(root)
    census, idx, by_name = census_index(root, cfg)
    det, bad = [], 0
    codes = Counter(r["island_code"] for r in census)
    dup = [c for c, n in codes.items() if n > 1]
    if dup:
        det.append(f"FAIL duplicate Census island_code: {dup}")
        bad += 1
    else:
        det.append(f"OK Census island_code unique ({len(codes)} codes in {len(census)} rows)")
    census_codes = {int(c) for c in codes}
    census_names = {int(r["island_code"]): census_key(r, cfg) for r in census}
    data = {}
    # ferry stops
    stops = read_csv(root / "data/processed/ferry/stops.csv")
    filled = [s for s in stops if s["official_island_code"]]
    cw = root / "data/reference/crosswalk/ferry_stop_census_island.csv"
    unresolved, ambiguous = [], []
    cwmap = {}
    if cw.exists():
        cwmap = {r["stop_id"]: r["census_island_code"] for r in read_csv(cw)}
    mapped = 0
    for s in stops:
        code = s["official_island_code"] or cwmap.get(s["stop_id"], "")
        if code:
            mapped += 1
            if int(code) not in census_codes:
                det.append(f"FAIL ferry stop {s['stop_id']} has island code {code} not in Census"); bad += 1
            continue
        c, how = map_name(s["atoll_abbreviation"], s["island_name_en"], idx, by_name, cfg)
        if len(c) == 1:
            continue
        (ambiguous if len(c) > 1 else unresolved).append(f"{s['stop_id']} ({s['atoll_abbreviation'] or '-'} {s['island_name_en']})")
    name_ok = len(stops) - len(unresolved) - len(ambiguous) - mapped
    det.append(f"ferry stops: {len(stops)} total; {mapped} carry a Census island code (official_island_code or crosswalk); {name_ok} more match a Census island by strict name+atoll only (audit evidence, not a crosswalk); {len(unresolved)} unmapped; {len(ambiguous)} ambiguous")
    if not mapped:
        det.append("FAIL no ferry stop has a Census island code: the crosswalk is MISSING, so stops are unresolved by definition")
        bad += 1
    data["ferry_stops_unmapped"] = unresolved
    data["ferry_stops_ambiguous"] = ambiguous
    det.append("unmapped ferry stops (first 40): " + "; ".join(unresolved[:40]))
    # vessel services / jetties: LD#### must be a Census code and the name must agree
    vs = read_csv(root / "data/derived/vessels/service_departures.csv")
    vcodes = {}
    for r in vs:
        vcodes[r["island_code"]] = r["island_name"]
    for r in read_csv(root / "data/derived/vessels/service_edges.csv"):
        vcodes[r["origin_code"]] = r["origin_name"]; vcodes[r["destination_code"]] = r["destination_name"]
    vmis = []
    for code, nm in sorted(vcodes.items()):
        try:
            ci = int(code.replace("LD", ""))
        except ValueError:
            vmis.append(f"{code} {nm}: not LD####"); continue
        if ci not in census_codes:
            vmis.append(f"{code} {nm}: code not in Census")
        else:
            a, b = norm_name(nm), cfg["name_aliases"].get(norm_name(nm), norm_name(nm))
            if census_names[ci] not in (a, b):
                vmis.append(f"{code}: vessel name {nm!r} vs Census name {census_names[ci]!r}")
    det.append(f"vessel services: {len(vcodes)} island codes, {len(vmis)} not matching Census code+name" + (": " + "; ".join(vmis) if vmis else ""))
    bad += len(vmis)
    data["vessel_mismatches"] = vmis
    # health facilities
    hf = read_csv(root / "data/reference/health/health_facilities.csv")
    hun = []
    for r in hf:
        c, how = map_name(r["atoll_code"], r["island"], idx, by_name, cfg)
        if len(c) != 1:
            hun.append(f"{r['facility_name']} ({r['atoll_code']} {r['island']}): {'ambiguous' if c else 'unmapped'}")
    det.append(f"health facilities: {len(hf)} rows, {len(hun)} do not map to one Census code" + (": " + "; ".join(hun) if hun else ""))
    data["health_unmapped"] = hun
    # polygons
    feats = json.loads((root / "data/reference/followme/onemap_study_islands.geojson").read_text())["features"]
    pun = []
    pcodes = Counter()
    for f in feats:
        p = f["properties"]
        fc = p["FCODE"]
        ci = int(fc.replace("LD", "")) if fc.startswith("LD") else None
        pcodes[fc] += 1
        if ci not in census_codes:
            pun.append(f"{fc} {p['atoll']} {p['islandName']}")
    dupp = [k for k, n in pcodes.items() if n > 1]
    det.append(f"island polygons: {len(feats)} features; {len(pun)} FCODEs have no Census island (expected: uninhabited/resort/industrial islands are not in the Census); {len(dupp)} duplicate FCODEs {dupp[:5]}")
    ph = sum(1 for c in census_codes if f"LD{c:04d}" not in pcodes and c < 40000)
    det.append(f"Census islands with no polygon (outside the 5 study atolls or not in layer): {ph}")
    data.update(polygons_without_census=len(pun), census_without_polygon=ph)
    # status: failures are real contradictions; unmapped names are reported
    status = "fail" if bad else "pass"
    return result("3", "Join integrity (island_code)", status, ["census_2022_per_island", "ferry_stops", "vessel_service_departures", "vessel_service_edges", "health_facilities", "onemap_island_footprints"] if not bad else [],
                  f"{bad} hard failures; {len(unresolved)} unmapped ferry stops, {len(hun)} unmapped health rows", det, data)


# ---------------------------------------------------------------- check 4 coordinates
def check_coordinates(cat, root=ROOT, cfg=None, **_):
    from pyproj import Transformer
    from shapely.geometry import Point, shape
    from shapely.ops import transform

    cfg = cfg or load_config(root)
    tol = cfg["polygon_tolerance_m"]
    tf = Transformer.from_crs(4326, 32643, always_xy=True).transform
    feats = json.loads((root / "data/reference/followme/onemap_study_islands.geojson").read_text())["features"]
    polys = defaultdict(list)
    for f in feats:
        polys[int(f["properties"]["FCODE"][2:])].append(transform(tf, shape(f["geometry"])))
    census, idx, by_name = census_index(root, cfg)
    exc, nopoly, checked = [], [], 0
    for r in census:
        if "non admin" in r["island_name"].lower() or not r["latitude"]:
            continue
        code = int(r["island_code"])
        if code not in polys:
            nopoly.append(f"{code} {r['island_name']}")
            continue
        pt = transform(tf, Point(float(r["longitude"]), float(r["latitude"])))
        d = min(p.distance(pt) for p in polys[code])
        checked += 1
        if d > tol:
            exc.append(f"Census {code} {r['island_name']}: {d:.0f} m from polygon")
    det = [f"Census island points checked against own polygon: {checked}; exceptions >{tol} m: {len(exc)}; islands with no polygon in the layer (not checked): {len(nopoly)}"]
    det += exc
    # Male area: compare against Census points instead
    cen_pts = {int(r["island_code"]): (float(r["longitude"]), float(r["latitude"])) for r in census if r["latitude"] and "non admin" not in r["island_name"].lower()}
    hf = read_csv(root / "data/reference/health/health_facilities.csv")
    hexc, hno, hchk, hmale = [], [], 0, 0
    for r in hf:
        c, _how = map_name(r["atoll_code"], r["island"], idx, by_name, cfg)
        if len(c) != 1:
            hno.append(f"{r['facility_name']}: island not mapped"); continue
        code = int(c[0])
        pt = transform(tf, Point(float(r["longitude"]), float(r["latitude"])))
        if code in polys:
            d = min(p.distance(pt) for p in polys[code]); ref = "polygon"
        elif code in cen_pts:
            d = transform(tf, Point(*cen_pts[code])).distance(pt); ref = "Census point"; hmale += 1
        else:
            hno.append(f"{r['facility_name']}: no polygon or Census point"); continue
        hchk += 1
        limit = tol
        if d > limit:
            hexc.append(f"{r['facility_name']} ({r['island']}): {d:.0f} m from {ref}")
    det.append(f"health facilities checked: {hchk} (of which {hmale} against the Census point because the island is not in the polygon layer); exceptions >{tol} m: {len(hexc)}; unchecked: {len(hno)}")
    det += hexc + hno
    bad = len(exc) + len(hexc)
    return result("4", "Coordinates within ~1 km of island polygon", "fail" if bad else "pass", ["health_facilities", "census_2022_per_island", "onemap_island_footprints"] if not bad else [],
                  f"{len(exc)} Census and {len(hexc)} health exceptions", det, {"census_exceptions": exc, "health_exceptions": hexc, "census_without_polygon": nopoly, "health_unchecked": hno})


# ---------------------------------------------------------------- check 5 time ranges
def check_time(cat, root=ROOT, cfg=None, **_):
    import duckdb

    cfg = cfg or load_config(root)
    lo, hi = cfg["followme_window"]
    det, bad = [], 0
    con = duckdb.connect(); con.execute("SET TimeZone='UTC'")
    gp = root / "data/private/followme/gps_clean.parquet"
    if gp.exists():
        a, b = con.execute(f"select min(collection_time)::varchar, max(collection_time)::varchar from read_parquet('{gp}')").fetchone()
        ok = a[:10] == lo and b[:10] == hi
        det.append(f"{'OK' if ok else 'FAIL'} FollowMe gps_clean collection_time {a} .. {b} (expected {lo} .. {hi})"); bad += not ok
    else:
        det.append("SKIP gps_clean.parquet absent")
    wp = root / "data/reference/weather/weather_hourly.parquet"
    exp = cfg["weather_hours_per_point"]
    cnt = con.execute(f"select point_id, count(*), count(distinct time_utc), min(time_utc)::varchar, max(time_utc)::varchar from read_parquet('{wp}') group by 1").fetchall()
    badp = [c[0] for c in cnt if c[1] != exp or c[2] != exp]
    ranges = {(c[3], c[4]) for c in cnt}
    ok = not badp and ranges == {("2025-08-01 00:00:00+00", "2025-08-31 23:00:00+00")}
    det.append(f"{'OK' if ok else 'FAIL'} weather: {len(cnt)} points, {exp} hourly values each expected; points off-count {badp[:5]}; UTC ranges {sorted(ranges)}"); bad += not ok
    det.append("INFO weather time_local runs 2025-08-01 05:00 to 2025-09-01 04:00 (UTC+5): the first five local hours of 1 Aug are absent")
    src = read_csv(root / "data/processed/ferry/sources.csv")
    with_date = [s["source_id"] for s in src if s["effective_from"]]
    without = [s["source_id"] for s in src if not s["effective_from"]]
    det.append(f"MTCC timetable effective dates recorded for {len(with_date)} of {len(src)} sources: " + ", ".join(f"{s['source_id']}={s['effective_from']}" for s in src if s["effective_from"]))
    det.append(f"no printed effective date (effective_from blank; date evidence is file name/PDF metadata only): {', '.join(without)}")
    sv = read_csv(root / "data/processed/ferry/schedule_versions.csv")
    sel = Counter(r["selection_status"] for r in sv)
    det.append(f"schedule_versions selection_status: {dict(sel)}")
    bad_ev = [s["source_id"] for s in src if not s["date_evidence"]]
    if bad_ev:
        det.append(f"FAIL sources without date_evidence: {bad_ev}"); bad += 1
    return result("5", "Time ranges", "fail" if bad else "pass", ["followme_gps_clean", "weather_hourly", "ferry_sources"] if not bad else [], f"{bad} failures", det)


# ---------------------------------------------------------------- check 6 vessel services
def check_vessels(cat, root=ROOT, cfg=None, **_):
    cfg = cfg or load_config(root)
    mind, mind_days = cfg["disclosure_min_devices"], cfg["disclosure_min_days"]
    d = root / "data/derived/vessels"
    det, bad = [], 0
    low = []
    for name, dev_col, day_col in [("service_departures.csv", "distinct_devices", "days_seen"), ("service_signatures.csv", "distinct_devices", "days_seen"), ("service_edges.csv", "distinct_devices", "days_seen")]:
        for i, r in enumerate(read_csv(d / name), 2):
            try:
                if int(r[dev_col]) < mind or int(r[day_col]) < mind_days:
                    low.append(f"{name} line {i}: devices={r[dev_col]} days={r[day_col]}")
            except ValueError:
                low.append(f"{name} line {i}: non-numeric {r[dev_col]!r}/{r[day_col]!r}")
    cs = json.loads((d / "corridor_services.json").read_text())
    for s in cs["services"]:
        if s["distinct_devices"] < mind or s["days_seen"] < mind_days:
            low.append(f"corridor_services {s['service_id']}: devices={s['distinct_devices']} days={s['days_seen']}")
        if s.get("inferred") is not True:
            low.append(f"corridor_services {s['service_id']}: not flagged inferred")
    det.append(f"{'OK' if not low else 'FAIL'} disclosure minimum ({mind} devices, {mind_days} days) on every exported row; violations {len(low)}"); det += low[:20]; bad += len(low)
    notinf = [s["service_id"] for s in cs["services"] if not s.get("inferred")]
    flags = {k: cs.get(k) for k in ("inferred", "timezone_confirmed", "speed_unit_confirmed")}
    sm = json.loads((d / "service_summary.json").read_text())
    missing = [k for k in ("timezone_confirmed", "speed_unit_confirmed") if k not in cs or k not in sm]
    notfalse = [k for k in ("timezone_confirmed", "speed_unit_confirmed") if cs.get(k) is not False or sm.get(k) is not False]
    det.append(f"{'OK' if not missing and not notfalse else 'FAIL'} flags: corridor_services {flags}; summary timezone_confirmed={sm.get('timezone_confirmed')} speed_unit_confirmed={sm.get('speed_unit_confirmed')}; missing {missing}; not false {notfalse}")
    bad += bool(missing or notfalse or notinf)
    org = (root / "data/organiser_answers.md").read_text() if (root / "data/organiser_answers.md").exists() else ""
    if (cs.get("timezone_confirmed") is False) != ("| Q1 | OPEN" in org):
        det.append("WARN organiser_answers.md Q1 status disagrees with the timezone_confirmed flag")
    basis = Counter(r.get("time_basis", "") for r in read_csv(d / "service_departures.csv"))
    det.append(f"time_basis: corridor_services={cs.get('time_basis')}, summary={sm.get('time_basis')}, service_departures rows {dict(basis)}")
    det.append(f"time correction: observations_corrected_in_scope={sm['clock_correction'].get('observations_corrected_in_scope')} (raw and corrected times are therefore identical); services using corrected time: {len(cs['services'])} of {len(cs['services'])} in corridor_services (basis 'corrected', 0 observations actually shifted); services using raw time: 0")
    return result("6", "Vessel services", "fail" if bad else "pass", ["vessel_corridor_services", "vessel_service_departures", "vessel_service_signatures", "vessel_service_edges", "vessel_service_summary"] if not bad else [],
                  f"{len(cs['services'])} services; {len(low)} disclosure violations", det, {"flags": flags})


# ---------------------------------------------------------------- check 7 ferry
def _hms(t):
    return t[:5]


def check_ferry(cat, root=ROOT, cfg=None, **_):
    cfg = cfg or load_config(root)
    p = root / "data/processed/ferry"
    trips = {r["trip_id"]: r for r in read_csv(p / "trips.csv")}
    elig = {r["trip_id"]: r for r in read_csv(p / "routing_eligibility.csv")}
    st = defaultdict(list)
    for r in read_csv(p / "stop_times.csv"):
        st[r["trip_id"]].append(r)
    det, bad = [], 0
    app = [t for t, e in elig.items() if e[cfg["app_ferry_eligible_flag"]] == "1"]
    srcs = Counter(trips[t]["source_id"] for t in app)
    det.append(f"app trips (routing_eligible=1): {len(app)} by source {dict(srcs)}")
    nonmono_all, nonmono_app = [], []
    for t, rows in st.items():
        rows.sort(key=lambda r: int(r["stop_sequence"]))
        prev = None
        for r in rows:
            a = int(r["arrival_seconds"]) if r["arrival_seconds"] else None
            dpt = int(r["departure_seconds"]) if r["departure_seconds"] else None
            bad_row = (a is not None and dpt is not None and dpt < a) or (prev is not None and (a if a is not None else dpt) is not None and (a if a is not None else dpt) < prev)
            if bad_row:
                (nonmono_app if t in app else nonmono_all).append(t); break
            prev = dpt if dpt is not None else a
    det.append(f"{'OK' if not nonmono_app else 'FAIL'} times non-decreasing within every app trip: {len(nonmono_app)} violations {nonmono_app[:5]}; across all {len(st)} trips with stop_times: {len(nonmono_all) + len(nonmono_app)} violations {nonmono_all[:5]}")
    bad += bool(nonmono_app)
    sv = read_csv(p / "services.csv")
    days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    unp = [s["service_id"] for s in sv if any(s[d] not in ("0", "1", "") for d in days)]
    allblank = [s["service_id"] for s in sv if all(s[d] == "" for d in days)]
    app_services = {trips[t]["service_id"] for t in app}
    app_blank = [s for s in allblank if s in app_services]
    det.append(f"{'OK' if not unp and not app_blank else 'FAIL'} day-of-week fields: {len(sv)} services; unparseable {len(unp)}; all-blank {len(allblank)} (of which used by app trips: {len(app_blank)})"); bad += bool(unp or app_blank)
    # Rasdhoo
    rid = cfg["rasdhoo_stop_id"]
    found = {}
    for chk in cfg["rasdhoo_checks"]:
        field = "arrival_time" if chk["event"] == "arrival" else "departure_time"
        hits = [(t, r) for t in app if trips[t]["route_id"] == f"cfn:{chk['route']}" for r in st[t] if r["stop_id"] == rid and r[field] == chk["time"]]
        found[f"{chk['route']} {chk['event']} {chk['time']}"] = len(hits)
    ok = all(v >= 1 for v in found.values())
    det.append(f"{'OK' if ok else 'FAIL'} Rasdhoo in app version ({cfg['app_ferry_source_id']}): {found}"); bad += not ok
    r303 = [(t, [(r['arrival_time'], r['departure_time']) for r in st[t] if r['stop_id'] == rid]) for t in app if trips[t]["route_id"] == "cfn:303"]
    r301 = [(t, [(r['arrival_time'], r['departure_time']) for r in st[t] if r['stop_id'] == rid]) for t in app if trips[t]["route_id"] == "cfn:301"]
    det.append(f"INFO consistency: route 301 trips call at Rasdhoo {[x[1] for x in r301 if x[1]]}; route 303 trips {[x[1] for x in r303 if x[1]]}. The 10:55 arrival is a Route 301 trip and the 11:00 departure a Route 303 trip (that 303 trip arrives 10:35), so the 5-minute connection is a designed link between two routes, not one vessel dwelling.")
    # 2022 vs 2024 vs 2026 for the corridor
    def sig(source, route):
        out = []
        for t, tr in trips.items():
            if tr["source_id"] == source and tr["route_id"].split(":")[1] == route:
                out.append(tuple((r["stop_id"], r["arrival_time"], r["departure_time"]) for r in sorted(st[t], key=lambda r: int(r["stop_sequence"]))))
        return sorted(out)
    diffs = []
    for route in cfg["corridor_route_codes"]:
        a, b, c = sig("ctn_2022_k_aa_adh_v", route), sig("ctn_2024_all_atolls", route), sig("cfn_2026_all", route)
        diffs.append(f"route {route}: 2022 trips={len(a)}, 2024={len(b)}, 2026={len(c)}; 2022==2026 stop times identical: {a == c}; 2024==2026: {b == c}")
        if a != c:
            sa, sc = set(a), set(c)
            diffs.append(f"   only in 2022: {[[(s[0].replace('stp_',''), s[1][:5], s[2][:5]) for s in t] for t in sorted(sa - sc)][:3]}")
            diffs.append(f"   only in 2026: {[[(s[0].replace('stp_',''), s[1][:5], s[2][:5]) for s in t] for t in sorted(sc - sa)][:3]}")
    vd = read_csv(root / "data/interim/ferry/version_diffs.csv")
    corr = [r for r in vd if r["route_code"] in cfg["corridor_route_codes"] or r["route_code"] in ("302", "304", "305", "306", "307", "308", "309", "310")]
    diffs.append("version_diffs.csv for AA/ADh/V corridor routes 301-310: " + "; ".join(f"{r['route_code']} {r['older_or_a_source'][:8]}→{r['newer_or_b_source'][:8]}: days_changed={r['days_changed']}, +stops {r['stops_only_in_b'] or '-'}, legs 2022-only {len(r['legs_only_in_a'].split(' | ')) if r['legs_only_in_a'] else 0}/new {len(r['legs_only_in_b'].split(' | ')) if r['legs_only_in_b'] else 0}" for r in corr))
    det += diffs
    return result("7", "Ferry timetables", "fail" if bad else "pass", ["ferry_trips", "ferry_stop_times", "ferry_services", "ferry_routing_eligibility"] if not bad else [], f"{len(app)} app trips; {bad} failures", det, {"rasdhoo": found})


# ---------------------------------------------------------------- check 8 health
def check_health(cat, root=ROOT, cfg=None, **_):
    cfg = cfg or load_config(root)
    hf = read_csv(root / "data/reference/health/health_facilities.csv")
    old = {r["facility_name"]: r for r in read_csv(root / "data/archive/health_facilities_gemini_audited/health_facilities_gemini_audited.csv")}
    unv = [r["facility_name"] for r in hf if r["verification_status"] == "UNVERIFIED"]
    fixed = [r for r in hf if r["facility_name"] in old and (r["latitude"], r["longitude"]) != (old[r["facility_name"]]["latitude"], old[r["facility_name"]]["longitude"])]
    wrong = [r["facility_name"] for r in fixed if r["coordinate_quality"] != "island_centroid"]
    det = [f"UNVERIFIED rows ({len(unv)}): " + "; ".join(unv)]
    det.append(f"rows whose coordinates were replaced by the second pass (differ from the Gemini copy): {[r['facility_name'] for r in fixed]}")
    det.append(f"{'OK' if not wrong else 'FAIL'} coordinate_quality of fixed rows should be 'island_centroid'; found {Counter(r['coordinate_quality'] for r in fixed)}")
    det.append(f"coordinate_quality overall: {dict(Counter(r['coordinate_quality'] for r in hf))}; verification_status: {dict(Counter(r['verification_status'] for r in hf))}")
    det.append("note: 'LOCATED-BY-ISLAND-ONLY' is applied to 39 rows, including all fixed rows; it does not say the point is the polygon centroid. Either rename the 4 fixed rows to island_centroid or relax this expectation.")
    return result("8", "Health file", "fail" if wrong else "pass", ["health_facilities"] if not wrong else [], f"{len(unv)} UNVERIFIED rows; {len(wrong)} fixed rows lack island_centroid", det, {"unverified": unv, "fixed_rows_wrong_quality": wrong})


# ---------------------------------------------------------------- check 9 live (optional)
def check_live(cat, root=ROOT, live=False, **_):
    if not live:
        return result("9", "Live OSM checks (optional)", "skip", [], "not requested (use --live); nothing marked LIVE_CHECKED", [])
    import urllib.request

    hf = [r for r in read_csv(root / "data/reference/health/health_facilities.csv") if r["coordinate_quality"] == "source-provided" and r["facility_type"].lower().startswith("hospital")]
    det, ok_ids, bad = [], [], 0
    for r in hf:
        m = re.search(r"openstreetmap\.org/(node|way)/(\d+)", r["source_url"])
        if not m:
            det.append(f"{r['facility_name']}: no OSM id in source_url"); continue
        try:
            with urllib.request.urlopen(f"https://api.openstreetmap.org/api/0.6/{m.group(1)}/{m.group(2)}.json", timeout=8) as resp:
                el = json.loads(resp.read())["elements"][0]
        except Exception as e:  # network unavailable or object missing
            return result("9", "Live OSM checks (optional)", "skip", [], f"network/API unavailable ({type(e).__name__}); skipped cleanly", det)
        name = el.get("tags", {}).get("name", "")
        pos = (el.get("lat"), el.get("lon"))
        det.append(f"{r['facility_name']}: {m.group(1)}/{m.group(2)} exists; OSM name {name!r}; position {pos} vs row ({r['latitude']},{r['longitude']})")
        if pos[0] is not None and (abs(pos[0] - float(r["latitude"])) > 0.003 or abs(pos[1] - float(r["longitude"])) > 0.003):
            bad += 1
    return result("9", "Live OSM checks (optional)", "fail" if bad else "pass", [], f"{len(hf)} hospital rows checked on {__import__('datetime').date.today()}", det)


ALL = [check_catalog_schema, check_files, check_privacy, check_joins, check_coordinates, check_time, check_vessels, check_ferry, check_health, check_live]
