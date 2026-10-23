"""Normalisation helpers: clock times, weekdays, Dhivehi day names, island names."""
from __future__ import annotations

import re
import unicodedata

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

# ---------------------------------------------------------------- times

_HHMM = re.compile(r"(\d{1,2}):(\d{2})")
_AMPM = re.compile(r"(\d{1,2}):(\d{2})\s*([AP])\s*\.?\s*M", re.I)


def parse_clock_24h(raw: str) -> tuple[int, int] | None:
    """'6:15' / '07:00' -> (6, 15). Returns None unless the whole token is a time."""
    s = re.sub(r"\s+", "", raw or "")
    m = re.fullmatch(_HHMM, s)
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 24 or mi > 59:
        return None
    return h, mi


def parse_clock_ampm(raw: str) -> tuple[tuple[int, int] | None, str | None]:
    """Parse a 12-hour clock such as '05:45 AM'.

    Returns ((hour24, minute), problem). ``problem`` is set when the printed
    value is not a clean 12-hour time (e.g. '18:00 PM'); in that case the
    hour is kept as printed when it is already a valid 24-hour value.
    """
    s = re.sub(r"\s+", "", raw or "")
    m = _AMPM.search(s)
    if not m:
        hm = _HHMM.search(s)
        if hm:
            return (int(hm.group(1)), int(hm.group(2))), "missing_am_pm"
        return None, "unparseable"
    h, mi, ap = int(m.group(1)), int(m.group(2)), m.group(3).upper()
    if mi > 59:
        return None, "unparseable"
    if 1 <= h <= 12:
        if ap == "A":
            return (0 if h == 12 else h, mi), None
        return (12 if h == 12 else h + 12, mi), None
    if 13 <= h <= 23 and ap == "P":
        return (h, mi), "24h_value_with_pm_suffix"
    return None, "invalid_12h_hour"


def step_day_offset(prev: int | None, current: int, offset: int,
                    overnight_from: int = 20 * 3600, overnight_to: int = 5 * 3600) -> tuple[int, str | None]:
    """Service-day offset for the next clock time (seconds) in one published sequence.

    Returns (offset, kind). kind is None when time moves forward, 'overnight' when it
    falls from >= overnight_from to <= overnight_to (offset + 1), and 'backwards' for
    any other decrease, which is left unadjusted for review rather than repaired.
    """
    if prev is None or current >= prev:
        return offset, None
    if prev >= overnight_from and current <= overnight_to:
        return offset + 1, "overnight"
    return offset, "backwards"


def hhmmss(h: int, m: int) -> str:
    return f"{h:02d}:{m:02d}:00"


def clock_seconds(h: int, m: int) -> int:
    return h * 3600 + m * 60


# --------------------------------------------------------------- weekdays

_DAY_ALIASES = {
    "monday": "monday", "mon": "monday",
    "tuesday": "tuesday", "tue": "tuesday", "tues": "tuesday",
    "wednesday": "wednesday", "wed": "wednesday",
    "thursday": "thursday", "thu": "thursday", "thur": "thursday", "thurs": "thursday",
    "friday": "friday", "fri": "friday",
    "saturday": "saturday", "sat": "saturday",
    "sunday": "sunday", "sun": "sunday",
}
# Misspellings seen in the sources; recorded as a note when used.
_DAY_TYPOS = {"thurday": "thursday"}


def parse_weekdays_en(raw: str) -> tuple[set[str], list[str]]:
    """Parse an English day heading into a set of weekday names.

    Supports lists ('Saturday, Sunday', 'Saturday and Thursday', 'MON,TUE')
    and ranges ('SATURDAY - THURSDAY' runs Sat..Thu through the week).
    Returns (days, notes).
    """
    notes: list[str] = []
    text = (raw or "").lower()
    words = re.findall(r"[a-z]+|-|–", text)
    seq: list[str] = []
    for w in words:
        if w in ("-", "–"):
            seq.append("-")
        elif w in _DAY_ALIASES:
            seq.append(_DAY_ALIASES[w])
        elif w in _DAY_TYPOS:
            seq.append(_DAY_TYPOS[w])
            notes.append(f"day word '{w}' read as {_DAY_TYPOS[w]}")
    days: set[str] = set()
    i = 0
    while i < len(seq):
        if i + 2 < len(seq) and seq[i] != "-" and seq[i + 1] == "-" and seq[i + 2] != "-":
            a, b = WEEKDAYS.index(seq[i]), WEEKDAYS.index(seq[i + 2])
            k = a
            while True:
                days.add(WEEKDAYS[k])
                if k == b:
                    break
                k = (k + 1) % 7
            i += 3
        else:
            if seq[i] != "-":
                days.add(seq[i])
            i += 1
    return days, notes


# Consonant skeletons of Dhivehi weekday names. The PDFs store Thaana with
# vowel signs displaced, so we only compare consonant order. Used solely to
# raise review issues, never to fill values.
_DV_DAY_SKELETONS = {
    "sunday": "އދއތ",      # އދއތ  (އާދިއްތަ)
    "monday": "ހމ",                  # ހމ    (ހޯމަ)
    "tuesday": "އނގރ",     # އނގރ  (އަންގާރަ)
    "wednesday": "ބދ",               # ބދ    (ބުދަ)
    "thursday": "ބރސފތ",  # ބރސފތ (ބުރާސްފަތި)
    "friday": "ހކރ",            # ހކރ   (ހުކުރު)
    "saturday": "ހނހރ",    # ހނހރ  (ހޮނިހިރު)
}


def dv_skeleton(raw: str) -> str:
    """Thaana consonants (U+0780..U+07A5) plus '-' range markers, in stored order."""
    return "".join(ch for ch in (raw or "") if "ހ" <= ch <= "ޥ" or ch in "-–")


def parse_weekdays_dv(raw: str) -> set[str] | None:
    """Best-effort weekday set from raw Dhivehi; None when nothing recognisable."""
    sk = dv_skeleton(raw)
    if not sk:
        return None
    found: list[tuple[int, str]] = []
    taken = [False] * len(sk)
    for day, pat in sorted(_DV_DAY_SKELETONS.items(), key=lambda kv: -len(kv[1])):
        start = 0
        while True:
            i = sk.find(pat, start)
            if i < 0:
                break
            if not any(taken[i:i + len(pat)]):
                for j in range(i, i + len(pat)):
                    taken[j] = True
                found.append((i, day))
            start = i + 1
    if not found:
        return None
    found.sort()
    days: set[str] = set()
    for k, (pos, day) in enumerate(found):
        days.add(day)
        if k + 1 < len(found):
            between = sk[pos:found[k + 1][0]]
            if "-" in between or "–" in between:
                a, b = WEEKDAYS.index(day), WEEKDAYS.index(found[k + 1][1])
                j = a
                while j != b:
                    days.add(WEEKDAYS[j])
                    j = (j + 1) % 7
    return days


# ------------------------------------------------------------ island names

# Administrative atoll abbreviations used as name prefixes in the sources.
# Used only to recognise printed prefixes, never to assign an atoll.
ATOLL_PREFIXES = {
    "HA", "HDH", "SH", "N", "R", "B", "LH", "K", "AA", "ADH", "V", "M", "F",
    "DH", "TH", "L", "GA", "GDH", "GN", "S",
}


def name_key(name: str) -> str:
    """Spelling-insensitive key: lowercase ASCII letters only (apostrophes dropped)."""
    s = unicodedata.normalize("NFKD", name or "")
    s = s.replace("ﬀ", "ff")
    return re.sub(r"[^a-z]", "", s.lower())


def clean_name(raw: str) -> str:
    s = (raw or "").replace("ﬀ", "ff")
    s = re.sub(r"\s+", " ", s).strip()
    s = s.strip(" .,;:")
    return s


def split_atoll_prefix(raw: str, allow_space: bool = False) -> tuple[str | None, str, str | None]:
    """Split 'Ha. Uligamu' / '.K.Maafushi' / 'Th.Vilufushi' into (prefix, name, unknown_prefix).

    Returns the normalised atoll prefix when it is a recognised abbreviation.
    An unrecognised letter prefix (e.g. 'TM.') is returned as unknown_prefix.
    ``allow_space`` also accepts 'HDH KULHUDHUFFUSHI' (route titles only).
    """
    s = clean_name(raw)
    s = re.sub(r"^[.\s]+", "", s)
    m = re.match(r"^([A-Za-z]{1,3})\s*\.\s*(\S.*)$", s)
    if m:
        pre = m.group(1).upper()
        rest = clean_name(m.group(2))
        if pre in ATOLL_PREFIXES:
            return pre, rest, None
        return None, rest, pre
    if allow_space:
        m = re.match(r"^([A-Z]{1,3})\s+(\S.*)$", s)
        if m and m.group(1) in ATOLL_PREFIXES:
            return m.group(1), clean_name(m.group(2)), None
    return None, s, None


def display_name(raw_name: str) -> str:
    """Title-case an upper-case title fragment, keep mixed-case cells as printed."""
    s = clean_name(raw_name)
    if s.isupper():
        s = s.title().replace("’S", "’s")
    return s
