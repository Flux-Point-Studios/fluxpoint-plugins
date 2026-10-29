import unittest
from topo import order, CycleError


class Hidden(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(order({"b": ["a"], "c": ["b"]}), ["a", "b", "c"])

    def test_lexicographic_ready(self):
        self.assertEqual(order({"z": [], "y": [], "a": ["z"]}), ["y", "z", "a"])

    def test_implicit_nodes(self):
        self.assertEqual(order({"app": ["lib", "log"], "lib": ["log"]}), ["log", "lib", "app"])

    def test_duplicates(self):
        self.assertEqual(order({"b": ["a", "a"], "a": []}), ["a", "b"])

    def test_empty(self):
        self.assertEqual(order({}), [])

    def test_cycle_is_valueerror(self):
        with self.assertRaises(ValueError):
            order({"a": ["b"], "b": ["a"]})

    def test_cycle_rotation(self):
        with self.assertRaises(CycleError) as cm:
            order({"c": ["a"], "a": ["b"], "b": ["c"]})
        self.assertEqual(cm.exception.cycle, ["a", "b", "c", "a"])

    def test_self_loop(self):
        with self.assertRaises(CycleError) as cm:
            order({"x": ["x"], "a": []})
        self.assertEqual(cm.exception.cycle, ["x", "x"])

    def test_cycle_direction(self):
        # m depends on n, n depends on m: the list follows dependency edges
        with self.assertRaises(CycleError) as cm:
            order({"n": ["m"], "m": ["n"], "q": ["m"]})
        self.assertEqual(cm.exception.cycle, ["m", "n", "m"])

    def test_smallest_cycle_reported(self):
        deps = {"p": ["q"], "q": ["p"], "d": ["e"], "e": ["f"], "f": ["d"], "a": ["d"]}
        with self.assertRaises(CycleError) as cm:
            order(deps)
        self.assertEqual(cm.exception.cycle, ["d", "e", "f", "d"])

    def test_two_cycles_same_min(self):
        deps = {"a": ["c", "b"], "b": ["a"], "c": ["a"]}
        with self.assertRaises(CycleError) as cm:
            order(deps)
        self.assertEqual(cm.exception.cycle, ["a", "b", "a"])

    def test_long_chain(self):
        n = 100000
        deps = {f"n{i:06d}": [f"n{i - 1:06d}"] for i in range(1, n)}
        out = order(deps)
        self.assertEqual(len(out), n)
        self.assertEqual(out[0], "n000000")
        self.assertEqual(out[-1], f"n{n - 1:06d}")

    def test_long_cycle(self):
        n = 50000
        deps = {f"n{i:06d}": [f"n{(i + 1) % n:06d}"] for i in range(n)}
        with self.assertRaises(CycleError) as cm:
            order(deps)
        self.assertEqual(len(cm.exception.cycle), n + 1)
        self.assertEqual(cm.exception.cycle[0], "n000000")
