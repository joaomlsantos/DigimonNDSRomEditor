"""MoveData editor — filterable list of moves + detail form.

Field semantics follow research_docs/moves_research.txt. The "primary effect"
and "secondary effect" lookup tables are encoded here for readability — see
that doc for the canonical mapping.
"""
from __future__ import annotations

from typing import Dict, List, Tuple

from PySide6.QtCore import QSortFilterProxyModel, Qt, Signal
from PySide6.QtGui import QStandardItem, QStandardItemModel, QUndoStack

from ..commands import SetAttrCommand
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from digimon_core import constants, model

from .form_helpers import (
    BoldGroupBox as QGroupBox,
    BoundCheckBox,
    BoundEnumCombo,
    BoundIntChoiceCombo,
    BoundSpinBox,
    NoWheelSpinBox,
    add_unknown_form_row,
    is_record_dirty,
    make_form,
    register_unknown_container,
    silenced,
    wrap_in_scroll,
)


_DIRTY_PREFIX = "● "
_CLEAN_PREFIX = "  "


# Soft caps that surface as red-border warnings. MP cost above the digimon MP
# cap (9999) is unusable in practice — the move can never be cast — and
# level_learned matches the in-game level cap (99); higher values mean the
# move is never naturally learned.
_MP_COST_CAP = 9999
_LEVEL_LEARNED_CAP = 99
_MP_COST_MESSAGE = (
    f"MP cost above {_MP_COST_CAP} exceeds the digimon MP cap — the move can "
    "never be cast in-game."
)
_LEVEL_LEARNED_MESSAGE = (
    f"Level learned above {_LEVEL_LEARNED_CAP} means the move is never "
    "naturally learned (level cap)."
)


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
    ("Raise/Reduce Light Res", 0x15),
    ("Raise/Reduce Fire Res", 0x16),
    ("Raise/Reduce Water Res", 0x17),
    ("Raise/Reduce Wind Res", 0x18),
    ("Raise/Reduce Dark Res", 0x19),
    ("Raise/Reduce Earth Res", 0x1A),
    ("Raise/Reduce Steel Res", 0x1B),
    ("Raise/Reduce Thunder Res", 0x1C),
    ("Attack + reduce Def (Acid Rain)", 0x101),
]

_SECONDARY_EFFECT_CHOICES: List[Tuple[str, int]] = [
    ("None", 0x00),
    ("Raise/Reduce Attack", 0x05),
    ("Raise/Reduce Defense", 0x06),
    ("Raise/Reduce Spirit", 0x07),
    ("Raise/Reduce Speed", 0x08),
    ("Doom", 0x0A),
    ("Confusion", 0x0B),
    ("Poison", 0x0C),
    ("Paralyze", 0x0D),
    ("Sleep", 0x0E),
    ("Raise/Reduce Resistance", 0x0F),
    ("Status restore", 0x10),
    ("Revive Ally", 0x110),
    ("Raise/Reduce Light Res", 0x15),
    ("Raise/Reduce Fire Res", 0x16),
    ("Raise/Reduce Water Res", 0x17),
    ("Raise/Reduce Wind Res", 0x18),
    ("Raise/Reduce Dark Res", 0x19),
    ("Raise/Reduce Earth Res", 0x1A),
    ("Raise/Reduce Steel Res", 0x1B),
    ("Raise/Reduce Thunder Res", 0x1C),
]

# Effect ids whose primary/secondary value is a signed two's-complement u16
# delta (raise = positive, reduce = 0x10000 - X). Stat modifiers (atk/def/spi/
# spd, generic resistance, elemental res) all share this encoding — the engine
# routes Reduce-Spirit and Raise-Spirit through the same opcode, distinguishing
# them by sign. Damage / status / heal effects keep unsigned semantics.
_SIGNED_VALUE_EFFECT_IDS = {
    0x05, 0x06, 0x07, 0x08,        # Atk/Def/Spi/Spd modifier
    0x0F,                          # Resistance modifier
    0x15, 0x16, 0x17, 0x18,        # Element res: Light/Fire/Water/Wind
    0x19, 0x1A, 0x1B, 0x1C,        # Element res: Dark/Earth/Steel/Thunder
}


class _MoveValueSpinBox(NoWheelSpinBox):
    """Spinbox for move primary/secondary value with effect-driven signed mode.

    The underlying u16 attribute is stored raw; the widget toggles between
    unsigned (0..65535) and signed (-32768..32767) display based on the
    sibling effect attribute. Reduce-style effects encode the magnitude as
    `0x10000 - X`, so showing -28 instead of 65508 keeps the UI legible.
    """

    def __init__(
        self,
        target,
        attr: str,
        effect_attr: str,
        undo_stack: QUndoStack,
    ):
        super().__init__()
        self._target = target
        self._attr = attr
        self._effect_attr = effect_attr
        self._undo_stack = undo_stack
        # None sentinel so the first _sync_mode_silent() call always sets the
        # range — Qt's default 0..99 satisfies `minimum() != maximum()` and
        # would otherwise trip the no-op guard, leaving the cap at 99.
        self._signed: object = None
        self.setMinimumWidth(90)
        self.setMaximumWidth(100)
        self._sync_mode_silent()
        with silenced(self):
            self.setValue(self._to_display(getattr(target, attr)))
        self.valueChanged.connect(self._on_value_changed)

    def _is_signed(self) -> bool:
        return getattr(self._target, self._effect_attr) in _SIGNED_VALUE_EFFECT_IDS

    def _sync_mode_silent(self) -> None:
        signed = self._is_signed()
        if signed == self._signed and self.minimum() != self.maximum():
            return
        self._signed = signed
        with silenced(self):
            if signed:
                self.setRange(-32768, 32767)
            else:
                self.setRange(0, 65535)

    def _to_display(self, raw: int) -> int:
        if self._signed and raw >= 0x8000:
            return raw - 0x10000
        return raw

    def _to_storage(self, display: int) -> int:
        return display & 0xFFFF

    def rebind(self, new_target) -> None:
        self._target = new_target
        self._sync_mode_silent()
        with silenced(self):
            self.setValue(self._to_display(getattr(new_target, self._attr)))

    def refresh(self) -> None:
        self._sync_mode_silent()
        with silenced(self):
            self.setValue(self._to_display(getattr(self._target, self._attr)))

    def _on_value_changed(self, new_value: int) -> None:
        storage = self._to_storage(new_value)
        if getattr(self._target, self._attr) == storage:
            return
        self._undo_stack.push(SetAttrCommand(self._target, self._attr, storage))


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
    """Filterable list of moves by id + name.

    `dirty_aware=True` prepends "● " to rows whose move record bytes differ
    from the original ROM snapshot; clean rows get matching whitespace so the
    text column stays aligned. Editors wire `undo_stack.indexChanged` to
    `refresh_dirty_state` to keep the markers fresh.
    """

    moveIndexSelected = Signal(int)  # emits index into the moves list

    def __init__(self, moves: List[model.MoveData], parent=None, dirty_aware: bool = False):
        super().__init__(parent)
        self._moves = moves
        self._dirty_aware = dirty_aware

        self._source_model = QStandardItemModel(self)
        self._row_by_id: Dict[int, int] = {}
        for ix, move in enumerate(moves):
            item = QStandardItem(self._decorated_label(ix))
            item.setEditable(False)
            item.setData(ix, Qt.UserRole)
            self._source_model.appendRow(item)
            self._row_by_id[move.id] = ix

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

    def _decorated_label(self, index: int) -> str:
        move = self._moves[index]
        base = f"0x{move.id:03x} — {_move_name(move.id)}"
        if not self._dirty_aware:
            return base
        return (_DIRTY_PREFIX if is_record_dirty(move) else _CLEAN_PREFIX) + base

    def refresh_dirty_state(self) -> None:
        """Re-render every row label so dirty markers track the current state."""
        if not self._dirty_aware:
            return
        for ix in range(self._source_model.rowCount()):
            item = self._source_model.item(ix)
            if item is not None:
                item.setText(self._decorated_label(ix))

    def select_first(self) -> None:
        if self._proxy.rowCount() == 0:
            return
        self._view.setCurrentIndex(self._proxy.index(0, 0))

    def select_by_id(self, move_id: int) -> bool:
        """Select the row whose move record has the given id. Clears the filter
        so the row is guaranteed visible. Returns False if the id is unknown.
        """
        row = self._row_by_id.get(move_id)
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

        self._list_panel = MoveListPanel(moves, dirty_aware=True)
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
        undo_stack.indexChanged.connect(self._list_panel.refresh_dirty_state)
        self._list_panel.select_first()

    def select_by_id(self, move_id: int) -> bool:
        return self._list_panel.select_by_id(move_id)

    # ---- detail form -----------------------------------------------------

    def _build_detail_container(self) -> QWidget:
        first = self._moves[0]

        self._title = QLabel("—")
        font = self._title.font()
        font.setPointSize(font.pointSize() + 3)
        font.setBold(True)
        self._title.setFont(font)

        self._id_spin = BoundSpinBox(first, "id", 2, self._undo_stack, hex_display=True, read_only=True)
        self._mp_spin = BoundSpinBox(
            first, "mp_cost", 2, self._undo_stack,
            warn_above=_MP_COST_CAP, warn_message=_MP_COST_MESSAGE,
        )
        self._element_combo = BoundEnumCombo(first, "element", model.Element, self._undo_stack)
        self._special_combo = BoundIntChoiceCombo(
            first, "special_identifier", _SPECIAL_IDENTIFIER_CHOICES, self._undo_stack
        )
        self._level_spin = BoundSpinBox(
            first, "level_learned", 2, self._undo_stack,
            warn_above=_LEVEL_LEARNED_CAP, warn_message=_LEVEL_LEARNED_MESSAGE,
        )

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
        self._primary_value_spin = _MoveValueSpinBox(
            first, "primary_value", "primary_effect", self._undo_stack,
        )
        self._secondary_combo = BoundIntChoiceCombo(
            first, "secondary_effect", _SECONDARY_EFFECT_CHOICES, self._undo_stack
        )
        self._secondary_value_spin = _MoveValueSpinBox(
            first, "secondary_value", "secondary_effect", self._undo_stack,
        )

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
        register_unknown_container(misc_box)
        misc_form = make_form(misc_box)
        add_unknown_form_row(misc_form, "Unknown 0x0e", self._unk_0xe_spin)
        add_unknown_form_row(misc_form, "Unknown 0x14", self._unk_0x14_spin)
        add_unknown_form_row(misc_form, "Unknown 0x16", self._unk_0x16_spin)

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

        return wrap_in_scroll(content)

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


from .validation import ValidationIssue  # noqa: E402 — bottom-of-file utility


def move_issues(moves: List[model.MoveData]) -> List[ValidationIssue]:
    """Footer-level issues for moves.

    Mirrors the per-widget warn caps (mp_cost, level_learned). Power cap and
    duplicate-move checks are intentionally absent — vanilla legitimately
    breaches them on hundreds of records.
    """
    issues: List[ValidationIssue] = []
    for move in moves:
        name = _move_name(move.id)
        if move.mp_cost > _MP_COST_CAP:
            issues.append(ValidationIssue(
                section="Moves",
                category="MP Cost",
                message=f"{name} — MP cost {move.mp_cost} exceeds the cap of {_MP_COST_CAP}.",
                editor_key="moves",
                record_id=move.id,
            ))
        if move.level_learned > _LEVEL_LEARNED_CAP:
            issues.append(ValidationIssue(
                section="Moves",
                category="Level Learned",
                message=f"{name} — level learned {move.level_learned} exceeds the cap of {_LEVEL_LEARNED_CAP}.",
                editor_key="moves",
                record_id=move.id,
            ))
    return issues
