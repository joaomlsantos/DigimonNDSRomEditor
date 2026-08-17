"""Interactive OAM cover editor canvas for the BTCHR browser.

Draw a sprite's shared OAM layout by hand: place legal OBJ rectangles over the
union render, move/delete them, and read the footprint live. All cells share one
layout (engine requirement), so editing happens once on the union and the rebuild
([`btchrspr.rebuild_with_manual_oam`]) applies it to every cell. This widget only
manages the rectangles + painting; validation/fs come from the core helpers.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from PySide6.QtCore import Qt, Signal, QPoint
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QWidget

from digimon_core import ncer as _ncer

# Legal NDS OBJ sizes in tiles (w, h), largest-area first — derived straight from
# the hardware shape/size table so the menu can never drift from what an OAM can
# actually encode. The DS has exactly 12 sizes: a 64-px side pairs only with
# 32/64, so there is no 64×8, 8×64, 64×16 or 16×64.
LEGAL_SHAPES: List[Tuple[int, int]] = sorted(
    {(w // 8, h // 8) for (w, h) in _ncer.SHAPE_SIZE.values()},
    key=lambda t: (-(t[0] * t[1]), -t[0]),
)


def fill_qcolor(frac: float) -> QColor:
    """Art-fill → colour: red (empty, cheap) → amber → green (solid)."""
    if frac < 0.5:
        t = frac / 0.5
        return QColor(220, int(45 + 120 * t), 35)
    t = (frac - 0.5) / 0.5
    return QColor(int(215 - 180 * t), 160, int(20 + 70 * t))


class OamEditCanvas(QWidget):
    """Editable OAM cover overlaid on a sprite's union render.

    Left-click empty space places the current shape; left-click a box selects it
    (drag to move); right-click / Delete removes it. Emits :attr:`changed` after
    any edit so the host can recompute fs + coverage."""

    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._base: Optional[QImage] = None       # union render, 1x
        self._zoom = 3
        self._gc = 0
        self._gr = 0
        self._union: List[List[bool]] = []
        self._origin: Tuple[int, int] = (0, 0)
        self._rects: List[List[int]] = []          # [tx, ty, tw, th]
        self._selected = -1
        self._shape: Tuple[int, int] = (2, 2)      # 16×16 default
        self._st = 4                               # tiles per slot (boundary/64)
        self._drag: Optional[Tuple[int, int]] = None
        self._hover: Optional[Tuple[int, int]] = None
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)

    # ---- data ----------------------------------------------------------

    def set_data(self, base: QImage, gc: int, gr: int, union, origin,
                 rects: List[Tuple[int, int, int, int]], slot_tiles: int = 4) -> None:
        self._base = base
        self._gc, self._gr = gc, gr
        self._union = union
        self._origin = origin
        self._st = max(1, slot_tiles)
        self._rects = [list(r) for r in rects]
        self._selected = -1
        self._drag = None
        self.setFixedSize(gc * 8 * self._zoom, gr * 8 * self._zoom)
        self.update()

    def set_shape(self, tw: int, th: int) -> None:
        self._shape = (tw, th)

    def rects(self) -> List[Tuple[int, int, int, int]]:
        return [tuple(r) for r in self._rects]

    def n_oams(self) -> int:
        return len(self._rects)

    def uncovered_count(self) -> int:
        """Opaque union tiles no OBJ covers — tile-level (cheap), for live edit
        feedback. > 0 means some art would vanish in-game."""
        if not self._union:
            return 0
        covered = set()
        for rx, ry, rw, rh in self._rects:
            for j in range(rh):
                for k in range(rw):
                    covered.add((rx + k, ry + j))
        return sum(
            1 for ty in range(self._gr) for tx in range(self._gc)
            if self._union[ty][tx] and (tx, ty) not in covered
        )

    # ---- geometry helpers ---------------------------------------------

    def _tile_at(self, pos: QPoint) -> Tuple[int, int]:
        s = 8 * self._zoom
        return (
            max(0, min(self._gc - 1, pos.x() // s)),
            max(0, min(self._gr - 1, pos.y() // s)),
        )

    def _topmost_at(self, tx: int, ty: int) -> int:
        for i in range(len(self._rects) - 1, -1, -1):  # last drawn = on top
            rx, ry, rw, rh = self._rects[i]
            if rx <= tx < rx + rw and ry <= ty < ry + rh:
                return i
        return -1

    def _clamp(self, tx: int, ty: int, tw: int, th: int) -> Tuple[int, int]:
        return (
            max(0, min(self._gc - tw, tx)),
            max(0, min(self._gr - th, ty)),
        )

    # ---- edit ops (unit-testable) -------------------------------------

    def place(self, tw: int, th: int, tx: int, ty: int) -> bool:
        if tw > self._gc or th > self._gr:
            return False
        tx, ty = self._clamp(tx, ty, tw, th)
        self._rects.append([tx, ty, tw, th])
        self._selected = len(self._rects) - 1
        self.update()
        self.changed.emit()
        return True

    def delete_selected(self) -> bool:
        if 0 <= self._selected < len(self._rects):
            self._rects.pop(self._selected)
            self._selected = -1
            self.update()
            self.changed.emit()
            return True
        return False

    def move_selected(self, tx: int, ty: int) -> None:
        if not (0 <= self._selected < len(self._rects)):
            return
        _, _, tw, th = self._rects[self._selected]
        tx, ty = self._clamp(tx, ty, tw, th)
        self._rects[self._selected][0] = tx
        self._rects[self._selected][1] = ty
        self.update()
        self.changed.emit()

    # ---- mouse / keys --------------------------------------------------

    def mousePressEvent(self, ev):
        tx, ty = self._tile_at(ev.position().toPoint())
        if ev.button() == Qt.RightButton:
            i = self._topmost_at(tx, ty)
            if i >= 0:
                self._selected = i
                self.delete_selected()
            return
        if ev.button() == Qt.LeftButton:
            i = self._topmost_at(tx, ty)
            if i >= 0:
                self._selected = i
                rx, ry, _, _ = self._rects[i]
                self._drag = (tx - rx, ty - ry)
                self.update()
            else:
                self.place(self._shape[0], self._shape[1], tx, ty)

    def mouseMoveEvent(self, ev):
        tx, ty = self._tile_at(ev.position().toPoint())
        self._hover = (tx, ty)
        if self._drag is not None and 0 <= self._selected < len(self._rects):
            dx, dy = self._drag
            self.move_selected(tx - dx, ty - dy)
        else:
            self.update()

    def mouseReleaseEvent(self, ev):
        self._drag = None

    def keyPressEvent(self, ev):
        if ev.key() in (Qt.Key_Delete, Qt.Key_Backspace):
            self.delete_selected()
        else:
            super().keyPressEvent(ev)

    def leaveEvent(self, ev):
        self._hover = None
        self.update()

    # ---- paint ---------------------------------------------------------

    def _fill_of(self, tx, ty, tw, th) -> float:
        if not self._union:
            return 1.0
        opq = sum(
            1 for j in range(th) for k in range(tw)
            if 0 <= ty + j < self._gr and 0 <= tx + k < self._gc
            and self._union[ty + j][tx + k]
        )
        return opq / (tw * th) if tw * th else 0.0

    def paintEvent(self, _ev):
        if self._base is None:
            return
        z = self._zoom
        w, h = self._gc * 8, self._gr * 8
        p = QPainter(self)
        p.drawImage(
            self.rect(),
            self._base.scaled(w * z, h * z, Qt.IgnoreAspectRatio, Qt.FastTransformation),
        )
        # uncovered opaque tiles → red wash (art that would vanish)
        covered = [[False] * self._gc for _ in range(self._gr)]
        for rx, ry, rw, rh in self._rects:
            for j in range(rh):
                for k in range(rw):
                    if 0 <= ry + j < self._gr and 0 <= rx + k < self._gc:
                        covered[ry + j][rx + k] = True
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(230, 40, 40, 110))
        for ty in range(self._gr):
            for tx in range(self._gc):
                if self._union and self._union[ty][tx] and not covered[ty][tx]:
                    p.fillRect(tx * 8 * z, ty * 8 * z, 8 * z, 8 * z, QColor(230, 40, 40, 120))
        # tile grid
        p.setPen(QPen(QColor(70, 70, 90, 45)))
        for gx in range(0, w + 1, 8):
            p.drawLine(gx * z, 0, gx * z, h * z)
        for gy in range(0, h + 1, 8):
            p.drawLine(0, gy * z, w * z, gy * z)
        # rects
        ox, oy = self._origin
        font = QFont(); font.setPixelSize(11); font.setBold(True)
        p.setFont(font)
        for i, (rx, ry, rw, rh) in enumerate(self._rects):
            frac = self._fill_of(rx, ry, rw, rh)
            col = fill_qcolor(frac)
            x0, y0 = rx * 8 * z, ry * 8 * z
            sel = i == self._selected
            pen = QPen(QColor(255, 235, 60) if sel else col)
            pen.setWidth(3 if sel else 2)
            p.setPen(pen); p.setBrush(Qt.NoBrush)
            p.drawRect(x0, y0, rw * 8 * z - 1, rh * 8 * z - 1)
            # tiles it costs: area rounds up to a whole slot (st tiles)
            slots = (rw * rh + self._st - 1) // self._st
            lbl = str(slots * self._st)
            p.fillRect(x0 + 2, y0 + 2, 7 * len(lbl) + 5, 14, QColor(0, 0, 0, 150))
            p.setPen(QColor(255, 255, 255))
            p.drawText(x0 + 4, y0 + 13, lbl)
        # hover ghost of the shape to place
        if self._hover is not None and self._drag is None:
            tw, th = self._shape
            gx, gy = self._clamp(self._hover[0], self._hover[1], tw, th)
            p.setPen(QPen(QColor(120, 220, 255, 200), 2, Qt.DashLine))
            p.setBrush(QColor(120, 220, 255, 40))
            p.drawRect(gx * 8 * z, gy * 8 * z, tw * 8 * z - 1, th * 8 * z - 1)
        p.end()
