"""Build isolated, short-lived Tonkatsu trial bundles in memory."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from cryptography.x509.verification import PolicyBuilder, Store

_BROKER_ID = "urn:foodtech:service:tonkatsu-read-broker"
_BFF_ID = "urn:foodtech:service:business-api"
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"


@dataclass(frozen=True, repr=False)
class TonkatsuTrialTlsMaterial:
    business_api: dict[str, str]
    broker: dict[str, str]
    gateway: dict[str, str]
    expires_at: datetime

    def __repr__(self):
        return "TonkatsuTrialTlsMaterial(redacted)"


def _public_pem(cert):
    return cert.public_bytes(serialization.Encoding.PEM).decode("ascii")


def _private_pem(key):
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
    ).decode("ascii")


def _identity(subject, issuer, public_key, begins, expires, *, ca):
    return (x509.CertificateBuilder()
            .subject_name(subject).issuer_name(issuer).public_key(public_key)
            .serial_number(x509.random_serial_number()).not_valid_before(begins).not_valid_after(expires)
            .add_extension(x509.BasicConstraints(ca=ca, path_length=0 if ca else None), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=not ca, content_commitment=False,
                key_encipherment=False, data_encipherment=False, key_agreement=False, key_cert_sign=ca,
                crl_sign=ca, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(public_key), critical=False))


def _name(value):
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, value)])


def _matching_pair(lane, server_host, client_uri, begins, expires):
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = _name(lane)
    ca = _identity(ca_name, ca_name, ca_key.public_key(), begins, expires, ca=True).sign(ca_key, hashes.SHA256())
    authority = x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key())

    def issue(subject, usage, alt_name):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        leaf = (_identity(_name(subject), ca_name, key.public_key(), begins, expires, ca=False)
                .add_extension(x509.ExtendedKeyUsage([usage]), critical=False)
                .add_extension(x509.SubjectAlternativeName([alt_name]), critical=False)
                .add_extension(authority, critical=False)
                .sign(ca_key, hashes.SHA256()))
        return _public_pem(leaf), _private_pem(key)

    server = issue(lane + " server", ExtendedKeyUsageOID.SERVER_AUTH, x509.DNSName(server_host))
    client = issue(lane + " client", ExtendedKeyUsageOID.CLIENT_AUTH, x509.UniformResourceIdentifier(client_uri))
    return _public_pem(ca), server, client


def verify_hostnames(broker_host, gateway_host):
    for host in (broker_host, gateway_host):
        if not isinstance(host, str) or len(host) > 253 or not re.fullmatch(rf"{_LABEL}(?:[.]{_LABEL})*[.]internal", host):
            raise ValueError("reviewed private service hostname required")
    if broker_host == gateway_host:
        raise ValueError("broker and Gateway require distinct private hostnames")


def generate(*, broker_host: str, gateway_host: str, now: datetime | None = None) -> TonkatsuTrialTlsMaterial:
    """Return three bundles for a bounded trial, without serializing either CA key."""
    verify_hostnames(broker_host, gateway_host)
    if now is None:
        now = datetime.now(timezone.utc)
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("an aware clock is required")
    now = now.astimezone(timezone.utc).replace(microsecond=0)
    begins, expires = now - timedelta(minutes=5), now + timedelta(hours=3)

    bff_ca, broker_server, bff_client = _matching_pair(
        "Foodtech synthetic BFF to broker", broker_host, _BFF_ID, begins, expires,
    )
    broker_ca, gateway_server, broker_client = _matching_pair(
        "Foodtech synthetic broker to Gateway", gateway_host, _BROKER_ID, begins, expires,
    )
    business_api = {
        "broker_server_cert_pem": broker_server[0], "bff_cert_pem": bff_client[0], "bff_key_pem": bff_client[1],
    }
    broker = {
        "broker_server_cert_pem": broker_server[0], "broker_server_key_pem": broker_server[1],
        "bff_ca_pem": bff_ca, "bff_cert_pem": bff_client[0], "gateway_server_cert_pem": gateway_server[0],
        "broker_client_cert_pem": broker_client[0], "broker_client_key_pem": broker_client[1],
    }
    gateway = {
        "server_cert_pem": gateway_server[0], "server_key_pem": gateway_server[1],
        "broker_ca_pem": broker_ca, "broker_cert_pem": broker_client[0],
    }
    return TonkatsuTrialTlsMaterial(business_api, broker, gateway, expires)

def verify_bundles(business_api, broker, gateway, *, broker_host, gateway_host):
    """Return an expiry and public pins only after verifying all three recovered bundles."""
    verify_hostnames(broker_host, gateway_host)
    required = (
        (business_api, {"broker_server_cert_pem", "bff_cert_pem", "bff_key_pem"}),
        (broker, {"broker_server_cert_pem", "broker_server_key_pem", "bff_ca_pem", "bff_cert_pem",
                  "gateway_server_cert_pem", "broker_client_cert_pem", "broker_client_key_pem"}),
        (gateway, {"server_cert_pem", "server_key_pem", "broker_ca_pem", "broker_cert_pem"}),
    )
    if any(type(bundle) is not dict or set(bundle) != keys or
           any(type(value) is not str or not 1 <= len(value) <= 65536 for value in bundle.values())
           for bundle, keys in required):
        raise ValueError("unexpected TLS bundle fields")
    if (business_api["bff_cert_pem"], business_api["broker_server_cert_pem"],
            broker["gateway_server_cert_pem"], broker["broker_client_cert_pem"]) != (
            broker["bff_cert_pem"], broker["broker_server_cert_pem"],
            gateway["server_cert_pem"], gateway["broker_cert_pem"]):
        raise ValueError("TLS bundle pins differ")

    lanes = (
        (broker["bff_ca_pem"], broker_host, _BFF_ID, broker["broker_server_cert_pem"],
         broker["broker_server_key_pem"], business_api["bff_cert_pem"], business_api["bff_key_pem"]),
        (gateway["broker_ca_pem"], gateway_host, _BROKER_ID, gateway["server_cert_pem"],
         gateway["server_key_pem"], broker["broker_client_cert_pem"], broker["broker_client_key_pem"]),
    )
    now = datetime.now(timezone.utc)
    bounds, public_keys, pins = set(), set(), []
    for ca_pem, host, uri, server_pem, server_key, client_pem, client_key in lanes:
        ca, server, client = (_certificate(pem) for pem in (ca_pem, server_pem, client_pem))
        ca.verify_directly_issued_by(ca)
        if ca.extensions.get_extension_for_class(x509.BasicConstraints).value != x509.BasicConstraints(ca=True, path_length=0):
            raise ValueError("unexpected TLS authority scope")
        verifier = PolicyBuilder().store(Store([ca])).time(now)
        verifier.build_server_verifier(x509.DNSName(host)).verify(server, [])
        verifier.build_client_verifier().verify(client, [])
        for leaf, key, usage, san in (
            (server, server_key, ExtendedKeyUsageOID.SERVER_AUTH, x509.DNSName(host)),
            (client, client_key, ExtendedKeyUsageOID.CLIENT_AUTH, x509.UniformResourceIdentifier(uri)),
        ):
            private_key = serialization.load_pem_private_key(key.encode("ascii"), password=None)
            if not isinstance(private_key, rsa.RSAPrivateKey) or private_key.key_size != 2048 or (
                private_key.public_key().public_numbers() != leaf.public_key().public_numbers()
                or leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value != x509.SubjectAlternativeName([san])
                or list(leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value) != [usage]
            ):
                raise ValueError("TLS identity or private key differs")
        for cert in (ca, server, client):
            bounds.add((cert.not_valid_before_utc, cert.not_valid_after_utc))
            public_keys.add(cert.public_key().public_bytes(serialization.Encoding.DER,
                                                          serialization.PublicFormat.SubjectPublicKeyInfo))
        pins.extend(cert.fingerprint(hashes.SHA256()).hex() for cert in (server, client))
    if len(bounds) != 1 or len(public_keys) != 6:
        raise ValueError("TLS lifetime or key separation differs")
    begins, expires = bounds.pop()
    if not begins <= now < expires or expires - begins != timedelta(hours=3, minutes=5):
        raise ValueError("unexpected TLS trial lifetime")
    return {"expires_at": expires.isoformat(), "leaf_fingerprints_sha256": pins}


def _certificate(value):
    certificates = x509.load_pem_x509_certificates(value.encode("ascii"))
    if len(certificates) != 1:
        raise ValueError("exactly one certificate required")
    return certificates[0]
