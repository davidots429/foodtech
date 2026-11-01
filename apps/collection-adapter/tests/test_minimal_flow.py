import json
import unittest
from pathlib import Path

import httpx
import jsonschema

from foodtech_collection_adapter.resequence import Received, Resequencer
from foodtech_collection_adapter.rest import RestMockClient
from foodtech_contracts import canonical_record, decode_record, payload_hash
from foodtech_standardization_worker import assess

FIXTURE = json.loads((Path(__file__).resolve().parents[3] / "fixtures/mockprofile-100-a-001.json").read_text())


class MinimalFlowTests(unittest.TestCase):
    def records(self):
        return [json.dumps(item, separators=(",", ":")) for item in FIXTURE["records"]]

    def test_schema_hash_and_quality(self):
        records = self.records()
        self.assertEqual([assess(str(index), raw).quality_status for index, raw in enumerate(records)],
                         ["VALID", "QUARANTINED", "VALID"])
        valid = decode_record(records[0])
        self.assertEqual(canonical_record(valid)["temperature_celsius"], "180")
        reordered = dict(reversed(list(valid.items())))
        self.assertEqual(payload_hash(valid), payload_hash(reordered))
        with self.assertRaises(ValueError):
            decode_record('{"schemaVersion":"1.0","schemaVersion":"1.0"}')
        with self.assertRaises(jsonschema.ValidationError):
            decode_record(records[0].replace('"recordId":"rec-a-001",', ""))

    def test_resequence_out_of_order_and_gap(self):
        raws = self.records()
        stream = Resequencer()
        def event(index, now):
            item = FIXTURE["records"][index]
            return Received(str(index), raws[index], item["sourceId"], item["deviceId"],
                            item["batchId"], item["sequenceNumber"], now)
        self.assertEqual([item.sequence_number for item in stream.accept(event(0, 0), 0)], [1])
        self.assertEqual(stream.accept(event(1, 1), 1), [])
        self.assertEqual([item.sequence_number for item in stream.accept(event(2, 2), 2)], [2, 3])
        gap = Resequencer()
        gap.accept(event(0, 0), 0)
        gap.accept(event(1, 1), 1)
        self.assertEqual(gap.tick(5), [])
        late_release = gap.tick(6)
        self.assertEqual([(item.sequence_number, item.gap_before) for item in late_release], [(3, (2, 2))])
        self.assertEqual(gap.accept(event(2, 7), 7)[0].sequence_status, "LATE_AFTER_GAP")
        self.assertEqual(assess("3", raws[1], gap_before=(2, 2)).quality_status, "QUARANTINED")

    def test_https_poll_retries_real_transport_disconnect(self):
        records = self.records()
        calls = {"records": 0}
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/oauth2/token":
                return httpx.Response(200, json={"access_token": "test-token", "token_type": "Bearer"})
            calls["records"] += 1
            if calls["records"] == 1:
                raise httpx.RemoteProtocolError("connection terminated")
            return httpx.Response(200, text="\n".join(records) + "\n", headers={
                "X-Contract-Version": "1.0", "X-Profile-ID": "mockprofile-100",
                "X-Provenance": "SYNTHETIC", "X-Next-Cursor": "3",
            })
        client = httpx.Client(base_url="https://localhost", transport=httpx.MockTransport(handler))
        poller = RestMockClient("https://localhost", "id", "secret", "test-ca", client)
        result = poller.poll(0)
        self.assertEqual(result.records, tuple(records))
        self.assertEqual(result.next_cursor, 3)
        self.assertEqual(calls["records"], 2)
        poller.close()


if __name__ == "__main__":
    unittest.main()
