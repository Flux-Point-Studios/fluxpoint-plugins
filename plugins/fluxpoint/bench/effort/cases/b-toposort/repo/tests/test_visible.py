import unittest
from topo import order, CycleError


class Visible(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(order({"b": ["a"], "c": ["b"]}), ["a", "b", "c"])

    def test_cycle(self):
        with self.assertRaises(CycleError):
            order({"a": ["b"], "b": ["a"]})
