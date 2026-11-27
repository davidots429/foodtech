"""Opaque temporary credentials for an authenticated private Combi TLS hop.

Never return this envelope or its explicit wire bytes from a customer API.
This is not signed authority or proof of a session policy: the receiver must
authenticate the reviewed issuer, match stored execution and live history, and
retain independent actual IAM/session/deployment verification requirements.
"""
from dataclasses import asdict, fields
from datetime import datetime, timezone
import json
import re

from .combi_execution_binding import parse_combi_binding
from .combi_history_witness import CombiHistoryWitness
from .combi_read_scope import CombiReadScope

SCHEMA = 'combi-source-read-handoff-v1'
CONTENT_TYPE = 'application/vnd.foodtech.combi-source-read+json'
MAX_BYTES = 65536
TLS_PEER_STATE_KEY = 'foodtech_combi_peer_sha256'
TLS_PIN_APP_STATE_KEY = 'combi_broker_peer_sha256'


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result: raise ValueError('duplicate internal field')
        result[key] = value
    return result


def _fields(kind, value):
    if type(value) is not dict or set(value) != {field.name for field in fields(kind)}:
        raise ValueError('invalid internal fields')
    return dict(value)


class CombiReadHandoff:
    __slots__ = ('__scope', '__role', '__assumed', '__expires', '__credentials', '__clock', '__last_time')

    def __init__(self, scope, read_role_arn, assumed_role_arn, expires_at,
                 access_key_id, secret_access_key, session_token, *, clock=lambda: datetime.now(timezone.utc)):
        self.__scope, self.__role, self.__assumed, self.__expires = scope, read_role_arn, assumed_role_arn, expires_at
        self.__credentials, self.__clock = (access_key_id, secret_access_key, session_token), clock
        self.__last_time = None
        self._validate()

    def __repr__(self): return 'CombiReadHandoff()'
    def __reduce_ex__(self, _protocol): raise TypeError('combi handoff cannot be serialized')
    @property
    def scope(self): return self.__scope
    @property
    def read_role_arn(self): return self.__role
    @property
    def assumed_role_arn(self): return self.__assumed
    @property
    def expires_at(self): return self.__expires

    def _validate(self):
        try:
            match = re.fullmatch(r'arn:aws:iam::([0-9]{12}):role/foodtech-combi-source-read-([a-z0-9-]{1,32})', self.read_role_arn) \
                if type(self.read_role_arn) is str else None
            if type(self.scope) is not CombiReadScope or not match or type(self.assumed_role_arn) is not str or \
                    not re.fullmatch(r'arn:aws:sts::'+match[1]+':assumed-role/foodtech-combi-source-read-'+match[2]+r'/combi-[a-f0-9]{32}', self.assumed_role_arn):
                raise ValueError
            now = self.__clock()
            if type(now) is not datetime or now.utcoffset() is None or type(self.expires_at) is not datetime or \
                    self.expires_at.utcoffset() is None or not 30 <= (self.expires_at-now).total_seconds() <= 930 or \
                    (self.__last_time is not None and now < self.__last_time) or \
                    not self.scope.history.writer_verified_at <= now.timestamp() < self.scope.history.writer_valid_until or \
                    self.expires_at.timestamp() > self.scope.history.writer_valid_until:
                raise ValueError
            self.__last_time = now
            access, secret, token = self.__credentials
            if type(access) is not str or not re.fullmatch('[A-Za-z0-9_]{16,128}', access) or any(
                    type(value) is not str or not 1 <= len(value) <= 16384 or
                    any(not 33 <= ord(char) <= 126 for char in value) for value in (secret, token)):
                raise ValueError
        except (ValueError, TypeError, AttributeError, ArithmeticError, OSError):
            raise ValueError('combi read handoff unavailable') from None

    def _sdk_credentials(self):
        self._validate()
        access, secret, token = self.__credentials
        return {'aws_access_key_id': access, 'aws_secret_access_key': secret, 'aws_session_token': token}

    def _encode_for_internal_tls(self):
        self._validate()
        access, secret, token = self.__credentials
        payload = {'schema': SCHEMA, 'scope': asdict(self.scope), 'read_role_arn': self.read_role_arn,
            'assumed_role_arn': self.assumed_role_arn, 'expires_at': self.expires_at.astimezone(timezone.utc).isoformat(),
            'credentials': {'access_key_id': access, 'secret_access_key': secret, 'session_token': token}}
        raw = json.dumps(payload, separators=(',', ':'), ensure_ascii=True).encode('utf-8')
        if len(raw) > MAX_BYTES: raise ValueError('combi read handoff unavailable')
        return raw

    @classmethod
    def _decode_from_internal_tls(cls, raw, *, clock=lambda: datetime.now(timezone.utc)):
        try:
            if type(raw) is not bytes or not 1 <= len(raw) <= MAX_BYTES: raise ValueError
            def constant(_): raise ValueError
            payload = json.loads(raw, object_pairs_hook=_unique, parse_constant=constant)
            if type(payload) is not dict or set(payload) != {'schema', 'scope', 'read_role_arn', 'assumed_role_arn', 'expires_at', 'credentials'} or \
                    payload['schema'] != SCHEMA: raise ValueError
            scope = _fields(CombiReadScope, payload['scope'])
            scope['binding'] = parse_combi_binding(scope['binding'])
            history = _fields(CombiHistoryWitness, scope['history'])
            history['binding'] = parse_combi_binding(history['binding'])
            scope['history'] = CombiHistoryWitness(**history)
            scope = CombiReadScope(**scope)
            credentials = payload['credentials']
            if type(credentials) is not dict or set(credentials) != {'access_key_id', 'secret_access_key', 'session_token'} or \
                    type(payload['expires_at']) is not str: raise ValueError
            return cls(scope, payload['read_role_arn'], payload['assumed_role_arn'], datetime.fromisoformat(payload['expires_at']),
                credentials['access_key_id'], credentials['secret_access_key'], credentials['session_token'], clock=clock)
        except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError, UnicodeError, RecursionError, OSError):
            raise ValueError('combi read handoff unavailable') from None
