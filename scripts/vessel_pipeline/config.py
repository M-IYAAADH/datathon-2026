"""Versioned configuration for the vessel service datasets."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = ROOT / "config" / "vessel_services.json"
REQUIRED = ("private_dir", "output_dir", "inputs", "reference", "timezone", "speed", "jetty", "scopes",
            "stops", "classification", "services", "coverage", "disclosure")


STOP_LOCATION_MODES = ("island_shoreline", "jetty")


def load_config(path: Path | str | None = None) -> dict:
    cfg = json.loads(Path(path or DEFAULT_CONFIG).read_text(encoding="utf-8"))
    missing = [k for k in REQUIRED if k not in cfg]
    if missing:
        raise ValueError(f"vessel config missing keys: {missing}")
    if "timezone_confirmed" not in cfg["timezone"] or "speed_unit_confirmed" not in cfg["speed"]:
        raise ValueError("timezone_confirmed and speed_unit_confirmed flags are required")
    cfg["stops"].setdefault("stop_location_mode", "island_shoreline")
    if cfg["stops"]["stop_location_mode"] not in STOP_LOCATION_MODES:
        raise ValueError(f"stops.stop_location_mode must be one of {STOP_LOCATION_MODES}")
    from .clock import load_clock_config
    cfg["clock"] = load_clock_config(Path(path).with_name("clock_offset.json") if path else None)
    return cfg
