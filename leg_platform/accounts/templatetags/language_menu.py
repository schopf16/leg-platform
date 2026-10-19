"""The language menu and the ``lang`` attribute of a page (D-056).

The menu lists ``settings.LANGUAGES``; the names are written in their own language and are not
translated. The active language is the one that ``leg_platform.language`` activated for the
request (the language cookie, else the instance default). Works without a request, so the
500 page can use it as well.
"""

from types import SimpleNamespace

from django import template
from django.conf import settings
from django.utils import translation

register = template.Library()

# The flag file below ``static/img/flags/`` for the languages the portal offers. A language added
# to ``settings.LANGUAGES`` without a flag is shown without one.
FLAGS = {"de-ch": "de.svg", "fr": "fr.svg", "it": "it.svg", "en": "gb.svg"}


def html_lang(code):
    """The language tag of the ``lang`` attribute: ``de-ch`` becomes ``de-CH`` (BCP 47 spelling)."""
    language, *subtags = code.split("-")
    return "-".join([language.lower(), *(tag.upper() if len(tag) == 2 else tag for tag in subtags)])


def short_name(code):
    """The two letters on the button: ``de-ch`` becomes ``DE``."""
    return code.split("-")[0].upper()


@register.simple_tag
def language_menu():
    """``current`` and ``items`` (code, name, short, flag, lang, is_current) of the language menu."""
    active = translation.get_language() or settings.LANGUAGE_CODE
    items = [
        SimpleNamespace(
            code=code,
            name=name,
            short=short_name(code),
            flag=FLAGS.get(code, ""),
            lang=html_lang(code),
            is_current=code == active,
        )
        for code, name in settings.LANGUAGES
    ]
    current = next((item for item in items if item.is_current), None)
    if current is None:
        # An active language that the menu does not offer: show the default and mark nothing as chosen.
        current = next((item for item in items if item.code == settings.LANGUAGE_CODE), items[0])
    return SimpleNamespace(current=current, items=items)


@register.simple_tag
def html_language():
    """The value of ``<html lang>`` for the active language."""
    return html_lang(translation.get_language() or settings.LANGUAGE_CODE)
