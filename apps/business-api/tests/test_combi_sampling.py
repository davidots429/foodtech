import unittest

from foodtech_contracts.combi_sampling import selection_issues


def product(key, shelf, position=None, rack="a", mass="0.13", thickness="1.5"):
    return {"product_id": key, "shelf": shelf, "rack_id": rack, "position": position,
            "mass_kg": mass, "thickness_cm": thickness}


class SamplingTests(unittest.TestCase):
    def test_single_shelf_and_fewer_than_three(self):
        items = [product("a", 1, "LEFT"), product("b", 1, "CENTER"), product("c", 1, "RIGHT")]
        self.assertEqual(selection_issues(items, {"a", "b", "c"}, None), [])
        self.assertEqual(selection_issues(items[:2], {"a", "b"}, None), [])
        items[2]["position"] = "LEFT"
        self.assertIn("SINGLE_SHELF_POSITIONS_INCOMPLETE", selection_issues(items, {"a", "b", "c"}, None))

    def test_two_shelves_and_thickest(self):
        items = [product("a", 1), product("b", 2, "LEFT"), product("c", 2, "RIGHT", thickness="2.0"),
                 product("d", 1)]
        self.assertEqual(selection_issues(items, {"a", "b", "c"}, None), [])
        issues = selection_issues(items, {"a", "b", "d"}, None)
        self.assertIn("THICKEST_PRODUCT_NOT_SELECTED", issues)
        self.assertIn("SHELF_LOAD_SELECTION_INCOMPLETE", issues)

    def test_three_shelves_and_two_racks(self):
        items = [product("a", 1), product("b", 2), product("c", 3), product("d", 4)]
        self.assertEqual(selection_issues(items, {"a", "b", "d"}, None), [])
        self.assertIn("SHELF_COVERAGE_INCOMPLETE", selection_issues(items, {"a", "b", "c"}, None))
        items = [product("a", 1, rack="a"), product("b", 2, rack="a"),
                 product("c", 3, rack="b"), product("d", 4, rack="b")]
        self.assertIn("EQUAL_RACK_SELECTION_REASON_REQUIRED", selection_issues(items, {"a", "b", "d"}, None))
        self.assertIn("RACK_HEIGHT_COVERAGE_INCOMPLETE", selection_issues(items, {"a", "b", "c"}, "이유"))
        self.assertEqual(selection_issues(items, {"a", "b", "d"}, "두 랙과 상하 위치를 선택"), [])
        items[0]["mass_kg"] = "0.15"
        self.assertIn("RACK_LOAD_SELECTION_INCOMPLETE", selection_issues(items, {"a", "c", "d"}, None))
