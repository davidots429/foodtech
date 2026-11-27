from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
import unittest
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from fastapi import HTTPException
from starlette.requests import Request

from foodtech_ai_gateway.policy import Execution, ExecutionSnapshot, Feature
from foodtech_business_api.auth_store import Session
from foodtech_business_api.combi_read_scope import resolve_combi_read_scope, authenticated_combi_read_scope
from foodtech_business_api.recipe_scope import RecipeScopeDenied
from foodtech_contracts.combi_read_scope import CombiReadScope
import test_combi_prior_history as fixtures


class CombiReadScopeTests(unittest.TestCase):
    def setUp(self):
        self.data = fixtures.CombiHistoryTests()
        self.data.setUp()
        self.binding = self.data.binding
        self.session = Session('customer', self.data.owner, None, 'synthetic-csrf', fixtures.NOW+500, 2)
        self.execution = Execution(str(uuid4()), self.session.subject, Feature.RECIPE_CORRECTION, 'llama3.1:8b',
            None, 'SYNTHETIC', Decimal(0), job_id=self.binding.current_job_id, job_version=4,
            prior_job_id=self.binding.prior_job_id, prior_job_version=4, combi_binding=self.binding)
        self.snapshot = ExecutionSnapshot(self.execution, 'PENDING', 1)
        self.reader = Mock(side_effect=lambda _: deepcopy(self.snapshot))

    def resolve(self):
        return resolve_combi_read_scope(self.session, self.execution.execution_id, execution_reader=self.reader,
            history_authority=self.data.authority, clock=lambda: fixtures.NOW)

    def test_execution_and_real_full_history_derive_exact_four_partitions(self):
        scope = self.resolve()
        self.assertEqual(len(set(scope.read_partitions)), 4)
        self.assertEqual(scope.meal_partitions, tuple(f'SCOPE#USER#{self.session.subject}#COMBI_MEAL#{meal}'
            for meal in (self.binding.current_meal_id, self.binding.prior_meal_id)))
        self.assertEqual(scope.source_partitions, tuple(pk+'#SOURCE' for pk in scope.meal_partitions))
        self.assertTrue(scope.matches_snapshot(self.snapshot))
        self.assertEqual(self.reader.call_count, 3)
        self.assertNotIn(self.session.subject, repr(scope))
        self.assertNotIn(self.execution.execution_id, repr(scope))
        self.assertFalse(any('#COMBI_MEALS' in pk or '#LIVE' in pk or '#JOB#' in pk for pk in scope.read_partitions))

    def test_invalid_session_or_execution_is_denied_before_any_history_read(self):
        for changes in ({'surface': 'admin'}, {'subject': 'foreign'}, {'epoch': True}, {'epoch': 3},
                        {'expires': fixtures.NOW}, {'expires': Decimal('1E100000')}):
            with self.subTest(changes=changes):
                before = vars(self.session).copy()
                vars(self.session).update(changes)
                self.data.client.calls.clear()
                with self.assertRaises(RecipeScopeDenied): self.resolve()
                self.assertEqual(self.data.client.calls, [])
                vars(self.session).update(before)
        for snapshot in (replace(self.snapshot, state='SUCCEEDED'), replace(self.snapshot, version=True),
                         replace(self.snapshot, execution=replace(self.execution, tenant_id='foreign'))):
            with self.subTest(snapshot=snapshot):
                self.reader.side_effect = lambda _: snapshot
                self.data.client.calls.clear()
                with self.assertRaises(RecipeScopeDenied): self.resolve()
                self.assertEqual(self.data.client.calls, [])

    def test_changing_execution_or_history_cannot_be_rebound_to_new_scope(self):
        for change in (replace(self.snapshot, state='RESERVED'), replace(self.snapshot, version=2)):
            with self.subTest(change=change):
                self.reader.side_effect = [self.snapshot, change]
                with self.assertRaises(RecipeScopeDenied): self.resolve()
        self.reader.side_effect = lambda _: self.snapshot
        self.data.client.control_reads = 0
        self.data.client.on_second_control = lambda: self.data.history.update(revision=13)
        with self.assertRaises(RecipeScopeDenied): self.resolve()

    def test_mutated_shared_snapshot_does_not_move_the_first_comparison(self):
        calls = 0
        def reader(_):
            nonlocal calls
            calls += 1
            if calls == 2: object.__setattr__(self.snapshot, 'version', 2)
            return self.snapshot
        self.reader.side_effect = reader
        with self.assertRaises(RecipeScopeDenied): self.resolve()

    def test_typed_scope_rejects_foreign_history_and_invalid_state(self):
        scope = self.resolve()
        for changes in ({'owner': 'foreign'}, {'binding': replace(self.binding, auth_epoch=3)},
                        {'execution_id': 'not-uuid'}, {'execution_version': True}, {'execution_state': 'SUCCEEDED'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError): replace(scope, **changes)
        with self.assertRaises(TypeError): CombiReadScope(**{**vars(scope), 'read_partitions': ('foreign',)})


class AuthenticatedCombiReadScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_csrf_and_fresh_account_then_shared_session_change_are_checked(self):
        session = Session('customer', 'customer_1', None, 'synthetic-csrf', 9999999999, 2)
        scope = object()
        async def resolve(*args, **kwargs):
            session.subject = 'changed_customer'
            return scope
        request = Request({'type': 'http', 'headers': [(b'x-csrf-token', b'synthetic-csrf')]})
        with patch('foodtech_business_api.combi_read_scope._session', AsyncMock(return_value=session)), \
             patch('foodtech_business_api.combi_read_scope._require_principal_record', AsyncMock()) as principal, \
             patch('foodtech_business_api.combi_read_scope.asyncio.to_thread', side_effect=resolve) as worker:
            with self.assertRaises(RecipeScopeDenied):
                await authenticated_combi_read_scope(request, str(uuid4()), execution_reader=Mock(), history_authority=Mock())
            self.assertEqual(principal.await_count, 2)
            self.assertEqual(worker.call_args.args[1].subject, 'customer_1')

    async def test_missing_csrf_never_reads_execution_or_history(self):
        session = Session('customer', 'customer_1', None, 'synthetic-csrf', 9999999999, 2)
        with patch('foodtech_business_api.combi_read_scope._session', AsyncMock(return_value=session)), \
             patch('foodtech_business_api.combi_read_scope.asyncio.to_thread', AsyncMock()) as worker:
            with self.assertRaises(HTTPException):
                await authenticated_combi_read_scope(Request({'type': 'http', 'headers': []}), str(uuid4()),
                    execution_reader=Mock(), history_authority=Mock())
            worker.assert_not_called()
