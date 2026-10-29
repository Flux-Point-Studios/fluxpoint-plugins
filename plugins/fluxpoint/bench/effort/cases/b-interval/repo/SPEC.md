# IntervalSet

A set of integers stored as sorted, disjoint, non-adjacent half-open
intervals `[start, end)`.

- `IntervalSet()` is empty. `add(start, end)` and `remove(start, end)` take
  ints with `start <= end`; `start == end` is a no-op; `start > end` raises
  `ValueError`.
- `add` merges overlapping **and touching** intervals: adding `[0,5)` then
  `[5,8)` stores the single interval `[0,8)`.
- `remove` subtracts the range, splitting an interval when needed.
- `intervals()` returns the stored intervals as a list of `(start, end)`
  tuples, sorted.
- `x in s` is membership of the integer `x`.
- `len(s)` is the number of integers in the set.
- `gaps(lo, hi)` returns the sorted list of maximal `(start, end)` ranges
  inside `[lo, hi)` that are not in the set (raises `ValueError` if
  `lo > hi`).
- Operations must stay fast with 100000 stored intervals: `in` must not
  scan every interval.
