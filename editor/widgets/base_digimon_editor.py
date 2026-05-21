"""BaseDataDigimon editor — left list of digimon, right detail form."""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import Qt
from PySide6.QtGui import QUndoStack
from PySide6.QtWidgets import (
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
    BoldGroupBox as QGroupBox,
    BoundCheckBox,
    BoundEnumCombo,
    BoundIdCombo,
    BoundIdComboRow,
    BoundSpinBox,
    _make_compact_grid,
    add_unknown_form_row,
    make_form,
    move_choices,
    trait_choices,
)


# Game-engine caps enforced by the level-up routine (see research_docs/
# levelup_research.md): HP/MP can't exceed 9999, the rest cap at 999. Set
# higher values here and the engine clamps them on the first level-up. The
# warn_above triggers a red border + tooltip so the user knows.
_HP_MP_CAP = 9999
_PRIMARY_STAT_CAP = 999
_LEVEL_CAP = 99
_CAP_MESSAGE_HP = f"Game engine caps HP/MP at {_HP_MP_CAP}. Higher values get clamped on level-up."
_CAP_MESSAGE_STAT = f"Game engine caps this stat at {_PRIMARY_STAT_CAP}. Higher values get clamped on level-up."
_CAP_MESSAGE_LEVEL = f"Level above {_LEVEL_CAP} is outside the normal in-game range."

# Exp curve is an index into a 3-entry curve table (slow / medium / fast).
# Out-of-range values fall through to the default curve in the engine; the
# warning surfaces this so users editing growth rates know the field can't
# accept arbitrary integers.
_EXP_CURVE_MAX = 2
_CAP_MESSAGE_EXP_CURVE = (
    "Exp curve only accepts 0, 1, or 2 (slow / medium / fast). "
    "Other values fall back to the default curve in the engine."
)

# (attribute, label, byte_width, hex_display, warn_above, warn_message)
_STAT_FIELDS: List[Tuple[str, str, int, bool, int, str]] = [
    ("hp", "HP", 2, False, _HP_MP_CAP, _CAP_MESSAGE_HP),
    ("mp", "MP", 2, False, _HP_MP_CAP, _CAP_MESSAGE_HP),
    ("attack", "Attack", 2, False, _PRIMARY_STAT_CAP, _CAP_MESSAGE_STAT),
    ("defense", "Defense", 2, False, _PRIMARY_STAT_CAP, _CAP_MESSAGE_STAT),
    ("spirit", "Spirit", 2, False, _PRIMARY_STAT_CAP, _CAP_MESSAGE_STAT),
    ("speed", "Speed", 2, False, _PRIMARY_STAT_CAP, _CAP_MESSAGE_STAT),
    ("evasion", "Evasion", 2, False, _PRIMARY_STAT_CAP, _CAP_MESSAGE_STAT),
    ("aptitude", "Aptitude", 2, False, _LEVEL_CAP, _CAP_MESSAGE_LEVEL),
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
# (attr, label, byte_width, hex_display, warn_above, warn_message)
_MISC_FIELDS: List[Tuple[str, str, int, bool, Optional[int], str]] = [
    ("level", "Level", 1, False, _LEVEL_CAP, _CAP_MESSAGE_LEVEL),
    ("dex_habitat", "Dex Habitat", 1, False, None, ""),
    ("exp_curve", "Exp Curve", 4, False, _EXP_CURVE_MAX, _CAP_MESSAGE_EXP_CURVE),
    ("unknown_0x26", "Unknown 0x26", 2, True, None, ""),
    ("unknown_0x38", "Unknown 0x38", 1, True, None, ""),
    ("unknown_0x3A", "Unknown 0x3A", 1, True, None, ""),
]




class BaseDigimonEditor(QWidget):
    def __init__(self, entries: Dict[int, model.BaseDataDigimon], undo_stack: QUndoStack, parent=None):
        super().__init__(parent)
        self._entries = entries
        self._undo_stack = undo_stack
        self._current_id: int = -1

        self._list_panel = DigimonListPanel(entries, dirty_aware=True)
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
        undo_stack.indexChanged.connect(self._list_panel.refresh_dirty_state)

        self._list_panel.select_first()

    def select_by_id(self, digimon_id: int) -> bool:
        return self._list_panel.select_by_id(digimon_id)

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
        for ix, (attr, label, width, hex_disp, warn_cap, warn_msg) in enumerate(_STAT_FIELDS):
            spin = BoundSpinBox(
                first, attr, width, self._undo_stack,
                hex_display=hex_disp, warn_above=warn_cap, warn_message=warn_msg,
            )
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
                none_value=0xFF, none_label="(none)",
            )
            self._trait_rows[attr] = combo
            traits_form.addRow(label, combo)

        self._move_rows: Dict[str, BoundIdComboRow] = {}
        moves_box = QGroupBox("Moves")
        moves_form = make_form(moves_box)
        for attr, label in _MOVE_FIELDS:
            row = BoundIdComboRow(
                first, attr, move_choices(), self._undo_stack,
                details_kind="move",
                none_value=0xFFFF, none_label="(none)",
            )
            self._move_rows[attr] = row
            moves_form.addRow(label, row)

        self._misc_widgets: Dict[str, object] = {}
        misc_box = QGroupBox("Misc")
        misc_form = make_form(misc_box)
        scannable_check = BoundCheckBox(first, "is_scannable", self._undo_stack)
        self._misc_widgets["is_scannable"] = scannable_check
        misc_form.addRow("Scannable", scannable_check)
        for attr, label, width, hex_disp, warn_cap, warn_msg in _MISC_FIELDS:
            spin = BoundSpinBox(
                first, attr, width, self._undo_stack,
                hex_display=hex_disp, warn_above=warn_cap, warn_message=warn_msg,
            )
            self._misc_widgets[attr] = spin
            if attr.startswith("unknown_"):
                add_unknown_form_row(misc_form, label, spin)
            else:
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


# ---- aggregated validation collector -------------------------------------
# Reads from the model (not from the active editor's widgets) so the footer
# registry stays correct whether the BaseDigimonEditor is currently open or
# not. Same invariants as the on-screen warn states; one issue per record per
# rule rather than per offending slot, since the popup row already names the
# record and lists which slots in the message body would just be redundant.
from .validation import ValidationIssue  # noqa: E402 — bottom-of-file utility


def base_digimon_issues(entries: Dict[int, model.BaseDataDigimon]) -> List[ValidationIssue]:
    """Footer-level issues for base digimon.

    Mirrors the per-widget red-border caps so anything highlighted on the
    active record also shows up in the footer list when other editors are
    open. One issue per (record, field) breach.
    """
    issues: List[ValidationIssue] = []
    for did, rec in entries.items():
        name = constants.DIGIMON_ID_TO_STR.get(did, f"0x{did:x}")
        for attr, label, _w, _h, cap, _msg in _STAT_FIELDS:
            value = getattr(rec, attr)
            if cap is not None and value > cap:
                issues.append(ValidationIssue(
                    section="Base Digimon",
                    category=label,
                    message=f"{name} — {label} {value} exceeds the cap of {cap}.",
                    editor_key="base_digimon",
                    record_id=did,
                ))
        for attr, label, _w, _h, cap, _msg in _MISC_FIELDS:
            if cap is None:
                continue
            value = getattr(rec, attr)
            if value > cap:
                issues.append(ValidationIssue(
                    section="Base Digimon",
                    category=label,
                    message=f"{name} — {label} {value} exceeds the cap of {cap}.",
                    editor_key="base_digimon",
                    record_id=did,
                ))
    return issues
