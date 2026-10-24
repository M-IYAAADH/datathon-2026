import csv
import json
import shutil
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
import numpy as np
import pytest
from shapely.geometry import Point, mapping, box

import build_stop_candidates as bsc
from harbour_pipeline.match import ReferenceIndex, check_preconditions, classify_cluster
from harbour_pipeline.reference import IslandIndex
from harbour_pipeline.stops import dbscan, detect_stay_points, summarize_clusters

ROOT = Path(__file__).resolve().parents[1]
LAT0, LON0 = 4.0, 73.0
M = 1 / 111_320  # degrees per metre of latitude


def track(offsets_m, step_s=60, breaks=()):
    n = len(offsets_m)
    lat = np.array([LAT0 + o * M for o in offsets_m])
    lon = np.full(n, LON0)
    t = np.arange(n) * step_s
    brk = np.zeros(n, bool)
    brk[0] = True
    for b in breaks:
        brk[b] = True
    return t, lat, lon, brk


def test_stay_detected_within_radius_and_dwell():
    t, lat, lon, brk = track([0, 5, 3, 8, 2, 4, 6, 1, 3, 5, 200, 400])
    assert detect_stay_points(t, lat, lon, brk, 50, 300) == [(0, 9)]
    assert detect_stay_points(t, lat, lon, brk, 50, 3600) == []  # dwell too short
    assert detect_stay_points(t, lat, lon, brk, 1, 300) == []  # radius too tight


def test_stay_not_bridged_across_split_or_rejected_transition():
    # 12 stationary fixes, but the transition into index 6 is rejected / a trajectory split
    t, lat, lon, brk = track([0] * 12, breaks=(6,))
    spans = detect_stay_points(t, lat, lon, brk, 50, 300)
    assert spans == [(0, 5), (6, 11)]
    assert all(not (i < 6 <= j) for i, j in spans)
    # with a break after every second fix no window reaches the 300 s dwell, so nothing is invented
    t, lat, lon, brk = track([0] * 12, step_s=60, breaks=range(2, 12, 2))
    assert detect_stay_points(t, lat, lon, brk, 50, 300) == []


def points_two_blobs():
    rng = np.random.default_rng(7)
    a = [(LAT0 + rng.normal(0, 10) * M, LON0 + rng.normal(0, 10) * M) for _ in range(20)]
    b = [(LAT0 + 0.05 + rng.normal(0, 10) * M, LON0 + rng.normal(0, 10) * M) for _ in range(15)]
    far = [(LAT0 + 0.2, LON0 + 0.2)]
    return a + b + far


def test_dbscan_deterministic_under_shuffle():
    pts = points_two_blobs()
    ids = [f"s{i:03d}" for i in range(len(pts))]
    lat, lon = [p[0] for p in pts], [p[1] for p in pts]
    base = dbscan(lat, lon, 75, 3, ids)
    assert len(set(base.tolist()) - {-1}) == 2 and base[-1] == -1
    for seed in (1, 2, 3):
        perm = np.random.default_rng(seed).permutation(len(pts))
        lab = dbscan([lat[i] for i in perm], [lon[i] for i in perm], 75, 3, [ids[i] for i in perm])
        parts = {frozenset(ids[perm[k]] for k in np.flatnonzero(lab == c)) for c in set(lab.tolist()) - {-1}}
        ref = {frozenset(ids[k] for k in np.flatnonzero(base == c)) for c in set(base.tolist()) - {-1}}
        assert parts == ref
    assert (dbscan(lat, lon, 75, 3, ids) == base).all()


def stays_for(devices, days, n=1):
    rows = {k: [] for k in ("lat", "lon", "device_key", "day", "dwell_s", "hour", "stay_id")}
    for d in range(devices):
        for dy in range(days):
            for _ in range(n):
                rows["lat"].append(LAT0); rows["lon"].append(LON0); rows["device_key"].append(f"dev{d}")
                rows["day"].append(f"2025-08-{dy + 1:02d}"); rows["dwell_s"].append(600.0 + d)
                rows["hour"].append(8 + dy); rows["stay_id"].append(f"{d}-{dy}")
    return rows


def test_cluster_stats_and_disclosure_thresholds():
    for devices, days, expected in [(5, 3, True), (4, 3, False), (5, 2, False), (6, 4, True)]:
        st = stays_for(devices, days)
        c = summarize_clusters(st, np.zeros(len(st["lat"]), int), 5, 3)[0]
        assert (c["distinct_devices"], c["distinct_days"]) == (devices, days)
        assert c["meets_disclosure_minimum"] is expected
        assert c["stop_count"] == devices * days and c["cluster_id"] == "sc_00000"
        assert sum(json.loads(c["hour_profile_json"])) == devices * days
        assert c["dwell_p10_s"] <= c["dwell_p50_s"] <= c["dwell_p90_s"] and c["spread_max_m"] < 1


def ref_features():
    def f(rid, cls, lon, lat):
        return {"type": "Feature", "geometry": mapping(Point(lon, lat)),
                "properties": {"harbour_ref_id": rid, "name": rid, "feature_class": cls, "study_area_code": "K",
                               "longitude": lon, "latitude": lat}}
    return [f("ref_pier", "pier", 73.0, 4.0), f("ref_endpoint", "ferry_route_endpoint", 73.1, 4.1)]


def islands_for_tests():
    return IslandIndex([{"type": "Feature", "properties": {"FCODE": "I1", "islandName": "Isle", "atoll": "Kaafu"},
                         "geometry": mapping(box(73.1, 4.1, 73.11, 4.11))}], 0)


def cluster_at(lon, lat, devices=6, days=4):
    return {"centroid_longitude": lon, "centroid_latitude": lat, "distinct_devices": devices, "distinct_days": days}


def test_classification_matched_near_island_offshore_insufficient():
    cfg = json.loads((ROOT / "config" / "harbour_stops.json").read_text())
    refs = ReferenceIndex(ref_features(), set(cfg["matching"]["excluded_reference_classes"]))
    isl = islands_for_tests()

    def run(c):
        return classify_cluster(c, refs, isl, cfg, 5, 3)

    assert run(cluster_at(73.0, 4.0 + 100 * M))["classification"] == "matched_reference"
    assert run(cluster_at(73.0, 4.0 + 400 * M))["classification"] == "offshore"  # beyond 150 m, no island
    assert run(cluster_at(73.105, 4.105))["classification"] == "near_island_no_reference"  # on the island
    # the route endpoint is evidence only: a cluster sitting on it never matches a reference
    assert run(cluster_at(73.1, 4.1 - 30 * M))["classification"] != "matched_reference"
    assert run(cluster_at(73.0, 4.0, devices=2))["classification"] == "insufficient"
    assert run(cluster_at(73.0, 4.0, days=1))["classification"] == "insufficient"


def write_manifest(d, status="complete", validation=None):
    (d / "run_manifest.json").write_text(json.dumps({"status": status, "validation": validation or {"a": True}}))
    (d / "regional_run_manifest.json").write_text(json.dumps({"status": "complete", "validation": {"b": True, "c": {"d": True}}}))


def test_preconditions_gate(tmp_path):
    assert any("missing" in p for p in check_preconditions(tmp_path))
    write_manifest(tmp_path)
    assert check_preconditions(tmp_path) == []
    write_manifest(tmp_path, status="running")
    assert check_preconditions(tmp_path)
    write_manifest(tmp_path, validation={"a": True, "b": False})
    assert check_preconditions(tmp_path)
    write_manifest(tmp_path)
    for blocker in ("failed_run.json", "regional_failed_run.json", ".run.lock"):
        (tmp_path / blocker).write_text("x")
        assert any(blocker in p for p in check_preconditions(tmp_path))
        (tmp_path / blocker).unlink()


SECRET_KEY = "devkey_SECRET_0123456789abcdef"


def build_workspace(tmp_path):
    root = tmp_path / "ws"
    private = root / "data/private/followme"
    ref = root / "data/reference/harbours"
    (root / "config").mkdir(parents=True)
    private.mkdir(parents=True)
    ref.mkdir(parents=True)
    shutil.copy(ROOT / "config/harbour_stops.json", root / "config/harbour_stops.json")
    cfg = json.loads((root / "config/harbour_stops.json").read_text())
    write_manifest(private)
    # 7 devices x 4 days stay 15 min at the pier; one device stays at sea (single device) and one trajectory is split
    con = duckdb.connect()
    con.execute("CREATE TABLE o(observation_id VARCHAR, device_key VARCHAR, trajectory_id VARCHAR, collection_time TIMESTAMP,"
                " latitude DOUBLE, longitude DOUBLE, usable_for_trajectory BOOLEAN)")
    con.execute("CREATE TABLE m(from_observation_id VARCHAR, to_observation_id VARCHAR, transition_accepted BOOLEAN)")
    obs, mov = [], []

    def add_traj(dev, day, lat, lon, tid, rejected_at=None, n=12):
        prev = None
        for k in range(n):
            oid = f"{tid}-{k}"
            ts = datetime(2025, 8, day, 9, 0) + timedelta(minutes=k * 2)
            obs.append((oid, dev, tid, ts, lat + (k % 3) * M, lon, True))
            if prev:
                mov.append((prev, oid, k != rejected_at))
            prev = oid

    for dev in range(7):
        for day in range(1, 5):
            add_traj(f"{SECRET_KEY}{dev}", day, 4.0, 73.0, f"t{dev}-{day}")
    for day in (1, 2, 3):
        add_traj(f"{SECRET_KEY}solo", day, 4.5, 73.5, f"tsea{day}")
    add_traj(f"{SECRET_KEY}rej", 2, 4.3, 73.3, "trej", rejected_at=6)
    con.executemany("INSERT INTO o VALUES (?,?,?,?,?,?,?)", obs)
    con.executemany("INSERT INTO m VALUES (?,?,?)", mov)
    con.execute(f"COPY o TO '{private}/gps_region_clean.parquet' (FORMAT parquet)")
    con.execute(f"COPY m TO '{private}/movement_segments.parquet' (FORMAT parquet)")
    con.close()
    feats = ref_features()
    (ref / "harbour_reference.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": feats}))
    (root / "isl.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"FCODE": "I1", "islandName": "Isle", "atoll": "Kaafu"},
         "geometry": mapping(box(73.1, 4.1, 73.11, 4.11))}]}))
    cfg["islands_path"] = "isl.geojson"
    return root, cfg, private


def test_end_to_end_synthetic_run_is_private_deterministic_and_correct(tmp_path):
    root, cfg, private = build_workspace(tmp_path)
    first = bsc.run(root, cfg)
    assert first["classification_counts"] == {"matched_reference": 1, "insufficient": 1}
    assert first["stay_points"] == 28 + 3 + 2 and first["disclosure_eligible_clusters"] == 1
    rows = list(csv.DictReader((private / "harbour_candidate_matches.csv").open()))
    pier = [r for r in rows if r["classification"] == "matched_reference"][0]
    assert pier["nearest_ref_ids"].startswith("ref_pier") and pier["meets_disclosure_minimum"] == "True"
    assert int(pier["distinct_devices"]) == 7 and int(pier["distinct_days"]) == 4
    assert pier["review_status"] == "unreviewed" and "reviewer_note" in pier
    # the sea cluster has one device only and is never disclosure-eligible
    sea = [r for r in rows if r["classification"] == "insufficient"][0]
    assert sea["distinct_devices"] == "1" and sea["meets_disclosure_minimum"] == "False"
    con = duckdb.connect()
    stays = con.execute(f"SELECT trajectory_id, count(*) FROM read_parquet('{private}/stop_points.parquet') GROUP BY 1").fetchall()
    con.close()
    per_traj = dict(stays)
    assert per_traj["trej"] == 2  # split at the rejected transition: two stays, none bridging it
    assert (private / "stop_sensitivity.csv").exists()
    # no device key outside data/private
    for p in root.rglob("*"):
        if p.is_file() and private not in p.parents and p.suffix in {".csv", ".json", ".geojson", ".md"}:
            assert SECRET_KEY not in p.read_text(encoding="utf-8"), p
    for name in ("harbour_candidate_matches.csv", "stop_clusters.parquet", "stop_run_manifest.json"):
        blob = (private / name).read_bytes()
        assert SECRET_KEY.encode() not in blob, name
    # identical inputs and config -> identical outputs
    second = bsc.run(root, cfg)
    assert second["output_content_hashes"] == first["output_content_hashes"]


def test_run_refuses_incomplete_gps_outputs(tmp_path):
    root, cfg, private = build_workspace(tmp_path)
    (private / "failed_run.json").write_text("{}")
    with pytest.raises(bsc.PreconditionError):
        bsc.run(root, cfg)
    assert not (private / "stop_points.parquet").exists()
