"""Tests of the early request body limit of open endpoints (security review of 2026-10-14)."""

from unittest.mock import Mock, PropertyMock, patch

from django.conf import settings
from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase

from leg_platform.middleware import BODY_LIMITS, RequestBodyLimitMiddleware

LOOKUP = "/addresses/lookup/"


class RequestBodyLimitTests(SimpleTestCase):
    def setUp(self):
        self.passed = Mock(return_value=HttpResponse("view"))
        self.middleware = RequestBodyLimitMiddleware(self.passed)
        self.factory = RequestFactory()

    def post(self, length, path=LOOKUP):
        request = self.factory.post(path, data=b"", content_type="application/x-www-form-urlencoded")
        request.META["CONTENT_LENGTH"] = length
        return self.middleware(request)

    def test_a_body_at_the_limit_passes_and_above_it_is_refused_before_the_view(self):
        limit = BODY_LIMITS[LOOKUP]

        self.assertEqual(self.post(str(limit)).status_code, 200)
        self.passed.reset_mock()
        self.assertEqual(self.post(str(limit + 1)).status_code, 413)
        self.passed.assert_not_called()

    def test_an_invalid_or_negative_length_is_refused(self):
        for value in ("abc", "-1", "1e3", "9" * 40 + "x"):
            with self.subTest(value=value):
                self.assertEqual(self.post(value).status_code, 400)
        self.passed.assert_not_called()

    def test_a_huge_length_is_refused_without_reading_the_body(self):
        request = self.factory.post(LOOKUP, data=b"", content_type="application/x-www-form-urlencoded")
        request.META["CONTENT_LENGTH"] = str(10**12)

        with patch.object(type(request), "body", new_callable=PropertyMock) as body:
            response = self.middleware(request)

        self.assertEqual(response.status_code, 413)
        body.assert_not_called()

    def test_other_paths_and_methods_are_not_limited(self):
        self.assertEqual(self.post(str(10**6), path="/login/").status_code, 200)
        request = self.factory.get(LOOKUP)
        request.META["CONTENT_LENGTH"] = str(10**6)
        self.assertEqual(self.middleware(request).status_code, 200)

    def test_a_missing_or_empty_length_passes_and_nothing_is_read(self):
        request = self.factory.post(LOOKUP, data=b"", content_type="application/x-www-form-urlencoded")
        request.META.pop("CONTENT_LENGTH", None)
        self.assertEqual(self.middleware(request).status_code, 200)
        request.META["CONTENT_LENGTH"] = ""
        self.assertEqual(self.middleware(request).status_code, 200)

    def test_put_and_patch_are_limited_too(self):
        for method in ("put", "patch"):
            with self.subTest(method=method):
                request = getattr(self.factory, method)(
                    LOOKUP, data=b"", content_type="application/x-www-form-urlencoded"
                )
                request.META["CONTENT_LENGTH"] = str(BODY_LIMITS[LOOKUP] + 1)
                self.assertEqual(self.middleware(request).status_code, 413)

    def test_only_the_exact_path_matches_and_other_spellings_do_not_resolve(self):
        from django.urls import Resolver404, resolve

        self.assertEqual(resolve(LOOKUP).view_name, "addresses:lookup")
        for path in ("//addresses/lookup/", "/Addresses/lookup/", "/addresses/lookup/x/"):
            with self.subTest(path=path), self.assertRaises(Resolver404):
                resolve(path)

    def test_the_middleware_runs_before_anything_that_reads_the_body(self):
        order = list(settings.MIDDLEWARE)
        name = "leg_platform.middleware.RequestBodyLimitMiddleware"

        self.assertEqual(order.count(name), 1)
        for later in (
            "django.contrib.sessions.middleware.SessionMiddleware",
            "django.middleware.csrf.CsrfViewMiddleware",
            "django.contrib.auth.middleware.AuthenticationMiddleware",
        ):
            self.assertLess(order.index(name), order.index(later))

    def test_an_oversized_post_through_the_full_stack_is_refused_without_csrf_processing(self):
        from django.test import Client

        client = Client(enforce_csrf_checks=True)
        response = client.post(
            LOOKUP,
            data="street=" + "x" * 5000,
            content_type="application/x-www-form-urlencoded",
        )

        self.assertEqual(response.status_code, 413)
