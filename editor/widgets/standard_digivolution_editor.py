"""StandardDigivolution editor — list of digimon, right panel shows their
degeneration target and three evolution targets, each with up to three
conditions (id + value).

Empty evolution targets are stored as 0xFFFFFFFF in the ROM; the
`BoundOptionalDigimonId` widget in form_helpers handles the sentinel.

Condition ids come from `constants.DIGIVOLUTION_CONDITIONS` (NONE, LEVEL,
DRAGON EXP, …); see research_docs/digivolution_conditions_parsed.txt.
"""
from __future__ import annotations

from typing import Dict, List, Tuple

from PySide6.QtCore import Qt
from PySide6.QtGui import QUndoStack
from PySide6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from digimon_core import model

from .digimon_list_panel import DigimonListPanel
from .form_helpers import (
    make_form,
    DIGIVOLUTION_CONDITION_CHOICES,
    NO_EVO_SENTINEL,
    BoundIdCombo,
    BoundIntChoiceCombo,
    BoundSpinBox,
    digimon_choices,
    digimon_name,
)


# Condition codes whose `value` field semantically refers to a specific digimon
# id (rather than a level / stat / exp amount). The editor swaps the value
# input to a digimon picker when one of these is selected.
DIGIMON_VALUED_CONDITIONS = {0x15, 0x16}


class _CondValueField(QWidget):
    """Value input that swaps between spinbox and digimon-combo by cond id.

    Both child widgets bind to the same `value_attr` on the model — only one is
    visible at a time, and `_update_visible()` flips between them based on the
    current cond id. Edits made on either child push regular SetAttrCommands.
    """

    def __init__(self, target, id_attr: str, value_attr: str, undo_stack):
        super().__init__()
        self._target = target
        self._id_attr = id_attr
        self._value_attr = value_attr

        self._spin = BoundSpinBox(target, value_attr, 4, undo_stack)
        self._digi_combo = BoundIdCombo(target, value_attr, digimon_choices(), undo_stack)

        self._stack = QStackedWidget()
        self._stack.addWidget(self._spin)        # index 0
        self._stack.addWidget(self._digi_combo)  # index 1

        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(self._stack)
        self._update_visible()

    def update_for_cond(self) -> None:
        self._update_visible()

    def _update_visible(self) -> None:
        cond_id = getattr(self._target, self._id_attr)
        self._stack.setCurrentIndex(1 if cond_id in DIGIMON_VALUED_CONDITIONS else 0)

    def rebind(self, new_target) -> None:
        self._target = new_target
        self._spin.rebind(new_target)
        self._digi_combo.rebind(new_target)
        self._update_visible()

    def refresh(self) -> None:
        self._spin.refresh()
        self._digi_combo.refresh()
        self._update_visible()


class _EvoTargetGroup(QGroupBox):
    """One groupbox: target id + (cond_id, cond_value) x3."""

    def __init__(
        self,
        title: str,
        target: model.StandardDigivolution,
        id_attr: str,
        cond_attr_pairs: List[Tuple[str, str]],
        undo_stack: QUndoStack,
    ):
        super().__init__(title)
        self._undo_stack = undo_stack

        self._id_field = BoundIdCombo(
            target, id_attr, digimon_choices(), undo_stack,
            none_value=NO_EVO_SENTINEL,
        )

        self._cond_id_combos: List[BoundIntChoiceCombo] = []
        self._cond_value_fields: List[_CondValueField] = []

        form = make_form(self)
        form.addRow("Target", self._id_field)
        for ix, (cid_attr, cval_attr) in enumerate(cond_attr_pairs, start=1):
            id_combo = BoundIntChoiceCombo(target, cid_attr, DIGIVOLUTION_CONDITION_CHOICES, undo_stack)
            val_field = _CondValueField(target, cid_attr, cval_attr, undo_stack)
            id_combo.currentIndexChanged.connect(lambda _i, f=val_field: f.update_for_cond())
            self._cond_id_combos.append(id_combo)
            self._cond_value_fields.append(val_field)
            row = QWidget()
            h = QHBoxLayout(row)
            h.setContentsMargins(0, 0, 0, 0)
            h.addWidget(id_combo, 2)
            h.addWidget(val_field, 1)
            form.addRow(f"Condition {ix}", row)

    def rebind(self, new_target) -> None:
        self._id_field.rebind(new_target)
        for c in self._cond_id_combos:
            c.rebind(new_target)
        for f in self._cond_value_fields:
            f.rebind(new_target)

    def refresh(self) -> None:
        self._id_field.refresh()
        for c in self._cond_id_combos:
            c.refresh()
        for f in self._cond_value_fields:
            f.refresh()


# (group title, id attr, [(cond_id_attr, cond_value_attr), ...])
_EVO_GROUPS: List[Tuple[str, str, List[Tuple[str, str]]]] = [
    (
        "Degeneration",
        "degen_evo_id",
        [
            ("degen_condition_id_1", "degen_condition_value_1"),
            ("degen_condition_id_2", "degen_condition_value_2"),
            ("degen_condition_id_3", "degen_condition_value_3"),
        ],
    ),
    (
        "Evolution 1",
        "evolution_1_id",
        [
            ("evo_1_condition_id_1", "evo_1_condition_value_1"),
            ("evo_1_condition_id_2", "evo_1_condition_value_2"),
            ("evo_1_condition_id_3", "evo_1_condition_value_3"),
        ],
    ),
    (
        "Evolution 2",
        "evolution_2_id",
        [
            ("evo_2_condition_id_1", "evo_2_condition_value_1"),
            ("evo_2_condition_id_2", "evo_2_condition_value_2"),
            ("evo_2_condition_id_3", "evo_2_condition_value_3"),
        ],
    ),
    (
        "Evolution 3",
        "evolution_3_id",
        [
            ("evo_3_condition_id_1", "evo_3_condition_value_1"),
            ("evo_3_condition_id_2", "evo_3_condition_value_2"),
            ("evo_3_condition_id_3", "evo_3_condition_value_3"),
        ],
    ),
]


class StandardDigivolutionEditor(QWidget):
    def __init__(
        self,
        entries: Dict[int, model.StandardDigivolution],
        undo_stack: QUndoStack,
        parent=None,
    ):
        super().__init__(parent)
        self._entries = entries
        self._undo_stack = undo_stack
        self._current_id: int = -1

        self._list_panel = DigimonListPanel(entries)
        self._list_panel.digimonSelected.connect(self._on_selection)

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

    def _build_detail_container(self) -> QWidget:
        first = next(iter(self._entries.values()))

        self._title = QLabel("—")
        font = self._title.font()
        font.setPointSize(font.pointSize() + 3)
        font.setBold(True)
        self._title.setFont(font)

        self._groups: List[_EvoTargetGroup] = []
        for title, id_attr, cond_pairs in _EVO_GROUPS:
            group = _EvoTargetGroup(title, first, id_attr, cond_pairs, self._undo_stack)
            self._groups.append(group)

        content = QWidget()
        content_layout = QVBoxLayout(content)

        content_layout.setContentsMargins(6, 6, 6, 6)

        content_layout.setSpacing(4)
        content_layout.addWidget(self._title)
        for group in self._groups:
            content_layout.addWidget(group)
        content_layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)
        return scroll

    def _on_selection(self, digimon_id: int) -> None:
        target = self._entries.get(digimon_id)
        if target is None:
            return
        self._current_id = digimon_id
        self._title.setText(self._title_for(target))
        for group in self._groups:
            group.rebind(target)

    def _refresh_form(self, _index: int) -> None:
        target = self._entries.get(self._current_id)
        if target is None:
            return
        self._title.setText(self._title_for(target))
        for group in self._groups:
            group.refresh()

    @staticmethod
    def _title_for(target: model.StandardDigivolution) -> str:
        name = digimon_name(target.digimon_id)
        return f"0x{target.digimon_id:03x}  —  {name}    (offset 0x{target.offset:08x})"
