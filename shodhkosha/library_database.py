"""SQLite persistence layer for the ShodhKosha reference library.

This module owns the database schema and every read/write operation on it.
No other module should execute SQL directly.

Threading: a ``LibraryDatabase`` instance must only be used from the thread
that created it (the GUI thread). Background workers return their results
through Qt signals and the GUI thread performs the writes.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Optional, Sequence, Union

SCHEMA_VERSION = 2

ANNOTATION_TYPES = ("highlight", "note", "text")

_EDITABLE_PAPER_FIELDS = frozenset(
    {
        "title",
        "authors",
        "publication_year",
        "journal",
        "volume",
        "issue",
        "pages",
        "publisher",
        "doi",
        "summary",
        "metadata_verified",
    }
)

_SCHEMA_SQL = """
CREATE TABLE folders (
    folder_id   INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE COLLATE NOCASE,
    created_at  TEXT NOT NULL
);

CREATE TABLE papers (
    paper_id           INTEGER PRIMARY KEY,
    title              TEXT NOT NULL DEFAULT '',
    authors_json       TEXT NOT NULL DEFAULT '[]',
    publication_year   INTEGER,
    journal            TEXT NOT NULL DEFAULT '',
    volume             TEXT NOT NULL DEFAULT '',
    issue              TEXT NOT NULL DEFAULT '',
    pages              TEXT NOT NULL DEFAULT '',
    publisher          TEXT NOT NULL DEFAULT '',
    doi                TEXT NOT NULL DEFAULT '',
    summary            TEXT NOT NULL DEFAULT '',
    file_path          TEXT NOT NULL UNIQUE,
    file_hash          TEXT NOT NULL UNIQUE,
    folder_id          INTEGER REFERENCES folders(folder_id) ON DELETE SET NULL,
    metadata_verified  INTEGER NOT NULL DEFAULT 0,
    date_added         TEXT NOT NULL,
    date_modified      TEXT NOT NULL
);
CREATE INDEX index_papers_folder ON papers(folder_id);

CREATE TABLE tags (
    tag_id  INTEGER PRIMARY KEY,
    name    TEXT NOT NULL UNIQUE COLLATE NOCASE
);

CREATE TABLE paper_tags (
    paper_id  INTEGER NOT NULL REFERENCES papers(paper_id) ON DELETE CASCADE,
    tag_id    INTEGER NOT NULL REFERENCES tags(tag_id) ON DELETE CASCADE,
    PRIMARY KEY (paper_id, tag_id)
) WITHOUT ROWID;
CREATE INDEX index_paper_tags_tag ON paper_tags(tag_id);

CREATE TABLE annotations (
    annotation_id    INTEGER PRIMARY KEY,
    paper_id         INTEGER NOT NULL REFERENCES papers(paper_id) ON DELETE CASCADE,
    page_number      INTEGER NOT NULL,
    annotation_type  TEXT NOT NULL
                     CHECK (annotation_type IN ('highlight', 'note', 'text')),
    color            TEXT NOT NULL DEFAULT '',
    selected_text    TEXT NOT NULL DEFAULT '',
    note_text        TEXT NOT NULL DEFAULT '',
    created_at       TEXT NOT NULL
);
CREATE INDEX index_annotations_paper ON annotations(paper_id);

CREATE TABLE settings (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
) WITHOUT ROWID;

-- One row per paper (rowid = paper_id) holding everything the search bar matches.
CREATE VIRTUAL TABLE paper_search USING fts5(
    title, authors, publication_year, folder, tags, summary, annotations,
    tokenize = 'unicode61 remove_diacritics 2'
);
"""


# Version 2: text of every page (for full-text search) and study cards.
_SCHEMA_V2_SQL = """
ALTER TABLE papers ADD COLUMN text_indexed INTEGER NOT NULL DEFAULT 0;

CREATE VIRTUAL TABLE page_search USING fts5(
    page_text, paper_id UNINDEXED, page_number UNINDEXED, is_ocr UNINDEXED,
    tokenize = 'unicode61 remove_diacritics 2'
);

CREATE TABLE study_cards (
    card_id      INTEGER PRIMARY KEY,
    paper_id     INTEGER NOT NULL REFERENCES papers(paper_id) ON DELETE CASCADE,
    page_number  INTEGER NOT NULL,
    question     TEXT NOT NULL,
    answer       TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE INDEX index_study_cards_paper ON study_cards(paper_id);
"""

# Applied in order; the key is the schema version each script upgrades from.
_MIGRATIONS = {0: _SCHEMA_SQL, 1: _SCHEMA_V2_SQL}
_MINIMUM_TITLE_KEY_LENGTH = 20


class LibraryDatabaseError(Exception):
    """Base class for errors raised by this module."""


class DuplicateNameError(LibraryDatabaseError):
    """A folder or tag with the requested name already exists."""


class DuplicatePaperError(LibraryDatabaseError):
    """The PDF being added is already in the library."""

    def __init__(self, existing_paper_id: int) -> None:
        super().__init__(f"Paper already exists in the library (id {existing_paper_id}).")
        self.existing_paper_id = existing_paper_id


class RecordNotFoundError(LibraryDatabaseError):
    """The requested record does not exist."""


@dataclass(frozen=True)
class Folder:
    folder_id: int
    name: str
    paper_count: int = 0


@dataclass(frozen=True)
class Tag:
    tag_id: int
    name: str
    paper_count: int = 0


@dataclass(frozen=True)
class Paper:
    paper_id: int
    title: str
    authors: list[str]
    publication_year: Optional[int]
    journal: str
    volume: str
    issue: str
    pages: str
    publisher: str
    doi: str
    summary: str
    file_path: str
    file_hash: str
    folder_id: Optional[int]
    folder_name: Optional[str]
    tags: list[str]
    metadata_verified: bool
    date_added: str
    date_modified: str


@dataclass(frozen=True)
class AnnotationDraft:
    """An annotation read from a PDF, not yet stored."""

    page_number: int
    annotation_type: str
    color: str = ""
    selected_text: str = ""
    note_text: str = ""


@dataclass(frozen=True)
class Annotation:
    annotation_id: int
    paper_id: int
    page_number: int
    annotation_type: str
    color: str
    selected_text: str
    note_text: str
    created_at: str


@dataclass(frozen=True)
class PageText:
    """The text of one page, either from the PDF or read from a scan by OCR."""

    page_number: int
    text: str
    is_ocr: bool = False


@dataclass(frozen=True)
class PageHit:
    """One page of a paper that matches a search, with the matching excerpt."""

    page_number: int
    excerpt: str
    is_ocr: bool


@dataclass(frozen=True)
class StudyCard:
    card_id: int
    paper_id: int
    page_number: int
    question: str
    answer: str


@dataclass(frozen=True)
class PaperFilter:
    """Criteria for :meth:`LibraryDatabase.search_papers`. All are optional."""

    search_text: str = ""
    folder_id: Optional[int] = None
    unfiled_only: bool = False
    tag_id: Optional[int] = None


def _current_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _normalize_name(name: str) -> str:
    """Trim and collapse whitespace; reject empty names."""
    normalized = " ".join(str(name).split())
    if not normalized:
        raise ValueError("Name must not be empty.")
    return normalized


def _title_key(title: str) -> str:
    """Comparison key for titles that ignores case, spacing and punctuation."""
    return "".join(character for character in title.casefold() if character.isalnum())


def _build_match_expression(search_text: str) -> str:
    """Turn free text into an FTS5 query: every word must match as a prefix."""
    terms = []
    for word in search_text.split():
        cleaned = word.replace('"', "").strip()
        if cleaned:
            terms.append(f'"{cleaned}"*')
    return " ".join(terms)


class LibraryDatabase:
    """Reference library stored in a single SQLite file."""

    def __init__(self, database_path: Union[str, Path]) -> None:
        self.database_path = str(database_path)
        if self.database_path != ":memory:":
            Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None: transactions are controlled explicitly below.
        self._connection = sqlite3.connect(self.database_path, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._transaction_depth = 0
        self._initialize_schema()

    # ------------------------------------------------------------------ lifecycle

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "LibraryDatabase":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _initialize_schema(self) -> None:
        version = self._connection.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise LibraryDatabaseError(
                f"Database schema version {version} is newer than this application "
                f"supports ({SCHEMA_VERSION})."
            )
        # A library made by an older version is upgraded one step at a time.
        while version < SCHEMA_VERSION:
            self._connection.executescript(
                f"BEGIN;\n{_MIGRATIONS[version]}\nPRAGMA user_version = {version + 1};\nCOMMIT;"
            )
            version += 1

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Group several operations so they all succeed or all roll back.

        Calls may be nested; only the outermost one commits.
        """
        if self._transaction_depth > 0:
            self._transaction_depth += 1
            try:
                yield
            finally:
                self._transaction_depth -= 1
            return

        self._connection.execute("BEGIN IMMEDIATE")
        self._transaction_depth = 1
        try:
            yield
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise
        else:
            self._connection.execute("COMMIT")
        finally:
            self._transaction_depth = 0

    # -------------------------------------------------------------------- folders

    def create_folder(self, name: str) -> Folder:
        folder_name = _normalize_name(name)
        try:
            with self.transaction():
                cursor = self._connection.execute(
                    "INSERT INTO folders (name, created_at) VALUES (?, ?)",
                    (folder_name, _current_timestamp()),
                )
        except sqlite3.IntegrityError as error:
            raise DuplicateNameError(f"Folder '{folder_name}' already exists.") from error
        return Folder(cursor.lastrowid, folder_name)

    def get_or_create_folder(self, name: str) -> Folder:
        """Return the folder with this name (case-insensitive), creating it if needed."""
        folder_name = _normalize_name(name)
        row = self._connection.execute(
            "SELECT folder_id, name FROM folders WHERE name = ?", (folder_name,)
        ).fetchone()
        if row is not None:
            return Folder(row["folder_id"], row["name"])
        return self.create_folder(folder_name)

    def list_folders(self) -> list[Folder]:
        rows = self._connection.execute(
            """
            SELECT f.folder_id, f.name, COUNT(p.paper_id) AS paper_count
            FROM folders f LEFT JOIN papers p ON p.folder_id = f.folder_id
            GROUP BY f.folder_id
            ORDER BY f.name COLLATE NOCASE
            """
        ).fetchall()
        return [Folder(r["folder_id"], r["name"], r["paper_count"]) for r in rows]

    def rename_folder(self, folder_id: int, new_name: str) -> None:
        folder_name = _normalize_name(new_name)
        try:
            with self.transaction():
                cursor = self._connection.execute(
                    "UPDATE folders SET name = ? WHERE folder_id = ?",
                    (folder_name, folder_id),
                )
                if cursor.rowcount == 0:
                    raise RecordNotFoundError(f"Folder {folder_id} does not exist.")
                self._refresh_search_index(self._paper_ids_in_folder(folder_id))
        except sqlite3.IntegrityError as error:
            raise DuplicateNameError(f"Folder '{folder_name}' already exists.") from error

    def delete_folder(self, folder_id: int) -> None:
        """Delete a folder. Its papers stay in the library as unfiled."""
        with self.transaction():
            affected_paper_ids = self._paper_ids_in_folder(folder_id)
            self._connection.execute("DELETE FROM folders WHERE folder_id = ?", (folder_id,))
            self._refresh_search_index(affected_paper_ids)

    def _paper_ids_in_folder(self, folder_id: int) -> list[int]:
        rows = self._connection.execute(
            "SELECT paper_id FROM papers WHERE folder_id = ?", (folder_id,)
        ).fetchall()
        return [r["paper_id"] for r in rows]

    # ----------------------------------------------------------------------- tags

    def get_or_create_tag(self, name: str) -> Tag:
        tag_name = _normalize_name(name)
        row = self._connection.execute(
            "SELECT tag_id, name FROM tags WHERE name = ?", (tag_name,)
        ).fetchone()
        if row is not None:
            return Tag(row["tag_id"], row["name"])
        with self.transaction():
            cursor = self._connection.execute("INSERT INTO tags (name) VALUES (?)", (tag_name,))
        return Tag(cursor.lastrowid, tag_name)

    def list_tags(self) -> list[Tag]:
        rows = self._connection.execute(
            """
            SELECT t.tag_id, t.name, COUNT(pt.paper_id) AS paper_count
            FROM tags t LEFT JOIN paper_tags pt ON pt.tag_id = t.tag_id
            GROUP BY t.tag_id
            ORDER BY t.name COLLATE NOCASE
            """
        ).fetchall()
        return [Tag(r["tag_id"], r["name"], r["paper_count"]) for r in rows]

    def rename_tag(self, tag_id: int, new_name: str) -> None:
        tag_name = _normalize_name(new_name)
        try:
            with self.transaction():
                cursor = self._connection.execute(
                    "UPDATE tags SET name = ? WHERE tag_id = ?", (tag_name, tag_id)
                )
                if cursor.rowcount == 0:
                    raise RecordNotFoundError(f"Tag {tag_id} does not exist.")
                self._refresh_search_index(self._paper_ids_with_tag(tag_id))
        except sqlite3.IntegrityError as error:
            raise DuplicateNameError(f"Tag '{tag_name}' already exists.") from error

    def delete_tag(self, tag_id: int) -> None:
        with self.transaction():
            affected_paper_ids = self._paper_ids_with_tag(tag_id)
            self._connection.execute("DELETE FROM tags WHERE tag_id = ?", (tag_id,))
            self._refresh_search_index(affected_paper_ids)

    def delete_unused_tags(self) -> int:
        """Remove tags attached to no paper. Returns how many were removed."""
        with self.transaction():
            cursor = self._connection.execute(
                "DELETE FROM tags WHERE tag_id NOT IN (SELECT tag_id FROM paper_tags)"
            )
        return cursor.rowcount

    def _paper_ids_with_tag(self, tag_id: int) -> list[int]:
        rows = self._connection.execute(
            "SELECT paper_id FROM paper_tags WHERE tag_id = ?", (tag_id,)
        ).fetchall()
        return [r["paper_id"] for r in rows]

    # --------------------------------------------------------------------- papers

    def add_paper(
        self,
        file_path: Union[str, Path],
        file_hash: str,
        *,
        title: str = "",
        authors: Sequence[str] = (),
        publication_year: Optional[int] = None,
        journal: str = "",
        volume: str = "",
        issue: str = "",
        pages: str = "",
        publisher: str = "",
        doi: str = "",
        summary: str = "",
        folder_id: Optional[int] = None,
        tag_names: Sequence[str] = (),
        metadata_verified: bool = False,
    ) -> Paper:
        """Add a PDF to the library. Raises DuplicatePaperError if already present."""
        existing = self.find_paper_by_hash(file_hash)
        if existing is not None:
            raise DuplicatePaperError(existing.paper_id)

        timestamp = _current_timestamp()
        with self.transaction():
            cursor = self._connection.execute(
                """
                INSERT INTO papers (
                    title, authors_json, publication_year, journal, volume, issue,
                    pages, publisher, doi, summary, file_path, file_hash, folder_id,
                    metadata_verified, date_added, date_modified
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    title.strip(),
                    json.dumps(list(authors), ensure_ascii=False),
                    publication_year,
                    journal,
                    volume,
                    issue,
                    pages,
                    publisher,
                    doi,
                    summary,
                    str(file_path),
                    file_hash,
                    folder_id,
                    int(metadata_verified),
                    timestamp,
                    timestamp,
                ),
            )
            paper_id = cursor.lastrowid
            self._replace_paper_tags(paper_id, tag_names)
            self._refresh_search_index([paper_id])
        return self.get_paper(paper_id)

    def get_paper(self, paper_id: int) -> Paper:
        papers = self._select_papers("p.paper_id = ?", [paper_id])
        if not papers:
            raise RecordNotFoundError(f"Paper {paper_id} does not exist.")
        return papers[0]

    def find_paper_by_hash(self, file_hash: str) -> Optional[Paper]:
        papers = self._select_papers("p.file_hash = ?", [file_hash])
        return papers[0] if papers else None

    def count_papers(self) -> int:
        return self._connection.execute("SELECT COUNT(*) FROM papers").fetchone()[0]

    def count_unfiled_papers(self) -> int:
        return self._connection.execute(
            "SELECT COUNT(*) FROM papers WHERE folder_id IS NULL"
        ).fetchone()[0]

    def search_papers(self, paper_filter: Optional[PaperFilter] = None) -> list[Paper]:
        """Return papers matching the filter, newest first.

        ``search_text`` matches title, authors, year, folder, tags, summary and
        the text of saved highlights and notes, or the text of any single page
        of the PDF. Every word must match; partial words match from their
        beginning, so results update as the user types.
        """
        paper_filter = paper_filter or PaperFilter()
        conditions: list[str] = []
        parameters: list[object] = []

        match_expression = _build_match_expression(paper_filter.search_text)
        if match_expression:
            conditions.append(
                "(p.paper_id IN (SELECT rowid FROM paper_search WHERE paper_search MATCH ?)"
                " OR p.paper_id IN (SELECT paper_id FROM page_search WHERE page_search MATCH ?))"
            )
            parameters.extend([match_expression, match_expression])
        if paper_filter.unfiled_only:
            conditions.append("p.folder_id IS NULL")
        elif paper_filter.folder_id is not None:
            conditions.append("p.folder_id = ?")
            parameters.append(paper_filter.folder_id)
        if paper_filter.tag_id is not None:
            conditions.append(
                "EXISTS (SELECT 1 FROM paper_tags pt "
                "WHERE pt.paper_id = p.paper_id AND pt.tag_id = ?)"
            )
            parameters.append(paper_filter.tag_id)

        return self._select_papers(" AND ".join(conditions) or "1 = 1", parameters)

    def update_paper_metadata(self, paper_id: int, **changes: object) -> Paper:
        """Update bibliographic fields, e.g. ``update_paper_metadata(3, title="...")``."""
        unknown_fields = set(changes) - _EDITABLE_PAPER_FIELDS
        if unknown_fields:
            raise ValueError(f"Fields cannot be edited: {sorted(unknown_fields)}")
        if not changes:
            return self.get_paper(paper_id)

        assignments: list[str] = []
        parameters: list[object] = []
        for field_name, value in changes.items():
            if field_name == "authors":
                assignments.append("authors_json = ?")
                parameters.append(json.dumps(list(value), ensure_ascii=False))  # type: ignore[arg-type]
            elif field_name == "metadata_verified":
                assignments.append("metadata_verified = ?")
                parameters.append(int(bool(value)))
            else:
                assignments.append(f"{field_name} = ?")
                parameters.append(value)
        assignments.append("date_modified = ?")
        parameters.extend([_current_timestamp(), paper_id])

        with self.transaction():
            cursor = self._connection.execute(
                f"UPDATE papers SET {', '.join(assignments)} WHERE paper_id = ?", parameters
            )
            if cursor.rowcount == 0:
                raise RecordNotFoundError(f"Paper {paper_id} does not exist.")
            self._refresh_search_index([paper_id])
        return self.get_paper(paper_id)

    def set_paper_folder(self, paper_id: int, folder_id: Optional[int]) -> None:
        """Move a paper to a folder, or pass ``None`` to make it unfiled."""
        with self.transaction():
            cursor = self._connection.execute(
                "UPDATE papers SET folder_id = ?, date_modified = ? WHERE paper_id = ?",
                (folder_id, _current_timestamp(), paper_id),
            )
            if cursor.rowcount == 0:
                raise RecordNotFoundError(f"Paper {paper_id} does not exist.")
            self._refresh_search_index([paper_id])

    def set_paper_tags(self, paper_id: int, tag_names: Sequence[str]) -> None:
        """Replace all tags on a paper with the given names."""
        with self.transaction():
            self._replace_paper_tags(paper_id, tag_names)
            self._connection.execute(
                "UPDATE papers SET date_modified = ? WHERE paper_id = ?",
                (_current_timestamp(), paper_id),
            )
            self._refresh_search_index([paper_id])

    def update_paper_file(
        self, paper_id: int, file_path: Union[str, Path], file_hash: Optional[str] = None
    ) -> None:
        """Record a new location (and optionally new content hash) for a paper's PDF."""
        with self.transaction():
            if file_hash is None:
                cursor = self._connection.execute(
                    "UPDATE papers SET file_path = ?, date_modified = ? WHERE paper_id = ?",
                    (str(file_path), _current_timestamp(), paper_id),
                )
            else:
                cursor = self._connection.execute(
                    "UPDATE papers SET file_path = ?, file_hash = ?, date_modified = ? "
                    "WHERE paper_id = ?",
                    (str(file_path), file_hash, _current_timestamp(), paper_id),
                )
            if cursor.rowcount == 0:
                raise RecordNotFoundError(f"Paper {paper_id} does not exist.")

    def delete_paper(self, paper_id: int) -> None:
        """Remove a paper's record, tags and annotations. The PDF file is not touched."""
        with self.transaction():
            self._connection.execute("DELETE FROM papers WHERE paper_id = ?", (paper_id,))
            self._connection.execute("DELETE FROM paper_search WHERE rowid = ?", (paper_id,))
            self._connection.execute("DELETE FROM page_search WHERE paper_id = ?", (paper_id,))

    def _replace_paper_tags(self, paper_id: int, tag_names: Iterable[str]) -> None:
        self._connection.execute("DELETE FROM paper_tags WHERE paper_id = ?", (paper_id,))
        for tag_name in tag_names:
            if not str(tag_name).strip():
                continue
            tag = self.get_or_create_tag(tag_name)
            self._connection.execute(
                "INSERT OR IGNORE INTO paper_tags (paper_id, tag_id) VALUES (?, ?)",
                (paper_id, tag.tag_id),
            )

    def _select_papers(self, where_clause: str, parameters: Sequence[object]) -> list[Paper]:
        rows = self._connection.execute(
            f"""
            SELECT p.*, f.name AS folder_name
            FROM papers p LEFT JOIN folders f ON f.folder_id = p.folder_id
            WHERE {where_clause}
            ORDER BY p.date_added DESC, p.paper_id DESC
            """,
            list(parameters),
        ).fetchall()
        if not rows:
            return []

        tags_by_paper: dict[int, list[str]] = {}
        for tag_row in self._connection.execute(
            """
            SELECT pt.paper_id, t.name
            FROM paper_tags pt JOIN tags t ON t.tag_id = pt.tag_id
            ORDER BY t.name COLLATE NOCASE
            """
        ):
            tags_by_paper.setdefault(tag_row["paper_id"], []).append(tag_row["name"])

        return [
            Paper(
                paper_id=row["paper_id"],
                title=row["title"],
                authors=json.loads(row["authors_json"]),
                publication_year=row["publication_year"],
                journal=row["journal"],
                volume=row["volume"],
                issue=row["issue"],
                pages=row["pages"],
                publisher=row["publisher"],
                doi=row["doi"],
                summary=row["summary"],
                file_path=row["file_path"],
                file_hash=row["file_hash"],
                folder_id=row["folder_id"],
                folder_name=row["folder_name"],
                tags=tags_by_paper.get(row["paper_id"], []),
                metadata_verified=bool(row["metadata_verified"]),
                date_added=row["date_added"],
                date_modified=row["date_modified"],
            )
            for row in rows
        ]

    def find_similar_paper(self, doi: str = "", title: str = "") -> Optional[Paper]:
        """A paper already in the library with the same DOI, or the same title
        apart from case and punctuation. Used to catch a second download of a
        paper whose file differs byte for byte."""
        doi_key = doi.strip().casefold()
        if doi_key:
            row = self._connection.execute(
                "SELECT paper_id FROM papers WHERE lower(doi) = ? LIMIT 1", (doi_key,)
            ).fetchone()
            if row is not None:
                return self.get_paper(row["paper_id"])
        title_key = _title_key(title)
        if len(title_key) >= _MINIMUM_TITLE_KEY_LENGTH:
            for row in self._connection.execute("SELECT paper_id, title FROM papers"):
                if _title_key(row["title"]) == title_key:
                    return self.get_paper(row["paper_id"])
        return None

    # ------------------------------------------------------------------ page text

    def replace_page_texts(self, paper_id: int, pages: Sequence[PageText]) -> None:
        """Store the text of every page of a paper and mark it as indexed."""
        with self.transaction():
            self._connection.execute("DELETE FROM page_search WHERE paper_id = ?", (paper_id,))
            self._connection.executemany(
                "INSERT INTO page_search (page_text, paper_id, page_number, is_ocr) "
                "VALUES (?, ?, ?, ?)",
                [(page.text, paper_id, page.page_number, int(page.is_ocr)) for page in pages],
            )
            self._connection.execute(
                "UPDATE papers SET text_indexed = 1 WHERE paper_id = ?", (paper_id,)
            )

    def set_page_text(self, paper_id: int, page: PageText) -> None:
        """Replace the stored text of one page, for example with OCR output."""
        with self.transaction():
            self._connection.execute(
                "DELETE FROM page_search WHERE paper_id = ? AND page_number = ?",
                (paper_id, page.page_number),
            )
            self._connection.execute(
                "INSERT INTO page_search (page_text, paper_id, page_number, is_ocr) "
                "VALUES (?, ?, ?, ?)",
                (page.text, paper_id, page.page_number, int(page.is_ocr)),
            )

    def get_page_texts(
        self, paper_id: int, page_numbers: Optional[Sequence[int]] = None
    ) -> dict[int, str]:
        """Stored text by page number, for all pages or only the ones listed."""
        rows = self._connection.execute(
            "SELECT page_number, page_text FROM page_search WHERE paper_id = ?", (paper_id,)
        ).fetchall()
        wanted = None if page_numbers is None else set(page_numbers)
        return {
            row["page_number"]: row["page_text"]
            for row in rows
            if wanted is None or row["page_number"] in wanted
        }

    def search_pages(self, paper_id: int, search_text: str, limit: int = 200) -> list[PageHit]:
        """Pages of one paper that contain every search word, in page order."""
        match_expression = _build_match_expression(search_text)
        if not match_expression:
            return []
        rows = self._connection.execute(
            """
            SELECT page_number, is_ocr,
                   snippet(page_search, 0, '', '', ' ... ', 18) AS excerpt
            FROM page_search
            WHERE page_search MATCH ? AND paper_id = ?
            ORDER BY page_number
            LIMIT ?
            """,
            (match_expression, paper_id, limit),
        ).fetchall()
        return [
            PageHit(row["page_number"], " ".join(row["excerpt"].split()), bool(row["is_ocr"]))
            for row in rows
        ]

    def mark_text_indexed(self, paper_id: int, is_indexed: bool) -> None:
        with self.transaction():
            self._connection.execute(
                "UPDATE papers SET text_indexed = ? WHERE paper_id = ?",
                (int(is_indexed), paper_id),
            )

    def list_unindexed_paper_ids(self) -> list[int]:
        rows = self._connection.execute(
            "SELECT paper_id FROM papers WHERE text_indexed = 0 ORDER BY paper_id"
        ).fetchall()
        return [row["paper_id"] for row in rows]

    # ---------------------------------------------------------------- study cards

    def add_study_cards(
        self, paper_id: int, cards: Sequence[tuple[int, str, str]]
    ) -> list[StudyCard]:
        """Store cards given as (page_number, question, answer)."""
        timestamp = _current_timestamp()
        card_ids = []
        with self.transaction():
            for page_number, question, answer in cards:
                cursor = self._connection.execute(
                    "INSERT INTO study_cards (paper_id, page_number, question, answer, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (paper_id, page_number, question.strip(), answer.strip(), timestamp),
                )
                card_ids.append(cursor.lastrowid)
        return [card for card in self.list_study_cards(paper_id) if card.card_id in card_ids]

    def list_study_cards(self, paper_id: int) -> list[StudyCard]:
        rows = self._connection.execute(
            "SELECT * FROM study_cards WHERE paper_id = ? ORDER BY page_number, card_id",
            (paper_id,),
        ).fetchall()
        return [
            StudyCard(r["card_id"], r["paper_id"], r["page_number"], r["question"], r["answer"])
            for r in rows
        ]

    def delete_study_card(self, card_id: int) -> None:
        with self.transaction():
            self._connection.execute("DELETE FROM study_cards WHERE card_id = ?", (card_id,))

    # ---------------------------------------------------------------- annotations

    def replace_annotations(self, paper_id: int, drafts: Sequence[AnnotationDraft]) -> None:
        """Make the stored annotations match what is in the saved PDF.

        The PDF is the source of truth: after each save the viewer reads every
        annotation back out of the file and passes the full list here.
        """
        for draft in drafts:
            if draft.annotation_type not in ANNOTATION_TYPES:
                raise ValueError(f"Unknown annotation type: {draft.annotation_type!r}")
        timestamp = _current_timestamp()
        with self.transaction():
            self._connection.execute("DELETE FROM annotations WHERE paper_id = ?", (paper_id,))
            self._connection.executemany(
                """
                INSERT INTO annotations (
                    paper_id, page_number, annotation_type, color,
                    selected_text, note_text, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        paper_id,
                        draft.page_number,
                        draft.annotation_type,
                        draft.color,
                        draft.selected_text,
                        draft.note_text,
                        timestamp,
                    )
                    for draft in drafts
                ],
            )
            self._refresh_search_index([paper_id])

    def list_annotations(self, paper_id: int) -> list[Annotation]:
        rows = self._connection.execute(
            "SELECT * FROM annotations WHERE paper_id = ? ORDER BY page_number, annotation_id",
            (paper_id,),
        ).fetchall()
        return [
            Annotation(
                annotation_id=r["annotation_id"],
                paper_id=r["paper_id"],
                page_number=r["page_number"],
                annotation_type=r["annotation_type"],
                color=r["color"],
                selected_text=r["selected_text"],
                note_text=r["note_text"],
                created_at=r["created_at"],
            )
            for r in rows
        ]

    # ------------------------------------------------------------------- settings

    def get_setting(self, key: str, default: str = "") -> str:
        row = self._connection.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row is not None else default

    def set_setting(self, key: str, value: str) -> None:
        with self.transaction():
            self._connection.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )

    # --------------------------------------------------------------- search index

    def _refresh_search_index(self, paper_ids: Iterable[int]) -> None:
        """Rebuild the search rows for the given papers from current data."""
        for paper_id in paper_ids:
            self._connection.execute("DELETE FROM paper_search WHERE rowid = ?", (paper_id,))
            row = self._connection.execute(
                """
                SELECT p.title, p.authors_json, p.publication_year, p.summary,
                       COALESCE(f.name, '') AS folder_name
                FROM papers p LEFT JOIN folders f ON f.folder_id = p.folder_id
                WHERE p.paper_id = ?
                """,
                (paper_id,),
            ).fetchone()
            if row is None:
                continue
            tag_names = [
                r["name"]
                for r in self._connection.execute(
                    "SELECT t.name FROM paper_tags pt JOIN tags t ON t.tag_id = pt.tag_id "
                    "WHERE pt.paper_id = ?",
                    (paper_id,),
                )
            ]
            annotation_texts = [
                f"{r['selected_text']} {r['note_text']}"
                for r in self._connection.execute(
                    "SELECT selected_text, note_text FROM annotations WHERE paper_id = ?",
                    (paper_id,),
                )
            ]
            self._connection.execute(
                """
                INSERT INTO paper_search (
                    rowid, title, authors, publication_year, folder, tags, summary, annotations
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    paper_id,
                    row["title"],
                    " ".join(json.loads(row["authors_json"])),
                    str(row["publication_year"] or ""),
                    row["folder_name"],
                    " ".join(tag_names),
                    row["summary"],
                    " ".join(annotation_texts),
                ),
            )
