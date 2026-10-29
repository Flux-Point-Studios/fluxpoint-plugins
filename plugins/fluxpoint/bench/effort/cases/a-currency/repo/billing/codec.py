import json
from billing.models import Invoice


def dumps(inv):
    return json.dumps({"id": inv.id, "customer": inv.customer, "cents": inv.cents})


def loads(text):
    d = json.loads(text)
    return Invoice(d["id"], d["customer"], d["cents"])
