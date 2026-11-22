import unittest
from types import SimpleNamespace
from unittest.mock import patch

from foodtech_business_api.legacy_meal_scan import LegacyMealPage
from foodtech_business_api.meal_scan_progress import MealCheckpointCipher, MealScanProgressStore
from foodtech_business_api.profile_checkpoint_cipher import ProfileCheckpointCipher
from test_profile_scan_progress import Cipher, Table


class FakeMealCipher(MealCheckpointCipher, Cipher):
    def __init__(self): Cipher.__init__(self)
    seal = Cipher.seal
    open = Cipher.open


class MealProgressTests(unittest.TestCase):
    def setUp(self):
        self.table, self.cipher = Table(), FakeMealCipher()
        self.store = MealScanProgressStore(self.table, self.cipher)
        self.job = '12c5f2d4-c6d2-4165-adf7-e7fed537af06'
        self.arn = 'arn:aws:dynamodb:ap-southeast-2:123456789012:table/cooking'
        self.source = SimpleNamespace(table_arn=self.arn)
        self.profiles = SimpleNamespace(table=self.source)
        self.store.start(self.job, 'owner-a', self.arn)

    def test_failure_resume_and_owner_binding(self):
        self.assertEqual(self.table.item['pk'], 'MEAL_SCAN#' + self.job)
        with patch('foodtech_business_api.meal_scan_progress.reconcile_legacy_meal_page', side_effect=RuntimeError('failed')):
            with self.assertRaises(RuntimeError):
                self.store.run_page(job_id=self.job, subject='owner-a', source_table=self.source, profile_store=self.profiles, limit=10)
        self.assertEqual(self.table.item['version'], 1)
        with patch('foodtech_business_api.meal_scan_progress.reconcile_legacy_meal_page',
                   side_effect=[LegacyMealPage((), {'pk': 'private', 'sk': 'META'}, 10), LegacyMealPage((), None, 1)]):
            first = self.store.run_page(job_id=self.job, subject='owner-a', source_table=self.source, profile_store=self.profiles, limit=10)
            self.assertEqual((first.phase, first.version), ('CONTINUE', 2))
            with self.assertRaises(ValueError):
                self.store.read_progress(self.job, 'owner-b', self.arn)
            last = self.store.run_page(job_id=self.job, subject='owner-a', source_table=self.source, profile_store=self.profiles, limit=10)
            self.assertEqual((last.phase, last.version), ('SCAN_COMPLETE', 3))

    def test_cipher_purpose_is_separate(self):
        profile = object.__new__(ProfileCheckpointCipher); profile.environment = 'dev'
        meal = object.__new__(MealCheckpointCipher); meal.environment = 'dev'
        profile_context = profile._context(self.job, 'INITIAL', 1, self.arn)
        meal_context = meal._context(self.job, 'INITIAL', 1, self.arn)
        self.assertNotEqual(profile_context['purpose'], meal_context['purpose'])
        with self.assertRaises(ValueError):
            MealScanProgressStore(self.table, profile)


if __name__ == '__main__':
    unittest.main()
