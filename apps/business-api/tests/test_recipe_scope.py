import time
import unittest
from types import SimpleNamespace
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

from foodtech_business_api.recipe_scope import RecipeScopeDenied, resolve_recipe_scope


class RecipeScopeTests(unittest.TestCase):
    def setUp(self):
        self.subject = 'customer-1'
        self.session = SimpleNamespace(surface='customer', subject=self.subject, expires=int(time.time()) + 300)
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

    def resolve(self):
        return resolve_recipe_scope(self.session, self.execution_id, lambda _: self.execution, self.jobs.get)

    def test_scope_is_derived_from_customer_owned_execution_and_jobs(self):
        scope = self.resolve()
        self.assertEqual(scope.source_partitions, ('JEYUK#jeyuk-current', 'JEYUK#jeyuk-previous'))
        self.assertEqual(scope.job_partitions, tuple(self.jobs))
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
            resolve_recipe_scope(self.session, self.execution_id, lambda _: self.execution, read)

    def test_session_expiring_during_reads_is_denied(self):
        self.session.expires = 200
        with patch('foodtech_business_api.recipe_scope.time.time', side_effect=[100, 200]):
            with self.assertRaises(RecipeScopeDenied):
                self.resolve()


if __name__ == '__main__':
    unittest.main()
