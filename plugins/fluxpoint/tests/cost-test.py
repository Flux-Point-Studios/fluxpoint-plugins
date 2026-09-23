#!/usr/bin/env python3
"""Budget as cost, not calls (issues #64, #65, #66, #67).

`budget.maxNodes` counted agent calls, and a campaign could pass it while
costing several times what a larger campaign would: raising a node's effort
was free to the check, and a fan-out that broke the prompt cache on every
hop paid full prefill where a warm prefix would have cost a tenth. These
cases pin the estimate the compiler now prices every graph with, the
ceiling on it, the TTL the graph declares, the transition warnings, and the
instrumentation a later effort sweep needs — executed against the compiler,
the emitted script, and metrics.py.
"""
import copy
import shutil
import importlib.util
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
spec = importlib.util.spec_from_file_location(
    "compile_graph", os.path.join(PLUGIN, "scripts", "compile-graph.py"))
cg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cg)
CONTRACTS = cg.load_contracts(os.path.join(PLUGIN, "contracts"))
passed = failed = 0


def report(name, ok, detail):
    global passed, failed
    print(f"{'PASS' if ok else 'FAIL'}  {name:<60} -> {detail}")
    passed, failed = (passed + ok, failed + (not ok))


def chain(*efforts, ttl=None, model=None):
    """A serial chain of nodes at the given efforts (None = inherit)."""
    ir = {"version": 1, "campaign": "c", "budget": {"maxNodes": 50},
          "defaults": {"effort": "medium"}, "treeGuard": False, "nodes": []}
    if ttl:
        ir["budget"]["cacheTtl"] = ttl
    for i, e in enumerate(efforts):
        n = {"id": f"n{i}", "prompt": f"step {i}" + (" {{prev}}" if i else ""),
             "contract": "DesignV1"}
        if i:
            n["after"] = f"n{i - 1}"
        if e:
            n["effort"] = e
        if model:
            n["model"] = model
        ir["nodes"].append(n)
    return ir


def est(ir):
    return cg.estimate_tokens(ir, CONTRACTS)


# ==================== effort is priced ===================================
low, med, high, mx = (est(chain(e))["total"] for e in ("low", "medium", "high", "max"))
report("the estimate rises with effort", low < med < high < mx, f"{low} < {med} < {high} < {mx}")
report("max costs about 3.5x low, prefix included",
       2.5 <= mx / low <= 4.5, f"{mx / low:.1f}x")

# ==================== the prompt cache is priced =========================
cold = est(chain("medium", "medium", "medium"))
warm = est(chain("medium", "medium", "medium", ttl="1h"))
report("a same-effort chain is cheaper under a 1-hour TTL", warm["total"] < cold["total"],
       f"{warm['total']} < {cold['total']}")
report("  because only the first hop is a cold prefill",
       warm["cold"] == 1 and cold["cold"] == 3, f"cold {warm['cold']} vs {cold['cold']}")
# medium -> high -> medium under 1h: the high hop is cold, and the medium
# prefix the first node warmed is still alive for the third — two cache
# entries, one per (model, effort) key, both within the hour.
mixed = est(chain("medium", "high", "medium", ttl="1h"))
report("an effort transition is a cold prefill even under 1h",
       mixed["cold"] == 2 and mixed["total"] > warm["total"], f"cold {mixed['cold']}")

# Siblings that dispatch together share the prefix under any TTL.
fan = {"version": 1, "campaign": "c", "budget": {"maxNodes": 50}, "treeGuard": False,
       "lists": {"la": [{"key": "a"}, {"key": "b"}, {"key": "c"}]},
       "nodes": [{"id": "f", "foreach": "la", "prompt": "do {{item.key}}",
                  "contract": "DesignV1"}]}
r = est(fan)
report("a fan-out's siblings share one cold prefill", r["cold"] == 1 and r["calls"] == 3,
       f"cold {r['cold']} of {r['calls']}")
empty_fan = copy.deepcopy(fan)
empty_fan["lists"]["la"] = []
try:
    ok_empty = cg.effort_transitions(empty_fan) == [] and est(empty_fan)["calls"] == 0
except Exception as e:  # noqa: BLE001
    ok_empty = False
report("an empty foreach list prices and walks without a crash", ok_empty, "no calls")

# A park is cold whatever the TTL: nothing survives the hours a person takes.
parked = chain("medium", "medium", "medium", ttl="1h")
parked["nodes"][1].update(actor="human", release={
    "instructions": "sign it", "whyNotAgent": "the key is on hardware no agent holds",
    "proofContract": "DesignV1"})
parked["nodes"][1].pop("effort", None)
r = est(parked)
# The advisor runs at medium right after n0, so it is warm; the node after
# the park is cold again even though its key was warmed before it. Without
# the park the same three-node chain has one cold prefill.
report("a park resets the warm prefix under 1h",
       r["cold"] == 2 and est(chain("medium", "medium", "medium", ttl="1h"))["cold"] == 1,
       f"cold {r['cold']} with a park, 1 without")
# Only what waits on the park waits for the release. A node that hangs off
# the node BEFORE the park spawns in the same run, right after the advisor:
# its medium prefix is still warm, and pricing it cold was the review's
# finding against #92.
beside = copy.deepcopy(parked)
beside["nodes"][2]["after"] = "n0"
report("  but a node that does not wait on the park keeps the run's warm prefix",
       est(beside)["cold"] == 1, f"cold {est(beside)['cold']}")
report("  and held_by names only the parks a node waits on",
       cg.held_by(parked)["n2"] == frozenset({"n1"})
       and cg.held_by(beside)["n2"] == frozenset(), "after-chain")

# ==================== refuters, sentinels and models are priced ==========
plain = {"version": 1, "campaign": "c", "budget": {"maxNodes": 50}, "treeGuard": False,
         "nodes": [{"id": "f", "prompt": "find", "contract": "FindingsV1"}]}
panel = copy.deepcopy(plain)
panel["nodes"][0].update(verify="panel:3", verifyOver="findings", expectItems=2)
rp, rr = est(plain), est(panel)
report("a panel adds its refuters to the estimate", rr["calls"] == 7 and rr["total"] > rp["total"],
       f"{rr['calls']} calls")
report("  at low effort (cheap skeptics)",
       rr["perNode"]["f"]["estimatedTokens"] - rp["perNode"]["f"]["estimatedTokens"]
       < 6 * cg.WORK_TOKENS, "low")
guarded = copy.deepcopy(plain)
guarded.pop("treeGuard")
rg = est(guarded)
tc = rg["perNode"].get("tree-check") or {"calls": 0, "estimatedTokens": 0}
report("the tree sentinels are priced, cheaply",
       rg["calls"] == 3 and tc["calls"] == 2
       and tc["estimatedTokens"] / tc["calls"] < rg["perNode"]["f"]["estimatedTokens"],
       f"{rg['calls']} calls, sentinel {tc['estimatedTokens'] // max(tc['calls'], 1)} each")
rh = est(chain("high", model="claude-haiku-4-5-20251001"))
report("a cheaper model weights the call down", rh["total"] < est(chain("high"))["total"],
       f"{rh['total']}")
report("the assumptions ride out with the number",
       est(plain)["assumptions"]["effortMult"] == cg.EFFORT_MULT, "stated")

# ==================== the ceiling refuses, like maxNodes =================

packet = {"packet": {"goal": "x" * 4000 + "\u20ac" * 20}, "identity": {"sha256": "a" * 64}}
repeated = copy.deepcopy(fan)
repeated["nodes"][0]["repeat"] = {"maxRounds": 3}
reduced = copy.deepcopy(plain)
reduced["nodes"].append({"id": "reduce", "reduce": {"from": "f", "over": "findings", "dedupeBy": ["title"]}})
for name, shape, charged in [("workers", plain, 1), ("refuters", panel, 7),
                             ("fan-out", fan, 3), ("repeat", repeated, 9),
                             ("advisors", parked, 3), ("sentinel exclusion", guarded, 1),
                             ("reducer exclusion", reduced, 1)]:
    with_spec = cg.estimate_tokens(shape, CONTRACTS, packet)
    delta = with_spec["total"] - est(shape)["total"]
    input_cost = with_spec["assumptions"]["specificationInputTokens"]
    report(f"spec input covers {name}", delta == charged * input_cost and input_cost > 4000,
           f"{charged} calls charged {delta}")
weighted = chain("high", model="claude-haiku-4-5-20251001")
weighted_spec = cg.estimate_tokens(weighted, CONTRACTS, packet)
# Within one token: each total is rounded once, and the prompt's own bytes
# now carry a fractional haiku weight into both sides of the difference.
report("spec input retains model price weighting",
       abs(weighted_spec["total"] - est(weighted)["total"]
           - weighted_spec["assumptions"]["specificationInputTokens"] * 0.25) <= 1,
       "haiku input weighted without effort scaling")
named_sentinel = copy.deepcopy(plain)
named_sentinel["nodes"][0]["id"] = "tree-check"
report("a user node named tree-check is priced as work",
       cg.estimate_tokens(named_sentinel, CONTRACTS, packet)["total"] == cg.estimate_tokens(plain, CONTRACTS, packet)["total"],
       "only actual sentinels omit the packet")
bounded = copy.deepcopy(plain)
total = cg.estimate_tokens(bounded, CONTRACTS, packet)["total"]
bounded["budget"]["maxEstimatedTokens"] = total
report("the exact spec-aware ceiling is accepted", not cg.validate(bounded, CONTRACTS, specification=packet), str(total))
bounded["budget"]["maxEstimatedTokens"] = total - 1
report("one token under the spec-aware estimate is rejected",
       any("maxEstimatedTokens" in finding for finding in cg.validate(bounded, CONTRACTS, specification=packet)), str(total - 1))
over = chain("high", "high")
over["budget"]["maxEstimatedTokens"] = 1000
e = cg.validate(over, CONTRACTS)
report("an estimate over maxEstimatedTokens is rejected",
       any("estimated at" in x and "maxEstimatedTokens" in x for x in e),
       e[0][:60] if e else "ACCEPTED")
under = chain("high", "high")
under["budget"]["maxEstimatedTokens"] = 10_000_000
report("and one under it compiles", not cg.validate(under, CONTRACTS), "accepted")
bad = chain("high")
bad["budget"]["cacheTtl"] = "forever"
e = cg.validate(bad, CONTRACTS)
report("a cacheTtl the runtime does not offer is rejected",
       any("cacheTtl must be" in x for x in e), e[0][:50] if e else "ACCEPTED")

# ==================== the prompt text is priced (issue #93) ==============
# The node prompt is what the compiler embeds in every call, and it is the
# one input an author changes between audit rounds. Priced at one token per
# UTF-8 byte, like the packet: conservative on purpose.
def single(prompt, **extra):
    ir = {"version": 1, "campaign": "c", "budget": {"maxNodes": 50}, "treeGuard": False,
          "nodes": [{"id": "f", "prompt": prompt, "contract": "DesignV1"}]}
    ir.update(extra)
    return ir


base = est(single("do the thing"))["total"]
grown = est(single("do the thing" + "x" * 5000))["total"]
report("a longer prompt raises the estimate by its bytes", grown - base == 5000,
       f"+{grown - base} for +5000 bytes")
report("  and an em-dash costs its three UTF-8 bytes",
       est(single("do the thing" + "—" * 10))["total"] - base == 30, "30")
cons = single("apply {{A.constraints}} here", argDefaults={"constraints": "c" * 3000})
nocons = single("apply {{A.constraints}} here")
report("an {{A.x}} expansion is priced at its argDefault",
       est(cons)["total"] - est(nocons)["total"] == 3000, "argDefaults")
report("  and a runtime expansion ({{prev}}, {{seen}}) is named as unpriced",
       "prev" in est(nocons)["assumptions"]["unpricedExpansions"], "stated")
haiku = single("do the thing" + "x" * 4000)
haiku["nodes"][0]["model"] = "claude-haiku-4-5-20251001"
haiku0 = single("do the thing")
haiku0["nodes"][0]["model"] = "claude-haiku-4-5-20251001"
report("prompt bytes carry the model's price weight",
       est(haiku)["total"] - est(haiku0)["total"] == 1000, "haiku 0.25")
# Siblings of one fan-out send the same text up to the first item token, so
# that prefix is a cache read for every worker after the first; what
# follows the item token differs per worker and is paid in full each time.
fan_pre = copy.deepcopy(fan)
fan_pre["nodes"][0]["prompt"] = "y" * 1000 + "do {{item.key}}"
fan_post = copy.deepcopy(fan)
fan_post["nodes"][0]["prompt"] = "do {{item.key}}" + "y" * 1000
d_pre = est(fan_pre)["total"] - est(fan)["total"]
d_post = est(fan_post)["total"] - est(fan)["total"]
report("a fan-out's shared prompt prefix is paid once, then read from cache",
       d_pre == 1000 + 2 * 100, f"+{d_pre}")
report("  and text after the item token is paid by every worker", d_post == 3000, f"+{d_post}")
# Warm prompt text is priced at a tenth, so a call's cost is fractional; the
# headline must still be the sum of the per-node profile beside it.
odd = copy.deepcopy(fan)
odd["nodes"][0]["prompt"] = "y" * 1003 + "do {{item.key}}"
odd["nodes"].append({"id": "g", "foreach": "la", "prompt": "z" * 7 + "{{item.key}}",
                     "contract": "DesignV1", "after": "f"})
e_odd = est(odd)
report("the estimate is the sum of its per-node profile",
       e_odd["total"] == sum(r["estimatedTokens"] for r in e_odd["perNode"].values()),
       f"{e_odd['total']} vs {sum(r['estimatedTokens'] for r in e_odd['perNode'].values())}")
# The estimator prices {{item}} as the script renders it, and a bare
# {{item}} over objects is rendered as JSON — it used to reach the worker as
# "[object Object]", no item data at all, while being priced as the object.
objs = {"version": 1, "campaign": "c", "budget": {"maxNodes": 50}, "treeGuard": False,
        "argDefaults": {"cfg": "x"},
        "lists": {"la": [{"key": "a", "arr": [1, 2], "o": {"k": "é"}, "n": 3, "b": True}]},
        "nodes": [{"id": "f", "foreach": "la", "contract": "DesignV1",
                   "prompt": "<{{item}}|{{item.arr}}|{{item.o}}|{{item.n}}|{{item.b}}"
                             "|{{item.missing}}|{{A.cfg}}>"}]}
item0 = objs["lists"]["la"][0]
expect = ("<" + json.dumps(item0, ensure_ascii=False, separators=(",", ":")) + "|[1,2]|"
          + '{"k":"é"}|3|true|undefined|x>')
shared, varying = cg.prompt_bytes([objs["nodes"][0]["prompt"]], objs, item0, 0)
report("an item token is priced as the bytes the script renders",
       shared + varying == len(expect.encode("utf-8")),
       f"{shared + varying} vs {len(expect.encode('utf-8'))}")
js_objs = cg.emit(objs, CONTRACTS, {})
report("  and the script renders item and A tokens through tokenText",
       "${tokenText(item)}" in js_objs and "${tokenText(A.cfg)}" in js_objs
       and "function tokenText(v)" in js_objs, "bound")
if shutil.which("node"):
    fn = next(l for l in js_objs.splitlines() if l.startswith("function tokenText(v)"))
    probe = (fn + "\nconst item = " + json.dumps(item0) + ", A = {cfg: 'x'};\n"
             "process.stdout.write(" + cg.js_template(objs["nodes"][0]["prompt"]) + ")\n")
    r = subprocess.run(["node", "-e", probe], capture_output=True, text=True, encoding="utf-8")
    report("  and node renders exactly what was priced", r.stdout == expect,
           r.stdout[:60] or r.stderr[:60])
# What the estimate cannot size, it names — with the nodes that use it.
blind = single("do {{A.goal}} with {{A.cfg}}", requiredArgs=["goal"], argDefaults={"cfg": "c"})
blind["nodes"].append({"id": "g", "prompt": "then {{prev}}", "contract": "DesignV1",
                       "after": "f"})
report("a launch arg with no default and a run-time {{prev}} are named as unpriced",
       dict(cg.unpriced_expansions(blind)) == {"{{A.goal}} (launch arg)": ["f"],
                                               "{{prev}}": ["g"]},
       str(cg.unpriced_expansions(blind)))
report("  and the compiled header names them",
       "// unpriced: {{A.goal}} (launch arg) in f; {{prev}} in g" in cg.emit(blind, CONTRACTS, {}),
       "header")
with tempfile.TemporaryDirectory() as d:
    work = os.path.join(d, "WORK.md")
    with open(work, "w", encoding="utf-8") as fh:
        fh.write("# w\n\n```json graph-ir\n" + json.dumps(blind) + "\n```\n")
    r = subprocess.run([sys.executable, os.path.join(PLUGIN, "scripts", "compile-graph.py"),
                        work, "--check", "--gates-root", d], capture_output=True, text=True)
    report("  and so does --check",
           "unpriced: {{A.goal}} (launch arg) in f; {{prev}} in g" in r.stdout,
           r.stdout.strip()[-80:] or r.stderr.strip()[-80:])
two = chain("medium", "medium", ttl="1h")
two["nodes"][1]["prompt"] += " " + "z" * 2000
d_two = est(two)["total"] - est(chain("medium", "medium", ttl="1h"))["total"]
report("a warm (model, effort) key does not discount a different node's prompt",
       d_two == 2001, f"+{d_two}")
adv = copy.deepcopy(parked)
adv["nodes"][1]["release"]["instructions"] += " " + "w" * 1500
d_adv = est(adv)["total"] - est(parked)["total"]
report("a park's advisor prices the release instructions it is handed",
       d_adv == 1501, f"+{d_adv}")
with tempfile.TemporaryDirectory() as d:
    lines = []
    for extra in ("", " " + "q" * 7000):
        work = os.path.join(d, "WORK.md")
        with open(work, "w", encoding="utf-8") as fh:
            fh.write("# w\n\n```json graph-ir\n"
                     + json.dumps(single("look" + extra, budget={"maxNodes": 5}))
                     + "\n```\n")
        r = subprocess.run([sys.executable, os.path.join(PLUGIN, "scripts", "compile-graph.py"),
                            work, "--check", "--gates-root", d], capture_output=True, text=True)
        lines.append(r.stdout)
    report("--check prints a different estimate when only a prompt grew",
           lines[0] != lines[1] and "estimated tokens" in lines[1], lines[1].strip()[-60:])

# ==================== warnings name the transitions ======================
w = cg.warnings(chain("medium", "high", "medium", ttl="1h"))
report("consecutive effort changes are warned about",
       any("transition" in x and "'n0'(medium) -> 'n1'(high)" in x for x in w),
       next((x[:60] for x in w if "transition" in x), "SILENT"))
# Under 1h the change back to medium reads the prefix n0 warmed: the
# estimator charges it warm, so the warning must not call it cold.
report("  but a return to a key the run already warmed is not",
       cg.effort_transitions(chain("medium", "high", "medium", ttl="1h"))
       == [("n0", ("", "medium"), "n1", ("", "high"))],
       str(cg.effort_transitions(chain("medium", "high", "medium", ttl="1h"))))
w = cg.warnings(chain("medium", "high"))
report("under the default TTL the warning says hops are cold anyway",
       any("transition" in x and "5-minute" in x for x in w), "cold anyway")
report("a same-effort chain has no transition to warn about",
       not any("transition" in x for x in cg.warnings(chain("medium", "medium"))), "silent")
# Issue #92: the estimator prices a park as a cold start, so a transition
# placed across one costs nothing extra — the placement the warning itself
# recommends. The warning walks the same sequence the estimator prices.
across = chain("medium", "medium", "high", ttl="1h")
across["nodes"][1].update(actor="human", release={
    "instructions": "sign it", "whyNotAgent": "the key is on hardware no agent holds",
    "proofContract": "DesignV1"})
across["nodes"][1].pop("effort", None)
report("a transition across a park is not reported (priced cold already)",
       not cg.effort_transitions(across)
       and not any("transition" in x for x in cg.warnings(across)),
       str(cg.effort_transitions(across)))
thirdp = copy.deepcopy(across)
thirdp["nodes"][1]["actor"] = "third-party"
report("  nor across a third-party park", not cg.effort_transitions(thirdp), "silent")
beside_t = copy.deepcopy(across)
beside_t["nodes"][2]["after"] = "n0"
report("  but a node that does not wait on the park is judged against the node before it",
       [(a, b) for a, _, b, _ in cg.effort_transitions(beside_t)] == [("n0", "n2")]
       and any("transition" in x for x in cg.warnings(beside_t)),
       str(cg.effort_transitions(beside_t)))
report("  while the same change with no park between is still warned",
       any("transition" in x for x in cg.warnings(chain("medium", "medium", "high", ttl="1h"))),
       "warned")
redundant = chain(None)
redundant["roles"] = {"r": {"effort": "high"}}
redundant["nodes"][0].update(role="r", effort="high")
w = cg.warnings(redundant)
report("an inline effort equal to its role's is warned as changing nothing",
       any("changes nothing" in x for x in w), next((x[:50] for x in w), "SILENT"))
w = cg.warnings(fan)
report("a fan-out with no cacheTtl is told the 5-minute cache expires",
       any("declares no budget.cacheTtl" in x for x in w), "warned")
fan_ttl = copy.deepcopy(fan)
fan_ttl["budget"]["cacheTtl"] = "1h"
report("  and is quiet once it declares 1h",
       not any("declares no budget.cacheTtl" in x for x in cg.warnings(fan_ttl)), "quiet")

# ==================== the emitted script carries the estimate ============
js = cg.emit(fan_ttl, CONTRACTS, {})
report("the compiled header names the estimate and TTL",
       "// estimate: ~" in js and "prompt-cache TTL 1h" in js, "header")
report("ESTIMATE and PROFILE reach the run summary",
       "estimate: ESTIMATE, profile: PROFILE" in js and "const PROFILE = " in js, "summary")
report("the launch log names the TTL the graph was written for",
       "declares a 1-hour TTL" in js, "logged")
js5 = cg.emit(fan, CONTRACTS, {})
report("  and under 5m says every hop is priced cold", "default 5-minute TTL" in js5, "logged")
# And it is valid JavaScript under the runtime's wrapper, as harness.sh checks.
with tempfile.TemporaryDirectory() as d:
    p = os.path.join(d, "w.mjs")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("const agent=0,parallel=0,pipeline=0,log=0,phase=0,args=0,budget=0,workflow=0;"
                 "(async () => {\n" + js.replace("export const meta", "const meta") + "\n})()\n")
    r = subprocess.run(["node", "--check", p], capture_output=True, text=True)
    report("the emitted script is still valid JavaScript", r.returncode == 0, r.stderr[:60] or "ok")

# --check prints the estimate beside the call count.
with tempfile.TemporaryDirectory() as d:
    work = os.path.join(d, "WORK.md")
    ir = chain("medium", "high", ttl="1h")
    ir["budget"]["maxEstimatedTokens"] = 500000
    with open(work, "w", encoding="utf-8") as fh:
        fh.write("# w\n\n```json graph-ir\n" + json.dumps(ir) + "\n```\n")
    r = subprocess.run([sys.executable, os.path.join(PLUGIN, "scripts", "compile-graph.py"),
                        work, "--check", "--gates-root", d], capture_output=True, text=True)
    report("--check prints the estimate, the ceiling and the TTL",
           "estimated tokens of 500,000 allowed" in r.stdout and "TTL 1h" in r.stdout,
           r.stdout.strip()[-80:])

# ==================== metrics folds estimate against spent ===============
with tempfile.TemporaryDirectory() as d:
    runs = os.path.join(d, ".claude", "fluxpoint", "runs")
    os.makedirs(runs)
    art = {"runId": "wf_c1", "when": "2026-09-09 00:00", "outcome": "COMPLETE",
           "nodesOk": 2, "nodesDead": 0, "nodesSkipped": 0,
           "summary": {"campaign": "priced", "provenance": [], "spawned": 3, "planned": 3,
                       "spent": 90000,
                       "estimate": {"total": 60000, "calls": 3, "cold": 1, "cacheTtl": "1h"},
                       "profile": {"a": {"effort": "high", "model": "default", "calls": 1,
                                         "estimatedTokens": 39000},
                                   "b": {"effort": "low", "model": "default", "calls": 2,
                                         "estimatedTokens": 21000}}}}
    with open(os.path.join(runs, "wf_c1.json"), "w", encoding="utf-8") as fh:
        json.dump(art, fh)
    r = subprocess.run([sys.executable, os.path.join(PLUGIN, "scripts", "metrics.py"),
                        "--root", d, "--json"], capture_output=True, text=True)
    folded = json.loads(r.stdout)["campaigns"]["priced"]
    report("metrics folds the estimate beside spent",
           folded.get("estimated") == 60000 and folded.get("spent") == 90000, "folded")
    report("and the effort mix per call",
           folded.get("effortMix") == {"high": 1, "low": 2}, str(folded.get("effortMix")))
    r = subprocess.run([sys.executable, os.path.join(PLUGIN, "scripts", "metrics.py"),
                        "--root", d], capture_output=True, text=True)
    report("and renders spent over estimated", "spent/estimated 1.50x" in r.stdout
           and "effort mix: high 1 call(s), low 2 call(s)" in r.stdout, "rendered")

# ==================== the shipped templates are priced and bounded =======
import glob
import re
REPO = os.path.dirname(os.path.dirname(PLUGIN))
GATES = cg.load_gates(REPO)
for t in sorted(glob.glob(os.path.join(PLUGIN, "templates", "WORK*.md"))):
    src = open(t, encoding="utf-8").read()
    m = cg.IR_FENCE.search(src)
    if not m:
        continue
    tir = json.loads(m.group(1))
    b = tir.get("budget") or {}
    name = os.path.basename(t)
    report(f"{name} declares cacheTtl 1h", b.get("cacheTtl") == "1h", str(b.get("cacheTtl")))
    cap = b.get("maxEstimatedTokens")
    total = est(tir)["total"]
    report(f"{name} declares a cost ceiling the estimate fits with headroom",
           isinstance(cap, int) and total <= cap <= total * 2, f"{total} <= {cap}")
    report(f"{name} carries no redundant inline effort",
           not any("changes nothing" in x for x in cg.warnings(tir)), "clean")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
