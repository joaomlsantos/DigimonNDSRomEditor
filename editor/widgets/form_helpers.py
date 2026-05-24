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
from typing import Callable, Dict, List, Optional, Tuple, Type

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QFontMetrics, QRegularExpressionValidator, QUndoStack
from PySide6.QtCore import QRegularExpression
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QCheckBox,
    QComboBox,
    QCompleter,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QScrollArea,
    QSpinBox,
    QStyle,
    QStyleOptionGroupBox,
    QStylePainter,
    QToolButton,
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


def digimon_stage(digimon_id: int) -> Optional[str]:
    """Stage name for `digimon_id`, or None if the id isn't categorised.

    Backed by the same reverse map used for combo inline summaries. Used by
    validators to flag stage regressions (e.g. a standard digivolution whose
    target is at or below the parent's stage).
    """
    return _stage_by_digimon_id().get(digimon_id)


# Index lookup so callers can compare two stages without string juggling.
# Higher index = later stage. Anything outside this list is "unknown stage".
STAGE_ORDER: List[str] = ["IN-TRAINING", "ROOKIE", "CHAMPION", "ULTIMATE", "MEGA"]


def digimon_stage_rank(digimon_id: int) -> Optional[int]:
    """0..len(STAGE_ORDER)-1 rank of the digimon's stage, or None if the stage
    isn't recognised (uncategorised digimon — typically NPCs / non-playable)."""
    stage = digimon_stage(digimon_id)
    if stage is None:
        return None
    upper = stage.upper()
    return STAGE_ORDER.index(upper) if upper in STAGE_ORDER else None


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


def wrap_in_scroll(content: QWidget, parent: Optional[QWidget] = None) -> QScrollArea:
    """Wrap an editor's content widget in the standard scroll area.

    Mirrors the shape every editor was using inline (`QScrollArea` +
    `setWidgetResizable(True)` + `setWidget`) and adds one extra step: after
    the first layout pass it pins the content's `minimumWidth` to the
    realised `sizeHint().width()`. Without that pin, `setWidgetResizable`
    forces the inner widget to viewport width at low resolutions and the
    rightmost combos / fields clip past their `sizeHint`; with it, the
    scroll area surfaces a horizontal scrollbar instead.

    The sizeHint is deferred via `QTimer.singleShot(0)` because reading it
    synchronously during `_build_form()` returns the pre-layout fallback,
    which is usually wider or narrower than the actual rendered width.
    """
    scroll = QScrollArea(parent) if parent is not None else QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setWidget(content)

    def _pin_min_width() -> None:
        try:
            content.setMinimumWidth(content.sizeHint().width())
        except RuntimeError:
            # Widget already deleted (editor torn down before the tick fired).
            pass

    QTimer.singleShot(0, _pin_min_width)
    return scroll


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
class BoldGroupBox(QGroupBox):
    """`QGroupBox` whose title text renders bold without affecting children.

    Implemented by overriding `paintEvent` and routing through `QStylePainter`
    with a bolded font — the QStyle uses the painter's current font to draw the
    title. Crucially this does NOT call `setFont` on the widget, so the bold
    attribute never cascades down to spinboxes / labels / etc inside the group.

    We use this instead of a global `QApplication.setStyleSheet(...)` because
    even a single QGroupBox rule on the app stylesheet roughly quadruples the
    construction time of complex editors (Qt re-evaluates the stylesheet
    against every widget); per-widget stylesheets and `setFont(..., "QGroupBox")`
    were also tried and either had similar overhead or cascaded bold to
    children. Painting directly is the only path that's both correct and
    perf-neutral.
    """

    def paintEvent(self, _event) -> None:  # noqa: N802 — Qt override name
        opt = QStyleOptionGroupBox()
        self.initStyleOption(opt)
        font = self.font()
        font.setBold(True)
        # QStyle uses opt.fontMetrics to size the title rect. Updating both the
        # option's metrics AND the painter's font keeps the reserved space in
        # sync with what we actually draw — otherwise the bold text is wider
        # than the rect and gets clipped at one edge.
        opt.fontMetrics = QFontMetrics(font)
        painter = QStylePainter(self)
        painter.setFont(font)
        painter.drawComplexControl(QStyle.CC_GroupBox, opt)


class NoWheelSpinBox(QSpinBox):
    def wheelEvent(self, event):  # noqa: N802 — Qt override name
        event.ignore()


class NoWheelComboBox(QComboBox):
    def wheelEvent(self, event):  # noqa: N802 — Qt override name
        event.ignore()


# Pin the foreground colour alongside the pink background — without it, dark
# system themes draw the value in their default light text colour, which
# disappears against the warn-state background.
_WARN_QSS = "QSpinBox { border: 1px solid #c0392b; background: #fdecea; color: #2b1b1b; }"
_COMBO_WARN_QSS = "QComboBox { border: 1px solid #c0392b; background: #fdecea; color: #2b1b1b; }"


class BoundSpinBox(NoWheelSpinBox):
    """Spinbox bound to `target.attr` with a fixed byte-width range.

    `warn_above` (optional) flags out-of-range values visually — a red border
    and a tooltip explaining the cap — without blocking input. The game-engine
    caps (HP/MP=9999, ATK/DEF/SPI/SPD=999, level=99) live above the byte-width
    cap, so users editing for mods can still set higher numbers; the warning
    is informational. Use `set_warn_threshold(value, message)` to update the
    threshold at runtime when it depends on another field (e.g. starter level
    capped by the chosen digimon's aptitude).
    """

    def __init__(
        self,
        target,
        attr: str,
        byte_width: int,
        undo_stack: QUndoStack,
        hex_display: bool = False,
        read_only: bool = False,
        warn_above: Optional[int] = None,
        warn_message: str = "",
        signed: bool = False,
    ):
        super().__init__()
        self._target = target
        self._attr = attr
        self._undo_stack = undo_stack
        self._warn_above = warn_above
        self._warn_message = warn_message
        self._base_tooltip: str = ""
        # `signed=True` treats the underlying raw u8/u16/u32 as two's-complement
        # for *display only* — storage stays raw so byte-perfect serialisation
        # is preserved. Used for fields like quest min-tamer-points where
        # 0xFFFF means -1, mirroring the move-effect signed encoding.
        self._signed = signed
        self._byte_width = byte_width
        if signed:
            half = 1 << (8 * byte_width - 1)
            self.setRange(-half, half - 1)
        else:
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
        # Debounce undo pushes so holding an arrow key (or typing digits)
        # doesn't fire the full validation + form-refresh cascade per event.
        # Visual warn-state still updates immediately in `_on_value_changed`.
        self._pending_commit = False
        self._commit_timer = QTimer(self)
        self._commit_timer.setSingleShot(True)
        self._commit_timer.setInterval(150)
        self._commit_timer.timeout.connect(self._commit_pending)
        with silenced(self):
            self.setValue(self._to_display(getattr(target, attr)))
        self._update_warn_state()
        self.valueChanged.connect(self._on_value_changed)

    def _to_display(self, raw: int) -> int:
        if self._signed:
            half = 1 << (8 * self._byte_width - 1)
            if raw >= half:
                return raw - (1 << (8 * self._byte_width))
        return raw

    def _to_storage(self, display: int) -> int:
        if self._signed:
            return display & ((1 << (8 * self._byte_width)) - 1)
        return display

    def setToolTip(self, tip: str) -> None:  # noqa: N802 — Qt override name
        # Record the externally-supplied tooltip so the warning text can be
        # appended on top of it (and removed cleanly when the value goes back
        # under the cap), instead of clobbering the caller's hint.
        self._base_tooltip = tip
        self._update_warn_state()

    def set_warn_threshold(self, value: Optional[int], message: str = "") -> None:
        """Update the warn threshold at runtime (e.g. when a sibling field
        changes the effective cap). Passing `None` removes the warning."""
        self._warn_above = value
        self._warn_message = message
        self._update_warn_state()

    def _update_warn_state(self) -> None:
        triggered = (
            self._warn_above is not None and self.value() > self._warn_above
        )
        self.setStyleSheet(_WARN_QSS if triggered else "")
        if triggered and self._warn_message:
            tip = f"{self._base_tooltip}\n{self._warn_message}" if self._base_tooltip else self._warn_message
            super().setToolTip(tip)
        else:
            super().setToolTip(self._base_tooltip)

    def rebind(self, new_target) -> None:
        # Flush any in-flight edit against the *old* target before switching;
        # otherwise the debounced push would land on the new target's record.
        self._flush_pending()
        self._target = new_target
        with silenced(self):
            self.setValue(self._to_display(getattr(new_target, self._attr)))
        self._update_warn_state()

    def refresh(self) -> None:
        # External value change (undo/redo, programmatic edit) — discard any
        # pending typed edit so we don't immediately push it back on top.
        self._commit_timer.stop()
        self._pending_commit = False
        with silenced(self):
            self.setValue(self._to_display(getattr(self._target, self._attr)))
        self._update_warn_state()

    def focusOutEvent(self, event):  # noqa: N802 — Qt override name
        self._flush_pending()
        super().focusOutEvent(event)

    def _on_value_changed(self, _new_value: int) -> None:
        # Keep the warn border live so the user sees out-of-range feedback as
        # they scrub; defer the undo push (and the cascade it triggers).
        self._update_warn_state()
        self._pending_commit = True
        self._commit_timer.start()

    def _commit_pending(self) -> None:
        if not self._pending_commit:
            return
        self._pending_commit = False
        storage = self._to_storage(self.value())
        if getattr(self._target, self._attr) == storage:
            return
        self._undo_stack.push(SetAttrCommand(self._target, self._attr, storage))

    def _flush_pending(self) -> None:
        if not self._pending_commit:
            return
        self._commit_timer.stop()
        self._commit_pending()


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

    # Emitted after `rebind()`/`refresh()` complete. `currentIndexChanged` is
    # blocked during those (to avoid pushing undo commands for programmatic
    # value updates), so external listeners that care about the new selection
    # — the inline open-in-editor button enabling itself, side labels — wire
    # to this signal in addition to `currentIndexChanged` so they still
    # refresh after a record swap.
    valueRebound = Signal()

    def __init__(
        self,
        target,
        attr: str,
        choices: List[Tuple[int, str]],
        undo_stack: QUndoStack,
        none_value: Optional[int] = None,
        none_label: str = "(none)",
        max_visible: int = 20,
        details_kind: Optional[str] = None,
        show_item_tooltips: bool = True,
        nav_kind: Optional[str] = None,
    ):
        super().__init__()
        self._target = target
        self._attr = attr
        self._choices = list(choices)
        self._undo_stack = undo_stack
        self._none_value = none_value
        self._none_label = none_label
        # `details_kind` drives tooltip / inline-summary content; `nav_kind`
        # picks which "Open in …" handler the inline button uses. Defaults to
        # `details_kind` so the common case stays implicit — the wild-encounters
        # picker overrides `nav_kind="enemy_digimon"` while keeping the digimon
        # tooltip content.
        self._details_kind = details_kind
        self._nav_kind = nav_kind if nav_kind is not None else details_kind
        self._show_item_tooltips = show_item_tooltips
        # Soft warn state — see `set_warn_state`. Stored separately from the
        # details-kind tooltip so toggling the warning doesn't permanently
        # clobber whatever the picker shows on hover.
        self._warn_active: bool = False
        self._warn_message: str = ""
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
        self._refresh_widget_tooltip()
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
        # Anything beyond this count is an "(undefined 0x...)" fallback row added
        # later by `_ensure_index_for`. We track it so rebind/refresh can drop
        # the prior session's fallback without rebuilding the entire choice list.
        self._base_count = self.count()
        self._apply_item_tooltips()

    def _trim_fallback_rows(self) -> None:
        """Remove any "(undefined …)" rows tacked on by prior selections.

        Keeps the cost of `rebind`/`refresh` roughly O(1) instead of O(choices)
        — important for move/digimon combos with hundreds of items selected
        across many entries.
        """
        while self.count() > self._base_count:
            self.removeItem(self.count() - 1)

    def _ensure_index_for(self, value: int) -> int:
        for i in range(self.count()):
            if self.itemData(i) == value:
                return i
        self.addItem(f"(undefined 0x{value:x})", userData=value)
        # Newly-appended fallback row needs the same tooltip treatment.
        self._set_item_tooltip(self.count() - 1, value)
        return self.count() - 1

    def _apply_item_tooltips(self) -> None:
        if not self._details_kind or not self._show_item_tooltips:
            return
        for i in range(self.count()):
            value = self.itemData(i)
            if not isinstance(value, int):
                continue
            self._set_item_tooltip(i, value)

    def _set_item_tooltip(self, index: int, value: int) -> None:
        if not self._details_kind or not self._show_item_tooltips:
            return
        text = _details_for(self._details_kind, value)
        if text:
            self.setItemData(index, text, Qt.ToolTipRole)

    def _refresh_widget_tooltip(self) -> None:
        # Warn state takes precedence over details — when a record-level
        # invariant (e.g. duplicate moves) is violated we want the user to
        # see the warning, not the move's MP / power summary.
        if self._warn_active and self._warn_message:
            self.setToolTip(self._warn_message)
            return
        if not self._details_kind or not self._show_item_tooltips:
            self.setToolTip("")
            return
        ix = self.currentIndex()
        value = self.itemData(ix) if ix >= 0 else None
        if isinstance(value, int):
            self.setToolTip(_details_for(self._details_kind, value))
        else:
            self.setToolTip("")

    def set_warn_state(self, triggered: bool, message: str = "") -> None:
        """Toggle a red-border warning on the combo with an explanatory tooltip.

        Used by editors that enforce record-level invariants the widget itself
        can't detect locally (e.g. "no two move slots may hold the same move").
        Passing `triggered=False` restores the default styling and tooltip.
        """
        self._warn_active = bool(triggered)
        self._warn_message = message if triggered else ""
        self.setStyleSheet(_COMBO_WARN_QSS if self._warn_active else "")
        self._refresh_widget_tooltip()

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
            self._trim_fallback_rows()
            self.setCurrentIndex(self._ensure_index_for(getattr(new_target, self._attr)))
        self._refresh_widget_tooltip()
        self.valueRebound.emit()

    def refresh(self) -> None:
        with silenced(self):
            self._trim_fallback_rows()
            self.setCurrentIndex(self._ensure_index_for(getattr(self._target, self._attr)))
        self._refresh_widget_tooltip()
        self.valueRebound.emit()

    def _on_index_changed(self, _index: int) -> None:
        new_value = self.currentData(Qt.UserRole)
        if new_value is None:
            return
        self._refresh_widget_tooltip()
        if getattr(self._target, self._attr) == new_value:
            return
        self._undo_stack.push(SetAttrCommand(self._target, self._attr, new_value))


class BoundIdComboRow(QWidget):
    """`BoundIdCombo` + inline details label.

    Used wherever the picker has room to surface live context next to the
    selection — digivolution targets/conditions, starter slots, wild
    encounters. The label re-renders on every selection change (and on
    rebind / refresh) so undo/redo and session swaps stay in sync.

    External wiring that needs the underlying combo's signal (e.g. an
    "id picker drives the side-list label" hookup) should reach in through
    `.combo.currentIndexChanged` — this wrapper deliberately doesn't proxy
    every QComboBox method.
    """

    def __init__(
        self,
        target,
        attr: str,
        choices: List[Tuple[int, str]],
        undo_stack: QUndoStack,
        details_kind: str = "digimon",
        include_level: bool = False,
        none_value: Optional[int] = None,
        none_label: str = "(none)",
        max_visible: int = 20,
        nav_kind: Optional[str] = None,
    ):
        super().__init__()
        # Inline label replaces the hover tooltip in these editors, so the
        # inner combo passes `show_item_tooltips=False` to skip the per-item
        # tooltip wiring (avoids duplicating what the inline label already
        # shows). `nav_kind` defaults to `details_kind`; wild encounters
        # overrides it so the open-in button jumps to the enemy editor.
        self.combo = BoundIdCombo(
            target, attr, choices, undo_stack,
            none_value=none_value,
            none_label=none_label,
            max_visible=max_visible,
            details_kind=details_kind,
            show_item_tooltips=False,
            nav_kind=nav_kind,
        )
        self._details_kind = details_kind
        self._include_level = include_level
        self._label = QLabel()
        self._label.setStyleSheet("color: palette(mid);")
        self._open_btn = _make_combo_open_button(self.combo)

        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(self.combo)
        h.addWidget(self._open_btn)
        h.addWidget(self._label, 1)

        self.combo.currentIndexChanged.connect(lambda _i: self._refresh_label())
        self.combo.valueRebound.connect(self._refresh_label)
        self._refresh_label()

    def rebind(self, new_target) -> None:
        self.combo.rebind(new_target)
        self._refresh_label()

    def refresh(self) -> None:
        self.combo.refresh()
        self._refresh_label()

    def set_warn_state(self, triggered: bool, message: str = "") -> None:
        self.combo.set_warn_state(triggered, message)

    def _refresh_label(self) -> None:
        ix = self.combo.currentIndex()
        value = self.combo.itemData(ix) if ix >= 0 else None
        if not isinstance(value, int) or value == self.combo._none_value:
            self._label.setText("")
            return
        if self._details_kind == "digimon":
            self._label.setText(
                digimon_inline_summary(value, include_level=self._include_level)
            )
        elif self._details_kind == "move":
            self._label.setText(move_inline_summary(value))
        else:
            self._label.setText("")


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


# ---- live session data + details helpers --------------------------------
# Combos with `details_kind="move"` (etc.) render extra context — element/
# MP/power for moves, stage/species for digimon, category for items —
# either as a hover tooltip (`BoundIdCombo`) or inline next to the picker
# (`BoundIdComboRow`). Both read from the same module-level session-data
# registry so they always see fresh edits and the session doesn't need to be
# threaded through every combo: `main_window` calls `set_details_providers`
# on ROM load to populate it.

_SESSION_DATA: Dict[str, object] = {}


def set_details_providers(session) -> None:
    """Install session-backed lookups for combo tooltips / inline details.

    Call this after `RomSession.from_file(...)` succeeds, before any editor
    referencing the new session is constructed. Replaces all prior data so
    the previous session's records can't leak into a re-open.
    """
    _SESSION_DATA.clear()
    _SESSION_DATA["base_digimon"] = session.base_digimon
    _SESSION_DATA["enemy_digimon"] = session.enemy_digimon
    _SESSION_DATA["moves"] = list(session.moves)
    _SESSION_DATA["encounter_rewards"] = session.encounter_rewards
    _SESSION_DATA["original_rom_data"] = session.original_rom_data


def clear_details_providers() -> None:
    _SESSION_DATA.clear()


def get_base_digimon(digimon_id: int):
    """Return the `BaseDataDigimon` record for `digimon_id`, or None if no
    session is loaded / the id isn't present. Used by editors that need a
    cross-field constraint (e.g. starter level capped by base aptitude)."""
    base = _SESSION_DATA.get("base_digimon")
    if not base:
        return None
    return base.get(digimon_id)


def get_encounter_rewards_count() -> int:
    """Length of the encounter-rewards table for the current session, or 0
    if no session is loaded. Used to validate reward-slot indices."""
    rewards = _SESSION_DATA.get("encounter_rewards")
    if not rewards:
        return 0
    return len(rewards)


def is_record_dirty(record) -> bool:
    """True if `record` has been edited since the session was loaded.

    Compares `record.getByteArray()` against the matching slice of the
    pristine ROM snapshot stored at load time. Models without an `offset` or
    `getByteArray` (or sessions that haven't been registered yet) report as
    clean — callers should treat the result as "show no dirty marker" rather
    than truth about whether anything was edited.
    """
    rom_data = _SESSION_DATA.get("original_rom_data")
    if rom_data is None or record is None:
        return False
    offset = getattr(record, "offset", None)
    if offset is None:
        return False
    try:
        current = bytes(record.getByteArray())
    except (AttributeError, TypeError):
        return False
    original = bytes(rom_data[offset:offset + len(current)])
    return current != original


# ---- cross-reference navigation ----------------------------------------
# Each picker can sprout an inline "Open in X editor" button (see
# `make_open_in_editor_button`). Editors register a single handler per `kind`
# on session load. Digimon pickers default to the base-digimon editor; the
# wild-encounters editor opts into the enemy-digimon route by passing
# `nav_kind="enemy_digimon"` to its pickers.

_NAV_HANDLERS: Dict[str, Tuple[str, Callable[[int], None]]] = {}


def register_nav_handler(kind: str, label: str, handler: Callable[[int], None]) -> None:
    _NAV_HANDLERS[kind] = (label, handler)


def clear_nav_handlers() -> None:
    _NAV_HANDLERS.clear()


def _nav_handler_for(kind: Optional[str]) -> Optional[Tuple[str, Callable[[int], None]]]:
    if not kind:
        return None
    return _NAV_HANDLERS.get(kind)


# ---- "show unknown fields" toggle --------------------------------------
# Editors register their unknown_0x* rows here so the Edit-menu toggle can
# hide them in bulk. Default is hidden — most users only care about the
# documented fields. Dead widgets (Qt-deleted across editor swaps) are pruned
# on the next toggle pass.

_UNKNOWN_FIELDS: List[Tuple[QWidget, QWidget]] = []
_unknown_visible: bool = False


def register_unknown_field(label_widget: QWidget, field_widget: QWidget) -> None:
    """Tag a (label, field) pair as part of an editor's unknown-fields block.

    Both widgets get hidden/shown together when the user toggles the global
    flag. Newly-registered widgets only get an explicit `setVisible(False)`
    when the toggle is currently off — calling `setVisible(True)` on a widget
    that doesn't have a parent yet (the common case at build time, since
    pickers are registered before their containing layout is added to the
    page) would make it pop up as a top-level window for one frame. Qt
    defaults to "visible when the parent shows," so the True branch is a
    no-op anyway.
    """
    _UNKNOWN_FIELDS.append((label_widget, field_widget))
    if _unknown_visible:
        return
    try:
        label_widget.setVisible(False)
        field_widget.setVisible(False)
    except RuntimeError:
        pass


def set_unknown_fields_visible(visible: bool) -> None:
    """Flip every registered unknown-field row to the given visibility."""
    global _unknown_visible
    _unknown_visible = bool(visible)
    alive: List[Tuple[QWidget, QWidget]] = []
    for label_widget, field_widget in _UNKNOWN_FIELDS:
        try:
            label_widget.setVisible(_unknown_visible)
            field_widget.setVisible(_unknown_visible)
            alive.append((label_widget, field_widget))
        except RuntimeError:
            continue
    _UNKNOWN_FIELDS[:] = alive


def unknown_fields_visible() -> bool:
    return _unknown_visible


def add_unknown_form_row(form: QFormLayout, label_text: str, widget: QWidget) -> None:
    """Add `widget` under `label_text` to `form` and register the row as an
    unknown-field row so the global toggle hides both sides together."""
    form.addRow(label_text, widget)
    label_widget = form.labelForField(widget)
    if label_widget is not None:
        register_unknown_field(label_widget, widget)


def register_unknown_container(widget: QWidget) -> None:
    """Register a container (typically a `QGroupBox` of pure-unknown rows) so
    the global toggle hides the whole container — avoids a titled but empty
    group sitting on the form when its only children are unknown rows."""
    register_unknown_field(widget, widget)


def add_unknown_grid_field(
    grid: QGridLayout,
    row: int,
    col_pair: int,
    label_text: str,
    field: QWidget,
) -> QLabel:
    """Add `[label, field]` into a 2-cells-per-pair compact grid and register
    them as an unknown row. Returns the label widget for callers that need to
    style it further."""
    label_widget = QLabel(label_text)
    grid.addWidget(label_widget, row, col_pair * 2)
    grid.addWidget(field, row, col_pair * 2 + 1)
    register_unknown_field(label_widget, field)
    return label_widget


# ---- "Open in X editor" inline button ----------------------------------

# Width of the inline open-in-editor button. Exposed so widgets that share a
# column with `with_open_button` outputs can reserve the matching footprint.
OPEN_BUTTON_WIDTH = 22


def _with_right_slot(left_widget: QWidget, right_widget: QWidget) -> QWidget:
    """Standard HBox layout shared by `with_open_button` and
    `with_open_button_placeholder`. Centralised so spacing/margins stay
    identical — that's what aligns the input's right edge across pages that
    share a stacked-column. The trailing `addStretch(1)` keeps the input on
    the left when the column is wider than `left+right` (e.g. the encounter
    rewards reward column), instead of letting the input expand to fill.
    """
    container = QWidget()
    h = QHBoxLayout(container)
    h.setContentsMargins(0, 0, 0, 0)
    h.setSpacing(2)
    h.addWidget(left_widget)
    h.addWidget(right_widget)
    h.addStretch(1)
    return container


def with_open_button(combo: "BoundIdCombo") -> QWidget:
    """Wrap a standalone `BoundIdCombo` in an HBox with an inline open-in
    button on the right. Use this for pickers that aren't already inside a
    `BoundIdComboRow`. The returned QWidget is form-layout friendly; the
    original combo reference still works for rebind/refresh/signal access.
    """
    return _with_right_slot(combo, _make_combo_open_button(combo))


def with_open_button_placeholder(widget: QWidget) -> QWidget:
    """Counterpart to `with_open_button` for widgets that don't get an open
    button but share a column with ones that do (e.g. money / empty pages in
    the encounter-rewards stacked reward column).

    Adds an invisible spacer of `OPEN_BUTTON_WIDTH` on the right so the inner
    widget's right edge aligns with the combo's right edge in `with_open_button`
    rows, regardless of how wide the column gets.
    """
    spacer = QWidget()
    spacer.setFixedWidth(OPEN_BUTTON_WIDTH)
    return _with_right_slot(widget, spacer)


def with_open_button_for_spin(spin: "BoundSpinBox", nav_kind: str) -> QWidget:
    """Wrap a `BoundSpinBox` whose value is an id/index into another editor
    (e.g. the wild-encounter `reward_slot` pointing at an entry in the
    encounter-rewards table). The button passes the spinbox's current integer
    value to the `nav_kind`-registered handler when clicked."""
    button = _OpenInEditorButton(
        spin,
        nav_kind=nav_kind,
        value_getter=lambda: spin.value(),
        change_signals=[spin.valueChanged],
    )
    return _with_right_slot(spin, button)


def make_open_in_editor_button(combo: "BoundIdCombo") -> QToolButton:
    """Build a small inline button that opens the picker's current value in
    its target editor. Always returns a button — it disables itself when no
    handler is registered yet (e.g. before a ROM is loaded)."""
    return _make_combo_open_button(combo)


def _combo_value_getter(combo: "BoundIdCombo") -> Callable[[], Optional[int]]:
    """Pull the navigable int id out of a `BoundIdCombo`'s current selection,
    returning None for the `(none)` sentinel or non-int item data."""
    def get() -> Optional[int]:
        ix = combo.currentIndex()
        value = combo.itemData(ix) if ix >= 0 else None
        if not isinstance(value, int):
            return None
        if value == combo._none_value:
            return None
        return value
    return get


def _make_combo_open_button(combo: "BoundIdCombo") -> "_OpenInEditorButton":
    return _OpenInEditorButton(
        combo,
        nav_kind=combo._nav_kind,
        value_getter=_combo_value_getter(combo),
        # `currentIndexChanged` is the user-driven signal; `valueRebound` fires
        # after `BoundIdCombo.rebind/refresh`, which silence `currentIndexChanged`
        # during their programmatic setCurrentIndex to avoid spurious undo
        # pushes. The button needs both so its enabled-state stays in sync
        # across record swaps.
        change_signals=[combo.currentIndexChanged, combo.valueRebound],
    )


class _OpenInEditorButton(QToolButton):
    """`↗` button that fires a registered nav handler with a source value.

    Disabled when no handler is registered (no ROM loaded) or the source has
    no value to navigate to. Generic over the value source: combos pass a
    getter that reads `currentIndex()`/`itemData()`, spinboxes pass `lambda:
    spin.value()`. The button refreshes its enabled/tooltip state whenever
    any of `change_signals` fires.
    """

    def __init__(
        self,
        parent: QWidget,
        nav_kind: Optional[str],
        value_getter: Callable[[], Optional[int]],
        change_signals: List,
    ):
        super().__init__(parent)
        self._nav_kind = nav_kind
        self._value_getter = value_getter
        # `↗` (U+2197) is the conventional "open elsewhere / external link"
        # glyph — same visual idiom as "open in new tab" on the web. It reads
        # less aggressively than the earlier `➜` (U+279C) heavy arrow.
        self.setText("\u2197")
        self.setAutoRaise(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        # Tight square so it doesn't dominate the picker's footprint.
        self.setFixedWidth(OPEN_BUTTON_WIDTH)
        # Lighter visual weight than surrounding form text — the button is
        # an accessory, not the primary control. Disabled state falls back
        # to Qt's default disabled palette so the user can still tell the
        # button isn't clickable.
        self.setStyleSheet("QToolButton:enabled { color: palette(mid); }")
        for sig in change_signals:
            # Slot accepts no args; Qt drops the signal's parameter (int / etc).
            sig.connect(self._refresh_state)
        self.clicked.connect(self._on_clicked)
        self._refresh_state()

    def _resolve(self) -> Optional[Tuple[str, Callable[[int], None]]]:
        return _nav_handler_for(self._nav_kind)

    def _refresh_state(self, *_args) -> None:
        handler = self._resolve()
        value = self._value_getter()
        enabled = handler is not None and value is not None
        self.setEnabled(enabled)
        if handler is not None:
            self.setToolTip(handler[0])
        else:
            self.setToolTip("")

    def _on_clicked(self) -> None:
        handler = self._resolve()
        value = self._value_getter()
        if handler is None or value is None:
            return
        handler[1](value)


_stage_by_digimon_id_cache: Optional[Dict[int, str]] = None


def _stage_by_digimon_id() -> Dict[int, str]:
    """Reverse map digimon-id → stage name from `constants.DIGIMON_IDS`."""
    global _stage_by_digimon_id_cache
    if _stage_by_digimon_id_cache is None:
        out: Dict[int, str] = {}
        for stage, mapping in constants.DIGIMON_IDS.items():
            for _name, did in mapping.items():
                out[did] = stage
        _stage_by_digimon_id_cache = out
    return _stage_by_digimon_id_cache


def _move_tooltip(move_id: int) -> str:
    moves = _SESSION_DATA.get("moves")
    if not moves or not (0 <= move_id < len(moves)):
        return ""
    m = moves[move_id]
    return (
        f"Element: {m.element.name.title()}\n"
        f"Power: {m.primary_value}\n"
        f"MP: {m.mp_cost}\n"
        f"Hits: {m.num_hits}"
    )


def _digimon_tooltip(digi_id: int) -> str:
    stage = _stage_by_digimon_id().get(digi_id)
    base = _SESSION_DATA.get("base_digimon")
    record = base.get(digi_id) if base else None
    parts: List[str] = []
    if stage is not None:
        parts.append(f"Stage: {stage.title()}")
    if record is not None:
        parts.append(f"Species: {record.species.name.title()}")
    return "\n".join(parts)


_ITEM_TYPE_RANGES_CACHE: Optional[List[Tuple[str, Tuple[int, int]]]] = None


def _item_tooltip(item_id: int) -> str:
    global _ITEM_TYPE_RANGES_CACHE
    if _ITEM_TYPE_RANGES_CACHE is None:
        _ITEM_TYPE_RANGES_CACHE = list(constants.ITEM_TYPE_IDS.items())
    for type_name, (lo, hi) in _ITEM_TYPE_RANGES_CACHE:
        if lo <= item_id <= hi:
            return f"Type: {type_name.replace('_', ' ').title()}"
    return ""


_TOOLTIP_PROVIDERS: Dict[str, Callable[[int], str]] = {
    "digimon": _digimon_tooltip,
    "move": _move_tooltip,
    "item": _item_tooltip,
}


def _details_for(kind: Optional[str], value: int) -> str:
    if not kind:
        return ""
    provider = _TOOLTIP_PROVIDERS.get(kind)
    if provider is None:
        return ""
    return provider(value) or ""


def move_inline_summary(move_id: int) -> str:
    """One-line summary used as the inline label beside a move picker.

    Format is `Lv N  ·  Pow P  ·  MP M`. Returns "" if the move can't be
    resolved (no session, undefined id) so the label degrades to empty
    rather than breaking the layout.
    """
    moves = _SESSION_DATA.get("moves")
    if not moves or not (0 <= move_id < len(moves)):
        return ""
    m = moves[move_id]
    return f"Lv {m.level_learned}  ·  Pow {m.primary_value}  ·  MP {m.mp_cost}"


def digimon_inline_summary(digi_id: int, *, include_level: bool = False) -> str:
    """One-line summary used as the inline label beside a digimon picker.

    Format is `Stage  ·  Species[  ·  Lv N]`. Pieces missing from the
    session (e.g. an undefined id with no matching record) are simply omitted
    so the label degrades gracefully.

    `include_level=True` pulls species AND level from `enemy_digimon` —
    that's the right table for wild-encounter pickers, since the encounter's
    level lives on the enemy record (base records store the starting level,
    which is always 1 for vanilla rookies and would be misleading here).
    """
    stage = _stage_by_digimon_id().get(digi_id)
    source_key = "enemy_digimon" if include_level else "base_digimon"
    source = _SESSION_DATA.get(source_key)
    record = source.get(digi_id) if source else None
    parts: List[str] = []
    if stage is not None:
        parts.append(stage.title())
    if record is not None:
        parts.append(record.species.name.title())
        if include_level:
            parts.append(f"Lv {record.level}")
    return "  ·  ".join(parts)
