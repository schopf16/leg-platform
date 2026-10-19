"""Raw tables of the management data in the Django administration (D-091).

Two faces of one admin class:

* A staff account with the ``view`` right of a table gets the plain, read-only table with the leak rule: the
  explicit ``staff_list_display`` and ``staff_fields`` only, no free text, no IBAN, no names or emails.
* A system administrator (``is_superuser``, checked on the server) edits the table like a spreadsheet: every
  column that is a real model field is an input of the list (one "Save" for the page), an empty "new row" at the top
  adds a row (``add_row_view``, no form page), a single row can be deleted. Nothing is checked beyond what the
  database and Django's field types need: no rule of the management area, no service layer, no history entry
  (owner decision 2026-10-19, accepted risk).

Left out on purpose: the primary key, the automatic timestamps, the encrypted IBAN (editing the ciphertext would
destroy it) and the address digest (``Location.save`` recomputes it).

Audit: Django's ``LogEntry`` for every add, change and delete, plus one security event per request
(``management_data_edited``: the table, the row ids and the field names, never a value).
"""

import logging
from contextlib import suppress

from django import forms
from django.contrib import admin, messages
from django.contrib.admin.utils import NestedObjects, flatten_fieldsets
from django.core.exceptions import FieldDoesNotExist, PermissionDenied, ValidationError
from django.db import DataError, IntegrityError, OperationalError, models, router, transaction
from django.http import HttpResponseRedirect
from django.urls import path, reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext_lazy as _

from leg_platform.accounts import security_log
from leg_platform.accounts.models import SecurityEvent

logger = logging.getLogger(__name__)

NEW_ROW_PREFIX = "new"
NEW_ROW_FORM_ID = "raw-new-row-form"
# Never an input: the ciphertext would be destroyed by editing it; the digest is recomputed by ``Location.save``.
NEVER_EDITABLE = frozenset({"iban_encrypted", "address_digest"})
MAX_LOGGED_ROWS = 100
# A refusal by the database is a message, never a 500.
DATABASE_ERRORS = (IntegrityError, DataError, OperationalError)

DATABASE_REFUSED = _(
    "The database refused the row or the change (for example a value that exists already, a missing related "
    "row or a value that does not fit). Nothing was saved."
)


def may_edit(request):
    """Only an active system administrator edits the raw tables; the server decides, never a group name."""
    user = getattr(request, "user", None)
    return bool(user is not None and user.is_active and user.is_superuser)


def _is_stamp(field):
    return bool(getattr(field, "auto_now", False) or getattr(field, "auto_now_add", False))


def raw_clean_fields(instance, exclude=None):
    """What the database and Django's field types need, and no rule of the application.

    Like ``Model.clean_fields`` (type, required, choices, length, digits, range of the integer type), but without the
    validators a model declares itself (``validators=[...]``: number formats, a minimum value, the length of a remark).
    """
    errors = {}
    for field in instance._meta.fields:
        if field.name in (exclude or ()) or field.generated:
            continue
        raw_value = getattr(instance, field.attname)
        if field.blank and raw_value in field.empty_values:
            continue
        try:
            value = field.to_python(raw_value)
            field.validate(value, instance)
            problems = []
            for validator in field.validators:
                if validator in field._validators:
                    continue
                try:
                    validator(value)
                except ValidationError as error:
                    if getattr(error, "code", None) in field.error_messages:
                        error.message = field.error_messages[error.code]
                    problems.extend(error.error_list)
            if problems:
                raise ValidationError(problems)
            setattr(instance, field.attname, value)
        except ValidationError as error:
            errors[field.name] = error.error_list
    if errors:
        raise ValidationError(errors)


class RawRowForm(forms.ModelForm):
    """Base of every row form: also the model fields with ``editable=False`` are inputs (declared per request).

    A ``ModelForm`` skips such fields (digest-like state, suggestion state, undo columns, log times). The raw
    table shows them as declared form fields and copies the cleaned value to the instance before the model checks.
    The model checks are the raw ones: no validator of the model and no ``Model.clean`` (a rule of the application);
    uniqueness and the constraints of the table stay, because the database enforces them anyway.
    """

    extra_model_fields = {}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.instance._state.adding:
            for name, field in self.extra_model_fields.items():
                self.initial[name] = field.value_from_object(self.instance)

    def clean(self):
        cleaned = super().clean()
        # An emptied JSON input is None: a not-null column with a default (for example ``{}``) takes its default.
        for name, value in list(cleaned.items()):
            if value is not None:
                continue
            with suppress(FieldDoesNotExist):
                field = self._meta.model._meta.get_field(name)
                if not field.null and field.has_default():
                    cleaned[name] = field.get_default()
        return cleaned

    def _post_clean(self):
        for name in self.extra_model_fields:
            if name in self.cleaned_data:
                setattr(self.instance, name, self.cleaned_data[name])
        instance = self.instance
        # ``full_clean`` of the parent calls these two: the raw version, and no ``clean()``, for this one call.
        instance.clean_fields = lambda exclude=None: raw_clean_fields(instance, exclude)
        instance.clean = lambda: None
        try:
            super()._post_clean()
        finally:
            del instance.clean_fields, instance.clean


class RawTableAdmin(admin.ModelAdmin):
    """A table of the management data: read-only for the ``view`` right, raw and editable for system administrators.

    Subclasses name the leak-safe layout of the read-only face (``staff_list_display``, ``staff_fields``) and
    leave everything else (columns, inputs, widgets) to this class.
    """

    actions = None
    list_per_page = 50
    show_full_result_count = False
    change_list_template = "admin/community/raw_change_list.html"
    form = RawRowForm
    staff_list_display = ()
    staff_fields = ()
    # Read-only columns that are no model field (for example a derived status), shown after the inputs.
    derived_columns = ()
    formfield_overrides = {
        models.DateField: {"widget": forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"})},
        models.DateTimeField: {
            "form_class": forms.DateTimeField,
            "widget": forms.DateTimeInput(format="%Y-%m-%dT%H:%M:%S", attrs={"type": "datetime-local"}),
        },
        models.TextField: {"widget": forms.Textarea(attrs={"rows": 2, "cols": 30, "class": "vLargeTextField"})},
        models.JSONField: {"widget": forms.Textarea(attrs={"rows": 2, "cols": 30, "class": "vLargeTextField"})},
    }

    def __init__(self, model, admin_site):
        super().__init__(model, admin_site)
        fields = [
            f for f in model._meta.concrete_fields
            if not f.primary_key and f.name not in NEVER_EDITABLE and not _is_stamp(f)
        ]
        pk = model._meta.pk.name
        stamps = tuple(f.name for f in model._meta.concrete_fields if _is_stamp(f))
        self.raw_extras = tuple(f.name for f in fields if not f.editable)
        self.raw_readonly = (pk, *self.derived_columns, *stamps)
        # The first column (the primary key, read-only) links to the page of the row, so no input is a link.
        self.list_editable = tuple(f.name for f in fields if f.editable)
        self.list_display = (pk, *self.list_editable, *self.raw_extras, *self.derived_columns, *stamps)
        self.list_display_links = (pk,)
        self.fields = self.list_display
        self.raw_id_fields = tuple(f.name for f in model._meta.concrete_fields if f.is_relation)

    # Access: explicit, and only for an active system administrator (``default_permissions = ("view",)`` creates
    # no add, change or delete right; the checks below do not depend on any permission row).

    def has_add_permission(self, request):
        return may_edit(request)

    def has_change_permission(self, request, obj=None):
        return may_edit(request)

    def has_delete_permission(self, request, obj=None):
        return may_edit(request)

    # The two faces.

    def get_list_display(self, request):
        return self.list_display if may_edit(request) else self.staff_list_display

    def get_list_display_links(self, request, list_display):
        if may_edit(request):
            return self.list_display_links
        return list(list_display)[:1]

    def get_fields(self, request, obj=None):
        return self.fields if may_edit(request) else self.staff_fields

    def get_readonly_fields(self, request, obj=None):
        return self.raw_readonly if may_edit(request) else self.staff_fields

    def lookup_allowed(self, lookup, value, request):
        """Plain face: only a lookup on a field of ``list_filter`` (hidden columns must not be probed by an address).

        Django allows a lookup on any local field of the model (``?staff_note__startswith=...``), which would let a
        ``view`` holder find out the free text the leak rule hides, one prefix at a time.
        """
        if not may_edit(request):
            filtered = {item[0] if isinstance(item, (tuple, list)) else item for item in self.get_list_filter(request)}
            filtered = {item for item in filtered if isinstance(item, str)}
            if not any(lookup == name or lookup.startswith(f"{name}__") for name in filtered):
                return False
        return super().lookup_allowed(lookup, value, request)

    # Forms: the ``editable=False`` columns are declared inputs.

    def row_form(self, request):
        extras = {}
        for name in self.raw_extras:
            field = self.model._meta.get_field(name)
            formfield = self.formfield_for_dbfield(field, request, show_hidden_initial=False)
            if formfield is not None:
                extras[name] = formfield
        attrs = {**extras, "extra_model_fields": {name: self.model._meta.get_field(name) for name in extras}}
        return type(f"{self.model.__name__}RawRowForm", (self.form,), attrs)

    def get_form(self, request, obj=None, change=False, **kwargs):
        if may_edit(request):
            if "fields" not in kwargs:
                kwargs["fields"] = flatten_fieldsets(self.get_fieldsets(request, obj))
            if kwargs["fields"] is not None:
                kwargs["fields"] = [name for name in kwargs["fields"] if name not in self.raw_extras]
            kwargs["form"] = self.row_form(request)
        return super().get_form(request, obj, change=change, **kwargs)

    def get_changelist_form(self, request, **kwargs):
        if may_edit(request):
            kwargs.setdefault("form", self.row_form(request))
            kwargs.setdefault("fields", self.list_editable)
        return super().get_changelist_form(request, **kwargs)

    def new_row(self, request, form=None):
        """The context of the empty row at the top of the list: an unbound form, or the bound one with its errors."""
        if form is None:
            form = self.get_changelist_form(request)(prefix=NEW_ROW_PREFIX)
        for field in form.fields.values():
            # The inputs sit in the table of the list but belong to the form of the new row (HTML ``form``).
            field.widget.attrs["form"] = NEW_ROW_FORM_ID
            field.show_hidden_initial = False
        return {
            "form": form,
            "form_id": NEW_ROW_FORM_ID,
            "url": reverse(self._route("add_row")),
            "next": self._list_address(request) if request.method == "POST" else request.get_full_path(),
        }

    def _route(self, name):
        return f"{self.admin_site.name}:{self.opts.app_label}_{self.opts.model_name}_{name}"

    def _list_address(self, request):
        """Where to go back to: the list address the new row was posted from, or the list itself."""
        base = reverse(self._route("changelist"))
        target = request.POST.get("next", "")
        if target.split("?", 1)[0] == base and url_has_allowed_host_and_scheme(
            target, allowed_hosts={request.get_host()}, require_https=request.is_secure()
        ):
            return target
        return base

    # Audit: every write is noted, one security event per request.

    def _edits(self, request):
        """The audit list of this request. Fail closed: a write path that is not audited must not exist."""
        try:
            return request._raw_edits
        except AttributeError:
            raise RuntimeError("A raw table write outside an audited request.") from None

    def _note(self, request, action, pk, field_names=(), cascade=None):
        self._edits(request).append((action, pk, tuple(field_names), dict(cascade or {})))

    def _record(self, request):
        edits = request._raw_edits
        if not edits:
            return
        actions = "/".join(sorted({action for action, _pk, _names, _cascade in edits}))
        rows = [str(pk) for _action, pk, _names, _cascade in edits[:MAX_LOGGED_ROWS]]
        if len(edits) > MAX_LOGGED_ROWS:
            rows.append("...")
        names = sorted({name for _action, _pk, field_names, _cascade in edits for name in field_names})
        cascade = {}
        for _action, _pk, _names, counts in edits:
            for label, number in counts.items():
                cascade[label] = cascade.get(label, 0) + number
        # Identifiers and numbers only, never a value.
        detail = f"table {self.opts.label_lower}, {actions}, rows {','.join(rows)}, fields {','.join(names) or '-'}"
        if cascade:
            detail += ", cascade " + ",".join(f"{label}={number}" for label, number in sorted(cascade.items()))
        security_log.record(SecurityEvent.Event.MANAGEMENT_DATA_EDITED, actor=request.user, detail=detail)

    def _refused(self, request, error):
        """A refusal by the database (log the class only: its text may carry a value)."""
        logger.warning(
            "Management data write refused by the database: table %s, %s.",
            self.opts.label_lower, type(error).__name__,
        )

    def _run_audited(self, request, view, *args):
        """Run a writing view in one transaction with its security event; a database refusal is a message."""
        request._raw_edits = []
        try:
            with transaction.atomic(using=router.db_for_write(self.model)):
                response = view(*args)
                self._record(request)
        except DATABASE_ERRORS as error:
            self._refused(request, error)
            self.message_user(request, DATABASE_REFUSED, messages.ERROR)
            return HttpResponseRedirect(request.get_full_path())
        return response

    def save_row(self, obj, change):
        obj.save()

    def save_model(self, request, obj, form, change):
        self._edits(request)  # before the write
        self.save_row(obj, change)
        self._note(request, "change" if change else "add", obj.pk, form.changed_data)

    def delete_model(self, request, obj):
        self._edits(request)  # before the write
        # The rows that go with it (cascade), as numbers per table.
        collector = NestedObjects(using=router.db_for_write(self.model))
        collector.collect([obj])
        cascade = {model._meta.label_lower: len(objects) for model, objects in collector.data.items()}
        cascade[self.opts.label_lower] = cascade.get(self.opts.label_lower, 1) - 1
        pk = obj.pk
        super().delete_model(request, obj)
        self._note(request, "delete", pk, cascade={label: number for label, number in cascade.items() if number})
    # Views.

    def get_model_perms(self, request):
        # The start page offers no "Add" link: a row is added with the new row of the list.
        return {**super().get_model_perms(request), "add": False}

    def add_view(self, request, form_url="", extra_context=None):
        if may_edit(request):
            return HttpResponseRedirect(reverse(self._route("changelist")))
        return super().add_view(request, form_url, extra_context)

    def changelist_view(self, request, extra_context=None):
        if not may_edit(request):
            return super().changelist_view(request, extra_context)
        # The new row replaces the "Add" button of the mask.
        context = {"has_add_permission": False, "raw_new_row": self.new_row(request), **(extra_context or {})}
        if request.method == "POST" and "_save" in request.POST:
            return self._run_audited(request, super().changelist_view, request, context)
        return super().changelist_view(request, context)

    def changeform_view(self, request, object_id=None, form_url="", extra_context=None):
        if request.method == "POST" and may_edit(request):
            return self._run_audited(request, super().changeform_view, request, object_id, form_url, extra_context)
        return super().changeform_view(request, object_id, form_url, extra_context)

    def delete_view(self, request, object_id, extra_context=None):
        if request.method == "POST" and may_edit(request):
            return self._run_audited(request, super().delete_view, request, object_id, extra_context)
        return super().delete_view(request, object_id, extra_context)

    def get_urls(self):
        return [
            path(
                "add-row/",
                self.admin_site.admin_view(self.add_row_view),
                name=f"{self.opts.app_label}_{self.opts.model_name}_add_row",
            ),
            *super().get_urls(),
        ]

    def add_row_view(self, request):
        """Create a row from the new row of the list and return to the list (no form page)."""
        if not self.has_add_permission(request):
            raise PermissionDenied
        if request.method != "POST":
            query = request.GET.urlencode()
            return HttpResponseRedirect(reverse(self._route("changelist")) + (f"?{query}" if query else ""))
        if "_save" in request.POST:
            # The "Save" of the list, posted to this address after an invalid new row was shown.
            return self.changelist_view(request)
        form = self.get_changelist_form(request)(request.POST, prefix=NEW_ROW_PREFIX)
        request._raw_edits = []
        row = None
        try:
            with transaction.atomic(using=router.db_for_write(self.model)):
                if form.is_valid():
                    row = self.save_form(request, form, change=False)
                    self.save_model(request, row, form, change=False)
                    self.save_related(request, form, formsets=[], change=False)
                    self.log_addition(request, row, self.construct_change_message(request, form, [], True))
                    self._record(request)
        except DATABASE_ERRORS as error:
            self._refused(request, error)
            row = None
            form.add_error(None, DATABASE_REFUSED)
        if row is not None:
            self.message_user(request, _("Row added (ID %(id)s).") % {"id": row.pk}, messages.SUCCESS)
            return HttpResponseRedirect(self._list_address(request))
        self.message_user(request, _("The row was not added. Check the marked fields of the new row."), messages.ERROR)
        return self.changelist_view(request, {"raw_new_row": self.new_row(request, form)})
