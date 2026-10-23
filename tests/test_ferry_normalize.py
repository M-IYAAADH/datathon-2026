"""Unit tests for time, weekday, Dhivehi and island-name normalisation."""
from ferry_pipeline.normalize import (
    name_key, parse_clock_24h, parse_clock_ampm, parse_weekdays_dv, parse_weekdays_en, split_atoll_prefix,
    step_day_offset,
)

ALL_BUT_FRIDAY = {"saturday", "sunday", "monday", "tuesday", "wednesday", "thursday"}


def test_24h_clock_accepts_split_tokens_and_rejects_text():
    assert parse_clock_24h("6:15") == (6, 15)
    assert parse_clock_24h("1 5:00") == (15, 0)       # span split inside the PDF
    assert parse_clock_24h("15 MIN") is None
    assert parse_clock_24h("25:00") is None


def test_ampm_conversion():
    assert parse_clock_ampm("05:45AM") == ((5, 45), None)
    assert parse_clock_ampm("04:05 PM") == ((16, 5), None)
    assert parse_clock_ampm("12:30PM") == ((12, 30), None)
    assert parse_clock_ampm("12:10AM") == ((0, 10), None)
    # invisible 'M' fragment in front of the time (RTL text layer)
    assert parse_clock_ampm("M07:39AM") == ((7, 39), None)


def test_ampm_problems_are_reported_not_repaired():
    assert parse_clock_ampm("18:00PM") == ((18, 0), "24h_value_with_pm_suffix")
    assert parse_clock_ampm("07:20")[1] == "missing_am_pm"


def test_weekday_range_and_lists():
    assert parse_weekdays_en("SATURDAY - THURSDAY")[0] == ALL_BUT_FRIDAY
    assert parse_weekdays_en("SUNDAY - THURSDAY")[0] == {"sunday", "monday", "tuesday", "wednesday", "thursday"}
    assert parse_weekdays_en("Saturday and Thursday")[0] == {"saturday", "thursday"}
    assert parse_weekdays_en("OPERATING DAYS: SUN,MON,TUE,WED,THU,FRI,SAT")[0] == ALL_BUT_FRIDAY | {"friday"}
    assert parse_weekdays_en("Sunday, Tuesday & Thursday")[0] == {"sunday", "tuesday", "thursday"}


def test_weekday_typo_is_noted():
    days, notes = parse_weekdays_en("Saturday, Sunday,Tuesday, Thurday")
    assert "thursday" in days and notes


def test_dhivehi_day_skeleton_detects_cargo_conflict():
    # Raw text as extracted from the cargo PDF (vowel signs displaced). English says Thursday.
    raw = "ައާދީއްތަ، އަންގާރަ އަދި ބުދ"
    assert parse_weekdays_dv(raw) == {"sunday", "tuesday", "wednesday"}


def test_dhivehi_range():
    raw = "ުހޮނިހިރ - | ިބުރާސްފަތ"
    assert parse_weekdays_dv(raw) == ALL_BUT_FRIDAY


def test_atoll_prefixes():
    assert split_atoll_prefix("Ha. Uligamu") == ("HA", "Uligamu", None)
    assert split_atoll_prefix(".K.Maafushi") == ("K", "Maafushi", None)
    assert split_atoll_prefix(". L.Gan") == ("L", "Gan", None)
    assert split_atoll_prefix("TM. DHIGGARU") == (None, "DHIGGARU", "TM")
    assert split_atoll_prefix("Male’")[0] is None
    assert split_atoll_prefix("HDH KULHUDHUFFUSHI", allow_space=True)[0] == "HDH"
    assert split_atoll_prefix("HDH KULHUDHUFFUSHI")[0] is None


def test_name_key_ignores_apostrophes_case_and_ligatures():
    assert name_key("Ka’nditheemu") == name_key("KANDITHEEMU") == name_key("Kan'ditheemu")
    assert name_key("Kulhudhuﬀushi") == "kulhudhuffushi"


def test_day_offset_only_for_plausible_midnight_crossing():
    assert step_day_offset(None, 3600, 0) == (0, None)
    assert step_day_offset(23 * 3600 + 15 * 60, 15 * 60, 0) == (1, "overnight")      # 23:15 -> 00:15
    assert step_day_offset(16 * 3600 + 50 * 60, 14 * 3600 + 55 * 60, 0) == (0, "backwards")  # not repaired
    assert step_day_offset(9 * 3600, 9 * 3600, 0) == (0, None)
