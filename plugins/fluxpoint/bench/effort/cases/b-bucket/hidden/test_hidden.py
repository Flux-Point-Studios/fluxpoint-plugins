import unittest
from bucket import Limiter


class Clock:
    def __init__(self):
        self.t = 0

    def __call__(self):
        return self.t


class Hidden(unittest.TestCase):
    def setUp(self):
        self.c = Clock()

    def test_burst_then_block(self):
        lim = Limiter(1, 2, self.c)
        self.assertTrue(lim.allow("a"))
        self.assertTrue(lim.allow("a"))
        self.assertFalse(lim.allow("a"))

    def test_keys_independent(self):
        lim = Limiter(1, 1, self.c)
        self.assertTrue(lim.allow("a"))
        self.assertTrue(lim.allow("b"))
        self.assertFalse(lim.allow("a"))

    def test_refill(self):
        lim = Limiter(2, 2, self.c)
        lim.allow("a", 2)
        self.c.t = 499
        self.assertFalse(lim.allow("a"))
        self.c.t = 500
        self.assertTrue(lim.allow("a"))

    def test_cap(self):
        lim = Limiter(10, 3, self.c)
        lim.allow("a", 3)
        self.c.t = 100000
        self.assertTrue(lim.allow("a", 3))
        self.assertFalse(lim.allow("a"))

    def test_exact_accumulation(self):
        lim = Limiter(1, 1, self.c)
        self.assertTrue(lim.allow("a"))
        for _ in range(999):
            self.c.t += 1
            self.assertFalse(lim.allow("a"))
        self.c.t += 1
        self.assertTrue(lim.allow("a"))

    def test_odd_rate_exact(self):
        lim = Limiter(3, 3, self.c)
        lim.allow("a", 3)
        for _ in range(3):
            self.c.t += 111
        self.assertFalse(lim.allow("a"))  # 333 ms -> 0.999 tokens
        self.c.t += 1
        self.assertTrue(lim.allow("a"))

    def test_cost_over_burst(self):
        lim = Limiter(1, 2, self.c)
        self.assertFalse(lim.allow("a", 3))
        self.assertTrue(lim.allow("a", 2))
        with self.assertRaises(ValueError):
            lim.retry_after("a", 3)

    def test_failed_allow_spends_nothing(self):
        lim = Limiter(1, 3, self.c)
        lim.allow("a", 2)
        self.assertFalse(lim.allow("a", 2))
        self.assertTrue(lim.allow("a", 1))

    def test_bad_args(self):
        for args in ((0, 1), (1, 0), (1.5, 2), (1, -1), (True, 2)):
            with self.assertRaises(ValueError, msg=args):
                Limiter(args[0], args[1], self.c)
        lim = Limiter(1, 1, self.c)
        for cost in (0, -1, 1.0, True):
            with self.assertRaises(ValueError, msg=cost):
                lim.allow("a", cost)

    def test_clock_backwards(self):
        lim = Limiter(1, 2, self.c)
        self.c.t = 10000
        lim.allow("a", 2)
        self.c.t = 10500
        self.c.t = 5000
        self.assertFalse(lim.allow("a"))
        self.c.t = 6000
        self.assertTrue(lim.allow("a"))

    def test_clock_backwards_keeps_earned(self):
        lim = Limiter(1, 2, self.c)
        lim.allow("a", 2)
        self.c.t = 1000
        self.assertFalse(lim.allow("a", 2))  # earns 1 token
        self.c.t = 0
        self.assertTrue(lim.allow("a"))

    def test_retry_after(self):
        lim = Limiter(3, 3, self.c)
        self.assertEqual(lim.retry_after("a"), 0)
        lim.allow("a", 3)
        self.assertEqual(lim.retry_after("a"), 334)
        self.assertEqual(lim.retry_after("a", 2), 667)
        self.c.t = 334
        self.assertTrue(lim.allow("a"))

    def test_bounded_memory(self):
        lim = Limiter(1000, 1, self.c)
        for i in range(1000):
            lim.allow(f"k{i}")
        self.c.t += 1
        lim.allow("fresh")
        self.assertLessEqual(len(lim), 1)
