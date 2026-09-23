#!/usr/bin/env python3
"""Release a blocked node: record the proof that a human or third party acted.

A campaign with `actor: human` or `actor: third-party` nodes parks rather
than halting. This is how the park is lifted — and the discipline is the
one the Stop gate already applies to harness output: a claim without proof
is treated as false.

So a release is not "yes, done". It is a document validated against the
node's declared `proofContract`: a real transaction hash for a signature,
an exit code for a check, whatever the contract names. An adjective is
rejected. That is the whole point — an unattended run resumes on the
strength of this file, and a run that resumes on somebody's recollection
is a run that will eventually resume on a mistake.

  release.py --campaign C --node N --proof FILE     record a release
  release.py --load --campaign C                    map for args._releases
  release.py --list                                 what is parked, what is freed

Nothing here decides whether the work was done well. It decides whether
what was pasted is the shape the graph said it needed.
"""
import argparse
import datetime
import json
import os
import sys

REL_DIR = os.path.join(".claude", "fluxpoint", "releases")


def slug(s):
    return "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in s)[:80]


def dir_for(root, campaign):
    return os.path.join(root, REL_DIR, slug(campaign))


def validate(proof, schema):
    """Check a pasted proof against its contract. Returns a list of problems.

    Deliberately shallow — required keys, declared types, enum membership.
    A full JSON Schema implementation is not the point; catching "SHIP" typed
    where an object belongs, or a missing tx hash, is.
    """
    problems = []
    if not isinstance(proof, dict):
        return [f"proof must be a JSON object, got {type(proof).__name__}"]
    for req in schema.get("required", []):
        if req not in proof:
            problems.append(f"missing required field '{req}'")
    props = schema.get("properties", {})
    for k, v in proof.items():
        spec = props.get(k)
        if not spec:
            continue
        if "enum" in spec and v not in spec["enum"]:
            problems.append(f"'{k}' must be one of {spec['enum']}, got {v!r}")
        want = spec.get("type")
        kinds = {"string": str, "integer": int, "number": (int, float),
                 "boolean": bool, "array": list, "object": dict}
        if want in kinds and not isinstance(v, kinds[want]):
            problems.append(f"'{k}' must be {want}, got {type(v).__name__}")
        if want == "string" and isinstance(v, str) and not v.strip():
            problems.append(f"'{k}' is empty — a blank proof is not a proof")
    return problems


def load_contract(plugin_root, name, root=".", graph=None):
    """The proofContract schema, repo-local first when the graph declares one.

    A graph whose `CONTRACTS:` header names a repo-local directory compiles
    against that set overlaid on the shipped one, so a park whose
    proofContract lives there must be released against the same file —
    otherwise the compiler accepts a node the release can never clear.
    """
    if graph:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import specification
        try:
            local = specification.graph_headers(os.path.join(root, graph)).get("CONTRACTS")
            if local:
                d = os.path.join(root, str(specification.in_repo(local, "CONTRACTS:")))
                found = []
                for fn in sorted(os.listdir(d)) if os.path.isdir(d) else []:
                    if not fn.endswith(".schema.json"):
                        continue
                    with open(os.path.join(d, fn), encoding="utf-8") as fh:
                        schema = json.load(fh)
                    if schema.get("$id", fn.split(".")[0]) == name:
                        found.append((fn, schema))
                # The compiler refuses a directory where two files claim one
                # $id; so does the release, or each could bind a different one.
                if len(found) > 1:
                    raise ValueError(f"{', '.join(f for f, _ in found)} all declare $id "
                                     f"{name} — one contract name, one file")
                if found:
                    return found[0][1]
        except (OSError, ValueError) as e:
            raise SystemExit(f"release: {graph}: {e}")
    p = os.path.join(plugin_root, "contracts", f"{name}.schema.json")
    if not os.path.exists(p):
        raise SystemExit(f"release: unknown contract '{name}' ({p} not found)")
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=".")
    ap.add_argument("--plugin-root", default=os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))
    ap.add_argument("--campaign")
    ap.add_argument("--node")
    ap.add_argument("--contract", help="the node's release.proofContract")
    ap.add_argument("--graph", default="WORK.md",
                    help="the campaign's graph file; its CONTRACTS: header is honored")
    ap.add_argument("--proof", help="file holding the proof JSON (default stdin)")
    ap.add_argument("--by", default=os.environ.get("USER", "operator"))
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--record", action="store_true")
    g.add_argument("--load", action="store_true")
    g.add_argument("--list", action="store_true")
    a = ap.parse_args()

    if a.load:
        if not a.campaign:
            ap.error("--load requires --campaign")
        d = dir_for(a.root, a.campaign)
        out = {}
        if os.path.isdir(d):
            for fn in sorted(os.listdir(d)):
                if fn.endswith(".json"):
                    with open(os.path.join(d, fn), encoding="utf-8") as fh:
                        rec = json.load(fh)
                    out[rec.get("node", fn[:-5])] = rec
        print(json.dumps(out))
        return 0

    if a.list:
        base = os.path.join(a.root, REL_DIR)
        if not os.path.isdir(base):
            print("release: nothing released yet")
            return 0
        for camp in sorted(os.listdir(base)):
            print(f"  {camp}")
            for fn in sorted(os.listdir(os.path.join(base, camp))):
                if not fn.endswith(".json"):
                    continue
                with open(os.path.join(base, camp, fn), encoding="utf-8") as fh:
                    r = json.load(fh)
                print(f"      {r.get('node')}  released {r.get('when')} "
                      f"by {r.get('by')}")
        return 0

    for req in ("campaign", "node", "contract"):
        if not getattr(a, req.replace("-", "_")):
            ap.error(f"--record requires --{req}")
    schema = load_contract(a.plugin_root, a.contract, a.root, a.graph)
    raw = open(a.proof, encoding="utf-8").read() if a.proof else sys.stdin.read()
    try:
        proof = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"release: proof is not JSON: {e}", file=sys.stderr)
        print("  A release is a document, not a sentence. Paste the "
              f"{a.contract} the node asked for.", file=sys.stderr)
        return 1
    problems = validate(proof, schema)
    if problems:
        print(f"release: REFUSED — the proof does not satisfy {a.contract}",
              file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        print("\n  The graph resumes on the strength of this file. It is not "
              "a formality.", file=sys.stderr)
        return 1

    d = dir_for(a.root, a.campaign)
    os.makedirs(d, exist_ok=True)
    rec = {
        "campaign": a.campaign,
        "node": a.node,
        "contract": a.contract,
        "by": a.by,
        "when": datetime.datetime.now(datetime.timezone.utc)
                .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "proof": proof,
    }
    path = os.path.join(d, f"{slug(a.node)}.json")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(rec, fh, indent=2)
    print(f"release: {a.node} released by {a.by} — {path}")
    print("  Re-run /fluxpoint:graph-run to continue the campaign from here.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
