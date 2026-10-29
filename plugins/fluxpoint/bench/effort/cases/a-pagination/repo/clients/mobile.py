import json


def parse_orders(body):
    return [o["id"] for o in json.loads(body)]
