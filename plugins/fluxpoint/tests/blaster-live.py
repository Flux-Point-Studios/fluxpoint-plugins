#!/usr/bin/env python3
"""Real Lean/Z3 discover/repair/replay/pin smoke; pass --backend CHECKOUT.

Requires pinned tools on PATH. Builds only a temporary Lean fixture, not a
Cardano validator. Reuses compiled backend outputs from the same revision.
"""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
REV = 'a16614e5e44a693664043552f259235f15f1c884'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend', type=Path, required=True)
    backend = parser.parse_args().backend.resolve()
    actual = subprocess.check_output(['git', '-C', str(backend), 'rev-parse', 'HEAD'], text=True).strip()
    if actual != REV:
        raise ValueError('live fixture requires backend revision ' + REV)
    with tempfile.TemporaryDirectory(prefix='fluxpoint-blaster-live-') as tmp:
        root = Path(tmp)

        def command(argv, ok=True):
            result = subprocess.run(argv, cwd=root, text=True, capture_output=True, timeout=600)
            if (result.returncode == 0) != ok:
                raise AssertionError(' '.join(map(str, argv)) + '\n' + result.stdout + result.stderr)
            return result.stdout + result.stderr

        def git(*argv):
            return command(['git', *argv])

        def adapter(*argv, ok=True, tool='blaster.py'):
            return command([sys.executable, str(SCRIPTS / tool), '--root', str(root), *argv], ok)

        git('init', '-q')
        git('config', 'user.name', 'Blaster Live Test')
        git('config', 'user.email', 'blaster-live@example.invalid')
        (root / '.gitignore').write_text('.lake/\n.claude/\n')
        (root / 'lean-toolchain').write_text('leanprover/lean4:v4.24.0\n')
        (root / 'lakefile.lean').write_text('import Lake\nopen Lake DSL\npackage Probe\n'
            'require Blaster from git "https://github.com/input-output-hk/Lean-blaster" @ "' + REV + '"\n'
            '@[default_target] lean_lib Model\n')
        packages = root / '.lake/packages'
        packages.mkdir(parents=True)
        command(['git', 'clone', '-q', '--shared', str(backend), str(packages / 'Blaster')])
        if (backend / '.lake/build').exists():
            shutil.copytree(backend / '.lake/build', packages / 'Blaster/.lake/build')
        (root / 'lake-manifest.json').write_text(json.dumps({'version': '1.1.0', 'packagesDir': '.lake/packages',
            'packages': [{'type': 'git', 'scope': '', 'subDir': None, 'name': 'Blaster',
                          'manifestFile': 'lake-manifest.json', 'inputRev': REV, 'inherited': False,
                          'configFile': 'lakefile.lean', 'url': 'https://github.com/input-output-hk/Lean-blaster', 'rev': REV}],
            'name': 'Probe', 'lakeDir': '.lake'}))
        model = ('import Blaster\nnamespace Model\n'
                 'def step (n : Nat) : Nat := if n < 4 then n + 1 else n\n'
                 'def safe : Prop := ∀ n : Nat, n >= 1 → step n > n\n'
                 'def feasible : Prop := ∃ n : Nat, n >= 1 ∧ step n > n\n'
                 'set_option warn.sorry false\n'
                 'theorem solverLemma (n : Nat) : n + 1 > n := by blaster\n'
                 'end Model\n')
        (root / 'Model.lean').write_text(model)
        manifest = json.loads((SCRIPTS.parent / 'templates/blaster/manifest.json').read_text())
        manifest['checks'][0].update(module='Model', declaration='Model.safe', witness='Model.feasible',
                                     scope='Every natural-number step strictly increases; the input domain is inhabited')
        manifest['checks'].append(dict(manifest['checks'][0], id='trust-audit', declaration='Model.solverLemma',
                                       scope='Imported solver axiom remains visible even with warnings suppressed'))
        manifest['timeoutSeconds'] = 5
        config = root / '.fluxpoint-blaster.json'
        config.write_text(json.dumps(manifest, indent=2) + '\n')
        git('add', '.')
        git('commit', '-qm', 'live boundary defect')
        print(adapter('--run', ok=False), end='')
        receipt = json.loads((root / '.claude/fluxpoint/blaster/latest.json').read_text())
        assert 'Blaster.Tactic.blasterProven' in receipt['checks'][1]['axioms'], receipt
        row = json.loads((root / '.fluxpoint-cex.jsonl').read_text().splitlines()[-1])
        assert row['bindings'] and row['bindings'][0]['value'] >= 4, row
        print('LIVE counterexample:', row['bindings'])
        adapter('--check', tool='cex.py', ok=False)
        manifest['regressions'] = [{'cexId': row['cexId'], 'check': 'safety', 'bindings': row['bindings']}]
        config.write_text(json.dumps(manifest, indent=2) + '\n')
        # Change implementation only; the quantified property stays frozen.
        (root / 'Model.lean').write_text(model.replace('if n < 4 then n + 1 else n', 'n + 1'))
        print(adapter('--run'), end='')
        print(adapter('--pin', row['cexId'], '--file', str(config), '--test-name', row['cexId'], tool='cex.py'), end='')
        print(adapter('--check'), end='')
        print(adapter('--check', tool='cex.py'), end='')
        (root / 'Model.lean').write_text(model)
        adapter('--check', ok=False)
        adapter('--run', ok=False)
        adapter('--check', tool='cex.py', ok=False)
        print('LIVE PASS: falsification, repaired property, exact replay, pin and drift refusal')
    return 0


if __name__ == '__main__':
    sys.exit(main())
