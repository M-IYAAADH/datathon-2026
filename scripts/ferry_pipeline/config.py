"""Source registry and loaders for reviewed override files.

Everything here is a documented decision, not data extracted from the PDFs.
Network names are taken from file names only (the documents do not print a
network name); see ``network_evidence``.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Source:
    source_id: str
    filename: str
    parser: str
    route_ns: str             # namespace for route_id
    network_name: str
    network_evidence: str
    transport_type: str       # ferry | speed_ferry | cargo_ferry
    version_family: str       # sources in one family may hold versions of the same routes
    version_rank: int         # relative publication order inside the family (higher = later)
    version_rank_evidence: str
    filename_date: str        # date-like text in the file name; NOT a confirmed effective date


SOURCES: list[Source] = [
    Source("ctn_2022_k_aa_adh_v", "CTN-Ferry-Schedule-Effective-15.05.2022-K.And-Aa.-Adh.V.pdf", "four_column",
           "ctn", "CTN", "file name prefix 'CTN' only", "ferry", "mtcc_atoll", 1,
           "printed 'Effective: 15th May 2022' on every page", "15.05.2022"),
    Source("ctn_2024_all_atolls", "CTN-Ferry-–-All-Atolls-30.04.pdf", "four_column",
           "ctn", "CTN", "file name prefix 'CTN' only", "ferry", "mtcc_atoll", 2,
           "no printed date; PDF creation date 2024-04-28 and file name '30.04'", "30.04 (year not stated)"),
    Source("cfn_2026_all", "CFN-All-ferry-Schedule-1-August-2026.pdf", "four_column",
           "cfn", "CFN", "file name prefix 'CFN' only", "ferry", "mtcc_atoll", 3,
           "no printed date; PDF creation date 2026-07-28 and file name '1-August-2026'", "1 August 2026"),
    Source("cfn_2026_zone6", "Zone-6-CFN-Ferry-Schedule-2026.pdf", "four_column",
           "cfn", "CFN Zone 6", "file name 'Zone-6-CFN' only", "speed_ferry", "mtcc_atoll", 3,
           "no printed date; PDF creation date 2026-05-11; file name year 2026", "2026"),
    Source("speed_2026_meemu", "28.03.2026-Meemu-Speed.pdf", "four_column",
           "speed", "Speed ferry (Meemu)", "file name 'Meemu-Speed' only", "speed_ferry", "mtcc_speed", 1,
           "no printed date; PDF creation date 2026-03-27; file name '28.03.2026'", "28.03.2026"),
    Source("speed_2026_thaa_laamu", "28.03.2026-Thaa-Laamu-Speed.pdf", "four_column",
           "speed", "Speed ferry (Thaa/Laamu)", "file name 'Thaa-Laamu-Speed' only", "speed_ferry", "mtcc_speed", 1,
           "no printed date; PDF creation date 2026-03-27; file name '28.03.2026'", "28.03.2026"),
    Source("rtl_zone1", "RTL-Ferry-Zone-1.pdf", "rtl",
           "rtl_zone1", "RTL Zone 1", "file name 'RTL-Ferry-Zone-1' only", "ferry", "rtl", 1,
           "printed 'EFFECTIVE FROM' per route box (2022 dates)", ""),
    Source("mtcc_gulhifalhu_2026_v1", "Gulhifalhu-Ferry-Schedule-2026-V1.pdf", "departure_list",
           "mtcc_male", "Male-area ferries", "page heading 'Gulhifalhu Ferry'", "ferry", "mtcc_male", 1,
           "printed 'Effective: 21st March 2026'", "2026 (V1)"),
    Source("mtcc_thilafushi_2026_v2", "Thilafushi-Ferry-Schedule-2026-V2.pdf", "departure_list",
           "mtcc_male", "Male-area ferries", "page heading 'Thilafushi Ferry'", "ferry", "mtcc_male", 1,
           "printed 'Effective: 24th August 2026' (page 2 only)", "2026 (V2)"),
    Source("mtcc_cargo_2026_v4", "Cargo-Ferry-Schedule-2026-V4.pdf", "departure_list",
           "mtcc_male", "Male-area cargo ferries", "page headings '... Cargo Ferry'", "cargo_ferry", "mtcc_male", 1,
           "no printed date; PDF creation date 2026-08-11; file name '2026-V4'", "2026 (V4)"),
    Source("mtcc_vilimale_2026_v1", "Vili-male-Ferry-Schedule-2026-V1.pdf", "frequency",
           "mtcc_male", "Male-area ferries", "page heading 'Vilimale’ Ferry'", "ferry", "mtcc_male", 1,
           "printed 'Effective: 21st March 2026' (page 3 only)", "2026 (V1)"),
]

SOURCE_BY_ID = {s.source_id: s for s in SOURCES}


def _read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as fh:
        return [{k: (v or "").strip() for k, v in row.items()} for row in csv.DictReader(fh)
                if not (row.get(next(iter(row))) or "").startswith("#")]


@dataclass
class Overrides:
    source_metadata: dict          # source_id -> row
    aliases: dict                  # variant name_key -> row (canonical_key, reason)
    stop_identity: dict            # name_key -> row (identity decisions without atoll prefix)
    block_annotations: list        # rows
    reviewed_pages: set            # {(source_id, page)}
    reviewed_pages_rows: list


def load_overrides(manual_dir: Path) -> Overrides:
    sm = {r["source_id"]: r for r in _read_csv(manual_dir / "source_metadata.csv")}
    al = {r["variant_key"]: r for r in _read_csv(manual_dir / "stop_name_aliases.csv")}
    si = {r["name_key"]: r for r in _read_csv(manual_dir / "stop_identity_overrides.csv")}
    ba = _read_csv(manual_dir / "block_annotations.csv")
    rp_rows = _read_csv(manual_dir / "reviewed_pages.csv")
    rp = set()
    for r in rp_rows:
        for p in r["pages"].split(";"):
            if p.strip():
                rp.add((r["source_id"], int(p)))
    return Overrides(sm, al, si, ba, rp, rp_rows)
