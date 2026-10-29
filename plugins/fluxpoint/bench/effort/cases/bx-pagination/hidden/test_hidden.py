import base64
import json
import unittest

from api import db, handlers, orders


def setup(rows):
    db.CONN.execute("DROP TABLE IF EXISTS orders")
    db.CONN.executescript(open("api/schema.sql").read())
    db.CONN.executemany("INSERT INTO orders (id, account, created_at, total) VALUES (?, ?, ?, ?)", rows)
    db.CONN.commit()


def get(account, v2=True, **query):
    headers = {"Accept-Version": "2"} if v2 else {}
    status, body = handlers.get_orders({"account": account, "headers": headers, "query": query})
    return status, json.loads(body)


def walk(account, limit):
    seen, cursor, pages = [], None, 0
    while True:
        q = {"limit": str(limit)}
        if cursor:
            q["cursor"] = cursor
        status, body = get(account, **q)
        assert status == 200, body
        seen += [o["id"] for o in body["orders"]]
        cursor = body["next"]
        pages += 1
        if not cursor or pages > 100:
            return seen


ROWS = [(i, "a", f"2026-01-{(i % 5) + 1:02d}", i * 10) for i in range(1, 24)] + [(100, "b", "2026-01-01", 1)]


class Hidden(unittest.TestCase):
    def setUp(self):
        setup(ROWS)

    def expected(self, account="a"):
        rows = [r for r in ROWS if r[1] == account]
        return [r[0] for r in sorted(rows, key=lambda r: (r[2], r[0]), reverse=True)]

    def test_v1_bare_list(self):
        status, body = get("a", v2=False)
        self.assertEqual(status, 200)
        self.assertIsInstance(body, list)
        self.assertEqual(len(body), 23)
        self.assertEqual(set(body[0]), {"id", "created_at", "total"})

    def test_mobile_client_still_parses(self):
        from clients.mobile import parse_orders
        status, body = handlers.get_orders({"account": "a", "headers": {}})
        self.assertEqual(len(parse_orders(body)), 23)

    def test_list_orders_unchanged(self):
        self.assertEqual(len(orders.list_orders("a")), 23)

    def test_first_page_order(self):
        status, body = get("a", limit="5")
        self.assertEqual([o["id"] for o in body["orders"]], self.expected()[:5])
        self.assertIsNotNone(body["next"])

    def test_default_limit(self):
        status, body = get("a")
        self.assertEqual(len(body["orders"]), 20)

    def test_walk_all_ties(self):
        for limit in (1, 3, 4, 7, 23, 100):
            self.assertEqual(walk("a", limit), self.expected(), limit)

    def test_last_page_next_null(self):
        status, body = get("a", limit="100")
        self.assertIsNone(body["next"])

    def test_bad_limit(self):
        for bad in ("0", "101", "-1", "abc", "5.5", ""):
            status, body = get("a", limit=bad)
            self.assertEqual((status, body), (400, {"error": "limit"}), bad)

    def test_inserts_between_pages(self):
        status, body = get("a", limit="5")
        first = [o["id"] for o in body["orders"]]
        db.CONN.execute("INSERT INTO orders VALUES (500, 'a', '2026-01-09', 1)")
        db.CONN.execute("INSERT INTO orders VALUES (501, 'a', '2026-01-03', 1)")
        db.CONN.commit()
        rest, cursor = [], body["next"]
        while cursor:
            status, body = get("a", limit="5", cursor=cursor)
            rest += [o["id"] for o in body["orders"]]
            cursor = body["next"]
        ids = first + rest
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(set(self.expected()) <= set(ids))

    def test_tampered_cursor(self):
        status, body = get("a", limit="5")
        c = body["next"]
        flipped = c[:-2] + ("A" if c[-2] != "A" else "B") + c[-1]
        for bad in ("garbage", flipped, "", base64.urlsafe_b64encode(b'{"id": 1}').decode()):
            status, body = get("a", limit="5", cursor=bad)
            self.assertEqual((status, body), (400, {"error": "cursor"}), bad)

    def test_cursor_other_account(self):
        status, body = get("a", limit="5")
        status, body = get("b", limit="5", cursor=body["next"])
        self.assertEqual((status, body), (400, {"error": "cursor"}))
