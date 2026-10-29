# Cursor pagination for GET /orders

`handlers.get_orders(request)` returns every order. Add cursor pagination,
while the released mobile client (`clients/mobile.py`, which must not be
changed) keeps working.

- `request` is a dict with `account`, `headers` (dict) and `query` (dict of
  str to str, possibly missing). Without the header `Accept-Version: 2`
  the response is exactly what it is today: status 200 and a JSON bare list
  of every order.
- With `Accept-Version: 2` the body is JSON `{"orders": [...], "next":
  cursor-or-null}`. Orders are newest first by `created_at`, and orders
  with the same `created_at` are ordered by `id` descending. Each order
  object keeps today's shape.
- `query["limit"]` is an integer string from 1 to 100 (default 20).
  Anything else returns status 400 with body `{"error": "limit"}`.
- `next` is an opaque string, or null when there are no more orders.
  Passing it back as `query["cursor"]` returns the following page. Walking
  the pages returns every order exactly once even if new orders are
  inserted between page requests (new orders newer than the first page may
  be missed; none may be duplicated or skipped among the rest).
- A cursor that was not issued by the server (malformed, edited, or issued
  for a different account) returns status 400 with body
  `{"error": "cursor"}`. Cursors are signed with HMAC-SHA256 using
  `api.config.CURSOR_KEY` (bytes); create that module.
- `orders.list_orders(account)` must keep returning every order newest
  first, as it does today.
