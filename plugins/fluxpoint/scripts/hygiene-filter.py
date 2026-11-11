#!/usr/bin/env python3
"""Filter added source lines, keeping prose hedges scoped to comments.

.fluxpoint-hygiene-allow contains one Python regex per line, matched against
source text (without diff prefixes). It narrows the scan, never its arming
predicate; changes to this policy are surfaced by the gate's trust warning.
"""
import io
from pathlib import Path
import re
import sys
import tokenize

# Spelled one way by a language or a convention, so matched in that spelling:
# the comment markers, Coq's `Admitted` and Dafny's two attributes. Any other
# casing is an ordinary word or identifier ("a todo list", "re-admitted", a
# masked "xx-xxx-1234").
MARKER = re.compile(r'TODO|FIXME|\bXXX\b|\bAdmitted\b|\{:axiom\}|\{:verify false\}')
# Calls, attributes and flags match in any case. `todo!` is listed because a
# case-sensitive TODO no longer reaches the Rust macro.
CALL = re.compile(
    r'\.unwrap\(\)|\.(skip|only)\(|\bxit\(|#\[ignore(?:\s*=[^\]]*)?\]'
    r'|\btodo!|\bunimplemented!|verifier::external_body'
    r'|--skip-tests|--no-verify|catch\s*(\([^)]*\))?\s*\{\s*\}', re.I)
# A keyword in Lean and Isabelle and an English word in every other file, in
# the same lower case, so the file type decides.
SORRY = re.compile(r'\bsorry\b')
HEDGE = re.compile(r'\bfor now\b', re.I)
STRINGS = re.compile(r'"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|`(?:[^`\\]|\\.)*`')


def marked(line, suffix):
    if MARKER.search(line) or CALL.search(line):
        return True
    if suffix == '.lean':
        # A line that opens with `--` and closes neither a string nor a block
        # comment holds no code: it is a line comment or lies inside one of
        # those. Every other line counts, a trailing comment and a string
        # included, because one added line cannot show what surrounds it.
        text = line.lstrip()
        if text.startswith('--') and '"' not in text and '-/' not in text:
            return False
        return bool(SORRY.search(line))
    return suffix == '.thy' and bool(SORRY.search(line))


def comments(line, suffix):
    if suffix in ('.py', '.pyi'):
        try:
            return ' '.join(t.string for t in tokenize.generate_tokens(io.StringIO(line).readline)
                            if t.type == tokenize.COMMENT)
        except (tokenize.TokenError, IndentationError):
            return ''
    # Mask strings before locating comment syntax; identifiers and literals
    # like `for now in nows` / "for now" are not prose hedges.
    masked = STRINGS.sub(lambda m: ' ' * len(m.group()), line)
    marker = r'#' if suffix in ('.sh', '.bash', '.rb', '.yaml', '.yml', '.toml') else r'//|/\*|^\s*\*|--|\(\*'
    m = re.search(marker, masked)
    return line[m.start():] if m else ''


def main():
    path = sys.argv[1]
    allow = []
    try:
        policy = Path('.fluxpoint-hygiene-allow').read_text(encoding='utf-8').splitlines()
    except FileNotFoundError:
        policy = []
    for n, line in enumerate(policy, 1):
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        try:
            allow.append(re.compile(line))
        except re.error as e:
            print(f'.fluxpoint-hygiene-allow:{n}: invalid regex: {e}')
            return 1
    suffix = Path(path).suffix
    for n, line in enumerate(sys.stdin, 1):
        if marked(line, suffix.lower()) or HEDGE.search(comments(line, suffix)):
            if not any(p.search(line) for p in allow):
                print(f'{path}:{n}: {line.rstrip()}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
