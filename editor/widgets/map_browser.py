"""Field-map browser (PLAN.md §14.5).

Lists every field-map id present in the ROM and previews the static
two-layer composite plus per-layer renders and an *editable* walkability
overlay. Mirrors the battle-background browser's tab structure so the
shared graphics editors read as one idiom.

Tabs:

- **Composite** — A + B composited (palette index 0 transparent on B).
- **Walkability** — composite with blocked pixels (``.0t`` bit=1)
  tinted red. Active in Phase C: click + drag paints walkable/blocked
  pixels with a pixel- or tile-aligned brush. The FAT splice on save
  lands in Phase F; until then, edits live on the session only.
- **Layer A / Layer B** — Porymap-style tilemap painter (PLAN §14.5
  Phase D). Tile picker on the left, layer canvas on the right; a
  toolbar selects tool (Paint/Pick), palette bank, and H/V flip flags.
  Edits the layer's ``.s`` entries only — tile graphics (``.c``) and
  palette (``.p``) are not re-authored here. Layer B is disabled when
  the map ships no B layer.

Beneath the tabs sits a metadata block (dimensions, file count) and the
``.d`` tuple table — read-only here, structurally matching the Phase E
editor so the visual shape doesn't shift when editing arrives.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import QRect, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPixmap, QUndoStack
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from digimon_core import map as mapmod, map_import

from ..commands import ReplaceMapFileCommand
from .paint_canvas import PaintCanvas
from .record_list_panel import RecordListPanel


_BRUSH_SIZES = (1, 4, 8, 16, 32)
_TOOL_BLOCK = "block"
_TOOL_WALKABLE = "walkable"
_TOOL_PICKER = "picker"

# Tilemap painter tool ids — distinct from walkability tool ids because
# the picker semantics differ ("Pick" copies tile_ix+bank+flips here, not
# block/walkable bit).
_TILE_TOOL_PAINT = "paint"
_TILE_TOOL_PICK = "pick"

_PICKER_TILES_PER_ROW = 8
_PICKER_SCALE = 4

# Selected-tile preview lives above the picker grid. 48 px = 8 px tile
# scaled 6× — big enough to read the pixel art without dominating the
# toolbar column.
_PREVIEW_PX = 48

# Zoom levels for the layer canvas. Integer factors only — fractional
# zoom interpolates pixel art into mush; nearest-neighbour scaling is
# the right call here.
_ZOOM_LEVELS = (1, 2, 3, 4)

# Sectioned tile picker layout. Each section is one palette bank, with
# a header band above the tile grid. Vanilla bank-0 styling for headers
# (#2a2a2a fill, #e0e0e0 text) — visible against any tile content.
_PICKER_HEADER_PX = 18
_PICKER_SECTION_GAP_PX = 4

# Picker zoom range. 1 = 8×8 (raw tiles); 8 = 64×64 (very chunky). 4 is the
# default — matches the legacy fixed cell size.
_PICKER_SCALE_MIN = 1
_PICKER_SCALE_MAX = 8


class _PickerScrollArea(QScrollArea):
    """QScrollArea that emits ``viewportResized`` whenever Qt resizes it.

    The tile picker uses this to reflow its row count when the splitter
    is dragged — wider viewport = more cells per row at the same zoom.
    """

    viewportResized = Signal()

    def resizeEvent(self, ev) -> None:  # noqa: N802
        super().resizeEvent(ev)
        self.viewportResized.emit()


@dataclass
class _LayerPaintState:
    """Per-layer cached state for the Phase D tilemap painter.

    Bound to the currently-selected map; ``MapBrowser._on_index_selected``
    drops both layers' states when the selection changes so a stale tile
    cache can't bleed across maps.
    """
    layer: str  # "a" or "b"
    tiles: List[bytes]
    palettes: List[List[Tuple[int, int, int]]]
    palette_trailer: bytes
    entries: List[int]
    width_px: int
    height_px: int
    # base_rgba is mutated in place during paint strokes — kept as a
    # bytearray (not bytes) so the per-cell repaint is a buffer overwrite
    # rather than a full-image copy.
    base_rgba: bytearray
    selected_tile_ix: int = 0
    selected_bank: int = 0
    hflip: bool = False
    vflip: bool = False
    tool: str = _TILE_TOOL_PAINT
    # Snapshot of entries[] at stroke start, captured the first time
    # _on_tile_painted fires after a press — drives undo by giving the
    # command a stable "old bytes" view to revert to.
    snapshot_entries: Optional[List[int]] = None
    last_cell: Optional[Tuple[int, int]] = None
    # Cached picker QPixmap (no selection overlay); the selection rectangle
    # is drawn on a copy each time the selected tile changes so picking
    # doesn't re-pay the full picker render.
    picker_base_pixmap: Optional[QPixmap] = None
    # Cell map for the sectioned picker, populated alongside
    # picker_base_pixmap. Each entry is (x, y, w, h, tile_ix, bank) —
    # drives both click hit-testing and the selection overlay.
    picker_cells: List[Tuple[int, int, int, int, int, int]] = field(default_factory=list)
    # Picker filter mode. Default (show_all_tiles=False) groups only the
    # (tile, bank) pairs the layer actually uses; "Show all" exposes the
    # whole tile set per bank.
    show_all_tiles: bool = False
    filter_by_bank: bool = False
    # Integer zoom (1..4) for the layer canvas. Larger values upscale
    # the displayed pixmap via nearest-neighbour so pixel art stays sharp.
    scale: int = 1
    # Integer zoom (1..8) for the picker cells. Cell pixel size is
    # ``8 * picker_scale``; the per-row count is computed dynamically from
    # the picker scroll viewport so zooming out widens each row with more
    # cells instead of producing dead horizontal space.
    picker_scale: int = _PICKER_SCALE
    # (tx, ty) tile-grid cell the mouse is currently over, or None when
    # the cursor isn't on the canvas. Drives the gentle hover highlight.
    hover_cell: Optional[Tuple[int, int]] = None
    # Cached scaled pixmap of base_rgba — rebuilt only when base_rgba or
    # scale changes. Hover overlays paint onto a copy of this rather than
    # re-rendering from RGBA each mouse move.
    scaled_base_pixmap: Optional[QPixmap] = None

    @property
    def n_tiles_x(self) -> int:
        return self.width_px // 8

    @property
    def n_tiles_y(self) -> int:
        return self.height_px // 8

    def entry_value(self) -> int:
        """Pack the toolbar state into an NDS BG tilemap u16."""
        e = self.selected_tile_ix & 0x3FF
        if self.hflip:
            e |= 0x400
        if self.vflip:
            e |= 0x800
        e |= (self.selected_bank & 0xF) << 12
        return e


class MapBrowser(QWidget):
    """Viewer for ``DAT/map/`` field maps.

    Phase B: read-only preview tabs + per-map metadata + ``.d`` tuple
    table. Selection state is persisted on the session under the
    ``map_browser`` cursor key.
    """

    _CURSOR_KEY = "map_browser"

    # Layer A leads since it's the primary editing surface; Composite is
    # last because it's read-only/derived and exists for visual reference.
    _TAB_LAYER_A = 0
    _TAB_LAYER_B = 1
    _TAB_WALK = 2
    _TAB_COMPOSITE = 3

    # Column labels for the .d tuple table. Best-guess names per the
    # recon doc (research_docs/claude_notes/btmap_map_recon.md §.d);
    # values are still shown as raw integers per
    # feedback_no_fabricated_game_mechanics.
    _TUPLE_COLUMNS = ("kind", "param_a", "param_b", "flag", "reserved", "weight")

    def __init__(self, session, undo_stack: Optional[QUndoStack] = None, parent=None):
        super().__init__(parent)
        self._session = session
        self._undo_stack = undo_stack
        self._file_table = session.vanilla_file_table()
        self._rom = session.original_rom_data
        self._map_ids: List[str] = mapmod.discover_map_ids(self._file_table)
        self._current_id: Optional[str] = None
        # Pinned RGBA buffers — QImage views the bytes directly. Keying
        # by tab prevents a tab switch from invalidating a still-on-screen
        # pixmap's source buffer.
        self._pinned_rgba: dict[int, bytes] = {}

        # Walkability paint state. ``_active_bits`` holds the live
        # bytearray during a stroke; the composite layer-A/B render is
        # cached per map so each mouse-move re-applies only the tint,
        # not the full layer render.
        self._walk_tool = _TOOL_BLOCK
        self._walk_brush_size = 8
        self._walk_snap_to_tile = False
        self._walk_active_bits: Optional[bytearray] = None
        self._walk_last_pos: Optional[Tuple[int, int]] = None
        self._walk_composite_cache: Optional[mapmod.MapPreview] = None

        # Tilemap-painter state — keyed by "a"/"b" so the two layer tabs
        # don't trample each other when the user flips between them.
        self._layer_state: Dict[str, Optional[_LayerPaintState]] = {"a": None, "b": None}

        self._build_ui()
        remembered = self._session.recall_selection(self._CURSOR_KEY)
        target = 0
        if remembered is not None:
            try:
                target = self._map_ids.index(str(remembered))
            except ValueError:
                target = 0
        if not self._list.select_index(target):
            self._list.select_first()

    # ---- UI construction -------------------------------------------------

    def _build_ui(self) -> None:
        self._list = RecordListPanel(
            records=list(self._map_ids),
            label_for=lambda _ix, mid: f"{int(mid):04d}",
        )
        self._list.indexSelected.connect(self._on_index_selected)

        self._tabs = QTabWidget()
        self._tabs.addTab(self._build_layer_paint_tab("a"), "Layer A")
        self._tabs.addTab(self._build_layer_paint_tab("b"), "Layer B")
        self._tabs.addTab(self._build_walk_tab(), "Walkability")
        self._tabs.addTab(self._build_preview_tab("composite"), "Composite")
        self._tabs.currentChanged.connect(self._on_tab_changed)

        # Metadata block — compact, left-aligned under the tabs.
        self._meta_size = QLabel("\u2014")
        self._meta_layer_b = QLabel("\u2014")
        self._meta_palette = QLabel("\u2014")
        self._meta_walk = QLabel("\u2014")
        self._meta_tuples = QLabel("\u2014")
        fm = self._meta_size.fontMetrics()
        worst = fm.horizontalAdvance("1024\u00d7768  (B: 1024\u00d7768)")
        for lbl in (
            self._meta_size, self._meta_layer_b, self._meta_palette,
            self._meta_walk, self._meta_tuples,
        ):
            lbl.setMinimumWidth(worst)
        meta_form = QFormLayout()
        meta_form.setContentsMargins(0, 0, 0, 0)
        meta_form.addRow("Layer A", self._meta_size)
        meta_form.addRow("Layer B", self._meta_layer_b)
        meta_form.addRow("Palette banks", self._meta_palette)
        meta_form.addRow("Walkability", self._meta_walk)
        meta_form.addRow(".d tuples", self._meta_tuples)

        # .d tuple table is built but hidden — the columns are still
        # opaque (raw integers, no pinned semantics), so showing the grid
        # invites confusion. Kept around so the population path stays warm
        # for when Phase E lands. ``research_docs/.../d_table.tsv`` is the
        # read surface for these in the meantime.
        self._tuples_table = QTableWidget(
            mapmod.DESCRIPTOR_MAX_TUPLES, len(self._TUPLE_COLUMNS),
        )
        self._tuples_table.setHorizontalHeaderLabels(self._TUPLE_COLUMNS)
        self._tuples_table.verticalHeader().setDefaultSectionSize(20)
        self._tuples_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._tuples_table.setSelectionMode(QTableWidget.NoSelection)
        self._tuples_table.setFocusPolicy(Qt.NoFocus)
        hdr = self._tuples_table.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.Stretch)
        self._tuples_table.setMaximumHeight(20 * mapmod.DESCRIPTOR_MAX_TUPLES + 28)
        self._tuples_table.hide()

        # Just the metadata form under the tabs — no table column. Layout
        # left-aligns it so it doesn't stretch across the full width.
        bottom_row = QHBoxLayout()
        bottom_row.setContentsMargins(0, 0, 0, 0)
        bottom_row.addLayout(meta_form, 0)
        bottom_row.addStretch(1)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addWidget(self._tabs, 1)
        right_layout.addLayout(bottom_row, 0)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._list)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([220, 900])

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(splitter)

    def _build_preview_tab(self, key: str) -> QWidget:
        label = QLabel("Select a field map to preview.")
        label.setAlignment(Qt.AlignCenter)
        label.setMinimumSize(512, 384)
        scroll = QScrollArea()
        scroll.setWidget(label)
        scroll.setWidgetResizable(True)
        scroll.setAlignment(Qt.AlignCenter)

        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.addWidget(scroll, 1)
        setattr(self, f"_{key}_label", label)
        return page

    def _build_layer_paint_tab(self, layer: str) -> QWidget:
        """Porymap-style tilemap painter for one layer.

        Layout: a slim toolbar (tool + zoom only) above a horizontal
        splitter. The left side is the layer canvas in a QScrollArea
        (zoom-aware, right-drag panning, Ctrl+wheel zoom). The right
        column groups the *selected tile preview* (image + label +
        H/V flip), the *palette bank* spinner, and the *tile picker*
        — all the things the user manipulates to choose what gets
        stamped — into one visually contiguous block.
        """
        # Tile picker — clicking emits image-space coords; we map those
        # to a tile-grid index in :meth:`_on_picker_painted`.
        picker_canvas = PaintCanvas()
        picker_canvas.setText("(no map loaded)")
        picker_canvas.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        picker_canvas.painted.connect(
            lambda x, y, btns, mods, l=layer: self._on_picker_painted(l, x, y)
        )
        picker_canvas.zoomStepRequested.connect(
            lambda steps, l=layer: self._on_picker_zoom_step(l, steps)
        )
        picker_scroll = _PickerScrollArea()
        picker_scroll.setWidget(picker_canvas)
        picker_scroll.setWidgetResizable(False)
        picker_scroll.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        picker_scroll.viewportResized.connect(
            lambda l=layer: self._on_picker_viewport_resized(l)
        )
        picker_px = _PICKER_TILES_PER_ROW * 8 * _PICKER_SCALE

        # Layer canvas — paints on left-drag, hover highlight on mouse
        # move, right-drag pans, Ctrl+wheel zooms.
        layer_canvas = PaintCanvas()
        layer_canvas.setText("Select a field map to preview.")
        layer_canvas.setAlignment(Qt.AlignCenter)
        layer_canvas.setMinimumSize(512, 384)
        layer_canvas.setHoverEnabled(True)
        layer_canvas.painted.connect(
            lambda x, y, btns, mods, l=layer: self._on_tile_painted(l, x, y)
        )
        layer_canvas.paintFinished.connect(
            lambda l=layer: self._on_tile_paint_finished(l)
        )
        layer_canvas.hovered.connect(
            lambda x, y, l=layer: self._on_tile_hovered(l, x, y)
        )
        layer_canvas.hoverLeft.connect(
            lambda l=layer: self._on_tile_hover_left(l)
        )
        layer_canvas.zoomStepRequested.connect(
            lambda steps, l=layer: self._on_zoom_step(l, steps)
        )
        layer_canvas.panRequested.connect(
            lambda dx, dy, l=layer: self._on_pan(l, dx, dy)
        )
        # Scroll bars when the (zoomed) canvas exceeds viewport bounds —
        # setWidgetResizable(False) keeps the canvas at its pixmap size
        # rather than shrinking to fit, which is what lets zoom + scroll
        # coexist.
        canvas_scroll = QScrollArea()
        canvas_scroll.setWidget(layer_canvas)
        canvas_scroll.setWidgetResizable(False)
        canvas_scroll.setAlignment(Qt.AlignCenter)

        # Color Pick toggle lives in the right column next to the picker
        # — see ``color_pick_row`` further down. Paint is the implicit
        # default tool; Ctrl+wheel on the canvas handles zoom (no on-screen
        # combo). PNG export/import buttons are in a bottom row outside the
        # splitter.
        color_pick_btn = QToolButton()
        color_pick_btn.setText("Color Pick")
        color_pick_btn.setCheckable(True)
        color_pick_btn.setToolTip(
            "Toggle eyedropper mode: clicks on the canvas copy a tile's "
            "index / palette bank / flip state into the picker instead of "
            "painting. Auto-switches back off after one pick."
        )
        color_pick_btn.toggled.connect(
            lambda checked, l=layer: self._on_tile_tool_chosen(
                l, _TILE_TOOL_PICK if checked else _TILE_TOOL_PAINT,
            )
        )

        # Selected-tile row: preview on the left; "Selected tile: N" label
        # and H/V flip checkboxes stacked vertically on the right. Stacking
        # the flips under the label keeps the row narrow so the right
        # column (and therefore the picker) can be dragged smaller.
        preview_row = QHBoxLayout()
        preview_row.setContentsMargins(0, 0, 0, 0)
        preview_lbl = QLabel()
        preview_lbl.setFixedSize(_PREVIEW_PX, _PREVIEW_PX)
        preview_lbl.setAlignment(Qt.AlignCenter)
        preview_lbl.setStyleSheet(
            "background: #1d1d1d; border: 1px solid #555;"
        )
        preview_row.addWidget(preview_lbl)

        sel_block = QVBoxLayout()
        sel_block.setContentsMargins(0, 0, 0, 0)
        sel_lbl = QLabel("Selected tile: \u2014")
        sel_lbl.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        sel_block.addWidget(sel_lbl)
        flip_row = QHBoxLayout()
        flip_row.setContentsMargins(0, 0, 0, 0)
        hflip_chk = QCheckBox("H-flip")
        hflip_chk.setToolTip(
            "Mirror the selected tile horizontally before painting."
        )
        hflip_chk.toggled.connect(
            lambda v, l=layer: self._on_flip_toggled(l, "h", v)
        )
        flip_row.addWidget(hflip_chk)
        vflip_chk = QCheckBox("V-flip")
        vflip_chk.setToolTip(
            "Mirror the selected tile vertically before painting."
        )
        vflip_chk.toggled.connect(
            lambda v, l=layer: self._on_flip_toggled(l, "v", v)
        )
        flip_row.addWidget(vflip_chk)
        flip_row.addStretch(1)
        sel_block.addLayout(flip_row)
        preview_row.addLayout(sel_block, 1)

        # Filter block — default shows only (tile, bank) pairs actually
        # used in the layer, grouped by bank. "Show all tiles" exposes
        # the full set; "Filter by palette bank" + spinner narrows the
        # full set to one bank (gated behind "Show all" — meaningless
        # in the default filtered view since each used tile is already
        # shown in its canonical bank).
        show_all_chk = QCheckBox("Show all tiles")
        show_all_chk.setToolTip(
            "Default: only tiles used in this layer, grouped by palette bank.\n"
            "Checked: every tile in the tileset, listed per palette bank."
        )
        show_all_chk.toggled.connect(
            lambda v, l=layer: self._on_show_all_toggled(l, v)
        )

        filter_bank_chk = QCheckBox("Filter by palette bank")
        filter_bank_chk.setEnabled(False)
        filter_bank_chk.setToolTip(
            "Narrow the 'Show all tiles' view to a single palette bank."
        )
        filter_bank_chk.toggled.connect(
            lambda v, l=layer: self._on_filter_bank_toggled(l, v)
        )
        bank_spin = QSpinBox()
        bank_spin.setMinimum(0)
        bank_spin.setMaximum(15)
        bank_spin.setEnabled(False)
        bank_spin.setToolTip(
            "Palette bank stamped onto painted tiles. Editable only "
            "with 'Show all tiles' + 'Filter by palette bank' enabled; "
            "otherwise picking a tile selects its bank for you."
        )
        bank_spin.valueChanged.connect(
            lambda v, l=layer: self._on_bank_changed(l, v)
        )

        bank_row = QHBoxLayout()
        bank_row.setContentsMargins(0, 0, 0, 0)
        bank_row.addWidget(filter_bank_chk)
        bank_row.addWidget(bank_spin)
        bank_row.addStretch(1)

        color_pick_row = QHBoxLayout()
        color_pick_row.setContentsMargins(0, 0, 0, 0)
        color_pick_row.addWidget(color_pick_btn)
        color_pick_row.addStretch(1)

        right_col = QWidget()
        # Min width sized to the preview+flips block (the picker reflows
        # to whatever the splitter gives it, but the preview pixmap and
        # H/V flip checkboxes need a sensible floor or they overflow).
        right_col_min = _PREVIEW_PX + 150
        right_col.setMinimumWidth(right_col_min)
        right_col_layout = QVBoxLayout(right_col)
        right_col_layout.setContentsMargins(0, 0, 0, 0)
        right_col_layout.addLayout(preview_row)
        right_col_layout.addWidget(show_all_chk)
        right_col_layout.addLayout(bank_row)
        right_col_layout.addLayout(color_pick_row)
        right_col_layout.addWidget(picker_scroll, 1)

        # Canvas on the left (stretches), preview+picker column on the right.
        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(canvas_scroll)
        splitter.addWidget(right_col)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)

        # Bottom row: PNG round-trip buttons live outside the splitter so
        # they don't fight the picker for horizontal space.
        export_btn = QToolButton()
        export_btn.setText("Export PNG")
        export_btn.setToolTip(
            "Save the current layer composite as a PNG. Round-tripping "
            "is lossy: import quantizes colors and may merge near-identical "
            "tiles to stay under the 1024-tile per-layer cap."
        )
        export_btn.clicked.connect(
            lambda _checked=False, l=layer: self._on_export_layer_png(l)
        )
        import_btn = QToolButton()
        import_btn.setText("Import PNG")
        import_btn.setToolTip(
            "Replace this layer's tileset, palette and tilemap from a "
            "PNG of the same dimensions. The pipeline quantizes to "
            "multi-bank 16-color palettes, dedups 8×8 tiles with flips, "
            "and merges to ≤1024 unique tiles."
        )
        import_btn.clicked.connect(
            lambda _checked=False, l=layer: self._on_import_layer_png(l)
        )
        bottom_row = QHBoxLayout()
        bottom_row.setContentsMargins(0, 0, 0, 0)
        bottom_row.addStretch(1)
        bottom_row.addWidget(export_btn)
        bottom_row.addWidget(import_btn)

        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.addWidget(splitter, 1)
        page_layout.addLayout(bottom_row)

        # Stash widget refs per layer so event handlers can mutate them.
        setattr(self, f"_layer_{layer}_picker_canvas", picker_canvas)
        setattr(self, f"_layer_{layer}_picker_scroll", picker_scroll)
        setattr(self, f"_layer_{layer}_canvas", layer_canvas)
        setattr(self, f"_layer_{layer}_canvas_scroll", canvas_scroll)
        setattr(self, f"_layer_{layer}_bank_spin", bank_spin)
        setattr(self, f"_layer_{layer}_hflip_chk", hflip_chk)
        setattr(self, f"_layer_{layer}_vflip_chk", vflip_chk)
        setattr(self, f"_layer_{layer}_color_pick_btn", color_pick_btn)
        setattr(self, f"_layer_{layer}_sel_lbl", sel_lbl)
        setattr(self, f"_layer_{layer}_preview_lbl", preview_lbl)
        setattr(self, f"_layer_{layer}_show_all_chk", show_all_chk)
        setattr(self, f"_layer_{layer}_filter_bank_chk", filter_bank_chk)
        return page

    def _build_walk_tab(self) -> QWidget:
        """Walkability tab: paint canvas + tool/brush row.

        Canvas is a :class:`PaintCanvas` so mouse press/drag map to
        image-space pixel coordinates without the host having to know
        about Qt's pixmap-centering quirks.
        """
        self._walk_label = PaintCanvas()
        self._walk_label.setText("Select a field map to preview.")
        self._walk_label.setAlignment(Qt.AlignCenter)
        self._walk_label.setMinimumSize(512, 384)
        self._walk_label.painted.connect(self._on_walk_painted)
        self._walk_label.paintFinished.connect(self._on_walk_paint_finished)

        scroll = QScrollArea()
        scroll.setWidget(self._walk_label)
        scroll.setWidgetResizable(True)
        scroll.setAlignment(Qt.AlignCenter)

        # Toolbar row — three tools (block / walkable / picker), brush
        # size combo, snap-to-tile toggle. Disabled until a map is loaded
        # so the buttons don't look interactive before any preview ships.
        tool_row = QHBoxLayout()
        tool_row.setContentsMargins(0, 0, 0, 0)
        self._walk_tool_group = QButtonGroup(self)
        self._walk_tool_group.setExclusive(True)
        for tool_id, label in (
            (_TOOL_BLOCK, "Block"),
            (_TOOL_WALKABLE, "Walkable"),
            (_TOOL_PICKER, "Pick"),
        ):
            btn = QToolButton()
            btn.setText(label)
            btn.setCheckable(True)
            if tool_id == _TOOL_BLOCK:
                btn.setChecked(True)
            btn.clicked.connect(
                lambda _checked=False, tid=tool_id: self._on_walk_tool_chosen(tid)
            )
            self._walk_tool_group.addButton(btn)
            tool_row.addWidget(btn)

        tool_row.addSpacing(12)
        tool_row.addWidget(QLabel("Brush:"))
        self._walk_brush_combo = QComboBox()
        for size in _BRUSH_SIZES:
            self._walk_brush_combo.addItem(f"{size} px", size)
        self._walk_brush_combo.setCurrentIndex(_BRUSH_SIZES.index(self._walk_brush_size))
        self._walk_brush_combo.currentIndexChanged.connect(
            lambda _ix: self._set_brush_size(self._walk_brush_combo.currentData())
        )
        tool_row.addWidget(self._walk_brush_combo)

        self._walk_snap_chk = QCheckBox("Snap to 8 px")
        self._walk_snap_chk.toggled.connect(self._on_walk_snap_toggled)
        tool_row.addWidget(self._walk_snap_chk)

        tool_row.addStretch(1)

        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.addLayout(tool_row)
        page_layout.addWidget(scroll, 1)
        return page

    # ---- Walkability paint event handlers -------------------------------

    def _on_walk_tool_chosen(self, tool_id: str) -> None:
        self._walk_tool = tool_id

    def _set_brush_size(self, size: int) -> None:
        self._walk_brush_size = int(size)

    def _on_walk_snap_toggled(self, checked: bool) -> None:
        self._walk_snap_to_tile = bool(checked)

    def _on_walk_painted(
        self, x: int, y: int, _buttons, _modifiers,
    ) -> None:
        if self._current_id is None or self._undo_stack is None:
            return
        files = mapmod.MapFiles(self._current_id)
        if files.walkability not in self._file_table:
            return
        if self._walk_active_bits is None:
            # Snapshot the live bits at stroke start so undo restores
            # the pre-stroke state regardless of how many move events
            # arrive between press and release.
            _, _, bits = mapmod.parse_walkability(
                self._session.map_file_bytes(files.walkability)
            )
            self._walk_active_bits = bytearray(bits)
            self._walk_last_pos = None
        if self._walk_tool == _TOOL_PICKER:
            # Picker reads the live bit and switches the active tool;
            # no in-place edit, no re-render. Strides by walk width so
            # the bit lookup is correct even on dim-mismatched maps.
            ww, wh = self._walk_dims_or_fallback()
            if 0 <= x < ww and 0 <= y < wh:
                ix = y * ww + x
                blocked = (self._walk_active_bits[ix >> 3] >> (ix & 7)) & 1
                next_tool = _TOOL_BLOCK if blocked else _TOOL_WALKABLE
                self._set_walk_tool_button(next_tool)
            return
        # Block / Walkable: paint a brush, with line interpolation from
        # the previous sample so fast drags don't leave gaps.
        if self._walk_last_pos is not None:
            self._walk_draw_line(self._walk_last_pos, (x, y))
        else:
            self._walk_paint_brush(x, y)
        self._walk_last_pos = (x, y)
        self._refresh_walk_overlay_live()

    def _on_walk_paint_finished(self) -> None:
        if self._walk_active_bits is None or self._current_id is None:
            return
        if self._walk_tool == _TOOL_PICKER or self._undo_stack is None:
            self._walk_active_bits = None
            self._walk_last_pos = None
            return
        files = mapmod.MapFiles(self._current_id)
        w, h, _ = mapmod.parse_walkability(
            self._session.map_file_bytes(files.walkability)
        )
        new_bytes = mapmod.build_walkability(w, h, bytes(self._walk_active_bits))
        map_id = self._current_id
        cmd = ReplaceMapFileCommand(
            self._session,
            files.walkability,
            new_bytes,
            f"Paint walkability ({map_id})",
            on_change=self._on_walk_command_applied,
        )
        # Push the new buffer through redo first so the dirty cache
        # picks up the edit; redo() inside QUndoStack.push() handles it.
        self._walk_active_bits = None
        self._walk_last_pos = None
        self._undo_stack.push(cmd)

    def _on_walk_command_applied(self) -> None:
        # Triggered after every redo/undo flip — drop the cached
        # composite isn't necessary (only the .0t changed, not A/B), but
        # re-rendering the walk tab makes the overlay reflect the new
        # bits. Also refresh metadata since blocked-% may have changed.
        if self._current_id is not None:
            self._update_metadata_and_tuples(self._current_id)
        if self._tabs.currentIndex() == self._TAB_WALK:
            self._refresh_active_tab()

    def _walk_paint_brush(self, cx: int, cy: int) -> None:
        if self._walk_active_bits is None or self._current_id is None:
            return
        size = max(1, self._walk_brush_size)
        if self._walk_snap_to_tile:
            cx = (cx // 8) * 8 + 4
            cy = (cy // 8) * 8 + 4
        half = size // 2
        # Stride by walk dims so the bit index is correct on maps where
        # the walkability is wider than the composite (map 88).
        w, h = self._walk_dims_or_fallback()
        x0 = max(0, cx - half)
        y0 = max(0, cy - half)
        x1 = min(w, cx + (size - half))
        y1 = min(h, cy + (size - half))
        bits = self._walk_active_bits
        set_bit = self._walk_tool == _TOOL_BLOCK
        for y in range(y0, y1):
            row = y * w
            for x in range(x0, x1):
                ix = row + x
                byte = bits[ix >> 3]
                mask = 1 << (ix & 7)
                bits[ix >> 3] = (byte | mask) if set_bit else (byte & ~mask)

    def _walk_draw_line(
        self, p0: Tuple[int, int], p1: Tuple[int, int],
    ) -> None:
        """Bresenham line between two sample points, painting the brush
        at every step. Prevents dotted strokes when the mouse moves
        faster than mouseMoveEvent samples."""
        x0, y0 = p0
        x1, y1 = p1
        dx = abs(x1 - x0)
        dy = -abs(y1 - y0)
        sx = 1 if x0 < x1 else -1
        sy = 1 if y0 < y1 else -1
        err = dx + dy
        while True:
            self._walk_paint_brush(x0, y0)
            if x0 == x1 and y0 == y1:
                return
            e2 = 2 * err
            if e2 >= dy:
                err += dy
                x0 += sx
            if e2 <= dx:
                err += dx
                y0 += sy

    def _walk_dims_or_fallback(self) -> Tuple[int, int]:
        """Always returns the *walkability's* own dims, not the composite's.

        In vanilla Dusk, map 88's walkability is 768×384 vs a 752×384
        composite — striding by composite width would corrupt the bit
        indexing in both the paint brush and the overlay.
        """
        if self._current_id is None:
            return (0, 0)
        files = mapmod.MapFiles(self._current_id)
        w, h, _ = mapmod.parse_walkability(
            self._session.map_file_bytes(files.walkability)
        )
        return w, h

    def _refresh_walk_overlay_live(self) -> None:
        """Re-tint the cached composite with the in-progress bits and
        push the resulting pixmap to the label. Skips the layer-A/B
        render — that's cached for the lifetime of the selection."""
        if self._walk_composite_cache is None or self._walk_active_bits is None:
            return
        ww, wh = self._walk_dims_or_fallback()
        preview = mapmod.apply_walkability_overlay(
            self._walk_composite_cache, bytes(self._walk_active_bits), ww, wh,
        )
        self._set_pixmap_on_tab(self._TAB_WALK, preview)

    def _set_walk_tool_button(self, tool_id: str) -> None:
        self._walk_tool = tool_id
        for btn in self._walk_tool_group.buttons():
            if btn.text() == ("Block" if tool_id == _TOOL_BLOCK else
                              "Walkable" if tool_id == _TOOL_WALKABLE else
                              "Pick"):
                btn.setChecked(True)
                break

    # ---- Tilemap painter event handlers ---------------------------------

    def _ensure_layer_state(self, layer: str) -> _LayerPaintState:
        """Build the painter state for one layer from the live FAT.

        Cached on ``self._layer_state[layer]`` for the lifetime of the
        current selection — switching maps drops it, switching tabs
        reuses it.
        """
        state = self._layer_state.get(layer)
        if state is not None:
            return state
        files = mapmod.MapFiles(self._current_id)
        tiles_path, pal_path, scr_path = (
            (files.layer_a_tiles, files.layer_a_palette, files.layer_a_screen)
            if layer == "a"
            else (files.layer_b_tiles, files.layer_b_palette, files.layer_b_screen)
        )
        tiles = mapmod.parse_tiles(self._session.map_file_bytes(tiles_path))
        palettes, trailer = mapmod.parse_palette(self._session.map_file_bytes(pal_path))
        width_px, height_px, entries = mapmod.parse_screen(
            self._session.map_file_bytes(scr_path)
        )
        rgba = mapmod._render_layer(
            width_px, height_px, entries, tiles, palettes,
            backdrop_opaque=True,
        )
        state = _LayerPaintState(
            layer=layer,
            tiles=tiles,
            palettes=palettes,
            palette_trailer=trailer,
            entries=list(entries),
            width_px=width_px,
            height_px=height_px,
            base_rgba=rgba,
        )
        # Clamp the spinner to the palette banks the map actually has —
        # painting with bank 8 when only 7 exist would silently fall back
        # to bank 0 inside _render_layer.
        bank_spin: QSpinBox = getattr(self, f"_layer_{layer}_bank_spin")
        bank_spin.blockSignals(True)
        bank_spin.setMaximum(max(0, len(palettes) - 1))
        bank_spin.setValue(state.selected_bank)
        bank_spin.blockSignals(False)
        # Reset the canvas image-scale to 1x on every new selection —
        # carrying zoom across maps would feel disorienting when the
        # next map has different dimensions.
        state.scale = 1
        canvas: PaintCanvas = getattr(self, f"_layer_{layer}_canvas")
        canvas.setImageScale(state.scale)
        # Reset the picker filter checkboxes to the default (filtered
        # by used pairs) on each map switch — bank/tile mappings are
        # per-map, so persisting a "Show all" choice across maps usually
        # creates more confusion than convenience.
        show_all_chk: QCheckBox = getattr(self, f"_layer_{layer}_show_all_chk")
        filter_chk: QCheckBox = getattr(self, f"_layer_{layer}_filter_bank_chk")
        for chk in (show_all_chk, filter_chk):
            chk.blockSignals(True)
            chk.setChecked(False)
            chk.blockSignals(False)
        filter_chk.setEnabled(False)
        bank_spin.setEnabled(False)
        self._layer_state[layer] = state
        self._refresh_selected_tile_preview(layer)
        return state

    def _compute_picker_sections(
        self, state: _LayerPaintState,
    ) -> List[Tuple[int, List[int]]]:
        """Return ``[(bank_ix, [tile_ix, ...]), ...]`` for the current filter.

        Three modes:

        - ``show_all_tiles=False``: default. Walk ``entries[]`` and
          collect the (tile_ix, bank) pairs the layer actually uses,
          then within each bank order tiles by a greedy adjacency
          chain — anchor at the first-appearance tile, then repeatedly
          append the unplaced tile most strongly 4-connected to
          already-placed ones. Ladder pieces (vertically stacked on
          the map) and doorway pairs (side-by-side) collapse into
          contiguous picker runs that scanning by tile_ix scatters.
        - ``show_all_tiles=True, filter_by_bank=True``: one section
          containing every tile in ``state.selected_bank``.
        - ``show_all_tiles=True, filter_by_bank=False``: one section
          per available palette bank, each containing every tile.
        """
        if not state.show_all_tiles:
            used: Dict[int, set] = {}
            for e in state.entries:
                tile_ix = e & 0x3FF
                bank = (e >> 12) & 0xF
                if tile_ix >= len(state.tiles) or bank >= len(state.palettes):
                    continue
                used.setdefault(bank, set()).add(tile_ix)
            return [
                (bank, self._cluster_bank_tiles_by_adjacency(state, bank, used[bank]))
                for bank in sorted(used)
            ]
        if state.filter_by_bank:
            bank = state.selected_bank
            if bank >= len(state.palettes):
                bank = 0
            return [(bank, list(range(len(state.tiles))))]
        return [
            (bank, list(range(len(state.tiles))))
            for bank in range(len(state.palettes))
        ]

    def _cluster_bank_tiles_by_adjacency(
        self, state: _LayerPaintState, bank: int, used_tiles: set,
    ) -> List[int]:
        """Greedy-chain order tiles in ``bank`` by 4-connected adjacency.

        Counts every right/down co-occurrence of (this bank, tile_a)
        with (this bank, tile_b) on the tilemap, then walks tiles by
        always picking the unplaced one whose adjacency to already-
        placed tiles is highest (ties broken by first-appearance, so
        the start of a fresh cluster is still the top-left-most tile
        in it). O(n_entries) build + O(used²) walk; well under a
        millisecond on Dusk's maps.
        """
        if not used_tiles:
            return []
        n_x = state.n_tiles_x
        entries = state.entries
        n_y = len(entries) // n_x if n_x else 0

        first_pos: Dict[int, int] = {}
        adj: Dict[int, Dict[int, int]] = {t: {} for t in used_tiles}
        for y in range(n_y):
            row_off = y * n_x
            for x in range(n_x):
                e = entries[row_off + x]
                if (e >> 12) & 0xF != bank:
                    continue
                t = e & 0x3FF
                if t not in used_tiles:
                    continue
                pos = row_off + x
                if t not in first_pos:
                    first_pos[t] = pos
                # Right neighbour
                if x + 1 < n_x:
                    er = entries[row_off + x + 1]
                    if (er >> 12) & 0xF == bank:
                        tr = er & 0x3FF
                        if tr in used_tiles and tr != t:
                            adj[t][tr] = adj[t].get(tr, 0) + 1
                            adj[tr][t] = adj[tr].get(t, 0) + 1
                # Down neighbour
                if y + 1 < n_y:
                    ed = entries[row_off + n_x + x]
                    if (ed >> 12) & 0xF == bank:
                        td = ed & 0x3FF
                        if td in used_tiles and td != t:
                            adj[t][td] = adj[t].get(td, 0) + 1
                            adj[td][t] = adj[td].get(t, 0) + 1

        sentinel = len(entries) + 1
        remaining = set(used_tiles)
        order: List[int] = []
        start = min(remaining, key=lambda t: first_pos.get(t, sentinel))
        order.append(start)
        remaining.discard(start)
        score: Dict[int, int] = dict(adj.get(start, {}))
        while remaining:
            best = None
            best_key: Optional[Tuple[int, int]] = None
            for t in remaining:
                key = (-score.get(t, 0), first_pos.get(t, sentinel))
                if best_key is None or key < best_key:
                    best_key = key
                    best = t
            order.append(best)
            remaining.discard(best)
            score.pop(best, None)
            for nb, n in adj.get(best, {}).items():
                if nb in remaining:
                    score[nb] = score.get(nb, 0) + n
        return order

    def _refresh_picker(self, layer: str) -> None:
        """Rebuild the sectioned picker pixmap + cell map.

        Each section is one palette bank: header band with the bank
        index, then a grid of tile cells in that bank's palette. Cells
        are laid out 16 wide; the section's grid height grows to fit
        its tile count.

        Picker cells are cached on the state for hit-testing (so a
        click at (px, py) maps back to the correct (tile_ix, bank)) and
        for the selection-overlay redraw path.
        """
        state = self._layer_state.get(layer)
        if state is None:
            return
        canvas: PaintCanvas = getattr(self, f"_layer_{layer}_picker_canvas")
        scroll: _PickerScrollArea = getattr(self, f"_layer_{layer}_picker_scroll")
        cell_px = 8 * state.picker_scale
        # Cell columns scale with the picker viewport — when the splitter
        # is wider (or the zoom is lower), more cells fit per row and each
        # row "expands horizontally" rather than producing wasted gutter.
        # Pre-show viewport().width() is small; fall back to one default
        # row width so the initial render isn't a single thin column.
        viewport_w = scroll.viewport().width()
        if viewport_w < cell_px:
            viewport_w = _PICKER_TILES_PER_ROW * 8 * _PICKER_SCALE
        cols = max(1, viewport_w // cell_px)
        width_px = cols * cell_px

        sections = self._compute_picker_sections(state)
        if not sections:
            # Filtered mode on a layer with no usable entries — show a
            # placeholder so the canvas isn't a confusing blank.
            pixmap = QPixmap(width_px, _PICKER_HEADER_PX)
            pixmap.fill(QColor("#1a1a1a"))
            state.picker_base_pixmap = pixmap
            state.picker_cells = []
            canvas.setText("")
            canvas.setPixmap(pixmap)
            canvas.resize(width_px, _PICKER_HEADER_PX)
            return

        # Precompute layout so we can size the pixmap before painting.
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
                gx = i % cols
                gy = i // cols
                cx = gx * cell_px
                cy = grid_y + gy * cell_px
                buf = bytearray(8 * 8 * 4)
                entry = (tile_ix & 0x3FF) | ((bank_ix & 0xF) << 12)
                mapmod.paint_tile_into_rgba(
                    buf, 8, 0, 0, entry,
                    state.tiles, state.palettes, backdrop_opaque=True,
                )
                img = QImage(bytes(buf), 8, 8, 32, QImage.Format_RGBA8888)
                painter.drawImage(QRect(cx, cy, cell_px, cell_px), img)
                cells.append((cx, cy, cell_px, cell_px, tile_ix, bank_ix))
        painter.end()

        state.picker_base_pixmap = pixmap
        state.picker_cells = cells
        canvas.setText("")
        canvas.resize(width_px, pixmap.height())
        self._refresh_picker_overlay(layer)

    def _refresh_picker_overlay(self, layer: str) -> None:
        """Draw the selection rectangle around the cell that matches the
        current (selected_tile_ix, selected_bank). In modes where the
        selection isn't present (e.g. filtered view of a tile the layer
        doesn't use), no overlay is drawn — the picker just shows the
        underlying tiles. Cheap because only the rectangle is repainted.
        """
        state = self._layer_state.get(layer)
        if state is None or state.picker_base_pixmap is None:
            return
        pixmap = QPixmap(state.picker_base_pixmap)
        target = None
        for (cx, cy, cw, ch, tile_ix, bank_ix) in state.picker_cells:
            if tile_ix == state.selected_tile_ix and bank_ix == state.selected_bank:
                target = (cx, cy, cw, ch)
                break
        if target is not None:
            cx, cy, cw, ch = target
            rect = QRect(cx, cy, cw, ch)
            painter = QPainter(pixmap)
            # Two-tone outline: white outer + black inner so the rectangle
            # is visible against any tile color (light tiles, dark tiles,
            # the picker's black gutter pixels).
            outer = QPen(QColor(255, 255, 255))
            outer.setWidth(2)
            painter.setPen(outer)
            painter.drawRect(rect.adjusted(0, 0, -1, -1))
            inner = QPen(QColor(0, 0, 0))
            inner.setWidth(1)
            painter.setPen(inner)
            painter.drawRect(rect.adjusted(1, 1, -2, -2))
            painter.end()
        canvas: PaintCanvas = getattr(self, f"_layer_{layer}_picker_canvas")
        canvas.setPixmap(pixmap)

    def _rebuild_base_pixmap(self, layer: str) -> None:
        """Re-render ``state.base_rgba`` into a QPixmap and cache the
        zoom-scaled version on the state. The hover overlay is drawn
        onto a copy of this cached pixmap so every mouse-move doesn't
        re-pay the full RGBA → QPixmap conversion.
        """
        state = self._layer_state.get(layer)
        if state is None:
            return
        # `backing` only needs to live for the QImage → QPixmap.fromImage
        # call; fromImage copies into device memory, so the bytes can
        # be released as soon as this method returns.
        backing = bytes(state.base_rgba)
        image = QImage(
            backing,
            state.width_px, state.height_px, state.width_px * 4,
            QImage.Format_RGBA8888,
        )
        pixmap = QPixmap.fromImage(image)
        if state.scale > 1:
            pixmap = pixmap.scaled(
                state.width_px * state.scale,
                state.height_px * state.scale,
                Qt.IgnoreAspectRatio,
                Qt.FastTransformation,  # nearest-neighbour — pixel art stays sharp
            )
        state.scaled_base_pixmap = pixmap

    def _refresh_layer_canvas(self, layer: str) -> None:
        """Push the cached scaled pixmap (with hover overlay) to the canvas.

        Rebuilds the cache lazily when invalidated. The hover overlay is
        painted onto a fresh copy each call so toggling hover on/off
        doesn't smear ghost rectangles across the canvas.
        """
        state = self._layer_state.get(layer)
        if state is None:
            return
        if state.scaled_base_pixmap is None:
            self._rebuild_base_pixmap(layer)
        if state.scaled_base_pixmap is None:
            return
        pixmap = QPixmap(state.scaled_base_pixmap)  # cheap copy-on-write
        if state.hover_cell is not None:
            tx, ty = state.hover_cell
            if 0 <= tx < state.n_tiles_x and 0 <= ty < state.n_tiles_y:
                cell = 8 * state.scale
                rect = QRect(tx * cell, ty * cell, cell, cell)
                painter = QPainter(pixmap)
                # Translucent yellow fill + outline — visible against
                # both light and dark tilesets without obscuring the
                # underlying pixels.
                painter.fillRect(rect, QColor(255, 230, 0, 70))
                pen = QPen(QColor(255, 230, 0, 220))
                pen.setWidth(1)
                painter.setPen(pen)
                painter.drawRect(rect.adjusted(0, 0, -1, -1))
                painter.end()
        canvas: PaintCanvas = getattr(self, f"_layer_{layer}_canvas")
        canvas.setText("")
        canvas.setPixmap(pixmap)
        canvas.resize(pixmap.width(), pixmap.height())

    def _refresh_selected_tile_preview(self, layer: str) -> None:
        """Render the picked tile (with bank + flips) into the preview label."""
        state = self._layer_state.get(layer)
        if state is None:
            return
        preview_lbl: QLabel = getattr(self, f"_layer_{layer}_preview_lbl")
        sel_lbl: QLabel = getattr(self, f"_layer_{layer}_sel_lbl")
        if not state.tiles:
            preview_lbl.clear()
            sel_lbl.setText("Selected tile: \u2014")
            return
        # 8x8 RGBA buffer just big enough for one tile; paint_tile_into_rgba
        # writes the picked entry (tile + bank + flips) into (0, 0).
        buf = bytearray(8 * 8 * 4)
        mapmod.paint_tile_into_rgba(
            buf, 8, 0, 0, state.entry_value(),
            state.tiles, state.palettes, backdrop_opaque=True,
        )
        backing = bytes(buf)
        image = QImage(backing, 8, 8, 8 * 4, QImage.Format_RGBA8888)
        pixmap = QPixmap.fromImage(image).scaled(
            _PREVIEW_PX, _PREVIEW_PX,
            Qt.KeepAspectRatio, Qt.FastTransformation,
        )
        preview_lbl.setPixmap(pixmap)
        sel_lbl.setText(f"Selected tile: {state.selected_tile_ix}")

    def _on_picker_painted(self, layer: str, x: int, y: int) -> None:
        """Hit-test (x, y) against the cached picker cell rects. Each
        cell carries its own (tile_ix, bank), so picking sets both —
        the sectioned picker treats each section's bank as the
        canonical bank for the tiles within it.
        """
        state = self._layer_state.get(layer)
        if state is None:
            return
        for (cx, cy, cw, ch, tile_ix, bank_ix) in state.picker_cells:
            if cx <= x < cx + cw and cy <= y < cy + ch:
                state.selected_tile_ix = tile_ix
                if state.selected_bank != bank_ix:
                    state.selected_bank = bank_ix
                    # Keep the spinner display in sync even when it's
                    # disabled — otherwise its stale value is the one
                    # the user sees when they later enable the filter.
                    bank_spin: QSpinBox = getattr(self, f"_layer_{layer}_bank_spin")
                    bank_spin.blockSignals(True)
                    bank_spin.setValue(min(bank_ix, bank_spin.maximum()))
                    bank_spin.blockSignals(False)
                self._refresh_selected_tile_preview(layer)
                self._refresh_picker_overlay(layer)
                return

    def _on_tile_painted(self, layer: str, x: int, y: int) -> None:
        if self._current_id is None or self._undo_stack is None:
            return
        state = self._layer_state.get(layer)
        if state is None:
            return
        tx = x // 8
        ty = y // 8
        if not (0 <= tx < state.n_tiles_x and 0 <= ty < state.n_tiles_y):
            return
        if state.tool == _TILE_TOOL_PICK:
            self._pick_cell(layer, tx, ty)
            return
        if state.snapshot_entries is None:
            # First sample of a new stroke — freeze the pre-stroke entries
            # so the undo command can revert to byte-identical .s.
            state.snapshot_entries = list(state.entries)
            state.last_cell = None
        if state.last_cell is None:
            self._paint_cell(layer, tx, ty)
        else:
            # Bresenham in tile-space — fast drags across multiple cells
            # otherwise leave a dotted trail of single-tile stamps.
            self._paint_tile_line(layer, state.last_cell, (tx, ty))
        state.last_cell = (tx, ty)
        state.scaled_base_pixmap = None  # base_rgba mutated — drop cache
        self._refresh_layer_canvas(layer)

    def _on_tile_paint_finished(self, layer: str) -> None:
        state = self._layer_state.get(layer)
        if state is None or self._current_id is None or self._undo_stack is None:
            return
        if state.tool == _TILE_TOOL_PICK or state.snapshot_entries is None:
            state.snapshot_entries = None
            state.last_cell = None
            return
        files = mapmod.MapFiles(self._current_id)
        scr_path = files.layer_a_screen if layer == "a" else files.layer_b_screen
        new_bytes = mapmod.build_screen(
            state.width_px, state.height_px, state.entries,
        )
        map_id = self._current_id
        cmd = ReplaceMapFileCommand(
            self._session, scr_path, new_bytes,
            f"Paint tilemap ({map_id} layer {layer.upper()})",
            on_change=lambda l=layer: self._on_tile_command_applied(l),
        )
        state.snapshot_entries = None
        state.last_cell = None
        self._undo_stack.push(cmd)

    def _on_tile_command_applied(self, layer: str) -> None:
        # The .s file changed under us (undo or redo). Only entries[] and
        # base_rgba depend on it — tiles/palettes/dims are stable. Update
        # them in place rather than dropping the whole state, so the
        # toolbar (selected tile / bank / flips / tool) doesn't reset on
        # every undo step.
        state = self._layer_state.get(layer)
        if state is None or self._current_id is None:
            return
        files = mapmod.MapFiles(self._current_id)
        scr_path = files.layer_a_screen if layer == "a" else files.layer_b_screen
        _, _, entries = mapmod.parse_screen(
            self._session.map_file_bytes(scr_path)
        )
        state.entries = list(entries)
        state.base_rgba = mapmod._render_layer(
            state.width_px, state.height_px, entries,
            state.tiles, state.palettes, backdrop_opaque=True,
        )
        state.scaled_base_pixmap = None  # base_rgba replaced — drop cache
        tab_ix = self._TAB_LAYER_A if layer == "a" else self._TAB_LAYER_B
        if self._tabs.currentIndex() == tab_ix:
            self._refresh_layer_canvas(layer)
            # Filtered picker's set of (tile, bank) pairs depends on
            # entries[] — a stroke or undo may have added/removed pairs.
            # Show-all modes' contents don't change with entries[], so
            # an overlay refresh is enough.
            if not state.show_all_tiles:
                self._refresh_picker(layer)
            else:
                self._refresh_picker_overlay(layer)

    # ---- PNG export / import --------------------------------------------

    def _on_export_layer_png(self, layer: str) -> None:
        """Save the current layer composite as a PNG."""
        state = self._layer_state.get(layer)
        if state is None or self._current_id is None:
            QMessageBox.information(
                self, "Export PNG", "Select a map first."
            )
            return
        default_name = f"map_{self._current_id}_layer_{layer.upper()}.png"
        path, _ = QFileDialog.getSaveFileName(
            self, f"Export Layer {layer.upper()} as PNG",
            default_name, "PNG image (*.png)",
        )
        if not path:
            return
        png_bytes = map_import.export_rgba_to_png_bytes(
            bytes(state.base_rgba), state.width_px, state.height_px,
        )
        try:
            with open(path, "wb") as fh:
                fh.write(png_bytes)
        except OSError as exc:
            QMessageBox.critical(self, "Export PNG", f"Couldn't write file:\n{exc}")
            return

    def _on_import_layer_png(self, layer: str) -> None:
        """Replace tileset + palette + tilemap from a PNG (lossy)."""
        state = self._layer_state.get(layer)
        if state is None or self._current_id is None or self._undo_stack is None:
            QMessageBox.information(
                self, "Import PNG", "Select a map first."
            )
            return
        path, _ = QFileDialog.getOpenFileName(
            self, f"Import Layer {layer.upper()} from PNG",
            "", "PNG image (*.png)",
        )
        if not path:
            return
        try:
            result = map_import.import_layer_from_png(
                path,
                target_width_px=state.width_px,
                target_height_px=state.height_px,
                n_palette_banks=len(state.palettes),
                palette_trailer=state.palette_trailer,
                max_tiles=1024,
                # NDS BG 4bpp hardware treats palette index 0 as transparent
                # for EVERY bank regardless of which BG layer you're on —
                # if the quantizer drops opaque colors into slot 0, those
                # pixels render as backdrop in-game (the editor's preview
                # uses backdrop_opaque=True so it lies about Layer A).
                # Reserve slot 0 on both layers to stay hardware-safe.
                is_transparent_layer=True,
            )
        except ValueError as exc:
            QMessageBox.critical(self, "Import PNG", str(exc))
            return
        s = result.stats
        msg = (
            f"PNG → {s.cells_total} cells.\n\n"
            f"Unique 8×8 tiles (raw):       {s.unique_tiles_raw}\n"
            f"After flip-aware dedup:      {s.unique_after_flip_dedup}\n"
            f"After merge to ≤{s.max_tiles}:  {s.unique_after_merge}"
            f" {'(merged)' if s.was_reduced else ''}\n"
            f"Palette banks used:           {s.banks_used}\n\n"
            f"Replace this layer's .c / .p / .s files?"
        )
        if QMessageBox.question(
            self, f"Import Layer {layer.upper()}", msg,
            QMessageBox.Ok | QMessageBox.Cancel,
        ) != QMessageBox.Ok:
            return
        files = mapmod.MapFiles(self._current_id)
        c_path = files.layer_a_tiles if layer == "a" else files.layer_b_tiles
        p_path = files.layer_a_palette if layer == "a" else files.layer_b_palette
        s_path = files.layer_a_screen if layer == "a" else files.layer_b_screen
        map_id = self._current_id
        macro_label = f"Import PNG ({map_id} layer {layer.upper()})"
        self._undo_stack.beginMacro(macro_label)
        try:
            self._undo_stack.push(ReplaceMapFileCommand(
                self._session, c_path, result.new_c_bytes,
                f"{macro_label} (.c)",
                on_change=lambda l=layer: self._on_layer_png_replaced(l),
            ))
            self._undo_stack.push(ReplaceMapFileCommand(
                self._session, p_path, result.new_p_bytes,
                f"{macro_label} (.p)",
                on_change=lambda l=layer: self._on_layer_png_replaced(l),
            ))
            self._undo_stack.push(ReplaceMapFileCommand(
                self._session, s_path, result.new_s_bytes,
                f"{macro_label} (.s)",
                on_change=lambda l=layer: self._on_layer_png_replaced(l),
            ))
        finally:
            self._undo_stack.endMacro()

    def _on_layer_png_replaced(self, layer: str) -> None:
        """All three layer files changed — drop cached state so the next
        refresh re-parses tiles/palette/entries from session bytes.
        """
        self._layer_state[layer] = None
        tab_ix = self._TAB_LAYER_A if layer == "a" else self._TAB_LAYER_B
        if self._tabs.currentIndex() == tab_ix:
            self._refresh_active_tab()

    def _paint_cell(self, layer: str, tx: int, ty: int) -> None:
        state = self._layer_state[layer]
        entry = state.entry_value()
        cell_ix = ty * state.n_tiles_x + tx
        if state.entries[cell_ix] == entry:
            return
        state.entries[cell_ix] = entry
        mapmod.paint_tile_into_rgba(
            state.base_rgba, state.width_px, tx, ty, entry,
            state.tiles, state.palettes, backdrop_opaque=True,
        )

    def _paint_tile_line(
        self, layer: str, p0: Tuple[int, int], p1: Tuple[int, int],
    ) -> None:
        """Bresenham in tile-cell coords. Avoids dotted strokes on fast
        drags by stamping every cell the cursor crossed."""
        x0, y0 = p0
        x1, y1 = p1
        dx = abs(x1 - x0)
        dy = -abs(y1 - y0)
        sx = 1 if x0 < x1 else -1
        sy = 1 if y0 < y1 else -1
        err = dx + dy
        while True:
            self._paint_cell(layer, x0, y0)
            if x0 == x1 and y0 == y1:
                return
            e2 = 2 * err
            if e2 >= dy:
                err += dy
                x0 += sx
            if e2 <= dx:
                err += dx
                y0 += sy

    def _pick_cell(self, layer: str, tx: int, ty: int) -> None:
        """Pull an entry's tile_ix / bank / flips into the toolbar so the
        next paint stroke reuses them — Porymap's eyedropper idiom."""
        state = self._layer_state[layer]
        cell_ix = ty * state.n_tiles_x + tx
        entry = state.entries[cell_ix]
        state.selected_tile_ix = entry & 0x3FF
        state.hflip = bool(entry & 0x400)
        state.vflip = bool(entry & 0x800)
        state.selected_bank = (entry >> 12) & 0xF
        # Mirror the new state into the toolbar widgets without firing
        # their valueChanged handlers (which would recurse back into
        # state mutation).
        bank_spin: QSpinBox = getattr(self, f"_layer_{layer}_bank_spin")
        bank_spin.blockSignals(True)
        bank_spin.setValue(min(state.selected_bank, bank_spin.maximum()))
        bank_spin.blockSignals(False)
        hflip_chk: QCheckBox = getattr(self, f"_layer_{layer}_hflip_chk")
        hflip_chk.blockSignals(True)
        hflip_chk.setChecked(state.hflip)
        hflip_chk.blockSignals(False)
        vflip_chk: QCheckBox = getattr(self, f"_layer_{layer}_vflip_chk")
        vflip_chk.blockSignals(True)
        vflip_chk.setChecked(state.vflip)
        vflip_chk.blockSignals(False)
        # Picker tab uses the new bank → re-render the tile grid + preview.
        # _refresh_picker draws the new selection overlay internally.
        self._refresh_picker(layer)
        self._refresh_selected_tile_preview(layer)
        # Auto-switch to Paint after picking — mirrors most paint apps so
        # the eyedropper-then-stamp flow is one click each. Untoggling the
        # Color Pick button drives ``_on_tile_tool_chosen`` which flips
        # ``state.tool`` back to PAINT.
        color_pick_btn: QToolButton = getattr(
            self, f"_layer_{layer}_color_pick_btn",
        )
        if color_pick_btn.isChecked():
            color_pick_btn.setChecked(False)
        else:
            state.tool = _TILE_TOOL_PAINT

    def _on_tile_tool_chosen(self, layer: str, tool_id: str) -> None:
        state = self._layer_state.get(layer)
        if state is not None:
            state.tool = tool_id

    def _on_bank_changed(self, layer: str, value: int) -> None:
        state = self._layer_state.get(layer)
        if state is None:
            return
        state.selected_bank = int(value)
        # The picker only re-renders when the bank actually drives its
        # layout — i.e. show-all + filter-by-bank. In other modes the
        # selected_bank is decided by which cell the user picks, and the
        # spinner is disabled, so this branch is rarely taken.
        if state.show_all_tiles and state.filter_by_bank:
            self._refresh_picker(layer)
        else:
            self._refresh_picker_overlay(layer)
        self._refresh_selected_tile_preview(layer)

    def _on_show_all_toggled(self, layer: str, checked: bool) -> None:
        state = self._layer_state.get(layer)
        if state is None:
            return
        state.show_all_tiles = bool(checked)
        filter_chk: QCheckBox = getattr(self, f"_layer_{layer}_filter_bank_chk")
        filter_chk.setEnabled(state.show_all_tiles)
        if not state.show_all_tiles and state.filter_by_bank:
            # Filter is a sub-option of Show all — collapse it when
            # the parent toggles off so the spinner doesn't linger as
            # "enabled in a disabled mode".
            state.filter_by_bank = False
            filter_chk.blockSignals(True)
            filter_chk.setChecked(False)
            filter_chk.blockSignals(False)
        bank_spin: QSpinBox = getattr(self, f"_layer_{layer}_bank_spin")
        bank_spin.setEnabled(state.show_all_tiles and state.filter_by_bank)
        self._refresh_picker(layer)

    def _on_filter_bank_toggled(self, layer: str, checked: bool) -> None:
        state = self._layer_state.get(layer)
        if state is None:
            return
        state.filter_by_bank = bool(checked)
        bank_spin: QSpinBox = getattr(self, f"_layer_{layer}_bank_spin")
        bank_spin.setEnabled(state.show_all_tiles and state.filter_by_bank)
        self._refresh_picker(layer)

    def _on_flip_toggled(self, layer: str, axis: str, value: bool) -> None:
        state = self._layer_state.get(layer)
        if state is None:
            return
        if axis == "h":
            state.hflip = bool(value)
        else:
            state.vflip = bool(value)
        self._refresh_selected_tile_preview(layer)

    def _on_tile_hovered(self, layer: str, x: int, y: int) -> None:
        """Track which cell the cursor is over so the canvas can paint
        a soft highlight on the about-to-stamp tile. Coords arrive in
        image space (PaintCanvas divides by image scale)."""
        state = self._layer_state.get(layer)
        if state is None:
            return
        tx = x // 8
        ty = y // 8
        cell = (tx, ty)
        if state.hover_cell == cell:
            return
        state.hover_cell = cell
        self._refresh_layer_canvas(layer)

    def _on_tile_hover_left(self, layer: str) -> None:
        state = self._layer_state.get(layer)
        if state is None or state.hover_cell is None:
            return
        state.hover_cell = None
        self._refresh_layer_canvas(layer)

    def _on_zoom_changed(self, layer: str, scale: int) -> None:
        state = self._layer_state.get(layer)
        if state is None:
            return
        state.scale = max(1, int(scale))
        state.scaled_base_pixmap = None
        canvas: PaintCanvas = getattr(self, f"_layer_{layer}_canvas")
        canvas.setImageScale(state.scale)
        self._refresh_layer_canvas(layer)

    def _on_zoom_step(self, layer: str, steps: int) -> None:
        """Ctrl+wheel step through ``_ZOOM_LEVELS`` on the layer canvas."""
        if steps == 0:
            return
        state = self._layer_state.get(layer)
        if state is None:
            return
        try:
            cur = _ZOOM_LEVELS.index(state.scale)
        except ValueError:
            cur = 0
        new = max(0, min(len(_ZOOM_LEVELS) - 1, cur + steps))
        if new != cur:
            self._on_zoom_changed(layer, _ZOOM_LEVELS[new])

    def _on_picker_zoom_step(self, layer: str, steps: int) -> None:
        """Ctrl+wheel on the picker steps the per-state ``picker_scale``.

        Re-rendering the picker is cheap (one QPainter pass over a few
        hundred 8×8 tiles); the cell pixel size grows / shrinks and the
        column count is recomputed from the viewport, so zooming out
        widens each row instead of shrinking the picker.
        """
        state = self._layer_state.get(layer)
        if state is None or steps == 0:
            return
        new = max(_PICKER_SCALE_MIN, min(_PICKER_SCALE_MAX, state.picker_scale + steps))
        if new != state.picker_scale:
            state.picker_scale = new
            self._refresh_picker(layer)

    def _on_picker_viewport_resized(self, layer: str) -> None:
        """Splitter drag (or window resize) changes the picker viewport
        width — reflow the cell grid so the new width is used.
        """
        if self._layer_state.get(layer) is None:
            return
        self._refresh_picker(layer)

    def _on_pan(self, layer: str, dx: int, dy: int) -> None:
        """Right-drag pan — shift the canvas QScrollArea's scrollbars by
        (-dx, -dy) so the content tracks the cursor (Photoshop "hand").
        """
        scroll: QScrollArea = getattr(self, f"_layer_{layer}_canvas_scroll")
        h = scroll.horizontalScrollBar()
        v = scroll.verticalScrollBar()
        if dx:
            h.setValue(h.value() - dx)
        if dy:
            v.setValue(v.value() - dy)

    # ---- Selection / tab change -----------------------------------------

    def _on_index_selected(self, ix: int) -> None:
        if not (0 <= ix < len(self._map_ids)):
            return
        map_id = self._map_ids[ix]
        self._current_id = map_id
        self._session.remember_selection(self._CURSOR_KEY, int(map_id))
        # Drop any in-progress paint stroke — switching maps invalidates
        # the bits being edited, and the composite cache is per-map.
        self._walk_active_bits = None
        self._walk_last_pos = None
        self._walk_composite_cache = None
        # Same deal for the tilemap painter — the state holds tile/palette
        # buffers tied to this map; the layer tab refresh will rebuild.
        self._layer_state["a"] = None
        self._layer_state["b"] = None
        self._update_tab_availability(map_id)
        self._update_metadata_and_tuples(map_id)
        self._refresh_active_tab()

    def _on_tab_changed(self, _ix: int) -> None:
        self._refresh_active_tab()

    def _update_tab_availability(self, map_id: str) -> None:
        files = mapmod.MapFiles(map_id)
        has_b = (
            files.layer_b_screen in self._file_table
            and files.layer_b_tiles in self._file_table
            and files.layer_b_palette in self._file_table
        )
        has_walk = files.walkability in self._file_table
        self._tabs.setTabEnabled(self._TAB_LAYER_B, has_b)
        self._tabs.setTabEnabled(self._TAB_WALK, has_walk)
        if not self._tabs.isTabEnabled(self._tabs.currentIndex()):
            self._tabs.blockSignals(True)
            self._tabs.setCurrentIndex(self._TAB_LAYER_A)
            self._tabs.blockSignals(False)

    # ---- Render dispatch -------------------------------------------------

    def _refresh_active_tab(self) -> None:
        if self._current_id is None:
            return
        ix = self._tabs.currentIndex()
        try:
            preview = self._render_for_tab(ix)
        except (ValueError, KeyError) as e:
            self._set_label_text(ix, f"Render failed: {e}")
            return
        # Layer A/B tabs manage their own pixmaps inside _render_for_tab
        # (tile picker + canvas update via their own pinned buffers), so
        # a None return there is a normal control-flow signal, not empty.
        if ix in (self._TAB_LAYER_A, self._TAB_LAYER_B):
            return
        if preview is None or preview.width == 0 or preview.height == 0:
            self._set_label_text(ix, "(empty)")
            return
        self._set_pixmap_on_tab(ix, preview)

    def _render_for_tab(self, ix: int) -> Optional[mapmod.MapPreview]:
        map_id = self._current_id
        files = mapmod.MapFiles(map_id)
        if ix == self._TAB_LAYER_A:
            self._ensure_layer_state("a")
            self._refresh_layer_canvas("a")
            self._refresh_picker("a")
            return None  # handled directly via per-layer pinned buffers
        if ix == self._TAB_LAYER_B:
            if files.layer_b_screen not in self._file_table:
                return None
            self._ensure_layer_state("b")
            self._refresh_layer_canvas("b")
            self._refresh_picker("b")
            return None
        composite = mapmod.render_map_from_file_table(
            map_id, self._file_table, self._rom,
        )
        if ix == self._TAB_WALK:
            # Cache the composite for the lifetime of this selection so
            # subsequent paint events re-tint only — the layer A/B render
            # is the expensive step.
            self._walk_composite_cache = composite
            if files.walkability not in self._file_table:
                return composite
            walk_raw = self._session.map_file_bytes(files.walkability)
            ww, wh, bits = mapmod.parse_walkability(walk_raw)
            return mapmod.apply_walkability_overlay(composite, bits, ww, wh)
        return composite

    # ---- Metadata + tuple table -----------------------------------------

    def _update_metadata_and_tuples(self, map_id: str) -> None:
        files = mapmod.MapFiles(map_id)
        # Layer A dimensions — every map has one, so this read can't fail.
        wa, ha, _ = mapmod.parse_screen(self._slice(files.layer_a_screen))
        self._meta_size.setText(f"{wa}\u00d7{ha}")

        if files.layer_b_screen in self._file_table:
            wb, hb, _ = mapmod.parse_screen(self._slice(files.layer_b_screen))
            self._meta_layer_b.setText(f"{wb}\u00d7{hb}")
        else:
            self._meta_layer_b.setText("(none)")

        banks_a, _ = mapmod.parse_palette(self._slice(files.layer_a_palette))
        if files.layer_b_palette in self._file_table:
            banks_b, _ = mapmod.parse_palette(self._slice(files.layer_b_palette))
            self._meta_palette.setText(f"A: {len(banks_a)}  B: {len(banks_b)}")
        else:
            self._meta_palette.setText(f"A: {len(banks_a)}")

        if files.walkability in self._file_table:
            ww, hw, bits = mapmod.parse_walkability(
                self._session.map_file_bytes(files.walkability)
            )
            # Blocked-fraction is the at-a-glance summary that maps
            # cleanly to "how much of the map can you walk on".
            n = ww * hw
            blocked = sum(bin(b).count("1") for b in bits[: (n + 7) // 8])
            pct = (blocked * 100) // max(n, 1)
            self._meta_walk.setText(f"{ww}\u00d7{hw}  ({pct}% blocked)")
        else:
            self._meta_walk.setText("(none)")

        desc = mapmod.parse_descriptor(self._slice(files.descriptor))
        used = desc.used_tuples()
        self._meta_tuples.setText(f"{len(used)} / {mapmod.DESCRIPTOR_MAX_TUPLES}")

        self._populate_tuples_table(desc)

    def _populate_tuples_table(self, desc: mapmod.MapDescriptor) -> None:
        for row, tup in enumerate(desc.tuples):
            is_empty = tup == mapmod.EMPTY_TUPLE
            for col, val in enumerate(tup):
                item = QTableWidgetItem("\u2014" if is_empty else str(val))
                item.setTextAlignment(Qt.AlignCenter)
                if is_empty:
                    item.setForeground(Qt.gray)
                self._tuples_table.setItem(row, col, item)

    # ---- Helpers ---------------------------------------------------------

    def _slice(self, path: str) -> bytes:
        return self._file_table.slice(self._rom, path)

    def _label_for_tab(self, ix: int) -> QLabel:
        # Layer tabs use a different pixmap path (per-layer canvas +
        # picker, not a single label). _set_label_text routes through
        # the layer canvas so the error message lands somewhere visible.
        return {
            self._TAB_COMPOSITE: self._composite_label,
            self._TAB_WALK: self._walk_label,
            self._TAB_LAYER_A: self._layer_a_canvas,
            self._TAB_LAYER_B: self._layer_b_canvas,
        }[ix]

    def _set_label_text(self, ix: int, text: str) -> None:
        label = self._label_for_tab(ix)
        label.setPixmap(QPixmap())
        label.setText(text)

    def _set_pixmap_on_tab(self, ix: int, preview: mapmod.MapPreview) -> None:
        # Pin per-tab so the QImage's backing buffer outlives the pixmap.
        self._pinned_rgba[ix] = preview.rgba
        image = QImage(
            self._pinned_rgba[ix],
            preview.width,
            preview.height,
            preview.width * 4,
            QImage.Format_RGBA8888,
        )
        label = self._label_for_tab(ix)
        label.setText("")
        label.setPixmap(QPixmap.fromImage(image))
        label.adjustSize()
