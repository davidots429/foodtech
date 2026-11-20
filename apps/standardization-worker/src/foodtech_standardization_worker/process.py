"""First synthetic profile mapping and physical-limit quality rule."""

from dataclasses import dataclass
from decimal import Decimal
import json

from foodtech_contracts import canonical_record, decode_record, dedup_key, legacy_dedup_key, payload_hash

ABSOLUTE_ZERO_C = Decimal("-273.15")
MAPPING_VERSION = "mockprofile-100-a-map-v1"
QUALITY_RULE_VERSION = "synthetic-physical-limits-v1"


@dataclass(frozen=True)
class Finding:
    code: str
    rule_id: str
    rule_version: str
    detail: str


@dataclass(frozen=True)
class Assessment:
    ingestion_event_id: str
    business_dedup_key: str
    legacy_business_dedup_key: str
    payload_hash: str
    quality_status: str
    findings: tuple[Finding, ...]
    normalized: dict | None
    raw_values: dict
    corrected_values: dict
    correction_rule_id: str
    mapping_version: str
    provenance: str = "SYNTHETIC"


def assess(ingestion_event_id: str, raw: str, *, sequence_status: str = "IN_ORDER",
           gap_before: tuple[int, ...] = ()) -> Assessment:
    record = decode_record(raw)
    canonical = canonical_record(record)
    temperature = Decimal(canonical["temperature_celsius"])
    if "output_percent" in canonical or "speed_level" in canonical:
        if raw.lstrip().startswith("<"):
            from foodtech_contracts.xml101 import parse_record
            source_raw = parse_record(raw)
        else:
            source_raw = json.loads(raw, parse_float=Decimal)
    else:
        source_raw = None
    findings = []
    if temperature < ABSOLUTE_ZERO_C:
        findings.append(Finding(
            "HARD_LIMIT_EXCEEDED", "FT-Q-ABSOLUTE-ZERO", QUALITY_RULE_VERSION,
            "temperatureCelsius is below absolute zero",
        ))
    if gap_before:
        findings.append(Finding(
            "DATA_GAP", "FT-Q-SEQUENCE-GAP", QUALITY_RULE_VERSION,
            f"missing sequence {gap_before[0]}..{gap_before[-1]}",
        ))
    if sequence_status == "LATE_AFTER_GAP":
        findings.append(Finding(
            "LATE_AFTER_GAP", "FT-Q-SEQUENCE-LATE", QUALITY_RULE_VERSION,
            "record arrived after the sequence gap was closed",
        ))
    quality = "QUARANTINED" if any(item.code == "HARD_LIMIT_EXCEEDED" for item in findings) else \
        "WARNING" if findings else "VALID"
    normalized = {
        "source_id": canonical["source_id"],
        "device_id": canonical["device_id"],
        "batch_id": canonical["batch_id"],
        "record_id": canonical["record_id"],
        "sequence_number": canonical["sequence_number"],
        "event_time": canonical["event_time"],
        "device_status": "RUNNING" if canonical["device_status"] == "running" else "STOPPED",
        "CHAMBER_TEMPERATURE": {"value": canonical["temperature_celsius"], "unit": "°C"},
        **({"OUTPUT_PERCENT": {"value": canonical["output_percent"], "unit": "%"}}
           if "output_percent" in canonical else
           {"SPEED_LEVEL": {"value": canonical["speed_level"], "unit": None}} if "speed_level" in canonical else
           {"ROTATIONAL_SPEED_RPM": {"value": canonical["speed_rpm"], "unit": "rpm"}}),
        "DURATION": {"value": canonical["elapsed_seconds"], "unit": "s"},
    } if quality != "QUARANTINED" else None
    return Assessment(
        ingestion_event_id=ingestion_event_id,
        business_dedup_key=dedup_key(record),
        legacy_business_dedup_key=legacy_dedup_key(record),
        payload_hash=payload_hash(record),
        quality_status=quality,
        findings=tuple(findings),
        normalized=normalized,
        raw_values=({
            "temperature_f": str(source_raw["temperature_f"]),
            "output_percent": canonical["output_percent"],
            "elapsed_ms": source_raw["elapsed_ms"],
            "device_status": source_raw["device_status"],
            "event_time_ms": source_raw["event_time_ms"],
        } if "output_percent" in canonical else {
            "TempC": str(source_raw["Readings"]["TempC"]),
            "SpeedLvl": source_raw["Readings"]["SpeedLvl"],
            "ElapsedMin": str(source_raw["Readings"]["ElapsedMin"]),
            "Status": source_raw["Status"], "Timestamp": source_raw["Timestamp"], "Alarm": source_raw["Alarm"],
        } if "speed_level" in canonical else {
            "temperatureCelsius": canonical["temperature_celsius"],
            "speedRpm": canonical["speed_rpm"],
            "elapsedSeconds": canonical["elapsed_seconds"],
            "deviceStatus": canonical["device_status"],
            "eventTime": record["eventTime"],
        }),
        corrected_values={
            "temperature_celsius": canonical["temperature_celsius"],
            **({"output_percent": canonical["output_percent"]} if "output_percent" in canonical
               else {"speed_level": canonical["speed_level"]} if "speed_level" in canonical
               else {"speed_rpm": canonical["speed_rpm"]}),
            "elapsed_seconds": canonical["elapsed_seconds"],
            "device_status": "RUNNING" if canonical["device_status"] == "running" else "STOPPED",
            "event_time": canonical["event_time"],
        },
        correction_rule_id=("FT-MAP-VENDOR-B-1" if "output_percent" in canonical else
                            "FT-MAP-VENDOR-C-1" if "speed_level" in canonical else "FT-MAP-VENDOR-A-1"),
        mapping_version={"mockprofile-301-a-001": "mockprofile-301-a-map-v1",
                         "mockprofile-301-b-001": "mockprofile-301-b-map-v1",
                         "mockprofile-301-c-001": "mockprofile-301-c-map-v1",
                         "mockprofile-101-b-001": "mockprofile-101-b-map-v1",
                         "mockprofile-101-c-001": "mockprofile-101-c-map-v1",
                         "mockprofile-101-a-001": "mockprofile-101-a-map-v1",
                         "mockprofile-203-b-001": "mockprofile-203-b-map-v1",
                         "mockprofile-203-c-001": "mockprofile-203-c-map-v1",
                         "mockprofile-200-b-001": "mockprofile-200-b-map-v1",
                         "mockprofile-200-c-001": "mockprofile-200-c-map-v1",
                         "mockprofile-300-b-001": "mockprofile-300-b-map-v1",
                         "mockprofile-300-c-001": "mockprofile-300-c-map-v1",
                         "mockprofile-100-c-001": "mockprofile-100-c-map-v1",
                         "mockprofile-100-b-001": "mockprofile-100-b-map-v1",
                         "mockprofile-300-a-001": "mockprofile-300-a-map-v1",
                         "mockprofile-203-a-001": "mockprofile-203-a-map-v1",
                         "mockprofile-200-a-001": "mockprofile-200-a-map-v1"}.get(record["deviceId"], MAPPING_VERSION),
    )
