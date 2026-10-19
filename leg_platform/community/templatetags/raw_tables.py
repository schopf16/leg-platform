"""The result list of a raw table with the empty "new row" on top (``community.raw_tables``, D-091)."""

from django import template
from django.contrib.admin.templatetags.admin_list import result_list

register = template.Library()


@register.inclusion_tag("admin/community/raw_change_list_results.html")
def raw_result_list(cl, new_row):
    """Django's ``result_list`` plus the new row: one cell per column, an input where the form has one."""
    context = result_list(cl)
    form = new_row["form"]
    context["new_row"] = new_row
    context["new_row_cells"] = [
        {"name": name, "field": form[name] if name in form.fields else None} for name in cl.list_display
    ]
    return context
