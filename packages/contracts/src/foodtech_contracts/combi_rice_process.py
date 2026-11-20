"""Synthetic rice steam temperature and humidity assessment."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal
from .combi_door import assess as assess_door


@dataclass(frozen=True)
class Sample:
    second: int
    chamber_celsius: Decimal
    humidity_percent: Decimal | None
    door: str | None = None


def assess(samples: list[Sample], duration: int, time_status: str,
           humidity_source: Literal["MEASURED", "CONTROLLER_ESTIMATE", "NOT_SUPPORTED"]) -> dict:
    if type(duration) is not int or not 1 <= duration <= 2000 or len(samples) != duration:
        raise ValueError("one sample per second for the entire cooking duration is required")
    if humidity_source not in ("MEASURED", "CONTROLLER_ESTIMATE", "NOT_SUPPORTED"):
        raise ValueError("invalid humidity source")
    doors = [sample.door for sample in samples]
    if all(door is None for door in doors):
        door = {"status": "NOT_MEASURED"}
    elif any(door is None for door in doors):
        raise ValueError("door observations must cover the complete interval")
    else:
        door = assess_door(doors)
    low_run = high_run = low_total = high_total = 0
    humidity_warning_run = humidity_failure_run = 0
    failures, warnings = set(), set()
    suspects = []
    prior = None
    transient_humidity = False
    for number, sample in enumerate(samples, 1):
        temp = sample.chamber_celsius
        humidity = sample.humidity_percent
        if (type(sample.second) is not int or sample.second != number or not isinstance(temp, Decimal) or
                not temp.is_finite() or not Decimal("-50") <= temp <= Decimal("350")):
            raise ValueError("invalid or nonconsecutive rice temperature sample")
        if humidity_source == "NOT_SUPPORTED":
            if humidity is not None:
                raise ValueError("humidity must be absent when unsupported")
        elif (not isinstance(humidity, Decimal) or not humidity.is_finite() or
              not Decimal("0") <= humidity <= Decimal("100")):
            raise ValueError("valid humidity is required for supported sensors")
        low_run = low_run + 1 if temp < 95 else 0
        high_run = high_run + 1 if temp > 105 else 0
        low_total += temp < 95
        high_total += temp > 105
        if temp < 98:
            warnings.add("LOW_TEMPERATURE_WARNING")
        elif temp > 102:
            warnings.add("HIGH_TEMPERATURE_WARNING")
        if low_run >= 60 or low_total >= 180:
            failures.add("LOW_TEMPERATURE_PROCESS_FAILURE")
        if high_run >= 30 or high_total >= 90:
            failures.add("HIGH_TEMPERATURE_PROCESS_FAILURE")
        if prior is not None and prior <= 105 and temp > 105 and abs(temp - prior) >= 5:
            suspects.append(number)
        prior = temp
        if humidity is not None:
            humidity_warning_run = humidity_warning_run + 1 if 90 <= humidity <= 95 else 0
            humidity_failure_run = humidity_failure_run + 1 if humidity < 90 else 0
            transient_humidity |= humidity <= 95
            if humidity_warning_run >= 60:
                warnings.add("LOW_HUMIDITY_WARNING")
            if humidity_failure_run >= 120:
                failures.add("STEAM_SUPPLY_LOST")
    if transient_humidity and not ({"LOW_HUMIDITY_WARNING", "STEAM_SUPPLY_LOST"} & (warnings | failures)):
        warnings.add("TRANSIENT_DEVIATION")
    if time_status in ("EARLY_TERMINATION", "MAX_TIME_EXCEEDED"):
        failures.add(time_status)
    elif time_status in ("SHORT_WARNING", "DELAY_WARNING"):
        warnings.add(time_status)
    elif time_status != "NORMAL":
        raise ValueError("invalid rice time status")
    if suspects:
        warnings.add("SENSOR_ANOMALY_SUSPECTED")
    if door.get("reason_code") == "DOOR_OPEN_FAILURE":
        failures.add("DOOR_OPEN_FAILURE")
    elif door.get("reason_code") == "DOOR_OPEN_WARNING":
        warnings.add("DOOR_OPEN_WARNING")
    status = ("FAILED" if failures else "INSUFFICIENT_DATA" if humidity_source == "NOT_SUPPORTED" or
              door["status"] in ("INSUFFICIENT_DATA", "NOT_MEASURED") else
              "WARNING" if warnings else "NORMAL")
    return {"status": status, "reason_codes": sorted(failures | warnings),
            "humidity_value_source": humidity_source, "anomaly_suspected_seconds": suspects,
            "low_temperature_total_seconds": low_total, "high_temperature_total_seconds": high_total,
            "sample_count": len(samples), "door_assessment": door}
