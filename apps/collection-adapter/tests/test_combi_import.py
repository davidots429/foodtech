import json
import itertools
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx

from foodtech_collection_adapter.combi_import import _api_url, import_meal
from foodtech_collection_adapter.rest import PollResult

MEAL = "12345678-1234-4234-8234-123456789abc"


def line(sequence, phase, job):
    return json.dumps({"eventId": f"{MEAL}-{sequence:06d}", "mealBatchId": MEAL,
        "cookingJobId": job, "deviceId": "combi-1", "mockInstanceId": "mockprofile-120-a-001",
        "eventType": "OBSERVATION", "occurredAt": f"2026-01-01T00:00:0{sequence - 1}Z",
        "receivedAt": f"2026-01-01T00:00:0{sequence - 1}Z", "sequence": sequence,
        "schemaVersion": "synthetic-combi-v1", "programVersion": "v1", "provenance": "SYNTHETIC",
        "dataQuality": "VALID", "phase": phase, "phaseSecond": 1, "chamberCelsius": 100,
        "climateSetpointPercent": 100, "measuredHumidityPercent": 98,
        "humidityValueSource": "MEASURED", "fanLevel": 4, "doorState": "CLOSED"})


class ImportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.posts = []
        self.meal = {"meal_batch_id": MEAL, "status": "PREPARING_RICE", "version": 1,
                     "rice_cooking_job_id": "rice", "mode_transition_id": "transition",
                     "mackerel_cooking_job_id": "fish"}
        def handler(request):
            if request.method == "GET":
                return httpx.Response(200, json={"meal": self.meal})
            self.posts.append(request)
            return httpx.Response(201, json={"meal": {"source": {"record_count": 3}}})
        self.api = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://test")
        self.addAsyncCleanup(self.api.aclose)
        self.records = (line(1, "RICE", "rice"), line(2, "TRANSITION", "transition"),
                        line(3, "MACKEREL", "fish"))

    async def test_imports_only_complete_stream_with_csrf_and_snapshot_version(self):
        mock = Mock()
        mock.poll.side_effect = [PollResult(self.records[:1], 1), PollResult(self.records[1:], 3), PollResult((), 3)]
        source = await import_meal(mock, self.api, MEAL, "combi-1", "csrf")
        self.assertEqual(source["record_count"], 3)
        self.assertEqual(self.posts[0].headers["X-CSRF-Token"], "csrf")
        self.assertEqual(json.loads(self.posts[0].content)["expected_version"], 1)

    async def test_waits_for_pending_stream_before_submitting(self):
        mock = Mock()
        mock.poll.side_effect = [PollResult((), 0, False), PollResult(self.records[:1], 1, False),
                                 PollResult((), 1, False), PollResult(self.records[1:], 3, True),
                                 PollResult((), 3, True)]
        with patch("foodtech_collection_adapter.combi_import.asyncio.sleep", new_callable=AsyncMock) as sleep:
            source = await import_meal(mock, self.api, MEAL, "combi-1", "csrf")
        self.assertEqual(source["record_count"], 3)
        self.assertEqual(sleep.await_count, 2)

    async def test_finalizes_using_latest_meal_version(self):
        mock = Mock()
        def poll(cursor):
            if cursor == 0:
                return PollResult(self.records, 3)
            self.meal["version"] = 2
            self.meal["status"] = "RICE_COOKING"
            return PollResult((), 3)
        mock.poll.side_effect = poll
        await import_meal(mock, self.api, MEAL, "combi-1", "csrf")
        self.assertEqual(json.loads(self.posts[-1].content)["expected_version"], 2)

    async def test_pending_stream_timeout_never_submits_partial_records(self):
        mock = Mock()
        mock.poll.side_effect = itertools.chain([PollResult(self.records[:1], 1, False)],
                                                itertools.repeat(PollResult((), 1, False)))
        with self.assertRaises(TimeoutError):
            await import_meal(mock, self.api, MEAL, "combi-1", "csrf", timeout_seconds=0.001)
        self.assertFalse(self.posts)

    async def test_foreign_or_bad_cursor_is_not_submitted(self):
        for records, cursor in ((self.records, 4), ((self.records[0], self.records[1],
                                 line(3, "MACKEREL", "foreign")), 3)):
            mock = Mock()
            mock.poll.return_value = PollResult(records, cursor)
            with self.assertRaises(ValueError):
                await import_meal(mock, self.api, MEAL, "combi-1", "csrf")
            self.assertFalse(self.posts)

    async def test_existing_source_is_not_polled(self):
        self.meal["source"] = {"record_count": 3}
        mock = Mock()
        with self.assertRaises(ValueError):
            await import_meal(mock, self.api, MEAL, "combi-1", "csrf")
        mock.poll.assert_not_called()

    def test_api_origin_restrictions(self):
        self.assertEqual(_api_url("http://localhost:8000"), "http://localhost:8000")
        for url in ("http://example.com", "https://user:pass@example.com", "https://example.com/path"):
            with self.assertRaises(ValueError):
                _api_url(url)
