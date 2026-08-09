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
    QHBoxLayout,
    QLabel,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from digimon_core import constants, map_labels, model

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


# location_destination_id is a field-map id biased by this base: for 20 of the
# 21 vanilla habitats, ``destination_id - _DEST_MAP_BASE`` resolves to that
# area (most landing exactly on the area's first sub-map, e.g. Magnet Mine
# 347 -> map 111, Coliseum 283 -> map 47 "Entry"). The exact base could be
# off by one in-engine — surfaced as a derived hint, not an editable field.
_DEST_MAP_BASE = 236


class _DestinationIdRow(QWidget):
    """Spinbox for ``location_destination_id`` + the field map it resolves to.

    Mirrors :class:`form_helpers.BoundDigimonIdRow` — the label tracks the
    spinbox live and shows ``area_name(destination_id - _DEST_MAP_BASE)`` so
    a warp id reads as its destination area instead of a bare number.
    """

    def __init__(self, target, undo_stack: QUndoStack):
        super().__init__()
        self._target = target
        self._spin = BoundSpinBox(target, "location_destination_id", 2, undo_stack)
        self._label = QLabel()
        self._label.setStyleSheet("color: #888;")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._spin)
        layout.addWidget(self._label, 1)
        self._refresh_label()
        self._spin.valueChanged.connect(lambda _v: self._refresh_label())

    def rebind(self, new_target) -> None:
        self._target = new_target
        self._spin.rebind(new_target)
        self._refresh_label()

    def refresh(self) -> None:
        self._spin.refresh()
        self._refresh_label()

    def _refresh_label(self) -> None:
        map_id = self._spin.value() - _DEST_MAP_BASE
        if 0 <= map_id < len(map_labels.AREA_NAMES):
            self._label.setText(f"→ field map {map_id}: {map_labels.area_name(map_id)}")
        else:
            self._label.setText(f"→ field map {map_id} (?)")


def _record_columns(ix: int, _rec: model.HabitatWorldmap):
    return (f"{ix:02d}", _location_name(ix))


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


class _MapPreviewRow(QWidget):
    """Spinbox for ``map_preview_id`` + a live thumbnail of the SPR it indexes.

    ``map_preview_id`` is an index into ``SPR_*.PAK`` — the 96×64 worldmap
    location preview image the game shows when a location is highlighted on
    the world map. The image tracks the spinbox so retargeting the id
    previews the new artwork inline.
    """

    def __init__(self, target, session, undo_stack: QUndoStack):
        super().__init__()
        self._target = target
        self._session = session
        self._spin = BoundSpinBox(target, "map_preview_id", 2, undo_stack)
        self._image = QLabel()
        self._image.setAlignment(Qt.AlignCenter)
        self._image.setMinimumSize(96, 64)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(self._spin)
        layout.addWidget(self._image, 0, Qt.AlignLeft)
        self._refresh_image()
        self._spin.valueChanged.connect(lambda _v: self._refresh_image())

    def rebind(self, new_target) -> None:
        self._target = new_target
        self._spin.rebind(new_target)
        self._refresh_image()

    def refresh(self) -> None:
        self._spin.refresh()
        self._refresh_image()

    def _refresh_image(self) -> None:
        # Native preview is 96×64; a generous cap keeps it at native size
        # (spr_sprite_pixmap only scales down) rather than shrinking it.
        pix = self._session.spr_sprite_pixmap(self._spin.value(), max_size=256)
        if pix is None:
            self._image.clear()
            self._image.setText("(no preview)")
            self._image.setStyleSheet("color: #888;")
        else:
            self._image.setStyleSheet("")
            self._image.setPixmap(pix)


class HabitatsWorldmapEditor(QWidget):
    _CURSOR_KEY = "habitats_worldmap"

    def __init__(
        self,
        records: List[model.HabitatWorldmap],
        undo_stack: QUndoStack,
        session,
        parent=None,
    ):
        super().__init__(parent)
        self._records = records
        self._undo_stack = undo_stack
        self._session = session
        self._current_ix: int = -1
        self._all_widgets: List[object] = []

        self._list_panel = RecordListPanel(
            records, dirty_aware=True,
            columns_for=_record_columns, headers=("#", "Location"),
        )
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
        self._add_field(graphics_form, "Map preview id", _MapPreviewRow(first, self._session, self._undo_stack))
        self._add_field(graphics_form, "Location text id", BoundSpinBox(first, "location_text_id", 2, self._undo_stack))

        flags = QGroupBox("Availability Flags")
        flags_form = make_form(flags)
        self._add_field(flags_form, "Available flag", BoundSpinBox(first, "location_available_flag", 2, self._undo_stack))
        self._add_field(flags_form, "Visited flag", BoundSpinBox(first, "location_visited_flag", 2, self._undo_stack))

        warp = QGroupBox("Warp / Spawn")
        warp_form = make_form(warp)
        self._add_field(warp_form, "Destination id", _DestinationIdRow(first, self._undo_stack))
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
        self._session.remember_selection(self._CURSOR_KEY, ix)
        target = self._records[ix]
        self._title.setText(f"{_location_name(ix)}    (offset 0x{target.offset:08x})")
        for w in self._all_widgets:
            w.rebind(target)

    def _refresh_form(self, _index: int) -> None:
        if not (0 <= self._current_ix < len(self._records)):
            return
        for w in self._all_widgets:
            w.refresh()
