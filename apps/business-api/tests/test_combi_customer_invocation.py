import json
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,patch
from uuid import uuid4
from fastapi import HTTPException
from starlette.requests import Request

from foodtech_business_api.auth_store import Session
from foodtech_business_api.combi_customer_invocation import invoke_customer_combi


class CombiCustomerInvocationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.session=Session('customer','synthetic_owner',None,'synthetic-csrf',int(time.time())+500,2,
            access_token='PRIVATE_ACCESS',refresh_ciphertext=b'PRIVATE_REFRESH')
        self.identifier=str(uuid4())
        self.outcome={'execution_id':self.identifier,'state':'DENIED','response_text':None,'error_code':'TEST_ONLY'}
        self.env={'FOODTECH_COMBI_CUSTOMER_INVOCATION_ENABLED':'true','FOODTECH_PRINCIPAL_PROVISIONING_ENABLED':'true'}

    def request(self,body=b'',csrf=True):
        return Request({'type':'http','headers':[(b'x-csrf-token',b'synthetic-csrf')] if csrf else []},
            AsyncMock(return_value={'type':'http.request','body':body,'more_body':False}))

    async def invoke(self,request=None,delivery=None):
        with patch.dict('os.environ',self.env),patch('foodtech_business_api.combi_customer_invocation._session',AsyncMock(return_value=self.session)),\
                patch('foodtech_business_api.combi_customer_invocation._require_principal_record',AsyncMock()) as principal,\
                patch('foodtech_business_api.combi_customer_invocation.configured_bff_broker_settings',return_value=object()),\
                patch('foodtech_business_api.combi_customer_invocation.deliver_bff_delegation',new=delivery or AsyncMock(return_value=self.outcome)):
            response=await invoke_customer_combi(request or self.request(),self.identifier)
            return response,principal

    async def test_fresh_authenticated_metadata_only_crosses_broker_boundary(self):
        delivery=AsyncMock(return_value=self.outcome)
        response,principal=await self.invoke(delivery=delivery)
        metadata=delivery.call_args.args[0]
        self.assertEqual(metadata.subject,self.session.subject)
        self.assertEqual(metadata.epoch,2)
        self.assertEqual(principal.await_count,2)
        self.assertEqual(response.headers['cache-control'],'no-store')
        for value in (b'PRIVATE_ACCESS',b'PRIVATE_REFRESH',b'synthetic-csrf'):
            self.assertNotIn(value,metadata.encode_for_broker());self.assertNotIn(value,response.body)

    async def test_missing_csrf_body_injection_and_disabled_feature_do_not_send(self):
        for request,enabled in ((self.request(csrf=False),'true'),(self.request(b'{"subject":"foreign"}'),'true'),(self.request(),'false')):
            self.env['FOODTECH_COMBI_CUSTOMER_INVOCATION_ENABLED']=enabled
            delivery=AsyncMock(return_value=self.outcome)
            with self.subTest(enabled=enabled),self.assertRaises(HTTPException): await self.invoke(request,delivery)
            delivery.assert_not_awaited()

    async def test_mutated_shared_session_after_call_never_exposes_outcome(self):
        async def changed(*args):
            self.session.subject='foreign';return self.outcome
        with self.assertRaises(HTTPException) as error: await self.invoke(delivery=changed)
        self.assertEqual(error.exception.status_code,401)
