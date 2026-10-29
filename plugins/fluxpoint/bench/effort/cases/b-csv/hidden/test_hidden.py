import unittest
from csvlite import parse


class Hidden(unittest.TestCase):
    def test_simple(self):
        self.assertEqual(parse("a,b\n1,2\n"), [["a", "b"], ["1", "2"]])

    def test_quoted_comma(self):
        self.assertEqual(parse('"x,y",z'), [["x,y", "z"]])

    def test_empty(self):
        self.assertEqual(parse(""), [])

    def test_no_trailing_newline(self):
        self.assertEqual(parse("a,b"), [["a", "b"]])

    def test_crlf(self):
        self.assertEqual(parse("a,b\r\nc,d\r\n"), [["a", "b"], ["c", "d"]])

    def test_lone_cr_literal(self):
        self.assertEqual(parse("a\rb,c\n"), [["a\rb", "c"]])

    def test_blank_middle_line(self):
        self.assertEqual(parse("a\n\nb\n"), [["a"], [""], ["b"]])

    def test_empty_fields(self):
        self.assertEqual(parse(",,\n"), [["", "", ""]])
        self.assertEqual(parse('"",x'), [["", "x"]])

    def test_escaped_quote(self):
        self.assertEqual(parse('"say ""hi""",2'), [['say "hi"', "2"]])

    def test_embedded_newlines(self):
        self.assertEqual(parse('"l1\nl2\r\nl3",b\nc,d'), [["l1\nl2\r\nl3", "b"], ["c", "d"]])

    def test_quote_inside_unquoted(self):
        self.assertEqual(parse('a"b,c'), [['a"b', "c"]])

    def test_garbage_after_quote(self):
        with self.assertRaisesRegex(ValueError, r"line 2\b"):
            parse('ok\n"bad"x,y\n')

    def test_garbage_after_multiline_quote(self):
        with self.assertRaisesRegex(ValueError, r"line 3\b"):
            parse('a\n"one\ntwo"z\n')

    def test_unterminated(self):
        with self.assertRaisesRegex(ValueError, r"line 2\b"):
            parse('a,b\n"open,\nmore\n')

    def test_ragged(self):
        self.assertEqual(parse("a\nb,c,d\n"), [["a"], ["b", "c", "d"]])
