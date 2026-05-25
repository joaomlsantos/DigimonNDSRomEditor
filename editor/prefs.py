"""Preferences storage — flat ini file with portable/appdata resolution.

All editor code obtains its `QSettings` instance through `prefs()`. The actual
ini location is resolved once at startup by `configure_storage()`:

* Frozen builds on Windows/Linux first try `<exe_dir>/DigimonNDSRomEditor.ini`
  — a true side-by-side flat file. This is what makes the "drop the zip
  on a USB stick" workflow self-contained: move the folder, the prefs
  follow.
* macOS is excluded from portable mode. Frozen builds there are `.app`
  bundles, and `sys.executable` points inside `Contents/MacOS/` — writing
  next to it would pollute the bundle and break the code signature, so
  Gatekeeper would reject it on next launch. macOS always takes the
  appdata fallback.
* If the install lives somewhere read-only (Program Files under standard
  UAC, mounted RO volume), we fall back to Qt's per-user IniFormat
  location (`%APPDATA%/DigimonNDSRomEditor/DigimonNDSRomEditor.ini` on
  Windows, `~/.config/...` on Linux, `~/Library/Preferences/...` on macOS).
* Dev runs (`python main.py`) always take the fallback path — otherwise
  every launch would dump an ini into the repo root.

Routing through this helper (instead of bare `QSettings()`) is what lets us
hand back a `QSettings(path, IniFormat)` for the portable case. Qt's
implicit constructor always nests under `<base>/<Org>/<App>.ini`, which
would put the portable ini in a subfolder next to the exe rather than
beside it.
"""
from __future__ import annotations

import os
import sys
from typing import Optional

from PySide6.QtCore import QSettings

_APP_NAME = "DigimonNDSRomEditor"
_portable_path: Optional[str] = None
_configured = False


def configure_storage() -> None:
    """Resolve the preferences location. Idempotent; call once at startup."""
    global _portable_path, _configured
    if _configured:
        return
    _configured = True
    QSettings.setDefaultFormat(QSettings.IniFormat)
    if not getattr(sys, "frozen", False):
        return
    if sys.platform == "darwin":
        # See module docstring: writing inside the .app bundle breaks the
        # code signature, so portable mode is Windows/Linux only.
        return
    candidate_dir = os.path.dirname(sys.executable)
    if not _is_writable(candidate_dir):
        return
    _portable_path = os.path.join(candidate_dir, f"{_APP_NAME}.ini")


def prefs() -> QSettings:
    """Return a fresh `QSettings` bound to the resolved ini location.

    A new instance per call matches Qt's expected usage pattern — the
    instance flushes on destruction, so short-lived calls like
    `prefs().setValue(k, v)` write to disk immediately when the temporary
    goes out of scope.
    """
    if not _configured:
        configure_storage()
    if _portable_path is not None:
        return QSettings(_portable_path, QSettings.IniFormat)
    return QSettings(QSettings.IniFormat, QSettings.UserScope, _APP_NAME, _APP_NAME)


def _is_writable(directory: str) -> bool:
    probe = os.path.join(directory, ".write_probe")
    try:
        with open(probe, "w"):
            pass
        os.remove(probe)
        return True
    except OSError:
        return False
