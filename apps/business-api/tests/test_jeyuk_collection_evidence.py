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
                             {"status": "MATCHED_AT_READ", "raw_count": 3, "source_revision": 3})
            table.revisions = [3, 3]
            self.assertEqual(read_collection_completion("table", "evidence", include_reference=True)["status"],
                             "REFERENCE_UNAVAILABLE")
            receipt = {**receipt, "pk": "JEYUK#evidence", "sk": "COLLECTION#RECEIPT#test"}
            table.revisions = [3, 3]
            reference = read_collection_completion("table", "evidence", include_reference=True)["receipt_reference"]
            self.assertEqual(reference, {"pk": "JEYUK#evidence", "sk": "COLLECTION#RECEIPT#test",
                                         "record_count": 3, "ordered_raw_sha256": receipt["ordered_raw_sha256"]})
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
            for index, row in enumerate(rows):
                from datetime import datetime, timedelta, timezone
                value = json.loads(row["raw"])
                value["observedAt"] = (datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=index + 1)).isoformat()
                value["actualRpm"] = 20; value["motorLoadPercent"] = 95
                rows[index] = {**row, "raw": json.dumps(value)}
            receipt = refresh_receipt(); table.revisions = [3, 3]
            observed = read_collection_completion("table", "evidence", include_settings=True)
            self.assertEqual(observed["sample_timeline"]["status"], "RECORDED")
            self.assertEqual(observed["sample_timeline"]["sample_count"], 3)
            self.assertEqual(observed["process_observations"]["reason_codes"], [])
            # A broken sample clock never becomes a verified timeline.
            value = json.loads(rows[1]["raw"]); value["observedAt"] = "2026-01-01T00:00:04+00:00"
            rows[1] = {**rows[1], "raw": json.dumps(value)}
            receipt = refresh_receipt(); table.revisions = [3, 3]
            self.assertEqual(read_collection_completion("table", "evidence", include_settings=True)
                             ["sample_timeline"]["status"], "INCONSISTENT")
            table.revisions = [3, 3]
            rows[1] = {**rows[1], "raw": rows[1]["raw"] + " "}
            table.revisions = [3, 3]
            mismatch = read_collection_completion("table", "evidence")
            self.assertEqual(mismatch["status"], "NOT_MATCHED")
            self.assertNotIn("source_revision", mismatch)
            table.revisions = [3, 3]
            self.assertNotIn("settings", read_collection_completion("table", "evidence", include_settings=True))
            table.revisions = [3, 4]
            changed = read_collection_completion("table", "evidence")
            self.assertEqual(changed["status"], "CHANGED_DURING_READ")
            self.assertNotIn("source_revision", changed)
            table.revisions = [None, None]
            untracked = read_collection_completion("table", "evidence")
            self.assertEqual(untracked["status"], "REVISION_UNVERIFIED")
            self.assertNotIn("source_revision", untracked)
            table.revisions = [2, 2]
            self.assertEqual(read_collection_completion("table", "evidence")["status"], "REVISION_UNVERIFIED")


class TimelineLinkTests(unittest.TestCase):
    def test_exact_job_span_is_required(self):
        from foodtech_business_api.recommendation_evidence import _jeyuk_timeline_link
        job = {"actual_placed_at": "2026-01-01T00:00:00Z", "actual_lifted_at": "2026-01-01T00:00:03Z",
               "actual_cooking_seconds": 3}
        timeline = {"status": "MATCHED_AT_READ", "sample_timeline": {"status": "RECORDED",
                    "first_at": "2026-01-01T00:00:01Z", "last_at": "2026-01-01T00:00:03Z", "sample_count": 3}}
        self.assertEqual(_jeyuk_timeline_link(job, timeline), "MATCHED")
        timeline["sample_timeline"]["last_at"] = "2026-01-01T00:00:04Z"
        self.assertEqual(_jeyuk_timeline_link(job, timeline), "NOT_MATCHED")
        timeline["status"] = "REVISION_UNVERIFIED"
        self.assertEqual(_jeyuk_timeline_link(job, timeline), "NOT_ASSESSED")
        self.assertEqual(_jeyuk_timeline_link(job, None), "NOT_ASSESSED")


class ProcessObservationTests(unittest.TestCase):
    def test_only_recorded_synthetic_samples_produce_jam_observation(self):
        tracker = JeyukCompletionTracker(0)
        rows = []
        for second in range(1, 7):
            value = json.loads(sample(second)); value.update(actualRpm=20, motorLoadPercent=95)
            raw = json.dumps(value); tracker.record(raw)
            rows.append({"pk": "JEYUK#evidence", "sk": f"RAW#{second:06d}", "raw": raw,
                         "provenance": "SYNTHETIC"})
        receipt = tracker.finish(cursor=6, complete=True, explicit=True)
        class Table:
            name = "table"
            def __init__(self): self.meta = self; self.client = self; self.calls = 0
            def get_item(self, **_kwargs): return {"Item": {"revision": 6}}
            def get_paginator(self, _operation): return self
            def paginate(self, **_kwargs):
                self.calls += 1
                return [{"Items": [receipt] if self.calls == 1 else rows}]
        table = Table()
        class Session:
            def __init__(self, **_kwargs): pass
            def resource(self, _service): return self
            def Table(self, _name): return table
        with patch("foodtech_business_api.jeyuk_source_evidence.boto3.Session", Session):
            result = read_collection_completion("table", "evidence", include_settings=True)
        self.assertEqual(result["sample_timeline"]["status"], "NOT_RECORDED")
        self.assertEqual(result["process_observations"]["reason_codes"], ["AGITATOR_JAM_WARNING"])
        self.assertEqual(result["process_observations"]["basis"], "SYNTHETIC_SAMPLES_ONLY")
