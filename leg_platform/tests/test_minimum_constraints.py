"""Tests of scripts/check_minimum_constraints.py (D-015, D-026)."""

import contextlib
import importlib.util
import io

from django.conf import settings
from django.test import SimpleTestCase

_spec = importlib.util.spec_from_file_location(
    "check_minimum_constraints", settings.BASE_DIR / "scripts" / "check_minimum_constraints.py"
)
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)

PYPROJECT = """
[project]
dependencies = [
    "Django>=5.2.18,<6.2",
    "django_axes>=8.3.1",
    "segno[extra] >= 1.6.6",
]
"""
CONSTRAINTS = """
# comment
Django==5.2.18
django-axes==8.3.1
segno==1.6.6  # trailing comment
"""


class MinimumConstraintsTests(SimpleTestCase):
    def test_matching_files(self):
        self.assertEqual(check.compare(PYPROJECT, CONSTRAINTS), [])

    def test_raised_pin_is_reported(self):
        errors = check.compare(PYPROJECT, CONSTRAINTS.replace("Django==5.2.18", "Django==6.1.2"))
        self.assertEqual(len(errors), 1)
        self.assertIn("django", errors[0])

    def test_raised_lower_bound_is_reported(self):
        errors = check.compare(PYPROJECT.replace(">=8.3.1", ">=8.4.0"), CONSTRAINTS)
        self.assertEqual(len(errors), 1)
        self.assertIn("django-axes", errors[0])

    def test_missing_and_extra_pins_are_reported(self):
        errors = check.compare(PYPROJECT, CONSTRAINTS.replace("segno==1.6.6", "msal==1.39.0"))
        self.assertEqual(len(errors), 2)

    def test_dependency_without_lower_bound_is_reported(self):
        errors = check.compare(PYPROJECT.replace(">= 1.6.6", ""), CONSTRAINTS)
        self.assertTrue(any("lower bound" in error for error in errors))

    def test_constraint_that_is_not_a_pin_is_reported(self):
        errors = check.compare(PYPROJECT, CONSTRAINTS.replace("segno==1.6.6", "segno>=1.6.6"))
        self.assertTrue(any("name==version" in error for error in errors))

    def test_repository_files_agree(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            result = check.main()
        self.assertEqual(result, 0, output.getvalue())
