import asyncio
from dataclasses import replace
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,Mock,patch

from foodtech_business_api.combi_delegation import CombiInvocationDelegation,MAX_BYTES
from foodtech_business_api.combi_execution_reader import DynamoCombiExecutionReader
from foodtech_business_api.combi_broker_invocation import invoke_delegated_combi
from foodtech_business_api.combi_read_delivery import CombiDeliverySettings
from foodtech_business_api.recipe_scope import RecipeScopeDenied
import test_combi_read_broker as broker_fixture
import test_combi_execution_reader as reader_fixture


class CombiDelegationFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.data=broker_fixture.CombiReadBrokerTests();self.data.setUp()
        self.clock=lambda:self.data.now.timestamp()
        self.delegation=CombiInvocationDelegation(self.data.scope.owner,self.data.scope.binding.auth_epoch,
            self.data.scope.execution_id,int(self.clock())+500)
        self.client=reader_fixture.ExecutionClient(self.data.data)
        self.reader=DynamoCombiExecutionReader(table_name='foodtech-ai-execution-registry-dev',client=self.client,
            subject=self.delegation.subject,epoch=self.delegation.epoch)
        frozen=SimpleNamespace(access_key='SYNTHETICKEY1234567890',secret_key='synthetic-not-a-real-secret',token='synthetic-not-a-real-token')
        self.data.sdk_session.get_credentials.return_value=SimpleNamespace(method='explicit',get_frozen_credentials=lambda:frozen)
        original=self.data.response
        def response(**args):
            result=original(**args);result['Credentials']['AccessKeyId']=frozen.access_key;return result
        self.data.sts.assume_role.side_effect=response
        self.gateway=CombiDeliverySettings('https://gateway.internal','synthetic-server.pem','synthetic-broker.pem','synthetic-broker.key')
        self.result={'execution_id':self.delegation.execution_id,'state':'DENIED','response_text':None,'error_code':'TEST_ONLY'}
        self.delivery=AsyncMock(return_value=self.result)

    async def invoke(self):
        with patch('foodtech_business_api.combi_broker_invocation.deliver_internal_combi_handoff',new=self.delivery):
            return await invoke_delegated_combi(self.delegation,broker=self.data.broker,reader=self.reader,
                history=self.data.data.data.authority,gateway=self.gateway,clock=self.clock)

    async def test_full_stored_history_and_lease_are_revalidated_and_only_outcome_returns(self):
        result=await self.invoke()
        self.assertEqual(result,self.result)
        self.delivery.assert_awaited_once();self.data.sts.assume_role.assert_called_once()
        handoff=self.delivery.call_args.args[0]
        self.assertEqual(handoff.scope.read_partitions,self.data.scope.read_partitions)
        self.assertGreaterEqual(len(self.client.calls),6)
        self.assertNotIn('synthetic-not-a-real-secret',json.dumps(result))

    async def test_expired_or_mismatched_delegation_never_issues_lease(self):
        for changes in ({'session_expires':int(self.clock())},{'subject':'foreign'},{'epoch':3}):
            with self.subTest(changes=changes):
                original=self.delegation;self.delegation=replace(original,**changes)
                with self.assertRaises(RecipeScopeDenied): await self.invoke()
                self.delegation=original
        self.data.sts.assume_role.assert_not_called();self.delivery.assert_not_awaited()

    async def test_history_change_after_sts_blocks_delivery_and_drops_lease(self):
        original=self.data.sts.assume_role.side_effect
        def change(**args):
            result=original(**args);self.data.data.data.history['revision']+=1;return result
        self.data.sts.assume_role.side_effect=change
        with self.assertRaises(RecipeScopeDenied): await self.invoke()
        self.delivery.assert_not_awaited()

    async def test_terminal_execution_after_delivery_is_not_replayed(self):
        async def terminal(*args):
            self.client.row['state']='SUCCEEDED';return self.result
        self.delivery.side_effect=terminal
        result=await self.invoke()
        self.assertEqual(result,self.result)
        self.data.sts.assume_role.assert_called_once();self.delivery.assert_awaited_once()

    async def test_revoked_account_after_delivery_never_returns_candidate(self):
        async def revoked(*args):
            self.data.data.data.account['suspended']=True;return self.result
        self.delivery.side_effect=revoked
        with self.assertRaises(RecipeScopeDenied): await self.invoke()
        self.delivery.assert_awaited_once()

    async def test_cancelled_sts_issue_closes_late_unpublished_lease(self):
        import threading
        started,release=threading.Event(),threading.Event()
        original=self.data.broker.issue
        issued=[]
        def issue(*args,**kwargs):
            started.set();release.wait(3)
            result=original(*args,**kwargs);issued.append(result);return result
        with patch.object(self.data.broker,'issue',side_effect=issue):
            task=asyncio.create_task(self.invoke())
            try:
                self.assertTrue(await asyncio.to_thread(started.wait,2));task.cancel()
                with self.assertRaises(asyncio.CancelledError): await task
            finally: release.set()
            for _ in range(200):
                if issued:
                    try: issued[0].client(issued[0].scope)
                    except RecipeScopeDenied: break
                await asyncio.sleep(.01)
            self.assertEqual(len(issued),1)
            with self.assertRaises(RecipeScopeDenied): issued[0].client(issued[0].scope)
            self.delivery.assert_not_awaited()



class CombiDelegationFormatTests(unittest.TestCase):
    def setUp(self):
        import uuid
        self.value=CombiInvocationDelegation('synthetic_owner',2,str(uuid.uuid4()),9999999999)

    def test_exact_bounded_format_has_no_session_tokens_or_csrf(self):
        raw=self.value.encode_for_broker()
        self.assertLess(len(raw),MAX_BYTES)
        self.assertEqual(CombiInvocationDelegation.decode_authenticated_bff(raw),self.value)
        self.assertEqual(set(json.loads(raw)),{'schema','subject','epoch','execution_id','session_expires'})
        self.assertEqual(repr(self.value),'CombiInvocationDelegation()')

    def test_duplicate_unknown_boolean_numeric_and_large_fields_are_rejected(self):
        raw=self.value.encode_for_broker()
        cases=[raw.replace(b'"epoch":2',b'"epoch":2,"epoch":3'),raw[:-1]+b',"refresh_token":"no"}',
            raw.replace(b'"epoch":2',b'"epoch":true'),raw.replace(b'"epoch":2',b'"epoch":2.0'),
            b'x'*(MAX_BYTES+1),b'{"subject":NaN}',raw.replace(b'9999999999',b'false')]
        for value in cases:
            with self.subTest(value=value[:80]),self.assertRaises(ValueError):
                CombiInvocationDelegation.decode_authenticated_bff(value)
