"""Signatures, canonical routes, departure clustering, weekday patterns and disclosure."""
from __future__ import annotations

import hashlib
from collections import Counter
from datetime import date, timedelta

import numpy as np

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def signature_id(islands: tuple, direction: str) -> str:
    return "sig_" + hashlib.sha1((direction + "|" + ">".join(islands)).encode()).hexdigest()[:10]


def skipped_in(seq: tuple, canon: tuple, max_skip: int):
    """Stops of canon omitted by seq if seq is an ordered subsequence of canon omitting <= max_skip, else None."""
    if len(canon) - len(seq) > max_skip or len(canon) < len(seq):
        return None
    i, skipped = 0, []
    for c in canon:
        if i < len(seq) and seq[i] == c:
            i += 1
        else:
            skipped.append(c)
    return tuple(skipped) if i == len(seq) and len(skipped) <= max_skip else None


def build_canonical(groups: dict, max_skip: int, min_days: int) -> dict:
    """Map every (islands, direction) key to (canonical key, skipped stops).

    A signature with its own support >= min_days becomes canonical unless it is an at-most-max_skip subsequence of
    a longer canonical route, in which case it is a variant of that route (the omitted stop is recorded).
    """
    keys = sorted(groups, key=lambda k: (-len(k[0]), -len(groups[k]["days"]), k))
    canon: list = []
    mapping: dict = {}
    for k in keys:
        islands, direction = k
        best = None
        for c in canon:
            if c[1] != direction or len(c[0]) <= len(islands):
                continue
            sk = skipped_in(islands, c[0], max_skip)
            if sk is not None and (best is None or len(groups[c]["days"]) > len(groups[best[0]]["days"])):
                best = (c, sk)
        if best:
            mapping[k] = best
        else:
            mapping[k] = (k, ())
            if len(groups[k]["days"]) >= min_days:
                canon.append(k)
    return mapping


def cluster_departures(minutes, days, window, min_days=2):
    """Greedy deterministic clustering of departure minutes-of-day: repeatedly take the window of width `window`
    containing the most distinct days. Returns a label per trip (-1 = unclustered)."""
    minutes = np.asarray(minutes, float)
    labels = np.full(len(minutes), -1, int)
    remaining = np.arange(len(minutes))
    label = 0
    while len(remaining):
        order = remaining[np.argsort(minutes[remaining], kind="stable")]
        m = minutes[order]
        best, best_days = None, 0
        for i in range(len(order)):
            j = int(np.searchsorted(m, m[i] + window, side="right"))
            nd = len({days[x] for x in order[i:j]})
            if nd > best_days:
                best, best_days = (i, j), nd
        if best is None or best_days < min_days:
            break
        members = order[best[0]:best[1]]
        labels[members] = label
        label += 1
        remaining = np.setdiff1d(remaining, members, assume_unique=True)
    return labels


def weekday_pattern(dates: set, period_start: date, days_in_period: int, skip_min_occurrences: int) -> dict:
    occ = Counter((period_start + timedelta(d)).weekday() for d in range(days_in_period))
    seen = Counter(d.weekday() for d in dates)
    by = "|".join(f"{WEEKDAYS[w]}:{seen[w]}/{occ[w]}" for w in range(7))
    skipped = [WEEKDAYS[w] for w in range(7) if occ[w] >= skip_min_occurrences and seen[w] == 0]
    return {"days_seen_by_weekday": by, "weekdays_not_observed": ",".join(skipped)}


def hhmm(minutes: float) -> str:
    m = int(round(minutes)) % 1440
    return f"{m // 60:02d}:{m % 60:02d}"


def passes_disclosure(devices: int, days: int, cfg: dict) -> bool:
    d = cfg["disclosure"]
    return devices >= d["min_distinct_devices"] and days >= d["min_distinct_days"]


def plurality(classes: list[str]) -> tuple[str, float, str]:
    c = Counter(classes)
    ranked = sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))
    mix = "|".join(f"{k}:{round(100 * v / len(classes))}%" for k, v in ranked)
    return ranked[0][0], ranked[0][1] / len(classes), mix


def service_label(cls: str, regular: bool) -> str:
    if cls == "fast_vessel":
        return "inferred speedboat service" if regular else "inferred fast-vessel movements (not regular)"
    if cls == "slow_vessel":
        return "inferred slow ferry/dhoni service" if regular else "inferred supply/other"
    return "inferred vessel service (class unclear)" if regular else "inferred vessel movements (class unclear, not regular)"
