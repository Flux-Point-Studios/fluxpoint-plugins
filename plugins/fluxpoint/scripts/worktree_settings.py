"""Advisory project/local worktree-base setting; native checks remain authoritative."""
import json
from pathlib import Path

CAVEAT = ('Project/local settings only. User or managed settings, --settings overrides and an alternate '
          'CLAUDE_CONFIG_DIR may differ; this is not effective runtime policy. The in-node exact-base check decides acceptance.')


def report(root):
    root = Path(root).resolve()
    result = {'state': 'fresh/absent', 'configured': None, 'source': None, 'advisory': True, 'scope': 'project/local', 'caveat': CAVEAT}
    for relative in ('.claude/settings.json', '.claude/settings.local.json'):
        path = root / relative
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(data, dict): raise ValueError('settings must be an object')
            if 'worktree' not in data: continue
            if not isinstance(data['worktree'], dict): raise ValueError('worktree settings must be an object')
            if 'baseRef' not in data['worktree']: continue
            value = data['worktree']['baseRef']
            if value not in ('head', 'fresh'): raise ValueError('unrecognized worktree.baseRef')
            result.update(state='head' if value == 'head' else 'fresh/absent', configured=value, source=relative)
        except FileNotFoundError:
            if path.is_symlink():
                result.update(state='unknown', source=relative, detail='settings link is unreadable')
                return result
        except (OSError, ValueError, TypeError) as exc:
            result.update(state='unknown', source=relative, detail=str(exc))
            return result
    return result


def describe(value):
    return 'worktree baseRef: ' + value['state'] + (' (' + value['source'] + ')' if value.get('source') else '') + '; ' + value['caveat']
