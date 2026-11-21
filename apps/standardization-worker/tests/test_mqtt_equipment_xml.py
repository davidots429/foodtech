import json
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from foodtech_contracts import decode_record
from foodtech_standardization_worker.process import assess

ROOT = Path(__file__).resolve().parents[3]


class EquipmentXmlTests(unittest.TestCase):
    def test_equipment_variants_and_unsupported_humidity(self):
        for profile in ('311', '321'):
            for vendor in 'abc':
                with self.subTest(profile=profile, vendor=vendor):
                    records = json.loads((ROOT / f'fixtures/mockprofile-{profile}-{vendor}-001.json').read_text())['records']
                    results = [assess(str(i), raw) for i, raw in enumerate(records)]
                    self.assertEqual([item.quality_status for item in results], ['VALID', 'VALID', 'QUARANTINED'])
                    self.assertEqual(results[0].mapping_version, f'mockprofile-{profile}-{vendor}-map-v1')
                    self.assertIn('POT_TEMPERATURE' if profile == '311' else 'CHAMBER_TEMPERATURE', results[0].normalized)
                    with self.assertRaises(ValueError): decode_record('<!DOCTYPE record>' + records[0])
                    if profile == '321':
                        self.assertIn('RELATIVE_HUMIDITY', results[0].normalized)
                        root = ET.fromstring(records[0])
                        source, humidity = {'a': ('extensions/humiditySource', 'extensions/humidityPercent'),
                            'b': ('ext_humidity_source', 'ext_humidity_percent'),
                            'c': ('Extension/HumiditySource', 'Extension/HumidityPct')}[vendor]
                        root.find(source).text = 'NOT_SUPPORTED'; root.find(humidity).text = ''
                        result = assess('unsupported', ET.tostring(root, encoding='unicode'))
                        self.assertEqual(result.normalized['humidity_value_source'], 'NOT_SUPPORTED')
                        self.assertNotIn('RELATIVE_HUMIDITY', result.normalized)


if __name__ == '__main__': unittest.main()
