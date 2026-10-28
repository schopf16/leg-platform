"""The address suggestions in the change forms of the Django administration (D-111).

The portal forms include ``addresses/widget.html`` under the street and ``addresses/widget_notice.html`` under the town
(``static/js/address-directory.js`` does the rest). The administration draws its fields itself, so the two widgets here
append the same two templates to the street and town inputs and bring the script and the stylesheet with the form's
media (the script is deferred: the media of the administration sit in the head). ``AddressSuggestionsMixin`` gives them
to the change AND add form of a ``ModelAdmin`` (``get_form``); the raw list with ``list_editable``
(``community.raw_tables``) keeps plain inputs, because an overlay does not belong into a table row. Nothing changes who
may edit what: the mixin replaces two widgets and never adds or removes a field.
"""

from django.contrib.admin.widgets import AdminTextInputWidget
from django.forms import Media
from django.forms.widgets import Script
from django.template.loader import render_to_string
from django.utils.safestring import mark_safe

ADDRESS_PARTS = ("street", "house_number", "postal_code", "town")


class _AddressInput(AdminTextInputWidget):
    """A text input of the administration followed by one of the address templates."""

    template = ""

    def __init__(self, attrs=None, *, address_prefix="", address_country=""):
        super().__init__(attrs)
        self.address_prefix = address_prefix
        self.address_country = address_country

    @property
    def media(self):
        return Media(css={"all": ["css/address-suggest.css"]}, js=[Script("js/address-directory.js", defer=True)])

    def context(self):
        return {}

    def render(self, name, value, attrs=None, renderer=None):
        # Everyone who sees the administration is staff: the sentence about an empty directory may be shown.
        return mark_safe(super().render(name, value, attrs, renderer) + render_to_string(self.template, self.context()))


class AddressStreetInput(_AddressInput):
    template = "addresses/widget.html"

    def context(self):
        return {
            "address_prefix": self.address_prefix, "address_country_value": self.address_country, "address_staff": True,
        }


class AddressTownInput(_AddressInput):
    template = "addresses/widget_notice.html"


class AddressSuggestionsMixin:
    """Put before ``ModelAdmin`` in the bases. ``address_prefix`` is the prefix of the four field names (``"sender_"``);
    ``address_country`` is ``"CH"`` where the model has no country field (the field ``country`` is read otherwise)."""

    address_prefix = ""
    address_country = ""

    def get_form(self, request, obj=None, change=False, **kwargs):
        form = super().get_form(request, obj, change=change, **kwargs)
        names = {part: f"{self.address_prefix}{part}" for part in ADDRESS_PARTS}
        # The script needs all four inputs; a form that shows fewer of them stays as it is.
        if all(name in form.base_fields for name in names.values()):
            for part, widget_class in (("street", AddressStreetInput), ("town", AddressTownInput)):
                field = form.base_fields[names[part]]
                widget = widget_class(
                    attrs=field.widget.attrs, address_prefix=self.address_prefix, address_country=self.address_country
                )
                widget.is_required = field.widget.is_required
                field.widget = widget
        return form
