import unittest

from foodtech_business_api.legacy_meal_scan import reconcile_legacy_meal_page, scan_legacy_meals


class Table:
    def __init__(self, response):
        self.response, self.calls = response, []

    def scan(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


class LegacyMealScanTests(unittest.TestCase):
    def test_empty_page_preserves_private_checkpoint(self):
        checkpoint = {'pk': 'other-owner-secret', 'sk': 'META'}
        table = Table({'Items': [], 'ScannedCount': 10, 'LastEvaluatedKey': checkpoint})
        page = scan_legacy_meals(table, subject='alice', limit=10)
        self.assertEqual(page.references, ())
        self.assertEqual(page.checkpoint, checkpoint)
        self.assertNotIn('other-owner-secret', repr(page))
        self.assertTrue(table.calls[0]['ConsistentRead'])
        scan_legacy_meals(table, subject='alice', limit=10, checkpoint=checkpoint)
        self.assertEqual(table.calls[1]['ExclusiveStartKey'], checkpoint)

    def test_rejects_malformed_and_cross_owner_rows(self):
        row = {'pk': 'SCOPE#USER#alice#COMBI_MEAL#old', 'sk': 'META', 'meal_batch_id': 'old'}
        self.assertEqual(scan_legacy_meals(Table({'Items': [row], 'ScannedCount': 1}),
                                           subject='alice', limit=1).references, ('old',))
        for change in ({'pk': row['pk'].replace('alice', 'bob')}, {'meal_batch_id': 'other'},
                       {'meal_batch_id': 'bad#key'}, {'sk': 'CHUNK#1'}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                scan_legacy_meals(Table({'Items': [{**row, **change}], 'ScannedCount': 1}), subject='alice', limit=1)
        for subject, limit, checkpoint in [('alice#bad', 1, None), ('alice', 0, None), ('alice', 1, {'pk': 'only'})]:
            with self.subTest(subject=subject), self.assertRaises(ValueError):
                scan_legacy_meals(Table({}), subject=subject, limit=limit, checkpoint=checkpoint)

    def test_failed_reconciliation_does_not_return_page_checkpoint(self):
        rows = [{'pk': f'SCOPE#USER#alice#COMBI_MEAL#{meal}', 'sk': 'META', 'meal_batch_id': meal}
                for meal in ('one', 'two')]
        table = Table({'Items': rows, 'ScannedCount': 2, 'LastEvaluatedKey': {'pk': 'next', 'sk': 'META'}})
        class Store:
            def __init__(self): self.calls = []
            def ensure_meal_listed(self, subject, meal):
                self.calls.append(meal)
                if meal == 'two': raise RuntimeError('failed')
        store = Store()
        with self.assertRaises(RuntimeError):
            reconcile_legacy_meal_page(table, store, subject='alice', limit=2)
        self.assertEqual(store.calls, ['one', 'two'])


if __name__ == '__main__':
    unittest.main()
