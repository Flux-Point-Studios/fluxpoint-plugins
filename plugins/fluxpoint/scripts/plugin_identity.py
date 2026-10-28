"""Content identity for a compiled graph's local execution witness."""
import hashlib
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent.parent
FIELDS = {'root', 'version', 'attestSha256', 'launcherSha256', 'scriptsSha256'}


def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def current(root=None):
    root = Path(root or ROOT).resolve()
    version = json.loads((root / 'plugin.json').read_text(encoding='utf-8'))['version']
    scripts = root / 'scripts'
    digest = hashlib.sha256()
    # Bind the transitive witness helpers as well as the two entry points.
    for path in sorted(scripts.iterdir()):
        if path.is_file() and path.suffix in ('.py', '.sh'):
            digest.update(path.name.encode('utf-8') + b'\0' + path.read_bytes() + b'\0')
    return {'root': str(root), 'version': version, 'attestSha256': _hash(scripts / 'attest.py'),
            'launcherSha256': _hash(scripts / 'py.sh'), 'scriptsSha256': digest.hexdigest()}


def check_shape(pin):
    if not isinstance(pin, dict) or set(pin) != FIELDS:
        raise ValueError('PLUGIN-PIN: missing or malformed compiler witness identity; recompile with a pinned compiler')
    if not isinstance(pin['root'], str) or not pin['root'] or not isinstance(pin['version'], str) or not pin['version']:
        raise ValueError('PLUGIN-PIN: root and version must be nonempty strings')
    if any(not isinstance(pin[key], str) or not re.fullmatch('[0-9a-f]{64}', pin[key])
           for key in ('attestSha256', 'launcherSha256', 'scriptsSha256')):
        raise ValueError('PLUGIN-PIN: witness hashes must be SHA-256 hex digests')
    return pin


def verify(pin):
    check_shape(pin)
    try:
        actual = current(pin['root'])
    except (OSError, ValueError, KeyError) as exc:
        raise ValueError(f"PLUGIN-PIN: pinned installation is unavailable at {pin['root']}: {exc}") from exc
    if pin != actual:
        changed = ', '.join(key for key in sorted(FIELDS) if pin[key] != actual[key])
        raise ValueError('PLUGIN-PIN: pinned installation changed (' + changed + '); restore it or explicitly recompile and audit the graph')
    return actual


def from_script(path):
    text = Path(path).read_text(encoding='utf-8')
    match = re.search(r'^const PLUGIN = (.+);$', text, re.MULTILINE)
    if not match:
        raise ValueError('PLUGIN-PIN: launch script has no pinned compiler witness identity; recompile before launch')
    try:
        pin = json.loads(match.group(1))
    except ValueError as exc:
        raise ValueError('PLUGIN-PIN: malformed identity in launch script') from exc
    return verify(pin)
