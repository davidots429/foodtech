from copy import deepcopy
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from foodtech_business_api.combi_owned_observations import (
    CombiObservationUnavailable, read_owned_combi_observation)


class OwnedCombiObservationTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 28, tzinfo=timezone.utc)
        self.session = SimpleNamespace(subject='customer_1', surface='customer', epoch=2,
                                       expires=int(self.now.timestamp()) + 60)
        self.meal_id, self.digest = str(uuid4()), 'a' * 64
        self.pk = f'SCOPE#USER#customer_1#COMBI_MEAL#{self.meal_id}'
        self.rows = [
            {'pk': self.pk, 'sk': 'META', 'meal_batch_id': self.meal_id, 'version': 3,
             'provenance': 'SYNTHETIC', 'source': {'raw_sha256': self.digest, 'record_count': 3}},
            {'pk': self.pk, 'sk': 'RAW#0000', 'first_sequence': 1, 'records': ['one', 'two', 'three']},
        ]
        self.reader = Mock(side_effect=lambda pk: deepcopy(self.rows))
        self.chunk = self.rows.pop()
        self.chunk['pk'] = self.pk + '#SOURCE'
        self.sources = Mock(side_effect=lambda pk, i: deepcopy(self.chunk))
        self.principal = {'pk': 'USER#customer_1', 'epoch': 2}
        self.principals = Mock(side_effect=lambda owner: deepcopy(self.principal))
        self.clock = Mock(return_value=self.now)
        self.observer_patch = patch('foodtech_business_api.combi_owned_observations.observe_combi_batch')
        self.observer = self.observer_patch.start()
        self.addCleanup(self.observer_patch.stop)

    def read(self, **changes):
        args = dict(meal_id=self.meal_id, expected_version=3, source_digest=self.digest, menu='RICE',
                    meal_reader=self.reader, source_reader=self.sources,
                    principal_reader=self.principals, clock=self.clock)
        args.update(changes)
        return read_owned_combi_observation(self.session, **args)

    def denied(self, **changes):
        with self.assertRaises(CombiObservationUnavailable):
            self.read(**changes)
        self.observer.assert_not_called()

    def test_uses_exact_owned_key_and_only_persisted_chunks(self):
        result = self.read()
        self.assertIs(result, self.observer.return_value)
        self.assertEqual(self.reader.call_count, 2)
        self.assertTrue(all(c.args == (self.pk,) for c in self.reader.call_args_list))
        self.assertEqual(self.principals.call_count, 2)
        self.assertEqual(self.sources.call_count, 2)
        self.assertTrue(all(c.args == (self.pk, 0) for c in self.sources.call_args_list))
        self.observer.assert_called_once_with({r['sk']: r for r in self.rows},
                                             ['one', 'two', 'three'], 'RICE', now=self.now)

    def test_invalid_caller_scope_never_reads_meal(self):
        for field, value in [('subject', 'foreign#customer'), ('surface', 'admin'), ('epoch', True),
                             ('epoch', Decimal('1E100000')), ('expires', True)]:
            with self.subTest(field=field, value=value):
                old = getattr(self.session, field)
                setattr(self.session, field, value)
                self.denied()
                setattr(self.session, field, old)
        self.reader.assert_not_called()

    def test_invalid_reference_or_expected_revision_never_reads(self):
        for change in ({'meal_id': str(uuid4()).upper()}, {'meal_id': 'x'}, {'expected_version': True},
                       {'expected_version': 0}, {'source_digest': 'not-a-hash'}, {'menu': 'JEYUK'}):
            with self.subTest(change=change):
                self.denied(**change)
        self.reader.assert_not_called()

    def test_missing_suspended_locked_foreign_or_wrong_epoch_account_denied(self):
        original = self.principal
        for row in (None, {}, {**original, 'pk': 'USER#foreign'}, {**original, 'epoch': 3},
                    {**original, 'epoch': True}, {**original, 'suspended': True},
                    {**original, 'suspended': 0}, {**original, 'deletion_lock_request': None}):
            with self.subTest(row=row):
                self.principal = row
                self.denied()
        self.reader.assert_not_called()

    def test_revocation_after_reading_denied(self):
        for revoked in ({**self.principal, 'epoch': 3}, {**self.principal, 'suspended': True}, None):
            with self.subTest(revoked=revoked):
                self.principals.side_effect = [deepcopy(self.principal), revoked]
                self.denied()

    def test_expiry_and_clock_reversal_denied(self):
        for final in (self.now + timedelta(seconds=60), self.now - timedelta(seconds=1)):
            with self.subTest(final=final):
                self.clock.side_effect = [self.now, final]
                self.denied()

    def test_row_owner_duplicates_and_missing_meta_denied(self):
        original = deepcopy(self.rows)
        for rows in ([], [*original, original[0]], [{**original[0], 'pk': 'foreign'}]):
            with self.subTest(rows=rows):
                self.rows = rows
                self.denied()

    def test_session_owner_changes_during_read_denied(self):
        def reader(_pk):
            self.session.subject = 'foreign-customer'
            return deepcopy(self.rows)
        self.reader.side_effect = reader
        self.denied()

    def test_expired_session_never_reads_account_or_meal(self):
        self.session.expires = int(self.now.timestamp())
        self.denied()
        self.reader.assert_not_called()
        self.principals.assert_not_called()

    def test_version_digest_and_meal_identity_are_bound(self):
        for field, value in [('version', 4), ('version', True), ('meal_batch_id', str(uuid4())),
                             ('provenance', 'REAL')]:
            with self.subTest(field=field):
                old = self.rows[0][field]
                self.rows[0][field] = value
                self.denied()
                self.rows[0][field] = old
        self.denied(source_digest='b' * 64)

    def test_complete_contiguous_chunk_contract_required(self):
        original = deepcopy(self.chunk)
        for change in ({'pk': 'foreign#SOURCE'}, {'sk': 'RAW#0001'}, {'first_sequence': True}, {'first_sequence': 2},
                       {'records': ['one', 'two']}, {'records': ['one', 'two', None]}):
            with self.subTest(change=change):
                self.chunk = {**original, **change}
                self.denied()
        self.chunk = None
        self.denied()

    def test_concurrent_nested_change_even_with_unchanged_version_denied(self):
        def reader(_pk, _index):
            # Return shared mutable objects, as a faulty shallow-copy store might.
            if self.sources.call_count == 2:
                self.chunk['records'][0] = 'changed'
            return self.chunk
        self.sources.side_effect = reader
        self.denied()

    def test_boolean_numeric_change_is_not_hidden_by_python_equality(self):
        first, second = deepcopy(self.rows), deepcopy(self.rows)
        first[0]['extra'], second[0]['extra'] = True, 1
        self.reader.side_effect = [first, second]
        self.denied()

    def test_integral_dynamo_decimals_accepted_without_converting_unbounded_values(self):
        self.principal['epoch'] = Decimal(2)
        self.rows[0]['version'] = Decimal(3)
        self.rows[0]['source']['record_count'] = Decimal(3)
        self.chunk['first_sequence'] = Decimal(1)
        self.read()
        self.observer.reset_mock()
        self.rows[0]['source']['record_count'] = Decimal('1E100000')
        self.denied()

    def test_observation_integrity_failure_does_not_escape_as_usable_result(self):
        self.observer.side_effect = ValueError('raw hash mismatch')
        with self.assertRaises(CombiObservationUnavailable):
            self.read()


if __name__ == '__main__':
    unittest.main()
