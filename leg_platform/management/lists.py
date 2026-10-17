"""The list framework of the management pages (D-078).

Usage: describe columns with ``Column``; ``state = parse(request.GET, ...)`` validates the address (unknown
values fall back to defaults); build the queryset yourself and ``paginate(queryset, state)``;
``list_context(...)`` gives the template context (headers with sort links and saved widths, footer, widths URL).

The address of a list is its state: ``q``, the filter names, ``sort``, ``dir``, ``per`` (30, 50, 100, ``all``),
``page`` and ``problems=1``; default values are left out of built addresses.
"""

import math
from dataclasses import dataclass, field
from urllib.parse import urlencode

from django.utils.translation import gettext

from leg_platform.community import preferences

PAGE_SIZES = (30, 50, 100)
ALL = "all"
DEFAULT_PER = 50
# The most rows "All" shows at once (D-084).
MAX_ALL_ROWS = 5000
MAX_QUERY_LENGTH = 200


@dataclass(frozen=True)
class Column:
    """A list column. ``width`` is the start width in pixels (None: takes the rest); ``resizable`` is False
    for the column next to the fixed link column; ``keep`` never narrows it on a medium screen."""

    key: str
    label: object
    sortable: bool = True
    info: object = ""
    info_placement: str = ""
    width: int | None = None
    resizable: bool = True
    info_template: str = ""
    keep: bool = False


@dataclass
class ListState:
    path: str
    query: str = ""
    values: dict = field(default_factory=dict)
    defaults: dict = field(default_factory=dict)
    sort: str = ""
    default_sort: str = ""
    descending: bool = False
    per: str = str(DEFAULT_PER)
    page: int = 1
    problems: bool = False
    focus: str = ""

    def params(self, **changes):
        """Query parameters of this state with ``changes`` applied, defaults left out; a change of
        anything but the page goes back to page 1."""
        state = {
            "q": self.query, "sort": self.sort, "dir": "desc" if self.descending else "asc",
            "per": self.per, "page": self.page, "problems": "1" if self.problems else "",
            **self.values,
        }
        if any(name != "page" for name in changes):
            state["page"] = 1
        state.update(changes)
        params = {}
        for name in ("q", *self.values):
            value = state.get(name)
            if value not in ("", None) and value != self.defaults.get(name, ""):
                params[name] = value
        descending = state["dir"] == "desc"
        if state["sort"] != self.default_sort or descending:
            params["sort"] = state["sort"]
        if descending:
            params["dir"] = "desc"
        if str(state["per"]) != str(DEFAULT_PER):
            params["per"] = state["per"]
        if int(state["page"]) > 1:
            params["page"] = state["page"]
        if state["problems"]:
            params["problems"] = "1"
        return params

    def url(self, **changes):
        params = self.params(**changes)
        return f"{self.path}?{urlencode(params)}" if params else self.path

    def url_to(self, path, **changes):
        """The address of another page of the list (the print dialog) with this state, never a page number."""
        params = self.params(page=1, **changes)
        return f"{path}?{urlencode(params)}" if params else path

    def hidden(self):
        """Hidden form fields carrying sort and "problems" (the form's own fields are search, filters, page size)."""
        params = self.params()
        return [(name, params[name]) for name in ("sort", "dir", "problems") if name in params]


def parse(params, *, path, columns, default_sort, filters=None):
    """Read the state of a list from ``request.GET`` (any mapping with ``get``).

    ``filters`` maps a filter name to ``(allowed values, default)``."""
    filters = filters or {}
    sortable = [column.key for column in columns if column.sortable]
    sort = params.get("sort")
    if sort not in sortable:
        sort = default_sort
    per = params.get("per", str(DEFAULT_PER))
    if per != ALL and per not in {str(size) for size in PAGE_SIZES}:
        per = str(DEFAULT_PER)
    page = params.get("page", "1")
    page = int(page) if page.isascii() and page.isdigit() and len(page) < 9 else 1
    values, defaults = {}, {}
    for name, (allowed, default) in filters.items():
        value = params.get(name, default)
        values[name] = value if value in allowed else default
        defaults[name] = default
    return ListState(
        path=path,
        query=(params.get("q") or "").strip()[:MAX_QUERY_LENGTH],
        values=values,
        defaults=defaults,
        sort=sort,
        default_sort=default_sort,
        descending=params.get("dir") == "desc",
        per=per,
        page=max(page, 1),
        problems=params.get("problems") == "1",
        focus=params.get("focus", "") if params.get("focus") == "q" else "",
    )


@dataclass
class Page:
    items: list
    total: int
    first: int
    last: int
    page: int
    pages: int
    prev_url: str = ""
    next_url: str = ""
    notice: str = ""


class ListRows(list):
    """Rows built and sorted in Python (figures that are no database columns); ``paginate`` takes it like a queryset."""

    def count(self):
        return len(self)


def paginate(queryset, state):
    """The rows of the requested page (beyond the end: the last page). "All" is bounded by ``MAX_ALL_ROWS``:
    a longer list shows default-size pages and a notice (D-084)."""
    total = queryset.count()
    notice = ""
    per = state.per
    if per == ALL and total > MAX_ALL_ROWS:
        per = str(DEFAULT_PER)
        notice = gettext(
            "The list has more than %(max)d entries; all of them cannot be shown at once. "
            "Narrow it with the search or the filters."
        ) % {"max": MAX_ALL_ROWS}
    if per == ALL:
        items = list(queryset)
        return Page(items, total, 1 if total else 0, total, 1, 1)
    size = int(per)
    pages = max(1, math.ceil(total / size))
    page = min(state.page, pages)
    items = list(queryset[(page - 1) * size : page * size])
    first = (page - 1) * size + 1 if items else 0
    return Page(
        items, total, first, first + len(items) - 1 if items else 0, page, pages,
        prev_url=state.url(page=page - 1) if page > 1 else "",
        next_url=state.url(page=page + 1) if page < pages else "",
        notice=notice,
    )


def header_cells(columns, state, saved_widths, key):
    """The table head cells: label, sort link, arrow, aria sort state, width."""
    cells = []
    for column in columns:
        active = column.sortable and column.key == state.sort
        direction = ("desc" if state.descending else "asc") if active else ""
        cells.append(
            {
                "key": column.key,
                "label": column.label,
                "sortable": column.sortable,
                "url": state.url(sort=column.key, dir="desc" if (active and not state.descending) else "asc")
                if column.sortable else "",
                "active": active,
                "arrow": ("↑" if direction == "asc" else "↓") if active else "↕",
                "aria_sort": {"asc": "ascending", "desc": "descending"}.get(direction, ""),
                "width": saved_widths.get(column.key) or column.width,
                "resizable": column.resizable,
                "keep": column.keep,
                "info": column.info,
                "info_template": column.info_template,
                "info_placement": column.info_placement,
                "tip_id": f"tip-{key}-{column.key}",
            }
        )
    return cells


def list_context(request, key, columns, state, page, *, count_text, widths_url):
    """The list templates' context; ``key`` is in ``preferences.LIST_KEYS``."""
    saved = preferences.get_preference(request.user, key).column_widths
    return {
        "list_key": key,
        "list_columns": header_cells(columns, state, saved, key),
        "list_state": state,
        "list_page": page,
        "list_count_text": count_text,
        "list_widths_url": widths_url,
        "list_page_sizes": [*[str(size) for size in PAGE_SIZES], ALL],
    }
