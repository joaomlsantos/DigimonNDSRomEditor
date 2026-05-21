"""Entry point for the Digimon NDS ROM Editor."""
import os
import sys

# `digimon_core` sits next to `editor/` in this repo. Running `python main.py`
# already puts the project root on sys.path, but tools that import this file
# from elsewhere (PyInstaller, IDEs) won't — keep the explicit insert.
HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from PySide6.QtWidgets import QApplication  # noqa: E402

from editor.main_window import MainWindow  # noqa: E402


def main() -> int:
    app = QApplication(sys.argv)
    # QSettings keys off these — set before any QSettings() is constructed so
    # window-state and recent-files persistence have a consistent backing store.
    app.setOrganizationName("DigimonNDSRomEditor")
    app.setApplicationName("DigimonNDSRomEditor")
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
