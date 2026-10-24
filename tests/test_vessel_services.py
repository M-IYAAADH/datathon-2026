import json
import json
import re
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pytest
from shapely.geometry import box

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from vessel_pipeline import build, geo, mtcc  # noqa: E402
from vessel_pipeline.classify import classify_device  # noqa: E402
from vessel_pipeline.config import load_config  # noqa: E402
from vessel_pipeline.journeys import build_visits, split_journeys  # noqa: E402
from vessel_pipeline.services import (build_canonical, cluster_departures, passes_disclosure,  # noqa: E402
                                      skipped_in, weekday_pattern)
from vessel_pipeline.stops import detect_stops  # noqa: E402

CFG = load_config()
DAY0 = (date(2025, 8, 1) - date(1970, 1, 1)).days * 86400


def seq(near_flags, step=60, breaks=(), zone_id=0, dist_in=10.0):
    """Observations ordered in time; True = inside zone 0."""
    n = len(near_flags)
    t = np.arange(n, dtype=np.int64) * step
    brk = np.zeros(n, bool)
    brk[0] = True
    for b in breaks:
        brk[b] = True
    zone = np.where(near_flags, zone_id, -1).astype(np.int16)
    dist = np.where(near_flags, dist_in, np.inf).astype(np.float32)
    jet = np.where(near_flags, 0, -1).astype(np.int16)
    return t, brk, zone, dist, jet


def stops_for(args, radius=150, dwell=120, gap=300):
    t, brk, zone, dist, jet = args
    return detect_stops(t, brk, zone, dist, jet, radius, dwell, 2, gap)


# ------------------------------------------------------------------------------- class thresholds

def ev(p90, **kw):
    base = {"moving_segments": 50, "p90_knots": p90, "max_run_path_m": 5000, "days_with_run": 3, "moving_share_off_land": 0.9}
    base.update(kw)
    return base


def test_class_thresholds():
    assert classify_device(ev(18.0), CFG)[0] == "fast_vessel"
    assert classify_device(ev(17.9), CFG)[0] == "mixed"
    assert classify_device(ev(14.0), CFG)[0] == "mixed"
    assert classify_device(ev(13.9), CFG)[0] == "slow_vessel"
    assert classify_device(ev(20, moving_segments=0), CFG)[0] == "unclassified"
    assert classify_device(ev(20, max_run_path_m=1999), CFG)[0] == "unclassified"
    assert classify_device(ev(20, moving_share_off_land=0.39), CFG)[0] == "unclassified"
    assert classify_device(ev(20, days_with_run=1), CFG)[1] == "low"
    assert classify_device(ev(20, days_with_run=2), CFG)[1] == "high"


def test_flags_unconfirmed_in_config():
    assert CFG["timezone"]["timezone_confirmed"] is False
    assert CFG["speed"]["speed_unit_confirmed"] is False


# ---------------------------------------------------------------------------------- stop detection

def test_stop_found_with_dwell_and_radius():
    s = stops_for(seq([False, True, True, True, False]))
    assert len(s["first"]) == 1 and s["n_obs"][0] == 3 and not s["unconfirmed"][0]
    assert not s["start_truncated"][0] and not s["end_truncated"][0]
    assert len(stops_for(seq([False, True, True, False]), dwell=300)["first"]) == 0
    assert len(stops_for(seq([False, True, True, True, False]), radius=5)["first"]) == 0  # dist_in 10 m


def test_stop_never_bridges_a_split_or_rejected_transition():
    flags = [True, True, True, True]
    assert len(stops_for(seq(flags), dwell=120)["first"]) == 1
    # a break (trajectory split or non-accepted transition) in the middle: two runs of 60 s, below dwell
    assert len(stops_for(seq(flags, breaks=(2,)), dwell=120)["first"]) == 0
    two = stops_for(seq(flags, breaks=(2,)), dwell=60)
    assert len(two["first"]) == 2 and two["end_truncated"][0] and two["start_truncated"][1]


def test_gap_inside_stop_is_flagged_not_filled():
    t, brk, zone, dist, jet = seq([True, True, True])
    t = np.array([0, 60, 900], np.int64)
    s = detect_stops(t, brk, zone, dist, jet, 150, 120, 2, 300)
    assert len(s["first"]) == 1 and s["unconfirmed"][0] and s["max_gap"][0] == 840 and s["n_obs"][0] == 3


def test_stops_are_deterministic():
    a = stops_for(seq([False, True, True, True, False, True, True, True]))
    b = stops_for(seq([False, True, True, True, False, True, True, True]))
    assert all(np.array_equal(a[k], b[k]) for k in a)


# ------------------------------------------------------------------- sequences and partial matching

def test_visit_sequence_collapses_repeated_stops():
    stops = {"first": np.array([0, 5, 9]), "zone": np.array([1, 1, 2], np.int16), "jetty": np.array([0, 0, 0], np.int16),
             "start": np.array([0, 300, 900]), "end": np.array([120, 420, 1000]), "n_obs": np.array([3, 3, 2]),
             "unconfirmed": np.array([False, True, False]), "start_truncated": np.array([True, False, False]),
             "end_truncated": np.array([False, False, True])}
    v = build_visits(stops, np.zeros(20, int))[0]
    assert [x["zone"] for x in v] == [1, 2]
    assert v[0]["arrive"] == 0 and v[0]["depart"] == 420 and v[0]["unconfirmed"] and not v[0]["arrive_known"]


def test_split_journeys_on_direction_reversal():
    vs = [{"zone": z} for z in (0, 1, 2, 1, 0)]
    out = split_journeys(vs, [30.0, 20.0, 10.0])  # distance to Male per zone
    assert [(d, [v["zone"] for v in j]) for d, j in out] == [("toward_male", [0, 1, 2]), ("away_from_male", [2, 1, 0])]


def test_partial_run_matches_longer_route_with_one_skip():
    assert skipped_in(("A", "C"), ("A", "B", "C"), 1) == ("B",)
    assert skipped_in(("A", "B"), ("A", "B", "C"), 1) == ("C",)
    assert skipped_in(("C", "A"), ("A", "B", "C"), 1) is None  # order matters
    assert skipped_in(("A", "D"), ("A", "B", "C", "D"), 1) is None  # two stops skipped
    d = lambda n: {"days": set(range(n)), "devices": {1, 2, 3}}  # noqa: E731
    groups = {(("A", "B", "C"), "x"): d(20), (("A", "C"), "x"): d(3), (("B", "C"), "x"): d(2)}
    m = build_canonical(groups, 1, 8)
    assert m[(("A", "C"), "x")] == ((("A", "B", "C"), "x"), ("B",))
    assert m[(("B", "C"), "x")] == ((("A", "B", "C"), "x"), ("A",))
    assert m[(("A", "B", "C"), "x")][1] == ()


# ------------------------------------------------------------- clustering, N days, weekday pattern

def test_departure_clustering_groups_by_time_not_device():
    mins = [430, 432, 441, 435, 600, 603, 598, 431]
    days = [1, 2, 3, 4, 1, 2, 3, 5]
    lab = cluster_departures(mins, days, 15)
    assert len({lab[i] for i in (0, 1, 2, 3, 7)}) == 1 and len({lab[i] for i in (4, 5, 6)}) == 1
    assert lab[0] != lab[4]
    assert cluster_departures([430, 480], [1, 2], 15, min_days=2)[0] == -1  # too far apart for one window


def test_weekday_pattern_and_skipped_weekdays():
    start = date(2025, 8, 1)  # Friday
    dates = {date(2025, 8, d) for d in range(1, 32) if date(2025, 8, d).weekday() not in (5, 6)}
    w = weekday_pattern(dates, start, 31, 4)
    assert "Mon:4/4" in w["days_seen_by_weekday"] and "Fri:5/5" in w["days_seen_by_weekday"]
    assert w["weekdays_not_observed"] == "Sat,Sun" and "Sat:0/5" in w["days_seen_by_weekday"]
    assert weekday_pattern(dates | {date(2025, 8, 2), date(2025, 8, 3)}, start, 31, 4)["weekdays_not_observed"] == ""


def make_sites():
    return [{"idx": i, "code": c, "name": c, "category": "Residential Island", "atoll": "x", "jetties": [], "no_jetty_point": False,
             "anchor": (73.0 + i * 0.1, 4.0)} for i, c in enumerate(["A", "B", "C"])]


def synthetic_journeys(spec):
    """spec: list of (day, minute_of_day_at_A, device) for the route A>B>C."""
    out = []
    for k, (day, minute, dev) in enumerate(spec):
        t0 = DAY0 + (day - 1) * 86400 + minute * 60
        vs = [{"zone": 0, "arrive": t0 - 600, "depart": t0, "arrive_known": True, "depart_known": True, "unconfirmed": False},
              {"zone": 1, "arrive": t0 + 1800, "depart": t0 + 2100, "arrive_known": True, "depart_known": True, "unconfirmed": False},
              {"zone": 2, "arrive": t0 + 3600, "depart": t0 + 3900, "arrive_known": True, "depart_known": False, "unconfirmed": False}]
        out.append({"traj": k, "device": dev, "cls": "fast_vessel", "direction": "away_from_male", "visits": vs,
                    "islands": ("A", "B", "C")})
    return out


def run_agg(spec, cfg=CFG):
    js = synthetic_journeys(spec)
    A = {"t": np.zeros(0, np.int64), "traj": np.zeros(0, np.int64), "traj_device": np.array(sorted({j["device"] for j in js}), object)}
    sites = make_sites()
    day_dev = {date(2025, 8, d): 100 for d in range(1, 32)}
    return build.aggregate({"journeys": js, "trips": []}, A, np.zeros(0, np.int16), np.zeros(0, np.float32), sites, day_dev, cfg, 150)


def test_regular_service_needs_n_distinct_days():
    morning = [(d, 430 + (d % 3) * 5, f"dev{d % 4}") for d in range(1, 11)]  # 10 days, 4 devices, within 15 min
    evening = [(d, 1000, f"dev{d % 4}") for d in range(1, 8)]  # 7 days: below N = 8, above the disclosure minimum
    r = run_agg(morning + evening)
    by = {}
    for row in r["departures"]:
        by.setdefault(row["service_id"], row)
    cats = sorted(v["service_category"] for v in by.values())
    assert cats == ["not_regular_insufficient_days", "regular"]
    reg = next(v for v in by.values() if v["service_category"] == "regular")
    assert reg["days_seen"] == 10 and reg["time_p10"] >= "07:10" and reg["time_p90"] <= "07:30"
    assert r["signatures"][0]["regular_services"] == 1


def test_disclosure_thresholds_suppress_small_groups():
    assert passes_disclosure(3, 5, CFG) and not passes_disclosure(2, 9, CFG) and not passes_disclosure(9, 4, CFG)
    few_devices = [(d, 430, "dev0" if d % 2 else "dev1") for d in range(1, 12)]  # 11 days but only 2 devices
    r = run_agg(few_devices)
    assert r["services"] == [] and r["signatures"] == [] and r["departures"] == [] and r["service_days"] == []
    assert r["suppressed"]["signatures"] == 1 and r["suppressed"]["service_clusters"] >= 1
    few_days = [(d, 430, f"dev{d}") for d in range(1, 5)]  # 4 devices, 4 days
    assert run_agg(few_days)["signatures"] == []


def test_aggregation_is_deterministic_and_has_no_device_keys():
    spec = [(d, 430 + (d % 3) * 5, f"secretkey{d % 4}" + "a" * 50) for d in range(1, 12)]
    a, b = run_agg(spec), run_agg(spec)
    ja = json.dumps({k: a[k] for k in ("signatures", "departures", "edges", "service_days", "services")}, sort_keys=True, default=str)
    jb = json.dumps({k: b[k] for k in ("signatures", "departures", "edges", "service_days", "services")}, sort_keys=True, default=str)
    assert ja == jb and "secretkey" not in ja and "device_key" not in ja
    days = [r for r in a["service_days"] if r["status"] == "observed"]
    assert days and all(r["status"] in ("observed", "not_observed") for r in a["service_days"])
    assert "cancel" not in " ".join(r["status"] for r in a["service_days"])
    assert all(r["devices_seen"] in ("<3", 0) or int(r["devices_seen"]) >= 3 for r in a["service_days"])


# --------------------------------------------------------------------- jetties and the fallback

HEADER = ["harbour_ref_id", "island_id", "island_name", "feature_class", "name", "confidence", "source_feature_id",
          "longitude", "latitude", "review_status"]


def hrow(rid, island, cls, conf, sfid, name="", review="unreviewed", lon=73.0, lat=4.0):
    return dict(zip(HEADER, [rid, island, island, cls, name, conf, sfid, str(lon), str(lat), review]))


def test_jetty_selection_rules_and_fallback_rows():
    rows = [hrow("a", "LD1", "marina", "high", "way/123"), hrow("b", "LD1", "pier", "medium", "way/124"),
            hrow("c", "LD1", "ferry_terminal", "high", "way/abc"), hrow("d", "LD1", "ferry_route_endpoint", "high", "way/1#start"),
            hrow("e", "LD1", "ferry_terminal", "high", "node/5", name="Maafannu Sahara Bus Stop"),
            hrow("f", "LD1", "marina", "high", "node/6", review="rejected"), hrow("g", "LD2", "pier", "medium", "way/9")]
    used, skipped = geo.select_jetties(rows, HEADER, CFG, {"LD1"})
    assert [u["harbour_ref_id"] for u in used] == ["a"]
    reasons = {s["harbour_ref_id"]: s["reason"] for s in skipped}
    assert reasons["b"].startswith("confidence") and reasons["c"] == "no_numeric_osm_id"
    assert reasons["d"].startswith("excluded_feature_class") and reasons["e"] == "excluded_name_token" and reasons["f"] == "review_rejected"
    assert "g" not in reasons  # island outside scope
    verified = HEADER + ["verification_status"]
    r2 = [dict(hrow("x", "LD1", "marina", "low", "way/1"), verification_status="VERIFIED"),
          dict(hrow("y", "LD1", "marina", "high", "way/2"), verification_status="")]
    u2, s2 = geo.select_jetties(r2, verified, CFG, {"LD1"})
    assert [u["harbour_ref_id"] for u in u2] == ["x"] and s2[0]["reason"].startswith("verification_status")


def test_verification_overlay_adds_status_column(tmp_path):
    ref, ov = tmp_path / "ref.csv", tmp_path / "ov.csv"
    ref.write_text("harbour_ref_id,name\nx,A\ny,B\n", encoding="utf-8")
    ov.write_text("harbour_ref_id,verification_status\nx,VERIFIED\n", encoding="utf-8")
    rows, cols = geo.read_harbour_rows(ref, ov)
    assert "verification_status" in cols and [r["verification_status"] for r in rows] == ["VERIFIED", ""]
    rows, cols = geo.read_harbour_rows(ref, tmp_path / "missing.csv")
    assert "verification_status" not in cols


def test_no_jetty_point_fallback_uses_shoreline_never_centroid():
    isl = box(73.000, 4.000, 73.010, 4.010)  # about 1.1 km square
    islands = {"LD9": {"code": "LD9", "name": "Testisland", "atoll": "x", "category": "Residential Island", "geom": isl}}
    sites = geo.build_sites(["LD9"], [], islands)
    assert sites[0]["no_jetty_point"] and sites[0]["jetties"] == []
    lat = np.array([4.005, 4.0000, 4.0105, 4.020])
    lon = np.array([73.005, 73.005, 73.005, 73.005])
    zone, dist, _ = geo.assign_zones(lat, lon, sites, islands, 150)
    assert zone[0] == -1  # island centre, 550 m from shore: the centroid is not a jetty
    assert zone[1] == 0 and dist[1] < 5  # on the shoreline
    assert zone[2] == 0 and 40 < dist[2] < 100  # offshore side of the shore, inside the band
    assert zone[3] == -1


def test_island_shoreline_mode_ignores_jetties_for_every_island():
    isl = box(73.000, 4.000, 73.010, 4.010)
    islands = {"LD9": {"code": "LD9", "name": "Testisland", "atoll": "x", "category": "Residential Island", "geom": isl}}
    used = [{"island_code": "LD9", "longitude": 73.005, "latitude": 4.005, "harbour_ref_id": "j1"}]  # island centre
    sites = geo.build_sites(["LD9"], used, islands, "island_shoreline")
    assert sites[0]["shoreline"] and sites[0]["jetties"] == [] and not sites[0]["no_jetty_point"]
    zone, dist, _ = geo.assign_zones(np.array([4.005, 4.0000, 4.0105]), np.array([73.005] * 3), sites, islands, 150)
    assert zone[0] == -1 and zone[1] == 0 and zone[2] == 0  # the jetty point at the centre is not a stop location
    jsites = geo.build_sites(["LD9"], used, islands, "jetty")
    assert not jsites[0]["shoreline"] and len(jsites[0]["jetties"]) == 1


def test_config_default_stop_location_mode_is_island_shoreline():
    from vessel_pipeline.config import load_config
    assert load_config()["stops"]["stop_location_mode"] == "island_shoreline"


def test_jetty_site_uses_point_distance():
    islands = {"LD9": {"code": "LD9", "name": "T", "atoll": "x", "category": "Residential Island", "geom": box(73, 4, 73.01, 4.01)}}
    used = [{"island_code": "LD9", "longitude": 73.0, "latitude": 4.0, "harbour_ref_id": "j1"}]
    sites = geo.build_sites(["LD9"], used, islands)
    assert not sites[0]["no_jetty_point"]
    zone, dist, jet = geo.assign_zones(np.array([4.0005, 4.005]), np.array([73.0, 73.005]), sites, islands, 150)
    assert zone[0] == 0 and zone[1] == -1 and jet[0] == 0


# ------------------------------------------------------------------------------------- other

def test_mtcc_name_normalisation():
    assert mtcc.norm("Malé") == mtcc.norm("Male") == "male"
    assert mtcc.norm("Hulhumale’") == mtcc.norm("Hulhumalé")


def test_no_device_identifier_outside_private():
    private = ROOT / "data" / "private" / "followme"
    out = ROOT / "data" / "derived" / "vessels"
    if not out.exists():
        pytest.skip("no derived outputs yet")
    pattern = re.compile(r"\b[0-9a-f]{64}\b")
    keys = set()
    cls = private / "vessel_classes.parquet"
    if cls.exists():
        import duckdb
        keys = {r[0] for r in duckdb.connect().execute(f"SELECT device_key FROM read_parquet('{cls}')").fetchall()}
    for f in (x for x in out.rglob("*") if x.is_file()):
        text = f.read_text(encoding="utf-8")
        assert "device_key" not in text.split("\n", 1)[0], f.name
        if f.suffix == ".csv":  # the JSON summary legitimately carries sha256 file hashes
            assert not pattern.search(text), f"{f.name} contains a 64-hex identifier"
        assert not any(k in text for k in list(keys)[:2000]), f.name
    names = " ".join(p.name for p in out.rglob("*"))
    assert "parquet" not in names
    summary = out / "service_summary.json"
    if summary.exists() and json.loads(summary.read_text(encoding="utf-8")).get("stop_location", {}).get("stop_location_mode") == "island_shoreline":
        assert not (out / "jetty_selection.csv").exists()
        for f in out.glob("*.csv"):
            assert "jetty" not in f.read_text(encoding="utf-8").split("\n", 1)[0].lower(), f.name
