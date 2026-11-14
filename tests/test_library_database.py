"""Tests for the SQLite layer. Run from the project folder with:

    python -m unittest discover tests -v
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shodhkosha.library_database import (  # noqa: E402
    AnnotationDraft,
    DuplicateNameError,
    DuplicatePaperError,
    LibraryDatabase,
    PageText,
    PaperFilter,
    RecordNotFoundError,
)


class LibraryDatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.database = LibraryDatabase(":memory:")

    def tearDown(self) -> None:
        self.database.close()

    def _add_sample_paper(self, suffix: str = "a", **overrides):
        details = dict(
            title="Photothermal imaging of single gold nanoparticles",
            authors=["Ada Lovelace", "Niels Bohr"],
            publication_year=2021,
            tag_names=["microscopy", "plasmonics"],
        )
        details.update(overrides)
        return self.database.add_paper(f"library/paper_{suffix}.pdf", f"hash_{suffix}", **details)

    def test_folder_names_are_unique_ignoring_case(self) -> None:
        folder = self.database.create_folder("  Optical   Metrology ")
        self.assertEqual(folder.name, "Optical Metrology")
        with self.assertRaises(DuplicateNameError):
            self.database.create_folder("optical metrology")
        self.assertEqual(
            self.database.get_or_create_folder("OPTICAL METROLOGY").folder_id, folder.folder_id
        )
        with self.assertRaises(ValueError):
            self.database.create_folder("   ")

    def test_add_paper_round_trip(self) -> None:
        folder = self.database.create_folder("Microscopy")
        paper = self._add_sample_paper(folder_id=folder.folder_id)
        self.assertEqual(paper.authors, ["Ada Lovelace", "Niels Bohr"])
        self.assertEqual(paper.tags, ["microscopy", "plasmonics"])
        self.assertEqual(paper.folder_name, "Microscopy")
        self.assertFalse(paper.metadata_verified)
        self.assertEqual(self.database.list_folders()[0].paper_count, 1)

    def test_duplicate_file_is_rejected(self) -> None:
        paper = self._add_sample_paper()
        with self.assertRaises(DuplicatePaperError) as raised:
            self.database.add_paper("library/copy.pdf", "hash_a")
        self.assertEqual(raised.exception.existing_paper_id, paper.paper_id)
        self.assertEqual(self.database.count_papers(), 1)

    def test_tags_are_reused_ignoring_case(self) -> None:
        self._add_sample_paper("a", tag_names=["Plasmonics"])
        self._add_sample_paper("b", tag_names=["plasmonics", "plasmonics", " "])
        tags = self.database.list_tags()
        self.assertEqual([(t.name, t.paper_count) for t in tags], [("Plasmonics", 2)])

    def test_search_matches_prefixes_across_fields(self) -> None:
        folder = self.database.create_folder("Thermal Lensing")
        first = self._add_sample_paper("a", folder_id=folder.folder_id)
        second = self._add_sample_paper(
            "b", title="Knife-edge beam profiling", authors=["Marie Curie"],
            publication_year=2019, tag_names=["beam"],
        )

        def search(text: str) -> list[int]:
            return [p.paper_id for p in self.database.search_papers(PaperFilter(search_text=text))]

        self.assertEqual(search("phototh"), [first.paper_id])       # title prefix
        self.assertEqual(search("curie"), [second.paper_id])        # author
        self.assertEqual(search("2019"), [second.paper_id])         # year
        self.assertEqual(search("lensing"), [first.paper_id])       # folder name
        self.assertEqual(search("plasmon gold"), [first.paper_id])  # tag + title, all words
        self.assertEqual(search("gold curie"), [])
        self.assertEqual(search('knife-edge "'), [second.paper_id])  # punctuation is safe
        self.assertEqual(len(search("")), 2)

    def test_search_finds_text_inside_highlights(self) -> None:
        paper = self._add_sample_paper()
        self.database.replace_annotations(
            paper.paper_id,
            [
                AnnotationDraft(0, "highlight", "yellow", selected_text="thermal diffusivity"),
                AnnotationDraft(2, "note", note_text="compare with PDMS substrate"),
            ],
        )
        found = self.database.search_papers(PaperFilter(search_text="diffusiv"))
        self.assertEqual([p.paper_id for p in found], [paper.paper_id])
        self.assertEqual(len(self.database.search_papers(PaperFilter(search_text="pdms"))), 1)

        # Replacing with a new list removes the old text from search.
        self.database.replace_annotations(paper.paper_id, [])
        self.assertEqual(self.database.search_papers(PaperFilter(search_text="diffusiv")), [])
        self.assertEqual(self.database.list_annotations(paper.paper_id), [])

    def test_filters_by_folder_tag_and_unfiled(self) -> None:
        folder = self.database.create_folder("Microscopy")
        filed = self._add_sample_paper("a", folder_id=folder.folder_id)
        unfiled = self._add_sample_paper("b", tag_names=["beam"])
        beam_tag = self.database.get_or_create_tag("beam")

        def ids(paper_filter: PaperFilter) -> list[int]:
            return [p.paper_id for p in self.database.search_papers(paper_filter)]

        self.assertEqual(ids(PaperFilter(folder_id=folder.folder_id)), [filed.paper_id])
        self.assertEqual(ids(PaperFilter(unfiled_only=True)), [unfiled.paper_id])
        self.assertEqual(ids(PaperFilter(tag_id=beam_tag.tag_id)), [unfiled.paper_id])
        self.assertEqual(self.database.count_unfiled_papers(), 1)

    def test_update_metadata_and_search_stay_in_sync(self) -> None:
        paper = self._add_sample_paper()
        updated = self.database.update_paper_metadata(
            paper.paper_id, title="Absorption spectroscopy", authors=["Max Planck"],
            metadata_verified=True,
        )
        self.assertEqual(updated.title, "Absorption spectroscopy")
        self.assertTrue(updated.metadata_verified)
        self.assertEqual(self.database.search_papers(PaperFilter(search_text="photothermal")), [])
        self.assertEqual(len(self.database.search_papers(PaperFilter(search_text="planck"))), 1)
        with self.assertRaises(ValueError):
            self.database.update_paper_metadata(paper.paper_id, file_hash="x")
        with self.assertRaises(RecordNotFoundError):
            self.database.update_paper_metadata(999, title="x")

    def test_renaming_and_deleting_folder_updates_papers(self) -> None:
        folder = self.database.create_folder("Old Name")
        paper = self._add_sample_paper(folder_id=folder.folder_id)
        self.database.rename_folder(folder.folder_id, "Spectroscopy")
        self.assertEqual(len(self.database.search_papers(PaperFilter(search_text="spectros"))), 1)

        self.database.delete_folder(folder.folder_id)
        self.assertIsNone(self.database.get_paper(paper.paper_id).folder_id)
        self.assertEqual(self.database.search_papers(PaperFilter(search_text="spectros")), [])

    def test_deleting_paper_removes_everything_attached(self) -> None:
        paper = self._add_sample_paper()
        self.database.replace_annotations(
            paper.paper_id, [AnnotationDraft(0, "highlight", selected_text="gold")]
        )
        self.database.delete_paper(paper.paper_id)
        self.assertEqual(self.database.count_papers(), 0)
        self.assertEqual(self.database.search_papers(PaperFilter(search_text="gold")), [])
        self.assertEqual(self.database.delete_unused_tags(), 2)

    def test_failed_transaction_rolls_back_every_change(self) -> None:
        with self.assertRaises(RuntimeError):
            with self.database.transaction():
                self.database.create_folder("Temporary")
                self._add_sample_paper()
                raise RuntimeError("simulated failure")
        self.assertEqual(self.database.list_folders(), [])
        self.assertEqual(self.database.count_papers(), 0)
        self.assertEqual(self.database.list_tags(), [])

    def test_unknown_annotation_type_is_rejected(self) -> None:
        paper = self._add_sample_paper()
        with self.assertRaises(ValueError):
            self.database.replace_annotations(paper.paper_id, [AnnotationDraft(0, "scribble")])

    def test_full_text_search_finds_words_on_any_page(self) -> None:
        paper = self._add_sample_paper("a")
        other = self._add_sample_paper("b", title="Unrelated")
        self.assertEqual(self.database.list_unindexed_paper_ids(), [paper.paper_id, other.paper_id])
        self.database.replace_page_texts(
            paper.paper_id,
            [
                PageText(0, "Introduction to photothermal contrast."),
                PageText(1, "The thermal boundary conductance of the interface was measured."),
                PageText(2, "Conductance results and the boundary conditions used."),
            ],
        )
        self.assertEqual(self.database.list_unindexed_paper_ids(), [other.paper_id])

        found = self.database.search_papers(PaperFilter(search_text="boundary conduct"))
        self.assertEqual([p.paper_id for p in found], [paper.paper_id])
        hits = self.database.search_pages(paper.paper_id, "boundary conduct")
        self.assertEqual([hit.page_number for hit in hits], [1, 2])
        self.assertIn("thermal boundary conductance", hits[0].excerpt)
        self.assertEqual(self.database.search_pages(paper.paper_id, ""), [])
        self.assertEqual(self.database.search_pages(other.paper_id, "boundary"), [])

        # OCR text for one page replaces only that page.
        self.database.set_page_text(paper.paper_id, PageText(0, "scanned zeolite page", True))
        ocr_hits = self.database.search_pages(paper.paper_id, "zeolite")
        self.assertEqual([(h.page_number, h.is_ocr) for h in ocr_hits], [(0, True)])
        self.assertEqual(self.database.search_pages(paper.paper_id, "photothermal contrast"), [])
        self.assertEqual(sorted(self.database.get_page_texts(paper.paper_id)), [0, 1, 2])
        self.assertEqual(
            self.database.get_page_texts(paper.paper_id, [0]), {0: "scanned zeolite page"}
        )

        self.database.mark_text_indexed(paper.paper_id, False)
        self.assertIn(paper.paper_id, self.database.list_unindexed_paper_ids())
        self.database.delete_paper(paper.paper_id)
        self.assertEqual(self.database.search_papers(PaperFilter(search_text="zeolite")), [])

    def test_find_similar_paper_by_doi_or_title(self) -> None:
        paper = self.database.add_paper(
            "a.pdf", "hash_a", title="Photothermal Imaging of Single Gold Nanoparticles",
            doi="10.1021/ACSnano.2c09212",
        )
        self.assertEqual(
            self.database.find_similar_paper(doi=" 10.1021/acsnano.2c09212 ").paper_id,
            paper.paper_id,
        )
        self.assertEqual(
            self.database.find_similar_paper(
                title="photothermal imaging of single gold-nanoparticles."
            ).paper_id,
            paper.paper_id,
        )
        self.assertIsNone(self.database.find_similar_paper(doi="10.1/other", title="Other Title Here"))
        self.assertIsNone(self.database.find_similar_paper(title="Short"))
        self.assertIsNone(self.database.find_similar_paper())

    def test_study_cards(self) -> None:
        paper = self._add_sample_paper()
        added = self.database.add_study_cards(
            paper.paper_id, [(2, " What is measured? ", "Absorption."), (0, "Which particles?", "Gold.")]
        )
        self.assertEqual(len(added), 2)
        cards = self.database.list_study_cards(paper.paper_id)
        self.assertEqual([(c.page_number, c.question) for c in cards],
                         [(0, "Which particles?"), (2, "What is measured?")])
        self.database.delete_study_card(cards[0].card_id)
        self.assertEqual(len(self.database.list_study_cards(paper.paper_id)), 1)
        self.database.delete_paper(paper.paper_id)
        self.assertEqual(self.database.list_study_cards(paper.paper_id), [])

    def test_version_one_library_is_upgraded(self) -> None:
        import sqlite3

        from shodhkosha import library_database

        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "library.sqlite3"
            connection = sqlite3.connect(database_path)
            connection.executescript(library_database._SCHEMA_SQL + "PRAGMA user_version = 1;")
            connection.execute(
                "INSERT INTO papers (title, file_path, file_hash, date_added, date_modified) "
                "VALUES ('Old Paper', 'old.pdf', 'h', 't', 't')"
            )
            connection.commit()
            connection.close()

            with LibraryDatabase(database_path) as upgraded:
                self.assertEqual(upgraded.count_papers(), 1)
                paper_id = upgraded.list_unindexed_paper_ids()[0]
                upgraded.replace_page_texts(paper_id, [PageText(0, "legacy text")])
                self.assertEqual(len(upgraded.search_pages(paper_id, "legacy")), 1)
                self.assertEqual(upgraded.add_study_cards(paper_id, [(0, "Q", "A")])[0].answer, "A")

    def test_data_and_settings_persist_on_disk(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "nested" / "library.sqlite3"
            with LibraryDatabase(database_path) as database:
                database.set_setting("mirror_folders_on_disk", "1")
                database.set_setting("mirror_folders_on_disk", "0")
                database.add_paper("a.pdf", "hash", title="Persistent")
            with LibraryDatabase(database_path) as reopened:
                self.assertEqual(reopened.get_setting("mirror_folders_on_disk"), "0")
                self.assertEqual(reopened.get_setting("missing", "fallback"), "fallback")
                self.assertEqual(reopened.count_papers(), 1)


if __name__ == "__main__":
    unittest.main()
