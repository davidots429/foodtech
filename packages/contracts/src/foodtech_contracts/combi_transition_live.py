"""Partial synthetic transition observation; never grants equipment loading permission."""
from decimal import Decimal

RULE_VERSION = "synthetic-transition-live-v1"


def observe(previous: dict | None, sample: dict) -> dict:
    if sample.get("phase") != "TRANSITION":
        raise ValueError("transition sample required")
    second = sample["phaseSecond"]
    if type(second) is not int or second < 1:
        raise ValueError("invalid phase second")
    old = previous or {}
    if old and (old.get("rule_version") != RULE_VERSION or old.get("last_second") != second - 1):
        raise ValueError("transition monitor sequence mismatch")
    source = sample["humidityValueSource"]
    is_v2 = sample["schemaVersion"] == "synthetic-combi-v2"
    vent_supported = sample.get("ventSupported") if is_v2 else None
    capable = is_v2 and source in ("MEASURED", "NOT_SUPPORTED") and type(vent_supported) is bool
    same_capability = not old or (old.get("humidity_source") == source and old.get("vent_supported") == vent_supported)
    complete = bool(old.get("complete_from_start", second == 1)) and capable and same_capability
    vent = old.get("vent_status", "UNKNOWN" if vent_supported is None else "NOT_OPENED" if vent_supported else "NOT_SUPPORTED")
    if old and old.get("vent_supported") != vent_supported:
        vent = "UNKNOWN"
    vent_event = sample.get("ventEvent") if is_v2 else None
    if vent_event == "OPENED":
        if vent != "NOT_OPENED": complete = False
        else: vent = "OPENED"
    elif vent_event == "CLOSED":
        if vent != "OPENED": complete = False
        else: vent = "CLOSED"
    valid = (complete and Decimal("215") <= Decimal(sample["chamberCelsius"]) <= Decimal("225") and
             Decimal(sample["climateSetpointPercent"]) == 10 and sample["doorState"] == "CLOSED" and
             not sample["criticalAlarm"] and
             (source == "NOT_SUPPORTED" or Decimal(sample["measuredHumidityPercent"]) <= 15))
    stable = int(old.get("stable_seconds", 0)) + 1 if valid else 0
    ready_at = old.get("ready_observed_at_second")
    timeout = bool(old.get("timeout", False))
    vent_ready = source == "MEASURED" or not vent_supported or vent == "CLOSED"
    ready_current = bool(valid and stable >= 30 and (source == "MEASURED" or second >= 180) and vent_ready and not timeout)
    if ready_current and ready_at is None:
        ready_at = second
    if complete and second >= 300 and ready_at is None:
        timeout = True
        ready_current = False
    return {"rule_version": RULE_VERSION, "last_second": second, "complete_from_start": complete,
            "humidity_source": source, "vent_supported": vent_supported, "vent_status": vent,
            "stable_seconds": stable, "ready_observed_at_second": ready_at,
            "ready_conditions_current": ready_current, "timeout": timeout}
