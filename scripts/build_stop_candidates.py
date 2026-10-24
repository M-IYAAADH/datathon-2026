"""GPS-derived stop candidates and matching to the harbour reference (private outputs only).

    .venv/bin/python scripts/build_stop_candidates.py

Refuses to run unless the FollowMe national and regional runs are complete and valid. Every output
is written under data/private/followme/; device keys never leave that directory.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

import duckdb
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from harbour_pipeline.config import load_config  # noqa: E402
from harbour_pipeline.match import (  # noqa: E402
    ReferenceIndex, check_preconditions, classify_cluster, load_reference_features)
from harbour_pipeline.reference import IslandIndex  # noqa: E402
from harbour_pipeline.stops import dbscan, detect_stay_points, stay_id, summarize_clusters  # noqa: E402


class PreconditionError(RuntimeError):
    pass


def load_observations(private: Path, cfg: dict) -> dict:
    inp = cfg["inputs"]
    obs, mov = private / inp["observations"], private / inp["movements"]
    con = duckdb.connect()
    try:
        have_o = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet('{obs}')").fetchall()}
        have_m = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet('{mov}')").fetchall()}
        need_o = {"observation_id", "device_key", "trajectory_id", "collection_time", "latitude", "longitude",
                  "usable_for_trajectory"}
        need_m = {inp["movement_from_column"], inp["movement_to_column"], inp["movement_accepted_column"]}
        if need_o - have_o or need_m - have_m:
            raise PreconditionError(f"input schema mismatch: missing {sorted(need_o - have_o)} {sorted(need_m - have_m)}")
        f, t, a = inp["movement_from_column"], inp["movement_to_column"], inp["movement_accepted_column"]
        sql = f"""
        WITH o AS (
          SELECT observation_id, device_key, trajectory_id, collection_time, latitude, longitude,
                 LAG(observation_id) OVER (PARTITION BY trajectory_id ORDER BY collection_time, observation_id) AS prev_id
          FROM read_parquet('{obs}') WHERE usable_for_trajectory AND trajectory_id IS NOT NULL)
        SELECT o.observation_id, o.device_key, o.trajectory_id, o.collection_time, o.latitude, o.longitude,
               (o.prev_id IS NULL OR m.{a} IS NOT TRUE) AS break_before
        FROM o LEFT JOIN read_parquet('{mov}') m ON m.{f} = o.prev_id AND m.{t} = o.observation_id
        ORDER BY o.trajectory_id, o.collection_time, o.observation_id"""
        return con.execute(sql).fetchnumpy()
    finally:
        con.close()


def stays_from_arrays(d: dict, radius_m: float, dwell_s: float, min_obs: int) -> list[tuple[int, int]]:
    t = d["collection_time"].astype("datetime64[s]").astype(np.int64)
    return detect_stay_points(t, d["latitude"], d["longitude"], d["break_before"], radius_m, dwell_s, min_obs)


def stay_table(d: dict, spans: list[tuple[int, int]]) -> dict:
    t = d["collection_time"].astype("datetime64[s]")
    cols: dict[str, list] = {k: [] for k in ("stay_id", "device_key", "trajectory_id", "start_time", "end_time",
                                              "dwell_s", "lat", "lon", "observation_count", "day", "hour")}
    for i, j in spans:
        cols["stay_id"].append(stay_id(str(d["trajectory_id"][i]), str(d["observation_id"][i])))
        cols["device_key"].append(str(d["device_key"][i]))
        cols["trajectory_id"].append(str(d["trajectory_id"][i]))
        cols["start_time"].append(str(t[i]).replace("T", " "))
        cols["end_time"].append(str(t[j]).replace("T", " "))
        cols["dwell_s"].append(float((t[j] - t[i]).astype(np.int64)))
        cols["lat"].append(float(np.mean(d["latitude"][i:j + 1])))
        cols["lon"].append(float(np.mean(d["longitude"][i:j + 1])))
        cols["observation_count"].append(j - i + 1)
        cols["day"].append(str(t[i])[:10])
        cols["hour"].append(int(str(t[i])[11:13]))
    return cols


def csv_to_parquet(rows: list[dict], columns: dict[str, str], parquet: Path, scratch: Path) -> str:
    """Write rows through a scratch CSV (explicit types, no pandas/pyarrow) and return a content hash."""
    scratch.parent.mkdir(parents=True, exist_ok=True)
    with scratch.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(columns), lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    digest = hashlib.sha256(scratch.read_bytes()).hexdigest()
    con = duckdb.connect()
    try:
        spec = ", ".join(f"'{k}': '{v}'" for k, v in columns.items())
        con.execute(f"COPY (SELECT * FROM read_csv('{scratch}', header=true, columns={{{spec}}})) "
                    f"TO '{parquet}' (FORMAT parquet, COMPRESSION zstd)")
    finally:
        con.close()
    scratch.unlink()
    return digest


def write_csv(path: Path, header: list[str], rows: list[list]) -> str:
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(header)
        w.writerows(rows)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(root: Path, cfg: dict) -> dict:
    private = root / cfg["private_dir"]
    problems = check_preconditions(private)
    if problems:
        raise PreconditionError("GPS outputs are not complete/valid: " + "; ".join(problems))
    d = load_observations(private, cfg)
    sp, cl, dis, mt = cfg["stay_point"], cfg["clustering"], cfg["disclosure"], cfg["matching"]

    sens = []
    for r in sp["sensitivity_radius_m"]:
        for w in sp["sensitivity_dwell_seconds"]:
            tab = stay_table(d, stays_from_arrays(d, r, w, sp["min_observations"]))
            dw = np.asarray(tab["dwell_s"]) if tab["dwell_s"] else np.zeros(1)
            sens.append([r, w, len(tab["stay_id"]), len(set(tab["device_key"])), len(set(tab["day"])),
                         round(float(np.median(dw)), 1), round(float(np.quantile(dw, 0.9)), 1)])
    hashes = {"stop_sensitivity.csv": write_csv(
        private / "stop_sensitivity.csv",
        ["radius_m", "dwell_seconds", "stay_points", "distinct_devices", "distinct_days", "dwell_p50_s", "dwell_p90_s"], sens)}

    tab = stay_table(d, stays_from_arrays(d, sp["radius_m"], sp["dwell_seconds"], sp["min_observations"]))
    n = len(tab["stay_id"])
    labels = dbscan(tab["lat"], tab["lon"], cl["eps_m"], cl["min_samples"], tab["stay_id"]) if n else np.zeros(0, int)
    clusters = summarize_clusters(tab, labels, dis["min_distinct_devices"], dis["min_distinct_days"]) if n else []
    cluster_of = {}
    for c in clusters:
        for i in c["_members"]:
            cluster_of[i] = c["cluster_id"]

    stay_rows = [{"stay_id": tab["stay_id"][i], "device_key": tab["device_key"][i], "trajectory_id": tab["trajectory_id"][i],
                  "start_time": tab["start_time"][i], "end_time": tab["end_time"][i], "dwell_seconds": tab["dwell_s"][i],
                  "latitude": round(tab["lat"][i], 7), "longitude": round(tab["lon"][i], 7),
                  "observation_count": tab["observation_count"][i], "collection_date": tab["day"][i],
                  "start_hour_unconfirmed_tz": tab["hour"][i], "cluster_id": cluster_of.get(i, "")}
                 for i in sorted(range(n), key=lambda k: tab["stay_id"][k])]
    hashes["stop_points.parquet"] = csv_to_parquet(stay_rows, {
        "stay_id": "VARCHAR", "device_key": "VARCHAR", "trajectory_id": "VARCHAR", "start_time": "TIMESTAMP",
        "end_time": "TIMESTAMP", "dwell_seconds": "DOUBLE", "latitude": "DOUBLE", "longitude": "DOUBLE",
        "observation_count": "INTEGER", "collection_date": "DATE", "start_hour_unconfirmed_tz": "INTEGER",
        "cluster_id": "VARCHAR"}, private / "stop_points.parquet", private / ".stop_points.tmp.csv")

    cluster_cols = {"cluster_id": "VARCHAR", "centroid_latitude": "DOUBLE", "centroid_longitude": "DOUBLE",
                    "stop_count": "INTEGER", "distinct_devices": "INTEGER", "distinct_days": "INTEGER",
                    "dwell_p10_s": "DOUBLE", "dwell_p50_s": "DOUBLE", "dwell_p90_s": "DOUBLE",
                    "hour_profile_json": "VARCHAR", "spread_p50_m": "DOUBLE", "spread_p90_m": "DOUBLE",
                    "spread_max_m": "DOUBLE", "meets_disclosure_minimum": "BOOLEAN"}
    hashes["stop_clusters.parquet"] = csv_to_parquet(
        [{k: ("true" if c[k] is True else "false" if c[k] is False else c[k]) for k in cluster_cols} for c in clusters],
        cluster_cols, private / "stop_clusters.parquet", private / ".stop_clusters.tmp.csv")

    refs_feats = load_reference_features(root / cfg["reference_dir"] / "harbour_reference.geojson")
    refs = ReferenceIndex(refs_feats, set(mt["excluded_reference_classes"]))
    islands = IslandIndex(json.loads((root / cfg["islands_path"]).read_text(encoding="utf-8"))["features"], 0)
    out_rows, near_ref_ids = [], Counter()
    for c in clusters:
        res = classify_cluster(c, refs, islands, cfg, dis["min_distinct_devices"], dis["min_distinct_days"])
        for p, dist in res["nearest"]:
            if dist <= mt["reference_distance_m"]:
                near_ref_ids[p["harbour_ref_id"]] += 1
        nearest = res["nearest"]
        isl = res["island"]
        out_rows.append([
            c["cluster_id"], res["classification"], c["centroid_latitude"], c["centroid_longitude"], c["stop_count"],
            c["distinct_devices"], c["distinct_days"], c["dwell_p50_s"], c["spread_p90_m"], c["meets_disclosure_minimum"],
            "|".join(p["harbour_ref_id"] for p, _ in nearest), "|".join(p["name"] for p, _ in nearest),
            "|".join(p["feature_class"] for p, _ in nearest), "|".join(f"{dist:.1f}" for _, dist in nearest),
            isl["islandName"] if isl else "", isl["FCODE"] if isl else "",
            "" if res["island_distance_m"] is None else round(res["island_distance_m"], 1),
            "unreviewed", ""])
    header = ["cluster_id", "classification", "centroid_latitude", "centroid_longitude", "stop_count", "distinct_devices",
              "distinct_days", "dwell_p50_s", "spread_p90_m", "meets_disclosure_minimum", "nearest_ref_ids",
              "nearest_ref_names", "nearest_ref_classes", "nearest_ref_distances_m", "nearest_island_name",
              "nearest_island_id", "nearest_island_distance_m", "review_status", "reviewer_note"]
    hashes["harbour_candidate_matches.csv"] = write_csv(private / "harbour_candidate_matches.csv", header, out_rows)
    unmatched = [[p["harbour_ref_id"], p["name"], p["feature_class"], p["study_area_code"], p["longitude"], p["latitude"]]
                 for p in sorted(refs.props, key=lambda p: p["harbour_ref_id"]) if p["harbour_ref_id"] not in near_ref_ids]
    hashes["harbour_reference_without_gps_cluster.csv"] = write_csv(
        private / "harbour_reference_without_gps_cluster.csv",
        ["harbour_ref_id", "name", "feature_class", "study_area_code", "longitude", "latitude"], unmatched)

    summary = {"stay_points": n, "clusters": len(clusters),
               "classification_counts": dict(Counter(r[1] for r in out_rows)),
               "disclosure_eligible_clusters": sum(1 for c in clusters if c["meets_disclosure_minimum"]),
               "references_without_gps_cluster": len(unmatched), "output_content_hashes": hashes,
               "config_sha256": hashlib.sha256((root / "config" / "harbour_stops.json").read_bytes()).hexdigest(),
               "note": "GPS clusters are candidates only. Nothing here is a confirmed terminal; confirmation needs human review."}
    (private / "stop_run_manifest.json").write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=str(ROOT / "config" / "harbour_stops.json"))
    args = ap.parse_args()
    try:
        print(json.dumps(run(ROOT, load_config(Path(args.config))), indent=2))
    except PreconditionError as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
