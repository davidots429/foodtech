import unittest

from foodtech_contracts.combi_fan import observe


class FanObservationTests(unittest.TestCase):
    def test_match_deviation_and_incomplete_are_distinct(self):
        self.assertEqual(observe([4, 4], 4)["status"], "MATCHED")
        self.assertEqual(observe([None, None], 4)["status"], "NOT_MEASURED")
        self.assertEqual(observe([4, None], 4)["status"], "INCOMPLETE")
        result = observe([3, None, 4, 3], 3)
        self.assertEqual((result["status"], result["deviation_seconds"], result["first_deviation_second"],
                          result["missing_seconds"]), ("DEVIATION_OBSERVED", 1, 3, 1))
        self.assertEqual(result["reason_code"], "FAN_DEVIATION")

    def test_invalid_values_are_rejected(self):
        for levels in ([True], [-1], [11], ["4"]):
            with self.assertRaises(ValueError):
                observe(levels, 4)
