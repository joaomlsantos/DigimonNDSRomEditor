"""Equipment editor — 146 records keyed by item id (0x8F..0x120).

Bytes 0x08-0x16 are zero on every vanilla record. They're surfaced as named
"unknown" spinboxes under a collapsed group so the form stays focused on the
documented boost/resistance fields.
"""
from __future__ import annotations

from typing import Dict, List, Tuple

from PySide6.QtCore import Qt
from PySide6.QtGui import QUndoStack
from PySide6.QtWidgets import (
    QGridLayout,
    QLabel,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from digimon_core import constants, model

from .form_helpers import (
    BoldGroupBox as QGroupBox,
    BoundSpinBox,
    _make_compact_grid,
    add_unknown_grid_field,
    make_form,
    register_unknown_container,
    wrap_in_scroll,
)
from .record_list_panel import RecordListPanel


_STAT_BOOSTS: List[Tuple[str, str]] = [
    ("Attack",  "atk_boost"),
    ("Defense", "defense_boost"),
    ("Spirit",  "spirit_boost"),
    ("Speed",   "speed_boost"),
]

_ELEMENTAL_RES: List[Tuple[str, str]] = [
    ("Light",   "light_res_boost"),
    ("Fire",    "fire_res_boost"),
    ("Water",   "water_res_boost"),
    ("Wind",    "wind_res_boost"),
    ("Dark",    "dark_res_boost"),
    ("Earth",   "earth_res_boost"),
    ("Steel",   "steel_res_boost"),
    ("Thunder", "thunder_res_boost"),
]

_STATUS_RES: List[Tuple[str, str]] = [
    ("Poison",    "poison_res"),
    ("Confusion", "confusion_res"),
    ("Death",     "death_res"),
    ("Paralysis", "paralysis_res"),
    ("Sleep",     "sleep_res"),
]

_BATTLE_BOOSTS: List[Tuple[str, str]] = [
    ("Accuracy",  "accuracy_boost"),
    ("Dodge",     "dodge_boost"),
    ("Critical",  "critical_boost"),
    ("Flee Rate", "flee_rate_boost"),
    ("Damage",    "dmg_boost"),
    ("Money",     "money_boost"),
    ("Exp",       "exp_boost"),
]

_UNKNOWN_FIELDS: List[str] = [
    "unknown_0x08", "unknown_0x0a", "unknown_0x0c", "unknown_0x0e",
    "unknown_0x10", "unknown_0x12", "unknown_0x14",
]


def _item_name(item_id: int) -> str:
    return constants.ITEM_ID_TO_STR.get(item_id, f"<item 0x{item_id:03x}>")


def _record_label(_ix: int, rec: model.Equipment) -> str:
    return f"0x{rec.id:03x}  {_item_name(rec.id)}"


class EquipmentEditor(QWidget):
    def __init__(
        self,
        records: Dict[int, model.Equipment],
        undo_stack: QUndoStack,
        parent=None,
    ):
        super().__init__(parent)
        self._records: List[model.Equipment] = [records[k] for k in sorted(records)]
        self._undo_stack = undo_stack
        self._current_ix: int = -1
        self._all_widgets: List[object] = []

        self._list_panel = RecordListPanel(self._records, _record_label, dirty_aware=True)
        self._list_panel.indexSelected.connect(self._on_selection)

        self._detail = self._build_detail_container()

        splitter = QSplitter(Qt.Horizontal, self)
        splitter.addWidget(self._list_panel)
        splitter.addWidget(self._detail)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([280, 720])

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(splitter)

        undo_stack.indexChanged.connect(self._refresh_form)
        undo_stack.indexChanged.connect(self._list_panel.refresh_dirty_state)
        self._list_panel.select_first()

    def select_by_id(self, item_id: int) -> bool:
        for ix, rec in enumerate(self._records):
            if rec.id == item_id:
                return self._list_panel.select_index(ix)
        return False

    def _add_field(self, form, label: str, widget) -> None:
        form.addRow(label, widget)
        self._all_widgets.append(widget)

    def _add_grid_field(self, grid: QGridLayout, row: int, col: int, label: str, widget) -> None:
        grid.addWidget(QLabel(label), row, col * 2)
        grid.addWidget(widget, row, col * 2 + 1)
        self._all_widgets.append(widget)

    def _grid_for(self, group: QGroupBox, cols: int) -> QGridLayout:
        return _make_compact_grid(group, cols=cols)

    def _build_detail_container(self) -> QWidget:
        first = self._records[0]

        self._title = QLabel("—")
        font = self._title.font()
        font.setPointSize(font.pointSize() + 3)
        font.setBold(True)
        self._title.setFont(font)

        identity = QGroupBox("Identity / Requirements")
        identity_form = make_form(identity)
        self._add_field(identity_form, "Item id",            BoundSpinBox(first, "id", 2, self._undo_stack, hex_display=True, read_only=True))
        self._add_field(identity_form, "Level requirement",  BoundSpinBox(first, "lvl_condition", 1, self._undo_stack))
        self._add_field(identity_form, "Species requirement", BoundSpinBox(first, "species_condition", 1, self._undo_stack))
        self._add_field(identity_form, "Bit cost",           BoundSpinBox(first, "bit_cost", 4, self._undo_stack))

        stats_box = QGroupBox("Stat Boosts")
        stats_grid = self._grid_for(stats_box, cols=2)
        for i, (label, attr) in enumerate(_STAT_BOOSTS):
            self._add_grid_field(stats_grid, i // 2, i % 2, label, BoundSpinBox(first, attr, 2, self._undo_stack))

        elem_box = QGroupBox("Elemental Resistance Boosts")
        elem_grid = self._grid_for(elem_box, cols=4)
        for i, (label, attr) in enumerate(_ELEMENTAL_RES):
            self._add_grid_field(elem_grid, i // 4, i % 4, label, BoundSpinBox(first, attr, 2, self._undo_stack))

        status_box = QGroupBox("Status Resistance")
        status_grid = self._grid_for(status_box, cols=2)
        for i, (label, attr) in enumerate(_STATUS_RES):
            self._add_grid_field(status_grid, i // 2, i % 2, label, BoundSpinBox(first, attr, 2, self._undo_stack))

        battle_box = QGroupBox("Battle / Reward Boosts")
        battle_grid = self._grid_for(battle_box, cols=4)
        for i, (label, attr) in enumerate(_BATTLE_BOOSTS):
            self._add_grid_field(battle_grid, i // 4, i % 4, label, BoundSpinBox(first, attr, 2, self._undo_stack))

        unknowns = QGroupBox("Unknown / Unmapped (raw 2-byte fields, vanilla = 0)")
        register_unknown_container(unknowns)
        unknowns_grid = self._grid_for(unknowns, cols=2)
        for i, attr in enumerate(_UNKNOWN_FIELDS):
            spin = BoundSpinBox(first, attr, 2, self._undo_stack)
            self._all_widgets.append(spin)
            add_unknown_grid_field(unknowns_grid, i // 2, i % 2, attr, spin)

        content = QWidget()
        cl = QVBoxLayout(content)
        cl.setContentsMargins(6, 6, 6, 6)
        cl.setSpacing(4)
        cl.addWidget(self._title)
        cl.addWidget(identity)
        cl.addWidget(stats_box)
        cl.addWidget(status_box)
        cl.addWidget(elem_box)
        cl.addWidget(battle_box)
        cl.addWidget(unknowns)
        cl.addStretch(1)

        return wrap_in_scroll(content)

    def _on_selection(self, ix: int) -> None:
        if not (0 <= ix < len(self._records)):
            return
        self._current_ix = ix
        target = self._records[ix]
        self._title.setText(
            f"{_item_name(target.id)}    (offset 0x{target.offset:08x}, id 0x{target.id:03x})"
        )
        for w in self._all_widgets:
            w.rebind(target)

    def _refresh_form(self, _index: int) -> None:
        if not (0 <= self._current_ix < len(self._records)):
            return
        for w in self._all_widgets:
            w.refresh()
