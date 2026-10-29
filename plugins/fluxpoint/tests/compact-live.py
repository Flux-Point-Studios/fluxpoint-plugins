#!/usr/bin/env python3
"""Real Compact/native proof smoke. Toolchains are explicit, external and pinned.

See references/compact.md for reproducible preparation. This generates synthetic
proofs locally; it never submits a transaction or contacts a model provider.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import urllib.request

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN / 'scripts'))
import compact_native as compact

SOURCE_REV = 'd1e1548682d33a430c0f42ac53f88f01a652fefe'
LEDGER_REV = 'b5d3e965a18e4cac17fac4f38446fc55c524439a'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for field in ('compiler-dir', 'params', 'node-modules', 'native-verifier', 'native-source'):
        parser.add_argument('--' + field, required=True, type=Path)
    parser.add_argument('--fetch-params', action='store_true', help='download missing public SRS files, refusing unpinned bytes')
    args = parser.parse_args()
    try:
        source = args.native_source.resolve()
        actual = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
        if actual != SOURCE_REV: raise ValueError('native fixture source must be pinned Orynq commit ' + SOURCE_REV)
        subprocess.run(['git', '-C', str(source), 'diff', '--exit-code', '--', 'packages/midnight-prover/native', 'tools/compactc'], check=True, stdout=subprocess.DEVNULL)
        cargo = (source / 'packages/midnight-prover/native/Cargo.toml').read_text()
        if LEDGER_REV not in cargo or 'features = ["proof-verifying"]' not in cargo or 'mock-verify' in cargo:
            raise ValueError('native fixture must build the real pinned ledger proof verifier')
        sha = lambda path: hashlib.sha256(Path(path).read_bytes()).hexdigest()
        compiler = args.compiler_dir.resolve()
        official = {}
        for line in (source / 'tools/compactc/compactc.sha256').read_text().splitlines():
            fields = line.split()
            if len(fields) == 2 and fields[1].startswith('x86_64-unknown-linux-musl/'):
                official[fields[1].split('/', 1)[1]] = fields[0]
        if not official: raise ValueError('no official Linux x86_64 compiler pins')
        for name, expected in official.items():
            if sha(compiler / name) != expected: raise ValueError('compiler differs from official release pin: ' + name)
        modules = args.node_modules.resolve()
        for package, expected in [('compact-runtime', '0.16.0'), ('ledger-v8', '8.1.3'), ('zkir-v2', '2.1.1')]:
            if json.loads((modules / '@midnight-ntwrk' / package / 'package.json').read_text())['version'] != expected:
                raise ValueError('fixture npm version differs: ' + package)
        pins = json.loads((Path(__file__).parent / 'fixtures/compact-native/parameters.json').read_text())
        params = args.params.resolve(); params.mkdir(parents=True, exist_ok=True)
        for name, expected in pins.items():
            path = params / name
            if not path.is_file() and args.fetch_params:
                with urllib.request.urlopen('https://srs.midnight.network/' + name, timeout=60) as response:
                    data = response.read(64 * 1024 * 1024 + 1)
                if len(data) > 64 * 1024 * 1024 or compact.digest(data) != expected: raise ValueError('public proving parameter pin mismatch: ' + name)
                path.write_bytes(data)
            if sha(path) != expected: raise ValueError('missing/changed pinned proving parameter: ' + name)
        node = shutil.which('node')
        if not node: raise ValueError('Node.js is required for real ledger/prover controls')
        native = args.native_verifier.resolve()
        inspector = PLUGIN / 'scripts/compact-native-inspect.mjs'
        inspection_files = {str(path): sha(path) for path in modules.rglob('*') if path.is_file()}
        inspection_files.update({node: sha(node), str(inspector): sha(inspector)})
        with tempfile.TemporaryDirectory(prefix='fpl-compact-live-') as directory:
            root = Path(directory)
            shutil.copy2(Path(__file__).parent / 'fixtures/compact-native/contract.compact', root / 'contract.compact')
            cfg = {'version': 1, 'compiler': {'argv': [str(compiler / 'compactc')], 'files': {str(compiler / name): expected for name, expected in official.items()}},
                   'compilerVersion': '0.31.1', 'compilerLedger': 'ledger-8.0.2', 'runtimeVersion': '0.16.0',
                   'native': {'argv': [str(native)], 'files': {str(native): sha(native)}},
                   'inspector': {'argv': [node, str(inspector), '--modules', str(modules)], 'files': inspection_files},
                   'params': {str(params / name): expected for name, expected in pins.items()},
                   'projects': [{'source': 'contract.compact', 'circuits': ['check'],
                                 'driver': [node, str(Path(__file__).parent / 'fixtures/compact-native/driver.mjs'), '{artifacts}'],
                                 'cases': {'genuine': True, 'corrupted': False, 'wrong-secret': False}, 'timeoutSeconds': 300}]}
            (root / compact.CONFIG).write_text(json.dumps(cfg, indent=2) + '\n')
            (root / '.fluxpoint-mutation.json').write_text('{"version":1,"tool":"compact","failWhenStale":true}\n')
            os.environ['FPL_COMPACT_NODE_MODULES'] = str(modules)
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            subprocess.run(['git', '-C', str(root), 'add', '.'], check=True)
            loaded = compact.load(root)
            first = compact.measure(root, loaded, require_semantic=True)
            second = compact.measure(root, loaded, require_semantic=True)
            if compact.compare(first, second): raise ValueError('real native artifacts or costs were nondeterministic')
            subprocess.run(['git', '-C', str(root), 'add', '.'], check=True)
            subprocess.run(['git', '-C', str(root), '-c', 'user.name=Compact fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'Synthetic pinned native proof fixture'], check=True)
            record, findings = compact.mutation_measure(root, [])
            if findings: raise ValueError('; '.join(findings))
            if record['killed'] != 2 or record['survived'] or record['unviable'] != 1 or record['equivalent'] != 1:
                raise ValueError('native semantic mutants did not behave as expected: ' + json.dumps(record))
            print(json.dumps({'nativeSource': SOURCE_REV, 'ledger': LEDGER_REV, 'costs': first['costs'], 'mutations': record, 'controls': 'genuine verified; corrupted proof invalid-proof; wrong-secret refused; source mutants noticed'}, sort_keys=True))
        return 0
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        print('compact-live: ' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
