#!/usr/bin/env python3
"""Python typing suppressions are ratcheted without counting prose (#198)."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))


class PythonHatches(unittest.TestCase):
    def scan(self, text, name='app.py'):
        from python_hatches import scan
        return [category for category, _, _ in scan(name, text)]

    def test_strings_and_imports_are_not_uses(self):
        self.assertEqual(self.scan('''"""A docstring: Any # type: ignore"""
from typing import Any, cast, assert_type
text = "# type: ignore"
assert_type(1, int)
'''), [])

    def test_annotations_aliases_and_forward_references(self):
        self.assertEqual(self.scan('''import typing as t
from typing import Any as A
def f(x: dict[str, A], *args: t.Any, **kwargs: A) -> "t.Any":
    local: A = x
''').count('py.any'), 5)

    def test_literal_values_and_annotation_metadata_are_not_types(self):
        self.assertEqual(self.scan('from typing import Any, Literal, Annotated\nx: Literal["Any"]\ny: Annotated[int, "Any", Any]\nz: Annotated[Any, "metadata"]\n'), ['py.any'])

    def test_casts_resolve_imports(self):
        self.assertEqual(self.scan('''from mylib import cast
cast(int, value)
import typing as t
from typing import cast as tc
t.cast(int, value)
tc(str, value)
''').count('py.cast'), 2)

    def test_shadowed_cast_does_not_count(self):
        self.assertEqual(self.scan('''from typing import cast
def f(cast):
    return cast(int, 1)
'''), [])

    def test_comment_directives_and_decorators(self):
        cats = self.scan('''import typing
x = value  # type: ignore[attr-defined]
# pyright: ignore
# mypy: ignore-errors
# pyright: basic
# noqa: F821
@typing.no_type_check
def f(): pass
''')
        self.assertEqual(cats.count('py.type_ignore'), 4)
        self.assertEqual(cats.count('py.no_type_check'), 1)

    def test_methods_resolve_outer_imports_after_class_shadowing(self):
        text = """from typing import cast, no_type_check
@no_type_check
class C:
    cast = staticmethod(lambda value: value)
    def method(self, value):
        return cast(int, value)
"""
        self.assertEqual(self.scan(text).count('py.cast'), 1)
        self.assertEqual(self.scan(text).count('py.no_type_check'), 1)

    def test_comprehension_and_lambda_bindings_do_not_escape_their_scope(self):
        for body, wanted in [('values = [fn(value) for cast in callbacks]\ncast(str, value)\n', 1),
                             ('values = [cast(int, value) for cast in callbacks]\n', 0),
                             ('f = lambda cast: cast(int, 1)\n', 0)]:
            with self.subTest(body=body):
                self.assertEqual(self.scan('from typing import cast\n' + body).count('py.cast'), wanted)

    def test_typing_star_imports_resolve_the_known_hatches(self):
        self.assertEqual(self.scan('from typing import *\nx: Any = cast(int, value)\n'), ['py.any', 'py.cast'])

    def test_pyright_jsonc_comments_and_multiline_values(self):
        self.assertEqual(self.scan('/* "typeCheckingMode": "off" */\n{"typeCheckingMode": "strict"}', 'pyrightconfig.json'), [])
        self.assertEqual(self.scan('{"typeCheckingMode":\n"off"}', 'pyrightconfig.json'), ['flags.types_off'])

    def test_checker_configs_only(self):
        for name, text in [('mypy.ini', '[mypy]\nignore_errors = true\n'),
                           ('setup.cfg', '[mypy-app.*]\nignore_errors = True\n'),
                           ('pyproject.toml', '[tool.mypy]\nignore_errors = true\n'),
                           ('pyproject.toml', '[[tool.mypy.overrides]]\nignore_errors = true\n'),
                           ('pyproject.toml', '[tool.pyright]\ntypeCheckingMode = "off"\n'),
                           ('pyrightconfig.json', '{"typeCheckingMode":"off"}')]:
            with self.subTest(name=name, text=text):
                self.assertEqual(self.scan(text, name), ['flags.types_off'])
        self.assertEqual(self.scan('[other]\nignore_errors = true\n', 'setup.cfg'), [])
        self.assertEqual(self.scan('{"typeCheckingMode":"off"}', 'data.json'), [])

    def test_cli_ratchets_new_categories_and_keeps_python_dormant(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            source = root / 'app.py'
            source.write_text('from typing import Any\nx: Any = 1\n')
            subprocess.run(['git', '-C', str(root), 'add', '.'], check=True)
            def cli(*args):
                return subprocess.run([sys.executable, str(SCRIPTS / 'proof-guard.py'), '--root', str(root), *args], capture_output=True, text=True)
            self.assertIn('dormant', cli('--scan').stdout)
            self.assertEqual(cli('--baseline').returncode, 0)
            baseline = root / '.fluxpoint-proof-baseline.json'
            self.assertEqual(json.loads(baseline.read_text())['counts']['py.any'], 1)
            source.write_text('from typing import Any\nx: Any = 1\ny: Any = 2\n')
            self.assertEqual(cli('--check').returncode, 1)
            self.assertIn('py.any: 1 -> 2', cli('--check').stderr)
            baseline.write_text(json.dumps({'counts': {'ts.any': 0}}))
            result = cli('--check')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('NOT yet ratcheted', result.stderr + result.stdout)


if __name__ == '__main__':
    unittest.main()
