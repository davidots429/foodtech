import importlib.util
from pathlib import Path
import shutil
import socket
import ssl
import sys
import tempfile
from threading import Thread
import time
import unittest
from unittest.mock import patch

from fastapi import FastAPI
import uvicorn

from foodtech_ai_gateway.serve_tonkatsu import tonkatsu_server_config
from foodtech_ai_gateway.serve_tonkatsu_business import business_server_config
from foodtech_ai_gateway.serve_tonkatsu_inference import inference_server_config
from foodtech_business_api.combi_broker_tls import broker_server_config

ROOT = Path(__file__).resolve().parents[2]
PROBE = ROOT / "infra/containers/tonkatsu_transport_probe.py"
SPEC = importlib.util.spec_from_file_location("tonkatsu_transport_probe", PROBE)
assert SPEC and SPEC.loader
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)
with patch.object(sys, "path", [str(ROOT / "infra/scripts"), *sys.path]):
    from tonkatsu_trial_tls_material import generate


class TonkatsuTransportProbeTests(unittest.TestCase):
    def test_probe_rejects_workload_credentials_before_connecting(self):
        with self.assertRaisesRegex(probe.ProbeFailed, "without workload credentials"):
            probe.probe({"AWS_CONTAINER_CREDENTIALS_RELATIVE_URI": "/synthetic"}, [])

    def test_probe_rejects_an_unreviewed_origin_before_loading_certificates(self):
        with self.assertRaisesRegex(probe.ProbeFailed, "unexpected private origin"):
            probe.probe({"FOODTECH_SERVICE": "tonkatsu-broker",
                         "FOODTECH_TONKATSU_GATEWAY_URL": "https://unreviewed.invalid:8445"}, [])

    def test_four_native_listeners_accept_pinned_clients_and_reject_mismatched_leaf(self):
        material = generate(**{f"{name}_host": f"{name}.{probe.DOMAIN}"
                               for name in ("broker", "gateway", "records", "inference")})
        staging = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, staging)

        def save(role, bundle):
            directory = staging / role
            directory.mkdir(mode=0o700)
            paths = {}
            for key, content in bundle.items():
                path = directory / key
                path.write_text(content)
                path.chmod(0o600)
                paths[key.removesuffix("_pem")] = str(path)
            return paths

        bff = save("bff", material.business_api)
        broker = save("broker", material.broker)
        gateway = save("gateway", material.gateway)
        inference = save("inference", material.inference)
        configs = {
            "broker": broker_server_config(FastAPI(), recipe="tonkatsu", server_cert=broker["broker_server_cert"],
                server_key=broker["broker_server_key"], bff_ca=broker["bff_ca"], bff_certificate=broker["bff_cert"]),
            "gateway": tonkatsu_server_config(FastAPI(), server_cert=gateway["server_cert"],
                server_key=gateway["server_key"], broker_ca=gateway["broker_ca"], broker_certificate=gateway["broker_cert"]),
            "records": business_server_config(server_cert=gateway["records_server_cert"],
                server_key=gateway["records_server_key"], client_ca=gateway["records_client_ca"],
                client_certificate=gateway["records_client_cert"]),
            "inference": inference_server_config(server_cert=inference["inference_server_cert"],
                server_key=inference["inference_server_key"], client_ca=inference["inference_client_ca"],
                client_certificate=inference["inference_client_cert"]),
        }
        ports = {}
        for name, configuration in configs.items():
            listener = socket.socket()
            self.addCleanup(listener.close)
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            ports[f"{name}.{probe.DOMAIN}"] = listener.getsockname()[1]
            server = uvicorn.Server(configuration)
            thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
            thread.start()
            self.addCleanup(self.stop_server, server, thread)
            stop = time.monotonic() + 5
            while not server.started and thread.is_alive() and time.monotonic() < stop:
                time.sleep(0.01)
            self.assertTrue(server.started)

        cases = {
            "tonkatsu-business-api": {
                "BROKER_SERVER_CERT": bff["broker_server_cert"], "BFF_CERT": bff["bff_cert"], "BFF_KEY": bff["bff_key"],
                "GATEWAY_RECORDS_SERVER_CERT": bff["records_server_cert"],
                "GATEWAY_RECORDS_CLIENT_CERT": bff["records_client_cert"],
                "GATEWAY_RECORDS_CLIENT_KEY": bff["records_client_key"],
            },
            "tonkatsu-broker": {"GATEWAY_SERVER_CERT": broker["gateway_server_cert"],
                               "BROKER_CLIENT_CERT": broker["broker_client_cert"], "BROKER_CLIENT_KEY": broker["broker_client_key"]},
            "tonkatsu-gateway": {"OLLAMA_SERVER_CERT": gateway["inference_server_cert"],
                                "OLLAMA_CLIENT_CERT": gateway["inference_client_cert"], "OLLAMA_CLIENT_KEY": gateway["inference_client_key"]},
        }
        getaddrinfo = socket.getaddrinfo

        def resolve(host, port, *args, **kwargs):
            self.assertIn(host, ports)
            return getaddrinfo("127.0.0.1", ports[host], *args, **kwargs)

        with patch.object(socket, "getaddrinfo", side_effect=resolve):
            for role, values in cases.items():
                env = {"FOODTECH_SERVICE": role, **{probe.PREFIX + key: val for key, val in values.items()}}
                for lane, port, key, *_ in probe.LANES[role]:
                    env[probe.PREFIX + key] = f"https://{lane}.{probe.DOMAIN}:{port}"
                completed = []
                probe.probe(env, completed)
                self.assertEqual(completed, [lane[0] for lane in probe.LANES[role]])

            ctx, fingerprint = probe.pinned_context(broker["gateway_server_cert"], bff["records_client_cert"],
                                                    bff["records_client_key"])
            with self.assertRaises(ssl.SSLCertVerificationError):
                probe.probe_peer(f"records.{probe.DOMAIN}", 8447, ctx, fingerprint)

            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
            ctx.load_verify_locations(cafile=bff["records_server_cert"])
            _, records_pin = probe.pinned_context(bff["records_server_cert"], bff["records_client_cert"],
                                                  bff["records_client_key"])
            with self.assertRaises((OSError, probe.HTTPException)):
                probe.probe_peer(f"records.{probe.DOMAIN}", 8447, ctx, records_pin)

    def stop_server(self, server, thread):
        server.should_exit = True
        thread.join(5)
        self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
