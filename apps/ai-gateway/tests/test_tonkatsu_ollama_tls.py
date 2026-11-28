import asyncio
from pathlib import Path
import socket
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


from combi_tls_fixture import certificates
from foodtech_ai_gateway.ollama import OllamaClient, OllamaFailure, OllamaResult
from foodtech_ai_gateway.recommendation_generation import _FIELDS, _schema, _SYSTEM, TonkatsuBaseline
from foodtech_ai_gateway.serve_tonkatsu_inference import CLIENT_URI, inference_server_config
from foodtech_ai_gateway.tonkatsu_invocation import configured_tonkatsu_ollama
from foodtech_ai_gateway.tonkatsu_ollama_tls import TonkatsuInferenceTLS
from test_combi_pinned_tls import listening

HOST = "ollama.foodtech.internal"
MODEL = "llama3.1:8b"
PREFIX = "FOODTECH_TONKATSU_OLLAMA_"


class TonkatsuOllamaTlsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = self.enterContext(TemporaryDirectory())
        self.certs = await asyncio.to_thread(certificates, Path(temporary), server_dns=HOST, broker_uri=CLIENT_URI)
        self.tls = TonkatsuInferenceTLS(self.certs["server"][0], *self.certs["broker"])
        self.seen = []
        self.messages = [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": "{}"}]
        self.schema = _schema(_FIELDS[TonkatsuBaseline])

    async def structured_chat(self, model, messages, schema):
        self.seen.append((model, messages, schema))
        return OllamaResult('{"example":1}', 2, 3)

    def configuration(self, app, *, server_cert, server_key, broker_ca, broker_certificate, port):
        return inference_server_config(server_cert=server_cert, server_key=server_key, client_ca=broker_ca,
                                       client_certificate=broker_certificate, port=port, ollama=self)

    async def test_private_chat_uses_native_mtls_and_reviewed_leaf(self):
        lookup = socket.getaddrinfo
        def resolve(host, *args, **kwargs):
            if host in (HOST, HOST.encode()):
                host = "127.0.0.1"
            return lookup(host, *args, **kwargs)

        async with listening(None, self.certs, server_config=self.configuration) as (url, _):
            with patch.object(socket, "getaddrinfo", side_effect=resolve):
                client = OllamaClient(url.replace("127.0.0.1", HOST), private_tls=self.tls, timeout=5)
                result = await client.structured_chat(MODEL, self.messages, self.schema)
                self.assertEqual(result.text, '{"example":1}')
                with self.assertRaisesRegex(OllamaFailure, "PRIVATE_REQUEST"):
                    await client.generate(MODEL, "do not send")
                with self.assertRaisesRegex(OllamaFailure, "PRIVATE_REQUEST"):
                    await client.structured_chat("unreviewed", [], {})
                with self.assertRaisesRegex(OllamaFailure, "OLLAMA_HTTP_ERROR"):
                    await client.structured_chat(MODEL, self.messages, {})
                unpinned = TonkatsuInferenceTLS(self.certs["server"][0], *self.certs["other_broker"])
                with self.assertRaisesRegex(OllamaFailure, "OLLAMA_UNAVAILABLE|OLLAMA_TRANSPORT_ERROR"):
                    await OllamaClient(url.replace("127.0.0.1", HOST), private_tls=unpinned, timeout=5).structured_chat(
                        MODEL, self.messages, self.schema,
                    )
        async with listening(None, self.certs, server_config=self.configuration, other_server=True) as (url, _):
            with patch.object(socket, "getaddrinfo", side_effect=resolve):
                with self.assertRaisesRegex(OllamaFailure, "OLLAMA_UNAVAILABLE"):
                    await OllamaClient(url.replace("127.0.0.1", HOST), private_tls=self.tls, timeout=5).structured_chat(
                        MODEL, self.messages, self.schema,
                    )
        self.assertEqual(self.seen, [(MODEL, self.messages, self.schema)])

    async def test_bad_private_settings_cannot_select_loopback(self):
        with patch.dict("os.environ", {PREFIX + "PRIVATE_ORIGIN": f"https://{HOST}:8448"}, clear=True):
            with self.assertRaises(ValueError):
                configured_tonkatsu_ollama()
        for origin in ("http://ollama.foodtech.internal:8448", "https://example.com:8448",
                       "https://other.foodtech.internal:8448", f"https://{HOST}:8448/#fragment"):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                OllamaClient(origin, private_tls=self.tls)
