"""Actual local mTLS hop; all keys and AWS responses are synthetic."""
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import socket
import ssl
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

import boto3
from botocore.config import Config
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import httpx
import uvicorn

from foodtech_ai_gateway.recipe_read_handoff import receive_internal_recipe_handoff
from foodtech_business_api.recipe_read_broker import RecipeReadLease
from foodtech_business_api.recipe_read_delivery import deliver_internal_recipe_read_lease
from foodtech_contracts.recipe_read_handoff import CONTENT_TYPE
from foodtech_contracts.recipe_read_scope import RecipeReadScope
from foodtech_contracts.source_read_authorization import SourceReadAuthorization
import test_mtls as tls_fixture


class RecipeHandoffMutualTlsTests(unittest.IsolatedAsyncioTestCase):
    async def test_scoped_lease_crosses_real_tls_without_credentials_in_outcome_or_error(self):
        now = datetime.now(timezone.utc)
        owner, execution_id = 'opaque_subject', str(uuid4())
        jobs = (str(uuid4()), str(uuid4()))
        scope = RecipeReadScope(owner, execution_id, tuple(f'SCOPE#USER#{owner}#JOB#{job}' for job in jobs),
            ('JEYUK#jeyuk-current', 'JEYUK#jeyuk-prior'), (2, 1), (12, 11), 'PENDING', 1, 3)
        grant = SourceReadAuthorization('foodtech-auth-sessions-dev', 'foodtech-source-access-dev', owner,
            execution_id, 3, scope.source_partitions, (str(uuid4()), str(uuid4())), (1, 1),
            (int(now.timestamp()) + 900,) * 2)
        role = 'arn:aws:iam::123456789012:role/foodtech-recipe-source-read-dev'
        arn = 'arn:aws:sts::123456789012:assumed-role/foodtech-recipe-source-read-dev/recipe-' + 'a' * 32
        source_session = boto3.Session(region_name='ap-southeast-2', aws_access_key_id='ASIA' + 'A' * 16,
            aws_secret_access_key='synthetic-secret-marker', aws_session_token='synthetic-token-marker')
        lease = RecipeReadLease(scope, now + timedelta(seconds=899), source_session, Config(),
                                read_role_arn=role, assumed_role_arn=arn)
        lease._bind_authorization(grant)
        snapshot = SimpleNamespace(state='PENDING', version=1, execution=SimpleNamespace(
            execution_id=execution_id, tenant_id=owner, feature='RECIPE_CORRECTION', job_id=jobs[0],
            prior_job_id=jobs[1], job_version=2, prior_job_version=1, jeyuk_source_revision=12, prior_source_revision=11))
        session = MagicMock()
        session.get_credentials.return_value = SimpleNamespace(method='explicit')
        session.client.return_value.meta.endpoint_url = 'https://sts.ap-southeast-2.amazonaws.com'
        session.client.return_value.get_caller_identity.return_value = {'Account': '123456789012', 'Arn': arn}
        session.resource.return_value.meta.client.meta.endpoint_url = 'https://dynamodb.ap-southeast-2.amazonaws.com'
        session.resource.return_value.Table.side_effect = lambda name: SimpleNamespace(name=name)
        sessions, conditions = MagicMock(return_value=session), MagicMock()
        app = FastAPI()
        app.state.mtls_listener = True
        accepted = []

        @app.post('/internal/ai/recipe-executions/{identifier}/invoke')
        async def receive(request: Request, identifier: str):
            guard = await receive_internal_recipe_handoff(request, snapshot=snapshot,
                expected_read_role_arn=role, condition_client=conditions, session_factory=sessions)
            accepted.append(guard)
            # Fixture only: authoritative recipe input is not fabricated here.
            return JSONResponse({'execution_id': identifier, 'state': 'DENIED', 'response_text': None,
                'error_code': 'RECOMMENDATION_EVIDENCE_UNVERIFIED'}, headers={'Cache-Control': 'no-store'})

        with tempfile.TemporaryDirectory() as directory:
            ca, cert, key, client_cert, client_key = tls_fixture._certificates(Path(directory))
            listener = socket.socket()
            listener.bind(('127.0.0.1', 0))
            listener.listen(16)
            port = listener.getsockname()[1]
            server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, ssl_certfile=cert,
                ssl_keyfile=key, ssl_ca_certs=ca, ssl_cert_reqs=ssl.CERT_REQUIRED, proxy_headers=False,
                access_log=False, log_level='error', lifespan='off'))
            task = asyncio.create_task(server.serve(sockets=[listener]))
            try:
                for _ in range(500):
                    if server.started:
                        break
                    await asyncio.sleep(0.01)
                self.assertTrue(server.started)
                url = f'https://127.0.0.1:{port}'
                with patch('foodtech_business_api.ai._gateway_settings', return_value=(url, ca, client_cert, client_key)), \
                     self.assertLogs('httpx', level='DEBUG') as logs:
                    result = await deliver_internal_recipe_read_lease(lease)
                    self.assertEqual(result['state'], 'DENIED')
                    self.assertIsNone(result['response_text'])
                    context = ssl.create_default_context(cafile=ca)
                    context.load_cert_chain(client_cert, client_key)
                    async with httpx.AsyncClient(verify=context, trust_env=False, timeout=5) as client:
                        bad = await client.post(f'{url}/internal/ai/recipe-executions/{execution_id}/invoke',
                            headers={'Content-Type': CONTENT_TYPE}, content=b'synthetic-secret-marker')
                    self.assertEqual(bad.status_code, 400)
                    self.assertNotIn('synthetic-secret-marker', bad.text)
                self.assertNotIn('synthetic-secret-marker', '\n'.join(logs.output))
                self.assertNotIn('synthetic-token-marker', '\n'.join(logs.output))
                self.assertEqual(len(accepted), 1)
                self.assertEqual(accepted[0].execution.execution_id, execution_id)
                self.assertEqual(sessions.call_args.kwargs['aws_secret_access_key'], 'synthetic-secret-marker')
                self.assertEqual(conditions.transact_write_items.call_count, 2)
            finally:
                server.should_exit = True
                await asyncio.wait_for(task, timeout=10)
                listener.close()
