import sqlite3


def connect(url):
    return sqlite3.connect(url.removeprefix("sqlite:///"))


def run_batch(db, n):
    db.execute("SELECT 1")
