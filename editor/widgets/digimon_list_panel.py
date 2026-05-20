"""Filterable left-pane list of digimon entries keyed by digimon id."""
from __future__ import annotations

from typing import Callable, Dict, Optional

from PySide6.QtCore import QSortFilterProxyModel, Qt, Signal
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import QLineEdit, QListView, QVBoxLayout, QWidget

from digimon_core import constants


def _default_label(digimon_id: int) -> str:
    name = constants.DIGIMON_ID_TO_STR.get(digimon_id, "<unknown>")
    return f"0x{digimon_id:03x} — {name}"


class DigimonListPanel(QWidget):
    """Shows "0x041 — Chicchimon" rows for a Dict[id, model], with a filter box.

    A custom `label_for(id) -> str` may be supplied for editors that want to
    decorate rows with additional info (e.g. the enemy editor showing the
    reskin display for fixed enemies). Call `refresh_label(id)` after any
    change that would alter the label so the row stays accurate.
    """

    digimonSelected = Signal(int)  # emits the digimon id

    def __init__(
        self,
        entries: Dict[int, object],
        parent=None,
        label_for: Optional[Callable[[int], str]] = None,
    ):
        super().__init__(parent)
        self._label_for = label_for or _default_label

        self._source_model = QStandardItemModel(self)
        self._row_by_id: Dict[int, int] = {}
        for row_ix, digimon_id in enumerate(sorted(entries.keys())):
            item = QStandardItem(self._label_for(digimon_id))
            item.setEditable(False)
            item.setData(digimon_id, Qt.UserRole)
            self._source_model.appendRow(item)
            self._row_by_id[digimon_id] = row_ix

        self._proxy = QSortFilterProxyModel(self)
        self._proxy.setSourceModel(self._source_model)
        self._proxy.setFilterCaseSensitivity(Qt.CaseInsensitive)

        self._filter_box = QLineEdit()
        self._filter_box.setPlaceholderText("Filter by name or id…")
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

    def refresh_label(self, digimon_id: int) -> None:
        row = self._row_by_id.get(digimon_id)
        if row is None:
            return
        item = self._source_model.item(row)
        if item is not None:
            item.setText(self._label_for(digimon_id))

    def refresh_all_labels(self) -> None:
        for digimon_id, row in self._row_by_id.items():
            item = self._source_model.item(row)
            if item is not None:
                item.setText(self._label_for(digimon_id))

    def _on_current_changed(self, current, _previous):
        if not current.isValid():
            return
        digimon_id = current.data(Qt.UserRole)
        if digimon_id is not None:
            self.digimonSelected.emit(int(digimon_id))
