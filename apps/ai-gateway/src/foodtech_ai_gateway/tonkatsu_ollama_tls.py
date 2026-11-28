"""Certificate settings for an isolated internal Tonkatsu inference host."""
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import re
import ssl

from cryptography import x509
from cryptography.x509.oid import ExtendedKeyUsageOID

_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_ORIGIN = re.compile(rf"https://({_LABEL}(?:\.{_LABEL})*\.internal):([1-9][0-9]{{0,4}})")


@dataclass(frozen=True, repr=False)
class TonkatsuInferenceTLS:
    server_certificate: str
    client_certificate: str
    client_key: str

    def context(self, origin: str) -> ssl.SSLContext:
        match = _ORIGIN.fullmatch(origin)
        if match is None or len(match[1]) > 253 or int(match[2]) > 65535:
            raise ValueError("reviewed private inference origin required")
        with Path(self.server_certificate).open("rb") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError("invalid inference server certificate")
        leaves = x509.load_pem_x509_certificates(raw)
        if len(leaves) != 1:
            raise ValueError("one inference server leaf is required")
        leaf = leaves[0]
        constraints = leaf.extensions.get_extension_for_class(x509.BasicConstraints).value
        usage = leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
        names = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        now = datetime.now(timezone.utc)
        if (constraints.ca or list(usage) != [ExtendedKeyUsageOID.SERVER_AUTH]
                or list(names) != [x509.DNSName(match[1])]
                or not leaf.not_valid_before_utc <= now < leaf.not_valid_after_utc):
            raise ValueError("unreviewed inference server certificate")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
        context.load_verify_locations(cadata=raw.decode("ascii"))
        context.load_cert_chain(self.client_certificate, self.client_key)
        if not context.check_hostname or context.verify_mode != ssl.CERT_REQUIRED or context.keylog_filename is not None:
            raise ValueError("inference endpoint requires native hostname and certificate checks")
        return context
