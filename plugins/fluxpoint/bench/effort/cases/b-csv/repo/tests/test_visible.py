import unittest
from csvlite import parse


class Visible(unittest.TestCase):
    def test_simple(self):
        self.assertEqual(parse("a,b\n1,2\n"), [["a", "b"], ["1", "2"]])

    def test_quoted_comma(self):
        self.assertEqual(parse('"x,y",z'), [["x,y", "z"]])
