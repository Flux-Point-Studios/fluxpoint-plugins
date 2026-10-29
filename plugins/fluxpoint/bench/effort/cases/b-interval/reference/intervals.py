import bisect


class IntervalSet:
    def __init__(self):
        self._s, self._e = [], []

    @staticmethod
    def _check(a, b):
        if a > b:
            raise ValueError("start > end")

    def add(self, a, b):
        self._check(a, b)
        if a == b:
            return
        i = bisect.bisect_left(self._e, a)
        j = bisect.bisect_right(self._s, b)
        if i < j:
            a = min(a, self._s[i])
            b = max(b, self._e[j - 1])
        self._s[i:j] = [a]
        self._e[i:j] = [b]

    def remove(self, a, b):
        self._check(a, b)
        if a == b:
            return
        i = bisect.bisect_right(self._e, a)
        j = bisect.bisect_left(self._s, b)
        if i >= j:
            return
        ns, ne = [], []
        if self._s[i] < a:
            ns.append(self._s[i]); ne.append(a)
        if self._e[j - 1] > b:
            ns.append(b); ne.append(self._e[j - 1])
        self._s[i:j] = ns
        self._e[i:j] = ne

    def intervals(self):
        return list(zip(self._s, self._e))

    def __contains__(self, x):
        i = bisect.bisect_right(self._s, x) - 1
        return i >= 0 and x < self._e[i]

    def __len__(self):
        return sum(e - s for s, e in zip(self._s, self._e))

    def gaps(self, lo, hi):
        self._check(lo, hi)
        out, cur = [], lo
        i = bisect.bisect_right(self._e, lo)
        while i < len(self._s) and self._s[i] < hi:
            if self._s[i] > cur:
                out.append((cur, self._s[i]))
            cur = max(cur, self._e[i])
            i += 1
        if cur < hi:
            out.append((cur, hi))
        return out
