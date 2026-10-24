"""OneMap ArcGIS REST helpers: harbour-layer discovery and GeoJSON query parsing (untrusted input)."""
from __future__ import annotations

from shapely.geometry import mapping, shape

from .overpass import representative_point


def discover_harbour_layers(directory: dict, layer_meta: dict[str, dict], keywords: list[str]) -> list[dict]:
    """Find services/layers whose names or descriptions mention a harbour keyword.

    layer_meta maps a service name to its FeatureServer JSON ({"layers": [{"id", "name"}], ...}).
    """
    hits = []
    kws = [k.lower() for k in keywords]
    for svc in directory.get("services", []):
        name = str(svc.get("name", ""))
        meta = layer_meta.get(name, {})
        texts = [name, str(meta.get("serviceDescription", ""))]
        layers = meta.get("layers", []) if isinstance(meta, dict) else []
        texts += [str(layer.get("name", "")) for layer in layers]
        blob = " ".join(texts).lower()
        matched = sorted(k for k in kws if k in blob)
        if matched:
            hits.append({"service": name, "type": svc.get("type"), "url": svc.get("url"), "keywords": matched})
    return hits


def parse_arcgis_geojson(payload: dict, layer_name: str) -> list[dict]:
    """Parse an ArcGIS f=geojson query result into harbour records (same shape as the OSM parser)."""
    if not isinstance(payload, dict) or not isinstance(payload.get("features"), list):
        raise ValueError("ArcGIS payload has no features list")
    records = []
    for f in payload["features"]:
        if not isinstance(f, dict) or not isinstance(f.get("geometry"), dict):
            continue
        props = f.get("properties") if isinstance(f.get("properties"), dict) else {}
        try:
            geom = shape(f["geometry"])
        except Exception:  # untrusted geometry
            continue
        if geom.is_empty:
            continue
        fid = props.get("OBJECTID", f.get("id"))
        if fid is None:
            continue
        tags = {str(k): str(v) for k, v in props.items() if v is not None}
        name = tags.get("name") or tags.get("islandName") or tags.get("NAME") or ""
        rep = representative_point(geom)
        records.append({
            "source": "onemap", "source_feature_id": f"{layer_name}/{fid}", "feature_class": "onemap_" + layer_name.lower(),
            "name": name, "name_alt": "", "operator": "", "tags": tags, "geometry": geom,
            "rep_lon": rep.x, "rep_lat": rep.y, "geometry_geojson": mapping(geom),
        })
    return records
