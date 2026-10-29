import unittest
from duration import parse, format_ms


class Visible(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(parse("1h30m"), 5400000)

    def test_format(self):
        self.assertEqual(format_ms(5400000), "1h30m")
