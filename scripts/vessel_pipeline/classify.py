"""Device class from derived speed evidence (thresholds from FollowMe_pipeline/boat_types.py, in config)."""
from __future__ import annotations


def classify_device(ev: dict, cfg: dict) -> tuple[str, str, str]:
    """ev: moving_segments, p90_knots, max_run_path_m, days_with_run, moving_share_off_land.
    Returns (class, confidence, reason). Classes are review hints only; they never filter rows."""
    c = cfg["classification"]
    if ev["moving_segments"] == 0 or ev["max_run_path_m"] < c["min_run_path_m"]:
        return "unclassified", "low", "no_sustained_movement"
    if ev["moving_share_off_land"] < c["min_moving_share_off_land"]:
        return "unclassified", "low", "mostly_land_movement"
    p90 = ev["p90_knots"]
    cls = "fast_vessel" if p90 >= c["fast_p90_knots_min"] else "slow_vessel" if p90 < c["slow_p90_knots_max"] else "mixed"
    conf = "high" if ev["days_with_run"] >= c["high_confidence_min_days_with_run"] else "low"
    return cls, conf, "p90_speed_band"
