import importlib.util
import ipaddress
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.x509.verification import PolicyBuilder, Store, VerificationError

path = Path(__file__).resolve().parents[2] / 'infra/scripts/private_rest_material.py'
spec = importlib.util.spec_from_file_location('private_rest_material', path)
material = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = material
spec.loader.exec_module(material)


class PrivateRestMaterialTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.now = datetime.now(timezone.utc).replace(microsecond=0)
        cls.value = material.generate(cls.now)
        cls.ca = x509.load_pem_x509_certificate(cls.value.collector['ca_pem'].encode())
        cls.server = x509.load_pem_x509_certificate(cls.value.mock['server_cert_pem'].encode())

    def verifier(self, address, *, at=None):
        return (PolicyBuilder().store(Store([self.ca])).time(at or self.now)
                .build_server_verifier(x509.IPAddress(ipaddress.ip_address(address))))

    def test_real_chain_accepts_only_the_reviewed_usable_private_addresses(self):
        for address in material.SERVER_ADDRESSES:
            with self.subTest(address=str(address)):
                chain = self.verifier(str(address)).verify(self.server, [])
                self.assertEqual(len(chain), 2)
        for address in ('10.42.0.32', '10.42.0.35', '10.42.0.63', '10.42.0.1', '127.0.0.1', '8.8.8.8'):
            with self.subTest(address=address), self.assertRaises(VerificationError):
                self.verifier(address).verify(self.server, [])

    def test_expired_chain_is_rejected_and_lifetime_is_bounded(self):
        self.assertEqual(self.value.expires_at, self.now + timedelta(hours=3))
        with self.assertRaises(VerificationError):
            self.verifier('10.42.0.40', at=self.value.expires_at + timedelta(seconds=1)).verify(self.server, [])

    def test_secret_boundaries_key_match_and_safe_metadata(self):
        value = self.value
        self.assertEqual(set(value.mock), {'ca_pem', 'server_cert_pem', 'server_key_pem', 'client_id', 'client_secret'})
        self.assertEqual(set(value.collector), {'ca_pem', 'client_id', 'client_secret'})
        self.assertTrue(value.mock['client_secret'] == value.collector['client_secret'], 'OAuth pair differs')
        key = serialization.load_pem_private_key(value.mock['server_key_pem'].encode(), password=None)
        self.assertEqual(key.public_key().public_numbers(), self.server.public_key().public_numbers())
        self.server.verify_directly_issued_by(self.ca)
        public = repr(value) + json.dumps(value.metadata())
        for secret in (value.mock['server_key_pem'], value.mock['client_secret']):
            self.assertTrue(secret not in public, 'sensitive material leaked into public metadata')
        self.assertEqual(value.metadata()['subject_alt_ip_count'], 27)

    def test_generation_never_reuses_fixture_seed_for_keys_or_oauth_secret(self):
        other = material.generate(self.now)
        self.assertTrue(other.mock['client_secret'] != self.value.mock['client_secret'], 'OAuth material was reused')
        self.assertTrue(other.mock['server_key_pem'] != self.value.mock['server_key_pem'], 'server key was reused')
        self.assertNotEqual(other.server_fingerprint, self.value.server_fingerprint)

    def test_ambiguous_naive_clock_is_rejected(self):
        with self.assertRaises(ValueError):
            material.generate(datetime(2026, 11, 27))

    def test_resume_recovers_same_pair_without_a_ca_private_key(self):
        recovered = material.restore(self.value.mock, self.now)
        self.assertTrue(recovered.collector == self.value.collector, 'resumed client material differs')
        self.assertEqual(recovered.metadata(), self.value.metadata())
        for key, value in (('client_id', 'other'), ('client_secret', ''), ('server_key_pem', 'invalid'),
                           ('ca_pem', material.generate(self.now).collector['ca_pem'])):
            with self.subTest(key=key), self.assertRaises((ValueError, TypeError, VerificationError)):
                material.restore({**self.value.mock, key: value}, self.now)


if __name__ == '__main__':
    unittest.main()
