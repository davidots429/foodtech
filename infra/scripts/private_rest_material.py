"""Generate short-lived synthetic REST material in memory; never uploads or prints it."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import ipaddress
import secrets

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


NETWORK = ipaddress.ip_network('10.42.0.32/27')
SERVER_ADDRESSES = tuple(NETWORK.network_address + offset for offset in range(4, 31))


@dataclass(frozen=True, repr=False)
class PrivateRestMaterial:
    mock: dict[str, str]
    collector: dict[str, str]
    expires_at: datetime
    server_fingerprint: str

    def __repr__(self):
        return 'PrivateRestMaterial(redacted)'

    def metadata(self):
        return {'purpose': 'SYNTHETIC_PRIVATE_REST_TRIAL', 'expires_at': self.expires_at.isoformat(),
                'server_certificate_sha256': self.server_fingerprint, 'subject_alt_ip_count': len(SERVER_ADDRESSES)}


def generate(now=None):
    """Return separate server/client values; no seed, .env or disk key files.

    Three hours covers a bounded two-hour trial plus setup margin. This is a
    proposed trial contract, not a production certificate rotation policy.
    """
    now = datetime.now(timezone.utc) if now is None else now
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('an aware clock is required')
    now = now.astimezone(timezone.utc).replace(microsecond=0)
    expires = now + timedelta(hours=3)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Foodtech synthetic REST trial CA')])
    server_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Foodtech synthetic REST Mock')])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=5)).not_valid_after(expires)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.KeyUsage(digital_signature=False, content_commitment=False, key_encipherment=False,
            data_encipherment=False, key_agreement=False, key_cert_sign=True, crl_sign=True,
            encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256()))
    server = (x509.CertificateBuilder().subject_name(server_name).issuer_name(ca_name).public_key(server_key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=5)).not_valid_after(expires)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=True,
            data_encipherment=False, key_agreement=False, key_cert_sign=False, crl_sign=False,
            encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(address) for address in SERVER_ADDRESSES]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(server_key.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256()))
    client = {'client_id': 'foodtech-synthetic-rest-trial', 'client_secret': secrets.token_urlsafe(32)}
    mock = {**client, 'server_cert_pem': server.public_bytes(serialization.Encoding.PEM).decode('ascii'),
            'server_key_pem': server_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                      serialization.NoEncryption()).decode('ascii')}
    collector = {**client, 'ca_pem': ca.public_bytes(serialization.Encoding.PEM).decode('ascii')}
    # The signing CA private key is never serialized. Python does not guarantee
    # erasure of all in-memory copies; do not claim secure zeroization here.
    return PrivateRestMaterial(mock, collector, expires, server.fingerprint(hashes.SHA256()).hex())
