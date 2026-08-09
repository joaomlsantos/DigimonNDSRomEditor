"""Inline single-slot palette color editor.

Sits in a browser's side panel next to a :class:`PaletteGrid`. When the user
selects a swatch, the host calls :meth:`set_slot`; the user then edits the
colour with R/G/B sliders + spin boxes + a hex field, or opens the native
colour dialog with the "Color picker…" button. Emits
``colorEdited(slot, (r, g, b))`` — the host writes it back (quantising to
NDS 5-bit BGR555), exactly like :class:`PaletteGrid`'s popup path did.

Live edits (dragging a slider) update the preview only; the write-back signal
fires on *release* / commit so a drag lands as one undo step, not dozens.
"""
from __future__ import annotations

from typing import Tuple

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QColorDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

Rgb = Tuple[int, int, int]


class PaletteEditor(QWidget):
    colorEdited = Signal(int, tuple)  # (slot, (r, g, b)) — commit (undoable)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._slot = -1
        self._syncing = False  # guard against feedback while mirroring widgets

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)

        self._title = QLabel("No slot selected")
        root.addWidget(self._title)

        self._preview = QLabel()
        self._preview.setFixedHeight(22)
        self._preview.setAutoFillBackground(True)
        root.addWidget(self._preview)

        grid = QGridLayout()
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(3)
        self._sliders = {}
        self._spins = {}
        for row, ch in enumerate("RGB"):
            sld = QSlider(Qt.Horizontal)
            sld.setRange(0, 255)
            spn = QSpinBox()
            spn.setRange(0, 255)
            sld.valueChanged.connect(lambda v, c=ch: self._on_slider(c, v))
            sld.sliderReleased.connect(self._commit)
            spn.valueChanged.connect(lambda v, c=ch: self._on_spin(c, v))
            grid.addWidget(QLabel(ch), row, 0)
            grid.addWidget(sld, row, 1)
            grid.addWidget(spn, row, 2)
            self._sliders[ch] = sld
            self._spins[ch] = spn
        root.addLayout(grid)

        hexrow = QHBoxLayout()
        hexrow.addWidget(QLabel("Hex"))
        self._hex = QLineEdit()
        self._hex.setMaxLength(7)
        self._hex.setPlaceholderText("#RRGGBB")
        self._hex.editingFinished.connect(self._on_hex)
        hexrow.addWidget(self._hex, 1)
        root.addLayout(hexrow)

        self._picker_btn = QPushButton("Color picker…")
        self._picker_btn.clicked.connect(self._on_picker)
        root.addWidget(self._picker_btn)
        root.addStretch(1)

        self._set_enabled(False)

    # ---- public --------------------------------------------------------

    def set_slot(self, slot: int, rgb: Rgb = (0, 0, 0)) -> None:
        """Load ``slot`` for editing (no signal). ``slot < 0`` clears/disables."""
        self._syncing = True
        self._slot = slot
        if slot < 0:
            self._title.setText("No slot selected")
            self._preview.setStyleSheet("")
            self._set_enabled(False)
            self._syncing = False
            return
        r, g, b = (int(rgb[0]), int(rgb[1]), int(rgb[2]))
        self._title.setText(f"Palette slot {slot}"
                            + ("  (transparent)" if slot == 0 else ""))
        for ch, val in zip("RGB", (r, g, b)):
            self._sliders[ch].setValue(val)
            self._spins[ch].setValue(val)
        self._hex.setText(f"#{r:02X}{g:02X}{b:02X}")
        self._paint_preview(r, g, b)
        self._set_enabled(True)
        self._syncing = False

    # ---- internals -----------------------------------------------------

    def _set_enabled(self, on: bool) -> None:
        for ch in "RGB":
            self._sliders[ch].setEnabled(on)
            self._spins[ch].setEnabled(on)
        self._hex.setEnabled(on)
        self._picker_btn.setEnabled(on)

    def _current(self) -> Rgb:
        return (self._spins["R"].value(), self._spins["G"].value(),
                self._spins["B"].value())

    def _paint_preview(self, r: int, g: int, b: int) -> None:
        self._preview.setStyleSheet(
            f"background:#{r:02X}{g:02X}{b:02X}; border:1px solid #808080;"
        )

    def _refresh_readouts(self) -> None:
        r, g, b = self._current()
        self._syncing = True
        self._hex.setText(f"#{r:02X}{g:02X}{b:02X}")
        self._syncing = False
        self._paint_preview(r, g, b)

    def _on_slider(self, ch: str, v: int) -> None:
        if self._syncing:
            return
        self._syncing = True
        self._spins[ch].setValue(v)      # mirror, no commit (wait for release)
        self._syncing = False
        self._refresh_readouts()

    def _on_spin(self, ch: str, v: int) -> None:
        if self._syncing:
            return
        self._syncing = True
        self._sliders[ch].setValue(v)
        self._syncing = False
        self._refresh_readouts()
        self._commit()                   # spin steps are discrete — commit each

    def _on_hex(self) -> None:
        if self._syncing:
            return
        t = self._hex.text().strip().lstrip("#")
        if len(t) != 6:
            return
        try:
            r, g, b = int(t[0:2], 16), int(t[2:4], 16), int(t[4:6], 16)
        except ValueError:
            return
        self._syncing = True
        for ch, val in zip("RGB", (r, g, b)):
            self._sliders[ch].setValue(val)
            self._spins[ch].setValue(val)
        self._syncing = False
        self._paint_preview(r, g, b)
        self._commit()

    def _on_picker(self) -> None:
        if self._slot < 0:
            return
        r, g, b = self._current()
        chosen = QColorDialog.getColor(
            QColor(r, g, b), self, f"Palette slot {self._slot}"
        )
        if not chosen.isValid():
            return
        self._syncing = True
        for ch, val in zip("RGB", (chosen.red(), chosen.green(), chosen.blue())):
            self._sliders[ch].setValue(val)
            self._spins[ch].setValue(val)
        self._syncing = False
        self._refresh_readouts()
        self._commit()

    def _commit(self) -> None:
        if self._syncing or self._slot < 0:
            return
        self.colorEdited.emit(self._slot, self._current())
