"""Dedicated native BFF leaf pin: the broker never accepts browser credentials."""
from datetime import datetime,timezone
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
from fastapi import HTTPException
import uvicorn
from uvicorn.protocols.http.h11_impl import H11Protocol

PEER_STATE='foodtech_combi_bff_peer_sha256'
BFF_URI='urn:foodtech:service:business-api'


def require_bff_transport(request, *, recipe='combi'):
    expected=getattr(request.app.state,recipe+'_bff_leaf_sha256',None)
    state=request.scope.get('state',{})
    actual=state.get('foodtech_'+recipe+'_bff_peer_sha256') if type(state) is dict else None
    if request.scope.get('scheme')!='https' or type(expected) is not str or not re.fullmatch('[a-f0-9]{64}',expected) or \
            type(actual) is not str or not hmac.compare_digest(actual,expected):
        raise HTTPException(401,'BFF mutual TLS required',headers={'Cache-Control':'no-store'})


def _bff_pin(path):
    try:
        with Path(path).open('rb') as stream: raw=stream.read(65537)
        certs=x509.load_pem_x509_certificates(raw)
        if len(raw)>65536 or len(certs)!=1: raise ValueError
        cert=certs[0]
        if cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca or \
                list(cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value)!=[ExtendedKeyUsageOID.CLIENT_AUTH] or \
                cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.UniformResourceIdentifier)!=[BFF_URI] or \
                not cert.not_valid_before_utc<=datetime.now(timezone.utc)<cert.not_valid_after_utc: raise ValueError
        return hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()
    except (ValueError,TypeError,OSError,x509.ExtensionNotFound,x509.DuplicateExtension,UnsupportedAlgorithm):
        raise ValueError('BFF certificate unavailable') from None


def _pinned_protocol(pin, *, peer_state=PEER_STATE):
    class BffPinnedH11(H11Protocol):
        _valid_bff=False
        def connection_made(self,transport):
            super().connection_made(transport)
            try:
                peer=transport.get_extra_info('ssl_object')
                der=peer.getpeercert(binary_form=True) if peer else None
                self._valid_bff=bool(der) and hmac.compare_digest(hashlib.sha256(der).hexdigest(),pin)
            except (TypeError,AttributeError,ssl.SSLError): self._valid_bff=False
            if self._valid_bff: self.app_state={**self.app_state,peer_state:pin}
            else: transport.close()
        def data_received(self,data):
            if self._valid_bff: super().data_received(data)
            else: self.transport.close()
    return BffPinnedH11


class BrokerRoutesOnly:
    def __init__(self, app, *, recipe='combi'):
        self.app = app
        self.recipe = recipe
        self.path = re.compile('/internal/' + recipe + '/invocations/([a-f0-9-]{36})')
    async def __call__(self,scope,receive,send):
        if scope['type']=='lifespan': return await self.app(scope,receive,send)
        match=self.path.fullmatch(scope.get('path',''))
        try: valid=bool(match) and str(UUID(match[1]))==match[1]
        except ValueError: valid=False
        if scope['type']=='http' and scope.get('method')=='POST' and not scope.get('query_string') and valid:
            return await self.app(scope,receive,send)
        if scope['type']=='websocket': return await send({'type':'websocket.close','code':1008})
        await send({'type':'http.response.start','status':404,'headers':[(b'content-type',b'application/json'),(b'cache-control',b'no-store')]})
        await send({'type':'http.response.body','body':(f'{{"detail":"{self.recipe.upper()}_BROKER_ROUTE_UNAVAILABLE"}}').encode('ascii')})


def broker_server_config(app,*,server_cert,server_key,bff_ca,bff_certificate,host='127.0.0.1',port=8445,recipe='combi'):
    if recipe not in ('combi', 'tonkatsu'): raise ValueError('unknown broker route')
    if type(host) is not str or not host or type(port) is not int or not 1<=port<=65535: raise ValueError('broker address required')
    pin=_bff_pin(bff_certificate)
    config=uvicorn.Config(BrokerRoutesOnly(app, recipe=recipe),host=host,port=port,
        http=_pinned_protocol(pin, peer_state='foodtech_'+recipe+'_bff_peer_sha256'),ws='none',
        ssl_version=ssl.PROTOCOL_TLS_SERVER,ssl_certfile=server_cert,ssl_keyfile=server_key,ssl_ca_certs=bff_ca,
        ssl_cert_reqs=ssl.CERT_REQUIRED,proxy_headers=False,access_log=False,log_level='error')
    config.load()
    if config.ssl is None or config.ssl.verify_mode!=ssl.CERT_REQUIRED or config.ssl.keylog_filename is not None:
        raise ValueError('broker mutual TLS required')
    setattr(app.state, recipe + '_bff_leaf_sha256', pin)
    return config
