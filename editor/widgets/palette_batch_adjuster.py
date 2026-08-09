"""Batch HSL adjuster for a palette selection — the recolour primitive.

Sits beside a :class:`PaletteGrid` in multi-select mode. The user selects a
run of swatches (a shading ramp / a material), then dials Hue / Saturation /
Lightness *deltas* that apply to every selected colour by the same amount —
so the ramp's light-to-dark relationships survive and a blue body becomes a
red one with its shading intact.

Explore-then-apply: dragging a slider live-previews via ``previewChanged`` (the
host tints the grid swatches only); **Apply** fires ``committed`` once for the
combined H/S/L transform (one undo step), and **Reset** clears the pending
delta. Deltas are computed from a *snapshot* taken in :meth:`set_selection`
(8-bit RGB), so dragging back and forth never accumulates BGR555 rounding — the
host quantises only when it writes the committed colours back.
"""
from __future__ import annotations

import colorsys
from typing import Dict, List, Tuple

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

Rgb = Tuple[int, int, int]


def _shift(rgb: Rgb, dh: float, ds: float, dl: float) -> Rgb:
    """Apply a hue-rotation (deg) + additive sat/lightness delta to one colour.

    Near-grey colours (s≈0) keep their neutrality under a hue rotation — hue is
    ill-defined there but saturation stays 0, so outlines/whites don't tint."""
    r, g, b = (rgb[0] / 255.0, rgb[1] / 255.0, rgb[2] / 255.0)
    h, light, s = colorsys.rgb_to_hls(r, g, b)
    h = (h + dh / 360.0) % 1.0
    s = min(1.0, max(0.0, s + ds / 100.0))
    light = min(1.0, max(0.0, light + dl / 100.0))
    r, g, b = colorsys.hls_to_rgb(h, light, s)
    return (round(r * 255), round(g * 255), round(b * 255))


class PaletteBatchAdjuster(QWidget):
    previewChanged = Signal(object)  # {slot: (r, g, b)} — display-only preview
    committed = Signal(object)       # {slot: (r, g, b)} — apply (one undo step)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._base: Dict[int, Rgb] = {}   # snapshot: slot -> original rgb
        self._syncing = False

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)

        self._title = QLabel("Adjust colours")
        self._title.setStyleSheet("font-weight: bold;")
        root.addWidget(self._title)

        self._hint = QLabel(
            "Select swatches (shift = range, drag = box), then shift them "
            "together."
        )
        self._hint.setWordWrap(True)
        self._hint.setStyleSheet("color: #888; font-size: 10px;")
        root.addWidget(self._hint)

        grid = QGridLayout()
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(3)
        self._sliders: Dict[str, QSlider] = {}
        self._readouts: Dict[str, Tuple[QLabel, str]] = {}
        specs = [("Hue", -180, 180, "°"), ("Sat", -100, 100, ""),
                 ("Light", -100, 100, "")]
        for row, (name, lo, hi, unit) in enumerate(specs):
            sld = QSlider(Qt.Horizontal)
            sld.setRange(lo, hi)
            sld.setValue(0)
            sld.valueChanged.connect(self._on_change)
            read = QLabel("0" + unit)
            read.setMinimumWidth(34)
            read.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            grid.addWidget(QLabel(name), row, 0)
            grid.addWidget(sld, row, 1)
            grid.addWidget(read, row, 2)
            self._sliders[name] = sld
            self._readouts[name] = (read, unit)
        root.addLayout(grid)

        btns = QHBoxLayout()
        self._reset_btn = QPushButton("Reset")
        self._reset_btn.clicked.connect(self._reset)
        self._apply_btn = QPushButton("Apply")
        self._apply_btn.clicked.connect(self._apply)
        btns.addWidget(self._reset_btn)
        btns.addWidget(self._apply_btn)
        root.addLayout(btns)
        root.addStretch(1)

        self.set_selection([], [])

    # ---- public --------------------------------------------------------

    def set_selection(self, slots: List[int], colors: List[Rgb]) -> None:
        """Snapshot the selection and zero the sliders (no signal). Empty
        selection disables the panel."""
        self._syncing = True
        self._base = {int(s): (int(c[0]), int(c[1]), int(c[2]))
                      for s, c in zip(slots, colors)}
        for sld in self._sliders.values():
            sld.setValue(0)
        self._refresh_readouts()
        n = len(self._base)
        self._title.setText(
            "Adjust colours" if n == 0
            else f"Adjust {n} colour{'s' if n != 1 else ''}"
        )
        self._set_enabled(n > 0)
        self._syncing = False

    # ---- internals -----------------------------------------------------

    def _set_enabled(self, on: bool) -> None:
        for sld in self._sliders.values():
            sld.setEnabled(on)
        self._reset_btn.setEnabled(on)
        self._apply_btn.setEnabled(on)

    def _deltas(self) -> Tuple[float, float, float]:
        return (float(self._sliders["Hue"].value()),
                float(self._sliders["Sat"].value()),
                float(self._sliders["Light"].value()))

    def _refresh_readouts(self) -> None:
        for name, sld in self._sliders.items():
            read, unit = self._readouts[name]
            v = sld.value()
            read.setText(f"{'+' if v > 0 else ''}{v}{unit}")

    def _compute(self) -> Dict[int, Rgb]:
        dh, ds, dl = self._deltas()
        if dh == 0 and ds == 0 and dl == 0:
            return dict(self._base)
        return {slot: _shift(rgb, dh, ds, dl) for slot, rgb in self._base.items()}

    def _on_change(self, _v: int = 0) -> None:
        if self._syncing:
            return
        self._refresh_readouts()
        self.previewChanged.emit(self._compute())

    def _reset(self) -> None:
        self._syncing = True
        for sld in self._sliders.values():
            sld.setValue(0)
        self._refresh_readouts()
        self._syncing = False
        self.previewChanged.emit({})  # empty = drop the tint, show base palette

    def _apply(self) -> None:
        dh, ds, dl = self._deltas()
        if not self._base or (dh == 0 and ds == 0 and dl == 0):
            return
        self.committed.emit(self._compute())
        # Host re-decodes + re-snapshots us via set_selection; reset locally too
        # so the pending delta doesn't linger if it doesn't.
        self._reset()
