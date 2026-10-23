#!/usr/bin/env python
"""Extract published ferry timetables (PDF) into documented CSVs.

Usage (from the repository root):
    python scripts/extract_ferry_schedules.py              # extract + validate
    python scripts/extract_ferry_schedules.py --validate-only
    python scripts/extract_ferry_schedules.py --render      # also re-render page PNGs for review

Offline and deterministic: PyMuPDF only, no OCR, no network, no model calls.
Source PDFs are read, never modified.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ferry_pipeline.build import Build, TIMEZONE  # noqa: E402
from ferry_pipeline.config import SOURCES, load_overrides  # noqa: E402
from ferry_pipeline.validate import validate  # noqa: E402

COLUMNS = {
    "sources": "source_id,filename,sha256,page_count,document_title,operator_name,network_name,effective_from,"
               "effective_to,date_evidence,source_url,extraction_method,notes",
    "stops": "stop_id,island_name_en,island_name_dv,atoll_abbreviation,official_island_code,official_atoll_code,"
             "jetty_name,latitude,longitude,identity_status,notes",
    "stop_aliases": "alias_id,stop_id,raw_name,raw_atoll,source_id,source_page,match_method,review_status",
    "routes": "route_id,operator_name,network_name,published_route_code,published_route_name,transport_type,"
              "passenger_eligibility,notes",
    "services": "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,holiday_rule,effective_from,"
                "effective_to,days_raw_en,days_raw_dv,calendar_status,source_id,source_page",
    "trips": "trip_id,route_id,service_id,source_trip_label,direction_label,origin_stop_id,destination_stop_id,"
             "schedule_type,source_id,source_page,source_block_id,review_status,notes",
    "legs": "leg_id,trip_id,leg_sequence,from_stop_id,to_stop_id,departure_time_raw,arrival_time_raw,departure_time,"
            "arrival_time,departure_day_offset,arrival_day_offset,departure_seconds,arrival_seconds,duration_minutes,"
            "source_id,source_page,source_row_id,review_status,notes",
    "stop_times": "trip_id,stop_sequence,stop_id,arrival_time,departure_time,arrival_day_offset,departure_day_offset,"
                  "arrival_seconds,departure_seconds,time_status,source_id,source_page,source_row_ids,review_status",
    "frequency_windows": "frequency_id,route_id,service_id,direction_scope,from_stop_id,to_stop_id,start_time_raw,"
                         "end_time_raw,start_time,end_time,start_day_offset,end_day_offset,start_seconds,end_seconds,"
                         "headway_seconds,operation_status,exact_times,source_id,source_page,review_status,notes",
    "schedule_versions": "version_record_id,source_id,source_page,source_block_id,route_id,service_id,"
                         "schedule_fingerprint,selection_status,comparison_source_id,decision_reason,review_status",
    "extraction_issues": "issue_id,severity,issue_type,source_id,source_page,record_type,record_id,raw_value,"
                         "description,suggested_resolution,resolution_status",
    "routing_eligibility": "trip_id,schedule_version_id,passenger_eligible,timing_complete,calendar_resolved,"
                           "stop_identity_resolved,version_resolved,routing_eligible,exclusion_reasons",
    "frequency_eligibility": "frequency_id,schedule_version_id,passenger_eligible,direction_resolved,stops_resolved,"
                             "calendar_resolved,version_resolved,headway_model_eligible,exclusion_reasons",
}


def write_csv(path: Path, columns: str, rows) -> int:
    cols = columns.split(",")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore", quoting=csv.QUOTE_MINIMAL,
                           lineterminator="\n")
        w.writeheader()
        n = 0
        for r in rows:
            w.writerow({c: r.get(c, "") for c in cols})
            n += 1
    return n


def write_interim(b: Build, interim: Path):
    interim.mkdir(parents=True, exist_ok=True)
    # raw text per source (plain PyMuPDF text; kept for audit, not used for parsing)
    import fitz
    (interim / "raw_text").mkdir(exist_ok=True)
    for s in SOURCES:
        doc = fitz.open(b.pdf_dir / s.filename)
        with (interim / "raw_text" / f"{s.source_id}.txt").open("w", encoding="utf-8") as fh:
            for i, p in enumerate(doc, 1):
                fh.write(f"\n##### {s.filename} PAGE {i}\n{p.get_text()}")
    # coordinate evidence: every parsed row with cells and y position
    rows, blocks, dv_rows = [], [], []
    for blk in b.blocks:
        blocks.append({"block_id": blk.block_id, "source_id": blk.source_id, "page": blk.page,
                       "layout": blk.layout, "y_top": round(blk.y0, 1), "route_code": blk.route_code,
                       "route_title": blk.route_title, "days_raw_en": blk.days_raw_en,
                       "days_raw_dv": blk.days_raw_dv, "notes_en": " | ".join(blk.notes_en),
                       "notes_dv": " | ".join(blk.notes_dv),
                       "meta": json.dumps({k: v for k, v in blk.meta.items() if not k.startswith("_")},
                                          ensure_ascii=False, default=str)})
        for r in blk.rows:
            rows.append({"row_id": r.row_id, "block_id": blk.block_id, "source_id": blk.source_id,
                         "page": r.page, "y": round(r.y, 1), "trip_label": r.trip_label,
                         "cells": json.dumps(r.cells, ensure_ascii=False), "line_text": r.line_text,
                         "raw_dv": r.dv, "parser_notes": ";".join(r.problems)})
            if r.dv:
                dv_rows.append({"source_id": blk.source_id, "page": r.page, "record": r.row_id,
                                "kind": "row_cells", "raw_dhivehi": r.dv})
        for kind, val in (("days_heading", blk.days_raw_dv), ("block_note", " | ".join(blk.notes_dv))):
            if val:
                dv_rows.append({"source_id": blk.source_id, "page": blk.page, "record": blk.block_id,
                                "kind": kind, "raw_dhivehi": val})
    write_csv(interim / "parsed_blocks.csv", "block_id,source_id,page,layout,y_top,route_code,route_title,"
              "days_raw_en,days_raw_dv,notes_en,notes_dv,meta", blocks)
    write_csv(interim / "parsed_rows.csv", "row_id,block_id,source_id,page,y,trip_label,cells,line_text,raw_dv,"
              "parser_notes", rows)
    write_csv(interim / "dhivehi_raw_strings.csv", "source_id,page,record,kind,raw_dhivehi", dv_rows)
    unused = []
    for sid, pages in b.pages.items():
        for pr in pages:
            for y, kind, text, dv in pr.unused_lines:
                unused.append({"source_id": sid, "page": pr.page, "y": y, "kind": kind, "text": text, "raw_dv": dv})
            if pr.hidden_text:
                unused.append({"source_id": sid, "page": pr.page, "y": "", "kind": "hidden_text",
                               "text": pr.hidden_text, "raw_dv": ""})
            for n in pr.footer:
                unused.append({"source_id": sid, "page": pr.page, "y": "", "kind": "footer", "text": n,
                               "raw_dv": ""})
            for n in pr.page_notes_en + pr.page_notes_dv:
                unused.append({"source_id": sid, "page": pr.page, "y": "", "kind": "page_note", "text": n,
                               "raw_dv": ""})
    write_csv(interim / "unmapped_text.csv", "source_id,page,y,kind,text,raw_dv", unused)
    write_csv(interim / "coverage.csv", "source_id,page,block_id,route_code,rows_detected,rows_extracted,"
              "coverage_status,notes", b.coverage)
    write_csv(interim / "version_diffs.csv", "comparison,route_code,older_or_a_source,a_page,newer_or_b_source,"
              "b_page,a_days,b_days,days_changed,stops_only_in_a,stops_only_in_b,legs_only_in_a,legs_only_in_b,"
              "identical_legs", b.version_diffs)
    write_csv(interim / "service_notes.csv", "service_id,notes",
              [{"service_id": k, "notes": v["_notes"]} for k, v in b.services.items() if v["_notes"]])
    # human-readable diff report
    lines = ["# Ferry schedule version difference report", "",
             "Generated by `scripts/extract_ferry_schedules.py`. One section per compared pair.",
             "Leg notation: `from_key HH:MM->to_key HH:MM`. Days are the printed English headings.", ""]
    for d in sorted(b.version_diffs, key=lambda d: (d["comparison"], str(d["route_code"]), d["older_or_a_source"])):
        lines += [f"## {d['comparison']}: route {d['route_code']}",
                  f"- A: `{d['older_or_a_source']}` p{d['a_page']} days: {d['a_days']}",
                  f"- B: `{d['newer_or_b_source']}` p{d['b_page']} days: {d['b_days']}",
                  f"- identical legs: {d['identical_legs']}; days changed: {'yes' if d['days_changed'] else 'no'}",
                  f"- stops only in A: {d['stops_only_in_a'] or '-'}; only in B: {d['stops_only_in_b'] or '-'}",
                  f"- legs only in A: {d['legs_only_in_a'] or '-'}",
                  f"- legs only in B: {d['legs_only_in_b'] or '-'}", ""]
    (interim / "version_diff_report.md").write_text("\n".join(lines), encoding="utf-8")


def render_pages(pdf_dir: Path, interim: Path, dpi: int = 90):
    import fitz
    out = interim / "renders"
    out.mkdir(parents=True, exist_ok=True)
    for s in SOURCES:
        doc = fitz.open(pdf_dir / s.filename)
        for i, p in enumerate(doc, 1):
            p.get_pixmap(dpi=dpi).save(out / f"{s.source_id}_p{i:02d}.png")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pdf-dir", type=Path, default=ROOT, help="folder holding the source PDFs (default: repo root)")
    ap.add_argument("--out-dir", type=Path, default=ROOT / "data/processed/ferry")
    ap.add_argument("--interim-dir", type=Path, default=ROOT / "data/interim/ferry")
    ap.add_argument("--manual-dir", type=Path, default=ROOT / "data/manual/ferry")
    ap.add_argument("--validate-only", action="store_true")
    ap.add_argument("--render", action="store_true", help="re-render page PNGs into the interim folder")
    args = ap.parse_args(argv)

    if not args.validate_only:
        ov = load_overrides(args.manual_dir)
        b = Build(args.pdf_dir, ov).run()
        issues = b.issues.finalize()
        tables = {
            "sources": b.sources_rows, "stops": list(b.stops.values()), "stop_aliases": b.aliases,
            "routes": list(b.routes.values()), "services": list(b.services.values()), "trips": b.trips,
            "legs": b.legs, "stop_times": b.stop_times, "frequency_windows": b.freqs,
            "schedule_versions": list(b.versions.values()), "extraction_issues": issues,
            "routing_eligibility": b.eligibility, "frequency_eligibility": b.freq_eligibility,
        }
        counts = {name: write_csv(args.out_dir / f"{name}.csv", COLUMNS[name], rows) for name, rows in tables.items()}
        write_interim(b, args.interim_dir)
        if args.render:
            render_pages(args.pdf_dir, args.interim_dir)
        summary = {
            "timezone_assumption": TIMEZONE, "row_counts": counts,
            "coverage": dict(Counter(c["coverage_status"] for c in b.coverage)),
            "version_status": dict(Counter(v["selection_status"] for v in b.versions.values())),
            "routing_eligible_trips": sum(e["routing_eligible"] for e in b.eligibility),
            "issues_by_severity": dict(Counter(i["severity"] for i in issues)),
            "issues_by_type": dict(sorted(Counter(i["issue_type"] for i in issues).items())),
        }
        (args.interim_dir / "run_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        for name, n in counts.items():
            print(f"{name:24s} {n:6d}  {args.out_dir / (name + '.csv')}")
        print(json.dumps({k: v for k, v in summary.items() if k != "row_counts"}, indent=2))

    report = validate(args.out_dir, args.pdf_dir, args.interim_dir)
    (args.interim_dir / "validation_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nVALIDATION: {report['status']}  "
          f"({report['n_checks']} checks, {len(report['failures'])} failed)")
    for f in report["failures"]:
        print("  FAIL", f["check"], "-", f["detail"][:300])
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
