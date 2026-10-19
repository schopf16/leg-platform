"""The one way a stored PDF leaves the portal (D-103): the contract versions, for the staff and for the customers.

An uploaded file is hostile until proven otherwise (``community.contract_pdf`` checks it by content, not completely), so it is
never shown in a page of the portal and never run in its origin:

* ``Content-Disposition: attachment`` with the name built by the portal (``django.utils.http.content_disposition_header``),
* ``X-Content-Type-Options: nosniff``,
* ``Content-Security-Policy: sandbox; default-src 'none'``,
* ``Cross-Origin-Resource-Policy: same-origin`` (no other site embeds the file),
* ``Referrer-Policy: no-referrer``.

The views that call this are ``management.contract_views.ContractDownloadView`` and ``customer.views.ContractDownloadView``;
both are never cached (the management middleware and ``never_cache``).
"""

from django.http import HttpResponse
from django.utils.http import content_disposition_header

PDF_CONTENT_SECURITY_POLICY = "sandbox; default-src 'none'"


def pdf_attachment(data, name):
    """The response that serves ``data`` (bytes) as the attachment ``name``, with every protective header."""
    response = HttpResponse(data, content_type="application/pdf")
    response["Content-Disposition"] = content_disposition_header(True, name)
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = PDF_CONTENT_SECURITY_POLICY
    response["Cross-Origin-Resource-Policy"] = "same-origin"
    response["Referrer-Policy"] = "no-referrer"
    return response
