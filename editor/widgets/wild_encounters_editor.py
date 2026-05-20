"""Wild encounters editor — area-by-area view of the 0x200-byte regions.

Left pane: every wild-encounter area, labelled by the location it lives in
(derived via `loaders.getCurrentLocation`). Right pane: the area's header
(encounter count, rate lower/upper) and a scrollable list of editable
encounter rows (digimon id + reward-slot index). The 22 opaque bytes inside
each encounter (filler 0x0C80s, the 0x12 field that crashes the game when set
to unknown values, the trailing 0xFFFF terminator) are preserved verbatim.
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
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from digimon_core import loaders, model

from .form_helpers import BoundIdCombo, BoundSpinBox, digimon_choices, make_form
from .record_list_panel import RecordListPanel


def _build_area_labels(version: str, areas: List[model.WildEncounterArea]) -> List[str]:
    """Label each area as `<Location> Area <n>` with n resetting per location."""
    labels: List[str] = []
    last_location: str = ""
    counter = 0
    for area in areas:
        location = loaders.getCurrentLocation(area.offset, version)
        if location != last_location:
            counter = 1
            last_location = location
        else:
            counter += 1
        labels.append(f"{location} Area {counter}")
    return labels


class _EncounterRow:
    """One encounter — digimon-id picker + reward-slot spin."""

    def __init__(self, target: model.WildEncounter, undo_stack: QUndoStack):
        self._target = target
        self.id_combo = BoundIdCombo(target, "digimon_id", digimon_choices(), undo_stack)
        self.reward_spin = BoundSpinBox(target, "reward_slot", 2, undo_stack)


class WildEncountersEditor(QWidget):
    def __init__(
        self,
        version: str,
        areas: List[model.WildEncounterArea],
        undo_stack: QUndoStack,
        parent=None,
    ):
        super().__init__(parent)
        self._version = version
        self._areas = areas
        self._undo_stack = undo_stack
        self._labels = _build_area_labels(version, areas)
        self._current_ix: int = -1
        self._encounter_rows: List[_EncounterRow] = []

        self._list_panel = RecordListPanel(
            areas,
            lambda ix, _rec: self._labels[ix],
        )
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

        undo_stack.indexChanged.connect(self._on_undo_redo)
        self._list_panel.select_first()

    def _build_detail_container(self) -> QWidget:
        self._title = QLabel("—")
        font = self._title.font()
        font.setPointSize(font.pointSize() + 3)
        font.setBold(True)
        self._title.setFont(font)

        self._header_group = QGroupBox("Area Header")
        self._header_form = make_form(self._header_group)
        self._num_enc_spin: BoundSpinBox | None = None
        self._rate_lo_spin: BoundSpinBox | None = None
        self._rate_hi_spin: BoundSpinBox | None = None

        self._enc_group = QGroupBox("Encounters")
        self._enc_layout = QGridLayout(self._enc_group)
        self._enc_layout.setHorizontalSpacing(8)
        self._enc_layout.setVerticalSpacing(4)

        self._content = QWidget()
        content_layout = QVBoxLayout(self._content)

        content_layout.setContentsMargins(6, 6, 6, 6)

        content_layout.setSpacing(4)
        content_layout.addWidget(self._title)
        content_layout.addWidget(self._header_group)
        content_layout.addWidget(self._enc_group)
        content_layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self._content)
        return scroll

    def _clear_layout(self, layout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()

    def _on_selection(self, ix: int) -> None:
        if not (0 <= ix < len(self._areas)):
            return
        self._current_ix = ix
        area = self._areas[ix]
        self._title.setText(f"{self._labels[ix]}    (offset 0x{area.offset:08x})")

        # rebuild header form
        self._clear_layout(self._header_form)
        self._num_enc_spin = BoundSpinBox(area, "num_encounters", 2, self._undo_stack)
        self._rate_lo_spin = BoundSpinBox(area, "rate_lower", 2, self._undo_stack)
        self._rate_hi_spin = BoundSpinBox(area, "rate_upper", 2, self._undo_stack)
        self._header_form.addRow("Num encounters", self._num_enc_spin)
        self._header_form.addRow("Rate lower bound", self._rate_lo_spin)
        self._header_form.addRow("Rate upper bound", self._rate_hi_spin)

        # rebuild encounter grid
        self._clear_layout(self._enc_layout)
        self._encounter_rows = []

        if not area.encounters:
            self._enc_layout.addWidget(QLabel("(no encounters in this area)"), 0, 0)
            return

        for col, text in enumerate(["#", "Digimon", "Reward slot", "Offset"]):
            lbl = QLabel(text)
            f = lbl.font()
            f.setBold(True)
            lbl.setFont(f)
            self._enc_layout.addWidget(lbl, 0, col)

        for row_ix, enc in enumerate(area.encounters):
            row = _EncounterRow(enc, self._undo_stack)
            self._encounter_rows.append(row)
            self._enc_layout.addWidget(QLabel(f"{row_ix + 1:02d}"), row_ix + 1, 0)
            self._enc_layout.addWidget(row.id_combo, row_ix + 1, 1)
            self._enc_layout.addWidget(row.reward_spin, row_ix + 1, 2)
            self._enc_layout.addWidget(QLabel(f"0x{enc.offset:08x}"), row_ix + 1, 3)
        self._enc_layout.setColumnStretch(1, 1)

    def _on_undo_redo(self, _index: int) -> None:
        if not (0 <= self._current_ix < len(self._areas)):
            return
        if self._num_enc_spin is not None:
            self._num_enc_spin.refresh()
            self._rate_lo_spin.refresh()
            self._rate_hi_spin.refresh()
        for row in self._encounter_rows:
            row.id_combo.refresh()
            row.reward_spin.refresh()
