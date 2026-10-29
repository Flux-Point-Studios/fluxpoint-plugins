from collections import OrderedDict


def _pos_int(x):
    return isinstance(x, int) and not isinstance(x, bool) and x > 0


class Limiter:
    """Tokens are stored in units of 1/1000 token-ms: tokens * 1000."""

    def __init__(self, rate, burst, clock):
        if not _pos_int(rate) or not _pos_int(burst):
            raise ValueError("rate and burst must be positive ints")
        self.rate, self.burst, self.clock = rate, burst, clock
        self.cap = burst * 1000
        self.b = OrderedDict()  # key -> (milli_tokens, last_ms), ordered by last touch

    def _now(self, key):
        now = self.clock()
        if key in self.b:
            tok, last = self.b[key]
            if now > last:
                tok = min(self.cap, tok + self.rate * (now - last))
            last = now
            return tok, last, now
        return self.cap, now, now

    def _check_cost(self, cost):
        if not _pos_int(cost):
            raise ValueError("cost must be a positive int")

    def allow(self, key, cost=1):
        self._check_cost(cost)
        tok, last, now = self._now(key)
        ok = cost <= self.burst and tok >= cost * 1000
        if ok:
            tok -= cost * 1000
        if ok or key in self.b:
            self.b[key] = (tok, last)
            self.b.move_to_end(key)
        self._sweep(now, key)
        return ok

    def _sweep(self, now, keep):
        for k in list(self.b):
            tok, last = self.b[k]
            if k != keep and min(self.cap, tok + self.rate * max(0, now - last)) >= self.cap:
                del self.b[k]
        if keep in self.b and self.b[keep][0] >= self.cap:
            del self.b[keep]

    def retry_after(self, key, cost=1):
        self._check_cost(cost)
        if cost > self.burst:
            raise ValueError("cost above burst")
        tok, _, _ = self._now(key)
        need = cost * 1000 - tok
        return 0 if need <= 0 else -(-need // self.rate)

    def __len__(self):
        return len(self.b)
