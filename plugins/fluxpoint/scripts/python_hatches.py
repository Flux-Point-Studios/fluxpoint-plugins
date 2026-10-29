"""Static typing escape hatches; never import code from the scanned repository."""
import ast
import io
import json
from pathlib import Path
import re
import tokenize

CATEGORIES = ('py.type_ignore', 'py.no_type_check', 'py.any', 'py.cast')


def scan(name, text):
    """Yield (category, line, source) for Python sources and checker configs."""
    base = Path(name).name
    lines = text.splitlines()
    if base in ('mypy.ini', '.mypy.ini', 'setup.cfg', 'pyproject.toml'):
        section = ''
        for n, line in enumerate(lines, 1):
            line = line.split('#', 1)[0].split(';', 1)[0].strip()
            if line.startswith('['):
                section = line.strip('[] ')
            enabled = (section == 'tool.mypy' or section.startswith('tool.mypy.')) if base == 'pyproject.toml' else (section == 'mypy' or section.startswith('mypy-'))
            if enabled and re.fullmatch(r'ignore_errors\s*=\s*(?:true|yes|1)', line, re.I):
                yield 'flags.types_off', n, lines[n - 1]
            if base == 'pyproject.toml' and section == 'tool.pyright' and re.fullmatch(r'''typeCheckingMode\s*=\s*["']off["']''', line):
                yield 'flags.types_off', n, lines[n - 1]
        return
    if base == 'pyrightconfig.json':
        # Pyright accepts JSONC: comments and trailing commas are syntax, while
        # quoted comment markers and escaped JSON keys remain ordinary data.
        string = r'"(?:\\.|[^"\\])*"'
        uncommented = re.sub(string + r'|//[^\n]*|/\*[\s\S]*?\*/',
                             lambda match: match[0] if match[0].startswith('"') else
                             ''.join('\n' if c == '\n' else ' ' for c in match[0]), text)
        plain = re.sub(string + r'|,(?=\s*[}\]])',
                       lambda match: match[0] if match[0].startswith('"') else '', uncommented)
        try:
            config = json.loads(plain)
        except ValueError:
            return  # Invalid checker configuration is the project's checker's responsibility.
        if isinstance(config, dict) and config.get('typeCheckingMode') == 'off':
            key = re.search(r'"typeCheckingMode"\s*:', uncommented)
            line = uncommented.count('\n', 0, key.start()) + 1 if key else 1
            yield 'flags.types_off', line, lines[line - 1] if lines else ''
        return
    if Path(name).suffix not in ('.py', '.pyi'):
        return
    try:
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.type == tokenize.COMMENT and re.match(
                    r'#\s*(?:type:\s*ignore\b|pyright:\s*(?:ignore\b|basic\b|off\b)|mypy:\s*ignore-errors\b)', token.string):
                yield 'py.type_ignore', token.start[0], token.line.rstrip()
    except (tokenize.TokenError, IndentationError):
        pass
    try:
        tree = ast.parse(text, filename=name)
    except SyntaxError:
        return  # The project's syntax/type check remains responsible for invalid code.

    class Visitor(ast.NodeVisitor):
        def __init__(self):
            self.bindings = {}
            self.hits = []
            self.in_class = False
            self.class_outer = None

        def qualified(self, node):
            if isinstance(node, ast.Name):
                return self.bindings.get(node.id)
            if isinstance(node, ast.Attribute):
                parent = self.qualified(node.value)
                return parent + '.' + node.attr if parent else None

        def hit(self, category, node):
            n = getattr(node, 'lineno', 1)
            self.hits.append((category, n, lines[n - 1] if n <= len(lines) else ''))

        def annotation(self, node, original=None):
            if node is None:
                return
            if self.qualified(node) in ('typing.Any', 'typing_extensions.Any'):
                self.hit('py.any', original or node)
                return
            if isinstance(node, ast.Subscript):
                constructor = self.qualified(node.value)
                if constructor in ('typing.Literal', 'typing_extensions.Literal'):
                    return  # Literal parameters are values, including quoted strings.
                if constructor in ('typing.Annotated', 'typing_extensions.Annotated'):
                    types = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
                    if types:
                        self.annotation(types[0], original)
                    return  # Remaining Annotated operands are metadata, not type expressions.
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                try:
                    self.annotation(ast.parse(node.value, mode='eval').body, original or node)
                except SyntaxError:
                    pass
                return
            for child in ast.iter_child_nodes(node):
                self.annotation(child, original)

        def visit_Import(self, node):
            for alias in node.names:
                self.bindings[alias.asname or alias.name.split('.')[0]] = alias.name if alias.asname else alias.name.split('.')[0]

        def visit_ImportFrom(self, node):
            for alias in node.names:
                if alias.name == '*' and not node.level and node.module in ('typing', 'typing_extensions'):
                    for name in ('Any', 'cast', 'no_type_check'):
                        self.bindings[name] = node.module + '.' + name
                else:
                    self.bindings[alias.asname or alias.name] = (node.module + '.' + alias.name) if node.module and not node.level else None

        def visit_Name(self, node):
            if isinstance(node.ctx, (ast.Store, ast.Del)):
                self.bindings.pop(node.id, None)

        def visit_Call(self, node):
            if self.qualified(node.func) in ('typing.cast', 'typing_extensions.cast'):
                self.hit('py.cast', node)
            self.generic_visit(node)

        def visit_AnnAssign(self, node):
            self.annotation(node.annotation)
            self.visit(node.target)
            if node.value:
                self.visit(node.value)

        def visit_FunctionDef(self, node):
            for decorator in node.decorator_list:
                if self.qualified(decorator) in ('typing.no_type_check', 'typing_extensions.no_type_check'):
                    self.hit('py.no_type_check', decorator)
                self.visit(decorator)
            args = node.args.posonlyargs + node.args.args + node.args.kwonlyargs
            args += [arg for arg in (node.args.vararg, node.args.kwarg) if arg]
            for arg in args:
                self.annotation(arg.annotation)
            self.annotation(node.returns)
            for value in node.args.defaults + [value for value in node.args.kw_defaults if value]:
                self.visit(value)
            outer = self.bindings.copy()
            was_class = self.in_class
            self.bindings = self.class_outer.copy() if self.in_class else outer.copy()
            self.in_class = False
            for arg in args:
                self.bindings.pop(arg.arg, None)
            for statement in node.body:
                self.visit(statement)
            self.bindings = outer
            self.in_class = was_class
            self.bindings.pop(node.name, None)

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_ClassDef(self, node):
            outer = self.bindings.copy()
            for decorator in node.decorator_list:
                if self.qualified(decorator) in ('typing.no_type_check', 'typing_extensions.no_type_check'):
                    self.hit('py.no_type_check', decorator)
                self.visit(decorator)
            for value in node.bases + [keyword.value for keyword in node.keywords]:
                self.visit(value)
            previous = self.in_class, self.class_outer
            # Class namespaces are visible to their own statement expressions,
            # but they are not enclosing lexical scopes for methods or nested classes.
            lexical = self.class_outer.copy() if self.in_class else outer.copy()
            self.class_outer = lexical.copy()
            self.bindings, self.in_class = lexical, True
            for statement in node.body:
                self.visit(statement)
            self.bindings = outer
            self.in_class, self.class_outer = previous
            self.bindings.pop(node.name, None)

        def visit_Lambda(self, node):
            for value in node.args.defaults + [value for value in node.args.kw_defaults if value]:
                self.visit(value)
            outer, was_class = self.bindings, self.in_class
            self.bindings = self.class_outer.copy() if was_class else outer.copy()
            self.in_class = False
            args = node.args.posonlyargs + node.args.args + node.args.kwonlyargs
            args += [arg for arg in (node.args.vararg, node.args.kwarg) if arg]
            for arg in args:
                self.bindings.pop(arg.arg, None)
            self.visit(node.body)
            self.bindings, self.in_class = outer, was_class

        def comprehension(self, node):
            outer, was_class = self.bindings, self.in_class
            for index, generator in enumerate(node.generators):
                # The first iterator is evaluated in the enclosing scope.
                self.visit(generator.iter)
                if index == 0:
                    self.bindings = self.class_outer.copy() if was_class else outer.copy()
                    self.in_class = False
                self.visit(generator.target)
                for condition in generator.ifs:
                    self.visit(condition)
            if isinstance(node, ast.DictComp):
                self.visit(node.key)
                self.visit(node.value)
            else:
                self.visit(node.elt)
            self.bindings, self.in_class = outer, was_class

        visit_ListComp = comprehension
        visit_SetComp = comprehension
        visit_GeneratorExp = comprehension
        visit_DictComp = comprehension

    visitor = Visitor()
    visitor.visit(tree)
    yield from visitor.hits
