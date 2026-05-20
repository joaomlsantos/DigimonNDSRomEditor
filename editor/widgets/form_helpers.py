"""Reusable form widgets that bind to a model attribute and push undo commands.

The pattern: each helper returns a Qt widget already wired to mutate
`target.attr` through a `SetAttrCommand` on the supplied `QUndoStack`. The
binding survives across selections — when the form switches to a different
target, call `rebind(widget, new_target)` to retarget without rebuilding the
widget.

Programmatic updates (loading values into a freshly-bound widget, undo/redo
refreshes) must be wrapped in `silenced(widget)` so they don't fire spurious
commands back onto the stack.
"""
from __future__ import annotations

from contextlib import contextmanager
from enum import Enum
from typing import List, Optional, Tuple, Type

from PySide6.QtCore import Qt
from PySide6.QtGui import QRegularExpressionValidator, QUndoStack
from PySide6.QtCore import QRegularExpression
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QCheckBox,
    QComboBox,
    QCompleter,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QSpinBox,
    QWidget,
)

from digimon_core import constants

from ..commands import SetAttrCommand


# Shared by every digivolution editor (standard, armor, DNA). The condition
# table is the same across all three structures.
DIGIVOLUTION_CONDITION_CHOICES: List[Tuple[str, int]] = sorted(
    [(name, code) for code, name in constants.DIGIVOLUTION_CONDITIONS.items()],
    key=lambda x: x[1],
)


def digimon_name(digimon_id: int) -> str:
    return constants.DIGIMON_ID_TO_STR.get(digimon_id, "<unknown>")


# Qt's QSpinBox is backed by a signed 32-bit int, so 4-byte unsigned fields
# can't be represented in their full 0..0xFFFFFFFF range. We clamp to INT32_MAX;
# fields that legitimately exceed 2**31 - 1 will need a 64-bit-capable input
# widget (e.g. a hex-validated QLineEdit) — none of the digimon stat fields hit
# that ceiling in vanilla, so this is fine for the current editors.
_QSPINBOX_MAX = (1 << 31) - 1


def _max_for_bytes(byte_width: int) -> int:
    return min((1 << (8 * byte_width)) - 1, _QSPINBOX_MAX)


def _make_two_column_grid(parent: QWidget) -> QGridLayout:
    """Two-column (label, value) grid — kept for compatibility; new code should
    use `_make_compact_grid(parent, cols)` for configurable columns."""
    return _make_compact_grid(parent, cols=2)


def _make_compact_grid(parent: QWidget, cols: int = 2) -> QGridLayout:
    """N (label, value)-pair grid with tight spacing.

    Labels and values cluster on the left; a stretch on the rightmost column
    eats leftover horizontal space so values stay compact instead of expanding
    to fill the row. `cols` is the number of label+value pairs per row.
    """
    grid = QGridLayout(parent)
    grid.setContentsMargins(8, 8, 8, 8)
    grid.setHorizontalSpacing(12)
    grid.setVerticalSpacing(3)
    grid.setColumnStretch(cols * 2, 1)
    return grid


def _tighten_form(form) -> None:
    """Apply the standard tight spacing/margins to a QFormLayout."""
    from PySide6.QtWidgets import QFormLayout as _QFL
    form.setContentsMargins(8, 8, 8, 8)
    form.setHorizontalSpacing(12)
    form.setVerticalSpacing(3)
    # Without this, QFormLayout stretches its field column to fill all available
    # width — and capping individual widget widths leaves awkward gaps. With
    # `FieldsStayAtSizeHint`, widgets render at their sizeHint and labels sit
    # tight to the left, matching the compact grids visually.
    form.setFieldGrowthPolicy(_QFL.FieldsStayAtSizeHint)


def make_form(parent: QWidget):
    """Tightened QFormLayout factory — drop-in replacement for `QFormLayout(box)`.

    Equivalent to `QFormLayout(parent)` followed by `_tighten_form(...)`. Use
    this in editors so spacing stays consistent across the app.
    """
    from PySide6.QtWidgets import QFormLayout as _QFL
    form = _QFL(parent)
    _tighten_form(form)
    return form


@contextmanager
def silenced(widget):
    """Suppress signals from `widget` for the duration of the block."""
    widget.blockSignals(True)
    try:
        yield
    finally:
        widget.blockSignals(False)


# Mouse-wheel edits on numeric/choice inputs cause accidental mutations as
# the user scrolls past them in a long form. Forward the wheel event to the
# parent (typically a QScrollArea) so it scrolls instead of mutating the
# control. To change a value, the user must type or click the arrows.
class NoWheelSpinBox(QSpinBox):
    def wheelEvent(self, event):  # noqa: N802 — Qt override name
        event.ignore()


class NoWheelComboBox(QComboBox):
    def wheelEvent(self, event):  # noqa: N802 — Qt override name
        event.ignore()


class BoundSpinBox(NoWheelSpinBox):
    """Spinbox bound to `target.attr` with a fixed byte-width range."""

    def __init__(
        self,
        target,
        attr: str,
        byte_width: int,
        undo_stack: QUndoStack,
        hex_display: bool = False,
        read_only: bool = False,
    ):
        super().__init__()
        self._target = target
        self._attr = attr
        self._undo_stack = undo_stack
        self.setRange(0, _max_for_bytes(byte_width))
        # Cap so the spinbox doesn't bloat to fill its grid cell, and floor so
        # the field always shows at least "0xFFFF" (6 chars) plus arrows even
        # when packed into a narrow grid cell.
        self.setMinimumWidth(90)
        self.setMaximumWidth(120 if byte_width >= 4 else 100)
        if hex_display:
            self.setDisplayIntegerBase(16)
            self.setPrefix("0x")
        if read_only:
            # setEnabled(False) greys the whole widget (value + arrows) using
            # the platform's standard disabled-palette colors, so the user can
            # see at a glance that the field can't be touched.
            self.setEnabled(False)
            self.setButtonSymbols(QAbstractSpinBox.NoButtons)
        with silenced(self):
            self.setValue(getattr(target, attr))
        self.valueChanged.connect(self._on_value_changed)

    def rebind(self, new_target) -> None:
        self._target = new_target
        with silenced(self):
            self.setValue(getattr(new_target, self._attr))

    def refresh(self) -> None:
        with silenced(self):
            self.setValue(getattr(self._target, self._attr))

    def _on_value_changed(self, new_value: int) -> None:
        if getattr(self._target, self._attr) == new_value:
            return
        self._undo_stack.push(SetAttrCommand(self._target, self._attr, new_value))


class BoundHexLineEdit(QLineEdit):
    """Hex-text line edit for full-range unsigned-int fields.

    Use this for 4-byte fields that can legitimately exceed 0x7FFFFFFF (raw
    pointers, packed-flag words, undocumented blobs) where `BoundSpinBox` would
    clamp at INT32_MAX. The widget edits as "0x…" hex; the commit happens on
    `editingFinished` so partial typing doesn't push commands.
    """

    def __init__(self, target, attr: str, byte_width: int, undo_stack: QUndoStack):
        super().__init__()
        self._target = target
        self._attr = attr
        self._byte_width = byte_width
        self._max_value = (1 << (8 * byte_width)) - 1
        self._undo_stack = undo_stack
        hex_digits = byte_width * 2
        self.setMaxLength(2 + hex_digits)  # "0x" + digits
        # 4-byte hex ("0xFFFFFFFF") needs ~120px including cursor; 2-byte
        # variants ("0xFFFF") need ~70. Floor at 80 so a 2-byte field never
        # shrinks below 6 characters of room in a tight grid cell.
        self.setMinimumWidth(80)
        self.setMaximumWidth(140 if byte_width >= 4 else 90)
        self.setValidator(
            QRegularExpressionValidator(
                QRegularExpression(rf"0[xX][0-9a-fA-F]{{1,{hex_digits}}}"),
                self,
            )
        )
        self._set_text_from(getattr(target, attr))
        self.editingFinished.connect(self._on_editing_finished)

    def _set_text_from(self, value: int) -> None:
        with silenced(self):
            self.setText(f"0x{value:0{self._byte_width * 2}x}")

    def rebind(self, new_target) -> None:
        self._target = new_target
        self._set_text_from(getattr(new_target, self._attr))

    def refresh(self) -> None:
        self._set_text_from(getattr(self._target, self._attr))

    def _on_editing_finished(self) -> None:
        text = self.text().strip()
        try:
            new_value = int(text, 16)
        except ValueError:
            self._set_text_from(getattr(self._target, self._attr))
            return
        if new_value < 0 or new_value > self._max_value:
            self._set_text_from(getattr(self._target, self._attr))
            return
        cur = getattr(self._target, self._attr)
        if cur == new_value:
            # snap text to canonical 0x… form even when value didn't change
            self._set_text_from(cur)
            return
        self._undo_stack.push(SetAttrCommand(self._target, self._attr, new_value))


class BoundEnumCombo(NoWheelComboBox):
    """Combo box bound to an Enum-valued attribute (`target.attr` is an Enum)."""

    def __init__(self, target, attr: str, enum_cls: Type[Enum], undo_stack: QUndoStack):
        super().__init__()
        self._target = target
        self._attr = attr
        self._enum_cls = enum_cls
        self._undo_stack = undo_stack
        for member in enum_cls:
            self.addItem(member.name, userData=member)
        self.setMaximumWidth(180)
        with silenced(self):
            self.setCurrentIndex(self._index_for(getattr(target, attr)))
        self.currentIndexChanged.connect(self._on_index_changed)

    def _index_for(self, member: Enum) -> int:
        for i in range(self.count()):
            if self.itemData(i) == member:
                return i
        return 0

    def rebind(self, new_target) -> None:
        self._target = new_target
        with silenced(self):
            self.setCurrentIndex(self._index_for(getattr(new_target, self._attr)))

    def refresh(self) -> None:
        with silenced(self):
            self.setCurrentIndex(self._index_for(getattr(self._target, self._attr)))

    def _on_index_changed(self, _index: int) -> None:
        new_value = self.currentData(Qt.UserRole)
        if getattr(self._target, self._attr) == new_value:
            return
        self._undo_stack.push(SetAttrCommand(self._target, self._attr, new_value))


class BoundDigimonIdRow(QWidget):
    """Spinbox + name-label for a digimon-id attribute (no None semantics).

    Use this when a digimon id field is always present (every armor / DNA
    digivolution record fills it). For optional ids that may be 0xFFFFFFFF,
    see `BoundOptionalDigimonId`.
    """

    def __init__(self, target, attr: str, undo_stack: QUndoStack, byte_width: int = 4):
        super().__init__()
        self._target = target
        self._attr = attr
        self._spin = BoundSpinBox(target, attr, byte_width, undo_stack)
        self._label = QLabel()
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
        self._label.setText(digimon_name(self._spin.value()))


# 0xFFFFFFFF marks an empty evolution slot in StandardDigivolution records.
NO_EVO_SENTINEL = 0xFFFFFFFF
_DIGIMON_ID_MAX = 0xFFFF  # vanilla ids fit in 2 bytes; keeps spinbox in INT32 range


class BoundOptionalDigimonId(QWidget):
    """4-byte digimon-id field where 0xFFFFFFFF means "no value".

    Layout: [none] checkbox + spinbox + resolved-name label.
    Toggling "none" rewrites the model attribute to the sentinel; the previous
    real id is remembered so unchecking restores it.
    """

    def __init__(self, target, attr: str, undo_stack: QUndoStack):
        super().__init__()
        self._target = target
        self._attr = attr
        self._undo_stack = undo_stack

        self._none_check = QCheckBox("none")
        self._spin = NoWheelSpinBox()
        self._spin.setRange(0, _DIGIMON_ID_MAX)
        self._name_label = QLabel()

        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(self._none_check)
        h.addWidget(self._spin)
        h.addWidget(self._name_label, 1)

        cur = getattr(target, attr)
        self._last_real_id = 0 if cur == NO_EVO_SENTINEL else cur
        self._apply_state(cur)

        self._none_check.toggled.connect(self._on_none_toggled)
        self._spin.valueChanged.connect(self._on_spin_changed)

    def rebind(self, new_target) -> None:
        self._target = new_target
        cur = getattr(new_target, self._attr)
        self._last_real_id = 0 if cur == NO_EVO_SENTINEL else cur
        self._apply_state(cur)

    def refresh(self) -> None:
        cur = getattr(self._target, self._attr)
        if cur != NO_EVO_SENTINEL:
            self._last_real_id = cur
        self._apply_state(cur)

    def _apply_state(self, value: int) -> None:
        is_none = value == NO_EVO_SENTINEL
        with silenced(self._none_check):
            self._none_check.setChecked(is_none)
        with silenced(self._spin):
            self._spin.setValue(self._last_real_id if is_none else value)
        self._spin.setEnabled(not is_none)
        self._name_label.setText("(none)" if is_none else digimon_name(value))

    def _on_none_toggled(self, checked: bool) -> None:
        cur = getattr(self._target, self._attr)
        if checked and cur != NO_EVO_SENTINEL:
            self._last_real_id = cur
            self._undo_stack.push(SetAttrCommand(self._target, self._attr, NO_EVO_SENTINEL))
        elif not checked and cur == NO_EVO_SENTINEL:
            self._undo_stack.push(SetAttrCommand(self._target, self._attr, self._last_real_id))

    def _on_spin_changed(self, new_value: int) -> None:
        cur = getattr(self._target, self._attr)
        if cur == new_value:
            return
        self._undo_stack.push(SetAttrCommand(self._target, self._attr, new_value))


class BoundIntChoiceCombo(NoWheelComboBox):
    """Combo bound to an int-valued attribute with a fixed (label, value) list.

    If the model's current value isn't in `choices`, an extra "unknown (0xNN)"
    row is added on the fly so we don't accidentally clobber unrecognized data
    when the user changes selections elsewhere on the form.
    """

    def __init__(
        self,
        target,
        attr: str,
        choices: List[Tuple[str, int]],
        undo_stack: QUndoStack,
    ):
        super().__init__()
        self._target = target
        self._attr = attr
        self._choices = list(choices)
        self._undo_stack = undo_stack
        self.setMaximumWidth(280)
        self._populate()
        with silenced(self):
            self.setCurrentIndex(self._ensure_index_for(getattr(target, attr)))
        self.currentIndexChanged.connect(self._on_index_changed)

    def _populate(self) -> None:
        self.clear()
        for label, value in self._choices:
            self.addItem(label, userData=value)

    def _ensure_index_for(self, value: int) -> int:
        for i in range(self.count()):
            if self.itemData(i) == value:
                return i
        # add a fallback row to preserve the unknown value
        self.addItem(f"unknown (0x{value:x})", userData=value)
        return self.count() - 1

    def rebind(self, new_target) -> None:
        self._target = new_target
        with silenced(self):
            self._populate()
            self.setCurrentIndex(self._ensure_index_for(getattr(new_target, self._attr)))

    def refresh(self) -> None:
        with silenced(self):
            self._populate()
            self.setCurrentIndex(self._ensure_index_for(getattr(self._target, self._attr)))

    def _on_index_changed(self, _index: int) -> None:
        new_value = self.currentData(Qt.UserRole)
        if getattr(self._target, self._attr) == new_value:
            return
        self._undo_stack.push(SetAttrCommand(self._target, self._attr, new_value))


class BoundIdCombo(NoWheelComboBox):
    """Combo bound to an int-id attribute backed by a fixed (id, name) lookup.

    Used for large lookup tables (digimon ~400, items ~400, moves ~500,
    traits ~175) where a spinbox+label was uncomfortable. Supports an optional
    sentinel value (e.g. 0xFFFFFFFF for empty digivolution slots) rendered as a
    "(none)" row prepended to the list. Unknown values get an
    "(undefined 0xNN)" fallback row so we never clobber data the user didn't
    touch.

    The combo is editable with a substring-matching completer — the user can
    type any part of the visible label (id prefix or name fragment) to filter
    the dropdown. Free-text typing never overwrites the model: on focus-out
    the combo snaps back to whichever item is currently selected.
    """

    def __init__(
        self,
        target,
        attr: str,
        choices: List[Tuple[int, str]],
        undo_stack: QUndoStack,
        none_value: Optional[int] = None,
        none_label: str = "(none)",
        max_visible: int = 20,
    ):
        super().__init__()
        self._target = target
        self._attr = attr
        self._choices = list(choices)
        self._undo_stack = undo_stack
        self._none_value = none_value
        self._none_label = none_label
        self.setMaxVisibleItems(max_visible)
        # Wide enough for "0x1ff  Some Long Move Name" without expanding to
        # fill the entire form column.
        self.setMaximumWidth(280)

        # Editable + NoInsert lets the user type to filter without ever adding
        # new rows to the model. The completer does substring matching so the
        # user can search by name fragment regardless of the leading id prefix.
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.NoInsert)
        completer = QCompleter(self)
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        completer.setFilterMode(Qt.MatchContains)
        completer.setCompletionMode(QCompleter.PopupCompletion)
        self.setCompleter(completer)
        self._completer = completer

        self._populate()
        with silenced(self):
            self.setCurrentIndex(self._ensure_index_for(getattr(target, attr)))
        self.currentIndexChanged.connect(self._on_index_changed)
        # Free-typed text that doesn't match an item: snap back on focus-out.
        line_edit = self.lineEdit()
        if line_edit is not None:
            line_edit.editingFinished.connect(self._reset_text_to_selection)

    def _populate(self) -> None:
        self.clear()
        if self._none_value is not None:
            self.addItem(self._none_label, userData=self._none_value)
        for cid, name in self._choices:
            self.addItem(f"0x{cid:03x}  {name}", userData=cid)
        # Re-point completer at the freshly populated model.
        if self._completer is not None:
            self._completer.setModel(self.model())

    def _ensure_index_for(self, value: int) -> int:
        for i in range(self.count()):
            if self.itemData(i) == value:
                return i
        self.addItem(f"(undefined 0x{value:x})", userData=value)
        return self.count() - 1

    def _reset_text_to_selection(self) -> None:
        """Resolve free-typed text on focus-out / Enter.

        Three cases, in order:
          1) Text already matches the current item → leave it alone.
          2) Text exactly matches another item label (case-insensitive) or is a
             unique substring of exactly one item → commit that item.
          3) Otherwise → snap back to the current selection.
        """
        line_edit = self.lineEdit()
        if line_edit is None:
            return
        ix = self.currentIndex()
        text = line_edit.text()
        if ix >= 0 and text == self.itemText(ix):
            return
        match_ix = self._find_match_index(text)
        if match_ix is not None and match_ix != ix:
            self.setCurrentIndex(match_ix)
            return
        if ix >= 0:
            expected = self.itemText(ix)
            if text != expected:
                with silenced(line_edit):
                    line_edit.setText(expected)

    def _find_match_index(self, text: str) -> Optional[int]:
        text = text.strip()
        if not text:
            return None
        lowered = text.casefold()
        exact_ix: Optional[int] = None
        substring_matches: List[int] = []
        for i in range(self.count()):
            item_text = self.itemText(i)
            if item_text.casefold() == lowered:
                exact_ix = i
                break
            if lowered in item_text.casefold():
                substring_matches.append(i)
        if exact_ix is not None:
            return exact_ix
        if len(substring_matches) == 1:
            return substring_matches[0]
        return None

    def rebind(self, new_target) -> None:
        self._target = new_target
        with silenced(self):
            self._populate()
            self.setCurrentIndex(self._ensure_index_for(getattr(new_target, self._attr)))

    def refresh(self) -> None:
        with silenced(self):
            self._populate()
            self.setCurrentIndex(self._ensure_index_for(getattr(self._target, self._attr)))

    def _on_index_changed(self, _index: int) -> None:
        new_value = self.currentData(Qt.UserRole)
        if new_value is None:
            return
        if getattr(self._target, self._attr) == new_value:
            return
        self._undo_stack.push(SetAttrCommand(self._target, self._attr, new_value))


class BoundCheckBox(QCheckBox):
    """Checkbox bound to an int attribute that is logically 0 / non-zero."""

    def __init__(self, target, attr: str, undo_stack: QUndoStack, label: str = ""):
        super().__init__(label)
        self._target = target
        self._attr = attr
        self._undo_stack = undo_stack
        with silenced(self):
            self.setChecked(bool(getattr(target, attr)))
        self.toggled.connect(self._on_toggled)

    def rebind(self, new_target) -> None:
        self._target = new_target
        with silenced(self):
            self.setChecked(bool(getattr(new_target, self._attr)))

    def refresh(self) -> None:
        with silenced(self):
            self.setChecked(bool(getattr(self._target, self._attr)))

    def _on_toggled(self, checked: bool) -> None:
        new_value = 1 if checked else 0
        if getattr(self._target, self._attr) == new_value:
            return
        self._undo_stack.push(SetAttrCommand(self._target, self._attr, new_value))


# ---- cached choice-list builders ----------------------------------------

_digimon_choices_cache: Optional[List[Tuple[int, str]]] = None
_move_choices_cache: Optional[List[Tuple[int, str]]] = None
_trait_choices_cache: Optional[List[Tuple[int, str]]] = None
_item_choices_cache: Optional[List[Tuple[int, str]]] = None


def digimon_choices() -> List[Tuple[int, str]]:
    global _digimon_choices_cache
    if _digimon_choices_cache is None:
        _digimon_choices_cache = sorted(
            constants.DIGIMON_ID_TO_STR.items(), key=lambda kv: kv[0]
        )
    return _digimon_choices_cache


def move_choices() -> List[Tuple[int, str]]:
    global _move_choices_cache
    if _move_choices_cache is None:
        _move_choices_cache = [(i, name) for i, name in enumerate(constants.MOVE_ARRAY_STR)]
    return _move_choices_cache


def trait_choices() -> List[Tuple[int, str]]:
    global _trait_choices_cache
    if _trait_choices_cache is None:
        _trait_choices_cache = [(i, name) for i, name in enumerate(constants.TRAIT_ARRAY_STR)]
    return _trait_choices_cache


def item_choices() -> List[Tuple[int, str]]:
    global _item_choices_cache
    if _item_choices_cache is None:
        _item_choices_cache = sorted(
            constants.ITEM_ID_TO_STR.items(), key=lambda kv: kv[0]
        )
    return _item_choices_cache
