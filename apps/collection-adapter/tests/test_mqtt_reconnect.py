import unittest
import ssl
from types import SimpleNamespace
from unittest.mock import patch

from foodtech_collection_adapter import mqtt as collector


class MqttReconnectTests(unittest.TestCase):
    def test_initial_reset_retries_before_accepting_messages(self):
        class Client:
            def __init__(self): self.attempts = 0; self.closed = False
            def username_pw_set(self, *args): pass
            def tls_set_context(self, *args): pass
            def connect(self, *args, **kwargs):
                self.attempts += 1
                if self.attempts == 1: raise ConnectionResetError('reset')
                return 0
            def disconnect(self): self.closed = True
            def loop(self, timeout):
                self.on_message(self, None, SimpleNamespace(mid=1, payload=b'fixture', dup=False))
                return collector.mqtt.MQTT_ERR_SUCCESS
        client = Client()
        with patch.object(collector.mqtt, 'Client', return_value=client), \
             patch.object(collector.ssl, 'create_default_context', return_value=object()), \
             patch.object(collector, 'accept_message', return_value=None), \
             patch.object(collector.time, 'sleep'):
            self.assertEqual(collector.collect('localhost', 8883, 'ca', 'user', 'password',
                'foodtech-collector-test', object(), count=1, timeout_seconds=5), 1)
        self.assertEqual(client.attempts, 2)
        self.assertTrue(client.closed)

    def test_initial_failure_deadline_and_tls_verification_are_not_bypassed(self):
        clock = [0.0]
        class Client:
            def __init__(self, error): self.error = error; self.attempts = 0; self.closed = False
            def username_pw_set(self, *args): pass
            def tls_set_context(self, *args): pass
            def connect(self, *args, **kwargs): self.attempts += 1; raise self.error
            def disconnect(self): self.closed = True
        def sleep(seconds): clock[0] += seconds
        for error, expected in ((ConnectionRefusedError('offline'), TimeoutError),
                                (ssl.SSLCertVerificationError('bad certificate'), ssl.SSLCertVerificationError)):
            clock[0] = 0
            client = Client(error)
            with self.subTest(error=type(error).__name__), \
                 patch.object(collector.mqtt, 'Client', return_value=client), \
                 patch.object(collector.ssl, 'create_default_context', return_value=object()), \
                 patch.object(collector.time, 'monotonic', side_effect=lambda: clock[0]), \
                 patch.object(collector.time, 'sleep', side_effect=sleep):
                with self.assertRaises(expected):
                    collector.collect('localhost', 8883, 'ca', 'user', 'password',
                        'foodtech-collector-test', object(), count=1, timeout_seconds=1)
            self.assertTrue(client.closed)
            if isinstance(error, ssl.SSLError): self.assertEqual(client.attempts, 1)
            else: self.assertGreater(client.attempts, 1)

    def test_known_qos_retransmission_is_persisted_but_does_not_complete_collection(self):
        class Client:
            def __init__(self, *args, **kwargs): self.index = 0
            def username_pw_set(self, *args): pass
            def tls_set_context(self, *args): pass
            def connect(self, *args, **kwargs): return 0
            def subscribe(self, *args, **kwargs): return 0
            def disconnect(self): pass
            def loop(self, timeout):
                messages = [(7, b"first", False), (7, b"first", True), (8, b"second", False)]
                mid, payload, dup = messages[self.index]; self.index += 1
                self.on_message(self, None, SimpleNamespace(mid=mid, payload=payload, dup=dup))
                return collector.mqtt.MQTT_ERR_SUCCESS
        client = Client()
        with patch.object(collector.mqtt, "Client", return_value=client), \
             patch.object(collector.ssl, "create_default_context", return_value=object()), \
             patch.object(collector, "accept_message", return_value=None) as accept:
            self.assertEqual(collector.collect("localhost", 8883, "ca", "user", "password",
                "foodtech-collector-test", object(), count=2, timeout_seconds=5), 2)
        self.assertEqual(client.index, 3)
        self.assertEqual(accept.call_count, 3)

    def test_resequencer_is_drained_while_connection_is_down(self):
        clock = [0.0]
        class Client:
            def __init__(self, *args, **kwargs): self.loops = 0
            def username_pw_set(self, *args): pass
            def tls_set_context(self, *args): pass
            def connect(self, *args, **kwargs): return 0
            def subscribe(self, *args, **kwargs): return 0
            def disconnect(self): pass
            def reconnect(self): raise OSError("offline")
            def loop(self, timeout):
                self.loops += 1; clock[0] += .6
                if self.loops == 1:
                    self.on_connect(self, None, SimpleNamespace(session_present=False),
                                    SimpleNamespace(is_failure=False), None)
                    self.on_message(self, None, SimpleNamespace(mid=1, payload=b"sample", dup=False))
                    return collector.mqtt.MQTT_ERR_SUCCESS
                return collector.mqtt.MQTT_ERR_CONN_LOST
        class Ingestor:
            def __init__(self): self.drains = []
            def drain(self, stream): self.drains.append(stream)
        client, ingestor = Client(), Ingestor()
        def sleep(seconds): clock[0] += seconds
        with patch.object(collector.mqtt, "Client", return_value=client), \
             patch.object(collector.ssl, "create_default_context", return_value=object()), \
             patch.object(collector, "accept_message", return_value="STREAM"), \
             patch.object(collector.time, "monotonic", side_effect=lambda: clock[0]), \
             patch.object(collector.time, "sleep", side_effect=sleep):
            with self.assertRaises(TimeoutError):
                collector.collect("localhost", 8883, "ca", "user", "password", "foodtech-collector-test",
                                  ingestor, count=2, timeout_seconds=4)
        self.assertGreaterEqual(len(ingestor.drains), 2)
        self.assertTrue(all(stream == "STREAM" for stream in ingestor.drains))

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
