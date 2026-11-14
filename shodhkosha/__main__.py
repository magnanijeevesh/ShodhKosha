"""Start ShodhKosha. Run from the project folder with:  python -m shodhkosha"""

from __future__ import annotations

import sys
import traceback

from PyQt6.QtWidgets import QApplication, QMessageBox

from shodhkosha.library_database import LibraryDatabase
from shodhkosha.library_storage import LibraryStorage, default_library_directory
from shodhkosha.main_window import APPLICATION_NAME, MainWindow
from shodhkosha.theme import LIGHT_THEME, apply_theme


def _report_unexpected_error(exception_type, exception, exception_traceback) -> None:
    """Show unexpected errors in a dialog instead of closing the application."""
    details = "".join(traceback.format_exception(exception_type, exception, exception_traceback))
    if sys.stderr is not None:  # there is no console in the packaged .exe
        sys.stderr.write(details)
    message_box = QMessageBox(QMessageBox.Icon.Critical, APPLICATION_NAME,
                              f"Something went wrong:\n\n{exception}")
    message_box.setDetailedText(details)
    message_box.exec()


def main() -> int:
    application = QApplication(sys.argv)
    application.setApplicationName(APPLICATION_NAME)
    application.setOrganizationName(APPLICATION_NAME)
    sys.excepthook = _report_unexpected_error

    storage = LibraryStorage(default_library_directory())
    database = LibraryDatabase(storage.database_path)
    apply_theme(application, database.get_setting("theme", LIGHT_THEME))

    window = MainWindow(database, storage)
    window.show()
    exit_code = application.exec()
    database.close()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
