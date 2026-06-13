"""Mouse-driven paint canvas — a QLabel that emits image-space coords.

The label displays a pixmap at native (1:1) size and forwards mouse
press / drag / release as ``painted(x, y, buttons, modifiers)`` and
``paintFinished()`` signals, where ``x`` and ``y`` are *image* pixel
coordinates (the centering offset Qt applies when the label is larger
than the pixmap is subtracted before reporting). Only left-button
events emit ``painted`` — right-button is reserved for pan.

When hover is enabled (``setHoverEnabled(True)``) the canvas also emits
``hovered(x, y)`` while the cursor is over the pixmap with no button
held, and ``hoverLeft()`` when it leaves — drives the field-map paint
tab's "about-to-stamp" cell highlight.

When the displayed pixmap is upscaled (``setImageScale(N)``) reported
coordinates are divided by N so the painter and picker stay in image
space regardless of zoom.

Two extra signals support host-driven zoom/pan UX:

- ``zoomStepRequested(steps)`` fires on Ctrl+wheel — host walks its
  zoom level list by ``steps`` (positive = in, negative = out).
- ``panRequested(dx, dy)`` fires while the right mouse button is
  dragged — host shifts its enclosing QScrollArea's scroll bars by
  ``(-dx, -dy)`` to give a Photoshop-style hand-tool feel.
"""
from __future__ import annotations

from PySide6.QtCore import QPointF, Qt, Signal
from PySide6.QtGui import QMouseEvent, QWheelEvent
from PySide6.QtWidgets import QLabel


class PaintCanvas(QLabel):
    """Pixel-coordinate-emitting label.

    ``painted(x, y, buttons, modifiers)`` fires once per left-button press
    and per move event while the left button is held. ``paintFinished()``
    fires on left-button release — paint tools push their undo command
    here after a stroke.

    Coordinates are integer pixel positions within the displayed image,
    clamped to ``[0, image_w/h - 1]``. When no pixmap is set the
    signals don't fire.
    """

    painted = Signal(int, int, Qt.MouseButtons, Qt.KeyboardModifiers)
    paintFinished = Signal()
    hovered = Signal(int, int)
    hoverLeft = Signal()
    zoomStepRequested = Signal(int)
    panRequested = Signal(int, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(False)  # only emit while a button is held by default
        self._dragging = False
        self._panning = False
        self._pan_last: QPointF = QPointF(0.0, 0.0)
        self._image_scale = 1
        self._hover_enabled = False

    def setImageScale(self, scale: int) -> None:  # noqa: N802
        """Tell the canvas the displayed pixmap is upscaled by ``scale``.

        Used to convert mouse coords back into the underlying image
        space — without it a 2× zoom would report a 16-pixel-wide hit
        zone as 16 image pixels rather than 8.
        """
        self._image_scale = max(1, int(scale))

    def setHoverEnabled(self, enabled: bool) -> None:  # noqa: N802
        """Opt in to hover (``hovered`` / ``hoverLeft``) signals.

        Off by default so the walkability tab — which only cares about
        drag events — doesn't spend cycles emitting hover signals nobody
        listens to.
        """
        self._hover_enabled = bool(enabled)
        self.setMouseTracking(bool(enabled))

    def _map_to_image(self, ev_x: float, ev_y: float) -> tuple[int, int] | None:
        pm = self.pixmap()
        if pm is None or pm.isNull():
            return None
        pix_w = pm.width()
        pix_h = pm.height()
        # QLabel centers its pixmap when the label is larger than it.
        off_x = max(0, (self.width() - pix_w) // 2)
        off_y = max(0, (self.height() - pix_h) // 2)
        rx = int(ev_x) - off_x
        ry = int(ev_y) - off_y
        if rx < 0 or ry < 0 or rx >= pix_w or ry >= pix_h:
            return None
        return rx // self._image_scale, ry // self._image_scale

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.RightButton:
            # Right-button drag = pan. Start tracking even if the cursor
            # is off the pixmap so the user can grab from the margin.
            self._panning = True
            self._pan_last = event.position()
            self.setCursor(Qt.ClosedHandCursor)
            event.accept()
            return
        if event.button() != Qt.LeftButton:
            super().mousePressEvent(event)
            return
        coords = self._map_to_image(event.position().x(), event.position().y())
        if coords is None:
            super().mousePressEvent(event)
            return
        self._dragging = True
        self.painted.emit(coords[0], coords[1], event.buttons(), event.modifiers())
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._panning:
            cur = event.position()
            dx = int(cur.x() - self._pan_last.x())
            dy = int(cur.y() - self._pan_last.y())
            self._pan_last = cur
            if dx or dy:
                self.panRequested.emit(dx, dy)
            event.accept()
            return
        coords = self._map_to_image(event.position().x(), event.position().y())
        if self._dragging:
            if coords is None:
                event.accept()
                return
            self.painted.emit(coords[0], coords[1], event.buttons(), event.modifiers())
            event.accept()
            return
        if self._hover_enabled:
            if coords is None:
                self.hoverLeft.emit()
            else:
                self.hovered.emit(coords[0], coords[1])
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._panning and event.button() == Qt.RightButton:
            self._panning = False
            self.unsetCursor()
            event.accept()
            return
        if self._dragging and event.button() == Qt.LeftButton:
            self._dragging = False
            self.paintFinished.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802
        if event.modifiers() & Qt.ControlModifier:
            # Standard wheel delta is 120 per notch — sign carries the
            # direction (up = zoom in, down = zoom out).
            steps = event.angleDelta().y() // 120
            if steps != 0:
                self.zoomStepRequested.emit(steps)
            event.accept()
            return
        # Let the parent QScrollArea handle plain wheel scrolling.
        event.ignore()

    def leaveEvent(self, event) -> None:  # noqa: N802
        if self._hover_enabled:
            self.hoverLeft.emit()
        super().leaveEvent(event)
