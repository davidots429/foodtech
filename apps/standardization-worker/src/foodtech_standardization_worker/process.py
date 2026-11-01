"""First synthetic profile mapping and physical-limit quality rule."""

from dataclasses import dataclass
from decimal import Decimal

from foodtech_contracts import canonical_record, decode_record, dedup_key, payload_hash

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
        "ROTATIONAL_SPEED_RPM": {"value": canonical["speed_rpm"], "unit": "rpm"},
        "DURATION": {"value": canonical["elapsed_seconds"], "unit": "s"},
    } if quality != "QUARANTINED" else None
    return Assessment(
        ingestion_event_id=ingestion_event_id,
        business_dedup_key=dedup_key(record),
        payload_hash=payload_hash(record),
        quality_status=quality,
        findings=tuple(findings),
        normalized=normalized,
        raw_values={
            "temperatureCelsius": canonical["temperature_celsius"],
            "speedRpm": canonical["speed_rpm"],
            "elapsedSeconds": canonical["elapsed_seconds"],
            "deviceStatus": canonical["device_status"],
            "eventTime": record["eventTime"],
        },
        corrected_values={
            "temperature_celsius": canonical["temperature_celsius"],
            "speed_rpm": canonical["speed_rpm"],
            "elapsed_seconds": canonical["elapsed_seconds"],
            "device_status": "RUNNING" if canonical["device_status"] == "running" else "STOPPED",
            "event_time": canonical["event_time"],
        },
        correction_rule_id="FT-MAP-VENDOR-A-1",
        mapping_version=MAPPING_VERSION,
    )
