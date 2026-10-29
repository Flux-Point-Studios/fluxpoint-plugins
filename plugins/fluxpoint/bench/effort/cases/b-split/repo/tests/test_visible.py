import unittest
from ledger import split


class Visible(unittest.TestCase):
    def test_even(self):
        self.assertEqual(split(10, [1, 1]), [5, 5])

    def test_remainder(self):
        self.assertEqual(split(10, [1, 1, 1]), [4, 3, 3])
