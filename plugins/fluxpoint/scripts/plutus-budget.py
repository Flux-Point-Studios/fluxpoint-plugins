#!/usr/bin/env python3
"""On-chain budget gate: a proved-correct validator that cannot be submitted.

Correctness and submittability are different properties and only one of them
has a prover. `aiken check` will happily certify a validator that is too
large to fit in a transaction, and the failure surfaces at submission time,
against real funds, long after the harness went green.

Script size is derivable from the build output, so it is gated here. The
blueprint `aiken build` writes to plutus.json carries `compiledCode` for
every validator; its byte length is the on-chain size, and a script larger
than the protocol's maximum transaction size cannot be submitted in any
transaction at all.

Two kinds of limit, kept apart on purpose:

  * Protocol limits are facts. Exceeding one is not a preference, it is an
    unsubmittable script, so it fails whether or not anything is configured.
    Defaults below are Cardano mainnet; pass --params with the output of
    `cardano-cli query protocol-parameters` to use the network you actually
    target rather than trusting a constant in this file.
  * A project budget is a preference — headroom you want to keep, because a
    validator at 99% of maxTxSize leaves no room for inputs, outputs, and
    the rest of the transaction. Set it in .fluxpoint-budget.json.

Execution units require a concrete evaluation. Mem and CPU are
properties of *evaluating a script against a specific transaction*, not of
the compiled artifact, so no honest number can be read out of plutus.json.
Pass --aiken-output with pinned Aiken unit-test JSON to check each measured
unit scenario; these are not a whole-transaction estimate. Alternatively, record
measured values from your own transaction-building tests under
`exUnits` in the budget file and they are checked against the protocol
maximum here; absent that, this reports the limits you need to measure
against rather than inventing a figure.

  plutus-budget.py --check     fail on any exceeded limit
  plutus-budget.py --report    print sizes and limits, always exit 0

Dormant by design: no plutus.json means no build to measure, exit 0.
"""
import argparse
import json
import os
import subprocess
import sys

BUDGET_FILE = ".fluxpoint-budget.json"

# Cardano mainnet, used only when --params is not supplied. These are the
# protocol's numbers, not this tool's opinion — but they are a SNAPSHOT, and the
# network changes them. Read from mainnet 2026-08-06. maxTxExMem stood at
# 14,000,000 for years and this file carried that number well past the rise to
# 16,500,000, which reports every measurement as a larger share of the budget
# than it is. That error is in the safe direction and still cost a real
# investigation, so: --params is the only way to be current, and the date above
# is what makes this constant auditable rather than merely plausible.
DEFAULT_LIMITS = {
    "maxTxSize": 16384,
    "maxTxExMem": 16_500_000,
    "maxTxExSteps": 10_000_000_000,
}


def load_limits(params_path):
    """Protocol limits, from a real protocol-parameters file when given."""
    limits = dict(DEFAULT_LIMITS)
    source = "mainnet defaults"
    if not params_path:
        return limits, source
    with open(params_path, encoding="utf-8") as fh:
        p = json.load(fh)
    if "maxTxSize" in p:
        limits["maxTxSize"] = int(p["maxTxSize"])
    ex = p.get("maxTxExecutionUnits") or {}
    # cardano-cli has spelled these both ways across eras.
    mem = ex.get("memory", ex.get("exUnitsMem"))
    steps = ex.get("steps", ex.get("exUnitsSteps"))
    if mem is not None:
        limits["maxTxExMem"] = int(mem)
    if steps is not None:
        limits["maxTxExSteps"] = int(steps)
    return limits, os.path.basename(params_path)


def validators(blueprint):
    """(title, size_in_bytes) per distinct compiled script.

    Aiken emits one blueprint entry per purpose, and a multi-purpose
    validator repeats the same `compiledCode` in each. Counting those
    separately would report a script twice and imply a total that is not
    what goes on chain, so entries are folded by compiled code.
    """
    seen = {}
    for v in blueprint.get("validators", []):
        code = v.get("compiledCode")
        if not code:
            continue
        # Hex string: two characters per on-chain byte.
        size = len(code) // 2
        title = v.get("title", "<untitled>")
        if code in seen:
            seen[code][0].append(title)
        else:
            seen[code] = ([title], size)
    return [(" / ".join(t), s) for t, s in seen.values()]


def load_budget(root):
    path = os.path.join(root, BUDGET_FILE)
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def check(root, params_path, report_only, aiken_output=None):
    bp_path = os.path.join(root, "plutus.json")
    if not os.path.exists(bp_path):
        print("plutus-budget: no plutus.json — nothing built to measure")
        return 0
    with open(bp_path, encoding="utf-8") as fh:
        blueprint = json.load(fh)

    limits, source = load_limits(params_path)
    budget = load_budget(root)
    max_bytes = budget.get("maxScriptBytes")
    failures = []

    scripts = validators(blueprint)
    if not scripts:
        print("plutus-budget: plutus.json contains no compiled validators")
        return 0

    width = max(len(t) for t, _ in scripts)
    print(f"plutus-budget: script sizes (limits from {source})")
    for title, size in sorted(scripts, key=lambda x: -x[1]):
        pct = 100.0 * size / limits["maxTxSize"]
        note = ""
        if size > limits["maxTxSize"]:
            note = "  EXCEEDS maxTxSize — cannot be submitted"
            failures.append(
                f"{title}: {size} B > maxTxSize {limits['maxTxSize']} B")
        elif max_bytes is not None and size > max_bytes:
            note = f"  over budget ({max_bytes} B)"
            failures.append(
                f"{title}: {size} B > project budget {max_bytes} B")
        print(f"  {title:<{width}}  {size:>6} B  {pct:5.1f}% of maxTxSize{note}")

    measured = {}
    if aiken_output:
        from aiken_units import check_version, parse
        check_version(root)
        with open(aiken_output, encoding="utf-8") as f:
            measured, excluded = parse(f.read())
        for name in excluded:
            print(f"plutus-budget: property statistics excluded: {name}")
    if measured:
        print("plutus-budget: measured Aiken unit scenarios (not a whole-transaction budget)")
        for name, units in sorted(measured.items()):
            print(f"  {name}: mem {units['mem']:,}, cpu {units['cpu']:,}")
            if units['mem'] > limits['maxTxExMem']:
                failures.append(f"{name}: measured mem {units['mem']} > maxTxExMem {limits['maxTxExMem']}")
            if units['cpu'] > limits['maxTxExSteps']:
                failures.append(f"{name}: measured cpu {units['cpu']} > maxTxExSteps {limits['maxTxExSteps']}")
    ex = budget.get("exUnits")
    if ex and not measured:
        mem, steps = ex.get("mem"), ex.get("steps")
        print("plutus-budget: measured execution units")
        if mem is not None:
            pct = 100.0 * mem / limits["maxTxExMem"]
            print(f"  mem    {mem:>14,}  {pct:5.1f}% of {limits['maxTxExMem']:,}")
            if mem > limits["maxTxExMem"]:
                failures.append(f"exUnits.mem {mem} > maxTxExMem {limits['maxTxExMem']}")
        if steps is not None:
            pct = 100.0 * steps / limits["maxTxExSteps"]
            print(f"  steps  {steps:>14,}  {pct:5.1f}% of {limits['maxTxExSteps']:,}")
            if steps > limits["maxTxExSteps"]:
                failures.append(
                    f"exUnits.steps {steps} > maxTxExSteps {limits['maxTxExSteps']}")
    elif not measured:
        # Saying nothing here would let an unmeasured budget read as a met
        # one, which is the failure mode this whole harness exists to stop.
        print("plutus-budget: execution units NOT measured — no exUnits in "
              f"{BUDGET_FILE}")
        print(f"  measure against mem {limits['maxTxExMem']:,} / "
              f"steps {limits['maxTxExSteps']:,} per transaction")

    if max_bytes is None:
        print(f"plutus-budget: no maxScriptBytes in {BUDGET_FILE} — protocol "
              "limits enforced, no headroom target set")

    if failures and not report_only:
        print("\nplutus-budget: RED — on-chain budget exceeded\n", file=sys.stderr)
        for f in failures:
            print(f"  {f}", file=sys.stderr)
        print("\n  A correctness proof says nothing about submittability.\n",
              file=sys.stderr)
        return 1
    if not failures:
        print("plutus-budget: green")
    return 0


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=".")
    ap.add_argument("--params", help="cardano-cli protocol-parameters JSON")
    ap.add_argument("--aiken-output", help="current pinned Aiken check JSON; measured unit scenarios supersede hand-entered exUnits")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true")
    g.add_argument("--report", action="store_true")
    a = ap.parse_args()
    try:
        return check(a.root, a.params, a.report, a.aiken_output)
    except (ValueError, OSError, subprocess.SubprocessError) as e:
        print(f"plutus-budget: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
