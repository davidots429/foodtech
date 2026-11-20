"""Bounded evidence for a complete, single-run synthetic REST collection.

This is a receipt of what the collector observed, not a quality decision or
proof that asynchronous standardization has completed.
"""
from hashlib import sha256
from collections import Counter
from decimal import Decimal

from foodtech_contracts import stream_key

MAX_RECEIPT_RECORDS = 1000
RECEIPT_VERSION = "synthetic-rest-collection-receipt-v1"


class CompletionTracker:
    def __init__(self, starting_cursor: int, profile_id: str, instance_id: str):
        self.enabled = starting_cursor == 0 and profile_id == "mockprofile-100"
        self.instance_id = instance_id
        self.stream = None
        self.hashes = []
        self.completed = False

    def reject(self) -> None:
        self.enabled = False

    def record(self, raw: str, record: dict) -> None:
        if not self.enabled:
            return
        key = stream_key(record)
        if self.completed or record.get("deviceId") != self.instance_id or \
                (self.stream is not None and self.stream != key) or len(self.hashes) >= MAX_RECEIPT_RECORDS:
            self.reject()
            return
        self.stream = key
        self.hashes.append(sha256(raw.encode("utf-8")).hexdigest())

    def finish(self, *, next_cursor: int, complete: bool, completion_explicit: bool) -> dict | None:
        if not self.enabled or self.completed or not complete or not completion_explicit or \
                not self.stream or not self.hashes or next_cursor != len(self.hashes):
            return None
        self.completed = True
        return {"stream_pk": self.stream, "receipt_version": RECEIPT_VERSION,
                "provenance": "SYNTHETIC", "profile_id": "mockprofile-100",
                "instance_id": self.instance_id, "starting_cursor": 0, "ending_cursor": next_cursor,
                "raw_sha256": list(self.hashes), "record_count": len(self.hashes)}


def matches_snapshot(receipt: dict, stream_pk: str, raw_items: list[dict]) -> bool:
    """Match the received bytes to an exact raw snapshot, with multiplicity.

    A match says nothing about source truth, data quality, or stream changes
    after the snapshot and does not authorize downstream model execution.
    """
    hashes = receipt.get("raw_sha256")
    count = receipt.get("record_count")
    if isinstance(count, Decimal) and not count.is_finite():
        return False
    if receipt.get("receipt_version") != RECEIPT_VERSION or receipt.get("provenance") != "SYNTHETIC" or \
            receipt.get("profile_id") != "mockprofile-100" or receipt.get("stream_pk") != stream_pk or \
            receipt.get("starting_cursor") != 0 or isinstance(count, bool) or not isinstance(count, (int, Decimal)) or \
            not 1 <= count <= MAX_RECEIPT_RECORDS or count != int(count) or receipt.get("ending_cursor") != count or \
            not isinstance(hashes, list) or len(hashes) != count or len(raw_items) != count or \
            any(not isinstance(value, str) or len(value) != 64 for value in hashes):
        return False
    actual = []
    for item in raw_items:
        if item.get("pk") != stream_pk or item.get("provenance") != "SYNTHETIC" or not isinstance(item.get("raw"), str):
            return False
        actual.append(sha256(item["raw"].encode("utf-8")).hexdigest())
    return Counter(hashes) == Counter(actual)
