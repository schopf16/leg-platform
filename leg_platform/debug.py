"""Exception reporter filter of LEG-Platform.

Django's ``SafeExceptionReporterFilter`` masks settings and ``request.META`` values whose names
match ``hidden_settings`` on debug pages and in error reports. Its default pattern does not
cover ``EMAIL_ALLOWED_RECIPIENTS``, which holds personal email addresses on development
machines (docs/DECISIONS.md, D-028). This filter extends Django's own pattern with
``RECIPIENTS``, so that terms Django adds in later versions are kept.

https://docs.djangoproject.com/en/6.0/howto/error-reporting/
"""

import re

from django.views.debug import SafeExceptionReporterFilter

HIDDEN_SETTINGS_PATTERN = SafeExceptionReporterFilter.hidden_settings.pattern + "|RECIPIENTS"


class ExceptionReporterFilter(SafeExceptionReporterFilter):
    hidden_settings = re.compile(HIDDEN_SETTINGS_PATTERN, flags=re.IGNORECASE)
