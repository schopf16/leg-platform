"""Printing of the lists of the management area as PDF (D-082; ``LISTEN-STANDARD.md``, section 10).

* ``document``: the plain description of what is printed (texts only, no markup, no request).
* ``pdf``: draws a document with ReportLab, in memory.
* ``areas``: builds a document for each list (persons, locations, LEGs, metering points) from the
  address of the list and the account, with the same rules as the pages.
* ``views``: the dialog "Print list" and the response with the PDF.
"""
