#!/usr/bin/env python3
"""Execute origin attribution on fresh archives of the pinned repair base.

Save the compiler's ORIGIN REPLAY CONTEXT as JSON, then run:
  repair-replay.py --root . --context context.json --defect-class decode \
    --control 'python valid.py' --command 'python attack.py'
Target classes come only from the compiler context. Optional --target-class
arguments assert that same set; they cannot choose a different attribution.
The control must exit 0. The attack exits 1 when the harm reproduces, 0 when
it is defended; every other exit, missing patch or failed control is INCOMPLETE.
Each complete patch is applied separately to a new base archive, never composed
with earlier round patches. stdout is JSON origin/introducedIn/originEvidence.
"""
import argparse
import io
import json
import locale
import math
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import time

from process_tree import CleanupPending, ProcessTree, shell_argv, remove_tree


def text(value):
    return isinstance(value, str) and bool(value.strip())


def target_classes(value):
    return isinstance(value, list) and all(text(item) for item in value) and len(set(value)) == len(value)


def execute(command, cwd, timeout):
    deadline = time.monotonic() + timeout
    tree = ProcessTree(shell_argv(command), cwd, subprocess.PIPE)
    try:
        tree.resume()
        stdout, _ = tree.proc.communicate(timeout=max(0, deadline - time.monotonic()))
        # A descendant may close/redirect its pipes and outlive a successful
        # shell. Its work remains part of this control or reproduction.
        while tree.running():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout, output=stdout)
            time.sleep(min(0.05, remaining))
    except BaseException:
        try:
            tree.terminate()
            tree.proc.communicate(timeout=5)
            cleanup_deadline = time.monotonic() + 5
            while tree.running():
                if time.monotonic() >= cleanup_deadline:
                    raise CleanupPending('replay command still has owned descendants after termination')
                time.sleep(0.05)
        except (OSError, subprocess.SubprocessError) as exc:
            raise CleanupPending('cannot confirm replay command tree drained: {}'.format(exc)) from exc
        raise
    finally:
        tree.close()
    output = stdout.decode(locale.getpreferredencoding(False), errors='replace').strip()
    return tree.proc.returncode, 'exit {}{}'.format(tree.proc.returncode, ': ' + output if output else '')


def observe(archive, patch, control, command, timeout):
    scratch = tempfile.mkdtemp(prefix='fluxpoint-origin-')
    cleanup = True
    try:
        with tarfile.open(fileobj=io.BytesIO(archive)) as package:
            # Git paths cannot traverse upwards, but refuse special archive
            # entries as well so extraction never escapes the scratch tree.
            for member in package.getmembers():
                path = Path(member.name)
                if path.is_absolute() or '..' in path.parts or member.isdev():
                    raise ValueError('unsafe base archive entry')
                if member.issym() or member.islnk():
                    target = Path(member.linkname)
                    if target.is_absolute() or '..' in target.parts:
                        raise ValueError('origin replay refuses an escaping archive link')
            package.extractall(scratch)
        if patch:
            applied = subprocess.run(['git', 'apply', '--whitespace=nowarn', str(Path(patch).resolve())],
                                     cwd=scratch, text=True, capture_output=True)
            if applied.returncode:
                raise ValueError('complete patch cannot apply to bare base: ' + applied.stderr.strip())
        rc, control_result = execute(control, scratch, timeout)
        if rc:
            raise ValueError('valid control failed on {}: {}'.format(patch or 'base', control_result))
        rc, result = execute(command, scratch, timeout)
        if rc not in (0, 1):
            raise ValueError('reproduction did not complete on {}: {}'.format(patch or 'base', result))
        return dict(command=command, result=result, controlCommand=control,
                    controlResult=control_result, controlPassed=True, reproduces=rc == 1)
    except CleanupPending as exc:
        cleanup = False
        raise CleanupPending('{}; snapshot preserved at {}'.format(exc, scratch)) from exc
    finally:
        if cleanup:
            remove_tree(scratch)


def replay(root, context, defect_class, targets, control, command, timeout):
    if (not isinstance(context, dict) or not text(context.get('base'))
            or type(context.get('round')) is not int or context['round'] < 1
            or not isinstance(context.get('patches'), list) or not context['patches']):
        raise ValueError('context requires pinned base and actual patches array')
    if not target_classes(context.get('targetClasses')):
        raise ValueError('context requires explicit pinned targetClasses (use [] for no targeted classes)')
    if targets is not None and (not target_classes(targets) or sorted(context['targetClasses']) != sorted(targets)):
        raise ValueError('target classes differ from the compiler context')
    targets = list(context['targetClasses'])
    def valid_patch(patch):
        return (isinstance(patch, dict) and type(patch.get('round')) is int
                and 1 <= patch['round'] <= context['round']
                and text(patch.get('item')) and text(patch.get('patch')))
    if not all(valid_patch(patch) for patch in context['patches']):
        raise ValueError('context contains an invalid actual patch identity')
    current = context.get('integrated') or context.get('current')
    if not valid_patch(current) or current['round'] != context['round']:
        raise ValueError('context must pin the exact current round patch')
    base = subprocess.run(['git', 'rev-parse', '--verify', context['base'] + '^{commit}'], cwd=root,
                          text=True, capture_output=True, check=True).stdout.strip()
    if base != context['base']:
        raise ValueError('context base must be the resolved launch commit')
    archive = subprocess.run(['git', 'archive', base], cwd=root, capture_output=True, check=True).stdout
    evidence = dict(base=dict(snapshot=base, **observe(archive, None, control, command, timeout)),
                    rounds=[], targetClasses=targets)
    seen = set()
    for patch in context['patches']:
        if (not isinstance(patch, dict) or type(patch.get('round')) is not int or patch['round'] < 1
                or not text(patch.get('item')) or not text(patch.get('patch'))):
            raise ValueError('invalid actual patch identity')
        key = (patch['item'], patch['round'], patch['patch'])
        if key in seen:
            raise ValueError('duplicate actual patch identity')
        seen.add(key)
        evidence['rounds'].append(dict(patch, **observe(archive, patch['patch'], control, command, timeout)))
    integrated = context.get('integrated')
    if integrated:
        evidence['integrated'] = dict(integrated, **observe(archive, integrated['patch'], control, command, timeout))
        current = evidence['integrated']
    else:
        current = next((p for p in evidence['rounds'] if all(p.get(k) == context.get('current', {}).get(k)
                       for k in ('item', 'round', 'patch'))), None)
    if not current or not current['reproduces']:
        raise ValueError('the claimed harm does not reproduce on the current patch')
    result = {'originEvidence': evidence}
    if evidence['base']['reproduces']:
        result['origin'] = 'incomplete-fix' if defect_class in targets else 'pre-existing'
    else:
        first = min((p['round'] for p in evidence['rounds'] if p['reproduces']), default=None)
        if first is not None:
            result.update(origin='fix-introduced', introducedIn=first)
        elif integrated:
            result['origin'] = 'cross-slice'
        else:
            raise ValueError('no replay establishes the finding origin')
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--root', default='.')
    ap.add_argument('--context', required=True)
    ap.add_argument('--defect-class', required=True)
    ap.add_argument('--target-class', action='append', default=None,
                    help='optional assertion of the compiler-pinned target class set')
    ap.add_argument('--control', required=True)
    ap.add_argument('--command', required=True)
    ap.add_argument('--timeout', type=float, default=120)
    args = ap.parse_args()
    try:
        with open(args.context, encoding='utf-8') as fh:
            context = json.load(fh)
        if not math.isfinite(args.timeout) or args.timeout <= 0:
            raise ValueError('timeout must be positive')
        result = replay(args.root, context, args.defect_class, args.target_class, args.control, args.command, args.timeout)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError, tarfile.TarError) as exc:
        print('repair-replay: INCOMPLETE: {}'.format(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
