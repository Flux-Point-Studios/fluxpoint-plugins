#!/usr/bin/env bash
# Mutation score: the judge of the judge.
#
# Every other gate asks whether the tests pass; this one asks whether they
# can fail. The cases below are weighted toward the two failures that would
# make it useless: a ratchet that lets the suite quietly get weaker, and a
# gate that goes red because an expensive job has not been re-run — the
# second being how a gate gets switched off, taking the harness with it.
#
# cargo-mutants is not installed here and would take minutes if it were, so
# measurements are fed through `--from`, which is the same path a CI job
# takes when it ran the tool in its own step.
set -uo pipefail

if [ -z "${FPL_PY:-}" ]; then
  # Probe each candidate by RUNNING it, rather than asking whether the name
  # exists. Windows ships a `python3` App Execution Alias that is on PATH by
  # default on a machine with no python3 at all: it satisfies `command -v`,
  # then prints "Python was not found" and exits 49 for every argument.
  for _fpl_cand in python3 python; do
    if command -v "$_fpl_cand" >/dev/null 2>&1 &&
       "$_fpl_cand" -c "import sys" >/dev/null 2>&1; then
      FPL_PY="$_fpl_cand"
      break
    fi
  done
  unset _fpl_cand
  if [ -z "${FPL_PY:-}" ]; then
    echo "fluxpoint: no working python interpreter on PATH" >&2
    exit 127
  fi
fi
export PYTHONIOENCODING=utf-8

PLUGIN="$(cd "$(dirname "$0")/.." && pwd)"
MG="$PLUGIN/scripts/mutation-guard.py"
PG="$PLUGIN/scripts/proof-guard.py"
INBOX="$PLUGIN/scripts/inbox.py"
ROOT="$(mktemp -d)"
R="$ROOT/r"
pass=0; fail=0

ok()  { printf 'PASS  %-58s -> %s\n' "$1" "$2"; pass=$((pass+1)); }
bad() { printf 'FAIL  %-58s -> %s\n' "$1" "$2"; fail=$((fail+1)); }
check(){ [ "$2" = "$3" ] && ok "$1" "$3" || bad "$1" "$3 (wanted $2)"; }

mg() {
  local args=("$@") arg has_from=0 has_from_head=0
  for arg in "${args[@]}"; do
    [ "$arg" = "--from" ] && has_from=1
    [ "$arg" = "--from-head" ] && has_from_head=1
  done
  if [ "$has_from" -eq 1 ] && [ "$has_from_head" -eq 0 ]; then
    args+=(--from-head "$(git -C "$R" rev-parse HEAD)")
  fi
  "$FPL_PY" "$MG" --root "$R" "${args[@]}"
}
rc_of() { mg "$@" >/dev/null 2>&1; echo $?; }

mkrepo() {
  rm -rf "$R"; mkdir -p "$R/src"; cd "$R" || exit 1
  git init -q -b main
  printf 'fn a() {}\n' >src/lib.rs
  printf '{"version":1,"tool":"cargo-mutants"}' >"$R/.fluxpoint-mutation.json"
  git add -A; git -c user.email=t@t -c user.name=t commit -qm base
}
commit() { git -C "$R" add -A; git -C "$R" -c user.email=t@t -c user.name=t commit -qm "${1:-x}"; }

# Shaped exactly like cargo-mutants 27.1.0's own outcomes.json, verified
# against a real run of the tool: top-level counters plus an `outcomes`
# array that INCLUDES the baseline entry, whose `scenario` is the bare
# string "Baseline" while every mutant's is an object {"Mutant": {...}}.
# A parser assuming that array is uniform, or that it is one-per-mutant,
# is wrong on both counts — which is why the fixture carries the baseline.
outcomes() { # $1 = caught, $2 = missed, $3 = unviable, $4 = timeout
  "$FPL_PY" - "$1" "$2" "$3" "${4:-0}" >"$R/out.json" <<'PY'
import json, sys
caught, missed, unviable, timeout = (int(x) for x in sys.argv[1:5])
o = [{"scenario": "Baseline", "summary": "Success",
      "phase_results": [{"phase": "Test", "process_status": "Success"}]}]


def mutant(line, summary, what):
    return {"scenario": {"Mutant": {
                "name": f"src/lib.rs:{line}:5: {what}",
                "file": "src/lib.rs",
                "function": {"function_name": "a", "return_type": "",
                             "span": {"start": {"line": 1, "column": 1},
                                      "end": {"line": 9, "column": 2}}},
                "span": {"start": {"line": line, "column": 5},
                         "end": {"line": line, "column": 9}},
                "replacement": "()", "genre": "FnValue"}},
            "summary": summary,
            "log_path": f"log/src__lib.rs_line_{line}.log"}


for i in range(caught):
    o.append(mutant(i + 1, "CaughtMutant", "replace a with ()"))
for i in range(missed):
    o.append(mutant(100 + i, "MissedMutant", "replace a with ()"))
for i in range(unviable):
    o.append(mutant(200 + i, "Unviable", "replace a with Default::default()"))
for i in range(timeout):
    o.append(mutant(300 + i, "Timeout", "replace a with loop {}"))
json.dump({"outcomes": o,
           "total_mutants": caught + missed + unviable + timeout,
           "caught": caught, "missed": missed, "timeout": timeout,
           "unviable": unviable, "success": 0,
           "start_time": "2026-08-29T00:00:00Z",
           "end_time": "2026-08-29T00:00:01Z",
           "cargo_mutants_version": "27.1.0"},
          sys.stdout)
PY
}

# ================= 1. the states it must tell apart ======================
mkrepo; rm "$R/.fluxpoint-mutation.json"
out="$(mg --check)"
check "no config: dormant and green" 0 "$?"
case "$out" in *dormant*) ok "and says so rather than passing in silence" "said" ;;
  *) bad "and says so rather than passing in silence" "${out:0:40}" ;; esac

mkrepo
out="$(mg --check 2>&1)"
check "declared but never measured: not armed, still green" 0 "$?"
case "$out" in *"NOT armed"*) ok "and names the unarmed state" "named" ;;
  *) bad "and names the unarmed state" "${out:0:40}" ;; esac

mkrepo
printf '{"version":1,"tool":"mutmut"}' >"$R/.fluxpoint-mutation.json"
out="$(mg --check 2>&1)"
check "a real tool this guard cannot parse is refused" 1 "$?"
case "$out" in *"does not parse it yet"*)
  ok "and is named rather than silently skipped" "named" ;;
  *) bad "and is named rather than silently skipped" "${out:0:40}" ;; esac

mkrepo
printf '{"version":1,"tool":"cargo-mutants","typo":1}' >"$R/.fluxpoint-mutation.json"
check "an unknown config field is fatal" 1 "$(rc_of --check)"
printf 'not json' >"$R/.fluxpoint-mutation.json"
out="$(mg --check 2>&1)"
case "$out" in *DISARMED*) ok "a malformed config says it is disarmed" "said" ;;
  *) bad "a malformed config says it is disarmed" "${out:0:40}" ;; esac

# ================= 2. recording, and what is scored ======================
mkrepo
outcomes 8 2 0
"$FPL_PY" "$MG" --root "$R" --measure --from "$R/out.json" \
  >/dev/null 2>&1
check "an external report without producer-HEAD provenance is refused" 1 "$?"

mkrepo
old_head="$(git -C "$R" rev-parse HEAD)"
printf 'fn newer_head() {}\n' >>"$R/src/lib.rs"
commit newer
outcomes 8 2 0
out="$("$FPL_PY" "$MG" --root "$R" --measure --from "$R/out.json" \
  --from-head "$old_head" 2>&1)"
check "an external report attributed to an older HEAD is refused" 1 "$?"
case "$out" in *"producer HEAD"*) ok "and the provenance mismatch is named" "named" ;;
  *) bad "and the provenance mismatch is named" "${out:0:60}" ;; esac

mkrepo
outcomes 8 2 5
mg --measure --from "$R/out.json" >/dev/null 2>&1
check "a measurement records" 0 "$?"
score="$("$FPL_PY" -c '
import json,sys; print(json.load(open(sys.argv[1]))["mutation"]["score"])' \
  "$R/.fluxpoint-proof-baseline.json")"
check "unviable mutants are not scored (8/10, not 8/15)" "0.8" "$score"
line="$("$FPL_PY" -c '
import json,sys; print(json.load(open(sys.argv[1]))["mutation"]["survivors"][0]["line"])' \
  "$R/.fluxpoint-proof-baseline.json")"
check "a survivor's line comes from the mutated span, not the function" 100 "$line"

# A timeout means the suite did not silently pass the mutant, so it counts
# as detected — the convention Stryker publishes — but it is kept separate
# because a timeout usually means the limit is wrong, not that a test bit.
mkrepo
outcomes 8 2 0 2
mg --measure --from "$R/out.json" >/dev/null 2>&1
score="$("$FPL_PY" -c '
import json,sys
d = json.load(open(sys.argv[1]))["mutation"]
print(d["score"], d["timeout"])' "$R/.fluxpoint-proof-baseline.json")"
check "a timeout counts as detected and is recorded separately" "0.8333 2" "$score"

mkrepo
outcomes 1 60 0
check "survivor-heavy measurements record with bounded detail" 0 \
  "$(rc_of --measure --from "$R/out.json")"
check "stored survivor detail is capped at 50" 50 \
  "$($FPL_PY -c 'import json,sys; print(len(json.load(open(sys.argv[1]))["mutation"]["survivors"]))' \
     "$R/.fluxpoint-proof-baseline.json")"
"$FPL_PY" - "$R/.fluxpoint-proof-baseline.json" <<'PY'
import json, sys

path = sys.argv[1]
with open(path, encoding="utf-8") as fh:
    document = json.load(fh)
document["mutation"]["survivors"].pop()
with open(path, "w", encoding="utf-8", newline="\n") as fh:
    json.dump(document, fh)
    fh.write("\n")
PY
check "a truncated survivor-detail list is RED" 1 "$(rc_of --check)"

# ---- the three shapes that would publish a fake number ------------------
# The baseline failing writes a real outcomes.json full of zeroes. Reading
# those as a clean sweep would report a perfect score for a broken build.
mkrepo
printf '{"outcomes":[{"scenario":"Baseline","summary":"Failure"}],"total_mutants":0,"caught":0,"missed":0,"timeout":0,"unviable":0,"success":0,"cargo_mutants_version":"27.1.0"}' >"$R/out.json"
out="$(mg --measure --from "$R/out.json" 2>&1)"
check "a failed baseline is not a clean sweep" 1 "$?"
case "$out" in *"baseline run failed"*) ok "and says the build never ran" "said" ;;
  *) bad "and says the build never ran" "${out:0:44}" ;; esac

# `cargo mutants --check` only compiles mutants: every one is Success, and
# the tool exits 0. A gate trusting that reports a green run that measured
# nothing.
mkrepo
printf '{"outcomes":[{"scenario":"Baseline","summary":"Success"}],"total_mutants":5,"caught":0,"missed":0,"timeout":0,"unviable":0,"success":5,"cargo_mutants_version":"27.1.0"}' >"$R/out.json"
out="$(mg --measure --from "$R/out.json" 2>&1)"
check "a --check run cannot produce a score" 1 "$?"
case "$out" in *"--check"*) ok "and names why" "named" ;;
  *) bad "and names why" "${out:0:44}" ;; esac

# The schema has churned across releases; an older one is refused rather
# than misread.
mkrepo
outcomes 8 2 0
"$FPL_PY" - "$R/out.json" <<'PY'
import json, sys
p = sys.argv[1]; d = json.load(open(p))
d["cargo_mutants_version"] = "25.0.0"
json.dump(d, open(p, "w"))
PY
out="$(mg --measure --from "$R/out.json" 2>&1)"
check "an untested cargo-mutants version is refused" 1 "$?"
case "$out" in *"older schema"*) ok "and says why rather than guessing" "said" ;;
  *) bad "and says why rather than guessing" "${out:0:44}" ;; esac

mkrepo
outcomes 8 2 0
"$FPL_PY" - "$R/out.json" <<'PY'
import json, sys
p = sys.argv[1]
d = json.load(open(p))
d.pop("cargo_mutants_version")
json.dump(d, open(p, "w"))
PY
check "a cargo-mutants report with no version is refused" 1 \
  "$(rc_of --measure --from "$R/out.json")"

mkrepo
outcomes 8 2 0
"$FPL_PY" - "$R/out.json" <<'PY'
import json, sys
p = sys.argv[1]
d = json.load(open(p))
d["cargo_mutants_version"] = "99.0.0"
json.dump(d, open(p, "w"))
PY
check "a newer cargo-mutants version is accepted" 0 \
  "$(rc_of --measure --from "$R/out.json")"

mkrepo
outcomes 8 2 0
"$FPL_PY" - "$R/out.json" <<'PY'
import json, sys
p = sys.argv[1]
d = json.load(open(p))
d["end_time"] = None
json.dump(d, open(p, "w"))
PY
check "an unfinished cargo-mutants report is refused" 1 \
  "$(rc_of --measure --from "$R/out.json")"

mkrepo
outcomes 8 2 0
"$FPL_PY" - "$R/out.json" <<'PY'
import json, sys
p = sys.argv[1]
d = json.load(open(p))
d["caught"] = True
json.dump(d, open(p, "w"))
PY
check "a boolean cargo-mutants counter is refused" 1 \
  "$(rc_of --measure --from "$R/out.json")"

mkrepo
outcomes 8 2 0
"$FPL_PY" - "$R/out.json" <<'PY'
import json, sys
p = sys.argv[1]
d = json.load(open(p))
d["missed"] = -1
json.dump(d, open(p, "w"))
PY
check "a negative cargo-mutants counter is refused" 1 \
  "$(rc_of --measure --from "$R/out.json")"

mkrepo
outcomes 8 2 0
"$FPL_PY" - "$R/out.json" <<'PY'
import json, sys
p = sys.argv[1]
d = json.load(open(p))
d["total_mutants"] += 1
json.dump(d, open(p, "w"))
PY
check "inconsistent cargo-mutants arithmetic is refused" 1 \
  "$(rc_of --measure --from "$R/out.json")"

# The shared baseline file must survive its siblings: proof-guard owns
# `counts`, spec-guard owns `spec`, this owns `mutation`, and whichever
# writes last must not disarm the others.
mkrepo
outcomes 8 2 0
mg --measure --from "$R/out.json" >/dev/null 2>&1
"$FPL_PY" "$PG" --root "$R" --baseline >/dev/null 2>&1
has() { "$FPL_PY" -c '
import json,sys; print("yes" if sys.argv[2] in json.load(open(sys.argv[1])) else "no")' \
  "$R/.fluxpoint-proof-baseline.json" "$1"; }
check "re-arming proof-guard preserves the mutation section" "yes" "$(has mutation)"
outcomes 8 2 5
mg --measure --from "$R/out.json" >/dev/null 2>&1
check "and re-measuring preserves proof-guard's counts" "yes" "$(has counts)"

mkrepo
outcomes 8 2 0
head_probe="$($FPL_PY - "$MG" "$R" "$R/out.json" <<'PY'
import contextlib, importlib.util, io, json, os, sys
spec = importlib.util.spec_from_file_location("mutation_guard", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.git = lambda *args: ""
sink = io.StringIO()
with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
    rc = module.measure(sys.argv[2], {"tool": "cargo-mutants"}, False, "", sys.argv[3])
baseline = os.path.join(sys.argv[2], ".fluxpoint-proof-baseline.json")
print("safe" if rc == 1 and not os.path.exists(baseline) else "unsafe")
PY
)"
check "a measurement without a full trusted HEAD is never recorded" "safe" "$head_probe"

mkrepo
outcomes 8 2 0
printf 'fn only_dirty_tests_know() {}\n' >>"$R/src/lib.rs"
check "unstaged tracked changes cannot influence a recorded measurement" 1 \
  "$(rc_of --measure --from "$R/out.json")"

mkrepo
outcomes 8 2 0
printf 'fn only_staged_tests_know() {}\n' >>"$R/src/lib.rs"
git -C "$R" add src/lib.rs
check "staged tracked changes cannot influence a recorded measurement" 1 \
  "$(rc_of --measure --from "$R/out.json")"

mkrepo
outcomes 8 2 0
mkdir -p "$R/tests"
printf 'fn test_visible_only_to_the_runner() {}\n' >"$R/tests/untracked.rs"
check "untracked source or tests cannot influence a recorded measurement" 1 \
  "$(rc_of --measure --from "$R/out.json")"

mkrepo
outcomes 8 2 0
mkdir -p "$R/ mutants.out/tests"
printf 'test("leading-space bypass", () => {});\n' \
  >"$R/ mutants.out/tests/extra.test.js"
check "leading whitespace cannot disguise an untracked path as generated output" 1 \
  "$(rc_of --measure --from "$R/out.json")"

raw_filename_probe="$($FPL_PY - "$MG" <<'PY'
import importlib.util, json, os, sys

spec = importlib.util.spec_from_file_location("mutation_guard", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
head = "a" * 40
module._inside = lambda *_args: True
module._safe_path_entries = lambda _root: [os.path.dirname(sys.executable)]
module._find_executable = lambda *_args: sys.executable
original_sep = module.os.sep
module.os.sep = "/"


def refused(payload):
    class Result:
        returncode = 0
        stderr = ""

        def __init__(self, stdout):
            self.stdout = stdout

    def run(command, **kwargs):
        if "rev-parse" in command:
            output = head + "\n"
        elif "ls-files" in command:
            output = payload
        else:
            output = ""
        if kwargs.get("text") is False:
            output = output.encode()
        return Result(output)

    module.subprocess.run = run
    measured, finding = module.clean_measurement_head(
        "/repo", {"tool": "cargo-mutants"})
    return measured is None and "untracked" in finding


try:
    result = {
        "backslash": refused("mutants.out\\extra.test.js\0"),
        "whitespace": refused(" \0"),
    }
finally:
    module.os.sep = original_sep
print(json.dumps(result))
PY
)"
raw_filename_field() {
  printf '%s' "$raw_filename_probe" | "$FPL_PY" -c \
    'import json,sys; print("safe" if json.load(sys.stdin)[sys.argv[1]] else "unsafe")' "$1"
}
check "a POSIX backslash filename cannot impersonate an output-directory child" \
  "safe" "$(raw_filename_field backslash)"
check "a whitespace-only untracked filename is never discarded" "safe" \
  "$(raw_filename_field whitespace)"

mkrepo
outcomes 8 2 0
mkdir -p "$ROOT/outside-generated"
if "$FPL_PY" - "$R/mutants.out" "$ROOT/outside-generated" <<'PY'
import os, subprocess, sys
link, target = map(os.path.abspath, sys.argv[1:3])
if os.name == "nt":
    result = subprocess.run(
        [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", "mklink", "/J",
         link, target],
        capture_output=True,
    )
    raise SystemExit(result.returncode)
os.symlink(target, link, target_is_directory=True)
PY
then
  check "a generated-output junction cannot escape the repository" 1 \
    "$(rc_of --measure --from "$R/out.json")"
  "$FPL_PY" - "$R/mutants.out" <<'PY'
import os, sys
if os.path.islink(sys.argv[1]):
    os.unlink(sys.argv[1])
else:
    os.rmdir(sys.argv[1])
PY
else
  bad "a generated-output junction cannot escape the repository" "fixture unavailable"
fi

mkrepo
outcomes 8 2 0
generated_output_probe="$($FPL_PY - "$MG" "$R" <<'PY'
import contextlib, importlib.util, io, json, os, sys

spec = importlib.util.spec_from_file_location("mutation_guard", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
root = sys.argv[2]
source = os.path.join(root, "out.json")
with open(source, encoding="utf-8") as fh:
    document = json.load(fh)
measurement, findings = module.parse_cargo_mutants(document)
assert measurement is not None and not findings
os.unlink(source)


def runner(*_args):
    report = os.path.join(root, "mutants.out", "outcomes.json")
    os.makedirs(os.path.dirname(report), exist_ok=True)
    with open(report, "w", encoding="utf-8") as fh:
        json.dump(document, fh)
    return measurement, []


module.run_cargo_mutants = runner
sink = io.StringIO()
with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
    rc = module.measure(root, {"tool": "cargo-mutants"}, False, "")
baseline = os.path.join(root, ".fluxpoint-proof-baseline.json")
print("safe" if rc == 0 and os.path.exists(baseline) else "unsafe")
PY
)"
check "the exact cargo-mutants output prefix remains permitted" "safe" \
  "$generated_output_probe"

mkrepo
outcomes 8 2 0
post_untracked_probe="$($FPL_PY - "$MG" "$R" <<'PY'
import contextlib, importlib.util, io, json, os, sys

spec = importlib.util.spec_from_file_location("mutation_guard", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
root = sys.argv[2]
source = os.path.join(root, "out.json")
with open(source, encoding="utf-8") as fh:
    measurement, findings = module.parse_cargo_mutants(json.load(fh))
assert measurement is not None and not findings
os.unlink(source)


def runner(*_args):
    path = os.path.join(root, "tests", "created-during-run.rs")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("fn visible_only_after_the_run() {}\n")
    return measurement, []


module.run_cargo_mutants = runner
sink = io.StringIO()
with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
    rc = module.measure(root, {"tool": "cargo-mutants"}, False, "")
baseline = os.path.join(root, ".fluxpoint-proof-baseline.json")
print("safe" if rc == 1 and not os.path.exists(baseline) else "unsafe")
PY
)"
check "an untracked input created during the runner prevents recording" "safe" \
  "$post_untracked_probe"

post_measure_probe() {
  "$FPL_PY" - "$MG" "$R" "$1" <<'PY'
import contextlib, importlib.util, io, json, os, subprocess, sys

spec = importlib.util.spec_from_file_location("mutation_guard", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
root, mode = sys.argv[2:4]
with open(os.path.join(root, "out.json"), encoding="utf-8") as fh:
    measurement, findings = module.parse_cargo_mutants(json.load(fh))
assert not findings


def runner(*_args):
    with open(os.path.join(root, "src", "lib.rs"), "a", encoding="utf-8") as fh:
        fh.write("fn changed_during_measurement() {}\n")
    if mode == "commit":
        subprocess.run(["git", "-C", root, "add", "src/lib.rs"], check=True)
        subprocess.run(["git", "-C", root, "-c", "user.name=fixture",
                        "-c", "user.email=fixture@example.test", "commit", "-qm",
                        "runner changed HEAD"], check=True)
    return measurement, []


module.run_cargo_mutants = runner
sink = io.StringIO()
with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
    rc = module.measure(root, {"tool": "cargo-mutants"}, False, "")
baseline = os.path.join(root, ".fluxpoint-proof-baseline.json")
print("safe" if rc == 1 and not os.path.exists(baseline) else "unsafe")
PY
}

mkrepo
outcomes 8 2 0
check "tracked files changed by the runner are refused after measurement" "safe" \
  "$(post_measure_probe dirty)"

mkrepo
outcomes 8 2 0
check "a runner that moves HEAD cannot choose the commit recorded afterwards" "safe" \
  "$(post_measure_probe commit)"

mkrepo
outcomes 8 2 0
candidate_schema_probe="$($FPL_PY - "$MG" "$R" <<'PY'
import contextlib, importlib.util, io, json, os, sys

spec = importlib.util.spec_from_file_location("mutation_guard", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
root = sys.argv[2]
with open(os.path.join(root, "out.json"), encoding="utf-8") as fh:
    measurement, findings = module.parse_cargo_mutants(json.load(fh))
assert not findings
measurement["manualOverride"] = True
module.run_cargo_mutants = lambda *_args: (measurement, [])
sink = io.StringIO()
with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
    rc = module.measure(root, {"tool": "cargo-mutants"}, False, "")
baseline = os.path.join(root, ".fluxpoint-proof-baseline.json")
print("safe" if rc == 1 and not os.path.exists(baseline) else "unsafe")
PY
)"
check "the fully stamped candidate is schema-checked before baseline write" "safe" \
  "$candidate_schema_probe"

# ================= 3. the ratchet, both directions =======================
mkrepo
outcomes 8 2 0
mg --measure --from "$R/out.json" >/dev/null 2>&1
outcomes 9 1 0
check "a stronger suite records without ceremony" 0 \
  "$(rc_of --measure --from "$R/out.json")"

mkrepo
outcomes 9 1 0
mg --measure --from "$R/out.json" >/dev/null 2>&1
outcomes 7 3 0
out="$(mg --measure --from "$R/out.json" 2>&1)"
check "a weaker suite is RED" 1 "$?"
case "$out" in *"score:"*) ok "and the drop is named" "named" ;;
  *) bad "and the drop is named" "${out:0:40}" ;; esac
case "$out" in *"no test noticed"*)
  ok "with the survivors listed as changes nothing caught" "listed" ;;
  *) bad "with the survivors listed as changes nothing caught" "${out:0:40}" ;; esac

# The case a ratio alone cannot see: delete a well-tested module and the
# score holds while absolute coverage shrinks.
mkrepo
outcomes 90 10 0     # score 0.9, 10 survivors
mg --measure --from "$R/out.json" >/dev/null 2>&1
outcomes 180 20 0    # score 0.9 still, but twice the survivors
check "a flat ratio with more survivors is RED" 1 \
  "$(rc_of --measure --from "$R/out.json")"

mkrepo
outcomes 9999 1 0
mg --measure --from "$R/out.json" >/dev/null 2>&1
outcomes 9998 1 0
check "an exact ratio drop hidden by four-decimal display rounding is RED" 1 \
  "$(rc_of --measure --from "$R/out.json")"

mkrepo
outcomes 90 10 0
mg --measure --from "$R/out.json" >/dev/null 2>&1
outcomes 45 5 0
check "a smaller mutation surface is RED even when its ratio holds" 1 \
  "$(rc_of --measure --from "$R/out.json")"

# The release valve costs a written reason.
check "accepting a weaker score without a reason is refused" 1 \
  "$(rc_of --measure --from "$R/out.json" --accept)"
check "with a real one it records" 0 \
  "$(rc_of --measure --from "$R/out.json" --accept --reason \
      "the module was split, so the survivor count is not comparable")"
"$FPL_PY" -c '
import json,sys
d = json.load(open(sys.argv[1]))["mutation"]
print("yes" if d.get("acceptedWeaker",{}).get("reason") else "no")' \
  "$R/.fluxpoint-proof-baseline.json" | grep -q yes \
  && ok "and the reason is kept in the committed record" "kept" \
  || bad "and the reason is kept in the committed record" "lost"

# ================= 4. staleness is loud, not fatal by default ============
mkrepo
outcomes 9 1 0
mg --measure --from "$R/out.json" >/dev/null 2>&1
check "a fresh measurement is green" 0 "$(rc_of --check)"

head_sha="$(git -C "$R" rev-parse HEAD)"
printf '{"version":1,"mutation":{"headSha":"%s"}}\n' "$head_sha" \
  >"$R/.fluxpoint-proof-baseline.json"
check "a hand-written baseline containing only HEAD is RED, never fresh" 1 \
  "$(rc_of --check)"

mkrepo
outcomes 8 2 0
mg --measure --from "$R/out.json" >/dev/null 2>&1
baseline_schema_probe="$($FPL_PY - "$MG" "$R" <<'PY'
import copy, json, os, subprocess, sys

guard, root = sys.argv[1:3]
baseline = os.path.join(root, ".fluxpoint-proof-baseline.json")
with open(baseline, encoding="utf-8") as fh:
    document = json.load(fh)
record = document["mutation"]


def changed(**values):
    candidate = copy.deepcopy(record)
    candidate.update(values)
    return candidate


def origin(**values):
    candidate = {key: record[key] for key in
                 ("score", "killed", "survived", "scored", "timeout")}
    candidate.update(values)
    return candidate


variants = {
    "unknown field": changed(manualOverride=True),
    "wrong tool": changed(tool="stryker"),
    "missing tool version": {key: value for key, value in record.items()
                             if key != "toolVersion"},
    "integer score": changed(score=1),
    "nonfinite score": changed(score=float("nan")),
    "boolean count": changed(killed=True),
    "inconsistent count arithmetic": changed(scored=record["scored"] + 1),
    "malformed survivor": changed(survivors=[{"file": "src/lib.rs"}]),
    "missing survivor details": changed(survivors=[]),
    "malformed timestamp": changed(when="recently"),
    "changed ratchet note": changed(note="trust me"),
    "malformed acceptance": changed(
        acceptedWeaker={"reason": "short", "from": {"score": 1, "survived": 0}}),
    "false acceptance": changed(
        acceptedWeaker={"reason": "the same floor was not actually weaker",
                        "from": origin()}),
    "overprecise acceptance origin": changed(
        acceptedWeaker={"reason": "the old floor carries impossible precision",
                        "from": origin(score=record["score"] + 0.00001)}),
    "inconsistent acceptance origin": changed(
        acceptedWeaker={"reason": "the old floor carries inconsistent counts",
                        "from": origin(scored=record["scored"] + 1)}),
}
failed = []
for name, candidate in variants.items():
    document["mutation"] = candidate
    with open(baseline, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(document, fh)
        fh.write("\n")
    result = subprocess.run([sys.executable, guard, "--root", root, "--check"],
                            capture_output=True, text=True)
    if result.returncode != 1:
        failed.append(name)
print("safe" if not failed else "unsafe:" + ",".join(failed))
PY
)"
check "stored measurements enforce exact schema, types, and arithmetic" "safe" \
  "$baseline_schema_probe"

mkrepo
outcomes 8 2 0
mg --measure --from "$R/out.json" >/dev/null 2>&1
"$FPL_PY" - "$R/.fluxpoint-proof-baseline.json" <<'PY'
import json, sys

path = sys.argv[1]
with open(path, encoding="utf-8") as fh:
    document = json.load(fh)
del document["mutation"]["score"]
with open(path, "w", encoding="utf-8", newline="\n") as fh:
    json.dump(document, fh, sort_keys=True)
    fh.write("\n")
PY
baseline_digest() {
  "$FPL_PY" -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' \
    "$R/.fluxpoint-proof-baseline.json"
}
before_digest="$(baseline_digest)"
check "measure refuses an invalid prior instead of resetting either ratchet" 1 \
  "$(rc_of --measure --from "$R/out.json")"
check "refusing an invalid prior preserves its exact reviewable bytes" "$before_digest" \
  "$(baseline_digest)"
report_out="$(mg --report 2>&1)"; report_rc=$?
check "report rejects a malformed record without crashing" 1 "$report_rc"
case "$report_out" in *Traceback*) bad "and report emits no traceback" "traceback" ;;
  *) ok "and report emits no traceback" "clean error" ;; esac

mkrepo
outcomes 8 2 0
mg --measure --from "$R/out.json" >/dev/null 2>&1
"$FPL_PY" - "$R/.fluxpoint-proof-baseline.json" <<'PY'
import json, sys

path = sys.argv[1]
with open(path, encoding="utf-8") as fh:
    document = json.load(fh)
document["version"] = 2
with open(path, "w", encoding="utf-8", newline="\n") as fh:
    json.dump(document, fh)
    fh.write("\n")
PY
check "a mutation record under an unknown baseline version is RED" 1 "$(rc_of --check)"

mkrepo
outcomes 9 1 0
mg --measure --from "$R/out.json" >/dev/null 2>&1
git_failure_probe="$($FPL_PY - "$MG" "$R" <<'PY'
import contextlib, importlib.util, io, sys
spec = importlib.util.spec_from_file_location("mutation_guard", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module._git_status = lambda *args: (False, "")
sink = io.StringIO()
with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
    rc = module.check(sys.argv[2])
print("safe" if rc == 1 else "unsafe")
PY
)"
check "a later Git comparison failure can never report fresh" "safe" "$git_failure_probe"

mkrepo
git -C "$R" checkout -qb measured
printf 'fn measured_branch() {}\n' >>"$R/src/lib.rs"
commit measured
outcomes 9 1 0
mg --measure --from "$R/out.json" >/dev/null 2>&1
git -C "$R" checkout -q main
printf 'fn divergent_branch() {}\n' >>"$R/src/lib.rs"
printf '{"version":1,"tool":"cargo-mutants"}' >"$R/.fluxpoint-mutation.json"
commit divergent
check "a measurement from a divergent branch is RED, not fresh" 1 "$(rc_of --check)"

mkrepo
outcomes 9 1 0
mg --measure --from "$R/out.json" >/dev/null 2>&1
for i in $(seq 1 25); do printf 'fn f%s() {}\n' "$i" >>"$R/src/lib.rs"; commit "c$i" >/dev/null; done
out="$(mg --check 2>&1)"
check "a stale measurement does not fail the build by default" 0 "$?"
case "$out" in *STALE*) ok "but says how stale, loudly" "named" ;;
  *) bad "but says how stale, loudly" "${out:0:44}" ;; esac
check "and raises it for a person to schedule" 1 \
  "$("$FPL_PY" "$INBOX" --root "$R" --count)"

printf '{"version":1,"tool":"cargo-mutants","failWhenStale":true}' \
  >"$R/.fluxpoint-mutation.json"
check "a repo that asks for staleness to be fatal gets it" 1 "$(rc_of --check)"

printf '{"version":1,"tool":"cargo-mutants","maxStaleCommits":100}' \
  >"$R/.fluxpoint-mutation.json"
check "a repo that widens the window is green again" 0 "$(rc_of --check)"

# A measurement taken at a commit this repo no longer has cannot be related
# to the tree at all, and that is different from merely being old.
mkrepo
outcomes 9 1 0
mg --measure --from "$R/out.json" >/dev/null 2>&1
"$FPL_PY" - "$R/.fluxpoint-proof-baseline.json" <<'PY'
import json, sys
p = sys.argv[1]
d = json.load(open(p))
d["mutation"]["headSha"] = "0" * 40
json.dump(d, open(p, "w"), indent=2, sort_keys=True)
PY
out="$(mg --check 2>&1)"
case "$out" in *"no longer has"*)
  ok "a measurement from a vanished commit is reported" "reported" ;;
  *) bad "a measurement from a vanished commit is reported" "${out:0:44}" ;; esac

"$FPL_PY" - "$R/.fluxpoint-proof-baseline.json" <<'PY'
import json, sys
p = sys.argv[1]
d = json.load(open(p))
d["mutation"]["headSha"] = ""
json.dump(d, open(p, "w"), indent=2, sort_keys=True)
PY
check "an existing measurement with a malformed HEAD is never green" 1 "$(rc_of --check)"

# ================= 5. a format this parser cannot read ===================
# The adapter was written from documentation, not from a run of the real
# tool, so the property that matters most is what happens when the shape is
# wrong: a parser that quietly scored 0 mutants would record a fake number
# and ratchet everything afterwards against it.
mkrepo
printf '{"results":[{"status":"killed"}]}' >"$R/wrong.json"
out="$(mg --measure --from "$R/wrong.json" 2>&1)"
check "an unrecognized outcomes shape fails, not scores zero" 1 "$?"
case "$out" in *"not a shape this parser understands"*)
  ok "and says nothing was measured" "said" ;;
  *) bad "and says nothing was measured" "${out:0:44}" ;; esac
[ -f "$R/.fluxpoint-proof-baseline.json" ] \
  && bad "and records nothing to ratchet against later" "wrote a baseline" \
  || ok "and records nothing to ratchet against later" "untouched"

# ================= 6. excusing a mutant ratchets too =====================
mkrepo
printf '#[mutants::skip]\nfn skipped() {}\n' >>"$R/src/lib.rs"
commit skip >/dev/null
out="$("$FPL_PY" "$PG" --root "$R" --scan 2>&1)"
case "$out" in *mutants_skip*)
  ok "#[mutants::skip] is counted as an escape hatch" "counted" ;;
  *) bad "#[mutants::skip] is counted as an escape hatch" "invisible" ;; esac

cd /; rm -rf "$ROOT"

# --- stryker: JS/TS, parsed from the tool's OWN report -------------------
# The fixture is a REAL Stryker 10 run, captured from a throwaway project with
# one deliberately weak test — not a shape written here. Every number below was
# produced by the tool, so if the parser and the tool ever disagree this is what
# says so. A hand-written fixture would only prove the parser agrees with itself.
HERE_T="$PLUGIN/tests"
FIX="$HERE_T/fixtures/stryker-mutation.json"
probe() { "$FPL_PY" "$HERE_T/stryker-probe.py" "$PLUGIN" "$FIX" "$@"; }
field() { "$FPL_PY" -c 'import json,sys;d=json.load(sys.stdin);print(d["m"][sys.argv[1]] if d["m"] else "NONE")' "$1"; }

mkrepo
outcomes 8 2 0
report_freshness_probe="$($FPL_PY - "$MG" "$R/out.json" "$FIX" "$ROOT" <<'PY'
import importlib.util, json, os, shutil, sys

spec = importlib.util.spec_from_file_location("mutation_guard", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
cargo_fixture, stryker_fixture, base = sys.argv[2:5]
module._safe_path_entries = lambda _root: []
module._find_executable = lambda *_args: sys.executable
module._git_status = lambda *_args: (True, "")


class Result:
    returncode = 2
    stdout = "runner stopped before writing a report"
    stderr = ""


def reset(name):
    root = os.path.join(base, name)
    shutil.rmtree(root, ignore_errors=True)
    os.makedirs(root)
    return root


def copy_report(source, destination):
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    shutil.copyfile(source, destination)


cargo_stale_root = reset("cargo-stale")
cargo_stale = os.path.join(cargo_stale_root, "mutants.out", "outcomes.json")
copy_report(cargo_fixture, cargo_stale)
module.subprocess.run = lambda *_args, **_kwargs: Result()
cargo_rec, _ = module.run_cargo_mutants(cargo_stale_root, [])

cargo_fresh_root = reset("cargo-fresh")
def cargo_fresh_run(*_args, **_kwargs):
    copy_report(cargo_fixture,
                os.path.join(cargo_fresh_root, "mutants.out", "outcomes.json"))
    return Result()
module.subprocess.run = cargo_fresh_run
cargo_fresh, _ = module.run_cargo_mutants(cargo_fresh_root, [])

stryker_stale_root = reset("stryker-stale")
stryker_stale = os.path.join(stryker_stale_root, "reports", "mutation", "mutation.json")
copy_report(stryker_fixture, stryker_stale)
module.subprocess.run = lambda *_args, **_kwargs: Result()
stryker_rec, _ = module.run_stryker(stryker_stale_root, [])

stryker_fresh_root = reset("stryker-fresh")
def stryker_fresh_run(*_args, **_kwargs):
    copy_report(stryker_fixture,
                os.path.join(stryker_fresh_root, "reports", "mutation", "mutation.json"))
    return Result()
module.subprocess.run = stryker_fresh_run
stryker_fresh, _ = module.run_stryker(stryker_fresh_root, [])

print(json.dumps({
    "cargo_stale": cargo_rec is None,
    "cargo_fresh": cargo_fresh is not None,
    "stryker_stale": stryker_rec is None,
    "stryker_fresh": stryker_fresh is not None,
}))
PY
)"
freshness_field() {
  printf '%s' "$report_freshness_probe" | "$FPL_PY" -c \
    'import json,sys; print("safe" if json.load(sys.stdin)[sys.argv[1]] else "unsafe")' "$1"
}
check "cargo: a failed run cannot reuse a pre-existing valid report" "safe" \
  "$(freshness_field cargo_stale)"
check "cargo: a report freshly written by this run remains accepted" "safe" \
  "$(freshness_field cargo_fresh)"
check "stryker: a failed run cannot reuse a pre-existing valid report" "safe" \
  "$(freshness_field stryker_stale)"
check "stryker: a report freshly written by this run remains accepted" "safe" \
  "$(freshness_field stryker_fresh)"

out="$(probe)"
check "stryker: killed is the tool's own count"    8      "$(field killed  <<<"$out")"
check "stryker: survived is the tool's own count"  6      "$(field survived <<<"$out")"
check "stryker: scored excludes nothing here"      14     "$(field scored  <<<"$out")"
check "stryker: score is detected over valid"      0.5714 "$(field score   <<<"$out")"
case "$out" in *EqualityOperator*) ok "stryker: a survivor names its mutator" "named" ;;
  *) bad "stryker: a survivor names its mutator" "${out:0:70}" ;; esac

mkrepo
printf '{"version":1,"tool":"stryker"}' >"$R/.fluxpoint-mutation.json"
commit stryker
check "stryker: the real fixture records under the shared baseline contract" 0 \
  "$(rc_of --measure --from "$FIX")"
check "stryker: its recorded measurement passes the cheap gate" 0 "$(rc_of --check)"

# NoCoverage is UNDETECTED, never excluded. A mutant no test exercises is the
# most important thing this measure reports; folding it in with the ones that
# failed to compile would let a suite raise its score by testing LESS.
check "stryker: NoCoverage counts as survived" 7 "$(probe Killed NoCoverage | field survived)"

# CompileError says nothing about the tests — that mutant never ran — so it
# leaves the ratio rather than lowering it, exactly as cargo-mutants' unviable.
check "stryker: CompileError leaves the ratio"  13 "$(probe Survived CompileError | field scored)"
check "stryker: CompileError is counted aside"  1  "$(probe Survived CompileError | field unviable)"

pending_probe="$($FPL_PY - "$MG" <<'PY'
import importlib.util, sys

spec = importlib.util.spec_from_file_location("mutation_guard", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
doc = {"schemaVersion": "1.0", "files": {"src/lib.js": {"mutants": [
    {"status": "Killed"}, *({"status": "Pending"} for _ in range(1000)),
]}}}
measurement, findings = module.parse_stryker(doc)
safe = measurement is None and any("Pending" in finding for finding in findings)
print("safe" if safe else "unsafe")
PY
)"
check "stryker: generated-but-unrun Pending mutants make the report incomplete" "safe" \
  "$pending_probe"

# An unrecognised status is dropped from BOTH halves, so it would move the score
# without moving the tests. That has to be said out loud.
unk="$(probe Killed Rehydrated)"
case "$unk" in *Rehydrated*) ok "stryker: an unknown status is reported, not silent" "said" ;;
  *) bad "stryker: an unknown status is reported, not silent" "silent" ;; esac

mkrepo
printf '{"version":1,"tool":"stryker"}' >"$R/.fluxpoint-mutation.json"
commit stryker
"$FPL_PY" - "$FIX" "$R/out.json" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as fh:
    doc = json.load(fh)
for entry in doc["files"].values():
    mutants = entry.get("mutants") or []
    if mutants:
        mutants[0]["status"] = "Rehydrated"
        break
with open(sys.argv[2], "w", encoding="utf-8") as fh:
    json.dump(doc, fh)
PY
check "stryker: an unknown status makes measurement RED" 1 \
  "$(rc_of --measure --from "$R/out.json")"

mkrepo
printf '{"version":1,"tool":"stryker"}' >"$R/.fluxpoint-mutation.json"
commit stryker
"$FPL_PY" - "$FIX" "$R/out.json" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as fh:
    doc = json.load(fh)
doc.pop("schemaVersion")
with open(sys.argv[2], "w", encoding="utf-8") as fh:
    json.dump(doc, fh)
PY
check "stryker: a report with no schema version is refused" 1 \
  "$(rc_of --measure --from "$R/out.json")"

mkrepo
printf '{"version":1,"tool":"stryker"}' >"$R/.fluxpoint-mutation.json"
commit stryker
"$FPL_PY" - "$FIX" "$R/out.json" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as fh:
    doc = json.load(fh)
doc["schemaVersion"] = "99.0"
with open(sys.argv[2], "w", encoding="utf-8") as fh:
    json.dump(doc, fh)
PY
check "stryker: an unverified future schema is refused" 1 \
  "$(rc_of --measure --from "$R/out.json")"


# --- workDir: the tool does not always live at the repo root ---------------
# In a pnpm/turbo monorepo the mutation tool is a devDependency of ONE package,
# so `npx --no-install <tool>` from the repo root cannot resolve it — and npx
# then tries to FETCH a same-named package from the registry, which is both a
# broken gate and a supply-chain hazard. Most of our repos are monorepos, so a
# ratchet that can only be armed at the root can only be armed in half of them.
mkrepo; mkdir -p "$R/packages/core"
printf '{"version":1,"tool":"stryker","workDir":"packages/core"}' >"$R/.fluxpoint-mutation.json"
commit
out="$(mg --report 2>&1)"
case "$out" in *"unknown field"*) bad "workDir is an accepted field" "rejected" ;;
  *) ok "workDir is an accepted field" "accepted" ;; esac

# It must be a relative path INSIDE the repo. An absolute or escaping path would
# run the tool somewhere the repo does not control.
mkrepo; mkdir -p "$ROOT/sibling"
# The sibling EXISTS, so only the escape rule can refuse it — with a path that is
# merely absent, the existence check fires and nothing pins this rule at all.
printf '{"version":1,"tool":"stryker","workDir":"../sibling"}' >"$R/.fluxpoint-mutation.json"
commit
err="$(mg --report 2>&1)"
case "$err" in *escapes*) ok "an escaping workDir is refused AS an escape" "refused" ;;
  *) bad "an escaping workDir is refused AS an escape" "${err:0:60}" ;; esac

# A lexical child can still resolve outside the repo through a symlink or a
# Windows junction. The runner must validate the resolved directory it will
# actually use, not only the spelling committed in the config.
mkrepo; mkdir -p "$ROOT/outside-workdir"
if "$FPL_PY" - "$R/linked" "$ROOT/outside-workdir" <<'PY'
import os, subprocess, sys
link, target = map(os.path.abspath, sys.argv[1:3])
if os.name == "nt":
    result = subprocess.run(
        [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", "mklink", "/J",
         link, target],
        capture_output=True,
    )
    raise SystemExit(result.returncode)
os.symlink(target, link, target_is_directory=True)
PY
then
  printf '{"version":1,"tool":"stryker","workDir":"linked"}' >"$R/.fluxpoint-mutation.json"
  err="$(mg --report 2>&1)"
  case "$err" in *escapes*) ok "a linked workDir cannot resolve outside the repo" "refused" ;;
    *) bad "a linked workDir cannot resolve outside the repo" "${err:0:60}" ;; esac
else
  bad "a linked workDir cannot resolve outside the repo" "fixture unavailable"
fi

mkrepo
printf '{"version":1,"tool":"stryker","workDir":"/etc"}' >"$R/.fluxpoint-mutation.json"
commit
err="$(mg --report 2>&1)"
case "$err" in *workDir*) ok "an absolute workDir is refused by name" "refused" ;;
  *) bad "an absolute workDir is refused by name" "${err:0:60}" ;; esac

# And it must EXIST — a typo'd path would otherwise fail much later, inside npx,
# with an error about the tool rather than about the config.
mkrepo
printf '{"version":1,"tool":"stryker","workDir":"packages/typo"}' >"$R/.fluxpoint-mutation.json"
commit
err="$(mg --report 2>&1)"
case "$err" in *"does not exist"*) ok "a workDir that does not exist is named" "named" ;;
  *) bad "a workDir that does not exist is named" "${err:0:60}" ;; esac


# The validation above is worthless if the runner ignores the field. `--measure`
# with no tool installed still names the report path it looked for, and that path
# is the observable proof of which directory it ran in.
mkrepo; mkdir -p "$R/packages/core"
printf '{"version":1,"tool":"stryker","workDir":"packages/core"}' >"$R/.fluxpoint-mutation.json"
commit
err="$(mg --measure 2>&1)"
err="${err//\\//}"
case "$err" in *packages/core*) ok "the runner looks for its report UNDER workDir" "scoped" ;;
  *) bad "the runner looks for its report UNDER workDir" "${err:0:70}" ;; esac

resolved_target="$($FPL_PY - "$MG" "$ROOT" <<'PY'
import importlib.util, os, sys, tempfile
spec = importlib.util.spec_from_file_location("mutation_guard", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
root = os.path.join(sys.argv[2], "resolution-root")
external = os.path.join(sys.argv[2], "resolution-bin")
os.makedirs(root, exist_ok=True)
os.makedirs(external, exist_ok=True)
candidate = os.path.join(external, "npx" + (".cmd" if os.name == "nt" else ""))
inside = os.path.join(root, "repo-owned-npx")
open(candidate, "w").close()
open(inside, "w").close()
if os.name != "nt":
    os.chmod(candidate, 0o755)
original = module.os.path.realpath
def redirected(value):
    if os.path.normcase(os.path.abspath(value)) == os.path.normcase(os.path.abspath(candidate)):
        return inside
    return original(value)
module.os.path.realpath = redirected
try:
    found = module._find_executable(root, "npx", [external])
except TypeError:
    found = inside
print("safe" if found is None else "unsafe")
PY
)"
check "an external PATH link cannot resolve back into the repo" "safe" "$resolved_target"

resolved_tools="$($FPL_PY - "$MG" "$ROOT" <<'PY'
import importlib.util, os, sys
spec = importlib.util.spec_from_file_location("mutation_guard", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
root = os.path.join(sys.argv[2], "tool-root")
external = os.path.join(sys.argv[2], "tool-bin")
os.makedirs(root, exist_ok=True)
os.makedirs(external, exist_ok=True)
suffix = ".exe" if os.name == "nt" else ""
for name in ("git", "cargo"):
    tool = os.path.join(external, name + suffix)
    open(tool, "w").close()
    if os.name != "nt": os.chmod(tool, 0o755)
seen = []
def run(command, **kwargs):
    seen.append((command[0], kwargs.get("env", {})))
    class Result:
        returncode = 0 if "ls-files" in command else 1
        stdout = ""
        stderr = ""
    return Result()
module.subprocess.run = run
module.os.environ["PATH"] = external
module.git(root, "rev-parse", "HEAD")
module.run_cargo_mutants(root, [])
names = [os.path.basename(command).lower() for command, _ in seen]
safe = (len(seen) == 3 and names[0].startswith("git")
        and names[1].startswith("git") and names[2].startswith("cargo")
        and all(os.path.isabs(command) for command, _ in seen))
if os.name == "nt":
    safe = safe and all(env.get("NoDefaultCurrentDirectoryInExePath") == "1" for _, env in seen)
print("safe" if safe else "unsafe")
PY
)"
check "Git and Cargo use resolved tools with the hardened child environment" "safe" "$resolved_tools"

git_hardening_probe="$($FPL_PY - "$MG" "$ROOT" <<'PY'
import importlib.util, json, os, shutil, subprocess, sys

spec = importlib.util.spec_from_file_location("mutation_guard", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
base = os.path.join(sys.argv[2], "git-hardening")
git = shutil.which("git")


def make_repo(name, changes):
    root = os.path.join(base, name)
    os.makedirs(root, exist_ok=True)
    subprocess.run([git, "-C", root, "init", "-q", "-b", "main"], check=True)
    shas = []
    for message, files in changes:
        for path, content in files.items():
            with open(os.path.join(root, path), "w", encoding="utf-8") as fh:
                fh.write(content + "\n")
        subprocess.run([git, "-C", root, "add", "-A"], check=True)
        subprocess.run([git, "-C", root, "-c", "user.name=fixture",
                        "-c", "user.email=fixture@example.test", "commit", "-qm", message],
                       check=True)
        shas.append(subprocess.check_output([git, "-C", root, "rev-parse", "HEAD"],
                                            text=True).strip())
    return root, shas


target, (target_sha,) = make_repo("target", [("target", {"identity.txt": "target"})])
attacker, (attacker_sha,) = make_repo(
    "attacker", [("attacker", {"identity.txt": "attacker"})])
original_env = dict(os.environ)
os.environ.update({
    "GIT_DIR": os.path.join(attacker, ".git"),
    "GIT_WORK_TREE": attacker,
    "GIT_COMMON_DIR": os.path.join(attacker, ".git"),
    "GIT_INDEX_FILE": os.path.join(attacker, ".git", "index"),
    "GIT_OBJECT_DIRECTORY": os.path.join(attacker, ".git", "objects"),
    "GIT_NAMESPACE": "attacker",
})
observed = module.git(target, "rev-parse", "--verify", "HEAD^{commit}")
head_safe = observed == target_sha
os.environ.clear()
os.environ.update(original_env)

os.environ["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = os.path.join(attacker, ".git", "objects")
exists, kind = module._git_status(target, "cat-file", "-t", attacker_sha)
objects_safe = not exists and not kind
os.environ.clear()
os.environ.update(original_env)

replacement, (base_sha, head_sha) = make_repo(
    "replacement", [("base", {"identity.txt": "base"}),
                     ("changed", {"changed.txt": "changed"})])
subprocess.run([git, "-C", replacement, "replace", head_sha, base_sha], check=True)
changed = module.git(replacement, "diff", "--name-only", f"{base_sha}..{head_sha}")
replace_safe = changed == "changed.txt"

poisoned = {
    "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_REPLACE_REF_BASE", "GIT_NAMESPACE", "GIT_SHALLOW_FILE",
    "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0",
    "GIT_CEILING_DIRECTORIES", "GIT_DISCOVERY_ACROSS_FILESYSTEM",
}
for key in poisoned:
    os.environ[key] = "attacker-controlled"
os.environ["GIT_NO_REPLACE_OBJECTS"] = "0"
env = module._child_env([])
remaining = {key for key in env if key.upper().startswith("GIT_")}
env_safe = (remaining == {"GIT_NO_REPLACE_OBJECTS"}
            and env["GIT_NO_REPLACE_OBJECTS"] == "1")
print(json.dumps({"head": head_safe, "objects": objects_safe,
                  "replace": replace_safe, "environment": env_safe}))
PY
)"
git_probe_field() {
  printf '%s' "$git_hardening_probe" | "$FPL_PY" -c \
    'import json,sys; print("safe" if json.load(sys.stdin)[sys.argv[1]] else "unsafe")' "$1"
}
check "inherited Git repository routing cannot replace target HEAD" "safe" \
  "$(git_probe_field head)"
check "an inherited alternate object store cannot resurrect a foreign commit" "safe" \
  "$(git_probe_field objects)"
check "replacement refs cannot conceal files changed since measurement" "safe" \
  "$(git_probe_field replace)"
check "the child environment removes every Git route and disables replacements" "safe" \
  "$(git_probe_field environment)"

case "$(uname -s 2>/dev/null || true)" in
MINGW*|MSYS*|CYGWIN*)
  mkrepo
  printf '{"version":1,"tool":"stryker"}' >"$R/.fluxpoint-mutation.json"
  mkdir -p "$R/node_modules/npm/bin"
  printf '@echo off\r\nexit /b 0\r\n' >"$R/npx.cmd"
  printf 'require("node:fs").writeFileSync(process.env.MUTATION_GUARD_HIJACK_MARKER,"ran")\n' \
    >"$R/node_modules/npm/bin/npx-cli.js"
  commit
  export MUTATION_GUARD_HIJACK_MARKER="$ROOT/npx-hijack-ran"
  rm -f "$MUTATION_GUARD_HIJACK_MARKER"
  mg --measure >/dev/null 2>&1 || true
  if [ -e "$MUTATION_GUARD_HIJACK_MARKER" ]; then
    bad "a repo-local npx.cmd cannot hijack the mutation runner" "executed"
  else
    ok "a repo-local npx.cmd cannot hijack the mutation runner" "ignored"
  fi
  unset MUTATION_GUARD_HIJACK_MARKER

  mkrepo
  printf '{"version":1,"tool":"stryker"}' >"$R/.fluxpoint-mutation.json"
  NODE_A="$ROOT/node-a"; NODE_B="$ROOT/node-b"
  mkdir -p "$NODE_A" "$NODE_B/node_modules/npm/bin"
  cp "$WINDIR/System32/cmd.exe" "$NODE_A/node.exe"
  cp "$(command -v node)" "$NODE_B/node.exe"
  printf '@echo off\r\nexit /b 0\r\n' >"$NODE_B/npx.cmd"
  printf 'require("node:fs").writeFileSync(process.env.MUTATION_GUARD_SIBLING_MARKER,"sibling")\n' \
    >"$NODE_B/node_modules/npm/bin/npx-cli.js"
  export MUTATION_GUARD_SIBLING_MARKER="$ROOT/npx-sibling-ran"
  rm -f "$MUTATION_GUARD_SIBLING_MARKER"
  "$FPL_PY" - "$MG" "$R" "$NODE_A" "$NODE_B" <<'PY' >/dev/null 2>&1
import importlib.util, os, sys
spec = importlib.util.spec_from_file_location("mutation_guard", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
os.environ["PATH"] = os.pathsep.join(sys.argv[3:5])
module._clear_live_report = lambda *_args: None
module.run_stryker(sys.argv[2], [])
PY
  if [ -e "$MUTATION_GUARD_SIBLING_MARKER" ]; then
    ok "npx.cmd uses the node.exe in its own installation" "sibling"
  else
    bad "npx.cmd uses the node.exe in its own installation" "unrelated Node selected"
  fi
  unset MUTATION_GUARD_SIBLING_MARKER NODE_A NODE_B
  ;;
esac

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
