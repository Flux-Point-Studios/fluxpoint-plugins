import os
from billing import codec

DIR = os.environ.get("BILLING_DIR", "/var/lib/billing")


def save(inv):
    with open(os.path.join(DIR, inv.id + ".json"), "w") as f:
        f.write(codec.dumps(inv))


def load_all():
    return [codec.loads(open(os.path.join(DIR, n)).read())
            for n in sorted(os.listdir(DIR)) if n.endswith(".json")]
