#!/usr/bin/env python3
"""Counterexample ledger: a prover's failing input outlives the run that found it.

The most valuable thing a prover ever produces is the shrunk input that
breaks the property. It is minimal, concrete, and reproducible — and it
lives in a log file the next command overwrites. So the same bug is
rediscovered, re-shrunk, and re-explained from scratch, and a fix ships
with no permanent evidence that the case it fixed is still handled.

This records it, and makes the fix permanent:

  cex.py --ingest --tool T --from FILE --exit N   record failures from prover output
  cex.py --pin <cexId>                     write the evidence and a draft
  cex.py --pin <cexId> --file F --test-name T   record the regression test
  cex.py --check                           a pinned counterexample still bites
  cex.py --retire <cexId> --reason "..."   release the ratchet, with a diff
  cex.py --list                            what is recorded, for humans

The store is `.fluxpoint-cex.jsonl` at the repo root, next to
`.fluxpoint-proof-baseline.json` and `.fluxpoint-pairs.json` and for the
same reason: it is a ratchet, so it has to be committed. Everything under
`.claude/fluxpoint/` is gitignored by this plugin's own installer, and a
ratchet nobody else can see is a ratchet that only fails on the machine
that found the bug.

## Provers

One ledger, one pinning discipline, one parser per prover. `TOOLS` is a
registry: each entry knows how to read that prover's output into rows and
how to read a regression test written in that prover's language. The
ledger, the containment rule, the refusal to mint a counterexample from a
green test, and the INGEST-FAILED inbox row are shared, and a second parser
must not relax any of them.

### aiken

There is no `--json`. Aiken picks its output listener at runtime: when
stdout is not a TTY it emits structured JSON and sends every diagnostic to
stderr. A plain redirect therefore yields the machine form for free, and
nothing here parses a terminal box. The published `--show-json-schema` is
wrong in two ways that break a schema-driven parser — it declares a `kind`
discriminator that is never emitted, and names the array `test` where the
real key is `tests` — so kinds are told apart by which fields are present.

### dafny

`dafny verify` prints one line per failed obligation —
`file.dfy(LINE,COL): Error: <what could not be proved>` — and, when run
with `--extract-counterexample` (legacy `/extractCounterexample`), a block
headed `Counterexample for first failing assertion:` made of state blocks:
a location line `file.dfy(LINE,COL): initial state:` followed by indented
`name : type = value` lines, one state per program point. Dafny 4 renders
the same model as `assume name == value && ...;` statements meant to be
pasted into the code. The parser reads both spellings and keeps the
`initial state` block, which is the method's inputs — the shrunk-input
equivalent. Dafny extracts a model for the FIRST failing assertion only;
later failures are recorded without one. A `Counterexample` heading with no
assignment under it is parser drift and files INGEST-FAILED rather than
reading as a run with no failures. Resolution and type errors also print
as `Error:` lines; only messages that name a verification failure ("could
not be proved", "might not hold", …) become rows.

### kani

`cargo kani` prints, per harness, a `RESULTS:` block of numbered checks
(`Check 3: my_harness.assertion.1` / `- Status: FAILURE` / `- Description:
"…"` / `- Location: src/lib.rs:12:5 in function my_harness`), a `Failed
Checks:` summary with a `File: "…", line N, in <fn>` line, and a verdict
line `VERIFICATION:- FAILED` (or `SUCCESSFUL`). With `-Z concrete-playback
--concrete-playback=print` it also prints a generated Rust unit test
introduced by ``Concrete playback unit test for `my_harness`:`` whose
`concrete_vals` vector carries one `// <value>` comment per input (the
interpreted value, in order) above the raw bytes `vec![…]`. The run ends
with a `Summary:` block: `Verification failed for - <harness>` per failure
and `Complete - N successfully verified harnesses, M failures, T total.`
One row per failed harness; the comment values are the recorded input
(`255, 1` — Kani does not name them), and the byte vectors are kept as an
alternative spelling so the test Kani itself writes into the source under
`--concrete-playback=inplace` is a legal pin. A build error prints no
verdict line at all and records nothing, said aloud. A `Complete` line
counting more failures than rows were extracted is parser drift.

### apalache

`apalache-mc check --inv=Inv Spec.tla` exits 12 on a violation
(`EXITCODE: ERROR (12)`) and names the trace files on stdout — any line
carrying a path ending in `.itf.json` is read, and `--from` may point at
the ITF file itself. ITF (Informal Trace Format): `#meta` (with `source`
and `description`), `vars`, and `states`, each state a map from variable
to value with `#meta.index`; values are plain JSON scalars or the tagged
forms `{"#bigint": "…"}`, `{"#set": […]}`, `{"#map": [[k, v], …]}`,
`{"#tup": […]}`, `{"#unserializable": "…"}`, and records as plain
objects. One row per trace; `input` renders every state in order
(`x = 1, y = {1, 2} ; x = 2, y = {1, 2, 3}`), and containment is judged on
the FIRST state — the initial-state block is the shrunk-input equivalent
here as it is for Dafny, and a TLA+ regression is written as an `Init`
constrained to it, not as a paste of the whole trace.

## What a pin has to survive

`--pin` cannot write the regression test itself: the recorded value does
not carry the test's parameter type or the predicate it broke, so a
generated test would be a guess, and a wrong guess is a non-compiling file
dropped into a source tree that was merely red. The agent writes the test;
this tool refuses to record it unless the recorded value is physically in
it. Specifically, a pin is rejected when:

  * the file is untracked, outside a directory the prover compiles (Aiken:
    `lib/`, `validators/`, `env/`; Dafny: any tracked `.dfy`; Kani: any
    tracked `.rs`; Apalache: any tracked `.tla`), or under `.claude/`
    (where this tool writes its own draft — a tool must not mint the
    artifact that satisfies its own check),
  * the named test inverts or disables the oracle: an Aiken test annotated
    `fail`, which passes *because* the predicate is broken and goes red the
    day someone fixes it; a Dafny declaration carrying `{:verify false}` or
    `{:axiom}`, or a body containing `assume`, which makes anything verify;
    a Rust fn under `#[should_panic]` or `#[ignore]`, or one with neither
    `#[test]` nor `#[kani::proof]`, which nothing ever runs; a TLA+
    operator introduced by `ASSUME`, which the checker takes as given,
  * the recorded literals are absent from that test's own body, in order,
    on token boundaries — not merely somewhere in the file, where a
    counterexample of `0` or `True` would match by accident,
  * or the body is hollow: a bare boolean, a self-comparison, a value bound
    and then ignored (`let v = <value> True`), or an `assert true`.

Comments and string literals are stripped before any of that, or pasting
the value into a comment would satisfy it. For Kani and Apalache each
string literal is replaced by a digest of its contents rather than
blanked, and a string-valued leaf is digested the same way before the
search: a `pc = "L1"` state stays pinnable, and still nothing else can
hide inside a string.

## What this does NOT claim

A pinned test is recorded as *containing* the counterexample. It is not
re-run against the un-fixed code to prove it would have caught it. That is
the honest gap, it is why `signature` is in the schema already, and it is
the next increment — until then a pin means "the evidence is in the tree",
not "the regression bites".
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import subprocess
import sys

from process_tree import shell_argv

CEX = ".fluxpoint-cex.jsonl"
EVIDENCE = ".fluxpoint-cex"
SCRATCH = os.path.join(".claude", "fluxpoint", "cex")
# `fixed` and `superseded` are declared so the schema never needs migrating
# and unreachable on purpose: the only honest way to set `fixed` is to re-run
# the pinned test against the un-fixed code, which this slice does not do.
# A status an agent could set by assertion is a self-report where an exit
# code belongs.
STATUSES = {"open", "pinned", "retired", "fixed", "superseded"}
COMPILE_ROOTS = ("lib/", "validators/", "env/")

AK_COMMENT = re.compile(r"//[^\n]*")
AK_STRING = re.compile(r'@"(?:[^"\\]|\\.)*"')
TEST_HEAD = re.compile(r"(?:^|\n)[ \t]*test[ \t]+([A-Za-z_][A-Za-z0-9_]*)[ \t]*\(")
# Ordered literal leaves of a reified Aiken value. Bytearrays and strings
# first so their contents are not re-tokenized as integers.
LEAF = re.compile(
    r'(#"[0-9a-fA-F]*")'
    r'|(@"(?:[^"\\]|\\.)*")'
    r"|(-?\d+)"
    r"|([A-Z][A-Za-z0-9_]*)"
)
VACUOUS = re.compile(r"^(True|False|(?P<x>[A-Za-z0-9_.]+)\s*==\s*(?P=x))$")
TAIL_OPS = {"==", "!=", "&&", "||", ">", "<", ">=", "<=", "?", "|>"}

# ---- dafny ------------------------------------------------------------------
DFY_COMMENT = re.compile(r"//[^\n]*|/\*.*?\*/", re.S)
DFY_STRING = re.compile(r'@"(?:[^"]|"")*"|"(?:[^"\\\n]|\\.)*"')
DFY_ERROR = re.compile(
    r"^(?P<file>[^\s(]+\.dfy)\((?P<line>\d+),(?P<col>\d+)\):\s*Error:\s*(?P<msg>.+?)\s*$",
    re.M)
# Only these are counterexamples. A resolution error ("unresolved
# identifier") prints in the same shape and records nothing about the code's
# behaviour, so it must not be minted as a failing input.
DFY_VERIFICATION = re.compile(
    r"could not be proved|might not|cannot prove|could not prove|"
    r"is not proved|violat|does not hold|not maintained|not established|"
    r"nontermination|decreases", re.I)
DFY_SUMMARY = re.compile(r"verifier finished with \d+ verified, (?P<n>\d+) error", re.I)
# Three spellings of the heading, all seen in real output: Dafny 3 prints
# `Counterexample for first failing assertion:`, Dafny 4.9 prints
# ` Related counterexample:` under the Error line (indented, and followed
# by a WARNING that the model may be inconsistent), older 4.x a bare
# `Counterexample:`.
DFY_CEX_HEAD = re.compile(
    r"^\s*(?:Related\s+)?[Cc]ounterexample(?: for first failing assertion)?:\s*$", re.M)
DFY_STATE = re.compile(
    r"^\s*(?P<file>[^\s(]+\.dfy)\((?P<line>\d+),(?P<col>\d+)\)(?::\s*(?P<label>[^:\n]*?))?\s*:\s*$")
DFY_ASSIGN = re.compile(
    r"^\s+(?P<name>[A-Za-z_][\w'#$]*)\s*:\s*(?P<type>[^=\n]+?)\s*=\s*(?P<value>.+?)\s*$")
DFY_ASSUME = re.compile(r"^\s*assume\s+(?P<expr>.+?)\s*;\s*$")
# Dafny 4.9 writes the literal on the left (`assume 0 == bal && 1 == amt`),
# earlier 4.x the name; both orders are read and recorded as name == value.
DFY_CONJUNCT = re.compile(
    r"(?:(?P<name>[A-Za-z_][\w'#$]*)\s*==\s*(?P<value>[^&]+?)"
    r"|(?P<lvalue>[^&=]+?)\s*==\s*(?P<lname>[A-Za-z_][\w'#$]*))\s*(?=&&|$)")
DFY_DECL = re.compile(
    r"^\s*(?:ghost\s+|static\s+|twostate\s+)*"
    r"(?:method|function|lemma|predicate|function method|greatest lemma|least lemma)"
    r"(?:\s*\{:[^}]*\})*\s+([A-Za-z_][A-Za-z0-9_']*)")
DFY_LEAF = re.compile(
    r'("(?:[^"\\]|\\.)*")'
    r"|('(?:[^'\\]|\\.)')"
    r"|(-?\d+(?:\.\d+)?)"
    r"|\b(true|false|null)\b"
    r"|\b([A-Z][A-Za-z0-9_]*)\b"
)
DFY_VACUOUS = re.compile(r"^(?:(?:assert|expect)\s+true\s*;?\s*)+$")

# ---- kani -------------------------------------------------------------------
RS_COMMENT = re.compile(r"//[^\n]*|/\*.*?\*/", re.S)
# Ordinary, byte, and raw string literals. The contents are captured so a
# digest can stand in for them (see _digest_strings).
RS_STRING = re.compile(
    r'b?r(?P<h>#*)"(?P<raw>[\s\S]*?)"(?P=h)'
    r'|b?"(?P<s>(?:[^"\\\n]|\\.)*)"')
RS_LEAF = re.compile(
    r'("(?:[^"\\]|\\.)*")'
    r"|('(?:[^'\\]|\\.)')"
    r"|(-?\d+(?:\.\d+)?)"
    r"|\b(true|false)\b"
    r"|\b([A-Z][A-Za-z0-9_]*)\b"
)
RS_VACUOUS = re.compile(
    r"^(?:(?:assert!\s*\(\s*true\s*\)|assert_eq!\s*\(\s*true\s*,\s*true\s*\))\s*;?\s*)+$")
# An identifier or macro followed by `(`: the body reaches some code. Control
# keywords are not calls.
RS_CALL = re.compile(
    r"\b(?!(?:if|while|for|match|let|return|loop|in|as|unsafe|else)\b)"
    r"[A-Za-z_][\w:]*!?\s*\(")
RS_TEST_ATTR = re.compile(r"#\[\s*(?:[\w:]+::)?test\s*[\](]|kani::proof")
RS_INVERTED_ATTR = re.compile(r"#\[\s*(?:should_panic|ignore)\s*[\](]")
KANI_HARNESS_START = re.compile(r"^Checking harness (?P<name>\S+?)\.\.\.\s*$", re.M)
KANI_VERDICT = re.compile(r"^VERIFICATION:-\s*(?P<v>FAILED|SUCCESSFUL)", re.M)
KANI_CHECK = re.compile(
    r"^Check \d+: (?P<name>\S+)[ \t]*\n"
    r"[ \t]*- Status: (?P<status>\S+)[ \t]*\n"
    r"[ \t]*- Description: \"(?P<desc>[^\n]*)\"[ \t]*"
    r"(?:\n[ \t]*- Location: (?P<loc>[^\n]*?)[ \t]*(?=\n|$))?", re.M)
KANI_LOCATION = re.compile(
    r"(?P<file>[^\s:]+):(?P<line>\d+):(?P<col>\d+)(?:\s+in function\s+(?P<fn>\S+))?")
KANI_FAILED_CHECK = re.compile(
    r"^Failed Checks: (?P<desc>[^\n]*)\n[ \t]*File: \"(?P<file>[^\"\n]+)\", line "
    r"(?P<line>\d+), in (?P<fn>\S+)", re.M)
KANI_PLAYBACK = re.compile(
    r"^Concrete playback unit test for `(?P<name>[^`\n]+)`:[ \t]*\n"
    r"(?P<body>.*?)kani::concrete_playback_run\(", re.M | re.S)
KANI_PLAYBACK_VAL = re.compile(r"^[ \t]*//[ \t]*(?P<v>.*?)[ \t]*\n[ \t]*vec!\[(?P<bytes>[^\]]*)\]", re.M)
KANI_FAILED_FOR = re.compile(r"^Verification failed for - (?P<name>\S+)[ \t]*$", re.M)
KANI_COMPLETE = re.compile(
    r"^Complete - \d+ successfully verified harness(?:es)?, (?P<n>\d+) failures?, "
    r"\d+ total", re.M)
# rustc/cargo diagnostics: the run never reached a harness.
RS_BUILD_ERROR = re.compile(r"^\s*error(?:\[E\d+\])?:", re.M)

# ---- apalache ---------------------------------------------------------------
TLA_COMMENT = re.compile(r"\\\*[^\n]*|\(\*.*?\*\)", re.S)
TLA_STRING = re.compile(r'"(?P<s>(?:[^"\\\n]|\\.)*)"')
TLA_LEAF = re.compile(
    r'("(?:[^"\\]|\\.)*")'
    r"|(-?\d+)"
    r"|\b(TRUE|FALSE)\b"
)
TLA_VACUOUS = re.compile(r"^(TRUE|FALSE)$")
TLA_DEF = re.compile(
    r"(?:^|\n)[ \t]*(?:(?P<kw>ASSUME|AXIOM|THEOREM|LEMMA)[ \t]+)?"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)[ \t]*(?:\([^)]*\))?[ \t]*==")
# Where an operator body ends: the next column-0 definition or declaration,
# a separator line, or the module end.
TLA_DEF_END = re.compile(
    r"\n(?=[A-Za-z_][A-Za-z0-9_]*[ \t]*(?:\([^)]*\))?[ \t]*==|"
    r"(?:ASSUME|AXIOM|THEOREM|LEMMA|VARIABLES?|CONSTANTS?|EXTENDS|INSTANCE|LOCAL|"
    r"RECURSIVE)\b|-{4,}|[ \t]*={4,})")
ITF_PATH = re.compile(r"(?P<path>[^\s,\"'<>]+\.itf\.json)")
AP_EXITCODE = re.compile(r"EXITCODE:\s*(?:ERROR\s*\()?(?P<n>\d+)", re.I)
AP_VIOLATION = re.compile(r"invariant[^\n]*violated|Check the (?:trace|counterexample)", re.I)

# ---- fast-check (off-chain property tests, through the vitest reporter) -----
# Jest and a bare `fc.assert` throw print the same error body in a different
# frame; only the vitest reporter's ` FAIL file > describe > it` header is
# parsed, and anything else is named rather than guessed at.
TS_COMMENT = re.compile(r"//[^\n]*|/\*.*?\*/", re.S)
TS_STRING = re.compile(r'"(?P<s>(?:[^"\\\n]|\\.)*)"'
                       r"|'(?P<t>(?:[^'\\\n]|\\.)*)'"
                       r"|`(?P<u>(?:[^`\\]|\\.)*)`", re.S)
TS_LEAF = re.compile(
    r'("(?:[^"\\]|\\.)*")'
    r"|('(?:[^'\\]|\\.)*')"
    r"|(-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?n?)"
    r"|\b(true|false|null|undefined|NaN|Infinity)\b"
)
TS_VACUOUS = re.compile(
    r"^(?:expect\s*\(\s*(?P<x>true|1)\s*\)\s*\.\s*to(?:Be|Equal|BeTruthy)\s*\([^)]*\)\s*;?\s*)+$")
TS_CALL = re.compile(r"[A-Za-z_$][\w$]*\s*\(")
# `.skip`/`.todo` never run; `.fails`/`.failing` invert the oracle, passing
# because the code is still broken.
TS_TEST_HEAD = re.compile(
    r"\b(?P<x>x)?(?P<fn>it|test)(?P<mod>(?:\.\w+)*)\s*\(\s*(?P<q>[\"'`])")
TS_NAMED = re.compile(
    r"(?:^|\n)[ \t]*(?:export[ \t]+)?(?:async[ \t]+)?"
    r"(?:function[ \t]+(?P<f>{name})\b|(?:const|let|var)[ \t]+(?P<c>{name})\b[^\n=]*=)")
FC_BLOCK = re.compile(r"^[ \t]*FAIL[ \t]+(?P<sel>\S+.*?)[ \t]*$", re.M)
FC_HEADER = re.compile(r"Failed Tests[ \t]+(?P<n>\d+)")
FC_PROPERTY = re.compile(r"Property failed after (?P<n>\d+) test")
FC_REPLAY = re.compile(r"\{\s*seed:\s*(?P<seed>-?\d+)\s*,\s*path:\s*\"(?P<path>[^\"]*)\"")
FC_CEX = re.compile(r"^[ \t]*Counterexample:[ \t]*(?P<value>.+?)[ \t]*$", re.M)
FC_SHRUNK = re.compile(r"Shrunk (?P<n>\d+) time")
FC_CAUSE = re.compile(r"^[ \t]*Caused by:[ \t]*(?P<msg>.+?)[ \t]*$", re.M)
# The run never reached a test: nothing to record, and not parser drift.
FC_NO_RUN = re.compile(
    r"No test files found|Failed to load|Transform failed|SyntaxError|"
    r"Cannot find (?:module|package)|ERR_MODULE_NOT_FOUND", re.I)


def path_for(root):
    return os.path.join(root, CEX)


def sha(text):
    return hashlib.sha256(str(text).encode("utf-8", "replace")).hexdigest()


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def norm(s):
    """Whitespace-collapsed, formatter-tolerant form.

    `aiken fmt` adds a trailing comma before a closing bracket when it wraps
    a value across lines, and the recorder may have captured the one-line
    form. Dropping it here keeps a reformat from reading as a weakening.
    """
    s = " ".join(str(s or "").split())
    return re.sub(r",\s*(?=[}\])])", "", s)


def strip_ak(text):
    """Comments and string literals removed — both are places to hide a paste."""
    return AK_STRING.sub(' @"" ', AK_COMMENT.sub(" ", text))


def strip_dfy(text):
    """Dafny comments (line and block) and string literals removed."""
    return DFY_STRING.sub(' "" ', DFY_COMMENT.sub(" ", text))


def _digest_strings(text, pattern, groups):
    """Every string literal replaced by a literal holding a digest of its contents.

    Blanking strings outright (as the Aiken and Dafny readers do) makes a
    string-valued counterexample unpinnable: the literal a pin needs is the
    very thing removed. A digest keeps string-to-string matching exact while
    nothing else — no integer, no boolean — can hide inside one.
    """
    def sub(m):
        for g in groups:
            if m.group(g) is not None:
                return '"' + sha(m.group(g))[:12] + '"'
        return '""'
    return pattern.sub(sub, text)


def strip_rs(text):
    """Rust comments removed; string literals digested (see _digest_strings)."""
    return _digest_strings(RS_COMMENT.sub(" ", text), RS_STRING, ("raw", "s"))


def strip_tla(text):
    """TLA+ comments (`\\*` and `(* *)`) removed; string literals digested."""
    return _digest_strings(TLA_COMMENT.sub(" ", text), TLA_STRING, ("s",))


def strip_ts(text):
    """TypeScript comments removed; string literals digested.

    Digested rather than blanked so `pc = "L1"` stays pinnable while nothing
    can hide inside a string, which is the rule kani and apalache already
    use. Single, double and template quotes all carry data in TypeScript.
    """
    return _digest_strings(TS_COMMENT.sub(" ", text), TS_STRING, ("s", "t", "u"))


def needle_for(nd, tool):
    """A recorded leaf in the form the stripped body would carry it."""
    if tool in ("kani", "apalache", "fastcheck") and len(nd) >= 2 and nd[0] == nd[-1] == '"':
        return '"' + sha(nd[1:-1])[:12] + '"'
    # fast-check prints strings in double quotes; a pin may legitimately write
    # the same string in single quotes or a template literal, and the digest
    # of the contents is what both reduce to.
    if tool == "fastcheck" and len(nd) >= 2 and nd[0] == nd[-1] == "'":
        return '"' + sha(nd[1:-1])[:12] + '"'
    return nd


def leaves(value, tool="aiken"):
    """The literals of a reified value, in order, as (kind, text).

    Constructor names are collected separately because opaque types render
    as something that is not writable source: `Dict([(#"ab", True)])` has to
    be built with `dict.from_list(...)`, so requiring the word `Dict` would
    make a legitimate pin impossible. The data inside it is still required.
    """
    vals, ctors = [], []
    if tool in ("dafny", "kani"):
        for m in (DFY_LEAF if tool == "dafny" else RS_LEAF).finditer(str(value or "")):
            string, char, number, keyword, ident = m.groups()
            if string or char or number or keyword:
                vals.append(string or char or number or keyword)
            else:
                ctors.append(ident)
        return vals, ctors
    if tool == "apalache":
        # No constructors in a TLA+ value: field names and variable names are
        # not literals, and everything else is.
        for m in TLA_LEAF.finditer(str(value or "")):
            vals.append(next(g for g in m.groups() if g))
        return vals, ctors
    if tool == "fastcheck":
        # fast-check prints a JavaScript literal array. Record-shaped
        # counterexamples carry their keys, which are identifiers rather than
        # data, so only the literals are required of a pin.
        for m in TS_LEAF.finditer(str(value or "")):
            vals.append(next(g for g in m.groups() if g))
        return vals, ctors
    for m in LEAF.finditer(str(value or "")):
        byte, string, integer, ident = m.groups()
        if byte or string or integer:
            vals.append(byte or string or integer)
        elif ident in ("True", "False"):
            vals.append(ident)
        else:
            ctors.append(ident)
    return vals, ctors


def required_leaves(value, tool="aiken"):
    """What a pinned test must physically contain, in order."""
    vals, ctors = leaves(value, tool)
    if vals:
        return vals
    if ctors:
        return ctors
    n = norm(value)
    return [n] if n else []


def contains_in_order(body, needles):
    """Every needle present, in order, on token boundaries."""
    cursor = 0
    for nd in needles:
        pat = re.compile(
            r"(?<![A-Za-z0-9_.#\"@])" + re.escape(nd) + r"(?![A-Za-z0-9_.])")
        m = pat.search(body, cursor)
        if not m:
            return False, nd
        cursor = m.end()
    return True, None


def _brace_body(src, start):
    """The text between the `{` at or after `start` and its matching `}`."""
    brace = src.find("{", start)
    if brace < 0:
        return None
    depth, body = 0, []
    for j in range(brace, len(src)):
        ch = src[j]
        if ch == "{":
            depth += 1
            if depth == 1:
                continue
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return "".join(body)
        body.append(ch)
    return None


def test_body(text, name):
    """(body, is_fail) for `test name(...)` in Aiken source, else (None, None).

    Body is comment- and string-stripped and whitespace-collapsed, so
    containment is judged against code the compiler would actually run.
    """
    src = strip_ak(text)
    for m in TEST_HEAD.finditer(src):
        if m.group(1) != name:
            continue
        depth, i = 0, m.end() - 1
        close = -1
        for j in range(i, len(src)):
            if src[j] == "(":
                depth += 1
            elif src[j] == ")":
                depth -= 1
                if depth == 0:
                    close = j
                    break
        if close < 0:
            continue
        rest = src[close + 1:]
        brace = rest.find("{")
        if brace < 0:
            continue
        is_fail = bool(re.match(r"\s*fail\b", rest[:brace]))
        body = _brace_body(src, close + 1)
        if body is None:
            return None, None
        return norm(body), is_fail
    return None, None


def dafny_test_body(text, name):
    """(body, disabled) for a Dafny method/lemma named `name`, else (None, None).

    `disabled` is true when the declaration carries `{:verify false}` or
    `{:axiom}` — a test whose obligations the verifier is told to skip is
    decoration — or when its body contains `assume`, under which anything
    verifies. Attributes sit between the keyword and the name in Dafny
    (`method {:test} Name()`), so they are read off the declaration head.
    """
    src = strip_dfy(text)
    head = re.compile(
        r"(?:^|\n)[ \t]*(?:(?:ghost|static|twostate)\s+)*"
        r"(?:method|lemma|function|predicate|greatest lemma|least lemma)"
        r"(?P<attrs>(?:\s*\{:[^}]*\})*)\s+" + re.escape(name) + r"\s*(?:<[^>]*>)?\s*\(")
    for m in head.finditer(src):
        attrs = m.group("attrs") or ""
        disabled = bool(re.search(r"\{:\s*(?:verify\s+false|axiom)\b", attrs))
        # Skip the parameter list, then any spec clauses, to the body brace.
        depth, close = 0, -1
        for j in range(m.end() - 1, len(src)):
            if src[j] == "(":
                depth += 1
            elif src[j] == ")":
                depth -= 1
                if depth == 0:
                    close = j
                    break
        if close < 0:
            continue
        body = _brace_body(src, close + 1)
        if body is None:
            return None, None
        if re.search(r"\bassume\b", body):
            disabled = True
        return norm(body), disabled
    return None, None


def _skip_parens(src, i):
    """Index of the `)` closing the `(` at or after `i`, or -1."""
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "(":
            depth += 1
        elif src[j] == ")":
            depth -= 1
            if depth == 0:
                return j
    return -1


def rust_test_body(text, name):
    """(body, inverted) for `fn name(...)` in Rust source, else (None, None).

    `inverted` is True under `#[should_panic]` (it passes because the code
    still panics) or `#[ignore]` (it never runs), and is a reason string
    when the fn carries neither `#[test]` nor `#[kani::proof]`: cargo runs
    no such function, so it guards nothing. Attributes are read off the
    contiguous `#[...]` block directly above the fn, comments already gone.
    """
    src = strip_rs(text)
    head = re.compile(
        r"(?:^|\n)[ \t]*(?:pub(?:\([^)]*\))?[ \t]+)?"
        r"(?:(?:async|unsafe|const|extern(?:[ \t]+\"[^\"]*\")?)[ \t]+)*"
        r"fn[ \t]+" + re.escape(name) + r"\s*(?:<[^>]*>)?\s*\(")
    for m in head.finditer(src):
        attrs = re.search(r"(?:#\[[^\]]*\]\s*)*$", src[:m.start()]).group(0)
        inverted = bool(RS_INVERTED_ATTR.search(attrs))
        if not inverted and not RS_TEST_ATTR.search(attrs):
            inverted = ("a plain `fn` with neither `#[test]` nor `#[kani::proof]` "
                        "— cargo runs no such function, so it guards nothing")
        close = _skip_parens(src, m.end() - 1)
        if close < 0:
            continue
        body = _brace_body(src, close + 1)
        if body is None:
            return None, None
        return norm(body), inverted
    return None, None


def tla_test_body(text, name):
    """(body, inverted) for `name == body` in a TLA+ module, else (None, None).

    The body runs to the next column-0 definition or declaration, a
    separator line, or the module end `====`. `inverted` when the operator
    is introduced by `ASSUME`: the checker takes it as given.
    """
    src = strip_tla(text)
    for m in TLA_DEF.finditer(src):
        if m.group("name") != name:
            continue
        end = TLA_DEF_END.search(src, m.end())
        body = src[m.end():end.start() if end else len(src)]
        return norm(body), m.group("kw") == "ASSUME"
    return None, None


def ts_test_body(text, name):
    """(body, inverted) for a TypeScript test carrying `name`, else (None, None).

    A test is found by its TITLE containing the name, which is how the pin
    rule reads in a language whose tests are named by a string rather than an
    identifier; a bare `function`/`const` of that name is accepted too.
    Comments are stripped first, but strings are NOT digested until the body
    is found, or the title this looks for would be a hash.
    """
    src = TS_COMMENT.sub(" ", text)
    for m in TS_TEST_HEAD.finditer(src):
        q = m.group("q")
        end = src.find(q, m.end())
        if end < 0:
            continue
        title = src[m.end():end]
        if name not in title:
            continue
        mod = (m.group("mod") or "").lower()
        inverted = False
        if m.group("x") or ".skip" in mod or ".todo" in mod:
            inverted = ("declared `x`/`.skip`/`.todo`, so the runner never "
                        "executes it and it guards nothing")
        elif ".fails" in mod or ".failing" in mod:
            inverted = ("declared `.fails`, which inverts the oracle: it passes "
                        "because the code is still broken and goes red the day "
                        "someone fixes it")
        # The callback's block, told apart from an options object by what
        # precedes it: `=> {` or `) {` opens a body, `, {` opens options.
        i = end + 1
        while i < len(src):
            if src[i] == "{":
                j = i - 1
                while j >= 0 and src[j] in " \t\n\r":
                    j -= 1
                if j >= 0 and (src[j] == ">" or src[j] == ")"):
                    break
            i += 1
        else:
            return None, None
        body = _brace_body(src, i)
        if body is None:
            return None, None
        return norm(_digest_strings(body, TS_STRING, ("s", "t", "u"))), inverted
    nm = re.compile(TS_NAMED.pattern.replace("{name}", re.escape(name)))
    m = nm.search(src)
    if m:
        i = src.find("{", m.end())
        if i >= 0:
            body = _brace_body(src, i)
            if body is not None:
                return norm(_digest_strings(body, TS_STRING, ("s", "t", "u"))), False
    return None, None


def hollow(body, tool="aiken"):
    """A body that runs but asserts nothing. Returns a reason or ''."""
    if not body:
        return "the body is empty"
    if tool == "dafny":
        if DFY_VACUOUS.match(body):
            return f"the body is `{body}` — it proves nothing"
        return ""
    if tool == "kani":
        if RS_VACUOUS.match(body):
            return f"the body is `{body}` — it cannot fail"
        if not re.search(r"\bassert|kani::", body) and not RS_CALL.search(body):
            return ("the body calls nothing and asserts nothing — the values "
                    "are bound and then ignored")
        return ""
    if tool == "apalache":
        if TLA_VACUOUS.match(body):
            return f"the body is `{body}` — it constrains nothing"
        return ""
    if tool == "fastcheck":
        if TS_VACUOUS.match(body):
            return f"the body is `{body}` — it cannot fail"
        if not re.search(r"\bexpect\b|\bassert\w*\b|\bfc\s*\.", body) \
                and not TS_CALL.search(body):
            return ("the body calls nothing and asserts nothing — the values "
                    "are bound and then ignored")
        return ""
    if VACUOUS.match(body):
        return f"the body is `{body}` — it cannot fail"
    toks = body.split()
    if len(toks) >= 2 and toks[-1] in ("True", "False") and toks[-2] not in TAIL_OPS:
        return (f"the body ends in a bare `{toks[-1]}` that no operator "
                f"consumes — the value is bound and then ignored")
    return ""


def tracked(root):
    try:
        out = subprocess.run(["git", "-C", root, "ls-files"],
                             capture_output=True, text=True, check=True).stdout
    except Exception:  # noqa: BLE001
        return set()
    return {f for f in out.splitlines() if f}


def head_sha(root):
    try:
        r = subprocess.run(["git", "-C", root, "rev-parse", "HEAD"],
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:  # noqa: BLE001
        return ""


def tool_version(tool, flag="--version"):
    try:
        r = subprocess.run([tool, flag], capture_output=True, text=True,
                           timeout=10)
        return r.stdout.strip()[:80] if r.returncode == 0 else ""
    except Exception:  # noqa: BLE001
        return ""


def read(root):
    """Every recorded row, oldest first. A malformed line is fatal.

    Rows are kept small — the bulky prover payload lives in the evidence
    sidecar — so a partial append is unlikely; but a store that silently
    shrank would let a pinned counterexample disappear, which is the one
    direction this file must never fail in.
    """
    p = path_for(root)
    if not os.path.exists(p):
        return []
    out = []
    with open(p, encoding="utf-8") as fh:
        for i, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError as e:
                raise SystemExit(f"cex: {p}:{i} is not valid JSON: {e}")
    return out


def current(root):
    """Latest state per dedupeKey, in first-seen order."""
    state = {}
    for r in read(root):
        state[r.get("dedupeKey")] = r
    return state


def append(root, row):
    p = path_for(root)
    with open(p, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")
    return row


def write_evidence(root, row, raw):
    d = os.path.join(root, EVIDENCE)
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, f"{row['cexId']}.json")
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        json.dump({"row": row, "raw": raw}, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return p


def file_inbox(root, kind, node, detail):
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import inbox as _inbox
        _inbox.add(root, kind, node, "", detail)
    except Exception as e:  # noqa: BLE001 - a recorder never decides green
        print(f"cex: could not file an inbox row: {e}", file=sys.stderr)


def ingest_failed(root, output, why, tool="aiken"):
    """Loud, and never red. The prover's exit code is the gate, not ours."""
    d = os.path.join(root, SCRATCH)
    os.makedirs(d, exist_ok=True)
    stamp = now().replace(":", "").replace("-", "")
    p = os.path.join(d, f"ingest-failed-{stamp}.txt")
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(output[:20000])
    # Distinct failure shapes get distinct inbox ids, or the second parser
    # break is swallowed as a duplicate of the first and never surfaces.
    # The tool is in the id too: two parsers drifting in the same week must
    # not collapse into one row.
    node = f"{tool}:{sha(output[:2000])[:8]}"
    file_inbox(root, "cex-ingest-failed", node,
               f"[{tool}] {why}. Unparsed output head saved to {p}")
    print(f"cex: INGEST-FAILED ({tool}) — {why}", file=sys.stderr)
    print(f"cex: saved {p}; filed an inbox item. Not failing the build: the "
          f"prover's exit code is the gate, not the recorder's.", file=sys.stderr)
    return 0


# ------------------------------------------------------------------- parsers
# Each parser takes (root, output, exit_code, source) — `source` is the
# --from path when there was one, or None — and returns one of:
#   ("nothing", None)          the run recorded no failures (green, or nothing ran)
#   ("failed", why)            parser drift — file INGEST-FAILED, never red
#   ("rows", rows, raw_for)    rows to record; raw_for(row) is the evidence blob

def parse_aiken(root, output, exit_code, source=None):
    text = output.strip()
    if not text:
        if exit_code not in (0, 1):
            # An ill-formed fuzzer panics with nothing on stdout; reading that
            # as "no failures" would be silently wrong.
            return ("failed", f"the prover exited {exit_code} and wrote nothing "
                              f"to stdout")
        return ("nothing", None)  # nothing ran, or a type error that never reached the tests
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        if exit_code == 0:
            return ("nothing", None)
        return ("failed", "stdout is not JSON (a pre-1.1.6 aiken writes "
                          "terminal boxes; JSON needs a non-TTY stdout)")
    mods = doc.get("modules")
    if not isinstance(mods, list) or not all(
            isinstance(m, dict) and isinstance(m.get("tests"), list) for m in mods):
        return ("failed", "no recognizable modules[].tests[] (note: the "
                          "published --show-json-schema names it 'test', "
                          "real output says 'tests')")

    seed = doc.get("seed")
    rows, extracted = [], 0
    for mod in mods:
        module = str(mod.get("name") or "")
        for t in mod.get("tests", []):
            if not isinstance(t, dict) or t.get("status") != "fail":
                # A `fail`-annotated property that correctly failed is a PASS
                # carrying a counterexample. Recording it would mint a
                # counterexample out of a green test.
                continue
            extracted += 1
            title = str(t.get("title") or "")
            cx = t.get("counterexample")
            is_prop = "iterations" in t or "counterexample" in t
            if isinstance(cx, str):
                payload, signature = cx, "counterexample"
            elif isinstance(cx, dict):
                payload, signature = "", "fuzzer-error"
            elif is_prop:
                payload, signature = "", "no-counterexample-found"
            else:
                payload, signature = "", "assertion"
            assertion = t.get("assertion") if isinstance(t.get("assertion"), str) else ""
            kind = "property" if is_prop else ("unit" if not is_prop else "unknown")
            # Containment needs something to check. A unit test has no
            # generated input, so its assertion block is what a regression
            # must reproduce.
            contain = payload or (assertion if kind == "unit" else "")
            key = f"aiken|{module}|{title}|{norm(payload) or norm(assertion)}"
            rows.append({
                "cexId": "cex_" + sha(key)[:12],
                "dedupeKey": key,
                "tool": "aiken",
                "toolVersion": tool_version("aiken"),
                "module": module,
                "title": title,
                "selector": f"{module}.{{{title}}}",
                "kind": kind,
                "input": payload or None,
                "inputForm": "aiken-source" if payload else None,
                "assertion": assertion,
                "containment": norm(contain),
                "signature": signature,
                "iterations": t.get("iterations"),
                "seed": seed,
                "status": "open",
                "firstSeen": now(),
                "headSha": head_sha(root),
            })

    # Extraction is an invariant, not a best effort: if the run reported
    # failures and none were extracted, the format moved under us and
    # silence would read as a clean sweep forever after.
    reported = ((doc.get("summary") or {}).get("failed")
                if isinstance(doc.get("summary"), dict) else None)
    if isinstance(reported, int) and reported > 0 and extracted == 0:
        return ("failed", f"summary.failed is {reported} but no failing test "
                          f"could be extracted — the output format changed")

    def raw_for(row):
        return next((t for m in mods for t in m.get("tests", [])
                     if isinstance(t, dict) and t.get("title") == row["title"]), {})
    return ("rows", rows, raw_for)


def dafny_model(section):
    """(assignments, label) from a Dafny counterexample section.

    The `initial state` block is preferred: it holds the method's inputs,
    which is the shrunk-input equivalent. Both spellings are read — the
    `name : type = value` lines Dafny 3 prints and the `assume a == 1 &&
    b == 2;` statements Dafny 4 prints — so the parser survives either.
    """
    blocks, cur = [], None
    for line in section.splitlines():
        st = DFY_STATE.match(line)
        if st:
            cur = {"label": (st.group("label") or "").strip(), "assign": []}
            blocks.append(cur)
            continue
        if cur is None:
            cur = {"label": "", "assign": []}
            blocks.append(cur)
        a = DFY_ASSIGN.match(line)
        if a:
            cur["assign"].append((a.group("name"), a.group("value")))
            continue
        s = DFY_ASSUME.match(line)
        if s:
            for m in DFY_CONJUNCT.finditer(s.group("expr")):
                if m.group("name"):
                    cur["assign"].append((m.group("name"), m.group("value").strip()))
                elif m.group("lname"):
                    cur["assign"].append((m.group("lname"), m.group("lvalue").strip()))
    initial = next((b for b in blocks if "initial" in b["label"].lower() and b["assign"]), None)
    first = next((b for b in blocks if b["assign"]), None)
    chosen = initial or first
    if not chosen:
        return [], ""
    return chosen["assign"], chosen["label"]


def dafny_enclosing(root, rel, line):
    """Name of the declaration containing `line` in a Dafny file, or ''."""
    p = os.path.join(root, rel)
    try:
        with open(p, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return ""
    name = ""
    for i, raw in enumerate(lines[:line], 1):
        m = DFY_DECL.match(DFY_COMMENT.sub("", raw))
        if m:
            name = m.group(1)
    return name


def parse_dafny(root, output, exit_code, source=None):
    text = output.strip()
    if exit_code == 0:
        return ("nothing", None)
    if not text:
        return ("failed", f"dafny exited {exit_code} and wrote nothing to stdout")
    errors = [m for m in DFY_ERROR.finditer(text)]
    summary = DFY_SUMMARY.search(text)
    reported = int(summary.group("n")) if summary else None
    head = DFY_CEX_HEAD.search(text)
    assigns, label = ([], "")
    if head:
        assigns, label = dafny_model(text[head.end():])
        if not assigns:
            return ("failed", "a Counterexample heading with no `name : type = "
                              "value` or `assume name == value;` line under it — "
                              "the model format changed")
    if not errors and reported is None:
        return ("failed", "no `file.dfy(line,col): Error:` line and no verifier "
                          "summary — this is not dafny verify output as this "
                          "parser knows it")
    verification = [e for e in errors if DFY_VERIFICATION.search(e.group("msg"))]
    if not verification:
        if errors:
            # Resolution or type errors: the run never verified anything, so
            # there is no failing input to record. Said, not swallowed.
            print(f"cex: {len(errors)} dafny error(s) but none is a verification "
                  f"failure (resolution or type errors) — nothing to record")
            return ("nothing", None)
        if reported and reported > 0:
            return ("failed", f"the verifier summary reports {reported} error(s) "
                              f"but no Error: line could be parsed — the output "
                              f"format changed")
        return ("nothing", None)

    rows = []
    for i, e in enumerate(verification):
        rel = e.group("file").replace(os.sep, "/")
        line = int(e.group("line"))
        method = dafny_enclosing(root, rel, line) or f"line-{line}"
        msg = e.group("msg")
        # Dafny models the FIRST failing assertion only. The model belongs to
        # that error; later failures are recorded as assertions with no input.
        model = assigns if (i == 0 and assigns) else []
        payload = ", ".join(f"{n} == {v}" for n, v in model)
        signature = "counterexample" if model else "assertion"
        key = f"dafny|{rel}|{method}|{norm(payload) or norm(msg)}"
        rows.append({
            "cexId": "cex_" + sha(key)[:12],
            "dedupeKey": key,
            "tool": "dafny",
            "toolVersion": tool_version("dafny"),
            "module": rel,
            "title": method,
            "selector": f"{rel}:{method}",
            "kind": "method",
            "input": payload or None,
            "inputForm": "dafny-model" if payload else None,
            "assertion": f"{rel}({line},{e.group('col')}): {msg}",
            "containment": norm(payload),
            "signature": signature,
            "iterations": None,
            "seed": None,
            "status": "open",
            "firstSeen": now(),
            "headSha": head_sha(root),
            "modelState": label or None,
        })

    def raw_for(row):
        return {"error": row["assertion"],
                "model": [{"name": n, "value": v} for n, v in
                          (assigns if row["signature"] == "counterexample" else [])],
                "output": text[:20000]}
    return ("rows", rows, raw_for)


def kani_segments(text):
    """[(harness, text)] per `Checking harness X...` section.

    One unnamed segment when the run printed no such line, so a single
    `--harness` run and an older output shape still read.
    """
    starts = list(KANI_HARNESS_START.finditer(text))
    if not starts:
        return [(None, text)]
    out = []
    for i, m in enumerate(starts):
        end = starts[i + 1].start() if i + 1 < len(starts) else len(text)
        out.append((m.group("name"), text[m.end():end]))
    return out


def parse_kani(root, output, exit_code, source=None):
    text = output.strip()
    if exit_code == 0:
        return ("nothing", None)
    if not text:
        return ("failed", f"cargo kani exited {exit_code} and wrote nothing to stdout")
    verdicts = [m.group("v") for m in KANI_VERDICT.finditer(text)]
    complete = KANI_COMPLETE.search(text)
    reported = int(complete.group("n")) if complete else None
    if not verdicts:
        if RS_BUILD_ERROR.search(text):
            # A build error: no harness ran, so there is no failing input to
            # record. Said, not swallowed — the same call parse_dafny makes
            # for resolution errors.
            print(f"cex: cargo kani exited {exit_code} on a build error before "
                  f"any harness ran — nothing was verified, nothing to record")
            return ("nothing", None)
        return ("failed", "no `VERIFICATION:- FAILED|SUCCESSFUL` verdict and no "
                          "rustc error line — this is not cargo kani output as "
                          "this parser knows it")
    if "FAILED" not in verdicts:
        if reported:
            return ("failed", f"the summary counts {reported} failure(s) but every "
                              f"harness printed VERIFICATION:- SUCCESSFUL — the "
                              f"output format changed")
        print(f"cex: cargo kani exited {exit_code} but every harness verified — "
              f"nothing to record")
        return ("nothing", None)

    failed_for = [m.group("name") for m in KANI_FAILED_FOR.finditer(text)]
    rows, evidence = [], {}
    for harness, seg in kani_segments(text):
        segv = [m.group("v") for m in KANI_VERDICT.finditer(seg)]
        if not segv or segv[-1] != "FAILED":
            continue
        checks = [c for c in KANI_CHECK.finditer(seg) if c.group("status") == "FAILURE"]
        summary = KANI_FAILED_CHECK.search(seg)
        play = KANI_PLAYBACK.search(seg)
        # The harness is named from the strongest source present: the
        # section header, the playback block, the summary's `in <fn>`, a
        # failed check's location, or the run summary when it names one.
        name = harness or (play.group("name") if play else None) \
            or (summary.group("fn") if summary else None)
        locs = [KANI_LOCATION.search(c.group("loc") or "") for c in checks]
        if not name:
            name = next((lo.group("fn") for lo in locs if lo and lo.group("fn")), None)
        if not name and len(failed_for) == 1:
            name = failed_for[0]
        if not name:
            return ("failed", "VERIFICATION:- FAILED but no harness name could be "
                              "read from the section header, the playback block, "
                              "the Failed Checks summary or a check location — "
                              "the output format changed")
        module = next((lo.group("file") for lo in locs if lo), None) \
            or (summary.group("file") if summary else "")
        module = (module or "").replace(os.sep, "/")
        parts = [f"\"{c.group('desc')}\" at {c.group('loc') or 'unknown location'}"
                 for c in checks]
        if not parts and summary:
            parts = [f"\"{summary.group('desc')}\" at {summary.group('file')}:"
                     f"{summary.group('line')} in function {summary.group('fn')}"]
        assertion = "; ".join(parts) or "verification failed"
        vals = KANI_PLAYBACK_VAL.findall(play.group("body")) if play else []
        payload = ", ".join(v for v, _ in vals)
        alt = ", ".join(f"vec![{norm(b)}]" for _, b in vals)
        key = f"kani|{module}|{name}|{norm(payload) or norm(assertion)}"
        cexid = "cex_" + sha(key)[:12]
        rows.append({
            "cexId": cexid,
            "dedupeKey": key,
            "tool": "kani",
            "toolVersion": tool_version("cargo-kani"),
            "module": module,
            "title": name,
            "selector": f"{module}:{name}" if module else name,
            "kind": "harness",
            "input": payload or None,
            "inputForm": "kani-concrete-playback" if payload else None,
            "assertion": assertion,
            "containment": norm(payload),
            # The raw bytes Kani writes under --concrete-playback=inplace are
            # the same value in the prover's own second spelling; a pin may
            # carry either.
            "containmentAlt": norm(alt) or None,
            "signature": "counterexample" if payload else "assertion",
            "iterations": None,
            "seed": None,
            "status": "open",
            "firstSeen": now(),
            "headSha": head_sha(root),
        })
        evidence[cexid] = {
            "harness": name,
            "checks": [{"name": c.group("name"), "status": c.group("status"),
                        "description": c.group("desc"), "location": c.group("loc")}
                       for c in checks],
            "playback": [{"value": v, "bytes": norm(b)} for v, b in vals],
            "output": text[:20000],
        }
    if not rows:
        return ("failed", "VERIFICATION:- FAILED was printed but no harness "
                          "section could be read — the output format changed")
    # Extraction is an invariant, as with aiken's summary.failed: a summary
    # that counts more failures than were read means the format moved.
    if reported is not None and reported > len(rows):
        return ("failed", f"the summary counts {reported} failure(s) but only "
                          f"{len(rows)} harness(es) could be extracted — the "
                          f"output format changed")

    def raw_for(row):
        return evidence.get(row["cexId"], {})
    return ("rows", rows, raw_for)


def itf_value(v):
    """A compact TLA+-like rendering of one ITF value."""
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, list):
        return "<<" + ", ".join(itf_value(x) for x in v) + ">>"
    if isinstance(v, dict):
        if "#bigint" in v:
            return str(v["#bigint"])
        if "#set" in v:
            return "{" + ", ".join(itf_value(x) for x in v["#set"]) + "}"
        if "#tup" in v:
            return "<<" + ", ".join(itf_value(x) for x in v["#tup"]) + ">>"
        if "#map" in v:
            return "SetAsFun({" + ", ".join(
                f"<<{itf_value(k)}, {itf_value(x)}>>" for k, x in v["#map"]) + "})"
        if "#unserializable" in v:
            return str(v["#unserializable"])
        return "[" + ", ".join(f"{k} |-> {itf_value(x)}" for k, x in v.items()
                               if not str(k).startswith("#")) + "]"
    return json.dumps(v)


def itf_state(state, names):
    """`x = 1, y = {1, 2}` for one ITF state, variables in `vars` order."""
    order = [n for n in names if n in state]
    order += [k for k in state if not str(k).startswith("#") and k not in order]
    return ", ".join(f"{n} = {itf_value(state[n])}" for n in order)


def parse_apalache(root, output, exit_code, source=None):
    text = output.strip()
    if exit_code == 0:
        return ("nothing", None)
    if not text:
        return ("failed", f"apalache-mc exited {exit_code} and wrote nothing to stdout")
    traces = []
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        doc = None
    if isinstance(doc, dict) and isinstance(doc.get("states"), list):
        # --from pointed at the ITF file itself.
        label = source.replace(os.sep, "/") if source else "(stdin)"
        if source and os.path.isabs(source):
            r = os.path.relpath(source, os.path.abspath(root))
            label = r.replace(os.sep, "/") if not r.startswith("..") else label
        traces.append((label, doc))
    else:
        paths = []
        for m in ITF_PATH.finditer(text):
            if m.group("path") not in paths:
                paths.append(m.group("path"))
        if not paths:
            code = AP_EXITCODE.search(text)
            n = int(code.group("n")) if code else None
            if n == 12 or AP_VIOLATION.search(text):
                return ("failed", "the run reports a violated invariant but names "
                                  "no .itf.json trace — the output format changed")
            if n is not None or re.search(r"\berror\b", text, re.I):
                # A parse, type or configuration error: the checker never
                # reached a counterexample. Said, not swallowed.
                print(f"cex: apalache-mc exited {exit_code} without reaching a "
                      f"counterexample (a parse, type or configuration error) — "
                      f"nothing to record")
                return ("nothing", None)
            return ("failed", "no EXITCODE line and no .itf.json path — this is "
                              "not apalache-mc output as this parser knows it")
        for p in paths:
            full = p if os.path.isabs(p) else os.path.join(root, p)
            if not os.path.exists(full):
                return ("failed", f"the output names {p} but it does not exist")
            try:
                with open(full, encoding="utf-8") as fh:
                    d = json.load(fh)
            except (OSError, json.JSONDecodeError) as e:
                return ("failed", f"{p} is not readable ITF JSON: {e}")
            if not isinstance(d, dict) or not isinstance(d.get("states"), list):
                return ("failed", f"{p} has no `states` list — not ITF as this "
                                  f"parser knows it")
            rel = os.path.relpath(os.path.abspath(full), os.path.abspath(root))
            traces.append((rel.replace(os.sep, "/") if not rel.startswith("..") else p, d))

    rows, evidence = [], {}
    for label, d in traces:
        meta = d.get("#meta") if isinstance(d.get("#meta"), dict) else {}
        names = [str(v) for v in d.get("vars")] if isinstance(d.get("vars"), list) else []
        states = [s for s in d["states"] if isinstance(s, dict)]
        if not states:
            return ("failed", f"{label}: the trace has no states")
        try:
            rendered = [itf_state(s, names) for s in states]
        except (TypeError, ValueError, AttributeError) as e:
            return ("failed", f"{label}: a value could not be rendered ({e}) — the "
                              f"ITF encoding changed")
        module = meta.get("source") if isinstance(meta.get("source"), str) else ""
        if module and os.path.isabs(module):
            r = os.path.relpath(module, os.path.abspath(root))
            module = r if not r.startswith("..") else module
        module = (module or label).replace(os.sep, "/")
        # Apalache 0.47 writes each run under _apalache-out/<Spec.tla>/<stamp>/
        # and its ITF carries no `source`, so the spec is read from that
        # directory. A key carrying the timestamp would file the same trace
        # as new on every run.
        if "_apalache-out/" in module:
            after = module.split("_apalache-out/", 1)[1].split("/")
            if after and after[0]:
                module = after[0]
        base = os.path.splitext(os.path.basename(module))[0]
        n = len(states)
        payload = " ; ".join(rendered)
        title = f"{base} ({n} states)"
        key = f"apalache|{module}|{title}|{norm(payload)}"
        cexid = "cex_" + sha(key)[:12]
        rows.append({
            "cexId": cexid,
            "dedupeKey": key,
            "tool": "apalache",
            "toolVersion": tool_version("apalache-mc", "version"),
            "module": module,
            "title": title,
            "selector": f"{module}:{n}-state-trace",
            "kind": "trace",
            "input": payload,
            "inputForm": "itf",
            "assertion": f"invariant violated after {n - 1} transition(s)",
            # The initial state is what a regression re-checks from; see the
            # module docstring.
            "containment": norm(rendered[0]),
            "signature": "counterexample",
            "iterations": None,
            "seed": None,
            "status": "open",
            "firstSeen": now(),
            "headSha": head_sha(root),
            "trace": label,
            "traceLength": n,
        })
        evidence[cexid] = {"trace": label, "itf": d, "output": text[:20000]}

    def raw_for(row):
        return evidence.get(row["cexId"], {})
    return ("rows", rows, raw_for)


# The registry. `parse` reads a run; `body` reads a regression test in that
# prover's language into (body, oracle_inverted); `roots` bounds where a pin
# may live (None: anywhere tracked, judged by `ext`); `rerun` is the command a
# human types to reproduce; `draft` is the skeleton --pin writes to scratch.
def parse_fastcheck(root, output, exit_code, source=None):
    """fast-check failures out of a vitest run.

    A property failure carries the shrunk counterexample plus the `seed` and
    `path` that replay it, which is the same triple the Aiken parser reads.
    A plain assertion failure in the same run carries none of that and is
    deliberately NOT recorded: it is a failing test, not a counterexample,
    and minting one would put a value in the ledger that no generator found.
    """
    text = output.strip()
    if exit_code == 0:
        return ("nothing", None)
    if not text:
        return ("failed", f"the test run exited {exit_code} and wrote nothing")
    head = FC_HEADER.search(text)
    region = text[head.end():] if head else text
    blocks = list(FC_BLOCK.finditer(region))
    if not blocks:
        if FC_NO_RUN.search(text):
            print("cex: the test run failed before any test reported "
                  "(load, transform or resolution) — nothing to record")
            return ("nothing", None)
        if head and head.group("n") == "0":
            return ("nothing", None)
        return ("failed", "no ` FAIL <file> > <test>` block and no load error — "
                          "this is not vitest output as this parser knows it")
    if head and len(blocks) != int(head.group("n")):
        return ("failed", f"the summary counts {head.group('n')} failed test(s) but "
                          f"{len(blocks)} block(s) could be read — the reporter "
                          f"format changed")

    rows, properties = [], 0
    for n, m in enumerate(blocks):
        seg = region[m.end():blocks[n + 1].start() if n + 1 < len(blocks) else len(region)]
        prop = FC_PROPERTY.search(seg)
        if not prop:
            # A plain failing test. Said once, never recorded.
            continue
        properties += 1
        sel = norm(m.group("sel"))
        parts = [p.strip() for p in sel.split(">")]
        rel = parts[0].replace(os.sep, "/")
        title = parts[-1] if len(parts) > 1 else sel
        cex = FC_CEX.search(seg)
        replay = FC_REPLAY.search(seg)
        cause = FC_CAUSE.search(seg)
        # NOT normalised. fast-check generates arbitrary strings, and a
        # counterexample like `[-10,"         "]` is nine spaces: collapsing
        # whitespace inside a string literal would record a value the
        # generator never produced, and pin a test against the wrong one.
        # The line is already one line, so there is nothing to fold.
        payload = cex.group("value").strip() if cex else ""
        shrunk = FC_SHRUNK.search(seg)
        key = f"fastcheck|{rel}|{sel}|{payload}"
        rows.append({
            "cexId": "cex_" + sha(key)[:12],
            "dedupeKey": key,
            "tool": "fastcheck",
            "toolVersion": tool_version("npx", "--version"),
            "module": rel,
            "title": title,
            "selector": sel,
            "kind": "property",
            "input": payload or None,
            "inputForm": "js-literal" if payload else None,
            "assertion": (cause.group("msg") if cause else
                          f"property failed after {prop.group('n')} test(s)"),
            "containment": payload,
            "signature": "counterexample" if payload else "no-counterexample-found",
            "iterations": int(prop.group("n")),
            "seed": int(replay.group("seed")) if replay else None,
            "replayPath": replay.group("path") if replay else None,
            "shrinks": int(shrunk.group("n")) if shrunk else None,
            "status": "open",
            "firstSeen": now(),
            "headSha": head_sha(root),
        })
    if not rows:
        print(f"cex: {len(blocks)} failing test(s), none of them a property "
              f"failure — nothing to record")
        return ("nothing", None)

    def raw_for(row):
        for n, m in enumerate(blocks):
            if norm(m.group("sel")) == row["selector"]:
                seg = region[m.start():blocks[n + 1].start()
                             if n + 1 < len(blocks) else len(region)]
                return {"block": seg[:20000]}
        return {}
    return ("rows", rows, raw_for)


def parse_blaster(root, output, exit_code, source=None):
    """Structured adapter evidence; expected refutations are not open bugs."""
    import blaster
    try:
        receipt = blaster.loads(output)
        rows = blaster.counterexamples(root, receipt)
        return ("rows", rows, lambda row: {"receipt": receipt, "cexId": row["cexId"]})
    except (ValueError, KeyError, TypeError) as exc:
        return ("failed", "invalid Blaster verification receipt: " + str(exc))


TOOLS = {
    "blaster": {
        "parse": parse_blaster,
        "body": lambda text, name: (None, None),
        "roots": None,
        "ext": ".json",
        "inverted": "not a successfully replayed concrete Blaster regression",
        "rerun": lambda row: "blaster.py --run (registered property plus exact scalar regressions)",
    },
    "aiken": {
        "parse": parse_aiken,
        "body": test_body,
        "roots": COMPILE_ROOTS,
        "ext": ".ak",
        "inverted": ("annotated `fail`, which inverts the oracle: it passes "
                     "because the code is still broken and goes red the day "
                     "someone fixes it"),
        "rerun": lambda row: (f"aiken check -e -m '{row.get('selector')}'"
                              f" --seed {row.get('seed')}"),
        "head": lambda cexid: f"test {cexid}() {{",
        "hint": "// replace with the real predicate this value broke",
        "call": lambda row: f"<predicate>({row.get('input') or row.get('assertion') or ''})",
    },
    "dafny": {
        "parse": parse_dafny,
        "body": dafny_test_body,
        "roots": None,
        "ext": ".dfy",
        "inverted": ("verification-disabled: `{:verify false}`, `{:axiom}`, or "
                     "an `assume` in its body, under which anything verifies"),
        "rerun": lambda row: (f"dafny verify --extract-counterexample "
                              f"{row.get('module')}"),
        "head": lambda cexid: f"method {{:test}} {cexid}() {{",
        "hint": "// call the method with the recorded values and expect the fixed result",
        "call": lambda row: f"var r := <method>({', '.join(v for _, v in _pairs(row))});",
    },
    "kani": {
        "parse": parse_kani,
        "body": rust_test_body,
        "roots": None,
        "ext": ".rs",
        "inverted": ("annotated `#[should_panic]` or `#[ignore]`: the first passes "
                     "because the code still panics and goes red the day someone "
                     "fixes it, the second never runs"),
        "rerun": lambda row: (f"cargo kani --harness {row.get('title')} -Z "
                              f"concrete-playback --concrete-playback=print   "
                              f"(=inplace writes the test into the source instead; "
                              f"renamed to carry this cexId and tracked, it is a "
                              f"legal pin target)"),
        "head": lambda cexid: f"#[test]\nfn {cexid}() {{",
        "hint": ("// call the code under proof with the recorded values (Kani does "
                 "not name them; they are in harness order) and assert the fixed result"),
        "call": lambda row: f"assert!(<property>({row.get('input') or ''}));",
    },
    "apalache": {
        "parse": parse_apalache,
        "body": tla_test_body,
        "roots": None,
        "ext": ".tla",
        "inverted": ("introduced by `ASSUME`, which the checker takes as given "
                     "rather than checks"),
        "rerun": lambda row: f"apalache-mc check --inv=<Inv> {row.get('module')}",
        "head": lambda cexid: f"{cexid} ==",
        "hint": ("\\* the recorded trace's first state, as an Init to re-check the "
                 "invariant from: apalache-mc check --init=<this> --inv=<Inv> <module>"),
        "call": lambda row: _tla_conj(row.get("containment")),
        "comment": "\\*",
        "tail": "",
    },
    "fastcheck": {
        "parse": parse_fastcheck,
        "body": ts_test_body,
        "roots": None,
        "ext": (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs"),
        "inverted": ("declared `.skip`, `.todo` or `.fails` — the first two never "
                     "run and the last inverts the oracle"),
        "rerun": lambda row: (
            f"npx vitest run {row.get('module')} -t {row.get('title')!r}   "
            f"(replay the exact case with fc.assert(prop, {{ seed: {row.get('seed')}, "
            f"path: {row.get('replayPath')!r}, endOnFailure: true }}))"),
        "head": lambda cexid: f'it("{cexid} regression", () => {{',
        "hint": "// call the code under test with the recorded values and assert the fix",
        "call": lambda row: f"expect(<fn>{row.get('input') or '()'}).toBe(<expected>);",
        "comment": "//",
        "tail": "});",
    },
}


def _tla_conj(state):
    """`x = 1, y = {1, 2}` as `x = 1 /\\ y = {1, 2}`: split on top-level commas."""
    parts, cur, depth, prev = [], [], 0, ""
    for ch in str(state or ""):
        if ch in "{[<(":
            depth += 1
        elif ch in "}])" or (ch == ">" and prev != "-"):
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
        prev = ch
    if "".join(cur).strip():
        parts.append("".join(cur).strip())
    return " /\\ ".join(parts)


def _pairs(row):
    """(name, value) pairs out of a recorded `n == 1, m == 2` payload."""
    out = []
    for part in str(row.get("input") or "").split(", "):
        if " == " in part:
            n, v = part.split(" == ", 1)
            out.append((n.strip(), v.strip()))
    return out


def ingest(root, output, exit_code, tool, source=None):
    spec = TOOLS.get(tool)
    if spec is None:
        print(f"cex: unknown tool '{tool}' (known: {sorted(TOOLS)})", file=sys.stderr)
        return 1
    parsed = spec["parse"](root, output, exit_code, source)
    if parsed[0] == "failed":
        return ingest_failed(root, output, parsed[1], tool)
    if parsed[0] == "nothing":
        return 0
    rows, raw_for = parsed[1], parsed[2]
    if not rows:
        print("cex: no failing tests in this run")
        return 0

    os.makedirs(os.path.dirname(path_for(root)) or ".", exist_ok=True)
    state = current(root)
    written = 0
    for row in rows:
        prior = state.get(row["dedupeKey"])
        if prior:
            status = prior.get("status")
            if status == "pinned":
                # The prover just produced the exact value a pinned test
                # claims to guard. That is the only mechanical evidence this
                # slice can offer that a pin does not bite, so it is never
                # swallowed as a duplicate.
                file_inbox(root, "cex-pin-inert", prior["cexId"],
                           f"{prior['selector']} was re-found with the recorded "
                           f"counterexample while pinned to "
                           f"{(prior.get('pin') or {}).get('file')} — the "
                           f"regression test does not prevent it")
                print(f"cex: PIN INERT — {prior['cexId']} re-found while pinned",
                      file=sys.stderr)
            else:
                print(f"cex: already {status} {prior['cexId']} ({row['selector']})")
            continue
        append(root, row)
        write_evidence(root, row, raw_for(row))
        written += 1
        print(f"cex: recorded {row['cexId']} {row['selector']} "
              f"[{row['signature']}] {(row['input'] or row['assertion'] or '')[:70]}")
    if written:
        print(f"cex: {written} new counterexample(s) in {CEX} — commit it; "
              f"pin one with: cex.py --pin <cexId>")
    return 0


def resolve(root, cexid):
    for row in current(root).values():
        if row.get("cexId") == cexid:
            return row
    return None


def pin(root, cexid, path, testname):
    row = resolve(root, cexid)
    if not row:
        print(f"cex: no such counterexample '{cexid}'", file=sys.stderr)
        return 1
    if row.get("status") == "retired":
        print(f"cex: {cexid} is retired; re-ingest it before pinning",
              file=sys.stderr)
        return 1
    tool = row.get("tool") or "aiken"
    spec = TOOLS.get(tool)
    if spec is None:
        print(f"cex: {cexid} was recorded by '{tool}', which this cex.py does "
              f"not know (known: {sorted(TOOLS)})", file=sys.stderr)
        return 1

    if not path:
        # Step 1: freeze the evidence and offer a draft. The draft goes to
        # scratch, which is not a legal pin target — a tool must not write
        # the artifact that satisfies its own containment check.
        d = os.path.join(root, SCRATCH)
        os.makedirs(d, exist_ok=True)
        # A tool may read several extensions; the first is the one a draft
        # is written as.
        exts = spec["ext"] if isinstance(spec["ext"], tuple) else (spec["ext"],)
        draft = os.path.join(d, f"{cexid}{exts[0]}.draft")
        if tool == "blaster":
            import blaster
            try:
                entry = blaster.regression(row)
            except ValueError as exc:
                print(f"cex: {exc}", file=sys.stderr)
                return 1
            with open(draft, "w", encoding="utf-8", newline="\n") as fh:
                json.dump(entry, fh, indent=2)
                fh.write("\n")
            print(f"cex: add {draft} to .fluxpoint-blaster.json regressions; run blaster.py --run, then pin")
            return 0
        need = required_leaves(row.get("containment"), tool)
        # The draft speaks the prover's language down to its comment marker
        # and its closing line (none, for a TLA+ operator).
        c = spec.get("comment", "//")
        tail = spec.get("tail", "}")
        with open(draft, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(
                f"{c} DRAFT from fluxpoint cex.py --pin. Not a pin yet, and not\n"
                f"{c} compiled from here — write the real test into the source\n"
                f"{c} tree, then record it with:\n"
                f"{c}   cex.py --pin {cexid} --file <path> --test-name {cexid}\n"
                f"{c}\n"
                f"{c} cexId:  {cexid}\n"
                f"{c} source: {row.get('selector')} ({tool} "
                f"{row.get('toolVersion')}"
                + (f", seed {row.get('seed')}, after {row.get('iterations')} test(s)"
                   if row.get("seed") is not None else "")
                + ")\n"
                f"{c} re-run: {spec['rerun'](row)}\n"
                f"{c}\n"
                f"{c} The value below is copied verbatim from {CEX}. It is\n"
                f"{c} evidence, not a restatement — retyping it from memory is\n"
                f"{c} the one thing the pin check refuses.\n"
                f"{c} Literals your test must contain, in order: "
                f"{', '.join(need) if need else '(none)'}\n\n"
                f"{spec['head'](cexid)}\n"
                f"  {spec['hint']}\n"
                f"  {spec['call'](row)}\n"
                + (f"{tail}\n" if tail else ""))
        print(f"cex: evidence at {os.path.join(EVIDENCE, cexid + '.json')}")
        print(f"cex: draft at {draft}")
        print(f"cex: still OPEN. Write the compiling test, then re-run with "
              f"--file and --test-name.")
        return 0

    # Step 2: record a real test, but only if the tree actually carries it.
    rel = os.path.relpath(os.path.abspath(path), os.path.abspath(root))
    if rel.startswith(".."):
        print(f"cex: {path} is outside the repo", file=sys.stderr)
        return 1
    rel = rel.replace(os.sep, "/")
    if rel.startswith(".claude/"):
        print(f"cex: {rel} is scratch, not source — a pin must live where "
              f"{tool} compiles it", file=sys.stderr)
        return 1
    roots = spec["roots"]
    if roots is not None and not rel.startswith(roots):
        print(f"cex: {rel} is not under {', '.join(roots)} — a test "
              f"{tool} never compiles cannot guard anything", file=sys.stderr)
        return 1
    exts = spec["ext"] if isinstance(spec["ext"], tuple) else (spec["ext"],)
    if not rel.endswith(exts):
        print(f"cex: {rel} is not a {' or '.join(exts)} file — {tool} never reads "
              f"it, "
              f"so it cannot guard anything", file=sys.stderr)
        return 1
    if rel not in tracked(root):
        print(f"cex: {rel} is not tracked by git — commit it first, or the "
              f"pin guards a file nobody else has", file=sys.stderr)
        return 1
    testname = testname or cexid
    if cexid not in testname:
        print(f"cex: --test-name must contain {cexid}, so one test cannot "
              f"silently discharge several counterexamples", file=sys.stderr)
        return 1
    for other in current(root).values():
        opin = other.get("pin") or {}
        if (other.get("status") == "pinned" and other.get("cexId") != cexid
                and opin.get("file") == rel and opin.get("testName") == testname):
            print(f"cex: {rel}:{testname} is already the pin for "
                  f"{other['cexId']}", file=sys.stderr)
            return 1

    ok, why = verify_pin(root, row, rel, testname)
    if not ok:
        print(f"cex: refusing the pin — {why}", file=sys.stderr)
        return 1

    newrow = dict(row)
    newrow["status"] = "pinned"
    newrow["pin"] = {"file": rel, "testName": testname, "pinnedWhen": now()}
    append(root, newrow)
    print(f"cex: pinned {cexid} to {rel}:{testname}")
    print(f"cex: commit {CEX} — the guard only exists for others once it is "
          f"in the tree")
    return 0


def verify_pin(root, row, rel, testname):
    """(ok, why). The mechanical part of trusting a pin."""
    tool = row.get("tool") or "aiken"
    if tool == "blaster":
        import blaster
        if rel not in tracked(root):
            return False, "Blaster regression manifest must be tracked"
        return blaster.verify_pin(root, row, rel, testname)
    spec = TOOLS.get(tool)
    if spec is None:
        return False, f"recorded by unknown tool '{tool}'"
    p = os.path.join(root, rel)
    if not os.path.exists(p):
        return False, f"{rel} does not exist"
    try:
        with open(p, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError as e:
        return False, f"{rel} is unreadable: {e}"
    body, inverted = spec["body"](text, testname)
    if body is None:
        return False, f"{rel} has no test/method named `{testname}`"
    if inverted:
        # A reader may hand back its own reason instead of True.
        why = inverted if isinstance(inverted, str) else spec["inverted"]
        return False, f"`{testname}` is {why}"
    why = hollow(body, tool)
    if why:
        return False, f"`{testname}` proves nothing — {why}"
    need = required_leaves(row.get("containment"), tool)
    if not need:
        return False, ("this counterexample recorded no literal to check for; "
                       "it cannot be pinned mechanically yet")
    probed = [needle_for(n, tool) for n in need]
    ok, missing = contains_in_order(body, probed)
    if not ok and row.get("containmentAlt"):
        # The prover's own second spelling of the same value (Kani's raw
        # byte vectors) is the value too — but only whole: its leaves alone
        # would let `f(44, 1)` stand in for the u16 300.
        ok, _ = contains_in_order(body, [norm(row["containmentAlt"])])
    if not ok:
        shown = need[probed.index(missing)] if missing in probed else missing
        return False, (
            f"`{shown}` from the recorded counterexample is not in the body of "
            f"`{testname}` (needed, in order: {', '.join(need)}). The value "
            f"is evidence, not a restatement — paste it, do not retype it")
    return True, ""


def check(root):
    # The sweep's state is reported whether or not anything is recorded: a
    # repo whose gate pins seed 1 and has never swept past it is exploring
    # the same cases forever, and silence would read as coverage.
    fuzz_lines, fuzz_fatal = fuzz_status(root)
    for line in fuzz_lines:
        print(f"cex: {line}", file=sys.stderr if fuzz_fatal else sys.stdout)
    p = path_for(root)
    if not os.path.exists(p):
        print("cex: no counterexamples recorded — dormant")
        return 1 if fuzz_fatal else 0
    rows = current(root)
    pinned = [r for r in rows.values() if r.get("status") == "pinned"]
    openrows = [r for r in rows.values() if r.get("status") == "open"]
    blaster_open = [r for r in openrows if r.get("tool") == "blaster"]
    if blaster_open:
        print(f"cex: {len(blaster_open)} Blaster counterexample(s) require concrete replay and a pin, or explicit retirement", file=sys.stderr)
    if not pinned:
        print(f"cex: {len(openrows)} open counterexample(s), none pinned yet")
        return 1 if fuzz_fatal or blaster_open else 0
    bad = []
    for row in pinned:
        pinrec = row.get("pin") or {}
        ok, why = verify_pin(root, row, pinrec.get("file", ""),
                             pinrec.get("testName", ""))
        if not ok:
            bad.append((row, why))
    if not bad:
        if blaster_open:
            print(f"cex: RED — {len(pinned)} pins verified; {len(blaster_open)} Blaster counterexamples remain open", file=sys.stderr)
            return 1
        print(f"cex: green — {len(pinned)} pinned counterexample(s) still in "
              f"the tree, {len(openrows)} open")
        return 1 if fuzz_fatal or blaster_open else 0
    print("\ncex: RED — a pinned counterexample lost its regression\n",
          file=sys.stderr)
    for row, why in bad:
        pinrec = row.get("pin") or {}
        print(f"  {row['cexId']} ({row.get('selector')})", file=sys.stderr)
        print(f"      pinned to {pinrec.get('file')}:{pinrec.get('testName')}",
              file=sys.stderr)
        print(f"      {why}", file=sys.stderr)
    print(
        "\n  A counterexample whose test is gone is a bug with no evidence it is\n"
        "  still handled. Two ways out, both leaving a diff:\n"
        "    re-pin it   cex.py --pin <cexId> --file <path> --test-name <name>\n"
        "    retire it   cex.py --retire <cexId> --reason \"...\"  (also needs a\n"
        "                Decisions row in the work file naming the cexId)\n"
        "  If the pinned test was deleted and spec-guard is armed, it will want\n"
        "  a Decisions row for the obligation too.",
        file=sys.stderr)
    return 1


# ------------------------------------------------------------------ the sweep
# The gate pins `--seed 1` so a shrink is reproducible and this ledger can
# dedupe. The cost was never stated: every run of every property test then
# explores the SAME cases, at whatever iteration count the tool defaults to.
# Reproducible and shallow. The fix is the split mutation-guard already
# makes — the cheap half stays in `--full`, the expensive half runs
# off-session on a Routine and carries the exploration.
FUZZ = ".fluxpoint-fuzz.json"
SWEEP_DEFAULT_SEEDS = 25
SWEEP_DEFAULT_MAX_SUCCESS = 1000
SWEEP_TOOLS = {
    # `aiken check --help` on v1.1.9: `--seed <UINT>` seeds the generator and
    # `--max-success <UINT>` sets how many successful runs make a property
    # valid. Both are read here rather than assumed.
    "aiken": "aiken check --seed {seed} --max-success {max_success}",
}


def load_fuzz(root):
    p = os.path.join(root, FUZZ)
    if not os.path.exists(p):
        return None, []
    try:
        with open(p, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        return None, [f"{FUZZ} is not readable JSON ({e}) — no sweep is configured"]
    if not isinstance(doc, dict):
        return None, [f"{FUZZ} must be an object"]
    tool = doc.get("tool")
    if tool not in SWEEP_TOOLS and not doc.get("command"):
        return None, [f"{FUZZ}: 'tool' must be one of {', '.join(sorted(SWEEP_TOOLS))}, "
                      f"or give an explicit 'command' with {{seed}} in it"]
    return doc, []


def _commits_since(root, sha_):
    try:
        r = subprocess.run(["git", "-C", root, "rev-list", "--count", f"{sha_}..HEAD"],
                           capture_output=True, text=True, timeout=30)
        return int(r.stdout.strip()) if r.returncode == 0 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def fuzz_status(root):
    """(lines, fatal). What the wide sweep covered, and whether that is stale."""
    doc, problems = load_fuzz(root)
    if doc is None:
        return problems, bool(problems)
    last = doc.get("last")
    if not isinstance(last, dict) or not last.get("when"):
        return ([f"a wide fuzz sweep is declared in {FUZZ} and has never run — "
                 f"the gate's seed is pinned, so nothing has explored past it. "
                 f"Run: cex.py --sweep"], False)
    stale = _commits_since(root, last.get("headSha") or "")
    where = (f"seeds {last.get('from')}-{last.get('to')} at max-success "
             f"{last.get('maxSuccess')}, {last.get('found', 0)} new counterexample(s)")
    if stale is None:
        return ([f"last wide sweep: {where}, at a commit this clone no longer has "
                 f"({last.get('when')})"], bool(doc.get("failWhenStale")))
    if stale == 0:
        return ([f"last wide sweep: {where}, on this exact tree"], False)
    limit = doc.get("maxStaleCommits", 50)
    line = f"last wide sweep: {where}, {stale} commit(s) ago ({last.get('when')})"
    if stale > limit:
        return ([line + f" — past the {limit} this repo allows"],
                bool(doc.get("failWhenStale")))
    return [line], False


def sweep(root, seeds, max_success):
    """Run the prover over a rotating seed window and ingest what it finds."""
    doc, problems = load_fuzz(root)
    if doc is None:
        for p in problems:
            print(f"cex: {p}", file=sys.stderr)
        if not problems:
            print(f"cex: no {FUZZ} — declare the tool and the sweep width first, "
                  f"e.g. {{\"version\": 1, \"tool\": \"aiken\"}}", file=sys.stderr)
        return 1
    tool = doc.get("tool", "aiken")
    template = doc.get("command") or SWEEP_TOOLS[tool]
    seeds = seeds or int(doc.get("seeds") or SWEEP_DEFAULT_SEEDS)
    max_success = max_success or int(doc.get("maxSuccess") or SWEEP_DEFAULT_MAX_SUCCESS)
    # Start where the last sweep stopped: repeating seed 1 forever is the
    # very shallowness this exists to fix.
    start = int(((doc.get("last") or {}).get("to") or 1)) + 1
    before = len(current(root))
    ran = failed = 0
    for seed in range(start, start + seeds):
        cmd = template.format(seed=seed, max_success=max_success)
        try:
            r = subprocess.run(shell_argv(cmd), cwd=root, capture_output=True,
                               text=True, timeout=3600)
        except (OSError, subprocess.SubprocessError) as e:
            print(f"cex: seed {seed}: could not run {cmd!r}: {e}", file=sys.stderr)
            return 1
        ran += 1
        if r.returncode != 0:
            failed += 1
        ingest(root, r.stdout, r.returncode, tool)
    found = len(current(root)) - before
    doc["last"] = {"from": start, "to": start + seeds - 1, "seeds": seeds,
                   "maxSuccess": max_success, "found": found, "failingSeeds": failed,
                   "headSha": head_sha(root), "when": now()}
    with open(os.path.join(root, FUZZ), "w", encoding="utf-8", newline="\n") as fh:
        json.dump(doc, fh, indent=2, sort_keys=True)
        fh.write("\n")
    print(f"cex: swept seeds {start}-{start + seeds - 1} at max-success "
          f"{max_success}: {failed}/{ran} run(s) failed, {found} new "
          f"counterexample(s)")
    print(f"cex: commit {FUZZ} and {CEX} — a sweep nobody else can see is one "
          f"that did not happen")
    return 0


def work_text(root):
    """The Decisions section of the work file — the tracked, reviewed place."""
    for name in ("WORK.md", "LOOP.md"):
        p = os.path.join(root, name)
        if not os.path.exists(p):
            continue
        try:
            with open(p, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        m = re.search(r"^##\s+Decisions\s*$(.*?)(?=^##\s|\Z)", text, re.S | re.M | re.I)
        return m.group(1) if m else ""
    return ""


def retire(root, cexid, reason):
    row = resolve(root, cexid)
    if not row:
        print(f"cex: no such counterexample '{cexid}'", file=sys.stderr)
        return 1
    if len((reason or "").strip()) < 20:
        print("cex: --reason must be at least 20 characters — retiring is the "
              "one move that weakens this ratchet", file=sys.stderr)
        return 1
    if cexid not in work_text(root):
        print(f"cex: refusing to retire {cexid} — no Decisions row in the work "
              f"file names it.\n"
              f"  A reason in a store nobody reads is not accountability. Add a "
              f"row naming {cexid},\n"
              f"  the same way spec-guard requires one to forgive a removed "
              f"obligation.", file=sys.stderr)
        return 1
    newrow = dict(row)
    newrow["status"] = "retired"
    newrow["retire"] = {"reason": reason.strip(), "when": now()}
    append(root, newrow)
    print(f"cex: retired {cexid}")
    pinrec = row.get("pin") or {}
    if pinrec.get("testName"):
        print(f"cex: if you also delete the test and spec-guard is armed, it "
              f"will want a Decisions row for:\n"
              f"      {row.get('tool')}:{pinrec.get('file')}:{pinrec.get('testName')}")
    return 0


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=".")
    ap.add_argument("--from", dest="src", help="prover output file (default stdin)")
    ap.add_argument("--exit", type=int, default=0, help="the prover's exit code")
    ap.add_argument("--tool", default="aiken", help=f"one of {sorted(TOOLS)}")
    ap.add_argument("--file")
    ap.add_argument("--test-name")
    ap.add_argument("--reason", default="")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--ingest", action="store_true")
    g.add_argument("--pin", metavar="CEXID")
    g.add_argument("--retire", metavar="CEXID")
    g.add_argument("--check", action="store_true")
    g.add_argument("--sweep", action="store_true",
                   help="run the prover over a rotating seed window (off-session)")
    g.add_argument("--list", action="store_true")
    ap.add_argument("--seeds", type=int, help="how many seeds the sweep covers")
    ap.add_argument("--max-success", type=int, dest="max_success",
                    help="successful runs per property during the sweep")
    a = ap.parse_args()

    if a.ingest:
        raw = (open(a.src, encoding="utf-8", errors="replace").read()
               if a.src else sys.stdin.read())
        return ingest(a.root, raw, a.exit, a.tool, a.src)
    if a.pin:
        return pin(a.root, a.pin, a.file, a.test_name)
    if a.retire:
        return retire(a.root, a.retire, a.reason)
    if a.check:
        return check(a.root)
    if a.sweep:
        return sweep(a.root, a.seeds, a.max_success)

    rows = current(a.root)
    if not rows:
        print("cex: no counterexamples recorded")
        return 0
    by = {}
    for r in rows.values():
        by.setdefault(r.get("status"), []).append(r)
    print(f"cex: {len(rows)} counterexample(s)")
    for status in sorted(by):
        print(f"  [{status}] {len(by[status])}")
        for r in by[status][:10]:
            val = (r.get("input") or r.get("assertion") or "")[:60]
            print(f"      {r['cexId']}  [{r.get('tool')}] {r.get('selector')}  {val}")
            if r.get("pin"):
                print(f"          pinned to {r['pin'].get('file')}:"
                      f"{r['pin'].get('testName')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
