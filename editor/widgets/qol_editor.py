"""QoL toggles editor — surfaces the DWDDRandomizer-derived byte-patches.

Each row is a checkbox bound to a bool field on `RomSession.qol`; toggles
that have a numeric parameter also expose a spinbox whose enabled-state
mirrors the checkbox. Both kinds of widget push `SetAttrCommand`s onto the
session's undo stack so QoL flips are dirty-tracked like any other edit.

The patches themselves live in `digimon_core/qol.py`; this widget only
wires user intent into the `QolSettings` dataclass. Application happens at
save time via `RomSession.serialize_all_with_qol()`.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QUndoStack
from PySide6.QtWidgets import (
    QCheckBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from digimon_core import qol

from ..commands import SetAttrCommand
from .form_helpers import BoldGroupBox as QGroupBox, silenced


class _BoundBoolCheck(QCheckBox):
    """Checkbox bound to a real `bool` attribute on `target`.

    Differs from form_helpers.BoundCheckBox which writes 0/1 — QolSettings
    fields are typed bool, and JSON serialization (project file) treats
    True/False distinctly from 0/1, so we preserve the type here.
    """

    def __init__(self, target, attr: str, undo_stack: QUndoStack, label: str = ""):
        super().__init__(label)
        self._target = target
        self._attr = attr
        self._undo_stack = undo_stack
        with silenced(self):
            self.setChecked(bool(getattr(target, attr)))
        self.toggled.connect(self._on_toggled)

    def refresh(self) -> None:
        with silenced(self):
            self.setChecked(bool(getattr(self._target, self._attr)))

    def _on_toggled(self, checked: bool) -> None:
        new_value = bool(checked)
        if getattr(self._target, self._attr) == new_value:
            return
        self._undo_stack.push(SetAttrCommand(self._target, self._attr, new_value))


class _BoundFloat(QDoubleSpinBox):
    """Double-spin bound to a `float` attribute on `target`."""

    def __init__(
        self,
        target,
        attr: str,
        undo_stack: QUndoStack,
        minimum: float,
        maximum: float,
        step: float,
        decimals: int = 2,
    ):
        super().__init__()
        self._target = target
        self._attr = attr
        self._undo_stack = undo_stack
        self.setRange(minimum, maximum)
        self.setSingleStep(step)
        self.setDecimals(decimals)
        with silenced(self):
            self.setValue(float(getattr(target, attr)))
        self.valueChanged.connect(self._on_changed)

    def refresh(self) -> None:
        with silenced(self):
            self.setValue(float(getattr(self._target, self._attr)))

    def _on_changed(self, value: float) -> None:
        if abs(getattr(self._target, self._attr) - value) < 1e-9:
            return
        self._undo_stack.push(SetAttrCommand(self._target, self._attr, float(value)))


class _BoundInt(QSpinBox):
    """Int spinbox bound to an `int` attribute on `target`."""

    def __init__(
        self,
        target,
        attr: str,
        undo_stack: QUndoStack,
        minimum: int,
        maximum: int,
    ):
        super().__init__()
        self._target = target
        self._attr = attr
        self._undo_stack = undo_stack
        self.setRange(minimum, maximum)
        with silenced(self):
            self.setValue(int(getattr(target, attr)))
        self.valueChanged.connect(self._on_changed)

    def refresh(self) -> None:
        with silenced(self):
            self.setValue(int(getattr(self._target, self._attr)))

    def _on_changed(self, value: int) -> None:
        if getattr(self._target, self._attr) == value:
            return
        self._undo_stack.push(SetAttrCommand(self._target, self._attr, int(value)))


class QolEditor(QWidget):
    """Form of toggles + linked parameters for `RomSession.qol`."""

    def __init__(self, settings: qol.QolSettings, undo_stack: QUndoStack, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._settings = settings
        self._undo_stack = undo_stack
        self._rows: list[tuple[_BoundBoolCheck, Optional[QWidget]]] = []

        body = QVBoxLayout()
        body.setContentsMargins(12, 12, 12, 12)
        body.setSpacing(6)

        intro = QLabel(
            "Quality-of-life byte-patches applied at save time, after every "
            "data edit. Toggles are off by default; flipping them is undoable "
            "like any other edit. The movement-speed multiplier scales the "
            "current byte value, so saving a QoL-patched ROM and re-opening "
            "it will compound the multiplier on the next save — use a project "
            "file (.romproj) for round-trip-safe persistence of these settings."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet("color: palette(mid); padding-bottom: 8px;")
        body.addWidget(intro)

        # ---- groupings keep semantically related patches together ----------
        body.addWidget(self._build_section(
            "Pacing",
            [
                ("fast_text", "Increase text speed (1.5×)", None),
                ("fast_movement", "Increase movement speed",
                 ("movement_speed_multiplier", 1.0, 5.0, 0.25, 2, "×")),
                ("improve_battle_performance", "Improve battle performance", None),
            ],
        ))
        body.addWidget(self._build_section(
            "Scanning",
            [
                ("fast_scan", "Increase base scan rate",
                 ("scan_rate", 1, 100, "")),
            ],
        ))
        body.addWidget(self._build_section(
            "World",
            [
                ("unlock_exclusive_areas", "Unlock version-exclusive areas", None),
                ("expand_player_name", "Expand player name length (max 7)", None),
            ],
        ))
        body.addStretch(1)

        container = QWidget()
        container.setLayout(body)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setWidget(container)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)

        undo_stack.indexChanged.connect(self._on_undo_redo)

    def _build_section(self, title: str, rows: list) -> QGroupBox:
        group = QGroupBox(title)
        layout = QVBoxLayout(group)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(4)
        for row in rows:
            attr, label, param_spec = row
            row_widget = self._build_row(attr, label, param_spec)
            layout.addWidget(row_widget)
        return group

    def _build_row(self, attr: str, label: str, param_spec) -> QWidget:
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)

        check = _BoundBoolCheck(self._settings, attr, self._undo_stack, label)
        h.addWidget(check)

        param_widget: Optional[QWidget] = None
        if param_spec is not None:
            param_widget = self._build_param(param_spec)
            # Parameter spinbox is informationally meaningless when the toggle
            # is off; grey it out so the only knob the user has when a feature
            # is disabled is the checkbox itself.
            param_widget.setEnabled(check.isChecked())
            check.toggled.connect(param_widget.setEnabled)
            h.addStretch(1)
            h.addWidget(param_widget)
        else:
            h.addStretch(1)

        self._rows.append((check, param_widget))
        return row

    def _build_param(self, spec) -> QWidget:
        # spec for float param: (attr, min, max, step, decimals, suffix)
        # spec for int   param: (attr, min, max, suffix)
        if len(spec) == 6:
            attr, lo, hi, step, decimals, suffix = spec
            w = _BoundFloat(self._settings, attr, self._undo_stack, lo, hi, step, decimals)
        else:
            attr, lo, hi, suffix = spec
            w = _BoundInt(self._settings, attr, self._undo_stack, lo, hi)
        if suffix:
            w.setSuffix(f" {suffix}")
        w.setAlignment(Qt.AlignRight)
        w.setMinimumWidth(90)
        return w

    def _on_undo_redo(self, _index: int) -> None:
        # Undo/redo bypasses our toggled handlers — pull the current settings
        # state back into every widget so the form mirrors the dataclass.
        for check, param in self._rows:
            check.refresh()
            if param is not None:
                param.refresh()
                param.setEnabled(check.isChecked())
