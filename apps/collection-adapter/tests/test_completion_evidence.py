import json
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from foodtech_collection_adapter import __main__ as cli
from foodtech_collection_adapter.rest import PollResult
from foodtech_contracts import stream_key
from foodtech_contracts.collection_evidence import CompletionTracker, matches_snapshot

FIXTURE = json.loads((Path(__file__).resolve().parents[3] / "fixtures/mockprofile-100-a-001.json").read_text())


class CompletionEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.records = FIXTURE["records"]
        self.raw = [json.dumps(item, separators=(",", ":")) for item in self.records]
        self.stream = stream_key(self.records[0])

    def tracker(self, cursor=0):
        return CompletionTracker(cursor, "mockprofile-100", "mockprofile-100-a-001")

    def test_exact_snapshot_match_detects_changes_and_multiplicity(self):
        tracker = self.tracker()
        for raw, record in zip(self.raw, self.records): tracker.record(raw, record)
        self.assertIsNone(tracker.finish(next_cursor=3, complete=True, completion_explicit=False))
        receipt = tracker.finish(next_cursor=3, complete=True, completion_explicit=True)
        receipt["record_count"] = Decimal(3)  # DynamoDB resource reads numbers as Decimal.
        rows = [{"pk": self.stream, "provenance": "SYNTHETIC", "raw": raw} for raw in self.raw]
        self.assertTrue(matches_snapshot(receipt, self.stream, list(reversed(rows))))
        self.assertFalse(matches_snapshot(receipt, self.stream, rows[:-1]))
        self.assertFalse(matches_snapshot(receipt, self.stream, [rows[0], rows[0], rows[2]]))
        self.assertFalse(matches_snapshot(receipt, self.stream, rows + rows[:1]))
        self.assertIsNone(tracker.finish(next_cursor=3, complete=True, completion_explicit=True))

    def test_incomplete_resumed_mixed_and_quarantined_collections_have_no_receipt(self):
        for variant in ("resumed", "mixed", "quarantine", "not_terminal", "wrong_cursor"):
            tracker = self.tracker(1 if variant == "resumed" else 0)
            tracker.record(self.raw[0], self.records[0])
            if variant == "mixed": tracker.record(self.raw[1], {**self.records[1], "batchId": "other"})
            if variant == "quarantine": tracker.reject()
            receipt = tracker.finish(next_cursor=2 if variant == "wrong_cursor" else 1,
                                     complete=variant != "not_terminal", completion_explicit=True)
            self.assertIsNone(receipt, variant)

    def test_cli_saves_receipt_only_after_cursor_and_explicit_terminal(self):
        calls = []
        raw = self.raw[0]
        class Client:
            def __init__(self, *_args): pass
            def poll(self, cursor):
                self.assert_cursor = cursor
                return PollResult((raw,), 1, True, True)
            def close(self): pass
        class Ingestor:
            def __init__(self, *_args): pass
            def cursor_state(self, _key): return 0, set()
            def ingest(self, _raw): calls.append("ingest"); return []
            def drain(self, _stream): return []
            def save_cursor(self, *args): calls.append("cursor")
            def save_completion_receipt(self, receipt, key):
                calls.append("receipt")
                assert receipt["stream_pk"] == stream_key(FIXTURE["records"][0])
                assert key == "default"
        config = {name: "test" for name in (
            "FOODTECH_MOCK_BASE_URL", "FOODTECH_MOCK_CLIENT_ID", "FOODTECH_MOCK_CLIENT_SECRET",
            "FOODTECH_MOCK_CA_FILE", "FOODTECH_DATA_TABLE", "FOODTECH_ORDERED_QUEUE_URL")}
        with patch.dict("os.environ", config), patch.object(cli, "RestMockClient", Client), \
             patch.object(cli, "DynamoOrderedIngestor", Ingestor), patch("sys.argv", ["collector"]):
            self.assertEqual(cli.main(), 0)
        self.assertEqual(calls, ["ingest", "cursor", "receipt"])
