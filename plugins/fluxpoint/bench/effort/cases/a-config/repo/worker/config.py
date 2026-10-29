import tomllib

DEFAULTS = {"batch_size": 100, "poll_seconds": 5, "db": {"url": "sqlite:///w.db"}}


def load(path):
    with open(path, "rb") as f:
        data = tomllib.load(f)
    cfg = {**DEFAULTS, **data}
    if cfg["batch_size"] <= 0:
        raise ValueError("batch_size")
    return cfg
