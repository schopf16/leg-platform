"""The description of a printout: plain texts and numbers, never markup.

Everything a person typed (names, remarks, addresses) ends up here as ordinary text. The drawing
(``pdf.py``) is the only place that knows ReportLab's paragraph markup and it escapes every text, so a
name such as ``<b>x</b>`` or ``<img src="http://..."/>`` is printed as typed.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Column:
    """A column of a table: the heading and the relative width (the widths are scaled to the page)."""

    label: str
    width: float = 1.0


@dataclass
class Table:
    """A table: a row is a list of cell texts, one per column; a line break in a text is kept."""

    columns: list
    rows: list = field(default_factory=list)
    empty_text: str = ""


@dataclass
class Section:
    """A block of a detail page: a heading, label and value lines, a table, a note."""

    title: str
    facts: list = field(default_factory=list)  # [(label, value)]
    table: Table | None = None
    note: str = ""


@dataclass
class DetailPage:
    """The detail page of one entry. It starts on a new page."""

    title: str
    subtitle: str = ""
    problems: list = field(default_factory=list)  # one sentence each
    sections: list = field(default_factory=list)


@dataclass
class PrintDocument:
    """The whole printout: the title, the lines above the list (``info``: label and text; the date, the search,
    the filters, the sorting), the list as a table and the detail pages after it."""

    title: str
    info: list
    table: Table
    details: list = field(default_factory=list)

    def row_count(self):
        """The number of table rows of the whole document (the bound of the size of a printout)."""
        total = len(self.table.rows)
        for page in self.details:
            for section in page.sections:
                total += len(section.facts) + (len(section.table.rows) if section.table else 0)
        return total
