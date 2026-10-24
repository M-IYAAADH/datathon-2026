"""Inferred vessel services from the cleaned FollowMe outputs.

    .venv/bin/python scripts/build_vessel_services.py --scope corridor
    .venv/bin/python scripts/build_vessel_services.py --scope full

Refuses to run unless both FollowMe run manifests are complete and valid. Device keys stay in
data/private/followme/; data/derived/vessels/ holds aggregates only. Times are collection_time read as local
Maldives time (UTC+5, unconfirmed), with a +5 h correction added only for device-days that
build_clock_offsets.py classed as offset_5h (--time-basis corrected, the default; raw applies no correction). Devices
classed unknown or other stay in the data but are not used to form regular services (--clock-exclude). Run
scripts/build_clock_offsets.py first. Speed unit is unconfirmed.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from harbour_pipeline.match import check_preconditions  # noqa: E402
from vessel_pipeline import build, clock, geo, mtcc  # noqa: E402
from vessel_pipeline.config import load_config  # noqa: E402


class PreconditionError(RuntimeError):
    pass


def write_csv(path: Path, rows: list[dict], columns: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = columns or (list(rows[0]) if rows else [])
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def write_parquet(con, path: Path, rows: list[dict], types: dict[str, str]) -> None:
    """rows -> parquet via a temporary CSV (no pandas/pyarrow needed). Empty string is NULL."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp.csv")
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(list(types))
        for r in rows:
            w.writerow(["" if r.get(k) is None else (str(r[k]).lower() if isinstance(r[k], bool) else r[k]) for k in types])
    cols = ", ".join(f"'{k}': '{v}'" for k, v in types.items())
    con.execute(f"COPY (SELECT * FROM read_csv('{tmp}', header=true, nullstr='', columns={{{cols}}})) "
                f"TO '{path}' (FORMAT PARQUET)")
    tmp.unlink()


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scope", choices=["corridor", "full"], default="corridor")
    ap.add_argument("--config", default=None)
    ap.add_argument("--skip-sensitivity", action="store_true")
    ap.add_argument("--time-basis", choices=["raw", "corrected"], default="corrected",
                    help="corrected: collection_time plus the measured clock correction (default); raw: collection_time as recorded")
    ap.add_argument("--clock-exclude", default=None,
                    help="comma-separated clock classes kept out of regular-service clustering (default: config/clock_offset.json; "
                         "'' keeps every class)")
    ap.add_argument("--stop-location-mode", choices=["island_shoreline", "jetty"], default=None,
                    help="override stops.stop_location_mode from the config (default island_shoreline)")
    ap.add_argument("--out-dir", default=None,
                    help="write the shareable outputs here and skip the private trip/journey/class tables "
                         "(for comparison runs; the default output_dir and private tables stay untouched)")
    ap.add_argument("--use-cache", action="store_true", help="reuse the loaded observation arrays (private staging)")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    if args.stop_location_mode:
        cfg["stops"]["stop_location_mode"] = args.stop_location_mode
    mode = cfg["stops"]["stop_location_mode"]
    shore = mode == "island_shoreline"
    private = ROOT / cfg["private_dir"]
    out = Path(args.out_dir) if args.out_dir else ROOT / cfg["output_dir"]
    out.mkdir(parents=True, exist_ok=True)
    problems = check_preconditions(private)
    clock_file = private / build.CLOCK_FILE
    if not clock_file.exists():
        problems.append(f"{build.CLOCK_FILE} missing: run scripts/build_clock_offsets.py first")
    if problems:
        raise PreconditionError("FollowMe outputs are not complete and valid: " + "; ".join(problems))

    islands = geo.load_islands(ROOT / cfg["reference"]["islands_geojson"])
    if shore:  # the harbour reference (data/reference/harbours/) is not read at all
        rows, columns = [], []
    else:
        rows, columns = geo.read_harbour_rows(ROOT / cfg["reference"]["harbour_csv"],
                                             ROOT / cfg["reference"]["verification_csv"])
    codes = geo.scope_island_codes(cfg, args.scope, islands, rows, columns)
    used, skipped = geo.select_jetties(rows, columns, cfg, set(codes))
    sites = geo.build_sites(codes, used, islands, mode)
    if shore:
        print(f"scope={args.scope}: {len(sites)} islands, stop_location_mode=island_shoreline "
              f"(band {cfg['stops']['radius_m']} m, harbour reference not read)", flush=True)
    else:
        print(f"scope={args.scope}: {len(sites)} islands, stop_location_mode=jetty, {len(used)} jetty rows used, "
              f"{len(skipped)} skipped, {sum(s['no_jetty_point'] for s in sites)} islands without a verified jetty", flush=True)

    max_r = max(cfg["stops"]["sensitivity_radius_m"] + [cfg["stops"]["radius_m"]])
    boxes = []
    for s in sites:
        if geo.uses_shoreline(s):
            x0, y0, x1, y1 = islands[s["code"]]["geom"].bounds
        else:
            xs = [j["longitude"] for j in s["jetties"]]
            ys = [j["latitude"] for j in s["jetties"]]
            x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
        boxes.append((y0 - 0.004, y1 + 0.004, x0 - 0.004, x1 + 0.004))

    con = build.connect(cfg)
    print("loading observations ...", flush=True)
    cache = (private / "staging" / "vessel_tmp" /
             f"obs_v3_{args.scope}_{mode}_{sha(private / 'run_manifest.json')[:12]}_{sha(clock_file)[:8]}.npz")
    if args.use_cache and cache.exists():
        z = np.load(cache, allow_pickle=True)
        A = {k: z[k] for k in z.files}
    else:
        A = build.load_observations(con, cfg, private, boxes)
        if args.use_cache:
            np.savez(cache, **A)
    A["t"] = A["t_corr"] if args.time_basis == "corrected" else A["t_raw"]
    exclude_clock = frozenset(clock.excluded_classes(cfg["clock"], args.clock_exclude))
    print(f"  {len(A['t']):,} observations, {len(A['traj_id']):,} trajectories; time basis {args.time_basis}; "
          f"{int((A['t_corr'] != A['t_raw']).sum()):,} observations carry a clock correction; "
          f"clock classes excluded from regular-service clustering: {sorted(exclude_clock) or 'none'}", flush=True)
    zone, dist, jet = geo.assign_zones(A["lat"], A["lon"], sites, islands, max_r)
    print(f"  {int((zone >= 0).sum()):,} observations within {max_r} m of a scope site", flush=True)

    print("classifying devices ...", flush=True)
    classes = build.device_classes(con, cfg, private, islands)
    dev_class = {c["device_key"]: c["class"] for c in classes}
    unit = build.speed_unit_check(con, cfg, private)
    day_dev = build.day_device_counts(con, cfg, private, args.time_basis)

    sc = cfg["stops"]
    ex = build.extract(A, zone, dist, jet, sites, dev_class, cfg, sc["radius_m"], sc["dwell_seconds"])
    agg = build.aggregate(ex, A, zone, dist, sites, day_dev, cfg, sc["radius_m"], exclude_clock)
    print(f"  stops={len(ex['stops']['first'])} journeys={len(ex['journeys'])} trips={len(ex['trips'])} "
          f"signatures_exported={len(agg['signatures'])} services_exported={len(agg['services'])}", flush=True)

    sens = []
    if not args.skip_sensitivity:
        for r in sc["sensitivity_radius_m"]:
            for d in sc["sensitivity_dwell_seconds"]:
                e2 = build.extract(A, zone, dist, jet, sites, dev_class, cfg, r, d)
                a2 = build.aggregate(e2, A, zone, dist, sites, day_dev, cfg, r, exclude_clock)
                sens.append({"radius_m": r, "dwell_seconds": d, "stops": int(len(e2["stops"]["first"])),
                             "journeys": len(e2["journeys"]), "trips": len(e2["trips"]),
                             "signatures_exported": len(a2["signatures"]),
                             "regular_services_exported": sum(1 for s in a2["services"] if s["regular"]),
                             "services_exported": len(a2["services"]),
                             "is_default": r == sc["radius_m"] and d == sc["dwell_seconds"]})
                print("  sensitivity", sens[-1], flush=True)

        import copy
        c2 = copy.deepcopy(cfg)
        c2["use_unconfirmed_stops_as_origin"] = not cfg["use_unconfirmed_stops_as_origin"]
        e2 = build.extract(A, zone, dist, jet, sites, dev_class, c2, sc["radius_m"], sc["dwell_seconds"])
        a2 = build.aggregate(e2, A, zone, dist, sites, day_dev, c2, sc["radius_m"], exclude_clock)
        sens.append({"radius_m": sc["radius_m"], "dwell_seconds": sc["dwell_seconds"], "use_unconfirmed_stops_as_origin": c2["use_unconfirmed_stops_as_origin"],
                     "stops": int(len(e2["stops"]["first"])), "journeys": len(e2["journeys"]), "trips": len(e2["trips"]),
                     "signatures_exported": len(a2["signatures"]),
                     "regular_services_exported": sum(1 for s in a2["services"] if s["regular"]),
                     "services_exported": len(a2["services"]), "is_default": False})
        print("  sensitivity", sens[-1], flush=True)

    # ---------------------------------------------------------------- private outputs
    tz, ust = cfg["timezone"]["assumed"], build.speed_status(cfg)
    save_private = (lambda *a, **k: None) if args.out_dir else write_parquet  # comparison run: private tables untouched
    save_private(con, private / "vessel_classes.parquet", classes, {
        "device_key": "VARCHAR", "class": "VARCHAR", "confidence": "VARCHAR", "class_reason": "VARCHAR",
        "median_speed_knots": "DOUBLE", "p90_speed_knots": "DOUBLE", "share_time_moving": "DOUBLE", "days_seen": "BIGINT",
        "accepted_segments": "BIGINT", "moving_segments": "BIGINT", "max_run_path_m": "DOUBLE",
        "days_with_sustained_run": "BIGINT", "land_share_all_sample": "DOUBLE", "land_share_moving_sample": "DOUBLE",
        "land_device_flag": "BOOLEAN", "label_note": "VARCHAR", "timezone_assumed": "VARCHAR", "speed_unit_status": "VARCHAR"})
    code_name = {s["code"]: s["name"] for s in sites}

    def jid(s, v):  # jetty ids only exist in jetty mode; island_shoreline outputs carry none
        return "" if geo.uses_shoreline(s) else s["jetties"][v["jetty"]]["harbour_ref_id"]

    def no_jetty(s):
        return "" if shore else s["no_jetty_point"]

    trip_rows = []
    for k, t in enumerate(ex["trips"], 1):
        so, sd = sites[t["o"]["zone"]], sites[t["d"]["zone"]]
        trip_rows.append({
            "trip_id": f"trip_{k:07d}", "trajectory_id": A["traj_id"][t["traj"]], "device_key": t["device"],
            "device_class": t["cls"], "origin_island_code": so["code"], "origin_island_name": so["name"],
            "destination_island_code": sd["code"], "destination_island_name": sd["name"],
            "origin_jetty_id": jid(so, t["o"]), "destination_jetty_id": jid(sd, t["d"]),
            "origin_no_jetty_point": no_jetty(so), "destination_no_jetty_point": no_jetty(sd),
            "stop_location_mode": mode,
            "departure_time": build.to_dt(t["o"]["depart"]).isoformat(sep=" "),
            "arrival_time": build.to_dt(t["d"]["arrive"]).isoformat(sep=" "),
            "duration_minutes": round((t["d"]["arrive"] - t["o"]["depart"]) / 60, 2),
            "origin_stop_unconfirmed": t["o"]["unconfirmed"], "destination_stop_unconfirmed": t["d"]["unconfirmed"],
            "clock_class": t["clock"], "time_basis": args.time_basis, "timezone_assumed": tz})
    save_private(con, private / "vessel_trips.parquet", trip_rows, {
        "trip_id": "VARCHAR", "trajectory_id": "VARCHAR", "device_key": "VARCHAR", "device_class": "VARCHAR",
        "origin_island_code": "VARCHAR", "origin_island_name": "VARCHAR", "destination_island_code": "VARCHAR",
        "destination_island_name": "VARCHAR", "origin_jetty_id": "VARCHAR", "destination_jetty_id": "VARCHAR",
        "origin_no_jetty_point": "BOOLEAN", "destination_no_jetty_point": "BOOLEAN", "stop_location_mode": "VARCHAR",
        "departure_time": "TIMESTAMP",
        "arrival_time": "TIMESTAMP", "duration_minutes": "DOUBLE", "origin_stop_unconfirmed": "BOOLEAN",
        "destination_stop_unconfirmed": "BOOLEAN", "clock_class": "VARCHAR", "time_basis": "VARCHAR",
        "timezone_assumed": "VARCHAR"})
    jrows = []
    for k, j in enumerate(agg["journeys"], 1):
        cat, svc = agg["journey_category"].get(id(j), ("", ""))
        jrows.append({"journey_id": f"jny_{k:07d}", "trajectory_id": A["traj_id"][j["traj"]], "device_key": j["device"],
                      "device_class": j["cls"], "direction": j["direction"], "stop_sequence": ">".join(j["islands"]),
                      "canonical_stop_sequence": ">".join(j["canon"][0]), "skipped_stops": ">".join(j["skipped"]),
                      "service_category": cat, "service_id": svc,
                      "first_departure_time": build.to_dt(j["visits"][0]["depart"]).isoformat(sep=" ") if j["visits"][0]["depart_known"] else None,
                      "clock_class": j["clock"], "time_basis": args.time_basis, "timezone_assumed": tz})
    save_private(con, private / "vessel_journeys.parquet", jrows, {
        "journey_id": "VARCHAR", "trajectory_id": "VARCHAR", "device_key": "VARCHAR", "device_class": "VARCHAR",
        "direction": "VARCHAR", "stop_sequence": "VARCHAR", "canonical_stop_sequence": "VARCHAR",
        "skipped_stops": "VARCHAR", "service_category": "VARCHAR", "service_id": "VARCHAR",
        "first_departure_time": "TIMESTAMP", "clock_class": "VARCHAR", "time_basis": "VARCHAR",
        "timezone_assumed": "VARCHAR"})

    # ------------------------------------------------------------ shareable outputs (aggregates only)
    def tb(rows):
        return [{**r, "time_basis": args.time_basis, "stop_location_mode": mode} for r in rows]

    write_csv(out / "service_signatures.csv", tb(agg["signatures"]))
    write_csv(out / "service_departures.csv", tb(agg["departures"]))
    write_csv(out / "service_edges.csv", tb(agg["edges"]))
    write_csv(out / "service_days.csv", tb(agg["service_days"]))
    if shore:  # no jetty ids in shareable outputs; drop a stale file from an earlier jetty-mode run
        (out / "jetty_selection.csv").unlink(missing_ok=True)
    else:
        write_csv(out / "jetty_selection.csv", used + skipped,
                  ["decision", "reason", "island_code", "island_name", "harbour_ref_id", "name", "feature_class",
                   "confidence", "source_feature_id", "longitude", "latitude"])
    corridor = {c["island_code"] for c in cfg["scopes"]["corridor"]["islands"]}
    csvc = [s for s in agg["services"] if all(x["island_code"] in corridor for x in s["stops"])]
    n_corrected = int((A["t_corr"] != A["t_raw"]).sum())
    clk_summary_path = ROOT / cfg["output_dir"] / "clock" / "clock_offsets_summary.json"
    clk_summary = json.loads(clk_summary_path.read_text(encoding="utf-8")) if clk_summary_path.exists() else {}
    evidence = (clk_summary.get("clock_correction") or {}).get("evidence_summary", "see data/derived/vessels/clock/")
    in_scope = Counter(build.CLOCK_NAMES[int(c)] for c in A["clock"])
    stop_location = {
        "stop_location_mode": mode, "stop_band_m": cfg["stops"]["radius_m"] if shore else None,
        "description": (f"A stop is a run of observations inside a {cfg['stops']['radius_m']} m band either side of the island's "
                        f"OneMap shoreline for at least {cfg['stops']['dwell_seconds']} s: stops are 'at Island X', not at a named jetty. "
                        "The harbour/jetty reference (data/reference/harbours/) is not used."
                        if shore else "A stop is within the radius of a verified jetty point; the shoreline band is the flagged "
                        "no_jetty_point fallback."),
        "jetty_ids_in_shareable_outputs": not shore}
    meta = {"stop_location": stop_location, "timezone_assumed": tz, "timezone_confirmed": cfg["timezone"]["timezone_confirmed"],
            "time_basis": args.time_basis,
            "clock_correction": {
                "method": clk_summary.get("clock_correction", {}).get("method", "per device-day median of database_time - collection_time"),
                "evidence_summary": evidence, "provisional": True,
                "observations_corrected_in_scope": n_corrected if args.time_basis == "corrected" else 0,
                "observations_unchanged_in_scope": int(len(A["t"])) - (n_corrected if args.time_basis == "corrected" else 0),
                "statement": ("times corrected for a measured clock offset on some devices, pending organiser confirmation"
                              if args.time_basis == "corrected" and n_corrected else
                              "no time correction applied" + (": no device-day met the offset criteria" if args.time_basis == "corrected" else
                                                              " (time basis raw)") + "; pending organiser confirmation of the timezone of "
                              "both time fields"),
                "organisers_confirmed": False},
            "clock_exclusion": {"classes_excluded_from_regular_service_clustering": sorted(exclude_clock),
                                "observations_in_scope_by_clock_class": dict(in_scope),
                                "journeys_dropped_from_clustering_by_class": agg["clock_excluded_journeys"],
                                "note": "excluded journeys stay in the private trip/journey tables and in service_edges.csv; "
                                        "they are not used to form regular services"},
            "speed_unit_status": ust, "speed_unit_confirmed": cfg["speed"]["speed_unit_confirmed"]}
    corridor_json = {
        "dataset": "corridor_services", "inferred": True, "scope": args.scope,
        "description": "Inferred services from observed device movements on the demo corridor. Not an official timetable, "
                       "not confirmed ferries, passenger services or specific vessels.",
        **meta,
        "provenance": {"source": "FollowMe GPS, August 2025 (cleaned, private)", "method": "docs/vessel_datasets.md",
                       "period": f"{cfg['services']['period_start']} to 31 days",
                       "disclosure_rule": cfg["disclosure"]},
        "wording": {"inferred class": "slow_vessel / fast_vessel / mixed / unclassified",
                    "regular fast": "inferred speedboat service", "regular slow": "inferred slow ferry/dhoni service",
                    "slow not repeating": "inferred supply/other",
                    "caveat": "Times are ranges. Missing GPS is not non-operation; 'not observed' is not 'cancelled'."},
        "islands": [({"island_code": s["code"], "island_name": s["name"], "stop_location": f"at {s['name']}",
                      "stop_location_mode": mode, "stop_band_m": cfg["stops"]["radius_m"]} if shore else
                     {"island_code": s["code"], "island_name": s["name"],
                      "jetty_status": "no_jetty_point" if s["no_jetty_point"] else "verified_jetty_point",
                      "jetty_points_used": len(s["jetties"])}) for s in sites if s["code"] in corridor],
        "services": csvc}
    (out / "corridor_services.json").write_text(json.dumps(corridor_json, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    names = {s["code"]: s["name"] for s in sites if s["code"] in corridor}
    mt, stop_map = mtcc.load_mtcc(ROOT / cfg["reference"]["ferry_dir"], names)
    cmp_rows = mtcc.compare(mt, csvc, names, cfg)
    write_csv(out / "inferred_vs_mtcc.csv", tb(cmp_rows), ["origin_code", "origin_name", "destination_code", "destination_name",
                                                       "source", "ref_id", "route_or_signature", "departure_time", "departure_p10",
                                                       "departure_p90", "arrival_time", "days", "days_seen_of_31", "calendar_status",
                                                       "inferred_label", "long_mtcc_wait", "mtcc_wait_min_range", "pair_longest_mtcc_gap_min",
                                                       "pair_longest_mtcc_gap_window", "note", "time_basis"])

    # departure hour-of-day profile, raw vs corrected, for information only (never used to decide a correction)
    raw_h, cor_h = Counter(), Counter()
    for tr in ex["trips"]:
        if not tr["usable_origin"]:
            continue
        shift = int(A["t_corr"][tr["o"]["idx"]] - A["t_raw"][tr["o"]["idx"]])
        d = tr["o"]["depart"]
        d_raw = d - shift if args.time_basis == "corrected" else d
        raw_h[int(d_raw % 86400 // 3600)] += 1
        cor_h[int((d_raw + shift) % 86400 // 3600)] += 1
    write_csv(out / "departure_hour_profile.csv", [{"hour_of_day": h, "trip_departures_raw": raw_h[h],
                                                    "trip_departures_corrected": cor_h[h], "time_basis": args.time_basis,
                                                    "scope": args.scope} for h in range(24)])
    night = [{"service_id": sv["service_id"], "inferred_label": sv["inferred_label"], "regular": sv["regular"],
              "first_stop": sv["stops"][0]["island_name"], "stop_sequence": " > ".join(x["island_name"] for x in sv["stops"]),
              "in_corridor": all(x["island_code"] in corridor for x in sv["stops"]),
              "departure_p10": sv["stops"][0]["time_p10"], "departure_median": sv["stops"][0]["time_median"],
              "departure_p90": sv["stops"][0]["time_p90"], "days_seen": sv["days_seen"], "distinct_devices": sv["distinct_devices"],
              "trips": sv["trips"], "time_basis": args.time_basis,
              "note": "information only: a night departure is not evidence of a wrong clock"}
             for sv in agg["services"] if sv["stops"] and (sv["stops"][0]["time_median"] >= "22:00" or sv["stops"][0]["time_median"] < "05:00")]
    write_csv(out / "night_services_2200_0500.csv", night, list(night[0]) if night else
              ["service_id", "inferred_label", "regular", "first_stop", "stop_sequence", "in_corridor", "departure_p10",
               "departure_median", "departure_p90", "days_seen", "distinct_devices", "trips", "time_basis", "note"])

    cls_counts = Counter(c["class"] for c in classes)
    summary = {
        "generated_at_utc": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds") + "Z", "scope": args.scope,
        "config_sha256": sha(Path(args.config) if args.config else ROOT / "config" / "vessel_services.json"),
        "clock_config_sha256": sha(ROOT / "config" / "clock_offset.json"),
        "input_run_manifest_sha256": sha(private / "run_manifest.json"),
        "input_regional_run_manifest_sha256": sha(private / "regional_run_manifest.json"),
        **meta, "speed_unit_check": unit,
        "parameters": {k: cfg[k] for k in ("stops", "classification", "services", "coverage", "disclosure", "use_unconfirmed_stops_as_origin")},
        "clock_thresholds": cfg["clock"],
        "disclosure_rule": {**cfg["disclosure"], "rule": "no exported row is backed by fewer than the minimum distinct devices "
                            "AND distinct days; per-date device counts below the device minimum print as '<N'"},
        "suppressed_by_disclosure": agg["suppressed"],
        "counts": {"islands_in_scope": len(sites),
                   "islands_without_verified_jetty": None if shore else [s["name"] for s in sites if s["no_jetty_point"]],
                   "jetty_rows_used": None if shore else len(used), "jetty_rows_skipped": None if shore else len(skipped),
                   "observations_loaded": int(len(A["t"])), "trajectories_loaded": int(len(A["traj_id"])),
                   "stops": int(len(ex["stops"]["first"])), "stops_unconfirmed": int(ex["stops"]["unconfirmed"].sum()),
                   "journeys": len(ex["journeys"]), "trips": len(ex["trips"]),
                   "signatures_exported": len(agg["signatures"]), "services_exported": len(agg["services"]),
                   "regular_services_exported": sum(1 for s in agg["services"] if s["regular"]),
                   "corridor_services": len(csvc), "night_services_2200_0500": len(night),
                   "clock_excluded_journeys": agg["clock_excluded_journeys"],
                   "device_classes": dict(cls_counts), "land_devices_flagged": sum(c["land_device_flag"] for c in classes),
                   "journey_categories": dict(Counter(v[0] for v in agg["journey_category"].values()))},
        "sensitivity": sens,
        "labels": "Output labels are 'inferred class' and 'inferred service'. Nothing here is a confirmed ferry, "
                  "official timetable, passenger service or specific vessel."}
    (out / "service_summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    print(json.dumps(summary["counts"], indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
