"""PDF reading, annotation and safe saving for ShodhKosha, built on PyMuPDF.

This module contains no GUI code and no SQL. The viewer widget calls it to
render pages and edit annotations; the main window stores what it returns.

Coordinates: every point and rectangle is in PDF page units (1/72 inch,
origin at the top-left of the page), independent of the zoom level.

File locking: a PDF is read fully into memory when opened, so ShodhKosha never
holds an operating-system lock on the file while it is being viewed.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence, Union

try:
    import pymupdf
except ImportError:  # PyMuPDF older than 1.24.3 only provides the legacy name
    import fitz as pymupdf

from shodhkosha.library_database import AnnotationDraft

HIGHLIGHT_COLORS: dict[str, tuple[float, float, float]] = {
    "yellow": (1.0, 0.92, 0.23),
    "green": (0.56, 0.93, 0.56),
    "pink": (1.0, 0.71, 0.76),
}
DEFAULT_HIGHLIGHT_COLOR = "yellow"
DRAWING_TYPE = "drawing"  # pen strokes and arrows, as reported by list_page_annotations

ANALYSIS_TEXT_LIMIT = 8000
_ANALYSIS_LEADING_PAGES = 3
_BOOK_MINIMUM_PAGES = 60
_BOOK_LEADING_PAGES = 12
_SPARSE_PAGE_CHARACTERS = 25
_DRAWING_COLOR = (0.86, 0.15, 0.15)
_DRAWING_WIDTH = 1.5

_DOI_PATTERN = re.compile(r"\b10\.\d{4,9}/[^\s\"<>]+", re.IGNORECASE)
_YEAR_PATTERN = re.compile(r"(19|20)\d{2}")
_PLACEHOLDER_TITLE_PATTERN = re.compile(
    r"^(untitled|microsoft word\b.*|.*\.(docx?|pdf|tex|dvi|indd)|.*\d\.\.\d+)$",
    re.IGNORECASE,
)
_TITLE_SIZE_TOLERANCE = 0.6
_TITLE_MINIMUM_LENGTH = 12
_TITLE_MAXIMUM_LENGTH = 300


class PdfDocumentError(Exception):
    """Base class for errors raised by this module."""


class PdfOpenError(PdfDocumentError):
    """The file could not be opened as a PDF."""


class PdfEditError(PdfDocumentError):
    """The requested edit is not allowed."""


class PdfSaveError(PdfDocumentError):
    """Saving failed. The original file is intact and the edits are still in memory."""


@dataclass(frozen=True)
class RenderedPage:
    """Raw RGB pixels of one page (3 bytes per pixel, ``stride`` bytes per row)."""

    width: int
    height: int
    stride: int
    samples: bytes


@dataclass(frozen=True)
class WordBox:
    """One word on a page and the rectangle it occupies."""

    x0: float
    y0: float
    x1: float
    y1: float
    text: str
    block_number: int
    line_number: int
    word_number: int


@dataclass(frozen=True)
class PageAnnotation:
    """An annotation as shown in the viewer. ``xref`` identifies it for deletion.

    An xref is only valid until the next save; list the annotations again
    after :meth:`PdfDocumentService.save_and_replace`.
    """

    xref: int
    page_number: int
    annotation_type: str
    rectangle: tuple[float, float, float, float]
    color: str
    selected_text: str
    note_text: str


@dataclass(frozen=True)
class EmbeddedMetadata:
    """Bibliographic details found inside the PDF itself. Fields may be empty."""

    title: str
    authors: list[str]
    publication_year: Optional[int]
    doi: str


@dataclass(frozen=True)
class SaveResult:
    """What is in the file after a successful save."""

    page_count: int
    annotations: list[AnnotationDraft]


def compute_file_hash(file_path: Union[str, Path]) -> str:
    """SHA-256 of a file's contents, used to detect duplicate imports."""
    digest = hashlib.sha256()
    with open(file_path, "rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _remove_quietly(file_path: Path) -> None:
    try:
        file_path.unlink()
    except OSError:
        pass


def _distance_to_word(word: WordBox, x: float, y: float) -> float:
    horizontal = max(word.x0 - x, 0.0, x - word.x1)
    vertical = max(word.y0 - y, 0.0, y - word.y1)
    return math.hypot(horizontal, vertical)


def words_between_points(
    words: Sequence[WordBox],
    start_point: tuple[float, float],
    end_point: tuple[float, float],
) -> list[WordBox]:
    """The run of words, in reading order, from the word nearest one point to
    the word nearest the other. This is what dragging the mouse over text selects."""
    if not words:
        return []

    def nearest_index(point: tuple[float, float]) -> int:
        return min(
            range(len(words)),
            key=lambda index: _distance_to_word(words[index], point[0], point[1]),
        )

    first, last = sorted((nearest_index(start_point), nearest_index(end_point)))
    return list(words[first : last + 1])


def _nearest_highlight_color(rgb: Optional[Sequence[float]]) -> str:
    if not rgb or len(rgb) != 3:
        return ""
    return min(
        HIGHLIGHT_COLORS,
        key=lambda name: sum((a - b) ** 2 for a, b in zip(HIGHLIGHT_COLORS[name], rgb)),
    )


def _split_author_field(author_field: str) -> list[str]:
    text = " ".join(author_field.split())
    if not text:
        return []
    parts = [p.strip() for p in re.split(r";|\band\b|&", text) if p.strip()]
    if len(parts) == 1 and "," in text:
        comma_parts = [p.strip() for p in text.split(",") if p.strip()]
        # "Ada Lovelace, Niels Bohr" is a list; "Lovelace, Ada" is one person.
        if all(" " in part for part in comma_parts):
            parts = comma_parts
    return parts


class PdfDocumentService:
    """One open PDF: render it, annotate it, and save it back safely."""

    def __init__(self) -> None:
        self._document = None
        self._file_path: Optional[Path] = None
        self._has_unsaved_changes = False

    # ------------------------------------------------------------------ lifecycle

    @property
    def is_open(self) -> bool:
        return self._document is not None

    @property
    def file_path(self) -> Optional[Path]:
        return self._file_path

    @property
    def page_count(self) -> int:
        return self._require_document().page_count

    @property
    def has_unsaved_changes(self) -> bool:
        return self._has_unsaved_changes

    def open(self, file_path: Union[str, Path]) -> None:
        """Open a PDF, closing any document that is already open."""
        path = Path(file_path)
        try:
            pdf_bytes = path.read_bytes()
        except OSError as error:
            raise PdfOpenError(f"Could not read '{path.name}': {error}") from error
        document = self._open_bytes(pdf_bytes, path.name)
        self.close()
        self._document = document
        self._file_path = path
        self._has_unsaved_changes = False

    def set_file_path(self, file_path: Union[str, Path]) -> None:
        """Tell the open document that its file was moved, so saving writes
        to the new location."""
        self._require_document()
        self._file_path = Path(file_path)

    def close(self) -> None:
        """Close the document and discard unsaved changes."""
        if self._document is not None:
            self._document.close()
        self._document = None
        self._file_path = None
        self._has_unsaved_changes = False

    @staticmethod
    def _open_bytes(pdf_bytes: bytes, display_name: str):
        try:
            document = pymupdf.open(stream=pdf_bytes, filetype="pdf")
        except Exception as error:
            raise PdfOpenError(f"'{display_name}' is not a readable PDF.") from error
        if document.needs_pass:
            document.close()
            raise PdfOpenError(f"'{display_name}' is password-protected.")
        if document.page_count == 0:
            document.close()
            raise PdfOpenError(f"'{display_name}' is not a readable PDF.")
        return document

    def _require_document(self):
        if self._document is None:
            raise PdfDocumentError("No PDF is open.")
        return self._document

    def _load_page(self, page_number: int):
        document = self._require_document()
        if not 0 <= page_number < document.page_count:
            raise PdfDocumentError(f"Page {page_number + 1} does not exist.")
        return document.load_page(page_number)

    # ------------------------------------------------------------------ rendering

    def page_size(self, page_number: int) -> tuple[float, float]:
        """Width and height of a page in PDF units."""
        rectangle = self._load_page(page_number).rect
        return rectangle.width, rectangle.height

    def render_page(self, page_number: int, scale: float = 1.0) -> RenderedPage:
        """Render a page to RGB pixels.

        ``scale`` is zoom multiplied by the screen's device pixel ratio, so a
        150 % zoom on a 2x high-DPI display uses ``scale=3.0``.
        """
        page = self._load_page(page_number)
        pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
        return RenderedPage(pixmap.width, pixmap.height, pixmap.stride, bytes(pixmap.samples))

    # ------------------------------------------------------------- text selection

    def get_page_words(self, page_number: int) -> list[WordBox]:
        """All words on a page in reading order."""
        page = self._load_page(page_number)
        return [
            WordBox(w[0], w[1], w[2], w[3], w[4], int(w[5]), int(w[6]), int(w[7]))
            for w in page.get_text("words")
        ]

    def get_page_text(self, page_number: int) -> str:
        """Plain text of one page with whitespace collapsed ('' for a scanned page)."""
        return " ".join(self._load_page(page_number).get_text("text").split())

    def page_needs_ocr(self, page_number: int) -> bool:
        """True when a page has almost no text of its own, as a scanned page does."""
        return len(self.get_page_text(page_number)) < _SPARSE_PAGE_CHARACTERS

    def render_page_png(self, page_number: int, scale: float = 3.0) -> bytes:
        """A page as PNG image data, for OCR. Scale 3.0 is about 216 dots per inch."""
        page = self._load_page(page_number)
        return page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False).tobytes("png")

    def find_text_rectangles(
        self, page_number: int, text: str
    ) -> list[tuple[float, float, float, float]]:
        """Where a word or phrase appears on a page."""
        if not text.strip():
            return []
        page = self._load_page(page_number)
        return [(r.x0, r.y0, r.x1, r.y1) for r in page.search_for(text.strip())]

    def select_words(
        self,
        page_number: int,
        start_point: tuple[float, float],
        end_point: tuple[float, float],
    ) -> list[WordBox]:
        """Words between two points in reading order, as when dragging over text."""
        return words_between_points(self.get_page_words(page_number), start_point, end_point)

    # ---------------------------------------------------------------- annotations

    def add_highlight(
        self,
        page_number: int,
        words: Sequence[WordBox],
        color_name: str = DEFAULT_HIGHLIGHT_COLOR,
    ) -> None:
        """Highlight the given words (normally the result of ``select_words``)."""
        if color_name not in HIGHLIGHT_COLORS:
            raise PdfEditError(f"Unknown highlight color: {color_name!r}")
        if not words:
            raise PdfEditError("Select some text before highlighting.")
        page = self._load_page(page_number)

        # One rectangle per line of text, so the highlight follows the lines.
        line_rectangles: dict[tuple[int, int], list[float]] = {}
        for word in words:
            key = (word.block_number, word.line_number)
            bounds = line_rectangles.get(key)
            if bounds is None:
                line_rectangles[key] = [word.x0, word.y0, word.x1, word.y1]
            else:
                bounds[0] = min(bounds[0], word.x0)
                bounds[1] = min(bounds[1], word.y0)
                bounds[2] = max(bounds[2], word.x1)
                bounds[3] = max(bounds[3], word.y1)

        annotation = page.add_highlight_annot(
            [pymupdf.Rect(*bounds) for bounds in line_rectangles.values()]
        )
        annotation.set_colors(stroke=HIGHLIGHT_COLORS[color_name])
        annotation.update()
        self._has_unsaved_changes = True

    def add_sticky_note(self, page_number: int, point: tuple[float, float], note_text: str) -> None:
        """Add a comment icon at a point; the text shows when it is opened."""
        if not note_text.strip():
            raise PdfEditError("The note is empty.")
        page = self._load_page(page_number)
        annotation = page.add_text_annot(pymupdf.Point(*point), note_text.strip(), icon="Note")
        annotation.update()
        self._has_unsaved_changes = True

    def add_text_box(
        self,
        page_number: int,
        rectangle: tuple[float, float, float, float],
        text: str,
        font_size: float = 11.0,
    ) -> None:
        """Add text that is always visible on the page."""
        if not text.strip():
            raise PdfEditError("The text box is empty.")
        page = self._load_page(page_number)
        annotation = page.add_freetext_annot(
            pymupdf.Rect(*rectangle),
            text.strip(),
            fontsize=font_size,
            text_color=(0.1, 0.1, 0.1),
            fill_color=(1.0, 1.0, 0.85),
        )
        annotation.update()
        self._has_unsaved_changes = True

    def add_pen_stroke(self, page_number: int, points: Sequence[tuple[float, float]]) -> None:
        """Add a freehand line through the given points."""
        if len(points) < 2:
            raise PdfEditError("Drag on the page to draw.")
        page = self._load_page(page_number)
        annotation = page.add_ink_annot([[(float(x), float(y)) for x, y in points]])
        annotation.set_colors(stroke=_DRAWING_COLOR)
        annotation.set_border(width=_DRAWING_WIDTH)
        annotation.update()
        self._has_unsaved_changes = True

    def add_arrow(
        self, page_number: int, start: tuple[float, float], end: tuple[float, float]
    ) -> None:
        """Add a straight arrow pointing at ``end``."""
        if math.dist(start, end) < 4.0:
            raise PdfEditError("Drag on the page to draw an arrow.")
        page = self._load_page(page_number)
        annotation = page.add_line_annot(pymupdf.Point(*start), pymupdf.Point(*end))
        annotation.set_line_ends(pymupdf.PDF_ANNOT_LE_NONE, pymupdf.PDF_ANNOT_LE_OPEN_ARROW)
        annotation.set_colors(stroke=_DRAWING_COLOR)
        annotation.set_border(width=_DRAWING_WIDTH)
        annotation.update()
        self._has_unsaved_changes = True

    def delete_annotation(self, page_number: int, xref: int) -> None:
        page = self._load_page(page_number)
        for annotation in list(page.annots() or []):
            if annotation.xref == xref:
                page.delete_annot(annotation)
                self._has_unsaved_changes = True
                return
        raise PdfEditError("That annotation no longer exists.")

    def list_page_annotations(self, page_number: int) -> list[PageAnnotation]:
        """Highlights, sticky notes and text boxes on one page."""
        page = self._load_page(page_number)
        words: Optional[list[WordBox]] = None
        results: list[PageAnnotation] = []
        for annotation in page.annots() or []:
            type_code = annotation.type[0]
            note_text = (annotation.info.get("content") or "").strip()
            rectangle = annotation.rect
            bounds = (rectangle.x0, rectangle.y0, rectangle.x1, rectangle.y1)

            if type_code == pymupdf.PDF_ANNOT_HIGHLIGHT:
                if words is None:
                    words = self.get_page_words(page_number)
                results.append(
                    PageAnnotation(
                        xref=annotation.xref,
                        page_number=page_number,
                        annotation_type="highlight",
                        rectangle=bounds,
                        color=_nearest_highlight_color(annotation.colors.get("stroke")),
                        selected_text=self._text_under_highlight(annotation, words),
                        note_text=note_text,
                    )
                )
            elif type_code == pymupdf.PDF_ANNOT_TEXT:
                results.append(
                    PageAnnotation(annotation.xref, page_number, "note", bounds, "", "", note_text)
                )
            elif type_code == pymupdf.PDF_ANNOT_FREE_TEXT:
                results.append(
                    PageAnnotation(annotation.xref, page_number, "text", bounds, "", "", note_text)
                )
            elif type_code in (pymupdf.PDF_ANNOT_INK, pymupdf.PDF_ANNOT_LINE):
                results.append(
                    PageAnnotation(annotation.xref, page_number, DRAWING_TYPE, bounds, "", "", "")
                )
        return results

    @staticmethod
    def _text_under_highlight(annotation, words: Sequence[WordBox]) -> str:
        """Recover highlighted text from the page, so highlights made in other
        PDF programs are indexed too."""
        vertices = annotation.vertices or []
        regions: list[tuple[float, float, float, float]] = []
        for start in range(0, len(vertices) - 3, 4):
            corners = vertices[start : start + 4]
            xs = [corner[0] for corner in corners]
            ys = [corner[1] for corner in corners]
            regions.append((min(xs), min(ys), max(xs), max(ys)))
        if not regions:
            rectangle = annotation.rect
            regions.append((rectangle.x0, rectangle.y0, rectangle.x1, rectangle.y1))

        selected = []
        for word in words:
            center_x = (word.x0 + word.x1) / 2
            center_y = (word.y0 + word.y1) / 2
            if any(
                x0 - 0.5 <= center_x <= x1 + 0.5 and y0 - 0.5 <= center_y <= y1 + 0.5
                for x0, y0, x1, y1 in regions
            ):
                selected.append(word.text)
        return " ".join(selected)

    def extract_annotations(self) -> list[AnnotationDraft]:
        """Every highlight, note and text box in the document, ready to store.

        Pen strokes and arrows carry no text, so they are not included.
        """
        drafts: list[AnnotationDraft] = []
        for page_number in range(self.page_count):
            for annotation in self.list_page_annotations(page_number):
                if annotation.annotation_type == DRAWING_TYPE:
                    continue
                drafts.append(
                    AnnotationDraft(
                        page_number=annotation.page_number,
                        annotation_type=annotation.annotation_type,
                        color=annotation.color,
                        selected_text=annotation.selected_text,
                        note_text=annotation.note_text,
                    )
                )
        return drafts

    # ---------------------------------------------------------------------- pages

    def delete_page(self, page_number: int) -> None:
        document = self._require_document()
        if document.page_count <= 1:
            raise PdfEditError("A PDF must keep at least one page.")
        self._load_page(page_number)  # validates the page number
        document.delete_page(page_number)
        self._has_unsaved_changes = True

    # ----------------------------------------------------------- text and metadata

    def extract_analysis_text(self, character_limit: int = ANALYSIS_TEXT_LIMIT) -> str:
        """Text for the AI: the first three pages plus the last page.

        That covers the title, abstract, introduction and conclusion of a
        typical paper. If the total exceeds the limit, the opening pages are
        trimmed and up to a quarter of the limit is kept for the last page.

        A long document is treated as a book: its first twelve pages (title
        page, contents, preface) are used and the last page, usually an index,
        is left out.
        """
        document = self._require_document()
        page_count = document.page_count
        is_book = page_count >= _BOOK_MINIMUM_PAGES
        leading_page_count = min(
            _BOOK_LEADING_PAGES if is_book else _ANALYSIS_LEADING_PAGES, page_count
        )

        def page_text(page_number: int) -> str:
            return " ".join(document.load_page(page_number).get_text("text").split())

        leading_text = "\n\n".join(page_text(n) for n in range(leading_page_count))
        closing_text = ""
        if page_count > leading_page_count and not is_book:
            closing_text = page_text(page_count - 1)

        if len(leading_text) + len(closing_text) + 2 <= character_limit:
            return "\n\n".join(part for part in (leading_text, closing_text) if part)

        closing_text = closing_text[-(character_limit // 4) :] if closing_text else ""
        leading_budget = max(character_limit - len(closing_text) - 2, 0)
        combined = "\n\n".join(
            part for part in (leading_text[:leading_budget], closing_text) if part
        )
        return combined[:character_limit]

    def read_embedded_metadata(self) -> EmbeddedMetadata:
        """Title, authors, year and DOI stored in the PDF, where present."""
        document = self._require_document()
        metadata = document.metadata or {}

        title = " ".join((metadata.get("title") or "").split())
        if _PLACEHOLDER_TITLE_PATTERN.match(title):
            title = ""
        if not title:
            title = self._find_title_on_first_page()

        publication_year = None
        year_match = _YEAR_PATTERN.search(metadata.get("creationDate") or "")
        if year_match:
            publication_year = int(year_match.group(0))

        doi = ""
        for page_number in range(min(2, document.page_count)):
            doi_match = _DOI_PATTERN.search(document.load_page(page_number).get_text("text"))
            if doi_match:
                doi = doi_match.group(0).rstrip(".,;:)]}")
                break

        return EmbeddedMetadata(
            title=title,
            authors=_split_author_field(metadata.get("author") or ""),
            publication_year=publication_year,
            doi=doi,
        )

    def _find_title_on_first_page(self) -> str:
        """Best guess at the title: the largest text on page one.

        Used when the PDF carries no usable title of its own, which is common
        for journal articles. Returns "" when nothing stands out from the
        body text, so the caller can fall back to the file name.
        """
        page = self._require_document().load_page(0)
        lines: list[tuple[float, str]] = []
        characters_by_size: dict[float, int] = {}
        for block in page.get_text("dict").get("blocks", []):
            for line in block.get("lines", []):
                direction = line.get("dir", (1.0, 0.0))
                if abs(direction[0] - 1.0) > 0.01 or abs(direction[1]) > 0.01:
                    continue  # rotated text such as margin watermarks
                spans = [span for span in line.get("spans", []) if span.get("text", "").strip()]
                if not spans:
                    continue
                line_text = " ".join("".join(span["text"] for span in spans).split())
                line_size = round(max(span["size"] for span in spans), 1)
                lines.append((line_size, line_text))
                characters_by_size[line_size] = (
                    characters_by_size.get(line_size, 0) + len(line_text)
                )
        if not lines:
            return ""

        body_size = max(characters_by_size, key=characters_by_size.get)
        larger_sizes = sorted(
            (size for size in characters_by_size if size > body_size + _TITLE_SIZE_TOLERANCE),
            reverse=True,
        )
        for candidate_size in larger_sizes[:3]:
            title_lines: list[str] = []
            for line_size, line_text in lines:
                if abs(line_size - candidate_size) <= _TITLE_SIZE_TOLERANCE:
                    title_lines.append(line_text)
                elif title_lines:
                    break  # the run of title-sized lines has ended
            candidate = " ".join(title_lines)
            if (
                _TITLE_MINIMUM_LENGTH <= len(candidate) <= _TITLE_MAXIMUM_LENGTH
                and len(candidate.split()) >= 2
                and sum(character.isalpha() for character in candidate) >= len(candidate) / 2
            ):
                return candidate
        return ""

    # --------------------------------------------------------------------- saving

    def save_and_replace(self) -> SaveResult:
        """Write the edited PDF over the original without ever risking the original.

        1. Save to a temporary file beside the original and check it opens.
        2. Close the in-memory document.
        3. Copy the original to a backup, then swap the temporary file into
           place with ``os.replace`` (a single atomic operation).
        4. Reopen from disk; only then delete the backup.

        If anything fails, the original file is left as it was, the edits stay
        open in memory, and :class:`PdfSaveError` explains what happened.
        """
        document = self._require_document()
        original_path = self._file_path
        assert original_path is not None
        token = uuid.uuid4().hex
        temporary_path = original_path.parent / f".tmp_{token}.pdf"
        backup_path = original_path.parent / f".backup_{token}.pdf"

        try:
            document.save(str(temporary_path), garbage=4, deflate=True)
            saved_bytes = temporary_path.read_bytes()
            check_document = self._open_bytes(saved_bytes, temporary_path.name)
            saved_page_count = check_document.page_count
            check_document.close()
            if saved_page_count != document.page_count:
                raise PdfSaveError("The saved copy has the wrong number of pages.")
        except Exception as error:
            _remove_quietly(temporary_path)
            raise PdfSaveError(f"Could not write the updated PDF: {error}") from error

        document.close()
        self._document = None

        try:
            if original_path.exists():
                shutil.copy2(original_path, backup_path)
            os.replace(temporary_path, original_path)
        except OSError as error:
            self._restore_unsaved_document(saved_bytes, original_path.name)
            _remove_quietly(temporary_path)
            _remove_quietly(backup_path)
            raise PdfSaveError(
                f"Could not replace '{original_path.name}'. If it is open in another "
                "program, close it there and save again. Your changes are still here."
            ) from error

        try:
            self._document = self._open_bytes(original_path.read_bytes(), original_path.name)
        except (OSError, PdfOpenError) as error:
            if backup_path.exists():
                os.replace(backup_path, original_path)
            self._restore_unsaved_document(saved_bytes, original_path.name)
            raise PdfSaveError(
                "The saved PDF could not be reopened, so the previous version was restored."
            ) from error

        _remove_quietly(backup_path)
        self._has_unsaved_changes = False
        return SaveResult(
            page_count=self._document.page_count,
            annotations=self.extract_annotations(),
        )

    def _restore_unsaved_document(self, saved_bytes: bytes, display_name: str) -> None:
        self._document = self._open_bytes(saved_bytes, display_name)
        self._has_unsaved_changes = True
