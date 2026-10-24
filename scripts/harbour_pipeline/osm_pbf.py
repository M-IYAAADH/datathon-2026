"""Read a Geofabrik OSM PBF into Overpass-style JSON elements (same parser path as Overpass)."""
from __future__ import annotations

from pathlib import Path

import osmium

from .overpass import classify


def wanted(tags: dict[str, str]) -> bool:
    return classify(tags) != "other" or tags.get("route") == "ferry"


def pbf_to_elements(path: Path, bbox_swne: tuple[float, float, float, float]) -> dict:
    """Return {"elements": [...]} for harbour-related objects touching the bbox (S, W, N, E)."""
    s, w, n, e = bbox_swne

    def inside(lon: float, lat: float) -> bool:
        return w <= lon <= e and s <= lat <= n

    way_coords: dict[int, list[dict]] = {}
    elements: list[dict] = []
    relations: list[tuple[int, dict, list[tuple[str, int]]]] = []
    wanted_ways: set[int] = set()
    for obj in osmium.FileProcessor(str(path)).with_locations():
        if obj.is_node():
            tags = {t.k: t.v for t in obj.tags}
            if tags and wanted(tags) and obj.location.valid() and inside(obj.location.lon, obj.location.lat):
                elements.append({"type": "node", "id": obj.id, "lat": obj.location.lat,
                                 "lon": obj.location.lon, "tags": tags})
        elif obj.is_way():
            coords = [{"lat": nd.location.lat, "lon": nd.location.lon} for nd in obj.nodes if nd.location.valid()]
            if not coords:
                continue
            way_coords[obj.id] = coords
            tags = {t.k: t.v for t in obj.tags}
            if tags and wanted(tags) and any(inside(c["lon"], c["lat"]) for c in coords):
                elements.append({"type": "way", "id": obj.id, "geometry": coords, "tags": tags})
        elif obj.is_relation():
            tags = {t.k: t.v for t in obj.tags}
            if tags and wanted(tags):
                members = [(m.type, m.ref) for m in obj.members if m.type == "w"]
                relations.append((obj.id, tags, members))
    for rid, tags, members in relations:
        mem = [{"type": "way", "ref": ref, "geometry": way_coords[ref]} for _, ref in members if ref in way_coords]
        if mem and any(inside(c["lon"], c["lat"]) for m in mem for c in m["geometry"]):
            elements.append({"type": "relation", "id": rid, "members": mem, "tags": tags})
    elements.sort(key=lambda x: (x["type"], x["id"]))
    return {"elements": elements}
