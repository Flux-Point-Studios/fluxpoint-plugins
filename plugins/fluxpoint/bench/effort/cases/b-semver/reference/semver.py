import re

NUM = r"(0|[1-9]\d*)"
IDENT = r"[0-9A-Za-z-]+"
VERSION = re.compile(rf"^{NUM}\.{NUM}\.{NUM}(?:-({IDENT}(?:\.{IDENT})*))?(?:\+{IDENT}(?:\.{IDENT})*)?$")
PARTIAL = re.compile(rf"^(?:{NUM}|[xX*])(?:\.(?:{NUM}|[xX*]))?(?:\.(?:{NUM}|[xX*]))?$")


def parse(v):
    m = VERSION.match(v)
    if not m:
        raise ValueError(v)
    pre = tuple(m.group(4).split(".")) if m.group(4) else ()
    for p in pre:
        if p.isdigit() and len(p) > 1 and p[0] == "0":
            raise ValueError(v)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))), pre


def key(v):
    core, pre = v
    if not pre:
        return core, (1,)
    ids = tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in pre)
    return core, (0, ids)


def cmp(a, b):
    ka, kb = key(a), key(b)
    return (ka > kb) - (ka < kb)


def expand(tok):
    """A comparator token -> list of (op, version)."""
    if tok in ("", "*", "x", "X"):
        return []
    m = re.match(r"^(>=|<=|>|<|=|\^|~)?(.*)$", tok)
    op, rest = m.group(1) or "", m.group(2)
    if op in (">=", "<=", ">", "<", "=") or (op == "" and VERSION.match(rest)):
        return [(op or "=", parse(rest))]
    if op in ("^", "~"):
        v = parse(rest)
        (M, m_, p), pre = v
        if op == "~":
            hi = (M, m_ + 1, 0)
        elif M:
            hi = (M + 1, 0, 0)
        elif m_:
            hi = (0, m_ + 1, 0)
        else:
            hi = (0, 0, p + 1)
        return [(">=", v), ("<", (hi, ()))]
    if PARTIAL.match(rest) and op == "":
        parts = rest.split(".")
        nums = []
        for p in parts:
            if p in ("x", "X", "*"):
                break
            nums.append(int(p))
        if len(nums) == 3:
            return [("=", (tuple(nums), ()))]
        if not nums:
            return []
        if len(nums) == 1:
            return [(">=", ((nums[0], 0, 0), ())), ("<", ((nums[0] + 1, 0, 0), ()))]
        return [(">=", ((nums[0], nums[1], 0), ())), ("<", ((nums[0], nums[1] + 1, 0), ()))]
    raise ValueError(tok)


def test(op, v, c):
    r = cmp(v, c)
    return {"=": r == 0, ">": r > 0, ">=": r >= 0, "<": r < 0, "<=": r <= 0}[op]


def satisfies(version, range_):
    v = parse(version)
    sets = [s.split() for s in range_.split("||")]
    parsed = [[c for tok in toks for c in expand(tok)] for toks in sets]
    for comps in parsed:
        if not all(test(op, v, c) for op, c in comps):
            continue
        if v[1] and not any(c[1] and c[0] == v[0] for _, c in comps):
            continue
        return True
    return False
