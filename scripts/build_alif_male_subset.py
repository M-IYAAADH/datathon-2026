#!/usr/bin/env python
"""Build the Alif atolls (AA, ADh) <-> Malé ferry subset from the processed ferry CSVs.

Usage (after scripts/extract_ferry_schedules.py):
    python scripts/build_alif_male_subset.py

Writes to data/processed/ferry/subsets/:
    alif_male_direct.csv        every published trip that carries a passenger between an AA/ADh island and
                                Malé or Hulhumalé without changing ferry (all versions, with eligibility)
    alif_male_one_transfer.csv  derived journeys with one change at a common island, built only from
                                routing-eligible trips (NOT published as connections; see notes column)

Times are clock times (Asia/Male assumption) with seconds from the start of the service day.
"""
from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
ALIF_PREFIXES = ("stp_aa_", "stp_adh_")
MALE_AREA = {"stp_noatoll_male", "stp_noatoll_hulhumale"}

DIRECT_COLS = (
    "od_id,direction,trip_id,route_id,published_route_code,board_stop_id,board_island,board_atoll,"
    "board_departure_time,board_departure_seconds,alight_stop_id,alight_island,alight_atoll,alight_arrival_time,"
    "alight_arrival_seconds,travel_minutes,intermediate_stops,monday,tuesday,wednesday,thursday,friday,saturday,"
    "sunday,calendar_status,effective_from,version_status,routing_eligible,exclusion_reasons,source_id,"
    "source_page,source_row_ids"
).split(",")
TRANSFER_COLS = (
    "journey_id,direction,board_stop_id,board_island,board_atoll,board_departure_time,board_departure_seconds,"
    "transfer_stop_id,transfer_island,transfer_arrival_time,transfer_departure_time,wait_minutes,alight_stop_id,"
    "alight_island,alight_atoll,alight_arrival_time,alight_arrival_seconds,total_minutes,monday,tuesday,wednesday,thursday,"
    "friday,saturday,sunday,leg1_trip_id,leg1_route_id,leg2_trip_id,leg2_route_id,leg1_source,leg2_source,notes"
).split(",")
TRANSFER_NOTE = ("Derived connection, not published by the operator: same island, same weekday, onward departure "
                 "not earlier than arrival. Minimum transfer time and jetty (whether both ferries use the same "
                 "landing) are unknown; the two ferries may or may not be the same vessel.")


def read(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def is_alif(stop_id: str) -> bool:
    return stop_id.startswith(ALIF_PREFIXES)


def segments(stop_times, a_pred, b_pred):
    """(i, j) pairs with stop i matching a_pred, j>i matching b_pred, with a departure at i and arrival at j."""
    for i, si in enumerate(stop_times):
        if not a_pred(si["stop_id"]) or si["departure_seconds"] == "":
            continue
        for sj in stop_times[i + 1:]:
            if b_pred(sj["stop_id"]) and sj["arrival_seconds"] != "":
                yield si, sj


def build(proc: Path):
    stops = {r["stop_id"]: r for r in read(proc / "stops.csv")}
    trips = {r["trip_id"]: r for r in read(proc / "trips.csv")}
    routes = {r["route_id"]: r for r in read(proc / "routes.csv")}
    services = {r["service_id"]: r for r in read(proc / "services.csv")}
    elig = {r["trip_id"]: r for r in read(proc / "routing_eligibility.csv")}
    versions = {r["version_record_id"]: r for r in read(proc / "schedule_versions.csv")}
    st = defaultdict(list)
    for r in read(proc / "stop_times.csv"):
        st[r["trip_id"]].append(r)
    for v in st.values():
        v.sort(key=lambda r: int(r["stop_sequence"]))

    def stop_cols(prefix, sid):
        s = stops[sid]
        return {f"{prefix}_stop_id": sid, f"{prefix}_island": s["island_name_en"], f"{prefix}_atoll": s["atoll_abbreviation"]}

    # ---- direct
    direct = []
    for tid in sorted(st):
        t, e = trips[tid], elig[tid]
        svc = services[t["service_id"]]
        for direction, a, b in (("to_male", is_alif, MALE_AREA.__contains__),
                                ("from_male", MALE_AREA.__contains__, is_alif)):
            for si, sj in segments(st[tid], a, b):
                k0, k1 = int(si["stop_sequence"]), int(sj["stop_sequence"])
                direct.append({
                    "direction": direction, "trip_id": tid, "route_id": t["route_id"],
                    "published_route_code": routes[t["route_id"]]["published_route_code"],
                    **stop_cols("board", si["stop_id"]),
                    "board_departure_time": si["departure_time"], "board_departure_seconds": si["departure_seconds"],
                    **stop_cols("alight", sj["stop_id"]),
                    "alight_arrival_time": sj["arrival_time"], "alight_arrival_seconds": sj["arrival_seconds"],
                    "travel_minutes": (int(sj["arrival_seconds"]) - int(si["departure_seconds"])) // 60,
                    "intermediate_stops": ";".join(stops[x["stop_id"]]["island_name_en"]
                                                   for x in st[tid] if k0 < int(x["stop_sequence"]) < k1),
                    **{d: svc[d] for d in WEEKDAYS}, "calendar_status": svc["calendar_status"],
                    "effective_from": svc["effective_from"],
                    "version_status": versions[e["schedule_version_id"]]["selection_status"],
                    "routing_eligible": e["routing_eligible"], "exclusion_reasons": e["exclusion_reasons"],
                    "source_id": t["source_id"], "source_page": t["source_page"],
                    "source_row_ids": ";".join(x["source_row_ids"] for x in st[tid]
                                               if k0 <= int(x["stop_sequence"]) <= k1),
                })
    direct.sort(key=lambda r: (r["direction"], r["routing_eligible"] != "1", r["board_island"],
                               int(r["board_departure_seconds"]), r["alight_island"], r["trip_id"]))
    for i, r in enumerate(direct, 1):
        r["od_id"] = f"alm_d_{i:04d}"

    # ---- one transfer (routing-eligible trips only)
    ok = [tid for tid in sorted(st) if elig[tid]["routing_eligible"] == "1"]
    days = {tid: {d for d in WEEKDAYS if services[trips[tid]["service_id"]][d] == "1"} for tid in ok}
    main_to = [(tid, si, sj) for tid in ok for si, sj in segments(st[tid], is_alif, MALE_AREA.__contains__)]
    main_from = [(tid, si, sj) for tid in ok for si, sj in segments(st[tid], MALE_AREA.__contains__, is_alif)]
    direct_pairs = {(r["board_stop_id"], r["alight_stop_id"]) for r in direct if r["routing_eligible"] == "1"}
    transfers = []

    def add(direction, leg1, leg2, board, xfer_in, xfer_out, alight):
        common = days[leg1] & days[leg2]
        wait = int(xfer_out["departure_seconds"]) - int(xfer_in["arrival_seconds"])
        if not common or wait < 0 or (board["stop_id"], alight["stop_id"]) in direct_pairs:
            return
        transfers.append({
            "direction": direction, **stop_cols("board", board["stop_id"]),
            "board_departure_time": board["departure_time"], "board_departure_seconds": board["departure_seconds"],
            "transfer_stop_id": xfer_in["stop_id"], "transfer_island": stops[xfer_in["stop_id"]]["island_name_en"],
            "transfer_arrival_time": xfer_in["arrival_time"], "transfer_departure_time": xfer_out["departure_time"],
            "wait_minutes": wait // 60, **stop_cols("alight", alight["stop_id"]),
            "alight_arrival_time": alight["arrival_time"], "alight_arrival_seconds": alight["arrival_seconds"],
            "total_minutes": (int(alight["arrival_seconds"]) - int(board["departure_seconds"])) // 60,
            **{d: "1" if d in common else "0" for d in WEEKDAYS},
            "leg1_trip_id": leg1, "leg1_route_id": trips[leg1]["route_id"],
            "leg2_trip_id": leg2, "leg2_route_id": trips[leg2]["route_id"],
            "leg1_source": f"{trips[leg1]['source_id']} p{trips[leg1]['source_page']}",
            "leg2_source": f"{trips[leg2]['source_id']} p{trips[leg2]['source_page']}", "notes": TRANSFER_NOTE,
        })

    for mtid, x_out, alight in main_to:              # feeder AA/ADh island -> X, then X -> Malé area
        for ftid in ok:
            if ftid == mtid:
                continue
            for board, x_in in segments(st[ftid], is_alif, lambda s: s == x_out["stop_id"]):
                if board["stop_id"] != x_out["stop_id"]:
                    add("to_male", ftid, mtid, board, x_in, x_out, alight)
    for mtid, board, x_in in main_from:              # Malé area -> X, then X -> AA/ADh island
        for ftid in ok:
            if ftid == mtid:
                continue
            for x_out, alight in segments(st[ftid], lambda s: s == x_in["stop_id"], is_alif):
                if alight["stop_id"] != x_in["stop_id"]:
                    add("from_male", mtid, ftid, board, x_in, x_out, alight)
    transfers.sort(key=lambda r: (r["direction"], r["board_island"], r["alight_island"],
                                  int(r["board_departure_seconds"]), r["leg1_trip_id"], r["leg2_trip_id"]))
    for i, r in enumerate(transfers, 1):
        r["journey_id"] = f"alm_t_{i:04d}"
    return direct, transfers


def write(path: Path, cols, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, lineterminator="\n")
        w.writeheader()
        w.writerows({c: r.get(c, "") for c in cols} for r in rows)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--processed-dir", type=Path, default=ROOT / "data/processed/ferry")
    args = ap.parse_args(argv)
    direct, transfers = build(args.processed_dir)
    out = args.processed_dir / "subsets"
    write(out / "alif_male_direct.csv", DIRECT_COLS, direct)
    write(out / "alif_male_one_transfer.csv", TRANSFER_COLS, transfers)
    print(f"alif_male_direct.csv        {len(direct):5d} rows "
          f"({sum(r['routing_eligible'] == '1' for r in direct)} routing-eligible)")
    print(f"alif_male_one_transfer.csv  {len(transfers):5d} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
