"""FarmTrainingPen editor — 48 fixed-stride 0x1C-byte records.

Each pen has four outcome chances (great-failure/failure/success/great-success)
whose bytes sum to `total_odds` (vanilla = 0x64 = 100). The four stat-delta
fields are signed s16 — vanilla uses 0xFFFF (-1) on great_failure to subtract a
point on a miss.
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
    BoundSpinBox,
    _make_compact_grid,
    make_form,
    wrap_in_scroll,
)
from .record_list_panel import RecordListPanel


def _pen_name(ix: int) -> str:
    if 0 <= ix < len(constants.FARM_TRAINING_PEN_NAMES):
        return constants.FARM_TRAINING_PEN_NAMES[ix]
    return f"<training pen {ix}>"


def _record_label(ix: int, _rec: model.FarmTrainingPen) -> str:
    return f"{ix:02d}  {_pen_name(ix)}"


class FarmTrainingPenEditor(QWidget):
    def __init__(
        self,
        records: List[model.FarmTrainingPen],
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
        splitter.setSizes([260, 740])

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(splitter)

        undo_stack.indexChanged.connect(self._refresh_form)
        undo_stack.indexChanged.connect(self._list_panel.refresh_dirty_state)
        self._list_panel.select_first()

    def _add_field(self, form, label: str, widget) -> None:
        form.addRow(label, widget)
        self._all_widgets.append(widget)

    def _build_detail_container(self) -> QWidget:
        content = QWidget()
        cl = QVBoxLayout(content)
        cl.setContentsMargins(6, 6, 6, 6)
        cl.setSpacing(4)

        self._title = QLabel("—")
        font = self._title.font()
        font.setPointSize(font.pointSize() + 3)
        font.setBold(True)
        self._title.setFont(font)
        cl.addWidget(self._title)

        # Empty session (e.g. DAWN_US until offsets are added) — show a stub
        # placeholder so the user gets a clear "nothing to edit here" instead
        # of a crash when the records list is empty.
        if not self._records:
            cl.addWidget(QLabel("No training-pen records loaded for this ROM version."))
            cl.addStretch(1)
            return wrap_in_scroll(content)

        first = self._records[0]

        identity = QGroupBox("Identity")
        identity_form = make_form(identity)
        self._add_field(identity_form, "String id",  BoundSpinBox(first, "string_id",  2, self._undo_stack, hex_display=True))
        self._add_field(identity_form, "Sprite id",  BoundSpinBox(first, "sprite_id",  2, self._undo_stack, hex_display=True))
        self._add_field(identity_form, "Stat op id", BoundSpinBox(first, "stat_op_id", 2, self._undo_stack, hex_display=True))

        outcomes = QGroupBox("Outcome Values (signed stat delta) and Chances")
        outcomes_grid = _make_compact_grid(outcomes, cols=2)
        outcome_rows = [
            ("Great Failure", "great_failure_value", "great_failure_chance"),
            ("Failure",       "failure_value",       "failure_chance"),
            ("Success",       "success_value",       "success_chance"),
            ("Great Success", "great_success_value", "great_success_chance"),
        ]
        for row, (label, value_attr, chance_attr) in enumerate(outcome_rows):
            outcomes_grid.addWidget(QLabel(f"{label} value"), row, 0)
            v_spin = BoundSpinBox(first, value_attr, 2, self._undo_stack, signed=True)
            outcomes_grid.addWidget(v_spin, row, 1)
            self._all_widgets.append(v_spin)

            outcomes_grid.addWidget(QLabel(f"{label} chance"), row, 2)
            c_spin = BoundSpinBox(first, chance_attr, 1, self._undo_stack)
            outcomes_grid.addWidget(c_spin, row, 3)
            self._all_widgets.append(c_spin)

        caps = QGroupBox("Caps / Totals")
        caps_form = make_form(caps)
        self._add_field(caps_form, "Stat cap",   BoundSpinBox(first, "stat_cap",   2, self._undo_stack))
        self._add_field(caps_form, "Total odds", BoundSpinBox(first, "total_odds", 2, self._undo_stack))

        presentation = QGroupBox("Presentation")
        pres_form = make_form(presentation)
        self._add_field(pres_form, "Animation id",      BoundSpinBox(first, "animation_id",      2, self._undo_stack, hex_display=True))
        self._add_field(pres_form, "Sound id",          BoundSpinBox(first, "sound_id",          2, self._undo_stack, hex_display=True))
        self._add_field(pres_form, "Vertical position", BoundSpinBox(first, "vertical_position", 2, self._undo_stack))

        cl.addWidget(identity)
        cl.addWidget(outcomes)
        cl.addWidget(caps)
        cl.addWidget(presentation)
        cl.addStretch(1)

        return wrap_in_scroll(content)

    def _on_selection(self, ix: int) -> None:
        if not (0 <= ix < len(self._records)):
            return
        self._current_ix = ix
        target = self._records[ix]
        self._title.setText(
            f"{_pen_name(ix)}    (offset 0x{target.offset:08x})"
        )
        for w in self._all_widgets:
            w.rebind(target)

    def _refresh_form(self, _index: int) -> None:
        if not (0 <= self._current_ix < len(self._records)):
            return
        for w in self._all_widgets:
            w.refresh()
