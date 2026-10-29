import unittest
from duration import parse, format_ms


class Hidden(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(parse("1h30m"), 5400000)
        self.assertEqual(parse("2d"), 172800000)
        self.assertEqual(parse("250ms"), 250)

    def test_ms_vs_m(self):
        self.assertEqual(parse("1m5ms"), 60005)
        self.assertEqual(parse("1s5ms"), 1005)

    def test_fractions_exact(self):
        self.assertEqual(parse("1.5h"), 5400000)
        self.assertEqual(parse("0.1s"), 100)
        self.assertEqual(parse("0.001s"), 1)
        self.assertEqual(parse("1.25m"), 75000)

    def test_fraction_not_whole_ms(self):
        for t in ("1.0005s", "0.5ms", "0.00001m"):
            with self.assertRaises(ValueError, msg=t):
                parse(t)

    def test_big_exact(self):
        self.assertEqual(parse("100000000000d"), 100000000000 * 86400000)
        self.assertEqual(parse("1d1ms"), 86400001)

    def test_negative(self):
        self.assertEqual(parse("-1h"), -3600000)
        self.assertEqual(parse("-1h30m"), -5400000)
        self.assertEqual(parse("-0s"), 0)

    def test_order_and_repeat(self):
        for t in ("30m1h", "1h1h", "1ms1s", "1s1s"):
            with self.assertRaises(ValueError, msg=t):
                parse(t)

    def test_number_shape(self):
        for t in (".5h", "5.h", "05m", "+1h", "1 h", "1h 30m", "1e3ms", "1,5h"):
            with self.assertRaises(ValueError, msg=t):
                parse(t)
        self.assertEqual(parse("0.5m"), 30000)
        self.assertEqual(parse("0m"), 0)

    def test_garbage(self):
        for t in ("", "-", "5", "h", "1y", "1hh", "--1h", "1h-30m", "1H"):
            with self.assertRaises(ValueError, msg=t):
                parse(t)

    def test_format(self):
        self.assertEqual(format_ms(5400000), "1h30m")
        self.assertEqual(format_ms(90061001), "1d1h1m1s1ms")
        self.assertEqual(format_ms(0), "0s")
        self.assertEqual(format_ms(-60005), "-1m5ms")
        self.assertEqual(format_ms(86400000 * 3 + 2), "3d2ms")

    def test_roundtrip(self):
        for ms in (0, 1, 999, 1000, 59999, 60000, 3599999, 86400000, -1, -86399999,
                   10 ** 15 + 7):
            self.assertEqual(parse(format_ms(ms)), ms)
