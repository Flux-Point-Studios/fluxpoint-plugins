#!/usr/bin/env bash
# The hygiene scan may be narrowed. The gate that ARMS it may not.
#
# fpl_scan_hygiene greps whole UNTRACKED files while the tracked branch three
# lines above takes only added lines, so a design-tool export or a vendored
# sample that predates the session blocks every stop in the repo, on code
# nobody in this session wrote. .gitignore scopes that, but a file you intend
# to commit LATER has no correct lever: gitignoring it is wrong and committing
# it early to silence the gate is worse.
#
# The dangerous fix is the obvious one. _fpl_skip_path is shared with
# fpl_code_dirty, the predicate that arms the gate, so a repo-extensible skip
# list wired there would let `src/**` in a file switch the DoD gate off
# silently. The property under test is that the new list narrows the SCAN and
# cannot disarm the GATE.
set -uo pipefail

PLUGIN="$(cd "$(dirname "$0")/.." && pwd)"
pass=0; fail=0
ok()  { printf 'PASS  %-54s -> %s\n' "$1" "$2"; pass=$((pass+1)); }
bad() { printf 'FAIL  %-54s -> %s\n' "$1" "$2"; fail=$((fail+1)); }
check(){ [ "$2" = "$3" ] && ok "$1" "$3" || bad "$1" "$3 (wanted $2)"; }

WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
mkrepo() {
  r="$WORK/$1"; rm -rf "$r"; mkdir -p "$r"
  ( cd "$r" && git init -q . && git config user.email t@t && git config user.name t \
    && printf 'ok\n' > seed.txt && git add -A && git commit -qm init ) >/dev/null
  printf '%s' "$r"
}
scan() { ( cd "$1" && . "$PLUGIN/scripts/lib.sh" >/dev/null 2>&1; fpl_scan_hygiene 2>/dev/null ); }
# fpl_code_dirty is a PREDICATE: it prints nothing and answers by exit status.
# Counting its stdout would report "not dirty" for every repo, which is the
# shape of test that passes while proving nothing.
dirty() { ( cd "$1" && . "$PLUGIN/scripts/lib.sh" >/dev/null 2>&1; fpl_code_dirty >/dev/null 2>&1; echo $? ); }

# ============ 1. the reported case: a pre-existing untracked export ======
R="$(mkrepo export)"
printf 'function a(){ try{x()}catch(e){} }\nfunction b(){ try{y()}catch(e){} }\n' > "$R/design-export.js"
n="$(scan "$R" | wc -l | tr -d ' ')"
[ "$n" -gt 0 ] && ok "an untracked file with catch{} is flagged" "$n hit(s)" \
                || bad "an untracked file with catch{} is flagged" "0"

printf 'design-export.js\n' > "$R/.fluxpoint-hygiene-ignore"
n="$(scan "$R" | grep -c 'design-export' || true)"
check "and a hygiene-ignore entry silences it" 0 "$n"

# ============ 2. globs, comments and blank lines ========================
R="$(mkrepo globs)"
mkdir -p "$R/vendor"
printf 'const x = 1; // TODO later\n' > "$R/vendor/sample.js"
printf '# vendored samples, committed next sprint\n\nvendor/*\n' > "$R/.fluxpoint-hygiene-ignore"
n="$(scan "$R" | grep -c 'vendor/sample' || true)"
check "a glob with comments and blanks is honored" 0 "$n"

# ============ 3. IT MUST NOT DISARM THE GATE ============================
# The whole reason this is a separate list. fpl_code_dirty is the arming
# predicate; a hygiene-ignore naming the source tree must not touch it.
R="$(mkrepo arming)"
mkdir -p "$R/src"
printf 'let v = 1\n' > "$R/src/app.js"
printf 'src/*\nsrc/**\n*\n' > "$R/.fluxpoint-hygiene-ignore"
check "a hygiene-ignore cannot disarm fpl_code_dirty" 0 "$(dirty "$R")"

# The same repo WITHOUT the ignore file must answer identically: if the two
# differ, the list reached the arming predicate.
rm -f "$R/.fluxpoint-hygiene-ignore"
before="$(dirty "$R")"
printf 'src/*\nsrc/**\n*\n' > "$R/.fluxpoint-hygiene-ignore"
after="$(dirty "$R")"
check "even a wildcard leaves the arming answer unchanged" "$before" "$after"

# And the structural half: the predicate must not reference the list at all.
n="$(sed -n '/^fpl_code_dirty()/,/^}/p' "$PLUGIN/scripts/lib.sh" | grep -c '_fpl_hygiene_ignored' || true)"
check "fpl_code_dirty never consults the ignore list" 0 "$n"

# ============ 4. widening the blind spot is itself reported =============
R="$(mkrepo trust)"
printf 'design-export.js\n' > "$R/.fluxpoint-hygiene-ignore"
t="$( cd "$R" && . "$PLUGIN/scripts/lib.sh" >/dev/null 2>&1; fpl_trust_base_modified 2>/dev/null )"
case "$t" in *.fluxpoint-hygiene-ignore*)
    ok "the ignore file is in the trust base" "reported" ;;
  *) bad "the ignore file is in the trust base" "${t:-not reported}" ;; esac

# ============ 5. tracked files are unaffected ===========================
# The tracked branch already takes only added lines; the list is for the
# untracked branch and must not quietly widen to cover committed code.
R="$(mkrepo tracked)"
printf 'let a = 1 // TODO real\n' > "$R/live.js"
( cd "$R" && git add -A && git commit -qm add ) >/dev/null
printf 'live.js\n' > "$R/.fluxpoint-hygiene-ignore"
printf 'let a = 1 // TODO real\nlet b = 2 // FIXME new\n' > "$R/live.js"
n="$(scan "$R" | grep -c 'FIXME' || true)"
[ "$n" -gt 0 ] && ok "a tracked file's added lines are still scanned" "$n hit(s)" \
                || bad "a tracked file's added lines are still scanned" "SILENCED"

# ============ 6. build-output skips cover Python =========================
R="$(mkrepo pyenv)"
mkdir -p "$R/.venv/lib/site-packages" "$R/__pycache__"
printf 'x = 1  # TODO upstream\n' > "$R/.venv/lib/site-packages/dep.py"
printf 'y = 2  # FIXME upstream\n' > "$R/__pycache__/c.py"
n="$(scan "$R" | grep -cE '\.venv|__pycache__' || true)"
check "a virtualenv is not walked by the scan" 0 "$n"

# ====== the list has to work on the machines that author it =============
# Both of these leave the gate blocked while the operator has done exactly
# what the feature asks, which is the failure mode a silent no-op always has:
# the fix looks applied and nothing changes.

# A list written on Windows carries CRLF. `read -r` keeps the CR, it lands
# INSIDE the glob, and `scratch/*<CR>` matches nothing — so the file silently
# does nothing and the gate keeps blocking.
R="$(mkrepo crlf)"
mkdir -p "$R/scratch"; printf 'x = 1  # TODO later\n' > "$R/scratch/notes.py"
printf 'scratch/*\r\n' > "$R/.fluxpoint-hygiene-ignore"
n="$(scan "$R" | wc -l | tr -d ' ')"
check "a CRLF-authored ignore list still narrows the scan" 0 "$n"

# The list is untracked too, so the scan reads it — and the natural comment a
# person writes when adding an entry names the marker they are ignoring. The
# act of documenting the exclusion then blocks the gate on the exclusion file.
R="$(mkrepo selfscan)"
mkdir -p "$R/scratch"; printf 'x = 1  # TODO later\n' > "$R/scratch/notes.py"
printf '# the scratch dir, full of TODO markers\nscratch/*\n' > "$R/.fluxpoint-hygiene-ignore"
n="$(scan "$R" | wc -l | tr -d ' ')"
check "the ignore list is not scanned as its own violation" 0 "$n"

# ====== a marker is spelled the way its language spells it ===============
# The filter matched every alternative case-insensitively, so ordinary words
# tripped the proof-escape and marker checks: "re-admitted" read as Coq's
# Admitted and "a todo list" as a TODO. One row per decision, with both sides
# of it. Case cannot separate Lean's `sorry` from the English word, so the
# file type does: it is a keyword in .lean and .thy files and prose elsewhere.
# In a Lean file only a line that cannot hold code is let through: one that
# opens with `--` and closes neither a string nor a block comment. A trailing
# comment still matches, because the `--` before it may sit inside a string.
# The rows wanting 1 for a call, an attribute or a flag pin what must not
# change: those keep matching in any case.
# Dropping the case-insensitive match also dropped comments people do write:
# `// todo: handle the error path`, `# fixme`, and the `@todo` and `\todo`
# tags that JSDoc, PHPDoc and Doxygen define in lower case. A comment that
# opens with the word as a marker is one, in any case, and so is the tag.
# The word in the middle of a sentence, in code or in a string is not.
PY="$( . "$PLUGIN/scripts/python-env.sh" >/dev/null 2>&1; printf '%s' "$FPL_PY" )"
mkdir -p "$WORK/empty"
flagged() {
  printf '%s\n' "$2" | ( cd "$WORK/empty" && "$PY" "$PLUGIN/scripts/hygiene-filter.py" "$1" ) | wc -l | tr -d ' '
}
while IFS='|' read -r want path line; do
  [ -n "$want" ] || continue
  check "$path: $line" "$want" "$(flagged "$path" "$line")"
done <<'ROWS'
0|notes.txt|the record was re-admitted
0|notes.txt|a todo list
0|notes.txt|phone xx-xxx-1234
0|notes.txt|sorry, the file moved
1|notes.txt|TODO: fix
1|Proof.v|Admitted.
1|app.js|let a = 1 // FIXME new
0|app.js|let fixme = 1
1|app.js|// XXX: remove before merge
1|spec.dfy|lemma {:axiom} L()
1|spec.dfy|method {:verify false} M()
0|spec.dfy|// {:AXIOM} in capitals is an attribute Dafny does not read
1|Proofs.lean|theorem t : True := by sorry
1|Proofs.lean|  sorry
1|Scratch.thy|  sorry
1|Proofs.lean|theorem c : parse "-- x" = none := by sorry
1|Proofs.lean|  exact h -- sorry about this
1|Proofs.lean|-- the header ends here -/ theorem t : True := by sorry
1|Proofs.lean|-- " ++ (sorry : String)
0|Proofs.lean|-- sorry, the file moved
0|Proofs.lean|def Sorry := 1
0|app.js|throw new Error("sorry, the file moved")
1|src/lib.rs|    todo!()
1|src/lib.rs|    unimplemented!("later")
1|src/lib.rs|    #[ignore = "slow"]
1|src/lib.rs|    let v = x.unwrap();
1|src/lib.rs|#[verifier::external_body]
1|app_test.go|	t.Skip("flaky")
1|app.test.js|it.only("runs alone", () => {})
1|ci.sh|git commit --no-verify -m wip
1|ci.sh|git commit --NO-VERIFY -m wip
1|deploy.ps1|Try { Publish } Catch { }
1|app.js|// todo: handle the error path
1|util.py|# todo: validate input
1|app.js|// Fixme: off by one
1|util.py|# fixme
1|util.py|x = 1  # ToDo(alice): later
1|app.js|/* fixme */
1|app.js| * todo: move this into the parser
1|query.sql|-- todo: add the index
1|app.js|/** @todo handle the error path */
1|app.js| * @todo: handle the error path
1|lib.php|/// @todo
1|vec.hpp|/* \todo bounds check */
1|vec.hpp|//! \todo
0|app.js|// a todo list
0|app.js|// todo items are kept in order
0|util.py|# the fixme tag is described in the README
0|util.py|todo = load()  # the list a user keeps
0|util.py|    @todo.setter
0|util.py|    @todo
0|app.js|const email = "me@todo.example" // contact
0|app.js|open("C:\todo\list.txt")
0|notes.txt|write a todo: buy milk
0|service.conf|url=http://todo:8080/api
0|service.conf|upstream = https://fixme:9000
0|notes.txt|usage: task --todo
0|notes.txt|usage: task --fixme
1|app.js|go("http://tracker/1") // todo: retry
1|main.c|case 1:// todo: fall through
1|Makefile|# todo: split the target
1|Dockerfile|# fixme
1|build.pl|# Todo: handle the error
1|deploy.ps1|# todo: sign the binary
1|main.tf|# fixme: pin the provider
1|settings.ini|; todo: rotate the key
1|settings.ini|# fixme
1|page.html|<!-- todo -->
1|page.html|<!-- fixme: alt text -->
1|view.vue|  <!-- todo: loading state -->
0|page.html|<a href="#todo">the list</a>
0|style.css|#todo:hover { color: red }
0|main.c|#define todo(x) (x)
0|Makefile|todo: build
0|Makefile|	@echo "# todo: later"
0|settings.ini|todo = buy milk ; and eggs
ROWS

# The same rule through the scan the gate runs. The path reaches the filter on
# both branches: the added lines of a tracked file and a whole untracked one.
R="$(mkrepo spelled)"
printf 'theorem t : True := trivial\n' > "$R/Proofs.lean"
( cd "$R" && git add -A && git commit -qm lean ) >/dev/null
printf 'theorem t : True := by sorry\n-- sorry, the file moved\n' > "$R/Proofs.lean"
printf '# sorry, the file moved\n# the record was re-admitted to a todo list\n' > "$R/notes.py"
out="$(scan "$R")"
check "the scan flags a Lean sorry on an added line" 1 "$(printf '%s\n' "$out" | grep -c 'by sorry' || true)"
check "and flags no prose around it, tracked or untracked" 1 "$(printf '%s\n' "$out" | grep -c . || true)"

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
