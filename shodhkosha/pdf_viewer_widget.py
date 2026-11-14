"""Scrolling PDF viewer and editor.

Pages are laid out in one continuous column and only the pages near the
visible area are rendered. The viewer also provides text selection,
highlights, sticky notes, text boxes, page deletion and safe saving.

Edits are held in memory until the user clicks "Save & Replace PDF".
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Optional, Sequence, Union

from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QImage,
    QKeySequence,
    QPainter,
    QPen,
    QPixmap,
    QPolygonF,
    QShortcut,
)
from PyQt6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

try:  # Windows' built-in offline voices; absent from some PyQt6 builds
    from PyQt6.QtTextToSpeech import QTextToSpeech
except ImportError:
    QTextToSpeech = None

from shodhkosha.pdf_document_service import (
    HIGHLIGHT_COLORS,
    PageAnnotation,
    PdfDocumentError,
    PdfDocumentService,
    PdfSaveError,
    WordBox,
    words_between_points,
)

# PDF units are 1/72 inch; screens are treated as 96 logical pixels per inch,
# so 100 % zoom shows a page at about its physical size.
_SCREEN_PIXELS_PER_POINT = 96.0 / 72.0
_MINIMUM_ZOOM = 0.25
_MAXIMUM_ZOOM = 5.0
_ZOOM_STEP = 1.25
_PAGE_MARGIN = 16
_PAGE_SPACING = 14
_PRERENDER_PAGES = 1
_CACHED_PAGES = 4

_SELECT_TOOL = "select"
_NOTE_TOOL = "note"
_TEXT_BOX_TOOL = "text"
_PEN_TOOL = "pen"
_ARROW_TOOL = "arrow"

_CLICK_TOLERANCE_POINTS = 2.0
_MINIMUM_TEXT_BOX_WIDTH = 30.0
_MINIMUM_TEXT_BOX_HEIGHT = 14.0
_DEFAULT_TEXT_BOX_SIZE = (220.0, 60.0)

_SELECTION_COLOR = QColor(47, 111, 237, 80)
_RUBBER_BAND_COLOR = QColor(47, 111, 237)
_SEARCH_HIT_COLOR = QColor(255, 165, 0, 95)
_DRAWING_PREVIEW_COLOR = QColor(220, 38, 38)
_DARK_PAGE_COLOR = QColor(18, 18, 18)
_ANNOTATION_LABELS = {
    "highlight": "Highlight",
    "note": "Note",
    "text": "Text Box",
    "drawing": "Drawing",
}
_READ_ALOUD_TEXT = "Read Aloud"

_DEFAULT_PLACEHOLDER = "Select a reference to view its PDF"


class _PageWidget(QWidget):
    """Shows one rendered page and forwards mouse input to the viewer."""

    def __init__(self, page_number: int, viewer: "PdfViewerWidget", parent: QWidget) -> None:
        super().__init__(parent)
        self.page_number = page_number
        self._viewer = viewer
        self._pixmap: Optional[QPixmap] = None
        self._selection_rectangles: list[QRectF] = []
        self._search_rectangles: list[QRectF] = []
        self._preview_points: list[QPointF] = []
        self._rubber_band: Optional[QRectF] = None
        self._is_dark = False

    def set_dark(self, is_dark: bool) -> None:
        self._is_dark = is_dark
        self.update()

    def set_search_rectangles(self, rectangles: Sequence[QRectF]) -> None:
        self._search_rectangles = list(rectangles)
        self.update()

    def set_preview_points(self, points: Sequence[QPointF]) -> None:
        """Line shown while the user is drawing with the pen or arrow tool."""
        self._preview_points = list(points)
        self.update()

    def has_pixmap(self) -> bool:
        return self._pixmap is not None

    def set_pixmap(self, pixmap: QPixmap) -> None:
        self._pixmap = pixmap
        self.update()

    def clear_pixmap(self) -> None:
        if self._pixmap is not None:
            self._pixmap = None
            self.update()

    def set_selection_rectangles(self, rectangles: Sequence[QRectF]) -> None:
        self._selection_rectangles = list(rectangles)
        self.update()

    def set_rubber_band(self, rectangle: Optional[QRectF]) -> None:
        self._rubber_band = rectangle
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        painter = QPainter(self)
        painter.fillRect(self.rect(), _DARK_PAGE_COLOR if self._is_dark else Qt.GlobalColor.white)
        if self._pixmap is not None:
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.drawPixmap(self.rect(), self._pixmap)
        for rectangle in self._search_rectangles:
            painter.fillRect(rectangle, _SEARCH_HIT_COLOR)
        for rectangle in self._selection_rectangles:
            painter.fillRect(rectangle, _SELECTION_COLOR)
        if len(self._preview_points) >= 2:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(QPen(_DRAWING_PREVIEW_COLOR, 2))
            painter.drawPolyline(QPolygonF(self._preview_points))
        if self._rubber_band is not None:
            painter.setPen(QPen(_RUBBER_BAND_COLOR, 1, Qt.PenStyle.DashLine))
            painter.drawRect(self._rubber_band)
        painter.end()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._viewer._on_page_pressed(self, event.position())
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if event.buttons() & Qt.MouseButton.LeftButton:
            self._viewer._on_page_dragged(self, event.position())

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._viewer._on_page_released(self, event.position())

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        self._viewer._show_page_menu(self, QPointF(event.pos()), event.globalPos())


class _PageScrollArea(QScrollArea):
    """Scroll area that turns Ctrl + mouse wheel into zoom requests."""

    zoom_step_requested = pyqtSignal(int)

    def wheelEvent(self, event) -> None:  # noqa: N802
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            delta = event.angleDelta().y()
            if delta:
                self.zoom_step_requested.emit(1 if delta > 0 else -1)
            event.accept()
            return
        super().wheelEvent(event)


class PdfViewerWidget(QWidget):
    """Displays and edits one PDF. Owns the :class:`PdfDocumentService` for it."""

    page_changed = pyqtSignal(int)
    document_saved = pyqtSignal(object)  # SaveResult
    status_message = pyqtSignal(str)
    annotations_changed = pyqtSignal()  # the open document's annotations may differ now

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._document_service = PdfDocumentService()
        self._zoom = 1.0
        self._fit_width_enabled = True
        self._page_widgets: list[_PageWidget] = []
        self._page_sizes: list[tuple[float, float]] = []  # in PDF units
        self._page_tops: list[int] = []  # in canvas pixels
        self._page_heights: list[int] = []
        self._current_page = 0

        self._active_tool = _SELECT_TOOL
        self._page_words: dict[int, list[WordBox]] = {}
        self._drag_page: Optional[int] = None
        self._drag_start: tuple[float, float] = (0.0, 0.0)
        self._selection_page: Optional[int] = None
        self._selected_words: list[WordBox] = []
        self._pen_points: list[tuple[float, float]] = []
        self._preview_points: list[QPointF] = []
        self._search_page: Optional[int] = None
        self._dark_pages = False
        # Returns stored text for a page whose PDF has none (a scan read by OCR).
        self.page_text_fallback = None
        self._speech = None
        if QTextToSpeech is not None:
            try:
                self._speech = QTextToSpeech(self)
                self._speech.stateChanged.connect(lambda _state: self._update_read_aloud_button())
            except Exception:  # no speech engine on this computer
                self._speech = None

        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(0)
        self._render_timer.timeout.connect(self._render_visible_pages)

        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self._build_interface()
        self._build_shortcuts()
        self._update_controls()

    # ---------------------------------------------------------------- interface

    def _build_interface(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._build_navigation_toolbar())
        layout.addWidget(self._build_annotation_toolbar())

        self._canvas = QWidget()
        self._canvas.setObjectName("pdfCanvas")

        self._scroll_area = _PageScrollArea(self)
        self._scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll_area.setAlignment(
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop
        )
        self._scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self._scroll_area.viewport().setObjectName("pdfViewport")
        self._scroll_area.setWidget(self._canvas)
        self._scroll_area.verticalScrollBar().valueChanged.connect(self._on_scrolled)
        self._scroll_area.zoom_step_requested.connect(self._on_zoom_step_requested)

        self._placeholder_label = QLabel(_DEFAULT_PLACEHOLDER, self)
        self._placeholder_label.setObjectName("emptyStateLabel")
        self._placeholder_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._placeholder_label.setWordWrap(True)

        self._content_stack = QStackedWidget(self)
        self._content_stack.addWidget(self._placeholder_label)
        self._content_stack.addWidget(self._scroll_area)
        layout.addWidget(self._content_stack, 1)

    def _build_navigation_toolbar(self) -> QWidget:
        toolbar = QWidget(self)
        toolbar.setObjectName("viewerToolbar")
        row = QHBoxLayout(toolbar)
        row.setContentsMargins(8, 4, 8, 4)
        row.setSpacing(2)

        self._previous_page_button = self._create_tool_button(
            toolbar, "‹", "Previous page", self.go_to_previous_page
        )
        self._page_number_field = QSpinBox(toolbar)
        self._page_number_field.setMinimum(1)
        self._page_number_field.setKeyboardTracking(False)
        self._page_number_field.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        self._page_number_field.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._page_number_field.setFixedWidth(56)
        self._page_number_field.setToolTip("Go to page")
        self._page_number_field.valueChanged.connect(self._on_page_number_entered)
        self._page_count_label = QLabel("", toolbar)
        self._page_count_label.setObjectName("mutedLabel")
        self._next_page_button = self._create_tool_button(
            toolbar, "›", "Next page", self.go_to_next_page
        )
        self._zoom_out_button = self._create_tool_button(
            toolbar, "−", "Zoom out (Ctrl + scroll)", self.zoom_out
        )
        self._zoom_label = QLabel("", toolbar)
        self._zoom_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._zoom_label.setMinimumWidth(48)
        self._zoom_in_button = self._create_tool_button(
            toolbar, "+", "Zoom in (Ctrl + scroll)", self.zoom_in
        )
        self._fit_width_button = self._create_tool_button(
            toolbar, "Fit width", "Fit the page to the width of the viewer", self.fit_to_width
        )
        self._save_button = QPushButton("Save && Replace PDF", toolbar)
        self._save_button.setObjectName("primaryButton")
        self._save_button.setToolTip(
            "Write your highlights, notes and page deletions into the PDF (Ctrl+S)"
        )
        self._save_button.clicked.connect(lambda _checked=False: self.save_document())

        row.addWidget(self._previous_page_button)
        row.addWidget(self._page_number_field)
        row.addWidget(self._page_count_label)
        row.addWidget(self._next_page_button)
        row.addSpacing(12)
        row.addWidget(self._zoom_out_button)
        row.addWidget(self._zoom_label)
        row.addWidget(self._zoom_in_button)
        row.addWidget(self._fit_width_button)
        row.addStretch(1)
        row.addWidget(self._save_button)
        return toolbar

    def _build_annotation_toolbar(self) -> QWidget:
        toolbar = QWidget(self)
        toolbar.setObjectName("viewerToolbar")
        row = QHBoxLayout(toolbar)
        row.setContentsMargins(8, 4, 8, 4)
        row.setSpacing(2)

        self._tool_group = QButtonGroup(self)
        self._tool_group.setExclusive(True)
        self._tool_buttons: dict[str, QToolButton] = {}
        for tool_name, text, tool_tip in (
            (_SELECT_TOOL, "Select", "Drag over text to select it, then pick a highlight color"),
            (_NOTE_TOOL, "Note", "Click on the page to add a sticky note"),
            (_TEXT_BOX_TOOL, "Text Box", "Drag on the page to add visible text"),
            (_PEN_TOOL, "Pen", "Draw freehand on the page"),
            (_ARROW_TOOL, "Arrow", "Drag to draw an arrow; it points where you release"),
        ):
            button = QToolButton(toolbar)
            button.setText(text)
            button.setToolTip(tool_tip)
            button.setCheckable(True)
            button.setAutoRaise(True)
            button.clicked.connect(lambda _checked, name=tool_name: self._set_active_tool(name))
            self._tool_group.addButton(button)
            self._tool_buttons[tool_name] = button
            row.addWidget(button)
        self._tool_buttons[_SELECT_TOOL].setChecked(True)

        row.addSpacing(12)
        highlight_label = QLabel("Highlight", toolbar)
        highlight_label.setObjectName("mutedLabel")
        row.addWidget(highlight_label)
        row.addSpacing(4)
        self._highlight_buttons: list[QToolButton] = []
        for color_name, (red, green, blue) in HIGHLIGHT_COLORS.items():
            swatch = QToolButton(toolbar)
            swatch.setFixedSize(22, 22)
            swatch.setToolTip(f"Highlight the selected text in {color_name}")
            swatch.setStyleSheet(
                "QToolButton {"
                f" background: rgb({round(red * 255)}, {round(green * 255)}, {round(blue * 255)});"
                " border: 1px solid rgba(0, 0, 0, 70); border-radius: 4px; padding: 0; }"
                "QToolButton:hover { border: 2px solid rgba(0, 0, 0, 140); }"
            )
            swatch.clicked.connect(lambda _checked, name=color_name: self.highlight_selection(name))
            self._highlight_buttons.append(swatch)
            row.addWidget(swatch)
            row.addSpacing(3)

        row.addStretch(1)
        self._read_aloud_button = self._create_tool_button(
            toolbar,
            _READ_ALOUD_TEXT,
            "Read the selected text, or the current page, aloud with an offline voice",
            self.toggle_read_aloud,
        )
        if self._speech is None:
            self._read_aloud_button.setToolTip("Read aloud is not available on this computer.")
        row.addWidget(self._read_aloud_button)
        self._delete_page_button = self._create_tool_button(
            toolbar, "Delete Page", "Remove the current page from the PDF", self.delete_current_page
        )
        row.addWidget(self._delete_page_button)
        return toolbar

    def _create_tool_button(self, parent: QWidget, text: str, tool_tip: str, on_click) -> QToolButton:
        button = QToolButton(parent)
        button.setText(text)
        button.setToolTip(tool_tip)
        button.setAutoRaise(True)
        button.clicked.connect(lambda _checked=False: on_click())
        return button

    def _build_shortcuts(self) -> None:
        for key_sequence, handler in (
            (QKeySequence.StandardKey.Copy, self.copy_selection),
            (QKeySequence.StandardKey.Save, self.save_document),
        ):
            shortcut = QShortcut(QKeySequence(key_sequence), self)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(lambda run=handler: run())

    def _update_controls(self) -> None:
        is_open = self._document_service.is_open
        for control in (
            self._previous_page_button,
            self._next_page_button,
            self._page_number_field,
            self._zoom_out_button,
            self._zoom_in_button,
            self._fit_width_button,
            self._delete_page_button,
            *self._tool_buttons.values(),
            *self._highlight_buttons,
        ):
            control.setEnabled(is_open)
        self._save_button.setEnabled(is_open and self._document_service.has_unsaved_changes)
        self._read_aloud_button.setEnabled(is_open and self._speech is not None)
        page_count = self._document_service.page_count if is_open else 0
        self._page_count_label.setText(f"of {page_count}" if is_open else "")
        self._zoom_label.setText(f"{round(self._zoom * 100)}%" if is_open else "")
        self._page_number_field.blockSignals(True)
        self._page_number_field.setMaximum(max(page_count, 1))
        self._page_number_field.setValue(self._current_page + 1)
        self._page_number_field.blockSignals(False)

    # ----------------------------------------------------------------- document

    @property
    def document_service(self) -> PdfDocumentService:
        return self._document_service

    @property
    def current_page(self) -> int:
        return self._current_page

    @property
    def zoom(self) -> float:
        return self._zoom

    @property
    def has_unsaved_changes(self) -> bool:
        return self._document_service.is_open and self._document_service.has_unsaved_changes

    def load_document(self, file_path: Union[str, Path]) -> None:
        """Open a PDF and show its first page. Raises ``PdfOpenError`` on failure.

        Any unsaved changes to the previous document are discarded; the main
        window asks the user about them before calling this.
        """
        self.stop_reading_aloud()
        self._document_service.open(file_path)
        self._set_active_tool(_SELECT_TOOL)
        self._rebuild_pages()
        self._content_stack.setCurrentWidget(self._scroll_area)
        self._current_page = 0
        if self._fit_width_enabled:
            fit_zoom = self._fit_width_zoom()
            if fit_zoom is not None:
                self._zoom = fit_zoom
        self._layout_pages()
        self._scroll_area.verticalScrollBar().setValue(0)
        self._update_controls()
        self._render_timer.start()
        self.annotations_changed.emit()

    def close_document(self, placeholder_text: str = _DEFAULT_PLACEHOLDER) -> None:
        """Close the PDF, discarding unsaved changes."""
        self.stop_reading_aloud()
        self._document_service.close()
        self._remove_page_widgets()
        self._current_page = 0
        self._placeholder_label.setText(placeholder_text)
        self._content_stack.setCurrentWidget(self._placeholder_label)
        self._update_controls()
        self.annotations_changed.emit()

    def _remove_page_widgets(self) -> None:
        for page_widget in self._page_widgets:
            page_widget.setParent(None)
            page_widget.deleteLater()
        self._page_widgets = []
        self._page_sizes = []
        self._page_tops = []
        self._page_heights = []
        self._page_words = {}
        self._drag_page = None
        self._selection_page = None
        self._selected_words = []
        self._search_page = None
        self._pen_points = []
        self._preview_points = []

    def _rebuild_pages(self) -> None:
        """Create one widget per page of the open document."""
        self._remove_page_widgets()
        cursor = self._tool_cursor()
        for page_number in range(self._document_service.page_count):
            self._page_sizes.append(self._document_service.page_size(page_number))
            page_widget = _PageWidget(page_number, self, self._canvas)
            page_widget.set_dark(self._dark_pages)
            page_widget.setCursor(cursor)
            page_widget.show()
            self._page_widgets.append(page_widget)

    def _layout_pages(self) -> None:
        """Position every page on the canvas for the current zoom."""
        if not self._page_widgets:
            return
        pixels_per_point = self._pixels_per_point()
        pixel_sizes = [
            (max(round(width * pixels_per_point), 1), max(round(height * pixels_per_point), 1))
            for width, height in self._page_sizes
        ]
        canvas_width = max(width for width, _ in pixel_sizes) + 2 * _PAGE_MARGIN

        self._page_tops = []
        self._page_heights = []
        top = _PAGE_MARGIN
        for page_widget, (width, height) in zip(self._page_widgets, pixel_sizes):
            page_widget.setGeometry((canvas_width - width) // 2, top, width, height)
            self._page_tops.append(top)
            self._page_heights.append(height)
            top += height + _PAGE_SPACING
        self._canvas.setFixedSize(canvas_width, top - _PAGE_SPACING + _PAGE_MARGIN)

    def _reload_pages_keeping_position(self, position: tuple[int, float]) -> None:
        """Rebuild after the document's pages changed (save or page deletion)."""
        self._rebuild_pages()
        self._layout_pages()
        self.restore_view_position(*position)
        self._update_current_page()
        self._update_controls()
        self._render_timer.start()

    # ---------------------------------------------------------------- rendering

    def _pixels_per_point(self) -> float:
        return self._zoom * _SCREEN_PIXELS_PER_POINT

    def _visible_page_range(self) -> Optional[tuple[int, int]]:
        view_top = self._scroll_area.verticalScrollBar().value()
        view_bottom = view_top + self._scroll_area.viewport().height()
        visible = [
            index
            for index, (top, height) in enumerate(zip(self._page_tops, self._page_heights))
            if top < view_bottom and top + height > view_top
        ]
        if not visible:
            return None
        return visible[0], visible[-1]

    def _render_visible_pages(self) -> None:
        if not self._document_service.is_open or not self._page_widgets:
            return
        visible_range = self._visible_page_range()
        if visible_range is None:
            return
        first_visible, last_visible = visible_range
        page_count = len(self._page_widgets)

        first_needed = max(first_visible - _PRERENDER_PAGES, 0)
        last_needed = min(last_visible + _PRERENDER_PAGES, page_count - 1)
        for page_number in range(first_needed, last_needed + 1):
            page_widget = self._page_widgets[page_number]
            if not page_widget.has_pixmap():
                page_widget.set_pixmap(self._render_page(page_number))

        for page_number, page_widget in enumerate(self._page_widgets):
            if (
                page_number < first_visible - _CACHED_PAGES
                or page_number > last_visible + _CACHED_PAGES
            ):
                page_widget.clear_pixmap()

    def _render_page(self, page_number: int) -> QPixmap:
        device_pixel_ratio = self.devicePixelRatioF()
        rendered = self._document_service.render_page(
            page_number, self._pixels_per_point() * device_pixel_ratio
        )
        image = QImage(
            rendered.samples,
            rendered.width,
            rendered.height,
            rendered.stride,
            QImage.Format.Format_RGB888,
        )
        if self._dark_pages:
            image = image.copy()  # own the pixels before changing them
            image.invertPixels()
        pixmap = QPixmap.fromImage(image)  # copies the pixels out of ``rendered``
        pixmap.setDevicePixelRatio(device_pixel_ratio)
        return pixmap

    def set_dark_pages(self, enabled: bool) -> None:
        """Show pages light-on-dark. Only the display changes, never the PDF."""
        if enabled == self._dark_pages:
            return
        self._dark_pages = enabled
        for page_widget in self._page_widgets:
            page_widget.set_dark(enabled)
            page_widget.clear_pixmap()
        self._render_timer.start()

    def show_text_matches(self, page_number: int, words: Sequence[str]) -> None:
        """Go to a page and mark where the given words appear on it."""
        if not self._document_service.is_open or not 0 <= page_number < len(self._page_widgets):
            return
        if self._search_page is not None and self._search_page < len(self._page_widgets):
            self._page_widgets[self._search_page].set_search_rectangles([])
        rectangles = [
            self._to_widget_rectangle(*bounds)
            for word in words
            for bounds in self._document_service.find_text_rectangles(page_number, word)
        ]
        self._search_page = page_number
        self._page_widgets[page_number].set_search_rectangles(rectangles)
        self.go_to_page(page_number)

    def refresh_page(self, page_number: int) -> None:
        """Redraw one page immediately after its content changed."""
        if 0 <= page_number < len(self._page_widgets):
            self._page_widgets[page_number].set_pixmap(self._render_page(page_number))

    # --------------------------------------------------------------- navigation

    def view_position(self) -> tuple[int, float]:
        """The page at the top of the view and how far down it the view starts
        (0.0 = top of the page, 1.0 = bottom). Pair with ``restore_view_position``."""
        if not self._page_tops:
            return 0, 0.0
        view_top = self._scroll_area.verticalScrollBar().value()
        page_number = 0
        for index, top in enumerate(self._page_tops):
            if top <= view_top:
                page_number = index
            else:
                break
        fraction = (view_top - self._page_tops[page_number]) / self._page_heights[page_number]
        return page_number, fraction

    def restore_view_position(self, page_number: int, fraction: float = 0.0) -> None:
        if not self._page_tops:
            return
        page_number = min(max(page_number, 0), len(self._page_tops) - 1)
        target = self._page_tops[page_number] + fraction * self._page_heights[page_number]
        self._scroll_area.verticalScrollBar().setValue(round(target))
        self._on_scrolled()

    def go_to_page(self, page_number: int) -> None:
        if not self._page_tops:
            return
        page_number = min(max(page_number, 0), len(self._page_tops) - 1)
        self._scroll_area.verticalScrollBar().setValue(
            self._page_tops[page_number] - _PAGE_MARGIN // 2
        )
        self._on_scrolled()

    def go_to_previous_page(self) -> None:
        self.go_to_page(self._current_page - 1)

    def go_to_next_page(self) -> None:
        self.go_to_page(self._current_page + 1)

    def _on_page_number_entered(self, one_based_page: int) -> None:
        self.go_to_page(one_based_page - 1)

    def _on_scrolled(self, *_: object) -> None:
        self._update_current_page()
        self._render_timer.start()

    def _update_current_page(self) -> None:
        if not self._page_tops:
            return
        scroll_bar = self._scroll_area.verticalScrollBar()
        viewport_height = self._scroll_area.viewport().height()
        # The current page is the one under a line one third of the way down the view.
        reference_line = scroll_bar.value() + viewport_height / 3
        page_number = 0
        for index, top in enumerate(self._page_tops):
            if top <= reference_line:
                page_number = index
            else:
                break
        if scroll_bar.value() >= scroll_bar.maximum() > 0:
            page_number = len(self._page_tops) - 1
        if page_number != self._current_page:
            self._current_page = page_number
            self._page_number_field.blockSignals(True)
            self._page_number_field.setValue(page_number + 1)
            self._page_number_field.blockSignals(False)
            self.page_changed.emit(page_number)

    # --------------------------------------------------------------------- zoom

    def zoom_in(self) -> None:
        self._fit_width_enabled = False
        self.set_zoom(self._zoom * _ZOOM_STEP)

    def zoom_out(self) -> None:
        self._fit_width_enabled = False
        self.set_zoom(self._zoom / _ZOOM_STEP)

    def fit_to_width(self) -> None:
        self._fit_width_enabled = True
        fit_zoom = self._fit_width_zoom()
        if fit_zoom is not None:
            self.set_zoom(fit_zoom)

    def set_zoom(self, zoom: float) -> None:
        """Change the zoom, keeping the same part of the document in view."""
        zoom = min(max(zoom, _MINIMUM_ZOOM), _MAXIMUM_ZOOM)
        if not self._document_service.is_open or abs(zoom - self._zoom) < 1e-4:
            return
        page_number, fraction = self.view_position()
        self._zoom = zoom
        for page_widget in self._page_widgets:
            page_widget.clear_pixmap()
        self._layout_pages()
        self._update_selection_overlay()
        if self._search_page is not None and self._search_page < len(self._page_widgets):
            # Search marks are positioned in pixels, so they are dropped on zoom.
            self._page_widgets[self._search_page].set_search_rectangles([])
            self._search_page = None
        self.restore_view_position(page_number, fraction)
        self._update_controls()
        self._render_timer.start()

    def _on_zoom_step_requested(self, direction: int) -> None:
        if direction > 0:
            self.zoom_in()
        else:
            self.zoom_out()

    def _fit_width_zoom(self) -> Optional[float]:
        if not self._page_sizes:
            return None
        available_width = self._scroll_area.viewport().width() - 2 * _PAGE_MARGIN
        widest_page = max(width for width, _ in self._page_sizes)
        if available_width < 50 or widest_page <= 0:
            return None
        zoom = available_width / (widest_page * _SCREEN_PIXELS_PER_POINT)
        return min(max(zoom, _MINIMUM_ZOOM), _MAXIMUM_ZOOM)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if self._document_service.is_open:
            # Runs after the scroll area has taken its new size.
            QTimer.singleShot(0, self._on_viewer_resized)

    def _on_viewer_resized(self) -> None:
        if not self._document_service.is_open:
            return
        if self._fit_width_enabled:
            fit_zoom = self._fit_width_zoom()
            if fit_zoom is not None:
                self.set_zoom(fit_zoom)
        self._render_timer.start()

    # -------------------------------------------------------------------- tools

    def _tool_cursor(self) -> Qt.CursorShape:
        if self._active_tool == _SELECT_TOOL:
            return Qt.CursorShape.IBeamCursor
        return Qt.CursorShape.CrossCursor

    def _set_active_tool(self, tool_name: str) -> None:
        self._active_tool = tool_name
        self._tool_buttons[tool_name].setChecked(True)
        cursor = self._tool_cursor()
        for page_widget in self._page_widgets:
            page_widget.setCursor(cursor)
            page_widget.set_rubber_band(None)
            page_widget.set_preview_points([])
        if tool_name != _SELECT_TOOL:
            self._clear_selection()

    def _to_page_point(self, position: QPointF) -> tuple[float, float]:
        pixels_per_point = self._pixels_per_point()
        return position.x() / pixels_per_point, position.y() / pixels_per_point

    def _to_widget_rectangle(self, x0: float, y0: float, x1: float, y1: float) -> QRectF:
        pixels_per_point = self._pixels_per_point()
        return QRectF(
            x0 * pixels_per_point,
            y0 * pixels_per_point,
            (x1 - x0) * pixels_per_point,
            (y1 - y0) * pixels_per_point,
        )

    def _words_on_page(self, page_number: int) -> list[WordBox]:
        if page_number not in self._page_words:
            self._page_words[page_number] = self._document_service.get_page_words(page_number)
        return self._page_words[page_number]

    def _on_page_pressed(self, page_widget: _PageWidget, position: QPointF) -> None:
        self.setFocus()
        self._drag_page = page_widget.page_number
        self._drag_start = self._to_page_point(position)
        self._pen_points = [self._drag_start]
        self._preview_points = [position]
        if self._active_tool == _SELECT_TOOL:
            self._clear_selection()

    def _on_page_dragged(self, page_widget: _PageWidget, position: QPointF) -> None:
        if self._drag_page != page_widget.page_number:
            return
        point = self._to_page_point(position)
        if self._active_tool == _SELECT_TOOL:
            if math.dist(self._drag_start, point) < _CLICK_TOLERANCE_POINTS:
                return
            self._selection_page = page_widget.page_number
            self._selected_words = words_between_points(
                self._words_on_page(page_widget.page_number), self._drag_start, point
            )
            self._update_selection_overlay()
        elif self._active_tool == _TEXT_BOX_TOOL:
            page_widget.set_rubber_band(
                self._to_widget_rectangle(*self._normalized_rectangle(self._drag_start, point))
            )
        elif self._active_tool == _PEN_TOOL:
            self._pen_points.append(point)
            self._preview_points.append(position)
            page_widget.set_preview_points(self._preview_points)
        elif self._active_tool == _ARROW_TOOL:
            page_widget.set_preview_points([self._preview_points[0], position])

    def _on_page_released(self, page_widget: _PageWidget, position: QPointF) -> None:
        if self._drag_page != page_widget.page_number:
            return
        page_number = page_widget.page_number
        point = self._to_page_point(position)
        start = self._drag_start
        self._drag_page = None

        if self._active_tool == _NOTE_TOOL:
            self._add_sticky_note(page_number, start)
        elif self._active_tool == _TEXT_BOX_TOOL:
            page_widget.set_rubber_band(None)
            self._add_text_box(page_number, start, point)
        elif self._active_tool in (_PEN_TOOL, _ARROW_TOOL):
            page_widget.set_preview_points([])
            self._add_drawing(page_number, start, point)
        elif self._selected_words:
            self.status_message.emit(
                "Text selected. Click a highlight color, or press Ctrl+C to copy."
            )

    @staticmethod
    def _normalized_rectangle(
        first: tuple[float, float], second: tuple[float, float]
    ) -> tuple[float, float, float, float]:
        return (
            min(first[0], second[0]),
            min(first[1], second[1]),
            max(first[0], second[0]),
            max(first[1], second[1]),
        )

    # ---------------------------------------------------------------- selection

    def _clear_selection(self) -> None:
        if self._selection_page is not None and self._selection_page < len(self._page_widgets):
            self._page_widgets[self._selection_page].set_selection_rectangles([])
        self._selection_page = None
        self._selected_words = []

    def _update_selection_overlay(self) -> None:
        if self._selection_page is None or self._selection_page >= len(self._page_widgets):
            return
        line_bounds: dict[tuple[int, int], list[float]] = {}
        for word in self._selected_words:
            key = (word.block_number, word.line_number)
            bounds = line_bounds.get(key)
            if bounds is None:
                line_bounds[key] = [word.x0, word.y0, word.x1, word.y1]
            else:
                bounds[0] = min(bounds[0], word.x0)
                bounds[1] = min(bounds[1], word.y0)
                bounds[2] = max(bounds[2], word.x1)
                bounds[3] = max(bounds[3], word.y1)
        self._page_widgets[self._selection_page].set_selection_rectangles(
            [self._to_widget_rectangle(*bounds) for bounds in line_bounds.values()]
        )

    def selected_text(self) -> str:
        return " ".join(word.text for word in self._selected_words)

    def copy_selection(self) -> None:
        text = self.selected_text()
        if text:
            QApplication.clipboard().setText(text)
            self.status_message.emit("Selected text copied.")

    # ---------------------------------------------------------------- annotating

    def highlight_selection(self, color_name: str) -> None:
        if self._selection_page is None or not self._selected_words:
            self.status_message.emit("Drag over some text first, then click a highlight color.")
            return
        page_number = self._selection_page
        try:
            self._document_service.add_highlight(page_number, self._selected_words, color_name)
        except PdfDocumentError as error:
            self.status_message.emit(str(error))
            return
        self._clear_selection()
        self._after_edit(page_number, "Highlight added. Click Save & Replace PDF to keep it.")

    def _add_sticky_note(self, page_number: int, point: tuple[float, float]) -> None:
        note_text, accepted = QInputDialog.getMultiLineText(self, "Sticky Note", "Note:")
        self._set_active_tool(_SELECT_TOOL)
        if not accepted or not note_text.strip():
            return
        try:
            self._document_service.add_sticky_note(page_number, point, note_text)
        except PdfDocumentError as error:
            self.status_message.emit(str(error))
            return
        self._after_edit(page_number, "Note added. Click Save & Replace PDF to keep it.")

    def _add_text_box(
        self, page_number: int, start: tuple[float, float], end: tuple[float, float]
    ) -> None:
        x0, y0, x1, y1 = self._normalized_rectangle(start, end)
        page_width, page_height = self._page_sizes[page_number]
        if x1 - x0 < _MINIMUM_TEXT_BOX_WIDTH or y1 - y0 < _MINIMUM_TEXT_BOX_HEIGHT:
            # A click or a tiny drag: use a default box at that spot.
            default_width, default_height = _DEFAULT_TEXT_BOX_SIZE
            x0 = min(start[0], max(page_width - default_width, 0.0))
            y0 = min(start[1], max(page_height - default_height, 0.0))
            x1, y1 = x0 + default_width, y0 + default_height
        x1, y1 = min(x1, page_width), min(y1, page_height)

        text, accepted = QInputDialog.getMultiLineText(self, "Text Box", "Text:")
        self._set_active_tool(_SELECT_TOOL)
        if not accepted or not text.strip():
            return
        try:
            self._document_service.add_text_box(page_number, (x0, y0, x1, y1), text)
        except PdfDocumentError as error:
            self.status_message.emit(str(error))
            return
        self._after_edit(page_number, "Text box added. Click Save & Replace PDF to keep it.")

    def _add_drawing(
        self, page_number: int, start: tuple[float, float], end: tuple[float, float]
    ) -> None:
        """Finish a pen stroke or an arrow. The pen stays active for more strokes."""
        try:
            if self._active_tool == _PEN_TOOL:
                self._document_service.add_pen_stroke(page_number, self._pen_points)
            else:
                self._document_service.add_arrow(page_number, start, end)
        except PdfDocumentError:
            return  # a click without a drag: nothing to draw
        finally:
            self._pen_points = []
        self._after_edit(page_number, "Drawing added. Click Save & Replace PDF to keep it.")

    def _after_edit(self, page_number: int, message: str) -> None:
        self.refresh_page(page_number)
        self._update_controls()
        self.status_message.emit(message)
        self.annotations_changed.emit()

    # --------------------------------------------------------------- read aloud

    def _is_speaking(self) -> bool:
        if self._speech is None:
            return False
        return self._speech.state() not in (
            QTextToSpeech.State.Ready,
            QTextToSpeech.State.Error,
        )

    def _update_read_aloud_button(self) -> None:
        self._read_aloud_button.setText("Stop Reading" if self._is_speaking() else _READ_ALOUD_TEXT)

    def stop_reading_aloud(self) -> None:
        if self._speech is not None and self._is_speaking():
            self._speech.stop()

    def toggle_read_aloud(self) -> None:
        """Read the selected text, or else the whole current page, aloud."""
        if self._speech is None or not self._document_service.is_open:
            return
        if self._is_speaking():
            self._speech.stop()
            return
        text = self.selected_text() or self.page_text(self._current_page)
        if not text.strip():
            self.status_message.emit("There is no readable text on this page.")
            return
        self._speech.say(text)

    def page_text(self, page_number: int) -> str:
        """Text of a page; for a scanned page, the text read by OCR if there is any."""
        text = self._document_service.get_page_text(page_number)
        if not text and self.page_text_fallback is not None:
            text = self.page_text_fallback(page_number) or ""
        return text

    def _annotations_at(self, page_number: int, point: tuple[float, float]) -> list[PageAnnotation]:
        return [
            annotation
            for annotation in self._document_service.list_page_annotations(page_number)
            if annotation.rectangle[0] - 2 <= point[0] <= annotation.rectangle[2] + 2
            and annotation.rectangle[1] - 2 <= point[1] <= annotation.rectangle[3] + 2
        ]

    def _show_page_menu(self, page_widget: _PageWidget, position: QPointF, global_position) -> None:
        if not self._document_service.is_open:
            return
        page_number = page_widget.page_number
        menu = QMenu(self)

        if self._selected_words and self._selection_page == page_number:
            menu.addAction("Copy Selected Text").triggered.connect(
                lambda _checked=False: self.copy_selection()
            )
            for color_name in HIGHLIGHT_COLORS:
                menu.addAction(f"Highlight in {color_name.capitalize()}").triggered.connect(
                    lambda _checked=False, name=color_name: self.highlight_selection(name)
                )

        annotations = self._annotations_at(page_number, self._to_page_point(position))
        if annotations and not menu.isEmpty():
            menu.addSeparator()
        for annotation in annotations:
            label = _ANNOTATION_LABELS[annotation.annotation_type]
            if annotation.note_text:
                menu.addAction(f"View {label}…").triggered.connect(
                    lambda _checked=False, text=annotation.note_text, title=label:
                    QMessageBox.information(self, title, text)
                )
            menu.addAction(f"Delete {label}").triggered.connect(
                lambda _checked=False, xref=annotation.xref: self._delete_annotation(
                    page_number, xref
                )
            )

        if not menu.isEmpty():
            menu.exec(global_position)

    def _delete_annotation(self, page_number: int, xref: int) -> None:
        try:
            self._document_service.delete_annotation(page_number, xref)
        except PdfDocumentError as error:
            self.status_message.emit(str(error))
            return
        self._after_edit(page_number, "Annotation removed. Click Save & Replace PDF to keep the change.")

    # ------------------------------------------------------- pages and saving

    def delete_current_page(self) -> None:
        if not self._document_service.is_open:
            return
        page_number = self._current_page
        if self._document_service.page_count <= 1:
            QMessageBox.information(self, "Delete Page", "A PDF must keep at least one page.")
            return
        answer = QMessageBox.question(
            self,
            "Delete Page",
            f"Delete page {page_number + 1} of {self._document_service.page_count}?\n\n"
            "The page is removed from the file when you click Save & Replace PDF.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self._document_service.delete_page(page_number)
        except PdfDocumentError as error:
            self.status_message.emit(str(error))
            return
        remaining_pages = self._document_service.page_count
        self._current_page = min(page_number, remaining_pages - 1)
        self._reload_pages_keeping_position((self._current_page, 0.0))
        self.annotations_changed.emit()
        self.status_message.emit(
            f"Page {page_number + 1} deleted. Click Save & Replace PDF to keep the change."
        )

    def save_document(self) -> bool:
        """Write the edits into the PDF file. Returns True if the file was saved.

        The view returns to the same page and scroll position afterwards.
        """
        if not self.has_unsaved_changes:
            return True
        position = self.view_position()
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            result = self._document_service.save_and_replace()
        except PdfSaveError as error:
            QApplication.restoreOverrideCursor()
            self._update_controls()
            self.status_message.emit("The PDF was not saved.")
            QMessageBox.warning(self, "Save & Replace PDF", str(error))
            return False
        QApplication.restoreOverrideCursor()
        self._reload_pages_keeping_position(position)
        self.document_saved.emit(result)
        self.annotations_changed.emit()
        return True
