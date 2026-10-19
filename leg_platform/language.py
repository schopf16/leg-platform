"""The language of a page: chosen by the person, never guessed (D-010, D-056).

* ``ChosenLanguageMiddleware`` takes the language only from the language cookie and falls back to
  ``settings.LANGUAGE_CODE``. Django's ``LocaleMiddleware`` would also read the ``Accept-Language``
  header of the browser (``get_language_from_request``), so that a browser set to French would
  change the default of an instance (https://docs.djangoproject.com/en/6.1/topics/i18n/translation/#how-django-discovers-language-preference).
* ``set_language`` is the one endpoint that sets the cookie: Django's view, but POST only and for a
  language of ``settings.LANGUAGES`` only.
* ``install_english_fallback`` removes Django's fallback to ``LANGUAGE_CODE``: a text that a
  French or Italian catalogue does not have is shown in its English source, never in German.
"""

from django.conf import settings
from django.http import HttpResponse
from django.middleware.locale import LocaleMiddleware
from django.utils import translation
from django.utils.cache import patch_vary_headers
from django.utils.translation.trans_real import DjangoTranslation
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST
from django.views.i18n import LANGUAGE_QUERY_PARAMETER
from django.views.i18n import set_language as django_set_language


def chosen_language(request):
    """The language of the language cookie if it is one of ``settings.LANGUAGES``, else the instance default."""
    code = request.COOKIES.get(settings.LANGUAGE_COOKIE_NAME)
    if code is not None and code in dict(settings.LANGUAGES) and translation.check_for_language(code):
        return code
    return settings.LANGUAGE_CODE


class ChosenLanguageMiddleware(LocaleMiddleware):
    """Activates the chosen language for the request (after the session, before the common middleware).

    Subclass of Django's ``LocaleMiddleware``: only the choice of the language differs. The portal
    uses no language prefix in its URLs, so the redirect logic of the parent is not needed.
    """

    def process_request(self, request):
        translation.activate(chosen_language(request))
        request.LANGUAGE_CODE = translation.get_language()

    def process_response(self, request, response):
        # The public presentation files (logo, favicon) do not depend on the language and must
        # carry no "Vary: Cookie" (D-073).
        from leg_platform.accounts.second_factor import SESSION_FREE_VIEWS

        match = getattr(request, "resolver_match", None)
        if match is not None and match.view_name in SESSION_FREE_VIEWS:
            return response
        # The page depends on the cookie, not on the Accept-Language header.
        patch_vary_headers(response, ("Cookie",))
        response.headers.setdefault("Content-Language", translation.get_language())
        return response


@require_POST
@never_cache
def set_language(request):
    """Sets the language cookie and redirects (Django's ``set_language`` view).

    POST only: Django's CSRF middleware checks the token. Django also checks ``next`` with
    ``url_has_allowed_host_and_scheme`` (own host, https if the request is https) and falls back to
    the referrer and to ``/``. Unlike Django's view, a language that is not in ``settings.LANGUAGES``
    is refused with 400 instead of being ignored: Django accepts any code that has some catalogue.
    The view reads no session and no account.
    """
    if request.POST.get(LANGUAGE_QUERY_PARAMETER) not in dict(settings.LANGUAGES):
        return HttpResponse(status=400)
    return django_set_language(request)


def _without_fallback(self, localedirs=None):
    """Replaces ``DjangoTranslation._add_fallback``: no fallback to the catalogue of ``LANGUAGE_CODE``."""


def install_english_fallback():
    """A text without a translation is shown in English, the language of the source texts.

    Django adds the catalogue of ``LANGUAGE_CODE`` (Swiss German) as the fallback of every other
    language except English (``DjangoTranslation._add_fallback``). There is no setting for it, so a
    French page would show German for every text that its catalogue lacks. Without the fallback
    ``gettext`` returns the message ID, which is the English source text. The default language never
    had a fallback. ``leg_platform/tests/test_language.py`` fails if a Django update changes this.
    """
    DjangoTranslation._add_fallback = _without_fallback
