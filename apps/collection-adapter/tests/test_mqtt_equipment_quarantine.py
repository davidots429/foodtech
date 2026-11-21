import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from foodtech_collection_adapter.mqtt import accept_message, topic_for


class EquipmentQuarantineTests(unittest.TestCase):
    def test_malformed_data_is_stored_under_its_profile_before_ack(self):
        for profile in ("310", "311", "312", "320", "321", "322"):
            for vendor in "abc":
                with self.subTest(profile=profile, vendor=vendor):
                    instance = f"mockprofile-{profile}-{vendor}-001"
                    message = SimpleNamespace(topic=topic_for(instance), qos=1, retain=False,
                                              payload=b"{invalid", mid=1)
                    client, sink, ingestor = Mock(), Mock(), Mock()
                    client.ack.return_value = 0
                    accept_message(client, message, ingestor, sink, "foodtech-collector-test", instance)
                    self.assertEqual(sink.preserve_mqtt.call_args.kwargs["profile_id"], f"mockprofile-{profile}")
                    ingestor.ingest.assert_not_called()
                    client.ack.assert_called_once()
                    client.reset_mock()
                    sink.preserve_mqtt.side_effect = RuntimeError("storage failed")
                    with self.assertRaisesRegex(RuntimeError, "storage failed"):
                        accept_message(client, message, ingestor, sink, "foodtech-collector-test", instance)
                    client.ack.assert_not_called()


if __name__ == "__main__": unittest.main()
