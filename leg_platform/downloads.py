"""The one way a stored PDF leaves the portal (D-103): the contract versions, for the staff and for the customers.

An uploaded file is hostile until proven otherwise (``community.contract_pdf`` checks it by content, not completely), so it is
never shown in a page of the portal and never run in its origin:

* ``Content-Disposition: attachment`` with the name built by the portal (``django.utils.http.content_disposition_header``),
* ``X-Content-Type-Options: nosniff``,
* ``Content-Security-Policy: sandbox; default-src 'none'``,
* ``Cross-Origin-Resource-Policy: same-origin`` (no other site embeds the file),
* ``Referrer-Policy: no-referrer``.

The one exception is ``pdf_inline`` (owner order 2026-10-21, D-108): the customer's reading view of the current contract, shown
in a frame of the contract pages only. It differs in exactly two protective headers (no ``sandbox``, which the PDF viewers of
the browsers refuse; ``frame-ancestors 'self'`` and ``X-Frame-Options: SAMEORIGIN`` instead of the site-wide framing ban) and
sends ``Content-Disposition: inline``. Every other header is the same.

The views that call this are ``management.contract_views.ContractDownloadView``, ``customer.views.ContractDownloadView`` and
(``pdf_inline``) ``customer.views.ContractReadingView``; all are never cached (the management middleware and ``never_cache``).
"""

from django.http import HttpResponse
from django.utils.http import content_disposition_header

PDF_CONTENT_SECURITY_POLICY = "sandbox; default-src 'none'"
# The reading view: no ``sandbox`` (the PDF viewers of the browsers do not run in a sandboxed document), and the file may be
# framed by the own origin only (the CSP replaces the site-wide ``frame-ancestors 'none'`` for this one response).
PDF_INLINE_CONTENT_SECURITY_POLICY = "default-src 'none'; frame-ancestors 'self'"


def _pdf_response(data, name, *, attachment, policy):
    response = HttpResponse(data, content_type="application/pdf")
    response["Content-Disposition"] = content_disposition_header(attachment, name)
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = policy
    response["Cross-Origin-Resource-Policy"] = "same-origin"
    response["Referrer-Policy"] = "no-referrer"
    return response


def pdf_attachment(data, name):
    """The response that serves ``data`` (bytes) as the attachment ``name``, with every protective header."""
    return _pdf_response(data, name, attachment=True, policy=PDF_CONTENT_SECURITY_POLICY)


def pdf_inline(data, name):
    """The response that serves ``data`` (bytes) for reading in a frame of the own site: ``inline``, framing allowed for
    the own origin only (``X-Frame-Options: SAMEORIGIN`` and ``frame-ancestors 'self'``; both replace the site-wide ban for
    this response, which the middleware leaves alone because the header is already set). Callers keep it GET-only, behind
    the access check and never cached."""
    response = _pdf_response(data, name, attachment=False, policy=PDF_INLINE_CONTENT_SECURITY_POLICY)
    response["X-Frame-Options"] = "SAMEORIGIN"
    return response
