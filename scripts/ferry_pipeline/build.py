"""Assemble normalised ferry records from parsed PDF evidence.

Conservative by design: values are only filled from the documents (or from a
reviewed override file); anything ambiguous is kept as printed and reported
in extraction_issues instead of being repaired.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import fitz

from .config import SOURCES, Overrides, Source
from .normalize import (
    WEEKDAYS, clock_seconds, display_name, hhmmss, name_key, parse_clock_24h, parse_clock_ampm,
    parse_weekdays_dv, parse_weekdays_en, split_atoll_prefix, step_day_offset,
)
from .parsers import PARSERS, PageResult, RawBlock, RawRow

TIMEZONE = "Asia/Male"
LONG_DWELL_MIN = 120          # a stop-over this long starts a new trip (no through-service assumed)
OVERNIGHT_FROM = 20 * 3600    # a clock time going backwards counts as crossing midnight only
OVERNIGHT_TO = 5 * 3600       # when it falls from >= 20:00 to <= 05:00 in a published sequence
MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], start=1)}
MONTHS.update({k[:3]: v for k, v in list(MONTHS.items())})

CALENDAR_RESOLVED = {"explicit_days"}


def parse_printed_date(text: str) -> str | None:
    m = re.search(r"(\d{1,2})\s*(?:st|nd|rd|th)?\s+([A-Za-z]+)\s+(\d{4})", text or "")
    if not m or m.group(2).lower() not in MONTHS:
        return None
    return f"{int(m.group(3)):04d}-{MONTHS[m.group(2).lower()]:02d}-{int(m.group(1)):02d}"


def _hash(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name_key_ascii(text)).strip("_")


def name_key_ascii(text: str) -> str:
    import unicodedata
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()


def _is_placeholder_dv(raw: str) -> bool:
    """Zone-6/Gulhifalhu style text layers: one Thaana letter repeated as filler."""
    letters = [c for c in raw if "ހ" <= c <= "ޥ"]
    return bool(letters) and len(set(letters)) <= 2 and len(letters) >= 4


# ------------------------------------------------------------------ issues

class Issues:
    def __init__(self):
        self.rows: list[dict] = []

    def add(self, severity, issue_type, source_id, page, record_type, record_id, raw_value,
            description, suggested_resolution, resolution_status="open"):
        self.rows.append({
            "severity": severity, "issue_type": issue_type, "source_id": source_id,
            "source_page": "" if page in (None, "") else int(page), "record_type": record_type,
            "record_id": record_id, "raw_value": raw_value, "description": description,
            "suggested_resolution": suggested_resolution, "resolution_status": resolution_status,
        })

    def finalize(self) -> list[dict]:
        seen, out = set(), []
        for r in self.rows:
            key = tuple(r[k] for k in ("severity", "issue_type", "source_id", "source_page",
                                       "record_type", "record_id", "description"))
            if key not in seen:
                seen.add(key)
                out.append(r)
        out.sort(key=lambda r: (r["source_id"], r["source_page"] if r["source_page"] != "" else -1,
                                r["record_type"], r["record_id"], r["issue_type"], r["description"]))
        for i, r in enumerate(out, start=1):
            r["issue_id"] = f"iss_{i:05d}"
        return out

    def blocking_types(self, record_type, record_ids) -> list[str]:
        ids = set(record_ids)
        return sorted({r["issue_type"] for r in self.rows
                       if r["severity"] == "error" and r["record_type"] == record_type and r["record_id"] in ids})

    def has_any(self, record_type, record_ids, severities=("error", "warning")) -> bool:
        ids = set(record_ids)
        return any(r["severity"] in severities and r["record_type"] == record_type and r["record_id"] in ids
                   for r in self.rows)


# ------------------------------------------------------------------- state

@dataclass
class Occurrence:
    occ_id: str               # row_id + ':' + role
    source_id: str
    page: int
    block_id: str
    route_ctx: str
    raw: str
    prefix: str | None
    unknown_prefix: str | None
    name: str
    key: str
    alias_used: str | None
    role: str
    stop_id: str = ""
    method: str = ""
    candidates: tuple = ()


@dataclass
class Build:
    pdf_dir: Path
    ov: Overrides
    issues: Issues = field(default_factory=Issues)
    pages: dict = field(default_factory=dict)          # source_id -> [PageResult]
    doc_info: dict = field(default_factory=dict)
    blocks: list = field(default_factory=list)
    occ: dict = field(default_factory=dict)
    stops: dict = field(default_factory=dict)
    aliases: list = field(default_factory=list)
    routes: dict = field(default_factory=dict)
    block_route: dict = field(default_factory=dict)
    services: dict = field(default_factory=dict)
    block_service: dict = field(default_factory=dict)
    service_days: dict = field(default_factory=dict)
    trips: list = field(default_factory=list)
    legs: list = field(default_factory=list)
    stop_times: list = field(default_factory=list)
    freqs: list = field(default_factory=list)
    versions: dict = field(default_factory=dict)
    version_diffs: list = field(default_factory=list)
    eligibility: list = field(default_factory=list)
    freq_eligibility: list = field(default_factory=list)
    coverage: list = field(default_factory=list)
    sources_rows: list = field(default_factory=list)
    block_rows_ok: dict = field(default_factory=dict)

    # ----------------------------------------------------------- loading
    def load(self):
        for s in SOURCES:
            path = self.pdf_dir / s.filename
            if not path.exists():
                raise FileNotFoundError(f"missing source PDF: {path}")
            data = path.read_bytes()
            doc = fitz.open(stream=data, filetype="pdf")
            pages = PARSERS[s.parser](doc, s.source_id)
            self.pages[s.source_id] = pages
            meta = doc.metadata or {}
            self.doc_info[s.source_id] = {
                "sha256": hashlib.sha256(data).hexdigest(),
                "page_count": doc.page_count,
                "creation": (meta.get("creationDate") or "")[2:10],
                "producer": meta.get("producer") or "",
            }
            for pr in pages:
                for b in pr.blocks:
                    b.meta["_source"] = s
                    self.blocks.append(b)
                if pr.hidden_text:
                    self.issues.add("info", "extraction_artifact", s.source_id, pr.page, "page",
                                    f"{s.source_id}:p{pr.page:02d}", pr.hidden_text[:300],
                                    "Text present in the PDF text layer but not visible on the rendered page "
                                    "(white on white or off-page); ignored.",
                                    "None; confirm visually if in doubt.", "documented_no_action")
                for y, kind, text, dv in pr.unused_lines:
                    self.issues.add("warning", "unparsed_content", s.source_id, pr.page, "page",
                                    f"{s.source_id}:p{pr.page:02d}", f"{text} {dv}".strip(),
                                    f"Visible line at y={y} ({kind}) was not mapped to any record.",
                                    "Inspect the page and extend the parser or record a manual note.")
                for note in pr.page_notes_dv:
                    if _is_placeholder_dv(note):
                        self.issues.add("info", "dhivehi_unreadable", s.source_id, pr.page, "page",
                                        f"{s.source_id}:p{pr.page:02d}", note[:200],
                                        "Dhivehi text layer contains placeholder glyphs (text is drawn as an image).",
                                        "Human reading of the rendered page if the note matters.")

    # ----------------------------------------------------------- helpers
    def src(self, block: RawBlock) -> Source:
        return block.meta["_source"]

    def canon_key(self, key: str) -> tuple[str, str | None]:
        a = self.ov.aliases.get(key)
        return (a["canonical_key"], key) if a else (key, None)

    @staticmethod
    def title_endpoints(block: RawBlock) -> list[tuple[str | None, str, str | None]]:
        t = block.route_title or ""
        if block.route_code and t.startswith(block.route_code):
            t = t[len(block.route_code):]
        if block.layout in ("departure_list", "frequency"):
            return []
        parts = [p for p in re.split(r"\s*[-–]\s*", t.strip()) if p.strip()]
        return [split_atoll_prefix(p, allow_space=True) for p in parts]

    def route_ctx(self, block: RawBlock) -> str:
        return f"{block.source_id}:p{block.page:02d}:{block.route_code or block.route_title}"

    # ------------------------------------------------------------ stops
    def resolve_stops(self):
        ctx_endpoints: dict[str, dict] = {}
        ctx_atolls: dict[str, set] = {}
        ctx_roles: dict[str, dict] = defaultdict(lambda: defaultdict(set))
        occs: list[Occurrence] = []
        for b in self.blocks:
            if b.layout == "frequency":
                continue
            rc = self.route_ctx(b)
            eps = {}
            atolls = set()
            for pre, nm, unk in self.title_endpoints(b):
                if unk:
                    self.issues.add("warning", "stop_identity", b.source_id, b.page, "route",
                                    b.route_code or b.route_title, b.route_title,
                                    f"Unrecognised atoll prefix '{unk}.' in route title; prefix ignored.",
                                    "Confirm the intended atoll (other versions of this route may help).")
                if nm.upper() == "ATOLL":
                    if pre:
                        atolls.add(pre)
                    continue
                k, _ = self.canon_key(name_key(nm))
                eps[k] = pre
                if pre:
                    atolls.add(pre)
            ctx_endpoints[rc] = eps
            ctx_atolls[rc] = atolls
            for r in b.rows:
                roles = (("dep_stop", "dep"), ("arr_stop", "arr")) if b.layout == "timed_legs" else \
                        (("origin", "dep"), ("destination_implied", "arr"))
                for cell, role in roles:
                    raw = r.cells.get(cell, "")
                    if not raw:
                        continue
                    pre, nm, unk = split_atoll_prefix(raw)
                    k0 = name_key(nm)
                    k, alias = self.canon_key(k0)
                    o = Occurrence(f"{r.row_id}:{role}", b.source_id, b.page, b.block_id, rc, raw, pre, unk,
                                   nm, k, alias, role)
                    occs.append(o)
                    ctx_roles[rc][k].add(role)

        resolved_index: set[tuple[str, str]] = set()
        pending = []
        for o in occs:
            eps, atolls = ctx_endpoints.get(o.route_ctx, {}), ctx_atolls.get(o.route_ctx, set())
            confirmed = ctx_roles[o.route_ctx][o.key] == {"dep", "arr"} or o.key in eps
            if o.prefix:
                o.method, atoll = "explicit_cell_prefix", o.prefix
            elif o.key in self.ov.stop_identity:
                o.method, atoll = "reviewed_no_atoll_prefix", ""
            elif o.key in eps and eps[o.key]:
                o.method, atoll = "route_title_endpoint", eps[o.key]
            elif len(atolls) == 1 and confirmed:
                o.method, atoll = "single_atoll_route_title", next(iter(atolls))
            else:
                o.candidates = tuple(sorted(atolls))
                pending.append(o)
                continue
            o.stop_id = self._stop_id(atoll, o.key)
            if o.method != "reviewed_no_atoll_prefix":
                resolved_index.add((atoll, o.key))
        for o in pending:
            hits = [a for a in o.candidates if (a, o.key) in resolved_index]
            if len(hits) == 1:
                o.method = "cross_route_name_match"
                o.stop_id = self._stop_id(hits[0], o.key)
            else:
                o.method = "unresolved"
                cand = "-".join(a.lower() for a in o.candidates) or "none"
                o.stop_id = f"stp_unres_{cand}_{o.key}"
        for o in occs:
            self.occ[o.occ_id] = o

        rank = {"explicit_cell_prefix": 0, "route_title_endpoint": 0, "reviewed_no_atoll_prefix": 0,
                "single_atoll_route_title": 1, "cross_route_name_match": 2, "unresolved": 3}
        status = {0: "atoll_explicit", 1: "atoll_inferred_route_title", 2: "atoll_inferred_cross_route",
                  3: "atoll_unresolved"}
        by_stop = defaultdict(list)
        for o in occs:
            by_stop[o.stop_id].append(o)
        for sid, os_ in sorted(by_stop.items()):
            best = min(rank[o.method] for o in os_)
            names = Counter(display_name(o.name) for o in os_ if not o.alias_used) or \
                Counter(display_name(o.name) for o in os_)
            disp = sorted(names.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
            atoll = "" if sid.startswith(("stp_unres_", "stp_noatoll_")) else sid.split("_")[1].upper()
            key = os_[0].key
            notes = ["Island-level stop: no jetty/terminal is named in the sources; terminal identity unresolved."]
            ident = status[best]
            if key in self.ov.stop_identity:
                ident = self.ov.stop_identity[key]["identity_status"]
                notes.append(self.ov.stop_identity[key]["reason"])
            if ident == "atoll_unresolved":
                cands = sorted({c for o in os_ for c in o.candidates})
                notes.append(f"Atoll not established; route-title atolls: {', '.join(cands) or 'none'}.")
                srcs = sorted({(o.source_id, o.page) for o in os_})
                self.issues.add("warning", "stop_identity", srcs[0][0], srcs[0][1], "stop", sid,
                                "; ".join(sorted({o.raw for o in os_})),
                                f"Atoll for '{disp}' could not be established from cell prefixes, route titles or "
                                f"other routes (candidates: {', '.join(cands) or 'none'}).",
                                "Confirm the island's atoll from an authoritative island register.")
            elif best == 1:
                notes.append("Atoll inferred: both route-title endpoints share this atoll prefix.")
            elif best == 2:
                notes.append("Atoll inferred: only candidate atoll in which this name is otherwise established.")
            self.stops[sid] = {
                "stop_id": sid, "island_name_en": disp, "island_name_dv": "", "atoll_abbreviation": atoll,
                "official_island_code": "", "official_atoll_code": "", "jetty_name": "", "latitude": "",
                "longitude": "", "identity_status": ident,
                "notes": " ".join(notes) + " Dhivehi name not filled: extracted Thaana is glyph-reordered "
                                           "(raw strings in data/interim/ferry/dhivehi_raw_strings.csv).",
            }
        # same name, several explicit atolls -> keep separate, ask a human
        by_key = defaultdict(set)
        for o in occs:
            if o.method in ("explicit_cell_prefix", "route_title_endpoint"):
                by_key[o.key].add(o.stop_id)
        for k, sids in sorted(by_key.items()):
            if len(sids) > 1:
                self.issues.add("info", "stop_identity", "", "", "stop", ";".join(sorted(sids)), k,
                                f"Name '{k}' is printed with different atoll prefixes; kept as separate stops "
                                f"({', '.join(sorted(sids))}).",
                                "Confirm whether these are distinct islands or a prefix error in a title.",
                                "documented_no_action")
        # alias table
        seen = {}
        for o in occs:
            raw_atoll = o.prefix or ""
            key = (o.stop_id, o.raw, raw_atoll, o.source_id, o.page)
            if key in seen:
                continue
            mm = "name_key_exact" if not o.alias_used else f"reviewed_alias({o.alias_used}->{o.key})"
            mm += f"+{o.method}"
            rs = ("unresolved_needs_human_review" if o.method == "unresolved"
                  else "alias_ai_agent_reviewed" if o.alias_used else "auto_rule")
            seen[key] = {"stop_id": o.stop_id, "raw_name": o.raw, "raw_atoll": raw_atoll,
                         "source_id": o.source_id, "source_page": o.page, "match_method": mm,
                         "review_status": rs}
        for i, row in enumerate(sorted(seen.values(), key=lambda r: (r["stop_id"], r["source_id"],
                                                                     r["source_page"], r["raw_name"])), 1):
            self.aliases.append({"alias_id": f"als_{i:05d}", **row})

    def _stop_id(self, atoll: str, key: str) -> str:
        return f"stp_{atoll.lower()}_{key}" if atoll else f"stp_noatoll_{key}"

    # ------------------------------------------------------------ routes
    def build_routes(self):
        variants = defaultdict(list)
        for b in self.blocks:
            s = self.src(b)
            if b.route_code:
                rid = f"{s.route_ns}:{b.route_code}"
                name = b.route_title[len(b.route_code):].strip() if b.route_title.startswith(b.route_code) \
                    else b.route_title
            else:
                rid = f"{s.route_ns}:{_slug(b.route_title)}"
                name = b.route_title
            name = re.sub(r"\s+", " ", name).strip()
            self.block_route[b.block_id] = rid
            variants[rid].append((s.version_rank, s.source_id, name, s))
        for rid, vs in sorted(variants.items()):
            vs.sort(key=lambda v: (-v[0], v[1]))
            rank, sid, name, s = vs[0]
            names = sorted({v[2] for v in vs})
            notes = []
            if len(names) > 1:
                notes.append("Title varies between sources: " + " | ".join(names) + ".")
            if s.transport_type == "cargo_ferry":
                pe = "not_established"
                notes.append("Cargo ferry; passenger carriage is not stated, so excluded from passenger routing.")
            else:
                pe = "presumed_public_ferry"
                notes.append("Published public ferry timetable; passenger carriage presumed, not stated.")
            if s.transport_type == "speed_ferry" and s.source_id.startswith("speed_"):
                notes.append("'Speed' classification comes from the file name only.")
            op = self.ov.source_metadata.get(sid, {}).get("operator_name", "")
            self.routes[rid] = {
                "route_id": rid, "operator_name": op, "network_name": s.network_name,
                "published_route_code": rid.split(":", 1)[1] if s.route_ns != "mtcc_male" else "",
                "published_route_name": name, "transport_type": s.transport_type,
                "passenger_eligibility": pe,
                "notes": " ".join(notes + [f"Sources: {', '.join(sorted({v[1] for v in vs}))}."]),
            }

    # ---------------------------------------------------------- services
    def build_services(self):
        ann = defaultdict(list)
        for a in self.ov.block_annotations:
            ann[a["block_id"]].append(a)
        for b in self.blocks:
            s = self.src(b)
            sid = f"svc_{b.block_id}"
            self.block_service[b.block_id] = sid
            raw_en = b.days_raw_en
            days, notes = parse_weekdays_en(raw_en.replace("OPERATING DAYS:", ""))
            raw_dv = b.days_raw_dv
            status = "explicit_days" if days else "unknown_days"
            holiday = ""
            if re.search(r"holiday", raw_en, re.I):
                status = "explicit_days_plus_unlisted_holidays"
                holiday = "Also applies on 'other holidays' per heading; holiday dates are not listed in the source."
            flags = {d: ("1" if d in days else "0") if days else "" for d in WEEKDAYS}
            svc_notes = list(notes)
            if b.notes_en:
                svc_notes += [f"Printed note: {n}" for n in b.notes_en]
            # Dhivehi cross-check (consonant skeleton only; raises issues, never sets values)
            if raw_dv and _is_placeholder_dv(raw_dv):
                self.issues.add("info", "dhivehi_unreadable", s.source_id, b.page, "service", sid, raw_dv[:120],
                                "Dhivehi day heading is an image; the text layer holds placeholder glyphs, so the "
                                "English heading could not be cross-checked.",
                                "Human check of the rendered Dhivehi heading.", "documented_no_action")
            elif raw_dv and days:
                dv_days = parse_weekdays_dv(raw_dv)
                lang_ann = [a for a in ann.get(b.block_id, []) if a["annotation_type"] == "language_check"]
                if lang_ann and lang_ann[0]["value"] == "dhivehi_matches_english_on_visual_review":
                    self.issues.add("info", "language_conflict", s.source_id, b.page, "service", sid,
                                    f"EN: {raw_en} | DV: {raw_dv}", lang_ann[0]["description"],
                                    lang_ann[0]["reason"], "documented_no_action")
                    dv_days = None if dv_days != days else dv_days
                    if dv_days is None:
                        dv_days = days
                if dv_days is None:
                    self.issues.add("info", "language_check_incomplete", s.source_id, b.page, "service", sid,
                                    raw_dv[:120], "Could not recognise weekday names in the Dhivehi heading.",
                                    "Human check of the Dhivehi heading.", "documented_no_action")
                elif dv_days != days and status != "explicit_days_plus_unlisted_holidays":
                    diff = sorted(dv_days ^ days, key=WEEKDAYS.index)
                    for d in diff:
                        flags[d] = ""
                    status = "language_conflict"
                    self.issues.add("warning", "language_conflict", s.source_id, b.page, "service", sid,
                                    f"EN: {raw_en} | DV: {raw_dv}",
                                    f"English heading gives {sorted(days, key=WEEKDAYS.index)}, Dhivehi heading "
                                    f"appears to give {sorted(dv_days, key=WEEKDAYS.index)}; disputed days "
                                    f"({', '.join(diff)}) left blank.",
                                    "Confirm the operating days with the operator.")
            for a in ann.get(b.block_id, []):
                if a["annotation_type"] == "calendar_status":
                    status = a["value"]
                    if status in ("alternating_weeks_unanchored", "fortnightly_unanchored"):
                        flags = {d: "" for d in WEEKDAYS}
                    svc_notes.append(f"Reviewed annotation: {a['description']} {a['reason']}")
                    self.issues.add("warning", "operating_day_ambiguity", s.source_id, b.page, "service", sid,
                                    raw_en or raw_dv[:120], a["description"] + " " + a["reason"],
                                    "Obtain the anchor week / exception dates from the operator.")
            if not days and status == "unknown_days":
                self.issues.add("warning", "operating_day_ambiguity", s.source_id, b.page, "service", sid,
                                raw_dv[:120], "Block has no English day heading; operating days unknown.",
                                "Read the Dhivehi heading/notes and confirm with the operator.")
            eff = ""
            eff_raw = b.meta.get("effective_raw") or b.meta.get("effective_raw_page") or ""
            if not eff_raw:
                pr = self.page_result(b)
                eff_raw = " ".join(f for f in pr.footer if "Effective" in f)
            if eff_raw:
                eff = parse_printed_date(eff_raw) or ""
            self.service_days[sid] = days
            self.services[sid] = {
                "service_id": sid, **flags, "holiday_rule": holiday, "effective_from": eff, "effective_to": "",
                "days_raw_en": raw_en, "days_raw_dv": raw_dv, "calendar_status": status,
                "source_id": s.source_id, "source_page": b.page, "_notes": " ".join(svc_notes),
            }
            for n in notes:
                self.issues.add("info", "operating_day_ambiguity", s.source_id, b.page, "service", sid, raw_en, n,
                                "None required.", "documented_no_action")

    def page_result(self, b: RawBlock) -> PageResult:
        return self.pages[b.source_id][b.page - 1]

    # ------------------------------------------------------------- trips
    def build_trips(self):
        ann_rows = {}
        for a in self.ov.block_annotations:
            if a["annotation_type"] == "row_language_conflict":
                for rid in a["affected_rows"].split(";"):
                    ann_rows[rid.strip()] = a
        reviewed = self.ov.reviewed_pages
        for b in self.blocks:
            s = self.src(b)
            if b.layout == "timed_legs":
                self._timed_block(b, s, ann_rows, reviewed)
            elif b.layout == "departure_list":
                self._departure_block(b, s, reviewed)
            elif b.layout == "frequency":
                self._frequency_block(b, s)

    def _parse_time(self, raw: str, s: Source, row: RawRow) -> tuple[tuple[int, int] | None, str | None]:
        if not raw:
            return None, "missing"
        if s.parser == "rtl":
            return parse_clock_ampm(raw)
        hm = parse_clock_24h(raw)
        return (hm, None) if hm else (None, "unparseable")

    def _timed_block(self, b: RawBlock, s: Source, ann_rows, reviewed):
        rid, svc = self.block_route[b.block_id], self.block_service[b.block_id]
        eps = [name_key(n) for _, n, _ in self.title_endpoints(b)]
        eps = [self.canon_key(k)[0] for k in eps]
        rows = []
        ok_rows = 0
        for r in b.rows:
            dep, dprob = self._parse_time(r.cells.get("dep_time", ""), s, r)
            arr, aprob = self._parse_time(r.cells.get("arr_time", ""), s, r)
            fo, to = self.occ.get(f"{r.row_id}:dep"), self.occ.get(f"{r.row_id}:arr")
            leg_id = f"leg_{r.row_id}"
            notes = []
            for p in r.problems:
                if p.startswith("stray_fragment_removed"):
                    notes.append("invisible text-layer fragment '" + p.split(":")[-1] + "' removed from island cell")
                elif p.endswith("_on_offset_line"):
                    notes.append(p.split("_on_")[0] + " printed slightly off its row; attached by position")
                else:
                    notes.append(p)
            for label, prob, raw in (("departure", dprob, r.cells.get("dep_time", "")),
                                     ("arrival", aprob, r.cells.get("arr_time", ""))):
                if prob in ("missing", "unparseable", "invalid_12h_hour"):
                    self.issues.add("error", "time_inconsistency", s.source_id, b.page, "leg", leg_id, raw,
                                    f"{label} time {prob}.", "Read the value from the rendered page.")
                elif prob == "24h_value_with_pm_suffix":
                    self.issues.add("warning", "am_pm_ambiguity", s.source_id, b.page, "leg", leg_id, raw,
                                    f"{label} time printed as a 24-hour value with a PM suffix; kept as the 24-hour "
                                    f"value.", "Confirm the intended time with the operator.")
                    notes.append(f"{label} '{raw}' read as 24-hour value")
                elif prob == "missing_am_pm":
                    self.issues.add("warning", "am_pm_ambiguity", s.source_id, b.page, "leg", leg_id, raw,
                                    f"{label} time has no AM/PM marker.", "Confirm from the rendered page.")
            if not fo or not to:
                self.issues.add("error", "source_row_error", s.source_id, b.page, "leg", leg_id, r.line_text,
                                "Row lacks a departure or arrival island.", "Read the row from the rendered page.")
                continue
            if fo.stop_id == to.stop_id:
                self.issues.add("error", "source_row_error", s.source_id, b.page, "leg", leg_id,
                                f"{fo.raw} -> {to.raw}",
                                "Departure and arrival island are the same in this row.",
                                "Confirm the intended island with the operator; row excluded from routing.")
            if r.row_id in ann_rows:
                a = ann_rows[r.row_id]
                self.issues.add("error", "language_conflict", s.source_id, b.page, "leg", leg_id,
                                f"{fo.raw} -> {to.raw}", a["description"], a["reason"])
                notes.append("English/Dhivehi island names disagree (see issues)")
            if fo and to and not (dprob in ("missing", "unparseable", "invalid_12h_hour") or
                                  aprob in ("missing", "unparseable", "invalid_12h_hour")):
                ok_rows += 1
            rows.append((r, fo, to, dep, arr, notes, leg_id))
        self.block_rows_ok[b.block_id] = (len(b.rows), ok_rows)

        # segment into trips
        groups, cur, reasons = [], [], []
        for item in rows:
            r, fo, to, dep, arr = item[:5]
            if cur:
                pr_, pfo, pto, pdep, parr = cur[-1][:5]
                why = []
                if r.trip_label != pr_.trip_label:
                    why.append("source_trip_label")
                if fo.stop_id != pto.stop_id:
                    why.append("discontinuity")
                elif to.stop_id == pfo.stop_id:
                    why.append("reversal")
                elif dep and parr and clock_seconds(*dep) - clock_seconds(*parr) >= LONG_DWELL_MIN * 60:
                    why.append("long_dwell")
                if why:
                    groups.append((cur, reasons))
                    cur, reasons = [], why
            cur.append(item)
        if cur:
            groups.append((cur, reasons))

        rc_page = (s.source_id, b.page) in reviewed
        for ti, (items, why) in enumerate(groups, start=1):
            trip_id = f"trp_{b.block_id}:t{ti:02d}"
            if "discontinuity" in why:
                prev_items = groups[ti - 2][0]
                self.issues.add("warning", "trip_segmentation", s.source_id, b.page, "trip", trip_id,
                                f"{prev_items[-1][2].raw} -> {items[0][1].raw}",
                                "Row does not continue from the previous row's arrival island; started a new trip "
                                "rather than inventing a connection.",
                                "Check the table: an island column may be misprinted.")
            if "long_dwell" in why:
                self.issues.add("info", "trip_segmentation", s.source_id, b.page, "trip", trip_id,
                                items[0][1].raw,
                                f"Stop-over of {LONG_DWELL_MIN}+ minutes at {items[0][1].raw}; split into a new trip "
                                f"(no through-service assumed).", "None unless through-ticketing is confirmed.",
                                "documented_no_action")
            # day offsets along the published sequence
            offset, prev = 0, None
            events = []
            for (r, fo, to, dep, arr, notes, leg_id) in items:
                rec = {}
                for label, hm in (("departure", dep), ("arrival", arr)):
                    if hm is None:
                        rec[label] = None
                        continue
                    c = clock_seconds(*hm)
                    offset, kind = step_day_offset(prev, c, offset, OVERNIGHT_FROM, OVERNIGHT_TO)
                    if kind == "overnight":
                        self.issues.add("info", "midnight_interpretation", s.source_id, b.page, "leg", leg_id,
                                        hhmmss(*hm), "Time falls after midnight in the published sequence; "
                                        "service-day offset 1 applied.", "None.", "documented_no_action")
                    elif kind == "backwards":
                        self.issues.add("error", "time_inconsistency", s.source_id, b.page, "leg", leg_id,
                                        f"{label} {hhmmss(*hm)[:5]} after {prev // 3600:02d}:{prev % 3600 // 60:02d}",
                                        f"{label.capitalize()} time is earlier than the previous event in the "
                                        f"published sequence; not adjusted.",
                                        "Confirm the printed time with the operator.")
                    if prev is not None and c == prev and label == "arrival":
                        self.issues.add("error", "time_inconsistency", s.source_id, b.page, "leg", leg_id,
                                        hhmmss(*hm)[:5], "Arrival time equals departure time.",
                                        "Confirm the printed time with the operator.")
                    rec[label] = (hm, offset, offset * 86400 + c)
                    prev = c
                events.append(rec)
            direction = "not_determined"
            o_key, d_key = items[0][1].key, items[-1][2].key
            if len(eps) >= 2:
                if o_key == eps[0] or d_key == eps[-1]:
                    direction = f"title_order ({eps[0]} -> {eps[-1]})"
                elif o_key == eps[-1] or d_key == eps[0]:
                    direction = f"reverse_title_order ({eps[-1]} -> {eps[0]})"
            leg_ids = [it[6] for it in items]
            trip = {
                "trip_id": trip_id, "route_id": rid, "service_id": svc,
                "source_trip_label": items[0][0].trip_label, "direction_label": direction,
                "origin_stop_id": items[0][1].stop_id, "destination_stop_id": items[-1][2].stop_id,
                "schedule_type": "scheduled", "source_id": s.source_id, "source_page": b.page,
                "source_block_id": b.block_id, "review_status": "",
                "notes": ("Split from previous rows: " + ", ".join(why) + ". ") if why else "",
                "_leg_ids": leg_ids, "_reviewed_page": rc_page,
            }
            self.trips.append(trip)
            for seq, ((r, fo, to, dep, arr, notes, leg_id), ev) in enumerate(zip(items, events), start=1):
                d, a = ev["departure"], ev["arrival"]
                dur = (a[2] - d[2]) // 60 if d and a and a[2] > d[2] else ""
                self.legs.append({
                    "leg_id": leg_id, "trip_id": trip_id, "leg_sequence": seq,
                    "from_stop_id": fo.stop_id, "to_stop_id": to.stop_id,
                    "departure_time_raw": r.cells.get("dep_time", ""), "arrival_time_raw": r.cells.get("arr_time", ""),
                    "departure_time": hhmmss(*d[0]) if d else "", "arrival_time": hhmmss(*a[0]) if a else "",
                    "departure_day_offset": d[1] if d else "", "arrival_day_offset": a[1] if a else "",
                    "departure_seconds": d[2] if d else "", "arrival_seconds": a[2] if a else "",
                    "duration_minutes": dur, "source_id": s.source_id, "source_page": b.page,
                    "source_row_id": r.row_id, "review_status": "", "notes": "; ".join(notes),
                    "_reviewed_page": rc_page,
                })

    def _departure_block(self, b: RawBlock, s: Source, reviewed):
        rid, svc = self.block_route[b.block_id], self.block_service[b.block_id]
        rc_page = (s.source_id, b.page) in reviewed
        by_col = defaultdict(list)
        for r in sorted(b.rows, key=lambda r: (r.cells["column_index"], r.cells["list_position"])):
            by_col[r.cells["column_index"]].append(r)
        n = 0
        ok = 0
        for col, rs in sorted(by_col.items()):
            offset, prev = 0, None
            for r in rs:
                n += 1
                fo, to = self.occ[f"{r.row_id}:dep"], self.occ.get(f"{r.row_id}:arr")
                hm = parse_clock_24h(r.cells["dep_time"])
                leg_id = f"leg_{r.row_id}"
                trip_id = f"trp_{b.block_id}:t{n:02d}"
                if hm is None:
                    self.issues.add("error", "time_inconsistency", s.source_id, b.page, "leg", leg_id,
                                    r.cells["dep_time"], "Unparseable departure time.", "Read from page.")
                    continue
                ok += 1
                c = clock_seconds(*hm)
                offset, kind = step_day_offset(prev, c, offset, OVERNIGHT_FROM, OVERNIGHT_TO)
                if kind == "overnight":
                    self.issues.add("info", "midnight_interpretation", s.source_id, b.page, "leg", leg_id,
                                    r.cells["dep_time"],
                                    "Departure follows a late-evening departure in the same 'FROM' column, so it "
                                    "is placed on the following calendar day of the same service day (offset 1). "
                                    "Only this column's own order was used.",
                                    "Confirm with the operator that this run belongs to the previous service day.",
                                    "documented_no_action")
                elif kind == "backwards":
                    self.issues.add("error", "time_inconsistency", s.source_id, b.page, "leg", leg_id,
                                    r.cells["dep_time"], "Departure earlier than the previous one in its column.",
                                    "Confirm the printed time.")
                prev = c
                self.trips.append({
                    "trip_id": trip_id, "route_id": rid, "service_id": svc, "source_trip_label": "",
                    "direction_label": f"from {fo.name}", "origin_stop_id": fo.stop_id,
                    "destination_stop_id": to.stop_id if to else "", "schedule_type": "departure_only",
                    "source_id": s.source_id, "source_page": b.page, "source_block_id": b.block_id,
                    "review_status": "", "notes": "Departure-only list: destination implied by the two-column "
                                                  "'FROM' table; arrival time and sailing duration not published.",
                    "_leg_ids": [leg_id], "_reviewed_page": rc_page,
                })
                self.legs.append({
                    "leg_id": leg_id, "trip_id": trip_id, "leg_sequence": 1, "from_stop_id": fo.stop_id,
                    "to_stop_id": to.stop_id if to else "", "departure_time_raw": r.cells["dep_time"],
                    "arrival_time_raw": "", "departure_time": hhmmss(*hm), "arrival_time": "",
                    "departure_day_offset": offset, "arrival_day_offset": "",
                    "departure_seconds": offset * 86400 + c, "arrival_seconds": "", "duration_minutes": "",
                    "source_id": s.source_id, "source_page": b.page, "source_row_id": r.row_id,
                    "review_status": "", "notes": "Independent departure; the adjacent time in the other column is "
                                                  "a separate departure, not this sailing's arrival.",
                    "_reviewed_page": rc_page,
                })
        self.block_rows_ok[b.block_id] = (len(b.rows), ok)
        self.issues.add("info", "departure_only_timing", s.source_id, b.page, "service",
                        self.block_service[b.block_id], b.route_title,
                        "Departure-only table: arrival times and durations are blank (unknown); these trips are not "
                        "usable for arrival-time routing until durations are sourced.",
                        "Source published crossing durations.", "documented_no_action")

    def _frequency_block(self, b: RawBlock, s: Source):
        rid, svc = self.block_route[b.block_id], self.block_service[b.block_id]
        offset, prev_end = 0, None
        ok = 0
        for r in b.rows:
            fid = f"frq_{r.row_id}"
            st, en = parse_clock_24h(r.cells.get("start", "")), parse_clock_24h(r.cells.get("end", ""))
            freq = r.cells.get("frequency", "")
            if not st or not en:
                self.issues.add("error", "time_inconsistency", s.source_id, b.page, "frequency_window", fid,
                                json.dumps(r.cells), "Unparseable start/end.", "Read from page.")
                continue
            ok += 1
            sc, ec = clock_seconds(*st), clock_seconds(*en)
            if prev_end is not None:
                if sc < prev_end[0] and prev_end[0] - sc > 12 * 3600:
                    offset += 1
                if prev_end[1] * 86400 + prev_end[0] != offset * 86400 + sc:
                    self.issues.add("warning", "frequency_gap", s.source_id, b.page, "frequency_window", fid,
                                    f"previous end {prev_end[2]} / start {r.cells['start']}",
                                    "Window does not start where the previous window ended; the gap is preserved "
                                    "as printed (no service is implied or invented for it).",
                                    "Confirm whether the gap is intentional.")
            start_off = offset
            end_off = start_off + (1 if ec <= sc else 0)
            if end_off > start_off or start_off > 0:
                self.issues.add("warning", "midnight_interpretation", s.source_id, b.page, "frequency_window", fid,
                                f"{r.cells['start']}-{r.cells['end']}",
                                "Window lies after midnight; anchored to the previous service day by table order "
                                "(the table runs 05:30 to 05:30). Which calendar date's heading governs the early-"
                                "morning windows is not stated.",
                                "Confirm service-day anchoring with the operator.")
            m = re.match(r"(\d+)\s*(MIN|HR)", freq.upper())
            if m:
                headway = int(m.group(1)) * (60 if m.group(2) == "MIN" else 3600)
                status = "operating"
            elif re.search(r"break", freq, re.I):
                headway, status = "", "suspended"
            else:
                headway, status = "", "operating"
                self.issues.add("error", "unparsed_content", s.source_id, b.page, "frequency_window", fid, freq,
                                "Frequency text not understood.", "Read from page.")
            self.freqs.append({
                "frequency_id": fid, "route_id": rid, "service_id": svc, "direction_scope": "unspecified",
                "from_stop_id": "", "to_stop_id": "", "start_time_raw": r.cells["start"],
                "end_time_raw": r.cells["end"], "start_time": hhmmss(*st), "end_time": hhmmss(*en),
                "start_day_offset": start_off, "end_day_offset": end_off,
                "start_seconds": start_off * 86400 + sc, "end_seconds": end_off * 86400 + ec,
                "headway_seconds": headway, "operation_status": status, "exact_times": 0,
                "source_id": s.source_id, "source_page": b.page, "review_status": "",
                "notes": (f"Printed frequency '{freq}'. " if freq else "") +
                         "Direction and terminals are not stated in the table; whether the headway applies per "
                         "direction is unknown. Departures are NOT expanded into exact times.",
            })
            prev_end = (ec, end_off, r.cells["end"])
            offset = end_off
        self.block_rows_ok[b.block_id] = (len(b.rows), ok)
        self.issues.add("warning", "direction_unspecified", s.source_id, b.page, "service",
                        self.block_service[b.block_id], b.route_title,
                        "Frequency table gives no direction or terminal names; from/to stops left blank and "
                        "direction_scope='unspecified'.", "Confirm terminals and whether headways are per direction.")

    # ------------------------------------------------------------ stop_times
    def build_stop_times(self):
        legs_by_trip = defaultdict(list)
        for lg in self.legs:
            legs_by_trip[lg["trip_id"]].append(lg)
        for t in self.trips:
            lgs = sorted(legs_by_trip[t["trip_id"]], key=lambda x: x["leg_sequence"])
            if not lgs:
                continue
            seq = []
            seq.append((lgs[0]["from_stop_id"], None, lgs[0], [lgs[0]["source_row_id"]]))
            for i, lg in enumerate(lgs):
                nxt = lgs[i + 1] if i + 1 < len(lgs) else None
                seq.append((lg["to_stop_id"], lg, nxt, [lg["source_row_id"]] + ([nxt["source_row_id"]] if nxt else [])))
            for n, (stop, inc, out, rows) in enumerate(seq, start=1):
                arr = inc if inc and inc["arrival_time"] else None
                dep = out if out and out["departure_time"] else None
                ts = ("published_arrival_and_departure" if arr and dep else
                      "published_departure_only" if dep else "published_arrival_only" if arr else "unknown")
                self.stop_times.append({
                    "trip_id": t["trip_id"], "stop_sequence": n, "stop_id": stop,
                    "arrival_time": arr["arrival_time"] if arr else "",
                    "departure_time": dep["departure_time"] if dep else "",
                    "arrival_day_offset": arr["arrival_day_offset"] if arr else "",
                    "departure_day_offset": dep["departure_day_offset"] if dep else "",
                    "arrival_seconds": arr["arrival_seconds"] if arr else "",
                    "departure_seconds": dep["departure_seconds"] if dep else "",
                    "time_status": ts, "source_id": t["source_id"], "source_page": t["source_page"],
                    "source_row_ids": ";".join(rows), "review_status": "",
                })

    # ------------------------------------------------------------ versions
    def block_signature(self, b: RawBlock):
        days = sorted(self.service_days.get(self.block_service[b.block_id], set()), key=WEEKDAYS.index)
        legs = []
        for r in b.rows:
            if b.layout == "timed_legs":
                fo, to = self.occ.get(f"{r.row_id}:dep"), self.occ.get(f"{r.row_id}:arr")
                legs.append([fo.key if fo else "", self._norm_t(r.cells.get("dep_time", "")),
                             to.key if to else "", self._norm_t(r.cells.get("arr_time", ""))])
            elif b.layout == "departure_list":
                fo = self.occ.get(f"{r.row_id}:dep")
                legs.append([fo.key if fo else "", self._norm_t(r.cells["dep_time"])])
            else:
                legs.append([r.cells.get("start"), r.cells.get("end"), r.cells.get("frequency")])
        # Parsed weekdays, not heading wording ('Saturday and Thursday' == 'Saturday, Thursday').
        return {"days": days, "days_raw": "" if days else (b.days_raw_en + b.days_raw_dv), "legs": legs}

    @staticmethod
    def _norm_t(raw):
        hm = parse_clock_24h(raw)
        if hm:
            return f"{hm[0]:02d}:{hm[1]:02d}"
        hm, _ = parse_clock_ampm(raw)
        return f"{hm[0]:02d}:{hm[1]:02d}" if hm else raw

    def build_versions(self):
        sigs = {b.block_id: self.block_signature(b) for b in self.blocks}
        fps = {k: _hash(v) for k, v in sigs.items()}
        # route-page units
        units = defaultdict(list)
        for b in self.blocks:
            s = self.src(b)
            units[(s.version_family, b.route_code or self.block_route[b.block_id], s.source_id, b.page)].append(b)
        unit_keys = {}
        for (fam, code, sid, page), bs in units.items():
            stopset = {leg[i] for b in bs for leg in sigs[b.block_id]["legs"] for i in (0, 2) if len(leg) > 2}
            unit_keys[(fam, code, sid, page)] = (stopset, _hash([sigs[b.block_id] for b in bs]))
        decided = {}
        reason = {}
        comp = {}
        # within-source duplicates of a route code on different pages with overlapping days
        by_src_code = defaultdict(list)
        for (fam, code, sid, page), bs in units.items():
            by_src_code[(sid, code)].append((page, bs))
        conflict_blocks = set()
        for (sid, code), pages in by_src_code.items():
            if len(pages) < 2:
                continue
            for i in range(len(pages)):
                for j in range(i + 1, len(pages)):
                    pi, bi = pages[i]
                    pj, bj = pages[j]
                    di = set().union(*[self.service_days[self.block_service[b.block_id]] for b in bi])
                    dj = set().union(*[self.service_days[self.block_service[b.block_id]] for b in bj])
                    if di & dj:
                        for b in bi + bj:
                            conflict_blocks.add(b.block_id)
                        self.issues.add("error", "version_conflict", sid, pi, "route", code,
                                        f"pages {pi} and {pj}",
                                        f"Route {code} appears on pages {pi} and {pj} of the same document with "
                                        f"overlapping operating days but different trip lists; neither is preferred.",
                                        "Ask the operator which page is current (or whether both vessels run).")
                        self.version_diffs.append(self._diff(sid, code, pi, bi, sid, pj, bj, sigs, "within_source"))
        # families
        fam_groups = defaultdict(list)
        for (fam, code, sid, page), v in unit_keys.items():
            fam_groups[(fam, code)].append((sid, page))
        for (fam, code), members in fam_groups.items():
            srcs = {sid for sid, _ in members}
            ranked = sorted(srcs, key=lambda x: -next(s.version_rank for s in SOURCES if s.source_id == x))
            if fam == "rtl":
                for sid, page in members:
                    for b in units[(fam, code, sid, page)]:
                        decided[b.block_id] = "unresolved"
                        reason[b.block_id] = ("Only version: RTL Zone 1 box effective from a 2022 date. No later "
                                              "RTL edition is available to confirm or replace it; later MTCC CTN/CFN "
                                              "routes serve the same islands with different codes and times, so "
                                              "replacement cannot be demonstrated either. Current validity unknown.")
                continue
            top_rank = max(next(s.version_rank for s in SOURCES if s.source_id == x) for x in srcs)
            newest = [x for x in ranked if next(s.version_rank for s in SOURCES if s.source_id == x) == top_rank]
            new_members = [(sid, p) for sid, p in members if sid in newest]
            new_stops = set().union(*[unit_keys[(fam, code, sid, p)][0] for sid, p in new_members])
            new_fp = sorted(unit_keys[(fam, code, sid, p)][1] for sid, p in new_members)
            for sid, page in members:
                bs = units[(fam, code, sid, page)]
                stops, fp = unit_keys[(fam, code, sid, page)]
                if sid in newest:
                    for b in bs:
                        decided[b.block_id] = "preferred_candidate"
                        older = sorted(srcs - set(newest))
                        comp[b.block_id] = ";".join(older)
                        reason[b.block_id] = ("Latest available version in its document family"
                                              + (f" (later than {', '.join(older)} by printed/filename/PDF-metadata "
                                                 f"dates)" if older else "; no other version found")
                                              + ". Not a confirmation that the service currently operates.")
                    continue
                jac = len(stops & new_stops) / max(1, len(stops | new_stops))
                newest_sid = newest[0]
                for b in bs:
                    comp[b.block_id] = newest_sid
                if jac < 0.5:
                    for b in bs:
                        decided[b.block_id] = "unresolved"
                        reason[b.block_id] = (f"Same route code as {newest_sid} but island sets overlap only "
                                              f"{jac:.0%}; not demonstrably the same service.")
                    self.issues.add("warning", "version_conflict", sid, page, "route", code, f"jaccard={jac:.2f}",
                                    f"Route code {code} in {sid} serves a different island set from {newest_sid}.",
                                    "Review whether the code was reused for a different route.")
                    continue
                status = "duplicate" if [fp] == new_fp else "historical"
                for b in bs:
                    decided[b.block_id] = status
                    reason[b.block_id] = (
                        (f"Identical published content to the later {newest_sid} version."
                         if status == "duplicate" else
                         f"Superseded for this route by later {newest_sid} (same route code, {jac:.0%} island-set "
                         f"overlap). Ordering evidence: {next(s.version_rank_evidence for s in SOURCES if s.source_id == newest_sid)}.")
                        + " effective_to is not asserted.")
                new_page_members = [(s2, p2) for s2, p2 in new_members]
                if status == "historical":
                    s2, p2 = new_page_members[0]
                    self.version_diffs.append(self._diff(sid, code, page, bs, s2, p2,
                                                         [x for (f, c, si, pg), xs in units.items()
                                                          if f == fam and c == code and si == s2 for x in xs],
                                                         sigs, "historical_vs_preferred"))
        for b in self.blocks:
            status = decided.get(b.block_id, "unresolved")
            rsn = reason.get(b.block_id, "No version rule applied.")
            if b.block_id in conflict_blocks:
                status = "unresolved"
                rsn = "Conflicting pages for the same route code and days inside one document. " + rsn
            self.versions[b.block_id] = {
                "version_record_id": f"ver_{b.block_id}", "source_id": b.source_id, "source_page": b.page,
                "source_block_id": b.block_id, "route_id": self.block_route[b.block_id],
                "service_id": self.block_service[b.block_id], "schedule_fingerprint": fps[b.block_id],
                "selection_status": status, "comparison_source_id": comp.get(b.block_id, ""),
                "decision_reason": rsn, "review_status": "rule_based_needs_human_confirmation",
            }
        self._cross_family_overlaps(units, unit_keys, sigs)
        self._shared_departure_conflicts(units, sigs)

    def _shared_departure_conflicts(self, units, sigs):
        """Two preferred blocks of different routes publishing the same crossing on the same weekday.

        Either both run (unlikely: identical departure, different vessels/products) or one replaces
        the other; neither can be shown from the documents, so both become unresolved.
        """
        trip_block = {t["trip_id"]: t["source_block_id"] for t in self.trips}
        idx = defaultdict(set)
        for lg in self.legs:
            blk = trip_block[lg["trip_id"]]
            if self.versions[blk]["selection_status"] != "preferred_candidate" or not lg["arrival_time"]:
                continue
            svc = self.services[self.block_service[blk]]
            for d in WEEKDAYS:
                if svc[d] == "1":
                    idx[(lg["from_stop_id"], lg["to_stop_id"], lg["departure_time"], d)].add(blk)
        pairs = defaultdict(set)
        for (fr, to, dep, d), blks in idx.items():
            if len({self.block_route[b] for b in blks}) > 1:
                pairs[tuple(sorted(blks))].add(f"{d[:3]} {dep[:5]} {fr}->{to}")
        for blks, examples in sorted(pairs.items()):
            for b in blks:
                v = self.versions[b]
                v["selection_status"] = "unresolved"
                others = [x for x in blks if x != b]
                v["comparison_source_id"] = ";".join(sorted({x.split(':')[0] for x in others}))
                v["decision_reason"] = (f"Publishes the same crossing(s) on the same weekday as {', '.join(others)} "
                                        f"(route {', '.join(self.block_route[x] for x in others)}): "
                                        f"{'; '.join(sorted(examples))}. Whether both services run or one replaces "
                                        f"the other is not stated. " + v["decision_reason"])
            b0 = next(b for b in self.blocks if b.block_id == blks[0])
            self.issues.add("warning", "version_conflict", b0.source_id, b0.page, "route",
                            " vs ".join(self.block_route[b] for b in blks), "; ".join(sorted(examples)),
                            f"Different routes publish identical crossings on a shared weekday ({', '.join(blks)}); "
                            f"both blocks set to unresolved and excluded from routing.",
                            "Ask the operator whether both services run or which timetable is current.")
            sa = [b for b in self.blocks if b.block_id == blks[0]]
            sb = [b for b in self.blocks if b.block_id == blks[1]]
            self.version_diffs.append(self._diff(sa[0].source_id, f"{self.block_route[blks[0]]} vs "
                                                 f"{self.block_route[blks[1]]}", sa[0].page, sa,
                                                 sb[0].source_id, sb[0].page, sb, sigs, "shared_departure_conflict"))

    def _diff(self, sid_a, code, page_a, blocks_a, sid_b, page_b, blocks_b, sigs, kind):
        def legs(bs):
            return {tuple(leg) for b in bs for leg in sigs[b.block_id]["legs"]}
        def dayset(bs):
            return sorted({",".join(d[:3] for d in sigs[b.block_id]["days"]) or f"[{b.days_raw_en or 'no heading'}]"
                           for b in bs})
        days_a, days_b = dayset(blocks_a), dayset(blocks_b)
        all_a, all_b = legs(blocks_a), legs(blocks_b)
        stops_a = {x for leg in all_a for x in (leg[0], leg[2] if len(leg) > 2 else leg[0])}
        stops_b = {x for leg in all_b for x in (leg[0], leg[2] if len(leg) > 2 else leg[0])}
        return {
            "comparison": kind, "route_code": code, "older_or_a_source": sid_a, "a_page": page_a,
            "newer_or_b_source": sid_b, "b_page": page_b,
            "a_days": " / ".join(days_a), "b_days": " / ".join(days_b),
            "days_changed": int(days_a != days_b),
            "stops_only_in_a": ";".join(sorted(stops_a - stops_b)),
            "stops_only_in_b": ";".join(sorted(stops_b - stops_a)),
            "legs_only_in_a": " | ".join(f"{x[0]} {x[1]}->{x[2]} {x[3]}" if len(x) > 2 else " ".join(x)
                                         for x in sorted(all_a - all_b)),
            "legs_only_in_b": " | ".join(f"{x[0]} {x[1]}->{x[2]} {x[3]}" if len(x) > 2 else " ".join(x)
                                         for x in sorted(all_b - all_a)),
            "identical_legs": len(all_a & all_b),
        }

    def _cross_family_overlaps(self, units, unit_keys, sigs):
        items = [(k, v[0]) for k, v in unit_keys.items() if v[0]]
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                (fa, ca, sa, pa), A = items[i]
                (fb, cb, sb, pb), B = items[j]
                if fa == fb:
                    continue
                jac = len(A & B) / max(1, len(A | B))
                if jac >= 0.6:
                    va = [self.versions[b.block_id]["selection_status"] for b in units[(fa, ca, sa, pa)]]
                    vb = [self.versions[b.block_id]["selection_status"] for b in units[(fb, cb, sb, pb)]]
                    if "preferred_candidate" in va or "preferred_candidate" in vb:
                        d = self._diff(sa, f"{ca} vs {cb}", pa, units[(fa, ca, sa, pa)], sb, pb,
                                       units[(fb, cb, sb, pb)], sigs, "cross_network_potential_overlap")
                        self.version_diffs.append(d)
                        self.issues.add("info", "version_conflict", sa, pa, "route", ca,
                                        f"{sb} p{pb} route {cb}; island overlap {jac:.0%}",
                                        f"Potential overlap between {sa} route {ca} and {sb} route {cb} (different "
                                        f"networks/codes). Not merged; both kept with their own version status.",
                                        "Review whether one replaces the other.", "documented_no_action")

    # --------------------------------------------------------- review/eligibility
    def finalize_review_status(self):
        trip_by_id = {t["trip_id"]: t for t in self.trips}
        for lg in self.legs:
            flagged = self.issues.has_any("leg", [lg["leg_id"]])
            lg["review_status"] = ("needs_human_review" if flagged else
                                   "auto_extracted_ai_page_review" if lg["_reviewed_page"] else "auto_extracted")
        legs_by_trip = defaultdict(list)
        for lg in self.legs:
            legs_by_trip[lg["trip_id"]].append(lg)
        for t in self.trips:
            svc = self.services[t["service_id"]]
            flagged = (self.issues.has_any("trip", [t["trip_id"]]) or
                       any(lg["review_status"] == "needs_human_review" for lg in legs_by_trip[t["trip_id"]]) or
                       self.issues.has_any("service", [t["service_id"]], ("warning", "error")) or
                       any(self.stops[s]["identity_status"] == "atoll_unresolved"
                           for s in (t["origin_stop_id"], t["destination_stop_id"]) if s))
            t["review_status"] = ("needs_human_review" if flagged else
                                  "auto_extracted_ai_page_review" if t["_reviewed_page"] else "auto_extracted")
        for st in self.stop_times:
            st["review_status"] = trip_by_id[st["trip_id"]]["review_status"]
        for f in self.freqs:
            f["review_status"] = "needs_human_review"

    def build_eligibility(self):
        legs_by_trip = defaultdict(list)
        for lg in self.legs:
            legs_by_trip[lg["trip_id"]].append(lg)
        stops_by_trip = defaultdict(set)
        for st in self.stop_times:
            stops_by_trip[st["trip_id"]].add(st["stop_id"])
        for t in self.trips:
            lgs = legs_by_trip[t["trip_id"]]
            route = self.routes[t["route_id"]]
            svc = self.services[t["service_id"]]
            ver = self.versions[t["source_block_id"]]
            reasons = []
            pe = int(route["passenger_eligibility"] != "not_established")
            if not pe:
                reasons.append("passenger_eligibility_not_established(cargo)")
            blocking = self.issues.blocking_types("leg", [lg["leg_id"] for lg in lgs])
            timing = int(t["schedule_type"] == "scheduled" and all(
                lg["departure_seconds"] != "" and lg["arrival_seconds"] != "" and
                lg["arrival_seconds"] > lg["departure_seconds"] for lg in lgs) and
                "time_inconsistency" not in blocking)
            if not timing:
                reasons.append("departure_only_no_arrival_times" if t["schedule_type"] == "departure_only"
                               else "timing_incomplete_or_inconsistent")
            cal = int(svc["calendar_status"] in CALENDAR_RESOLVED and
                      all(svc[d] in ("0", "1") for d in WEEKDAYS))
            if not cal:
                reasons.append(f"calendar_{svc['calendar_status']}")
            sid_ok = int(all(self.stops[s]["identity_status"] != "atoll_unresolved"
                             for s in stops_by_trip[t["trip_id"]]))
            if not sid_ok:
                reasons.append("stop_identity_unresolved")
            vr = int(ver["selection_status"] == "preferred_candidate")
            if not vr:
                reasons.append(f"version_{ver['selection_status']}")
            other_block = [b for b in blocking if b != "time_inconsistency"]
            if other_block:
                reasons.append("blocking_issue:" + "+".join(other_block))
            elig = int(pe and timing and cal and sid_ok and vr and not other_block)
            self.eligibility.append({
                "trip_id": t["trip_id"], "schedule_version_id": ver["version_record_id"],
                "passenger_eligible": pe, "timing_complete": timing, "calendar_resolved": cal,
                "stop_identity_resolved": sid_ok, "version_resolved": vr, "routing_eligible": elig,
                "exclusion_reasons": ";".join(reasons),
            })
        for f in self.freqs:
            svc = self.services[f["service_id"]]
            blk = f["service_id"][4:]
            ver = self.versions[blk]
            reasons = ["direction_unspecified", "terminals_not_stated"]
            if f["operation_status"] == "suspended":
                reasons.append("suspended_window")
            if f["start_day_offset"] or f["end_day_offset"]:
                reasons.append("service_day_anchoring_unconfirmed")
            if svc["calendar_status"] not in CALENDAR_RESOLVED:
                reasons.append(f"calendar_{svc['calendar_status']}")
            self.freq_eligibility.append({
                "frequency_id": f["frequency_id"], "schedule_version_id": ver["version_record_id"],
                "passenger_eligible": int(self.routes[f["route_id"]]["passenger_eligibility"] != "not_established"),
                "direction_resolved": 0, "stops_resolved": 0,
                "calendar_resolved": int(svc["calendar_status"] in CALENDAR_RESOLVED),
                "version_resolved": int(ver["selection_status"] == "preferred_candidate"),
                "headway_model_eligible": 0, "exclusion_reasons": ";".join(reasons),
            })

    # ------------------------------------------------------------ coverage
    def build_coverage(self):
        for s in SOURCES:
            for pr in self.pages[s.source_id]:
                page_status = []
                if not pr.blocks:
                    self.coverage.append({"source_id": s.source_id, "page": pr.page, "block_id": "",
                                          "route_code": "", "rows_detected": 0, "rows_extracted": 0,
                                          "coverage_status": "non-schedule", "notes": "No timetable block found."})
                    continue
                for b in pr.blocks:
                    total, ok = self.block_rows_ok.get(b.block_id, (len(b.rows), 0))
                    ver = self.versions[b.block_id]["selection_status"]
                    svc = self.services[self.block_service[b.block_id]]
                    notes = []
                    if ok < total or (pr.unused_lines and b is pr.blocks[-1]):
                        status = "partially_extracted"
                        notes.append(f"{total - ok} row(s) not fully parsed; {len(pr.unused_lines)} unused line(s).")
                    elif ver == "duplicate":
                        status = "duplicate"
                    elif ver == "unresolved" or svc["calendar_status"] not in CALENDAR_RESOLVED | \
                            {"explicit_days_plus_unlisted_holidays"}:
                        status = "unresolved"
                        notes.append(f"version={ver}; calendar={svc['calendar_status']}")
                    else:
                        status = "extracted"
                    if b.notes_dv:
                        notes.append("Dhivehi note(s) captured as raw text; meaning recorded only via reviewed "
                                     "annotations.")
                    if ver == "historical":
                        notes.append("Extracted; version is historical.")
                    self.coverage.append({"source_id": s.source_id, "page": pr.page, "block_id": b.block_id,
                                          "route_code": b.route_code or b.route_title, "rows_detected": total,
                                          "rows_extracted": ok, "coverage_status": status,
                                          "notes": " ".join(notes)})

    # ------------------------------------------------------------ sources
    def build_sources(self):
        for s in SOURCES:
            info = self.doc_info[s.source_id]
            meta = self.ov.source_metadata.get(s.source_id, {})
            pages = self.pages[s.source_id]
            title = Counter(p.page_title for p in pages if p.page_title).most_common(1)
            printed = sorted({self.services[self.block_service[b.block_id]]["effective_from"]
                              for p in pages for b in p.blocks} - {""})
            pages_with_date = sorted({b.page for p in pages for b in p.blocks
                                      if self.services[self.block_service[b.block_id]]["effective_from"]})
            if len(printed) == 1 and len(pages_with_date) == len(pages):
                eff = printed[0]
                ev = f"printed effective date {eff} on every page"
            elif len(printed) == 1:
                eff = printed[0]
                ev = (f"printed effective date {eff} on page(s) {','.join(map(str, pages_with_date))} only; "
                      f"other pages carry no date")
                self.issues.add("info", "date_ambiguity", s.source_id, "", "source", s.source_id, eff,
                                "Effective date is printed on only some pages; applied at document level only "
                                "(service rows on undated pages keep effective_from blank).",
                                "Confirm the date applies to the whole document.", "documented_no_action")
            elif printed:
                eff = ""
                ev = "different printed effective dates per block: " + ", ".join(printed)
            else:
                eff = ""
                ev = "no effective date printed"
                self.issues.add("warning", "date_ambiguity", s.source_id, "", "source", s.source_id,
                                s.filename_date, "No effective date printed; file-name date and PDF metadata are "
                                "evidence of publication period only.", "Obtain the effective date from the operator.")
            ev += (f"; file name date text '{s.filename_date}' (unverified)" if s.filename_date else "")
            ev += f"; PDF metadata creation date {info['creation']}" if info["creation"] else ""
            self.sources_rows.append({
                "source_id": s.source_id, "filename": s.filename, "sha256": info["sha256"],
                "page_count": info["page_count"], "document_title": title[0][0] if title else "",
                "operator_name": meta.get("operator_name", ""), "network_name": s.network_name,
                "effective_from": eff, "effective_to": "", "date_evidence": ev,
                "source_url": meta.get("source_url", ""),
                "extraction_method": f"PyMuPDF character-level coordinates; parser={s.parser}; no OCR",
                "notes": "; ".join(x for x in [
                    f"Operator evidence: {meta.get('operator_evidence', 'none')}",
                    f"Network name evidence: {s.network_evidence}",
                    f"Version order evidence: {s.version_rank_evidence}",
                    meta.get("notes", "")] if x),
            })
            if not meta.get("operator_name"):
                self.issues.add("info", "operator_unconfirmed", s.source_id, "", "source", s.source_id, "",
                                "Operator is not named in the document.", "Confirm the operator.",
                                "documented_no_action")

    def run(self):
        self.load()
        self.resolve_stops()
        self.build_routes()
        self.build_services()
        self.build_trips()
        self.build_stop_times()
        self.build_versions()
        self.finalize_review_status()
        self.build_eligibility()
        self.build_coverage()
        self.build_sources()
        return self
