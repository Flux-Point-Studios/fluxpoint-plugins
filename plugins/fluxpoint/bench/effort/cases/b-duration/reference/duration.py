from fractions import Fraction

UNITS = [("d", 86400000), ("h", 3600000), ("ms", 1), ("m", 60000), ("s", 1000)]
RANK = {"d": 0, "h": 1, "m": 2, "s": 3, "ms": 4}
SIZE = dict(UNITS)


def parse(text):
    if not isinstance(text, str):
        raise ValueError("not a string")
    i, n, neg = 0, len(text), False
    if text.startswith("-"):
        neg, i = True, 1
    if i >= n:
        raise ValueError("empty")
    total, last = Fraction(0), -1
    while i < n:
        j = i
        while j < n and text[j].isdigit() and text[j].isascii():
            j += 1
        intpart = text[i:j]
        if not intpart or (len(intpart) > 1 and intpart[0] == "0"):
            raise ValueError(text)
        frac = ""
        if j < n and text[j] == ".":
            k = j + 1
            while k < n and text[k].isdigit() and text[k].isascii():
                k += 1
            frac = text[j + 1:k]
            if not frac:
                raise ValueError(text)
            j = k
        for name, _ in UNITS:
            if text.startswith(name, j):
                unit = name
                break
        else:
            raise ValueError(text)
        if RANK[unit] <= last:
            raise ValueError(text)
        last = RANK[unit]
        value = Fraction(int(intpart)) + (Fraction(int(frac), 10 ** len(frac)) if frac else 0)
        total += value * SIZE[unit]
        i = j + len(unit)
    if total.denominator != 1:
        raise ValueError(text)
    v = int(total)
    return -v if neg else v


def format_ms(ms):
    if ms == 0:
        return "0s"
    sign, ms = ("-", -ms) if ms < 0 else ("", ms)
    out = []
    for name in ("d", "h", "m", "s", "ms"):
        q, ms = divmod(ms, SIZE[name])
        if q:
            out.append(f"{q}{name}")
    return sign + "".join(out)
