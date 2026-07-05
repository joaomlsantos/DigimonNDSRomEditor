"""FarmItem editor — 86 fixed-stride 0x30-byte records.

Grouped as Identity / Behaviour (training pen + stat) / Outcome values /
Placement (overworld sprite + x/y). The few still-undecoded 2-byte fields —
notably the per-outcome chances, not yet located in this record — stay under
an "Unknown / Unmapped" group so they round-trip on save while staying out of
the way during typical edits.
"""
from __future__ import annotations

from typing import List, Tuple

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
    BoundIntChoiceCombo,
    BoundSpinBox,
    OddsTotalLabel,
    _make_compact_grid,
    add_unknown_grid_field,
    make_form,
    register_unknown_container,
    wrap_in_scroll,
)
from .record_list_panel import RecordListPanel


# Rank is the single byte at offset 0x03 (ordinal 0=S..4=D). The byte at
# offset 0x02 is an independent data-size value, exposed as its own field.
_RANK_CHOICES: List[Tuple[str, int]] = [
    ("S-Rank", 0),
    ("A-Rank", 1),
    ("B-Rank", 2),
    ("C-Rank", 3),
    ("D-Rank", 4),
]


def _training_pen_choices() -> List[Tuple[str, int]]:
    return [
        (f"{ix:02d}  {name}", ix)
        for ix, name in enumerate(constants.FARM_TRAINING_PEN_NAMES)
    ]


def _item_name(item_id: int) -> str:
    return constants.ITEM_ID_TO_STR.get(item_id, f"<item 0x{item_id:03x}>")


def _record_label(_ix: int, rec: model.FarmItem) -> str:
    return f"0x{rec.id:03x}  {_item_name(rec.id)}"


class FarmItemEditor(QWidget):
    _CURSOR_KEY = "farm_items"

    def __init__(
        self,
        records: List[model.FarmItem],
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

        self._list_panel = RecordListPanel(records, _record_label, dirty_aware=True)
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

        undo_stack.indexChanged.connect(self._refresh_form)
        undo_stack.indexChanged.connect(self._list_panel.refresh_dirty_state)
        remembered = self._session.recall_selection(self._CURSOR_KEY)
        if remembered is None or not self._list_panel.select_index(int(remembered)):
            self._list_panel.select_first()

    def select_by_id(self, item_id: int) -> bool:
        for ix, rec in enumerate(self._records):
            if rec.id == item_id:
                return self._list_panel.select_index(ix)
        return False

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
        self._add_field(identity_form, "Item id",     BoundSpinBox(first, "id", 2, self._undo_stack, hex_display=True, read_only=True))
        self._add_field(identity_form, "Data size",   BoundSpinBox(first, "data_size", 1, self._undo_stack))
        self._add_field(identity_form, "Rank",        BoundIntChoiceCombo(first, "rank", _RANK_CHOICES, self._undo_stack))
        self._add_field(identity_form, "Max points",  BoundSpinBox(first, "max_points", 2, self._undo_stack))
        self._add_field(identity_form, "Bit cost",    BoundSpinBox(first, "bit_cost", 4, self._undo_stack))

        behaviour = QGroupBox("Behaviour")
        behaviour_form = make_form(behaviour)
        self._add_field(behaviour_form, "Training pen", BoundIntChoiceCombo(first, "training_pen_id", _training_pen_choices(), self._undo_stack))
        # 0x06 is the stat the good raises (u8); the neighbouring 0x07 byte is a
        # separate still-unmapped qualifier, kept in the Unknown group below.
        self._add_field(behaviour_form, "Stat id", BoundSpinBox(first, "stat_id", 1, self._undo_stack))

        # Value = signed stat delta; odds = u8 chance. No stored total — the
        # four odds sum to 100 in vanilla (see model docstring).
        outcomes = QGroupBox("Outcome Values (signed stat delta) and Odds")
        outcomes_grid = _make_compact_grid(outcomes, cols=2)
        outcome_rows = [
            ("Great failure", "great_failure_value", "great_failure_chance"),
            ("Failure",       "failure_value",       "failure_chance"),
            ("Success",       "success_value",       "success_chance"),
            ("Great success", "great_success_value", "great_success_chance"),
        ]
        for row, (label, value_attr, chance_attr) in enumerate(outcome_rows):
            outcomes_grid.addWidget(QLabel(f"{label} value"), row, 0)
            v_spin = BoundSpinBox(first, value_attr, 2, self._undo_stack, signed=True)
            outcomes_grid.addWidget(v_spin, row, 1)
            self._all_widgets.append(v_spin)
            outcomes_grid.addWidget(QLabel(f"{label} odds"), row, 2)
            c_spin = BoundSpinBox(first, chance_attr, 1, self._undo_stack)
            outcomes_grid.addWidget(c_spin, row, 3)
            self._all_widgets.append(c_spin)
        odds_total = OddsTotalLabel(
            first,
            [c for _, _, c in outcome_rows],
            100,
        )
        outcomes_grid.addWidget(QLabel("Total odds"), len(outcome_rows), 2)
        outcomes_grid.addWidget(odds_total, len(outcome_rows), 3)
        self._all_widgets.append(odds_total)

        placement = QGroupBox("Placement")
        placement_form = make_form(placement)
        # sprite_id → overworld sprite; the id↔sprite mapping (and the palette
        # it renders with) isn't pinned yet, so this is a raw editable id.
        self._add_field(placement_form, "Overworld sprite id", BoundSpinBox(first, "sprite_id", 2, self._undo_stack, hex_display=True))
        self._add_field(placement_form, "X position",          BoundSpinBox(first, "x_position", 2, self._undo_stack, signed=True))
        self._add_field(placement_form, "Y position",          BoundSpinBox(first, "y_position", 2, self._undo_stack, signed=True))

        unknowns = QGroupBox("Unknown / Unmapped (raw fields)")
        register_unknown_container(unknowns)
        unknowns_grid = _make_compact_grid(unknowns, cols=2)
        # The single 1-byte unknown (0x07) rides alongside the 2-byte ones; it
        # lives outside _UNKNOWN_FIELDS (that list is 2-byte-only) so it's added
        # explicitly here.
        unknown_specs = (
            [(attr, 2) for _offset, attr in model.FarmItem._UNKNOWN_FIELDS]
            + [("unknown_0x07", 1)]
        )
        for ix, (attr, width) in enumerate(unknown_specs):
            spin = BoundSpinBox(first, attr, width, self._undo_stack)
            self._all_widgets.append(spin)
            add_unknown_grid_field(unknowns_grid, ix // 2, ix % 2, attr, spin)

        content = QWidget()
        cl = QVBoxLayout(content)

        cl.setContentsMargins(6, 6, 6, 6)

        cl.setSpacing(4)
        cl.addWidget(self._title)
        cl.addWidget(identity)
        cl.addWidget(behaviour)
        cl.addWidget(outcomes)
        cl.addWidget(placement)
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
            f"{_item_name(target.id)}    (offset 0x{target.offset:08x}, id 0x{target.id:03x})"
        )
        for w in self._all_widgets:
            w.rebind(target)

    def _refresh_form(self, _index: int) -> None:
        if not (0 <= self._current_ix < len(self._records)):
            return
        for w in self._all_widgets:
            w.refresh()


from .validation import ValidationIssue  # noqa: E402 — bottom-of-file utility


def farm_item_issues(records: List[model.FarmItem]) -> List[ValidationIssue]:
    """Footer-level issues for farm items: the four outcome odds must sum to
    100 (there's no stored total to fall back on). Warning-only — nothing
    blocks saving a record with off-100 odds."""
    issues: List[ValidationIssue] = []
    for rec in records:
        total = (
            rec.great_failure_chance + rec.failure_chance
            + rec.success_chance + rec.great_success_chance
        )
        if total != 100:
            issues.append(ValidationIssue(
                section="Farm Items",
                category="Odds Sum",
                message=(
                    f"{_item_name(rec.id)} — outcome odds sum to {total}, not 100."
                ),
                editor_key="farm_items",
                record_id=rec.id,
            ))
    return issues
