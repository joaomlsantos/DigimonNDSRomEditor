"""Battle-sprite browser for ``BTCHR.PAK``.

Read-only visualizer (v1):

* left list — 415 digimon labelled ``"NNNN  id=DDDD"`` where ``DDDD`` is
  the in-game id from ``chrsize.bin``.
* right pane — composite preview of the currently-selected cell, with a
  spinner to switch cells and a checkbox to show all cells side-by-side.

Editing lands in a follow-up. The widget is wired with an ``undo_stack``
reference now so the edit chunk doesn't have to touch the constructor
signature later.
"""
from __future__ import annotations

import os
import re
import struct
from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import (
    QColor, QFont, QImage, QPainter, QPen, QPixmap, QUndoStack, qRgba,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFrame,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from digimon_core import btchr, btchrspr, fnt, mchr, ncer as ncer_mod, pak, sprite

from ..runtime import is_admin
from ..commands import (
    AppendBtchrGroupCommand,
    PortBtchrSpriteCommand,
    ReplaceSpriteCommand,
)
from ._png_palette import (
    build_palette_from_png,
    intensity_matched_palette,
    nearest_idx_opaque,
)
from . import oam_edit_canvas
from .frame_align_canvas import FrameAlignCanvas
from .collapsible import CollapsibleSection
from .palette_batch_adjuster import PaletteBatchAdjuster
from .palette_editor import PaletteEditor
from .palette_grid import PaletteGrid
from .flow_layout import FlowLayout, make_height_for_width
from .form_helpers import ReflowHeightSync, add_unknown_form_row, wrap_tooltip
from .cell_png_io import (
    CellPngContext,
    CellPngError,
    build_palette_for_per_cell_import,
    cell_layout as shared_cell_layout,
    count_uncovered_content_composite,
    count_uncovered_content_per_cell,
    import_cells_to_tiles,
    import_per_cell_to_tiles,
    overlay_red_gaps_composite,
    overlay_red_gaps_single_cell,
    render_cells_qimage as shared_render_cells_qimage,
    render_one_cell_qimage as shared_render_one_cell_qimage,
)
from .record_list_panel import RecordListPanel
from .transparent_picker import TransparentColorPicker


BTCHR_PAK = "DAT/BTCHR.PAK"
CHRSIZE_PATH = "DAT/BTCHR/CHRSIZE.BIN"
PREVIEW_ZOOM = 2  # nearest-neighbor zoom so 64-pixel sprites are visible
# Cap on the palette scroll's height — the bank is taller than the pane at the
# minimum column count, so beyond this it scrolls instead of shoving the editor
# down. PALETTE_COLS_MIN is the fewest swatches/row; widening the pane adds more.
PALETTE_SCROLL_MAX_H = 360
PALETTE_SWATCH = 20
PALETTE_COLS_MIN = 8
# Sheet default picks a width based on the digimon's on-screen footprint
# so a 32×32 mini doesn't preview at 8 wide (lots of vertical padding) and
# a 96×96 boss doesn't preview at 4 wide (very tall narrow strip). User
# can still override via the spinner; range covers the largest tile counts
# so even a "single horizontal strip" mode is reachable.
SHEET_COLS_MAX = 4096


DEFAULT_SHEET_COLUMNS = 5  # one slab per cell — vanilla BTCHR has 5 cells


def _default_sheet_cols(bbox_w: int) -> int:
    """Default tile-sheet width (in tiles) from the sprite's bbox width.
    Same shape buckets as the sprite browser uses — ≤16 px → 2 tiles,
    17–63 → 4, ≥64 → 8. Capped at 8 even for huge sprites: the slab
    layout (5 columns by default) reads naturally at 8 tiles per slab,
    and bigger widths produce blocky chunks that don't divide cleanly
    into the per-cell mosaics users actually want to edit."""
    if bbox_w <= 0:
        return 8
    if bbox_w <= 16:
        return 2
    if bbox_w < 64:
        return 4
    return 8


def _load_chrsize_rows(session) -> List[tuple]:
    """Resolve and parse ``BTCHR/CHRSIZE.BIN`` from the session ROM bytes.

    Returns ``[(digimon_id, tile_count_div5), ...]`` per group, or an
    empty list if the file isn't in the ROM (defensive — shouldn't happen
    on vanilla but lets the widget still load on broken inputs).
    """
    try:
        ft = fnt.FileTable.from_rom(session.original_rom_data)
        start, end = ft.resolve(CHRSIZE_PATH)
        return btchr.parse_chrsize(session.original_rom_data[start:end])
    except (KeyError, ValueError):
        return []


def _load_chrsize_rows_live(session) -> List[tuple]:
    """Vanilla chrsize.bin parse, then overlay any in-memory edits.

    The widget caches chrsize rows on session open for the left-list
    label; after an in-editor port the cache is stale (target's tpf has
    changed). Overlaying ``session._chrsize_edits`` on top of the
    vanilla parse gives every reader the post-port view without forcing
    a session re-read.
    """
    rows = _load_chrsize_rows(session)
    edits = getattr(session, "_chrsize_edits", {})
    if not edits:
        return rows
    out = list(rows)
    for g, word in edits.items():
        if 0 <= g < len(out):
            out[g] = (word & 0xFFFF, (word >> 16) & 0xFFFF)
    return out


def _format_btchr_label(
    g: int,
    digimon_id: int,
    name: Optional[str],
    placeholder: bool = False,
) -> str:
    """Compose a BTCHR group list label.

    Format with name: ``"0x{g:04x} {name} [id=0x{digimon_id:04x}]"``.
    Format without name: ``"0x{g:04x} [id=0x{digimon_id:04x}]"``.
    Missing digimon id renders as ``[id=????]``. Sentinel/placeholder
    groups get a trailing ``(placeholder)`` marker.
    """
    id_token = f"[id=0x{digimon_id:04x}]" if digimon_id >= 0 else "[id=????]"
    name_token = f"{name} " if name else ""
    tag = " (placeholder)" if placeholder else ""
    return f"0x{g:04x}  {name_token}{id_token}{tag}"


def compute_btchr_group_labels(session) -> List[str]:
    """Public helper: BTCHR group label list. One per group (vanilla 415 +
    any appended). Used by other widgets (e.g. the enemy-digimon editor's
    main-sprite picker) so labels stay consistent across the app."""
    chrsize_rows = _load_chrsize_rows_live(session)
    # Frozen at session load — see RomSession.sprite_attribution. Means
    # reassigning a digimon's main_sprite later doesn't relabel the
    # original sprite.
    sprite_to_base = session.sprite_attribution()["main_sprite"]

    n_groups = session.vanilla_btchr_group_count() + len(session.btchr_appended_sidecars())
    out: List[str] = []
    for g in range(n_groups):
        digimon_id = chrsize_rows[g][0] if g < len(chrsize_rows) else -1
        base_id = sprite_to_base.get(g)
        name: Optional[str] = None
        if base_id is not None:
            resolved = session.digimon_display_name(base_id)
            if not resolved.startswith("<unnamed"):
                name = resolved
        out.append(_format_btchr_label(
            g, digimon_id, name,
            placeholder=(g in btchr.SENTINEL_GROUPS),
        ))
    return out


def scan_compressible_btchr(session, should_cancel=None, on_progress=None):
    """Re-cover every BTCHR group with occupied-only OAM coverage and collect
    the ones that actually shrink. Pure (no UI) so the header-bar menu action
    and a headless test share it: ``should_cancel()`` aborts early (the call
    then returns ``None``), ``on_progress(done, total)`` drives a progress bar.

    Returns ``(ports, stats)`` — ``ports`` is ``[(group, BtchrSprite)]`` ready
    for :class:`BatchCompressBtchrCommand`; ``stats`` carries ``old_sum`` /
    ``new_sum`` (footprint totals over the shrinking groups), the ``tight`` /
    ``declined`` skip counts, and ``savers`` = ``[(saved, group)]``.
    """
    pak_obj = session.sprite_pak(BTCHR_PAK)
    total = btchr.parse_pak_groups(pak_obj)
    ports = []
    old_sum = new_sum = 0
    tight = declined = 0
    savers = []
    for g in range(total):
        if should_cancel is not None and should_cancel():
            return None
        if on_progress is not None:
            on_progress(g, total)
        entries = [
            bytes(pak_obj.entries[g * btchr.GROUP_SIZE + i])
            for i in range(btchr.GROUP_SIZE)
        ]
        try:
            spr, old_fs, new_fs = btchrspr.compress_existing(entries)
        except ValueError:
            declined += 1
            continue
        if new_fs >= old_fs:
            tight += 1
            continue
        ports.append((g, spr))
        old_sum += old_fs
        new_sum += new_fs
        savers.append((old_fs - new_fs, g))
    return ports, {
        "old_sum": old_sum, "new_sum": new_sum,
        "tight": tight, "declined": declined, "savers": savers,
    }


class BtchrBrowser(QWidget):
    """Read-only browser for BTCHR battle sprites."""

    _CURSOR_KEY = "btchr_browser"

    def __init__(self, session, undo_stack: Optional[QUndoStack] = None, parent=None):
        super().__init__(parent)
        self._session = session
        self._undo_stack = undo_stack
        self._pak: pak.PakFile = session.sprite_pak(BTCHR_PAK)
        self._n_groups = btchr.parse_pak_groups(self._pak)
        self._chrsize_rows = _load_chrsize_rows_live(session)

        self._current_group: Optional[int] = None
        self._current_decoded: Optional[btchr.BtchrDigimon] = None
        self._current_cell: int = 0
        # group -> per-slot pixel histogram (for dominant-colour borrow match)
        self._group_counts_cache: dict[int, Optional[List[int]]] = {}
        # Non-None while the batch adjuster is dragging: a full palette with the
        # selected slots recoloured, used to render a live preview of the sprite
        # without committing. Cleared on Apply/Reset and on any re-decode.
        self._preview_palette: Optional[List[Tuple[int, int, int]]] = None
        # Per-group tile-sheet width memory. Switching away from a
        # digimon and back restores the user's last pick instead of
        # snapping back to the bbox-based default.
        self._sheet_cols_overrides: dict[int, int] = {}
        # Per-group "split into N columns" memory. Tile data is stored
        # linearly, but many BTCHR sprites pack multiple sub-sheets
        # (frames, cells) back-to-back — slicing the bank into N strips
        # and placing them side-by-side often lines up sub-sprites
        # neatly without going through OAM rendering.
        self._sheet_columns_overrides: dict[int, int] = {}
        # Per-group fill-direction memory. False = top-to-bottom (slab 0
        # gets the first chunk of tiles), True = left-to-right (tiles
        # flow across slabs row-by-row before wrapping). LTR is right
        # when the tile bank is stored screen-row-major and the slab
        # layout is purely a viewing convenience.
        self._sheet_fill_ltr_overrides: dict[int, bool] = {}
        # Per-group view-mode memory. "cells" composes each NCER cell via
        # OAM (assembled character per slab — much easier to edit), "tiles"
        # shows raw 8×8 tile data (legacy slab grid). Cells mode is the
        # default because users almost always want to see the assembled
        # character, not the underlying tile layout.
        self._view_mode_overrides: dict[int, str] = {}

        # Animation playback state. ``_anim_flat`` is one entry per
        # output tick (see :func:`btchr.flatten_anim_track`); the timer
        # drives ``_anim_pos`` forward and ``_refresh_preview`` reads
        # ``_current_cell`` so playback rides the existing render path.
        # Default FPS = 60 to match NDS vblank — DWDD's mini-header
        # durations behave as 60Hz tick counts in-game.
        self._anim_track_key: str = "idle"
        self._anim_flat: List[Tuple[int, int]] = []
        self._anim_pos: int = 0
        self._anim_fps: int = 60

        # Cached cell QPixmaps for the *current* digimon. Cleared on
        # selection change.
        self._cell_pixmaps: List[Optional[QPixmap]] = []
        # Sheet preview is lazy — `setPixel` x ~120k tiles is slow in
        # Python, so only re-render when the user actually views the tab.
        self._sheet_dirty: bool = True
        # OAM map is likewise lazy (per-pixel union render).
        self._oam_dirty: bool = True

        # Preview source caches for the transparent-colour picker. The
        # picker reads these via the bound source-provider callbacks on
        # click. Stored here rather than inside the picker because each
        # preview re-renders independently and we want the picker to read
        # the fresh image without re-binding.
        self._cells_src_qimage: Optional[QImage] = None
        self._cells_src_size: Tuple[int, int] = (0, 0)
        self._cells_pix_size: Tuple[int, int] = (0, 0)
        self._sheet_src_qimage: Optional[QImage] = None
        self._sheet_src_size: Tuple[int, int] = (0, 0)
        self._sheet_pix_size: Tuple[int, int] = (0, 0)

        # Reverse lookup: BTCHR group index -> first sprite-map list
        # position pointing at it. SpriteMapEntry.main_sprite carries
        # the battle-sprite (BTCHR group) index, and the entry's list
        # position is the key into DIGIMON_ID_TO_STR — so this join is
        # what turns "group 2" into "Koromon". Multiple recolors can
        # share a battle sprite; setdefault keeps the first (canonical)
        # name. Groups whose battle sprite isn't referenced by any
        # entry fall through to "id=NNNN" only.
        self._sprite_to_base: dict[int, int] = {}
        for base_id, entry in enumerate(getattr(session, "sprite_map", [])):
            self._sprite_to_base.setdefault(entry.main_sprite, base_id)

        self._labels: List[str] = self._build_labels()

        self._build_ui()
        remembered = self._session.recall_selection(self._CURSOR_KEY)
        if remembered is None or not self._list.select_index(int(remembered)):
            self._list.select_first()

    # ---- labels ---------------------------------------------------------

    def _name_for_group(self, g: int) -> Optional[str]:
        """Human name for BTCHR group ``g`` via the sprite-map cross-
        reference. Routes through ``digimon_display_name`` so bosses /
        NPCs surface their battle-string name (e.g. group 0x192 →
        sprite_map slot 0x1f9 → "OphanimonC"). Returns None when no
        entry points at this group or the resolver can't name the slot."""
        base_id = self._sprite_to_base.get(g)
        if base_id is None:
            return None
        name = self._session.digimon_display_name(base_id)
        return None if name.startswith("<unnamed") else name

    def _build_labels(self) -> List[str]:
        out: List[str] = []
        for g in range(self._n_groups):
            digimon_id = (
                self._chrsize_rows[g][0]
                if g < len(self._chrsize_rows) else -1
            )
            out.append(_format_btchr_label(
                g, digimon_id, self._name_for_group(g),
                placeholder=(g in btchr.SENTINEL_GROUPS),
            ))
        return out

    # ---- UI -------------------------------------------------------------

    def _build_ui(self) -> None:
        self._list = RecordListPanel(
            records=list(range(self._n_groups)),
            label_for=lambda g, _rec: self._labels[g],
        )
        self._list.indexSelected.connect(self._on_group_selected)

        self._preview = QLabel("Select a digimon to preview.")
        self._preview.setAlignment(Qt.AlignCenter)
        self._preview.setMinimumSize(320, 320)

        self._scroll = QScrollArea()
        self._scroll.setWidget(self._preview)
        self._scroll.setWidgetResizable(False)
        self._scroll.setAlignment(Qt.AlignCenter)

        self._cell_spin = QSpinBox()
        self._cell_spin.setRange(0, 0)
        self._cell_spin.valueChanged.connect(self._on_cell_changed)

        self._show_all_cells = QCheckBox("Show all cells (strip)")
        self._show_all_cells.toggled.connect(self._on_show_all_toggled)

        # OAM-gap toggles — kept always-visible so the footer layout
        # doesn't shift when switching between sprites with/without
        # gaps; disabled (grayed out) instead when the current sprite
        # has no gaps. Tooltips carry the "what does this mean" detail
        # since the labels themselves can't fit a full explanation and
        # "red overlay" alone doesn't convey the semantics.
        self._show_red_overlay_cb = QCheckBox("Highlight OAM coverage gaps")
        self._show_red_overlay_cb.setChecked(True)
        self._show_red_overlay_cb.setToolTip(wrap_tooltip(
            "Some sprites leave regions inside their bounding box that "
            "no OAM references. Content painted there (via PNG import) "
            "is written into tile storage but never rendered in-game. "
            "When enabled, those regions are tinted red on the cells "
            "and tile-sheet previews."
        ))
        self._show_red_overlay_cb.toggled.connect(
            self._on_show_red_overlay_toggled
        )

        self._bake_red_overlay_cb = QCheckBox(
            "Include OAM coverage gaps in exported PNGs"
        )
        self._bake_red_overlay_cb.setChecked(False)
        self._bake_red_overlay_cb.setToolTip(wrap_tooltip(
            "When enabled, exported tile-sheet and per-cell PNGs will "
            "have the red gap highlight painted into the image itself, "
            "so an external image editor also shows where content "
            "would be lost on re-import. Leave unchecked for a "
            "lossless round-trip."
        ))

        # Tile-sheet tab widgets.
        self._sheet_preview = QLabel("Select a digimon.")
        self._sheet_preview.setAlignment(Qt.AlignCenter)
        self._sheet_preview.setMinimumSize(320, 320)

        self._sheet_scroll = QScrollArea()
        self._sheet_scroll.setWidget(self._sheet_preview)
        self._sheet_scroll.setWidgetResizable(False)
        self._sheet_scroll.setAlignment(Qt.AlignCenter)

        # View mode: cells (OAM-composed) vs tiles (raw 8×8 grid). Cells
        # mode pulls each NCER cell through `render_cell_rgba` so each
        # "slab" is a fully-assembled character/frame — what users
        # actually want to edit. Tiles mode keeps the raw-tile slab view
        # for sprite-format inspection and edge cases where OAM doesn't
        # cover every tile.
        self._view_mode_combo = QComboBox()
        self._view_mode_combo.addItem("Cells (OAM)", "cells")
        self._view_mode_combo.addItem("Raw tiles", "tiles")
        self._view_mode_combo.currentIndexChanged.connect(
            self._on_view_mode_changed
        )

        self._sheet_cols_spin = QSpinBox()
        self._sheet_cols_spin.setRange(1, SHEET_COLS_MAX)
        self._sheet_cols_spin.setValue(1)  # placeholder; set per-group below
        self._sheet_cols_spin.valueChanged.connect(self._on_sheet_cols_changed)

        # Number of sub-sheet "columns" laid side-by-side. Defaults to 5
        # because every vanilla BTCHR sprite has 5 cells (header /
        # NCGR-8bpp / NCLR-256 / NCER-5cells / NANR — project memory
        # `project_btchr_format`), and slabbing the tile bank into 5
        # parts lines up one cell's tile range per slab. User can
        # change it for non-standard sprites or to inspect raw layout.
        self._sheet_columns_spin = QSpinBox()
        self._sheet_columns_spin.setRange(1, 64)
        self._sheet_columns_spin.setValue(DEFAULT_SHEET_COLUMNS)
        self._sheet_columns_spin.valueChanged.connect(
            self._on_sheet_columns_changed
        )

        self._sheet_fill_ltr_cb = QCheckBox("Fill left-to-right")
        self._sheet_fill_ltr_cb.setToolTip(
            "Off: each slab is a contiguous chunk of tiles, filled top-"
            "to-bottom (slab 0 = first chunk, slab 1 = next, …).\n"
            "On: tiles flow row-major across all slabs first, then wrap. "
            "Right when the bank is stored screen-row-major and the "
            "character composes naturally at the wider grid."
        )
        self._sheet_fill_ltr_cb.toggled.connect(self._on_sheet_fill_changed)

        self._export_pal_btn = QPushButton("Export palette PNG…")
        self._export_pal_btn.clicked.connect(self._on_export_palette_png)
        self._import_pal_btn = QPushButton("Import palette PNG…")
        self._import_pal_btn.clicked.connect(self._on_import_palette_png)

        # Live palette editor (same widgets as the icons/portraits/ui browser):
        # click a swatch to select, edit R/G/B + hex or the colour picker below.
        # 8 colours per row with larger swatches — easier to see and click for
        # recolour work (the 256-colour bank scrolls; the pane is collapsible).
        self._palette_grid = PaletteGrid(cols=PALETTE_COLS_MIN, swatch=PALETTE_SWATCH)
        self._palette_grid.set_select_mode(True)
        self._palette_grid.set_multi_select(True)
        self._palette_grid.setToolTip(wrap_tooltip(
            "The sprite's 256-colour palette. Click a swatch to edit one colour "
            "below; shift-click a range or drag a box to select several, then "
            "shift them together with the H/S/L sliders (great for recolours). "
            "Colours quantise to NDS 5-bit BGR555 on save. Slot 0 (diagonal "
            "mark) is the transparent index."
        ))
        self._palette_grid.colorEdited.connect(self._apply_palette_color)
        self._palette_editor = PaletteEditor()
        self._palette_editor.colorEdited.connect(self._apply_palette_color)
        self._palette_grid.selectedChanged.connect(
            lambda s: self._palette_editor.set_slot(s, self._palette_grid.color_at(s))
        )
        # Batch recolour: shift H/S/L of every selected swatch by the same delta.
        self._palette_adjuster = PaletteBatchAdjuster()
        self._palette_grid.selectionChanged.connect(self._refresh_palette_adjuster)
        self._palette_adjuster.previewChanged.connect(self._on_palette_preview)
        self._palette_adjuster.committed.connect(self._apply_palette_colors)
        # Coalesces rapid slider ticks into ~30 fps sprite re-renders so the
        # live recolour preview stays smooth during a drag.
        self._live_preview_timer = QTimer(self)
        self._live_preview_timer.setSingleShot(True)
        self._live_preview_timer.setInterval(33)
        self._live_preview_timer.timeout.connect(self._refresh_preview)

        # Transparent-color picker — palette slot 0 of the live NCLR. The
        # engine honors index 0 as the transparent slot regardless of its
        # RGB, so changing the colour here mostly affects PNG round-trips
        # and external NCLR-aware tools. The hidden second effect (and the
        # one users actually want most of the time): if the picked RGB
        # already lives at some other slot K, `_apply_transparent_color`
        # also remaps every NCGR pixel that pointed at K to 0 so it
        # actually becomes transparent. Without that remap the engine
        # still draws those pixels — only the slot-0 RGB drives
        # transparency, not the colour.
        self._picker = TransparentColorPicker(
            on_color_picked=self._apply_transparent_color,
            on_slot_picked=self._pick_slot_from_rgb,
        )
        # Eyedropper lives in the palette sidebar (distinct from the single-slot
        # "Colour picker…" dialog). Click capture is the shared picker's slot-pick
        # mode; this checkable button drives it and stays in sync both ways.
        self._eyedropper_btn = QPushButton("Eyedropper")
        self._eyedropper_btn.setCheckable(True)
        self._eyedropper_btn.setToolTip(wrap_tooltip(
            "Then click a pixel on the sprite preview to select its palette "
            "slot — handy for finding which slot a given shade uses."
        ))
        self._eyedropper_btn.toggled.connect(self._picker.set_slot_pick_mode)
        self._picker.slotPickModeChanged.connect(self._eyedropper_btn.setChecked)

        self._export_sheet_btn = QPushButton("Export tile sheet PNG…")
        self._export_sheet_btn.clicked.connect(self._on_export_sheet_png)
        self._import_sheet_btn = QPushButton("Import tile sheet PNG…")
        self._import_sheet_btn.clicked.connect(self._on_import_sheet_png)
        # Per-cell IO: 5 separate PNGs (one per cell) instead of a single
        # composite. Same OAM-walk codec as the composite cells mode, just
        # sourcing pixels from N independent files. The on-disk PNGs are
        # all sized to the union bbox over all 5 cells — guarantees the
        # "all cells must be the same size" invariant the engine relies
        # on, and lets the user copy frames between digimon freely.
        # Composite cells-sheet IO: export renders all cells side by side
        # into one PNG (the OAM view the Tile-sheet tab shows); import
        # reads that composite back. Export is read-only; import writes
        # tiles (and optionally the palette) but never the OAM layout —
        # the cells codec paints into the existing OAM rectangles, it
        # doesn't move them.
        self._export_cells_sheet_btn = QPushButton("Export cells sheet PNG…")
        self._export_cells_sheet_btn.setToolTip(wrap_tooltip(
            "Save all cells composited side by side into a single PNG — "
            "the same image the Tile-sheet tab shows in Cells (OAM) "
            "view. Read-only: it does not modify the sprite, tiles, or "
            "OAM."
        ))
        self._export_cells_sheet_btn.clicked.connect(
            self._on_export_cells_sheet_png
        )
        self._import_cells_sheet_btn = QPushButton("Import cells sheet PNG…")
        self._import_cells_sheet_btn.setToolTip(wrap_tooltip(
            "Read a cells-composite PNG (as written by Export cells "
            "sheet PNG) back into the sprite's tiles. Paints into the "
            "existing OAM rectangles — cell count and OAM layout are "
            "unchanged; only the tile pixels (and optionally the "
            "palette) are rewritten."
        ))
        self._import_cells_sheet_btn.clicked.connect(
            self._on_import_cells_sheet_png
        )
        self._export_per_cell_btn = QPushButton("Export per-cell PNGs…")
        self._export_per_cell_btn.clicked.connect(self._on_export_per_cell_pngs)
        self._import_per_cell_btn = QPushButton("Import per-cell PNGs…")
        self._import_per_cell_btn.clicked.connect(self._on_import_per_cell_pngs)
        # Custom-OAM import: unlike the two above (which repaint pixels
        # *through* the existing OAM layout), this generates a fresh OAM
        # layout to fit each imported cell's dimensions and rebuilds the whole
        # sprite kit — so you can import differently-shaped/animated sprites.
        self._import_custom_cells_btn = QPushButton("Import cells → new OAM…")
        self._import_custom_cells_btn.setToolTip(wrap_tooltip(
            "Import one PNG per cell and rebuild the sprite with a freshly "
            "generated OAM layout sized to each image (not limited to the "
            "current sprite's shape). Cells share one concatenated tile bank; "
            "the tpf / btchrsize sidecars are recomputed. Sprites over 512 "
            "tiles/cell garble everywhere (party viewer, gallery, battles) — "
            "you'll be warned."
        ))
        self._import_custom_cells_btn.clicked.connect(self._on_import_custom_cells)

        # Consolidated sprite-sheet IO: one Export / one Import fronting the
        # cells-sheet (composite) and per-cell paths, switched by a "Separate
        # frames" toggle. The old four buttons stay in code, just hidden.
        self._separate_frames_cb = QCheckBox("Separate frames (one PNG per cell)")
        self._separate_frames_cb.setToolTip(wrap_tooltip(
            "Off: the sprite sheet is one PNG with every frame composited side "
            "by side. On: one PNG per frame. Applies to both Export and Import."
        ))
        self._export_sprite_sheet_btn = QPushButton("Export sprite sheet…")
        self._export_sprite_sheet_btn.setToolTip(wrap_tooltip(
            "Save the sprite's frames to PNG (read-only). Tick 'Separate frames' "
            "for one PNG per frame instead of a single composite."
        ))
        self._export_sprite_sheet_btn.clicked.connect(self._on_export_sprite_sheet)
        self._import_sprite_sheet_btn = QPushButton("Import sprite sheet…")
        self._import_sprite_sheet_btn.setToolTip(wrap_tooltip(
            "Repaint the sprite's art from a PNG (as written by Export sprite "
            "sheet). Paints into the current OAM layout — cell count and OAM "
            "rectangles are unchanged. Tick 'Separate frames' for one PNG per "
            "frame. To reshape the sprite, use 'Import cells → new OAM' instead."
        ))
        self._import_sprite_sheet_btn.clicked.connect(self._on_import_sprite_sheet)

        # Compress OAM: re-cover THIS sprite with occupied-only coverage (same
        # pixels, tighter tile bank) to cut its footprint_scale — no art. Lets
        # a sprite that overflows a VRAM budget (party pool / wild-spawn Σfs)
        # drop under it. No-ops when the OAM is already tight.
        self._compress_oam_btn = QPushButton("Compress OAM…")
        self._compress_oam_btn.setToolTip(wrap_tooltip(
            "Rebuild this sprite's OAM to cover only its non-transparent tiles "
            "— identical pixels, fewer tiles per frame (lower footprint_scale). "
            "Sparse silhouettes (wings, long bodies) shrink a lot; it lets them "
            "fit VRAM budgets they currently exceed. Does nothing when the OAM "
            "is already tight."
        ))
        self._compress_oam_btn.clicked.connect(self._on_compress_oam)

        # Fit-to-512: lossless union re-cover first, and only if that still
        # overflows the party-viewer cap (512 tiles/cell), trim the minimum
        # faint edge tiles needed to fit — showing the pixel cost for approval.
        self._compress_oam_fit_btn = QPushButton("Compress OAM (fit ≤512)…")
        self._compress_oam_fit_btn.setToolTip(wrap_tooltip(
            "Re-cover losslessly; if the sprite still just misses the 512-tile "
            "party-viewer cap, trim the fewest faint edge pixels needed to fit "
            "and show the exact cost first. Use for a boss that lands a little "
            "over 512 (same gallery-safe union layout, just a hair smaller)."
        ))
        self._compress_oam_fit_btn.clicked.connect(self._on_compress_oam_fit)

        # .btchrspr: portable single-digimon sprite kit. Export packs the 5
        # PAK entries + the two sidecar u32s into one file; import replays
        # them onto the selected slot (keeping the slot's secondary id).
        # The whole port (5 PAK entries + chrsize.tpf + btchrsize) lands as
        # one undo step.
        self._export_btchrspr_btn = QPushButton("Export .btchrspr…")
        self._export_btchrspr_btn.clicked.connect(self._on_export_btchrspr)
        self._import_btchrspr_btn = QPushButton("Import .btchrspr…")
        self._import_btchrspr_btn.clicked.connect(self._on_import_btchrspr)
        # In-context duplicate: same op as the list's "+ Add Entry" button
        # but acts on the currently-selected group directly (no extra
        # picker step).
        self._duplicate_entry_btn = QPushButton("Duplicate entry")
        self._duplicate_entry_btn.setToolTip(
            "Append a new BTCHR group at the end of the list carrying a "
            "copy of this sprite's data. Equivalent to selecting + Add "
            "Entry below the list with this group selected."
        )
        self._duplicate_entry_btn.clicked.connect(self._on_add_entry)

        # "Export as…" / "Import as…" dropdowns front every format & mode, so
        # nothing has to be hidden behind a toggle and a new option is just one
        # more menu row. Each row calls the existing per-format handler.
        self._export_menu = QMenu(self)
        self._export_menu.addAction(
            "Sprite sheet (all frames, one PNG)…", self._on_export_cells_sheet_png)
        self._export_menu.addAction(
            "Individual frames (one PNG per frame)…", self._on_export_per_cell_pngs)
        self._export_menu.addSeparator()
        self._export_menu.addAction(
            "Tile sheet (raw 8×8 tiles)…", self._on_export_sheet_png)
        self._export_menu.addAction("Palette (PNG)…", self._on_export_palette_png)
        self._export_menu.addSeparator()
        self._export_menu.addAction(
            ".btchrspr (portable sprite kit)…", self._on_export_btchrspr)
        # Raw Nitro components in a submenu so the top menu stays short.
        export_src = self._export_menu.addMenu("Source files (NCGR/NCLR/…)")
        export_src.addAction(
            "All components (to a folder)…", self._on_export_all_sources)
        export_src.addSeparator()
        for _i, _name, _ext in self._SOURCE_COMPONENTS:
            export_src.addAction(
                f"{_name}…",
                lambda _checked=False, i=_i, n=_name, e=_ext:
                    self._on_export_source(i, n, e),
            )
        self._export_as_btn = QPushButton("Export…")
        self._export_as_btn.setMenu(self._export_menu)

        # The sprite-sheet / individual-frames rows honour the "Build new OAM
        # layout on import" checkbox: on → rebuild the OAM sized to the images
        # (reshape, obsoletes the old "Import cells → new OAM" button); off →
        # paint into the current OAM rectangles.
        self._import_menu = QMenu(self)
        self._import_menu.addAction(
            "Sprite sheet (all frames, one PNG)…",
            self._on_import_cells_sheet_png)
        self._import_menu.addAction(
            "Individual frames (one PNG per frame)…",
            self._on_import_per_cell_pngs)
        self._import_menu.addSeparator()
        self._import_menu.addAction(
            "Tile sheet (raw 8×8 tiles)…", self._on_import_sheet_png)
        self._import_menu.addAction("Palette (PNG)…", self._on_import_palette_png)
        self._import_menu.addSeparator()
        self._import_menu.addAction(
            ".btchrspr (portable sprite kit)…", self._on_import_btchrspr)
        # Raw Nitro components (re-derives fs/btchrsize to stay consistent).
        import_src = self._import_menu.addMenu("Source files (NCGR/NCLR/…)")
        for _i, _name, _ext in self._SOURCE_COMPONENTS:
            import_src.addAction(
                f"{_name}…",
                lambda _checked=False, i=_i, n=_name, e=_ext:
                    self._on_import_source(i, n, e),
            )
        self._import_as_btn = QPushButton("Import…")
        self._import_as_btn.setMenu(self._import_menu)

        # Every individual IO button is now a row on those two menus — kept in
        # code (nothing deleted), just not laid out. "Import cells → new OAM"
        # is folded into the Import dropdown + the Build-OAM checkbox below.
        for _superseded in (
            self._export_sheet_btn, self._import_sheet_btn,
            self._export_cells_sheet_btn, self._import_cells_sheet_btn,
            self._export_per_cell_btn, self._import_per_cell_btn,
            self._export_sprite_sheet_btn, self._import_sprite_sheet_btn,
            self._separate_frames_cb, self._compress_oam_fit_btn,
            self._export_pal_btn, self._import_pal_btn,
            self._export_btchrspr_btn, self._import_btchrspr_btn,
            self._import_custom_cells_btn,
        ):
            _superseded.setParent(self)
            _superseded.setVisible(False)

        # When on, an Indexed8 import also rebuilds the NCLR from the PNG's
        # embedded color table — matches the natural Aseprite/GIMP workflow
        # where the working palette ships inside the file.
        self._import_pal_with_sheet_cb = QCheckBox("Also import palette from PNG")
        self._import_pal_with_sheet_cb.setChecked(True)
        self._import_pal_with_sheet_cb.setToolTip(
            "Treat the PNG as the source of truth for colours.\n"
            "  Indexed-8 PNG: rebuild the NCLR from its embedded palette.\n"
            "  RGB/RGBA PNG: median-cut a fresh 256-colour palette from "
            "the PNG's opaque pixels and re-index.\n"
            "Off: index against the existing NCLR (colours may posterize)."
        )

        # Applies to the two "Sprite sheet" / "Individual frames" import rows.
        # On (default): rebuild a fresh OAM layout sized to the imported images
        # — reshape the sprite freely (the old "Import cells → new OAM"). The
        # only constraints are 8-px-multiple dimensions and uniform frame size.
        # Off: paint the art into the sprite's existing OAM rectangles (layout
        # and footprint_scale unchanged; art must already fit the current shape).
        self._build_oam_on_import_cb = QCheckBox("Build new OAM layout on import")
        self._build_oam_on_import_cb.setChecked(True)
        self._build_oam_on_import_cb.setToolTip(wrap_tooltip(
            "Sprite-sheet / individual-frames import only.\n"
            "On (default): rebuild the OAM to fit the imported frames — reshape "
            "the sprite to any size (frames must be multiples of 8 px and all "
            "the same size). Recomputes the tile budget; larger sprites may "
            "exceed VRAM and get a warning.\n"
            "Off: paint the frames into the current OAM layout — cell count, "
            "OAM rectangles, and footprint_scale stay put, so the art has to "
            "fit the sprite's existing shape."
        ))

        cells_controls = QFormLayout()
        cells_controls.addRow("Cell", self._cell_spin)
        cells_controls.addRow("", self._show_all_cells)
        cells_controls.addRow("", self._show_red_overlay_cb)
        # "Include OAM coverage gaps in exported PNGs" is hidden (kept in code):
        # now that imports can build a fresh gap-free OAM, baking the gap tint
        # into exports is a niche concern. Reparented so it stays constructed
        # and its handlers callable, just not laid out.
        self._bake_red_overlay_cb.setParent(self)
        self._bake_red_overlay_cb.setVisible(False)

        # ---- Animation playback + step editing -----------------------
        # Timer drives _anim_pos at the chosen FPS; _on_anim_tick reads
        # the flattened track and updates _current_cell. Stopped state
        # leaves _current_cell wherever the user last manually picked.
        self._anim_timer = QTimer(self)
        self._anim_timer.setInterval(max(1, 1000 // self._anim_fps))
        self._anim_timer.timeout.connect(self._on_anim_tick)

        self._anim_track_combo = QComboBox()
        self._anim_track_combo.addItem("Idle", "idle")
        self._anim_track_combo.addItem("Attack", "attack")
        self._anim_track_combo.addItem("Defend", "defend")
        self._anim_track_combo.currentIndexChanged.connect(
            self._on_anim_track_changed
        )

        self._anim_play_btn = QPushButton("▶ Play")
        self._anim_play_btn.setCheckable(True)
        self._anim_play_btn.toggled.connect(self._on_anim_play_toggled)

        self._anim_fps_spin = QSpinBox()
        self._anim_fps_spin.setRange(1, 120)
        self._anim_fps_spin.setValue(self._anim_fps)
        self._anim_fps_spin.setSuffix(" fps")
        self._anim_fps_spin.valueChanged.connect(self._on_anim_fps_changed)

        # Steps table — 2 cols (cell, duration). Track A's first row is
        # the implicit cell-0 step (cell editable disabled). Edits push
        # ReplaceSpriteCommand re-encoding the whole entry-0; merging
        # rapid edits into one undo step is a known follow-up.
        self._anim_table = QTableWidget(0, 2)
        self._anim_table.setHorizontalHeaderLabels(["Cell", "Duration"])
        self._anim_table.verticalHeader().setVisible(False)
        self._anim_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.Stretch
        )
        self._anim_table.setEditTriggers(
            QAbstractItemView.DoubleClicked
            | QAbstractItemView.EditKeyPressed
            | QAbstractItemView.SelectedClicked
        )
        self._anim_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._anim_table.itemChanged.connect(self._on_anim_step_edited)
        # Re-entrancy guard: programmatic table population must not
        # trip itemChanged → re-encode.
        self._anim_table_loading = False

        # Add / remove buttons. Idle[0] is the implicit cell-0 anchor —
        # the engine reads from there even if the bytes say otherwise,
        # so removing it would desync the rest of the track. Remove is
        # disabled when the active row is Idle[0], when no row is
        # selected, or when the track is down to a single step (the
        # parser asserts ≥1 step per track across all vanilla groups).
        self._anim_add_btn = QPushButton("+ Add step")
        self._anim_add_btn.clicked.connect(self._on_anim_add_step)
        self._anim_remove_btn = QPushButton("- Remove step")
        self._anim_remove_btn.clicked.connect(self._on_anim_remove_step)
        self._anim_remove_btn.setEnabled(False)
        self._anim_table.itemSelectionChanged.connect(
            self._update_anim_remove_enabled
        )

        # Metadata block — fixed-width labels so switching digimon doesn't
        # cause the layout to reflow.
        self._meta_name = QLabel("—")
        self._meta_cells = QLabel("—")
        self._meta_tiles = QLabel("—")
        self._meta_footprint_scale = QLabel("—")
        self._meta_footprint_scale.setToolTip(wrap_tooltip(
            "Derived tiles-per-cell budget (mini_header u16 @ 0x00). "
            "Always equals NCGR tiles ÷ cell count and matches "
            "chrsize.tpf for the group — read-only because editing it "
            "independently of the tile bank makes the engine index "
            "garbled tiles for cells past 0."
        ))
        self._meta_cell_size = QLabel("—")
        for lbl in (
            self._meta_name,
            self._meta_cells, self._meta_tiles,
            self._meta_footprint_scale, self._meta_cell_size,
        ):
            lbl.setMinimumWidth(280)
        name_font = self._meta_name.font()
        name_font.setBold(True)
        self._meta_name.setFont(name_font)

        # Editable mini-header fields. y_pivot_a (i16 @ 0x06) drives the
        # scan-target overlay Y — user-facing. x_pivot (i16 @ 0x08) and
        # y_pivot_b (i16 @ 0x0A) have no observable in-game effect in
        # vanilla testing, so they're labelled with their raw offsets
        # and hidden behind the global "Show unknown fields" toggle.
        # footprint_scale (u16 @ 0x00) is deliberately NOT editable —
        # the field is a derived tiles-per-cell value that must equal
        # ``n_tiles // n_cells`` (== chrsize.tpf); editing it independent
        # of the tile bank garbles cells past 0. It's surfaced read-only
        # via `_meta_footprint_scale` in the metadata block above.
        # Edits go through _push_header_replacement so undo/redo wraps
        # them. _hdr_loading guards programmatic refresh from re-firing
        # the valueChanged → re-encode loop on selection change.
        self._hdr_loading = False
        self._hdr_y_pivot_a_spin = QSpinBox()
        self._hdr_y_pivot_a_spin.setRange(-0x8000, 0x7FFF)
        self._hdr_y_pivot_a_spin.setMaximumWidth(110)
        self._hdr_y_pivot_a_spin.setToolTip(
            "Scan target Y (mini_header i16 @ 0x06) — perceived Y for "
            "the scan-target circle overlay; does not move the sprite "
            "on the battlefield. Vanilla values are always ≤ 0."
        )
        self._hdr_y_pivot_a_spin.valueChanged.connect(
            lambda v: self._on_header_field_changed("y_pivot_a", v)
        )
        self._hdr_x_pivot_spin = QSpinBox()
        self._hdr_x_pivot_spin.setRange(-0x8000, 0x7FFF)
        self._hdr_x_pivot_spin.setMaximumWidth(110)
        self._hdr_x_pivot_spin.setToolTip(
            "mini_header i16 @ 0x08 — no observable in-game effect in "
            "vanilla testing."
        )
        self._hdr_x_pivot_spin.valueChanged.connect(
            lambda v: self._on_header_field_changed("x_pivot", v)
        )
        self._hdr_y_pivot_b_spin = QSpinBox()
        self._hdr_y_pivot_b_spin.setRange(-0x8000, 0x7FFF)
        self._hdr_y_pivot_b_spin.setMaximumWidth(110)
        self._hdr_y_pivot_b_spin.setToolTip(
            "mini_header i16 @ 0x0A — no observable in-game effect in "
            "vanilla testing."
        )
        self._hdr_y_pivot_b_spin.valueChanged.connect(
            lambda v: self._on_header_field_changed("y_pivot_b", v)
        )

        meta_form = QFormLayout()
        meta_form.addRow("Name", self._meta_name)
        meta_form.addRow("Cells", self._meta_cells)
        meta_form.addRow("NCGR tiles", self._meta_tiles)
        meta_form.addRow("Tiles / cell", self._meta_footprint_scale)
        meta_form.addRow("Per-cell PNG size", self._meta_cell_size)
        meta_form.addRow("Scan target Y", self._hdr_y_pivot_a_spin)
        add_unknown_form_row(meta_form, "Unknown 0x08", self._hdr_x_pivot_spin)
        add_unknown_form_row(meta_form, "Unknown 0x0A", self._hdr_y_pivot_b_spin)
        # Idle/Attack/Defend track summaries live in the Animation panel, not
        # the footer — no need to duplicate them here.

        # ---- Cells tab: preview (left) + animation editor (right) ----
        # Cell spinner + show-all-cells toggle moved to the actions row
        # below the tabs so they sit alongside the import/export and
        # metadata controls — no per-tab subheader.
        cells_tab = QWidget()
        cells_layout = QVBoxLayout(cells_tab)
        cells_layout.setContentsMargins(8, 8, 8, 8)

        # Coverage-gap note lives above the footer (see right_layout
        # below) so it's visible from both the Cells and Tile-sheet
        # tabs — the previous cells-tab-only placement left users on
        # the tile-sheet tab staring at red pixels with no explanation.
        self._coverage_note = QLabel()
        self._coverage_note.setWordWrap(True)
        self._coverage_note.setStyleSheet(
            "color: #b00020; font-size: 11px; padding: 2px 4px 0 4px;"
        )
        self._coverage_note.setVisible(False)

        # Left column: the composite preview with the Frame-offsets editor
        # collapsed beneath it.
        cells_left = QWidget()
        cells_left_layout = QVBoxLayout(cells_left)
        cells_left_layout.setContentsMargins(0, 0, 0, 0)
        cells_left_layout.addWidget(self._scroll, 1)

        # Move-frame (drag): translate one frame to align it with the others,
        # keeping its OAM structure — shrinks the shared footprint so a following
        # Compress OAM re-covers smaller. Swaps in over the preview when on.
        self._align_canvas = FrameAlignCanvas()
        self._align_canvas.committed.connect(self._on_align_committed)
        self._align_canvas.footprint.connect(self._on_align_footprint)
        self._align_scroll = QScrollArea()
        self._align_scroll.setWidgetResizable(False)
        self._align_scroll.setWidget(self._align_canvas)
        self._align_scroll.setVisible(False)
        cells_left_layout.addWidget(self._align_scroll, 1)

        move_row = QHBoxLayout()
        self._move_frame_cb = QCheckBox("Move frame (drag)")
        self._move_frame_cb.setToolTip(wrap_tooltip(
            "Drag a frame to line its content up with the others (shown as faint "
            "ghosts), keeping the OAM structure exactly. Aligning the frames "
            "shrinks the shared footprint — the live count shows it dropping — so "
            "Compress OAM can then re-cover into a smaller layout."
        ))
        self._move_frame_cb.toggled.connect(self._on_move_frame_toggled)
        move_row.addWidget(self._move_frame_cb)
        move_row.addWidget(QLabel("Frame:"))
        self._move_frame_combo = QComboBox()
        self._move_frame_combo.setVisible(False)
        self._move_frame_combo.currentIndexChanged.connect(
            lambda i: self._align_canvas.set_current(i) if i >= 0 else None
        )
        move_row.addWidget(self._move_frame_combo)
        move_row.addStretch(1)
        self._move_footprint_label = QLabel()
        self._move_footprint_label.setVisible(False)
        move_row.addWidget(self._move_footprint_label)
        cells_left_layout.addLayout(move_row)

        # Frame offsets: each cell's on-screen position, editable. The
        # position of a frame is baked into all its OAM x/y (no dedicated
        # pivot field — see project memory), so shifting a frame moves
        # every OAM in that cell uniformly. Values are the absolute OAM
        # origin as stored in the data. Collapsed by default.
        # "True positions" makes the preview + playback honour these
        # offsets (normally each frame is drawn tight to its own bbox,
        # hiding the movement).
        self._frame_off_group = CollapsibleSection("Frame offsets", expanded=False)
        fo_content = QWidget()
        fo_content_layout = QVBoxLayout(fo_content)
        fo_content_layout.setContentsMargins(0, 0, 0, 0)
        self._true_pos_cb = QCheckBox("Preview true frame positions")
        self._true_pos_cb.setToolTip(wrap_tooltip(
            "Draw each frame at its real on-screen offset within the "
            "sprite's combined bounds, so flipping frames or playing an "
            "animation shows the actual vertical bob / horizontal lunge. "
            "Off (default) draws each frame tight to its own bounding "
            "box, which is better for pixel editing but hides the "
            "per-frame movement."
        ))
        self._true_pos_cb.toggled.connect(lambda _=False: self._refresh_preview())
        fo_content_layout.addWidget(self._true_pos_cb)
        # Header row + per-frame rows are (re)built per selection in
        # _rebuild_frame_offset_rows since the cell count varies.
        self._frame_off_grid = QGridLayout()
        self._frame_off_grid.setContentsMargins(0, 0, 0, 0)
        self._frame_off_grid.setHorizontalSpacing(8)
        self._frame_off_grid.setVerticalSpacing(3)
        self._frame_off_spins: List[Tuple[QSpinBox, QSpinBox]] = []
        self._frame_off_loading = False
        fo_grid_host = QWidget()
        fo_grid_host.setLayout(self._frame_off_grid)
        fo_content_layout.addWidget(fo_grid_host)
        self._frame_off_group.set_content_widget(fo_content)
        cells_left_layout.addWidget(self._frame_off_group)

        # Right column: the Animation editor, always visible so the track
        # picker, playback controls, and editable step list sit beside the
        # preview instead of hidden in a bottom drawer.
        # Hidden as a whole (not collapsed to a strip) via the "Animation"
        # view checkbox under the tabs — the Cells splitter then hands the
        # freed width to the preview.
        self._anim_panel = QWidget()
        anim_panel_layout = QVBoxLayout(self._anim_panel)
        anim_panel_layout.setContentsMargins(0, 0, 0, 0)
        anim_header = QLabel("Animation")
        _hf = anim_header.font()
        _hf.setBold(True)
        anim_header.setFont(_hf)
        anim_panel_layout.addWidget(anim_header)
        anim_controls_row = QHBoxLayout()
        anim_controls_row.addWidget(QLabel("Track:"))
        anim_controls_row.addWidget(self._anim_track_combo)
        anim_controls_row.addWidget(self._anim_play_btn)
        anim_controls_row.addWidget(self._anim_fps_spin)
        anim_controls_row.addStretch(1)
        anim_panel_layout.addLayout(anim_controls_row)
        anim_panel_layout.addWidget(self._anim_table, 1)
        anim_btn_row = QHBoxLayout()
        anim_btn_row.addStretch(1)
        anim_btn_row.addWidget(self._anim_add_btn)
        anim_btn_row.addWidget(self._anim_remove_btn)
        anim_panel_layout.addLayout(anim_btn_row)

        self._cells_split = QSplitter(Qt.Horizontal)
        self._cells_split.addWidget(cells_left)
        self._cells_split.addWidget(self._anim_panel)
        self._cells_split.setStretchFactor(0, 1)
        self._cells_split.setStretchFactor(1, 0)
        self._cells_split.setSizes([560, 300])
        cells_layout.addWidget(self._cells_split, 1)

        # ---- Tile sheet tab: width + columns spinners + sheet preview
        sheet_tab = QWidget()
        sheet_layout = QVBoxLayout(sheet_tab)
        sheet_layout.setContentsMargins(8, 8, 8, 8)
        sheet_layout.addWidget(self._sheet_scroll, 1)
        sheet_form = QFormLayout()
        sheet_form.addRow("View", self._view_mode_combo)
        sheet_form.addRow("Width (tiles)", self._sheet_cols_spin)
        sheet_form.addRow("Columns", self._sheet_columns_spin)
        sheet_form.addRow("", self._sheet_fill_ltr_cb)
        sheet_controls = QHBoxLayout()
        sheet_controls.addLayout(sheet_form)
        sheet_controls.addStretch(1)
        sheet_layout.addLayout(sheet_controls)

        # Eyedropper click capture lives in the shared picker widget;
        # bind both previews so a click on either samples a colour.
        self._picker.bind_preview(self._preview, self._cells_source)
        self._picker.bind_preview(self._sheet_preview, self._sheet_source)

        # ---- OAM map tab: the sprite with its OAM cover overlaid, each OBJ
        # coloured by how much real art it holds (red = mostly empty → a whole
        # tile slot cheap to reclaim; green = solid → essential) and labelled
        # with the tiles it costs. Reads the live decode, so it reflects edits
        # before they're saved. "Edit OAM" turns it into a hand-drawn cover.
        oam_tab = QWidget()
        oam_layout = QVBoxLayout(oam_tab)
        oam_layout.setContentsMargins(8, 8, 8, 8)

        oam_ctrl = QHBoxLayout()
        self._oam_edit_cb = QCheckBox("Edit OAM")
        self._oam_edit_cb.setToolTip(
            "Draw the OAM cover by hand: left-click to place the chosen shape, "
            "click a box to select + drag to move, right-click / Delete to remove. "
            "Red tiles are art no OBJ covers (it would vanish). Apply re-lays the "
            "sprite through the same rebuild the auto-compressor uses."
        )
        self._oam_edit_cb.toggled.connect(self._on_oam_edit_toggled)
        oam_ctrl.addWidget(self._oam_edit_cb)
        self._oam_shape_label = QLabel("Shape:")
        self._oam_shape_combo = QComboBox()
        for _tw, _th in oam_edit_canvas.LEGAL_SHAPES:
            self._oam_shape_combo.addItem(f"{_tw * 8}×{_th * 8}", (_tw, _th))
        self._oam_shape_combo.setCurrentIndex(
            next(i for i, (a, b) in enumerate(oam_edit_canvas.LEGAL_SHAPES)
                 if (a, b) == (2, 2))  # default 16×16
        )
        self._oam_shape_combo.currentIndexChanged.connect(self._on_oam_shape_changed)
        oam_ctrl.addWidget(self._oam_shape_label)
        oam_ctrl.addWidget(self._oam_shape_combo)
        oam_ctrl.addStretch(1)
        self._oam_reset_btn = QPushButton("Reset")
        self._oam_reset_btn.setToolTip("Restore the current stored OAM layout.")
        self._oam_reset_btn.clicked.connect(self._seed_oam_editor)
        self._oam_apply_btn = QPushButton("Apply")
        self._oam_apply_btn.clicked.connect(self._on_oam_apply)
        oam_ctrl.addWidget(self._oam_reset_btn)
        oam_ctrl.addWidget(self._oam_apply_btn)
        oam_layout.addLayout(oam_ctrl)

        self._oam_stats_label = QLabel()
        self._oam_stats_label.setTextFormat(Qt.RichText)
        self._oam_stats_label.setWordWrap(True)
        oam_layout.addWidget(self._oam_stats_label)

        self._oam_map_label = QLabel("Select a sprite.")
        self._oam_map_label.setAlignment(Qt.AlignCenter)
        self._oam_scroll = QScrollArea()
        self._oam_scroll.setWidgetResizable(True)
        self._oam_scroll.setWidget(self._oam_map_label)
        oam_layout.addWidget(self._oam_scroll, 1)

        self._oam_edit_canvas = oam_edit_canvas.OamEditCanvas()
        self._oam_edit_canvas.changed.connect(self._on_oam_canvas_changed)
        self._oam_edit_scroll = QScrollArea()
        self._oam_edit_scroll.setWidgetResizable(False)
        self._oam_edit_scroll.setWidget(self._oam_edit_canvas)
        self._oam_edit_scroll.setVisible(False)
        oam_layout.addWidget(self._oam_edit_scroll, 1)
        self._oam_edit_origin: Tuple[int, int] = (0, 0)
        for _w in (self._oam_shape_label, self._oam_shape_combo,
                   self._oam_reset_btn, self._oam_apply_btn):
            _w.setVisible(False)

        self._tabs = QTabWidget()
        self._tabs.addTab(cells_tab, "Cells")
        self._tabs.addTab(sheet_tab, "Tile sheet")
        self._oam_tab_index = self._tabs.addTab(oam_tab, "OAM map")
        self._tabs.currentChanged.connect(self._on_tab_changed)

        # Import/export buttons stay visible regardless of the active tab
        # so the workflow doesn't require remembering which tab hosts which
        # action. Every format/mode now lives inside the two "as…" dropdown
        # menus; the standalone actions (compress, duplicate) sit in a second
        # column. All buttons pinned to the widest label.
        vis_btns = [
            self._import_as_btn, self._export_as_btn,
            self._compress_oam_btn, self._duplicate_entry_btn,
        ]
        # Admin-only (--admin): expose the lossy fit-≤512 trim beside Compress
        # OAM. Hidden by default (it drops real edge pixels); see editor.runtime.
        if is_admin():
            vis_btns.append(self._compress_oam_fit_btn)
        # Pin all four to one width so they read as an aligned group. Target the
        # width the dropdowns naturally take under the checkbox column (the
        # widest sizeHint among the buttons *and* the two checkboxes) so nothing
        # looks cramped.
        max_btn_w = max(
            w.sizeHint().width() for w in (
                *vis_btns,
                self._build_oam_on_import_cb, self._import_pal_with_sheet_cb,
            )
        )
        for b in vis_btns:
            b.setFixedWidth(max_btn_w)
        # Column 1 — the two "as…" dropdowns (every format/mode lives inside),
        # Import above Export since importing is the common task. The two
        # checkboxes beneath modify how the import rows behave.
        sheet_col = QVBoxLayout()
        sheet_col.setSpacing(4)
        sheet_col.addWidget(self._import_as_btn)
        sheet_col.addWidget(self._export_as_btn)
        sheet_col.addWidget(self._build_oam_on_import_cb)
        sheet_col.addWidget(self._import_pal_with_sheet_cb)
        sheet_col.addStretch(1)
        # Column 2 — the standalone actions (compress, duplicate).
        per_cell_col = QVBoxLayout()
        per_cell_col.setSpacing(4)
        per_cell_col.addWidget(self._compress_oam_btn)
        # The fit-≤512 button is superseded/hidden by default (added to the
        # _superseded list above). Under --admin, un-hide it here — addWidget
        # reparents it out of that hidden state, and setVisible re-shows it.
        if is_admin():
            self._compress_oam_fit_btn.setVisible(True)
            per_cell_col.addWidget(self._compress_oam_fit_btn)
        per_cell_col.addWidget(self._duplicate_entry_btn)
        per_cell_col.addStretch(1)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        # self._tabs + the scrollable controls below are assembled into a
        # vertical splitter at the end of this block.

        # View toggles: hide the whole palette / animation panel to give the
        # preview the width. Hiding a splitter child hands its space to the
        # sibling, so this reclaims the full pane (not a leftover strip).
        # anim_panel exists now; palette_col is wired after it's built below.
        view_col = QVBoxLayout()
        view_col.setSpacing(4)
        # Palette moved to its own tab, so the show/hide-palette toggle is
        # gone; the Animation toggle stays (it hides the Cells-tab anim panel).
        self._show_anim_cb = QCheckBox("Animation")
        self._show_anim_cb.setChecked(True)
        self._show_anim_cb.setToolTip("Show/hide the Cells-tab animation panel")
        self._show_anim_cb.toggled.connect(self._anim_panel.setVisible)
        view_col.addWidget(self._show_anim_cb)
        view_col.addStretch(1)

        # Single row under the tabs: cell nav controls (leftmost — the
        # space the picker used to occupy), button columns, metadata,
        # stretch. Picker drops to its own row below so the transparent
        # colour edit sits visually under the empty space left by the
        # nav controls.
        # Flow the control groups so they wrap onto a second row when the pane
        # narrows, instead of the panels side by side pinning a wide floor.
        def _panel(inner_layout) -> QWidget:
            holder = QWidget()
            holder.setLayout(inner_layout)
            return holder

        actions_row_w = QWidget()
        actions_flow = FlowLayout(actions_row_w, margin=0, h_spacing=16, v_spacing=8)
        for inner in (cells_controls, sheet_col, per_cell_col, view_col, meta_form):
            actions_flow.addWidget(_panel(inner))
        make_height_for_width(actions_row_w)

        # Everything below the preview tabs (coverage note, control panels,
        # transparent-colour picker) goes in a scroll area, split vertically
        # from the tabs so the pane is compressible top-to-bottom instead of
        # growing tall as the control panels wrap.
        controls_container = QWidget()
        cc_layout = QVBoxLayout(controls_container)
        cc_layout.setContentsMargins(0, 0, 0, 0)
        cc_layout.addWidget(self._coverage_note)
        cc_layout.addWidget(actions_row_w)
        cc_layout.addWidget(self._picker)
        controls_scroll = QScrollArea()
        controls_scroll.setWidgetResizable(True)
        controls_scroll.setFrameShape(QFrame.NoFrame)
        controls_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        controls_scroll.setWidget(controls_container)
        ReflowHeightSync(controls_container)

        mid_split = QSplitter(Qt.Vertical)
        mid_split.addWidget(self._tabs)
        mid_split.addWidget(controls_scroll)
        mid_split.setStretchFactor(0, 1)
        mid_split.setStretchFactor(1, 0)
        mid_split.setCollapsible(0, False)
        mid_split.setSizes([480, 300])
        right_layout.addWidget(mid_split)


        # `+ Add Entry` sits below the digimon list so it appears next to
        # the slot the new row will land in (appends always land at the
        # end). Wraps the list + button in one column container so they
        # share the same splitter pane.
        self._add_entry_btn = QPushButton("+ Add Entry")
        self._add_entry_btn.setToolTip(
            "Duplicate the currently-selected battle sprite into a new "
            "group appended at the end. The new entry can then be edited "
            "and pointed at by any enemy via sprite_map."
        )
        self._add_entry_btn.clicked.connect(self._on_add_entry)
        list_col = QWidget()
        list_col_layout = QVBoxLayout(list_col)
        list_col_layout.setContentsMargins(0, 0, 0, 0)
        list_col_layout.setSpacing(4)
        list_col_layout.addWidget(self._list, 1)
        list_col_layout.addWidget(self._add_entry_btn)

        # Hidden as a whole (not collapsed to a strip) via the "Palette" view
        # checkbox under the tabs — the outer splitter hands the freed width to
        # the preview. Grid + single-slot editor + batch adjuster stack top to
        # bottom; the 256-colour bank is taller than the pane at 8/row so the
        # scroll caps and scrolls (h-bar off, room reserved for the v-bar).
        self._palette_col = QWidget()
        palette_col_layout = QVBoxLayout(self._palette_col)
        palette_col_layout.setContentsMargins(4, 4, 4, 4)
        palette_col_layout.setSpacing(6)
        pal_header = QHBoxLayout()
        palette_title = QLabel("Palette")
        palette_title.setStyleSheet("font-weight: bold;")
        pal_header.addWidget(palette_title)
        pal_header.addStretch(1)
        pal_header.addWidget(self._eyedropper_btn)
        palette_col_layout.addLayout(pal_header)
        self._pal_scroll = QScrollArea()
        self._pal_scroll.setWidget(self._palette_grid)
        self._pal_scroll.setWidgetResizable(False)
        self._pal_scroll.setAlignment(Qt.AlignTop | Qt.AlignHCenter)
        self._pal_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._pal_scroll.setMinimumWidth(self._palette_grid.width() + 20)
        self._pal_scroll.setFixedHeight(
            min(self._palette_grid.height() + 2, PALETTE_SCROLL_MAX_H)
        )
        palette_col_layout.addWidget(self._pal_scroll)
        palette_col_layout.addWidget(self._palette_editor)
        palette_col_layout.addWidget(self._palette_adjuster)
        palette_col_layout.addLayout(self._build_borrow_palette_section())
        palette_col_layout.addStretch(1)
        # Never shrink below the full swatch grid (+ vertical scrollbar + margins).
        self._pal_min_w = self._palette_grid.width() + 30

        # Palette tab: the sprite preview (for reference) on the left + the
        # palette editor on the right, so recolouring keeps the image on
        # screen. The preview mirrors the Cells render (_refresh_preview). The
        # editor side scrolls so its tall 256-colour panel doesn't pin the tab
        # widget's minimum height.
        self._palette_preview_label = QLabel("Select a sprite to preview.")
        self._palette_preview_label.setAlignment(Qt.AlignCenter)
        pal_prev_scroll = QScrollArea()
        pal_prev_scroll.setWidgetResizable(False)
        pal_prev_scroll.setAlignment(Qt.AlignCenter)
        pal_prev_scroll.setWidget(self._palette_preview_label)
        pal_edit_scroll = QScrollArea()
        pal_edit_scroll.setWidgetResizable(True)
        pal_edit_scroll.setFrameShape(QFrame.NoFrame)
        pal_edit_scroll.setWidget(self._palette_col)
        palette_tab = QSplitter(Qt.Horizontal)
        palette_tab.addWidget(pal_prev_scroll)
        palette_tab.addWidget(pal_edit_scroll)
        palette_tab.setStretchFactor(0, 1)
        palette_tab.setStretchFactor(1, 0)
        palette_tab.setSizes([420, 300])
        self._palette_tab_index = self._tabs.addTab(palette_tab, "Palette")

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(list_col)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setChildrenCollapsible(False)
        splitter.setSizes([230, 900])

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(splitter)

    # ---- selection / refresh -------------------------------------------

    def _on_group_selected(self, g: int) -> None:
        self._current_group = g
        self._session.remember_selection(self._CURSOR_KEY, g)
        self._cell_pixmaps = []
        try:
            digimon_id = (
                self._chrsize_rows[g][0]
                if g < len(self._chrsize_rows) else -1
            )
            self._current_decoded = btchr.decode_digimon(
                self._pak, g, digimon_id=digimon_id,
            )
        except (ValueError, IndexError) as exc:
            self._current_decoded = None
            self._preview.setText(f"(decode error: {exc})")
            return

        d = self._current_decoded
        self._sync_palette_grid()
        self._reset_borrow()
        self._cell_spin.blockSignals(True)
        self._cell_spin.setRange(0, max(0, len(d.ncer.cells) - 1))
        self._cell_spin.setValue(0)
        self._cell_spin.blockSignals(False)
        self._current_cell = 0

        h = d.header
        name = self._name_for_group(g)
        self._meta_name.setText(name if name else "—")
        n_cells = len(d.ncer.cells)
        self._meta_cells.setText(str(n_cells))
        self._meta_tiles.setText(f"{d.n_tiles} tiles (8bpp)")
        derived_fs = btchr.derived_footprint_scale(d.n_tiles, n_cells)
        stored_fs = d.header.footprint_scale
        if stored_fs == derived_fs:
            self._meta_footprint_scale.setText(str(derived_fs))
        else:
            self._meta_footprint_scale.setText(
                f"{stored_fs} (expected {derived_fs} — mini-header out of sync)"
            )
        # Red when over the party-viewer VRAM cap — the sprite won't render
        # there (see the note under the tabs + _update_coverage_note).
        over_cap = derived_fs > btchrspr.PARTY_VIEWER_TPF_CAP
        self._meta_footprint_scale.setStyleSheet(
            "color: #c0392b; font-weight: bold;" if over_cap else ""
        )
        layout = self._cell_layout()
        if layout is None:
            self._meta_cell_size.setText("—")
        else:
            _, max_w, max_h = layout
            self._meta_cell_size.setText(f"{max_w}×{max_h} px")
        self._load_header_spinboxes(h)

        # Sheet width: prefer the user's last pick for this digimon
        # (per-group memory); fall back to a bbox-derived default so
        # minis get a narrow grid and bosses a wider one.
        if g in self._sheet_cols_overrides:
            sheet_cols = self._sheet_cols_overrides[g]
        else:
            bbox_w, _ = ncer_mod.sprite_bbox(d.ncer)
            sheet_cols = _default_sheet_cols(bbox_w)
        self._sheet_cols_spin.blockSignals(True)
        self._sheet_cols_spin.setValue(sheet_cols)
        self._sheet_cols_spin.blockSignals(False)
        # Columns: same per-group memory rule, default = 5-cell slabbing.
        sheet_columns = self._sheet_columns_overrides.get(
            g, DEFAULT_SHEET_COLUMNS,
        )
        self._sheet_columns_spin.blockSignals(True)
        self._sheet_columns_spin.setValue(sheet_columns)
        self._sheet_columns_spin.blockSignals(False)
        # Fill direction: default top-to-bottom (legacy slab behaviour).
        fill_ltr = self._sheet_fill_ltr_overrides.get(g, False)
        self._sheet_fill_ltr_cb.blockSignals(True)
        self._sheet_fill_ltr_cb.setChecked(fill_ltr)
        self._sheet_fill_ltr_cb.blockSignals(False)
        # View mode: default to cells (OAM-composed) so the assembled
        # character shows up on first selection.
        view_mode = self._view_mode_overrides.get(g, "cells")
        self._view_mode_combo.blockSignals(True)
        self._view_mode_combo.setCurrentIndex(
            0 if view_mode == "cells" else 1
        )
        self._view_mode_combo.blockSignals(False)
        self._update_view_mode_controls()

        self._sheet_dirty = True
        self._oam_dirty = True
        self._refresh_preview()
        if self._tabs.currentIndex() == 1:
            self._refresh_sheet_preview()
            self._sheet_dirty = False
        self._refresh_oam_tab_if_active()
        self._picker.set_current_color(self._current_decoded.palette[0])
        # Selection swap implicitly stops playback — the new digimon
        # has different cell/track indices so blind continuation would
        # render against the wrong tile bank.
        self._stop_anim_playback()
        self._refresh_anim_table()
        self._rebuild_frame_offset_rows()
        self._load_frame_offsets()
        if getattr(self, "_move_frame_cb", None) and self._move_frame_cb.isChecked():
            self._rebuild_align_data()

    def _on_cell_changed(self, value: int) -> None:
        self._current_cell = value
        self._refresh_preview()

    # ---- frame offsets --------------------------------------------------

    def _rebuild_frame_offset_rows(self) -> None:
        """(Re)create the per-frame X/Y spinbox grid for the current sprite.

        Cell count varies (sentinels have 1, normal sprites 5), so the
        rows are torn down and rebuilt on each selection rather than
        shown/hidden. Every frame (including 0) is editable — values are
        the absolute OAM origin stored in the data.
        """
        while self._frame_off_grid.count():
            item = self._frame_off_grid.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._frame_off_spins = []
        d = self._current_decoded
        if d is None or not d.ncer.cells:
            return
        self._frame_off_grid.addWidget(QLabel("Frame"), 0, 0)
        self._frame_off_grid.addWidget(QLabel("X"), 0, 1)
        self._frame_off_grid.addWidget(QLabel("Y"), 0, 2)
        for ci in range(len(d.ncer.cells)):
            self._frame_off_grid.addWidget(QLabel(str(ci)), ci + 1, 0)
            x_spin = QSpinBox()
            y_spin = QSpinBox()
            for axis, spin in (("x", x_spin), ("y", y_spin)):
                spin.setMaximumWidth(70)
                spin.valueChanged.connect(
                    lambda v, c=ci, a=axis: self._on_frame_offset_edited(c, a, v)
                )
            self._frame_off_grid.addWidget(x_spin, ci + 1, 1)
            self._frame_off_grid.addWidget(y_spin, ci + 1, 2)
            self._frame_off_spins.append((x_spin, y_spin))

    def _load_frame_offsets(self) -> None:
        """Populate the frame-offset spinboxes from the live NCER.

        Values are the absolute OAM origin (top-left of the frame's OAM
        union) as stored in the data. Per-spin ranges are clamped so the
        resulting coords always fit the hardware fields (x 9-bit signed,
        y 8-bit signed) — the edit path can then never push
        :func:`ncer.shift_cell_oams` out of range.
        """
        d = self._current_decoded
        if d is None or len(self._frame_off_spins) != len(d.ncer.cells):
            return
        cells = d.ncer.cells
        self._frame_off_loading = True
        try:
            for ci, (x_spin, y_spin) in enumerate(self._frame_off_spins):
                cell = cells[ci]
                ox, oy = _cell_origin(cell)
                oxs = [o.x for o in cell.oams] or [0]
                oys = [o.y for o in cell.oams] or [0]
                # The origin is the min OAM coord; moving it to V shifts
                # every OAM by (V - origin). Field limits on the extreme
                # OAMs collapse to: origin >= field_lo, origin <= field_hi
                # - (span). span keeps the far OAM in range.
                span_x = max(oxs) - min(oxs)
                span_y = max(oys) - min(oys)
                x_spin.setRange(-256, 255 - span_x)
                y_spin.setRange(-128, 127 - span_y)
                x_spin.setValue(ox)
                y_spin.setValue(oy)
        finally:
            self._frame_off_loading = False

    def _on_frame_offset_edited(self, cell_idx: int, axis: str, value: int) -> None:
        """Move ``cell_idx``'s OAM origin to ``value`` on ``axis`` (absolute).
        One NCER byte-patch, wrapped as an undo command; the post-change
        refresh reloads the spinboxes."""
        if self._frame_off_loading:
            return
        d = self._current_decoded
        if d is None or self._current_group is None:
            return
        cells = d.ncer.cells
        if not (0 <= cell_idx < len(cells)):
            return
        ox, oy = _cell_origin(cells[cell_idx])
        cur = ox if axis == "x" else oy
        delta = value - cur
        if delta == 0:
            return
        dx, dy = (delta, 0) if axis == "x" else (0, delta)
        self._push_cell_shift(
            cell_idx, dx, dy,
            description=(
                f"Move BTCHR 0x{self._current_group:04x} frame {cell_idx} "
                f"{axis.upper()}={value}"
            ),
        )

    def _push_cell_shift(
        self, cell_idx: int, dx: int, dy: int, description: str,
    ) -> None:
        group = self._current_group
        entry = self._ncer_entry_idx(group)
        ncer_raw = sprite.decompress_rle30(self._pak.entries[entry])
        try:
            patched = ncer_mod.shift_cell_oams(ncer_raw, cell_idx, dx, dy)
        except (ValueError, IndexError) as exc:
            QMessageBox.critical(self, "Move failed", str(exc))
            self._load_frame_offsets()
            return
        compressed = sprite.compress_rle30(patched)
        cmd = ReplaceSpriteCommand(
            self._session,
            [(BTCHR_PAK, entry, compressed)],
            description=description,
            on_change=self._refresh_after_pak_change,
        )
        if self._undo_stack is not None:
            self._undo_stack.push(cmd)
        else:
            cmd.redo()

    # ---- move-frame (drag alignment) -----------------------------------

    def _on_move_frame_toggled(self, on: bool) -> None:
        self._move_frame_combo.setVisible(on)
        self._move_footprint_label.setVisible(on)
        self._scroll.setVisible(not on)
        self._align_scroll.setVisible(on)
        if on:
            self._rebuild_align_data()

    def _rebuild_align_data(self) -> None:
        """Render every frame onto the shared canvas + its tile occupancy and
        hand them to the drag canvas. Called on toggle-on and after each move."""
        d = self._current_decoded
        if d is None or not d.ncer.cells:
            return
        cells = d.ncer.cells
        xo, yo, w, h = btchr.cells_union_canvas(cells)
        gc, gr = w // 8, h // 8
        pal = d.palette
        imgs: List[QImage] = []
        occ: List[List[List[bool]]] = []
        for c in cells:
            idx = btchr.render_cell_indexed(
                c, d.tile_bytes, w, h, xo, yo, d.ncer.boundary_bytes
            )
            buf = bytearray(w * h * 4)
            m = [[False] * gc for _ in range(gr)]
            for y in range(h):
                base = y * w
                for x in range(w):
                    v = idx[base + x]
                    if v:
                        r, g, b = pal[v]
                        o = (base + x) * 4
                        buf[o] = r; buf[o + 1] = g; buf[o + 2] = b; buf[o + 3] = 255
                        m[y // 8][x // 8] = True
            imgs.append(QImage(bytes(buf), w, h, QImage.Format_RGBA8888).copy())
            occ.append(m)
        cur = max(0, min(self._current_cell, len(cells) - 1))
        self._move_frame_combo.blockSignals(True)
        self._move_frame_combo.clear()
        for i in range(len(cells)):
            self._move_frame_combo.addItem(f"Frame {i}", i)
        self._move_frame_combo.setCurrentIndex(cur)
        self._move_frame_combo.blockSignals(False)
        self._align_canvas.set_data(imgs, occ, gc, gr, cur)

    def _on_align_footprint(self, n_tiles: int) -> None:
        self._move_footprint_label.setText(
            f"footprint: <b>{n_tiles}</b> tiles (aligned union) — "
            "lower is smaller after Compress"
        )
        self._move_footprint_label.setTextFormat(Qt.RichText)

    def _on_align_committed(self, cell_idx: int, dx: int, dy: int) -> None:
        if self._current_group is None:
            return
        # _push_cell_shift → _refresh_after_pak_change re-decodes and (in move
        # mode) re-seeds the canvas, so it shows the committed positions — or
        # snaps back if the move was rejected as out of range.
        self._push_cell_shift(
            cell_idx, dx, dy,
            description=f"Align BTCHR 0x{self._current_group:04x} frame {cell_idx} "
                       f"(dx={dx}, dy={dy})",
        )

    def _on_show_all_toggled(self, checked: bool) -> None:
        self._cell_spin.setEnabled(not checked)
        self._refresh_preview()

    # ---- rendering -----------------------------------------------------

    def _cell_pixmap(self, cell_idx: int) -> Optional[QPixmap]:
        """Render + memoize one cell of the current digimon.

        Overlays a semi-transparent green checkerboard on pixels inside
        the cell bbox that no OAM covers — those regions get exported to
        PNG and can be painted over by the user, but the engine never
        draws them (no OAM references the underlying tiles), so any
        imported content there is silently lost. Sprite 0x00a1
        (Baihumon) is the canonical example.
        """
        if self._current_decoded is None:
            return None
        d = self._current_decoded
        if not (0 <= cell_idx < len(d.ncer.cells)):
            return None
        if len(self._cell_pixmaps) < len(d.ncer.cells):
            self._cell_pixmaps = [None] * len(d.ncer.cells)
        cached = self._cell_pixmaps[cell_idx]
        if cached is not None:
            return cached
        cell = d.ncer.cells[cell_idx]
        rgba, w, h = btchr.render_cell_rgba(
            cell, d.tile_bytes, self._render_palette(),
            boundary_bytes=d.ncer.boundary_bytes,
        )
        if self._show_red_overlay_cb.isChecked():
            xmin, ymin, _, _ = btchr.cell_bbox(cell)
            mask = btchr.oam_coverage_mask(cell, w, h, xmin, ymin)
            if mask:
                buf = bytearray(rgba)
                for i, covered in enumerate(mask):
                    if covered:
                        continue
                    py = i // w
                    px = i % w
                    if ((px + py) & 1) == 0:
                        po = i * 4
                        buf[po] = 255
                        buf[po + 1] = 0
                        buf[po + 2] = 0
                        buf[po + 3] = 96
                rgba = bytes(buf)
        img = QImage(rgba, w, h, w * 4, QImage.Format_RGBA8888).copy()
        pm = QPixmap.fromImage(img)
        self._cell_pixmaps[cell_idx] = pm
        return pm

    def _union_bbox(self):
        """Combined (xmin, ymin, xmax, ymax) over all cells' OAM unions —
        the shared canvas that preserves inter-frame offsets."""
        d = self._current_decoded
        boxes = [btchr.cell_bbox(c) for c in d.ncer.cells if c.oams]
        if not boxes:
            return None
        return (
            min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes),
        )

    def _cell_pixmap_anchored(self, cell_idx: int) -> Optional[QPixmap]:
        """The cell rendered at its true offset inside the union canvas.

        Same tight per-cell pixmap (with any red overlay) as
        :meth:`_cell_pixmap`, just blitted at ``(xmin, ymin)`` relative to
        the union origin so flipping frames / playing an animation shows
        the real bob and lunge instead of every frame snapping to (0, 0).
        """
        tight = self._cell_pixmap(cell_idx)
        if tight is None:
            return None
        union = self._union_bbox()
        if union is None:
            return tight
        uxmin, uymin, uxmax, uymax = union
        w, h = uxmax - uxmin, uymax - uymin
        if w <= 0 or h <= 0:
            return tight
        cell = self._current_decoded.ncer.cells[cell_idx]
        xmin, ymin, _, _ = btchr.cell_bbox(cell)
        canvas = QImage(w, h, QImage.Format_RGBA8888)
        canvas.fill(0)
        painter = QPainter(canvas)
        painter.drawPixmap(xmin - uxmin, ymin - uymin, tight)
        painter.end()
        return QPixmap.fromImage(canvas)

    def _mirror_palette_preview(self, *, pixmap=None, text=None) -> None:
        """Keep the Palette-tab reference image in step with the Cells render
        so recolouring on the Palette tab shows the sprite live."""
        lbl = getattr(self, "_palette_preview_label", None)
        if lbl is None:
            return
        if pixmap is not None:
            lbl.setPixmap(pixmap)
            lbl.adjustSize()
        else:
            lbl.setText(text)

    def _refresh_preview(self) -> None:
        if self._current_decoded is None:
            self._cells_src_qimage = None
            self._coverage_note.setVisible(False)
            self._mirror_palette_preview(text="Select a sprite to preview.")
            return
        self._update_coverage_note()
        if self._show_all_cells.isChecked():
            pm = self._build_all_cells_strip()
        elif self._true_pos_cb.isChecked():
            pm = self._cell_pixmap_anchored(self._current_cell)
        else:
            pm = self._cell_pixmap(self._current_cell)
        if pm is None or pm.isNull():
            self._preview.setText("(empty)")
            self._cells_src_qimage = None
            self._mirror_palette_preview(text="(empty)")
            return
        scaled = pm.scaled(
            pm.width() * PREVIEW_ZOOM, pm.height() * PREVIEW_ZOOM,
            Qt.KeepAspectRatio, Qt.FastTransformation,
        )
        self._preview.setPixmap(scaled)
        self._mirror_palette_preview(pixmap=scaled)
        # Force the QScrollArea to honor the pixmap's size so a wide
        # "show all cells" strip gets a horizontal scroll bar instead of
        # being silently clipped.
        self._preview.adjustSize()
        # Stash native-size source for the eyedropper. `pm.toImage()` keeps
        # the alpha channel, so picking on transparent cell gutters / cell
        # backgrounds can be skipped cleanly in `_sample_pixel`.
        self._cells_src_qimage = pm.toImage()
        self._cells_src_size = (pm.width(), pm.height())
        self._cells_pix_size = (scaled.width(), scaled.height())

    def _nclr_entry_idx(self, group_idx: int) -> int:
        return group_idx * btchr.GROUP_SIZE + 2

    def _ncgr_entry_idx(self, group_idx: int) -> int:
        return group_idx * btchr.GROUP_SIZE + 1

    def _ncer_entry_idx(self, group_idx: int) -> int:
        return group_idx * btchr.GROUP_SIZE + 3

    # ---- tile sheet rendering ------------------------------------------

    def _render_sheet_qimage(
        self, cols: int, columns: int = 1, fill_ltr: bool = False,
    ) -> Optional[QImage]:
        """Lay the NCGR tile bank into ``columns`` slabs, each ``cols``
        tiles wide, placed side-by-side.

        - ``columns == 1`` → classic single ``cols × ceil(n_tiles/cols)``
          grid (legacy behaviour).
        - ``columns > 1`` + ``fill_ltr=False`` → bank split into
          ``columns`` equal slabs; each slab fills top-to-bottom before
          the next starts. Right when each slab is a self-contained
          cell or frame.
        - ``columns > 1`` + ``fill_ltr=True``  → tiles flow row-major
          across all slabs first, then wrap. Equivalent to laying the
          bank out at width ``cols * columns`` in one big grid — right
          when the bank is screen-row-major and a character composes
          naturally at the wider grid.

        Colour table mirrors the live NCLR with slot 0 transparent so
        the result round-trips through indexed-aware editors.
        """
        if self._current_decoded is None:
            return None
        d = self._current_decoded
        n_tiles = d.n_tiles
        if n_tiles == 0 or cols <= 0 or columns <= 0:
            return None
        total_cols = cols * columns
        if fill_ltr:
            # One big grid at total_cols wide. tile index t → row=t/total,
            # col=t%total. Slabs are purely a visual unit here.
            rows = (n_tiles + total_cols - 1) // total_cols
        else:
            # Round slab size up to a multiple of ``cols`` so every slab
            # is full-row-aligned — otherwise the last row of a slab
            # would span fewer columns than the row above and the
            # side-by-side alignment would visibly drift between slabs.
            tiles_per_slab = (n_tiles + columns - 1) // columns
            rows = (tiles_per_slab + cols - 1) // cols
            tiles_per_slab = rows * cols
        img = QImage(total_cols * 8, rows * 8, QImage.Format_Indexed8)
        ctable = []
        for pi, (r, g, b) in enumerate(d.palette):
            ctable.append(qRgba(r, g, b, 0 if pi == 0 else 255))
        img.setColorTable(ctable)
        img.fill(0)
        for t in range(n_tiles):
            if fill_ltr:
                tx0 = (t % total_cols) * 8
                ty0 = (t // total_cols) * 8
            else:
                slab = t // tiles_per_slab
                t_in_slab = t % tiles_per_slab
                tx0 = (slab * cols + t_in_slab % cols) * 8
                ty0 = (t_in_slab // cols) * 8
            tile = d.tile_bytes[t * 64:(t + 1) * 64]
            for py in range(8):
                for px in range(8):
                    img.setPixel(tx0 + px, ty0 + py, tile[py * 8 + px])
        return img

    def _cell_uncovered_pixel_count(self, cell_idx: int) -> int:
        """Number of pixels inside the cell's bbox that no OAM covers."""
        if self._current_decoded is None:
            return 0
        d = self._current_decoded
        if not (0 <= cell_idx < len(d.ncer.cells)):
            return 0
        cell = d.ncer.cells[cell_idx]
        xmin, ymin, xmax, ymax = btchr.cell_bbox(cell)
        w = xmax - xmin
        h = ymax - ymin
        if w <= 0 or h <= 0:
            return 0
        mask = btchr.oam_coverage_mask(cell, w, h, xmin, ymin)
        return (w * h) - sum(mask)

    def _sprite_has_gaps(self) -> bool:
        """True if any cell of the current sprite has uncovered pixels."""
        if self._current_decoded is None:
            return False
        d = self._current_decoded
        return any(
            self._cell_uncovered_pixel_count(i) > 0
            for i in range(len(d.ncer.cells))
        )

    def _on_show_red_overlay_toggled(self, _checked: bool) -> None:
        """Invalidate cell pixmap cache and refresh both previews so the
        overlay toggle takes effect immediately (cells preview reads the
        checkbox directly; the sheet preview re-renders in cells mode)."""
        self._cell_pixmaps = []
        self._refresh_preview()
        self._refresh_sheet_preview()

    def _orphaned_opaque_pixels(self) -> int:
        """Non-transparent pixels in tiles no OAM references (won't render)."""
        if self._current_decoded is None:
            return 0
        d = self._current_decoded
        return btchr.count_orphaned_opaque_pixels(
            d.tile_bytes, d.ncer.cells, d.ncer.boundary_bytes,
        )

    def _update_coverage_note(self) -> None:
        """Flag sprite issues that stop it rendering correctly, and point at
        the fix. Currently two, shown together when both apply:

        - Over the party-viewer VRAM cap (tiles/cell > 512): the sprite won't
          render in the party viewer / gallery (and large battles may crash).
        - Orphaned content: non-transparent pixels in tiles no OAM references
          (art stranded by earlier OAM edits) — never renders; Compress OAM
          re-covers from the visible cells and drops the strays.

        Silent for normal, in-budget sprites.
        """
        # The red-tint overlay toggles are a separate geometric preview aid
        # (bbox holes); keep them keyed to actual gaps, independent of this note.
        sprite_has_gaps = self._sprite_has_gaps()
        self._show_red_overlay_cb.setEnabled(sprite_has_gaps)
        self._bake_red_overlay_cb.setEnabled(sprite_has_gaps)

        lines: List[str] = []
        d = self._current_decoded
        if d is not None:
            n_cells = len(d.ncer.cells)
            fs = btchr.derived_footprint_scale(d.n_tiles, n_cells) if n_cells else 0
            cap = btchrspr.PARTY_VIEWER_TPF_CAP
            if fs > cap:
                lines.append(
                    f"{fs} tiles/cell exceeds the {cap}-tile party-viewer VRAM "
                    "cap — this sprite won't render in the party viewer / gallery "
                    "(large battles may also crash). Try \"Compress OAM\"; a "
                    "genuinely large boss may not fit."
                )
            orphaned = self._orphaned_opaque_pixels()
            if orphaned > 0:
                lines.append(
                    f"{orphaned} non-transparent pixel{'s' if orphaned != 1 else ''} "
                    "sit in tiles no OAM references, so they won't render in-game "
                    "(usually left over from OAM edits). Click \"Compress OAM\" to "
                    "re-cover the sprite and clear them."
                )
        if not lines:
            self._coverage_note.setVisible(False)
            return
        self._coverage_note.setText("\n".join(lines))
        self._coverage_note.setVisible(True)

    def _confirm_uncovered_content(self, lost: int) -> bool:
        """Ask the user whether to proceed when the PNG has non-transparent
        pixels in regions no OAM covers. Those pixels get written to NCGR
        tile storage but the engine never draws them, so the visible import
        is silently smaller than the source PNG."""
        reply = QMessageBox.warning(
            self,
            "Content outside OAM coverage",
            f"The imported image has {lost} non-transparent pixel"
            f"{'s' if lost != 1 else ''} in regions that no OAM covers. "
            "Those pixels will not appear in-game — this sprite's OAM "
            "layout leaves gaps inside its bounding box.\n\n"
            "Tip: enable \"Build new OAM layout on import\" to reshape the "
            "sprite to fit the whole image instead.\n\n"
            "Import anyway?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        return reply == QMessageBox.Yes

    def _cell_ctx(self) -> Optional[CellPngContext]:
        """Build a CellPngContext over the current BTCHR sprite.

        BTCHR is always 8bpp with a single 256-entry palette, so the
        bit-depth / palette decisions are trivial — they all collapse
        into a fixed context the shared cell_png_io helpers consume.
        """
        if self._current_decoded is None:
            return None
        d = self._current_decoded
        return CellPngContext(
            ncer=d.ncer,
            tile_bytes=d.tile_bytes,
            n_tiles=d.n_tiles,
            palette=list(d.palette),
            is_8bpp=True,
        )

    def _cell_layout(self):
        """Thin wrapper around shared_cell_layout, gated on the current sprite."""
        if self._current_decoded is None:
            return None
        return shared_cell_layout(self._current_decoded.ncer)

    def _render_cells_qimage(self, columns: int) -> Optional[QImage]:
        ctx = self._cell_ctx()
        if ctx is None:
            return None
        return shared_render_cells_qimage(ctx, columns)

    def _render_one_cell_qimage(self, cell_idx: int) -> Optional[QImage]:
        ctx = self._cell_ctx()
        if ctx is None:
            return None
        return shared_render_one_cell_qimage(ctx, cell_idx)

    def _refresh_sheet_preview(self) -> None:
        if self._current_decoded is None:
            self._sheet_preview.setText("Select a digimon.")
            self._sheet_src_qimage = None
            return
        if self._view_mode_combo.currentData() == "cells":
            columns_val = self._sheet_columns_spin.value()
            img = self._render_cells_qimage(columns_val)
            if img is not None and self._show_red_overlay_cb.isChecked():
                layout = self._cell_layout()
                if layout is not None:
                    img = overlay_red_gaps_composite(
                        img,
                        ncer=self._current_decoded.ncer,
                        layout=layout,
                        columns=columns_val,
                    )
        else:
            img = self._render_sheet_qimage(
                self._sheet_cols_spin.value(),
                self._sheet_columns_spin.value(),
                fill_ltr=self._sheet_fill_ltr_cb.isChecked(),
            )
        if img is None:
            self._sheet_preview.setText("(empty)")
            self._sheet_src_qimage = None
            return
        pm = QPixmap.fromImage(img)
        scaled = pm.scaled(
            pm.width() * PREVIEW_ZOOM, pm.height() * PREVIEW_ZOOM,
            Qt.KeepAspectRatio, Qt.FastTransformation,
        )
        self._sheet_preview.setPixmap(scaled)
        # Without this, `setWidgetResizable(True)` clamps the QLabel to
        # the viewport and the QScrollArea never grows a vertical bar —
        # tall narrow sheets (small width + big tile bank) would silently
        # crop. Setting the minimum to the pixmap size forces the scroll
        # area to honor the pixmap's height.
        self._sheet_preview.adjustSize()
        # Cache the native Indexed8 source — sampling reads RGB through
        # `pixelColor` regardless of source format, so this works the same
        # as the RGBA cells preview.
        self._sheet_src_qimage = img
        self._sheet_src_size = (pm.width(), pm.height())
        self._sheet_pix_size = (scaled.width(), scaled.height())

    # ---- OAM map --------------------------------------------------------

    _OAM_MAP_ZOOM = 3

    @staticmethod
    def _fill_qcolor(frac: float) -> QColor:
        """Art-fill → colour: red (empty, cheap to cut) → amber → green (solid).
        Saturated so it reads on white ground and on the dark sprite alike."""
        if frac < 0.5:
            t = frac / 0.5
            return QColor(220, int(45 + 120 * t), 35)
        t = (frac - 0.5) / 0.5
        return QColor(int(215 - 180 * t), 160, int(20 + 70 * t))

    def _oam_union_qimage(self, xo: int, yo: int, w: int, h: int) -> QImage:
        """Union render of all frames on the shared canvas, gamma-lifted (the
        demon sprites are near-black) onto a white ground so the OAM overlay
        reads. A tile shows the first frame that draws it."""
        d = self._current_decoded
        boundary = d.ncer.boundary_bytes
        pal = d.palette
        lut = [int(255 * (i / 255.0) ** 0.5) for i in range(256)]
        imgs = [
            btchr.render_cell_indexed(c, d.tile_bytes, w, h, xo, yo, boundary)
            for c in d.ncer.cells
        ]
        buf = bytearray(b"\xfa\xf7\xf7\xff" * (w * h))  # white BGRA ground
        for i in range(w * h):
            for cb in imgs:
                v = cb[i]
                if v:
                    r, g, b = pal[v]
                    o = i * 4
                    buf[o] = lut[b]; buf[o + 1] = lut[g]; buf[o + 2] = lut[r]
                    break
        return QImage(bytes(buf), w, h, QImage.Format_RGB32).copy()

    def _refresh_oam_map(self) -> None:
        if self._current_decoded is None or not self._current_decoded.ncer.cells:
            self._oam_map_label.setText("Select a sprite.")
            self._oam_stats_label.setText("")
            return
        an = btchr.analyze_oam_cover(
            self._current_decoded.ncer, self._current_decoded.tile_bytes
        )
        xo, yo = an.origin
        w, h = an.size
        z = self._OAM_MAP_ZOOM
        img = self._oam_union_qimage(xo, yo, w, h).scaled(
            w * z, h * z, Qt.IgnoreAspectRatio, Qt.FastTransformation
        )
        p = QPainter(img)
        p.setPen(QPen(QColor(70, 70, 90, 45)))  # faint tile grid
        for gx in range(0, w + 1, 8):
            p.drawLine(gx * z, 0, gx * z, h * z)
        for gy in range(0, h + 1, 8):
            p.drawLine(0, gy * z, w * z, gy * z)
        font = QFont(); font.setPixelSize(11); font.setBold(True)
        p.setFont(font)
        for b in an.boxes:
            col = self._fill_qcolor(b.fill)
            x0, y0 = (b.x - xo) * z, (b.y - yo) * z
            pen = QPen(col); pen.setWidth(2)
            p.setPen(pen); p.setBrush(Qt.NoBrush)
            p.drawRect(x0, y0, b.w * z - 1, b.h * z - 1)
            # Label the *tiles* it costs (slots × stride) so the numbers sum to
            # fs — a "4" on a tiny 8×8 box makes its 3-tile waste obvious.
            lbl = str(b.slots * an.slot_tiles)
            p.fillRect(x0 + 2, y0 + 2, 7 * len(lbl) + 5, 14, QColor(0, 0, 0, 150))
            p.setPen(QColor(255, 255, 255))
            p.drawText(x0 + 4, y0 + 13, lbl)
        p.end()
        self._oam_map_label.setPixmap(QPixmap.fromImage(img))
        self._oam_map_label.adjustSize()

        cap = btchrspr.PARTY_VIEWER_TPF_CAP
        over = an.fs - cap
        verdict = (
            f"<span style='color:#e46b73'>over the {cap} party cap by {over}</span>"
            if over > 0 else
            f"<span style='color:#57c489'>{-over} under the {cap} cap</span>"
        )
        oam_warn = " ⚠ over 128" if an.n_oams > 128 else ""
        # This cover re-lays every frame into ONE shared layout. When the sprite
        # stores per-frame OAM positions, that shared layout can cost more than
        # its current footprint — flag it so the number isn't a surprise.
        stored_note = ""
        if an.stored_fs != an.fs:
            grew = an.fs > an.stored_fs
            stored_note = (
                f"<br><span style='color:{'#e2a54b' if grew else '#999'}'>"
                f"stored fs {an.stored_fs} — this sprite uses per-frame OAM "
                f"layouts; shown as one shared cover ({an.fs})"
                + (", which costs more (per-frame positions can't be kept here)"
                   if grew else "") + ".</span>"
            )
        self._oam_stats_label.setText(
            f"<b>fs {an.fs}</b> tiles/cell &nbsp;·&nbsp; {an.total_slots} slots "
            f"× {an.slot_tiles} &nbsp;·&nbsp; {an.n_oams} OBJs{oam_warn} "
            f"&nbsp;·&nbsp; {verdict}<br>"
            "<span style='color:#999'>box colour = art fill "
            "(<span style='color:#dc4823'>red = mostly empty, cheap to cut</span> · "
            "<span style='color:#57a030'>green = solid, essential</span>) · "
            f"number = tiles it costs (they sum to fs {an.fs}; each OBJ rounds up "
            f"to a {an.slot_tiles}-tile slot) · cut a red OBJ's art in every frame "
            "to reclaim its tiles</span>" + stored_note
        )

    # ---- manual OAM editor ---------------------------------------------

    def _refresh_oam_tab_if_active(self) -> None:
        """After the sprite changed (select / pak edit): if the OAM tab is open,
        re-seed the editor (edit mode) or re-render the map (view mode)."""
        if self._tabs.currentIndex() != getattr(self, "_oam_tab_index", -1):
            return
        if getattr(self, "_oam_edit_cb", None) and self._oam_edit_cb.isChecked():
            self._seed_oam_editor()
        else:
            self._refresh_oam_map()
            self._oam_dirty = False

    def _on_oam_shape_changed(self, _idx: int) -> None:
        tw, th = self._oam_shape_combo.currentData()
        self._oam_edit_canvas.set_shape(tw, th)

    def _on_oam_edit_toggled(self, on: bool) -> None:
        for w in (self._oam_shape_label, self._oam_shape_combo,
                  self._oam_reset_btn, self._oam_apply_btn):
            w.setVisible(on)
        self._oam_scroll.setVisible(not on)
        self._oam_edit_scroll.setVisible(on)
        if on:
            self._seed_oam_editor()
        else:
            # back to the read-only map (reflects any applied change)
            self._oam_dirty = True
            self._refresh_oam_map()
            self._oam_dirty = False

    def _seed_oam_editor(self) -> None:
        """Load the canvas from the current sprite's stored OAM cover, so editing
        starts from what's there rather than a blank grid."""
        d = self._current_decoded
        if d is None or not d.ncer.cells:
            return
        an = btchr.analyze_oam_cover(d.ncer, d.tile_bytes)
        xo, yo = an.origin
        w, h = an.size
        self._oam_edit_origin = (xo, yo)
        base = self._oam_union_qimage(xo, yo, w, h)
        cell_indexed = [
            btchr.render_cell_indexed(c, d.tile_bytes, w, h, xo, yo, d.ncer.boundary_bytes)
            for c in d.ncer.cells
        ]
        union, *_ = ncer_mod.union_tile_mask(
            cell_indexed, [(w, h)] * len(d.ncer.cells), 1
        )
        rects = [(b.tile_col, b.tile_row, b.w // 8, b.h // 8) for b in an.boxes]
        self._oam_edit_canvas.set_data(
            base, w // 8, h // 8, union, (xo, yo), rects, slot_tiles=an.slot_tiles
        )
        self._on_oam_canvas_changed()

    def _on_oam_canvas_changed(self) -> None:
        d = self._current_decoded
        if d is None:
            return
        rects = self._oam_edit_canvas.rects()
        n_cells = len(d.ncer.cells)
        st = ncer_mod.manual_layout_stats(rects, n_cells, *self._oam_edit_origin)
        uncovered = self._oam_edit_canvas.uncovered_count()
        cap = btchrspr.PARTY_VIEWER_TPF_CAP
        fs = st["fs"]
        # Coverage no longer blocks Apply — uncovered art can be dropped on
        # purpose (behind a warning). Apply just needs a buildable layout.
        self._oam_apply_btn.setEnabled(st["fits"] and st["n_oams"] >= 1)

        cover = (f"<span style='color:#e46b73'>{uncovered} tiles uncovered "
                 "(dropped on apply)</span>" if uncovered
                 else "<span style='color:#57c489'>fully covered</span>")
        over = fs - cap
        vram = (f"<span style='color:#e46b73'>over {cap} by {over}</span>"
                if over > 0 else f"<span style='color:#57c489'>{-over} under {cap}</span>")
        warns = []
        if not st["in_range"]:
            warns.append("<span style='color:#e2a54b'>an OBJ is off-canvas</span>")
        if st["n_oams"] > 128:
            warns.append("<span style='color:#e2a54b'>over 128 OBJs</span>")
        tail = (" · " + " · ".join(warns)) if warns else ""
        self._oam_stats_label.setText(
            f"<b>fs {fs}</b> tiles/cell &nbsp;·&nbsp; {st['n_oams']} OBJs "
            f"&nbsp;·&nbsp; {cover} &nbsp;·&nbsp; {vram}{tail}<br>"
            "<span style='color:#999'>left-click = place shape · click a box + "
            "drag = move · right-click / Delete = remove · Apply relays the sprite "
            "losslessly</span>"
        )

    def _on_oam_apply(self) -> None:
        if self._current_group is None:
            return
        group = self._current_group
        entries = [
            bytes(self._pak.entries[group * btchr.GROUP_SIZE + i]) for i in range(5)
        ]
        uncovered = self._oam_edit_canvas.uncovered_count()
        if uncovered:
            reply = QMessageBox.warning(
                self, "Drop uncovered art?",
                f"{uncovered} tile(s) of art aren't covered by any OBJ and will be "
                "permanently dropped from every frame — that part of the sprite "
                "won't render.\n\nApply anyway?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return
        try:
            spr, old_fs, new_fs = btchrspr.rebuild_with_manual_oam(
                entries, self._oam_edit_canvas.rects(), allow_uncovered=True,
            )
        except ValueError as exc:
            QMessageBox.warning(self, "Can't apply OAM", str(exc))
            return
        if not self._confirm_vram_budget(spr):
            return
        cmd = PortBtchrSpriteCommand(
            self._session, group, spr,
            description=f"Manual OAM for BTCHR 0x{group:04x} ({old_fs}→{new_fs} tpf)",
            on_change=self._refresh_after_pak_change,
        )
        if self._undo_stack is not None:
            self._undo_stack.push(cmd)
        else:
            cmd.redo()
        self._chrsize_rows = _load_chrsize_rows_live(self._session)
        self._oam_edit_cb.setChecked(False)  # back to the map, now showing the result

    def _on_sheet_cols_changed(self, value: int) -> None:
        if self._current_group is not None:
            self._sheet_cols_overrides[self._current_group] = value
        self._refresh_sheet_preview()
        self._sheet_dirty = False

    def _on_sheet_columns_changed(self, value: int) -> None:
        if self._current_group is not None:
            self._sheet_columns_overrides[self._current_group] = value
        self._refresh_sheet_preview()
        self._sheet_dirty = False

    def _on_sheet_fill_changed(self, checked: bool) -> None:
        if self._current_group is not None:
            self._sheet_fill_ltr_overrides[self._current_group] = checked
        self._refresh_sheet_preview()
        self._sheet_dirty = False

    def _on_view_mode_changed(self, _idx: int) -> None:
        mode = self._view_mode_combo.currentData()
        if self._current_group is not None:
            self._view_mode_overrides[self._current_group] = mode
        self._update_view_mode_controls()
        self._refresh_sheet_preview()
        self._sheet_dirty = False

    def _update_view_mode_controls(self) -> None:
        """Grey out tile-layout knobs in cells mode — they don't apply
        when each slab is a fully-composed cell instead of a raw tile run.
        Columns still applies (cells per row), so it stays enabled."""
        is_cells = self._view_mode_combo.currentData() == "cells"
        self._sheet_cols_spin.setEnabled(not is_cells)
        self._sheet_fill_ltr_cb.setEnabled(not is_cells)

    def _on_tab_changed(self, idx: int) -> None:
        if idx == 1 and self._sheet_dirty:
            self._refresh_sheet_preview()
            self._sheet_dirty = False
        if idx == getattr(self, "_oam_tab_index", -1):
            # In edit mode the canvas keeps its own state; only the read-only map
            # needs a (lazy) re-render.
            if not (getattr(self, "_oam_edit_cb", None)
                    and self._oam_edit_cb.isChecked()) and self._oam_dirty:
                self._refresh_oam_map()
                self._oam_dirty = False
        if idx == getattr(self, "_palette_tab_index", -1):
            # Render the current sprite into the tab's reference preview + reflow.
            self._refresh_preview()
            self._reflow_palette()

    # ---- tile sheet PNG ------------------------------------------------

    def _on_export_sprite_sheet(self) -> None:
        """Consolidated sprite-sheet export — routes to the per-cell path when
        'Separate frames' is ticked, else the single composite."""
        if self._separate_frames_cb.isChecked():
            self._on_export_per_cell_pngs()
        else:
            self._on_export_cells_sheet_png()

    def _on_import_sprite_sheet(self) -> None:
        """Consolidated sprite-sheet import (paints into the existing OAM
        layout) — per-cell path when 'Separate frames' is ticked, else the
        single composite."""
        if self._separate_frames_cb.isChecked():
            self._on_import_per_cell_pngs()
        else:
            self._on_import_cells_sheet_png()

    def _on_export_cells_sheet_png(self) -> None:
        """Export all cells composited into one row as a single PNG.

        Independent of the Tile-sheet tab's view-mode combo — always the
        OAM cells composite, one row (columns = n_cells), so the file
        matches the attached-style reference layout every time. Embeds
        ``btchr_mode=cells`` so ``Import tile sheet PNG`` can round-trip
        it. Read-only w.r.t. the ROM.
        """
        if self._current_decoded is None or self._current_group is None:
            return
        d = self._current_decoded
        n_cells = len(d.ncer.cells)
        if n_cells == 0:
            QMessageBox.critical(
                self, "Export failed",
                "Sprite has no cells — nothing to export.",
            )
            return
        columns = n_cells
        img = self._render_cells_qimage(columns)
        if img is None:
            QMessageBox.critical(self, "Export failed", "Render failed.")
            return
        if self._bake_red_overlay_cb.isChecked():
            layout = self._cell_layout()
            if layout is not None:
                img = overlay_red_gaps_composite(
                    img, ncer=d.ncer, layout=layout, columns=columns,
                )
        img.setText("btchr_mode", "cells")
        img.setText("btchr_columns", str(columns))
        suggested = f"btchr_cells_0x{self._current_group:04x}.png"
        path, _ = QFileDialog.getSaveFileName(
            self, "Export cells sheet PNG", suggested, "PNG (*.png)"
        )
        if not path:
            return
        if not img.save(path, "PNG"):
            QMessageBox.critical(self, "Export failed", f"Could not write {path}.")

    def _on_import_cells_sheet_png(self) -> None:
        """Import one composite sprite-sheet PNG (all frames side by side).

        Honours the "Build new OAM layout on import" checkbox: on → split the
        sheet into the sprite's frames and rebuild a fresh OAM sized to them;
        off → paint the sheet into the existing OAM rectangles (symmetric with
        :meth:`_on_export_cells_sheet_png`, via :meth:`_import_cells_png`).
        """
        if self._current_decoded is None or self._current_group is None:
            return
        d = self._current_decoded
        path, _ = QFileDialog.getOpenFileName(
            self, "Import sprite sheet PNG", "", "PNG (*.png)"
        )
        if not path:
            return
        img = QImage(path)
        if img.isNull():
            QMessageBox.critical(self, "Import failed", f"Could not read {path}.")
            return
        if self._build_oam_on_import_cb.isChecked():
            n_cells = len(d.ncer.cells)
            imgs = self._split_cells_sheet(img, n_cells) if n_cells > 1 else [img]
            if imgs is None:
                return
            self._build_new_oam_from_images(imgs)
        else:
            self._import_cells_png(img, d)

    def _on_export_sheet_png(self) -> None:
        if self._current_decoded is None or self._current_group is None:
            return
        n_tiles = self._current_decoded.n_tiles
        if n_tiles == 0:
            QMessageBox.critical(self, "Export failed", "Sprite has no tiles.")
            return
        suggested = f"btchr_chr_0x{self._current_group:04x}.png"
        path, _ = QFileDialog.getSaveFileName(
            self, "Export tile sheet PNG", suggested, "PNG (*.png)"
        )
        if not path:
            return
        # Export uses the preview's current view mode + layout knobs so
        # the file the user sees in their image editor matches the on-
        # screen layout. All embed in PNG tEXt chunks so re-import
        # recovers the original layout regardless of the editor's
        # current widget state (which is per-session only).
        mode = self._view_mode_combo.currentData()
        columns_val = self._sheet_columns_spin.value()
        if mode == "cells":
            img = self._render_cells_qimage(columns_val)
            if img is None:
                QMessageBox.critical(
                    self, "Export failed", f"Could not write {path}.",
                )
                return
            if self._bake_red_overlay_cb.isChecked():
                layout = self._cell_layout()
                if layout is not None:
                    img = overlay_red_gaps_composite(
                        img,
                        ncer=self._current_decoded.ncer,
                        layout=layout,
                        columns=columns_val,
                    )
            img.setText("btchr_mode", "cells")
            img.setText("btchr_columns", str(columns_val))
        else:
            cols_val = self._sheet_cols_spin.value()
            fill_ltr = self._sheet_fill_ltr_cb.isChecked()
            img = self._render_sheet_qimage(
                cols_val, columns_val, fill_ltr=fill_ltr,
            )
            if img is None:
                QMessageBox.critical(
                    self, "Export failed", f"Could not write {path}.",
                )
                return
            img.setText("btchr_mode", "tiles")
            img.setText("btchr_cols", str(cols_val))
            img.setText("btchr_columns", str(columns_val))
            img.setText("btchr_fill", "ltr" if fill_ltr else "ttb")
        if not img.save(path, "PNG"):
            QMessageBox.critical(self, "Export failed", f"Could not write {path}.")

    def _on_import_sheet_png(self) -> None:
        if self._current_decoded is None or self._current_group is None:
            return
        d = self._current_decoded
        n_tiles = d.n_tiles
        path, _ = QFileDialog.getOpenFileName(
            self, "Import tile sheet PNG", "", "PNG (*.png)"
        )
        if not path:
            return
        img = QImage(path)
        if img.isNull():
            QMessageBox.critical(self, "Import failed", f"Could not read {path}.")
            return
        # Branch on embedded mode tag — cells mode lays the bank out by
        # OAM rectangles (uniform per-cell grid), not 8×8 tiles, so the
        # size + tile-walk logic below doesn't apply. Falls back to the
        # live combo for PNGs made externally (no metadata).
        embedded_mode = img.text("btchr_mode")
        if embedded_mode == "cells" or (
            not embedded_mode
            and self._view_mode_combo.currentData() == "cells"
        ):
            self._import_cells_png(img, d)
            return
        if img.width() % 8 != 0 or img.height() % 8 != 0:
            QMessageBox.critical(
                self, "Bad image size",
                f"Sheet dimensions must be multiples of 8 px "
                f"(got {img.width()}×{img.height()}).",
            )
            return
        total_cols = img.width() // 8
        rows = img.height() // 8
        n_input_tiles = total_cols * rows
        # Strict tile count for v1 — OAMs index into a bank of exactly
        # `n_tiles` 8bpp tiles, and chrsize.bin records that budget. Growing
        # or shrinking the sheet without updating those would crash or
        # render garbage. Trailing blank tiles in the grid are fine: any
        # tile beyond `n_tiles` is ignored.
        if n_input_tiles < n_tiles:
            QMessageBox.critical(
                self, "Bad image size",
                f"Sheet has {n_input_tiles} tiles ({total_cols}×{rows}); "
                f"sprite needs {n_tiles}. Widen or lengthen the sheet.",
            )
            return
        # Inverse of the slab layout used at export/preview. Prefer the
        # PNG's embedded `btchr_columns` / `btchr_fill` tEXt chunks
        # (written on export) so a session-fresh import without the
        # right spinner / checkbox state still recovers the original
        # tile order. Falls back to the live controls for PNGs made
        # externally or pre-metadata exports; collapses to columns==1
        # (legacy row-major) when neither is available.
        embedded_columns = img.text("btchr_columns")
        if embedded_columns:
            try:
                columns = max(1, int(embedded_columns))
            except ValueError:
                columns = max(1, self._sheet_columns_spin.value())
        else:
            columns = max(1, self._sheet_columns_spin.value())
        embedded_fill = img.text("btchr_fill")
        if embedded_fill in ("ltr", "ttb"):
            fill_ltr = (embedded_fill == "ltr")
        else:
            fill_ltr = self._sheet_fill_ltr_cb.isChecked()
        if total_cols % columns != 0:
            QMessageBox.critical(
                self, "Bad image size",
                f"Sheet width ({total_cols} tiles) is not divisible by "
                f"the Columns setting ({columns}). Either reflow the PNG "
                f"or set Columns to a divisor.",
            )
            return
        cols_per_slab = total_cols // columns
        tiles_per_slab = cols_per_slab * rows

        use_indexed = img.format() == QImage.Format_Indexed8
        if not use_indexed:
            img = img.convertToFormat(QImage.Format_RGBA8888)

        # Decide the source of truth for colours.
        #
        # - use_indexed + checkbox + non-trivial PLTE → use the PNG's
        #   embedded colour table verbatim. Aseprite/GIMP ship the working
        #   palette there, so the tile indices in the file only make sense
        #   alongside it.
        # - not use_indexed + checkbox → build a fresh 256-colour palette
        #   from the PNG's opaque pixels via median-cut, then nearest-match
        #   every pixel against the new palette. This is the natural
        #   workflow for a hand-painted RGB sprite that doesn't yet have a
        #   reduced palette.
        # - else (checkbox off, or PNG carries no real palette) → keep the
        #   existing NCLR; index the PNG against it.
        checkbox_on = self._import_pal_with_sheet_cb.isChecked()
        pal_from_plte = (
            use_indexed and checkbox_on and len(img.colorTable()) >= 2
        )
        pal_from_quant = (not use_indexed) and checkbox_on
        rebuild_palette = pal_from_plte or pal_from_quant

        if rebuild_palette:
            built = build_palette_from_png(img, total_slots=256)
            if built is None:
                QMessageBox.critical(
                    self, "PNG is fully transparent",
                    "Cannot rebuild a palette from a PNG with no opaque pixels.",
                )
                return
            new_palette: List[Tuple[int, int, int]] = list(built)
        else:
            new_palette = list(d.palette)

        new_tiles = bytearray(n_tiles * 64)
        for t in range(n_tiles):
            if fill_ltr:
                tx0 = (t % total_cols) * 8
                ty0 = (t // total_cols) * 8
            else:
                slab = t // tiles_per_slab
                t_in_slab = t % tiles_per_slab
                tx0 = (slab * cols_per_slab + t_in_slab % cols_per_slab) * 8
                ty0 = (t_in_slab // cols_per_slab) * 8
            if use_indexed:
                for py in range(8):
                    for px in range(8):
                        new_tiles[t * 64 + py * 8 + px] = (
                            img.pixelIndex(tx0 + px, ty0 + py)
                        )
            elif pal_from_quant:
                for py in range(8):
                    for px in range(8):
                        c = img.pixelColor(tx0 + px, ty0 + py)
                        if c.alpha() < 128:
                            idx = 0
                        else:
                            idx = nearest_idx_opaque(
                                c.red(), c.green(), c.blue(), new_palette,
                            )
                        new_tiles[t * 64 + py * 8 + px] = idx
            else:
                rgba_buf = bytearray(64 * 4)
                for py in range(8):
                    for px in range(8):
                        c = img.pixelColor(tx0 + px, ty0 + py)
                        off = (py * 8 + px) * 4
                        rgba_buf[off] = c.red()
                        rgba_buf[off + 1] = c.green()
                        rgba_buf[off + 2] = c.blue()
                        rgba_buf[off + 3] = c.alpha()
                indices = mchr.quantize_rgba_to_indices(bytes(rgba_buf), d.palette)
                for k, idx in enumerate(indices):
                    new_tiles[t * 64 + k] = idx

        group = self._current_group
        orig_ncgr_raw = sprite.decompress_rle30(
            self._pak.entries[self._ncgr_entry_idx(group)]
        )
        new_ncgr = sprite.build_ncgr_from_template(bytes(new_tiles), orig_ncgr_raw)
        compressed = sprite.compress_rle30(new_ncgr)
        replacements = [(BTCHR_PAK, self._ncgr_entry_idx(group), compressed)]

        if rebuild_palette:
            nclr_raw = sprite.decompress_rle30(
                self._pak.entries[self._nclr_entry_idx(group)]
            )
            new_nclr = sprite.build_nclr_from_template(nclr_raw, {0: new_palette})
            replacements.append((
                BTCHR_PAK,
                self._nclr_entry_idx(group),
                sprite.compress_rle30(new_nclr),
            ))
            desc = f"Import BTCHR tile sheet + palette 0x{group:04x}"
        else:
            desc = f"Import BTCHR tile sheet 0x{group:04x}"

        cmd = ReplaceSpriteCommand(
            self._session,
            replacements,
            description=desc,
            on_change=self._refresh_after_pak_change,
        )
        if self._undo_stack is not None:
            self._undo_stack.push(cmd)
        else:
            cmd.redo()

    def _import_cells_png(self, img: QImage, d: btchr.BtchrDigimon) -> None:
        """Round-trip a cells-mode PNG back into the tile bank.

        Delegates the OAM-inverse decode to ``cell_png_io``; this method
        handles BTCHR-specific bits: reading the embedded column count,
        deciding whether to rebuild the palette (the checkbox), and
        wrapping the result in a ReplaceSpriteCommand.
        """
        layout = self._cell_layout()
        if layout is None:
            QMessageBox.critical(
                self, "Import failed",
                "Current digimon has no cells — nothing to import.",
            )
            return
        n_cells = len(d.ncer.cells)

        # Columns comes from the PNG when available, else the live spinner.
        # Cap at n_cells so an over-large value doesn't produce 0 rows.
        embedded_columns = img.text("btchr_columns")
        if embedded_columns:
            try:
                columns = max(1, min(n_cells, int(embedded_columns)))
            except ValueError:
                columns = max(1, min(n_cells, self._sheet_columns_spin.value()))
        else:
            columns = max(1, min(n_cells, self._sheet_columns_spin.value()))

        # Palette decision — same checkbox semantics as the raw-tiles path.
        use_indexed = img.format() == QImage.Format_Indexed8
        checkbox_on = self._import_pal_with_sheet_cb.isChecked()
        pal_from_plte = (
            use_indexed and checkbox_on and len(img.colorTable()) >= 2
        )
        pal_from_quant = (not use_indexed) and checkbox_on
        rebuild_palette = pal_from_plte or pal_from_quant
        if rebuild_palette:
            built = build_palette_from_png(img, total_slots=256)
            if built is None:
                QMessageBox.critical(
                    self, "PNG is fully transparent",
                    "Cannot rebuild a palette from a PNG with no opaque pixels.",
                )
                return
            new_palette: List[Tuple[int, int, int]] = list(built)
        else:
            new_palette = list(d.palette)

        ctx = CellPngContext(
            ncer=d.ncer,
            tile_bytes=d.tile_bytes,
            n_tiles=d.n_tiles,
            palette=new_palette,
            is_8bpp=True,
        )
        lost = count_uncovered_content_composite(
            img, ncer=d.ncer, layout=layout, columns=columns,
        )
        if lost > 0 and not self._confirm_uncovered_content(lost):
            return
        try:
            new_tiles = import_cells_to_tiles(
                img, ctx=ctx, layout=layout,
                columns=columns, palette=new_palette,
            )
        except CellPngError as exc:
            QMessageBox.critical(self, exc.title, exc.message)
            return

        group = self._current_group
        orig_ncgr_raw = sprite.decompress_rle30(
            self._pak.entries[self._ncgr_entry_idx(group)]
        )
        new_ncgr = sprite.build_ncgr_from_template(new_tiles, orig_ncgr_raw)
        compressed = sprite.compress_rle30(new_ncgr)
        replacements = [(BTCHR_PAK, self._ncgr_entry_idx(group), compressed)]

        if rebuild_palette:
            nclr_raw = sprite.decompress_rle30(
                self._pak.entries[self._nclr_entry_idx(group)]
            )
            new_nclr = sprite.build_nclr_from_template(
                nclr_raw, {0: new_palette},
            )
            replacements.append((
                BTCHR_PAK,
                self._nclr_entry_idx(group),
                sprite.compress_rle30(new_nclr),
            ))
            desc = f"Import BTCHR cells + palette 0x{group:04x}"
        else:
            desc = f"Import BTCHR cells 0x{group:04x}"

        cmd = ReplaceSpriteCommand(
            self._session,
            replacements,
            description=desc,
            on_change=self._refresh_after_pak_change,
        )
        if self._undo_stack is not None:
            self._undo_stack.push(cmd)
        else:
            cmd.redo()

    # ---- per-cell PNG IO -----------------------------------------------

    def _on_export_per_cell_pngs(self) -> None:
        """Write each cell to its own PNG, all sized to the union bbox.

        File naming: ``<base>_cell_<K>.png``. Embeds ``btchr_mode=per_cell``
        and ``btchr_cell=<K>`` so import can validate the set. Uniform
        size across cells means files can be swapped between digimon
        without manual cropping, and matches the engine invariant that
        all 5 cells share one slot.
        """
        if self._current_decoded is None or self._current_group is None:
            return
        d = self._current_decoded
        layout = self._cell_layout()
        if layout is None:
            QMessageBox.critical(
                self, "Export failed",
                "Current digimon has no cells — nothing to export.",
            )
            return
        n_cells = len(d.ncer.cells)
        suggested = f"btchr_chr_0x{self._current_group:04x}.png"
        path, _ = QFileDialog.getSaveFileName(
            self, "Export per-cell PNGs (pick base name)",
            suggested, "PNG (*.png)",
        )
        if not path:
            return
        # Normalise the base: strip the user-typed .png, and also any
        # existing ``_cell_N`` suffix so re-exporting on top of a previous
        # set doesn't produce ``foo_cell_0_cell_0.png``.
        base = path[:-4] if path.lower().endswith(".png") else path
        m = re.match(r"^(.*)_cell_\d+$", base)
        if m:
            base = m.group(1)
        bake_overlay = self._bake_red_overlay_cb.isChecked()
        for ci in range(n_cells):
            img = self._render_one_cell_qimage(ci)
            if img is None:
                QMessageBox.critical(
                    self, "Export failed",
                    f"Could not render cell {ci}.",
                )
                return
            if bake_overlay:
                img = overlay_red_gaps_single_cell(
                    img,
                    ncer=d.ncer,
                    layout=layout,
                    cell_idx=ci,
                )
            img.setText("btchr_mode", "per_cell")
            img.setText("btchr_cell", str(ci))
            img.setText("btchr_n_cells", str(n_cells))
            out_path = f"{base}_cell_{ci}.png"
            if not img.save(out_path, "PNG"):
                QMessageBox.critical(
                    self, "Export failed",
                    f"Could not write {out_path}.",
                )
                return

    @staticmethod
    def _order_frame_files(paths: List[str]) -> List[str]:
        """Order selected frame files by the trailing number in each name
        (so ``*_cell_0.png`` … ``*_cell_4.png`` land in cell order regardless
        of the OS file-dialog's selection order); files without a number sort
        after, lexically."""
        def key(p: str):
            nums = re.findall(r"\d+", os.path.basename(p))
            return (0, int(nums[-1])) if nums else (1, os.path.basename(p).lower())
        return sorted(paths, key=key)

    def _on_import_per_cell_pngs(self) -> None:
        """Import one PNG per frame (multi-select).

        Select exactly one file per cell; they're ordered by the trailing
        number in each name. Honours the "Build new OAM layout on import"
        checkbox: on → rebuild a fresh OAM sized to the frames; off → paint
        the frames into the sprite's existing OAM rectangles.
        """
        if self._current_decoded is None or self._current_group is None:
            return
        d = self._current_decoded
        n_cells = len(d.ncer.cells)
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            f"Import {n_cells} frame PNGs (one per cell, order 0..{n_cells - 1})",
            "", "PNG (*.png)",
        )
        if not paths:
            return
        if len(paths) != n_cells:
            QMessageBox.critical(
                self, "Wrong number of frames",
                f"This sprite has {n_cells} cells — select exactly {n_cells} "
                f"frame PNGs (you selected {len(paths)}).",
            )
            return
        paths = self._order_frame_files(paths)
        pngs: List[QImage] = []
        for p in paths:
            im = QImage(p)
            if im.isNull():
                QMessageBox.critical(self, "Import failed", f"Could not read {p}.")
                return
            pngs.append(im)
        if self._build_oam_on_import_cb.isChecked():
            self._build_new_oam_from_images(pngs)
        else:
            self._paint_per_cell_into_layout(pngs, d)

    def _paint_per_cell_into_layout(
        self, pngs: List[QImage], d: btchr.BtchrDigimon,
    ) -> None:
        """Paint per-cell frames into the sprite's existing OAM rectangles
        (no reshape). Delegates the OAM-inverse decode to
        ``cell_png_io.import_per_cell_to_tiles``; handles palette-rebuild
        dispatch and undo wrapping."""
        if self._current_group is None:
            return
        layout = self._cell_layout()
        if layout is None:
            QMessageBox.critical(
                self, "Import failed",
                "Current digimon has no cells — nothing to import.",
            )
            return
        _, max_w, max_h = layout
        bad = [
            f"{im.width()}×{im.height()}"
            for im in pngs if (im.width(), im.height()) != (max_w, max_h)
        ]
        if bad:
            QMessageBox.critical(
                self, "Frame size mismatch",
                f"Painting into the current layout needs every frame at "
                f"{max_w}×{max_h} px (the sprite's cell size). Enable \"Build "
                "new OAM layout on import\" to import differently-sized frames.",
            )
            return

        # Palette decision — same checkbox semantics as the composite path.
        # ``rebuild_palette`` path delegates to the shared helper so the
        # RGB strip composite stays in one place.
        first_indexed = pngs[0].format() == QImage.Format_Indexed8
        checkbox_on = self._import_pal_with_sheet_cb.isChecked()
        pal_from_plte = (
            first_indexed and checkbox_on and len(pngs[0].colorTable()) >= 2
        )
        pal_from_quant = (not first_indexed) and checkbox_on
        rebuild_palette = pal_from_plte or pal_from_quant
        if rebuild_palette:
            built = build_palette_for_per_cell_import(
                pngs, total_slots=256, max_w=max_w, max_h=max_h,
            )
            if built is None:
                QMessageBox.critical(
                    self, "PNG is fully transparent",
                    "Cannot rebuild a palette from PNGs with no opaque "
                    "pixels.",
                )
                return
            new_palette: List[Tuple[int, int, int]] = list(built)
        else:
            new_palette = list(d.palette)

        ctx = CellPngContext(
            ncer=d.ncer,
            tile_bytes=d.tile_bytes,
            n_tiles=d.n_tiles,
            palette=new_palette,
            is_8bpp=True,
        )
        lost = count_uncovered_content_per_cell(
            pngs, ncer=d.ncer, layout=layout,
        )
        if lost > 0 and not self._confirm_uncovered_content(lost):
            return
        try:
            new_tiles = import_per_cell_to_tiles(
                pngs, ctx=ctx, layout=layout, palette=new_palette,
            )
        except CellPngError as exc:
            QMessageBox.critical(self, exc.title, exc.message)
            return

        group = self._current_group
        orig_ncgr_raw = sprite.decompress_rle30(
            self._pak.entries[self._ncgr_entry_idx(group)]
        )
        new_ncgr = sprite.build_ncgr_from_template(new_tiles, orig_ncgr_raw)
        compressed = sprite.compress_rle30(new_ncgr)
        replacements = [(BTCHR_PAK, self._ncgr_entry_idx(group), compressed)]

        if rebuild_palette:
            nclr_raw = sprite.decompress_rle30(
                self._pak.entries[self._nclr_entry_idx(group)]
            )
            new_nclr = sprite.build_nclr_from_template(
                nclr_raw, {0: new_palette},
            )
            replacements.append((
                BTCHR_PAK,
                self._nclr_entry_idx(group),
                sprite.compress_rle30(new_nclr),
            ))
            desc = f"Import BTCHR per-cell + palette 0x{group:04x}"
        else:
            desc = f"Import BTCHR per-cell 0x{group:04x}"

        cmd = ReplaceSpriteCommand(
            self._session,
            replacements,
            description=desc,
            on_change=self._refresh_after_pak_change,
        )
        if self._undo_stack is not None:
            self._undo_stack.push(cmd)
        else:
            cmd.redo()

    # ---- palette (live grid + inline editor) ---------------------------

    def _sync_palette_grid(self) -> None:
        """Feed the palette grid + inline editor the current sprite's 256-colour
        NCLR. Called after every (re)decode so bank edits / undo reflect live."""
        self._preview_palette = None  # any live recolour preview is now stale
        d = self._current_decoded
        if d is None:
            self._palette_grid.set_palette([])
            self._palette_editor.set_slot(-1)
            return
        self._palette_grid.set_palette(list(d.palette))
        self._pal_scroll.setFixedHeight(
            min(self._palette_grid.height() + 2, PALETTE_SCROLL_MAX_H)
        )
        sel = self._palette_grid.selected()
        self._palette_editor.set_slot(
            sel, self._palette_grid.color_at(sel) if sel >= 0 else (0, 0, 0)
        )
        # Re-snapshot the batch adjuster from the (possibly re-coloured) palette
        # so an Apply lands one undo step and the next delta starts clean.
        self._refresh_palette_adjuster()

    def _refresh_palette_adjuster(self) -> None:
        """Feed the batch adjuster the current multi-selection (slot 0 excluded —
        it's transparent, so recolouring its RGB is pointless)."""
        self._palette_grid.set_preview(None)  # drop any stale transform tint
        slots = [s for s in self._palette_grid.selected_slots() if s != 0]
        colors = [self._palette_grid.color_at(s) for s in slots]
        self._palette_adjuster.set_selection(slots, colors)

    def _reflow_palette(self) -> None:
        """Fit as many swatches per row as the palette pane's width allows
        (≥ the minimum), then re-cap the scroll height to the new grid height."""
        avail = self._pal_scroll.viewport().width()
        cols = max(PALETTE_COLS_MIN, avail // PALETTE_SWATCH)
        self._palette_grid.set_cols(cols)
        self._pal_scroll.setFixedHeight(
            min(self._palette_grid.height() + 2, PALETTE_SCROLL_MAX_H)
        )

    def _pick_slot_from_rgb(self, rgb: Tuple[int, int, int]) -> None:
        """Eyedropper: select the palette slot matching a clicked sprite pixel.
        The rendered pixel IS a palette colour, so an exact match is expected;
        fall back to nearest if the click landed on a blended/overlay pixel."""
        d = self._current_decoded
        if d is None:
            return
        target = (int(rgb[0]), int(rgb[1]), int(rgb[2]))
        match = next(
            (i for i, c in enumerate(d.palette) if tuple(c) == target), None
        )
        if match is None:
            match = min(
                range(len(d.palette)),
                key=lambda i: sum(
                    (a - b) ** 2 for a, b in zip(d.palette[i], target)
                ),
            )
        # Switch to the Palette tab so the picked slot is visible.
        self._tabs.setCurrentIndex(self._palette_tab_index)
        self._palette_grid.select_slot(match)
        x, y = self._palette_grid.slot_top_left(match)
        self._pal_scroll.ensureVisible(x, y, 0, 40)

    def _render_palette(self) -> List[Tuple[int, int, int]]:
        """Palette the cell preview renders with — the live batch-adjust preview
        while dragging, else the sprite's committed NCLR."""
        if self._preview_palette is not None:
            return self._preview_palette
        return self._current_decoded.palette if self._current_decoded else []

    def _on_palette_preview(self, overrides: Dict[int, Tuple[int, int, int]]) -> None:
        """Batch adjuster preview: tint the swatches AND live-recolour the sprite
        (throttled) without committing. Empty ``overrides`` clears both."""
        self._palette_grid.set_preview(overrides)
        d = self._current_decoded
        if d is None:
            return
        if overrides:
            pal = list(d.palette)
            for slot, rgb in overrides.items():
                if 0 <= slot < len(pal):
                    pal[slot] = (int(rgb[0]), int(rgb[1]), int(rgb[2]))
            self._preview_palette = pal
        else:
            self._preview_palette = None
        self._cell_pixmaps = []  # palette changed → re-render the composed cells
        if not self._live_preview_timer.isActive():
            self._live_preview_timer.start()

    # ---- borrow palette (scroll other groups' palettes, preview, then copy) --

    def _build_borrow_palette_section(self) -> QVBoxLayout:
        box = QVBoxLayout()
        box.setSpacing(3)
        title = QLabel("Borrow palette")
        title.setStyleSheet("font-weight: bold;")
        box.addWidget(title)
        hint = QLabel("Scroll another sprite's palette onto this one, then apply.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #888; font-size: 10px;")
        box.addWidget(hint)
        row = QHBoxLayout()
        row.setSpacing(4)
        row.addWidget(QLabel("From #"))
        self._borrow_spin = QSpinBox()
        self._borrow_spin.setRange(0, max(0, self._n_groups - 1))
        self._borrow_spin.setToolTip(
            "Preview this sprite under another group's palette (non-destructive). "
            "\"Use this palette\" copies it in as one undo step."
        )
        self._borrow_spin.valueChanged.connect(self._on_borrow_changed)
        row.addWidget(self._borrow_spin, 1)
        box.addLayout(row)
        # Battle sprites map pixels to slots tuned to their own palette, so a
        # raw copy scrambles the colours — match by brightness so the shading
        # survives. On by default here (off for icons/overworld, which carry
        # foreign palettes fine).
        self._borrow_match_cb = QCheckBox("Match by brightness")
        self._borrow_match_cb.setChecked(True)
        self._borrow_match_cb.setToolTip(wrap_tooltip(
            "Remap each of this sprite's slots to the source colour of nearest "
            "brightness (dark stays dark, highlights stay light) so a borrowed "
            "palette reads coherently. Off = raw slot-for-slot copy."
        ))
        self._borrow_match_cb.toggled.connect(
            lambda _=False: self._on_borrow_changed(self._borrow_spin.value())
        )
        box.addWidget(self._borrow_match_cb)
        self._borrow_name = QLabel("")
        self._borrow_name.setWordWrap(True)
        self._borrow_name.setStyleSheet("font-size: 10px;")
        box.addWidget(self._borrow_name)
        self._borrow_apply_btn = QPushButton("Use this palette")
        self._borrow_apply_btn.setEnabled(False)
        self._borrow_apply_btn.clicked.connect(self._on_borrow_apply)
        box.addWidget(self._borrow_apply_btn)
        return box

    def _group_palette(self, g: int) -> Optional[List[Tuple[int, int, int]]]:
        """The 256-colour NCLR palette of group ``g`` (entry 2), or None."""
        if not (0 <= g < self._n_groups):
            return None
        try:
            nclr_raw = sprite.decompress_rle30(
                self._pak.entries[g * btchr.GROUP_SIZE + 2]
            )
            palettes, _ = sprite.parse_nclr(nclr_raw)
            return list(palettes[0])
        except (ValueError, IndexError):
            return None

    def _group_pixel_counts(self, g: int) -> Optional[List[int]]:
        """Per-slot pixel usage of group ``g``'s sprite (its 8bpp NCGR tiles),
        used to weight the borrow match toward the source's dominant colours.
        Cached — borrow-scrolling revisits the same groups."""
        if g in self._group_counts_cache:
            return self._group_counts_cache[g]
        counts: Optional[List[int]] = None
        try:
            ncgr_raw = sprite.decompress_rle30(
                self._pak.entries[g * btchr.GROUP_SIZE + 1]
            )
            tile_bytes, *_ = sprite.parse_ncgr(ncgr_raw)
            counts = [0] * 256
            for b in tile_bytes:
                counts[b] += 1
        except (ValueError, IndexError):
            counts = None
        self._group_counts_cache[g] = counts
        return counts

    def _reset_borrow(self) -> None:
        """Return the borrow spinner to the current group (= preview own palette)
        and clear any borrow preview. Called on (re)selection."""
        if self._current_group is None:
            return
        self._borrow_spin.blockSignals(True)
        self._borrow_spin.setValue(self._current_group)
        self._borrow_spin.blockSignals(False)
        self._borrow_name.setText("")
        self._borrow_apply_btn.setEnabled(False)

    def _borrowed_palette(self, g: int) -> Optional[List[Tuple[int, int, int]]]:
        """Source group ``g``'s palette as it would land on THIS sprite —
        brightness-matched to the sprite's own slots when the toggle is on,
        else a raw copy."""
        src = self._group_palette(g)
        if src is None:
            return None
        if self._borrow_match_cb.isChecked() and self._current_decoded is not None:
            return intensity_matched_palette(
                list(self._current_decoded.palette), src,
                source_counts=self._group_pixel_counts(g),
            )
        return src

    def _on_borrow_changed(self, g: int) -> None:
        """Live-preview the current sprite under group ``g``'s palette (no
        commit). Selecting the current group clears the preview."""
        if self._current_group is None:
            return
        if g == self._current_group:
            self._preview_palette = None
            self._borrow_name.setText("")
            self._borrow_apply_btn.setEnabled(False)
        else:
            pal = self._borrowed_palette(g)
            if pal is None:
                return
            self._preview_palette = pal
            name = self._name_for_group(g)
            self._borrow_name.setText(
                f"0x{g:04x}" + (f" — {name}" if name else "")
            )
            self._borrow_apply_btn.setEnabled(True)
        self._cell_pixmaps = []
        self._refresh_preview()

    def _on_borrow_apply(self) -> None:
        """Copy the previewed group's palette into this sprite's NCLR — one undo
        step (reuses the batch write). Then clear the borrow preview."""
        if self._current_group is None:
            return
        g = self._borrow_spin.value()
        if g == self._current_group:
            return
        pal = self._borrowed_palette(g)
        if pal is None:
            return
        self._apply_palette_colors({i: c for i, c in enumerate(pal)})
        # _apply_palette_colors → refresh re-decodes + _sync_palette_grid, which
        # clears _preview_palette; snap the spinner back to self.
        self._reset_borrow()

    def _apply_palette_colors(self, mapping: Dict[int, Tuple[int, int, int]]) -> None:
        """Recolour several NCLR slots in one undoable step (the batch adjuster's
        Apply). Same NCLR rebuild as :meth:`_apply_palette_color`, just many
        slots at once."""
        if self._current_decoded is None or self._current_group is None:
            return
        palette = list(self._current_decoded.palette)
        changed = 0
        for slot, rgb in mapping.items():
            new = (int(rgb[0]), int(rgb[1]), int(rgb[2]))
            if 0 <= slot < len(palette) and palette[slot] != new:
                palette[slot] = new
                changed += 1
        if changed == 0:
            return  # no net change (e.g. hue-rotating black) — no empty undo step
        group = self._current_group
        nclr_raw = sprite.decompress_rle30(
            self._pak.entries[self._nclr_entry_idx(group)]
        )
        new_nclr = sprite.build_nclr_from_template(nclr_raw, {0: palette})
        cmd = ReplaceSpriteCommand(
            self._session,
            [(BTCHR_PAK, self._nclr_entry_idx(group),
              sprite.compress_rle30(new_nclr))],
            description=f"Adjust BTCHR palette 0x{group:04x} ({changed} colours)",
            on_change=self._refresh_after_pak_change,
        )
        if self._undo_stack is not None:
            self._undo_stack.push(cmd)
        else:
            cmd.redo()

    def _apply_palette_color(self, slot: int, rgb: Tuple[int, int, int]) -> None:
        """Recolor one palette slot of the sprite's NCLR (5-bit BGR555-quantised
        on save) and push an undoable ReplaceSpriteCommand. Only the NCLR
        changes — the CHR indices still point at the same slot."""
        if self._current_decoded is None or self._current_group is None:
            return
        palette = list(self._current_decoded.palette)
        if not (0 <= slot < len(palette)):
            return
        palette[slot] = (int(rgb[0]), int(rgb[1]), int(rgb[2]))
        group = self._current_group
        nclr_raw = sprite.decompress_rle30(
            self._pak.entries[self._nclr_entry_idx(group)]
        )
        new_nclr = sprite.build_nclr_from_template(nclr_raw, {0: palette})
        cmd = ReplaceSpriteCommand(
            self._session,
            [(BTCHR_PAK, self._nclr_entry_idx(group),
              sprite.compress_rle30(new_nclr))],
            description=f"Edit BTCHR palette 0x{group:04x} slot {slot}",
            on_change=self._refresh_after_pak_change,
        )
        if self._undo_stack is not None:
            self._undo_stack.push(cmd)
        else:
            cmd.redo()

    # ---- palette PNG ---------------------------------------------------

    def _on_export_palette_png(self) -> None:
        if self._current_decoded is None or self._current_group is None:
            return
        palette = self._current_decoded.palette
        suggested = f"btchr_pal_0x{self._current_group:04x}.png"
        path, _ = QFileDialog.getSaveFileName(
            self, "Export palette PNG", suggested, "PNG (*.png)"
        )
        if not path:
            return
        # 16×16 RGB888 grid — index pi sits at (pi % 16, pi // 16). Trivial
        # to edit in any image editor; zoomed swatches re-import cleanly
        # since we sample one pixel per cell.
        img = QImage(16, 16, QImage.Format_RGB888)
        for pi, (r, g, b) in enumerate(palette):
            img.setPixelColor(pi % 16, pi // 16, QColor(r, g, b))
        if not img.save(path, "PNG"):
            QMessageBox.critical(self, "Export failed", f"Could not write {path}.")

    def _on_import_palette_png(self) -> None:
        if self._current_decoded is None or self._current_group is None:
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Import palette PNG", "", "PNG (*.png)"
        )
        if not path:
            return
        img = QImage(path)
        if img.isNull():
            QMessageBox.critical(self, "Import failed", f"Could not read {path}.")
            return
        if img.width() < 16 or img.height() < 16:
            QMessageBox.critical(
                self, "Bad image size",
                f"Palette PNG must be at least 16×16 (got {img.width()}×{img.height()}).",
            )
            return
        img = img.convertToFormat(QImage.Format_RGBA8888)
        # Sample one pixel per swatch — supports a raw 16×16 strip or an
        # editor canvas with uniformly-scaled swatches (e.g. 256×256 with
        # 16×16 cells: top-left pixel of each cell is its colour).
        cell_w = img.width() // 16
        cell_h = img.height() // 16
        colours = []
        for pi in range(256):
            x = (pi % 16) * cell_w
            y = (pi // 16) * cell_h
            c = img.pixelColor(x, y)
            colours.append((c.red(), c.green(), c.blue()))
        group = self._current_group
        nclr_raw = sprite.decompress_rle30(
            self._pak.entries[self._nclr_entry_idx(group)]
        )
        new_nclr = sprite.build_nclr_from_template(nclr_raw, {0: colours})
        compressed = sprite.compress_rle30(new_nclr)
        cmd = ReplaceSpriteCommand(
            self._session,
            [(BTCHR_PAK, self._nclr_entry_idx(group), compressed)],
            description=f"Import BTCHR palette 0x{group:04x}",
            on_change=self._refresh_after_pak_change,
        )
        if self._undo_stack is not None:
            self._undo_stack.push(cmd)
        else:
            cmd.redo()

    # ---- .btchrspr (portable sprite kit) -------------------------------

    def _on_export_btchrspr(self) -> None:
        """Pack the current digimon's full sprite kit into a .btchrspr file.

        Reads the live PAK entries + the session's current
        ``chrsize.bin`` / ``btchrsize.bin`` slots (so an export after an
        in-editor port carries the ported state, not vanilla). ``source_*``
        fields are informational — import preserves the destination's id.
        """
        if self._current_decoded is None or self._current_group is None:
            return
        group = self._current_group
        digimon_id_label = (
            self._chrsize_rows[group][0]
            if group < len(self._chrsize_rows) else group
        )
        suggested = f"btchr_0x{group:04x}_id0x{digimon_id_label:04x}.btchrspr"
        path, _ = QFileDialog.getSaveFileName(
            self, "Export .btchrspr", suggested, ".btchrspr (*.btchrspr)"
        )
        if not path:
            return
        chrsize_word = self._session.current_chrsize_word(group)
        source_id = chrsize_word & 0xFFFF
        source_tpf = (chrsize_word >> 16) & 0xFFFF
        btchrsize_value = self._session.current_btchrsize_value(group)
        try:
            payload = btchrspr.serialize(
                self._pak, group,
                source_digimon_id=source_id,
                source_tpf=source_tpf,
                btchrsize_value=btchrsize_value,
            )
        except (ValueError, IndexError) as exc:
            QMessageBox.critical(self, "Export failed", str(exc))
            return
        try:
            with open(path, "wb") as fh:
                fh.write(payload)
        except OSError as exc:
            QMessageBox.critical(
                self, "Export failed", f"Could not write {path}: {exc}"
            )

    @staticmethod
    def _palette_from_cell_images(imgs: List[QImage]):
        """Median-cut a shared 256-colour palette from every cell image's
        opaque pixels (all cells share one NCLR). Returns None if fully
        transparent."""
        total_h = sum(im.height() for im in imgs)
        max_w = max((im.width() for im in imgs), default=0)
        if max_w == 0 or total_h == 0:
            return None
        stack = QImage(max_w, total_h, QImage.Format_RGBA8888)
        stack.fill(qRgba(0, 0, 0, 0))
        painter = QPainter(stack)
        y = 0
        for im in imgs:
            painter.drawImage(0, y, im)
            y += im.height()
        painter.end()
        built = build_palette_from_png(stack, total_slots=256)
        return None if built is None else list(built)

    def _split_cells_sheet(self, sheet: QImage, n_cells: int) -> Optional[List[QImage]]:
        """Split one composite cells-sheet into ``n_cells`` uniform slot images.

        Grid layout follows the ``btchr_columns`` tEXt chunk that
        ``Export cells sheet`` writes; absent that, a single row of
        ``n_cells``. Returns None (after an error dialog) if the sheet doesn't
        divide into a whole grid of 8-aligned slots."""
        if sheet.isNull():
            QMessageBox.critical(self, "Import failed", "Could not read the sheet PNG.")
            return None
        txt = sheet.text("btchr_columns")
        try:
            columns = max(1, min(n_cells, int(txt))) if txt else n_cells
        except ValueError:
            columns = n_cells
        rows = (n_cells + columns - 1) // columns
        if sheet.width() % columns or sheet.height() % rows:
            QMessageBox.critical(
                self, "Can't split sheet",
                f"A {n_cells}-cell sheet is read as a {columns}×{rows} grid, but "
                f"{sheet.width()}×{sheet.height()} px doesn't divide evenly. Use a "
                f"grid exported from 'Export cells sheet', or pick {n_cells} "
                "separate images instead.",
            )
            return None
        sw, sh = sheet.width() // columns, sheet.height() // rows
        if sw % 8 or sh % 8:
            QMessageBox.critical(
                self, "Bad slot size",
                f"Each cell slot works out to {sw}×{sh} px — both must be "
                "multiples of 8.",
            )
            return None
        return [
            sheet.copy((k % columns) * sw, (k // columns) * sh, sw, sh)
            for k in range(n_cells)
        ]

    def _on_import_custom_cells(self) -> None:
        """Legacy "Import cells → new OAM" entry point (button hidden; folded
        into the Import dropdown + Build-OAM checkbox). Accepts one composite
        cells-sheet PNG (split into the cells' grid) or one PNG per cell, then
        delegates to :meth:`_build_new_oam_from_images`."""
        if self._current_decoded is None or self._current_group is None:
            return
        n_cells = len(self._current_decoded.ncer.cells)
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            f"Import 1 cells-sheet PNG or {n_cells} per-cell PNGs "
            f"(cell order 0..{n_cells - 1})",
            "", "PNG (*.png)",
        )
        if not paths:
            return
        if len(paths) == 1 and n_cells > 1:
            imgs = self._split_cells_sheet(QImage(paths[0]), n_cells)
            if imgs is None:
                return
        elif len(paths) == n_cells:
            imgs = [QImage(p) for p in self._order_frame_files(paths)]
        else:
            QMessageBox.critical(
                self, "Wrong number of images",
                f"This sprite has {n_cells} cells — select one composite sheet "
                f"PNG or exactly {n_cells} per-cell PNGs (you selected "
                f"{len(paths)}).",
            )
            return
        self._build_new_oam_from_images(imgs)

    # ---- raw source-file (Nitro) IO ------------------------------------
    # (entry index, label, extension). Order: the four standard Nitro files an
    # external tool edits, then the mini-header. Entry 0 = mini-header, 1 =
    # NCGR, 2 = NCLR, 3 = NCER, 4 = NANR.
    _SOURCE_COMPONENTS = (
        (1, "NCGR", "NCGR"),
        (2, "NCLR", "NCLR"),
        (3, "NCER", "NCER"),
        (4, "NANR", "NANR"),
        (0, "Mini-header", "bin"),
    )

    def _on_export_source(self, idx: int, name: str, ext: str) -> None:
        """Write one PAK component out as its decompressed standard Nitro file,
        editable in NitroPaint etc."""
        if self._current_group is None:
            return
        group = self._current_group
        raw = sprite.decompress_rle30(
            self._pak.entries[group * btchr.GROUP_SIZE + idx]
        )
        stem = name.lower().replace("-", "")
        path, _ = QFileDialog.getSaveFileName(
            self, f"Export {name}", f"btchr_0x{group:04x}_{stem}.{ext}",
            f"{name} (*.{ext});;All files (*.*)",
        )
        if not path:
            return
        try:
            with open(path, "wb") as fh:
                fh.write(raw)
        except OSError as exc:
            QMessageBox.critical(self, "Export failed", f"Could not write {path}: {exc}")

    def _on_export_all_sources(self) -> None:
        """Batch form of :meth:`_on_export_source`: write every PAK component
        (NCGR/NCLR/NCER/NANR/mini-header) of the current group into one chosen
        folder as decompressed standard Nitro files. Same naming as the single
        export, so the group id keeps them from colliding across sprites."""
        if self._current_group is None:
            return
        group = self._current_group
        directory = QFileDialog.getExistingDirectory(
            self, "Export all source files to folder"
        )
        if not directory:
            return
        written, failed = [], []
        for idx, name, ext in self._SOURCE_COMPONENTS:
            stem = name.lower().replace("-", "")
            raw = sprite.decompress_rle30(
                self._pak.entries[group * btchr.GROUP_SIZE + idx]
            )
            path = os.path.join(directory, f"btchr_0x{group:04x}_{stem}.{ext}")
            try:
                with open(path, "wb") as fh:
                    fh.write(raw)
                written.append(os.path.basename(path))
            except OSError as exc:
                failed.append(f"{os.path.basename(path)}: {exc}")
        if failed:
            QMessageBox.critical(
                self, "Export incomplete",
                "Wrote {}/{} files to:\n{}\n\nFailed:\n{}".format(
                    len(written), len(self._SOURCE_COMPONENTS), directory,
                    "\n".join(failed)),
            )
        else:
            QMessageBox.information(
                self, "Export complete",
                "Wrote {} source files to:\n{}".format(len(written), directory),
            )

    def _on_import_source(self, idx: int, name: str, ext: str) -> None:
        """Replace one PAK component from a standard Nitro file, then re-derive
        the mini-header fs/flag + btchrsize so the sprite stays loadable
        (``btchrspr.rebuild_from_entries``). One undo step."""
        if self._current_decoded is None or self._current_group is None:
            return
        path, _ = QFileDialog.getOpenFileName(
            self, f"Import {name}", "", f"{name} (*.{ext});;All files (*.*)",
        )
        if not path:
            return
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError as exc:
            QMessageBox.critical(self, "Import failed", f"Could not read {path}: {exc}")
            return
        group = self._current_group
        cur = [
            bytes(self._pak.entries[group * btchr.GROUP_SIZE + i]) for i in range(5)
        ]
        digimon_id = self._session.current_chrsize_word(group) & 0xFFFF
        try:
            spr = btchrspr.rebuild_from_entries(cur, digimon_id, {idx: data})
        except Exception as exc:  # noqa: BLE001 — surface any codec error to the user
            QMessageBox.critical(
                self, "Import failed",
                f"Could not rebuild the sprite from this {name}:\n{exc}",
            )
            return
        if not self._confirm_vram_budget(spr):
            return
        cmd = PortBtchrSpriteCommand(
            self._session, group, spr,
            description=f"Import {name} into BTCHR 0x{group:04x}",
            on_change=self._refresh_after_pak_change,
        )
        if self._undo_stack is not None:
            self._undo_stack.push(cmd)
        else:
            cmd.redo()
        self._chrsize_rows = _load_chrsize_rows_live(self._session)

    def _confirm_vram_budget(self, spr: btchrspr.BtchrSprite) -> bool:
        """Warn (Yes/No) when a to-be-applied sprite exceeds the per-cell VRAM
        cap — the one limit for correct display everywhere. Returns True to
        proceed. Shared by the image-build, source-import and .btchrspr paths."""
        cap = btchrspr.PARTY_VIEWER_TPF_CAP  # 512 tiles/cell
        if spr.source_tpf <= cap:
            return True
        total = spr.source_tpf * btchr.GROUP_SIZE
        reply = QMessageBox.warning(
            self, "Sprite over VRAM budget",
            f"{spr.source_tpf} tiles/cell ({total} total) exceeds the "
            f"{cap}-tiles/cell cap ({cap * btchr.GROUP_SIZE} total) — the sprite "
            "will garble or vanish in the party viewer, gallery and multi-sprite "
            "battles.\n\nRun \"Compress OAM\" to shrink it, reduce the frame size, "
            "or edit the OAM by hand.\n\nApply anyway?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        return reply == QMessageBox.Yes

    def _build_new_oam_from_images(self, imgs: List[QImage]) -> None:
        """Rebuild the current sprite with a fresh OAM layout sized to ``imgs``
        (via ``btchrspr.build_from_cells``) — one cell per image, preserving
        the animation cell count. Frames must be 8-px multiples and all the
        same size (the engine's shared-slot invariant). Shared by the Import
        dropdown (Build-OAM on) and the legacy custom-cells button."""
        if self._current_group is None:
            return
        conv: List[QImage] = []
        dims: List[Tuple[int, int]] = []
        for img in imgs:
            if img.isNull():
                QMessageBox.critical(self, "Import failed", "Could not read a PNG.")
                return
            if img.width() % 8 or img.height() % 8:
                QMessageBox.critical(
                    self, "Bad frame size",
                    "Every frame's dimensions must be multiples of 8 px "
                    f"(got {img.width()}×{img.height()}).",
                )
                return
            conv.append(img.convertToFormat(QImage.Format_RGBA8888))
            dims.append((img.width(), img.height()))
        if len(set(dims)) > 1:
            sizes = ", ".join(f"{w}×{h}" for w, h in sorted(set(dims)))
            QMessageBox.critical(
                self, "Frames differ in size",
                f"All frames must be the same size to build an OAM layout "
                f"(got {sizes}).",
            )
            return
        imgs = conv

        palette = self._palette_from_cell_images(imgs)
        if palette is None:
            QMessageBox.critical(
                self, "No opaque pixels",
                "Every imported frame is fully transparent — nothing to build a "
                "palette from.",
            )
            return
        # Quantise each cell against the shared palette (index 0 = transparent).
        cell_indexed: List[bytes] = []
        for img, (w, h) in zip(imgs, dims):
            buf = bytearray(w * h)
            for yy in range(h):
                for xx in range(w):
                    c = img.pixelColor(xx, yy)
                    buf[yy * w + xx] = (
                        0 if c.alpha() < 128
                        else nearest_idx_opaque(c.red(), c.green(), c.blue(), palette)
                    )
            cell_indexed.append(bytes(buf))

        group = self._current_group
        template = [
            bytes(self._pak.entries[group * btchr.GROUP_SIZE + i]) for i in range(5)
        ]
        try:
            spr = btchrspr.build_from_cells(cell_indexed, dims, palette, template)
        except ValueError as exc:
            QMessageBox.critical(self, "Import failed", str(exc))
            return

        if not self._confirm_vram_budget(spr):
            return

        cmd = PortBtchrSpriteCommand(
            self._session, group, spr,
            description=f"Import frames (new OAM) into BTCHR 0x{group:04x}",
            on_change=self._refresh_after_pak_change,
        )
        if self._undo_stack is not None:
            self._undo_stack.push(cmd)
        else:
            cmd.redo()
        self._chrsize_rows = _load_chrsize_rows_live(self._session)

    def _on_compress_oam(self) -> None:
        """Re-cover the selected sprite with occupied-only OAM coverage — same
        pixels, tighter (union) tile bank (``btchrspr.compress_existing``). When
        it can't shrink the footprint it still offers to apply, because the
        re-cover regenerates a clean union layout — a way to reset a sprite left
        in an odd state by earlier edits without reimporting a .btchrspr."""
        if self._current_group is None:
            return
        group = self._current_group
        entries = [
            bytes(self._pak.entries[group * btchr.GROUP_SIZE + i]) for i in range(5)
        ]
        try:
            spr, old_fs, new_fs = btchrspr.compress_existing(entries)
        except ValueError as exc:
            QMessageBox.warning(
                self, "Can't compress", f"This sprite can't be re-covered:\n{exc}"
            )
            return
        if new_fs < old_fs:
            saved = old_fs - new_fs
            pct = 100 * saved // old_fs
            prompt = (
                f"Re-cover BTCHR 0x{group:04x} with occupied-only OAM.\n\n"
                f"footprint_scale: {old_fs} → {new_fs} tiles/cell  "
                f"(−{saved}, −{pct}%)\n\n"
                "Pixels are unchanged — the sprite just needs less VRAM, which "
                "can let it fit budgets it currently overflows (party pool, wild "
                "spawns). Applies as one undoable step.\n\nApply?"
            )
            default = QMessageBox.Yes
        else:
            # No reduction, but re-covering rebuilds a clean union OAM layout at
            # the same pixels — offer it (don't refuse) so a sprite left smaller-
            # but-odd by an earlier edit (e.g. an experimental cover) can be
            # reset here instead of reimporting a .btchrspr.
            change = f"+{new_fs - old_fs}" if new_fs != old_fs else "unchanged"
            prompt = (
                f"BTCHR 0x{group:04x} is already occupied-only — re-covering "
                f"won't shrink it (footprint_scale {old_fs} → {new_fs}, "
                f"{change}).\n\nIt rebuilds a clean union OAM layout at the same "
                "pixels, which resets a sprite left in an odd state by earlier "
                "edits. Re-cover anyway?"
            )
            default = QMessageBox.No
        reply = QMessageBox.question(
            self, "Compress OAM", prompt,
            QMessageBox.Yes | QMessageBox.No, default,
        )
        if reply != QMessageBox.Yes:
            return
        cmd = PortBtchrSpriteCommand(
            self._session, group, spr,
            description=(
                f"Compress OAM of BTCHR 0x{group:04x} ({old_fs}→{new_fs} tpf)"
            ),
            on_change=self._refresh_after_pak_change,
        )
        if self._undo_stack is not None:
            self._undo_stack.push(cmd)
        else:
            cmd.redo()
        self._chrsize_rows = _load_chrsize_rows_live(self._session)

    def _on_compress_oam_fit(self) -> None:
        """Lossless union re-cover; if it still overflows 512, trim the fewest
        faint edge pixels needed to fit and confirm the cost
        (``btchrspr.compress_existing_fit``)."""
        if self._current_group is None:
            return
        group = self._current_group
        entries = [
            bytes(self._pak.entries[group * btchr.GROUP_SIZE + i]) for i in range(5)
        ]
        try:
            result = btchrspr.compress_existing_fit(entries, target=512)
        except ValueError as exc:
            QMessageBox.warning(
                self, "Can't compress", f"This sprite can't be re-covered:\n{exc}"
            )
            return
        if result is None:
            QMessageBox.information(
                self, "Can't fit ≤512",
                f"BTCHR 0x{group:04x} can't reach ≤512 tiles/cell even after "
                "trimming faint edges — its biggest single frame carries too "
                "much solid content. Only reducing the artwork would fit it.",
            )
            return
        spr, old_fs, new_fs, min_opaque, dropped = result
        if new_fs >= old_fs:
            QMessageBox.information(
                self, "Already tight",
                f"BTCHR 0x{group:04x} is already at {old_fs} tiles/cell — nothing "
                "to reclaim.",
            )
            return
        saved = old_fs - new_fs
        pct = 100 * saved // old_fs
        if min_opaque == 1:
            body = (
                f"footprint_scale: {old_fs} → {new_fs} tiles/cell (−{saved}, "
                f"−{pct}%), lossless — fits the 512 party-viewer cap.\n\nApply?"
            )
        else:
            body = (
                f"footprint_scale: {old_fs} → {new_fs} tiles/cell (−{saved}, "
                f"−{pct}%) — now ≤512 (party-viewer cap).\n\nTo fit, it trims "
                f"{dropped} faint edge pixel(s) (tiles with < {min_opaque} opaque "
                f"pixels). Everything else is unchanged.\n\nApply?"
            )
        reply = QMessageBox.question(
            self, "Compress OAM (fit ≤512)", body,
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
        )
        if reply != QMessageBox.Yes:
            return
        desc = (
            f"Compress OAM (fit ≤512) of BTCHR 0x{group:04x} ({old_fs}→{new_fs} tpf"
            + (f", −{dropped}px" if min_opaque > 1 else "") + ")"
        )
        cmd = PortBtchrSpriteCommand(
            self._session, group, spr, description=desc,
            on_change=self._refresh_after_pak_change,
        )
        if self._undo_stack is not None:
            self._undo_stack.push(cmd)
        else:
            cmd.redo()
        self._chrsize_rows = _load_chrsize_rows_live(self._session)

    def reload_after_external_edit(self) -> None:
        """Redecode after a session-level edit made outside this widget (e.g.
        the header-bar 'Compress All Battle Sprite OAMs' batch). Reloads the
        chrsize cache first so the new footprints show, then re-renders."""
        self._chrsize_rows = _load_chrsize_rows_live(self._session)
        self._refresh_after_pak_change()

    def _on_import_btchrspr(self) -> None:
        """Replay a .btchrspr onto the selected group.

        The destination's secondary digimon id (chrsize.lo) is preserved;
        only the tpf and btchrsize follow the imported sprite. Sprites over
        the 512-tiles/cell cap (2560 total) get a confirmation prompt — above
        that they garble/vanish everywhere (party viewer, gallery, and
        multi-sprite battles).
        """
        if self._current_group is None:
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Import .btchrspr", "", ".btchrspr (*.btchrspr)"
        )
        if not path:
            return
        try:
            with open(path, "rb") as fh:
                raw = fh.read()
        except OSError as exc:
            QMessageBox.critical(
                self, "Import failed", f"Could not read {path}: {exc}"
            )
            return
        try:
            spr = btchrspr.parse(raw)
        except ValueError as exc:
            QMessageBox.critical(self, "Import failed", str(exc))
            return
        try:
            spr.ncgr_tile_count  # surface a malformed NCGR before applying
        except (ValueError, struct.error) as exc:
            QMessageBox.critical(
                self, "Import failed", f"Could not inspect NCGR: {exc}"
            )
            return
        if not self._confirm_vram_budget(spr):
            return
        group = self._current_group
        cmd = PortBtchrSpriteCommand(
            self._session,
            group,
            spr,
            description=f"Import .btchrspr into BTCHR 0x{group:04x}",
            on_change=self._refresh_after_pak_change,
        )
        if self._undo_stack is not None:
            self._undo_stack.push(cmd)
        else:
            cmd.redo()
        # chrsize.bin is also touched by the port; refresh the cached
        # parse so the left-list label (``id=DDDD``) and other readers
        # see the post-port tpf next time they re-read.
        self._chrsize_rows = _load_chrsize_rows_live(self._session)

    # ---- Add Entry / Duplicate sprite entry ----------------------------

    def _on_add_entry(self) -> None:
        """Append a new BTCHR group cloned from the current selection.

        Same handler for both the toolbar "+ Add Entry" button (below the
        list) and the panel "Duplicate sprite entry" button (under
        Import btchrspr) — neither needs an extra picker step because
        the source is always the currently-selected group. Confirms
        first so the user can back out without an undo step on the stack.
        """
        if self._current_group is None:
            return
        source = self._current_group
        new_group_idx = self._pak.count // btchr.GROUP_SIZE
        chrsize_word = self._session.current_chrsize_word(source)
        source_id = chrsize_word & 0xFFFF
        name = self._name_for_group(source)
        name_token = f" ({name})" if name else ""
        confirm = QMessageBox.question(
            self,
            "Duplicate sprite entry?",
            f"Append a new BTCHR group at index {new_group_idx} carrying "
            f"a copy of group 0x{source:04x} (id=0x{source_id:02x}{name_token}) "
            f"data?\n\nThe new group will need a sprite_map entry pointing "
            f"at index {new_group_idx} before any enemy can use it.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if confirm != QMessageBox.Yes:
            return
        cmd = AppendBtchrGroupCommand(
            self._session,
            source,
            description=f"Append BTCHR group 0x{new_group_idx:04x} from 0x{source:04x}",
            on_change=self._refresh_after_group_appended,
        )
        if self._undo_stack is not None:
            self._undo_stack.push(cmd)
        else:
            cmd.redo()
        self._list.select_index(cmd.new_group_index)

    def _refresh_after_group_appended(self) -> None:
        """Reconcile the browser's caches with the live BTCHR group count.

        Called from AppendBtchrGroupCommand's redo/undo. Re-syncs the
        local group count, the chrsize-rows cache (left-list label
        source), and the list panel — appending a row on redo, dropping
        it on undo. The sprite_to_base reverse lookup doesn't need to
        change: no sprite_map entry points at the new group yet, so it
        stays nameless in the list (``id=NNNN`` only).
        """
        live_groups = btchr.parse_pak_groups(self._pak)
        if live_groups > self._n_groups:
            for g in range(self._n_groups, live_groups):
                self._labels.append(self._label_for_new_group(g))
                self._list.append_record(g)
            self._n_groups = live_groups
        elif live_groups < self._n_groups:
            for _ in range(self._n_groups - live_groups):
                self._labels.pop()
                self._list.pop_record()
            self._n_groups = live_groups
        # Re-decode the current selection if it survived; otherwise the
        # selection model will land on whatever the list panel picks.
        if self._current_group is not None and self._current_group < live_groups:
            self._refresh_after_pak_change()

    def _label_for_new_group(self, g: int) -> str:
        """Label for a freshly-appended BTCHR group. Mirrors
        ``_build_labels`` for one entry; reads the chrsize word from the
        session's appended-sidecar list so the id token matches the
        u32 the splice path will write."""
        appended_idx = g - self._session.vanilla_btchr_group_count()
        if 0 <= appended_idx < len(self._session._btchr_appended_chrsize):
            digimon_id = self._session._btchr_appended_chrsize[appended_idx] & 0xFFFF
        else:
            digimon_id = -1
        return _format_btchr_label(g, digimon_id, self._name_for_group(g))

    # ---- transparent-color picker --------------------------------------

    @staticmethod
    def _snap5(rgb: Tuple[int, int, int]) -> Tuple[int, int, int]:
        """Project an 8-bit RGB triple onto the 5-bit grid NCLR stores at.
        Two RGBs that snap to the same triple encode to identical NCLR
        bytes — used for both equality checks and remap detection so a
        user-typed colour matches its on-disk neighbour."""
        def s(v: int) -> int:
            return (v * 31 + 127) // 255 * 255 // 31
        return (s(rgb[0]), s(rgb[1]), s(rgb[2]))

    def _cells_source(self):
        """Source-provider callback for the cells preview eyedropper."""
        if self._cells_src_qimage is None:
            return None
        return (
            self._cells_src_qimage,
            self._cells_src_size,
            self._cells_pix_size,
        )

    def _sheet_source(self):
        """Source-provider callback for the tile sheet preview eyedropper."""
        if self._sheet_src_qimage is None:
            return None
        return (
            self._sheet_src_qimage,
            self._sheet_src_size,
            self._sheet_pix_size,
        )

    def _apply_transparent_color(self, rgb: Tuple[int, int, int]) -> None:
        """Make ``rgb`` the group's transparent (slot 0) colour.

        Two-part edit, staged atomically:

        1. NCLR slot 0 ← ``rgb`` so PNG exports and external NCLR-aware
           tools display the intended chroma.
        2. If ``rgb`` already lives at some non-zero slot K, every NCGR
           pixel pointing at K is remapped to 0 — without this the engine
           still renders those pixels opaquely because transparency is
           driven by *index* (== 0), not by the colour at slot 0.

        No-op when slot 0 already matches AND no remap candidate exists.
        """
        if self._current_decoded is None or self._current_group is None:
            return
        d = self._current_decoded
        palette = list(d.palette)
        target_snap = self._snap5(rgb)

        source_idx: Optional[int] = None
        for si in range(1, len(palette)):
            if self._snap5(palette[si]) == target_snap:
                source_idx = si
                break

        if self._snap5(palette[0]) == target_snap and source_idx is None:
            return

        group = self._current_group
        new_palette = list(palette)
        new_palette[0] = rgb
        nclr_raw = sprite.decompress_rle30(
            self._pak.entries[self._nclr_entry_idx(group)]
        )
        try:
            new_nclr = sprite.build_nclr_from_template(
                nclr_raw, {0: new_palette},
            )
        except ValueError as exc:
            QMessageBox.critical(self, "Build failed", f"NCLR rewrite: {exc}")
            return

        replacements = [
            (BTCHR_PAK, self._nclr_entry_idx(group), sprite.compress_rle30(new_nclr)),
        ]

        if source_idx is not None:
            ncgr_raw = sprite.decompress_rle30(
                self._pak.entries[self._ncgr_entry_idx(group)]
            )
            try:
                tile_bytes, *_ = sprite.parse_ncgr(ncgr_raw)
            except ValueError as exc:
                QMessageBox.critical(self, "Build failed", f"NCGR parse: {exc}")
                return
            k = source_idx & 0xFF
            new_tiles = bytes(0 if byte == k else byte for byte in tile_bytes)
            try:
                new_ncgr = sprite.build_ncgr_from_template(new_tiles, ncgr_raw)
            except ValueError as exc:
                QMessageBox.critical(self, "Build failed", f"NCGR rewrite: {exc}")
                return
            replacements.append((
                BTCHR_PAK,
                self._ncgr_entry_idx(group),
                sprite.compress_rle30(new_ncgr),
            ))

        cmd = ReplaceSpriteCommand(
            self._session,
            replacements,
            description=f"Set transparent color for BTCHR 0x{group:04x}",
            on_change=self._refresh_after_pak_change,
        )
        if self._undo_stack is not None:
            self._undo_stack.push(cmd)
        else:
            cmd.redo()

    # ---- animation playback / step editing -----------------------------

    def _current_track(self) -> List[btchr.AnimStep]:
        """Return the AnimStep list for the active track on the live
        digimon (empty list if no decode)."""
        if self._current_decoded is None:
            return []
        h = self._current_decoded.header
        return {
            "idle": h.idle, "attack": h.attack, "defend": h.defend,
        }.get(self._anim_track_key, [])

    def _refresh_anim_table(self) -> None:
        """Repopulate the steps table from the live track.

        Track A's first row is the implicit cell-0 step — its cell
        cell is shown but disabled (the engine reads cell 0 here
        regardless of what the bytes say). Both columns are otherwise
        editable u16s; the upper bound on Cell is clipped to
        n_cells - 1 in :meth:`_on_anim_step_edited` so the engine
        doesn't read past the bank.
        """
        self._anim_table_loading = True
        try:
            steps = self._current_track()
            self._anim_table.setRowCount(len(steps))
            is_idle = self._anim_track_key == "idle"
            for r, step in enumerate(steps):
                cell_item = QTableWidgetItem(str(step.cell))
                dur_item = QTableWidgetItem(str(step.duration))
                if is_idle and r == 0:
                    # Idle[0]'s cell is implicit 0 — engine ignores the
                    # stored value, so we disable editing instead of
                    # silently dropping the user's edits.
                    cell_item.setFlags(cell_item.flags() & ~Qt.ItemIsEditable)
                    cell_item.setToolTip(
                        "First Idle step is implicit cell 0 — "
                        "duration is editable, cell is not."
                    )
                self._anim_table.setItem(r, 0, cell_item)
                self._anim_table.setItem(r, 1, dur_item)
        finally:
            self._anim_table_loading = False
        self._recompute_anim_flat()

    def _recompute_anim_flat(self) -> None:
        """Expand the current track into per-tick cell sequence. Resets
        ``_anim_pos`` if it now points past the new track length so
        playback keeps a valid index after duration shrinks."""
        steps = self._current_track()
        self._anim_flat = btchr.flatten_anim_track(steps)
        if not self._anim_flat:
            self._anim_pos = 0
            return
        if self._anim_pos >= len(self._anim_flat):
            self._anim_pos = 0

    def _on_anim_track_changed(self, _idx: int) -> None:
        self._anim_track_key = self._anim_track_combo.currentData() or "idle"
        # Restart playback at frame 0 of the new track so the user
        # sees the new sequence from its start.
        self._anim_pos = 0
        self._refresh_anim_table()
        if self._anim_timer.isActive() and self._anim_flat:
            cell_idx, _ = self._anim_flat[0]
            self._current_cell = cell_idx
            self._refresh_preview()

    def _on_anim_fps_changed(self, fps: int) -> None:
        self._anim_fps = fps
        self._anim_timer.setInterval(max(1, 1000 // fps))

    def _on_anim_play_toggled(self, checked: bool) -> None:
        if checked:
            if not self._anim_flat:
                self._recompute_anim_flat()
            if not self._anim_flat or self._current_decoded is None:
                self._anim_play_btn.setChecked(False)
                return
            self._anim_play_btn.setText("■ Stop")
            self._cell_spin.setEnabled(False)
            self._show_all_cells.setEnabled(False)
            self._anim_pos = 0
            cell_idx, _ = self._anim_flat[0]
            self._current_cell = cell_idx
            self._refresh_preview()
            self._anim_timer.start()
        else:
            self._stop_anim_playback()

    def _stop_anim_playback(self) -> None:
        """Halt the timer and restore manual cell-selection state."""
        if self._anim_timer.isActive():
            self._anim_timer.stop()
        self._anim_play_btn.blockSignals(True)
        self._anim_play_btn.setChecked(False)
        self._anim_play_btn.setText("▶ Play")
        self._anim_play_btn.blockSignals(False)
        self._cell_spin.setEnabled(not self._show_all_cells.isChecked())
        self._show_all_cells.setEnabled(True)

    def _on_anim_tick(self) -> None:
        if not self._anim_flat or self._current_decoded is None:
            return
        self._anim_pos = (self._anim_pos + 1) % len(self._anim_flat)
        cell_idx, _ = self._anim_flat[self._anim_pos]
        n_cells = len(self._current_decoded.ncer.cells)
        if not (0 <= cell_idx < n_cells):
            return
        self._current_cell = cell_idx
        # Drive the spinner too so it reflects the playing position
        # without re-firing _on_cell_changed (signals blocked).
        self._cell_spin.blockSignals(True)
        self._cell_spin.setValue(cell_idx)
        self._cell_spin.blockSignals(False)
        self._refresh_preview()

    def _on_anim_step_edited(self, item: QTableWidgetItem) -> None:
        """Validate the edit, rebuild the mini-header, and push it as a
        sprite replace command. Invalid input (non-int / out of range)
        is reverted in-place from the live model so the table can never
        carry stale state."""
        if self._anim_table_loading:
            return
        if self._current_decoded is None or self._current_group is None:
            return
        row = item.row()
        col = item.column()
        steps = self._current_track()
        if not (0 <= row < len(steps)):
            return
        try:
            value = int(item.text())
        except ValueError:
            # Reload the canonical value from the model — silent revert
            # rather than dialog spam while the user is typing.
            self._refresh_anim_table()
            return
        if value < 0:
            self._refresh_anim_table()
            return
        n_cells = len(self._current_decoded.ncer.cells)
        if col == 0:
            # Cell index. Cap to n_cells-1; idle[0] should never reach
            # here because we disabled the item, but guard anyway.
            if self._anim_track_key == "idle" and row == 0:
                return
            if value >= n_cells:
                value = n_cells - 1
            if value == steps[row].cell:
                return
            new_step = btchr.AnimStep(cell=value, duration=steps[row].duration)
        else:
            # Duration. Clamp to u16 range.
            if value > 0xFFFF:
                value = 0xFFFF
            if value == steps[row].duration:
                return
            new_step = btchr.AnimStep(cell=steps[row].cell, duration=value)
        new_header = self._build_header_with_step_edit(row, new_step)
        self._push_header_replacement(
            new_header,
            description=(
                f"Edit BTCHR 0x{self._current_group:04x} "
                f"{self._anim_track_key}[{row}]"
            ),
        )

    def _build_header_with_step_edit(
        self, row: int, new_step: btchr.AnimStep,
    ) -> btchr.MiniHeader:
        """Clone the live mini-header with ``row`` of the active track
        replaced by ``new_step``. Other tracks pass through unchanged."""
        h = self._current_decoded.header
        tracks = {
            "idle": list(h.idle),
            "attack": list(h.attack),
            "defend": list(h.defend),
        }
        tracks[self._anim_track_key][row] = new_step
        return btchr.MiniHeader(
            footprint_scale=h.footprint_scale,
            flag=h.flag,
            pad_04=h.pad_04,
            y_pivot_a=h.y_pivot_a,
            x_pivot=h.x_pivot,
            y_pivot_b=h.y_pivot_b,
            pad_0c=h.pad_0c,
            idle=tracks["idle"],
            attack=tracks["attack"],
            defend=tracks["defend"],
            raw_size=h.raw_size,
        )

    def _push_header_replacement(
        self, new_header: btchr.MiniHeader, description: str,
    ) -> None:
        """Re-encode entry-0 from ``new_header`` and push as a
        ReplaceSpriteCommand. ``_refresh_after_pak_change`` redecodes
        and repopulates the table so undo/redo land cleanly."""
        group = self._current_group
        try:
            raw = btchr.serialize_mini_header(new_header)
        except (ValueError, struct.error) as exc:
            QMessageBox.critical(self, "Build failed", f"mini-header: {exc}")
            self._refresh_anim_table()
            return
        compressed = sprite.compress_rle30(raw)
        base = group * btchr.GROUP_SIZE
        cmd = ReplaceSpriteCommand(
            self._session,
            [(BTCHR_PAK, base, compressed)],
            description=description,
            on_change=self._refresh_after_pak_change,
        )
        if self._undo_stack is not None:
            self._undo_stack.push(cmd)
        else:
            cmd.redo()

    def _load_header_spinboxes(self, h: btchr.MiniHeader) -> None:
        """Programmatic refresh of the 3 editable header spinboxes. The
        loading guard prevents the valueChanged signals from re-firing
        _on_header_field_changed → re-encode loop during selection or
        post-undo refresh."""
        self._hdr_loading = True
        try:
            self._hdr_y_pivot_a_spin.setValue(h.y_pivot_a)
            self._hdr_x_pivot_spin.setValue(h.x_pivot)
            self._hdr_y_pivot_b_spin.setValue(h.y_pivot_b)
        finally:
            self._hdr_loading = False

    def _on_header_field_changed(self, attr: str, value: int) -> None:
        """One handler for the three editable header spinboxes — clones
        the live MiniHeader with ``attr`` swapped and pushes a
        replacement.
        Skipped when the spinbox value matches the live header (e.g.
        loading guard let one through, or undo restored the same value)."""
        if self._hdr_loading:
            return
        if self._current_decoded is None or self._current_group is None:
            return
        h = self._current_decoded.header
        if getattr(h, attr) == value:
            return
        new_header = btchr.MiniHeader(
            footprint_scale=h.footprint_scale,
            flag=h.flag,
            pad_04=h.pad_04,
            y_pivot_a=h.y_pivot_a,
            x_pivot=h.x_pivot,
            y_pivot_b=h.y_pivot_b,
            pad_0c=h.pad_0c,
            idle=list(h.idle),
            attack=list(h.attack),
            defend=list(h.defend),
            raw_size=h.raw_size,
        )
        setattr(new_header, attr, value)
        self._push_header_replacement(
            new_header,
            description=f"Edit BTCHR 0x{self._current_group:04x} {attr}",
        )

    def _build_header_with_track(
        self, new_track: List[btchr.AnimStep],
    ) -> btchr.MiniHeader:
        """Clone the live mini-header with the active track replaced
        wholesale by ``new_track``. Used by add/remove which change the
        track's length, unlike _build_header_with_step_edit which only
        substitutes one step in place."""
        h = self._current_decoded.header
        tracks = {
            "idle": list(h.idle),
            "attack": list(h.attack),
            "defend": list(h.defend),
        }
        tracks[self._anim_track_key] = list(new_track)
        return btchr.MiniHeader(
            footprint_scale=h.footprint_scale,
            flag=h.flag,
            pad_04=h.pad_04,
            y_pivot_a=h.y_pivot_a,
            x_pivot=h.x_pivot,
            y_pivot_b=h.y_pivot_b,
            pad_0c=h.pad_0c,
            idle=tracks["idle"],
            attack=tracks["attack"],
            defend=tracks["defend"],
            raw_size=h.raw_size,
        )

    def _on_anim_add_step(self) -> None:
        """Append a step to the active track. New step defaults to
        (cell=last step's cell, duration=8) — copying the last cell
        keeps the sequence visually continuous so the user sees the
        new step land at the end without flicker."""
        if self._current_decoded is None or self._current_group is None:
            return
        steps = list(self._current_track())
        last_cell = steps[-1].cell if steps else 0
        new_track = steps + [btchr.AnimStep(cell=last_cell, duration=8)]
        new_header = self._build_header_with_track(new_track)
        self._push_header_replacement(
            new_header,
            description=(
                f"Add BTCHR 0x{self._current_group:04x} "
                f"{self._anim_track_key} step"
            ),
        )

    def _on_anim_remove_step(self) -> None:
        """Remove the selected step from the active track. Refuses to
        remove Idle[0] (engine anchor) or to shrink a track to zero
        steps (parser invariant: every track has ≥1 step)."""
        if self._current_decoded is None or self._current_group is None:
            return
        row = self._anim_table.currentRow()
        if row < 0:
            return
        steps = list(self._current_track())
        if not (0 <= row < len(steps)):
            return
        if self._anim_track_key == "idle" and row == 0:
            return
        if len(steps) <= 1:
            return
        new_track = steps[:row] + steps[row + 1:]
        new_header = self._build_header_with_track(new_track)
        self._push_header_replacement(
            new_header,
            description=(
                f"Remove BTCHR 0x{self._current_group:04x} "
                f"{self._anim_track_key}[{row}]"
            ),
        )

    def _update_anim_remove_enabled(self) -> None:
        """Remove is enabled only when the selected row is removable:
        a row is selected, it isn't Idle[0], and the track wouldn't
        drop below one step."""
        row = self._anim_table.currentRow()
        if row < 0:
            self._anim_remove_btn.setEnabled(False)
            return
        steps = self._current_track()
        if len(steps) <= 1:
            self._anim_remove_btn.setEnabled(False)
            return
        if self._anim_track_key == "idle" and row == 0:
            self._anim_remove_btn.setEnabled(False)
            return
        self._anim_remove_btn.setEnabled(True)

    def _refresh_after_pak_change(self) -> None:
        """Re-decode current digimon + invalidate pixmap cache after a
        pak entry was replaced (palette or NCGR). Preserves cell selection
        and the show-all toggle so the user sees the edit land in-place.
        """
        if self._current_group is None:
            return
        g = self._current_group
        try:
            digimon_id = (
                self._chrsize_rows[g][0]
                if g < len(self._chrsize_rows) else -1
            )
            self._current_decoded = btchr.decode_digimon(
                self._pak, g, digimon_id=digimon_id,
            )
        except (ValueError, IndexError) as exc:
            self._current_decoded = None
            self._preview.setText(f"(decode error: {exc})")
            return
        self._cell_pixmaps = []
        self._sheet_dirty = True
        self._oam_dirty = True
        self._refresh_preview()
        if self._tabs.currentIndex() == 1:
            self._refresh_sheet_preview()
            self._sheet_dirty = False
        self._refresh_oam_tab_if_active()
        if getattr(self, "_move_frame_cb", None) and self._move_frame_cb.isChecked():
            self._rebuild_align_data()
        self._sync_palette_grid()
        self._picker.set_current_color(self._current_decoded.palette[0])
        # Header may have just been rewritten by an animation edit;
        # repopulate the table + flattened track from the fresh decode.
        h = self._current_decoded.header
        self._load_header_spinboxes(h)
        self._refresh_anim_table()
        # Cell count is stable across an in-place edit, so reload values
        # into the existing rows rather than rebuilding the grid (avoids
        # tearing down the spinbox the user just interacted with).
        if len(self._frame_off_spins) == len(self._current_decoded.ncer.cells):
            self._load_frame_offsets()
        else:
            self._rebuild_frame_offset_rows()
            self._load_frame_offsets()

    def _build_all_cells_strip(self) -> Optional[QPixmap]:
        d = self._current_decoded
        if d is None or not d.ncer.cells:
            return None
        cell_pms = [self._cell_pixmap(i) for i in range(len(d.ncer.cells))]
        cell_pms = [pm for pm in cell_pms if pm is not None and not pm.isNull()]
        if not cell_pms:
            return None
        gutter = 4
        max_h = max(pm.height() for pm in cell_pms)
        total_w = sum(pm.width() for pm in cell_pms) + gutter * (len(cell_pms) - 1)
        strip = QImage(total_w, max_h, QImage.Format_RGBA8888)
        strip.fill(0)
        painter = QPainter(strip)
        x = 0
        for pm in cell_pms:
            y = (max_h - pm.height()) // 2
            painter.drawPixmap(x, y, pm)
            x += pm.width() + gutter
        painter.end()
        return QPixmap.fromImage(strip)


def _cell_origin(cell) -> Tuple[int, int]:
    """Top-left of a cell's OAM union — the single (x, y) that the whole
    frame's position collapses to. ``(0, 0)`` for an empty cell."""
    if not cell.oams:
        return (0, 0)
    return (min(o.x for o in cell.oams), min(o.y for o in cell.oams))


def _format_track(track) -> str:
    """One-line readable summary of an animation track."""
    if not track:
        return "—"
    total = sum(s.duration for s in track)
    seq = " → ".join(f"c{s.cell}×{s.duration}" for s in track)
    return f"{total}f: {seq}"
