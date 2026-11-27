"""Freeze a separately verified combi history observation; never issue coverage.

Caller must obtain the witness from the protected full-roster reader and prove
the live customer/execution and separate bounded source-read lease. No public
decoder or default credentials. These checks alone cannot authorize a model.
"""
from copy import deepcopy
import math
import re
import time
from uuid import uuid4

from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import BotoCoreError, ClientError

from foodtech_contracts.combi_history_witness import (CombiHistoryWitness, HISTORY_CONTRACT, COVERAGE_SCOPE,
    CONTROL_KEY, COVERAGE_FIELD)
from .policy import Execution


class DynamoCombiHistoryConditions:
    def __init__(self, witness, *, auth_table, cooking_table, account_id, client, clock=time.time):
        stage = re.fullmatch('foodtech-auth-sessions-([a-z0-9-]{1,32})', auth_table) if type(auth_table) is str else None
        if type(witness) is not CombiHistoryWitness or not stage or cooking_table != 'foodtech-cooking-jobs-' + stage[1] or \
                type(account_id) is not str or not re.fullmatch('[0-9]{12}', account_id) or client is None:
            raise ValueError('explicit protected combi history and condition client required')
        self.witness, self.auth_table, self.cooking_table = witness, auth_table, cooking_table
        self.cooking_arn = f'arn:aws:dynamodb:ap-southeast-2:{account_id}:table/{cooking_table}'
        self.client, self.clock, self.serializer = client, clock, TypeSerializer()

    def __repr__(self):
        return 'DynamoCombiHistoryConditions()'

    def _encode(self, values):
        return {key: self.serializer.serialize(value) for key, value in values.items()}

    def matches(self, execution):
        w = self.witness
        return type(execution) is Execution and getattr(execution, 'tenant_id', None) == w.owner and getattr(execution, 'feature', None) == 'RECIPE_CORRECTION' and \
            getattr(execution, 'data_classification', None) == 'SYNTHETIC' and getattr(execution, 'combi_binding', None) == w.binding and \
            (getattr(execution, 'job_id', None), getattr(execution, 'job_version', None),
             getattr(execution, 'prior_job_id', None), getattr(execution, 'prior_job_version', None)) == \
            (w.binding.current_job_id, w.binding.current_meal_version, w.binding.prior_job_id, w.binding.prior_meal_version)

    def settlement_checks(self, execution):
        if not self.matches(execution): raise ValueError('combi history/execution scope changed')
        instant = self.clock()
        if type(instant) not in (int, float) or not 0 < instant < 2**63 or not math.isfinite(instant):
            raise ValueError('valid combi condition clock required')
        now, w, encode = int(instant), self.witness, self._encode
        if not w.writer_verified_at <= now < w.writer_valid_until:
            raise ValueError('protected writer observation expired')
        names = {'#ch_epoch': 'epoch', '#ch_suspended': 'suspended', '#ch_cov': COVERAGE_FIELD,
            '#ch_state': 'state', '#ch_version': 'version', '#ch_generation': 'generation',
            '#ch_contract': 'writer_contract', '#ch_scope': 'coverage_scope', '#ch_manifest': 'writer_manifest_sha256',
            '#ch_revision': 'coverage_revision', '#ch_roster': 'baseline_roster_sha256', '#ch_proof': 'proof_sha256'}
        values = {':ch_epoch': w.binding.auth_epoch, ':ch_false': False, ':ch_active': 'ACTIVE',
            ':ch_version': w.coverage_version, ':ch_generation': w.generation, ':ch_contract': HISTORY_CONTRACT,
            ':ch_scope': COVERAGE_SCOPE, ':ch_manifest': w.writer_manifest_sha256, ':ch_revision': w.coverage_revision,
            ':ch_roster': w.baseline_roster_sha256, ':ch_proof': w.coverage_proof_sha256}
        checks = [{'ConditionCheck': {'TableName': self.auth_table, 'Key': encode({'pk': 'USER#' + w.owner}),
            'ConditionExpression': 'attribute_exists(pk) AND #ch_epoch = :ch_epoch AND '
                '(attribute_not_exists(#ch_suspended) OR #ch_suspended = :ch_false) AND attribute_not_exists(deletion_lock_request) AND '
                '#ch_cov.#ch_state = :ch_active AND #ch_cov.#ch_version = :ch_version AND '
                '#ch_cov.#ch_generation = :ch_generation AND #ch_cov.#ch_contract = :ch_contract AND '
                '#ch_cov.#ch_scope = :ch_scope AND #ch_cov.#ch_manifest = :ch_manifest AND '
                '#ch_cov.#ch_revision = :ch_revision AND #ch_cov.#ch_roster = :ch_roster AND #ch_cov.#ch_proof = :ch_proof',
            'ExpressionAttributeNames': names, 'ExpressionAttributeValues': encode(values),
            'ReturnValuesOnConditionCheckFailure': 'NONE'}},
            {'ConditionCheck': {'TableName': self.auth_table, 'Key': encode({'pk': CONTROL_KEY}),
                'ConditionExpression': '#s = :s AND #v = :v AND #g = :g AND writer_contract = :contract AND '
                    'cooking_table_arn = :table AND manifest_sha256 = :manifest AND verified_at = :verified AND '
                    'valid_until = :expires AND verified_at <= :now AND valid_until > :now',
                'ExpressionAttributeNames': {'#s': 'state', '#v': 'version', '#g': 'generation'},
                'ExpressionAttributeValues': encode({':s': 'ENFORCED', ':v': w.writer_version, ':g': w.generation,
                    ':contract': HISTORY_CONTRACT, ':table': self.cooking_arn, ':manifest': w.writer_manifest_sha256,
                    ':verified': w.writer_verified_at, ':expires': w.writer_valid_until, ':now': now}),
                'ReturnValuesOnConditionCheckFailure': 'NONE'}},
            {'ConditionCheck': {'TableName': self.cooking_table,
                'Key': encode({'pk': f'SCOPE#USER#{w.owner}#COMBI_MEALS', 'sk': 'HISTORY#REVISION'}),
                'ConditionExpression': '#revision = :revision', 'ExpressionAttributeNames': {'#revision': 'revision'},
                'ExpressionAttributeValues': encode({':revision': w.history_revision}),
                'ReturnValuesOnConditionCheckFailure': 'NONE'}}]
        for meal, version, job, digest in (
            (w.binding.current_meal_id, w.binding.current_meal_version, w.binding.current_job_id, w.binding.current_source_sha256),
            (w.binding.prior_meal_id, w.binding.prior_meal_version, w.binding.prior_job_id, w.binding.prior_source_sha256)):
            checks.append({'ConditionCheck': {'TableName': self.cooking_table,
                'Key': encode({'pk': f'SCOPE#USER#{w.owner}#COMBI_MEAL#{meal}', 'sk': 'META'}),
                'ConditionExpression': '#v = :v AND meal_batch_id = :meal AND #job = :job AND #source.device_id = :device AND '
                    '#source.raw_sha256 = :digest AND equipment_size = :size AND #provenance = :synthetic',
                'ExpressionAttributeNames': {'#v': 'version', '#source': 'source', '#provenance': 'provenance',
                    '#job': 'rice_cooking_job_id' if w.binding.menu == 'RICE' else 'mackerel_cooking_job_id'},
                'ExpressionAttributeValues': encode({':v': version, ':meal': meal, ':job': job, ':device': w.device_id,
                    ':digest': digest, ':size': w.equipment_size, ':synthetic': 'SYNTHETIC'}),
                'ReturnValuesOnConditionCheckFailure': 'NONE'}})
        return checks

    def merge_auth_check(self, execution, existing_auth_check):
        """Compose with a registry guard without two operations on the same USER."""
        checks = self.settlement_checks(execution)
        expected = checks[0]['ConditionCheck']
        if type(existing_auth_check) is not dict or set(existing_auth_check) != {'ConditionCheck'}:
            raise ValueError('one existing account condition required')
        old = deepcopy(existing_auth_check['ConditionCheck'])
        if type(old) is not dict or old.get('TableName') != self.auth_table or old.get('Key') != expected['Key'] or \
                type(old.get('ConditionExpression')) is not str or not old['ConditionExpression'] or \
                type(old.get('ExpressionAttributeNames', {})) is not dict or type(old.get('ExpressionAttributeValues', {})) is not dict or \
                old.get('ReturnValuesOnConditionCheckFailure', 'NONE') != 'NONE' or \
                set(old.get('ExpressionAttributeNames', {})) & set(expected['ExpressionAttributeNames']) or \
                set(old.get('ExpressionAttributeValues', {})) & set(expected['ExpressionAttributeValues']):
            raise ValueError('existing account scope or placeholders conflict')
        old['ConditionExpression'] = '(' + old['ConditionExpression'] + ') AND (' + expected['ConditionExpression'] + ')'
        old.setdefault('ExpressionAttributeNames', {}).update(expected['ExpressionAttributeNames'])
        old.setdefault('ExpressionAttributeValues', {}).update(expected['ExpressionAttributeValues'])
        old['ReturnValuesOnConditionCheckFailure'] = 'NONE'
        checks[0] = {'ConditionCheck': old}
        return checks

    def verify(self, execution):
        try:
            self.client.transact_write_items(TransactItems=self.settlement_checks(execution), ClientRequestToken=str(uuid4()))
            return True
        except (ValueError, BotoCoreError, ClientError):
            return False
