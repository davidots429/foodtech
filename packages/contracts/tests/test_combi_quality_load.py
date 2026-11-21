import json
import unittest
from decimal import Decimal
from pathlib import Path

from foodtech_contracts.combi_quality_load import RULE_VERSION, classify


class CombiQualityLoadTests(unittest.TestCase):
    def test_fixtures_and_boundary_are_consistent(self):
        fixture = json.loads((Path(__file__).resolve().parents[3] / 'fixtures/combi-quality-load-v1.json').read_text())
        self.assertEqual(fixture['provenance'], 'SYNTHETIC')
        self.assertEqual(fixture['rule_version'], RULE_VERSION)
        self.assertEqual(len(fixture['cases']), 4)
        for case in fixture['cases']:
            menu, size = case['menu'], case['equipment_size']
            partial = classify(menu, size, case['partial_mass_kg'])
            reference = classify(menu, size, case['reference_mass_kg'])
            self.assertEqual((partial.band, partial.ratio), ('PARTIAL', Decimal('0.5')))
            self.assertEqual((reference.band, reference.ratio), ('REFERENCE', Decimal('1')))
            self.assertEqual(classify(menu, size, partial.actual_mass_kg + Decimal('0.001')).band, 'REFERENCE')
            self.assertEqual(classify(menu, size, reference.actual_mass_kg + Decimal('0.001')).band, 'OVER_RECOMMENDED')

    def test_bad_or_nonfinite_inputs_are_rejected(self):
        for value in ('0', '-1', 'NaN', 'Infinity', '', True, 1.0):
            with self.assertRaises(ValueError):
                classify('RICE', 'MEDIUM_FIXED_RACK_10', value)
        with self.assertRaises(ValueError):
            classify('RICE', 'UNKNOWN', '1')


if __name__ == '__main__':
    unittest.main()
