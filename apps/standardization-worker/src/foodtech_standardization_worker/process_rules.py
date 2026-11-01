"""First approved synthetic fryer temperature rules over ordered, valid samples."""

from datetime import datetime
from decimal import Decimal

RULE_VERSION = "tonkatsu-temperature-v1"
SAMPLE_INTERVAL_SECONDS = 5


def _seconds(start: str | None, end: str) -> int:
    if not start:
        return 0
    return int((datetime.fromisoformat(end.replace("Z", "+00:00")) -
                datetime.fromisoformat(start.replace("Z", "+00:00"))).total_seconds())


def advance(previous: dict | None, normalized: dict, *, ingestion_event_id: str,
            quality_status: str, finding_codes: set[str]) -> dict | None:
    """Return a new assessment state; late, duplicate or quarantined input has no effect."""
    if quality_status == "QUARANTINED" or not normalized or "LATE_AFTER_GAP" in finding_codes:
        return None
    at = normalized["event_time"]
    before = previous or {}
    last_at = before.get("last_event_time")
    if last_at and _seconds(last_at, at) <= 0:
        return None
    temperature = Decimal(normalized["CHAMBER_TEMPERATURE"]["value"])
    gap = "DATA_GAP" in finding_codes or "LATE_AFTER_GAP" in finding_codes or \
        bool(last_at and _seconds(last_at, at) > SAMPLE_INTERVAL_SECONDS)
    low = temperature < 160
    high = temperature > 180
    below_normal = temperature < 165
    previous_low = before.get("low_since") if not gap else None
    previous_high = before.get("high_since") if not gap else None
    previous_recovery = before.get("recovery_since") if not gap else None
    low_since = (previous_low or at) if low else None
    high_since = (previous_high or at) if high else None
    recovery_since = (previous_recovery or at) if below_normal else None
    low_accumulated = int(before.get("low_accumulated_seconds", 0))
    if low and previous_low and last_at and not gap:
        low_accumulated += _seconds(last_at, at)
    if not low and temperature >= 165 and previous_recovery and _seconds(previous_recovery, at) > 90:
        recovery_failed = True
    else:
        recovery_failed = bool(recovery_since and _seconds(recovery_since, at) > 90)
    low_failed = (low and (_seconds(low_since, at) >= 20 or low_accumulated >= 90)) or recovery_failed
    high_failed = high and _seconds(high_since, at) >= 20
    if low_failed:
        temperature_status = "LOW_PROCESS_FAILURE"
    elif high_failed:
        temperature_status = "HIGH_PROCESS_FAILURE"
    elif temperature < 165:
        temperature_status = "LOW_WARNING"
    elif temperature > 175:
        temperature_status = "HIGH_WARNING"
    else:
        temperature_status = "NORMAL"
    failed = before.get("status") == "FAILED" or low_failed or high_failed
    status = "FAILED" if failed else "WARNING" if temperature_status != "NORMAL" or gap else "NORMAL"
    return {
        "rule_version": RULE_VERSION, "status": status, "temperature_status": temperature_status,
        "last_event_time": at, "last_ingestion_event_id": ingestion_event_id,
        "last_temperature_celsius": str(temperature), "low_since": low_since,
        "high_since": high_since, "recovery_since": recovery_since,
        "low_accumulated_seconds": low_accumulated, "data_gap": gap,
        "failure_reason": before.get("failure_reason") or (
            "LOW_TEMPERATURE_DURATION" if low_failed else "HIGH_TEMPERATURE_DURATION" if high_failed else None),
    }
