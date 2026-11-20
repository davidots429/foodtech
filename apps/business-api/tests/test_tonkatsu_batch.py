import unittest
import json
from pathlib import Path
from decimal import Decimal

from foodtech_contracts.tonkatsu_batch import batch


class TonkatsuBatchTests(unittest.TestCase):
    def test_versioned_fixture_examples_match_calculation(self):
        fixture = json.loads((Path(__file__).resolve().parents[3] / "fixtures/tonkatsu-business-v2.json").read_text())
        for key, mode in (("referenceExample", "REFERENCE"), ("partialExample", "PARTIAL")):
            item = fixture[key]
            result = batch(item["profile"], item["productWeightsGrams"], mode)
            self.assertEqual(Decimal(result["total_mass_kg"]), Decimal(item["massKg"]))
            self.assertEqual(Decimal(result["load_percent"]), Decimal(item["ratedLoadPercent"]))

    def test_approved_reference_profiles(self):
        for profile, count, weight, total in (("THIN", 18, 135, "2.43"),
                                               ("STANDARD", 15, 160, "2.4"),
                                               ("THICK", 12, 200, "2.4")):
            with self.subTest(profile=profile):
                value = batch(profile, [weight] * count, "REFERENCE")
                self.assertEqual((value["total_mass_kg"], value["product_count"]), (total, count))
                self.assertEqual(value["rated_capacity_kg"], "4.0")

    def test_weight_count_and_total_bounds(self):
        self.assertEqual(batch("STANDARD", [155] * 15, "REFERENCE")["total_mass_kg"], "2.325")
        self.assertEqual(batch("STANDARD", [165] * 15, "REFERENCE")["total_mass_kg"], "2.475")
        for weights, mode in (([154] * 15, "REFERENCE"), ([166] * 15, "REFERENCE"),
                              ([160] * 16, "REFERENCE"), ([160] * 14, "REFERENCE"),
                              ([160] * 15, "PARTIAL")):
            with self.subTest(mode=mode, weights=weights), self.assertRaises(ValueError):
                batch("STANDARD", weights, mode)
        self.assertEqual(batch("STANDARD", [160] * 2, "PARTIAL")["load_percent"], "8.00")
