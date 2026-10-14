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

MARKER = re.compile(
    r'TODO|FIXME|\bXXX\b|\.unwrap\(\)|\.(skip|only)\(|\bxit\(|#\[ignore\]'
    r'|\bsorry\b|\bAdmitted\b|\{:axiom\}|\{:verify false\}|verifier::external_body'
    r'|--skip-tests|--no-verify|catch\s*(\([^)]*\))?\s*\{\s*\}', re.I)
HEDGE = re.compile(r'\bfor now\b', re.I)
STRINGS = re.compile(r'"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|`(?:[^`\\]|\\.)*`')


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
    for n, line in enumerate(sys.stdin, 1):
        if MARKER.search(line) or HEDGE.search(comments(line, Path(path).suffix)):
            if not any(p.search(line) for p in allow):
                print(f'{path}:{n}: {line.rstrip()}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
