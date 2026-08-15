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

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import QPoint, QRect, QSize, Qt, QSignalBlocker, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QImage, QPainter, QPen, QPixmap, QUndoStack
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QCompleter,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from digimon_core import map as mapmod, map_import, map_labels, overlay5 as overlay5_mod

from ..commands import (
    EditDialogFieldCommand,
    EditExitBoxCommand,
    EditExitDestinationCommand,
    EditExitSpawnArgCommand,
    EditOverworldSpriteBehaviorCommand,
    EditOverworldSpriteIdCommand,
    MoveOverworldSpriteCommand,
    ReplaceMapFileCommand,
    SetAttrCommand,
)
from .cutscenes_tab import CutscenesTab
from .form_helpers import build_editor_footer, io_button_column
from .map_encounter_tab import MapEncounterTab
from .events_canvas import EventMarkerSpec, EventsCanvas, ExitZoneSpec
from .paint_canvas import PaintCanvas
from .record_list_panel import RecordListPanel


_BRUSH_SIZES = (1, 4, 8, 16, 32)
# Tile-paint brush footprints: N×N tiles stamped/picked at once (1, 4, 9, 16
# tiles). Distinct from the walkability pixel brush above.
_TILE_BRUSH_SIZES = (1, 2, 3, 4)
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
# Per-cell display size (px) for the N×N brush composer preview — big enough
# that individual sub-cells are comfortably clickable at a 4×4 brush.
_BRUSH_CELL_PX = 30

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

# Events sidebar row-type tag (Qt.UserRole+1 on each list item). Lets a
# single QListWidget mix sprite / exit / spawn rows and still route the
# selection to the right form page on click.
_EVT_ROW_SPRITE = "sprite"
_EVT_ROW_EXIT = "exit"
_EVT_ROW_SPAWN = "spawn"
_EVT_ROW_HITBOX = "hitbox"
_EVT_ROW_TYPE_ROLE = Qt.UserRole + 1

# Events sidebar stacked-form page indices.
_EVT_PAGE_EMPTY = 0
_EVT_PAGE_SPRITE = 1
_EVT_PAGE_EXIT = 2
_EVT_PAGE_SPAWN = 3
_EVT_PAGE_HITBOX = 4

# u32 fields (spawn_arg, destination file offset) clamp into the QSpinBox
# int32 range. Vanilla values stay well under this; if someone authors a
# real u32 > 0x7FFFFFFF we'll widen to a hex line edit then.
_U32_SPINBOX_MAX = 0x7FFFFFFF

# "Custom (raw offset)" sentinel value in the destination combo. The
# integer can't collide with a real entry_ix because entry_starts caps at
# the overlay payload size (well under 1B).
_DEST_COMBO_CUSTOM = -1


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
    # N×N tile-brush footprint (1..4). Painting stamps this many tiles
    # anchored top-left at the clicked cell; color-picking captures this
    # many into ``brush_entries``.
    brush_size: int = 1
    # N×N brush pattern (row-major, length brush_size²). ``None`` only in
    # 1×1 mode, where the footprint is just the toolbar tile. For N>1 it's
    # always populated so each cell can be composed independently in the
    # sidebar; a map color-pick overwrites the whole pattern.
    brush_entries: Optional[List[int]] = None
    # Active sub-cell (0..brush_size²-1) the sidebar composer edits.
    brush_sel: int = 0
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

    def brush_cell(self, i: int, j: int) -> int:
        """Tilemap entry for footprint position ``(i, j)`` (0-based, i=col).

        Single-tile mode (``brush_entries is None``) repeats the toolbar
        tile; captured mode indexes the row-major pattern, falling back to
        the toolbar tile if the index is somehow out of range.
        """
        if self.brush_entries is None:
            return self.entry_value()
        idx = j * self.brush_size + i
        if 0 <= idx < len(self.brush_entries):
            return self.brush_entries[idx]
        return self.entry_value()


@dataclass
class _ExitFormData:
    """Sidebar-side resolved view of one 0x001b block.

    Wraps the canvas-side :class:`ExitZoneSpec` with the extra info the
    sidebar needs to commit edits: the exit handler's location (entry +
    rel offset, often inside the current entry but not always), the
    handler's current op 0x0030 destination, and the resolved
    destination map_id (None when the dest doesn't land on a known
    entry start).

    Spawn-point blocks (degenerate zones with dst_file_off=0) have
    ``handler_entry_ix == -1`` and skip every handler-side field.
    """
    block_offset: int
    idx: int
    x1: int
    y1: int
    x2: int
    y2: int
    is_spawn: bool
    dst_file_off: int = 0
    handler_entry_ix: int = -1
    handler_rel_offset: int = -1
    handler_dest: int = 0
    handler_spawn_arg: int = 0
    dest_map_id: Optional[int] = None
    dest_label: str = ""
    # Per-type sequential index for display ("Exit 0", "Hitbox 0",
    # "Spawn 0", "Exit 1", …). Independent of the underlying block
    # ``idx`` so the sidebar + canvas labels stay dense even when the
    # script interleaves the three types.
    display_idx: int = 0
    # True when the block is an interaction trigger (non-standard
    # handler — talking to an unreachable NPC, cutscene fire). Painted
    # in the canvas's hitbox color and surfaced read-only by the
    # sidebar: the editor doesn't fully understand the handler shape
    # so a destination/spawn-arg edit could silently break the script.
    is_hitbox: bool = False


class _BrushPreview(QLabel):
    """Clickable N×N brush preview.

    Shows the rendered brush footprint; for N>1 it overlays grid lines and
    a highlight on the active cell and emits ``cellClicked(flat_index)`` when
    a sub-cell is clicked, so the brush can be composed tile-by-tile in the
    sidebar. For 1×1 it's an inert preview.
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

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.LeftButton or self._n <= 1:
            super().mousePressEvent(event)
            return
        w = max(1, self.width())
        h = max(1, self.height())
        col = min(self._n - 1, max(0, int(event.position().x()) * self._n // w))
        row = min(self._n - 1, max(0, int(event.position().y()) * self._n // h))
        self.cellClicked.emit(row * self._n + col)

    def paintEvent(self, event) -> None:
        super().paintEvent(event)  # draws the pixmap
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
            painter.drawRect(
                round(sc * cw) + 1, round(sr * ch) + 1,
                round(cw) - 2, round(ch) - 2,
            )
        painter.end()


class MapBrowser(QWidget):
    """Viewer for ``DAT/map/`` field maps.

    Phase B: read-only preview tabs + per-map metadata + ``.d`` tuple
    table. Selection state is persisted on the session under the
    ``map_browser`` cursor key.
    """

    _CURSOR_KEY = "map_browser"

    # Two id spaces, deliberately decoupled:
    #
    # ``_TAB_*`` are *logical render ids* — the keys used by ``_render_for_tab``,
    # ``_label_for_tab``, ``_pinned_rgba`` and the layer-refresh guards. Their
    # numeric values are historical and no longer track tab positions.
    #
    # Layer A / Layer B / Composite are NOT separate top-level tabs anymore:
    # they're the three views of the unified first "Map" tab, switched by a
    # View radio (``_map_view``). ``_active_render_id`` maps the current
    # physical tab (+ the Map tab's active view) back onto a logical id.
    _TAB_LAYER_A = 0
    _TAB_LAYER_B = 1
    _TAB_WALK = 2
    _TAB_EVENTS = 3
    _TAB_CUTSCENES = 4
    _TAB_ENCOUNTERS = 5
    _TAB_COMPOSITE = 6

    # Physical QTabWidget positions.
    _REAL_TAB_MAP = 0
    _REAL_TAB_WALK = 1
    _REAL_TAB_EVENTS = 2
    _REAL_TAB_CUTSCENES = 3
    _REAL_TAB_ENCOUNTERS = 4

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
        # Scaled QPixmap of the current overlay-applied preview — rebuilt
        # only when the underlying MapPreview changes or zoom changes.
        # Hover/stroke previews paint a brush rect onto a copy each tick.
        self._walk_scaled_base_pixmap: Optional[QPixmap] = None
        self._walk_current_preview: Optional[mapmod.MapPreview] = None
        # (map_id, w, h) cache for the walkability grid dims — constant for a
        # map, but was being re-parsed from the FAT on every hover/paint
        # sample (the paint brush called it per Bresenham step). Keyed by
        # map id so it self-invalidates on selection change.
        self._walk_dims_cache: Optional[Tuple[int, int, int]] = None
        self._walk_zoom: int = 1
        # Cursor in image-space px (None when off-canvas). Drives both
        # the hover preview and the live stroke indicator.
        self._walk_hover_pos: Optional[Tuple[int, int]] = None

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
        # Row label format: ``NNNN  Area Name`` (with the area name
        # muted-elided when the id is past the vanilla 265). Sourced
        # from :mod:`digimon_core.map_labels` — same table the enemy
        # editor's "Appears in" section uses, so a map id reads
        # identically in both views.
        def _map_row_label(_ix, mid) -> str:
            map_id = int(mid)
            name = map_labels.area_name(map_id)
            if name and name != "?":
                return f"{map_id:04d}  {name}"
            return f"{map_id:04d}"

        self._list = RecordListPanel(
            records=list(self._map_ids),
            label_for=_map_row_label,
        )
        self._list.indexSelected.connect(self._on_index_selected)

        self._tabs = QTabWidget()
        self._tabs.addTab(self._build_map_tab(), "Map")
        self._tabs.addTab(self._build_walk_tab(), "Walkability")
        # The Events tab is dormant (hidden, superseded by Events/Cutscenes).
        # A QTabWidget's minimum size is the max over ALL pages — hidden ones
        # included — so its wide sidebar was pinning the whole browser's
        # minimum width. Scroll-wrap it (non-resizable → small floor) so a
        # dormant page can't hold the window hostage on small screens.
        self._tabs.addTab(
            self._shrink_wrap(self._build_events_tab()), "Events")
        self._tabs.addTab(self._build_cutscenes_tab(), "Events/Cutscenes")
        self._tabs.addTab(self._build_encounter_tab(), "Encounters")
        # The Events tab is superseded by the Events/Cutscenes tab (its
        # objects/dialogs are all reachable there). Hide the tab button but
        # keep the widget built + wired — dormant, not deleted — so the code
        # path stays live and can be re-surfaced if needed.
        if hasattr(self._tabs, "setTabVisible"):
            self._tabs.setTabVisible(self._REAL_TAB_EVENTS, False)
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

        # Metadata footer under the tabs — the shared graphics-editor footer
        # idiom (Import/Export io column + details form flowing in a wrapping,
        # vertically-compressible strip). Cutscenes/Encounters tabs hide the
        # whole strip via ``self._meta_footer.setVisible(False)``.
        self._map_import_btn = QPushButton("Import…")
        self._map_import_btn.setMenu(self._build_map_import_menu())
        self._map_import_btn.setEnabled(self._undo_stack is not None)
        self._map_export_btn = QPushButton("Export…")
        self._map_export_btn.setMenu(self._build_map_export_menu())
        io_panel = io_button_column(self._map_import_btn, self._map_export_btn)
        details_panel = QWidget()
        details_panel.setLayout(meta_form)
        self._meta_footer = build_editor_footer([io_panel, details_panel])
        self._sync_map_io_actions()

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addWidget(self._tabs, 1)
        right_layout.addWidget(self._meta_footer, 0)

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
        # Small floor + non-resizable scroll so the preview can shrink
        # below the rendered pixmap and scroll, instead of pinning the
        # whole Map tab wide on small screens. ``_set_pixmap_on_tab``
        # calls ``adjustSize()`` so the label tracks the pixmap.
        label.setMinimumSize(160, 120)
        scroll = QScrollArea()
        scroll.setWidget(label)
        scroll.setWidgetResizable(False)
        scroll.setAlignment(Qt.AlignCenter)

        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.addWidget(scroll, 1)
        setattr(self, f"_{key}_label", label)
        return page

    def _build_map_tab(self) -> QWidget:
        """Unified field-map visualiser.

        Left: a canvas stack (Layer A painter canvas / Layer B painter canvas /
        composite preview). Right: ONE shared sidebar — a ``View`` selector on
        top, then a controls stack holding the active layer's paint tools
        (tile picker, flips, brush …). Switching View swaps both stacks in
        lockstep, so the layer painters read as views of one surface with a
        single sidebar rather than each carrying its own. Every ``layer``-keyed
        handler keeps working untouched.
        """
        a_canvas, a_controls = self._build_layer_paint_tab("a")
        b_canvas, b_controls = self._build_layer_paint_tab("b")

        self._canvas_stack = QStackedWidget()
        self._canvas_stack.addWidget(a_canvas)                             # 0
        self._canvas_stack.addWidget(b_canvas)                            # 1
        self._canvas_stack.addWidget(self._build_preview_tab("composite"))  # 2

        # Controls stack — the active layer's paint tools. Composite has none,
        # so it gets an empty placeholder (the View selector stays visible).
        self._controls_stack = QStackedWidget()
        self._controls_stack.addWidget(a_controls)  # 0
        self._controls_stack.addWidget(b_controls)  # 1
        self._controls_stack.addWidget(QWidget())   # 2 (composite: no tools)

        view_box = QGroupBox("View")
        vb = QVBoxLayout(view_box)
        vb.setContentsMargins(8, 6, 8, 6)
        vb.setSpacing(2)
        self._map_view_group = QButtonGroup(self)
        self._view_a = QRadioButton("Layer A")
        self._view_b = QRadioButton("Layer B")
        self._view_comp = QRadioButton("Composite")
        for rb, key in (
            (self._view_a, "a"), (self._view_b, "b"), (self._view_comp, "composite"),
        ):
            self._map_view_group.addButton(rb)
            rb.toggled.connect(
                lambda checked, k=key: self._on_map_view_changed(k) if checked else None
            )
            vb.addWidget(rb)

        # One right sidebar: View selector on top, the active layer's paint
        # controls below it.
        sidebar = QWidget()
        sl = QVBoxLayout(sidebar)
        sl.setContentsMargins(6, 6, 6, 6)
        sl.setSpacing(8)
        sl.addWidget(view_box, 0)
        sl.addWidget(self._controls_stack, 1)

        # Layer A is the default view — it's the primary editing surface.
        self._map_view = "a"
        self._view_a.setChecked(True)
        self._canvas_stack.setCurrentIndex(0)
        self._controls_stack.setCurrentIndex(0)

        split = QSplitter(Qt.Horizontal)
        split.addWidget(self._canvas_stack)
        split.addWidget(sidebar)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 0)
        split.setCollapsible(0, False)
        split.setSizes([820, 250])

        page = QWidget()
        pl = QVBoxLayout(page)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.setSpacing(0)
        pl.addWidget(split, 1)
        return page

    _MAP_VIEW_STACK_IX = {"a": 0, "b": 1, "composite": 2}
    _MAP_VIEW_RENDER_ID = {
        "a": _TAB_LAYER_A, "b": _TAB_LAYER_B, "composite": _TAB_COMPOSITE,
    }
    _REAL_TAB_RENDER_ID = {
        _REAL_TAB_WALK: _TAB_WALK,
        _REAL_TAB_EVENTS: _TAB_EVENTS,
        _REAL_TAB_CUTSCENES: _TAB_CUTSCENES,
        _REAL_TAB_ENCOUNTERS: _TAB_ENCOUNTERS,
    }

    def _on_map_view_changed(self, key: str) -> None:
        self._map_view = key
        ix = self._MAP_VIEW_STACK_IX[key]
        self._canvas_stack.setCurrentIndex(ix)
        self._controls_stack.setCurrentIndex(ix)
        self._sync_map_io_actions()
        self._refresh_active_tab()

    def _active_render_id(self) -> int:
        """Logical render id for whatever is currently on screen.

        On the Map tab this is the active View's id; elsewhere it's the
        physical tab mapped onto its logical id.
        """
        if self._tabs.currentIndex() == self._REAL_TAB_MAP:
            return self._MAP_VIEW_RENDER_ID[self._map_view]
        return self._REAL_TAB_RENDER_ID.get(
            self._tabs.currentIndex(), self._TAB_COMPOSITE,
        )

    def _shrink_wrap(self, inner: QWidget) -> QScrollArea:
        """Wrap a page in a non-resizable scroll so its (possibly wide)
        content can't pin the QTabWidget's minimum size — it h/v-scrolls
        below its natural size instead. For dormant/secondary pages only;
        primary surfaces keep ``widgetResizable(True)`` to fill the view.
        """
        scroll = QScrollArea()
        scroll.setWidget(inner)
        scroll.setWidgetResizable(False)
        scroll.setFrameShape(QScrollArea.NoFrame)
        return scroll

    # ---- Footer Import / Export (PNG + native .c/.p/.s) ------------------

    def _active_map_layer(self) -> str:
        """Which layer the footer I/O acts on. Composite has no single
        layer — default to A so a native export still has a target."""
        return "b" if self._map_view == "b" else "a"

    def _map_view_is_layer(self) -> bool:
        return self._map_view in ("a", "b")

    def _layer_triple(self, layer: str) -> Dict[str, str]:
        files = mapmod.MapFiles(self._current_id)
        if layer == "b":
            return {"c": files.layer_b_tiles, "p": files.layer_b_palette,
                    "s": files.layer_b_screen}
        return {"c": files.layer_a_tiles, "p": files.layer_a_palette,
                "s": files.layer_a_screen}

    def _build_map_export_menu(self) -> QMenu:
        menu = QMenu(self)
        menu.addAction("PNG (current view)…").triggered.connect(
            self._on_map_export_png)
        menu.addAction("Native files (.c / .p / .s)…").triggered.connect(
            self._on_map_export_native)
        return menu

    def _build_map_import_menu(self) -> QMenu:
        menu = QMenu(self)
        self._act_map_import_png = menu.addAction("PNG → selected layer…")
        self._act_map_import_png.triggered.connect(
            lambda: self._on_import_layer_png(self._active_map_layer()))
        menu.addSeparator()
        self._act_map_import_c = menu.addAction("Replace tiles (.c)…")
        self._act_map_import_c.triggered.connect(
            lambda: self._on_map_import_native("c"))
        self._act_map_import_p = menu.addAction("Replace palette (.p)…")
        self._act_map_import_p.triggered.connect(
            lambda: self._on_map_import_native("p"))
        self._act_map_import_s = menu.addAction("Replace screen (.s)…")
        self._act_map_import_s.triggered.connect(
            lambda: self._on_map_import_native("s"))
        return menu

    def _sync_map_io_actions(self) -> None:
        """Native/PNG imports need a concrete layer; disable them on the
        Composite view (and when there's no undo stack)."""
        if not hasattr(self, "_act_map_import_png"):
            return
        editable = self._map_view_is_layer() and self._undo_stack is not None
        for act in (self._act_map_import_png, self._act_map_import_c,
                    self._act_map_import_p, self._act_map_import_s):
            act.setEnabled(editable)

    def _on_map_export_png(self) -> None:
        if self._current_id is None:
            return
        if self._map_view_is_layer():
            layer = self._active_map_layer()
            self._ensure_layer_state(layer)
            self._on_export_layer_png(layer)
            return
        # Composite view: render + export the A+B composite.
        map_id = self._current_id
        try:
            comp = mapmod.render_map_from_file_table(
                map_id, self._file_table, self._rom)
        except (ValueError, KeyError) as exc:
            QMessageBox.critical(self, "Export PNG", str(exc))
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export composite as PNG",
            f"map_{map_id}_composite.png", "PNG image (*.png)")
        if not path:
            return
        png = map_import.export_rgba_to_png_bytes(
            bytes(comp.rgba), comp.width, comp.height)
        try:
            with open(path, "wb") as fh:
                fh.write(png)
        except OSError as exc:
            QMessageBox.critical(self, "Export PNG", f"Couldn't write file:\n{exc}")

    def _on_map_export_native(self) -> None:
        if self._current_id is None:
            return
        layer = self._active_map_layer()
        triple = self._layer_triple(layer)
        folder = QFileDialog.getExistingDirectory(
            self, "Export native files to folder")
        if not folder:
            return
        wrote = []
        for ext, path in triple.items():
            if path in self._file_table:
                data = self._session.map_file_bytes(path)
                name = f"{self._current_id}{layer}.{ext}"
                with open(os.path.join(folder, name), "wb") as fh:
                    fh.write(data)
                wrote.append(name)
        QMessageBox.information(
            self, "Export complete",
            "Wrote:\n" + "\n".join(wrote) if wrote else "Nothing to write.")

    def _on_map_import_native(self, ext: str) -> None:
        if self._current_id is None or self._undo_stack is None:
            return
        layer = self._active_map_layer()
        target = self._layer_triple(layer)[ext]
        if target not in self._file_table:
            QMessageBox.warning(
                self, "No such component",
                f"This map has no .{ext} for layer {layer.upper()}.")
            return
        path, _ = QFileDialog.getOpenFileName(
            self, f"Import .{ext} for layer {layer.upper()}",
            "", f"Map {ext} file (*.{ext} *.bin);;All files (*)")
        if not path:
            return
        with open(path, "rb") as fh:
            raw = fh.read()
        self._undo_stack.push(ReplaceMapFileCommand(
            self._session, target, raw,
            f"Import .{ext} → {target.rsplit('/', 1)[-1]}",
            on_change=lambda l=layer: self._on_layer_png_replaced(l)))

    def _build_layer_paint_tab(self, layer: str) -> Tuple[QScrollArea, QWidget]:
        """Porymap-style tilemap painter for one layer.

        Returns ``(canvas_scroll, controls)``: the layer canvas in a
        QScrollArea (zoom-aware, right-drag panning, Ctrl+wheel zoom), and a
        controls column grouping the *selected tile preview* (image + label +
        H/V flip), the *palette bank* spinner, and the *tile picker*. The Map
        tab stacks the canvases behind one shared right sidebar (View selector
        + these controls), so the two aren't bundled into a private per-page
        splitter anymore.
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
        # Floor kept small so the canvas scroll can shrink on small screens;
        # the real size comes from the rendered pixmap after a map loads.
        layer_canvas.setMinimumSize(160, 120)
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

        # Brush size: stamp / pick an N×N block instead of a single tile.
        brush_combo = QComboBox()
        for n in _TILE_BRUSH_SIZES:
            brush_combo.addItem(f"{n}×{n}  ({n * n} tiles)", n)
        brush_combo.setToolTip(
            "Tiles painted (and color-picked) at once, anchored top-left at "
            "the clicked cell. Pick from the map with a large brush to copy a "
            "block; pick a single tile from the picker to fill the block solid."
        )
        brush_combo.currentIndexChanged.connect(
            lambda _ix, l=layer, c=brush_combo: self._on_tile_brush_size_changed(
                l, c.currentData(),
            )
        )

        # Brush preview + caption + flips, stacked vertically: the preview
        # grows with the brush size (up to 4×4), so keeping the caption and
        # flip row below it — not beside it — stops them colliding in the
        # narrow right column.
        preview_row = QVBoxLayout()
        preview_row.setContentsMargins(0, 0, 0, 0)
        preview_lbl = _BrushPreview()
        preview_lbl.setFixedSize(_PREVIEW_PX, _PREVIEW_PX)
        preview_lbl.setAlignment(Qt.AlignCenter)
        preview_lbl.setStyleSheet(
            "background: #1d1d1d; border: 1px solid #555;"
        )
        preview_lbl.setToolTip(
            "Brush preview. With a 2×2–4×4 brush, click a cell to select it, "
            "then pick a tile / toggle flips to set just that cell."
        )
        preview_lbl.cellClicked.connect(
            lambda idx, l=layer: self._on_brush_cell_clicked(l, idx)
        )
        preview_row.addWidget(preview_lbl, 0, Qt.AlignLeft)

        sel_block = QVBoxLayout()
        sel_block.setContentsMargins(0, 0, 0, 0)
        sel_lbl = QLabel("Selected tile: \u2014")
        sel_lbl.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        sel_lbl.setWordWrap(True)
        sel_block.addWidget(sel_lbl)
        flip_row = QHBoxLayout()
        flip_row.setContentsMargins(0, 0, 0, 0)
        hflip_chk = QCheckBox("H-flip")
        hflip_chk.setToolTip(
            "1×1 brush: mirror the selected tile horizontally.\n"
            "N×N brush: mirror the whole selection — every tile flips and "
            "swaps to its opposing column (a full horizontal flip)."
        )
        hflip_chk.toggled.connect(
            lambda v, l=layer: self._on_flip_toggled(l, "h", v)
        )
        flip_row.addWidget(hflip_chk)
        vflip_chk = QCheckBox("V-flip")
        vflip_chk.setToolTip(
            "1×1 brush: mirror the selected tile vertically.\n"
            "N×N brush: mirror the whole selection — every tile flips and "
            "swaps to its opposing row (a full vertical flip)."
        )
        vflip_chk.toggled.connect(
            lambda v, l=layer: self._on_flip_toggled(l, "v", v)
        )
        flip_row.addWidget(vflip_chk)
        flip_row.addStretch(1)
        sel_block.addLayout(flip_row)
        preview_row.addLayout(sel_block)

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

        brush_row = QHBoxLayout()
        brush_row.setContentsMargins(0, 0, 0, 0)
        brush_row.addWidget(QLabel("Brush:"))
        brush_row.addWidget(brush_combo)
        brush_row.addStretch(1)

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
        right_col_layout.addLayout(brush_row)
        right_col_layout.addLayout(color_pick_row)
        right_col_layout.addWidget(picker_scroll, 1)

        # Canvas and the preview+picker control column are returned separately
        # so the Map tab can stack the canvases behind one shared right sidebar
        # (View selector on top + the active layer's controls) rather than
        # bundling a private sidebar per page. PNG/native round-trip lives in
        # the shared footer's Import/Export dropdowns.
        #
        # Stash widget refs per layer so event handlers can mutate them.
        setattr(self, f"_layer_{layer}_picker_canvas", picker_canvas)
        setattr(self, f"_layer_{layer}_picker_scroll", picker_scroll)
        setattr(self, f"_layer_{layer}_canvas", layer_canvas)
        setattr(self, f"_layer_{layer}_canvas_scroll", canvas_scroll)
        setattr(self, f"_layer_{layer}_bank_spin", bank_spin)
        setattr(self, f"_layer_{layer}_hflip_chk", hflip_chk)
        setattr(self, f"_layer_{layer}_vflip_chk", vflip_chk)
        setattr(self, f"_layer_{layer}_color_pick_btn", color_pick_btn)
        setattr(self, f"_layer_{layer}_brush_combo", brush_combo)
        setattr(self, f"_layer_{layer}_sel_lbl", sel_lbl)
        setattr(self, f"_layer_{layer}_preview_lbl", preview_lbl)
        setattr(self, f"_layer_{layer}_show_all_chk", show_all_chk)
        setattr(self, f"_layer_{layer}_filter_bank_chk", filter_bank_chk)
        return canvas_scroll, right_col

    def _build_walk_tab(self) -> QWidget:
        """Walkability tab: paint canvas (left) + tool sidebar (right).

        Canvas is a :class:`PaintCanvas` — mouse press/drag map to
        image-space pixel coordinates, Ctrl+wheel zooms, right-drag pans,
        and ``hovered`` drives the brush-footprint preview. Tools live in
        a right-side sidebar (Porymap-style) so the canvas gets the full
        vertical extent.
        """
        self._walk_label = PaintCanvas()
        self._walk_label.setText("Select a field map to preview.")
        self._walk_label.setAlignment(Qt.AlignCenter)
        self._walk_label.setMinimumSize(512, 384)
        self._walk_label.setHoverEnabled(True)
        self._walk_label.painted.connect(self._on_walk_painted)
        self._walk_label.paintFinished.connect(self._on_walk_paint_finished)
        self._walk_label.hovered.connect(self._on_walk_hovered)
        self._walk_label.hoverLeft.connect(self._on_walk_hover_left)
        self._walk_label.zoomStepRequested.connect(self._on_walk_zoom_step)
        self._walk_label.panRequested.connect(self._on_walk_pan)

        # Canvas in a scroll-area; widgetResizable=False so zoom can grow
        # the canvas past the viewport (matches the layer-paint pattern).
        self._walk_canvas_scroll = QScrollArea()
        self._walk_canvas_scroll.setWidget(self._walk_label)
        self._walk_canvas_scroll.setWidgetResizable(False)
        self._walk_canvas_scroll.setAlignment(Qt.AlignCenter)

        # Tool sidebar — three tools, brush, snap-to-tile, zoom indicator.
        self._walk_tool_group = QButtonGroup(self)
        self._walk_tool_group.setExclusive(True)
        tool_box = QVBoxLayout()
        tool_box.setContentsMargins(0, 0, 0, 0)
        tool_box.addWidget(QLabel("<b>Tool</b>"))
        for tool_id, label, tip in (
            (_TOOL_BLOCK, "Block", "Paint blocked (red) tiles."),
            (_TOOL_WALKABLE, "Walkable", "Paint walkable (clear) tiles."),
            (_TOOL_PICKER, "Pick", "Click a tile to switch to its current tool."),
        ):
            btn = QToolButton()
            btn.setText(label)
            btn.setToolTip(tip)
            btn.setCheckable(True)
            btn.setToolButtonStyle(Qt.ToolButtonTextOnly)
            if tool_id == _TOOL_BLOCK:
                btn.setChecked(True)
            btn.clicked.connect(
                lambda _checked=False, tid=tool_id: self._on_walk_tool_chosen(tid)
            )
            self._walk_tool_group.addButton(btn)
            tool_box.addWidget(btn)

        tool_box.addSpacing(8)
        tool_box.addWidget(QLabel("<b>Brush</b>"))
        self._walk_brush_combo = QComboBox()
        for size in _BRUSH_SIZES:
            self._walk_brush_combo.addItem(f"{size} px", size)
        self._walk_brush_combo.setCurrentIndex(_BRUSH_SIZES.index(self._walk_brush_size))
        self._walk_brush_combo.currentIndexChanged.connect(
            lambda _ix: self._set_brush_size(self._walk_brush_combo.currentData())
        )
        tool_box.addWidget(self._walk_brush_combo)

        self._walk_snap_chk = QCheckBox("Snap to 8 px")
        self._walk_snap_chk.setToolTip(
            "Snap brush center to 8-pixel tile boundaries before painting."
        )
        self._walk_snap_chk.toggled.connect(self._on_walk_snap_toggled)
        tool_box.addWidget(self._walk_snap_chk)

        tool_box.addSpacing(8)
        tool_box.addWidget(QLabel("<b>Zoom</b>"))
        self._walk_zoom_lbl = QLabel(f"{self._walk_zoom}\u00d7 (Ctrl+wheel)")
        self._walk_zoom_lbl.setToolTip(
            "Ctrl + mouse wheel on the canvas to zoom. "
            "Right-drag pans."
        )
        tool_box.addWidget(self._walk_zoom_lbl)
        tool_box.addStretch(1)

        right_col = QWidget()
        right_col.setMinimumWidth(160)
        right_col.setLayout(tool_box)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._walk_canvas_scroll)
        splitter.addWidget(right_col)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)

        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.addWidget(splitter, 1)
        return page

    def _build_events_tab(self) -> QWidget:
        """OVERWORLD_SPRITE + exit-zone viewer + sidebar for the selected map.

        Layout: a horizontal splitter — canvas on the left, sidebar on
        the right. The sidebar's top half is a :class:`QStackedWidget`
        switching between sprite, exit, and spawn forms; the bottom
        half is a Photoshop-layer-style list of every marker, exit,
        and spawn on the map so users can pick through overlapping
        objects.
        """
        self._events_canvas = EventsCanvas()
        self._events_status = QLabel("Select a field map.")
        self._events_status.setStyleSheet("color: #888; padding: 2px 4px;")

        # ---- sidebar: stacked forms ------------------------------------
        # ``setKeyboardTracking(False)`` + ``valueChanged`` together give
        # us live arrow-click updates (one commit per tick) without
        # firing on every keystroke during typed input — that only
        # commits on Enter/focus-out. ``editingFinished`` alone misses
        # arrow clicks because the spinbox keeps focus.
        self._events_stack = QStackedWidget()

        # Page 0: placeholder, shown when nothing is selected.
        empty_page = QLabel("Select an object on the map.")
        empty_page.setStyleSheet("color: #888; padding: 8px;")
        empty_page.setAlignment(Qt.AlignCenter)
        self._events_stack.addWidget(empty_page)

        # Page 1: sprite form (existing — Sprite ID, X, Y). Sprite ID is
        # an MCHR id, so it gets the same name-filterable picker the base
        # digimon data's Overworld field uses (shared "mchr" picker model
        # — built once at ROM load). The actual write goes through
        # ``EditOverworldSpriteIdCommand`` in
        # :meth:`_on_event_field_id_committed`, not a SetAttrCommand, so
        # we wire ``currentIndexChanged`` to a plain handler instead of
        # using :class:`_SpriteListPicker` (which hard-codes SetAttrCommand).
        self._events_field_id = QComboBox()
        self._events_field_id.setEditable(True)
        self._events_field_id.setInsertPolicy(QComboBox.NoInsert)
        self._events_field_id.setMaxVisibleItems(20)
        self._events_field_id.setSizeAdjustPolicy(
            QComboBox.AdjustToMinimumContentsLengthWithIcon,
        )
        self._events_field_id.setMinimumContentsLength(14)
        # ow-id space (UserRole == overworld_sprite_id), NOT CHR index — the
        # placement field is an ow-id, so selecting/assigning here must round
        # -trip through the same space the marker renders from.
        _mchr_model = self._session.picker_model("mchr_ow")
        if _mchr_model is not None:
            self._events_field_id.setModel(_mchr_model)
        _events_id_completer = QCompleter(self._events_field_id)
        _events_id_completer.setCaseSensitivity(Qt.CaseInsensitive)
        _events_id_completer.setFilterMode(Qt.MatchContains)
        _events_id_completer.setCompletionMode(QCompleter.PopupCompletion)
        _events_id_completer.setModel(self._events_field_id.model())
        self._events_field_id.setCompleter(_events_id_completer)
        # Sprite frame — the placement's behavior u16 (offset 24 in the
        # OVERWORLD_SPRITE block). Hypothesis that it indexes the MCHR
        # animation frame didn't pan out, so the spinbox is kept as a
        # backing field (so undo/sync paths stay intact) but not surfaced
        # in the form until we understand what behavior actually drives.
        self._events_field_frame = QSpinBox()
        self._events_field_frame.setRange(0, 0xFFFF)
        self._events_field_frame.setKeyboardTracking(False)
        self._events_field_x = QSpinBox()
        self._events_field_x.setRange(0, 0xFFFF)
        self._events_field_x.setKeyboardTracking(False)
        self._events_field_y = QSpinBox()
        self._events_field_y.setRange(0, 0xFFFF)
        self._events_field_y.setKeyboardTracking(False)
        sprite_page = QWidget()
        sprite_form = QFormLayout(sprite_page)
        sprite_form.setContentsMargins(6, 6, 6, 0)
        sprite_form.addRow(QLabel("<b>Overworld sprite</b>"))
        sprite_form.addRow("Sprite ID", self._events_field_id)
        sprite_form.addRow("X", self._events_field_x)
        sprite_form.addRow("Y", self._events_field_y)
        self._events_stack.addWidget(sprite_page)

        # Page 2: exit form (X1/Y1/X2/Y2 + destination combo + spawn arg).
        self._events_exit_x1 = QSpinBox()
        self._events_exit_x1.setRange(0, 0xFFFF)
        self._events_exit_x1.setKeyboardTracking(False)
        self._events_exit_y1 = QSpinBox()
        self._events_exit_y1.setRange(0, 0xFFFF)
        self._events_exit_y1.setKeyboardTracking(False)
        self._events_exit_x2 = QSpinBox()
        self._events_exit_x2.setRange(0, 0xFFFF)
        self._events_exit_x2.setKeyboardTracking(False)
        self._events_exit_y2 = QSpinBox()
        self._events_exit_y2.setRange(0, 0xFFFF)
        self._events_exit_y2.setKeyboardTracking(False)
        self._events_exit_dest = QComboBox()
        # Without these, the combo's implicit size hint follows its widest
        # item ("Custom (raw offset) — unchanged") and drags the whole
        # sidebar's minimum width up by ~250 px.
        self._events_exit_dest.setSizeAdjustPolicy(
            QComboBox.AdjustToMinimumContentsLengthWithIcon,
        )
        self._events_exit_dest.setMinimumContentsLength(14)
        # Populated lazily on first map render so we can reuse the
        # cached entry_starts table — re-running the combo build per
        # map is wasted work since entry_starts is static per ROM.
        self._events_exit_dest_built = False
        self._events_exit_spawn_arg = QSpinBox()
        self._events_exit_spawn_arg.setRange(0, _U32_SPINBOX_MAX)
        self._events_exit_spawn_arg.setDisplayIntegerBase(16)
        self._events_exit_spawn_arg.setPrefix("0x")
        self._events_exit_spawn_arg.setKeyboardTracking(False)
        self._events_exit_handler_label = QLabel("—")
        self._events_exit_handler_label.setStyleSheet("color: #888;")
        exit_page = QWidget()
        exit_form = QFormLayout(exit_page)
        exit_form.setContentsMargins(6, 6, 6, 0)
        exit_form.addRow(QLabel("<b>Exit zone</b>"))
        exit_form.addRow("X1 (tile)", self._events_exit_x1)
        exit_form.addRow("Y1 (tile)", self._events_exit_y1)
        exit_form.addRow("X2 (tile)", self._events_exit_x2)
        exit_form.addRow("Y2 (tile)", self._events_exit_y2)
        exit_form.addRow("Destination", self._events_exit_dest)
        exit_form.addRow("Spawn arg", self._events_exit_spawn_arg)
        exit_form.addRow("Handler", self._events_exit_handler_label)
        self._events_stack.addWidget(exit_page)

        # Page 3: spawn form (tile X/Y + spawn-arg-equivalent on shared
        # handler is N/A — spawn zones have no handler).
        self._events_spawn_x = QSpinBox()
        self._events_spawn_x.setRange(0, 0xFFFF)
        self._events_spawn_x.setKeyboardTracking(False)
        self._events_spawn_y = QSpinBox()
        self._events_spawn_y.setRange(0, 0xFFFF)
        self._events_spawn_y.setKeyboardTracking(False)
        spawn_page = QWidget()
        spawn_form = QFormLayout(spawn_page)
        spawn_form.setContentsMargins(6, 6, 6, 0)
        spawn_form.addRow(QLabel("<b>Spawn point</b>"))
        spawn_form.addRow("X (tile)", self._events_spawn_x)
        spawn_form.addRow("Y (tile)", self._events_spawn_y)
        spawn_form.addRow(QLabel(
            "<i>Player arrives here when an exit on another map points "
            "to this entry.</i>"
        ))
        self._events_stack.addWidget(spawn_page)

        # Page 4: hitbox form — trigger box for a 0x001b block whose
        # handler is bespoke script (cutscene/dialog) rather than a
        # standard CALL_SCRIPT_AT_OFFSET. The box coords are editable
        # (same byte layout as exits), but the handler itself stays
        # read-only — we don't fully understand the script bytecode it
        # points at, and a stray edit would corrupt it.
        self._events_hitbox_x1 = QSpinBox()
        self._events_hitbox_x1.setRange(0, 0xFFFF)
        self._events_hitbox_x1.setKeyboardTracking(False)
        self._events_hitbox_y1 = QSpinBox()
        self._events_hitbox_y1.setRange(0, 0xFFFF)
        self._events_hitbox_y1.setKeyboardTracking(False)
        self._events_hitbox_x2 = QSpinBox()
        self._events_hitbox_x2.setRange(0, 0xFFFF)
        self._events_hitbox_x2.setKeyboardTracking(False)
        self._events_hitbox_y2 = QSpinBox()
        self._events_hitbox_y2.setRange(0, 0xFFFF)
        self._events_hitbox_y2.setKeyboardTracking(False)
        self._events_hitbox_handler_label = QLabel("—")
        self._events_hitbox_handler_label.setTextInteractionFlags(
            Qt.TextSelectableByMouse,
        )
        hitbox_page = QWidget()
        hitbox_form = QFormLayout(hitbox_page)
        hitbox_form.setContentsMargins(6, 6, 6, 0)
        hitbox_form.addRow(QLabel("<b>Interaction hitbox</b>"))
        hitbox_form.addRow("X1 (tile)", self._events_hitbox_x1)
        hitbox_form.addRow("Y1 (tile)", self._events_hitbox_y1)
        hitbox_form.addRow("X2 (tile)", self._events_hitbox_x2)
        hitbox_form.addRow("Y2 (tile)", self._events_hitbox_y2)
        hitbox_form.addRow("Handler", self._events_hitbox_handler_label)
        hitbox_help = QLabel(
            "<i>Triggers a cutscene or dialog with bespoke script."
            " Handler is read-only (editing the bytes would risk"
            " corrupting the script), but the trigger box can move.</i>"
        )
        hitbox_help.setWordWrap(True)
        hitbox_form.addRow(hitbox_help)
        self._events_stack.addWidget(hitbox_page)

        # ---- sidebar: per-object dialog list + editor ------------------
        # Always visible: a placeholder row appears when the selected
        # object has no dialog pointer or no decoded dialog blocks, so
        # the surrounding form/list don't shift on every selection
        # change. Forward-scan only — see ``iter_dialogs_from``.
        #
        # Layout: a horizontal split — left = list + 3 spinboxes (the
        # editable u16s on the dialog block), right = a textbox-style
        # preview (portrait pixmap + speaker name + the resolved msg
        # text, editable inline). The two halves stay aligned because
        # the placeholder row keeps the left column's height stable
        # regardless of how many dialogs the selection actually has.
        self._dialogs_group = QWidget()
        dialogs_outer = QVBoxLayout(self._dialogs_group)
        dialogs_outer.setContentsMargins(0, 4, 0, 0)
        dialogs_outer.addWidget(QLabel("<b>Dialogs</b>"))

        dialogs_split = QHBoxLayout()
        dialogs_split.setContentsMargins(0, 0, 0, 0)
        dialogs_split.setSpacing(6)

        # Left column: dialog list + the three field spinboxes.
        dialogs_left = QWidget()
        dialogs_left_layout = QVBoxLayout(dialogs_left)
        dialogs_left_layout.setContentsMargins(0, 0, 0, 0)
        dialogs_left_layout.setSpacing(4)

        self._dialogs_list = QListWidget()
        self._dialogs_list.setUniformItemSizes(True)
        self._dialogs_list.setMaximumHeight(96)
        self._dialogs_list.setMinimumWidth(0)
        self._dialogs_list.setTextElideMode(Qt.ElideRight)
        self._dialogs_list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarAlwaysOff
        )
        self._dialogs_list.setStyleSheet(
            "QListWidget::item { padding: 1px 4px; }"
        )
        self._dialogs_list.currentRowChanged.connect(
            self._on_dialog_row_changed
        )
        dialogs_left_layout.addWidget(self._dialogs_list)

        self._dialog_target_spin = QSpinBox()
        self._dialog_target_spin.setRange(0, 0xFFFF)
        self._dialog_target_spin.setDisplayIntegerBase(16)
        self._dialog_target_spin.setPrefix("0x")
        self._dialog_target_spin.setKeyboardTracking(False)
        self._dialog_target_spin.valueChanged.connect(
            lambda v: self._on_dialog_field_committed("target", v)
        )
        self._dialog_msg_id_spin = QSpinBox()
        self._dialog_msg_id_spin.setRange(0, 0xFFFF)
        self._dialog_msg_id_spin.setDisplayIntegerBase(16)
        self._dialog_msg_id_spin.setPrefix("0x")
        self._dialog_msg_id_spin.setKeyboardTracking(False)
        self._dialog_msg_id_spin.valueChanged.connect(
            lambda v: self._on_dialog_field_committed("msg_id", v)
        )
        # Portrait/Name picker — same digimon+NPC sprite_map list the
        # base-digimon Display/Reskin "Appears as" combo uses (since the
        # portrait u16 indexes that same sprite_map table per
        # ``project_dialog_portrait_id_is_sprite_map_index``). Editable
        # with substring completer so typing "lunamon" filters the
        # dropdown; out-of-range ids get an "(undefined 0xNNNN)" row so
        # we never clobber data we can't represent.
        self._dialog_portrait_combo = QComboBox()
        self._dialog_portrait_combo.setEditable(True)
        self._dialog_portrait_combo.setInsertPolicy(QComboBox.NoInsert)
        self._dialog_portrait_combo.setMaximumWidth(280)
        self._dialog_portrait_combo.setMaxVisibleItems(20)
        portrait_model = self._session.picker_model("sprite_map")
        if portrait_model is not None:
            self._dialog_portrait_combo.setModel(portrait_model)
        portrait_completer = QCompleter(self._dialog_portrait_combo)
        portrait_completer.setCaseSensitivity(Qt.CaseInsensitive)
        portrait_completer.setFilterMode(Qt.MatchContains)
        portrait_completer.setCompletionMode(QCompleter.PopupCompletion)
        portrait_completer.setModel(self._dialog_portrait_combo.model())
        self._dialog_portrait_combo.setCompleter(portrait_completer)
        self._dialog_portrait_combo.currentIndexChanged.connect(
            self._on_dialog_portrait_combo_changed
        )

        # Target spin is kept as a backing field (so setValue / undo sync
        # paths stay intact) but not surfaced in the form — the target
        # slot id has no clear use yet, hide until we wire it up.
        dialog_form = QFormLayout()
        dialog_form.setContentsMargins(0, 0, 0, 0)
        dialog_form.addRow("Msg id", self._dialog_msg_id_spin)
        dialog_form.addRow("Portrait/Name", self._dialog_portrait_combo)
        dialogs_left_layout.addLayout(dialog_form)

        # Right column: portrait + speaker name + editable msg text.
        dialogs_right = QWidget()
        dialogs_right_layout = QVBoxLayout(dialogs_right)
        dialogs_right_layout.setContentsMargins(0, 0, 0, 0)
        dialogs_right_layout.setSpacing(2)

        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.setSpacing(6)
        self._dialog_preview_portrait = QLabel()
        self._dialog_preview_portrait.setFixedSize(48, 48)
        self._dialog_preview_portrait.setAlignment(Qt.AlignCenter)
        self._dialog_preview_portrait.setStyleSheet(
            "QLabel { border: 1px solid palette(mid); background: palette(base); }"
        )
        header_row.addWidget(self._dialog_preview_portrait, 0)
        self._dialog_preview_name = QLabel("—")
        self._dialog_preview_name.setStyleSheet("QLabel { font-weight: bold; }")
        self._dialog_preview_name.setTextInteractionFlags(
            Qt.TextSelectableByMouse
        )
        self._dialog_preview_name.setWordWrap(True)
        header_row.addWidget(self._dialog_preview_name, 1)
        dialogs_right_layout.addLayout(header_row)

        self._dialog_preview_text = QPlainTextEdit()
        self._dialog_preview_text.setMinimumHeight(80)
        self._dialog_preview_text.setMaximumHeight(160)
        self._dialog_preview_text.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        self._dialog_preview_text.setPlaceholderText("(select a dialog)")
        self._dialog_preview_text.textChanged.connect(
            self._on_dialog_text_changed
        )
        dialogs_right_layout.addWidget(self._dialog_preview_text, 1)

        # Debounce text-edit commits so each keystroke doesn't push a
        # full SetAttrCommand onto the undo stack — mirrors the
        # StringEditor's pattern.
        self._dialog_text_commit_timer = QTimer(self)
        self._dialog_text_commit_timer.setSingleShot(True)
        self._dialog_text_commit_timer.setInterval(250)
        self._dialog_text_commit_timer.timeout.connect(
            self._commit_dialog_text_edit
        )
        # The GameString currently bound to the preview (or None when
        # the dialog has no resolvable msg). Used by the debounced
        # commit + by the redo/undo on-change callback.
        self._dialog_preview_string = None

        dialogs_split.addWidget(dialogs_left, 1)
        dialogs_split.addWidget(dialogs_right, 1)
        dialogs_outer.addLayout(dialogs_split)

        # ---- sidebar: object list (Photoshop layer-panel style) --------
        self._events_list = QListWidget()
        self._events_list.setIconSize(QSize(32, 32))
        self._events_list.setUniformItemSizes(False)
        self._events_list.setMinimumWidth(0)
        self._events_list.setTextElideMode(Qt.ElideRight)
        self._events_list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarAlwaysOff
        )
        self._events_list.setStyleSheet(
            "QListWidget::item { padding: 2px 4px; }"
        )

        sidebar = QWidget()
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(0, 0, 0, 0)
        sidebar_layout.addWidget(self._events_stack, 0)
        sidebar_layout.addWidget(self._dialogs_group, 0)
        sidebar_layout.addWidget(QLabel("Objects on map:"), 0)
        sidebar_layout.addWidget(self._events_list, 1)

        # ---- assemble splitter ------------------------------------------
        canvas_wrap = QWidget()
        canvas_layout = QVBoxLayout(canvas_wrap)
        canvas_layout.setContentsMargins(0, 0, 0, 0)
        canvas_layout.addWidget(self._events_status, 0)
        canvas_layout.addWidget(self._events_canvas, 1)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(canvas_wrap)
        splitter.addWidget(sidebar)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        # Sidebar default 600px — wide enough that the hitbox help
        # paragraph and other multi-line labels read on one line,
        # without leaving them to dictate width via their own
        # implicit minimum (the cause of the old runaway-wide sidebar).
        # User can still drag the splitter; word-wrapped labels rewrap
        # cleanly when it's narrowed.
        splitter.setSizes([1000, 600])

        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.addWidget(splitter)

        # ---- sidebar state + signal wiring ------------------------------
        # Tracks which object the sidebar form belongs to; -1 = none.
        self._events_selected_offset: int = -1
        # "sprite" / "exit" / "spawn" / "" — drives form dispatch.
        self._events_selected_type: str = ""
        # Guards against feedback loops when the canvas-side and list-
        # side selections sync each other.
        self._events_syncing = False
        # Cached per-block resolved fields so the list re-render after
        # an undo doesn't have to re-pull sprite_map every time.
        self._events_marker_index: Dict[int, EventMarkerSpec] = {}
        # Per-block resolved exit/spawn info (handler location,
        # destination map_id, etc.) — keyed by 0x001b block_offset.
        self._events_exit_index: Dict[int, _ExitFormData] = {}
        # Current overlay5 entry that owns the selected map. Cached so
        # dialog scans (which run on the entry's bytes) don't have to
        # recompute it on every selection change.
        self._events_entry_ix: int = -1
        # Cached DialogBlocks for the currently selected object, plus the
        # entry_ix they live in (usually ``_events_entry_ix`` for sprite
        # ``string_ptr``; can be a different entry for hitbox ``dst``).
        self._dialogs_for_selection: List[overlay5_mod.DialogBlock] = []
        self._dialogs_entry_ix: int = -1

        self._events_canvas.markerSelected.connect(
            self._on_event_canvas_sprite_selected,
        )
        self._events_canvas.exitSelected.connect(
            self._on_event_canvas_exit_selected,
        )
        self._events_list.currentRowChanged.connect(
            self._on_event_list_row_changed,
        )
        self._events_field_id.currentIndexChanged.connect(
            lambda _ix: self._on_event_field_id_committed(),
        )
        self._events_field_frame.valueChanged.connect(
            lambda _v: self._on_event_field_frame_committed(),
        )
        self._events_field_x.valueChanged.connect(
            lambda _v: self._on_event_field_xy_committed(),
        )
        self._events_field_y.valueChanged.connect(
            lambda _v: self._on_event_field_xy_committed(),
        )
        for box in (self._events_exit_x1, self._events_exit_y1,
                    self._events_exit_x2, self._events_exit_y2,
                    self._events_hitbox_x1, self._events_hitbox_y1,
                    self._events_hitbox_x2, self._events_hitbox_y2):
            box.valueChanged.connect(
                lambda _v: self._on_event_field_box_committed(),
            )
        self._events_exit_dest.currentIndexChanged.connect(
            self._on_event_field_dest_committed,
        )
        self._events_exit_spawn_arg.valueChanged.connect(
            lambda _v: self._on_event_field_spawn_arg_committed(),
        )
        for box in (self._events_spawn_x, self._events_spawn_y):
            box.valueChanged.connect(
                lambda _v: self._on_event_field_spawn_committed(),
            )
        self._events_stack.setCurrentIndex(_EVT_PAGE_EMPTY)
        return page

    def _show_events_form(self, form_type: str) -> None:
        """Switch the sidebar's stacked form to the page that matches
        ``form_type`` ("", "sprite", "exit", "spawn", or "hitbox"). The
        empty string shows the placeholder page — used when no object
        is selected or the canvas is empty."""
        page = {
            _EVT_ROW_SPRITE: _EVT_PAGE_SPRITE,
            _EVT_ROW_EXIT: _EVT_PAGE_EXIT,
            _EVT_ROW_SPAWN: _EVT_PAGE_SPAWN,
            _EVT_ROW_HITBOX: _EVT_PAGE_HITBOX,
        }.get(form_type, _EVT_PAGE_EMPTY)
        self._events_stack.setCurrentIndex(page)

    # ---- Walkability paint event handlers -------------------------------

    def _on_walk_tool_chosen(self, tool_id: str) -> None:
        self._walk_tool = tool_id
        # Brush preview color depends on tool — repaint so the hover rect
        # reflects the new tool immediately.
        if self._walk_hover_pos is not None:
            self._refresh_walk_canvas()

    def _set_brush_size(self, size: int) -> None:
        self._walk_brush_size = int(size)
        if self._walk_hover_pos is not None:
            self._refresh_walk_canvas()

    def _on_walk_snap_toggled(self, checked: bool) -> None:
        self._walk_snap_to_tile = bool(checked)
        if self._walk_hover_pos is not None:
            self._refresh_walk_canvas()

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
        # Keep the brush footprint preview anchored to the stroke head so
        # the user can see which cells the next sample will touch.
        self._walk_hover_pos = (int(x), int(y))
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
        if self._active_render_id() == self._TAB_WALK:
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

        Cached per map id: the dims are constant for a selection, but this
        is called on every hover/paint sample (the brush hits it per
        Bresenham step), and re-parsing the file each time was the walk
        tab's main source of lag.
        """
        if self._current_id is None:
            return (0, 0)
        cache = self._walk_dims_cache
        if cache is not None and cache[0] == self._current_id:
            return cache[1], cache[2]
        files = mapmod.MapFiles(self._current_id)
        w, h, _ = mapmod.parse_walkability(
            self._session.map_file_bytes(files.walkability)
        )
        self._walk_dims_cache = (self._current_id, w, h)
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

    def _brush_footprint(self, cx: int, cy: int) -> Tuple[int, int, int, int]:
        """Return (x, y, w, h) in image-space px for the brush footprint
        centered at ``(cx, cy)`` — mirrors :meth:`_walk_paint_brush` so
        the preview rect lines up byte-for-byte with what painting hits.
        """
        size = max(1, self._walk_brush_size)
        if self._walk_snap_to_tile:
            cx = (cx // 8) * 8 + 4
            cy = (cy // 8) * 8 + 4
        half = size // 2
        return (cx - half, cy - half, size, size)

    def _refresh_walk_canvas(
        self, *, base_image: Optional[QImage] = None,
    ) -> None:
        """Compose the scaled walk canvas: base pixmap (cached) + brush
        footprint overlay if hovering. Rebuilds the scaled base only when
        the underlying preview or zoom changed.
        """
        if self._walk_scaled_base_pixmap is None:
            preview = self._walk_current_preview
            if preview is None:
                return
            if base_image is None:
                # Re-pin RGBA so the QImage doesn't outlive the buffer.
                self._pinned_rgba[self._TAB_WALK] = preview.rgba
                base_image = QImage(
                    self._pinned_rgba[self._TAB_WALK],
                    preview.width, preview.height,
                    preview.width * 4,
                    QImage.Format_RGBA8888,
                )
            pix = QPixmap.fromImage(base_image)
            if self._walk_zoom > 1:
                pix = pix.scaled(
                    pix.width() * self._walk_zoom,
                    pix.height() * self._walk_zoom,
                    Qt.IgnoreAspectRatio, Qt.FastTransformation,
                )
            self._walk_scaled_base_pixmap = pix

        pixmap = QPixmap(self._walk_scaled_base_pixmap)  # copy-on-write
        if self._walk_hover_pos is not None and self._walk_tool != _TOOL_PICKER:
            cx, cy = self._walk_hover_pos
            x, y, w, h = self._brush_footprint(cx, cy)
            scale = self._walk_zoom
            rect = QRect(x * scale, y * scale, w * scale, h * scale)
            painter = QPainter(pixmap)
            # Color follows the tool — red for block, green for walkable —
            # so the preview previews the *result*, not just "a brush".
            if self._walk_tool == _TOOL_BLOCK:
                fill = QColor(255, 60, 60, 100)
                edge = QColor(255, 200, 200, 230)
            else:
                fill = QColor(80, 220, 120, 100)
                edge = QColor(220, 255, 220, 230)
            painter.fillRect(rect, fill)
            pen = QPen(edge)
            pen.setWidth(1)
            painter.setPen(pen)
            painter.drawRect(rect.adjusted(0, 0, -1, -1))
            painter.end()

        self._walk_label.setText("")
        self._walk_label.setPixmap(pixmap)
        self._walk_label.resize(pixmap.width(), pixmap.height())

    def _on_walk_hovered(self, x: int, y: int) -> None:
        ww, wh = self._walk_dims_or_fallback()
        if ww and (x < 0 or x >= ww or y < 0 or y >= wh):
            # Outside the walkability grid — treat as a leave so a stale
            # rect doesn't linger at the right/bottom edge of bigger maps.
            self._on_walk_hover_left()
            return
        new_pos = (int(x), int(y))
        if self._walk_hover_pos == new_pos:
            return
        self._walk_hover_pos = new_pos
        self._refresh_walk_canvas()

    def _on_walk_hover_left(self) -> None:
        if self._walk_hover_pos is None:
            return
        self._walk_hover_pos = None
        self._refresh_walk_canvas()

    def _on_walk_zoom_step(self, steps: int) -> None:
        if steps == 0:
            return
        try:
            cur = _ZOOM_LEVELS.index(self._walk_zoom)
        except ValueError:
            cur = 0
        new = max(0, min(len(_ZOOM_LEVELS) - 1, cur + steps))
        if _ZOOM_LEVELS[new] == self._walk_zoom:
            return
        self._walk_zoom = _ZOOM_LEVELS[new]
        self._walk_label.setImageScale(self._walk_zoom)
        self._walk_zoom_lbl.setText(f"{self._walk_zoom}\u00d7 (Ctrl+wheel)")
        self._walk_scaled_base_pixmap = None
        self._refresh_walk_canvas()

    def _on_walk_pan(self, dx: int, dy: int) -> None:
        h = self._walk_canvas_scroll.horizontalScrollBar()
        v = self._walk_canvas_scroll.verticalScrollBar()
        if dx:
            h.setValue(h.value() - dx)
        if dy:
            v.setValue(v.value() - dy)

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
                # Footprint is the N×N brush anchored top-left at the hover
                # cell, clipped to the map so the preview matches the stamp.
                n = max(1, state.brush_size)
                w = min(n, state.n_tiles_x - tx)
                h = min(n, state.n_tiles_y - ty)
                rect = QRect(tx * cell, ty * cell, w * cell, h * cell)
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
        preview_lbl: _BrushPreview = getattr(self, f"_layer_{layer}_preview_lbl")
        sel_lbl: QLabel = getattr(self, f"_layer_{layer}_sel_lbl")
        if not state.tiles:
            preview_lbl.clear()
            sel_lbl.setText("Selected tile: \u2014")
            return
        # Render the whole N\u00d7N brush footprint so the preview shows exactly
        # what a stamp lays down (a solid block in single-tile mode, or the
        # captured pattern after a map color-pick).
        n = max(1, state.brush_size)
        side = 8 * n
        buf = bytearray(side * side * 4)
        for j in range(n):
            for i in range(n):
                mapmod.paint_tile_into_rgba(
                    buf, side, i, j, state.brush_cell(i, j),
                    state.tiles, state.palettes, backdrop_opaque=True,
                )
        backing = bytes(buf)
        image = QImage(backing, side, side, side * 4, QImage.Format_RGBA8888)
        # Grow the preview for multi-tile brushes so each sub-cell is a
        # comfortable click target for the composer.
        disp = _PREVIEW_PX if n == 1 else _BRUSH_CELL_PX * n
        preview_lbl.setFixedSize(disp, disp)
        pixmap = QPixmap.fromImage(image).scaled(
            disp, disp, Qt.KeepAspectRatio, Qt.FastTransformation,
        )
        preview_lbl.setPixmap(pixmap)
        preview_lbl.set_grid(n, state.brush_sel)
        if n > 1:
            sel_lbl.setText(
                f"Brush {n}\u00d7{n} \u00b7 editing cell {state.brush_sel} "
                f"\u00b7 tile {state.selected_tile_ix}"
            )
        else:
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
                # Write the picked tile into the brush's active cell (N×N),
                # so the tileset picker composes the brush cell-by-cell.
                self._write_current_to_brush_cell(layer)
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
            self._stamp_brush(layer, tx, ty)
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
        if self._active_render_id() == tab_ix:
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
        if self._active_render_id() == tab_ix:
            self._refresh_active_tab()

    def _paint_cell(self, layer: str, tx: int, ty: int, entry: int) -> None:
        state = self._layer_state[layer]
        if not (0 <= tx < state.n_tiles_x and 0 <= ty < state.n_tiles_y):
            return
        cell_ix = ty * state.n_tiles_x + tx
        if state.entries[cell_ix] == entry:
            return
        state.entries[cell_ix] = entry
        mapmod.paint_tile_into_rgba(
            state.base_rgba, state.width_px, tx, ty, entry,
            state.tiles, state.palettes, backdrop_opaque=True,
        )

    def _stamp_brush(self, layer: str, tx: int, ty: int) -> None:
        """Stamp the N×N brush footprint with its top-left at ``(tx, ty)``.

        A 1×1 brush reduces to a single tile; larger brushes fill the block
        with the captured pattern (or the repeated toolbar tile). Cells past
        the map edge are skipped so a brush near the border clips cleanly.
        """
        state = self._layer_state[layer]
        n = max(1, state.brush_size)
        for j in range(n):
            for i in range(n):
                self._paint_cell(layer, tx + i, ty + j, state.brush_cell(i, j))

    def _paint_tile_line(
        self, layer: str, p0: Tuple[int, int], p1: Tuple[int, int],
    ) -> None:
        """Bresenham in tile-cell coords. Avoids dotted strokes on fast
        drags by stamping the brush at every cell the cursor crossed."""
        x0, y0 = p0
        x1, y1 = p1
        dx = abs(x1 - x0)
        dy = -abs(y1 - y0)
        sx = 1 if x0 < x1 else -1
        sy = 1 if y0 < y1 else -1
        err = dx + dy
        while True:
            self._stamp_brush(layer, x0, y0)
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
        next paint stroke reuses them — Porymap's eyedropper idiom.

        With an N×N brush, also capture the N×N block anchored at the
        clicked cell into ``brush_entries`` so the next stamp replays that
        exact pattern; the toolbar single-tile fields track the top-left."""
        state = self._layer_state[layer]
        n = max(1, state.brush_size)
        if n > 1:
            captured: List[int] = []
            for j in range(n):
                for i in range(n):
                    cx, cy = tx + i, ty + j
                    if 0 <= cx < state.n_tiles_x and 0 <= cy < state.n_tiles_y:
                        captured.append(state.entries[cy * state.n_tiles_x + cx])
                    else:
                        captured.append(0)  # off-map cell → tile 0
            state.brush_entries = captured
        else:
            state.brush_entries = None
        state.brush_sel = 0
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

    def _write_current_to_brush_cell(self, layer: str) -> None:
        """Store the toolbar's current tile into the brush's active cell.

        No-op in 1×1 mode (the footprint is just the toolbar tile). Lets the
        tileset picker / flip / bank controls compose an N×N brush one cell
        at a time — the cell chosen in the sidebar composer."""
        state = self._layer_state.get(layer)
        if state is None or state.brush_size <= 1 or state.brush_entries is None:
            return
        if 0 <= state.brush_sel < len(state.brush_entries):
            state.brush_entries[state.brush_sel] = state.entry_value()

    def _load_entry_into_toolbar(self, layer: str, entry: int) -> None:
        """Load a brush cell's tile + bank into the toolbar (for the N×N
        composer) without firing change handlers.

        The flip boxes are deliberately left *clear*: for an N×N brush they
        act on the whole selection, not one cell, so a per-cell flip bit
        isn't mirrored onto them. The cell keeps its own flip in
        ``brush_entries``; picking a fresh tile for it lands unflipped."""
        state = self._layer_state[layer]
        state.selected_tile_ix = entry & 0x3FF
        state.hflip = False
        state.vflip = False
        state.selected_bank = (entry >> 12) & 0xF
        bank_spin: QSpinBox = getattr(self, f"_layer_{layer}_bank_spin")
        bank_spin.blockSignals(True)
        bank_spin.setValue(min(state.selected_bank, bank_spin.maximum()))
        bank_spin.blockSignals(False)
        for chk_name in (f"_layer_{layer}_hflip_chk", f"_layer_{layer}_vflip_chk"):
            chk: QCheckBox = getattr(self, chk_name)
            chk.blockSignals(True)
            chk.setChecked(False)
            chk.blockSignals(False)

    def _on_brush_cell_clicked(self, layer: str, idx: int) -> None:
        """Select sub-cell ``idx`` of the N×N brush as the edit target and
        load its tile into the toolbar, so the next pick/flip/bank edits
        that cell."""
        state = self._layer_state.get(layer)
        if state is None:
            return
        n = max(1, state.brush_size)
        if not (0 <= idx < n * n):
            return
        state.brush_sel = idx
        if state.brush_entries is not None and idx < len(state.brush_entries):
            self._load_entry_into_toolbar(layer, state.brush_entries[idx])
        self._refresh_selected_tile_preview(layer)
        self._refresh_picker_overlay(layer)

    def _on_tile_brush_size_changed(self, layer: str, size: int) -> None:
        state = self._layer_state.get(layer)
        if state is None:
            return
        n = max(1, int(size))
        state.brush_size = n
        state.brush_sel = 0
        # Seed an N×N brush with the current tile in every cell (an editable
        # solid block); 1×1 just tracks the toolbar tile. Either way an old
        # captured pattern is dropped since it no longer fits the footprint.
        state.brush_entries = [state.entry_value()] * (n * n) if n > 1 else None
        self._refresh_selected_tile_preview(layer)
        # Repaint so the hover footprint outline tracks the new size.
        self._refresh_layer_canvas(layer)

    def _on_bank_changed(self, layer: str, value: int) -> None:
        state = self._layer_state.get(layer)
        if state is None:
            return
        state.selected_bank = int(value)
        self._write_current_to_brush_cell(layer)  # apply to the active cell
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
        if state.brush_size > 1 and state.brush_entries is not None:
            # N×N brush: the flip mirrors the *whole* selection. Momentary —
            # apply on toggle-on, then reset the box so it reads as a button.
            if value:
                self._mirror_brush(layer, axis)
                chk: QCheckBox = getattr(
                    self, f"_layer_{layer}_{'hflip' if axis == 'h' else 'vflip'}_chk",
                )
                chk.blockSignals(True)
                chk.setChecked(False)
                chk.blockSignals(False)
            return
        if axis == "h":
            state.hflip = bool(value)
        else:
            state.vflip = bool(value)
        self._write_current_to_brush_cell(layer)  # apply to the active cell
        self._refresh_selected_tile_preview(layer)

    def _mirror_brush(self, layer: str, axis: str) -> None:
        """Mirror the whole N×N brush along ``axis`` ('h'/'v'): each tile
        swaps to its opposing column/row and its own flip bit toggles, so the
        stamp is a true horizontal/vertical flip of the selection."""
        state = self._layer_state.get(layer)
        if state is None or state.brush_size <= 1 or state.brush_entries is None:
            return
        n = state.brush_size
        be = state.brush_entries
        new = [0] * (n * n)
        for j in range(n):
            for i in range(n):
                if axis == "h":
                    new[j * n + i] = be[j * n + (n - 1 - i)] ^ 0x400
                else:
                    new[j * n + i] = be[(n - 1 - j) * n + i] ^ 0x800
        state.brush_entries = new
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
        self._walk_scaled_base_pixmap = None
        self._walk_current_preview = None
        self._walk_hover_pos = None
        # Same deal for the tilemap painter — the state holds tile/palette
        # buffers tied to this map; the layer tab refresh will rebuild.
        self._layer_state["a"] = None
        self._layer_state["b"] = None
        self._update_tab_availability(map_id)
        self._update_metadata_and_tuples(map_id)
        self._refresh_active_tab()

    def _on_tab_changed(self, _ix: int) -> None:
        # Cutscenes + Encounters read overlay5 / ENCTBL data — the
        # tilemap-oriented metadata footer (Layer A/B dimensions, palette
        # banks, walkability, .d tuples) is irrelevant there, so hide it.
        self._meta_footer.setVisible(
            self._tabs.currentIndex() not in (
                self._REAL_TAB_CUTSCENES, self._REAL_TAB_ENCOUNTERS,
            )
        )
        self._refresh_active_tab()

    def _update_tab_availability(self, map_id: str) -> None:
        files = mapmod.MapFiles(map_id)
        has_b = (
            files.layer_b_screen in self._file_table
            and files.layer_b_tiles in self._file_table
            and files.layer_b_palette in self._file_table
        )
        has_walk = files.walkability in self._file_table
        # Events tab disabled when no overlay5 entry backs this map id
        # (entries 0..234 / 500..504 — see ``overlay5.map_id_for``).
        has_events = (
            overlay5_mod.entry_ix_for_map(int(map_id)) is not None
        )
        # Layer B is a View radio inside the Map tab now, not a tab — grey it
        # out when absent and fall the view back to Composite if it was live.
        self._view_b.setEnabled(has_b)
        if not has_b and self._view_b.isChecked():
            self._view_comp.setChecked(True)  # fires _on_map_view_changed
        self._tabs.setTabEnabled(self._REAL_TAB_WALK, has_walk)
        self._tabs.setTabEnabled(self._REAL_TAB_EVENTS, has_events)
        # Cutscenes tab follows the same gating as Events — both source
        # their data from overlay5, no entry → nothing to browse.
        self._tabs.setTabEnabled(self._REAL_TAB_CUTSCENES, has_events)
        if not self._tabs.isTabEnabled(self._tabs.currentIndex()):
            self._tabs.blockSignals(True)
            self._tabs.setCurrentIndex(self._REAL_TAB_MAP)
            self._tabs.blockSignals(False)

    # ---- Render dispatch -------------------------------------------------

    def _refresh_active_tab(self) -> None:
        if self._current_id is None:
            return
        ix = self._active_render_id()
        try:
            preview = self._render_for_tab(ix)
        except (ValueError, KeyError) as e:
            self._set_label_text(ix, f"Render failed: {e}")
            return
        # Layer A/B tabs manage their own pixmaps inside _render_for_tab
        # (tile picker + canvas update via their own pinned buffers), so
        # a None return there is a normal control-flow signal, not empty.
        # Events tab uses its own QGraphicsView (events_canvas), not the
        # shared pinned-RGBA path.
        if ix in (
            self._TAB_LAYER_A, self._TAB_LAYER_B,
            self._TAB_EVENTS, self._TAB_CUTSCENES, self._TAB_ENCOUNTERS,
        ):
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
        if ix == self._TAB_EVENTS:
            self._refresh_events_tab()
            return None
        if ix == self._TAB_CUTSCENES:
            self._refresh_cutscenes_tab()
            return None
        if ix == self._TAB_ENCOUNTERS:
            self._refresh_encounter_tab()
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

    # ---- Events tab refresh ---------------------------------------------

    def _build_event_canvas_specs(
        self, entry_ix: int, entry_bytes: bytes,
    ) -> Tuple[List[EventMarkerSpec], List[ExitZoneSpec], List["_ExitFormData"]]:
        """Decode + resolve every event-canvas item from an overlay5 entry.

        Returns ``(sprite_specs, exit_canvas_specs, exit_form_data)``.
        Used by the Events tab to populate its drag canvas, and by the
        Cutscenes tab to render the same overworld objects on its own
        read-only canvas. The ``exit_form_data`` is the Events tab's
        sidebar payload (handler resolution, destination label, etc.) —
        the Cutscenes tab can ignore it.
        """
        scan_bytes = overlay5_mod.script_prologue_bytes(entry_ix, entry_bytes)
        # Dedup by (sprite, position) not sprite alone, so a graphic reused
        # for two different NPCs at different spots (e.g. entry 262's two
        # Tanemon) both appear; only same-position story-state duplicates fold.
        placements = overlay5_mod.first_per_sprite_id_pos(
            overlay5_mod.iter_overworld_sprites(scan_bytes)
        )
        exit_zones = overlay5_mod.iter_exit_zones(scan_bytes)

        sprite_map = getattr(self._session, "sprite_map", []) or []
        mchr_to_base = self._mchr_to_base_lookup(sprite_map)
        specs: List[EventMarkerSpec] = [
            EventMarkerSpec(
                block_offset=p.block_offset,
                overworld_sprite_id=p.overworld_sprite_id,
                x=p.x, y=p.y,
                label=self._events_label_for(p.overworld_sprite_id, mchr_to_base),
                pixmap=self._events_pixmap_for(p.overworld_sprite_id),
                behavior=p.behavior,
            )
            for p in placements
        ]

        exit_form_data = self._compute_exit_form_data(exit_zones)
        exit_canvas_specs = [
            ExitZoneSpec(
                block_offset=d.block_offset,
                idx=d.idx,
                x1=d.x1, y1=d.y1, x2=d.x2, y2=d.y2,
                is_spawn=d.is_spawn,
                dest_label=d.dest_label,
                display_idx=d.display_idx,
                is_hitbox=d.is_hitbox,
            )
            for d in exit_form_data
        ]
        return specs, exit_canvas_specs, exit_form_data

    def _refresh_events_tab(self) -> None:
        """Re-render the Events tab for the current map.

        Reads the OVERWORLD_SPRITE placements + 0x001b exit / spawn
        zones from overlay5, resolves each ``digimon_id`` to a sprite +
        label via the session helpers, and hands the result to
        :class:`EventsCanvas`. Falls back to a status string when the
        map has no entry or no placements rather than showing an empty
        canvas with no explanation.
        """
        if self._current_id is None:
            return
        map_id = int(self._current_id)
        entry_ix = overlay5_mod.entry_ix_for_map(map_id)
        if entry_ix is None:
            self._events_canvas.clear()
            self._events_status.setText(
                f"Map {map_id} has no overlay5 entry — events tab disabled."
            )
            self._events_entry_ix = -1
            return
        self._events_entry_ix = entry_ix
        try:
            entry_bytes = self._session.overlay5_entry_bytes(entry_ix)
        except (ValueError, KeyError) as e:
            self._events_canvas.clear()
            self._events_status.setText(f"Failed to load overlay5 entry: {e}")
            return
        try:
            specs, exit_canvas_specs, exit_form_data = (
                self._build_event_canvas_specs(entry_ix, entry_bytes)
            )
        except (ValueError, KeyError) as e:
            self._events_canvas.clear()
            self._events_status.setText(f"Failed to parse overlay5 entry: {e}")
            return
        self._events_exit_index = {
            d.block_offset: d for d in exit_form_data
        }

        # Composite-render the map as the canvas backdrop.
        try:
            composite = mapmod.render_map_from_file_table(
                self._current_id, self._file_table, self._rom,
            )
        except (ValueError, KeyError) as e:
            self._events_canvas.clear()
            self._events_status.setText(f"Render failed: {e}")
            return
        # Pin the backing buffer so the QImage survives until we're done
        # converting it into a pixmap (which itself copies the data).
        self._pinned_rgba[self._TAB_EVENTS] = composite.rgba
        bg_image = QImage(
            self._pinned_rgba[self._TAB_EVENTS],
            composite.width, composite.height, composite.width * 4,
            QImage.Format_RGBA8888,
        )
        bg_pixmap = QPixmap.fromImage(bg_image)

        # Drag commits go through QUndoStack when one is wired; otherwise
        # the canvas stays read-only (snap-back on drop suppressed too,
        # since the marker never enters move-mode).
        moved_cb = self._on_event_marker_moved if self._undo_stack else None
        exit_moved_cb = (
            self._on_event_exit_moved if self._undo_stack else None
        )
        self._events_canvas.set_map(
            bg_pixmap, specs, moved_cb=moved_cb,
            exit_specs=exit_canvas_specs,
            exit_moved_cb=exit_moved_cb,
        )

        # Sidebar state — cache specs by block_offset so the layer-list
        # rebuild + the sprite-id edit paths can look them up without
        # re-parsing the overlay entry.
        self._events_marker_index = {s.block_offset: s for s in specs}
        self._events_selected_offset = -1
        self._events_selected_type = ""
        self._build_dest_combo_once()
        self._populate_events_list(specs, exit_form_data)
        self._show_events_form("")
        self._clear_event_fields()
        self._refresh_dialogs_panel(-1, "")

        n_exits = sum(1 for d in exit_form_data if not d.is_spawn)
        n_spawns = sum(1 for d in exit_form_data if d.is_spawn)
        self._events_status.setText(
            f"Map {map_id}  ·  entry {entry_ix:04d}  ·  "
            f"{len(specs)} sprite{'s' if len(specs) != 1 else ''}, "
            f"{n_exits} exit{'s' if n_exits != 1 else ''}, "
            f"{n_spawns} spawn{'s' if n_spawns != 1 else ''}"
        )

    # ---- Cutscenes tab --------------------------------------------------

    def _build_cutscenes_tab(self) -> QWidget:
        """Construct the read-only Cutscenes tab.

        Owns a :class:`CutscenesTab` widget — the chip-row scene browser
        layered over the map composite. Render dispatch is driven by
        :meth:`_refresh_cutscenes_tab`, which hands it the same
        composite render the Events tab and Composite tab use.
        """
        self._cutscenes_tab = CutscenesTab(
            self._session, parent=self, undo_stack=self._undo_stack,
        )
        # Let the tab's cross-map Index jump to any scene: flip the map cursor
        # + select the chain via the existing navigation entry point. The
        # reload cb restores the real map after a standalone shared-scene view.
        self._cutscenes_tab.set_navigate_cb(self.navigate_to_cutscene_chain)
        self._cutscenes_tab.set_reload_cb(self._refresh_cutscenes_tab)
        return self._cutscenes_tab

    def _build_encounter_tab(self) -> QWidget:
        """Construct the Encounters tab (per-map ENCTBL.BIN assignment).

        The tab's "Open in Wild Encounters editor" button routes through
        ``_navigate_to_wild_area`` up to the main window, mirroring the
        cross-references the enemy / cutscene panels use.
        """
        self._encounter_tab = MapEncounterTab(
            self._session, self._undo_stack,
            navigate_to_area=self._navigate_to_wild_area,
            parent=self,
        )
        return self._encounter_tab

    def _refresh_encounter_tab(self) -> None:
        if self._current_id is None:
            return
        self._encounter_tab.set_map(int(self._current_id))

    def _navigate_to_wild_area(self, area_index: int) -> None:
        """Encounters tab → Wild Encounters editor at ``area_index``.

        Thin bridge to the main window (which owns the editor stack); a
        no-op when the host doesn't expose the navigation method
        (headless tests)."""
        nav = getattr(self.window(), "navigate_to_wild_area", None)
        if nav is not None:
            nav(area_index)

    def navigate_to_map_encounters(self, map_id: int) -> None:
        """Public: open this browser at ``map_id`` on the Encounters tab.

        The reverse of :meth:`_navigate_to_wild_area` — the Wild
        Encounters editor's "Used by maps" links call this via the main
        window. No-ops when ``map_id`` isn't in the discovered list.
        """
        try:
            row_ix = self._map_ids.index(str(map_id))
        except ValueError:
            return
        self._list.select_index(row_ix)
        self._tabs.setCurrentIndex(self._REAL_TAB_ENCOUNTERS)

    def navigate_to_cutscene_chain(self, map_id: int, chain_ix: int) -> None:
        """Public: open this browser at ``map_id`` on the Cutscenes tab,
        with the chain at global ``chain_ix`` selected.

        Two-phase: first flip the map-list cursor to ``map_id`` (which
        triggers the standard per-tab render pipeline), then switch to
        the Cutscenes tab and forward the ``chain_ix`` to
        :class:`CutscenesTab.select_chain_by_global_ix`. Skipped when
        the map id isn't in the discovered list (out-of-range or
        stripped ROM) — no partial state left behind.
        """
        try:
            row_ix = self._map_ids.index(str(map_id))
        except ValueError:
            return
        self._list.select_index(row_ix)
        self._tabs.setCurrentIndex(self._REAL_TAB_CUTSCENES)
        self._cutscenes_tab.select_chain_by_global_ix(chain_ix)

    def _refresh_cutscenes_tab(self) -> None:
        """Re-render the Cutscenes tab for the current map.

        Decodes the same OVERWORLD_SPRITE / exit-zone / hitbox specs the
        Events tab uses (via :meth:`_build_event_canvas_specs`) and hands
        them to the cutscenes widget so its read-only canvas displays
        the overworld objects on top of the map.
        """
        if self._current_id is None:
            return
        map_id = int(self._current_id)
        entry_ix = overlay5_mod.entry_ix_for_map(map_id)
        if entry_ix is None:
            self._cutscenes_tab.clear()
            return
        try:
            entry_bytes = self._session.overlay5_entry_bytes(entry_ix)
            specs, exit_canvas_specs, exit_form_data = (
                self._build_event_canvas_specs(entry_ix, entry_bytes)
            )
        except (ValueError, KeyError):
            self._cutscenes_tab.clear()
            return
        try:
            composite = mapmod.render_map_from_file_table(
                self._current_id, self._file_table, self._rom,
            )
        except (ValueError, KeyError):
            self._cutscenes_tab.clear()
            return
        # Pin the rgba buffer in this tab's slot — the EventsCanvas
        # inside the cutscenes tab keeps a QPixmap, but the QImage that
        # produces it needs the buffer to live until QPixmap.fromImage
        # returns.
        self._pinned_rgba[self._TAB_CUTSCENES] = composite.rgba
        bg_image = QImage(
            self._pinned_rgba[self._TAB_CUTSCENES],
            composite.width, composite.height, composite.width * 4,
            QImage.Format_RGBA8888,
        )
        bg_pixmap = QPixmap.fromImage(bg_image)
        self._cutscenes_tab.set_map(
            map_id, entry_ix, bg_pixmap, specs, exit_canvas_specs,
            exit_form_data=exit_form_data,
        )

    def _populate_events_list(
        self,
        specs: List[EventMarkerSpec],
        exit_form_data: List["_ExitFormData"],
    ) -> None:
        """Refill the sidebar layer-list with sprite + exit + spawn rows.

        Sprites first, then exits, then spawns — matches the visual
        layering on the canvas (sprites paint on top). Each row tags
        its type via ``_EVT_ROW_TYPE_ROLE`` so selection dispatch
        routes to the right form page.
        """
        self._events_syncing = True
        try:
            self._events_list.clear()
            for s in specs:
                item = QListWidgetItem()
                item.setText(
                    f"0x{s.overworld_sprite_id:04x}  ({s.x}, {s.y})"
                )
                if s.pixmap is not None and not s.pixmap.isNull():
                    item.setIcon(QIcon(s.pixmap))
                else:
                    pm = QPixmap(32, 32)
                    pm.fill(QColor(80, 80, 100))
                    item.setIcon(QIcon(pm))
                item.setData(Qt.UserRole, s.block_offset)
                item.setData(_EVT_ROW_TYPE_ROLE, _EVT_ROW_SPRITE)
                item.setToolTip(s.label)
                self._events_list.addItem(item)
            for d in exit_form_data:
                if d.is_spawn or d.is_hitbox:
                    continue
                item = QListWidgetItem()
                item.setText(
                    f"Exit {d.display_idx}  →  {d.dest_label or '?'}"
                )
                item.setIcon(self._events_exit_icon())
                item.setData(Qt.UserRole, d.block_offset)
                item.setData(_EVT_ROW_TYPE_ROLE, _EVT_ROW_EXIT)
                item.setToolTip(
                    f"Exit zone (tile {d.x1},{d.y1} — {d.x2},{d.y2})\n"
                    f"to: {d.dest_label or '(unknown)'}"
                )
                self._events_list.addItem(item)
            for d in exit_form_data:
                if not d.is_hitbox:
                    continue
                item = QListWidgetItem()
                item.setText(
                    f"Hitbox {d.display_idx}  "
                    f"(tile {d.x1},{d.y1} — {d.x2},{d.y2})"
                )
                item.setIcon(self._events_hitbox_icon())
                item.setData(Qt.UserRole, d.block_offset)
                item.setData(_EVT_ROW_TYPE_ROLE, _EVT_ROW_HITBOX)
                item.setToolTip(
                    f"Interaction hitbox (read-only)\n"
                    f"tile ({d.x1},{d.y1}) — ({d.x2},{d.y2})\n"
                    f"{d.dest_label or ''}".rstrip()
                )
                self._events_list.addItem(item)
            for d in exit_form_data:
                if not d.is_spawn:
                    continue
                item = QListWidgetItem()
                item.setText(f"Spawn {d.display_idx}  ({d.x1}, {d.y1})")
                item.setIcon(self._events_spawn_icon())
                item.setData(Qt.UserRole, d.block_offset)
                item.setData(_EVT_ROW_TYPE_ROLE, _EVT_ROW_SPAWN)
                item.setToolTip(
                    f"Spawn point (tile {d.x1}, {d.y1})"
                )
                self._events_list.addItem(item)
            self._events_list.setCurrentRow(-1)
        finally:
            self._events_syncing = False

    def _events_exit_icon(self) -> QIcon:
        """Cached blue-rectangle swatch for exit-zone list rows.

        Built once per browser instance; same color family as the
        canvas exit overlay so the two read as the same kind of object.
        """
        if getattr(self, "_events_exit_icon_cache", None) is None:
            pm = QPixmap(32, 32)
            pm.fill(QColor(0, 0, 0, 0))
            p = QPainter(pm)
            p.setRenderHint(QPainter.Antialiasing, True)
            p.setPen(QPen(QColor(80, 180, 255, 240), 2))
            p.setBrush(QColor(80, 180, 255, 90))
            p.drawRect(4, 8, 24, 16)
            p.end()
            self._events_exit_icon_cache = QIcon(pm)
        return self._events_exit_icon_cache

    def _events_spawn_icon(self) -> QIcon:
        """Cached green-diamond swatch for spawn-point list rows."""
        if getattr(self, "_events_spawn_icon_cache", None) is None:
            pm = QPixmap(32, 32)
            pm.fill(QColor(0, 0, 0, 0))
            p = QPainter(pm)
            p.setRenderHint(QPainter.Antialiasing, True)
            p.setPen(QPen(QColor(60, 180, 60, 240), 2))
            p.setBrush(QColor(120, 220, 120, 150))
            p.drawPolygon([
                QPoint(16, 4), QPoint(28, 16),
                QPoint(16, 28), QPoint(4, 16),
            ])
            p.end()
            self._events_spawn_icon_cache = QIcon(pm)
        return self._events_spawn_icon_cache

    def _events_hitbox_icon(self) -> QIcon:
        """Cached orange-rectangle swatch for interaction-hitbox rows.

        Distinct hue from exit rows (blue) so the user can tell at a
        glance which 0x001b blocks are bespoke triggers vs map exits.
        """
        if getattr(self, "_events_hitbox_icon_cache", None) is None:
            pm = QPixmap(32, 32)
            pm.fill(QColor(0, 0, 0, 0))
            p = QPainter(pm)
            p.setRenderHint(QPainter.Antialiasing, True)
            p.setPen(QPen(QColor(255, 165, 0, 240), 2))
            p.setBrush(QColor(255, 165, 0, 90))
            p.drawRect(4, 8, 24, 16)
            p.end()
            self._events_hitbox_icon_cache = QIcon(pm)
        return self._events_hitbox_icon_cache

    def _clear_event_fields(self) -> None:
        """Zero every sidebar spinbox / combo without firing edit signals."""
        self._events_syncing = True
        try:
            self._set_events_sprite_id_combo(0)
            self._events_field_frame.setValue(0)
            self._events_field_x.setValue(0)
            self._events_field_y.setValue(0)
            self._events_exit_x1.setValue(0)
            self._events_exit_y1.setValue(0)
            self._events_exit_x2.setValue(0)
            self._events_exit_y2.setValue(0)
            self._events_exit_spawn_arg.setValue(0)
            if self._events_exit_dest.count() > 0:
                self._events_exit_dest.setCurrentIndex(0)
            self._events_exit_handler_label.setText("—")
            self._events_spawn_x.setValue(0)
            self._events_spawn_y.setValue(0)
            self._events_hitbox_x1.setValue(0)
            self._events_hitbox_y1.setValue(0)
            self._events_hitbox_x2.setValue(0)
            self._events_hitbox_y2.setValue(0)
            self._events_hitbox_handler_label.setText("—")
        finally:
            self._events_syncing = False

    def _load_event_fields_for(
        self, block_offset: int, row_type: str,
    ) -> None:
        """Populate the active form from the cached spec for the given
        block_offset and row type. Falls back to the empty page when
        the spec lookup misses (shouldn't normally happen — the lists
        and caches are populated in lockstep)."""
        if row_type == _EVT_ROW_SPRITE:
            spec = self._events_marker_index.get(block_offset)
            if spec is None:
                self._show_events_form("")
                self._clear_event_fields()
                return
            self._events_syncing = True
            try:
                self._set_events_sprite_id_combo(int(spec.overworld_sprite_id))
                self._events_field_frame.setValue(int(spec.behavior) & 0xFFFF)
                self._events_field_x.setValue(int(spec.x))
                self._events_field_y.setValue(int(spec.y))
            finally:
                self._events_syncing = False
            self._show_events_form(_EVT_ROW_SPRITE)
            return
        data = self._events_exit_index.get(block_offset)
        if data is None:
            self._show_events_form("")
            self._clear_event_fields()
            return
        if row_type == _EVT_ROW_EXIT:
            self._events_syncing = True
            try:
                self._events_exit_x1.setValue(int(data.x1))
                self._events_exit_y1.setValue(int(data.y1))
                self._events_exit_x2.setValue(int(data.x2))
                self._events_exit_y2.setValue(int(data.y2))
                self._events_exit_spawn_arg.setValue(
                    int(data.handler_spawn_arg) & _U32_SPINBOX_MAX
                )
                self._set_dest_combo_to(data.dest_map_id, data.handler_dest)
                self._events_exit_handler_label.setText(
                    self._format_handler_label(data)
                )
            finally:
                self._events_syncing = False
            # Disable handler-side fields when no handler resolved — we
            # can't write to a u32 that isn't a real 0x0002/0x0030 prefix.
            handler_ok = data.handler_entry_ix >= 0
            self._events_exit_dest.setEnabled(handler_ok)
            self._events_exit_spawn_arg.setEnabled(handler_ok)
            self._show_events_form(_EVT_ROW_EXIT)
            return
        if row_type == _EVT_ROW_SPAWN:
            self._events_syncing = True
            try:
                self._events_spawn_x.setValue(int(data.x1))
                self._events_spawn_y.setValue(int(data.y1))
            finally:
                self._events_syncing = False
            self._show_events_form(_EVT_ROW_SPAWN)
            return
        if row_type == _EVT_ROW_HITBOX:
            self._events_syncing = True
            try:
                self._events_hitbox_x1.setValue(int(data.x1))
                self._events_hitbox_y1.setValue(int(data.y1))
                self._events_hitbox_x2.setValue(int(data.x2))
                self._events_hitbox_y2.setValue(int(data.y2))
                self._events_hitbox_handler_label.setText(
                    data.dest_label or "—"
                )
            finally:
                self._events_syncing = False
            self._show_events_form(_EVT_ROW_HITBOX)
            return
        self._show_events_form("")

    # ---- Dialog panel ---------------------------------------------------

    def _resolve_dialog_entry_for_selection(
        self, block_offset: int, row_type: str,
    ) -> Tuple[int, int]:
        """Return ``(entry_ix, in_entry_off)`` to feed ``iter_dialogs_from``.

        Both ``sprite.string_ptr`` and ``hitbox.dst_file_off`` are absolute
        overlay file offsets — verified against entry 264 of DUSK where
        every sprite's string_ptr lives well past its own entry. Resolve
        either via :meth:`Overlay5Index.find_entry_containing`. Exits and
        spawns never own dialog blocks (fade+call handler / no handler).

        Returns ``(-1, -1)`` when no dialog pointer can be resolved.
        """
        if self._events_entry_ix < 0:
            return (-1, -1)
        idx = self._session.overlay5_index()
        if row_type == _EVT_ROW_SPRITE:
            entry = self._session.overlay5_entry_bytes(self._events_entry_ix)
            try:
                sprite = overlay5_mod.OverworldSpritePlacement.from_bytes(
                    entry, block_offset,
                )
            except (AssertionError, ValueError, IndexError):
                return (-1, -1)
            if sprite.string_ptr == 0:
                return (-1, -1)
            resolved = idx.find_entry_containing(int(sprite.string_ptr))
            if resolved is None:
                return (-1, -1)
            return resolved
        if row_type == _EVT_ROW_HITBOX:
            data = self._events_exit_index.get(block_offset)
            if data is None or data.dst_file_off == 0:
                return (-1, -1)
            resolved = idx.find_entry_containing(int(data.dst_file_off))
            if resolved is None:
                return (-1, -1)
            return resolved
        return (-1, -1)

    def _refresh_dialogs_panel(
        self, block_offset: int, row_type: str,
    ) -> None:
        """Re-scan dialogs for the selected object and re-populate the
        sidebar's dialog list + edit form. Always-visible: when the
        object has no dialog pointer or the pointer doesn't decode, the
        list shows a single placeholder row and the form is disabled —
        the surrounding sidebar geometry stays put on selection."""
        entry_ix, off = self._resolve_dialog_entry_for_selection(
            block_offset, row_type,
        )
        dialogs: List[overlay5_mod.DialogBlock] = []
        if entry_ix >= 0:
            try:
                entry = self._session.overlay5_entry_bytes(entry_ix)
                dialogs = overlay5_mod.iter_dialogs_from(entry, off)
            except (ValueError, KeyError):
                dialogs = []
        self._dialogs_for_selection = dialogs
        self._dialogs_entry_ix = entry_ix if dialogs else -1
        self._dialogs_list.blockSignals(True)
        self._dialogs_list.clear()
        if not dialogs:
            placeholder = QListWidgetItem(
                "(no dialogs)" if block_offset >= 0 else "(no selection)"
            )
            placeholder.setFlags(Qt.NoItemFlags)
            self._dialogs_list.addItem(placeholder)
        else:
            for i, d in enumerate(dialogs):
                self._dialogs_list.addItem(self._format_dialog_list_label(i, d))
        self._dialogs_list.blockSignals(False)
        if dialogs:
            self._dialogs_list.setCurrentRow(0)
        else:
            self._set_dialog_form_enabled(False)
            self._reset_dialog_form_values()

    def _set_dialog_form_enabled(self, enabled: bool) -> None:
        for w in (
            self._dialog_target_spin,
            self._dialog_msg_id_spin,
            self._dialog_portrait_combo,
            self._dialog_preview_text,
        ):
            w.setEnabled(enabled)

    def _reset_dialog_form_values(self) -> None:
        self._events_syncing = True
        try:
            self._dialog_target_spin.setValue(0)
            self._dialog_msg_id_spin.setValue(0)
            self._set_dialog_portrait_combo_value(0)
        finally:
            self._events_syncing = False
        self._refresh_dialog_preview(None, None)

    def _on_dialog_row_changed(self, row: int) -> None:
        if self._events_syncing:
            return
        if row < 0 or row >= len(self._dialogs_for_selection):
            self._set_dialog_form_enabled(False)
            self._reset_dialog_form_values()
            return
        d = self._dialogs_for_selection[row]
        self._events_syncing = True
        try:
            self._dialog_target_spin.setValue(int(d.target))
            self._dialog_msg_id_spin.setValue(int(d.msg_id))
            self._set_dialog_portrait_combo_value(int(d.portrait))
        finally:
            self._events_syncing = False
        self._set_dialog_form_enabled(True)
        self._refresh_dialog_preview(int(d.msg_id), int(d.portrait))

    def _set_dialog_portrait_combo_value(self, portrait_id: int) -> None:
        """Snap the portrait combobox to ``portrait_id``.

        Walks the shared ``sprite_map`` picker model for the row whose
        UserRole matches ``portrait_id``. If no slot exists for the id
        (e.g. a hand-edited dialog block targeting an out-of-table
        portrait), appends an ``(undefined 0xNNNN)`` row — the same
        fallback pattern :class:`BoundIdCombo` uses for shared models,
        bounded by the count of distinct unknown ids the session sees.
        """
        combo = self._dialog_portrait_combo
        target = int(portrait_id) & 0xFFFF
        for i in range(combo.count()):
            if combo.itemData(i, Qt.UserRole) == target:
                combo.setCurrentIndex(i)
                return
        combo.addItem(f"(undefined 0x{target:04x})", userData=target)
        combo.setCurrentIndex(combo.count() - 1)

    def _format_dialog_list_label(self, index: int, dialog) -> str:
        """Row label for the dialogs list.

        Format: ``#{i} 0x{portrait:03x} {name}  msg="{preview}"``. Portrait
        is the speaker — the same id the Portrait/Name combo drives — so
        resolving it via ``digimon_display_name`` covers digimon + digieggs
        + bosses + NPCs uniformly. Preview is the first chunk of the
        resolved MSG.PAK string with [BR] markers collapsed to spaces, so
        a list row reads "who says what" at a glance instead of three
        opaque hex ids.
        """
        portrait = int(dialog.portrait)
        name = self._session.digimon_display_name(portrait)
        gs = self._session.dialog_msg_text(int(dialog.msg_id))
        if gs is None:
            preview = "(no msg)"
        else:
            preview = gs.text.replace("[BR]", " ").replace("[END]", " ").strip()
            if len(preview) > 40:
                preview = preview[:40] + "…"
        return f"#{index}  0x{portrait:03x}  {name}  msg=\"{preview}\""

    def _on_dialog_portrait_combo_changed(self, _row: int) -> None:
        if self._events_syncing:
            return
        value = self._dialog_portrait_combo.currentData(Qt.UserRole)
        if value is None:
            return
        self._on_dialog_field_committed("portrait", int(value))

    def _refresh_dialog_preview(
        self, msg_id: Optional[int], portrait_id: Optional[int],
    ) -> None:
        """Repopulate the right-side preview (portrait + speaker name +
        msg text) for the current dialog. ``None`` clears the panel and
        is used when no dialog row is selected."""
        # Flush any pending text-commit against the *previous* GameString
        # before swapping bindings, otherwise mid-edit row switches would
        # silently lose the user's typing.
        if self._dialog_text_commit_timer.isActive():
            self._dialog_text_commit_timer.stop()
            self._commit_dialog_text_edit()

        if portrait_id is None:
            self._dialog_preview_portrait.setPixmap(QPixmap())
            self._dialog_preview_portrait.setText("")
            self._dialog_preview_name.setText("—")
        else:
            pid = int(portrait_id)
            self._dialog_preview_name.setText(
                self._session.digimon_display_name(pid)
            )
            icon = self._session.digimon_portrait_icon(pid)
            if icon is None:
                self._dialog_preview_portrait.setPixmap(QPixmap())
                self._dialog_preview_portrait.setText("?")
            else:
                self._dialog_preview_portrait.setText("")
                self._dialog_preview_portrait.setPixmap(
                    icon.pixmap(48, 48)
                )

        if msg_id is None:
            self._dialog_preview_string = None
            with QSignalBlocker(self._dialog_preview_text):
                self._dialog_preview_text.setPlainText("")
            self._dialog_preview_text.setPlaceholderText("(select a dialog)")
            self._dialog_preview_text.setEnabled(False)
            return

        gs = self._session.dialog_msg_text(int(msg_id))
        self._dialog_preview_string = gs
        if gs is None:
            with QSignalBlocker(self._dialog_preview_text):
                self._dialog_preview_text.setPlainText("")
            self._dialog_preview_text.setPlaceholderText(
                f"(no msgpak entry for msg 0x{int(msg_id):04x})"
            )
            self._dialog_preview_text.setEnabled(False)
            return

        with QSignalBlocker(self._dialog_preview_text):
            self._dialog_preview_text.setPlainText(
                gs.text.replace("[BR]", "\n")
            )
        self._dialog_preview_text.setEnabled(True)

    def _on_dialog_text_changed(self) -> None:
        if self._events_syncing or self._dialog_preview_string is None:
            return
        self._dialog_text_commit_timer.start()

    def _commit_dialog_text_edit(self) -> None:
        """Push a SetAttrCommand for the pending msg-text edit, if any."""
        gs = self._dialog_preview_string
        if gs is None or self._undo_stack is None:
            return
        new_text = (
            self._dialog_preview_text.toPlainText()
            .replace("\r\n", "[BR]")
            .replace("\r", "[BR]")
            .replace("\n", "[BR]")
        )
        if new_text == gs.text:
            return
        cmd = SetAttrCommand(
            gs,
            "text",
            new_text,
            description=f"Edit dialog msg 0x{int(self._dialog_msg_id_spin.value()):04x}",
            on_change=self._on_dialog_text_committed,
        )
        self._undo_stack.push(cmd)

    def _on_dialog_text_committed(self) -> None:
        gs = self._dialog_preview_string
        if gs is None:
            return
        display = gs.text.replace("[BR]", "\n")
        if self._dialog_preview_text.toPlainText() != display:
            with QSignalBlocker(self._dialog_preview_text):
                self._dialog_preview_text.setPlainText(display)

    def _on_dialog_field_committed(self, field: str, value: int) -> None:
        if self._events_syncing or self._undo_stack is None:
            return
        row = self._dialogs_list.currentRow()
        if row < 0 or row >= len(self._dialogs_for_selection):
            return
        d = self._dialogs_for_selection[row]
        old = getattr(d, field)
        new = int(value) & 0xFFFF
        if new == int(old):
            return
        entry_ix = self._dialogs_entry_ix
        block_offset = int(d.block_offset)
        cmd = EditDialogFieldCommand(
            self._session,
            entry_ix,
            block_offset,
            field,
            new,
            f"Edit dialog {field} (entry {entry_ix:04d}, off 0x{block_offset:04x})",
            on_change=lambda _v, ro=row, fd=field: self._on_dialog_changed(ro, fd),
        )
        self._undo_stack.push(cmd)

    def _on_dialog_changed(self, row: int, field: str) -> None:
        """Re-pull the dialog block after redo/undo so the cached list
        and the form spinboxes both reflect the new byte state."""
        if self._dialogs_entry_ix < 0:
            return
        try:
            entry = self._session.overlay5_entry_bytes(self._dialogs_entry_ix)
        except (ValueError, KeyError):
            return
        if row < 0 or row >= len(self._dialogs_for_selection):
            return
        old = self._dialogs_for_selection[row]
        try:
            updated = overlay5_mod.DialogBlock.from_bytes(
                entry, old.block_offset,
            )
        except (ValueError, IndexError):
            return
        self._dialogs_for_selection[row] = updated
        item = self._dialogs_list.item(row)
        if item is not None:
            item.setText(self._format_dialog_list_label(row, updated))
        if self._dialogs_list.currentRow() == row:
            self._events_syncing = True
            try:
                if field == "target":
                    self._dialog_target_spin.setValue(int(updated.target))
                elif field == "msg_id":
                    self._dialog_msg_id_spin.setValue(int(updated.msg_id))
                elif field == "portrait":
                    self._set_dialog_portrait_combo_value(int(updated.portrait))
            finally:
                self._events_syncing = False
            if field in ("msg_id", "portrait"):
                self._refresh_dialog_preview(
                    int(updated.msg_id), int(updated.portrait)
                )

    # ---- Events sidebar selection sync ----------------------------------

    def _on_event_canvas_sprite_selected(self, block_offset: int) -> None:
        """Canvas → sidebar: a sprite-marker click routes through the
        sprite form. ``block_offset == -1`` is the "no sprite selected"
        signal; we still ignore it if an exit/spawn is currently in
        focus, since both signals fire on every selection change."""
        if self._events_syncing:
            return
        if block_offset < 0:
            # Clear sidebar only if a sprite was the active selection;
            # otherwise the exit-selected handler is responsible.
            if self._events_selected_type == _EVT_ROW_SPRITE:
                self._events_selected_offset = -1
                self._events_selected_type = ""
                self._events_list.setCurrentRow(-1)
                self._show_events_form("")
                self._clear_event_fields()
                self._refresh_dialogs_panel(-1, "")
            return
        self._sync_list_to_selection(int(block_offset), _EVT_ROW_SPRITE)
        self._events_selected_offset = int(block_offset)
        self._events_selected_type = _EVT_ROW_SPRITE
        self._load_event_fields_for(int(block_offset), _EVT_ROW_SPRITE)
        self._refresh_dialogs_panel(int(block_offset), _EVT_ROW_SPRITE)

    def _on_event_canvas_exit_selected(self, block_offset: int) -> None:
        """Canvas → sidebar: an exit/spawn click routes through the
        exit or spawn form (chosen by the cached ``is_spawn`` flag)."""
        if self._events_syncing:
            return
        if block_offset < 0:
            if self._events_selected_type in (
                _EVT_ROW_EXIT, _EVT_ROW_SPAWN, _EVT_ROW_HITBOX,
            ):
                self._events_selected_offset = -1
                self._events_selected_type = ""
                self._events_list.setCurrentRow(-1)
                self._show_events_form("")
                self._clear_event_fields()
                self._refresh_dialogs_panel(-1, "")
            return
        data = self._events_exit_index.get(int(block_offset))
        if data and data.is_spawn:
            row_type = _EVT_ROW_SPAWN
        elif data and data.is_hitbox:
            row_type = _EVT_ROW_HITBOX
        else:
            row_type = _EVT_ROW_EXIT
        self._sync_list_to_selection(int(block_offset), row_type)
        self._events_selected_offset = int(block_offset)
        self._events_selected_type = row_type
        self._load_event_fields_for(int(block_offset), row_type)
        self._refresh_dialogs_panel(int(block_offset), row_type)

    def _sync_list_to_selection(
        self, block_offset: int, row_type: str,
    ) -> None:
        """Move the sidebar list's current row to the (offset, type)
        pair without firing the row-changed handler."""
        self._events_syncing = True
        try:
            for i in range(self._events_list.count()):
                item = self._events_list.item(i)
                if item is None:
                    continue
                if (int(item.data(Qt.UserRole)) == block_offset
                        and item.data(_EVT_ROW_TYPE_ROLE) == row_type):
                    self._events_list.setCurrentRow(i)
                    break
        finally:
            self._events_syncing = False

    def _on_event_list_row_changed(self, row: int) -> None:
        """Sidebar list → canvas: row pick focuses the matching object
        and switches the sidebar to the correct form page."""
        if self._events_syncing:
            return
        if row < 0:
            self._events_selected_offset = -1
            self._events_selected_type = ""
            self._show_events_form("")
            self._clear_event_fields()
            self._refresh_dialogs_panel(-1, "")
            return
        item = self._events_list.item(row)
        if item is None:
            return
        block_offset = int(item.data(Qt.UserRole))
        row_type = str(item.data(_EVT_ROW_TYPE_ROLE) or "")
        self._events_selected_offset = block_offset
        self._events_selected_type = row_type
        self._events_syncing = True
        try:
            if row_type == _EVT_ROW_SPRITE:
                self._events_canvas.select_marker(block_offset)
            elif row_type in (_EVT_ROW_EXIT, _EVT_ROW_SPAWN):
                self._events_canvas.select_exit(block_offset)
        finally:
            self._events_syncing = False
        self._load_event_fields_for(block_offset, row_type)
        self._refresh_dialogs_panel(block_offset, row_type)

    def _on_event_field_xy_committed(self) -> None:
        """Sidebar X/Y spinbox → MoveOverworldSpriteCommand."""
        if self._events_syncing or self._events_selected_offset < 0:
            return
        if self._current_id is None or self._undo_stack is None:
            return
        entry_ix = overlay5_mod.entry_ix_for_map(int(self._current_id))
        if entry_ix is None:
            return
        block_offset = self._events_selected_offset
        spec = self._events_marker_index.get(block_offset)
        new_x = int(self._events_field_x.value())
        new_y = int(self._events_field_y.value())
        if spec is not None and (new_x, new_y) == (spec.x, spec.y):
            return
        cmd = MoveOverworldSpriteCommand(
            self._session,
            entry_ix,
            block_offset,
            new_x,
            new_y,
            description=f"Edit overworld sprite xy @0x{block_offset:04x}",
            on_change=lambda x, y, off=block_offset:
                self._on_event_xy_applied(off, x, y),
        )
        self._undo_stack.push(cmd)

    def _on_event_field_id_committed(self) -> None:
        """Sidebar Sprite-ID spinbox → EditOverworldSpriteIdCommand."""
        if self._events_syncing or self._events_selected_offset < 0:
            return
        if self._current_id is None or self._undo_stack is None:
            return
        entry_ix = overlay5_mod.entry_ix_for_map(int(self._current_id))
        if entry_ix is None:
            return
        block_offset = self._events_selected_offset
        spec = self._events_marker_index.get(block_offset)
        value = self._events_field_id.currentData(Qt.UserRole)
        if value is None:
            return
        new_id = int(value) & 0xFFFF
        if spec is not None and new_id == spec.overworld_sprite_id:
            return
        cmd = EditOverworldSpriteIdCommand(
            self._session,
            entry_ix,
            block_offset,
            new_id,
            description=f"Edit overworld sprite id @0x{block_offset:04x}",
            on_change=lambda sid, off=block_offset:
                self._on_event_id_applied(off, sid),
        )
        self._undo_stack.push(cmd)

    def _on_event_xy_applied(
        self, block_offset: int, x: int, y: int,
    ) -> None:
        """Model→view sync after Move command (redo or undo)."""
        self._events_canvas.update_marker_position(block_offset, x, y)
        old = self._events_marker_index.get(block_offset)
        if old is not None:
            self._events_marker_index[block_offset] = EventMarkerSpec(
                block_offset=old.block_offset,
                overworld_sprite_id=old.overworld_sprite_id,
                x=x, y=y,
                label=old.label,
                pixmap=old.pixmap,
                behavior=old.behavior,
            )
        self._refresh_event_list_row(block_offset)
        if block_offset == self._events_selected_offset:
            self._events_syncing = True
            try:
                self._events_field_x.setValue(x)
                self._events_field_y.setValue(y)
            finally:
                self._events_syncing = False

    def _on_event_id_applied(
        self, block_offset: int, sprite_id: int,
    ) -> None:
        """Model→view sync after EditId command (redo or undo).

        Pulls a fresh label + pixmap for the new id and pushes them
        through the canvas + list row so the marker visually matches
        the new sprite.
        """
        sprite_map = getattr(self._session, "sprite_map", []) or []
        mchr_to_base = self._mchr_to_base_lookup(sprite_map)
        new_label = self._events_label_for(sprite_id, mchr_to_base)
        old = self._events_marker_index.get(block_offset)
        new_pixmap = self._events_pixmap_for(sprite_id)
        self._events_canvas.update_marker_sprite_id(
            block_offset, sprite_id, new_label, new_pixmap,
        )
        if old is not None:
            self._events_marker_index[block_offset] = EventMarkerSpec(
                block_offset=old.block_offset,
                overworld_sprite_id=int(sprite_id),
                x=old.x, y=old.y,
                label=new_label,
                pixmap=new_pixmap,
                behavior=old.behavior,
            )
        self._refresh_event_list_row(block_offset)
        if block_offset == self._events_selected_offset:
            self._events_syncing = True
            try:
                self._set_events_sprite_id_combo(int(sprite_id))
            finally:
                self._events_syncing = False

    def _on_event_field_frame_committed(self) -> None:
        """Sidebar Sprite-Frame spinbox → EditOverworldSpriteBehaviorCommand."""
        if self._events_syncing or self._events_selected_offset < 0:
            return
        if self._current_id is None or self._undo_stack is None:
            return
        entry_ix = overlay5_mod.entry_ix_for_map(int(self._current_id))
        if entry_ix is None:
            return
        block_offset = self._events_selected_offset
        spec = self._events_marker_index.get(block_offset)
        new_behavior = int(self._events_field_frame.value()) & 0xFFFF
        if spec is not None and new_behavior == int(spec.behavior):
            return
        cmd = EditOverworldSpriteBehaviorCommand(
            self._session,
            entry_ix,
            block_offset,
            new_behavior,
            description=f"Edit overworld sprite frame @0x{block_offset:04x}",
            on_change=lambda bv, off=block_offset:
                self._on_event_behavior_applied(off, bv),
        )
        self._undo_stack.push(cmd)

    def _on_event_behavior_applied(
        self, block_offset: int, behavior: int,
    ) -> None:
        """Model→view sync after EditBehavior command (redo or undo).

        Re-renders the marker pixmap at the new frame so the canvas
        visually reflects the edited behavior byte.
        """
        old = self._events_marker_index.get(block_offset)
        if old is None:
            return
        new_pixmap = self._events_pixmap_for(
            int(old.overworld_sprite_id), int(behavior),
        )
        self._events_canvas.update_marker_behavior(
            block_offset, int(behavior), new_pixmap,
        )
        self._events_marker_index[block_offset] = EventMarkerSpec(
            block_offset=old.block_offset,
            overworld_sprite_id=old.overworld_sprite_id,
            x=old.x, y=old.y,
            label=old.label,
            pixmap=new_pixmap,
            behavior=int(behavior) & 0xFFFF,
        )
        self._refresh_event_list_row(block_offset)
        if block_offset == self._events_selected_offset:
            self._events_syncing = True
            try:
                self._events_field_frame.setValue(int(behavior) & 0xFFFF)
            finally:
                self._events_syncing = False

    def _refresh_event_list_row(self, block_offset: int) -> None:
        """Re-render the sprite row matching ``block_offset`` — used
        after x/y or sprite-id edits so the row label and icon stay
        current. Filters by row type so the sprite refresh can't land
        on a coincidentally-equal exit row's UserRole."""
        spec = self._events_marker_index.get(block_offset)
        if spec is None:
            return
        for i in range(self._events_list.count()):
            item = self._events_list.item(i)
            if item is None:
                continue
            if int(item.data(Qt.UserRole)) != int(block_offset):
                continue
            if item.data(_EVT_ROW_TYPE_ROLE) != _EVT_ROW_SPRITE:
                continue
            self._events_syncing = True
            try:
                item.setText(
                    f"0x{spec.overworld_sprite_id:04x}  ({spec.x}, {spec.y})"
                )
                if spec.pixmap is not None and not spec.pixmap.isNull():
                    item.setIcon(QIcon(spec.pixmap))
                else:
                    pm = QPixmap(32, 32)
                    pm.fill(QColor(80, 80, 100))
                    item.setIcon(QIcon(pm))
                item.setToolTip(spec.label)
            finally:
                self._events_syncing = False
            break

    def _on_event_marker_moved(
        self, block_offset: int, new_x: int, new_y: int,
    ) -> None:
        """Drop-handler: push a :class:`MoveOverworldSpriteCommand`.

        The canvas already updated the marker's QGraphicsItem position
        optimistically; we mirror that into the model and rely on the
        command's ``on_change`` hook to keep the two in lockstep across
        redo/undo.
        """
        if self._current_id is None or self._undo_stack is None:
            return
        entry_ix = overlay5_mod.entry_ix_for_map(int(self._current_id))
        if entry_ix is None:
            return
        cmd = MoveOverworldSpriteCommand(
            self._session,
            entry_ix,
            block_offset,
            new_x,
            new_y,
            description=f"Move overworld sprite @0x{block_offset:04x}",
            on_change=lambda x, y, off=block_offset:
                self._on_event_xy_applied(off, x, y),
        )
        self._undo_stack.push(cmd)

    def _on_event_exit_moved(
        self,
        block_offset: int,
        new_x1: int, new_y1: int, new_x2: int, new_y2: int,
    ) -> None:
        """Drop-handler: push a :class:`EditExitBoxCommand` for a
        canvas-dragged exit / spawn item.

        The canvas translated the box by a tile-snapped delta and
        already moved the item; we mirror that into the model and let
        the command's ``on_change`` hook re-sync the canvas + sidebar
        spinboxes on redo/undo.
        """
        if self._current_id is None or self._undo_stack is None:
            return
        entry_ix = overlay5_mod.entry_ix_for_map(int(self._current_id))
        if entry_ix is None:
            return
        data = self._events_exit_index.get(block_offset)
        if data is not None and (
            new_x1, new_y1, new_x2, new_y2
        ) == (data.x1, data.y1, data.x2, data.y2):
            return
        if data is not None and data.is_spawn:
            description = f"Move spawn point @0x{block_offset:04x}"
        elif data is not None and data.is_hitbox:
            description = f"Move hitbox @0x{block_offset:04x}"
        else:
            description = f"Move exit zone @0x{block_offset:04x}"
        cmd = EditExitBoxCommand(
            self._session,
            entry_ix,
            block_offset,
            new_x1, new_y1, new_x2, new_y2,
            description=description,
            on_change=lambda x1, y1, x2, y2, off=block_offset:
                self._on_exit_box_applied(off, x1, y1, x2, y2),
        )
        self._undo_stack.push(cmd)

    # ---- Exit-zone commit handlers --------------------------------------

    def _on_event_field_box_committed(self) -> None:
        """Sidebar X1/Y1/X2/Y2 spinbox → :class:`EditExitBoxCommand`.

        Shared between the exit-zone and interaction-hitbox forms — the
        underlying 0x001b block layout is identical; only the active
        sidebar page differs."""
        if self._events_syncing or self._events_selected_offset < 0:
            return
        row_type = self._events_selected_type
        if row_type == _EVT_ROW_EXIT:
            spinboxes = (
                self._events_exit_x1, self._events_exit_y1,
                self._events_exit_x2, self._events_exit_y2,
            )
            kind = "exit zone"
        elif row_type == _EVT_ROW_HITBOX:
            spinboxes = (
                self._events_hitbox_x1, self._events_hitbox_y1,
                self._events_hitbox_x2, self._events_hitbox_y2,
            )
            kind = "hitbox"
        else:
            return
        if self._current_id is None or self._undo_stack is None:
            return
        entry_ix = overlay5_mod.entry_ix_for_map(int(self._current_id))
        if entry_ix is None:
            return
        block_offset = self._events_selected_offset
        data = self._events_exit_index.get(block_offset)
        new_box = tuple(int(sb.value()) for sb in spinboxes)
        if data is not None and new_box == (data.x1, data.y1, data.x2, data.y2):
            return
        cmd = EditExitBoxCommand(
            self._session,
            entry_ix,
            block_offset,
            *new_box,
            description=f"Edit {kind} box @0x{block_offset:04x}",
            on_change=lambda x1, y1, x2, y2, off=block_offset:
                self._on_exit_box_applied(off, x1, y1, x2, y2),
        )
        self._undo_stack.push(cmd)

    def _on_event_field_spawn_committed(self) -> None:
        """Sidebar spawn-point X/Y → :class:`EditExitBoxCommand` with
        degenerate corners (x1=x2, y1=y2). Same splice path as exits;
        the codec doesn't distinguish at the 0x001b block level."""
        if self._events_syncing or self._events_selected_offset < 0:
            return
        if self._events_selected_type != _EVT_ROW_SPAWN:
            return
        if self._current_id is None or self._undo_stack is None:
            return
        entry_ix = overlay5_mod.entry_ix_for_map(int(self._current_id))
        if entry_ix is None:
            return
        block_offset = self._events_selected_offset
        data = self._events_exit_index.get(block_offset)
        new_x = int(self._events_spawn_x.value())
        new_y = int(self._events_spawn_y.value())
        if data is not None and (new_x, new_y) == (data.x1, data.y1):
            return
        cmd = EditExitBoxCommand(
            self._session,
            entry_ix,
            block_offset,
            new_x, new_y, new_x, new_y,
            description=f"Move spawn point @0x{block_offset:04x}",
            on_change=lambda x1, y1, x2, y2, off=block_offset:
                self._on_exit_box_applied(off, x1, y1, x2, y2),
        )
        self._undo_stack.push(cmd)

    def _on_event_field_dest_committed(self, combo_ix: int) -> None:
        """Sidebar destination dropdown → :class:`EditExitDestinationCommand`.

        The combo's per-item user-data holds the file offset to write
        into the handler's op 0x0030 u32. ``_DEST_COMBO_CUSTOM`` is a
        no-op placeholder for handlers whose current destination isn't
        a known entry start (keeps the existing value visible without
        forcing a commit).
        """
        if self._events_syncing or self._events_selected_offset < 0:
            return
        if self._events_selected_type != _EVT_ROW_EXIT:
            return
        if combo_ix < 0 or self._undo_stack is None:
            return
        data = self._events_exit_index.get(self._events_selected_offset)
        if data is None or data.handler_entry_ix < 0:
            return
        new_dest = int(self._events_exit_dest.itemData(combo_ix))
        if new_dest == _DEST_COMBO_CUSTOM:
            return
        if new_dest == int(data.handler_dest):
            return
        block_offset = self._events_selected_offset
        cmd = EditExitDestinationCommand(
            self._session,
            data.handler_entry_ix,
            data.handler_rel_offset,
            new_dest,
            description=(
                f"Edit exit destination @0x{block_offset:04x} → "
                f"0x{new_dest:08x}"
            ),
            on_change=lambda dest, off=block_offset:
                self._on_exit_dest_applied(off, dest),
        )
        self._undo_stack.push(cmd)

    def _on_event_field_spawn_arg_committed(self) -> None:
        """Sidebar spawn-arg spinbox → :class:`EditExitSpawnArgCommand`."""
        if self._events_syncing or self._events_selected_offset < 0:
            return
        if self._events_selected_type != _EVT_ROW_EXIT:
            return
        if self._undo_stack is None:
            return
        data = self._events_exit_index.get(self._events_selected_offset)
        if data is None or data.handler_entry_ix < 0:
            return
        new_arg = int(self._events_exit_spawn_arg.value())
        if new_arg == int(data.handler_spawn_arg):
            return
        block_offset = self._events_selected_offset
        cmd = EditExitSpawnArgCommand(
            self._session,
            data.handler_entry_ix,
            data.handler_rel_offset,
            new_arg,
            description=(
                f"Edit exit spawn arg @0x{block_offset:04x} → 0x{new_arg:08x}"
            ),
            on_change=lambda arg, off=block_offset:
                self._on_exit_spawn_arg_applied(off, arg),
        )
        self._undo_stack.push(cmd)

    # ---- Exit-zone model→view appliers ----------------------------------

    def _on_exit_box_applied(
        self, block_offset: int, x1: int, y1: int, x2: int, y2: int,
    ) -> None:
        """Sync canvas + cached spec after EditExitBoxCommand redo/undo."""
        old = self._events_exit_index.get(block_offset)
        if old is None:
            return
        new_data = _ExitFormData(
            block_offset=old.block_offset,
            idx=old.idx,
            x1=x1, y1=y1, x2=x2, y2=y2,
            is_spawn=old.is_spawn,
            dst_file_off=old.dst_file_off,
            handler_entry_ix=old.handler_entry_ix,
            handler_rel_offset=old.handler_rel_offset,
            handler_dest=old.handler_dest,
            handler_spawn_arg=old.handler_spawn_arg,
            dest_map_id=old.dest_map_id,
            dest_label=old.dest_label,
            display_idx=old.display_idx,
            is_hitbox=old.is_hitbox,
        )
        self._events_exit_index[block_offset] = new_data
        self._events_canvas.update_exit_box(block_offset, x1, y1, x2, y2)
        self._refresh_exit_list_row(block_offset)
        if block_offset == self._events_selected_offset:
            self._events_syncing = True
            try:
                if old.is_spawn:
                    self._events_spawn_x.setValue(x1)
                    self._events_spawn_y.setValue(y1)
                elif old.is_hitbox:
                    self._events_hitbox_x1.setValue(x1)
                    self._events_hitbox_y1.setValue(y1)
                    self._events_hitbox_x2.setValue(x2)
                    self._events_hitbox_y2.setValue(y2)
                else:
                    self._events_exit_x1.setValue(x1)
                    self._events_exit_y1.setValue(y1)
                    self._events_exit_x2.setValue(x2)
                    self._events_exit_y2.setValue(y2)
            finally:
                self._events_syncing = False

    def _on_exit_dest_applied(
        self, block_offset: int, new_dest: int,
    ) -> None:
        """Sync canvas tooltip + cached spec after EditExitDestinationCommand.

        Any other exit block whose handler is the same shared script
        also picks up the new destination — they all share the same
        ``handler_entry_ix`` + ``handler_rel_offset``. Iterate the
        cache so co-selected exits update their dest_label too.
        """
        owner = self._events_exit_index.get(block_offset)
        if owner is None or owner.handler_entry_ix < 0:
            return
        shared_key = (owner.handler_entry_ix, owner.handler_rel_offset)
        dest_map_id, dest_label = self._resolve_dest_label(int(new_dest))
        for off, d in self._events_exit_index.items():
            if (d.handler_entry_ix, d.handler_rel_offset) != shared_key:
                continue
            self._events_exit_index[off] = _ExitFormData(
                block_offset=d.block_offset,
                idx=d.idx,
                x1=d.x1, y1=d.y1, x2=d.x2, y2=d.y2,
                is_spawn=d.is_spawn,
                dst_file_off=d.dst_file_off,
                handler_entry_ix=d.handler_entry_ix,
                handler_rel_offset=d.handler_rel_offset,
                handler_dest=int(new_dest),
                handler_spawn_arg=d.handler_spawn_arg,
                dest_map_id=dest_map_id,
                dest_label=dest_label,
                display_idx=d.display_idx,
                is_hitbox=d.is_hitbox,
            )
            self._events_canvas.update_exit_dest_label(off, dest_label)
            self._refresh_exit_list_row(off)
        if block_offset == self._events_selected_offset:
            self._events_syncing = True
            try:
                self._set_dest_combo_to(dest_map_id, int(new_dest))
                self._events_exit_handler_label.setText(
                    self._format_handler_label(
                        self._events_exit_index[block_offset]
                    )
                )
            finally:
                self._events_syncing = False

    def _on_exit_spawn_arg_applied(
        self, block_offset: int, new_arg: int,
    ) -> None:
        """Sync cached spec after EditExitSpawnArgCommand redo/undo.

        Shared-handler aware (same pattern as the destination apply).
        """
        owner = self._events_exit_index.get(block_offset)
        if owner is None or owner.handler_entry_ix < 0:
            return
        shared_key = (owner.handler_entry_ix, owner.handler_rel_offset)
        for off, d in self._events_exit_index.items():
            if (d.handler_entry_ix, d.handler_rel_offset) != shared_key:
                continue
            self._events_exit_index[off] = _ExitFormData(
                block_offset=d.block_offset,
                idx=d.idx,
                x1=d.x1, y1=d.y1, x2=d.x2, y2=d.y2,
                is_spawn=d.is_spawn,
                dst_file_off=d.dst_file_off,
                handler_entry_ix=d.handler_entry_ix,
                handler_rel_offset=d.handler_rel_offset,
                handler_dest=d.handler_dest,
                handler_spawn_arg=int(new_arg),
                dest_map_id=d.dest_map_id,
                dest_label=d.dest_label,
                display_idx=d.display_idx,
                is_hitbox=d.is_hitbox,
            )
        if block_offset == self._events_selected_offset:
            self._events_syncing = True
            try:
                self._events_exit_spawn_arg.setValue(
                    int(new_arg) & _U32_SPINBOX_MAX
                )
            finally:
                self._events_syncing = False

    def _refresh_exit_list_row(self, block_offset: int) -> None:
        """Re-render the exit/spawn row matching ``block_offset``.

        Filters by row type so a coincidentally-equal sprite UserRole
        can't get clobbered with an exit's text (block offsets across
        opcodes can in principle collide, even though they don't in
        practice — exits sit at the prologue, sprites at the body)."""
        data = self._events_exit_index.get(block_offset)
        if data is None:
            return
        for i in range(self._events_list.count()):
            item = self._events_list.item(i)
            if item is None:
                continue
            if int(item.data(Qt.UserRole)) != int(block_offset):
                continue
            row_type = item.data(_EVT_ROW_TYPE_ROLE)
            if data.is_spawn:
                expected = _EVT_ROW_SPAWN
            elif data.is_hitbox:
                expected = _EVT_ROW_HITBOX
            else:
                expected = _EVT_ROW_EXIT
            if row_type != expected:
                continue
            self._events_syncing = True
            try:
                if data.is_spawn:
                    item.setText(f"Spawn {data.display_idx}  ({data.x1}, {data.y1})")
                    item.setToolTip(
                        f"Spawn point (tile {data.x1}, {data.y1})"
                    )
                elif data.is_hitbox:
                    item.setText(
                        f"Hitbox {data.display_idx}  "
                        f"(tile {data.x1},{data.y1} — {data.x2},{data.y2})"
                    )
                    item.setToolTip(
                        f"Interaction hitbox (read-only)\n"
                        f"tile ({data.x1},{data.y1}) — ({data.x2},{data.y2})\n"
                        f"{data.dest_label or ''}".rstrip()
                    )
                else:
                    item.setText(
                        f"Exit {data.display_idx}  →  {data.dest_label or '?'}"
                    )
                    item.setToolTip(
                        f"Exit zone (tile {data.x1},{data.y1} — "
                        f"{data.x2},{data.y2})\n"
                        f"to: {data.dest_label or '(unknown)'}"
                    )
            finally:
                self._events_syncing = False
            break

    # ---- Exit destination helpers ---------------------------------------

    def _compute_exit_form_data(
        self, zones: List[overlay5_mod.ExitZone],
    ) -> List[_ExitFormData]:
        """Resolve each 0x001b zone into a sidebar-friendly form spec.

        For non-spawn zones, looks up which entry hosts the handler
        script (often the same entry, sometimes another) and decodes
        the handler's 0x0002 + 0x0030 prefix. Falls back to handler-
        less mode when the dst_file_off doesn't land on a recognized
        handler prefix — the box stays editable but the destination
        combo and spawn-arg spinbox are disabled.
        """
        out: List[_ExitFormData] = []
        index = self._session.overlay5_index()
        next_exit_display = 0
        next_hitbox_display = 0
        next_spawn_display = 0
        # Map exit = handler decodes as the fade+call prefix
        # (:class:`ExitHandler` — 0x0002 + 0x0030). Anything else with
        # non-zero dst is a bespoke interaction script: signs, locked
        # gates, NPC trigger zones — surfaced as read-only "hitboxes".
        # The EXIT_ZONE ``flag`` u16 is destination-specific (entry
        # 0259 carries 0x03/0x06/0x09 across three real map exits), so
        # it can't be used to gate the classification.
        for z in zones:
            if z.is_spawn:
                out.append(_ExitFormData(
                    block_offset=z.block_offset,
                    idx=z.idx,
                    x1=z.x1, y1=z.y1, x2=z.x2, y2=z.y2,
                    is_spawn=True,
                    display_idx=next_spawn_display,
                ))
                next_spawn_display += 1
                continue
            handler_entry_ix, handler_rel_offset = (
                self._resolve_handler_location(z.dst_file_off, index)
            )
            handler_dest = 0
            handler_arg = 0
            dest_map_id: Optional[int] = None
            dest_label = ""
            handler_ok = False
            if handler_entry_ix >= 0:
                try:
                    h_entry = self._session.overlay5_entry_bytes(handler_entry_ix)
                    handler = overlay5_mod.ExitHandler.from_bytes(
                        h_entry, handler_rel_offset,
                    )
                except (ValueError, KeyError):
                    handler = None
                if handler is None:
                    # Reachable address but the prefix isn't 0x0002 +
                    # 0x0030 — bespoke handler, treat as hitbox.
                    handler_entry_ix = -1
                    handler_rel_offset = -1
                else:
                    handler_ok = True
                    handler_dest = handler.dest_file_off
                    handler_arg = handler.spawn_arg
                    dest_map_id, dest_label = self._resolve_dest_label(
                        handler_dest, index,
                    )
            is_hitbox = not handler_ok
            if is_hitbox:
                hb_label = (
                    f"handler @entry {handler_entry_ix:04d} "
                    f"+0x{handler_rel_offset:04x}"
                    if handler_entry_ix >= 0
                    else f"dst=0x{z.dst_file_off:08x}"
                )
                out.append(_ExitFormData(
                    block_offset=z.block_offset,
                    idx=z.idx,
                    x1=z.x1, y1=z.y1, x2=z.x2, y2=z.y2,
                    is_spawn=False,
                    dst_file_off=z.dst_file_off,
                    handler_entry_ix=handler_entry_ix,
                    handler_rel_offset=handler_rel_offset,
                    handler_dest=handler_dest,
                    handler_spawn_arg=handler_arg,
                    dest_map_id=dest_map_id,
                    dest_label=hb_label,
                    display_idx=next_hitbox_display,
                    is_hitbox=True,
                ))
                next_hitbox_display += 1
                continue
            out.append(_ExitFormData(
                block_offset=z.block_offset,
                idx=z.idx,
                x1=z.x1, y1=z.y1, x2=z.x2, y2=z.y2,
                is_spawn=False,
                dst_file_off=z.dst_file_off,
                handler_entry_ix=handler_entry_ix,
                handler_rel_offset=handler_rel_offset,
                handler_dest=handler_dest,
                handler_spawn_arg=handler_arg,
                dest_map_id=dest_map_id,
                dest_label=dest_label,
                display_idx=next_exit_display,
            ))
            next_exit_display += 1
        return out

    def _resolve_handler_location(
        self, file_off: int,
        index: Optional[overlay5_mod.Overlay5Index] = None,
    ) -> Tuple[int, int]:
        """Find which overlay5 entry hosts ``file_off`` and return
        ``(entry_ix, rel_offset)``. Returns ``(-1, -1)`` when no entry
        contains the offset (out-of-range / corruption)."""
        if file_off <= 0:
            return -1, -1
        ix = index if index is not None else self._session.overlay5_index()
        for entry_ix, start in ix.entry_starts.items():
            end = ix.entry_ends[entry_ix]
            if start <= file_off < end:
                return entry_ix, file_off - start
        return -1, -1

    def _resolve_dest_label(
        self, file_off: int,
        index: Optional[overlay5_mod.Overlay5Index] = None,
    ) -> Tuple[Optional[int], str]:
        """Reverse-lookup ``file_off`` into ``(map_id, label)``.

        When ``file_off`` matches an entry start whose entry_ix maps to
        a field map, returns ``(map_id, "Map N (entry NNNN)")``;
        otherwise ``(None, "0x........ (raw)")``. The "raw" case
        surfaces in the dropdown as the Custom sentinel so the user
        can see the editor isn't ignoring the current value."""
        if file_off <= 0:
            return None, ""
        ix = index if index is not None else self._session.overlay5_index()
        for entry_ix, start in ix.entry_starts.items():
            if start != file_off:
                continue
            map_id = overlay5_mod.map_id_for(entry_ix)
            if map_id is not None:
                return map_id, f"Map {map_id} (entry {entry_ix:04d})"
            return None, f"entry {entry_ix:04d} (non-map)"
        return None, f"0x{file_off:08x} (raw)"

    def _build_dest_combo_once(self) -> None:
        """Populate the destination combo from ``overlay5_index.entry_starts``.

        One-shot: entry_starts is static per ROM, so the combo only
        needs to build the first time the events tab renders. Each
        item stores the entry start file offset in ``userData`` — the
        commit handler writes that into the handler's op 0x0030 u32.
        """
        if self._events_exit_dest_built:
            return
        self._events_syncing = True
        try:
            self._events_exit_dest.clear()
            self._events_exit_dest.addItem(
                "Custom (raw offset) — unchanged", _DEST_COMBO_CUSTOM,
            )
            index = self._session.overlay5_index()
            for entry_ix in sorted(index.entry_starts):
                mid = overlay5_mod.map_id_for(entry_ix)
                if mid is None:
                    continue
                file_off = index.entry_starts[entry_ix]
                self._events_exit_dest.addItem(
                    f"Map {mid} (entry {entry_ix:04d})", int(file_off),
                )
        finally:
            self._events_syncing = False
        self._events_exit_dest_built = True

    def _set_events_sprite_id_combo(self, sprite_id: int) -> None:
        """Snap the Sprite ID combo to the row for ``sprite_id``.

        Walks the shared ``mchr`` picker model for the row whose UserRole
        matches ``sprite_id``. If no MCHR slot maps to the id (e.g. a
        hand-edited overlay block pointing past the table), appends an
        ``(undefined 0xNNNN)`` row — the same fallback pattern
        :class:`BoundIdCombo` uses for shared models.
        """
        combo = self._events_field_id
        target = int(sprite_id) & 0xFFFF
        for i in range(combo.count()):
            if combo.itemData(i, Qt.UserRole) == target:
                combo.setCurrentIndex(i)
                return
        combo.addItem(f"(undefined 0x{target:04x})", userData=target)
        combo.setCurrentIndex(combo.count() - 1)

    def _set_dest_combo_to(
        self, map_id: Optional[int], file_off: int,
    ) -> None:
        """Move the destination combo to the row matching ``file_off``.

        Caller already holds ``_events_syncing`` while this runs. Maps
        the file offset → combo index via userData; falls back to the
        Custom row when no entry starts at that offset."""
        del map_id  # informational only — userData is the file offset
        for i in range(self._events_exit_dest.count()):
            if int(self._events_exit_dest.itemData(i)) == int(file_off):
                self._events_exit_dest.setCurrentIndex(i)
                return
        self._events_exit_dest.setCurrentIndex(0)

    def _format_handler_label(self, data: _ExitFormData) -> str:
        if data.handler_entry_ix < 0:
            return f"<no handler at 0x{data.dst_file_off:08x}>"
        return (
            f"entry {data.handler_entry_ix:04d} "
            f"+ 0x{data.handler_rel_offset:04x}"
        )

    def _mchr_to_base_lookup(self, sprite_map) -> Dict[int, int]:
        """Reverse ``sprite_map[base_id].unknown_0x4 -> base_id``.

        The overlay5 OVERWORLD_SPRITE field is an MCHR index; the engine
        finds the matching base species by scanning sprite_map for the
        row whose party-follower (``unknown_0x4``) points at that MCHR.
        Cached per map-browser instance — sprite_map is static per ROM.
        """
        cached = getattr(self, "_mchr_to_base_cache", None)
        if cached is not None and cached.get("_len") == len(sprite_map):
            return cached["_map"]
        rev: Dict[int, int] = {}
        for base_id, entry in enumerate(sprite_map):
            mchr_idx = getattr(entry, "unknown_0x4", None)
            if mchr_idx and mchr_idx > 0 and mchr_idx not in rev:
                rev[int(mchr_idx)] = base_id
        self._mchr_to_base_cache = {"_len": len(sprite_map), "_map": rev}
        return rev

    def _events_label_for(
        self, overworld_sprite_id: int, mchr_to_base: Dict[int, int],
    ) -> str:
        """Marker tooltip — base-species display name + hex MCHR id.

        Falls back to a bare ``<mchr 0x...>`` label when the MCHR id
        doesn't appear in sprite_map (genuinely unmapped sprites like
        prop discs, or table gaps).
        """
        # sprite_map is keyed by CHR index; resolve the ow-id through the
        # ARM9 table first so followers/NPCs still name-match (scene props
        # like the chest have no base id and fall through to the hex label).
        chr_idx = self._session.mchr_ow_to_chr.get(
            int(overworld_sprite_id), int(overworld_sprite_id)
        )
        base_id = mchr_to_base.get(chr_idx)
        if base_id is None:
            return f"<mchr 0x{overworld_sprite_id:04x}>"
        try:
            name = self._session.digimon_display_name(base_id)
        except (AttributeError, KeyError):
            name = f"<id {base_id}>"
        return f"{name} (0x{overworld_sprite_id:04x})"

    def _events_pixmap_for(
        self, overworld_sprite_id: int, behavior: Optional[int] = None,
    ) -> Optional[QPixmap]:
        """MCHR sprite render for ``overworld_sprite_id``, or ``None``.

        The script field is an *ow-id* (the 906-entry overworld-sprite space),
        not a CHR index — it resolves through the ARM9 table to a CHR graphic
        under that id's palette (see session.mchr_sprite_pixmap_by_ow_id), so
        e.g. object 0x2fb renders the chest (CHR 0x2ed) rather than the
        unrelated CHR-0x2fb graphic. Pass a max_size large enough that the
        downscale branch never fires — we want the native frame, matching the
        in-game pixel scale.

        ``behavior`` is the placement's u16 frame index; passing it lets
        the marker render the in-game pose instead of the canonical
        frame 3. ``None`` keeps the default (used by callers that don't
        carry a behavior — e.g. fallback / placeholder paths).
        """
        if overworld_sprite_id <= 0:
            return None
        try:
            return self._session.mchr_sprite_pixmap_by_ow_id(
                int(overworld_sprite_id), max_size=512,
                frame=behavior if behavior is None else int(behavior),
            )
        except (AttributeError, ValueError):
            return None

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
        if ix == self._TAB_WALK:
            # Route through the cached + hover-overlay path so zoom and
            # brush preview can stack on top of the same base pixmap.
            self._walk_current_preview = preview
            self._walk_scaled_base_pixmap = None
            self._refresh_walk_canvas(base_image=image)
            return
        label = self._label_for_tab(ix)
        label.setText("")
        label.setPixmap(QPixmap.fromImage(image))
        label.adjustSize()
