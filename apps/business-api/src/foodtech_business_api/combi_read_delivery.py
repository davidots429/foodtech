"""One private Combi POST with exact server-leaf trust and no credential output."""
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import ssl
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.x509.oid import ExtendedKeyUsageOID
import httpx

from foodtech_contracts.combi_read_handoff import CombiReadHandoff, CONTENT_TYPE
from .recipe_read_delivery import _outcome


class CombiDeliveryUnavailable(Exception): pass


@dataclass(frozen=True, repr=False)
class CombiDeliverySettings:
    gateway_url: str
    server_certificate: str
    broker_certificate: str
    broker_key: str

    def __repr__(self): return 'CombiDeliverySettings()'

    def __post_init__(self):
        try:
            if type(self.gateway_url) is not str or not 1 <= len(self.gateway_url) <= 2048 or \
                    any(not 33 <= ord(c) <= 126 for c in self.gateway_url): raise ValueError
            url = urlsplit(self.gateway_url)
            if url.scheme != 'https' or not url.hostname or url.username or url.password or url.path not in ('','/') or \
                    url.query or url.fragment or (url.port is not None and not 1 <= url.port <= 65535): raise ValueError
            if any(type(path) is not str or not path for path in (self.server_certificate,self.broker_certificate,self.broker_key)): raise ValueError
            object.__setattr__(self,'gateway_url',self.gateway_url.rstrip('/'))
        except (TypeError,ValueError):
            raise ValueError('explicit Combi TLS destination required') from None


def _context(settings):
    with Path(settings.server_certificate).open('rb') as stream: raw = stream.read(65537)
    if len(raw) > 65536: raise ValueError
    certificates = x509.load_pem_x509_certificates(raw)
    if len(certificates) != 1: raise ValueError
    leaf = certificates[0]
    try:
        if leaf.extensions.get_extension_for_class(x509.BasicConstraints).value.ca: raise ValueError
    except x509.ExtensionNotFound: pass
    usage = leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    now = datetime.now(timezone.utc)
    if list(usage) != [ExtendedKeyUsageOID.SERVER_AUTH] or not leaf.not_valid_before_utc <= now < leaf.not_valid_after_utc:
        raise ValueError
    # Trust only this reviewed leaf, never its whole CA. Native chain, purpose,
    # expiry and hostname validation remain enabled; no response-time pin check.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
    context.load_verify_locations(cadata=leaf.public_bytes(serialization.Encoding.PEM).decode('ascii'))
    context.load_cert_chain(settings.broker_certificate,settings.broker_key)
    if not context.check_hostname or context.verify_mode != ssl.CERT_REQUIRED or context.keylog_filename is not None:
        raise ValueError
    return context


async def deliver_internal_combi_handoff(handoff, settings):
    """Explicit internal caller only. Never retry a model invocation or redirect."""
    try:
        if type(handoff) is not CombiReadHandoff or type(settings) is not CombiDeliverySettings: raise ValueError
        credentials = tuple(handoff._sdk_credentials().values())
        execution_id = handoff.scope.execution_id
        path = '/internal/ai/combi-recipe-executions/' + execution_id + '/invoke'
        return await _deliver_pinned(settings, handoff, credentials, path, CONTENT_TYPE, execution_id)
    except (ValueError,TypeError,AttributeError,OSError,UnicodeError,RecursionError,httpx.HTTPError,
            x509.ExtensionNotFound,x509.DuplicateExtension,UnsupportedAlgorithm):
        raise CombiDeliveryUnavailable('Combi invocation unavailable') from None


async def _deliver_pinned(settings, handoff, credentials, path, content_type, execution_id):
    raw = handoff._encode_for_internal_tls()
    context = _context(settings)
    async with httpx.AsyncClient(verify=context,timeout=195,trust_env=False,follow_redirects=False,http2=False) as client:
        async with client.stream('POST',settings.gateway_url + path,
                content=raw,headers={'Content-Type':content_type,'Cache-Control':'no-store','Accept':'application/json','Accept-Encoding':'identity'}) as response:
            if response.status_code != 200 or response.headers.get('Cache-Control') != 'no-store' or \
                    response.headers.get('Content-Type','').split(';',1)[0].strip().lower() != 'application/json' or \
                    response.headers.get('Content-Encoding','identity') != 'identity': raise ValueError
            content = bytearray()
            async for chunk in response.aiter_bytes():
                if len(content)+len(chunk)>24576: raise ValueError
                content.extend(chunk)
    outcome = _outcome(content,execution_id)
    if any(secret in value for secret in credentials for value in outcome.values() if type(value) is str): raise ValueError
    return outcome
