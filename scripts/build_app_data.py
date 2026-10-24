"""Build the five JSON files the journey-planner app loads (stops, legs, config, census, meta).

Inputs (all tracked in this repo, no private data):
  - data/processed/ferry/{routing_eligibility,trips,legs,services,routes,stops}.csv
      2026 MTCC timetables only (source_id in MTCC_2026_SOURCES); historical tables are never used.
  - data/derived/vessels/corridor_services.json   inferred vessel services (Aug 2025, aggregate only)
  - per_island/*.csv                              Census 2022 per-island rows

Usage:
  python3 scripts/build_app_data.py --out <app_repo>/app/public/data [--build-version 2026-10-24T17:00+05:00]

Output is deterministic: same inputs + same --build-version give identical bytes.
Standard library only.
"""
from __future__ import annotations

import argparse
import collections
import csv
import glob
import json
import os
import re
import shutil
import sys
import tempfile
import unicodedata
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEFAULT_BUILD_VERSION = "2026-10-24T17:00+05:00"

CONFIG = {
    "timezone": "Indian/Maldives",
    "bufferOfficialMin": 5,
    "bufferInferredMin": 15,
    "longWaitMin": 120,
    "maxTransfers": 2,
    "horizonHours": 48,
    "shiftStepMin": 5,
    "maxShiftMin": 30,
    "minDevices": 3,
    "minDays": 8,
}

# 2026 MTCC sources allowed into the bundle -> validFrom. No printed effective dates exist in
# sources.csv for these, so validFrom is the date in the file name (assumption, stated in docs).
MTCC_2026_SOURCES = {
    "cfn_2026_all": "2026-08-01",
    "cfn_2026_zone6": "2026-01-01",
    "speed_2026_thaa_laamu": "2026-03-28",
}
VALID_TO = "2026-12-31"  # assumption: no printed end date
INFERRED_VALID_FROM = "2026-01-01"  # projects the Aug 2025 pattern onto 2026
EVIDENCE_PERIOD = "2025-08"

# Ferry spelling (atoll, folded name) -> Census spelling (atoll, folded name).
# Hand-checked. Exact match only; never substring (Kolamaafushi is not Maafushi;
# GA Villingili is a different island from Villimale and is NOT aliased here).
CENSUS_ALIAS = {
    ("", "male"): ("MLE", "maale"),
    ("", "villimale"): ("MLE", "vilingili"),
    ("", "hulhumale"): ("MLE", "hulhumaale"),
    ("ADH", "dhangethi"): ("ADH", "dhagethi"),
    ("ADH", "kunburudhoo"): ("ADH", "kuburudhoo"),
    ("ADH", "hangnaameedhoo"): ("ADH", "hangnameedhoo"),
}

DAY_COLS = ["sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday"]
WK = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
CLS = {"fast_vessel": "fast", "slow_vessel": "slow"}
FORBIDDEN_SUBSTRINGS = ("imei", "device_key", "device_id", "deviceid", "dates_seen", "trajectory", "lat_raw", "lon_raw")


def norm(s: str) -> str:
    """ASCII-fold, lower-case, letters only."""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z]", "", s)


def hm(s: str) -> int:
    h, m = s.split(":")[:2]
    return int(h) * 60 + int(m)


def normalise_after_midnight(r: dict, base: int) -> dict:
    """Keep a time range monotonic from the first departure of the service (base, minutes).

    A time more than 60 min before the first departure is the next day (+1440); a range whose
    max is before its min wraps midnight. Example: base 23:30 (1410), "01:10" -> 1510.
    """
    r = dict(r)
    while r["min"] < base - 60:
        r["min"] += 1440
        r["max"] += 1440
    if r["max"] < r["min"]:
        r["max"] += 1440
    return r


def read_csv(path: Path) -> list[dict]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def resolve_census_key(atoll: str, name: str, index: dict) -> tuple | None:
    atoll = atoll.upper()
    n = norm(name)
    key = CENSUS_ALIAS.get((atoll, n), (atoll or "MLE", n))
    return index.get(key)


def load_census(root: Path):
    census, index = {}, {}
    for f in sorted(glob.glob(str(root / "per_island" / "*.csv"))):
        rows = read_csv(Path(f))
        if not rows:
            continue
        r = rows[0]
        key = f"LD{int(r['island_code']):04d}"
        name = r["island_name"].split(".", 1)[-1]
        census[key] = {
            "name": name,
            "population": int(r["population_total"]),
            "male": int(r["population_male"]),
            "female": int(r["population_female"]),
            "age0_17": int(r["children_0_17"]),
            "age18_64": int(r["adults_18_64"]),
            "age65p": int(r["elderly_65_plus"]),
            "disability": int(r["disability_count"]),
            "disabilityEligible": int(r["disability_eligible_population"]),
            "elderlyAlone": int(r["elderly_living_alone"]),
        }
        if r["latitude"] and r["longitude"]:
            index[(r["atoll_code"].upper(), norm(name))] = (key, float(r["latitude"]), float(r["longitude"]))
    return census, index


def build(root: Path, build_version: str):
    report = collections.OrderedDict()

    def note(k, v):
        report.setdefault(k, []).append(v)

    ferry = root / "data" / "processed" / "ferry"
    census, cidx = load_census(root)

    # ---- MTCC (2026 only) ----
    elig = {e["trip_id"] for e in read_csv(ferry / "routing_eligibility.csv") if e["routing_eligible"] == "1"}
    trips = {t["trip_id"]: t for t in read_csv(ferry / "trips.csv")}
    svc = {s["service_id"]: s for s in read_csv(ferry / "services.csv")}
    routes = {r["route_id"]: r for r in read_csv(ferry / "routes.csv")}
    fstops = {s["stop_id"]: s for s in read_csv(ferry / "stops.csv")}

    mtcc, used = [], set()
    legs_csv = sorted(read_csv(ferry / "legs.csv"), key=lambda l: (l["trip_id"], int(l["leg_sequence"])))
    for l in legs_csv:
        if l["trip_id"] not in elig:
            continue
        t = trips[l["trip_id"]]
        if t["source_id"] not in MTCC_2026_SOURCES:
            note("mtcc_leg_dropped_not_2026_source", l["leg_id"])
            continue
        s = svc[t["service_id"]]
        days = [i for i, d in enumerate(DAY_COLS) if s[d] == "1"]
        # seconds from service-day start; day offset already included, so >=1440 min passes through
        dep = int(l["departure_seconds"]) // 60
        arr = int(l["arrival_seconds"]) // 60
        if arr <= dep:
            note("mtcc_leg_dropped_arr_not_after_dep", l["leg_id"])
            continue
        if not days:
            note("mtcc_leg_dropped_no_days", l["leg_id"])
            continue
        mtcc.append({
            "id": "mtcc-" + l["leg_id"].removeprefix("leg_"),
            "source": "mtcc",
            "routeId": routes[t["route_id"]]["published_route_code"] or t["route_id"],
            "tripId": t["trip_id"].removeprefix("trp_"),
            "from": l["from_stop_id"],
            "to": l["to_stop_id"],
            "dep": {"min": dep, "max": dep},
            "arr": {"min": arr, "max": arr},
            "days": days,
            "validFrom": MTCC_2026_SOURCES[t["source_id"]],
            "validTo": VALID_TO,
            "cls": None,
            "evidence": None,
        })
        used |= {l["from_stop_id"], l["to_stop_id"]}

    # ---- Stops: ferry stop -> Census (key + coordinates) ----
    hits, base_count = {}, collections.Counter()
    for sid in sorted(used):
        fs = fstops[sid]
        hits[sid] = resolve_census_key(fs["atoll_abbreviation"], fs["island_name_en"], cidx)
        base_count[norm(fs["island_name_en"]).upper()] += 1
    stop_id, stops = {}, []
    for sid in sorted(used):
        fs, hit = fstops[sid], hits[sid]
        base = norm(fs["island_name_en"]).upper()
        new_id = base if base_count[base] == 1 else f"{base}-{fs['atoll_abbreviation'].upper() or 'X'}"
        if not hit:
            note("stop_unmapped_no_census_match", f"{sid} ({fs['island_name_en']}, {fs['atoll_abbreviation'] or '-'})")
            continue
        stop_id[sid] = new_id
        name = "Malé" if new_id == "MALE" else fs["island_name_en"].replace("’", "").replace("'", "")
        stops.append({
            "id": new_id, "name": name, "atoll": fs["atoll_abbreviation"] or "MLE",
            "lat": hit[1], "lon": hit[2], "censusKey": hit[0],
            "kind": "hub" if new_id == "MALE" else "island",
        })
    legs = []
    for l in mtcc:
        if l["from"] not in stop_id or l["to"] not in stop_id:
            bad = [x for x in (l["from"], l["to"]) if x not in stop_id]
            note("mtcc_leg_dropped_stop_unmapped", f"{l['id']} ({','.join(bad)})")
            continue
        l["from"], l["to"] = stop_id[l["from"]], stop_id[l["to"]]
        legs.append(l)
    by_census = {s["censusKey"]: s["id"] for s in stops}

    # ---- Inferred: service stops -> consecutive boardable segments ----
    with open(root / "data" / "derived" / "vessels" / "corridor_services.json", encoding="utf-8") as f:
        V = json.load(f)
    md, mv = CONFIG["minDays"], CONFIG["minDevices"]
    for s in V["services"]:
        if not s["regular"]:
            note("inferred_service_dropped_not_regular", s["service_id"])
            continue
        if s["distinct_devices"] < mv or s["days_seen"] < md:
            note("inferred_service_dropped_below_disclosure_min",
                 f"{s['service_id']} devices={s['distinct_devices']} days={s['days_seen']}")
            continue
        wk = dict(p.split(":") for p in s["days_seen_by_weekday"].split("|"))
        obs = [int(wk[d].split("/")[0]) for d in WK]
        days = [i for i, o in enumerate(obs) if o > 0]
        st = sorted(s["stops"], key=lambda x: x["stop_order"])
        for x in st:
            if x["n_observed"] < md:
                note("inferred_stop_skipped_low_support",
                     f"{s['service_id']}#{x['stop_order']} {x['island_name']} n_observed={x['n_observed']}")
        st = [x for x in st if x["n_observed"] >= md]  # boat treated as passing a weakly observed stop
        base = None
        for a, b in zip(st, st[1:]):
            dep = {"min": hm(a["time_p10"]), "max": hm(a["time_p90"])}
            arr = {"min": hm(b["time_p10"]), "max": hm(b["time_p90"])}
            if base is None:
                base = dep["min"]
            dep, arr = normalise_after_midnight(dep, base), normalise_after_midnight(arr, base)
            seg = f"{s['service_id']}:{a['stop_order']}-{b['stop_order']}"
            if arr["min"] < dep["min"] or arr["max"] < dep["max"]:
                note("inferred_segment_dropped_arr_before_dep", seg)
                continue
            support = min(s["days_seen"], a["n_observed"], b["n_observed"])
            if support < md:
                note("inferred_segment_dropped_low_support", f"{seg} days={support}")
                continue
            fa, fb = by_census.get(a["island_code"]), by_census.get(b["island_code"])
            if not fa or not fb:
                note("inferred_segment_dropped_island_not_in_stops", seg)
                continue
            if b["event"] != "arrival":
                note("inferred_arr_uses_next_stop_departure_range", seg)
            legs.append({
                "id": f"inf-{seg}",
                "source": "inferred",
                "routeId": s["signature_id"],
                "tripId": s["service_id"],
                "from": fa,
                "to": fb,
                "dep": dep,
                "arr": arr,
                "days": days,
                "validFrom": INFERRED_VALID_FROM,
                "validTo": VALID_TO,
                "cls": CLS.get(s["inferred_class"]),
                "evidence": {
                    "devices": s["distinct_devices"],
                    "days": support,
                    "weekdayObs": obs,
                    "period": EVIDENCE_PERIOD,
                    "unconfirmedOriginShare": s["unconfirmed_origin_share"],
                },
            })

    meta = {
        "buildVersion": build_version,
        "fixture": False,
        "evidencePeriod": EVIDENCE_PERIOD,
        "timezoneStatus": "confirmed" if V.get("timezone_confirmed") else "unconfirmed",
        "speedUnitStatus": "confirmed" if V.get("speed_unit_confirmed") else "unconfirmed",
        "clockCorrections": int(V["clock_correction"]["observations_corrected_in_scope"]),
        "timetableYear": 2026,
    }
    config = dict(CONFIG, buildVersion=build_version)
    keys = {s["censusKey"] for s in stops}
    census_out = {k: census[k] for k in sorted(keys)}
    bundle = {"stops": stops, "legs": legs, "config": config, "census": census_out, "meta": meta}
    validate(bundle)
    return bundle, report


def validate(b: dict) -> None:
    stops, legs, cfg = b["stops"], b["legs"], b["config"]
    sids = {s["id"] for s in stops}
    assert len(sids) == len(stops), "duplicate stop id"
    assert len({l["id"] for l in legs}) == len(legs), "duplicate leg id"
    for s in stops:
        assert s["censusKey"] in b["census"], s["id"]
    for l in legs:
        assert l["from"] in sids and l["to"] in sids, l["id"]
        assert l["dep"]["min"] <= l["dep"]["max"] and l["arr"]["min"] <= l["arr"]["max"], l["id"]
        assert l["arr"]["min"] >= l["dep"]["min"], l["id"]
        assert l["days"] and all(0 <= d <= 6 for d in l["days"]), l["id"]
        if l["source"] == "inferred":
            assert l["evidence"]["devices"] >= cfg["minDevices"] and l["evidence"]["days"] >= cfg["minDays"], l["id"]
    assert b["config"]["buildVersion"] == b["meta"]["buildVersion"]
    blob = json.dumps(b).lower()
    for bad in FORBIDDEN_SUBSTRINGS:
        assert bad not in blob, bad
    assert not re.search(r"\b\d{15}\b", blob), "15-digit number (IMEI-like) in output"


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=1) + "\n"


def write(bundle: dict, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=".app_data_", dir=out))
    try:
        for name in ("stops", "legs", "config", "census", "meta"):
            (tmp / f"{name}.json").write_text(dumps(bundle[name]), encoding="utf-8")
        for name in ("stops", "legs", "config", "census", "meta"):
            os.replace(tmp / f"{name}.json", out / f"{name}.json")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=Path, default=REPO, help="data repo root")
    ap.add_argument("--out", type=Path, required=True, help="output directory (e.g. <app>/app/public/data)")
    ap.add_argument("--build-version", default=os.environ.get("APP_DATA_BUILD_VERSION", DEFAULT_BUILD_VERSION))
    args = ap.parse_args(argv)
    bundle, report = build(args.root, args.build_version)
    write(bundle, args.out)
    legs = bundle["legs"]
    summary = {
        "buildVersion": args.build_version,
        "stops": len(bundle["stops"]),
        "legs": len(legs),
        "legs_mtcc": sum(l["source"] == "mtcc" for l in legs),
        "legs_inferred": sum(l["source"] == "inferred" for l in legs),
        "census": len(bundle["census"]),
    }
    print(json.dumps(summary, indent=1))
    for k, v in report.items():
        print(f"- {k}: {len(v)}")
        for x in v:
            print(f"    {x}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
