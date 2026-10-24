import csv
import json
from pathlib import Path

import pytest
from shapely.geometry import Point, Polygon, mapping

from harbour_pipeline.onemap import discover_harbour_layers, parse_arcgis_geojson
from harbour_pipeline.overpass import build_overpass_query, classify, parse_overpass, study_bbox
from harbour_pipeline.reference import (FerryStopMatcher, IslandIndex, confidence, group_features, make_ref_id,
                                        norm_name)

FIX = Path(__file__).parent / "fixtures"
ROOT = Path(__file__).resolve().parents[1]


def load(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def test_overpass_parser_classes_and_untrusted_input():
    recs = {r["source_feature_id"]: r for r in parse_overpass(load("overpass_sample.json"))}
    assert recs["node/1"]["feature_class"] == "ferry_terminal"
    assert recs["node/1"]["name_alt"] == "Fixture Terminal EN; dv-name"
    assert recs["node/1"]["operator"] == "MTCC"
    assert recs["node/2"]["feature_class"] == "ferry_platform"
    assert "node/3" not in recs  # platform without ferry=yes does not match the query
    assert "node/4" not in recs  # no coordinates
    assert "node/5" not in recs  # unrelated tag
    assert recs["way/10"]["geometry"].geom_type == "LineString"
    assert recs["way/11"]["geometry"].geom_type == "Polygon" and recs["way/11"]["feature_class"] == "port_area"
    assert recs["relation/21"]["feature_class"] == "marina"
    for r in recs.values():  # representative point is on the original geometry, tags retained
        assert r["geometry"].distance(Point(r["rep_lon"], r["rep_lat"])) < 1e-9
        assert isinstance(r["tags"], dict)


def test_route_ways_yield_endpoints_not_terminals():
    recs = parse_overpass(load("overpass_sample.json"))
    ends = [r for r in recs if r["feature_class"] == "ferry_route_endpoint"]
    ids = {r["source_feature_id"] for r in ends}
    assert {"way/12#start", "way/12#end"} <= ids
    assert any(i.startswith("relation/20#") for i in ids)
    assert not any(r["feature_class"] == "ferry_terminal" and r["source_feature_id"].startswith("way/12") for r in recs)


def test_parser_rejects_malformed_payload():
    with pytest.raises(ValueError):
        parse_overpass({"elements": "nope"})


def test_overpass_query_is_one_bounded_query():
    cfg = json.loads((ROOT / "config" / "harbour_reference.json").read_text())
    q = build_overpass_query(cfg)
    assert q.count("out geom") == 1 and "ferry_terminal" in q and '"route"="ferry"' in q
    s, w, n, e = study_bbox((72.0, 3.0, 73.0, 5.0), 0.05)
    assert (s, w, n, e) == (2.95, 71.95, 5.05, 73.05)


def test_classify_priority():
    assert classify({"amenity": "ferry_terminal", "man_made": "pier"}) == "ferry_terminal"
    assert classify({"harbour": "yes"}) == "harbour"
    assert classify({"seamark:type": "mooring"}) == "seamark_mooring"
    assert classify({"highway": "bus_stop"}) == "other"


def test_onemap_parsers():
    recs = parse_arcgis_geojson(load("arcgis_sample.geojson"), "Jetties")
    assert [r["source_feature_id"] for r in recs] == ["Jetties/7", "Jetties/8"]
    assert recs[0]["name"] == "Fixture Jetty" and "extra" not in recs[0]["tags"]
    hits = discover_harbour_layers(load("onemap_directory.json"), {"bathymetry": {"layers": [{"name": "Depth"}]}},
                                   ["harbour", "port", "jetty"])
    assert [h["service"] for h in hits] == ["ports_layer"]
    with pytest.raises(ValueError):
        parse_arcgis_geojson({"features": 3}, "x")


def test_real_onemap_directory_has_no_harbour_layer():
    raw = ROOT / "data/reference/harbours/raw"
    if not (raw / "onemap_service_directory.json").exists():
        pytest.skip("raw OneMap archive not present")
    cfg = json.loads((ROOT / "config" / "harbour_reference.json").read_text())
    meta = {p.stem: json.loads(p.read_text()) for p in (raw / "onemap_service_metadata").glob("*.json")}
    directory = json.loads((raw / "onemap_service_directory.json").read_text())
    assert discover_harbour_layers(directory, meta, cfg["onemap"]["harbour_keywords"]) == []


def test_ref_ids_stable_and_order_independent():
    assert make_ref_id("osm_geofabrik", "node/123") == "hr-osm-geofabrik-node-123"
    assert make_ref_id("osm_geofabrik", "way/9#start") == "hr-osm-geofabrik-way-9-start"
    payload = load("overpass_sample.json")
    a = {r["source_feature_id"] for r in parse_overpass(payload)}
    payload["elements"] = payload["elements"][::-1]
    b = {r["source_feature_id"] for r in parse_overpass(payload)}
    assert a == b
    assert make_ref_id("osm_geofabrik", "node/1") != make_ref_id("osm_overpass", "node/1")


def _rec(source, sid, name, lon, lat, cls="pier"):
    return {"source": source, "harbour_ref_id": make_ref_id(source, sid), "name": name, "rep_lon": lon, "rep_lat": lat,
            "feature_class": cls}


def test_dedup_groups_across_sources_and_keeps_every_row():
    recs = [_rec("osm_geofabrik", "node/1", "Ukulhas Jetty", 72.8648, 4.2164),
            _rec("onemap", "jetties/5", "Ukulhas jetty", 72.86482, 4.21641),
            _rec("osm_geofabrik", "node/2", "Ukulhas Jetty", 72.9, 4.3),  # same name, far away
            _rec("osm_geofabrik", "node/3", "", 72.8000, 4.2000), _rec("osm_geofabrik", "node/4", "", 72.80002, 4.20001)]
    gids = group_features(recs, 150, 0.8)
    assert len(gids) == len(recs)  # nothing discarded
    assert gids[0] == gids[1] != gids[2]
    assert gids[3] != gids[4]  # same-source unnamed neighbours do not chain
    cross = recs + [_rec("onemap", "jetties/6", "", 72.80001, 4.20001)]
    g2 = group_features(cross, 150, 0.8)
    assert len(g2) == 6 and g2[5] in (g2[3], g2[4])  # a cross-source unnamed neighbour does link
    assert group_features(list(recs), 150, 0.8) == gids  # deterministic


def _islands():
    # C-shaped island A (centroid lies in the water inside the C), island B small and close to that centroid
    a = Polygon([(73.000, 4.000), (73.020, 4.000), (73.020, 4.0020), (73.0020, 4.0020), (73.0020, 4.0180),
                 (73.020, 4.0180), (73.020, 4.020), (73.000, 4.020)])
    b = Polygon([(73.010, 4.0095), (73.0105, 4.0095), (73.0105, 4.0105), (73.010, 4.0105)])
    feats = [{"type": "Feature", "properties": {"FCODE": "A", "islandName": "Alpha", "atoll": "Kaafu"}, "geometry": mapping(a)},
             {"type": "Feature", "properties": {"FCODE": "B", "islandName": "Beta", "atoll": "Vaavu"}, "geometry": mapping(b)}]
    return feats, a, b


def test_island_assignment_by_polygon_not_centroid():
    feats, a, b = _islands()
    idx = IslandIndex(feats, 500)
    assert a.centroid.distance(b.centroid) < a.centroid.distance(Point(73.001, 4.001))  # centroid trap is real
    inside_a = Point(73.001, 4.001)  # in A's arm, far from A's centroid, B's centroid is nearer
    r = idx.assign(inside_a.x, inside_a.y, inside_a)
    assert (r["island_id"], r["island_assignment"], r["island_distance_m"]) == ("A", "within_polygon", 0.0)
    assert r["study_area_code"] == "K"


def test_island_assignment_offshore_shoreline_and_none():
    feats, _, _ = _islands()
    idx = IslandIndex(feats, 500)
    near = Point(73.0225, 4.001)  # about 275 m east of A's south-arm tip, B is farther
    r = idx.assign(near.x, near.y, near)
    assert r["island_id"] == "A" and r["island_assignment"] == "nearest_shoreline"
    assert 200 < r["island_distance_m"] < 350
    far = Point(73.2, 4.5)
    assert idx.assign(far.x, far.y, far)["island_assignment"] == "none"


def test_ferry_stop_matching_rules():
    stops = [{"stop_id": "s_aa_x", "island_name_en": "Ukulhas", "atoll_abbreviation": "AA"},
             {"stop_id": "s_b_x", "island_name_en": "Ukulhas", "atoll_abbreviation": "B"},
             {"stop_id": "s_n_male", "island_name_en": "Malé", "atoll_abbreviation": ""},
             {"stop_id": "s_n_vil", "island_name_en": "Villimalé", "atoll_abbreviation": ""}]
    m = FerryStopMatcher(stops, {"villimale": ["vilingili"]})
    assert m.match({"study_area_code": "AA", "island_name": "Ukulhas", "name": "", "feature_class": "pier"})[0].startswith("s_aa_x")
    assert m.match({"study_area_code": "K", "island_name": "Ukulhas", "name": "", "feature_class": "pier"}) == ("", "")
    assert m.match({"study_area_code": "Greater_Male", "island_name": "Malé", "name": "", "feature_class": "pier"})[1] == "island_name"
    assert m.match({"study_area_code": "Greater_Male", "island_name": "Vilin'gili", "name": "", "feature_class": "pier"})[1] == "island_alias"
    # a route name mentioning Malé on another island is not evidence for the Malé stop
    assert m.match({"study_area_code": "K", "island_name": "Hulhule", "name": "Ferry to Male", "feature_class": "ferry_stop_position"}) == ("", "")
    tok = m.match({"study_area_code": "AA", "island_name": "", "name": "Ukulhas ferry terminal", "feature_class": "ferry_terminal"})
    assert tok == ("s_aa_x|Ukulhas", "feature_name_token")
    assert m.match({"study_area_code": "Greater_Male", "island_name": "Malé", "name": "Malé-X", "feature_class": "ferry_route_endpoint"})[1] == "island_name"


def test_confidence_rules():
    base = {"island_assignment": "within_polygon", "island_distance_m": 0.0}
    assert confidence({**base, "feature_class": "ferry_terminal", "name": "T"}) == "high"
    assert confidence({**base, "feature_class": "ferry_terminal", "name": ""}) == "medium"
    assert confidence({**base, "feature_class": "ferry_route_endpoint", "name": "R"}) == "low"
    assert confidence({**base, "feature_class": "breakwater", "name": ""}) == "low"
    assert confidence({"island_assignment": "none", "island_distance_m": "", "feature_class": "ferry_terminal", "name": "T"}) == "low"
    assert confidence({"island_assignment": "nearest_shoreline", "island_distance_m": 300.0, "feature_class": "ferry_terminal", "name": "T"}) == "medium"


def test_norm_name():
    assert norm_name("Dha’ngethi") == norm_name("Dhan'gethi") == "dhangethi"
    assert norm_name("Malé") == "male"


def test_committed_reference_outputs_have_no_device_columns_and_keep_rows():
    csv_path = ROOT / "data/reference/harbours/harbour_reference.csv"
    gj_path = ROOT / "data/reference/harbours/harbour_reference.geojson"
    if not csv_path.exists():
        pytest.skip("reference not built")
    rows = list(csv.DictReader(csv_path.open(encoding="utf-8")))
    assert not any("device" in c.lower() for c in rows[0])
    assert len({r["harbour_ref_id"] for r in rows}) == len(rows)
    assert all(r["review_status"] == "unreviewed" for r in rows)
    assert len(json.loads(gj_path.read_text(encoding="utf-8"))["features"]) == len(rows)
