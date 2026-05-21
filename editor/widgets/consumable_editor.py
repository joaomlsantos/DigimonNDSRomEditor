"""Consumable editor — 57 fixed-stride 0x14-byte records."""
from __future__ import annotations

from typing import List

from PySide6.QtCore import Qt
from PySide6.QtGui import QUndoStack
from PySide6.QtWidgets import (
    QLabel,
    QScrollArea,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from digimon_core import constants, model

from .form_helpers import (
    BoldGroupBox as QGroupBox,
    BoundHexLineEdit,
    BoundSpinBox,
    add_unknown_form_row,
    make_form,
    register_unknown_container,
)
from .record_list_panel import RecordListPanel


def _item_name(item_id: int) -> str:
    return constants.ITEM_ID_TO_STR.get(item_id, f"<item 0x{item_id:03x}>")


def _record_label(_ix: int, rec: model.Consumable) -> str:
    return f"0x{rec.id:03x}  {_item_name(rec.id)}"


class ConsumableEditor(QWidget):
    def __init__(
        self,
        records: List[model.Consumable],
        undo_stack: QUndoStack,
        parent=None,
    ):
        super().__init__(parent)
        self._records = records
        self._undo_stack = undo_stack
        self._current_ix: int = -1
        self._all_widgets: List[object] = []

        self._list_panel = RecordListPanel(records, _record_label, dirty_aware=True)
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

    def _build_detail_container(self) -> QWidget:
        first = self._records[0]

        self._title = QLabel("—")
        font = self._title.font()
        font.setPointSize(font.pointSize() + 3)
        font.setBold(True)
        self._title.setFont(font)

        identity = QGroupBox("Identity")
        identity_form = make_form(identity)
        self._add_field(identity_form, "Item id",           BoundSpinBox(first, "id", 2, self._undo_stack, hex_display=True, read_only=True))
        self._add_field(identity_form, "Consumable marker", BoundSpinBox(first, "consumable_marker", 2, self._undo_stack))
        self._add_field(identity_form, "Bit cost",          BoundSpinBox(first, "bit_cost", 2, self._undo_stack))

        effect = QGroupBox("Effect")
        effect_form = make_form(effect)
        self._add_field(effect_form, "Primary effect id",   BoundSpinBox(first, "primary_effect_id", 2, self._undo_stack, hex_display=True))
        self._add_field(effect_form, "Secondary effect id", BoundSpinBox(first, "secondary_effect_id", 2, self._undo_stack, hex_display=True))
        self._add_field(effect_form, "Effect value",        BoundSpinBox(first, "effect_value", 4, self._undo_stack))
        self._add_field(effect_form, "Flags (hex)",         BoundHexLineEdit(first, "flags", 4, self._undo_stack))

        # Single-field unknown group rather than misc — keeps the door open for
        # adding the rest of the per-byte slots once their meaning is sorted.
        unknowns = QGroupBox("Unknown / Unmapped (raw 2-byte fields)")
        register_unknown_container(unknowns)
        unknowns_form = make_form(unknowns)
        unk_0x06 = BoundSpinBox(first, "unknown_0x06", 2, self._undo_stack, hex_display=True)
        self._all_widgets.append(unk_0x06)
        add_unknown_form_row(unknowns_form, "Unknown 0x06", unk_0x06)

        content = QWidget()
        cl = QVBoxLayout(content)
        cl.setContentsMargins(6, 6, 6, 6)
        cl.setSpacing(4)
        cl.addWidget(self._title)
        cl.addWidget(identity)
        cl.addWidget(effect)
        cl.addWidget(unknowns)
        cl.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)
        return scroll

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
