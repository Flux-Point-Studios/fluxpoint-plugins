import unittest
from semver import satisfies


class Hidden(unittest.TestCase):
    def test_caret(self):
        self.assertTrue(satisfies("1.4.0", "^1.2.3"))
        self.assertFalse(satisfies("2.0.0", "^1.2.3"))
        self.assertFalse(satisfies("1.2.2", "^1.2.3"))

    def test_caret_zero(self):
        self.assertTrue(satisfies("0.2.9", "^0.2.3"))
        self.assertFalse(satisfies("0.3.0", "^0.2.3"))
        self.assertTrue(satisfies("0.0.3", "^0.0.3"))
        self.assertFalse(satisfies("0.0.4", "^0.0.3"))

    def test_tilde(self):
        self.assertTrue(satisfies("1.2.9", "~1.2.3"))
        self.assertFalse(satisfies("1.3.0", "~1.2.3"))

    def test_x_ranges(self):
        self.assertTrue(satisfies("1.2.7", "1.2.x"))
        self.assertTrue(satisfies("1.2.0", "1.2.*"))
        self.assertTrue(satisfies("1.2.5", "1.2"))
        self.assertFalse(satisfies("1.3.0", "1.2"))
        self.assertTrue(satisfies("1.9.9", "1.x"))
        self.assertTrue(satisfies("1.0.0", "1"))
        self.assertFalse(satisfies("2.0.0", "1"))
        self.assertTrue(satisfies("7.1.1", "*"))
        self.assertTrue(satisfies("7.1.1", ""))

    def test_and_set(self):
        self.assertTrue(satisfies("1.5.0", ">=1.2.0 <2.0.0"))
        self.assertFalse(satisfies("2.0.0", ">=1.2.0 <2.0.0"))
        self.assertTrue(satisfies("1.2.0", "<=1.2.0 >1.1.9"))

    def test_union(self):
        self.assertTrue(satisfies("3.0.0", "^1.0.0 || >=3.0.0"))
        self.assertFalse(satisfies("2.5.0", "^1.0.0 || >=3.0.0"))

    def test_exact(self):
        self.assertTrue(satisfies("1.2.3", "=1.2.3"))
        self.assertTrue(satisfies("1.2.3+build.5", "1.2.3"))
        self.assertFalse(satisfies("1.2.4", "1.2.3"))

    def test_numeric_compare(self):
        self.assertTrue(satisfies("1.10.0", ">1.9.0"))
        self.assertTrue(satisfies("10.0.0", ">9.0.0"))

    def test_prerelease_precedence(self):
        self.assertTrue(satisfies("1.0.0-alpha.10", ">1.0.0-alpha.9"))
        self.assertTrue(satisfies("1.0.0-alpha.beta", ">1.0.0-alpha.1"))
        self.assertTrue(satisfies("1.0.0-alpha.1", ">1.0.0-alpha"))
        self.assertTrue(satisfies("1.0.0", ">1.0.0-rc.1"))
        self.assertFalse(satisfies("1.0.0-rc.1", ">=1.0.0-rc.1 <1.0.0-rc.1"))

    def test_prerelease_gating(self):
        self.assertTrue(satisfies("1.2.3-beta.2", ">=1.2.3-beta.1"))
        self.assertFalse(satisfies("1.2.4-beta", ">=1.2.3-beta.1"))
        self.assertFalse(satisfies("1.2.4-beta", "*"))
        self.assertFalse(satisfies("1.3.0-rc.1", "^1.2.0"))
        self.assertTrue(satisfies("1.3.0-rc.1", "^1.2.0 || >=1.3.0-rc.0"))

    def test_malformed(self):
        for v, r in (("1.2", "*"), ("a.b.c", "*"), ("1.2.3", ">=x.y"), ("1.2.3", "^1.2.3.4"),
                     ("01.2.3", "*")):
            with self.assertRaises(ValueError, msg=(v, r)):
                satisfies(v, r)
