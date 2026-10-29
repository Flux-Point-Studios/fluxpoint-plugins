import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from billing.models import Invoice
from billing import codec, totals


class Hidden(unittest.TestCase):
    def test_default_usd(self):
        self.assertEqual(Invoice("a", "c", 5).currency, "USD")

    def test_currency_positional(self):
        self.assertEqual(Invoice("a", "c", 5, "EUR").currency, "EUR")

    def test_invalid_code(self):
        for bad in ("usd", "US", "EURO", "U$D", ""):
            with self.assertRaises(ValueError, msg=bad):
                Invoice("a", "c", 5, bad)

    def test_roundtrip(self):
        i = Invoice("a", "c", 5, "JPY")
        self.assertEqual(codec.loads(codec.dumps(i)), i)
        self.assertEqual(json.loads(codec.dumps(i))["currency"], "JPY")

    def test_legacy_loads_usd(self):
        self.assertEqual(codec.loads('{"id": "a", "customer": "c", "cents": 7}'),
                         Invoice("a", "c", 7, "USD"))

    def test_stored_invalid(self):
        with self.assertRaises(ValueError):
            codec.loads('{"id": "a", "customer": "c", "cents": 7, "currency": "eur"}')

    def test_total_same(self):
        self.assertEqual(totals.total_cents([Invoice("a", "c", 1, "EUR"), Invoice("b", "c", 2, "EUR")]), 3)
        self.assertEqual(totals.total_cents([]), 0)

    def test_total_mixed_raises(self):
        with self.assertRaises(ValueError):
            totals.total_cents([Invoice("a", "c", 1, "EUR"), Invoice("b", "c", 2)])

    def test_total_by_currency(self):
        inv = [Invoice("a", "c", 1, "EUR"), Invoice("b", "d", 2), Invoice("c", "c", 4, "EUR")]
        self.assertEqual(totals.total_by_currency(inv), {"EUR": 5, "USD": 2})

    def test_by_customer(self):
        inv = [Invoice("a", "c", 1, "EUR"), Invoice("b", "c", 2), Invoice("c", "c", 4, "EUR")]
        self.assertEqual(totals.by_customer(inv), {("c", "EUR"): 5, ("c", "USD"): 2})

    def test_cli(self):
        from billing import cli, store
        d = tempfile.mkdtemp()
        with mock.patch.object(store, "DIR", d):
            for i in (Invoice("a", "c", 3, "USD"), Invoice("b", "c", 4, "EUR"), Invoice("c", "d", 5, "USD")):
                store.save(i)
            with open(os.path.join(d, "old.json"), "w") as f:
                f.write('{"id": "old", "customer": "x", "cents": 10}')
            buf = io.StringIO()
            with redirect_stdout(buf):
                cli.main()
        self.assertEqual(buf.getvalue(), "EUR 4\nUSD 18\n")

    def test_cli_empty(self):
        from billing import cli, store
        d = tempfile.mkdtemp()
        with mock.patch.object(store, "DIR", d):
            buf = io.StringIO()
            with redirect_stdout(buf):
                cli.main()
        self.assertEqual(buf.getvalue(), "")
