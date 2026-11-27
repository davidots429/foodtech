"""Customer invocation rejects injected scope and rechecks account state after delivery."""
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from fastapi import HTTPException
from starlette.requests import Request
from foodtech_business_api.recipe_invocation import invoke_customer_recipe, _store_and_history
from foodtech_business_api.recipe_read_broker import RecipeReadBroker
from foodtech_business_api.source_read_authority import DynamoSourceReadAuthority
from foodtech_business_api.recipe_scope import RecipeScopeDenied
from foodtech_business_api.recipe_read_delivery import RecipeDeliveryUnavailable


def request(body=b'', csrf='csrf'):
    sent = False
    async def receive():
        nonlocal sent
        if sent: return {'type':'http.disconnect'}
        sent = True
        return {'type':'http.request', 'body':body, 'more_body':False}
    return Request({'type':'http','method':'POST','headers':[(b'x-csrf-token',csrf.encode())]}, receive)


class CustomerInvocationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.session = SimpleNamespace(subject='synthetic-customer', epoch=3, csrf='csrf')
        self.broker = RecipeReadBroker(account='123456789012', stage='dev',
            read_role_arn='arn:aws:iam::123456789012:role/foodtech-recipe-source-read-dev', sts=MagicMock(),
            source_authority=DynamoSourceReadAuthority(auth_table='foodtech-auth-sessions-dev',
                catalog_table='foodtech-source-access-dev', client=MagicMock()), cooking_resource=MagicMock())
        self.env = {'FOODTECH_RECIPE_INVOCATION_ENABLED':'true','FOODTECH_PRINCIPAL_PROVISIONING_ENABLED':'true'}
        self.identifier = str(uuid4())
        self.outcome = {'execution_id':self.identifier,'state':'DENIED','response_text':None,'error_code':'HISTORY_SCOPE_UNAVAILABLE'}

    async def test_missing_csrf_disabled_mode_or_browser_scope_stops_before_broker(self):
        for env, req, status in [(self.env,request(csrf='wrong'),403),
                ({**self.env,'FOODTECH_RECIPE_INVOCATION_ENABLED':'false'}, request(),503),
                ({**self.env,'FOODTECH_PRINCIPAL_PROVISIONING_ENABLED':'false'},request(),503),
                (self.env,request(b'{"tenant_id":"other","secret":"synthetic-marker"}'),400)]:
            with self.subTest(status=status), patch.dict(os.environ,env), \
                    patch('foodtech_business_api.recipe_invocation._session',new=AsyncMock(return_value=self.session)), \
                    patch('foodtech_business_api.recipe_invocation.configured_broker') as broker:
                with self.assertRaises(HTTPException) as error:
                    await invoke_customer_recipe(req,self.identifier)
                self.assertEqual(error.exception.status_code,status)
                self.assertNotIn('synthetic-marker',str(error.exception.detail))
                broker.assert_not_called()

    def test_reads_use_verified_broker_clients_without_default_aws_identity(self):
        with patch.dict(os.environ,self.env):
            store,history=_store_and_history(self.broker)
        self.assertIs(store.client,self.broker.source_authority.client)
        self.assertIs(history.client,store.client)
        self.assertIs(store.table,self.broker.cooking_resource.Table.return_value)
        self.assertTrue(store.require_principal)
        self.assertEqual(store.auth_table_name,'foodtech-auth-sessions-dev')

    async def test_only_server_lease_and_history_are_delivered_and_clients_closed(self):
        lease=object()
        with patch.dict(os.environ,self.env), \
                patch('foodtech_business_api.recipe_invocation._session',new=AsyncMock(return_value=self.session)), \
                patch('foodtech_business_api.recipe_invocation._require_principal_record',new=AsyncMock()) as principal, \
                patch('foodtech_business_api.recipe_invocation.configured_broker',return_value=self.broker), \
                patch('foodtech_business_api.recipe_invocation.issue_customer_recipe_reads',new=AsyncMock(return_value=lease)) as issue, \
                patch('foodtech_business_api.recipe_invocation.deliver_internal_recipe_read_lease',new=AsyncMock(return_value=self.outcome)) as deliver:
            response=await invoke_customer_recipe(request(),self.identifier)
        self.assertEqual(response.headers['cache-control'],'no-store')
        self.assertEqual(principal.await_count,2)
        self.assertIs(issue.call_args.kwargs['broker'],self.broker)
        self.assertEqual(issue.call_args.kwargs['history_authority'].auth,'foodtech-auth-sessions-dev')
        deliver.assert_awaited_once_with(lease)
        self.broker.sts.close.assert_called_once()
        self.broker.source_authority.client.close.assert_called_once()

    async def test_session_revoke_after_delivery_hides_outcome_without_reinvoking(self):
        changed=SimpleNamespace(subject=self.session.subject,epoch=4,csrf='csrf')
        with patch.dict(os.environ,self.env), \
                patch('foodtech_business_api.recipe_invocation._session',new=AsyncMock(side_effect=[self.session,changed])), \
                patch('foodtech_business_api.recipe_invocation._require_principal_record',new=AsyncMock()), \
                patch('foodtech_business_api.recipe_invocation.configured_broker',return_value=self.broker), \
                patch('foodtech_business_api.recipe_invocation.issue_customer_recipe_reads',new=AsyncMock(return_value=object())), \
                patch('foodtech_business_api.recipe_invocation.deliver_internal_recipe_read_lease',new=AsyncMock(return_value=self.outcome)) as deliver:
            with self.assertRaises(HTTPException) as error:
                await invoke_customer_recipe(request(),self.identifier)
            self.assertEqual(error.exception.status_code,401)
            self.assertEqual(deliver.await_count,1)

    async def test_missing_principal_stops_before_issuance(self):
        with patch.dict(os.environ,self.env), \
                patch('foodtech_business_api.recipe_invocation._session',new=AsyncMock(return_value=self.session)), \
                patch('foodtech_business_api.recipe_invocation._require_principal_record',new=AsyncMock(side_effect=RecipeScopeDenied('private-marker'))), \
                patch('foodtech_business_api.recipe_invocation.configured_broker') as broker:
            with self.assertRaises(HTTPException) as error:
                await invoke_customer_recipe(request(),self.identifier)
            self.assertEqual(error.exception.status_code,409)
            self.assertNotIn('private-marker',str(error.exception.detail))
            broker.assert_not_called()

    async def test_ambiguous_delivery_does_not_retry_or_expose_error(self):
        with patch.dict(os.environ,self.env), \
                patch('foodtech_business_api.recipe_invocation._session',new=AsyncMock(return_value=self.session)), \
                patch('foodtech_business_api.recipe_invocation._require_principal_record',new=AsyncMock()), \
                patch('foodtech_business_api.recipe_invocation.configured_broker',return_value=self.broker), \
                patch('foodtech_business_api.recipe_invocation.issue_customer_recipe_reads',new=AsyncMock(return_value=object())), \
                patch('foodtech_business_api.recipe_invocation.deliver_internal_recipe_read_lease',new=AsyncMock(side_effect=RecipeDeliveryUnavailable('private-marker'))) as deliver:
            with self.assertRaises(HTTPException) as error:
                await invoke_customer_recipe(request(),self.identifier)
            self.assertEqual(error.exception.status_code,503)
            self.assertNotIn('private-marker',str(error.exception.detail))
            self.assertEqual(deliver.await_count,1)

    async def test_history_binding_runs_before_gateway_observation_client_is_closed(self):
        from foodtech_business_api.gateway_recipe_scope import issue_customer_recipe_reads
        session=self.session
        lease, bound=object(),object()
        client=MagicMock()
        authority=object()
        async def bind(req, actual, **kwargs):
            self.assertIs(actual,lease)
            self.assertIs(kwargs['authority'],authority)
            client.close.assert_not_called()
            return bound
        with patch('foodtech_business_api.gateway_recipe_scope._session',new=AsyncMock(return_value=session)), \
                patch('foodtech_business_api.ai._gateway_settings',return_value=('https://localhost','ca','cert','key')), \
                patch('ssl.create_default_context',return_value=MagicMock()), \
                patch('foodtech_business_api.gateway_recipe_scope.httpx.Client',return_value=client), \
                patch('foodtech_business_api.recipe_read_broker.issue_authenticated_reads',new=AsyncMock(return_value=lease)), \
                patch('foodtech_business_api.recipe_read_broker.bind_authenticated_recipe_history',new=bind):
            result=await issue_customer_recipe_reads(request(),self.identifier,job_reader=object(),broker=self.broker,
                                                      history_authority=authority)
        self.assertIs(result,bound)
        client.close.assert_called_once()
