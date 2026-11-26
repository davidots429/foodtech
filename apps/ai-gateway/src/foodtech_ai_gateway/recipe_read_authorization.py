"""Condition-only live grant checks, also included in successful settlement.

The injected client must independently permit the reviewed ConditionCheckItem
operations. No default AWS identity, source GetItem/Query or role issuance is
provided. A receipt alone never authorizes a model call.
"""
import math
import time
from uuid import uuid4

from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import BotoCoreError, ClientError
from foodtech_contracts.source_read_authorization import SourceReadAuthorization


class DynamoRecipeReadAuthorization:
    def __init__(self, observation, *, client, clock=time.time):
        if type(observation) is not SourceReadAuthorization or client is None:
            raise ValueError('explicit source authorization observation and client required')
        self.observation, self.client, self.clock = observation, client, clock
        self.serializer = TypeSerializer()

    def __repr__(self):
        return 'DynamoRecipeReadAuthorization()'

    def matches(self, execution, source_partitions):
        receipt = self.observation
        return (getattr(execution, 'execution_id', None), getattr(execution, 'tenant_id', None), source_partitions) == \
            (receipt.execution_id, receipt.subject, receipt.source_partitions)

    def settlement_checks(self, execution, source_partitions):
        if not self.matches(execution, source_partitions):
            raise ValueError('source authorization scope changed')
        now = self.clock()
        receipt = self.observation
        if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now) or \
                not 0 < now < min(receipt.expires_at):
            raise ValueError('source authorization expired')
        encode = lambda row: {key: self.serializer.serialize(value) for key, value in row.items()}
        # Placeholders protect opaque source IDs containing punctuation from
        # interpretation as DynamoDB document paths.
        checks = [{'ConditionCheck': {'TableName': receipt.auth_table,
            'Key': encode({'pk': 'USER#' + receipt.subject}),
            'ConditionExpression': 'attribute_exists(pk) AND #epoch = :epoch AND '
                '(attribute_not_exists(suspended) OR suspended = :no) AND attribute_not_exists(deletion_lock_request) '
                'AND #grants.#first = :first AND #grants.#second = :second',
            'ExpressionAttributeNames': {'#epoch': 'epoch', '#grants': 'source_read_grants',
                '#first': receipt.source_partitions[0], '#second': receipt.source_partitions[1]},
            'ExpressionAttributeValues': encode({':epoch': receipt.auth_epoch, ':no': False,
                ':first': receipt.grant_ids[0], ':second': receipt.grant_ids[1]}),
            'ReturnValuesOnConditionCheckFailure': 'NONE'}}]
        for source, grant, version, expires in zip(receipt.source_partitions, receipt.grant_ids,
                receipt.grant_versions, receipt.expires_at, strict=True):
            checks.append({'ConditionCheck': {'TableName': receipt.catalog_table,
                'Key': encode({'pk': 'GRANT#' + grant, 'sk': 'META'}),
                'ConditionExpression': '#schema = :schema AND grant_id = :grant AND source_partition = :source '
                    'AND #permission = :read AND #state = :active AND #version = :version '
                    'AND expires_at = :expires AND expires_at > :now',
                'ExpressionAttributeNames': {'#schema': 'schema', '#permission': 'permission',
                    '#state': 'state', '#version': 'version'},
                'ExpressionAttributeValues': encode({':schema': 1, ':grant': grant, ':source': source,
                    ':read': 'RECIPE_SOURCE_READ', ':active': 'ACTIVE', ':version': version,
                    ':expires': expires, ':now': int(now)}), 'ReturnValuesOnConditionCheckFailure': 'NONE'}})
        return checks

    def verify(self, execution, source_partitions):
        try:
            checks = self.settlement_checks(execution, source_partitions)
            self.client.transact_write_items(TransactItems=checks, ClientRequestToken=str(uuid4()))
            return True
        except (ValueError, BotoCoreError, ClientError):
            # Denied, revoked, missing or unavailable are all non-authorizations.
            return False
