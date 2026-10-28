"""Static typing escape hatches; never import code from the scanned repository."""
import ast
import io
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
        for n, line in enumerate(lines, 1):
            if re.search(r'"typeCheckingMode"\s*:\s*"off"', line.split('//', 1)[0]):
                yield 'flags.types_off', n, line
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
            for arg in args:
                self.bindings.pop(arg.arg, None)
            for statement in node.body:
                self.visit(statement)
            self.bindings = outer
            self.bindings.pop(node.name, None)

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_ClassDef(self, node):
            outer = self.bindings.copy()
            for statement in node.body:
                self.visit(statement)
            self.bindings = outer
            self.bindings.pop(node.name, None)

    visitor = Visitor()
    visitor.visit(tree)
    yield from visitor.hits
