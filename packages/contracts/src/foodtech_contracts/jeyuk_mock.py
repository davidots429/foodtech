"""Read-only synthetic Jeyuk Mock sample contract for local collection checks."""

import json
import re
from dataclasses import dataclass
from datetime import datetime
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
    mixture_celsius: Decimal | None
    calibration_status: str | None
    safety_sensor_position: str | None
    last_ingredient_addition_completed: bool | None
    validation_identity: dict[str, str] | None
    observed_at: datetime | None


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
    safety_fields = {"mixtureCelsius", "calibrationStatus", "safetySensorPosition",
                     "lastIngredientAdditionCompleted"}
    identity_fields = {"ingredientSpecId", "ingredientLotId", "supplierId", "sensorId",
                       "sensorCalibrationRecordId", "processConditionProfileId", "processConditionVersion"}
    if not isinstance(record, dict) or (set(record) - {"observedAt"}) not in (
            required, required | safety_fields, required | safety_fields | {"validationIdentity"}):
        raise ValueError("unexpected Jeyuk Mock sample fields")
    observed = record.get("observedAt")
    if observed is not None:
        try:
            if not isinstance(observed, str):
                raise ValueError("timestamp must be a string")
            fraction = re.search(r"\.(\d+)(?:Z|[+-]\d\d:\d\d)$", observed)
            if fraction and any(digit != "0" for digit in fraction.group(1)[6:]):
                raise ValueError("timestamp precision exceeds microseconds")
            observed = datetime.fromisoformat(observed.replace("Z", "+00:00"))
            if observed.utcoffset() is None:
                raise ValueError("timestamp requires an offset")
        except (ValueError, OverflowError) as exc:
            raise ValueError("invalid synthetic observation timestamp") from exc
    elif "observedAt" in record:
        raise ValueError("observation timestamp cannot be null")
    if (record["provenance"] != "SYNTHETIC" or record["profileId"] != "mockprofile-110"
            or record["instanceId"] != "mockprofile-110-a-001"
            or record["profile"] not in ("SMALL", "MEDIUM", "LARGE")
            or not isinstance(record["fixtureId"], str)
            or re.fullmatch(r"[A-Za-z0-9-]{1,80}", record["fixtureId"]) is None):
        raise ValueError("unexpected Jeyuk Mock source")
    if (type(record["second"]) is not int or record["second"] < 1
            or type(record["sampleIntervalSeconds"]) is not int
            or record["sampleIntervalSeconds"] != 1):
        raise ValueError("a consecutive one-second sample is required")
    has_safety = safety_fields <= set(record)
    if has_safety and (record["calibrationStatus"] not in ("OK", "EXPIRED")
                       or record["safetySensorPosition"] != "LOWEST_MIXTURE_POINT"
                       or type(record["lastIngredientAdditionCompleted"]) is not bool):
        raise ValueError("unexpected Jeyuk safety sensor contract")
    identity = record.get("validationIdentity")
    if identity is not None and (not isinstance(identity, dict) or set(identity) != identity_fields or
            any(not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,79}", value) is None
                for value in identity.values())):
        raise ValueError("unexpected Jeyuk validation identity")
    return JeyukMockSample(
        record["fixtureId"], record["profile"], _decimal(record, "actualLoadKg", positive=True),
        record["second"], _decimal(record, "surfaceCelsius"),
        _decimal(record, "actualRpm"), _decimal(record, "targetRpm"),
        _decimal(record, "motorLoadPercent"),
        _decimal(record, "mixtureCelsius") if has_safety else None,
        record["calibrationStatus"] if has_safety else None,
        record["safetySensorPosition"] if has_safety else None,
        record["lastIngredientAdditionCompleted"] if has_safety else None,
        identity, observed,
    )
