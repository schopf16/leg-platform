"""What the customer is told when a service refuses a step (``ApplicationError.code``, ``PersonError.code``).

One place for the wording. The refusals about other accounts are **neutral**: they never say that something belongs to
someone else, never name an account, a number or a building of another account (D-103, ``docs/SECURITY.md``, section 4).
The owner's words (``LEG-Design-Vorgaben/anmeldungprozess``) are used where there are some; the others are the
developer's proposals.
"""

from django.utils.translation import gettext_lazy as _

# Where the owner gave the text (SCHRITT-1, SCHRITT-2, SCHRITT-3).
NOT_CLEAR = _(
    "This address could not be assigned to one building clearly. Please check your entry or contact the LEG."
)
NUMBER_TOO_LONG = _("The metering point number is too long. Please check it on your last electricity bill.")
NUMBER_EXISTS_HERE = _("This metering point is already created at this building.")
NUMBER_NEUTRAL = _("This metering point number cannot be created. Please contact the LEG.")
IBAN_INVALID = _("The IBAN is not valid. Please check your entry.")
# The deregistration (ABMELDUNG.md): the date is never shown, only that the notice period is not met.
DATE_PAST = _("The date must not be in the past.")
DATE_TOO_EARLY = _("This date is too early. The notice period is four months to the end of a month.")
DATE_NOT_MONTH_END = _("Choose the last day of a month.")
IBAN_NEEDED = _(
    "For the compensation of the feed-in we need your bank or postal connection. Enter it first. "
    "Afterwards you can continue the deregistration."
)
NOT_DEREGISTRABLE = _("One of the metering points cannot be deregistered (any more). Please check your choice.")

MESSAGES = {
    "directory_unavailable": _("The address search is not available right now. Please try again later."),
    "already_in_progress": _("An application for this building is already running."),
    "building_complete": _("You have registered a draw and a feed-in at this building already."),
    "building_unavailable": _("This building cannot be created. Please contact the LEG."),
    "number_unavailable": NUMBER_NEUTRAL,
    "too_many_buildings": _("There are too many buildings in this application."),
    "too_many_applications": _("You have too many applications running. Please finish or cancel one first."),
    "too_many_drafts": _("You have too many drafts. Please finish or cancel one first."),
    "prefix_not_configured": _("Metering point numbers cannot be entered yet. Please contact the LEG."),
    "number_in_application": NUMBER_EXISTS_HERE,
    "number_own": _("You have already registered this metering point."),
    "direction_taken": _("You already have this kind of metering point at this building."),
    "version_changed": _("The contract was replaced in the meantime. Please read the new version."),
    "no_contract": _("The contract is not available yet. Please try again later."),
    "both_ticks_required": _("Please tick both boxes."),
    "contract_missing": _("Please agree to the contract first."),
    "start_invalid": _("Choose as soon as possible or a day that is not too early."),
    "not_editable": _("This application cannot be changed any more."),
    "wrong_state": _("This is not possible any more."),
    "step_not_available": _("This step is not available yet."),
    "building_fixed": _("This building is fixed and cannot be changed."),
    "rate_limited": _("You changed your bank connection too often. Please try again later."),
    "iban_required": _("Enter your IBAN."),
    "not_active_yet": NOT_DEREGISTRABLE,
    "nothing_to_deregister": NOT_DEREGISTRABLE,
    "too_many_deregistrations": _(
        "You have too many deregistrations running. Please wait until the LEG has processed them."
    ),
    "date_before_earliest": DATE_TOO_EARLY,
    "not_month_end": DATE_NOT_MONTH_END,
}
FALLBACK = _("This is not possible right now.")


def message(code):
    """The customer's text for a refusal code."""
    return MESSAGES.get(code, FALLBACK)
