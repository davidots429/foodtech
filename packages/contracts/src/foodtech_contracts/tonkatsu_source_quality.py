"""Verify collection and standardization evidence within one fryer stream."""
from hashlib import sha256
from uuid import UUID

from .collection_evidence import MAX_RECEIPT_RECORDS, matches_snapshot
from .profile100 import decode_record, payload_hash, stream_key

QUALITY_PREFIX = "TONKATSU#QUALITY#"
QUALITY_SCHEMA = "synthetic-tonkatsu-source-quality-v1"


def _raw_identity(row):
    event_id = row["ingestion_event_id"]
    if str(UUID(event_id)) != event_id or row["sk"] != f"RAW#{event_id}":
        raise ValueError("invalid fryer event identity")
    return event_id


def quality_evidence(raw, assessment):
    """Build an immutable, minimal projection from an original worker decision."""
    if raw.get("source_id") != "synthetic-rest" or raw.get("device_id") != "mockprofile-100-a-001":
        return None
    record = decode_record(raw["raw"])
    if "tonkatsuSamples" not in record.get("extensions", {}):
        return None
    event_id = _raw_identity(raw)
    digest = payload_hash(record)
    if raw["pk"] != stream_key(record) or raw.get("provenance") != "SYNTHETIC" or (
        assessment["pk"], assessment["sk"], assessment["source_pk"],
        assessment["raw_sk"], assessment["ingestion_event_id"], assessment["payload_hash"]
    ) != (f"EVENT#{event_id}", "ASSESSMENT#v1", raw["pk"], raw["sk"], event_id, digest):
        raise ValueError("fryer assessment does not match its original source")
    return {
        "pk": raw["pk"],
        "sk": QUALITY_PREFIX + event_id,
        "schema_version": QUALITY_SCHEMA,
        "raw_sha256": sha256(raw["raw"].encode("utf-8")).hexdigest(),
        "payload_hash": digest,
        "quality_status": assessment["quality_status"],
        "dedup_status": assessment["dedup_status"],
        "finding_codes": assessment["finding_codes"],
    }


def verify_source_quality(stream, raws, receipt, projections):
    """Require exactly one accepted decision for every receipt-matched raw item.

    The caller must read the entire scoped snapshot and fence its raw revision.
    A missing projection is unavailable evidence, including for legacy inputs.
    """
    try:
        if not 1 <= len(raws) <= MAX_RECEIPT_RECORDS or len(raws) != len(projections):
            raise ValueError
        if receipt.get("pk") != stream or receipt.get("instance_id") != "mockprofile-100-a-001" or (
            not matches_snapshot(receipt, stream, raws)
        ):
            raise ValueError
        indexed = {item["sk"]: item for item in projections}
        if len(indexed) != len(raws):
            raise ValueError
        for raw in raws:
            item = indexed.pop(QUALITY_PREFIX + _raw_identity(raw))
            record = decode_record(raw["raw"])
            if (
                raw["pk"] != stream_key(record) or record["sourceId"] != "synthetic-rest"
                or record["deviceId"] != "mockprofile-100-a-001"
                or "tonkatsuSamples" not in record.get("extensions", {})
                or item.get("pk") != stream or item.get("schema_version") != QUALITY_SCHEMA
                or item.get("raw_sha256") != sha256(raw["raw"].encode("utf-8")).hexdigest()
                or item.get("payload_hash") != payload_hash(record)
                or item.get("quality_status") != "VALID" or item.get("dedup_status") != "UNIQUE"
                or item.get("finding_codes") != []
            ):
                raise ValueError
    except (KeyError, TypeError, ValueError, AttributeError, ArithmeticError):
        raise ValueError("complete fryer source quality evidence unavailable") from None
