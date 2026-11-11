"""Rust escape hatches in production code; reads tokens, never runs rustc.

`cargo check` exits 0 on `todo!()` exactly as on the function it stands in
for, and `cargo clippy -- -D warnings` exits 0 on the lint an `#[allow]`
just silenced. Each category below is a construct that overrules a checker
which then reports success:

  rust.unsafe          the `unsafe` keyword: a block, an impl, an fn, an attribute
  rust.todo            `todo!` and `unimplemented!`, which type-check at any type
  rust.panic           `.unwrap()`, `.expect("...")`, `panic!` and `unreachable!`
  rust.lint_allow      each lint an `allow(...)` attribute names, and each lint a
                       Cargo manifest's `[lints]` table sets to "allow"
  rust.let_underscore  `let _ = expr;` and `_ = expr;`, which silence must_use

FRAME (Polkadot SDK pallets and runtimes) states its safety obligations as
attributes, and its opt-outs the same way:

  frame.unbenchmarked_weight  a `#[pallet::weight(...)]` that does not take its
                              value from a `WeightInfo`; a Config wired with
                              `type WeightInfo = ();`; `#[pallet::whitelist_storage]`
  frame.unbounded_storage     `#[pallet::without_storage_info]`, `#[pallet::unbounded]`
  frame.dev_mode              `#[frame_support::pallet(dev_mode)]`
  frame.feeless               `Pays::No` and `#[pallet::feeless_if(...)]`
  frame.unsigned              `#[pallet::validate_unsigned]`, `#[pallet::authorize(...)]`
                              and `ensure_none(origin)`: a call admitted with no
                              signature, nonce or fee

A weight counts on a syntactic fact, never a judgment of the number: the
attribute names no `WeightInfo`, so the value was written by hand. That is
`Weight::from_parts(10_000, 0)` and it is also a careful sum over
`T::DbWeight`; the first is a guess and the second is an argument, and both
are claims nobody measured. The baseline absorbs the ones a pallet has, and
the next one is a diff someone reads.

Only production code counts. A test that unwraps is asserting, so counting
test code would red every new test and the baseline would be re-recorded
without being read. What decides "test code" is the evidence the compiler
itself uses, never a file name: an item under a `cfg` that requires `test`,
`kani`, `fuzzing`, `doctest` or FRAME's `runtime-benchmarks` / `try-runtime`
features; a function under `#[test]` or a sibling attribute; a file every
declaration of which is such a module (`#[cfg(test)] mod tests;` and
whatever that file declares in turn); a file that opens with `#![cfg(test)]`;
and Cargo's own `tests/` and `benches/` targets beside a tracked manifest.
The `build.rs` beside a manifest is left out on the same evidence: it runs
at build time, and when it panics the build fails, which is the opposite
of a hatch that passes in silence.
A module merely named `tests` and declared without the cfg ships, and is
counted. So is a tracked `vendor/` tree: a marker file that took a crate
out of the count would be a file anyone could add, and a ratchet has no use
for an off switch that costs nothing. The baseline absorbs vendored code,
and a dependency bump that moves a count is re-recorded in the diff that
made it.

Deliberately not counted, because a ratchet that cries wolf is re-baselined
blind. `#[expect(...)]` warns once the lint it names stops firing, so it
cannot rot in place the way `#[allow]` can. `.expect(` counts only with a
message, since a parser's own `self.expect(Token::Comma)?` is a different
method. `let _ = f()?;` propagated the error and dropped only the payload.
Numeric `as` casts, indexing and arithmetic are hatches for some types and
not others, and a token scan has no types. Clippy does, in its
`cast_possible_truncation`, `indexing_slicing` and `arithmetic_side_effects`
lints, and the `allow` that silences it is counted here.

Known misses, stated rather than assumed: code a macro generates (a macro
definition is counted once, where it is written), `include!`d files,
`Option::unwrap(x)` written as a path, a message passed to `.expect` in a
variable, `.ok();` used to discard a result, `if cfg!(test)` branches, a
`WeightInfo` whose own implementation was written by hand, and the opt-outs
of the benchmarks themselves (`#[benchmark(skip_meta)]`, `pov_mode =
Ignored`), which sit in code this scan leaves out as benchmark-only.
"""
import bisect
import posixpath
import re

CATEGORIES = ('rust.unsafe', 'rust.todo', 'rust.panic', 'rust.lint_allow', 'rust.let_underscore',
              'frame.unbenchmarked_weight', 'frame.unbounded_storage', 'frame.dev_mode', 'frame.feeless',
              'frame.unsigned')

# What a cfg predicate has to require for the code under it to be absent
# from every build that ships.
NONPROD_FLAGS = frozenset(('test', 'kani', 'fuzzing', 'doctest'))
NONPROD_FEATURES = frozenset(('runtime-benchmarks', 'try-runtime'))
# The last path segment of an attribute that makes a function a collected
# test: `#[test]`, `#[tokio::test]`, `#[rstest]`, `#[quickcheck]`, ...
TEST_ATTRIBUTES = frozenset(('test', 'rstest', 'test_case', 'test_matrix', 'bench', 'quickcheck',
                             'proptest', 'wasm_bindgen_test'))
# Libraries whose presence in a test file means its tests can run over
# generated inputs instead of the cases the file lists.
GENERATORS = frozenset(('proptest', 'quickcheck', 'bolero', 'arbtest', 'kani'))

# One token per match. Whitespace and line comments are consumed in runs;
# block comments nest in Rust, which no regular expression can count, so
# the lexer handles the opener itself. A char literal is tried before a
# lifetime because `'a'` is one and `'a` is the other, and a number never
# takes a `.` that is not followed by a digit, or `self.0.unwrap()` would
# be read as the number `0.unwrap`.
_TOKEN = re.compile(r'''
    (?P<skip>(?:\s+|//[^\n]*)+)
  | (?P<block>/\*)
  | (?P<s>(?:b|c)?r(?P<hashes>\#*)"[\s\S]*?"(?P=hashes)|(?:b|c)?"(?:[^"\\]|\\[\s\S])*")
  | (?P<c>b?'(?:\\(?:u\{[0-9a-fA-F_]{1,8}\}|x[0-9a-fA-F]{2}|[\s\S])|[^'\\\n])')
  | (?P<l>'(?:r\#)?[^\W\d]\w*)
  | (?P<i>(?:r\#)?[^\W\d]\w*)
  | (?P<n>\d\w*(?:\.\d\w*)?)
  | (?P<p>[\s\S])
''', re.X)
_NEST = re.compile(r'/\*|\*/')
_PAIRS = {')': '(', ']': '[', '}': '{'}
_PAD = 8

_ITEM_BLOCK = frozenset(('fn', 'mod', 'struct', 'enum', 'union', 'trait', 'impl', 'macro_rules'))
_ITEM_SEMI = frozenset(('type', 'use', 'static', 'let'))
_ITEM_PREFIX = frozenset(('pub', 'crate', 'super', 'self', 'in', 'default', 'async', 'unsafe', 'extern', 'safe'))
_MOD_ROOTS = frozenset(('lib.rs', 'main.rs', 'mod.rs'))
_CRATE_ROOT = re.compile(r'(?:^|/)(?:src/bin|tests|benches|examples)/[^/]+\.rs$')
_STATEMENT_START = frozenset((';', '{', '}'))
_MACRO_OPEN = frozenset(('(', '[', '{'))


class _Source:
    """One file, lexed: parallel token arrays and the regions production never sees."""

    def __init__(self, rel, text):
        self.rel = rel
        self.text = text
        self.kinds, self.texts, self.starts = [], [], []
        self._lex(text)
        self.size = len(self.texts)
        for _ in range(_PAD):
            self.kinds.append('e')
            self.texts.append('')
            self.starts.append(len(text))
        self.match = self._brackets()
        self.excluded = bytearray(self.size + _PAD)
        self.nonprod = False        # the file declares itself test-only
        self.attributes = []        # (index of '#', index of ']', first token inside)
        self.mods = []              # (name, inline module names, #[path] value or None, test-only)
        self.tests = []             # (function name, token index) under a test attribute
        self.regions = []           # (first, last) token of each item a test-only attribute covers
        self._mod_paths = {}
        self._lines = None
        self._walk()
        self._modules()

    # ---------------------------------------------------------------- lexing
    def _lex(self, text):
        kinds, texts, starts = self.kinds, self.texts, self.starts
        pos, end, match = 0, len(text), _TOKEN.match
        if text.startswith('#!') and not text[2:].lstrip(' \t').startswith('['):
            pos = text.find('\n')           # a script's interpreter line
            pos = end if pos < 0 else pos
        while pos < end:
            m = match(text, pos)
            kind = m.lastgroup
            if kind == 'skip':
                pos = m.end()
            elif kind == 'block':
                depth, at = 1, m.end()
                while depth:
                    nested = _NEST.search(text, at)
                    if not nested:
                        at = end
                        break
                    depth += 1 if nested.group() == '/*' else -1
                    at = nested.end()
                pos = at
            else:
                kinds.append(kind)
                texts.append(m.group())
                starts.append(pos)
                pos = m.end()

    def _brackets(self):
        match, stack, texts, kinds = [-1] * (self.size + _PAD), [], self.texts, self.kinds
        for index in range(self.size):
            if kinds[index] != 'p':
                continue
            token = texts[index]
            if token in '([{':
                stack.append(index)
            elif token in _PAIRS:
                opener = _PAIRS[token]
                for depth in range(len(stack) - 1, -1, -1):
                    if texts[stack[depth]] == opener:
                        match[stack[depth]], match[index] = index, stack[depth]
                        del stack[depth:]
                        break
        return match

    def line(self, index):
        if self._lines is None:
            self._lines = [m.start() for m in re.finditer(r'\n', self.text)]
        return bisect.bisect_right(self._lines, self.starts[index]) + 1

    # ------------------------------------------------------------ attributes
    def _path(self, at, end):
        """(path segments, index after the path) for the path starting at `at`."""
        texts, kinds, segments = self.texts, self.kinds, []
        while at < end and kinds[at] == 'i':
            segments.append(texts[at])
            if texts[at + 1] == ':' and texts[at + 2] == ':' and at + 3 < end:
                at += 3
            else:
                at += 1
                break
        return segments, at

    def _requires_nonprod(self, at, end):
        """(the cfg predicate at `at` holds only in a build that never ships, next index)."""
        texts = self.texts
        name = texts[at]
        at += 1
        if at < end and texts[at] == '(' and self.match[at] > at:
            close, parts, inner = self.match[at], [], at + 1
            while inner < close:
                value, after = self._requires_nonprod(inner, close)
                parts.append(value)
                inner = max(after, inner + 1)
                if inner < close and texts[inner] == ',':
                    inner += 1
            if name == 'all':
                return any(parts), close + 1
            if name == 'any':
                return bool(parts) and all(parts), close + 1
            return False, close + 1         # not(...), and anything unrecognised
        if at < end and texts[at] == '=':
            value = texts[at + 1]
            return name == 'feature' and value.strip('"') in NONPROD_FEATURES, at + 2
        return name in NONPROD_FLAGS, at

    def _marks_nonprod(self, first, close):
        """(test-only, collected test) for the attribute whose contents are [first, close)."""
        path, after = self._path(first, close)
        if not path:
            return False, False
        if path == ['cfg']:
            if self.texts[after] == '(' and self.match[after] > after:
                return self._requires_nonprod(after + 1, self.match[after])[0], False
            return False, False
        if path[-1] in TEST_ATTRIBUTES:
            return True, True
        if len(path) == 2 and path[0] == 'kani' and path[1].startswith('proof'):
            return True, False
        return False, False

    def _item(self, start):
        """(last token of the item starting at `start`, its deciding keyword index or -1)."""
        texts, kinds, match, size = self.texts, self.kinds, self.match, self.size
        mode, keyword, at = 'other', -1, start
        while at < min(size, start + 12):
            token, kind = texts[at], kinds[at]
            if kind == 'i':
                if token in _ITEM_BLOCK:
                    mode, keyword = 'block', at
                    break
                if token in _ITEM_SEMI:
                    mode, keyword = 'semi', at
                    break
                if token == 'const':
                    if texts[at + 1] in ('fn', 'unsafe', 'async', 'extern'):
                        at += 1
                        continue
                    mode, keyword = 'semi', at
                    break
                if token in _ITEM_PREFIX:
                    at += 1
                    continue
                break
            if kind == 's' and at > start and texts[at - 1] == 'extern':
                at += 1
                continue
            if token == '(' and at > start and texts[at - 1] == 'pub' and match[at] > at:
                at = match[at] + 1
                continue
            if token == '{':
                mode = 'block'
            break
        at = start
        while at < size:
            if kinds[at] == 'p':
                token = texts[at]
                if token == '(' or token == '[':
                    at = match[at] + 1 if match[at] > at else at + 1
                    continue
                if token == '{':
                    if match[at] < at:
                        return size - 1, keyword
                    if mode == 'semi':
                        at = match[at] + 1
                        continue
                    return match[at], keyword
                if token == ';':
                    return at, keyword
                if token in _PAIRS:
                    return at - 1, keyword
                if token == ',' and mode == 'other':
                    return at, keyword
            at += 1
        return size - 1, keyword

    def _walk(self):
        """Find every attribute, and mark what a test-only one covers."""
        texts, kinds, match, size, excluded = self.texts, self.kinds, self.match, self.size, self.excluded
        open_braces, at = [], 0
        while at < size:
            token = texts[at]
            if kinds[at] != 'p':
                at += 1
                continue
            if token == '{':
                open_braces.append(at)
            elif token == '}':
                while open_braces and match[open_braces[-1]] <= at:
                    open_braces.pop()
            elif token == '#':
                inner = texts[at + 1] == '!'
                bracket = at + 2 if inner else at + 1
                if texts[bracket] == '[' and match[bracket] > bracket:
                    close = match[bracket]
                    if inner:
                        self.attributes.append((at, close, bracket + 1))
                        if self._marks_nonprod(bracket + 1, close)[0]:
                            if open_braces:
                                top = open_braces[-1]
                                for index in range(top, match[top] + 1):
                                    excluded[index] = 1
                            else:
                                self.nonprod = True
                        at = close + 1
                        continue
                    group_start, group, nonprod, collected = at, [], False, False
                    while texts[at] == '#' and texts[at + 1] == '[' and match[at + 1] > at:
                        close = match[at + 1]
                        self.attributes.append((at, close, at + 2))
                        group.append((at + 2, close))
                        skipped, test = self._marks_nonprod(at + 2, close)
                        nonprod, collected = nonprod or skipped, collected or test
                        at = close + 1
                    last, keyword = self._item(at)
                    if keyword >= 0 and texts[keyword] == 'mod':
                        for first, close in group:
                            if (texts[first] == 'path' and texts[first + 1] == '=' and kinds[first + 2] == 's'
                                    and first + 3 == close):
                                self._mod_paths[keyword] = texts[first + 2].strip('"')
                    if collected and keyword >= 0 and texts[keyword] == 'fn' and kinds[keyword + 1] == 'i':
                        self.tests.append((texts[keyword + 1], keyword + 1))
                    if nonprod:
                        self.regions.append((group_start, last))
                        for index in range(group_start, last + 1):
                            excluded[index] = 1
                    continue
            at += 1

    def _modules(self):
        """Out-of-line `mod name;` declarations, with the inline modules around each."""
        texts, kinds, match, excluded = self.texts, self.kinds, self.match, self.excluded
        inline = []
        for index in range(self.size):
            if texts[index] != 'mod' or kinds[index] != 'i' or kinds[index + 1] != 'i':
                continue
            while inline and inline[-1][1] < index:
                inline.pop()
            name = texts[index + 1][2:] if texts[index + 1].startswith('r#') else texts[index + 1]
            after = texts[index + 2]
            if after == '{' and match[index + 2] > index:
                inline.append((name, match[index + 2]))
            elif after == ';':
                self.mods.append((name, tuple(part for part, _ in inline),
                                  self._mod_paths.get(index), bool(excluded[index])))

    # --------------------------------------------------------------- hatches
    def _statement_tail(self, at):
        """The token before the `;` that ends the statement continuing at `at`."""
        texts, kinds, match, size = self.texts, self.kinds, self.match, self.size
        while at < size:
            if kinds[at] == 'p':
                token = texts[at]
                if token in '([{':
                    at = match[at] + 1 if match[at] > at else at + 1
                    continue
                if token == ';' or token in _PAIRS:
                    break
            at += 1
        return texts[at - 1]

    def _has_message(self, at):
        """`.expect(` at `at` (its paren) is Option's or Result's: it carries a message."""
        texts, kinds = self.texts, self.kinds
        at += 1
        while texts[at] == '&' or texts[at] == '*':
            at += 1
        if kinds[at] == 's':
            return True
        path, after = self._path(at, self.size)
        return bool(path) and path[-1] in ('format', 'concat', 'format_args') and texts[after] == '!'

    def _attribute_hits(self, first, close, anchor, out):
        texts, kinds, match = self.texts, self.kinds, self.match
        path, after = self._path(first, close)
        if not path:
            return
        opened = texts[after] == '(' and after < close and match[after] > after
        if path == ['allow'] and opened:
            lints, depth, named, assigned = 0, 0, False, False
            for index in range(after + 1, match[after] + 1):
                token = texts[index]
                if kinds[index] == 'p' and token in '([{':
                    depth += 1
                elif kinds[index] == 'p' and token in _PAIRS and index < match[after]:
                    depth -= 1
                elif (token == ',' and depth == 0) or index == match[after]:
                    lints += named and not assigned
                    named = assigned = False
                elif token == '=' and depth == 0:
                    assigned = True
                elif kinds[index] == 'i':
                    named = True
            out.extend(('rust.lint_allow', anchor) for _ in range(lints))
        elif path == ['cfg_attr'] and opened:
            parts, depth, begin = [], 0, after + 1
            for index in range(after + 1, match[after] + 1):
                token = texts[index]
                if kinds[index] == 'p' and token in '([{':
                    depth += 1
                elif kinds[index] == 'p' and token in _PAIRS and index < match[after]:
                    depth -= 1
                elif (token == ',' and depth == 0) or index == match[after]:
                    parts.append((begin, index))
                    begin = index + 1
            if parts and not self._requires_nonprod(parts[0][0], parts[0][1])[0]:
                for begin, end in parts[1:]:
                    self._attribute_hits(begin, end, anchor, out)
        elif len(path) >= 2 and path[-2] == 'pallet':
            if path[-1] in ('weight', 'weight_of_authorize') and opened:
                # The benchmarked route is `T::WeightInfo::call()`. A weight
                # that names no WeightInfo was written by hand, whatever it says.
                if match[after] > after + 1 and not any(
                        kinds[index] == 'i' and texts[index].endswith('WeightInfo')
                        for index in range(after + 1, match[after])):
                    out.append(('frame.unbenchmarked_weight', anchor))
            elif path[-1] == 'whitelist_storage':
                out.append(('frame.unbenchmarked_weight', anchor))
            elif path[-1] in ('without_storage_info', 'unbounded'):
                out.append(('frame.unbounded_storage', anchor))
            elif path[-1] == 'feeless_if':
                out.append(('frame.feeless', anchor))
            elif path[-1] in ('validate_unsigned', 'authorize'):
                out.append(('frame.unsigned', anchor))
        elif path[-1] == 'pallet' and opened:
            if any(texts[index] == 'dev_mode' for index in range(after + 1, match[after])):
                out.append(('frame.dev_mode', anchor))

    def hits(self):
        """[(category, token index)] over the tokens production code compiles."""
        if self.nonprod:
            return []
        texts, kinds, match, excluded = self.texts, self.kinds, self.match, self.excluded
        out = []
        for start, close, first in self.attributes:
            if not excluded[start]:
                self._attribute_hits(first, close, start, out)
        for index in range(self.size):
            if kinds[index] != 'i' or excluded[index]:
                continue
            token = texts[index]
            if token == 'unsafe':
                out.append(('rust.unsafe', index))
            elif token == 'unwrap' or token == 'unwrap_err':
                if texts[index - 1] == '.' and index and texts[index + 1] == '(' and texts[index + 2] == ')':
                    out.append(('rust.panic', index))
            elif token == 'expect' or token == 'expect_err':
                if texts[index - 1] == '.' and index and texts[index + 1] == '(' and self._has_message(index + 1):
                    out.append(('rust.panic', index))
            elif token == 'panic' or token == 'unreachable':
                if texts[index + 1] == '!' and texts[index + 2] in _MACRO_OPEN:
                    out.append(('rust.panic', index))
            elif token == 'todo' or token == 'unimplemented':
                if texts[index + 1] == '!' and texts[index + 2] in _MACRO_OPEN:
                    out.append(('rust.todo', index))
            elif token == 'let':
                if (texts[index + 1] == '_' and texts[index + 2] == '=' and texts[index + 3] != '='
                        and (index == 0 or texts[index - 1] in _STATEMENT_START or texts[index - 1] == ']')
                        and self._statement_tail(index + 3) != '?'):
                    out.append(('rust.let_underscore', index))
            elif token == '_':
                if (texts[index + 1] == '=' and texts[index + 2] not in ('=', '>')
                        and index and texts[index - 1] in _STATEMENT_START
                        and self._statement_tail(index + 2) != '?'):
                    out.append(('rust.let_underscore', index))
            elif token == 'Pays':
                if texts[index + 1] == ':' and texts[index + 2] == ':' and texts[index + 3] == 'No':
                    out.append(('frame.feeless', index))
            elif token == 'ensure_none':
                if texts[index + 1] == '(' and texts[index - 1] != 'fn':
                    out.append(('frame.unsigned', index))
            elif token == 'type':
                if (texts[index + 1] == 'WeightInfo' and texts[index + 2] == '=' and texts[index + 3] == '('
                        and texts[index + 4] == ')' and texts[index + 5] == ';'):
                    out.append(('frame.unbenchmarked_weight', index))
        out.sort(key=lambda hit: hit[1])
        return out

    def frame(self):
        """This file declares a FRAME pallet or assembles a runtime."""
        texts, kinds = self.texts, self.kinds
        for start, close, first in self.attributes:
            path, _ = self._path(first, close)
            if 'pallet' in path or path == ['frame_support', 'runtime']:
                return True
        return any(texts[index] == 'construct_runtime' and kinds[index] == 'i' and texts[index + 1] == '!'
                   for index in range(self.size))

    def generated(self):
        """A property-testing or model-checking library is used in this file."""
        return any(kind == 'i' and token in GENERATORS for kind, token in zip(self.kinds, self.texts))


def _uncommented(line):
    """A TOML line up to its comment; a `#` inside a string is data."""
    quote = ''
    for index, char in enumerate(line):
        if quote:
            if char == quote:
                quote = ''
        elif char in '"\'':
            quote = char
        elif char == '#':
            return line[:index]
    return line


_SECTION = re.compile(r'^\s*\[+\s*([^\]]*?)\s*\]+\s*$')
_ALLOW = re.compile(r'''=\s*["']allow["']''')
_ROOT_LINTS = re.compile(r'^\s*(?:workspace\s*\.\s*)?lints\s*\.')


def manifest_hits(text):
    """[(category, line number)] for each lint a Cargo manifest sets to "allow"."""
    out, section = [], ''
    for number, raw in enumerate(text.splitlines(), 1):
        line = _uncommented(raw)
        header = _SECTION.match(line)
        if header:
            section = re.sub(r'[\s"\']', '', header.group(1))
            continue
        lints = (section in ('lints', 'workspace.lints') or section.startswith(('lints.', 'workspace.lints.'))
                 or (not section and _ROOT_LINTS.match(line)))
        if lints:
            out.extend(('rust.lint_allow', number) for _ in _ALLOW.findall(line))
    return out


class Tree:
    """Every tracked Rust source and Cargo manifest, read together.

    Together, because whether a file is test code is decided by the files
    that declare it. `files` maps a repository-relative path to its text,
    for every tracked `.rs` file and every `Cargo.toml`; a manifest is also
    what makes the `tests/` and `benches/` beside it Cargo targets.
    """

    def __init__(self, files):
        self.manifests, self.sources = {}, {}
        for rel in sorted(files):
            if rel.endswith('.rs'):
                self.sources[rel] = _Source(rel, files[rel])
            elif posixpath.basename(rel) == 'Cargo.toml':
                self.manifests[rel] = files[rel]
        self.nonprod_files = self._nonprod_files()

    def _declared(self, rel, source):
        """(file a `mod` declaration in `rel` names, declared test-only)."""
        directory, base = posixpath.split(rel)
        own = posixpath.join(directory, base[:-3])
        # `lib.rs`, `main.rs` and `mod.rs` keep their modules beside them. So
        # does any crate root, whatever it is called, and a file's place does
        # not say for certain which it is: `tests/it.rs` is a root under a
        # manifest and an ordinary module under `src/`. Both homes are tried,
        # the likelier first.
        if base in _MOD_ROOTS:
            homes = (directory,)
        elif _CRATE_ROOT.search(rel) or (base == 'build.rs'
                                        and posixpath.join(directory, 'Cargo.toml') in self.manifests):
            homes = (directory, own)
        else:
            homes = (own, directory)
        for name, inline, path, nonprod in source.mods:
            if path is not None:
                target = posixpath.normpath(posixpath.join(homes[0] if inline else directory, *inline, path))
                if target in self.sources:
                    yield target, nonprod
                continue
            for where in homes:
                inside = posixpath.join(where, *inline) if inline else where
                found = [candidate for candidate in (posixpath.join(inside, name + '.rs'),
                                                     posixpath.join(inside, name, 'mod.rs'))
                         if candidate in self.sources]
                if found:
                    for candidate in found:
                        yield candidate, nonprod
                    break

    def _nonprod_files(self):
        """Files no shipped build compiles: tests, benches, build scripts, and what they declare."""
        tests = set()
        for rel, source in self.sources.items():
            parts = rel.split('/')
            if source.nonprod or any(
                    part in ('tests', 'benches') and '/'.join(parts[:depth] + ['Cargo.toml']) in self.manifests
                    for depth, part in enumerate(parts[:-1])):
                tests.add(rel)
            elif parts[-1] == 'build.rs' and '/'.join(parts[:-1] + ['Cargo.toml']) in self.manifests:
                tests.add(rel)
        declared = {}
        for rel, source in self.sources.items():
            for target, nonprod in self._declared(rel, source):
                declared.setdefault(target, []).append((rel, nonprod))
        changed = True
        while changed:
            changed = False
            for target, edges in declared.items():
                if target not in tests and all(nonprod or parent in tests for parent, nonprod in edges):
                    tests.add(target)
                    changed = True
        return tests

    def hits(self):
        """Yield (category, path, line, source line) for every production hatch."""
        for rel, text in self.manifests.items():
            lines = text.splitlines()
            for category, number in manifest_hits(text):
                yield category, rel, number, lines[number - 1]
        for rel, source in self.sources.items():
            if rel in self.nonprod_files:
                continue
            lines = None
            for category, index in source.hits():
                if lines is None:
                    lines = source.text.splitlines()
                number = source.line(index)
                yield category, rel, number, lines[number - 1] if number <= len(lines) else ''

    def tests(self):
        """{path: [test function names]} for every function a test build collects."""
        return {rel: [name for name, _ in source.tests] for rel, source in self.sources.items() if source.tests}

    def generated(self, rel):
        return self.sources[rel].generated()

    def frame(self):
        return any(source.frame() for source in self.sources.values())


def scan_tree(files):
    """Yield (category, path, line, source line) over a {path: text} tree."""
    return Tree(files).hits()
