"""Real local BFF mTLS -> broker/history/lease -> fixture Gateway outcome."""
import asyncio
from contextlib import asynccontextmanager
import importlib.util
from pathlib import Path
import socket
import ssl
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,Mock,patch
from uuid import UUID

from fastapi import HTTPException
from starlette.requests import Request
import httpx
import uvicorn
from foodtech_business_api.combi_broker_app import app,invoke
from foodtech_business_api.combi_broker_runtime import CombiBrokerRuntime
from foodtech_business_api.combi_broker_tls import broker_server_config,PEER_STATE
from foodtech_business_api.combi_customer_invocation import deliver_bff_delegation,CombiBrokerDeliveryUnavailable
from foodtech_business_api.combi_delegation import CONTENT_TYPE,MAX_BYTES
from foodtech_business_api.combi_read_delivery import CombiDeliverySettings,_context
import test_combi_broker_delegation as flow_fixture

spec=importlib.util.spec_from_file_location('foodtech_ephemeral_combi_certs',Path(__file__).resolve().parents[2]/'ai-gateway/tests/combi_tls_fixture.py')
tls=importlib.util.module_from_spec(spec);spec.loader.exec_module(tls)


@asynccontextmanager
async def listening(certs):
    listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen(16)
    port=listener.getsockname()[1]
    config=broker_server_config(app,server_cert=certs['server'][0],server_key=certs['server'][1],
        bff_ca=certs['ca'],bff_certificate=certs['business'][0],port=port)
    server=uvicorn.Server(config);task=asyncio.create_task(server.serve(sockets=[listener]))
    loop=asyncio.get_running_loop();previous=loop.get_exception_handler()
    def rejection(loop,context):
        if context.get('message')=='Error on transport creation for incoming connection' and isinstance(context.get('exception'),(ssl.SSLError,ConnectionResetError)): return
        if previous: previous(loop,context)
        else: loop.default_exception_handler(context)
    loop.set_exception_handler(rejection)
    try:
        for _ in range(500):
            if server.started: break
            if task.done(): await task
            await asyncio.sleep(.01)
        if not server.started: raise AssertionError('broker TLS listener not started')
        yield f'https://127.0.0.1:{port}'
    finally:
        server.should_exit=True
        await asyncio.wait_for(task,10);listener.close();loop.set_exception_handler(previous)


class CombiBrokerAppTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.flow=flow_fixture.CombiDelegationFlowTests();self.flow.setUp()
        self.client_close=Mock()
        self.runtime=CombiBrokerRuntime(self.flow.data.broker,self.flow.reader,self.flow.data.data.data.authority,self.flow.gateway,[self.client_close])
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.certs=await asyncio.to_thread(tls.certificates,Path(self.temp.name))
        old=getattr(app.state,'combi_bff_leaf_sha256',None)
        self.addCleanup(self.restore_pin,old)

    def restore_pin(self,old):
        if old is None:
            if hasattr(app.state,'combi_bff_leaf_sha256'): del app.state.combi_bff_leaf_sha256
        else: app.state.combi_bff_leaf_sha256=old

    def settings(self,url,peer='business'):
        return CombiDeliverySettings(url,self.certs['server'][0],*self.certs[peer])

    async def test_real_bff_pin_stored_execution_history_lease_and_no_credential_response(self):
        with patch.dict('os.environ',{'FOODTECH_COMBI_BROKER_ENABLED':'true'}),\
                patch('foodtech_business_api.combi_customer_invocation.time.time',new=self.flow.clock),\
                patch('foodtech_business_api.combi_broker_app.configured_combi_broker_runtime',return_value=self.runtime) as factory,\
                patch('foodtech_business_api.combi_broker_invocation.deliver_internal_combi_handoff',new=self.flow.delivery):
            async with listening(self.certs) as url:
                result=await deliver_bff_delegation(self.flow.delegation,self.settings(url))
        self.assertEqual(result,self.flow.result)
        self.assertEqual(factory.call_args.args[0],self.flow.delegation)
        self.flow.data.sts.assume_role.assert_called_once();self.flow.delivery.assert_awaited_once()
        self.client_close.close.assert_called_once()
        self.assertNotIn('synthetic-not-a-real-secret',str(result));self.assertNotIn('synthetic-not-a-real-token',str(result))

    async def test_same_ca_same_bff_uri_other_leaf_and_broker_peer_never_enter_runtime(self):
        with patch.dict('os.environ',{'FOODTECH_COMBI_BROKER_ENABLED':'true'}),\
                patch('foodtech_business_api.combi_customer_invocation.time.time',new=self.flow.clock),\
                patch('foodtech_business_api.combi_broker_app.configured_combi_broker_runtime') as factory:
            async with listening(self.certs) as url:
                for peer in ('other_business','broker','wrong_ca'):
                    with self.subTest(peer=peer),self.assertRaises(CombiBrokerDeliveryUnavailable):
                        await deliver_bff_delegation(self.flow.delegation,self.settings(url,peer))
            factory.assert_not_called()

    async def test_broker_listener_rejects_other_routes_queries_and_shared_listener_spoofing(self):
        async with listening(self.certs) as url:
            async with httpx.AsyncClient(verify=_context(self.settings(url)),trust_env=False) as client:
                for path in ('/api/customer/auth/me','/internal/combi/invocations/'+self.flow.delegation.execution_id+'?extra=1'):
                    response=await client.post(url+path,content=b'not-a-delegation')
                    self.assertEqual(response.status_code,404)
        with patch('foodtech_business_api.combi_broker_app.configured_combi_broker_runtime') as factory:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://generic') as client:
                response=await client.post('/internal/combi/invocations/'+self.flow.delegation.execution_id,
                    content=self.flow.delegation.encode_for_broker(),headers={CONTENT_TYPE:'ignored','X-Combi-Bff-Peer':'spoofed'})
            self.assertEqual(response.status_code,401);factory.assert_not_called()

    def request(self,raw,authenticated=True):
        app.state.combi_bff_leaf_sha256='a'*64
        return Request({'type':'http','scheme':'https','app':app,'state':{PEER_STATE:'a'*64 if authenticated else 'b'*64},
            'headers':[(b'content-type',CONTENT_TYPE.encode())]},AsyncMock(return_value={'type':'http.request','body':raw,'more_body':False}))

    async def test_transport_and_frame_size_are_checked_before_runtime(self):
        with patch.dict('os.environ',{'FOODTECH_COMBI_BROKER_ENABLED':'true'}),\
                patch('foodtech_business_api.combi_broker_app.configured_combi_broker_runtime') as factory:
            request=self.request(b'not-a-frame',authenticated=False)
            with self.assertRaises(HTTPException) as error: await invoke(request,UUID(self.flow.delegation.execution_id))
            self.assertEqual(error.exception.status_code,401);request._receive.assert_not_awaited()
            with self.assertRaises(HTTPException) as error:
                await invoke(self.request(b'x'*(MAX_BYTES+1)),UUID(self.flow.delegation.execution_id))
            self.assertEqual(error.exception.status_code,400);factory.assert_not_called()

    async def test_cancelled_initialization_closes_late_owned_runtime(self):
        started,release=threading.Event(),threading.Event()
        def initialize(_):
            started.set();release.wait(3);return self.runtime
        with patch.dict('os.environ',{'FOODTECH_COMBI_BROKER_ENABLED':'true'}),\
                patch('foodtech_business_api.combi_broker_app.configured_combi_broker_runtime',side_effect=initialize):
            task=asyncio.create_task(invoke(self.request(self.flow.delegation.encode_for_broker()),UUID(self.flow.delegation.execution_id)))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait,2));task.cancel()
                with self.assertRaises(asyncio.CancelledError): await task
            finally: release.set()
            for _ in range(100):
                if self.client_close.close.called: break
                await asyncio.sleep(.01)
            self.client_close.close.assert_called_once()
