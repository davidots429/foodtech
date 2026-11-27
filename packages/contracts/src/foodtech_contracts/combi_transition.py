"""Synthetic steam-to-dry transition assessment on consecutive one-second samples."""

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class TransitionSample:
    second: int
    chamber_celsius: Decimal
    climate_setpoint_percent: Decimal
    door_closed: bool
    critical_alarm: bool
    humidity_percent: Decimal | None
    vent_event: str | None = None


def assess_transition(samples: list[TransitionSample], *, humidity_supported: bool,
                      vent_supported: bool, minimum_seconds: int = 30) -> dict:
    if type(minimum_seconds) is not int or not 30 <= minimum_seconds <= 300:
        raise ValueError("transition target requires 30 to 300 seconds")
    if not samples or len(samples) > 300:
        raise ValueError("transition requires 1 to 300 consecutive samples")
    stable = 0
    vent_state = "NOT_OPENED" if vent_supported else "NOT_SUPPORTED"
    ready_at = None
    for expected, sample in enumerate(samples, 1):
        if type(sample.second) is not int or sample.second != expected:
            raise ValueError("transition samples must start at one and be consecutive")
        if (not isinstance(sample.chamber_celsius, Decimal) or not sample.chamber_celsius.is_finite() or
                not Decimal("-50") <= sample.chamber_celsius <= Decimal("350") or
                not isinstance(sample.climate_setpoint_percent, Decimal) or
                not sample.climate_setpoint_percent.is_finite() or
                not Decimal("0") <= sample.climate_setpoint_percent <= Decimal("100") or
                type(sample.door_closed) is not bool or type(sample.critical_alarm) is not bool):
            raise ValueError("invalid transition observation")
        if humidity_supported:
            if (not isinstance(sample.humidity_percent, Decimal) or not sample.humidity_percent.is_finite() or
                    not Decimal("0") <= sample.humidity_percent <= Decimal("100")):
                raise ValueError("measured humidity is required for every supported sample")
        elif sample.humidity_percent is not None:
            raise ValueError("unsupported humidity must not be supplied")
        if sample.vent_event not in (None, "OPENED", "CLOSED") or (sample.vent_event and not vent_supported):
            raise ValueError("invalid vent event")
        if sample.vent_event == "OPENED":
            if vent_state != "NOT_OPENED":
                raise ValueError("vent must open once before closing")
            vent_state = "OPENED"
        elif sample.vent_event == "CLOSED":
            if vent_state != "OPENED":
                raise ValueError("vent must open before closing")
            vent_state = "CLOSED"
        valid = (Decimal("215") <= sample.chamber_celsius <= Decimal("225") and
                 sample.climate_setpoint_percent == 10 and sample.door_closed and not sample.critical_alarm and
                 (not humidity_supported or sample.humidity_percent <= 15))
        stable = stable + 1 if valid else 0
        if stable >= 30 and sample.second >= minimum_seconds and (humidity_supported or sample.second >= 180) and \
                (humidity_supported or not vent_supported or vent_state == "CLOSED"):
            ready_at = sample.second
            if expected != len(samples):
                raise ValueError("samples after transition readiness require a separate monitoring flow")
    status = "DRY_HEAT_READY" if ready_at is not None else "MODE_TRANSITION_TIMEOUT" if len(samples) == 300 else "IN_PROGRESS"
    basis = ("MEASURED_HUMIDITY" if humidity_supported else
             "TIME_TEMPERATURE_AND_VENT_EVENT" if vent_supported else "TIME_AND_TEMPERATURE")
    return {"status": status, "ready_at_second": ready_at, "stable_seconds": stable,
            "humidity_verification_status": "MEASURED" if humidity_supported else "NOT_SUPPORTED",
            "dryness_verification_basis": basis, "vent_status": vent_state,
            "mackerel_load_allowed": status == "DRY_HEAT_READY"}
