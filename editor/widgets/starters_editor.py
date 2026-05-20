"""Starter pack editor — 12 fixed-slot starter records grouped 3-per-pack.

Each starter is 8 bytes: digimon_id, level, screen position (x, y). The 12
records form four starter packs of three digimon each (slots 1-3, 4-6, 7-9,
10-12); each pack gets its own QGroupBox so the boundaries are obvious in the
UI.

Per starter_pack research: level must not exceed the base aptitude for the
chosen digimon (the game softlocks otherwise). We don't enforce that here —
the user is expected to know what they're doing — but the spinbox tooltip
mentions it.
"""
from __future__ import annotations

from typing import List

from PySide6.QtCore import Qt
from PySide6.QtGui import QUndoStack
from PySide6.QtWidgets import (
    QGridLayout,
    QGroupBox,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from digimon_core import model

from .form_helpers import BoundIdCombo, BoundSpinBox, digimon_choices


# Standard structure: 4 packs × 3 digimon. If a future ROM ever ships a
# different grouping, change this constant and the rows will re-group cleanly.
PACK_SIZE = 3


class _StarterRow:
    """One starter slot rendered as a row of widgets inside the parent grid."""

    def __init__(self, target: model.StarterEntry, undo_stack: QUndoStack):
        self._target = target
        self.id_combo = BoundIdCombo(target, "digimon_id", digimon_choices(), undo_stack)
        self.id_combo.setToolTip("Digimon for this starter slot.")
        self.level_spin = BoundSpinBox(target, "level", 2, undo_stack)
        self.level_spin.setToolTip(
            "Starter level. Game softlocks if level exceeds the digimon's base aptitude."
        )
        self.x_spin = BoundSpinBox(target, "screen_x", 2, undo_stack)
        self.y_spin = BoundSpinBox(target, "screen_y", 2, undo_stack)
        self._all = [self.id_combo, self.level_spin, self.x_spin, self.y_spin]

    def refresh(self) -> None:
        for w in self._all:
            w.refresh()


class StartersEditor(QWidget):
    def __init__(
        self,
        records: List[model.StarterEntry],
        undo_stack: QUndoStack,
        parent=None,
    ):
        super().__init__(parent)
        self._records = records
        self._undo_stack = undo_stack
        self._rows: List[_StarterRow] = []

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(8, 8, 8, 8)

        headers = ["Slot", "Digimon", "Level", "Screen X", "Screen Y", "Offset"]

        for pack_ix in range((len(records) + PACK_SIZE - 1) // PACK_SIZE):
            start = pack_ix * PACK_SIZE
            end = min(start + PACK_SIZE, len(records))
            pack_box = QGroupBox(f"Starter Pack {pack_ix + 1}  (slots {start + 1}–{end})")
            grid = QGridLayout(pack_box)
            grid.setHorizontalSpacing(12)
            grid.setVerticalSpacing(4)
            for col, text in enumerate(headers):
                lbl = QLabel(text)
                f = lbl.font()
                f.setBold(True)
                lbl.setFont(f)
                grid.addWidget(lbl, 0, col)
            for offset_in_pack, ix in enumerate(range(start, end)):
                rec = records[ix]
                row = _StarterRow(rec, undo_stack)
                self._rows.append(row)
                grid.addWidget(QLabel(f"#{ix + 1:02d}"), offset_in_pack + 1, 0)
                grid.addWidget(row.id_combo, offset_in_pack + 1, 1)
                grid.addWidget(row.level_spin, offset_in_pack + 1, 2)
                grid.addWidget(row.x_spin, offset_in_pack + 1, 3)
                grid.addWidget(row.y_spin, offset_in_pack + 1, 4)
                grid.addWidget(QLabel(f"0x{rec.offset:08x}"), offset_in_pack + 1, 5)
            grid.setColumnStretch(1, 1)
            content_layout.addWidget(pack_box)
        content_layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(scroll)

        undo_stack.indexChanged.connect(self._refresh_all)

    def _refresh_all(self, _index: int) -> None:
        for row in self._rows:
            row.refresh()
