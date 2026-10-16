"""The test areas (scripts/test_areas.py, D-071) cannot rot.

Every test module belongs to at least one area, every label and path of the areas exists, every
file of the repository is mapped to an area or is a core file, and the resolution of changed files
has the documented safe defaults. Needs no database; the file lists come from ``git ls-files``.
"""

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.test import SimpleTestCase

ROOT = Path(settings.BASE_DIR)
SPEC = importlib.util.spec_from_file_location("leg_test_areas", ROOT / "scripts" / "test_areas.py")
test_areas = importlib.util.module_from_spec(SPEC)
# Registered before the execution: dataclasses look their module up in sys.modules.
sys.modules[SPEC.name] = test_areas
SPEC.loader.exec_module(test_areas)

PATH_RESOLUTION = "leg_platform/accounts/registration.py"


def repository_files(test):
    """Return the repository files (tracked and untracked, not ignored) as relative paths."""
    git = shutil.which("git")
    if git is None:
        message = "Git is not available; the file list needs 'git ls-files'."
        if os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"):
            test.fail(message)
        test.skipTest(message)
    result = subprocess.run(
        [git, "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT, capture_output=True, check=False, timeout=60,
    )
    test.assertEqual(result.returncode, 0)
    names = [name for name in result.stdout.decode("utf-8").split("\0") if name]
    # A file deleted in the working tree but still listed by Git does not exist any more.
    return [name for name in names if (ROOT / name).is_file()]


def all_test_modules():
    return sorted(
        ".".join(path.relative_to(ROOT).with_suffix("").parts)
        for path in (ROOT / "leg_platform").rglob("test_*.py")
        if "tests" in path.relative_to(ROOT).parts[:-1]
    )


class AreaDefinitionTests(SimpleTestCase):
    def test_every_test_module_belongs_to_an_area(self):
        labels = [label for area in test_areas.AREAS.values() for label in area.tests]
        orphans = [
            module
            for module in all_test_modules()
            if not any(test_areas.labels_cover(label, module) for label in labels)
        ]
        self.assertEqual(orphans, [], "Test modules that belong to no area (scripts/test_areas.py)")

    def test_every_label_names_an_existing_module_or_package(self):
        missing = [
            f"{name}: {label}"
            for name, area in test_areas.AREAS.items()
            for label in (*area.tests, *test_areas.ALWAYS)
            if not test_areas.label_exists(label)
        ]
        self.assertEqual(missing, [])

    def test_every_path_pattern_matches_a_file(self):
        files = repository_files(self)
        stale = []
        for name, area in test_areas.AREAS.items():
            for pattern in area.paths:
                if not any(test_areas.matches(pattern, file) for file in files):
                    stale.append(f"{name}: {pattern}")
        for pattern in test_areas.CORE_PATHS:
            if not any(test_areas.matches(pattern, file) for file in files):
                stale.append(f"core: {pattern}")
        self.assertEqual(stale, [], "Path patterns that match no file")

    def test_every_file_is_mapped_to_an_area_or_is_a_core_file(self):
        unmapped = [
            file
            for file in repository_files(self)
            if not test_areas.is_core(file)
            and test_areas.test_module_label(file) is None
            and not test_areas.areas_of(file)
        ]
        self.assertEqual(
            unmapped, [], "Files that no area maps (add them to scripts/test_areas.py)"
        )

    def test_names_descriptions_and_fresh_database_flags(self):
        for name, area in test_areas.AREAS.items():
            with self.subTest(area=name):
                self.assertRegex(name, r"^[a-z]+(-[a-z]+)*$")
                self.assertTrue(area.description)
        self.assertTrue(test_areas.AREAS["migrations"].fresh_db)
        self.assertIn("makemigrations", test_areas.AREAS["migrations"].checks)
        self.assertEqual(set(test_areas.CHECK_COMMANDS), {"makemigrations", "system-check"})
        for area in test_areas.AREAS.values():
            for check in area.checks:
                self.assertIn(check, test_areas.CHECK_COMMANDS)


class PlanTests(SimpleTestCase):
    def test_documentation_only_runs_the_repository_checks_only(self):
        result = test_areas.plan(files=["docs/STARTUP.md", "AGENTS.md", ".claude/agents/tester.md"])
        self.assertEqual(result["mode"], "scoped")
        self.assertEqual(result["areas"], ["docs"])
        self.assertEqual(result["labels"], list(test_areas.ALWAYS))
        self.assertFalse(result["fresh_db"])

    def test_a_translation_change_does_not_run_the_migration_area(self):
        result = test_areas.plan(files=["locale/de_CH/LC_MESSAGES/django.po"])
        self.assertEqual(result["areas"], ["translations"])
        self.assertFalse(result["fresh_db"])
        self.assertEqual(result["checks"], [])

    def test_a_migration_needs_a_fresh_database_and_the_missing_migration_check(self):
        result = test_areas.plan(files=["leg_platform/accounts/migrations/0025_example.py"])
        self.assertEqual(result["mode"], "scoped")
        self.assertIn("migrations", result["areas"])
        self.assertTrue(result["fresh_db"])
        self.assertEqual(result["checks"], [test_areas.CHECK_COMMANDS["makemigrations"]])

    def test_a_source_file_selects_its_areas(self):
        result = test_areas.plan(files=[PATH_RESOLUTION])
        self.assertEqual(result["areas"], ["registration"])
        self.assertIn("leg_platform.accounts.tests.test_registration", result["labels"])
        self.assertNotIn("leg_platform.accounts.tests.test_models", result["labels"])

    def test_a_changed_test_module_runs_only_that_module_and_the_repository_checks(self):
        module = "leg_platform/accounts/tests/test_registration.py"
        result = test_areas.plan(files=[module])
        self.assertEqual(result["areas"], [])
        self.assertEqual(
            result["labels"], ["leg_platform.accounts.tests.test_registration", *test_areas.ALWAYS]
        )

    def test_a_deleted_test_module_has_nothing_to_run(self):
        result = test_areas.plan(
            files=["leg_platform/accounts/tests/test_does_not_exist.py", "docs/STARTUP.md"]
        )
        self.assertNotIn("leg_platform.accounts.tests.test_does_not_exist", result["labels"])

    def test_core_and_unmapped_files_run_the_full_suite(self):
        for path in (
            "leg_platform/settings.py",
            "leg_platform/middleware.py",
            "leg_platform/urls.py",
            "leg_platform/accounts/models.py",
            "leg_platform/environment.py",
            "pyproject.toml",
            "leg_platform/new_module_nobody_mapped.py",
            "something/unknown.txt",
        ):
            with self.subTest(path=path):
                result = test_areas.plan(files=[docs_file(), path])
                self.assertEqual(result["mode"], "full")
                self.assertTrue(result["fresh_db"])
                self.assertTrue(result["reasons"])

    def test_no_change_runs_nothing(self):
        self.assertEqual(test_areas.plan(files=[])["mode"], "nothing")

    def test_unknown_area_is_refused(self):
        with self.assertRaises(SystemExit):
            test_areas.plan(area_names=["does-not-exist"])

    def test_named_areas_are_combined_without_duplicates(self):
        result = test_areas.plan(area_names=["registration", "login"])
        self.assertEqual(result["areas"], ["registration", "login"])
        self.assertEqual(len(result["labels"]), len(set(result["labels"])))

    def test_a_package_label_replaces_the_modules_below_it(self):
        self.assertEqual(
            test_areas.minimal_labels(["leg_platform.accounts.tests.test_models", "leg_platform.accounts"]),
            ["leg_platform.accounts"],
        )

    def test_full_is_requested_explicitly(self):
        result = test_areas.plan(full=True)
        self.assertEqual(result["mode"], "full")
        self.assertTrue(result["fresh_db"])

    def test_the_workflow_runs_the_packaging_and_the_deployment_check_tests(self):
        # The stage deployment check of CI has a local counterpart (test_production_settings).
        result = test_areas.plan(files=[".github/workflows/ci.yml"])
        self.assertEqual(result["areas"], ["packaging"])
        self.assertIn("leg_platform.tests.test_production_settings", result["labels"])

    def test_a_test_module_with_an_unusual_name_runs_the_full_suite(self):
        # A label is an argument of ``manage.py test``: only plain dotted names are trusted (L-2).
        for path in (
            "-x/tests/test_a.py",
            "leg_platform/tests/test_--testrunner=x.py",
            "leg_platform/tests/test_a b.py",
            "leg_platform/tests/test_a-b.py",
        ):
            with self.subTest(path=path):
                result = test_areas.plan(files=[path])
                self.assertEqual(result["mode"], "full")
                self.assertTrue(result["reasons"])

    def test_the_label_pattern_accepts_only_plain_dotted_names(self):
        for label in ("leg_platform.tests.test_areas", "_a.b9", "A"):
            self.assertTrue(test_areas.TEST_LABEL.fullmatch(label), label)
        for label in ("-x", ".a", "9a", "a-b", "a b", "a\n", "a=b", ""):
            self.assertFalse(test_areas.TEST_LABEL.fullmatch(label), repr(label))


def entries(*names):
    """The NUL-separated output of ``git ... -z`` for the given entries."""
    return "".join(name + "\0" for name in names).encode("utf-8")


def fake_git(outputs):
    """Replace ``_git_output`` by a function that answers by the first git argument."""

    def run(*arguments):
        return outputs[arguments[0]] if arguments[0] in outputs else b""

    return patch.object(test_areas, "_git_output", side_effect=run)


class ChangedFilesParsingTests(SimpleTestCase):
    """``changed_files`` with fixed git output (the format of ``git status --porcelain=v1 -z``)."""

    def test_entries_are_separated_by_nul_and_names_may_contain_spaces_and_quotes(self):
        status = entries(" M docs/a file.md", "?? it's \"here\".py", "A  new.py")
        with fake_git({"status": status}):
            self.assertEqual(
                test_areas.changed_files(), ["docs/a file.md", "it's \"here\".py", "new.py"]
            )

    def test_a_rename_and_a_copy_count_the_new_and_the_old_path(self):
        status = entries("R  new name.py", "old name.py", "C  copy.py", "source.py", " M other.py")
        with fake_git({"status": status}):
            self.assertEqual(
                test_areas.changed_files(),
                ["copy.py", "new name.py", "old name.py", "other.py", "source.py"],
            )

    def test_a_rename_at_the_end_without_the_old_path_does_not_fail(self):
        with fake_git({"status": entries("R  new.py")}):
            self.assertEqual(test_areas.changed_files(), ["new.py"])

    def test_no_change_gives_no_files(self):
        with fake_git({"status": b""}):
            self.assertEqual(test_areas.changed_files(), [])

    def test_the_files_since_a_revision_are_added(self):
        outputs = {
            "status": entries(" M a.py"),
            "rev-parse": b"0123456789abcdef0123456789abcdef01234567\n",
            "diff": entries("b file.py", "a.py"),
        }
        with fake_git(outputs) as git:
            self.assertEqual(test_areas.changed_files("HEAD~2"), ["a.py", "b file.py"])
        diff = [call.args for call in git.call_args_list if call.args[0] == "diff"][0]
        # The resolved hash, never the text of the command line, reaches ``git diff``.
        self.assertEqual(
            diff,
            ("diff", "--name-only", "-z", "0123456789abcdef0123456789abcdef01234567", "--"),
        )

    def test_a_file_name_that_is_not_utf8_stops_with_a_clear_message(self):
        with fake_git({"status": b" M caf\xe9.py\0"}):
            with self.assertRaises(SystemExit) as context:
                test_areas.changed_files()
        message = str(context.exception)
        self.assertIn("not valid UTF-8", message)
        self.assertIn("-Full", message)


class RevisionValidationTests(SimpleTestCase):
    """``--since`` is checked before it reaches git (L-1); the checks that need no repository."""

    def test_an_option_is_refused_before_git_is_called(self):
        for value in ("--output=x", "-p", "--output=x y", "-"):
            with self.subTest(value=value):
                with patch.object(test_areas, "_git_output") as git:
                    with self.assertRaises(SystemExit) as context:
                        test_areas.verified_revision(value)
                git.assert_not_called()
                self.assertIn("not an option", str(context.exception))

    def test_a_value_with_a_nul_character_is_refused(self):
        with self.assertRaises(SystemExit):
            test_areas.verified_revision("HEAD\0--output=x")

    def test_the_value_is_separated_from_the_options_and_must_be_a_commit(self):
        with patch.object(test_areas, "_git_output", return_value=b"abc123\n") as git:
            self.assertEqual(test_areas.verified_revision("HEAD~3"), "abc123")
        git.assert_called_once_with(
            "rev-parse", "--verify", "--quiet", "--end-of-options", "HEAD~3^{commit}"
        )

    def test_an_unknown_revision_is_refused_with_a_clear_message(self):
        with patch.object(test_areas, "_git_output", side_effect=SystemExit("git failed: ")):
            with self.assertRaises(SystemExit) as context:
                test_areas.verified_revision("no-such-revision")
        self.assertIn("must name an existing commit", str(context.exception))


class RealGitTests(SimpleTestCase):
    """``changed_files`` against a real, throw-away repository (the real output format of git)."""

    def git(self, *arguments):
        result = subprocess.run(
            [
                "git", "-c", "user.name=Test", "-c", "user.email=test@example.org",
                "-c", "commit.gpgsign=false", *arguments,
            ],
            cwd=self.repository, capture_output=True, check=False, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))

    def setUp(self):
        if shutil.which("git") is None:
            message = "Git is not available."
            if os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"):
                self.fail(message)
            self.skipTest(message)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.repository = Path(directory.name)
        self.git("init", "--quiet")
        (self.repository / "docs").mkdir()
        (self.repository / "docs" / "old name.md").write_text("old\n" * 20, encoding="utf-8")
        (self.repository / "kept.py").write_text("kept\n", encoding="utf-8")
        self.git("add", "--all")
        self.git("commit", "--quiet", "-m", "first")
        patcher = patch.object(test_areas, "ROOT", self.repository)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_staged_rename_with_spaces_counts_both_names(self):
        self.git("mv", "docs/old name.md", "docs/new name.md")
        (self.repository / "untracked file.py").write_text("x\n", encoding="utf-8")
        self.assertEqual(
            test_areas.changed_files(),
            ["docs/new name.md", "docs/old name.md", "untracked file.py"],
        )

    def test_the_files_since_a_revision_include_committed_changes(self):
        (self.repository / "kept.py").write_text("changed\n", encoding="utf-8")
        self.git("commit", "--quiet", "-am", "second")
        self.assertEqual(test_areas.changed_files(), [])
        self.assertEqual(test_areas.changed_files("HEAD~1"), ["kept.py"])

    def test_since_refuses_an_option_and_an_unknown_revision(self):
        for value in ("--output=x y", "no-such-revision", "HEAD~99"):
            with self.subTest(value=value):
                with self.assertRaises(SystemExit):
                    test_areas.changed_files(value)
        self.assertFalse((self.repository / "x y").exists())


def docs_file():
    return "docs/STARTUP.md"
