#!/usr/bin/env python3
"""Relation gate: check artifact PAIRS, not artifact health.

The Stop gate has two evidence channels — a harness exit code and a regex
over added lines — and both measure one artifact in isolation. That misses
an entire defect class, because the expensive failures are *relationships*:

    An on-chain predicate is tightened. Its off-chain transaction builder
    is not. Every test passes, on both sides, because each artifact is
    individually correct — and the change rejects 100% of honest
    transactions.

No count of passing tests can see that. The suite is green precisely
because nothing in it evaluates the pair together.

So the pair is declared, in a repo-owned `.fluxpoint-pairs.json`, and
checked three ways:

  co-change     A commit that moves one side of a declared pair and not the
                other is reported. Cheap, catches the common case, and says
                nothing about whether the two sides *agree* — only that
                somebody changed one and forgot the other.
  parity        A command whose job is to build the off-chain artifact and
                evaluate the on-chain one against it, so the relation itself
                becomes an exit code. This is the real check; co-change is
                the smoke alarm.
  differential  The parity nobody has to hand-roll: a generator emits
                inputs, both implementations run over every one, and any
                difference in exit code, stdout OR stderr — byte for byte,
                error text and key order included — is a divergence. Seven
                rounds of review on one keeper found every HIGH in a
                hand-written reader that "delegated to the same code"; two
                hand-rolled comparisons got it wrong before a differential
                settled it (52,869 documents, 0 differences).

An acknowledgement clears CO-CHANGE, never parity. `.fluxpoint-pair-acks.json`
holds `{pair, source_sha, why}`; `source_sha` addresses the CURRENT bytes of the
source files the alarm named, so editing them again makes the ack stale and the
alarm re-opens on its own. That is the difference between recording that someone
read the mirror and switching the alarm off.

## A parity that cannot fail is not a parity

A parity command that passes because it compares nothing is indistinguishable
from one that passes because the artifacts agree — the same false green
`guard-guard.py` refuses for guards. So a pair may declare a `bite`: a
mutation of the reader (`file`, `find`, `replace`, optional `compile`), and
`--verify` proves the parity intact, mutates the reader, requires the parity
to go RED, and restores — reading the file back after both the write and the
restore, because an anchor that misses is a silent no-op that looks exactly
like a survivor, and a restore that did not restore leaves every later
measurement running against a mutant. The `compile` exit is printed on its own
line every time: under a test runner that compiles in a global setup, a mutant
that does not compile reports zero failures and reads as a survivor.

`--check` and `--list` name a parity with no bite ("nothing has shown it can
fail"); `--verify` fails it. Like `guard-guard.py --verify`, this belongs
off-session, on a Routine, where the full suite already runs.

## Authority

`source` and `mirror` read as symmetric, and the defect is not: one side is
the funds-moving path and the other is a reader of it. `authority: "source"`
(or `"mirror"`) names which, so a divergence is reported as *the reader
diverged from the authority* rather than *they differ*, a `bite` is refused
when it would mutate the authority to prove a reader notices, and `--scan`
knows which direction a suggested pair runs.

## Undeclared mirrors

Nobody declares a mirror they do not know they wrote. `--scan` proposes them
from three cheap, high-signal heuristics over the tracked and untracked code:
the same non-trivial error message thrown from two modules; two modules whose
thrown-message sets overlap; and a comment or docstring claiming to mirror or
delegate to another module ("same code", "mirror of", "as the keeper does").
Output is a JSON list of suggested manifest entries on stdout with the
evidence in each `why`, exit 0 always — a suggestion, never a gate. The
parity, differential and bite are the repo's to write.

  pair-guard.py --check              co-change over the diff, then parity + differential
  pair-guard.py --check --against origin/main    diff a branch, for CI
  pair-guard.py --verify             prove every parity bites (mutate, RED, restore)
  pair-guard.py --scan               propose undeclared mirrors as manifest entries
  pair-guard.py --list               what is declared, and what is not

Differential protocol: the generator runs once with FPL_PAIR_SEED and
FPL_PAIR_CASES in its environment and prints one case per line (opaque bytes;
one JSON document per line is the recommendation). Each case is written, with
a trailing newline, to the stdin of `sourceRun` and `mirrorRun`, and the two
(exit, stdout, stderr) triples must be identical. No normalisation exists on
purpose: a command that needs to canonicalise does it inside the command,
where the choice is reviewable. `--seed` and `--cases` override the manifest
so a divergence is reproducible from the line that reported it; the
FPL_PAIR_CASES environment variable caps the count so a harness can size the
run.

Dormant by design: no manifest means no declared relations, exit 0.
"""
import argparse
import atexit
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile

from process_tree import shell_argv

MANIFEST = ".fluxpoint-pairs.json"
ACKS = ".fluxpoint-pair-acks.json"
ACK_FIELDS = {"pair", "source_sha", "why"}
ACK_WHY_FLOOR = 60
PAIR_FIELDS = {"id", "source", "mirror", "parity", "symmetric", "why",
               "differential", "bite", "authority"}
DIFF_FIELDS = {"generator", "sourceRun", "mirrorRun", "cases", "seed", "timeout"}
BITE_FIELDS = {"file", "find", "replace", "compile"}
AUTHORITY = {"source", "mirror"}
SENTINEL = ".fluxpoint-pairs-restoring"
DIFF_DEFAULT_CASES = 200
DIFF_DEFAULT_SEED = 1
DIFF_DEFAULT_TIMEOUT = 30
DIFF_DETAIL_CAP = 5

# --scan corpus and heuristics.
SCAN_EXT = {".py", ".ts", ".tsx", ".js", ".mjs", ".cjs", ".rs", ".go", ".ak",
            ".java", ".kt"}
SCAN_SKIP_SEGMENTS = {"tests", "test", "__tests__", "spec", "node_modules",
                      "dist", "build", "out", "coverage", ".git", "vendor",
                      "__pycache__", ".claude"}
SCAN_TEST_FILE = re.compile(r"(?:^|[/_.-])(?:test|spec)s?(?:[/_.-]|$)|_test\.|\.test\.|\.spec\.")
# A thrown message: a throw/raise/panic/fail followed, within the statement,
# by a string literal long enough to be a sentence rather than a token.
SCAN_THROW = re.compile(
    r"\b(?:throw|raise|panic!|fail|error|bail!|anyhow!|Error|Exception|Err)\b"
    r"[^\n;]{0,80}?(?:@?)([\"'`])((?:(?!\1)[^\n\\]|\\.){12,})\1")
SCAN_CLAIM = re.compile(
    r"\bmirror(?:s|ed|ing)?\s+of\b|\bsame\s+code\b|\bas\s+the\s+\w+\s+(?:does|checks|parses|validates|reads)\b"
    r"|\bdelegat(?:es|ing|e)\s+to\b|\bkept\s+in\s+sync\s+with\b|\bmust\s+match\b|\bre-?implement(?:s|ation|ed)?\s+(?:of|the)\b",
    re.I)
SCAN_SOURCEISH = re.compile(r"contract|validator|keeper|sign|submit|\btx\b|ledger|parser", re.I)
SCAN_MIN_SHARED_LEN = 24
SCAN_MIN_SHARED_WORDS = 4
SCAN_OVERLAP_MIN_SHARED = 3
SCAN_OVERLAP_JACCARD = 0.3


def glob_to_re(pat):
    """Translate a path glob to a regex, with `**` spanning directories.

    fnmatch is not usable here: it treats `*` as matching `/` too, so
    `contracts/*.ak` would match a file three directories down and a pair
    would silently cover more than it claims.
    """
    out, i = [], 0
    while i < len(pat):
        c = pat[i]
        if pat.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pat.startswith("**", i):
            out.append(".*")
            i += 2
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def as_list(v):
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def matches(patterns, files):
    hit = []
    for pat in patterns:
        rx = glob_to_re(pat)
        hit += [f for f in files if rx.match(f)]
    return sorted(set(hit))


def _resolve(root, rel):
    return os.path.normcase(os.path.realpath(os.path.join(root, rel)))


def _inside(root, rel):
    real_root = os.path.normcase(os.path.realpath(root))
    real = _resolve(root, rel)
    try:
        return os.path.commonpath([real, real_root]) == real_root
    except ValueError:
        return False


def _side_of(p, rel):
    """Which side of the pair a file belongs to: 'source', 'mirror', or None."""
    if matches(as_list(p.get("source")), [rel]):
        return "source"
    if matches(as_list(p.get("mirror")), [rel]):
        return "mirror"
    return None


def load_manifest(root):
    """Parse and validate the manifest. A malformed pair is a hard error.

    Accepting a half-declared pair would mean a relation nobody checks
    while the manifest claims coverage — the same silent-inertness failure
    the IR's closed field registry exists to prevent.
    """
    path = os.path.join(root, MANIFEST)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        try:
            data = json.load(fh)
        except json.JSONDecodeError as e:
            raise SystemExit(f"pair-guard: {MANIFEST} is not valid JSON: {e}")
    if not isinstance(data, list):
        raise SystemExit(f"pair-guard: {MANIFEST} must be a list of pairs")
    for i, p in enumerate(data):
        where = f"{MANIFEST}[{i}]"
        if not isinstance(p, dict):
            raise SystemExit(f"pair-guard: {where} must be an object")
        unknown = sorted(set(p) - PAIR_FIELDS)
        if unknown:
            raise SystemExit(
                f"pair-guard: {where}: unknown field(s) {unknown} — known: "
                f"{', '.join(sorted(PAIR_FIELDS))}")
        if not p.get("id"):
            raise SystemExit(f"pair-guard: {where}: id required")
        where = f"{where} ('{p['id']}')"
        if not p.get("source") or not as_list(p.get("mirror")):
            raise SystemExit(
                f"pair-guard: {where}: both source and mirror "
                f"are required — a pair with one side is not a relation")
        auth = p.get("authority")
        if auth is not None and auth not in AUTHORITY:
            raise SystemExit(
                f"pair-guard: {where}: authority must be one of "
                f"{', '.join(sorted(AUTHORITY))} — it names the funds-moving "
                f"side, and a value that names neither protects nothing")
        d = p.get("differential")
        if d is not None:
            if not isinstance(d, dict):
                raise SystemExit(f"pair-guard: {where}: differential must be an object")
            unknown = sorted(set(d) - DIFF_FIELDS)
            if unknown:
                raise SystemExit(
                    f"pair-guard: {where}: differential has unknown field(s) "
                    f"{unknown} — known: {', '.join(sorted(DIFF_FIELDS))}")
            for k in ("generator", "sourceRun", "mirrorRun"):
                if not isinstance(d.get(k), str) or not d[k].strip():
                    raise SystemExit(
                        f"pair-guard: {where}: differential.{k} is required — "
                        f"a differential with no {k} compares nothing")
            for k, floor in (("cases", 1), ("seed", None), ("timeout", 1)):
                v = d.get(k)
                if v is None:
                    continue
                if isinstance(v, bool) or not isinstance(v, int) or (
                        floor is not None and v < floor):
                    raise SystemExit(
                        f"pair-guard: {where}: differential.{k} must be an "
                        f"integer" + (f" >= {floor}" if floor is not None else ""))
        bites = p.get("bite")
        if bites is not None:
            if isinstance(bites, dict):
                bites = [bites]
            if not isinstance(bites, list) or not bites:
                raise SystemExit(f"pair-guard: {where}: bite must be an object or a "
                                 f"non-empty list of objects")
            if not p.get("parity") and d is None:
                raise SystemExit(
                    f"pair-guard: {where}: bite without parity or differential is "
                    f"half-declared — a mutation proves something only about a "
                    f"check that runs")
            for j, b in enumerate(bites):
                bw = f"{where} bite[{j}]"
                if not isinstance(b, dict):
                    raise SystemExit(f"pair-guard: {bw} must be an object")
                unknown = sorted(set(b) - BITE_FIELDS)
                if unknown:
                    raise SystemExit(
                        f"pair-guard: {bw}: unknown field(s) {unknown} — known: "
                        f"{', '.join(sorted(BITE_FIELDS))}")
                rel = b.get("file")
                if not isinstance(rel, str) or not rel.strip():
                    raise SystemExit(f"pair-guard: {bw}: file required")
                # The verifier WRITES to this path. A manifest arriving by PR
                # is reviewed as data, and a reviewer scanning a "file" field
                # is not primed to read it as "this path gets overwritten".
                if os.path.isabs(rel) or not _inside(root, rel):
                    raise SystemExit(f"pair-guard: {bw}: file '{rel}' is outside the repo")
                for k in ("find", "replace"):
                    if not isinstance(b.get(k), str):
                        raise SystemExit(f"pair-guard: {bw}: {k} required (a string)")
                if not b["find"]:
                    raise SystemExit(f"pair-guard: {bw}: find must not be empty")
                if b["find"] == b["replace"]:
                    raise SystemExit(f"pair-guard: {bw}: find equals replace — "
                                     f"the mutation changes nothing")
                if b.get("compile") is not None and (
                        not isinstance(b["compile"], str) or not b["compile"].strip()):
                    raise SystemExit(f"pair-guard: {bw}: compile must be a non-empty string")
                side = _side_of(p, rel.replace(os.sep, "/"))
                if side is None:
                    raise SystemExit(
                        f"pair-guard: {bw}: file '{rel}' is not part of this pair "
                        f"(matches neither source nor mirror) — mutating it proves "
                        f"nothing about this relation")
                if auth and side == auth:
                    raise SystemExit(
                        f"pair-guard: {bw}: file '{rel}' is on the authority side "
                        f"({auth}) — a bite disables the READER to prove the parity "
                        f"notices; disabling the funds path proves the wrong thing")
                b["_side"] = side
            p["bite"] = bites
    return data


def changed_files(root, against):
    """Paths this change touches, tracked edits plus new untracked files.

    Mirrors the Stop gate's rule rather than inventing a second one: the
    marker cannot see files written through the Bash tool, so dirtiness is
    re-derived from git.
    """
    def git(*a):
        r = subprocess.run(["git", "-C", root, *a], capture_output=True, text=True)
        return [x for x in r.stdout.splitlines() if x] if r.returncode == 0 else []

    if against == "HEAD":
        files = git("diff", "HEAD", "--name-only", "--diff-filter=ACMR")
    else:
        files = git("diff", f"{against}...HEAD", "--name-only", "--diff-filter=ACMR")
        files += git("diff", "HEAD", "--name-only", "--diff-filter=ACMR")
    # Untracked files are changed relative to EVERY base, so this sits outside
    # the branch above. Listing them only under HEAD meant the Stop gate's
    # --against path could not see them at all: a mirror written as a new file
    # false-REDded the co-change rule over work sitting right there, and a
    # source change existing only untracked was invisible — both directions of
    # the docstring's own contract, broken on exactly the path the Stop gate
    # takes.
    files += git("ls-files", "--others", "--exclude-standard")
    return sorted(set(files))


def source_sha(root, paths):
    """Content address over the source files a co-change alarm named.

    The whole value of an acknowledgement is that it CANNOT become a blanket
    exemption. Keying it on the bytes that tripped the alarm means the next edit
    to those same files produces a different address, the ack goes stale, and the
    alarm re-opens on its own -- the pattern a consuming repo already applies to
    its provenance census and hash surface, for the same reason.

    The path is hashed with its own length in front of it, so no arrangement of
    names and contents can collide with a different arrangement.
    """
    h = hashlib.sha256()
    for rel in sorted(paths):
        b = rel.encode("utf-8")
        h.update(str(len(b)).encode("ascii") + b":" + b)
        try:
            with open(os.path.join(root, rel), "rb") as fh:
                body = fh.read()
        except FileNotFoundError:
            body = b"<deleted>"
        h.update(str(len(body)).encode("ascii") + b":")
        h.update(body)
    return h.hexdigest()


def load_acks(root, pair_ids):
    """Parse and validate the ack file. A malformed ack is a hard error.

    An ack silences a check, so a half-declared one is strictly worse than none:
    it is a gate somebody believes is armed. Naming a pair that does not exist is
    refused for the same reason -- an ack matching nothing reads as coverage of
    something.
    """
    path = os.path.join(root, ACKS)
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        try:
            data = json.load(fh)
        except json.JSONDecodeError as e:
            raise SystemExit("pair-guard: %s is not valid JSON: %s" % (ACKS, e))
    if not isinstance(data, list):
        raise SystemExit("pair-guard: %s must be a list of acknowledgements" % ACKS)
    for i, a in enumerate(data):
        where = "%s[%d]" % (ACKS, i)
        if not isinstance(a, dict):
            raise SystemExit("pair-guard: %s must be an object" % where)
        unknown = sorted(set(a) - ACK_FIELDS)
        if unknown:
            raise SystemExit("pair-guard: %s has unknown field(s): %s" % (where, unknown))
        for f in ("pair", "source_sha", "why"):
            if not isinstance(a.get(f), str) or not a[f].strip():
                raise SystemExit("pair-guard: %s needs a non-empty %r" % (where, f))
        if a["pair"] not in pair_ids:
            raise SystemExit(
                "pair-guard: %s acknowledges %r, which is not a declared pair. "
                "An ack that matches nothing reads as coverage." % (where, a["pair"]))
        if len(a["why"].strip()) < ACK_WHY_FLOOR:
            raise SystemExit(
                "pair-guard: %s needs a real reason (%d+ chars). The ack records "
                "that a human READ the mirror and found it unaffected; a reason too "
                "short to say why is not that record." % (where, ACK_WHY_FLOOR))
    return data


def reader_words(p):
    """How a failure names the two sides: neutral, or reader vs authority."""
    auth = p.get("authority")
    if not auth:
        return None
    reader = "mirror" if auth == "source" else "source"
    return (f"the reader ({', '.join(as_list(p[reader]))}) diverged from the "
            f"authority ({', '.join(as_list(p[auth]))})")


# ------------------------------------------------------------- differential
def _run(cmd, root, stdin=None, env=None, timeout=None):
    """(rc, stdout, stderr) as bytes; rc None on timeout."""
    try:
        r = subprocess.run(shell_argv(cmd), cwd=root, input=stdin,
                           capture_output=True, env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, b"", b""
    return r.returncode, r.stdout, r.stderr


def _bad_rc(rc):
    """A verdict about the RUN, not the relation: killed, timed out, absent."""
    if rc is None:
        return "timed out"
    if rc < 0 or rc >= 128:
        return f"killed (rc={rc})"
    if rc == 127:
        return "not found (rc=127) — nothing ran"
    return ""


def run_differential(root, p, cases=None, seed=None):
    """{ok, refused, cases, seed, count, detail, distinct, all_same, words}.

    `refused` is set when the run could not establish anything — a generator
    that failed or emitted too little, a side that was killed or absent —
    and is reported as RED without being called a divergence, because those
    are different sentences and only one of them is about the code.
    """
    d = p["differential"]
    cases = int(cases if cases is not None else
                os.environ.get("FPL_PAIR_CASES") or d.get("cases", DIFF_DEFAULT_CASES))
    seed = int(seed if seed is not None else d.get("seed", DIFF_DEFAULT_SEED))
    timeout = int(d.get("timeout", DIFF_DEFAULT_TIMEOUT))
    env = dict(os.environ, FPL_PAIR_SEED=str(seed), FPL_PAIR_CASES=str(cases))
    out = {"ok": False, "refused": "", "cases": cases, "seed": seed, "count": 0,
           "detail": [], "distinct": 0, "all_same": False}
    rc, gout, gerr = _run(d["generator"], root, env=env, timeout=max(timeout, 600))
    why = _bad_rc(rc) if rc is None or rc >= 127 else ""
    if rc != 0:
        tail = (gerr or gout).decode("utf-8", "replace").strip().splitlines()[-3:]
        out["refused"] = (f"the generator {why or f'exited {rc}'}: {d['generator']}"
                          + ("\n      " + "\n      ".join(tail) if tail else ""))
        return out
    lines = gout.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    if len(lines) < cases:
        out["refused"] = (f"the generator produced {len(lines)} of {cases} cases "
                          f"(seed {seed}) — a differential over fewer inputs than "
                          f"declared reads as coverage it did not deliver")
        return out
    lines = lines[:cases]
    out["distinct"] = len(set(lines))
    if out["distinct"] < 2:
        out["refused"] = (f"the generator emitted {out['distinct']} distinct case(s) "
                          f"in {cases} — one input compares nothing")
        return out
    source_triples = set()
    for i, case in enumerate(lines):
        s = _run(d["sourceRun"], root, stdin=case + b"\n", env=env, timeout=timeout)
        m = _run(d["mirrorRun"], root, stdin=case + b"\n", env=env, timeout=timeout)
        for name, (rc2, _, _) in (("sourceRun", s), ("mirrorRun", m)):
            bad = _bad_rc(rc2)
            if bad:
                out["refused"] = (f"{name} {bad} on case {i} — a run that did not "
                                  f"finish evaluated nothing, and its silence is "
                                  f"not agreement")
                return out
        source_triples.add(s)
        if s != m:
            out["count"] += 1
            if len(out["detail"]) < DIFF_DETAIL_CAP:
                out["detail"].append((i, case, s, m))
    out["all_same"] = len(source_triples) == 1
    out["ok"] = out["count"] == 0
    return out


def _b(x, n=200):
    return x[:n].decode("utf-8", "replace").replace("\n", "\\n")


def report_differential(p, r, failures):
    """Print the differential verdict; append to failures when RED."""
    pid = p["id"]
    if r["refused"]:
        failures.append(f"{pid}: differential could not run — {r['refused']}")
        return
    if r["ok"]:
        print(f"  {pid}: differential ok — {r['cases']} cases, seed {r['seed']}, "
              f"0 divergences")
        if r["all_same"]:
            print(f"  {pid}: NOTE every case produced the identical source result — "
                  f"the generator is probably not reaching the checks")
        return
    who = reader_words(p)
    lines = [f"{pid}: differential DIVERGED on {r['count']} of {r['cases']} cases "
             f"(seed {r['seed']})" + (f" — {who}" if who else "")]
    for i, case, s, m in r["detail"]:
        lines.append(f"      case {i}  input: {_b(case)}")
        lines.append(f"        source rc={s[0]} stdout: {_b(s[1])} stderr: {_b(s[2])}")
        lines.append(f"        mirror rc={m[0]} stdout: {_b(m[1])} stderr: {_b(m[2])}")
    lines.append(f"      re-run: pair-guard.py --check --seed {r['seed']} --cases {r['cases']}")
    failures.append("\n".join(lines))


def run_parity_cmd(root, p):
    """(rc, tail) of the declared parity command."""
    r = subprocess.run(shell_argv(p["parity"]), cwd=root, capture_output=True, text=True)
    tail = (r.stdout + r.stderr).strip().splitlines()[-3:]
    return r.returncode, tail


def check(root, against, run_parity, report_only, cases=None, seed=None):
    pairs = load_manifest(root)
    if pairs is None:
        print(f"pair-guard: no {MANIFEST} — no declared relations to check")
        return 0
    if not pairs:
        print(f"pair-guard: {MANIFEST} declares no pairs")
        return 0
    if os.path.exists(os.path.join(root, SENTINEL)):
        print(f"pair-guard: {SENTINEL} is present — a --verify was interrupted "
              f"with a mutant possibly still on disk. Inspect and remove it "
              f"before trusting this tree.", file=sys.stderr)
        return 1

    acks = load_acks(root, {p["id"] for p in pairs})
    files = changed_files(root, against)
    failures, checked = [], 0
    honoured = set()

    for p in pairs:
        src = matches(as_list(p["source"]), files)
        mir = matches(as_list(p["mirror"]), files)
        if src and not mir:
            # An ack discharges CO-CHANGE and nothing else. It is the record that
            # a human opened the mirror and found it genuinely unaffected -- the
            # one answer this alarm asks for and had no slot for. It is keyed on
            # the CURRENT bytes of the source files named here, so the next edit
            # to them re-opens the alarm without anybody remembering to.
            now = source_sha(root, src)
            ack = next((a for a in acks
                        if a["pair"] == p["id"] and a["source_sha"] == now), None)
            if ack:
                honoured.add(id(ack))
                print(f"  {p['id']}: co-change acknowledged — {ack['why'].strip()}")
            else:
                stale = [a for a in acks if a["pair"] == p["id"]]
                failures.append(
                    f"{p['id']}: {', '.join(src[:3])} changed, but nothing matching "
                    f"{as_list(p['mirror'])} did"
                    + (f" — {p['why']}" if p.get("why") else "")
                    + (f"\n      An ack exists for this pair but addresses different "
                       f"bytes (source is now {now[:12]}...): the source was edited "
                       f"again after it was written, so it no longer says anything "
                       f"about what is there. Re-read the mirror and re-ack."
                       if stale else
                       f"\n      If the mirror genuinely needs no change, record that "
                       f"in {ACKS}: {{\"pair\": \"{p['id']}\", \"source_sha\": "
                       f"\"{now}\", \"why\": \"...\"}}"))
        elif mir and not src and p.get("symmetric"):
            failures.append(
                f"{p['id']}: {', '.join(mir[:3])} changed, but nothing matching "
                f"'{p['source']}' did"
                + (f" — {p['why']}" if p.get("why") else ""))
        if src or mir:
            checked += 1

    if files:
        print(f"pair-guard: {len(pairs)} declared pair(s), {checked} touched by "
              f"this change")
    else:
        print(f"pair-guard: {len(pairs)} declared pair(s), no changes to compare")

    # Parity is the real check and runs regardless of what moved: two sides
    # can disagree without either being edited today, and --full is meant to
    # be everything the Definition of Done requires.
    if run_parity:
        for p in pairs:
            cmd = p.get("parity")
            has_diff = p.get("differential") is not None
            if not cmd and not has_diff:
                print(f"  {p['id']}: co-change only — no parity command declared")
                continue
            if cmd:
                rc, tail = run_parity_cmd(root, p)
                if rc == 0:
                    print(f"  {p['id']}: parity ok")
                else:
                    who = reader_words(p)
                    failures.append(
                        f"{p['id']}: parity command failed (exit {rc}): {cmd}"
                        + (f"\n      {who}" if who else "")
                        + ("\n      " + "\n      ".join(tail) if tail else ""))
            if has_diff:
                report_differential(p, run_differential(root, p, cases, seed), failures)
            if not p.get("bite"):
                # Loud where it cannot know: a parity nobody has shown to fail
                # is reported, never failed here — --verify is where it fails.
                print(f"  {p['id']}: parity declared, no bite — nothing has shown "
                      f"it can fail; declare one and run --verify")

    # An ack that discharged nothing is not harmless: it reads, to the next
    # person, as a relation somebody vouched for. Say it is spent.
    for a in acks:
        if id(a) not in honoured:
            print(f"  {a['pair']}: ack on file is unused — either the mirror moved "
                  f"with the source this time, or the source changed since. Drop it.")

    if failures and not report_only:
        print("\npair-guard: RED — a declared relation is broken or unverified\n",
              file=sys.stderr)
        for f in failures:
            print(f"  {f}", file=sys.stderr)
        print("\n  Both artifacts can pass their own tests while the pair is "
              "wrong.\n", file=sys.stderr)
        return 1
    if not failures:
        print("pair-guard: green")
    return 0


# ------------------------------------------------------------------- verify
# A reader is mutated on disk for the length of one parity run. `finally`
# covers an exception and Ctrl-C; it does NOT cover SIGTERM, which is what CI
# cancellation sends. The pending restore is replayed from a signal handler
# and at interpreter exit, and while the window is open a sentinel sits in
# the repo where a human or the next --check will see it.
_PENDING = {}
_SENTINEL = []


def _restore_pending():
    """Put every mutated file back and VERIFY it is back.

    A restore that did not restore is the trap the issue names: an anchor
    miss left a mutant in place and every later measurement ran against
    corrupted source. So the file is read back after the copy, and a
    mismatch leaves the backup and the sentinel on disk and says so.
    """
    problems = []
    for target, backup in list(_PENDING.items()):
        try:
            shutil.copyfile(backup, target)
            with open(backup, "rb") as fh:
                want = fh.read()
            with open(target, "rb") as fh:
                got = fh.read()
            if got != want:
                problems.append(f"{target} differs from its backup {backup} after restore")
                continue
            os.unlink(backup)
        except OSError as e:
            problems.append(f"{target}: restore failed ({e}); backup kept at {backup}")
            continue
        _PENDING.pop(target, None)
    if not _PENDING:
        for s in _SENTINEL:
            try:
                os.unlink(s)
            except OSError:
                pass
        _SENTINEL.clear()
    for pr in problems:
        print(f"pair-guard: RESTORE DID NOT RESTORE — {pr}; the sentinel stays "
              f"on disk", file=sys.stderr)
    return problems


atexit.register(_restore_pending)


def _on_signal(signum, _frame):
    _restore_pending()
    raise SystemExit(128 + signum)


for _sig in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGHUP", None)):
    if _sig is not None:
        try:
            signal.signal(_sig, _on_signal)
        except (OSError, ValueError):
            pass


def _checks_verdict(root, p, cases, seed):
    """Run parity and/or differential once.

    Returns (status, detail) with status in {'pass', 'fail', 'refused'}:
    'pass' means every declared check was green, 'fail' means at least one
    check went RED for a reason that is about the relation, 'refused' means
    a check could not establish anything (killed, not found, generator
    broke) — which is evidence of nothing in either direction.
    """
    status, details = "pass", []
    if p.get("parity"):
        rc, tail = run_parity_cmd(root, p)
        bad = _bad_rc(rc)
        if bad:
            return "refused", f"parity {bad}: {p['parity']}"
        if rc != 0:
            status = "fail"
            details.append(f"parity exit {rc}")
    if p.get("differential") is not None:
        r = run_differential(root, p, cases, seed)
        if r["refused"]:
            return "refused", r["refused"]
        if not r["ok"]:
            status = "fail"
            details.append(f"differential {r['count']} divergence(s)")
    return status, ", ".join(details) or "all checks green"


def _compile(root, b, when):
    """Run the declared compile, printing its exit on its own line, always."""
    cmd = b.get("compile")
    if not cmd:
        return 0
    rc, out, err = _run(cmd, root, timeout=900)
    print(f"    compile  -> rc={rc} ({when})")
    return rc


def verify_bite(root, p, b, cases, seed):
    """(ok, why). Prove one bite: intact green, mutated RED, restored."""
    rel = b["file"]
    path = os.path.join(root, rel)
    try:
        with open(path, "rb") as fh:
            original = fh.read()
    except OSError as e:
        return False, f"{rel} is unreadable: {e}"

    find, replace = b["find"].encode("utf-8"), b["replace"].encode("utf-8")
    n = original.count(find)
    if n != 1:
        return False, (f"anchor matched {n} time(s) in {rel}; it must match exactly "
                       f"once — a replace on the first of several hits, or on none, "
                       f"is a silent no-op that reads as a survivor: {b['find']!r}")
    mutated = original.replace(find, replace, 1)

    rc = _compile(root, b, "intact")
    if rc != 0:
        return False, (f"the tree does not compile INTACT (rc={rc}) — nothing "
                       f"measured on it testifies to anything; fix the build first")
    status, detail = _checks_verdict(root, p, cases, seed)
    if status == "refused":
        return False, (f"the check could not run with the reader intact — {detail}; "
                       f"a check that cannot run cannot testify that it bites")
    if status == "fail":
        return False, (f"the check is RED with the reader INTACT ({detail}) — a "
                       f"parity that fails on a healthy tree cannot show that "
                       f"failing under mutation means anything")
    print("    intact   -> checks green")

    backup = tempfile.NamedTemporaryFile("wb", delete=False)
    backup.write(original)
    backup.close()
    sentinel = os.path.join(root, SENTINEL)
    try:
        with open(path, "wb") as fh:
            fh.write(mutated)
        # Registered only once the write is CLOSED, not before it: a restore
        # that fires from the signal handler while this handle is still open
        # is undone by the flush on close.
        _PENDING[path] = backup.name
        with open(sentinel, "w", encoding="utf-8") as fh:
            fh.write(f"{p['id']}\n{rel}\n")
        _SENTINEL[:] = [sentinel]
        with open(path, "rb") as fh:
            landed = fh.read()
        if landed != mutated:
            return False, f"the mutation did not land in {rel} — the file read back differs"
        rc = _compile(root, b, "mutated")
        if rc != 0:
            return False, (f"the mutant does not compile (rc={rc}) — under a test "
                           f"runner that compiles in a global setup, a non-compiling "
                           f"mutant reports zero failures and reads as a survivor. "
                           f"Choose a mutation that compiles")
        status, detail = _checks_verdict(root, p, cases, seed)
        if status == "pass":
            return False, (f"the checks PASSED with the reader mutated ({b['find']!r} -> "
                           f"{b['replace']!r} in {rel}) — the parity compares nothing")
        if status == "refused":
            return False, (f"the checks could not run with the reader mutated — {detail}; "
                           f"a run that died is not a parity that bit")
        print(f"    mutated  -> checks RED ({detail}) — the parity bites")
        return True, None
    finally:
        problems = _restore_pending()
        if problems:
            print(f"    restore  -> FAILED", file=sys.stderr)
        else:
            print("    restore  -> verified byte-for-byte")
            _compile(root, b, "restored")


def verify(root, cases=None, seed=None):
    pairs = load_manifest(root)
    if pairs is None:
        print(f"pair-guard: no {MANIFEST} — nothing to verify")
        return 0
    if os.path.exists(os.path.join(root, SENTINEL)):
        print(f"pair-guard: {SENTINEL} is present — an earlier --verify was "
              f"interrupted with a mutant possibly on disk. Inspect and remove it "
              f"first.", file=sys.stderr)
        return 1
    problems, proven = [], 0
    checked = [p for p in pairs if p.get("parity") or p.get("differential") is not None]
    if not checked:
        print("pair-guard: no pair declares a parity or differential — nothing to verify")
        return 0
    print(f"pair-guard: verifying {len(checked)} pair(s) by mutating each reader:\n")
    for p in checked:
        print(f"  {p['id']}")
        bites = p.get("bite") or []
        if not bites:
            problems.append(f"{p['id']}: parity declared, no bite — nothing has shown "
                            f"it can fail. Declare a bite (file, find, replace) on the "
                            f"reader side")
            print("    no bite declared")
            continue
        for b in bites:
            ok, why = verify_bite(root, p, b, cases, seed)
            if ok:
                proven += 1
            else:
                problems.append(f"{p['id']} ({b['file']}): {why}")
    if problems:
        print("\npair-guard: VERIFY FAILED\n", file=sys.stderr)
        for pr in problems:
            print(f"  - {pr}", file=sys.stderr)
        print("\n  A parity that stays green under a deliberate divergence is not a "
              "parity.\n", file=sys.stderr)
        return 1
    print(f"\npair-guard: {proven} bite(s) proven — every parity goes RED when its "
          f"reader is mutated")
    return 0


# --------------------------------------------------------------------- scan
def _corpus(root):
    """Tracked plus untracked code files, tests and generated trees excluded."""
    def git(*a):
        r = subprocess.run(["git", "-C", root, *a], capture_output=True, text=True)
        return [x for x in r.stdout.splitlines() if x] if r.returncode == 0 else []
    files = git("ls-files") + git("ls-files", "--others", "--exclude-standard")
    if not files:
        for dp, dns, fns in os.walk(root):
            dns[:] = [d for d in dns if d not in SCAN_SKIP_SEGMENTS]
            for fn in fns:
                files.append(os.path.relpath(os.path.join(dp, fn), root).replace(os.sep, "/"))
    out = []
    for f in sorted(set(files)):
        if os.path.splitext(f)[1] not in SCAN_EXT:
            continue
        segs = f.split("/")
        if any(s in SCAN_SKIP_SEGMENTS for s in segs[:-1]):
            continue
        if SCAN_TEST_FILE.search(segs[-1]):
            continue
        out.append(f)
    return out


SCAN_COMMENT = re.compile(r"/\*.*?\*/|//[^\n]*|^\s*#[^\n]*", re.S | re.M)
SCAN_DOCSTRING = re.compile(r"\"\"\".*?\"\"\"|'''.*?'''", re.S)


def _strip_code_comments(text):
    return SCAN_COMMENT.sub(" ", text)


def _comments_and_docstrings(text):
    return "\n".join(m.group(0) for m in SCAN_COMMENT.finditer(text)) + "\n" + \
        "\n".join(m.group(0) for m in SCAN_DOCSTRING.finditer(text))


def _thrown(text):
    code = _strip_code_comments(text)
    out = set()
    for m in SCAN_THROW.finditer(code):
        msg = " ".join(m.group(2).split())
        if len(msg) >= 12 and re.search(r"[A-Za-z]", msg):
            out.add(msg)
    return out


def _imports_of(text):
    """Basenames (no extension) of modules a file imports, best effort."""
    names = set()
    for m in re.finditer(r"(?:from|import|require\(|use)\s*[\"'(]?([\w./@:-]+)", text):
        names.add(os.path.splitext(os.path.basename(m.group(1).replace("::", "/")))[0].lower())
    return names


def _slug(path):
    return re.sub(r"[^a-z0-9]+", "-", os.path.splitext(os.path.basename(path))[0].lower()).strip("-")


def _covered(pairs, a, b):
    for p in pairs or []:
        sa, sb = _side_of(p, a), _side_of(p, b)
        if sa and sb:
            return True
    return False


def _direction(a, b, imports):
    """(source, mirror, authority) for a candidate pair of files."""
    ba, bb = _slug(a), _slug(b)
    a_imports_b = bb in imports.get(a, set())
    b_imports_a = ba in imports.get(b, set())
    if a_imports_b and not b_imports_a:
        return b, a, "source"
    if b_imports_a and not a_imports_b:
        return a, b, "source"
    if SCAN_SOURCEISH.search(a) and not SCAN_SOURCEISH.search(b):
        return a, b, "?"
    if SCAN_SOURCEISH.search(b) and not SCAN_SOURCEISH.search(a):
        return b, a, "?"
    return a, b, "?"


def scan(root, overlap):
    pairs = load_manifest(root)
    files = _corpus(root)
    texts, thrown, imports = {}, {}, {}
    for f in files:
        try:
            with open(os.path.join(root, f), encoding="utf-8", errors="replace") as fh:
                t = fh.read()
        except OSError:
            continue
        texts[f] = t
        thrown[f] = _thrown(t)
        imports[f] = _imports_of(t)

    cands = {}  # (a, b) sorted -> list of why

    def add(a, b, why):
        if a == b:
            return
        key = tuple(sorted((a, b)))
        cands.setdefault(key, []).append(why)

    # H1: the same non-trivial message thrown from two modules.
    by_msg = {}
    for f, msgs in thrown.items():
        for msg in msgs:
            if len(msg) >= SCAN_MIN_SHARED_LEN and len(msg.split()) >= SCAN_MIN_SHARED_WORDS:
                by_msg.setdefault(msg, set()).add(f)
    for msg, fs in by_msg.items():
        fs = sorted(fs)
        for i in range(len(fs)):
            for j in range(i + 1, len(fs)):
                add(fs[i], fs[j], f"shared-throw: {msg[:60]!r} is raised in both")
    # H2: overlapping thrown-message sets.
    fl = [f for f in files if thrown.get(f)]
    for i in range(len(fl)):
        for j in range(i + 1, len(fl)):
            a, b = fl[i], fl[j]
            shared = thrown[a] & thrown[b]
            union = thrown[a] | thrown[b]
            if len(shared) >= SCAN_OVERLAP_MIN_SHARED and len(shared) / len(union) >= overlap:
                add(a, b, f"throw-overlap: {len(shared)}/{len(union)} messages shared")
    # H3: a claim of mirroring or delegation in a comment or docstring.
    for f, t in texts.items():
        prose = _comments_and_docstrings(t)
        for m in SCAN_CLAIM.finditer(prose):
            window = prose[m.start():m.end() + 80]
            target = None
            for g in files:
                if g != f and (os.path.basename(g) in window or _slug(g) in window.lower()):
                    target = g
                    break
            if target is None:
                for g in files:
                    if g != f and _slug(g) in imports.get(f, set()):
                        target = g
                        break
            claim = " ".join(window[:70].split())
            if target:
                add(f, target, f"claims: {claim!r}")
            else:
                key = (f, "<fill in: the module this claims to mirror>")
                cands.setdefault(key, []).append(f"claims: {claim!r}")

    entries, covered = [], 0
    for (a, b), whys in sorted(cands.items()):
        if _covered(pairs, a, b):
            covered += 1
            continue
        if b.startswith("<fill in"):
            src, mir, auth = b, a, "?"
        else:
            src, mir, auth = _direction(a, b, imports)
        entries.append({
            "id": f"scan-{_slug(mir)}-{_slug(src)}"[:60],
            "source": src, "mirror": mir, "authority": auth,
            "why": "; ".join(dict.fromkeys(whys)),
        })
    print(f"pair-guard: {len(entries)} undeclared mirror candidate(s) over "
          f"{len(files)} file(s)" + (f"; {covered} covered by declared pairs" if covered else "")
          + ". Suggestions, never findings: write the parity, differential and "
          f"bite before adding one to {MANIFEST}.", file=sys.stderr)
    print(json.dumps(entries, indent=2))
    return 0


def listing(root):
    pairs = load_manifest(root)
    if pairs is None:
        print(f"pair-guard: no {MANIFEST}")
        return 0
    for p in pairs:
        tiers = ["co-change"]
        if p.get("parity"):
            tiers.append("parity")
        if p.get("differential") is not None:
            tiers.append("differential")
        print(f"  {p['id']:<24} {' + '.join(tiers)}")
        print(f"      source: {p['source']}" + (" (authority)" if p.get("authority") == "source" else ""))
        print(f"      mirror: {', '.join(as_list(p['mirror']))}"
              + (" (authority)" if p.get("authority") == "mirror" else ""))
        if len(tiers) == 1:
            # Named, because a co-change rule proves only that somebody
            # remembered to touch both files, never that they agree.
            print("      no parity command — nothing evaluates the two together")
        elif not p.get("bite"):
            print("      no bite — nothing has shown the parity can fail; declare "
                  "one and run --verify")
        else:
            print(f"      bite: {len(p['bite'])} declared ({', '.join(b['file'] for b in p['bite'])})")
    return 0


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=".")
    ap.add_argument("--against", default="HEAD",
                    help="ref to diff against; HEAD means the working tree")
    ap.add_argument("--no-parity", action="store_true",
                    help="co-change only, for a fast pre-commit pass")
    ap.add_argument("--cases", type=int, help="differential: override the case count")
    ap.add_argument("--seed", type=int, help="differential: override the seed")
    ap.add_argument("--overlap", type=float, default=SCAN_OVERLAP_JACCARD,
                    help="scan: Jaccard floor for throw-set overlap")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true")
    g.add_argument("--report", action="store_true")
    g.add_argument("--list", action="store_true")
    g.add_argument("--verify", action="store_true")
    g.add_argument("--scan", action="store_true")
    a = ap.parse_args()
    if a.list:
        return listing(a.root)
    if a.verify:
        return verify(a.root, a.cases, a.seed)
    if a.scan:
        return scan(a.root, a.overlap)
    return check(a.root, a.against, not a.no_parity, a.report, a.cases, a.seed)


if __name__ == "__main__":
    sys.exit(main())
