"""Scan every module of the project for a function, method or class defined twice in the same scope (flake8 F811: the
second silently replaces the first; a security-relevant parameter such as a confirmation token can be lost that way)."""

import ast
import inspect
from pathlib import Path

from django.test import SimpleTestCase

from leg_platform.community import deadline_settings

ROOT = Path(__file__).resolve().parents[2]


def duplicates(tree):
    found = []
    for scope in ast.walk(tree):
        if not isinstance(scope, (ast.Module, ast.ClassDef)):
            continue
        seen = {}
        for node in scope.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                # A property setter or an overload decorated with the name of the first is no duplicate.
                decorated = any(
                    isinstance(item, ast.Attribute) and item.attr in {"setter", "deleter", "getter", "register"}
                    or isinstance(item, ast.Name) and item.id == "overload"
                    for item in node.decorator_list
                )
                if node.name in seen and not decorated:
                    found.append((node.name, seen[node.name], node.lineno))
                seen[node.name] = node.lineno
    return found


class DuplicateDefinitionTests(SimpleTestCase):
    def test_no_module_of_the_app_code_defines_a_name_twice_in_one_scope(self):
        problems = []
        for path in sorted(ROOT.rglob("*.py")):
            if "migrations" in path.parts or "tests" in path.parts:
                continue
            for name, first, second in duplicates(ast.parse(path.read_text(encoding="utf-8"))):
                problems.append(f"{path.relative_to(ROOT)}: {name} (lines {first} and {second})")
        self.assertEqual(problems, [])

    def test_the_deadline_update_has_the_confirmation_token(self):
        # The earlier, shorter ``update`` without the token once shadowed nothing but could be re-added by mistake.
        parameters = inspect.signature(deadline_settings.update).parameters
        self.assertIn("expected", parameters)
        self.assertIn("run_time", parameters)
