"""Tests for the PDF layer. Requires PyMuPDF (pip install pymupdf). Run with:

    python -m unittest discover tests -v
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    import pymupdf
except ImportError:
    import fitz as pymupdf

from shodhkosha.pdf_document_service import (  # noqa: E402
    PdfDocumentService,
    PdfEditError,
    PdfOpenError,
    PdfSaveError,
    compute_file_hash,
)


def create_sample_pdf(file_path: Path, page_count: int = 4) -> None:
    document = pymupdf.open()
    for index in range(page_count):
        page = document.new_page()
        page.insert_text(
            (72, 100), f"Page {index + 1} thermal diffusivity of gold nanoparticles", fontsize=12
        )
        page.insert_text(
            (72, 130), "Photothermal microscopy study doi:10.1234/abcd.5678.", fontsize=12
        )
    document.set_metadata(
        {
            "title": "Sample Paper",
            "author": "Ada Lovelace; Niels Bohr",
            "creationDate": "D:20210415120000",
        }
    )
    document.save(str(file_path))
    document.close()


class PdfDocumentServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.directory = Path(self._directory.name)
        self.pdf_path = self.directory / "sample.pdf"
        create_sample_pdf(self.pdf_path)
        self.service = PdfDocumentService()
        self.service.open(self.pdf_path)

    def tearDown(self) -> None:
        self.service.close()
        self._directory.cleanup()

    def _leftover_files(self) -> list[str]:
        return sorted(p.name for p in self.directory.iterdir() if p.name != "sample.pdf")

    def _highlight_words(self, first_index: int, last_index: int, color_name: str) -> None:
        words = self.service.get_page_words(0)
        start, end = words[first_index], words[last_index]
        selection = self.service.select_words(
            0,
            ((start.x0 + start.x1) / 2, (start.y0 + start.y1) / 2),
            ((end.x0 + end.x1) / 2, (end.y0 + end.y1) / 2),
        )
        self.service.add_highlight(0, selection, color_name)

    def test_open_reports_pages_and_no_changes(self) -> None:
        self.assertTrue(self.service.is_open)
        self.assertEqual(self.service.page_count, 4)
        self.assertFalse(self.service.has_unsaved_changes)

    def test_invalid_file_is_rejected(self) -> None:
        bad_path = self.directory / "notes.pdf"
        bad_path.write_text("this is not a pdf")
        with self.assertRaises(PdfOpenError):
            PdfDocumentService().open(bad_path)
        with self.assertRaises(PdfOpenError):
            PdfDocumentService().open(self.directory / "missing.pdf")

    def test_render_scales_with_zoom(self) -> None:
        normal = self.service.render_page(0, scale=1.0)
        doubled = self.service.render_page(0, scale=2.0)
        self.assertEqual(len(normal.samples), normal.stride * normal.height)
        self.assertAlmostEqual(doubled.width / normal.width, 2.0, places=1)
        width, height = self.service.page_size(0)
        self.assertAlmostEqual(normal.width, width, delta=1)
        self.assertAlmostEqual(normal.height, height, delta=1)

    def test_select_words_follows_reading_order(self) -> None:
        words = self.service.get_page_words(0)
        self.assertEqual([w.text for w in words[:4]], ["Page", "1", "thermal", "diffusivity"])
        thermal, microscopy = words[2], words[8]
        self.assertEqual(microscopy.text, "microscopy")
        # Dragging backwards (end before start) selects the same words.
        selection = self.service.select_words(
            0, (microscopy.x0 + 1, microscopy.y0 + 1), (thermal.x0 + 1, thermal.y0 + 1)
        )
        self.assertEqual(
            " ".join(w.text for w in selection),
            "thermal diffusivity of gold nanoparticles Photothermal microscopy",
        )

    def test_highlight_text_and_color_are_recovered(self) -> None:
        self._highlight_words(2, 3, "green")
        self.assertTrue(self.service.has_unsaved_changes)
        annotations = self.service.extract_annotations()
        self.assertEqual(len(annotations), 1)
        self.assertEqual(annotations[0].annotation_type, "highlight")
        self.assertEqual(annotations[0].selected_text, "thermal diffusivity")
        self.assertEqual(annotations[0].color, "green")
        self.assertEqual(annotations[0].page_number, 0)

    def test_multi_line_highlight_keeps_all_words(self) -> None:
        self._highlight_words(5, 8, "pink")
        annotation = self.service.extract_annotations()[0]
        self.assertEqual(annotation.selected_text, "gold nanoparticles Photothermal microscopy")
        self.assertEqual(annotation.color, "pink")

    def test_notes_text_boxes_and_deletion(self) -> None:
        self.service.add_sticky_note(1, (300, 300), "Check this derivation")
        self.service.add_text_box(1, (72, 400, 300, 440), "Compare with Fig. 3")
        with self.assertRaises(PdfEditError):
            self.service.add_sticky_note(1, (10, 10), "   ")
        with self.assertRaises(PdfEditError):
            self.service.add_highlight(0, [], "yellow")
        with self.assertRaises(PdfEditError):
            self.service.add_highlight(0, self.service.get_page_words(0)[:1], "purple")

        annotations = self.service.list_page_annotations(1)
        self.assertEqual(
            sorted((a.annotation_type, a.note_text) for a in annotations),
            [("note", "Check this derivation"), ("text", "Compare with Fig. 3")],
        )
        self.service.delete_annotation(1, annotations[0].xref)
        self.assertEqual(len(self.service.list_page_annotations(1)), 1)
        with self.assertRaises(PdfEditError):
            self.service.delete_annotation(1, 999999)

    def test_pen_strokes_and_arrows(self) -> None:
        self.service.add_pen_stroke(0, [(100, 300), (120, 310), (140, 300)])
        self.service.add_arrow(0, (100, 400), (200, 420))
        with self.assertRaises(PdfEditError):
            self.service.add_pen_stroke(0, [(1, 1)])
        with self.assertRaises(PdfEditError):
            self.service.add_arrow(0, (5, 5), (6, 6))
        drawings = self.service.list_page_annotations(0)
        self.assertEqual([a.annotation_type for a in drawings], ["drawing", "drawing"])
        self.assertEqual(self.service.extract_annotations(), [])  # drawings hold no text
        self.service.delete_annotation(0, drawings[0].xref)
        self.assertEqual(len(self.service.list_page_annotations(0)), 1)
        self.assertEqual(self.service.save_and_replace().annotations, [])
        self.assertEqual(len(self.service.list_page_annotations(0)), 1)

    def test_page_text_helpers(self) -> None:
        self.assertIn("thermal diffusivity", self.service.get_page_text(0))
        self.assertFalse(self.service.page_needs_ocr(0))
        blank_path = self.directory / "blank.pdf"
        document = pymupdf.open()
        document.new_page()
        document.save(str(blank_path))
        document.close()
        blank = PdfDocumentService()
        blank.open(blank_path)
        self.assertTrue(blank.page_needs_ocr(0))
        self.assertTrue(blank.render_page_png(0, 1.0).startswith(b"\x89PNG"))
        blank.close()
        rectangles = self.service.find_text_rectangles(0, "diffusivity")
        self.assertEqual(len(rectangles), 1)
        self.assertEqual(self.service.find_text_rectangles(0, "   "), [])
        self.assertEqual(self.service.find_text_rectangles(0, "zeolite"), [])

    def test_long_documents_use_front_matter_for_analysis(self) -> None:
        book_path = self.directory / "book.pdf"
        create_sample_pdf(book_path, page_count=70)
        book = PdfDocumentService()
        book.open(book_path)
        text = book.extract_analysis_text()
        self.assertIn("Page 12 ", text)
        self.assertNotIn("Page 13 ", text)
        self.assertNotIn("Page 70 ", text)
        book.close()

    def test_delete_page_keeps_at_least_one(self) -> None:
        self.service.delete_page(1)
        self.assertEqual(self.service.page_count, 3)
        self.assertEqual(self.service.get_page_words(1)[1].text, "3")
        self.service.delete_page(0)
        self.service.delete_page(0)
        with self.assertRaises(PdfEditError):
            self.service.delete_page(0)

    def test_save_and_replace_persists_edits_and_cleans_up(self) -> None:
        hash_before = compute_file_hash(self.pdf_path)
        self._highlight_words(2, 3, "yellow")
        self.service.add_sticky_note(2, (200, 200), "Key result")
        self.service.delete_page(3)

        result = self.service.save_and_replace()

        self.assertEqual(result.page_count, 3)
        self.assertEqual(
            sorted((a.annotation_type, a.page_number) for a in result.annotations),
            [("highlight", 0), ("note", 2)],
        )
        self.assertFalse(self.service.has_unsaved_changes)
        self.assertTrue(self.service.is_open)
        self.assertEqual(self._leftover_files(), [])
        self.assertNotEqual(compute_file_hash(self.pdf_path), hash_before)

        reopened = PdfDocumentService()
        reopened.open(self.pdf_path)
        self.assertEqual(reopened.page_count, 3)
        self.assertEqual(
            [a.selected_text for a in reopened.extract_annotations() if a.selected_text],
            ["thermal diffusivity"],
        )
        reopened.close()

        # A second round of edits on the reopened document also saves cleanly.
        self.service.add_sticky_note(0, (100, 300), "Second pass")
        self.assertEqual(len(self.service.save_and_replace().annotations), 3)

    def test_failed_replace_keeps_original_file_and_edits(self) -> None:
        original_bytes = self.pdf_path.read_bytes()
        self._highlight_words(2, 3, "yellow")

        with mock.patch(
            "shodhkosha.pdf_document_service.os.replace",
            side_effect=PermissionError("file is open elsewhere"),
        ):
            with self.assertRaises(PdfSaveError):
                self.service.save_and_replace()

        self.assertEqual(self.pdf_path.read_bytes(), original_bytes)
        self.assertEqual(self._leftover_files(), [])
        self.assertTrue(self.service.is_open)
        self.assertTrue(self.service.has_unsaved_changes)
        self.assertEqual(len(self.service.extract_annotations()), 1)

        # Once the file is free again, the same edits save normally.
        self.assertEqual(len(self.service.save_and_replace().annotations), 1)

    def test_embedded_metadata_and_doi(self) -> None:
        metadata = self.service.read_embedded_metadata()
        self.assertEqual(metadata.title, "Sample Paper")
        self.assertEqual(metadata.authors, ["Ada Lovelace", "Niels Bohr"])
        self.assertEqual(metadata.publication_year, 2021)
        self.assertEqual(metadata.doi, "10.1234/abcd.5678")

    def test_title_is_found_on_first_page_when_not_embedded(self) -> None:
        untitled_path = self.directory / "nn2c09212.pdf"
        document = pymupdf.open()
        page = document.new_page()
        page.insert_text((72, 60), "www.journal.org", fontsize=8)
        page.insert_text((72, 100), "Photothermal Imaging of Single", fontsize=20)
        page.insert_text((72, 126), "Gold Nanoparticles", fontsize=20)
        page.insert_text((72, 160), "Ada Lovelace and Niels Bohr", fontsize=12)
        for line_index in range(20):
            page.insert_text(
                (72, 200 + 14 * line_index),
                "Body text of the article that fills most of the first page.",
                fontsize=10,
            )
        document.set_metadata({"title": "nn2c09212 1..10"})
        document.save(str(untitled_path))
        document.close()

        other = PdfDocumentService()
        other.open(untitled_path)
        self.assertEqual(
            other.read_embedded_metadata().title,
            "Photothermal Imaging of Single Gold Nanoparticles",
        )
        other.close()

    def test_analysis_text_uses_first_three_and_last_page(self) -> None:
        text = self.service.extract_analysis_text()
        self.assertIn("Page 1", text)
        self.assertIn("Page 3", text)
        self.assertIn("Page 4", text)
        five_page_path = self.directory / "five.pdf"
        create_sample_pdf(five_page_path, page_count=5)
        other = PdfDocumentService()
        other.open(five_page_path)
        five_page_text = other.extract_analysis_text()
        self.assertNotIn("Page 4", five_page_text)
        self.assertIn("Page 5", five_page_text)
        self.assertLessEqual(len(other.extract_analysis_text(character_limit=120)), 120)
        other.close()


if __name__ == "__main__":
    unittest.main()
