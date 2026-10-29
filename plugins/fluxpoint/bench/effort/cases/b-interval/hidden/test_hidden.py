import time
import unittest
from intervals import IntervalSet


def mk(*pairs):
    s = IntervalSet()
    for a, b in pairs:
        s.add(a, b)
    return s


class Hidden(unittest.TestCase):
    def test_add_merge(self):
        self.assertEqual(mk((0, 5), (3, 8)).intervals(), [(0, 8)])

    def test_touching_merges(self):
        self.assertEqual(mk((0, 5), (5, 8)).intervals(), [(0, 8)])
        self.assertEqual(mk((5, 8), (0, 5)).intervals(), [(0, 8)])

    def test_bridge_many(self):
        self.assertEqual(mk((0, 2), (4, 6), (8, 10), (1, 9)).intervals(), [(0, 10)])

    def test_disjoint_sorted(self):
        self.assertEqual(mk((10, 12), (0, 2), (5, 6)).intervals(), [(0, 2), (5, 6), (10, 12)])

    def test_empty_add_noop(self):
        self.assertEqual(mk((3, 3)).intervals(), [])

    def test_bad_range(self):
        with self.assertRaises(ValueError):
            IntervalSet().add(5, 4)
        with self.assertRaises(ValueError):
            IntervalSet().remove(5, 4)

    def test_remove_split(self):
        s = mk((0, 10))
        s.remove(3, 5)
        self.assertEqual(s.intervals(), [(0, 3), (5, 10)])

    def test_remove_across(self):
        s = mk((0, 3), (5, 8), (10, 12))
        s.remove(2, 11)
        self.assertEqual(s.intervals(), [(0, 2), (11, 12)])

    def test_remove_exact_and_outside(self):
        s = mk((0, 3), (5, 8))
        s.remove(5, 8)
        s.remove(20, 30)
        self.assertEqual(s.intervals(), [(0, 3)])

    def test_contains_half_open(self):
        s = mk((0, 3), (5, 8))
        self.assertIn(0, s)
        self.assertIn(2, s)
        self.assertNotIn(3, s)
        self.assertNotIn(4, s)
        self.assertIn(7, s)
        self.assertNotIn(8, s)
        self.assertNotIn(-1, s)

    def test_len(self):
        s = mk((0, 3), (5, 8), (2, 6))
        self.assertEqual(len(s), 8)
        self.assertEqual(len(IntervalSet()), 0)

    def test_gaps(self):
        s = mk((2, 4), (6, 7))
        self.assertEqual(s.gaps(0, 10), [(0, 2), (4, 6), (7, 10)])
        self.assertEqual(s.gaps(3, 6), [(4, 6)])
        self.assertEqual(s.gaps(2, 4), [])
        self.assertEqual(IntervalSet().gaps(1, 1), [])
        with self.assertRaises(ValueError):
            s.gaps(5, 1)

    def test_fast_contains(self):
        s = IntervalSet()
        for i in range(100000):
            s.add(3 * i, 3 * i + 1)
        t = time.time()
        hits = sum(1 for x in range(0, 300000, 7) if x in s)
        self.assertLess(time.time() - t, 2.0)
        self.assertEqual(hits, sum(1 for x in range(0, 300000, 7) if x % 3 == 0))
