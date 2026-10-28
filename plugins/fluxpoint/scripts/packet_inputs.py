"""Static, repository-local packet input closure. Never imports repository code."""
import ast
import configparser
from collections import deque
import hashlib
import os
from pathlib import Path
import re
import shlex
import subprocess
import tokenize


TEST_DIRS = {'test', 'tests', 'testing', 'test_support', 'fixtures'}
JS_SUFFIXES = ('.js', '.ts', '.mjs', '.cjs', '.mts', '.cts', '.jsx', '.tsx')
PATH_FLAGS = {'--path', '--config', '-c', '--testPathPattern', '--testPathPatterns'}
CWD_FLAGS = {'--prefix', '--dir', '--cwd', '-C', '--project'}
IGNORED_DIRS = {'.git', '.venv', 'venv', 'node_modules', '__pycache__', '.tox'}


def trust_paths(root, doc, frozen=()):
    """Best-effort live warning paths; freezing and recording use strict parsing."""
    inputs = Inputs(root, doc, closure=False, allow_missing=True)
    try:
        inputs.closure = True
        inputs._expand()
        inputs.include_locked(frozen)
    except (OSError, ValueError, TypeError):
        # Already discovered files still count, especially the file that failed
        # to parse. Previously frozen paths never depend on successful parsing.
        pass
    return set(inputs.reasons) | set(frozen)


def sha(path):
    return hashlib.sha256(path.read_bytes().replace(b'\r\n', b'\n')).hexdigest() if path.is_file() else None


def resolved(path):
    """resolve() in the spelling of a resolved root. For a missing path whose parent
    exists, Windows can return an extended-length \\\\?\\ path, e.g. once Win32 has
    dropped a '...' segment, and that spelling is never relative to the root."""
    text = str(Path(path).resolve())
    if text.startswith('\\\\?\\UNC\\'):
        return Path('\\\\' + text[8:])
    return Path(text[4:] if text.startswith('\\\\?\\') else text)


# A '/' after one of these starts a regex literal; after anything else it divides.
REGEX_AFTER = ({('punct', p) for p in '(,=:[!&|?{};+-*%<>~^'}
               | {('word', w) for w in ('return', 'typeof', 'instanceof', 'in', 'of', 'new', 'delete',
                                        'void', 'throw', 'case', 'do', 'else', 'yield', 'await')})


def js_tokens(text):
    """Words, punctuation and string literals of JS/TS source. Comments are dropped,
    so prose that quotes an import is never read as one; template and regex
    literals become opaque tokens, so a '/*' or quote inside them hides no code."""
    tokens, templates, parens, i, n = [], [], [], 0, len(text)
    control_end = -1
    while i < n:
        c = text[i]
        if c == '`' or (c == '}' and templates and templates[-1] == 0):
            if c == '}':
                templates.pop()
            j = i + 1
            while j < n and text[j] != '`' and not text.startswith('${', j):
                j += 2 if text[j] == '\\' else 1
            if text.startswith('${', j):
                templates.append(0)
                i = j + 2
            else:
                tokens.append(('template', ''))
                i = j + 1
        elif c.isspace():
            i += 1
        elif text.startswith('//', i):
            end = text.find('\n', i)
            i = n if end < 0 else end
        elif text.startswith('/*', i):
            end = text.find('*/', i + 2)
            i = n if end < 0 else end + 2
        elif c in '\'"':
            j = i + 1
            while j < n and text[j] not in (c, '\n'):
                j += 2 if text[j] == '\\' else 1
            tokens.append(('string', text[i + 1:j]))
            i = j + 1
        elif c == '/' and (not tokens or tokens[-1] in REGEX_AFTER or len(tokens) - 1 == control_end):
            j, klass = i + 1, False
            while j < n and text[j] != '\n' and (klass or text[j] != '/'):
                klass = (klass or text[j] == '[') and text[j] != ']'
                j += 2 if text[j] == '\\' else 1
            if j < n and text[j] == '/':
                tokens.append(('regex', ''))
                i = j + 1
            else:
                tokens.append(('punct', c))
                i += 1
        elif c.isalnum() or c in '_$':
            j = i
            while j < n and (text[j].isalnum() or text[j] in '_$'):
                j += 1
            tokens.append(('word', text[i:j]))
            i = j
        else:
            if c == '(':
                # A regex can start a statement after if/while/etc. conditions,
                # but a slash after a function call still means division.
                control = (bool(tokens) and tokens[-1] in
                           {('word', w) for w in ('if', 'while', 'for', 'with', 'switch', 'catch')}
                           and (len(tokens) < 2 or tokens[-2] != ('punct', '.')))
                parens.append(control)
            elif c == ')' and parens and parens.pop():
                control_end = len(tokens)
            if templates and c == '{':
                templates[-1] += 1
            elif templates and c == '}':
                templates[-1] -= 1
            tokens.append(('punct', c))
            i += 1
    return tokens


def js_specifiers(text):
    """Relative specifiers of literal import, export-from, import() and require()."""
    tokens = js_tokens(text)
    for k, (kind, value) in enumerate(tokens):
        if kind != 'string' or len(value) < 2 or not value.startswith('.'):
            continue
        before = tokens[k - 1] if k else None
        if before in (('word', 'from'), ('word', 'import')) or (
                before == ('punct', '(') and k > 1 and tokens[k - 2] in (('word', 'import'), ('word', 'require'))):
            yield value


class Inputs:
    def __init__(self, root, doc, closure=True, allow_missing=False):
        self.root = resolved(root)
        self.doc, self.closure, self.allow_missing = doc, closure, allow_missing
        self.reasons, self.owners, self.seeds, self.edges, self.trees = {}, {}, {}, {}, {}
        self.search = {self.root, self.root / 'src'}
        self.test_roots, self.warnings = set(), set()
        self._tracked = None
        self._packages = None
        self._queue = deque()
        self._seed()
        if closure:
            self._expand()

    def path(self, rel):
        if not isinstance(rel, str) or not rel.strip():
            raise ValueError('packet input must be a nonempty repository path')
        path = Path(rel)
        if (path.is_absolute() or rel.startswith(('/', '\\')) or '..' in path.parts
                or '\\' in rel or '\0' in rel or re.match(r'^[A-Za-z]:', rel)):
            raise ValueError('packet input must stay inside the repository: ' + repr(rel))
        path = self.root / path
        if not resolved(path).is_relative_to(self.root):
            raise ValueError('packet input must stay inside the repository: ' + repr(rel))
        return path

    def rel(self, path):
        # Keep the declared spelling for drift reporting, but refuse physical escapes.
        self.path(path.relative_to(self.root).as_posix())
        return path.relative_to(self.root).as_posix()

    def local(self, path):
        """Normalize valid relative imports without erasing symlink spelling."""
        physical = resolved(path)
        path = Path(os.path.abspath(path))
        if (not path.is_relative_to(self.root) or not physical.is_relative_to(self.root)
                or not resolved(path).is_relative_to(self.root)):
            raise ValueError('import/config path must stay inside the repository: ' + str(path))
        if resolved(path) != physical:
            raise ValueError('ambiguous parent traversal through a symlink in import/config path: ' + str(path))
        return path

    def tracked(self):
        if self._tracked is None:
            result = subprocess.run(['git', 'ls-files', '-z', '--cached'], cwd=self.root,
                                    capture_output=True, check=False)
            if result.returncode:
                self._tracked = set()
            else:
                self._tracked = {v.decode('utf-8') for v in result.stdout.split(b'\0') if v}
        return self._tracked

    def packages(self):
        if self._packages is None:
            names = {'package.json', 'pyproject.toml', 'pytest.ini'}
            paths = self.tracked()
            if paths:
                self._packages = {self.root / Path(p).parent for p in paths if Path(p).name in names}
            else:
                self._packages = set()
                for base, dirs, files in os.walk(self.root):
                    dirs[:] = [d for d in dirs if d not in IGNORED_DIRS]
                    if names.intersection(files):
                        self._packages.add(Path(base))
        return self._packages

    def add(self, path, kind, check, via=None):
        rel = self.rel(path)
        if not path.is_file() and not (self.allow_missing and not path.exists()):
            raise ValueError('packet input does not name a repository file: ' + rel)
        self.reasons.setdefault(rel, set()).add((kind, check, via or ''))
        self.owners.setdefault(rel, set()).add(check)
        return rel

    def files(self, path):
        self.rel(path)
        if path.is_dir():
            prefix = '' if path == self.root else self.rel(path).rstrip('/') + '/'
            names = sorted(p for p in self.tracked() if p.startswith(prefix))
            if not names:
                raise ValueError('packet input directory has no tracked files: ' + self.rel(path))
            return [self.path(p) for p in names]
        return [path]

    def locate(self, text, cwd=None, explicit=False):
        text = text.split('::', 1)[0]
        if not text or text.startswith('-'):
            return None
        path = Path(text)
        if path.is_absolute():
            if not path.is_relative_to(self.root):
                if explicit:
                    raise ValueError('check input is outside the repository: ' + text)
                return None  # An installed interpreter or executable.
            path = self.path(path.relative_to(self.root).as_posix())
            return path if path.exists() else None
        # An explicit package cwd wins; do not silently bind a root file instead.
        if cwd is not None:
            candidate = self.path((cwd.relative_to(self.root) / path).as_posix())
            if candidate.exists():
                return candidate
            if explicit:
                raise ValueError('check input does not exist in its package cwd: ' + text)
        candidate = self.path(text)
        if candidate.exists():
            return candidate
        if explicit or '/' in text:
            matches = sorted({base / path for base in self.packages() if (base / path).exists()})
            for match in matches:
                self.rel(match)
            if len(matches) > 1:
                raise ValueError('ambiguous package-relative check input ' + repr(text) + ': '
                                 + ', '.join(self.rel(p) for p in matches))
            if matches:
                return matches[0]
            if explicit:
                raise ValueError('check input does not exist: ' + text)
        return None

    def _seed_path(self, path, kind, check, selector=None):
        for file in self.files(path):
            rel = self.add(file, kind, check)
            self.seeds.setdefault(check, {}).setdefault(rel, set())
            if selector and '::' in selector and path.is_file():
                self.seeds[check][rel].add(rel + '::' + selector.split('::', 1)[1])
            self._queue.append((rel, check, True))
            for parent in file.parents:
                if parent == self.root:
                    break
                if parent.name in TEST_DIRS:
                    self.test_roots.add(parent)
                    self.search.update({parent, parent.parent})

    def _seed(self):
        for check in self.doc['checks']:
            cid = check['id']
            for kind in ('argv', 'witness'):
                argv, cwd = check.get(kind, []), None
                pytest = any(Path(arg).name in ('pytest', 'pytest.exe') for arg in argv)
                for i, arg in enumerate(argv):
                    flag, sep, value = arg.partition('=')
                    if flag in CWD_FLAGS:
                        value = value if sep else (argv[i + 1] if i + 1 < len(argv) else '')
                        cwd = self.path(value)
                        if not cwd.is_dir():
                            raise ValueError('check working directory does not exist: ' + value)
                        self.search.add(cwd)
                for i, arg in enumerate(argv):
                    flag, sep, value = arg.partition('=')
                    if flag in CWD_FLAGS or (i and argv[i - 1] in CWD_FLAGS):
                        continue
                    if i and argv[i - 1] == '-c' and not pytest:
                        continue  # Python -c code is not pytest's configuration path.
                    explicit = flag in PATH_FLAGS if sep else bool(i and argv[i - 1] in PATH_FLAGS)
                    text = value if sep and arg.startswith('-') else arg
                    if arg.startswith('-') and not sep:
                        continue
                    path = self.locate(text, cwd, explicit)
                    if path is not None:
                        self._seed_path(path, kind, cid, text)
        for rel in self.doc.get('harnessInputs', []):
            for check in self.doc['checks']:
                self._seed_path(self.path(rel), 'harnessInputs', check['id'])

    def support(self, path):
        return (any(path.is_relative_to(base) for base in self.test_roots)
                or any(part in TEST_DIRS for part in path.relative_to(self.root).parts[:-1])
                or path.name == 'conftest.py' or path.name.startswith('test_')
                or path.name.endswith('_test.py'))

    def module(self, parts, bases=None, parents=True):
        found = set()
        for base in bases if bases is not None else self.search:
            current = base
            for part in parts:
                current = current / part
                init = current / '__init__.py'
                if parents and init.is_file():
                    self.rel(init)
                    found.add(self.rel(init))
            for candidate in (current.with_suffix('.py'), current / '__init__.py'):
                if candidate.is_relative_to(self.root) and candidate.is_file():
                    self.rel(candidate)
                    found.add(self.rel(candidate))
        return found

    def parse(self, rel, strict):
        if rel in self.trees:
            return self.trees[rel]
        path = self.path(rel)
        if not path.is_file():
            return None
        try:
            with tokenize.open(str(path)) as source:
                tree = ast.parse(source.read(), filename=rel)
        except (SyntaxError, UnicodeError, LookupError) as exc:
            if strict:
                raise ValueError('cannot parse locked Python input ' + rel + ': ' + str(exc)) from exc
            self.warnings.add('reach could not parse production module ' + rel)
            return None
        self.trees[rel] = tree
        return tree

    def imports(self, rel, strict):
        tree = self.parse(rel, strict)
        if tree is None:
            return set()
        file = self.path(rel)
        bases = set(self.search) | {file.parent}
        result = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for name in node.names:
                    result.update(self.module(name.name.split('.'), bases))
            elif isinstance(node, ast.ImportFrom):
                parts = node.module.split('.') if node.module else []
                if node.level:
                    base = file.parent
                    for _ in range(node.level - 1):
                        base = base.parent
                    if not base.is_relative_to(self.root):
                        raise ValueError('relative import leaves repository in ' + rel)
                    source_bases = {base}
                else:
                    source_bases = bases
                result.update(self.module(parts, source_bases))
                for name in node.names:
                    if name.name != '*':
                        result.update(self.module(parts + name.name.split('.'), source_bases))
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if any(isinstance(target, ast.Name) and target.id == 'pytest_plugins' for target in targets):
                    try:
                        plugins = ast.literal_eval(node.value)
                    except (ValueError, TypeError, SyntaxError):
                        self.warnings.add('dynamic pytest_plugins needs explicit harnessInputs: ' + rel)
                        continue
                    plugins = [plugins] if isinstance(plugins, str) else plugins
                    if isinstance(plugins, (list, tuple)) and all(isinstance(p, str) for p in plugins):
                        for plugin in plugins:
                            result.update(self.module(plugin.split('.'), bases))
        return result - {rel}

    def config(self, path):
        """Pin pytest tables and use literal pythonpath entries for local resolution."""
        text = path.read_text(encoding='utf-8-sig')
        values = {}
        if path.name == 'pyproject.toml':
            section = re.search(r'^[ \t]*\[tool\.pytest(?:\.ini_options)?\][ \t]*(?:#[^\n]*)?(?:\n|\Z)(.*?)(?=^[ \t]*\[|\Z)',
                                text, re.M | re.S)
            if not section:
                return False
            match = re.search(r'^\s*pythonpath\s*=\s*(.*)', section[1], re.M)
            if match:
                literal = match[1].strip()
                if literal.startswith('['):
                    start = section[1].find(match[0]) + match[0].find('[')
                    end = section[1].find(']', start)
                    if end < 0:
                        raise ValueError('unterminated pytest pythonpath in ' + self.rel(path))
                    literal = section[1][start:end + 1]
                try:
                    values['pythonpath'] = ast.literal_eval(literal)
                except (ValueError, SyntaxError) as exc:
                    raise ValueError('pytest pythonpath must be a literal string/list in ' + self.rel(path)) from exc
        else:
            section = 'tool:pytest' if path.name == 'setup.cfg' else 'pytest'
            if not re.search(r'^[ \t]*\[' + re.escape(section) + r'\]', text, re.M):
                return False
            config = configparser.ConfigParser(interpolation=None)
            try:
                config.read_string(text)
            except configparser.Error as exc:
                raise ValueError('cannot parse pytest configuration ' + self.rel(path)) from exc
            if not config.has_section(section):
                return False
            values = dict(config[section])
        paths = values.get('pythonpath', [])
        if isinstance(paths, str):
            paths = shlex.split(paths)
        if not isinstance(paths, list) or any(not isinstance(p, str) for p in paths):
            raise ValueError('pytest pythonpath must contain paths in ' + self.rel(path))
        for value in paths:
            candidate = self.local(path.parent / value)
            if not candidate.is_dir():
                raise ValueError('pytest pythonpath does not name a directory: ' + value)
            self.search.add(candidate)
        return True

    def ambient(self, rel):
        path, result = self.path(rel), set()
        for parent in path.parents:
            if not parent.is_relative_to(self.root):
                break
            if path.suffix == '.py':
                for name, kind in (('conftest.py', 'conftest'), ('__init__.py', 'package-init')):
                    file = parent / name
                    if file.is_file() and file != path:
                        result.add((self.rel(file), kind))
                for name in ('pytest.ini', 'tox.ini', 'setup.cfg', 'pyproject.toml'):
                    file = parent / name
                    if file.is_file() and self.config(file):
                        result.add((self.rel(file), 'pytest-config'))
            elif path.suffix in JS_SUFFIXES:
                package = parent / 'package.json'
                if package.is_file():
                    result.add((self.rel(package), 'package-config'))
                for prefix in ('vitest.config', 'vite.config'):
                    for suffix in JS_SUFFIXES:
                        file = parent / (prefix + suffix)
                        if file.is_file():
                            result.add((self.rel(file), 'vitest-config'))
            if parent == self.root:
                break
        return result

    def js_imports(self, rel):
        text, result = self.path(rel).read_text(encoding='utf-8'), set()
        # Literal imports only; dynamic loaders remain explicit harnessInputs.
        for specifier in js_specifiers(text):
            base = self.local(self.path(rel).parent / specifier)
            candidates = [base] + [Path(str(base) + suffix) for suffix in JS_SUFFIXES]
            candidates += [base / ('index' + suffix) for suffix in JS_SUFFIXES]
            result.update(self.rel(p) for p in candidates if p.is_file())
        return result

    def _expand(self):
        # Config pythonpath must be available before resolving any file's imports.
        for rel in list(self.reasons):
            for ambient, kind in self.ambient(rel):
                for cid in list(self.owners[rel]):
                    self.add(self.path(ambient), kind, cid, rel)
                    self._queue.append((ambient, cid, True))
        seen = set()
        while self._queue:
            rel, cid, locked = self._queue.popleft()
            if (rel, cid, locked) in seen:
                continue
            seen.add((rel, cid, locked))
            path = self.path(rel)
            if not path.is_file():
                continue
            dependencies = set()
            if locked:
                for ambient, kind in self.ambient(rel):
                    self.add(self.path(ambient), kind, cid, rel)
                    dependencies.add(ambient)
                    self._queue.append((ambient, cid, True))
            if path.suffix == '.py':
                imported = self.imports(rel, locked)
            elif path.suffix in JS_SUFFIXES:
                imported = self.js_imports(rel)
            else:
                imported = set()
            dependencies.update(imported)
            self.edges.setdefault(rel, set()).update(dependencies)
            for dep in imported:
                is_locked = self.support(self.path(dep)) or dep in self.reasons
                if is_locked:
                    self.add(self.path(dep), 'test-import', cid, rel)
                self._queue.append((dep, cid, is_locked))

    def include_locked(self, paths):
        """Retained paths remain roots, even when the new argv no longer names them."""
        for rel in paths:
            if rel not in self.reasons:
                for check in self.doc['checks']:
                    self.add(self.path(rel), 'locked-harness', check['id'])
                    self._queue.append((rel, check['id'], True))
        if self.closure:
            self._expand()

    def hashes(self):
        return {rel: sha(self.path(rel)) for rel in sorted(self.reasons)}

    def rows(self):
        return [{'path': rel, 'sha256': sha(self.path(rel)), 'checks': sorted(self.owners[rel]),
                 'reasons': [dict(kind=k, check=c, **({'via': v} if v else {}))
                             for k, c, v in sorted(self.reasons[rel])]}
                for rel in sorted(self.reasons)]

    def nodes(self, rel, check):
        selected = self.seeds.get(check, {}).get(rel, set())
        if selected:
            return sorted(selected)
        tree = self.trees.get(rel)
        if tree is None:
            return [rel]
        result = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith('test'):
                result.append(rel + '::' + node.name)
            elif isinstance(node, ast.ClassDef) and node.name.startswith('Test'):
                result.extend(rel + '::' + node.name + '::' + method.name for method in node.body
                              if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef))
                              and method.name.startswith('test'))
        return result or [rel]

    def reach(self, queries):
        targets, named = set(), []
        for query in queries:
            if '/' in query or query.endswith(('.py',) + JS_SUFFIXES):
                path = self.path(query.split('::', 1)[0])
                paths = {self.rel(file) for file in self.files(path) if file.is_file()}
            else:
                paths = self.module(query.split('.'), parents=False)
            if not paths:
                raise ValueError('reach target does not resolve to a repository module: ' + query)
            targets.update(paths)
            named.append({'query': query, 'paths': sorted(paths)})

        def reaches(start):
            pending, seen = [start], set()
            while pending:
                current = pending.pop()
                if current in targets:
                    return True
                if current not in seen:
                    seen.add(current)
                    pending.extend(self.edges.get(current, ()))
            return False

        matches = []
        for check in self.doc['checks']:
            cid = check['id']
            files = [{'path': rel, 'nodes': self.nodes(rel, cid)}
                     for rel in sorted(self.reasons) if cid in self.owners[rel] and reaches(rel)]
            if files:
                matches.append({'check': cid, 'files': files, 'argv': check['argv'],
                                'witness': check.get('witness', [])})
        return {'targets': named, 'checks': matches, 'warnings': sorted(self.warnings),
                'scope': 'Static early probe only; every packet check still runs at the gate.'}
