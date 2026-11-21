import unittest
from decimal import Decimal
from unittest.mock import patch

from foodtech_business_api.legacy_profile_scan import LegacyProfilePage
from foodtech_business_api.profile_scan_progress import ProfileScanProgressStore


class Cipher:
    def __init__(self): self.values = {}
    def seal(self, job, subject, checkpoint, *, phase, version):
        token = str(len(self.values)).encode()
        self.values[token] = (job, subject, phase, version, checkpoint.copy())
        return token
    def open(self, job, subject, ciphertext, *, phase, version):
        saved_job, saved_subject, saved_phase, saved_version, value = self.values[ciphertext]
        if (job, subject, phase, version) != (saved_job, saved_subject, saved_phase, saved_version): raise ValueError('scope mismatch')
        return value.copy()


class Table:
    def __init__(self): self.item, self.fail_update, self.fail_after_update = None, False, False
    def put_item(self, *, Item, **kwargs):
        if self.item: raise RuntimeError('duplicate')
        self.item = Item.copy()
    def get_item(self, **kwargs): return {'Item': self.item.copy()}
    def update_item(self, *, ExpressionAttributeValues, **kwargs):
        values = ExpressionAttributeValues
        if self.fail_update or self.item['version'] != values[':expected'] or self.item['phase'] != values[':old']:
            raise RuntimeError('conflict')
        self.item.update(phase=values[':next'], version=values[':next_version'], ciphertext=values[':ciphertext'])
        if self.fail_after_update: raise RuntimeError('response lost')


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.table, self.cipher = Table(), Cipher()
        self.store = ProfileScanProgressStore(self.table, self.cipher)
        self.job = '12c5f2d4-c6d2-4165-adf7-e7fed537af06'
        self.store.start(self.job, 'owner-a')

    def test_completed_pages_advance_and_wrong_owner_is_rejected(self):
        key = {'pk': 'other-owner', 'sk': 'META'}
        with patch('foodtech_business_api.profile_scan_progress.reconcile_legacy_page',
                   side_effect=[LegacyProfilePage((), key, 10), LegacyProfilePage((), None, 2)]) as scan:
            first = self.store.run_page(job_id=self.job, subject='owner-a', source_table=object(), profile_store=object(), limit=10)
            self.assertEqual((first.phase, first.version), ('CONTINUE', 2))
            with self.assertRaises(ValueError):
                self.store.run_page(job_id=self.job, subject='owner-b', source_table=object(), profile_store=object(), limit=10)
            self.table.item['version'] = Decimal(2)
            second = self.store.run_page(job_id=self.job, subject='owner-a', source_table=object(), profile_store=object(), limit=10)
            self.assertEqual((second.phase, second.version), ('SCAN_COMPLETE', 3))
            self.assertEqual(self.store.read_progress(self.job, 'owner-a'), second)
            self.assertNotIn('other-owner', repr(self.store.read_progress(self.job, 'owner-a')))
            with self.assertRaises(ValueError):
                self.store.read_progress(self.job, 'owner-b')
            self.assertEqual(scan.call_args.kwargs['checkpoint'], key)
            with self.assertRaises(ValueError):
                self.store.run_page(job_id=self.job, subject='owner-a', source_table=object(), profile_store=object(), limit=10)

    def test_failure_and_racing_update_do_not_advance_checkpoint(self):
        with patch('foodtech_business_api.profile_scan_progress.reconcile_legacy_page', side_effect=RuntimeError('failed')):
            with self.assertRaises(RuntimeError):
                self.store.run_page(job_id=self.job, subject='owner-a', source_table=object(), profile_store=object(), limit=10)
        self.assertEqual(self.table.item['version'], 1)
        self.table.fail_update = True
        with patch('foodtech_business_api.profile_scan_progress.reconcile_legacy_page',
                   return_value=LegacyProfilePage((), {'pk': 'next', 'sk': 'META'}, 10)):
            with self.assertRaises(RuntimeError):
                self.store.run_page(job_id=self.job, subject='owner-a', source_table=object(), profile_store=object(), limit=10)
        self.assertEqual((self.table.item['phase'], self.table.item['version']), ('INITIAL', 1))
        self.assertEqual(self.store.read_progress(self.job, 'owner-a').phase, 'INITIAL')

    def test_response_loss_can_be_resolved_without_exposing_checkpoint(self):
        self.table.fail_after_update = True
        with patch('foodtech_business_api.profile_scan_progress.reconcile_legacy_page',
                   return_value=LegacyProfilePage((), None, 0)):
            with self.assertRaisesRegex(RuntimeError, 'response lost'):
                self.store.run_page(job_id=self.job, subject='owner-a', source_table=object(), profile_store=object(), limit=10)
        result = self.store.read_progress(self.job, 'owner-a')
        self.assertEqual((result.phase, result.version), ('SCAN_COMPLETE', 2))
        with self.assertRaises(ValueError):
            self.store.read_progress(self.job, 'owner-b')

    def test_old_ciphertext_cannot_be_replayed_at_a_new_version(self):
        old_ciphertext = self.table.item['ciphertext']
        with patch('foodtech_business_api.profile_scan_progress.reconcile_legacy_page',
                   return_value=LegacyProfilePage((), {'pk': 'next', 'sk': 'META'}, 1)):
            self.store.run_page(job_id=self.job, subject='owner-a', source_table=object(), profile_store=object(), limit=1)
        self.table.item['ciphertext'] = old_ciphertext
        with self.assertRaises(ValueError):
            self.store.read_progress(self.job, 'owner-a')

    def test_last_version_is_readable_but_cannot_advance(self):
        maximum = 2**63 - 1
        self.table.item.update(phase='CONTINUE', version=maximum - 1,
            ciphertext=self.cipher.seal(self.job, 'owner-a', {'pk': 'next', 'sk': 'META'},
                                        phase='CONTINUE', version=maximum - 1))
        with patch('foodtech_business_api.profile_scan_progress.reconcile_legacy_page',
                   return_value=LegacyProfilePage((), {'pk': 'next2', 'sk': 'META'}, 1)):
            self.assertEqual(self.store.run_page(job_id=self.job, subject='owner-a', source_table=object(),
                                                 profile_store=object(), limit=10).version, maximum)
        self.assertEqual(self.store.read_progress(self.job, 'owner-a').version, maximum)
        with patch('foodtech_business_api.profile_scan_progress.reconcile_legacy_page') as scan:
            with self.assertRaisesRegex(ValueError, 'version exhausted'):
                self.store.run_page(job_id=self.job, subject='owner-a', source_table=object(), profile_store=object(), limit=10)
            scan.assert_not_called()


if __name__ == '__main__':
    unittest.main()
