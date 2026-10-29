def _int(x):
    return isinstance(x, int) and not isinstance(x, bool)


def split(amount, weights):
    if not _int(amount) or amount < 0 or not isinstance(weights, list) or not weights:
        raise ValueError("bad amount or weights")
    if not all(_int(w) and w >= 0 for w in weights):
        raise ValueError("bad weight")
    total = sum(weights)
    if total == 0:
        raise ValueError("all weights zero")
    out = [amount * w // total for w in weights]
    left = amount - sum(out)
    order = sorted((i for i, w in enumerate(weights) if w > 0),
                   key=lambda i: (-(amount * weights[i] % total), i))
    for i in order[:left]:
        out[i] += 1
    return out
