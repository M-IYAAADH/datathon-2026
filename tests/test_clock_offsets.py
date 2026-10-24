"""Clock classification, correction and exclusion. No network, small fixtures, no real device keys."""
import json
import re
import sys
from datetime import date
from pathlib import Path

import duckdb
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from vessel_pipeline import build, clock  # noqa: E402
from vessel_pipeline.config import load_config  # noqa: E402
from test_vessel_services import CFG, DAY0, make_sites  # noqa: E402

CC = CFG["clock"]


def row(dev, day, n, med, share=1.0):
    return {"device_key": dev, "day": day, "n_obs": n, "median_diff": med, "consistent_share": share}


# ------------------------------------------------------------------------- thresholds

def test_clock_thresholds_classify_by_median_and_consistency():
    f = lambda n, med, sh=1.0: clock.classify_device_day(n, med, sh, CC)[0]  # noqa: E731
    assert f(100, 0) == "local" and f(100, 900) == "local" and f(100, -60) == "local"
    assert f(100, 901) == "other" and f(100, -61) == "other"  # consistent, not near 5 h
    assert f(100, 18000) == "offset_5h" and f(100, 16199) == "other" and f(100, 16200) == "offset_5h"
    assert f(100, 19800) == "offset_5h" and f(100, 19801) == "other"
    assert f(19, 18000) == "unknown" and f(20, 18000) == "offset_5h"
    # a 5 h median with a wide spread is delayed upload, not a clock offset
    assert f(100, 18000, 0.79) == "unknown" and f(100, 18000, 0.80) == "offset_5h"
    # a local median stays local even with a heavy tail of delayed uploads
    assert f(100, 3, 0.3) == "local"
    assert clock.classify_device_day(100, None, None, CC)[0] == "unknown"
    assert f(100, -18000) == "other"  # the opposite sign is never treated as the 5 h offset


def test_unusual_hour_is_never_an_input():
    # the classifier takes counts, a median difference and a spread only: no time of day argument exists
    import inspect
    assert list(inspect.signature(clock.classify_device_day).parameters) == ["n_obs", "median_diff", "consistent_share", "cc"]


# ------------------------------------------------------------------------- correction

def test_correction_applies_only_to_offset_class_and_keeps_raw_time():
    t = DAY0 + 3600
    assert clock.corrected_time(t, "offset_5h", CC) == t + 18000
    for c in ("local", "other", "unknown"):
        assert clock.corrected_time(t, c, CC) == t
    con = duckdb.connect()
    con.execute("CREATE TABLE g AS SELECT * FROM (VALUES ('o1','d1',TIMESTAMP '2025-08-02 03:00:00'),"
                "('o2','d2',TIMESTAMP '2025-08-02 03:00:00')) t(observation_id, device_key, collection_time)")
    con.execute("CREATE TABLE c AS SELECT * FROM (VALUES ('d1',DATE '2025-08-02','offset_5h',18000),"
                "('d2',DATE '2025-08-02','local',0)) t(device_key, day, clock_class, correction_seconds)")
    r = con.execute("""SELECT g.observation_id, g.collection_time,
        g.collection_time + to_seconds(coalesce(c.correction_seconds,0)::BIGINT) AS corrected, c.clock_class
        FROM g LEFT JOIN c ON c.device_key=g.device_key AND c.day=CAST(g.collection_time AS DATE) ORDER BY 1""").fetchall()
    assert str(r[0][1]) == "2025-08-02 03:00:00" and str(r[0][2]) == "2025-08-02 08:00:00"  # raw kept beside corrected
    assert str(r[1][1]) == str(r[1][2])


def test_correction_does_not_overwrite_collection_time_in_loader_arrays(tmp_path):
    """build.load_observations keeps t_raw and t_corr as separate arrays."""
    import inspect
    src = inspect.getsource(build.load_observations)
    assert '"t_raw"' in src and '"t_corr"' in src


# ------------------------------------------------------------------------- switching and inheritance

def test_device_that_switches_class_is_reported_and_keeps_own_days():
    days = [row("dev_sw", date(2025, 8, d), 100, 2.0) for d in range(1, 4)] + \
           [row("dev_sw", date(2025, 8, d), 100, 18010.0, 0.95) for d in range(4, 7)]
    resolved, devices = clock.classify_devices(days, CC)
    dev = devices[0]
    assert dev["switches_class"] and not dev["stable"] and dev["device_class"] == "switching"
    assert {r["clock_class"] for r in resolved} == {"local", "offset_5h"}  # each day keeps its own measured class
    assert [r["clock_class"] for r in resolved][:3] == ["local"] * 3


def test_stable_device_inherits_class_for_sparse_days_but_unstable_does_not():
    stable = [row("stable", date(2025, 8, d), 100, 1.0) for d in range(1, 6)] + [row("stable", date(2025, 8, 9), 5, 40000.0, 0.1)]
    shaky = [row("shaky", date(2025, 8, 1), 100, 1.0), row("shaky", date(2025, 8, 2), 5, 0.0)]
    resolved, devices = clock.classify_devices(stable + shaky, CC)
    by = {(r["device_key"], str(r["day"])): r for r in resolved}
    assert by[("stable", "2025-08-09")]["clock_class"] == "local"
    assert by[("stable", "2025-08-09")]["class_reason"].startswith("inherited_device_class")
    assert by[("stable", "2025-08-09")]["measured_class"] == "unknown"
    assert by[("shaky", "2025-08-02")]["clock_class"] == "unknown"  # only one measured day: no stable device class


def test_stable_offset_device_is_corrected_on_every_day():
    days = [row("off", date(2025, 8, d), 80, 18005.0, 0.9) for d in range(1, 5)] + [row("off", date(2025, 8, 9), 3, 5.0)]
    resolved, devices = clock.classify_devices(days, CC)
    assert devices[0]["device_class"] == "offset_5h" and devices[0]["stable"]
    assert all(r["clock_class"] == "offset_5h" for r in resolved)
    assert [clock.correction_for(r["clock_class"], CC) for r in resolved] == [18000] * 5


# ------------------------------------------------------------------------- exclusion from clustering

def spec_journeys(clock_by_dev, n_days=10):
    out = []
    k = 0
    for d in range(1, n_days + 1):
        for dev, cls in clock_by_dev.items():
            t0 = DAY0 + (d - 1) * 86400 + 430 * 60
            vs = [{"zone": 0, "arrive": t0 - 600, "depart": t0, "arrive_known": True, "depart_known": True, "unconfirmed": False},
                  {"zone": 1, "arrive": t0 + 1800, "depart": t0 + 2100, "arrive_known": True, "depart_known": True, "unconfirmed": False},
                  {"zone": 2, "arrive": t0 + 3600, "depart": t0 + 3900, "arrive_known": True, "depart_known": False, "unconfirmed": False}]
            out.append({"traj": k, "device": dev, "cls": "fast_vessel", "direction": "away_from_male", "visits": vs,
                        "islands": ("A", "B", "C"), "clock": cls})
            k += 1
    return out


def agg(js, exclude):
    A = {"t": np.zeros(0, np.int64), "traj": np.zeros(0, np.int64), "traj_device": np.array(sorted({j["device"] for j in js}), object)}
    day_dev = {date(2025, 8, d): 100 for d in range(1, 32)}
    return build.aggregate({"journeys": js, "trips": []}, A, np.zeros(0, np.int16), np.zeros(0, np.float32), make_sites(), day_dev,
                           CFG, 150, frozenset(exclude))


def test_unknown_and_other_excluded_from_clustering_but_counted():
    js = spec_journeys({"a": "local", "b": "local", "c": "local", "u": "unknown", "o": "other"})
    r = agg(js, clock.excluded_classes(CC))
    assert r["clock_excluded_journeys"] == {"unknown": 10, "other": 10}
    assert r["services"] and r["services"][0]["trips"] == 30  # only the three local devices
    cats = {id(j): r["journey_category"][id(j)][0] for j in js}
    assert all(cats[id(j)] == "not_regular_excluded_clock_class" for j in js if j["clock"] in ("unknown", "other"))
    assert all(cats[id(j)] == "regular_service" for j in js if j["clock"] == "local")
    r_all = agg(js, [])
    assert r_all["services"][0]["trips"] == 50 and r_all["clock_excluded_journeys"] == {}
    assert clock.excluded_classes(CC, "") == set() and clock.excluded_classes(CC, "other") == {"other"}


def test_only_excluded_devices_means_no_regular_service():
    js = spec_journeys({"u1": "unknown", "u2": "unknown", "u3": "other"})
    assert agg(js, clock.excluded_classes(CC))["services"] == []


# ------------------------------------------------------------------------- determinism and privacy

def test_classification_and_aggregation_are_deterministic_and_leak_no_keys():
    days = [row(f"secretdevicekey{i}" + "z" * 40, date(2025, 8, d), 50, float(i % 3)) for i in range(5) for d in range(1, 6)]
    a, b = clock.classify_devices(days, CC), clock.classify_devices(list(reversed(days)), CC)
    assert json.dumps(a, default=str, sort_keys=True) == json.dumps(b, default=str, sort_keys=True)
    js = spec_journeys({"secretdevicekeyA" + "q" * 40: "local", "secretdevicekeyB" + "q" * 40: "local", "secretdevicekeyC" + "q" * 40: "local"})
    r1, r2 = agg(js, clock.excluded_classes(CC)), agg(js, clock.excluded_classes(CC))
    keys = ("signatures", "departures", "edges", "service_days", "services", "clock_excluded_journeys")
    j1 = json.dumps({k: r1[k] for k in keys}, sort_keys=True, default=str)
    assert j1 == json.dumps({k: r2[k] for k in keys}, sort_keys=True, default=str)
    assert "secretdevicekey" not in j1 and "device_key" not in j1


def test_no_device_key_in_shareable_outputs():
    out = ROOT / "data" / "derived"
    key = re.compile(r"\b[0-9a-f]{32,}\b")
    for p in out.rglob("*"):
        if p.suffix in (".csv", ".json") and p.is_file():
            text = "\n".join(ln for ln in p.read_text(encoding="utf-8", errors="ignore").splitlines() if "sha256" not in ln)
            assert "device_key" not in text.replace("distinct_devices", ""), p
            assert not key.search(text), p


def test_clock_config_is_provisional_and_timezone_stays_unconfirmed():
    assert "PROVISIONAL" in json.loads((ROOT / "config" / "clock_offset.json").read_text())["note"]
    assert CFG["timezone"]["timezone_confirmed"] is False
    assert load_config()["clock"] == CC
