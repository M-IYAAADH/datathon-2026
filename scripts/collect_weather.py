"""Collect the August weather reference; use --offline to rebuild from immutable raw cache.

Run with .venv/bin/python scripts/collect_weather.py. No FollowMe code is imported or run.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import subprocess
import tempfile
import time
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb
from pyproj import Geod, Transformer
from shapely.geometry import Point, shape
from shapely.ops import transform, unary_union

ROOT = Path(__file__).resolve().parents[1]
UNITS = {
    "wind_speed_10m": "km/h", "wind_gusts_10m": "km/h",
    "wind_direction_10m": "°", "precipitation": "mm", "surface_pressure": "hPa",
    "wave_height": "m", "wave_direction": "°", "wave_period": "s",
    "wind_wave_height": "m", "swell_wave_height": "m", "swell_wave_period": "s",
}
POINT_FIELDS = [
    "point_id", "latitude", "longitude", "kind", "reason", "onemap_fcodes",
    "onemap_island_names", "land_by_onemap", "study_area_codes", "nearest_island_distance_m",
    "weather_returned_latitude", "weather_returned_longitude", "weather_status",
    "marine_returned_latitude", "marine_returned_longitude", "marine_status",
]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")


def read_json(path: Path):
    def invalid(value):
        raise ValueError(f"Non-standard JSON numeric constant: {value}")
    return json.loads(path.read_bytes(), parse_constant=invalid)


def hours_for(cfg: dict) -> list[datetime]:
    first = datetime.combine(date.fromisoformat(cfg["start_date"]), datetime.min.time(), timezone.utc)
    last = datetime.combine(date.fromisoformat(cfg["end_date"]) + timedelta(days=1), datetime.min.time(), timezone.utc)
    return [first + timedelta(hours=i) for i in range(int((last - first).total_seconds() // 3600))]


def build_points(cfg: dict, root: Path = ROOT) -> list[dict]:
    for prefix in ("study_area", "islands"):
        path = root / cfg[f"{prefix}_path"]
        if sha256(path) != cfg[f"{prefix}_sha256"]:
            raise ValueError(f"Reviewed geography hash mismatch: {path}")
    areas = read_json(root / cfg["study_area_path"])["features"]
    islands = read_json(root / cfg["islands_path"])["features"]
    area_shapes = [(f["properties"]["study_area_code"], shape(f["geometry"])) for f in areas]
    island_shapes = [shape(f["geometry"]) for f in islands]
    if any(not g.is_valid for _, g in area_shapes) or any(not g.is_valid for g in island_shapes):
        raise ValueError("Invalid reference polygon")
    study = unary_union([g for _, g in area_shapes])
    land = unary_union(island_shapes)
    forward = Transformer.from_crs(4326, 32643, always_xy=True).transform
    backward = Transformer.from_crs(32643, 4326, always_xy=True).transform
    land_m = transform(forward, land)
    points = []

    def add(point_id, lat, lon, kind, reason, fcodes="", names=""):
        lat, lon = round(lat, 7), round(lon, 7)
        p = Point(lon, lat)
        distance = transform(forward, p).distance(land_m)
        on_land = land.covers(p)
        if kind == "grid" and on_land and distance > cfg["near_island_distance_m"]:
            return
        points.append(dict(point_id=point_id, latitude=lat, longitude=lon, kind=kind,
                           reason=reason, onemap_fcodes=fcodes, onemap_island_names=names,
                           land_by_onemap=on_land,
                           study_area_codes=";".join(code for code, g in area_shapes if g.covers(p)),
                           nearest_island_distance_m=round(distance, 2)))

    west, south, east, north = study.bounds
    step = cfg["grid_spacing_degrees"]
    origin_lat, origin_lon = cfg["grid_origin_degrees"]
    for iy in range(math.ceil((south - origin_lat) / step), math.floor((north - origin_lat) / step) + 1):
        for ix in range(math.ceil((west - origin_lon) / step), math.floor((east - origin_lon) / step) + 1):
            lat, lon = origin_lat + iy * step, origin_lon + ix * step
            add(f"grid_{iy:03d}_{ix:03d}", lat, lon, "grid",
                "0.25 degree grid in study bounding box; sea proxy or within configured distance of study island")
    endpoints = {}
    for spec in cfg["route_points"]:
        matches = [f for f in islands if f["properties"]["islandName"] == spec["island_name"]
                   and f["properties"]["atoll"] == spec["atoll"]]
        if len(matches) != 1:
            raise ValueError(f"Expected one OneMap polygon for {spec['id']}, got {len(matches)}")
        feature = matches[0]
        geom = transform(forward, shape(feature["geometry"]))
        p = geom.centroid
        method = "projected polygon centroid"
        if not geom.covers(p):
            p = geom.representative_point()
            method = "projected polygon representative point (centroid outside)"
        lon, lat = backward(p.x, p.y)
        fcode = feature["properties"]["FCODE"]
        endpoints[spec["id"]] = (lon, lat, fcode, spec["island_name"])
        add(f"route_{spec['id']}", lat, lon, "route_endpoint",
            f"{spec['island_name']} route-area endpoint/additional island: OneMap {fcode}, {method}; not a harbour", fcode, spec["island_name"])
    geod = Geod(ellps="WGS84")
    for a, b in zip(cfg["corridor"], cfg["corridor"][1:]):
        pa, pb = endpoints[a], endpoints[b]
        lon, lat = geod.npts(pa[0], pa[1], pb[0], pb[1], 1)[0]
        add(f"route_mid_{a}_{b}", lat, lon, "route_midpoint",
            f"WGS84 geodesic midpoint of OneMap-derived {a}–{b} island-area endpoints; not a sailed track",
            f"{pa[2]};{pb[2]}", f"{pa[3]};{pb[3]}")
    return sorted(points, key=lambda p: p["point_id"])


def request_parameters(cfg: dict, source: str, batch: list[dict]) -> dict:
    spec = cfg["sources"][source]
    params = {
        "latitude": ",".join(str(p["latitude"]) for p in batch),
        "longitude": ",".join(str(p["longitude"]) for p in batch),
        "start_date": cfg["start_date"], "end_date": cfg["end_date"],
        "timezone": cfg["request_timezone"], "timeformat": "iso8601",
        "hourly": ",".join(spec["variables"]), "models": spec["model"],
        "wind_speed_unit": cfg["request"]["wind_speed_unit"],
        "cell_selection": cfg["request"]["cell_selection"],
    }
    if source == "weather":
        params.update(precipitation_unit=cfg["request"]["precipitation_unit"], elevation=",".join("nan" for _ in batch))
    else:
        params["length_unit"] = "metric"
    return params


def fetch_batch(cfg: dict, source: str, batch: list[dict], raw: Path, offline: bool) -> dict:
    """Cache exact response bodies and request/transport metadata; never execute response content."""
    params = request_parameters(cfg, source, batch)
    endpoint = cfg["sources"][source]["endpoint"]
    url = endpoint + "?" + urllib.parse.urlencode(params)
    key = hashlib.sha256(url.encode()).hexdigest()[:20]
    stem = f"{source}_{key}"
    receipt_path = raw / f"{stem}.request.json"
    if receipt_path.exists():
        receipt = read_json(receipt_path)
        if receipt["url"] != url or receipt["point_ids"] != [p["point_id"] for p in batch]:
            raise ValueError("Cache request identity mismatch")
        for attempt in receipt["attempts"]:
            if sha256(raw / attempt["file"]) != attempt["sha256"]:
                raise ValueError("Raw cache hash mismatch")
        if receipt["success"] or offline:
            return receipt
    elif offline:
        raise FileNotFoundError(f"Offline cache is missing: {receipt_path}")
    else:
        receipt = dict(source=source, endpoint=endpoint, parameters=params, url=url,
                       point_ids=[p["point_id"] for p in batch], attempts=[], success=False)
    policy = cfg["request"]
    # A later explicit online rerun may retry a failed request. Prior bodies are retained.
    for retry in range(policy["max_attempts"]):
        time.sleep(policy["sleep_seconds"])
        attempt_no = len(receipt["attempts"]) + 1
        body_path = raw / f"{stem}.attempt{attempt_no:02d}.json"
        header_path = raw / f"{stem}.attempt{attempt_no:02d}.headers.txt"
        started = datetime.now(timezone.utc).isoformat(timespec="seconds")
        cmd = ["curl", "--silent", "--show-error", "--proto", "=https", "--tlsv1.2",
               "--max-time", str(policy["timeout_seconds"]), "--max-filesize", str(policy["max_response_bytes"]),
               "--user-agent", policy["user_agent"], "--dump-header", str(header_path),
               "--output", str(body_path), "--write-out", "%{http_code}", url]
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if not body_path.exists():
            body_path.write_bytes(b"")
        status = int(result.stdout) if result.stdout.isdigit() else 0
        attempt = dict(file=body_path.name, sha256=sha256(body_path), bytes=body_path.stat().st_size,
                       http_status=status, curl_exit_code=result.returncode, retrieved_at_utc=started,
                       completed_at_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                       headers_file=header_path.name, transport_error=result.stderr.strip()[:1000])
        receipt["attempts"].append(attempt)
        error = None
        if result.returncode == 0 and status == 200:
            try:
                response = read_json(body_path)
                if isinstance(response, dict) and response.get("error"):
                    error = str(response.get("reason", "API error"))[:1000]
                elif not isinstance(response, (dict, list)):
                    error = "Unexpected JSON root"
                else:
                    receipt.update(success=True, response_file=body_path.name)
            except (ValueError, UnicodeError) as exc:
                error = f"Invalid JSON response: {exc}"
        else:
            try:
                response = read_json(body_path)
                error = str(response.get("reason", f"HTTP {status}"))[:1000] if isinstance(response, dict) else f"HTTP {status}"
            except (ValueError, UnicodeError):
                error = f"HTTP {status}; curl exit {result.returncode}"
        receipt["error"] = error
        write_json(receipt_path, receipt)
        print(f"{source}: {batch[0]['point_id']} … {batch[-1]['point_id']}: HTTP {status}, {body_path.stat().st_size} bytes", flush=True)
        if receipt["success"]:
            return receipt
        if 400 <= status < 500 and status != 429:
            break
        delay = policy["backoff_seconds"] * 2 ** retry
        for line in header_path.read_text(errors="replace").splitlines() if header_path.exists() else []:
            if line.lower().startswith("retry-after:"):
                value = line.split(":", 1)[1].strip()
                try:
                    wait = float(value)
                except ValueError:
                    try:
                        wait = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
                    except (ValueError, TypeError, OverflowError):
                        continue  # Untrusted malformed header: retain the configured backoff.
                if not math.isfinite(wait):
                    continue
                delay = max(delay, wait)
        if retry + 1 < policy["max_attempts"]:
            print(f"Retry after {delay:.0f}s: {receipt['error']}", flush=True)
            time.sleep(max(0, delay))
    return receipt


def finite_number(value, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"Expected finite number for {label}")
    return float(value)


def parse_payload(payload: dict, variables: list[str], expected_hours: list[datetime]) -> dict:
    """Validate untrusted structure, units and UTC times; preserve missing values verbatim."""
    if not isinstance(payload, dict) or payload.get("error"):
        raise ValueError("API error or invalid location response")
    if payload.get("utc_offset_seconds") != 0:
        raise ValueError("Expected UTC response (utc_offset_seconds=0)")
    lat = finite_number(payload.get("latitude"), "returned latitude")
    lon = finite_number(payload.get("longitude"), "returned longitude")
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("Returned coordinates outside WGS84 bounds")
    hourly, units = payload.get("hourly"), payload.get("hourly_units")
    if not isinstance(hourly, dict) or not isinstance(units, dict) or units.get("time") != "iso8601":
        raise ValueError("Missing hourly structure or incompatible time unit")
    times = hourly.get("time")
    if not isinstance(times, list):
        raise ValueError("Missing time array")
    expected = set(expected_hours)
    parsed_times = []
    for text in times:
        if not isinstance(text, str):
            raise ValueError("Non-string time")
        stamp = datetime.fromisoformat(text)
        if stamp.tzinfo is not None and stamp.utcoffset() != timedelta(0):
            raise ValueError("Expected UTC timestamp, not a nonzero offset")
        stamp = stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp.astimezone(timezone.utc)
        if stamp not in expected or stamp in parsed_times:
            raise ValueError(f"Unexpected/duplicate hour {text}")
        parsed_times.append(stamp)
    if parsed_times != sorted(parsed_times):
        raise ValueError("Unordered hours")
    values = {}
    absent = []
    for var in variables:
        if var not in hourly:
            absent.append(var)
            values[var] = [None] * len(times)
            continue
        series = hourly[var]
        if not isinstance(series, list) or len(series) != len(times):
            raise ValueError(f"Mismatched array for {var}")
        # Unsupported ERA5-Ocean components are explicitly all-null with unit 'undefined'.
        # Preserve those nulls and the literal returned unit, without interpreting any value.
        if units.get(var) != UNITS[var] and not (units.get(var) == "undefined" and all(v is None for v in series)):
            raise ValueError(f"Unexpected unit for {var}: {units.get(var)!r}; expected {UNITS[var]!r}")
        values[var] = [None if v is None else finite_number(v, var) for v in series]
    by_time = {stamp: {var: values[var][i] for var in variables} for i, stamp in enumerate(parsed_times)}
    status = "ok"
    if absent or len(times) != len(expected_hours) or any(v is None for row in by_time.values() for v in row.values()):
        status = "partial_nulls"
    if not any(v is not None for row in by_time.values() for v in row.values()):
        status = "all_null"
    return dict(latitude=lat, longitude=lon, by_time=by_time, units=units, status=status,
                absent_variables=absent, response_hour_count=len(times),
                metadata={k: v for k, v in payload.items() if k != "hourly"})


def parse_batch(payload, count: int, variables: list[str], hours: list[datetime]) -> list[dict]:
    items = payload if isinstance(payload, list) else [payload]
    if len(items) != count:
        raise ValueError(f"Expected {count} location responses, got {len(items)}")
    if any("location_id" in p for p in items if isinstance(p, dict)):
        # Open-Meteo omits the default location_id=0 in its actual multi-point JSON.
        ids = [p.get("location_id", 0) if isinstance(p, dict) else None for p in items]
        if any(type(i) is not int for i in ids) or set(ids) != set(range(count)):
            raise ValueError("Invalid or duplicate batch location_id")
        items = sorted(items, key=lambda p: p.get("location_id", 0))
    return [parse_payload(p, variables, hours) for p in items]


def hourly_rows(points: list[dict], cfg: dict, parsed: dict, references: dict) -> list[dict]:
    rows = []
    local_zone = ZoneInfo(cfg["local_timezone"])
    for p in points:
        for stamp in hours_for(cfg):
            row = dict(point_id=p["point_id"], time_utc=stamp,
                       time_local=stamp.astimezone(local_zone).replace(tzinfo=None),
                       local_timezone=cfg["local_timezone"])
            for source, spec in cfg["sources"].items():
                data = parsed.get((source, p["point_id"]))
                hour = data["by_time"].get(stamp) if data else None
                row.update({var: hour.get(var) if hour else None for var in spec["variables"]})
                for axis in ("latitude", "longitude"):
                    row[f"{source}_returned_{axis}"] = data[axis] if data else None
                flag = "request_failed" if data is None else ("hour_missing" if hour is None else data["status"])
                row[f"{source}_source_flag"] = flag
                row[f"{source}_raw_file"] = references.get((source, p["point_id"]))
                row[f"{source}_requested_model"] = spec["model"]
            rows.append(row)
    return rows


def materialize(rows: list[dict], cfg: dict, out: Path, points: list[dict] | None = None) -> dict:
    """DuckDB performs deterministic typed export, daily aggregation and validation."""
    con = duckdb.connect(":memory:")
    con.execute("SET threads=1")
    con.execute("SET TimeZone='UTC'")
    keys = list(rows[0])
    types = {}
    for key in keys:
        types[key] = ("TIMESTAMPTZ" if key == "time_utc" else "TIMESTAMP" if key == "time_local"
                      else "DOUBLE" if key in UNITS or key.endswith(("_latitude", "_longitude")) else "VARCHAR")
    con.execute("CREATE TABLE hourly (" + ",".join(f"{key} {types[key]}" for key in keys) + ")")
    # Bulk-load with explicit types; per-row Python TIMESTAMPTZ binding is very slow.
    # This is a temporary derived staging file, not a source response or retained dataset.
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".jsonl", prefix=".weather-hourly-", dir=out) as staging:
        for row in rows:
            staging.write(json.dumps(row, default=lambda value: value.isoformat(), ensure_ascii=False, allow_nan=False) + "\n")
        staging.flush()
        con.execute("INSERT INTO hourly SELECT * FROM read_json(?, columns=?, format='newline_delimited')", [staging.name, types])
    con.execute("COPY (SELECT * FROM hourly ORDER BY point_id,time_utc) TO ? (FORMAT PARQUET, COMPRESSION ZSTD)", [str(out / "weather_hourly.parquet")])
    threshold = cfg["review_thresholds"]
    # Count observed hours separately: an entirely missing variable has a NULL exceedance count.
    con.execute("""CREATE TABLE daily AS SELECT point_id, CAST(time_local AS DATE) AS date_local,
        count(*) AS hours_in_window,
        count(wind_speed_10m) AS wind_observed_hours,
        count(wind_gusts_10m) AS gust_observed_hours,
        count(wave_height) AS wave_observed_hours,
        max(wind_speed_10m) AS max_wind_speed_10m_kmh,
        avg(wind_speed_10m) AS mean_wind_speed_10m_kmh,
        max(wind_gusts_10m) AS max_wind_gusts_10m_kmh,
        max(wave_height) AS max_wave_height_m,
        sum(CASE WHEN wind_speed_10m IS NULL THEN NULL WHEN wind_speed_10m > ? THEN 1 ELSE 0 END) AS hours_wind_above_threshold,
        sum(CASE WHEN wind_gusts_10m IS NULL THEN NULL WHEN wind_gusts_10m > ? THEN 1 ELSE 0 END) AS hours_gust_above_threshold,
        sum(CASE WHEN wave_height IS NULL THEN NULL WHEN wave_height > ? THEN 1 ELSE 0 END) AS hours_wave_above_threshold
        FROM hourly GROUP BY point_id, date_local""", [threshold["wind_speed_10m_kmh"], threshold["wind_gusts_10m_kmh"], threshold["wave_height_m"]])
    con.execute("COPY (SELECT * FROM daily ORDER BY point_id,date_local) TO ? (FORMAT CSV, HEADER)", [str(out / "daily_summary.csv")])
    coverage = {}
    for var in UNITS:
        count, nulls, minimum, maximum = con.execute(f"SELECT count({var}),count(*)-count({var}),min({var}),max({var}) FROM hourly").fetchone()
        coverage[var] = dict(non_null_count=count, null_count=nulls, minimum=minimum, maximum=maximum, unit=UNITS[var])
    outliers = []
    for var, bounds in cfg["plausible_ranges"].items():
        if var == "comment":
            continue
        for point_id, stamp, value in con.execute(f"SELECT point_id,CAST(time_utc AS VARCHAR),{var} FROM hourly WHERE {var} < ? OR {var} > ? ORDER BY point_id,time_utc", bounds).fetchall():
            outliers.append(dict(variable=var, point_id=point_id, time_utc=datetime.fromisoformat(stamp).isoformat(), value=value, plausible_range=bounds))
    duplicates = con.execute("SELECT count(*) FROM (SELECT point_id,time_utc FROM hourly GROUP BY ALL HAVING count(*)>1)").fetchone()[0]
    counts = {p: n for p, n in con.execute("SELECT point_id,count(*) FROM hourly GROUP BY point_id ORDER BY point_id").fetchall()}
    scope_ids = [p["point_id"] for p in points if p["study_area_codes"]] if points is not None else sorted(counts)
    con.execute("CREATE TABLE rank_scope (point_id VARCHAR)")
    if scope_ids:
        con.executemany("INSERT INTO rank_scope VALUES (?)", [[pid] for pid in scope_ids])
    rankings, context_rankings = {}, {}
    for label, var in (("wind", "wind_speed_10m"), ("wave", "wave_height")):
        # Maxima are unaffected by points sharing returned cells; these are not area-weighted means.
        rankings[label] = [dict(date_local=str(day), maximum=value) for day, value in con.execute(
            f"SELECT CAST(time_local AS DATE) AS day,max({var}) AS peak FROM hourly JOIN rank_scope USING(point_id) WHERE {var} IS NOT NULL AND month(time_local)=8 GROUP BY day ORDER BY peak DESC,day LIMIT 5").fetchall()]
        context_rankings[label] = [dict(date_local=str(day), maximum=value) for day, value in con.execute(
            f"SELECT CAST(time_local AS DATE) AS day,max({var}) AS peak FROM hourly WHERE {var} IS NOT NULL AND month(time_local)=8 GROUP BY day ORDER BY peak DESC,day LIMIT 5").fetchall()]
    result = dict(row_count=len(rows), hours_expected_per_point=len(hours_for(cfg)), point_hour_counts=counts,
                  duplicate_point_hour_groups=duplicates, variables=coverage, outliers=outliers, top_days=rankings,
                  top_days_bbox_context=context_rankings,
                  ranking_scope=dict(rule="Query points covered by at least one reviewed study-area polygon", point_ids=scope_ids),
                  daily_row_count=con.execute("SELECT count(*) FROM daily").fetchone()[0],
                  time_utc_min=rows[0]["time_utc"].isoformat(), time_utc_max=rows[-1]["time_utc"].isoformat())
    con.close()
    if duplicates or any(n != len(hours_for(cfg)) for n in counts.values()):
        raise ValueError("Failed point-hour cardinality validation")
    return result


def write_documentation(cfg: dict, points: list[dict], validation: dict, failures: list[dict], provenance: dict, path: Path):
    lines = ["# August 2025 hourly wind and wave reference", "",
        f"Config version: `{cfg['config_version']}`. {len(points)} points; {validation['row_count']:,} point-hours; "
        f"{validation['hours_expected_per_point']} hours per point. {len(failures)} failed point/source pairs. "
        f"Duplicate point-hours: {validation['duplicate_point_hour_groups']}. Diagnostic outliers: {len(validation['outliers'])}.", "",
        "## Sources, models and attribution", "",
        "[Weather data by Open-Meteo.com](https://open-meteo.com/), using ECMWF IFS historical analysis for atmospheric variables and NOAA NCEP GFS Wave archived forecasts for marine variables. "
        "Modelled history is not an observed sea state. The requested models are explicit; model/dataset names actually reported in each response (or their absence) and all returned metadata are in `provenance.json`. "
        "Do not interpret a requested model identifier as independent API confirmation.", "",
        "- [Historical Weather API](https://open-meteo.com/en/docs/historical-weather-api): `https://archive-api.open-meteo.com/v1/archive`, `models=ecmwf_ifs`, about 9 km. "
        "IFS was chosen because Open-Meteo's documented ERA5 variable matrix does not provide gusts.",
        "- [Marine Weather API](https://open-meteo.com/en/docs/marine-weather-api): `https://marine-api.open-meteo.com/v1/marine`, `models=ncep_gfswave025`, documented 0.25° (about 25 km), hourly, since June 2024. "
        "The identifier is listed in [Open-Meteo's official marine model inventory](https://github.com/open-meteo/open-data#marine-wave-models). "
        "These are archived model forecasts, not marine reanalysis; the response does not identify the contributing run/lead time.",
        "- [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) and [Open-Meteo licence](https://open-meteo.com/en/licence). "
        "The [free API terms](https://open-meteo.com/en/terms) restrict service use to non-commercial activity (600 calls/minute, 5,000/hour, 10,000/day; multi-location/long requests can count as multiple calls). "
        "Credit Open-Meteo, ECMWF and NOAA NCEP. Changes here: point selection, joining, local-time derivation and daily aggregation; source values are not adjusted.",
        "- Island coordinates inherit [OneMap's data usage licence](https://readme.onemap.mv/pdf/Data%20Usage%20License.pdf); "
        "study-area geometry inherits Government of Maldives/OCHA COD-AB CC BY-IGO attribution. See [geography review](followme_geography.md).", "",
        "## Query points and returned cells", "",
        cfg["grid_filter_note"], "",
        f"{sum(p['kind']=='grid' for p in points)} grid points, {sum(p['kind']=='route_endpoint' for p in points)} island-area endpoints/additional islands, "
        f"and {sum(p['kind']=='route_midpoint' for p in points)} corridor midpoints. Grid origin is 0°/0°, spacing 0.25°, clipped to the unexpanded study union bounds. "
        "Sea means outside the supplied OneMap footprints, including possible lagoon/reef water; no bathymetry or global land mask was fetched. "
        "All points lying on a study-island footprint are at distance zero and therefore satisfy the configured 5 km rule. "
        "`study_area_codes` is empty for bounding-box context points outside the administrative union; no legal marine boundary is inferred.", "",
        cfg["endpoint_method"], "",
        "The corridor is Malé–Rasdhoo–Ukulhas–Mathiveri, with all four endpoints and three consecutive-leg midpoints, plus Thoddoo, Maafushi and Hulhumalé. "
        "Exact names and atolls must match one OneMap polygon. `weather_points.csv` records FCODEs, coordinates, inclusion reason, footprint land status, "
        "distance in UTM 43N metres, administrative memberships, returned cells and per-source status. Input geometry SHA-256s are pinned in the config and provenance.", "",
        "`cell_selection=sea` is explicit for both sources. Returned coordinates are retained separately for weather and marine, in both the points table and every hourly row. "
        "Points may share a returned grid cell, so they are not independent samples. Land endpoints remain island-area proxies even when snapped offshore. "
        "An all-null marine response at a land endpoint is labelled `all_null_land_point` in the points table; it is not replaced by a guessed offshore observation.", "",
        "## Time and units", "",
        "Request: `start_date=2025-08-01`, `end_date=2025-08-31`, `timezone=GMT`, `timeformat=iso8601`; response UTC offset must be zero. "
        "The 744-hour window is 2025-08-01 00:00 UTC through 2025-08-31 23:00 UTC inclusive. "
        "`time_utc` is Parquet TIMESTAMPTZ. `time_local` is a local wall-clock TIMESTAMP derived in Python with `ZoneInfo('Indian/Maldives')`, "
        "with a separate `local_timezone` column; UTC+5, no DST. It runs from 1 August 05:00 through 1 September 04:00 local. "
        "Thus it does not contain the first five local hours of 1 August. No extra dates were collected; confirm GPS timezone before joining.", "",
        "DuckDB may display TIMESTAMPTZ in the reader's session timezone. Use `SET TimeZone='UTC'` to display `time_utc` in UTC, or `time_utc AT TIME ZONE 'UTC'` "
        "for a UTC wall-clock value. The stored timestamp represents a UTC instant regardless of display settings.", "",
        "`wind_speed_unit=kmh` is explicitly sent to both endpoints; `precipitation_unit=mm`, `length_unit=metric`, and atmospheric `elevation=nan` "
        "(disable elevation downscaling) are also explicit where applicable. Parser rejects incompatible units, except that `undefined` is retained for explicitly all-null unsupported series. All `hourly_units` returned by the API are recorded per location in provenance, "
        "including time. Wind and wave directions indicate where they come from (degrees clockwise from north). Precipitation is the preceding-hour sum; wave height is significant wave height, not an individual maximum wave.", "",
        "## Coverage and nulls", "", "| Variable | Unit | Non-null hours | Null hours | Minimum | Maximum |", "| --- | --- | ---: | ---: | ---: | ---: |"]
    for var, stats in validation["variables"].items():
        lines.append(f"| {var} | {stats['unit']} | {stats['non_null_count']} | {stats['null_count']} | {stats['minimum']} | {stats['maximum']} |")
    lines += ["", "Expected rows and actual source coverage are different checks. Per-point source time-array and non-null counts are in `validation.json` under `source_point_coverage`. The complete point-hour scaffold preserves failed/missing hours as NULL. "
              "No gaps are interpolated, forward-filled or replaced with zero. Source flags distinguish `ok`, `partial_nulls`, `all_null`, `hour_missing` and `request_failed`. "
              "A source-level `partial_nulls` flag describes that location's entire response; individual variable nulls remain directly visible. "
              "`weather_raw_file`/`marine_raw_file` trace each row to its original response; batch `point_ids` and `location_id` map the response to its query point.", ""]
    for source in cfg["sources"]:
        cells = {(p.get(f"{source}_returned_latitude"), p.get(f"{source}_returned_longitude")) for p in points if p.get(f"{source}_returned_latitude") is not None}
        lines.append(f"- {source}: {len(cells)} unique returned cells; statuses: " + json.dumps({s: sum(p[f"{source}_status"] == s for p in points) for s in sorted({p[f"{source}_status"] for p in points})}) + ".")
    lines += ["", "Failed point/source requests:", ""]
    lines += [f"- `{f['point_id']}` / {f['source']}: {f['reason']}" for f in failures] or ["None."]
    lines += ["", "## Daily summaries and highest days", "",
        "`daily_summary.csv` groups by **Maldives local day**, with counts of observed wind/gust/wave hours. "
        "The first day has 19 hours and the 1 September spillover has 5; all intervening days have 24. "
        "`hours_in_window` and per-variable observed-hour columns expose incomplete days. Threshold counts use strict `>` and count only observed hours; an entirely missing variable produces NULL, not zero. "
        "Thresholds in `config/weather.json` are provisional review thresholds, **not safety limits**: "
        f"wind {cfg['review_thresholds']['wind_speed_10m_kmh']} km/h, gust {cfg['review_thresholds']['wind_gusts_10m_kmh']} km/h, significant wave height {cfg['review_thresholds']['wave_height_m']} m.", "",
        f"Rankings below use the maximum over the {len(validation['ranking_scope']['point_ids'])} query points covered by the reviewed study-area polygons per local August day (not an area-weighted mean). "
        f"The other {len(points)-len(validation['ranking_scope']['point_ids'])} bounding-box context points remain in the dataset and daily summaries; their all-point rankings are retained separately as `top_days_bbox_context` in validation. "
        "Repeated returned cells cannot alter the maximum. Ties sort by date. Partial boundary days use available hours only; 1 September is excluded.", ""]
    for label, unit in (("wind", "km/h"), ("wave", "m")):
        lines += [f"### Five highest {label} days", "", f"| Local date | Maximum ({unit}) |", "| --- | ---: |"]
        lines += [f"| {d['date_local']} | {d['maximum']} |" for d in validation["top_days"][label]]
        if not validation["top_days"][label]:
            lines.append("| No available values | NULL |")
        lines.append("")
    lines += ["## Files, cache and validation", "",
        "All dataset files are under `data/reference/weather/`: `weather_hourly.parquet`, `weather_points.csv`, `daily_summary.csv`, "
        "`provenance.json`, `validation.json`, and `raw/`. Raw response bodies are saved byte-for-byte, including error responses, "
        "with HTTP headers and request receipts. Receipts include UTC retrieval/completion timestamps, exact URL/parameters, attempts and SHA-256. "
        "Provenance records raw and output row counts/hashes, every returned unit, full returned location metadata and model fields if reported. "
        "`validation.json` contains point-hour counts, non-null ranges, failures and every outlier. Outliers are retained unchanged.", "",
        "Collection diagnostics are retained in the same raw cache: initial sandbox DNS failures, multi-coordinate elevation parameter errors, an unsupported `gfs_wave` model alias, "
        "and ERA5-Ocean responses whose three component series were all null with unit `undefined`. These are not sources for the final table. "
        "`requests` in provenance identifies final-source batches, while `unused_cached_requests` records earlier requests separately, including their original parameters, timestamps and reasons.", "",
        "Five coordinates per request, serial requests, 3 seconds minimum between calls, 90 second timeout, at most four attempts per request per online run, "
        "5/10/20 second exponential retry backoff and `Retry-After` honoured for rate limiting. Permanent 4xx errors are not retried. "
        "Successful cache entries are immutable and reused; failed bodies are retained when an online rerun retries. Offline mode never invokes curl. "
        "JSON is treated as data: no evaluation, shell interpolation, response-directed URLs or executable content. Arrays, units, times, finite numeric types, "
        "location IDs and coordinates are validated. A malformed batch is reported as failed and remains null in outputs.", "",
        "```bash", ".venv/bin/python scripts/collect_weather.py --config config/weather.json", "# Exact offline rebuild from this cache:",
        ".venv/bin/python scripts/collect_weather.py --config config/weather.json --offline", "# Offline fixture tests:",
        ".venv/bin/python -m pytest tests/test_weather.py -q", "```", "",
        "Run commands from `datathon-2026/`. This collector never invokes FollowMe or writes its outputs. Existing dependencies suffice (Python + DuckDB + Shapely + pyproj); no new package or API key is required. "
        "Identical config/geography/cache inputs produce byte-identical primary outputs and deterministic documentation/validation. "
        "Provenance uses stored retrieval timestamps, not the rebuild clock.", "",
        "## Known limits and team questions", "",
        "- Model analysis, reanalysis or archived forecasts are not an observed sea state, actual disruption, or proof that a vessel sailed. Wind/wave cells are coarse around atolls, reefs, channels and sheltered lagoons; "
        "the 0.25° query grid cannot improve the approximately 25 km marine model resolution. Source API preprocessing may itself resample model data; this collector does no gap filling.",
        "- Polygon snapshot was downloaded in October 2026; historical August 2025 shorelines/reclamation are not independently confirmed. Island-area points are not actual jetties; "
        "geodesic midpoints do not define navigation routes. No route/GPS matching or safety classification is performed.",
        "- Confirm whether the GPS window is defined in UTC or Maldives local time. A full local-August join would additionally need 31 July 19:00–23:00 UTC, outside this requested collection.",
        "- Ask organisers/crew which review thresholds and vessel classes are meaningful, and which harbours/sailed routes should replace island proxies for later exposure matching.",
        "- Confirm whether bounding-box context sea points outside the administrative union should enter later aggregate analysis, and whether approximately 25 km archived wave forecasts are sufficient. "
        "Organisers should confirm that archived GFS forecasts are appropriate for retrospective exposure; ERA5-Ocean at this endpoint lacked component variables.",
        "- Confirm that datathon use and any subsequent reuse meet Open-Meteo's non-commercial free-tier terms; retain attribution if derived results are shared.", ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "config/weather.json")
    parser.add_argument("--offline", action="store_true", help="Rebuild only; never make network calls")
    args = parser.parse_args()
    cfg = read_json(args.config)
    if cfg["request_timezone"] != "GMT" or cfg["request"]["wind_speed_unit"] != "kmh":
        raise ValueError("This collection requires GMT and explicit kmh")
    points = build_points(cfg)
    out = ROOT / cfg["output_dir"]
    raw = out / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    hours = hours_for(cfg)
    parsed, references, failures, receipts, locations = {}, {}, [], [], []
    for source, spec in cfg["sources"].items():
        for offset in range(0, len(points), cfg["request"]["batch_size"]):
            batch = points[offset:offset + cfg["request"]["batch_size"]]
            receipt = fetch_batch(cfg, source, batch, raw, args.offline)
            receipts.append(receipt)
            if receipt["success"]:
                path = raw / receipt["response_file"]
                try:
                    data = parse_batch(read_json(path), len(batch), spec["variables"], hours)
                except (ValueError, TypeError) as exc:
                    error = f"Response validation failed: {exc}"
                else:
                    for p, item in zip(batch, data):
                        key = source, p["point_id"]
                        parsed[key] = item
                        references[key] = str(path.relative_to(ROOT))
                        metadata = item["metadata"]
                        locations.append(dict(source=source, point_id=p["point_id"], raw_file=references[key],
                                              response_hours=item["response_hour_count"], absent_variables=item["absent_variables"],
                                              hourly_units=item["units"], returned_metadata=metadata,
                                              reported_model_dataset_fields={k: v for k, v in metadata.items() if "model" in k.lower() or "dataset" in k.lower()}))
                    continue
            else:
                error = receipt.get("error", "Request failed")
            for p in batch:
                failures.append(dict(source=source, point_id=p["point_id"], reason=error))
                if receipt.get("response_file"):
                    references[source, p["point_id"]] = str((raw / receipt["response_file"]).relative_to(ROOT))
    for p in points:
        for source in cfg["sources"]:
            item = parsed.get((source, p["point_id"]))
            p[f"{source}_status"] = item["status"] if item else "request_failed"
            if source == "marine" and item and item["status"] == "all_null" and p["land_by_onemap"]:
                p[f"{source}_status"] = "all_null_land_point"
            for axis in ("latitude", "longitude"):
                p[f"{source}_returned_{axis}"] = item[axis] if item else None
    with (out / "weather_points.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=POINT_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(points)
    rows = hourly_rows(points, cfg, parsed, references)
    validation = materialize(rows, cfg, out, points)
    validation["failures"] = failures
    validation["source_response_hours"] = {s: sum(d["response_hour_count"] for (source, _), d in parsed.items() if source == s) for s in cfg["sources"]}
    validation["source_point_coverage"] = {
        source: {
            p["point_id"]: {
                "returned_time_count": parsed[source, p["point_id"]]["response_hour_count"] if (source, p["point_id"]) in parsed else 0,
                "non_null_hours": {
                    var: sum(row[var] is not None for row in parsed.get((source, p["point_id"]), {}).get("by_time", {}).values())
                    for var in spec["variables"]
                },
            } for p in points
        } for source, spec in cfg["sources"].items()
    }
    write_json(out / "validation.json", validation)
    provenance = dict(config_version=cfg["config_version"], config_file=str(args.config.resolve().relative_to(ROOT)),
                      config_sha256=sha256(args.config), sources=cfg["sources"], request_timezone="GMT",
                      local_timezone=cfg["local_timezone"], query_point_count=len(points),
                      retrieval_timestamps_utc=sorted({a["retrieved_at_utc"] for r in receipts for a in r["attempts"]}),
                      requests=receipts, locations=locations, failures=failures,
                      licence="CC BY 4.0", licence_url="https://creativecommons.org/licenses/by/4.0/",
                      attribution="Weather data by Open-Meteo.com (https://open-meteo.com/); ECMWF IFS and NOAA NCEP GFS Wave. Derived point selection, joins, local time and summaries by the Datathon team.",
                      unused_cache_attribution="Diagnostic ERA5-Ocean responses: Open-Meteo.com and ECMWF/Copernicus Climate Change Service (C3S). These values are not used in the final table.",
                      free_tier_terms="Non-commercial use only; https://open-meteo.com/en/terms",
                      geography_attribution="Government of Maldives / OCHA COD-AB CC BY-IGO; OneMap Geomatics Department, Ministry of Infrastructure, Housing and Urban Development, OneMap Data Usage License. See docs/followme_geography.md.",
                      model_reporting_note="Requested model IDs are recorded separately from returned fields. Empty reported_model_dataset_fields means the API did not independently report a model/dataset name.",
                      inputs={cfg[k]: {"sha256": sha256(ROOT / cfg[k])} for k in ("study_area_path", "islands_path")})
    active_urls = {r["url"] for r in receipts}
    provenance["unused_cached_requests"] = [r for p in sorted(raw.glob("*.request.json")) if (r := read_json(p))["url"] not in active_urls]
    doc = ROOT / cfg["documentation_path"]
    write_documentation(cfg, points, validation, failures, provenance, doc)
    file_rows = {"weather_hourly.parquet": len(rows), "weather_points.csv": len(points), "daily_summary.csv": validation["daily_row_count"]}
    files = {}
    for path in sorted(raw.iterdir()):
        if not path.is_file():
            continue
        info = dict(sha256=sha256(path), bytes=path.stat().st_size)
        if ".attempt" in path.name and path.suffix == ".json":
            try:
                payload = read_json(path)
                payloads = payload if isinstance(payload, list) else [payload]
                info["row_count"] = sum(len(p.get("hourly", {}).get("time", [])) for p in payloads if isinstance(p, dict))
            except (ValueError, UnicodeError):
                info["row_count"] = 0
        files[str(path.relative_to(ROOT))] = info
    for name in ("weather_hourly.parquet", "weather_points.csv", "daily_summary.csv", "validation.json"):
        path = out / name
        files[str(path.relative_to(ROOT))] = dict(sha256=sha256(path), bytes=path.stat().st_size, row_count=file_rows.get(name))
    files[str(doc.relative_to(ROOT))] = dict(sha256=sha256(doc), bytes=doc.stat().st_size)
    provenance["files"] = files  # This manifest cannot include its own self-referential hash.
    write_json(out / "provenance.json", provenance)
    print(json.dumps({"points": len(points), "rows": len(rows), "failures": len(failures), "outliers": len(validation["outliers"]), "top_days": validation["top_days"]}, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
