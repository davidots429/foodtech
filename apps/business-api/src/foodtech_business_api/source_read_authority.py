"""Independent source-read grants; a job/reverse binding never grants access.

The auth domain maps a principal to opaque grant references. The separate
catalog contains source keys and opaque grant IDs, without Cognito subjects.
Only a separately authorized registrar may write either side. This reader
neither creates grants nor certifies exclusive ownership for deletion.
"""
from decimal import Decimal
import re
import time
import math
from uuid import UUID

from boto3.dynamodb.types import TypeDeserializer

from .recipe_scope import RecipeReadScope
from .subject_identity import valid_subject
from foodtech_contracts.source_read_authorization import SourceReadAuthorization


def _integer(value, minimum=0):
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)) or \
            (isinstance(value, Decimal) and not value.is_finite()) or value != int(value) or not minimum <= value < 2**63:
        return None
    return int(value)


class DynamoSourceReadAuthority:
    def __init__(self, *, auth_table, catalog_table, client, clock=time.time):
        auth = re.fullmatch(r'foodtech-auth-sessions-([a-z0-9-]{1,32})', auth_table) if isinstance(auth_table, str) else None
        if not auth or catalog_table != f'foodtech-source-access-{auth[1]}' or client is None:
            raise ValueError('separate matching-stage source authority required')
        self.auth_table, self.catalog_table, self.client = auth_table, catalog_table, client
        self.clock, self.decoder = clock, TypeDeserializer()

    def _decode(self, item):
        if not isinstance(item, dict):
            return None
        try:
            return {key: self.decoder.deserialize(value) for key, value in item.items()}
        except (TypeError, ValueError, KeyError, AttributeError):
            return None

    @staticmethod
    def _refs(scope, principal):
        if not isinstance(principal, dict) or principal.get('pk') != 'USER#' + scope.owner or \
                _integer(principal.get('epoch')) != scope.auth_epoch or \
                principal.get('suspended', False) is not False or 'deletion_lock_request' in principal:
            return None
        mapping = principal.get('source_read_grants')
        if not isinstance(mapping, dict):
            return None
        refs = tuple(mapping.get(pk) for pk in scope.source_partitions)
        try:
            if any(not isinstance(ref, str) or str(UUID(ref)) != ref for ref in refs) or len(set(refs)) != 2:
                return None
        except (TypeError, ValueError, AttributeError):
            return None
        return refs

    def authorize_read(self, scope):
        return self.authorization_snapshot(scope) is not None

    def authorization_snapshot(self, scope):
        if type(scope) is not RecipeReadScope or not valid_subject(scope.owner) or \
                type(scope.auth_epoch) is not int or not 0 <= scope.auth_epoch < 2**63 or \
                not isinstance(scope.source_partitions, tuple) or len(scope.source_partitions) != 2 or \
                any(not isinstance(pk, str) or not re.fullmatch(r'JEYUK#jeyuk-[A-Za-z0-9-]{1,121}', pk)
                    for pk in scope.source_partitions) or scope.source_partitions[0] == scope.source_partitions[1]:
            return None
        key = {'pk': {'S': 'USER#' + scope.owner}}
        principal = self._decode(self.client.get_item(TableName=self.auth_table, Key=key,
            ConsistentRead=True).get('Item'))
        refs = self._refs(scope, principal)
        if refs is None:
            return None
        response = self.client.transact_get_items(TransactItems=[
            {'Get': {'TableName': self.auth_table, 'Key': key}},
            *({'Get': {'TableName': self.catalog_table, 'Key': {
                'pk': {'S': 'GRANT#' + ref}, 'sk': {'S': 'META'}}}} for ref in refs),
        ])
        rows = response.get('Responses')
        if not isinstance(rows, list) or len(rows) != 3 or any(not isinstance(row, dict) for row in rows):
            return None
        current = self._decode(rows[0].get('Item'))
        if self._refs(scope, current) != refs:
            return None
        now = self.clock()
        if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now) or now <= 0:
            return None
        deadlines, versions = [], []
        for ref, source, row in zip(refs, scope.source_partitions, rows[1:], strict=True):
            grant = self._decode(row.get('Item'))
            if not isinstance(grant, dict) or set(grant) != {
                    'pk', 'sk', 'schema', 'grant_id', 'source_partition', 'permission', 'state', 'version', 'expires_at'} or \
                    grant['pk'] != 'GRANT#' + ref or grant['sk'] != 'META' or \
                    _integer(grant['schema'], 1) != 1 or grant['grant_id'] != ref or \
                    grant['source_partition'] != source or grant['permission'] != 'RECIPE_SOURCE_READ' or \
                    grant['state'] != 'ACTIVE' or _integer(grant['version'], 1) is None:
                return None
            expires = _integer(grant['expires_at'], 1)
            if expires is None or expires <= now:
                return None
            deadlines.append(expires)
            versions.append(_integer(grant['version'], 1))
        final_now = self.clock()
        if not isinstance(final_now, (int, float)) or isinstance(final_now, bool) or \
                not math.isfinite(final_now) or not now <= final_now < min(deadlines):
            return None
        return SourceReadAuthorization(self.auth_table, self.catalog_table, scope.owner, scope.execution_id,
                                       scope.auth_epoch, scope.source_partitions, refs, tuple(versions), tuple(deadlines))
