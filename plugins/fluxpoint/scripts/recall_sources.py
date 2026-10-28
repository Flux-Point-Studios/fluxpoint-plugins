"""Read-only, bounded corpus discovery for the derived recall index."""
import datetime
import hashlib
import json
import os
import posixpath
from pathlib import Path
import re
import sys

MAX_FILE = 1024 * 1024


def config(root):
    path = Path(os.environ.get('FPL_RECALL_CONFIG') or
                Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'fluxpoint' / 'recall.json').expanduser()
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(value, dict): raise ValueError('expected an object')
        if 'refresh' in value and not isinstance(value['refresh'], bool):
            raise ValueError('refresh must be true or false')
        if not isinstance(value.get('corpora', []), list): raise ValueError('corpora must be a list')
        return value
    except (OSError, ValueError) as exc:
        raise SystemExit(f'recall: invalid config {path}: {exc}')


def corpus_roots(root):
    """Collect every ancestor memory folder; empty folders never shadow."""
    cfg = config(root)
    claude = Path(os.environ.get('CLAUDE_CONFIG_DIR', Path.home() / '.claude')).expanduser()
    base = Path(root).resolve()
    roots = []
    for path in (base, *base.parents):
        slug = re.sub(r'[^A-Za-z0-9]', '-', str(path))
        folder = claude / 'projects' / slug / 'memory'
        if folder.is_dir():
            roots.append({'path': str(folder), 'origin': 'local-' + hashlib.sha256(str(folder).encode()).hexdigest()[:10],
                          'format': 'claude-automemory', 'weight': 1.0})
    for row in cfg.get('corpora', []):
        if not isinstance(row, dict) or not isinstance(row.get('path'), str):
            raise SystemExit('recall: corpora entries need a path')
        folder = Path(os.path.expandvars(row['path'])).expanduser()
        if not folder.is_absolute(): folder = base / folder
        fmt = row.get('format', 'claude-automemory')
        if fmt not in ('claude-automemory', 'markdown', 'codex-memories'):
            raise SystemExit(f'recall: unsupported corpus format {fmt!r}')
        origin = str(row.get('origin') or 'peer-' + hashlib.sha256(str(folder).encode()).hexdigest()[:10])
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', origin):
            raise SystemExit('recall: corpus origin must use 1-80 letters, numbers, dots, underscores or dashes')
        weight = row.get('weight', 1.0)
        if isinstance(weight, bool) or not isinstance(weight, (int, float)) or not 0 < weight <= 2:
            raise SystemExit('recall: corpus weight must be greater than zero and at most two')
        aliases = row.get('pathAliases', {})
        if not isinstance(aliases, dict) or any(not isinstance(a, str) or not isinstance(b, str) for a, b in aliases.items()):
            raise SystemExit('recall: pathAliases must map source paths to local project paths')
        if not isinstance(row.get('allowUnscoped', False), bool):
            raise SystemExit('recall: allowUnscoped must be true or false')
        roots.append({'path': str(folder.resolve()), 'origin': origin, 'format': fmt, 'weight': weight,
                      'pathAliases': aliases, 'allowUnscoped': row.get('allowUnscoped', False)})
    codex = Path(os.environ.get('CODEX_HOME', Path.home() / '.codex')).expanduser() / 'memories'
    if codex.is_dir():
        roots.append({'path': str(codex.resolve()), 'origin': 'local-codex-' + hashlib.sha256(str(codex).encode()).hexdigest()[:10],
                      'format': 'codex-memories', 'weight': 1.0})
    seen, out = set(), []
    origins = {}
    for row in roots:
        key = os.path.normcase(row['path'])
        if key in seen: continue
        if row['origin'] in origins:
            raise SystemExit(f"recall: duplicate corpus origin {row['origin']!r}")
        seen.add(key); origins[row['origin']] = key; out.append(row)
    return out


def files(corpus):
    folder = Path(corpus['path'])
    if not folder.is_dir(): return []
    if corpus['format'] == 'codex-memories':
        candidates = [folder / name for name in ('MEMORY.md', 'memory_summary.md', 'raw_memories.md')]
        rollouts = folder / 'rollout_summaries'
        if rollouts.is_dir() and not rollouts.is_symlink(): candidates.extend(sorted(rollouts.glob('*.md')))
        return [p for p in candidates if p.is_file() and not p.is_symlink()]
    return [p for p in sorted(folder.glob('*.md')) if not p.name.startswith('.')
            and p.name != 'MEMORY.md' and not p.name.endswith(('.bak.md', '.backup.md'))
            and not p.is_symlink() and p.is_file()]


def fingerprints(root):
    out = {'config': config(root), 'corpora': []}
    for row in corpus_roots(root):
        items = []
        for p in files(row):
            try:
                st = p.stat(); items.append([p.relative_to(row['path']).as_posix(), st.st_size, st.st_mtime_ns])
            except OSError: items.append([p.name, 'unreadable'])
        out['corpora'].append(dict(row, files=items))
    return hashlib.sha256(json.dumps(out, sort_keys=True).encode()).hexdigest()


def _front(text):
    """Small metadata reader: scalar YAML fields used by memory writers.

    No arbitrary YAML construction or code; unknown structured fields are ignored.
    """
    if not text.startswith('---\n'): return {}, text
    pieces = text.split('\n---', 1)
    if len(pieces) != 2: return {}, text
    meta, parent = {}, ''
    for line in pieces[0].splitlines()[1:]:
        m = re.match(r'^(\s*)([\w]+):\s*(.*?)\s*$', line)
        if not m: continue
        indent, key, value = m.groups()
        value = value.strip('"\'')
        if not indent:
            parent = key if not value else ''
            if value: meta[key] = value
        elif parent == 'metadata' and value:
            meta[key] = value
    return meta, pieces[1].lstrip('\r\n')


def _instant(value):
    try:
        parsed = datetime.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if parsed.tzinfo is None: parsed = parsed.replace(tzinfo=datetime.timezone.utc)
        return parsed.timestamp()
    except (ValueError, TypeError, OverflowError): return float('-inf')


def _portable_path(path):
    value = posixpath.normpath(str(path).replace('\\', '/')).rstrip('/')
    return value.lower() if re.match(r'^[A-Za-z]:', value) else value


def _codex_scope(cwd, root, corpus):
    source = _portable_path(cwd)
    for old, local in sorted(corpus.get('pathAliases', {}).items(), key=lambda row: -len(row[0])):
        old = _portable_path(old)
        if source == old or source.startswith(old + '/'):
            source = _portable_path(local) + source[len(old):]
            break
    base = _portable_path(Path(root).resolve())
    return source == base or source.startswith(base + '/')


def _codex_passages(raw):
    headings, body = [], []
    for line in raw.splitlines():
        match = re.match(r'^(#{1,6})\s+(.+?)\s*#*$', line)
        if match:
            if any(part.strip() for part in body):
                yield ' / '.join(headings), '\n'.join(body).strip()
            level, title = len(match[1]), match[2]
            headings = headings[:level - 1] + [title]
            body = []
        else:
            body.append(line)
    if any(part.strip() for part in body):
        yield ' / '.join(headings), '\n'.join(body).strip()


def _codex_nodes(root, corpus, p, raw, when):
    meta = dict(re.findall(r'^(thread_id|updated_at|cwd|rollout_path):\s*(.*?)\s*$', raw, re.M))
    relative = p.relative_to(corpus['path']).as_posix()
    filekey = corpus['origin'] + '/' + relative
    rollout = relative.startswith('rollout_summaries/')
    if rollout:
        if not meta.get('cwd') or not _codex_scope(meta['cwd'], root, corpus): return {}
        raw = re.sub(r'^(?:thread_id|updated_at|cwd|rollout_path):.*\n?', '', raw, flags=re.M).strip()
        passages = [(next((line.lstrip('# ') for line in raw.splitlines() if line.startswith('#')), p.stem), raw)]
    else:
        passages = list(_codex_passages(raw))
    out = {}
    project = re.compile(r'\b(?:project|repo|repository):\s*' + re.escape(Path(root).resolve().name) + r'(?=\s|/|$)', re.I)
    for num, (name, passage) in enumerate(passages, 1):
        if not rollout and not corpus.get('allowUnscoped'):
            if meta.get('cwd'):
                if not _codex_scope(meta['cwd'], root, corpus): continue
            elif not project.search(name):
                continue
        # Keep all text through bounded passages; a large MEMORY section cannot
        # crowd out the rest of the corpus with one giant ranked document.
        for part, start in enumerate(range(0, len(passage), 1200), 1):
            nid = f'note:{filekey}#{num}.{part}'
            out[nid] = {'kind': 'note', 'text': passage[start:start + 1200] + ' || ' + name,
                        'file': filekey, 'name': name or p.stem, 'status': 'note',
                        'when': meta.get('updated_at') or when, 'origin': corpus['origin'],
                        'session': meta.get('thread_id', ''), 'weight': corpus['weight']}
    return out


def notes(root):
    nodes, diagnostics = {}, []
    names = {}
    for corpus in corpus_roots(root):
        for p in files(corpus):
            try:
                if p.stat().st_size > MAX_FILE: raise ValueError('larger than 1 MiB')
                with p.open(encoding='utf-8') as fh: raw = fh.read(MAX_FILE + 1)
                if len(raw.encode('utf-8')) > MAX_FILE: raise ValueError('larger than 1 MiB')
                meta, body = _front(raw)
                when = meta.get('modified') or datetime.datetime.fromtimestamp(p.stat().st_mtime, datetime.timezone.utc).isoformat()
            except (OSError, UnicodeError, ValueError) as exc:
                diagnostics.append(f'{p.name} skipped: {exc}'); continue
            if corpus['format'] == 'codex-memories':
                nodes.update(_codex_nodes(root, corpus, p, raw, when))
                continue
            name = meta.get('name') or p.stem
            filekey = corpus['origin'] + '/' + p.name
            card = ' || '.join(x for x in (name, meta.get('description')) if x)
            passages = []
            for para in re.split(r'\n\s*\n', body):
                para = para.strip()
                for start in range(0, len(para), 1200):
                    if para[start:start + 1200].strip(): passages.append(para[start:start + 1200])
            if not passages: passages = [card]
            status = 'superseded' if meta.get('supersededBy') or meta.get('status') == 'superseded' else 'note'
            for num, passage in enumerate(passages, 1):
                nid = f'note:{filekey}#{num}'
                nodes[nid] = {'kind': 'note', 'text': passage + ' || ' + card, 'file': filekey,
                              'name': name, 'status': status, 'when': when,
                              'origin': corpus['origin'], 'session': meta.get('originSessionId', ''),
                              'weight': corpus['weight']}
                names.setdefault(name, []).append(nid)
    # Explicit replacement wins; duplicate names across roots serve the newest.
    for ids in names.values():
        newest = max((_instant(nodes[n]['when']), nodes[n]['file']) for n in ids)
        for nid in ids:
            if (_instant(nodes[nid]['when']), nodes[nid]['file']) < newest:
                nodes[nid]['status'] = 'superseded'
    return nodes, diagnostics
