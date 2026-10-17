"""Draws a ``PrintDocument`` as a PDF with ReportLab (D-082). In memory only: no temporary file.

ReportLab reads a small XML markup in its paragraphs (``<b>``, ``<font>``, ``<img src="...">``); older releases
could be made to run code or to fetch a web address through it (CVE-2019-17626, CVE-2020-28463, CVE-2023-33733,
all fixed in the declared minimum). Nothing a person typed is ever given to ReportLab as markup: every text
goes through ``markup`` here, which removes control characters, limits the length and escapes ``& < >``. No
image is drawn and no markup is built from data.

The font is DejaVu Sans Condensed (``fonts/``, Bitstream Vera licence), which covers the Latin alphabets with
all their accents, the Greek and the Cyrillic alphabet.
"""

import re
import threading
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import BaseDocTemplate, Frame, PageBreak, PageTemplate, Paragraph, Spacer, Table, TableStyle

FONT_DIRECTORY = Path(__file__).resolve().parent / "fonts"
REGULAR = "LegSans"
BOLD = "LegSans-Bold"

# A person can type a very long text (a remark, a note). A table cell that is higher than the page cannot be
# drawn, so a text in a table is cut at 300 characters; a line of a detail page is printed in full up to the
# stored maximum of a remark or note (``community.models.REMARK_MAX_LENGTH`` = 5000; a test keeps them equal), as a
# paragraph that may run over a page.
MAX_CELL_CHARS = 300
MAX_FACT_CHARS = 5000
MAX_TITLE_CHARS = 200

MARGIN = 15 * mm
BOTTOM_MARGIN = 18 * mm
CONTENT_WIDTH = A4[0] - 2 * MARGIN

INK = colors.HexColor("#1B1D20")
MUTED = colors.HexColor("#555B63")
LINE = colors.HexColor("#B8BEC6")
HEAD_BACKGROUND = colors.HexColor("#E6E9ED")
NOTICE_BACKGROUND = colors.HexColor("#FBF1D6")

_CONTROL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\ud800-\udfff]")
_fonts_lock = threading.Lock()


class PdfError(Exception):
    """The document cannot be drawn (a page element that does not fit)."""


def register_fonts():
    """Register the two faces once (ReportLab keeps them for the process)."""
    with _fonts_lock:
        if REGULAR not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(REGULAR, str(FONT_DIRECTORY / "DejaVuSansCondensed.ttf")))
            pdfmetrics.registerFont(TTFont(BOLD, str(FONT_DIRECTORY / "DejaVuSansCondensed-Bold.ttf")))
            pdfmetrics.registerFontFamily(REGULAR, normal=REGULAR, bold=BOLD, italic=REGULAR, boldItalic=BOLD)


def plain(value, limit):
    """The text without control characters (a line break stays), cut to ``limit`` characters with an ellipsis."""
    value = "" if value is None else str(value)
    value = _CONTROL.sub("", value.replace("\r\n", "\n").replace("\r", "\n").replace("\t", " "))
    if len(value) > limit:
        value = value[: limit - 1].rstrip() + "…"
    return value


def markup(value, limit=MAX_CELL_CHARS):
    """A text for a ReportLab paragraph: escaped, line breaks as ``<br/>``. The only way text enters a paragraph."""
    return escape(plain(value, limit)).replace("\n", "<br/>")


def _style(name, **values):
    values.setdefault("fontName", REGULAR)
    values.setdefault("textColor", INK)
    return ParagraphStyle(name, **values)


def _styles():
    return {
        "title": _style("title", fontName=BOLD, fontSize=17, leading=21, spaceAfter=4),
        "subtitle": _style("subtitle", fontSize=10, leading=13, textColor=MUTED, spaceAfter=6),
        "heading": _style("heading", fontName=BOLD, fontSize=11, leading=14, spaceBefore=10, spaceAfter=4, keepWithNext=1),
        "cell": _style("cell", fontSize=8.5, leading=10.5),
        "head": _style("head", fontName=BOLD, fontSize=8.5, leading=10.5),
        "label": _style("label", fontSize=9, leading=11.5, textColor=MUTED),
        "value": _style("value", fontSize=9, leading=11.5),
        "note": _style("note", fontSize=8.5, leading=11, textColor=MUTED, spaceBefore=4),
        "problem": _style("problem", fontName=BOLD, fontSize=9.5, leading=12),
        "empty": _style("empty", fontSize=9, leading=12, textColor=MUTED),
    }


def _scaled(columns, width, minimum=None):
    """The widths of the columns: proportional to their weights, but never narrower than ``minimum`` (the
    heading must fit); a column that is raised takes its room from the others."""
    minimum = minimum or [0] * len(columns)
    if sum(minimum) >= width:
        total = sum(minimum)
        return [width * value / total for value in minimum]
    total = sum(column.width for column in columns) or 1
    widths = [width * column.width / total for column in columns]
    fixed = set()
    for _ in range(len(columns)):
        fixed |= {index for index, (value, least) in enumerate(zip(widths, minimum)) if value < least}
        free = [index for index in range(len(columns)) if index not in fixed]
        if not fixed or not free:
            break
        room = width - sum(minimum[index] for index in fixed)
        weight = sum(columns[index].width for index in free)
        widths = [
            minimum[index] if index in fixed else room * columns[index].width / weight for index in range(len(columns))
        ]
    return widths


def _table(table, styles, width=CONTENT_WIDTH, limit=MAX_CELL_CHARS):
    """A table with a repeated head row, or the note for an empty one."""
    if not table.rows:
        return Paragraph(markup(table.empty_text, MAX_FACT_CHARS), styles["empty"])
    data = [[Paragraph(markup(column.label, MAX_TITLE_CHARS), styles["head"]) for column in table.columns]]
    for row in table.rows:
        data.append([Paragraph(markup(cell, limit), styles["cell"]) for cell in row])
    minimum = [pdfmetrics.stringWidth(plain(column.label, MAX_TITLE_CHARS), BOLD, 8.5) + 10 for column in table.columns]
    flowable = Table(data, colWidths=_scaled(table.columns, width, minimum), repeatRows=1)
    flowable.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("BACKGROUND", (0, 0), (-1, 0), HEAD_BACKGROUND),
                ("LINEBELOW", (0, 0), (-1, -1), 0.4, LINE),
                ("LINEABOVE", (0, 0), (-1, 0), 0.4, LINE),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    return flowable


LONG_FACT_CHARS = 400


def _facts(facts, styles):
    """The lines of label and value as flowables. A short value is a row of a table; a long one (a remark, a note:
    up to the stored maximum) is a label and a paragraph of its own, which can run over a page break; a table row
    that is higher than the page cannot be drawn."""
    flowables, group = [], []

    def flush():
        if not group:
            return
        table = Table(group[:], colWidths=[CONTENT_WIDTH * 0.34, CONTENT_WIDTH * 0.66])
        table.setStyle(
            TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LINEBELOW", (0, 0), (-1, -1), 0.3, LINE),
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                    ("TOPPADDING", (0, 0), (-1, -1), 2.5),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
                ]
            )
        )
        flowables.append(table)
        group.clear()

    for label, value in facts:
        text = markup(value, MAX_FACT_CHARS)
        label_paragraph = Paragraph(markup(label, MAX_TITLE_CHARS), styles["label"])
        if len(plain(value, MAX_FACT_CHARS)) > LONG_FACT_CHARS:
            flush()
            flowables.extend([label_paragraph, Paragraph(text, styles["value"]), Spacer(1, 4)])
        else:
            group.append([label_paragraph, Paragraph(text, styles["value"])])
    flush()
    return flowables


def _header(document, styles):
    story = [Paragraph(markup(document.title, MAX_TITLE_CHARS), styles["title"])]
    story.extend(_facts(document.info, styles))
    story.append(Spacer(1, 8))
    return story


def _detail(page, styles):
    story = [PageBreak(), Paragraph(markup(page.title, MAX_TITLE_CHARS), styles["title"])]
    if page.subtitle:
        story.append(Paragraph(markup(page.subtitle, MAX_TITLE_CHARS), styles["subtitle"]))
    if page.problems:
        notice = Table(
            [[Paragraph(markup(text, MAX_FACT_CHARS), styles["problem"])] for text in page.problems],
            colWidths=[CONTENT_WIDTH],
        )
        notice.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), NOTICE_BACKGROUND),
                    ("BOX", (0, 0), (-1, -1), 0.8, INK),
                    ("LEFTPADDING", (0, 0), (-1, -1), 8),
                    ("TOPPADDING", (0, 0), (-1, -1), 4),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ]
            )
        )
        story.extend([Spacer(1, 4), notice])
    for section in page.sections:
        story.append(Paragraph(markup(section.title, MAX_TITLE_CHARS), styles["heading"]))
        if section.facts:
            story.extend(_facts(section.facts, styles))
        if section.table is not None:
            story.append(_table(section.table, styles))
        if section.note:
            story.append(Paragraph(markup(section.note, MAX_FACT_CHARS), styles["note"]))
    return story


def render(document, *, compress=True):
    """The PDF of the document as bytes. ``compress=False`` leaves the page streams readable (tests)."""
    register_fonts()
    styles = _styles()
    buffer = BytesIO()
    footer_title = plain(document.title, MAX_TITLE_CHARS).replace("\n", " ")

    def draw_footer(canvas, doc):
        canvas.saveState()
        canvas.setFont(REGULAR, 8)
        canvas.setFillColor(MUTED)
        canvas.setStrokeColor(LINE)
        canvas.setLineWidth(0.4)
        canvas.line(MARGIN, BOTTOM_MARGIN - 4 * mm, A4[0] - MARGIN, BOTTOM_MARGIN - 4 * mm)
        number = str(canvas.getPageNumber())
        room = CONTENT_WIDTH - pdfmetrics.stringWidth(number, REGULAR, 8) - 12
        shown = footer_title
        while shown and pdfmetrics.stringWidth(shown, REGULAR, 8) > room:
            shown = shown[:-2] + "…" if len(shown) > 1 else ""
        canvas.drawString(MARGIN, BOTTOM_MARGIN - 8 * mm, shown)
        canvas.drawRightString(A4[0] - MARGIN, BOTTOM_MARGIN - 8 * mm, number)
        canvas.restoreState()

    doc = BaseDocTemplate(
        buffer, pagesize=A4, leftMargin=MARGIN, rightMargin=MARGIN, topMargin=MARGIN, bottomMargin=BOTTOM_MARGIN,
        title=plain(document.title, MAX_TITLE_CHARS).replace("\n", " "), author="", subject="",
        creator="LEG-Platform", pageCompression=1 if compress else 0,
    )
    # No padding inside the frame: the tables are exactly as wide as the text area.
    frame = Frame(MARGIN, BOTTOM_MARGIN, CONTENT_WIDTH, A4[1] - MARGIN - BOTTOM_MARGIN, 0, 0, 0, 0, id="content")
    doc.addPageTemplates([PageTemplate(id="page", frames=[frame], onPage=draw_footer)])
    story = _header(document, styles)
    story.append(_table(document.table, styles))
    for page in document.details:
        story.extend(_detail(page, styles))
    try:
        doc.build(story)
    except Exception as error:  # ReportLab raises LayoutError and others for an element that does not fit
        raise PdfError(type(error).__name__) from None
    return buffer.getvalue()
