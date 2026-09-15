"""A small, dependency-free PDF-1.4 writer (task 18.2).

Phase 18 needs a *branded, deterministic* PDF pack, produced offline with no AWS and no new heavy
dependency in the install path (task 0.1 pins exact versions and the phase gate requires the whole
feature to run offline). A full layout engine (reportlab, weasyprint) would add a large dependency
and a font toolchain for what Phase 18 actually needs: headed text pages with the built-in PDF fonts
(Helvetica), wrapped paragraphs and simple rules. That is a few hundred lines of well-understood
PDF-1.4 structure, so it lives here rather than as a dependency.

Determinism
-----------

The document carries no creation timestamp and no random ids: the same content produces byte-
identical output every time, which is what the report tests assert and what makes a re-run
reproducible. Object numbering is sequential and the xref table is computed from exact byte offsets.

Scope of the writer
-------------------

Deliberately minimal: one page size (US Letter), the three standard Helvetica variants, left-aligned
wrapped text, horizontal rules and page breaks. It is not a general layout engine; it renders the
report document model in :mod:`c360.reports.render` and nothing else. Text is limited to WinAnsi
(Latin-1) glyphs — the synthetic dataset is Latin-script — and any character outside it is replaced
so a stray glyph can never corrupt the stream.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

# US Letter, 72pt per inch.
_PAGE_WIDTH = 612.0
_PAGE_HEIGHT = 792.0
_MARGIN = 54.0
_CONTENT_WIDTH = _PAGE_WIDTH - 2 * _MARGIN

# Average glyph width as a fraction of font size for Helvetica, used for greedy word wrapping. The
# built-in fonts have per-glyph metrics; a single average keeps the writer dependency-free while
# wrapping conservatively (it slightly over-estimates, so lines never overflow the margin).
_AVG_GLYPH_RATIO = 0.52

# WinAnsi (Latin-1) is the printable range the built-in fonts encode directly.
_MIN_PRINTABLE = 32
_MAX_WINANSI = 255


class Font(Enum):
    """The three built-in Helvetica variants this writer emits."""

    REGULAR = "Helvetica"
    BOLD = "Helvetica-Bold"
    OBLIQUE = "Helvetica-Oblique"


@dataclass(frozen=True, slots=True)
class TextRun:
    """One line of text at a size and font, already wrapped to the content width."""

    text: str
    size: float
    font: Font


class _Line:
    """A positioned line on a page: its text runs share a baseline y."""

    __slots__ = ("run", "y")

    def __init__(self, run: TextRun, y: float) -> None:
        self.run = run
        self.y = y


@dataclass(slots=True)
class _Page:
    """A page under construction: a list of positioned lines and rules."""

    lines: list[_Line] = field(default_factory=list)
    rules: list[float] = field(default_factory=list)  # y positions of horizontal rules


def _wrap(text: str, size: float) -> list[str]:
    """Greedily wrap ``text`` to the content width at the given font size."""
    max_chars = max(1, int(_CONTENT_WIDTH / (size * _AVG_GLYPH_RATIO)))
    words = text.split()
    if not words:
        return [""]
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        if len(current) + 1 + len(word) <= max_chars:
            current = f"{current} {word}"
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def _escape(text: str) -> str:
    """Escape a string for a PDF literal and drop glyphs outside WinAnsi (Latin-1)."""
    out: list[str] = []
    for char in text:
        if char in ("\\", "(", ")"):
            out.append("\\" + char)
        elif _MIN_PRINTABLE <= ord(char) <= _MAX_WINANSI:
            out.append(char)
        else:
            out.append("?")
    return "".join(out)


class PdfBuilder:
    """Accumulates text and rules across pages, then serializes to PDF bytes.

    The API is intentionally tiny — :meth:`heading`, :meth:`subheading`, :meth:`body`,
    :meth:`spacer`, :meth:`page_break` — matching the report document model. Layout is
    single-column, top-to-bottom; a block that would cross the bottom margin starts a new page.
    """

    __slots__ = ("_cursor_y", "_pages")

    def __init__(self) -> None:
        self._pages: list[_Page] = [_Page()]
        self._cursor_y = _PAGE_HEIGHT - _MARGIN

    def _current(self) -> _Page:
        return self._pages[-1]

    def _ensure_space(self, needed: float) -> None:
        if self._cursor_y - needed < _MARGIN:
            self.page_break()

    def page_break(self) -> None:
        self._pages.append(_Page())
        self._cursor_y = _PAGE_HEIGHT - _MARGIN

    def spacer(self, height: float = 8.0) -> None:
        self._cursor_y -= height

    def rule(self) -> None:
        self._ensure_space(6.0)
        self._cursor_y -= 4.0
        self._current().rules.append(self._cursor_y)
        self._cursor_y -= 6.0

    def _emit(self, text: str, *, size: float, font: Font, leading: float) -> None:
        for wrapped in _wrap(text, size):
            self._ensure_space(leading)
            self._cursor_y -= leading
            self._current().lines.append(_Line(TextRun(wrapped, size, font), self._cursor_y))

    def heading(self, text: str) -> None:
        self.spacer(6.0)
        self._emit(text, size=18.0, font=Font.BOLD, leading=22.0)

    def subheading(self, text: str) -> None:
        self.spacer(4.0)
        self._emit(text, size=13.0, font=Font.BOLD, leading=17.0)

    def body(self, text: str, *, italic: bool = False) -> None:
        font = Font.OBLIQUE if italic else Font.REGULAR
        self._emit(text, size=10.5, font=font, leading=14.0)

    def caption(self, text: str) -> None:
        self._emit(text, size=8.5, font=Font.OBLIQUE, leading=12.0)

    # ---------------------------------------------------------------- serialization
    def build(self) -> bytes:
        """Serialize the accumulated pages to a complete PDF-1.4 document (deterministic)."""
        font_objs = {
            Font.REGULAR: 0,
            Font.BOLD: 0,
            Font.OBLIQUE: 0,
        }

        # Object 1: catalog; object 2: pages tree. Fonts and page/content objects follow.
        # We assign numbers as we build, tracking the pages tree's kids.
        # Layout of object numbers:
        #   1 = Catalog, 2 = Pages, 3..5 = Fonts, then per page: content, page.
        catalog_num = 1
        pages_num = 2
        next_num = 3
        for f in (Font.REGULAR, Font.BOLD, Font.OBLIQUE):
            font_objs[f] = next_num
            next_num += 1

        page_nums: list[int] = []
        content_nums: list[int] = []
        for _ in self._pages:
            content_nums.append(next_num)
            next_num += 1
            page_nums.append(next_num)
            next_num += 1

        # Build object bodies keyed by number.
        bodies: dict[int, bytes] = {}
        kids = " ".join(f"{n} 0 R" for n in page_nums)
        bodies[catalog_num] = f"<< /Type /Catalog /Pages {pages_num} 0 R >>".encode("latin-1")
        bodies[pages_num] = f"<< /Type /Pages /Count {len(page_nums)} /Kids [{kids}] >>".encode(
            "latin-1"
        )
        font_names = {Font.REGULAR: "F1", Font.BOLD: "F2", Font.OBLIQUE: "F3"}
        for f, num in font_objs.items():
            bodies[num] = (
                f"<< /Type /Font /Subtype /Type1 /BaseFont /{f.value} "
                f"/Encoding /WinAnsiEncoding >>"
            ).encode("latin-1")

        font_resource = " ".join(
            f"/{font_names[f]} {font_objs[f]} 0 R" for f in (Font.REGULAR, Font.BOLD, Font.OBLIQUE)
        )
        for index, page in enumerate(self._pages):
            stream = self._content_stream(page, font_names)
            content = (
                f"<< /Length {len(stream)} >>\nstream\n".encode("latin-1") + stream + b"\nendstream"
            )
            bodies[content_nums[index]] = content
            bodies[page_nums[index]] = (
                f"<< /Type /Page /Parent {pages_num} 0 R "
                f"/MediaBox [0 0 {_PAGE_WIDTH:g} {_PAGE_HEIGHT:g}] "
                f"/Resources << /Font << {font_resource} >> >> "
                f"/Contents {content_nums[index]} 0 R >>"
            ).encode("latin-1")

        return _assemble(bodies, next_num - 1)

    def _content_stream(self, page: _Page, font_names: dict[Font, str]) -> bytes:
        parts: list[str] = []
        for line in page.lines:
            run = line.run
            parts.append("BT")
            parts.append(f"/{font_names[run.font]} {run.size:g} Tf")
            parts.append(f"1 0 0 1 {_MARGIN:g} {line.y:g} Tm")
            parts.append(f"({_escape(run.text)}) Tj")
            parts.append("ET")
        for y in page.rules:
            parts.append("0.6 w")
            parts.append(f"{_MARGIN:g} {y:g} m {(_PAGE_WIDTH - _MARGIN):g} {y:g} l S")
        return ("\n".join(parts)).encode("latin-1")


def _assemble(bodies: dict[int, bytes], max_num: int) -> bytes:
    """Serialize objects 1..max_num with a correct xref table and trailer."""
    out = bytearray()
    out += b"%PDF-1.4\n"
    # A binary comment marks the file as containing binary data, per the PDF convention.
    out += b"%\xe2\xe3\xcf\xd3\n"
    offsets: dict[int, int] = {}
    for num in range(1, max_num + 1):
        offsets[num] = len(out)
        out += f"{num} 0 obj\n".encode("latin-1")
        out += bodies[num]
        out += b"\nendobj\n"

    xref_offset = len(out)
    out += f"xref\n0 {max_num + 1}\n".encode("latin-1")
    out += b"0000000000 65535 f \n"
    for num in range(1, max_num + 1):
        out += f"{offsets[num]:010d} 00000 n \n".encode("latin-1")
    out += b"trailer\n"
    out += f"<< /Size {max_num + 1} /Root 1 0 R >>\n".encode("latin-1")
    out += b"startxref\n"
    out += f"{xref_offset}\n".encode("latin-1")
    out += b"%%EOF\n"
    return bytes(out)


__all__ = ["Font", "PdfBuilder"]
