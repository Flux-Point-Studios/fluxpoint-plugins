# satisfies(version, range_) -> bool

Versions are `MAJOR.MINOR.PATCH` with an optional `-PRERELEASE` made of
dot-separated identifiers. Build metadata (`+...`) is ignored everywhere.
Precedence follows SemVer 2.0.0: numeric identifiers compare numerically
and below alphanumeric ones, a shorter prerelease list is lower when all
shared identifiers are equal, and any prerelease is lower than the same
version without one.

A range is one or more comparator sets joined by `||`; it matches if any set
matches. A set is comparators separated by whitespace; it matches if all of
them match. Comparators:

- `1.2.3` or `=1.2.3`: exactly equal.
- `>`, `>=`, `<`, `<=` followed by a full version.
- `^1.2.3`: `>=1.2.3 <2.0.0`; `^0.2.3`: `>=0.2.3 <0.3.0`; `^0.0.3`: `>=0.0.3 <0.0.4`.
- `~1.2.3`: `>=1.2.3 <1.3.0`.
- `1.2.x`, `1.2.*` or `1.2`: `>=1.2.0 <1.3.0`; `1.x` or `1`: `>=1.0.0 <2.0.0`;
  `*`, `x` or an empty range: any version without a prerelease.

Prerelease gating: a version with a prerelease matches a set only if some
comparator in that set has a prerelease on the same `MAJOR.MINOR.PATCH`
(as in node-semver). So `1.2.3-beta.2` matches `>=1.2.3-beta.1` but
`1.2.4-beta` does not match `>=1.2.3-beta.1`, and no prerelease matches `*`.

A malformed version or comparator raises `ValueError`.
