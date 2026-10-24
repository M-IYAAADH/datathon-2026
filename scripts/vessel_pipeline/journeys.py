"""Visit sequences, journeys (one direction of travel) and trips between consecutive stops."""
from __future__ import annotations

from collections import defaultdict


def build_visits(stops: dict, traj) -> dict[int, list[dict]]:
    """Order stops by time within each trajectory and collapse repeated consecutive stops at the same island."""
    by_traj: dict[int, list[dict]] = defaultdict(list)
    for i in range(len(stops["first"])):
        tj = int(traj[stops["first"][i]])
        v = {"idx": int(stops["first"][i]), "zone": int(stops["zone"][i]), "jetty": int(stops["jetty"][i]), "arrive": int(stops["start"][i]),
             "depart": int(stops["end"][i]), "n_obs": int(stops["n_obs"][i]),
             "unconfirmed": bool(stops["unconfirmed"][i]), "arrive_known": not bool(stops["start_truncated"][i]),
             "depart_known": not bool(stops["end_truncated"][i])}
        seq = by_traj[tj]
        if seq and seq[-1]["zone"] == v["zone"]:
            p = seq[-1]
            p["depart"], p["depart_known"] = v["depart"], v["depart_known"]
            p["n_obs"] += v["n_obs"]
            p["unconfirmed"] = p["unconfirmed"] or v["unconfirmed"]
        else:
            seq.append(v)
    return dict(by_traj)


def split_journeys(visits: list[dict], dist_to_ref: list[float]) -> list[tuple[str, list[dict]]]:
    """Split a trajectory's visit sequence where the direction relative to the reference island reverses.
    The turnaround visit ends one journey and starts the next. Returns [(direction, visits)] with >= 2 visits."""
    out, cur, sign = [], [], 0
    for v in visits:
        if not cur:
            cur = [v]
            continue
        delta = dist_to_ref[v["zone"]] - dist_to_ref[cur[-1]["zone"]]
        s = -1 if delta < 0 else 1
        if sign and s != sign:
            out.append((sign, cur))
            cur = [cur[-1]]
        sign = s
        cur.append(v)
    if len(cur) >= 2:
        out.append((sign, cur))
    return [("toward_male" if s < 0 else "away_from_male", vs) for s, vs in out if len(vs) >= 2]


def trips_from_visits(visits: list[dict]) -> list[dict]:
    """One trip per pair of consecutive visits (different islands by construction)."""
    return [{"origin": a, "destination": b} for a, b in zip(visits, visits[1:])]
