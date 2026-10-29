import json
from billing.models import Invoice


def dumps(inv):
    return json.dumps({"id": inv.id, "customer": inv.customer, "cents": inv.cents,
                       "currency": inv.currency})


def loads(text):
    d = json.loads(text)
    return Invoice(d["id"], d["customer"], d["cents"], d.get("currency", "USD"))
