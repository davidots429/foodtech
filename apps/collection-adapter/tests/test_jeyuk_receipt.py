import json
import unittest
from decimal import Decimal
from unittest.mock import patch

from foodtech_collection_adapter import __main__ as cli
from foodtech_collection_adapter.rest import PollResult

from foodtech_contracts.jeyuk_receipt import JeyukCompletionTracker, matches_jeyuk_snapshot


def raw(second, fixture="sample"):
    return json.dumps({"fixtureId": fixture, "provenance": "SYNTHETIC", "profileId": "mockprofile-110",
                       "instanceId": "mockprofile-110-a-001", "profile": "SMALL", "actualLoadKg": 1.8,
                       "second": second, "sampleIntervalSeconds": 1, "surfaceCelsius": 190,
                       "actualRpm": 30, "targetRpm": 30, "motorLoadPercent": 40})


class JeyukReceiptTests(unittest.TestCase):
    def test_terminal_receipt_matches_only_exact_ordered_raw_snapshot(self):
        tracker = JeyukCompletionTracker(0)
        rows = []
        for second in range(1, 4):
            sample = raw(second)
            tracker.record(sample)
            rows.append({"pk": "JEYUK#sample", "sk": f"RAW#{second:06d}", "raw": sample,
                         "provenance": "SYNTHETIC"})
        self.assertIsNone(tracker.finish(cursor=3, complete=True, explicit=False))
        receipt = tracker.finish(cursor=3, complete=True, explicit=True)
        receipt["record_count"] = Decimal(3)
        self.assertTrue(matches_jeyuk_snapshot(receipt, "sample", rows))
        for altered in (rows[:-1], rows + rows[:1], list(reversed(rows)),
                        [rows[0], {**rows[1], "raw": raw(2) + " "}, rows[2]]):
            self.assertFalse(matches_jeyuk_snapshot(receipt, "sample", altered))
        self.assertFalse(matches_jeyuk_snapshot(receipt, "other", rows))
        self.assertIsNone(tracker.finish(cursor=3, complete=True, explicit=True))

    def test_collector_persists_receipt_only_after_all_raw_writes(self):
        calls = []
        class Client:
            def __init__(self, *_args, **_kwargs): self.results = iter([
                PollResult((raw(1),), 1, False, True), PollResult((raw(2),), 2, True, True)])
            def poll(self, _cursor): return next(self.results)
            def close(self): pass
        class Ingestor:
            def __init__(self, *_args): pass
            def cursor(self, _key): return 0
            def ingest(self, sample): calls.append(("raw", json.loads(sample)["second"]))
            def save_completion_receipt(self, receipt, _key): calls.append(("receipt", receipt["record_count"]))
            def save_cursor(self, cursor, _key): calls.append(("cursor", cursor))
        config = {key: "test" for key in ("FOODTECH_MOCK_BASE_URL", "FOODTECH_MOCK_CLIENT_ID",
                  "FOODTECH_MOCK_CLIENT_SECRET", "FOODTECH_MOCK_CA_FILE", "FOODTECH_DATA_TABLE",
                  "FOODTECH_ORDERED_QUEUE_URL")}
        with patch.dict("os.environ", config), patch.object(cli, "RestMockClient", Client), \
                patch.object(cli, "DynamoJeyukIngestor", Ingestor), \
                patch("sys.argv", ["collector", "--profile", "mockprofile-110", "--until-complete", "--interval", "0"]):
            self.assertEqual(cli.main(), 0)
        self.assertEqual(calls, [("raw", 1), ("cursor", 1), ("raw", 2), ("receipt", 2), ("cursor", 2)])

    def test_resumed_gap_mixed_and_missing_completion_are_not_receipted(self):
        for cursor, samples, terminal, expected_cursor in [
            (1, [raw(1)], True, 1), (0, [raw(1), raw(3)], True, 2),
            (0, [raw(1), raw(2, "other")], True, 2), (0, [raw(1)], False, 1),
            (0, [raw(1)], True, 2), (0, ["invalid"], True, 1),
        ]:
            tracker = JeyukCompletionTracker(cursor)
            for sample in samples: tracker.record(sample)
            self.assertIsNone(tracker.finish(cursor=expected_cursor, complete=terminal, explicit=True))
