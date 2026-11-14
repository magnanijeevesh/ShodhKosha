"""Tests for importing PDFs into the library. Requires PyMuPDF."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    import pymupdf
except ImportError:
    import fitz as pymupdf

from shodhkosha.library_database import LibraryDatabase  # noqa: E402
from shodhkosha.library_storage import (  # noqa: E402
    LibraryStorage,
    LibraryStorageError,
    collect_pdf_paths,
    sanitize_file_name,
    unique_file_path,
)


def create_pdf(file_path: Path, text: str, title: str = "") -> None:
    document = pymupdf.open()
    document.new_page().insert_text((72, 100), text, fontsize=12)
    if title:
        document.set_metadata({"title": title, "author": "Ada Lovelace"})
    document.save(str(file_path))
    document.close()


class LibraryStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.directory = Path(self._directory.name)
        self.storage = LibraryStorage(self.directory / "library")
        self.database = LibraryDatabase(self.storage.database_path)

    def tearDown(self) -> None:
        self.database.close()
        self._directory.cleanup()

    def test_import_copies_file_and_reads_embedded_details(self) -> None:
        source = self.directory / "lovelace 1843.pdf"
        create_pdf(source, "Notes on the analytical engine", title="Analytical Engine Notes")
        outcome = self.storage.import_pdf(source, self.database)

        self.assertFalse(outcome.is_duplicate)
        self.assertEqual(outcome.paper.title, "Analytical Engine Notes")
        self.assertEqual(outcome.paper.authors, ["Ada Lovelace"])
        self.assertFalse(outcome.paper.metadata_verified)
        library_copy = Path(outcome.paper.file_path)
        self.assertEqual(library_copy.parent, self.storage.papers_directory)
        self.assertTrue(library_copy.exists())
        self.assertTrue(source.exists())

    def test_title_falls_back_to_file_name(self) -> None:
        source = self.directory / "untitled scan.pdf"
        create_pdf(source, "No metadata here")
        self.assertEqual(self.storage.import_pdf(source, self.database).paper.title, "untitled scan")

    def test_same_file_is_not_imported_twice(self) -> None:
        source = self.directory / "paper.pdf"
        create_pdf(source, "Same content")
        first = self.storage.import_pdf(source, self.database)
        second = self.storage.import_pdf(source, self.database)
        self.assertTrue(second.is_duplicate)
        self.assertEqual(second.paper.paper_id, first.paper.paper_id)
        self.assertEqual(len(list(self.storage.papers_directory.iterdir())), 1)

    def test_different_files_with_same_name_both_import(self) -> None:
        first_source = self.directory / "a" / "paper.pdf"
        second_source = self.directory / "b" / "paper.pdf"
        first_source.parent.mkdir()
        second_source.parent.mkdir()
        create_pdf(first_source, "First content")
        create_pdf(second_source, "Second, different content")
        self.storage.import_pdf(first_source, self.database)
        second = self.storage.import_pdf(second_source, self.database)
        self.assertEqual(Path(second.paper.file_path).name, "paper (2).pdf")
        self.assertEqual(self.database.count_papers(), 2)

    def test_unreadable_files_leave_nothing_behind(self) -> None:
        fake_pdf = self.directory / "fake.pdf"
        fake_pdf.write_text("not a pdf")
        text_file = self.directory / "notes.txt"
        text_file.write_text("hello")
        for path in (fake_pdf, text_file):
            with self.assertRaises(LibraryStorageError):
                self.storage.import_pdf(path, self.database)
        self.assertEqual(self.database.count_papers(), 0)
        self.assertEqual(list(self.storage.papers_directory.iterdir()), [])

    def test_remove_paper_deletes_record_and_copy(self) -> None:
        source = self.directory / "paper.pdf"
        create_pdf(source, "Content")
        paper = self.storage.import_pdf(source, self.database).paper
        self.storage.remove_paper(paper, self.database)
        self.assertEqual(self.database.count_papers(), 0)
        self.assertFalse(Path(paper.file_path).exists())
        self.assertTrue(source.exists())

    def test_mirror_folders_moves_files_to_match_folders(self) -> None:
        first_source = self.directory / "first.pdf"
        second_source = self.directory / "second.pdf"
        create_pdf(first_source, "First content")
        create_pdf(second_source, "Second content")
        first = self.storage.import_pdf(first_source, self.database).paper
        second = self.storage.import_pdf(second_source, self.database).paper
        folder = self.database.create_folder('Optics: "Thermal" Lensing?')
        self.database.set_paper_folder(first.paper_id, folder.folder_id)

        outcome = self.storage.mirror_folders(self.database)

        self.assertEqual(list(outcome.moved_files), [first.paper_id])
        self.assertEqual(outcome.failed_file_names, [])
        moved_path = Path(self.database.get_paper(first.paper_id).file_path)
        self.assertEqual(moved_path.parent.name, "Optics Thermal Lensing")
        self.assertTrue(moved_path.exists())
        self.assertEqual(
            Path(self.database.get_paper(second.paper_id).file_path).parent,
            self.storage.papers_directory,
        )
        # Running again changes nothing; unfiling moves the file back and
        # removes the now-empty folder.
        self.assertEqual(self.storage.mirror_folders(self.database).moved_files, {})
        self.database.set_paper_folder(first.paper_id, None)
        self.storage.mirror_folders(self.database)
        self.assertEqual(
            Path(self.database.get_paper(first.paper_id).file_path).parent,
            self.storage.papers_directory,
        )
        self.assertFalse(moved_path.parent.exists())

    def test_similar_paper_is_held_back_unless_allowed(self) -> None:
        first_source = self.directory / "publisher copy.pdf"
        second_source = self.directory / "preprint copy.pdf"
        create_pdf(first_source, "Version one", title="Photothermal Imaging of Gold Nanoparticles")
        create_pdf(second_source, "Version two", title="Photothermal imaging of gold nanoparticles.")
        first = self.storage.import_pdf(first_source, self.database).paper

        held_back = self.storage.import_pdf(second_source, self.database)
        self.assertTrue(held_back.is_similar)
        self.assertFalse(held_back.is_duplicate)
        self.assertEqual(held_back.paper.paper_id, first.paper_id)
        self.assertEqual(self.database.count_papers(), 1)

        allowed = self.storage.import_pdf(second_source, self.database, allow_similar=True)
        self.assertFalse(allowed.is_similar)
        self.assertEqual(self.database.count_papers(), 2)

    def test_collect_pdf_paths_searches_subfolders(self) -> None:
        folder = self.directory / "to import"
        (folder / "nested" / "deeper").mkdir(parents=True)
        (folder / "a.pdf").write_text("")
        (folder / "nested" / "b.PDF").write_text("")
        (folder / "nested" / "deeper" / "c.pdf").write_text("")
        (folder / "nested" / "notes.txt").write_text("")
        loose_file = self.directory / "loose.pdf"
        loose_file.write_text("")
        found = collect_pdf_paths([folder, loose_file, loose_file, self.directory / "x.docx"])
        self.assertEqual(sorted(p.name for p in found), ["a.pdf", "b.PDF", "c.pdf", "loose.pdf"])

    def test_tidy_file_names(self) -> None:
        source = self.directory / "nn2c09212.pdf"
        other_source = self.directory / "unknown.pdf"
        create_pdf(source, "Content one")
        create_pdf(other_source, "Content two")
        paper = self.storage.import_pdf(source, self.database).paper
        other = self.storage.import_pdf(other_source, self.database).paper
        self.database.update_paper_metadata(
            paper.paper_id, title='Imaging: "Hot" Particles?', authors=["Ludwig van Beethoven"],
            publication_year=2023,
        )
        renamed = self.storage.tidy_file_names(self.database)
        self.assertEqual(list(renamed), [paper.paper_id])
        new_path = Path(self.database.get_paper(paper.paper_id).file_path)
        self.assertEqual(new_path.name, "van Beethoven_2023_Imaging Hot Particles.pdf")
        self.assertTrue(new_path.exists())
        self.assertEqual(Path(self.database.get_paper(other.paper_id).file_path).name, "unknown.pdf")
        self.assertEqual(self.storage.tidy_file_names(self.database), {})  # already tidy

    def test_file_names_are_made_safe(self) -> None:
        self.assertEqual(sanitize_file_name('Optics: "A/B" test?'), "Optics A B test")
        self.assertEqual(sanitize_file_name("  ...  "), "untitled")
        self.assertEqual(sanitize_file_name("CON"), "_CON")
        self.assertLessEqual(len(sanitize_file_name("x" * 500)), 120)
        (self.directory / "name.pdf").write_text("")
        self.assertEqual(unique_file_path(self.directory, "name").name, "name (2).pdf")


if __name__ == "__main__":
    unittest.main()
