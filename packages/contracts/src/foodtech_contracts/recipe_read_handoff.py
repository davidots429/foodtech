"""Private temporary-credential envelope for the authenticated internal TLS hop.

Do not return this object or its explicit wire encoding from a customer API.
Ordinary repr, JSON encoding and pickle do not expose credential material.
This envelope is not signed authority: the recipient must authenticate its
transport, match authoritative execution records and recheck live grants.
"""
from dataclasses import asdict, fields
from datetime import datetime, timezone
import json
import re

from .recipe_read_scope import RecipeReadScope, validate_recipe_read_scope
from .source_read_authorization import SourceReadAuthorization
from .prior_history_witness import PriorHistoryWitness

SCHEMA = 'recipe-source-read-handoff-v1'
HISTORY_SCHEMA = 'recipe-source-read-handoff-v2'
CONTENT_TYPE = 'application/vnd.foodtech.recipe-source-read+json'
MAX_BYTES = 65536


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate internal field')
        result[key] = value
    return result


def _record(kind, value, tuple_fields):
    if not isinstance(value, dict) or set(value) != {field.name for field in fields(kind)}:
        raise ValueError('invalid internal fields')
    value = dict(value)
    for name in tuple_fields:
        if type(value[name]) is not list or len(value[name]) != 2:
            raise ValueError('invalid internal bounds')
        value[name] = tuple(value[name])
    return kind(**value)


class RecipeReadHandoff:
    __slots__ = ('__scope', '__authorization', '__read_role', '__assumed_role', '__expires', '__credentials', '__history')

    def __init__(self, scope, authorization, read_role_arn, assumed_role_arn, expires_at,
                 access_key_id, secret_access_key, session_token, *, history=None):
        self.__scope, self.__authorization, self.__history = scope, authorization, history
        self.__read_role, self.__assumed_role, self.__expires = read_role_arn, assumed_role_arn, expires_at
        self.__credentials = (access_key_id, secret_access_key, session_token)
        self._validate()

    def __repr__(self):
        return 'RecipeReadHandoff()'

    def __reduce_ex__(self, _protocol):
        raise TypeError('internal read handoff cannot be pickled')

    @property
    def scope(self):
        return self.__scope

    @property
    def authorization(self):
        return self.__authorization

    @property
    def history(self):
        return self.__history

    @property
    def read_role_arn(self):
        return self.__read_role

    @property
    def assumed_role_arn(self):
        return self.__assumed_role

    @property
    def expires_at(self):
        return self.__expires

    def _validate(self):
        try:
            validate_recipe_read_scope(self.scope)
            match = re.fullmatch(r'arn:aws:iam::([0-9]{12}):role/foodtech-recipe-source-read-([a-z0-9-]{1,32})',
                                 self.read_role_arn) if isinstance(self.read_role_arn, str) else None
            if not match or not isinstance(self.assumed_role_arn, str) or not re.fullmatch(
                    r'arn:aws:sts::' + match[1] + r':assumed-role/foodtech-recipe-source-read-' + match[2] +
                    r'/recipe-[a-f0-9]{32}', self.assumed_role_arn):
                raise ValueError
            grant = self.authorization
            if type(grant) is not SourceReadAuthorization or \
                    (grant.subject, grant.execution_id, grant.auth_epoch, grant.source_partitions, grant.auth_table) != \
                    (self.scope.owner, self.scope.execution_id, self.scope.auth_epoch, self.scope.source_partitions,
                     'foodtech-auth-sessions-' + match[2]):
                raise ValueError
            if self.history is not None and (type(self.history) is not PriorHistoryWitness or
                    (self.history.owner, self.history.auth_epoch, self.history.job_versions,
                     self.history.current_job_id, self.history.prior_job_id) !=
                    (self.scope.owner, self.scope.auth_epoch, self.scope.job_versions,
                     *(key.rsplit('#', 1)[-1] for key in self.scope.job_partitions))):
                raise ValueError
            now = datetime.now(timezone.utc)
            if not isinstance(self.expires_at, datetime) or self.expires_at.tzinfo is None or \
                    not 30 <= (self.expires_at - now).total_seconds() <= 930 or min(grant.expires_at) <= now.timestamp():
                raise ValueError
            access, secret, token = self.__credentials
            if not isinstance(access, str) or not re.fullmatch(r'[A-Za-z0-9_]{16,128}', access) or any(
                    not isinstance(value, str) or not 1 <= len(value) <= 16384 or
                    any(not 33 <= ord(char) <= 126 for char in value) for value in (secret, token)):
                raise ValueError
        except (ValueError, TypeError, AttributeError, OverflowError):
            raise ValueError('recipe read handoff unavailable') from None

    def _sdk_credentials(self):
        self._validate()
        access, secret, token = self.__credentials
        return {'aws_access_key_id': access, 'aws_secret_access_key': secret, 'aws_session_token': token}

    def _encode_for_internal_tls(self):
        self._validate()
        access, secret, token = self.__credentials
        payload = {'schema': HISTORY_SCHEMA if self.history is not None else SCHEMA, 'scope': asdict(self.scope), 'authorization': asdict(self.authorization),
            'read_role_arn': self.read_role_arn, 'assumed_role_arn': self.assumed_role_arn,
            'expires_at': self.expires_at.astimezone(timezone.utc).isoformat(),
            'credentials': {'access_key_id': access, 'secret_access_key': secret, 'session_token': token}}
        if self.history is not None:
            payload['history'] = asdict(self.history)
        raw = json.dumps(payload, separators=(',', ':'), ensure_ascii=True).encode('utf-8')
        if len(raw) > MAX_BYTES:
            raise ValueError('recipe read handoff unavailable')
        return raw

    @classmethod
    def _decode_from_internal_tls(cls, raw):
        try:
            if type(raw) is not bytes or not 1 <= len(raw) <= MAX_BYTES:
                raise ValueError
            payload = json.loads(raw, object_pairs_hook=_unique)
            if not isinstance(payload, dict) or not isinstance(payload.get('schema'), str) or payload['schema'] not in {SCHEMA, HISTORY_SCHEMA}:
                raise ValueError
            keys = {'schema', 'scope', 'authorization', 'read_role_arn', 'assumed_role_arn', 'expires_at', 'credentials'}
            if set(payload) != keys | ({'history'} if payload['schema'] == HISTORY_SCHEMA else set()):
                raise ValueError
            history = (_record(PriorHistoryWitness, payload['history'], ('job_versions',))
                       if payload['schema'] == HISTORY_SCHEMA else None)
            scope = _record(RecipeReadScope, payload['scope'],
                ('job_partitions', 'source_partitions', 'job_versions', 'source_revisions'))
            grant = _record(SourceReadAuthorization, payload['authorization'],
                ('source_partitions', 'grant_ids', 'grant_versions', 'expires_at'))
            credentials = payload['credentials']
            if not isinstance(credentials, dict) or set(credentials) != {'access_key_id', 'secret_access_key', 'session_token'} or \
                    not isinstance(payload['expires_at'], str):
                raise ValueError
            return cls(scope, grant, payload['read_role_arn'], payload['assumed_role_arn'],
                datetime.fromisoformat(payload['expires_at']), credentials['access_key_id'],
                credentials['secret_access_key'], credentials['session_token'], history=history)
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError, UnicodeError, OverflowError):
            raise ValueError('recipe read handoff unavailable') from None
