import csv
import io
import json
import unittest
from pathlib import Path

from foodtech_contracts import decode_record
from foodtech_standardization_worker.process import assess

ROOT = Path(__file__).resolve().parents[3]


class EquipmentCsvTests(unittest.TestCase):
    def test_equipment_variants_preserve_types_and_reject_formula(self):
        for profile in ('312', '322'):
            for vendor in 'abc':
                with self.subTest(profile=profile, vendor=vendor):
                    fixture = json.loads((ROOT / f'fixtures/mockprofile-{profile}-{vendor}-001.json').read_text())
                    for raw in fixture['records']:
                        result = assess('test', raw)
                        self.assertEqual(result.quality_status, 'VALID')
                        self.assertEqual(result.mapping_version, f'mockprofile-{profile}-{vendor}-map-v1')
                        self.assertIn('POT_TEMPERATURE' if profile == '312' else 'CHAMBER_TEMPERATURE', result.normalized)
                        if profile == '322': self.assertIn('RELATIVE_HUMIDITY', result.normalized)
                    header, row = list(csv.reader(io.StringIO(fixture['records'][0])))
                    row[8] = '=1+1'
                    output = io.StringIO(newline=''); csv.writer(output, lineterminator='\n').writerows([header, row])
                    with self.assertRaises(ValueError): decode_record(output.getvalue())
                    if profile == '322':
                        header, row = list(csv.reader(io.StringIO(fixture['records'][0])))
                        source, humidity = {'a': ('extensions.humiditySource', 'extensions.humidityPercent'),
                                            'b': ('ext_humidity_source', 'ext_humidity_percent'),
                                            'c': ('Extension_HumiditySource', 'Extension_HumidityPct')}[vendor]
                        row[header.index(source)] = 'NOT_SUPPORTED'; row[header.index(humidity)] = ''
                        output = io.StringIO(newline=''); csv.writer(output, lineterminator='\n').writerows([header, row])
                        result = assess('unsupported', output.getvalue())
                        self.assertEqual(result.normalized['humidity_value_source'], 'NOT_SUPPORTED')
                        self.assertNotIn('RELATIVE_HUMIDITY', result.normalized)


if __name__ == '__main__': unittest.main()
