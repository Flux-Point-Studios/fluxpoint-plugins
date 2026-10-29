import unittest
from billing.models import Invoice
from billing.totals import total_cents


class T(unittest.TestCase):
    def test_sum(self):
        self.assertEqual(total_cents([Invoice("a", "c", 1), Invoice("b", "c", 2)]), 3)
