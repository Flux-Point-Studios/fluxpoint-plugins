"""Shared precondition for safe, reviewable baseline tightening."""
import os
import subprocess


def clean(root):
    result = subprocess.run(['git', '-C', root, 'status', '--porcelain', '--untracked-files=all'],
                            capture_output=True, text=True, check=True)
    if result.stdout.strip():
        raise ValueError('--tighten requires a clean tree (including untracked files)')


def green(root):
    """Run the real full harness; neither cached verdicts nor caller claims suffice."""
    clean(root)
    harness = os.path.join(root, 'scripts', 'harness.sh')
    if not os.path.isfile(harness):
        raise ValueError('--tighten requires scripts/harness.sh --full')
    result = subprocess.run(['bash', 'scripts/harness.sh', '--full'], cwd=root,
                            capture_output=True, text=True)
    if result.returncode:
        raise ValueError('--tighten refused: harness --full is red\n' +
                         (result.stdout + result.stderr)[-3000:])
    clean(root)


def tighten(kind, root, src=None):
    """Measure before writing; preserve all sibling baseline sections."""
    import importlib.util
    import json
    import sys
    root = os.path.abspath(root)
    green(root)
    path = os.path.join(os.path.dirname(__file__), kind + '-guard.py')
    spec = importlib.util.spec_from_file_location('_tightened_guard', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if kind == 'mutation':
        cfg, findings = mod.load_config(root)
        if findings or cfg is None:
            raise ValueError('--tighten requires an armed mutation manifest: ' + '; '.join(findings))
        prior = mod.load_baseline(root).get('mutation')
        if not prior:
            raise ValueError('--tighten requires an existing mutation measurement')
        rec = mod.measure(root, cfg, False, '', src, persist=False)
        if not isinstance(rec, dict):
            raise ValueError('--tighten refused a weaker or failed mutation measurement')
        clean(root)
        if rec['score'] != prior['score'] or rec['survived'] != prior['survived']:
            print(f'mutation-guard: score {prior["score"]} -> {rec["score"]}; '
                  f'survivors {prior["survived"]} -> {rec["survived"]}')
        mod.write_measurement(root, rec)
        return 0
    check_cmd = [sys.executable, path, '--root', root, '--check']
    if kind == 'proof':
        check_cmd.append('--require-armed')
    checked = subprocess.run(check_cmd, capture_output=True, text=True)
    if checked.returncode:
        raise ValueError('--tighten refused a red ratchet\n' + checked.stdout + checked.stderr)
    bpath = os.path.join(root, mod.BASELINE)
    if not os.path.isfile(bpath):
        raise ValueError('--tighten requires an existing baseline')
    with open(bpath, encoding='utf-8') as f:
        doc = json.load(f)
    changes = []
    if kind in ('proof', 'seam'):
        section = 'counts' if kind == 'proof' else mod.SECTION
        prior = doc.get(section)
        if not isinstance(prior, dict):
            raise ValueError(f'--tighten requires an armed {kind} baseline')
        current = mod.scan(root)[0] if kind == 'proof' else mod.scan(root, mod.tracked_files(root))[0]
        for key, old in prior.items():
            if key in current and current[key] < old:
                changes.append(f'{key}: {old} -> {current[key]}')
                prior[key] = current[key]
    elif kind == 'guard':
        guards = mod.load_manifest(root)
        old_ids = set(doc.get('ids', []))
        ids = {g['id'] for g in guards}
        if not old_ids <= ids or len(guards) < doc.get('count', 0):
            raise ValueError('--tighten cannot lower a guard floor or remove a guard id')
        if ids != old_ids or len(guards) > doc.get('count', 0):
            changes.append(f'guards: {doc.get("count", 0)} -> {len(guards)}')
            doc.update(count=len(guards), ids=sorted(ids))
    clean(root)
    if changes:
        for change in changes:
            print(f'{kind}-guard: {change}')
        with open(bpath, 'w', encoding='utf-8', newline='\n') as f:
            json.dump(doc, f, indent=2, sort_keys=True)
            f.write('\n')
    else:
        print(f'{kind}-guard: no safe tightening available')
    return 0


def cli(kind, root, src=None):
    try:
        return tighten(kind, root, src)
    except (ValueError, OSError, subprocess.SubprocessError) as e:
        import sys
        print(f'{kind}-guard: {e}', file=sys.stderr)
        return 1
