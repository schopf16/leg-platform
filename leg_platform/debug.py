"""Exception reporter filter of LEG-Platform.

Django's ``SafeExceptionReporterFilter`` masks settings and ``request.META`` values whose names
match ``hidden_settings`` on debug pages and in error reports. Its default pattern does not
cover ``EMAIL_ALLOWED_RECIPIENTS``, which holds personal email addresses on development
machines (docs/DECISIONS.md, D-028). This filter adds ``RECIPIENTS`` to the pattern.

https://docs.djangoproject.com/en/6.0/howto/error-reporting/
"""

import re

from django.views.debug import SafeExceptionReporterFilter

# Django's default pattern (django/views/debug.py, 6.0.9 and 6.1.2) plus RECIPIENTS.
HIDDEN_SETTINGS_PATTERN = r"API|AUTH|TOKEN|KEY|SECRET|PASS|SIGNATURE|HTTP_COOKIE|RECIPIENTS"


class ExceptionReporterFilter(SafeExceptionReporterFilter):
    hidden_settings = re.compile(HIDDEN_SETTINGS_PATTERN, flags=re.IGNORECASE)
