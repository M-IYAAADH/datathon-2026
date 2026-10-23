"""Parser and end-to-end policy tests against the source PDFs in the repository root.

Skipped when the PDFs are not present.
"""
import csv
import sys
from pathlib import Path

import fitz
import pytest

ROOT = Path(__file__).resolve().parents[1]
from ferry_pipeline.config import SOURCE_BY_ID, SOURCES, load_overrides  # noqa: E402
from ferry_pipeline.parsers import PARSERS  # noqa: E402

pytestmark = pytest.mark.skipif(not all((ROOT / s.filename).exists() for s in SOURCES),
                                reason="source PDFs not available")


def parse(source_id):
    s = SOURCE_BY_ID[source_id]
    return PARSERS[s.parser](fitz.open(ROOT / s.filename), source_id)


def block(pages, page, n=1):
    return pages[page - 1].blocks[n - 1]


# ------------------------------------------------------------------ parsers

def test_four_column_orientation_departure_on_the_right():
    b = block(parse("cfn_2026_all"), 1)
    r = b.rows[0].cells
    # Visual row: 6:15 | Uligamu | Thuraakunu | 6:00  ->  Thuraakunu 6:00 departs, Uligamu 6:15 arrives
    assert (r["dep_stop"], r["dep_time"], r["arr_stop"], r["arr_time"]) == ("Thuraakunu", "6:00", "Uligamu", "6:15")
    assert b.days_raw_en == "Monday"


def test_split_time_spans_are_joined():
    rows = block(parse("cfn_2026_all"), 14).rows
    assert rows[3].cells["dep_time"] == "8:20"            # printed as '8:' + '20' spans


def test_day_sub_blocks_are_not_carried_across():
    pages = parse("cfn_2026_all")
    days = [b.days_raw_en for b in pages[27].blocks]       # route 303
    assert days == ["Saturday Tuesday", "Sunday Wednesday", "Monday, Thursday"]


def test_dhivehi_only_sub_block_has_no_inherited_days():
    b = block(parse("cfn_2026_all"), 44, 2)
    assert b.days_raw_en == "" and b.meta.get("dv_only_heading")
    assert b.rows[0].cells["dep_stop"] == "Ka’ndoodhoo" and b.rows[0].cells["arr_stop"] == "Buruni"


def test_merged_spans_resolved_by_character_positions():
    r = block(parse("speed_2026_thaa_laamu"), 1).rows[4].cells
    assert (r["dep_stop"], r["dep_time"], r["arr_stop"], r["arr_time"]) == \
        ("Th.Guraidhoo", "11:00", "Th.Gaadhiffushi", "11:15")


def test_time_printed_below_its_row_is_attached():
    r = block(parse("ctn_2022_k_aa_adh_v"), 5, 2).rows[0]
    assert r.cells["dep_time"] == "11:00" and "dep_time_on_offset_line" in r.problems


def test_hidden_white_text_is_ignored():
    pages = parse("ctn_2024_all_atolls")
    assert "Sunday" in pages[43].hidden_text
    assert len(pages[43].blocks) == 2 and pages[43].blocks[1].days_raw_en == ""


def test_rtl_departure_first_and_stray_fragments_removed():
    b = block(parse("rtl_zone1"), 1)
    r = b.rows[2].cells
    assert (r["dep_stop"], r["arr_stop"]) == ("Ha. Molhadhoo", "Ha. Dhidhdhoo")
    assert b.route_code == "S1C1" and "SUN" in b.days_raw_en


def test_departure_lists_keep_columns_independent():
    b = block(parse("mtcc_thilafushi_2026_v2"), 1)
    last = [r.cells for r in b.rows if r.cells["list_position"] == 14]
    assert {(c["origin"], c["dep_time"]) for c in last} == {("MALÉ", "23:45"), ("THILAFUSHI", "00:15")}
    assert all("arr_time" not in c for c in last)


def test_frequency_rows_include_prayer_break():
    rows = block(parse("mtcc_vilimale_2026_v1"), 2).rows
    assert rows[2].cells == {"start": "12:00", "end": "13:30", "frequency": "Friday Prayer Break"}


# --------------------------------------------------------------- end to end

@pytest.fixture(scope="module")
def out(tmp_path_factory):
    sys.path.insert(0, str(ROOT / "scripts"))
    import extract_ferry_schedules as cli
    d = tmp_path_factory.mktemp("ferry")
    code = cli.main(["--pdf-dir", str(ROOT), "--out-dir", str(d / "processed"),
                     "--interim-dir", str(d / "interim"), "--manual-dir", str(ROOT / "data/manual/ferry")])
    tables = {p.stem: list(csv.DictReader(p.open(encoding="utf-8"))) for p in (d / "processed").glob("*.csv")}
    return code, tables, d


def by(rows, key):
    return {r[key]: r for r in rows}


def test_validation_passes(out):
    assert out[0] == 0


def test_overnight_departure_offset(out):
    legs = by(out[1]["legs"], "leg_id")
    lg = legs["leg_mtcc_thilafushi_2026_v2:p01:b1:r14b"]
    assert (lg["departure_time"], lg["departure_day_offset"], lg["departure_seconds"]) == ("00:15:00", "1", "87300")


def test_departure_only_keeps_unknown_arrival_and_is_not_routable(out):
    _, t, _ = out
    trips = [r for r in t["trips"] if r["schedule_type"] == "departure_only"]
    legs = by(t["legs"], "trip_id")
    elig = by(t["routing_eligibility"], "trip_id")
    assert trips
    for tr in trips:
        lg = legs[tr["trip_id"]]
        assert lg["arrival_time"] == lg["arrival_seconds"] == lg["duration_minutes"] == ""
        assert elig[tr["trip_id"]]["routing_eligible"] == "0"


def test_cargo_excluded_and_language_conflict_left_blank(out):
    _, t, _ = out
    routes = by(t["routes"], "route_id")
    elig = by(t["routing_eligibility"], "trip_id")
    cargo = [r for r in t["trips"] if routes[r["route_id"]]["transport_type"] == "cargo_ferry"]
    assert cargo and all(elig[r["trip_id"]]["passenger_eligible"] == "0" for r in cargo)
    svc = by(t["services"], "service_id")["svc_mtcc_cargo_2026_v4:p01:b3"]
    assert svc["calendar_status"] == "language_conflict"
    assert (svc["sunday"], svc["tuesday"], svc["wednesday"], svc["thursday"]) == ("1", "1", "", "")


def test_frequency_not_expanded(out):
    _, t, _ = out
    fw = [r for r in t["frequency_windows"]]
    assert len(fw) == 26 and all(r["exact_times"] == "0" for r in fw)
    brk = by(fw, "frequency_id")["frq_mtcc_vilimale_2026_v1:p02:b1:r03"]
    assert brk["operation_status"] == "suspended" and brk["headway_seconds"] == ""
    assert not [r for r in t["trips"] if r["route_id"] == fw[0]["route_id"]]
    assert all(r["direction_scope"] == "unspecified" for r in fw)


def test_reversal_splits_trips(out):
    _, t, _ = out
    trips = [r for r in t["trips"] if r["source_block_id"] == "cfn_2026_all:p01:b1"]
    assert [(r["origin_stop_id"], r["destination_stop_id"]) for r in trips] == [
        ("stp_ha_thuraakunu", "stp_hdh_kulhudhuffushi"), ("stp_hdh_kulhudhuffushi", "stp_ha_thuraakunu")]


def test_source_trip_labels_preserved(out):
    _, t, _ = out
    trips = [r for r in t["trips"] if r["source_block_id"] == "cfn_2026_zone6:p01:b1"]
    assert [r["source_trip_label"] for r in trips] == ["Trip 1", "Trip 1", "Trip 2", "Trip 2"]


def test_discontinuous_rows_not_joined_and_stops_unresolved(out):
    _, t, _ = out
    trips = [r for r in t["trips"] if r["source_block_id"] == "ctn_2024_all_atolls:p19:b1"]
    assert len(trips) >= 6
    stops = by(t["stops"], "stop_id")
    assert stops["stp_unres_n_villingili"]["identity_status"] == "atoll_unresolved"


def test_alternating_weeks_blank_and_excluded(out):
    _, t, _ = out
    svc = by(t["services"], "service_id")["svc_cfn_2026_all:p41:b1"]
    assert svc["calendar_status"] == "alternating_weeks_unanchored"
    assert all(svc[d] == "" for d in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"))


def test_versions(out):
    _, t, _ = out
    v = by(t["schedule_versions"], "source_block_id")
    assert v["cfn_2026_all:p01:b1"]["selection_status"] == "preferred_candidate"
    assert v["ctn_2024_all_atolls:p01:b1"]["selection_status"] == "duplicate"
    assert v["ctn_2024_all_atolls:p19:b1"]["selection_status"] == "historical"
    assert v["rtl_zone1:p01:b1"]["selection_status"] == "unresolved"
    assert v["cfn_2026_zone6:p03:b1"]["selection_status"] == "unresolved"
    assert v["cfn_2026_zone6:p04:b1"]["selection_status"] == "unresolved"


def test_same_name_different_atoll_not_merged(out):
    stops = by(out[1]["stops"], "stop_id")
    assert "stp_r_maduvvari" in stops and "stp_m_maduvvari" in stops
    assert "stp_sh_feydhoo" in stops and "stp_s_feydhoo" in stops


def test_no_invented_geography(out):
    for s in out[1]["stops"]:
        assert s["latitude"] == s["longitude"] == s["official_island_code"] == s["jetty_name"] == ""
        assert s["island_name_dv"] == ""


def test_alif_male_subset(out):
    import build_alif_male_subset as sub
    direct, transfers = sub.build(out[2] / "processed")
    routable = [r for r in direct if r["routing_eligible"] == "1"]
    assert {r["route_id"] for r in routable} == {"cfn:303", "cfn:305"}
    assert all(r["board_stop_id"].startswith(("stp_aa_", "stp_adh_")) for r in direct if r["direction"] == "to_male")
    for r in transfers:
        assert r["wait_minutes"] >= 0 and any(r[d] == "1" for d in sub.WEEKDAYS)
        assert r["leg1_trip_id"] != r["leg2_trip_id"]


def test_source_rows_with_errors_are_excluded(out):
    _, t, _ = out
    elig = by(t["routing_eligibility"], "trip_id")
    legs = by(t["legs"], "leg_id")
    for leg_id in ("leg_cfn_2026_all:p32:b1:r03", "leg_speed_2026_meemu:p02:b1:r17",
                   "leg_ctn_2022_k_aa_adh_v:p09:b1:r06"):
        assert elig[legs[leg_id]["trip_id"]]["routing_eligible"] == "0"
