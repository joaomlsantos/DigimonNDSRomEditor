"""MoveData editor — filterable list of moves + detail form.

Field semantics follow research_docs/moves_research.txt. The "primary effect"
and "secondary effect" lookup tables are encoded here for readability — see
that doc for the canonical mapping.
"""
from __future__ import annotations

from typing import Dict, List, Tuple

from PySide6.QtCore import QSortFilterProxyModel, Qt, Signal
from PySide6.QtGui import QStandardItem, QStandardItemModel, QUndoStack
from PySide6.QtWidgets import (
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QScrollArea,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from digimon_core import constants, model

from .form_helpers import BoundCheckBox, BoundEnumCombo, BoundIntChoiceCombo, BoundSpinBox, make_form


# id 0 is unused; MOVE_ARRAY_STR[0] is "Charge" but in-game move ids start at 1
def _move_name(move_id: int) -> str:
    if 0 <= move_id < len(constants.MOVE_ARRAY_STR):
        return constants.MOVE_ARRAY_STR[move_id]
    return "(undefined)"


# (label, value) — see moves_research.txt for the full table
_SPECIAL_IDENTIFIER_CHOICES: List[Tuple[str, int]] = [
    ("Damage", 0x00),
    ("Status / Buff / Debuff", 0x01),
    ("Heal / Recovery", 0x02),
]

_PRIMARY_EFFECT_CHOICES: List[Tuple[str, int]] = [
    ("Damage only", 0x01),
    ("HP Drain", 0x02),
    ("MP Drain", 0x03),
    ("?? 0x04", 0x04),
    ("Raise/Reduce Attack", 0x05),
    ("Raise/Reduce Defense", 0x06),
    ("Raise/Reduce Spirit", 0x07),
    ("Raise/Reduce Speed", 0x08),
    ("Counter Status", 0x09),
    ("?? 0x0A", 0x0A),
    ("Confusion (Flash Ray)", 0x0B),
    ("?? 0x0C", 0x0C),
    ("Paralyze", 0x0D),
    ("Sleep", 0x0E),
    ("Raise/Reduce Resistance", 0x0F),
    ("Restore HP", 0x10),
    ("Restore MP", 0x11),
    ("Revive Ally", 0x12),
    ("Restore HP + Cure Poison", 0x13),
    ("Restore HP + Cure status", 0x14),
    ("Raise Light Res", 0x15),
    ("Raise Fire Res", 0x16),
    ("Raise Water Res", 0x17),
    ("Raise Wind Res", 0x18),
    ("Raise Dark Res", 0x19),
    ("Raise Earth Res", 0x1A),
    ("Raise Steel Res", 0x1B),
    ("Raise Thunder Res", 0x1C),
    ("Attack + reduce Def (Acid Rain)", 0x101),
]

_SECONDARY_EFFECT_CHOICES: List[Tuple[str, int]] = [
    ("None", 0x00),
    ("Reduce Attack", 0x05),
    ("Reduce Defense", 0x06),
    ("Reduce Spirit", 0x07),
    ("Reduce Speed", 0x08),
    ("Doom", 0x0A),
    ("Confusion", 0x0B),
    ("Poison", 0x0C),
    ("Paralyze", 0x0D),
    ("Sleep", 0x0E),
    ("Reduce Resistance", 0x0F),
    ("Status restore", 0x10),
    ("Revive Ally", 0x110),
    ("Reduce Light Res", 0x15),
    ("Reduce Fire Res", 0x16),
    ("Reduce Water Res", 0x17),
    ("Reduce Wind Res", 0x18),
    ("Reduce Dark Res", 0x19),
    ("Reduce Earth Res", 0x1A),
    ("Reduce Steel Res", 0x1B),
    ("Reduce Thunder Res", 0x1C),
]

# move_range encoding (see moves_research.txt § 20th byte)
_MOVE_RANGE_CHOICES: List[Tuple[str, int]] = [
    ("Self only / unused (0x00)", 0x00),
    ("(XXOXX) Selectable Enemy", 0x04),
    ("(XXOOX) Selectable Enemy", 0x06),
    ("(XOXOX) Selectable Enemy", 0x0A),
    ("(XOOXX) Selectable Enemy", 0x0C),
    ("(XOOOX) Selectable Enemy", 0x0E),
    ("(XOOOO) Selectable Enemy", 0x0F),
    ("(XXOXX) Selectable Ally", 0x44),
    ("(XXOOX) Selectable Ally", 0x46),
    ("(XOOOX) Selectable Ally", 0x4E),
    ("(XOOOO) Selectable Ally", 0x4F),
    ("(XOOOX) Fixed Enemy", 0x8E),
    ("(OXOXO) Fixed Enemy", 0x95),
    ("(OOXOO) Fixed Enemy", 0x9B),
    ("(OOOOO) Fixed Enemy", 0x9F),
    ("Moon Tears (0xCE)", 0xCE),
    ("(OOOOO) Fixed Ally", 0xDF),
]


class MoveListPanel(QWidget):
    """Filterable list of moves by id + name."""

    moveIndexSelected = Signal(int)  # emits index into the moves list

    def __init__(self, moves: List[model.MoveData], parent=None):
        super().__init__(parent)

        self._source_model = QStandardItemModel(self)
        for ix, move in enumerate(moves):
            name = _move_name(move.id)
            item = QStandardItem(f"0x{move.id:03x} — {name}")
            item.setEditable(False)
            item.setData(ix, Qt.UserRole)
            self._source_model.appendRow(item)

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
        self._view.setCurrentIndex(self._proxy.index(0, 0))

    def _on_current_changed(self, current, _previous):
        if not current.isValid():
            return
        ix = current.data(Qt.UserRole)
        if ix is not None:
            self.moveIndexSelected.emit(int(ix))


class MoveEditor(QWidget):
    def __init__(self, moves: List[model.MoveData], undo_stack: QUndoStack, parent=None):
        super().__init__(parent)
        self._moves = moves
        self._undo_stack = undo_stack
        self._current_ix: int = -1

        self._list_panel = MoveListPanel(moves)
        self._list_panel.moveIndexSelected.connect(self._on_selection)

        self._detail = self._build_detail_container()

        splitter = QSplitter(Qt.Horizontal, self)
        splitter.addWidget(self._list_panel)
        splitter.addWidget(self._detail)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([260, 740])

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(splitter)

        undo_stack.indexChanged.connect(self._refresh_form)
        self._list_panel.select_first()

    # ---- detail form -----------------------------------------------------

    def _build_detail_container(self) -> QWidget:
        first = self._moves[0]

        self._title = QLabel("—")
        font = self._title.font()
        font.setPointSize(font.pointSize() + 3)
        font.setBold(True)
        self._title.setFont(font)

        self._id_spin = BoundSpinBox(first, "id", 2, self._undo_stack, hex_display=True, read_only=True)
        self._mp_spin = BoundSpinBox(first, "mp_cost", 2, self._undo_stack)
        self._element_combo = BoundEnumCombo(first, "element", model.Element, self._undo_stack)
        self._special_combo = BoundIntChoiceCombo(
            first, "special_identifier", _SPECIAL_IDENTIFIER_CHOICES, self._undo_stack
        )
        self._level_spin = BoundSpinBox(first, "level_learned", 2, self._undo_stack)

        identity_box = QGroupBox("Identity")
        identity_form = make_form(identity_box)
        identity_form.addRow("ID", self._id_spin)
        identity_form.addRow("MP Cost", self._mp_spin)
        identity_form.addRow("Element", self._element_combo)
        identity_form.addRow("Special", self._special_combo)
        identity_form.addRow("Level Learned", self._level_spin)

        self._primary_combo = BoundIntChoiceCombo(
            first, "primary_effect", _PRIMARY_EFFECT_CHOICES, self._undo_stack
        )
        self._primary_value_spin = BoundSpinBox(first, "primary_value", 2, self._undo_stack)
        self._secondary_combo = BoundIntChoiceCombo(
            first, "secondary_effect", _SECONDARY_EFFECT_CHOICES, self._undo_stack
        )
        self._secondary_value_spin = BoundSpinBox(first, "secondary_value", 2, self._undo_stack)

        effects_box = QGroupBox("Effects")
        effects_form = make_form(effects_box)
        effects_form.addRow("Primary Effect", self._primary_combo)
        effects_form.addRow("Primary Value (power)", self._primary_value_spin)
        effects_form.addRow("Secondary Effect", self._secondary_combo)
        effects_form.addRow("Secondary Value", self._secondary_value_spin)

        self._num_hits_spin = BoundSpinBox(first, "num_hits", 1, self._undo_stack)
        self._range_combo = BoundIntChoiceCombo(
            first, "move_range", _MOVE_RANGE_CHOICES, self._undo_stack
        )
        self._consumable_check = BoundCheckBox(first, "is_consumable", self._undo_stack)

        targeting_box = QGroupBox("Targeting")
        targeting_form = make_form(targeting_box)
        targeting_form.addRow("Num Hits / Targets prompted", self._num_hits_spin)
        targeting_form.addRow("Range", self._range_combo)
        targeting_form.addRow("Is Consumable Item", self._consumable_check)

        self._unk_0xe_spin = BoundSpinBox(first, "unknown_0xe", 2, self._undo_stack, hex_display=True)
        self._unk_0x14_spin = BoundSpinBox(first, "unknown_0x14", 2, self._undo_stack, hex_display=True)
        self._unk_0x16_spin = BoundSpinBox(first, "unknown_0x16", 2, self._undo_stack, hex_display=True)

        misc_box = QGroupBox("Misc / Unknown")
        misc_form = make_form(misc_box)
        misc_form.addRow("Unknown 0x0e", self._unk_0xe_spin)
        misc_form.addRow("Unknown 0x14", self._unk_0x14_spin)
        misc_form.addRow("Unknown 0x16", self._unk_0x16_spin)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(6, 6, 6, 6)
        content_layout.setSpacing(4)
        content_layout.addWidget(self._title)
        content_layout.addWidget(identity_box)
        content_layout.addWidget(effects_box)
        content_layout.addWidget(targeting_box)
        content_layout.addWidget(misc_box)
        content_layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)
        return scroll

    # ---- selection / refresh --------------------------------------------

    def _all_widgets(self):
        return [
            self._id_spin, self._mp_spin, self._element_combo, self._special_combo,
            self._level_spin,
            self._primary_combo, self._primary_value_spin,
            self._secondary_combo, self._secondary_value_spin,
            self._num_hits_spin, self._range_combo, self._consumable_check,
            self._unk_0xe_spin, self._unk_0x14_spin, self._unk_0x16_spin,
        ]

    def _on_selection(self, ix: int) -> None:
        if not (0 <= ix < len(self._moves)):
            return
        target = self._moves[ix]
        self._current_ix = ix
        self._title.setText(self._title_for(target))
        for w in self._all_widgets():
            w.rebind(target)

    def _refresh_form(self, _index: int) -> None:
        if not (0 <= self._current_ix < len(self._moves)):
            return
        target = self._moves[self._current_ix]
        self._title.setText(self._title_for(target))
        for w in self._all_widgets():
            w.refresh()

    @staticmethod
    def _title_for(target: model.MoveData) -> str:
        return f"0x{target.id:03x}  —  {_move_name(target.id)}    (offset 0x{target.offset:08x})"
