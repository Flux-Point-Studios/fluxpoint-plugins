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
failure_hook = any('exec-attest.sh' in hook.get('command', '') or hook.get('args') == ['${CLAUDE_PLUGIN_ROOT}/scripts/native-hook.mjs', 'attest']
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
for label, needle in (("isolated nodes establish the exact campaign base", "Every isolated node establishes the exact campaign base"),
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

# ============ Rust: the guides name what the scanner counts ===============
# A category the guide does not name is one no reader arms, and a category
# the guide names that the scanner dropped is a guarantee nobody provides.
# Both directions are derived from the registry that decides them.
sys.path.insert(0, os.path.join(PLUGIN, "scripts"))
import rust_hatches  # noqa: E402

RUST_GUIDE = read("references", "rust.md")
PROOF = read("scripts", "proof-guard.py")
PLUGIN_README = read("README.md")
TEMPLATE = read("templates", "harness.sh")
_cats = list(rust_hatches.CATEGORIES) + ["flags.lints_off"]
for _where, _text in (("the Rust guide", RUST_GUIDE), ("proof-guard's own help", PROOF),
                      ("the plugin README", PLUGIN_README)):
    _missing = [c for c in _cats if c not in _text]
    report(f"every Rust and FRAME category is named in {_where}", not _missing,
           f"{len(_cats)} categories" if not _missing else f"MISSING {_missing}")
_named = set(re.findall(r"`((?:rust|frame)\.[a-z_]+)`", RUST_GUIDE))
_stale = sorted(_named - set(rust_hatches.CATEGORIES)
                - {"rust.external_body", "rust.trusted", "rust.assume",
                   "rust.mutants_skip", "rust.mutants_exclude"})
report("and the guide names no category the scanner does not count", not _stale,
       "none stale" if not _stale else f"STALE {_stale}")


def _rust_counts(files):
    out = {}
    for _cat, _, _, _ in rust_hatches.scan_tree(files):
        out[_cat] = out.get(_cat, 0) + 1
    return out


# The exclusions are promises too. Each is EXECUTED against the scanner, so
# the sentence and the behaviour cannot part ways quietly.
_GUIDE_FLAT = re.sub(r"\s+", " ", RUST_GUIDE)
for _label, _needle, _files, _want in (
        ("#[expect] is documented as uncounted, and is", "`#[expect(...)]` warns once the lint it names stops firing",
         {"src/lib.rs": "#[expect(dead_code)]\nfn f() {}\n"}, {}),
        ("numeric casts are documented as left to clippy, and are", "Numeric `as` casts, indexing and unchecked arithmetic",
         {"src/lib.rs": "pub fn f(x: u64, v: &[u8]) -> u32 { (x as u32) + u32::from(v[0]) }\n"}, {}),
        ("a propagated error is documented as uncounted, and is", "`let _ = f()?;` propagated the error",
         {"src/lib.rs": "fn f() -> Result<(), E> { let _ = g()?; Ok(()) }\n"}, {}),
        ("a module merely named tests is documented as counted, and is",
         "A module merely named `tests` and declared without the cfg ships in production and is counted",
         {"src/lib.rs": "mod tests;\n", "src/tests.rs": "pub fn f(x: Option<u8>) -> u8 { x.unwrap() }\n"},
         {"rust.panic": 1}),
        ("a declared test module is documented as left out, and is",
         "`#[cfg(test)] mod tests;` makes `tests.rs` test code",
         {"src/lib.rs": "#[cfg(test)]\nmod tests;\n", "src/tests.rs": "fn t() { x().unwrap(); }\n"}, {}),
        ("a build script is documented as left out, and is", "and the `build.rs` beside it",
         {"Cargo.toml": "[package]\nname = \"x\"\n", "build.rs": "fn main() { run().unwrap(); }\n",
          "src/lib.rs": "pub fn f() {}\n"}, {}),
        ("a crate root a test declares is documented as counted, and is",
         "A crate root is never test code by declaration",
         {"Cargo.toml": "[package]\nname = \"x\"\n", "src/main.rs": "fn main() { run().unwrap(); }\n",
          "tests/it.rs": "#[path = \"../src/main.rs\"]\nmod app;\n#[test]\nfn t() { app::main(); }\n"},
         {"rust.panic": 1}),
        ("a module moved under tests/ is documented as counted, and is",
         "One production `mod` or `include!` line keeps a file counted wherever it sits",
         {"Cargo.toml": "[package]\nname = \"x\"\n",
          "src/lib.rs": "#[path = \"../tests/moved.rs\"]\nmod moved;\n",
          "tests/moved.rs": "pub fn f(x: Option<u8>) -> u8 { x.unwrap() }\n"}, {"rust.panic": 1}),
        ("an include is documented as followed, and is", "and so is `include!(\"file.rs\")`",
         {"src/lib.rs": "#[cfg(test)]\nmod tests { include!(\"cases.rs\"); }\n",
          "src/cases.rs": "fn helper() { x().unwrap(); }\n"}, {}),
        ("a select branch is documented as no discard, and is not counted",
         "`_ = shutdown.cancelled() => break` is a branch of `tokio::select!`",
         {"src/lib.rs": "async fn f() { tokio::select! { _ = a() => {}, _ = b() => {} } }\n"}, {}),
        ("an inner attribute handed to a macro is documented as tokens, and is",
         "Handed to a macro, as in `stringify!(#![cfg(test)])`, it is tokens",
         {"src/lib.rs": "const _: &str = stringify!(#![cfg(test)]);\npub fn f(x: Option<u8>) -> u8 { x.unwrap() }\n"},
         {"rust.panic": 1}),
        ("a test-only parameter is documented as covering the parameter alone, and does",
         "On a generic or a closure parameter that is the parameter",
         {"src/lib.rs": "impl<#[cfg(feature = \"runtime-benchmarks\")] F: Mutate> S<F> {\n"
                        "    fn f(x: Option<u8>) -> u8 { x.unwrap() }\n}\n"}, {"rust.panic": 1}),
        ("a bench target a manifest names is documented as left out, and is",
         "together with the `path` a manifest gives a `[[test]]`, a `[[bench]]` or its `build` key",
         {"Cargo.toml": "[package]\nname = \"x\"\n\n[[bench]]\nname = \"b\"\npath = \"perf/b.rs\"\n",
          "perf/b.rs": "fn main() { run().unwrap(); }\n", "src/lib.rs": "pub fn f() {}\n"}, {}),
        ("a raw identifier is documented as the call it spells, and is counted",
         "`x.r#unwrap()` and `x.unwrap::<>()` are `x.unwrap()`",
         {"src/lib.rs": "pub fn f(x: Option<u8>) -> u8 { x.r#unwrap() + x.unwrap::<>() }\n"}, {"rust.panic": 2}),
        ("a lint allowed under [workspace] is documented as counted, and is",
         "a dotted key or an inline table at the root or under `[workspace]`",
         {"Cargo.toml": "[workspace]\nlints.rust.unused = \"allow\"\n", "src/lib.rs": "pub fn f() {}\n"},
         {"rust.lint_allow": 1}),
        ("a fuzz target is documented as counted, and is", "a cargo-fuzz crate, whose targets are binaries",
         {"fuzz/Cargo.toml": "[package]\nname = \"x-fuzz\"\n\n[package.metadata]\ncargo-fuzz = true\n\n"
                             "[[bin]]\nname = \"parse\"\npath = \"fuzz_targets/parse.rs\"\n",
          "fuzz/fuzz_targets/parse.rs": "#![no_main]\nlibfuzzer_sys::fuzz_target!(|data: &[u8]| { parse(data).unwrap(); });\n"},
         {"rust.panic": 1}),
        ("a benchmark feature on by default is documented as a miss, and is one",
         "naming `runtime-benchmarks` in a `default` feature list",
         {"Cargo.toml": "[package]\nname = \"x\"\n\n[features]\ndefault = [\"runtime-benchmarks\"]\nruntime-benchmarks = []\n",
          "src/lib.rs": "#[cfg(feature = \"runtime-benchmarks\")]\npub fn f(x: Option<u8>) -> u8 { x.unwrap() }\n"}, {}),
        ("a vendored tree is documented as counted, and is", "Tracked `vendor/` trees are counted like any tracked source",
         {"vendor/dep/src/lib.rs": "pub fn f(x: Option<u8>) -> u8 { x.unwrap() }\n",
          "vendor/dep/.cargo-checksum.json": "{}\n"}, {"rust.panic": 1}),
        ("a hand-written weight is documented as counted whatever it says, and is",
         "The first is a guess and the second is an argument",
         {"src/lib.rs": "#[pallet::weight(T::DbWeight::get().reads_writes(1, 1))]\nfn a() {}\n"
                        "#[pallet::weight(T::WeightInfo::b())]\nfn b() {}\n"},
         {"frame.unbenchmarked_weight": 1})):
    _got = _rust_counts(_files)
    report(_label, _needle in _GUIDE_FLAT and _got == _want,
           "stated and executed" if _needle in _GUIDE_FLAT and _got == _want
           else ("NOT STATED" if _needle not in _GUIDE_FLAT else f"scanner says {_got}, guide says {_want}"))

# What the guide says the scaffold runs is what the template runs.
for _line in ("cargo fmt --all -- --check",
              "cargo clippy --workspace --all-targets --quiet -- -D warnings",
              "cargo test --workspace --quiet"):
    report(f"the guide's `{_line[:34]}` line is in the template",
           _line in RUST_GUIDE and _line in TEMPLATE,
           "both" if _line in RUST_GUIDE and _line in TEMPLATE else "DRIFTED")
_seed = [n for n, t in (("template", TEMPLATE), ("guide", RUST_GUIDE), ("root README", ROOT_README),
                        ("init", INIT)) if "FPL_PROPTEST_SEED" not in t]
report("the proptest seed variable is documented wherever it is promised", not _seed,
       "four places" if not _seed else f"MISSING in {_seed}")
_frame = ("proptest >= 1.2 under rustc >= 1.73" in CEX
          and "since 1.2 under the panic header rustc has printed since 1.73" in _GUIDE_FLAT)
report("the guide and the parser agree on which proptest output is read", _frame,
       "1.2 / 1.73" if _frame else "DRIFTED")

# The attack classes: the shipped manifest is the list, and the documents
# that enumerate it may not fall behind it.
with open(os.path.join(PLUGIN, "templates", "attack-taxonomy.json"), encoding="utf-8") as _fh:
    _taxonomies = {t["language"]: [c["id"] for c in t["classes"]] for t in json.load(_fh)["taxonomies"]}
REDTEAM = read("references", "red-team-cardano.md")
_words = {8: "eight", 9: "nine", 10: "ten", 11: "eleven", 12: "twelve", 13: "thirteen", 14: "fourteen"}
for _lang in ("rust", "frame"):
    _ids = _taxonomies.get(_lang, [])
    _missing = [c for c in _ids if c not in REDTEAM]
    report(f"every {_lang} attack class is named in the red-team reference", bool(_ids) and not _missing,
           f"{len(_ids)} classes" if _ids and not _missing else f"MISSING {_missing or 'the taxonomy'}")
    _word = _words.get(len(_ids), str(len(_ids)))
    _lower = re.sub(r"\s+", " ", PLUGIN_README)
    _claim = (f"holds {_word} classes" if _lang == "rust" else f"taxonomy {_word} for pallets")
    report(f"the plugin README counts the {_lang} classes as the manifest does", _claim in _lower,
           _word if _claim in _lower else f"manifest has {len(_ids)}; README does not say `{_claim}`")
_known = re.search(r"^TAXONOMY_LANGUAGES = \((.*?)\)", SPEC, re.M)
_known = re.findall(r'"(\w+)"', _known.group(1)) if _known else []
_untold = [lang for lang in _known if f"**{lang}**" not in INIT]
report("every taxonomy language spec-guard gates is introduced by init", bool(_known) and not _untold,
       f"{len(_known)} languages" if _known and not _untold else f"MISSING {_untold}")

# ============ 1.51.0: refusal codes and flags the documents name ============
# A document that names a code or a flag is pinned to the script that prints
# it, by running the script: a renamed code or a dropped flag fails here.
import importlib.util  # noqa: E402

_cg_spec = importlib.util.spec_from_file_location("cg_doc_claims", os.path.join(PLUGIN, "scripts", "compile-graph.py"))
_cg = importlib.util.module_from_spec(_cg_spec)
_cg_spec.loader.exec_module(_cg)
_review = {"version": 1, "campaign": "c", "treeGuard": False, "budget": {"maxNodes": 4},
           "lists": {"lens": [{"key": "a"}]},
           "nodes": [{"id": "lenses", "foreach": "lens", "prompt": "review {{item.key}}", "contract": "FindingsV1",
                      "resume": "never"}]}
_refusal = _cg.resume_refusal(_review, {"results": {"lenses": [{"findings": []}]}}) or ""
_code = _refusal.split(":")[0]
_emitted = _cg.emit(_review, _cg.load_contracts(os.path.join(PLUGIN, "contracts")), {})
_resume_docs = {"graph reference": SKILL, "graph-run": read("commands", "graph-run.md"),
                "structured resume": read("references", "structured-resume.md"),
                "feature template": read("templates", "WORK.feature.md")}
_silent = [name for name, text in _resume_docs.items() if _code not in text]
report("the resume refusal the documents name is the one the compiler raises",
       _code == "RESUME-PAST-NO-RESUME-NODE" and not _silent and _code in _emitted,
       _code if _code and not _silent else f"code {_code!r}; not named in {_silent}")
_unknown = "RESUME-REACH-UNKNOWN"
report("and the refusal for a resume that does not say what its source reached",
       _unknown in _emitted and _unknown in SKILL and _unknown in _resume_docs["structured resume"],
       _unknown if _unknown in _emitted else "NOT EMITTED")
_unflagged = dict(_review, nodes=[{k: v for k, v in dict(_review["nodes"][0], contract="RedTeamV1").items()
                                   if k != "resume"}])
_skill_flat = re.sub(r"\s+", " ", SKILL)
report("a red-team fan-out without the field is warned about, as the graph reference says",
       any('"resume": "never"' in w for w in _cg.warnings(_unflagged))
       and "`--check` warns about a `RedTeamV1` fan-out that omits it" in _skill_flat,
       "warned")


def _help(script):
    return subprocess.run([sys.executable, os.path.join(PLUGIN, "scripts", script), "--help"],
                          capture_output=True, text=True, encoding="utf-8").stdout


_recorder_help = _help("record-run.py")
_veto_docs = [name for name, text in (("graph-run", _resume_docs["graph-run"]), ("graph reference", SKILL))
              if "--veto" not in text or "--veto-evidence" not in text]
report("the veto flags the documents name are flags record-run.py takes",
       "--veto " in _recorder_help and "--veto-evidence" in _recorder_help and not _veto_docs,
       "--veto, --veto-evidence" if not _veto_docs else f"MISSING in {_veto_docs}")
_spec_help = _help("specification.py")
_format = read("references", "spec-format.md")
report("the retire flags the packet format names are flags specification.py takes",
       all(flag in _spec_help and flag in _format for flag in ("--retire", "--because")),
       "--retire, --because")
_cap = _cg.emit({"version": 1, "campaign": "c", "treeGuard": False,
                 "budget": {"maxNodes": 2, "maxEstimatedTokens": 900000},
                 "nodes": [{"id": "one", "prompt": "p", "contract": "DesignV1"}]},
                _cg.load_contracts(os.path.join(PLUGIN, "contracts")), {})
report("the token cap the graph reference says holds the run is in the compiled script",
       "const MAX_TOKENS = 900000" in _cap and "estimatedSpend" in _cap and "`estimatedSpend`" in SKILL
       and "The cap holds the run as well as the plan" in _skill_flat,
       "MAX_TOKENS, estimatedSpend")


def _hygiene(path, line):
    return subprocess.run([sys.executable, os.path.join(PLUGIN, "scripts", "hygiene-filter.py"), path], input=line,
                          capture_output=True, text=True, encoding="utf-8", cwd=tempfile.gettempdir()).stdout


_sorry = "sorry, the file moved\n"
report("sorry is flagged in Lean and Isabelle files only, as the plugin README says",
       not _hygiene("notes.py", _sorry) and bool(_hygiene("Proof.lean", "  sorry\n"))
       and bool(_hygiene("Proof.thy", "  sorry\n")) and "flagged in `.lean` and `.thy` files only" in PLUGIN_README,
       ".lean and .thy")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
