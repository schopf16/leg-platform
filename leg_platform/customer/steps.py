"""The step names and words that the customer's pages share (design walkthrough W1-01, W1-02, W1-14).

One list, one wording: the wizard, the card on the start page, the card of a building and the page "Your application was
submitted" use the same names. Before the submission the steps are the wizard's (``STEP_NAMES``); after it they are the five
steps of ``applications.post_submit_progress`` (``post_submit_steps``).
"""

from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy as _lazy

from leg_platform.community import applications

STEP_NAMES = {
    applications.BUILDING: _lazy("Building"),
    applications.METERING_POINTS: _lazy("Metering points"),
    applications.BANK_CONNECTION: _lazy("Bank or postal connection"),
    applications.CONTRACT: _lazy("Contract"),
    applications.START: _lazy("Start date"),
}


def state_word(state):
    """The word of a step state: finished, next up or open."""
    return {applications.DONE: _("Finished"), "next": _("Next up")}.get(state, _("Open"))


def post_submit_steps(application, operator):
    """``[(key, state, label, word)]`` of the five steps after the submission, with the grid operator's name.

    The fourth step is the customer's own (the confirmation in the email of the grid operator: "Open" until it is
    done); the fifth is the start date, which the grid operator sets."""
    labels = {
        "submitted": _("Details submitted"),
        "review": _("Check by the LEG"),
        "operator_registration": _("Registration at %(operator)s") % {"operator": operator},
        "operator_confirmation": _("You confirm the request in the email from %(operator)s") % {"operator": operator},
        "start": _("Start date"),
    }
    rows = []
    for key, state in applications.post_submit_progress(application):
        word = state_word(state)
        if key == "operator_confirmation" and state != applications.DONE:
            word = _("Open")
        elif key == "start" and state != applications.DONE:
            word = _("Set by %(operator)s") % {"operator": operator}
        rows.append((key, state, labels[key], word))
    return rows
