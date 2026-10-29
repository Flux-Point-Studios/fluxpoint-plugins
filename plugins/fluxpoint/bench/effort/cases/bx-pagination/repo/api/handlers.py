import json
from api import orders


def get_orders(request):
    rows = orders.list_orders(request["account"])
    return 200, json.dumps([{"id": r[0], "created_at": r[1], "total": r[2]} for r in rows])
