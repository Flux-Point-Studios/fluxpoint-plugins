import unittest
from billing.models import Invoice
from billing import codec


class T(unittest.TestCase):
    def test_roundtrip(self):
        i = Invoice("a", "c", 5)
        self.assertEqual(codec.loads(codec.dumps(i)), i)
