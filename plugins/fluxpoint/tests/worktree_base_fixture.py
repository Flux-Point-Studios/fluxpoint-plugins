"""Read the emitted preamble itself for Git and native-runtime controls."""
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location('base_fixture_compiler', SCRIPTS / 'compile-graph.py')
cg = importlib.util.module_from_spec(spec); spec.loader.exec_module(cg)
CONTRACTS = cg.load_contracts(str(SCRIPTS.parent / 'contracts'))


def graph(nodes=None):
    return {'version': 1, 'campaign': 'Exact worktree base', 'treeGuard': False, 'budget': {'maxNodes': 20},
            'nodes': nodes or [{'id': 'worker', 'contract': 'DesignV1', 'prompt': 'Inspect the base.', 'isolation': 'worktree'}]}


def preamble(base):
    source = cg.emit(graph(), CONTRACTS)
    match = re.search(r'^function basePreamble\(\) \{\n.*?^\}', source, re.M | re.S)
    if not match: raise ValueError('compiler did not emit its worktree preamble')
    program = 'const BASE=' + json.dumps(base) + ';\n' + match[0] + '\nprocess.stdout.write(basePreamble());\n'
    completed = subprocess.run(['node', '-'], input=program, capture_output=True, text=True, encoding='utf-8', check=True)
    return completed.stdout, match[0]


def commands(base):
    text, _ = preamble(base)
    rows = re.findall(r'\b[Rr]un: (git [^\n]+)', text)
    if len(rows) != 6: raise ValueError('unexpected emitted preamble command inventory: ' + repr(rows))
    return rows
