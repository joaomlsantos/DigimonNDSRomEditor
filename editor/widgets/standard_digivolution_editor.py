"""StandardDigivolution editor — list of digimon, right panel shows their
degeneration target and three evolution targets, each with up to three
conditions (id + value).

Empty evolution targets are stored as 0xFFFFFFFF in the ROM; the
`BoundOptionalDigimonId` widget in form_helpers handles the sentinel.

Condition ids come from `constants.DIGIVOLUTION_CONDITIONS` (NONE, LEVEL,
DRAGON EXP, …); see research_docs/digivolution_conditions_parsed.txt.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QUndoStack
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from digimon_core import model

from .digimon_list_panel import DigimonListPanel
from .form_helpers import (
    BoldGroupBox as QGroupBox,
    make_form,
    DIGIVOLUTION_CONDITION_CHOICES,
    NO_EVO_SENTINEL,
    BoundIdComboRow,
    BoundIntChoiceCombo,
    BoundSpinBox,
    digimon_choices,
    digimon_name,
    digimon_stage_rank,
    get_digimon_portrait_icon,
    wrap_in_scroll,
)


# Condition codes whose `value` field semantically refers to a specific digimon
# id (rather than a level / stat / exp amount). The editor swaps the value
# input to a digimon picker when one of these is selected.
DIGIMON_VALUED_CONDITIONS = {0x15, 0x16}


class _CondValueField(QWidget):
    """Value input that swaps between spinbox and digimon-combo by cond id.

    Both child widgets bind to the same `value_attr` on the model — only one is
    visible at a time, and `_update_visible()` flips between them based on the
    current cond id. Edits made on either child push regular SetAttrCommands.
    """

    def __init__(self, target, id_attr: str, value_attr: str, undo_stack):
        super().__init__()
        self._target = target
        self._id_attr = id_attr
        self._value_attr = value_attr

        self._spin = BoundSpinBox(target, value_attr, 4, undo_stack)
        self._digi_combo = BoundIdComboRow(
            target, value_attr, digimon_choices(), undo_stack,
            nav_kind="standard_digivolution",
            shared_kind="digimon",
            show_portrait_icons=False,
        )

        self._stack = QStackedWidget()
        self._stack.addWidget(self._spin)        # index 0
        self._stack.addWidget(self._digi_combo)  # index 1

        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(self._stack)
        self._update_visible()

    def update_for_cond(self) -> None:
        self._update_visible()

    def _update_visible(self) -> None:
        cond_id = getattr(self._target, self._id_attr)
        self._stack.setCurrentIndex(1 if cond_id in DIGIMON_VALUED_CONDITIONS else 0)

    def rebind(self, new_target) -> None:
        self._target = new_target
        self._spin.rebind(new_target)
        self._digi_combo.rebind(new_target)
        self._update_visible()

    def refresh(self) -> None:
        self._spin.refresh()
        self._digi_combo.refresh()
        self._update_visible()


class _EvoTargetGroup(QGroupBox):
    """One groupbox: target id + (cond_id, cond_value) x3."""

    def __init__(
        self,
        title: str,
        target: model.StandardDigivolution,
        id_attr: str,
        cond_attr_pairs: List[Tuple[str, str]],
        undo_stack: QUndoStack,
    ):
        super().__init__(title)
        self._undo_stack = undo_stack
        self._id_attr = id_attr

        self._id_field = BoundIdComboRow(
            target, id_attr, digimon_choices(), undo_stack,
            none_value=NO_EVO_SENTINEL,
            nav_kind="standard_digivolution",
            shared_kind="digimon_evo",
        )

        self._cond_id_combos: List[BoundIntChoiceCombo] = []
        self._cond_value_fields: List[_CondValueField] = []

        form = make_form(self)
        form.addRow("Target", self._id_field)
        for ix, (cid_attr, cval_attr) in enumerate(cond_attr_pairs, start=1):
            id_combo = BoundIntChoiceCombo(target, cid_attr, DIGIVOLUTION_CONDITION_CHOICES, undo_stack)
            val_field = _CondValueField(target, cid_attr, cval_attr, undo_stack)
            id_combo.currentIndexChanged.connect(lambda _i, f=val_field: f.update_for_cond())
            self._cond_id_combos.append(id_combo)
            self._cond_value_fields.append(val_field)
            row = QWidget()
            h = QHBoxLayout(row)
            h.setContentsMargins(0, 0, 0, 0)
            h.addWidget(id_combo, 2)
            h.addWidget(val_field, 1)
            form.addRow(f"Condition {ix}", row)

    def rebind(self, new_target) -> None:
        self._id_field.rebind(new_target)
        for c in self._cond_id_combos:
            c.rebind(new_target)
        for f in self._cond_value_fields:
            f.rebind(new_target)

    def refresh(self) -> None:
        self._id_field.refresh()
        for c in self._cond_id_combos:
            c.refresh()
        for f in self._cond_value_fields:
            f.refresh()

    def set_warn_state(self, triggered: bool, message: str = "") -> None:
        self._id_field.set_warn_state(triggered, message)


# (group title, id attr, [(cond_id_attr, cond_value_attr), ...])
_EVO_GROUPS: List[Tuple[str, str, List[Tuple[str, str]]]] = [
    (
        "Degeneration",
        "degen_evo_id",
        [
            ("degen_condition_id_1", "degen_condition_value_1"),
            ("degen_condition_id_2", "degen_condition_value_2"),
            ("degen_condition_id_3", "degen_condition_value_3"),
        ],
    ),
    (
        "Evolution 1",
        "evolution_1_id",
        [
            ("evo_1_condition_id_1", "evo_1_condition_value_1"),
            ("evo_1_condition_id_2", "evo_1_condition_value_2"),
            ("evo_1_condition_id_3", "evo_1_condition_value_3"),
        ],
    ),
    (
        "Evolution 2",
        "evolution_2_id",
        [
            ("evo_2_condition_id_1", "evo_2_condition_value_1"),
            ("evo_2_condition_id_2", "evo_2_condition_value_2"),
            ("evo_2_condition_id_3", "evo_2_condition_value_3"),
        ],
    ),
    (
        "Evolution 3",
        "evolution_3_id",
        [
            ("evo_3_condition_id_1", "evo_3_condition_value_1"),
            ("evo_3_condition_id_2", "evo_3_condition_value_2"),
            ("evo_3_condition_id_3", "evo_3_condition_value_3"),
        ],
    ),
]


class StandardDigivolutionEditor(QWidget):
    # Emitted when the user clicks "Show in evolution tree" — main_window
    # routes this to the dedicated Evolution Trees tab with the current
    # digimon selected and highlighted in its tree.
    treeRequested = Signal(int)

    _CURSOR_KEY = "standard_digivolutions"

    def __init__(
        self,
        entries: Dict[int, model.StandardDigivolution],
        undo_stack: QUndoStack,
        session,
        parent=None,
    ):
        super().__init__(parent)
        self._entries = entries
        self._undo_stack = undo_stack
        self._session = session
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

        undo_stack.indexChanged.connect(self._refresh_form)
        undo_stack.indexChanged.connect(self._list_panel.refresh_dirty_state)
        # Re-validate the currently-shown record any time an evo / degen target
        # combo changes — invariants are cross-slot so a single edit can both
        # clear and trigger warnings on sibling rows.
        for group in self._groups:
            group._id_field.combo.currentIndexChanged.connect(lambda *_a: self._validate_record())
        remembered = self._session.recall_selection(self._CURSOR_KEY)
        if remembered is None or not self._list_panel.select_by_id(int(remembered)):
            self._list_panel.select_first()

    def _build_detail_container(self) -> QWidget:
        first = next(iter(self._entries.values()))

        self._title = QLabel("—")
        font = self._title.font()
        font.setPointSize(font.pointSize() + 3)
        font.setBold(True)
        self._title.setFont(font)

        self._portrait_label = QLabel()
        self._portrait_label.setFixedSize(40, 40)
        self._portrait_label.setAlignment(Qt.AlignCenter)

        self._tree_button = QPushButton("Show in evolution tree")
        self._tree_button.clicked.connect(self._emit_tree_request)

        title_row = QWidget()
        title_layout = QHBoxLayout(title_row)
        title_layout.setContentsMargins(0, 0, 0, 0)
        title_layout.addWidget(self._portrait_label)
        title_layout.addWidget(self._title)
        title_layout.addStretch(1)
        title_layout.addWidget(self._tree_button)

        self._groups: List[_EvoTargetGroup] = []
        for title, id_attr, cond_pairs in _EVO_GROUPS:
            group = _EvoTargetGroup(title, first, id_attr, cond_pairs, self._undo_stack)
            self._groups.append(group)

        content = QWidget()
        content_layout = QVBoxLayout(content)

        content_layout.setContentsMargins(6, 6, 6, 6)

        content_layout.setSpacing(4)
        content_layout.addWidget(title_row)
        for group in self._groups:
            content_layout.addWidget(group)
        content_layout.addStretch(1)

        return wrap_in_scroll(content)

    def _on_selection(self, digimon_id: int) -> None:
        target = self._entries.get(digimon_id)
        if target is None:
            return
        self._current_id = digimon_id
        self._session.remember_selection(self._CURSOR_KEY, digimon_id)
        self._title.setText(self._title_for(target))
        self._refresh_portrait()
        for group in self._groups:
            group.rebind(target)
        self._validate_record()

    def _refresh_form(self, _index: int) -> None:
        target = self._entries.get(self._current_id)
        if target is None:
            return
        self._title.setText(self._title_for(target))
        self._refresh_portrait()
        for group in self._groups:
            group.refresh()
        self._validate_record()

    def _refresh_portrait(self) -> None:
        icon = get_digimon_portrait_icon(self._current_id)
        if icon is None:
            self._portrait_label.clear()
            return
        self._portrait_label.setPixmap(icon.pixmap(QSize(40, 40)))

    # ---- validation -----------------------------------------------------

    def _validate_record(self) -> None:
        """Apply warn states across the four evo/degen groups for the current
        record. Three invariants:
          1. Every evo target E must list this digimon as its `degen_evo_id`.
          2. No other record may list E as one of its evo targets (one parent).
          3. `degen_evo_id = P` must appear among P's evolution slots.
        """
        record = self._entries.get(self._current_id)
        if record is None:
            return
        issues = self._compute_issues(record)
        for group in self._groups:
            triggered, message = issues.get(group._id_attr, (False, ""))
            group.set_warn_state(triggered, message)

    _EVO_ATTRS = ("evolution_1_id", "evolution_2_id", "evolution_3_id")

    def _compute_issues(self, record) -> Dict[str, Tuple[bool, str]]:
        issues: Dict[str, Tuple[bool, str]] = {attr: (False, "") for _, attr, _ in _EVO_GROUPS}
        me = record.digimon_id

        for evo_attr in self._EVO_ATTRS:
            target = getattr(record, evo_attr)
            if target == NO_EVO_SENTINEL:
                continue
            target_rec = self._entries.get(target)
            if target_rec is None:
                issues[evo_attr] = (
                    True,
                    f"Evolution target 0x{target:x} has no StandardDigivolution record.",
                )
                continue
            if target_rec.degen_evo_id != me:
                if target_rec.degen_evo_id == NO_EVO_SENTINEL:
                    listed = "(none)"
                else:
                    listed = digimon_name(target_rec.degen_evo_id)
                issues[evo_attr] = (
                    True,
                    f"Reciprocal degeneration mismatch — {digimon_name(target)} "
                    f"lists {listed} as its degeneration, not this digimon.",
                )
                continue
            other_parent = self._other_parent_of(target, exclude=me)
            if other_parent is not None:
                issues[evo_attr] = (
                    True,
                    f"{digimon_name(target)} is already an evolution target of "
                    f"{digimon_name(other_parent)} — one digimon may only have "
                    f"one parent in the standard tree.",
                )

        degen = record.degen_evo_id
        if degen != NO_EVO_SENTINEL:
            parent_rec = self._entries.get(degen)
            if parent_rec is None:
                issues["degen_evo_id"] = (
                    True,
                    f"Degeneration 0x{degen:x} has no StandardDigivolution record.",
                )
            elif not any(getattr(parent_rec, a) == me for a in self._EVO_ATTRS):
                issues["degen_evo_id"] = (
                    True,
                    f"{digimon_name(degen)} does not list this digimon as one "
                    f"of its evolutions.",
                )
        return issues

    def _other_parent_of(self, target_id: int, *, exclude: int) -> Optional[int]:
        """Return the id of a record (other than `exclude`) that lists
        `target_id` in one of its evolution slots, or None.
        """
        for other_id, other_rec in self._entries.items():
            if other_id == exclude:
                continue
            if any(getattr(other_rec, a) == target_id for a in self._EVO_ATTRS):
                return other_id
        return None

    def select_by_id(self, digimon_id: int) -> bool:
        return self._list_panel.select_by_id(digimon_id)

    def _emit_tree_request(self) -> None:
        if self._current_id != -1:
            self.treeRequested.emit(self._current_id)

    @staticmethod
    def _title_for(target: model.StandardDigivolution) -> str:
        name = digimon_name(target.digimon_id)
        return f"0x{target.digimon_id:03x}  —  {name}    (offset 0x{target.offset:08x})"


# ---- aggregated validation collector -------------------------------------
# Reads the same three invariants the on-screen warn states check, but yields
# one issue per offending edge so the popup can list (and link to) every
# inconsistent record. Cheap: ~778 records × 3 evo slots × O(N) parent scan
# is well under a millisecond.
from .validation import ValidationIssue  # noqa: E402 — bottom-of-file utility


_EVO_ATTRS = ("evolution_1_id", "evolution_2_id", "evolution_3_id")

# Condition slot names per evolution path + the degen track. Used by the
# level-range check to walk every (cond_id, cond_value) pair in a record.
_STD_CONDITION_GROUPS: List[Tuple[str, List[Tuple[str, str]]]] = [
    ("evolution 1", [(f"evo_1_condition_id_{i}", f"evo_1_condition_value_{i}") for i in (1, 2, 3)]),
    ("evolution 2", [(f"evo_2_condition_id_{i}", f"evo_2_condition_value_{i}") for i in (1, 2, 3)]),
    ("evolution 3", [(f"evo_3_condition_id_{i}", f"evo_3_condition_value_{i}") for i in (1, 2, 3)]),
    ("degeneration", [(f"degen_condition_id_{i}", f"degen_condition_value_{i}") for i in (1, 2, 3)]),
]

# Condition code whose `value` is a level (1..99). All other codes have their
# own semantics — exp totals, item ids, digimon ids — so this is the only one
# we range-check. Exported so armor/DNA validators can reuse the same
# constants and helper instead of duplicating the loop.
LEVEL_CONDITION_CODE = 0x1
LEVEL_MIN = 1
LEVEL_MAX = 99


def level_range_issues(
    rec_section: str,
    editor_key: str,
    record_id: int,
    record_label: str,
    groups: List[Tuple[str, List[Tuple[str, str]]]],
    rec,
) -> List[ValidationIssue]:
    """Walk `groups` (named (cond_id_attr, cond_value_attr) lists) and flag any
    LEVEL-typed condition whose value lies outside [1, 99]. Shared across the
    three digivolution validators so the message style stays consistent."""
    out: List[ValidationIssue] = []
    for group_name, slots in groups:
        for cid_attr, cval_attr in slots:
            if getattr(rec, cid_attr) != LEVEL_CONDITION_CODE:
                continue
            value = getattr(rec, cval_attr)
            if LEVEL_MIN <= value <= LEVEL_MAX:
                continue
            out.append(ValidationIssue(
                section=rec_section,
                category="Level Out of Range",
                message=(
                    f"{record_label} — {group_name} LEVEL condition is {value} "
                    f"(must be {LEVEL_MIN}–{LEVEL_MAX})."
                ),
                editor_key=editor_key,
                record_id=record_id,
            ))
    return out


def std_digivolution_issues(entries: Dict[int, model.StandardDigivolution]) -> List[ValidationIssue]:
    issues: List[ValidationIssue] = []
    for did, rec in entries.items():
        name = digimon_name(did)

        # Duplicate-slot check: two evolution slots in the same record holding
        # the same id is almost certainly a typo. Compare ids pair-wise (rather
        # than via a set) so we can name the slots in the message.
        for ix_a, attr_a in enumerate(_EVO_ATTRS):
            for attr_b in _EVO_ATTRS[ix_a + 1:]:
                a = getattr(rec, attr_a)
                b = getattr(rec, attr_b)
                if a == NO_EVO_SENTINEL or a != b:
                    continue
                issues.append(ValidationIssue(
                    section="Standard Digivolutions",
                    category="Duplicate Evolution Slot",
                    message=(
                        f"{name} lists {digimon_name(a)} in both "
                        f"{attr_a.replace('_id', '')} and {attr_b.replace('_id', '')}."
                    ),
                    editor_key="standard_digivolutions",
                    record_id=did,
                ))

        parent_rank = digimon_stage_rank(did)
        for evo_attr in _EVO_ATTRS:
            target = getattr(rec, evo_attr)
            if target == NO_EVO_SENTINEL:
                continue
            target_rec = entries.get(target)
            if target_rec is None:
                issues.append(ValidationIssue(
                    section="Standard Digivolutions",
                    category="Missing Record",
                    message=f"{name} → 0x{target:x}: target has no StandardDigivolution record.",
                    editor_key="standard_digivolutions",
                    record_id=did,
                ))
                continue
            # Stage regression: target stage must be strictly later than parent.
            # Same-stage edges are tolerated (some games permit lateral evolutions),
            # but a backward step is almost always a typo and also implies a cycle.
            # Skip entirely if either side has no recognised stage — that's the
            # uncategorised-id case (NPCs, story-only digimon) and would just
            # produce noise.
            target_rank = digimon_stage_rank(target)
            if parent_rank is not None and target_rank is not None and target_rank < parent_rank:
                issues.append(ValidationIssue(
                    section="Standard Digivolutions",
                    category="Stage Regression",
                    message=(
                        f"{name} evolves to {digimon_name(target)} — "
                        f"target stage is earlier than parent's."
                    ),
                    editor_key="standard_digivolutions",
                    record_id=did,
                ))
            if target_rec.degen_evo_id != did:
                if target_rec.degen_evo_id == NO_EVO_SENTINEL:
                    listed = "(none)"
                else:
                    listed = digimon_name(target_rec.degen_evo_id)
                issues.append(ValidationIssue(
                    section="Standard Digivolutions",
                    category="Reciprocal Mismatch",
                    message=(
                        f"{name} evolves to {digimon_name(target)}, "
                        f"but {digimon_name(target)}'s degeneration is {listed}."
                    ),
                    editor_key="standard_digivolutions",
                    record_id=did,
                ))
                continue
            for other_id, other_rec in entries.items():
                if other_id == did:
                    continue
                if any(getattr(other_rec, a) == target for a in _EVO_ATTRS):
                    # Only report each duplicate-parent edge once (from the
                    # numerically-lower parent id) so the popup doesn't show
                    # mirror entries from both sides.
                    if did < other_id:
                        issues.append(ValidationIssue(
                            section="Standard Digivolutions",
                            category="Shared Evolution Target",
                            message=(
                                f"{digimon_name(target)} is an evolution target "
                                f"of both {name} and {digimon_name(other_id)}."
                            ),
                            editor_key="standard_digivolutions",
                            record_id=did,
                        ))
                    break

        degen = rec.degen_evo_id
        if degen != NO_EVO_SENTINEL:
            parent_rec = entries.get(degen)
            if parent_rec is None:
                issues.append(ValidationIssue(
                    section="Standard Digivolutions",
                    category="Missing Record",
                    message=f"{name} ← 0x{degen:x}: degeneration target has no record.",
                    editor_key="standard_digivolutions",
                    record_id=did,
                ))
            elif not any(getattr(parent_rec, a) == did for a in _EVO_ATTRS):
                issues.append(ValidationIssue(
                    section="Standard Digivolutions",
                    category="Orphan Degeneration",
                    message=(
                        f"{name}'s degeneration is {digimon_name(degen)}, but "
                        f"{digimon_name(degen)} does not list {name} as an "
                        f"evolution."
                    ),
                    editor_key="standard_digivolutions",
                    record_id=did,
                ))

        issues.extend(level_range_issues(
            rec_section="Standard Digivolutions",
            editor_key="standard_digivolutions",
            record_id=did,
            record_label=name,
            groups=_STD_CONDITION_GROUPS,
            rec=rec,
        ))
    return issues
