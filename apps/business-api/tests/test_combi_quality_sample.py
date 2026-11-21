import unittest
from copy import deepcopy
from decimal import Decimal

from foodtech_business_api.combi_quality_sample import current_quality
from foodtech_business_api.cooking import SCORE_ITEMS


class CombiQualitySampleTests(unittest.TestCase):
    def setUp(self):
        scores = {name: Decimal(4) for name in SCORE_ITEMS}
        assessment = {'version': Decimal(1), 'safety_version': Decimal(2), 'menu': 'RICE',
                      'status': 'COMPLETED', 'scale_version': 'rice-common-v1', 'scores': scores,
                      'quality_status': 'QUALITY_PASSED', 'score_percent': '80.00', 'quality_target': True}
        pointer = {'version': Decimal(1), 'safety_version': Decimal(2), 'event_sk': 'EVENT#1',
                   'quality_status': 'QUALITY_PASSED', 'score_percent': '80.00', 'quality_target': True, 'usable': True}
        self.view = {'meal': {'version': Decimal(3)}, 'rice': {'status': 'UNLOADED', 'safety_status': 'PASSED',
                     'safety_version': Decimal(2), 'quality_version': Decimal(1), 'quality_current': pointer},
                     'events': [{'sk': 'EVENT#1', 'event': 'RICE_QUALITY_ASSESSED', 'assessment': assessment}]}

    def test_dynamodb_numeric_values_are_accepted(self):
        result = current_quality(self.view, 'RICE')
        self.assertEqual((result['status'], result['quality_version'], result['safety_version']), ('ELIGIBLE', 1, 2))

    def test_invalidated_or_missing_pointer_is_excluded(self):
        self.view['rice']['quality_current']['usable'] = False
        self.assertEqual(current_quality(self.view, 'RICE')['reason'], 'CURRENT_ASSESSMENT_BLOCKED')
        self.view['rice']['quality_current'] = None
        self.assertEqual(current_quality(self.view, 'RICE')['reason'], 'NO_CURRENT_ASSESSMENT')

    def test_changed_or_duplicate_history_and_invalid_scores_fail(self):
        views = []
        changed = deepcopy(self.view); changed['events'][0]['assessment']['score_percent'] = '50.00'; views.append(changed)
        duplicated = deepcopy(self.view); duplicated['events'].append(deepcopy(duplicated['events'][0])); views.append(duplicated)
        invalid = deepcopy(self.view); invalid['events'][0]['assessment']['scores'][SCORE_ITEMS[0]] = True; views.append(invalid)
        for view in views:
            with self.assertRaises(ValueError):
                current_quality(view, 'RICE')


if __name__ == '__main__':
    unittest.main()
