"""FarmTerrain editor — 17 entries (default terrain + 16 buyable).

Only the identity (id, name from FARM_TERRAINS_NAMES) and the 8 per-species
exp values are documented; the bulk of the 0x5C-byte record is uncharacterized
and exposed as a generic list of `unknown_0x*` spinboxes (kept editable but
collapsed into their own group so they don't clutter the documented fields).
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
    add_unknown_grid_field,
    make_form,
    register_unknown_container,
    wrap_in_scroll,
)
from .record_list_panel import RecordListPanel


# The farm screen can hold at most 8 digimon — values above that don't unlock
# more pen slots in-game, they just go ignored. Surfaced as both a spinbox
# warn-state and a footer validation issue so the user sees the cap before
# editing breaks expectations.
FARM_DIGIMON_LIMIT_CAP = 8
_FARM_DIGIMON_LIMIT_MESSAGE = (
    f"Farm digimon limit above {FARM_DIGIMON_LIMIT_CAP} has no effect — "
    f"the farm screen tops out at {FARM_DIGIMON_LIMIT_CAP} slots."
)


_SPECIES_EXP_FIELDS = [
    ("Holy",         "holy_exp"),
    ("Dark",         "dark_exp"),
    ("Dragon",       "dragon_exp"),
    ("Beast",        "beast_exp"),
    ("Bird",         "bird_exp"),
    ("Machine",      "machine_exp"),
    ("Aquan",        "aquan_exp"),
    ("Insect/Plant", "insectplant_exp"),
]


def _terrain_name(ix: int) -> str:
    if 0 <= ix < len(constants.FARM_TERRAINS_NAMES):
        return constants.FARM_TERRAINS_NAMES[ix]
    return f"<terrain {ix}>"


def _record_columns(ix: int, rec: model.FarmTerrain):
    return (f"{ix:02d}", _terrain_name(ix), f"0x{rec.id:04x}")


class FarmTerrainsEditor(QWidget):
    _CURSOR_KEY = "farm_terrains"

    def __init__(
        self,
        records: List[model.FarmTerrain],
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
            columns_for=_record_columns, headers=("#", "Terrain", "ID"),
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

        identity = QGroupBox("Identity")
        identity_form = make_form(identity)
        self._add_field(identity_form, "Terrain id", BoundSpinBox(first, "id", 2, self._undo_stack, read_only=True))
        self._add_field(
            identity_form, "Farm digimon limit",
            BoundSpinBox(
                first, "farm_digimon_limit", 2, self._undo_stack,
                warn_above=FARM_DIGIMON_LIMIT_CAP,
                warn_message=_FARM_DIGIMON_LIMIT_MESSAGE,
            ),
        )

        exp_box = QGroupBox("Per-Species Exp")
        exp_grid = _make_compact_grid(exp_box, cols=4)
        for ix, (label, attr) in enumerate(_SPECIES_EXP_FIELDS):
            spin = BoundSpinBox(first, attr, 2, self._undo_stack)
            self._all_widgets.append(spin)
            exp_grid.addWidget(QLabel(label), ix // 4, (ix % 4) * 2)
            exp_grid.addWidget(spin, ix // 4, (ix % 4) * 2 + 1)

        unknowns = QGroupBox("Unknown / Unmapped (raw 2-byte fields)")
        register_unknown_container(unknowns)
        unknowns_grid = _make_compact_grid(unknowns, cols=2)
        for ix, (_offset, attr) in enumerate(model.FarmTerrain._UNKNOWN_FIELDS):
            spin = BoundSpinBox(first, attr, 2, self._undo_stack)
            self._all_widgets.append(spin)
            add_unknown_grid_field(unknowns_grid, ix // 2, ix % 2, attr, spin)

        content = QWidget()
        cl = QVBoxLayout(content)

        cl.setContentsMargins(6, 6, 6, 6)

        cl.setSpacing(4)
        cl.addWidget(self._title)
        cl.addWidget(identity)
        cl.addWidget(exp_box)
        cl.addWidget(unknowns)
        cl.addStretch(1)

        return wrap_in_scroll(content)

    def _on_selection(self, ix: int) -> None:
        if not (0 <= ix < len(self._records)):
            return
        self._current_ix = ix
        self._session.remember_selection(self._CURSOR_KEY, ix)
        target = self._records[ix]
        self._title.setText(
            f"{_terrain_name(ix)}    (offset 0x{target.offset:08x}, id 0x{target.id:04x})"
        )
        for w in self._all_widgets:
            w.rebind(target)

    def _refresh_form(self, _index: int) -> None:
        if not (0 <= self._current_ix < len(self._records)):
            return
        for w in self._all_widgets:
            w.refresh()


from .validation import ValidationIssue  # noqa: E402 — bottom-of-file utility


def farm_terrain_issues(records: List[model.FarmTerrain]) -> List[ValidationIssue]:
    """Footer-level issues for farm terrains.

    Currently only flags `farm_digimon_limit` above the hard 8-slot cap; other
    fields are still uncharacterised so no further invariants are checked.
    """
    issues: List[ValidationIssue] = []
    for ix, rec in enumerate(records):
        if rec.farm_digimon_limit > FARM_DIGIMON_LIMIT_CAP:
            issues.append(ValidationIssue(
                section="Farm Terrains",
                category="Farm Digimon Limit",
                message=(
                    f"{_terrain_name(ix)} — farm digimon limit {rec.farm_digimon_limit} "
                    f"exceeds the {FARM_DIGIMON_LIMIT_CAP}-slot cap."
                ),
                editor_key="farm_terrains",
                record_id=ix,
            ))
    return issues
