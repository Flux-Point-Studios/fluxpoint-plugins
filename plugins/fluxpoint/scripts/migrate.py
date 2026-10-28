#!/usr/bin/env python3
"""Migrate a pre-1.0 repo onto the unified WORK.md contract.

This used to be prose in a slash command, which meant a `git rm` of a repo's
work-state files was driven by an agent following instructions. The
mechanical parts are now code with tests, and the command drives this.

Three phases, deliberately separate so nothing is deleted before the result
has been checked:

  --plan       report what would change; touch nothing
  --apply      write WORK.md, move local state, rewire config; the old files
               are left in place so the result can be inspected and diffed
  --finalize   remove the superseded files, only after verifying WORK.md
               carries at least as many Evidence rows as the sources did

Only one thing is left to judgment and stays with the agent: converting a
pre-0.2 prose GRAPH.md (tables, no IR block) into a graph-ir block. This
script reports that case and refuses to invent one.
"""
import argparse
import json
import os
import re
import shutil
import sys

LOOP = "LOOP.md"
GRAPH = "GRAPH.md"
WORK = "WORK.md"
OLD_PROMPT = "LOOP_PROMPT.md"
NEW_PROMPT = "WORK_PROMPT.md"
OLD_DIRS = [".claude/fluxpoint-loop", ".claude/fluxpoint-graph"]
NEW_DIR = ".claude/fluxpoint"

UNIFIED_HDR = "| When (UTC) | Source | Outcome | Claim | Proof |"
UNIFIED_SEP = "|---|---|---|---|---|"
IR_FENCE = re.compile(r"```json\s+graph-ir\s*\n.*?\n```", re.S)


# ------------------------------------------------------------------ parsing
def sections(text):
    """Split markdown into (heading, body) by '## ', preserving order."""
    out, head, buf = [], None, []
    for line in text.splitlines():
        if line.startswith("## "):
            out.append((head, "\n".join(buf).strip("\n")))
            head, buf = line[3:].strip(), []
        else:
            buf.append(line)
    out.append((head, "\n".join(buf).strip("\n")))
    return out


def table_rows(body):
    """Data rows of the first markdown table in a section."""
    rows, seen_sep = [], False
    for line in body.splitlines():
        s = line.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if all(set(c) <= set("-: ") and c for c in cells):
            seen_sep = True
            continue
        if seen_sep and any(cells):
            rows.append(cells)
    return rows


def to_unified(rows):
    """Map pre-1.0 Evidence rows onto | When | Source | Outcome | Claim | Proof |."""
    out = []
    for r in rows:
        if len(r) >= 7:  # graph: when runId outcome nodes findings harness redteam
            when, run, outcome, nodes, findings, harness, red = r[:7]
            out.append([when, run, outcome,
                        f"graph run: nodes {nodes}, {findings} finding(s)",
                        f"harness {harness}; red-team {red}"])
        elif len(r) >= 5:  # already unified
            out.append(r[:5])
        elif len(r) >= 3:  # loop: when claim proof
            out.append([r[0], "loop", "—", r[1], r[2]])
        elif r:
            out.append([r[0], "loop", "—", " ".join(r[1:]) or "—", "—"])
    return out


def survey(root):
    p = lambda *a: os.path.join(root, *a)
    ir_block = None
    graph_text = ""
    if os.path.exists(p(GRAPH)):
        graph_text = open(p(GRAPH), encoding="utf-8").read()
        m = IR_FENCE.search(graph_text)
        ir_block = m.group(0) if m else None
    return {
        "loop": os.path.exists(p(LOOP)),
        "graph": os.path.exists(p(GRAPH)),
        "work": os.path.exists(p(WORK)),
        "old_prompt": os.path.exists(p(OLD_PROMPT)),
        "graph_has_ir": ir_block is not None,
        "ir_block": ir_block,
        "graph_text": graph_text,
        "loop_text": open(p(LOOP), encoding="utf-8").read() if os.path.exists(p(LOOP)) else "",
        "old_dirs": [d for d in OLD_DIRS if os.path.isdir(p(d))],
        "settings": os.path.exists(p(".claude/settings.json")),
        "gitignore": os.path.exists(p(".gitignore")),
        "workflows": sorted(
            f for f in os.listdir(p(".claude/workflows"))
            if f.endswith(".graph.js")
        ) if os.path.isdir(p(".claude/workflows")) else [],
    }


# ------------------------------------------------------------------ building
def build_work(s):
    """Merge LOOP.md and GRAPH.md into one WORK.md, preserving content."""
    loop_secs = sections(s["loop_text"]) if s["loop_text"] else []
    graph_secs = sections(s["graph_text"]) if s["graph_text"] else []
    get = lambda secs, name: next((b for h, b in secs if h == name), "")

    preamble = loop_secs[0][1] if loop_secs else (graph_secs[0][1] if graph_secs else "")
    lines = preamble.splitlines()
    title = next((l for l in lines if l.startswith("# ")), "# WORK: <goal>")
    title = re.sub(r"^# (LOOP|GRAPH):", "# WORK:", title)
    mode = "both" if (s["loop"] and s["graph"]) else ("graph" if s["graph"] else "loop")
    # A migrated graph lands at DESIGN, not ACTIVE. graph-run's preflight
    # accepts READY or RUNNING, and /fluxpoint:graph-design is what flips
    # DESIGN to READY -- so defaulting a graph to ACTIVE handed the user a
    # file the run gate refuses, with the command that fixes it named
    # nowhere in the migrate flow. Loop mode keeps ACTIVE, where READY has
    # no meaning.
    default_status = "STATUS: DESIGN" if mode in ("graph", "both") else "STATUS: ACTIVE"
    status = next((l for l in lines if l.startswith("STATUS:")), default_status)
    if mode in ("graph", "both"):
        # ACTIVE is a loop word. Carrying it into a graph file is the drift
        # itself: graph-run accepts READY or RUNNING, so a migrated campaign
        # inherited a status its own run gate refuses. DESIGN is the honest
        # landing state -- the IR came from a loop and has not been reviewed
        # as a graph yet -- and graph-design is what flips it to READY.
        if status.split(":", 1)[1].strip() in ("ACTIVE", ""):
            status = "STATUS: DESIGN"

    ev_rows = to_unified(table_rows(get(loop_secs, "Evidence"))) + \
              to_unified(table_rows(get(graph_secs, "Evidence")))
    ev_rows.sort(key=lambda r: r[0])

    notes = "\n\n".join(
        b for b in (get(loop_secs, "Notes for the next iteration"),
                    get(graph_secs, "Notes for the next run")) if b
    ) or "<current state, blockers, resume point>"

    out = [title, "", status, f"MODE: {mode}", ""]

    def add(name, body, fallback=""):
        out.extend([f"## {name}", body or fallback, ""])

    add("Definition of Done", get(loop_secs, "Definition of Done"),
        "- [ ] `scripts/harness.sh --full` exits 0")
    if mode in ("loop", "both"):
        add("Plan", get(loop_secs, "Plan"), "- [ ] <next slice>")
    if mode in ("graph", "both"):
        if s["ir_block"]:
            body = s["ir_block"]
        else:
            body = ("<!-- MIGRATION: the previous GRAPH.md predates the IR and\n"
                    "     described its campaign in prose. Convert it to a\n"
                    "     ```json graph-ir block per the graph-engineering skill,\n"
                    "     then run: compile-graph.py WORK.md --check -->\n\n"
                    + (get(graph_secs, "Work graph") or "").strip())
        add("Campaign", body)
    add("Constraints", get(loop_secs, "Constraints"), "- <what must not change>")
    add("Merge policy", get(loop_secs, "Merge policy"), "- Auto-merge: <yes | no>.")

    ev = ["## Evidence",
          "Graph rows are appended by `scripts/record-run.py`; loop rows are "
          "written per slice.", "", UNIFIED_HDR, UNIFIED_SEP]
    ev += ["| " + " | ".join(r) + " |" for r in ev_rows]
    out.extend(ev + [""])
    out.extend(["## Notes for the next iteration", notes, ""])
    return "\n".join(out), len(ev_rows)


def rewire_gitignore(path):
    lines = open(path, encoding="utf-8").read().splitlines() if os.path.exists(path) else []
    kept = [l for l in lines if l.strip().rstrip("/") not in
            (".claude/fluxpoint-loop", ".claude/fluxpoint-graph")]
    # .claude/worktrees/ holds the isolated trees mutating nodes run in;
    # they are transient and must never be committed.
    for want in (f"{NEW_DIR}/", ".claude/worktrees/", ".claude/workflows/", "__pycache__/"):
        if want not in [l.strip() for l in kept]:
            kept.append(want)
    open(path, "w", encoding="utf-8", newline="\n").write("\n".join(kept) + "\n")
    return kept


OLD_PLUGINS = ("fluxpoint-loop@fluxpoint", "fluxpoint-graph@fluxpoint")
NEW_PLUGIN = "fluxpoint@fluxpoint"


def check_settings(path):
    """Parse settings before anything is mutated. Returns an error string, or
    None when it is safe to proceed."""
    if not os.path.exists(path):
        return None
    try:
        d = json.load(open(path, encoding="utf-8"))
    except json.JSONDecodeError as e:
        return f"{path} is not valid JSON ({e}); fix it before migrating"
    ep = d.get("enabledPlugins")
    if ep is not None and not isinstance(ep, (dict, list)):
        return f"{path}: enabledPlugins is a {type(ep).__name__}; expected an object or list"
    return None


def rewire_settings(path):
    """Swap the split plugins for the unified one. Returns the new value, or
    None when there was nothing to rewire — never a value implying success
    that did not happen."""
    if not os.path.exists(path):
        return None
    d = json.load(open(path, encoding="utf-8"))
    ep = d.get("enabledPlugins")
    if isinstance(ep, dict):
        for old in OLD_PLUGINS:
            ep.pop(old, None)
        ep[NEW_PLUGIN] = True
    elif isinstance(ep, list):
        # The list form is equally valid config and used to be skipped
        # silently, leaving the repo enabling plugins that no longer exist.
        ep = [x for x in ep if x not in OLD_PLUGINS]
        if NEW_PLUGIN not in ep:
            ep.append(NEW_PLUGIN)
    else:
        return None
    d["enabledPlugins"] = ep
    json.dump(d, open(path, "w", encoding="utf-8", newline="\n"), indent=2)
    open(path, "a", encoding="utf-8", newline="\n").write("\n")
    return ep


# --------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=".")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--plan", action="store_true")
    g.add_argument("--apply", action="store_true")
    g.add_argument("--finalize", action="store_true")
    args = ap.parse_args()
    root = args.root
    p = lambda *a: os.path.join(root, *a)
    s = survey(root)

    if not (s["loop"] or s["graph"]):
        print("migrate: no LOOP.md or GRAPH.md — nothing to migrate "
              "(this repo is already unified, or was never onboarded)")
        return 0

    if args.plan:
        print("migrate --plan")
        print(f"  sources        : {'LOOP.md ' if s['loop'] else ''}"
              f"{'GRAPH.md' if s['graph'] else ''}".strip())
        _, n = build_work(s)
        print(f"  WORK.md        : would be written, MODE "
              f"{'both' if s['loop'] and s['graph'] else ('graph' if s['graph'] else 'loop')}, "
              f"{n} Evidence row(s) carried")
        if s["graph"] and not s["graph_has_ir"]:
            print("  campaign       : GRAPH.md predates the IR — a graph-ir block "
                  "must be authored by hand; this script will not invent one")
        if s["old_prompt"]:
            print(f"  {OLD_PROMPT} : would be renamed to {NEW_PROMPT}")
        for d in s["old_dirs"]:
            print(f"  state          : {d} -> {NEW_DIR}")
        if s["workflows"]:
            print(f"  workflows      : {len(s['workflows'])} compiled script(s) "
                  f"would be removed (build output; recompile from WORK.md)")
        err = check_settings(p(".claude/settings.json"))
        if err:
            print(f"  BLOCKER        : {err}")
        print("  deletions      : none in this phase (--finalize removes sources)")
        return 0

    if args.apply:
        # Everything that can fail is checked before anything is written, so a
        # bad settings file cannot leave a half-migrated tree that --apply
        # then refuses to touch again.
        err = check_settings(p(".claude/settings.json"))
        if err:
            print(f"migrate: {err}", file=sys.stderr)
            print("migrate: nothing was changed", file=sys.stderr)
            return 1
        if s["work"]:
            print("migrate: WORK.md already exists — refusing to overwrite. "
                  "Move it aside and re-run, or finish the migration by hand.",
                  file=sys.stderr)
            return 1
        text, n = build_work(s)
        open(p(WORK), "w", encoding="utf-8", newline="\n").write(text)
        print(f"migrate --apply")
        print(f"  wrote {WORK} ({n} Evidence row(s) carried)")

        if s["old_prompt"] and not os.path.exists(p(NEW_PROMPT)):
            body = open(p(OLD_PROMPT), encoding="utf-8").read().replace(LOOP, WORK).replace(OLD_PROMPT, NEW_PROMPT)
            open(p(NEW_PROMPT), "w", encoding="utf-8", newline="\n").write(body)
            os.remove(p(OLD_PROMPT))
            print(f"  renamed {OLD_PROMPT} -> {NEW_PROMPT} (references updated)")

        os.makedirs(p(NEW_DIR), exist_ok=True)
        for d in s["old_dirs"]:
            for item in os.listdir(p(d)):
                src, dst = p(d, item), p(NEW_DIR, item)
                if item in ("runs",) and os.path.isdir(src):
                    os.makedirs(dst, exist_ok=True)
                    for f in os.listdir(src):
                        shutil.move(os.path.join(src, f), os.path.join(dst, f))
                elif item.endswith((".dirty", ".blocks")):
                    continue  # per-session, expires on its own
                elif not os.path.exists(dst):
                    shutil.move(src, dst)
            print(f"  moved state {d} -> {NEW_DIR}")

        rewire_gitignore(p(".gitignore"))
        print("  rewired .gitignore")
        ep = rewire_settings(p(".claude/settings.json"))
        if ep is not None:
            shown = sorted(ep) if isinstance(ep, list) else sorted(ep)
            print(f"  rewired enabledPlugins -> {shown}")
        elif s["settings"]:
            print("  settings.json has no enabledPlugins block — left untouched")

        for f in s["workflows"]:
            os.remove(p(".claude/workflows", f))
        if s["workflows"]:
            print(f"  removed {len(s['workflows'])} compiled workflow script(s) "
                  f"(build output)")

        print(f"  sources left in place: verify, then run --finalize")
        if s["graph"] and not s["graph_has_ir"]:
            print("  ACTION REQUIRED: author the graph-ir block in WORK.md's "
                  "Campaign section before finalizing")
        return 0

    # --finalize
    if not s["work"]:
        print("migrate: no WORK.md — run --apply first", file=sys.stderr)
        return 1
    work_rows = len(table_rows(next((b for h, b in sections(open(p(WORK), encoding="utf-8").read())
                                     if h == "Evidence"), "")))
    src_rows = len(table_rows(next((b for h, b in sections(s["loop_text"])
                                    if h == "Evidence"), ""))) + \
               len(table_rows(next((b for h, b in sections(s["graph_text"])
                                    if h == "Evidence"), "")))
    if work_rows < src_rows:
        print(f"migrate: WORK.md carries {work_rows} Evidence row(s) but the "
              f"sources had {src_rows} — refusing to delete evidence",
              file=sys.stderr)
        return 1
    removed = []
    for f in (LOOP, GRAPH):
        if os.path.exists(p(f)):
            os.remove(p(f))
            removed.append(f)
    print(f"migrate --finalize: removed {', '.join(removed)} "
          f"({work_rows} Evidence row(s) preserved in {WORK})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
