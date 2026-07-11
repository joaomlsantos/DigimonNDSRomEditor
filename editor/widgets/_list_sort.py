"""Shared helpers for the columnar list panels.

Both :class:`DigimonListPanel` and :class:`RecordListPanel` render a
multi-column ``QTreeView`` in columnar mode and want the same:

* header-click sort semantics — the leading id column sorts numerically,
  everything else case-insensitively, and blank cells sink to the bottom
  so sorting by an optional column clusters the populated rows on top
  (:class:`ListSortProxy`);
* an inline row marker — a "● edited" dot (and, for the enemy list, a
  "▸ wild-encounter" triangle) drawn as a cell *decoration* on the id
  column rather than in a dedicated gutter column, so a pristine list
  shows no marker and no wasted left column at all
  (:func:`marker_icon`).
"""
from __future__ import annotations

from functools import lru_cache
from typing import Optional

from PySide6.QtCore import QPoint, QSortFilterProxyModel, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPalette, QPixmap, QPolygon
from PySide6.QtWidgets import QApplication


# "Modified" amber reads as a distinct accent on both light and dark
# themes without colliding with selection blue.
_DIRTY_COLOR = QColor(0xE0, 0x8A, 0x1E)
_MARK_ICON_W = 22
_MARK_ICON_H = 14


@lru_cache(maxsize=4)
def marker_icon(dirty: bool, marked: bool) -> QIcon:
    """Small icon for a row's ``(dirty, marked)`` state, cached.

    Drawn onto a fixed-size transparent pixmap so multi-glyph states
    (both dirty + marked, used only by the enemy list) line up. Callers
    set it as ``Qt.DecorationRole`` on the id-column item *only* when at
    least one flag is set, so clean/unmarked rows carry no decoration
    and the column keeps its natural left edge.
    """
    pm = QPixmap(_MARK_ICON_W, _MARK_ICON_H)
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setPen(Qt.NoPen)
    x = 1
    cy = _MARK_ICON_H // 2
    if dirty:
        painter.setBrush(_DIRTY_COLOR)
        painter.drawEllipse(QPoint(x + 3, cy), 3, 3)
        x += 10
    if marked:
        painter.setBrush(
            QApplication.palette().color(QPalette.Disabled, QPalette.WindowText)
        )
        painter.drawPolygon(QPolygon([
            QPoint(x, cy - 4), QPoint(x, cy + 4), QPoint(x + 6, cy),
        ]))
    painter.end()
    return QIcon(pm)


def _as_number(text: str) -> Optional[int]:
    """Parse ``text`` as an int (hex ``0x..`` or decimal), else ``None``.

    The id column of every columnar list is a short hex or decimal
    string (``0x041``, ``26``); parsing it means ``0x2`` sorts before
    ``0x10`` instead of lexicographically after it. Non-numeric cells
    fall through to the text comparison in :meth:`ListSortProxy.lessThan`.
    """
    s = text.strip()
    if not s:
        return None
    try:
        return int(s, 16) if s.lower().startswith("0x") else int(s)
    except ValueError:
        return None


# Explicit per-cell sort key, checked ahead of the visible text. Set it
# on columns whose display cell can't sort on its own — e.g. an icon-only
# column (the move list's Element icon) — so the header still sorts them.
SORT_KEY_ROLE = Qt.UserRole + 1


class ListSortProxy(QSortFilterProxyModel):
    """Filter/sort proxy with numeric-aware, per-cell sort semantics.

    Every column auto-detects numbers: a cell that parses as an int
    (decimal or ``0x``-hex) sorts numerically, so a count column reads
    2 < 10 < 100 instead of "10" < "100" < "2". Non-numeric cells sort
    as case-insensitive text with empty cells pushed last. A cell may
    override its key via :data:`SORT_KEY_ROLE` (used for icon-only
    columns that have no visible text to sort on).

    ``id_column`` is retained for call-site compatibility (it's the
    default sort column the panels pick) but no longer gates numeric
    sorting — that now applies to all columns.
    """

    def __init__(self, id_column: int, parent=None):
        super().__init__(parent)
        self._id_column = id_column

    def lessThan(self, left, right) -> bool:  # noqa: D401
        lk = left.data(SORT_KEY_ROLE)
        rk = right.data(SORT_KEY_ROLE)
        if lk is not None or rk is not None:
            return self._compare(lk, rk)
        return self._compare(left.data(), right.data())

    def _compare(self, lv, rv) -> bool:
        ls = "" if lv is None else str(lv)
        rs = "" if rv is None else str(rv)
        ln = _as_number(ls)
        rn = _as_number(rs)
        if ln is not None and rn is not None:
            return ln < rn
        # Blanks-last: key on emptiness before text so the populated
        # rows cluster together in ascending order (the common case).
        if (ls == "") != (rs == ""):
            return rs == ""  # non-empty left sorts before empty right
        return ls.casefold() < rs.casefold()
