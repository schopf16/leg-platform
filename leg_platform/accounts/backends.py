"""Authentication backends for the account model."""

from django.contrib.auth.backends import ModelBackend


class EmailModelBackend(ModelBackend):
    """Authenticate through the user's normalised email identifier."""

    def authenticate(self, request, username=None, password=None, **kwargs):
        # Preserve ModelBackend's dummy hash for unknown users without passing an empty value
        # into User.set_password(), which deliberately rejects empty passwords.
        if password == "":
            password = " "
        return super().authenticate(request, username=username, password=password, **kwargs)