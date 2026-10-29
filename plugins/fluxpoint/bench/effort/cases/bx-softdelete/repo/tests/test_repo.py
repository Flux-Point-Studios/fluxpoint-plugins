import sqlite3
import unittest
from docs import repo


class T(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.executescript(open("docs/schema.sql").read())

    def test_delete(self):
        self.db.execute("INSERT INTO documents VALUES (1, 'u', 't', 'b')")
        repo.delete(self.db, 1)
        self.assertIsNone(repo.get(self.db, 1))
