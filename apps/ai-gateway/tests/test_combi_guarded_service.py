"""Observed Combi -> real policy/transactions -> validated model candidate fixtures."""
from dataclasses import replace
from decimal import Decimal
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from botocore.exceptions import EndpointConnectionError
from foodtech_ai_gateway.policy import Caller, Consent
from foodtech_ai_gateway.service import GatewayService
from foodtech_ai_gateway.combi_recommendation_inputs import build_combi_inputs
import test_combi_atomic_execution as atomic
import test_combi_recommendation as proposals


class CombiGuardedServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.data = atomic.CombiAtomicExecutionTests(); self.data.setUp()
        self.addCleanup(self.data.guard.close)
        self.registry, self.client = self.data.registry, self.data.client
        self.registry.require_principal = True
        self.execution = replace(self.data.execution,downgrade_consent_id='synthetic-consent',parent_execution_id=str(uuid4()))
        self.data.record.update(downgrade_consent_id=self.execution.downgrade_consent_id,
            parent_execution_id=self.execution.parent_execution_id)
        self.registry.create_consent(Consent('synthetic-consent',self.execution.execution_id,self.execution.tenant_id,
            self.execution.model_id,'TIER_DOWNGRADE','synthetic-notice-v1',self.data.source.now,
            self.execution.parent_execution_id,True,'LOCAL',Decimal(0)))
        self.data.source.snapshot = replace(self.data.source.snapshot,execution=self.execution)
        self.data.guard.close()
        self.guard = self.data.source.accept();self.addCleanup(self.guard.close)
        self.service = GatewayService(self.registry)
        self.caller = Caller('business-api',self.execution.tenant_id)
        self.ollama = SimpleNamespace(structured_chat=AsyncMock(side_effect=self.answer))
        self.responses = []
        self.after_call = None
        observed = self.guard.observe_inputs()
        self.evidence,self.baseline,self.context = build_combi_inputs(*observed)
        self.proposal = proposals.proposed()
        self.proposal['pan_positions'] = [{'rack':1,'shelf':i+1} for i in range(self.baseline.pan_count)]
        self.client.requests.clear()

    async def answer(self,*_):
        if self.after_call: self.after_call()
        return SimpleNamespace(text=self.responses.pop(0) if self.responses else json.dumps(self.proposal))

    async def execute(self, caller=None):
        return await self.service.execute_observed_combi_recipe(caller or self.caller,self.execution.execution_id,
            combi_source_guard=self.guard,ollama=self.ollama)

    async def test_observed_inputs_produce_stored_review_candidate_and_safe_model_context(self):
        result = await self.execute()
        self.assertEqual(result.state,'SUCCEEDED',result)
        value=json.loads(result.response_text)
        self.assertEqual(value['status'],'NEEDS_REVIEW')
        self.assertEqual(self.data.record['state'],'SUCCEEDED')
        self.assertEqual(self.registry.budget(self.caller.tenant_id,'ollama').calls_remaining,2)
        messages=self.ollama.structured_chat.call_args.args[1]
        for secret in ('private-actor','private-note','synthetic-not-a-real-secret','synthetic-not-a-real-token',self.caller.tenant_id):
            self.assertNotIn(secret,json.dumps(messages))

    async def test_invalid_output_retry_charges_calls_and_checks_same_protected_history(self):
        self.responses=['invalid','{}',json.dumps(self.proposal)]
        result=await self.execute()
        self.assertEqual(result.state,'SUCCEEDED',result)
        self.assertEqual(json.loads(result.response_text)['attempts'],3)
        self.assertEqual(self.registry.budget(self.caller.tenant_id,'ollama').calls_remaining,0)
        self.assertEqual(self.ollama.structured_chat.await_count,3)

    async def test_generic_entry_cannot_inject_combi_baseline_even_with_valid_read_object(self):
        result=await self.service.execute_local_recipe(self.caller,self.execution.execution_id,
            evidence=self.evidence,baseline=self.baseline,context=self.context,ollama=self.ollama)
        self.assertEqual(result.error_code,'COMBI_SOURCE_SCOPE_UNAVAILABLE')
        self.ollama.structured_chat.assert_not_awaited()

    async def test_missing_downgrade_consent_blocks_before_provider(self):
        self.data.record['downgrade_consent_id']=None
        # Rebind the received object to a stored execution that has no consent;
        # scope alone cannot bypass feature/Tier policy.
        self.execution=replace(self.execution,downgrade_consent_id=None)
        self.data.source.snapshot=replace(self.data.source.snapshot,execution=self.execution)
        self.guard.close();self.guard=self.data.source.accept();self.addCleanup(self.guard.close)
        result=await self.execute()
        self.assertEqual(result.state,'DENIED')
        self.assertEqual(result.error_code,'TIER_NOT_ALLOWED')
        self.ollama.structured_chat.assert_not_awaited()

    async def test_source_change_after_provider_persists_failure_without_refunding_call(self):
        self.after_call=lambda: self.data.source.data.client.items.pop(next(iter(self.data.source.data.client.items)))
        result=await self.execute()
        self.assertEqual(result.state,'FAILED',result)
        self.assertIsNone(result.response_text)
        self.assertEqual(self.registry.budget(self.caller.tenant_id,'ollama').calls_remaining,2)
        self.assertIsNone(self.registry.execution_result(self.execution.execution_id)['response_text'])

    async def test_settlement_condition_conflict_does_not_publish_candidate(self):
        def revoke():
            spec=self.guard.settlement_checks(self.registry._active_tenant_guard(self.caller.tenant_id,
                expected_epoch=self.execution.combi_binding.auth_epoch))[2]['ConditionCheck']
            key=self.client._decode(spec['Key'])
            self.client.reject_target=(spec['TableName'],key['pk'],key.get('sk'))
        self.after_call=revoke
        result=await self.execute()
        self.assertEqual((result.state,result.error_code),('FAILED','RECIPE_SETTLEMENT_CONFLICT'))
        self.assertEqual(self.registry.budget(self.caller.tenant_id,'ollama').calls_remaining,2)
        self.assertIsNone(result.response_text)

    async def test_unknown_success_settlement_is_indeterminate_and_not_retried(self):
        original=self.registry.finish
        def unknown(*args,**kwargs):
            if kwargs['succeeded']: raise EndpointConnectionError(endpoint_url='https://dynamodb.invalid')
            return original(*args,**kwargs)
        with patch.object(self.registry,'finish',side_effect=unknown): result=await self.execute()
        self.assertEqual(result.state,'INDETERMINATE')
        self.assertIsNone(result.response_text)
        self.ollama.structured_chat.assert_awaited_once()
        self.assertEqual(self.data.record['state'],'RESERVED')

    async def test_foreign_caller_and_closed_guard_do_not_consume_calls(self):
        result=await self.execute(Caller('business-api','other'))
        self.assertEqual(result.state,'DENIED')
        self.guard.close()
        result=await self.execute()
        self.assertEqual(result.state,'DENIED')
        self.ollama.structured_chat.assert_not_awaited()
        self.assertEqual(self.registry.budget(self.caller.tenant_id,'ollama').calls_remaining,3)

    async def test_exhausted_budget_denies_without_provider_or_refund(self):
        limit=self.data.resource.table.items[('LIMIT#2026-11#TENANT#'+self.caller.tenant_id,'META')]
        limit['calls_remaining']=0
        result=await self.execute()
        self.assertEqual((result.state,result.error_code),('DENIED','LIMIT_ACTION_REQUIRED'))
        self.ollama.structured_chat.assert_not_awaited()
        self.assertEqual(self.data.record['state'],'PENDING')

    async def test_model_revoked_before_retry_preserves_only_consumed_call(self):
        self.responses=['not JSON']
        def revoke():
            model=self.data.resource.table.items[('MODEL#'+self.execution.model_id,'META')]
            model.update(state='INACTIVE',version=model['version']+1)
        self.after_call=revoke
        result=await self.execute()
        self.assertEqual((result.state,result.error_code),('FAILED','MODEL_NOT_ACTIVE'))
        self.ollama.structured_chat.assert_awaited_once()
        self.assertEqual(self.registry.budget(self.caller.tenant_id,'ollama').calls_remaining,2)

    async def test_mackerel_uses_same_observed_path_and_menu_specific_schema(self):
        from foodtech_contracts.combi_read_handoff import CombiReadHandoff
        source=self.data.source;self.guard.close()
        current,prior=(meta for meta,_ in source.data.records)
        binding=replace(self.execution.combi_binding,menu='MACKEREL',
            current_job_id=current['mackerel_cooking_job_id'],prior_job_id=prior['mackerel_cooking_job_id'])
        self.execution=replace(self.execution,combi_binding=binding,
            job_id=binding.current_job_id,prior_job_id=binding.prior_job_id)
        witness=replace(source.data.witness,binding=binding)
        scope=replace(source.data.scope,binding=binding,history=witness)
        source.snapshot=replace(source.snapshot,execution=self.execution)
        source.handoff=CombiReadHandoff(scope,source.role,source.assumed,source.handoff.expires_at,
            'SYNTHETICKEY1234567890','synthetic-not-a-real-secret','synthetic-not-a-real-token',clock=lambda:source.now)
        source.raw=source.handoff._encode_for_internal_tls()
        from dataclasses import asdict
        self.data.record.update(job_id=binding.current_job_id,prior_job_id=binding.prior_job_id,combi_binding=asdict(binding))
        self.guard=source.accept();self.addCleanup(self.guard.close)
        _,baseline,_=build_combi_inputs(*self.guard.observe_inputs())
        self.proposal=proposals.proposed('MACKEREL')
        self.proposal['pan_positions']=[{'rack':1,'shelf':i+1} for i in range(baseline.pan_count)]
        result=await self.execute()
        self.assertEqual(result.state,'SUCCEEDED',result)
        schema=self.ollama.structured_chat.call_args.args[2]
        self.assertIn('transition_duration_seconds',schema['properties'])
        self.assertNotIn('effective_water_ratio',schema['properties'])

    async def test_unknown_response_after_committed_settlement_is_not_overwritten_or_reinvoked(self):
        original=self.registry.finish
        def lost_response(*args,**kwargs):
            original(*args,**kwargs)
            if kwargs['succeeded']: raise EndpointConnectionError(endpoint_url='https://dynamodb.invalid')
        with patch.object(self.registry,'finish',side_effect=lost_response): result=await self.execute()
        self.assertEqual(result.state,'INDETERMINATE')
        self.assertIsNone(result.response_text)
        self.assertEqual(self.data.record['state'],'SUCCEEDED')
        self.ollama.structured_chat.assert_awaited_once()
        self.assertEqual(self.registry.budget(self.caller.tenant_id,'ollama').calls_remaining,2)
