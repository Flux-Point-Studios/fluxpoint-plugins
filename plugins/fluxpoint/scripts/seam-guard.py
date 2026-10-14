#!/usr/bin/env python3
"""Seam ratchet: a mock of code you own is an unverified contract.

`vi.mock('@/services/x')` does not test anything about `@/services/x`. It
asserts what the author BELIEVED that module does, and nothing ever checks
the belief. When the belief is wrong the suite stays green — not because
the code works, but because the test never reached it.

That is not a hypothetical. The campaign that produced this script shipped
eight defects of one shape into a feature with a green, mutation-verified
suite: the unit was correct and the thing that reaches it was not. Four
were invisible until the ledger, the compiler or an adversarial reader
refused. The measured cause was a suite that mocked its own modules three
to one over real boundaries.

Mutation testing does not catch this. A mutant only reddens a test whose
input reaches the mutated line, so a suite that mocks the caller reports
the same green either way.

So the count is recorded in a committed baseline and this script fails when
it rises. Adding a mock stops being a reflex and becomes a diff someone has
to justify — and the honest justification is usually a wire test instead.

  seam-guard.py --scan       report current counts and where they are
  seam-guard.py --baseline   record counts to .fluxpoint-proof-baseline.json
  seam-guard.py --check      fail if any category rose above the baseline

Two categories, because they fail differently:

  first_party   mocking a module in this repo. The contract is one you own
                and could have tested for real; the mock asserts a shape
                you are free to change without anything noticing.

  third_party   mocking a library. Worse in one specific way: you are
                asserting a shape someone ELSE controls, from memory. Every
                one of the eight defects above lived here — the CBOR tag
                shape, the ledger's escaping, the error class a wallet
                throws. A library's real output is a capture, never a
                recollection.

Dormant by design: a repo with no test files exits 0 in silence, exactly
like the DoD gate in a repo with no harness.

Deliberately NOT counted, because each is a real boundary rather than a
contract you could have verified:

  * `stubGlobal('fetch')`, `window.*`, `globalThis.*` — the process edge.
  * a partial mock — a factory that reaches back through importActual,
    requireActual, or vitest's importOriginal helper keeps the real module
    in the graph, so the contract is still exercised. Judged PER CALL:
    one partial mock in a file must not exempt a wall-off mock of a
    different module sitting beside it, or a single importActual would
    buy unlimited uncounted walls for the rest of that file's life.

Comments are stripped before matching, the same discipline as
proof-guard: `// we deliberately do not vi.mock('@/wallet') here` is the
opposite of a mock, and counting it fails the ratchet on exactly the diff
that explains a fix.
"""

import argparse
import json
import os
import re
import subprocess
import sys

BASELINE = ".fluxpoint-proof-baseline.json"
SECTION = "seams"

TEST_SUFFIXES = tuple(".%s.%s" % (kind, ext)
                      for kind in ("test", "spec")
                      for ext in ("ts", "tsx", "js", "jsx", "mjs", "cjs", "mts", "cts"))

# vi.mock('x') / jest.mock("x") / vi.doMock(`x`) — the specifier is what matters.
MOCK = re.compile(r"""\b(?:vi|jest)\s*\.\s*(?:do)?[Mm]ock\s*\(\s*(['"`])([^'"`]+)\1""")

# importActual / requireActual / vitest's importOriginal factory helper mean
# the real module is still in play; that is a partial mock, not a wall, and it
# does not hide the contract.
PARTIAL = re.compile(r"\bimportActual\b|\brequireActual\b|\bimportOriginal\b")

BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
LINE_COMMENT = re.compile(r"^\s*//.*$", re.M)


def strip_comments(text):
    """Full-line // comments and /* */ blocks, with newlines kept so the
    reported line numbers stay true to the file."""
    text = BLOCK_COMMENT.sub(lambda m: "\n" * m.group(0).count("\n"), text)
    return LINE_COMMENT.sub("", text)


def tracked_files(root):
    """Test files git knows about, PLUS untracked ones — mirroring
    fpl_code_dirty, and for the same reason: a mock added in a file not yet
    committed must count before the commit, or --check flips from green to
    red the moment the work lands. Falls back to a walk outside a repo."""
    try:
        out = subprocess.run(["git", "-C", root, "ls-files", "-z"],
                             capture_output=True, text=True, check=True).stdout
        out += subprocess.run(["git", "-C", root, "ls-files", "-z",
                               "--others", "--exclude-standard"],
                              capture_output=True, text=True, check=True).stdout
        names = [n for n in out.split("\0") if n]
    except Exception:
        names = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames
                           if d not in ("node_modules", ".git", "dist", "build", "out", "coverage")]
            for f in filenames:
                names.append(os.path.relpath(os.path.join(dirpath, f), root))
    return [n for n in names if n.endswith(TEST_SUFFIXES)]


def is_first_party(spec):
    """A specifier that resolves inside this repo rather than to a package."""
    return spec.startswith((".", "/", "@/", "~/", "#"))


def scan(root, files):
    counts = {"first_party": 0, "third_party": 0}
    where = {"first_party": [], "third_party": []}
    for rel in files:
        path = os.path.join(root, rel)
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        text = strip_comments(text)
        matches = list(MOCK.finditer(text))
        for i, m in enumerate(matches):
            # Each call is judged over its own stretch of the file — from this
            # mock to the next one — which in practice is its factory body. A
            # whole-file skip was a bypass: one importActual anywhere exempted
            # every wall-off mock that shared the file, forever.
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            if PARTIAL.search(text, m.start(), end):
                # A partial mock keeps the real module in the graph; the
                # contract is still exercised, so walling it off is not what
                # happened here.
                continue
            spec = m.group(2)
            key = "first_party" if is_first_party(spec) else "third_party"
            counts[key] += 1
            line = text.count("\n", 0, m.start()) + 1
            where[key].append("%s:%d  %s" % (rel, line, spec))
    return counts, where


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=".")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--scan", action="store_true")
    g.add_argument("--baseline", action="store_true")
    g.add_argument("--check", action="store_true")
    g.add_argument("--tighten", action="store_true")
    args = ap.parse_args()
    if args.tighten:
        from ratchet import cli
        return cli("seam", args.root)


    root = os.path.abspath(args.root)
    bpath = os.path.join(root, BASELINE)

    files = tracked_files(root)
    if not files:
        # Said in every mode, not only --scan: init.md tells ANY repo with
        # tests to arm this, and a --baseline that exits 0 in silence leaves
        # the operator unable to tell "armed" from "silently inapplicable".
        print("seam-guard: no JS/TS test files; dormant — nothing %s"
              % ("recorded" if args.baseline else
                 "checked" if args.check else "to scan"))
        return 0

    counts, where = scan(root, files)

    if args.scan:
        for key in ("first_party", "third_party"):
            print("seam-guard: %s = %d" % (key, counts[key]))
            for line in where[key]:
                print("    " + line)
        return 0

    doc = {}
    if os.path.exists(bpath):
        try:
            with open(bpath, encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, json.JSONDecodeError) as e:
            # The file is shared with proof-guard and spec-guard. Reading a
            # corrupt one as absent would silently disarm this ratchet on
            # --check and destroy the siblings' sections on --baseline — a
            # baseline that parses to nothing while looking configured is the
            # failure mode this plugin exists to refuse.
            print("seam-guard: %s is not readable JSON (%s)" % (bpath, e),
                  file=sys.stderr)
            return 1
        if not isinstance(doc, dict):
            print("seam-guard: %s must be a JSON object" % bpath, file=sys.stderr)
            return 1

    if args.baseline:
        doc[SECTION] = counts
        with open(bpath, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(doc, fh, indent=2, sort_keys=True)
            fh.write("\n")
        print("seam-guard: baseline armed at first_party=%d third_party=%d"
              % (counts["first_party"], counts["third_party"]))
        return 0

    base = doc.get(SECTION)
    if base is None:
        print("seam-guard: no baseline recorded; run --baseline to arm", file=sys.stderr)
        return 0
    if not isinstance(base, dict) \
       or any(not isinstance(base.get(k, 0), int) for k in counts):
        # A section someone hand-edited into the wrong shape must fail loudly,
        # not crash with a traceback and not read as unarmed.
        print("seam-guard: the '%s' section of %s is not usable; re-arm with "
              "--baseline" % (SECTION, BASELINE), file=sys.stderr)
        return 1

    risen = [(k, base.get(k, 0), counts[k]) for k in counts if counts[k] > base.get(k, 0)]
    if not risen:
        print("seam-guard: OK — first_party=%d third_party=%d, at or below baseline"
              % (counts["first_party"], counts["third_party"]))
        return 0

    for key, was, now in risen:
        print("seam-guard: FAIL — %s mocks rose %d -> %d" % (key, was, now), file=sys.stderr)
        for line in where[key]:
            print("    " + line, file=sys.stderr)
    print("", file=sys.stderr)
    print("A mock of a module you own asserts a contract nothing verifies. If the new", file=sys.stderr)
    print("mock is standing in for something the test could drive for real, drive it:", file=sys.stderr)
    print("stub only the process boundary (fetch, the wallet, the clock) and let the", file=sys.stderr)
    print("modules between be themselves. If the mock is genuinely right, re-arm with", file=sys.stderr)
    print("--baseline and say why in the commit.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
