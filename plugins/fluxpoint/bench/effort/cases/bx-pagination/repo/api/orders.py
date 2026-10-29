from api import db


def list_orders(account):
    return db.query("SELECT id, created_at, total FROM orders WHERE account = ? ORDER BY created_at DESC", (account,))
