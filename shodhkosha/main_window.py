"""The ShodhKosha main window: sidebar, reference list, PDF viewer and inspector."""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

from PyQt6.QtCore import (
    QAbstractTableModel,
    QMimeData,
    QModelIndex,
    QSettings,
    QSortFilterProxyModel,
    Qt,
    QTimer,
    QUrl,
)
from PyQt6.QtGui import QAction, QDesktopServices, QKeySequence
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFileDialog,
    QFrame,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressDialog,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QTableView,
    QTabWidget,
    QToolBar,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from shodhkosha.ai_analysis_worker import MODEL_SETTING_KEY, AiAnalysisQueue
from shodhkosha.bibtex_formatter import export_bibtex, format_bibtex_entry
from shodhkosha.document_panels import AnnotationsPanel, PageHitsPanel
from shodhkosha.doi_lookup import title_matches_text
from shodhkosha.doi_lookup_worker import DoiLookupQueue
from shodhkosha.library_database import (
    DuplicateNameError,
    LibraryDatabase,
    Paper,
    PaperFilter,
    RecordNotFoundError,
)
from shodhkosha.library_storage import (
    LibraryStorage,
    LibraryStorageError,
    collect_pdf_paths,
    tidy_file_stem,
)
from shodhkosha.metadata_inspector_widget import MetadataInspectorWidget
from shodhkosha.paper_analysis import DEFAULT_MODEL_NAME
from shodhkosha.pdf_document_service import PdfDocumentError, PdfDocumentService
from shodhkosha.pdf_viewer_widget import PdfViewerWidget
from shodhkosha.study_cards_widget import StudyCardsWidget
from shodhkosha.text_index_queue import TextIndexQueue
from shodhkosha.theme import DARK_THEME, LIGHT_THEME, apply_theme, normalize_theme_name

APPLICATION_NAME = "ShodhKosha"

_FILTER_ALL = "all"
_FILTER_UNFILED = "unfiled"
_FILTER_FOLDER = "folder"
_FILTER_TAG = "tag"
_SIDEBAR_HEADING = "heading"

_FILTER_ROLE = Qt.ItemDataRole.UserRole
_SORT_ROLE = Qt.ItemDataRole.UserRole
_MIRROR_SETTING_KEY = "mirror_folders_on_disk"
_AUTOMATIC_LOOKUP_SETTING_KEY = "automatic_doi_lookup"
_DETAILS_TAB, _HIGHLIGHTS_TAB, _SEARCH_HITS_TAB, _STUDY_CARDS_TAB = range(4)
_SEARCH_DELAY_MILLISECONDS = 150
_MAXIMUM_AUTHORS_SHOWN = 3


@dataclass
class _ImportTally:
    """Running totals for one import, which may take two passes."""

    new_papers: list = field(default_factory=list)
    similar_paths: list = field(default_factory=list)
    failures: list = field(default_factory=list)
    duplicate_count: int = 0
    last_paper_id: Optional[int] = None


def _format_authors(authors: Sequence[str]) -> str:
    if len(authors) > _MAXIMUM_AUTHORS_SHOWN:
        return f"{authors[0]} et al."
    return ", ".join(authors)


class PaperTableModel(QAbstractTableModel):
    """Read-only table of papers for the reference list."""

    COLUMN_TITLES = ("Title", "Authors", "Year", "Folder", "Tags")
    TITLE_COLUMN, AUTHORS_COLUMN, YEAR_COLUMN, FOLDER_COLUMN, TAGS_COLUMN = range(5)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._papers: list[Paper] = []

    def set_papers(self, papers: Sequence[Paper]) -> None:
        self.beginResetModel()
        self._papers = list(papers)
        self.endResetModel()

    def paper_at(self, row: int) -> Optional[Paper]:
        return self._papers[row] if 0 <= row < len(self._papers) else None

    def all_papers(self) -> list[Paper]:
        return list(self._papers)

    def row_of_paper(self, paper_id: int) -> int:
        for row, paper in enumerate(self._papers):
            if paper.paper_id == paper_id:
                return row
        return -1

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._papers)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.COLUMN_TITLES)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.COLUMN_TITLES[section]
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        paper = self._papers[index.row()]
        column = index.column()

        if role == Qt.ItemDataRole.DisplayRole:
            if column == self.TITLE_COLUMN:
                return paper.title or Path(paper.file_path).stem
            if column == self.AUTHORS_COLUMN:
                return _format_authors(paper.authors)
            if column == self.YEAR_COLUMN:
                return str(paper.publication_year) if paper.publication_year else ""
            if column == self.FOLDER_COLUMN:
                return paper.folder_name or ""
            if column == self.TAGS_COLUMN:
                return ", ".join(paper.tags)
        elif role == _SORT_ROLE:
            if column == self.YEAR_COLUMN:
                return paper.publication_year or 0
            return str(self.data(index, Qt.ItemDataRole.DisplayRole) or "").casefold()
        elif role == Qt.ItemDataRole.ToolTipRole:
            if column == self.TITLE_COLUMN:
                return paper.summary or paper.title
            if column == self.AUTHORS_COLUMN:
                return "; ".join(paper.authors)
        elif role == Qt.ItemDataRole.TextAlignmentRole and column == self.YEAR_COLUMN:
            return Qt.AlignmentFlag.AlignCenter
        return None


class MainWindow(QMainWindow):
    def __init__(self, database: LibraryDatabase, storage: LibraryStorage) -> None:
        super().__init__()
        self._database = database
        self._storage = storage
        self._active_filter: tuple[str, Optional[int]] = (_FILTER_ALL, None)
        self._displayed_paper_id: Optional[int] = None
        self._is_refreshing_list = False

        self.setWindowTitle(APPLICATION_NAME)
        self.setAcceptDrops(True)
        self.resize(1500, 900)

        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(_SEARCH_DELAY_MILLISECONDS)
        self._search_timer.timeout.connect(self._refresh_paper_list)

        self._build_actions()
        self._build_toolbar()
        self._build_central_panes()
        self._build_status_bar()
        self._restore_window_state()

        self._ai_queue = AiAnalysisQueue(self._database, self)
        self._ai_queue.indicator_changed.connect(self._ai_indicator_label.setText)
        self._ai_queue.message_reported.connect(self._show_status)
        self._ai_queue.paper_analyzed.connect(self._on_paper_analyzed)

        self._text_index_queue = TextIndexQueue(self._database, self)
        self._text_index_queue.indicator_changed.connect(self._index_indicator_label.setText)
        self._text_index_queue.message_reported.connect(self._show_status)
        self._text_index_queue.paper_indexed.connect(self._on_paper_indexed)

        self._doi_queue = DoiLookupQueue(self)
        self._doi_queue.lookup_succeeded.connect(self._on_automatic_lookup_succeeded)
        self._doi_queue.message_reported.connect(self._show_status)

        self._refresh_sidebar()
        self._refresh_paper_list()
        self._update_document_panels()
        # Papers added before full-text search existed are indexed now.
        self._text_index_queue.enqueue(self._database.list_unindexed_paper_ids())

    # ------------------------------------------------------------- construction

    def _build_actions(self) -> None:
        self._import_action = QAction("Import PDFs…", self)
        self._import_action.setShortcut(QKeySequence.StandardKey.Open)
        self._import_action.setToolTip("Add PDF files to the library (Ctrl+O)")
        self._import_action.triggered.connect(self._choose_files_to_import)

        self._import_folder_action = QAction("Import Folder\u2026", self)
        self._import_folder_action.setToolTip(
            "Add every PDF in a folder and its subfolders to the library"
        )
        self._import_folder_action.triggered.connect(
            lambda _checked=False: self._choose_folder_to_import()
        )

        self._tidy_names_action = QAction("Rename Files to Author_Year_Title", self)
        self._tidy_names_action.triggered.connect(lambda _checked=False: self._tidy_file_names())

        self._automatic_lookup_action = QAction("Look Up DOIs Automatically (Uses Internet)", self)
        self._automatic_lookup_action.setCheckable(True)
        self._automatic_lookup_action.setChecked(self._is_automatic_lookup_enabled())
        self._automatic_lookup_action.toggled.connect(self._set_automatic_lookup_enabled)

        self._new_folder_action = QAction("New Folder", self)
        self._new_folder_action.triggered.connect(self._create_folder)

        self._focus_search_action = QAction("Search", self)
        self._focus_search_action.setShortcut(QKeySequence.StandardKey.Find)
        self._focus_search_action.triggered.connect(self._focus_search_field)
        self.addAction(self._focus_search_action)

        self._analyze_selected_action = QAction("Re-analyze Selected with AI", self)
        self._analyze_selected_action.triggered.connect(
            lambda _checked=False: self._analyze_selected_paper()
        )
        self._analyze_missing_action = QAction("Analyze All Papers Without a Summary", self)
        self._analyze_missing_action.triggered.connect(
            lambda _checked=False: self._analyze_papers_without_summary()
        )
        self._choose_model_action = QAction("Choose AI Model…", self)
        self._choose_model_action.triggered.connect(lambda _checked=False: self._choose_ai_model())

        self._export_bibtex_action = QAction("Export Listed References to BibTeX…", self)
        self._export_bibtex_action.triggered.connect(lambda _checked=False: self._export_bibtex())

        self._copy_bibtex_action = QAction("Copy BibTeX", self)
        self._copy_bibtex_action.setShortcut(QKeySequence("Ctrl+Shift+C"))
        self._copy_bibtex_action.setToolTip(
            "Copy the selected reference as a BibTeX entry (Ctrl+Shift+C)"
        )
        self._copy_bibtex_action.triggered.connect(lambda _checked=False: self._copy_bibtex())

        self._mirror_action = QAction("Mirror Folders on Disk", self)
        self._mirror_action.setCheckable(True)
        self._mirror_action.setChecked(self._is_mirroring_enabled())
        self._mirror_action.toggled.connect(self._set_mirroring_enabled)

        self._dark_mode_action = QAction("Dark Mode", self)
        self._dark_mode_action.setCheckable(True)
        self._dark_mode_action.setChecked(
            normalize_theme_name(self._database.get_setting("theme", LIGHT_THEME)) == DARK_THEME
        )
        self._dark_mode_action.toggled.connect(self._set_dark_mode)

    def _build_toolbar(self) -> None:
        toolbar = QToolBar("Main Toolbar", self)
        toolbar.setObjectName("mainToolbar")
        toolbar.setMovable(False)
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        toolbar.addAction(self._import_action)
        toolbar.addAction(self._import_folder_action)
        toolbar.addAction(self._new_folder_action)

        toolbar.addAction(self._copy_bibtex_action)

        share_button = QToolButton(toolbar)
        share_button.setText("Share PDF")
        share_button.setToolTip("Copy the selected reference's PDF, or save a copy of it")
        share_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        share_menu = QMenu(share_button)
        self._add_share_actions(share_menu)
        share_button.setMenu(share_menu)
        toolbar.addWidget(share_button)

        tools_button = QToolButton(toolbar)
        tools_button.setText("Tools")
        tools_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        tools_menu = QMenu(tools_button)
        tools_menu.addAction(self._analyze_selected_action)
        tools_menu.addAction(self._analyze_missing_action)
        tools_menu.addAction(self._choose_model_action)
        tools_menu.addSeparator()
        tools_menu.addAction(self._export_bibtex_action)
        tools_menu.addSeparator()
        tools_menu.addAction(self._automatic_lookup_action)
        tools_menu.addAction(self._tidy_names_action)
        tools_menu.addAction(self._mirror_action)
        tools_button.setMenu(tools_menu)
        toolbar.addWidget(tools_button)

        spacer = QWidget(toolbar)
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        toolbar.addWidget(spacer)
        toolbar.addAction(self._dark_mode_action)
        self.addToolBar(toolbar)

    def _build_central_panes(self) -> None:
        self._main_splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self._main_splitter.setObjectName("mainSplitter")
        self._main_splitter.setHandleWidth(1)
        self._main_splitter.setChildrenCollapsible(False)
        self._main_splitter.addWidget(self._build_sidebar_pane())
        self._main_splitter.addWidget(self._build_reference_pane())
        self._main_splitter.addWidget(self._build_document_pane())
        self._main_splitter.setStretchFactor(0, 0)
        self._main_splitter.setStretchFactor(1, 2)
        self._main_splitter.setStretchFactor(2, 3)
        self._main_splitter.setSizes([220, 640, 640])
        self.setCentralWidget(self._main_splitter)

    def _build_sidebar_pane(self) -> QWidget:
        self._sidebar = QTreeWidget(self)
        self._sidebar.setColumnCount(2)
        self._sidebar.setHeaderHidden(True)
        self._sidebar.setRootIsDecorated(False)
        self._sidebar.setIndentation(14)
        self._sidebar.setMinimumWidth(180)
        self._sidebar.header().setStretchLastSection(False)
        self._sidebar.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._sidebar.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self._sidebar.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._sidebar.customContextMenuRequested.connect(self._show_sidebar_menu)
        self._sidebar.currentItemChanged.connect(self._on_sidebar_selection_changed)
        return self._sidebar

    def _build_reference_pane(self) -> QWidget:
        self._search_field = QLineEdit(self)
        self._search_field.setObjectName("searchField")
        self._search_field.setPlaceholderText(
            "Search title, author, year, folder, tag or highlighted text"
        )
        self._search_field.setClearButtonEnabled(True)
        self._search_field.textChanged.connect(lambda _text: self._search_timer.start())

        self._paper_model = PaperTableModel(self)
        self._paper_proxy = QSortFilterProxyModel(self)
        self._paper_proxy.setSourceModel(self._paper_model)
        self._paper_proxy.setSortRole(_SORT_ROLE)

        self._paper_table = QTableView(self)
        self._paper_table.setModel(self._paper_proxy)
        self._paper_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._paper_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._paper_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._paper_table.setAlternatingRowColors(True)
        self._paper_table.setShowGrid(False)
        self._paper_table.setWordWrap(False)
        self._paper_table.verticalHeader().setVisible(False)
        self._paper_table.verticalHeader().setDefaultSectionSize(30)
        self._paper_table.setSortingEnabled(True)
        self._paper_table.sortByColumn(-1, Qt.SortOrder.AscendingOrder)  # newest first
        header = self._paper_table.horizontalHeader()
        header.setHighlightSections(False)
        header.setSectionResizeMode(PaperTableModel.TITLE_COLUMN, QHeaderView.ResizeMode.Stretch)
        self._paper_table.setColumnWidth(PaperTableModel.AUTHORS_COLUMN, 130)
        self._paper_table.setColumnWidth(PaperTableModel.YEAR_COLUMN, 52)
        self._paper_table.setColumnWidth(PaperTableModel.FOLDER_COLUMN, 95)
        self._paper_table.setColumnWidth(PaperTableModel.TAGS_COLUMN, 100)
        self._paper_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._paper_table.customContextMenuRequested.connect(self._show_paper_menu)
        self._paper_table.selectionModel().selectionChanged.connect(
            self._on_paper_selection_changed
        )

        self._empty_list_label = QLabel(self)
        self._empty_list_label.setObjectName("emptyStateLabel")
        self._empty_list_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_list_label.setWordWrap(True)

        self._list_stack = QStackedWidget(self)
        self._list_stack.addWidget(self._paper_table)
        self._list_stack.addWidget(self._empty_list_label)

        pane = QWidget(self)
        pane.setMinimumWidth(460)
        layout = QVBoxLayout(pane)
        layout.setContentsMargins(10, 10, 10, 0)
        layout.setSpacing(8)
        layout.addWidget(self._search_field)
        layout.addWidget(self._list_stack, 1)
        return pane

    def _build_document_pane(self) -> QWidget:
        self._viewer = PdfViewerWidget(self)
        self._viewer.status_message.connect(self._show_status)
        self._viewer.document_saved.connect(self._on_document_saved)
        self._viewer.annotations_changed.connect(self._update_annotations_panel)
        self._viewer.page_changed.connect(self._on_viewer_page_changed)
        self._viewer.page_text_fallback = self._stored_page_text
        self._viewer.set_dark_pages(self._dark_mode_action.isChecked())
        self._inspector = MetadataInspectorWidget(self._database, self)
        self._inspector.paper_saved.connect(self._on_paper_details_saved)
        self._inspector.status_message.connect(self._show_status)

        inspector_scroll_area = QScrollArea(self)
        inspector_scroll_area.setWidgetResizable(True)
        inspector_scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        inspector_scroll_area.setWidget(self._inspector)

        self._annotations_panel = AnnotationsPanel(self)
        self._annotations_panel.page_requested.connect(self._viewer.go_to_page)
        self._annotations_panel.status_message.connect(self._show_status)
        self._page_hits_panel = PageHitsPanel(self)
        self._page_hits_panel.hit_requested.connect(self._show_page_hit)
        self._study_cards_panel = StudyCardsWidget(self._database, self._viewer.page_text, self)
        self._study_cards_panel.page_requested.connect(self._viewer.go_to_page)
        self._study_cards_panel.status_message.connect(self._on_study_cards_message)

        self._document_tabs = QTabWidget(self)
        self._document_tabs.setDocumentMode(True)
        self._document_tabs.addTab(inspector_scroll_area, "Details")
        self._document_tabs.addTab(self._annotations_panel, "Highlights")
        self._document_tabs.addTab(self._page_hits_panel, "Search Hits")
        self._document_tabs.addTab(self._study_cards_panel, "Study Cards")

        self._document_splitter = QSplitter(Qt.Orientation.Vertical, self)
        self._document_splitter.setObjectName("documentSplitter")
        self._document_splitter.setHandleWidth(1)
        self._document_splitter.addWidget(self._viewer)
        self._document_splitter.addWidget(self._document_tabs)
        self._document_splitter.setStretchFactor(0, 3)
        self._document_splitter.setStretchFactor(1, 1)
        self._document_splitter.setSizes([560, 340])
        self._document_splitter.setMinimumWidth(380)
        return self._document_splitter

    def _build_status_bar(self) -> None:
        self._index_indicator_label = QLabel(self)
        self._ai_indicator_label = QLabel(self)
        self._reference_count_label = QLabel(self)
        self.statusBar().addPermanentWidget(self._index_indicator_label)
        self.statusBar().addPermanentWidget(self._ai_indicator_label)
        self.statusBar().addPermanentWidget(self._reference_count_label)
        self.statusBar().setSizeGripEnabled(False)

    # ------------------------------------------------------------- window state

    def _restore_window_state(self) -> None:
        settings = QSettings()
        for key, restore in (
            ("window/geometry", self.restoreGeometry),
            ("window/main_splitter_v2", self._main_splitter.restoreState),
            ("window/document_splitter", self._document_splitter.restoreState),
        ):
            value = settings.value(key)
            if value is not None:
                restore(value)

    def closeEvent(self, event) -> None:  # noqa: N802
        if not self._resolve_unsaved_changes():
            event.ignore()
            return
        settings = QSettings()
        settings.setValue("window/geometry", self.saveGeometry())
        settings.setValue("window/main_splitter_v2", self._main_splitter.saveState())
        settings.setValue("window/document_splitter", self._document_splitter.saveState())
        self._ai_queue.finish_background_work()
        self._text_index_queue.finish_background_work()
        self._doi_queue.finish_background_work()
        self._study_cards_panel.finish_background_work()
        self._inspector.finish_background_work()
        self._viewer.close_document()
        super().closeEvent(event)

    def _set_dark_mode(self, enabled: bool) -> None:
        theme_name = DARK_THEME if enabled else LIGHT_THEME
        self._database.set_setting("theme", theme_name)
        apply_theme(QApplication.instance(), theme_name)
        self._viewer.set_dark_pages(enabled)

    def _show_status(self, message: str, milliseconds: int = 6000) -> None:
        self.statusBar().showMessage(message, milliseconds)

    # ------------------------------------------------------------------ sidebar

    def _refresh_sidebar(self) -> None:
        """Rebuild the folder and tag tree, keeping the current filter selected."""
        self._sidebar.blockSignals(True)
        self._sidebar.clear()

        def add_item(parent, text: str, count: Optional[int], kind: str, item_id=None):
            item = QTreeWidgetItem(parent, [text, "" if count is None else str(count)])
            item.setData(0, _FILTER_ROLE, (kind, item_id))
            item.setTextAlignment(
                1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            )
            if kind == _SIDEBAR_HEADING:
                item.setFlags(Qt.ItemFlag.ItemIsEnabled)
                heading_font = item.font(0)
                heading_font.setBold(True)
                item.setFont(0, heading_font)
            return item

        add_item(self._sidebar, "All References", self._database.count_papers(), _FILTER_ALL)
        add_item(self._sidebar, "Unfiled", self._database.count_unfiled_papers(), _FILTER_UNFILED)

        folders_heading = add_item(self._sidebar, "Folders", None, _SIDEBAR_HEADING)
        for folder in self._database.list_folders():
            add_item(folders_heading, folder.name, folder.paper_count, _FILTER_FOLDER,
                     folder.folder_id)

        tags_heading = add_item(self._sidebar, "Tags", None, _SIDEBAR_HEADING)
        for tag in self._database.list_tags():
            add_item(tags_heading, tag.name, tag.paper_count, _FILTER_TAG, tag.tag_id)

        self._sidebar.expandAll()

        selected_item = self._find_sidebar_item(self._active_filter)
        if selected_item is None:
            self._active_filter = (_FILTER_ALL, None)
            selected_item = self._sidebar.topLevelItem(0)
        self._sidebar.setCurrentItem(selected_item)
        self._sidebar.blockSignals(False)

    @staticmethod
    def _sidebar_item_key(item: QTreeWidgetItem) -> tuple[str, Optional[int]]:
        kind, item_id = item.data(0, _FILTER_ROLE)
        return kind, item_id

    def _find_sidebar_item(self, filter_key: tuple[str, Optional[int]]):
        def search(item: QTreeWidgetItem):
            if self._sidebar_item_key(item) == filter_key:
                return item
            for child_index in range(item.childCount()):
                found = search(item.child(child_index))
                if found is not None:
                    return found
            return None

        for index in range(self._sidebar.topLevelItemCount()):
            found = search(self._sidebar.topLevelItem(index))
            if found is not None:
                return found
        return None

    def _on_sidebar_selection_changed(self, current, _previous) -> None:
        if current is None:
            return
        kind, item_id = self._sidebar_item_key(current)
        if kind == _SIDEBAR_HEADING:
            return
        self._active_filter = (kind, item_id)
        self._refresh_paper_list()

    def _show_sidebar_menu(self, position) -> None:
        item = self._sidebar.itemAt(position)
        menu = QMenu(self)
        menu.addAction(self._new_folder_action)
        if item is not None:
            kind, item_id = self._sidebar_item_key(item)
            if kind in (_FILTER_FOLDER, _FILTER_TAG):
                noun = "Folder" if kind == _FILTER_FOLDER else "Tag"
                menu.addSeparator()
                rename_action = menu.addAction(f"Rename {noun}…")
                delete_action = menu.addAction(f"Delete {noun}…")
                rename_action.triggered.connect(
                    lambda: self._rename_sidebar_entry(kind, item_id, item.text(0))
                )
                delete_action.triggered.connect(
                    lambda: self._delete_sidebar_entry(kind, item_id, item.text(0))
                )
        menu.exec(self._sidebar.viewport().mapToGlobal(position))

    def _create_folder(self) -> None:
        name, accepted = QInputDialog.getText(self, "New Folder", "Folder name:")
        if not accepted or not name.strip():
            return
        try:
            self._database.create_folder(name)
        except DuplicateNameError as error:
            QMessageBox.information(self, APPLICATION_NAME, str(error))
            return
        self._refresh_everything()

    def _rename_sidebar_entry(self, kind: str, item_id: int, current_name: str) -> None:
        noun = "folder" if kind == _FILTER_FOLDER else "tag"
        name, accepted = QInputDialog.getText(
            self, f"Rename {noun.capitalize()}", f"New {noun} name:", text=current_name
        )
        if not accepted or not name.strip() or name.strip() == current_name:
            return
        try:
            if kind == _FILTER_FOLDER:
                self._database.rename_folder(item_id, name)
            else:
                self._database.rename_tag(item_id, name)
        except DuplicateNameError as error:
            QMessageBox.information(self, APPLICATION_NAME, str(error))
            return
        self._synchronize_file_locations()
        self._refresh_everything()

    def _delete_sidebar_entry(self, kind: str, item_id: int, name: str) -> None:
        if kind == _FILTER_FOLDER:
            question = (
                f"Delete the folder “{name}”?\n\n"
                "Its references stay in the library and become unfiled."
            )
        else:
            question = f"Delete the tag “{name}” from every reference?"
        answer = QMessageBox.question(
            self,
            APPLICATION_NAME,
            question,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        if kind == _FILTER_FOLDER:
            self._database.delete_folder(item_id)
        else:
            self._database.delete_tag(item_id)
        self._synchronize_file_locations()
        self._refresh_everything()

    # ----------------------------------------------------------- reference list

    def _current_filter(self) -> PaperFilter:
        kind, item_id = self._active_filter
        return PaperFilter(
            search_text=self._search_field.text(),
            folder_id=item_id if kind == _FILTER_FOLDER else None,
            unfiled_only=kind == _FILTER_UNFILED,
            tag_id=item_id if kind == _FILTER_TAG else None,
        )

    def _refresh_paper_list(
        self, select_paper_id: Optional[int] = None, reload_inspector: bool = True
    ) -> None:
        """Reload the list for the current sidebar filter and search text.

        Pass ``reload_inspector=False`` when the selected paper itself has not
        changed, so details the user is typing are not overwritten.
        """
        if select_paper_id is None:
            selected_paper = self._selected_paper()
            select_paper_id = selected_paper.paper_id if selected_paper else None

        papers = self._database.search_papers(self._current_filter())
        self._is_refreshing_list = True
        self._paper_model.set_papers(papers)

        if papers:
            self._list_stack.setCurrentWidget(self._paper_table)
        else:
            if self._database.count_papers() == 0:
                message = "Your library is empty.\n\nDrop PDF files here, or click Import PDFs."
            else:
                message = "No references match."
            self._empty_list_label.setText(message)
            self._list_stack.setCurrentWidget(self._empty_list_label)

        total = self._database.count_papers()
        shown = len(papers)
        noun = "reference" if total == 1 else "references"
        self._reference_count_label.setText(
            f"{total} {noun}" if shown == total else f"{shown} of {total} {noun}"
        )

        if select_paper_id is not None:
            source_row = self._paper_model.row_of_paper(select_paper_id)
            if source_row >= 0:
                proxy_index = self._paper_proxy.mapFromSource(self._paper_model.index(source_row, 0))
                self._paper_table.selectRow(proxy_index.row())
                self._paper_table.scrollTo(proxy_index)
        self._is_refreshing_list = False
        self._show_paper(self._selected_paper(), reload_inspector)
        self._update_page_hits()

    def _on_paper_selection_changed(self, *_: object) -> None:
        # While the list is being reloaded the selection is briefly empty;
        # reacting then would close and reopen the PDF being read.
        if not self._is_refreshing_list:
            self._show_paper(self._selected_paper())

    def _refresh_everything(
        self, select_paper_id: Optional[int] = None, reload_inspector: bool = True
    ) -> None:
        self._refresh_sidebar()
        self._refresh_paper_list(select_paper_id, reload_inspector)

    def _selected_paper(self) -> Optional[Paper]:
        selected_rows = self._paper_table.selectionModel().selectedRows()
        if not selected_rows:
            return None
        return self._paper_model.paper_at(self._paper_proxy.mapToSource(selected_rows[0]).row())

    def _focus_search_field(self) -> None:
        self._search_field.setFocus()
        self._search_field.selectAll()

    def _show_paper(self, paper: Optional[Paper], reload_inspector: bool = True) -> None:
        """Show a paper in the inspector and viewer (or clear both for ``None``)."""
        target_paper_id = paper.paper_id if paper is not None else None
        if target_paper_id != self._displayed_paper_id and not self._resolve_unsaved_changes():
            self._reselect_displayed_paper()
            return
        if reload_inspector or target_paper_id != self._displayed_paper_id:
            self._inspector.set_paper(paper)
        if paper is None:
            if self._displayed_paper_id is not None:
                self._viewer.close_document()
                self._displayed_paper_id = None
                self._update_document_panels()
            return
        if paper.paper_id == self._displayed_paper_id:
            return
        try:
            self._viewer.load_document(paper.file_path)
            self._displayed_paper_id = paper.paper_id
        except PdfDocumentError as error:
            self._viewer.close_document(f"This PDF could not be opened.\n\n{error}")
            self._displayed_paper_id = None
            self._show_status(str(error))
        self._update_document_panels()

    def _on_viewer_page_changed(self, page_number: int) -> None:
        self._study_cards_panel.set_current_page(page_number)

    def _on_study_cards_message(self, message: str) -> None:
        self._show_status(message)
        count = self._study_cards_panel.card_count
        self._document_tabs.setTabText(
            _STUDY_CARDS_TAB, f"Study Cards ({count})" if count else "Study Cards"
        )

    def _resolve_unsaved_changes(self) -> bool:
        """Ask what to do with unsaved PDF edits. Returns False if the user cancels."""
        if not self._viewer.has_unsaved_changes:
            return True
        message_box = QMessageBox(self)
        message_box.setIcon(QMessageBox.Icon.Question)
        message_box.setWindowTitle(APPLICATION_NAME)
        message_box.setText("Save the changes to this PDF?")
        message_box.setInformativeText(
            "Highlights, notes and page deletions that are not saved will be lost."
        )
        message_box.setStandardButtons(
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel
        )
        message_box.setDefaultButton(QMessageBox.StandardButton.Save)
        message_box.exec()
        clicked_button = message_box.clickedButton()
        if clicked_button is message_box.button(QMessageBox.StandardButton.Save):
            return self._viewer.save_document()
        return clicked_button is message_box.button(QMessageBox.StandardButton.Discard)

    def _reselect_displayed_paper(self) -> None:
        """Put the list selection back on the paper that is open in the viewer."""
        self._is_refreshing_list = True
        try:
            source_row = (
                self._paper_model.row_of_paper(self._displayed_paper_id)
                if self._displayed_paper_id is not None
                else -1
            )
            if source_row >= 0:
                proxy_index = self._paper_proxy.mapFromSource(self._paper_model.index(source_row, 0))
                self._paper_table.selectRow(proxy_index.row())
            else:
                self._paper_table.clearSelection()
        finally:
            self._is_refreshing_list = False

    def _on_document_saved(self, save_result) -> None:
        """Index the saved PDF's highlights and notes so search can find them."""
        if self._displayed_paper_id is None:
            return
        self._database.replace_annotations(self._displayed_paper_id, save_result.annotations)
        # Pages may have been deleted, so the full-text index is rebuilt.
        self._database.mark_text_indexed(self._displayed_paper_id, False)
        self._text_index_queue.enqueue([self._displayed_paper_id])
        count = len(save_result.annotations)
        self._show_status(
            f"PDF saved \u00b7 {count} annotation{'' if count == 1 else 's'} indexed for search."
        )

    def _on_paper_details_saved(self, paper_id: int) -> None:
        self._synchronize_file_locations()
        self._refresh_everything(select_paper_id=paper_id)
        self._show_status("Details saved.")

    def _show_paper_menu(self, position) -> None:
        index = self._paper_table.indexAt(position)
        if not index.isValid():
            return
        paper = self._paper_model.paper_at(self._paper_proxy.mapToSource(index).row())
        if paper is None:
            return
        menu = QMenu(self)
        self._add_share_actions(menu, paper)
        menu.addSeparator()
        open_action = menu.addAction("Open in Default PDF Program")
        show_action = menu.addAction("Show File Location")
        menu.addSeparator()
        menu.addAction("Copy BibTeX").triggered.connect(
            lambda _checked=False: self._copy_bibtex(paper)
        )
        analyze_action = menu.addAction("Re-analyze with AI")
        analyze_action.triggered.connect(
            lambda _checked=False: self._queue_for_analysis([paper.paper_id])
        )
        menu.addSeparator()
        remove_action = menu.addAction("Remove from Library…")
        open_action.triggered.connect(
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(paper.file_path))
        )
        show_action.triggered.connect(
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(paper.file_path).parent)))
        )
        remove_action.triggered.connect(lambda: self._remove_paper(paper))
        menu.exec(self._paper_table.viewport().mapToGlobal(position))

    def _remove_paper(self, paper: Paper) -> None:
        title = paper.title or Path(paper.file_path).stem
        answer = QMessageBox.question(
            self,
            APPLICATION_NAME,
            f"Remove “{title}” from the library?\n\n"
            "This deletes the library's copy of the PDF, including its highlights "
            "and notes. The file you originally imported is not affected.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        if self._displayed_paper_id == paper.paper_id:
            self._viewer.close_document()
            self._displayed_paper_id = None
        try:
            self._storage.remove_paper(paper, self._database)
        except LibraryStorageError as error:
            QMessageBox.warning(self, APPLICATION_NAME, str(error))
        self._paper_table.clearSelection()
        self._refresh_everything()

    # ------------------------------------------------------------------- import

    def _choose_files_to_import(self) -> None:
        file_paths, _selected_filter = QFileDialog.getOpenFileNames(
            self, "Import PDFs", "", "PDF files (*.pdf)"
        )
        if file_paths:
            self.import_files(file_paths)

    def _choose_folder_to_import(self) -> None:
        folder_path = QFileDialog.getExistingDirectory(self, "Import Folder")
        if folder_path:
            self.import_files([folder_path])

    def import_files(self, paths: Sequence[str]) -> None:
        """Import PDF files, and the PDFs inside any folders given.

        Reports how many were added, skipped or unreadable, asks before adding
        papers that look like ones already in the library, and queues the new
        papers for text indexing, AI analysis and DOI lookup.
        """
        pdf_paths = collect_pdf_paths(paths)
        if not pdf_paths:
            self._show_status("No PDF files were found there.")
            return

        tally = _ImportTally()
        self._import_batch(pdf_paths, tally, allow_similar=False)
        if tally.similar_paths:
            count = len(tally.similar_paths)
            names = "\n".join(path.name for path in tally.similar_paths[:8])
            if count > 8:
                names += f"\n\u2026 and {count - 8} more"
            answer = QMessageBox.question(
                self,
                APPLICATION_NAME,
                f"{count} file{'' if count == 1 else 's'} look like papers already in your "
                f"library (same DOI or title):\n\n{names}\n\nImport "
                f"{'it' if count == 1 else 'them'} anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            similar_paths, tally.similar_paths = tally.similar_paths, []
            if answer == QMessageBox.StandardButton.Yes:
                self._import_batch(similar_paths, tally, allow_similar=True)
            else:
                tally.duplicate_count += count

        imported_count = len(tally.new_papers)
        parts = [f"Imported {imported_count} reference{'' if imported_count == 1 else 's'}"]
        if tally.duplicate_count:
            parts.append(f"{tally.duplicate_count} already in the library")
        if tally.failures:
            parts.append(f"{len(tally.failures)} could not be read")
        self._show_status(" \u00b7 ".join(parts), 10000)

        if tally.last_paper_id is not None:
            # Show the whole library so the imported paper is visible and selected.
            self._active_filter = (_FILTER_ALL, None)
            self._search_field.blockSignals(True)
            self._search_field.clear()
            self._search_field.blockSignals(False)
            self._refresh_everything(select_paper_id=tally.last_paper_id)
        if tally.failures:
            QMessageBox.warning(
                self,
                APPLICATION_NAME,
                "Some files could not be imported:\n\n" + "\n".join(tally.failures[:10]),
            )
        if tally.new_papers:
            new_paper_ids = [paper.paper_id for paper in tally.new_papers]
            self._text_index_queue.enqueue(new_paper_ids)
            self._ai_queue.enqueue(new_paper_ids)
            if self._is_automatic_lookup_enabled():
                for paper in tally.new_papers:
                    self._doi_queue.enqueue(paper.paper_id, paper.doi)

    def _import_batch(
        self, pdf_paths: Sequence[Path], tally: "_ImportTally", allow_similar: bool
    ) -> None:
        progress = QProgressDialog("Importing PDFs\u2026", "Cancel", 0, len(pdf_paths), self)
        progress.setWindowTitle(APPLICATION_NAME)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(700)
        for index, pdf_path in enumerate(pdf_paths):
            progress.setValue(index)
            if progress.wasCanceled():
                break
            try:
                outcome = self._storage.import_pdf(pdf_path, self._database, allow_similar)
            except LibraryStorageError as error:
                tally.failures.append(str(error))
                continue
            if outcome.is_similar:
                tally.similar_paths.append(pdf_path)
                continue
            tally.last_paper_id = outcome.paper.paper_id
            if outcome.is_duplicate:
                tally.duplicate_count += 1
            else:
                tally.new_papers.append(outcome.paper)
        progress.setValue(len(pdf_paths))

    # ------------------------------------------------------ automatic DOI lookup

    def _is_automatic_lookup_enabled(self) -> bool:
        return self._database.get_setting(_AUTOMATIC_LOOKUP_SETTING_KEY, "1") == "1"

    def _set_automatic_lookup_enabled(self, enabled: bool) -> None:
        self._database.set_setting(_AUTOMATIC_LOOKUP_SETTING_KEY, "1" if enabled else "0")
        self._show_status(
            "New papers with a DOI will be looked up on Crossref."
            if enabled
            else "Automatic DOI lookup is off. Nothing is sent to the internet on import."
        )

    def _on_automatic_lookup_succeeded(self, paper_id: int, record) -> None:
        """Fill in a new paper's details from Crossref, if the record is really its own."""
        try:
            paper = self._database.get_paper(paper_id)
        except RecordNotFoundError:
            return
        if paper.metadata_verified:
            return
        opening_text = " ".join(self._database.get_page_texts(paper_id, [0, 1]).values())
        if not opening_text.strip():
            reader = PdfDocumentService()
            try:
                reader.open(paper.file_path)
                opening_text = " ".join(
                    reader.get_page_text(n) for n in range(min(2, reader.page_count))
                )
            except PdfDocumentError:
                return
            finally:
                reader.close()
        if not opening_text.strip():
            return  # a scan that has not been read yet; nothing to check against
        if not title_matches_text(record.title, opening_text):
            self._show_status(
                f"The DOI found in \u201c{paper.title}\u201d belongs to another work; "
                "its details were left for you to check."
            )
            return

        changes = {
            "title": record.title,
            "authors": record.authors,
            "publication_year": record.publication_year,
            "journal": record.journal,
            "volume": record.volume,
            "issue": record.issue,
            "pages": record.pages,
            "publisher": record.publisher,
            "doi": record.doi,
        }
        changes = {name: value for name, value in changes.items() if value}
        changes["metadata_verified"] = True
        self._database.update_paper_metadata(paper_id, **changes)
        self._tidy_file_names(quiet=True)
        selected_paper = self._selected_paper()
        is_selected = selected_paper is not None and selected_paper.paper_id == paper_id
        self._refresh_everything(reload_inspector=is_selected)
        self._show_status(f"Details confirmed from Crossref: \u201c{record.title}\u201d")

    # --------------------------------------------------------------- file names

    def _tidy_file_names(self, quiet: bool = False) -> None:
        """Rename library PDFs to Author_Year_Title.pdf where the details are known."""
        renamed_files = self._storage.tidy_file_names(self._database)
        if self._displayed_paper_id in renamed_files:
            self._viewer.document_service.set_file_path(renamed_files[self._displayed_paper_id])
        if quiet:
            return
        if renamed_files:
            self._refresh_paper_list(reload_inspector=False)
            count = len(renamed_files)
            self._show_status(f"Renamed {count} file{'' if count == 1 else 's'}.")
        else:
            self._show_status(
                "Nothing to rename. A file is renamed once its paper has an author, year and title."
            )

    # ------------------------------------------------------------ document panels

    def _displayed_paper(self) -> Optional[Paper]:
        if self._displayed_paper_id is None:
            return None
        try:
            return self._database.get_paper(self._displayed_paper_id)
        except RecordNotFoundError:
            return None

    def _stored_page_text(self, page_number: int) -> str:
        """Text read by OCR for a page of the open paper, if there is any."""
        if self._displayed_paper_id is None:
            return ""
        stored = self._database.get_page_texts(self._displayed_paper_id, [page_number])
        return stored.get(page_number, "")

    def _update_annotations_panel(self) -> None:
        paper = self._displayed_paper()
        document_service = self._viewer.document_service
        if paper is None or not document_service.is_open:
            self._annotations_panel.set_annotations("", [])
        else:
            self._annotations_panel.set_annotations(
                paper.title, document_service.extract_annotations()
            )
        count = self._annotations_panel.annotation_count
        self._document_tabs.setTabText(
            _HIGHLIGHTS_TAB, f"Highlights ({count})" if count else "Highlights"
        )

    def _update_page_hits(self) -> None:
        search_text = self._search_field.text()
        hits = []
        if self._displayed_paper_id is not None:
            hits = self._database.search_pages(self._displayed_paper_id, search_text)
        self._page_hits_panel.set_hits(hits, search_text)
        self._document_tabs.setTabText(
            _SEARCH_HITS_TAB, f"Search Hits ({len(hits)})" if hits else "Search Hits"
        )

    def _show_page_hit(self, page_number: int) -> None:
        words = [word.strip('"') for word in self._search_field.text().split()]
        self._viewer.show_text_matches(page_number, [word for word in words if word])

    def _update_study_cards_panel(self) -> None:
        document_service = self._viewer.document_service
        page_count = document_service.page_count if document_service.is_open else 0
        self._study_cards_panel.set_paper(self._displayed_paper(), page_count)
        count = self._study_cards_panel.card_count
        self._document_tabs.setTabText(
            _STUDY_CARDS_TAB, f"Study Cards ({count})" if count else "Study Cards"
        )

    def _update_document_panels(self) -> None:
        self._update_annotations_panel()
        self._update_page_hits()
        self._update_study_cards_panel()

    def _on_paper_indexed(self, paper_id: int, ocr_page_count: int) -> None:
        if paper_id == self._displayed_paper_id:
            self._update_page_hits()
        if ocr_page_count:
            # A scan has just become readable: let the AI catalogue it now.
            try:
                paper = self._database.get_paper(paper_id)
            except RecordNotFoundError:
                return
            if not paper.summary:
                self._ai_queue.enqueue([paper_id])

    # ------------------------------------------------------------------ sharing

    def _add_share_actions(self, menu: QMenu, paper: Optional[Paper] = None) -> None:
        """Without ``paper``, the entries act on the reference selected when clicked."""
        menu.addAction("Copy PDF File").triggered.connect(
            lambda _checked=False: self._copy_pdf_file(paper)
        )
        menu.addAction("Save a Copy As\u2026").triggered.connect(
            lambda _checked=False: self._save_pdf_copy(paper)
        )

    def _pdf_ready_to_share(self, paper: Optional[Paper]) -> Optional[Paper]:
        """The paper to share, once any unsaved edits to it have been dealt with.

        Sharing uses the file on disk, so edits still waiting in the viewer
        would be missing from the copy unless they are saved first.
        """
        paper = paper or self._selected_paper()
        if paper is None:
            self._show_status("Select a reference first.")
            return None
        if paper.paper_id == self._displayed_paper_id and self._viewer.has_unsaved_changes:
            message_box = QMessageBox(self)
            message_box.setIcon(QMessageBox.Icon.Question)
            message_box.setWindowTitle(APPLICATION_NAME)
            message_box.setText("This PDF has highlights or other changes that are not saved yet.")
            message_box.setInformativeText("Save them first so the copy includes them?")
            save_button = message_box.addButton("Save First", QMessageBox.ButtonRole.AcceptRole)
            without_button = message_box.addButton(
                "Share Without Them", QMessageBox.ButtonRole.DestructiveRole
            )
            message_box.addButton(QMessageBox.StandardButton.Cancel)
            message_box.exec()
            clicked_button = message_box.clickedButton()
            if clicked_button is save_button:
                if not self._viewer.save_document():
                    return None
            elif clicked_button is not without_button:
                return None
        # Saving or mirroring may have changed the record; read it again.
        try:
            paper = self._database.get_paper(paper.paper_id)
        except RecordNotFoundError:
            return None
        if not Path(paper.file_path).exists():
            QMessageBox.warning(
                self, APPLICATION_NAME, "The PDF file for this reference could not be found."
            )
            return None
        return paper

    def _copy_pdf_file(self, paper: Optional[Paper] = None) -> None:
        """Put the PDF on the clipboard as a file, ready to paste into an
        email, a chat or a folder."""
        paper = self._pdf_ready_to_share(paper)
        if paper is None:
            return
        mime_data = QMimeData()
        mime_data.setUrls([QUrl.fromLocalFile(paper.file_path)])
        QApplication.clipboard().setMimeData(mime_data)
        self._show_status("PDF copied. Paste it into an email, a chat or a folder (Ctrl+V).")

    def _save_pdf_copy(self, paper: Optional[Paper] = None) -> None:
        """Save a copy of the PDF, with its highlights and notes, wherever the user chooses."""
        paper = self._pdf_ready_to_share(paper)
        if paper is None:
            return
        source_path = Path(paper.file_path)
        suggested_name = (tidy_file_stem(paper) or source_path.stem) + ".pdf"
        file_path, _selected_filter = QFileDialog.getSaveFileName(
            self, "Save a Copy", suggested_name, "PDF files (*.pdf)"
        )
        if not file_path:
            return
        try:
            shutil.copy2(source_path, file_path)
        except shutil.SameFileError:
            self._show_status("That is the library's own copy; choose another location.")
            return
        except OSError as error:
            QMessageBox.warning(self, APPLICATION_NAME, f"Could not save the copy:\n\n{error}")
            return
        self._show_status(f"Copy saved as {Path(file_path).name}.")

    # ------------------------------------------------------------------- BibTeX

    def _copy_bibtex(self, paper: Optional[Paper] = None) -> None:
        """Copy the BibTeX entry of a paper (by default the selected one)."""
        paper = paper or self._selected_paper()
        if paper is None:
            self._show_status("Select a reference first.")
            return
        QApplication.clipboard().setText(format_bibtex_entry(paper))
        reminder = "" if paper.metadata_verified else " Its details are not marked as verified."
        self._show_status(f"BibTeX entry copied.{reminder}")

    def _export_bibtex(self) -> None:
        papers = self._paper_model.all_papers()
        if not papers:
            self._show_status("There are no references in the list to export.")
            return
        file_path, _selected_filter = QFileDialog.getSaveFileName(
            self, "Export BibTeX", "references.bib", "BibTeX files (*.bib)"
        )
        if not file_path:
            return
        try:
            Path(file_path).write_text(export_bibtex(papers), encoding="utf-8")
        except OSError as error:
            QMessageBox.warning(self, APPLICATION_NAME, f"Could not write the file:\n\n{error}")
            return
        count = len(papers)
        self._show_status(
            f"Exported {count} reference{'' if count == 1 else 's'} to {Path(file_path).name}."
        )

    # ---------------------------------------------------------------- AI filing

    def _queue_for_analysis(self, paper_ids: Sequence[int]) -> None:
        if not paper_ids:
            self._show_status("There is nothing to analyze.")
            return
        count = len(paper_ids)
        self._show_status(f"Queued {count} paper{'' if count == 1 else 's'} for AI analysis.")
        self._ai_queue.enqueue(paper_ids)

    def _analyze_selected_paper(self) -> None:
        paper = self._selected_paper()
        if paper is None:
            self._show_status("Select a reference first.")
            return
        self._queue_for_analysis([paper.paper_id])

    def _analyze_papers_without_summary(self) -> None:
        self._queue_for_analysis(
            [paper.paper_id for paper in self._database.search_papers() if not paper.summary]
        )

    def _choose_ai_model(self) -> None:
        current_model = self._database.get_setting(MODEL_SETTING_KEY, DEFAULT_MODEL_NAME)
        model_name, accepted = QInputDialog.getText(
            self,
            "AI Model",
            "Ollama model name (for example llama3.2 or qwen2.5:7b).\n"
            "The model must already be installed with “ollama pull”.",
            text=current_model,
        )
        if accepted and model_name.strip():
            self._database.set_setting(MODEL_SETTING_KEY, model_name.strip())
            self._show_status(f"AI model set to {model_name.strip()}.")

    def _on_paper_analyzed(self, paper_id: int) -> None:
        self._synchronize_file_locations()
        selected_paper = self._selected_paper()
        is_selected = selected_paper is not None and selected_paper.paper_id == paper_id
        self._refresh_everything(reload_inspector=is_selected)

    # ----------------------------------------------------------- folder mirroring

    def _is_mirroring_enabled(self) -> bool:
        return self._database.get_setting(_MIRROR_SETTING_KEY, "0") == "1"

    def _set_mirroring_enabled(self, enabled: bool) -> None:
        self._database.set_setting(_MIRROR_SETTING_KEY, "1" if enabled else "0")
        if enabled:
            self._synchronize_file_locations()
            self._refresh_paper_list(reload_inspector=False)
            self._show_status("PDF files are now arranged in folders on disk.")
        else:
            self._show_status("Folder mirroring is off. Files stay where they are.")

    def _synchronize_file_locations(self) -> None:
        """With mirroring on, move PDFs so the disk matches the library's folders."""
        if not self._is_mirroring_enabled():
            return
        outcome = self._storage.mirror_folders(self._database)
        if self._displayed_paper_id in outcome.moved_files:
            self._viewer.document_service.set_file_path(
                outcome.moved_files[self._displayed_paper_id]
            )
        if outcome.failed_file_names:
            count = len(outcome.failed_file_names)
            self._show_status(
                f"{count} file{'' if count == 1 else 's'} could not be moved "
                "(open in another program?). They will be moved next time."
            )

    @staticmethod
    def _import_paths_from_event(event) -> list[str]:
        """PDF files and folders being dragged onto the window."""
        mime_data = event.mimeData()
        if not mime_data.hasUrls():
            return []
        paths = [url.toLocalFile() for url in mime_data.urls() if url.isLocalFile()]
        return [path for path in paths if path.lower().endswith(".pdf") or Path(path).is_dir()]

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if self._import_paths_from_event(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if self._import_paths_from_event(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:  # noqa: N802
        paths = self._import_paths_from_event(event)
        if paths:
            event.acceptProposedAction()
            self.import_files(paths)
        else:
            event.ignore()
