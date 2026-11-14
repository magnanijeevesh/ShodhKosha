"""Where ShodhKosha keeps its files on disk, and how PDFs get into the library.

Layout of a library directory::

    ShodhKosha Library/
        library.sqlite3      the database
        papers/              one managed copy of every imported PDF

Imported PDFs are copied, never moved, so the user's original stays untouched.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Union

from shodhkosha.bibtex_formatter import parse_author_name
from shodhkosha.library_database import LibraryDatabase, Paper
from shodhkosha.pdf_document_service import (
    PdfDocumentError,
    PdfDocumentService,
    compute_file_hash,
)

_FORBIDDEN_CHARACTERS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED_NAMES = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + [f"COM{n}" for n in range(1, 10)]
    + [f"LPT{n}" for n in range(1, 10)]
)
_MAXIMUM_NAME_LENGTH = 120
_MAXIMUM_TITLE_LENGTH_IN_FILE_NAME = 80


class LibraryStorageError(Exception):
    """A file could not be imported or removed."""


@dataclass(frozen=True)
class ImportOutcome:
    """``is_duplicate``: the identical file is already in the library.
    ``is_similar``: a different file of what looks like the same paper (same
    DOI or title) is in the library; nothing was imported and ``paper`` is
    the existing one."""

    paper: Paper
    is_duplicate: bool
    is_similar: bool = False


@dataclass(frozen=True)
class MirrorOutcome:
    """Result of arranging PDFs on disk to match their folders."""

    moved_files: dict[int, Path]  # paper_id -> new location
    failed_file_names: list[str]


def default_library_directory() -> Path:
    return Path.home() / "ShodhKosha Library"


def sanitize_file_name(name: str, fallback: str = "untitled") -> str:
    """Make text safe to use as a file or folder name on Windows, macOS and Linux."""
    cleaned = _FORBIDDEN_CHARACTERS.sub(" ", name)
    cleaned = " ".join(cleaned.split()).strip(". ")
    cleaned = cleaned[:_MAXIMUM_NAME_LENGTH].rstrip(". ")
    if not cleaned:
        return fallback
    if cleaned.split(".")[0].upper() in _WINDOWS_RESERVED_NAMES:
        cleaned = f"_{cleaned}"
    return cleaned


def collect_pdf_paths(paths: Iterable[Union[str, Path]]) -> list[Path]:
    """Expand a mix of files and folders into the PDF files they contain.

    Folders are searched through all their subfolders.
    """
    pdf_paths: list[Path] = []
    seen: set[Path] = set()
    for entry in paths:
        path = Path(entry)
        if path.is_dir():
            candidates = sorted(p for p in path.rglob("*") if p.is_file())
        else:
            candidates = [path]
        for candidate in candidates:
            if candidate.suffix.lower() == ".pdf" and candidate not in seen:
                seen.add(candidate)
                pdf_paths.append(candidate)
    return pdf_paths


def tidy_file_stem(paper: Paper) -> Optional[str]:
    """'Lovelace_2023_Photothermal Imaging of ...' or None if details are missing."""
    if not (paper.authors and paper.publication_year and paper.title.strip()):
        return None
    family_name = parse_author_name(paper.authors[0]).family
    title = " ".join(paper.title.split())[:_MAXIMUM_TITLE_LENGTH_IN_FILE_NAME].strip()
    return sanitize_file_name(f"{family_name}_{paper.publication_year}_{title}")


def unique_file_path(directory: Path, file_stem: str, suffix: str = ".pdf") -> Path:
    """A path in ``directory`` that does not exist yet: name.pdf, name (2).pdf, ..."""
    candidate = directory / f"{file_stem}{suffix}"
    counter = 2
    while candidate.exists():
        candidate = directory / f"{file_stem} ({counter}){suffix}"
        counter += 1
    return candidate


class LibraryStorage:
    """Manages the library directory and the PDF files inside it."""

    def __init__(self, library_directory: Union[str, Path]) -> None:
        self.library_directory = Path(library_directory)
        self.papers_directory = self.library_directory / "papers"
        self.papers_directory.mkdir(parents=True, exist_ok=True)

    @property
    def database_path(self) -> Path:
        return self.library_directory / "library.sqlite3"

    def import_pdf(
        self,
        source_path: Union[str, Path],
        database: LibraryDatabase,
        allow_similar: bool = False,
    ) -> ImportOutcome:
        """Copy a PDF into the library and create its record.

        Details stored inside the PDF (title, authors, year, DOI) are used as
        a starting point and left marked as unverified. Importing a file that
        is already in the library returns the existing paper. A different
        file that looks like a paper already in the library is not imported
        unless ``allow_similar`` is set.
        """
        source = Path(source_path)
        if source.suffix.lower() != ".pdf":
            raise LibraryStorageError(f"'{source.name}' is not a PDF file.")

        reader = PdfDocumentService()
        try:
            reader.open(source)
            embedded = reader.read_embedded_metadata()
        except PdfDocumentError as error:
            raise LibraryStorageError(str(error)) from error
        finally:
            reader.close()

        file_hash = compute_file_hash(source)
        existing_paper = database.find_paper_by_hash(file_hash)
        if existing_paper is not None:
            return ImportOutcome(existing_paper, is_duplicate=True)
        if not allow_similar:
            similar_paper = database.find_similar_paper(embedded.doi, embedded.title)
            if similar_paper is not None:
                return ImportOutcome(similar_paper, is_duplicate=False, is_similar=True)

        destination = unique_file_path(self.papers_directory, sanitize_file_name(source.stem))
        try:
            shutil.copy2(source, destination)
        except OSError as error:
            raise LibraryStorageError(f"Could not copy '{source.name}': {error}") from error

        try:
            paper = database.add_paper(
                destination,
                file_hash,
                title=embedded.title or source.stem,
                authors=embedded.authors,
                publication_year=embedded.publication_year,
                doi=embedded.doi,
            )
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        return ImportOutcome(paper, is_duplicate=False)

    def mirror_folders(self, database: LibraryDatabase) -> MirrorOutcome:
        """Move each PDF into ``papers/<folder name>/`` to match its folder.

        Unfiled papers go directly in ``papers/``. Files that cannot be moved
        (for example because another program has them open) are left where
        they are and reported; they will be moved on a later call.
        """
        moved_files: dict[int, Path] = {}
        failed_file_names: list[str] = []
        for paper in database.search_papers():
            current_path = Path(paper.file_path)
            target_directory = self.papers_directory
            if paper.folder_name:
                target_directory = self.papers_directory / sanitize_file_name(paper.folder_name)
            if current_path.parent == target_directory or not current_path.exists():
                continue
            try:
                target_directory.mkdir(parents=True, exist_ok=True)
                destination = unique_file_path(
                    target_directory, current_path.stem, current_path.suffix
                )
                shutil.move(str(current_path), str(destination))
            except OSError:
                failed_file_names.append(current_path.name)
                continue
            database.update_paper_file(paper.paper_id, destination)
            moved_files[paper.paper_id] = destination

        for subdirectory in self.papers_directory.iterdir():
            if subdirectory.is_dir():
                try:
                    subdirectory.rmdir()  # only succeeds when the folder is empty
                except OSError:
                    pass
        return MirrorOutcome(moved_files, failed_file_names)

    def tidy_file_names(self, database: LibraryDatabase) -> dict[int, Path]:
        """Rename library PDFs to Author_Year_Title.pdf where those details are known.

        Returns the new location of every file that was renamed. Files that
        cannot be renamed right now are left as they are.
        """
        renamed_files: dict[int, Path] = {}
        for paper in database.search_papers():
            current_path = Path(paper.file_path)
            target_stem = tidy_file_stem(paper)
            if target_stem is None or not current_path.exists():
                continue
            if current_path.stem == target_stem or current_path.stem.startswith(f"{target_stem} ("):
                continue
            destination = unique_file_path(current_path.parent, target_stem, current_path.suffix)
            try:
                current_path.rename(destination)
            except OSError:
                continue
            database.update_paper_file(paper.paper_id, destination)
            renamed_files[paper.paper_id] = destination
        return renamed_files

    def remove_paper(self, paper: Paper, database: LibraryDatabase) -> None:
        """Delete a paper's record and its managed PDF copy."""
        file_path = Path(paper.file_path)
        try:
            file_path.unlink(missing_ok=True)
        except OSError as error:
            raise LibraryStorageError(
                f"Could not delete '{file_path.name}'. If it is open in another "
                "program, close it there and try again."
            ) from error
        database.delete_paper(paper.paper_id)
