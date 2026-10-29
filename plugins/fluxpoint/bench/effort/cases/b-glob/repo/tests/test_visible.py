import unittest
from globmatch import match


class Visible(unittest.TestCase):
    def test_star(self):
        self.assertTrue(match("src/*.py", "src/app.py"))
        self.assertFalse(match("src/*.py", "src/a/app.py"))

    def test_globstar(self):
        self.assertTrue(match("src/**/*.py", "src/a/b/app.py"))
