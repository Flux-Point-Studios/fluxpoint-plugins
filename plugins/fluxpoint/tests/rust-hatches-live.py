#!/usr/bin/env python3
"""The Rust scanner beside two tools that parse Rust, over a real corpus.

`rust_hatches.py` reads tokens and never runs rustc, so the two judgments
everything else rests on are checked against tools that do parse Rust:

  lexing    what is code and what is a comment, a string, a char literal or
            a lifetime. Compared with proc-macro2, token pattern by token
            pattern, over each whole file.
  extents   where an item under a test-only attribute ends. Compared with
            syn, to the character, for every item, impl item, trait item,
            foreign item and `let`; to within one column for a field, a
            variant and a match arm, where owning the trailing comma is a
            matter of taste.

Not in the harness: it needs a Rust toolchain, crates.io and a corpus, none
of which the gate may assume. Run it by hand after touching the lexer or
the item walker:

  rust-hatches-live.py ~/.cargo/registry/src [more directories or files]

Exit 0 when both comparisons agree on every file, 1 on any difference, 2
when the toolchain or the corpus is missing. Regions only the scanner
reports are counted and not failed: syn does not parse inside a macro body
(`proptest! { #[test] fn ... }`) or attributes on expressions, and the
scanner reads both.
"""
import bisect
import collections
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / 'scripts'))
from rust_hatches import _Source  # noqa: E402

TRUTH = HERE / 'fixtures' / 'rust-truth'
OPEN = ('(', '[', '{')
BATCH = 400


def corpus(arguments):
    files = []
    for argument in arguments:
        path = Path(argument).expanduser()
        if path.is_file():
            files.append(str(path))
        else:
            files.extend(str(found) for found in sorted(path.rglob('*.rs')) if found.is_file())
    return files


def build(target):
    cargo = shutil.which('cargo')
    if not cargo:
        return None
    result = subprocess.run([cargo, 'build', '--release', '--quiet', '--manifest-path', str(TRUTH / 'Cargo.toml'),
                             '--target-dir', str(target)], capture_output=True, text=True)
    if result.returncode:
        print(result.stderr[-2000:], file=sys.stderr)
        return None
    name = 'rust-truth.exe' if os.name == 'nt' else 'rust-truth'
    return target / 'release' / name


def truth(binary, mode, files):
    out, unread = {}, []
    for start in range(0, len(files), BATCH):
        result = subprocess.run([str(binary), mode, *files[start:start + BATCH]], capture_output=True, text=True,
                                errors='replace')
        for line in result.stdout.splitlines():
            parts = line.split()
            if parts and parts[0] == 'FILE':
                out[parts[1]] = parts[2:]
            elif parts and parts[0] in ('LEXFAIL', 'PARSEFAIL'):
                unread.append(parts[1])
    return out, unread


def patterns(source):
    """The same eight patterns `rust-truth tokens` counts, from the scanner's tokens."""
    texts, kinds, counts = source.texts, source.kinds, collections.Counter()
    for index in range(source.size):
        token = texts[index]
        if kinds[index] == 'i':
            macro = texts[index + 1] == '!' and texts[index + 2] in OPEN
            if token == 'unsafe':
                counts['unsafe'] += 1
            elif token in ('unwrap', 'unwrap_err'):
                counts['unwrap'] += bool(index and texts[index - 1] == '.' and texts[index + 1] == '('
                                         and texts[index + 2] == ')')
            elif token in ('expect', 'expect_err'):
                counts['expect_any'] += bool(index and texts[index - 1] == '.' and texts[index + 1] == '(')
            elif token in ('panic', 'unreachable'):
                counts['panic_macro'] += macro
            elif token in ('todo', 'unimplemented'):
                counts['todo'] += macro
            elif token == 'let':
                counts['let_underscore_raw'] += texts[index + 1] == '_' and texts[index + 2] == '='
            elif token == 'mod':
                counts['mod_decl'] += kinds[index + 1] == 'i' and texts[index + 2] == ';'
        elif token == '#':
            counts['allow_attr'] += ((texts[index + 1] == '[' and texts[index + 2] == 'allow' and texts[index + 3] == '(')
                                     or (texts[index + 1] == '!' and texts[index + 2] == '['
                                         and texts[index + 3] == 'allow'))
    return {name: count for name, count in counts.items() if count}


def ends(source):
    """`line:column` one past the last character of each test-only item."""
    breaks = [index for index, char in enumerate(source.text) if char == '\n']
    out = collections.Counter()
    for _, last in source.regions:
        end = source.starts[last] + len(source.texts[last])
        line = bisect.bisect_left(breaks, end - 1) + 1
        column = end - (breaks[line - 2] + 1 if line > 1 else 0)
        out[f'{line}:{column}'] += 1
    return out


def main():
    files = corpus(sys.argv[1:])
    if not files:
        print(__doc__.strip().splitlines()[0])
        print('usage: rust-hatches-live.py DIRECTORY_OR_FILE...', file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory() as scratch:
        binary = build(Path(scratch))
        if binary is None:
            print('rust-hatches-live: cargo could not build the ground-truth crate', file=sys.stderr)
            return 2
        lexed, unlexed = truth(binary, 'tokens', files)
        parsed, unparsed = truth(binary, 'extents', files)

    differing, totals = [], collections.Counter()
    items = misplaced = others = scanner_only = flags = 0
    for path in files:
        try:
            text = Path(path).read_text(encoding='utf-8')
        except (OSError, UnicodeDecodeError):
            continue
        if text.startswith('#!') and not text[2:].lstrip(' \t').startswith('['):
            # rust-truth drops a script's interpreter line before parsing, so
            # the scanner is given the same text and the lines agree.
            text = text.split('\n', 1)[1] if '\n' in text else ''
        source = _Source(path, text)
        if path in lexed:
            wanted = dict(pair.split('=') for pair in lexed[path])
            wanted = {name: int(count) for name, count in wanted.items()}
            got = patterns(source)
            totals.update(wanted)
            if wanted != got:
                differing.append((path, wanted, got))
        if path in parsed:
            flag, found = parsed[path][0], parsed[path][1:]
            flags += flag != f'inner={int(source.nonprod)}'
            remaining = ends(source)
            for entry in found:
                line, column, kind = entry.split(':')
                if kind == 'item':
                    items += 1
                    if remaining[f'{line}:{column}'] > 0:
                        remaining[f'{line}:{column}'] -= 1
                    else:
                        misplaced += 1
                        if misplaced <= 20:
                            print(f'  EXTENT {path}:{line}:{column} is where syn ends an item the scanner ends elsewhere')
                else:
                    others += 1
                    near = [key for key, count in remaining.items() if count > 0 and key.split(':')[0] == line
                            and abs(int(key.split(':')[1]) - int(column)) <= 1]
                    if near:
                        remaining[near[0]] -= 1
                    else:
                        misplaced += 1
            scanner_only += sum(count for count in remaining.values() if count > 0)

    for path, wanted, got in differing[:20]:
        names = sorted(set(wanted) | set(got))
        print(f'  LEX {path}', {name: (wanted.get(name, 0), got.get(name, 0)) for name in names
                                if wanted.get(name, 0) != got.get(name, 0)})
    print(f'rust-hatches-live: {len(files)} file(s)')
    print(f'  lexing   {len(lexed)} compared with proc-macro2, {len(differing)} differing '
          f'({len(unlexed)} it could not lex)')
    for name in sorted(totals):
        print(f'             {name:<20} {totals[name]}')
    print(f'  extents  {len(parsed)} compared with syn ({len(unparsed)} it could not parse): '
          f'{items} item(s) and {others} field/variant/arm(s), {misplaced} ended elsewhere, '
          f'{flags} whole-file disagreement(s)')
    print(f'           {scanner_only} region(s) only the scanner reports (macro bodies, expression attributes)')
    return 1 if differing or misplaced or flags else 0


if __name__ == '__main__':
    sys.exit(main())
