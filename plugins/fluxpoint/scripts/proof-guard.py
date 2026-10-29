#!/usr/bin/env python3
"""Proof-strength ratchet: escape hatches may go down, never up.

A prover's exit code is a much better signal than "the code looks right",
which is why this harness is well suited to verified work. But exit 0 does
not mean what it looks like. Every proof assistant ships a way to make a
goal go away without proving it — `todo` in Aiken, `assume` and `{:axiom}`
in Dafny, `sorry` in Lean, `Admitted` in Coq, `#[verifier::external_body]`
in Verus — and a checker exits 0 just as happily on an assumed lemma as on
a discharged one. An agent asked to "make the proof pass" will find those.

So the count of escape hatches is recorded in a committed baseline and this
script fails when any category rises. Weakening a proof stops being a
judgment call and becomes a diff someone has to justify. Going the other
way is always allowed: proving something you previously assumed lowers the
count, and `--baseline` re-records it.

  proof-guard.py --scan       report current counts and where they are
  proof-guard.py --baseline   record counts to .fluxpoint-proof-baseline.json
  proof-guard.py --check      fail if any category rose above the baseline

Dormant by design: a repo with no proof-language files exits 0 in silence,
exactly like the DoD gate in a repo with no harness.

Two weakenings add no hatch at all, so both are counted structurally by
matching braces and reading the body — but only where the body is
unambiguous, because a false positive here would train people to ignore
the ratchet:

  * Gutting a test. `test t() { True }` still runs, still passes, and
    asserts nothing, so no line-based scan can see it.
  * Discharging a condition by fiat. `fn credential_matches(o, k) -> Bool
    { True }` moves an obligation into a helper that always agrees — the
    validator still reads as if it checks something, and the count of
    `todo`/`expect` never moves.

Known misses are pinned by tests rather than assumed covered — a `when x
is { _ -> True }` is vacuous but needs real expression analysis to see, so
this does not claim it.

TypeScript is ratcheted on the same argument. A type checker is a prover
with a weak logic, and `as any` is the off-chain `sorry`: the obligation
the checker had is discharged without being met, and `tsc` exits 0 either
way. Off-chain code is where an autonomous caller actually reaches a
protocol, so the erased type is the one that matters:

  ts.any          `as any`, a `: any` annotation, `any[]`, `<any>`
  ts.double_cast  `as unknown as T`, which exists only to defeat the checker
  ts.non_null     the `!` assertion, claiming what the checker could not
  ts.ts_ignore    `@ts-ignore` and `@ts-nocheck`
  flags.types_off `"strict": false` and its siblings in a tsconfig

Python sources and stubs use tokenize and ast rather than line patterns:
  py.type_ignore   type: ignore, pyright: ignore/basic/off, mypy: ignore-errors
  py.no_type_check imported typing.no_type_check decorators
  py.any           imported typing.Any in annotations, including nested types
  py.cast          calls resolving to typing.cast through explicit imports
Checker off switches in mypy.ini, setup.cfg, pyproject.toml and
pyrightconfig.json also count as flags.types_off. Imports, docstrings,
assert_type and lint-only noqa comments do not count. Variadic Any annotations
and .pyi stubs count. Python alone keeps the ratchet dormant until explicitly
baselined; existing baselines report new categories as not yet ratcheted.
This is static import resolution, not execution or a replacement for a checker.

Two deliberate exclusions, because a ratchet that cries wolf gets
re-baselined blind. `@ts-expect-error` is NOT counted: it fails the build
once the error it names goes away, so it cannot rot in place, and counting
it would push people toward the suppression that can. Generic
`eslint-disable` is not counted either — most of those lines are style
rules, and burying four type suppressions in forty style ones is how a
category stops being read.

Known misses, stated rather than assumed: `Record<string, any>` (catching
it would mean matching `any` after a comma, where it is a legal identifier
in value position), a `!` assertion followed by an operator rather than a
member access, and anything inside a block comment or a template literal
spanning lines. Single-line strings ARE removed before counting, so a
message reading "expected: any" does not register as an erased type.

What remains beyond counting: a theorem whose statement lost a conjunct, a
property proved about an unreachable state, a generator that cannot produce
the interesting case, a solver `unknown` read as success. Those need
judgment, and they are the `proof-auditor` agent's job — run
`/fluxpoint:proof-audit`, which does both passes. A ratchet is a floor,
not a ceiling.
"""
import argparse
import json
import os
import re
import subprocess
import sys
from python_hatches import CATEGORIES as PY_CATEGORIES, scan as scan_python
from compact_native import CATEGORIES as COMPACT_CATEGORIES, hatches as compact_hatches

BASELINE = ".fluxpoint-proof-baseline.json"

# (category, file suffixes, regex). Categories are namespaced by tool so a
# repo that adds a second prover ratchets each independently.
#
# These are the documented ways to discharge an obligation without proving
# it. Some are legitimate in context — an `expect` in an Aiken validator, a
# negative `fail` test — which is exactly why this ratchets rather than
# forbids: the count may hold or fall, and a rise needs a human.
# Type syntax is TypeScript's alone; the `@ts-` directives also govern a
# `// @ts-check`ed JavaScript file, so they are matched there too.
TS = (".ts", ".tsx", ".mts", ".cts")
TSJS = TS + (".js", ".jsx", ".mjs", ".cjs")

PATTERNS = [
    # --- Aiken ---------------------------------------------------------
    ("aiken.todo",          (".ak",),   r"\btodo\b"),
    ("aiken.expect",        (".ak",),   r"\bexpect\b"),
    ("aiken.trace",         (".ak",),   r"\btrace\b"),
    # --- Dafny ---------------------------------------------------------
    ("dafny.assume",        (".dfy",),  r"\bassume\b"),
    ("dafny.axiom",         (".dfy",),  r"\{:axiom\}"),
    ("dafny.verify_false",  (".dfy",),  r"\{:verify\s+false\}"),
    ("dafny.extern",        (".dfy",),  r"\{:extern\b"),
    # --- Lean ----------------------------------------------------------
    ("lean.sorry",          (".lean",), r"\bsorry\b"),
    ("lean.axiom",          (".lean",), r"^\s*axiom\b"),
    ("lean.native_decide",  (".lean",), r"\bnative_decide\b"),
    ("lean.blaster",        (".lean",), r"(?<!#)\bblaster\b|\bblasterProven\b"),
    # --- Coq / Rocq ----------------------------------------------------
    ("coq.admitted",        (".v",),    r"\b(Admitted|admit)\b"),
    ("coq.axiom",           (".v",),    r"^\s*(Axiom|Parameter|Hypothesis)\b"),
    # --- Rust verification (Verus, Prusti, Kani) ------------------------
    ("rust.external_body",  (".rs",),   r"#\[verifier::external(_body)?\]"),
    ("rust.trusted",        (".rs",),   r"#\[trusted\]"),
    # Excusing a mutant is excusing a change no test has to notice, which is
    # the same move as excusing a proof obligation — so it ratchets here
    # rather than being free to sprinkle wherever mutation-guard goes red.
    ("rust.mutants_skip",   (".rs",),   r"mutants::skip\b"),
    ("rust.mutants_exclude", (".rs",),  r"mutants::exclude_re\b"),
    ("rust.assume",         (".rs",),   r"\b(kani::assume|prusti_assume|assume)\s*\("),
    # --- Isabelle -------------------------------------------------------
    ("isabelle.sorry",      (".thy",),  r"\b(sorry|oops)\b"),
    # --- TLA+ / Apalache -------------------------------------------------
    ("tla.assume",          (".tla",),  r"^\s*ASSUME\b"),
    # --- TypeScript ------------------------------------------------------
    # `as any` is the off-chain `sorry`. Each of these discharges an
    # obligation the type checker had, and `tsc` exits 0 on all of them.
    ("ts.any",          TS,   r"\bas\s+any\b|:\s*any\b|\bany\[\]|<\s*any\s*>"),
    ("ts.double_cast",  TS,   r"\bas\s+unknown\s+as\b"),
    # The `!` assertion, told apart from negation and `!==` by what sits on
    # either side: a value on the left, a member access or a terminator on
    # the right. `return !ok` and `a !== b` are neither.
    ("ts.non_null",     TS,   r"[\w\]\)]\s*!\s*(?=[.\[,;)\}\]]|$)"),
    ("ts.ts_ignore",    TSJS, r"@ts-ignore\b|@ts-nocheck\b"),
    # --- Type checking disabled from a config file ------------------------
    ("flags.types_off", (".json",),
     r'"(?:strict|noImplicitAny|strictNullChecks|strictFunctionTypes|'
     r'strictBindCallApply|alwaysStrict|noUncheckedIndexedAccess|'
     r'useUnknownInCatchVariables)"\s*:\s*false'),
    # --- Verification disabled from the command line ---------------------
    # A flag in a script or CI config that turns the checker off is the
    # bluntest escape hatch of all, and the easiest to miss in review.
    ("flags.verification_off",
     (".sh", ".yml", ".yaml", ".toml", ".json", ".just", ".mk", "Makefile"),
     r"--(no-verify|skip-tests|skip-verification|no-check|dont-verify)\b"),
]

# Line-comment prefixes, so prose like "we assume the oracle is honest" in a
# comment does not register as an assumption in the proof.
COMMENT = {
    ".ak": "//", ".rs": "//", ".dfy": "//", ".compact": "//", ".lean": "--", ".tla": r"\\\*",
    ".thy": None, ".v": None, ".sh": "#", ".yml": "#", ".yaml": "#",
    ".toml": "#", ".just": "#", ".mk": "#", "Makefile": "#", ".json": None,
    **{s: "//" for s in TSJS},
}

# Categories whose marker IS a comment. TypeScript's strongest suppression
# is written `// @ts-ignore`, which the strip above removes before any
# pattern can see it, so these read the raw line instead.
IN_COMMENT = {"ts.ts_ignore"}

# Quoted text is data. A message reading "expected: any" must not count as
# an erased type, and a URL in a string must not truncate the line at its
# `//`. Blanked in place so a span that opens and closes on one line leaves
# the columns after it where they were; a template literal spanning lines
# is a known miss.
STRING = re.compile(r'"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|`(?:[^`\\]|\\.)*`')
STRINGED = set(TSJS)


def tracked_files(root):
    """Only git-tracked files: build output and vendored deps are not ours."""
    try:
        out = subprocess.run(
            ["git", "-C", root, "ls-files"],
            capture_output=True, text=True, check=True,
        ).stdout
    except Exception:
        return []
    return [f for f in out.splitlines() if f]


def strip_comment(line, suffix):
    marker = COMMENT.get(suffix)
    if not marker:
        return line
    m = re.search(marker, line)
    return line[: m.start()] if m else line


def strip_source(line, suffix):
    """Only code. Strings go first, so a `//` inside one is not a comment."""
    if suffix in STRINGED:
        line = STRING.sub(lambda m: " " * len(m.group(0)), line)
    return strip_comment(line, suffix)


# Categories that need structure rather than a line match, so they are
# counted by a small parser instead of a regex.
STRUCTURAL = ["aiken.vacuous_test", "aiken.constant_predicate", *PY_CATEGORIES, *COMPACT_CATEGORIES]

TEST_START = re.compile(r"^\s*test\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(")
# A body that proves nothing: a bare boolean, or a value compared to itself.
VACUOUS_BODY = re.compile(r"^(True|False|(?P<x>[A-Za-z0-9_.]+)\s*==\s*(?P=x))$")

# A predicate whose body is a bare boolean decides nothing, whatever its
# arguments say. Declared return type is required: without `-> Bool` this
# would flag constructors and helpers that merely happen to end in a literal.
CONST_FN_START = re.compile(r"^\s*(?:pub\s+)?fn\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(")
CONST_FN_TAIL = re.compile(r"^\s*->\s*Bool\s*\{")
CONST_BODY = re.compile(r"^(True|False)$")


def after_params(line, m):
    """Text following the parameter list opened at the end of match `m`.

    Parameters are matched by counting parens rather than with a `[^)]*`
    class, because they routinely contain their own: a property test's
    fuzzer (`n: Int via bounded_int(1, 99)`) and a tuple type
    (`p: (Int, Int)`) both stop such a class at the wrong paren. The class
    made every parameterised property test invisible to the vacuity scan —
    which is where a gutted body is most likely to hide, since a property
    test is the one a reader is least likely to re-read.
    """
    depth = 0
    for j in range(m.end() - 1, len(line)):
        if line[j] == "(":
            depth += 1
        elif line[j] == ")":
            depth -= 1
            if depth == 0:
                return line[j + 1:]
    return None


def body_of(lines, i):
    """Brace-matched body of the block opening on line i, comments stripped.

    Returns the inner text whitespace-collapsed, or None if the braces never
    balance (a truncated file). Shared by both structural scanners so they
    agree on what "the body" means.
    """
    depth, body = 0, []
    for j in range(i, len(lines)):
        src = strip_comment(lines[j], ".ak")
        depth += src.count("{") - src.count("}")
        body.append(src)
        if depth <= 0 and j > i:
            break
        if depth <= 0 and j == i and "}" in src:
            break
    else:
        return None
    inner = "\n".join(body)
    if "}" not in inner:
        return None
    return " ".join(inner[inner.find("{") + 1 : inner.rfind("}")].split())


def scan_vacuous_tests(path, text):
    """Aiken tests that cannot fail.

    Counting escape hatches misses the most direct way to weaken a suite:
    leave the test in place and empty it out. `test t() { True }` still runs,
    still passes, and asserts nothing — no `todo`, no `expect`, nothing for a
    line-based scan to see. This finds the unambiguous cases by matching the
    test's braces and looking at what is actually in the body. Anything less
    than obvious is left to the proof-auditor; a false positive here would
    train people to ignore the ratchet.
    """
    found = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        src = strip_comment(line, ".ak")
        m = TEST_START.match(src)
        if not m:
            continue
        rest = after_params(src, m)
        if rest is None or "{" not in rest:
            continue
        stripped = body_of(lines, i)
        if stripped is None:
            continue
        if VACUOUS_BODY.match(stripped) or not stripped:
            found.append((i + 1, m.group(1), stripped or "<empty>"))
    return found


def scan_constant_predicates(path, text):
    """Aiken predicates that always return the same answer.

    The other way to weaken a validator without adding a hatch: keep the
    call site and make the callee agree unconditionally. `spend` still reads
    as `signed_by(..) && credential_matches(..)`, so a reviewer skimming the
    validator sees a conjunction that is checked; the helper is where the
    obligation went. Only a literal `True`/`False` body is flagged, and a
    pre-existing one is absorbed into the baseline, so this fires on the
    diff that introduces one rather than on a repo that already had it.
    """
    found = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        src = strip_comment(line, ".ak")
        m = CONST_FN_START.match(src)
        if not m:
            continue
        rest = after_params(src, m)
        if rest is None or not CONST_FN_TAIL.match(rest):
            continue
        stripped = body_of(lines, i)
        if stripped and CONST_BODY.match(stripped):
            found.append((i + 1, m.group(1), stripped))
    return found


def scan(root):
    """Return (counts, hits) where hits is category -> ['path:line: text']."""
    counts = {c: 0 for c, _, _ in PATTERNS} | {c: 0 for c in STRUCTURAL}
    hits = {c: [] for c, _, _ in PATTERNS} | {c: [] for c in STRUCTURAL}
    compiled = [(c, sfx, re.compile(rx)) for c, sfx, rx in PATTERNS]
    for rel in tracked_files(root):
        base = os.path.basename(rel)
        suffix = base if base in ("Makefile",) else os.path.splitext(rel)[1]
        if not suffix:
            continue
        applicable = [(c, rx) for c, sfx, rx in compiled if suffix in sfx]
        python_input = suffix in ('.py', '.pyi') or base in ('mypy.ini', '.mypy.ini', 'setup.cfg', 'pyproject.toml', 'pyrightconfig.json')
        if not applicable and not python_input and suffix != '.compact':
            continue
        path = os.path.join(root, rel)
        try:
            with open(path, errors="replace", encoding="utf-8") as fh:
                lines = fh.readlines()
        except OSError:
            continue
        if suffix == '.compact':
            disclosure_path = os.path.join(root, '.fluxpoint-compact-disclosures.json')
            disclosures = {}
            if os.path.isfile(disclosure_path):
                with open(disclosure_path, encoding='utf-8') as source:
                    disclosures = json.load(source)
                if not isinstance(disclosures, dict): raise ValueError('Compact disclosures must map file:line to reasons')
            for cat, n, raw in compact_hatches(rel, ''.join(lines), disclosures):
                counts[cat] += 1
                if len(hits[cat]) < 20: hits[cat].append(f"{rel}:{n}: {raw.strip()[:100]}")
        if python_input:
            for cat, n, raw in scan_python(rel, ''.join(lines)):
                counts[cat] += 1
                if len(hits[cat]) < 20:
                    hits[cat].append(f"{rel}:{n}: {raw.strip()[:100]}")
        for n, raw in enumerate(lines, 1):
            line = strip_source(raw, suffix)
            for cat, rx in applicable:
                if rx.search(raw if cat in IN_COMMENT else line):
                    counts[cat] += 1
                    if len(hits[cat]) < 20:
                        hits[cat].append(f"{rel}:{n}: {raw.strip()[:100]}")
        if suffix == ".ak":
            text = "".join(lines)
            for lineno, name, body in scan_vacuous_tests(path, text):
                counts["aiken.vacuous_test"] += 1
                if len(hits["aiken.vacuous_test"]) < 20:
                    hits["aiken.vacuous_test"].append(
                        f"{rel}:{lineno}: test {name} body is `{body}` — cannot fail"
                    )
            for lineno, name, body in scan_constant_predicates(path, text):
                counts["aiken.constant_predicate"] += 1
                if len(hits["aiken.constant_predicate"]) < 20:
                    hits["aiken.constant_predicate"].append(
                        f"{rel}:{lineno}: fn {name} always returns `{body}` — decides nothing"
                    )
    return counts, hits


# Suffixes that carry categories without arming the ratchet on their own. A
# CI file or a tsconfig is not a reason to tell a repo it has proof work, and
# plain JavaScript reaches the `@ts-` directives without being type-checked
# at all — arming there would red a repo that never asked for a type gate.
NOT_ARMING = {".sh", ".yml", ".yaml", ".toml", ".json", ".just", ".mk",
              ".js", ".jsx", ".mjs", ".cjs"}


def applicable(counts, root):
    """True when this repo has anything worth ratcheting."""
    suffixes = {os.path.splitext(f)[1] for f in tracked_files(root)}
    proof_suffixes = {s for _, sfx, _ in PATTERNS for s in sfx if s.startswith(".")}
    proof_suffixes.add('.compact')
    return bool(suffixes & (proof_suffixes - NOT_ARMING))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=".")
    ap.add_argument("--require-armed", action="store_true",
                    help="fail --check when the ratchet or an active category is unarmed")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--scan", action="store_true")
    g.add_argument("--baseline", action="store_true")
    g.add_argument("--check", action="store_true")
    g.add_argument("--tighten", action="store_true")
    args = ap.parse_args()
    if args.tighten:
        from ratchet import cli
        return cli("proof", args.root)

    root = args.root
    bpath = os.path.join(root, BASELINE)

    counts, hits = scan(root)
    nonzero = {k: v for k, v in counts.items() if v}

    if args.scan:
        if not applicable(counts, root):
            print("proof-guard: no proof-language files tracked — dormant")
            return 0
        print("proof-guard: escape hatches by category")
        for cat in sorted(counts):
            if counts[cat]:
                print(f"  {cat:<26} {counts[cat]}")
                for h in hits[cat][:5]:
                    print(f"      {h}")
        if not nonzero:
            print("  none found")
        return 0

    if args.baseline:
        # spec-guard.py owns the `spec` section of this same file. Rewriting
        # the document wholesale would silently disarm the statement ratchet
        # every time someone re-recorded the hatch counts, so preserve
        # whatever else is in there.
        doc = {}
        if os.path.exists(bpath):
            try:
                with open(bpath, encoding="utf-8") as fh:
                    doc = json.load(fh)
            except (OSError, json.JSONDecodeError):
                doc = {}
        doc["version"] = 1
        doc["note"] = (
            "Escape-hatch counts. proof-guard.py --check fails if any "
            "category rises. Lowering one (proving what was assumed) is "
            "always allowed; re-record with --baseline and explain the "
            "rise in review if a count must go up."
        )
        doc["counts"] = counts
        with open(bpath, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(doc, fh, indent=2, sort_keys=True)
            fh.write("\n")
        print(f"proof-guard: wrote {BASELINE} ({sum(counts.values())} hatch(es) recorded)")
        return 0

    # --check
    if not os.path.exists(bpath):
        if not args.require_armed and not applicable(counts, root):
            return 0  # dormant, like the gate in a repo with no harness
        print(
            f"proof-guard: proof files are tracked but {BASELINE} is absent, so the "
            f"ratchet is NOT armed. Run: proof-guard.py --baseline",
            file=sys.stderr,
        )
        return 1 if args.require_armed else 0

    with open(bpath, encoding="utf-8") as fh:
        doc = json.load(fh)
    if "counts" not in doc:
        # The file is shared with spec-guard and seam-guard, so its presence
        # proves SOME ratchet armed — not this one. Reading an absent section
        # as an armed all-zero baseline made `seam-guard.py --baseline` in a
        # plain JS repo turn the first `--no-verify` in a tracked workflow
        # file into a proof-guard RED, in a repo with zero proof files.
        # An absent section is the absent-file case.
        if not args.require_armed and not applicable(counts, root):
            return 0
        print(
            f"proof-guard: proof files are tracked but {BASELINE} has no hatch "
            f"counts, so the ratchet is NOT armed. Run: proof-guard.py --baseline",
            file=sys.stderr,
        )
        return 1 if args.require_armed else 0
    base = doc.get("counts", {})

    # A category the baseline never recorded is NEW since it was written —
    # this plugin added it — so its first sighting is not a rise. Reading an
    # absent key as a recorded zero makes every category the plugin ever
    # ships a red gate for every repo that armed before it, on an upgrade
    # those repos did not ask for and a diff they did not write. The first
    # run names it and tells them to arm it; from the next baseline on it
    # ratchets like any other.
    unratcheted = sorted(c for c in counts if c not in base and counts[c])
    risen = [
        (cat, base[cat], counts[cat])
        for cat in sorted(counts)
        if cat in base and counts[cat] > base[cat]
    ]
    if unratcheted:
        print("proof-guard: categories added to this plugin since the baseline "
              "was recorded, counted but NOT yet ratcheted:")
        for cat in unratcheted:
            print(f"  {cat}: {counts[cat]} (new)")
            for h in hits[cat][:3]:
                print(f"      {h}")
        print("  Re-record with --baseline to hold them down.")
        if args.require_armed:
            return 1
    if risen:
        print("proof-guard: RED — proof strength decreased\n", file=sys.stderr)
        for cat, was, now in risen:
            print(f"  {cat}: {was} -> {now}", file=sys.stderr)
            for h in hits[cat][:8]:
                print(f"      {h}", file=sys.stderr)
        print(
            "\n  An escape hatch discharges a goal without proving it, so the checker "
            "still exits 0.\n  Prove it, or justify the rise in review and re-record "
            "with --baseline.",
            file=sys.stderr,
        )
        return 1

    fell = [(c, base.get(c, 0), counts[c]) for c in sorted(counts) if counts[c] < base.get(c, 0)]
    if fell:
        print("proof-guard: green — and stronger than the baseline:")
        for cat, was, now in fell:
            print(f"  {cat}: {was} -> {now} (run --tighten on a clean green tree to lock it in)")
    else:
        print(f"proof-guard: green ({sum(counts.values())} hatch(es), none risen)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
