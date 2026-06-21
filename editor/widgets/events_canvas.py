"""OVERWORLD_SPRITE marker canvas for the field-map Events tab.

QGraphicsView/QGraphicsScene-based painter. The composite map render
sits at the bottom of the scene as a static pixmap; each
OVERWORLD_SPRITE placement is rendered as a draggable marker item
floating above it. Pixel-space (x, y) from the overlay5 payload maps
1-to-1 to scene coordinates — no scaling needed.

Marker rendering falls back through two layers:

1. The MCHR sprite at ``overworld_sprite_id`` — the script field is
   already an MCHR index, so it's a direct lookup (no detour through
   the sprite-map row).
2. A colored disc + hex MCHR id, when the MCHR lookup fails (no
   pixmap available, parse error).

Drag support (Phase E in PLAN.md §14.9) is wired through a ``moved``
callback supplied by the parent — the canvas reports
``(block_offset, x, y)`` after each drop so the parent can push an
undo command without this widget needing to know about
:class:`QUndoStack`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QPainter,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QGraphicsItem,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGraphicsView,
)


# Marker geometry. Disc radius is in scene px (= map pixel space); a
# 6 px radius matches the visual weight of an MCHR sprite tile at 1x.
_DISC_RADIUS = 6
# Z order — pixmap background sits at 0, exit overlays float just above
# it, sprite markers above those, and selected / dragged items nudge
# higher so highlights aren't clipped by neighbors.
_Z_BACKGROUND = 0
_Z_EXIT = 5
_Z_EXIT_HOT = 15
_Z_MARKER = 10
_Z_MARKER_HOT = 20

# Tile -> scene-pixel scale for exit-zone / spawn-point rendering. Exit
# blocks store TILE coords (unlike OVERWORLD_SPRITE x/y which is in
# pixel space); one tile spans 8 composite-render pixels, so multiply
# tile coords by 8 to land in the same scene space as the background
# pixmap. The in-game renderer applies an isometric projection on top,
# but the composite render the canvas paints under is rectilinear —
# flat *8 lines up with that.
_TILE_PIXEL_SCALE = 8

# Exit / spawn / hitbox colors. Blue translucent for exit boxes, green
# for spawn diamonds, orange for "interaction" hitboxes (non-standard
# handlers — talking to an unreachable NPC, cutscene triggers); yellow
# outline takes over on selection so the highlight reads on top of any.
_EXIT_FILL = QColor(80, 180, 255, 90)
_EXIT_OUTLINE = QColor(80, 180, 255, 220)
_SPAWN_FILL = QColor(120, 220, 120, 150)
_SPAWN_OUTLINE = QColor(60, 180, 60, 240)
_HITBOX_FILL = QColor(255, 165, 0, 90)
_HITBOX_OUTLINE = QColor(255, 165, 0, 220)
_SELECT_OUTLINE = QColor(255, 220, 80, 240)

# Stable per-id color when MCHR art isn't available. Cycles through a
# small palette so adjacent markers in the same map don't share a hue.
_FALLBACK_COLORS = (
    QColor(255,  80,  80),
    QColor(255, 165,   0),
    QColor(255, 230,   0),
    QColor(110, 230,  80),
    QColor( 80, 200, 255),
    QColor(200, 130, 255),
    QColor(255, 110, 200),
)


@dataclass
class EventMarkerSpec:
    """One marker to place on the canvas — codec output + display name.

    Kept separate from :class:`overlay5.OverworldSpritePlacement` so the
    canvas doesn't need a session import; the parent widget does the
    name + sprite resolution and hands us already-painted artifacts.
    """
    block_offset: int      # entry-relative offset of the OVERWORLD_SPRITE block
    overworld_sprite_id: int  # MCHR idx, *not* sprite_map digimon_id
    x: int
    y: int
    label: str             # e.g. "ToyAgumon (0x00a2)"
    pixmap: Optional[QPixmap]  # MCHR sprite render, or None for disc fallback
    behavior: int = 0      # placement's behavior u16 (MCHR frame index)
    # Optional override for the on-canvas badge above the marker. Default
    # ``None`` keeps the existing ``0x{overworld_sprite_id:04x}`` form
    # used by Events tab markers; the Cutscenes tab passes an explicit
    # ``entry NNNN @0xPPPP`` so the user can match each chain-injected
    # NPC back to the script byte that placed it.
    display_label: Optional[str] = None


# Callback signature for drag-finished events. Phase E wires this up to
# a QUndoCommand push; Phase C leaves it unset.
EventMovedCallback = Callable[[int, int, int], None]
# (block_offset, new_x, new_y)

# Right-click on a marker. Parent gets the marker's block_offset + the
# global screen position so it can pop its own QMenu (sprite-id swap,
# delete, etc.). Cutscenes tab uses this to surface "Change sprite…"
# without growing a permanent sidebar in chip mode.
MarkerContextMenuCallback = Callable[[int, "QPoint"], None]
# (block_offset, global_pos)

# Drag-finished callback for exit / spawn items. Reports the new tile
# bounding box so the box's width/height are preserved across the drag
# (parent translates the old box by the drop delta and pushes one
# :class:`EditExitBoxCommand`). Spawns arrive as a degenerate box with
# x1==x2 and y1==y2.
ExitMovedCallback = Callable[[int, int, int, int, int], None]
# (block_offset, new_x1, new_y1, new_x2, new_y2)


@dataclass
class ExitZoneSpec:
    """One 0x001b exit-zone or spawn-point block to render on the canvas.

    Tile coords come straight from the overlay5 payload; the canvas
    multiplies by :data:`_TILE_PIXEL_SCALE` to place items in pixel
    space. ``dest_label`` is a precomputed string used as the tooltip —
    typically the destination map's name and id ("Dark Plaza (map 26)"),
    or ``""`` for spawn points (no destination).

    ``display_idx`` is a per-type sequential counter (exits get their
    own 0,1,2,… run, spawns get their own, hitboxes get their own)
    used for the on-canvas "E{n}" / "S{n}" / "H{n}" badge so the labels
    read densely even when the underlying block ``idx`` field
    interleaves the three.

    ``is_hitbox`` flags an interaction trigger — a 0x001b block whose
    handler isn't the standard `0x0002 + 0x0030` map-exit pair. Painted
    in a different colour and treated as read-only by the sidebar so a
    bespoke cutscene/dialog trigger doesn't get accidentally rewritten
    as a map exit.
    """
    block_offset: int
    idx: int
    x1: int
    y1: int
    x2: int
    y2: int
    is_spawn: bool
    dest_label: str = ""
    display_idx: int = 0
    is_hitbox: bool = False


class _EventMarkerItem(QGraphicsItem):
    """One overworld-sprite marker. Pixmap on top, hex id label
    underneath; draggable via ``ItemIsMovable``.

    The QGraphicsItem subclass route (rather than composing a
    pixmap-item with a child text-item) keeps the bounding rect tight
    enough that the drag hitbox lines up with what the user sees.
    """

    def __init__(
        self,
        spec: EventMarkerSpec,
        moved_cb: Optional[EventMovedCallback] = None,
        context_menu_cb: Optional[MarkerContextMenuCallback] = None,
    ) -> None:
        super().__init__()
        self._spec = spec
        self._moved_cb = moved_cb
        self._context_menu_cb = context_menu_cb
        self.setFlag(QGraphicsItem.ItemIsMovable, moved_cb is not None)
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.ItemSendsGeometryChanges, moved_cb is not None)
        self.setAcceptHoverEvents(True)
        self.setToolTip(spec.label)
        self.setZValue(_Z_MARKER)
        # Item-local (0, 0) is the placement point — the disc/pixmap
        # centers on it. setPos() takes scene coords directly.
        self.setPos(QPointF(spec.x, spec.y))
        # Disc fallback color is determined per-id so re-renders stay
        # consistent across selection changes.
        self._fallback_color = _FALLBACK_COLORS[
            spec.overworld_sprite_id % len(_FALLBACK_COLORS)
        ]
        # Cache the bounding rect — recomputed only on spec change.
        self._bounds = self._compute_bounds()
        # Track last *valid* drop position so we can revert if the user
        # dragged off the map (negative coords are rejected on commit).
        self._press_pos: Optional[QPointF] = None

    # ---- spec mutation --------------------------------------------------

    def replace_spec(self, spec: EventMarkerSpec) -> None:
        """Swap in a new spec — used after an undo/redo to keep the
        marker visually in sync with the model."""
        self.prepareGeometryChange()
        self._spec = spec
        self._bounds = self._compute_bounds()
        self.setToolTip(spec.label)
        self.setPos(QPointF(spec.x, spec.y))
        self.update()

    def spec(self) -> EventMarkerSpec:
        return self._spec

    # ---- QGraphicsItem hooks --------------------------------------------

    def boundingRect(self) -> QRectF:
        return self._bounds

    def paint(self, painter: QPainter, _option, _widget=None) -> None:
        painter.setRenderHint(QPainter.Antialiasing, True)
        spec = self._spec
        selected = self.isSelected()
        sel_color = QColor(80, 180, 255)
        if spec.pixmap is not None and not spec.pixmap.isNull():
            pw = spec.pixmap.width()
            ph = spec.pixmap.height()
            # Anchor sprite bottom-center on the placement point — the
            # vanilla overworld puts the digimon's feet at (x, y).
            painter.drawPixmap(QPointF(-pw / 2, -ph + 2), spec.pixmap)
            outline = QPen(sel_color, 2) if selected else QPen(QColor(0, 0, 0, 220), 1)
            painter.setPen(outline)
            painter.setBrush(Qt.NoBrush)
            painter.drawRect(QRectF(-pw / 2, -ph + 2, pw, ph))
        else:
            # Disc fallback — filled with the id-derived color, dark
            # outline so it reads on any backdrop.
            outline = QPen(sel_color, 2) if selected else QPen(QColor(0, 0, 0, 255), 1.5)
            painter.setPen(outline)
            painter.setBrush(QBrush(self._fallback_color))
            painter.drawEllipse(
                QPointF(0, 0), float(_DISC_RADIUS), float(_DISC_RADIUS),
            )
        # Label sits above the sprite/disc with a translucent backdrop
        # for legibility against busy tilemaps. Chain-injected markers
        # override the default hex MCHR id with their source script
        # offset so the user can trace each marker back to its 0x0150
        # block — the default form is the Events-tab MCHR id.
        label = spec.display_label or f"0x{spec.overworld_sprite_id:04x}"
        font = QFont()
        font.setPointSize(7)
        painter.setFont(font)
        metrics = painter.fontMetrics()
        text_w = metrics.horizontalAdvance(label)
        text_h = metrics.height()
        top = -text_h - (_DISC_RADIUS + 2 if spec.pixmap is None else
                         spec.pixmap.height() + 4 if spec.pixmap is not None else 0)
        bg = QRectF(-text_w / 2 - 2, top, text_w + 4, text_h)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor(0, 0, 0, 180)))
        painter.drawRoundedRect(bg, 2, 2)
        painter.setPen(QPen(QColor(255, 255, 255), 1))
        painter.drawText(bg, Qt.AlignCenter, label)

    def hoverEnterEvent(self, event) -> None:  # noqa: D401
        self.setZValue(_Z_MARKER_HOT)
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event) -> None:  # noqa: D401
        self.setZValue(_Z_MARKER)
        super().hoverLeaveEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: D401
        self._press_pos = self.pos()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: D401
        super().mouseReleaseEvent(event)
        if self._moved_cb is None or self._press_pos is None:
            return
        end = self.pos()
        new_x = int(round(end.x()))
        new_y = int(round(end.y()))
        # Snap-to-positive-integer + clamp to a u16 range — the script
        # payload encodes x/y as u16, anything outside that wraps.
        if new_x < 0 or new_y < 0 or new_x > 0xFFFF or new_y > 0xFFFF:
            self.setPos(self._press_pos)
            self._press_pos = None
            return
        if (new_x, new_y) == (self._spec.x, self._spec.y):
            # Pure click / no real drag — don't push an undo entry.
            self._press_pos = None
            return
        cb = self._moved_cb
        self._press_pos = None
        cb(self._spec.block_offset, new_x, new_y)

    def contextMenuEvent(self, event) -> None:  # noqa: D401
        """Forward right-click to the parent via ``context_menu_cb``.
        Falls through to the default if no callback is wired so the
        scene's own context handling (if any) still runs.
        """
        if self._context_menu_cb is None:
            super().contextMenuEvent(event)
            return
        global_pos = event.screenPos()
        self._context_menu_cb(self._spec.block_offset, global_pos)
        event.accept()

    # ---- internal -------------------------------------------------------

    def _compute_bounds(self) -> QRectF:
        spec = self._spec
        # The label backdrop is centered horizontally at item origin and
        # sits above the sprite — at 7pt a 4-digit hex id ("0xFFFF") is
        # ~44 px wide and ~16 px tall, while the cutscene-tab override
        # form ``entry 0499 @0x21a4`` is ~120 px. The boundingRect MUST
        # enclose every pixel paint() touches, otherwise Qt under-
        # invalidates when the item moves and the label leaves trails
        # behind it — so we widen the cap based on the active label.
        active_label = spec.display_label or f"0x{spec.overworld_sprite_id:04x}"
        # ~5 px per char at 7pt + 4 px padding on each side.
        label_half_w = max(24, len(active_label) * 5 // 2 + 4)
        label_h = 18
        if spec.pixmap is not None and not spec.pixmap.isNull():
            pw = spec.pixmap.width()
            ph = spec.pixmap.height()
            half_w = max(pw / 2, label_half_w) + 4
            return QRectF(
                -half_w, -ph - label_h - 8,
                2 * half_w, ph + label_h + 14,
            )
        half_w = max(_DISC_RADIUS, label_half_w) + 4
        return QRectF(
            -half_w, -_DISC_RADIUS - label_h - 8,
            2 * half_w, 2 * _DISC_RADIUS + label_h + 14,
        )


class _ExitZoneItem(QGraphicsItem):
    """One exit-zone (rectangle) or spawn-point (diamond) marker.

    Selectable; draggable when a ``moved_cb`` is wired (translation
    only — corner resize stays in the sidebar form so click-and-drag
    keeps the box shape stable). Drops snap to the tile grid so the
    final position lines up cleanly with the composite background.
    The label corner shows ``E<display_idx>`` for exits and
    ``S<display_idx>`` for spawns.
    """

    def __init__(
        self,
        spec: ExitZoneSpec,
        moved_cb: Optional["ExitMovedCallback"] = None,
    ) -> None:
        super().__init__()
        self._spec = spec
        self._moved_cb = moved_cb
        self.setFlag(QGraphicsItem.ItemIsMovable, self._moved_cb is not None)
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)
        self.setFlag(
            QGraphicsItem.ItemSendsGeometryChanges,
            self._moved_cb is not None,
        )
        self.setAcceptHoverEvents(True)
        self.setZValue(_Z_EXIT)
        # Item-local (0, 0) is the (x1, y1) corner — paint() draws the
        # box / diamond relative to that origin.
        self.setPos(QPointF(
            spec.x1 * _TILE_PIXEL_SCALE, spec.y1 * _TILE_PIXEL_SCALE,
        ))
        self._update_tooltip()
        self._bounds = self._compute_bounds()
        # Track the press-time scene position so we can revert on
        # rejected drops (off-map / would wrap a u16 tile coord).
        self._press_pos: Optional[QPointF] = None

    # ---- spec mutation --------------------------------------------------

    def replace_spec(self, spec: ExitZoneSpec) -> None:
        self.prepareGeometryChange()
        self._spec = spec
        self._bounds = self._compute_bounds()
        self._update_tooltip()
        self.setPos(QPointF(
            spec.x1 * _TILE_PIXEL_SCALE, spec.y1 * _TILE_PIXEL_SCALE,
        ))
        self.update()

    def spec(self) -> ExitZoneSpec:
        return self._spec

    # ---- QGraphicsItem hooks --------------------------------------------

    def boundingRect(self) -> QRectF:
        return self._bounds

    def paint(self, painter: QPainter, _option, _widget=None) -> None:
        painter.setRenderHint(QPainter.Antialiasing, True)
        spec = self._spec
        selected = self.isSelected()
        if spec.is_spawn:
            # Small diamond centered at item origin (the spawn point).
            outline = _SELECT_OUTLINE if selected else _SPAWN_OUTLINE
            painter.setPen(QPen(outline, 2))
            painter.setBrush(QBrush(_SPAWN_FILL))
            half = 6
            painter.drawPolygon([
                QPointF(0, -half), QPointF(half, 0),
                QPointF(0, half), QPointF(-half, 0),
            ])
            label = f"S{spec.display_idx}"
            label_origin = QPointF(half + 2, -half - 2)
        else:
            w = max((spec.x2 - spec.x1) * _TILE_PIXEL_SCALE, 8)
            h = max((spec.y2 - spec.y1) * _TILE_PIXEL_SCALE, 8)
            if spec.is_hitbox:
                base_outline = _HITBOX_OUTLINE
                fill = _HITBOX_FILL
                label = f"H{spec.display_idx}"
            else:
                base_outline = _EXIT_OUTLINE
                fill = _EXIT_FILL
                label = f"E{spec.display_idx}"
            outline = _SELECT_OUTLINE if selected else base_outline
            painter.setPen(QPen(outline, 2))
            painter.setBrush(QBrush(fill))
            painter.drawRect(QRectF(0, 0, w, h))
            label_origin = QPointF(2, 2)
        # Idx label with translucent backdrop so it reads on any
        # tilemap. Drawn last so the box outline doesn't cut into it.
        font = QFont()
        font.setPointSize(7)
        painter.setFont(font)
        metrics = painter.fontMetrics()
        text_w = metrics.horizontalAdvance(label)
        text_h = metrics.height()
        bg = QRectF(
            label_origin.x(), label_origin.y(), text_w + 6, text_h,
        )
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor(0, 0, 0, 180)))
        painter.drawRoundedRect(bg, 2, 2)
        painter.setPen(QPen(QColor(255, 255, 255), 1))
        painter.drawText(bg, Qt.AlignCenter, label)

    def hoverEnterEvent(self, event) -> None:  # noqa: D401
        self.setZValue(_Z_EXIT_HOT)
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event) -> None:  # noqa: D401
        self.setZValue(_Z_EXIT)
        super().hoverLeaveEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: D401
        self._press_pos = self.pos()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: D401
        super().mouseReleaseEvent(event)
        if self._moved_cb is None or self._press_pos is None:
            return
        end = self.pos()
        # Snap the drop to the nearest tile so the final box lines up
        # cleanly with the iso-rectilinear composite background.
        new_x1 = int(round(end.x() / _TILE_PIXEL_SCALE))
        new_y1 = int(round(end.y() / _TILE_PIXEL_SCALE))
        spec = self._spec
        dx = new_x1 - spec.x1
        dy = new_y1 - spec.y1
        # Pure click / no real movement — don't push an undo entry.
        if (dx, dy) == (0, 0):
            # Re-snap visually in case the press jiggled by sub-tile px.
            self.setPos(QPointF(
                spec.x1 * _TILE_PIXEL_SCALE, spec.y1 * _TILE_PIXEL_SCALE,
            ))
            self._press_pos = None
            return
        new_x2 = spec.x2 + dx
        new_y2 = spec.y2 + dy
        # Tile coords encode as u16 in the script payload; reject any
        # drop that would wrap. Also reject negative coords.
        if (
            new_x1 < 0 or new_y1 < 0
            or new_x2 < 0 or new_y2 < 0
            or new_x1 > 0xFFFF or new_y1 > 0xFFFF
            or new_x2 > 0xFFFF or new_y2 > 0xFFFF
        ):
            self.setPos(self._press_pos)
            self._press_pos = None
            return
        cb = self._moved_cb
        self._press_pos = None
        cb(spec.block_offset, new_x1, new_y1, new_x2, new_y2)

    # ---- internal -------------------------------------------------------

    def _update_tooltip(self) -> None:
        spec = self._spec
        if spec.is_spawn:
            self.setToolTip(
                f"Spawn {spec.display_idx}\ntile ({spec.x1}, {spec.y1})"
            )
        elif spec.is_hitbox:
            tip = (
                f"Hitbox {spec.display_idx}\n"
                f"tile ({spec.x1}, {spec.y1}) - ({spec.x2}, {spec.y2})"
            )
            if spec.dest_label:
                tip += f"\nhandler: {spec.dest_label}"
            self.setToolTip(tip)
        else:
            tip = (
                f"Exit {spec.display_idx}\n"
                f"tile ({spec.x1}, {spec.y1}) - ({spec.x2}, {spec.y2})"
            )
            if spec.dest_label:
                tip += f"\nto: {spec.dest_label}"
            self.setToolTip(tip)

    def _compute_bounds(self) -> QRectF:
        spec = self._spec
        # Label backdrop is ~24x16 px in the corner; bounding rect must
        # enclose it (and the diamond / box outline at +/- 2 px for the
        # 2-pixel-wide selected pen) so Qt invalidates dirty regions
        # cleanly on selection/spec changes.
        label_w = 32
        label_h = 16
        if spec.is_spawn:
            diamond_half = 8
            return QRectF(
                -diamond_half - 2,
                -diamond_half - 4,
                diamond_half * 2 + label_w + 4,
                diamond_half * 2 + label_h + 6,
            )
        w = max((spec.x2 - spec.x1) * _TILE_PIXEL_SCALE, 8)
        h = max((spec.y2 - spec.y1) * _TILE_PIXEL_SCALE, 8)
        return QRectF(-2, -2, max(w, label_w) + 4, h + 4)


class EventsCanvas(QGraphicsView):
    """QGraphicsView holding a composite map background + draggable markers.

    The view doesn't keep a session reference — it operates entirely on
    bytes / pixmaps / specs supplied by the parent. That keeps the
    headless test path simple (instantiate, ``set_map``, inspect
    ``markers()``) and the import surface narrow.
    """

    # Emitted when scene selection changes. ``markerSelected`` payload
    # is the selected sprite marker's block_offset (or -1 when no
    # marker is selected); ``exitSelected`` is the analogous signal
    # for exit-zone / spawn-point items. Both fire on every selection
    # change so the parent sidebar can clear the form that no longer
    # applies. (Qt-friendly int payload — None doesn't survive cleanly
    # through PySide signals.)
    markerSelected = Signal(int)
    exitSelected = Signal(int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        scene = QGraphicsScene(self)
        self.setScene(scene)
        self.setRenderHint(QPainter.SmoothPixmapTransform, False)
        self.setRenderHint(QPainter.Antialiasing, True)
        self.setBackgroundBrush(QColor(20, 20, 20))
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        # ScrollHandDrag is reserved for empty-area pans only — markers
        # capture the press first (ItemIsMovable), so users can drag
        # markers and pan the view with the same primary button.
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self._pixmap_item: Optional[QGraphicsPixmapItem] = None
        self._markers: List[_EventMarkerItem] = []
        self._marker_by_offset: Dict[int, _EventMarkerItem] = {}
        self._exits: List[_ExitZoneItem] = []
        self._exit_by_offset: Dict[int, _ExitZoneItem] = {}
        # Suppress selection signals while we're programmatically
        # setting the selection (select_marker / select_exit) so the
        # parent's list↔canvas round-trip can't feedback-loop.
        self._suppress_selection_signal = False
        scene.selectionChanged.connect(self._on_scene_selection_changed)

    # ---- public API -----------------------------------------------------

    def clear(self) -> None:
        self.scene().clear()
        self._pixmap_item = None
        self._markers = []
        self._marker_by_offset = {}
        self._exits = []
        self._exit_by_offset = {}

    def set_map(
        self,
        background: Optional[QPixmap],
        specs: List[EventMarkerSpec],
        *,
        moved_cb: Optional[EventMovedCallback] = None,
        exit_specs: List[ExitZoneSpec] = (),
        exit_moved_cb: Optional[ExitMovedCallback] = None,
        marker_context_menu_cb: Optional[MarkerContextMenuCallback] = None,
    ) -> None:
        """Replace the canvas contents with ``background`` + ``specs``.

        ``background`` is the composite-render pixmap (transparent
        sections fall through to the dark backdrop). Pass ``None`` to
        leave the canvas empty (e.g. when the map ships no overlay5
        entry). ``moved_cb`` is forwarded to every marker; pass ``None``
        for a read-only canvas. ``exit_specs`` adds 0x001b exit /
        spawn overlays; safe to leave empty for maps without an
        editable overlay5 entry. ``exit_moved_cb`` is forwarded to every
        exit/spawn item; pass ``None`` to leave them static.
        """
        self.clear()
        if background is not None:
            self._pixmap_item = self.scene().addPixmap(background)
            self._pixmap_item.setZValue(_Z_BACKGROUND)
            self.setSceneRect(QRectF(0, 0, background.width(), background.height()))
        else:
            self.setSceneRect(QRectF())
        for spec in specs:
            item = _EventMarkerItem(
                spec, moved_cb=moved_cb,
                context_menu_cb=marker_context_menu_cb,
            )
            self.scene().addItem(item)
            self._markers.append(item)
            self._marker_by_offset[spec.block_offset] = item
        for ezs in exit_specs:
            exit_item = _ExitZoneItem(ezs, moved_cb=exit_moved_cb)
            self.scene().addItem(exit_item)
            self._exits.append(exit_item)
            self._exit_by_offset[ezs.block_offset] = exit_item

    def marker_spec(self, block_offset: int) -> Optional[EventMarkerSpec]:
        """Look up the live :class:`EventMarkerSpec` for ``block_offset``.

        Returns ``None`` if no marker with that offset is currently on
        the canvas — used by context-menu / sidebar paths that need the
        marker's freshest id + xy without round-tripping through the
        parent's caches.
        """
        item = self._marker_by_offset.get(block_offset)
        return item.spec() if item is not None else None

    def update_marker_position(
        self, block_offset: int, x: int, y: int,
    ) -> None:
        """Re-sync the on-screen position of one marker without recreating it.

        Used by the undo path: a model-side x/y change feeds back into
        the canvas via this method so the user sees the marker hop back
        without the canvas clearing and re-laying out all markers.
        """
        item = self._marker_by_offset.get(block_offset)
        if item is None:
            return
        new_spec = EventMarkerSpec(
            block_offset=item.spec().block_offset,
            overworld_sprite_id=item.spec().overworld_sprite_id,
            x=x,
            y=y,
            label=item.spec().label,
            pixmap=item.spec().pixmap,
            behavior=item.spec().behavior,
        )
        item.replace_spec(new_spec)
        self.viewport().update()

    def update_marker_sprite_id(
        self,
        block_offset: int,
        sprite_id: int,
        label: str,
        pixmap: Optional[QPixmap],
    ) -> None:
        """Re-sync a marker's sprite id + visuals after an id edit.

        Caller (map_browser) resolves the new label + pixmap; the canvas
        just swaps the spec so paint() picks up the new sprite. Forces
        a viewport refresh so the new pixmap shows immediately —
        prepareGeometryChange + update() inside replace_spec sometimes
        gets coalesced when the marker's bounds don't change.
        """
        item = self._marker_by_offset.get(block_offset)
        if item is None:
            return
        old = item.spec()
        new_spec = EventMarkerSpec(
            block_offset=old.block_offset,
            overworld_sprite_id=int(sprite_id),
            x=old.x,
            y=old.y,
            label=label,
            pixmap=pixmap,
            behavior=old.behavior,
        )
        item.replace_spec(new_spec)
        self.viewport().update()

    def update_marker_behavior(
        self,
        block_offset: int,
        behavior: int,
        pixmap: Optional[QPixmap],
    ) -> None:
        """Swap a marker's behavior u16 + cached frame pixmap.

        Mirrors :meth:`update_marker_sprite_id` for the frame-edit path —
        sprite id stays the same, only the rendered pose changes.
        """
        item = self._marker_by_offset.get(block_offset)
        if item is None:
            return
        old = item.spec()
        new_spec = EventMarkerSpec(
            block_offset=old.block_offset,
            overworld_sprite_id=old.overworld_sprite_id,
            x=old.x,
            y=old.y,
            label=old.label,
            pixmap=pixmap,
            behavior=int(behavior),
        )
        item.replace_spec(new_spec)
        self.viewport().update()

    def update_exit_box(
        self, block_offset: int,
        x1: int, y1: int, x2: int, y2: int,
    ) -> None:
        """Re-sync the on-canvas rectangle of one exit / spawn item.

        Used by the undo path: a model-side box edit feeds back into
        the canvas via this method so the user sees the rectangle
        snap to its new corners without rebuilding the layer.
        """
        item = self._exit_by_offset.get(block_offset)
        if item is None:
            return
        old = item.spec()
        new_spec = ExitZoneSpec(
            block_offset=old.block_offset,
            idx=old.idx,
            x1=x1, y1=y1, x2=x2, y2=y2,
            is_spawn=(x1 == x2 and y1 == y2 and old.is_spawn),
            dest_label=old.dest_label,
            display_idx=old.display_idx,
            is_hitbox=old.is_hitbox,
        )
        item.replace_spec(new_spec)
        self.viewport().update()

    def update_exit_dest_label(
        self, block_offset: int, dest_label: str,
    ) -> None:
        """Refresh the tooltip after a destination edit.

        The canvas doesn't paint the destination, but the tooltip
        helps the user spot "this stair now goes to a different map"
        without opening the sidebar.
        """
        item = self._exit_by_offset.get(block_offset)
        if item is None:
            return
        old = item.spec()
        new_spec = ExitZoneSpec(
            block_offset=old.block_offset,
            idx=old.idx,
            x1=old.x1, y1=old.y1, x2=old.x2, y2=old.y2,
            is_spawn=old.is_spawn,
            dest_label=dest_label,
            display_idx=old.display_idx,
            is_hitbox=old.is_hitbox,
        )
        item.replace_spec(new_spec)

    def select_marker(self, block_offset: int) -> None:
        """Programmatically pick one marker (sidebar-list-driven flow).

        Suppresses :pyattr:`markerSelected` for the duration so the
        parent's list→canvas → canvas→list round-trip can't loop.
        """
        item = self._marker_by_offset.get(block_offset)
        if item is None:
            return
        self._suppress_selection_signal = True
        try:
            self.scene().clearSelection()
            item.setSelected(True)
            self.ensureVisible(item, 50, 50)
        finally:
            self._suppress_selection_signal = False

    def select_exit(self, block_offset: int) -> None:
        """Programmatically pick one exit / spawn item.

        Same loop-suppression pattern as :meth:`select_marker`.
        """
        item = self._exit_by_offset.get(block_offset)
        if item is None:
            return
        self._suppress_selection_signal = True
        try:
            self.scene().clearSelection()
            item.setSelected(True)
            self.ensureVisible(item, 50, 50)
        finally:
            self._suppress_selection_signal = False

    def markers(self) -> List[_EventMarkerItem]:
        return list(self._markers)

    def exit_items(self) -> List[_ExitZoneItem]:
        return list(self._exits)

    # ---- internal -------------------------------------------------------

    def _on_scene_selection_changed(self) -> None:
        if self._suppress_selection_signal:
            return
        selected = self.scene().selectedItems()
        sel_markers = [it for it in selected if isinstance(it, _EventMarkerItem)]
        sel_exits = [it for it in selected if isinstance(it, _ExitZoneItem)]
        # Always fire both signals so the sidebar clears the form that
        # no longer applies (e.g. picking an exit after a sprite was
        # selected needs to wipe the sprite form too).
        if sel_markers:
            self.markerSelected.emit(sel_markers[0].spec().block_offset)
        else:
            self.markerSelected.emit(-1)
        if sel_exits:
            self.exitSelected.emit(sel_exits[0].spec().block_offset)
        else:
            self.exitSelected.emit(-1)

    # ---- view interaction ----------------------------------------------

    def wheelEvent(self, event) -> None:  # noqa: D401
        # Ctrl+wheel zooms (matches the rest of the editor); plain
        # wheel scrolls.
        if event.modifiers() & Qt.ControlModifier:
            factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
            self.scale(factor, factor)
            event.accept()
            return
        super().wheelEvent(event)
