"""Read-only synthetic Jeyuk Mock sample contract for local collection checks."""

import json
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class JeyukMockSample:
    fixture_id: str
    profile: str
    actual_load_kg: Decimal
    second: int
    surface_celsius: Decimal
    actual_rpm: Decimal
    target_rpm: Decimal
    motor_load_percent: Decimal


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _decimal(record: dict, name: str, *, positive: bool = False) -> Decimal:
    value = record[name]
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise ValueError(f"{name} must be numeric")
    number = Decimal(value)
    if not number.is_finite() or number < 0 or (positive and number == 0):
        raise ValueError(f"{name} is outside the allowed range")
    return number


def decode_jeyuk_mock_sample(raw: str | bytes) -> JeyukMockSample:
    record = json.loads(raw, parse_float=Decimal, object_pairs_hook=_unique_pairs)
    required = {
        "fixtureId", "provenance", "profileId", "instanceId", "profile",
        "actualLoadKg", "second", "sampleIntervalSeconds", "surfaceCelsius",
        "actualRpm", "targetRpm", "motorLoadPercent",
    }
    if not isinstance(record, dict) or set(record) != required:
        raise ValueError("unexpected Jeyuk Mock sample fields")
    if (record["provenance"] != "SYNTHETIC" or record["profileId"] != "mockprofile-110"
            or record["instanceId"] != "mockprofile-110-a-001"
            or record["profile"] not in ("SMALL", "MEDIUM", "LARGE")
            or not isinstance(record["fixtureId"], str) or not record["fixtureId"]):
        raise ValueError("unexpected Jeyuk Mock source")
    if (type(record["second"]) is not int or record["second"] < 1
            or type(record["sampleIntervalSeconds"]) is not int
            or record["sampleIntervalSeconds"] != 1):
        raise ValueError("a consecutive one-second sample is required")
    return JeyukMockSample(
        record["fixtureId"], record["profile"], _decimal(record, "actualLoadKg", positive=True),
        record["second"], _decimal(record, "surfaceCelsius"),
        _decimal(record, "actualRpm"), _decimal(record, "targetRpm"),
        _decimal(record, "motorLoadPercent"),
    )
