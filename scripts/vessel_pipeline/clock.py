"""Device clock classification from database_time - collection_time (seconds).

Pure functions plus the SQL that measures per device-day statistics. Nothing here reads when boats operate:
an unusual hour of day is never evidence about a clock.
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CLOCK_CONFIG = ROOT / "config" / "clock_offset.json"
CLASS_CODES = {"local": 0, "offset_5h": 1, "other": 2, "unknown": 3}


def load_clock_config(path: Path | str | None = None) -> dict:
    c = json.loads(Path(path or DEFAULT_CLOCK_CONFIG).read_text(encoding="utf-8"))
    for k in ("expected_offset_seconds", "correction_seconds", "local", "offset_5h", "consistency",
              "min_observations_device_day", "device", "exclude_from_regular_clustering"):
        if k not in c:
            raise ValueError(f"clock config missing key: {k}")
    return c


def classify_device_day(n_obs: int, median_diff: float | None, consistent_share: float | None, cc: dict) -> tuple[str, str]:
    """-> (class, reason). consistent_share: share of observations within consistency.window_seconds of the median."""
    if median_diff is None or n_obs < cc["min_observations_device_day"]:
        return "unknown", "too_few_observations"
    lo, hi = cc["local"]["min_median_seconds"], cc["local"]["max_median_seconds"]
    if lo <= median_diff <= hi:
        return "local", "median_difference_near_zero"
    if consistent_share is None or consistent_share < cc["consistency"]["min_share_in_window"]:
        return "unknown", "inconsistent_difference"
    if abs(median_diff - cc["expected_offset_seconds"]) <= cc["offset_5h"]["tolerance_seconds"]:
        return "offset_5h", "consistent_difference_near_5h"
    return "other", "consistent_other_difference"


MEASURED = ("local", "offset_5h", "other")


def classify_devices(day_rows: list[dict], cc: dict) -> tuple[list[dict], list[dict]]:
    """day_rows: dicts with device_key, day, n_obs, median_diff, consistent_share. Returns (resolved day rows, device rows).

    Each day row gets measured_class (own evidence only), clock_class (after inheritance) and class_reason.
    """
    dcfg = cc["device"]
    by_dev = defaultdict(list)
    out = []
    for r in sorted(day_rows, key=lambda r: (r["device_key"], str(r["day"]))):
        cls, reason = classify_device_day(r["n_obs"], r["median_diff"], r["consistent_share"], cc)
        o = dict(r, measured_class=cls, class_reason=reason, clock_class=cls)
        out.append(o)
        by_dev[r["device_key"]].append(o)
    devices = []
    for dk in sorted(by_dev):
        days = by_dev[dk]
        measured = Counter(d["measured_class"] for d in days if d["measured_class"] in MEASURED)
        n_meas = sum(measured.values())
        top, top_n = (measured.most_common(1)[0] if measured else (None, 0))
        stable = n_meas >= dcfg["min_measured_days"] and top_n / n_meas >= dcfg["min_share_same_class"]
        if stable:
            for d in days:
                if d["measured_class"] == "unknown":
                    d["clock_class"] = top
                    d["class_reason"] = f"inherited_device_class({d['class_reason']})"
        devices.append({"device_key": dk, "device_days": len(days), "measured_days": n_meas,
                        "measured_classes": dict(measured), "stable": bool(stable),
                        "device_class": top if stable else ("switching" if len(measured) > 1 else "unknown"),
                        "switches_class": len(measured) > 1})
    return out, devices


def correction_for(clock_class: str, cc: dict) -> int:
    """Seconds added to collection_time to give local time. Only the offset_5h class is ever corrected."""
    return int(cc["correction_seconds"]) if clock_class == "offset_5h" else 0


def corrected_time(collection_epoch: int, clock_class: str, cc: dict) -> int:
    return int(collection_epoch) + correction_for(clock_class, cc)


def excluded_classes(cc: dict, override: str | None = None) -> set[str]:
    if override is None:
        return set(cc["exclude_from_regular_clustering"])
    return {x.strip() for x in override.split(",") if x.strip()}


DEVICE_DAY_SQL = """
SELECT device_key, CAST(collection_time AS DATE) AS day, count(*)::BIGINT AS n_obs,
       median(diff) AS median_diff,
       avg((abs(diff - med0) <= {window})::INT) AS consistent_share
FROM (SELECT device_key, collection_time, diff, median(diff) OVER (PARTITION BY device_key, CAST(collection_time AS DATE)) AS med0
      FROM (SELECT device_key, collection_time, epoch(database_time) - epoch(collection_time) AS diff
            FROM read_parquet('{obs}') WHERE collection_time IS NOT NULL AND database_time IS NOT NULL))
GROUP BY 1, 2 ORDER BY 1, 2
"""


def measure_device_days(con, obs_path: Path, cc: dict) -> list[dict]:
    rows = con.execute(DEVICE_DAY_SQL.format(window=cc["consistency"]["window_seconds"], obs=obs_path)).fetchall()
    return [{"device_key": r[0], "day": r[1], "n_obs": int(r[2]), "median_diff": float(r[3]),
             "consistent_share": float(r[4])} for r in rows]
