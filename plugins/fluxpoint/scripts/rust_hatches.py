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
whatever that file declares in turn, with `#[path]` and
`include!("file.rs")` followed); a file or a block that opens with
`#![cfg(test)]`; and what Cargo builds as a test, a bench or a build
script, which is `tests/`, `benches/` and `build.rs` beside a tracked
manifest and the `path` a manifest gives a `[[test]]`, a `[[bench]]` or its
`build` key. A build script runs at build time, and when it panics the
build fails, which is the opposite of a hatch that passes in silence.

A test-only attribute covers what it is on and nothing after it. On a
generic or a closure parameter that is the parameter, up to the `>` or the
`|` that closes its list. On `const { .. }` it is the block. An inner
attribute counts where one can stand, at the head of a file or a block:
`stringify!(#![cfg(test)])` is tokens, and the file around it ships.

A file ships when anything that ships compiles it. One production `mod` or
`include!` line therefore keeps a file in the count wherever it sits,
`tests/` included. A crate root is never test code by declaration, because
Cargo builds it on its own: `src/lib.rs`, `src/main.rs`, a target under
`src/bin/` or `examples/`, and the `path` a manifest gives a `[lib]`,
`[[bin]]` or `[[example]]`. A test that pulls the binary in with
`#[path = "../src/main.rs"]` does not take the program out of the count.
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
`_ = future => body` is a `select!` branch and discards nothing.
Numeric `as` casts, indexing and arithmetic are hatches for some types and
not others, and a token scan has no types. Clippy does, in its
`cast_possible_truncation`, `indexing_slicing` and `arithmetic_side_effects`
lints, and the `allow` that silences it is counted here.

Known misses, stated rather than assumed: code a macro generates (a macro
definition is counted once, where it is written); what a macro does with
the tokens it is handed, which are read here as the code they look like,
so a macro that strips `#[cfg(test)]` from its input hides what follows
it; a release build that turns a test-only cfg on, by naming
`runtime-benchmarks` in a `default` feature list or printing
`cargo:rustc-cfg=fuzzing` from a build script; an `include!` whose path is
computed, and a `#[path]` or `include!` literal that carries an escape; a
target path a manifest spreads over several lines of an inline table; a
crate root in a tree that tracks no `Cargo.toml` (nothing there tells it
from a module, so one that only test code declares is left out);
`Option::unwrap(x)` written as a path; a message passed to `.expect` in a
variable; `.ok();` used to discard a result; `if cfg!(test)` branches; a
`WeightInfo` whose own implementation was written by hand; and the
opt-outs of the benchmarks themselves (`#[benchmark(skip_meta)]`,
`pov_mode = Ignored`), which sit in code this scan leaves out as
benchmark-only. Counted although it never ships: a cargo-fuzz crate, whose
targets are binaries with nothing in their source to say otherwise.
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

# One token per match. Whitespace and line comments are consumed in runs,
# and whitespace is what rustc calls whitespace: U+200E and U+200F separate
# tokens there, so `x.<U+200E>unwrap()` is a call, and one no diff shows.
# Block comments nest in Rust, which no regular expression can count, so
# the lexer handles the opener itself. A char literal is tried before a
# lifetime because `'a'` is one and `'a` is the other, and a number never
# takes a `.` that is not followed by a digit, or `self.0.unwrap()` would
# be read as the number `0.unwrap`.
_TOKEN = re.compile(r'''
    (?P<skip>(?:[\s\u200e\u200f]+|//[^\n]*)+)
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
_OPENERS = frozenset(_PAIRS.values())
_PAD = 8
# How far a scan for the end of an item header or a statement goes before it
# stops and leaves the rest counted, and how deep a cfg predicate is read.
# Both are far past anything written by hand; they keep a file of hostile
# shape from costing more than its length.
_SCAN = 1024
_DEPTH = 64
# Words a raw identifier exists to spell. `r#type` is a field's name and
# must not read as the keyword. Any other `r#name` is the identifier
# `name`, and `x.r#unwrap()` is the call it looks like.
_KEYWORDS = frozenset('''
    abstract as async auto await become box break const continue crate default do dyn else enum extern false final fn
    for gen if impl in let loop macro macro_rules match mod move mut override priv pub raw ref return safe self Self
    static struct super trait true try type typeof union unsafe unsized use virtual where while yield
'''.split())
_QUOTED = re.compile(r'r(#*)"([\s\S]*)"\1\Z|"([\s\S]*)"\Z')

_ITEM_BLOCK = frozenset(('fn', 'mod', 'struct', 'enum', 'union', 'trait', 'impl', 'macro_rules'))
_ITEM_SEMI = frozenset(('type', 'use', 'static', 'let'))
_ITEM_PREFIX = frozenset(('pub', 'crate', 'super', 'self', 'in', 'default', 'async', 'unsafe', 'extern', 'safe'))
_MOD_ROOTS = frozenset(('lib.rs', 'main.rs', 'mod.rs'))
_CRATE_ROOT = re.compile(r'(?:^|/)(?:src/bin|tests|benches|examples)/[^/]+\.rs$')
# Where Cargo finds the targets that ship, relative to a manifest, and the
# manifest tables whose `path` moves one.
_TARGET = re.compile(r'src/(?:lib|main)\.rs|(?:src/bin|examples)/[^/]+(?:/main)?\.rs')
_TARGET_TABLES = frozenset(('lib', 'bin', 'example'))
_TARGET_KEY = re.compile(r'^\s*(?:lib|bin|example)\s*[.=]')
_TARGET_PATH = re.compile(r'''(?<![\w-])path\s*=\s*(["'])([^"']+)\1''')
_STATEMENT_START = frozenset((';', '{', '}'))
_MACRO_OPEN = frozenset(('(', '[', '{'))


def _block_end(text, at):
    """The position after the block comment whose opener ends at `at`; comments nest."""
    depth = 1
    while depth:
        nested = _NEST.search(text, at)
        if not nested:
            return len(text)
        depth += 1 if nested.group() == '/*' else -1
        at = nested.end()
    return at


def _trivia(text, pos):
    """The position after whatever whitespace and comments stand at `pos`."""
    end = len(text)
    while pos < end:
        m = _TOKEN.match(text, pos)
        if m.lastgroup == 'skip':
            pos = m.end()
        elif m.lastgroup == 'block':
            pos = _block_end(text, m.end())
        else:
            break
    return pos


def _unquote(token):
    """The path a string literal spells, or None when it carries an escape or is not a plain string."""
    m = _QUOTED.match(token)
    if not m:
        return None
    if m.group(2) is not None:
        return m.group(2)
    return None if '\\' in m.group(3) else m.group(3)


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
        self.includes = []          # (path an `include!` names, test-only)
        self.tests = []             # (function name, token index) under a test attribute
        self.regions = []           # (first, last) token of each item a test-only attribute covers
        self._mod_paths = {}
        self._lines = None
        self._walk()
        self._modules()

    # ---------------------------------------------------------------- lexing
    def _lex(self, text):
        kinds, texts, starts = self.kinds, self.texts, self.starts
        end, match = len(text), _TOKEN.match
        pos = 1 if text.startswith('\ufeff') else 0     # rustc drops a byte order mark
        if text.startswith('#!', pos):
            # A script's interpreter line, as rustc decides it: `#!` opens
            # one unless what follows, whitespace and comments aside, is
            # the `[` of an attribute.
            after = _trivia(text, pos + 2)
            if text[after:after + 1] != '[':
                pos = text.find('\n', pos)
                pos = end if pos < 0 else pos
        while pos < end:
            m = match(text, pos)
            kind = m.lastgroup
            if kind == 'skip':
                pos = m.end()
            elif kind == 'block':
                pos = _block_end(text, m.end())
            else:
                token = m.group()
                if kind == 'i' and token.startswith('r#') and token[2:] not in _KEYWORDS:
                    token = token[2:]
                kinds.append(kind)
                texts.append(token)
                starts.append(pos)
                pos = m.end()

    def _brackets(self):
        match, stack, texts, kinds = [-1] * (self.size + _PAD), [], self.texts, self.kinds
        unclosed = dict.fromkeys(_OPENERS, 0)
        for index in range(self.size):
            if kinds[index] != 'p':
                continue
            token = texts[index]
            if token in unclosed:
                stack.append(index)
                unclosed[token] += 1
            elif token in _PAIRS:
                opener = _PAIRS[token]
                if not unclosed[opener]:
                    continue                # a stray closer: nothing it could pair with is open
                for depth in range(len(stack) - 1, -1, -1):
                    if texts[stack[depth]] == opener:
                        match[stack[depth]], match[index] = index, stack[depth]
                        for dropped in stack[depth:]:
                            unclosed[texts[dropped]] -= 1
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

    def _requires_nonprod(self, at, end, depth=0):
        """(the cfg predicate at `at` holds only in a build that never ships, next index)."""
        texts = self.texts
        name = texts[at]
        at += 1
        if at < end and texts[at] == '(' and self.match[at] > at:
            close, parts, inner = self.match[at], [], at + 1
            if depth >= _DEPTH:
                return False, close + 1     # nested past reading: taken to ship
            while inner < close:
                value, after = self._requires_nonprod(inner, close, depth + 1)
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

    def _enclosing(self, at):
        """`<` or `|` when the attribute at `at` is on a generic or a closure parameter, else ''.

        Those two lists close with a token that pairs with nothing, so where
        a parameter ends cannot be read off the bracket around it. This
        reads backwards over what is balanced and stops at the first thing
        a parameter list cannot hold.
        """
        texts, kinds, match = self.texts, self.kinds, self.match
        angle = 0
        for _ in range(256):
            at -= 1
            if at < 0:
                break
            if kinds[at] != 'p':
                continue
            token = texts[at]
            if token == ')' or token == ']':
                if match[at] < 0:
                    break
                at = match[at]
            elif token in _OPENERS or token == '}' or token == ';':
                break
            elif token == '>':
                if texts[at - 1] == '=':
                    break                   # a match arm's arrow
                if texts[at - 1] != '-':
                    angle += 1
            elif token == '<':
                if not angle:
                    return '<'
                angle -= 1
            elif token == '|':
                return '' if angle else '|'
        return ''

    def _item(self, start, context=''):
        """(last token of the item starting at `start`, its deciding keyword index or -1).

        `context` is what `_enclosing` found. A generic or a closure
        parameter is never an item, whatever word it opens with, and it
        ends where its list does.
        """
        texts, kinds, match, size = self.texts, self.kinds, self.match, self.size
        mode, keyword, at = 'other', -1, start
        while not context and at < min(size, start + 12):
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
                    if texts[at + 1] == '{':
                        mode = 'block'      # an inline const block ends at its brace
                    else:
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
        at, angle = start, 0
        for _ in range(_SCAN):
            if at >= size:
                return size - 1, keyword
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
                if context == '<':
                    if token == '<':
                        angle += 1
                    elif token == '>' and texts[at - 1] not in ('-', '='):
                        if not angle:
                            return at - 1, keyword
                        angle -= 1
                elif context == '|' and token == '|':
                    return at - 1, keyword
            at += 1
        return at - 1, keyword              # no end in sight: stop here, and what follows is counted

    def _split(self, opener):
        """[(first, end)] for each comma-separated part inside the bracket at `opener`."""
        texts, kinds, match = self.texts, self.kinds, self.match
        close, parts, begin, at = match[opener], [], opener + 1, opener + 1
        while at < close:
            if kinds[at] == 'p':
                token = texts[at]
                if token in _OPENERS and match[at] > at:
                    at = match[at] + 1
                    continue
                if token == ',':
                    parts.append((begin, at))
                    begin = at + 1
            at += 1
        if begin < close:
            parts.append((begin, close))
        return parts

    def _paths(self, first, close):
        """[(file a `path` attribute names, it sits under a cfg_attr, one only a test build satisfies)]."""
        texts, kinds, match = self.texts, self.kinds, self.match
        out, pending = [], [(first, close, False, False)]
        while pending:
            first, close, conditional, nonprod = pending.pop()
            if texts[first] == 'path':
                if texts[first + 1] == '=' and kinds[first + 2] == 's' and first + 3 == close:
                    literal = _unquote(texts[first + 2])
                    if literal is not None:
                        out.append((literal, conditional, nonprod))
            elif texts[first] == 'cfg_attr' and texts[first + 1] == '(' and first + 1 < close and match[first + 1] > first:
                parts = self._split(first + 1)
                if parts:
                    nonprod = nonprod or self._requires_nonprod(parts[0][0], parts[0][1])[0]
                    pending.extend((begin, end, True, nonprod) for begin, end in parts[1:])
        return out

    def _walk(self):
        """Find every attribute, and mark what a test-only one covers."""
        texts, kinds, match, size, excluded = self.texts, self.kinds, self.match, self.size, self.excluded
        open_braces, at = [], 0
        # Where the next inner attribute of the file (-1) or of each open
        # block may stand: at its head, after the ones before it. Anywhere
        # else `#![cfg(test)]` is tokens a macro was handed, and the code
        # around it ships.
        head = {-1: 0}
        while at < size:
            token = texts[at]
            if kinds[at] != 'p':
                at += 1
                continue
            if token == '{':
                open_braces.append(at)
                head[at] = at + 1
            elif token == '}':
                while open_braces and match[open_braces[-1]] <= at:
                    head.pop(open_braces.pop(), None)
            elif token == '#':
                inner = texts[at + 1] == '!'
                bracket = at + 2 if inner else at + 1
                if texts[bracket] == '[' and match[bracket] > bracket:
                    close = match[bracket]
                    if inner:
                        self.attributes.append((at, close, bracket + 1))
                        owner = open_braces[-1] if open_braces else -1
                        if head.get(owner) == at:
                            head[owner] = close + 1
                            if self._marks_nonprod(bracket + 1, close)[0]:
                                if owner < 0:
                                    self.nonprod = True
                                elif not excluded[owner]:
                                    after = match[owner] + 1
                                    excluded[owner:after] = b'\x01' * (after - owner)
                        at = close + 1
                        continue
                    group_start, group, nonprod, collected, pathed = at, [], False, False, False
                    while texts[at] == '#' and texts[at + 1] == '[' and match[at + 1] > at:
                        close = match[at + 1]
                        self.attributes.append((at, close, at + 2))
                        group.append((at + 2, close))
                        skipped, test = self._marks_nonprod(at + 2, close)
                        nonprod, collected = nonprod or skipped, collected or test
                        pathed = pathed or texts[at + 2] == 'path' or texts[at + 2] == 'cfg_attr'
                        at = close + 1
                    if not (nonprod or collected or pathed):
                        continue            # nothing here needs to know where the item ends
                    last, keyword = self._item(at, self._enclosing(group_start))
                    if keyword >= 0 and texts[keyword] == 'mod':
                        paths = [found for first, close in group for found in self._paths(first, close)]
                        if paths:
                            self._mod_paths[keyword] = paths
                    if collected and keyword >= 0 and texts[keyword] == 'fn' and kinds[keyword + 1] == 'i':
                        self.tests.append((texts[keyword + 1], keyword + 1))
                    if nonprod:
                        self.regions.append((group_start, last))
                        if not excluded[group_start]:       # else an enclosing region already covers it
                            excluded[group_start:last + 1] = b'\x01' * (last + 1 - group_start)
                    continue
            at += 1

    def _modules(self):
        """Out-of-line `mod name;` declarations, with the inline modules around each, and `include!` sites."""
        texts, kinds, match, excluded = self.texts, self.kinds, self.match, self.excluded
        inline = []
        for index in range(self.size):
            if kinds[index] != 'i':
                continue
            if texts[index] == 'include':
                # Only a literal path: `include!(concat!(env!("OUT_DIR"), ...))`
                # names a file the build writes, which is not tracked.
                opener = index + 2
                if texts[index + 1] == '!' and texts[opener] in _MACRO_OPEN and kinds[index + 3] == 's':
                    close = match[opener]
                    if close == index + 4 or (close == index + 5 and texts[index + 4] == ','):
                        literal = _unquote(texts[index + 3])
                        if literal is not None:
                            self.includes.append((literal, bool(excluded[index])))
                continue
            if texts[index] != 'mod' or kinds[index + 1] != 'i':
                continue
            while inline and inline[-1][1] < index:
                inline.pop()
            name = texts[index + 1][2:] if texts[index + 1].startswith('r#') else texts[index + 1]
            paths = self._mod_paths.get(index, ())
            after = texts[index + 2]
            if after == '{' and match[index + 2] > index:
                # `#[path]` on an inline module names the directory its
                # children live in, in place of the module's own name.
                moved = [literal for literal, conditional, _ in paths if not conditional]
                inline.append(((moved[0], True) if moved else (name, False), match[index + 2]))
            elif after == ';':
                self.mods.append((name, tuple(part for part, _ in inline), paths, bool(excluded[index])))

    # --------------------------------------------------------------- hatches
    def _statement_tail(self, at):
        """The token before the `;` that ends the statement continuing at `at`.

        `=>` instead when a fat arrow comes first. That is a `select!`
        branch, `pattern = future => body`, and not a statement at all.
        """
        texts, kinds, match, size = self.texts, self.kinds, self.match, self.size
        for _ in range(_SCAN):
            if at >= size:
                break
            if kinds[at] == 'p':
                token = texts[at]
                if token in _OPENERS:
                    at = match[at] + 1 if match[at] > at else at + 1
                    continue
                if token == ';' or token in _PAIRS:
                    break
                if token == '=' and texts[at + 1] == '>':
                    return '=>'
            at += 1
        return texts[at - 1]

    def _opens_statement(self, index, after_attribute):
        """The token at `index` begins a statement, in a block or in a macro's repetition."""
        if not index:
            return after_attribute
        before = self.texts[index - 1]
        return (before in _STATEMENT_START or (after_attribute and before == ']')
                or (before == '(' and self.texts[index - 2] == '$'))

    def _call(self, at):
        """The index of a method's `(`, past an empty turbofish: `x.unwrap::<>()` is `x.unwrap()`."""
        texts = self.texts
        if texts[at] == ':' and texts[at + 1] == ':' and texts[at + 2] == '<' and texts[at + 3] == '>':
            return at + 4
        return at

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
        pending = [(first, close)]
        while pending:
            first, close = pending.pop()
            path, after = self._path(first, close)
            if not path:
                continue
            opened = texts[after] == '(' and after < close and match[after] > after
            if path == ['allow'] and opened:
                lints, depth, named, assigned = 0, 0, False, False
                for index in range(after + 1, match[after] + 1):
                    token = texts[index]
                    if kinds[index] == 'p' and token in _OPENERS:
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
                # What it wraps is read in turn, and not by recursion: rustc
                # takes a thousand of these nested, and so must this.
                parts = self._split(after)
                if parts and not self._requires_nonprod(parts[0][0], parts[0][1])[0]:
                    pending.extend(parts[1:])
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
                call = self._call(index + 1)
                if texts[index - 1] == '.' and index and texts[call] == '(' and texts[call + 1] == ')':
                    out.append(('rust.panic', index))
            elif token == 'expect' or token == 'expect_err':
                call = self._call(index + 1)
                if texts[index - 1] == '.' and index and texts[call] == '(' and self._has_message(call):
                    out.append(('rust.panic', index))
            elif token == 'panic' or token == 'unreachable':
                if texts[index + 1] == '!' and texts[index + 2] in _MACRO_OPEN:
                    out.append(('rust.panic', index))
            elif token == 'todo' or token == 'unimplemented':
                if texts[index + 1] == '!' and texts[index + 2] in _MACRO_OPEN:
                    out.append(('rust.todo', index))
            elif token == 'let':
                if (texts[index + 1] == '_' and texts[index + 2] == '=' and texts[index + 3] != '='
                        and self._opens_statement(index, True)
                        and self._statement_tail(index + 3) != '?'):
                    out.append(('rust.let_underscore', index))
            elif token == '_':
                if (texts[index + 1] == '=' and texts[index + 2] not in ('=', '>')
                        and self._opens_statement(index, False)
                        and self._statement_tail(index + 2) not in ('?', '=>')):
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
_ALLOW = re.compile(r'''=\s*("{3}|'{3}|"|')allow\1''')
_LINT_KEY = re.compile(r'^\s*(?:workspace\s*\.\s*)?lints\s*[.=]')
_BUILD_KEY = re.compile(r'''^\s*build\s*=\s*(["'])([^"']+)\1''')


def manifest_hits(text):
    """[(category, line number)] for each lint a Cargo manifest sets to "allow".

    In every spelling Cargo honours: a `[lints.rust]` table, a dotted key
    or an inline table at the root or under `[workspace]`, and a level
    written `{ level = "allow" }`.
    """
    out, section = [], ''
    for number, raw in enumerate(text.splitlines(), 1):
        line = _uncommented(raw)
        header = _SECTION.match(line)
        if header:
            section = re.sub(r'[\s"\']', '', header.group(1))
            continue
        lints = (section in ('lints', 'workspace.lints') or section.startswith(('lints.', 'workspace.lints.'))
                 or (section in ('', 'workspace') and _LINT_KEY.match(line)))
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

    @staticmethod
    def _inside(home, directory, inline):
        """The directory the children of the inline modules `inline` live in.

        As rustc resolves it: a module's name is a directory under its
        parent's, the file's own module directory at the top. A `#[path]`
        on an inline module replaces its name, and at the top of a file it
        starts from the directory the file is in.
        """
        base = None
        for component, moved in inline:
            base = posixpath.join((directory if moved else home) if base is None else base, component)
        return home if base is None else posixpath.normpath(base)

    def _declared(self, rel, source):
        """(file a `mod` declaration or an `include!` in `rel` names, named by test-only code)."""
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
        for name, inline, paths, nonprod in source.mods:
            # A `#[path]` names the file outright. Under a `cfg_attr` it
            # names the file for the builds that satisfy the predicate, and
            # the module's usual file is still the one for the rest.
            start = self._inside(homes[0], directory, inline) if inline else directory
            for literal, conditional, test_only in paths:
                target = posixpath.normpath(posixpath.join(start, literal))
                if target in self.sources:
                    yield target, nonprod or test_only
            if any(not conditional for _, conditional, _ in paths):
                continue
            for where in homes:
                inside = self._inside(where, directory, inline)
                found = [candidate for candidate in (posixpath.join(inside, name + '.rs'),
                                                     posixpath.join(inside, name, 'mod.rs'))
                         if candidate in self.sources]
                if found:
                    for candidate in found:
                        yield candidate, nonprod
                    break
        for path, nonprod in source.includes:
            # Relative to the including file, whatever inline module it is in.
            target = posixpath.normpath(posixpath.join(directory, path))
            if target in self.sources:
                yield target, nonprod

    def _targets(self):
        """(shipping, staged): the files Cargo builds on their own.

        `shipping` maps a library, binary or example root to the package
        directories whose manifest names it by `path`, an empty set for one
        found where Cargo looks. `staged` holds what a manifest names as a
        test, a bench or its build script, none of which reach a release.
        """
        shipping, staged = {}, set()
        for rel in self.sources:
            parts = rel.split('/')
            for depth in range(max(0, len(parts) - 4), len(parts) - 1):
                if ('/'.join(parts[:depth] + ['Cargo.toml']) in self.manifests
                        and _TARGET.fullmatch('/'.join(parts[depth:]))):
                    shipping.setdefault(rel, set())
        for manifest, text in self.manifests.items():
            home, section = posixpath.dirname(manifest), ''
            for raw in text.splitlines():
                line = _uncommented(raw)
                header = _SECTION.match(line)
                if header:
                    section = re.sub(r'[\s"\']', '', header.group(1))
                elif section in _TARGET_TABLES or (not section and _TARGET_KEY.match(line)):
                    for _, value in _TARGET_PATH.findall(line):
                        shipping.setdefault(posixpath.normpath(posixpath.join(home, value)), set()).add(home)
                elif section == 'test' or section == 'bench':
                    for _, value in _TARGET_PATH.findall(line):
                        staged.add(posixpath.normpath(posixpath.join(home, value)))
                elif section == 'package':
                    script = _BUILD_KEY.match(line)
                    if script:
                        staged.add(posixpath.normpath(posixpath.join(home, script.group(2))))
        return shipping, staged

    def _nonprod_files(self):
        """Files no shipped build compiles: tests, benches, build scripts, and what only they name.

        The least set closed under three rules. A file that declares itself
        test-only is in it. So is a file Cargo builds as a test, bench or
        build target, unless shipping code names it too. So is any other
        file that something names and nothing shipping does, unless Cargo
        builds it as a target of its own.
        """
        (shipping, staged), located, named = self._targets(), set(), {}
        for rel, source in self.sources.items():
            parts = rel.split('/')
            # Where Cargo looks for tests, benches and the build script, and
            # what a manifest names as one. Not the file a package's own
            # manifest names as its library or a binary: that one ships
            # from wherever it sits, `tests/` and `build.rs` included.
            shipped_by = shipping.get(rel)
            if shipped_by is None and rel in staged:
                located.add(rel)
            elif (parts[-1] == 'build.rs' and '/'.join(parts[:-1] + ['Cargo.toml']) in self.manifests
                  and '/'.join(parts[:-1]) not in (shipped_by or ())) or any(
                    part in ('tests', 'benches') and '/'.join(parts[:depth] + ['Cargo.toml']) in self.manifests
                    and '/'.join(parts[:depth]) not in (shipped_by or ())
                    for depth, part in enumerate(parts[:-1])):
                located.add(rel)
            for target, nonprod in self._declared(rel, source):
                if target != rel:           # a file is not test code for naming itself
                    named.setdefault(target, []).append((rel, nonprod))
        tests = {rel for rel, source in self.sources.items() if source.nonprod}
        changed = True
        while changed:
            changed = False
            for rel in self.sources:
                edges = named.get(rel, ())
                if (rel not in tests and (rel in located or (edges and rel not in shipping))
                        and all(nonprod or parent in tests for parent, nonprod in edges)):
                    tests.add(rel)
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
                    lines = source.text.split('\n')     # the split `line` counts by, and no other
                number = source.line(index)
                yield category, rel, number, lines[number - 1].rstrip('\r') if number <= len(lines) else ''

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
