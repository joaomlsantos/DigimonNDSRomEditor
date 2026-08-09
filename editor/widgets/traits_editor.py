"""TraitData editor — the 177 arm9 trait effect records (Digimon Data → Traits).

Left: the named traits. Right: the 8-byte record's editable fields — effect type
(labelled) + magnitude, plus the raw kind/index. See
``digimon_core.constants.TRAIT_EFFECT_TYPE_NAMES`` for the effect taxonomy.
"""
from __future__ import annotations

from typing import List, Tuple

from PySide6.QtCore import Qt
from PySide6.QtGui import QUndoStack
from PySide6.QtWidgets import QLabel, QSplitter, QVBoxLayout, QWidget

from digimon_core import constants, model

from .form_helpers import (
    BoldGroupBox as QGroupBox,
    BoundIdCombo,
    BoundSpinBox,
    make_form,
    wrap_in_scroll,
)
from .record_list_panel import RecordListPanel


def _trait_name(ix: int) -> str:
    names = constants.TRAIT_ARRAY_STR
    return names[ix] if 0 <= ix < len(names) else "<unnamed>"


def _build_effect_labels(records: List[model.TraitData]) -> dict:
    """effect_type -> label. Unmapped types show ``Unknown_0x<t>`` plus the first
    trait (lowest id) that uses them, e.g. ``Unknown_0x127 (Flame Aura)``."""
    labels = dict(constants.TRAIT_EFFECT_TYPE_NAMES)
    for rec in records:
        if rec.effect_type not in labels:
            labels[rec.effect_type] = f"Unknown_0x{rec.effect_type:X} ({_trait_name(rec.index)})"
    return labels


class TraitsEditor(QWidget):
    _CURSOR_KEY = "traits"
    _MODE_CHOICES = [(model.TraitData.FLAT, "Flat"),
                     (model.TraitData.PERCENT, "Percent (% of base)")]

    def __init__(self, records: List[model.TraitData], undo_stack: QUndoStack, session, parent=None):
        super().__init__(parent)
        self._records = records
        self._undo_stack = undo_stack
        self._session = session
        self._current_ix = -1
        self._widgets: List[object] = []
        self._effect_labels = _build_effect_labels(records)

        self._list_panel = RecordListPanel(
            records, dirty_aware=True, columns_for=self._columns,
            headers=("ID", "Trait", "Effect", "Value"),
        )
        self._list_panel.indexSelected.connect(self._on_selection)
        detail = self._build_detail()

        splitter = QSplitter(Qt.Horizontal, self)
        splitter.addWidget(self._list_panel)
        splitter.addWidget(detail)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([300, 640])
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(splitter)

        undo_stack.indexChanged.connect(self._refresh_form)
        undo_stack.indexChanged.connect(self._list_panel.refresh_dirty_state)

        remembered = self._session.recall_selection(self._CURSOR_KEY)
        if remembered is None or not self._list_panel.select_index(int(remembered)):
            self._list_panel.select_first()

    def _add(self, form, label, w):
        form.addRow(label, w)
        self._widgets.append(w)

    def _effect_label(self, effect_type: int) -> str:
        return self._effect_labels.get(effect_type, f"Unknown_0x{effect_type:X}")

    def _columns(self, ix: int, rec: model.TraitData) -> Tuple[str, ...]:
        value = f"{rec.magnitude}%" if rec.is_percent else str(rec.magnitude)
        return (f"{ix:03d}", _trait_name(rec.index),
                self._effect_label(rec.effect_type), value)

    def _effect_choices(self) -> List[Tuple[int, str]]:
        return sorted(self._effect_labels.items())

    def _build_detail(self) -> QWidget:
        first = self._records[0] if self._records else None
        self._title = QLabel("—")
        font = self._title.font()
        font.setPointSize(font.pointSize() + 2)
        font.setBold(True)
        self._title.setFont(font)

        box = QGroupBox("Effect")
        form = make_form(box)
        if first is not None:
            self._add(form, "ID", BoundSpinBox(first, "index", 2, self._undo_stack, read_only=True))
            self._add(form, "Effect",
                      BoundIdCombo(first, "effect_type", self._effect_choices(), self._undo_stack))
            self._add(form, "Value", BoundSpinBox(first, "magnitude", 2, self._undo_stack))
            self._add(form, "Value mode",
                      BoundIdCombo(first, "value_mode", self._MODE_CHOICES, self._undo_stack))

        content = QWidget()
        cl = QVBoxLayout(content)
        cl.setContentsMargins(6, 6, 6, 6)
        cl.addWidget(self._title)
        cl.addWidget(box)
        cl.addStretch(1)
        return wrap_in_scroll(content)

    def _on_selection(self, ix: int) -> None:
        if not (0 <= ix < len(self._records)):
            return
        self._current_ix = ix
        self._session.remember_selection(self._CURSOR_KEY, ix)
        rec = self._records[ix]
        self._title.setText(f"#{ix:03d}  {_trait_name(rec.index)}   (offset 0x{rec.offset:08x})")
        for w in self._widgets:
            w.rebind(rec)

    def _refresh_form(self, _index: int) -> None:
        if not (0 <= self._current_ix < len(self._records)):
            return
        for w in self._widgets:
            w.refresh()
        self._list_panel.refresh_label(self._current_ix)
