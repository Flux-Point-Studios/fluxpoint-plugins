# match(pattern, path) -> bool

Match a `/`-separated relative path against a glob pattern. The whole path
must match. Do not use `fnmatch`, `glob`, `pathlib` matching or `re`.

- `?` matches exactly one character other than `/`.
- `*` matches any run (possibly empty) of characters other than `/`.
- `**` as a whole path segment (the entire text between slashes, or at
  the start or end of the pattern) matches zero or more whole segments.
  So `a/**/b` matches `a/b`, `a/x/b` and `a/x/y/b`; `**/c` matches `c`
  and `x/y/c`; `a/**` matches `a/x` and `a/x/y` but not `a` itself.
  A `**` that is not a whole segment (as in `a**b`) behaves like `*`.
- `[abc]` matches one listed character; `[a-z]` a range; `[!...]` the
  complement. A class never matches `/`. A `]` immediately after `[` or
  `[!` is a literal member. A `[` with no closing `]` is a literal `[`.
- `\x` matches the character `x` literally (for any `x`, including `*`,
  `?`, `[` and `\`). A trailing lone `\` is a literal backslash.
- Everything else matches itself. Matching is case-sensitive.
- It must not take exponential time: a pattern of 30 `*a` pieces against
  a 200-character path of `a`s ending in `b` must answer in well under a
  second.
