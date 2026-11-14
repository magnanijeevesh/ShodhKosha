"""Builds the full-text search index in the background, including OCR of scans.

Everything here runs on the GUI thread in small slices driven by a timer, so
the window stays responsive without PyMuPDF ever being used from two threads.
The one slow job, recognising text in a scanned page, is handed to the
Tesseract program as a separate process.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from collections import deque
from pathlib import Path
from typing import Iterable, Optional

from PyQt6.QtCore import QObject, QProcess, QTimer, pyqtSignal

from shodhkosha.library_database import LibraryDatabase, PageText, RecordNotFoundError
from shodhkosha.pdf_document_service import PdfDocumentError, PdfDocumentService

_PAGES_PER_SLICE = 12
_SLICE_INTERVAL_MILLISECONDS = 5
_OCR_LANGUAGE = "eng"
_SPARSE_PAGE_CHARACTERS = 25  # fewer characters than this: treat the page as a scan
_TESSERACT_LOCATIONS = (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe",
    "/opt/homebrew/bin/tesseract",
    "/usr/local/bin/tesseract",
)


def find_tesseract() -> Optional[str]:
    """Path of the Tesseract OCR program, or None if it is not installed."""
    found = shutil.which("tesseract")
    if found:
        return found
    for location in _TESSERACT_LOCATIONS:
        expanded = os.path.expandvars(location)
        if os.path.isfile(expanded):
            return expanded
    return None


class TextIndexQueue(QObject):
    """Indexes the text of queued papers one after another."""

    paper_indexed = pyqtSignal(int, int)  # paper_id, number of pages read by OCR
    indicator_changed = pyqtSignal(str)  # short, persistent text; "" when idle
    message_reported = pyqtSignal(str)

    def __init__(self, database: LibraryDatabase, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._database = database
        self._tesseract_path = find_tesseract()
        self._pending_paper_ids: deque[int] = deque()
        self._reader = PdfDocumentService()
        self._paper_id: Optional[int] = None
        self._next_page = 0
        self._page_texts: list[PageText] = []
        self._ocr_pages: deque[int] = deque()
        self._ocr_page_number = 0
        self._ocr_pages_read = 0
        self._ocr_process: Optional[QProcess] = None
        self._ocr_image_path: Optional[Path] = None
        self._scans_without_ocr = 0

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(_SLICE_INTERVAL_MILLISECONDS)
        self._timer.timeout.connect(self._process_slice)

    @property
    def is_ocr_available(self) -> bool:
        return self._tesseract_path is not None

    def enqueue(self, paper_ids: Iterable[int]) -> None:
        for paper_id in paper_ids:
            if paper_id != self._paper_id and paper_id not in self._pending_paper_ids:
                self._pending_paper_ids.append(paper_id)
        if self._paper_id is None:
            self._start_next_paper()

    # ------------------------------------------------------------ text extraction

    def _start_next_paper(self) -> None:
        while self._pending_paper_ids:
            paper_id = self._pending_paper_ids.popleft()
            try:
                paper = self._database.get_paper(paper_id)
                self._reader.open(paper.file_path)
            except (RecordNotFoundError, PdfDocumentError):
                continue  # removed, or its file is missing; nothing to index
            self._paper_id = paper_id
            self._next_page = 0
            self._page_texts = []
            self._ocr_pages = deque()
            self._ocr_pages_read = 0
            self._update_indicator()
            self._timer.start()
            return

        self._paper_id = None
        self.indicator_changed.emit("")
        if self._scans_without_ocr:
            count = self._scans_without_ocr
            self._scans_without_ocr = 0
            self.message_reported.emit(
                f"{count} scanned PDF{'' if count == 1 else 's'} cannot be searched yet. "
                "Install Tesseract OCR, then restart ShodhKosha."
            )

    def _update_indicator(self) -> None:
        waiting = len(self._pending_paper_ids)
        suffix = f" ({waiting} more waiting)" if waiting else ""
        if self._ocr_pages or self._ocr_process is not None:
            remaining = len(self._ocr_pages) + (1 if self._ocr_process is not None else 0)
            self.indicator_changed.emit(f"OCR: {remaining} scanned pages left{suffix}")
        else:
            self.indicator_changed.emit(f"Indexing text{suffix}")

    def _process_slice(self) -> None:
        """Extract the text of the next few pages, then yield to the window."""
        if self._paper_id is None or not self._reader.is_open:
            return
        last_page = min(self._next_page + _PAGES_PER_SLICE, self._reader.page_count)
        for page_number in range(self._next_page, last_page):
            text = self._reader.get_page_text(page_number)
            self._page_texts.append(PageText(page_number, text))
            if len(text) < _SPARSE_PAGE_CHARACTERS:
                self._ocr_pages.append(page_number)
        self._next_page = last_page

        if self._next_page < self._reader.page_count:
            self._timer.start()
            return
        if not self._store_page_texts():
            self._finish_paper()
            return
        if self._ocr_pages and self._tesseract_path is None:
            # Count it as a scan only when most pages have no text of their own.
            if len(self._ocr_pages) * 2 >= self._reader.page_count:
                self._scans_without_ocr += 1
            self._ocr_pages.clear()
        if self._ocr_pages:
            self._start_next_ocr_page()
        else:
            self._finish_paper()

    def _store_page_texts(self) -> bool:
        try:
            self._database.get_paper(self._paper_id)
        except RecordNotFoundError:
            return False  # removed from the library while it was being read
        self._database.replace_page_texts(self._paper_id, self._page_texts)
        return True

    def _finish_paper(self) -> None:
        paper_id, pages_read = self._paper_id, self._ocr_pages_read
        self._reader.close()
        self._page_texts = []
        self._paper_id = None
        if paper_id is not None:
            self.paper_indexed.emit(paper_id, pages_read)
        self._start_next_paper()

    # ------------------------------------------------------------------------ OCR

    def _start_next_ocr_page(self) -> None:
        if not self._ocr_pages or self._paper_id is None:
            self._finish_paper()
            return
        self._ocr_page_number = self._ocr_pages.popleft()
        try:
            image_data = self._reader.render_page_png(self._ocr_page_number)
            self._ocr_image_path = (
                Path(tempfile.gettempdir()) / f"shodhkosha_ocr_{uuid.uuid4().hex}.png"
            )
            self._ocr_image_path.write_bytes(image_data)
        except (PdfDocumentError, OSError):
            self._ocr_image_path = None
            self._start_next_ocr_page()
            return

        process = QProcess(self)
        process.finished.connect(self._on_ocr_finished)
        process.errorOccurred.connect(self._on_ocr_error)
        self._ocr_process = process
        self._update_indicator()
        process.start(
            self._tesseract_path, [str(self._ocr_image_path), "stdout", "-l", _OCR_LANGUAGE]
        )

    def _release_ocr_process(self) -> Optional[QProcess]:
        process, self._ocr_process = self._ocr_process, None
        if self._ocr_image_path is not None:
            try:
                self._ocr_image_path.unlink()
            except OSError:
                pass
            self._ocr_image_path = None
        if process is not None:
            process.deleteLater()
        return process

    def _on_ocr_finished(self, exit_code: int, _exit_status) -> None:
        process = self._ocr_process
        if process is None:
            return
        output = bytes(process.readAllStandardOutput()).decode("utf-8", errors="replace")
        self._release_ocr_process()
        text = " ".join(output.split())
        if exit_code == 0 and text and self._paper_id is not None:
            try:
                self._database.get_paper(self._paper_id)
                self._database.set_page_text(
                    self._paper_id, PageText(self._ocr_page_number, text, is_ocr=True)
                )
                self._ocr_pages_read += 1
            except RecordNotFoundError:
                self._ocr_pages.clear()
        self._start_next_ocr_page()

    def _on_ocr_error(self, _error) -> None:
        if self._release_ocr_process() is None:
            return
        # Tesseract could not be run at all; do not retry it for every page.
        self._ocr_pages.clear()
        self._tesseract_path = None
        self.message_reported.emit("Tesseract OCR could not be started; scans were not read.")
        self._finish_paper()

    def finish_background_work(self) -> None:
        """Call before the window closes."""
        self._pending_paper_ids.clear()
        self._ocr_pages.clear()
        self._timer.stop()
        process = self._ocr_process
        if process is not None:
            process.finished.disconnect()
            process.errorOccurred.disconnect()
            process.kill()
            process.waitForFinished(1000)
            self._release_ocr_process()
        self._reader.close()
        self._paper_id = None
