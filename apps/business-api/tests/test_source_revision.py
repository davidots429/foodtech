import unittest
from decimal import Decimal
from unittest.mock import Mock, patch

from foodtech_business_api.source_revision import _read


class SourceRevisionTests(unittest.TestCase):
    def test_consistent_read_and_missing_record(self):
        table = Mock()
        table.get_item.return_value = {"Item": {"revision": Decimal(4)}}
        with patch("foodtech_business_api.source_revision._table", return_value=table):
            self.assertEqual(_read("data", "STREAM#test"), 4)
            table.get_item.assert_called_once_with(Key={"pk": "STREAM#test", "sk": "SOURCE#REVISION"},
                                                   ConsistentRead=True)
            table.get_item.return_value = {}
            self.assertIsNone(_read("data", "STREAM#test"))

    def test_invalid_revision_is_not_accepted(self):
        table = Mock()
        with patch("foodtech_business_api.source_revision._table", return_value=table):
            for invalid in (True, 0, -1, "4", Decimal("1.5"), Decimal("NaN"), None):
                table.get_item.return_value = {"Item": {"revision": invalid}}
                with self.subTest(value=invalid), self.assertRaises(ValueError):
                    _read("data", "STREAM#test")
