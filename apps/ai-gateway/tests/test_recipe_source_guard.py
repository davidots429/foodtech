import unittest
from dataclasses import replace
from decimal import Decimal
from uuid import uuid4

from foodtech_ai_gateway.policy import Execution, Feature
from foodtech_ai_gateway.recipe_source_guard import DynamoRecipeSourceGuard


class FakeTable:
    def __init__(self): self.items = {}
    def get_item(self, *, Key, ConsistentRead):
        assert ConsistentRead is True
        return {'Item': self.items.get((Key['pk'], Key['sk']))}


class FakeResource:
    def __init__(self): self.tables = {'foodtech-cooking-jobs-dev': FakeTable(), 'foodtech-data-flow-dev': FakeTable()}
    def Table(self, name): return self.tables[name]


class RecipeSourceGuardTests(unittest.TestCase):
    def setUp(self):
        self.resource = FakeResource()
        self.guard = DynamoRecipeSourceGuard('foodtech-cooking-jobs-dev', 'foodtech-data-flow-dev', resource=self.resource)
        self.current, self.prior = str(uuid4()), str(uuid4())
        self.execution = Execution('exec', 'tenant-a', Feature.RECIPE_CORRECTION, 'model', None,
            'SYNTHETIC', Decimal('0'), job_id=self.current, job_version=7,
            prior_job_id=self.prior, prior_job_version=3, jeyuk_source_revision=30, prior_source_revision=29)
        for job, version, batch, revision in ((self.current, 7, 'batch-current', 30), (self.prior, 3, 'batch-prior', 29)):
            self.resource.tables['foodtech-cooking-jobs-dev'].items[(f'SCOPE#USER#tenant-a#JOB#{job}', 'META')] = {
                'owner_subject': 'tenant-a', 'job_id': job, 'version': Decimal(version), 'menu': 'JEYUK',
                'provenance': 'SYNTHETIC', 'source_binding': {'source_id': 'synthetic-rest',
                'device_id': 'mockprofile-110-a-001', 'batch_id': batch}}
            self.resource.tables['foodtech-data-flow-dev'].items[(f'JEYUK#{batch}', 'SOURCE#REVISION')] = {'revision': Decimal(revision)}

    def test_exact_owner_job_and_source_versions_are_checked(self):
        self.assertTrue(self.guard.verify(self.execution))
        self.assertFalse(self.guard.verify(replace(self.execution, tenant_id='tenant-b')))
        self.assertFalse(self.guard.verify(replace(self.execution, prior_source_revision=28)))
        self.assertFalse(self.guard.verify(replace(self.execution, prior_job_version=4)))
        self.assertFalse(self.guard.verify(replace(self.execution, prior_job_id=None, prior_job_version=None)))
        self.resource.tables['foodtech-data-flow-dev'].items[('JEYUK#batch-current', 'SOURCE#REVISION')]['revision'] = Decimal(31)
        self.assertFalse(self.guard.verify(self.execution))

    def test_rejects_wrong_provenance_binding_and_shared_batch(self):
        key = (f'SCOPE#USER#tenant-a#JOB#{self.prior}', 'META')
        row = self.resource.tables['foodtech-cooking-jobs-dev'].items[key]
        row['provenance'] = 'REAL'
        self.assertFalse(self.guard.verify(self.execution))
        row['provenance'] = 'SYNTHETIC'
        row['source_binding']['batch_id'] = 'batch-current'
        self.assertFalse(self.guard.verify(self.execution))
        row['source_binding']['batch_id'] = 'batch-prior'
        row['source_binding']['device_id'] = 'other'
        self.assertFalse(self.guard.verify(self.execution))

    def test_rejects_unrelated_tables(self):
        with self.assertRaises(ValueError):
            DynamoRecipeSourceGuard('foodtech-credential-vault-dev', 'foodtech-data-flow-dev', resource=self.resource)
        with self.assertRaises(ValueError):
            DynamoRecipeSourceGuard('foodtech-cooking-jobs-dev', 'foodtech-identity-vault-dev', resource=self.resource)
