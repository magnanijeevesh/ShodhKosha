"""Tests for BibTeX formatting."""

import sys
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shodhkosha.bibtex_formatter import (  # noqa: E402
    AuthorName,
    bibtex_key,
    export_bibtex,
    format_bibtex_entry,
    parse_author_name,
)
from shodhkosha.library_database import Paper  # noqa: E402

ARTICLE = Paper(
    paper_id=1,
    title="Photothermal Imaging of Single Gold Nanoparticles",
    authors=["Ada Lovelace", "Niels H. D. Bohr", "Ludwig van Beethoven"],
    publication_year=2023,
    journal="ACS Nano",
    volume="17",
    issue="1",
    pages="505–514",
    publisher="American Chemical Society",
    doi="10.1021/acsnano.2c09212",
    summary="",
    file_path="paper.pdf",
    file_hash="hash",
    folder_id=None,
    folder_name=None,
    tags=[],
    metadata_verified=True,
    date_added="",
    date_modified="",
)


class BibtexFormatterTests(unittest.TestCase):
    def test_parse_author_name(self) -> None:
        self.assertEqual(parse_author_name("Ada Lovelace"), AuthorName("Ada", "Lovelace"))
        self.assertEqual(parse_author_name("Lovelace, Ada"), AuthorName("Ada", "Lovelace"))
        self.assertEqual(
            parse_author_name("Ludwig van Beethoven"), AuthorName("Ludwig", "van Beethoven")
        )
        self.assertEqual(
            parse_author_name("Martin Luther King Jr."), AuthorName("Martin Luther", "King", "Jr.")
        )
        self.assertEqual(parse_author_name("Aristotle"), AuthorName("", "Aristotle"))

    def test_article_entry(self) -> None:
        self.assertEqual(
            format_bibtex_entry(ARTICLE),
            "@article{lovelace2023photothermal,\n"
            "  author    = {Lovelace, Ada and Bohr, Niels H. D. and van Beethoven, Ludwig},\n"
            "  title     = {{Photothermal Imaging of Single Gold Nanoparticles}},\n"
            "  journal   = {ACS Nano},\n"
            "  year      = {2023},\n"
            "  volume    = {17},\n"
            "  number    = {1},\n"
            "  pages     = {505--514},\n"
            "  publisher = {American Chemical Society},\n"
            "  doi       = {10.1021/acsnano.2c09212},\n"
            "}",
        )

    def test_special_characters_names_and_keys(self) -> None:
        paper = replace(
            ARTICLE,
            title="The 100% Yield of H_2 & O_2",
            authors=["René Müller", "Martin Luther King Jr.", "CERN"],
            journal="",
            pages="505 - 514",
        )
        entry = format_bibtex_entry(paper)
        self.assertTrue(entry.startswith("@book{muller2023100,"))  # publisher, no journal
        self.assertIn(r"{{The 100\% Yield of H\_2 \& O\_2}}", entry)
        self.assertIn("Müller, René and King, Jr., Martin Luther and {CERN}", entry)
        self.assertIn("{505--514}", entry)
        self.assertNotIn("journal", entry)

    def test_entry_with_almost_nothing(self) -> None:
        bare = replace(
            ARTICLE, authors=[], publication_year=None, title="", journal="", volume="",
            issue="", pages="", publisher="", doi="",
        )
        self.assertEqual(bibtex_key(bare), "reference1")
        self.assertEqual(format_bibtex_entry(bare), "@misc{reference1,\n  title = {{}},\n}")

    def test_export_makes_keys_unique(self) -> None:
        exported = export_bibtex([ARTICLE, replace(ARTICLE, paper_id=2), replace(ARTICLE, paper_id=3)])
        self.assertIn("@article{lovelace2023photothermal,", exported)
        self.assertIn("@article{lovelace2023photothermal-b,", exported)
        self.assertIn("@article{lovelace2023photothermal-c,", exported)
        self.assertTrue(exported.endswith("}\n"))
        self.assertEqual(export_bibtex([]), "")


if __name__ == "__main__":
    unittest.main()
