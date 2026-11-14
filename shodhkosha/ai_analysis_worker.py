"""Runs AI paper analysis in the background, one paper at a time.

``AiAnalysisQueue`` lives on the GUI thread. For each queued paper it gathers
the text and the library's current folders and tags, hands them to a
short-lived ``AiAnalysisWorker`` thread that talks to Ollama, and files the
paper when the answer comes back. Only the worker's network call happens off
the GUI thread; all PDF reading and database access stay on it.
"""

from __future__ import annotations

from collections import deque
from typing import Iterable, Optional

from PyQt6.QtCore import QObject, QThread, pyqtSignal

from shodhkosha.library_database import LibraryDatabase, Paper, RecordNotFoundError
from shodhkosha.paper_analysis import (
    DEFAULT_MODEL_NAME,
    MINIMUM_TEXT_LENGTH,
    AiError,
    AiUnavailableError,
    AnalysisRequest,
    PaperAnalysis,
    apply_analysis,
    request_analysis,
)
from shodhkosha.pdf_document_service import (
    ANALYSIS_TEXT_LIMIT,
    PdfDocumentError,
    PdfDocumentService,
)

MODEL_SETTING_KEY = "ai_model"
_SHUTDOWN_WAIT_MILLISECONDS = 1500


class AiAnalysisWorker(QThread):
    """Analyzes one paper. Emits exactly one of the two result signals."""

    analysis_succeeded = pyqtSignal(int, object)  # paper_id, PaperAnalysis
    analysis_failed = pyqtSignal(int, str, bool)  # paper_id, message, AI unavailable?

    def __init__(
        self, paper_id: int, request: AnalysisRequest, parent: Optional[QObject] = None
    ) -> None:
        super().__init__(parent)
        self._paper_id = paper_id
        self._request = request

    def run(self) -> None:
        try:
            analysis = request_analysis(self._request)
        except AiUnavailableError as error:
            self.analysis_failed.emit(self._paper_id, str(error), True)
        except AiError as error:
            self.analysis_failed.emit(self._paper_id, str(error), False)
        except Exception as error:  # never let a worker thread crash the application
            self.analysis_failed.emit(self._paper_id, f"AI analysis failed: {error}", False)
        else:
            self.analysis_succeeded.emit(self._paper_id, analysis)


class AiAnalysisQueue(QObject):
    """Queues papers for analysis and reports progress for the status bar."""

    indicator_changed = pyqtSignal(str)  # short, persistent text; "" when idle
    message_reported = pyqtSignal(str)  # one-off messages
    paper_analyzed = pyqtSignal(int)  # paper_id, after the database was updated

    def __init__(self, database: LibraryDatabase, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._database = database
        self._pending_paper_ids: deque[int] = deque()
        self._worker: Optional[AiAnalysisWorker] = None
        self._current_paper_id: Optional[int] = None
        self._completed_count = 0
        self._total_count = 0
        self._unavailable_text = ""

    @property
    def is_busy(self) -> bool:
        return self._worker is not None

    def enqueue(self, paper_ids: Iterable[int]) -> None:
        """Add papers to the queue; papers already waiting are not added twice."""
        self._unavailable_text = ""
        for paper_id in paper_ids:
            if paper_id != self._current_paper_id and paper_id not in self._pending_paper_ids:
                self._pending_paper_ids.append(paper_id)
                self._total_count += 1
        if self._worker is None:
            self._start_next()

    def _start_next(self) -> None:
        while self._pending_paper_ids:
            paper_id = self._pending_paper_ids.popleft()
            try:
                paper = self._database.get_paper(paper_id)
            except RecordNotFoundError:
                self._completed_count += 1
                continue  # removed from the library while waiting
            try:
                request = self._build_request(paper)
            except PdfDocumentError as error:
                self._completed_count += 1
                self.message_reported.emit(f"AI skipped a paper: {error}")
                continue

            self._current_paper_id = paper_id
            self._worker = AiAnalysisWorker(paper_id, request, self)
            self._worker.analysis_succeeded.connect(self._on_analysis_succeeded)
            self._worker.analysis_failed.connect(self._on_analysis_failed)
            self._worker.finished.connect(self._on_worker_finished)
            self.indicator_changed.emit(
                f"AI: reading paper {self._completed_count + 1} of {self._total_count}…"
            )
            self._worker.start()
            return

        self._completed_count = 0
        self._total_count = 0
        self.indicator_changed.emit(self._unavailable_text)

    def _build_request(self, paper: Paper) -> AnalysisRequest:
        reader = PdfDocumentService()
        try:
            reader.open(paper.file_path)
            paper_text = reader.extract_analysis_text()
        finally:
            reader.close()
        if len(paper_text.strip()) < MINIMUM_TEXT_LENGTH:
            paper_text = self._stored_text_excerpt(paper.paper_id) or paper_text
        highlights = [
            f"{annotation.selected_text} {annotation.note_text}".strip()
            for annotation in self._database.list_annotations(paper.paper_id)
        ]
        return AnalysisRequest(
            paper_text=paper_text,
            highlights=[text for text in highlights if text],
            existing_folders=[folder.name for folder in self._database.list_folders()],
            existing_tags=[tag.name for tag in self._database.list_tags()],
            model_name=self._database.get_setting(MODEL_SETTING_KEY, DEFAULT_MODEL_NAME),
        )

    def _stored_text_excerpt(self, paper_id: int) -> str:
        """For a scanned PDF: the first three pages and the last, as read by OCR."""
        stored_pages = self._database.get_page_texts(paper_id)
        page_numbers = sorted(stored_pages)
        chosen = page_numbers[:3] + (page_numbers[-1:] if len(page_numbers) > 3 else [])
        return "\n\n".join(stored_pages[n] for n in chosen if stored_pages[n])[:ANALYSIS_TEXT_LIMIT]

    def _on_analysis_succeeded(self, paper_id: int, analysis: PaperAnalysis) -> None:
        try:
            paper = self._database.get_paper(paper_id)
        except RecordNotFoundError:
            return
        updated_paper = apply_analysis(self._database, paper, analysis)
        self._database.delete_unused_tags()  # tags replaced by this analysis
        folder_text = f" → {updated_paper.folder_name}" if updated_paper.folder_name else ""
        self.message_reported.emit(f"AI filed “{updated_paper.title}”{folder_text}")
        self.paper_analyzed.emit(paper_id)

    def _on_analysis_failed(self, _paper_id: int, message: str, ai_unavailable: bool) -> None:
        if ai_unavailable:
            # Every remaining paper would fail the same way, so stop here.
            self._pending_paper_ids.clear()
            self._unavailable_text = "AI unavailable"
        self.message_reported.emit(message)

    def _on_worker_finished(self) -> None:
        if self._worker is not None:
            self._worker.deleteLater()
            self._worker = None
        self._current_paper_id = None
        self._completed_count += 1
        self._start_next()

    def finish_background_work(self) -> None:
        """Call before the window closes. Stops the queue and the running job."""
        self._pending_paper_ids.clear()
        worker = self._worker
        if worker is None or not worker.isRunning():
            return
        for signal in (worker.analysis_succeeded, worker.analysis_failed, worker.finished):
            signal.disconnect()
        if not worker.wait(_SHUTDOWN_WAIT_MILLISECONDS):
            # The model may take minutes to answer; do not hold the window open for it.
            worker.terminate()
            worker.wait(_SHUTDOWN_WAIT_MILLISECONDS)
        self._worker = None
