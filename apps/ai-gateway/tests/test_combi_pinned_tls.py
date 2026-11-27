"""Real local TLS and source consumer; SDK/account responses remain fixtures."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
import socket
import ssl
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI,Request
from fastapi.responses import JSONResponse
import httpx
import uvicorn

from foodtech_ai_gateway.combi_tls import broker_leaf_pin,combi_server_config
from foodtech_ai_gateway.combi_read_handoff import receive_internal_combi_handoff
from foodtech_business_api.combi_read_delivery import CombiDeliverySettings,CombiDeliveryUnavailable,_context,deliver_internal_combi_handoff
import combi_tls_fixture as tls
import test_combi_read_handoff as read_fixture


@asynccontextmanager
async def listening(app, certs, *, other_server=False, same_key=False):
    listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen(16)
    port=listener.getsockname()[1]
    cert,key=certs['other_server_same_key' if same_key else 'other_server' if other_server else 'server']
    config=combi_server_config(app,server_cert=cert,server_key=key,broker_ca=certs['ca'],broker_certificate=certs['broker'][0],port=port)
    server=uvicorn.Server(config);task=asyncio.create_task(server.serve(sockets=[listener]))
    loop=asyncio.get_running_loop();previous=loop.get_exception_handler()
    def expected_rejection(loop,context):
        error=context.get('exception')
        if context.get('message')=='Error on transport creation for incoming connection' and isinstance(error,(ssl.SSLError,ConnectionResetError)):
            return
        if previous: previous(loop,context)
        else: loop.default_exception_handler(context)
    loop.set_exception_handler(expected_rejection)
    try:
        for _ in range(500):
            if server.started: break
            if task.done(): await task
            await asyncio.sleep(.01)
        if not server.started: raise AssertionError('local TLS listener not started')
        yield f'https://127.0.0.1:{port}',config
    finally:
        server.should_exit=True
        await asyncio.wait_for(task,10)
        listener.close()
        loop.set_exception_handler(previous)


class CombiPinnedTlsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.certs=await asyncio.to_thread(tls.certificates,Path(self.temp.name))
        def receiver_fixture():
            data=read_fixture.CombiReceiverTests();data.setUp();return data
        self.data=await asyncio.to_thread(receiver_fixture)
        self.app=FastAPI();self.entered=[];self.observed=[];self.echo=False
        @self.app.post('/internal/ai/combi-recipe-executions/{identifier}/invoke')
        async def receive(request:Request,identifier:str):
            self.entered.append(identifier)
            if identifier!=self.data.snapshot.execution.execution_id:
                return JSONResponse({'detail':'EXECUTION_UNAVAILABLE'},status_code=404,headers={'Cache-Control':'no-store'})
            reads=await receive_internal_combi_handoff(request,snapshot=self.data.snapshot,
                expected_read_role_arn=self.data.role,expected_read_role_id=self.data.role_id,
                condition_client=self.data.data.condition_client,session_factory=self.data.factory,clock=lambda:self.data.now)
            try:
                batches=await asyncio.to_thread(reads.observe_inputs)
                self.observed.append(tuple(batch.meal_id for batch in batches))
            finally: reads.close()
            return JSONResponse({'execution_id':identifier,'state':'SUCCEEDED' if self.echo else 'DENIED',
                'response_text':'synthetic-not-a-real-secret' if self.echo else None,
                'error_code':None if self.echo else 'COMBI_EXECUTION_DISABLED'},headers={'Cache-Control':'no-store'})
        @self.app.post('/internal/ai/executions')
        async def unrelated():
            self.entered.append('unrelated');return {'unexpected':True}

    async def asyncTearDown(self): self.temp.cleanup()

    def settings(self,url,client='broker'):
        return CombiDeliverySettings(url,self.certs['server'][0],*self.certs[client])

    async def test_pinned_broker_handoff_reaches_source_guard_without_key_output_or_keylog(self):
        keylog=Path(self.temp.name)/'ambient-keylog'
        with patch.dict('os.environ',{'SSLKEYLOGFILE':str(keylog)}),self.assertLogs('httpx',level='DEBUG') as logs:
            async with listening(self.app,self.certs) as (url,config):
                result=await deliver_internal_combi_handoff(self.data.handoff,self.settings(url))
                self.assertEqual(result['state'],'DENIED');self.assertIsNone(result['response_text'])
                self.assertIsNone(config.ssl.keylog_filename)
        self.assertFalse(keylog.exists())
        self.assertEqual(self.observed,[(self.data.data.binding.current_meal_id,self.data.data.binding.prior_meal_id)])
        for marker in ('synthetic-not-a-real-secret','synthetic-not-a-real-token'):
            self.assertNotIn(marker,'\n'.join(logs.output));self.assertNotIn(marker,str(result))
        self.data.sts.close.assert_called_once();self.data.data.client.close.assert_called_once()

    async def test_other_leaf_same_ca_same_broker_identity_or_wrong_ca_never_enters_asgi(self):
        async with listening(self.app,self.certs) as (url,_):
            for client in ('other_broker','business','wrong_ca'):
                with self.subTest(client=client),self.assertRaisesRegex(CombiDeliveryUnavailable,'^Combi invocation unavailable$'):
                    await deliver_internal_combi_handoff(self.data.handoff,self.settings(url,client))
            anonymous=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            anonymous.load_verify_locations(cafile=self.certs['server'][0]);anonymous.verify_flags|=ssl.VERIFY_X509_PARTIAL_CHAIN
            async with httpx.AsyncClient(verify=anonymous,trust_env=False,timeout=5) as client:
                with self.assertRaises(httpx.TransportError):
                    await client.post(url+'/internal/ai/combi-recipe-executions/'+self.data.snapshot.execution.execution_id+'/invoke',content=b'invalid')
        self.assertEqual(self.entered,[]);self.data.factory.assert_not_called()

    async def test_different_server_leaf_with_same_ca_and_hostname_is_rejected(self):
        async with listening(self.app,self.certs,other_server=True) as (url,_):
            with self.assertRaises(CombiDeliveryUnavailable):
                await deliver_internal_combi_handoff(self.data.handoff,self.settings(url))
        self.assertEqual(self.entered,[])

    async def test_different_server_certificate_even_with_reused_key_requires_new_pin(self):
        async with listening(self.app,self.certs,same_key=True) as (url,_):
            with self.assertRaises(CombiDeliveryUnavailable):
                await deliver_internal_combi_handoff(self.data.handoff,self.settings(url))
        self.assertEqual(self.entered,[])

    async def test_generic_internal_route_is_not_exposed_on_broker_listener(self):
        async with listening(self.app,self.certs) as (url,_):
            async with httpx.AsyncClient(verify=_context(self.settings(url)),trust_env=False) as client:
                for method,path in [('POST','/internal/ai/executions'),('GET','/internal/ai/combi-recipe-executions/'+self.data.snapshot.execution.execution_id+'/invoke'),
                    ('POST','/internal/ai/combi-recipe-executions/'+self.data.snapshot.execution.execution_id+'/invoke?extra=1')]:
                    response=await client.request(method,url+path)
                    self.assertEqual(response.status_code,404);self.assertEqual(response.headers['cache-control'],'no-store')
        self.assertEqual(self.entered,[])

    async def test_even_a_valid_outcome_cannot_return_credential_material(self):
        self.echo=True
        async with listening(self.app,self.certs) as (url,_):
            with self.assertRaisesRegex(CombiDeliveryUnavailable,'^Combi invocation unavailable$'):
                await deliver_internal_combi_handoff(self.data.handoff,self.settings(url))

    async def test_generic_listener_on_same_app_cannot_reuse_global_authentication_flags(self):
        async with listening(self.app,self.certs):
            listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen(16)
            port=listener.getsockname()[1]
            cert,key=self.certs['server']
            server=uvicorn.Server(uvicorn.Config(self.app,ssl_certfile=cert,ssl_keyfile=key,ssl_ca_certs=self.certs['ca'],
                ssl_cert_reqs=ssl.CERT_REQUIRED,proxy_headers=False,access_log=False,log_level='error'))
            task=asyncio.create_task(server.serve(sockets=[listener]))
            try:
                for _ in range(500):
                    if server.started:break
                    await asyncio.sleep(.01)
                self.assertTrue(server.started)
                settings=self.settings(f'https://127.0.0.1:{port}','business')
                async with httpx.AsyncClient(verify=_context(settings),trust_env=False) as client:
                    response=await client.post(settings.gateway_url+'/internal/ai/combi-recipe-executions/'+self.data.snapshot.execution.execution_id+'/invoke',
                        content=self.data.raw,headers={'Content-Type':'application/vnd.foodtech.combi-source-read+json',
                            'foodtech_combi_peer_sha256':self.app.state.combi_broker_peer_sha256,'X-Combi-Broker-Listener':'true'})
                self.assertEqual(response.status_code,401)
                self.data.factory.assert_not_called()
            finally:
                server.should_exit=True;await asyncio.wait_for(task,10);listener.close()

    async def test_pin_configuration_rejects_generic_client_ca_or_bundle_before_flags(self):
        for path in (self.certs['business'][0],self.certs['ca'],self.certs['server'][0]):
            with self.subTest(path=Path(path).name),self.assertRaises(ValueError): broker_leaf_pin(path)
        app=FastAPI()
        with self.assertRaises(ValueError):
            combi_server_config(app,server_cert=self.certs['server'][0],server_key=self.certs['server'][1],
                broker_ca=self.certs['ca'],broker_certificate=self.certs['business'][0])
        self.assertFalse(getattr(app.state,'combi_broker_listener',False))
        bundle=Path(self.temp.name)/'bundle.pem'
        bundle.write_bytes(Path(self.certs['broker'][0]).read_bytes()+Path(self.certs['ca']).read_bytes())
        with self.assertRaises(ValueError):broker_leaf_pin(bundle)
        for path in (self.certs['ca'],self.certs['broker'][0],str(bundle)):
            with self.assertRaises(ValueError):_context(replace(self.settings('https://127.0.0.1'),server_certificate=path))
