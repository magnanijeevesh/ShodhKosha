"""Background thread that runs a DOI lookup so the window stays responsive."""

from __future__ import annotations

from collections import deque
from typing import Optional

from PyQt6.QtCore import QObject, QThread, pyqtSignal

from shodhkosha.doi_lookup import DoiLookupError, look_up_doi


class DoiLookupWorker(QThread):
    """Emits ``lookup_succeeded(paper_id, record)`` or ``lookup_failed(paper_id, message)``."""

    lookup_succeeded = pyqtSignal(int, object)
    lookup_failed = pyqtSignal(int, str)

    def __init__(self, paper_id: int, doi_text: str, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._paper_id = paper_id
        self._doi_text = doi_text

    def run(self) -> None:
        try:
            record = look_up_doi(self._doi_text)
        except DoiLookupError as error:
            self.lookup_failed.emit(self._paper_id, str(error))
        except Exception as error:  # never let a worker thread crash the application
            self.lookup_failed.emit(self._paper_id, f"The DOI lookup failed: {error}")
        else:
            self.lookup_succeeded.emit(self._paper_id, record)


class DoiLookupQueue(QObject):
    """Looks up the DOIs of newly imported papers, one request at a time."""

    lookup_succeeded = pyqtSignal(int, object)  # paper_id, BibliographicRecord
    message_reported = pyqtSignal(str)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._pending: deque[tuple[int, str]] = deque()
        self._worker: Optional[DoiLookupWorker] = None

    def enqueue(self, paper_id: int, doi_text: str) -> None:
        if doi_text.strip():
            self._pending.append((paper_id, doi_text))
            if self._worker is None:
                self._start_next()

    def _start_next(self) -> None:
        if not self._pending:
            return
        paper_id, doi_text = self._pending.popleft()
        self._worker = DoiLookupWorker(paper_id, doi_text, self)
        self._worker.lookup_succeeded.connect(self.lookup_succeeded)
        self._worker.lookup_failed.connect(self._on_lookup_failed)
        self._worker.finished.connect(self._on_worker_finished)
        self._worker.start()

    def _on_lookup_failed(self, _paper_id: int, message: str) -> None:
        if message.startswith("Could not reach"):
            # Offline: every remaining lookup would fail the same way.
            self._pending.clear()
            self.message_reported.emit(
                "No internet connection, so DOIs were not looked up. "
                "Use Look Up DOI Online later."
            )

    def _on_worker_finished(self) -> None:
        if self._worker is not None:
            self._worker.deleteLater()
            self._worker = None
        self._start_next()

    def finish_background_work(self) -> None:
        """Call before the window closes."""
        self._pending.clear()
        if self._worker is not None and self._worker.isRunning():
            self._worker.wait(11000)
