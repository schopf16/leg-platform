"""The one-time banners of the customer's start page and the page Buildings (D-103, design walkthrough W2-03).

After an event that removed or changed the application (a rejection, a lapse because the grid operator's confirmation did
not arrive) the domain writes a ``CustomerMessage`` (kind, optional note of the staff, times; nothing else). The next visit
of the start page or the page Buildings shows it **once**. The words are in the template ``customer/_banners.html`` (one
sentence per kind, in plain words); this module only reads and marks.

* **Reading does not mark.** ``pending`` reads the messages; the view builds the whole response first and only then calls
  ``mark_shown``, and only for a real page view: a ``HEAD`` request, a prefetch (``Purpose`` / ``Sec-Purpose``) or a
  page that failed to render never consumes a message.
* The note of the staff is plain text: the template escapes it (never markup).
* Read through the account only (``customer_messages`` filters by person).
"""

from dataclasses import dataclass, field

from leg_platform.community import customer_messages

Kind = customer_messages.Kind
# The kinds that have a banner of their own text; any other kind is marked seen without a banner.
SHOWN = (Kind.APPLICATION_REJECTED, Kind.APPLICATION_REJECTED_OPERATOR, Kind.OPERATOR_NOT_CONFIRMED)


@dataclass(frozen=True)
class Banner:
    kind: str
    note: str = ""  # the optional reason the staff wrote; plain text


@dataclass(frozen=True)
class Pending:
    banners: list = field(default_factory=list)
    ids: tuple = ()  # every unseen message that was read (shown or not): these are marked after the page was sent


def pending(person):
    """The unseen messages of the account as banners; nothing is marked yet."""
    unseen = customer_messages.messages_for(person)
    banners = [Banner(message.kind, message.text) for message in unseen if message.kind in SHOWN]
    return Pending(banners, tuple(message.pk for message in unseen))


def is_page_view(request):
    """A real visit: a GET that is not a prefetch or a preview of the browser."""
    if request.method != "GET":
        return False
    purpose = f"{request.headers.get('Purpose', '')} {request.headers.get('Sec-Purpose', '')}".lower()
    return "prefetch" not in purpose and "prerender" not in purpose


def mark_shown(request, person, read):
    """Mark what ``pending`` read, after the response was built, for a real page view only."""
    if read.ids and is_page_view(request):
        customer_messages.mark_seen(person, read.ids)
