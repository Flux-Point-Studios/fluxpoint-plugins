import sqlite3

CONN = sqlite3.connect(":memory:", check_same_thread=False)


def query(sql, args=()):
    return CONN.execute(sql, args).fetchall()
