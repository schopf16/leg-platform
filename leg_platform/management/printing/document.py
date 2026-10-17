"""A printout as plain texts and numbers, never markup (``pdf.py`` escapes everything it draws)."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Column:
    """A table column: heading and relative width."""

    label: str
    width: float = 1.0


@dataclass
class Table:
    """Rows are lists of cell texts, one per column."""

    columns: list
    rows: list = field(default_factory=list)
    empty_text: str = ""


@dataclass
class Section:
    """A block of a detail page: heading, ``facts`` as [(label, value)], a table, a note."""

    title: str
    facts: list = field(default_factory=list)
    table: Table | None = None
    note: str = ""


@dataclass
class DetailPage:
    """The detail page of one entry; starts on a new page. ``problems`` are sentences."""

    title: str
    subtitle: str = ""
    problems: list = field(default_factory=list)
    sections: list = field(default_factory=list)


@dataclass
class PrintDocument:
    """Title, ``info`` lines (label, text) above the list, the list table and the detail pages."""

    title: str
    info: list
    table: Table
    details: list = field(default_factory=list)

    def row_count(self):
        """Table rows of the whole document (bounds the printout size)."""
        total = len(self.table.rows)
        for page in self.details:
            for section in page.sections:
                total += len(section.facts) + (len(section.table.rows) if section.table else 0)
        return total
