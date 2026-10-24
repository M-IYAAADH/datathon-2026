"""Orchestration: load cleaned inputs, classify devices, detect stops, infer services, aggregate."""
from __future__ import annotations

import csv
import json
import math
import os
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb
import numpy as np
import shapely

from . import geo
from .classify import classify_device
from .clock import CLASS_CODES
from .journeys import build_visits, split_journeys, trips_from_visits
from .services import (build_canonical, cluster_departures, hhmm, passes_disclosure, plurality, service_label,
                       signature_id, weekday_pattern)
from .stops import detect_stops

EPOCH = date(1970, 1, 1)


def connect(cfg: dict, memory="5GB", threads=3) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute(f"PRAGMA memory_limit='{memory}'; PRAGMA threads={threads}; PRAGMA preserve_insertion_order=false")
    tmp = Path(cfg["private_dir"]) / "staging" / "vessel_tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    con.execute(f"PRAGMA temp_directory='{tmp}'")
    return con


def to_dt(t: int) -> datetime:
    return datetime(1970, 1, 1) + timedelta(seconds=int(t))


def to_date(t: int) -> date:
    return EPOCH + timedelta(days=int(t) // 86400)


def minutes_of_day(t: int) -> float:
    return (int(t) % 86400) / 60.0


# ---------------------------------------------------------------------------------------------- inputs

CLOCK_NAMES = {v: k for k, v in CLASS_CODES.items()}
CLOCK_FILE = "clock_device_day.parquet"


def load_observations(con, cfg: dict, private: Path, boxes: list[tuple]) -> dict:
    """Usable observations of every trajectory that has a point inside one of the boxes, with break_before set at
    trajectory starts and wherever the preceding usable observation is not linked by an accepted transition.

    t_raw is collection_time as recorded; t_corr adds the clock correction of the observation's device-day (non-zero
    only for the offset_5h class, see build_clock_offsets.py); clock is the class code (clock.CLASS_CODES). The caller
    picks which of t_raw / t_corr becomes t."""
    obs = private / cfg["inputs"]["observations"]
    mov = private / cfg["inputs"]["movements"]
    clk = private / CLOCK_FILE
    cond = " OR ".join(f"(latitude BETWEEN {a} AND {b} AND longitude BETWEEN {c} AND {d})" for a, b, c, d in boxes)
    con.execute(f"""CREATE OR REPLACE TEMP TABLE sel AS SELECT DISTINCT trajectory_id FROM read_parquet('{obs}')
        WHERE usable_for_trajectory AND trajectory_id IS NOT NULL AND ({cond})""")
    con.execute(f"""CREATE OR REPLACE TEMP TABLE o AS
        SELECT g.observation_id, g.device_key, g.trajectory_id, epoch(g.collection_time)::BIGINT AS t, g.latitude, g.longitude,
               coalesce(c.correction_seconds, 0)::BIGINT AS corr, coalesce(c.clock_class, 'unknown') AS clock_class,
               LAG(g.observation_id) OVER (PARTITION BY g.trajectory_id ORDER BY g.collection_time, g.observation_id) AS prev_id
        FROM read_parquet('{obs}') g LEFT JOIN read_parquet('{clk}') c
          ON c.device_key = g.device_key AND c.day = CAST(g.collection_time AS DATE)
        WHERE g.usable_for_trajectory AND g.trajectory_id IN (SELECT trajectory_id FROM sel)""")
    con.execute(f"""CREATE OR REPLACE TEMP TABLE m AS
        SELECT from_observation_id, to_observation_id, transition_accepted FROM read_parquet('{mov}')
        WHERE to_observation_id IN (SELECT observation_id FROM o)""")
    r = con.execute("""
    SELECT dense_rank() OVER (ORDER BY o.trajectory_id) - 1 AS traj, o.trajectory_id, o.device_key, o.t,
           o.corr, o.clock_class, o.latitude, o.longitude, (o.prev_id IS NULL OR m.transition_accepted IS NOT TRUE) AS break_before
    FROM o LEFT JOIN m ON m.from_observation_id = o.prev_id AND m.to_observation_id = o.observation_id
    ORDER BY o.trajectory_id, o.t, o.observation_id""").fetchnumpy()
    traj = np.asarray(r["traj"], np.int64)
    uniq, first = np.unique(traj, return_index=True)
    t_raw = np.asarray(r["t"], np.int64)
    clock = np.array([CLASS_CODES[c] for c in r["clock_class"]], np.int8)
    return {"traj": traj, "t_raw": t_raw, "t_corr": t_raw + np.asarray(r["corr"], np.int64), "clock": clock,
            "lat": np.asarray(r["latitude"], float),
            "lon": np.asarray(r["longitude"], float), "break": np.asarray(r["break_before"], bool),
            "traj_id": np.asarray(r["trajectory_id"], object)[first], "traj_device": np.asarray(r["device_key"], object)[first]}


def day_device_counts(con, cfg: dict, private: Path, time_basis: str = "corrected") -> dict[date, int]:
    obs = private / cfg["inputs"]["observations"]
    clk = private / CLOCK_FILE
    shift = "+ to_seconds(coalesce(c.correction_seconds, 0)::BIGINT)" if time_basis == "corrected" else ""
    rows = con.execute(f"""SELECT CAST(g.collection_time {shift} AS DATE) AS d, count(DISTINCT g.device_key)
        FROM read_parquet('{obs}') g LEFT JOIN read_parquet('{clk}') c
          ON c.device_key = g.device_key AND c.day = CAST(g.collection_time AS DATE)
        WHERE g.usable_for_trajectory AND g.trajectory_id IS NOT NULL GROUP BY 1""").fetchall()
    return {d: n for d, n in rows}


def speed_unit_check(con, cfg: dict, private: Path) -> dict:
    obs, mov = private / cfg["inputs"]["observations"], private / cfg["inputs"]["movements"]
    row = con.execute(f"""
    WITH m AS (SELECT from_observation_id f, to_observation_id t, derived_speed_kmh d
               FROM read_parquet('{mov}') WHERE transition_accepted AND elapsed_seconds BETWEEN 5 AND 60
                    AND derived_speed_kmh > 10 AND hash(from_observation_id) % 20 = 0),
    o AS (SELECT observation_id id, try_cast(speed_raw AS DOUBLE) s FROM read_parquet('{obs}'))
    SELECT count(*), quantile_cont(m.d / ((a.s + b.s) / 2), [0.25, 0.5, 0.75])
    FROM m JOIN o a ON a.id = m.f JOIN o b ON b.id = m.t
    WHERE a.s > 3 AND b.s > 3 AND abs(a.s - b.s) < 0.2 * a.s""").fetchone()
    q = [float(x) for x in row[1]]
    kmh = cfg["speed"]["kmh_per_knot"]
    nearest = "knots" if abs(q[1] - kmh) < abs(q[1] - 1.0) else "km/h"
    return {"segments_compared": int(row[0]), "derived_kmh_over_raw_q25_q50_q75": [round(x, 3) for x in q],
            "ratio_if_knots": kmh, "ratio_if_kmh": 1.0, "consistent_with": nearest,
            "note": cfg["speed"]["speed_unit_note"]}


# --------------------------------------------------------------------------------------- device classes

def land_union_utm(islands: dict, interior_buffer_m: float):
    polys = [geo.to_utm(i["geom"]).buffer(-interior_buffer_m) for i in islands.values()]
    u = shapely.union_all([p for p in polys if not p.is_empty])
    shapely.prepare(u)
    return u


def on_interior_land(u, lon, lat) -> np.ndarray:
    if not len(lon):
        return np.zeros(0, bool)
    x, y = geo.project_points(lon, lat)
    return shapely.contains_xy(u, x, y)


def device_classes(con, cfg: dict, private: Path, islands: dict) -> list[dict]:
    c = cfg["classification"]
    obs, mov = private / cfg["inputs"]["observations"], private / cfg["inputs"]["movements"]
    kmh = cfg["speed"]["kmh_per_knot"]
    thr_kmh = c["moving_knots"] * kmh
    con.execute(f"""CREATE OR REPLACE TEMP TABLE reg_obs AS
        SELECT observation_id, device_key, latitude, longitude FROM read_parquet('{obs}')
        WHERE usable_for_trajectory AND trajectory_id IS NOT NULL""")
    con.execute(f"""CREATE OR REPLACE TEMP TABLE seg AS
        SELECT device_key, from_observation_id AS f, to_observation_id AS t, from_time, elapsed_seconds,
               distance_metres, derived_speed_kmh AS kmh
        FROM read_parquet('{mov}')
        WHERE transition_accepted AND derived_speed_kmh IS NOT NULL
          AND from_observation_id IN (SELECT observation_id FROM reg_obs)""")
    stats = {r[0]: r for r in con.execute(f"""
        SELECT device_key, count(*) FILTER (WHERE kmh >= {thr_kmh}) AS moving_segments,
               quantile_cont(kmh / {kmh}, 0.5) FILTER (WHERE kmh >= {thr_kmh}) AS median_kn,
               quantile_cont(kmh / {kmh}, 0.9) FILTER (WHERE kmh >= {thr_kmh}) AS p90_kn,
               coalesce(sum(elapsed_seconds) FILTER (WHERE kmh >= {thr_kmh}), 0) / nullif(sum(elapsed_seconds), 0) AS share_moving,
               count(DISTINCT CAST(from_time AS DATE)) AS days_seen, count(*) AS segments
        FROM seg GROUP BY device_key""").fetchall()}
    runs = {r[0]: r for r in con.execute(f"""
        WITH mv AS (SELECT device_key, f, t, from_time, distance_metres FROM seg WHERE kmh >= {thr_kmh}),
        l AS (SELECT *, lag(t) OVER (PARTITION BY device_key ORDER BY from_time, f) AS pt FROM mv),
        r AS (SELECT *, sum(CASE WHEN f IS DISTINCT FROM pt THEN 1 ELSE 0 END)
                          OVER (PARTITION BY device_key ORDER BY from_time, f) AS rid FROM l),
        agg AS (SELECT device_key, rid, sum(distance_metres) AS path_m, min(CAST(from_time AS DATE)) AS d FROM r GROUP BY 1, 2)
        SELECT device_key, max(path_m), count(DISTINCT d) FILTER (WHERE path_m >= {c['min_run_path_m']}) FROM agg GROUP BY 1""").fetchall()}
    n = c["per_device_sample"]
    all_s = con.execute(f"""SELECT device_key, longitude, latitude FROM (
        SELECT *, row_number() OVER (PARTITION BY device_key ORDER BY hash(observation_id)) rn FROM reg_obs) WHERE rn <= {n}""").fetchnumpy()
    mov_s = con.execute(f"""SELECT s.device_key, o.longitude, o.latitude FROM (
        SELECT f, device_key, row_number() OVER (PARTITION BY device_key ORDER BY hash(f)) rn FROM seg WHERE kmh >= {thr_kmh}) s
        JOIN reg_obs o ON o.observation_id = s.f WHERE rn <= {n}""").fetchnumpy()
    u = land_union_utm(islands, c["land_interior_buffer_m"])
    share_all = _share_by_device(all_s, u)
    share_mov = _share_by_device(mov_s, u)
    out = []
    for dk in sorted(stats):
        s = stats[dk]
        run = runs.get(dk, (dk, 0.0, 0))
        land_all = share_all.get(dk)
        land_mov = share_mov.get(dk)
        ev = {"moving_segments": int(s[1]), "p90_knots": float(s[3] or 0), "max_run_path_m": float(run[1] or 0),
              "days_with_run": int(run[2] or 0), "moving_share_off_land": 1.0 - land_mov if land_mov is not None else 0.0}
        cls, conf, reason = classify_device(ev, cfg)
        out.append({"device_key": dk, "class": cls, "confidence": conf, "class_reason": reason,
                    "median_speed_knots": round(float(s[2]), 2) if s[2] is not None else None,
                    "p90_speed_knots": round(float(s[3]), 2) if s[3] is not None else None,
                    "share_time_moving": round(float(s[4] or 0), 4), "days_seen": int(s[5]),
                    "accepted_segments": int(s[6]), "moving_segments": int(s[1]),
                    "max_run_path_m": round(ev["max_run_path_m"], 1), "days_with_sustained_run": ev["days_with_run"],
                    "land_share_all_sample": round(land_all, 4) if land_all is not None else None,
                    "land_share_moving_sample": round(land_mov, 4) if land_mov is not None else None,
                    "land_device_flag": bool(land_all is not None and land_all >= c["land_device_min_share"]),
                    "label_note": "inferred class: review hint only, never used to filter rows",
                    "timezone_assumed": cfg["timezone"]["assumed"], "speed_unit_status": speed_status(cfg)})
    return out


def _share_by_device(rows: dict, u) -> dict:
    if not len(rows["device_key"]):
        return {}
    on = on_interior_land(u, np.asarray(rows["longitude"], float), np.asarray(rows["latitude"], float))
    keys = np.asarray(rows["device_key"], object)
    tot, hit = Counter(keys.tolist()), Counter(keys[on].tolist())
    return {k: hit[k] / v for k, v in tot.items()}


def speed_status(cfg: dict) -> str:
    return f"consistent with {cfg['speed']['speed_unit_assumed']}, pending organiser confirmation" \
        if not cfg["speed"]["speed_unit_confirmed"] else f"confirmed:{cfg['speed']['speed_unit_assumed']}"


# --------------------------------------------------------------------------------------------- services

def haversine_km(a, b) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (a[1], a[0], b[1], b[0]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(h))


def extract(A, zone, dist, jet, sites, dev_class, cfg, radius, dwell) -> dict:
    """Stops -> visits -> journeys and trips. Pure function of its inputs (deterministic)."""
    sc = cfg["stops"]
    stops = detect_stops(A["t"], A["break"], zone, dist, jet, radius, dwell, sc["min_observations"], sc["stop_gap_flag_seconds"])
    visits = build_visits(stops, A["traj"])
    ref = next(s for s in sites if s["code"] == cfg["scopes"]["direction_reference_island_code"])
    dist_ref = [haversine_km(s["anchor"], ref["anchor"]) for s in sites]
    use_unc = cfg["use_unconfirmed_stops_as_origin"]
    journeys, trips = [], []
    for tj in sorted(visits):
        dk = A["traj_device"][tj]
        cls = dev_class.get(dk, "unclassified")
        vs = visits[tj]
        for direction, jv in split_journeys(vs, dist_ref):
            journeys.append({"traj": tj, "device": dk, "cls": cls, "direction": direction, "visits": jv,
                             "clock": CLOCK_NAMES[int(A["clock"][jv[0]["idx"]])] if "clock" in A else "local",
                             "islands": tuple(sites[v["zone"]]["code"] for v in jv)})
        for tr in trips_from_visits(vs):
            o, d = tr["origin"], tr["destination"]
            if not o["depart_known"] or not d["arrive_known"] or d["arrive"] <= o["depart"]:
                continue
            trips.append({"traj": tj, "device": dk, "cls": cls, "o": o, "d": d,
                          "clock": CLOCK_NAMES[int(A["clock"][o["idx"]])] if "clock" in A else "local",
                          "usable_origin": use_unc or not o["unconfirmed"]})
    return {"stops": stops, "journeys": journeys, "trips": trips, "n_visits": sum(len(v) for v in visits.values())}


def aggregate(ex: dict, A, zone, dist, sites, day_dev, cfg, radius, exclude_clock=frozenset()) -> dict:
    """exclude_clock: clock classes whose journeys stay in every table but are not used to form regular services."""
    sv, cv, dv = cfg["services"], cfg["coverage"], cfg["disclosure"]
    p0 = date.fromisoformat(sv["period_start"])
    N = sv["min_days_regular"]
    code_name = {s["code"]: s["name"] for s in sites}
    site_by_code = {s["code"]: s for s in sites}
    at_island = cfg["stops"]["stop_location_mode"] == "island_shoreline"
    journeys = ex["journeys"]
    for j in journeys:
        v0 = j["visits"][0]
        j["date"] = to_date(v0["depart"] if v0["depart_known"] else v0["arrive"])
        j["key"] = (j["islands"], j["direction"])
    groups: dict = {}
    for j in journeys:
        g = groups.setdefault(j["key"], {"days": set(), "devices": set()})
        g["days"].add(j["date"])
        g["devices"].add(j["device"])
    mapping = build_canonical(groups, sv["max_skipped_stops"], sv["min_days_canonical"])
    by_canon: dict = defaultdict(list)
    for j in journeys:
        ck, skipped = mapping[j["key"]]
        j["canon"], j["skipped"] = ck, skipped
        by_canon[ck].append(j)

    sig_rows, dep_rows, services, journey_cat = [], [], [], {}
    suppressed = Counter()
    clock_dropped: Counter = Counter()
    exported_sigs = {}
    for ck in sorted(by_canon):
        js = by_canon[ck]
        islands, direction = ck
        sid = signature_id(islands, direction)
        days = {j["date"] for j in js}
        devices = {j["device"] for j in js}
        cls_top, _, mix = plurality([j["cls"] for j in js])
        n_stop = len(islands)
        skip_cnt = Counter(s for j in js for s in j["skipped"])
        skip_txt = "|".join(f"{code_name[c]}:{round(100 * skip_cnt[c] / len(js))}%" for c in islands if skip_cnt[c])
        # eligible journeys: start at the canonical first stop with a known, confirmed departure
        elig = [j for j in js if j["visits"][0]["zone"] == site_by_code[islands[0]]["idx"] and j["visits"][0]["depart_known"]
                and (cfg["use_unconfirmed_stops_as_origin"] or not j["visits"][0]["unconfirmed"])]
        for j in elig:
            if j.get("clock", "local") in exclude_clock:
                clock_dropped[j.get("clock", "local")] += 1
        elig = [j for j in elig if j.get("clock", "local") not in exclude_clock]
        for j in js:
            journey_cat[id(j)] = (("not_regular_excluded_clock_class", "") if j.get("clock", "local") in exclude_clock
                                  else ("not_regular_partial_or_unconfirmed", ""))
        regular_n = 0
        svc_for_sig = []
        if elig:
            mins = [minutes_of_day(j["visits"][0]["depart"]) for j in elig]
            labels = cluster_departures(mins, [j["date"] for j in elig], sv["departure_window_minutes"])
            clusters = []
            for lab in sorted(set(labels) - {-1}):
                members = [elig[i] for i in np.flatnonzero(labels == lab)]
                clusters.append((float(np.median([minutes_of_day(m["visits"][0]["depart"]) for m in members])), members))
            clusters.sort(key=lambda c: c[0])
            for j, lab in zip(elig, labels):
                if lab == -1:
                    journey_cat[id(j)] = ("not_regular_one_off", "")
            for k, (_, members) in enumerate(clusters, 1):
                cdays = {m["date"] for m in members}
                cdev = {m["device"] for m in members}
                regular = len(cdays) >= N
                svc_id = f"{sid}_s{k:02d}"
                for m in members:
                    journey_cat[id(m)] = ("regular_service" if regular else "not_regular_insufficient_days", svc_id)
                if not passes_disclosure(len(cdev), len(cdays), cfg):
                    suppressed["service_clusters"] += 1
                    if regular:
                        suppressed["regular_service_clusters"] += 1
                        if len(cdev) < dv["min_distinct_devices"]:
                            suppressed["regular_service_clusters_below_device_minimum"] += 1
                    continue
                regular_n += regular
                ctop, cshare, cmix = plurality([m["cls"] for m in members])
                unc = round(sum(m["visits"][0]["unconfirmed"] for m in members) / len(members), 3)
                wp = weekday_pattern(cdays, p0, sv["days_in_period"], sv["weekday_skip_min_days_in_period"])
                label = service_label(ctop, regular)
                stops_out = []
                for pos, code in enumerate(islands):
                    zi = site_by_code[code]["idx"]
                    last = pos == n_stop - 1
                    vals = []
                    for m in members:
                        for v in m["visits"]:
                            if v["zone"] == zi:
                                if last and v["arrive_known"]:
                                    vals.append(minutes_of_day(v["arrive"]))
                                elif not last and v["depart_known"]:
                                    vals.append(minutes_of_day(v["depart"]))
                    if not vals:
                        continue
                    p10, p50, p90 = np.percentile(vals, [10, 50, 90])
                    stops_out.append({"stop_order": pos + 1, "island_code": code, "island_name": code_name[code],
                                      "event": "arrival" if last else "departure", "time_median": hhmm(p50),
                                      "time_p10": hhmm(p10), "time_p90": hhmm(p90), "n_observed": len(vals),
                                      "share_of_trips_visiting": round(len(vals) / len(members), 3),
                                      **({"stop_location": f"at {code_name[code]}"} if at_island else {})})
                for so in stops_out:
                    dep_rows.append({"signature_id": sid, "service_id": svc_id, "direction": direction,
                                     "class": ctop, "class_share": round(cshare, 3), "service_category":
                                     "regular" if regular else "not_regular_insufficient_days",
                                     "regular_service": regular, "inferred_label": label, **so,
                                     "trips": len(members), "days_seen": len(cdays), "days_in_period": sv["days_in_period"],
                                     **wp, "distinct_devices": len(cdev),
                                     "unconfirmed_origin_share": unc, "timezone_assumed": cfg["timezone"]["assumed"]})
                first = stops_out[0] if stops_out else None
                svc = {"service_id": svc_id, "signature_id": sid, "inferred": True, "regular": regular,
                       "inferred_label": label, "inferred_class": ctop, "class_mix": cmix, "direction": direction,
                       "stops": stops_out, "days_seen": len(cdays), "days_in_period": sv["days_in_period"],
                       "days_seen_by_weekday": wp["days_seen_by_weekday"],
                       "weekdays_not_observed": wp["weekdays_not_observed"], "trips": len(members),
                       "distinct_devices": len(cdev), "unconfirmed_origin_share": unc,
                       "dates_seen": sorted(d.isoformat() for d in cdays)}
                if first:
                    where = f"from a stop at {first['island_name']}," if at_island else first["island_name"]
                    svc["wording"] = (f"Observed device movements consistent with the pattern '{label}': usually departs "
                                      f"{where} {first['time_p10']}-{first['time_p90']}, seen on "
                                      f"{len(cdays)} of {sv['days_in_period']} days.")
                services.append(svc)
                svc_for_sig.append(svc_id)
        if not passes_disclosure(len(devices), len(days), cfg):
            suppressed["signatures"] += 1
            continue
        exported_sigs[sid] = {"key": ck, "days": days}
        sig_rows.append({"signature_id": sid, "stop_sequence": " > ".join(code_name[c] for c in islands),
                         "stop_island_codes": ">".join(islands), "direction": direction, "n_stops": n_stop,
                         "class_mix": mix, "inferred_class": cls_top, "days_seen": len(days),
                         "days_in_period": sv["days_in_period"], "distinct_devices": len(devices),
                         "journeys": len(js), "journeys_in_regular_services": sum(
                             1 for j in js if journey_cat[id(j)][0] == "regular_service"),
                         "regular_services": regular_n, "skipped_stop_share": skip_txt,
                         "timezone_assumed": cfg["timezone"]["assumed"],
                         "speed_unit_status": speed_status(cfg)})
    sig_ids_ok = set(exported_sigs)
    dep_rows = [r for r in dep_rows if r["signature_id"] in sig_ids_ok]
    services = [s for s in services if s["signature_id"] in sig_ids_ok]

    # ---- edges from trips between consecutive stops
    edge_groups: dict = defaultdict(list)
    for tr in ex["trips"]:
        if not tr["usable_origin"]:
            continue
        oc, dc = sites[tr["o"]["zone"]]["code"], sites[tr["d"]["zone"]]["code"]
        edge_groups[(oc, dc, tr["cls"])].append(tr)
    reg_pairs = Counter()
    for s in services:
        if s["regular"]:
            codes = [x["island_code"] for x in s["stops"]]
            for a, b in zip(codes, codes[1:]):
                reg_pairs[(a, b)] += 1
    edge_rows = []
    for (oc, dc, cls), trs in sorted(edge_groups.items()):
        dates = [to_date(t["o"]["depart"]) for t in trs]
        devs = {t["device"] for t in trs}
        if not passes_disclosure(len(devs), len(set(dates)), cfg):
            suppressed["edges"] += 1
            continue
        per_day: dict = defaultdict(list)
        for t in trs:
            per_day[to_date(t["o"]["depart"])].append(minutes_of_day(t["o"]["depart"]))
        firsts = [min(v) for v in per_day.values()]
        lasts = [max(v) for v in per_day.values()]
        durs = [(t["d"]["arrive"] - t["o"]["depart"]) / 60 for t in trs]
        wp = weekday_pattern(set(dates), p0, sv["days_in_period"], sv["weekday_skip_min_days_in_period"])
        notes = []
        for c in (oc, dc):
            if not at_island and site_by_code[c]["no_jetty_point"]:
                notes.append(f"{code_name[c]}: no verified jetty point, shoreline buffer used (no_jetty_point)")
        lim = [d for d in per_day if day_dev.get(d, 0) < cv["min_day_device_ratio"] * (np.median(list(day_dev.values())) if day_dev else 0)]
        if lim:
            notes.append(f"{len(lim)} observed day(s) had limited regional coverage")
        notes.append("days without observation say little: missing GPS is not non-operation")
        edge_rows.append({"origin_code": oc, "origin_name": code_name[oc], "destination_code": dc,
                          "destination_name": code_name[dc], "class": cls,
                          "inferred_label": service_label(cls, reg_pairs[(oc, dc)] > 0),
                          "trips": len(trs), "services_per_observed_day_median": float(np.median([len(v) for v in per_day.values()])),
                          "first_departure_median": hhmm(np.median(firsts)),
                          "first_departure_p10": hhmm(np.percentile(firsts, 10)), "first_departure_p90": hhmm(np.percentile(firsts, 90)),
                          "last_departure_median": hhmm(np.median(lasts)),
                          "last_departure_p10": hhmm(np.percentile(lasts, 10)), "last_departure_p90": hhmm(np.percentile(lasts, 90)),
                          "typical_duration_min_median": round(float(np.median(durs)), 1),
                          "duration_min_p10": round(float(np.percentile(durs, 10)), 1),
                          "duration_min_p90": round(float(np.percentile(durs, 90)), 1),
                          "days_seen": len(set(dates)), "days_in_period": sv["days_in_period"], **wp,
                          "distinct_devices": len(devs), "regular_services_on_pair": reg_pairs[(oc, dc)],
                          "coverage_notes": "; ".join(notes), "timezone_assumed": cfg["timezone"]["assumed"]})

    # ---- service days with coverage flag
    zrows = np.flatnonzero((zone >= 0) & (dist <= radius))
    day_idx = (A["t"][zrows] // 86400).astype(np.int64)
    dev_codes = {d: i for i, d in enumerate(sorted(set(A["traj_device"].tolist())))}
    dcode = np.array([dev_codes[d] for d in A["traj_device"]], np.int64)[A["traj"][zrows]]
    key = np.unique((day_idx * 1024 + zone[zrows].astype(np.int64)) * 8192 + dcode)
    site_dev: Counter = Counter()
    for k in key:
        site_dev[(int(k // 8192 // 1024), int((k // 8192) % 1024))] += 1
    med_day = float(np.median(list(day_dev.values()))) if day_dev else 0.0
    day_rows = []
    epoch_p0 = (p0 - EPOCH).days
    for sid, info in sorted(exported_sigs.items()):
        islands, direction = info["key"]
        js = [j for j in by_canon[info["key"]]]
        for d in range(sv["days_in_period"]):
            dd = p0 + timedelta(d)
            devs = {j["device"] for j in js if j["date"] == dd}
            obs_flag = len(devs) > 0
            first_zone = site_by_code[islands[0]]["idx"]
            near = site_dev.get((epoch_p0 + d, first_zone), 0)
            good = day_dev.get(dd, 0) >= cv["min_day_device_ratio"] * med_day and near >= cv["min_site_devices"]
            reason = "good_coverage" if good else ("low_regional_device_count" if day_dev.get(dd, 0) < cv["min_day_device_ratio"] * med_day
                                                  else "few_devices_near_first_stop")
            day_rows.append({"signature_id": sid, "stop_sequence": " > ".join(code_name[c] for c in islands),
                             "direction": direction, "date": dd.isoformat(),
                             "status": "observed" if obs_flag else "not_observed",
                             "devices_seen": (len(devs) if len(devs) >= dv["min_distinct_devices"] else ("<%d" % dv["min_distinct_devices"] if devs else 0)),
                             "coverage_flag": "good" if good else "limited", "coverage_reason": reason,
                             "regional_devices_that_day": day_dev.get(dd, 0), "devices_near_first_stop": near,
                             "note": "not_observed is not cancelled: missing GPS is not non-operation",
                             "timezone_assumed": cfg["timezone"]["assumed"]})
    return {"signatures": sig_rows, "departures": dep_rows, "edges": edge_rows, "service_days": day_rows,
            "services": services, "journey_category": journey_cat, "suppressed": dict(suppressed),
            "clock_excluded_journeys": dict(clock_dropped),
            "journeys": journeys}
