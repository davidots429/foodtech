"""The first vendor-a REST/JSON contract; no manufacturer compatibility claim."""

import hashlib
import json
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from importlib.resources import files

import jsonschema
import rfc8785

SCHEMA_VERSION = "1.0"
PROFILE_ID = "mockprofile-100"
INSTANCE_ID = "mockprofile-100-a-001"
SCHEMA = json.loads(files("foodtech_contracts").joinpath("mockprofile-100-a-v1.schema.json").read_text())
VALIDATOR = jsonschema.Draft202012Validator(SCHEMA, format_checker=jsonschema.FormatChecker())
MQTT_SCHEMA = json.loads(files("foodtech_contracts").joinpath("mockprofile-300-a-v1.schema.json").read_text())
MQTT_VALIDATOR = jsonschema.Draft202012Validator(MQTT_SCHEMA, format_checker=jsonschema.FormatChecker())
UA_SCHEMA = json.loads(files("foodtech_contracts").joinpath("mockprofile-203-a-v1.schema.json").read_text())
UA_VALIDATOR = jsonschema.Draft202012Validator(UA_SCHEMA, format_checker=jsonschema.FormatChecker())


UA_JSON_SCHEMA = json.loads(files("foodtech_contracts").joinpath("mockprofile-200-a-v1.schema.json").read_text())
UA_JSON_VALIDATOR = jsonschema.Draft202012Validator(UA_JSON_SCHEMA, format_checker=jsonschema.FormatChecker())


VENDOR_B_SCHEMA = json.loads(files("foodtech_contracts").joinpath("mockprofile-100-b-v1.schema.json").read_text())
VENDOR_B_VALIDATOR = jsonschema.Draft202012Validator(VENDOR_B_SCHEMA, format_checker=jsonschema.FormatChecker())
NORMALIZED_B_SCHEMA = json.loads(json.dumps(SCHEMA))
NORMALIZED_B_SCHEMA["properties"]["deviceId"] = {"const": "mockprofile-100-b-001"}
NORMALIZED_B_SCHEMA["properties"]["batchId"]["pattern"] = "^batch-b-[A-Za-z0-9-]+$"
NORMALIZED_B_SCHEMA["properties"]["recordId"]["pattern"] = "^rec-b-[A-Za-z0-9-]+$"
NORMALIZED_B_SCHEMA["properties"]["measurements"]["required"] = ["temperatureCelsius", "outputPercent", "elapsedSeconds"]
NORMALIZED_B_SCHEMA["properties"]["measurements"]["properties"]["elapsedSeconds"] = {"type": "number", "minimum": 0}
NORMALIZED_B_SCHEMA["properties"]["measurements"]["properties"]["outputPercent"] = {"type": "number", "minimum": 0, "maximum": 100}
NORMALIZED_B_VALIDATOR = jsonschema.Draft202012Validator(NORMALIZED_B_SCHEMA, format_checker=jsonschema.FormatChecker())


VENDOR_C_SCHEMA = json.loads(files("foodtech_contracts").joinpath("mockprofile-100-c-v1.schema.json").read_text())
VENDOR_C_VALIDATOR = jsonschema.Draft202012Validator(VENDOR_C_SCHEMA, format_checker=jsonschema.FormatChecker())
NORMALIZED_C_SCHEMA = json.loads(json.dumps(SCHEMA))
NORMALIZED_C_SCHEMA["properties"]["deviceId"] = {"const": "mockprofile-100-c-001"}
NORMALIZED_C_SCHEMA["properties"]["batchId"]["pattern"] = "^batch-c-[A-Za-z0-9-]+$"
NORMALIZED_C_SCHEMA["properties"]["recordId"]["pattern"] = "^rec-c-[A-Za-z0-9-]+$"
NORMALIZED_C_SCHEMA["properties"]["measurements"]["required"] = ["temperatureCelsius", "speedLevel", "elapsedSeconds"]
NORMALIZED_C_SCHEMA["properties"]["measurements"]["properties"]["elapsedSeconds"] = {"type": "number", "minimum": 0}
NORMALIZED_C_SCHEMA["properties"]["measurements"]["properties"]["speedLevel"] = {"type": "integer", "minimum": 0, "maximum": 10}
NORMALIZED_C_VALIDATOR = jsonschema.Draft202012Validator(NORMALIZED_C_SCHEMA, format_checker=jsonschema.FormatChecker())


MQTT_B_SCHEMA = json.loads(files("foodtech_contracts").joinpath("mockprofile-300-b-v1.schema.json").read_text())
MQTT_B_VALIDATOR = jsonschema.Draft202012Validator(MQTT_B_SCHEMA, format_checker=jsonschema.FormatChecker())
MQTT_C_SCHEMA = json.loads(files("foodtech_contracts").joinpath("mockprofile-300-c-v1.schema.json").read_text())
MQTT_C_VALIDATOR = jsonschema.Draft202012Validator(MQTT_C_SCHEMA, format_checker=jsonschema.FormatChecker())
NORMALIZED_MQTT_B = json.loads(json.dumps(NORMALIZED_B_SCHEMA))
NORMALIZED_MQTT_B["properties"]["deviceId"] = {"const": "mockprofile-300-b-001"}
NORMALIZED_MQTT_B_VALIDATOR = jsonschema.Draft202012Validator(NORMALIZED_MQTT_B, format_checker=jsonschema.FormatChecker())
NORMALIZED_MQTT_C = json.loads(json.dumps(NORMALIZED_C_SCHEMA))
NORMALIZED_MQTT_C["properties"]["deviceId"] = {"const": "mockprofile-300-c-001"}
NORMALIZED_MQTT_C_VALIDATOR = jsonschema.Draft202012Validator(NORMALIZED_MQTT_C, format_checker=jsonschema.FormatChecker())


UA_JSON_B_SCHEMA = json.loads(files("foodtech_contracts").joinpath("mockprofile-200-b-v1.schema.json").read_text())
UA_JSON_B_VALIDATOR = jsonschema.Draft202012Validator(UA_JSON_B_SCHEMA, format_checker=jsonschema.FormatChecker())
UA_JSON_C_SCHEMA = json.loads(files("foodtech_contracts").joinpath("mockprofile-200-c-v1.schema.json").read_text())
UA_JSON_C_VALIDATOR = jsonschema.Draft202012Validator(UA_JSON_C_SCHEMA, format_checker=jsonschema.FormatChecker())
NORMALIZED_UA_JSON_B = json.loads(json.dumps(NORMALIZED_B_SCHEMA))
NORMALIZED_UA_JSON_B["properties"]["deviceId"] = {"const": "mockprofile-200-b-001"}
NORMALIZED_UA_JSON_B_VALIDATOR = jsonschema.Draft202012Validator(NORMALIZED_UA_JSON_B, format_checker=jsonschema.FormatChecker())
NORMALIZED_UA_JSON_C = json.loads(json.dumps(NORMALIZED_C_SCHEMA))
NORMALIZED_UA_JSON_C["properties"]["deviceId"] = {"const": "mockprofile-200-c-001"}
NORMALIZED_UA_JSON_C_VALIDATOR = jsonschema.Draft202012Validator(NORMALIZED_UA_JSON_C, format_checker=jsonschema.FormatChecker())


UA_BINARY_B_SCHEMA = json.loads(files("foodtech_contracts").joinpath("mockprofile-203-b-v1.schema.json").read_text())
UA_BINARY_B_VALIDATOR = jsonschema.Draft202012Validator(UA_BINARY_B_SCHEMA, format_checker=jsonschema.FormatChecker())
UA_BINARY_C_SCHEMA = json.loads(files("foodtech_contracts").joinpath("mockprofile-203-c-v1.schema.json").read_text())
UA_BINARY_C_VALIDATOR = jsonschema.Draft202012Validator(UA_BINARY_C_SCHEMA, format_checker=jsonschema.FormatChecker())
NORMALIZED_UA_BINARY_B = json.loads(json.dumps(NORMALIZED_B_SCHEMA))
NORMALIZED_UA_BINARY_B["properties"]["deviceId"] = {"const": "mockprofile-203-b-001"}
NORMALIZED_UA_BINARY_B_VALIDATOR = jsonschema.Draft202012Validator(NORMALIZED_UA_BINARY_B, format_checker=jsonschema.FormatChecker())
NORMALIZED_UA_BINARY_C = json.loads(json.dumps(NORMALIZED_C_SCHEMA))
NORMALIZED_UA_BINARY_C["properties"]["deviceId"] = {"const": "mockprofile-203-c-001"}
NORMALIZED_UA_BINARY_C_VALIDATOR = jsonschema.Draft202012Validator(NORMALIZED_UA_BINARY_C, format_checker=jsonschema.FormatChecker())


NORMALIZED_XML_A = json.loads(json.dumps(SCHEMA))
NORMALIZED_XML_A["properties"]["deviceId"] = {"const": "mockprofile-101-a-001"}
NORMALIZED_XML_A_VALIDATOR = jsonschema.Draft202012Validator(NORMALIZED_XML_A, format_checker=jsonschema.FormatChecker())


XML_B_MAPPED_SCHEMA = json.loads(files("foodtech_contracts").joinpath("mockprofile-101-b-mapped-v1.schema.json").read_text())
XML_B_MAPPED_VALIDATOR = jsonschema.Draft202012Validator(XML_B_MAPPED_SCHEMA, format_checker=jsonschema.FormatChecker())
XML_C_MAPPED_SCHEMA = json.loads(files("foodtech_contracts").joinpath("mockprofile-101-c-mapped-v1.schema.json").read_text())
XML_C_MAPPED_VALIDATOR = jsonschema.Draft202012Validator(XML_C_MAPPED_SCHEMA, format_checker=jsonschema.FormatChecker())
NORMALIZED_XML_B = json.loads(json.dumps(NORMALIZED_B_SCHEMA))
NORMALIZED_XML_B["properties"]["deviceId"] = {"const": "mockprofile-101-b-001"}
NORMALIZED_XML_B_VALIDATOR = jsonschema.Draft202012Validator(NORMALIZED_XML_B, format_checker=jsonschema.FormatChecker())
NORMALIZED_XML_C = json.loads(json.dumps(NORMALIZED_C_SCHEMA))
NORMALIZED_XML_C["properties"]["deviceId"] = {"const": "mockprofile-101-c-001"}
NORMALIZED_XML_C_VALIDATOR = jsonschema.Draft202012Validator(NORMALIZED_XML_C, format_checker=jsonschema.FormatChecker())


MQTT_XML_B_SCHEMA = json.loads(files("foodtech_contracts").joinpath("mockprofile-301-b-mapped-v1.schema.json").read_text())
MQTT_XML_B_VALIDATOR = jsonschema.Draft202012Validator(MQTT_XML_B_SCHEMA, format_checker=jsonschema.FormatChecker())
MQTT_XML_C_SCHEMA = json.loads(files("foodtech_contracts").joinpath("mockprofile-301-c-mapped-v1.schema.json").read_text())
MQTT_XML_C_VALIDATOR = jsonschema.Draft202012Validator(MQTT_XML_C_SCHEMA, format_checker=jsonschema.FormatChecker())
NORMALIZED_MQTT_XML_A = json.loads(json.dumps(SCHEMA))
NORMALIZED_MQTT_XML_A["properties"]["deviceId"] = {"const": "mockprofile-301-a-001"}
NORMALIZED_MQTT_XML_A_VALIDATOR = jsonschema.Draft202012Validator(NORMALIZED_MQTT_XML_A, format_checker=jsonschema.FormatChecker())
NORMALIZED_MQTT_XML_B = json.loads(json.dumps(NORMALIZED_B_SCHEMA))
NORMALIZED_MQTT_XML_B["properties"]["deviceId"] = {"const": "mockprofile-301-b-001"}
NORMALIZED_MQTT_XML_B_VALIDATOR = jsonschema.Draft202012Validator(NORMALIZED_MQTT_XML_B, format_checker=jsonschema.FormatChecker())
NORMALIZED_MQTT_XML_C = json.loads(json.dumps(NORMALIZED_C_SCHEMA))
NORMALIZED_MQTT_XML_C["properties"]["deviceId"] = {"const": "mockprofile-301-c-001"}
NORMALIZED_MQTT_XML_C_VALIDATOR = jsonschema.Draft202012Validator(NORMALIZED_MQTT_XML_C, format_checker=jsonschema.FormatChecker())


CSV_A_NORMALIZED = json.loads(json.dumps(SCHEMA))
CSV_A_NORMALIZED["properties"]["deviceId"] = {"const": "mockprofile-302-a-001"}
CSV_A_NORMALIZED_VALIDATOR = jsonschema.Draft202012Validator(CSV_A_NORMALIZED, format_checker=jsonschema.FormatChecker())
CSV_B_NORMALIZED = json.loads(json.dumps(NORMALIZED_B_SCHEMA))
CSV_B_NORMALIZED["properties"]["deviceId"] = {"const": "mockprofile-302-b-001"}
CSV_B_NORMALIZED_VALIDATOR = jsonschema.Draft202012Validator(CSV_B_NORMALIZED, format_checker=jsonschema.FormatChecker())
CSV_B_SCHEMA = json.loads(files("foodtech_contracts").joinpath("mockprofile-302-b-v1.schema.json").read_text())
CSV_B_VALIDATOR = jsonschema.Draft202012Validator(CSV_B_SCHEMA, format_checker=jsonschema.FormatChecker())
CSV_C_NORMALIZED = json.loads(json.dumps(NORMALIZED_C_SCHEMA))
CSV_C_NORMALIZED["properties"]["deviceId"] = {"const": "mockprofile-302-c-001"}
CSV_C_NORMALIZED_VALIDATOR = jsonschema.Draft202012Validator(CSV_C_NORMALIZED, format_checker=jsonschema.FormatChecker())
CSV_C_SCHEMA = json.loads(files("foodtech_contracts").joinpath("mockprofile-302-c-v1.schema.json").read_text())
CSV_C_VALIDATOR = jsonschema.Draft202012Validator(CSV_C_SCHEMA, format_checker=jsonschema.FormatChecker())

STIR_UA_JSON_SOURCE = {
    vendor: jsonschema.Draft202012Validator(
        json.loads(files("foodtech_contracts").joinpath(f"mockprofile-210-{vendor}-v1.schema.json").read_text()),
        format_checker=jsonschema.FormatChecker()) for vendor in "abc"
}
STIR_UA_JSON_NORMALIZED = {"a": STIR_UA_JSON_SOURCE["a"]}
for _vendor, _base in (("b", NORMALIZED_B_SCHEMA), ("c", NORMALIZED_C_SCHEMA)):
    _schema = json.loads(json.dumps(_base))
    _schema["properties"]["deviceId"] = {"const": f"mockprofile-210-{_vendor}-001"}
    _schema["required"].append("extensions")
    _schema["properties"]["extensions"] = ({"type": "object", "required": ["ext_equipment_type"],
        "properties": {"ext_equipment_type": {"const": "STIR_FRYER"}}, "additionalProperties": True}
        if _vendor == "b" else {"type": "object", "required": ["vendor_extension"],
        "properties": {"vendor_extension": {"type": "object", "required": ["EquipmentType"],
          "properties": {"EquipmentType": {"const": "STIR_FRYER"}}, "additionalProperties": True}},
        "additionalProperties": True})
    STIR_UA_JSON_NORMALIZED[_vendor] = jsonschema.Draft202012Validator(_schema, format_checker=jsonschema.FormatChecker())

COMBI_UA_JSON_SCHEMAS = {vendor: json.loads(files("foodtech_contracts").joinpath(
    f"mockprofile-220-{vendor}-v1.schema.json").read_text()) for vendor in "abc"}
COMBI_UA_JSON_SOURCE = {vendor: jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
                        for vendor, schema in COMBI_UA_JSON_SCHEMAS.items()}
COMBI_UA_JSON_NORMALIZED = {"a": COMBI_UA_JSON_SOURCE["a"]}
for _vendor, _base in (("b", NORMALIZED_B_SCHEMA), ("c", NORMALIZED_C_SCHEMA)):
    _schema = json.loads(json.dumps(_base))
    _schema["properties"]["deviceId"] = {"const": f"mockprofile-220-{_vendor}-001"}
    _schema["required"].append("extensions")
    _source = COMBI_UA_JSON_SCHEMAS[_vendor]
    if _vendor == "b":
        _keys = ["ext_equipment_type", "ext_climate_setpoint_percent", "ext_humidity_source", "ext_humidity_percent", "ext_fan_level"]
        _schema["properties"]["extensions"] = {"type": "object", "required": _keys,
            "properties": {key: _source["properties"][key] for key in _keys}, "allOf": _source["allOf"], "additionalProperties": True}
    else:
        _schema["properties"]["extensions"] = {"type": "object", "required": ["vendor_extension"],
            "properties": {"vendor_extension": _source["properties"]["Extension"]}, "additionalProperties": True}
    COMBI_UA_JSON_NORMALIZED[_vendor] = jsonschema.Draft202012Validator(_schema, format_checker=jsonschema.FormatChecker())

EQUIPMENT_SYNTHETIC_SOURCE = {}
EQUIPMENT_SYNTHETIC_NORMALIZED = {}
for _binary, _normalized in (("213", STIR_UA_JSON_NORMALIZED), ("223", COMBI_UA_JSON_NORMALIZED),
                               ("310", STIR_UA_JSON_NORMALIZED), ("320", COMBI_UA_JSON_NORMALIZED),
                               ("312", STIR_UA_JSON_NORMALIZED), ("322", COMBI_UA_JSON_NORMALIZED),
                               ("311", STIR_UA_JSON_NORMALIZED), ("321", COMBI_UA_JSON_NORMALIZED),
                               ("111", STIR_UA_JSON_NORMALIZED), ("121", COMBI_UA_JSON_NORMALIZED)):
    for _vendor in "abc":
        _id = f"mockprofile-{_binary}-{_vendor}-001"
        _source_schema = json.loads(files("foodtech_contracts").joinpath(
            f"mockprofile-{_binary}-{_vendor}-v1.schema.json").read_text())
        EQUIPMENT_SYNTHETIC_SOURCE[_id] = jsonschema.Draft202012Validator(
            _source_schema, format_checker=jsonschema.FormatChecker())
        if _vendor == "a":
            EQUIPMENT_SYNTHETIC_NORMALIZED[_id] = EQUIPMENT_SYNTHETIC_SOURCE[_id]
        else:
            _schema = json.loads(json.dumps(_normalized[_vendor].schema))
            _schema["properties"]["deviceId"] = {"const": _id}
            EQUIPMENT_SYNTHETIC_NORMALIZED[_id] = jsonschema.Draft202012Validator(
                _schema, format_checker=jsonschema.FormatChecker())

def _validate(record: dict) -> None:
    binary_id = record.get("deviceId")
    if binary_id in EQUIPMENT_SYNTHETIC_NORMALIZED:
        EQUIPMENT_SYNTHETIC_NORMALIZED[binary_id].validate(record)
        if binary_id.startswith(("mockprofile-223-c-", "mockprofile-320-c-", "mockprofile-322-c-", "mockprofile-321-c-", "mockprofile-121-c-")) and record["measurements"]["speedLevel"] != record["extensions"]["vendor_extension"]["FanLevel"]:
            raise ValueError("conflicting synthetic fan levels")
        return
    for vendor, validator in COMBI_UA_JSON_NORMALIZED.items():
        if record.get("deviceId") == f"mockprofile-220-{vendor}-001":
            validator.validate(record)
            if vendor == "c" and record["measurements"]["speedLevel"] != record["extensions"]["vendor_extension"]["FanLevel"]:
                raise ValueError("conflicting synthetic fan levels")
            return
    for vendor, validator in STIR_UA_JSON_NORMALIZED.items():
        if record.get("deviceId") == f"mockprofile-210-{vendor}-001":
            validator.validate(record)
            return
    validator = {"mockprofile-302-a-001": CSV_A_NORMALIZED_VALIDATOR, "mockprofile-302-b-001": CSV_B_NORMALIZED_VALIDATOR, "mockprofile-302-c-001": CSV_C_NORMALIZED_VALIDATOR, "mockprofile-301-a-001": NORMALIZED_MQTT_XML_A_VALIDATOR,
                 "mockprofile-301-b-001": NORMALIZED_MQTT_XML_B_VALIDATOR,
                 "mockprofile-301-c-001": NORMALIZED_MQTT_XML_C_VALIDATOR, "mockprofile-101-b-001": NORMALIZED_XML_B_VALIDATOR,
                 "mockprofile-101-c-001": NORMALIZED_XML_C_VALIDATOR, "mockprofile-101-a-001": NORMALIZED_XML_A_VALIDATOR, "mockprofile-203-b-001": NORMALIZED_UA_BINARY_B_VALIDATOR,
                 "mockprofile-203-c-001": NORMALIZED_UA_BINARY_C_VALIDATOR, "mockprofile-200-b-001": NORMALIZED_UA_JSON_B_VALIDATOR,
                 "mockprofile-200-c-001": NORMALIZED_UA_JSON_C_VALIDATOR, "mockprofile-300-b-001": NORMALIZED_MQTT_B_VALIDATOR,
                 "mockprofile-300-c-001": NORMALIZED_MQTT_C_VALIDATOR, "mockprofile-100-c-001": NORMALIZED_C_VALIDATOR, "mockprofile-100-b-001": NORMALIZED_B_VALIDATOR, "mockprofile-300-a-001": MQTT_VALIDATOR, "mockprofile-203-a-001": UA_VALIDATOR, "mockprofile-200-a-001": UA_JSON_VALIDATOR}.get(
        record.get("deviceId"), VALIDATOR)
    validator.validate(record)


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def decode_record(raw: str | bytes) -> dict:
    """Preserve decimal precision and reject ambiguous duplicate JSON keys."""
    is_xml = raw.lstrip().startswith(b"<") if isinstance(raw, bytes) else raw.lstrip().startswith("<")
    from .csv302 import is_csv, parse_record as parse_csv
    csv_source = is_csv(raw)
    if csv_source:
        record = parse_csv(raw)
    elif is_xml:
        from .xml101 import parse_record
        record = parse_record(raw)
    else:
        record = json.loads(raw, parse_float=Decimal, object_pairs_hook=_unique_pairs)
    if not isinstance(record, dict):
        raise ValueError("record must be an object")
    if any(key in record and not isinstance(record[key], str) for key in ("deviceId", "device_id", "DeviceId")):
        raise ValueError("device identifier must be a string")
    if not is_xml and any(isinstance(record.get(key), str) and
                          record[key] in {"mockprofile-101-a-001", "mockprofile-101-b-001", "mockprofile-101-c-001",
                                          "mockprofile-301-a-001", "mockprofile-301-b-001", "mockprofile-301-c-001",
                                          *{f"mockprofile-{profile}-{vendor}-001" for profile in ("111", "121", "311", "321") for vendor in "abc"}}
                          for key in ("deviceId", "device_id", "DeviceId")):
        raise ValueError("XML source profile requires XML input")
    if not csv_source and any(isinstance(record.get(key), str) and record[key] in
            {f"mockprofile-{profile}-{vendor}-001" for profile in ("302", "312", "322") for vendor in "abc"}
            for key in ("deviceId", "device_id", "DeviceId")):
        raise ValueError("CSV source profile requires CSV input")
    if (record.get("deviceId") in EQUIPMENT_SYNTHETIC_SOURCE and "-c-" in record["deviceId"] and record.get("DeviceId") != record["deviceId"]) or (record.get("deviceId") in {"mockprofile-220-c-001", "mockprofile-210-c-001", "mockprofile-302-c-001", "mockprofile-100-c-001", "mockprofile-101-c-001", "mockprofile-301-c-001", "mockprofile-200-c-001", "mockprofile-203-c-001", "mockprofile-300-c-001"} and record.get("DeviceId") != record.get("deviceId")):
        raise ValueError("vendor-c input must use its source schema")
    if (record.get("deviceId") in EQUIPMENT_SYNTHETIC_SOURCE and "-b-" in record["deviceId"] and record.get("device_id") != record["deviceId"]) or (record.get("deviceId") in {"mockprofile-220-b-001", "mockprofile-210-b-001", "mockprofile-302-b-001", "mockprofile-100-b-001", "mockprofile-101-b-001", "mockprofile-301-b-001", "mockprofile-200-b-001", "mockprofile-203-b-001", "mockprofile-300-b-001"} and record.get("device_id") != record.get("deviceId")):
        raise ValueError("vendor-b input must use its source schema")
    if record.get("device_id") in EQUIPMENT_SYNTHETIC_SOURCE or record.get("device_id") in {"mockprofile-220-b-001", "mockprofile-210-b-001", "mockprofile-302-b-001", "mockprofile-100-b-001", "mockprofile-101-b-001", "mockprofile-301-b-001", "mockprofile-200-b-001", "mockprofile-203-b-001", "mockprofile-300-b-001"}:
        ({"mockprofile-220-b-001": COMBI_UA_JSON_SOURCE["b"], "mockprofile-210-b-001": STIR_UA_JSON_SOURCE["b"], "mockprofile-302-b-001": CSV_B_VALIDATOR, "mockprofile-301-b-001": MQTT_XML_B_VALIDATOR, "mockprofile-101-b-001": XML_B_MAPPED_VALIDATOR, "mockprofile-300-b-001": MQTT_B_VALIDATOR, "mockprofile-200-b-001": UA_JSON_B_VALIDATOR,
          "mockprofile-203-b-001": UA_BINARY_B_VALIDATOR}
         .get(record["device_id"], EQUIPMENT_SYNTHETIC_SOURCE.get(record["device_id"], VENDOR_B_VALIDATOR))).validate(record)
        temperature_f = Decimal(record["temperature_f"])
        tenths = (temperature_f - 32) * 50
        if tenths % 9 != 0:
            raise ValueError("vendor-b temperature is not exactly reversible to 0.1 Celsius")
        # The project fixture permits only exact 0.1 Celsius conversion.
        temperature_c = (tenths // 9) / 10
        event_time = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=record["event_time_ms"])
        record = {"schemaVersion": record["schema_version"], "sourceId": record["source_id"],
                  "deviceId": record["device_id"], "batchId": record["batch_id"], "recordId": record["record_id"],
                  "sequenceNumber": record["sequence_number"], "eventTime": event_time.isoformat().replace("+00:00", "Z"),
                  "deviceStatus": "running" if record["device_status"] == 1 else "stopped",
                  "measurements": {"temperatureCelsius": temperature_c, "outputPercent": record["output_percent"],
                                   "elapsedSeconds": Decimal(record["elapsed_ms"]) / 1000},
                  "extensions": {key: value for key, value in record.items() if key.startswith("ext_")}}
    elif record.get("DeviceId") in EQUIPMENT_SYNTHETIC_SOURCE or record.get("DeviceId") in {"mockprofile-220-c-001", "mockprofile-210-c-001", "mockprofile-302-c-001", "mockprofile-100-c-001", "mockprofile-101-c-001", "mockprofile-301-c-001", "mockprofile-200-c-001", "mockprofile-203-c-001", "mockprofile-300-c-001"}:
        ({"mockprofile-220-c-001": COMBI_UA_JSON_SOURCE["c"], "mockprofile-210-c-001": STIR_UA_JSON_SOURCE["c"], "mockprofile-302-c-001": CSV_C_VALIDATOR, "mockprofile-301-c-001": MQTT_XML_C_VALIDATOR, "mockprofile-101-c-001": XML_C_MAPPED_VALIDATOR, "mockprofile-300-c-001": MQTT_C_VALIDATOR, "mockprofile-200-c-001": UA_JSON_C_VALIDATOR,
          "mockprofile-203-c-001": UA_BINARY_C_VALIDATOR}
         .get(record["DeviceId"], EQUIPMENT_SYNTHETIC_SOURCE.get(record["DeviceId"], VENDOR_C_VALIDATOR))).validate(record)
        reading = record["Readings"]
        record = {"schemaVersion": record["SchVer"], "sourceId": record["SrcId"],
                  "deviceId": record["DeviceId"], "batchId": record["BatchId"], "recordId": record["RecId"],
                  "sequenceNumber": record["SeqNo"], "eventTime": record["Timestamp"],
                  "deviceStatus": "running" if record["Status"] == "RUN" else "stopped",
                  "errorReason": record.get("Err") or None,
                  "measurements": {"temperatureCelsius": reading["TempC"], "speedLevel": reading["SpeedLvl"],
                                   "elapsedSeconds": Decimal(reading["ElapsedMin"]) * 60},
                  "extensions": {"alarm": record["Alarm"] == "Y", "vendor_extension": record.get("Extension", {})}}
    _validate(record)
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
    _validate(record)
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
        **({"output_percent": _decimal(measurements["outputPercent"])} if "outputPercent" in measurements
           else {"speed_level": measurements["speedLevel"]} if "speedLevel" in measurements
           else {"speed_rpm": _decimal(measurements["speedRpm"])}),
        "elapsed_seconds": _decimal(measurements["elapsedSeconds"]),
        "error_reason": record.get("errorReason"),
        "extensions": _extension(record.get("extensions", {})),
    }


def payload_hash(record: dict) -> str:
    return hashlib.sha256(rfc8785.dumps(canonical_record(record))).hexdigest()


def _component(value: str) -> str:
    return f"{len(value.encode('utf-8'))}:{value}"


def legacy_dedup_key(record: dict) -> str:
    """The v1 key is used only to find historical canonical records."""
    return "|".join(_component(record[key]) for key in ("sourceId", "schemaVersion", "recordId"))


def dedup_key(record: dict) -> str:
    """Versioned key includes the device as required by the common event contract."""
    source, device = record.get("sourceId"), record.get("deviceId")
    if not isinstance(source, str) or not source or not isinstance(device, str) or not device:
        raise ValueError("source and device are required for business deduplication")
    record_id = record.get("recordId")
    if record_id is not None and record_id != "":
        version = record.get("schemaVersion")
        if not isinstance(record_id, str) or not isinstance(version, str) or not version:
            raise ValueError("invalid business deduplication key")
        return "v2|R|" + "|".join(_component(value) for value in (source, device, version, record_id))
    sequence, event_time = record.get("sequenceNumber"), record.get("eventTime")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1 or not isinstance(event_time, str):
        raise ValueError("fallback business deduplication components are missing")
    return "v2|F|" + "|".join(_component(value) for value in (source, device, str(sequence), _utc(event_time)))


def stream_binding_key(source_id: str, device_id: str, batch_id: str) -> str:
    return "STREAM#" + "|".join(_component(value) for value in (source_id, device_id, batch_id))


def stream_key(record: dict) -> str:
    return stream_binding_key(record["sourceId"], record["deviceId"], record["batchId"])
