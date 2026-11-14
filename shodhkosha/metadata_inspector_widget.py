"""Form for viewing and editing one reference's details, folder and tags."""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QIntValidator
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from shodhkosha.doi_lookup import BibliographicRecord, DoiLookupError, normalize_doi
from shodhkosha.doi_lookup_worker import DoiLookupWorker
from shodhkosha.library_database import LibraryDatabase, Paper

_LOOKUP_BUTTON_TEXT = "Look Up DOI Online"
_LOOKUP_WAIT_MILLISECONDS = 11000


def _split_list(text: str, separator: str) -> list[str]:
    return [" ".join(part.split()) for part in text.split(separator) if part.strip()]


class MetadataInspectorWidget(QWidget):
    """Edits the selected paper. Emits ``paper_saved`` after writing to the database."""

    paper_saved = pyqtSignal(int)
    status_message = pyqtSignal(str)

    def __init__(self, database: LibraryDatabase, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("inspectorPanel")
        self._database = database
        self._paper: Optional[Paper] = None
        self._lookup_worker: Optional[DoiLookupWorker] = None
        self._build_interface()
        self.set_paper(None)

    def _build_interface(self) -> None:
        self._title_field = QLineEdit(self)
        self._authors_field = QLineEdit(self)
        self._authors_field.setPlaceholderText("Separate authors with semicolons")
        self._year_field = QLineEdit(self)
        self._year_field.setValidator(QIntValidator(1000, 2999, self))
        self._journal_field = QLineEdit(self)
        self._volume_field = QLineEdit(self)
        self._issue_field = QLineEdit(self)
        self._pages_field = QLineEdit(self)
        self._doi_field = QLineEdit(self)
        self._publisher_field = QLineEdit(self)
        self._folder_field = QComboBox(self)
        self._folder_field.setEditable(True)
        self._folder_field.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self._folder_field.lineEdit().setPlaceholderText("Unfiled")
        self._tags_field = QLineEdit(self)
        self._tags_field.setPlaceholderText("Separate tags with commas")
        self._summary_field = QPlainTextEdit(self)
        self._summary_field.setFixedHeight(84)
        self._summary_field.setTabChangesFocus(True)

        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)

        def add_wide_row(row: int, label_text: str, field: QWidget) -> None:
            grid.addWidget(self._create_label(label_text), row, 0)
            grid.addWidget(field, row, 1, 1, 3)

        def add_pair_row(
            row: int, left_label: str, left_field: QWidget, right_label: str, right_field: QWidget
        ) -> None:
            grid.addWidget(self._create_label(left_label), row, 0)
            grid.addWidget(left_field, row, 1)
            grid.addWidget(self._create_label(right_label), row, 2)
            grid.addWidget(right_field, row, 3)

        add_wide_row(0, "Title", self._title_field)
        add_wide_row(1, "Authors", self._authors_field)
        add_pair_row(2, "Year", self._year_field, "Journal", self._journal_field)
        add_pair_row(3, "Volume", self._volume_field, "Issue", self._issue_field)
        add_pair_row(4, "Pages", self._pages_field, "DOI", self._doi_field)
        add_pair_row(5, "Publisher", self._publisher_field, "Folder", self._folder_field)
        add_wide_row(6, "Tags", self._tags_field)
        grid.addWidget(self._create_label("Summary"), 7, 0, Qt.AlignmentFlag.AlignTop)
        grid.addWidget(self._summary_field, 7, 1, 1, 3)

        self._verified_checkbox = QCheckBox("Details verified", self)
        self._verified_checkbox.setToolTip(
            "Tick once you have checked the title, authors and year against the paper."
        )
        self._save_button = QPushButton("Save Details", self)
        self._save_button.setObjectName("primaryButton")
        self._save_button.clicked.connect(self.save_details)

        self._lookup_button = QPushButton(_LOOKUP_BUTTON_TEXT, self)
        self._lookup_button.setToolTip(
            "Fetch the title, authors, journal and pages for this DOI from Crossref.\n"
            "Uses the internet; only the DOI is sent."
        )
        self._lookup_button.clicked.connect(self.look_up_doi_online)

        footer = QHBoxLayout()
        footer.addWidget(self._verified_checkbox)
        footer.addStretch(1)
        footer.addWidget(self._lookup_button)
        footer.addWidget(self._save_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(10)
        layout.addLayout(grid)
        layout.addLayout(footer)
        layout.addStretch(1)

        self._title_field.returnPressed.connect(self.save_details)
        self._tags_field.returnPressed.connect(self.save_details)

    def _create_label(self, text: str) -> QLabel:
        label = QLabel(text, self)
        label.setObjectName("fieldLabel")
        return label

    def set_paper(self, paper: Optional[Paper]) -> None:
        """Show a paper's details, or clear and disable the form for ``None``."""
        self._paper = paper
        self.setEnabled(paper is not None)

        self._folder_field.clear()
        self._folder_field.addItem("")
        for folder in self._database.list_folders():
            self._folder_field.addItem(folder.name)

        self._title_field.setText(paper.title if paper else "")
        self._authors_field.setText("; ".join(paper.authors) if paper else "")
        year = paper.publication_year if paper else None
        self._year_field.setText(str(year) if year else "")
        self._journal_field.setText(paper.journal if paper else "")
        self._volume_field.setText(paper.volume if paper else "")
        self._issue_field.setText(paper.issue if paper else "")
        self._pages_field.setText(paper.pages if paper else "")
        self._doi_field.setText(paper.doi if paper else "")
        self._publisher_field.setText(paper.publisher if paper else "")
        self._folder_field.setCurrentText((paper.folder_name or "") if paper else "")
        self._tags_field.setText(", ".join(paper.tags) if paper else "")
        self._summary_field.setPlainText(paper.summary if paper else "")
        self._verified_checkbox.setChecked(bool(paper and paper.metadata_verified))
        self._title_field.setCursorPosition(0)
        self._update_lookup_button()

    # --------------------------------------------------------------- DOI lookup

    def _update_lookup_button(self) -> None:
        is_running = self._lookup_worker is not None
        self._lookup_button.setEnabled(not is_running)
        self._lookup_button.setText("Looking up\u2026" if is_running else _LOOKUP_BUTTON_TEXT)

    def look_up_doi_online(self) -> None:
        """Fetch details for the DOI in the form and fill in the other fields."""
        if self._paper is None or self._lookup_worker is not None:
            return
        try:
            doi = normalize_doi(self._doi_field.text())
        except DoiLookupError as error:
            self.status_message.emit(str(error))
            self._doi_field.setFocus()
            return
        self._lookup_worker = DoiLookupWorker(self._paper.paper_id, doi, self)
        self._lookup_worker.lookup_succeeded.connect(self._on_lookup_succeeded)
        self._lookup_worker.lookup_failed.connect(self._on_lookup_failed)
        self._lookup_worker.finished.connect(self._on_lookup_finished)
        self._update_lookup_button()
        self.status_message.emit(f"Looking up {doi} on Crossref\u2026")
        self._lookup_worker.start()

    def _on_lookup_succeeded(self, paper_id: int, record: BibliographicRecord) -> None:
        if self._paper is None or self._paper.paper_id != paper_id:
            self.status_message.emit("DOI lookup finished, but a different reference is open now.")
            return
        # Only fields Crossref actually returned replace what is in the form.
        for field_widget, value in (
            (self._title_field, record.title),
            (self._authors_field, "; ".join(record.authors)),
            (self._year_field, str(record.publication_year or "")),
            (self._journal_field, record.journal),
            (self._volume_field, record.volume),
            (self._issue_field, record.issue),
            (self._pages_field, record.pages),
            (self._publisher_field, record.publisher),
            (self._doi_field, record.doi),
        ):
            if value:
                field_widget.setText(value)
                field_widget.setCursorPosition(0)
        self.status_message.emit(
            "Details filled in from Crossref. Check them, then click Save Details."
        )

    def _on_lookup_failed(self, _paper_id: int, message: str) -> None:
        self.status_message.emit(message)

    def _on_lookup_finished(self) -> None:
        if self._lookup_worker is not None:
            self._lookup_worker.deleteLater()
            self._lookup_worker = None
        self._update_lookup_button()

    def finish_background_work(self) -> None:
        """Call before the window closes so no lookup thread is left running."""
        if self._lookup_worker is not None and self._lookup_worker.isRunning():
            self._lookup_worker.wait(_LOOKUP_WAIT_MILLISECONDS)

    # ------------------------------------------------------------------- saving

    def save_details(self) -> None:
        if self._paper is None:
            return
        paper_id = self._paper.paper_id
        year_text = self._year_field.text().strip()
        folder_name = " ".join(self._folder_field.currentText().split())

        with self._database.transaction():
            self._database.update_paper_metadata(
                paper_id,
                title=self._title_field.text().strip(),
                authors=_split_list(self._authors_field.text(), ";"),
                publication_year=int(year_text) if year_text else None,
                journal=self._journal_field.text().strip(),
                volume=self._volume_field.text().strip(),
                issue=self._issue_field.text().strip(),
                pages=self._pages_field.text().strip(),
                doi=self._doi_field.text().strip(),
                publisher=self._publisher_field.text().strip(),
                summary=self._summary_field.toPlainText().strip(),
                metadata_verified=self._verified_checkbox.isChecked(),
            )
            folder_id = (
                self._database.get_or_create_folder(folder_name).folder_id if folder_name else None
            )
            self._database.set_paper_folder(paper_id, folder_id)
            self._database.set_paper_tags(paper_id, _split_list(self._tags_field.text(), ","))
        self.paper_saved.emit(paper_id)
