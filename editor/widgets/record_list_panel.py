"""Filterable left-pane list for a `List[record]` collection.

Pass a callable that turns a record into its display label. The panel emits the
record's index in the source list whenever selection changes.
"""
from __future__ import annotations

from typing import Callable, List

from PySide6.QtCore import QSortFilterProxyModel, Qt, Signal
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import QLineEdit, QListView, QVBoxLayout, QWidget

from .form_helpers import is_record_dirty


DIRTY_PREFIX = "● "
CLEAN_PREFIX = "  "


class RecordListPanel(QWidget):
    """List view backed by indices into `records`. Emits `indexSelected(int)`.

    `dirty_aware=True` prepends "● " to rows whose record bytes differ from the
    original ROM snapshot (clean rows get matching whitespace so columns stay
    aligned). Editors using this mode should wire `undo_stack.indexChanged` to
    `refresh_dirty_state`.
    """

    indexSelected = Signal(int)

    def __init__(
        self,
        records: List[object],
        label_for: Callable[[int, object], str],
        parent=None,
        dirty_aware: bool = False,
    ):
        super().__init__(parent)

        self._records = records
        self._inner_label_for = label_for
        self._dirty_aware = dirty_aware

        self._source_model = QStandardItemModel(self)
        for ix, rec in enumerate(records):
            item = QStandardItem(self._decorated_label(ix))
            item.setEditable(False)
            item.setData(ix, Qt.UserRole)
            self._source_model.appendRow(item)

        self._proxy = QSortFilterProxyModel(self)
        self._proxy.setSourceModel(self._source_model)
        self._proxy.setFilterCaseSensitivity(Qt.CaseInsensitive)

        self._filter_box = QLineEdit()
        self._filter_box.setPlaceholderText("Filter…")
        self._filter_box.textChanged.connect(self._proxy.setFilterFixedString)

        self._view = QListView()
        self._view.setModel(self._proxy)
        self._view.setUniformItemSizes(True)
        self._view.setEditTriggers(QListView.NoEditTriggers)
        self._view.selectionModel().currentChanged.connect(self._on_current_changed)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(self._filter_box)
        layout.addWidget(self._view)

    def _decorated_label(self, index: int) -> str:
        rec = self._records[index]
        base = self._inner_label_for(index, rec)
        if not self._dirty_aware:
            return base
        return (DIRTY_PREFIX if is_record_dirty(rec) else CLEAN_PREFIX) + base

    def select_first(self) -> None:
        if self._proxy.rowCount() == 0:
            return
        first = self._proxy.index(0, 0)
        self._view.setCurrentIndex(first)

    def select_index(self, index: int) -> bool:
        """Select the row at the given source index. Clears the filter so the
        row is always visible. Returns False if `index` is out of range."""
        if not (0 <= index < self._source_model.rowCount()):
            return False
        self._filter_box.clear()
        src_index = self._source_model.index(index, 0)
        proxy_index = self._proxy.mapFromSource(src_index)
        if not proxy_index.isValid():
            return False
        self._view.setCurrentIndex(proxy_index)
        self._view.scrollTo(proxy_index)
        return True

    def refresh_label(self, index: int, new_label: str = None) -> None:
        """Refresh a row's label. If `new_label` is None, the panel recomputes
        the label via the originally-supplied `label_for` (and applies the
        dirty prefix when `dirty_aware`)."""
        item = self._source_model.item(index)
        if item is None:
            return
        if new_label is None:
            item.setText(self._decorated_label(index))
        else:
            prefix = ""
            if self._dirty_aware:
                prefix = DIRTY_PREFIX if is_record_dirty(self._records[index]) else CLEAN_PREFIX
            item.setText(prefix + new_label)

    def refresh_dirty_state(self) -> None:
        """Re-render every row label so dirty markers track the current state."""
        if not self._dirty_aware:
            return
        for ix in range(self._source_model.rowCount()):
            item = self._source_model.item(ix)
            if item is not None:
                item.setText(self._decorated_label(ix))

    def _on_current_changed(self, current, _previous):
        if not current.isValid():
            return
        ix = current.data(Qt.UserRole)
        if ix is not None:
            self.indexSelected.emit(int(ix))
