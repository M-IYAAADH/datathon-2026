"""Jetty selection, island sites and per-observation zone distances."""
from __future__ import annotations

import csv
import json
import re
from pathlib import Path

import numpy as np
import shapely
from pyproj import Transformer
from shapely.geometry import shape
from shapely.ops import transform as shp_transform

_TO_UTM = Transformer.from_crs(4326, 32643, always_xy=True)
OSM_ID = re.compile(r"^(node|way|relation)/\d+$")
CONF_RANK = {"low": 0, "medium": 1, "high": 2}
M_PER_DEG_LAT = 110_574.0
M_PER_DEG_LON_EQ = 111_320.0


def load_islands(path: Path) -> dict:
    out = {}
    for f in json.loads(Path(path).read_text(encoding="utf-8"))["features"]:
        p = f["properties"]
        out[p["FCODE"]] = {"code": p["FCODE"], "name": p["islandName"], "atoll": p["atoll"],
                           "category": p["category"], "geom": shape(f["geometry"])}
    return out


def read_harbour_rows(path: Path, verification_path: Path | None = None) -> tuple[list[dict], list[str]]:
    """Read the harbour reference; an optional manual verification CSV adds a verification_status column
    (keyed on harbour_ref_id, blank for rows it does not list)."""
    with open(path, newline="", encoding="utf-8-sig") as fh:
        rd = csv.DictReader(fh)
        rows, columns = list(rd), list(rd.fieldnames or [])
    if verification_path is not None and Path(verification_path).exists():
        with open(verification_path, newline="", encoding="utf-8-sig") as fh:
            status = {r["harbour_ref_id"]: (r.get("verification_status") or "").strip() for r in csv.DictReader(fh)}
        for r in rows:
            r["verification_status"] = status.get(r["harbour_ref_id"], "")
        if "verification_status" not in columns:
            columns.append("verification_status")
    return rows, columns


def jetty_decision(row: dict, columns: list[str], cfg: dict) -> tuple[bool, str]:
    """Return (usable, reason) for one harbour reference row."""
    j = cfg["jetty"]
    if row["feature_class"] in j["excluded_feature_classes"]:
        return False, f"excluded_feature_class:{row['feature_class']}"
    name = (row.get("name") or "").lower()
    if any(tok in name for tok in j["excluded_name_tokens"]):
        return False, "excluded_name_token"
    if (row.get("review_status") or "").lower() == "rejected":
        return False, "review_rejected"
    col = j["verification_column"]
    if col in columns:
        ok = (row.get(col) or "").upper() in j["accepted_verification"]
        return ok, "verified_or_corrected" if ok else f"{col}:{row.get(col) or 'blank'}"
    if not OSM_ID.match(row.get("source_feature_id") or ""):
        return False, "no_numeric_osm_id"
    if CONF_RANK.get(row.get("confidence"), -1) < CONF_RANK[j["fallback_min_confidence"]]:
        return False, f"confidence:{row.get('confidence')}"
    return True, "numeric_osm_id_and_high_confidence"


def select_jetties(rows: list[dict], columns: list[str], cfg: dict, scope_codes: set[str]):
    used, skipped = [], []
    for r in rows:
        if r.get("island_id") not in scope_codes:
            continue
        ok, reason = jetty_decision(r, columns, cfg)
        (used if ok else skipped).append({"harbour_ref_id": r["harbour_ref_id"], "island_code": r["island_id"],
                                          "island_name": r["island_name"], "name": r.get("name", ""),
                                          "feature_class": r["feature_class"], "confidence": r["confidence"],
                                          "source_feature_id": r["source_feature_id"],
                                          "longitude": float(r["longitude"]), "latitude": float(r["latitude"]),
                                          "decision": "used" if ok else "skipped", "reason": reason})
    return used, skipped


def scope_island_codes(cfg: dict, scope: str, islands: dict, rows: list[dict], columns: list[str]) -> list[str]:
    if scope == "corridor":
        return [i["island_code"] for i in cfg["scopes"]["corridor"]["islands"]]
    sc = cfg["scopes"]["full"]
    codes = {c for c, i in islands.items() if i["category"] in sc["island_categories"]}
    if sc["include_islands_with_terminal_class_jetty"] and cfg["stops"]["stop_location_mode"] == "jetty":
        for r in rows:
            if r["feature_class"] in sc["terminal_classes"] and r.get("island_id") in islands \
                    and jetty_decision(r, columns, cfg)[0]:
                codes.add(r["island_id"])
    return sorted(codes)


def uses_shoreline(site: dict) -> bool:
    """True when the site's stop zone is a band around the OneMap shoreline rather than a jetty point."""
    return bool(site.get("shoreline") or site["no_jetty_point"])


def build_sites(codes: list[str], used: list[dict], islands: dict, mode: str = "jetty") -> list[dict]:
    """mode island_shoreline: every site is a shoreline band and the harbour reference is not consulted (`used` is ignored).
    mode jetty: verified jetty points, with the shoreline band as the flagged no_jetty_point fallback."""
    sites = []
    for idx, code in enumerate(codes):
        isl = islands[code]
        jets = [] if mode == "island_shoreline" else [u for u in used if u["island_code"] == code]
        if jets:
            anchor = (float(np.mean([j["longitude"] for j in jets])), float(np.mean([j["latitude"] for j in jets])))
        else:
            rp = isl["geom"].representative_point()  # direction ordering only, never a stop location
            anchor = (rp.x, rp.y)
        sites.append({"idx": idx, "code": code, "name": isl["name"], "category": isl["category"], "atoll": isl["atoll"],
                      "jetties": jets, "no_jetty_point": mode == "jetty" and not jets,
                      "shoreline": mode == "island_shoreline", "anchor": anchor})
    return sites


def to_utm(geom):
    return shp_transform(_TO_UTM.transform, geom)


def project_points(lon, lat):
    x, y = _TO_UTM.transform(np.asarray(lon, float), np.asarray(lat, float))
    return np.asarray(x), np.asarray(y)


def assign_zones(lat, lon, sites, islands, max_radius_m, margin_deg=0.004):
    """Nearest scope site per observation: (zone idx, distance m, jetty idx). Distance is to the jetty point,
    or to the island shoreline for shoreline sites (every site in island_shoreline mode, no_jetty_point sites in
    jetty mode); observations farther than max_radius_m stay -1."""
    n = len(lat)
    zone = np.full(n, -1, np.int16)
    dist = np.full(n, np.inf, np.float32)
    jet = np.full(n, -1, np.int16)
    for s in sites:
        if uses_shoreline(s):
            minx, miny, maxx, maxy = islands[s["code"]]["geom"].bounds
        else:
            lons = [j["longitude"] for j in s["jetties"]]
            lats = [j["latitude"] for j in s["jetties"]]
            minx, maxx, miny, maxy = min(lons), max(lons), min(lats), max(lats)
        cand = np.flatnonzero((lat >= miny - margin_deg) & (lat <= maxy + margin_deg)
                              & (lon >= minx - margin_deg) & (lon <= maxx + margin_deg))
        if not len(cand):
            continue
        if uses_shoreline(s):
            x, y = project_points(lon[cand], lat[cand])
            d = shapely.distance(shapely.points(x, y), to_utm(islands[s["code"]]["geom"]).boundary)
            which = np.zeros(len(cand), np.int16)
        else:
            d = np.full(len(cand), np.inf)
            which = np.zeros(len(cand), np.int16)
            for k, j in enumerate(s["jetties"]):
                dx = (lon[cand] - j["longitude"]) * M_PER_DEG_LON_EQ * np.cos(np.radians(j["latitude"]))
                dy = (lat[cand] - j["latitude"]) * M_PER_DEG_LAT
                dk = np.hypot(dx, dy)
                better = dk < d
                d = np.where(better, dk, d)
                which = np.where(better, k, which)
        upd = (d <= max_radius_m) & (d < dist[cand])
        sel = cand[upd]
        zone[sel], dist[sel], jet[sel] = s["idx"], d[upd], which[upd]
    return zone, dist, jet
