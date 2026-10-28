#!/usr/bin/env python3
"""Prompt hygiene stays hygienic (issue #68).

The plugin's prompts are clean today and nothing kept them that way: every
future "CRITICAL:" and "double-check your work" is individually reasonable,
and the cost only shows up in aggregate. This pins the scanner that makes
the drift visible — each anti-pattern class fires on a sample and stays
silent on the clean idiom, suppressions are on the record and a spent one
is reported, advisory mode never fails the harness, --strict is the
promotion path, and the shipped prompt surface itself scans clean.
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
REPO = os.path.dirname(os.path.dirname(PLUGIN))
AUDIT = os.path.join(PLUGIN, "scripts", "prompt-audit.py")
passed = failed = 0


def report(name, ok, detail):
    global passed, failed
    print(f"{'PASS' if ok else 'FAIL'}  {name:<58} -> {detail}")
    passed, failed = (passed + ok, failed + (not ok))


def run(files, *flags, allow=None):
    """Scan a temp tree of {relpath: text}. Returns (rc, stdout, stderr)."""
    with tempfile.TemporaryDirectory() as d:
        for rel, text in files.items():
            p = os.path.join(d, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(text)
        if allow is not None:
            with open(os.path.join(d, ".fluxpoint-prompt-audit.json"), "w",
                      encoding="utf-8") as fh:
                fh.write(allow if isinstance(allow, str) else json.dumps(allow))
        r = subprocess.run([sys.executable, AUDIT, "--root", d, *flags, "plugins/x/agents"],
                           capture_output=True, text=True)
        return r.returncode, r.stdout, r.stderr


A = "plugins/x/agents/a.md"

# ==================== each class fires on a sample =======================
for rule, text in [
    ("booster", "CRITICAL: read every file before answering."),
    ("booster", "You must NEVER SKIP a test, and ALWAYS RERUN the suite."),
    ("booster", "Be thorough and double-check the totals."),
    ("booster", "Search exhaustively for every caller of the function."),
    ("booster", "It is crucial that the output is complete!!"),
    ("ritual", "Verify your work before responding."),
    ("ritual", "Re-read your answer and check it against the spec."),
    ("scaffold", "Think step by step inside <thinking> tags."),
    ("scaffold", "Use a chain of thought before the final answer."),
    ("dated", "Use claude-3-5-sonnet-20241022 for the review pass."),
    ("dated", "The default model is claude-sonnet-4-20250514."),
]:
    rc, out, err = run({A: text})
    report(f"{rule}: {text[:44]!r}", f"[{rule}]" in out, "flagged" if f"[{rule}]" in out else out.strip()[:60])

rc, out, err = run({A: "Always run the harness before returning.\n\nNever run the harness before returning."})
report("contradiction: always X and never X in one file", "[contradiction]" in out, "flagged")
rc, out, err = run({A: "Read the repo before you write a single line of code.\n\n"
                       "Start here. Read the repo before you write a single line of code."})
report("repeat: the same imperative sentence twice", "[repeat]" in out, "flagged")
rc, out, err = run({A: "If the argument is `build`, run the build and stop there.\n\n"
                       "If the argument is `gate`, run the build and stop there."})
report("repeat: sentences differing only inside a code span are two",
       "[repeat]" not in out, "not flagged" if "[repeat]" not in out else out.strip()[:60])
rc, out, err = run({A: "Do not touch `NOTE:` labels in the log; keep `CRITICAL: x` as is."})
report("booster: a shouted label inside a code span is quoted, not said",
       "[booster]" not in out, "not flagged" if "[booster]" not in out else out.strip()[:60])
rc, out, err = run({A: "Appended by `scripts/x.py` — do not hand-edit this line.\n\n"
                       "Appended by `scripts/x.py` — do not hand-edit this line."},
                   allow={"allow": [{"path": A, "rule": "repeat",
                                     "text": "Appended by `scripts/x.py`"}]})
report("an allow entry may quote the code span it suppresses",
       "[repeat]" not in out and "spent" not in err, "suppressed")

# ==================== the house idiom stays silent =======================
clean = (
    "You review proofs, not code style. Your single question is whether this\n"
    "still proves what it claims. Severity is CRITICAL, HIGH, MEDIUM, or LOW.\n"
    "Report it as CRITICAL if so. A `repeat` block makes one sweep exhaustive;\n"
    "it does nothing for the next one. Never hand-edit a compiled file; edit\n"
    "the IR and recompile. Include only findings you can show.\n")
rc, out, err = run({A: clean})
report("dense prose with a severity vocabulary scans clean",
       rc == 0 and "finding(s)" in out and "0 finding(s)" in out, out.strip().splitlines()[-1][:70])

# ==================== advisory never fails; strict is the promotion ======
rc, out, err = run({A: "CRITICAL: do it now."})
report("advisory mode exits 0 with findings", rc == 0 and "advisory" in out, f"rc={rc}")
rc, out, err = run({A: "CRITICAL: do it now."}, "--strict")
report("--strict exits 1 on the same findings", rc == 1, f"rc={rc}")
rc, out, err = run({A: clean}, "--strict")
report("--strict exits 0 on a clean surface", rc == 0, f"rc={rc}")
rc, out, err = run({A: "CRITICAL: do it now."}, "--json")
doc = json.loads(out)
report("--json carries the findings and the mode",
       doc["mode"] == "advisory" and doc["findings"][0]["rule"] == "booster", "json")

# ==================== suppressions are on the record ====================
rc, out, err = run({A: "<!-- prompt-audit: allow booster -->\nCRITICAL: do it now."})
report("an inline allow on the line before suppresses", "[booster]" not in out, "suppressed")
rc, out, err = run({A: "CRITICAL: do it now. <!-- prompt-audit: allow booster -->"})
report("an inline allow on the same line suppresses", "[booster]" not in out, "suppressed")
rc, out, err = run({A: "CRITICAL: do it now. <!-- prompt-audit: allow ritual -->"})
report("an allow for a different rule does not", "[booster]" in out, "still flagged")
rc, out, err = run({A: "CRITICAL: do it now."},
                   allow={"allow": [{"path": A, "rule": "booster", "text": "CRITICAL: do it"}]})
report("an allowlist entry suppresses", "[booster]" not in out and "spent" not in err, "suppressed")
rc, out, err = run({A: "plain prose here."},
                   allow={"allow": [{"path": A, "rule": "booster", "text": "CRITICAL: do it"}]})
report("an allowlist entry matching nothing is reported as spent", "matched nothing" in err, "spent")
rc, out, err = run({A: "CRITICAL: do it now."}, allow="{not json")
report("a malformed allowlist allows nothing", "[booster]" in out and "not readable" in err, "refused")
rc, out, err = run({A: "CRITICAL: do it now."},
                   allow={"allow": [{"path": A, "rule": "booster"}]})
report("an allowlist entry missing a field allows nothing",
       "[booster]" in out and "needs exactly" in err, "refused")

# ==================== an empty scope is not a clean scan =================
with tempfile.TemporaryDirectory() as d:
    r = subprocess.run([sys.executable, AUDIT, "--root", d], capture_output=True, text=True)
    report("no prompt files is exit 2, never a green", r.returncode == 2, f"rc={r.returncode}")

# ==================== the shipped surface, and the harness wiring ========
r = subprocess.run([sys.executable, AUDIT, "--root", REPO], capture_output=True, text=True)
last = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
report("the plugin's own prompt surface scans in advisory mode", r.returncode == 0,
       f"rc={r.returncode}")
report("and every allowlist entry is live (none spent)", "matched nothing" not in r.stderr,
       "live" if "matched nothing" not in r.stderr else r.stderr.strip()[:70])
harness = open(os.path.join(REPO, "scripts", "harness.sh"), encoding="utf-8").read()
report("harness.sh runs the audit as an advisory step",
       "prompt-audit.py" in harness and "advise " in harness, "wired")
ci = open(os.path.join(REPO, ".woodpecker.yaml"), encoding="utf-8").read()
report("CI runs the audit on its own line", "prompt-audit.py" in ci, "wired")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
