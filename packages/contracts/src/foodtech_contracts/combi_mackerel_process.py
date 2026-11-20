"""Synthetic dry-heat recovery and debounced door assessment."""

from dataclasses import dataclass
from decimal import Decimal
from .combi_fan import observe as observe_fan


@dataclass(frozen=True)
class Sample:
    second: int
    chamber_celsius: Decimal
    door: str
    fan_level: int | None = None


def assess(samples: list[Sample], duration: int, time_status: str) -> dict:
    if type(duration) is not int or not 1 <= duration <= 900 or len(samples) != duration:
        raise ValueError("one sample per second for the entire cooking duration is required")
    for number, sample in enumerate(samples, 1):
        if (type(sample.second) is not int or sample.second != number or
                not isinstance(sample.chamber_celsius, Decimal) or not sample.chamber_celsius.is_finite() or
                not Decimal("-50") <= sample.chamber_celsius <= Decimal("350") or
                sample.door not in ("OPEN", "CLOSED")):
            raise ValueError("invalid or nonconsecutive cooking sample")
    if len(samples) < 2 or any(sample.door != "CLOSED" for sample in samples[:2]):
        raise ValueError("the initial loading door close must be confirmed for two seconds")
    # Loading close is observed at second 1 and confirmed at second 2.
    stable = 0
    recovered = None
    low = 0
    failures = set()
    for sample in samples[1:]:
        if recovered is None:
            stable = stable + 1 if Decimal("215") <= sample.chamber_celsius <= Decimal("225") else 0
            if stable >= 30 and sample.second <= 122:
                recovered = sample.second
            elif sample.second >= 122:
                failures.add("TEMPERATURE_RECOVERY_FAILURE")
        else:
            low = low + 1 if sample.chamber_celsius < 200 else 0
            if low >= 30:
                failures.add("LOW_TEMPERATURE_PROCESS_FAILURE")

    confirmed = "CLOSED"
    candidate = None
    candidate_at = None
    open_start = None
    open_durations = []
    changes = []
    for sample in samples[2:]:
        if sample.door == confirmed:
            candidate = candidate_at = None
        elif candidate == sample.door:
            confirmed = sample.door
            changes.append({"state": confirmed, "observed_second": candidate_at,
                            "confirmed_second": sample.second})
            if confirmed == "OPEN":
                open_start = candidate_at
            else:
                open_durations.append(candidate_at - open_start)
                open_start = None
            candidate = candidate_at = None
        else:
            candidate, candidate_at = sample.door, sample.second
    if open_start is not None:
        open_durations.append(duration + 1 - open_start)
    total_open = sum(open_durations)
    max_open = max(open_durations, default=0)
    if total_open >= 30 or max_open >= 15:
        failures.add("DOOR_OPEN_FAILURE")
    if time_status in ("EARLY_TERMINATION", "MAX_TIME_EXCEEDED"):
        failures.add(time_status)
    warnings = []
    if 10 <= total_open < 30 and max_open < 15:
        warnings.append("DOOR_OPEN_WARNING")
    if time_status in ("SHORT_WARNING", "DELAY_WARNING"):
        warnings.append(time_status)
    incomplete = recovered is None and "TEMPERATURE_RECOVERY_FAILURE" not in failures or candidate is not None
    status = "FAILED" if failures else "INSUFFICIENT_DATA" if incomplete else "WARNING" if warnings else "NORMAL"
    return {"status": status, "reason_codes": sorted(failures | set(warnings)),
            "recovered_at_second": recovered, "recovery_clock_started_second": 2,
            "door_open_seconds": total_open, "door_max_open_seconds": max_open,
            "door_changes": changes, "door_final_state_pending": candidate is not None,
            "sample_count": len(samples), "fan_observation": observe_fan([sample.fan_level for sample in samples], 3)}
