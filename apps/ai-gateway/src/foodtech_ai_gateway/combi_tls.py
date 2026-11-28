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


def broker_leaf_pin(path, *, broker_uri=BROKER_URI, clock=lambda: datetime.now(timezone.utc)):
    return client_leaf_pin(path, client_uri=broker_uri, clock=clock)


def client_leaf_pin(path, *, client_uri, clock=lambda: datetime.now(timezone.utc)):
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
        if list(usage) != [ExtendedKeyUsageOID.CLIENT_AUTH] or uris != [client_uri] or \
                type(now) is not datetime or now.utcoffset() is None or not cert.not_valid_before_utc <= now < cert.not_valid_after_utc:
            raise ValueError
        return hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()
    except (ValueError, TypeError, OSError, x509.ExtensionNotFound, x509.DuplicateExtension, UnsupportedAlgorithm):
        raise ValueError('peer certificate unavailable') from None


def pinned_http_protocol(pin, *, state_key=TLS_PEER_STATE_KEY):
    if type(pin) is not str or not re.fullmatch('[a-f0-9]{64}', pin): raise ValueError('broker peer pin required')
    class BrokerPinnedH11(H11Protocol):
        _broker_peer_valid = False
        def connection_made(self, transport):
            super().connection_made(transport)
            peer = transport.get_extra_info('ssl_object')
            try:
                der = peer.getpeercert(binary_form=True) if peer else None
                self._broker_peer_valid = bool(der) and hmac.compare_digest(hashlib.sha256(der).hexdigest(), pin)
            except (AttributeError, TypeError, ssl.SSLError):
                self._broker_peer_valid = False
            if self._broker_peer_valid:
                # Isolate this connection's ASGI state. A different listener on
                # the same app must not inherit a global authentication Boolean.
                self.app_state = {**self.app_state,state_key:pin}
            else: transport.close()
        def data_received(self, data):
            if self._broker_peer_valid: super().data_received(data)
            else: self.transport.close()
    return BrokerPinnedH11


class BrokerRoutesOnly:
    def __init__(self, app, *, recipe='combi'):
        self.app = app
        self.recipe = recipe
        self.invoke_path = re.compile(r'/internal/ai/' + recipe + r'-recipe-executions/([a-f0-9-]{36})/invoke')
    async def __call__(self, scope, receive, send):
        if scope['type'] == 'lifespan': return await self.app(scope, receive, send)
        path = scope.get('path', '')
        match = self.invoke_path.fullmatch(path)
        try: valid = bool(match) and str(UUID(match[1])) == match[1]
        except ValueError: valid = False
        if scope['type'] == 'http' and scope.get('method') == 'POST' and not scope.get('query_string') and valid:
            return await self.app(scope, receive, send)
        if scope['type'] == 'websocket': return await send({'type':'websocket.close','code':1008})
        body = ('{"detail":"' + self.recipe.upper() + '_ROUTE_UNAVAILABLE"}').encode('ascii')
        await send({'type':'http.response.start','status':404,'headers':[(b'content-type',b'application/json'),(b'cache-control',b'no-store')]})
        await send({'type':'http.response.body','body':body})


def combi_server_config(app, *, server_cert, server_key, broker_ca, broker_certificate,
                        host='127.0.0.1', port=8444):
    return broker_server_config(app, recipe='combi', broker_uri=BROKER_URI, peer_state_key=TLS_PEER_STATE_KEY,
        pin_app_key=TLS_PIN_APP_STATE_KEY, server_cert=server_cert, server_key=server_key, broker_ca=broker_ca,
        broker_certificate=broker_certificate, host=host, port=port)


def broker_server_config(app, *, recipe, broker_uri, peer_state_key, pin_app_key,
                         server_cert, server_key, broker_ca, broker_certificate, host, port):
    pin = broker_leaf_pin(broker_certificate, broker_uri=broker_uri)
    if type(port) is not int or not 1 <= port <= 65535 or type(host) is not str or not host:
        raise ValueError('broker listener address required')
    config = uvicorn.Config(BrokerRoutesOnly(app, recipe=recipe), host=host, port=port,
        http=pinned_http_protocol(pin, state_key=peer_state_key), ws='none', ssl_version=ssl.PROTOCOL_TLS_SERVER,
        ssl_certfile=server_cert, ssl_keyfile=server_key, ssl_ca_certs=broker_ca,
        ssl_cert_reqs=ssl.CERT_REQUIRED, proxy_headers=False, access_log=False, log_level='error')
    config.load()
    if config.ssl is None or config.ssl.verify_mode != ssl.CERT_REQUIRED or config.ssl.keylog_filename is not None:
        raise ValueError('broker mutual TLS context required')
    app.state.mtls_listener = True
    setattr(app.state, recipe + '_broker_listener', True)
    setattr(app.state, pin_app_key, pin)
    return config
