"""Tests for exporting highlights and notes."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shodhkosha.annotation_export import describe_annotation, format_annotations_markdown  # noqa: E402
from shodhkosha.library_database import AnnotationDraft  # noqa: E402


class AnnotationExportTests(unittest.TestCase):
    def test_markdown_groups_by_page(self) -> None:
        annotations = [
            AnnotationDraft(2, "note", note_text="Check   the derivation"),
            AnnotationDraft(0, "highlight", "yellow", selected_text="thermal  diffusivity"),
            AnnotationDraft(0, "highlight", "green", selected_text="gold", note_text="why gold?"),
            AnnotationDraft(2, "text", note_text="Compare with Fig. 3"),
        ]
        self.assertEqual(
            format_annotations_markdown(" My Paper ", annotations),
            "# My Paper\n\n"
            "## Page 1\n\n"
            "> thermal diffusivity\n\n"
            "> gold\n\n"
            "**Note:** why gold?\n\n"
            "## Page 3\n\n"
            "**Note:** Check the derivation\n\n"
            "**Text box:** Compare with Fig. 3\n",
        )

    def test_empty_export(self) -> None:
        self.assertEqual(
            format_annotations_markdown("", []), "# Untitled\n\nNo highlights or notes.\n"
        )

    def test_describe_annotation(self) -> None:
        highlight = AnnotationDraft(4, "highlight", "yellow", selected_text="word " * 60)
        description = describe_annotation(highlight)
        self.assertTrue(description.startswith("p. 5 · Highlight: word word"))
        self.assertTrue(description.endswith("…"))
        self.assertEqual(
            describe_annotation(AnnotationDraft(0, "note", note_text="Key result")),
            "p. 1 · Note: Key result",
        )


if __name__ == "__main__":
    unittest.main()
