"""A wrapping (flow) layout — lays items left-to-right and wraps to the next
row when the current row runs out of width.

This is the standard Qt "Flow Layout" example ported to PySide6, with one
addition: ``hasHeightForWidth`` / ``heightForWidth`` are implemented so the
layout works inside a ``QScrollArea(widgetResizable=True)`` — the scroll area
hands the content its viewport width and asks how tall it needs to be, and the
flow reports the wrapped height. That's what lets a row of stat cells stack
onto a second row when the pane narrows instead of forcing a horizontal
scrollbar.
"""
from __future__ import annotations

from typing import List, Optional

from PySide6.QtCore import QMargins, QPoint, QRect, QSize, Qt
from PySide6.QtWidgets import QLayout, QLayoutItem, QSizePolicy, QWidget


class FlowLayout(QLayout):
    def __init__(
        self,
        parent: Optional[QWidget] = None,
        margin: int = 0,
        h_spacing: int = 6,
        v_spacing: int = 6,
    ):
        super().__init__(parent)
        self._items: List[QLayoutItem] = []
        self._h_space = h_spacing
        self._v_space = v_spacing
        if parent is not None:
            self.setContentsMargins(QMargins(margin, margin, margin, margin))

    # QLayout plumbing -----------------------------------------------------

    def addItem(self, item: QLayoutItem) -> None:  # noqa: N802 — Qt override
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> Optional[QLayoutItem]:  # noqa: N802
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index: int) -> Optional[QLayoutItem]:  # noqa: N802
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self) -> Qt.Orientations:  # noqa: N802
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return self._do_layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self) -> QSize:  # noqa: N802
        return self.minimumSize()

    def minimumSize(self) -> QSize:  # noqa: N802
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        size += QSize(
            margins.left() + margins.right(),
            margins.top() + margins.bottom(),
        )
        return size

    # layout core ----------------------------------------------------------

    def _do_layout(self, rect: QRect, test_only: bool) -> int:
        margins = self.contentsMargins()
        effective = rect.adjusted(
            margins.left(), margins.top(), -margins.right(), -margins.bottom()
        )
        x = effective.x()
        y = effective.y()
        line_height = 0

        for item in self._items:
            hint = item.sizeHint()
            next_x = x + hint.width() + self._h_space
            if next_x - self._h_space > effective.right() and line_height > 0:
                # Wrap to the next row.
                x = effective.x()
                y = y + line_height + self._v_space
                next_x = x + hint.width() + self._h_space
                line_height = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = next_x
            line_height = max(line_height, hint.height())

        return y + line_height - rect.y() + margins.bottom()


def make_height_for_width(widget: QWidget) -> QWidget:
    """Tag ``widget``'s size policy so a parent box layout honours its
    ``heightForWidth`` — needed when a FlowLayout-backed widget is nested
    inside a QVBoxLayout (the group boxes in the digimon detail form)."""
    policy = widget.sizePolicy()
    policy.setHeightForWidth(True)
    policy.setVerticalPolicy(QSizePolicy.Minimum)
    widget.setSizePolicy(policy)
    return widget
