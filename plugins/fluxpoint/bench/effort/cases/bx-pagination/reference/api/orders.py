from api import db

COLS = "id, created_at, total"


def list_orders(account):
    return db.query(f"SELECT {COLS} FROM orders WHERE account = ? ORDER BY created_at DESC, id DESC", (account,))


def page(account, limit, after=None):
    """`after` is (created_at, id) of the last order already returned."""
    if after is None:
        return db.query(f"SELECT {COLS} FROM orders WHERE account = ? ORDER BY created_at DESC, id DESC LIMIT ?",
                        (account, limit))
    return db.query(f"SELECT {COLS} FROM orders WHERE account = ? AND (created_at < ? OR (created_at = ? AND id < ?)) "
                    "ORDER BY created_at DESC, id DESC LIMIT ?", (account, after[0], after[0], after[1], limit))
