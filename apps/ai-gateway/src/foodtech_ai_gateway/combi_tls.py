"""Dedicated Combi HTTP listener with native mTLS and an exact peer leaf pin.

The CA authenticates the handshake; the protocol rejects another valid leaf
before HTTP parsing/ASGI dispatch. No forwarded identity header is trusted.
This transport does not prove issuer IAM, history coverage or model authority.
"""
from datetime import datetime, timezone
import hashlib
import hmac
from pathlib import Path
import re
import ssl
from uuid import UUID

from cryptography import x509
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.x509.oid import ExtendedKeyUsageOID
import uvicorn
from uvicorn.protocols.http.h11_impl import H11Protocol
from foodtech_contracts.combi_read_handoff import TLS_PEER_STATE_KEY,TLS_PIN_APP_STATE_KEY

BROKER_URI = 'urn:foodtech:service:combi-read-broker'


def broker_leaf_pin(path, *, clock=lambda: datetime.now(timezone.utc)):
    try:
        with Path(path).open('rb') as stream: raw = stream.read(65537)
        if len(raw) > 65536: raise ValueError
        certificates = x509.load_pem_x509_certificates(raw)
        if len(certificates) != 1: raise ValueError
        cert = certificates[0]
        try:
            if cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca: raise ValueError
        except x509.ExtensionNotFound: pass
        usage = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
        uris = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.UniformResourceIdentifier)
        now = clock()
        if list(usage) != [ExtendedKeyUsageOID.CLIENT_AUTH] or uris != [BROKER_URI] or \
                type(now) is not datetime or now.utcoffset() is None or not cert.not_valid_before_utc <= now < cert.not_valid_after_utc:
            raise ValueError
        return hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()
    except (ValueError, TypeError, OSError, x509.ExtensionNotFound, x509.DuplicateExtension, UnsupportedAlgorithm):
        raise ValueError('Combi broker certificate unavailable') from None


def pinned_http_protocol(pin):
    if type(pin) is not str or not re.fullmatch('[a-f0-9]{64}', pin): raise ValueError('Combi peer pin required')
    class BrokerPinnedH11(H11Protocol):
        _combi_peer_valid = False
        def connection_made(self, transport):
            super().connection_made(transport)
            peer = transport.get_extra_info('ssl_object')
            try:
                der = peer.getpeercert(binary_form=True) if peer else None
                self._combi_peer_valid = bool(der) and hmac.compare_digest(hashlib.sha256(der).hexdigest(), pin)
            except (AttributeError, TypeError, ssl.SSLError):
                self._combi_peer_valid = False
            if self._combi_peer_valid:
                # Isolate this connection's ASGI state. A different listener on
                # the same app must not inherit a global authentication Boolean.
                self.app_state = {**self.app_state,TLS_PEER_STATE_KEY:pin}
            else: transport.close()
        def data_received(self, data):
            if self._combi_peer_valid: super().data_received(data)
            else: self.transport.close()
    return BrokerPinnedH11


class CombiRoutesOnly:
    def __init__(self, app): self.app = app
    async def __call__(self, scope, receive, send):
        if scope['type'] == 'lifespan': return await self.app(scope, receive, send)
        path = scope.get('path', '')
        match = re.fullmatch(r'/internal/ai/combi-recipe-executions/([a-f0-9-]{36})/invoke', path)
        try: valid = bool(match) and str(UUID(match[1])) == match[1]
        except ValueError: valid = False
        if scope['type'] == 'http' and scope.get('method') == 'POST' and not scope.get('query_string') and valid:
            return await self.app(scope, receive, send)
        if scope['type'] == 'websocket': return await send({'type':'websocket.close','code':1008})
        body = b'{"detail":"COMBI_ROUTE_UNAVAILABLE"}'
        await send({'type':'http.response.start','status':404,'headers':[(b'content-type',b'application/json'),(b'cache-control',b'no-store')]})
        await send({'type':'http.response.body','body':body})


def combi_server_config(app, *, server_cert, server_key, broker_ca, broker_certificate,
                        host='127.0.0.1', port=8444):
    pin = broker_leaf_pin(broker_certificate)
    if type(port) is not int or not 1 <= port <= 65535 or type(host) is not str or not host:
        raise ValueError('Combi listener address required')
    config = uvicorn.Config(CombiRoutesOnly(app), host=host, port=port,
        http=pinned_http_protocol(pin), ws='none', ssl_version=ssl.PROTOCOL_TLS_SERVER,
        ssl_certfile=server_cert, ssl_keyfile=server_key, ssl_ca_certs=broker_ca,
        ssl_cert_reqs=ssl.CERT_REQUIRED, proxy_headers=False, access_log=False, log_level='error')
    config.load()
    if config.ssl is None or config.ssl.verify_mode != ssl.CERT_REQUIRED or config.ssl.keylog_filename is not None:
        raise ValueError('Combi mutual TLS context required')
    app.state.mtls_listener = True
    app.state.combi_broker_listener = True
    setattr(app.state,TLS_PIN_APP_STATE_KEY,pin)
    return config
