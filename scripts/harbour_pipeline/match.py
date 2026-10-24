"""Preconditions gate and cluster-to-reference classification."""
from __future__ import annotations

import json
from pathlib import Path

from shapely import STRtree
from shapely.geometry import Point, shape

from .reference import IslandIndex, project


def check_preconditions(private_dir: Path) -> list[str]:
    """Return a list of blocking problems; empty means GPS outputs are complete and valid."""
    problems = []
    for name in ("failed_run.json", "regional_failed_run.json", ".run.lock"):
        if (private_dir / name).exists():
            problems.append(f"{name} exists")
    for name in ("run_manifest.json", "regional_run_manifest.json"):
        path = private_dir / name
        if not path.exists():
            problems.append(f"{name} missing")
            continue
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            problems.append(f"{name} unreadable")
            continue
        if manifest.get("status") != "complete":
            problems.append(f"{name} status is {manifest.get('status')!r}, not 'complete'")
        validation = manifest.get("validation")
        if not isinstance(validation, dict) or not validation:
            problems.append(f"{name} has no validation values")
        elif not all(_all_true(v) for v in validation.values()):
            problems.append(f"{name} has validation values that are not true")
    return problems


def _all_true(value) -> bool:
    if isinstance(value, dict):
        return bool(value) and all(_all_true(v) for v in value.values())
    return value is True


class ReferenceIndex:
    def __init__(self, features: list[dict], excluded_classes: set[str]):
        self.props, self.geoms = [], []
        for f in features:
            p = f["properties"]
            if p["feature_class"] in excluded_classes:
                continue
            self.props.append(p)
            self.geoms.append(project(shape(f["geometry"])))
        self.tree = STRtree(self.geoms)

    def nearest(self, lon: float, lat: float, k: int, search_m: float) -> list[tuple[dict, float]]:
        pt = project(Point(lon, lat))
        cand = self.tree.query(pt.buffer(search_m))
        scored = sorted(((float(self.geoms[i].distance(pt)), self.props[i]["harbour_ref_id"], int(i)) for i in cand))
        return [(self.props[i], d) for d, _, i in scored[:k]]


def island_distance(index: IslandIndex, lon: float, lat: float, search_m: float) -> tuple[dict | None, float | None]:
    pt = project(Point(lon, lat))
    best = None
    for i in sorted(index.tree.query(pt.buffer(search_m))):
        d = 0.0 if index.geoms[i].covers(pt) else float(index.geoms[i].distance(pt))
        if d <= search_m and (best is None or d < best[1]):
            best = (int(i), d)
    return (index.props[best[0]], best[1]) if best else (None, None)


def classify_cluster(cluster: dict, refs: ReferenceIndex, islands: IslandIndex, cfg: dict, min_devices: int,
                     min_days: int) -> dict:
    m = cfg["matching"]
    near = refs.nearest(cluster["centroid_longitude"], cluster["centroid_latitude"], m["nearest_k"],
                        max(m["reference_distance_m"] * 20, 3000))
    island, idist = island_distance(islands, cluster["centroid_longitude"], cluster["centroid_latitude"],
                                    m["near_island_m"])
    if cluster["distinct_devices"] < min_devices or cluster["distinct_days"] < min_days:
        label = "insufficient"
    elif near and near[0][1] <= m["reference_distance_m"]:
        label = "matched_reference"
    elif island is not None:
        label = "near_island_no_reference"
    else:
        label = "offshore"
    return {"classification": label, "nearest": near, "island": island, "island_distance_m": idist}


def load_reference_features(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))["features"]
