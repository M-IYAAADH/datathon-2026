"""Offline weather tests. Synthetic fixtures are never collection inputs."""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pytest

import collect_weather as weather

FIXTURES = Path(__file__).parent / "fixtures/weather"


@pytest.fixture(autouse=True)
def prohibit_network(monkeypatch):
    def fail(*args, **kwargs):
        pytest.fail("Network/subprocess access is forbidden in weather tests")
    monkeypatch.setattr(weather.subprocess, "run", fail)


@pytest.fixture
def cfg():
    config = weather.read_json(weather.ROOT / "config/weather.json")
    config["end_date"] = config["start_date"]
    return config


@pytest.fixture
def payloads():
    return {s: weather.read_json(FIXTURES / name) for s, name in
            (("weather", "archive_small.json"), ("marine", "marine_small.json"))}


def test_parser_units_and_null_preservation(cfg, payloads):
    parsed = weather.parse_payload(payloads["weather"], cfg["sources"]["weather"]["variables"], weather.hours_for(cfg))
    stamp = datetime(2025, 8, 1, 19, tzinfo=timezone.utc)
    assert parsed["by_time"][stamp]["wind_speed_10m"] is None
    assert parsed["by_time"][stamp]["precipitation"] is None
    assert parsed["by_time"][stamp]["wind_gusts_10m"] == 46
    assert parsed["units"]["wind_speed_10m"] == "km/h"
    assert parsed["status"] == "partial_nulls"
    assert parsed["response_hour_count"] == 2
    assert parsed["metadata"]["latitude"] == 4.125


@pytest.mark.parametrize("source,var,bad_unit", [("weather", "wind_speed_10m", "m/s"), ("weather", "precipitation", "inch"), ("marine", "wave_height", "ft")])
def test_reject_incompatible_units(cfg, payloads, source, var, bad_unit):
    payloads[source]["hourly_units"][var] = bad_unit
    with pytest.raises(ValueError, match="Unexpected unit"):
        weather.parse_payload(payloads[source], cfg["sources"][source]["variables"], weather.hours_for(cfg))


def fixture_rows(cfg, payloads):
    points = [dict(point_id="fixture", latitude=4.17, longitude=73.5)]
    parsed = {(source, "fixture"): weather.parse_payload(payloads[source], cfg["sources"][source]["variables"], weather.hours_for(cfg)) for source in cfg["sources"]}
    refs = {(source, "fixture"): "synthetic-fixture.json" for source in cfg["sources"]}
    return weather.hourly_rows(points, cfg, parsed, refs)


def test_utc_maldives_conversion_and_missing_hours(cfg, payloads):
    rows = fixture_rows(cfg, payloads)
    assert len(rows) == 24
    assert rows[18]["time_local"] == datetime(2025, 8, 1, 23)
    assert rows[19]["time_local"] == datetime(2025, 8, 2, 0)
    assert rows[19]["time_utc"] == datetime(2025, 8, 1, 19, tzinfo=timezone.utc)
    assert rows[19]["local_timezone"] == "Indian/Maldives"
    assert rows[0]["wind_speed_10m"] is None
    assert rows[0]["weather_source_flag"] == "hour_missing"
    assert rows[18]["weather_returned_latitude"] == 4.125
    assert rows[18]["marine_returned_latitude"] == 4


def test_missing_variable_all_null_and_failed_source(cfg, payloads):
    del payloads["marine"]["hourly"]["swell_wave_period"]
    parsed = weather.parse_payload(payloads["marine"], cfg["sources"]["marine"]["variables"], weather.hours_for(cfg))
    assert parsed["absent_variables"] == ["swell_wave_period"]
    assert all(v["swell_wave_period"] is None for v in parsed["by_time"].values())
    for var in cfg["sources"]["marine"]["variables"]:
        payloads["marine"]["hourly"][var] = [None, None]
    parsed = weather.parse_payload(payloads["marine"], cfg["sources"]["marine"]["variables"], weather.hours_for(cfg))
    assert parsed["status"] == "all_null"
    rows = weather.hourly_rows([dict(point_id="land")], cfg, {}, {})
    assert rows[0]["marine_source_flag"] == "request_failed"
    assert rows[0]["wave_height"] is None


def test_undefined_units_only_permitted_for_all_null_components(cfg, payloads):
    marine = payloads["marine"]
    marine["hourly_units"]["wind_wave_height"] = "undefined"
    marine["hourly"]["wind_wave_height"] = [None, None]
    parsed = weather.parse_payload(marine, cfg["sources"]["marine"]["variables"], weather.hours_for(cfg))
    assert parsed["units"]["wind_wave_height"] == "undefined"
    assert all(row["wind_wave_height"] is None for row in parsed["by_time"].values())
    marine["hourly"]["wind_wave_height"][0] = 1
    with pytest.raises(ValueError, match="Unexpected unit"):
        weather.parse_payload(marine, cfg["sources"]["marine"]["variables"], weather.hours_for(cfg))


@pytest.mark.parametrize("mutation,match", [
    (lambda p: p.update(utc_offset_seconds=18000), "UTC"),
    (lambda p: p["hourly"]["time"].__setitem__(1, "2025-08-01T18:00"), "duplicate"),
    (lambda p: p["hourly"]["time"].__setitem__(1, "2025-09-01T00:00"), "Unexpected"),
    (lambda p: p["hourly"]["time"].__setitem__(1, "2025-08-01T19:00+05:00"), "Expected UTC"),
    (lambda p: p["hourly"]["wind_speed_10m"].append(10), "Mismatched"),
    (lambda p: p["hourly"]["wind_speed_10m"].__setitem__(0, "__import__('os')"), "finite number"),
    (lambda p: p["hourly"]["wind_speed_10m"].__setitem__(0, True), "finite number"),
    (lambda p: p["hourly"]["wind_speed_10m"].__setitem__(0, float("nan")), "finite number"),
])
def test_untrusted_response_rejected(cfg, payloads, mutation, match):
    mutation(payloads["weather"])
    with pytest.raises(ValueError, match=match):
        weather.parse_payload(payloads["weather"], cfg["sources"]["weather"]["variables"], weather.hours_for(cfg))


def test_batch_location_id_mapping(cfg, payloads):
    a, b = deepcopy(payloads["weather"]), deepcopy(payloads["weather"])
    a.update(location_id=0, latitude=4.1)
    b.update(location_id=1, latitude=4.2)
    parsed = weather.parse_batch([b, a], 2, cfg["sources"]["weather"]["variables"], weather.hours_for(cfg))
    assert [p["latitude"] for p in parsed] == [4.1, 4.2]
    del a["location_id"]  # Actual API omits the default ID zero.
    parsed = weather.parse_batch([a, b], 2, cfg["sources"]["weather"]["variables"], weather.hours_for(cfg))
    assert [p["latitude"] for p in parsed] == [4.1, 4.2]
    b["location_id"] = 0
    with pytest.raises(ValueError, match="location_id"):
        weather.parse_batch([a, b], 2, cfg["sources"]["weather"]["variables"], weather.hours_for(cfg))


def test_deterministic_parquet_daily_thresholds_and_outlier_retention(cfg, payloads, tmp_path):
    payloads["weather"]["hourly"]["surface_pressure"][0] = 1200
    rows = fixture_rows(cfg, payloads)
    results = []
    hashes = []
    for name in ("first", "repeat"):
        out = tmp_path / name
        out.mkdir()
        results.append(weather.materialize(rows, cfg, out))
        hashes.append({p.name: weather.sha256(p) for p in out.iterdir()})
    assert results[0] == results[1]
    assert hashes[0] == hashes[1]
    assert results[0]["variables"]["wind_speed_10m"]["null_count"] == 23
    assert results[0]["duplicate_point_hour_groups"] == 0
    assert results[0]["outliers"][0]["value"] == 1200
    con = duckdb.connect()
    data = con.execute("SELECT date_local,hours_in_window,hours_wind_above_threshold,hours_gust_above_threshold,hours_wave_above_threshold FROM read_csv_auto(?) ORDER BY date_local", [str(tmp_path / "first/daily_summary.csv")]).fetchall()
    assert data[0][1:] == (19, 0, 0, 0)  # strict >, not >=
    assert data[1][1:] == (5, None, 1, 1)  # unknown wind is not zero exposure
    assert con.execute("SELECT surface_pressure FROM read_parquet(?) WHERE surface_pressure=1200", [str(tmp_path / "first/weather_hourly.parquet")]).fetchone() == (1200,)
    con.close()


def test_full_august_hours_and_geography():
    cfg = weather.read_json(weather.ROOT / "config/weather.json")
    hours = weather.hours_for(cfg)
    assert len(hours) == len(set(hours)) == 744
    assert hours[0] == datetime(2025, 8, 1, tzinfo=timezone.utc)
    assert hours[-1] == datetime(2025, 8, 31, 23, tzinfo=timezone.utc)
    points = weather.build_points(cfg)
    assert len(points) == len({p["point_id"] for p in points}) == 45
    assert sum(p["kind"] == "grid" for p in points) == 35
    assert sum(p["kind"] == "route_midpoint" for p in points) == 3
    male = next(p for p in points if p["point_id"] == "route_male")
    assert male["onemap_fcodes"] == "LD0442"
    assert male["land_by_onemap"] is True
    assert male["nearest_island_distance_m"] == 0
    assert points == weather.build_points(cfg)


def test_highest_days_exclude_bbox_points_outside_study_polygons(cfg, payloads, tmp_path):
    inside = fixture_rows(cfg, payloads)
    outside = deepcopy(inside)
    for row in outside:
        row["point_id"] = "outside"
        if row["wind_speed_10m"] is not None:
            row["wind_speed_10m"] = 99
        if row["wave_height"] is not None:
            row["wave_height"] = 9
    points = [dict(point_id="fixture", study_area_codes="MLE"), dict(point_id="outside", study_area_codes="")]
    result = weather.materialize(inside + outside, cfg, tmp_path, points)
    assert result["ranking_scope"]["point_ids"] == ["fixture"]
    assert result["top_days"]["wind"][0]["maximum"] == 30
    assert result["top_days"]["wave"][0]["maximum"] == 2.1
    assert result["top_days_bbox_context"]["wind"][0]["maximum"] == 99
    assert result["top_days_bbox_context"]["wave"][0]["maximum"] == 9


def test_request_parameters_and_offline_missing_cache(cfg, tmp_path):
    point = dict(point_id="test", latitude=4, longitude=73)
    for source in cfg["sources"]:
        params = weather.request_parameters(cfg, source, [point])
        assert params["wind_speed_unit"] == "kmh"
        assert params["timezone"] == "GMT"
        assert params["cell_selection"] == "sea"
        with pytest.raises(FileNotFoundError, match="Offline cache"):
            weather.fetch_batch(cfg, source, [point], tmp_path, offline=True)
