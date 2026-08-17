"""Frame-alignment canvas for the BTCHR browser.

Drag one animation frame to translate it — keeping its OAM structure exactly
(pure :func:`ncer.shift_cell_oams`, only the position changes) — while the other
frames show as faint ghosts (onion-skin). Aligning the frames' content shrinks
the shared footprint toward the biggest single frame, so a following Compress OAM
re-covers into a smaller structure. The widget only handles drag + painting; the
host applies the move and recomputes the real fs.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from PySide6.QtCore import Qt, Signal, QRect
from PySide6.QtGui import QColor, QImage, QPainter, QPen
from PySide6.QtWidgets import QWidget


class FrameAlignCanvas(QWidget):
    """Onion-skinned per-frame mover. Drag the current frame; release commits a
    ``shift_cell_oams`` via :attr:`committed`. :attr:`footprint` reports the live
    union tile count as you align."""

    committed = Signal(int, int, int)   # (cell_idx, dx, dy) — apply + one undo
    footprint = Signal(int)             # live union tile count

    def __init__(self, parent=None):
        super().__init__(parent)
        self._imgs: List[QImage] = []          # each cell on the shared canvas
        self._occ: List[List[List[bool]]] = []  # tile occupancy per cell
        self._gc = 0
        self._gr = 0
        self._current = 0
        self._zoom = 3
        self._off: Tuple[int, int] = (0, 0)     # live drag offset (px)
        self._drag: Optional[Tuple[int, int]] = None
        self.setMouseTracking(False)
        self.setFocusPolicy(Qt.StrongFocus)

    # ---- data ----------------------------------------------------------

    def set_data(self, imgs, occ, gc, gr, current=0) -> None:
        self._imgs = imgs
        self._occ = occ
        self._gc, self._gr = gc, gr
        self._current = max(0, min(current, len(imgs) - 1)) if imgs else 0
        self._off = (0, 0)
        self._drag = None
        self.setFixedSize(gc * 8 * self._zoom, gr * 8 * self._zoom)
        self.update()
        self._emit_footprint()

    def set_current(self, idx: int) -> None:
        if 0 <= idx < len(self._imgs) and idx != self._current:
            self._current = idx
            self._off = (0, 0)
            self._drag = None
            self.update()
            self._emit_footprint()

    # ---- footprint -----------------------------------------------------

    def _union_count(self, dtx: int, dty: int) -> int:
        """Union opaque-tile count with the current cell shifted by (dtx, dty)
        tiles. Tile-granular approximation for live feedback; the host recomputes
        the exact fs on commit."""
        if not self._occ:
            return 0
        u = [[False] * self._gc for _ in range(self._gr)]
        for k, occ in enumerate(self._occ):
            for ty in range(self._gr):
                row = occ[ty]
                for tx in range(self._gc):
                    if not row[tx]:
                        continue
                    if k == self._current:
                        ny, nx = ty + dty, tx + dtx
                        if 0 <= ny < self._gr and 0 <= nx < self._gc:
                            u[ny][nx] = True
                    else:
                        u[ty][tx] = True
        return sum(sum(r) for r in u)

    def _emit_footprint(self) -> None:
        dx, dy = self._off
        self.footprint.emit(self._union_count(round(dx / 8), round(dy / 8)))

    # ---- mouse ---------------------------------------------------------

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton and self._imgs:
            pt = ev.position().toPoint()
            self._drag = (pt.x(), pt.y())
            self._off = (0, 0)

    def mouseMoveEvent(self, ev):
        if self._drag is None:
            return
        pt = ev.position().toPoint()
        self._off = (
            (pt.x() - self._drag[0]) // self._zoom,
            (pt.y() - self._drag[1]) // self._zoom,
        )
        self.update()
        self._emit_footprint()

    def mouseReleaseEvent(self, ev):
        if self._drag is None:
            return
        self._drag = None
        dx, dy = self._off
        self._off = (0, 0)
        if dx or dy:
            self.committed.emit(self._current, dx, dy)  # host applies + re-seeds
        else:
            self.update()

    # ---- paint ---------------------------------------------------------

    def paintEvent(self, _ev):
        if not self._imgs:
            return
        z = self._zoom
        W, H = self._gc * 8 * z, self._gr * 8 * z
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(247, 247, 250))
        full = QRect(0, 0, W, H)
        # ghosts: every other frame, faint
        p.setOpacity(0.26)
        for k, img in enumerate(self._imgs):
            if k != self._current:
                p.drawImage(full, img)
        # tile grid
        p.setOpacity(1.0)
        p.setPen(QPen(QColor(70, 70, 90, 45)))
        for gx in range(0, self._gc + 1):
            p.drawLine(gx * 8 * z, 0, gx * 8 * z, H)
        for gy in range(0, self._gr + 1):
            p.drawLine(0, gy * 8 * z, W, gy * 8 * z)
        # current frame, full opacity, shifted by the live drag offset
        dx, dy = self._off
        cur = QRect(dx * z, dy * z, W, H)
        p.drawImage(cur, self._imgs[self._current])
        # a cyan frame around it so it reads as the movable one
        p.setPen(QPen(QColor(60, 180, 235, 230), 2))
        p.setBrush(Qt.NoBrush)
        p.drawRect(cur.adjusted(0, 0, -1, -1))
        p.end()
