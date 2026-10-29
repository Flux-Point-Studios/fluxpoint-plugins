import base64
import hashlib
import hmac
import json

from api import config, orders


def _shape(rows):
    return [{"id": r[0], "created_at": r[1], "total": r[2]} for r in rows]


def _sign(payload):
    return hmac.new(config.CURSOR_KEY, payload, hashlib.sha256).hexdigest()


def encode_cursor(account, created_at, order_id):
    payload = json.dumps([account, created_at, order_id]).encode()
    return base64.urlsafe_b64encode(payload).decode() + "." + _sign(payload)


def decode_cursor(account, cursor):
    try:
        b64, sig = cursor.rsplit(".", 1)
        payload = base64.urlsafe_b64decode(b64.encode())
        if not hmac.compare_digest(_sign(payload), sig):
            return None
        acct, created_at, order_id = json.loads(payload)
    except (ValueError, TypeError):
        return None
    if acct != account or not isinstance(order_id, int) or not isinstance(created_at, str):
        return None
    return created_at, order_id


def _err(what):
    return 400, json.dumps({"error": what})


def get_orders(request):
    account = request["account"]
    if request.get("headers", {}).get("Accept-Version") != "2":
        return 200, json.dumps(_shape(orders.list_orders(account)))
    query = request.get("query") or {}
    raw = query.get("limit", "20")
    if not (raw.isascii() and raw.isdigit()) or not 1 <= int(raw) <= 100:
        return _err("limit")
    limit = int(raw)
    after = None
    if "cursor" in query:
        after = decode_cursor(account, query["cursor"])
        if after is None:
            return _err("cursor")
    rows = orders.page(account, limit + 1, after)
    more, rows = len(rows) > limit, rows[:limit]
    nxt = encode_cursor(account, rows[-1][1], rows[-1][0]) if more else None
    return 200, json.dumps({"orders": _shape(rows), "next": nxt})
