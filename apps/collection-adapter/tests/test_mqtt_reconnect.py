import unittest
from types import SimpleNamespace
from unittest.mock import patch

from foodtech_collection_adapter import mqtt as collector


class MqttReconnectTests(unittest.TestCase):
    def test_lost_session_is_not_treated_as_complete(self):
        class Client:
            def __init__(self, *args, **kwargs): self.loops = 0; self.reconnects = 0
            def username_pw_set(self, *args): pass
            def tls_set_context(self, *args): pass
            def connect(self, *args, **kwargs): return 0
            def subscribe(self, *args, **kwargs): return 0
            def disconnect(self): pass
            def reconnect(self): self.reconnects += 1; return 0
            def loop(self, timeout):
                self.loops += 1
                if self.loops == 1:
                    self.on_connect(self, None, SimpleNamespace(session_present=False),
                                    SimpleNamespace(is_failure=False), None)
                    return collector.mqtt.MQTT_ERR_SUCCESS
                if self.loops == 2: return collector.mqtt.MQTT_ERR_CONN_LOST
                self.on_connect(self, None, SimpleNamespace(session_present=False),
                                SimpleNamespace(is_failure=False), None)
                return collector.mqtt.MQTT_ERR_SUCCESS
        client = Client()
        with patch.object(collector.mqtt, "Client", return_value=client), \
             patch.object(collector.ssl, "create_default_context", return_value=object()):
            with self.assertRaisesRegex(RuntimeError, "persistent session lost"):
                collector.collect("localhost", 8883, "ca", "user", "password", "foodtech-collector-test",
                                  object(), count=1, timeout_seconds=5)
        self.assertEqual(client.reconnects, 1)
