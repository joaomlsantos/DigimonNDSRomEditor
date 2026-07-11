"""Filterable left-pane list for a `List[record]` collection.

Two rendering modes:

* **Single-column** (default) — a ``label_for(index, record) -> str``
  callback per row, rendered headerless. What the graphics browsers
  (sprite / mchr / btchr / btmap / map) use.
* **Columnar** — pass ``columns_for(index, record) -> Sequence[str]`` +
  ``headers`` and the panel renders a real multi-column ``QTreeView``
  with resizable, content-sized columns, a header row, header-click
  sorting, and a native horizontal scrollbar when the columns overflow.
  The data editors (items, quests, digivolutions, …) use this so their
  fields line up as true columns.

Both modes share the filter box (matches any column), the dirty gutter,
and the index-based selection/append/pop API.
"""
from __future__ import annotations

from typing import Callable, List, Optional, Sequence

from PySide6.QtCore import QSortFilterProxyModel, Qt, Signal
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QHeaderView,
    QLineEdit,
    QListView,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from ._list_sort import SORT_KEY_ROLE, ListSortProxy, marker_icon
from .form_helpers import is_record_dirty


DIRTY_PREFIX = "● "
CLEAN_PREFIX = "  "


class RecordListPanel(QWidget):
    """List view backed by indices into `records`. Emits `indexSelected(int)`.

    `label_for(index, record) -> str` supplies a single-column row label
    (legacy mode). `columns_for(index, record) -> Sequence[str]` +
    `headers` switch the panel into a sortable multi-column QTreeView;
    columns win when both are supplied.

    `dirty_aware=True` marks rows whose record bytes differ from the
    original ROM snapshot — "● " prefixed (legacy single-column) or an
    inline "●" decoration on the id column (columnar). Editors using
    this should wire `undo_stack.indexChanged` to `refresh_dirty_state`.
    """

    indexSelected = Signal(int)

    def __init__(
        self,
        records: List[object],
        label_for: Optional[Callable[[int, object], str]] = None,
        parent=None,
        dirty_aware: bool = False,
        columns_for: Optional[Callable[[int, object], Sequence[str]]] = None,
        headers: Optional[Sequence[str]] = None,
        decorations_for: Optional[Callable[[int, object], Sequence[object]]] = None,
        sort_keys_for: Optional[Callable[[int, object], Sequence[object]]] = None,
    ):
        super().__init__(parent)

        self._records = records
        self._inner_label_for = label_for
        self._columns_for = columns_for
        # Optional per-column cell icons (QIcon/QPixmap, or None to skip a
        # column). Lets a columnar list render an icon-only cell — e.g. the
        # move list's Element column — instead of text. Column 0's decoration
        # stays reserved for the dirty marker.
        self._decorations_for = decorations_for
        # Optional per-column explicit sort keys (or None to sort a column on
        # its visible text). Needed for icon-only columns that have no text to
        # sort on — e.g. sorting the move list by element.
        self._sort_keys_for = sort_keys_for
        self._columnar = columns_for is not None
        self._dirty_aware = dirty_aware
        # Dirty state renders as an inline decoration on the id column in
        # columnar mode (no reserved gutter column), or a "● " text
        # prefix in the legacy single-column mode.
        self._show_markers = dirty_aware and self._columnar
        self._n_data = len(headers) if (self._columnar and headers) else 1
        self._n_cols = self._n_data
        # Pin the user's last selection so it survives filter changes and
        # label refreshes. Qt's selection model maps through layout changes,
        # but it drops selections when the filter hides the row, and the view
        # never re-scrolls on its own after the proxy resettles.
        self._tracked_source_row: int = -1

        self._source_model = QStandardItemModel(0, self._n_cols, self)
        for ix in range(len(records)):
            self._source_model.appendRow(self._make_row_items(ix))

        if self._columnar:
            self._proxy = ListSortProxy(0, self)
        else:
            self._proxy = QSortFilterProxyModel(self)
        self._proxy.setSourceModel(self._source_model)
        self._proxy.setFilterCaseSensitivity(Qt.CaseInsensitive)
        self._proxy.setFilterKeyColumn(-1)

        self._filter_box = QLineEdit()
        self._filter_box.setPlaceholderText("Filter…")
        self._filter_box.textChanged.connect(self._on_filter_text_changed)

        if self._columnar:
            self._view = QTreeView()
            self._view.setRootIsDecorated(False)
            self._view.setUniformRowHeights(True)
            self._view.setAllColumnsShowFocus(True)
            self._view.setSelectionBehavior(QTreeView.SelectRows)
            self._view.setAlternatingRowColors(True)
            self._view.setModel(self._proxy)
            self._view.setEditTriggers(QTreeView.NoEditTriggers)
            hdr = self._view.header()
            hdr.setStretchLastSection(False)
            if headers:
                self._source_model.setHorizontalHeaderLabels(list(headers))
            for c in range(self._n_cols):
                hdr.setSectionResizeMode(c, QHeaderView.Interactive)
            self._resize_data_columns()
            # Header-click sorting; default to the id column ascending so
            # first paint matches the natural record order.
            self._view.setSortingEnabled(True)
            self._view.sortByColumn(0, Qt.AscendingOrder)
        else:
            self._view = QListView()
            self._view.setModel(self._proxy)
            self._view.setUniformItemSizes(True)
            self._view.setEditTriggers(QListView.NoEditTriggers)
        self._view.selectionModel().currentChanged.connect(
            self._on_current_changed
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(self._filter_box)
        layout.addWidget(self._view)

    # ---- row construction ------------------------------------------------

    def _row_values(self, index: int) -> List[str]:
        rec = self._records[index]
        if self._columnar and self._columns_for is not None:
            vals = [str(v) for v in self._columns_for(index, rec)]
            if len(vals) < self._n_data:
                vals += [""] * (self._n_data - len(vals))
            return vals[: self._n_data]
        label = self._inner_label_for(index, rec) if self._inner_label_for else str(index)
        if self._dirty_aware:
            label = (DIRTY_PREFIX if is_record_dirty(rec) else CLEAN_PREFIX) + label
        return [label]

    def _apply_marker(self, index: int, id_item: Optional[QStandardItem]) -> None:
        """Set (or clear) the id-column dirty decoration for a row."""
        if id_item is None or not self._show_markers:
            return
        if is_record_dirty(self._records[index]):
            id_item.setData(marker_icon(True, False), Qt.DecorationRole)
        else:
            id_item.setData(None, Qt.DecorationRole)

    def _apply_decorations(self, index: int, items: List[QStandardItem]) -> None:
        """Set per-column DecorationRole icons from ``decorations_for``.

        Column 0 is reserved for the dirty marker when markers are active,
        so its decoration is left untouched here. A ``None`` entry clears
        that column's decoration (so an icon drops when the value that
        produced it changes)."""
        if self._decorations_for is None:
            return
        icons = list(self._decorations_for(index, self._records[index]))
        for col, icon in enumerate(icons):
            if col >= len(items) or items[col] is None:
                continue
            if col == 0 and self._show_markers:
                continue
            items[col].setData(icon, Qt.DecorationRole)

    def _apply_sort_keys(self, index: int, items: List[QStandardItem]) -> None:
        """Set per-column ``SORT_KEY_ROLE`` values from ``sort_keys_for`` so
        columns without sortable display text (icon-only cells) still sort.
        A ``None`` entry clears the key, falling the column back to its
        visible text."""
        if self._sort_keys_for is None:
            return
        keys = list(self._sort_keys_for(index, self._records[index]))
        for col, key in enumerate(keys):
            if col >= len(items) or items[col] is None:
                continue
            items[col].setData(key, SORT_KEY_ROLE)

    def _make_row_items(self, index: int) -> List[QStandardItem]:
        items: List[QStandardItem] = []
        for val in self._row_values(index):
            it = QStandardItem(val)
            it.setEditable(False)
            items.append(it)
        for it in items:
            it.setData(index, Qt.UserRole)
        self._apply_decorations(index, items)
        self._apply_sort_keys(index, items)
        if items:
            self._apply_marker(index, items[0])
        return items

    def _row_items(self, row: int) -> List[QStandardItem]:
        return [self._source_model.item(row, c) for c in range(self._n_cols)]

    def _resize_data_columns(self) -> None:
        if not self._columnar:
            return
        for c in range(self._n_cols):
            self._view.resizeColumnToContents(c)

    def _write_row(self, index: int) -> None:
        """Re-render one existing row's cells from current record state."""
        items = self._row_items(index)
        for i, val in enumerate(self._row_values(index)):
            it = items[i]
            if it is not None:
                it.setText(val)
        self._apply_decorations(index, items)
        self._apply_sort_keys(index, items)
        if items:
            self._apply_marker(index, items[0])

    # ---- selection -------------------------------------------------------

    def select_first(self) -> None:
        if self._proxy.rowCount() == 0:
            return
        self._view.setCurrentIndex(self._proxy.index(0, 0))

    def select_index(self, index: int) -> bool:
        """Select the row at source index `index`, clearing the filter so
        it's visible. Returns False if `index` is out of range."""
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

    # ---- list growth (undo paths) ----------------------------------------

    def append_record(self, record: object) -> int:
        """Append ``record`` and return its source row.

        Mutates the same ``records`` list passed to ``__init__`` so the
        caller's reference stays in sync. The proxy + selection survive;
        callers wanting to focus the new row follow up with
        ``select_index(returned_row)``.
        """
        self._records.append(record)
        index = len(self._records) - 1
        self._source_model.appendRow(self._make_row_items(index))
        return index

    def pop_record(self) -> None:
        """Remove the last appended row (mirror of ``append_record`` for
        undo). Quietly no-ops on an empty list."""
        if not self._records:
            return
        self._records.pop()
        self._source_model.removeRow(self._source_model.rowCount() - 1)

    # ---- refresh ---------------------------------------------------------

    def refresh_label(self, index: int, new_label: str = None) -> None:
        """Refresh a row from current record state.

        In columnar mode ``new_label`` is ignored — the row is recomputed
        via ``columns_for``. In single-column mode a supplied ``new_label``
        overrides the ``label_for`` recompute (legacy fast-path used by a
        few editors that already hold the freshly-built label)."""
        if index < 0 or index >= self._source_model.rowCount():
            return
        if not self._columnar and new_label is not None:
            item = self._source_model.item(index, 0)
            if item is not None:
                prefix = ""
                if self._dirty_aware:
                    prefix = (
                        DIRTY_PREFIX if is_record_dirty(self._records[index])
                        else CLEAN_PREFIX
                    )
                item.setText(prefix + new_label)
        else:
            self._write_row(index)
        self._resize_data_columns()
        self._ensure_tracked_visible()

    def refresh_dirty_state(self) -> None:
        """Re-render every row so dirty markers track the current state."""
        if not self._dirty_aware:
            return
        dynamic = self._proxy.dynamicSortFilter()
        self._proxy.setDynamicSortFilter(False)
        try:
            for ix in range(self._source_model.rowCount()):
                self._write_row(ix)
        finally:
            self._proxy.setDynamicSortFilter(dynamic)
        self._resize_data_columns()
        self._ensure_tracked_visible()

    def _on_filter_text_changed(self, text: str) -> None:
        self._proxy.setFilterFixedString(text)
        self._ensure_tracked_visible()

    def _ensure_tracked_visible(self) -> None:
        if self._tracked_source_row < 0:
            return
        src_index = self._source_model.index(self._tracked_source_row, 0)
        proxy_index = self._proxy.mapFromSource(src_index)
        if not proxy_index.isValid():
            return
        if self._view.currentIndex() != proxy_index:
            self._view.setCurrentIndex(proxy_index)
        self._view.scrollTo(proxy_index)

    def _on_current_changed(self, current, _previous):
        if not current.isValid():
            return
        ix = current.data(Qt.UserRole)
        if ix is None:
            return
        ix = int(ix)
        self._tracked_source_row = ix
        self.indexSelected.emit(ix)
