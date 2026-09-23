#!/usr/bin/env python3
"""Statement ratchet: what is being proved may not quietly get weaker.

`proof-guard.py` polices proof *bodies* — the `todo`, `assume`, `sorry` and
`{:axiom}` that discharge an obligation without proving it. It has nothing
to say about the obligation itself. An agent blocked from adding an
`assume` has an easier move available: weaken the theorem. Drop a conjunct
from an `ensures`. Widen a `requires` until the hard case is out of scope.
Delete the property test that covered the attack. Rename it so nothing
points at it any more.

Every one of those keeps the hatch counts flat, keeps the checker exiting
0, and keeps the suite green. The `proof-auditor` agent's own checklist
calls this the most common way verified code regresses, and until now it
was caught only if a model happened to notice.

So the statements are recorded, hashed, and compared:

  spec-guard.py --scan       list every obligation this repo declares
  spec-guard.py --baseline   record them into .fluxpoint-proof-baseline.json
  spec-guard.py --check      fail when a recorded obligation changed or vanished

Adding obligations is always free — the ratchet only turns one way, exactly
like proof-guard's counts. A *changed* or *removed* obligation needs one of
two things: a Decisions row in the work file naming the obligation id, or a
re-recorded baseline. Both leave the weakening in a committed diff with
someone's name on it, which is the whole point; neither pretends to make
weakening impossible. A ratchet makes it loud, not unreachable.

Coverage, stated rather than assumed:

  aiken     test and property signatures: name, fuzzer types, `fail` polarity
  dafny     requires / ensures / invariant clauses per declaration
  lean      theorem and lemma statements (binders and the proposition)
  coq       Theorem / Lemma / Corollary / Proposition / Fact / Remark statements
  isabelle  lemma / theorem / corollary / proposition statements
  tla       THEOREM statements, and every INVARIANT / PROPERTY a TLC .cfg
            names together with the operator definition it checks
  kani      every Rust fn carrying a kani:: attribute — proof harnesses and
            requires / ensures contracts — with the attributes and signature

Anything else that looks like proof work (Agda, F*, Alloy) is named as NOT
COVERED by `--scan` and `--check`, because a guard that silently covers
nothing is worse than one that is absent.

Three more checks ride on the same scan:

  --axioms   the prover's own assumption audit over baselined headline
             theorems: `#print axioms` (Lean), `Print Assumptions` (Coq),
             `dafny audit`. A NEW axiom in a headline's dependency set is
             red even when every hatch count is flat, which closes
             assumption laundering through a helper lemma. Where the
             toolchain is absent the output says NOT RUN, never clean.
  DoD tails  a checked `- [x]` line in the work file's Definition of Done
             with a `— proof: <obligation id>` tail is a claim; the id must
             exist and be unchanged, or the box is red.
  taxonomy   `.fluxpoint-attacks.json` (from templates/attack-taxonomy.json)
             names the attack classes a repo must specify, one taxonomy
             per language: the eUTxO classes a validator answers for, the
             builder classes the off-chain TypeScript answers for. A class
             with neither a test of that name nor a waiver with a reason is
             red — route 0 of the escape routes, never specifying the
             property, gets a gate too. Each taxonomy gates only a repo
             that carries its language, so a validator-only repo is
             silent on the builder classes and the other way round. A
             taxonomy in a language this guard has no rule for is NOT a
             malformed manifest: the manifest is the repo's declaration
             and this script is what lags it, so those classes are
             reported NOT COVERED and counted UNCHECKED, never as
             specified and never as a failure. The known languages are
             printed beside the unknown one, and a near miss is named,
             so a transposition cannot quietly un-gate a language.
             The other direction is checked too: a language this guard
             knows, tracked here, with no taxonomy in the manifest is
             named NO TAXONOMY with the classes the shipped template
             carries for it. That is a note, because reddening a repo for
             a manifest it has not finished writing makes a gate people
             delete; `"requireAllLanguages": true` makes it a failure.
             `"languages": [...]` names the halves a repo gates on
             purpose, and a tracked language it leaves out is printed as
             excluded by declaration instead of passed over.

What a statement hash cannot see: a property proved about an unreachable
state, a generator that cannot produce the interesting case, a test whose
body was gutted while its signature held. The first two are the
`proof-auditor` agent's job. The third is `mutation-guard`'s.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys

BASELINE = ".fluxpoint-proof-baseline.json"
ATTACKS = ".fluxpoint-attacks.json"
AXIOM_SCRATCH = os.path.join(".claude", "fluxpoint", "axioms")
MIN_REASON = 20

# Tools whose obligations this script can actually read, by file suffix.
COVERED = {
    ".ak": "aiken", ".dfy": "dafny", ".lean": "lean", ".v": "coq",
    ".thy": "isabelle", ".tla": "tla", ".cfg": "tla", ".rs": "kani",
}
# Proof languages this script recognises and does NOT parse. Reported by
# name rather than passed over.
UNCOVERED = {".agda": "Agda", ".fst": "F*", ".als": "Alloy"}

# `test name(params) fail {` — the signature is the obligation. The fuzzer
# types in the params are part of it: narrowing a generator narrows what was
# actually checked, and `fail` inverts the whole claim.
#
# Only the head is matched here; the parameter list is read by matching
# parens, because a fuzzer carries its own — `n: Int via bounded_int(1, 99)`
# — and a `[^)]*` class stops at the first one, which makes exactly the
# property tests this ratchet exists to protect invisible to it.
AIKEN_TEST_HEAD = re.compile(
    r"(?:^|\n)[ \t]*test[ \t]+([A-Za-z_][A-Za-z0-9_]*)[ \t]*\(")
# A Dafny declaration that can carry a specification. Attributes sit between
# the keyword and the name (`lemma {:axiom} Helper`, `method {:verify false}
# Skipped`); a pattern that skipped those folded their clauses into the
# previous declaration, which a real file showed.
DAFNY_DECL = re.compile(
    r"^\s*(?:ghost\s+|static\s+|twostate\s+|opaque\s+)*"
    r"(?:method|function|lemma|predicate|constructor|iterator|function method|"
    r"predicate method|greatest lemma|least lemma|greatest predicate|least predicate)"
    r"(?:\s*\{:[^}]*\})*\s+([A-Za-z_][A-Za-z0-9_']*)")
# The clauses that ARE the specification. `invariant` is included because a
# loop invariant that loses a conjunct weakens the proof exactly as an
# `ensures` does.
DAFNY_CLAUSE = re.compile(r"^\s*(requires|ensures|invariant)\b(.*)$")

# Lean 4: `theorem name binders : prop := ...`. The statement runs from the
# name to the `:=` at bracket depth zero, or to a `| pat => ...` alternative
# line, or to the next top-level command at column 0.
LEAN_DECL = re.compile(
    r"^[ \t]*(?:@\[[^\]]*\][ \t]*)*(?:(?:private|protected|noncomputable|nonrec|partial)"
    r"[ \t]+)*(theorem|lemma)[ \t]+([^\s:({\[⟨]+)", re.M)
LEAN_TOP = re.compile(
    r"^(?:theorem|lemma|def|example|instance|structure|inductive|class|namespace|end|"
    r"section|open|variable|universe|axiom|abbrev|noncomputable|private|protected|"
    r"@\[|#|set_option|import|deriving|macro|syntax|elab|notation|infix|prefix|"
    r"postfix|attribute|mutual|opaque|termination_by|decreasing_by)\b")
LEAN_OPEN, LEAN_CLOSE = "([{⟨", ")]}⟩"

# Coq / Rocq: a named statement command, terminated by the sentence `.`
# (a period followed by whitespace, so `Nat.add` and `1.5` are not ends).
COQ_DECL = re.compile(
    r"^[ \t]*(?:#\[[^\]]*\][ \t]*)*(?:(?:Local|Global|Program)[ \t]+)?"
    r"(Theorem|Lemma|Corollary|Proposition|Fact|Remark|Property|Example)"
    r"[ \t]+([A-Za-z_][\w']*)", re.M)
COQ_END = re.compile(r"\.(?=\s|$)")

# Isabelle: `lemma name [attrs]: "stmt"`, with `assumes … shows …` forms; the
# statement ends where the proof begins.
ISA_DECL = re.compile(
    r"^[ \t]*(lemma|theorem|corollary|proposition|schematic_goal)[ \t]+"
    r"(?:\(in[ \t]+[\w.']+\)[ \t]+)?([A-Za-z_][\w']*)[ \t]*(?:\[[^\]]*\][ \t]*)?:", re.M)
ISA_PROOF = re.compile(r"(?:by|proof|apply|using|unfolding|oops|sorry|done)(?![\w'])")
ISA_TOP = re.compile(
    r"^(?:lemma|theorem|corollary|proposition|schematic_goal|definition|fun|function|"
    r"primrec|datatype|type_synonym|abbreviation|locale|context|end|section|subsection|"
    r"subsubsection|paragraph|text|declare|instantiation|instance|interpretation|"
    r"inductive|inductive_set|record|value|export_code|hide_const|hide_fact|notation|"
    r"lemmas|named_theorems|method|ML|setup|typedecl|consts|axiomatization|theory|"
    r"imports|begin|term|typ|thm|find_theorems|nitpick|quickcheck|sledgehammer)\b")

# TLA+: `THEOREM Name == stmt`, ending at PROOF/BY/OBVIOUS/OMITTED, a proof
# step, or the next column-0 definition. Column-0 `Name == body` definitions
# are collected so a TLC .cfg INVARIANT can be tied to what it checks.
TLA_THEOREM = re.compile(
    r"^(THEOREM|LEMMA|PROPOSITION|COROLLARY)[ \t]+([A-Za-z_]\w*)[ \t]*==", re.M)
TLA_DEF = re.compile(r"^([A-Za-z_]\w*)(?:\([^)]*\))?[ \t]*==", re.M)
TLA_PROOF = re.compile(r"(?:PROOF|BY|OBVIOUS|OMITTED)\b|<\d+>")
TLA_TOP = re.compile(
    r"^(?:[A-Za-z_]\w*(?:\([^)]*\))?[ \t]*==|----|====|THEOREM\b|LEMMA\b|PROPOSITION\b|"
    r"COROLLARY\b|ASSUME\b|ASSUMPTION\b|AXIOM\b|VARIABLES?\b|CONSTANTS?\b|INSTANCE\b|"
    r"EXTENDS\b|LOCAL\b|RECURSIVE\b)")
CFG_KEY = re.compile(
    r"^[ \t]*(SPECIFICATION|INIT|NEXT|INVARIANTS?|PROPERT(?:Y|IES)|CONSTANTS?|"
    r"CONSTRAINTS?|ACTION[-_]CONSTRAINTS?|SYMMETRY|VIEW|CHECK_DEADLOCK|ALIAS|"
    r"POSTCONDITION)\b[ \t]*(.*)$")
CFG_MARKS = ("SPECIFICATION", "INIT", "NEXT", "INVARIANT", "PROPERT")

# Rust under Kani: any fn carrying a kani:: attribute is an obligation — a
# `#[kani::proof]` harness, or a `#[kani::requires]` / `#[kani::ensures]`
# contract on an ordinary function.
RUST_FN = re.compile(
    r"\s*(?:pub(?:\([^)]*\))?\s+)?(?:(?:const|async|unsafe|extern\s+\"[^\"]*\")\s+)*"
    r"fn\s+([A-Za-z_]\w*)")

# A checked Definition-of-Done line whose proof tail names an obligation.
DOD_LINE = re.compile(
    r"^\s*-\s*\[(?P<box>[ xX])\]\s*(?P<claim>.*?)\s*(?:—|--|-)\s*proof:\s*(?P<tail>.+?)\s*$")
OBLIGATION_ID = re.compile(r"\b(?:aiken|dafny|lean|coq|isabelle|tla|kani):[^\s,;]+")

# A finding row from `dafny audit --report-format txt` (Dafny 4.9.1):
#   src/vault.dfy(10,17):Helper: Declaration has explicit `{:axiom}` attribute. …
# The declaration name follows the position with no space; the ordinary
# `file(l,c): Warning: …` lines the same run prints have one, and are not
# findings.
DAFNY_AUDIT = re.compile(
    r"^(?P<file>[^\n(]+?\.dfy)\((?P<line>\d+),(?P<col>\d+)\):(?P<name>[^\s:][^:\n]*):(?P<msg>.+)$")
# A Lean name may itself end in a prime (`add_zero'`), so the quoted name is
# matched lazily up to the closing quote that precedes the verb.
LEAN_AXIOMS = re.compile(r"'(.+?)' depends on axioms: \[([^\]]*)\]")
LEAN_NONE = re.compile(r"'(.+?)' does not depend on any axioms")


def tracked_files(root):
    try:
        out = subprocess.run(["git", "-C", root, "ls-files"],
                             capture_output=True, text=True, check=True).stdout
    except Exception:  # noqa: BLE001
        return []
    return [f for f in out.splitlines() if f]


def norm(s):
    """Whitespace-insensitive form. Reformatting is not weakening."""
    return " ".join(str(s).split())


def sha(text):
    import hashlib
    return hashlib.sha256(norm(text).encode("utf-8", "replace")).hexdigest()[:16]


def strip_line_comments(text, marker):
    out = []
    for line in text.split("\n"):
        i = line.find(marker)
        out.append(line[:i] if i >= 0 else line)
    return "\n".join(out)


def strip_block(text, open_, close, nested=True):
    """Blank block comments in place; newlines survive so lines keep their numbers."""
    out, i, depth, n = [], 0, 0, len(text)
    while i < n:
        if text.startswith(open_, i) and (nested or depth == 0):
            depth += 1
            i += len(open_)
            continue
        if depth and text.startswith(close, i):
            depth -= 1
            i += len(close)
            continue
        if depth:
            if text[i] == "\n":
                out.append("\n")
        else:
            out.append(text[i])
        i += 1
    return "".join(out)


def _matching(text, i, open_="(", close=")"):
    """Index of the bracket closing the one at i, or -1."""
    depth = 0
    for j in range(i, len(text)):
        if text[j] == open_:
            depth += 1
        elif text[j] == close:
            depth -= 1
            if depth == 0:
                return j
    return -1


def _at_line_start(text, pos):
    """True when pos is the first character of a line."""
    return pos == 0 or text[pos - 1] == "\n"


def obligation(tool, rel, name, statement):
    return {"id": f"{tool}:{rel}:{name}", "tool": tool, "file": rel,
            "name": name, "statement": norm(statement)}


# ------------------------------------------------------------------ scanners

def scan_aiken(rel, text, _ctx):
    """One obligation per test: its name, its generators, its polarity."""
    text = strip_line_comments(text, "//")
    out = []
    for m in AIKEN_TEST_HEAD.finditer(text):
        name = m.group(1)
        close = _matching(text, m.end() - 1)
        if close < 0:
            continue
        rest = text[close + 1:]
        brace = rest.find("{")
        if brace < 0:
            continue
        params = norm(text[m.end():close])
        fail = bool(re.match(r"\s*fail\b", rest[:brace]))
        out.append(obligation("aiken", rel, name, f"({params}){' fail' if fail else ''}"))
    return out


def scan_dafny(rel, text, _ctx):
    """One obligation per declaration that carries a specification.

    Clauses are sorted, so reordering them is not a change; a declaration
    that had clauses and now has none has lost its obligation entirely,
    which is the case this exists to catch.
    """
    out, current, clauses = [], None, []

    def flush():
        if current and clauses:
            out.append(obligation("dafny", rel, current, " ; ".join(sorted(clauses))))

    for raw in strip_line_comments(text, "//").split("\n"):
        d = DAFNY_DECL.match(raw)
        if d:
            flush()
            current, clauses = d.group(1), []
            continue
        c = DAFNY_CLAUSE.match(raw)
        if c and current:
            clauses.append(norm(f"{c.group(1)} {c.group(2)}"))
    flush()
    return out


def _lean_statement(text, start):
    depth, i, n = 0, start, len(text)
    while i < n:
        c = text[i]
        if c in LEAN_OPEN:
            depth += 1
        elif c in LEAN_CLOSE:
            depth = max(0, depth - 1)
        elif depth == 0:
            if text.startswith(":=", i):
                return text[start:i]
            if c == "\n":
                rest = text[i + 1:]
                if rest.lstrip(" \t").startswith("|"):
                    return text[start:i]
                if rest[:1] not in ("", " ", "\t", "\n") and LEAN_TOP.match(rest):
                    return text[start:i]
        i += 1
    return text[start:]


def scan_lean(rel, text, _ctx):
    text = strip_line_comments(strip_block(text, "/-", "-/"), "--")
    return [obligation("lean", rel, m.group(2), _lean_statement(text, m.end()))
            for m in LEAN_DECL.finditer(text)]


def scan_coq(rel, text, _ctx):
    text = strip_block(text, "(*", "*)")
    out = []
    for m in COQ_DECL.finditer(text):
        e = COQ_END.search(text, m.end())
        out.append(obligation("coq", rel, m.group(2),
                              text[m.end():e.start() if e else len(text)]))
    return out


def _isa_statement(text, start):
    i, n, quote, cart = start, len(text), False, 0
    while i < n:
        c = text[i]
        if quote:
            if c == '"':
                quote = False
        elif c == "‹" or text.startswith("\\<open>", i):
            cart += 1
        elif c == "›" or text.startswith("\\<close>", i):
            cart = max(0, cart - 1)
        elif cart == 0:
            if c == '"':
                quote = True
            elif c == "\n":
                rest = text[i + 1:]
                if rest[:1] not in ("", " ", "\t", "\n") and ISA_TOP.match(rest):
                    return text[start:i]
            elif (i == start or not (text[i - 1].isalnum() or text[i - 1] in "_'")) \
                    and ISA_PROOF.match(text, i):
                return text[start:i]
        i += 1
    return text[start:]


def scan_isabelle(rel, text, _ctx):
    text = strip_block(text, "(*", "*)")
    return [obligation("isabelle", rel, m.group(2), _isa_statement(text, m.end()))
            for m in ISA_DECL.finditer(text)]


def _tla_clean(text):
    return strip_line_comments(strip_block(text, "(*", "*)"), "\\*")


def _tla_body(text, start):
    """Text from start to the end of the definition or theorem statement."""
    pos = start
    while True:
        nl = text.find("\n", pos)
        if nl < 0:
            return text[start:]
        rest = text[nl + 1:]
        if TLA_PROOF.match(rest.lstrip(" \t")):
            return text[start:nl]
        if rest[:1] not in ("", " ", "\t", "\n") and TLA_TOP.match(rest):
            return text[start:nl]
        pos = nl + 1


def tla_definitions(rel, text):
    """{name: body} for every column-0 `Name == body` in a module."""
    text = _tla_clean(text)
    return {m.group(1): norm(_tla_body(text, m.end())) for m in TLA_DEF.finditer(text)}


def scan_tla(rel, text, _ctx):
    text = _tla_clean(text)
    return [obligation("tla", rel, m.group(2), _tla_body(text, m.end()))
            for m in TLA_THEOREM.finditer(text)]


def looks_like_tlc(text):
    return any(CFG_KEY.match(l) and CFG_KEY.match(l).group(1).upper().startswith(CFG_MARKS)
               for l in text.split("\n"))


def scan_tlc(rel, text, ctx):
    """Every INVARIANT and PROPERTY a TLC config names, bound to its definition.

    Deleting the line from the .cfg removes the obligation; editing the
    operator it names changes it. Both are the moves a green model check
    hides, so both are recorded.
    """
    text = _tla_clean(text)
    kind, names = None, []
    for line in text.split("\n"):
        m = CFG_KEY.match(line)
        if m:
            key = m.group(1).upper()
            kind = ("INVARIANT" if key.startswith("INVARIANT")
                    else "PROPERTY" if key.startswith("PROPERT") else None)
            tokens = m.group(2).split()
        else:
            tokens = line.split()
        if kind:
            names.extend((kind, t) for t in tokens)
    defs = ctx.get("tla_defs", {})
    here = os.path.dirname(rel)
    base = os.path.splitext(os.path.basename(rel))[0]
    out = []
    for kind, name in names:
        cands = defs.get(name, [])
        # The module beside the config, then anything else tracked.
        cands = sorted(cands, key=lambda c: (
            0 if c[0] == os.path.join(here, base + ".tla").replace(os.sep, "/") else
            1 if os.path.dirname(c[0]) == here else 2))
        stmt = (f"{kind} {name} == {cands[0][1]}" if cands
                else f"{kind} {name} (definition not found in a tracked .tla module)")
        out.append(obligation("tla", rel, name, stmt))
    return out


def scan_kani(rel, text, _ctx):
    if "kani::" not in text:
        return []
    text = strip_line_comments(strip_block(text, "/*", "*/"), "//")
    out, pos = [], 0
    while True:
        j = text.find("#[", pos)
        if j < 0:
            break
        attrs, k = [], j
        while True:
            m = re.match(r"\s*#\[", text[k:])
            if not m:
                break
            open_i = k + m.end() - 1
            close_i = _matching(text, open_i, "[", "]")
            if close_i < 0:
                break
            attrs.append(text[open_i + 1:close_i])
            k = close_i + 1
        pos = max(k, j + 2)
        fm = RUST_FN.match(text, k)
        if not fm or not any("kani::" in a for a in attrs):
            continue
        name = fm.group(1)
        ends = [x for x in (text.find("{", fm.end()), text.find(";", fm.end())) if x >= 0]
        sig = text[fm.end():min(ends)] if ends else ""
        kani_attrs = sorted(norm(a) for a in attrs if "kani::" in a)
        stmt = "; ".join(f"#[{a}]" for a in kani_attrs) + f" | fn {name}{norm(sig)}"
        out.append(obligation("kani", rel, name, stmt))
    return out


SCANNERS = {
    "aiken": scan_aiken, "dafny": scan_dafny, "lean": scan_lean, "coq": scan_coq,
    "isabelle": scan_isabelle, "kani": scan_kani,
}


def scan(root):
    """Return (obligations_by_id, uncovered_tools_present)."""
    obligations, uncovered, texts = {}, set(), {}
    for rel in tracked_files(root):
        suffix = os.path.splitext(rel)[1]
        if suffix in UNCOVERED:
            uncovered.add(UNCOVERED[suffix])
            continue
        if suffix not in COVERED:
            continue
        try:
            with open(os.path.join(root, rel), encoding="utf-8", errors="replace") as fh:
                texts[rel] = fh.read()
        except OSError:
            continue
    # TLC configs point at operators defined in modules, so the modules are
    # read first.
    ctx = {"tla_defs": {}}
    for rel, text in texts.items():
        if rel.endswith(".tla"):
            for name, body in tla_definitions(rel, text).items():
                ctx["tla_defs"].setdefault(name, []).append((rel, body))
    for rel, text in texts.items():
        suffix = os.path.splitext(rel)[1]
        if suffix == ".tla":
            found = scan_tla(rel, text, ctx)
        elif suffix == ".cfg":
            found = scan_tlc(rel, text, ctx) if looks_like_tlc(text) else []
        else:
            found = SCANNERS[COVERED[suffix]](rel, text, ctx)
        for o in found:
            o["statementSha"] = sha(o["statement"])
            obligations[o["id"]] = o
    return obligations, uncovered


# ------------------------------------------------------------------ baseline

def load_baseline(root):
    p = os.path.join(root, BASELINE)
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        raise SystemExit(f"spec-guard: {p} is not readable JSON: {e}")


def _save(root, doc):
    p = os.path.join(root, BASELINE)
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(doc, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return p


def write_baseline(root, obligations):
    """Record the spec section, preserving everything else in the file.

    proof-guard owns `counts` in this same file and rewrites it on its own
    `--baseline`; each script must leave the other's section alone or arming
    one ratchet would silently disarm the other. The `axioms` section is
    left alone here for the same reason.
    """
    doc = load_baseline(root) or {"version": 1}
    doc["spec"] = {
        "note": ("Obligation statements, hashed. spec-guard.py --check fails "
                 "when one changes or disappears without a Decisions row "
                 "naming its id. Adding obligations is always allowed."),
        "obligations": {
            oid: {"tool": o["tool"], "file": o["file"], "name": o["name"],
                  "statementSha": o["statementSha"]}
            for oid, o in sorted(obligations.items())
        },
    }
    return _save(root, doc)


def _work_file(root):
    for name in ("WORK.md", "LOOP.md"):
        p = os.path.join(root, name)
        if os.path.exists(p):
            try:
                with open(p, encoding="utf-8", errors="replace") as fh:
                    return fh.read()
            except OSError:
                return ""
    return ""


def _section(root, heading):
    m = re.search(rf"^##\s+{heading}\s*$(.*?)(?=^##\s|\Z)", _work_file(root),
                  re.S | re.M | re.I)
    return m.group(1) if m else ""


def work_text(root):
    """The Decisions section of the repo's work file, if there is one."""
    return _section(root, "Decisions")


# ------------------------------------------------------------------ DoD tails

def dod_findings(root, now, recorded):
    """A checked box that cites an obligation is a claim the scan must back."""
    findings, notes = [], []
    for line in _section(root, "Definition of Done").split("\n"):
        m = DOD_LINE.match(line)
        if not m:
            continue
        ids = OBLIGATION_ID.findall(m.group("tail"))
        if not ids or m.group("box") == " ":
            continue
        for oid in ids:
            if oid not in now:
                findings.append((oid, f"DoD CLAIM — the checked line '{m.group('claim')[:60]}' "
                                      f"cites an obligation that does not exist"))
            elif recorded is not None and oid not in recorded:
                notes.append(f"DoD line cites {oid}, which is not in the baseline yet "
                             f"(re-record to lock it in)")
    return findings, notes


# ------------------------------------------------------------------ taxonomy

# The languages a taxonomy may be written for. A manifest naming any other
# is reported by name: this script would have no rule for deciding whether a
# class in it is specified, and a taxonomy nothing can be decided about
# passes every class it holds.
TAXONOMY_LANGUAGES = ("aiken", "typescript")


def _near(lang):
    """A known language within a character or two of `lang`, or None.

    Uncovered is the right verdict for a language this guard has not learned,
    but it reads the same as a typo, and a typo silently un-gates every class
    under it. Naming the near miss makes a transposition obvious in the one
    line a reader sees.
    """
    for known in TAXONOMY_LANGUAGES:
        if lang == known:
            continue
        if sorted(lang.lower()) == sorted(known.lower()) or (
                abs(len(lang) - len(known)) <= 1
                and sum(1 for a, b in zip(lang.lower(), known.lower()) if a != b) <= 1):
            return known
    return None


def uncovered_note(tax):
    """The line an uncovered taxonomy prints, with what it cost and a hint."""
    lang, n = tax["language"], len(tax["classes"])
    hint = _near(lang)
    return (f"NOT COVERED: taxonomy {lang!r} ({n} class(es) unchecked) — this guard "
            f"decides 'specified' for {', '.join(TAXONOMY_LANGUAGES)} only"
            + (f". Did you mean {hint!r}?" if hint else ""))
NAMED_BY = {
    "aiken": "no Aiken test named",
    "typescript": "no TypeScript test title or declaration names",
}
DORMANT_WHY = {
    "aiken": "no Aiken file is tracked",
    "typescript": "no TypeScript test file is tracked",
}
# How a tracked language and its classes are named when the manifest has no
# taxonomy for it. TypeScript is tracked by its test files, the same signal
# its dormancy reads, so the line says tests rather than source.
TRACKED_AS = {"aiken": "Aiken", "typescript": "TypeScript tests"}
CLASS_KIND = {"aiken": "eUTxO", "typescript": "off-chain builder"}
# The shipped taxonomies, beside this script in the plugin. A missing
# taxonomy is reported with what it leaves ungated, and the template is the
# one place that list is written down.
TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir,
                        "templates", "attack-taxonomy.json")
TEMPLATE_REL = "templates/attack-taxonomy.json"


def template_ids(lang):
    """The class ids the shipped template carries for `lang`, or None.

    None means the template could not be read. The report then says the
    classes are ungated without counting them, which is less useful and
    still true; a guessed count would not be.
    """
    try:
        with open(TEMPLATE, encoding="utf-8") as fh:
            doc = json.load(fh)
        for tax in doc["taxonomies"]:
            if tax["language"] == lang:
                return [c["id"] for c in tax["classes"]]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return None

# What the off-chain side runs as a test: the conventional suffixes, plus any
# .ts/.tsx under a test directory, which is where a repo that keeps its tests
# out of the source tree puts them.
TS_TEST_SUFFIX = (".test.ts", ".test.tsx", ".spec.ts", ".spec.tsx")
TS_SOURCE_SUFFIX = (".ts", ".tsx")
TS_TEST_DIR = re.compile(r"(?:^|/)(?:tests?|__tests__)/")

# A test title: `it("…")`, `test('…')`, `describe(`…`)`, through any chained
# modifier (`it.only`, `test.skip`, `it.each`). The class id has to appear
# inside the title, so a title that says more than the id still counts.
TS_TITLE = re.compile(
    r"\b(?:it|test|describe|suite|bench)\s*(?:\.\s*[A-Za-z_$][\w$]*\s*)*"
    r"\(\s*(['\"`])(.*?)\1", re.S)
# A declaration carrying the class id as its own name.
TS_DECL = re.compile(r"\b(?:function|const|let|var|class)\s+([A-Za-z_$][\w$]*)")
# fast-check in the file: the package imported, or a call on its conventional
# binding. Either one means the matched test can run over a generator.
TS_FASTCHECK = re.compile(r"\bfc\s*\.\s*[A-Za-z_$]|['\"]fast-check['\"]")


def _taxonomy_problem(tax, where):
    """What is wrong with one taxonomy object, or None."""
    if not isinstance(tax, dict):
        return f"{where} must be an object carrying a 'language' and a 'classes' list"
    # A language this guard has no rule for is NOT a malformed manifest. The
    # manifest is a repo's declaration of what it answers for, and this script
    # is what lags behind it; failing the build teaches people to delete
    # classes, which is the opposite of the taxonomy's purpose. It is reported
    # as uncovered instead, the way a tracked Agda file already is — loud every
    # run, never counted as covered. Only the SHAPE is a problem here.
    if not isinstance(tax.get("language"), str) or not tax["language"]:
        return f"{where} needs a 'language' string"
    classes = tax.get("classes")
    if not isinstance(classes, list) or not all(
            isinstance(c, dict) and isinstance(c.get("id"), str) and c["id"] for c in classes):
        return f"{where} needs a 'classes' list of objects with an 'id'"
    if not isinstance(tax.get("waived", {}), dict):
        return f"{where} 'waived' must be an object of id: reason"
    return None


def load_attacks(root):
    """(doc, problem), the doc normalised to the multi-taxonomy shape.

    Two shapes are read. The 1.38.0 form carries `language`, `classes` and
    `waived` at the top level and is one taxonomy, so a manifest already
    copied into a repo keeps its exact behaviour. The current form carries a
    `taxonomies` list, one entry per language. Anything else comes back as a
    named problem, because a manifest this script cannot read is a gate that
    passes every class in it.
    """
    p = os.path.join(root, ATTACKS)
    if not os.path.exists(p):
        return None, None
    try:
        with open(p, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        return None, f"{ATTACKS} is not readable JSON ({e})"
    if not isinstance(doc, dict):
        return None, f"{ATTACKS} must be a JSON object"
    raw = doc.get("taxonomies")
    if raw is None:
        if "classes" not in doc:
            return None, (f"{ATTACKS} needs a 'taxonomies' list, or the single-taxonomy "
                          f"'classes' list at the top level")
        # The shape that shipped in 1.38.0 named no language beside 'aiken'.
        legacy = dict(doc)
        legacy.setdefault("language", "aiken")
        raw = [legacy]
    elif not isinstance(raw, list) or not raw:
        return None, f"{ATTACKS} 'taxonomies' must be a non-empty list"
    out, seen = [], set()
    for i, tax in enumerate(raw):
        problem = _taxonomy_problem(tax, f"{ATTACKS} taxonomy {i + 1}")
        if problem:
            return None, problem
        if tax["language"] in seen:
            return None, f"{ATTACKS} names the {tax['language']} taxonomy twice"
        seen.add(tax["language"])
        out.append({"language": tax["language"], "classes": tax["classes"],
                    "waived": tax.get("waived", {})})
    # Both fields change what the gate is allowed to leave unchecked, so a
    # value this script cannot read is a problem with a name, like every
    # other shape error here. Treating `"yes"` as unset would drop a strict
    # repo back to notes without a word; treating a bare string as a list
    # would exclude whatever its letters happened to spell.
    strict = doc.get("requireAllLanguages", False)
    if not isinstance(strict, bool):
        return None, f"{ATTACKS} 'requireAllLanguages' must be true or false"
    listed = None
    if "languages" in doc:
        listed = doc["languages"]
        problem = _languages_problem(listed, seen)
        if problem:
            return None, problem
    return {"version": doc.get("version", 1), "taxonomies": out,
            "languages": listed, "requireAllLanguages": strict}, None


def _languages_problem(listed, declared):
    """What is wrong with a `languages` list, or None.

    A name must be one this guard gates, or the language of a taxonomy the
    manifest itself declares. The second is allowed for the reason an
    unlearned taxonomy is NOT COVERED rather than malformed: the manifest may
    run ahead of this script. A name that is neither refers to nothing, and
    its likely cause is a typo, which would leave the language it meant out
    of the list and so excluded by declaration. That is the silent un-gating
    the list exists to make visible, so the near miss is named.
    """
    where = f"{ATTACKS} 'languages'"
    if (not isinstance(listed, list) or not listed
            or not all(isinstance(x, str) and x for x in listed)):
        return (f"{where} must be a non-empty list of language names; to gate "
                f"nothing, remove the manifest instead")
    seen = set()
    for lang in listed:
        if lang in seen:
            return f"{where} names {lang!r} twice"
        seen.add(lang)
        if lang not in TAXONOMY_LANGUAGES and lang not in declared:
            hint = _near(lang)
            return (f"{where} names {lang!r}, which neither this guard "
                    f"({', '.join(TAXONOMY_LANGUAGES)}) nor a taxonomy in the manifest "
                    f"declares" + (f" (did you mean {hint!r}?)" if hint else ""))
    return None


def typescript_test_files(tracked):
    """The tracked files the off-chain side runs as tests."""
    return [rel for rel in tracked
            if rel.endswith(TS_TEST_SUFFIX)
            or (rel.endswith(TS_SOURCE_SUFFIX) and TS_TEST_DIR.search(rel))]


def typescript_specified(root, files, ids):
    """{class id: [(file, the file uses fast-check)]} over the ids named.

    A class id is named by a test title that contains it or by a declaration
    of exactly that name. Comments are blanked first, so an id sitting in a
    `//` line or a `/* */` block is not evidence that anything runs, which is
    the rule the Aiken side already applies to a commented-out test.
    """
    hits = {}
    for rel in files:
        try:
            with open(os.path.join(root, rel), encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        text = strip_line_comments(strip_block(text, "/*", "*/", nested=False), "//")
        fuzzed = bool(TS_FASTCHECK.search(text))
        names = {m.group(1) for m in TS_DECL.finditer(text)}
        # Titles are joined on a newline the ids cannot contain, so no id is
        # assembled out of the tail of one title and the head of the next.
        titles = "\n".join(m.group(2) for m in TS_TITLE.finditer(text))
        for cid in ids:
            if cid in names or cid in titles:
                hits.setdefault(cid, []).append((rel, fuzzed))
    return hits


def taxonomy_context(root, now, tracked=None):
    """Per language: whether the taxonomy gates anything in this repo, and
    what it reads to decide a class is specified.

    Dormancy is per language. A repo carrying only a validator has nothing
    for the builder classes to hold of, and a repo carrying only a builder
    has nothing for the eUTxO classes to hold of; reddening every class in a
    language the repo does not carry produces a gate people delete instead of
    satisfy. A repo carrying both is gated by both.
    """
    if tracked is None:
        tracked = tracked_files(root)
    aiken = {}
    for o in now.values():
        if o["tool"] == "aiken":
            aiken.setdefault(o["name"], []).append(o)
    ts_files = typescript_test_files(tracked)
    return {
        "aiken": {"dormant": not aiken and not any(r.endswith(".ak") for r in tracked),
                  "tests": aiken},
        "typescript": {"dormant": not ts_files, "files": ts_files},
    }


def specified_in(root, tax, ctx):
    """{class id: the evidence that names it} for one taxonomy.

    One function so `--scan` and `--check` cannot disagree about what counts
    as specified. A missing id is a class nothing names.
    """
    ids = [c["id"] for c in tax["classes"]]
    if tax["language"] == "aiken":
        tests = ctx["aiken"]["tests"]
        return {cid: tests[cid] for cid in ids if cid in tests}
    return typescript_specified(root, ctx["typescript"]["files"], ids)


def _ungated(lang):
    """(what a missing taxonomy leaves ungated, the first of them by id).

    Three ids are enough to recognise the list and short enough to read in
    one line; the rest are counted, and the template holds them all.
    """
    ids, kind = template_ids(lang), CLASS_KIND[lang]
    if not ids:
        return f"its known {kind} classes", ""
    shown = ", ".join(ids[:3]) + (f", +{len(ids) - 3} more" if len(ids) > 3 else "")
    return f"{len(ids)} known {kind} classes", f" ({shown})"


def language_gaps(doc, ctx):
    """[(language, missing, text)] for each known language the manifest leaves out.

    Every other taxonomy check runs the manifest against the repo. This runs
    the repo against the manifest, the direction that stayed silent: a repo
    of validators whose manifest carried only the typescript half left every
    eUTxO class ungated and read green. `missing` marks a tracked language
    with no taxonomy, which the caller reports as NO TAXONOMY: a note, or a
    failure under requireAllLanguages. Every other entry makes a declared
    choice visible and never fails the gate.

    "Tracked" is the dormancy `taxonomy_context` already decides, so a
    language needs a taxonomy here exactly when its taxonomy would gate
    something. The two directions cannot disagree about what a repo carries.
    """
    have = {t["language"] for t in doc["taxonomies"]}
    listed = doc.get("languages")
    out = []
    for lang in TAXONOMY_LANGUAGES:
        named = listed is None or lang in listed
        if lang in have:
            # The list never switches off a taxonomy the manifest writes out.
            # Honouring it would be a second, quieter way to drop every class
            # in a language, which is the move this check exists to expose.
            if not named:
                out.append((lang, False,
                            f"the {lang} taxonomy gates although 'languages' does not "
                            f"list {lang}; a list never switches off a taxonomy the "
                            f"manifest carries, so add {lang} to it or remove the taxonomy"))
            continue
        if ctx[lang]["dormant"]:
            # Nothing in this language is tracked, so nothing is ungated. A
            # list naming it still contradicts the manifest, which is worth a
            # line before the first file in that language lands.
            if listed is not None and lang in listed:
                out.append((lang, False,
                            f"'languages' lists {lang} but no {lang} taxonomy is declared; "
                            f"{DORMANT_WHY[lang]}, so nothing is ungated yet"))
            continue
        if not named:
            # Excluded on purpose, and said so in one line. A choice nobody
            # can see is indistinguishable from an accident.
            cost, _ = _ungated(lang)
            out.append((lang, False,
                        f"{lang} excluded by declaration: this repo tracks {TRACKED_AS[lang]}, "
                        f"and 'languages' names {', '.join(listed)} only, so {cost} "
                        f"are not gated"))
            continue
        # Declared gated by the list and gated by nothing is the same gap as
        # no list at all, with a contradiction on top, so it is reported the
        # same way and says which of its two claims is false.
        head = (f"'languages' lists {lang} and this repo tracks {TRACKED_AS[lang]}, but"
                if listed is not None else f"this repo tracks {TRACKED_AS[lang]} but")
        fix = (f"take {lang} out of 'languages'" if listed is not None
               else "name the halves this repo gates on purpose in 'languages'")
        cost, shown = _ungated(lang)
        out.append((lang, True,
                    f"{head} {ATTACKS} declares no {lang} taxonomy — {cost} are "
                    f"ungated{shown}. Copy them from {TEMPLATE_REL} and waive any "
                    f"that cannot apply, or {fix}."))
    return out


def attack_findings(root, now):
    doc, problem = load_attacks(root)
    if problem:
        return [("attacks", f"UNREADABLE — {problem}; every class counts as unspecified")], []
    if doc is None:
        return [], []
    ctx = taxonomy_context(root, now)
    findings, notes = class_findings(root, doc, ctx)
    for lang, missing, text in language_gaps(doc, ctx):
        if not missing:
            notes.append(f"{ATTACKS}: {text}")
        elif doc["requireAllLanguages"]:
            findings.append((f"attack:{lang}",
                             f"NO TAXONOMY, and requireAllLanguages is set: {text}"))
        else:
            notes.append(f"NO TAXONOMY: {text}")
    return findings, notes


def class_findings(root, doc, ctx):
    """(findings, notes) for every class in every taxonomy the manifest carries."""
    findings, notes = [], []
    taxonomies = []
    for tax in doc["taxonomies"]:
        if tax["language"] in TAXONOMY_LANGUAGES:
            taxonomies.append(tax)
        else:
            # Unchecked, and said so every run. Silence here would let a
            # reader take a green gate for coverage of classes nothing read.
            notes.append(uncovered_note(tax))
    if not taxonomies:
        return findings, notes
    if all(ctx[t["language"]]["dormant"] for t in taxonomies):
        why = "; ".join(dict.fromkeys(DORMANT_WHY[t["language"]] for t in taxonomies))
        notes.append(f"{ATTACKS} is present but gates nothing here: {why}")
        return findings, notes
    for tax in taxonomies:
        lang = tax["language"]
        if ctx[lang]["dormant"]:
            notes.append(f"{ATTACKS}: the {lang} taxonomy gates nothing here "
                         f"({DORMANT_WHY[lang]})")
            continue
        waived, evidence = tax["waived"], specified_in(root, tax, ctx)
        for cls in tax["classes"]:
            cid = cls["id"]
            oid = f"attack:{lang}:{cid}"
            # A waiver belongs to the taxonomy it is written in. The same id
            # excused on the on-chain side says nothing about the off-chain
            # one: they are different properties over different code.
            if cid in waived:
                reason = waived[cid] if isinstance(waived[cid], str) else ""
                if len(reason.strip()) < MIN_REASON:
                    findings.append((oid, f"WAIVED WITHOUT A REASON — a waiver needs "
                                          f"at least {MIN_REASON} characters saying why "
                                          f"this class cannot apply"))
                else:
                    notes.append(f"attack class {lang}:{cid} waived: {reason.strip()[:80]}")
                continue
            where = evidence.get(cid)
            if not where:
                prop = cls.get("property") or "(no property text in the manifest)"
                findings.append((oid, f"UNSPECIFIED — {NAMED_BY[lang]} `{cid}`. "
                                      f"The property to state: {prop}"))
                continue
            if lang == "aiken":
                if not any(" via " in t["statement"] for t in where):
                    notes.append(f"attack class {lang}:{cid} is a unit test, not a property "
                                 f"over aiken/fuzz (no `via` generator in its signature)")
            elif not any(fuzzed for _, fuzzed in where):
                notes.append(f"attack class {lang}:{cid} is specified by an example in "
                             f"{where[0][0]}; that file imports no fast-check and calls no "
                             f"`fc.`, so it covers the cases the file lists. State it over "
                             f"an fc generator to cover the range.")
    return findings, notes


def print_taxonomy(root, obligations, doc, problem):
    """The taxonomy half of `--scan`: every class, its language and its state."""
    if problem:
        print(f"  attack taxonomy: UNREADABLE — {problem}")
        return
    if not doc:
        return
    ctx = taxonomy_context(root, obligations)
    for tax in doc["taxonomies"]:
        lang = tax["language"]
        if lang not in TAXONOMY_LANGUAGES:
            print(f"  attack taxonomy {lang}: {uncovered_note(tax)}")
            for cls in tax["classes"]:
                print(f"  attack class {lang}:{cls['id']}: UNCHECKED")
            continue
        if ctx[lang]["dormant"]:
            print(f"  attack taxonomy {lang}: dormant ({DORMANT_WHY[lang]})")
            continue
        evidence = specified_in(root, tax, ctx)
        for cls in tax["classes"]:
            state = ("specified" if cls["id"] in evidence
                     else "waived" if cls["id"] in tax["waived"]
                     else "UNSPECIFIED")
            print(f"  attack class {lang}:{cls['id']}: {state}")
    # The same gaps `--check` reports, so a scan cannot list every class it
    # was given and stay quiet about a tracked language it was given none for.
    for lang, missing, text in language_gaps(doc, ctx):
        print(f"  attack taxonomy {lang}: " + (f"NO TAXONOMY: {text}" if missing else text))


# ------------------------------------------------------------------ axioms

def _probe(root, name, body):
    d = os.path.join(root, AXIOM_SCRATCH)
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, name)
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(body)
    return p


def _run(cmd, root):
    try:
        r = subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=900)
    except (OSError, subprocess.SubprocessError) as e:
        return None, str(e)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def audit_lean(root, o):
    if not shutil.which("lake"):
        return "not-run", None, "lake is not on PATH"
    module = o["file"][:-len(".lean")].replace("/", ".")
    probe = _probe(root, "Probe.lean", f"import {module}\n#print axioms {o['name']}\n")
    rc, out = _run(["lake", "env", "lean", probe], root)
    if rc is None:
        return "unreadable", None, f"lake env lean could not run: {out}"
    m = LEAN_AXIOMS.search(out)
    if m:
        return "ok", sorted(x.strip() for x in m.group(2).split(",") if x.strip()), ""
    if LEAN_NONE.search(out):
        return "ok", [], ""
    return "unreadable", None, f"lake env lean exited {rc} without an axiom listing: {norm(out)[:200]}"


def coq_project(root):
    """[(dir, logical)] from _CoqProject -R/-Q lines, and the flags to pass on."""
    p = os.path.join(root, "_CoqProject")
    if not os.path.exists(p):
        return None, []
    maps, flags = [], []
    with open(p, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            toks = line.split("#", 1)[0].split()
            i = 0
            while i < len(toks):
                if toks[i] in ("-R", "-Q") and i + 2 < len(toks):
                    logical = toks[i + 2].strip('"')
                    maps.append((toks[i + 1], logical))
                    flags += [toks[i], toks[i + 1], logical]
                    i += 3
                elif toks[i] == "-I" and i + 1 < len(toks):
                    flags += [toks[i], toks[i + 1]]
                    i += 2
                else:
                    i += 1
    return maps, flags


def audit_coq(root, o):
    if not shutil.which("coqc"):
        return "not-run", None, "coqc is not on PATH"
    maps, flags = coq_project(root)
    if maps is None:
        return "not-run", None, "no _CoqProject at the repo root to resolve the module path"
    module = None
    for d, logical in maps:
        dn = os.path.normpath(d).replace(os.sep, "/")
        prefix = "" if dn == "." else dn.rstrip("/") + "/"
        if o["file"].startswith(prefix):
            rest = o["file"][len(prefix):-2].replace("/", ".")
            module = f"{logical}.{rest}" if logical else rest
            break
    if module is None:
        return "not-run", None, f"no -R/-Q entry in _CoqProject covers {o['file']}"
    probe = _probe(root, "Probe.v", f"Require Import {module}.\nPrint Assumptions {o['name']}.\n")
    rc, out = _run(["coqc", "-q", *flags, probe], root)
    if rc is None:
        return "unreadable", None, f"coqc could not run: {out}"
    if "Closed under the global context" in out:
        return "ok", [], ""
    m = re.search(r"Axioms:\n(.*)", out, re.S)
    if rc == 0 and m:
        names = []
        for line in m.group(1).split("\n"):
            if not line.strip():
                break
            nm = re.match(r"^(\S+)\s*:", line)
            if nm:
                names.append(nm.group(1))
        return "ok", sorted(set(names)), ""
    return "unreadable", None, f"coqc exited {rc} without an assumption listing: {norm(out)[:200]}"


def audit_dafny(root, o):
    if not shutil.which("dafny"):
        return "not-run", None, "dafny is not on PATH"
    files = ([o["file"]] if o["file"] != "*"
             else [r for r in tracked_files(root) if r.endswith(".dfy")])
    if not files:
        return "not-run", None, "no tracked .dfy file to audit"
    # `txt` is the spelling Dafny 4.9.1 accepts; its help lists `text` as an
    # alias, and rejects it.
    rc, out = _run(["dafny", "audit", "--report-format", "txt", *files], root)
    if rc is None:
        return "unreadable", None, f"dafny audit could not run: {out}"
    if rc != 0:
        return "unreadable", None, f"dafny audit exited {rc}: {norm(out)[:200]}"
    rows = []
    for line in out.split("\n"):
        m = DAFNY_AUDIT.match(line.strip())
        if m:
            # Line and column are dropped: a moved declaration is not a new
            # assumption, a new declaration with an assumption is.
            rows.append(norm(f"{m.group('file')}:{m.group('name')}: {m.group('msg')}"))
    if not rows and "auditor completed" not in out:
        return "unreadable", None, f"dafny audit printed no findings and no completion line: {norm(out)[:200]}"
    if o["name"] != "*":
        rows = [r for r in rows if f":{o['name']}:" in r]
    return "ok", sorted(set(rows)), ""


AUDITS = {"lean": audit_lean, "coq": audit_coq, "dafny": audit_dafny}
NO_AUDIT = {
    "aiken": "Aiken has no axiom mechanism; its hatches are proof-guard's counts",
    "kani": "Kani has no assumption listing; kani::assume is a proof-guard count",
    "isabelle": "no batch assumption listing is wired for Isabelle yet",
    "tla": "TLAPS has no per-theorem assumption listing this script reads",
}


def resolve_headline(oid, now):
    """The obligation an axiom headline names, or a synthetic project-wide one."""
    if oid in now:
        return now[oid]
    if oid == "dafny:*":
        return {"id": oid, "tool": "dafny", "file": "*", "name": "*"}
    return None


def audit(root, oid, now):
    o = resolve_headline(oid, now)
    if o is None:
        return "unknown", None, "is not an obligation in the current scan"
    fn = AUDITS.get(o["tool"])
    if fn is None:
        return "unsupported", None, NO_AUDIT.get(o["tool"], "no audit for this tool")
    return fn(root, o)


def write_axioms(root, headline, recorded):
    doc = load_baseline(root) or {"version": 1}
    doc["axioms"] = {
        "note": ("Assumption sets the prover itself reported for each headline "
                 "obligation. spec-guard.py --check fails when a headline depends "
                 "on an axiom that is not recorded here; fewer is always allowed."),
        "headline": sorted(headline),
        "recorded": {k: sorted(v) for k, v in sorted(recorded.items())},
    }
    return _save(root, doc)


def axiom_findings(root, base, now):
    findings, notes = [], []
    section = (base or {}).get("axioms") or {}
    recorded = section.get("recorded") or {}
    for oid in section.get("headline") or []:
        status, axioms, detail = audit(root, oid, now)
        if status == "ok":
            was = set(recorded.get(oid) or [])
            new = sorted(set(axioms) - was)
            gone = sorted(was - set(axioms))
            if new:
                findings.append((oid, f"NEW AXIOM — the prover reports {', '.join(new)} in "
                                      f"this theorem's dependency set and the baseline does "
                                      f"not (recorded: {', '.join(sorted(was)) or 'none'})"))
            elif gone:
                notes.append(f"{oid} no longer depends on {', '.join(gone)} (always allowed; "
                             f"re-record to lock it in)")
            else:
                notes.append(f"{oid}: assumptions unchanged "
                             f"({', '.join(sorted(was)) or 'none'})")
        elif status == "not-run":
            notes.append(f"AXIOM AUDIT NOT RUN for {oid}: {detail} — this headline is "
                         f"unverified in this run, which is not the same as clean")
        elif status == "unsupported":
            notes.append(f"axiom audit unavailable for {oid}: {detail}")
        else:
            findings.append((oid, f"AXIOM AUDIT {status.upper()} — {detail}"))
    return findings, notes


# ------------------------------------------------------------------ check

def check(root, axioms_only=False):
    """Return (findings, notes). A finding is an unjustified weakening."""
    base = load_baseline(root)
    now, uncovered = scan(root)
    notes, findings = [], []
    if axioms_only:
        return axiom_findings(root, base, now)
    if uncovered:
        notes.append(
            f"NOT covered by this ratchet: {', '.join(sorted(uncovered))} "
            f"file(s) are tracked but their obligations are not parsed yet")
    recorded = (base or {}).get("spec", {}).get("obligations") if base else None
    if recorded is None:
        if now:
            notes.append(
                f"{len(now)} obligation(s) found but the spec ratchet is NOT "
                f"armed. Run: spec-guard.py --baseline")
    else:
        # Legacy waivers must be decision rows. A locked campaign changes its
        # reviewed packet and baseline instead of exempting obligations in prose.
        justified = "" if os.path.exists(os.path.join(root, ".fluxpoint-spec-lock.json")) else "\n".join(
            line for line in work_text(root).splitlines()
            if re.match(r"^\|\s*\d{4}-\d{2}-\d{2}\b", line) and line.count("|") >= 7)
        # A file rename moves every obligation in it. That is not a weakening,
        # so an id that vanished while an identical statement appeared under
        # the same name elsewhere is treated as the same obligation, relocated.
        moved = {}
        for oid, was in recorded.items():
            if oid in now:
                continue
            for nid, o in now.items():
                if (nid not in recorded and o["name"] == was.get("name")
                        and o["tool"] == was.get("tool")
                        and o["statementSha"] == was.get("statementSha")):
                    moved[oid] = nid
                    break
        for oid, was in sorted(recorded.items()):
            if oid in moved:
                notes.append(f"{oid} moved to {moved[oid]} (statement unchanged)")
                continue
            if oid in justified:
                notes.append(f"{oid} changed, and a Decisions row names it")
                continue
            if oid not in now:
                findings.append(
                    (oid, f"REMOVED — {was.get('tool')} obligation '{was.get('name')}' "
                          f"is gone from {was.get('file')}"))
            elif now[oid]["statementSha"] != was.get("statementSha"):
                findings.append(
                    (oid, f"CHANGED — the statement is not what was recorded\n"
                          f"        now: {now[oid]['statement'][:120]}"))
        added = [oid for oid in now if oid not in recorded and oid not in moved.values()]
        if added:
            notes.append(f"{len(added)} obligation(s) added since the baseline "
                         f"(always allowed; re-record to lock them in)")
    f, n = dod_findings(root, now, recorded)
    findings += f
    notes += n
    f, n = attack_findings(root, now)
    findings += f
    notes += n
    f, n = axiom_findings(root, base, now)
    findings += f
    notes += n
    return findings, notes


# ------------------------------------------------------------------ CLI

def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=".")
    ap.add_argument("--axioms", action="store_true",
                    help="the prover's assumption audit over headline theorems "
                         "(with --scan, --baseline or --check)")
    ap.add_argument("--headline", action="append", default=[], metavar="ID",
                    help="with --baseline --axioms: an obligation id to audit "
                         "(repeatable; dafny:* audits every tracked .dfy)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--scan", action="store_true")
    g.add_argument("--baseline", action="store_true")
    g.add_argument("--check", action="store_true")
    a = ap.parse_args()
    root = a.root

    if a.scan:
        obligations, uncovered = scan(root)
        if a.axioms:
            base = load_baseline(root) or {}
            heads = a.headline or ((base.get("axioms") or {}).get("headline") or [])
            if not heads:
                print("spec-guard: no headline theorems named; pass --headline <id>")
                return 2
            for oid in heads:
                status, axioms, detail = audit(root, oid, obligations)
                if status == "ok":
                    print(f"  {oid}: {', '.join(axioms) or 'no axioms'}")
                else:
                    print(f"  {oid}: {status.upper()} — {detail}")
            return 0
        # A repo with no obligations still has a taxonomy to report: the
        # off-chain classes are gated by test files this scanner does not
        # read as obligations.
        doc, problem = load_attacks(root)
        if not obligations and not uncovered and doc is None and problem is None:
            print("spec-guard: no obligations in a covered language — dormant")
            return 0
        print(f"spec-guard: {len(obligations)} obligation(s)")
        for oid, o in sorted(obligations.items()):
            print(f"  {oid}")
            print(f"      {o['statement'][:110]}")
        for tool in sorted(uncovered):
            print(f"  NOT COVERED: {tool} obligations are not parsed yet")
        print_taxonomy(root, obligations, doc, problem)
        return 0

    if a.baseline:
        obligations, uncovered = scan(root)
        if a.axioms:
            base = load_baseline(root) or {}
            prior = (base.get("axioms") or {})
            heads = a.headline or prior.get("headline") or []
            if not heads:
                print("spec-guard: name the headline theorems to audit: "
                      "--baseline --axioms --headline <obligation id>", file=sys.stderr)
                return 2
            recorded = {}
            for oid in heads:
                status, axioms, detail = audit(root, oid, obligations)
                if status != "ok":
                    print(f"spec-guard: cannot record an axiom set for {oid}: "
                          f"{status.upper()} — {detail}", file=sys.stderr)
                    return 1
                recorded[oid] = axioms
                print(f"spec-guard: {oid} depends on {', '.join(axioms) or 'no axioms'}")
            p = write_axioms(root, heads, recorded)
            print(f"spec-guard: recorded {len(recorded)} headline assumption set(s) into {p}")
            return 0
        p = write_baseline(root, obligations)
        print(f"spec-guard: recorded {len(obligations)} obligation(s) into {p}")
        for tool in sorted(uncovered):
            print(f"spec-guard: NOT COVERED — {tool} obligations are not parsed yet")
        return 0

    findings, notes = check(root, axioms_only=a.axioms)
    for n in notes:
        print(f"spec-guard: {n}")
    if not findings:
        print("spec-guard: green — no recorded obligation weakened"
              if not a.axioms else "spec-guard: green — no headline gained an axiom")
        return 0
    print("\nspec-guard: RED — a proof obligation got weaker\n", file=sys.stderr)
    for oid, detail in findings:
        print(f"  {oid}: {detail}", file=sys.stderr)
    print(
        "\n  A statement that changed or vanished is a weaker claim than the one\n"
        "  that was recorded, and every checker still exits 0 on it. Restore it,\n"
        "  or justify the change: add a Decisions row naming the obligation id,\n"
        "  or re-record with --baseline so the weakening lands in a reviewed diff.\n"
        "  A DoD claim, an unspecified attack class or a new axiom is the same\n"
        "  trade in a different place: write the test, name the waiver, or\n"
        "  re-record — in a diff someone reads.",
        file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
