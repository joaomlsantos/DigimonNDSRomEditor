"""HabitatWorldmap editor — 21 fixed worldmap locations.

Each record is 0x18 bytes. The interesting field is `species_living` — an
8-bit mask where bit i corresponds to species i in the order HOLY, DARK,
DRAGON, BEAST, BIRD, MACHINE, AQUAN, INSECTPLANT (the bits are read LSB-first
into that species order; see research_docs/worldmap_habitats_etc.txt). We
present it as a row of 8 checkboxes for direct manipulation.

unknown_0x0e/0x10/0x12 are kept as plain spinboxes — their semantics aren't
yet documented in research_docs.
"""
from __future__ import annotations

from typing import List

from PySide6.QtCore import Qt
from PySide6.QtGui import QUndoStack
from PySide6.QtWidgets import (
    QCheckBox,
    QGridLayout,
    QLabel,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from digimon_core import constants, model

from ..commands import SetAttrCommand
from .form_helpers import (
    BoldGroupBox as QGroupBox,
    BoundSpinBox,
    add_unknown_form_row,
    silenced,
    make_form,
    register_unknown_container,
    wrap_in_scroll,
)
from .record_list_panel import RecordListPanel


_SPECIES_BITS = [
    "Holy", "Dark", "Dragon", "Beast",
    "Bird", "Machine", "Aquan", "Insect/Plant",
]


def _location_name(ix: int) -> str:
    if 0 <= ix < len(constants.LOCATION_LIST):
        return constants.LOCATION_LIST[ix]
    return f"<area {ix}>"


def _record_label(ix: int, _rec: model.HabitatWorldmap) -> str:
    return f"{ix:02d}  {_location_name(ix)}"


class _SpeciesFlagsRow(QWidget):
    """4×2 grid of checkboxes bound to a single bitfield int attribute.

    Laid out as a grid (not a single row) because 8 side-by-side checkboxes
    overflow the editor pane on common window widths.
    """

    def __init__(self, target, attr: str, undo_stack: QUndoStack):
        super().__init__()
        self._target = target
        self._attr = attr
        self._undo_stack = undo_stack

        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(4)

        self._checks: List[QCheckBox] = []
        cols = 4
        for bit, name in enumerate(_SPECIES_BITS):
            cb = QCheckBox(name)
            cb.toggled.connect(lambda checked, b=bit: self._on_bit_toggled(b, checked))
            grid.addWidget(cb, bit // cols, bit % cols)
            self._checks.append(cb)

        self._apply_from_target()

    def rebind(self, new_target) -> None:
        self._target = new_target
        self._apply_from_target()

    def refresh(self) -> None:
        self._apply_from_target()

    def _apply_from_target(self) -> None:
        value = getattr(self._target, self._attr)
        for bit, cb in enumerate(self._checks):
            with silenced(cb):
                cb.setChecked(bool(value & (1 << bit)))

    def _on_bit_toggled(self, bit: int, checked: bool) -> None:
        old = getattr(self._target, self._attr)
        if checked:
            new = old | (1 << bit)
        else:
            new = old & ~(1 << bit)
        if new == old:
            return
        self._undo_stack.push(SetAttrCommand(self._target, self._attr, new))


class HabitatsWorldmapEditor(QWidget):
    def __init__(
        self,
        records: List[model.HabitatWorldmap],
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
        first = self._records[0]

        self._title = QLabel("—")
        font = self._title.font()
        font.setPointSize(font.pointSize() + 3)
        font.setBold(True)
        self._title.setFont(font)

        coords = QGroupBox("World Map Position")
        coords_form = make_form(coords)
        self._add_field(coords_form, "X coordinate", BoundSpinBox(first, "x_coordinate", 2, self._undo_stack))
        self._add_field(coords_form, "Y coordinate", BoundSpinBox(first, "y_coordinate", 2, self._undo_stack))

        species_box = QGroupBox("Species Shown In Preview")
        species_form = make_form(species_box)
        self._add_field(species_form, "Species mask", _SpeciesFlagsRow(first, "species_living", self._undo_stack))

        graphics = QGroupBox("Graphics & Text IDs")
        graphics_form = make_form(graphics)
        self._add_field(graphics_form, "Map preview id", BoundSpinBox(first, "map_preview_id", 2, self._undo_stack))
        self._add_field(graphics_form, "Location text id", BoundSpinBox(first, "location_text_id", 2, self._undo_stack))

        flags = QGroupBox("Availability Flags")
        flags_form = make_form(flags)
        self._add_field(flags_form, "Available flag", BoundSpinBox(first, "location_available_flag", 2, self._undo_stack))
        self._add_field(flags_form, "Visited flag", BoundSpinBox(first, "location_visited_flag", 2, self._undo_stack))

        warp = QGroupBox("Warp / Spawn")
        warp_form = make_form(warp)
        self._add_field(warp_form, "Destination id", BoundSpinBox(first, "location_destination_id", 2, self._undo_stack))
        self._add_field(warp_form, "Spawn position flag", BoundSpinBox(first, "spawn_position_flag", 2, self._undo_stack))

        unknowns = QGroupBox("Unknown / Unmapped")
        register_unknown_container(unknowns)
        unknowns_form = make_form(unknowns)
        for attr in ("unknown_0x0e", "unknown_0x10", "unknown_0x12"):
            spin = BoundSpinBox(first, attr, 2, self._undo_stack)
            self._all_widgets.append(spin)
            add_unknown_form_row(unknowns_form, attr, spin)

        content = QWidget()
        cl = QVBoxLayout(content)

        cl.setContentsMargins(6, 6, 6, 6)

        cl.setSpacing(4)
        cl.addWidget(self._title)
        for group in (coords, species_box, graphics, flags, warp, unknowns):
            cl.addWidget(group)
        cl.addStretch(1)

        return wrap_in_scroll(content)

    def _on_selection(self, ix: int) -> None:
        if not (0 <= ix < len(self._records)):
            return
        self._current_ix = ix
        target = self._records[ix]
        self._title.setText(f"{_location_name(ix)}    (offset 0x{target.offset:08x})")
        for w in self._all_widgets:
            w.rebind(target)

    def _refresh_form(self, _index: int) -> None:
        if not (0 <= self._current_ix < len(self._records)):
            return
        for w in self._all_widgets:
            w.refresh()
