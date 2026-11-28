"""Build isolated, short-lived Tonkatsu trial bundles in memory."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

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


def generate(*, broker_host: str, gateway_host: str, now: datetime | None = None) -> TonkatsuTrialTlsMaterial:
    """Return three bundles for a bounded trial, without serializing either CA key."""
    for host in (broker_host, gateway_host):
        if not isinstance(host, str) or len(host) > 253 or not re.fullmatch(rf"{_LABEL}(?:[.]{_LABEL})*[.]internal", host):
            raise ValueError("reviewed private service hostname required")
    if broker_host == gateway_host:
        raise ValueError("broker and Gateway require distinct private hostnames")
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
