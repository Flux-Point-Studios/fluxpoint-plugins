import unittest
from bucket import Limiter


class Clock:
    def __init__(self):
        self.t = 0

    def __call__(self):
        return self.t


class Visible(unittest.TestCase):
    def test_burst_then_block(self):
        c = Clock()
        lim = Limiter(1, 2, c)
        self.assertTrue(lim.allow("a"))
        self.assertTrue(lim.allow("a"))
        self.assertFalse(lim.allow("a"))
