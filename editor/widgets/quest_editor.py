"""Quest editor — flat list of QuestData records.

Fields are grouped into Identity, Rewards, Quest Flags, Unlock Conditions, and
Raw Pointers (kept as hex-displayed spinboxes since pointer math beyond editing
the rewards isn't yet documented in research_docs/quests_research.txt).
"""
from __future__ import annotations

from typing import List

from PySide6.QtCore import Qt
from PySide6.QtGui import QUndoStack
from PySide6.QtWidgets import (
    QLabel,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from digimon_core import constants, model

from .form_helpers import (
    BoldGroupBox as QGroupBox,
    BoundCheckBox,
    BoundHexLineEdit,
    BoundIdCombo,
    BoundSpinBox,
    _make_compact_grid,
    add_unknown_grid_field,
    item_choices,
    make_form,
    register_unknown_container,
    with_open_button,
    wrap_in_scroll,
)
from .record_list_panel import RecordListPanel


def _quest_name(ix: int) -> str:
    names = constants.QUEST_NAMES
    if 0 <= ix < len(names) and names[ix]:
        return names[ix]
    return "<unnamed>"


def _record_label(ix: int, rec: model.QuestData) -> str:
    return f"Quest {ix:03d}  ({rec.quest_stars}★)  {_quest_name(ix)}"


class QuestEditor(QWidget):
    _CURSOR_KEY = "quests"

    def __init__(
        self,
        records: List[model.QuestData],
        undo_stack: QUndoStack,
        session,
        parent=None,
    ):
        super().__init__(parent)
        self._records = records
        self._undo_stack = undo_stack
        self._session = session
        self._current_ix: int = -1
        self._all_widgets: List[object] = []  # everything supporting rebind/refresh

        self._list_panel = RecordListPanel(records, _record_label, dirty_aware=True)
        self._list_panel.indexSelected.connect(self._on_selection)

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
        remembered = self._session.recall_selection(self._CURSOR_KEY)
        if remembered is None or not self._list_panel.select_index(int(remembered)):
            self._list_panel.select_first()

    def _add_field(self, form, label: str, widget) -> None:
        form.addRow(label, widget)
        self._all_widgets.append(widget)

    def _hex_field(self, target, attr: str, byte_width: int = 4) -> BoundHexLineEdit:
        """Hex line-edit for fields that can exceed INT32_MAX (raw pointers,
        unknown 4-byte blobs). Use a normal BoundSpinBox in hex mode only for
        narrow (≤2-byte) fields."""
        return BoundHexLineEdit(target, attr, byte_width, self._undo_stack)

    def _build_detail_container(self) -> QWidget:
        first = self._records[0]

        self._title = QLabel("—")
        font = self._title.font()
        font.setPointSize(font.pointSize() + 3)
        font.setBold(True)
        self._title.setFont(font)

        identity = QGroupBox("Identity")
        identity_form = make_form(identity)
        self._stars_spin = BoundSpinBox(first, "quest_stars", 1, self._undo_stack)
        self._species_spin = BoundSpinBox(first, "species_center", 1, self._undo_stack)
        self._add_field(identity_form, "Quest stars", self._stars_spin)
        self._add_field(identity_form, "Reception center", self._species_spin)

        rewards = QGroupBox("Rewards")
        rewards_form = make_form(rewards)
        self._money_spin = BoundSpinBox(first, "money_reward", 4, self._undo_stack)
        # Vanilla quest data uses 0x0000FFFF (low-half all-bits-set) to mark
        # "no item reward" — surface it as a "(undefined)" row.
        self._item_row = BoundIdCombo(
            first, "item_reward", item_choices(), self._undo_stack,
            none_value=0xFFFF, none_label="(undefined)",
            details_kind="item",
        )
        self._tp_spin = BoundSpinBox(first, "tamerpoints_reward", 4, self._undo_stack)
        self._add_field(rewards_form, "Money (bits)", self._money_spin)
        rewards_form.addRow("Item", with_open_button(self._item_row))
        self._all_widgets.append(self._item_row)
        self._add_field(rewards_form, "Tamer points", self._tp_spin)

        flags = QGroupBox("Quest Flags")
        flags_grid = _make_compact_grid(flags, cols=2)
        flag_specs = [
            (ix, "Clear flag" if ix == 5 else f"Flag {ix}") for ix in range(1, 6)
        ]
        for slot, (ix, label) in enumerate(flag_specs):
            spin = self._hex_field(first, f"questflag_{ix}", byte_width=2)
            self._all_widgets.append(spin)
            flags_grid.addWidget(QLabel(label), slot // 2, (slot % 2) * 2)
            flags_grid.addWidget(spin, slot // 2, (slot % 2) * 2 + 1)

        unlock = QGroupBox("Unlock Conditions")
        unlock_form = make_form(unlock)
        self._add_field(
            unlock_form, "Min completed quests",
            BoundSpinBox(first, "unlock_condition_numquests", 2, self._undo_stack),
        )
        self._add_field(
            unlock_form, "Required quest flag",
            self._hex_field(first, "unlock_condition_questflag", byte_width=2),
        )
        self._add_field(
            unlock_form, "Min tamer points",
            BoundSpinBox(
                first, "unlock_condition_tamerpoints", 2, self._undo_stack,
                signed=True,
            ),
        )
        self._add_field(
            unlock_form, "Online required",
            BoundCheckBox(first, "unlock_condition_online", self._undo_stack),
        )
        self._add_field(
            unlock_form, "Personality",
            BoundSpinBox(first, "unlock_condition_personality", 2, self._undo_stack),
        )

        pointers = QGroupBox("Raw Pointers (hex)")
        pointers_grid = _make_compact_grid(pointers, cols=2)
        pointer_specs = [
            (f"pointer_steps_{i}", f"Quest steps {i}", 4) for i in range(1, 5)
        ] + [("questgiver_name_pointer", "Questgiver name", 4)]
        for slot, (attr, label, width) in enumerate(pointer_specs):
            edit = self._hex_field(first, attr, byte_width=width)
            self._all_widgets.append(edit)
            pointers_grid.addWidget(QLabel(label), slot // 2, (slot % 2) * 2)
            pointers_grid.addWidget(edit, slot // 2, (slot % 2) * 2 + 1)

        unknowns = QGroupBox("Unknown / Unmapped")
        register_unknown_container(unknowns)
        unknowns_grid = _make_compact_grid(unknowns, cols=2)
        unknown_specs = [
            ("unknown_0x0", 4),
            ("unknown_0x6", 2),
            ("unknown_0x8", 4),
            ("unknown_0x36", 2),
            ("unknown_0x42", 2),
        ]
        for slot, (attr, width) in enumerate(unknown_specs):
            edit = self._hex_field(first, attr, byte_width=width)
            self._all_widgets.append(edit)
            add_unknown_grid_field(unknowns_grid, slot // 2, slot % 2, attr, edit)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(6, 6, 6, 6)
        content_layout.setSpacing(4)
        content_layout.addWidget(self._title)
        for group in (identity, rewards, flags, unlock, pointers, unknowns):
            content_layout.addWidget(group)
        content_layout.addStretch(1)

        scroll = wrap_in_scroll(content)

        self._stars_spin.valueChanged.connect(lambda _v: self._refresh_list_label())

        return scroll

    def _on_selection(self, ix: int) -> None:
        if not (0 <= ix < len(self._records)):
            return
        self._current_ix = ix
        self._session.remember_selection(self._CURSOR_KEY, ix)
        target = self._records[ix]
        self._title.setText(self._title_for(ix, target))
        for w in self._all_widgets:
            w.rebind(target)

    def _refresh_form(self, _index: int) -> None:
        if not (0 <= self._current_ix < len(self._records)):
            return
        target = self._records[self._current_ix]
        self._title.setText(self._title_for(self._current_ix, target))
        for w in self._all_widgets:
            w.refresh()
        self._refresh_list_label()

    def _refresh_list_label(self) -> None:
        if not (0 <= self._current_ix < len(self._records)):
            return
        target = self._records[self._current_ix]
        self._list_panel.refresh_label(self._current_ix, _record_label(self._current_ix, target))

    @staticmethod
    def _title_for(ix: int, target: model.QuestData) -> str:
        return f"Quest #{ix:03d}  ({target.quest_stars}★)  {_quest_name(ix)}    (offset 0x{target.offset:08x})"
