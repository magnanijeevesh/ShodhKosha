"""Small panels shown beside the details form: highlights and search hits."""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Sequence

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from shodhkosha.annotation_export import describe_annotation, format_annotations_markdown
from shodhkosha.library_database import AnnotationDraft, PageHit
from shodhkosha.library_storage import sanitize_file_name

_PAGE_ROLE = Qt.ItemDataRole.UserRole


def _create_page_list(parent: QWidget) -> QListWidget:
    page_list = QListWidget(parent)
    page_list.setWordWrap(True)
    page_list.setAlternatingRowColors(True)
    page_list.setSpacing(1)
    return page_list


class AnnotationsPanel(QWidget):
    """Lists the open paper's highlights and notes. Click one to go to its page."""

    page_requested = pyqtSignal(int)
    status_message = pyqtSignal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("inspectorPanel")
        self._title = ""
        self._annotations: list[AnnotationDraft] = []

        self._list = _create_page_list(self)
        self._list.itemClicked.connect(self._on_item_clicked)
        self._empty_label = QLabel(
            "No highlights or notes yet.\nSelect text in the PDF and click a highlight color.",
            self,
        )
        self._empty_label.setObjectName("mutedLabel")
        self._empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._copy_button = QPushButton("Copy All", self)
        self._copy_button.clicked.connect(lambda _checked=False: self._copy_all())
        self._export_button = QPushButton("Export as Markdown\u2026", self)
        self._export_button.clicked.connect(lambda _checked=False: self._export())

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self._copy_button)
        buttons.addWidget(self._export_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.addWidget(self._list, 1)
        layout.addWidget(self._empty_label, 1)
        layout.addLayout(buttons)
        self.set_annotations("", [])

    def set_annotations(self, title: str, annotations: Sequence[AnnotationDraft]) -> None:
        self._title = title
        self._annotations = sorted(annotations, key=lambda item: item.page_number)
        self._list.clear()
        for annotation in self._annotations:
            item = QListWidgetItem(describe_annotation(annotation))
            item.setData(_PAGE_ROLE, annotation.page_number)
            item.setToolTip(annotation.selected_text or annotation.note_text)
            self._list.addItem(item)
        has_annotations = bool(self._annotations)
        self._list.setVisible(has_annotations)
        self._empty_label.setVisible(not has_annotations)
        self._copy_button.setEnabled(has_annotations)
        self._export_button.setEnabled(has_annotations)

    @property
    def annotation_count(self) -> int:
        return len(self._annotations)

    def _on_item_clicked(self, item: QListWidgetItem) -> None:
        self.page_requested.emit(int(item.data(_PAGE_ROLE)))

    def _copy_all(self) -> None:
        QApplication.clipboard().setText(
            format_annotations_markdown(self._title, self._annotations)
        )
        self.status_message.emit("Highlights and notes copied.")

    def _export(self) -> None:
        suggested_name = sanitize_file_name(f"{self._title} - highlights", "highlights") + ".md"
        file_path, _selected_filter = QFileDialog.getSaveFileName(
            self, "Export Highlights", suggested_name, "Markdown files (*.md);;Text files (*.txt)"
        )
        if not file_path:
            return
        try:
            Path(file_path).write_text(
                format_annotations_markdown(self._title, self._annotations), encoding="utf-8"
            )
        except OSError as error:
            QMessageBox.warning(self, "Export Highlights", f"Could not write the file:\n\n{error}")
            return
        self.status_message.emit(f"Highlights exported to {Path(file_path).name}.")


class PageHitsPanel(QWidget):
    """Lists the pages of the open paper that match the search box."""

    hit_requested = pyqtSignal(int)  # page number

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("inspectorPanel")
        self._list = _create_page_list(self)
        self._list.itemClicked.connect(
            lambda item: self.hit_requested.emit(int(item.data(_PAGE_ROLE)))
        )
        self._empty_label = QLabel(self)
        self._empty_label.setObjectName("mutedLabel")
        self._empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_label.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.addWidget(self._list, 1)
        layout.addWidget(self._empty_label, 1)
        self.set_hits([], "")

    def set_hits(self, hits: Sequence[PageHit], search_text: str) -> None:
        self._list.clear()
        for hit in hits:
            source = " (scanned)" if hit.is_ocr else ""
            item = QListWidgetItem(f"p. {hit.page_number + 1}{source} \u00b7 {hit.excerpt}")
            item.setData(_PAGE_ROLE, hit.page_number)
            self._list.addItem(item)
        self._list.setVisible(bool(hits))
        self._empty_label.setVisible(not hits)
        if not search_text.strip():
            self._empty_label.setText(
                "Type in the search box to find words inside this paper's pages."
            )
        else:
            self._empty_label.setText(
                "No page of this paper contains all of those words.\n"
                "It matched on its title, authors, tags or highlights."
            )

    @property
    def hit_count(self) -> int:
        return self._list.count()
