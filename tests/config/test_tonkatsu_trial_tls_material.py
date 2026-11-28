"""Check the role bundles and certificate identities used in the trial."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.x509.oid import ExtendedKeyUsageOID
from cryptography.x509.verification import PolicyBuilder, Store, VerificationError

ROOT = Path(__file__).resolve().parents[2]
with patch.object(sys, "path", [str(ROOT / "infra/scripts"), *sys.path]):
    from tonkatsu_trial_tls_material import generate


def certificate(value):
    return x509.load_pem_x509_certificate(value.encode("ascii"))


class TonkatsuTrialTlsMaterialTests(unittest.TestCase):
    def test_role_bundles_bind_private_hosts_and_exact_client_identities(self):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        material = generate(broker_host="broker.foodtech.internal", gateway_host="gateway.foodtech.internal", now=now)
        bff, broker, gateway = material.business_api, material.broker, material.gateway
        self.assertEqual(set(bff), {"broker_server_cert_pem", "bff_cert_pem", "bff_key_pem"})
        self.assertEqual(set(broker), {"broker_server_cert_pem", "broker_server_key_pem", "bff_ca_pem",
                                       "bff_cert_pem", "gateway_server_cert_pem", "broker_client_cert_pem",
                                       "broker_client_key_pem"})
        self.assertEqual(set(gateway), {"server_cert_pem", "server_key_pem", "broker_ca_pem", "broker_cert_pem"})
        self.assertEqual(bff["broker_server_cert_pem"], broker["broker_server_cert_pem"])
        self.assertEqual(bff["bff_cert_pem"], broker["bff_cert_pem"])
        self.assertEqual(gateway["server_cert_pem"], broker["gateway_server_cert_pem"])
        self.assertEqual(gateway["broker_cert_pem"], broker["broker_client_cert_pem"])

        cases = (
            (broker["bff_ca_pem"], "broker.foodtech.internal", "urn:foodtech:service:business-api",
             broker["broker_server_cert_pem"], broker["broker_server_key_pem"], bff["bff_cert_pem"], bff["bff_key_pem"]),
            (gateway["broker_ca_pem"], "gateway.foodtech.internal", "urn:foodtech:service:tonkatsu-read-broker",
             gateway["server_cert_pem"], gateway["server_key_pem"], broker["broker_client_cert_pem"],
             broker["broker_client_key_pem"]),
        )
        unique_keys = set()
        for ca_pem, host, uri, server_pem, server_key, client_pem, client_key in cases:
            ca, server, client = map(certificate, (ca_pem, server_pem, client_pem))
            self.assertEqual(client.extensions.get_extension_for_class(x509.SubjectAlternativeName).value,
                             x509.SubjectAlternativeName([x509.UniformResourceIdentifier(uri)]))
            self.assertEqual(list(client.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value),
                             [ExtendedKeyUsageOID.CLIENT_AUTH])
            verifier = PolicyBuilder().store(Store([ca])).time(now)
            verifier.build_server_verifier(x509.DNSName(host)).verify(server, [])
            verifier.build_client_verifier().verify(client, [])
            with self.assertRaises(VerificationError):
                verifier.build_server_verifier(x509.DNSName("someone-else.internal")).verify(server, [])
            for leaf, key in ((server, server_key), (client, client_key)):
                private = serialization.load_pem_private_key(key.encode("ascii"), password=None)
                self.assertEqual(leaf.public_key().public_numbers(), private.public_key().public_numbers())
                self.assertEqual(leaf.not_valid_after_utc, material.expires_at)
                unique_keys.add(key)
        self.assertEqual(len(unique_keys), 4)
        self.assertNotEqual(broker["bff_ca_pem"], gateway["broker_ca_pem"])
        wrong_ca = PolicyBuilder().store(Store([certificate(broker["bff_ca_pem"])])).time(now).build_client_verifier()
        with self.assertRaises(VerificationError):
            wrong_ca.verify(certificate(gateway["broker_cert_pem"]), [])
        self.assertEqual(material.expires_at, now + timedelta(hours=3))
        self.assertNotIn("PRIVATE KEY", repr(material))

    def test_invalid_names_and_ambiguous_clock_are_rejected(self):
        for host in ("public.example", "https://gateway.internal", "127.0.0.1", "a" * 64 + ".internal", "broker.internal"):
            with self.subTest(host=host), self.assertRaises(ValueError):
                generate(broker_host="broker.internal", gateway_host=host)
        with self.assertRaisesRegex(ValueError, "aware clock"):
            generate(broker_host="broker.internal", gateway_host="gateway.internal", now=datetime(2026, 11, 28))
