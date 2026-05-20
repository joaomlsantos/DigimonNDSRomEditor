"""Filterable left-pane list for a `List[record]` collection.

Pass a callable that turns a record into its display label. The panel emits the
record's index in the source list whenever selection changes.
"""
from __future__ import annotations

from typing import Callable, List

from PySide6.QtCore import QSortFilterProxyModel, Qt, Signal
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import QLineEdit, QListView, QVBoxLayout, QWidget


class RecordListPanel(QWidget):
    """List view backed by indices into `records`. Emits `indexSelected(int)`."""

    indexSelected = Signal(int)

    def __init__(
        self,
        records: List[object],
        label_for: Callable[[int, object], str],
        parent=None,
    ):
        super().__init__(parent)

        self._source_model = QStandardItemModel(self)
        for ix, rec in enumerate(records):
            item = QStandardItem(label_for(ix, rec))
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

    def select_first(self) -> None:
        if self._proxy.rowCount() == 0:
            return
        first = self._proxy.index(0, 0)
        self._view.setCurrentIndex(first)

    def refresh_label(self, index: int, new_label: str) -> None:
        item = self._source_model.item(index)
        if item is not None:
            item.setText(new_label)

    def _on_current_changed(self, current, _previous):
        if not current.isValid():
            return
        ix = current.data(Qt.UserRole)
        if ix is not None:
            self.indexSelected.emit(int(ix))
