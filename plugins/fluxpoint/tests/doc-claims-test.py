#!/usr/bin/env python3
"""The docs may not promise a guarantee the code does not provide.

Three claims went stale in the direction that matters -- each described a
property the reader would rely on, and each was false against the shipped
code. A doc that overstates a guarantee is worse than one that says nothing,
because the reader stops looking.

Pinned here so they cannot drift back:
  - the irreversible ledger is gitignored, so it cannot be described as
    committed and cannot promise once-only across clones
  - attestation must state the actual configured failure event and retain
    the distinction between an unreadable exit and a failed gate
  - record-run must distinguish a node claiming green with no row from one
    claiming red with no row, because only the first is suspicious
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
REPO = os.path.dirname(os.path.dirname(PLUGIN))
passed = failed = 0


def report(name, ok, detail):
    global passed, failed
    print(f"{'PASS' if ok else 'FAIL'}  {name:<54} -> {detail}")
    passed, failed = (passed + ok, failed + (not ok))


def read(*parts):
    with open(os.path.join(PLUGIN, *parts), encoding="utf-8") as fh:
        return fh.read()


SKILL = read("skills", "graph-engineering", "SKILL.md")
ATTEST = read("scripts", "attest.py")
LEDGER = read("scripts", "ledger.py")
INIT = read("commands", "init.md")

# ==================== the ledger is local, and says so ==================
m = re.search(r'LEDGER_PATH\s*=\s*os\.path\.join\((.*?)\)', LEDGER)
ledger_rel = "/".join(x.strip().strip('"\'') for x in m.group(1).split(",")) if m else ""
report("the ledger lives under .claude/fluxpoint",
       ledger_rel.startswith(".claude/fluxpoint"), ledger_rel or "NOT FOUND")

gitignore = ""
gi = os.path.join(REPO, ".gitignore")
if os.path.exists(gi):
    gitignore = open(gi, encoding="utf-8").read()
ignored = ".claude/fluxpoint/" in gitignore
report("and that directory is gitignored", ignored,
       "gitignored" if ignored else "TRACKED")

# The claim that must not come back while the path above is ignored.
SKILL_FLAT = re.sub(r"\s+", " ", SKILL)
report("the skill no longer calls the ledger committed",
       "committed ledger" not in SKILL_FLAT,
       "clean" if "committed ledger" not in SKILL_FLAT else "STILL CLAIMED")
_bound = "not committed" in SKILL_FLAT and "fresh clone" in SKILL_FLAT
report("and states the once-only bound explicitly", _bound,
       "stated" if _bound else "NOT STATED")

# Executed: a fresh tree reads as a first run, which is the whole point.
with tempfile.TemporaryDirectory() as d:
    r = subprocess.run([sys.executable, os.path.join(PLUGIN, "scripts", "ledger.py"),
                        "--root", d, "--load", "--campaign", "x"],
                       capture_output=True, text=True)
    empty = r.returncode == 0 and r.stdout.strip() in ("{}", "")
report("a fresh clone's ledger reads as a first run", empty,
       "empty, exit 0" if empty else f"rc={r.returncode} {r.stdout[:20]}")

# ==================== attestation states its runtime boundary =============
HOOKS = json.loads(read('hooks', 'hooks.json'))['hooks']
failure_hook = any('exec-attest.sh' in hook.get('command', '')
                   for group in HOOKS.get('PostToolUseFailure', []) for hook in group.get('hooks', []))
report("attestation documents and registers the runtime failure event",
       'PostToolUseFailure' in ATTEST and failure_hook, "failure event routed")
# Collapse wrapped prose before matching: these are paragraphs, and a claim
# that happens to fall across a line break is still the claim.
FLAT = re.sub(r"\s+", " ", ATTEST)
ok = "carries no information about whether the gate passed" in FLAT
report("and says UNATTESTED carries no information about a red gate",
       ok, "stated" if ok else "NOT STATED")

# ==================== record-run splits the two absences ================
RR = read("scripts", "record-run.py")
report("record-run distinguishes a claimed pass with no row",
       "claimedExit" in RR and "silent_green" in RR, "split")
report("and records it in the durable tally",
       "unattested_claiming_pass" in RR, "recorded")

# ==================== isolation hazards are written down ================
for label, needle in (("the worktree base is not chosen here", "You do not choose the base"),
                      ("measuring nodes need isolation", "measurement*"),
                      ("a dirtied shared tree invalidates a gate", "no longer exists"),
                      ("the harness must be proven in a worktree", "tracked files only")):
    needle = needle.rstrip("*")
    report(label, needle in SKILL_FLAT,
           "documented" if needle in SKILL_FLAT else "MISSING")

report("init.md tells you to prove the harness in a worktree",
       "worktree add --detach" in read("commands", "init.md"), "step present")

# ===== and the snippet it tells you to run reports what it found ========
# EXECUTED, not grepped. The block ended `...harness.sh --full); git worktree
# remove --force ...` — a `;` before cleanup, so the whole thing exits with
# the REMOVE's status and a red harness reported success. `/fluxpoint:init`
# is a command an agent runs, and the exit code is the machine-readable
# answer, so following the documented step gave a false green.
_blocks = re.findall(r"```[a-z]*\n(.*?)```", INIT, re.S)
_probe = next((b for b in _blocks if "worktree add --detach" in b), None)
report("the worktree probe snippet is extractable", bool(_probe),
       "found" if _probe else "NOT FOUND")


def _bash():
    """A bash that RUNS, resolved by an explicit path rather than by name.

    subprocess resolves a bare "bash" through CreateProcess, which searches
    System32 first and finds the WSL launcher — present on stock Windows with
    no distribution installed. It exits non-zero for every argument, which made
    the case below PASS while executing nothing: the exact shape this file
    exists to catch, in the test doing the catching.
    """
    for c in (os.environ.get("SHELL"), shutil.which("bash"),
              r"C:\Program Files\Git\bin\bash.exe", "/bin/bash"):
        if not c:
            continue
        try:
            r = subprocess.run([c, "-c", "echo FPLOK"], capture_output=True,
                               text=True, timeout=60)
        except OSError:
            continue
        if r.returncode == 0 and "FPLOK" in r.stdout:
            return c
    return None


_sh = _bash()
report("a working bash is resolved for the executed case", bool(_sh),
       os.path.basename(_sh) if _sh else "NONE — case would prove nothing")
if _probe and _sh:
    with tempfile.TemporaryDirectory() as d:
        repo = os.path.join(d, "r")
        os.makedirs(os.path.join(repo, "scripts"))
        with open(os.path.join(repo, "scripts", "harness.sh"), "w",
                  encoding="utf-8", newline="\n") as fh:
            fh.write("#!/usr/bin/env bash\necho 'harness: RED'\nexit 1\n")
        for cmd in (["git", "init", "-q", "."], ["git", "config", "user.email", "t@t"],
                    ["git", "config", "user.name", "t"], ["git", "add", "-A"],
                    ["git", "commit", "-qm", "init"]):
            subprocess.run(cmd, cwd=repo, capture_output=True)
        r = subprocess.run([_sh, "-c", _probe], cwd=repo,
                           capture_output=True, text=True)
        report("a failing harness in the worktree is not reported as green",
               r.returncode != 0, f"rc={r.returncode}"
               + ("" if r.returncode else " — SWALLOWED BY CLEANUP"))

# ========== the front page counts what the code actually ships ==========
# "three ratchets hold the proof surface: escape hatches, theorem statements
# and the on-chain budget" outlived the slice that added a fourth. A summary
# undercounting its own subject is the drift this file exists for: a reader
# who trusts the number stops looking for the rest. So the number is checked
# against the list it introduces, and the coverage claims are derived from
# the registries that decide them rather than restated by hand.
ROOT_README = open(os.path.join(REPO, "README.md"), encoding="utf-8").read()
_sec = re.search(r"^## Verified work\s*$(.*?)(?=^## )", ROOT_README, re.S | re.M)
VERIFIED = re.sub(r"\s+", " ", _sec.group(1)).strip() if _sec else ""
report("the root README has a Verified work section", bool(VERIFIED),
       f"{len(VERIFIED)} chars" if VERIFIED else "NOT FOUND")

COUNTS = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7}
_m = re.search(r"\b(%s) ratchets hold the proof surface: (.*?)\." % "|".join(COUNTS),
               VERIFIED)
if _m:
    _listed = [re.sub(r"^and\s+", "", p.strip())
               for p in _m.group(2).split(",") if p.strip()]
    report("the ratchet count matches the list it introduces",
           COUNTS[_m.group(1)] == len(_listed),
           f"says {_m.group(1)}, lists {len(_listed)}")
else:
    report("the ratchet count matches the list it introduces", False,
           "the counted sentence is gone — re-pin the claim or drop this case")

SPEC = read("scripts", "spec-guard.py")
CEX = read("scripts", "cex.py")
DISPLAY = {"aiken": "Aiken", "dafny": "Dafny", "lean": "Lean", "coq": "Coq",
           "isabelle": "Isabelle", "tla": "TLA+", "kani": "Kani",
           "apalache": "apalache"}


def _shown(tool):
    return DISPLAY.get(tool, tool.title())


# Every language the statement ratchet parses, taken from the registry that
# decides it. A parser the summary does not name is one no reader arms.
_m = re.search(r"^COVERED = \{(.*?)\n\}", SPEC, re.S | re.M)
_covered = sorted(set(re.findall(r':\s*"(\w+)"', _m.group(1)))) if _m else []
_missing = [t for t in _covered if _shown(t) not in VERIFIED]
report("every language spec-guard parses is named on the front page",
       bool(_covered) and not _missing,
       f"{len(_covered)} covered" if not _missing else f"MISSING {_missing}")

# Same for the counterexample ledger, against its own tool registry.
_m = re.search(r"^TOOLS = \{(.*?)\n\}", CEX, re.S | re.M)
_tools = re.findall(r'^\s{4}"(\w+)":\s*\{', _m.group(1), re.M) if _m else []
_row = re.search(r"^\| Counterexamples \|.*$", ROOT_README, re.M)
# Hyphens dropped so the registry key `fastcheck` matches the product name
# `fast-check` as a reader would write it.
_row = _row.group(0).lower().replace("-", "") if _row else ""
_missing = [t for t in _tools if t not in _row]
report("every prover the ledger ingests is named in the enforcement table",
       bool(_tools) and not _missing,
       f"{len(_tools)} provers" if not _missing else f"MISSING {_missing}")

# Three gates that exist in the code and were silent on the front page for a
# release. Each is keyed to the constant that implements it, so removing the
# feature relaxes the pin and shipping one leaves the doc owing a sentence.
for label, needle, promised in (
        ("the axiom audit", '"--axioms"', "assumption"),
        ("the attack taxonomy", 'ATTACKS = ".fluxpoint-attacks.json"', "attack"),
        ("the Definition-of-Done citation", "DOD_LINE = re.compile", "Definition-of-Done")):
    if needle not in SPEC:
        report(f"{label} is still implemented", False, f"{needle} is gone — re-pin")
        continue
    report(f"{label} is described where it is claimed", promised in VERIFIED,
           "described" if promised in VERIFIED else f"SILENT on {promised!r}")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
