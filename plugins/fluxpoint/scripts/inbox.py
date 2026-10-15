#!/usr/bin/env python3
"""The owner's inbox: everything waiting on a human, in one place.

A campaign that parks instead of halting is only an improvement if somebody
finds out. Otherwise the blocked node is a quieter failure than the halt it
replaced — the run reports INCOMPLETE, nobody reads the provenance, and the
work sits.

So every event that needs a person appends a row here: a node blocked on a
human or a third party, a wake deadline that expired with its condition
unmet, an irreversible node that refused to fire without confirmation, an
outer loop that spent its iteration budget. `/fluxpoint:status` leads with
the open count and SessionStart injects it, so the first thing anyone sees
in a fresh context is what is waiting on them.

Rows are resolved explicitly, never expired automatically: something that
stops being shown because time passed is something that stops being done.

  inbox.py --add --kind K --node N --detail D    append an item
  inbox.py --count                               open items, for prompts
  inbox.py --list                                open items, for humans
  inbox.py --resolve ID                          close one
"""
import argparse
import datetime
import json
import os
import sys

INBOX = os.path.join(".claude", "fluxpoint", "inbox.jsonl")

KINDS = {
    "blocked": "a node is waiting on a person",
    "wake-ready": "a parked wait cleared and the campaign can resume",
    "wake-expired": "a wake deadline passed with the condition unmet",
    "confirm-required": "an irreversible node refused to fire unconfirmed",
    "plateau": "an unattended run stopped after metric progress stalled",
    "budget-exhausted": "an unattended run spent its iteration budget",
    "attest-mismatch": "a node's claimed gate exit contradicts the attested log",
    "cex-ingest-failed": "prover output could not be parsed into a counterexample",
    "cex-pin-inert": "a pinned counterexample was re-found, so its test does not bite",
    "mutation-stale": "the mutation score no longer describes this tree",
    "box-unhealthy": "machine health degraded during a Workflow; operator inspection required",
    "agent-stalled": "a live Workflow agent stopped producing transcript activity",
    "agent-killed": "a failed Workflow agent needs inspection and a resume",
}


def path_for(root):
    return os.path.join(root, INBOX)


def read(root):
    p = path_for(root)
    if not os.path.exists(p):
        return []
    rows = []
    with open(p, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return rows


def open_items(root):
    """Latest state per id, keeping only what is still open."""
    state = {}
    for r in read(root):
        state[r.get("id")] = r
    return [r for r in state.values() if not r.get("resolved")]


def add(root, kind, node, campaign, detail):
    p = path_for(root)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    when = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    item_id = f"{kind}:{campaign or '-'}:{node or '-'}"
    # Re-raising the same block on every run would bury the inbox in
    # duplicates of one problem, so identity is the thing, not the event.
    for r in open_items(root):
        if r.get("id") == item_id:
            return None
    row = {"id": item_id, "kind": kind, "node": node, "campaign": campaign,
           "detail": detail, "when": when, "resolved": False}
    with open(p, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(row) + "\n")
    return row


def resolve(root, item_id):
    p = path_for(root)
    if not os.path.exists(p):
        return False
    hit = [r for r in read(root) if r.get("id") == item_id]
    if not hit:
        return False
    row = dict(hit[-1])
    row["resolved"] = True
    row["resolvedWhen"] = datetime.datetime.now(
        datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(p, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(row) + "\n")
    return True


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=".")
    ap.add_argument("--kind", choices=sorted(KINDS))
    ap.add_argument("--node", default="")
    ap.add_argument("--campaign", default="")
    ap.add_argument("--detail", default="")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--add", action="store_true")
    g.add_argument("--count", action="store_true")
    g.add_argument("--list", action="store_true")
    g.add_argument("--resolve", metavar="ID")
    a = ap.parse_args()

    if a.add:
        if not a.kind:
            ap.error("--add requires --kind")
        row = add(a.root, a.kind, a.node, a.campaign, a.detail)
        print(f"inbox: {'added' if row else 'already open'} {a.kind} {a.node}")
        return 0
    if a.count:
        print(len(open_items(a.root)))
        return 0
    if a.resolve:
        ok = resolve(a.root, a.resolve)
        print(f"inbox: {'resolved' if ok else 'no such item'} {a.resolve}")
        return 0 if ok else 1

    items = open_items(a.root)
    if not items:
        print("inbox: nothing is waiting on you")
        return 0
    print(f"inbox: {len(items)} item(s) waiting on you")
    for r in sorted(items, key=lambda x: x.get("when", "")):
        who = f"{r.get('campaign') or '-'} / {r.get('node') or '-'}"
        print(f"  [{r['kind']}] {who}   ({r.get('when')})")
        if r.get("detail"):
            print(f"      {r['detail']}")
        print(f"      resolve with: inbox.py --resolve {r['id']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
