"""Condition-only history freshness, combined with live source authorization.

The witness must originate in the separately trusted full-history reader, never
customer/model fields. These checks freeze its protected records and ordering
revision; they do not independently audit the roster or issue attestations.
No source reads, default credentials, public decoder or IAM provisioning here.
"""
import re
import math
import time
from copy import deepcopy
from uuid import uuid4

from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import BotoCoreError, ClientError
from foodtech_contracts.prior_history_witness import HISTORY_CONTRACT, PriorHistoryWitness

from .recipe_read_authorization import DynamoRecipeReadAuthorization


class DynamoPriorHistoryConditions:
    def __init__(self, witness, *, auth_table, cooking_table, account_id, client, clock=time.time):
        match = re.fullmatch(r'foodtech-auth-sessions-([a-z0-9-]{1,32})', auth_table or '')
        if type(witness) is not PriorHistoryWitness or not match or cooking_table != 'foodtech-cooking-jobs-' + match[1] or \
                not isinstance(account_id, str) or not re.fullmatch('[0-9]{12}', account_id) or client is None:
            raise ValueError('explicit protected history witness and condition client required')
        self.witness, self.auth_table, self.cooking_table = witness, auth_table, cooking_table
        self.cooking_arn = f'arn:aws:dynamodb:ap-southeast-2:{account_id}:table/{cooking_table}'
        self.client, self.clock, self.serializer = client, clock, TypeSerializer()

    def __repr__(self):
        return 'DynamoPriorHistoryConditions()'

    def _encode(self, values):
        return {k: self.serializer.serialize(v) for k, v in values.items()}

    def matches(self, execution, authorization):
        w = self.witness
        return type(authorization) is DynamoRecipeReadAuthorization and authorization.client is self.client and \
            authorization.observation.auth_table == self.auth_table and authorization.observation.auth_epoch == w.auth_epoch and \
            authorization.observation.execution_id == getattr(execution, 'execution_id', None) and \
            (getattr(execution, 'tenant_id', None), getattr(execution, 'feature', None),
             getattr(execution, 'job_id', None), getattr(execution, 'prior_job_id', None),
             getattr(execution, 'job_version', None), getattr(execution, 'prior_job_version', None)) == \
            (w.owner, 'RECIPE_CORRECTION', w.current_job_id, w.prior_job_id, *w.job_versions)

    def settlement_checks(self, execution, authorization, source_partitions):
        if not self.matches(execution, authorization):
            raise ValueError('history/execution/source authorization scope changed')
        now = self.clock()
        if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now) or not 0 < now < 2**63:
            raise ValueError('valid prior-history condition clock required')
        now = int(now)
        w, encode = self.witness, self._encode
        checks = deepcopy(authorization.settlement_checks(execution, source_partitions))
        account = checks[0]['ConditionCheck']
        names = {'#hist': 'history_coverage', '#hs': 'state', '#hv': 'version', '#hg': 'generation',
            '#hc': 'writer_contract', '#hscope': 'coverage_scope', '#hm': 'writer_manifest_sha256',
            '#hbase': 'coverage_revision', '#hroster': 'baseline_roster_sha256', '#hproof': 'proof_sha256'}
        values = encode({':hactive': 'ACTIVE', ':hv': w.coverage_version, ':hg': w.generation,
            ':hc': HISTORY_CONTRACT, ':hscope': 'ALL_RETAINED_JOBS', ':hm': w.writer_manifest_sha256,
            ':hbase': w.coverage_revision, ':hroster': w.baseline_roster_sha256, ':hproof': w.coverage_proof_sha256})
        if set(names) & account.get('ExpressionAttributeNames', {}).keys() or \
                set(values) & account.get('ExpressionAttributeValues', {}).keys():
            raise ValueError('history authorization placeholder conflict')
        account['ConditionExpression'] = ('(' + account['ConditionExpression'] + ') AND (' +
            '#hist.#hs = :hactive AND #hist.#hv = :hv AND #hist.#hg = :hg AND #hist.#hc = :hc '
            'AND #hist.#hscope = :hscope AND #hist.#hm = :hm AND #hist.#hbase = :hbase '
            'AND #hist.#hroster = :hroster AND #hist.#hproof = :hproof)')
        account.setdefault('ExpressionAttributeNames', {}).update(names)
        account.setdefault('ExpressionAttributeValues', {}).update(values)
        checks.extend([
            {'ConditionCheck': {'TableName': self.auth_table,
                'Key': encode({'pk': 'CONTROL#COOKING_HISTORY_WRITERS'}),
                'ConditionExpression': '#s = :enforced AND #v = :version AND generation = :generation '
                    'AND writer_contract = :contract AND cooking_table_arn = :table AND manifest_sha256 = :manifest '
                    'AND verified_at <= :now AND valid_until > :now',
                'ExpressionAttributeNames': {'#s': 'state', '#v': 'version'},
                'ExpressionAttributeValues': encode({':enforced': 'ENFORCED', ':version': w.writer_version,
                    ':generation': w.generation, ':contract': HISTORY_CONTRACT, ':table': self.cooking_arn,
                    ':manifest': w.writer_manifest_sha256, ':now': now}), 'ReturnValuesOnConditionCheckFailure': 'NONE'}},
            {'ConditionCheck': {'TableName': self.cooking_table,
                'Key': encode({'pk': 'SCOPE#USER#' + w.owner + '#JOBS', 'sk': 'HISTORY#REVISION'}),
                'ConditionExpression': 'revision = :revision',
                'ExpressionAttributeValues': encode({':revision': w.history_revision}),
                'ReturnValuesOnConditionCheckFailure': 'NONE'}},
        ])
        return checks

    def verify(self, execution, authorization, source_partitions):
        try:
            self.client.transact_write_items(TransactItems=self.settlement_checks(execution, authorization, source_partitions),
                                            ClientRequestToken=str(uuid4()))
            return True
        except (ValueError, BotoCoreError, ClientError):
            return False
