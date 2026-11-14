"""Panel for making, reviewing and exporting study cards for the open paper."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from PyQt6.QtCore import QObject, Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from shodhkosha.ai_analysis_worker import MODEL_SETTING_KEY
from shodhkosha.library_database import LibraryDatabase, Paper, RecordNotFoundError, StudyCard
from shodhkosha.library_storage import sanitize_file_name
from shodhkosha.paper_analysis import DEFAULT_MODEL_NAME, AiError
from shodhkosha.study_cards import StudyCardRequest, export_anki_text, request_study_cards

_CARD_ROLE = Qt.ItemDataRole.UserRole
_MAXIMUM_PAGES_PER_REQUEST = 10
_GENERATE_BUTTON_TEXT = "Generate Cards"


class StudyCardWorker(QThread):
    """Asks the local model for cards. Emits exactly one of the two signals."""

    cards_ready = pyqtSignal(int, object)  # paper_id, list of (page, question, answer)
    generation_failed = pyqtSignal(int, str)

    def __init__(
        self, paper_id: int, request: StudyCardRequest, parent: Optional[QObject] = None
    ) -> None:
        super().__init__(parent)
        self._paper_id = paper_id
        self._request = request

    def run(self) -> None:
        try:
            cards = request_study_cards(self._request)
        except AiError as error:
            self.generation_failed.emit(self._paper_id, str(error))
        except Exception as error:  # never let a worker thread crash the application
            self.generation_failed.emit(self._paper_id, f"Card generation failed: {error}")
        else:
            self.cards_ready.emit(self._paper_id, cards)


class StudyCardsWidget(QWidget):
    """Study cards of one paper. ``page_text_provider`` returns a page's text."""

    page_requested = pyqtSignal(int)
    status_message = pyqtSignal(str)

    def __init__(
        self,
        database: LibraryDatabase,
        page_text_provider: Callable[[int], str],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("inspectorPanel")
        self._database = database
        self._page_text_provider = page_text_provider
        self._paper: Optional[Paper] = None
        self._worker: Optional[StudyCardWorker] = None
        self._build_interface()
        self.set_paper(None, 0)

    def _build_interface(self) -> None:
        self._first_page_field = QSpinBox(self)
        self._last_page_field = QSpinBox(self)
        for field in (self._first_page_field, self._last_page_field):
            field.setMinimum(1)
            field.setFixedWidth(64)
        self._first_page_field.valueChanged.connect(self._on_first_page_changed)
        self._generate_button = QPushButton(_GENERATE_BUTTON_TEXT, self)
        self._generate_button.setObjectName("primaryButton")
        self._generate_button.setToolTip(
            "The local AI writes question-and-answer cards from these pages "
            f"(up to {_MAXIMUM_PAGES_PER_REQUEST} pages at a time)."
        )
        self._generate_button.clicked.connect(lambda _checked=False: self.generate_cards())

        page_row = QHBoxLayout()
        page_row.addWidget(self._create_label("Pages"))
        page_row.addWidget(self._first_page_field)
        page_row.addWidget(self._create_label("to"))
        page_row.addWidget(self._last_page_field)
        page_row.addStretch(1)
        page_row.addWidget(self._generate_button)

        self._list = QListWidget(self)
        self._list.setWordWrap(True)
        self._list.setAlternatingRowColors(True)
        self._list.currentItemChanged.connect(self._on_card_selected)
        self._list.itemDoubleClicked.connect(self._on_card_double_clicked)

        self._answer_view = QPlainTextEdit(self)
        self._answer_view.setReadOnly(True)
        self._answer_view.setFixedHeight(76)
        self._answer_view.setPlaceholderText("Select a card to see its answer.")

        self._delete_button = QPushButton("Delete Card", self)
        self._delete_button.clicked.connect(lambda _checked=False: self._delete_selected_card())
        self._export_button = QPushButton("Export for Anki…", self)
        self._export_button.clicked.connect(lambda _checked=False: self._export_cards())
        button_row = QHBoxLayout()
        button_row.addStretch(1)
        button_row.addWidget(self._delete_button)
        button_row.addWidget(self._export_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)
        layout.addLayout(page_row)
        layout.addWidget(self._list, 1)
        layout.addWidget(self._answer_view)
        layout.addLayout(button_row)

    def _create_label(self, text: str) -> QLabel:
        label = QLabel(text, self)
        label.setObjectName("fieldLabel")
        return label

    # -------------------------------------------------------------------- state

    def set_paper(self, paper: Optional[Paper], page_count: int) -> None:
        """Show the cards of a paper; ``page_count`` is 0 when no PDF is open."""
        self._paper = paper
        self.setEnabled(paper is not None and page_count > 0)
        for field in (self._first_page_field, self._last_page_field):
            field.blockSignals(True)
            field.setMaximum(max(page_count, 1))
            field.setValue(1)
            field.blockSignals(False)
        self._reload_cards()
        self._update_generate_button()

    def set_current_page(self, page_number: int) -> None:
        """Follow the viewer, so the default is to make cards from the page in view."""
        if self._worker is None and self.isEnabled():
            self._first_page_field.setValue(page_number + 1)
            self._last_page_field.setValue(page_number + 1)

    def _on_first_page_changed(self, first_page: int) -> None:
        if self._last_page_field.value() < first_page:
            self._last_page_field.setValue(first_page)

    def _reload_cards(self) -> None:
        self._list.clear()
        self._answer_view.clear()
        cards = self._database.list_study_cards(self._paper.paper_id) if self._paper else []
        for card in cards:
            item = QListWidgetItem(f"p. {card.page_number + 1} · {card.question}")
            item.setData(_CARD_ROLE, card)
            self._list.addItem(item)
        self._delete_button.setEnabled(False)
        self._export_button.setEnabled(bool(cards))

    @property
    def card_count(self) -> int:
        return self._list.count()

    def _selected_card(self) -> Optional[StudyCard]:
        item = self._list.currentItem()
        return item.data(_CARD_ROLE) if item is not None else None

    def _on_card_selected(self, *_: object) -> None:
        card = self._selected_card()
        self._answer_view.setPlainText(card.answer if card else "")
        self._delete_button.setEnabled(card is not None)

    def _on_card_double_clicked(self, item: QListWidgetItem) -> None:
        self.page_requested.emit(item.data(_CARD_ROLE).page_number)

    def _update_generate_button(self) -> None:
        is_running = self._worker is not None
        self._generate_button.setEnabled(not is_running)
        self._generate_button.setText("Writing cards…" if is_running else _GENERATE_BUTTON_TEXT)

    # --------------------------------------------------------------- generation

    def generate_cards(self) -> None:
        if self._paper is None or self._worker is not None:
            return
        first_page = self._first_page_field.value() - 1
        last_page = max(self._last_page_field.value() - 1, first_page)
        if last_page - first_page + 1 > _MAXIMUM_PAGES_PER_REQUEST:
            last_page = first_page + _MAXIMUM_PAGES_PER_REQUEST - 1
            self._last_page_field.setValue(last_page + 1)
            self.status_message.emit(
                f"Cards are made from up to {_MAXIMUM_PAGES_PER_REQUEST} pages at a time."
            )
        pages = [
            (page_number, self._page_text_provider(page_number))
            for page_number in range(first_page, last_page + 1)
        ]
        request = StudyCardRequest(
            pages=pages,
            model_name=self._database.get_setting(MODEL_SETTING_KEY, DEFAULT_MODEL_NAME),
        )
        self._worker = StudyCardWorker(self._paper.paper_id, request, self)
        self._worker.cards_ready.connect(self._on_cards_ready)
        self._worker.generation_failed.connect(self._on_generation_failed)
        self._worker.finished.connect(self._on_worker_finished)
        self._update_generate_button()
        self.status_message.emit("The AI is writing study cards…")
        self._worker.start()

    def _on_cards_ready(self, paper_id: int, cards: list) -> None:
        try:
            self._database.get_paper(paper_id)
        except RecordNotFoundError:
            return
        self._database.add_study_cards(paper_id, cards)
        count = len(cards)
        self.status_message.emit(f"{count} study card{'' if count == 1 else 's'} added.")
        if self._paper is not None and self._paper.paper_id == paper_id:
            self._reload_cards()

    def _on_generation_failed(self, _paper_id: int, message: str) -> None:
        self.status_message.emit(message)

    def _on_worker_finished(self) -> None:
        if self._worker is not None:
            self._worker.deleteLater()
            self._worker = None
        self._update_generate_button()

    def finish_background_work(self) -> None:
        """Call before the window closes."""
        worker = self._worker
        if worker is None or not worker.isRunning():
            return
        for signal in (worker.cards_ready, worker.generation_failed, worker.finished):
            signal.disconnect()
        if not worker.wait(1500):
            worker.terminate()
            worker.wait(1500)
        self._worker = None

    # ------------------------------------------------------------------ actions

    def _delete_selected_card(self) -> None:
        card = self._selected_card()
        if card is not None:
            self._database.delete_study_card(card.card_id)
            self._reload_cards()

    def _export_cards(self) -> None:
        if self._paper is None:
            return
        cards = self._database.list_study_cards(self._paper.paper_id)
        suggested_name = sanitize_file_name(f"{self._paper.title} - cards", "study cards") + ".txt"
        file_path, _selected_filter = QFileDialog.getSaveFileName(
            self, "Export Study Cards", suggested_name, "Anki text files (*.txt)"
        )
        if not file_path:
            return
        try:
            Path(file_path).write_text(
                export_anki_text(
                    [(card.question, card.answer) for card in cards], self._paper.title
                ),
                encoding="utf-8",
            )
        except OSError as error:
            QMessageBox.warning(self, "Export Study Cards", f"Could not write the file:\n\n{error}")
            return
        self.status_message.emit(
            f"{len(cards)} cards exported. In Anki, use File > Import and choose that file."
        )
