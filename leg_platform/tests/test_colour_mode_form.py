"""Pages that send no referrer carry no form that posts elsewhere (D-054).

With ``Referrer-Policy: no-referrer`` a browser sends ``Origin: null`` on a form POST and the CSRF
check refuses it, so the colour-mode form of the footer is left out of those pages.
"""

from django.template.loader import render_to_string
from django.test import RequestFactory, SimpleTestCase


class ColourModeFormTests(SimpleTestCase):
    def render(self, no_referrer_page):
        request = RequestFactory().get("/")
        if no_referrer_page:
            request.no_referrer_page = True
        return render_to_string("_colour_mode.html", {"request": request, "colour_mode": "auto"})

    def test_the_form_is_shown_on_normal_pages(self):
        self.assertIn('action="/appearance/"', self.render(False))

    def test_the_form_is_left_out_of_pages_without_a_referrer(self):
        self.assertNotIn("<form", self.render(True))
