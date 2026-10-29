import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from users import bulk, service, store

ADMIN = {"role": "admin", "email": "root@x"}


def fresh():
    db = sqlite3.connect(":memory:")
    db.executescript(open("users/schema.sql").read())
    db.execute("INSERT INTO users VALUES (1, 'a@x', 'viewer')")
    db.execute("INSERT INTO users VALUES (2, 'b@x', 'editor')")
    db.commit()
    return db


def csvfile(text):
    fd, p = tempfile.mkstemp(suffix=".csv")
    os.write(fd, text.encode())
    os.close(fd)
    return p


class Hidden(unittest.TestCase):
    def test_set_role_audits(self):
        db = fresh()
        service.set_role(db, ADMIN, 1, "editor", 100)
        self.assertEqual(store.get_role(db, 1), "editor")
        self.assertEqual(store.audit_for(db, 1), [("root@x", "viewer", "editor", 100)])

    def test_same_role_no_audit(self):
        db = fresh()
        service.set_role(db, ADMIN, 2, "editor", 100)
        self.assertEqual(store.audit_for(db, 2), [])

    def test_non_admin_rejected(self):
        db = fresh()
        with self.assertRaises(PermissionError):
            service.set_role(db, {"role": "viewer", "email": "v@x"}, 1, "admin", 1)
        self.assertEqual(store.audit_for(db, 1), [])

    def test_unknown_user(self):
        db = fresh()
        with self.assertRaises(KeyError):
            service.set_role(db, ADMIN, 99, "admin", 1)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM role_audit").fetchone()[0], 0)

    def test_audit_failure_rolls_back_update(self):
        db = fresh()
        real = db.execute

        class Boom:
            def __init__(self, inner):
                self.inner = inner

            def execute(self, sql, *a):
                if "role_audit" in sql and sql.lstrip().upper().startswith("INSERT"):
                    raise sqlite3.OperationalError("disk full")
                return self.inner.execute(sql, *a)

            def __getattr__(self, n):
                return getattr(self.inner, n)

        with self.assertRaises(sqlite3.OperationalError):
            service.set_role(Boom(db), ADMIN, 1, "admin", 5)
        db.rollback()
        self.assertEqual(store.get_role(db, 1), "viewer")

    def test_notify_after_commit_only(self):
        db = fresh()
        with mock.patch("users.notify.role_changed") as n:
            service.set_role(db, ADMIN, 1, "admin", 5)
            service.set_role(db, ADMIN, 1, "admin", 6)
        self.assertEqual(n.call_count, 1)

    def test_bulk_audits_each(self):
        db = fresh()
        bulk.import_roles(db, ADMIN, csvfile("1,admin\n2,viewer\n"), 50)
        self.assertEqual(store.audit_for(db, 1), [("root@x", "viewer", "admin", 50)])
        self.assertEqual(store.audit_for(db, 2), [("root@x", "editor", "viewer", 50)])

    def test_bulk_chain_old_role(self):
        db = fresh()
        bulk.import_roles(db, ADMIN, csvfile("1,editor\n1,admin\n"), 50)
        self.assertEqual(store.audit_for(db, 1),
                         [("root@x", "viewer", "editor", 50), ("root@x", "editor", "admin", 50)])

    def test_bulk_all_or_nothing(self):
        for bad in ("1,admin\n99,admin\n", "1,admin\nx,admin\n", "1,admin\n2\n"):
            db = fresh()
            with self.assertRaises(Exception, msg=bad):
                bulk.import_roles(db, ADMIN, csvfile(bad), 50)
            self.assertEqual(store.get_role(db, 1), "viewer", bad)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM role_audit").fetchone()[0], 0, bad)

    def test_bulk_notifies_after_commit(self):
        db = fresh()
        with mock.patch("users.notify.role_changed") as n:
            with self.assertRaises(Exception):
                bulk.import_roles(db, ADMIN, csvfile("1,admin\n99,admin\n"), 50)
        self.assertEqual(n.call_count, 0)

    def test_migrate_existing(self):
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT, role TEXT)")
        db.execute("INSERT INTO users VALUES (1, 'a@x', 'viewer')")
        db.commit()
        store.migrate(db)
        store.migrate(db)
        service.set_role(db, ADMIN, 1, "admin", 9)
        self.assertEqual(store.audit_for(db, 1), [("root@x", "viewer", "admin", 9)])
