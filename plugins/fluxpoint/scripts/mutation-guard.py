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
when the exact detected ratio fell, the survivor count rose, or the measured
mutation surface shrank against what was recorded.
Run it off-session, on the wake/Routine layer. `--check` is the cheap half
that belongs in `harness.sh --full`: it re-runs nothing and only asks
whether a measurement exists and still describes this tree.

All three dimensions of the ratchet matter. The detected ratio may not fall,
the absolute survivor count may not rise, and the scored mutation surface may
not shrink. The ratio comparison uses integer counts rather than the rounded
display score, so four-decimal formatting cannot hide a regression.

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
import math
import os
import re
import subprocess
import sys

BASELINE = ".fluxpoint-proof-baseline.json"
CONFIG = ".fluxpoint-mutation.json"
CONFIG_FIELDS = {"version", "tool", "failWhenStale", "maxStaleCommits", "args", "workDir"}
SUPPORTED = {"cargo-mutants", "stryker"}
FULL_COMMIT = re.compile(r"^[0-9a-f]{40}$")
# Named so a repo using one of these is told it is not covered, rather than
# reading a silent exit 0 as a clean bill of health.
KNOWN_UNSUPPORTED = {
    "mutmut": "Python", "pitest": "Java", "aiken": "Aiken",
}


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _git_status(root, *args, raw=False):
    try:
        entries = _safe_path_entries(root)
        executable = _find_executable(root, "git", entries)
        if not executable:
            return False, b"" if raw else ""
        r = subprocess.run([executable, "-C", root, *args], capture_output=True,
                           text=not raw, timeout=30, env=_child_env(entries))
        output = r.stdout if raw else r.stdout.strip()
        return r.returncode == 0, output
    except Exception:  # noqa: BLE001
        return False, b"" if raw else ""


def git(root, *args):
    ok, output = _git_status(root, *args)
    return output if ok else ""


def _allowed_untracked(root, cfg, src):
    exact = {BASELINE}
    if src:
        source = os.path.abspath(src if os.path.isabs(src) else os.path.join(root, src))
        if _inside(root, source):
            exact.add(os.path.relpath(source, root).replace(os.sep, "/"))

    prefixes = []
    if cfg and cfg.get("tool") == "cargo-mutants":
        prefixes.append("mutants.out")
    elif cfg and cfg.get("tool") == "stryker":
        work_dir = cfg.get("_resolvedWorkDir") or root
        if _inside(root, work_dir):
            relative_work_dir = os.path.relpath(work_dir, root).replace(os.sep, "/")
            base = "" if relative_work_dir == "." else relative_work_dir + "/"
            prefixes.extend((base + "reports/mutation", base + ".stryker-tmp"))
    return exact, prefixes


def clean_measurement_head(root, cfg=None, src=None):
    """Return HEAD only when every input-visible repository path is clean."""
    head = git(root, "rev-parse", "--verify", "HEAD^{commit}")
    if not FULL_COMMIT.fullmatch(head):
        return None, "cannot bind this measurement to a full trusted Git HEAD"
    status_ok, tracked = _git_status(
        root, "status", "--porcelain=v1", "--untracked-files=no",
        "--ignore-submodules=none")
    if not status_ok:
        return None, "Git could not prove the tracked tree and index are clean"
    if tracked:
        return None, ("tracked files or the index are dirty; commit or restore them "
                      "before measuring")

    untracked_ok, raw_untracked = _git_status(
        root, "ls-files", "--others", "--exclude-standard", "-z", raw=True)
    if not untracked_ok:
        return None, "Git could not enumerate nonignored untracked inputs"
    exact, prefixes = _allowed_untracked(root, cfg, src)
    refused = []
    for prefix in prefixes:
        prefix_path = os.path.join(root, *prefix.split("/"))
        if not _inside(root, prefix_path):
            refused.append(prefix + "/ (resolves outside the repository)")
    for raw_path in raw_untracked.split(b"\0"):
        if not raw_path:
            continue
        native_path = os.fsdecode(raw_path)
        relative = native_path.replace(os.sep, "/")
        candidate = os.path.abspath(os.path.join(root, native_path))
        inside = _inside(root, candidate)
        allowed = relative in exact or any(
            relative.startswith(prefix + "/") for prefix in prefixes)
        if not inside or not allowed:
            refused.append(relative)
    if refused:
        sample = ", ".join(refused[:5])
        suffix = " ..." if len(refused) > 5 else ""
        return None, ("nonignored untracked paths can influence mutation tests "
                      f"({sample}{suffix}); commit, ignore, or remove them before "
                      "measuring")
    return head, None


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
        else:
            resolved_wd = os.path.realpath(os.path.join(root, wd))
            if not _inside(root, resolved_wd):
                f.append(f"{CONFIG}: workDir '{wd}' resolves outside and escapes "
                         f"the repo root")
            elif not os.path.isdir(resolved_wd):
                f.append(f"{CONFIG}: workDir '{wd}' does not exist — a typo here "
                         f"fails much later, inside the tool, as an error about "
                         f"the tool")
            else:
                doc["_resolvedWorkDir"] = resolved_wd

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


def write_measurement(root, rec, doc=None):
    """Record into the shared baseline, preserving every sibling section.

    proof-guard owns `counts` and spec-guard owns `spec` in this same file.
    Rewriting the document wholesale would disarm whichever ratchet did not
    write last, which is the kind of silent disarm this plugin exists to
    refuse.
    """
    doc = load_baseline(root) if doc is None else doc
    doc["version"] = 1
    doc["mutation"] = rec
    p = os.path.join(root, BASELINE)
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(doc, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return p


def _clear_live_report(root, path):
    relative = os.path.relpath(path, root).replace(os.sep, "/")
    if not _inside(root, path):
        return f"{relative} resolves outside the repository"
    tracked_ok, tracked = _git_status(root, "ls-files", "--", relative)
    if not tracked_ok:
        return f"Git could not prove {relative} is generated and untracked"
    if tracked:
        return f"{relative} is tracked; refusing to delete or overwrite it"
    if not os.path.lexists(path):
        return None
    if os.path.isdir(path) and not os.path.islink(path):
        return f"{relative} is a directory, not the generated JSON report"
    try:
        os.unlink(path)
    except OSError as e:
        return f"could not remove the pre-existing {relative}: {e}"
    return None


def _read_live_report(root, path, proc, label, parser, reporter_hint=""):
    relative = os.path.relpath(path, root).replace(os.sep, "/")
    if (not os.path.isfile(path) or os.path.islink(path)
            or not _inside(root, path)):
        tail = (proc.stderr or proc.stdout or "")[-600:]
        hint = f" {reporter_hint}" if reporter_hint else ""
        return None, [f"{label} exited {proc.returncode} but wrote no fresh, "
                      f"repository-local {relative} — nothing was measured.{hint} "
                      f"Tail:\n{tail}"]
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        return None, [f"{path} is not readable JSON ({e})"]
    return parser(doc)


# ------------------------------------------------------------ cargo-mutants
def run_cargo_mutants(root, extra):
    """(measurement, findings). Runs the tool and reads its own JSON."""
    out_dir = os.path.join(root, "mutants.out")
    report = os.path.join(out_dir, "outcomes.json")
    report_error = _clear_live_report(root, report)
    if report_error:
        return None, [report_error]
    entries = _safe_path_entries(root)
    cargo = _find_executable(root, "cargo", entries)
    if not cargo:
        return None, ["cargo not on PATH — install cargo-mutants "
                      "(`cargo install cargo-mutants`) or drop the config"]
    cmd = [cargo, "mutants", "--output", root, *extra]
    try:
        proc = subprocess.run(cmd, cwd=root, capture_output=True, text=True,
                              env=_child_env(entries))
    except FileNotFoundError:
        return None, ["cargo not on PATH — install cargo-mutants "
                      "(`cargo install cargo-mutants`) or drop the config"]
    except Exception as e:  # noqa: BLE001
        return None, [f"cargo mutants could not be run: {e}"]

    # Survivors can make these tools exit nonzero after writing a complete
    # report. Fresh creation plus strict schema validation is the completion
    # proof, not the process status alone.
    return _read_live_report(root, report, proc, "cargo mutants",
                             parse_cargo_mutants)


def _inside(base, candidate):
    try:
        return os.path.commonpath([os.path.normcase(os.path.realpath(base)),
                                   os.path.normcase(os.path.realpath(candidate))]) \
            == os.path.normcase(os.path.realpath(base))
    except (OSError, ValueError):
        return False


def _safe_path_entries(root):
    cwd = os.path.realpath(os.getcwd())
    entries = []
    for raw in os.environ.get("PATH", "").split(os.pathsep):
        entry = raw.strip().strip('"')
        if not entry or not os.path.isabs(entry) or not os.path.isdir(entry):
            continue
        resolved = os.path.realpath(entry)
        if _inside(root, resolved) or os.path.normcase(resolved) == os.path.normcase(cwd):
            continue
        if os.path.normcase(resolved) not in {os.path.normcase(item) for item in entries}:
            entries.append(resolved)
    return entries


def _find_executable(root, name, entries):
    suffixes = [""]
    if os.name == "nt" and not os.path.splitext(name)[1]:
        suffixes = [ext.lower() for ext in os.environ.get(
            "PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";") if ext]
    for directory in entries:
        for suffix in suffixes:
            candidate = os.path.join(directory, name + suffix)
            if os.path.isfile(candidate) and (os.name == "nt" or os.access(candidate, os.X_OK)):
                resolved = os.path.realpath(candidate)
                if _inside(root, resolved):
                    continue
                return resolved
    return None


def _child_env(entries):
    env = dict(os.environ)
    for key in list(env):
        if key.upper().startswith("GIT_"):
            del env[key]
    env["PATH"] = os.pathsep.join(entries)
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    if os.name == "nt":
        env["NoDefaultCurrentDirectoryInExePath"] = "1"
    return env


def _trusted_file(root, candidate, executable=False):
    if not os.path.isfile(candidate):
        return None
    if executable and os.name != "nt" and not os.access(candidate, os.X_OK):
        return None
    resolved = os.path.realpath(candidate)
    return None if _inside(root, resolved) else resolved


def run_stryker(root, extra, work_dir=None):
    """(measurement, findings). Runs Stryker and reads its own JSON report.

    `npx` rather than a bare binary: Stryker is a dev dependency in every JS
    project that has it, and a globally installed one would be a different
    version from the one the repo pins.
    """
    cwd = os.path.join(root, work_dir) if work_dir else root
    report = os.path.join(cwd, STRYKER_REPORT)
    report_error = _clear_live_report(root, report)
    if report_error:
        return None, [report_error]
    path_entries = _safe_path_entries(root)
    npx = _find_executable(root, "npx", path_entries)
    if not npx:
        return None, ["npx not on PATH — install Node, or add "
                      "@stryker-mutator/core to the project"]
    cmd = [npx, "--no-install", "stryker", "run", *extra]
    if os.name == "nt" and npx.lower().endswith((".cmd", ".bat")):
        directory = os.path.dirname(npx)
        node = _trusted_file(root, os.path.join(directory, "node.exe"), executable=True)
        cli = _trusted_file(root, os.path.join(directory, "node_modules", "npm", "bin", "npx-cli.js"))
        if node and cli:
            cmd = [node, cli, "--no-install", "stryker", "run", *extra]
        else:
            return None, ["npx on PATH has no trusted Node CLI beside it"]
    child_env = _child_env(path_entries)
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                              env=child_env)
    except FileNotFoundError:
        return None, ["npx not on PATH — install Node, or add "
                      "@stryker-mutator/core to the project"]
    except Exception as e:  # noqa: BLE001
        return None, [f"stryker could not be run: {e}"]

    return _read_live_report(
        root, report, proc, "stryker", parse_stryker,
        'The json reporter must be enabled ("reporters": ["json"]).')


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
    counter_names = ("total_mutants", "caught", "missed", "timeout", "unviable",
                     "success")
    if any(type(doc.get(k)) is not int for k in ("caught", "missed")):
        return None, ["outcomes.json carries no caught/missed counters — this "
                      "is not a shape this parser understands"]
    if any(type(doc.get(k)) is not int or doc[k] < 0 for k in counter_names):
        return None, ["outcomes.json counters must all be non-negative integers "
                      "— booleans, missing values, and negative counts are not "
                      "a completed cargo-mutants report"]
    counts = {k: doc[k] for k in counter_names}

    ver = doc.get("cargo_mutants_version")
    if not ver or _ver_tuple(ver) < _ver_tuple(CARGO_MUTANTS_TESTED):
        return None, [
            f"outcomes.json was written by cargo-mutants {ver or 'an unknown version'}; "
            f"this parser was verified against {CARGO_MUTANTS_TESTED} and an older schema "
            f"may read differently. Refusing to parse rather than misreading it — pin "
            f"{CARGO_MUTANTS_TESTED} or newer, or verify this parser against {ver or 'that version'} first."
        ]

    classified = sum(counts[k] for k in ("caught", "missed", "timeout",
                                          "unviable", "success"))
    if counts["total_mutants"] != classified:
        return None, [f"outcomes.json arithmetic is inconsistent: total_mutants is "
                      f"{counts['total_mutants']} but its outcome counters sum to "
                      f"{classified} — refusing a partial or corrupt report"]

    total = counts["total_mutants"]
    if total == 0:
        # Written, and empty, when the baseline itself failed: the tool never
        # tested a mutant. Reading the zeroes as a clean sweep would publish
        # a perfect score for a broken build.
        return None, ["cargo-mutants tested 0 mutants — the baseline run "
                      "failed, so nothing was measured. Fix the suite first: "
                      "a score computed here would describe a build that "
                      "never ran."]
    if counts["success"] > 0:
        # `--check` only compiles mutants; it files every one as Success and
        # exits 0. A gate trusting that exit reports a green mutation run
        # that measured nothing at all.
        return None, ["this looks like `cargo mutants --check`, which only "
                      "proves mutants compile and never runs the tests. It "
                      "cannot produce a score — drop --check from the "
                      "configured args."]
    if not isinstance(doc.get("end_time"), str) or not doc["end_time"].strip():
        return None, ["outcomes.json has no end_time — cargo-mutants writes this "
                      "file during the run, so this is an unfinished report and "
                      "cannot arm a ratchet"]

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
    }, []



# The report Stryker writes with `"reporters": ["json"]`. Schema version 1.0 is
# the mutation-testing-elements format, which is also the convention the
# cargo-mutants scoring above follows — so the two tools land on the same
# numbers by construction rather than by coincidence.
STRYKER_REPORT = os.path.join("reports", "mutation", "mutation.json")

# Statuses that say something about the TESTS. CompileError and Ignored say
# nothing, and counting them would report a worse score for a tree the tool
# itself considers clean, exactly as `unviable` does for cargo-mutants.
# RuntimeError is excluded for the same reason: the mutant broke before any
# assertion could have caught it. Pending is different: the tool generated that
# mutant but has not finished running it, so its presence makes the report
# incomplete rather than excluding it from the denominator.
STRYKER_DETECTED = {"Killed", "Timeout"}
STRYKER_UNDETECTED = {"Survived", "NoCoverage"}
STRYKER_EXCLUDED = {"CompileError", "Ignored", "RuntimeError"}
STRYKER_SCHEMA = "1.0"
# The note --measure last wrote may be a prior release's wording; validation
# accepts any of these so an upgrade does not turn an armed ratchet RED before
# the next --measure rewrites it to MEASUREMENT_NOTE.
PRIOR_MEASUREMENT_NOTES = frozenset({
    ("Mutation score. --measure fails when the score falls or the survivor count "
     "rises against this record; --accept re-records a weaker one, which is a "
     "reviewable diff."),
})

MEASUREMENT_NOTE = ("Mutation score. --measure fails when the score falls or the "
                    "survivor count rises, or the scored mutation surface shrinks "
                    "against this record; exact counts decide the ratio, while the "
                    "stored score is display-only. --accept re-records a weaker "
                    "one, which is a reviewable diff.")


def _ratchet_dimensions(current, prior):
    current_detected = current["killed"] + current["timeout"]
    prior_detected = prior["killed"] + prior["timeout"]
    ratio_fell = (current_detected * prior["scored"]
                  < prior_detected * current["scored"])
    survivors_rose = current["survived"] > prior["survived"]
    surface_shrank = current["scored"] < prior["scored"]
    return ratio_fell, survivors_rose, surface_shrank


def _ratio_rose(current, prior):
    current_detected = current["killed"] + current["timeout"]
    prior_detected = prior["killed"] + prior["timeout"]
    return (current_detected * prior["scored"]
            > prior_detected * current["scored"])


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
    schema = doc.get("schemaVersion")
    if schema != STRYKER_SCHEMA:
        return None, [f"mutation.json schemaVersion is {schema or 'missing'}; this "
                      f"parser is verified against {STRYKER_SCHEMA}. Refusing to "
                      f"score an unverified report schema."]

    counts = {"detected": 0, "undetected": 0, "excluded": 0, "timeout": 0}
    survivors, unknown, pending = [], set(), 0
    for path, entry in sorted(files.items()):
        if not isinstance(entry, dict) or not isinstance(entry.get("mutants"), list):
            return None, [f"mutation.json file entry '{path}' has no mutants array"]
        for m in entry["mutants"]:
            if not isinstance(m, dict):
                return None, [f"mutation.json file entry '{path}' contains a "
                              f"non-object mutant"]
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
            elif st == "Pending":
                pending += 1
            else:
                unknown.add(str(st))

    findings = []
    if pending:
        findings.append(
            f"mutation.json contains {pending} Pending mutant(s) — Stryker generated "
            f"them but did not run them, so this is an incomplete report and cannot "
            f"arm a mutation ratchet")
    if unknown:
        # Loud, because an unrecognised status is silently dropped from BOTH
        # halves of the ratio and would move the score without moving the tests.
        findings.append(
            f"mutation.json carries mutant status(es) this parser does not know "
            f"({', '.join(sorted(unknown))}) — they are counted in neither half "
            f"of the score, so the number below is about fewer mutants than were run")
    if findings:
        return None, findings
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


def validate_measurement(rec, expected_tool=None):
    required = {
        "tool", "toolVersion", "score", "killed", "survived", "scored",
        "unviable", "timeout", "survivors", "headSha", "when", "note",
    }
    optional = {"acceptedWeaker"}
    if not isinstance(rec, dict):
        return ["stored mutation measurement must be an object"]
    findings = []
    missing = required - set(rec)
    unknown = set(rec) - required - optional
    if missing:
        findings.append(f"stored mutation measurement is missing {', '.join(sorted(missing))}")
    if unknown:
        findings.append(f"stored mutation measurement has unknown field(s) "
                        f"{', '.join(sorted(unknown))}")

    tool = rec.get("tool")
    if tool not in SUPPORTED:
        findings.append(f"stored mutation tool must be one of {sorted(SUPPORTED)}")
    elif expected_tool and tool != expected_tool:
        findings.append(f"stored mutation tool is {tool}, but {CONFIG} declares "
                        f"{expected_tool}")
    expected_version = {
        "cargo-mutants": CARGO_MUTANTS_TESTED,
        "stryker": f"schema {STRYKER_SCHEMA}",
    }.get(tool)
    if expected_version and _ver_tuple(rec.get("toolVersion")) < _ver_tuple(expected_version):
        findings.append(f"stored mutation toolVersion must be {expected_version} or newer")

    count_names = ("killed", "survived", "scored", "unviable", "timeout")
    counts_valid = True
    for name in count_names:
        value = rec.get(name)
        if type(value) is not int or value < 0:
            findings.append(f"stored mutation {name} must be a non-negative integer")
            counts_valid = False
    score = rec.get("score")
    if type(score) is not float or not math.isfinite(score) or not 0.0 <= score <= 1.0:
        findings.append("stored mutation score must be a floating-point number from 0 to 1")
        score_valid = False
    else:
        score_valid = True
    if counts_valid:
        if rec["scored"] <= 0:
            findings.append("stored mutation scored count must be positive")
        elif rec["scored"] != rec["killed"] + rec["survived"] + rec["timeout"]:
            findings.append("stored mutation counts do not reconcile: scored must equal "
                            "killed + survived + timeout")
        elif score_valid:
            expected_score = round((rec["killed"] + rec["timeout"]) / rec["scored"], 4)
            if score != expected_score:
                findings.append(f"stored mutation score must reconcile to {expected_score}")

    survivors = rec.get("survivors")
    if not isinstance(survivors, list):
        findings.append("stored mutation survivors must be a list")
    else:
        if type(rec.get("survived")) is int \
           and len(survivors) != min(rec["survived"], 50):
            findings.append("stored mutation survivor details must equal survived count, "
                            "capped at 50")
        for index, survivor in enumerate(survivors):
            if not isinstance(survivor, dict) or set(survivor) != {"file", "line", "what"}:
                findings.append(f"stored mutation survivor {index} needs exactly file, line, what")
                continue
            if not isinstance(survivor["file"], str) or not survivor["file"].strip():
                findings.append(f"stored mutation survivor {index} file must be non-empty text")
            if type(survivor["line"]) is not int or survivor["line"] < 0:
                findings.append(f"stored mutation survivor {index} line must be a non-negative integer")
            if not isinstance(survivor["what"], str) or not survivor["what"].strip():
                findings.append(f"stored mutation survivor {index} what must be non-empty text")

    head = rec.get("headSha")
    if not isinstance(head, str) or not FULL_COMMIT.fullmatch(head):
        findings.append("stored mutation headSha must be a full lowercase Git commit")
    measured_at = rec.get("when")
    try:
        parsed_at = datetime.datetime.strptime(measured_at, "%Y-%m-%dT%H:%M:%SZ")
        if parsed_at.strftime("%Y-%m-%dT%H:%M:%SZ") != measured_at:
            raise ValueError
    except (TypeError, ValueError):
        findings.append("stored mutation when must be an exact UTC second timestamp")
    if rec.get("note") != MEASUREMENT_NOTE and rec.get("note") not in PRIOR_MEASUREMENT_NOTES:
        findings.append("stored mutation ratchet note is missing or changed")

    if "acceptedWeaker" in rec:
        accepted = rec["acceptedWeaker"]
        if not isinstance(accepted, dict) or set(accepted) != {"reason", "from"}:
            findings.append("stored mutation acceptedWeaker needs exactly reason and from")
        else:
            reason = accepted["reason"]
            origin = accepted["from"]
            if not isinstance(reason, str) or reason != reason.strip() or len(reason) < 20:
                findings.append("stored mutation acceptance reason must have at least 20 characters")
            origin_fields = {"score", "killed", "survived", "scored", "timeout"}
            if not isinstance(origin, dict) or set(origin) != origin_fields:
                findings.append("stored mutation acceptance origin needs exactly score, "
                                "killed, survived, scored, and timeout")
            else:
                origin_score_valid = (type(origin["score"]) is float
                                      and math.isfinite(origin["score"])
                                      and 0.0 <= origin["score"] <= 1.0
                                      and origin["score"] == round(origin["score"], 4))
                if not origin_score_valid:
                    findings.append("stored mutation acceptance origin score is invalid")
                origin_counts_valid = True
                for name in ("killed", "survived", "scored", "timeout"):
                    if type(origin[name]) is not int or origin[name] < 0:
                        findings.append(f"stored mutation acceptance origin {name} "
                                        "must be a non-negative integer")
                        origin_counts_valid = False
                if origin_counts_valid:
                    if origin["scored"] <= 0 or origin["scored"] != (
                            origin["killed"] + origin["survived"] + origin["timeout"]):
                        findings.append("stored mutation acceptance origin counts do not reconcile")
                        origin_counts_valid = False
                    elif origin_score_valid:
                        expected_origin_score = round(
                            (origin["killed"] + origin["timeout"]) / origin["scored"], 4)
                        if origin["score"] != expected_origin_score:
                            findings.append("stored mutation acceptance origin score does not "
                                            "reconcile to its counts")
                            origin_score_valid = False
                current_counts_valid = (counts_valid and rec.get("scored", 0) > 0
                                        and rec.get("scored") == rec.get("killed", -1)
                                        + rec.get("survived", -1) + rec.get("timeout", -1))
                if origin_score_valid and origin_counts_valid and current_counts_valid \
                   and not any(_ratchet_dimensions(rec, origin)):
                    findings.append("stored mutation acceptance does not describe a weaker floor")
    return findings


def validate_baseline(doc, expected_tool=None):
    if not isinstance(doc, dict):
        return [f"{BASELINE} must be an object"]
    if "mutation" not in doc:
        return []
    findings = []
    if type(doc.get("version")) is not int or doc["version"] != 1:
        findings.append(f"{BASELINE} version must be exactly 1 when mutation is recorded")
    return findings + validate_measurement(doc["mutation"], expected_tool)


# ------------------------------------------------------------------ commands
def measure(root, cfg, accept, reason, src=None, source_head=None):
    baseline = load_baseline(root)
    prior_findings = validate_baseline(baseline, cfg.get("tool"))
    for finding in prior_findings:
        print(f"mutation-guard: RED — {finding}", file=sys.stderr)
    if prior_findings:
        return 1
    prior = baseline.get("mutation")
    prior = prior or {}
    start_head, tree_error = clean_measurement_head(root, cfg, src)
    if tree_error:
        print(f"mutation-guard: {tree_error} — nothing was recorded", file=sys.stderr)
        return 1
    if src:
        if not isinstance(source_head, str) or not FULL_COMMIT.fullmatch(source_head):
            print("mutation-guard: --from requires --from-head with the full producer "
                  "HEAD so a historical report cannot be stamped onto this commit",
                  file=sys.stderr)
            return 1
        if source_head != start_head:
            print(f"mutation-guard: the external report's producer HEAD "
                  f"({source_head[:8]}) is not the clean HEAD being recorded "
                  f"({start_head[:8]}) — nothing was recorded", file=sys.stderr)
            return 1
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
        wd = cfg.get("_resolvedWorkDir")
        rec, findings = (run_stryker(root, extra, wd) if cfg["tool"] == "stryker"
                         else run_cargo_mutants(root, extra))
    for f in findings:
        print(f"mutation-guard: {f}", file=sys.stderr)
    if rec is None:
        if not findings:
            print("mutation-guard: the run produced no scored mutants — "
                  "nothing was measured", file=sys.stderr)
        return 1

    end_head, tree_error = clean_measurement_head(root, cfg, src)
    if tree_error:
        print(f"mutation-guard: the measurement changed its own input: {tree_error} "
              f"— nothing was recorded", file=sys.stderr)
        return 1
    if end_head != start_head:
        print("mutation-guard: Git HEAD moved while mutation tests ran — refusing "
              "to bind their result to a different commit", file=sys.stderr)
        return 1
    rec["headSha"] = start_head
    rec["when"] = now()
    rec["note"] = MEASUREMENT_NOTE

    if prior:
        fell, rose, shrank = _ratchet_dimensions(rec, prior)
        if (fell or rose or shrank) and not accept:
            print("\nmutation-guard: RED — the suite got weaker\n", file=sys.stderr)
            if fell:
                prior_detected = prior["killed"] + prior["timeout"]
                current_detected = rec["killed"] + rec["timeout"]
                print(f"  score:     {prior.get('score')} "
                      f"({prior_detected}/{prior['scored']}) -> {rec['score']} "
                      f"({current_detected}/{rec['scored']}; exact ratio fell)",
                      file=sys.stderr)
            if rose:
                print(f"  survivors: {prior.get('survived')} -> {rec['survived']} "
                      f"(a ratio can hold flat while coverage shrinks, so the "
                      f"absolute count is ratcheted too)", file=sys.stderr)
            if shrank:
                print(f"  scored:    {prior.get('scored')} -> {rec['scored']} "
                      f"(a smaller mutation surface needs explicit review)",
                      file=sys.stderr)
            for s in rec["survivors"][:8]:
                print(f"      {s['file']}:{s['line']}  {s['what']}", file=sys.stderr)
            print("\n  Every survivor is a change to your code that no test "
                  "noticed.\n  Kill them, or re-record deliberately:\n"
                  "    mutation-guard.py --measure --accept --reason \"...\"",
                  file=sys.stderr)
            return 1
        if accept and (fell or rose or shrank):
            if len((reason or "").strip()) < 20:
                print("mutation-guard: --accept needs a --reason of at least 20 "
                      "characters. Lowering this floor is the one move that "
                      "weakens the ratchet.", file=sys.stderr)
                return 1
            rec["acceptedWeaker"] = {"reason": reason.strip(),
                                     "from": {"score": prior.get("score"),
                                              "killed": prior.get("killed"),
                                              "survived": prior.get("survived"),
                                              "scored": prior.get("scored"),
                                              "timeout": prior.get("timeout")}}

    candidate_findings = validate_measurement(rec, cfg.get("tool"))
    for finding in candidate_findings:
        print(f"mutation-guard: refusing an invalid measurement — {finding}",
              file=sys.stderr)
    if candidate_findings:
        return 1
    p = write_measurement(root, rec, baseline)
    verdict = "stronger" if prior and _ratio_rose(rec, prior) else "recorded"
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
    if not isinstance(sha, str) or not FULL_COMMIT.fullmatch(sha):
        return -2, None
    exists_ok, object_type = _git_status(root, "cat-file", "-t", sha)
    if not exists_ok or object_type != "commit":
        return -1, None
    ancestor_ok, _ = _git_status(root, "merge-base", "--is-ancestor", sha, "HEAD")
    if not ancestor_ok:
        return -3, None
    rng = f"{sha}..HEAD"
    behind_ok, behind = _git_status(root, "rev-list", "--count", rng)
    names_ok, names = _git_status(root, "diff", "--name-only", rng)
    if not behind_ok or not names_ok:
        return -2, None
    files = [n for n in names.splitlines() if n.strip()]
    try:
        return int(behind), files
    except ValueError:
        return -2, None


def check(root):
    cfg, findings = load_config(root)
    for f in findings:
        print(f"mutation-guard: {f}", file=sys.stderr)
    if findings:
        return 1
    if cfg is None:
        print("mutation-guard: no .fluxpoint-mutation.json — dormant")
        return 0

    baseline = load_baseline(root)
    baseline_findings = validate_baseline(baseline, cfg.get("tool"))
    for finding in baseline_findings:
        print(f"mutation-guard: RED — {finding}", file=sys.stderr)
    if baseline_findings:
        return 1
    if "mutation" not in baseline:
        print(f"mutation-guard: {CONFIG} declares {cfg['tool']} but nothing has "
              f"been measured, so the ratchet is NOT armed. Run: "
              f"mutation-guard.py --measure", file=sys.stderr)
        return 0  # arming is a deliberate step; bootstrapping must not block
    rec = baseline["mutation"]

    behind, changed = staleness(root, rec)
    fresh = f"score {rec.get('score')}, {rec.get('survived')} survivor(s)"
    if behind == -2:
        print("mutation-guard: RED — the recorded Git HEAD is malformed or Git "
              "could not compare it to this tree. Re-measure.", file=sys.stderr)
        return 1
    if behind == -3:
        print("mutation-guard: RED — the measured commit is not an ancestor of "
              "HEAD, or Git could not prove that lineage. Re-measure on this "
              "branch.", file=sys.stderr)
        return 1
    if behind == -1:
        print(f"mutation-guard: measured at a commit this repo no longer has "
              f"({rec.get('headSha', '')[:8]}) — {fresh}, but it cannot be "
              f"related to this tree. Re-measure.", file=sys.stderr)
        return 1

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
    if findings:
        return 1
    baseline = load_baseline(root)
    baseline_findings = validate_baseline(
        baseline, cfg.get("tool") if cfg else None)
    for finding in baseline_findings:
        print(f"mutation-guard: RED — {finding}", file=sys.stderr)
    if baseline_findings:
        return 1
    if "mutation" not in baseline:
        print("mutation-guard: nothing measured yet")
        return 0
    rec = baseline["mutation"]
    behind, changed = staleness(root, rec)
    if behind == -3:
        print("mutation-guard: the measured commit is not an ancestor of HEAD, "
              "or Git could not prove that lineage. Re-measure on this branch.",
              file=sys.stderr)
        return 1
    if behind in {-2, -1}:
        print("mutation-guard: the recorded Git HEAD is malformed, unavailable, "
              "or cannot be compared to this tree. Re-measure.", file=sys.stderr)
        return 1
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
    ap.add_argument("--from-head",
                    help="full Git HEAD of the job that produced --from; it must "
                         "equal the clean HEAD being recorded")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--measure", action="store_true")
    g.add_argument("--check", action="store_true")
    g.add_argument("--report", action="store_true")
    a = ap.parse_args()

    if a.src and not a.measure:
        print("mutation-guard: --from is valid only with --measure", file=sys.stderr)
        return 1
    if a.from_head and not a.src:
        print("mutation-guard: --from-head requires --from", file=sys.stderr)
        return 1

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
    return measure(a.root, cfg, a.accept, a.reason, a.src, a.from_head)


if __name__ == "__main__":
    sys.exit(main())
