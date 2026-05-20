"""BaseDataDigimon editor — left list of digimon, right detail form."""
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
    QVBoxLayout,
    QWidget,
)

from digimon_core import constants, model

from .digimon_list_panel import DigimonListPanel
from .form_helpers import (
    BoundCheckBox,
    BoundEnumCombo,
    BoundIdCombo,
    BoundSpinBox,
    _make_compact_grid,
    make_form,
    move_choices,
    trait_choices,
)


# (attribute, label, byte_width, hex_display)
_STAT_FIELDS: List[Tuple[str, str, int, bool]] = [
    ("hp", "HP", 2, False),
    ("mp", "MP", 2, False),
    ("attack", "Attack", 2, False),
    ("defense", "Defense", 2, False),
    ("spirit", "Spirit", 2, False),
    ("speed", "Speed", 2, False),
    ("evasion", "Evasion", 2, False),
    ("aptitude", "Aptitude", 2, False),
]

_RES_FIELDS: List[Tuple[str, str]] = [
    ("light_res", "Light"),
    ("dark_res", "Dark"),
    ("fire_res", "Fire"),
    ("earth_res", "Earth"),
    ("wind_res", "Wind"),
    ("steel_res", "Steel"),
    ("water_res", "Water"),
    ("thunder_res", "Thunder"),
]

_TRAIT_FIELDS: List[Tuple[str, str]] = [
    ("trait_1", "Trait 1"),
    ("trait_2", "Trait 2"),
    ("trait_3", "Trait 3"),
    ("trait_4", "Trait 4"),
    ("support_trait", "Support"),
]

_MOVE_FIELDS: List[Tuple[str, str]] = [
    ("move_signature", "Signature"),
    ("move_1", "Move 1"),
    ("move_2", "Move 2"),
    ("move_3", "Move 3"),
    ("move_4", "Move 4"),
]

# is_scannable is rendered as a checkbox (see _build_detail_container);
# the rest of the misc block are plain spinboxes.
_MISC_FIELDS: List[Tuple[str, str, int, bool]] = [
    ("level", "Level", 1, False),
    ("dex_habitat", "Dex Habitat", 1, False),
    ("exp_curve", "Exp Curve", 4, False),
    ("unknown_0x26", "Unknown 0x26", 2, True),
    ("unknown_0x38", "Unknown 0x38", 1, True),
    ("unknown_0x3A", "Unknown 0x3A", 1, True),
]




class BaseDigimonEditor(QWidget):
    def __init__(self, entries: Dict[int, model.BaseDataDigimon], undo_stack: QUndoStack, parent=None):
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

        # repopulate after undo/redo so the form reflects the model
        undo_stack.indexChanged.connect(self._refresh_form)

        self._list_panel.select_first()

    # ---- detail form -----------------------------------------------------

    def _build_detail_container(self) -> QWidget:
        first = next(iter(self._entries.values()))

        self._title = QLabel("—")
        font = self._title.font()
        font.setPointSize(font.pointSize() + 3)
        font.setBold(True)
        self._title.setFont(font)

        self._id_spin = BoundSpinBox(first, "id", 2, self._undo_stack, hex_display=True, read_only=True)
        self._species_combo = BoundEnumCombo(first, "species", model.Species, self._undo_stack)
        self._type_combo = BoundEnumCombo(first, "digimon_type", model.DigimonType, self._undo_stack)

        identity_box = QGroupBox("Identity")
        identity_form = make_form(identity_box)
        identity_form.addRow("ID", self._id_spin)
        identity_form.addRow("Species", self._species_combo)
        identity_form.addRow("Type", self._type_combo)

        # Stats and resistances are 8 fields each — laid out as 2 rows × 4
        # label+value pairs so the entire detail form fits on one screen.
        self._stat_widgets: Dict[str, BoundSpinBox] = {}
        stats_box = QGroupBox("Stats")
        stats_grid = _make_compact_grid(stats_box, cols=4)
        for ix, (attr, label, width, hex_disp) in enumerate(_STAT_FIELDS):
            spin = BoundSpinBox(first, attr, width, self._undo_stack, hex_display=hex_disp)
            self._stat_widgets[attr] = spin
            stats_grid.addWidget(QLabel(label), ix // 4, (ix % 4) * 2)
            stats_grid.addWidget(spin, ix // 4, (ix % 4) * 2 + 1)

        self._res_widgets: Dict[str, BoundSpinBox] = {}
        res_box = QGroupBox("Resistances")
        res_grid = _make_compact_grid(res_box, cols=4)
        for ix, (attr, label) in enumerate(_RES_FIELDS):
            spin = BoundSpinBox(first, attr, 2, self._undo_stack)
            self._res_widgets[attr] = spin
            res_grid.addWidget(QLabel(label), ix // 4, (ix % 4) * 2)
            res_grid.addWidget(spin, ix // 4, (ix % 4) * 2 + 1)

        # Sentinel values for "no trait" / "no move" slots — all-bits-set for
        # the field's byte width. Base digimon trait fields are 1 byte → 0xFF;
        # move fields are 2 bytes → 0xFFFF.
        self._trait_rows: Dict[str, BoundIdCombo] = {}
        traits_box = QGroupBox("Traits")
        traits_form = make_form(traits_box)
        for attr, label in _TRAIT_FIELDS:
            combo = BoundIdCombo(
                first, attr, trait_choices(), self._undo_stack,
                none_value=0xFF, none_label="(undefined)",
            )
            self._trait_rows[attr] = combo
            traits_form.addRow(label, combo)

        self._move_rows: Dict[str, BoundIdCombo] = {}
        moves_box = QGroupBox("Moves")
        moves_form = make_form(moves_box)
        for attr, label in _MOVE_FIELDS:
            combo = BoundIdCombo(
                first, attr, move_choices(), self._undo_stack,
                none_value=0xFFFF, none_label="(undefined)",
            )
            self._move_rows[attr] = combo
            moves_form.addRow(label, combo)

        self._misc_widgets: Dict[str, object] = {}
        misc_box = QGroupBox("Misc")
        misc_form = make_form(misc_box)
        scannable_check = BoundCheckBox(first, "is_scannable", self._undo_stack)
        self._misc_widgets["is_scannable"] = scannable_check
        misc_form.addRow("Scannable", scannable_check)
        for attr, label, width, hex_disp in _MISC_FIELDS:
            spin = BoundSpinBox(first, attr, width, self._undo_stack, hex_display=hex_disp)
            self._misc_widgets[attr] = spin
            misc_form.addRow(label, spin)

        # Pair Traits and Moves in a single row to halve the vertical space.
        trait_move_row = QHBoxLayout()
        trait_move_row.setContentsMargins(0, 0, 0, 0)
        trait_move_row.setSpacing(4)
        trait_move_row.addWidget(traits_box, 1)
        trait_move_row.addWidget(moves_box, 1)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(6, 6, 6, 6)
        content_layout.setSpacing(4)
        content_layout.addWidget(self._title)
        content_layout.addWidget(identity_box)
        content_layout.addWidget(stats_box)
        content_layout.addWidget(res_box)
        content_layout.addLayout(trait_move_row)
        content_layout.addWidget(misc_box)
        content_layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)
        return scroll

    # ---- selection / refresh --------------------------------------------

    def _on_selection(self, digimon_id: int) -> None:
        target = self._entries.get(digimon_id)
        if target is None:
            return
        self._current_id = digimon_id
        self._title.setText(self._title_for(target))

        self._id_spin.rebind(target)
        self._species_combo.rebind(target)
        self._type_combo.rebind(target)
        for spin in self._stat_widgets.values():
            spin.rebind(target)
        for spin in self._res_widgets.values():
            spin.rebind(target)
        for row in self._trait_rows.values():
            row.rebind(target)
        for row in self._move_rows.values():
            row.rebind(target)
        for spin in self._misc_widgets.values():
            spin.rebind(target)

    def _refresh_form(self, _index: int) -> None:
        target = self._entries.get(self._current_id)
        if target is None:
            return
        self._title.setText(self._title_for(target))
        self._id_spin.refresh()
        self._species_combo.refresh()
        self._type_combo.refresh()
        for spin in self._stat_widgets.values():
            spin.refresh()
        for spin in self._res_widgets.values():
            spin.refresh()
        for row in self._trait_rows.values():
            row.refresh()
        for row in self._move_rows.values():
            row.refresh()
        for spin in self._misc_widgets.values():
            spin.refresh()

    @staticmethod
    def _title_for(target: model.BaseDataDigimon) -> str:
        name = constants.DIGIMON_ID_TO_STR.get(target.id, "<unknown>")
        return f"0x{target.id:03x}  —  {name}    (offset 0x{target.offset:08x})"
