"""Process-wide launch flags parsed from the command line at startup.

These are per-run toggles, not user preferences — set once from ``sys.argv`` in
``main()`` and never persisted (unlike :mod:`editor.prefs`, which is QSettings-
backed). ``--admin`` unlocks power-user tools that are easy to misuse on the
wrong sprite, e.g. the lossy "Compress OAM (fit ≤512)" trim, which drops real
edge pixels to force a dense sprite under the party-viewer cap.
"""
from __future__ import annotations

_admin = False


def set_admin(enabled: bool) -> None:
    """Enable/disable admin mode. Call once at startup (``--admin`` on argv)."""
    global _admin
    _admin = bool(enabled)


def is_admin() -> bool:
    """True when the editor was launched with ``--admin``."""
    return _admin
