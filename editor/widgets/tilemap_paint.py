"""Reusable Porymap-style tilemap (NSCR) painter.

Shared by the UI-background (:mod:`bg_browser`) and battle-background
(:mod:`btmap_browser`) editors — both edit the same Nitro trio (an NCGR tile
bank, an NCLR palette, and an NSCR tilemap), so the paint tools are identical
and only the *data source* + the *undo command* differ per host.

Tools: **Paint** (stamp the selected tile / N×N brush), **Select** (marquee a
block and drag to move — Ctrl-drag copies; vacated cells fill with the
background tile), **Eyedropper** (grab a cell's tile / bank / flips — with an
N×N brush it also captures the block), plus a definable **background/fill
tile**. The tile picker groups tiles into per-bank sections; by default it
shows only the (tile, bank) pairs the layer actually uses, with **Show all
tiles** / **Filter by palette bank** to widen it. Edits the NSCR only
(rearranging existing tiles) — lossless. One undo step per stroke / move /
fill.

The host supplies a ``provider`` with:

- ``paint_context() -> Optional[PaintContext]`` — the current NCGR/NSCR/NCLR
  bytes + the NSCR's FAT path + a cache key + a name for undo labels, or
  ``None`` when nothing is paintable.
- ``make_nscr_command(nscr_path, new_nscr, label, on_change) -> QUndoCommand``.
- ``on_external_change()`` — called after an NSCR redo/undo flip so the host
  can re-render its own previews.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from PySide6.QtCore import Qt, QRect, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from digimon_core import btmap
from digimon_core import sprite as sprite_mod

from .paint_canvas import PaintCanvas

_ZOOM_MIN = 1
_ZOOM_MAX = 8
_PICKER_PER_ROW = 16
_PICKER_SCALE = 3
_BRUSH_SIZES = (1, 2, 3, 4)
_BRUSH_CELL_PX = 30
_PICKER_HEADER_PX = 18
_PICKER_SECTION_GAP_PX = 4
_SEL_COLOR = QColor(255, 230, 0)


@dataclass
class PaintContext:
    """Everything the painter needs about the current target."""
    ncgr: bytes
    nscr: bytes
    nclr: bytes
    nscr_path: str
    key: Any     # opaque cache key — reload only when it changes
    name: str    # shown in undo labels


class _PickerScrollArea(QScrollArea):
    """Scroll area that reports viewport resizes so the picker can reflow
    its cell columns to the available width."""

    viewportResized = Signal()

    def resizeEvent(self, ev):  # noqa: N802 — Qt override
        super().resizeEvent(ev)
        self.viewportResized.emit()


class _BrushPreview(QLabel):
    """Clickable N×N brush preview.

    Shows the rendered brush footprint; for N>1 it overlays grid lines and a
    highlight on the active cell and emits ``cellClicked(flat_index)`` when a
    sub-cell is clicked, so the brush can be composed tile-by-tile. For 1×1
    it's an inert preview.
    """

    cellClicked = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._n = 1
        self._sel = 0

    def set_grid(self, n: int, sel: int) -> None:
        self._n = max(1, int(n))
        self._sel = int(sel)
        self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.LeftButton or self._n <= 1:
            super().mousePressEvent(event)
            return
        w = max(1, self.width())
        h = max(1, self.height())
        col = min(self._n - 1, max(0, int(event.position().x()) * self._n // w))
        row = min(self._n - 1, max(0, int(event.position().y()) * self._n // h))
        self.cellClicked.emit(row * self._n + col)

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        if self._n <= 1:
            return
        painter = QPainter(self)
        w = self.width()
        h = self.height()
        cw = w / self._n
        ch = h / self._n
        painter.setPen(QPen(QColor(0, 0, 0, 130), 1))
        for k in range(1, self._n):
            painter.drawLine(round(k * cw), 0, round(k * cw), h)
            painter.drawLine(0, round(k * ch), w, round(k * ch))
        if 0 <= self._sel < self._n * self._n:
            sr, sc = divmod(self._sel, self._n)
            painter.setPen(QPen(QColor(255, 230, 0, 240), 2))
            painter.drawRect(round(sc * cw), round(sr * ch), round(cw), round(ch))
        painter.end()


class TilemapPaintTab(QWidget):
    def __init__(self, undo_stack, provider, parent=None, header_widget=None):
        super().__init__(parent)
        self._undo_stack = undo_stack
        self._provider = provider
        # Optional host widget pinned to the top of the controls sidebar
        # (e.g. the battle-background Layer A/B selector).
        self._header_widget = header_widget

        # ---- Paint state (rebuilt when the provider key changes) ----------
        self._paint_key: Any = None
        self._paint_entries: List[int] = []
        self._paint_tiles: List[bytes] = []
        self._paint_palettes: List[List[Tuple[int, int, int]]] = []
        self._paint_w = 0
        self._paint_h = 0
        self._paint_base: bytearray = bytearray()
        self._paint_scale = 2
        self._paint_sel_tile = 0
        self._paint_bank = 0
        self._paint_hflip = False
        self._paint_vflip = False
        self._paint_pick_mode = False
        self._paint_hover: Optional[Tuple[int, int]] = None
        self._paint_stroke_snapshot: Optional[List[int]] = None
        self._last_cell: Optional[Tuple[int, int]] = None
        self._suppress_reload = False

        # Brush footprint. ``_brush_entries`` is None in 1×1 mode (the
        # footprint is just the toolbar tile); for N>1 it's a row-major N²
        # pattern so a map color-pick / per-cell compose can replay exactly.
        self._brush_size = 1
        self._brush_entries: Optional[List[int]] = None
        self._brush_sel = 0

        # Picker filter mode + cached layout.
        self._show_all_tiles = False
        self._filter_by_bank = False
        self._picker_scale = _PICKER_SCALE
        self._picker_base_pixmap: Optional[QPixmap] = None
        self._picker_cells: List[Tuple[int, int, int, int, int, int]] = []

        # Tool + background-tile + selection state.
        self._tool = "paint"                       # "paint" | "select"
        self._bg_entry = 0
        self._sel_rect: Optional[Tuple[int, int, int, int]] = None
        self._sel_dragging = False
        self._sel_kind: Optional[str] = None        # "define" | "move"
        self._sel_anchor: Tuple[int, int] = (0, 0)
        self._sel_move_start: Tuple[int, int] = (0, 0)
        self._sel_move_delta: Tuple[int, int] = (0, 0)
        self._sel_float: Optional[List[int]] = None
        self._sel_copy = False

        self._build_ui()

    # ---- public API ------------------------------------------------------

    def refresh(self) -> None:
        """Reload from the provider (call on selection / source / layer change
        or when the tab becomes visible)."""
        self._ensure_paint_state()

    def invalidate(self) -> None:
        """Force the next :meth:`refresh` to rebuild even if the key matches."""
        self._paint_key = None

    # ---- UI construction -------------------------------------------------

    @staticmethod
    def _hline() -> QWidget:
        line = QWidget()
        line.setFixedHeight(1)
        line.setStyleSheet("background: #444;")
        return line

    def _build_ui(self) -> None:
        self._paint_canvas = PaintCanvas()
        self._paint_canvas.setText("Select a background.")
        self._paint_canvas.setAlignment(Qt.AlignCenter)
        self._paint_canvas.setMinimumSize(160, 120)
        self._paint_canvas.setHoverEnabled(True)
        self._paint_canvas.painted.connect(
            lambda x, y, _b, m: self._on_canvas_painted(x, y, m)
        )
        self._paint_canvas.paintFinished.connect(self._on_canvas_paint_finished)
        self._paint_canvas.hovered.connect(self._on_canvas_hovered)
        self._paint_canvas.hoverLeft.connect(self._on_canvas_hover_left)
        self._paint_canvas.zoomStepRequested.connect(self._on_canvas_zoom)
        self._paint_canvas.panRequested.connect(self._on_canvas_pan)
        self._paint_canvas_scroll = QScrollArea()
        self._paint_canvas_scroll.setWidget(self._paint_canvas)
        self._paint_canvas_scroll.setWidgetResizable(False)
        self._paint_canvas_scroll.setAlignment(Qt.AlignCenter)

        # Selected-tile / brush preview + flips.
        self._sel_preview = _BrushPreview()
        self._sel_preview.setFixedSize(48, 48)
        self._sel_preview.setAlignment(Qt.AlignCenter)
        self._sel_preview.setStyleSheet("background: #1d1d1d; border: 1px solid #555;")
        self._sel_preview.cellClicked.connect(self._on_brush_cell_clicked)
        self._sel_label = QLabel("Tile —")
        self._sel_label.setWordWrap(True)
        self._hflip_chk = QCheckBox("H-flip")
        self._hflip_chk.toggled.connect(lambda v: self._on_flip("h", v))
        self._vflip_chk = QCheckBox("V-flip")
        self._vflip_chk.toggled.connect(lambda v: self._on_flip("v", v))
        flip_row = QHBoxLayout()
        flip_row.setContentsMargins(0, 0, 0, 0)
        flip_row.addWidget(self._hflip_chk)
        flip_row.addWidget(self._vflip_chk)
        flip_row.addStretch(1)
        sel_row = QHBoxLayout()
        sel_row.setContentsMargins(0, 0, 0, 0)
        sel_row.addWidget(self._sel_preview, 0, Qt.AlignTop)
        sel_col = QVBoxLayout()
        sel_col.setContentsMargins(0, 0, 0, 0)
        sel_col.addWidget(self._sel_label)
        sel_col.addLayout(flip_row)
        sel_row.addLayout(sel_col, 1)

        # Brush size.
        self._brush_combo = QComboBox()
        for n in _BRUSH_SIZES:
            self._brush_combo.addItem(f"{n}×{n}  ({n * n} tiles)", n)
        self._brush_combo.setToolTip(
            "Tiles stamped (and color-picked) at once, anchored top-left at "
            "the clicked cell. Pick from the map with a large brush to copy a "
            "block; pick a single tile from the picker to fill the block solid."
        )
        self._brush_combo.currentIndexChanged.connect(
            lambda _ix: self._on_brush_size_changed(self._brush_combo.currentData())
        )
        brush_row = QHBoxLayout()
        brush_row.setContentsMargins(0, 0, 0, 0)
        brush_row.addWidget(QLabel("Brush"))
        brush_row.addWidget(self._brush_combo)
        brush_row.addStretch(1)

        # Palette-bank filter. Default shows only used (tile, bank) pairs
        # grouped by bank; Show all exposes the full tile set per bank, and
        # Filter by palette bank narrows that to one bank via the spinner.
        self._show_all_chk = QCheckBox("Show all tiles")
        self._show_all_chk.setToolTip(
            "Default: only tiles used in this layer, grouped by palette bank.\n"
            "Checked: every tile in the tileset, listed per palette bank."
        )
        self._show_all_chk.toggled.connect(self._on_show_all_toggled)
        self._filter_bank_chk = QCheckBox("Filter by palette bank")
        self._filter_bank_chk.setEnabled(False)
        self._filter_bank_chk.setToolTip(
            "Narrow the 'Show all tiles' view to a single palette bank.")
        self._filter_bank_chk.toggled.connect(self._on_filter_bank_toggled)
        self._bank_spin = QSpinBox()
        self._bank_spin.setMinimum(0)
        self._bank_spin.setEnabled(False)
        self._bank_spin.setToolTip(
            "Palette bank stamped onto painted tiles. Editable only with "
            "'Show all tiles' + 'Filter by palette bank'; otherwise picking a "
            "tile selects its bank for you.")
        self._bank_spin.valueChanged.connect(self._on_bank_changed)
        bank_row = QHBoxLayout()
        bank_row.setContentsMargins(0, 0, 0, 0)
        bank_row.addWidget(self._filter_bank_chk)
        bank_row.addWidget(self._bank_spin)
        bank_row.addStretch(1)

        # Tool toggles: Select + Eyedropper (Paint is the default when both off).
        self._select_btn = QToolButton()
        self._select_btn.setText("Select")
        self._select_btn.setCheckable(True)
        self._select_btn.setToolTip(
            "Marquee-select a block of tiles, then drag it to move (Ctrl-drag"
            " copies). Vacated cells fill with the background tile. Off = Paint."
        )
        self._select_btn.toggled.connect(self._on_select_toggled)
        self._pick_btn = QToolButton()
        self._pick_btn.setText("Eyedropper")
        self._pick_btn.setCheckable(True)
        self._pick_btn.setToolTip(
            "Click a cell to copy its tile / bank / flips into the picker"
            " (one-shot). With an N×N brush it captures the whole block."
        )
        self._pick_btn.toggled.connect(self._on_pick_toggled)
        tool_row = QHBoxLayout()
        tool_row.setContentsMargins(0, 0, 0, 0)
        tool_row.addWidget(self._select_btn)
        tool_row.addWidget(self._pick_btn)
        tool_row.addStretch(1)

        # Background/fill tile — written into cleared / vacated cells.
        self._bg_preview = QLabel()
        self._bg_preview.setFixedSize(32, 32)
        self._bg_preview.setAlignment(Qt.AlignCenter)
        self._bg_preview.setStyleSheet("background: #1d1d1d; border: 1px solid #555;")
        self._bg_label = QLabel("Background: —")
        self._bg_label.setWordWrap(True)
        set_bg_btn = QToolButton()
        set_bg_btn.setText("Set = selected")
        set_bg_btn.setToolTip("Make the currently-selected tile the background/fill tile.")
        set_bg_btn.clicked.connect(self._on_set_bg_clicked)
        self._fill_sel_btn = QToolButton()
        self._fill_sel_btn.setText("Fill → bg")
        self._fill_sel_btn.setToolTip("Fill the current selection with the background tile.")
        self._fill_sel_btn.clicked.connect(self._on_fill_selection_clicked)
        bg_head = QHBoxLayout()
        bg_head.setContentsMargins(0, 0, 0, 0)
        bg_head.addWidget(self._bg_preview, 0, Qt.AlignTop)
        bg_col = QVBoxLayout()
        bg_col.setContentsMargins(0, 0, 0, 0)
        bg_col.addWidget(self._bg_label)
        bg_btn_row = QHBoxLayout()
        bg_btn_row.setContentsMargins(0, 0, 0, 0)
        bg_btn_row.addWidget(set_bg_btn)
        bg_btn_row.addWidget(self._fill_sel_btn)
        bg_btn_row.addStretch(1)
        bg_col.addLayout(bg_btn_row)
        bg_head.addLayout(bg_col, 1)

        self._zoom_label = QLabel("1× (Ctrl+wheel)")
        self._zoom_label.setStyleSheet("color: #888;")

        self._picker_canvas = PaintCanvas()
        self._picker_canvas.setText("(no background loaded)")
        self._picker_canvas.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self._picker_canvas.painted.connect(
            lambda x, y, _b, _m: self._on_picker_painted(x, y)
        )
        self._picker_canvas.zoomStepRequested.connect(self._on_picker_zoom)
        self._picker_scroll = _PickerScrollArea()
        self._picker_scroll.setWidget(self._picker_canvas)
        self._picker_scroll.setWidgetResizable(False)
        self._picker_scroll.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self._picker_scroll.viewportResized.connect(self._on_picker_viewport_resized)

        right_col = QWidget()
        right_col.setMinimumWidth(220)
        rc = QVBoxLayout(right_col)
        rc.setContentsMargins(0, 0, 0, 0)
        if self._header_widget is not None:
            rc.addWidget(self._header_widget)
            rc.addWidget(self._hline())
        rc.addLayout(sel_row)
        rc.addLayout(brush_row)
        rc.addWidget(self._show_all_chk)
        rc.addLayout(bank_row)
        rc.addLayout(tool_row)
        rc.addWidget(self._hline())
        rc.addLayout(bg_head)
        rc.addWidget(self._hline())
        rc.addWidget(self._zoom_label)
        rc.addWidget(QLabel("Tiles:"))
        rc.addWidget(self._picker_scroll, 1)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._paint_canvas_scroll)
        splitter.addWidget(right_col)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(splitter)

    # ---- state build -----------------------------------------------------

    def _ensure_paint_state(self) -> None:
        ctx = self._provider.paint_context()
        if ctx is None:
            self._paint_canvas.setPixmap(QPixmap())
            self._paint_canvas.setText("(nothing to paint)")
            self._paint_key = None
            return
        if ctx.key == self._paint_key:
            return
        try:
            self._paint_w, self._paint_h, self._paint_entries = btmap.parse_nscr(ctx.nscr)
            self._paint_tiles, _bd = btmap._ncgr_tiles_as_indices(ctx.ncgr)
            self._paint_palettes, _pbd = sprite_mod.parse_nclr(ctx.nclr)
        except (ValueError, KeyError) as e:
            self._paint_canvas.setPixmap(QPixmap())
            self._paint_canvas.setText(f"Cannot edit: {e}")
            self._paint_key = None
            return
        self._paint_key = ctx.key
        self._bank_spin.blockSignals(True)
        self._bank_spin.setMaximum(max(0, len(self._paint_palettes) - 1))
        if self._paint_bank > self._bank_spin.maximum():
            self._paint_bank = 0
        self._bank_spin.setValue(self._paint_bank)
        self._bank_spin.blockSignals(False)
        self._bank_spin.setEnabled(self._show_all_tiles and self._filter_by_bank)
        if self._paint_sel_tile >= len(self._paint_tiles):
            self._paint_sel_tile = 0
        # Reset the captured brush pattern for the new tileset (keep the size).
        self._brush_sel = 0
        self._brush_entries = (
            [self._sel_entry()] * (self._brush_size ** 2)
            if self._brush_size > 1 else None
        )
        # Default the background/fill tile to the most common entry — almost
        # always the blank backdrop tile.
        self._bg_entry = Counter(self._paint_entries).most_common(1)[0][0] if self._paint_entries else 0
        self._clear_selection()
        self._paint_base = self._compose_full(self._paint_entries)
        self._render_canvas()
        self._render_picker()
        self._render_sel_preview()
        self._render_bg_preview()

    # ---- RGBA composition ------------------------------------------------

    def _blit_cell(self, buf: bytearray, cx: int, cy: int, entry: int) -> None:
        w = self._paint_w
        tiles = self._paint_tiles
        pals = self._paint_palettes
        tile_ix = entry & 0x3FF
        hflip = bool(entry & 0x400)
        vflip = bool(entry & 0x800)
        bank = (entry >> 12) & 0xF
        pal = pals[bank] if bank < len(pals) else (pals[0] if pals else [(0, 0, 0)])
        tile = tiles[tile_ix] if tile_ix < len(tiles) else None
        for py in range(8):
            srow = py if not vflip else 7 - py
            oy = cy * 8 + py
            for px in range(8):
                scol = px if not hflip else 7 - px
                if tile is not None:
                    idx = tile[srow * 8 + scol]
                    r, g, b = pal[idx] if idx < len(pal) else (0, 0, 0)
                else:
                    r = g = b = 0
                o = (oy * w + (cx * 8 + px)) * 4
                buf[o] = r
                buf[o + 1] = g
                buf[o + 2] = b
                buf[o + 3] = 255

    def _compose_full(self, entries: List[int]) -> bytearray:
        buf = bytearray(self._paint_w * self._paint_h * 4)
        tw = self._paint_w // 8
        for cell, entry in enumerate(entries):
            self._blit_cell(buf, cell % tw, cell // tw, entry)
        return buf

    def _tile_image(self, tile_ix: int, bank: int) -> QImage:
        buf = bytearray(8 * 8 * 4)
        entry = (tile_ix & 0x3FF) | ((bank & 0xF) << 12)
        saved_w = self._paint_w
        self._paint_w = 8
        try:
            self._blit_cell(buf, 0, 0, entry)
        finally:
            self._paint_w = saved_w
        return QImage(bytes(buf), 8, 8, 32, QImage.Format_RGBA8888).copy()

    def _render_canvas(self) -> None:
        if not self._paint_base:
            return
        w, h, scale = self._paint_w, self._paint_h, self._paint_scale
        base = self._paint_base
        moving = self._sel_dragging and self._sel_kind == "move" and self._sel_float is not None
        if moving:
            base = self._move_preview_buffer()
        image = QImage(bytes(base), w, h, w * 4, QImage.Format_RGBA8888)
        pm = QPixmap.fromImage(image)
        if scale != 1:
            pm = pm.scaled(w * scale, h * scale, Qt.KeepAspectRatio, Qt.FastTransformation)
        painter = QPainter(pm)
        if self._paint_hover is not None and not moving:
            hx, hy = self._paint_hover
            tw, th = w // 8, h // 8
            n = max(1, self._brush_size)
            bw = min(n, tw - hx)
            bh = min(n, th - hy)
            rect = QRect(hx * 8 * scale, hy * 8 * scale, bw * 8 * scale, bh * 8 * scale)
            painter.fillRect(rect, QColor(255, 230, 0, 60))
            painter.setPen(QPen(QColor(255, 230, 0, 220), 1))
            painter.drawRect(rect.adjusted(0, 0, -1, -1))
        if self._sel_rect is not None:
            if moving:
                dx, dy = self._sel_move_delta
                x0, y0, x1, y1 = self._sel_rect
                self._draw_marquee(painter, (x0, y0, x1, y1), scale, faint=True)
                self._draw_marquee(painter, (x0 + dx, y0 + dy, x1 + dx, y1 + dy), scale, faint=False)
            elif self._tool == "select":
                self._draw_marquee(painter, self._sel_rect, scale, faint=False)
        painter.end()
        self._paint_canvas.setText("")
        self._paint_canvas.setImageScale(scale)
        self._paint_canvas.setPixmap(pm)
        self._paint_canvas.adjustSize()
        self._zoom_label.setText(f"{scale}× (Ctrl+wheel)")

    def _draw_marquee(self, painter, rect, scale, *, faint: bool) -> None:
        x0, y0, x1, y1 = rect
        pen = QPen(QColor(255, 230, 0, 110 if faint else 255), 2)
        pen.setStyle(Qt.DashLine)
        painter.setPen(pen)
        painter.drawRect(
            x0 * 8 * scale, y0 * 8 * scale,
            (x1 - x0 + 1) * 8 * scale - 1, (y1 - y0 + 1) * 8 * scale - 1,
        )

    def _move_preview_buffer(self) -> bytearray:
        buf = bytearray(self._paint_base)
        tw, th = self._paint_w // 8, self._paint_h // 8
        x0, y0, x1, y1 = self._sel_rect
        dx, dy = self._sel_move_delta
        if not self._sel_copy:
            for yy in range(y0, y1 + 1):
                for xx in range(x0, x1 + 1):
                    self._blit_cell(buf, xx, yy, self._bg_entry)
        w_sel = x1 - x0 + 1
        for j in range(y1 - y0 + 1):
            for i in range(w_sel):
                nx, ny = x0 + i + dx, y0 + j + dy
                if 0 <= nx < tw and 0 <= ny < th:
                    self._blit_cell(buf, nx, ny, self._sel_float[j * w_sel + i])
        return buf

    def _entry_swatch(self, entry: int, size: int) -> QPixmap:
        buf = bytearray(8 * 8 * 4)
        saved_w = self._paint_w
        self._paint_w = 8
        try:
            self._blit_cell(buf, 0, 0, entry)
        finally:
            self._paint_w = saved_w
        image = QImage(bytes(buf), 8, 8, 32, QImage.Format_RGBA8888)
        return QPixmap.fromImage(image).scaled(size, size, Qt.KeepAspectRatio, Qt.FastTransformation)

    def _render_sel_preview(self) -> None:
        n = max(1, self._brush_size)
        side = 8 * n
        buf = bytearray(side * side * 4)
        saved_w = self._paint_w
        self._paint_w = side
        try:
            for j in range(n):
                for i in range(n):
                    self._blit_cell(buf, i, j, self._brush_cell(i, j))
        finally:
            self._paint_w = saved_w
        image = QImage(bytes(buf), side, side, side * 4, QImage.Format_RGBA8888)
        disp = 48 if n == 1 else _BRUSH_CELL_PX * n
        self._sel_preview.setFixedSize(disp, disp)
        self._sel_preview.setPixmap(
            QPixmap.fromImage(image).scaled(disp, disp, Qt.KeepAspectRatio, Qt.FastTransformation))
        self._sel_preview.set_grid(n, self._brush_sel)
        flags = []
        if self._paint_hflip:
            flags.append("H")
        if self._paint_vflip:
            flags.append("V")
        suffix = (" · " + "".join(flags) + "-flip") if flags else ""
        if n > 1:
            self._sel_label.setText(
                f"Brush {n}×{n} · cell {self._brush_sel} · "
                f"tile {self._paint_sel_tile} · bank {self._paint_bank}")
        else:
            self._sel_label.setText(
                f"Tile {self._paint_sel_tile} · bank {self._paint_bank}{suffix}")

    def _render_bg_preview(self) -> None:
        self._bg_preview.setPixmap(self._entry_swatch(self._bg_entry, 32))
        tile = self._bg_entry & 0x3FF
        bank = (self._bg_entry >> 12) & 0xF
        self._bg_label.setText(f"Background: tile {tile} · bank {bank}")

    # ---- Picker: sectioned by palette bank -------------------------------

    def _compute_picker_sections(self) -> List[Tuple[int, List[int]]]:
        """``[(bank, [tile_ix, ...]), ...]`` for the current filter mode.

        Default (``show_all_tiles=False``): only the (tile, bank) pairs the
        layer actually uses, grouped by bank (sorted by tile index within a
        bank). Show all: every tile per bank, or a single bank when filtered.
        """
        if not self._show_all_tiles:
            used: Dict[int, set] = {}
            for e in self._paint_entries:
                tile_ix = e & 0x3FF
                bank = (e >> 12) & 0xF
                if tile_ix >= len(self._paint_tiles) or bank >= len(self._paint_palettes):
                    continue
                used.setdefault(bank, set()).add(tile_ix)
            return [(bank, sorted(used[bank])) for bank in sorted(used)]
        n_tiles = len(self._paint_tiles)
        if self._filter_by_bank:
            bank = self._paint_bank if self._paint_bank < len(self._paint_palettes) else 0
            return [(bank, list(range(n_tiles)))]
        return [(bank, list(range(n_tiles))) for bank in range(len(self._paint_palettes))]

    def _render_picker(self) -> None:
        if not self._paint_tiles:
            self._picker_canvas.setText("(no tiles)")
            self._picker_cells = []
            return
        cell_px = 8 * self._picker_scale
        viewport_w = self._picker_scroll.viewport().width()
        if viewport_w < cell_px:
            viewport_w = _PICKER_PER_ROW * cell_px
        cols = max(1, viewport_w // cell_px)
        width_px = cols * cell_px

        sections = self._compute_picker_sections()
        if not sections:
            pixmap = QPixmap(width_px, _PICKER_HEADER_PX)
            pixmap.fill(QColor("#1a1a1a"))
            self._picker_base_pixmap = pixmap
            self._picker_cells = []
            self._picker_canvas.setText("")
            self._picker_canvas.setImageScale(1)
            self._picker_canvas.setPixmap(pixmap)
            self._picker_canvas.resize(width_px, _PICKER_HEADER_PX)
            return

        layouts: List[Tuple[int, List[int], int, int]] = []
        total_h = 0
        for bank_ix, tile_ixs in sections:
            n_rows = (len(tile_ixs) + cols - 1) // cols
            layouts.append((bank_ix, tile_ixs, total_h, total_h + _PICKER_HEADER_PX))
            total_h += _PICKER_HEADER_PX + n_rows * cell_px + _PICKER_SECTION_GAP_PX

        pixmap = QPixmap(width_px, max(total_h, 1))
        pixmap.fill(QColor("#1a1a1a"))
        painter = QPainter(pixmap)
        font = painter.font()
        font.setPointSize(8)
        painter.setFont(font)
        cells: List[Tuple[int, int, int, int, int, int]] = []
        for bank_ix, tile_ixs, header_y, grid_y in layouts:
            painter.fillRect(0, header_y, width_px, _PICKER_HEADER_PX, QColor("#2a2a2a"))
            painter.setPen(QColor("#e0e0e0"))
            painter.drawText(4, header_y + _PICKER_HEADER_PX - 5, f"Bank {bank_ix}")
            for i, tile_ix in enumerate(tile_ixs):
                cx = (i % cols) * cell_px
                cy = grid_y + (i // cols) * cell_px
                painter.drawImage(QRect(cx, cy, cell_px, cell_px),
                                  self._tile_image(tile_ix, bank_ix))
                cells.append((cx, cy, cell_px, cell_px, tile_ix, bank_ix))
        painter.end()
        self._picker_base_pixmap = pixmap
        self._picker_cells = cells
        self._picker_canvas.setText("")
        self._picker_canvas.setImageScale(1)
        self._picker_canvas.resize(width_px, pixmap.height())
        self._render_picker_overlay()

    def _render_picker_overlay(self) -> None:
        if self._picker_base_pixmap is None:
            return
        pixmap = QPixmap(self._picker_base_pixmap)
        target = None
        for (cx, cy, cw, ch, tile_ix, bank_ix) in self._picker_cells:
            if tile_ix == self._paint_sel_tile and bank_ix == self._paint_bank:
                target = (cx, cy, cw, ch)
                break
        if target is not None:
            cx, cy, cw, ch = target
            rect = QRect(cx, cy, cw, ch)
            painter = QPainter(pixmap)
            outer = QPen(QColor(255, 255, 255))
            outer.setWidth(2)
            painter.setPen(outer)
            painter.drawRect(rect.adjusted(0, 0, -1, -1))
            inner = QPen(QColor(0, 0, 0))
            inner.setWidth(1)
            painter.setPen(inner)
            painter.drawRect(rect.adjusted(1, 1, -2, -2))
            painter.end()
        self._picker_canvas.setPixmap(pixmap)

    def _on_picker_viewport_resized(self) -> None:
        if self._paint_tiles:
            self._render_picker()

    # ---- helpers ---------------------------------------------------------

    def _sel_entry(self) -> int:
        e = self._paint_sel_tile & 0x3FF
        if self._paint_hflip:
            e |= 0x400
        if self._paint_vflip:
            e |= 0x800
        e |= (self._paint_bank & 0xF) << 12
        return e

    def _brush_cell(self, i: int, j: int) -> int:
        if self._brush_entries is None:
            return self._sel_entry()
        idx = j * self._brush_size + i
        if 0 <= idx < len(self._brush_entries):
            return self._brush_entries[idx]
        return self._sel_entry()

    def _cell_index(self, x: int, y: int) -> Optional[int]:
        tw = self._paint_w // 8
        th = self._paint_h // 8
        cx, cy = x // 8, y // 8
        if 0 <= cx < tw and 0 <= cy < th:
            return cy * tw + cx
        return None

    def _cell_xy(self, x: int, y: int) -> Tuple[int, int]:
        tw = self._paint_w // 8
        th = self._paint_h // 8
        return (min(tw - 1, max(0, x // 8)), min(th - 1, max(0, y // 8)))

    @staticmethod
    def _in_rect(cx: int, cy: int, rect: Tuple[int, int, int, int]) -> bool:
        x0, y0, x1, y1 = rect
        return x0 <= cx <= x1 and y0 <= cy <= y1

    def _clear_selection(self) -> None:
        self._sel_rect = None
        self._sel_dragging = False
        self._sel_kind = None
        self._sel_float = None
        self._sel_move_delta = (0, 0)

    def _apply_entries(self, new_entries: List[int], label: str) -> bool:
        """Commit ``new_entries`` as an undoable NSCR edit. Builds the NSCR
        before mutating in-memory state so a build failure is a clean no-op."""
        ctx = self._provider.paint_context()
        if self._undo_stack is None or ctx is None:
            return False
        try:
            new_nscr = btmap.build_nscr_from_template(
                new_entries, self._paint_w, self._paint_h, ctx.nscr,
            )
        except (ValueError, KeyError) as e:
            QMessageBox.warning(self, "Edit failed", str(e))
            return False
        self._paint_entries = new_entries
        self._paint_base = self._compose_full(new_entries)
        self._suppress_reload = True
        try:
            self._undo_stack.push(self._provider.make_nscr_command(
                ctx.nscr_path, new_nscr, f"{label} — {ctx.name}",
                self._on_nscr_replaced,
            ))
        finally:
            self._suppress_reload = False
        return True

    # ---- canvas interaction ---------------------------------------------

    def _paint_cell(self, tx: int, ty: int, entry: int) -> None:
        tw = self._paint_w // 8
        th = self._paint_h // 8
        if not (0 <= tx < tw and 0 <= ty < th):
            return
        cell = ty * tw + tx
        if self._paint_entries[cell] == entry:
            return
        self._paint_entries[cell] = entry
        self._blit_cell(self._paint_base, tx, ty, entry)

    def _stamp_brush(self, tx: int, ty: int) -> None:
        n = max(1, self._brush_size)
        for j in range(n):
            for i in range(n):
                self._paint_cell(tx + i, ty + j, self._brush_cell(i, j))

    def _paint_line(self, p0: Tuple[int, int], p1: Tuple[int, int]) -> None:
        x0, y0 = p0
        x1, y1 = p1
        dx = abs(x1 - x0)
        dy = -abs(y1 - y0)
        sx = 1 if x0 < x1 else -1
        sy = 1 if y0 < y1 else -1
        err = dx + dy
        while True:
            self._stamp_brush(x0, y0)
            if x0 == x1 and y0 == y1:
                return
            e2 = 2 * err
            if e2 >= dy:
                err += dy
                x0 += sx
            if e2 <= dx:
                err += dx
                y0 += sy

    def _on_canvas_painted(self, x: int, y: int, mods=Qt.NoModifier) -> None:
        if not self._paint_entries:
            return
        if self._paint_pick_mode:
            idx = self._cell_index(x, y)
            if idx is not None:
                self._pick_cell(idx)
            return
        if self._tool == "select":
            self._on_select_drag(x, y, mods)
            return
        tx, ty = x // 8, y // 8
        tw = self._paint_w // 8
        th = self._paint_h // 8
        if not (0 <= tx < tw and 0 <= ty < th) or self._undo_stack is None:
            return
        if self._paint_stroke_snapshot is None:
            self._paint_stroke_snapshot = list(self._paint_entries)
            self._last_cell = None
        if self._last_cell is None:
            self._stamp_brush(tx, ty)
        else:
            self._paint_line(self._last_cell, (tx, ty))
        self._last_cell = (tx, ty)
        self._render_canvas()

    def _on_canvas_paint_finished(self) -> None:
        if self._tool == "select":
            if not self._sel_dragging:
                return
            self._sel_dragging = False
            kind, self._sel_kind = self._sel_kind, None
            if kind == "move" and self._sel_move_delta != (0, 0):
                self._commit_move()
            else:
                self._render_canvas()
            return
        snap = self._paint_stroke_snapshot
        self._paint_stroke_snapshot = None
        self._last_cell = None
        if snap is None or snap == self._paint_entries:
            return
        entries = self._paint_entries
        if not self._apply_entries(list(entries), "Paint background"):
            self._paint_entries = snap
            self._paint_base = self._compose_full(snap)
            self._render_canvas()

    def _on_select_drag(self, x: int, y: int, mods) -> None:
        cx, cy = self._cell_xy(x, y)
        if not self._sel_dragging:
            self._sel_dragging = True
            if self._sel_rect is not None and self._in_rect(cx, cy, self._sel_rect):
                self._sel_kind = "move"
                self._sel_move_start = (cx, cy)
                self._sel_move_delta = (0, 0)
                self._sel_float = self._capture_rect(self._sel_rect)
                self._sel_copy = bool(mods & Qt.ControlModifier)
            else:
                self._sel_kind = "define"
                self._sel_anchor = (cx, cy)
                self._sel_rect = (cx, cy, cx, cy)
                self._render_canvas()
            return
        if self._sel_kind == "define":
            ax, ay = self._sel_anchor
            self._sel_rect = (min(ax, cx), min(ay, cy), max(ax, cx), max(ay, cy))
            self._render_canvas()
        elif self._sel_kind == "move":
            sx, sy = self._sel_move_start
            delta = (cx - sx, cy - sy)
            if delta != self._sel_move_delta:
                self._sel_move_delta = delta
                self._render_canvas()

    def _capture_rect(self, rect: Tuple[int, int, int, int]) -> List[int]:
        tw = self._paint_w // 8
        x0, y0, x1, y1 = rect
        return [
            self._paint_entries[yy * tw + xx]
            for yy in range(y0, y1 + 1)
            for xx in range(x0, x1 + 1)
        ]

    def _commit_move(self) -> None:
        tw, th = self._paint_w // 8, self._paint_h // 8
        x0, y0, x1, y1 = self._sel_rect
        dx, dy = self._sel_move_delta
        w_sel = x1 - x0 + 1
        new_entries = list(self._paint_entries)
        if not self._sel_copy:
            for yy in range(y0, y1 + 1):
                for xx in range(x0, x1 + 1):
                    new_entries[yy * tw + xx] = self._bg_entry
        for j in range(y1 - y0 + 1):
            for i in range(w_sel):
                nx, ny = x0 + i + dx, y0 + j + dy
                if 0 <= nx < tw and 0 <= ny < th:
                    new_entries[ny * tw + nx] = self._sel_float[j * w_sel + i]
        label = "Copy tiles" if self._sel_copy else "Move tiles"
        if self._apply_entries(new_entries, label):
            self._sel_rect = (x0 + dx, y0 + dy, x1 + dx, y1 + dy)
        self._sel_float = None
        self._sel_move_delta = (0, 0)
        self._render_canvas()

    def _pick_cell(self, idx: int) -> None:
        tw = self._paint_w // 8
        th = self._paint_h // 8
        tx, ty = idx % tw, idx // tw
        n = max(1, self._brush_size)
        if n > 1:
            captured: List[int] = []
            for j in range(n):
                for i in range(n):
                    cx, cy = tx + i, ty + j
                    if 0 <= cx < tw and 0 <= cy < th:
                        captured.append(self._paint_entries[cy * tw + cx])
                    else:
                        captured.append(0)
            self._brush_entries = captured
        else:
            self._brush_entries = None
        self._brush_sel = 0
        entry = self._paint_entries[idx]
        self._paint_sel_tile = entry & 0x3FF
        self._paint_bank = (entry >> 12) & 0xF
        self._paint_hflip = bool(entry & 0x400)
        self._paint_vflip = bool(entry & 0x800)
        for chk, val in ((self._hflip_chk, self._paint_hflip), (self._vflip_chk, self._paint_vflip)):
            chk.blockSignals(True)
            chk.setChecked(val)
            chk.blockSignals(False)
        self._bank_spin.blockSignals(True)
        self._bank_spin.setValue(min(self._paint_bank, self._bank_spin.maximum()))
        self._bank_spin.blockSignals(False)
        self._render_picker()
        self._render_sel_preview()
        self._pick_btn.setChecked(False)

    def _on_canvas_hovered(self, x: int, y: int) -> None:
        idx = self._cell_index(x, y)
        cell = None if idx is None else (x // 8, y // 8)
        if cell != self._paint_hover:
            self._paint_hover = cell
            if not self._sel_dragging:
                self._render_canvas()

    def _on_canvas_hover_left(self) -> None:
        if self._paint_hover is not None:
            self._paint_hover = None
            if not self._sel_dragging:
                self._render_canvas()

    def _on_canvas_zoom(self, steps: int) -> None:
        new = max(_ZOOM_MIN, min(_ZOOM_MAX, self._paint_scale + steps))
        if new != self._paint_scale:
            self._paint_scale = new
            self._render_canvas()

    def _on_canvas_pan(self, dx: int, dy: int) -> None:
        h = self._paint_canvas_scroll.horizontalScrollBar()
        v = self._paint_canvas_scroll.verticalScrollBar()
        h.setValue(h.value() - dx)
        v.setValue(v.value() - dy)

    def _on_picker_painted(self, x: int, y: int) -> None:
        for (cx, cy, cw, ch, tile_ix, bank_ix) in self._picker_cells:
            if cx <= x < cx + cw and cy <= y < cy + ch:
                self._paint_sel_tile = tile_ix
                if self._paint_bank != bank_ix:
                    self._paint_bank = bank_ix
                    self._bank_spin.blockSignals(True)
                    self._bank_spin.setValue(min(bank_ix, self._bank_spin.maximum()))
                    self._bank_spin.blockSignals(False)
                self._write_current_to_brush_cell()
                self._render_sel_preview()
                self._render_picker_overlay()
                return

    def _on_picker_zoom(self, steps: int) -> None:
        new = max(1, min(8, self._picker_scale + steps))
        if new != self._picker_scale:
            self._picker_scale = new
            self._render_picker()

    # ---- brush composer --------------------------------------------------

    def _write_current_to_brush_cell(self) -> None:
        if self._brush_size <= 1 or self._brush_entries is None:
            return
        if 0 <= self._brush_sel < len(self._brush_entries):
            self._brush_entries[self._brush_sel] = self._sel_entry()

    def _load_entry_into_toolbar(self, entry: int) -> None:
        self._paint_sel_tile = entry & 0x3FF
        self._paint_hflip = False
        self._paint_vflip = False
        self._paint_bank = (entry >> 12) & 0xF
        self._bank_spin.blockSignals(True)
        self._bank_spin.setValue(min(self._paint_bank, self._bank_spin.maximum()))
        self._bank_spin.blockSignals(False)
        for chk in (self._hflip_chk, self._vflip_chk):
            chk.blockSignals(True)
            chk.setChecked(False)
            chk.blockSignals(False)

    def _on_brush_cell_clicked(self, idx: int) -> None:
        n = max(1, self._brush_size)
        if not (0 <= idx < n * n):
            return
        self._brush_sel = idx
        if self._brush_entries is not None and idx < len(self._brush_entries):
            self._load_entry_into_toolbar(self._brush_entries[idx])
        self._render_sel_preview()
        self._render_picker_overlay()

    def _on_brush_size_changed(self, size) -> None:
        n = max(1, int(size))
        self._brush_size = n
        self._brush_sel = 0
        self._brush_entries = [self._sel_entry()] * (n * n) if n > 1 else None
        self._render_sel_preview()
        self._render_canvas()

    # ---- toolbar handlers ------------------------------------------------

    def _on_flip(self, axis: str, value: bool) -> None:
        if axis == "h":
            self._paint_hflip = value
        else:
            self._paint_vflip = value
        self._write_current_to_brush_cell()
        self._render_sel_preview()

    def _on_bank_changed(self, value: int) -> None:
        self._paint_bank = int(value)
        self._write_current_to_brush_cell()
        if self._show_all_tiles and self._filter_by_bank:
            self._render_picker()
        else:
            self._render_picker_overlay()
        self._render_sel_preview()

    def _on_show_all_toggled(self, checked: bool) -> None:
        self._show_all_tiles = bool(checked)
        self._filter_bank_chk.setEnabled(self._show_all_tiles)
        if not self._show_all_tiles and self._filter_by_bank:
            self._filter_by_bank = False
            self._filter_bank_chk.blockSignals(True)
            self._filter_bank_chk.setChecked(False)
            self._filter_bank_chk.blockSignals(False)
        self._bank_spin.setEnabled(self._show_all_tiles and self._filter_by_bank)
        self._render_picker()

    def _on_filter_bank_toggled(self, checked: bool) -> None:
        self._filter_by_bank = bool(checked)
        self._bank_spin.setEnabled(self._show_all_tiles and self._filter_by_bank)
        self._render_picker()

    def _on_pick_toggled(self, checked: bool) -> None:
        self._paint_pick_mode = checked
        if checked and self._select_btn.isChecked():
            self._select_btn.setChecked(False)

    def _on_select_toggled(self, checked: bool) -> None:
        self._tool = "select" if checked else "paint"
        if checked and self._pick_btn.isChecked():
            self._pick_btn.setChecked(False)
        if not checked:
            self._clear_selection()
        self._render_canvas()

    def _on_set_bg_clicked(self) -> None:
        self._bg_entry = self._sel_entry()
        self._render_bg_preview()

    def _on_fill_selection_clicked(self) -> None:
        if self._sel_rect is None or not self._paint_entries:
            return
        tw = self._paint_w // 8
        x0, y0, x1, y1 = self._sel_rect
        new_entries = list(self._paint_entries)
        for yy in range(y0, y1 + 1):
            for xx in range(x0, x1 + 1):
                new_entries[yy * tw + xx] = self._bg_entry
        if new_entries != self._paint_entries:
            self._apply_entries(new_entries, "Fill selection")
            self._render_canvas()

    # ---- replace-command callback ---------------------------------------

    def _on_nscr_replaced(self) -> None:
        """Re-render after a replace command's redo/undo flip. The host
        refreshes its own previews via ``on_external_change``; the paint state
        is reloaded from the new bytes (unless we're mid-apply) so undo/redo
        updates the canvas."""
        hook = getattr(self._provider, "on_external_change", None)
        if callable(hook):
            hook()
        if self._suppress_reload:
            return
        self._paint_key = None
        self._ensure_paint_state()
