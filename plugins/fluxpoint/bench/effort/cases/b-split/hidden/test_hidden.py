import unittest
from ledger import split


class Hidden(unittest.TestCase):
    def test_even(self):
        self.assertEqual(split(10, [1, 1]), [5, 5])

    def test_remainder_ties_low_index(self):
        self.assertEqual(split(10, [1, 1, 1]), [4, 3, 3])

    def test_largest_remainder_not_first(self):
        # 7*1/6=1.166, 7*2/6=2.333, 7*3/6=3.5 -> floors 1,2,3 =6, one left -> index 2 (rem 3)
        self.assertEqual(split(7, [1, 2, 3]), [1, 2, 4])

    def test_zero_weight_gets_nothing(self):
        self.assertEqual(split(5, [0, 1, 1]), [0, 3, 2])

    def test_zero_amount(self):
        self.assertEqual(split(0, [3, 4]), [0, 0])

    def test_big_no_float(self):
        a = 2 ** 70 + 1
        r = split(a, [1, 2])
        self.assertEqual(sum(r), a)
        self.assertEqual(r, [(a * 1) // 3 + 1, (a * 2) // 3])

    def test_conservation_many(self):
        for amount in (0, 1, 2, 99, 1000, 12345):
            for weights in ([1], [5, 0, 3], [7, 7, 7, 7], [1, 1000000], [2, 3, 5, 7, 11]):
                self.assertEqual(sum(split(amount, weights)), amount)

    def test_rejects_negative_amount(self):
        with self.assertRaises(ValueError):
            split(-1, [1])

    def test_rejects_float(self):
        with self.assertRaises(ValueError):
            split(1.0, [1])
        with self.assertRaises(ValueError):
            split(1, [1.0])

    def test_rejects_bool(self):
        with self.assertRaises(ValueError):
            split(True, [1])
        with self.assertRaises(ValueError):
            split(3, [True, 1])

    def test_rejects_empty_and_zero(self):
        with self.assertRaises(ValueError):
            split(1, [])
        with self.assertRaises(ValueError):
            split(1, [0, 0])

    def test_rejects_negative_weight(self):
        with self.assertRaises(ValueError):
            split(1, [2, -1])
