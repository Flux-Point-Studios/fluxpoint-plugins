#!/usr/bin/env python3
"""Watch a Workflow journal; surface stalls and failures with recoverable state.

The journal and transcript formats are internal runtime interfaces. Unknown agent
lifecycle events and malformed records exit 2 instead of declaring a run healthy. This
observer never kills an agent or resets its checkout.
"""
import argparse
import glob
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time

import inbox


def rows(path):
    try:
        values = [json.loads(line) for line in Path(path).read_text(
            encoding="utf-8").splitlines() if line.strip()]
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError(f"cannot parse {path}: {exc}") from exc
    if any(not isinstance(row, dict) for row in values):
        raise ValueError(f"{path}: every record must be an object")
    return values


def resolve_run(run, projects):
    if not run or Path(run).name != run or run in (".", ".."):
        raise ValueError("--run must be one run id, without path components")
    hits = sorted({p.parent.resolve() for p in Path(projects).glob(
        f"**/subagents/workflows/{glob.escape(run)}/journal.jsonl")})
    if len(hits) != 1:
        raise ValueError(f"run {run}: expected one journal under {projects}, found {len(hits)}")
    return hits[0]


def transcript(run_dir, agent):
    if not isinstance(agent, str) or not agent or Path(agent).name != agent:
        raise ValueError("journal agentId must be a nonempty filename component")
    # Runtime versions have used the workflow directory and its parent for
    # transcripts. Refuse ambiguity rather than inspect an unrelated agent.
    hits = {p.resolve() for parent in (run_dir, run_dir.parent, run_dir.parent.parent)
            for p in parent.glob(f"agent-{glob.escape(agent)}.jsonl") if p.is_file()}
    if len(hits) != 1:
        raise ValueError(f"agent {agent}: expected one transcript, found {len(hits)}")
    return hits.pop()


def git(cwd, *args):
    if not cwd or not Path(cwd).is_dir():
        return "unavailable (transcript cwd missing or absent)"
    result = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True,
                            text=True, timeout=20)
    return (result.stdout if result.returncode == 0 else result.stderr).rstrip() or "(clean / none)"


def last_tool(records):
    for row in reversed(records):
        message = row.get("message")
        content = message.get("content", []) if isinstance(message, dict) else row.get("content", [])
        if isinstance(content, list):
            for part in reversed(content):
                if isinstance(part, dict) and part.get("type") == "tool_use":
                    return json.dumps({"name": part.get("name"), "input": part.get("input")}, ensure_ascii=False)
    return "none recorded"


def resume_note(root, run, agent, info, records, base):
    cwd = next((r.get("cwd") for r in reversed(records) if r.get("cwd")), None)
    state = git(cwd, "status", "--porcelain")
    commits = git(cwd, "log", "--oneline", f"{base}..HEAD") if base else "unavailable (launch base not recorded)"
    name = hashlib.sha256(f"{run}:{agent}".encode()).hexdigest()[:20] + ".md"
    target = Path(root) / ".claude/fluxpoint/resume" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    body = (f"# Resume {run} / {agent}\n\nLabel: {info.get('label', agent)}\n\n"
            f"Working directory: {cwd or 'not recorded'}\n\nLast tool call:\n```json\n"
            f"{last_tool(records)}\n```\n\nGit status:\n```text\n{state}\n```\n\n"
            f"Commits since launch {base or '(unknown)'}:\n```text\n{commits}\n```\n\n"
            "A killed agent retains its changes and commits. Inspect this checkout before "
            "resuming; refresh the launch tree reading and re-run verdicts for changed code.\n")
    # Preserve the first failure's state across repeated watchdog launches.
    try:
        with target.open("x", encoding="utf-8") as fh:
            fh.write(body)
    except FileExistsError:
        pass
    return str(target)


def inspect(root, run, run_dir, stall_min=40, quiet_min=15, base=None, now=None):
    journal = Path(run_dir) / "journal.jsonl"
    events = rows(journal)
    states, starts = {}, {}
    for row in events:
        kind = row.get("type")
        if 'agentId' in row and kind not in ('started', 'result', 'failed'):
            raise ValueError(f'{journal}: unknown agent lifecycle event {kind!r}')
        if not isinstance(kind, str) and 'args' not in row:
            raise ValueError(f'{journal}: record lacks an event type or launch arguments')
        if kind in ("started", "result", "failed"):
            agent = row.get("agentId")
            if not isinstance(agent, str) or not agent:
                raise ValueError(f"{journal}: {kind} row lacks agentId")
            states[agent] = kind
            if kind == "started":
                starts[agent] = row
        # Read a captured launch argument where the journal supplies one.
        args = row.get("args")
        if args is not None:
            if not isinstance(args, dict) or ('_base' in args and not isinstance(args['_base'], dict)):
                raise ValueError(f'{journal}: launch args and _base must be objects')
            if not base:
                base = args.get('_base', {}).get('sha')
                if base is not None and (not isinstance(base, str) or not __import__('re').fullmatch(r'[0-9a-fA-F]{7,64}', base)):
                    raise ValueError(f'{journal}: launch base must be a commit hash')
    clock = time.time() if now is None else now
    alerts, live = [], False
    for agent, kind in states.items():
        if kind == "result":
            continue
        path = transcript(Path(run_dir), agent)
        records = rows(path)
        if not records:
            raise ValueError(f"{path}: empty transcript")
        if kind == "failed":
            last = records[-1]
            error = last.get("error", "unknown failure")
            quota = last.get('quotaLimits')
            if quota is not None and not isinstance(quota, dict):
                raise ValueError(f'{path}: quotaLimits must be an object')
            reset = (quota or {}).get("resetsAt")
            note = resume_note(root, run, agent, starts.get(agent, {}), records, base)
            detail = f"{error}; api status {last.get('apiErrorStatus', 'unknown')}"
            if reset:
                detail += f"; resetsAt {reset}"
            detail += f"; resume note: {note}"
            inbox.add(root, "agent-killed", agent, run, detail)
            alerts.append(f"agent-killed {agent}: {detail}")
        elif kind == "started":
            live = True
            minutes = (clock - path.stat().st_mtime) / 60
            if minutes >= stall_min:
                detail = f"silent for {minutes:.1f} min (threshold {stall_min:g}); transcript {path}"
                inbox.add(root, "agent-stalled", agent, run, detail)
                alerts.append(f"agent-stalled {agent}: {detail}")
    if alerts:
        return 1, alerts
    if not live and clock - journal.stat().st_mtime >= quiet_min * 60:
        return 0, ["no live agents; journal quiet"]
    return None, ["watching live agents" if live else "waiting for journal quiet"]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run", required=True)
    ap.add_argument("--root", default=".")
    ap.add_argument("--projects", default=str(Path.home() / ".claude/projects"))
    ap.add_argument("--base", help="args._base.sha captured at this launch")
    ap.add_argument("--stall-min", type=float, default=40)
    ap.add_argument("--quiet-min", type=float, default=15)
    ap.add_argument("--interval", type=float, default=60)
    ap.add_argument("--once", action="store_true", help="inspect once; exit 3 while pending")
    args = ap.parse_args()
    if any(not math.isfinite(value) or value <= 0 for value in (args.stall_min, args.quiet_min, args.interval)):
        ap.error("time limits must be finite and positive")
    if args.base and not __import__('re').fullmatch(r"[0-9a-fA-F]{7,64}", args.base):
        ap.error("--base must be a commit hash")
    try:
        directory = resolve_run(args.run, args.projects)
        while True:
            code, messages = inspect(args.root, args.run, directory,
                                     args.stall_min, args.quiet_min, args.base)
            for message in messages:
                print(f"watchdog: {message}", flush=True)
            if code is not None:
                return code
            if args.once:
                return 3
            time.sleep(args.interval)
    except (OSError, ValueError, TypeError, subprocess.SubprocessError) as exc:
        print(f"watchdog: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
