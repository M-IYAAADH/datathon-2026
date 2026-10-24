"""Tests for scripts/build_app_data.py: schema, references, chronology, disclosure, privacy, determinism."""
import json
import re

import pytest

import build_app_data as bad

FILES = ("stops", "legs", "config", "census", "meta")
BV = "test-build-0001"


@pytest.fixture(scope="module")
def out(tmp_path_factory):
    d = tmp_path_factory.mktemp("app_data")
    assert bad.main(["--out", str(d), "--build-version", BV]) == 0
    return d


@pytest.fixture(scope="module")
def b(out):
    return {n: json.loads((out / f"{n}.json").read_text(encoding="utf-8")) for n in FILES}


# ---- schema ----
STOP_KEYS = {"id", "name", "atoll", "lat", "lon", "censusKey", "kind"}
LEG_KEYS = {"id", "source", "routeId", "tripId", "from", "to", "dep", "arr", "days", "validFrom", "validTo", "cls", "evidence"}
EVIDENCE_KEYS = {"devices", "days", "weekdayObs", "period", "unconfirmedOriginShare"}
CONFIG_KEYS = {"timezone", "bufferOfficialMin", "bufferInferredMin", "longWaitMin", "maxTransfers", "horizonHours",
               "shiftStepMin", "maxShiftMin", "minDevices", "minDays", "buildVersion"}
CENSUS_KEYS = {"name", "population", "male", "female", "age0_17", "age18_64", "age65p", "disability",
               "disabilityEligible", "elderlyAlone"}
META_KEYS = {"buildVersion", "fixture", "evidencePeriod", "timezoneStatus", "speedUnitStatus", "clockCorrections",
             "timetableYear"}
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def test_stops_schema(b):
    assert b["stops"]
    for s in b["stops"]:
        assert set(s) == STOP_KEYS
        assert isinstance(s["lat"], float) and isinstance(s["lon"], float)
        assert s["kind"] in ("island", "hub")
    assert len({s["id"] for s in b["stops"]}) == len(b["stops"])


def test_legs_schema(b):
    for l in b["legs"]:
        assert set(l) == LEG_KEYS, l["id"]
        assert l["source"] in ("mtcc", "inferred")
        for r in (l["dep"], l["arr"]):
            assert set(r) == {"min", "max"} and all(isinstance(r[k], int) for k in r)
        assert l["days"] and l["days"] == sorted(set(l["days"])) and all(0 <= d <= 6 for d in l["days"])
        assert DATE.match(l["validFrom"]) and DATE.match(l["validTo"]) and l["validFrom"] <= l["validTo"]
        if l["source"] == "mtcc":
            assert l["cls"] is None and l["evidence"] is None
            assert l["dep"]["min"] == l["dep"]["max"] and l["arr"]["min"] == l["arr"]["max"]
        else:
            assert l["cls"] in ("fast", "slow")
            assert set(l["evidence"]) == EVIDENCE_KEYS
            assert len(l["evidence"]["weekdayObs"]) == 7
            assert l["evidence"]["period"] == "2025-08"
    assert len({l["id"] for l in b["legs"]}) == len(b["legs"])


def test_config_census_meta_schema(b):
    assert set(b["config"]) == CONFIG_KEYS
    assert b["config"]["minDevices"] == 3 and b["config"]["minDays"] == 8
    assert set(b["meta"]) == META_KEYS
    m = b["meta"]
    assert m["fixture"] is False and m["evidencePeriod"] == "2025-08"
    assert m["timezoneStatus"] == "unconfirmed" and m["speedUnitStatus"] == "unconfirmed"
    assert m["clockCorrections"] == 0 and m["timetableYear"] == 2026
    for k, v in b["census"].items():
        assert re.match(r"^LD\d{4}$", k) and set(v) == CENSUS_KEYS


# ---- references ----
def test_leg_ends_in_stops(b):
    ids = {s["id"] for s in b["stops"]}
    for l in b["legs"]:
        assert l["from"] in ids and l["to"] in ids, l["id"]
        assert l["from"] != l["to"], l["id"]


def test_census_keys(b):
    for s in b["stops"]:
        assert s["censusKey"] in b["census"], s["id"]
    assert set(b["census"]) == {s["censusKey"] for s in b["stops"]}


# ---- chronology ----
def test_chronology(b):
    for l in b["legs"]:
        assert l["dep"]["max"] >= l["dep"]["min"], l["id"]
        assert l["arr"]["max"] >= l["arr"]["min"], l["id"]
        assert l["arr"]["min"] >= l["dep"]["min"], l["id"]


def test_trip_legs_chain_in_time(b):
    by_trip = {}
    for l in b["legs"]:
        by_trip.setdefault((l["source"], l["tripId"]), []).append(l)
    for legs in by_trip.values():
        legs.sort(key=lambda l: l["dep"]["min"])
        for x, y in zip(legs, legs[1:]):
            assert y["dep"]["min"] >= x["dep"]["min"]


def test_after_midnight_normalisation():
    r = bad.normalise_after_midnight({"min": bad.hm("01:10"), "max": bad.hm("01:10")}, bad.hm("23:30"))
    assert r == {"min": 1510, "max": 1510}
    assert bad.normalise_after_midnight({"min": 1435, "max": 5}, 1400) == {"min": 1435, "max": 1445}


# ---- buildVersion ----
def test_single_build_version(b):
    assert b["config"]["buildVersion"] == b["meta"]["buildVersion"] == BV


def test_deterministic(tmp_path, out):
    d2 = tmp_path / "again"
    bad.main(["--out", str(d2), "--build-version", BV])
    for n in FILES:
        assert (out / f"{n}.json").read_bytes() == (d2 / f"{n}.json").read_bytes(), n


# ---- disclosure ----
def test_inferred_disclosure_minimums(b):
    inferred = [l for l in b["legs"] if l["source"] == "inferred"]
    assert inferred
    for l in inferred:
        assert l["evidence"]["devices"] >= b["config"]["minDevices"], l["id"]
        assert l["evidence"]["days"] >= b["config"]["minDays"], l["id"]


# ---- MTCC source restriction ----
def test_mtcc_only_2026_sources(b):
    for l in b["legs"]:
        if l["source"] == "mtcc":
            assert any(l["tripId"].startswith(s) for s in bad.MTCC_2026_SOURCES), l["id"]


# ---- aliases ----
def test_alias_table_exact_match_only():
    _, idx = bad.load_census(bad.REPO)
    assert bad.resolve_census_key("", "Malé", idx)[0] == "LD0442"
    assert bad.resolve_census_key("ADH", "Dha’ngethi", idx)[0] == "LD0273"
    assert bad.resolve_census_key("ADH", "Ku’nburudhoo", idx)[0] == "LD0771"
    assert bad.resolve_census_key("ADH", "Hangnaameedhoo", idx)[0] == "LD1023"
    assert bad.resolve_census_key("", "Villimalé", idx)[0] == "LD0268"
    maafushi = bad.resolve_census_key("K", "Maafushi", idx)
    kolamaafushi = bad.resolve_census_key("GA", "Kolamaafushi", idx)
    assert maafushi and maafushi[0] == "LD0069"
    assert kolamaafushi is None or kolamaafushi[0] != maafushi[0]
    ga_vil = bad.resolve_census_key("GA", "Villingili", idx)
    assert ga_vil is None or ga_vil[0] != "LD0268"


# ---- forbidden fields ----
FORBIDDEN_KEY = re.compile(r"imei|device_?id|device_?key|dates_seen|trajectory|track|lat_raw|lon_raw|mac|serial|msisdn",
                           re.I)


def _walk(o, path=""):
    if isinstance(o, dict):
        for k, v in o.items():
            yield path + "/" + k, k, v
            yield from _walk(v, path + "/" + k)
    elif isinstance(o, list):
        for i, v in enumerate(o):
            yield path + f"[{i}]", None, v
            yield from _walk(v, path + f"[{i}]")


def test_forbidden_fields(b, out):
    for n in FILES:
        text = (out / f"{n}.json").read_text(encoding="utf-8")
        assert not re.search(r"\d{15}", text), f"{n}: 15-digit run (IMEI-like)"
        for p, k, v in _walk(b[n]):
            if k is not None:
                assert not FORBIDDEN_KEY.search(k), f"{n}{p}"
            if isinstance(v, str):
                assert not re.fullmatch(r"[0-9a-fA-F]{12,}", v), f"{n}{p}: hex id-like value"
            if isinstance(v, list) and v and all(isinstance(x, (list, tuple)) for x in v):
                pytest.fail(f"{n}{p}: nested coordinate-like list")
    # coordinates only on stops, one point each
    for n in ("legs", "config", "census", "meta"):
        for p, k, _ in _walk(b[n]):
            assert k not in ("lat", "lon", "latitude", "longitude"), f"{n}{p}"
    # per-device rows: evidence carries aggregate counts only
    for l in b["legs"]:
        if l["evidence"]:
            assert isinstance(l["evidence"]["devices"], int)
