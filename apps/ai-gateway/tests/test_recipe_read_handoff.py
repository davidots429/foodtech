"""Synthetic temporary keys only; SDK calls are injected mocks."""
from dataclasses import replace, asdict
from datetime import datetime, timedelta, timezone
import json
import pickle
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock
from uuid import uuid4

from botocore.exceptions import ClientError
from foodtech_contracts.prior_history_witness import PriorHistoryWitness
import boto3
from botocore.config import Config
import httpx
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder

from foodtech_ai_gateway.recipe_read_handoff import RecipeHandoffDenied, _accept, receive_internal_recipe_handoff
from foodtech_ai_gateway.recipe_source_guard import BoundRecipeSourceGuard
from foodtech_contracts.recipe_read_handoff import CONTENT_TYPE, MAX_BYTES, RecipeReadHandoff
from foodtech_contracts.recipe_read_scope import RecipeReadScope
from foodtech_contracts.source_read_authorization import SourceReadAuthorization


class RecipeReadHandoffTests(unittest.TestCase):
    def setUp(self):
        now = datetime.now(timezone.utc)
        self.owner, self.execution_id = 'opaque_subject', str(uuid4())
        self.job_ids = (str(uuid4()), str(uuid4()))
        self.scope = RecipeReadScope(self.owner, self.execution_id,
            tuple(f'SCOPE#USER#{self.owner}#JOB#{job}' for job in self.job_ids),
            ('JEYUK#jeyuk-current', 'JEYUK#jeyuk-prior'), (2, 1), (12, 11), 'PENDING', 1, 3)
        self.authorization = SourceReadAuthorization('foodtech-auth-sessions-dev', 'foodtech-source-access-dev',
            self.owner, self.execution_id, 3, self.scope.source_partitions, (str(uuid4()), str(uuid4())),
            (1, 2), (int(now.timestamp()) + 900,) * 2)
        self.role = 'arn:aws:iam::123456789012:role/foodtech-recipe-source-read-dev'
        self.arn = 'arn:aws:sts::123456789012:assumed-role/foodtech-recipe-source-read-dev/recipe-' + 'a' * 32
        self.handoff = RecipeReadHandoff(self.scope, self.authorization, self.role, self.arn,
            now + timedelta(seconds=899), 'ASIA' + 'A' * 16, 'synthetic-secret-marker', 'synthetic-token-marker')
        self.raw = self.handoff._encode_for_internal_tls()
        self.snapshot = SimpleNamespace(state='PENDING', version=1, execution=SimpleNamespace(
            execution_id=self.execution_id, tenant_id=self.owner, feature='RECIPE_CORRECTION',
            job_id=self.job_ids[0], prior_job_id=self.job_ids[1], job_version=2, prior_job_version=1,
            jeyuk_source_revision=12, prior_source_revision=11))
        self.session = MagicMock()
        self.session.get_credentials.return_value = SimpleNamespace(method='explicit')
        self.session.client.return_value.meta.endpoint_url = 'https://sts.ap-southeast-2.amazonaws.com'
        self.session.client.return_value.get_caller_identity.return_value = {'Account': '123456789012', 'Arn': self.arn}
        self.resource = self.session.resource.return_value
        self.resource.meta.client.meta.endpoint_url = 'https://dynamodb.ap-southeast-2.amazonaws.com'
        self.resource.Table.side_effect = lambda name: SimpleNamespace(name=name)
        self.sessions, self.conditions = MagicMock(return_value=self.session), MagicMock()

    def accept(self, **changes):
        arguments = dict(snapshot=self.snapshot, expected_read_role_arn=self.role,
                         condition_client=self.conditions, session_factory=self.sessions)
        return _accept(self.raw, **{**arguments, **changes})

    def test_only_explicit_tls_encoding_exposes_synthetic_credentials(self):
        self.assertNotIn('synthetic-secret-marker', repr(self.handoff))
        self.assertNotIn(self.owner, repr(self.handoff))
        with self.assertRaises(ValueError):
            jsonable_encoder(self.handoff)
        with self.assertRaises(TypeError):
            pickle.dumps(self.handoff)
        decoded = RecipeReadHandoff._decode_from_internal_tls(self.raw)
        self.assertEqual(decoded.scope, self.scope)
        self.assertEqual(decoded.authorization, self.authorization)
        self.assertLessEqual(len(self.raw), MAX_BYTES)

    def test_verified_scoped_session_binds_exact_execution_without_default_credentials(self):
        guard = self.accept()
        self.assertIs(type(guard), BoundRecipeSourceGuard)
        self.assertEqual(guard.execution, self.snapshot.execution)
        self.assertEqual(guard.source_partitions, self.scope.source_partitions)
        self.assertEqual(self.conditions.transact_write_items.call_count, 2)
        args = self.sessions.call_args.kwargs
        self.assertEqual(set(args), {'region_name', 'aws_access_key_id', 'aws_secret_access_key', 'aws_session_token'})
        self.assertEqual(args['region_name'], 'ap-southeast-2')
        config = self.session.client.call_args.kwargs['config']
        self.assertTrue(config.ignore_configured_endpoint_urls)
        self.assertEqual(config.proxies, {})
        self.assertEqual(config.account_id_endpoint_mode, 'disabled')
        self.assertEqual(guard.guard.cooking.name, 'foodtech-cooking-jobs-dev')

    def history_handoff(self):
        witness = PriorHistoryWitness(self.owner, *self.job_ids, self.scope.job_versions, self.scope.auth_epoch,
            10, 1, 1, str(uuid4()), 'a' * 64, 'b' * 64, 10, 'a' * 64, 'c' * 64)
        return RecipeReadHandoff(self.scope, self.authorization, self.role, self.arn, self.handoff.expires_at,
            'ASIA' + 'A' * 16, 'synthetic-secret-marker', 'synthetic-token-marker', history=witness)

    def test_v2_history_is_scope_bound_and_live_checked_before_and_after_sts(self):
        handoff = self.history_handoff()
        self.raw = handoff._encode_for_internal_tls()
        self.assertEqual(RecipeReadHandoff._decode_from_internal_tls(self.raw).history, handoff.history)
        guard = self.accept()
        self.assertEqual(guard.history_authorization.witness, handoff.history)
        self.assertEqual(self.conditions.transact_write_items.call_count, 2)
        for call in self.conditions.transact_write_items.call_args_list:
            self.assertEqual(len(call.kwargs['TransactItems']), 5)
        self.assertNotIn('synthetic-secret-marker', repr(handoff))
        with self.assertRaises(ValueError):
            jsonable_encoder(handoff)
        with self.assertRaises(TypeError):
            pickle.dumps(handoff)

    def test_v2_unknown_flags_wrong_owner_or_missing_history_are_rejected_before_keys(self):
        original = json.loads(self.history_handoff()._encode_for_internal_tls())
        import copy
        for mode in ['owner', 'flag', 'missing', 'mixed_schema']:
            with self.subTest(mode=mode):
                payload = copy.deepcopy(original)
                if mode == 'owner':
                    payload['history']['owner'] = 'another-owner'
                elif mode == 'flag':
                    payload['history']['immediately_previous_verified'] = True
                elif mode == 'missing':
                    del payload['history']
                else:
                    payload['schema'] = 'recipe-source-read-handoff-v1'
                with self.assertRaises(ValueError):
                    RecipeReadHandoff._decode_from_internal_tls(json.dumps(payload).encode())
        self.sessions.assert_not_called()

    def test_v2_revoked_history_prevents_any_scoped_key_use(self):
        self.raw = self.history_handoff()._encode_for_internal_tls()
        self.conditions.transact_write_items.side_effect = ClientError(
            {'Error': {'Code': 'TransactionCanceledException', 'Message': 'synthetic changed history'}}, 'TransactWriteItems')
        with self.assertRaises(RecipeHandoffDenied):
            self.accept()
        self.sessions.assert_not_called()
        self.resource.Table.assert_not_called()

    def test_installed_sdk_uses_explicit_keys_and_expected_regional_endpoints_without_requests(self):
        session = boto3.Session(region_name='ap-southeast-2', aws_access_key_id='ASIA' + 'A' * 16,
            aws_secret_access_key='synthetic', aws_session_token='synthetic')
        config = Config(ignore_configured_endpoint_urls=True, proxies={}, account_id_endpoint_mode='disabled')
        self.assertEqual(session.get_credentials().method, 'explicit')
        self.assertEqual(session.client('sts', config=config).meta.endpoint_url, 'https://sts.ap-southeast-2.amazonaws.com')
        self.assertEqual(session.resource('dynamodb', config=config).meta.client.meta.endpoint_url,
                         'https://dynamodb.ap-southeast-2.amazonaws.com')

    def test_wrong_scope_role_or_snapshot_is_rejected_before_key_use(self):
        for snapshot in [SimpleNamespace(state='SUCCEEDED', version=1, execution=self.snapshot.execution),
                         SimpleNamespace(state='PENDING', version=True, execution=self.snapshot.execution)]:
            with self.assertRaises(RecipeHandoffDenied):
                self.accept(snapshot=snapshot)
        with self.assertRaises(RecipeHandoffDenied):
            self.accept(expected_read_role_arn=self.role.replace('dev', 'demo'))
        self.sessions.assert_not_called()
        self.conditions.transact_write_items.assert_not_called()

    def test_wrong_actual_role_or_profile_credentials_never_get_a_source_resource(self):
        self.session.client.return_value.get_caller_identity.return_value['Arn'] = self.arn.replace('source-read', 'gateway')
        with self.assertRaises(RecipeHandoffDenied):
            self.accept()
        self.session.resource.assert_not_called()
        self.session.get_credentials.return_value.method = 'container-role'
        with self.assertRaises(RecipeHandoffDenied):
            self.accept()
        self.session.resource.assert_not_called()

    def test_endpoint_override_or_revocation_during_sts_never_reads_sources(self):
        self.session.client.return_value.meta.endpoint_url = 'https://external.invalid'
        with self.assertRaises(RecipeHandoffDenied):
            self.accept()
        self.session.client.return_value.get_caller_identity.assert_not_called()
        self.session.client.return_value.meta.endpoint_url = 'https://sts.ap-southeast-2.amazonaws.com'
        self.conditions.transact_write_items.side_effect = [None, ValueError('synthetic revoked')]
        with self.assertRaises(RecipeHandoffDenied):
            self.accept()
        self.session.resource.assert_not_called()

    def test_malformed_duplicate_extra_or_expired_wire_data_has_only_generic_errors(self):
        payload = json.loads(self.raw)
        expired = {**payload, 'expires_at': (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()}
        missing = {**payload, 'credentials': {'secret_access_key': 'synthetic-secret-marker'}}
        for raw in [b'x' * (MAX_BYTES + 1), b'{"schema":1,"schema":2}',
                    json.dumps({**payload, 'unexpected': 'synthetic-secret-marker'}).encode(),
                    json.dumps(expired).encode(), json.dumps(missing).encode()]:
            with self.subTest(size=len(raw)), self.assertRaisesRegex(ValueError, '^recipe read handoff unavailable$'):
                RecipeReadHandoff._decode_from_internal_tls(raw)
        with self.assertRaises(ValueError):
            RecipeReadHandoff(self.scope, replace(self.authorization, auth_epoch=4), self.role, self.arn,
                self.handoff.expires_at, 'ASIA' + 'A' * 16, 'synthetic', 'synthetic')


class BoundedHandoffRequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_body_errors_and_missing_mtls_never_echo_or_use_keys(self):
        sessions = MagicMock()
        app = FastAPI()
        app.state.mtls_listener = True
        @app.post('/fixture')
        async def receive(request: Request):
            await receive_internal_recipe_handoff(request, snapshot=None,
                expected_read_role_arn='', condition_client=None, session_factory=sessions)
            raise AssertionError('invalid body accepted')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://test') as client:
            for headers, raw in [({'Content-Type': CONTENT_TYPE}, b'synthetic-secret-marker'),
                                 ({'Content-Type': 'application/json'}, b'synthetic-secret-marker'),
                                 ({'Content-Type': CONTENT_TYPE, 'Content-Length': '9' * 5000}, b'secret'),
                                 ({'Content-Type': CONTENT_TYPE, 'Content-Length': '1'}, b'x' * (MAX_BYTES + 1))]:
                result = await client.post('/fixture', headers=headers, content=raw)
                self.assertEqual(result.status_code, 400)
                self.assertEqual(result.headers['Cache-Control'], 'no-store')
                self.assertNotIn('synthetic-secret-marker', result.text)
                self.assertNotIn('input', result.json())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            self.assertEqual((await client.post('/fixture', headers={'Content-Type': CONTENT_TYPE}, content=b'secret')).status_code, 401)
        sessions.assert_not_called()
