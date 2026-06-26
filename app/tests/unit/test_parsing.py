"""Pruebas del parser numerico de Google Sheets."""

import os
import sys
import unittest

CURRENT_DIR = os.path.dirname(__file__)
APP_DIR = os.path.abspath(os.path.join(CURRENT_DIR, "..", ".."))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

from utils.parsing import format_coordinate_for_sheet, parse_coordinate


class ParsingTest(unittest.TestCase):
    """Valida separadores decimales y de miles."""

    def test_parse_decimal_with_comma(self):
        self.assertEqual(parse_coordinate("4,679"), 4.679)

    def test_parse_negative_decimal_with_comma(self):
        self.assertEqual(parse_coordinate("-74,089"), -74.089)

    def test_parse_local_thousands_and_decimals(self):
        self.assertEqual(parse_coordinate("1.234,56"), 1234.56)

    def test_format_coordinate_for_sheet_uses_decimal_comma(self):
        self.assertEqual(format_coordinate_for_sheet("4.730"), "4,73")


if __name__ == "__main__":
    unittest.main()
