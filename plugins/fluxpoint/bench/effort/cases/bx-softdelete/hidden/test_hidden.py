import json
import sqlite3
import unittest

from docs import export, jobs, repo, search, views

DAY = 86400
OLD_SCHEMA = "CREATE TABLE documents (id INTEGER PRIMARY KEY, owner TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL);"


def fresh():
    db = sqlite3.connect(":memory:", isolation_level=None)
    db.executescript(open("docs/schema.sql").read())
    for i, (owner, title, body) in enumerate([("u", "one", "alpha"), ("u", "two", "alpha beta"),
                                              ("v", "three", "alpha")], 1):
        db.execute("INSERT INTO documents (id, owner, title, body) VALUES (?, ?, ?, ?)",
                   (i, owner, title, body))
    return db


class Hidden(unittest.TestCase):
    def test_delete_hides_get(self):
        db = fresh()
        repo.delete(db, 1, 1000)
        self.assertIsNone(repo.get(db, 1))
        self.assertEqual(db.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 3)

    def test_get_shape(self):
        db = fresh()
        self.assertEqual(tuple(repo.get(db, 1)), (1, "u", "one", "alpha"))

    def test_list_for(self):
        db = fresh()
        repo.delete(db, 1, 1000)
        self.assertEqual([tuple(r) for r in repo.list_for(db, "u")], [(2, "two")])

    def test_search(self):
        db = fresh()
        repo.delete(db, 2, 1000)
        self.assertEqual([tuple(r) for r in search.search(db, "u", "alpha")], [(1, "one")])

    def test_export(self):
        db = fresh()
        repo.delete(db, 1, 1000)
        self.assertEqual(json.loads(export.export_owner(db, "u")), [{"id": 2, "title": "two", "body": "alpha beta"}])

    def test_view_404(self):
        db = fresh()
        repo.delete(db, 1, 1000)
        self.assertEqual(views.show(db, "u", 1), (404, None))
        self.assertEqual(views.show(db, "u", 2)[0], 200)

    def test_double_delete_keeps_first(self):
        db = fresh()
        repo.delete(db, 1, 1000)
        repo.delete(db, 1, 5000)
        self.assertEqual(db.execute("SELECT deleted_at FROM documents WHERE id = 1").fetchone()[0], 1000)

    def test_restore(self):
        db = fresh()
        repo.delete(db, 1, 1000)
        self.assertTrue(repo.restore(db, 1))
        self.assertEqual(repo.get(db, 1)[2], "one")
        self.assertFalse(repo.restore(db, 1))
        self.assertFalse(repo.restore(db, 99))

    def test_purge_boundary(self):
        db = fresh()
        repo.delete(db, 1, 0)
        repo.delete(db, 2, 1)
        jobs.nightly(db, 30 * DAY + 1)
        ids = [r[0] for r in db.execute("SELECT id FROM documents ORDER BY id")]
        self.assertEqual(ids, [2, 3])

    def test_purge_keeps_live(self):
        db = fresh()
        jobs.nightly(db, 10 ** 10)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 3)

    def test_migrate(self):
        db = sqlite3.connect(":memory:", isolation_level=None)
        db.executescript(OLD_SCHEMA)
        db.execute("INSERT INTO documents VALUES (1, 'u', 't', 'b')")
        repo.migrate(db)
        repo.migrate(db)
        self.assertEqual(tuple(repo.get(db, 1)), (1, "u", "t", "b"))
        repo.delete(db, 1, 10)
        self.assertIsNone(repo.get(db, 1))
