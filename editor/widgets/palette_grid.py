"""Editable palette swatch grid.

Visualises a palette as a grid of swatches. Three interaction modes, layered:

* Default (popup): clicking a swatch opens the native colour picker and
  reports ``colorEdited(slot, (r, g, b))``. The host quantises to NDS 5-bit
  BGR555 on write-back.
* Select mode (``set_select_mode(True)``): a click *selects* a slot for an
  external inline editor instead, reporting ``selectedChanged(slot)``.
* Multi-select (``set_multi_select(True)``, requires select mode): shift-click
  extends a linear range, ctrl/⌘-click toggles a slot, and a drag sweeps a
  rectangular marquee — for batch recolour tools. ``selectionChanged`` fires
  on every selection change; the host reads :meth:`selected_slots`.

Reusable across every graphics browser (SPR / MCHR / BTCHR / map): each just
feeds ``set_palette`` its current bank and wires the signals to its own NCLR
rebuild + undo path. :meth:`set_preview` lets a live transform tint the
swatches without touching the underlying palette.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

from PySide6.QtCore import QRect, Qt, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPen
from PySide6.QtWidgets import QColorDialog, QWidget

Rgb = Tuple[int, int, int]


class PaletteGrid(QWidget):
    """Grid of colour swatches; click a swatch to recolour or select it.

    ``colorEdited(slot, (r, g, b))`` fires after the user picks a new colour
    in popup mode. Slot 0 is drawn with a diagonal marker because the engine
    treats palette index 0 as transparent regardless of its stored RGB.
    """

    colorEdited = Signal(int, tuple)
    selectedChanged = Signal(int)  # anchor (last-clicked) slot, select mode only
    selectionChanged = Signal()    # any change to the multi-selection set

    def __init__(self, cols: int = 16, swatch: int = 15, parent=None):
        super().__init__(parent)
        self._cols = max(1, cols)
        self._swatch = max(4, swatch)
        self._colors: List[Rgb] = []
        self._hover = -1
        # Select mode (opt-in): a click SELECTS a slot (emits selectedChanged)
        # for an external inline editor, instead of opening the colour popup.
        # Default off — hosts that haven't adopted the inline editor keep the
        # click-opens-popup behaviour unchanged.
        self._selected = -1                 # anchor / primary (drives 1-slot editor)
        self._select_mode = False
        # Multi-select (opt-in, requires select mode): the full selection set
        # plus rubber-band drag state. Off keeps the pure single-select feel.
        self._multi_select = False
        self._selection: set = set()
        self._drag_anchor = -1              # slot where a marquee drag began
        self._dragging = False
        # Live preview overrides slot→rgb for painting only; the real palette
        # (``_colors``) is untouched so a transform can be cancelled cleanly.
        self._preview: Dict[int, Rgb] = {}
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)
        self._update_size()

    def set_select_mode(self, on: bool) -> None:
        self._select_mode = bool(on)

    def set_cols(self, cols: int) -> None:
        """Reflow the grid to ``cols`` columns (same colours, new layout). Lets
        a host widen the palette to show more swatches per row."""
        cols = max(1, int(cols))
        if cols != self._cols:
            self._cols = cols
            self._update_size()
            self.update()

    def set_multi_select(self, on: bool) -> None:
        """Enable shift/ctrl/drag multi-selection (needs select mode)."""
        self._multi_select = bool(on)

    def selected(self) -> int:
        return self._selected

    def selected_slots(self) -> List[int]:
        """Ascending list of every selected slot (the anchor included)."""
        return sorted(self._selection)

    def color_at(self, slot: int) -> Rgb:
        if 0 <= slot < len(self._colors):
            return self._colors[slot]
        return (0, 0, 0)

    def slot_top_left(self, slot: int) -> Tuple[int, int]:
        """Pixel (x, y) of a slot's top-left in the grid — lets a host scroll
        area reveal it (``ensureVisible``)."""
        col = slot % self._cols
        row = slot // self._cols
        return (col * self._swatch, row * self._swatch)

    def set_selected(self, slot: int) -> None:
        slot = slot if 0 <= slot < len(self._colors) else -1
        changed = slot != self._selected or self._selection != (
            {slot} if slot >= 0 else set()
        )
        self._selected = slot
        self._selection = {slot} if slot >= 0 else set()
        if changed:
            self.update()

    def select_slot(self, slot: int) -> None:
        """Programmatically single-select ``slot`` and emit the selection
        signals (so external editors / adjusters update), as if clicked."""
        if 0 <= slot < len(self._colors):
            self._set_selection({slot}, slot)

    # ---- preview -------------------------------------------------------

    def set_preview(self, overrides: Optional[Dict[int, Rgb]]) -> None:
        """Tint the given slots for display only (a live transform preview),
        or clear the preview when ``overrides`` is falsy."""
        self._preview = dict(overrides) if overrides else {}
        self.update()

    # ---- data ----------------------------------------------------------

    def set_palette(self, colors: Sequence[Rgb]) -> None:
        self._colors = [(int(r), int(g), int(b)) for (r, g, b) in colors]
        self._preview = {}
        if self._hover >= len(self._colors):
            self._hover = -1
        if self._selected >= len(self._colors):
            self._selected = -1
        # Keep the multi-selection across a re-decode (edit / undo) so a batch
        # transform's selection survives the commit-refresh round trip.
        self._selection = {s for s in self._selection if 0 <= s < len(self._colors)}
        self._update_size()
        self.update()

    def _rows(self) -> int:
        n = len(self._colors)
        return (n + self._cols - 1) // self._cols if n else 0

    def _update_size(self) -> None:
        w = self._cols * self._swatch + 1
        h = max(1, self._rows()) * self._swatch + 1
        self.setFixedSize(w, h)

    def _slot_at(self, x: int, y: int) -> int:
        col = x // self._swatch
        row = y // self._swatch
        if not (0 <= col < self._cols):
            return -1
        idx = row * self._cols + col
        return idx if 0 <= idx < len(self._colors) else -1

    # ---- paint ---------------------------------------------------------

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        s = self._swatch
        grid_pen = QPen(QColor(0, 0, 0, 90), 1)
        for i, base in enumerate(self._colors):
            r, g, b = self._preview.get(i, base)
            col = i % self._cols
            row = i // self._cols
            rect = QRect(col * s, row * s, s, s)
            painter.fillRect(rect, QColor(r, g, b))
            if i == 0:
                # Transparent slot — index 0 renders as backdrop in-game.
                painter.setPen(QPen(QColor(255, 255, 255, 170), 1))
                painter.drawLine(rect.topRight(), rect.bottomLeft())
            painter.setPen(grid_pen)
            painter.drawRect(rect)
        sel_pen = QPen(QColor(80, 180, 255, 255), 2)
        for slot in self._selection:
            col = slot % self._cols
            row = slot // self._cols
            painter.setPen(sel_pen)
            painter.drawRect(QRect(col * s, row * s, s - 1, s - 1))
        if 0 <= self._hover < len(self._colors):
            col = self._hover % self._cols
            row = self._hover // self._cols
            painter.setPen(QPen(QColor(255, 255, 255, 235), 2))
            painter.drawRect(QRect(col * s + 1, row * s + 1, s - 2, s - 2))
        painter.end()

    # ---- interaction ---------------------------------------------------

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        x, y = int(event.position().x()), int(event.position().y())
        if self._dragging and self._drag_anchor >= 0:
            i = self._slot_at(x, y)
            if i >= 0:
                self._select_marquee(self._drag_anchor, i)
            return
        i = self._slot_at(x, y)
        if i != self._hover:
            self._hover = i
            if 0 <= i < len(self._colors):
                r, g, b = self._colors[i]
                self.setToolTip(f"Slot {i} — #{r:02X}{g:02X}{b:02X}  (click to edit)")
            else:
                self.setToolTip("")
            self.update()

    def leaveEvent(self, _event) -> None:
        if self._hover != -1:
            self._hover = -1
            self.update()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.LeftButton:
            super().mousePressEvent(event)
            return
        i = self._slot_at(int(event.position().x()), int(event.position().y()))
        if not (0 <= i < len(self._colors)):
            return
        if not self._select_mode:
            self._open_popup(i)
            return
        if self._multi_select:
            mods = event.modifiers()
            if mods & Qt.ShiftModifier and self._selected >= 0:
                self._select_range(self._selected, i)  # anchor kept
                return
            if mods & (Qt.ControlModifier | Qt.MetaModifier):
                self._toggle(i)
                return
            # Plain click: single-select and arm a marquee drag from here.
            self._drag_anchor = i
            self._dragging = True
        self._set_selection({i}, anchor=i)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._dragging = False
        self._drag_anchor = -1

    # ---- selection helpers ---------------------------------------------

    def _emit_selection(self) -> None:
        self.selectedChanged.emit(self._selected)
        self.selectionChanged.emit()
        self.update()

    def _set_selection(self, slots: set, anchor: int) -> None:
        self._selection = {s for s in slots if 0 <= s < len(self._colors)}
        self._selected = anchor if anchor in self._selection else (
            min(self._selection) if self._selection else -1
        )
        self._emit_selection()

    def _select_range(self, a: int, b: int) -> None:
        lo, hi = (a, b) if a <= b else (b, a)
        self._selection = set(range(lo, hi + 1))
        self._selected = a  # keep the original anchor
        self._emit_selection()

    def _select_marquee(self, a: int, b: int) -> None:
        ar, ac = a // self._cols, a % self._cols
        br, bc = b // self._cols, b % self._cols
        r0, r1 = sorted((ar, br))
        c0, c1 = sorted((ac, bc))
        slots = {
            r * self._cols + c
            for r in range(r0, r1 + 1)
            for c in range(c0, c1 + 1)
            if 0 <= r * self._cols + c < len(self._colors)
        }
        self._set_selection(slots, anchor=a)

    def _toggle(self, i: int) -> None:
        if i in self._selection:
            self._selection.discard(i)
        else:
            self._selection.add(i)
        self._selected = i if i in self._selection else (
            min(self._selection) if self._selection else -1
        )
        self._emit_selection()

    def _open_popup(self, i: int) -> None:
        r, g, b = self._colors[i]
        chosen: Optional[QColor] = QColorDialog.getColor(
            QColor(r, g, b), self, f"Palette slot {i}",
        )
        if chosen.isValid():
            self.colorEdited.emit(i, (chosen.red(), chosen.green(), chosen.blue()))
