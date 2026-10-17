"""The list framework of the management pages (D-078; ``LISTEN-STANDARD.md``).

Not a big abstraction: a few small classes and helpers that every list (locations, metering points,
LEGs, persons) uses the same way.

1. Describe the columns with ``Column``.
2. ``state = parse(request.GET, columns=..., default_sort=..., filters={...})`` reads and validates
   the address: search text, filters, sort column and direction, page size and page, "problems".
   Nothing from the address is trusted: an unknown value falls back to the default.
3. Build the queryset yourself (search, filters, sort are your queries) and call
   ``page = paginate(queryset, state)``.
4. ``list_context(request, key, columns, state, page, count_text=...)`` gives the template context:
   the headers (with sort link, arrow and the saved width), the footer, the address of the endpoint
   that saves column widths. The templates ``management/_list_head.html``, ``_list_form_start.html`` and
   ``_list_foot.html`` render it; ``management/js/list.js`` adds the resizing of the columns,
   the submit on change and the rows that open.

The address of a list is its state: ``q``, the names of the filters, ``sort``, ``dir`` (``asc`` or
``desc``), ``per`` (30, 50, 100 or ``all``), ``page`` and ``problems=1``. A value that is the default
is left out of the addresses that the framework builds.
"""

import math
from dataclasses import dataclass, field
from urllib.parse import urlencode

from django.utils.translation import gettext

from leg_platform.community import preferences

PAGE_SIZES = (30, 50, 100)
ALL = "all"
DEFAULT_PER = 50
# The most rows that "All" shows at once; a longer list shows pages and says so (D-084, L-1).
MAX_ALL_ROWS = 5000
MAX_QUERY_LENGTH = 200
MIN_COLUMN_WIDTH = preferences.MIN_COLUMN_WIDTH


@dataclass(frozen=True)
class Column:
    """A column of a list. ``width`` is the start width in pixels (None: the column takes what is
    left); ``resizable`` is False for the column that sits next to the fixed link column."""

    key: str
    label: object
    sortable: bool = True
    info: object = ""
    info_placement: str = ""
    width: int | None = None
    resizable: bool = True
    info_template: str = ""
    keep: bool = False  # the column is never narrowed to fit a medium screen (a number that is never cut)


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
        """The query parameters of this state with ``changes`` applied; defaults are left out. A
        change of anything but the page goes back to page 1."""
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
        """The address of another page of the list (the print dialog) with this state; never a page number."""
        params = self.params(page=1, **changes)
        return f"{path}?{urlencode(params)}" if params else path

    def hidden(self):
        """The state that a form of the list must carry along (the sort and the "problems" filter);
        the form's own fields are the search, the filters and the page size."""
        params = self.params()
        return [(name, params[name]) for name in ("sort", "dir", "problems") if name in params]


def parse(params, *, path, columns, default_sort, filters=None):
    """Read the state of a list from ``request.GET`` (or any mapping with ``get``).

    ``filters`` maps the name of a filter to ``(allowed values, default)``; a value that is not
    allowed becomes the default."""
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
    """Rows that were built and sorted in Python (a list whose figures are not database columns, the
    LEGs): ``paginate`` takes it like a queryset."""

    def count(self):
        return len(self)


def paginate(queryset, state):
    """The rows of the page that the state asks for (a request for a page beyond the end shows the
    last page). "All" is bounded by ``MAX_ALL_ROWS``: a longer list shows the pages of the default size and a
    notice (D-084, L-1)."""
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
    """The cells of the table head: label, sort link, arrow, accessible sort state, width."""
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
    """What the list templates need. ``key`` is the key of the list (``preferences.LIST_KEYS``)."""
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
