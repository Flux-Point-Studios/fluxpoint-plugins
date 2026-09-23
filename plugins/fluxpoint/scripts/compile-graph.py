#!/usr/bin/env python3
"""Compile a WORK.md declarative IR block into a Claude Code Workflow script.

The graph-ir block in WORK.md is the single source of truth. This compiler
is deterministic and has
no model in the loop, which is the point: the spec cannot drift from the
script, because the script is generated from the spec.

Usage:
  compile-graph.py WORK.md -o .claude/workflows/campaign.graph.js
  compile-graph.py WORK.md --check       # validate only, emit nothing

Exit 0 = green. Any invariant violation exits 1 with the findings on stderr.
Standard library only; no third-party dependencies.
"""
import argparse
import json
import math
import os
import re
import sys

IR_FENCE = re.compile(r"```json\s+graph-ir\s*\n(.*?)\n```", re.S)
# 'harness' is deliberately absent: it was accepted, priced into the budget,
# and emitted nothing. Verifying that something is green is what
# mutates + independent already does, properly. See validate().
TIER = re.compile(r"^(schema-only|skeptic:(\d+)|panel:(\d+)|prove:([a-z][a-z0-9-]*))$")
GATES = ".fluxpoint-gates.json"
HALT = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*(==|!=|>=|<=|>|<)\s*(-?\d+|'[^']*')\s*$")
# Hyphens are in the class because decision ids are kebab-case by rule, so
# {{decisions.vault-window}} has to be a token the substituter can see. An
# unrecognised root is a compile error either way, so widening this cannot
# turn a literal into a silent expression.
SUBST = re.compile(r"\{\{\s*([A-Za-z0-9_.\-\[\]]+)\s*\}\}")
IDENT = re.compile(r"^[a-z][a-z0-9-]*$")

# Every key the IR may carry, by level. An unknown key is a compile error
# rather than a no-op, because the failure this closes is a field that looks
# honored and is silently inert — a misspelled `verifyOver` used to disable
# verification while the spec still claimed it. tests/emission-test.py holds
# these registries to the second half of the promise: every field listed here
# must demonstrably change what the compiler produces.
IR_FIELDS = {
    "version", "name", "campaign", "budget", "defaults", "roles", "lists",
    "nodes", "requiredArgs", "argDefaults", "imports", "treeGuard",
}
NODE_FIELDS = {
    "id", "phase", "prompt", "contract", "role", "effort", "model", "agentType",
    "foreach", "after", "mutates", "independent", "verifies", "verify",
    "verifyOver", "expectItems", "haltWhen", "haltReason", "onRed", "isolation",
    "repeat", "irreversible", "actor", "release", "wake", "decides", "honors",
    "memory", "reduce",
}
# The two failure policies. Anything else used to fall back to a default
# silently — a misspelled 'Halt' weakened the declared policy in the
# permissive direction, which is the exact failure closed registries kill.
ON_RED = {"halt", "drop+log"}
# isolation modes. True and 'worktree' are the runtime's worktree (the
# compiler emits the literal and the runtime picks the base — see the
# graph-engineering skill for what that does NOT promise). 'measure' is a
# frozen `git archive HEAD` snapshot the node creates itself in the shared
# cwd, so a fan-out of measuring nodes stops being a function of what every
# sibling is doing — and the base is the campaign's by construction.
ISOLATION = {True, "worktree", "measure"}
ACTORS = {"agent", "human", "third-party"}
RELEASE_FIELDS = {"instructions", "proofContract", "whyNotAgent"}
WAKE_FIELDS = {"check", "everyMinutes", "deadline"}
REPEAT_FIELDS = {"untilDryRounds", "maxRounds", "dedupeBy"}
# A reduce node is deterministic code between agents: dedupe, rank, cut.
# Use models for ambiguity and code for plumbing — a synthesis node that
# receives every raw fan-out item pays a reasoning model to do a Set's job.
REDUCE_FIELDS = {"from", "over", "dedupeBy", "sortBy", "order", "topK"}
MEMORY_FIELDS = {"seed", "emit", "key", "priors", "classBy"}
BUDGET_FIELDS = {"maxNodes", "verifyFloorTokens", "nodeFloorTokens",
                 "maxEstimatedTokens", "cacheTtl"}
ROLE_FIELDS = {"agentType", "effort", "model"}
DEFAULTS_FIELDS = {"effort", "model"}
# The prompt-cache time-to-live the graph is written for. The runtime sets
# it per session, never per agent call, so the IR can only declare what it
# needs and price accordingly: under the default five minutes a parent that
# blocks on a fan-out or a park outlives its own cache, so every hop is
# priced as a cold prefill; under an hour, consecutive calls at the same
# (model, effort) share a warm prefix.
CACHE_TTLS = {"5m": 300, "1h": 3600}
DEFAULT_CACHE_TTL = "5m"

# ---------------------------------------------------------------- cost model
# STATED ASSUMPTIONS, not measurements. `budget.maxNodes` counts agent calls,
# and spend is dominated by two things a call count cannot see: whether the
# prefix was a cache read or a cold prefill, and how much the model
# deliberated. These constants turn a plan into an estimate in cold-input-
# token equivalents, so an effort bump or a broken cache shows up at design
# time as a number the ceiling can refuse. Every one of them is a guess
# stated in one place; `metrics.py` folds `estimate` against the runtime's
# own `spent` per run, which is how the guesses get corrected.
PREFIX_TOKENS = 12000          # the shared prefix every spawned agent re-reads
CACHED_PREFIX_FRACTION = 0.1   # a cache read costs about a tenth of a prefill
WORK_TOKENS = 15000            # a medium-effort call's own reading and output
SENTINEL_WORK_TOKENS = 1500    # a tree sentinel runs two git commands and returns
EFFORT_MULT = {"low": 0.5, "medium": 1.0, "high": 1.8, "xhigh": 2.5, "max": 3.5}
# Price weight by model family, matched on a substring of the model id;
# anything unmatched is the session's default model at weight 1.
MODEL_MULT = (("haiku", 0.25), ("sonnet", 0.5))
# The text a node's author writes is priced on the packet's basis: one token
# per UTF-8 byte, deliberately conservative. The constants above never read
# a prompt, so a graph under repair could gain kilobytes of prompt per audit
# round while its estimate — and the ceiling set from it — stayed byte-
# identical. Only author-controlled text is priced here; the fixed harness
# wording each call carries is what PREFIX_TOKENS already stands for.
PROMPT_TOKENS_PER_BYTE = 1
# Expansions whose size is only known at run time. They are named in the
# estimate's assumptions rather than guessed, so a reader knows which part
# of a prompt the number cannot see.
UNPRICED_EXPANSIONS = ("prev", "decisions", "seen")
# Tokens whose value differs between calls of ONE node. The prompt cache is
# prefix-keyed, so a fan-out's workers share the text before the first of
# these and nothing after it.
VARYING_ROOTS = {"item", "i", "seen"}
SPEC_PREAMBLE = ("Implement and verify this locked requirement packet. Defaulted decisions are model choices, "
                 "not user authorization. Do not edit the packet, lock or proof baseline to pass a check. "
                 "If a requirement is wrong, return the counterexample for a spec revision.\n")


class GraphError(Exception):
    pass


def _unknown(where, obj, allowed):
    """Findings for keys that are not part of the IR at this level."""
    if not isinstance(obj, dict):
        return []
    return [
        f"{where}: unknown field '{k}' — not part of the IR, so it would be "
        f"silently ignored (known: {', '.join(sorted(allowed))})"
        for k in sorted(set(obj) - allowed)
    ]


# ---------------------------------------------------------------- extraction
def extract_ir(md_text):
    blocks = IR_FENCE.findall(md_text)
    if not blocks:
        raise GraphError(
            "no IR block found — the work file needs one ```json graph-ir fenced block"
        )
    if len(blocks) > 1:
        raise GraphError(f"{len(blocks)} IR blocks found; exactly one is allowed")
    try:
        return json.loads(blocks[0])
    except json.JSONDecodeError as e:
        raise GraphError(f"IR block is not valid JSON: {e}")


def load_contracts(contracts_dir):
    """{$id: schema} for a directory. Two files claiming one $id is an error:
    which one binds used to depend on the loader — the compiler kept the
    last in sorted order, release.py the first — so a park could be released
    on a proof the compiled contract refuses."""
    out, where = {}, {}
    if not os.path.isdir(contracts_dir):
        return out
    for fn in sorted(os.listdir(contracts_dir)):
        if fn.endswith(".schema.json"):
            with open(os.path.join(contracts_dir, fn), encoding="utf-8") as fh:
                schema = json.load(fh)
            cid = schema.get("$id", fn.split(".")[0])
            if cid in where:
                raise GraphError(f"{contracts_dir}: {where[cid]} and {fn} both declare "
                                 f"$id {cid} — one contract name, one file")
            where[cid] = fn
            out[cid] = schema
    return out


def resolve_contracts(shipped_dir, header=None, explicit=None, root="."):
    """(contracts, description) for one compile.

    `--contracts` replaces the set wholesale, as it always has. A graph
    file's `CONTRACTS:` header OVERLAYS the shipped set instead: a repo
    keeps the contracts the plugin does not ship, and any stricter copies,
    in a directory of its own, and everything else — the sentinel's
    TreeCheckV1, the refuters' VerdictV1 — still resolves. A header naming
    a directory that is not there is an error, never a quiet fallback to
    the shipped set: that fallback is exactly the page of false findings
    the header exists to prevent.
    """
    if explicit:
        return load_contracts(explicit), explicit
    shipped = load_contracts(shipped_dir)
    if not header:
        return shipped, "shipped"
    import specification
    rel = str(specification.in_repo(header, "CONTRACTS:"))
    local_dir = os.path.join(root or ".", rel)
    local = load_contracts(local_dir)
    if not local:
        raise GraphError(
            f"CONTRACTS: {header} names no directory of *.schema.json contracts "
            f"under {root or '.'} — the header is honored, so a missing set is an "
            f"error rather than a silent fallback to the shipped contracts")
    replaced = sorted(set(local) & set(shipped))
    merged = dict(shipped)
    merged.update(local)
    desc = (f"shipped + {header} ({len(local) - len(replaced)} added"
            + (f", {len(replaced)} overriding: {', '.join(replaced)}" if replaced else "")
            + ")")
    return merged, desc


RUNS_DIR = os.path.join(".claude", "fluxpoint", "runs")


def _decision_in(artifact, did):
    """The DecisionV1 record for `did` in one run artifact, or None.

    Mirrors record-run.py's filing order: the campaign's own decisions map
    first, then a node whose declared contract is DecisionV1 and whose id is
    the decision id — the same fallback that files a record produced
    without `decides`.
    """
    summary = artifact.get("summary") or {}
    # A decision this run IMPORTED rides in its decisions map as provenance
    # (record-run.py's decision_rows skips it for the same reason). Read as
    # a fresh ruling it was dated by this run and could hide a newer one
    # recorded while the run was parked.
    if did in (summary.get("decisionsImported") or {}):
        return None
    rec = (summary.get("decisions") or {}).get(did)
    if isinstance(rec, dict):
        return rec
    rec = (summary.get("results") or {}).get(did)
    if (summary.get("contracts") or {}).get(did) == "DecisionV1" and isinstance(rec, dict):
        return rec
    return None


def _order_key(when):
    """decision.py's order_key: both sources as 'YYYY-MM-DD HH:MM:SS'.

    A run artifact carries `recordedAt` to the second (a minute-only `when`
    before 1.43) and a stored decision carries seconds; compared as raw
    text, the longer string won every same-minute tie whatever happened
    first, and 'latest' froze the older choice.
    """
    s = str(when or "").replace("T", " ").rstrip("Z").strip()
    return s + ":00" if len(s) == 16 else s


def resolve_imports(ir, contracts, runs_dir, store=None):
    """Resolve the IR's imports from recorded runs and the decision store.

    Returns ({decisionId: {"record": ..., "runId": ...}}, findings).

    This resolution used to be a step the orchestrating agent performed by
    hand while the compiled graph checked only that *some* record arrived —
    which left the most-protected artifact class (frozen decisions) with the
    least-protected loading path: a fabricated or stale map satisfied the
    launch throw. Resolving here and embedding the record means no
    hand-assembled map exists for a launch to trust, and a missing decision
    fails the compile, which is earlier and louder than failing the launch.

    A malformed run artifact is a hard finding, never skipped: what
    'latest' names must not depend on which artifacts happened to parse.

    `store` is decision.py's `decisions.jsonl`. Operator rulings are made in
    chat, not by a graph node, so they never reach a run artifact; without
    the store they could not bind a later campaign at all. 'latest' spans
    both sources, and a store record is addressed by its recordId.
    """
    imports = ir.get("imports") or {}
    if not imports:
        return {}, []
    f = []
    resolved = {}
    required = (contracts.get("DecisionV1") or {}).get("required") or []
    arts = []  # (when, runId, artifact) — runId from the filename, which is
    # how a run is addressed; an artifact cannot rename itself via its body.
    if os.path.isdir(runs_dir):
        for fn in sorted(os.listdir(runs_dir)):
            if not fn.endswith(".json"):
                continue
            p = os.path.join(runs_dir, fn)
            try:
                with open(p, encoding="utf-8") as fh:
                    art = json.load(fh)
            except (OSError, json.JSONDecodeError) as e:
                f.append(
                    f"imports: {p} is not readable JSON ({e}) — restore or "
                    f"remove the artifact; a malformed run cannot be skipped, "
                    f"because what 'latest' names must not depend on which "
                    f"artifacts happened to parse")
                continue
            if not isinstance(art, dict):
                f.append(f"imports: {p} is not a run artifact (not an object)")
                continue
            arts.append((_order_key(art.get("recordedAt") or art.get("when")),
                         fn[: -len(".json")], art))
    kept = []  # (when, recordId, decisionId, record)
    if store and os.path.exists(store):
        with open(store, encoding="utf-8") as fh:
            for i, line in enumerate(fh, 1):
                if not line.strip():
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError as e:
                    f.append(f"imports: {store}:{i} is not valid JSON ({e}) — a "
                             f"malformed decision record cannot be skipped")
                    continue
                if isinstance(r, dict) and isinstance(r.get("record"), dict):
                    kept.append((_order_key(r.get("when")), str(r.get("recordId") or ""),
                                 r.get("id"), r["record"], i))
    for did in sorted(imports):
        ref = imports[did]
        if ref == "latest":
            # (when, order, source, record): ties in time go to the store
            # record appended last, then to the run id, never to chance.
            hits = []
            for when, rid, art in arts:
                rec = _decision_in(art, did)
                if rec is not None:
                    hits.append((when, (0, 0), rid, rec))
            hits += [(when, (1, i), rid, rec) for when, rid, kid, rec, i in kept if kid == did]
            if not hits:
                f.append(
                    f"imports.{did}: no recorded run in {runs_dir} carries this "
                    f"decision, and decision.py has recorded none — the campaign "
                    f"or the ruling that decides it has to come first; never "
                    f"hand-write a run artifact to get past this")
                continue
            _, _, rid, rec = max(hits, key=lambda h: (h[0], h[1], h[2]))
        elif str(ref).startswith("dec_"):
            hit = next(((rid, rec) for _, rid, kid, rec, _ in kept
                        if rid == ref and kid == did), None)
            if hit is None:
                f.append(f"imports.{did}: record '{ref}' not found in {store or 'the decision store'}")
                continue
            rid, rec = hit
        else:
            art = next((a for _, rid, a in arts if rid == ref), None)
            if art is None:
                f.append(f"imports.{did}: run '{ref}' not found in {runs_dir}")
                continue
            rid, rec = ref, _decision_in(art, did)
            if rec is None:
                f.append(f"imports.{did}: run '{ref}' does not carry this decision")
                continue
        missing = [k for k in required if k not in rec]
        if missing:
            f.append(
                f"imports.{did}: the record in run '{rid}' is missing required "
                f"DecisionV1 field(s) {missing} — a hand-edited artifact does "
                f"not count as a decision")
            continue
        resolved[did] = {"record": rec, "runId": rid}
    return resolved, f


# ---------------------------------------------------------------- validation
AGENT_FM = re.compile(r"\A---\s*\n(.*?)\n---", re.S)


def _fm_value(block, key):
    """One scalar out of a frontmatter block. Not YAML -- these files carry a
    handful of flat `key: value` lines, and a parser dependency for that would
    be a heavier promise than the data."""
    m = re.search(rf"^{re.escape(key)}:\s*(.+?)\s*$", block, re.M)
    if not m:
        return None
    v = m.group(1).strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1]
    return v or None


def load_agents(root="."):
    """Agents this compiler can SEE, name -> {"contract": str|None, "from": str}.

    Deliberately partial. Agents also resolve from every installed plugin and
    from the runtime's built-in types, so this set is a lower bound and a name
    missing from it proves nothing -- which is exactly why an unresolvable
    agentType is a warning and never a rejection. What it can prove is the
    opposite: that a name DOES resolve, to an agent whose declared contract
    contradicts the node's.
    """
    out = {}
    here = os.path.dirname(os.path.abspath(__file__))
    plugin_dir = os.path.dirname(here)
    # From the manifest, never from the directory name. A real install lives at
    # ~/.claude/plugins/cache/fluxpoint/fluxpoint/<VERSION>/, so basename() is
    # the VERSION — and every agent got qualified as "1.27.0:graph-auditor",
    # leaving `fluxpoint:graph-auditor` resolving to nothing. The rejection this
    # loader exists to enable then never fired anywhere the plugin is installed,
    # while passing in this repo, where the basename happens to be the name.
    plugin_name = os.path.basename(plugin_dir)
    try:
        with open(os.path.join(plugin_dir, ".claude-plugin", "plugin.json"),
                  encoding="utf-8") as fh:
            plugin_name = json.load(fh).get("name") or plugin_name
    except (OSError, ValueError):
        pass
    roots = [
        (os.path.join(plugin_dir, "agents"), plugin_name),
        (os.path.join(root, ".claude", "agents"), None),
        (os.path.join(os.path.expanduser("~"), ".claude", "agents"), None),
    ]
    for d, ns in roots:
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".md"):
                continue
            try:
                with open(os.path.join(d, fn), encoding="utf-8") as fh:
                    head = fh.read(4096)
            except OSError:
                continue
            m = AGENT_FM.match(head)
            if not m:
                continue
            block = m.group(1)
            name = _fm_value(block, "name") or fn[:-3]
            rec = {"contract": _fm_value(block, "contract"),
                   "from": os.path.join(d, fn)}
            out.setdefault(name, rec)
            if ns:
                # Plugin agents are addressable either way.
                out.setdefault(f"{ns}:{name}", rec)
    return out


class GateSet(set):
    """Declared gate names, carrying what the witness says of the manifest."""
    findings = ()


def _sibling(name):
    """A script of this directory as a module (attest.py for its manifest
    rules), loaded by path so the compiler needs no sys.path of its own."""
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), name + ".py")
    spec = importlib.util.spec_from_file_location("_fpl_" + name.replace("-", "_"), path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_gates(root):
    """Declared gate names, or None when the repo declares no manifest.

    `ci` is among them when the manifest has a `ci` section: the forge's
    own commit statuses, which attest.py --ci mints a row for. It is the
    one gate no node can run inside a tool call, and the least forgeable
    evidence a merge has.

    The manifest is judged by attest.py's own rules, and its findings ride
    along (`.findings`): a manifest the witness refuses mints no row, so a
    prove: node compiled against it could only ever cite a fabrication —
    and used to compile clean.
    """
    p = os.path.join(root or ".", GATES)
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        out = GateSet()
        out.findings = (f"{GATES} is not readable JSON ({e})",)
        return out
    gates = doc.get("gates") if isinstance(doc, dict) else None
    out = GateSet(gates if isinstance(gates, dict) else ())
    if isinstance(doc, dict) and isinstance(doc.get("ci"), dict):
        out.add("ci")
    try:
        out.findings = tuple(_sibling("attest").load_gates(root or ".")[1])
    except Exception as e:  # noqa: BLE001 - a rule we cannot run is a finding
        out.findings = (f"attest.py could not judge {GATES}: {e}",)
    return out


def prove_gate(n):
    """The gate this node's tier proves against, or None."""
    m = TIER.match(str(n.get("verify", "schema-only")))
    return m.group(4) if m else None


def is_reduce(n):
    return isinstance(n, dict) and n.get("reduce") is not None


def result_shape(n):
    """How a node's RESULTS entry is shaped at run time.

    'object'  — the node's contract object (plain agent node, parked node)
    'items'   — a flat array of judged/kept items (any panel tier, repeat,
                or a reduce node)
    'objects' — an array of whole contract objects (foreach with no panel)

    Downstream consumers need this distinction: {{prev.field}} projection
    and reduce.over only make sense against an object, and are rejected
    against an array — silently reading .field off an array would
    interpolate 'null' and the graph would run on nothing.
    """
    if is_reduce(n):
        return "items"
    if n.get("repeat") or panel_size(n):
        return "items"
    if n.get("foreach"):
        return "objects"
    return "object"


def derived_contract(n, nodes_by_id):
    """The contract whose items flow out of a node, chasing reduce chains."""
    while is_reduce(n):
        n = nodes_by_id.get(n["reduce"].get("from")) or {}
    return n.get("contract")


def derived_item_field(n, nodes_by_id):
    """The contract array field a node's flat items came from, or None."""
    while is_reduce(n):
        over = n["reduce"].get("over")
        if over:
            return over
        n = nodes_by_id.get(n["reduce"].get("from")) or {}
    return n.get("verifyOver")


def _item_props(schema, over):
    return (
        (schema or {}).get("properties", {})
        .get(over, {})
        .get("items", {})
        .get("properties", {})
    )


def _uses_prev(prompt):
    """True when any substitution token consumes the predecessor."""
    return any(t == "prev" or t.startswith("prev.") or t.startswith("prev[")
               for t in SUBST.findall(str(prompt)))


def _validate_reduce(n, where, seen, contracts):
    """Findings for one reduce node. `seen` holds the earlier nodes."""
    f = []
    red = n["reduce"]
    if not isinstance(red, dict):
        return [f"{where}: reduce must be an object"]
    f += _unknown(f"{where} reduce", red, REDUCE_FIELDS)
    stray = sorted(set(n) - {"id", "phase", "reduce"})
    if stray:
        f.append(
            f"{where}: reduce cannot be combined with {', '.join(stray)} — a "
            f"reduce node is deterministic code between agents: no prompt, no "
            f"contract of its own, no tier, no fan-out. Its contract is the "
            f"source node's, and its output is the reduced item array")
    src_id = red.get("from")
    if not src_id or src_id not in seen:
        f.append(f"{where}: reduce.from must name a node defined earlier")
        return f
    src = seen[src_id]
    shape = result_shape(src)
    over = red.get("over")
    src_contract = derived_contract(src, seen)
    item_props = {}
    checkable = False
    if shape == "items":
        if over:
            f.append(
                f"{where}: reduce.over is not allowed — '{src_id}' already "
                f"yields a flat item array (its judged or kept items), so "
                f"there is no contract object to project a field out of")
        fld = derived_item_field(src, seen)
        if src_contract in contracts and fld:
            checkable = True
            item_props = _item_props(contracts[src_contract], fld)
    else:
        fld = over
        if not over:
            f.append(
                f"{where}: reduce.over required — '{src_id}' yields its "
                f"contract {'objects' if shape == 'objects' else 'object'}, "
                f"so the array field being reduced must be named")
        elif src_contract in contracts:
            props = contracts[src_contract].get("properties", {})
            if over not in props:
                f.append(f"{where}: reduce.over '{over}' is not a field of "
                         f"{src_contract}")
            else:
                checkable = True
                item_props = _item_props(contracts[src_contract], over)
    if not any(red.get(k) is not None for k in ("dedupeBy", "sortBy", "topK")):
        f.append(
            f"{where}: reduce declares no operation (dedupeBy, sortBy, topK) "
            f"— an empty reduce reads as configured and does nothing")
    # Keys over items with no declared fields are rejected outright, not
    # waved through: at run time every item would key to String(undefined),
    # dedupe would collapse N distinct items to one, and the note would
    # file the destruction as deduplication. A reduce that reads as
    # configured while doing something else is the exact bug class the
    # closed registries exist to kill.
    no_fields = (
        f"the items of {src_contract}.{fld} declare no fields, so the key "
        f"cannot be validated and at run time every item would read "
        f"String(undefined) — give the contract's items a schema")
    dd = red.get("dedupeBy")
    if dd is not None:
        if (not isinstance(dd, list) or not dd
                or not all(isinstance(k, str) and k for k in dd)):
            f.append(f"{where}: reduce.dedupeBy must be a non-empty list of "
                     f"item field names")
        elif checkable and not item_props:
            f.append(f"{where}: reduce.dedupeBy — {no_fields}; deduping by "
                     f"it would collapse distinct items to one")
        else:
            for k in dd:
                if item_props and k not in item_props:
                    f.append(f"{where}: reduce.dedupeBy '{k}' is not a field "
                             f"of the items being reduced")
    sb = red.get("sortBy")
    if sb is not None:
        if not isinstance(sb, str) or not sb:
            f.append(f"{where}: reduce.sortBy must be an item field name")
        elif checkable and not item_props:
            f.append(f"{where}: reduce.sortBy — {no_fields}; every ordering "
                     f"it produced would be arbitrary")
        elif item_props and sb not in item_props:
            f.append(f"{where}: reduce.sortBy '{sb}' is not a field of the "
                     f"items being reduced")
    order = red.get("order")
    if order is not None:
        if order not in ("asc", "desc"):
            f.append(f"{where}: reduce.order must be 'asc' or 'desc'")
        if sb is None:
            f.append(f"{where}: reduce.order without sortBy orders nothing")
    tk = red.get("topK")
    if tk is not None:
        if not isinstance(tk, int) or tk < 1:
            f.append(f"{where}: reduce.topK must be an integer >= 1")
        if sb is None:
            f.append(
                f"{where}: reduce.topK without sortBy keeps an arbitrary K — "
                f"name the ranking that decides what survives the cut")
    return f


def validate(ir, contracts, gates=None, agents=None, specification=None):
    """Return a list of findings. Empty list means the graph may compile."""
    f = []
    if ir.get("version") != 1:
        f.append("IR version must be 1")
    if "treeGuard" in ir and not isinstance(ir["treeGuard"], bool):
        f.append("treeGuard must be a boolean — it disables the shared-tree "
                 "sentinel, and a truthy non-bool would read as a policy "
                 "while setting none")
    if not ir.get("campaign"):
        f.append("campaign: required, one line naming the goal")

    nodes = ir.get("nodes") or []
    if not nodes:
        f.append("nodes: at least one node required")
    lists = ir.get("lists") or {}
    roles = ir.get("roles") or {}
    seen = {}
    decided = {}          # decision id -> the node that makes it
    imports = ir.get("imports") or {}

    # A lists key is emitted as a JS identifier, so an unconstrained key is
    # arbitrary code in the generated script. Constrain it like a node id.
    for lname in lists:
        if not IDENT.match(str(lname)):
            f.append(
                f"lists: key '{lname}' must be lowercase kebab-case — it is "
                f"emitted as a JS identifier, so anything else is injected code"
            )

    # Strict keys at every level: a typo must fail loudly, never quietly
    # disable the thing it was meant to configure.
    f += _unknown("IR", ir, IR_FIELDS)
    f += _unknown("budget", ir.get("budget"), BUDGET_FIELDS)
    f += _unknown("defaults", ir.get("defaults"), DEFAULTS_FIELDS)
    # A ten-node ceiling forces big campaigns to split, and a split is
    # lossy unless a frozen decision can cross the boundary.
    if imports and not isinstance(imports, dict):
        f.append("imports: must be an object of decisionId -> runId | 'latest'")
    elif imports:
        for k, v in imports.items():
            if not IDENT.match(str(k)):
                f.append(f"imports: key '{k}' must be lowercase kebab-case")
            if not isinstance(v, str) or not v.strip():
                f.append(f"imports.{k}: must be a runId or 'latest'")
    for rname, rbody in (roles or {}).items():
        f += _unknown(f"role '{rname}'", rbody, ROLE_FIELDS)

    for i, n in enumerate(nodes):
        nid = n.get("id", f"<node {i}>")
        where = f"node '{nid}'"
        f += _unknown(where, n, NODE_FIELDS)
        f += _unknown(f"{where} repeat", n.get("repeat"), REPEAT_FIELDS)
        f += _unknown(f"{where} release", n.get("release"), RELEASE_FIELDS)
        f += _unknown(f"{where} wake", n.get("wake"), WAKE_FIELDS)
        if not IDENT.match(str(n.get("id", ""))):
            f.append(f"{where}: id must be lowercase kebab-case")
        if nid in seen:
            f.append(f"{where}: duplicate id")

        # Reduce nodes are deterministic code, not agents: they carry none of
        # the agent-node machinery, so they validate on their own path.
        if n.get("reduce") is not None:
            f += _validate_reduce(n, where, seen, contracts)
            seen[nid] = n
            continue

        if not n.get("prompt"):
            f.append(f"{where}: prompt required")
        if "onRed" in n and n["onRed"] not in ON_RED:
            f.append(
                f"{where}: onRed must be one of {', '.join(sorted(ON_RED))} — "
                f"'{n['onRed']}' would silently fall back to a default, which "
                f"weakens the declared failure policy in the permissive "
                f"direction")

        # Isolation is a closed registry for the same reason onRed is: a
        # misspelled mode used to be truthy, compile to a worktree, and read
        # as the isolation the author asked for while providing a different
        # one. 'measure' is a frozen `git archive HEAD` snapshot the node
        # makes itself — the shape for anything that builds, compiles, or
        # measures without meaning to keep the result.
        if "isolation" in n and n["isolation"] not in ISOLATION:
            f.append(
                f"{where}: isolation must be one of "
                f"{', '.join(repr(i) for i in sorted(ISOLATION, key=str))} — "
                f"'{n['isolation']}' is not a mode, and guessing one would "
                f"hand the node a different tree than the author declared")
        if n.get("mutates") and n.get("isolation") == "measure":
            f.append(
                f"{where}: mutates:true cannot combine with isolation:'measure' "
                f"— a mutator's writes are meant to land, and a measure "
                f"snapshot is thrown away by construction. Pick one.")

        # Contract layer: a node without a contract does not run.
        c = n.get("contract")
        if not c:
            f.append(f"{where}: contract required — a node without a contract cannot be verified")
        elif c not in contracts:
            f.append(f"{where}: unknown contract '{c}' (have: {', '.join(sorted(contracts)) or 'none'})")

        # Verification tier.
        tier = n.get("verify", "schema-only")
        m = TIER.match(str(tier))
        if str(tier) == "harness":
            f.append(
                f"{where}: verify 'harness' was removed — it compiled to nothing "
                f"while the spec claimed the node was checked. To gate on the "
                f"harness, mark the producing node mutates:true and add a node "
                f"with independent:true, verifies:'{nid}', and a haltWhen on its "
                f"real exit code"
            )
        elif not m:
            f.append(f"{where}: verify must be schema-only | skeptic:N | panel:N "
                     f"| prove:<gate>")
        elif m.group(4):
            # prove:<gate> — the claim is checked against an execution a hook
            # recorded, not against refuters who re-read the code. It only
            # means anything if the gate is a real declared command, so the
            # name is resolved here rather than at run time. This is the
            # `verify: harness` lesson: a tier that resolves to nothing must
            # not compile.
            gate = m.group(4)
            if gates is not None and getattr(gates, "findings", ()):
                f.append(
                    f"{where}: verify prove:{gate} rests on {GATES}, which the "
                    f"witness refuses ({gates.findings[0]}) — nothing can mint the "
                    f"row this node must cite until the manifest is fixed")
            elif gates is None:
                f.append(
                    f"{where}: verify prove:{gate} needs a {GATES} manifest "
                    f"declaring which commands decide things — without one the "
                    f"tier resolves to nothing, which is how 'harness' used to "
                    f"pass while checking nobody")
            elif gate not in gates:
                f.append(
                    f"{where}: verify prove:{gate} names no gate in {GATES} "
                    f"(declared: {', '.join(sorted(gates)) or 'none'})"
                    + (" — prove:ci needs a top-level 'ci' section naming the "
                       "forge and the contexts that decide" if gate == "ci" else ""))
            if c != "ExecutionV1":
                f.append(
                    f"{where}: verify prove:{gate} requires contract ExecutionV1 "
                    f"(has '{c}') — the attestId is what makes the exit code "
                    f"checkable, and no other contract carries one")
            if n.get("verifyOver"):
                f.append(f"{where}: verify prove:{gate} verifies the node's own "
                         f"execution, not an array — drop verifyOver")
        else:
            count = m.group(2) or m.group(3)
            if count:
                cnt = int(count)
                if cnt < 1:
                    f.append(f"{where}: verify panel size must be >= 1")
                if m.group(3) and cnt % 2 == 0:
                    f.append(f"{where}: panel:{cnt} is even — majority is undefined; use an odd panel")
                if cnt > 0 and not n.get("verifyOver"):
                    f.append(f"{where}: verify {tier} needs verifyOver naming the array field to verify")

        if n.get("verifyOver") and c in contracts:
            props = contracts[c].get("properties", {})
            if n["verifyOver"] not in props:
                f.append(f"{where}: verifyOver '{n['verifyOver']}' is not a field of {c}")

        # Edge layer: dependencies must already exist (keeps the DAG acyclic
        # and the emission order honest).
        after = n.get("after")
        if after and after not in seen:
            f.append(f"{where}: after '{after}' is not a node defined earlier")
        if after and not _uses_prev(n.get("prompt", "")):
            # Nodes already run in declaration order, so an `after` whose
            # output is never consumed is a phantom edge: it reads as a
            # dependency in the spec and constrains nothing in the run.
            f.append(
                f"{where}: after '{after}' but the prompt never uses {{{{prev}}}} — "
                f"declaration order already sequences nodes, so `after` means "
                f"'consumes that node's contract'. Use {{{{prev}}}} or drop the field"
            )
        if n.get("foreach") and n["foreach"] not in lists:
            f.append(f"{where}: foreach '{n['foreach']}' has no entry under lists")
        if n.get("role") and n["role"] not in roles:
            f.append(f"{where}: role '{n['role']}' is not declared under roles")

        if n.get("haltWhen"):
            if not HALT.match(str(n["haltWhen"])):
                f.append(f"{where}: haltWhen must be '<field> <op> <literal>' (e.g. 'exit != 0')")
            elif panel_size(n):
                # After a panel the node's value is verified items, not its own
                # contract, so the named field is not there to test. Accepting
                # this would emit a halt that can never fire.
                f.append(
                    f"{where}: haltWhen cannot be combined with verify {n.get('verify')} — "
                    f"the node's value after verification is the surviving items, not "
                    f"its contract, so '{n['haltWhen']}' would never fire. Halt on a "
                    f"separate unverified node, or drop the tier"
                )
            elif HALT.match(str(n["haltWhen"])) and c in contracts:
                field = HALT.match(str(n["haltWhen"])).group(1)
                if field not in contracts[c].get("properties", {}):
                    f.append(
                        f"{where}: haltWhen tests '{field}', which is not a field of "
                        f"{c} — the condition could never fire"
                    )

        # An agentType that RESOLVES to an agent whose declared contract is not
        # this node's is the expensive shape: the name is real, the agent runs,
        # and it answers in a schema the node cannot accept -- so the node dies
        # on a mismatch or, worse, answers about the wrong subject while
        # holding the campaign's only haltWhen. Rejected here because it is
        # provable. An UNRESOLVABLE name is only warned about (see warnings()),
        # since agents also come from installed plugins and built-in types that
        # this compiler cannot enumerate, and rejecting on a partial view would
        # refuse valid IR.
        want_agent = n.get("agentType") or (
            (ir.get("roles") or {}).get(n.get("role"), {}) or {}).get("agentType")
        if want_agent and (agents or {}).get(want_agent):
            declared = agents[want_agent].get("contract")
            if declared == "prose" and n.get("contract"):
                f.append(
                    f"{where}: agentType '{want_agent}' answers in a report, not "
                    f"in a contract, but this node is contracted to "
                    f"'{n['contract']}' — it would die on the schema or answer "
                    f"about the wrong subject. Bind an agent that produces "
                    f"{n['contract']}, or park this check outside the graph."
                )
            elif declared and declared != "prose" and n.get("contract") \
                    and declared != n["contract"]:
                f.append(
                    f"{where}: agentType '{want_agent}' declares contract "
                    f"'{declared}', but this node is contracted to "
                    f"'{n['contract']}' — that agent cannot produce this node's "
                    f"schema. Bind an agent that can, or change the node's "
                    f"contract to match."
                )

        hon = n.get("honors") or []

        # {{prev}} is only meaningful with a declared predecessor; otherwise
        # the node is reading state no edge delivers to it.
        if _uses_prev(n.get("prompt", "")) and not n.get("after"):
            f.append(f"{where}: prompt uses {{{{prev}}}} but declares no 'after' — hidden coupling")

        # Substitution tokens must resolve to something actually in scope for
        # this node, or the graph compiles clean and dies at launch on a
        # ReferenceError.
        bound = {"A", "campaign"}
        if n.get("after"):
            bound.add("prev")
        if n.get("foreach"):
            bound.update({"item", "i"})
        if n.get("repeat"):
            bound.add("seen")
        # Both fields the emitter interpolates must be checked, or the
        # validator blesses a token the emitter renders into executable
        # position. release.instructions became a `${...}` site when it
        # moved to js_template; unchecked, {{process.env.X}} in operator
        # prose compiled clean and read a secret into the advisor prompt,
        # the provenance note and the run log. Same rule, same message,
        # named by whichever field carried the token.
        subst_sites = [("prompt", str(n.get("prompt", "")))]
        rel_block = n.get("release") or {}
        if rel_block.get("instructions") is not None:
            subst_sites.append(("release.instructions",
                                str(rel_block["instructions"])))
        for site, site_text in subst_sites:
            for tok in SUBST.findall(site_text):
                root = tok.split(".")[0].split("[")[0]
                if root == "decisions":
                    # Scoped per id on purpose: honoring one decision must not
                    # hand the node every decision the campaign ever made.
                    want = tok.split(".", 1)[1] if "." in tok else ""
                    if want not in (hon or []):
                        f.append(
                            f"{where}: {site} uses {{{{{tok}}}}} but '{want}' is not "
                            f"in this node's honors — name it there, so what binds "
                            f"this node is declared rather than implied"
                        )
                    continue
                # {{prev.<field>}} projects one field of the predecessor's
                # contract instead of pasting the whole object — the cheapest
                # form of compress-before-reason. It used to pass this check on
                # its root and emit `${prev.<field>}` with no JS binding: a
                # graph that compiled clean and died at launch, which is the
                # precise failure this scope check exists to prevent. So the
                # token is validated all the way down, and emission binds it.
                if root == "prev" and "prev" in bound and tok != "prev":
                    if "[" in tok or tok.count(".") != 1:
                        f.append(
                            f"{where}: {site} uses {{{{{tok}}}}} — only "
                            f"{{{{prev}}}} or a single-hop {{{{prev.<field>}}}} "
                            f"is supported")
                    else:
                        pred = seen.get(after) or {}
                        field = tok.split(".", 1)[1]
                        shape = result_shape(pred)
                        if shape != "object":
                            yields = ("a flat array of judged items"
                                      if shape == "items"
                                      else "an array of contract objects")
                            f.append(
                                f"{where}: {site} uses {{{{{tok}}}}} but "
                                f"'{after}' yields {yields}, not its contract "
                                f"object — consume {{{{prev}}}} whole, or put a "
                                f"reduce node between them")
                        else:
                            pc = pred.get("contract")
                            if pc in contracts and field not in contracts[pc].get(
                                    "properties", {}):
                                f.append(
                                    f"{where}: {site} uses {{{{{tok}}}}} but "
                                    f"'{field}' is not a field of {pc} — it "
                                    f"would interpolate null at launch")
                    continue
                if root not in bound:
                    f.append(
                        f"{where}: {site} uses {{{{{tok}}}}} but '{root}' is not in "
                        f"scope for this node (available: {', '.join(sorted(bound))}) — "
                        f"it would compile clean and throw at launch"
                    )

        # Discovery loops: unknown-size work needs a dry rule, a hard round
        # ceiling, and a dedup key, or it either never converges or never ends.
        rep = n.get("repeat")
        if "{{seen}}" in str(n.get("prompt", "")) and not rep:
            f.append(f"{where}: prompt uses {{{{seen}}}} but declares no 'repeat' block")
        if rep is not None:
            if not isinstance(rep, dict):
                f.append(f"{where}: repeat must be an object")
            else:
                dry = rep.get("untilDryRounds")
                mx = rep.get("maxRounds")
                if not isinstance(dry, int) or dry < 1:
                    f.append(f"{where}: repeat.untilDryRounds must be an integer >= 1")
                if not isinstance(mx, int) or mx < 1:
                    f.append(f"{where}: repeat.maxRounds must be an integer >= 1 — an unbounded discovery loop has no halt condition")
                if isinstance(dry, int) and isinstance(mx, int) and dry > mx:
                    f.append(f"{where}: repeat.untilDryRounds ({dry}) exceeds maxRounds ({mx}); the dry rule can never fire")
                keys = rep.get("dedupeBy")
                if not isinstance(keys, list) or not keys or not all(isinstance(k, str) and k for k in keys):
                    f.append(f"{where}: repeat.dedupeBy must be a non-empty list of field names — without a key the loop re-finds the same items forever")
                elif not n.get("verifyOver"):
                    f.append(f"{where}: repeat needs verifyOver naming the array field being discovered")
                elif c in contracts:
                    item_props = (
                        contracts[c].get("properties", {})
                        .get(n["verifyOver"], {})
                        .get("items", {})
                        .get("properties", {})
                    )
                    for k in keys:
                        if item_props and k not in item_props:
                            f.append(f"{where}: repeat.dedupeBy '{k}' is not a field of {c}.{n['verifyOver']} items")

        # Lessons: what this node contributes to, and reads from, across
        # runs. Both directions are declared, because a sweep that silently
        # inherited state would be unreadable from the IR alone.
        mem = n.get("memory")
        if mem is not None:
            f += _unknown(f"{where} memory", mem, MEMORY_FIELDS)
            if not isinstance(mem, dict):
                f.append(f"{where}: memory must be an object")
            elif not mem.get("seed") and not mem.get("emit"):
                f.append(
                    f"{where}: memory declares neither seed nor emit — an empty "
                    f"block reads as configured and does nothing")
            else:
                for side in ("seed", "emit"):
                    tag = mem.get(side)
                    if tag is not None and not (isinstance(tag, str) and IDENT.match(tag)):
                        f.append(f"{where}: memory.{side} must be a lowercase "
                                 f"kebab-case tag")
                if mem.get("seed") and not n.get("repeat"):
                    f.append(
                        f"{where}: memory.seed without a repeat block — the seed "
                        f"feeds a discovery sweep's seen-list, and a node that "
                        f"runs once has nowhere to put it")
                if mem.get("emit"):
                    if not panel_size(n):
                        f.append(
                            f"{where}: memory.emit needs a verification tier "
                            f"(skeptic:N or panel:N) — a lesson's worth is its "
                            f"verdict and the objection behind it, and filing "
                            f"unjudged output would promote a well-formed guess "
                            f"to institutional knowledge")
                    if not n.get("verifyOver"):
                        f.append(
                            f"{where}: memory.emit needs verifyOver naming the "
                            f"array of items to file as lessons")
                    elif c in contracts:
                        item_props = (
                            contracts[c].get("properties", {})
                            .get(n["verifyOver"], {})
                            .get("items", {})
                            .get("properties", {})
                        )
                        if item_props and "claim" not in item_props:
                            f.append(
                                f"{where}: memory.emit needs {c}.{n['verifyOver']} "
                                f"items to carry a 'claim' — a lesson without one "
                                f"is a row no later sweep can act on")
                key = mem.get("key")
                if key is not None and n.get("repeat"):
                    f.append(
                        f"{where}: memory.key with a repeat block — the dedupe "
                        f"identity is already declared as repeat.dedupeBy, and two "
                        f"spellings of one key is how they drift apart")
                elif mem.get("emit") and not n.get("repeat"):
                    if not isinstance(key, list) or not key or not all(
                            isinstance(k, str) and k for k in key):
                        f.append(
                            f"{where}: memory.emit on a node with no repeat block "
                            f"needs memory.key — the cross-run identity of a lesson "
                            f"cannot be implicit")
                    elif c in contracts and n.get("verifyOver"):
                        item_props = (
                            contracts[c].get("properties", {})
                            .get(n["verifyOver"], {})
                            .get("items", {})
                            .get("properties", {})
                        )
                        for k in key:
                            if item_props and k not in item_props:
                                f.append(
                                    f"{where}: memory.key '{k}' is not a field of "
                                    f"{c}.{n['verifyOver']} items")
                # The class: a second, coarser identity carried beside the
                # instance key. The instance key answers "is this the same
                # finding"; every field it can be built from names a defect's
                # LOCATION or its WORDING, so one defect shape recurring in
                # three places produces three keys and reads as three lessons.
                # The class answers "is this the same SHAPE", and it is
                # declared rather than inferred: a class guessed from claim
                # similarity would collapse unrelated lessons, and a gate that
                # fires on everything is one people learn to skim.
                # The absence of a class is a DECISION, not a default. Every
                # field the instance key can be built from names a defect's
                # location or its wording, so one shape recurring in three
                # places files as three unrelated lessons — the measured
                # history did exactly that. A node that files lessons either
                # declares its class or declares, with `null`, that its
                # lessons have none; silence is the forgot-to-declare hole
                # this same gate exists to kill.
                cls = mem.get("classBy")
                if "classBy" not in mem:
                    if mem.get("emit"):
                        f.append(
                            f"{where}: memory.emit without a classBy decision "
                            f"— an instance key names a defect's location, so "
                            f"a shape recurring in three places files as three "
                            f"unrelated lessons. Declare classBy (the item "
                            f"field naming the SHAPE) or classBy: null to "
                            f"state these lessons have no class")
                elif cls is None:
                    if not mem.get("emit"):
                        f.append(
                            f"{where}: memory.classBy without memory.emit — a "
                            f"class on a node that files nothing groups nothing")
                else:
                    if not isinstance(cls, list) or not cls or not all(
                            isinstance(k, str) and k for k in cls):
                        f.append(
                            f"{where}: memory.classBy must be a non-empty list "
                            f"of field names — the class is declared by the "
                            f"finder, never inferred from the claim")
                    elif not mem.get("emit"):
                        f.append(
                            f"{where}: memory.classBy without memory.emit — a "
                            f"class on a node that files nothing groups nothing")
                    elif c in contracts and n.get("verifyOver"):
                        item_props = (
                            contracts[c].get("properties", {})
                            .get(n["verifyOver"], {})
                            .get("items", {})
                            .get("properties", {})
                        )
                        for k in cls:
                            if item_props and k not in item_props:
                                f.append(
                                    f"{where}: memory.classBy '{k}' is not a "
                                    f"field of {c}.{n['verifyOver']} items")
                pri = mem.get("priors")
                if pri is not None:
                    if pri is not True:
                        f.append(
                            f"{where}: memory.priors must be literally true — "
                            f"its budget (5 killed claims, 400 chars each) is "
                            f"fixed, so declare it or leave it out")
                    else:
                        if not mem.get("seed"):
                            f.append(
                                f"{where}: memory.priors without memory.seed — "
                                f"the priors are the seed tag's killed lessons, "
                                f"and a node that seeds nothing loads none")
                        if not panel_size(n):
                            f.append(
                                f"{where}: memory.priors needs a verification "
                                f"tier (skeptic:N or panel:N) — priors are "
                                f"addressed to refuters, and a node with no "
                                f"panel has nobody to tell")

        # Self-report invariant. A node that changes the tree cannot be the
        # node that certifies the change; some later independent node must
        # verify it. This is the feature.graph.js bug, promoted to a rule.
        if n.get("mutates"):
            verifiers = [
                o for o in nodes
                if o.get("independent") and o.get("verifies") == nid
            ]
            if not verifiers:
                f.append(
                    f"{where}: mutates the tree but no node with independent:true "
                    f"verifies:'{nid}' — a mutator may not certify its own work"
                )
        # Decisions. DesignV1 has summary/plan/files/risks and none of them
        # is *the choice*, so a frozen architectural decision had to be
        # smuggled into free text and the rejected alternatives had nowhere
        # to go at all. A decision that overturns the prior is precisely the
        # one a fresh context re-decides the other way, and when the
        # parameter freezes at genesis that re-decision is unrecoverable.
        if n.get("decides"):
            if c != "DecisionV1":
                f.append(
                    f"{where}: decides requires contract DecisionV1 (has '{c}') "
                    f"— the record is the point, not the prose around it"
                )
            if not IDENT.match(str(n["decides"])):
                f.append(f"{where}: decides id must be lowercase kebab-case")
            if n["decides"] in imports:
                f.append(
                    f"{where}: decides '{n['decides']}', which this campaign "
                    f"imports — an imported decision is frozen, and re-deciding "
                    f"it here is exactly the silent overturn imports exist to "
                    f"prevent; drop the import or rename the decision")
            if n["decides"] in decided:
                f.append(f"{where}: decides '{n['decides']}' is already decided by "
                         f"node '{decided[n['decides']]}'")
            else:
                decided[n["decides"]] = nid
        if n.get("honors") is not None:
            if not isinstance(n["honors"], list) or not all(
                    isinstance(h, str) for h in n["honors"]):
                f.append(f"{where}: honors must be a list of decision ids")
            else:
                for h in hon:
                    if h not in decided and h not in imports:
                        f.append(
                            f"{where}: honors '{h}' is neither decided by an "
                            f"earlier node nor listed under imports — a decision "
                            f"this node cannot see cannot bind it"
                        )
                    # Same rule as `after`: a declared dependency the prompt
                    # never reads is a phantom. It looks binding in the spec
                    # and constrains nothing in the run, which is worse than
                    # not declaring it — a reader would believe it held.
                    if ("{{decisions." + h + "}}") not in str(n.get("prompt", "")):
                        f.append(
                            f"{where}: honors '{h}' but the prompt never uses "
                            f"{{{{decisions.{h}}}}} — the decision would not reach "
                            f"the agent, so nothing would be bound by it"
                        )

        # Actors. The engine had two responses to a node it could not
        # complete — halt the campaign, or drop the item and march on with a
        # null — and no third state for "this one is blocked, work the other
        # branches". Every real delivery has nodes only a human or a third
        # party can execute.
        actor = n.get("actor", "agent")
        if actor not in ACTORS:
            f.append(f"{where}: actor must be one of {', '.join(sorted(ACTORS))}")
        if actor != "agent":
            rel = n.get("release")
            if not isinstance(rel, dict):
                f.append(
                    f"{where}: actor '{actor}' needs a release block — a node no "
                    f"agent can run is a dead stop unless it says what unblocks it"
                )
            else:
                if not str(rel.get("instructions", "")).strip():
                    f.append(
                        f"{where}: release.instructions required — this text is "
                        f"the entire message the blocked human gets"
                    )
                # Parking is a last resort, not a first response. Most things
                # that feel human-only are not: a CLI, an API, or a headless
                # browser does them. Naming what was ruled out is the cheapest
                # way to stop a node being parked out of habit.
                if not str(rel.get("whyNotAgent", "")).strip():
                    f.append(
                        f"{where}: release.whyNotAgent required — say what makes "
                        f"this impossible for an agent (key material it must not "
                        f"hold, legal authority, physical possession, another "
                        f"party's action), because a step a CLI or a headless "
                        f"browser could do should not be parked on a person"
                    )
                pc = rel.get("proofContract")
                if not pc:
                    f.append(f"{where}: release.proofContract required")
                elif pc not in contracts:
                    f.append(f"{where}: release.proofContract '{pc}' is not a known contract")
                elif c and pc != c:
                    f.append(
                        f"{where}: release.proofContract '{pc}' differs from the "
                        f"node's contract '{c}' — the node yields exactly what the "
                        f"operator pastes, so downstream would be promised a shape "
                        f"the release can never produce"
                    )
            for bad in ("mutates", "irreversible", "foreach", "repeat", "verify",
                        "onRed"):
                if n.get(bad) and not (bad == "verify" and n.get(bad) == "schema-only"):
                    f.append(
                        f"{where}: actor '{actor}' cannot be combined with "
                        f"'{bad}' — no agent runs this node, so there is nothing "
                        f"for it to isolate, fan out, verify, or fail; a parked "
                        f"node blocks, and blocking already has its own policy"
                    )
        wake = n.get("wake")
        if wake is not None:
            if actor == "agent":
                f.append(
                    f"{where}: wake is only meaningful with actor human or "
                    f"third-party — an agent node is not waiting on anyone"
                )
            if not isinstance(wake, dict):
                f.append(f"{where}: wake must be an object")
            else:
                if not str(wake.get("check", "")).strip():
                    f.append(
                        f"{where}: wake.check required — a poll with no predicate "
                        f"never fires, and the node waits forever"
                    )
                ev = wake.get("everyMinutes")
                if not isinstance(ev, int) or ev < 1:
                    f.append(f"{where}: wake.everyMinutes must be an integer >= 1")

        # Irreversible effects. `mutates` buys worktree isolation, which is
        # real containment for a filesystem write and none at all for a chain
        # write — the same marker covering "edit a test file" and "mint a
        # one-shot NFT" reads as protection it does not provide.
        if n.get("irreversible"):
            if n.get("foreach"):
                f.append(
                    f"{where}: irreversible cannot be combined with foreach — "
                    f"a fan-out of unrepeatable effects under one confirmation "
                    f"authorizes N ceremonies by naming one. Declare each"
                )
            if n.get("repeat"):
                f.append(
                    f"{where}: irreversible cannot be combined with repeat — "
                    f"a discovery loop re-fires its node by design"
                )
            # The adversarial gate has to be ordered BEFORE the effect. A
            # verifier that runs after cannot un-mint an NFT.
            guards = [
                o for o in seen.values()
                if o.get("independent") and o.get("haltWhen") and o.get("verifies")
            ]
            if not guards:
                f.append(
                    f"{where}: irreversible but no earlier node with "
                    f"independent:true, verifies:'<node>' and a haltWhen — the "
                    f"gate must be ordered before the effect, because a verifier "
                    f"that runs afterwards cannot undo it"
                )
            elif gates and not any(prove_gate(o) for o in guards):
                # The ordering invariant was sound in structure and hollow in
                # fidelity: the guard runs the harness and then types its own
                # exit code into a contract, so the integer standing between a
                # campaign and an unrepeatable chain write was a transcription.
                # Where the repo has declared its gates, that is no longer the
                # cheapest available shape, so it is no longer an allowed one.
                f.append(
                    f"{where}: irreversible, and the gate ordered before it "
                    f"({', '.join(sorted(o['id'] for o in guards))}) reports its "
                    f"own exit code. This repo declares gates in {GATES}, so the "
                    f"guard must use verify prove:<gate> and contract "
                    f"ExecutionV1 — an effect nobody can undo may not rest on a "
                    f"number the node that ran it typed by hand. A gate longer "
                    f"than one tool call runs through attest.py --run and "
                    f"--await; CI's own statuses are prove:ci"
                )
            if "confirm" not in (ir.get("requiredArgs") or []):
                f.append(
                    f"{where}: irreversible requires 'confirm' in requiredArgs — "
                    f"the run must refuse to start unless a human named this node "
                    f"in --confirm, not merely launched the campaign"
                )
        if n.get("verifies"):
            if n["verifies"] not in seen and n["verifies"] != nid:
                f.append(f"{where}: verifies '{n['verifies']}' is not a node defined earlier")
            if n["verifies"] == nid:
                f.append(f"{where}: cannot verify itself")
            if not n.get("independent"):
                f.append(f"{where}: verifies '{n['verifies']}' but is not marked independent:true")
        seen[nid] = n

    # Budget layer: fan-out must fit the declared ceiling.
    budget = ir.get("budget") or {}
    max_nodes = budget.get("maxNodes")
    planned = plan_node_count(ir)
    if max_nodes is not None and planned > max_nodes:
        f.append(
            f"budget: graph plans {planned} agent calls but budget.maxNodes is "
            f"{max_nodes} — raise the ceiling or shrink the fan-out"
        )
    if max_nodes is None:
        f.append("budget.maxNodes: required — an unbounded graph has no halt condition")
    # The cost ceiling. maxNodes stays as the fan-out guardrail; this is the
    # number anyone actually cares about, denominated in what the bill is.
    ttl = budget.get("cacheTtl")
    if ttl is not None and ttl not in CACHE_TTLS:
        f.append(
            f"budget.cacheTtl must be one of {', '.join(sorted(CACHE_TTLS))} — "
            f"'{ttl}' names no prompt-cache lifetime the runtime offers, and an "
            f"unknown value would price every hop as warm while nothing is")
    cap = budget.get("maxEstimatedTokens")
    if cap is not None:
        if isinstance(cap, bool) or not isinstance(cap, int) or cap < 1:
            f.append("budget.maxEstimatedTokens must be a positive integer")
        elif ttl is None or ttl in CACHE_TTLS:
            est = estimate_tokens(ir, contracts, specification)
            if est["total"] > cap:
                f.append(
                    f"budget: the graph is estimated at ~{est['total']:,} tokens "
                    f"({est['calls']} agent call(s), {est['cold']} cold prefill(s), "
                    f"prompt-cache TTL {est['ttl']}) but budget.maxEstimatedTokens "
                    f"is {cap:,} — lower effort where it buys nothing, put "
                    f"same-effort work together so the prefix stays warm, declare "
                    f"cacheTtl '1h' if the session provides it, or raise the "
                    f"ceiling on purpose")
    return f


def warnings(ir, agents=None):
    """Non-blocking findings: shapes that compile but will predictably
    disappoint. Returned separately from validate() so they inform without
    refusing to build."""
    w = []
    for n in ir.get("nodes") or []:
        nid = n.get("id", "?")
        # A bad agentType fails at the worst possible node: `agent type not
        # found` is raised at the spawn, so a gate late in the graph pays for
        # it only after everything upstream has run. The compiler cannot close
        # the agent namespace -- plugins and built-in types are outside its
        # view -- so this names the risk before the first spawn instead of
        # refusing IR that may be perfectly valid.
        want = n.get("agentType") or (
            (ir.get("roles") or {}).get(n.get("role"), {}) or {}).get("agentType")
        if want and agents is not None and want not in agents:
            w.append(
                f"node '{nid}': agentType '{want}' does not resolve to any agent "
                f"this compiler can see. That is not proof it is missing — "
                f"plugins and built-in types are outside its view — but if it "
                f"is, the graph dies at this node's spawn after everything "
                f"upstream has run. Confirm it is registered in this session."
            )
        # A fan-out that builds or measures against a SHARED working tree makes
        # every number a function of what the siblings are doing, and the
        # failure is silent: each node exits 0 with a confident figure. The
        # compiler cannot know what a prompt really does, so this names the
        # risk on the words that usually mean it, without refusing the IR.
        if n.get("foreach") and not n.get("isolation") and not n.get("mutates"):
            if re.search(r"\b(build|compile|measure|benchmark|bytes?|size|price)\b",
                         str(n.get("prompt", "")), re.I):
                w.append(
                    f"node '{nid}': this fan-out looks like it builds or measures, "
                    f"and its workers share ONE working tree — each measurement "
                    f"becomes a function of what every sibling is doing, silently. "
                    f"Declare isolation: 'measure' (a frozen `git archive HEAD` "
                    f"snapshot, base guaranteed) or isolation: 'worktree'."
                )
        rep = n.get("repeat") or {}
        dry, mx = rep.get("untilDryRounds"), rep.get("maxRounds")
        if isinstance(dry, int) and isinstance(mx, int):
            # A sweep needs room for productive rounds AND the dry streak that
            # proves it is finished. Without that headroom the ceiling, not the
            # dry rule, ends every run — and every run reports INCOMPLETE.
            headroom = mx - dry
            if headroom < 2:
                w.append(
                    f"node '{nid}': maxRounds {mx} leaves only {headroom} round(s) "
                    f"above untilDryRounds {dry}, so the ceiling will end the sweep "
                    f"before the dry rule can — expect INCOMPLETE. Prefer "
                    f"maxRounds >= {dry + 3} unless a short sweep is the point."
                )
        if n.get("repeat") and not panel_size(n) and n.get("verify", "schema-only") == "schema-only":
            w.append(
                f"node '{nid}': discovery with no verification tier — a sweep's "
                f"output is usually consumed as fact; consider skeptic:1 or panel:3"
            )
    nodes = ir.get("nodes") or []
    # --- the prompt cache: effort transitions and the TTL the graph needs ---
    # A forked call shares the parent's prompt cache only on a byte-identical
    # prefix at the same model and effort. An inline effort equal to what the
    # role or default already gives changes nothing and reads as a decision;
    # a real change between consecutive nodes is a cold prefill each time.
    roles = ir.get("roles") or {}
    dflt = (ir.get("defaults") or {}).get("effort")
    for n in nodes:
        if is_reduce(n) or n.get("actor", "agent") != "agent" or not n.get("effort"):
            continue
        inherited = (roles.get(n.get("role"), {}) or {}).get("effort") or dflt
        if inherited and n["effort"] == inherited:
            w.append(
                f"node '{n.get('id', '?')}': effort '{n['effort']}' inline equals "
                f"what its {'role' if n.get('role') else 'defaults'} already "
                f"gives — it changes nothing and reads as a deliberate bump; "
                f"drop it, or bump it on purpose")
    budget = ir.get("budget") or {}
    ttl = budget.get("cacheTtl")
    trans = effort_transitions(ir)
    if trans:
        hops = ", ".join(f"'{a}'({ka[1]}{'@' + ka[0] if ka[0] else ''}) -> "
                         f"'{b}'({kb[1]}{'@' + kb[0] if kb[0] else ''})"
                         for a, ka, b, kb in trans[:4])
        more = f" (+{len(trans) - 4} more)" if len(trans) > 4 else ""
        if ttl in CACHE_TTLS and CACHE_TTLS[ttl] >= 3600:
            w.append(
                f"{len(trans)} effort/model transition(s) between consecutive "
                f"nodes into a key the run has not warmed yet ({hops}{more}) — "
                f"each is a cold prefill the estimate charges even under the "
                f"1-hour TTL, so a bump that the node's task shape does not need "
                f"is a cost error; put same-effort work together, or move the "
                f"change to a point that is cold anyway (a node that waits on a "
                f"park, which runs after the release and is priced cold)")
        else:
            w.append(
                f"{len(trans)} effort/model transition(s) between consecutive "
                f"nodes into a key the run has not warmed yet ({hops}{more}) — "
                f"under the default 5-minute prompt-cache TTL every sequential "
                f"hop is priced cold, so these cost nothing extra yet; declare "
                f"budget.cacheTtl '1h' and the same-effort hops become warm, at "
                f"which point each of these is a cold prefill the estimate "
                f"charges for")
    fans_or_parks = [n.get("id", "?") for n in nodes
                     if n.get("foreach") or n.get("repeat") or panel_size(n)
                     or n.get("actor", "agent") != "agent"]
    if fans_or_parks and ttl is None:
        w.append(
            f"{len(fans_or_parks)} node(s) fan out or park ({', '.join(fans_or_parks[:3])}"
            f"{' …' if len(fans_or_parks) > 3 else ''}) and the graph declares no "
            f"budget.cacheTtl — the default 5-minute prompt-cache TTL is counted "
            f"from the request start, so the parent's prefix expires while it "
            f"blocks; declare cacheTtl '1h' and run under a session configured "
            f"for one, or accept that every hop is priced cold (as the estimate "
            f"does now)")
    # The skill's ten-node rule, said where the author is looking. A warning
    # rather than a rejection: a big campaign is legal, but it is usually a
    # campaign that should have been split, with frozen decisions crossing
    # the boundary via imports.
    if len(nodes) > 10:
        w.append(
            f"{len(nodes)} nodes — a work graph beyond ten nodes is scope "
            f"creep per the graph-engineering skill; split the campaign and "
            f"carry frozen decisions across with imports")
    # Top-level nodes execute serially in declaration order. Two adjacent
    # nodes with no declared dependency therefore serialize work the
    # executor could overlap — either the order matters and the edge is
    # undeclared, or it does not and the shape is quietly slower than it
    # reads. Both deserve a sentence at compile time, because nothing at
    # run time will ever say it: latency has no witness in the artifact.
    serial = []
    for i in range(1, len(nodes)):
        cur = nodes[i]
        if is_reduce(cur) or cur.get("actor", "agent") != "agent":
            continue
        if not (cur.get("after") or cur.get("honors") or cur.get("verifies")):
            serial.append((nodes[i - 1].get("id", "?"), cur.get("id", "?")))
    if serial:
        pairs = ", ".join(f"'{a}' -> '{b}'" for a, b in serial[:3])
        more = f" (+{len(serial) - 3} more)" if len(serial) > 3 else ""
        w.append(
            f"{len(serial)} adjacent top-level pair(s) declare no dependency "
            f"({pairs}{more}) yet run serially in declaration order — if they "
            f"are truly independent, fold them into one foreach fan-out so "
            f"they overlap; if the order matters, it is an undeclared edge")
    return w


def node_key(n, ir):
    """(model, effort) a node's spawn runs at — the prompt-cache identity."""
    roles = ir.get("roles") or {}
    role = roles.get(n.get("role"), {}) or {}
    effort = n.get("effort") or role.get("effort") or (ir.get("defaults") or {}).get("effort") or "medium"
    model = n.get("model") or role.get("model") or (ir.get("defaults") or {}).get("model") or ""
    return (str(model), str(effort))


def _model_mult(model):
    m = (model or "").lower()
    for needle, mult in MODEL_MULT:
        if needle in m:
            return mult
    return 1.0


def plan_groups(ir):
    """The campaign's spawns as groups that run concurrently, in order.

    Each group is a list of (nodeId, (model, effort)) — a fan-out's workers,
    a panel's refuters, a tree sentinel, an advisor. `park` marks an
    advisor's group. `track` is the set of parked nodes the group waits on
    (held_by): a node downstream of a park does not spawn until a person
    releases it, hours later, so it runs in a later run whose cache starts
    cold; a node that does not wait on the park runs on in the same run,
    right after the advisor, and keeps whatever that run has warmed. This is
    the same arithmetic as plan_node_count(), laid out so each call carries
    the identity the prompt cache keys on.
    """
    lists = ir.get("lists") or {}
    tree_guard = ir.get("treeGuard", True)
    held = held_by(ir)
    groups = []
    sentinel = ("", "low")
    if tree_guard:
        groups.append({"calls": [("tree-check", sentinel)], "park": False, "sentinel": True,
                       "kind": "sentinel", "track": frozenset()})
    for n in ir.get("nodes") or []:
        if is_reduce(n):
            continue
        nid = n.get("id", "?")
        track = held.get(nid, frozenset())
        if tree_guard and (prove_gate(n) or n.get("independent")):
            groups.append({"calls": [("tree-check", sentinel)], "park": False, "sentinel": True,
                           "kind": "sentinel", "track": track})
        if n.get("actor", "agent") != "agent":
            groups.append({"calls": [(nid, ("", "medium"))], "park": True,
                           "kind": "advise", "node": n, "track": track})
            continue
        key = node_key(n, ir)
        items = lists.get(n.get("foreach"), [None]) if n.get("foreach") else [None]
        fan = len(items)
        rounds = max(1, int((n.get("repeat") or {}).get("maxRounds", 1)))
        cnt = panel_size(n)
        per = int(n.get("expectItems", 3))
        for _ in range(rounds):
            groups.append({"calls": [(nid, key)] * fan, "park": False,
                           "kind": "work", "node": n, "items": items, "track": track})
            if cnt:
                groups.append({"calls": [(nid, ("", "low"))] * (fan * per * cnt),
                               "park": False, "kind": "refute", "track": track})
    if tree_guard:
        groups.append({"calls": [("tree-check", sentinel)], "park": False, "sentinel": True,
                       "track": frozenset()})
    return groups


def held_by(ir):
    """nodeId -> the parked nodes it waits on, through `after` and a
    reduce's `from` — the edges the emitted BLOCKED guard follows.

    A node with an empty set spawns in the run that reaches it, parks or no
    parks: a parked node only marks itself BLOCKED and the campaign carries
    on. Only what hangs below a park waits for the release.
    """
    nodes = ir.get("nodes") or []
    by_id = {n.get("id"): n for n in nodes}
    memo = {}

    def walk(nid, stack):
        if nid in memo:
            return memo[nid]
        n = by_id.get(nid) or {}
        dep = n.get("after") or (n.get("reduce") or {}).get("from")
        out = frozenset()
        if isinstance(dep, str) and dep in by_id and dep not in stack:
            up = walk(dep, stack | {nid})
            out = up | ({dep} if by_id[dep].get("actor", "agent") != "agent" else frozenset())
        memo[nid] = out
        return out

    return {n.get("id"): walk(n.get("id"), frozenset()) for n in nodes}


def _utf8(s):
    return len(str(s).encode("utf-8"))


def _js_text(v):
    """What the emitted tokenText() renders a value to: text as itself, a
    list or object as compact JSON, anything else as JS's String() would."""
    if isinstance(v, str):
        return v
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False, separators=(",", ":"))
    if isinstance(v, bool):
        return "true" if v else "false"
    if v is None:
        return "null"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _descend(value, path):
    """Follow a token's dotted path into a value; MISSING when it ends."""
    cur = value
    for part in path:
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return _MISSING
    return cur


_MISSING = object()


def _item_value(item, tok):
    """What {{item}} / {{item.<field>}} renders to for one list entry."""
    v = _descend(item, tok.split(".")[1:])
    return "undefined" if v is _MISSING else _js_text(v)


def _expansion_bytes(tok, ir, item=None, index=0):
    """Bytes one {{token}} is estimated to render to. 0 when unknowable —
    unpriced_expansions() names those."""
    root = tok.split(".")[0].split("[")[0]
    if root == "A":
        defaults = ir.get("argDefaults") or {}
        if "." not in tok:
            return _utf8(_js_text(defaults))
        v = _descend(defaults, tok.split(".")[1:])
        return 0 if v is _MISSING else _utf8(_js_text(v))
    if root == "campaign":
        return _utf8(ir.get("campaign", ""))
    if root == "item" and item is not None:
        return _utf8(_item_value(item, tok))
    if root == "i":
        return len(str(index))
    return 0


def prompt_bytes(texts, ir, item=None, index=0):
    """(shared, varying) UTF-8 bytes of the author text one call carries.

    `shared` is everything before the first token whose value differs
    between calls of the same node — the part a sibling's prefix cache can
    hold. `varying` is the rest, paid in full by every call.
    """
    shared = varying = 0
    split = False
    for text in texts:
        pos = 0
        for m in SUBST.finditer(text):
            lit = _utf8(text[pos:m.start()])
            tok = m.group(1)
            if not split and tok.split(".")[0].split("[")[0] in VARYING_ROOTS:
                shared += lit
                split = True
                varying += _expansion_bytes(tok, ir, item, index)
            elif split:
                varying += lit + _expansion_bytes(tok, ir, item, index)
            else:
                shared += lit + _expansion_bytes(tok, ir, item, index)
            pos = m.end()
        tail = _utf8(text[pos:])
        if split:
            varying += tail
        else:
            shared += tail
    return shared, varying


def estimate_tokens(ir, contracts=None, specification=None):
    """Cold-input-token equivalents for the worst-case plan.

    A call's prefix is warm when an earlier call at the same (model, effort)
    key is still in the cache: within one concurrent group always (siblings
    dispatch together), across groups only under a one-hour TTL, and never
    across a park for a node that waits on it — that node runs in a later
    run, after the hours a person takes (plan_groups' `track`). Everything
    else is a cold prefill. Work tokens scale with effort and the whole call
    with the model's price weight. The node's own prompt is priced by its
    bytes; its shared part is a cache read only when the same node already
    sent it under a warm key (a fan-out's siblings, a sweep's later rounds)
    — a warm key says nothing about a DIFFERENT node's text, which the cache
    has never seen. Returns the total, the call and cold-prefill counts, a
    per-node breakdown (the total is its sum), the expansions no number
    could price, and the assumptions it rested on, so a reader can disagree
    with a number rather than a feeling.
    """
    budget = ir.get("budget") or {}
    ttl = budget.get("cacheTtl") or DEFAULT_CACHE_TTL
    persist = CACHE_TTLS.get(ttl, 300) >= 3600
    # Per track: the keys this run has warmed, and (key, nodeId) pairs whose
    # shared text is cached.
    tracks = {}
    calls, cold = 0, 0
    per_node, raw = {}, {}
    # One token per ASCII serialization byte is deliberately conservative.
    # Do not assume the packet, after a varying preamble, shares the cache.
    spec_input = (len(SPEC_PREAMBLE) + len(json.dumps(specification, ensure_ascii=True,
                                                    separators=(',', ':'))) + 1) if specification else 0
    for g in plan_groups(ir):
        if not persist:
            tracks.pop(g["track"], None)
        warm, warm_prompts = tracks.setdefault(g["track"], (set(), set()))
        n = g.get("node") or {}
        for ci, (nid, (model, effort)) in enumerate(g["calls"]):
            calls += 1
            key = (model, effort)
            is_warm = key in warm
            prefix = PREFIX_TOKENS * (CACHED_PREFIX_FRACTION if is_warm else 1.0)
            work = (SENTINEL_WORK_TOKENS if g.get("sentinel")
                    else WORK_TOKENS * EFFORT_MULT.get(effort, 1.0))
            text = 0.0
            if g.get("kind") in ("work", "advise"):
                literal = 0
                if g["kind"] == "advise":
                    rel = n.get("release") or {}
                    texts = [str(n.get("prompt", "")), str(rel.get("instructions", ""))]
                    # Sent through js_str, not js_template: its braces reach
                    # the advisor as written, so they cost what they spell.
                    literal = _utf8(rel.get("whyNotAgent", "unstated"))
                    item = None
                else:
                    texts = [str(n.get("prompt", ""))]
                    items = g.get("items") or [None]
                    item = items[ci % len(items)]
                shared, varying = prompt_bytes(texts, ir, item, ci)
                shared += literal
                seen_text = is_warm and (key, nid) in warm_prompts
                text = (shared * (CACHED_PREFIX_FRACTION if seen_text else 1.0)
                        + varying) * PROMPT_TOKENS_PER_BYTE
                warm_prompts.add((key, nid))
            cost = (prefix + work + text
                    + (0 if g.get("sentinel") else spec_input)) * _model_mult(model)
            if not is_warm:
                cold += 1
                warm.add(key)
            raw[nid] = raw.get(nid, 0.0) + cost
            rec = per_node.setdefault(nid, {"calls": 0, "estimatedTokens": 0, "effort": effort,
                                            "model": model or "default"})
            rec["calls"] += 1
    # Rounded once per node, and the headline is the sum of those: a profile
    # that does not add up to the estimate beside it is two numbers to argue
    # with, and the sweep and the metrics fold compare against the profile.
    for nid, rec in per_node.items():
        rec["estimatedTokens"] = int(round(raw[nid]))
    return {
        "total": sum(r["estimatedTokens"] for r in per_node.values()),
        "calls": calls, "cold": cold, "ttl": ttl,
        "perNode": per_node,
        "unpriced": unpriced_expansions(ir),
        "assumptions": {"prefixTokens": PREFIX_TOKENS,
                        "specificationInputTokens": spec_input,
                        "promptTokensPerByte": PROMPT_TOKENS_PER_BYTE,
                        "unpricedExpansions": list(UNPRICED_EXPANSIONS),
                        "cachedPrefixFraction": CACHED_PREFIX_FRACTION,
                        "workTokens": WORK_TOKENS, "effortMult": EFFORT_MULT,
                        "modelMult": dict(MODEL_MULT)},
    }


def unpriced_expansions(ir):
    """[(token, [nodeId, ...])] for every {{token}} the estimate prices at 0.

    A run-time value — a predecessor's result, an honored decision, what a
    sweep has seen — has no size until the run, and neither has a launch
    argument with no argDefault. The estimate cannot see them, so it says
    which ones it could not see and where, rather than letting the number
    read as complete.
    """
    defaults = ir.get("argDefaults") or {}
    found = {}
    for n in ir.get("nodes") or []:
        if is_reduce(n):
            continue
        texts = [str(n.get("prompt", ""))]
        if n.get("actor", "agent") != "agent":
            texts.append(str((n.get("release") or {}).get("instructions", "")))
        for text in texts:
            for tok in SUBST.findall(text):
                root = tok.split(".")[0].split("[")[0]
                label = None
                if root in UNPRICED_EXPANSIONS:
                    label = "{{" + (root + ".*" if root == "decisions" else root) + "}}"
                elif root == "A":
                    arg = tok.split(".")[1].split("[")[0] if "." in tok else None
                    if arg is None:
                        label = "{{A}} (launch args)"
                    elif arg not in defaults:
                        label = "{{A." + arg + "}} (launch arg)"
                if label:
                    ids = found.setdefault(label, [])
                    if n.get("id", "?") not in ids:
                        ids.append(n.get("id", "?"))
    return list(found.items())


def unpriced_note(est):
    """The estimate's blind spots as one clause, or '' when it has none."""
    if not est.get("unpriced"):
        return ""
    return "unpriced: " + "; ".join(
        f"{tok} in {', '.join(ids[:4])}{' …' if len(ids) > 4 else ''}"
        for tok, ids in est["unpriced"])


def effort_transitions(ir):
    """Consecutive agent nodes of one run whose (model, effort) key changes
    to a key that run has not warmed yet.

    Each is a cold prefill for the second node the estimate charges under a
    one-hour TTL. Returns [(from_id, from_key, to_id, to_key)]. It walks the
    groups estimate_tokens() prices, so a hop is reported exactly when the
    estimator charges it:

    - A change back to a key an earlier call of the same run already warmed
      is read from the cache, and is not reported.
    - A node that waits on a park runs in a later run, after the hours a
      person takes; its first call is cold whatever its key, so a change
      placed there costs nothing extra and is not reported either. Reporting
      it warned authors who followed this warning's own advice.
    - A node that does NOT wait on the park runs on in the same run, right
      after the advisor, and keeps its relation to the node before it.
    """
    out, warm, prev = [], {}, {}
    for g in plan_groups(ir):
        track = g["track"]
        keys = warm.setdefault(track, set())
        if g.get("kind") == "work" and g["calls"]:  # an empty foreach spawns nothing
            nid = g["node"].get("id", "?")
            key = g["calls"][0][1]
            p = prev.get(track)
            if p is not None and p[0] != nid and key != p[1] and key not in keys:
                out.append((p[0], p[1], nid, key))
            prev[track] = (nid, key)
        keys.update(k for _, k in g["calls"])
    return out


def plan_node_count(ir):
    """Worst-case agent calls: fan-out times verification times rounds."""
    lists = ir.get("lists") or {}
    total = 0
    for n in ir.get("nodes") or []:
        # A reduce node is pure emitted code: no spawn, no advisor, no cost.
        if is_reduce(n):
            continue
        # A parked node spawns no worker, but it does spawn one advisor to
        # produce the recommendation that goes with the block. Pricing it at
        # zero would make the ceiling lie by exactly the number of parks.
        if n.get("actor", "agent") != "agent":
            total += 1
            continue
        fan = len(lists.get(n.get("foreach"), [None])) if n.get("foreach") else 1
        per_round = fan
        m = TIER.match(str(n.get("verify", "schema-only")))
        if m:
            cnt = int(m.group(2) or m.group(3) or 0)
            if cnt:
                # Verification runs per produced item; assume the declared
                # expectation, defaulting to 3 items per producing node.
                per = int(n.get("expectItems", 3))
                per_round += fan * per * cnt
            elif m.group(1) == "harness":
                per_round += fan
        # A discovery node re-runs until it goes dry; the ceiling must price
        # the worst case, not one round of it.
        rounds = int((n.get("repeat") or {}).get("maxRounds", 1))
        total += per_round * max(1, rounds)
    return total


# ------------------------------------------------------------------ emission
def list_var(name):
    """JS identifier for a lists key. Validated by IDENT, so this is a
    rename, not sanitisation — the guarantee lives in validate()."""
    return "LIST_" + name.replace("-", "_")


def panel_size(n):
    """Refuters per item for this node's tier; 0 when it needs none."""
    m = TIER.match(str(n.get("verify", "schema-only")))
    return int(m.group(2) or m.group(3) or 0) if m else 0


def lesson_sink(n, need):
    """JS arrow filing one judged item as a lesson.

    The killed half is the point. Today `verifyItems` filters rejects away
    and the reasoning dies with them, so the next sweep re-finds the item
    and pays a fresh panel to reach the verdict that already existed.
    """
    mem = n["memory"]
    keys = (n.get("repeat") or {}).get("dedupeBy") or mem.get("key")
    keyexpr = " + '|' + ".join(f"String(it[{js_str(k)}])" for k in keys)
    status = (f"(it.kills || 0) >= {need} ? 'killed' : 'surviving'"
              if need > 0 else "'surviving'")
    cls = mem.get("classBy")
    clsexpr = (", classKey: " + " + '|' + ".join(
        f"String(it[{js_str(k)}] || '')" for k in cls)) if cls else ""
    return (
        f"it => MEMORY.push({{ tag: {js_str(mem['emit'])}, dedupeKey: {keyexpr}, "
        f"claim: String(it.claim || ''), status: {status}, "
        f"objection: (it.objections || [])[0] || '', kills: it.kills || 0, "
        f"node: {js_str(n['id'])}{clsexpr} }})"
    )


def js_str(s):
    return json.dumps(str(s))


def js_tmpl(s):
    """Escape for a JS TEMPLATE literal: backslash, backtick, and ${.

    json.dumps closes the quote-termination hole but not this one -- its
    output is a DOUBLE-quoted string, and ${ inside a backtick context
    interpolates, so a haltWhen literal reached executable position through
    the very log line that reported the halt.
    """
    return (str(s).replace(chr(92), chr(92) * 2)
            .replace(chr(96), chr(92) + chr(96))
            .replace("${", chr(92) + "${"))


# Roots whose values are launch or list data: rendered through tokenText(),
# so an object or a list reaches the prompt as JSON rather than as
# "[object Object]" or a comma-joined flattening.
TEXT_ROOTS = {"item", "A"}


def js_template(s, mapping=None):
    """Render an IR prompt as a JS template literal, honoring {{expr}}.

    mapping rewrites bare tokens to JS expressions, which is how {{prev}}
    reaches a downstream node's prompt without a global temp binding. An
    unmapped {{item...}} or {{A...}} goes through tokenText(): a bare
    {{item}} over a list of objects used to hand the worker the string
    "[object Object]" — no item data at all — while the estimate priced it
    as the object's JSON.
    """
    mapping = mapping or {}
    s = s.replace("\\", "\\\\").replace("`", "\\`").replace("${", "\\${")

    def bind(m):
        tok = m.group(1)
        if tok in mapping:
            return "${" + mapping[tok] + "}"
        if tok.split(".")[0].split("[")[0] in TEXT_ROOTS:
            return "${tokenText(" + tok + ")}"
        return "${" + tok + "}"
    s = SUBST.sub(bind, s)
    return "`" + s + "`"


def subst_mapping(n, var=None, texts=None):
    """The JS expression each blessed {{token}} binds to.

    validate() proves a token is in scope; this is the other half of that
    promise -- the binding the emitter actually makes. A token blessed there
    and left unmapped here emits `${prev}` against no such variable: a graph
    that compiles clean and throws ReferenceError at launch, which is the
    precise failure the scope check exists to prevent.

    `texts` names the strings scanned for {{prev.<field>}} projections, so a
    field a parked node uses only in its release instructions is bound too.
    """
    texts = [str(n.get("prompt", ""))] if texts is None else texts
    mapping = {}
    if n.get("after"):
        # {{prev}} carries the predecessor's contract into this prompt -- the
        # justified barrier (judging candidates side by side, reducing a set).
        # {{prev.<field>}} projects one validated field instead: bracket access
        # through js_str so the field name can never become code, `?? null` so
        # an absent optional field reads as null rather than the string
        # "undefined". validate() already proved the field is in the contract.
        mapping["prev"] = f"JSON.stringify(RESULTS[{js_str(n['after'])}])"
        for text in texts:
            for tok in SUBST.findall(text):
                if (tok.startswith("prev.") and tok.count(".") == 1
                        and "[" not in tok):
                    fld = tok.split(".", 1)[1]
                    mapping[tok] = (
                        f"JSON.stringify((RESULTS[{js_str(n['after'])}] || {{}})"
                        f"[{js_str(fld)}] ?? null)")
    if n.get("repeat") and var:
        # Later rounds are told what earlier rounds already surfaced, so the
        # finder spends its round on new ground instead of re-reporting.
        mapping["seen"] = f"(seenList_{var}.join('; ') || 'nothing yet')"
    # Honored decisions arrive as the record alone, at any distance and with
    # no `after` chain. Pasting the deciding node's whole result instead is
    # the context-packet smell graph-auditor already flags -- and `after` is
    # single-valued, so a chain could not carry more than one hop anyway.
    for h in (n.get("honors") or []):
        mapping[f"decisions.{h}"] = f"JSON.stringify(DECISIONS[{js_str(h)}])"
    return mapping


def opts(n, ir, phase, label):
    roles = ir.get("roles") or {}
    role = roles.get(n.get("role"), {})
    parts = [f"label: {label}", f"phase: {js_str(phase)}", f"schema: C[{js_str(n['contract'])}]"]
    agent_type = n.get("agentType") or role.get("agentType")
    if agent_type:
        parts.append(f"agentType: {js_str(agent_type)}")
    effort = n.get("effort") or role.get("effort") or (ir.get("defaults") or {}).get("effort")
    if effort:
        parts.append(f"effort: {js_str(effort)}")
    model = n.get("model") or role.get("model")
    if model:
        parts.append(f"model: {js_str(model)}")
    if n.get("isolation") == "measure":
        # No runtime worktree: the node snapshots `git archive HEAD` itself
        # in the shared cwd (see measurePreamble), which both freezes the
        # tree and guarantees the base is the campaign's — the runtime's
        # worktree promises neither.
        pass
    elif n.get("isolation") or n.get("mutates"):
        parts.append("isolation: 'worktree'")
    return "{ " + ", ".join(parts) + " }"


def halt_parts(n):
    """(field, js_operator, js_literal) for a validated haltWhen.

    The literal is re-emitted from its parsed value rather than pasted from
    the regex match: a crafted quote in the source text must not be able to
    terminate the string it lands in.
    """
    m = HALT.match(str(n["haltWhen"]))
    field, op, lit = m.group(1), m.group(2), m.group(3)
    op = {"==": "===", "!=": "!=="}.get(op, op)
    lit = json.dumps(int(lit)) if re.fullmatch(r"-?\d+", lit) else json.dumps(lit[1:-1])
    return field, op, lit


def git_prefix(root):
    """The project's path inside its git repository ('' at the top level).

    A project below a monorepo's top level is supported by every hook, and
    `git status --porcelain` names paths from the repository root, so the
    tree guard's ignore list has to carry this prefix to match anything.
    """
    try:
        import subprocess
        r = subprocess.run(["git", "-C", root or ".", "rev-parse", "--show-prefix"],
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def red_team_checked(n):
    """A node whose raw RedTeamV1 result the emitted code can re-derive."""
    return (not is_reduce(n) and n.get("contract") == "RedTeamV1"
            and n.get("actor", "agent") == "agent"
            and not n.get("repeat") and not panel_size(n))


def emit_red_team_check(n, var, many=False):
    """Halt on a red-team result its own findings contradict."""
    nid = n["id"]
    items = var if many else f"[{var}]"
    return (
        f"const incoherent_{var} = {items}.map(redTeamIncoherent).filter(Boolean)\n"
        f"if (incoherent_{var}.length) {{\n"
        f"  log(`HALT at {nid}: red-team result contradicts itself — ${{incoherent_{var}[0]}}`)\n"
        f"  note({js_str(nid)}, 'HALTED', incoherent_{var}.join('; '))\n"
        f"  RESULTS[{js_str(nid)}] = {var}\n"
        f"  return summary('HALTED')\n"
        f"}}"
    )


def emit_halt(n, var):
    field, op, lit = halt_parts(n)
    return (
        f"if ({var} && {var}.{field} {op} {lit}) {{\n"
        f"  log(`HALT at {n['id']}: {field}=${{{var}.{field}}} — ` + {js_str(n.get('haltReason', 'halt condition met'))})\n"
        f"  RESULTS[{js_str(n['id'])}] = {var}\n"
        f"  return summary('HALTED')\n"
        f"}}"
    )


def emit_halt_any(n, var):
    """haltWhen across a fan-out: any tripping item halts the campaign."""
    field, op, lit = halt_parts(n)
    return (
        f"const tripped_{var} = {var}.filter(r => r && r.{field} {op} {lit})\n"
        f"if (tripped_{var}.length) {{\n"
        f"  log(`HALT at {n['id']}: ${{tripped_{var}.length}}/${{{var}.length}} item(s) "
        f"tripped {js_tmpl(field)} {js_tmpl(op)} {js_tmpl(lit)} — ` + {js_str(n.get('haltReason', 'halt condition met'))})\n"
        f"  note({js_str(n['id'])}, 'HALTED', `${{tripped_{var}.length}} item(s) tripped "
        f"{field}`)\n"
        f"  RESULTS[{js_str(n['id'])}] = {var}\n"
        f"  return summary('HALTED')\n"
        f"}}"
    )


def emit(ir, contracts, imports_resolved=None, specification=None, graph_file=None,
         project_prefix=""):
    nodes = ir["nodes"]
    lists = ir.get("lists") or {}
    nodes_by_id = {x.get("id"): x for x in nodes}
    tree_guard = ir.get("treeGuard", True)
    measure_nodes = [n for n in nodes if n.get("isolation") == "measure"]
    worktree_nodes = [n for n in nodes
                      if n.get("isolation") in (True, "worktree") or n.get("mutates")]
    used = sorted({n["contract"] for n in nodes if not is_reduce(n)}
                  # The advisor emits DecisionV1 whether or not a node
                  # declares it, so its schema has to be in scope.
                  | ({"DecisionV1"} if any(
                      x.get("actor", "agent") != "agent" for x in nodes) else set())
                  # The tree sentinel is not a node, but its schema still has
                  # to be in scope for the agent call to be schema-forced.
                  | ({"TreeCheckV1"} if tree_guard else set()))
    phases, seen_phase = [], set()
    for n in nodes:
        p = n.get("phase", "Run")
        if p not in seen_phase:
            seen_phase.add(p)
            phases.append(p)
    needs_panel = any(panel_size(n) for n in nodes)

    est = estimate_tokens(ir, contracts, specification)
    L = []
    a = L.append
    a("// GENERATED by fluxpoint compile-graph.py — DO NOT EDIT.")
    a("// Source of truth is the ```json graph-ir block in WORK.md.")
    a("// Regenerate with /fluxpoint:graph-run (or compile-graph.py).")
    a(f"// estimate: ~{est['total']:,} tokens across {est['calls']} agent call(s), "
      f"{est['cold']} cold prefill(s), prompt-cache TTL {est['ttl']} "
      f"(stated assumptions live in compile-graph.py; metrics.py checks them "
      f"against spent)")
    if unpriced_note(est):
        a(f"// {unpriced_note(est)}")
    a("export const meta = {")
    a(f"  name: {js_str(ir.get('name') or 'graph-campaign')},")
    a(f"  description: {js_str(ir['campaign'])},")
    a("  phases: [")
    for p in phases:
        a(f"    {{ title: {js_str(p)} }},")
    a("  ],")
    a("}")
    a("")
    a("// --- contracts (from contracts/*.schema.json, referenced by name) ---")
    a("const C = {")
    for name in used:
        a(f"  {json.dumps(name)}: {json.dumps(contracts[name], indent=2)},")
    a("}")
    a("")
    a("// --- inputs: normalize object | JSON string | bare string, fail loudly ---")
    a("const A =")
    a("  args && typeof args === 'object'")
    a("    ? args")
    a("    : typeof args === 'string' && args.trim()")
    a("    ? (() => { try { return JSON.parse(args) } catch { return { target: args } } })()")
    a("    : {}")
    for req in ir.get("requiredArgs") or []:
        a(f"if (!A[{js_str(req)}]) throw new Error({js_str('missing required arg: ' + req)})")
    for k, v in (ir.get("argDefaults") or {}).items():
        a(f"if (!A[{js_str(k)}]) A[{js_str(k)}] = {js_str(v)}")
    a(f"const campaign = {js_str(ir['campaign'])}")
    a("// {{item...}} / {{A...}} render text as itself and structure as JSON.")
    a("function tokenText(v) { return v !== null && typeof v === 'object' ? JSON.stringify(v) : String(v) }")
    if specification:
        a("const SPECIFICATION = " + json.dumps(specification, ensure_ascii=True))
        a("function specificationPreamble() { return " + js_str(SPEC_PREAMBLE) + " + JSON.stringify(SPECIFICATION) + '\\n'; }")
    a("// Resolved inputs are logged, never silently defaulted behind your back.")
    a("log(`inputs: ${JSON.stringify(A)}`)")
    # What the spec priced, next to what the run will meter. The profile is
    # per node — effort, model, calls, estimated tokens — so a later sweep of
    # effort settings has a per-role number to compare against `spent`.
    a("// --- cost: what the compiler estimated, so the run can be held to it ---")
    a("const ESTIMATE = " + json.dumps(
        {"total": est["total"], "calls": est["calls"], "cold": est["cold"],
         "cacheTtl": est["ttl"]}, sort_keys=True))
    a("const PROFILE = " + json.dumps(est["perNode"], sort_keys=True))
    a(f"log(`estimate: ~${{ESTIMATE.total}} tokens across ${{ESTIMATE.calls}} call(s), "
      f"${{ESTIMATE.cold}} cold prefill(s), prompt-cache TTL {est['ttl']}`)")
    blocking = [n for n in nodes if n.get("foreach") or n.get("repeat") or panel_size(n)
                or n.get("actor", "agent") != "agent"]
    if blocking:
        if CACHE_TTLS.get(est["ttl"], 300) >= 3600:
            a("log('prompt cache: this graph fans out or parks and declares a 1-hour "
              "TTL — run it in a session configured for one, or every hop below is "
              "a cold prefill the estimate did not charge for')")
        else:
            a("log('prompt cache: this graph fans out or parks under the default "
              "5-minute TTL — the parent prefix expires while it blocks, and every "
              "hop is priced cold; declare budget.cacheTtl 1h once the session "
              "provides it')")
    a("")
    if lists:
        a("// --- lists ---")
        for name, items in lists.items():
            a(f"const {list_var(name)} = {json.dumps(items, indent=2)}")
        a("")
    decides_any = [n for n in nodes if n.get("decides")]
    imports = ir.get("imports") or {}
    if decides_any or imports:
        a("// --- decisions: the choice itself, not the prose around it ---")
        a("// A decision that overturned the prior is the one a fresh context")
        a("// silently re-decides the other way, and for a parameter that")
        a("// freezes at genesis the re-decision is unrecoverable.")
        a("const DECISIONS = {}")
    if imports:
        unresolved = sorted(set(imports) - set(imports_resolved or {}))
        if unresolved:
            # Emission-time backstop only: main() resolves before emitting,
            # and a caller that skips resolution must fail loudly rather
            # than regenerate the couriered-map hole this closed.
            raise GraphError(
                "imports must be resolved before emission (unresolved: "
                + ", ".join(unresolved) + ") — go through resolve_imports(), "
                "so the record is embedded rather than couriered by an agent")
        a("// Imported from earlier campaigns. A ten-node ceiling forces a big")
        a("// campaign to split, and the split is lossy unless a frozen choice")
        a("// can cross the boundary. Each record below was resolved from")
        a("// .claude/fluxpoint/runs at compile time and embedded — no")
        a("// hand-assembled map exists for a launch to trust, and no model")
        a("// sits between the recorded choice and this run. Re-deciding it")
        a("// requires a new deciding campaign, not a different argument.")
        for k in sorted(imports):
            r = imports_resolved[k]
            a(f"// {k} <- run {js_str(r['runId'])}")
            a(f"DECISIONS[{js_str(k)}] = "
              + json.dumps(r["record"], indent=2, sort_keys=True))
        a("const DECISIONS_IMPORTED = " + json.dumps(
            {k: imports_resolved[k]["runId"] for k in sorted(imports)},
            sort_keys=True))
        a("")
    prove_nodes = [n for n in nodes if prove_gate(n)]
    if prove_nodes:
        a("// --- prove: tiers ---")
        a("// This graph cannot check these itself: the attest log is a file and")
        a("// this sandbox has none. What it can do is refuse a result that is")
        a("// not even shaped like a citation, and name which node claimed which")
        a("// gate so record-run.py can hold each to the hook's own record.")
        a("const PROVE = " + json.dumps(
            {n["id"]: prove_gate(n) for n in prove_nodes}, sort_keys=True))
        a("// The launch stamp is what binds a citation to THIS run: record-run.py")
        a("// refuses a cited row minted before `since`, or by a run with another")
        a("// nonce, so a node cannot pass by citing an execution it did not cause —")
        a("// an earlier run's, or an overlapping one's on the same commit. It rides")
        a("// out in the summary; a resume reuses the stamp of the run it resumes.")
        a("if (!A._launch || !A._launch.since || !/^[A-Za-z0-9_-]{1,64}$/.test(String(A._launch.nonce || '')))")
        a("  throw new Error('this graph has prove: nodes but no launch stamp {since, nonce} was passed "
          "— launch it with /fluxpoint:graph-run, which stamps args._launch with attest.py --stamp')")
        a("const LAUNCH = A._launch")
        a("// The project the stamp was minted in: a node that steps into a worktree")
        a("// names it, so the wrapper attests into the log record-run.py reads.")
        a("const ROOT_ARG = typeof LAUNCH.root === 'string' && LAUNCH.root ? ' --root ' + JSON.stringify(LAUNCH.root) : ''")
        a("// Injected ahead of each prove: node's prompt. The nonce is what makes")
        a("// the witness's row this run's; a gate run without it is cited as STALE.")
        a("function provePreamble(gate) {")
        a("  return gate === 'ci'")
        a("    ? `PROVE GATE ci — ask the forge, never choose the commit yourself: run the fluxpoint plugin's scripts/py.sh attest.py --ci${ROOT_ARG} --pr <the pull request> --wait --nonce ${LAUNCH.nonce} (or --ref <branch>), and return gate 'ci', the exit and attestId it prints, and the sha it names. Whatever merges after this gate must pin that sha (gh pr merge --match-head-commit <sha>).\\n\\n`")
        a("    : `PROVE GATE ${gate} — run the command .fluxpoint-gates.json declares for '${gate}' exactly, prefixed with FPL_ATTEST_NONCE=${LAUNCH.nonce} and nothing else around it (no pipe, no || true). If it can outlive one tool call, run the fluxpoint plugin's scripts/py.sh attest.py --run ${gate}${ROOT_ARG} --nonce ${LAUNCH.nonce} in the background and collect it with attest.py --await${ROOT_ARG} <token>. Return gate '${gate}', the exit, and the attestId the witness recorded (scripts/py.sh attest.py --last ${gate}${ROOT_ARG} --nonce ${LAUNCH.nonce} prints it).\\n\\n`")
        a("}")
        a("function citation(id, gate, r) {")
        a("  if (!r || typeof r !== 'object') return `${id}: no result to prove`")
        a("  if (r.gate !== gate) return `${id}: claims gate '${r.gate}', declared '${gate}'`")
        a("  if (typeof r.exit !== 'number') return `${id}: no integer exit`")
        a("  if (!r.attestId) return `${id}: no attestId — an exit code nothing witnessed`")
        a("  if (gate === 'ci' && !r.sha) return `${id}: CI verdict names no commit — the merge cannot be pinned to it`")
        a("  return null")
        a("}")
        a("")
    if any(red_team_checked(n) for n in nodes):
        a("// --- red-team coherence: the verdict its own findings allow ---")
        a("// RedTeamV1 binds worstSeverity to the findings and refuses a SHIP over")
        a("// HIGH or CRITICAL, but a runtime validator that skipped conditional")
        a("// keywords would let exactly that through to the next node — often a")
        a("// person about to sign. Re-derived here from the findings, in code.")
        a("const SEVERITY_RANK = { NONE: 0, LOW: 1, MEDIUM: 2, HIGH: 3, CRITICAL: 4 }")
        a("function redTeamIncoherent(r) {")
        a("  if (!r || typeof r !== 'object') return null")
        a("  const worst = (Array.isArray(r.findings) ? r.findings : [])")
        a("    .reduce((w, f) => Math.max(w, SEVERITY_RANK[f && f.severity] || 0), 0)")
        a("  const name = Object.keys(SEVERITY_RANK).find(k => SEVERITY_RANK[k] === worst)")
        a("  if (r.verdict === 'SHIP' && worst >= SEVERITY_RANK.HIGH) return `SHIP over a ${name} finding`")
        a("  // A repo-local RedTeamV1 without the ordinal is still held to the SHIP rule.")
        a("  if (('worstSeverityRank' in r || 'worstSeverity' in r) &&")
        a("      (r.worstSeverityRank !== worst || SEVERITY_RANK[r.worstSeverity] !== worst))")
        a("    return `worstSeverity ${r.worstSeverity}/${r.worstSeverityRank} disagrees with its findings, whose worst is ${name}`")
        a("  return null")
        a("}")
        a("")
    mem_nodes = [n for n in nodes if n.get("memory")]
    seeds = sorted({n["memory"]["seed"] for n in mem_nodes if n["memory"].get("seed")})
    if mem_nodes:
        a("// --- lessons: what earlier campaigns established ---")
        a("// Rows are filed by record-run.py from this summary, never written")
        a("// by an agent, and keyed by the same dedupe fields the IR already")
        a("// declares — so cross-run identity costs no new vocabulary.")
        a("const MEMORY = []")
        a("const MEMORY_SEEDED = {}")
    if seeds:
        a("// Seeds are ADVISORY and only ever reach a prompt. Seeding the")
        a("// dedup set instead would silently drop a re-found item, which is")
        a("// precisely how a stale lesson hides a live regression: the finder")
        a("// reports it, the loop discards it as already-known, and the sweep")
        a("// reads clean. So a seeded key tells the finder where the frontier")
        a("// was; it never decides what this run is allowed to find.")
        a("const _seedSrc = (A && A._seen) || {}")
        for tag in seeds:
            a(f"MEMORY_SEEDED[{js_str(tag)}] = "
              f"((_seedSrc[{js_str(tag)}] || {{}}).keys || []).length")
        a("// A first sweep and one whose loader never ran look identical from")
        a("// inside; the count rides out in the summary so they do not read")
        a("// the same in the record.")
        for tag in seeds:
            a(f"log(`memory: seeded ${{MEMORY_SEEDED[{js_str(tag)}]}} prior key(s) "
              f"for tag {tag}`)")
        a("")
    pri_tags = sorted({n["memory"]["seed"] for n in mem_nodes
                       if n["memory"].get("priors") and n["memory"].get("seed")})
    if pri_tags:
        a("// Killed priors ride into refuter prompts: the objection that")
        a("// killed a claim once is the argument a fresh panel would pay a")
        a("// full fan-out to rediscover. Priors, never verdicts — the code")
        a("// may have changed, so a re-found item is still judged on its")
        a("// merits and a prior can be overturned by anyone who reads it.")
        a("const PRIORS = {}")
        for tag in pri_tags:
            a("{")
            a(f"  const k = ((_seedSrc[{js_str(tag)}] || {{}}).killed || [])"
              f".slice(0, 5)")
            a(f"  PRIORS[{js_str(tag)}] = k.length")
            a("    ? `Related claims earlier panels KILLED — priors, not "
              "verdicts; the code may have changed since:\\n` +")
            a("      k.map(p => `- ${String(p.claim || '').slice(0, 400)}"
              "${p.objection ? ` [killed because: "
              "${String(p.objection).slice(0, 400)}]` : ''}`).join('\\n') + "
              "`\\n\\n`")
            a("    : ''")
            a("}")
        a("")
    parked = [n for n in nodes if n.get("actor", "agent") != "agent"]
    if parked:
        a("// --- actors: nodes no agent can run ---")
        a("// Without this the engine had two answers for a node it could not")
        a("// complete: halt everything, or drop it and continue with a null.")
        a("// Neither is 'this one is blocked, work the other branches'.")
        a("if (!A._releases || typeof A._releases !== 'object')")
        a("  throw new Error('this graph has human or third-party nodes but no "
          "releases map — launch it with /fluxpoint:graph-run, which loads "
          ".claude/fluxpoint/releases into args._releases')")
        a("const RELEASES = A._releases")
        a("// Blocked is inherited: handing a dependent the literal null of a")
        a("// node nobody ran would report a failure where there is a wait.")
        a("const BLOCKED = new Set()")
        a("// A block handed over with no recommendation is a punt. Each")
        a("// one carries the reasoned alternative the advisor produced.")
        a("const RECOMMENDATIONS = {}")
        a("const WAITS = []")
        a("")
    if any(n.get("irreversible") for n in nodes):
        a("// --- irreversible effects: once-only ledger ---")
        a("// Resume is the flagship recovery path and also the operation that")
        a("// double-mints: repairing any upstream node re-fires everything")
        a("// after it. The ledger is consulted before every irreversible")
        a("// spawn, so replay-safety is structural rather than something the")
        a("// operator has to remember. It cannot help across a crash between")
        a("// the effect and the run's end — nothing this script can reach")
        a("// could, since it has no filesystem.")
        a("if (!A._ledger || typeof A._ledger !== 'object')")
        a("  throw new Error('this graph has irreversible nodes but no ledger was "
          "passed — launch it with /fluxpoint:graph-run, which loads "
          ".claude/fluxpoint/irreversible.jsonl into args._ledger')")
        a("const LEDGER = A._ledger")
        a("const LEDGER_WRITES = []")
        a("// FNV-1a over the resolved prompt. A change-detector, not a security")
        a("// boundary: a different prompt is a different operation and earns a")
        a("// new key, which is also why editing a ceremony prompt re-arms it.")
        a("// The confirm gate is what backstops that.")
        a("function ledgerKey(id, prompt) {")
        a("  let h = 2166136261")
        a("  for (let i = 0; i < prompt.length; i++) {")
        a("    h ^= prompt.charCodeAt(i); h = Math.imul(h, 16777619)")
        a("  }")
        a("  return `${campaign}|${id}|${(h >>> 0).toString(16)}`")
        a("}")
        a("// Naming the campaign is not naming the effect: --confirm lists the")
        a("// node ids a human authorized, so a blanket yes cannot carry an")
        a("// unrelated ceremony along with it.")
        a("function confirmed(id) {")
        a("  return String((A && A.confirm) || '').split(',')")
        a("    .map(s => s.trim()).filter(Boolean).includes(id)")
        a("}")
        a("")
    a("const RESULTS = {}")
    a("const PROVENANCE = []")
    a("// The optional data arg is structured, not prose: round counts, worker")
    a("// tallies, reduce before/after. metrics.py aggregates these across runs,")
    a("// and numbers buried in detail strings would make it parse sentences.")
    a("function note(id, status, detail, data) { PROVENANCE.push(data ? { node: id, status, detail: detail || '', data } : { node: id, status, detail: detail || '' }) }")
    # Which node returned which contract is known here and nowhere else.
    # Without it a reader of the summary has to guess a result's type from
    # its shape, and a recorder that guesses will eventually file a harness
    # exit code as a red-team verdict.
    a("// nodeId -> contract, so a consumer of the summary reads types rather")
    a("// than sniffing them out of the result's shape. A reduce node carries")
    a("// its source's contract: its output is that contract's items, fewer.")
    a("const CONTRACTS = {")
    for n in ir["nodes"]:
        c = n["contract"] if not is_reduce(n) else derived_contract(n, nodes_by_id)
        a(f"  {js_str(n['id'])}: {js_str(c)},")
    a("}")
    a("// Set whenever the campaign covered less ground than it set out to —")
    a("// budget declined work, or a sweep ended on its ceiling with more to")
    a("// find. It rides all the way out to the Evidence row, so a partial run")
    a("// can never be read as a clean one.")
    a("let INCOMPLETE = false")
    a("function summary(outcome) {")
    a("  const final = outcome === 'COMPLETE' && INCOMPLETE ? 'INCOMPLETE' : outcome")
    extra = ""
    if specification:
        extra += ", specification: SPECIFICATION.identity"
        if specification.get("path"):
            extra += ", specificationPath: SPECIFICATION.path"
    if any(n.get("irreversible") for n in nodes):
        extra += ", ledger: LEDGER_WRITES"
    if parked:
        extra += ", blocked: [...BLOCKED], waits: WAITS, recommendations: RECOMMENDATIONS"
    if decides_any or imports:
        extra += ", decisions: DECISIONS"
    if mem_nodes:
        extra += ", memory: MEMORY, memorySeeded: MEMORY_SEEDED"
    if prove_nodes:
        extra += ", prove: PROVE, launch: LAUNCH"
    if tree_guard:
        extra += ", tree: TREE"
    reducers = [n["id"] for n in nodes if is_reduce(n)]
    if reducers:
        # Named so a consumer counting produced items can tell a reducer's
        # output (the same items, fewer) from work a node actually produced.
        extra += ", reducers: " + json.dumps(reducers)
    # What the run actually cost, next to what the spec priced. planned is
    # the compile-time worst case; spawned is the calls that really went out;
    # spent is the runtime's own token meter. Without these in the artifact,
    # fan-out efficiency and budget accuracy are vibes, not numbers.
    if (ir.get("budget") or {}).get("maxNodes") is not None:
        extra += ", spawned: SPAWNED, declined: DECLINED"
    # spent is guarded: it is a newer runtime API than the rest of the
    # budget surface, and a summary that throws on an older runtime loses
    # the entire run record over a nice-to-have number.
    extra += (f", planned: {plan_node_count(ir)}, "
              "spent: (budget && typeof budget.spent === 'function') ? budget.spent() : null, "
              "estimate: ESTIMATE, profile: PROFILE")
    if imports:
        # Which run each imported record came from rides out in the summary,
        # so provenance can tell an imported decision from one this campaign
        # made — record-run.py files only the latter as new Decisions rows.
        extra += ", decisionsImported: DECISIONS_IMPORTED"
    # What the run was asked, minus the launcher's own state (_ledger,
    # _releases, _base, ...): an effort sweep scores a run against the case
    # it was launched for, and the summary is all record-run keeps.
    a("  const inputs = Object.fromEntries(Object.entries(A).filter(([k]) => !k.startsWith('_')))")
    a("  return { campaign, outcome: final, results: RESULTS, provenance: PROVENANCE,")
    a(f"           contracts: CONTRACTS, inputs{extra} }}")
    a("}")
    a("")
    budget_cfg = ir.get("budget") or {}
    floor = budget_cfg.get("verifyFloorTokens", 50000)
    node_floor = budget_cfg.get("nodeFloorTokens", floor)
    a("// --- budget: work nodes get their own floor, not just verification ---")
    a(f"const NODE_FLOOR = {int(node_floor)}")
    max_nodes = budget_cfg.get("maxNodes")
    if max_nodes is not None:
        a("// maxNodes was a compile-time estimate only, and the estimate leans on")
        a("// expectItems, which is a guess. A run that found more than expected")
        a("// could quietly exceed its own declared ceiling, so the ceiling is")
        a("// counted at run time too.")
        a(f"const MAX_NODES = {int(max_nodes)}")
        a("let SPAWNED = 0")
        a("// A declined spawn and a dead agent both come back null, and they are")
        a("// different sentences: one is the campaign hitting its own declared")
        a("// ceiling, the other is a worker failing. Call sites read these to")
        a("// file SKIPPED where record-run doctrine says budget-declined work")
        a("// is neither success nor failure — never DEAD, never a halt reason.")
        a("let DECLINED = 0")
        a("const DECLINED_LABELS = new Set()")
        a("// Every agent in this graph is spawned through here, so the ceiling")
        a("// counts what actually ran.")
        a("async function spawn(prompt, opts) {")
        a("  if (SPAWNED >= MAX_NODES) {")
        a("    log(`budget ceiling: ${opts.label} NOT RUN — ${SPAWNED}/${MAX_NODES} agent call(s) already spawned`)")
        a("    INCOMPLETE = true")
        a("    DECLINED++")
        a("    DECLINED_LABELS.add(String(opts.label))")
        a("    return null")
        a("  }")
        a("  SPAWNED++")
        a("  return agent(prompt, opts)")
        a("}")
    a("// True when there is room to spawn work. Anything declined is announced")
    a("// and recorded as SKIPPED — a graph never quietly does less than it says.")
    a("function affordable(label) {")
    a("  if (!budget.total) return true")
    a("  const rem = budget.remaining()")
    a("  if (rem < NODE_FLOOR) {")
    a("    log(`budget floor: ${label} NOT RUN — ${Math.round(rem / 1000)}k remaining < ${Math.round(NODE_FLOOR / 1000)}k floor`)")
    a("    INCOMPLETE = true")
    a("    return false")
    a("  }")
    a("  return true")
    a("}")
    a("")
    if needs_panel:
        a("// --- verification: refuters attack the claim; majority kills it ---")
        a(f"const VERIFY_FLOOR = {int(floor)}")
        a("async function refute(claimText, label, phase, n, priors) {")
        a("  if (budget.total && budget.remaining() < VERIFY_FLOOR) {")
        a("    log(`budget floor reached — ${label} left UNVERIFIED (no silent caps)`)")
        a("    return { kills: 0, cast: 0, unverified: true }")
        a("  }")
        a("  const votes = await parallel(Array.from({ length: n }, (_, i) => () =>")
        a("    spawn(")
        if specification:
            a("      specificationPreamble() +")
        a("      `Attempt to REFUTE this claim. ${claimText}\\n\\n` + (priors || '') +")
        a("        `Re-read the underlying code or evidence YOURSELF; do not trust the claim's own summary. ` +")
        a("        `Hunt for the reason it is wrong: a guard upstream, a type that forbids the state, a test that pins it. ` +")
        a("        `Default to refuted=true when uncertain.`,")
        a("      { label: `${label}:refute${i + 1}`, phase, schema: C.VerdictV1, effort: 'low' }")
        a("    )")
        a("  ))")
        a("  const cast = votes.filter(Boolean)")
        a("  // Any missing vote — a dead refuter or one declined by the node")
        a("  // ceiling — means this claim was not fully checked, and says so.")
        a("  if (cast.length < n) log(`${label}: only ${cast.length}/${n} votes cast — UNVERIFIED`)")
        a("  return {")
        a("    kills: cast.filter(v => v.refuted).length,")
        a("    cast: cast.length,")
        a("    unverified: cast.length < n,")
        a("    // The arguments that produced the verdict, not just the tally.")
        a("    // Discarding them threw away the most reusable thing a panel")
        a("    // makes: a survivor with its strongest objection recorded is")
        a("    // worth more later than a survivor with a vote count.")
        a("    objections: cast.map(v => v.reason).filter(Boolean).slice(0, 5),")
        a("  }")
        a("}")
        a("")
        a("// Applies a node's declared tier to every item it produced. Used by")
        a("// fan-out and single nodes alike, so a declared tier always runs.")
        a("async function verifyItems(result, field, label, phase, n, need, sink) {")
        a("  if (!result) return []")
        a("  const produced = result[field] || []")
        a("  const judged = await parallel(produced.map((it, i) => () =>")
        a("    refute(JSON.stringify(it), `${label}:${i}`, phase, n).then(v => ({ ...it, ...v }))")
        a("  ))")
        a("  const all = judged.filter(Boolean)")
        a("  // The sink sees every verdict, including the kills the filter")
        a("  // below drops: what a panel rejected, and why, is the half a")
        a("  // later sweep would otherwise pay to rediscover.")
        a("  if (sink) all.forEach(sink)")
        a("  return all.filter(v => v.kills < need)")
        a("}")
        a("")

    isolated = measure_nodes or worktree_nodes
    if isolated:
        a("// --- campaign base: what tree the isolated nodes think they are on ---")
        a("// A worktree's base is the runtime's choice, not this script's, and it")
        a("// has been observed cut from the default branch: files the campaign")
        a("// committed are simply ABSENT there, and nothing inside the worktree")
        a("// says so. Every isolated node is told the campaign's base and told to")
        a("// assert it before trusting what it sees.")
        a("if (!A._base || !A._base.sha)")
        a("  throw new Error('this graph has isolated nodes but no base was passed "
          "— launch it with /fluxpoint:graph-run, which loads `git rev-parse HEAD` "
          "and the branch name into args._base')")
        a("const BASE = A._base")
        a("")
    if worktree_nodes:
        a("// Injected ahead of every worktree-isolated node's own prompt. The")
        a("// wrong-base failure is silent from inside — git status is clean and a")
        a("// missing file looks like a missing file — so the assertion has to be")
        a("// the node's first act, with the expected base supplied, not recalled.")
        a("function basePreamble() {")
        a("  return `WORKTREE BASE CHECK — before anything else.\\n` +")
        a("    `You are in an isolated worktree, and the runtime chose its base; it has been observed cut from the default branch rather than the campaign's.\\n` +")
        a("    `The campaign's base is commit ${BASE.sha}` + (BASE.branch ? ` on branch ${BASE.branch}` : '') + `.\\n` +")
        a("    `1. Run: git rev-parse HEAD\\n` +")
        a("    `2. If it is not ${BASE.sha}, run: git merge-base --is-ancestor ${BASE.sha} HEAD\\n` +")
        a("    `3. If that fails, this tree is MISSING work the campaign committed. Do not report absent files or failed checks as findings — they describe the wrong tree. Make the observed commit and the mismatch the substance of your result, and stop.\\n` +")
        a("    `Name the commit you actually examined in your result.\\n\\n`")
        a("}")
        a("")
    if measure_nodes:
        a("// Injected ahead of every measuring node's own prompt. A measurement")
        a("// taken in the shared tree is a function of what every sibling is")
        a("// doing, and the failure is silent: every node exits 0 with a")
        a("// confident number. The snapshot also fixes the base by construction —")
        a("// it is cut from the campaign's own HEAD, not a runtime default.")
        a("function measurePreamble() {")
        a("  return `MEASUREMENT ISOLATION — do this first, exactly.\\n` +")
        a("    `This node runs beside siblings sharing one working tree; measure a frozen snapshot, never the shared tree.\\n` +")
        a("    `1. Run: SNAP=\"$(mktemp -d)\" && git archive HEAD | tar -x -C \"$SNAP\"\\n` +")
        a("    `2. Do ALL work inside $SNAP. Never write to the repository checkout.\\n` +")
        a("    `3. Run: git rev-parse HEAD — expect ${BASE.sha}; anything else means the tree moved under the campaign: report the mismatch instead of a number.\\n` +")
        a("    `If the snapshot cannot be created, say so and stop rather than measuring the shared tree. Name the commit you measured in your result.\\n\\n`")
        a("}")
        a("")
    if tree_guard:
        a("// --- tree integrity: the shared tree must not move under the campaign ---")
        a("// A node that dirties the shared tree invalidates every verdict minted")
        a("// before it: the gate that already passed was judging a tree that no")
        a("// longer exists. Non-mutating nodes run in the shared cwd, mutators run")
        a("// in worktrees and measurers in snapshots, so the shared tree's state")
        a("// must be IDENTICAL at every checkpoint — any drift is an undeclared")
        a("// mutation, and the campaign halts on it rather than advancing.")
        a("// treeGuard: false in the IR turns this off, on the record.")
        a("//")
        a("// The plugin's own writes are not the campaign's and are left out of")
        a("// the comparison: the Stop hook appends its Evidence row to the state")
        a("// file whenever the orchestrator ends a turn, and state, attestations,")
        a("// run records, recompiled scripts and worktrees live in the plugin's")
        a("// own directories under .claude/. Counted, one turn end during a live")
        a("// run discarded every verdict it had minted. Only those directories:")
        a("// tracked project config under .claude/ (settings, agents) still counts.")
        a("// Paths carry the project's prefix inside the repository, because")
        a("// `git status --porcelain` names paths from the repository root.")
        pre = project_prefix or ""
        state_files = [pre + f for f in ("WORK.md", "LOOP.md")]
        if graph_file and pre + graph_file not in state_files:
            state_files.append(pre + graph_file)
        a("const TREE_IGNORE = " + json.dumps({
            "files": state_files,
            "prefixes": [pre + d for d in (".claude/fluxpoint/", ".claude/workflows/",
                                            ".claude/worktrees/")],
            # A wholly untracked .claude/ collapses to one porcelain line.
            "exact": ["?? " + pre + ".claude/"]}))
        a("// git quotes a path with special or non-ASCII bytes and octal-escapes")
        a("// the bytes (core.quotePath); undo that before comparing names.")
        a("function gitUnquote(p) {")
        a("  if (!(p.length >= 2 && p[0] === '\"' && p[p.length - 1] === '\"')) return p")
        a("  const s = p.slice(1, -1)")
        a("  const esc = { n: 10, t: 9, r: 13, a: 7, b: 8, f: 12, v: 11, '\"': 34, '\\\\': 92 }")
        a("  let out = ''")
        a("  for (let i = 0; i < s.length; i++) {")
        a("    // Whole code points: a lone surrogate half makes encodeURIComponent throw.")
        a("    const c = String.fromCodePoint(s.codePointAt(i))")
        a("    if (c !== '\\\\' || i + 1 >= s.length) { try { out += encodeURIComponent(c) } catch (e) { return p } i += c.length - 1; continue }")
        a("    const oct = /^[0-7]{3}/.exec(s.slice(i + 1, i + 4))")
        a("    const byte = oct ? parseInt(oct[0], 8) : (esc[s[i + 1]] !== undefined ? esc[s[i + 1]] : s.charCodeAt(i + 1))")
        a("    out += '%' + (byte < 16 ? '0' : '') + byte.toString(16)")
        a("    i += oct ? 3 : 1")
        a("  }")
        a("  try { return decodeURIComponent(out) } catch (e) { return p }")
        a("}")
        a("function treeIgnored(line) {")
        a("  if (TREE_IGNORE.exact.includes(line)) return true")
        a("  const rest = line.replace(/^\\S{1,2}\\s+/, '')")
        a("  const paths = rest.split(' -> ').map(p => gitUnquote(p.trim()))")
        a("  return paths.every(p => TREE_IGNORE.files.includes(p) ||")
        a("    TREE_IGNORE.prefixes.some(x => p === x.replace(/\\/$/, '') || p.startsWith(x)))")
        a("}")
        a("const treeNorm = s => String(s || '').split('\\n').map(x => x.trim()).filter(Boolean)")
        a("  .filter(x => !treeIgnored(x)).sort().join('\\n')")
        a("const TREE = { baseline: null, launch: null, checks: [] }")
        a("async function treeCheck(point, phase) {")
        a("  // agent(), not spawn(): a guard rail must not spend the node budget")
        a("  // and must still run after the ceiling is reached.")
        a("  const r = await agent('Run exactly two commands in the repository at "
          "the current working directory and return their output verbatim in the "
          "schema, nothing else: `git rev-parse HEAD` as head, and `git status "
          "--porcelain` as porcelain (empty string when clean). Do not fix, clean, "
          "or explain anything.', "
          "{ label: `tree-check:${point}`, phase, schema: C['TreeCheckV1'], effort: 'low' })")
        a("  if (!r) {")
        a("    note('tree-check', 'DEAD', `${point}: sentinel died — tree state unknown`)")
        a("    INCOMPLETE = true")
        a("    return true")
        a("  }")
        a("  const rec = { point, head: String(r.head || '').trim(), porcelain: treeNorm(r.porcelain) }")
        a("  TREE.checks.push(rec)")
        a("  if (!TREE.baseline) { TREE.baseline = rec; return true }")
        a("  if (rec.head === TREE.baseline.head && rec.porcelain === TREE.baseline.porcelain) return true")
        a("  note('tree-check', 'TREE-MOVED', `${point}: HEAD ${TREE.baseline.head} -> ${rec.head}${rec.porcelain ? `; dirt: ${rec.porcelain}` : ''}`)")
        a("  log(`TREE-MOVED at ${point}: the shared tree changed under the campaign — verdicts minted before this describe a tree that no longer exists. ${rec.porcelain || '(clean tree, but HEAD moved)'}`)")
        a("  return false")
        a("}")
        a("")
    first_phase = nodes[0].get("phase", "Run") if nodes else "Run"
    last_phase = nodes[-1].get("phase", "Run") if nodes else "Run"
    if tree_guard:
        a(f"await treeCheck('campaign-start', {js_str(first_phase)})")
        a("// The sentinel above is an agent() call, so a resume replays its FIRST")
        a("// reading instead of taking one. The launcher's reading (args._base,")
        a("// taken fresh at every launch) is compared against it here: a resume on")
        a("// a tree that moved since the run it replays would otherwise hand back")
        a("// cached verdicts about a tree that no longer exists.")
        a("if (A._base && A._base.sha) {")
        a("  TREE.launch = { head: String(A._base.sha).trim(),")
        a("    porcelain: typeof A._base.porcelain === 'string' ? treeNorm(A._base.porcelain) : null }")
        a("  const b = TREE.baseline")
        a("  if (b && (TREE.launch.head !== b.head ||")
        a("      (TREE.launch.porcelain !== null && TREE.launch.porcelain !== b.porcelain))) {")
        a("    note('tree-check', 'TREE-MOVED', `launch: HEAD ${b.head} -> ${TREE.launch.head}` +")
        a("      (TREE.launch.porcelain !== null && TREE.launch.porcelain !== b.porcelain")
        a("        ? `; dirt: ${TREE.launch.porcelain || '(none)'} vs ${b.porcelain || '(none)'}` : ''))")
        a("    log(`TREE-MOVED at launch: this launch's tree is not the one the run's first reading described — a resume would replay verdicts about a tree that no longer exists. Launch fresh (no resumeFromRunId), or restore the tree the run started on.`)")
        a("    return summary('TREE-MOVED')")
        a("  }")
        a("} else {")
        a("  log('tree guard: no launch reading in args._base — a resume cannot tell whether the tree moved since the run it replays; /fluxpoint:graph-run passes one')")
        a("}")
        a("")
    for n in nodes:
        if tree_guard and (prove_gate(n) or n.get("independent")):
            a("// The gate is about to mint a verdict about the tree; assert it is")
            a("// still the tree every earlier verdict described.")
            a(f"if (!await treeCheck({js_str('before ' + n['id'])}, "
              f"{js_str(n.get('phase', 'Run'))})) return summary('TREE-MOVED')")
        a(emit_node(n, ir, specification=bool(specification)))
        a("")
    if tree_guard:
        a(f"if (!await treeCheck('campaign-end', {js_str(last_phase)})) return summary('TREE-MOVED')")
    a("return summary('COMPLETE')")
    return "\n".join(L) + "\n"


def emit_parked(n, specification=False):
    """A node no agent can run: released from a file, or reported blocked.

    Emits no spawn at all. The campaign continues past it rather than
    halting, because the branches that do not depend on this node are still
    workable — and it is marked INCOMPLETE, so a run carrying a blocked node
    can never be read as a finished one.
    """
    nid = n["id"]
    var = "n_" + nid.replace("-", "_")
    rel = n.get("release") or {}
    actor = n.get("actor")
    instructions = str(rel.get("instructions", ""))
    # Both interpolated strings are scanned, so a {{prev.<field>}} the
    # validator blessed in the release instructions binds here too rather
    # than emitting against a variable that does not exist.
    mapping = subst_mapping(n, texts=[str(n["prompt"]), instructions])
    L = []
    a = L.append
    a(f"const rel_{var} = RELEASES[{js_str(nid)}] || null")
    a(f"let {var} = null")
    a(f"if (rel_{var}) {{")
    a(f"  {var} = rel_{var}.proof")
    a(f"  note({js_str(nid)}, 'RELEASED', `released by ${{rel_{var}.by || 'operator'}}`)")
    # A park is measured in hours or days; no prompt-cache TTL survives it.
    # Said here so the resume is understood as a cold start rather than hoped
    # to be warm — the estimate priced it cold for the same reason.
    a(f"  log(`{nid}: released — the {actor} step is done; resuming past a park is "
      f"a cold start for the prompt cache, and was priced as one`)")
    a("} else {")
    # Handing someone a block with no recommendation is a punt. The advisor
    # runs before the block is reported, is contracted to DecisionV1 so a
    # bare "ask the operator" cannot satisfy it, and is asked first whether
    # the block is real — most steps that feel human-only are not.
    a(f"  const advice_{var} = affordable({js_str('advice for ' + nid)})")
    a(f"    ? await spawn(")
    if specification:
        a("        specificationPreamble() +")
    a(f"        `A campaign step cannot be run by an agent and is about to be "
      f"handed to a person. Do not simply agree.\\n\\n` +")
    a(f"        `The step: ` + {js_template(str(n['prompt']), mapping)} + `\\n` +")
    a(f"        `What the human is being asked to do: ` + {js_template(instructions, mapping)} + `\\n` +")
    a(f"        `The stated reason no agent can do it: ` + "
      f"{js_str(str(rel.get('whyNotAgent', 'unstated')))} + `\\n\\n` +")
    a("        `FIRST, challenge that reason. Could this actually be done "
      "without a person — a CLI, an API, a headless browser, a read-only "
      "query, a generated file the human only has to sign? If yes, your "
      "recommendation is that concrete agent-executable path, and say what "
      "tooling it needs. Only genuine blockers survive: key material an "
      "agent must not hold, legal authority, physical possession, or "
      "another party's own action.` +")
    a("        `\\n\\nTHEN recommend the single best course of action for the "
      "human, with the alternatives you rejected and the strongest objection "
      "to each — including to the one you are recommending. Ground it in "
      "this repo: read what you need to. A recommendation with no reasoning "
      "is worth less than no recommendation, because it will be followed.`,")
    a(f"        {{ label: {js_str(nid + ':advice')}, phase: {js_str(n.get('phase', 'Run'))}, "
      f"schema: C.DecisionV1, effort: 'medium' }}")
    a("      )")
    a("    : null")
    a(f"  if (advice_{var}) {{")
    a(f"    RECOMMENDATIONS[{js_str(nid)}] = advice_{var}")
    a(f"    log(`{nid}: recommended — ${{advice_{var}.chosen}}`)")
    a("  } else {")
    a(f"    log(`{nid}: BLOCKED with no recommendation — the advisory call was "
      f"declined by the budget floor. This is a worse hand-off, not a cheaper one.`)")
    a("  }")
    a(f"  note({js_str(nid)}, 'BLOCKED', {js_template(instructions, mapping)}"
      f" + (advice_{var} ? ` | RECOMMENDED: ${{advice_{var}.chosen}} — "
      f"${{advice_{var}.rationale}}` : ''))")
    a(f"  log(`BLOCKED at {nid} ({actor}): ` + {js_template(instructions, mapping)})")
    a(f"  BLOCKED.add({js_str(nid)})")
    a("  INCOMPLETE = true")
    w = n.get("wake")
    if w:
        fields = [f"node: {js_str(nid)}", f"check: {js_str(w['check'])}",
                  f"everyMinutes: {int(w['everyMinutes'])}"]
        if w.get("deadline"):
            fields.append(f"deadline: {js_str(str(w['deadline']))}")
        a(f"  WAITS.push({{ {', '.join(fields)} }})")
    a("}")
    return "\n".join(L)


def emit_reduce(n, ir):
    """Deterministic reduction between agents: dedupe, rank, cut — in code.

    No spawn, no tokens, no model. The ops run in a fixed order (dedupe,
    then sort, then topK) so the same IR always cuts the same items, and a
    topK cut names how many it dropped — a reducer that silently truncates
    reads as coverage it did not deliver. Field access is bracket-notation
    through js_str: a field name can never become code.
    """
    nid = n["id"]
    var = "n_" + nid.replace("-", "_")
    red = n["reduce"]
    src_id = red["from"]
    nodes_by_id = {x.get("id"): x for x in ir["nodes"]}
    shape = result_shape(nodes_by_id[src_id])
    over = red.get("over")
    L = []
    a = L.append
    a(f"const src_{var} = RESULTS[{js_str(src_id)}]")
    if shape == "object":
        a(f"let {var} = ((src_{var} && src_{var}[{js_str(over)}]) || []).slice()")
    elif shape == "objects":
        a(f"let {var} = (src_{var} || []).filter(Boolean)"
          f".flatMap(r => r[{js_str(over)}] || [])")
    else:
        a(f"let {var} = (src_{var} || []).slice()")
    a(f"const before_{var} = {var}.length")
    if red.get("dedupeBy"):
        keyexpr = " + '|' + ".join(
            f"String(it[{js_str(k)}])" for k in red["dedupeBy"])
        a("{")
        a("  const seen = new Set()")
        a(f"  {var} = {var}.filter(it => {{ const k = {keyexpr}; "
          f"if (seen.has(k)) return false; seen.add(k); return true }})")
        a("}")
    if red.get("sortBy"):
        sb = js_str(red["sortBy"])
        sign = "-1" if red.get("order") == "desc" else "1"
        a(f"{var}.sort((x, y) => {{ const xv = x[{sb}], yv = y[{sb}]; "
          f"return (xv < yv ? -1 : xv > yv ? 1 : 0) * {sign} }})")
    if red.get("topK") is not None:
        k = int(red["topK"])
        a(f"const cut_{var} = Math.max(0, {var}.length - {k})")
        a(f"if (cut_{var}) log(`{nid}: topK dropped ${{cut_{var}}} item(s) "
          f"beyond the top {k} — no silent caps`)")
        a(f"{var} = {var}.slice(0, {k})")
    a(f"note({js_str(nid)}, 'OK', `reduced ${{before_{var}}} -> "
      f"${{{var}.length}} item(s)`, {{ before: before_{var}, after: {var}.length }})")
    a(f"log(`{nid}: reduced ${{before_{var}}} -> ${{{var}.length}} item(s) in "
      f"code — deterministic, zero spawns`)")
    return "\n".join(L)


def emit_node(n, ir, specification=False):
    nid = n["id"]
    var = "n_" + nid.replace("-", "_")
    phase = n.get("phase", "Run")
    tier = str(n.get("verify", "schema-only"))
    m = TIER.match(tier)
    panel = int(m.group(2) or m.group(3) or 0) if m else 0
    is_panel = m and m.group(3)
    over = n.get("verifyOver")
    mapping = subst_mapping(n, var)
    # Isolated nodes carry their preamble as a runtime concatenation, so the
    # campaign base (known only at launch, via args._base) reaches the prompt
    # without the compiler pretending to know a sha it cannot.
    pre = ""
    if n.get("isolation") == "measure":
        pre = "measurePreamble() + "
    elif n.get("isolation") in (True, "worktree") or n.get("mutates"):
        pre = "basePreamble() + "
    if prove_gate(n):
        pre += f"provePreamble({js_str(prove_gate(n))}) + "
    if specification:
        pre += "specificationPreamble() + "
    prompt = (pre + js_template(n["prompt"], mapping)) if not is_reduce(n) else None
    L = []
    a = L.append
    kind = "reduce" if is_reduce(n) else tier
    a(f"// ===== node {nid} ({kind}{', mutates' if n.get('mutates') else ''}"
      f"{', independent' if n.get('independent') else ''}) =====")
    a(f"phase({js_str(phase)})")

    # Blocked is inherited down the `after` chain — and down a reduce's
    # `from`, which is the same edge wearing different clothes. Handing a
    # dependent the literal null of a node nobody ran would report a
    # failure where there is only a wait, and the campaign would argue
    # with itself about why.
    has_parked = any(o.get("actor", "agent") != "agent" for o in ir["nodes"])
    dep = n.get("after") or (n.get("reduce") or {}).get("from")
    guard = has_parked and dep and n.get("actor", "agent") == "agent"
    if guard:
        a(f"if (BLOCKED.has({js_str(dep)})) {{")
        a(f"  note({js_str(nid)}, 'BLOCKED', 'blocked on {dep}')")
        a(f"  log(`BLOCKED at {nid}: inherited from {dep}`)")
        a(f"  BLOCKED.add({js_str(nid)})")
        a("  INCOMPLETE = true")
        a(f"  RESULTS[{js_str(nid)}] = null")
        a("} else {")

    if n.get("actor", "agent") != "agent":
        a(emit_parked(n, specification))
    elif is_reduce(n):
        a(emit_reduce(n, ir))
    elif n.get("repeat"):
        a(emit_repeat(n, ir, prompt, phase, panel, over))
    elif n.get("foreach"):
        lst = list_var(n["foreach"])
        label = f"`{nid}:${{item.key || i}}`"
        # Fan-out defaults to drop+log — partial coverage, said out loud —
        # because that was always its behavior. Declaring halt makes a dead
        # worker end the campaign; before v1.4 the field was accepted on a
        # fan-out and silently did nothing, which read as a policy and
        # enforced none. A spawn the node ceiling declined is NOT a death:
        # it files SKIPPED, and under halt it ends the run BUDGET-EXHAUSTED
        # rather than pinning exhaustion on workers that never ran.
        on_red = n.get("onRed", "drop+log")
        has_ceiling = (ir.get("budget") or {}).get("maxNodes") is not None
        a(f"let {var} = []")
        a(f"let dead_{var} = 0")
        if has_ceiling:
            a(f"let skipped_{var} = 0")
        a(f"if (!affordable({js_str('node ' + nid)})) {{")
        a(f"  note({js_str(nid)}, 'SKIPPED', 'budget floor reached before fan-out')")
        a("} else {")
        if panel and over:
            if has_ceiling:
                dead_branch = (
                    f"      if (!prev) {{\n"
                    f"        if (DECLINED_LABELS.has(`{nid}:${{item.key || i}}`)) {{\n"
                    f"          skipped_{var}++; note({js_str(nid)}, 'SKIPPED', "
                    f"`${{item.key || i}} declined by the node ceiling`)\n"
                    f"        }} else {{\n"
                    f"          dead_{var}++; note({js_str(nid)}, 'DEAD', `${{item.key || i}} produced nothing`)\n"
                    f"          log(`node {nid} died for ${{item.key || i}} — dropped`)\n"
                    f"        }}\n"
                    f"        return []\n"
                    f"      }}")
            else:
                dead_branch = (
                    f"      if (!prev) {{ dead_{var}++; note({js_str(nid)}, 'DEAD', "
                    f"`${{item.key || i}} produced nothing`); "
                    f"log(`node {nid} died for ${{item.key || i}} — dropped`); return [] }}")
            a(f"  {var} = (await pipeline(")
            a(f"    {lst},")
            a(f"    (item, _o, i) => spawn({prompt}, {opts(n, ir, phase, label)}),")
            a("    async (prev, item, i) => {")
            a(dead_branch)
            a(f"      const produced = prev[{js_str(over)}] || []")
            a(f"      note({js_str(nid)}, 'OK', `${{item.key || i}}: ${{produced.length}} item(s)`, "
              f"{{ produced: produced.length }})")
            need = math.ceil(panel / 2)
            a(f"      const judged = await verifyItems(prev, {js_str(over)}, "
              f"`{nid}:${{item.key || i}}`, {js_str(phase)}, {panel}, {need})")
            a("      return judged.map(v => ({ ...v, source: item.key || String(i) }))")
            a("    }")
            a("  )).filter(Boolean).flat()")
            a(f"  log(`{nid}: ${{{var}.length}} item(s) survived {tier}`)")
        else:
            if has_ceiling:
                a(f"  const d0_{var} = DECLINED")
            a(f"  {var} = (await parallel({lst}.map((item, i) => () =>")
            a(f"    spawn({prompt}, {opts(n, ir, phase, label)})")
            a("  ))).filter(Boolean)")
            if has_ceiling:
                a(f"  skipped_{var} = DECLINED - d0_{var}")
            a(f"  dead_{var} = {lst}.length - {var}.length"
              + (f" - skipped_{var}" if has_ceiling else ""))
            a(f"  note({js_str(nid)}, {var}.length ? 'OK' : (dead_{var} ? 'DEAD' : 'SKIPPED'), "
              f"`${{{var}.length}}/${{{lst}.length}} returned`, "
              f"{{ returned: {var}.length, of: {lst}.length, died: dead_{var}"
              + (f", declined: skipped_{var}" if has_ceiling else "") + " })")
            if on_red != "halt":
                a(f"  if (dead_{var}) log(`{nid}: "
                  f"${{dead_{var}}} node(s) died — see provenance`)")
        if has_ceiling:
            a(f"  if (skipped_{var}) note({js_str(nid)}, 'SKIPPED', "
              f"`${{skipped_{var}}} spawn(s) declined by the node ceiling — "
              f"coverage incomplete, not worker death`)")
        if on_red == "halt":
            a(f"  if (dead_{var}) {{")
            a(f"    log(`node {nid}: ${{dead_{var}}} worker(s) died — halting per onRed=halt`)")
            a(f"    RESULTS[{js_str(nid)}] = {var}")
            a("    return summary('NODE-DEAD')")
            a("  }")
            if has_ceiling:
                a(f"  if (skipped_{var}) {{")
                a(f"    log(`node {nid}: ${{skipped_{var}}} spawn(s) declined by the node "
                  f"ceiling — halting as exhausted, not as dead`)")
                a(f"    RESULTS[{js_str(nid)}] = {var}")
                a("    return summary('BUDGET-EXHAUSTED')")
                a("  }")
        a("}")
        if red_team_checked(n):
            a(emit_red_team_check(n, var, many=True))
        if n.get("haltWhen"):
            # One item tripping the condition halts the campaign: a fan-out
            # gate that only fired when every branch failed would not be a gate.
            a(emit_halt_any(n, var))
    else:
        label = f"{js_str(nid)}"
        verified = bool(panel and over)
        raw = f"{var}_raw" if verified else var
        on_red = n.get("onRed", "halt")
        has_ceiling = (ir.get("budget") or {}).get("maxNodes") is not None
        if has_ceiling:
            a(f"const d0_{var} = DECLINED")
        # The floor verdict is taken once: calling affordable() twice logged
        # the decline twice, and a floor-declined node must not fall through
        # into the death branch below — declined and dead are different
        # sentences.
        a(f"let ok_{var} = true")
        if n.get("irreversible"):
            key = f"k_{var}"
            a(f"const {key} = ledgerKey({js_str(nid)}, {prompt})")
            # A replayed node must not also be filed 'OK'. Reporting a
            # ceremony that did not happen the same way as one that did is
            # the whole failure this is here to prevent.
            a(f"const replayed_{var} = {key} in LEDGER")
            a(f"let {raw}")
            a(f"if (replayed_{var}) {{")
            a(f"  log(`REPLAYED-FROM-LEDGER at {nid}: already recorded against this "
              f"campaign — the effect is not performed again`)")
            a(f"  note({js_str(nid)}, 'REPLAYED', 'once-only ledger hit; effect not repeated')")
            a(f"  {raw} = LEDGER[{key}].result")
            a("} else {")
            a(f"  if (!confirmed({js_str(nid)})) {{")
            a(f"    note({js_str(nid)}, 'REFUSED', 'irreversible node not named in confirm')")
            a(f"    log(`REFUSED at {nid}: irreversible, and confirm does not name it. "
              f"Re-launch with confirm listing {nid} once a human has authorized "
              f"this specific effect.`)")
            a("    return summary('CONFIRM-REQUIRED')")
            a("  }")
            a(f"  ok_{var} = affordable({js_str('node ' + nid)})")
            a(f"  if (!ok_{var}) {{")
            a(f"    note({js_str(nid)}, 'SKIPPED', 'budget floor reached')")
            if on_red == "halt":
                a("    return summary('BUDGET-EXHAUSTED')")
            a("  }")
            a(f"  {raw} = ok_{var} ? await spawn({prompt}, {opts(n, ir, phase, label)}) : null")
            # Recorded the moment it returns, so the row exists even if a later
            # node halts the campaign.
            a(f"  if ({raw}) LEDGER_WRITES.push({{ key: {key}, node: {js_str(nid)}, "
              f"campaign, result: {raw} }})")
            a("}")
        else:
            a(f"ok_{var} = affordable({js_str('node ' + nid)})")
            a(f"if (!ok_{var}) {{")
            a(f"  note({js_str(nid)}, 'SKIPPED', 'budget floor reached')")
            if on_red == "halt":
                a("  return summary('BUDGET-EXHAUSTED')")
            a("}")
            a(f"const {raw} = ok_{var} ? await spawn({prompt}, {opts(n, ir, phase, label)}) : null")
        # A node restored from the ledger already carries its provenance.
        if n.get("irreversible"):
            a(f"if (!replayed_{var}) {{")
        # A null from a ceiling-declined spawn is exhaustion, not death:
        # it files SKIPPED (record-run doctrine: budget-declined work is
        # neither success nor failure) and a halt policy ends the run
        # BUDGET-EXHAUSTED, never NODE-DEAD. A floor-declined node was
        # already filed SKIPPED above and must not be re-filed dead.
        a(f"if (!{raw} && !ok_{var}) {{")
        a("  // declined by the token floor above — nothing died")
        if has_ceiling:
            a(f"}} else if (!{raw} && DECLINED > d0_{var}) {{")
            a(f"  note({js_str(nid)}, 'SKIPPED', 'spawn declined by the node ceiling')")
            if on_red == "halt":
                a(f"  log(`node {nid} not run — node ceiling reached; halting as exhausted, not as dead`)")
                a("  return summary('BUDGET-EXHAUSTED')")
            a(f"}} else if (!{raw}) {{")
        else:
            a(f"}} else if (!{raw}) {{")
        a(f"  note({js_str(nid)}, 'DEAD', 'node returned nothing')")
        if on_red == "halt":
            a(f"  log(`node {nid} died — halting; an unverified gate never passes by default`)")
            a("  return summary('NODE-DEAD')")
        else:
            a(f"  log(`node {nid} died — continuing per onRed={on_red}`)")
        a("} else {")
        a(f"  note({js_str(nid)}, 'OK', '')")
        a("}")
        if n.get("irreversible"):
            a("}")
        if red_team_checked(n):
            a(emit_red_team_check(n, raw))
        if n.get("haltWhen"):
            # Halt on the raw contract: the gate reads the node's own fields.
            a(emit_halt(n, raw))
        if verified:
            # A declared tier always runs, fan-out or not.
            need = math.ceil(panel / 2)
            sink = (", " + lesson_sink(n, need)
                    if (n.get("memory") or {}).get("emit") else "")
            a(f"const {var} = await verifyItems({raw}, {js_str(over)}, "
              f"{js_str(nid)}, {js_str(phase)}, {panel}, {need}{sink})")
            a(f"log(`{nid}: ${{{var}.length}} item(s) survived {tier}`)")

    pg = prove_gate(n)
    if pg:
        a(f"const cite_{var} = citation({js_str(nid)}, {js_str(pg)}, {var})")
        a(f"if (cite_{var}) {{")
        a(f"  log(`UNPROVEN ${{cite_{var}}}`)")
        a(f"  note({js_str(nid)}, 'UNPROVEN', cite_{var})")
        a("  INCOMPLETE = true")
        a("}")
    a(f"RESULTS[{js_str(nid)}] = {var}")
    if n.get("decides"):
        a(f"DECISIONS[{js_str(n['decides'])}] = {var}")
        a(f"if ({var} && {var}.overturned_prior) log("
          f"`DECISION {n['decides']}: overturned the prior — ${{{var}.chosen}}`)")
    if guard:
        a("}")
    return "\n".join(L)


def emit_repeat(n, ir, prompt, phase, panel, over):
    """Loop-until-dry discovery: re-run the finder until K consecutive rounds
    surface nothing new, bounded by maxRounds and the budget floor.

    The dedup set holds everything SEEN, not everything confirmed — dedup
    against survivors instead and every judge-rejected item reappears next
    round, so the loop never converges.
    """
    nid = n["id"]
    var = "n_" + nid.replace("-", "_")
    rep = n["repeat"]
    keys = rep["dedupeBy"]
    dry_target = int(rep["untilDryRounds"])
    max_rounds = int(rep["maxRounds"])
    need = math.ceil(panel / 2) if panel else 0
    lst = list_var(n["foreach"]) if n.get("foreach") else None
    label = f"`{nid}:${{item.key || i}}`" if lst else js_str(nid)

    L = []
    a = L.append
    mem = n.get("memory") or {}
    a(f"const {var} = []")
    a(f"const seen_{var} = new Set()")
    if mem.get("seed"):
        # Prior keys land in the advisory list only. seen_ stays empty on
        # purpose: it decides what this run discards, and a run must never
        # discard a finding because an earlier run knew about it.
        a(f"const seenList_{var} = "
          f"((_seedSrc[{js_str(mem['seed'])}] || {{}}).keys || []).slice()")
    else:
        a(f"const seenList_{var} = []")
    a(f"let dry_{var} = 0, round_{var} = 0")
    keyexpr = " + '|' + ".join(f"String(it[{js_str(k)}])" for k in keys)
    a(f"const key_{var} = it => {keyexpr}")
    a(f"while (dry_{var} < {dry_target} && round_{var} < {max_rounds}) {{")
    a(f"  round_{var}++")
    a(f"  if (!affordable(`{nid} round ${{round_{var}}}`)) {{")
    a(f"    note({js_str(nid)}, 'SKIPPED', `stopped at round ${{round_{var}}} on budget floor; "
      f"discovery INCOMPLETE`)")
    a("    break")
    a("  }")
    # Round 1 of the fan-out, unverified — dedup happens before verification
    # so the panel never re-judges an item a previous round already saw.
    # The unfiltered array is kept so per-worker attribution survives a
    # death: filtering first shifted survivors into dead workers' slots.
    on_red = n.get("onRed", "drop+log")
    has_ceiling = (ir.get("budget") or {}).get("maxNodes") is not None
    if has_ceiling:
        a(f"  const d0r_{var} = DECLINED")
    if lst:
        a(f"  const rawAll_{var} = await parallel({lst}.map((item, i) => () =>")
        a(f"    spawn({prompt}, {opts(n, ir, phase, label)})")
        a("  ))")
        expected = f"{lst}.length"
    else:
        a(f"  const one_{var} = await spawn({prompt}, {opts(n, ir, phase, label)})")
        a(f"  const rawAll_{var} = [one_{var}]")
        expected = "1"
    a(f"  const raw_{var} = rawAll_{var}.filter(Boolean)")
    # A spawn the node ceiling declined is exhaustion, not death: every
    # later round would be declined too, so the sweep ends here — SKIPPED
    # under drop+log, BUDGET-EXHAUSTED under halt, never NODE-DEAD.
    if has_ceiling:
        a(f"  const declined_{var} = DECLINED - d0r_{var}")
        a(f"  if (declined_{var}) {{")
        a(f"    note({js_str(nid)}, 'SKIPPED', `round ${{round_{var}}}: "
          f"${{declined_{var}}} spawn(s) declined by the node ceiling — "
          f"discovery INCOMPLETE`, {{ round: round_{var}, declined: declined_{var} }})")
        if on_red == "halt":
            a(f"    log(`node {nid}: node ceiling reached in round ${{round_{var}}} — "
              f"halting as exhausted, not as dead`)")
            a(f"    RESULTS[{js_str(nid)}] = {var}")
            a("    return summary('BUDGET-EXHAUSTED')")
        else:
            a("    break")
        a("  }")
    # A round that lost a worker proves nothing about that worker's ground,
    # so it must never count toward the dry rule — count it and a finder
    # that keeps crashing ends the sweep looking converged, which is silent
    # incompleteness with a good alibi. onRed decides whether a death ends
    # the campaign or the round carries on with the workers that returned.
    a(f"  const deadRound_{var} = raw_{var}.length < {expected}")
    a(f"  if (deadRound_{var}) {{")
    a(f"    note({js_str(nid)}, 'DEAD', `round ${{round_{var}}}: only "
      f"${{raw_{var}.length}}/${{{expected}}} worker(s) returned`, "
      f"{{ round: round_{var}, returned: raw_{var}.length, of: {expected} }})")
    if on_red == "halt":
        a(f"    log(`node {nid}: worker died in round ${{round_{var}}} — "
          f"halting per onRed=halt`)")
        a(f"    RESULTS[{js_str(nid)}] = {var}")
        a("    return summary('NODE-DEAD')")
    else:
        a(f"    log(`{nid} round ${{round_{var}}}: "
          f"${{{expected} - raw_{var}.length}} worker(s) died — continuing "
          f"per onRed=drop+log; a dead round is never a dry round`)")
        a(f"    if (!raw_{var}.length) continue")
    a("  }")
    # Per-worker unique-new counts, attributed first-seen in worker order
    # and KEYED by the worker's list identity — a positional array over the
    # survivors would shift counts into dead workers' slots. A dead worker
    # reads null, distinct from a live worker that found nothing (0):
    # fan-out efficiency (which parallel worker still surfaces new ground)
    # is unmeasurable once the round's results are merged.
    wk = (f"String(({lst}[wi] || {{}}).key || wi)" if lst else "String(wi)")
    a(f"  let found_{var} = 0")
    a(f"  const fresh_{var} = []")
    a(f"  const perWorker_{var} = {{}}")
    a(f"  rawAll_{var}.forEach((r, wi) => {{")
    a(f"    const wk = {wk}")
    a(f"    if (!r) {{ perWorker_{var}[wk] = null; return }}")
    a("    let c = 0")
    a(f"    for (const it of (r[{js_str(over)}] || [])) {{")
    a(f"      found_{var}++")
    a(f"      if (!seen_{var}.has(key_{var}(it))) {{")
    a(f"        seen_{var}.add(key_{var}(it))")
    a(f"        seenList_{var}.push(key_{var}(it))")
    a(f"        fresh_{var}.push(it)")
    a("        c++")
    a("      }")
    a("    }")
    a(f"    perWorker_{var}[wk] = c")
    a("  })")
    a(f"  log(`{nid} round ${{round_{var}}}: ${{found_{var}}} found, "
      f"${{fresh_{var}.length}} new`)")
    a(f"  if (!fresh_{var}.length) {{")
    a(f"    if (deadRound_{var}) {{")
    a(f"      note({js_str(nid)}, 'OK', `round ${{round_{var}}}: nothing new "
      f"from survivors — NOT counted dry; a round with dead workers proves "
      f"nothing about their ground`, {{ round: round_{var}, "
      f"found: found_{var}, fresh: 0, kept: 0, perWorker: perWorker_{var} }})")
    a("    } else {")
    a(f"      dry_{var}++")
    a(f"      note({js_str(nid)}, 'OK', `round ${{round_{var}}} dry "
      f"(${{dry_{var}}}/{dry_target})`, {{ round: round_{var}, "
      f"found: found_{var}, fresh: 0, kept: 0, perWorker: perWorker_{var} }})")
    a("    }")
    a("    continue")
    a("  }")
    a(f"  dry_{var} = 0")
    if panel and over:
        # Priors reach the refuters, not the finder: the finder's job is to
        # look everywhere, the panel's job is to not re-derive an argument a
        # recorded objection already makes.
        pri_arg = (f", PRIORS[{js_str(mem['seed'])}]"
                   if mem.get("priors") and mem.get("seed") else "")
        a(f"  const judged_{var} = await parallel(fresh_{var}.map((it, i) => () =>")
        a(f"    refute(JSON.stringify(it), `{nid}:r${{round_{var}}}:${{i}}`, "
          f"{js_str(phase)}, {panel}{pri_arg}).then(v => ({{ ...it, ...v }}))")
        a("  ))")
        if mem.get("emit"):
            a(f"  judged_{var}.filter(Boolean).forEach({lesson_sink(n, need)})")
        a(f"  const kept_{var} = judged_{var}.filter(Boolean)"
          f".filter(v => v.kills < {need})")
    else:
        a(f"  const kept_{var} = fresh_{var}")
    a(f"  {var}.push(...kept_{var})")
    a(f"  note({js_str(nid)}, 'OK', `round ${{round_{var}}}: ${{kept_{var}.length}} kept "
      f"of ${{fresh_{var}.length}} new`, {{ round: round_{var}, found: found_{var}, "
      f"fresh: fresh_{var}.length, kept: kept_{var}.length, "
      f"perWorker: perWorker_{var} }})")
    a("}")
    a(f"if (round_{var} >= {max_rounds} && dry_{var} < {dry_target}) {{")
    a(f"  log(`{nid}: hit maxRounds {max_rounds} before the dry rule fired — "
      f"new items still arriving, or rounds kept losing workers; "
      f"discovery INCOMPLETE, not exhausted`)")
    a(f"  note({js_str(nid)}, 'INCOMPLETE', `ended on the {max_rounds}-round ceiling "
      f"before the dry rule fired; the sweep is not exhaustive`)")
    a("  INCOMPLETE = true")
    a("}")
    a(f"log(`{nid}: ${{{var}.length}} item(s) kept across ${{round_{var}}} round(s)`)")
    return "\n".join(L)


# ---------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("graph", nargs="?", default="WORK.md", help="path to WORK.md (or any file with a graph-ir block)")
    ap.add_argument("-o", "--out", help="path to write the compiled .graph.js")
    ap.add_argument("--check", action="store_true", help="validate only")
    ap.add_argument("--contracts", help="contracts directory")
    ap.add_argument("--runs-dir", default=RUNS_DIR,
                    help="recorded-runs directory imports resolve against")
    ap.add_argument("--decisions", default=os.path.join(".claude", "fluxpoint", "decisions.jsonl"),
                    help="decision.py's store, which imports also resolve against")
    ap.add_argument("--gates-root", default=".",
                    help=f"directory holding {GATES}, which prove: tiers resolve against")
    args = ap.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    shipped_dir = os.path.join(os.path.dirname(here), "contracts")
    sys.path.insert(0, here)
    import specification as spec_mod

    try:
        with open(args.graph, encoding="utf-8") as fh:
            text = fh.read()
        ir = extract_ir(text)
        # The graph file's own header says which packet and which contracts
        # it was written against. Ignoring it compiled a repo-local contract
        # set as a page of false findings, and bound one campaign's nodes to
        # whichever packet another campaign had locked last.
        header = spec_mod.headers(text)
        contracts, contracts_desc = resolve_contracts(
            shipped_dir, header.get("CONTRACTS"), args.contracts, args.gates_root)
    except (OSError, GraphError, ValueError) as e:
        print(f"graph-compile: {e}", file=sys.stderr)
        return 1

    if not contracts:
        print(f"graph-compile: no contracts found in {args.contracts or shipped_dir}",
              file=sys.stderr)
        return 1

    agents = load_agents(args.gates_root)
    gates = load_gates(args.gates_root)
    findings = validate(ir, contracts, gates, agents)
    specification = None
    spec_path = header.get("SPEC")
    if not findings and (spec_path or spec_mod.required(args.gates_root)
                         or any(n.get("mutates") or n.get("irreversible") for n in ir["nodes"])):
        try:
            packet_file, _ = spec_mod.packet_paths(args.gates_root, spec_path)
            packet, identity = spec_mod.load(args.gates_root, spec=spec_path)
            specification = {"packet": packet, "identity": identity,
                             "path": os.path.relpath(packet_file, args.gates_root).replace(os.sep, "/")}
            findings = validate(ir, contracts, gates, agents, specification)
        except (OSError, ValueError, TypeError) as e:
            print(f"graph-compile: spec required before implementation: {e}", file=sys.stderr)
            return 1

    if findings:
        print(f"graph-compile: IR rejected (contracts: {contracts_desc})\n", file=sys.stderr)
        for f in findings:
            print(f"  - {f}", file=sys.stderr)
        return 1

    planned = plan_node_count(ir)
    # Warnings inform, never block: these shapes are legal and occasionally
    # deliberate, but they will usually disappoint whoever reads the result.
    for w in warnings(ir, agents):
        print(f"graph-compile: warning — {w}", file=sys.stderr)

    # Resolved for --check too: a missing decision should fail preflight,
    # not the emission the preflight was supposed to clear.
    resolved, rfindings = resolve_imports(ir, contracts, args.runs_dir, args.decisions)
    if rfindings:
        print("graph-compile: imports unresolved\n", file=sys.stderr)
        for f in rfindings:
            print(f"  - {f}", file=sys.stderr)
        return 1

    est = estimate_tokens(ir, contracts, specification)
    cap = (ir.get("budget") or {}).get("maxEstimatedTokens")
    blind = unpriced_note(est)
    cost_line = (f"~{est['total']:,} estimated tokens"
                 + (f" of {cap:,} allowed" if cap is not None else "")
                 + f" ({est['cold']} cold prefill(s) of {est['calls']} call(s), "
                 f"prompt-cache TTL {est['ttl']}"
                 + (f"; {blind}" if blind else "") + ")")
    if args.check:
        imported = (f", {len(resolved)} imported decision(s) resolved"
                    if resolved else "")
        # Which inputs the verdict was reached against, said on the line
        # itself: findings against the wrong contract set used to read as
        # defects in the IR, with nothing naming the directory used.
        packet_note = (f"packet {specification['path']} "
                       f"{specification['identity']['sha256'][:12]}"
                       if specification else "no packet")
        print(f"graph-compile: IR valid — {len(ir['nodes'])} node(s), "
              f"{planned} planned agent call(s), budget.maxNodes="
              f"{(ir.get('budget') or {}).get('maxNodes')}, {cost_line}{imported}; "
              f"contracts: {contracts_desc}; {packet_note}")
        return 0

    try:
        graph_rel = os.path.relpath(os.path.abspath(args.graph),
                                    os.path.abspath(args.gates_root)).replace(os.sep, "/")
    except ValueError:  # another drive on Windows: not a file of this project
        graph_rel = "../"
    js = emit(ir, contracts, resolved, specification,
              graph_file=None if graph_rel.startswith("../") else graph_rel,
              project_prefix=git_prefix(args.gates_root))
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(js)
        print(f"graph-compile: wrote {args.out} — {len(ir['nodes'])} node(s), "
              f"{planned} planned agent call(s), {cost_line}")
    else:
        # Pin the newline on this path too. `--out` above opens with
        # newline="\n"; stdout is a TextIOWrapper with newline=None, which
        # translates every \n to os.linesep on write -- so on Windows the same
        # compiler emitted LF through one path and CRLF through the other.
        # PYTHONIOENCODING, which py.sh already exports, pins the ENCODING and
        # says nothing about line endings; they are separate translations.
        # The Workflow tool refuses a script carrying CR ("contains control
        # characters that would be hidden in the approval dialog"), naming
        # neither CRLF nor the compiler, so the failure lands far from here.
        try:
            sys.stdout.reconfigure(newline="\n")
            sys.stdout.write(js)
        except (AttributeError, ValueError):
            # Detached or replaced stdout: write the bytes we mean instead.
            sys.stdout.flush()
            sys.stdout.buffer.write(js.encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
