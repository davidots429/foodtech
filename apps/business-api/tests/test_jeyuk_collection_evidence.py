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
            # Settings are derived only from a matching, tracked complete receipt.
            for index, row in enumerate(rows):
                value = json.loads(row["raw"]); value["heatingSetpointCelsius"] = 200
                rows[index] = {**row, "raw": json.dumps(value)}
            def refresh_receipt():
                tracker = JeyukCompletionTracker(0)
                for row in rows: tracker.record(row["raw"])
                return tracker.finish(cursor=3, complete=True, explicit=True)
            receipt = refresh_receipt(); table.revisions = [3, 3]
            settings = read_collection_completion("table", "evidence", include_settings=True)["settings"]
            self.assertEqual(settings["heating_setpoint_celsius"], {"status": "STABLE", "value": "200"})
            self.assertEqual(settings["target_rpm"], {"status": "STABLE", "value": "30"})
            value = json.loads(rows[1]["raw"]); value["heatingSetpointCelsius"] = 195
            rows[1] = {**rows[1], "raw": json.dumps(value)}
            receipt = refresh_receipt(); table.revisions = [3, 3]
            self.assertEqual(read_collection_completion("table", "evidence", include_settings=True)
                             ["settings"]["heating_setpoint_celsius"]["status"], "CHANGED")
            value["targetRpm"] = 25
            rows[1] = {**rows[1], "raw": json.dumps(value)}
            receipt = refresh_receipt(); table.revisions = [3, 3]
            self.assertEqual(read_collection_completion("table", "evidence", include_settings=True)
                             ["settings"]["target_rpm"]["status"], "CHANGED")
            del value["heatingSetpointCelsius"]; rows[1] = {**rows[1], "raw": json.dumps(value)}
            receipt = refresh_receipt(); table.revisions = [3, 3]
            self.assertEqual(read_collection_completion("table", "evidence", include_settings=True)
                             ["settings"]["heating_setpoint_celsius"]["status"], "PARTIAL")
            for index, row in enumerate(rows):
                value = json.loads(row["raw"]); value.pop("heatingSetpointCelsius", None)
                rows[index] = {**row, "raw": json.dumps(value)}
            receipt = refresh_receipt(); table.revisions = [3, 3]
            self.assertEqual(read_collection_completion("table", "evidence", include_settings=True)
                             ["settings"]["heating_setpoint_celsius"]["status"], "NOT_RECORDED")
            table.revisions = [3, 3]
            rows[1] = {**rows[1], "raw": rows[1]["raw"] + " "}
            table.revisions = [3, 3]
            self.assertEqual(read_collection_completion("table", "evidence")["status"], "NOT_MATCHED")
            table.revisions = [3, 3]
            self.assertNotIn("settings", read_collection_completion("table", "evidence", include_settings=True))
            table.revisions = [3, 4]
            self.assertEqual(read_collection_completion("table", "evidence")["status"], "CHANGED_DURING_READ")
            table.revisions = [None, None]
            self.assertEqual(read_collection_completion("table", "evidence")["status"], "REVISION_UNVERIFIED")
            table.revisions = [2, 2]
            self.assertEqual(read_collection_completion("table", "evidence")["status"], "REVISION_UNVERIFIED")
