"""The Graph test-send abuse budget (D-048)."""

from unittest.mock import patch
from uuid import UUID

from django.contrib import messages as django_messages
from django.contrib.messages import get_messages
from django.test import TestCase, override_settings, tag
from django.urls import reverse
from django.utils.translation import gettext as _

from leg_platform.accounts.models import (
    GraphConfiguration,
    RateLimitBucket,
    SecurityEvent,
)
from leg_platform.accounts.rate_limits import MAIL_ADDRESS_LIMITS
from leg_platform.accounts.tests.helpers import (
    VerifiedClient,
    create_active_user,
    hour_and_day_limits,
)
from leg_platform.environment import EMAIL_MODE_CONSOLE, EMAIL_MODE_GRAPH


@tag("mariadb")
@hour_and_day_limits
class GraphTestEmailLimitTests(TestCase):
    client_class = VerifiedClient

    def setUp(self):
        self.administrator = create_active_user("admin@example.org")
        self.administrator.set_privileges(is_staff=True, is_superuser=True)
        self.client.force_login(self.administrator)
        self.configuration = GraphConfiguration.objects.create(
            id=1,
            tenant_id=UUID("00000000-0000-0000-0000-000000000001"),
            client_id=UUID("00000000-0000-0000-0000-000000000002"),
            sender_address="sender@example.org",
            certificate_pem="public certificate",
            encrypted_private_key="encrypted private key",
        )
        self.send_url = reverse("admin:accounts_graphconfiguration_send_test")

    @override_settings(EMAIL_MODE=EMAIL_MODE_GRAPH, EMAIL_ALLOWED_RECIPIENTS=[])
    def test_the_shared_address_budget_allows_exactly_twelve_test_emails(self):
        with patch(
            "leg_platform.accounts.admin.graph.GraphEmailBackend.send_messages", return_value=1
        ) as send:
            for _attempt in range(MAIL_ADDRESS_LIMITS[0][0]):
                response = self.client.post(self.send_url)
                self.assertEqual(response.status_code, 302)

            blocked = self.client.post(self.send_url)

        self.assertEqual(send.call_count, MAIL_ADDRESS_LIMITS[0][0])
        self.assertEqual(
            SecurityEvent.objects.filter(
                event=SecurityEvent.Event.GRAPH_TEST_EMAIL_REQUESTED,
                actor_id=self.administrator.pk,
            ).count(),
            MAIL_ADDRESS_LIMITS[0][0] + 1,
        )
        self.assertEqual(RateLimitBucket.objects.count(), 1)
        feedback = list(get_messages(blocked.wsgi_request))
        # The earlier successful sends left their messages in the session; the refusal is last.
        self.assertEqual(feedback[-1].level, django_messages.ERROR)
        self.assertEqual(
            str(feedback[-1].message),
            str(_("The test email limit has been reached. Try again later.")),
        )

    @override_settings(EMAIL_MODE=EMAIL_MODE_CONSOLE, EMAIL_ALLOWED_RECIPIENTS=[])
    def test_console_mode_refuses_without_calling_graph_or_claiming_success(self):
        with patch(
            "leg_platform.accounts.admin.graph.GraphEmailBackend.send_messages"
        ) as send:
            response = self.client.post(self.send_url)

        send.assert_not_called()
        feedback = [str(message.message) for message in get_messages(response.wsgi_request)]
        refusal = str(
            _(
                "In console mode, no test email is sent, so the Microsoft Graph delivery is "
                "not tested."
            )
        )
        self.assertIn(refusal, feedback)
        sent = str(_("A test email was sent to %(address)s.")) % {
            "address": self.administrator.email
        }
        self.assertNotIn(sent, feedback)
