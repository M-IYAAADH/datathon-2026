"""Published MTCC ferry departures beside inferred services on the same island pairs."""
from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from pathlib import Path

import duckdb

DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
ABBR = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def norm(name: str) -> str:
    s = unicodedata.normalize("NFKD", name or "")
    return re.sub(r"[^a-z0-9]", "", "".join(ch for ch in s if not unicodedata.combining(ch)).lower())


def load_mtcc(ferry_dir: Path, island_names: dict[str, str]) -> tuple[list[dict], dict]:
    """island_names: island_code -> OneMap name. Returns direct published departures between mapped island pairs."""
    con = duckdb.connect()
    d = Path(ferry_dir)
    stops = con.execute(f"SELECT stop_id, island_name_en FROM read_csv_auto('{d}/stops.csv', header=true)").fetchall()
    by_norm = {norm(n): sid for sid, n in stops}
    code_to_stop = {c: by_norm.get(norm(n)) for c, n in island_names.items()}
    stop_to_code = {s: c for c, s in code_to_stop.items() if s}
    ids = ",".join(f"'{s}'" for s in stop_to_code)
    if not ids:
        return [], code_to_stop
    rows = con.execute(f"""
    SELECT a.trip_id, a.stop_id, b.stop_id, a.departure_time, b.arrival_time, a.departure_seconds, b.arrival_seconds,
           r.published_route_code, {", ".join('s.' + x for x in DAYS)}, s.calendar_status, e.routing_eligible
    FROM read_csv_auto('{d}/stop_times.csv', header=true) a
    JOIN read_csv_auto('{d}/stop_times.csv', header=true) b ON a.trip_id = b.trip_id AND a.stop_sequence < b.stop_sequence
    JOIN read_csv_auto('{d}/trips.csv', header=true) t ON t.trip_id = a.trip_id
    JOIN read_csv_auto('{d}/routes.csv', header=true) r ON r.route_id = t.route_id
    JOIN read_csv_auto('{d}/services.csv', header=true) s ON s.service_id = t.service_id
    JOIN read_csv_auto('{d}/routing_eligibility.csv', header=true) e ON e.trip_id = t.trip_id
    WHERE a.stop_id IN ({ids}) AND b.stop_id IN ({ids}) AND a.departure_seconds IS NOT NULL""").fetchall()
    out = []
    for r in rows:
        if str(r[-1]).lower() not in ("1", "true"):
            continue
        days = [ABBR[i] for i in range(7) if str(r[8 + i]).lower() in ("1", "true")]
        out.append({"trip_id": r[0], "origin_code": stop_to_code[r[1]], "destination_code": stop_to_code[r[2]],
                    "departure": str(r[3])[:5], "arrival": (str(r[4])[:5] if r[4] else ""), "dep_s": r[5],
                    "route_code": r[7], "weekdays": days, "calendar_status": r[15]})
    return out, code_to_stop


def compare(mtcc: list[dict], services: list[dict], code_name: dict, cfg: dict) -> list[dict]:
    long_gap = cfg["mtcc_compare"]["long_gap_minutes"]
    pairs = defaultdict(lambda: {"mtcc": [], "inf": []})
    for m in mtcc:
        pairs[(m["origin_code"], m["destination_code"])]["mtcc"].append(m)
    for s in services:
        codes = [x["island_code"] for x in s["stops"]]
        for i, a in enumerate(codes):
            for b in codes[i + 1:]:
                pairs[(a, b)]["inf"].append((s, i, codes.index(b)))
    rows = []
    for (o, d), v in sorted(pairs.items()):
        per_day = {ab: sorted({m["dep_s"] // 60 for m in v["mtcc"] if ab in m["weekdays"]}) for ab in ABBR}
        gaps = [(y - x, x, y, ab) for ab, ts in per_day.items() for x, y in zip(ts, ts[1:])]  # within-day gaps only
        longest = max(gaps) if gaps else None
        base = {"origin_code": o, "origin_name": code_name[o], "destination_code": d, "destination_name": code_name[d],
                "pair_longest_mtcc_gap_min": longest[0] if longest else "",
                "pair_longest_mtcc_gap_window": (f"{longest[3]} {longest[1]//60:02d}:{longest[1]%60:02d}-{longest[2]//60:02d}:{longest[2]%60:02d}" if longest else "")}
        for m in sorted(v["mtcc"], key=lambda m: (m["dep_s"], m["trip_id"])):
            rows.append({**base, "source": "mtcc_published_schedule", "ref_id": m["trip_id"], "route_or_signature": m["route_code"],
                         "departure_time": m["departure"], "departure_p10": "", "departure_p90": "", "arrival_time": m["arrival"],
                         "days": ",".join(m["weekdays"]), "days_seen_of_31": "", "calendar_status": m["calendar_status"],
                         "inferred_label": "", "long_mtcc_wait": "", "mtcc_wait_min_range": "",
                         "note": "published schedule, not evidence the trip operated"})
        mtcc_pair = bool(v["mtcc"])
        for s, i, j in sorted(v["inf"], key=lambda x: (x[0]["stops"][x[1]]["time_median"], x[0]["service_id"])):
            st = s["stops"][i]
            med = int(st["time_median"][:2]) * 60 + int(st["time_median"][3:])
            waits, none_days = [], []
            for ab, ts in per_day.items():
                nxt = [t for t in ts if t >= med]
                if nxt:
                    waits.append(nxt[0] - med)
                elif ts:
                    none_days.append(ab)
            if not mtcc_pair:
                long_wait, wrange = "no_mtcc_departures_on_pair", ""
            else:
                long_wait = bool(none_days) or (bool(waits) and max(waits) >= long_gap)
                wrange = (f"{min(waits)}-{max(waits)}" if waits else "") + (f" (none after on {','.join(none_days)})" if none_days else "")
            rows.append({**base, "source": "inferred_from_device_movements", "ref_id": s["service_id"],
                         "route_or_signature": s["signature_id"], "departure_time": st["time_median"],
                         "departure_p10": st["time_p10"], "departure_p90": st["time_p90"], "arrival_time": "",
                         "days": s["days_seen_by_weekday"], "days_seen_of_31": s["days_seen"], "calendar_status": "inferred",
                         "inferred_label": s["inferred_label"], "long_mtcc_wait": long_wait, "mtcc_wait_min_range": wrange,
                         "note": "observed device movements consistent with this pattern; not a timetable"})
    return rows
