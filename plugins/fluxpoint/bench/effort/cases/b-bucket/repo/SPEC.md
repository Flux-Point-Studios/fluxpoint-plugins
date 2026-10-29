# Limiter(rate, burst, clock)

A per-key token-bucket rate limiter.

- `rate` is tokens added per second and `burst` the bucket capacity; both
  are positive ints, anything else raises `ValueError`. `clock` is a
  zero-argument callable returning the current time in integer
  **milliseconds**; it may go backwards: a backwards step adds no tokens and
  destroys none already earned, and later time is measured from the new,
  earlier reading.
- A key's bucket starts full the first time the key is seen.
- `allow(key, cost=1) -> bool` spends `cost` tokens if the bucket holds at
  least that many and returns True; otherwise it spends nothing and returns
  False. `cost` is an int >= 1, else `ValueError`. A `cost` above `burst`
  can never succeed and returns False without changing state.
- Refill is exact: after `t` ms a bucket gains `rate * t / 1000` tokens,
  capped at `burst`. Fractional tokens accumulate exactly across calls (no
  floating point drift: 1000 calls 1 ms apart at `rate=1` add exactly one
  token).
- `retry_after(key, cost=1) -> int` is the smallest number of whole ms
  after which `allow(key, cost)` would succeed if nothing else happened
  (0 if it would succeed now). It does not change state. For `cost > burst`
  it raises `ValueError`.
- Memory stays bounded: a bucket that has been full and untouched for at
  least `burst * 1000 / rate` ms is indistinguishable from a new one, so
  `len(limiter)` (the number of keys currently tracked) must drop such keys
  by the time any later call to `allow` returns.
