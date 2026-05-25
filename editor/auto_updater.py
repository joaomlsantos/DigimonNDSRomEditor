"""Background check for new GitHub releases.

Ported from DWDDRandomizer's `ui/autoUpdater.py`. Two ports vs the original:
  * Storage moves from TOML (`skipped_update_version`) to `QSettings`, which
    is already where the editor keeps its other persistent preferences.
  * The dialog moves from Tk `messagebox` to `QMessageBox`. The cross-thread
    hand-off uses a `Signal(..., Qt.QueuedConnection)` so the GUI thread is
    the only one touching widgets.

The check itself runs on a daemon thread so a slow GitHub API response doesn't
block window construction.
"""
from __future__ import annotations

import json
import ssl
import threading
import urllib.request
import webbrowser
from typing import Optional, Tuple

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import QMessageBox, QWidget

from .prefs import prefs


GITHUB_REPO = "joaomlsantos/DigimonNDSRomEditor"
_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
_REQUEST_TIMEOUT_S = 5
SETTINGS_SKIPPED_VERSION = "updates/skipped_version"


def _parse_version(version_str: str) -> Tuple[int, ...]:
    """Parse `v0.1.0` / `0.1.0` into a comparable tuple of ints.

    Stops at the first non-numeric component so a future `0.1.0-rc1` still
    compares sensibly against the numeric prefix instead of crashing.
    """
    cleaned = version_str.lstrip("v")
    parts = []
    for piece in cleaned.split("."):
        try:
            parts.append(int(piece))
        except ValueError:
            break
    return tuple(parts) or (0,)


def _build_ssl_context() -> Optional[ssl.SSLContext]:
    """SSL context built from `certifi`'s CA bundle, if installed.

    Frozen PyInstaller builds on Windows can't rely on a system trust store
    being available, so the randomizer ships certifi and routes through it.
    In dev where certifi isn't installed we fall back to the system default
    (None) — works on any developer machine with a normal CA store.
    """
    try:
        import certifi  # type: ignore
    except ImportError:
        return None
    return ssl.create_default_context(cafile=certifi.where())


class UpdateChecker(QObject):
    """Background GitHub-release checker with a Qt-safe completion signal.

    Usage:
        checker = UpdateChecker(main_window)
        checker.start(EDITOR_VERSION)

    The instance must outlive the request — store a reference on the parent
    (passing `parent=main_window` already handles that).
    """

    # Emitted from the worker thread. The queued connection below marshals it
    # back to the GUI thread before `_on_update_available` runs.
    update_available = Signal(str, str)  # (latest_version, release_url)

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._parent = parent
        self.update_available.connect(self._on_update_available, Qt.QueuedConnection)

    def start(self, current_version: str) -> None:
        thread = threading.Thread(
            target=self._check,
            args=(current_version,),
            daemon=True,
        )
        thread.start()

    def _check(self, current_version: str) -> None:
        try:
            req = urllib.request.Request(_API_URL)
            with urllib.request.urlopen(
                req, timeout=_REQUEST_TIMEOUT_S, context=_build_ssl_context()
            ) as response:
                data = json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 — network can throw many types
            print(f"Update check failed: {exc}")
            return

        latest = str(data.get("tag_name", "")).lstrip("v")
        release_url = str(data.get("html_url", ""))
        if not latest or not release_url:
            return
        if _parse_version(latest) <= _parse_version(current_version):
            return
        if prefs().value(SETTINGS_SKIPPED_VERSION) == latest:
            return
        self.update_available.emit(latest, release_url)

    def _on_update_available(self, latest_version: str, release_url: str) -> None:
        # Always invoked on the GUI thread because of the queued connection.
        message = (
            f"A new version ({latest_version}) is available.\n\n"
            "Would you like to download it now?"
        )
        result = QMessageBox.question(
            self._parent,
            "Update Available",
            message,
            QMessageBox.Yes | QMessageBox.No,
        )
        if result == QMessageBox.Yes:
            webbrowser.open(release_url)
        else:
            # "No" is treated as "skip this version" — same behaviour as the
            # randomizer's TOML-backed equivalent. The user gets re-prompted
            # only when an even newer version ships.
            prefs().setValue(SETTINGS_SKIPPED_VERSION, latest_version)
