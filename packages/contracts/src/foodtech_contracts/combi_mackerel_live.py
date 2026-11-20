"""Incremental synthetic mackerel recovery and debounced-door observations."""
from decimal import Decimal

RULE_VERSION = "synthetic-mackerel-live-v1"


def observe(previous: dict | None, sample: dict) -> dict:
    if sample.get("phase") != "MACKEREL":
        raise ValueError("mackerel sample required")
    second = sample["phaseSecond"]
    if type(second) is not int or second < 1:
        raise ValueError("invalid phase second")
    old = previous or {}
    if old and (old.get("rule_version") != RULE_VERSION or old.get("last_second") != second - 1):
        raise ValueError("mackerel monitor sequence mismatch")
    door = sample["doorState"]
    if door not in ("OPEN", "CLOSED"):
        raise ValueError("invalid door state")
    temperature = Decimal(sample["chamberCelsius"])
    complete = bool(old.get("complete_from_start", second == 1))
    initial_closed = bool(old.get("initial_closed", True))
    if second <= 2:
        initial_closed = initial_closed and door == "CLOSED"
    complete = complete and initial_closed
    stable = int(old.get("recovery_stable_seconds", 0))
    recovered = old.get("recovered_at_second")
    low = int(old.get("low_run", 0))
    failures = set(old.get("failure_codes", []))
    if complete and second >= 2:
        if recovered is None:
            stable = stable + 1 if Decimal("215") <= temperature <= Decimal("225") else 0
            if stable >= 30 and second <= 122:
                recovered = second
            elif second >= 122:
                failures.add("TEMPERATURE_RECOVERY_FAILURE")
        else:
            low = low + 1 if temperature < 200 else 0
            if low >= 30:
                failures.add("LOW_TEMPERATURE_PROCESS_FAILURE")
    confirmed = old.get("confirmed_door", "CLOSED")
    candidate = old.get("candidate_door")
    candidate_at = old.get("candidate_at")
    open_start = old.get("open_start")
    completed_total = int(old.get("completed_open_seconds", 0))
    completed_max = int(old.get("completed_max_open_seconds", 0))
    if complete and second >= 3:
        if door == confirmed:
            candidate = candidate_at = None
        elif candidate == door:
            confirmed = door
            if door == "OPEN":
                open_start = int(candidate_at)
            else:
                duration = int(candidate_at) - int(open_start)
                completed_total += duration
                completed_max = max(completed_max, duration)
                open_start = None
            candidate = candidate_at = None
        else:
            candidate, candidate_at = door, second
    current_open = 0
    if open_start is not None:
        # An unconfirmed closing may be backdated to its first observation.
        end = int(candidate_at) if candidate == "CLOSED" else second + 1
        current_open = end - int(open_start)
    total = completed_total + current_open
    maximum = max(completed_max, current_open)
    if complete and (total >= 30 or maximum >= 15):
        failures.add("DOOR_OPEN_FAILURE")
    warnings = ["DOOR_OPEN_WARNING"] if 10 <= total < 30 and maximum < 15 else []
    return {"rule_version": RULE_VERSION, "last_second": second, "complete_from_start": complete,
            "initial_closed": initial_closed, "recovery_stable_seconds": stable,
            "recovered_at_second": recovered, "low_run": low, "confirmed_door": confirmed,
            "candidate_door": candidate, "candidate_at": candidate_at, "open_start": open_start,
            "completed_open_seconds": completed_total, "completed_max_open_seconds": completed_max,
            "door_open_seconds": total, "door_max_open_seconds": maximum,
            "failure_codes": sorted(failures), "warning_codes": warnings}
