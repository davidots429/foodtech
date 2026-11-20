import unittest
from datetime import datetime, timedelta, timezone

from foodtech_contracts.cooking_live_time import observe
from foodtech_contracts.jeyuk_process import PROFILES
from foodtech_contracts.tonkatsu_process import TIME_BANDS

NOW = datetime(2026, 11, 20, tzinfo=timezone.utc)


def job(menu, profile, seconds, status='RUNNING'):
    return {'menu': menu, 'status': status, 'actual_placed_at': (NOW - timedelta(seconds=seconds)).isoformat(),
            'thickness_profile': profile if menu == 'TONKATSU' else None,
            'jeyuk_size_profile': profile if menu == 'JEYUK' else None}


class CookingLiveTimeTests(unittest.TestCase):
    def test_all_profiles_and_inclusive_thresholds(self):
        profiles = [('TONKATSU', name, bands) for name, bands in TIME_BANDS.items()]
        profiles += [('JEYUK', name, profile.time_bands) for name, profile in PROFILES.items()]
        for menu, profile, (_, _, normal, recommended, maximum) in profiles:
            for seconds, status in ((0, 'MONITORING'), (normal, 'MONITORING'),
                (normal + 1, 'DELAY_WARNING'), (recommended, 'DELAY_WARNING'),
                (recommended + 1, 'EXTENDED_COOKING'), (maximum, 'EXTENDED_COOKING'),
                (maximum + 1, 'MAX_TIME_EXCEEDED')):
                with self.subTest(menu=menu, profile=profile, seconds=seconds):
                    result = observe(job(menu, profile, seconds), NOW)
                    self.assertEqual((result['status'], result['elapsed_seconds']), (status, seconds))
                    self.assertEqual((result['recommended_limit_seconds'], result['maximum_seconds']), (recommended, maximum))

    def test_no_early_removal_prediction_and_pause_remains_elapsed(self):
        self.assertEqual(observe(job('TONKATSU', 'STANDARD', 1), NOW)['status'], 'MONITORING')
        self.assertEqual(observe(job('TONKATSU', 'STANDARD', 361, 'PAUSED'), NOW)['status'], 'MAX_TIME_EXCEEDED')
        self.assertEqual(observe(job('TONKATSU', 'STANDARD', 361, 'SAFETY_HOLD'), NOW)['status'], 'MAX_TIME_EXCEEDED')
        self.assertEqual(observe(job('TONKATSU', 'STANDARD', 361, 'COMPLETED'), NOW)['status'], 'NOT_ACTIVE')

    def test_missing_profile_and_clock_are_not_invented(self):
        self.assertEqual(observe(job('TONKATSU', None, 10), NOW)['status'], 'NOT_APPLICABLE')
        self.assertEqual(observe(job('JEYUK', 'SMALL', -1), NOW)['status'], 'CLOCK_MISMATCH')
        data = job('TONKATSU', 'STANDARD', 1); data['actual_placed_at'] = '2026-11-20T00:00:00'
        self.assertEqual(observe(data, NOW)['status'], 'NOT_MEASURED')
        with self.assertRaises(ValueError): observe(data, NOW.replace(tzinfo=None))
