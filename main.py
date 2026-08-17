"""Entry point for the Digimon NDS ROM Editor."""
import os
import sys

# `digimon_core` sits next to `editor/` in this repo. Running `python main.py`
# already puts the project root on sys.path, but tools that import this file
# from elsewhere (PyInstaller, IDEs) won't — keep the explicit insert.
HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from PySide6.QtGui import QIcon  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from editor.main_window import MainWindow  # noqa: E402
from editor.prefs import configure_storage  # noqa: E402
from editor.runtime import set_admin  # noqa: E402


def _resource_path(relative: str) -> str:
    """Resolve a bundled asset path that works in dev and under PyInstaller.

    PyInstaller (both onefile and onedir) sets `sys._MEIPASS` to the directory
    where bundled `datas=` files were unpacked; in a normal `python main.py`
    run that attribute doesn't exist and we fall back to the project root.
    """
    base = getattr(sys, "_MEIPASS", HERE)
    return os.path.join(base, relative)


def main() -> int:
    # Admin mode unlocks power-user tools (see editor.runtime). Read it before
    # any widget is built; --admin is a plain flag Qt ignores, so it's safe to
    # leave in sys.argv passed to QApplication.
    set_admin("--admin" in sys.argv)
    # Resolve the ini location before any prefs() consumer runs.
    configure_storage()
    app = QApplication(sys.argv)
    # Prefs storage doesn't depend on these (editor.prefs constructs QSettings
    # with explicit org/app names), but Qt itself uses them for window-class
    # grouping on Linux and the AppUserModelID on Windows — keep them set.
    app.setOrganizationName("DigimonNDSRomEditor")
    app.setApplicationName("DigimonNDSRomEditor")
    # PyInstaller's `icon=` only sets the EXE's file icon (what Explorer shows
    # in the folder). The runtime window/taskbar icon has to come from Qt —
    # otherwise the title bar and taskbar fall back to the generic Qt icon.
    # .ico works on Windows; the same call no-ops cleanly if the file is
    # missing on platforms that ship a different format (Linux uses .png).
    for candidate in ("public/editor.ico", "public/editor.png"):
        path = _resource_path(candidate)
        if os.path.exists(path):
            app.setWindowIcon(QIcon(path))
            break
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
