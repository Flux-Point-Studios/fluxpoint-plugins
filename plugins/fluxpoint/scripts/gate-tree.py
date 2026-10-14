#!/usr/bin/env python3
"""Hash HEAD, tracked binary diffs and every untracked path and its bytes."""
import hashlib
import os
import subprocess
import sys


def git(*args):
    return subprocess.check_output(['git', *args], stderr=subprocess.DEVNULL)


def main():
    work = sys.argv[1] if len(sys.argv) > 1 else ''
    paths = ['--', '.', f':!{work}'] if work else []
    h = hashlib.sha256()

    def add(data):
        # Length framing makes paths and contents unambiguous, including
        # filenames containing newlines, tabs, or diff-looking bytes.
        h.update(len(data).to_bytes(8, 'big'))
        h.update(data)

    add(git('rev-parse', 'HEAD'))
    add(git('diff', 'HEAD', '--binary', '--no-ext-diff', *paths))
    for raw in sorted(git('ls-files', '-z', '--others', '--exclude-standard').split(b'\0')):
        if not raw or os.fsdecode(raw) == work:
            continue
        path = os.fsdecode(raw)
        add(raw)
        if os.path.islink(path):
            add(os.fsencode(os.readlink(path)))
        else:
            add(git('hash-object', '--', path).strip())
    print(h.hexdigest())


if __name__ == '__main__':
    try:
        main()
    except (OSError, subprocess.CalledProcessError):
        sys.exit(1)
