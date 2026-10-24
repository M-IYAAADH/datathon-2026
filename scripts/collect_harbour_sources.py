"""Download raw harbour/jetty source data (network). Raw files are stored unchanged.

    .venv/bin/python scripts/collect_harbour_sources.py

One bounded Overpass query and a read-only probe of the public OneMap ArcGIS service directory.
Parsing and provenance finishing happen offline in scripts/build_harbour_reference.py.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import subprocess
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from harbour_pipeline.config import load_config  # noqa: E402
from harbour_pipeline.overpass import build_overpass_query  # noqa: E402


def fetch(url: str, ua: str, data: bytes | None = None, timeout: int = 200) -> bytes:
    """Use system curl (macOS trust store); python.org builds ship without a CA bundle."""
    cmd = ["curl", "--fail", "--location", "--silent", "--show-error", "--max-time", str(timeout), "-A", ua]
    if data is not None:
        cmd += ["--data-binary", "@-"]
    cmd.append(url)
    return subprocess.run(cmd, input=data, capture_output=True, check=True).stdout


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(ROOT / "config" / "harbour_reference.json"))
    parser.add_argument("--source", choices=["geofabrik", "overpass", "none"], default="geofabrik",
                        help="OSM route: Geofabrik Maldives extract (default) or one bounded Overpass query")
    args = parser.parse_args()
    cfg = load_config(Path(args.config))
    raw = ROOT / cfg["raw_dir"]
    raw.mkdir(parents=True, exist_ok=True)
    ua = cfg["user_agent"]
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    prior = raw / "retrieval.json"
    record: dict = json.loads(prior.read_text(encoding="utf-8")) if prior.exists() else {}
    record["last_run_utc"] = now

    if args.source == "geofabrik":
        url = cfg["geofabrik"]["url"]
        out = raw / "osm_geofabrik_maldives.osm.pbf"
        out.write_bytes(fetch(url, ua, timeout=300))
        record["osm_geofabrik"] = {"retrieved_at_utc": now, "file": out.name, "url": url, "sha256": sha256(out), "bytes": out.stat().st_size}
    if args.source == "overpass":
        query = build_overpass_query(cfg)
        body = urllib.parse.urlencode({"data": query}).encode()
        out = raw / "osm_overpass.json"
        content = fetch(cfg["overpass"]["endpoint"], ua, data=body)
        json.loads(content)  # must at least parse; content is otherwise stored unchanged
        out.write_bytes(content)
        (raw / "osm_overpass.query.txt").write_text(query, encoding="utf-8")
        record["osm_overpass"] = {"retrieved_at_utc": now, "file": out.name, "sha256": sha256(out), "bytes": out.stat().st_size}
        time.sleep(1)

    base = cfg["onemap"]["services_url"]
    directory = raw / "onemap_service_directory.json"
    directory.write_bytes(fetch(base + "?f=json", ua, timeout=60))
    meta_dir = raw / "onemap_service_metadata"
    meta_dir.mkdir(exist_ok=True)
    for svc in json.loads(directory.read_text(encoding="utf-8")).get("services", []):
        if svc.get("type") == "FeatureServer" and svc.get("name"):
            (meta_dir / f"{svc['name']}.json").write_bytes(fetch(svc["url"] + "?f=json", ua, timeout=60))
            time.sleep(0.5)
    record["onemap_service_directory"] = {"retrieved_at_utc": now, "file": directory.name, "sha256": sha256(directory)}
    (raw / "retrieval.json").write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(record, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
