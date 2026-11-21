import json
import unittest
from unittest.mock import patch

from foodtech_business_api.jeyuk_source_evidence import read_collection_completion
from foodtech_contracts.jeyuk_receipt import JeyukCompletionTracker


def sample(second):
    return json.dumps({"fixtureId": "evidence", "provenance": "SYNTHETIC", "profileId": "mockprofile-110",
                       "instanceId": "mockprofile-110-a-001", "profile": "SMALL", "actualLoadKg": 1.8,
                       "second": second, "sampleIntervalSeconds": 1, "surfaceCelsius": 190,
                       "actualRpm": 30, "targetRpm": 30, "motorLoadPercent": 40})


class EvidenceReadTests(unittest.TestCase):
    def test_receipt_is_verified_against_every_raw_row(self):
        tracker = JeyukCompletionTracker(0)
        rows = []
        for second in range(1, 4):
            raw = sample(second)
            tracker.record(raw)
            rows.append({"pk": "JEYUK#evidence", "sk": f"RAW#{second:06d}", "raw": raw,
                         "provenance": "SYNTHETIC"})
        receipt = tracker.finish(cursor=3, complete=True, explicit=True)
        class Table:
            name = "table"
            def __init__(self):
                self.calls = 0; self.meta = self; self.client = self; self.revisions = [3, 3]
            def get_item(self, **kwargs):
                assert kwargs["ConsistentRead"]
                value = self.revisions.pop(0)
                return {"Item": {"revision": value}} if value is not None else {}
            def get_paginator(self, operation):
                assert operation == "query"
                return self
            def paginate(self, **kwargs):
                self.calls += 1
                assert kwargs["ConsistentRead"]
                return ([{"Items": [receipt]}] if self.calls % 2 else
                        [{"Items": rows[:2]}, {"Items": rows[2:]}])
        table = Table()
        class Session:
            def __init__(self, **_kwargs): pass
            def resource(self, _service): return self
            def Table(self, _name): return table
        with patch("foodtech_business_api.jeyuk_source_evidence.boto3.Session", Session):
            self.assertEqual(read_collection_completion("table", "evidence"),
                             {"status": "MATCHED_AT_READ", "raw_count": 3})
            rows[1] = {**rows[1], "raw": rows[1]["raw"] + " "}
            table.revisions = [3, 3]
            self.assertEqual(read_collection_completion("table", "evidence")["status"], "NOT_MATCHED")
            table.revisions = [3, 4]
            self.assertEqual(read_collection_completion("table", "evidence")["status"], "CHANGED_DURING_READ")
            table.revisions = [None, None]
            self.assertEqual(read_collection_completion("table", "evidence")["status"], "REVISION_UNVERIFIED")
            table.revisions = [2, 2]
            self.assertEqual(read_collection_completion("table", "evidence")["status"], "REVISION_UNVERIFIED")
