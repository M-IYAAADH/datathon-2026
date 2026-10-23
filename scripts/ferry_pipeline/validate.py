"""Integrity and policy checks over the written ferry CSVs.

A check *fails* when the outputs break a structural rule or a stated policy
(e.g. a cargo trip marked routable, an arrival invented for a departure-only
row). Data-quality problems that come from the sources themselves are not
failures as long as they are recorded in extraction_issues and kept out of
default routing; this module verifies exactly that.
"""
from __future__ import annotations

import csv
import hashlib
import re
from collections import defaultdict
from pathlib import Path

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
PK = {
    "sources": ["source_id"], "stops": ["stop_id"], "stop_aliases": ["alias_id"], "routes": ["route_id"],
    "services": ["service_id"], "trips": ["trip_id"], "legs": ["leg_id"],
    "stop_times": ["trip_id", "stop_sequence"], "frequency_windows": ["frequency_id"],
    "schedule_versions": ["version_record_id"], "extraction_issues": ["issue_id"],
    "routing_eligibility": ["trip_id"], "frequency_eligibility": ["frequency_id"],
}
CONTROLLED = {
    ("trips", "schedule_type"): {"scheduled", "departure_only"},
    ("frequency_windows", "operation_status"): {"operating", "suspended"},
    ("schedule_versions", "selection_status"): {"preferred_candidate", "historical", "duplicate", "unresolved"},
    ("extraction_issues", "severity"): {"error", "warning", "info"},
    ("extraction_issues", "resolution_status"): {"open", "documented_no_action", "excluded_pending_review"},
    ("routes", "passenger_eligibility"): {"presumed_public_ferry", "not_established"},
    ("routes", "transport_type"): {"ferry", "speed_ferry", "cargo_ferry"},
    ("services", "calendar_status"): {"explicit_days", "explicit_days_plus_unlisted_holidays", "unknown_days",
                                      "language_conflict", "alternating_weeks_unanchored",
                                      "fortnightly_unanchored", "explicit_days_with_unanchored_exception"},
    ("stops", "identity_status"): {"atoll_explicit", "atoll_inferred_route_title", "atoll_inferred_cross_route",
                                   "atoll_unresolved", "no_atoll_prefix_reviewed"},
    ("frequency_windows", "direction_scope"): {"unspecified", "both_directions", "single_direction"},
}
TIME_RE = re.compile(r"^\d{2}:\d{2}:00$")


def _read(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def validate(out_dir: Path, pdf_dir: Path, interim_dir: Path | None = None) -> dict:
    t = {name: _read(out_dir / f"{name}.csv") for name in PK}
    checks = []

    def check(name, ok, detail=""):
        checks.append({"check": name, "passed": bool(ok), "detail": detail if not ok else ""})

    # 1. primary keys
    for name, cols in PK.items():
        keys = [tuple(r[c] for c in cols) for r in t[name]]
        dup = len(keys) - len(set(keys))
        check(f"pk_unique:{name}", dup == 0 and all(all(k) for k in keys), f"{dup} duplicate/blank keys")

    ids = {name: {r[PK[name][0]] for r in rows} for name, rows in t.items()}

    def fk(table, col, target, allow_blank=False):
        bad = [r[col] for r in t[table] if not (allow_blank and r[col] == "") and r[col] not in ids[target]]
        check(f"fk:{table}.{col}->{target}", not bad, f"{len(bad)} bad e.g. {bad[:3]}")

    fk("stop_aliases", "stop_id", "stops"); fk("stop_aliases", "source_id", "sources")
    fk("trips", "route_id", "routes"); fk("trips", "service_id", "services"); fk("trips", "source_id", "sources")
    fk("trips", "origin_stop_id", "stops"); fk("trips", "destination_stop_id", "stops", allow_blank=True)
    fk("legs", "trip_id", "trips"); fk("legs", "from_stop_id", "stops")
    fk("legs", "to_stop_id", "stops", allow_blank=True)
    fk("stop_times", "trip_id", "trips"); fk("stop_times", "stop_id", "stops", allow_blank=True)
    fk("frequency_windows", "route_id", "routes"); fk("frequency_windows", "service_id", "services")
    fk("frequency_windows", "from_stop_id", "stops", allow_blank=True)
    fk("frequency_windows", "to_stop_id", "stops", allow_blank=True)
    fk("schedule_versions", "route_id", "routes"); fk("schedule_versions", "service_id", "services")
    fk("schedule_versions", "source_id", "sources"); fk("services", "source_id", "sources")
    fk("routing_eligibility", "trip_id", "trips"); fk("frequency_eligibility", "frequency_id", "frequency_windows")
    fk("extraction_issues", "source_id", "sources", allow_blank=True)
    vids = {r["version_record_id"] for r in t["schedule_versions"]}
    blocks_with_versions = {r["source_block_id"] for r in t["schedule_versions"]}
    bad = [r["trip_id"] for r in t["trips"] if r["source_block_id"] not in blocks_with_versions]
    check("fk:trips.source_block_id->schedule_versions", not bad, f"{bad[:3]}")
    bad = [r["trip_id"] for r in t["routing_eligibility"] if r["schedule_version_id"] not in vids]
    check("fk:routing_eligibility.schedule_version_id", not bad, f"{bad[:3]}")
    check("eligibility_row_per_trip", ids["routing_eligibility"] == ids["trips"],
          f"{len(ids['trips'] ^ ids['routing_eligibility'])} mismatched")

    # 2. controlled vocabularies
    for (table, col), allowed in CONTROLLED.items():
        bad = sorted({r[col] for r in t[table]} - allowed)
        check(f"controlled:{table}.{col}", not bad, f"unexpected {bad}")
    bad = [r for name in t for r in t[name] if "human_verified" in (r.get("review_status") or "")]
    check("no_human_verified_labels", not bad, f"{len(bad)} rows")

    # 3. sources unchanged and counted
    bad = []
    for r in t["sources"]:
        p = pdf_dir / r["filename"]
        if not p.exists() or hashlib.sha256(p.read_bytes()).hexdigest() != r["sha256"]:
            bad.append(r["filename"])
    check("source_pdfs_match_sha256", not bad, f"{bad}")

    # 4. time formats and seconds arithmetic
    def tsec(s):
        h, m, _ = s.split(":")
        return int(h) * 3600 + int(m) * 60

    bad = []
    for r in t["legs"]:
        for p in ("departure", "arrival"):
            v, off, sec, raw = r[f"{p}_time"], r[f"{p}_day_offset"], r[f"{p}_seconds"], r[f"{p}_time_raw"]
            if v == "":
                if off != "" or sec != "":
                    bad.append((r["leg_id"], p, "blank time with offset/seconds"))
                continue
            if not TIME_RE.match(v) or off not in ("0", "1") or int(sec) != int(off) * 86400 + tsec(v) or not raw:
                bad.append((r["leg_id"], p, v, off, sec))
    check("legs_time_format_and_seconds", not bad, f"{bad[:3]}")
    bad = []
    for r in t["frequency_windows"]:
        for p in ("start", "end"):
            v, off, sec = r[f"{p}_time"], r[f"{p}_day_offset"], r[f"{p}_seconds"]
            if not TIME_RE.match(v) or off not in ("0", "1", "2") or int(sec) != int(off) * 86400 + tsec(v):
                bad.append((r["frequency_id"], p))
        if int(r["end_seconds"]) <= int(r["start_seconds"]):
            bad.append((r["frequency_id"], "end<=start"))
    check("frequency_time_format_and_order", not bad, f"{bad[:3]}")

    # issues lookup
    issue_recs = defaultdict(set)
    for i in t["extraction_issues"]:
        for rid in i["record_id"].split(";"):
            issue_recs[(i["record_type"], rid)].add(i["severity"])
    elig = {r["trip_id"]: r for r in t["routing_eligibility"]}
    legs_by_trip = defaultdict(list)
    for r in t["legs"]:
        legs_by_trip[r["trip_id"]].append(r)
    for v in legs_by_trip.values():
        v.sort(key=lambda r: int(r["leg_sequence"]))

    # 5. arrival after departure (or flagged + excluded)
    bad = []
    for r in t["legs"]:
        if r["departure_seconds"] and r["arrival_seconds"] and int(r["arrival_seconds"]) <= int(r["departure_seconds"]):
            flagged = "error" in issue_recs[("leg", r["leg_id"])]
            if not flagged or elig[r["trip_id"]]["routing_eligible"] == "1":
                bad.append(r["leg_id"])
    check("arrival_after_departure_or_flagged_excluded", not bad, f"{bad[:5]}")

    # 6. monotonic events / continuity / reversal handling
    nonmono, discont, reversal = [], [], []
    for tid, lgs in legs_by_trip.items():
        prev = None
        for lg in lgs:
            for p in ("departure", "arrival"):
                s = lg[f"{p}_seconds"]
                if s == "":
                    continue
                if prev is not None and int(s) < prev:
                    if elig[tid]["routing_eligible"] == "1" or not any(
                            "error" in issue_recs[("leg", x["leg_id"])] for x in lgs):
                        nonmono.append(tid)
                prev = int(s)
        for a, b in zip(lgs, lgs[1:]):
            if a["to_stop_id"] != b["from_stop_id"]:
                discont.append(tid)
            if b["to_stop_id"] == a["from_stop_id"]:
                reversal.append(tid)
    check("monotonic_times_within_trip_or_flagged", not nonmono, f"{sorted(set(nonmono))[:5]}")
    check("consecutive_leg_stop_continuity", not discont, f"{sorted(set(discont))[:5]}")
    check("no_unsplit_direction_reversal", not reversal, f"{sorted(set(reversal))[:5]}")

    # 7. stop_times derived exactly from legs
    st_by_trip = defaultdict(list)
    for r in t["stop_times"]:
        st_by_trip[r["trip_id"]].append(r)
    bad = []
    for tid, lgs in legs_by_trip.items():
        sts = sorted(st_by_trip[tid], key=lambda r: int(r["stop_sequence"]))
        if len(sts) != len(lgs) + 1:
            bad.append((tid, "count"))
            continue
        if sts[0]["arrival_time"] or sts[-1]["departure_time"]:
            bad.append((tid, "origin arrival / destination departure invented"))
        for k, lg in enumerate(lgs):
            if sts[k]["departure_time"] != lg["departure_time"] or sts[k + 1]["arrival_time"] != lg["arrival_time"] \
                    or sts[k]["stop_id"] != lg["from_stop_id"] or sts[k + 1]["stop_id"] != lg["to_stop_id"]:
                bad.append((tid, k))
    check("stop_times_match_legs", not bad, f"{bad[:3]}")

    # 8. weekdays
    bad = [r["service_id"] for r in t["services"] if any(r[d] not in ("0", "1", "") for d in WEEKDAYS)]
    check("weekday_values", not bad, f"{bad[:3]}")
    bad = [r["service_id"] for r in t["services"] if r["calendar_status"] == "explicit_days"
           and not all(r[d] in ("0", "1") for d in WEEKDAYS)]
    check("explicit_days_have_all_flags", not bad, f"{bad[:3]}")
    bad = [r["service_id"] for r in t["services"]
           if r["calendar_status"] in ("alternating_weeks_unanchored", "fortnightly_unanchored")
           and any(r[d] for d in WEEKDAYS)]
    check("unanchored_alternation_has_blank_flags", not bad, f"{bad[:3]}")
    bad = [r["service_id"] for r in t["services"] if r["effective_to"]]
    check("effective_to_not_asserted", not bad, f"{bad[:3]}")

    # 9. frequency windows
    bad = []
    for r in t["frequency_windows"]:
        if r["exact_times"] != "0":
            bad.append((r["frequency_id"], "exact_times"))
        if r["operation_status"] == "operating" and not (r["headway_seconds"] and int(r["headway_seconds"]) > 0):
            bad.append((r["frequency_id"], "headway"))
        if r["operation_status"] == "suspended" and r["headway_seconds"]:
            bad.append((r["frequency_id"], "suspended with headway"))
    check("frequency_headways_and_no_exact_times", not bad, f"{bad[:3]}")
    freq_routes = {r["route_id"] for r in t["frequency_windows"]}
    bad = [r["trip_id"] for r in t["trips"] if r["route_id"] in freq_routes]
    check("frequency_not_expanded_into_trips", not bad, f"{bad[:3]}")
    bad = [r["frequency_id"] for r in t["frequency_eligibility"] if r["headway_model_eligible"] != "0"
           and r["direction_resolved"] != "1"]
    check("frequency_unspecified_direction_not_eligible", not bad, f"{bad[:3]}")

    # 10. cargo and departure-only exclusions
    routes = {r["route_id"]: r for r in t["routes"]}
    trips = {r["trip_id"]: r for r in t["trips"]}
    bad = [tid for tid, e in elig.items() if routes[trips[tid]["route_id"]]["passenger_eligibility"] ==
           "not_established" and (e["routing_eligible"] == "1" or e["passenger_eligible"] == "1")]
    check("cargo_excluded_from_passenger_routing", not bad, f"{bad[:3]}")
    bad = []
    for tid, tr in trips.items():
        if tr["schedule_type"] != "departure_only":
            continue
        for lg in legs_by_trip[tid]:
            if lg["arrival_time"] or lg["arrival_time_raw"] or lg["arrival_seconds"] or lg["duration_minutes"]:
                bad.append(tid)
        if elig[tid]["routing_eligible"] == "1" or elig[tid]["timing_complete"] == "1":
            bad.append(tid)
    check("departure_only_keeps_unknown_arrivals", not bad, f"{sorted(set(bad))[:3]}")

    # 11. eligibility consistency
    bad = []
    for tid, e in elig.items():
        flags = all(e[c] == "1" for c in ("passenger_eligible", "timing_complete", "calendar_resolved",
                                          "stop_identity_resolved", "version_resolved"))
        if e["routing_eligible"] == "1" and (not flags or e["exclusion_reasons"]):
            bad.append(tid)
        if e["routing_eligible"] == "0" and not e["exclusion_reasons"]:
            bad.append(tid)
        if e["routing_eligible"] == "1":
            if any("error" in issue_recs[("leg", lg["leg_id"])] for lg in legs_by_trip[tid]):
                bad.append(tid)
    check("eligibility_flags_consistent", not bad, f"{bad[:3]}")
    vstat = {r["version_record_id"]: r["selection_status"] for r in t["schedule_versions"]}
    bad = [tid for tid, e in elig.items() if e["routing_eligible"] == "1" and
           vstat[e["schedule_version_id"]] != "preferred_candidate"]
    check("routable_only_preferred_versions", not bad, f"{bad[:3]}")

    # 12. duplicate routable services (overlapping versions double counted)
    svc = {r["service_id"]: r for r in t["services"]}
    seen, dups = {}, []
    for tid, e in elig.items():
        if e["routing_eligible"] != "1":
            continue
        tr = trips[tid]
        lg0 = legs_by_trip[tid][0]
        days = tuple(d for d in WEEKDAYS if svc[tr["service_id"]][d] == "1")
        for d in days:
            key = (tr["origin_stop_id"], lg0["to_stop_id"], lg0["departure_seconds"], d)
            if key in seen and trips[seen[key]]["source_block_id"] != tr["source_block_id"]:
                dups.append((seen[key], tid))
            seen[key] = tid
    check("no_duplicate_routable_departures_across_blocks", not dups, f"{dups[:3]}")

    # 13. no invented geography / codes / fares
    bad = [r["stop_id"] for r in t["stops"] if any(r[c] for c in ("official_island_code", "official_atoll_code",
                                                                    "latitude", "longitude", "jetty_name"))]
    check("no_unsourced_geography_or_codes", not bad, f"{bad[:3]}")
    hdrs = [c for rows in t.values() for c in (rows[0].keys() if rows else [])]
    check("no_fare_or_capacity_columns", not [c for c in hdrs if re.search(r"fare|capacity|vessel", c)], "")

    # 14. traceability
    bad = [r["leg_id"] for r in t["legs"] if not (r["source_id"] and r["source_page"] and r["source_row_id"])]
    check("legs_traceable_to_source_row", not bad, f"{bad[:3]}")
    bad = [r["trip_id"] for r in t["trips"] if not (r["source_id"] and r["source_page"] and r["source_block_id"])]
    check("trips_traceable_to_source_block", not bad, f"{bad[:3]}")

    # 15. coverage accounting
    if interim_dir is not None and (interim_dir / "coverage.csv").exists():
        cov = _read(interim_dir / "coverage.csv")
        covered = {(r["source_id"], int(r["page"])) for r in cov}
        expected = {(r["source_id"], p) for r in t["sources"] for p in range(1, int(r["page_count"]) + 1)}
        check("every_page_has_coverage_status", expected <= covered, f"missing {sorted(expected - covered)[:5]}")
        allowed = {"extracted", "duplicate", "non-schedule", "partially_extracted", "unresolved"}
        check("coverage_status_controlled", {r["coverage_status"] for r in cov} <= allowed, "")
        blocks = {r["source_block_id"] for r in t["schedule_versions"]}
        check("every_block_has_coverage_status", blocks <= {r["block_id"] for r in cov}, "")

    failures = [c for c in checks if not c["passed"]]
    stats = {
        "legs_with_error_issues": sum(1 for r in t["legs"] if "error" in issue_recs[("leg", r["leg_id"])]),
        "routing_eligible_trips": sum(1 for e in elig.values() if e["routing_eligible"] == "1"),
        "trips": len(elig),
    }
    return {"status": "pass" if not failures else "fail", "n_checks": len(checks), "failures": failures,
            "checks": checks, "stats": stats}
