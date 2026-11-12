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
# A comment that opens with the word is a marker in any case: `// todo: x`,
# `# fixme`, `/* fixme */`. So are the tags JSDoc, PHPDoc and Doxygen spell in
# lower case, `@todo` and `\todo`. Both are read in the comment alone, which
# leaves out "a todo list" in a sentence, a name in code and a decorator. A
# word joined to dashes is a flag, `--todo`, except in the languages whose
# comments open with `--`.
_OPENS = (r'^(?:\s|[#/*!;(<-])*', r'(?:todo|fixme)(?=\s*(?:[:(]|\*/|\*\)|-->|$))')
OPENS = re.compile(_OPENS[0] + r'(?<!-)' + _OPENS[1], re.I)
OPENS_DASHED = re.compile(_OPENS[0] + _OPENS[1], re.I)
DASH_SUFFIXES = {'.lua', '.sql', '.hs', '.lhs', '.elm', '.purs', '.idr', '.agda', '.ada', '.adb', '.ads',
                 '.vhd', '.vhdl', '.lean', '.applescript'}
# Where `#` is the one comment form, read anywhere outside a string, as the
# hedge has always read it.
HASH_ONLY = ('.sh', '.bash', '.rb', '.yaml', '.yml', '.toml')
# Where `#` opens a comment beside the other forms: build files, configuration
# and more scripts. In C, CSS and markup it does not (#define, an id selector,
# an anchor), so they stay out. `$#`, `${#` and a fragment after a word are no
# comment here.
HASH_SUFFIXES = {'.zsh', '.ksh', '.fish', '.pl', '.pm', '.r', '.ps1', '.psm1', '.tf', '.tfvars', '.ex', '.exs',
                 '.mk', '.cmake', '.nix', '.jl', '.tcl', '.awk', '.cfg', '.conf', '.ini', '.properties', '.env',
                 '.dockerfile'}
HASH_NAMES = {'makefile', 'gnumakefile', 'dockerfile', 'containerfile', 'rakefile', 'gemfile', 'justfile',
              'vagrantfile', 'cmakelists.txt', '.gitignore', '.dockerignore', '.env'}
# Where a line that starts with `;` is a comment.
SEMICOLON_SUFFIXES = {'.ini', '.cfg', '.conf', '.el', '.lisp', '.clj', '.scm', '.asm'}
# `//` after a scheme is the rest of a URL, http://todo:8080, and opens no
# comment. After a label it does when a space follows: `default:// todo`.
PLAIN = r'(?<![A-Za-z]:)//|(?<=[A-Za-z]:)//(?=\s)|/\*|^\s*\*|--|\(\*'
TAG = re.compile(r'(?:^|(?<=[\s*/!(]))[@\\]todo(?=[\s:*]|$)')
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
    m = re.search(r'#' if suffix in HASH_ONLY else PLAIN, masked)
    return line[m.start():] if m else ''


def marker_comment(line, path):
    """The comment on a line, as the marker rules read it.

    Every form the hedge is read in, and more, since a marker is written
    wherever a comment can be: `#` in build files, configuration and more
    scripts, beside their other forms; `;` at the start of a line in the
    files that use that; and `<!-- -->`. Nothing the hedge reads is left
    out, so no comment that was a marker before stops being one.
    """
    suffix, name = Path(path).suffix.lower(), Path(path).name.lower()
    if suffix in ('.py', '.pyi'):
        return comments(line, suffix)
    masked = STRINGS.sub(lambda m: ' ' * len(m.group()), line)
    if suffix in HASH_ONLY:
        marker = r'#'
    else:
        marker = PLAIN + r'|<!--'
        if suffix in HASH_SUFFIXES or name in HASH_NAMES:
            marker = r'(?<![A-Za-z_$&{])#|' + marker
    if suffix in SEMICOLON_SUFFIXES:
        marker += r'|^\s*;'
    m = re.search(marker, masked)
    if not m:
        return ''
    # The markup opener holds dashes of its own, so the text starts after it.
    return line[m.end():] if m.group() == '<!--' else line[m.start():]


def flagged(line, path):
    """Whether one added line of this file holds a marker, an escape or a hedge."""
    suffix = Path(path).suffix
    comment = marker_comment(line, path)
    opens = OPENS_DASHED if suffix.lower() in DASH_SUFFIXES else OPENS
    return bool(marked(line, suffix.lower()) or HEDGE.search(comments(line, suffix))
                or opens.search(comment) or TAG.search(comment))


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
        if flagged(line, path) and not any(p.search(line) for p in allow):
            print(f'{path}:{n}: {line.rstrip()}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
