"""DNADigivolution editor — list of (digimon_1 + digimon_2 -> dna_evolution)
records with three conditions.

DNA records have no 0xFFFFFFFF sentinel; every digimon-id is always populated.
"""
from __future__ import annotations

from typing import List

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

from .form_helpers import (
    make_form,
    DIGIVOLUTION_CONDITION_CHOICES,
    BoundIdCombo,
    BoundIntChoiceCombo,
    BoundSpinBox,
    digimon_choices,
    digimon_name,
)
from .record_list_panel import RecordListPanel


DIGIMON_VALUED_CONDITIONS = {0x15, 0x16}


def _record_label(ix: int, rec: model.DNADigivolution) -> str:
    return (
        f"{ix:03d}  {digimon_name(rec.digimon_1_id)} + {digimon_name(rec.digimon_2_id)} "
        f"→ {digimon_name(rec.dna_evolution_id)}"
    )


class _ConditionRow(QWidget):
    def __init__(self, target, id_attr: str, value_attr: str, undo_stack: QUndoStack):
        super().__init__()
        self._target = target
        self._id_attr = id_attr
        self._id_combo = BoundIntChoiceCombo(target, id_attr, DIGIVOLUTION_CONDITION_CHOICES, undo_stack)
        self._value_spin = BoundSpinBox(target, value_attr, 4, undo_stack)
        self._value_digi = BoundIdCombo(target, value_attr, digimon_choices(), undo_stack)
        self._value_stack = QStackedWidget()
        self._value_stack.addWidget(self._value_spin)
        self._value_stack.addWidget(self._value_digi)
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(self._id_combo, 2)
        h.addWidget(self._value_stack, 1)
        self._update_value_widget()
        self._id_combo.currentIndexChanged.connect(lambda _i: self._update_value_widget())

    def _update_value_widget(self) -> None:
        cond_id = getattr(self._target, self._id_attr)
        self._value_stack.setCurrentIndex(1 if cond_id in DIGIMON_VALUED_CONDITIONS else 0)

    def rebind(self, new_target) -> None:
        self._target = new_target
        self._id_combo.rebind(new_target)
        self._value_spin.rebind(new_target)
        self._value_digi.rebind(new_target)
        self._update_value_widget()

    def refresh(self) -> None:
        self._id_combo.refresh()
        self._value_spin.refresh()
        self._value_digi.refresh()
        self._update_value_widget()


class DNADigivolutionEditor(QWidget):
    def __init__(
        self,
        records: List[model.DNADigivolution],
        undo_stack: QUndoStack,
        parent=None,
    ):
        super().__init__(parent)
        self._records = records
        self._undo_stack = undo_stack
        self._current_ix: int = -1

        self._list_panel = RecordListPanel(records, _record_label)
        self._list_panel.indexSelected.connect(self._on_selection)

        self._detail = self._build_detail_container()

        splitter = QSplitter(Qt.Horizontal, self)
        splitter.addWidget(self._list_panel)
        splitter.addWidget(self._detail)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([320, 680])

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(splitter)

        undo_stack.indexChanged.connect(self._refresh_form)
        self._list_panel.select_first()

    def _build_detail_container(self) -> QWidget:
        first = self._records[0]

        self._title = QLabel("—")
        font = self._title.font()
        font.setPointSize(font.pointSize() + 3)
        font.setBold(True)
        self._title.setFont(font)

        identity = QGroupBox("Identity")
        identity_form = make_form(identity)
        self._d1_row = BoundIdCombo(first, "digimon_1_id", digimon_choices(), self._undo_stack)
        self._d2_row = BoundIdCombo(first, "digimon_2_id", digimon_choices(), self._undo_stack)
        self._evo_row = BoundIdCombo(first, "dna_evolution_id", digimon_choices(), self._undo_stack)
        identity_form.addRow("Digimon 1", self._d1_row)
        identity_form.addRow("Digimon 2", self._d2_row)
        identity_form.addRow("DNA evolution", self._evo_row)

        conditions = QGroupBox("Conditions")
        cond_form = make_form(conditions)
        self._cond_rows: List[_ConditionRow] = []
        for ix, (cid_attr, cval_attr) in enumerate(
            [
                ("condition_id_1", "condition_value_1"),
                ("condition_id_2", "condition_value_2"),
                ("condition_id_3", "condition_value_3"),
            ],
            start=1,
        ):
            row = _ConditionRow(first, cid_attr, cval_attr, self._undo_stack)
            self._cond_rows.append(row)
            cond_form.addRow(f"Condition {ix}", row)

        content = QWidget()
        content_layout = QVBoxLayout(content)

        content_layout.setContentsMargins(6, 6, 6, 6)

        content_layout.setSpacing(4)
        content_layout.addWidget(self._title)
        content_layout.addWidget(identity)
        content_layout.addWidget(conditions)
        content_layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)

        for row in (self._d1_row, self._d2_row, self._evo_row):
            row.currentIndexChanged.connect(lambda _i: self._refresh_list_label())

        return scroll

    def _on_selection(self, ix: int) -> None:
        if not (0 <= ix < len(self._records)):
            return
        self._current_ix = ix
        target = self._records[ix]
        self._title.setText(self._title_for(ix, target))
        for w in (self._d1_row, self._d2_row, self._evo_row):
            w.rebind(target)
        for r in self._cond_rows:
            r.rebind(target)

    def _refresh_form(self, _index: int) -> None:
        if not (0 <= self._current_ix < len(self._records)):
            return
        target = self._records[self._current_ix]
        self._title.setText(self._title_for(self._current_ix, target))
        for w in (self._d1_row, self._d2_row, self._evo_row):
            w.refresh()
        for r in self._cond_rows:
            r.refresh()
        self._refresh_list_label()

    def _refresh_list_label(self) -> None:
        if not (0 <= self._current_ix < len(self._records)):
            return
        target = self._records[self._current_ix]
        self._list_panel.refresh_label(self._current_ix, _record_label(self._current_ix, target))

    @staticmethod
    def _title_for(ix: int, target: model.DNADigivolution) -> str:
        return (
            f"#{ix:03d}  {digimon_name(target.digimon_1_id)} + {digimon_name(target.digimon_2_id)} "
            f"→ {digimon_name(target.dna_evolution_id)}    (offset 0x{target.offset:08x})"
        )
