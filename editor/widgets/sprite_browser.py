"""Read-only sprite browser (PLAN.md §11 Phase B).

Walks the ``DAT/SPR_CHR.PAK`` / ``DAT/SPR_PAL.PAK`` / ``DAT/SPR_CEL.PAK``
trio paired by index (project memory
``project_sprite_pak_pair_heuristic``: index N is the same logical
sprite across the three SPR_* directories) and renders each entry's
NCGR using the matching NCLR. Export / replace are the next phases —
this widget is the foundation they'll hang off.

Layout:
* left: filterable list of 1627 entries (``"0006"`` style — sprite paks
  are unnamed in vanilla)
* right: preview pane + metadata block + width-tiles spin so users can
  reflow the linear NCGR data when the RAHC width hint is missing
  (sprite sheets store ``0xFFFF`` more often than not — the engine relies
  on NCER OAMs to lay tiles out, so the raw NCGR is just a tile bag).
"""
from __future__ import annotations

import math
import os
import re
from typing import List, Optional, Tuple

from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtGui import (
    QBrush,
    QColor,
    QImage,
    QPainter,
    QPen,
    QPixmap,
    QTransform,
    QUndoStack,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
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

from digimon_core import nanr, ncer as ncer_mod, pak, sprite

from ..commands import AppendPakEntriesCommand, ReplaceSpriteCommand
from ._png_palette import build_palette_from_png, intensity_matched_palette
from .palette_batch_adjuster import PaletteBatchAdjuster
from .palette_editor import PaletteEditor
from .palette_grid import PaletteGrid
from .palette_toolkit import DEFAULT_SCROLL_MAX_H, PaletteToolkit
from .cell_export_policy import (
    allow_shared_tile_exports,
    register_change_callback as _register_cell_policy_callback,
)
from .form_helpers import wrap_tooltip
from .cell_png_io import (
    CellPngContext,
    CellPngError,
    OamTileConflicts,
    build_palette_for_per_cell_import,
    cell_layout as shared_cell_layout,
    detect_oam_tile_conflicts,
    import_cells_to_tiles,
    import_per_cell_to_tiles,
    render_cells_qimage as shared_render_cells_qimage,
    render_one_cell_qimage as shared_render_one_cell_qimage,
)
from .record_list_panel import RecordListPanel


SPR_CHR = "DAT/SPR_CHR.PAK"
SPR_PAL = "DAT/SPR_PAL.PAK"
SPR_CEL = "DAT/SPR_CEL.PAK"
SPR_ANM = "DAT/SPR_ANM.PAK"

# Every SPR_* pak is a strict parallel array of the same length — appending
# an entry to one without the others would desynchronize the trio that the
# preview pipeline relies on (CHR/PAL/CEL by-index pairing) plus the ANM
# table the engine reads in lockstep. "Duplicate entry" therefore clones
# the source index from all four paks at once.
SPR_PARALLEL_PAKS = (SPR_CHR, SPR_PAL, SPR_CEL, SPR_ANM)


def _oam_gap_tile_set(
    ncer_obj: Optional["ncer_mod.Ncer"], n_tiles: int, *, bpp4: bool,
) -> set:
    """Return the set of tile indices in the NCGR that no OAM references.

    Same "coverage gap" idea as BTCHR's per-pixel mask, one level up:
    here the unit is a whole tile, since the sprite_browser preview is
    a raw-tile grid rather than an OAM composite. An empty set means
    every tile is drawn by some cell; a non-empty set is exactly the
    tiles that any PNG-import edit would silently discard.
    """
    if ncer_obj is None or not ncer_obj.cells or n_tiles <= 0:
        return set()
    referenced = set()
    for cell_ranges in ncer_mod.cell_tile_ranges(ncer_obj, bpp4=bpp4):
        for tile_start, tile_end in cell_ranges:
            referenced.update(range(tile_start, min(tile_end, n_tiles)))
    return set(range(n_tiles)) - referenced


def _format_spr_label(prefix: str, role_token: str, metadata: str) -> str:
    """Compose a SPR_* list label.

    Format with role: ``"{prefix}  [ICON] {name} - {metadata}"`` (or BTMINI,
    or both joined). Format without role: ``"{prefix}  {metadata}"``.
    """
    if role_token:
        return f"{prefix}  {role_token} - {metadata}"
    return f"{prefix}  {metadata}"


def compute_spr_labels(session) -> List[str]:
    """Public helper: full SPR_* label list (one per CHR entry).

    Used by the enemy-digimon editor's picker comboboxes so portrait /
    battle-mini fields surface the same `[ICON]` / `[BTMINI]` annotations
    the browser shows. Equivalent to instantiating the browser and reading
    ``_labels`` but without building a widget.
    """
    chr_pak = session.sprite_pak(SPR_CHR)
    cel_pak = session.sprite_pak(SPR_CEL)
    count = min(chr_pak.count, cel_pak.count, session.sprite_pak(SPR_PAL).count)
    # Frozen at session load — see RomSession.sprite_attribution. Means
    # reassigning a digimon's portrait / battle-preview sprite later
    # doesn't relabel the original sprite.
    attribution = session.sprite_attribution()
    portrait_to_base = attribution["upperscreen_low"]
    preview_to_base = attribution["upperscreen_high"]

    out: List[str] = []
    for ix in range(count):
        prefix = f"0x{ix:04x}"
        parts: List[str] = []
        p_base = portrait_to_base.get(ix)
        if p_base is not None:
            # NPC portraits (e.g. icon 0x600 ↔ slot 0x30e for Glare)
            # resolve through the battle-string fallback the same way
            # digimon portraits do — single lookup for both cases.
            parts.append(f"[ICON] {session.digimon_display_name(p_base)}")
        b_base = preview_to_base.get(ix)
        if b_base is not None:
            parts.append(f"[BTMINI] {session.digimon_display_name(b_base)}")
        role = " ".join(parts)
        try:
            tile_bytes, bit_depth, *_ = sprite.parse_ncgr(chr_pak.entries[ix])
            parsed_ncer = ncer_mod.parse_ncer(cel_pak.entries[ix])
        except (ValueError, IndexError):
            out.append(_format_spr_label(prefix, role, "(parse error)"))
            continue
        bytes_per_tile = 32 if bit_depth == 3 else 64
        n_tiles = len(tile_bytes) // bytes_per_tile
        n_cells = len(parsed_ncer.cells)
        if n_cells == 0 or n_tiles == 0:
            out.append(_format_spr_label(prefix, role, "(empty)"))
            continue
        w, h = ncer_mod.sprite_bbox(parsed_ncer)
        bpp_token = "4bpp" if bit_depth == 3 else "8bpp"
        size_token = f"{w}×{h}" if (w and h) else "0×0"
        out.append(_format_spr_label(prefix, role, f"{bpp_token} {size_token} {n_cells}c"))
    return out


class SpriteBrowser(QWidget):
    """Viewer + dual-format import/export for the SPR_* sprite trio."""

    _CURSOR_KEY = "sprite_browser"

    def __init__(self, session, undo_stack: Optional[QUndoStack] = None, parent=None):
        super().__init__(parent)
        self._session = session
        self._undo_stack = undo_stack
        self._chr_pak: pak.PakFile = session.sprite_pak(SPR_CHR)
        self._pal_pak: pak.PakFile = session.sprite_pak(SPR_PAL)
        self._cel_pak: pak.PakFile = session.sprite_pak(SPR_CEL)
        # SPR_ANM is the fourth parallel pak (one NANR per sprite). Only
        # read for the animation viewer — not part of the CHR/PAL/CEL
        # preview pipeline, so it stays out of the _count clamp below.
        self._anm_pak: pak.PakFile = session.sprite_pak(SPR_ANM)
        # Sanity: pair heuristic requires equal counts. Don't crash if the
        # ROM is unusual — just clamp browsing to the smallest.
        self._count = min(self._chr_pak.count, self._pal_pak.count, self._cel_pak.count)

        self._current_idx: Optional[int] = None
        self._current_palette_bank: int = 0
        self._current_width_tiles: int = 4
        # entry -> per-slot pixel histogram (for dominant-colour borrow match)
        self._src_counts_cache: dict[int, Optional[List[int]]] = {}
        # Per-sprite width overrides. Lives in memory only — switching
        # away from a sprite and back uses the user's last width pick
        # instead of resnapping to the heuristic. Cleared on re-load.
        self._width_overrides: dict[int, int] = {}
        # Pick-from-image mode state. _preview_src_size is the unscaled
        # render dimensions so we can map a label-coordinate click back to
        # the source pixel (the QLabel may render at 1x or 2x scale).
        self._picking_transparent: bool = False
        self._preview_src_size: Tuple[int, int] = (0, 0)
        self._preview_pixmap_size: Tuple[int, int] = (0, 0)
        # OAM-gap overlay hidden from the UI: SPR_CHR data is tightly
        # packed (0/1627 vanilla entries have gap tiles), so the toggle
        # and meta row are dead weight for icons. The paint routine +
        # gap-set helper stay in place so the feature can be surfaced
        # again if a future workflow (e.g. tile-bank extension via PNG
        # import) starts producing gaps.
        self._show_oam_overlay: bool = False
        # Cells tab state: lazy-render when the tab becomes visible so
        # switching sprites while the tab is hidden doesn't pay for the
        # OAM composite cost. Per-entry column overrides persist for the
        # session — same UX as BTCHR's cells tab.
        self._cells_dirty: bool = True
        self._cells_columns_overrides: dict[int, int] = {}
        # Latest shared-tile detection for the selected sprite. Drives
        # both the footer warning text and the gating of the four cell-
        # mode export/import buttons. ``None`` before any selection.
        self._current_conflicts: Optional[OamTileConflicts] = None

        # Animation playback state (SPR_ANM / NANR). ``_anim_flat`` is one
        # cell index per output tick (see :func:`nanr.flatten_sequence`);
        # the timer drives ``_anim_pos`` forward and each tick paints that
        # cell into the Cells tab preview. Default FPS = 60 to match the
        # NDS vblank the NANR frame durations are counted in — same basis
        # as the BTCHR animation viewer.
        self._anim: Optional[nanr.Nanr] = None
        self._anim_seq_idx: int = 0
        self._anim_flat: List[nanr.NanrFrame] = []
        self._anim_pos: int = 0
        self._anim_fps: int = 60
        # Shared canvas for transformed playback: (ox, oy, w, h) fitting
        # every frame's SRT-transformed bounds so the sprite scales / rotates
        # / slides without the preview jumping. ``None`` for index (no
        # transform) sequences. ``_anim_editing_frame`` is the frame row the
        # transform editor targets; ``_anim_editor_loading`` guards the
        # spinbox valueChanged signals during programmatic refresh.
        self._anim_canvas: Optional[Tuple[int, int, int, int]] = None
        self._anim_editing_frame: int = -1
        self._anim_editor_loading: bool = False
        # Guards the frame table's itemChanged handler against programmatic
        # (re)population re-triggering a cell/duration re-encode.
        self._anim_table_loading: bool = False

        # Reverse lookups: SPR index -> first sprite-map slot that points at
        # it. SpriteMapEntry.upperscreen_low is the portrait sprite, and
        # upperscreen_high is the battle-preview mini — both index SPR_*.
        # The slot's list position is the digimon id, so this is what turns
        # "entry 0x0427" into "Agumon (portrait)". Recolors share sprites;
        # setdefault keeps the first (canonical) name.
        self._portrait_to_base: dict[int, int] = {}
        self._preview_to_base: dict[int, int] = {}
        for base_id, entry in enumerate(getattr(session, "sprite_map", [])):
            self._portrait_to_base.setdefault(entry.upperscreen_low, base_id)
            self._preview_to_base.setdefault(entry.upperscreen_high, base_id)

        # Precompute structural labels — one parse_ncgr + parse_ncer per
        # entry. Done eagerly so the filter box can match on bpp / size /
        # cell-count tokens from the moment the browser opens.
        self._labels: List[str] = self._build_index_labels()

        # Non-None while the batch adjuster drags: a preview palette override
        # the render paths consult. `_picking_slot` is the eyedropper mode
        # (mutually exclusive with the transparent-colour pick).
        self._preview_palette: Optional[List[Tuple[int, int, int]]] = None
        self._picking_slot: bool = False

        self._build_ui()
        remembered = self._session.recall_selection(self._CURSOR_KEY)
        if remembered is None or not self._list.select_index(int(remembered)):
            self._list.select_first()

    # ---- preview width heuristic ----------------------------------------

    @staticmethod
    def _default_width_tiles_for_bbox(bbox_w: int) -> int:
        """Pick a preview width (in tiles) from the cell bbox pixel width.

        Empirical rule the user observed across most DWDD sprites with no
        RAHC width hint: ≤16px → 2 tiles, 17–63px → 4 tiles, ≥64px → 8
        tiles. Not authoritative — the user can still adjust the spin
        for outliers; this just picks a better default than always 4.
        """
        if bbox_w <= 0:
            return 4
        if bbox_w <= 16:
            return 2
        if bbox_w < 64:
            return 4
        return 8

    # ---- structural categorisation --------------------------------------

    def _build_index_labels(self) -> List[str]:
        """Decorated list labels per index: ``"0006  4bpp 64×64 1c"`` etc.

        Filter box searches the label as a substring, so tokens are kept
        space-separated and lowercase-stable: typing ``4bpp`` filters by
        depth, ``64×64`` by size, ``1c`` by single-cell sprites.
        """
        out: List[str] = []
        for ix in range(self._count):
            out.append(self._compute_index_label(ix))
        return out

    def _compute_index_label(self, ix: int) -> str:
        prefix = f"0x{ix:04x}"
        role_token = self._role_tag(ix)
        try:
            tile_bytes, bit_depth, *_ = sprite.parse_ncgr(self._chr_pak.entries[ix])
            parsed_ncer = ncer_mod.parse_ncer(self._cel_pak.entries[ix])
        except (ValueError, IndexError):
            return _format_spr_label(prefix, role_token, "(parse error)")
        bytes_per_tile = 32 if bit_depth == 3 else 64
        n_tiles = len(tile_bytes) // bytes_per_tile
        n_cells = len(parsed_ncer.cells)
        if n_cells == 0 or n_tiles == 0:
            return _format_spr_label(prefix, role_token, "(empty)")
        w, h = ncer_mod.sprite_bbox(parsed_ncer)
        bpp_token = "4bpp" if bit_depth == 3 else "8bpp"
        size_token = f"{w}×{h}" if (w and h) else "0×0"
        meta = f"{bpp_token} {size_token} {n_cells}c"
        return _format_spr_label(prefix, role_token, meta)

    def _role_tag(self, ix: int) -> str:
        """`[ICON] <name>` / `[BTMINI] <name>` for SPR indices referenced
        by sprite_map (joined when one index plays both roles). Empty
        string for unreferenced UI / utility sprites.

        `digimon_display_name` resolves NPC slots (0x30e..0x363) via the
        battle-string fallback, so NPC portraits (e.g. SPR 0x600 ↔ slot
        0x30e for Glare) surface their name instead of the raw slot id.
        """
        parts: List[str] = []
        portrait_base = self._portrait_to_base.get(ix)
        if portrait_base is not None:
            parts.append(f"[ICON] {self._session.digimon_display_name(portrait_base)}")
        preview_base = self._preview_to_base.get(ix)
        if preview_base is not None:
            parts.append(f"[BTMINI] {self._session.digimon_display_name(preview_base)}")
        return " ".join(parts)

    # ---- UI construction -------------------------------------------------

    def _build_ui(self) -> None:
        # Index list — pass [0..count-1]; the label_for only uses the index
        # and looks up the precomputed structural label so opening the panel
        # doesn't pay for 1627 NCER parses on every selection change.
        self._list = RecordListPanel(
            records=list(range(self._count)),
            label_for=lambda ix, _rec: self._labels[ix],
        )
        self._list.indexSelected.connect(self._on_index_selected)

        # Right side: preview + metadata + controls.
        self._image_label = QLabel("Select a sprite to preview.")
        self._image_label.setAlignment(Qt.AlignCenter)
        self._image_label.setMinimumSize(256, 256)
        # Event filter routes preview clicks to the pick-transparent handler
        # without subclassing QLabel — simpler and keeps the widget tree flat.
        self._image_label.installEventFilter(self)

        self._scroll = QScrollArea()
        self._scroll.setWidget(self._image_label)
        self._scroll.setWidgetResizable(True)
        self._scroll.setAlignment(Qt.AlignCenter)

        # Cells tab: OAM-composed preview, one slot per cell on a
        # configurable column grid. Pure preview — no interaction with
        # the tile-mode picker/overlay state. The columns spinner is
        # tiny and lives above the preview so the pane stays self-
        # contained even at narrow widths.
        self._cells_label = QLabel("Select a sprite to preview.")
        self._cells_label.setAlignment(Qt.AlignCenter)
        self._cells_label.setMinimumSize(256, 256)
        self._cells_scroll = QScrollArea()
        self._cells_scroll.setWidget(self._cells_label)
        self._cells_scroll.setWidgetResizable(True)
        self._cells_scroll.setAlignment(Qt.AlignCenter)
        self._cells_columns_spin = QSpinBox()
        self._cells_columns_spin.setRange(1, 32)
        self._cells_columns_spin.setValue(4)
        self._cells_columns_spin.setMaximumWidth(80)
        self._cells_columns_spin.valueChanged.connect(self._on_cells_columns_changed)
        # Cell selector: picks the cell shown in single-cell view and the
        # one "Add cell" clones. Lives in the general controls (always
        # visible) so it's usable even from the Tiles tab.
        self._cell_view_spin = QSpinBox()
        self._cell_view_spin.setRange(0, 0)
        self._cell_view_spin.valueChanged.connect(self._on_cell_view_changed)
        # "Show all cells" grid (default) vs. a single selected cell.
        self._cells_show_all_cb = QCheckBox("Show all cells")
        self._cells_show_all_cb.setChecked(True)
        self._cells_show_all_cb.toggled.connect(self._on_cells_show_all_toggled)
        # Tile-conflict heads-up: shows when the sprite has OAMs that
        # share source tiles. The cells composite still renders fine
        # (preview is read-only), but the import path would last-write-
        # wins those shared bytes so any pixel edit leaks across OAMs.
        # Empty when the sprite is conflict-free — the toolbar stays
        # quiet for the common single-OAM portrait case.
        self._cells_conflict_label = QLabel("")
        self._cells_conflict_label.setStyleSheet(
            "color: #c47b00; font-weight: bold;"
        )
        self._cells_conflict_label.setToolTip(
            "Some OAMs in this sprite reference the same source tiles. "
            "The Cells preview renders correctly, but editing the PNG "
            "and re-importing will overwrite each shared tile from the "
            "last OAM that touches it — visible as cross-contamination "
            "between OAMs after re-import."
        )

        # Width-tiles spin: rendering controls only, doesn't mutate the sprite.
        self._width_spin = QSpinBox()
        self._width_spin.setRange(1, 64)
        self._width_spin.setValue(self._current_width_tiles)
        self._width_spin.valueChanged.connect(self._on_width_changed)

        self._palette_combo = QComboBox()
        self._palette_combo.currentIndexChanged.connect(self._on_palette_changed)

        # Transparent-color row: palette index 0 of the displayed bank.
        # The engine treats index 0 as fully transparent regardless of its
        # RGB, so changing this here doesn't visually change the preview —
        # it changes the RGB that gets written to PNG exports / re-imports
        # and what external NCLR-aware tools see.
        self._transparent_hex = QLineEdit()
        self._transparent_hex.setMaxLength(7)  # "#RRGGBB"
        self._transparent_hex.setPlaceholderText("#RRGGBB")
        self._transparent_hex.setFixedWidth(80)
        self._transparent_hex.editingFinished.connect(
            self._on_transparent_hex_submitted
        )
        self._transparent_swatch = QLabel()
        self._transparent_swatch.setFixedSize(20, 20)
        self._transparent_swatch.setFrameStyle(0)
        self._transparent_swatch.setStyleSheet(
            "background-color: #000000; border: 1px solid #555;"
        )
        self._pick_transparent_btn = QPushButton("Pick from image")
        self._pick_transparent_btn.setCheckable(True)
        self._pick_transparent_btn.toggled.connect(self._on_pick_toggled)
        transparent_row = QHBoxLayout()
        transparent_row.setSpacing(6)
        transparent_row.addWidget(self._transparent_hex)
        transparent_row.addWidget(self._transparent_swatch)
        transparent_row.addWidget(self._pick_transparent_btn)
        transparent_row.addStretch(1)
        transparent_widget = QWidget()
        transparent_widget.setLayout(transparent_row)
        transparent_row.setContentsMargins(0, 0, 0, 0)

        # OAM coverage-gap toggle. Tiles in the raw-tile-grid preview
        # that no NCER OAM references are tinted red — content painted
        # into those tiles won't render in-game. Mirrors the BTCHR
        # cell-preview red overlay so both editors flag the same class
        # of "wasted paint" identically.
        self._oam_overlay_check = QCheckBox("Highlight OAM coverage gaps")
        self._oam_overlay_check.setChecked(True)
        self._oam_overlay_check.setToolTip(wrap_tooltip(
            "Some sprites leave tiles inside their NCGR that no NCER "
            "OAM references. Content painted there (via PNG import) is "
            "written into tile storage but never rendered in-game. When "
            "enabled, those tiles are tinted red on the tile-grid "
            "preview."
        ))
        self._oam_overlay_check.toggled.connect(self._on_oam_overlay_toggled)

        # Editable swatch grid for the displayed palette bank. Clicking a
        # swatch recolors that slot (write-back is 5-bit BGR555-quantized in
        # _apply_palette_color); slot 0 is the engine's transparent index.
        # Palette editor widgets — grid + inline single-slot editor + batch
        # H/S/L adjuster + eyedropper, all wired by PaletteToolkit (built in
        # _build_ui once the scroll exists). Click a swatch to edit one colour;
        # shift-click / drag a box to select several and shift them together.
        self._palette_grid = PaletteGrid()
        self._palette_grid.setToolTip(wrap_tooltip(
            "The current palette bank. Click a swatch to edit it, or shift-click "
            "a range / drag a box to select several and shift them together with "
            "the H/S/L sliders. Colors quantize to NDS 5-bit BGR555 on save. "
            "Slot 0 (diagonal mark) is transparent."
        ))
        self._palette_editor = PaletteEditor()
        self._palette_adjuster = PaletteBatchAdjuster()
        self._eyedropper_btn = QPushButton("Eyedropper")
        self._eyedropper_btn.setCheckable(True)
        self._eyedropper_btn.setToolTip(
            "Then click a pixel on the sprite preview to select its palette slot."
        )
        self._eyedropper_btn.toggled.connect(self._on_slot_pick_toggled)

        controls = QFormLayout()
        controls.addRow("Width (tiles)", self._width_spin)
        controls.addRow("Palette bank", self._palette_combo)
        controls.addRow("Cell", self._cell_view_spin)
        controls.addRow("Transparent color", transparent_widget)
        # The swatch grid lives in its own right-hand sidebar (built below),
        # not inline here — an 8bpp 256-color palette is 16×16 and would
        # dwarf the form.
        # controls.addRow("OAM gaps", self._oam_overlay_check)

        # Metadata panel — one row per field, read-only.
        self._meta_tiles = QLabel("—")
        self._meta_bpp = QLabel("—")
        self._meta_palettes = QLabel("—")
        self._meta_cells = QLabel("—")
        self._meta_mapping = QLabel("—")
        self._meta_min_tiles = QLabel("—")
        self._meta_oam_gaps = QLabel("—")
        self._meta_oam_gaps.setToolTip(wrap_tooltip(
            "Tiles in the NCGR that no OAM references. Content painted "
            "into these tiles won't render in-game — highlighted red on "
            "the preview when the OAM gaps toggle is on."
        ))
        self._meta_chr_size = QLabel("—")
        # Pin the value column to a worst-case width so the form (and the
        # whole right pane via the splitter) doesn't reflow each time a
        # number gains or loses a digit — switching sprites would
        # otherwise jiggle every widget that shares the row.
        fm = self._meta_chr_size.fontMetrics()
        worst_width = fm.horizontalAdvance("999999B compressed / 9999999B raw")
        for lbl in (
            self._meta_tiles, self._meta_bpp, self._meta_palettes,
            self._meta_cells, self._meta_mapping, self._meta_min_tiles,
            self._meta_oam_gaps, self._meta_chr_size,
        ):
            lbl.setMinimumWidth(worst_width)

        meta_form = QFormLayout()
        meta_form.addRow("NCGR tiles", self._meta_tiles)
        meta_form.addRow("Bit depth", self._meta_bpp)
        meta_form.addRow("Palettes", self._meta_palettes)
        meta_form.addRow("NCER cells", self._meta_cells)
        meta_form.addRow("OBJ mapping", self._meta_mapping)
        meta_form.addRow("Min tiles required", self._meta_min_tiles)
        # meta_form.addRow("OAM coverage gaps", self._meta_oam_gaps)
        meta_form.addRow("CHR entry bytes", self._meta_chr_size)

        # Export / replace actions: PNG for content editing (round-trips
        # with render_rgba / encode_tiles), NCGR+NCLR for lossless
        # engine-native round-trip (see PLAN.md §11.4.1).
        self._export_png_btn = QPushButton("Export tiles PNG…")
        self._export_png_btn.clicked.connect(self._on_export_png)
        self._export_native_btn = QPushButton("Export NCGR+NCLR…")
        self._export_native_btn.clicked.connect(self._on_export_native)
        # Single PNG import button; the checkbox below picks between the
        # two underlying paths.
        # - off → match the existing palette (CHR-only edit, posterizes
        #   off-palette colours).
        # - on  → run median-cut on the PNG and rebuild the displayed
        #   bank too (CHR + PAL edit; siblings sharing the bank pick up
        #   the new colours).
        self._replace_png_btn = QPushButton("Import tiles from PNG…")
        self._replace_png_btn.clicked.connect(self._on_replace_png_dispatch)
        self._duplicate_entry_btn = QPushButton("Duplicate sprite entry")
        self._duplicate_entry_btn.setToolTip(
            "Append a new SPR_* entry cloned from the currently-selected "
            "entry. Grows SPR_CHR/SPR_PAL/SPR_CEL/SPR_ANM in lockstep so the "
            "preview pipeline and engine animation table stay in sync. "
            "Engine extensibility past vanilla 1627 is unproven — test "
            "in-game before relying on it."
        )
        self._duplicate_entry_btn.clicked.connect(self._on_add_entry)
        self._import_pal_with_sheet_cb = QCheckBox("Also import palette from PNG")
        self._import_pal_with_sheet_cb.setChecked(True)
        self._import_pal_with_sheet_cb.setToolTip(
            "On: median-cut a fresh palette from the PNG's opaque pixels "
            "and rebuild the displayed bank (siblings sharing the bank "
            "pick up the new colours too).\n"
            "Off: index against the existing palette (off-palette "
            "colours posterize)."
        )
        self._replace_native_btn = QPushButton("Import from NCGR+NCLR…")
        self._replace_native_btn.clicked.connect(self._on_replace_native)
        # OAM-composed cell-mode IO: the PNG dimensions match the OAM
        # bbox grid instead of a tile sheet, so what the user sees in
        # their image editor is the sprite's actual cell layout. Same
        # pipeline BTCHR uses (see cell_png_io). The per-cell variant
        # writes one PNG per cell, all sized to the union bbox so cells
        # can be swapped without manual cropping.
        self._export_cells_png_btn = QPushButton("Export cells sheet PNG…")
        self._export_cells_png_btn.clicked.connect(self._on_export_cells_png)
        self._export_per_cell_btn = QPushButton("Export per-cell PNGs…")
        self._export_per_cell_btn.clicked.connect(self._on_export_per_cell_pngs)
        self._import_cells_png_btn = QPushButton("Import cells sheet PNG…")
        self._import_cells_png_btn.clicked.connect(self._on_import_cells_png)
        self._import_per_cell_btn = QPushButton("Import per-cell PNGs…")
        self._import_per_cell_btn.clicked.connect(self._on_import_per_cell_pngs)

        # "Export…" / "Import…" dropdowns front every PNG mode + the native
        # NCGR+NCLR pair, mirroring Battle Sprites. The individual buttons stay
        # in code (nothing deleted) — they just back the menu rows and aren't
        # laid out. The cell rows are kept as QActions so the shared-tile export
        # policy can gate them (see _refresh_cell_export_button_states).
        self._export_menu = QMenu(self)
        self._act_export_cells = self._export_menu.addAction(
            "Sprite sheet (all frames, one PNG)…", self._on_export_cells_png)
        self._act_export_per_cell = self._export_menu.addAction(
            "Individual frames (one PNG each)…", self._on_export_per_cell_pngs)
        self._export_menu.addSeparator()
        self._export_menu.addAction(
            "Tile sheet (raw 8×8 tiles)…", self._on_export_png)
        self._export_menu.addSeparator()
        self._export_menu.addAction(
            "Source files (NCGR + NCLR)…", self._on_export_native)
        self._export_as_btn = QPushButton("Export…")
        self._export_as_btn.setMenu(self._export_menu)

        self._import_menu = QMenu(self)
        self._act_import_cells = self._import_menu.addAction(
            "Sprite sheet (all frames, one PNG)…", self._on_import_cells_png)
        self._act_import_per_cell = self._import_menu.addAction(
            "Individual frames (one PNG each)…", self._on_import_per_cell_pngs)
        self._import_menu.addSeparator()
        self._import_menu.addAction(
            "Tile sheet (raw 8×8 tiles)…", self._on_replace_png_dispatch)
        self._import_menu.addSeparator()
        self._import_menu.addAction(
            "Source files (NCGR + NCLR)…", self._on_replace_native)
        self._import_as_btn = QPushButton("Import…")
        self._import_as_btn.setMenu(self._import_menu)

        for _superseded in (
            self._export_png_btn, self._replace_png_btn,
            self._export_native_btn, self._replace_native_btn,
            self._export_cells_png_btn, self._export_per_cell_btn,
            self._import_cells_png_btn, self._import_per_cell_btn,
        ):
            _superseded.setParent(self)
            _superseded.setVisible(False)

        for btn in (
            self._export_as_btn, self._import_as_btn,
            self._duplicate_entry_btn,
        ):
            btn.setEnabled(False)
        # Import requires an undo stack to push onto; without one the widget
        # runs in read-only mode (e.g. embedded in a viewer) — hide the Import
        # dropdown + mutating actions, keep Export.
        if self._undo_stack is None:
            self._import_as_btn.setVisible(False)
            self._import_pal_with_sheet_cb.setVisible(False)
            self._duplicate_entry_btn.setVisible(False)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(8, 8, 8, 8)

        # Tile preview lives in the "Tiles" tab; the OAM-composed cells
        # render lives in "Cells". Splitting the two means the column-
        # spinner / cell layout doesn't clutter the tile-mode UI, and
        # users who only ever look at one mode aren't paying for the
        # other's render on every selection change (see `_cells_dirty`).
        tiles_tab = QWidget()
        tiles_layout = QVBoxLayout(tiles_tab)
        tiles_layout.setContentsMargins(0, 0, 0, 0)
        tiles_layout.addWidget(self._scroll, 1)

        cells_tab = QWidget()
        cells_layout = QVBoxLayout(cells_tab)
        cells_layout.setContentsMargins(0, 0, 0, 0)
        # Footer (not header) per user request — the column control + the
        # conflict warning stay close to the preview but out of the way
        # of the actual cells render.
        # "Add cell" appends a new, independently-editable cell (a copy of
        # the last one pointing at a freshly-duplicated tile block) so a
        # single-cell sprite can gain frames to animate between. Hidden in
        # read-only mode.
        self._add_cell_btn = QPushButton("+ Add cell (new frame)")
        self._add_cell_btn.setToolTip(wrap_tooltip(
            "Append a new cell that starts as a pixel-perfect copy of the "
            "last one, backed by its own duplicated tiles (the NCGR grows). "
            "Paint it independently via the tile / cell PNG tools, then "
            "reference it from the Animation tab to build a new frame — e.g. "
            "an eyes-closed copy of a portrait for a blink."
        ))
        self._add_cell_btn.clicked.connect(self._on_add_cell)
        self._add_cell_btn.setEnabled(False)
        if self._undo_stack is None:
            self._add_cell_btn.setVisible(False)

        cells_footer = QHBoxLayout()
        cells_footer.setContentsMargins(0, 0, 0, 0)
        cells_footer.addWidget(self._cells_show_all_cb)
        cells_footer.addSpacing(12)
        cells_footer.addWidget(QLabel("Columns"))
        cells_footer.addWidget(self._cells_columns_spin)
        cells_footer.addSpacing(12)
        cells_footer.addWidget(self._cells_conflict_label)
        cells_footer.addStretch(1)
        cells_layout.addWidget(self._cells_scroll, 1)
        cells_layout.addLayout(cells_footer)

        self._preview_tabs = QTabWidget()
        self._preview_tabs.addTab(tiles_tab, "Tiles")
        self._preview_tabs.addTab(cells_tab, "Cells")
        self._preview_tabs.addTab(self._build_anim_tab(), "Animation")
        self._anim_tab_index = self._preview_tabs.count() - 1
        # Disabled until a sprite with real animation is selected.
        self._preview_tabs.setTabEnabled(self._anim_tab_index, False)
        self._preview_tabs.currentChanged.connect(self._on_preview_tab_changed)
        right_layout.addWidget(self._preview_tabs, 1)
        # Import/Export dropdowns (every PNG mode + native NCGR+NCLR live
        # inside), then the standalone structural actions. All pinned to one
        # width so they read as an aligned group.
        io_btns = (
            self._import_as_btn, self._export_as_btn,
            self._duplicate_entry_btn, self._add_cell_btn,
        )
        max_io_w = max(
            w.sizeHint().width()
            for w in (*io_btns, self._import_pal_with_sheet_cb)
        )
        for b in io_btns:
            b.setFixedWidth(max_io_w)
        io_col = QVBoxLayout()
        io_col.setSpacing(4)
        io_col.addWidget(self._import_as_btn)
        io_col.addWidget(self._export_as_btn)
        io_col.addWidget(self._import_pal_with_sheet_cb)
        io_col.addStretch(1)
        actions_col = QVBoxLayout()
        actions_col.setSpacing(4)
        actions_col.addWidget(self._duplicate_entry_btn)
        actions_col.addWidget(self._add_cell_btn)
        actions_col.addStretch(1)
        controls_row = QHBoxLayout()
        controls_row.addLayout(controls)
        controls_row.addSpacing(16)
        controls_row.addLayout(io_col)
        controls_row.addSpacing(12)
        controls_row.addLayout(actions_col)
        controls_row.addStretch(1)
        controls_row.addLayout(meta_form)
        right_layout.addLayout(controls_row)

        # List column: index list on top, "+ Add Entry" toolbar below.
        # Placement mirrors BTCHR — the button sits under the list because
        # appended entries always land at the bottom, so the affordance
        # lives next to where the new row appears.
        self._add_entry_btn = QPushButton("+ Add Entry")
        self._add_entry_btn.setToolTip(
            "Append a new SPR_* entry cloned from the currently-selected "
            "entry. Grows SPR_CHR/SPR_PAL/SPR_CEL/SPR_ANM in lockstep."
        )
        self._add_entry_btn.clicked.connect(self._on_add_entry)
        # Read-only mode (no undo stack) hides the button to match the
        # rest of the import affordances.
        if self._undo_stack is None:
            self._add_entry_btn.setVisible(False)
        list_col = QWidget()
        list_col_layout = QVBoxLayout(list_col)
        list_col_layout.setContentsMargins(0, 0, 0, 0)
        list_col_layout.setSpacing(4)
        list_col_layout.addWidget(self._list)
        list_col_layout.addWidget(self._add_entry_btn)

        # Palette sidebar — its own pane so a 256-color 8bpp palette (16×16
        # swatches) has room and scrolls instead of stretching the form.
        palette_col = QWidget()
        palette_col_layout = QVBoxLayout(palette_col)
        palette_col_layout.setContentsMargins(4, 4, 4, 4)
        palette_col_layout.setSpacing(6)
        pal_header = QHBoxLayout()
        palette_title = QLabel("Palette")
        palette_title.setStyleSheet("font-weight: bold;")
        pal_header.addWidget(palette_title)
        pal_header.addStretch(1)
        pal_header.addWidget(self._eyedropper_btn)
        palette_col_layout.addLayout(pal_header)
        self._palette_scroll = QScrollArea()
        self._palette_scroll.setWidget(self._palette_grid)
        self._palette_scroll.setWidgetResizable(False)
        self._palette_scroll.setAlignment(Qt.AlignTop | Qt.AlignHCenter)
        self._palette_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._palette_scroll.setMinimumWidth(self._palette_grid.width() + 20)
        self._palette_scroll.setFixedHeight(
            min(self._palette_grid.height() + 2, DEFAULT_SCROLL_MAX_H)
        )
        palette_col_layout.addWidget(self._palette_scroll)
        palette_col_layout.addWidget(self._palette_editor)
        palette_col_layout.addWidget(self._palette_adjuster)
        palette_col_layout.addLayout(self._build_borrow_palette_section())
        palette_col_layout.addStretch(1)

        # Toolkit wires grid ↔ editor ↔ adjuster + the live preview; the
        # eyedropper button is wired manually (SPR uses its own click-capture,
        # not the shared TransparentColorPicker), so picker=None here.
        self._palette_toolkit = PaletteToolkit(
            self, self._palette_grid, self._palette_editor, self._palette_adjuster,
            get_palette=self._palette_get,
            write_palette=self._apply_palette_colors,
            set_preview_palette=self._palette_set_preview,
            refresh_preview=self._refresh_live_preview,
            scroll=self._palette_scroll,
        )

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(list_col)
        splitter.addWidget(right)
        splitter.addWidget(palette_col)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 0)
        splitter.setSizes([220, 760, 260])
        # Collapsible so the user can reclaim the width when they don't need
        # the palette open.
        splitter.setCollapsible(2, True)
        splitter.splitterMoved.connect(lambda *_: self._palette_toolkit.reflow())

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(splitter)

        # Live-update the cell-button gating whenever the policy menu is
        # flipped. ``cell_export_policy`` self-prunes callbacks whose
        # bound widget was Qt-deleted (catches RuntimeError) so we don't
        # need an explicit unregister on tear-down.
        _register_cell_policy_callback(self._on_cell_policy_changed)

    # ---- selection / refresh --------------------------------------------

    def _on_index_selected(self, ix: int) -> None:
        self._current_idx = ix
        self._session.remember_selection(self._CURSOR_KEY, ix)
        self._export_as_btn.setEnabled(True)
        if self._undo_stack is not None:
            self._import_as_btn.setEnabled(True)
            self._duplicate_entry_btn.setEnabled(True)
            self._add_cell_btn.setEnabled(True)
        self._refresh_palette_combo()
        self._refresh_meta_and_preview()
        self._reset_borrow()
        # A new sprite has different cells/sequences — blindly continuing
        # playback would render against the wrong tile bank. Stop first
        # (after _refresh_meta_and_preview so the grid restore reads the
        # fresh _cached), then repopulate the animation panel.
        self._stop_anim_playback()
        # Recompute shared-tile detection so the four cell-mode buttons
        # are gated correctly for the freshly selected sprite. Has to
        # run *after* ``_refresh_meta_and_preview`` because the cached
        # NCER it reads is populated there.
        self._recompute_current_conflicts()
        self._refresh_cell_export_button_states()
        # Cells tab uses the same NCGR + palette as the tile preview, so
        # any selection / width / palette change invalidates its render.
        # Re-render lazily — only paint if the user is actually viewing
        # the Cells tab.
        self._cells_dirty = True
        if self._preview_tabs.currentIndex() == 1:
            self._refresh_cells_preview()
        self._refresh_anim_panel()

    def _on_palette_changed(self, bank: int) -> None:
        if bank < 0:
            return
        self._current_palette_bank = bank
        self._refresh_preview_only()
        # Cells render uses the same effective palette — bank changes
        # invalidate it. Width changes don't (cells layout is OAM-driven).
        self._cells_dirty = True
        if self._preview_tabs.currentIndex() == 1:
            self._refresh_cells_preview()

    def _on_width_changed(self, w: int) -> None:
        self._current_width_tiles = w
        if self._current_idx is not None:
            self._width_overrides[self._current_idx] = w
        self._refresh_preview_only()

    def _on_preview_tab_changed(self, idx: int) -> None:
        if idx == 1 and self._cells_dirty:
            self._refresh_cells_preview()
        if idx == self._anim_tab_index:
            self._show_current_anim_frame_static()
        elif self._anim_timer.isActive():
            # Left the Animation tab — stop playback so the timer isn't
            # updating a hidden surface.
            self._stop_anim_playback()

    def _on_cells_columns_changed(self, value: int) -> None:
        if self._current_idx is not None:
            self._cells_columns_overrides[self._current_idx] = value
        self._cells_dirty = True
        if self._preview_tabs.currentIndex() == 1:
            self._refresh_cells_preview()

    def _refresh_cells_preview(self) -> None:
        """Render the OAM-composed cells grid into the Cells tab.

        Column count comes from the per-entry override (if the user has
        adjusted the spinner for this sprite this session), else from
        the current spinner value clamped to ``[1, n_cells]``. Single-
        cell sprites still render — useful for portraits/icons where
        seeing the composed cell side-by-side with the tile grid helps
        explain the OAM layout. Caller is responsible for calling this
        only when the Cells tab is actually visible / dirty.
        """
        self._cells_dirty = False
        self._refresh_conflict_label()
        if self._current_idx is None:
            self._cells_label.setText("Select a sprite to preview.")
            return
        ctx = self._cell_ctx(for_preview=True)
        layout = self._cell_layout()
        if ctx is None or layout is None:
            self._cells_label.setText("(no cells)")
            return
        n_cells = len(ctx.ncer.cells)
        sel = max(0, min(self._cell_view_spin.value(), n_cells - 1))
        show_all = self._cells_show_all_cb.isChecked()
        # Columns only apply to the grid; disable the spinner in single view.
        self._cells_columns_spin.setEnabled(show_all)
        if not show_all:
            img = shared_render_one_cell_qimage(ctx, sel)
            columns = 1
        else:
            # Spinner sync: use the per-entry override if present, otherwise
            # cap the live value at n_cells so a sprite with fewer cells
            # than the previous one doesn't render a half-empty grid.
            if self._current_idx in self._cells_columns_overrides:
                columns = max(1, min(n_cells, self._cells_columns_overrides[self._current_idx]))
            else:
                columns = max(1, min(n_cells, self._cells_columns_spin.value()))
            # Block signals so updating the spinner to match clamped value
            # doesn't fire _on_cells_columns_changed → re-render loop.
            self._cells_columns_spin.blockSignals(True)
            self._cells_columns_spin.setValue(columns)
            self._cells_columns_spin.blockSignals(False)
            img = shared_render_cells_qimage(ctx, columns)
        if img is None:
            self._cells_label.setText("(empty render)")
            return
        pm = QPixmap.fromImage(img)
        scale = 1
        # Match the tile preview's 2× zoom for small sprites.
        if max(pm.width(), pm.height()) < 256:
            pm = pm.scaled(
                pm.width() * 2, pm.height() * 2,
                Qt.KeepAspectRatio, Qt.FastTransformation,
            )
            scale = 2
        # Outline the selected cell in the grid (the one "Add cell" clones).
        if show_all and n_cells > 1:
            _rects, max_w, max_h = layout
            col, row = sel % columns, sel // columns
            painter = QPainter(pm)
            painter.setPen(QPen(QColor(0x2E, 0x9A, 0xFF), 2))
            painter.drawRect(
                col * max_w * scale, row * max_h * scale,
                max_w * scale - 1, max_h * scale - 1,
            )
            painter.end()
        self._cells_label.setPixmap(pm)
        self._cells_label.setMinimumSize(pm.size())

    def _on_cell_view_changed(self, _value: int) -> None:
        # Re-render the Cells tab (single-cell view or grid highlight) when
        # the selected cell changes.
        self._cells_dirty = True
        if self._preview_tabs.currentIndex() == 1:
            self._refresh_cells_preview()

    def _on_cells_show_all_toggled(self, _checked: bool) -> None:
        self._cells_dirty = True
        if self._preview_tabs.currentIndex() == 1:
            self._refresh_cells_preview()

    def _recompute_current_conflicts(self) -> None:
        """Refresh ``_current_conflicts`` from the currently-cached NCER.

        Called on selection change. The cached NCER is populated by
        ``_refresh_meta_and_preview``; if it isn't there (no cells, parse
        failed) treat the sprite as conflict-free so the cell buttons
        fall through their other gating (which will still disable them
        when ``_cell_ctx`` returns ``None``).
        """
        ctx = self._cell_ctx()
        if ctx is None:
            self._current_conflicts = None
            return
        self._current_conflicts = detect_oam_tile_conflicts(
            ctx.ncer, is_8bpp=ctx.is_8bpp,
        )

    def _refresh_conflict_label(self) -> None:
        """Update the cells-footer warning to match the cached conflicts.

        Shows nothing for conflict-free sprites; otherwise shows the
        shared-tile count with a hint about which tile-mode buttons to
        use instead. The text reads slightly differently depending on
        whether the user has overridden the safety gate via the Edit
        menu (so they can tell the buttons are actually clickable now).
        """
        conflicts = self._current_conflicts
        if conflicts is None or not conflicts.has_any:
            self._cells_conflict_label.setText("")
            return
        if allow_shared_tile_exports():
            msg = (
                f"{conflicts.shared_tiles} shared tiles: export is fine, but "
                f"per-cell import may not round-trip cleanly. Prefer "
                f"Export/Import tiles PNG to edit this sprite."
            )
        else:
            msg = (
                f"{conflicts.shared_tiles} shared tiles: export is fine, but "
                f"per-cell import is disabled. Use Export/Import tiles PNG to "
                f"edit this sprite."
            )
        self._cells_conflict_label.setText(msg)

    def _refresh_cell_export_button_states(self) -> None:
        """Gate the four cell-mode export/import buttons.

        Export is read-only — it can never corrupt the sprite — so the two
        export buttons follow selection alone, even for shared-tile sprites
        (e.g. the Digi-Egg animations, whose 37 cells reuse 8 tile blocks).
        Only *import* is gated: writing per-cell PNGs back through shared
        tiles is last-write-wins, so it stays disabled when the sprite has
        shared tiles and the Edit-menu override is off. Import buttons also
        require an undo stack (read-only mode hides them, see ``__init__``).
        """
        has_selection = self._current_idx is not None
        conflicts = self._current_conflicts
        blocked_by_conflicts = (
            conflicts is not None
            and conflicts.has_any
            and not allow_shared_tile_exports()
        )
        import_ok = has_selection and not blocked_by_conflicts
        # Gate the Export/Import dropdown's cell rows (the individual buttons
        # they replaced are hidden; the menu QActions carry the gating now).
        self._act_export_cells.setEnabled(has_selection)
        self._act_export_per_cell.setEnabled(has_selection)
        if self._undo_stack is not None:
            self._act_import_cells.setEnabled(import_ok)
            self._act_import_per_cell.setEnabled(import_ok)

    def _on_cell_policy_changed(self, _enabled: bool) -> None:
        """Menu fired — re-evaluate button gating + warning text.

        Self-pruning callback: if the widget has been Qt-deleted the
        policy module catches the ``RuntimeError`` and drops the
        registration, so we don't need an explicit deregister hook.
        """
        self._refresh_cell_export_button_states()
        self._refresh_conflict_label()

    def _refresh_palette_combo(self) -> None:
        if self._current_idx is None:
            return
        pal_raw = self._pal_pak.entries[self._current_idx]
        try:
            palettes, _ = sprite.parse_nclr(sprite.maybe_decompress(pal_raw))
        except ValueError:
            palettes = []
        # If the CHR is 8bpp and the NCLR is split into 4bpp banks the
        # engine concatenates them — show one synthetic "Flat (banks
        # concatenated)" entry instead of per-bank picks.
        chr_compressed = self._chr_pak.entries[self._current_idx]
        try:
            _, bd, *_ = sprite.parse_ncgr(sprite.maybe_decompress(chr_compressed))
        except ValueError:
            bd = 3
        chr_8bpp = (bd == 4)
        banks_are_16 = bool(palettes) and len(palettes[0]) == 16

        self._palette_combo.blockSignals(True)
        self._palette_combo.clear()
        if chr_8bpp and banks_are_16:
            self._palette_combo.addItem("Flat (banks concatenated)")
        else:
            for i in range(len(palettes)):
                self._palette_combo.addItem(f"Bank {i}")
            if not palettes:
                self._palette_combo.addItem("(no palette)")
        self._current_palette_bank = 0
        self._palette_combo.setCurrentIndex(0)
        self._palette_combo.blockSignals(False)

    def _refresh_meta_and_preview(self) -> None:
        if self._current_idx is None:
            return
        ix = self._current_idx
        chr_compressed = self._chr_pak.entries[ix]
        pal_raw = self._pal_pak.entries[ix]
        cel_raw = self._cel_pak.entries[ix]

        # Parse all three before touching the UI so a malformed entry
        # surfaces as a single error message rather than half-updated state.
        try:
            chr_decompressed = sprite.maybe_decompress(chr_compressed)
            tile_bytes, bit_depth, hint_w, _hint_h, is_bitmap = sprite.parse_ncgr(chr_decompressed)
            palettes, _pal_bpp = sprite.parse_nclr(sprite.maybe_decompress(pal_raw))
            parsed_ncer = ncer_mod.parse_ncer(cel_raw)
        except ValueError as exc:
            self._image_label.setText(f"Entry 0x{ix:04x} failed to parse:\n{exc}")
            return

        bytes_per_tile = 32 if bit_depth == 3 else 64
        n_tiles = len(tile_bytes) // bytes_per_tile
        min_tiles = ncer_mod.min_tiles_required(parsed_ncer, bpp4=(bit_depth == 3))

        # Default width-tiles per entry: prefer the RAHC hint when present,
        # otherwise pick a layout from the cell bbox (most DWDD sprites
        # store 0xFFFF in RAHC, so the bbox path is the common case).
        # A user pick on this sprite wins over both — revisiting the same
        # sprite keeps the width they chose last time.
        if self._current_idx in self._width_overrides:
            default_w = self._width_overrides[self._current_idx]
        elif hint_w:
            default_w = hint_w
        else:
            bbox_w, _ = ncer_mod.sprite_bbox(parsed_ncer)
            default_w = self._default_width_tiles_for_bbox(bbox_w)
        self._width_spin.blockSignals(True)
        self._width_spin.setValue(default_w)
        self._current_width_tiles = default_w
        self._width_spin.blockSignals(False)

        # Metadata block.
        bpp_label = {3: "4bpp", 4: "8bpp"}.get(bit_depth, f"raw={bit_depth}")
        mapping_label = "1D" if parsed_ncer.is_1d else "2D"
        mapping_label += f"  (boundary {parsed_ncer.boundary_bytes}B/slot)"
        self._meta_tiles.setText(str(n_tiles))
        self._meta_bpp.setText(bpp_label)
        self._meta_palettes.setText(str(len(palettes)))
        self._meta_cells.setText(str(len(parsed_ncer.cells)))
        self._meta_mapping.setText(mapping_label)
        # Highlight when min_tiles_required exceeds what the CHR provides —
        # that'd mean an in-game OAM reads off-end. Vanilla never does this,
        # but a future user-replaced CHR could.
        flag = ""
        if min_tiles > n_tiles:
            flag = f"  ⚠ exceeds NCGR ({n_tiles})"
        self._meta_min_tiles.setText(f"{min_tiles}{flag}")
        gap_tiles = _oam_gap_tile_set(
            parsed_ncer, n_tiles, bpp4=(bit_depth == 3),
        )
        if not parsed_ncer.cells:
            self._meta_oam_gaps.setText("—")
        elif gap_tiles:
            self._meta_oam_gaps.setText(f"{len(gap_tiles)} tiles")
        else:
            self._meta_oam_gaps.setText("0")
        self._meta_chr_size.setText(
            f"{len(chr_compressed)}B compressed / {len(chr_decompressed)}B raw"
        )

        # Cache parsed payload for fast width/palette changes. parsed_ncer
        # is stashed here too so the OAM overlay can paint without
        # reparsing the NCER on every preview refresh.
        self._cached = (tile_bytes, bit_depth, palettes, is_bitmap)
        self._cached_ncer = parsed_ncer
        # Keep the cell selector in range for the (possibly new) cell count.
        n_cells = len(parsed_ncer.cells)
        self._cell_view_spin.blockSignals(True)
        self._cell_view_spin.setRange(0, max(0, n_cells - 1))
        if self._cell_view_spin.value() > max(0, n_cells - 1):
            self._cell_view_spin.setValue(max(0, n_cells - 1))
        self._cell_view_spin.setEnabled(n_cells > 0)
        self._cell_view_spin.blockSignals(False)
        self._refresh_preview_only()

    def _refresh_preview_only(self, sync_palette: bool = True) -> None:
        cached = getattr(self, "_cached", None)
        if cached is None:
            return
        tile_bytes, bit_depth, palettes, is_bitmap = cached
        if not palettes:
            self._image_label.setText("(no palette banks in NCLR)")
            return
        # Match the engine: 8bpp tiles + 16-color banks = flat concatenated
        # palette. 4bpp uses one bank at a time (OAM pal field picks it).
        chr_8bpp = (bit_depth == 4)
        banks_are_16 = len(palettes[0]) == 16
        if chr_8bpp and banks_are_16:
            palette = [c for bank in palettes for c in bank]
        else:
            bank = min(self._current_palette_bank, len(palettes) - 1)
            palette = palettes[bank]
        # While the batch adjuster drags, render the previewed colours.
        if self._preview_palette is not None:
            palette = self._preview_palette
        rgba, w, h = sprite.render_rgba(
            tile_bytes,
            bit_depth,
            palette,
            self._current_width_tiles,
            is_bitmap,
        )
        if w == 0 or h == 0:
            self._image_label.setText("(empty render)")
            return
        # QImage needs the buffer to outlive the image; stash it on self.
        self._rgba_buf = rgba
        img = QImage(self._rgba_buf, w, h, w * 4, QImage.Format_RGBA8888)
        # Stash a copy (detached from the buffer) for PNG export so saving
        # works even after the next render reuses _rgba_buf.
        self._current_qimage = img.copy()
        pix = QPixmap.fromImage(img)
        # Show pixel-accurate at 2x for legibility on small sprites; if the
        # rendered image is already bigger than the viewport, no scaling.
        if max(w, h) < 256:
            pix = pix.scaled(w * 2, h * 2, Qt.KeepAspectRatio, Qt.FastTransformation)
        if self._show_oam_overlay:
            self._paint_oam_gap_overlay(pix, bit_depth, w, h)
        self._image_label.setPixmap(pix)
        # Stash dimensions for click→source-pixel mapping in the picker.
        self._preview_src_size = (w, h)
        self._preview_pixmap_size = (pix.width(), pix.height())
        self._sync_transparent_field()
        # Skip re-syncing the grid/adjuster during a live batch-adjust drag —
        # that would reset the pending selection mid-recolour.
        if sync_palette:
            self._sync_palette_grid()

    # ---- OAM overlay ----------------------------------------------------

    def _on_oam_overlay_toggled(self, checked: bool) -> None:
        self._show_oam_overlay = checked
        self._refresh_preview_only()

    def _paint_oam_gap_overlay(
        self, pix: QPixmap, bit_depth: int, src_w: int, src_h: int,
    ) -> None:
        """Tint tiles no OAM covers red on the tile-grid preview.

        Complement of :func:`ncer.cell_tile_ranges` — everything NOT
        referenced by any cell is painted with translucent red so users
        can see at a glance which tiles a PNG edit would silently
        discard. Draws whole 8×8 tiles rather than a per-pixel checker
        (BTCHR's approach) since the sprite_browser preview unit *is*
        the tile — sub-tile precision would just add noise.
        """
        ncer_obj = getattr(self, "_cached_ncer", None)
        if ncer_obj is None or src_w == 0 or src_h == 0:
            return
        cached = getattr(self, "_cached", None)
        if cached is None:
            return
        tile_bytes, *_ = cached
        bytes_per_tile = 32 if bit_depth == 3 else 64
        n_tiles = len(tile_bytes) // bytes_per_tile if bytes_per_tile else 0
        gap_tiles = _oam_gap_tile_set(
            ncer_obj, n_tiles, bpp4=(bit_depth == 3),
        )
        if not gap_tiles:
            return
        width_tiles = self._current_width_tiles
        if width_tiles <= 0:
            return
        scale_x = pix.width() / src_w
        scale_y = pix.height() / src_h

        painter = QPainter(pix)
        try:
            painter.setRenderHint(QPainter.Antialiasing, False)
            painter.setBrush(QBrush(QColor(0xB0, 0x00, 0x20, 140)))
            painter.setPen(QPen(QColor(0xB0, 0x00, 0x20, 220), 1))
            for t in sorted(gap_tiles):
                row = t // width_tiles
                col = t % width_tiles
                x = int(col * 8 * scale_x)
                y = int(row * 8 * scale_y)
                rw = int(8 * scale_x)
                rh = int(8 * scale_y)
                painter.drawRect(x, y, rw, rh)
        finally:
            painter.end()

    # ---- transparent-color editing --------------------------------------

    def _transparent_target_bank(self) -> Optional[int]:
        """Bank index whose slot 0 the "Transparent color" field controls.

        Returns ``None`` when there's no palette to act on. For 4bpp CHR
        the field tracks the currently-displayed bank (each bank has its
        own transparent color). For 8bpp + concatenated 16-color banks
        the engine's transparent index is bank0[0], so the field always
        targets bank 0. For 8bpp single-bank NCLRs bank 0 is again the
        only bank.
        """
        cached = getattr(self, "_cached", None)
        if cached is None:
            return None
        _tb, bit_depth, palettes, _ib = cached
        if not palettes:
            return None
        chr_8bpp = (bit_depth == 4)
        banks_are_16 = len(palettes[0]) == 16
        if chr_8bpp and banks_are_16:
            return 0
        if chr_8bpp:
            return 0
        return min(self._current_palette_bank, len(palettes) - 1)

    def _sync_transparent_field(self) -> None:
        """Refresh the hex field + swatch from the live NCLR's slot 0.
        Called after every preview render so palette-bank changes and
        undo/redo of slot-0 edits both reflect immediately."""
        bank_idx = self._transparent_target_bank()
        cached = getattr(self, "_cached", None)
        if bank_idx is None or cached is None:
            self._transparent_hex.blockSignals(True)
            self._transparent_hex.setText("")
            self._transparent_hex.blockSignals(False)
            self._transparent_swatch.setStyleSheet(
                "background-color: transparent; border: 1px solid #555;"
            )
            return
        _tb, _bd, palettes, _ib = cached
        r, g, b = palettes[bank_idx][0]
        hex_str = f"#{r:02X}{g:02X}{b:02X}"
        self._transparent_hex.blockSignals(True)
        self._transparent_hex.setText(hex_str)
        self._transparent_hex.blockSignals(False)
        self._transparent_swatch.setStyleSheet(
            f"background-color: {hex_str}; border: 1px solid #555;"
        )

    @staticmethod
    def _parse_hex_color(text: str) -> Optional[Tuple[int, int, int]]:
        s = text.strip().lstrip("#")
        if len(s) == 3:
            s = "".join(c * 2 for c in s)
        if len(s) != 6:
            return None
        try:
            return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
        except ValueError:
            return None

    def _on_transparent_hex_submitted(self) -> None:
        if self._current_idx is None or self._undo_stack is None:
            return
        bank_idx = self._transparent_target_bank()
        if bank_idx is None:
            return
        rgb = self._parse_hex_color(self._transparent_hex.text())
        if rgb is None:
            # Revert the field to the live value so a bad input doesn't
            # silently lose the user's actual current state.
            self._sync_transparent_field()
            QMessageBox.warning(
                self, "Bad color",
                "Enter a hex color like #FF00FF or #f0f.",
            )
            return
        self._apply_transparent_color(bank_idx, rgb)

    @staticmethod
    def _snap5(rgb: Tuple[int, int, int]) -> Tuple[int, int, int]:
        """Project an 8-bit RGB triple onto the 5-bit grid NCLR stores
        colors at, expressed back in 8-bit. Two RGBs that snap to the
        same triple encode to identical NCLR bytes."""
        def s(v: int) -> int:
            return (v * 31 + 127) // 255 * 255 // 31
        return (s(rgb[0]), s(rgb[1]), s(rgb[2]))

    def _sync_palette_grid(self) -> None:
        """Feed the swatch grid + inline editor + batch adjuster the palette the
        preview is currently using (concatenated flat for 8bpp, else the
        selected 4bpp bank). Called after every render so bank switches and
        undo/redo of palette edits reflect immediately."""
        toolkit = getattr(self, "_palette_toolkit", None)
        if toolkit is not None:
            toolkit.sync()

    def _palette_get(self) -> List[Tuple[int, int, int]]:
        cached = getattr(self, "_cached", None)
        if cached is None:
            return []
        _tb, bit_depth, palettes, _ib = cached
        palette, _ = self._current_effective_palette(palettes, bit_depth)
        return [tuple(c) for c in palette]

    def _palette_set_preview(self, pal) -> None:
        self._preview_palette = pal

    def _apply_palette_colors(self, mapping) -> None:
        """Recolour N palette slots in one undo step (the batch adjuster's
        Apply). Groups slots by bank the same way :meth:`_apply_palette_color`
        splits a single slot, then does one NCLR rebuild."""
        if self._current_idx is None or self._undo_stack is None:
            return
        ix = self._current_idx
        cached = getattr(self, "_cached", None)
        if cached is None:
            return
        _tb, bit_depth, palettes, _ib = cached
        if not palettes:
            return
        chr_8bpp = (bit_depth == 4)
        banks_are_16 = len(palettes[0]) == 16
        replacements: dict = {}
        changed = 0
        for slot, rgb in mapping.items():
            if chr_8bpp and banks_are_16:
                bank_idx, slot_in_bank = divmod(int(slot), 16)
            else:
                bank_idx = min(self._current_palette_bank, len(palettes) - 1)
                slot_in_bank = int(slot)
            if not (0 <= bank_idx < len(palettes)):
                continue
            if not (0 <= slot_in_bank < len(palettes[bank_idx])):
                continue
            new = (int(rgb[0]), int(rgb[1]), int(rgb[2]))
            if self._snap5(palettes[bank_idx][slot_in_bank]) == self._snap5(new):
                continue
            replacements.setdefault(bank_idx, list(palettes[bank_idx]))
            replacements[bank_idx][slot_in_bank] = new
            changed += 1
        if changed == 0:
            return
        try:
            new_nclr = sprite.build_nclr_from_template(
                self._pal_pak.entries[ix], replacements,
            )
        except ValueError as exc:
            QMessageBox.critical(self, "Build failed", f"NCLR rewrite: {exc}")
            return
        cmd = ReplaceSpriteCommand(
            self._session,
            [(SPR_PAL, ix, sprite.compress_rle30(new_nclr))],
            description=f"Recolor {changed} palette slots for sprite 0x{ix:04x}",
            on_change=self._reload_current_entry,
        )
        self._undo_stack.push(cmd)

    def _apply_palette_color(self, slot: int, rgb: Tuple[int, int, int]) -> None:
        """Recolor one palette slot and push a ReplaceSpriteCommand.

        ``slot`` indexes the *displayed* palette: the flat concatenated
        palette for 8bpp sprites (slot → bank ``slot // 16``, entry
        ``slot % 16``) or the selected bank for 4bpp. Only the NCLR changes
        — recoloring a slot doesn't remap which pixels reference it, so the
        CHR is untouched (unlike the transparent-slot path)."""
        if self._current_idx is None or self._undo_stack is None:
            return
        ix = self._current_idx
        cached = getattr(self, "_cached", None)
        if cached is None:
            return
        _tb, bit_depth, palettes, _ib = cached
        if not palettes:
            return
        chr_8bpp = (bit_depth == 4)
        banks_are_16 = len(palettes[0]) == 16
        if chr_8bpp and banks_are_16:
            bank_idx, slot_in_bank = divmod(slot, 16)
        else:
            bank_idx = min(self._current_palette_bank, len(palettes) - 1)
            slot_in_bank = slot
        if not (0 <= bank_idx < len(palettes)):
            return
        if not (0 <= slot_in_bank < len(palettes[bank_idx])):
            return
        # No-op if the color is unchanged once snapped to the 5-bit grid the
        # NCLR actually stores — avoids a redundant undo entry.
        if self._snap5(palettes[bank_idx][slot_in_bank]) == self._snap5(rgb):
            return
        replacements = {bank_idx: list(palettes[bank_idx])}
        replacements[bank_idx][slot_in_bank] = rgb
        try:
            new_nclr = sprite.build_nclr_from_template(
                self._pal_pak.entries[ix], replacements,
            )
        except ValueError as exc:
            QMessageBox.critical(self, "Build failed", f"NCLR rewrite: {exc}")
            return
        cmd = ReplaceSpriteCommand(
            self._session,
            [(SPR_PAL, ix, sprite.compress_rle30(new_nclr))],
            description=(
                f"Recolor palette bank {bank_idx} slot {slot_in_bank} "
                f"for sprite 0x{ix:04x}"
            ),
            on_change=self._reload_current_entry,
        )
        self._undo_stack.push(cmd)

    def _apply_transparent_color(
        self, bank_idx: int, rgb: Tuple[int, int, int],
    ) -> None:
        """Push a ReplaceSpriteCommand that makes ``rgb`` the bank's
        transparent color.

        Slot 0 of ``bank_idx`` is rewritten to ``rgb``. If ``rgb``
        already lives at some other palette slot K, the CHR pixel data
        is also rewritten: every pixel that references K is remapped to
        0. Without that remap the engine still renders those pixels
        opaquely (the index, not the RGB, drives the alpha=0 transparency
        rule) — which is what the user reported on dialog portraits
        where palette index 0 is honored but matching RGB alone isn't.

        Slot 0's RGB is only cosmetic for the engine; it shows up in
        PNG exports and external NCLR-aware tools.

        For 8bpp + concatenated 16-color banks, the search spans the
        flat 256-slot palette and the remap operates on the 8bpp tile
        data directly (pixel value = flat index).

        No-op if the new color already matches slot 0 (5-bit snapped)
        and no remap candidate exists.
        """
        if self._current_idx is None or self._undo_stack is None:
            return
        ix = self._current_idx
        cached = getattr(self, "_cached", None)
        if cached is None:
            return
        _tb, bit_depth, palettes, _ib = cached
        if not palettes:
            return

        target_snap = self._snap5(rgb)
        chr_8bpp = (bit_depth == 4)
        banks_are_16 = len(palettes[0]) == 16
        concatenated = chr_8bpp and banks_are_16

        # Find the palette slot that currently holds the picked color.
        # ``source_pixel_idx`` is the value we'll rewrite to 0 in the CHR
        # (8bpp pixel value, or 4bpp nibble — both meanings collapse to
        # "the integer that addresses ``palettes[*][si]``" because the
        # engine selects the bank via OAM, not per-pixel).
        source_pixel_idx: Optional[int] = None
        if concatenated:
            for bi, bank in enumerate(palettes):
                for si in range(len(bank)):
                    if bi == 0 and si == 0:
                        continue
                    if self._snap5(bank[si]) == target_snap:
                        source_pixel_idx = bi * 16 + si
                        break
                if source_pixel_idx is not None:
                    break
        else:
            bank = palettes[bank_idx]
            for si in range(1, len(bank)):
                if self._snap5(bank[si]) == target_snap:
                    source_pixel_idx = si
                    break

        # No-op guard: slot 0 already this color *and* there's nothing to
        # remap → pushing the command would be a redundant undo entry.
        if (
            self._snap5(palettes[bank_idx][0]) == target_snap
            and source_pixel_idx is None
        ):
            return

        # PAL: rewrite slot 0 RGB. The other slots can stay; the source
        # slot becomes unused after the CHR remap but leaving its RGB
        # alone keeps the NCLR diff minimal.
        replacements_pal: dict = {bank_idx: list(palettes[bank_idx])}
        replacements_pal[bank_idx][0] = rgb
        try:
            new_nclr = sprite.build_nclr_from_template(
                self._pal_pak.entries[ix], replacements_pal,
            )
        except ValueError as exc:
            QMessageBox.critical(self, "Build failed", f"NCLR rewrite: {exc}")
            return

        pak_changes = [(SPR_PAL, ix, sprite.compress_rle30(new_nclr))]

        # CHR remap: walk the tile bytes, rewrite any pixel pointing at
        # the source slot to index 0. For 4bpp the byte packs two pixels
        # (low nibble = left pixel) so both halves get checked. For 8bpp
        # the pixel is the whole byte.
        if source_pixel_idx is not None:
            chr_raw = sprite.maybe_decompress(self._chr_pak.entries[ix])
            try:
                tile_bytes, _bd, _hw, _hh, _ibit = sprite.parse_ncgr(chr_raw)
            except ValueError as exc:
                QMessageBox.critical(
                    self, "Build failed", f"NCGR parse: {exc}"
                )
                return
            new_tiles = bytearray(len(tile_bytes))
            if bit_depth == 3:
                k = source_pixel_idx & 0x0F
                for i, byte in enumerate(tile_bytes):
                    lo = byte & 0x0F
                    hi = (byte >> 4) & 0x0F
                    if lo == k:
                        lo = 0
                    if hi == k:
                        hi = 0
                    new_tiles[i] = (hi << 4) | lo
            else:
                k = source_pixel_idx & 0xFF
                for i, byte in enumerate(tile_bytes):
                    new_tiles[i] = 0 if byte == k else byte
            try:
                new_ncgr = sprite.build_ncgr_from_template(
                    bytes(new_tiles), self._chr_pak.entries[ix],
                )
            except ValueError as exc:
                QMessageBox.critical(
                    self, "Build failed", f"NCGR rewrite: {exc}"
                )
                return
            pak_changes.append((SPR_CHR, ix, sprite.compress_rle30(new_ncgr)))

        cmd = ReplaceSpriteCommand(
            self._session,
            pak_changes,
            description=(
                f"Set transparent color (bank {bank_idx}) for sprite 0x{ix:04x}"
            ),
            on_change=self._reload_current_entry,
        )
        self._undo_stack.push(cmd)

    # ---- pick-from-image mode -------------------------------------------

    def _on_pick_toggled(self, checked: bool) -> None:
        self._picking_transparent = checked
        if checked and self._eyedropper_btn.isChecked():
            self._eyedropper_btn.setChecked(False)  # mutually exclusive
        self._update_pick_cursor()

    def _on_slot_pick_toggled(self, checked: bool) -> None:
        self._picking_slot = checked
        if checked and self._pick_transparent_btn.isChecked():
            self._pick_transparent_btn.setChecked(False)  # mutually exclusive
        self._update_pick_cursor()

    def _update_pick_cursor(self) -> None:
        if self._picking_transparent or self._picking_slot:
            self._image_label.setCursor(Qt.CrossCursor)
        else:
            self._image_label.unsetCursor()

    def eventFilter(self, obj, event) -> bool:
        if (
            obj is self._image_label
            and (self._picking_transparent or self._picking_slot)
            and event.type() == QEvent.MouseButtonPress
        ):
            self._sample_preview_pixel(event.position().x(), event.position().y())
            return True
        return super().eventFilter(obj, event)

    def _sample_preview_pixel(self, click_x: float, click_y: float) -> None:
        """Map a click in label coordinates to a source-image pixel and
        write its RGB into the transparent-color field."""
        src_w, src_h = self._preview_src_size
        pix_w, pix_h = self._preview_pixmap_size
        if src_w == 0 or src_h == 0 or pix_w == 0 or pix_h == 0:
            return
        # QLabel centers its pixmap when the label is larger than the
        # pixmap; subtract that offset before scaling back to source coords.
        label_w = self._image_label.width()
        label_h = self._image_label.height()
        off_x = max(0, (label_w - pix_w) // 2)
        off_y = max(0, (label_h - pix_h) // 2)
        px = (click_x - off_x) * src_w / pix_w
        py = (click_y - off_y) * src_h / pix_h
        sx = int(px)
        sy = int(py)
        if not (0 <= sx < src_w and 0 <= sy < src_h):
            return
        img = getattr(self, "_current_qimage", None)
        if img is None:
            return
        color = img.pixelColor(sx, sy)
        if color.alpha() == 0:
            return  # clicked through the sprite, not on it
        rgb = (color.red(), color.green(), color.blue())
        # Eyedropper mode → select the clicked pixel's palette slot; otherwise
        # the transparent-colour pick. Both are one-shot (button un-toggles).
        if self._picking_slot:
            self._eyedropper_btn.setChecked(False)
            self._palette_toolkit.pick_slot_from_rgb(rgb)
            return
        self._pick_transparent_btn.setChecked(False)
        bank_idx = self._transparent_target_bank()
        if bank_idx is None:
            return
        self._apply_transparent_color(bank_idx, rgb)

    # ---- export actions -------------------------------------------------

    def _on_export_png(self) -> None:
        if self._current_idx is None:
            return
        cached = getattr(self, "_cached", None)
        img = getattr(self, "_current_qimage", None)
        if cached is None or img is None:
            return
        tile_bytes, bit_depth, *_ = cached
        bytes_per_tile = 32 if bit_depth == 3 else 64
        n_tiles = len(tile_bytes) // bytes_per_tile
        # Width that doesn't evenly divide tile count means decode pads with
        # transparent slots — re-importing the PNG would inflate the NCGR
        # because the padding rows are indistinguishable from real tiles.
        if n_tiles % self._current_width_tiles != 0:
            choice = QMessageBox.warning(
                self,
                "Width doesn't divide tile count",
                f"Width {self._current_width_tiles} tiles doesn't divide the NCGR's "
                f"{n_tiles} tiles evenly. Exporting will pad the bottom row with "
                f"transparent tiles, which would inflate the NCGR if re-imported "
                f"unchanged.\n\nExport anyway?",
                QMessageBox.Ok | QMessageBox.Cancel,
                QMessageBox.Cancel,
            )
            if choice != QMessageBox.Ok:
                return
        default = f"sprite_0x{self._current_idx:04x}.png"
        path, _ = QFileDialog.getSaveFileName(
            self, "Export tiles PNG", default, "PNG image (*.png)"
        )
        if not path:
            return
        if not img.save(path, "PNG"):
            QMessageBox.critical(self, "Export failed", f"Could not write {path}")

    def _on_export_native(self) -> None:
        if self._current_idx is None:
            return
        ix = self._current_idx
        try:
            ncgr_raw = sprite.maybe_decompress(self._chr_pak.entries[ix])
            nclr_raw = sprite.maybe_decompress(self._pal_pak.entries[ix])
        except ValueError as exc:
            QMessageBox.critical(self, "Export failed", str(exc))
            return
        # Single dialog asking for the NCGR path; the NCLR is written
        # alongside with a matching stem. Avoids a second file-picker round
        # trip and makes the pair obvious in the user's file browser.
        default = f"sprite_0x{ix:04x}.NCGR"
        path, _ = QFileDialog.getSaveFileName(
            self, "Export NCGR (NCLR will be written alongside)",
            default, "NDS character graphic (*.NCGR)",
        )
        if not path:
            return
        base, ext = os.path.splitext(path)
        if not ext:
            path = base + ".NCGR"
        nclr_path = base + ".NCLR"
        try:
            with open(path, "wb") as f:
                f.write(ncgr_raw)
            with open(nclr_path, "wb") as f:
                f.write(nclr_raw)
        except OSError as exc:
            QMessageBox.critical(self, "Export failed", str(exc))
            return
        QMessageBox.information(
            self, "Export complete",
            f"Wrote:\n  {path}\n  {nclr_path}",
        )

    # ---- replace (import) actions ---------------------------------------

    def _current_effective_palette(
        self, palettes: List[sprite.Palette], bit_depth: int,
    ) -> Tuple[sprite.Palette, bool]:
        """Mirror the preview logic to pick the palette indices are matched
        against. Returns ``(palette, is_8bpp)`` so the encoder knows whether
        to pack 1 byte/pixel or two-nibbles-per-byte.

        ``is_8bpp`` is the CHR bit depth (NCGR field == 4), independent of
        whether the NCLR was 4bpp-banked — the engine concatenates 16-color
        banks when the CHR is 8bpp and that's the palette we encode against.
        """
        chr_8bpp = (bit_depth == 4)
        if not palettes:
            return ([], chr_8bpp)
        banks_are_16 = len(palettes[0]) == 16
        if chr_8bpp and banks_are_16:
            return ([c for bank in palettes for c in bank], chr_8bpp)
        bank = min(self._current_palette_bank, len(palettes) - 1)
        return (palettes[bank], chr_8bpp)

    @staticmethod
    def _nearest_palette_index(
        r: int, g: int, b: int, palette: sprite.Palette, start: int,
    ) -> int:
        """Squared-distance nearest match in ``palette[start:]``. Index 0 is
        reserved for the engine's transparent slot, so callers pass
        ``start=1`` to keep alpha-bearing pixels off it."""
        best_i, best_d = start, 1 << 30
        for i in range(start, len(palette)):
            pr, pg, pb = palette[i]
            d = (r - pr) * (r - pr) + (g - pg) * (g - pg) + (b - pb) * (b - pb)
            if d < best_d:
                best_d, best_i = d, i
        return best_i

    def _on_replace_png_dispatch(self) -> None:
        """Pick the import path from the checkbox state.
        Off → quantize against the existing palette; on → rebuild the
        bank from the PNG via median-cut."""
        if self._import_pal_with_sheet_cb.isChecked():
            self._on_replace_png_new_palette()
        else:
            self._on_replace_png()

    def _on_add_entry(self) -> None:
        """Append a new SPR_* entry cloned from the current selection.

        Same handler for the toolbar "+ Add Entry" (below the list) and
        the panel "Duplicate sprite entry" button — neither needs an
        extra picker step because the source is always the currently
        selected entry. Confirms first so the user can back out without
        an undo step on the stack.
        """
        if self._current_idx is None or self._undo_stack is None:
            return
        source = self._current_idx
        new_idx = self._chr_pak.count
        confirm = QMessageBox.question(
            self,
            "Duplicate sprite entry?",
            f"Append a new SPR_* entry at index 0x{new_idx:04x} carrying a "
            f"copy of entry 0x{source:04x} (across SPR_CHR/SPR_PAL/SPR_CEL/"
            f"SPR_ANM)?\n\nEngine extensibility past vanilla 1627 is "
            f"unproven — test in-game before relying on the new entry.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if confirm != QMessageBox.Yes:
            return
        cmd = AppendPakEntriesCommand(
            self._session,
            list(SPR_PARALLEL_PAKS),
            source,
            description=f"Append SPR entry 0x{new_idx:04x} from 0x{source:04x}",
            on_change=self._refresh_after_entry_appended,
        )
        self._undo_stack.push(cmd)
        self._list.select_index(cmd.new_entry_index)

    def _refresh_after_entry_appended(self) -> None:
        """Reconcile the browser's caches with the live pak entry count.

        Called from AppendPakEntriesCommand's redo/undo. Re-syncs the
        local count + label list + RecordListPanel rows — appending on
        redo, dropping on undo.
        """
        live_count = min(
            self._session.sprite_pak(p).count for p in SPR_PARALLEL_PAKS
        )
        if live_count > self._count:
            for ix in range(self._count, live_count):
                self._labels.append(self._compute_index_label(ix))
                self._list.append_record(ix)
            self._count = live_count
        elif live_count < self._count:
            for _ in range(self._count - live_count):
                self._labels.pop()
                self._list.pop_record()
            self._count = live_count

    def _on_replace_png(self) -> None:
        if self._current_idx is None or self._undo_stack is None:
            return
        ix = self._current_idx
        cached = getattr(self, "_cached", None)
        if cached is None:
            return
        _tile_bytes, bit_depth, palettes, _is_bitmap = cached
        if not palettes:
            QMessageBox.warning(
                self, "Cannot replace",
                "The entry has no palette to quantize against. Use "
                "'Import from NCGR+NCLR…' to import a palette too.",
            )
            return

        path, _ = QFileDialog.getOpenFileName(
            self, "Import tiles from PNG", "", "PNG image (*.png);;All files (*)",
        )
        if not path:
            return
        img = QImage(path)
        if img.isNull():
            QMessageBox.critical(self, "Cannot read PNG", f"Could not load {path}")
            return
        img = img.convertToFormat(QImage.Format_RGBA8888)
        w, h = img.width(), img.height()
        if w % 8 != 0 or h % 8 != 0:
            QMessageBox.critical(
                self, "Bad PNG dimensions",
                f"PNG is {w}×{h}; both sides must be multiples of 8 (NDS tiles "
                f"are 8×8).",
            )
            return

        # Reference NCER to size-check the new tile data. min_tiles_required
        # reflects the *engine's* lower bound; a smaller import would let an
        # OAM read past the new tile array's end.
        try:
            parsed_ncer = ncer_mod.parse_ncer(self._cel_pak.entries[ix])
        except ValueError as exc:
            QMessageBox.critical(self, "Cannot replace", f"NCER parse failed: {exc}")
            return
        min_tiles = ncer_mod.min_tiles_required(parsed_ncer, bpp4=(bit_depth == 3))
        new_tile_count = (w // 8) * (h // 8)
        if new_tile_count < min_tiles:
            QMessageBox.critical(
                self, "PNG too small",
                f"PNG has {new_tile_count} tiles but the NCER needs at least "
                f"{min_tiles}. Re-export at a larger size or pad with empty "
                f"tiles.",
            )
            return

        palette, chr_8bpp = self._current_effective_palette(palettes, bit_depth)
        # Walk pixels in row-major order, matching the QImage layout. alpha<128
        # → reserved transparent index 0; opaque pixels match against [1:] so
        # we don't accidentally route a visible color through the transparent
        # slot.
        bits = img.bits()
        # QImage.constBits returns a sip.voidptr — .setsize() is needed before
        # slicing in PySide6 to expose the raw buffer length.
        try:
            bits.setsize(w * h * 4)
        except AttributeError:
            pass
        raw = bytes(bits)
        indices: List[int] = []
        for py in range(h):
            row_off = py * w * 4
            for px in range(w):
                off = row_off + px * 4
                r, g, b, a = raw[off], raw[off + 1], raw[off + 2], raw[off + 3]
                if a < 128:
                    indices.append(0)
                else:
                    indices.append(
                        self._nearest_palette_index(r, g, b, palette, start=1)
                    )

        tile_bytes = sprite.encode_tiles(indices, w, h, bpp4=not chr_8bpp)
        try:
            new_ncgr = sprite.build_ncgr_from_template(
                tile_bytes,
                self._chr_pak.entries[ix],
            )
        except ValueError as exc:
            QMessageBox.critical(self, "Build failed", str(exc))
            return
        new_entry = sprite.compress_rle30(new_ncgr)

        cmd = ReplaceSpriteCommand(
            self._session,
            [(SPR_CHR, ix, new_entry)],
            description=f"Replace sprite 0x{ix:04x} from PNG",
            on_change=self._reload_current_entry,
        )
        self._undo_stack.push(cmd)

    def _on_replace_png_new_palette(self) -> None:
        """Import PNG and rebuild the palette from its colors.

        Slot 0 stays reserved transparent (engine convention), the
        remaining slots get median-cut representatives of the PNG's
        opaque pixels. Writes both SPR_CHR (new tile indices) and
        SPR_PAL (rebuilt bank) atomically — siblings sharing the bank
        will start using the new colors too.

        For 4bpp CHR only the currently-displayed bank gets rebuilt
        (other banks belong to other animations and must stay intact).
        For 8bpp the engine concatenates 16-color banks into a 256
        palette, so all 16 banks get rebuilt from the same median-cut
        result split into 16-color slices.
        """
        if self._current_idx is None or self._undo_stack is None:
            return
        ix = self._current_idx
        cached = getattr(self, "_cached", None)
        if cached is None:
            return
        _tile_bytes, bit_depth, palettes, _is_bitmap = cached

        path, _ = QFileDialog.getOpenFileName(
            self, "Import tiles from PNG (rebuild palette)", "",
            "PNG image (*.png);;All files (*)",
        )
        if not path:
            return
        img = QImage(path)
        if img.isNull():
            QMessageBox.critical(self, "Cannot read PNG", f"Could not load {path}")
            return
        img = img.convertToFormat(QImage.Format_RGBA8888)
        w, h = img.width(), img.height()
        if w % 8 != 0 or h % 8 != 0:
            QMessageBox.critical(
                self, "Bad PNG dimensions",
                f"PNG is {w}×{h}; both sides must be multiples of 8.",
            )
            return

        try:
            parsed_ncer = ncer_mod.parse_ncer(self._cel_pak.entries[ix])
        except ValueError as exc:
            QMessageBox.critical(self, "Cannot replace", f"NCER parse failed: {exc}")
            return
        min_tiles = ncer_mod.min_tiles_required(parsed_ncer, bpp4=(bit_depth == 3))
        new_tile_count = (w // 8) * (h // 8)
        if new_tile_count < min_tiles:
            QMessageBox.critical(
                self, "PNG too small",
                f"PNG has {new_tile_count} tiles but the NCER needs at least "
                f"{min_tiles}.",
            )
            return

        chr_8bpp = (bit_depth == 4)
        banks_are_16 = bool(palettes) and len(palettes[0]) == 16
        rebuild_all_banks = chr_8bpp and banks_are_16
        # Total slot budget for the new palette as the engine sees it.
        # -1 because slot 0 (or slot 0 of bank 0 in the concatenated case)
        # is reserved for the transparent index.
        total_slots = 256 if chr_8bpp else 16
        max_quantized = total_slots - 1

        # Walk PNG pixels once to (a) collect opaque RGB samples for the
        # quantizer and (b) record per-pixel alpha so we can route alpha
        # pixels to slot 0 after the new palette is built.
        bits = img.bits()
        try:
            bits.setsize(w * h * 4)
        except AttributeError:
            pass
        raw = bytes(bits)
        opaque_rgb: List[Tuple[int, int, int]] = []
        is_opaque: List[bool] = [False] * (w * h)
        for py in range(h):
            row_off = py * w * 4
            for px in range(w):
                off = row_off + px * 4
                r, g, b, a = raw[off], raw[off + 1], raw[off + 2], raw[off + 3]
                if a >= 128:
                    is_opaque[py * w + px] = True
                    opaque_rgb.append((r, g, b))

        if not opaque_rgb:
            QMessageBox.critical(
                self, "PNG is fully transparent",
                "Cannot rebuild a palette from a PNG with no opaque pixels.",
            )
            return

        quantized = sprite.quantize_palette(opaque_rgb, max_quantized)
        # Final flat palette: slot 0 transparent placeholder, then the
        # quantized colors. The placeholder color value is arbitrary —
        # the engine never samples it because transparent pixels are
        # culled by index, not by RGB.
        new_flat_palette = [(0, 0, 0)] + quantized
        while len(new_flat_palette) < total_slots:
            new_flat_palette.append((0, 0, 0))

        # Map every pixel to an index in the new flat palette. Opaque
        # pixels nearest-match against [1:] (skip the transparent slot).
        indices: List[int] = []
        for i in range(w * h):
            if not is_opaque[i]:
                indices.append(0)
                continue
            off = i * 4
            r, g, b = raw[off], raw[off + 1], raw[off + 2]
            indices.append(
                self._nearest_palette_index(r, g, b, new_flat_palette, start=1)
            )

        tile_bytes = sprite.encode_tiles(indices, w, h, bpp4=not chr_8bpp)
        try:
            new_ncgr = sprite.build_ncgr_from_template(
                tile_bytes, self._chr_pak.entries[ix],
            )
        except ValueError as exc:
            QMessageBox.critical(self, "Build failed", str(exc))
            return

        # Split the flat palette back into banks the NCLR layout expects.
        if rebuild_all_banks:
            bank_replacements = {
                bi: new_flat_palette[bi * 16:(bi + 1) * 16] for bi in range(16)
            }
        elif chr_8bpp:
            # 8bpp + single 256-color bank: write all 256 into bank 0.
            bank_replacements = {0: new_flat_palette}
        else:
            # 4bpp: only the displayed bank gets rebuilt. Other banks
            # belong to other animations and must stay byte-identical.
            bank_idx = min(self._current_palette_bank, len(palettes) - 1)
            bank_replacements = {bank_idx: new_flat_palette}
        try:
            new_nclr = sprite.build_nclr_from_template(
                self._pal_pak.entries[ix], bank_replacements,
            )
        except ValueError as exc:
            QMessageBox.critical(self, "Build failed", f"NCLR rebuild: {exc}")
            return

        replacements = [
            (SPR_CHR, ix, sprite.compress_rle30(new_ncgr)),
            (SPR_PAL, ix, sprite.compress_rle30(new_nclr)),
        ]
        cmd = ReplaceSpriteCommand(
            self._session, replacements,
            description=f"Replace sprite 0x{ix:04x} from PNG (new palette)",
            on_change=self._reload_current_entry,
        )
        self._undo_stack.push(cmd)

    def _on_replace_native(self) -> None:
        if self._current_idx is None or self._undo_stack is None:
            return
        ix = self._current_idx

        path, _ = QFileDialog.getOpenFileName(
            self, "Import from NCGR (NCLR will be picked up alongside)",
            "", "NDS character graphic (*.NCGR);;All files (*)",
        )
        if not path:
            return
        try:
            with open(path, "rb") as f:
                ncgr_raw = f.read()
        except OSError as exc:
            QMessageBox.critical(self, "Cannot read NCGR", str(exc))
            return
        ncgr_decompressed = sprite.maybe_decompress(ncgr_raw)
        if ncgr_decompressed[:4] != b"RGCN":
            QMessageBox.critical(
                self, "Not an NCGR",
                f"File doesn't start with RGCN magic: {ncgr_decompressed[:4]!r}",
            )
            return

        # Sibling NCLR is optional — users may want to keep the existing
        # palette and only swap tile data. Same-stem .NCLR matches the export
        # path so a roundtrip needs no manual file-picker dance.
        base, _ext = os.path.splitext(path)
        nclr_path = base + ".NCLR"
        nclr_raw: Optional[bytes] = None
        if os.path.exists(nclr_path):
            try:
                with open(nclr_path, "rb") as f:
                    candidate = f.read()
                if sprite.maybe_decompress(candidate)[:4] != b"RLCN":
                    QMessageBox.warning(
                        self, "Sibling NCLR ignored",
                        f"{nclr_path} doesn't look like a NCLR (missing RLCN "
                        f"magic). Keeping the existing palette.",
                    )
                else:
                    nclr_raw = candidate
            except OSError as exc:
                QMessageBox.warning(
                    self, "Sibling NCLR ignored",
                    f"Could not read {nclr_path}: {exc}. Keeping the existing "
                    f"palette.",
                )

        # Tile-count gate uses the *current* NCER and the *new* NCGR's bpp,
        # since OAM addressing depends on bpp via the 2D-mapping byte stride.
        try:
            parsed_ncer = ncer_mod.parse_ncer(self._cel_pak.entries[ix])
            _tb, new_bd, *_ = sprite.parse_ncgr(ncgr_decompressed)
        except ValueError as exc:
            QMessageBox.critical(self, "Cannot replace", str(exc))
            return
        new_tile_count = sprite.ncgr_tile_count(ncgr_decompressed)
        min_tiles = ncer_mod.min_tiles_required(parsed_ncer, bpp4=(new_bd == 3))
        if new_tile_count < min_tiles:
            QMessageBox.critical(
                self, "NCGR too small",
                f"NCGR has {new_tile_count} tiles but the NCER needs at least "
                f"{min_tiles}. The engine would read off the end of the tile "
                f"array.",
            )
            return

        # RAHC+0x12 is per-sprite load-bearing (project memory
        # `project_ncgr_rahc_header_preserve`). Warn if the import doesn't
        # match the original — most "edited NCGRs" from third-party tools
        # zero it, which subtly shifts tile rows in-game.
        orig_ncgr = sprite.maybe_decompress(self._chr_pak.entries[ix])
        try:
            orig_rahc = sprite.find_block(orig_ncgr, b"RAHC")
            new_rahc = sprite.find_block(ncgr_decompressed, b"RAHC")
            orig_x12 = orig_ncgr[orig_rahc + 0x12:orig_rahc + 0x14]
            new_x12 = ncgr_decompressed[new_rahc + 0x12:new_rahc + 0x14]
        except (ValueError, IndexError):
            orig_x12 = new_x12 = b""
        if orig_x12 and new_x12 and orig_x12 != new_x12:
            choice = QMessageBox.warning(
                self, "RAHC header differs",
                f"The imported NCGR's RAHC+0x12 byte differs from the "
                f"original ({orig_x12.hex()} → {new_x12.hex()}). That field "
                f"is per-sprite load-bearing — a mismatch can shift tile "
                f"rows in-game.\n\nProceed anyway?",
                QMessageBox.Ok | QMessageBox.Cancel, QMessageBox.Cancel,
            )
            if choice != QMessageBox.Ok:
                return

        replacements: List[Tuple[str, int, bytes]] = [
            (SPR_CHR, ix, sprite.compress_rle30(ncgr_decompressed)),
        ]
        if nclr_raw is not None:
            nclr_decompressed = sprite.maybe_decompress(nclr_raw)
            replacements.append(
                (SPR_PAL, ix, sprite.compress_rle30(nclr_decompressed))
            )
        cmd = ReplaceSpriteCommand(
            self._session, replacements,
            description=f"Replace sprite 0x{ix:04x} from NCGR+NCLR",
            on_change=self._reload_current_entry,
        )
        self._undo_stack.push(cmd)

    # ---- cell-mode PNG IO ----------------------------------------------

    def _cell_ctx(self, for_preview: bool = False) -> Optional[CellPngContext]:
        """Build a CellPngContext over the current SPR_* sprite.

        Picks the effective palette the same way the preview does
        (concatenate 4bpp banks when the CHR is 8bpp, otherwise use
        the displayed bank), and tags ``is_8bpp`` from the NCGR's
        bit-depth field so the OAM-tile geometry comes out right.
        Returns ``None`` when no entry is loaded or the NCLR is empty.
        """
        cached = getattr(self, "_cached", None)
        ncer_obj = getattr(self, "_cached_ncer", None)
        if cached is None or ncer_obj is None:
            return None
        tile_bytes, bit_depth, palettes, _is_bitmap = cached
        if not palettes:
            return None
        palette, chr_8bpp = self._current_effective_palette(palettes, bit_depth)
        # Preview renders honour a live palette override (batch adjust / borrow);
        # export/import must NOT — they re-index against the committed palette.
        if for_preview and self._preview_palette is not None:
            palette = self._preview_palette
        bytes_per_tile = 64 if chr_8bpp else 32
        n_tiles = len(tile_bytes) // bytes_per_tile
        return CellPngContext(
            ncer=ncer_obj,
            tile_bytes=tile_bytes,
            n_tiles=n_tiles,
            palette=list(palette),
            is_8bpp=chr_8bpp,
        )

    def _cell_layout(self):
        ncer_obj = getattr(self, "_cached_ncer", None)
        if ncer_obj is None:
            return None
        return shared_cell_layout(ncer_obj)

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

    def _on_export_cells_png(self) -> None:
        """Composite all cells into a single PNG laid out as one row.

        Embeds ``spr_mode=cells`` + ``spr_columns`` so re-import recovers
        the layout regardless of how the user resized the file. One row
        is the simplest default — sprite_browser has no preview-side
        column knob, and users can re-arrange in their image editor
        before re-import (the metadata stays attached on save).
        """
        if self._current_idx is None:
            return
        ctx = self._cell_ctx()
        layout = self._cell_layout()
        if ctx is None or layout is None:
            QMessageBox.critical(
                self, "Export failed",
                "Current entry has no cells — nothing to export.",
            )
            return
        n_cells = len(ctx.ncer.cells)
        columns = n_cells
        img = shared_render_cells_qimage(ctx, columns)
        if img is None:
            QMessageBox.critical(self, "Export failed", "Render failed.")
            return
        img.setText("spr_mode", "cells")
        img.setText("spr_columns", str(columns))
        default = f"sprite_0x{self._current_idx:04x}_cells.png"
        path, _ = QFileDialog.getSaveFileName(
            self, "Export cells sheet PNG", default, "PNG (*.png)",
        )
        if not path:
            return
        if not img.save(path, "PNG"):
            QMessageBox.critical(self, "Export failed", f"Could not write {path}.")

    def _on_export_per_cell_pngs(self) -> None:
        """Write each cell to ``<base>_cell_<K>.png`` sized to the union bbox.

        Uniform per-cell size lets users swap cells between sprites
        without manual cropping, mirroring BTCHR's per-cell convention.
        Cells that previously rendered as a tighter sub-rect of the
        union bbox keep alpha=0 in the unused gutter.
        """
        if self._current_idx is None:
            return
        ctx = self._cell_ctx()
        if ctx is None:
            QMessageBox.critical(
                self, "Export failed",
                "Current entry has no cells — nothing to export.",
            )
            return
        n_cells = len(ctx.ncer.cells)
        suggested = f"sprite_0x{self._current_idx:04x}.png"
        path, _ = QFileDialog.getSaveFileName(
            self, "Export per-cell PNGs (pick base name)",
            suggested, "PNG (*.png)",
        )
        if not path:
            return
        base = path[:-4] if path.lower().endswith(".png") else path
        m = re.match(r"^(.*)_cell_\d+$", base)
        if m:
            base = m.group(1)
        for ci in range(n_cells):
            img = shared_render_one_cell_qimage(ctx, ci)
            if img is None:
                QMessageBox.critical(
                    self, "Export failed", f"Could not render cell {ci}.",
                )
                return
            img.setText("spr_mode", "per_cell")
            img.setText("spr_cell", str(ci))
            img.setText("spr_n_cells", str(n_cells))
            out_path = f"{base}_cell_{ci}.png"
            if not img.save(out_path, "PNG"):
                QMessageBox.critical(
                    self, "Export failed", f"Could not write {out_path}.",
                )
                return

    def _cells_bank_replacements(
        self,
        new_flat_palette: List[Tuple[int, int, int]],
        palettes: List[sprite.Palette],
        bit_depth: int,
    ) -> dict:
        """NCLR bank replacements for a rebuilt cells-mode palette.

        Mirrors ``_on_replace_png_new_palette``'s split logic so cells
        and tile-mode rebuilds produce identical NCLR layouts:
        8bpp+banked → 16 banks of 16; 8bpp single → one 256-entry bank;
        4bpp → only the displayed bank gets rebuilt (others belong to
        other animations and must stay byte-identical).
        """
        chr_8bpp = (bit_depth == 4)
        banks_are_16 = bool(palettes) and len(palettes[0]) == 16
        if chr_8bpp and banks_are_16:
            return {bi: new_flat_palette[bi * 16:(bi + 1) * 16] for bi in range(16)}
        if chr_8bpp:
            return {0: new_flat_palette}
        bank_idx = min(self._current_palette_bank, len(palettes) - 1)
        return {bank_idx: new_flat_palette}

    def _on_import_cells_png(self) -> None:
        """Round-trip a composite cells PNG back into the tile bank.

        Same checkbox semantics as tile-mode import — off → quantize
        against the existing effective palette; on → rebuild from the
        PNG and write NCLR alongside.
        """
        if self._current_idx is None or self._undo_stack is None:
            return
        ix = self._current_idx
        cached = getattr(self, "_cached", None)
        if cached is None:
            return
        _tile_bytes, bit_depth, palettes, _is_bitmap = cached
        if not palettes:
            QMessageBox.warning(
                self, "Cannot import",
                "Entry has no NCLR to anchor cell rendering on.",
            )
            return
        layout = self._cell_layout()
        if layout is None:
            QMessageBox.critical(
                self, "Import failed",
                "Current entry has no cells — nothing to import.",
            )
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Import cells sheet PNG", "", "PNG (*.png);;All files (*)",
        )
        if not path:
            return
        img = QImage(path)
        if img.isNull():
            QMessageBox.critical(self, "Cannot read PNG", f"Could not load {path}")
            return

        # Columns from embedded tag, capped at n_cells so over-large
        # values don't produce 0 rows. Falls back to a single row for
        # PNGs made externally (no metadata).
        n_cells = len(layout[0])
        embedded_columns = img.text("spr_columns") or img.text("btchr_columns")
        if embedded_columns:
            try:
                columns = max(1, min(n_cells, int(embedded_columns)))
            except ValueError:
                columns = n_cells
        else:
            columns = n_cells

        chr_8bpp = (bit_depth == 4)
        total_slots = 256 if chr_8bpp else 16

        use_indexed = img.format() == QImage.Format_Indexed8
        checkbox_on = self._import_pal_with_sheet_cb.isChecked()
        pal_from_plte = (
            use_indexed and checkbox_on and len(img.colorTable()) >= 2
        )
        pal_from_quant = (not use_indexed) and checkbox_on
        rebuild_palette = pal_from_plte or pal_from_quant
        if rebuild_palette:
            built = build_palette_from_png(img, total_slots=total_slots)
            if built is None:
                QMessageBox.critical(
                    self, "PNG is fully transparent",
                    "Cannot rebuild a palette from a PNG with no opaque pixels.",
                )
                return
            new_palette: List[Tuple[int, int, int]] = list(built)
        else:
            current_palette, _ = self._current_effective_palette(palettes, bit_depth)
            new_palette = list(current_palette)

        bytes_per_tile = 64 if chr_8bpp else 32
        n_tiles = len(_tile_bytes) // bytes_per_tile
        ctx = CellPngContext(
            ncer=self._cached_ncer,
            tile_bytes=_tile_bytes,
            n_tiles=n_tiles,
            palette=new_palette,
            is_8bpp=chr_8bpp,
        )
        try:
            new_tiles = import_cells_to_tiles(
                img, ctx=ctx, layout=layout,
                columns=columns, palette=new_palette,
            )
        except CellPngError as exc:
            QMessageBox.critical(self, exc.title, exc.message)
            return

        try:
            new_ncgr = sprite.build_ncgr_from_template(
                new_tiles, self._chr_pak.entries[ix],
            )
        except ValueError as exc:
            QMessageBox.critical(self, "Build failed", str(exc))
            return
        replacements: List[Tuple[str, int, bytes]] = [
            (SPR_CHR, ix, sprite.compress_rle30(new_ncgr)),
        ]
        if rebuild_palette:
            bank_replacements = self._cells_bank_replacements(
                new_palette, palettes, bit_depth,
            )
            try:
                new_nclr = sprite.build_nclr_from_template(
                    self._pal_pak.entries[ix], bank_replacements,
                )
            except ValueError as exc:
                QMessageBox.critical(self, "Build failed", f"NCLR rebuild: {exc}")
                return
            replacements.append((SPR_PAL, ix, sprite.compress_rle30(new_nclr)))
            desc = f"Import SPR cells + palette 0x{ix:04x}"
        else:
            desc = f"Import SPR cells 0x{ix:04x}"

        cmd = ReplaceSpriteCommand(
            self._session, replacements,
            description=desc,
            on_change=self._reload_current_entry,
        )
        self._undo_stack.push(cmd)

    def _on_import_per_cell_pngs(self) -> None:
        """Read N per-cell PNGs and rebuild the tile bank from them.

        User picks any ``<base>_cell_<K>.png``; siblings for the other
        cells are located by stripping the suffix and re-applying each
        index. Palette decision matches the composite path's checkbox.
        """
        if self._current_idx is None or self._undo_stack is None:
            return
        ix = self._current_idx
        cached = getattr(self, "_cached", None)
        if cached is None:
            return
        _tile_bytes, bit_depth, palettes, _is_bitmap = cached
        if not palettes:
            QMessageBox.warning(
                self, "Cannot import",
                "Entry has no NCLR to anchor cell rendering on.",
            )
            return
        layout = self._cell_layout()
        if layout is None:
            QMessageBox.critical(
                self, "Import failed",
                "Current entry has no cells — nothing to import.",
            )
            return
        _, max_w, max_h = layout
        n_cells = len(layout[0])
        path, _ = QFileDialog.getOpenFileName(
            self, "Import per-cell PNGs (pick any cell)",
            "", "PNG (*.png)",
        )
        if not path:
            return
        m = re.match(r"^(.*)_cell_(\d+)\.png$", path, re.IGNORECASE)
        if not m:
            QMessageBox.critical(
                self, "Bad filename",
                "Per-cell PNGs must follow the pattern "
                "'<name>_cell_<N>.png'.\n"
                f"Got: {os.path.basename(path)}",
            )
            return
        base = m.group(1)
        pngs: List[QImage] = []
        for ci in range(n_cells):
            sibling = f"{base}_cell_{ci}.png"
            if not os.path.exists(sibling):
                QMessageBox.critical(
                    self, "Missing cell PNG",
                    f"Expected file not found:\n{sibling}",
                )
                return
            cell_img = QImage(sibling)
            if cell_img.isNull():
                QMessageBox.critical(
                    self, "Import failed", f"Could not read {sibling}.",
                )
                return
            pngs.append(cell_img)

        chr_8bpp = (bit_depth == 4)
        total_slots = 256 if chr_8bpp else 16

        first_indexed = pngs[0].format() == QImage.Format_Indexed8
        checkbox_on = self._import_pal_with_sheet_cb.isChecked()
        pal_from_plte = (
            first_indexed and checkbox_on and len(pngs[0].colorTable()) >= 2
        )
        pal_from_quant = (not first_indexed) and checkbox_on
        rebuild_palette = pal_from_plte or pal_from_quant
        if rebuild_palette:
            built = build_palette_for_per_cell_import(
                pngs, total_slots=total_slots, max_w=max_w, max_h=max_h,
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
            current_palette, _ = self._current_effective_palette(palettes, bit_depth)
            new_palette = list(current_palette)

        bytes_per_tile = 64 if chr_8bpp else 32
        n_tiles = len(_tile_bytes) // bytes_per_tile
        ctx = CellPngContext(
            ncer=self._cached_ncer,
            tile_bytes=_tile_bytes,
            n_tiles=n_tiles,
            palette=new_palette,
            is_8bpp=chr_8bpp,
        )
        try:
            new_tiles = import_per_cell_to_tiles(
                pngs, ctx=ctx, layout=layout, palette=new_palette,
            )
        except CellPngError as exc:
            QMessageBox.critical(self, exc.title, exc.message)
            return

        try:
            new_ncgr = sprite.build_ncgr_from_template(
                new_tiles, self._chr_pak.entries[ix],
            )
        except ValueError as exc:
            QMessageBox.critical(self, "Build failed", str(exc))
            return
        replacements: List[Tuple[str, int, bytes]] = [
            (SPR_CHR, ix, sprite.compress_rle30(new_ncgr)),
        ]
        if rebuild_palette:
            bank_replacements = self._cells_bank_replacements(
                new_palette, palettes, bit_depth,
            )
            try:
                new_nclr = sprite.build_nclr_from_template(
                    self._pal_pak.entries[ix], bank_replacements,
                )
            except ValueError as exc:
                QMessageBox.critical(self, "Build failed", f"NCLR rebuild: {exc}")
                return
            replacements.append((SPR_PAL, ix, sprite.compress_rle30(new_nclr)))
            desc = f"Import SPR per-cell + palette 0x{ix:04x}"
        else:
            desc = f"Import SPR per-cell 0x{ix:04x}"

        cmd = ReplaceSpriteCommand(
            self._session, replacements,
            description=desc,
            on_change=self._reload_current_entry,
        )
        self._undo_stack.push(cmd)

    # ---- animation viewer (SPR_ANM / NANR) ------------------------------

    def _build_anim_tab(self) -> QWidget:
        """Assemble the standalone Animation preview tab.

        Mirrors the BTCHR animation viewer (sequence picker, play/stop,
        FPS) and extends it with the NANR affine transform: playback
        renders into this tab's own preview surface, applying each frame's
        scale / rotation / translation, and selecting a frame row exposes
        an editor for those values. The tab is disabled for sprites with no
        real animation (see :meth:`_refresh_anim_panel`)."""
        self._anim_timer = QTimer(self)
        self._anim_timer.setInterval(max(1, 1000 // self._anim_fps))
        self._anim_timer.timeout.connect(self._on_anim_tick)

        # Dedicated preview surface — playback no longer borrows the Cells
        # label, so switching tabs never disturbs the composite grid.
        self._anim_label = QLabel("Select an animated sprite.")
        self._anim_label.setAlignment(Qt.AlignCenter)
        self._anim_label.setMinimumSize(256, 256)
        self._anim_scroll = QScrollArea()
        self._anim_scroll.setWidget(self._anim_label)
        self._anim_scroll.setWidgetResizable(True)
        self._anim_scroll.setAlignment(Qt.AlignCenter)

        self._anim_seq_combo = QComboBox()
        self._anim_seq_combo.currentIndexChanged.connect(self._on_anim_seq_changed)
        self._anim_play_btn = QPushButton("▶ Play")
        self._anim_play_btn.setCheckable(True)
        self._anim_play_btn.toggled.connect(self._on_anim_play_toggled)
        self._anim_fps_spin = QSpinBox()
        self._anim_fps_spin.setRange(1, 120)
        self._anim_fps_spin.setValue(self._anim_fps)
        self._anim_fps_spin.setSuffix(" fps")
        self._anim_fps_spin.valueChanged.connect(self._on_anim_fps_changed)

        # Frame list: cell + duration are editable in-cell; the transform
        # summary column is read-only (edit those via the fields below).
        # Selecting a row loads that frame into the transform editor and
        # shows it (transformed) in the preview.
        self._anim_table = QTableWidget(0, 3)
        self._anim_table.setHorizontalHeaderLabels(["Cell", "Duration", "Transform"])
        self._anim_table.verticalHeader().setVisible(False)
        self._anim_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.Stretch
        )
        editable = self._undo_stack is not None
        self._anim_table.setEditTriggers(
            (QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
            if editable else QAbstractItemView.NoEditTriggers
        )
        self._anim_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._anim_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._anim_table.setMinimumHeight(220)
        self._anim_table.itemSelectionChanged.connect(self._on_anim_row_selected)
        self._anim_table.itemChanged.connect(self._on_anim_step_edited)

        # Add / remove frame buttons — sit under the frame list. Remove is
        # gated on a selection and on keeping at least one frame (the codec
        # and engine both expect ≥1 frame per sequence).
        self._anim_add_btn = QPushButton("+ Add frame")
        self._anim_add_btn.clicked.connect(self._on_anim_add_step)
        self._anim_remove_btn = QPushButton("- Remove frame")
        self._anim_remove_btn.clicked.connect(self._on_anim_remove_step)
        self._anim_remove_btn.setEnabled(False)

        # Transform editor — edits the selected frame's SRT. Scale/rotation
        # need an SRT sequence (element 1); translate needs SRT or the
        # translate format (element 2). Widgets enable per the sequence's
        # element in _load_transform_editor. Edits re-encode the whole NANR
        # entry and push a ReplaceSpriteCommand on SPR_ANM.
        self._tf_scale_x = QDoubleSpinBox()
        self._tf_scale_x.setRange(-32.0, 31.999)
        self._tf_scale_x.setSingleStep(0.05)
        self._tf_scale_x.setDecimals(3)
        self._tf_scale_y = QDoubleSpinBox()
        self._tf_scale_y.setRange(-32.0, 31.999)
        self._tf_scale_y.setSingleStep(0.05)
        self._tf_scale_y.setDecimals(3)
        self._tf_rot = QDoubleSpinBox()
        self._tf_rot.setRange(0.0, 359.999)
        self._tf_rot.setSingleStep(1.0)
        self._tf_rot.setDecimals(1)
        self._tf_rot.setSuffix("°")
        self._tf_rot.setWrapping(True)
        self._tf_tx = QSpinBox()
        self._tf_tx.setRange(-512, 511)
        self._tf_ty = QSpinBox()
        self._tf_ty.setRange(-512, 511)
        self._tf_scale_x.valueChanged.connect(lambda _v: self._on_transform_edited())
        self._tf_scale_y.valueChanged.connect(lambda _v: self._on_transform_edited())
        self._tf_rot.valueChanged.connect(lambda _v: self._on_transform_edited())
        self._tf_tx.valueChanged.connect(lambda _v: self._on_transform_edited())
        self._tf_ty.valueChanged.connect(lambda _v: self._on_transform_edited())
        for w in (self._tf_scale_x, self._tf_scale_y, self._tf_rot,
                  self._tf_tx, self._tf_ty):
            w.setMaximumWidth(90)

        # Transform editor as a compact form so it fits the right sidebar.
        # Fields enable per the sequence's element in _load_transform_editor.
        self._tf_row_widget = QWidget()
        tf_form = QFormLayout(self._tf_row_widget)
        tf_form.setContentsMargins(0, 0, 0, 0)
        scale_row = QHBoxLayout()
        scale_row.setContentsMargins(0, 0, 0, 0)
        scale_row.addWidget(self._tf_scale_x)
        scale_row.addWidget(self._tf_scale_y)
        scale_row.addStretch(1)
        scale_host = QWidget()
        scale_host.setLayout(scale_row)
        tf_form.addRow("Scale", scale_host)
        tf_form.addRow("Rotate", self._tf_rot)
        trans_row = QHBoxLayout()
        trans_row.setContentsMargins(0, 0, 0, 0)
        trans_row.addWidget(self._tf_tx)
        trans_row.addWidget(self._tf_ty)
        trans_row.addStretch(1)
        trans_host = QWidget()
        trans_host.setLayout(trans_row)
        tf_form.addRow("Translate", trans_host)
        # Read-only viewers (no undo stack) can't push edits — hide the
        # editor entirely so the panel reads as a pure visualizer.
        if self._undo_stack is None:
            self._tf_row_widget.setVisible(False)

        # "Enable transforms" upgrades an index/translate sequence to the SRT
        # format so scale/rotation/translation become editable. Lossless — the
        # frames keep identity transforms, so the animation looks unchanged
        # until a value is edited. Disabled once the sequence is already SRT.
        self._anim_srt_btn = QPushButton("Enable transforms (SRT)")
        self._anim_srt_btn.setToolTip(wrap_tooltip(
            "Most move sequences store only a cell index per frame — no room "
            "for scale/rotation/translation, so those fields are greyed out. "
            "This converts the sequence to the SRT format (frames start at "
            "identity, so nothing changes visually) and unlocks the transform "
            "fields. Note: whether the transform actually shows in-game "
            "depends on how that sprite is drawn."
        ))
        self._anim_srt_btn.clicked.connect(self._on_enable_srt)
        self._anim_srt_btn.setEnabled(False)

        # Right sidebar: sequence picker, play controls, frame list, the
        # add/remove buttons, and the transform editor — sits beside the
        # preview rather than under it.
        seq_row = QHBoxLayout()
        seq_row.addWidget(QLabel("Sequence:"))
        seq_row.addWidget(self._anim_seq_combo, 1)
        play_row = QHBoxLayout()
        play_row.addWidget(self._anim_play_btn)
        play_row.addWidget(self._anim_fps_spin)
        play_row.addStretch(1)
        step_btn_row = QHBoxLayout()
        step_btn_row.addWidget(self._anim_add_btn)
        step_btn_row.addWidget(self._anim_remove_btn)
        step_btn_row.addStretch(1)
        editor_panel = QWidget()
        editor_panel.setMinimumWidth(360)
        editor_layout = QVBoxLayout(editor_panel)
        editor_layout.setContentsMargins(0, 0, 0, 0)
        editor_layout.addLayout(seq_row)
        editor_layout.addLayout(play_row)
        editor_layout.addWidget(self._anim_table, 1)
        # Read-only viewers can't add/remove frames — hide the buttons.
        if self._undo_stack is not None:
            editor_layout.addLayout(step_btn_row)
            srt_row = QHBoxLayout()
            srt_row.addWidget(self._anim_srt_btn)
            srt_row.addStretch(1)
            editor_layout.addLayout(srt_row)
        editor_layout.addWidget(self._tf_row_widget)

        anim_split = QSplitter(Qt.Horizontal)
        anim_split.addWidget(self._anim_scroll)
        anim_split.addWidget(editor_panel)
        anim_split.setStretchFactor(0, 1)
        anim_split.setStretchFactor(1, 0)
        anim_split.setSizes([440, 400])

        tab = QWidget()
        tab_layout = QVBoxLayout(tab)
        tab_layout.setContentsMargins(8, 8, 8, 8)
        tab_layout.addWidget(anim_split, 1)
        return tab

    def _refresh_anim_panel(self) -> None:
        """Parse the current sprite's NANR, repopulate the sequence picker +
        frame list, and enable/disable the Animation tab.

        The tab is enabled only for sprites that actually animate (a
        sequence with more than one frame). Single-frame stubs (icons /
        portraits / UI) and pose-only sheets leave it disabled. If the
        Animation tab is showing when a non-animated sprite is selected,
        fall back to the Cells tab so the user isn't left on a dead pane."""
        self._anim = None
        if (
            self._current_idx is not None
            and self._current_idx < self._anm_pak.count
        ):
            try:
                self._anim = nanr.parse_nanr(
                    self._anm_pak.entries[self._current_idx]
                )
            except (ValueError, IndexError):
                self._anim = None
        sequences = self._anim.sequences if self._anim else []

        self._anim_seq_combo.blockSignals(True)
        self._anim_seq_combo.clear()
        for si, seq in enumerate(sequences):
            self._anim_seq_combo.addItem(self._seq_label(si, seq), si)
        self._anim_seq_combo.blockSignals(False)

        # Enable the tab when the sprite can show motion: it already
        # animates, or it has ≥2 NCER cells to cycle between — so a modder
        # can build an animation on a currently-static sprite by adding
        # frames to its (single-frame) sequence.
        ncer_obj = getattr(self, "_cached_ncer", None)
        n_cells = len(ncer_obj.cells) if ncer_obj is not None else 0
        can_animate = bool(
            sequences and (self._anim.has_animation or n_cells >= 2)
        )
        was_on_anim_tab = (
            self._preview_tabs.currentIndex() == self._anim_tab_index
        )
        if not can_animate and was_on_anim_tab:
            self._preview_tabs.setCurrentIndex(1)  # fall back to Cells
        self._preview_tabs.setTabEnabled(self._anim_tab_index, can_animate)

        self._anim_seq_idx = 0
        self._anim_pos = 0
        self._anim_editing_frame = -1
        if can_animate:
            self._anim_seq_combo.setCurrentIndex(0)
        self._refresh_anim_table()
        if not can_animate:
            self._anim_label.setText(
                "This sprite has a single cell — nothing to animate between."
                if n_cells < 2 else "This sprite has no animation."
            )
        elif self._preview_tabs.currentIndex() == self._anim_tab_index:
            self._show_current_anim_frame_static()

    @staticmethod
    def _seq_label(si: int, seq: nanr.NanrSequence) -> str:
        n = len(seq.frames)
        mode = "loop" if seq.loops else "once"
        tag = ""
        if seq.element == nanr.ELEMENT_SRT:
            tag = " · SRT"
        elif seq.element == nanr.ELEMENT_INDEX_T:
            tag = " · move"
        return f"Seq {si} — {n} frame{'s' if n != 1 else ''} ({mode}){tag}"

    @staticmethod
    def _transform_summary(fr: nanr.NanrFrame) -> str:
        if fr.is_identity:
            return "—"
        parts: List[str] = []
        if fr.scale_x != nanr.SCALE_ONE or fr.scale_y != nanr.SCALE_ONE:
            if fr.scale_x == fr.scale_y:
                parts.append(f"scale {fr.scale_x_f:.2f}")
            else:
                parts.append(f"scale {fr.scale_x_f:.2f},{fr.scale_y_f:.2f}")
        if fr.rot:
            parts.append(f"rot {fr.rotation_deg:.0f}°")
        if fr.trans_x or fr.trans_y:
            parts.append(f"move {fr.trans_x},{fr.trans_y}")
        return "  ".join(parts)

    def _current_sequence(self) -> Optional[nanr.NanrSequence]:
        if self._anim is None:
            return None
        if not (0 <= self._anim_seq_idx < len(self._anim.sequences)):
            return None
        return self._anim.sequences[self._anim_seq_idx]

    def _refresh_anim_table(self) -> None:
        seq = self._current_sequence()
        frames = seq.frames if seq else []
        self._anim_table_loading = True
        self._anim_table.blockSignals(True)
        self._anim_table.setRowCount(len(frames))
        for r, fr in enumerate(frames):
            cell_item = QTableWidgetItem(str(fr.cell))
            dur_item = QTableWidgetItem(str(fr.duration))
            tf_item = QTableWidgetItem(self._transform_summary(fr))
            # Transform column is read-only — edit it via the fields below.
            tf_item.setFlags(tf_item.flags() & ~Qt.ItemIsEditable)
            self._anim_table.setItem(r, 0, cell_item)
            self._anim_table.setItem(r, 1, dur_item)
            self._anim_table.setItem(r, 2, tf_item)
        self._anim_table.blockSignals(False)
        self._anim_table_loading = False
        self._recompute_anim_flat()
        self._compute_anim_canvas()
        self._load_transform_editor()
        self._update_anim_step_buttons()

    def _recompute_anim_flat(self) -> None:
        """Expand the active sequence into a per-tick frame list. Resets
        ``_anim_pos`` if it now points past the new length so playback
        keeps a valid index after a sequence switch."""
        seq = self._current_sequence()
        self._anim_flat = nanr.flatten_sequence(seq) if seq else []
        if self._anim_pos >= len(self._anim_flat):
            self._anim_pos = 0

    # ---- transform geometry ---------------------------------------------

    def _base_sprite_size(self) -> Optional[Tuple[int, int]]:
        """The union slot size every cell renders at (max cell bbox)."""
        layout = self._cell_layout()
        if layout is None:
            return None
        _, max_w, max_h = layout
        if max_w <= 0 or max_h <= 0:
            return None
        return max_w, max_h

    @staticmethod
    def _frame_transform(fr: nanr.NanrFrame, w: int, h: int) -> QTransform:
        """Affine for one frame: scale + rotate about the sprite centre,
        then translate. Matches the NDS OAM-affine convention closely
        enough for a faithful preview (the cell fills its slot, so the
        slot centre ≈ the sprite centre)."""
        t = QTransform()
        t.translate(w / 2.0 + fr.trans_x, h / 2.0 + fr.trans_y)
        t.rotate(fr.rotation_deg)
        t.scale(fr.scale_x_f, fr.scale_y_f)
        t.translate(-w / 2.0, -h / 2.0)
        return t

    def _compute_anim_canvas(self) -> None:
        """Size a shared canvas that fits every frame's transformed bounds.

        Keeps the preview stable across ticks (a shrinking egg or a sliding
        projectile doesn't resize the label each frame). ``None`` for a
        sequence with no transform, so those fall back to the plain cell
        render."""
        self._anim_canvas = None
        seq = self._current_sequence()
        size = self._base_sprite_size()
        if seq is None or size is None:
            return
        if all(fr.is_identity for fr in seq.frames):
            return
        w, h = size
        minx = miny = math.inf
        maxx = maxy = -math.inf
        corners = ((0, 0), (w, 0), (0, h), (w, h))
        for fr in seq.frames:
            t = self._frame_transform(fr, w, h)
            for cx, cy in corners:
                px, py = t.map(float(cx), float(cy))
                minx, miny = min(minx, px), min(miny, py)
                maxx, maxy = max(maxx, px), max(maxy, py)
        ox, oy = math.floor(minx), math.floor(miny)
        cw = max(1, math.ceil(maxx) - ox)
        ch = max(1, math.ceil(maxy) - oy)
        self._anim_canvas = (ox, oy, cw, ch)

    def _on_anim_seq_changed(self, _idx: int) -> None:
        data = self._anim_seq_combo.currentData()
        self._anim_seq_idx = int(data) if data is not None else 0
        self._anim_pos = 0
        self._anim_editing_frame = -1
        self._refresh_anim_table()
        if self._anim_timer.isActive() and self._anim_flat:
            self._show_anim_frame(self._anim_flat[0])

    def _on_anim_fps_changed(self, fps: int) -> None:
        self._anim_fps = fps
        self._anim_timer.setInterval(max(1, 1000 // fps))

    def _on_anim_play_toggled(self, checked: bool) -> None:
        if not checked:
            self._stop_anim_playback()
            return
        if not self._anim_flat:
            self._recompute_anim_flat()
        if not self._anim_flat or self._cell_ctx() is None:
            self._anim_play_btn.setChecked(False)
            return
        self._anim_play_btn.setText("■ Stop")
        self._anim_pos = 0
        self._show_anim_frame(self._anim_flat[0])
        self._anim_timer.start()

    def _stop_anim_playback(self) -> None:
        """Halt the timer and reset the play button. The Animation tab has
        its own preview surface, so nothing else needs restoring — the
        last-shown frame simply stays put."""
        if self._anim_timer.isActive():
            self._anim_timer.stop()
        self._anim_play_btn.blockSignals(True)
        self._anim_play_btn.setChecked(False)
        self._anim_play_btn.setText("▶ Play")
        self._anim_play_btn.blockSignals(False)

    def _on_anim_tick(self) -> None:
        if not self._anim_flat:
            return
        self._anim_pos = (self._anim_pos + 1) % len(self._anim_flat)
        self._show_anim_frame(self._anim_flat[self._anim_pos])

    def _show_current_anim_frame_static(self) -> None:
        """Paint the selected (or first) frame into the Animation preview
        without starting playback — used when the tab becomes visible or a
        new sprite is selected while the tab is open."""
        if self._anim_timer.isActive():
            return
        seq = self._current_sequence()
        if seq is None or not seq.frames:
            return
        idx = self._anim_editing_frame if self._anim_editing_frame >= 0 else 0
        idx = max(0, min(idx, len(seq.frames) - 1))
        self._show_anim_frame(seq.frames[idx])

    def _show_anim_frame(self, frame: nanr.NanrFrame) -> None:
        """Render one animation frame — its NCER cell with the frame's SRT
        transform applied — into the Animation tab's preview label.

        Index (untransformed) frames draw the cell in place at the union
        slot size, matching the BTCHR viewer. Transformed frames composite
        the cell into the shared canvas so scale / rotation / translation
        show correctly and stay stable across ticks."""
        img = self._render_one_cell_qimage(frame.cell)
        if img is None:
            return
        cell_pm = QPixmap.fromImage(img)
        if self._anim_canvas is None or frame.is_identity:
            pm = cell_pm
        else:
            ox, oy, cw, ch = self._anim_canvas
            canvas = QImage(cw, ch, QImage.Format_ARGB32)
            canvas.fill(0)
            painter = QPainter(canvas)
            painter.translate(-ox, -oy)
            painter.setWorldTransform(
                self._frame_transform(frame, cell_pm.width(), cell_pm.height()),
                combine=True,
            )
            painter.drawPixmap(0, 0, cell_pm)
            painter.end()
            pm = QPixmap.fromImage(canvas)
        if max(pm.width(), pm.height()) < 256:
            pm = pm.scaled(
                pm.width() * 2, pm.height() * 2,
                Qt.KeepAspectRatio, Qt.FastTransformation,
            )
        self._anim_label.setPixmap(pm)
        self._anim_label.setMinimumSize(pm.size())

    # ---- transform editing ----------------------------------------------

    def _on_anim_row_selected(self) -> None:
        rows = self._anim_table.selectionModel().selectedRows()
        self._anim_editing_frame = rows[0].row() if rows else -1
        self._load_transform_editor()
        self._update_anim_step_buttons()
        # Show the selected frame (transformed) when not playing so edits
        # are visible immediately.
        if not self._anim_timer.isActive():
            seq = self._current_sequence()
            if seq and 0 <= self._anim_editing_frame < len(seq.frames):
                self._show_anim_frame(seq.frames[self._anim_editing_frame])

    def _update_anim_step_buttons(self) -> None:
        seq = self._current_sequence()
        editable = self._undo_stack is not None and seq is not None
        self._anim_add_btn.setEnabled(editable)
        self._anim_remove_btn.setEnabled(
            editable
            and len(seq.frames) > 1
            and 0 <= self._anim_editing_frame < len(seq.frames)
        )
        # "Enable transforms" only makes sense on a non-SRT sequence.
        self._anim_srt_btn.setEnabled(
            editable and seq.element != nanr.ELEMENT_SRT
        )

    def _on_enable_srt(self) -> None:
        """Upgrade the current sequence to the SRT element format so its
        frames can carry scale / rotation / translation. Lossless: existing
        frames keep their (identity, or translate-only) transform values."""
        if self._undo_stack is None or self._current_idx is None:
            return
        seq = self._current_sequence()
        if seq is None or seq.element == nanr.ELEMENT_SRT:
            return
        seq.element = nanr.ELEMENT_SRT
        self._push_anim_change(
            f"Enable transforms (SRT) on SPR 0x{self._current_idx:04x} anim "
            f"seq {self._anim_seq_idx}"
        )
        # Reflect the new element in the sequence picker label + editors.
        if self._anim and 0 <= self._anim_seq_idx < len(self._anim.sequences):
            self._anim_seq_combo.blockSignals(True)
            self._anim_seq_combo.setItemText(
                self._anim_seq_idx,
                self._seq_label(self._anim_seq_idx,
                                self._anim.sequences[self._anim_seq_idx]),
            )
            self._anim_seq_combo.blockSignals(False)
        self._update_anim_step_buttons()
        self._load_transform_editor()

    def _on_anim_step_edited(self, item: QTableWidgetItem) -> None:
        """Apply an in-cell edit of a frame's Cell or Duration. Invalid
        input reverts from the model. The Transform column is read-only."""
        if self._anim_table_loading or self._undo_stack is None:
            return
        if self._current_idx is None:
            return
        seq = self._current_sequence()
        if seq is None:
            return
        row, col = item.row(), item.column()
        if not (0 <= row < len(seq.frames)) or col not in (0, 1):
            return
        fr = seq.frames[row]
        try:
            value = int(item.text())
        except ValueError:
            self._refresh_anim_table()  # silent revert to the model value
            return
        if value < 0:
            self._refresh_anim_table()
            return
        if col == 0:
            ncer_obj = getattr(self, "_cached_ncer", None)
            n_cells = len(ncer_obj.cells) if ncer_obj is not None else None
            if n_cells and value >= n_cells:
                value = n_cells - 1
            if value == fr.cell:
                return
            fr.cell = value
        else:  # duration
            if value > 0xFFFF:
                value = 0xFFFF
            if value == fr.duration:
                return
            fr.duration = value
        self._push_anim_change(
            f"Edit SPR 0x{self._current_idx:04x} anim seq {self._anim_seq_idx} "
            f"frame {row} {'cell' if col == 0 else 'duration'}"
        )

    def _on_anim_add_step(self) -> None:
        """Insert a frame after the selected one (duplicating it), or append
        when nothing is selected."""
        if self._undo_stack is None or self._current_idx is None:
            return
        seq = self._current_sequence()
        if seq is None:
            return
        row = self._anim_editing_frame
        insert_at = row + 1 if 0 <= row < len(seq.frames) else len(seq.frames)
        src = (
            seq.frames[row] if 0 <= row < len(seq.frames)
            else (seq.frames[-1] if seq.frames else None)
        )
        new = nanr.NanrFrame(
            cell=src.cell if src else 0,
            duration=src.duration if src else 4,
            rot=src.rot if src else 0,
            scale_x=src.scale_x if src else nanr.SCALE_ONE,
            scale_y=src.scale_y if src else nanr.SCALE_ONE,
            trans_x=src.trans_x if src else 0,
            trans_y=src.trans_y if src else 0,
        )
        seq.frames.insert(insert_at, new)
        self._anim_editing_frame = insert_at
        self._push_anim_change(
            f"Add SPR 0x{self._current_idx:04x} anim seq {self._anim_seq_idx} frame"
        )

    def _on_anim_remove_step(self) -> None:
        """Delete the selected frame. Guarded to keep at least one frame —
        the codec and engine both need a non-empty sequence."""
        if self._undo_stack is None or self._current_idx is None:
            return
        seq = self._current_sequence()
        if seq is None:
            return
        row = self._anim_editing_frame
        if not (0 <= row < len(seq.frames)) or len(seq.frames) <= 1:
            return
        del seq.frames[row]
        self._anim_editing_frame = min(row, len(seq.frames) - 1)
        self._push_anim_change(
            f"Remove SPR 0x{self._current_idx:04x} anim seq {self._anim_seq_idx} "
            f"frame {row}"
        )

    def _push_anim_change(self, description: str) -> None:
        """Serialize the live ``_anim`` and push it as a SPR_ANM replace."""
        if self._current_idx is None or self._undo_stack is None or self._anim is None:
            return
        try:
            new_nanr = nanr.serialize_nanr(
                self._anim, self._anm_pak.entries[self._current_idx]
            )
        except ValueError as exc:
            QMessageBox.critical(self, "Build failed", f"NANR rebuild: {exc}")
            return
        cmd = ReplaceSpriteCommand(
            self._session,
            [(SPR_ANM, self._current_idx, sprite.compress_rle30(new_nanr))],
            description=description,
            on_change=self._reload_anim_after_edit,
        )
        self._undo_stack.push(cmd)

    def _load_transform_editor(self) -> None:
        """Populate the transform spinboxes from the selected frame and
        enable only the fields the sequence's element format can store."""
        seq = self._current_sequence()
        fr = None
        if seq and 0 <= self._anim_editing_frame < len(seq.frames):
            fr = seq.frames[self._anim_editing_frame]
        srt = bool(seq and seq.element == nanr.ELEMENT_SRT)
        translate = bool(seq and seq.element in (nanr.ELEMENT_SRT, nanr.ELEMENT_INDEX_T))
        self._anim_editor_loading = True
        try:
            self._tf_scale_x.setValue(fr.scale_x_f if fr else 1.0)
            self._tf_scale_y.setValue(fr.scale_y_f if fr else 1.0)
            self._tf_rot.setValue(fr.rotation_deg if fr else 0.0)
            self._tf_tx.setValue(fr.trans_x if fr else 0)
            self._tf_ty.setValue(fr.trans_y if fr else 0)
        finally:
            self._anim_editor_loading = False
        has_frame = fr is not None
        self._tf_scale_x.setEnabled(has_frame and srt)
        self._tf_scale_y.setEnabled(has_frame and srt)
        self._tf_rot.setEnabled(has_frame and srt)
        self._tf_tx.setEnabled(has_frame and translate)
        self._tf_ty.setEnabled(has_frame and translate)

    def _on_transform_edited(self) -> None:
        """Write the edited transform back into the NANR and push it as a
        SPR_ANM replace command."""
        if self._anim_editor_loading or self._undo_stack is None:
            return
        if self._current_idx is None:
            return
        seq = self._current_sequence()
        if seq is None or not (0 <= self._anim_editing_frame < len(seq.frames)):
            return
        fr = seq.frames[self._anim_editing_frame]
        new_scale_x = int(round(self._tf_scale_x.value() * nanr.SCALE_ONE))
        new_scale_y = int(round(self._tf_scale_y.value() * nanr.SCALE_ONE))
        new_rot = int(round(self._tf_rot.value() / 360.0 * nanr.ROT_FULL)) & 0xFFFF
        new_tx = self._tf_tx.value()
        new_ty = self._tf_ty.value()
        if (new_scale_x, new_scale_y, new_rot, new_tx, new_ty) == (
            fr.scale_x, fr.scale_y, fr.rot, fr.trans_x, fr.trans_y
        ):
            return
        fr.scale_x = new_scale_x
        fr.scale_y = new_scale_y
        fr.rot = new_rot
        fr.trans_x = new_tx
        fr.trans_y = new_ty
        self._push_anim_change(
            f"Edit SPR 0x{self._current_idx:04x} anim seq "
            f"{self._anim_seq_idx} frame {self._anim_editing_frame} transform"
        )

    def _reload_anim_after_edit(self) -> None:
        """on_change hook after any frame edit (cell / duration / transform /
        add / remove) — fires on the edit's own redo and on later undo/redo.

        Re-parses the live entry and refreshes the table for the *current*
        sequence, preserving which sequence is shown and the selected frame.
        Same-count edits update the cells in place so the selection and any
        active spinbox survive a run of edits; add/remove rebuilds the rows
        and restores the selection. A blanket ``_refresh_anim_panel`` here
        would reset the sequence to 0 and drop ``_anim_editing_frame``,
        disabling the editors after a single click."""
        if self._current_idx is None:
            return
        try:
            self._anim = nanr.parse_nanr(self._anm_pak.entries[self._current_idx])
        except (ValueError, IndexError):
            self._anim = None
        if self._anim is None or not (
            0 <= self._anim_seq_idx < len(self._anim.sequences)
        ):
            self._refresh_anim_panel()
            return
        seq = self._anim.sequences[self._anim_seq_idx]
        if self._anim_table.rowCount() == len(seq.frames):
            self._anim_table_loading = True
            self._anim_table.blockSignals(True)
            for r, fr in enumerate(seq.frames):
                for col, text in (
                    (0, str(fr.cell)),
                    (1, str(fr.duration)),
                    (2, self._transform_summary(fr)),
                ):
                    item = self._anim_table.item(r, col)
                    if item is not None:
                        item.setText(text)
            self._anim_table.blockSignals(False)
            self._anim_table_loading = False
            self._recompute_anim_flat()
            self._compute_anim_canvas()
            self._load_transform_editor()
            self._update_anim_step_buttons()
            self._show_current_anim_frame_static()
        else:
            # Frame added or removed — rebuild rows, then restore selection.
            keep = self._anim_editing_frame
            self._refresh_anim_table()
            if seq.frames:
                row = keep if 0 <= keep < len(seq.frames) else len(seq.frames) - 1
                self._anim_table.selectRow(row)
            self._show_current_anim_frame_static()

    def aboutToTeardown(self) -> None:
        """Stop the playback timer before the widget is deleted so a
        deferred tick can't fire against a half-torn-down browser."""
        if self._anim_timer.isActive():
            self._anim_timer.stop()

    # ---- borrow palette (scroll another entry's palette, preview, then copy) --

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
        self._borrow_spin.setRange(0, max(0, self._count - 1))
        self._borrow_spin.setToolTip(
            "Preview this sprite under another entry's palette (non-destructive). "
            "\"Use this palette\" copies it in as one undo step."
        )
        self._borrow_spin.valueChanged.connect(self._on_borrow_changed)
        row.addWidget(self._borrow_spin, 1)
        box.addLayout(row)
        # Off by default — icons/portraits carry foreign palettes well; on
        # remaps each slot to the source colour of nearest brightness.
        self._borrow_match_cb = QCheckBox("Match by brightness")
        self._borrow_match_cb.setChecked(False)
        self._borrow_match_cb.setToolTip(
            "Remap each slot to the source colour of nearest brightness so the "
            "shading survives. Off = raw slot-for-slot copy."
        )
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

    def _refresh_live_preview(self) -> None:
        """Re-render whichever preview tab is showing under the current
        ``_preview_palette`` override (batch adjust / borrow). Doesn't touch the
        grid or adjuster selection."""
        if self._preview_tabs.currentIndex() == 1:  # Cells tab
            self._cells_dirty = False
            self._refresh_cells_preview()
        else:
            self._refresh_preview_only(sync_palette=False)

    def _source_palette(self, ix: int) -> Optional[List[Tuple[int, int, int]]]:
        """Displayed palette of entry ``ix`` (flat-256 for 8bpp, else bank 0)."""
        if not (0 <= ix < self._count):
            return None
        try:
            _tb, bit_depth, *_ = sprite.parse_ncgr(
                sprite.maybe_decompress(self._chr_pak.entries[ix])
            )
            palettes, _ = sprite.parse_nclr(
                sprite.maybe_decompress(self._pal_pak.entries[ix])
            )
        except (ValueError, IndexError):
            return None
        if not palettes:
            return None
        if bit_depth == 4 and len(palettes[0]) == 16:
            return [tuple(c) for bank in palettes for c in bank]
        return [tuple(c) for c in palettes[0]]

    def _source_pixel_counts(self, ix: int) -> Optional[List[int]]:
        """Per-slot pixel usage of entry ``ix``'s sprite, indexed to match
        ``_source_palette`` (0..255 for 8bpp, 0..15 nibbles for 4bpp). Weights
        the borrow match toward the source's dominant colours. Cached."""
        if not (0 <= ix < self._count):
            return None
        if ix in self._src_counts_cache:
            return self._src_counts_cache[ix]
        counts: Optional[List[int]] = None
        try:
            tile_bytes, bit_depth, *_ = sprite.parse_ncgr(
                sprite.maybe_decompress(self._chr_pak.entries[ix])
            )
            if bit_depth == 4:  # 8bpp: one byte per pixel
                counts = [0] * 256
                for b in tile_bytes:
                    counts[b] += 1
            else:               # 4bpp: two nibble pixels per byte
                counts = [0] * 16
                for b in tile_bytes:
                    counts[b & 0xF] += 1
                    counts[(b >> 4) & 0xF] += 1
        except (ValueError, IndexError):
            counts = None
        self._src_counts_cache[ix] = counts
        return counts

    def _reset_borrow(self) -> None:
        if self._current_idx is None:
            return
        self._borrow_spin.blockSignals(True)
        self._borrow_spin.setValue(self._current_idx)
        self._borrow_spin.blockSignals(False)
        self._borrow_name.setText("")
        self._borrow_apply_btn.setEnabled(False)

    def _borrowed_palette(self, ix: int) -> Optional[List[Tuple[int, int, int]]]:
        """Entry ``ix``'s palette as it would land on THIS sprite (sized to the
        current displayed palette) — brightness-matched when the toggle is on,
        else a raw slot-for-slot copy."""
        src = self._source_palette(ix)
        cur = self._palette_get()
        if src is None or not cur:
            return None
        if self._borrow_match_cb.isChecked():
            return intensity_matched_palette(
                cur, src, source_counts=self._source_pixel_counts(ix),
            )
        preview = list(cur)
        for i in range(min(len(src), len(preview))):
            preview[i] = src[i]
        return preview

    def _on_borrow_changed(self, ix: int) -> None:
        """Live-preview the current sprite under entry ``ix``'s palette (copying
        into the current sprite's displayed slots). Selecting self clears it."""
        if self._current_idx is None:
            return
        if ix == self._current_idx:
            self._preview_palette = None
            self._borrow_name.setText("")
            self._borrow_apply_btn.setEnabled(False)
        else:
            preview = self._borrowed_palette(ix)
            if preview is None:
                return
            self._preview_palette = preview
            self._borrow_name.setText(f"0x{ix:04x}")
            self._borrow_apply_btn.setEnabled(True)
        self._refresh_live_preview()

    def _on_borrow_apply(self) -> None:
        """Copy the previewed entry's palette into this sprite's NCLR — one undo
        step (bank-aware, via the batch write)."""
        if self._current_idx is None:
            return
        ix = self._borrow_spin.value()
        if ix == self._current_idx:
            return
        pal = self._borrowed_palette(ix)
        if pal is None:
            return
        self._apply_palette_colors({i: c for i, c in enumerate(pal)})
        self._reset_borrow()

    def _reload_current_entry(self) -> None:
        """on_change hook fired from :class:`ReplaceSpriteCommand` after a
        redo or undo. Re-reads the live pak bytes and rebuilds both the
        palette combo (NCLR may have been swapped on the native path) and
        the preview. No-op when no entry is selected (e.g. the browser
        tore down before the command's redo fired)."""
        if self._current_idx is None:
            return
        self._refresh_palette_combo()
        self._refresh_meta_and_preview()

    # ---- add cell (grow into a new animation frame) ---------------------

    def _on_add_cell(self) -> None:
        """Append a new cell that clones the *selected* cell, backed by a
        fresh copy of only that cell's tiles, so it can be edited
        independently. This is how a single-cell sprite gains a frame to
        animate between."""
        if self._current_idx is None or self._undo_stack is None:
            return
        ix = self._current_idx
        cached = getattr(self, "_cached", None)
        ncer_obj = getattr(self, "_cached_ncer", None)
        if cached is None or ncer_obj is None or not ncer_obj.cells:
            QMessageBox.warning(
                self, "Cannot add cell", "This entry has no cell to clone."
            )
            return
        tile_bytes, bit_depth, _palettes, _ib = cached
        bpt = 64 if bit_depth == 4 else 32
        # OAM ``tile`` is a slot index whose unit is ``slot_bytes`` (the NCER
        # boundary for 1D, 32B for 2D).
        slot_bytes = ncer_obj.boundary_bytes if ncer_obj.is_1d else 32
        n_tiles = len(tile_bytes) // bpt
        src_idx = max(0, min(self._cell_view_spin.value(), len(ncer_obj.cells) - 1))
        src_cell = ncer_obj.cells[src_idx]
        if not src_cell.oams:
            QMessageBox.warning(
                self, "Cannot add cell", "The selected cell references no tiles."
            )
            return
        # Copy *only the selected cell's* linear tile range (not the whole
        # bank), then shift the clone's OAM slots to point at the copy. The
        # copy must start on a slot boundary so the shift is a whole number
        # of slots.
        starts = [(o.tile * slot_bytes) // bpt for o in src_cell.oams]
        ends = [(o.tile * slot_bytes) // bpt + o.n_tiles for o in src_cell.oams]
        src_lo, src_hi = min(starts), max(ends)
        min_slot = min(o.tile for o in src_cell.oams)
        max_slot = max(o.tile for o in src_cell.oams)
        align = slot_bytes // math.gcd(bpt, slot_bytes)   # tiles per slot unit
        append_start = ((n_tiles + align - 1) // align) * align
        slot_delta = append_start * bpt // slot_bytes - min_slot
        if max_slot + slot_delta > 0x3FF:
            QMessageBox.warning(
                self, "Sprite too large",
                "Adding a cell would push an OAM tile index past the 10-bit "
                "hardware limit for this sprite. Its tile bank is already "
                "near the maximum.",
            )
            return
        pad = append_start - n_tiles
        new_tiles = (
            tile_bytes + b"\x00" * (pad * bpt)
            + tile_bytes[src_lo * bpt:src_hi * bpt]
        )
        try:
            new_ncgr = sprite.build_ncgr_from_template(
                new_tiles, self._chr_pak.entries[ix]
            )
            new_cel = ncer_mod.append_cloned_cell(
                self._cel_pak.entries[ix], src_idx, slot_delta,
            )
        except (ValueError, IndexError) as exc:
            QMessageBox.critical(self, "Add cell failed", str(exc))
            return
        cmd = ReplaceSpriteCommand(
            self._session,
            [
                (SPR_CHR, ix, sprite.compress_rle30(new_ncgr)),
                (SPR_CEL, ix, sprite.compress_rle30(new_cel)),
            ],
            description=f"Add cell (from cell {src_idx}) to sprite 0x{ix:04x}",
            on_change=self._reload_after_structural_change,
        )
        self._undo_stack.push(cmd)

    def _reload_after_structural_change(self) -> None:
        """on_change after a cell add/undo — the cell count changed, so
        re-parse everything and refresh both previews + the animation
        panel (which gates on cell count)."""
        if self._current_idx is None:
            return
        self._refresh_palette_combo()
        self._refresh_meta_and_preview()
        self._recompute_current_conflicts()
        self._refresh_cell_export_button_states()
        self._cells_dirty = True
        if self._preview_tabs.currentIndex() == 1:
            self._refresh_cells_preview()
        self._refresh_anim_panel()
