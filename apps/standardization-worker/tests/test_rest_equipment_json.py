import json
import unittest
from pathlib import Path
from foodtech_standardization_worker.process import assess

ROOT = Path(__file__).resolve().parents[3]


class RestEquipmentJsonTests(unittest.TestCase):
    def test_four_vendor_fixtures_preserve_equipment_semantics(self):
        for profile in ('110', '120'):
            for vendor in 'bc':
                with self.subTest(profile=profile, vendor=vendor):
                    fixture = json.loads((ROOT / f'fixtures/mockprofile-{profile}-{vendor}-001.json').read_text())
                    results = [assess(str(i), json.dumps(row)) for i, row in enumerate(fixture['records'])]
                    self.assertEqual([x.quality_status for x in results], ['VALID', 'VALID', 'QUARANTINED'])
                    self.assertEqual(results[0].mapping_version, f'mockprofile-{profile}-{vendor}-map-v1')
                    self.assertIn('POT_TEMPERATURE' if profile == '110' else 'RELATIVE_HUMIDITY', results[0].normalized)
                    self.assertNotIn('ROTATIONAL_SPEED_RPM', results[0].normalized)


if __name__ == '__main__': unittest.main()
