import csv
import io
import json
from pathlib import Path
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from foodtech_contracts import decode_record
from foodtech_contracts.csv302 import parse_record
from foodtech_collection_adapter.mqtt import accept_message, topic_for
from foodtech_standardization_worker.process import assess

ROOT = Path(__file__).resolve().parents[3]

class CsvMqttContractTests(unittest.TestCase):
    def raw(self, vendor):
        return json.loads((ROOT / f'fixtures/mockprofile-302-{vendor}-001.json').read_text())['records'][0]

    def test_vendor_conversion_and_source_only_extra_column(self):
        for vendor in 'abc':
            raw = self.raw(vendor)
            header, row = list(csv.reader(io.StringIO(raw)))
            header.append('future_field'); row.append('future-value')
            output = io.StringIO(newline=''); writer = csv.writer(output, lineterminator='\n')
            writer.writerows([header, row]); raw = output.getvalue()
            record = decode_record(raw)
            self.assertEqual(record['deviceId'], f'mockprofile-302-{vendor}-001')
            self.assertEqual(assess('evt', raw).quality_status, 'VALID')
            self.assertNotIn('future_field', record)

    def test_formula_cells_including_legitimate_negative_are_quarantined(self):
        for value in ('=1+1', '+1', '-1', '@cmd', '\ttext', '\rtext'):
            header, row = list(csv.reader(io.StringIO(self.raw('a'))))
            row[8] = value
            output = io.StringIO(newline=''); csv.writer(output, lineterminator='\n').writerows([header, row])
            raw = output.getvalue().encode()
            instance = 'mockprofile-302-a-001'
            client, sink, ingestor = Mock(), Mock(), Mock()
            client.ack.return_value = 0
            msg = SimpleNamespace(topic=topic_for(instance), qos=1, retain=False, payload=raw, mid=10)
            self.assertIsNone(accept_message(client, msg, ingestor, sink, 'test', instance))
            sink.preserve_mqtt.assert_called_once()
            self.assertEqual(sink.preserve_mqtt.call_args.kwargs['profile_id'], 'mockprofile-302')
            ingestor.ingest.assert_not_called(); client.ack.assert_called_once()

    def test_failed_quarantine_never_acknowledges(self):
        raw = self.raw('a').replace(',180.0,', ',-300,').encode()
        instance = 'mockprofile-302-a-001'
        msg = SimpleNamespace(topic=topic_for(instance), qos=1, retain=False, payload=raw, mid=10)
        client, sink = Mock(), Mock(); sink.preserve_mqtt.side_effect = RuntimeError('store failed')
        with self.assertRaises(RuntimeError): accept_message(client, msg, Mock(), sink, 'test', instance)
        client.ack.assert_not_called()

    def test_malformed_and_spoofed_inputs(self):
        raw = self.raw('a')
        with self.assertRaises(ValueError): parse_record(raw + 'extra,row\n')
        with self.assertRaises(ValueError): parse_record(raw.replace('sourceId,deviceId', 'sourceId,sourceId'))
        with self.assertRaises(ValueError): decode_record(json.dumps({'deviceId': 'mockprofile-302-a-001'}))
