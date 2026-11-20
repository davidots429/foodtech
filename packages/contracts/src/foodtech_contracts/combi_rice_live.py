"""Incremental synthetic rice temperature/humidity monitoring, not final safety approval."""
from decimal import Decimal

RULE_VERSION = "synthetic-rice-live-v1"


def observe(previous: dict | None, sample: dict) -> dict:
    if sample.get("phase") != "RICE":
        raise ValueError("rice sample required")
    second = sample["phaseSecond"]
    if type(second) is not int or second < 1:
        raise ValueError("invalid phase second")
    old = previous or {}
    if old and (old.get("rule_version") != RULE_VERSION or old.get("last_second") != second - 1):
        raise ValueError("rice monitor sequence mismatch")
    source = sample["humidityValueSource"]
    previous_source = old.get("humidity_source", source)
    continuous = bool(old.get("complete_from_start", second == 1)) and previous_source == source
    temperature = Decimal(sample["chamberCelsius"])
    humidity = sample["measuredHumidityPercent"]
    low_run = int(old.get("low_run", 0)) + 1 if temperature < 95 else 0
    high_run = int(old.get("high_run", 0)) + 1 if temperature > 105 else 0
    low_total = int(old.get("low_total", 0)) + int(temperature < 95)
    high_total = int(old.get("high_total", 0)) + int(temperature > 105)
    warning_run = int(old.get("humidity_warning_run", 0)) if previous_source == source else 0
    failure_run = int(old.get("humidity_failure_run", 0)) if previous_source == source else 0
    if humidity is None:
        warning_run = failure_run = 0
    else:
        humidity = Decimal(humidity)
        warning_run = warning_run + 1 if 90 <= humidity <= 95 else 0
        failure_run = failure_run + 1 if humidity < 90 else 0
    failures = set(old.get("failure_codes", []))
    warnings = set(old.get("warning_codes", []))
    if low_run >= 60 or low_total >= 180: failures.add("LOW_TEMPERATURE_PROCESS_FAILURE")
    if high_run >= 30 or high_total >= 90: failures.add("HIGH_TEMPERATURE_PROCESS_FAILURE")
    if warning_run >= 60: warnings.add("LOW_HUMIDITY_WARNING")
    if failure_run >= 120: failures.add("STEAM_SUPPLY_LOST")
    if temperature < 98: warnings.add("LOW_TEMPERATURE_WARNING")
    if temperature > 102: warnings.add("HIGH_TEMPERATURE_WARNING")
    prior = old.get("previous_temperature")
    if prior is not None and Decimal(prior) <= 105 < temperature and abs(temperature - Decimal(prior)) >= 5:
        warnings.add("SENSOR_ANOMALY_SUSPECTED")
    return {"rule_version": RULE_VERSION, "last_second": second, "complete_from_start": continuous,
            "humidity_source": source, "previous_temperature": str(temperature),
            "low_run": low_run, "high_run": high_run, "low_total": low_total, "high_total": high_total,
            "humidity_warning_run": warning_run, "humidity_failure_run": failure_run,
            "failure_codes": sorted(failures), "warning_codes": sorted(warnings)}
