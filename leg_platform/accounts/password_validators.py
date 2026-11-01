"""Password validators of the portal that only change the wording of Django's own checks (D-004)."""

from django.contrib.auth.password_validation import CommonPasswordValidator
from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _


class PlainCommonPasswordValidator(CommonPasswordValidator):
    """The check of Django's list of common passwords, with a message that says what to do next."""

    def validate(self, password, user=None):
        try:
            super().validate(password, user)
        except ValidationError as error:
            raise ValidationError(
                _("This password is used very often. Please choose another one, for example a sentence of several words."),
                code=error.code,
            ) from None
