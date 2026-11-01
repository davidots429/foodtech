"""The first vendor-a REST/JSON contract; no manufacturer compatibility claim."""

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from importlib.resources import files

import jsonschema
import rfc8785

SCHEMA_VERSION = "1.0"
PROFILE_ID = "mockprofile-100"
INSTANCE_ID = "mockprofile-100-a-001"
SCHEMA = json.loads(files("foodtech_contracts").joinpath("mockprofile-100-a-v1.schema.json").read_text())
VALIDATOR = jsonschema.Draft202012Validator(SCHEMA, format_checker=jsonschema.FormatChecker())


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def decode_record(raw: str | bytes) -> dict:
    """Preserve decimal precision and reject ambiguous duplicate JSON keys."""
    record = json.loads(raw, parse_float=Decimal, object_pairs_hook=_unique_pairs)
    if not isinstance(record, dict):
        raise ValueError("record must be a JSON object")
    VALIDATOR.validate(record)
    return record


def _decimal(value: Decimal | int) -> str:
    number = Decimal(value)
    if not number.is_finite():
        raise ValueError("non-finite number")
    rendered = format(number.normalize(), "f")
    return "0" if Decimal(rendered) == 0 else rendered


def _utc(value: str) -> str:
    point = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if point.tzinfo is None:
        raise ValueError("eventTime must have a timezone")
    utc = point.astimezone(timezone.utc)
    return utc.isoformat(timespec="microseconds" if utc.microsecond else "seconds").replace("+00:00", "Z")


def _extension(value: object) -> object:
    if isinstance(value, dict):
        return {key: _extension(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_extension(item) for item in value]
    if isinstance(value, (Decimal, int)) and not isinstance(value, bool):
        return _decimal(value)
    return value


def canonical_record(record: dict) -> dict:
    """Schema-specific normalization before RFC 8785 serialization."""
    VALIDATOR.validate(record)
    measurements = record["measurements"]
    return {
        "schema_version": SCHEMA_VERSION,
        "source_id": record["sourceId"],
        "device_id": record["deviceId"],
        "batch_id": record["batchId"],
        "record_id": record["recordId"],
        "sequence_number": record["sequenceNumber"],
        "event_time": _utc(record["eventTime"]),
        "device_status": record["deviceStatus"],
        "temperature_celsius": _decimal(measurements["temperatureCelsius"]),
        "speed_rpm": _decimal(measurements["speedRpm"]),
        "elapsed_seconds": _decimal(measurements["elapsedSeconds"]),
        "error_reason": record.get("errorReason"),
        "extensions": _extension(record.get("extensions", {})),
    }


def payload_hash(record: dict) -> str:
    return hashlib.sha256(rfc8785.dumps(canonical_record(record))).hexdigest()


def _component(value: str) -> str:
    return f"{len(value.encode('utf-8'))}:{value}"


def dedup_key(record: dict) -> str:
    return "|".join(_component(record[key]) for key in ("sourceId", "schemaVersion", "recordId"))


def stream_binding_key(source_id: str, device_id: str, batch_id: str) -> str:
    return "STREAM#" + "|".join(_component(value) for value in (source_id, device_id, batch_id))


def stream_key(record: dict) -> str:
    return stream_binding_key(record["sourceId"], record["deviceId"], record["batchId"])
