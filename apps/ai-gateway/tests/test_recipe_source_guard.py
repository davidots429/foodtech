import unittest
import json
from dataclasses import replace
from decimal import Decimal
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from foodtech_ai_gateway.policy import Execution, Feature
from foodtech_ai_gateway.recipe_source_guard import DynamoRecipeSourceGuard
from foodtech_contracts.jeyuk_receipt import JeyukCompletionTracker


class FakeTable:
    def __init__(self, name):
        self.name = name; self.items = {}; self.meta = self; self.client = self
    def get_item(self, *, Key, ConsistentRead):
        assert ConsistentRead is True
        return {'Item': self.items.get((Key['pk'], Key['sk']))}
    def get_paginator(self, name):
        assert name == 'query'
        return self
    def paginate(self, **kwargs):
        assert kwargs['ConsistentRead'] is True
        left, right = kwargs['KeyConditionExpression'].get_expression()['values']
        pk = left.get_expression()['values'][1]
        prefix = right.get_expression()['values'][1]
        rows = [row for (item_pk, sk), row in sorted(self.items.items()) if item_pk == pk and sk.startswith(prefix)]
        return [{'Items': rows[:20]}, {'Items': rows[20:]}]


class FakeResource:
    def __init__(self): self.tables = {name: FakeTable(name) for name in ('foodtech-cooking-jobs-dev', 'foodtech-data-flow-dev')}
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
            started = datetime(2026, 1, 1, tzinfo=timezone.utc) - timedelta(seconds=60 if job == self.prior else 0)
            self.resource.tables['foodtech-cooking-jobs-dev'].items[(f'SCOPE#USER#tenant-a#JOB#{job}', 'META')] = {
                'owner_subject': 'tenant-a', 'job_id': job, 'version': Decimal(version), 'menu': 'JEYUK',
                'actual_placed_at': started.isoformat(), 'actual_lifted_at': (started + timedelta(seconds=revision)).isoformat(),
                'actual_cooking_seconds': Decimal(revision),
                'provenance': 'SYNTHETIC', 'source_binding': {'source_id': 'synthetic-rest',
                'device_id': 'mockprofile-110-a-001', 'batch_id': batch}}
            self.resource.tables['foodtech-cooking-jobs-dev'].items[(f'JEYUK#{batch}', 'BINDING')] = {
                'job_pk': f'SCOPE#USER#tenant-a#JOB#{job}', 'job_id': job}
            self.resource.tables['foodtech-data-flow-dev'].items[(f'JEYUK#{batch}', 'SOURCE#REVISION')] = {'revision': Decimal(revision)}
            tracker = JeyukCompletionTracker(0)
            for second in range(1, revision + 1):
                raw = json.dumps({'fixtureId': batch, 'provenance': 'SYNTHETIC', 'profileId': 'mockprofile-110',
                    'instanceId': 'mockprofile-110-a-001', 'profile': 'SMALL', 'actualLoadKg': 1.8,
                    'second': second, 'sampleIntervalSeconds': 1, 'surfaceCelsius': 190,
                    'actualRpm': 30, 'targetRpm': 30, 'motorLoadPercent': 40,
                    'observedAt': (started + timedelta(seconds=second)).isoformat()})
                tracker.record(raw)
                sk = f'RAW#{second:06d}'
                self.resource.tables['foodtech-data-flow-dev'].items[(f'JEYUK#{batch}', sk)] = {
                    'pk': f'JEYUK#{batch}', 'sk': sk, 'raw': raw, 'provenance': 'SYNTHETIC'}
            receipt = tracker.finish(cursor=revision, complete=True, explicit=True)
            self.resource.tables['foodtech-data-flow-dev'].items[(f'JEYUK#{batch}', 'COLLECTION#RECEIPT#1')] = receipt

    def test_exact_owner_job_and_source_versions_are_checked(self):
        self.assertTrue(self.guard.verify(self.execution))
        self.assertFalse(self.guard.verify(replace(self.execution, tenant_id='tenant-b')))
        self.assertFalse(self.guard.verify(replace(self.execution, prior_source_revision=28)))
        self.assertFalse(self.guard.verify(replace(self.execution, prior_job_version=4)))
        self.assertFalse(self.guard.verify(replace(self.execution, prior_job_id=None, prior_job_version=None)))
        self.resource.tables['foodtech-data-flow-dev'].items[('JEYUK#batch-current', 'SOURCE#REVISION')]['revision'] = Decimal(31)
        self.assertFalse(self.guard.verify(self.execution))

    def test_scoped_guard_rejects_changed_binding_before_foreign_source_read(self):
        jobs = (f'SCOPE#USER#tenant-a#JOB#{self.current}', f'SCOPE#USER#tenant-a#JOB#{self.prior}')
        sources = ('JEYUK#batch-current', 'JEYUK#batch-prior')
        self.assertTrue(self.guard.verify_scoped(self.execution, jobs, sources))
        self.assertFalse(self.guard.verify_scoped(self.execution, (jobs[1], jobs[0]), sources))
        cooking = self.resource.tables['foodtech-cooking-jobs-dev']
        data = self.resource.tables['foodtech-data-flow-dev']
        cooking.items[(jobs[0], 'META')]['source_binding']['batch_id'] = 'foreign-batch'
        accessed = []
        original_cooking, original_data = cooking.get_item, data.get_item
        def read_cooking(*, Key, ConsistentRead):
            accessed.append(Key['pk'])
            return original_cooking(Key=Key, ConsistentRead=ConsistentRead)
        def read_data(*, Key, ConsistentRead):
            accessed.append(Key['pk'])
            return original_data(Key=Key, ConsistentRead=ConsistentRead)
        cooking.get_item, data.get_item = read_cooking, read_data
        def reject_query(_):
            raise AssertionError('unexpected source query')
        data.get_paginator = reject_query
        self.assertFalse(self.guard.verify_scoped(self.execution, jobs, sources))
        self.assertNotIn('JEYUK#foreign-batch', accessed)

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

    def test_rejects_missing_or_foreign_reverse_binding(self):
        key = ('JEYUK#batch-prior', 'BINDING')
        binding = self.resource.tables['foodtech-cooking-jobs-dev'].items.pop(key)
        self.assertFalse(self.guard.verify(self.execution))
        binding['job_pk'] = f'SCOPE#USER#other#JOB#{self.prior}'
        self.resource.tables['foodtech-cooking-jobs-dev'].items[key] = binding
        self.assertFalse(self.guard.verify(self.execution))

    def test_rejects_record_change_during_multi_item_read(self):
        table = self.resource.tables['foodtech-cooking-jobs-dev']
        original = table.get_item
        current_key = f'SCOPE#USER#tenant-a#JOB#{self.current}'
        reads = 0
        def changing(*, Key, ConsistentRead):
            nonlocal reads
            if Key['pk'] == current_key and Key['sk'] == 'META':
                reads += 1
                if reads == 2:
                    table.items[(current_key, 'META')]['status'] = 'CHANGED_WITHOUT_VERSION'
            return original(Key=Key, ConsistentRead=ConsistentRead)
        table.get_item = changing
        self.assertFalse(self.guard.verify(self.execution))

    def test_rejects_mutated_raw_or_missing_terminal_receipt(self):
        table = self.resource.tables['foodtech-data-flow-dev']
        key = ('JEYUK#batch-prior', 'RAW#000002')
        table.items[key]['raw'] += ' '
        self.assertFalse(self.guard.verify(self.execution))
        table.items[key]['raw'] = table.items[key]['raw'].rstrip()
        del table.items[('JEYUK#batch-prior', 'COLLECTION#RECEIPT#1')]
        self.assertFalse(self.guard.verify(self.execution))

    def test_rejects_extra_or_missing_raw_even_when_revision_does_not_change(self):
        table = self.resource.tables['foodtech-data-flow-dev']
        key = ('JEYUK#batch-current', 'RAW#000001')
        row = table.items.pop(key)
        self.assertFalse(self.guard.verify(self.execution))
        table.items[key] = row
        extra_key = ('JEYUK#batch-current', 'RAW#000031')
        table.items[extra_key] = {**row, 'sk': extra_key[1]}
        self.assertFalse(self.guard.verify(self.execution))

    def test_rejects_incomplete_or_wrong_job_timeline(self):
        key = (f'SCOPE#USER#tenant-a#JOB#{self.current}', 'META')
        job = self.resource.tables['foodtech-cooking-jobs-dev'].items[key]
        job['actual_cooking_seconds'] = Decimal(29)
        self.assertFalse(self.guard.verify(self.execution))
        job['actual_cooking_seconds'] = Decimal(30)
        job['actual_lifted_at'] = '2026-01-01T00:00:31+00:00'
        self.assertFalse(self.guard.verify(self.execution))
        job['actual_lifted_at'] = '2026-01-01T00:00:30+00:00'
        job['actual_placed_at'] = '2026-01-01T00:00:00'
        self.assertFalse(self.guard.verify(self.execution))

    def test_rejects_inconsistent_sample_clock_even_with_matching_receipt(self):
        table = self.resource.tables['foodtech-data-flow-dev']
        key = ('JEYUK#batch-prior', 'RAW#000002')
        raw = json.loads(table.items[key]['raw'])
        raw['observedAt'] = '2025-12-31T23:59:08+00:00'
        table.items[key]['raw'] = json.dumps(raw)
        tracker = JeyukCompletionTracker(0)
        for second in range(1, 30):
            tracker.record(table.items[('JEYUK#batch-prior', f'RAW#{second:06d}')]['raw'])
        table.items[('JEYUK#batch-prior', 'COLLECTION#RECEIPT#1')] = tracker.finish(cursor=29, complete=True, explicit=True)
        self.assertFalse(self.guard.verify(self.execution))

    def test_rejects_overlapping_prior_job(self):
        key = (f'SCOPE#USER#tenant-a#JOB#{self.current}', 'META')
        job = self.resource.tables['foodtech-cooking-jobs-dev'].items[key]
        # Shift both current endpoints and all its source samples consistently;
        # each job remains internally valid but the two jobs overlap.
        job['actual_placed_at'] = '2025-12-31T23:59:20+00:00'
        job['actual_lifted_at'] = '2025-12-31T23:59:50+00:00'
        table = self.resource.tables['foodtech-data-flow-dev']
        tracker = JeyukCompletionTracker(0)
        for second in range(1, 31):
            row = table.items[('JEYUK#batch-current', f'RAW#{second:06d}')]
            sample = json.loads(row['raw'])
            sample['observedAt'] = (datetime(2025, 12, 31, 23, 59, 20, tzinfo=timezone.utc) + timedelta(seconds=second)).isoformat()
            row['raw'] = json.dumps(sample)
            tracker.record(row['raw'])
        table.items[('JEYUK#batch-current', 'COLLECTION#RECEIPT#1')] = tracker.finish(cursor=30, complete=True, explicit=True)
        self.assertFalse(self.guard.verify(self.execution))

    def test_rejects_unrelated_tables(self):
        with self.assertRaises(ValueError):
            DynamoRecipeSourceGuard('foodtech-credential-vault-dev', 'foodtech-data-flow-dev', resource=self.resource)
        with self.assertRaises(ValueError):
            DynamoRecipeSourceGuard('foodtech-cooking-jobs-dev', 'foodtech-identity-vault-dev', resource=self.resource)
        with self.assertRaises(ValueError):
            DynamoRecipeSourceGuard('foodtech-cooking-jobs-dev', 'foodtech-data-flow-dev', resource=None)
