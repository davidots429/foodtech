"""Evidence of a single, explicitly completed synthetic Jeyuk collection.

A receipt describes the observed Mock stream only. It is not a safety decision
or authorization to use a model, and cannot prove later source immutability.
"""
from hashlib import sha256

from .jeyuk_mock import decode_jeyuk_mock_sample

VERSION = "synthetic-jeyuk-collection-v1"
MAX_SAMPLES = 10000


def _add(digest, raw: str) -> None:
    encoded = raw.encode("utf-8")
    digest.update(len(encoded).to_bytes(8, "big"))
    digest.update(encoded)


class JeyukCompletionTracker:
    def __init__(self, cursor: int):
        self.enabled = type(cursor) is int and cursor == 0
        self.fixture_id = None
        self.identity = None
        self.count = 0
        self.digest = sha256()

    def record(self, raw: str) -> None:
        if not self.enabled:
            return
        try:
            sample = decode_jeyuk_mock_sample(raw)
            identity = (sample.fixture_id, sample.profile, sample.actual_load_kg)
            if not isinstance(raw, str) or self.count >= MAX_SAMPLES or sample.second != self.count + 1 or \
                    (self.identity is not None and identity != self.identity):
                raise ValueError("non-contiguous or mixed Jeyuk source")
            self.identity = identity
            self.fixture_id = sample.fixture_id
            self.count += 1
            _add(self.digest, raw)
        except (KeyError, TypeError, ValueError, UnicodeError, OverflowError):
            self.enabled = False

    def finish(self, *, cursor: int, complete: bool, explicit: bool) -> dict | None:
        if not self.enabled or not self.count or type(cursor) is not int or cursor != self.count or \
                complete is not True or explicit is not True:
            return None
        self.enabled = False
        return {"receipt_version": VERSION, "provenance": "SYNTHETIC", "profile_id": "mockprofile-110",
                "fixture_id": self.fixture_id, "starting_cursor": 0, "ending_cursor": cursor,
                "record_count": self.count, "ordered_raw_sha256": self.digest.hexdigest()}


def matches_jeyuk_snapshot(receipt: dict, fixture_id: str, raws: list[dict]) -> bool:
    """Compare a full, ordered raw snapshot to one observed terminal receipt."""
    if not isinstance(receipt, dict) or receipt.get("receipt_version") != VERSION or \
            receipt.get("provenance") != "SYNTHETIC" or receipt.get("profile_id") != "mockprofile-110" or \
            receipt.get("fixture_id") != fixture_id or receipt.get("starting_cursor") != 0:
        return False
    count = receipt.get("record_count")
    # DynamoDB resource numeric values are Decimal; reject booleans and fractions.
    from decimal import Decimal
    if isinstance(count, bool) or not isinstance(count, (int, Decimal)) or \
            (isinstance(count, Decimal) and not count.is_finite()) or not 1 <= count <= MAX_SAMPLES or \
            count != int(count) or receipt.get("ending_cursor") != count or len(raws) != count:
        return False
    tracker = JeyukCompletionTracker(0)
    for second, row in enumerate(raws, 1):
        if not isinstance(row, dict) or row.get("pk") != f"JEYUK#{fixture_id}" or \
                row.get("sk") != f"RAW#{second:06d}" or row.get("provenance") != "SYNTHETIC":
            return False
        tracker.record(row.get("raw"))
    actual = tracker.finish(cursor=int(count), complete=True, explicit=True)
    return bool(actual and actual["ordered_raw_sha256"] == receipt.get("ordered_raw_sha256"))
