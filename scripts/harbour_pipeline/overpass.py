"""Overpass query builder and parser. Raw JSON is untrusted: every field is type-checked."""
from __future__ import annotations

import json
from pathlib import Path

from shapely.geometry import LineString, MultiPolygon, Point, Polygon, mapping
from shapely.geometry.base import BaseGeometry
from shapely.ops import linemerge, polygonize, unary_union

# (key, value-regex or None for any value) -> OSM selectors, with ferry-conditioned public_transport
SELECTORS = [
    '["amenity"="ferry_terminal"]',
    '["public_transport"~"^(station|platform|stop_position)$"]["ferry"="yes"]',
    '["man_made"~"^(pier|breakwater|quay)$"]',
    '["harbour"]',
    '["seamark:type"~"^(harbour|mooring|small_craft_facility)$"]',
    '["leisure"~"^(marina|slipway)$"]',
    '["waterway"="dock"]',
    '["landuse"~"^(harbour|port)$"]',
    '["industrial"="port"]',
]


def study_bbox(bounds: tuple[float, float, float, float], margin: float) -> tuple[float, float, float, float]:
    west, south, east, north = bounds
    return (south - margin, west - margin, north + margin, east + margin)  # Overpass order: S,W,N,E


def build_overpass_query(cfg: dict) -> str:
    bounds = tuple(cfg["_study_bounds"]) if "_study_bounds" in cfg else _bounds_from_config(cfg)
    s, w, n, e = study_bbox(bounds, cfg["bbox_margin_degrees"])
    box = f"{s:.6f},{w:.6f},{n:.6f},{e:.6f}"
    parts = [f"  nwr{sel}({box});" for sel in SELECTORS]
    parts.append(f'  relation["route"="ferry"]({box});')
    parts.append(f'  way["route"="ferry"]({box});')
    body = "\n".join(parts)
    return f"[out:json][timeout:{cfg['overpass']['timeout_seconds']}];\n(\n{body}\n);\nout geom tags;\n"


def _bounds_from_config(cfg: dict) -> tuple[float, float, float, float]:
    root = Path(__file__).resolve().parents[2]
    prov = json.loads((root / "data/reference/followme/geography_provenance.json").read_text(encoding="utf-8"))
    west, south, east, north = prov["geographic_bounds"]
    return (west, south, east, north)


def classify(tags: dict[str, str]) -> str:
    if tags.get("amenity") == "ferry_terminal":
        return "ferry_terminal"
    if tags.get("ferry") == "yes" and tags.get("public_transport") in {"station", "platform", "stop_position"}:
        return f"ferry_{tags['public_transport']}"
    mm = tags.get("man_made")
    if mm in {"pier", "breakwater", "quay"}:
        return mm
    if tags.get("leisure") in {"marina", "slipway"}:
        return tags["leisure"]
    if tags.get("waterway") == "dock":
        return "dock"
    if tags.get("landuse") in {"harbour", "port"} or tags.get("industrial") == "port":
        return "port_area"
    if "harbour" in tags:
        return "harbour"
    if tags.get("seamark:type") in {"harbour", "mooring", "small_craft_facility"}:
        return "seamark_" + tags["seamark:type"]
    if tags.get("route") == "ferry":
        return "ferry_route_endpoint"
    return "other"


def _coords(geom_list) -> list[tuple[float, float]]:
    out = []
    for p in geom_list or []:
        if isinstance(p, dict) and isinstance(p.get("lon"), (int, float)) and isinstance(p.get("lat"), (int, float)):
            out.append((float(p["lon"]), float(p["lat"])))
    return out


def _clean_tags(raw) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items()}


def _way_geometry(coords: list[tuple[float, float]]) -> BaseGeometry | None:
    if len(coords) < 2:
        return Point(coords[0]) if coords else None
    if coords[0] == coords[-1] and len(coords) >= 4:
        poly = Polygon(coords)
        if poly.is_valid and not poly.is_empty:
            return poly
    return LineString(coords)


def _relation_geometry(members: list[dict]) -> BaseGeometry | None:
    lines = []
    for m in members:
        if m.get("type") == "way":
            c = _coords(m.get("geometry"))
            if len(c) >= 2:
                lines.append(LineString(c))
    if not lines:
        return None
    polys = list(polygonize(unary_union(lines)))
    if polys:
        return polys[0] if len(polys) == 1 else MultiPolygon(polys)
    merged = linemerge(lines)
    return merged


def representative_point(geom: BaseGeometry) -> Point:
    """Point guaranteed on the geometry (never a bare centroid of an area)."""
    if geom.geom_type == "Point":
        return geom
    if geom.geom_type in {"LineString", "MultiLineString"}:
        return geom.interpolate(0.5, normalized=True) if geom.geom_type == "LineString" else geom.representative_point()
    return geom.representative_point()


def parse_overpass(payload: dict) -> list[dict]:
    """Return one record per OSM element, plus separate endpoint records for ferry routes."""
    if not isinstance(payload, dict) or not isinstance(payload.get("elements"), list):
        raise ValueError("Overpass payload has no elements list")
    records: list[dict] = []
    for el in payload["elements"]:
        if not isinstance(el, dict) or el.get("type") not in {"node", "way", "relation"}:
            continue
        etype, eid = el["type"], el.get("id")
        if not isinstance(eid, int):
            continue
        tags = _clean_tags(el.get("tags"))
        geom: BaseGeometry | None
        if etype == "node":
            if not isinstance(el.get("lon"), (int, float)) or not isinstance(el.get("lat"), (int, float)):
                continue
            geom = Point(float(el["lon"]), float(el["lat"]))
        elif etype == "way":
            geom = _way_geometry(_coords(el.get("geometry")))
        else:
            geom = _relation_geometry(el.get("members") or [])
        if geom is None or geom.is_empty:
            continue
        fclass = classify(tags)
        if fclass == "other":  # never trust the upstream query to have filtered
            continue
        if fclass == "ferry_route_endpoint":
            records.extend(_route_endpoints(etype, eid, tags, el))
            continue
        records.append(_record(etype, eid, tags, geom, fclass))
    return records


def _route_endpoints(etype: str, eid: int, tags: dict, el: dict) -> list[dict]:
    lines: list[list[tuple[float, float]]] = []
    if etype == "way":
        lines.append(_coords(el.get("geometry")))
    elif etype == "relation":
        for m in el.get("members") or []:
            if m.get("type") == "way":
                lines.append(_coords(m.get("geometry")))
    lines = [c for c in lines if len(c) >= 2]
    if not lines:
        return []
    if etype == "way":
        ends = [("start", lines[0][0]), ("end", lines[0][-1])]
    else:
        merged = linemerge([LineString(c) for c in lines])
        parts = list(merged.geoms) if merged.geom_type == "MultiLineString" else [merged]
        ends = []
        for i, part in enumerate(parts):
            ends.append((f"part{i}_start", part.coords[0]))
            ends.append((f"part{i}_end", part.coords[-1]))
    return [_record(etype, eid, {**tags, "_endpoint": label}, Point(xy), "ferry_route_endpoint",
                    suffix=label) for label, xy in ends]


def _record(etype: str, eid: int, tags: dict, geom: BaseGeometry, fclass: str, suffix: str = "") -> dict:
    rep = representative_point(geom)
    fid = f"{etype}/{eid}" + (f"#{suffix}" if suffix else "")
    alt = [tags[k] for k in ("name:en", "name:dv", "alt_name", "official_name", "int_name") if tags.get(k)]
    return {
        "source": "osm_overpass",
        "source_feature_id": fid,
        "feature_class": fclass,
        "name": tags.get("name", ""),
        "name_alt": "; ".join(dict.fromkeys(a for a in alt if a != tags.get("name"))),
        "operator": tags.get("operator", ""),
        "tags": tags,
        "geometry": geom,
        "rep_lon": rep.x,
        "rep_lat": rep.y,
        "geometry_geojson": mapping(geom),
    }
