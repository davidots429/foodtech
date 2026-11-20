"""Per-probe evidence for the synthetic 60-second mackerel safety check."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal


def summarize(probe: dict, product: dict, started: datetime) -> dict:
    readings = probe["readings"]
    valid_calibration = probe["calibration_status"] == "OK"
    complete = (valid_calibration and len(readings) == 60 and
                [row["second"] for row in readings] == list(range(60)) and
                all(row["celsius"] is not None for row in readings))
    values = [row["celsius"] for row in readings if row["celsius"] is not None]
    minimum = min(values) if values else None
    low = valid_calibration and minimum is not None and minimum < Decimal("75")
    longest = run = 0
    previous = None
    if valid_calibration:
        for row in readings:
            second, value = row["second"], row["celsius"]
            if value is not None and value >= Decimal("75"):
                run = run + 1 if previous is not None and second == previous + 1 else 1
                longest = max(longest, run)
            else:
                run = 0
            previous = second
    iso = lambda value: value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return {"probe_id": probe["probe_id"], "product_id": probe["product_id"],
            "rack_id": product["rack_id"], "shelf": product["shelf"], "pan_id": product["pan_id"],
            "position": product.get("position"), "mass_kg": product["mass_kg"],
            "thickness_cm": product["thickness_cm"], "calibration_status": probe["calibration_status"],
            "measurement_started_at": iso(started), "measurement_window_end_at": iso(started + timedelta(seconds=60)),
            "sample_count": len(readings), "observed_min_celsius": str(minimum) if minimum is not None else None,
            "full_interval_min_celsius": str(minimum) if complete else None,
            "longest_75c_hold_seconds": longest if valid_calibration else None,
            "status": "FAILED" if low else "PASSED" if complete else "ASSESSMENT_INCOMPLETE"}
