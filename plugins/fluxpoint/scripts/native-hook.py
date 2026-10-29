#!/usr/bin/env python3
"""Advisory attestation adapter; launched with bounded input by native-hook.mjs."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import attest


def git(root, *args):
    try:
        result = subprocess.run(['git', '-C', str(root), *args], capture_output=True, text=True, encoding='utf-8', timeout=2)
        return result.stdout.strip() if result.returncode == 0 else ''
    except (OSError, subprocess.SubprocessError):
        return ''


def contained(base, candidate):
    try:
        return os.path.normcase(os.path.commonpath([os.path.realpath(base), os.path.realpath(candidate)])) == os.path.normcase(os.path.realpath(base))
    except ValueError:
        return False


def project_root(payload):
    project = os.environ.get('CLAUDE_PROJECT_DIR') or ''
    if project and git(project, 'rev-parse', '--is-inside-work-tree') == 'true':
        return Path(project).resolve()
    raw = payload.get('cwd') if isinstance(payload.get('cwd'), str) else ''
    if project:
        candidate = Path(project) / (raw or '.')
        if not Path(project).is_dir() or not contained(project, candidate) or not candidate.is_dir():
            return None
    else:
        candidate = Path(raw) if raw and Path(raw).is_dir() else Path.cwd()
    top = git(candidate, 'rev-parse', '--show-toplevel')
    if not top or project and not contained(project, top):
        return None
    return Path(top).resolve()


def main():
    if os.environ.get('FPL_DISABLE') == '1':
        return 0
    payload = json.load(sys.stdin)
    if not isinstance(payload, dict):
        raise ValueError('hook payload must be an object')
    root = project_root(payload)
    if root is None or not (root / '.fluxpoint-gates.json').is_file():
        return 0
    os.chdir(root)
    _, findings = attest.record(str(root), payload)
    if findings:
        state = root / '.claude/fluxpoint'
        state.mkdir(parents=True, exist_ok=True)
        sid = re.sub('[^A-Za-z0-9_-]', '-', str(payload.get('session_id') or 'nosession'))[:200]
        try:
            with (state / (sid + '.attest-warned')).open('x'):
                pass
        except FileExistsError:
            return 0
        print(json.dumps({'systemMessage': 'fluxpoint attest: ' + '; '.join(findings)}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
