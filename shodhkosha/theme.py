"""Light and dark appearance for ShodhKosha."""

from __future__ import annotations

from string import Template

from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import QApplication

LIGHT_THEME = "light"
DARK_THEME = "dark"

_THEME_COLORS: dict[str, dict[str, str]] = {
    LIGHT_THEME: {
        "window": "#F4F5F7",
        "surface": "#FFFFFF",
        "alternate": "#F7F8FA",
        "hover": "#E9ECF1",
        "text": "#1F2328",
        "muted": "#667085",
        "border": "#D5D9E0",
        "accent": "#2F6FED",
        "accent_text": "#FFFFFF",
        "canvas": "#E2E5EA",
        "button": "#FFFFFF",
    },
    DARK_THEME: {
        "window": "#1E2025",
        "surface": "#25282E",
        "alternate": "#292C33",
        "hover": "#32363E",
        "text": "#E6E8EB",
        "muted": "#9AA3AF",
        "border": "#3A3F47",
        "accent": "#4C82F0",
        "accent_text": "#FFFFFF",
        "canvas": "#15171A",
        "button": "#2F333A",
    },
}

_STYLE_SHEET = Template(
    """
QMainWindow, QDialog { background: $window; }

QToolBar {
    background: $window; border: none; border-bottom: 1px solid $border;
    padding: 6px 8px; spacing: 6px;
}
QToolButton {
    padding: 5px 10px; border: 1px solid transparent; border-radius: 6px; color: $text;
}
QToolButton:hover { background: $hover; }
QToolButton:pressed, QToolButton:checked { background: $hover; border-color: $border; }
QToolButton:disabled { color: $muted; }

QPushButton {
    padding: 6px 16px; border: 1px solid $border; border-radius: 6px;
    background: $button; color: $text;
}
QPushButton:hover { border-color: $accent; }
QPushButton:disabled { color: $muted; border-color: $border; }
QPushButton#primaryButton {
    background: $accent; color: $accent_text; border-color: $accent; font-weight: 600;
}
QPushButton#primaryButton:disabled { background: $hover; color: $muted; border-color: $border; }

QLineEdit, QPlainTextEdit {
    padding: 5px 8px; border: 1px solid $border; border-radius: 6px;
    background: $surface; color: $text;
    selection-background-color: $accent; selection-color: $accent_text;
}
QLineEdit:focus, QPlainTextEdit:focus { border-color: $accent; }
QLineEdit:disabled, QPlainTextEdit:disabled { background: $window; color: $muted; }
QLineEdit#searchField { padding: 7px 10px; }

QTreeWidget, QTableView {
    background: $surface; alternate-background-color: $alternate;
    border: none; outline: 0; color: $text;
}
QTreeWidget::item { padding: 5px 2px; }
QTableView::item { padding: 4px 8px; }
QTreeWidget::item:hover, QTableView::item:hover { background: $hover; }
QTreeWidget::item:selected, QTableView::item:selected {
    background: $accent; color: $accent_text;
}
QHeaderView::section {
    background: $window; color: $muted; padding: 6px 8px; border: none;
    border-bottom: 1px solid $border; font-weight: 600;
}

QTabWidget::pane { border: none; border-top: 1px solid $border; }
QTabBar { background: $window; }
QTabBar::tab {
    padding: 7px 14px; color: $muted; background: transparent; border: none;
    border-bottom: 2px solid transparent;
}
QTabBar::tab:selected { color: $text; border-bottom: 2px solid $accent; }
QTabBar::tab:hover { color: $text; }

QListWidget {
    background: $surface; alternate-background-color: $alternate; color: $text;
    border: 1px solid $border; border-radius: 6px; outline: 0;
}
QListWidget::item { padding: 6px 8px; }
QListWidget::item:selected { background: $accent; color: $accent_text; }

QSplitter::handle { background: $border; }
QStatusBar { background: $window; color: $muted; border-top: 1px solid $border; }
QStatusBar QLabel { color: $muted; padding: 0 8px; }

QWidget#viewerToolbar { background: $window; border-bottom: 1px solid $border; }
QWidget#pdfViewport, QWidget#pdfCanvas { background: $canvas; }
QWidget#inspectorPanel { background: $window; }
QLabel#fieldLabel, QLabel#mutedLabel { color: $muted; }
QLabel#emptyStateLabel { color: $muted; font-size: 14px; }
QLabel#paneTitle { color: $muted; font-weight: 600; padding: 8px 10px 4px 10px; }
"""
)


def normalize_theme_name(theme_name: str) -> str:
    return DARK_THEME if theme_name == DARK_THEME else LIGHT_THEME


def apply_theme(application: QApplication, theme_name: str) -> None:
    """Apply the light or dark theme to the whole application."""
    colors = _THEME_COLORS[normalize_theme_name(theme_name)]
    application.setStyle("Fusion")

    palette = QPalette()
    role = QPalette.ColorRole
    palette.setColor(role.Window, QColor(colors["window"]))
    palette.setColor(role.WindowText, QColor(colors["text"]))
    palette.setColor(role.Base, QColor(colors["surface"]))
    palette.setColor(role.AlternateBase, QColor(colors["alternate"]))
    palette.setColor(role.Text, QColor(colors["text"]))
    palette.setColor(role.Button, QColor(colors["button"]))
    palette.setColor(role.ButtonText, QColor(colors["text"]))
    palette.setColor(role.ToolTipBase, QColor(colors["surface"]))
    palette.setColor(role.ToolTipText, QColor(colors["text"]))
    palette.setColor(role.PlaceholderText, QColor(colors["muted"]))
    palette.setColor(role.Highlight, QColor(colors["accent"]))
    palette.setColor(role.HighlightedText, QColor(colors["accent_text"]))
    palette.setColor(role.Link, QColor(colors["accent"]))
    for disabled_role in (role.WindowText, role.Text, role.ButtonText):
        palette.setColor(QPalette.ColorGroup.Disabled, disabled_role, QColor(colors["muted"]))
    application.setPalette(palette)

    application.setStyleSheet(_STYLE_SHEET.substitute(colors))
