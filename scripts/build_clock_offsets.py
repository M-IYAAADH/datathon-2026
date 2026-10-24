"""Measure and classify FollowMe device clocks from database_time - collection_time.

    .venv/bin/python scripts/build_clock_offsets.py

Reads only the cleaned parquet (gps_clean.parquet), never the raw CSVs. Writes:
  data/private/followme/clock_device_day.parquet, clock_devices.parquet, clock_observations.parquet  (device keys, private)
  data/derived/vessels/clock/*.csv + clock_offsets_summary.json  (aggregates only, no device keys)
Times are never overwritten: clock_observations.parquet adds collection_time_local_corrected and clock_class beside
collection_time. A correction is applied only where a device-day shows a consistent difference near 5 hours.
timezone_confirmed stays false: the organisers have not confirmed the timezone of either time field.
"""
from __future__ import annotations

import csv
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import duckdb  # noqa: E402
from vessel_pipeline import clock  # noqa: E402
from vessel_pipeline.config import load_config  # noqa: E402

QS = [0, 0.001, 0.01, 0.05, 0.1, 0.25, 0.5, 0.75, 0.9, 0.95, 0.99, 0.999, 1]
QNAMES = ["min", "p0.1", "p1", "p5", "p10", "p25", "p50", "p75", "p90", "p95", "p99", "p99.9", "max"]
HIST_LO_MIN, HIST_HI_MIN = -720, 1440  # 15-minute bins from -12 h to +24 h; everything else in two overflow rows
MIN_DEVICES = 3


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def stat_row(con, where: str, label: dict, window: int = 900, off: int = 18000, off_tol: int = 1800) -> dict:
    n, dev, qs, lag, p5, m5, hr, neg = con.execute(f"""
        SELECT count(*), count(DISTINCT device_key), quantile_cont(diff, {QS}),
               avg((diff BETWEEN -60 AND {window})::INT), avg((abs(diff - {off}) <= {off_tol})::INT),
               avg((abs(diff + {off}) <= {off_tol})::INT), avg((diff > 3600)::INT), avg((diff < -60)::INT)
        FROM d WHERE {where}""").fetchone()
    row = dict(label, observations=n, devices=dev)
    row.update({k: round(float(v), 1) for k, v in zip(QNAMES, qs)})
    row.update({"share_lag_le_15min": round(lag, 5), "share_within_30min_of_plus5h": round(p5, 6),
                "share_within_30min_of_minus5h": round(m5, 6), "share_gt_1h": round(hr, 5), "share_below_minus_1min": round(neg, 6)})
    return row


def main() -> int:
    cfg = load_config()
    cc = cfg["clock"]
    private = ROOT / cfg["private_dir"]
    out = ROOT / cfg["output_dir"] / "clock"
    obs = private / "gps_clean.parquet"
    con = duckdb.connect()
    con.execute("PRAGMA memory_limit='5GB'; PRAGMA threads=3")
    con.execute(f"""CREATE TABLE d AS SELECT device_key, network, CAST(collection_time AS DATE) AS dy,
        epoch(database_time) - epoch(collection_time) AS diff, hour(collection_time) AS hr
        FROM read_parquet('{obs}') WHERE collection_time IS NOT NULL AND database_time IS NOT NULL""")
    total = con.execute("SELECT count(*) FROM d").fetchone()[0]
    print(f"{total:,} retained observations", flush=True)

    # ---- 1. national distribution
    national = stat_row(con, "true", {"group": "all"})
    hist = con.execute(f"""SELECT CASE WHEN diff < {HIST_LO_MIN * 60} THEN 'below_{HIST_LO_MIN}'
                                       WHEN diff >= {HIST_HI_MIN * 60} THEN 'from_{HIST_HI_MIN}'
                                       ELSE CAST((floor(diff / 900) * 15)::INT AS VARCHAR) END AS b,
                                  count(*) FROM d GROUP BY 1""").fetchall()
    hmap = dict(hist)
    hist_rows = [{"bin_start_minutes": f"below_{HIST_LO_MIN}", "bin_end_minutes": HIST_LO_MIN, "observations": hmap.get(f"below_{HIST_LO_MIN}", 0)}]
    for m in range(HIST_LO_MIN, HIST_HI_MIN, 15):
        hist_rows.append({"bin_start_minutes": m, "bin_end_minutes": m + 15, "observations": hmap.get(str(m), 0)})
    hist_rows.append({"bin_start_minutes": f"from_{HIST_HI_MIN}", "bin_end_minutes": "", "observations": hmap.get(f"from_{HIST_HI_MIN}", 0)})
    for r in hist_rows:
        r["share"] = round(r["observations"] / total, 6)
    write_csv(out / "difference_histogram_15min.csv", hist_rows)

    # ---- by network
    nets = []
    net_where = {"1": "network = '1'", "2": "network = '2'", "0": "network = '0'",
                 "other_codes": "network IS NULL OR network NOT IN ('0','1','2')"}
    names = {"1": "Dhiraagu", "2": "Ooredoo", "0": "other/none", "other_codes": "unexpected codes"}
    for k, w in net_where.items():
        r = stat_row(con, w, {"network": k, "network_name": names[k]})
        if r["devices"] >= MIN_DEVICES or r["observations"] == 0:
            nets.append(r)
        else:
            nets.append({"network": k, "network_name": names[k], "observations": r["observations"], "devices": f"<{MIN_DEVICES}"})
    cols = list(nets[0])
    for r in nets:
        for c in nets[0]:
            r.setdefault(c, "")
    write_csv(out / "difference_by_network.csv", nets)

    # ---- by day
    days = []
    for (day,) in con.execute("SELECT DISTINCT dy FROM d ORDER BY 1").fetchall():
        days.append(stat_row(con, f"dy = DATE '{day}'", {"date": str(day)}))
    write_csv(out / "difference_by_day.csv", days)

    # ---- by device: private detail, aggregated bins for sharing
    con.execute("""CREATE TABLE dev AS SELECT device_key, count(*)::BIGINT AS n_obs, count(DISTINCT dy)::INT AS days_seen,
        median(diff) AS median_diff, quantile_cont(diff, 0.1) AS p10_diff, quantile_cont(diff, 0.9) AS p90_diff,
        avg((diff BETWEEN -60 AND 900)::INT) AS share_lag_le_15min FROM d GROUP BY 1""")
    con.execute(f"COPY dev TO '{private / 'clock_device_difference.parquet'}' (FORMAT PARQUET)")
    dbins = con.execute("""SELECT CASE WHEN median_diff < -3600 THEN 'below_-60' WHEN median_diff >= 1440*60 THEN 'from_1440'
                                       ELSE CAST((floor(median_diff / 900) * 15)::INT AS VARCHAR) END AS b,
                                  count(*), sum(n_obs) FROM dev GROUP BY 1""").fetchall()
    dev_rows = [{"device_median_difference_bin_start_minutes": b, "devices": (n if n >= MIN_DEVICES else f"<{MIN_DEVICES}"),
                 "observations": (o if n >= MIN_DEVICES else "")}
                for b, n, o in sorted(dbins, key=lambda x: (x[0].startswith("from"), not x[0].startswith("below"),
                                                            float(x[0]) if x[0][0] in "-0123456789" else 0))]
    write_csv(out / "device_median_distribution.csv", dev_rows)

    # ---- 2. classify
    print("classifying device-days ...", flush=True)
    day_rows = clock.measure_device_days(con, obs, cc)
    resolved, devices = clock.classify_devices(day_rows, cc)
    for r in resolved:
        r["correction_seconds"] = clock.correction_for(r["clock_class"], cc)
    con.execute("""CREATE TABLE clock_dd (device_key VARCHAR, day DATE, n_obs BIGINT, median_diff DOUBLE,
        consistent_share DOUBLE, measured_class VARCHAR, clock_class VARCHAR, class_reason VARCHAR, correction_seconds INTEGER)""")
    con.executemany("INSERT INTO clock_dd VALUES (?,?,?,?,?,?,?,?,?)",
                    [(r["device_key"], r["day"], r["n_obs"], r["median_diff"], r["consistent_share"], r["measured_class"],
                      r["clock_class"], r["class_reason"], r["correction_seconds"]) for r in resolved])
    con.execute(f"COPY clock_dd TO '{private / 'clock_device_day.parquet'}' (FORMAT PARQUET)")
    con.execute("""CREATE TABLE clock_dev (device_key VARCHAR, device_days INTEGER, measured_days INTEGER, measured_classes VARCHAR,
        stable BOOLEAN, device_class VARCHAR, switches_class BOOLEAN)""")
    con.executemany("INSERT INTO clock_dev VALUES (?,?,?,?,?,?,?)",
                    [(x["device_key"], x["device_days"], x["measured_days"], json.dumps(x["measured_classes"], sort_keys=True),
                      x["stable"], x["device_class"], x["switches_class"]) for x in devices])
    con.execute(f"COPY clock_dev TO '{private / 'clock_devices.parquet'}' (FORMAT PARQUET)")
    con.execute(f"""COPY (SELECT g.observation_id, g.device_key, g.collection_time,
            g.collection_time + to_seconds(coalesce(c.correction_seconds, 0)::BIGINT) AS collection_time_local_corrected,
            coalesce(c.clock_class, 'unknown') AS clock_class
        FROM read_parquet('{obs}') g LEFT JOIN clock_dd c ON c.device_key = g.device_key AND c.day = CAST(g.collection_time AS DATE))
        TO '{private / 'clock_observations.parquet'}' (FORMAT PARQUET)""")

    # ---- counts by class
    cls_names = cc["classes"]
    dd_by = Counter(r["clock_class"] for r in resolved)
    dd_measured = Counter(r["measured_class"] for r in resolved)
    obs_by = dict(con.execute("SELECT clock_class, sum(n_obs)::BIGINT FROM clock_dd GROUP BY 1").fetchall())
    dev_by = Counter(x["device_class"] for x in devices)
    corrected_obs = int(obs_by.get("offset_5h", 0))
    count_rows = []
    for c in cls_names:
        count_rows.append({"clock_class": c, "device_days": dd_by.get(c, 0), "device_days_own_evidence": dd_measured.get(c, 0),
                           "observations": int(obs_by.get(c, 0)),
                           "devices_stable_class": dev_by.get(c, 0)})
    count_rows.append({"clock_class": "device_switches_class", "device_days": "", "device_days_own_evidence": "", "observations": "",
                       "devices_stable_class": dev_by.get("switching", 0)})
    write_csv(out / "clock_class_counts.csv", count_rows)

    # ---- validation (a): stability within device across days
    meas = [r for r in resolved if r["measured_class"] in clock.MEASURED]
    by_dev: dict = {}
    for r in meas:
        by_dev.setdefault(r["device_key"], []).append(r)
    stab = {}
    for c in ("local", "offset_5h", "other"):
        spreads = []
        for dk, rs in by_dev.items():
            vals = [r["median_diff"] for r in rs if r["measured_class"] == c]
            if len(vals) >= 3:
                spreads.append(max(vals) - min(vals))
        spreads.sort()
        stab[c] = {"devices_with_ge3_days_in_class": len(spreads),
                   "median_spread_of_daily_medians_s": (round(spreads[len(spreads) // 2], 1) if spreads else None),
                   "share_with_spread_le_300s": (round(sum(s <= 300 for s in spreads) / len(spreads), 4) if spreads else None)}
        if 0 < len(spreads) < MIN_DEVICES:
            stab[c] = {"devices_with_ge3_days_in_class": f"<{MIN_DEVICES}", "note": "suppressed (disclosure minimum)"}
    switchers = [x for x in devices if x["switches_class"]]
    sw_pairs = Counter(tuple(sorted(x["measured_classes"])) for x in switchers)

    # observation hour-of-day profile before/after (information only)
    hp = con.execute(f"""SELECT hour(g.collection_time) AS h_raw, hour(g.collection_time + to_seconds(coalesce(c.correction_seconds,0)::BIGINT)) AS h_corr
        FROM read_parquet('{obs}') g LEFT JOIN clock_dd c ON c.device_key = g.device_key AND c.day = CAST(g.collection_time AS DATE)""")
    raw_h, cor_h = Counter(), Counter()
    for h_raw, h_corr in hp.fetchall():
        raw_h[h_raw] += 1
        cor_h[h_corr] += 1
    write_csv(out / "observation_hour_profile.csv",
              [{"hour_of_day": h, "observations_raw": raw_h[h], "observations_corrected": cor_h[h]} for h in range(24)])

    nd = con.execute("SELECT count(DISTINCT device_key) FROM d").fetchone()[0]
    summary = {
        "generated_at_utc": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds") + "Z",
        "status": "PROVISIONAL: thresholds in config/clock_offset.json are not validated against ground truth",
        "timezone_confirmed": cfg["timezone"]["timezone_confirmed"],
        "timezone_note": "The organisers have not confirmed the timezone of collection_time (device_date) or database_time.",
        "clock_correction": {"method": "per device-day median of database_time - collection_time; a +5 h shift is applied only to "
                                       "device-days whose difference is consistent and within tolerance of 18000 s",
                             "evidence_summary": None},
        "difference_definition": cc["difference_definition"],
        "thresholds": {k: cc[k] for k in ("expected_offset_seconds", "correction_seconds", "local", "offset_5h", "consistency",
                                          "min_observations_device_day", "device", "exclude_from_regular_clustering")},
        "observations": total, "devices": nd,
        "national": national, "by_network_file": "difference_by_network.csv", "by_day_file": "difference_by_day.csv",
        "class_counts": {"device_days": dict(dd_by), "device_days_own_evidence": dict(dd_measured),
                         "observations": {k: int(v) for k, v in obs_by.items()},
                         "devices_stable_class": {k: v for k, v in dev_by.items()}},
        "observations_corrected": corrected_obs, "observations_unchanged": int(total - corrected_obs),
        "devices_with_any_offset_5h_day": sum(1 for x in devices if x["measured_classes"].get("offset_5h")),
        "devices_switching_class": len(switchers),
        "switch_class_combinations": {"+".join(k): (v if v >= MIN_DEVICES else f"<{MIN_DEVICES}") for k, v in sw_pairs.items()},
        "stability_within_class": stab,
        "observation_hour_profile_file": "observation_hour_profile.csv"}
    summary["clock_correction"]["evidence_summary"] = (
        f"{dd_measured.get('offset_5h', 0)} of {len(resolved)} device-days show a consistent difference near 5 h; "
        f"{corrected_obs} of {total} observations corrected. National median difference "
        f"{national['p50']} s, p99 {national['p99']} s; no 5 h cluster separates from the long tail of delayed uploads.")
    (out / "clock_offsets_summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    print(json.dumps({k: summary[k] for k in ("observations", "devices", "class_counts", "observations_corrected",
                                              "devices_switching_class", "stability_within_class")}, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
