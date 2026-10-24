"""Harbour reference logic: ids, island assignment, ferry-stop matching, grouping, confidence."""
from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

import pyproj
from shapely import STRtree
from shapely.geometry import Point, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform

ATOLL_CODES = {"Alifu Alifu": "AA", "Alifu Dhaalu": "ADh", "Kaafu": "K", "Vaavu": "V", "MLE": "Greater_Male"}
_TO_UTM = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:32643", always_xy=True).transform  # UTM 43N, metres

TERMINAL_CLASSES = {"ferry_terminal", "ferry_platform", "ferry_station", "ferry_stop_position"}
AREA_NAMED_CLASSES = {"port_area", "harbour", "marina", "dock", "seamark_harbour"}
GENERIC_CLASSES = {"breakwater", "quay", "ferry_route_endpoint"}


def project(geom: BaseGeometry) -> BaseGeometry:
    return transform(_TO_UTM, geom)


def make_ref_id(source: str, source_feature_id: str) -> str:
    """Stable id from source + source id only (never row order)."""
    slug = re.sub(r"[^a-z0-9]+", "-", source_feature_id.lower()).strip("-")
    return f"hr-{source.replace('_', '-')}-{slug}"


def norm_name(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "", text)


def name_tokens(text: str) -> set[str]:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    text = text.replace("'", "")
    return {t for t in re.split(r"[^a-z0-9]+", text) if t}


class IslandIndex:
    """Island polygons in metres. Assignment is by polygon containment or nearest shoreline only."""

    def __init__(self, features: list[dict], max_offshore_m: float):
        self.max_offshore_m = max_offshore_m
        self.props = [f["properties"] for f in features]
        self.geoms = [project(shape(f["geometry"])) for f in features]
        self.tree = STRtree(self.geoms)

    def assign(self, rep_lon: float, rep_lat: float, geometry: BaseGeometry) -> dict:
        rep = project(Point(rep_lon, rep_lat))
        for i in sorted(self.tree.query(rep, predicate="intersects")):
            if self.geoms[i].covers(rep):
                return self._result(i, "within_polygon", 0.0)
        proj_geom = project(geometry)
        idx = self.tree.query(proj_geom.buffer(self.max_offshore_m))
        best = None
        for i in sorted(idx):
            d = float(self.geoms[i].distance(proj_geom))
            if d <= self.max_offshore_m and (best is None or d < best[1] - 1e-9):
                best = (int(i), d)
        if best is None:
            return {"island_id": "", "island_name": "", "island_atoll": "", "study_area_code": "",
                    "island_assignment": "none", "island_distance_m": ""}
        return self._result(best[0], "nearest_shoreline", best[1])

    def _result(self, i: int, method: str, dist: float) -> dict:
        p = self.props[i]
        return {"island_id": p.get("FCODE", ""), "island_name": p.get("islandName", ""),
                "island_atoll": p.get("atoll", ""), "study_area_code": ATOLL_CODES.get(p.get("atoll", ""), ""),
                "island_assignment": method, "island_distance_m": round(dist, 1)}


class FerryStopMatcher:
    def __init__(self, stops: list[dict], island_aliases: dict[str, list[str]] | None = None):
        self.aliases = {k: set(v) for k, v in (island_aliases or {}).items()}
        self.stops = []
        for s in stops:
            self.stops.append({"stop_id": s["stop_id"], "island": s["island_name_en"],
                               "norm": norm_name(s["island_name_en"]),
                               "atoll": (s["atoll_abbreviation"] or "").lower()})

    def _atoll_ok(self, stop: dict, code: str, unassigned_ok: bool) -> bool:
        if stop["atoll"] == "":  # Malé-area sheets carry no atoll prefix
            return code in {"greater_male", "k", ""}
        return code == stop["atoll"] or (not code and unassigned_ok)

    def match(self, rec: dict) -> tuple[str, str]:
        """Return ("stop_id|island", method) or ("", ""). Methods, in order of strength:
        island_name (assigned island equals stop island), island_alias (reviewed alias table),
        feature_name_token (feature name contains the island name, only when it does not contradict the
        assigned island; never for route endpoints, whose names describe journeys)."""
        code = (rec.get("study_area_code") or "").lower()
        island_norm = norm_name(rec.get("island_name", ""))
        tokens = {norm_name(t) for t in name_tokens(rec.get("name", ""))}
        for method in ("island_name", "island_alias", "feature_name_token"):
            hits = []
            for s in self.stops:
                if method == "island_name":
                    ok = bool(island_norm) and island_norm == s["norm"]
                elif method == "island_alias":
                    ok = bool(island_norm) and island_norm in self.aliases.get(s["norm"], set())
                else:
                    ok = (rec["feature_class"] != "ferry_route_endpoint" and s["norm"] in tokens
                          and (not island_norm or island_norm == s["norm"]
                               or island_norm in self.aliases.get(s["norm"], set())))
                if ok and self._atoll_ok(s, code, method == "feature_name_token"):
                    hits.append(s)
            if hits:
                hits.sort(key=lambda s: s["stop_id"])
                return f"{hits[0]['stop_id']}|{hits[0]['island']}", method
        return "", ""


def confidence(rec: dict) -> str:
    fclass = rec["feature_class"]
    assigned = rec.get("island_assignment") in {"within_polygon", "nearest_shoreline"}
    near = assigned and (rec["island_assignment"] == "within_polygon" or float(rec["island_distance_m"]) <= 100)
    if fclass == "ferry_route_endpoint" or not assigned:
        return "low"
    if fclass in TERMINAL_CLASSES and rec.get("name") and near:
        return "high"
    if fclass in AREA_NAMED_CLASSES and rec.get("name") and near:
        return "high"
    if fclass in GENERIC_CLASSES and not rec.get("name"):
        return "low"
    return "medium"


def group_features(recs: list[dict], distance_m: float, min_similarity: float) -> list[str]:
    """Single-linkage groups; ids derive from the sorted member ref ids so reruns are identical."""
    pts = [project(Point(r["rep_lon"], r["rep_lat"])) for r in recs]
    tree = STRtree(pts)
    parent = list(range(len(recs)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    names = [norm_name(r.get("name", "")) for r in recs]
    for i, p in enumerate(pts):
        for j in tree.query(p.buffer(distance_m)):
            if j <= i or p.distance(pts[j]) > distance_m:
                continue
            if not names[i] and not names[j]:
                linked = recs[i]["source"] != recs[j]["source"]  # unnamed segments never chain within one source
            elif not names[i] or not names[j]:
                linked = True
            else:
                linked = SequenceMatcher(None, names[i], names[j]).ratio() >= min_similarity
            if linked:
                parent[find(i)] = find(j)
    members: dict[int, list[str]] = {}
    for i, r in enumerate(recs):
        members.setdefault(find(i), []).append(r["harbour_ref_id"])
    import hashlib
    gid = {}
    for root, ids in members.items():
        digest = hashlib.sha1("|".join(sorted(ids)).encode()).hexdigest()[:10]
        for rid in ids:
            gid[rid] = f"hg-{digest}"
    return [gid[r["harbour_ref_id"]] for r in recs]
