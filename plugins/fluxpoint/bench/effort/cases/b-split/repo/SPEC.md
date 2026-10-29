# split(amount, weights)

Split an integer number of lovelace between payees in proportion to integer
weights.

- `amount` is an `int` >= 0. `weights` is a non-empty list of `int` >= 0
  with at least one weight > 0. Anything else (bool, float, negative,
  empty list, all-zero weights) raises `ValueError`.
- Each payee first gets `floor(amount * w / W)` where `W = sum(weights)`.
- The lovelace left over (always fewer than the number of payees with a
  positive weight) go one each to the payees with the largest remainder
  `amount * w mod W`. Ties go to the lower index.
- A payee with weight 0 never receives anything.
- The result is a list of ints, one per weight, and always sums to
  `amount` exactly. Do not use floating point: amounts can exceed 2**63.
