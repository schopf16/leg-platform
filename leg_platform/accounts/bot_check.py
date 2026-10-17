"""A cheap first protection of the forms that ask for an email (D-038, D-058): a hidden field a
person never fills and a signed time stamp of the form.

A script that posts at once or fills every field is recognised; a hit gets the same page as a real
request, without email or job, so it learns nothing. Level 0 only (proof of work is not built). The
visible refusal (HTTP 429) is reserved for the client-address and instance limits.
"""

import time

from django import forms
from django.conf import settings
from django.core import signing

SALT = "leg-platform.mail-request-form"
# A person needs more than this to read and fill a form; a script is faster.
MINIMUM_SECONDS = 2
# A form that was open for longer than this is treated as a hit (the signature has expired).
MAXIMUM_SECONDS = 3 * 24 * 3600
HONEYPOT_FIELD = "contact_fax_number"
STAMP_FIELD = "form_started"


def new_stamp():
    """The signed time at which a form was made."""
    return signing.dumps(time.time(), salt=SALT)


def looks_automated(honeypot_value, stamp):
    """Whether a posted form was filled in the hidden field or posted too early, too late or
    without a valid stamp."""
    if honeypot_value:
        return True
    try:
        started = signing.loads(stamp or "", salt=SALT, max_age=MAXIMUM_SECONDS)
    except signing.BadSignature:
        return True
    if not isinstance(started, (int, float)):
        return True
    minimum = getattr(settings, "MAIL_FORM_MINIMUM_SECONDS", MINIMUM_SECONDS)
    return time.time() - started < minimum


class BotCheckMixin:
    """Adds the hidden field and the stamp to a form; ``automated`` tells the view about a hit.

    The field has no visible label and is hidden by a style sheet class (the CSP allows no style
    attribute); it is out of the tab order, assistive technology and autofill.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields[HONEYPOT_FIELD] = forms.CharField(
            required=False,
            label="",
            widget=forms.TextInput(
                attrs={"tabindex": "-1", "autocomplete": "off", "aria-hidden": "true", "class": ""}
            ),
        )
        self.fields[STAMP_FIELD] = forms.CharField(required=False, widget=forms.HiddenInput())
        self.initial[STAMP_FIELD] = new_stamp()
        self.automated = False

    def clean(self):
        cleaned_data = super().clean()
        self.automated = looks_automated(
            self.data.get(self.add_prefix(HONEYPOT_FIELD), ""),
            self.data.get(self.add_prefix(STAMP_FIELD), ""),
        )
        return cleaned_data
