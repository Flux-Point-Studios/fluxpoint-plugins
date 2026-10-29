import unittest
from semver import satisfies


class Visible(unittest.TestCase):
    def test_caret(self):
        self.assertTrue(satisfies("1.4.0", "^1.2.3"))
        self.assertFalse(satisfies("2.0.0", "^1.2.3"))

    def test_union(self):
        self.assertTrue(satisfies("3.0.0", "^1.0.0 || >=3.0.0"))
