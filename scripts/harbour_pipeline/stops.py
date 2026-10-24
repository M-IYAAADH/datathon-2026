"""Stay-point detection, deterministic DBSCAN clustering and cluster statistics (numpy only)."""
from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict

import numpy as np
import pyproj

_EARTH_RADIUS_M = 6371008.8
_TO_UTM = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:32643", always_xy=True)


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * _EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def detect_stay_points(t_sec, lat, lon, break_before, radius_m: float, dwell_s: float,
                       min_observations: int = 2) -> list[tuple[int, int]]:
    """Return inclusive (start, end) index pairs. Inputs are time-sorted.

    break_before[k] True means no bridge between k-1 and k (trajectory split, rejected transition, or
    first observation). A window never extends across a break and nothing is interpolated.
    """
    n = len(t_sec)
    out: list[tuple[int, int]] = []
    i = 0
    while i < n:
        j = i
        while j + 1 < n and not break_before[j + 1] and haversine_m(lat[i], lon[i], lat[j + 1], lon[j + 1]) <= radius_m:
            j += 1
        if j > i and (j - i + 1) >= min_observations and (t_sec[j] - t_sec[i]) >= dwell_s:
            out.append((i, j))
            i = j + 1
        else:
            i += 1
    return out


def dbscan(lat, lon, eps_m: float, min_samples: int, tie_break) -> np.ndarray:
    """Deterministic DBSCAN. Labels are 0..k-1 in discovery order over (lat, lon, tie_break) order; -1 is noise."""
    lat = np.asarray(lat, dtype=float)
    lon = np.asarray(lon, dtype=float)
    n = len(lat)
    labels = np.full(n, -1, dtype=int)
    if n == 0:
        return labels
    x, y = _TO_UTM.transform(lon, lat)
    order = sorted(range(n), key=lambda k: (lat[k], lon[k], tie_break[k]))
    cells: dict[tuple[int, int], list[int]] = defaultdict(list)
    for k in order:
        cells[(math.floor(x[k] / eps_m), math.floor(y[k] / eps_m))].append(k)
    eps2 = eps_m * eps_m

    def neighbours(k: int) -> list[int]:
        cx, cy = math.floor(x[k] / eps_m), math.floor(y[k] / eps_m)
        found = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for m in cells.get((cx + dx, cy + dy), ()):
                    if (x[m] - x[k]) ** 2 + (y[m] - y[k]) ** 2 <= eps2:
                        found.append(m)
        return sorted(found, key=lambda m: (lat[m], lon[m], tie_break[m]))

    visited = np.zeros(n, dtype=bool)
    cluster = 0
    for k in order:
        if visited[k]:
            continue
        visited[k] = True
        nb = neighbours(k)
        if len(nb) < min_samples:
            continue
        labels[k] = cluster
        queue = list(nb)
        qi = 0
        while qi < len(queue):
            m = queue[qi]
            qi += 1
            if labels[m] == -1:
                labels[m] = cluster
            if visited[m]:
                continue
            visited[m] = True
            nb2 = neighbours(m)
            if len(nb2) >= min_samples:
                queue.extend(nb2)
        cluster += 1
    return labels


def summarize_clusters(stays: dict, labels, min_devices: int, min_days: int) -> list[dict]:
    """Per-cluster statistics. stays has arrays: lat, lon, device_key, day, dwell_s, hour, stay_id."""
    labels = np.asarray(labels)
    lat, lon = np.asarray(stays["lat"], float), np.asarray(stays["lon"], float)
    rows = []
    for lab in sorted(set(labels.tolist()) - {-1}):
        idx = np.flatnonzero(labels == lab)
        clat, clon = float(lat[idx].mean()), float(lon[idx].mean())
        x, y = _TO_UTM.transform(lon[idx], lat[idx])
        cx, cy = _TO_UTM.transform(clon, clat)
        dist = np.hypot(np.asarray(x) - cx, np.asarray(y) - cy)
        dwell = np.asarray(stays["dwell_s"], float)[idx]
        hours = np.bincount(np.asarray(stays["hour"], int)[idx], minlength=24)
        devices = len({stays["device_key"][i] for i in idx})
        days = len({stays["day"][i] for i in idx})
        rows.append({
            "centroid_latitude": round(clat, 7), "centroid_longitude": round(clon, 7),
            "stop_count": int(len(idx)), "distinct_devices": devices, "distinct_days": days,
            "dwell_p10_s": round(float(np.quantile(dwell, 0.1)), 1), "dwell_p50_s": round(float(np.quantile(dwell, 0.5)), 1),
            "dwell_p90_s": round(float(np.quantile(dwell, 0.9)), 1),
            "hour_profile_json": json.dumps([int(h) for h in hours]),
            "spread_p50_m": round(float(np.quantile(dist, 0.5)), 1), "spread_p90_m": round(float(np.quantile(dist, 0.9)), 1),
            "spread_max_m": round(float(dist.max()), 1),
            "meets_disclosure_minimum": bool(devices >= min_devices and days >= min_days),
            "_members": [int(i) for i in idx],
        })
    rows.sort(key=lambda r: (r["centroid_latitude"], r["centroid_longitude"]))
    for rank, r in enumerate(rows):
        r["cluster_id"] = f"sc_{rank:05d}"
    return rows


def stay_id(trajectory_id: str, start_observation_id: str) -> str:
    return hashlib.sha256(f"{trajectory_id}|{start_observation_id}".encode()).hexdigest()[:16]
