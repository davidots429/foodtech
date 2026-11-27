"""Read two Combi meals and their raw chunks through an explicitly scoped client.

This is a consumer of a separately verified read identity, not a lease issuer.
No STS identity assertion or successful read here proves its IAM restrictions.
The protected history guard uses a separate explicit condition client; the
source client never reads the owner roster, auth table, live stream or GRANT.
Registered model execution remains disabled until issuance/handoff and actual
IAM, writer deployment, reservation and settlement are connected and verified.
"""
from copy import deepcopy
from datetime import datetime, timezone
import re

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import BotoCoreError, ClientError

from foodtech_contracts.combi_history_witness import history_integer
from foodtech_contracts.combi_observed_inputs import observe_combi_batch
from foodtech_contracts.combi_read_scope import CombiReadScope
from .combi_history_conditions import DynamoCombiHistoryConditions
from .combi_recommendation_inputs import build_combi_inputs
from .policy import Execution

MAX_MEAL_ROWS = 10000
MAX_MEAL_PAGES = 1000


def _identical(left, right):
    if type(left) is not type(right): return False
    if type(left) is dict:
        return left.keys() == right.keys() and all(_identical(v, right[k]) for k, v in left.items())
    if type(left) in (list, tuple):
        return len(left) == len(right) and all(_identical(a, b) for a, b in zip(left, right))
    return left == right


class DynamoCombiSourceGuard:
    def __init__(self, scope, *, cooking_table, client, history_conditions,
                 clock=lambda: datetime.now(timezone.utc)):
        if type(scope) is not CombiReadScope or type(history_conditions) is not DynamoCombiHistoryConditions or \
                history_conditions.witness != scope.history or history_conditions.cooking_table != cooking_table or \
                type(cooking_table) is not str or not re.fullmatch('foodtech-cooking-jobs-[a-z0-9-]{1,32}', cooking_table) or \
                client is None or not callable(clock):
            raise ValueError('explicit matching combi source scope and clients required')
        self.scope, self.table, self.client = scope, cooking_table, client
        self.history, self.clock = history_conditions, clock
        self.serializer, self.deserializer = TypeSerializer(), TypeDeserializer()

    def __repr__(self): return 'DynamoCombiSourceGuard()'

    def _encode(self, values): return {k: self.serializer.serialize(v) for k, v in values.items()}
    def _decode(self, values): return {k: self.deserializer.deserialize(v) for k, v in values.items()}

    def _meal_rows(self, pk):
        if pk not in self.scope.meal_partitions: raise ValueError('unbound combi meal')
        rows, cursors, pages = {}, set(), 0
        paginator = self.client.get_paginator('query')
        for page in paginator.paginate(TableName=self.table, KeyConditionExpression='#pk = :pk',
                ExpressionAttributeNames={'#pk': 'pk'}, ExpressionAttributeValues=self._encode({':pk': pk}),
                ConsistentRead=True, PaginationConfig={'PageSize': 100}):
            pages += 1
            if pages > MAX_MEAL_PAGES or type(page) is not dict or type(page.get('Items')) is not list:
                raise ValueError('complete combi meal unavailable')
            for encoded in page['Items']:
                row = self._decode(encoded)
                sk = row.get('sk')
                if row.get('pk') != pk or type(sk) is not str or not sk or sk in rows or len(rows) >= MAX_MEAL_ROWS:
                    raise ValueError('invalid combi meal row')
                rows[sk] = deepcopy(row)
            cursor = page.get('LastEvaluatedKey')
            if cursor:
                cursor = self._decode(cursor)
                sk = cursor.get('sk')
                if set(cursor) != {'pk', 'sk'} or cursor['pk'] != pk or type(sk) is not str or \
                        not sk or sk in cursors:
                    raise ValueError('invalid combi meal cursor')
                cursors.add(sk)
        if 'META' not in rows: raise ValueError('combi meal metadata missing')
        return rows

    def _snapshot(self):
        result = []
        b = self.scope.binding
        for pk, version, digest in zip(self.scope.meal_partitions,
                (b.current_meal_version, b.prior_meal_version), (b.current_source_sha256, b.prior_source_sha256)):
            rows = self._meal_rows(pk)
            meta = rows['META']
            source = meta.get('source')
            if history_integer(meta.get('version')) != version or type(source) is not dict or source.get('raw_sha256') != digest or \
                    source.get('device_id') != self.scope.history.device_id:
                raise ValueError('combi source version changed')
            count = history_integer(source.get('record_count'), 3, 5000)
            raw, chunks = [], []
            for index in range((count+99)//100):
                key = {'pk': pk+'#SOURCE', 'sk': f'RAW#{index:04d}'}
                response = self.client.get_item(TableName=self.table, Key=self._encode(key), ConsistentRead=True)
                chunk = self._decode(response.get('Item', {}))
                part = chunk.get('records')
                if chunk.get('pk') != key['pk'] or chunk.get('sk') != key['sk'] or \
                        history_integer(chunk.get('first_sequence')) != index*100+1 or type(part) is not list or \
                        len(part) != min(100, count-index*100) or any(type(line) is not str for line in part):
                    raise ValueError('complete bound combi chunk required')
                raw.extend(part); chunks.append(chunk)
            result.append((rows, raw, chunks))
        return result

    def observe_inputs(self, execution):
        if type(execution) is not Execution or not self.scope.matches_execution(execution) or not self.history.verify(execution):
            raise ValueError('verified combi source unavailable')
        try:
            started = self.clock()
            if type(started) is not datetime or started.utcoffset() is None: raise ValueError
            first = self._snapshot()
            second = self._snapshot()
            if not _identical(first[0], second[0]) or not _identical(first[1], second[1]):
                raise ValueError('combi source changed')
            now = self.clock()
            if type(now) is not datetime or now.utcoffset() is None or now < started: raise ValueError
            observed = tuple(observe_combi_batch(rows, raw, self.scope.binding.menu, now=now) for rows, raw, _ in first)
            b = self.scope.binding
            for batch, meal, job, version, digest in zip(observed,
                    (b.current_meal_id, b.prior_meal_id), (b.current_job_id, b.prior_job_id),
                    (b.current_meal_version, b.prior_meal_version), (b.current_source_sha256, b.prior_source_sha256)):
                if (batch.meal_id, batch.job_id, batch.meal_version, batch.source_digest, batch.baseline.size) != \
                        (meal, job, version, digest, self.scope.history.equipment_size):
                    raise ValueError('observed combi execution mismatch')
            build_combi_inputs(*observed)
            if not self.history.verify(execution): raise ValueError('combi history changed')
            return observed
        except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError, BotoCoreError, ClientError):
            raise ValueError('verified combi source unavailable') from None

    def verify(self, execution):
        try:
            self.observe_inputs(execution)
            return True
        except (ValueError, BotoCoreError, ClientError):
            return False

    def settlement_checks(self, execution, existing_auth_check):
        if type(execution) is not Execution or not self.scope.matches_execution(execution):
            raise ValueError('combi execution changed')
        # Compose with the registry USER operation; never duplicate a transaction
        # key. A caller must include all five checks in its atomic state update.
        return self.history.merge_auth_check(execution, existing_auth_check)
