"""The scheduled cleanup tasks (docs/INSTALLATION.md, "Scheduled tasks"; D-018, D-037, D-041)."""

from datetime import timedelta
from io import StringIO
from pathlib import Path
from unittest import mock

from django.contrib.sessions.models import Session
from django.core.management import CommandError, call_command
from django.test import TestCase, tag
from django.utils import timezone

from leg_platform.accounts.management.commands import run_scheduled_tasks
from leg_platform.accounts.tests.helpers import create_active_user

COMMANDS = Path(run_scheduled_tasks.__file__).parent


@tag("mariadb")
class ScheduledTasksTests(TestCase):
    def test_every_cleanup_command_is_part_of_the_scheduled_tasks(self):
        cleanup_commands = {
            path.stem
            for path in COMMANDS.glob("*.py")
            if path.stem.startswith(("cleanup_", "delete_expired_"))
        }
        self.assertTrue(cleanup_commands)
        self.assertLessEqual(cleanup_commands, set(run_scheduled_tasks.TASKS))

    def test_the_tasks_run_and_remove_what_expired(self):
        user = create_active_user()
        session = Session.objects.create(
            session_key="expired", session_data="x", expire_date=timezone.now() - timedelta(days=1)
        )
        out = StringIO()
        call_command("run_scheduled_tasks", stdout=out)
        self.assertFalse(Session.objects.filter(pk=session.pk).exists())
        for name in ("expired login IP attempts", "security log entries", "stale mail jobs"):
            self.assertIn(name, out.getvalue())
        self.assertTrue(user.pk)

    def test_a_failing_task_does_not_stop_the_others_and_is_reported(self):
        called = []

        def fake(task, **kwargs):
            called.append(task)
            if task == "cleanup_mail_requests":
                raise RuntimeError("secret detail")

        err = StringIO()
        with mock.patch.object(run_scheduled_tasks, "call_command", side_effect=fake):
            with self.assertRaises(CommandError) as raised:
                call_command("run_scheduled_tasks", stdout=StringIO(), stderr=err)
        self.assertEqual(called, list(run_scheduled_tasks.TASKS))
        self.assertIn("cleanup_mail_requests", str(raised.exception))
        self.assertIn("RuntimeError", err.getvalue())
        self.assertNotIn("secret detail", err.getvalue())
