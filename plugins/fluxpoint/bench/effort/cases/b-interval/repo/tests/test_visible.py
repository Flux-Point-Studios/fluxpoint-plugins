import unittest
from intervals import IntervalSet


class Visible(unittest.TestCase):
    def test_add_merge(self):
        s = IntervalSet()
        s.add(0, 5)
        s.add(3, 8)
        self.assertEqual(s.intervals(), [(0, 8)])

    def test_remove_split(self):
        s = IntervalSet()
        s.add(0, 10)
        s.remove(3, 5)
        self.assertEqual(s.intervals(), [(0, 3), (5, 10)])
