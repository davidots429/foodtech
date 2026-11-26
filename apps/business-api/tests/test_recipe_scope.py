import time
import unittest
from types import SimpleNamespace
from decimal import Decimal
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi import HTTPException
from starlette.requests import Request

from foodtech_business_api import auth
from foodtech_business_api.auth_store import MemoryStore, Session

from foodtech_business_api.recipe_scope import (RecipeScopeDenied, authenticated_recipe_scope,
                                                resolve_recipe_scope, revalidate_recipe_scope, verify_recipe_sources)


class RecipeScopeTests(unittest.TestCase):
    def setUp(self):
        self.subject = 'customer-1'
        self.session = SimpleNamespace(surface='customer', subject=self.subject, expires=int(time.time()) + 300,
                                       epoch=0, csrf='synthetic-csrf')
        self.execution_id, self.job_id, self.prior_id = (str(uuid4()) for _ in range(3))
        self.execution = SimpleNamespace(execution_id=self.execution_id, tenant_id=self.subject,
            feature='RECIPE_CORRECTION', job_id=self.job_id, prior_job_id=self.prior_id,
            job_version=2, prior_job_version=1, jeyuk_source_revision=4, prior_source_revision=3)
        self.jobs = {}
        for job_id, version, batch in ((self.job_id, 2, 'jeyuk-current'), (self.prior_id, 1, 'jeyuk-previous')):
            key = f'SCOPE#USER#{self.subject}#JOB#{job_id}'
            self.jobs[key] = {'owner_subject': self.subject, 'job_id': job_id, 'version': version,
                'menu': 'JEYUK', 'provenance': 'SYNTHETIC', 'source_binding': {'source_id': 'synthetic-rest',
                'device_id': 'mockprofile-110-a-001', 'batch_id': batch}}

    def snapshot_reader(self, _):
        return SimpleNamespace(execution=self.execution, state='PENDING', version=1)

    def resolve(self):
        return resolve_recipe_scope(self.session, self.execution_id, self.snapshot_reader, self.jobs.get)

    def test_opaque_subject_with_underscore_preserves_exact_owner_scope(self):
        self.session.subject = 'opaque_subject_1'
        self.execution.tenant_id = self.session.subject
        self.jobs = {pk.replace(self.subject, self.session.subject): {
            **row, 'owner_subject': self.session.subject} for pk, row in self.jobs.items()}
        scope = self.resolve()
        self.assertEqual(scope.owner, 'opaque_subject_1')
        self.assertTrue(all(pk.startswith('SCOPE#USER#opaque_subject_1#JOB#') for pk in scope.job_partitions))
        self.execution.tenant_id = 'opaque-subject-1'
        with self.assertRaises(RecipeScopeDenied):
            self.resolve()

    def test_scope_is_derived_from_customer_owned_execution_and_jobs(self):
        scope = self.resolve()
        self.assertEqual(scope.source_partitions, ('JEYUK#jeyuk-current', 'JEYUK#jeyuk-previous'))
        self.assertEqual(scope.job_partitions, tuple(self.jobs))
        self.assertEqual(scope.job_versions, (2, 1))
        self.assertEqual(scope.source_revisions, (4, 3))
        self.assertEqual((scope.execution_state, scope.execution_version), ('PENDING', 1))
        self.assertNotIn(self.subject, repr(scope))

    def test_other_owner_expired_or_admin_is_denied(self):
        for changes in ({'subject': 'other'}, {'surface': 'admin'}, {'expires': 1}):
            with self.subTest(changes=changes):
                saved = vars(self.session).copy()
                vars(self.session).update(changes)
                with self.assertRaises(RecipeScopeDenied): self.resolve()
                vars(self.session).update(saved)

    def test_dynamodb_decimal_expiry_is_supported_but_invalid_values_are_denied(self):
        self.session.expires = Decimal(int(time.time()) + 300)
        self.resolve()
        for invalid in (True, Decimal('NaN'), Decimal('Infinity'), Decimal('1.5')):
            self.session.expires = invalid
            with self.subTest(invalid=invalid), self.assertRaises(RecipeScopeDenied):
                self.resolve()

    def test_rejects_forged_execution_and_stale_or_shared_source(self):
        self.execution.tenant_id = 'other'
        with self.assertRaises(RecipeScopeDenied): self.resolve()
        self.execution.tenant_id = self.subject
        prior = self.jobs[next(reversed(self.jobs))]
        prior['version'] = 9
        with self.assertRaises(RecipeScopeDenied): self.resolve()
        prior['version'] = 1
        prior['source_binding']['batch_id'] = 'jeyuk-current'
        with self.assertRaises(RecipeScopeDenied): self.resolve()

    def test_changed_binding_between_reads_is_denied(self):
        calls = 0
        def read(key):
            nonlocal calls
            calls += 1
            if calls == 3:
                self.jobs[key]['source_binding']['batch_id'] = 'jeyuk-changed'
            return self.jobs.get(key)
        with self.assertRaises(RecipeScopeDenied):
            resolve_recipe_scope(self.session, self.execution_id, self.snapshot_reader, read)

    def test_session_expiring_during_reads_is_denied(self):
        self.session.expires = 200
        with patch('foodtech_business_api.recipe_scope.time.time', side_effect=[100, 200]):
            with self.assertRaises(RecipeScopeDenied):
                self.resolve()

    def test_execution_state_and_record_version_are_checked(self):
        for state in ('FAILED', 'SUCCEEDED', 'CANCELLED'):
            with self.subTest(state=state), self.assertRaises(RecipeScopeDenied):
                resolve_recipe_scope(self.session, self.execution_id,
                    lambda _: SimpleNamespace(execution=self.execution, state=state, version=1), self.jobs.get)
        results = iter((SimpleNamespace(execution=self.execution, state='PENDING', version=1),
                        SimpleNamespace(execution=self.execution, state='RESERVED', version=2)))
        with self.assertRaises(RecipeScopeDenied):
            resolve_recipe_scope(self.session, self.execution_id, lambda _: next(results), self.jobs.get)
        scope = resolve_recipe_scope(self.session, self.execution_id,
            lambda _: SimpleNamespace(execution=self.execution, state='RESERVED', version=2), self.jobs.get)
        self.assertEqual(scope.owner, self.subject)


class AuthenticatedRecipeScopeTests(unittest.IsolatedAsyncioTestCase):
    snapshot_reader = RecipeScopeTests.snapshot_reader

    def setUp(self):
        RecipeScopeTests.setUp(self)
        self.scope_store = MemoryStore()
        self.scope_store.principals[self.subject] = {'epoch': 0}
        self.store_patch = patch('foodtech_business_api.recipe_scope.get_store', return_value=self.scope_store)
        self.store_patch.start()
        self.addCleanup(self.store_patch.stop)

    async def test_session_is_rechecked_after_store_reads(self):
        with patch('foodtech_business_api.recipe_scope._session', new_callable=AsyncMock,
                   side_effect=[self.session, self.session]) as check:
            scope = await authenticated_recipe_scope(object(), self.execution_id, self.snapshot_reader, self.jobs.get)
        self.assertEqual(scope.owner, self.subject)
        self.assertEqual(check.await_count, 2)

    async def test_changed_or_revoked_session_prevents_returning_scope(self):
        changed = SimpleNamespace(**{**vars(self.session), 'epoch': 1})
        with patch('foodtech_business_api.recipe_scope._session', new_callable=AsyncMock,
                   side_effect=[self.session, changed]):
            with self.assertRaises(RecipeScopeDenied):
                await authenticated_recipe_scope(object(), self.execution_id, self.snapshot_reader, self.jobs.get)
        with patch('foodtech_business_api.recipe_scope._session', new_callable=AsyncMock,
                   side_effect=[self.session, PermissionError('revoked')]):
            with self.assertRaises(PermissionError):
                await authenticated_recipe_scope(object(), self.execution_id, self.snapshot_reader, self.jobs.get)

    async def test_real_session_boundary_rejects_suspension_during_job_reads(self):
        store = MemoryStore()
        store.principals[self.subject] = {'epoch': 0}
        store.save_session('synthetic-session', Session('customer', self.subject, None, 'csrf',
                                                       int(time.time()) + 300, 0))
        request = Request({'type': 'http', 'method': 'GET', 'path': '/',
                           'headers': [(b'cookie', f'{auth.SESSION_COOKIE}=synthetic-session'.encode())]})
        calls = 0
        def read(key):
            nonlocal calls
            calls += 1
            if calls == 4:
                store.suspend_user(self.subject)
            return self.jobs.get(key)
        with patch.object(auth, 'get_store', return_value=store), \
             patch('foodtech_business_api.recipe_scope.get_store', return_value=store), \
             patch.object(auth.Settings, 'from_env', return_value=SimpleNamespace()), \
             patch.object(auth, '_cognito_access', return_value=True):
            with self.assertRaises(HTTPException) as error:
                await authenticated_recipe_scope(request, self.execution_id, self.snapshot_reader, read)
        self.assertEqual(error.exception.status_code, 401)
        self.assertIsNone(store.read_session('synthetic-session'))

    async def test_missing_or_deleted_principal_cannot_receive_scope(self):
        with patch('foodtech_business_api.recipe_scope._session', new_callable=AsyncMock, return_value=self.session):
            del self.scope_store.principals[self.subject]
            with self.assertRaises(RecipeScopeDenied):
                await authenticated_recipe_scope(object(), self.execution_id, self.snapshot_reader, self.jobs.get)
            self.scope_store.principals[self.subject] = {'epoch': 0, 'deletion_lock_request': 'locked'}
            with self.assertRaises(RecipeScopeDenied):
                await authenticated_recipe_scope(object(), self.execution_id, self.snapshot_reader, self.jobs.get)

    async def test_principal_removed_during_reads_cannot_receive_scope(self):
        calls = 0
        def read(key):
            nonlocal calls
            calls += 1
            if calls == 4:
                del self.scope_store.principals[self.subject]
            return self.jobs.get(key)
        with patch('foodtech_business_api.recipe_scope._session', new_callable=AsyncMock, return_value=self.session):
            with self.assertRaises(RecipeScopeDenied):
                await authenticated_recipe_scope(object(), self.execution_id, self.snapshot_reader, read)

    async def test_invalid_session_epoch_is_rejected(self):
        self.session.epoch = True
        with patch('foodtech_business_api.recipe_scope._session', new_callable=AsyncMock, return_value=self.session):
            with self.assertRaises(RecipeScopeDenied):
                await authenticated_recipe_scope(object(), self.execution_id, self.snapshot_reader, self.jobs.get)

    async def test_session_expiring_during_final_principal_check_is_denied(self):
        self.session.expires = 200
        with patch('foodtech_business_api.recipe_scope._session', new_callable=AsyncMock, return_value=self.session), \
             patch('foodtech_business_api.recipe_scope.time.time', side_effect=[100, 150, 200]):
            with self.assertRaises(RecipeScopeDenied):
                await authenticated_recipe_scope(object(), self.execution_id, self.snapshot_reader, self.jobs.get)

    async def test_revalidation_rejects_changed_record_and_source_revision(self):
        with patch('foodtech_business_api.recipe_scope._session', new_callable=AsyncMock, return_value=self.session):
            original = await authenticated_recipe_scope(object(), self.execution_id, self.snapshot_reader, self.jobs.get)
            same = await revalidate_recipe_scope(object(), original, self.snapshot_reader, self.jobs.get)
            self.assertEqual(same, original)
            self.execution.jeyuk_source_revision += 1
            with self.assertRaises(RecipeScopeDenied):
                await revalidate_recipe_scope(object(), original, self.snapshot_reader, self.jobs.get)
            self.execution.jeyuk_source_revision -= 1
            with self.assertRaises(RecipeScopeDenied):
                await revalidate_recipe_scope(object(), original,
                    lambda _: SimpleNamespace(execution=self.execution, state='PENDING', version=2), self.jobs.get)

    async def test_source_guard_requires_valid_result_and_rechecks_scope_afterward(self):
        with patch('foodtech_business_api.recipe_scope._session', new_callable=AsyncMock, return_value=self.session):
            expected = await authenticated_recipe_scope(object(), self.execution_id, self.snapshot_reader, self.jobs.get)
            guard = SimpleNamespace(verify_scoped=lambda *_: True)
            self.assertIsNone(await verify_recipe_sources(object(), expected, self.snapshot_reader, self.jobs.get, guard))
            for result in (False, None, 1):
                with self.subTest(result=result), self.assertRaises(RecipeScopeDenied):
                    await verify_recipe_sources(object(), expected, self.snapshot_reader, self.jobs.get,
                                                SimpleNamespace(verify_scoped=lambda *_, value=result: value))
            def change_after_source(*_):
                self.execution.jeyuk_source_revision += 1
                return True
            with self.assertRaises(RecipeScopeDenied):
                await verify_recipe_sources(object(), expected, self.snapshot_reader, self.jobs.get,
                                            SimpleNamespace(verify_scoped=change_after_source))

    async def test_source_guard_errors_fail_closed(self):
        def broken(*_):
            raise RuntimeError('private implementation detail')
        with patch('foodtech_business_api.recipe_scope._session', new_callable=AsyncMock, return_value=self.session):
            expected = await authenticated_recipe_scope(object(), self.execution_id, self.snapshot_reader, self.jobs.get)
            with self.assertRaises(RecipeScopeDenied) as error:
                await verify_recipe_sources(object(), expected, self.snapshot_reader, self.jobs.get,
                                            SimpleNamespace(verify_scoped=broken))
            self.assertNotIn('private implementation detail', str(error.exception))


if __name__ == '__main__':
    unittest.main()
