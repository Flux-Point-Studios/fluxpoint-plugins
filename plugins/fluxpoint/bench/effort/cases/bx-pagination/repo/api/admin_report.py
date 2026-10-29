from api import db


def revenue():
    return db.query("SELECT SUM(total) FROM orders")[0][0] or 0
