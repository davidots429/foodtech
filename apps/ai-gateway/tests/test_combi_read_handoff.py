"""Stored SDK fixtures for the private receiver; not an actual IAM/TLS proof."""
import asyncio
from dataclasses import replace
from datetime import timedelta
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from botocore.exceptions import ClientError
from fastapi import HTTPException
from starlette.requests import Request

from foodtech_ai_gateway.combi_read_handoff import _accept, CombiHandoffDenied, receive_internal_combi_handoff
from foodtech_ai_gateway.policy import ExecutionSnapshot
from foodtech_contracts.combi_read_handoff import CombiReadHandoff, CONTENT_TYPE, MAX_BYTES
import test_combi_source_guard as fixtures


class CombiReceiverTests(unittest.TestCase):
    def setUp(self):
        self.data = fixtures.CombiSourceGuardTests(); self.data.setUp()
        self.now = fixtures.NOW
        self.role = 'arn:aws:iam::123456789012:role/foodtech-combi-source-read-dev'
        self.role_id = 'AROASYNTHETICROLE12345'
        self.assumed = 'arn:aws:sts::123456789012:assumed-role/foodtech-combi-source-read-dev/combi-'+'f'*32
        self.snapshot = ExecutionSnapshot(self.data.execution, 'PENDING', 1)
        self.handoff = CombiReadHandoff(self.data.scope, self.role, self.assumed, self.now+timedelta(seconds=90),
            'SYNTHETICKEY1234567890', 'synthetic-not-a-real-secret', 'synthetic-not-a-real-token', clock=lambda:self.now)
        self.raw = self.handoff._encode_for_internal_tls()
        self.sts = Mock(); self.sts.meta.endpoint_url = 'https://sts.ap-southeast-2.amazonaws.com'
        self.sts.get_caller_identity.return_value = {'Account':'123456789012', 'Arn':self.assumed,
            'UserId':self.role_id+':'+self.assumed.rsplit('/',1)[1]}
        self.data.client.meta = SimpleNamespace(endpoint_url='https://dynamodb.ap-southeast-2.amazonaws.com')
        self.data.client.close = Mock()
        self.session = Mock(); self.session.get_credentials.return_value = SimpleNamespace(method='explicit')
        self.session.client.side_effect = lambda service, **kwargs: self.sts if service=='sts' else self.data.client
        self.factory = Mock(return_value=self.session)

    def accept(self, **changes):
        args = dict(snapshot=self.snapshot, expected_read_role_arn=self.role, expected_read_role_id=self.role_id,
            condition_client=self.data.condition_client, session_factory=self.factory, clock=lambda:self.now)
        args.update(changes)
        return _accept(self.raw, **args)

    def test_scoped_observations_round_trip_and_owned_clients_close_once(self):
        received = self.accept()
        observed = received.observe_inputs()
        self.assertEqual(tuple(v.meal_id for v in observed), (self.data.binding.current_meal_id, self.data.binding.prior_meal_id))
        self.assertTrue(received.verify(self.data.execution))
        self.assertEqual(repr(received), 'ReceivedCombiReads()')
        with self.assertRaises(TypeError): json.dumps(received)
        for call in self.session.client.call_args_list:
            self.assertTrue(call.kwargs['config'].ignore_configured_endpoint_urls)
            self.assertEqual(call.kwargs['config'].proxies,{})
            self.assertEqual(call.kwargs['region_name'],'ap-southeast-2')
        received.close(); received.close()
        self.sts.close.assert_called_once(); self.data.client.close.assert_called_once()
        self.assertFalse(received.verify(self.data.execution))

    def test_wrong_execution_snapshot_or_expected_role_never_uses_credentials(self):
        for changes in ({'snapshot':replace(self.snapshot,version=2)},
                        {'snapshot':replace(self.snapshot,execution=replace(self.data.execution,tenant_id='foreign'))},
                        {'expected_read_role_arn':self.role.replace('-dev','-demo')}, {'condition_client':None}):
            with self.subTest(changes=changes), self.assertRaises(CombiHandoffDenied): self.accept(**changes)
        self.factory.assert_not_called()

    def test_same_named_role_with_different_immutable_role_id_is_rejected(self):
        with self.assertRaises(CombiHandoffDenied): self.accept(expected_read_role_id='AROADIFFERENTROLE12345')
        self.sts.close.assert_called_once(); self.data.client.close.assert_not_called()
        self.assertEqual(len(self.session.client.call_args_list),1)

    def test_live_history_denial_precedes_temporary_session_construction(self):
        self.data.condition_client.transact_write_items.side_effect = ClientError({'Error':{'Code':'TransactionCanceledException'}},'TransactWriteItems')
        with self.assertRaises(CombiHandoffDenied): self.accept()
        self.factory.assert_not_called()

    def test_non_explicit_credentials_and_endpoint_redirection_fail_closed(self):
        self.session.get_credentials.return_value.method='env'
        with self.assertRaises(CombiHandoffDenied): self.accept()
        self.session.client.assert_not_called()
        self.session.get_credentials.return_value.method='explicit'
        self.data.client.meta.endpoint_url='https://unreviewed.invalid'
        with self.assertRaises(CombiHandoffDenied): self.accept()
        self.sts.close.assert_called_once(); self.data.client.close.assert_called_once()

    def test_identity_failure_never_echoes_sdk_credential_error(self):
        self.sts.get_caller_identity.side_effect = ClientError({'Error':{'Code':'AccessDenied','Message':'synthetic-not-a-real-secret'}},'GetCallerIdentity')
        with self.assertRaisesRegex(CombiHandoffDenied,'^combi read handoff unavailable$'): self.accept()
        self.sts.close.assert_called_once()

    def test_received_expiry_is_checked_before_any_source_read(self):
        received = self.accept()
        self.now += timedelta(seconds=61)
        self.assertFalse(received.verify(self.data.execution))
        self.assertEqual(self.data.client.calls,[])
        received.close()


class CombiReceiverHttpTests(unittest.IsolatedAsyncioTestCase):
    def request(self, body=b'frame', *, tls=True, listener=True, combi_listener=True, peer_pin='a'*64, headers=None):
        receive=AsyncMock(return_value={'type':'http.request','body':body,'more_body':False})
        request=Request({'type':'http','scheme':'https' if tls else 'http',
            'app':SimpleNamespace(state=SimpleNamespace(mtls_listener=listener,combi_broker_listener=combi_listener,combi_broker_peer_sha256='a'*64)),
            'state':{'foodtech_combi_peer_sha256':peer_pin},
            'headers':headers if headers is not None else [(b'content-type',CONTENT_TYPE.encode())]}, receive)
        return request,receive

    async def call(self, request):
        return await receive_internal_combi_handoff(request,snapshot=object(),expected_read_role_arn='unused',
            expected_read_role_id='unused',condition_client=object())

    async def test_untrusted_listener_or_scheme_is_rejected_before_reading_body(self):
        for changes in ({'tls':False},{'listener':False},{'combi_listener':False},{'peer_pin':None},{'peer_pin':'b'*64}):
            request,body=self.request(**changes)
            with self.assertRaises(HTTPException) as caught: await self.call(request)
            self.assertEqual(caught.exception.status_code,401)
            body.assert_not_called()

    async def test_type_encoding_length_and_size_errors_never_parse_credentials(self):
        for headers,body in [([],b'x'), ([(b'content-type',b'application/json')],b'x'),
            ([(b'content-type',CONTENT_TYPE.encode()),(b'content-encoding',b'gzip')],b'x'),
            ([(b'content-type',CONTENT_TYPE.encode()),(b'content-length',b'00x')],b'x'),
            ([(b'content-type',CONTENT_TYPE.encode()),(b'content-length',b'2')],b'x'),
            ([(b'content-type',CONTENT_TYPE.encode())],b'x'*(MAX_BYTES+1))]:
            request,_=self.request(body,headers=headers)
            with patch('foodtech_ai_gateway.combi_read_handoff._accept',Mock()) as accept:
                with self.assertRaises(HTTPException) as caught: await self.call(request)
                self.assertEqual(caught.exception.status_code,400)
                self.assertEqual(caught.exception.detail,'COMBI_READ_HANDOFF_UNAVAILABLE')
                accept.assert_not_called()

    async def test_cancelled_receive_disposes_eventual_unpublished_clients(self):
        started,release,closed=asyncio.Event(),asyncio.Event(),asyncio.Event()
        result=Mock();result.close.side_effect=closed.set
        async def accept(*args,**kwargs):
            started.set();await release.wait();return result
        request,_=self.request()
        with patch('foodtech_ai_gateway.combi_read_handoff.asyncio.to_thread',side_effect=accept):
            task=asyncio.create_task(self.call(request));await started.wait();task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
            release.set();await asyncio.wait_for(closed.wait(),1)
        result.close.assert_called_once()
