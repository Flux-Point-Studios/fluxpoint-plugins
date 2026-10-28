"""GitHub evidence read from its source, never from the writable attest log.

Only the guarded manifest can select identities, coverage and the packet base.
API failures and incomplete/malformed listings are undecided, never green.
"""
import ast
import base64
import hashlib
import json
import os
import re
import subprocess
import time
from urllib.parse import quote, urlsplit
from urllib.request import urlopen

SHA = re.compile(r"[0-9a-f]{40}\Z")
GATE = re.compile(r"[a-z][a-z0-9-]*\Z")
IMPLICIT = {'pyproject.toml', 'setup.cfg', 'pytest.ini', '.pytest.ini', 'pytest.toml', '.pytest.toml', 'tox.ini', 'conftest.py'}


class PolicyError(ValueError):
    pass


class TargetMoved(ValueError):
    pass


def _strings(value):
    return (isinstance(value, list) and bool(value) and
            all(isinstance(x, str) and x.strip() == x and x for x in value) and
            len(set(value)) == len(value))


def problems(ci, gates=None):
    if not isinstance(ci, dict):
        return ['ci must be an object']
    out = []
    unknown = set(ci) - {'forge', 'repository', 'contexts', 'witness', 'covers', 'packetSha', 'config'}
    out.extend('ci: unknown field ' + str(k) for k in sorted(unknown))
    if not isinstance(ci.get('repository'), str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]+', ci['repository']):
        out.append('ci.repository must pin the GitHub owner/repository')
    if ci.get('forge') != 'github':
        out.append('ci.forge must be github')
    if 'contexts' in ci and not _strings(ci['contexts']):
        out.append('ci.contexts must be a non-empty unique list of names')
    w = ci.get('witness')
    if not isinstance(w, dict) or set(w) != {'statusCreators', 'checkRunApps'}:
        out.append('ci.witness requires statusCreators and checkRunApps identity pins')
    else:
        creators, apps = w['statusCreators'], w['checkRunApps']
        if (not isinstance(creators, list) or any(not isinstance(x, str) or
                not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})', x) for x in creators)
                or len({x.lower() for x in creators}) != len(creators)):
            out.append('ci.witness.statusCreators must be unique GitHub logins')
        if (not isinstance(apps, list) or any(type(x) is not int or x <= 0 for x in apps)
                or len(set(apps)) != len(apps)):
            out.append('ci.witness.checkRunApps must be unique positive app IDs')
        if not creators and not apps:
            out.append('ci.witness must pin at least one independent identity')
    covers = ci.get('covers', {})
    if not isinstance(covers, dict):
        out.append('ci.covers must be an object')
    else:
        if covers and not SHA.fullmatch(str(ci.get('packetSha', ''))):
            out.append('ci.packetSha must pin a full packet commit before reuse is armed')
        for gate, cover in covers.items():
            if not GATE.fullmatch(gate) or gate == 'ci' or (gates is not None and gate not in gates):
                out.append('ci.covers names an undeclared local gate: ' + gate)
            if (not isinstance(cover, dict) or set(cover) != {'contexts', 'config'} or
                    not _strings(cover.get('contexts')) or not _strings(cover.get('config'))):
                out.append('ci.covers.' + gate + ' requires unique contexts and config lists')
                continue
            for path in cover['config']:
                if (path.startswith('/') or '\\' in path or any(p in ('', '.', '..') for p in path.split('/'))
                        or any(ord(c) < 32 for c in path)):
                    out.append('ci.covers.' + gate + ': config must name repository-relative files')
    if 'config' in ci:
        config = ci['config']
        if not _strings(config) or any(path.startswith('/') or '\\' in path
                or any(p in ('', '.', '..') for p in path.split('/'))
                or any(ord(c) < 32 for c in path) for path in config):
            out.append('ci.config must be a unique list of repository-relative files')
        if not SHA.fullmatch(str(ci.get('packetSha', ''))):
            out.append('ci.packetSha must pin a full packet commit for prove:ci')
    if 'packetSha' in ci and not SHA.fullmatch(str(ci['packetSha'])):
        out.append('ci.packetSha must be a full lowercase commit SHA')
    return out


def module_files(module, tracked):
    path = module.replace('.', '/')
    candidates = {path + '.py', path + '/__main__.py', path + '/__init__.py'}
    # Python can run from a component working directory, so include every
    # tracked suffix match rather than guessing at PYTHONPATH or cd semantics.
    found = {p for p in tracked if any(p == c or p.endswith('/' + c) for c in candidates)}
    for p in list(found):
        parts = p.split('/')[:-1]
        found.update('/'.join(parts[:i]) + '/__init__.py' for i in range(1, len(parts) + 1)
                     if '/'.join(parts[:i]) + '/__init__.py' in tracked)
    return found


def uncomment(content):
    """Drop shell/YAML comments without erasing quoted # characters."""
    result, quote_, escaped, i = [], None, False, 0
    while i < len(content):
        c = content[i]
        if escaped:
            escaped = False
        elif c == "\\" and quote_ != "'":
            escaped = True
        elif quote_:
            if c == quote_:
                quote_ = None
        elif c in ("'", '"'):
            quote_ = c
        elif c == '#' and (i == 0 or content[i - 1].isspace()):
            end = content.find('\n', i)
            i = len(content) if end < 0 else end
            continue
        result.append(c)
        i += 1
    return ''.join(result)


def coverage_text(path, content):
    if path.endswith('.py'):
        try:
            return '\n'.join(node.value for node in ast.walk(ast.parse(content))
                             if isinstance(node, ast.Constant) and isinstance(node.value, str))
        except SyntaxError as exc:
            raise ValueError('cannot scan Python CI dependency: ' + path) from exc
    return uncomment(content)


def executable_references(path, content, tracked):
    scanned = coverage_text(path, content)
    tree = ast.parse(content) if path.endswith('.py') else None
    tokens = set(re.findall(r'[A-Za-z0-9_@+./-]+', scanned))
    normalized = {re.sub(r'^(?:\.{1,2}/)+', '', token) for token in tokens}
    invoked = set()
    for candidate in tracked:
        parts = candidate.split('/')
        if {'/'.join(parts[i:]) for i in range(len(parts))} & normalized:
            invoked.add(candidate)
    if re.search(r'\$\{?\w+\}?[\"\']?\s+[^\n;&|]*?-m\s', scanned):
        raise ValueError('dynamic interpreter cannot establish coverage: ' + path)
    interpreter = r'\b(?:python(?:3(?:\.\d+)?)?|pypy3?|py)(?:\.exe)?\s+'
    command = interpreter + r'[^\n;&|]*?-m\s+'
    if not path.endswith('.py') and re.search(interpreter + r'[^\n;&|]*(?:-c\s|<<|<\s)', scanned):
        raise ValueError('inline Python execution cannot establish coverage: ' + path)
    for match in re.finditer(command, scanned):
        operand = re.match(r"[\"']?([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)[\"']?(?=\s|$|[;&|])", scanned[match.end():])
        if not operand:
            raise ValueError('dynamic Python module cannot establish coverage: ' + path)
        invoked.update(module_files(operand.group(1), tracked))
    if path.endswith('.py'):
        package = path.split('/')[:-1]
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    invoked.update(module_files(alias.name, tracked))
            elif isinstance(node, ast.ImportFrom):
                prefix = package[:max(0, len(package) - node.level + 1)] if node.level else []
                module = '.'.join(prefix + ([node.module] if node.module else []))
                if module:
                    invoked.update(module_files(module, tracked))
                for alias in node.names:
                    if alias.name != '*':
                        invoked.update(module_files('.'.join(filter(None, (module, alias.name))), tracked))
            elif isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name) and func.id in ('getattr', 'hasattr'):
                    dangerous = {'import_module', '__import__', 'exec_module', 'run_module', 'run_path',
                                 'spec_from_file_location', 'load_module', 'exec', 'eval', 'compile'}
                    attribute = node.args[1] if len(node.args) > 1 else None
                    if not isinstance(attribute, ast.Constant) or not isinstance(attribute.value, str) or attribute.value in dangerous:
                        raise ValueError('dynamic Python execution cannot establish coverage: ' + path)
                if (isinstance(func, ast.Name) and func.id in ('__import__', 'eval', 'exec', 'compile', 'globals', 'locals') or
                        isinstance(func, ast.Attribute) and func.attr in ('import_module', 'spec_from_file_location', 'exec_module', 'run_module', 'run_path')):
                    raise ValueError('dynamic Python execution cannot establish coverage: ' + path)
    return invoked


class GitHub:
    def __init__(self, root, api=None, deadline=None, repository=None):
        self.root, self._api = root, api
        self.repository = repository
        self.deadline = deadline if deadline is not None else time.monotonic() + 500

    def api(self, path):
        if self._api:
            return self._api(path)
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError('forge observation deadline expired')
        if path.startswith('repos/{owner}/{repo}/'):
            if not self.repository:
                raise ValueError('CI repository must be pinned in the guarded manifest')
            path = path.replace('{owner}/{repo}', self.repository, 1)
        return self.request(path, min(30, max(0.001, self.deadline - time.monotonic())))

    def request(self, path, timeout):
        try:
            # Neither GH_REPO nor GH_HOST may redirect proof to a different forge.
            p = subprocess.run(['gh', 'api', path, '--hostname', 'github.com'], cwd=self.root,
                               capture_output=True, text=True, encoding='utf-8', timeout=timeout)
        except subprocess.TimeoutExpired as e:
            raise RuntimeError('GitHub observation timed out') from e
        if p.returncode:
            raise RuntimeError(p.stderr.strip() or 'GitHub API failed')
        return json.loads(p.stdout)

    def _identity_command(self, argv, env):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError('forge identity observation deadline expired')
        try:
            return subprocess.run(argv, cwd=self.root, env=env, capture_output=True,
                                  text=True, encoding='utf-8', timeout=min(30, remaining))
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError('GitHub identity observation timed out') from exc

    def stored_logins(self):
        # Removing both token variables makes this an inventory of gh's stored
        # accounts, including inactive accounts hidden by an environment token.
        env = {key: value for key, value in os.environ.items()
               if key not in ('GH_TOKEN', 'GITHUB_TOKEN')}
        result = self._identity_command(
            ['gh', 'auth', 'status', '--json', 'hosts', '--jq',
             '{hosts: {"github.com": [.hosts["github.com"][]? | {login, state}]}}'], env)
        try:
            inventory = json.loads(result.stdout)
        except (TypeError, ValueError) as exc:
            raise RuntimeError('cannot inventory stored gh accounts; JSON auth status support is required') from exc
        hosts = inventory.get('hosts') if isinstance(inventory, dict) else None
        if not isinstance(hosts, dict):
            raise PolicyError('malformed stored gh account inventory')
        accounts = hosts.get('github.com', [])
        if (not isinstance(accounts, list) or any(not isinstance(row, dict)
                or not isinstance(row.get('login'), str) or not row['login'] for row in accounts)):
            raise PolicyError('malformed stored gh account login')
        if (result.returncode and not accounts
                and 'not logged into any GitHub hosts' not in (result.stderr or '')):
            raise RuntimeError('stored gh account inventory failed')
        # gh may exit nonzero when there are no stored accounts, or an expired
        # account. The valid inventory still names every available stored login.
        return {row['login'] for row in accounts}

    def identity(self, ci):
        creators = {login.lower() for login in ci['witness']['statusCreators']}
        if not creators:
            return
        got = self.api('user')
        login = got.get('login') if isinstance(got, dict) else None
        if not isinstance(login, str) or not login:
            raise PolicyError('cannot establish the harness token login')
        if login.lower() in creators:
            raise PolicyError('ci.witness.statusCreators pins the harness own gh login: ' + login)
        # Injected API fixtures are the complete observation boundary in tests.
        if self._api is not None:
            return
        available = {login.lower() for login in self.stored_logins()}
        if available & creators:
            raise PolicyError('ci.witness.statusCreators pins a gh account available on this host: '
                              + ', '.join(sorted(available & creators)))
        if os.environ.get('GH_TOKEN') and os.environ.get('GITHUB_TOKEN'):
            env = dict(os.environ)
            env.pop('GH_TOKEN', None)
            result = self._identity_command(['gh', 'api', 'user', '--hostname', 'github.com'], env)
            if result.returncode:
                raise RuntimeError('cannot establish the shadowed GITHUB_TOKEN login')
            try:
                got = json.loads(result.stdout)
            except (TypeError, ValueError) as exc:
                raise RuntimeError('cannot read the shadowed GITHUB_TOKEN login') from exc
            login = got.get('login') if isinstance(got, dict) else None
            if not isinstance(login, str) or not login:
                raise PolicyError('cannot establish the shadowed GITHUB_TOKEN login')
            if login.lower() in creators:
                raise PolicyError('ci.witness.statusCreators pins the shadowed GITHUB_TOKEN login: ' + login)

    def target(self, target):
        if re.fullmatch(r'pr:[1-9][0-9]*', str(target)):
            got = self.api('repos/{owner}/{repo}/pulls/' + target[3:])
            sha = got.get('head', {}).get('sha') if isinstance(got, dict) and isinstance(got.get('head'), dict) else None
        elif isinstance(target, str) and target.startswith('ref:') and re.fullmatch(r'[A-Za-z0-9._/-]{1,200}', target[4:]):
            ref = target[4:]
            if ref.startswith(('/', '-')) or '..' in ref or re.fullmatch(r'[0-9a-fA-F]{7,40}', ref):
                raise ValueError('ref must be a branch or tag, never a caller-selected SHA')
            try:
                got = self.api('repos/{owner}/{repo}/git/ref/heads/' + quote(ref, safe=''))
                expected = 'refs/heads/' + ref
            except RuntimeError:
                got = self.api('repos/{owner}/{repo}/git/ref/tags/' + quote(ref, safe=''))
                expected = 'refs/tags/' + ref
            if not isinstance(got, dict) or got.get('ref') != expected or not isinstance(got.get('object'), dict):
                raise ValueError('forge did not resolve the exact named ref')
            obj = got['object']
            for _ in range(5):
                if obj.get('type') != 'tag':
                    break
                if not SHA.fullmatch(str(obj.get('sha', ''))):
                    raise ValueError('malformed annotated tag')
                tag = self.api('repos/{owner}/{repo}/git/tags/' + obj['sha'])
                obj = tag.get('object', {}) if isinstance(tag, dict) else {}
            sha = obj.get('sha') if obj.get('type') == 'commit' else None
        else:
            raise ValueError('forge pointer must name --pr or --ref')
        if not isinstance(sha, str) or not SHA.fullmatch(sha):
            raise ValueError('forge returned no full commit SHA for ' + target)
        return sha

    def listing(self, path, key=None):
        items, expected = [], None
        for page in range(1, 51):
            got = self.api(path + ('&' if '?' in path else '?') + 'per_page=100&page=' + str(page))
            if key:
                if (not isinstance(got, dict) or type(got.get('total_count')) is not int or
                        got['total_count'] < 0 or not isinstance(got.get(key), list)):
                    raise ValueError('malformed GitHub listing: ' + path)
                if expected is not None and expected != got['total_count']:
                    raise ValueError('GitHub listing changed during pagination')
                expected, batch = got['total_count'], got[key]
            else:
                if not isinstance(got, list):
                    raise ValueError('malformed GitHub status listing')
                batch = got
            if len(batch) > 100 or any(not isinstance(x, dict) for x in batch):
                raise ValueError('malformed GitHub listing rows')
            items.extend(batch)
            if len(batch) < 100:
                if expected is not None and len(items) != expected:
                    raise ValueError('incomplete GitHub listing')
                return items
            if expected is not None and len(items) == expected:
                return items
            if expected is not None and len(items) > expected:
                raise ValueError('inconsistent GitHub listing count')
        raise ValueError('GitHub listing exceeds bounded pagination; no verdict')

    def pipeline(self, row, sha):
        """Inspect launch metadata when a status links a Woodpecker pipeline.

        GitHub authenticates who posted a status, not a CI server's restart
        inputs. An unreadable provider record cannot substantiate a green.
        """
        url = row.get('target_url') or ''
        parsed = urlsplit(url)
        match = re.fullmatch(r'/repos/([1-9][0-9]*)/pipeline/([1-9][0-9]*)(?:/[1-9][0-9]*)?/?', parsed.path)
        if not match:
            if str(row.get('context', '')).lower().startswith('ci/woodpecker/'):
                raise ValueError('Woodpecker pipeline status has no observable pipeline URL')
            return None
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment):
            raise ValueError('invalid Woodpecker pipeline URL')
        endpoint = 'https://' + parsed.netloc + '/api/repos/' + match[1] + '/pipelines/' + match[2]
        if self._api:
            pipeline = self._api('woodpecker:' + endpoint)
        else:
            remaining = min(30, self.deadline - time.monotonic())
            if remaining <= 0:
                raise ValueError('pipeline observation deadline expired')
            try:
                # No GitHub or CI credential is forwarded to the status URL.
                with urlopen(endpoint, timeout=remaining) as response:
                    data = response.read(1048577)
                if len(data) > 1048576:
                    raise ValueError('pipeline response exceeds the observation bound')
                pipeline = json.loads(data.decode('utf-8'))
            except (OSError, ValueError) as exc:
                raise ValueError('cannot establish pipeline launch provenance: ' + str(exc)) from exc
        if (not isinstance(pipeline, dict) or type(pipeline.get('parent')) is not int
                or pipeline['parent'] != 0 or pipeline.get('event') not in ('push', 'pull_request')
                or pipeline.get('commit') != sha or pipeline.get('number') != int(match[2])
                or pipeline.get('status') != 'success'):
            raise ValueError('pipeline is restarted, manual, incomplete or for another commit')
        return {'provider': 'woodpecker', 'url': endpoint, 'number': pipeline['number'],
                'parent': 0, 'event': pipeline['event'], 'commit': sha}

    def contexts(self, sha, ci):
        if not SHA.fullmatch(sha):
            raise ValueError('CI requires a full commit SHA')
        creators = {x.lower() for x in ci['witness']['statusCreators']}
        apps = set(ci['witness']['checkRunApps'])
        rows = self.listing('repos/{owner}/{repo}/commits/' + sha + '/statuses')
        checks = self.listing('repos/{owner}/{repo}/commits/' + sha + '/check-runs?filter=all', 'check_runs')
        result, newest, evidence, seen_ids = {}, {}, [], set()
        def add(name, state, pinned, row, kind):
            if not isinstance(name, str) or not name.strip() or type(row.get('id')) is not int or row['id'] <= 0:
                raise ValueError('malformed GitHub context identity')
            key = (kind, row['id'])
            if key in seen_ids:
                raise ValueError('duplicate GitHub row across pages')
            seen_ids.add(key)
            evidence.append({'kind': kind, 'id': row['id'], 'context': name, 'state': state,
                             'creator': row.get('creator', {}).get('login') if kind == 'status' else None,
                             'appId': row.get('app', {}).get('id') if kind == 'check' else None})
            result.setdefault(name, 'pending')
            if state == 'failure':
                result[name] = 'failure'
            if pinned and ((kind, name) not in newest or row['id'] > newest[kind, name][0]):
                newest[kind, name] = (row['id'], state, row)
        for row in rows:
            state, creator = row.get('state'), row.get('creator')
            if state not in ('success', 'failure', 'error', 'pending') or not isinstance(creator, dict) or not isinstance(creator.get('login'), str):
                raise ValueError('malformed status state or creator')
            add(row.get('context'), 'failure' if state in ('failure', 'error') else state,
                creator['login'].lower() in creators, row, 'status')
        for row in checks:
            app, status, conclusion = row.get('app'), row.get('status'), row.get('conclusion')
            if (not isinstance(app, dict) or type(app.get('id')) is not int or
                    row.get('head_sha') != sha or status not in ('queued', 'in_progress', 'completed', 'waiting', 'requested', 'pending')):
                raise ValueError('malformed check-run app, head or status')
            if status != 'completed':
                state = 'pending'
            elif conclusion == 'success':
                state = 'success'
            elif conclusion in ('failure', 'timed_out', 'cancelled', 'action_required', 'startup_failure', 'stale'):
                state = 'failure'
            elif conclusion in ('neutral', 'skipped'):
                state = 'pending'  # a suite that did not run proves no local execution
            else:
                raise ValueError('malformed check-run conclusion')
            add(row.get('name'), state, app['id'] in apps, row, 'check')
        for (kind, name), (_, state, row) in newest.items():
            if kind == 'status' and state == 'success' and result[name] != 'failure':
                observation = self.pipeline(row, sha)
                if observation:
                    next(item for item in evidence if item['kind'] == kind and item['id'] == row['id'])['pipeline'] = observation
        for name in result:
            if result[name] == 'failure':
                continue
            pinned = [value[1] for (kind, context), value in newest.items() if context == name]
            result[name] = 'success' if pinned and all(x == 'success' for x in pinned) else 'pending'
        return result, evidence

    def tree(self, sha):
        got = self.api('repos/{owner}/{repo}/git/trees/' + sha + '?recursive=1')
        if not isinstance(got, dict) or got.get('truncated') is not False or not isinstance(got.get('tree'), list):
            raise ValueError('incomplete GitHub config tree')
        out = {}
        for row in got['tree']:
            if not isinstance(row, dict) or not isinstance(row.get('path'), str) or not SHA.fullmatch(str(row.get('sha', ''))):
                raise ValueError('malformed GitHub tree entry')
            if row['path'] in out:
                raise ValueError('duplicate GitHub tree path')
            if row.get('type') != 'tree':
                out[row['path']] = row
        return out

    def blob(self, oid):
        got = self.api('repos/{owner}/{repo}/git/blobs/' + oid)
        if not isinstance(got, dict) or got.get('encoding') != 'base64' or got.get('sha') != oid or type(got.get('size')) is not int or not 0 <= got['size'] <= 1048576:
            raise ValueError('malformed or oversized config blob')
        data = base64.b64decode(re.sub(r'\s', '', got.get('content', '')), validate=True)
        if len(data) != got['size'] or hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest() != oid:
            raise ValueError('GitHub blob content does not match its object ID')
        return data.decode('utf-8')

    def coverage(self, sha, ci, gate):
        cover = ({'config': ci.get('config')} if gate == 'ci' else ci.get('covers', {}).get(gate))
        if not cover or not _strings(cover.get('config')) or not SHA.fullmatch(str(ci.get('packetSha', ''))):
            raise ValueError('no pinned manifest CI coverage for gate ' + gate)
        judged, base = self.tree(sha), self.tree(ci['packetSha'])
        config = set(cover['config'])
        # Deliberately conservative: implicit tool configs anywhere in the repo
        # are included. This covers nested working directories and pytest roots
        # without guessing at shell/YAML execution semantics.
        missing = {p for p in judged if p.rsplit('/', 1)[-1] in IMPLICIT} - config
        if missing:
            raise ValueError('coverage omits implicit build/test config: ' + ', '.join(sorted(missing)))
        blobs = {}
        for path in sorted(config):
            a, b = judged.get(path), base.get(path)
            if (not a or not b or a.get('type') != 'blob' or b.get('type') != 'blob' or
                    a.get('mode') not in ('100644', '100755') or b.get('mode') != a.get('mode')):
                raise ValueError('coverage file missing, symlinked or changed mode: ' + path)
            if a['sha'] != b['sha']:
                raise ValueError('CI config changed from pinned packet: ' + path)
            blobs[path] = a['sha']
            content = self.blob(a['sha'])
            invoked = executable_references(path, content, judged)
            if invoked - config:
                raise ValueError('coverage omits tracked file referenced by ' + path + ': ' + ', '.join(sorted(invoked - config)))
            # Constructed filenames cannot be resolved by a token scan. Refuse
            # executable substitution/path interpolation rather than guessing.
            if re.search(r'\$\(|[<>]\(|`|(?:\$\{?\w+\}?/|/\$\{?\w+)', coverage_text(path, content)):
                raise ValueError('dynamic pipeline paths cannot establish complete CI coverage: ' + path)
        if not any(p.endswith(('.yml', '.yaml')) for p in config):
            raise ValueError('coverage must include its pipeline YAML')
        return blobs


def verdict(contexts, required=None):
    want = required or list(contexts)
    if not want:
        return None
    states = [contexts.get(c, 'pending') for c in want]
    if 'failure' in states:
        return 1
    return 0 if all(s == 'success' for s in states) else None


def observe(root, ci, target, gate='ci', api=None, deadline=None):
    client = GitHub(root, api, deadline, ci['repository'])
    client.identity(ci)
    sha = client.target(target)
    contexts, evidence = client.contexts(sha, ci)
    required = ci.get('contexts') if gate == 'ci' else ci.get('covers', {}).get(gate, {}).get('contexts')
    if gate != 'ci' and not required:
        raise ValueError('no manifest CI coverage for gate ' + gate)
    code = verdict(contexts, required)
    blobs = client.coverage(sha, ci, gate) if code == 0 else {}
    if client.target(target) != sha:
        raise TargetMoved('forge target moved during observation')
    return {'headSha': sha, 'target': target, 'contexts': contexts, 'evidence': evidence,
            'exit': code, 'configBlobs': blobs, 'packetSha': ci.get('packetSha')}
