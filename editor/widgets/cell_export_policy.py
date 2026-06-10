"""User-toggleable gate for cell-mode export/import on shared-tile sprites.

Cell-mode IO assumes each OAM owns a unique slice of source tile bytes.
Sprites that reuse tile data across OAMs (icons / portraits often do this
for memory efficiency) violate that assumption — see
:func:`cell_png_io.detect_oam_tile_conflicts`. The default policy is to
disable the cell-mode export/import buttons for those sprites so users
don't get burned by silently non-round-trippable edits. This module
holds the override state plus a tiny callback registry so open browsers
can react when the user flips the menu setting.

Persistence routes through ``editor.prefs.prefs()`` — same .ini that
other view-state settings use, so the override survives restarts.
"""
from __future__ import annotations

from typing import Callable, List

from editor.prefs import prefs


_SETTINGS_KEY = "cells/allow_shared_tile_exports"
_callbacks: List[Callable[[bool], None]] = []


def allow_shared_tile_exports() -> bool:
    """Current policy: True if shared-tile sprites are exportable as cells."""
    return bool(prefs().value(_SETTINGS_KEY, False, type=bool))


def set_allow_shared_tile_exports(value: bool) -> None:
    """Persist the new policy and notify registered listeners.

    Listener invocations that raise ``RuntimeError`` (Qt-deleted widgets
    underneath a bound method) are pruned in place — same self-cleaning
    pattern :mod:`form_helpers` uses for its unknown-fields registry, so
    closed browsers don't accumulate stale callbacks across sessions.
    """
    prefs().setValue(_SETTINGS_KEY, bool(value))
    alive: List[Callable[[bool], None]] = []
    for cb in _callbacks:
        try:
            cb(bool(value))
        except RuntimeError:
            continue
        alive.append(cb)
    _callbacks[:] = alive


def register_change_callback(cb: Callable[[bool], None]) -> None:
    """Register a listener fired after every policy change.

    Listeners are responsible for being idempotent — the menu can fire
    the same value twice if the user clicks the action without changing
    state (rare, but Qt allows it via keyboard accelerators).
    """
    _callbacks.append(cb)
