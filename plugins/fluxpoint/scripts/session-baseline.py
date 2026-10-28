#!/usr/bin/env python3
"""Distinguish inherited checkouts from this session's unjudged commits.

A commit id alone cannot delimit reflog events: checking out an old id then
returning to a newly authored one repeats ids in the log. The count captured
at SessionStart/last PASS bounds all events since then. Imports may change
the diff baseline, while that anchor survives until the work actually passes.
"""
from pathlib import Path
import re
import subprocess
import sys


def git(*args):
    return subprocess.check_output(['git', *args], stderr=subprocess.DEVNULL).decode().strip()


def ancestor(base, head):
    return subprocess.run(['git', 'merge-base', '--is-ancestor', base, head],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def main():
    state = Path(sys.argv[1])
    try:
        base = state.read_text().strip()
    except OSError:
        base = 'HEAD'
    current = git('rev-parse', 'HEAD')
    entries = git('reflog', 'show', '--format=%H%x09%gs', 'HEAD').splitlines()
    try:
        seen = int(Path(str(state) + '.reflog-count').read_text().strip())
        fresh = entries[:len(entries) - seen] if 0 <= seen <= len(entries) else []
    except (OSError, ValueError):
        # Existing sessions without an anchor retain their old baseline.
        # Guessing which checkout was inherited would forgive real work.
        fresh = []
    events = [line.split('\t', 1) for line in fresh if '\t' in line]
    created = {sha for sha, action in events if (
        re.match(r'commit(?: \([^)]*\))?:|cherry-pick:|rebase \((?:pick|continue)\):', action)
        or ('Merge made by' in action))}
    # Any current ancestor created in this checkout since the last PASS is
    # still this session's work, including after detach, reset, or a merge
    # from another branch. The first such commit's parent excludes imports
    # before it without exempting that commit itself.
    ordered = git('rev-list', '--reverse', '--topo-order', current).splitlines() if created else []
    first = next((sha for sha in ordered if sha in created), None)
    if first:
        parents = git('rev-list', '--parents', '-n', '1', first).split()
        if len(parents) > 1:
            base = parents[1]
    elif events:
        imported = next((sha for sha, action in events if action.startswith('checkout:')), None)
        if imported and ancestor(imported, current):
            base = imported
    if base != 'HEAD' and not ancestor(base, current):
        base = 'HEAD'
    state.parent.mkdir(parents=True, exist_ok=True)
    if base != 'HEAD':
        state.write_text(base + '\n')
    Path(str(state) + '.head').write_text(current + '\n')
    print(base, end='')


if __name__ == '__main__':
    try:
        main()
    except (OSError, subprocess.CalledProcessError):
        # Preserve the old safe baseline when reflog is unavailable.
        try:
            base = Path(sys.argv[1]).read_text().strip()
            print(base if base and ancestor(base, 'HEAD') else 'HEAD', end='')
        except OSError:
            print('HEAD', end='')
