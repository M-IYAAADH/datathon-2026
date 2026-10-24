"""Stop detection near jetties. Pure numpy: inputs are observation arrays ordered by (trajectory, time)."""
from __future__ import annotations

import numpy as np


def detect_stops(t, break_before, zone, dist, jet, radius_m, dwell_s, min_obs, gap_flag_s):
    """Runs of consecutive observations inside radius_m of the same zone.

    A run never extends across a break (trajectory start, trajectory split or a non-accepted transition), so
    nothing is bridged or interpolated. Returns dict of arrays, one entry per accepted stop, in observation order.
    start_truncated: the arrival was not observed (the run begins at a break). end_truncated: the departure was not
    observed. unconfirmed: a gap longer than gap_flag_s lies inside the run.
    """
    n = len(t)
    empty = {k: np.zeros(0, d) for k, d in (("first", np.int64), ("last", np.int64), ("zone", np.int16),
                                            ("jetty", np.int16), ("start", np.int64), ("end", np.int64),
                                            ("n_obs", np.int64), ("max_gap", np.int64), ("unconfirmed", bool),
                                            ("start_truncated", bool), ("end_truncated", bool))}
    if n == 0:
        return empty
    near = np.where((zone >= 0) & (dist <= radius_m), zone, -1).astype(np.int32)
    inside = near >= 0
    prev_same = np.zeros(n, bool)
    prev_same[1:] = (near[1:] == near[:-1]) & inside[1:]
    cont = prev_same & ~break_before
    start_flag = inside & ~cont
    run_id = np.cumsum(start_flag)
    rows = np.flatnonzero(inside)
    if not len(rows):
        return empty
    rid = run_id[rows]
    new = np.r_[True, rid[1:] != rid[:-1]]
    first = rows[new]
    last = rows[np.r_[rid[1:] != rid[:-1], True]]
    nruns = len(first)
    dense = np.cumsum(new) - 1  # run ordinal per entry of rows
    gap = np.zeros(n, np.int64)
    gap[1:] = t[1:] - t[:-1]
    max_gap = np.zeros(nruns, np.int64)
    cont_rows = np.flatnonzero(cont)
    if len(cont_rows):
        pos = np.searchsorted(rows, cont_rows)
        np.maximum.at(max_gap, dense[pos], gap[cont_rows])
    order = np.lexsort((dist[rows], dense))
    best_first = np.r_[True, dense[order][1:] != dense[order][:-1]]
    best_rows = rows[order][best_first]
    nxt = last + 1
    end_trunc = (nxt >= n)
    inb = nxt < n
    end_trunc[inb] = break_before[nxt[inb]]
    keep = ((t[last] - t[first]) >= dwell_s) & ((last - first + 1) >= min_obs)
    return {"first": first[keep], "last": last[keep], "zone": near[first][keep].astype(np.int16),
            "jetty": jet[best_rows][keep], "start": t[first][keep], "end": t[last][keep],
            "n_obs": (last - first + 1)[keep], "max_gap": max_gap[keep], "unconfirmed": (max_gap > gap_flag_s)[keep],
            "start_truncated": break_before[first][keep], "end_truncated": end_trunc[keep]}
