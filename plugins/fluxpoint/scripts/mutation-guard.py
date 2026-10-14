#!/usr/bin/env python3
"""Mutation score: the judge of the judge.

Every other gate in this plugin asks whether the tests pass. None of them
asks whether the tests can fail. In loop-mode TDD the same agent writes the
code and the thing that grades it, and `proof-guard.py` only catches the
unambiguous vacuities — a body that is literally `True`, a predicate that
always agrees. A suite that executes every line and asserts nothing about
any of them is green, structurally clean, and worthless.

Mutation testing is the one measure that cannot be faked by executing code:
break the implementation on purpose, and count how many broken versions the
suite notices. A test that never fails kills no mutants.

  mutation-guard.py --measure [--accept --reason "..."]   run it; enforce the ratchet
  mutation-guard.py --check                               cheap gate: is there a fresh measurement
  mutation-guard.py --report                              what was measured, for humans

## Why the work is split this way

A mutation run takes minutes to hours — it compiles and tests the project
once per mutant — so it cannot be a per-stop gate. `--measure` is the
expensive half and is where the ratchet lives: it re-measures, and fails
when the score fell or the survivor count rose against what was recorded.
Run it off-session, on the wake/Routine layer. `--check` is the cheap half
that belongs in `harness.sh --full`: it re-runs nothing and only asks
whether a measurement exists and still describes this tree.

Both directions of the ratchet matter. Score is a ratio, and a ratio can be
held flat while coverage shrinks — delete a well-tested module and the
survivors that remain are a smaller share of a smaller whole. So the
absolute survivor count is ratcheted too: it may fall, never rise.

## Loud, not fatal, where it cannot know

A repo with no `.fluxpoint-mutation.json` is dormant and silent, like the
DoD gate with no harness. A repo that declares one but has never measured
is reported NOT MEASURED and still exits 0 — arming a ratchet is a
deliberate step, exactly as in `proof-guard.py`. A measurement that has gone
stale is named, with how stale, and files an inbox item so somebody
schedules the re-run; it fails the build only if the config asks for it.
That default is not timidity: a gate that goes red because an expensive job
has not been re-run yet is a gate people switch off, and it would take the
rest of the harness with it.

Coverage: `cargo-mutants` today. Other toolchains are declared unsupported
by name rather than silently skipped, because a mutation guard that quietly
measures nothing is worse than none at all.

The adapter reads `mutants.out/outcomes.json` and never the tool's stdout —
`--json` on cargo-mutants affects only `--list`, so stdout stays human text
even when asked for JSON. It was written against the schema of 27.1.0 and
refuses an older one rather than misreading it, because fields have been
renamed and collapsed between releases.

Three shapes are real output that would otherwise publish a fake number,
and each is refused by name:

  * A failed baseline still writes an `outcomes.json` — full of zeroes. Read
    as counters it looks like a clean sweep of a project with no mutants.
  * `cargo mutants --check` only compiles mutants. Every one is filed
    `Success`, nothing is caught or missed, and the tool exits 0: a green
    mutation gate that measured nothing.
  * `unviable` mutants did not compile. Scoring them would report 0% for a
    tree the tool itself considers perfectly clean.

And the `outcomes` array is not a list of mutants: it includes the baseline
run, whose `scenario` is the bare string `"Baseline"` where every mutant's
is an object. The top-level counters are the source of truth here for
exactly that reason.
"""
import argparse
import datetime
import json
import os
import subprocess
import sys

BASELINE = ".fluxpoint-proof-baseline.json"
CONFIG = ".fluxpoint-mutation.json"
CONFIG_FIELDS = {"version", "tool", "failWhenStale", "maxStaleCommits", "args", "workDir"}
SUPPORTED = {"cargo-mutants", "stryker"}
# Named so a repo using one of these is told it is not covered, rather than
# reading a silent exit 0 as a clean bill of health.
KNOWN_UNSUPPORTED = {
    "mutmut": "Python", "pitest": "Java", "aiken": "Aiken",
}


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def git(root, *args):
    try:
        r = subprocess.run(["git", "-C", root, *args], capture_output=True,
                           text=True, timeout=30)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:  # noqa: BLE001
        return ""


def load_config(root):
    """(config, findings). Missing file means dormant, not broken."""
    p = os.path.join(root, CONFIG)
    if not os.path.exists(p):
        return None, []
    try:
        with open(p, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        return None, [f"{CONFIG} is not readable JSON ({e}) — the mutation "
                      f"ratchet is DISARMED"]
    if not isinstance(doc, dict):
        return None, [f"{CONFIG} must be an object"]
    f = [f"{CONFIG}: unknown field '{k}' — not part of the manifest"
         for k in sorted(set(doc) - CONFIG_FIELDS)]
    if doc.get("version") != 1:
        f.append(f"{CONFIG}: version must be 1")
    tool = doc.get("tool")
    if tool in KNOWN_UNSUPPORTED:
        f.append(f"{CONFIG}: '{tool}' ({KNOWN_UNSUPPORTED[tool]}) is a real "
                 f"mutation tool but this guard does not parse it yet — "
                 f"supported: {sorted(SUPPORTED)}")
    elif tool not in SUPPORTED:
        f.append(f"{CONFIG}: tool must be one of {sorted(SUPPORTED)}")
    wd = doc.get("workDir")
    if wd is not None:
        # A monorepo keeps the mutation tool in ONE package, so `npx --no-install
        # <tool>` from the repo root cannot resolve it — and npx then tries to
        # FETCH a same-named package from the registry, which is a broken gate and
        # a supply-chain hazard in one. Relative and inside the repo, because a
        # config file must not be able to point the runner at another tree.
        if not isinstance(wd, str) or not wd.strip():
            f.append(f"{CONFIG}: workDir must be a non-empty string")
        elif os.path.isabs(wd) or wd.startswith("~"):
            f.append(f"{CONFIG}: workDir '{wd}' must be RELATIVE to the repo root")
        elif os.path.normpath(wd).startswith(".."):
            f.append(f"{CONFIG}: workDir '{wd}' escapes the repo root")
        elif not os.path.isdir(os.path.join(root, wd)):
            f.append(f"{CONFIG}: workDir '{wd}' does not exist — a typo here fails "
                     f"much later, inside the tool, as an error about the tool")

    if "args" in doc and not (isinstance(doc["args"], list)
                              and all(isinstance(x, str) for x in doc["args"])):
        f.append(f"{CONFIG}: args must be a list of strings")
    return (None if f else doc), f


def load_baseline(root):
    p = os.path.join(root, BASELINE)
    if not os.path.exists(p):
        return {}
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        raise SystemExit(f"mutation-guard: {p} is not readable JSON: {e}")


def write_measurement(root, rec):
    """Record into the shared baseline, preserving every sibling section.

    proof-guard owns `counts` and spec-guard owns `spec` in this same file.
    Rewriting the document wholesale would disarm whichever ratchet did not
    write last, which is the kind of silent disarm this plugin exists to
    refuse.
    """
    doc = load_baseline(root)
    doc.setdefault("version", 1)
    doc["mutation"] = rec
    p = os.path.join(root, BASELINE)
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(doc, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return p


# ------------------------------------------------------------ cargo-mutants
def run_cargo_mutants(root, extra):
    """(measurement, findings). Runs the tool and reads its own JSON."""
    out_dir = os.path.join(root, "mutants.out")
    cmd = ["cargo", "mutants", "--output", root, *extra]
    try:
        proc = subprocess.run(cmd, cwd=root, capture_output=True, text=True)
    except FileNotFoundError:
        return None, ["cargo not on PATH — install cargo-mutants "
                      "(`cargo install cargo-mutants`) or drop the config"]
    except Exception as e:  # noqa: BLE001
        return None, [f"cargo mutants could not be run: {e}"]

    p = os.path.join(out_dir, "outcomes.json")
    if not os.path.exists(p):
        tail = (proc.stderr or proc.stdout or "")[-600:]
        return None, [
            f"cargo mutants exited {proc.returncode} but wrote no "
            f"{os.path.relpath(p, root)} — nothing was measured. Tail:\n{tail}"]
    try:
        with open(p, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        return None, [f"{p} is not readable JSON ({e})"]
    return parse_cargo_mutants(doc)


def run_stryker(root, extra, work_dir=None):
    """(measurement, findings). Runs Stryker and reads its own JSON report.

    `npx` rather than a bare binary: Stryker is a dev dependency in every JS
    project that has it, and a globally installed one would be a different
    version from the one the repo pins.
    """
    cwd = os.path.join(root, work_dir) if work_dir else root
    cmd = ["npx", "--no-install", "stryker", "run", *extra]
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    except FileNotFoundError:
        return None, ["npx not on PATH — install Node, or add "
                      "@stryker-mutator/core to the project"]
    except Exception as e:  # noqa: BLE001
        return None, [f"stryker could not be run: {e}"]

    p = os.path.join(cwd, STRYKER_REPORT)
    if not os.path.exists(p):
        tail = (proc.stderr or proc.stdout or "")[-600:]
        return None, [
            f"stryker exited {proc.returncode} but wrote no "
            f"{os.path.relpath(p, root)} — nothing was measured. The json reporter must "
            f"be enabled (\"reporters\": [\"json\"]). Tail:\n{tail}"]
    try:
        with open(p, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        return None, [f"{p} is not readable JSON ({e})"]
    return parse_stryker(doc)


# The schema this parser was written against and verified on. cargo-mutants
# has renamed and collapsed fields between releases (`cargo_result` ->
# `process_status`, `command` -> `argv`, `line`/`return_type` folded into a
# `function` submessage plus `span`, the `failure` counter removed), and the
# project's own stability page permits more. A version this has not been
# read against is reported rather than parsed on optimism.
CARGO_MUTANTS_TESTED = "27.1.0"


def _ver_tuple(s):
    out = []
    for part in str(s or "").split("."):
        digits = "".join(c for c in part if c.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out[:3]) or (0,)


def parse_cargo_mutants(doc):
    """(measurement, findings) from cargo-mutants' outcomes.json.

    The top-level counters are the source of truth. Tallying the `outcomes`
    array instead would be wrong in two ways the tool documents: the array
    includes the baseline run (so it is one longer than the mutant count),
    and its `scenario` is a bare string `"Baseline"` for that entry but an
    object `{"Mutant": {...}}` for every other — a shape that punishes
    anything assuming uniformity.

    Scoring follows the mutation-testing-elements convention Stryker
    publishes: detected = caught + timeout, undetected = missed, and
    `valid` excludes `unviable`. Unviable mutants did not compile, so they
    say nothing about the tests; counting them would report 0% for a tree
    the tool itself considers clean. A timeout is counted as detected
    because the suite did not silently pass it — but it is recorded
    separately, since a timeout usually means the limit is wrong rather
    than that a test did its job.
    """
    if not isinstance(doc, dict):
        return None, ["outcomes.json is not an object"]
    ver = doc.get("cargo_mutants_version")
    findings = []
    if ver and _ver_tuple(ver) < _ver_tuple(CARGO_MUTANTS_TESTED):
        findings.append(
            f"outcomes.json was written by cargo-mutants {ver}; this parser "
            f"was verified against {CARGO_MUTANTS_TESTED} and the schema has "
            f"changed between releases. Refusing to parse rather than "
            f"misreading it — upgrade cargo-mutants, or check this parser "
            f"against {ver} first.")
        return None, findings

    counts = {}
    for k in ("total_mutants", "caught", "missed", "timeout", "unviable", "success"):
        v = doc.get(k)
        counts[k] = v if isinstance(v, int) else None
    if counts["caught"] is None or counts["missed"] is None:
        return None, ["outcomes.json carries no caught/missed counters — this "
                      "is not a shape this parser understands"]

    total = counts["total_mutants"] or 0
    if total == 0:
        # Written, and empty, when the baseline itself failed: the tool never
        # tested a mutant. Reading the zeroes as a clean sweep would publish
        # a perfect score for a broken build.
        return None, ["cargo-mutants tested 0 mutants — the baseline run "
                      "failed, so nothing was measured. Fix the suite first: "
                      "a score computed here would describe a build that "
                      "never ran."]
    if (counts["success"] or 0) > 0 and counts["caught"] == 0 and counts["missed"] == 0:
        # `--check` only compiles mutants; it files every one as Success and
        # exits 0. A gate trusting that exit reports a green mutation run
        # that measured nothing at all.
        return None, ["this looks like `cargo mutants --check`, which only "
                      "proves mutants compile and never runs the tests. It "
                      "cannot produce a score — drop --check from the "
                      "configured args."]

    detected = counts["caught"] + (counts["timeout"] or 0)
    valid = detected + counts["missed"]
    if valid == 0:
        return None, ["no viable mutants were tested — nothing was measured"]

    survivors = []
    for o in doc.get("outcomes") or []:
        if not isinstance(o, dict) or "missed" not in str(o.get("summary", "")).lower():
            continue
        scen = o.get("scenario")
        m = scen.get("Mutant") if isinstance(scen, dict) else None
        if not isinstance(m, dict):
            continue
        span = m.get("span") if isinstance(m.get("span"), dict) else {}
        start = span.get("start") if isinstance(span.get("start"), dict) else {}
        if len(survivors) < 50:
            survivors.append({
                # The mutated region, not `function.span`, which is the whole
                # enclosing function.
                "file": m.get("file") or "?",
                "line": start.get("line") or 0,
                # Pre-formatted by the tool as "<file>:<line>:<col>: <what>",
                # and byte-identical to the lines in missed.txt.
                "what": m.get("name") or m.get("replacement") or "?",
            })

    return {
        "tool": "cargo-mutants",
        "toolVersion": str(ver or "unknown"),
        "score": round(detected / valid, 4),
        "killed": counts["caught"],
        "survived": counts["missed"],
        "scored": valid,
        "unviable": counts["unviable"] or 0,
        "timeout": counts["timeout"] or 0,
        "survivors": survivors,
    }, findings



# The report Stryker writes with `"reporters": ["json"]`. Schema version 1.0 is
# the mutation-testing-elements format, which is also the convention the
# cargo-mutants scoring above follows — so the two tools land on the same
# numbers by construction rather than by coincidence.
STRYKER_REPORT = os.path.join("reports", "mutation", "mutation.json")

# Statuses that say something about the TESTS. CompileError and Ignored say
# nothing — the mutant never ran — and counting them would report a worse score
# for a tree the tool itself considers clean, exactly as `unviable` does for
# cargo-mutants. RuntimeError is excluded for the same reason: the mutant broke
# before any assertion could have caught it.
STRYKER_DETECTED = {"Killed", "Timeout"}
STRYKER_UNDETECTED = {"Survived", "NoCoverage"}
STRYKER_EXCLUDED = {"CompileError", "Ignored", "RuntimeError", "Pending"}


def parse_stryker(doc):
    """(measurement, findings) from Stryker's mutation.json.

    ⚠️ NoCoverage counts as UNDETECTED, not as excluded. A mutant no test
    exercises is the single most important thing this measure reports — it is
    a line the suite cannot possibly have an opinion about — and folding it in
    with the mutants that failed to compile would let a suite raise its score
    by testing less.
    """
    if not isinstance(doc, dict):
        return None, ["mutation.json is not an object"]
    files = doc.get("files")
    if not isinstance(files, dict):
        return None, ["mutation.json has no 'files' object — is this a Stryker "
                      "json report? The html reporter does not produce one."]

    counts = {"detected": 0, "undetected": 0, "excluded": 0, "timeout": 0}
    survivors, unknown = [], set()
    for path, entry in sorted(files.items()):
        for m in (entry or {}).get("mutants") or []:
            st = m.get("status")
            if st in STRYKER_DETECTED:
                counts["detected"] += 1
                if st == "Timeout":
                    counts["timeout"] += 1
            elif st in STRYKER_UNDETECTED:
                counts["undetected"] += 1
                if len(survivors) < 50:
                    loc = (m.get("location") or {}).get("start") or {}
                    survivors.append({
                        "file": path,
                        "line": loc.get("line") or 0,
                        # `mutatorName` names the operator; the replacement is
                        # what it became. Together they read like a diff.
                        "what": f"{m.get('mutatorName') or '?'} -> "
                                f"{(m.get('replacement') or '?')[:60]}"
                                + (" (no test covers it)" if st == "NoCoverage" else ""),
                    })
            elif st in STRYKER_EXCLUDED:
                counts["excluded"] += 1
            else:
                unknown.add(str(st))

    findings = []
    if unknown:
        # Loud, because an unrecognised status is silently dropped from BOTH
        # halves of the ratio and would move the score without moving the tests.
        findings.append(
            f"mutation.json carries mutant status(es) this parser does not know "
            f"({', '.join(sorted(unknown))}) — they are counted in neither half "
            f"of the score, so the number below is about fewer mutants than were run")
    ver = str(doc.get("schemaVersion") or "unknown")
    valid = counts["detected"] + counts["undetected"]
    if valid == 0:
        return None, findings + ["mutation.json scored no mutants — every one "
                                 "was excluded, so there is no measurement here"]

    return {
        "tool": "stryker",
        "toolVersion": f"schema {ver}",
        "score": round(counts["detected"] / valid, 4),
        "killed": counts["detected"] - counts["timeout"],
        "survived": counts["undetected"],
        "scored": valid,
        "unviable": counts["excluded"],
        "timeout": counts["timeout"],
        "survivors": survivors,
    }, findings


# ------------------------------------------------------------------ commands
def measure(root, cfg, accept, reason, src=None, persist=True):
    prior = load_baseline(root).get("mutation") or {}
    if src:
        # The run already happened — commonly in CI, where the mutation job
        # is its own long-running step. Recording and ratcheting its result
        # should not require running it a second time here.
        try:
            with open(src, encoding="utf-8") as fh:
                doc = json.load(fh)
            rec, findings = (parse_stryker(doc) if cfg["tool"] == "stryker"
                             else parse_cargo_mutants(doc))
        except (OSError, json.JSONDecodeError) as e:
            rec, findings = None, [f"{src} is not readable JSON ({e})"]
        if rec is None and not findings:
            findings = [f"{src} carries no scored mutants — nothing to record"]
    else:
        extra = list(cfg.get("args") or [])
        wd = cfg.get("workDir")
        rec, findings = (run_stryker(root, extra, wd) if cfg["tool"] == "stryker"
                         else run_cargo_mutants(root, extra))
    for f in findings:
        print(f"mutation-guard: {f}", file=sys.stderr)
    if findings and not persist:
        # Tightening cannot lock in a score whose parser dropped unknown data.
        return 1
    if rec is None:
        if not findings:
            print("mutation-guard: the run produced no scored mutants — "
                  "nothing was measured", file=sys.stderr)
        return 1

    rec["headSha"] = git(root, "rev-parse", "HEAD")
    rec["when"] = now()
    rec["note"] = ("Mutation score. --measure fails when the score falls or the "
                   "survivor count rises against this record; --accept re-records "
                   "a weaker one, which is a reviewable diff.")

    if prior:
        fell = rec["score"] < prior.get("score", 0) - 1e-9
        rose = rec["survived"] > prior.get("survived", 10 ** 9)
        if (fell or rose) and not accept:
            print("\nmutation-guard: RED — the suite got weaker\n", file=sys.stderr)
            if fell:
                print(f"  score:     {prior.get('score')} -> {rec['score']}",
                      file=sys.stderr)
            if rose:
                print(f"  survivors: {prior.get('survived')} -> {rec['survived']} "
                      f"(a ratio can hold flat while coverage shrinks, so the "
                      f"absolute count is ratcheted too)", file=sys.stderr)
            for s in rec["survivors"][:8]:
                print(f"      {s['file']}:{s['line']}  {s['what']}", file=sys.stderr)
            print("\n  Every survivor is a change to your code that no test "
                  "noticed.\n  Kill them, or re-record deliberately:\n"
                  "    mutation-guard.py --measure --accept --reason \"...\"",
                  file=sys.stderr)
            return 1
        if accept and (fell or rose):
            if len((reason or "").strip()) < 20:
                print("mutation-guard: --accept needs a --reason of at least 20 "
                      "characters. Lowering this floor is the one move that "
                      "weakens the ratchet.", file=sys.stderr)
                return 1
            rec["acceptedWeaker"] = {"reason": reason.strip(),
                                     "from": {"score": prior.get("score"),
                                              "survived": prior.get("survived")}}

    if not persist:
        return rec
    p = write_measurement(root, rec)
    verdict = "stronger" if prior and rec["score"] > prior.get("score", 0) else "recorded"
    print(f"mutation-guard: {verdict} — score {rec['score']}, "
          f"{rec['killed']} killed, {rec['survived']} survived "
          f"({rec['unviable']} unviable, {rec['timeout']} timeout) -> {p}")
    if rec["survived"]:
        print("mutation-guard: surviving mutants (changes no test noticed):")
        for s in rec["survivors"][:10]:
            print(f"    {s['file']}:{s['line']}  {s['what']}")
    return 0


def staleness(root, rec):
    """(commits_behind, changed_files) since the measured commit."""
    sha = rec.get("headSha")
    if not sha:
        return None, None
    rng = f"{sha}..HEAD"
    behind = git(root, "rev-list", "--count", rng)
    names = git(root, "diff", "--name-only", rng)
    if not behind and not names:
        # An unknown sha (rebased away, shallow clone) cannot be compared.
        if not git(root, "cat-file", "-t", sha):
            return -1, None
    files = [n for n in names.splitlines() if n.strip()]
    try:
        return int(behind or 0), files
    except ValueError:
        return 0, files


def check(root):
    cfg, findings = load_config(root)
    for f in findings:
        print(f"mutation-guard: {f}", file=sys.stderr)
    if findings:
        return 1
    if cfg is None:
        print("mutation-guard: no .fluxpoint-mutation.json — dormant")
        return 0

    rec = load_baseline(root).get("mutation")
    if not rec:
        print(f"mutation-guard: {CONFIG} declares {cfg['tool']} but nothing has "
              f"been measured, so the ratchet is NOT armed. Run: "
              f"mutation-guard.py --measure", file=sys.stderr)
        return 0  # arming is a deliberate step; bootstrapping must not block

    behind, changed = staleness(root, rec)
    fresh = f"score {rec.get('score')}, {rec.get('survived')} survivor(s)"
    if behind == -1:
        print(f"mutation-guard: measured at a commit this repo no longer has "
              f"({rec.get('headSha', '')[:8]}) — {fresh}, but it cannot be "
              f"related to this tree. Re-measure.", file=sys.stderr)
        return 1 if cfg.get("failWhenStale") else 0

    limit = int(cfg.get("maxStaleCommits", 20))
    if behind and behind > limit:
        msg = (f"the measurement is {behind} commit(s) behind HEAD "
               f"(limit {limit}), touching {len(changed or [])} file(s) — "
               f"{fresh} describes a tree that has moved")
        print(f"mutation-guard: STALE — {msg}", file=sys.stderr)
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            import inbox as _inbox
            _inbox.add(root, "mutation-stale", "mutation-guard", "",
                       f"{msg}. Re-run: mutation-guard.py --measure")
        except Exception as e:  # noqa: BLE001
            print(f"mutation-guard: could not file an inbox row: {e}",
                  file=sys.stderr)
        if cfg.get("failWhenStale"):
            return 1
        print("mutation-guard: not failing the build for staleness "
              "(failWhenStale is off) — an expensive job left un-run is a "
              "reason to schedule it, not to block a stop.")
        return 0

    print(f"mutation-guard: green — {fresh}, measured {behind or 0} commit(s) ago")
    return 0


def report(root):
    cfg, findings = load_config(root)
    for f in findings:
        print(f"mutation-guard: {f}", file=sys.stderr)
    rec = load_baseline(root).get("mutation")
    if not rec:
        print("mutation-guard: nothing measured yet")
        return 0
    behind, changed = staleness(root, rec)
    print(f"mutation-guard: {rec.get('tool')} score {rec.get('score')} "
          f"({rec.get('killed')} killed / {rec.get('scored')} scored)")
    print(f"  measured {rec.get('when')} at {str(rec.get('headSha'))[:8]}, "
          f"{behind if behind and behind > 0 else 0} commit(s) ago")
    print(f"  unviable {rec.get('unviable')}, timeout {rec.get('timeout')} "
          f"(neither is scored: a mutant that did not compile tested nothing)")
    if rec.get("acceptedWeaker"):
        aw = rec["acceptedWeaker"]
        print(f"  a weaker score was accepted deliberately: {aw.get('reason')}")
    for s in (rec.get("survivors") or [])[:20]:
        print(f"    survivor {s['file']}:{s['line']}  {s['what']}")
    return 0


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=".")
    ap.add_argument("--accept", action="store_true",
                    help="re-record a weaker score (needs --reason)")
    ap.add_argument("--reason", default="")
    ap.add_argument("--from", dest="src",
                    help="record an outcomes.json the tool already produced "
                         "(a CI job that ran it separately) instead of "
                         "running it here")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--measure", action="store_true")
    g.add_argument("--check", action="store_true")
    g.add_argument("--tighten", action="store_true")
    g.add_argument("--report", action="store_true")
    a = ap.parse_args()
    if a.tighten:
        from ratchet import cli
        return cli("mutation", a.root, a.src)


    if a.check:
        return check(a.root)
    if a.report:
        return report(a.root)

    cfg, findings = load_config(a.root)
    for f in findings:
        print(f"mutation-guard: {f}", file=sys.stderr)
    if findings:
        return 1
    if cfg is None:
        print(f"mutation-guard: no {CONFIG} — nothing to measure. Declare the "
              f"tool first, so 'unmeasured' and 'not applicable' stay "
              f"different states.", file=sys.stderr)
        return 1
    return measure(a.root, cfg, a.accept, a.reason, a.src)


if __name__ == "__main__":
    sys.exit(main())
