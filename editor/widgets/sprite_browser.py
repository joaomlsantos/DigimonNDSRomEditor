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

import os
import re
from typing import List, Optional, Tuple

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QBrush, QColor, QImage, QPainter, QPen, QPixmap, QUndoStack
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from digimon_core import ncer as ncer_mod, pak, sprite

from ..commands import AppendPakEntriesCommand, ReplaceSpriteCommand
from ._png_palette import build_palette_from_png
from .cell_export_policy import (
    allow_shared_tile_exports,
    register_change_callback as _register_cell_policy_callback,
)
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


# Per-cell highlight colors for the OAM overlay. RGBA with low alpha so the
# preview stays readable underneath. Cycles when n_cells exceeds the list.
OAM_OVERLAY_COLORS = (
    (255,  80,  80),
    ( 80, 200,  80),
    ( 80, 160, 255),
    (255, 200,  60),
    (220,  80, 220),
    ( 60, 220, 220),
    (255, 140,  40),
    (180, 120, 255),
)


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
        # Sanity: pair heuristic requires equal counts. Don't crash if the
        # ROM is unusual — just clamp browsing to the smallest.
        self._count = min(self._chr_pak.count, self._pal_pak.count, self._cel_pak.count)

        self._current_idx: Optional[int] = None
        self._current_palette_bank: int = 0
        self._current_width_tiles: int = 4
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

        # OAM overlay toggle. When on, the preview is painted with one
        # translucent rectangle per cell highlighting which NCGR tiles the
        # cell's OAMs read from. Tells the user which tile ranges a
        # replacement is load-bearing for (PLAN §11 G).
        self._oam_overlay_check = QCheckBox("Show OAM cells")
        self._oam_overlay_check.toggled.connect(self._on_oam_overlay_toggled)

        controls = QFormLayout()
        controls.addRow("Width (tiles)", self._width_spin)
        controls.addRow("Palette bank", self._palette_combo)
        controls.addRow("Transparent color", transparent_widget)
        controls.addRow("OAM overlay", self._oam_overlay_check)

        # Metadata panel — one row per field, read-only.
        self._meta_tiles = QLabel("—")
        self._meta_bpp = QLabel("—")
        self._meta_palettes = QLabel("—")
        self._meta_cells = QLabel("—")
        self._meta_mapping = QLabel("—")
        self._meta_min_tiles = QLabel("—")
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
            self._meta_chr_size,
        ):
            lbl.setMinimumWidth(worst_width)

        meta_form = QFormLayout()
        meta_form.addRow("NCGR tiles", self._meta_tiles)
        meta_form.addRow("Bit depth", self._meta_bpp)
        meta_form.addRow("Palettes", self._meta_palettes)
        meta_form.addRow("NCER cells", self._meta_cells)
        meta_form.addRow("OBJ mapping", self._meta_mapping)
        meta_form.addRow("Min tiles required", self._meta_min_tiles)
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
        self._export_cells_png_btn = QPushButton("Export cells PNG…")
        self._export_cells_png_btn.clicked.connect(self._on_export_cells_png)
        self._export_per_cell_btn = QPushButton("Export per-cell PNGs…")
        self._export_per_cell_btn.clicked.connect(self._on_export_per_cell_pngs)
        self._import_cells_png_btn = QPushButton("Import cells PNG…")
        self._import_cells_png_btn.clicked.connect(self._on_import_cells_png)
        self._import_per_cell_btn = QPushButton("Import per-cell PNGs…")
        self._import_per_cell_btn.clicked.connect(self._on_import_per_cell_pngs)
        for btn in (
            self._export_png_btn, self._export_native_btn,
            self._replace_png_btn, self._replace_native_btn,
            self._duplicate_entry_btn,
            self._export_cells_png_btn, self._export_per_cell_btn,
            self._import_cells_png_btn, self._import_per_cell_btn,
        ):
            btn.setEnabled(False)
        # Replace requires an undo stack to push onto; without one the
        # widget runs in read-only mode (e.g. embedded in a viewer).
        if self._undo_stack is None:
            self._replace_png_btn.setVisible(False)
            self._import_pal_with_sheet_cb.setVisible(False)
            self._replace_native_btn.setVisible(False)
            self._duplicate_entry_btn.setVisible(False)
            self._import_cells_png_btn.setVisible(False)
            self._import_per_cell_btn.setVisible(False)

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
        cells_footer = QHBoxLayout()
        cells_footer.setContentsMargins(0, 0, 0, 0)
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
        self._preview_tabs.currentChanged.connect(self._on_preview_tab_changed)
        right_layout.addWidget(self._preview_tabs, 1)
        # Action buttons grouped by target format: PNG column (lossy via
        # the engine's palette quantization) on the left, NCGR+NCLR
        # column (lossless engine-native) on the right. Stacking within
        # a column lets buttons share a width — Qt sizes each column to
        # its widest button, so the labels line up flush left/right.
        png_col = QVBoxLayout()
        png_col.setSpacing(4)
        png_col.addWidget(self._export_png_btn)
        png_col.addWidget(self._replace_png_btn)
        png_col.addWidget(self._import_pal_with_sheet_cb)
        png_col.addStretch(1)
        cells_col = QVBoxLayout()
        cells_col.setSpacing(4)
        cells_col.addWidget(self._export_cells_png_btn)
        cells_col.addWidget(self._export_per_cell_btn)
        cells_col.addWidget(self._import_cells_png_btn)
        cells_col.addWidget(self._import_per_cell_btn)
        cells_col.addStretch(1)
        native_col = QVBoxLayout()
        native_col.setSpacing(4)
        native_col.addWidget(self._export_native_btn)
        native_col.addWidget(self._replace_native_btn)
        native_col.addWidget(self._duplicate_entry_btn)
        native_col.addStretch(1)
        controls_row = QHBoxLayout()
        controls_row.addLayout(controls)
        controls_row.addSpacing(16)
        controls_row.addLayout(png_col)
        controls_row.addSpacing(8)
        controls_row.addLayout(cells_col)
        controls_row.addSpacing(8)
        controls_row.addLayout(native_col)
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

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(list_col)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([220, 800])

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
        self._export_png_btn.setEnabled(True)
        self._export_native_btn.setEnabled(True)
        if self._undo_stack is not None:
            self._replace_png_btn.setEnabled(True)
            self._replace_native_btn.setEnabled(True)
            self._duplicate_entry_btn.setEnabled(True)
        self._refresh_palette_combo()
        self._refresh_meta_and_preview()
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
        ctx = self._cell_ctx()
        layout = self._cell_layout()
        if ctx is None or layout is None:
            self._cells_label.setText("(no cells)")
            return
        n_cells = len(ctx.ncer.cells)
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
        # Match the tile preview's 2× zoom for small sprites.
        if max(pm.width(), pm.height()) < 256:
            pm = pm.scaled(
                pm.width() * 2, pm.height() * 2,
                Qt.KeepAspectRatio, Qt.FastTransformation,
            )
        self._cells_label.setPixmap(pm)
        self._cells_label.setMinimumSize(pm.size())

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
                f"{conflicts.shared_tiles} shared tiles: per-cell edits "
                f"may not round-trip cleanly. Prefer Export/Import tiles "
                f"PNG for this sprite."
            )
        else:
            msg = (
                f"{conflicts.shared_tiles} shared tiles: per-cell edits "
                f"disabled. Use Export/Import tiles PNG to edit this sprite."
            )
        self._cells_conflict_label.setText(msg)

    def _refresh_cell_export_button_states(self) -> None:
        """Gate the four cell-mode export/import buttons.

        Buttons stay visible (so users can see they exist) but are
        disabled when the sprite has shared tiles *and* the Edit-menu
        override is off. Selection must also be set, and import buttons
        additionally require an undo stack (read-only mode hides them
        outright, see ``__init__``).
        """
        has_selection = self._current_idx is not None
        conflicts = self._current_conflicts
        blocked_by_conflicts = (
            conflicts is not None
            and conflicts.has_any
            and not allow_shared_tile_exports()
        )
        cells_ok = has_selection and not blocked_by_conflicts
        self._export_cells_png_btn.setEnabled(cells_ok)
        self._export_per_cell_btn.setEnabled(cells_ok)
        if self._undo_stack is not None:
            self._import_cells_png_btn.setEnabled(cells_ok)
            self._import_per_cell_btn.setEnabled(cells_ok)

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
        self._meta_chr_size.setText(
            f"{len(chr_compressed)}B compressed / {len(chr_decompressed)}B raw"
        )

        # Cache parsed payload for fast width/palette changes. parsed_ncer
        # is stashed here too so the OAM overlay can paint without
        # reparsing the NCER on every preview refresh.
        self._cached = (tile_bytes, bit_depth, palettes, is_bitmap)
        self._cached_ncer = parsed_ncer
        self._refresh_preview_only()

    def _refresh_preview_only(self) -> None:
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
            self._paint_oam_overlay(pix, bit_depth, w, h)
        self._image_label.setPixmap(pix)
        # Stash dimensions for click→source-pixel mapping in the picker.
        self._preview_src_size = (w, h)
        self._preview_pixmap_size = (pix.width(), pix.height())
        self._sync_transparent_field()

    # ---- OAM overlay ----------------------------------------------------

    def _on_oam_overlay_toggled(self, checked: bool) -> None:
        self._show_oam_overlay = checked
        self._refresh_preview_only()

    def _paint_oam_overlay(
        self, pix: QPixmap, bit_depth: int, src_w: int, src_h: int,
    ) -> None:
        """Overlay per-cell tile rectangles on the rendered preview.

        Tiles a cell's OAMs occupy in the linear NCGR are translated to
        rectangles in the linear-tile-grid layout the preview uses (so the
        overlay matches what's visible). Each cell gets its own color from
        ``OAM_OVERLAY_COLORS``; the OAM ``tile`` field decoded via
        :func:`ncer.cell_tile_ranges` handles the 8bpp+2D quirk where a
        tile index addresses 32B slots rather than 64B tiles.
        """
        ncer_obj = getattr(self, "_cached_ncer", None)
        if ncer_obj is None or src_w == 0 or src_h == 0:
            return
        bpp4 = (bit_depth == 3)
        ranges_per_cell = ncer_mod.cell_tile_ranges(ncer_obj, bpp4=bpp4)
        if not ranges_per_cell:
            return
        width_tiles = self._current_width_tiles
        if width_tiles <= 0:
            return
        # Skipping the 2× upscale's transform jitter: derive scale from the
        # actual pixmap size rather than assuming `pix == src_w*2`.
        scale_x = pix.width() / src_w
        scale_y = pix.height() / src_h

        painter = QPainter(pix)
        try:
            painter.setRenderHint(QPainter.Antialiasing, False)
            for ci, ranges in enumerate(ranges_per_cell):
                r, g, b = OAM_OVERLAY_COLORS[ci % len(OAM_OVERLAY_COLORS)]
                fill = QColor(r, g, b, 70)
                outline = QColor(r, g, b, 220)
                painter.setBrush(QBrush(fill))
                painter.setPen(QPen(outline, 1))
                for tile_start, tile_end in ranges:
                    # Walk by row segments — a range that crosses a row
                    # boundary in the linear-tile grid renders as one
                    # rectangle per row instead of a single weirdly-shaped
                    # box.
                    cursor = tile_start
                    while cursor < tile_end:
                        row = cursor // width_tiles
                        col = cursor % width_tiles
                        row_end = min(tile_end, (row + 1) * width_tiles)
                        span = row_end - cursor
                        x = int(col * 8 * scale_x)
                        y = int(row * 8 * scale_y)
                        rw = int(span * 8 * scale_x)
                        rh = int(8 * scale_y)
                        painter.drawRect(x, y, rw, rh)
                        cursor = row_end
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
        if checked:
            self._image_label.setCursor(Qt.CrossCursor)
        else:
            self._image_label.unsetCursor()

    def eventFilter(self, obj, event) -> bool:
        if (
            obj is self._image_label
            and self._picking_transparent
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
        rgb = (color.red(), color.green(), color.blue())
        # Turn off picking mode after a successful sample — picking is a
        # one-shot action; users that want another can re-toggle the button.
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

    def _cell_ctx(self) -> Optional[CellPngContext]:
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
            self, "Export cells PNG", default, "PNG (*.png)",
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
            self, "Import cells PNG", "", "PNG (*.png);;All files (*)",
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
