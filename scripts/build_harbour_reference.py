"""Build the harbour/jetty reference from archived raw downloads (offline, deterministic).

    .venv/bin/python scripts/build_harbour_reference.py
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

from shapely.geometry import shape
from shapely.ops import unary_union

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from harbour_pipeline import __version__  # noqa: E402
from harbour_pipeline.config import load_config  # noqa: E402
from harbour_pipeline.onemap import discover_harbour_layers  # noqa: E402
from harbour_pipeline.osm_pbf import pbf_to_elements  # noqa: E402
from harbour_pipeline.overpass import parse_overpass, study_bbox  # noqa: E402
from harbour_pipeline.reference import (  # noqa: E402
    FerryStopMatcher, IslandIndex, confidence, group_features, make_ref_id)

CSV_COLUMNS = [
    "harbour_ref_id", "harbour_group_id", "group_size", "source", "source_feature_id", "feature_class", "name",
    "name_alt", "operator", "longitude", "latitude", "geometry_type", "island_id", "island_name", "island_atoll",
    "island_assignment", "island_distance_m", "study_area_code", "inside_study_area", "ferry_stop_match",
    "ferry_stop_id", "ferry_stop_match_method", "confidence", "review_status", "tags_json",
]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_records(cfg: dict, root: Path) -> tuple[list[dict], dict]:
    raw = root / cfg["raw_dir"]
    prov = json.loads((root / "data/reference/followme/geography_provenance.json").read_text(encoding="utf-8"))
    bbox = study_bbox(tuple(prov["geographic_bounds"]), cfg["bbox_margin_degrees"])
    study = unary_union([shape(f["geometry"]) for f in
                         json.loads((root / cfg["study_area_path"]).read_text(encoding="utf-8"))["features"]])
    islands = json.loads((root / cfg["islands_path"]).read_text(encoding="utf-8"))["features"]
    index = IslandIndex(islands, cfg["island_assignment"]["offshore_max_distance_m"])
    with (root / cfg["ferry_stops_path"]).open(encoding="utf-8", newline="") as fh:
        matcher = FerryStopMatcher(list(csv.DictReader(fh)), cfg["ferry_match"]["island_aliases"])

    recs: list[dict] = []
    stats: dict = {}
    pbf = raw / "osm_geofabrik_maldives.osm.pbf"
    ovp = raw / "osm_overpass.json"
    if pbf.exists():
        payload = pbf_to_elements(pbf, bbox)
        parsed = parse_overpass(payload)
        stats["osm_geofabrik"] = {"raw_elements_in_bbox": len(payload["elements"]), "parsed_features": len(parsed)}
        recs += [{**r, "source": "osm_geofabrik"} for r in parsed]
    if ovp.exists():
        payload = json.loads(ovp.read_text(encoding="utf-8"))
        parsed = parse_overpass(payload)
        stats["osm_overpass"] = {"raw_elements": len(payload["elements"]), "parsed_features": len(parsed)}
        recs += [{**r, "source": "osm_overpass"} for r in parsed]

    for r in recs:
        r["harbour_ref_id"] = make_ref_id(r["source"], r["source_feature_id"])
        r.update(index.assign(r["rep_lon"], r["rep_lat"], r["geometry"]))
        from shapely.geometry import Point
        r["inside_study_area"] = bool(study.covers(Point(r["rep_lon"], r["rep_lat"])))
        match, method = matcher.match(r)
        r["ferry_stop_id"] = match.split("|")[0] if match else ""
        r["ferry_stop_match"] = match.split("|")[1] if match else ""
        r["ferry_stop_match_method"] = method
        r["confidence"] = confidence(r)
        r["review_status"] = "unreviewed"
    recs.sort(key=lambda r: r["harbour_ref_id"])
    groups = group_features(recs, cfg["grouping"]["distance_m"], cfg["grouping"]["name_similarity_min"])
    sizes = Counter(groups)
    for r, g in zip(recs, groups):
        r["harbour_group_id"], r["group_size"] = g, sizes[g]
    return recs, stats


def write_outputs(recs: list[dict], cfg: dict, root: Path) -> None:
    out = root / cfg["output_dir"]
    rows = []
    for r in recs:
        row = {c: r.get(c, "") for c in CSV_COLUMNS}
        row["longitude"], row["latitude"] = round(r["rep_lon"], 7), round(r["rep_lat"], 7)
        row["geometry_type"] = r["geometry"].geom_type
        row["tags_json"] = json.dumps(r["tags"], sort_keys=True, ensure_ascii=False)
        rows.append(row)
    with (out / "harbour_reference.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_COLUMNS, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    features = [{"type": "Feature", "id": r["harbour_ref_id"], "geometry": r["geometry_geojson"],
                 "properties": {k: v for k, v in row.items()}} for r, row in zip(recs, rows)]
    fc = {"type": "FeatureCollection", "name": "harbour_reference",
          "attribution": cfg["overpass"]["attribution"], "features": features}
    (out / "harbour_reference.geojson").write_text(json.dumps(fc, sort_keys=True, ensure_ascii=False), encoding="utf-8")
    review_cols = ["harbour_ref_id", "harbour_group_id", "name", "feature_class", "longitude", "latitude",
                   "island_name", "study_area_code", "ferry_stop_match", "confidence"]
    with (out / "harbour_reference_review.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(review_cols + ["review_status", "reviewer_note"])
        for row in rows:
            if row["confidence"] != "low" or row["feature_class"] in {"ferry_terminal", "ferry_platform", "ferry_stop_position"}:
                w.writerow([row[c] for c in review_cols] + ["unreviewed", ""])


def coverage(recs: list[dict], cfg: dict, root: Path) -> dict:
    with (root / cfg["ferry_stops_path"]).open(encoding="utf-8", newline="") as fh:
        stops = list(csv.DictReader(fh))
    cov = list(csv.DictReader((root / "data/reference/followme/study_island_coverage.csv").open(encoding="utf-8")))
    strong = [r for r in recs if r["feature_class"] not in {"ferry_route_endpoint", "breakwater"}]
    inhabited = [c for c in cov if c["category"] == "Residential Island"]

    def has(fcode: str, pool: list[dict]) -> bool:
        return any(r["island_id"] == fcode for r in pool)

    study_stops = [s for s in stops if s["atoll_abbreviation"].upper() in {"AA", "ADH", "K", "V", ""} and
                   not s["stop_id"].startswith("stp_unres")]
    def stop_has(s: dict, pool: list[dict]) -> bool:
        return any(r["ferry_stop_id"] == s["stop_id"] for r in pool)

    return {
        "inhabited_study_islands": len(inhabited),
        "inhabited_with_any_feature": sum(has(c["FCODE"], recs) for c in inhabited),
        "inhabited_with_terminal_grade_feature": sum(has(c["FCODE"], strong) for c in inhabited),
        "inhabited_without_terminal_grade_feature": sorted(
            f"{c['study_area_code']}:{c['islandName']}" for c in inhabited if not has(c["FCODE"], strong)),
        "study_area_ferry_stops": len(study_stops),
        "ferry_stops_with_any_feature": sum(stop_has(s, recs) for s in study_stops),
        "ferry_stops_with_terminal_grade_feature": sum(stop_has(s, strong) for s in study_stops),
        "ferry_stops_without_terminal_grade_feature": sorted(
            f"{s['atoll_abbreviation'] or 'noatoll'}:{s['island_name_en']}" for s in study_stops if not stop_has(s, strong)),
        "terminal_grade_definition": "any class except ferry_route_endpoint and breakwater",
    }


def write_provenance(recs: list[dict], stats: dict, cfg: dict, root: Path) -> dict:
    raw = root / cfg["raw_dir"]
    retrieval = json.loads((raw / "retrieval.json").read_text(encoding="utf-8"))
    directory = json.loads((raw / "onemap_service_directory.json").read_text(encoding="utf-8"))
    meta = {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in sorted((raw / "onemap_service_metadata").glob("*.json"))}
    hits = discover_harbour_layers(directory, meta, cfg["onemap"]["harbour_keywords"])
    by_source = {s: Counter(r["feature_class"] for r in recs if r["source"] == s) for s in {r["source"] for r in recs}}
    sources = [{
        "source": "osm_geofabrik", "url": cfg["geofabrik"]["url"],
        "query_text": "Whole-country Geofabrik extract; objects matching the selectors in harbour_pipeline/overpass.py "
                      "(SELECTORS plus route=ferry) kept if any coordinate lies in the study bbox plus margin.",
        "retrieved_at_utc": retrieval.get("osm_geofabrik", {}).get("retrieved_at_utc"),
        "file": "raw/osm_geofabrik_maldives.osm.pbf", "sha256": sha256(raw / "osm_geofabrik_maldives.osm.pbf"),
        "licence": cfg["geofabrik"]["licence"], "attribution": cfg["geofabrik"]["attribution"],
        "feature_counts": stats.get("osm_geofabrik", {}), "features_by_class": dict(sorted(by_source.get("osm_geofabrik", {}).items())),
        "extract_last_modified": "Fri, 23 Oct 2026 03:11:28 GMT (HTTP Last-Modified)",
    }, {
        "source": "onemap", "url": cfg["onemap"]["services_url"],
        "query_text": "GET <services_url>?f=json and <service>/FeatureServer?f=json for each FeatureServer; keyword match on "
                      f"names/descriptions: {cfg['onemap']['harbour_keywords']}",
        "retrieved_at_utc": retrieval.get("onemap_service_directory", {}).get("retrieved_at_utc"),
        "file": "raw/onemap_service_directory.json", "sha256": sha256(raw / "onemap_service_directory.json"),
        "licence": f"OneMap Data Usage License: {cfg['onemap']['licence_url']}",
        "services_listed": [s["name"] for s in directory["services"] if s["type"] == "FeatureServer"],
        "harbour_layers_found": hits, "feature_counts": {"harbour_features_downloaded": 0},
        "outcome": "No public harbour, jetty, port or terminal layer. Island layer is used only for island assignment.",
    }]
    rejected = [
        {"source": "overpass.private.coffee / overpass.kumi.systems", "reason": cfg["overpass"]["status_note"]},
        {"source": "overpass-api.de", "reason": "robots.txt disallows /api/; not used."},
        {"source": "HDX (data.humdata.org package_search 'maldives harbour/jetty/port')", "reason": "0 matching datasets on 2026-10-24."},
        {"source": "Ferry PDFs / data/processed/ferry/stops.csv", "reason": "Named island-level stops only; no jetty names or coordinates, so used for matching only."},
        {"source": "Census island coordinates", "reason": "Island reference points, not harbours."},
        {"source": "MTCC / Ministry of Transport / Ports Limited lists", "reason": "No machine-readable public terminal list with coordinates was identified; not scraped."},
    ]
    doc = {"pipeline_version": __version__, "config_version": cfg["config_version"], "sources": sources,
           "rejected_or_unused": rejected, "total_reference_features": len(recs),
           "config_sha256": sha256(root / "config" / "harbour_reference.json")}
    (root / cfg["output_dir"] / "provenance.json").write_text(json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8")
    return doc


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=str(ROOT / "config" / "harbour_reference.json"))
    args = ap.parse_args()
    cfg = load_config(Path(args.config))
    recs, stats = build_records(cfg, ROOT)
    write_outputs(recs, cfg, ROOT)
    write_provenance(recs, stats, cfg, ROOT)
    cov = coverage(recs, cfg, ROOT)
    (ROOT / cfg["output_dir"] / "coverage.json").write_text(json.dumps(cov, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"features": len(recs), "by_class": Counter(r["feature_class"] for r in recs),
                      "by_area": Counter(r["study_area_code"] or "none" for r in recs),
                      "inside_study_area": sum(r["inside_study_area"] for r in recs),
                      "confidence": Counter(r["confidence"] for r in recs),
                      "groups": len({r["harbour_group_id"] for r in recs}), "max_group": max(r["group_size"] for r in recs)},
                     indent=1, default=dict))
    print(json.dumps({k: v for k, v in cov.items() if not isinstance(v, list)}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
