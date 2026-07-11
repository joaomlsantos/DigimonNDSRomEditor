"""Filterable left-pane list of digimon entries keyed by digimon id.

Two rendering modes:

* **Single-column** (default) — a ``label_for(id) -> str`` callback per
  row, rendered headerless. Used by editors that just want
  ``0xNNN — Name`` rows (e.g. the digivolution editors).
* **Columnar** — pass ``columns_for(id) -> Sequence[str]`` + ``headers``
  and the panel renders a real multi-column :class:`QTreeView` with
  resizable, content-sized columns, a header row, and a native
  horizontal scrollbar when the columns overflow the pane. This is what
  the base / enemy digimon editors use so their id / name / displayed-as
  / cutscene fields line up as true columns instead of monospace-padded
  text (which couldn't scroll horizontally and truncated silently).

Both modes share the filter box (matches any column), the dirty /
mark gutter, and the dim styling.
"""
from __future__ import annotations

from typing import Callable, Dict, List, Optional, Sequence

from PySide6.QtCore import QSortFilterProxyModel, Qt, Signal
from PySide6.QtGui import QBrush, QPalette, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QApplication,
    QHeaderView,
    QLabel,
    QLineEdit,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from digimon_core import constants

from .._perf import span
from ._list_sort import ListSortProxy, marker_icon
from .form_helpers import is_record_dirty


def _default_label(digimon_id: int) -> str:
    name = constants.DIGIMON_ID_TO_STR.get(digimon_id, "<unknown>")
    return f"0x{digimon_id:03x} — {name}"


class DigimonListPanel(QWidget):
    """Filterable per-id list with an optional multi-column layout.

    `label_for(id) -> str` supplies a single-column row label (legacy
    mode). `columns_for(id) -> Sequence[str]` + `headers` switch the
    panel into a real multi-column QTreeView; the two are mutually
    exclusive (columns win when both are supplied). Call `refresh_label`
    after any change that alters a row's contents so it stays accurate.

    `dirty_aware=True` adds a leading gutter column showing "●" in front
    of records whose bytes differ from the original ROM snapshot. Editors
    using this should connect `undo_stack.indexChanged` to
    `refresh_dirty_state`.

    `dim_for(id) -> bool` (optional) paints matching rows in the muted
    placeholder colour + italic — for rows whose record is inactive
    (e.g. not Scannable). Pair with `legend` to caption the convention.

    `mark_for(id) -> bool` (optional) shows `marker_char` in the gutter
    column for matching rows (alongside the dirty dot). Use when both
    categories carry live data and the goal is to tell them apart rather
    than de-emphasise one.
    """

    digimonSelected = Signal(int)  # emits the digimon id
    # Row activated (double-click OR Enter). Kept for callers that want a
    # primary action distinct from selection; inert when nothing connects.
    rowActivated = Signal(int)

    def __init__(
        self,
        entries: Dict[int, object],
        parent=None,
        label_for: Optional[Callable[[int], str]] = None,
        columns_for: Optional[Callable[[int], Sequence[str]]] = None,
        headers: Optional[Sequence[str]] = None,
        dirty_aware: bool = False,
        dim_for: Optional[Callable[[int], bool]] = None,
        mark_for: Optional[Callable[[int], bool]] = None,
        marker_char: str = "▸",
        legend: Optional[str] = None,
        id_column: int = 0,
        monospace: bool = False,  # accepted for back-compat; no longer used
    ):
        super().__init__(parent)
        self._entries = entries
        self._columnar = columns_for is not None
        self._columns_for = columns_for
        self._inner_label_for = label_for or _default_label
        self._dirty_aware = dirty_aware
        self._dim_for = dim_for
        self._mark_for = mark_for
        # Which data column holds the numeric id. It sorts numerically, is
        # the default sort column, and carries the ●/▸ row markers. Editors
        # that prepend a column (e.g. the enemy list's encounter-icon
        # column) point this past 0; the default keeps id as column 0.
        self._id_column = id_column
        # Row markers (● edited, ▸ wild-encounter) render as a decoration
        # on the id column — only on rows that carry one — so a pristine
        # list has no marker and no reserved gutter column.
        self._show_markers = dirty_aware or (mark_for is not None)

        # Column geometry. The id column is column 0 (no gutter); data
        # columns run 0..n-1.
        self._data_start = 0
        self._n_data = len(headers) if (self._columnar and headers) else 1
        self._n_cols = self._n_data

        # Cached brushes so per-row styling is allocation-free. Placeholder
        # text reads as "muted but legible" across light + dark themes.
        palette = QApplication.palette()
        self._dim_brush = QBrush(palette.color(QPalette.Disabled, QPalette.WindowText))
        self._normal_brush = QBrush(palette.color(QPalette.Active, QPalette.WindowText))

        with span(f"build_rows×{len(entries)}"):
            self._source_model = QStandardItemModel(0, self._n_cols, self)
            self._row_by_id: Dict[int, int] = {}
            for row_ix, digimon_id in enumerate(sorted(entries.keys())):
                items = self._make_row_items(digimon_id)
                self._source_model.appendRow(items)
                self._row_by_id[digimon_id] = row_ix

        with span("proxy+view"):
            # Columnar mode gets the per-column sort proxy so header
            # clicks sort correctly (numeric ID, blanks-last on the
            # optional columns). Single-column mode keeps the plain
            # proxy — its header is hidden, so there's nothing to click.
            if self._columnar:
                self._proxy = ListSortProxy(self._id_column, self)
            else:
                self._proxy = QSortFilterProxyModel(self)
            self._proxy.setSourceModel(self._source_model)
            self._proxy.setFilterCaseSensitivity(Qt.CaseInsensitive)
            # Match the filter text against every column (id hex, name,
            # displayed-as, cutscene) so "thriller" and "0x21e" both work.
            self._proxy.setFilterKeyColumn(-1)

            self._filter_box = QLineEdit()
            self._filter_box.setPlaceholderText("Filter by name or id…")
            self._filter_box.textChanged.connect(self._proxy.setFilterFixedString)

            self._view = QTreeView()
            self._view.setModel(self._proxy)
            self._view.setRootIsDecorated(False)      # no expand arrows — flat list
            self._view.setUniformRowHeights(True)     # fast for ~800 rows
            self._view.setAllColumnsShowFocus(True)
            self._view.setEditTriggers(QTreeView.NoEditTriggers)
            self._view.setSelectionBehavior(QTreeView.SelectRows)
            self._view.setHeaderHidden(not self._columnar)
            # Alternating row shading gives the columns a table feel and
            # makes a long list easier to track across.
            self._view.setAlternatingRowColors(self._columnar)
            self._view.selectionModel().currentChanged.connect(
                self._on_current_changed
            )
            self._view.activated.connect(self._on_activated)

            hdr = self._view.header()
            hdr.setStretchLastSection(False)
            if self._columnar and headers:
                self._source_model.setHorizontalHeaderLabels(list(headers))
            # Data columns size to their content so the horizontal
            # scrollbar appears whenever the total exceeds the pane width.
            for c in range(self._n_cols):
                hdr.setSectionResizeMode(c, QHeaderView.Interactive)
            self._resize_data_columns()

            if self._columnar:
                # Header-click sorting. Default to the ID column ascending
                # — that's the natural row order the list was built in, so
                # first paint looks unchanged and clicking ID again after
                # sorting by another column restores it.
                self._view.setSortingEnabled(True)
                self._view.sortByColumn(self._id_column, Qt.AscendingOrder)

            layout = QVBoxLayout(self)
            layout.setContentsMargins(4, 4, 4, 4)
            layout.addWidget(self._filter_box)
            layout.addWidget(self._view)
            if legend:
                legend_label = QLabel(legend)
                legend_label.setWordWrap(True)
                legend_label.setStyleSheet(
                    "color: palette(placeholder-text); font-style: italic;"
                )
                layout.addWidget(legend_label)

    # ---- row construction / refresh -------------------------------------

    def _row_values(self, digimon_id: int) -> List[str]:
        """The data-column strings for one row."""
        if self._columnar and self._columns_for is not None:
            vals = [str(v) for v in self._columns_for(digimon_id)]
            # Pad / trim to the declared column count so a callback that
            # returns fewer values (e.g. before a lazy dependency is
            # ready) doesn't desync the model shape.
            if len(vals) < self._n_data:
                vals += [""] * (self._n_data - len(vals))
            return vals[: self._n_data]
        return [self._inner_label_for(digimon_id)]

    def _apply_marker(self, digimon_id: int, id_item: Optional[QStandardItem]) -> None:
        """Set (or clear) the id-column decoration for a row's marker state."""
        if id_item is None or not self._show_markers:
            return
        dirty = self._dirty_aware and is_record_dirty(self._entries.get(digimon_id))
        marked = self._mark_for is not None and self._mark_for(digimon_id)
        if dirty or marked:
            id_item.setData(marker_icon(dirty, marked), Qt.DecorationRole)
        else:
            id_item.setData(None, Qt.DecorationRole)

    def _make_row_items(self, digimon_id: int) -> List[QStandardItem]:
        items: List[QStandardItem] = []
        for val in self._row_values(digimon_id):
            it = QStandardItem(val)
            it.setEditable(False)
            items.append(it)
        # Stamp the id on every column so a click anywhere in the row
        # resolves to it, and apply dim styling row-wide.
        for it in items:
            it.setData(digimon_id, Qt.UserRole)
        self._apply_row_style(digimon_id, items)
        if len(items) > self._id_column:
            self._apply_marker(digimon_id, items[self._id_column])
        return items

    def _apply_row_style(
        self, digimon_id: int, items: Sequence[QStandardItem],
    ) -> None:
        if self._dim_for is None:
            return
        dim = bool(self._dim_for(digimon_id))
        brush = self._dim_brush if dim else self._normal_brush
        for it in items:
            it.setForeground(brush)
            font = it.font()
            if font.italic() != dim:
                font.setItalic(dim)
                it.setFont(font)

    def _row_items(self, row: int) -> List[QStandardItem]:
        return [
            self._source_model.item(row, c)
            for c in range(self._n_cols)
        ]

    def _resize_data_columns(self) -> None:
        """Size each data column to its widest cell.

        Called after populate + after any bulk relabel. Interactive
        resize mode means Qt won't rescan on every model touch, so this
        is where the natural widths (and thus the horizontal scrollbar)
        get established.
        """
        for c in range(self._data_start, self._n_cols):
            self._view.resizeColumnToContents(c)

    # ---- selection ------------------------------------------------------

    def select_first(self) -> None:
        if self._proxy.rowCount() == 0:
            return
        self._view.setCurrentIndex(self._proxy.index(0, 0))

    def select_by_id(self, digimon_id: int) -> bool:
        """Select the row for `digimon_id`, clearing the filter so it's
        visible. Returns False if the id isn't present."""
        row = self._row_by_id.get(digimon_id)
        if row is None:
            return False
        self._filter_box.clear()
        src_index = self._source_model.index(row, 0)
        proxy_index = self._proxy.mapFromSource(src_index)
        if not proxy_index.isValid():
            return False
        self._view.setCurrentIndex(proxy_index)
        self._view.scrollTo(proxy_index)
        return True

    # ---- refresh --------------------------------------------------------

    def refresh_label(self, digimon_id: int) -> None:
        row = self._row_by_id.get(digimon_id)
        if row is None:
            return
        items = self._row_items(row)
        values = self._row_values(digimon_id)
        for i, val in enumerate(values):
            it = items[i]
            if it is not None:
                it.setText(val)
        self._apply_row_style(digimon_id, [it for it in items if it is not None])
        if len(items) > self._id_column:
            self._apply_marker(digimon_id, items[self._id_column])
        self._resize_data_columns()

    def refresh_all_labels(self) -> None:
        # With sorting live, the proxy's dynamic re-sort would fire on
        # every per-cell setText below — N re-sorts for an N-row bulk
        # relabel. Suspend it for the duration and re-enable once at the
        # end (re-enabling triggers a single re-sort + re-filter).
        dynamic = self._proxy.dynamicSortFilter()
        self._proxy.setDynamicSortFilter(False)
        try:
            with span(f"refresh_labels×{len(self._row_by_id)}"):
                for digimon_id, row in self._row_by_id.items():
                    items = self._row_items(row)
                    values = self._row_values(digimon_id)
                    for i, val in enumerate(values):
                        it = items[i]
                        if it is not None:
                            it.setText(val)
                    self._apply_row_style(
                        digimon_id, [it for it in items if it is not None],
                    )
                    if len(items) > self._id_column:
                        self._apply_marker(digimon_id, items[self._id_column])
        finally:
            self._proxy.setDynamicSortFilter(dynamic)
        self._resize_data_columns()

    def refresh_dirty_state(self) -> None:
        """Re-render every row so dirty / mark / dim decorations reflect
        the latest record contents. Cheap — a few QStandardItem touches
        per row."""
        if not self._dirty_aware and self._dim_for is None and self._mark_for is None:
            return
        self.refresh_all_labels()

    # ---- signals --------------------------------------------------------

    def _on_current_changed(self, current, _previous):
        if not current.isValid():
            return
        digimon_id = current.data(Qt.UserRole)
        if digimon_id is not None:
            self.digimonSelected.emit(int(digimon_id))

    def _on_activated(self, index):
        if not index.isValid():
            return
        digimon_id = index.data(Qt.UserRole)
        if digimon_id is not None:
            self.rowActivated.emit(int(digimon_id))
