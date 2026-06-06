"""Reusable transparent-colour picker for sprite editors.

The widget owns three small things only:

1. A hex field + colour swatch + checkable "Pick from image" button.
2. Click routing — caller registers preview QLabels via :meth:`bind_preview`
   and the widget installs an event filter that maps clicks (in label
   coords) back to source-image pixels.
3. A single ``on_color_picked((r, g, b))`` callback fired on either a typed
   hex value or a sampled non-transparent preview pixel.

It deliberately does NOT know about palette layout, banks, or NCGR/NCLR
formats — those vary between BTCHR (8bpp, single bank), MCHR (4bpp, per-id
banks), and sprite_browser (mixed). Each editor passes its own
``on_color_picked`` that handles the apply step.

This keeps the picker small enough that the three editors that need one
can share it without contorting their per-format apply logic.
"""
from __future__ import annotations

from typing import Callable, Optional, Tuple

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QImage
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QWidget,
)


# (source QImage at native size, source size in px, displayed pixmap size in px).
# `None` indicates the bound label has no source to sample (e.g. the editor
# is between selections).
PreviewSource = Optional[Tuple[QImage, Tuple[int, int], Tuple[int, int]]]
SourceProvider = Callable[[], PreviewSource]


class TransparentColorPicker(QWidget):
    """Hex field + swatch + eyedropper, with click capture on bound previews."""

    def __init__(
        self,
        *,
        on_color_picked: Callable[[Tuple[int, int, int]], None],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._on_color_picked = on_color_picked
        self._picking: bool = False
        # label → source-provider callback. Each provider returns the live
        # native-size QImage + scaled pixmap size, refreshed on every render
        # by the host editor.
        self._sources: dict[QObject, SourceProvider] = {}

        self._hex = QLineEdit()
        self._hex.setMaxLength(7)  # "#RRGGBB"
        self._hex.setPlaceholderText("#RRGGBB")
        self._hex.setFixedWidth(80)
        self._hex.editingFinished.connect(self._on_hex_submitted)

        self._swatch = QLabel()
        self._swatch.setFixedSize(20, 20)
        self._swatch.setFrameStyle(0)
        self._swatch.setStyleSheet(
            "background-color: transparent; border: 1px solid #555;"
        )

        self._pick_btn = QPushButton("Pick from image")
        self._pick_btn.setCheckable(True)
        self._pick_btn.toggled.connect(self._on_pick_toggled)

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(QLabel("Transparent color:"))
        row.addWidget(self._hex)
        row.addWidget(self._swatch)
        row.addWidget(self._pick_btn)
        row.addStretch(1)

    # ---- public API -----------------------------------------------------

    def bind_preview(self, label: QLabel, source_provider: SourceProvider) -> None:
        """Register ``label`` as a clickable preview.

        ``source_provider`` is called on each click to fetch the live
        source QImage + sizes (so the picker doesn't need to be re-bound
        on every render; the host editor refreshes the cached source as
        it pleases).
        """
        label.installEventFilter(self)
        self._sources[label] = source_provider

    def set_current_color(self, rgb: Optional[Tuple[int, int, int]]) -> None:
        """Update the hex field + swatch from external state. Pass ``None``
        to clear (e.g. no selection)."""
        if rgb is None:
            self._hex.blockSignals(True)
            self._hex.setText("")
            self._hex.blockSignals(False)
            self._swatch.setStyleSheet(
                "background-color: transparent; border: 1px solid #555;"
            )
            return
        r, g, b = rgb
        hex_str = f"#{r:02X}{g:02X}{b:02X}"
        self._hex.blockSignals(True)
        self._hex.setText(hex_str)
        self._hex.blockSignals(False)
        self._swatch.setStyleSheet(
            f"background-color: {hex_str}; border: 1px solid #555;"
        )

    # ---- internals ------------------------------------------------------

    @staticmethod
    def _parse_hex(text: str) -> Optional[Tuple[int, int, int]]:
        s = text.strip().lstrip("#")
        if len(s) == 3:
            s = "".join(c * 2 for c in s)
        if len(s) != 6:
            return None
        try:
            return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
        except ValueError:
            return None

    def _on_hex_submitted(self) -> None:
        rgb = self._parse_hex(self._hex.text())
        if rgb is None:
            QMessageBox.warning(
                self, "Bad color",
                "Enter a hex color like #FF00FF or #f0f.",
            )
            return
        self._on_color_picked(rgb)

    def _on_pick_toggled(self, checked: bool) -> None:
        self._picking = checked
        for label in self._sources:
            if checked:
                label.setCursor(Qt.CrossCursor)
            else:
                label.unsetCursor()

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 (Qt convention)
        if (
            self._picking
            and event.type() == QEvent.MouseButtonPress
            and obj in self._sources
        ):
            pos = event.position()
            self._sample(obj, pos.x(), pos.y())
            return True
        return super().eventFilter(obj, event)

    def _sample(self, label: QObject, click_x: float, click_y: float) -> None:
        provider = self._sources.get(label)
        if provider is None:
            return
        source = provider()
        if source is None:
            return
        qimg, (src_w, src_h), (pix_w, pix_h) = source
        if src_w == 0 or src_h == 0 or pix_w == 0 or pix_h == 0:
            return
        # QLabel centers its pixmap when the label is larger than the
        # pixmap; subtract that offset before scaling back to source coords.
        lbl: QLabel = label  # type: ignore[assignment]
        label_w = lbl.width()
        label_h = lbl.height()
        off_x = max(0, (label_w - pix_w) // 2)
        off_y = max(0, (label_h - pix_h) // 2)
        sx = int((click_x - off_x) * src_w / pix_w)
        sy = int((click_y - off_y) * src_h / pix_h)
        if not (0 <= sx < src_w and 0 <= sy < src_h):
            return
        color = qimg.pixelColor(sx, sy)
        # Skip transparent background — the user clicked through the
        # sprite, not on it. Stay in pick mode so a stray miss doesn't
        # force a button re-toggle.
        if color.alpha() == 0:
            return
        rgb = (color.red(), color.green(), color.blue())
        self._pick_btn.setChecked(False)
        self._on_color_picked(rgb)
