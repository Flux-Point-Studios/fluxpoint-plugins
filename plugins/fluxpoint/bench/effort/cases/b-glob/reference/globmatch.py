def _tokens(pattern):
    """Tokens: ('lit', c) ('any1',) ('star',) ('class', neg, items) ('gstar',) ('slash',)."""
    segs, cur, i, n = [], [], 0, len(pattern)
    out = []
    while i < n:
        c = pattern[i]
        if c == "\\":
            if i + 1 < n:
                out.append(("lit", pattern[i + 1])); i += 2
            else:
                out.append(("lit", "\\")); i += 1
        elif c == "*":
            j = i
            while j < n and pattern[j] == "*":
                j += 1
            seg_start = i == 0 or pattern[i - 1] == "/"
            seg_end = j == n or pattern[j] == "/"
            if j - i >= 2 and seg_start and seg_end and (i < 2 or pattern[i - 2] != "\\"):
                out.append(("gstar",))
            else:
                out.append(("star",))
            i = j
        elif c == "?":
            out.append(("any1",)); i += 1
        elif c == "[":
            j = i + 1
            neg = j < n and pattern[j] == "!"
            if neg:
                j += 1
            k = j + 1 if j < n and pattern[j] == "]" else j
            while k < n and pattern[k] != "]":
                k += 1
            if k >= n:
                out.append(("lit", "[")); i += 1
                continue
            body, items, m = pattern[j:k], [], 0
            while m < len(body):
                if m + 2 < len(body) and body[m + 1] == "-":
                    items.append((body[m], body[m + 2])); m += 3
                else:
                    items.append((body[m], body[m])); m += 1
            out.append(("class", neg, items)); i = k + 1
        else:
            out.append(("lit", c)); i += 1
    return out


def _one(tok, ch):
    if ch == "/":
        return tok == ("lit", "/")
    if tok[0] == "lit":
        return tok[1] == ch
    if tok[0] == "any1":
        return True
    if tok[0] == "class":
        hit = any(a <= ch <= b for a, b in tok[2])
        return hit != tok[1]
    return False


def match(pattern, path):
    toks = _tokens(pattern)
    # A gstar consumes zero or more whole segments together with one
    # adjacent slash. Normalize: "x/**/y" -> x, gstar_mid, y etc. We run an
    # NFA over (token index) states with epsilon closures.
    n = len(toks)

    def closure(states):
        stack, seen = list(states), set(states)
        while stack:
            s = stack.pop()
            nxt = []
            if s < n and toks[s][0] == "star":
                nxt.append(s + 1)
            if s < n and toks[s][0] == "gstar":
                # zero segments: skip gstar and the following slash (if any),
                # or, at end of pattern, require previous slash consumed: handled below
                if s + 1 < n and toks[s + 1] == ("lit", "/"):
                    nxt.append(s + 2)
            for t in nxt:
                if t not in seen:
                    seen.add(t); stack.append(t)
        return seen

    # state ("g", s) means inside a gstar at token s, mid-segment
    states = closure({0})
    gin = set()
    for ch in path:
        new, newg = set(), set()
        for s in states:
            if s >= n:
                continue
            t = toks[s]
            if t[0] == "star":
                if ch != "/":
                    new.add(s)
            elif t[0] == "gstar":
                if ch != "/":
                    newg.add(s)
            elif _one(t, ch):
                new.add(s + 1)
        for s in gin:
            if ch == "/":
                # finished one segment inside gstar; may continue or leave
                newg.add(("start", s))
            else:
                newg.add(s)
        realg = set()
        for g in newg:
            if isinstance(g, tuple):
                s = g[1]
                new.add(s)  # back at start of gstar (zero-or-more more segments)
                if s + 1 < n and toks[s + 1] == ("lit", "/"):
                    new.add(s + 2)
                elif s + 1 >= n:
                    pass
            else:
                realg.add(g)
        states, gin = closure(new), realg
        if not states and not gin:
            return False
    if n in states:
        return True
    # trailing gstar: matches one or more segments, so being inside it at end is fine
    return any(s == n - 1 for s in gin)
