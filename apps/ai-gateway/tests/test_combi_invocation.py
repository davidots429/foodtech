"""Combi private controller/identity checks; SDK responses are stored fixtures."""
import asyncio
from dataclasses import replace
import os
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import AsyncMock, MagicMock, Mock, patch
from uuid import uuid4

from fastapi import HTTPException
import httpx
from starlette.requests import Request
from foodtech_ai_gateway.combi_invocation import CombiRuntimeUnavailable,configured_combi_runtime,invoke_observed_combi
from foodtech_ai_gateway.main import app
from foodtech_ai_gateway.combi_read_handoff import receive_internal_combi_handoff
from foodtech_contracts.combi_read_handoff import CONTENT_TYPE
import test_combi_guarded_service as fixtures


class CombiRuntimeIdentityTests(unittest.TestCase):
    def setUp(self):
        self.env={'FOODTECH_STAGE':'dev','AWS_EXECUTION_ENV':'AWS_ECS_FARGATE',
            'FOODTECH_COMBI_LISTENER_ENABLED':'true','FOODTECH_COMBI_INVOCATION_ENABLED':'true',
            'FOODTECH_PRINCIPAL_PROVISIONING_ENABLED':'true',
            'FOODTECH_AI_REGISTRY_TABLE':'foodtech-ai-execution-registry-dev','FOODTECH_AUTH_TABLE':'foodtech-auth-sessions-dev',
            'FOODTECH_COMBI_GATEWAY_TASK_ROLE_ARN':'arn:aws:iam::123456789012:role/gateway-dev',
            'FOODTECH_COMBI_GATEWAY_TASK_ROLE_ID':'AROASYNTHETICGATEWAY1',
            'FOODTECH_COMBI_SOURCE_READ_ROLE_ARN':'arn:aws:iam::123456789012:role/foodtech-combi-source-read-dev',
            'FOODTECH_COMBI_SOURCE_READ_ROLE_ID':'AROASYNTHETICREADROLE'}
        self.session=MagicMock();self.session.get_credentials.return_value=SimpleNamespace(method='container-role')
        self.sts,self.client,self.resource=MagicMock(),MagicMock(),MagicMock()
        self.sts.meta.endpoint_url='https://sts.ap-southeast-2.amazonaws.com'
        for client in (self.client,self.resource.meta.client):
            client.meta.endpoint_url='https://dynamodb.ap-southeast-2.amazonaws.com'
        self.identity={'Account':'123456789012','Arn':'arn:aws:sts::123456789012:assumed-role/gateway-dev/session-1',
            'UserId':self.env['FOODTECH_COMBI_GATEWAY_TASK_ROLE_ID']+':session-1'}
        self.sts.get_caller_identity.return_value=self.identity
        self.session.client.side_effect=lambda name,**kw:self.sts if name=='sts' else self.client
        self.session.resource.return_value=self.resource

    def configure(self,**changes):
        with patch.dict(os.environ,{**self.env,**changes}),patch('foodtech_ai_gateway.combi_invocation.boto3.Session',return_value=self.session):
            return configured_combi_runtime()

    def test_verified_gateway_task_has_separate_registry_and_no_source_credentials(self):
        role,role_id,client,registry=self.configure()
        self.assertEqual(role,self.env['FOODTECH_COMBI_SOURCE_READ_ROLE_ARN'])
        self.assertEqual(role_id,self.env['FOODTECH_COMBI_SOURCE_READ_ROLE_ID'])
        self.assertIs(client,self.client);self.assertIs(registry.resource,self.resource)
        self.assertTrue(registry.require_principal)
        self.sts.close.assert_called_once()
        for call in self.session.client.call_args_list+self.session.resource.call_args_list:
            self.assertEqual(call.kwargs['region_name'],'ap-southeast-2')
            self.assertTrue(call.kwargs['config'].ignore_configured_endpoint_urls)
            self.assertEqual(call.kwargs['config'].proxies,{})

    def test_disabled_malformed_or_mismatched_configuration_rejected_before_sdk(self):
        for changes in ({'FOODTECH_COMBI_INVOCATION_ENABLED':'false'}, {'FOODTECH_COMBI_LISTENER_ENABLED':'false'},
            {'FOODTECH_PRINCIPAL_PROVISIONING_ENABLED':'false'},{'AWS_EXECUTION_ENV':''},
            {'FOODTECH_STAGE':'other'},{'FOODTECH_AUTH_TABLE':'other'},
            {'FOODTECH_COMBI_SOURCE_READ_ROLE_ID':'invalid'},
            {'FOODTECH_COMBI_GATEWAY_TASK_ROLE_ID':self.env['FOODTECH_COMBI_SOURCE_READ_ROLE_ID']},
            {'FOODTECH_COMBI_SOURCE_READ_ROLE_ARN':self.env['FOODTECH_COMBI_GATEWAY_TASK_ROLE_ARN']}):
            with self.subTest(changes=changes),self.assertRaises(CombiRuntimeUnavailable): self.configure(**changes)
        self.session.client.assert_not_called()

    def test_developer_credentials_wrong_identity_or_recreated_role_never_reads_registry(self):
        for method,identity in [('env',self.identity),('container-role',{**self.identity,'UserId':'AROARECREATEDROLE123:session-1'}),
            ('container-role',{**self.identity,'Account':'999999999999'}),
            ('container-role',{**self.identity,'Arn':self.identity['Arn'].replace('gateway-dev','other')})]:
            self.session.get_credentials.return_value.method=method
            self.sts.get_caller_identity.return_value=identity
            with self.subTest(method=method,identity=identity),self.assertRaises(CombiRuntimeUnavailable): self.configure()
        self.session.resource.assert_not_called()
        self.client.get_item.assert_not_called()

    def test_partial_initialization_or_endpoint_redirect_closes_created_clients(self):
        self.session.resource.side_effect=ValueError('private-marker')
        with self.assertRaisesRegex(CombiRuntimeUnavailable,'^combi runtime unavailable$'): self.configure()
        self.client.close.assert_called_once();self.sts.close.assert_called_once()
        self.session.resource.side_effect=None
        self.client.meta.endpoint_url='https://other.invalid'
        with self.assertRaises(CombiRuntimeUnavailable): self.configure()
        self.resource.meta.client.close.assert_called_once()


class CombiInvocationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.data=fixtures.CombiGuardedServiceTests();self.data.setUp()
        self.addCleanup(self.data.doCleanups)
        self.registry=self.data.registry;self.source=self.data.data.source
        self.data.client.close=Mock()
        self.resource_client=Mock()
        self.registry.resource.meta=SimpleNamespace(client=self.resource_client)
        self.runtime=(self.source.role,self.source.role_id,self.data.client,self.registry)
        self.source.sts.close.reset_mock()
        self.source.data.client.close.reset_mock()

    def request(self, *, authenticated=True):
        return Request({'type':'http','scheme':'https','app':SimpleNamespace(state=SimpleNamespace(
            mtls_listener=True,combi_broker_listener=True,combi_broker_peer_sha256='a'*64)),
            'state':{'foodtech_combi_peer_sha256':'a'*64 if authenticated else 'b'*64},
            'headers':[(b'content-type',CONTENT_TYPE.encode())]},
            AsyncMock(return_value={'type':'http.request','body':self.source.raw,'more_body':False}))

    async def receive(self,request,**kwargs):
        return await receive_internal_combi_handoff(request,**kwargs,session_factory=self.source.factory,clock=lambda:self.source.now)

    async def invoke(self,request=None):
        with patch.dict(os.environ,{'FOODTECH_COMBI_INVOCATION_ENABLED':'true'}),\
                patch('foodtech_ai_gateway.combi_invocation.configured_combi_runtime',return_value=self.runtime),\
                patch('foodtech_ai_gateway.combi_invocation.receive_internal_combi_handoff',new=self.receive):
            return await invoke_observed_combi(request or self.request(),self.data.execution.execution_id,ollama=self.data.ollama)

    async def test_actual_frame_controller_policy_observations_and_settlement_complete(self):
        response=await self.invoke()
        self.assertEqual(response.status_code,200)
        self.assertIn(b'SUCCEEDED',response.body)
        self.assertEqual(response.headers['cache-control'],'no-store')
        self.assertEqual(self.data.data.record['state'],'SUCCEEDED')
        self.data.client.close.assert_called_once();self.resource_client.close.assert_called_once()
        for secret in (b'synthetic-not-a-real-secret',b'synthetic-not-a-real-token'):
            self.assertNotIn(secret,response.body)

    async def test_transport_denial_precedes_sdk_registry_and_body(self):
        request=self.request(authenticated=False)
        with patch.dict(os.environ,{'FOODTECH_COMBI_INVOCATION_ENABLED':'true'}),\
                patch('foodtech_ai_gateway.combi_invocation.configured_combi_runtime') as runtime:
            with self.assertRaises(HTTPException) as error:
                await invoke_observed_combi(request,str(uuid4()),ollama=self.data.ollama)
        self.assertEqual(error.exception.status_code,401);runtime.assert_not_called()
        request._receive.assert_not_awaited()

    async def test_changed_snapshot_closes_received_lease_without_provider(self):
        snapshot=self.registry.execution_snapshot(self.data.execution.execution_id)
        with patch.object(self.registry,'execution_snapshot',side_effect=[snapshot,replace(snapshot,version=2)]):
            with self.assertRaises(HTTPException) as error: await self.invoke()
        self.assertEqual(error.exception.status_code,409)
        self.data.ollama.structured_chat.assert_not_awaited()
        self.source.sts.close.assert_called_once();self.data.client.close.assert_called_once()

    async def test_cancelled_runtime_initialization_closes_late_resources(self):
        started,release=threading.Event(),threading.Event()
        def initialize():
            started.set();release.wait(3);return self.runtime
        with patch.dict(os.environ,{'FOODTECH_COMBI_INVOCATION_ENABLED':'true'}),\
                patch('foodtech_ai_gateway.combi_invocation.configured_combi_runtime',side_effect=initialize):
            task=asyncio.create_task(invoke_observed_combi(self.request(),self.data.execution.execution_id,ollama=self.data.ollama))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait,2));task.cancel()
                with self.assertRaises(asyncio.CancelledError): await task
            finally: release.set()
            for _ in range(100):
                if self.data.client.close.called: break
                await asyncio.sleep(.01)
            self.data.client.close.assert_called_once();self.resource_client.close.assert_called_once()

    async def test_shared_http_route_cannot_use_global_flags_or_spoofed_header(self):
        previous={key:getattr(app.state,key,None) for key in ('mtls_listener','combi_broker_listener','combi_broker_peer_sha256')}
        try:
            app.state.mtls_listener=True;app.state.combi_broker_listener=True;app.state.combi_broker_peer_sha256='a'*64
            with patch.dict(os.environ,{'FOODTECH_COMBI_INVOCATION_ENABLED':'true'}),\
                    patch('foodtech_ai_gateway.combi_invocation.configured_combi_runtime') as runtime:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://test') as client:
                    response=await client.post('/internal/ai/combi-recipe-executions/'+self.data.execution.execution_id+'/invoke',
                        content=self.source.raw,headers={'X-Combi-Peer-Sha256':'a'*64,'Content-Type':CONTENT_TYPE})
                self.assertEqual(response.status_code,401);runtime.assert_not_called()
        finally:
            for key,value in previous.items():
                if value is None:
                    if hasattr(app.state,key): delattr(app.state,key)
                else: setattr(app.state,key,value)

    async def test_native_pinned_tls_to_controller_observed_model_and_stored_result(self):
        from pathlib import Path
        import tempfile
        import combi_tls_fixture as tls
        import test_combi_pinned_tls as network
        from foodtech_business_api.combi_read_delivery import CombiDeliverySettings,deliver_internal_combi_handoff
        from foodtech_ai_gateway.ollama import OllamaClient
        import json
        provider_requests=[]
        def provider(request):
            provider_requests.append(json.loads(request.content))
            return httpx.Response(200,json={'done':True,'message':{'role':'assistant','content':json.dumps(self.data.proposal)}})
        keys=('mtls_listener','combi_broker_listener','combi_broker_peer_sha256','ollama_client')
        previous={key:getattr(app.state,key,None) for key in keys}
        try:
            app.state.ollama_client=OllamaClient(transport=httpx.MockTransport(provider))
            with tempfile.TemporaryDirectory() as temp:
                certs=await asyncio.to_thread(tls.certificates,Path(temp))
                with patch.dict(os.environ,{'FOODTECH_COMBI_INVOCATION_ENABLED':'true'}),\
                        patch('foodtech_ai_gateway.combi_invocation.configured_combi_runtime',return_value=self.runtime),\
                        patch('foodtech_ai_gateway.combi_invocation.receive_internal_combi_handoff',new=self.receive):
                    async with network.listening(app,certs) as (url,_):
                        settings=CombiDeliverySettings(url,certs['server'][0],*certs['broker'])
                        result=await deliver_internal_combi_handoff(self.source.handoff,settings)
                self.assertEqual(result['state'],'SUCCEEDED',result)
                self.assertEqual(self.data.data.record['state'],'SUCCEEDED')
                self.assertEqual(len(provider_requests),1)
                self.assertIs(provider_requests[0]["stream"],False)
                self.assertFalse(provider_requests[0]["format"]["additionalProperties"])
                self.data.client.close.assert_called_once();self.resource_client.close.assert_called_once()
        finally:
            for key,value in previous.items():
                if value is None:
                    if hasattr(app.state,key): delattr(app.state,key)
                else: setattr(app.state,key,value)
