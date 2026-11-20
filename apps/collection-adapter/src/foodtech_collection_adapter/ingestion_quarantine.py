"""Preserve malformed synthetic REST input before advancing its source cursor."""
import hashlib
import json
import uuid
from datetime import datetime, timezone

import boto3

MAX_PAYLOAD_BYTES = 2_000_000


def failure(raw: str | bytes) -> tuple[str, list[str]] | None:
    """Identify only failures that cannot enter the ordered event pipeline."""
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON member")
            result[key] = value
        return result
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            return "UNSUPPORTED_ENCODING", []
    try:
        value = json.loads(raw, object_pairs_hook=unique, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, RecursionError):
        return "SCHEMA_VIOLATION", []
    if not isinstance(value, dict):
        return "SCHEMA_VIOLATION", []
    if (value.get("device_id") == "mockprofile-100-b-001" or
            ("deviceId" not in value and any(key in value for key in ("schema_version", "source_id", "device_id")))):
        value = {"sourceId": value.get("source_id"), "deviceId": value.get("device_id"),
                 "batchId": value.get("batch_id"), "schemaVersion": value.get("schema_version"),
                 "sequenceNumber": value.get("sequence_number"), "recordId": value.get("record_id"),
                 "eventTime": value.get("event_time_ms")}
    required = ("sourceId", "deviceId", "batchId", "schemaVersion", "sequenceNumber")
    missing = [name for name in required if value.get(name) is None or value.get(name) == ""]
    if not value.get("recordId") and not value.get("eventTime"):
        missing.extend(["recordId", "eventTime"])
    return ("MANDATORY_FIELD_MISSING", missing) if missing else None


class IngestionQuarantine:
    def __init__(self, bucket: str, table: str, region: str = "ap-southeast-2"):
        if not bucket or not table:
            raise ValueError("quarantine bucket and manifest table required")
        session = boto3.Session(region_name=region)
        self.s3 = session.client("s3")
        self.table = session.resource("dynamodb").Table(table)
        self.bucket = bucket

    def preserve(self, raw: str | bytes, *, cursor_key: str, position: int, reason: str, missing: list[str],
                 instance_id: str = "mockprofile-100-a-001") -> str:
        payload = raw if isinstance(raw, bytes) else raw.encode("utf-8")
        if len(payload) > MAX_PAYLOAD_BYTES:
            raise ValueError("payload exceeds quarantine limit; source cursor was not advanced")
        digest = hashlib.sha256(payload).hexdigest()
        # A retry of the same source cursor and identical bytes uses the same record.
        identity = (f"foodtech:rest100:{cursor_key}:{position}:{digest}"
                    if instance_id == "mockprofile-100-a-001" else
                    f"foodtech:rest100:{instance_id}:{cursor_key}:{position}:{digest}")
        event_id = str(uuid.uuid5(uuid.NAMESPACE_URL, identity))
        object_key = f"ingestion/REST/mockprofile-100/{event_id}/{digest}.bin"
        self.s3.put_object(Bucket=self.bucket, Key=object_key, Body=payload,
                           ContentType="application/octet-stream", ServerSideEncryption="AES256")
        item = {"pk": f"INGESTION_DLQ#{event_id}", "sk": "MANIFEST", "ingestion_event_id": event_id,
                "object_key": object_key, "bucket": self.bucket, "sha256": digest,
                "ingestion_failure_reason_code": reason, "missing_components": missing,
                "received_at": datetime.now(timezone.utc).isoformat(), "protocol": "REST",
                "profile_id": "mockprofile-100", "instance_id": instance_id, "cursor_key": cursor_key, "source_position": position,
                "provenance": "SYNTHETIC"}
        try:
            self.table.put_item(Item=item, ConditionExpression="attribute_not_exists(pk)")
        except self.table.meta.client.exceptions.ConditionalCheckFailedException:
            existing = self.table.get_item(Key={"pk": item["pk"], "sk": "MANIFEST"}, ConsistentRead=True).get("Item")
            if not existing or any(existing.get(key) != item[key] for key in
                                   ("object_key", "bucket", "sha256", "ingestion_failure_reason_code", "missing_components")):
                raise ValueError("conflicting ingestion quarantine manifest")
        return event_id

    def preserve_mqtt(self, payload: bytes, *, topic: str, client_id: str, reason: str, missing: list[str]) -> str:
        """Record each delivered message independently, including non-UTF-8 bytes."""
        if not isinstance(payload, bytes) or len(payload) > MAX_PAYLOAD_BYTES:
            raise ValueError("payload exceeds quarantine limit")
        event_id = str(uuid.uuid4())
        digest = hashlib.sha256(payload).hexdigest()
        object_key = f"ingestion/MQTT/mockprofile-300/{event_id}/{digest}.bin"
        self.s3.put_object(Bucket=self.bucket, Key=object_key, Body=payload,
                           ContentType="application/octet-stream", ServerSideEncryption="AES256")
        self.table.put_item(Item={
            "pk": f"INGESTION_DLQ#{event_id}", "sk": "MANIFEST", "ingestion_event_id": event_id,
            "object_key": object_key, "bucket": self.bucket, "sha256": digest,
            "ingestion_failure_reason_code": reason, "missing_components": missing,
            "received_at": datetime.now(timezone.utc).isoformat(), "protocol": "MQTT",
            "profile_id": "mockprofile-300", "topic": topic, "client_id": client_id,
            "qos": 1, "provenance": "SYNTHETIC",
        }, ConditionExpression="attribute_not_exists(pk)")
        return event_id
