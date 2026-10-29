import time
import unittest
from globmatch import match


class Hidden(unittest.TestCase):
    def test_star(self):
        self.assertTrue(match("src/*.py", "src/app.py"))
        self.assertFalse(match("src/*.py", "src/a/app.py"))
        self.assertTrue(match("*", ""))
        self.assertTrue(match("a*", "a"))

    def test_question(self):
        self.assertTrue(match("a?c", "abc"))
        self.assertFalse(match("a?c", "a/c"))
        self.assertFalse(match("a?c", "ac"))

    def test_globstar_middle(self):
        for p in ("a/b", "a/x/b", "a/x/y/b"):
            self.assertTrue(match("a/**/b", p), p)
        self.assertFalse(match("a/**/b", "a/xb"))
        self.assertFalse(match("a/**/b", "ab"))

    def test_globstar_ends(self):
        self.assertTrue(match("**/c", "c"))
        self.assertTrue(match("**/c", "x/y/c"))
        self.assertTrue(match("a/**", "a/x"))
        self.assertTrue(match("a/**", "a/x/y"))
        self.assertFalse(match("a/**", "a"))
        self.assertTrue(match("**", "a/b/c"))

    def test_globstar_not_segment(self):
        self.assertTrue(match("a**b", "axxb"))
        self.assertFalse(match("a**b", "ax/xb"))

    def test_globstar_then_star(self):
        self.assertTrue(match("src/**/*.py", "src/app.py"))
        self.assertTrue(match("src/**/*.py", "src/a/b/app.py"))
        self.assertFalse(match("src/**/*.py", "src/a/b/app.pyc"))

    def test_classes(self):
        self.assertTrue(match("[abc].txt", "b.txt"))
        self.assertFalse(match("[abc].txt", "d.txt"))
        self.assertTrue(match("[a-c][0-9]", "b7"))
        self.assertTrue(match("[!a-c]x", "dx"))
        self.assertFalse(match("[!a-c]x", "bx"))
        self.assertFalse(match("a[!x]b", "a/b"))
        self.assertFalse(match("a[/]b", "a/b"))

    def test_class_literal_bracket(self):
        self.assertTrue(match("[]a]", "]"))
        self.assertTrue(match("[!]a]", "b"))
        self.assertFalse(match("[!]a]", "]"))

    def test_unclosed_class_literal(self):
        self.assertTrue(match("a[b", "a[b"))
        self.assertFalse(match("a[b", "ab"))

    def test_escapes(self):
        self.assertTrue(match(r"a\*b", "a*b"))
        self.assertFalse(match(r"a\*b", "axb"))
        self.assertTrue(match(r"\[x]", "[x]"))
        self.assertTrue(match("a\\\\b", "a\\b"))
        self.assertTrue(match("a\\", "a\\"))
        self.assertTrue(match(r"\?", "?"))

    def test_case_sensitive(self):
        self.assertFalse(match("*.PY", "a.py"))

    def test_no_exponential(self):
        t = time.time()
        self.assertFalse(match("*a" * 30, "a" * 200 + "b"))
        self.assertFalse(match("**/" * 15 + "z", "/".join("a" * 40)))
        self.assertLess(time.time() - t, 1.0)
