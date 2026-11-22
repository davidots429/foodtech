import unittest

from foodtech_business_api.legacy_profile_scan import reconcile_legacy_page, scan_legacy_profiles


class Table:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def scan(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


class LegacyProfileScanTests(unittest.TestCase):
    def test_empty_filtered_page_keeps_checkpoint_and_does_not_expose_it_in_repr(self):
        key = {'pk': 'other-owner-secret', 'sk': 'META'}
        table = Table({'Items': [], 'ScannedCount': 10, 'LastEvaluatedKey': key})
        page = scan_legacy_profiles(table, subject='alice', limit=10)
        self.assertEqual(page.references, ())
        self.assertEqual(page.checkpoint, key)
        self.assertNotIn('other-owner-secret', repr(page))
        self.assertTrue(table.calls[0]['ConsistentRead'])
        self.assertEqual(table.calls[0]['Limit'], 10)
        scan_legacy_profiles(table, subject='alice', limit=10, checkpoint=key)
        self.assertEqual(table.calls[1]['ExclusiveStartKey'], key)

    def test_validates_owner_keys_before_returning_reference(self):
        row = {'pk': 'SCOPE#USER#alice#CONDITION_PROFILE#p1', 'sk': 'VERSION#v1',
               'profile_id': 'p1', 'profile_version': 'v1'}
        page = scan_legacy_profiles(Table({'Items': [row], 'ScannedCount': 1}), subject='alice', limit=10)
        self.assertEqual(page.references, (('p1', 'v1'),))
        for changed in ({'pk': row['pk'].replace('alice', 'bob')}, {'profile_version': 'v2'},
                        {'profile_id': 'bad#key'}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                scan_legacy_profiles(Table({'Items': [{**row, **changed}], 'ScannedCount': 1}), subject='alice', limit=10)

    def test_rejects_invalid_scope_checkpoint_and_malformed_page(self):
        for subject, limit, checkpoint in [('alice#bob', 1, None), ('alice', 0, None),
                                           ('alice', 1, {'pk': 'only'})]:
            with self.subTest(subject=subject), self.assertRaises(ValueError):
                scan_legacy_profiles(Table({}), subject=subject, limit=limit, checkpoint=checkpoint)
        with self.assertRaises(ValueError):
            scan_legacy_profiles(Table({'Items': [], 'ScannedCount': 101}), subject='alice', limit=100)

    def test_page_checkpoint_is_not_returned_when_reconciliation_fails(self):
        rows = [{'pk': f'SCOPE#USER#alice#CONDITION_PROFILE#{p}', 'sk': 'VERSION#v1',
                 'profile_id': p, 'profile_version': 'v1'} for p in ('p1', 'p2')]
        table = Table({'Items': rows, 'ScannedCount': 2, 'LastEvaluatedKey': {'pk': 'next', 'sk': 'key'}})
        class Store:
            def __init__(self): self.calls = []
            def ensure_condition_profile_listed(self, subject, profile_id, version):
                self.calls.append(profile_id)
                if profile_id == 'p2': raise RuntimeError('failed')
        store = Store()
        with self.assertRaises(RuntimeError):
            reconcile_legacy_page(table, store, subject='alice', limit=2)
        self.assertEqual(store.calls, ['p1', 'p2'])


if __name__ == '__main__':
    unittest.main()
