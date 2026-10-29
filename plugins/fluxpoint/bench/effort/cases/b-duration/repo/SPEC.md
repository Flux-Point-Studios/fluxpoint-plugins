# Durations

## parse(text) -> int

Return the duration in whole milliseconds.

- Grammar: optional leading `-`, then one or more `<number><unit>` parts
  with no spaces. Units: `d` (86400000 ms), `h`, `m`, `s`, `ms`.
- `<number>` is digits with an optional fractional part (`1.5`, `0.25`).
  No sign, no leading `.` (`.5h` is invalid), no trailing `.` (`5.h` is
  invalid), and no leading zeros on the integer part except a lone `0`
  (`05m` is invalid, `0.5m` is fine).
- Units must appear in strictly decreasing order of size and at most once:
  `1h30m` is fine, `30m1h` and `1h1h` are invalid.
- The total must be a whole number of milliseconds computed exactly (no
  floating point): `1.0005s` is invalid, `0.1s` is 100, `1.5h` is 5400000.
- `-` negates the whole duration. `-0s` is 0.
- Anything else, including the empty string, `-`, a bare number and an
  unknown unit, raises `ValueError`.

## format_ms(ms) -> str

The canonical form of an int number of milliseconds: the largest units
first, each unit once, zero parts omitted, no fractions, a leading `-` for
negative values, and `0s` for zero. So `5400000` is `1h30m` and `90061001`
is `1d1h1m1s1ms`. For every int `ms`, `parse(format_ms(ms)) == ms`.
