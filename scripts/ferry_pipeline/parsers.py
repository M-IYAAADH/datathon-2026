"""Layout-specific parsers producing RawBlock/RawRow evidence records.

Layouts
-------
four_column     CTN/CFN/speed tables:  arrival time | arrival island | departure island | departure time
                Each row is departure island @ departure time -> arrival island @ arrival time.
rtl             RTL boxes:             island | departure | island | arrival  (12-hour clock)
departure_list  MTCC Male-area tables: independent 'FROM X' departure columns
frequency       MTCC Vilimale table:   start | end | frequency (headway windows)

Every parser classifies every visible text line; lines that are not used are
returned as ``unused_lines`` so the build step can report them.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import fitz

from .normalize import parse_clock_24h, parse_weekdays_en
from .pdftext import Line, page_lines


@dataclass
class RawRow:
    row_id: str
    page: int
    y: float
    cells: dict
    dv: str = ""
    trip_label: str = ""
    line_text: str = ""
    problems: list = field(default_factory=list)


@dataclass
class RawBlock:
    block_id: str
    source_id: str
    page: int
    layout: str
    route_code: str = ""
    route_title: str = ""
    days_raw_en: str = ""
    days_raw_dv: str = ""
    rows: list = field(default_factory=list)
    notes_en: list = field(default_factory=list)
    notes_dv: list = field(default_factory=list)
    meta: dict = field(default_factory=dict)
    y0: float = 0.0


@dataclass
class PageResult:
    source_id: str
    page: int
    page_title: str = ""
    blocks: list = field(default_factory=list)
    unused_lines: list = field(default_factory=list)   # (y, kind, latin text, raw dv)
    hidden_text: str = ""
    page_notes_en: list = field(default_factory=list)
    page_notes_dv: list = field(default_factory=list)
    footer: list = field(default_factory=list)


_TITLE_RE = re.compile(r"^([A-Z]?\d{3})\s+(\S.*[-–].*)$")
_PUNCT_ONLY = re.compile(r"^[\W_]+$")


def _hidden_text(hidden) -> str:
    from .pdftext import chars_to_text
    rows: dict[int, list] = {}
    for c in hidden:
        rows.setdefault(round(c.yc / 6), []).append(c)
    return " / ".join(chars_to_text(sorted(cs, key=lambda c: c.x0)) for _, cs in sorted(rows.items()))


def _latin_tokens(line: Line, **kw):
    return [t for t in line.tokens(**kw) if not t.thaana and not _PUNCT_ONLY.match(t.compact)]


def _is_dv_only(line: Line) -> bool:
    t = line.text
    return line.has_thaana and (not t or re.fullmatch(r"[\d\s.,:;()\-]*", t) is not None)


def _block_id(source_id: str, page: int, n: int) -> str:
    return f"{source_id}:p{page:02d}:b{n}"


# ----------------------------------------------------------- four column

def _classify_four_column(line: Line) -> str:
    t = line.text
    toks = _latin_tokens(line)
    times = [k for k in toks if parse_clock_24h(k.compact)]
    names = [k for k in toks if re.search(r"[A-Za-z]{2,}", k.text) and not parse_clock_24h(k.compact)]
    if re.search(r"ferry schedule", t, re.I) and not times:
        return "page_title"
    if _TITLE_RE.match(t):
        return "route_title"
    if re.search(r"effective|more information|7998821", t, re.I):
        return "footer"
    if _is_dv_only(line) and re.search(r"20\d\d", t):
        return "footer"                                   # Dhivehi effective-date line
    if re.fullmatch(r"Trip\s*\d+", t):
        return "trip_label"
    if re.search(r"\barrival\b", t, re.I) and re.search(r"\bdeparture\b", t, re.I):
        return "header"
    if times and names:
        return "data"
    if times and not names and all(parse_clock_24h(k.compact) for k in toks):
        return "orphan_time"
    if not times and parse_weekdays_en(t)[0]:
        return "days"
    if _is_dv_only(line):
        return "dv_only"
    return "other"


def parse_four_column(doc: fitz.Document, source_id: str) -> list[PageResult]:
    results = []
    for pno, page in enumerate(doc, start=1):
        lines, hidden = page_lines(page)
        pr = PageResult(source_id, pno, hidden_text=_hidden_text(hidden))
        kinds = [_classify_four_column(ln) for ln in lines]
        mid_x = page.rect.width / 2

        # Thaana-only lines hugging a data row are that row's Dhivehi cells.
        for i, k in enumerate(kinds):
            if k == "dv_only":
                near = [j for j in range(len(lines)) if kinds[j] == "data" and abs(lines[j].y - lines[i].y) <= 7]
                if near:
                    j = near[0]
                    lines[j].dv_spans.extend(lines[i].dv_spans)
                    kinds[i] = "merged_dv"

        def next_struct(i):
            for j in range(i + 1, len(lines)):
                if kinds[j] not in ("dv_only", "merged_dv"):
                    return j
            return None

        def prev_struct(i):
            for j in range(i - 1, -1, -1):
                if kinds[j] not in ("dv_only", "merged_dv"):
                    return j
            return None

        route_code = route_title = ""
        block: RawBlock | None = None
        nblock = 0
        trip_label = ""
        header_split = None
        last_struct = None
        row_n = 0

        def new_block(y):
            nonlocal block, nblock, trip_label, row_n
            nblock += 1
            block = RawBlock(_block_id(source_id, pno, nblock), source_id, pno, "timed_legs",
                             route_code=route_code, route_title=route_title, y0=y)
            if header_split is not None:
                block.meta["name_split_x"] = header_split
            pr.blocks.append(block)
            trip_label = ""
            row_n = 0
            return block

        for i, (ln, kind) in enumerate(zip(lines, kinds)):
            t = ln.text
            if kind == "merged_dv":
                continue
            if kind == "page_title":
                pr.page_title = pr.page_title or t
            elif kind == "route_title":
                m = _TITLE_RE.match(t)
                route_code, route_title = m.group(1), t
                block = None
            elif kind == "footer":
                pr.footer.append(t + ((" || dv: " + ln.raw_dv) if ln.raw_dv else ""))
            elif kind == "days":
                if block is not None and not block.rows and last_struct == "days":
                    block.days_raw_en += " / " + t          # second heading line (alternation)
                    if ln.raw_dv:
                        block.days_raw_dv += (" / " if block.days_raw_dv else "") + ln.raw_dv
                else:
                    new_block(ln.y)
                    block.days_raw_en = t
                    block.days_raw_dv = ln.raw_dv
            elif kind == "header":
                toks = _latin_tokens(ln)
                arr = [k for k in toks if re.fullmatch(r"arrival", k.text, re.I)]
                dep = [k for k in toks if re.fullmatch(r"departure", k.text, re.I)]
                if arr and dep:
                    header_split = (arr[0].x0 + dep[0].x0) / 2
                if block is None or block.rows:
                    new_block(ln.y)
                    block.meta["no_day_heading"] = True
                block.meta["name_split_x"] = header_split
            elif kind == "trip_label":
                trip_label = t
            elif kind == "dv_only":
                nj, pj = next_struct(i), prev_struct(i)
                dv_x = [x for x, _ in ln.dv_spans]
                near_header = any(kinds[j] == "header" and abs(lines[j].y - ln.y) <= 25 for j in range(len(lines)))
                if near_header and len(dv_x) >= 3 and max(dv_x) - min(dv_x) > 250:
                    pass                                              # column header translation
                elif nj is not None and kinds[nj] == "days" and lines[nj].y - ln.y <= 25:
                    lines[nj].dv_spans.extend(ln.dv_spans)          # Dhivehi heading above English
                elif pj is not None and kinds[pj] == "days" and ln.y - lines[pj].y <= 25 and block is not None and not block.rows:
                    block.days_raw_dv += (" | " if block.days_raw_dv else "") + ln.raw_dv
                elif (nj is not None and kinds[nj] == "header" and lines[nj].y - ln.y <= 25) or \
                        (pj is not None and kinds[pj] == "header" and ln.y - lines[pj].y <= 25):
                    pass                                              # column header translation
                elif nj is not None and kinds[nj] in ("data", "orphan_time") and pj is not None and kinds[pj] == "data":
                    new_block(ln.y)                                   # Dhivehi-only sub-heading
                    block.meta["dv_only_heading"] = True
                    block.notes_dv.append(ln.raw_dv + (f" [digits: {t}]" if t else ""))
                else:
                    target = block.notes_dv if block is not None else pr.page_notes_dv
                    target.append(ln.raw_dv + (f" [digits: {t}]" if t else ""))
                continue
            elif kind == "data":
                if block is None:
                    new_block(ln.y)
                    block.meta["no_day_heading"] = True
                split = block.meta.get("name_split_x") or header_split or 270.0
                toks = _latin_tokens(ln)
                cells = {"arr_time": "", "arr_stop": "", "dep_stop": "", "dep_time": ""}
                problems = []
                arr_names, dep_names = [], []
                for k in toks:
                    if parse_clock_24h(k.compact):
                        key = "arr_time" if k.xc < mid_x else "dep_time"
                        if cells[key]:
                            problems.append(f"multiple_{key}")
                        cells[key] = k.compact
                    elif re.search(r"[A-Za-z]", k.text):
                        (arr_names if k.x0 < split else dep_names).append(k.text)
                    else:
                        problems.append(f"unclassified_token:{k.text}")
                cells["arr_stop"] = " ".join(arr_names)
                cells["dep_stop"] = " ".join(dep_names)
                row_n += 1
                block.rows.append(RawRow(f"{block.block_id}:r{row_n:02d}", pno, ln.y, cells, ln.raw_dv,
                                         trip_label, t, problems))
            elif kind == "orphan_time":
                toks = _latin_tokens(ln)
                attached = False
                if block is not None:
                    cand = [r for r in block.rows if abs(r.y - ln.y) <= 12]
                    for k in toks:
                        key = "arr_time" if k.xc < mid_x else "dep_time"
                        for r in sorted(cand, key=lambda r: abs(r.y - ln.y)):
                            if not r.cells[key]:
                                r.cells[key] = k.compact
                                r.problems.append(f"{key}_on_offset_line")
                                attached = True
                                break
                if not attached:
                    pr.unused_lines.append((round(ln.y, 1), kind, t, ln.raw_dv))
            else:
                pr.unused_lines.append((round(ln.y, 1), kind, t, ln.raw_dv))
            last_struct = kind
        results.append(pr)
    return results


# ------------------------------------------------------------------- RTL

def _rtl_pieces(line: Line):
    """Runs of letters or of digits/colons, split at gaps > 3pt or class changes."""
    def cls(ch):
        return "d" if ch.isdigit() or ch == ":" else "a"
    pieces = []
    for c in sorted(line.latin_chars, key=lambda c: c.x0):
        if pieces and cls(pieces[-1][-1].c) == cls(c.c) and c.x0 - pieces[-1][-1].x1 <= 3:
            pieces[-1].append(c)
        else:
            pieces.append([c])
    return pieces


def parse_rtl(doc: fitz.Document, source_id: str) -> list[PageResult]:
    results = []
    for pno, page in enumerate(doc, start=1):
        lines, hidden = page_lines(page)
        pr = PageResult(source_id, pno, hidden_text=_hidden_text(hidden))
        block: RawBlock | None = None
        nblock = 0
        bounds = None
        prev_text_line = None
        row_n = 0
        for ln in lines:
            t = ln.text
            m = re.search(r"\(Route\s*([A-Z0-9 ]+)\)?", t)
            if t.upper().startswith("FERRY SCHEDULES"):
                pr.page_title = pr.page_title or t
            elif m:
                nblock += 1
                code = m.group(1).replace(" ", "")
                title = prev_text_line.text if prev_text_line is not None else ""
                if prev_text_line is not None and pr.unused_lines and pr.unused_lines[-1][2] == title:
                    pr.unused_lines.pop()
                block = RawBlock(_block_id(source_id, pno, nblock), source_id, pno, "timed_legs",
                                 route_code=code, route_title=title, y0=ln.y)
                block.meta["route_code_raw"] = t
                pr.blocks.append(block)
                row_n = 0
            elif "ISLAND" in t and "DEPARTURE" in t:
                toks = _latin_tokens(ln)
                words = [(k.text, k.x0) for k in toks]
                isl = [x for w, x in words if w == "ISLAND"]
                dep = [x for w, x in words if w == "DEPARTURE"]
                arr = [x for w, x in words if w == "ARRIVAL"]
                if len(isl) == 2 and dep and arr:
                    bounds = (dep[0] - 6, isl[1] - 8, arr[0] - 14)
                if block is not None:
                    block.meta["column_bounds"] = bounds
            elif t.upper().startswith("OPERATING DAYS"):
                if block is not None:
                    block.days_raw_en = t
            elif t.upper().startswith("EFFECTIVE FROM"):
                if block is not None:
                    block.meta["effective_raw"] = t
            elif re.search(r"\d\s*:\s*\d", t) and block is not None and bounds is not None:
                b1, b2, b3 = bounds
                parts = {"dep_stop": [], "dep_time": [], "arr_stop": [], "arr_time": []}
                for piece in _rtl_pieces(ln):
                    x0 = piece[0].x0
                    key = ("dep_stop" if x0 < b1 else "dep_time" if x0 < b2
                           else "arr_stop" if x0 < b3 else "arr_time")
                    parts[key].extend(piece)
                from .pdftext import chars_to_text
                cells = {}
                problems = []
                for key in ("dep_stop", "arr_stop"):
                    txt = chars_to_text(sorted(parts[key], key=lambda c: c.x0))
                    # Invisible 'AM'/'A'/'M' fragments sit beside some island names.
                    stripped = re.sub(r"(?<=[a-z\s])(?:\s*(?:AM|PM|A|M|P))+$", "", txt).strip()
                    if stripped != txt:
                        problems.append(f"stray_fragment_removed:{key}:{txt[len(stripped):].strip()}")
                    cells[key] = stripped
                for key in ("dep_time", "arr_time"):
                    cells[key] = "".join(c.c for c in parts[key])
                row_n += 1
                block.rows.append(RawRow(f"{block.block_id}:r{row_n:02d}", pno, ln.y, cells, ln.raw_dv,
                                         "", t, problems))
            else:
                pr.unused_lines.append((round(ln.y, 1), "other", t, ln.raw_dv))
            prev_text_line = ln
        results.append(pr)
    return results


# --------------------------------------------- MTCC Male-area: shared bits

def _mtcc_classify(line: Line) -> str:
    t = line.text
    toks = _latin_tokens(line)
    times = [k for k in toks if parse_clock_24h(k.compact)]
    if t == "Ferry Schedule":
        return "page_title"
    if re.search(r"Ferry$", t) and not times:
        return "route_title"
    if re.search(r"effective", t, re.I):
        return "effective"
    if re.match(r"No scheduled", t, re.I):
        return "note"
    if re.search(r"\bFROM\b", t) or re.search(r"START", t):
        return "header"
    if times or re.search(r"\b\d+\s*(MIN|HR)\b", t) or re.search(r"Prayer Break", t, re.I):
        return "data"
    if parse_weekdays_en(t)[0]:
        return "days"
    if _is_dv_only(line):
        return "dv_only"
    return "other"


def _mtcc_effective(lines: list[Line], kinds: list[str]) -> str:
    """Raw text of the lines around an 'Effective' label (superscripts split them)."""
    idx = [i for i, k in enumerate(kinds) if k == "effective"]
    if not idx:
        return ""
    y = lines[idx[0]].y
    near = [ln for ln in lines if abs(ln.y - y) <= 22 and ln.text]
    return " / ".join(ln.text for ln in near)


def _mtcc_pages(doc, source_id, layout):
    results = []
    for pno, page in enumerate(doc, start=1):
        lines, hidden = page_lines(page)
        pr = PageResult(source_id, pno, hidden_text=_hidden_text(hidden))
        kinds = [_mtcc_classify(ln) for ln in lines]
        eff = _mtcc_effective(lines, kinds)
        route_title = ""
        block: RawBlock | None = None
        nblock = 0
        columns: list[tuple[float, str]] = []
        line_idx = 0
        for i, (ln, kind) in enumerate(zip(lines, kinds)):
            t = ln.text
            if kind == "page_title":
                pr.page_title = pr.page_title or t
            elif kind == "route_title":
                route_title = t
                block = None
            elif kind == "days":
                nblock += 1
                block = RawBlock(_block_id(source_id, pno, nblock), source_id, pno, layout,
                                 route_title=route_title, days_raw_en=t, y0=ln.y)
                if eff:
                    block.meta["effective_raw_page"] = eff
                pr.blocks.append(block)
                columns = []
                line_idx = 0
            elif kind == "dv_only":
                if block is not None and not block.rows and not columns and not block.days_raw_dv:
                    block.days_raw_dv = ln.raw_dv
                elif block is not None and not block.rows and columns:
                    pass                                    # header translation row
                elif block is not None and not block.rows and block.days_raw_dv:
                    block.days_raw_dv += " | " + ln.raw_dv  # heading split over lines
                else:
                    pr.page_notes_dv.append(ln.raw_dv + (f" [digits: {t}]" if t else ""))
            elif kind == "header" and block is not None:
                toks = _latin_tokens(ln, join_gap=6)
                if layout == "departure_list":
                    columns = [(k.xc, re.sub(r"^FROM\s+", "", k.text)) for k in toks if k.text.startswith("FROM")]
                else:
                    labels = []
                    for k in toks:
                        u = k.text.upper()
                        if u.startswith("START"):
                            labels.append((k.xc, "start"))
                        elif u.startswith("END"):
                            labels.append((k.xc, "end"))
                        elif u.startswith("FREQ"):
                            labels.append((k.xc, "frequency"))
                    columns = labels
                block.meta["columns"] = [c[1] for c in columns]
            elif kind == "data" and block is not None and columns:
                line_idx += 1
                toks = _latin_tokens(ln, join_gap=6)
                if layout == "departure_list":
                    for k in toks:
                        if not parse_clock_24h(k.compact):
                            pr.unused_lines.append((round(ln.y, 1), "token", k.text, ""))
                            continue
                        ci = min(range(len(columns)), key=lambda j: abs(columns[j][0] - k.xc))
                        origin = columns[ci][1]
                        others = [c[1] for j, c in enumerate(columns) if j != ci]
                        letter = "abcdefgh"[ci]
                        block.rows.append(RawRow(
                            f"{block.block_id}:r{line_idx:02d}{letter}", pno, ln.y,
                            {"origin": origin, "destination_implied": others[0] if len(others) == 1 else "",
                             "dep_time": k.compact, "column_index": ci, "list_position": line_idx},
                            ln.raw_dv, "", t, []))
                else:
                    cells = {"start": "", "end": "", "frequency": ""}
                    for k in toks:
                        ci = min(range(len(columns)), key=lambda j: abs(columns[j][0] - k.xc))
                        key = columns[ci][1]
                        cells[key] = (cells[key] + " " + k.text).strip()
                    block.rows.append(RawRow(f"{block.block_id}:r{line_idx:02d}", pno, ln.y, cells,
                                             ln.raw_dv, "", t, []))
            elif kind == "note":
                pr.page_notes_en.append(t)
                if block is not None:
                    block.notes_en.append(t)
            elif kind == "effective":
                pr.footer.append(eff)
            else:
                pr.unused_lines.append((round(ln.y, 1), kind, t, ln.raw_dv))
        results.append(pr)
    return results


def parse_departure_list(doc: fitz.Document, source_id: str) -> list[PageResult]:
    return _mtcc_pages(doc, source_id, "departure_list")


def parse_frequency(doc: fitz.Document, source_id: str) -> list[PageResult]:
    return _mtcc_pages(doc, source_id, "frequency")


PARSERS = {
    "four_column": parse_four_column,
    "rtl": parse_rtl,
    "departure_list": parse_departure_list,
    "frequency": parse_frequency,
}
