"""SDK-shaped transaction contracts; not actual AWS authorization evidence."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import unittest

from botocore.session import get_session
from botocore.validate import validate_parameters

from foodtech_ai_gateway.policy import Model, Tier
from foodtech_ai_gateway.registry import DynamoRegistry, RegistryConflict
import test_registry as storage
import test_combi_read_handoff as receiver


class CombiAtomicClient(storage.AtomicClient):
    """Reject selected protected conditions before the existing atomic fixture commits."""
    def __init__(self, table):
        super().__init__(table)
        self.requests, self.reject_target = [], None
        self.model = get_session().get_service_model('dynamodb')

    def transact_write_items(self, **args):
        validate_parameters(args, self.model.operation_model('TransactWriteItems').input_shape)
        actions = deepcopy(args['TransactItems'])
        targets = []
        protected = []
        for action in actions:
            kind, spec = next(iter(action.items()))
            key = spec.get('Key', spec.get('Item'))
            decoded = self._decode(key)
            target = (spec['TableName'], decoded['pk'], decoded.get('sk'))
            targets.append(target)
            if kind == 'ConditionCheck' and spec['TableName'] != 'registry-test':
                protected.append(target)
                if target == self.reject_target:
                    self.requests.append(actions)
                    raise storage.TransactionCanceledException()
        if len(targets) != len(set(targets)):
            raise AssertionError('duplicate transaction item')
        if len(protected) > 1:
            if len(protected) != 5: raise AssertionError('all five protected checks required')
            account = next(x['ConditionCheck'] for x in actions if 'ConditionCheck' in x and
                self._decode(x['ConditionCheck']['Key'])['pk'].startswith('USER#'))
            if any('ConditionCheck' not in action for action in actions):
                assert ':expected_epoch' in account['ExpressionAttributeValues']
            assert '#ch_cov.#ch_proof' in account['ConditionExpression']
            assert 'deletion_lock_request' in account['ConditionExpression']
            assert all(x['ConditionCheck']['ReturnValuesOnConditionCheckFailure'] == 'NONE'
                for x in actions if 'ConditionCheck' in x and x['ConditionCheck']['TableName'] != 'registry-test')
        self.requests.append(actions)
        remaining = [x for x in actions if not ('ConditionCheck' in x and x['ConditionCheck']['TableName'] != 'registry-test')]
        super().transact_write_items(TransactItems=remaining, ClientRequestToken=args['ClientRequestToken'])


class CombiAtomicExecutionTests(unittest.TestCase):
    def setUp(self):
        self.source = receiver.CombiReceiverTests(); self.source.setUp()
        self.guard = self.source.accept()
        self.addCleanup(self.guard.close)
        self.execution = self.source.data.execution
        self.resource = storage.MemoryResource()
        self.client = CombiAtomicClient(self.resource.table)
        self.registry = DynamoRegistry('registry-test', '2026-11', resource=self.resource,
            client=self.client, auth_table='foodtech-auth-sessions-dev', local_models={'llama3.1:8b'})
        now = self.source.now
        self.registry.create_model(Model('llama3.1:8b','ollama',Tier.TIER_3,'ACTIVE',True,True,now,now))
        self.registry.create_execution(self.execution)
        for scope, name in (('TENANT',self.execution.tenant_id),('PROVIDER','ollama'),('PROJECT','GLOBAL')):
            self.registry.create_limit(scope,name,Decimal(0),**({'calls_remaining':3} if scope=='TENANT' else {}))
        self.client.requests.clear()

    @property
    def record(self): return self.resource.table.items[('EXEC#'+self.execution.execution_id,'META')]

    def refresh(self):
        self.registry.execution(self.execution.execution_id)
        self.registry.model(self.execution.model_id)
        self.registry.budget(self.execution.tenant_id,'ollama')

    def reserve(self, guard=True):
        self.refresh()
        self.registry.reserve(self.execution,'ollama',combi_source_guard=self.guard if guard else None)

    def retry(self, attempt):
        self.refresh()
        self.registry.reserve_local_retry(self.execution,attempt,combi_source_guard=self.guard)

    def finish(self, succeeded=True, guard=True):
        self.registry.finish(self.execution.execution_id,succeeded=succeeded,actual_cost_usd=Decimal(0),
            response_text='sanitized candidate' if succeeded else None,error_code=None if succeeded else 'SOURCE_CHANGED',
            combi_source_guard=self.guard if guard else None)

    def test_reserve_retry_and_finish_preserve_binding_and_charge_each_call(self):
        self.reserve()
        for attempt in (2,3): self.retry(attempt)
        self.finish()
        self.assertEqual(self.record['state'],'SUCCEEDED')
        self.assertEqual(self.record['version'],5)
        self.assertEqual(self.registry.execution(self.execution.execution_id),self.execution)
        self.assertEqual(self.registry.budget(self.execution.tenant_id,'ollama').calls_remaining,0)
        self.assertEqual(len(self.client.requests),4)

    def test_each_protected_conflict_leaves_all_records_unchanged_at_each_phase(self):
        for phase in ('reserve','retry','finish'):
            for index in range(5):
                with self.subTest(phase=phase,index=index):
                    self.setUp()
                    if phase != 'reserve': self.reserve()
                    targets = self.guard.settlement_checks(self.registry._active_tenant_guard(
                        self.execution.tenant_id,expected_epoch=self.execution.combi_binding.auth_epoch))
                    spec = targets[index]['ConditionCheck']; key = self.client._decode(spec['Key'])
                    self.client.reject_target = (spec['TableName'],key['pk'],key.get('sk'))
                    before = deepcopy(self.resource.table.items)
                    with self.assertRaises(RegistryConflict):
                        if phase=='reserve': self.reserve()
                        elif phase=='retry': self.retry(2)
                        else: self.finish()
                    self.assertEqual(self.resource.table.items,before)
                    if phase=='finish':
                        self.finish(False,False)
                        self.assertEqual(self.record['state'],'FAILED')
                        self.assertEqual(self.registry.budget(self.execution.tenant_id,'ollama').calls_remaining,2)

    def test_missing_guard_cannot_reserve_retry_or_publish_through_generic_finish(self):
        with self.assertRaises(RegistryConflict): self.reserve(False)
        self.reserve()
        self.refresh()
        with self.assertRaises(RegistryConflict): self.registry.reserve_local_retry(self.execution,2)
        with self.assertRaises(RegistryConflict): self.finish(guard=False)
        self.assertNotIn(('EXEC#'+self.execution.execution_id,'RESULT'),self.resource.table.items)
        self.finish(False,False)

    def test_changed_execution_snapshot_or_retry_progression_is_not_accepted(self):
        self.record['version'] = 2
        with self.assertRaises(RegistryConflict): self.reserve()
        self.record['version'] = 1
        self.reserve()
        self.record['version'] += 1
        with self.assertRaises(RegistryConflict): self.retry(2)
        with self.assertRaises(RegistryConflict): self.finish()

    def test_foreign_execution_closed_and_expired_leases_fail_before_write(self):
        self.refresh()
        with self.assertRaises(RegistryConflict):
            self.registry.reserve(replace(self.execution,tenant_id='foreign'),'ollama',combi_source_guard=self.guard)
        self.source.now += timedelta(seconds=61)
        with self.assertRaises(RegistryConflict): self.reserve()
        self.guard.close()
        with self.assertRaises(RegistryConflict): self.reserve()
        self.assertEqual(self.client.requests,[])

    def test_wrong_authentication_table_is_not_composed(self):
        self.registry.auth_table='foodtech-auth-sessions-other'
        with self.assertRaises(RegistryConflict): self.reserve()
        self.assertEqual(self.client.requests,[])

    def test_local_only_provider_and_invalid_retry_count_fail_closed(self):
        self.refresh()
        with self.assertRaises(RegistryConflict):
            self.registry.reserve(self.execution,'external',combi_source_guard=self.guard)
        self.reserve()
        for invalid in (True,-1,3,Decimal('1.1')):
            self.record['retry_count']=invalid
            with self.subTest(invalid=invalid),self.assertRaises(RegistryConflict): self.finish()

    def test_omitted_or_changed_caller_binding_cannot_bypass_stored_combi_guard(self):
        self.refresh()
        legacy = replace(self.execution,combi_binding=None)
        with self.assertRaises(RegistryConflict): self.registry.reserve(legacy,'ollama')
        self.reserve()
        self.refresh()
        with self.assertRaises(RegistryConflict): self.registry.reserve_local_retry(legacy,2)
        self.assertEqual(self.record['state'],'RESERVED')
        self.assertEqual(self.registry.budget(self.execution.tenant_id,'ollama').calls_remaining,2)
