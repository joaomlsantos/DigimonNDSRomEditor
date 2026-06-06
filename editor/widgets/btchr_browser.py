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

from typing import List, Optional, Tuple

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap, QUndoStack, qRgba
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from digimon_core import btchr, fnt, mchr, ncer as ncer_mod, pak, sprite

from ..commands import ReplaceSpriteCommand
from ._png_palette import build_palette_from_png, nearest_idx_opaque
from .record_list_panel import RecordListPanel
from .transparent_picker import TransparentColorPicker


BTCHR_PAK = "DAT/BTCHR.PAK"
CHRSIZE_PATH = "DAT/BTCHR/CHRSIZE.BIN"
PREVIEW_ZOOM = 2  # nearest-neighbor zoom so 64-pixel sprites are visible
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


class BtchrBrowser(QWidget):
    """Read-only browser for BTCHR battle sprites."""

    def __init__(self, session, undo_stack: Optional[QUndoStack] = None, parent=None):
        super().__init__(parent)
        self._session = session
        self._undo_stack = undo_stack
        self._pak: pak.PakFile = session.sprite_pak(BTCHR_PAK)
        self._n_groups = btchr.parse_pak_groups(self._pak)
        self._chrsize_rows = _load_chrsize_rows(session)

        self._current_group: Optional[int] = None
        self._current_decoded: Optional[btchr.BtchrDigimon] = None
        self._current_cell: int = 0
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

        # Cached cell QPixmaps for the *current* digimon. Cleared on
        # selection change.
        self._cell_pixmaps: List[Optional[QPixmap]] = []
        # Sheet preview is lazy — `setPixel` x ~120k tiles is slow in
        # Python, so only re-render when the user actually views the tab.
        self._sheet_dirty: bool = True

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

        self._labels: List[str] = self._build_labels()

        self._build_ui()
        self._list.select_first()

    # ---- labels ---------------------------------------------------------

    def _build_labels(self) -> List[str]:
        out: List[str] = []
        for g in range(self._n_groups):
            digimon_id = (
                self._chrsize_rows[g][0]
                if g < len(self._chrsize_rows) else -1
            )
            tag = " (placeholder)" if g in btchr.SENTINEL_GROUPS else ""
            id_token = f"id={digimon_id:04d}" if digimon_id >= 0 else "id=????"
            out.append(f"{g:04d}  {id_token}{tag}")
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
        self._scroll.setWidgetResizable(True)
        self._scroll.setAlignment(Qt.AlignCenter)

        self._cell_spin = QSpinBox()
        self._cell_spin.setRange(0, 0)
        self._cell_spin.valueChanged.connect(self._on_cell_changed)

        self._show_all_cells = QCheckBox("Show all cells (strip)")
        self._show_all_cells.toggled.connect(self._on_show_all_toggled)

        # Tile-sheet tab widgets.
        self._sheet_preview = QLabel("Select a digimon.")
        self._sheet_preview.setAlignment(Qt.AlignCenter)
        self._sheet_preview.setMinimumSize(320, 320)

        self._sheet_scroll = QScrollArea()
        self._sheet_scroll.setWidget(self._sheet_preview)
        self._sheet_scroll.setWidgetResizable(True)
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
            on_color_picked=self._apply_transparent_color
        )

        self._export_sheet_btn = QPushButton("Export tile sheet PNG…")
        self._export_sheet_btn.clicked.connect(self._on_export_sheet_png)
        self._import_sheet_btn = QPushButton("Import tile sheet PNG…")
        self._import_sheet_btn.clicked.connect(self._on_import_sheet_png)

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

        cells_controls = QFormLayout()
        cells_controls.addRow("Cell", self._cell_spin)
        cells_controls.addRow("", self._show_all_cells)

        # Metadata block — fixed-width labels so switching digimon doesn't
        # cause the layout to reflow.
        self._meta_cells = QLabel("—")
        self._meta_tiles = QLabel("—")
        self._meta_bbox = QLabel("—")
        self._meta_idle = QLabel("—")
        self._meta_attack = QLabel("—")
        self._meta_defend = QLabel("—")
        for lbl in (
            self._meta_cells, self._meta_tiles, self._meta_bbox,
            self._meta_idle, self._meta_attack, self._meta_defend,
        ):
            lbl.setMinimumWidth(280)

        meta_form = QFormLayout()
        meta_form.addRow("Cells", self._meta_cells)
        meta_form.addRow("NCGR tiles", self._meta_tiles)
        meta_form.addRow("Header bbox", self._meta_bbox)
        meta_form.addRow("Idle", self._meta_idle)
        meta_form.addRow("Attack", self._meta_attack)
        meta_form.addRow("Defend", self._meta_defend)

        # ---- Cells tab: preview only ---------------------------------
        # Cell spinner + show-all-cells toggle moved to the actions row
        # below the tabs so they sit alongside the import/export and
        # metadata controls — no per-tab subheader.
        cells_tab = QWidget()
        cells_layout = QVBoxLayout(cells_tab)
        cells_layout.setContentsMargins(8, 8, 8, 8)
        cells_layout.addWidget(self._scroll, 1)

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

        self._tabs = QTabWidget()
        self._tabs.addTab(cells_tab, "Cells")
        self._tabs.addTab(sheet_tab, "Tile sheet")
        self._tabs.currentChanged.connect(self._on_tab_changed)

        # Import/export buttons stay visible regardless of the active tab
        # so the workflow doesn't require remembering which tab hosts
        # which action. Two columns (tile sheet PNG | palette PNG), all
        # four buttons pinned to the widest label so Export/Import line
        # up across columns.
        sheet_btns = (self._export_sheet_btn, self._import_sheet_btn)
        pal_btns = (self._export_pal_btn, self._import_pal_btn)
        max_btn_w = max(
            b.sizeHint().width() for b in sheet_btns + pal_btns
        )
        for b in sheet_btns + pal_btns:
            b.setMinimumWidth(max_btn_w)
        sheet_col = QVBoxLayout()
        sheet_col.setSpacing(4)
        sheet_col.addWidget(self._export_sheet_btn)
        sheet_col.addWidget(self._import_sheet_btn)
        sheet_col.addWidget(self._import_pal_with_sheet_cb)
        sheet_col.addStretch(1)
        pal_col = QVBoxLayout()
        pal_col.setSpacing(4)
        pal_col.addWidget(self._export_pal_btn)
        pal_col.addWidget(self._import_pal_btn)
        pal_col.addStretch(1)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addWidget(self._tabs, 1)

        # Single row under the tabs: cell nav controls (leftmost — the
        # space the picker used to occupy), button columns, metadata,
        # stretch. Picker drops to its own row below so the transparent
        # colour edit sits visually under the empty space left by the
        # nav controls.
        actions_row = QHBoxLayout()
        actions_row.addLayout(cells_controls)
        actions_row.addSpacing(16)
        actions_row.addLayout(sheet_col)
        actions_row.addSpacing(16)
        actions_row.addLayout(pal_col)
        actions_row.addSpacing(16)
        actions_row.addLayout(meta_form)
        actions_row.addStretch(1)
        right_layout.addLayout(actions_row)

        right_layout.addWidget(self._picker)


        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._list)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([240, 800])

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(splitter)

    # ---- selection / refresh -------------------------------------------

    def _on_group_selected(self, g: int) -> None:
        self._current_group = g
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
        self._cell_spin.blockSignals(True)
        self._cell_spin.setRange(0, max(0, len(d.ncer.cells) - 1))
        self._cell_spin.setValue(0)
        self._cell_spin.blockSignals(False)
        self._current_cell = 0

        h = d.header
        self._meta_cells.setText(str(len(d.ncer.cells)))
        self._meta_tiles.setText(f"{d.n_tiles} tiles (8bpp)")
        self._meta_bbox.setText(
            f"scale={h.footprint_scale}  "
            f"y₁={h.y_pivot_a:+d}  x={h.x_pivot:+d}  y₂={h.y_pivot_b:+d}"
        )
        self._meta_idle.setText(_format_track(h.idle))
        self._meta_attack.setText(_format_track(h.attack))
        self._meta_defend.setText(_format_track(h.defend))

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
        self._refresh_preview()
        if self._tabs.currentIndex() == 1:
            self._refresh_sheet_preview()
            self._sheet_dirty = False
        self._picker.set_current_color(self._current_decoded.palette[0])

    def _on_cell_changed(self, value: int) -> None:
        self._current_cell = value
        self._refresh_preview()

    def _on_show_all_toggled(self, checked: bool) -> None:
        self._cell_spin.setEnabled(not checked)
        self._refresh_preview()

    # ---- rendering -----------------------------------------------------

    def _cell_pixmap(self, cell_idx: int) -> Optional[QPixmap]:
        """Render + memoize one cell of the current digimon."""
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
        rgba, w, h = btchr.render_cell_rgba(
            d.ncer.cells[cell_idx], d.tile_bytes, d.palette,
            boundary_bytes=d.ncer.boundary_bytes,
        )
        img = QImage(rgba, w, h, w * 4, QImage.Format_RGBA8888).copy()
        pm = QPixmap.fromImage(img)
        self._cell_pixmaps[cell_idx] = pm
        return pm

    def _refresh_preview(self) -> None:
        if self._current_decoded is None:
            self._cells_src_qimage = None
            return
        if self._show_all_cells.isChecked():
            pm = self._build_all_cells_strip()
        else:
            pm = self._cell_pixmap(self._current_cell)
        if pm is None or pm.isNull():
            self._preview.setText("(empty)")
            self._cells_src_qimage = None
            return
        scaled = pm.scaled(
            pm.width() * PREVIEW_ZOOM, pm.height() * PREVIEW_ZOOM,
            Qt.KeepAspectRatio, Qt.FastTransformation,
        )
        self._preview.setPixmap(scaled)
        # Force the QScrollArea to honor the pixmap's size so a wide
        # "show all cells" strip gets a horizontal scroll bar instead of
        # being silently clipped.
        self._preview.setMinimumSize(scaled.size())
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

    def _cell_layout(self) -> Optional[Tuple[List[Tuple[int, int, int, int]], int, int]]:
        """Cell-mode layout: list of ``(xmin, ymin, w, h)`` per cell + the
        per-slot ``(max_w, max_h)`` used for uniform placement.

        Returns ``None`` when the current digimon has no cells or every
        cell's bbox collapses to zero (sentinel groups, decode error).
        Centralised because export, preview, and import all need the
        same numbers to agree.
        """
        if self._current_decoded is None:
            return None
        d = self._current_decoded
        if not d.ncer.cells:
            return None
        rects: List[Tuple[int, int, int, int]] = []
        max_w = max_h = 0
        for cell in d.ncer.cells:
            xmin, ymin, xmax, ymax = btchr.cell_bbox(cell)
            w = xmax - xmin
            h = ymax - ymin
            rects.append((xmin, ymin, w, h))
            if w > max_w:
                max_w = w
            if h > max_h:
                max_h = h
        if max_w <= 0 or max_h <= 0:
            return None
        return rects, max_w, max_h

    def _render_cells_qimage(self, columns: int) -> Optional[QImage]:
        """Compose each NCER cell into one Indexed8 slot, ``columns`` slots
        per row. Slot size = ``(max_cell_w, max_cell_h)`` so the grid is
        uniform and round-trip slicing on import is deterministic. Each
        cell sits at the top-left of its slot (origin = its xmin/ymin)
        — fixes the cell's pixel positions to a known offset.

        Indexed8 + 256-colour table mirrors the raw-tiles export, so PNG
        round-trips through Aseprite/GIMP without losing the palette.
        """
        if self._current_decoded is None:
            return None
        d = self._current_decoded
        layout = self._cell_layout()
        if layout is None:
            return None
        rects, max_w, max_h = layout
        n_cells = len(d.ncer.cells)
        columns = max(1, min(columns, n_cells))
        rows = (n_cells + columns - 1) // columns

        img = QImage(max_w * columns, max_h * rows, QImage.Format_Indexed8)
        ctable = []
        for pi, (r, g, b) in enumerate(d.palette):
            ctable.append(qRgba(r, g, b, 0 if pi == 0 else 255))
        img.setColorTable(ctable)
        img.fill(0)

        tile_mult = d.ncer.boundary_bytes // btchr.BYTES_PER_TILE_8BPP
        n_tiles = d.n_tiles
        img_w = img.width()
        img_h = img.height()

        for ci, cell in enumerate(d.ncer.cells):
            col = ci % columns
            row = ci // columns
            slot_x = col * max_w
            slot_y = row * max_h
            xmin, ymin, _, _ = rects[ci]
            for o in cell.oams:
                first_tile = o.tile * tile_mult
                ox = o.x - xmin
                oy = o.y - ymin
                ntw = o.w // 8
                nth = o.h // 8
                for ty in range(nth):
                    for tx in range(ntw):
                        idx = first_tile + ty * ntw + tx
                        if idx >= n_tiles:
                            continue
                        tile_off = idx * btchr.BYTES_PER_TILE_8BPP
                        for r in range(8):
                            sr = (7 - r) if o.vflip else r
                            src_row = tile_off + sr * 8
                            dst_y = slot_y + oy + ty * 8 + r
                            if not (0 <= dst_y < img_h):
                                continue
                            for c in range(8):
                                sc = (7 - c) if o.hflip else c
                                pi = d.tile_bytes[src_row + sc]
                                if pi == 0:
                                    continue
                                dst_x = slot_x + ox + tx * 8 + c
                                if not (0 <= dst_x < img_w):
                                    continue
                                img.setPixel(dst_x, dst_y, pi)
        return img

    def _refresh_sheet_preview(self) -> None:
        if self._current_decoded is None:
            self._sheet_preview.setText("Select a digimon.")
            self._sheet_src_qimage = None
            return
        if self._view_mode_combo.currentData() == "cells":
            img = self._render_cells_qimage(self._sheet_columns_spin.value())
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
        self._sheet_preview.setMinimumSize(scaled.size())
        # Cache the native Indexed8 source — sampling reads RGB through
        # `pixelColor` regardless of source format, so this works the same
        # as the RGBA cells preview.
        self._sheet_src_qimage = img
        self._sheet_src_size = (pm.width(), pm.height())
        self._sheet_pix_size = (scaled.width(), scaled.height())

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

    # ---- tile sheet PNG ------------------------------------------------

    def _on_export_sheet_png(self) -> None:
        if self._current_decoded is None or self._current_group is None:
            return
        n_tiles = self._current_decoded.n_tiles
        if n_tiles == 0:
            QMessageBox.critical(self, "Export failed", "Sprite has no tiles.")
            return
        suggested = f"btchr_chr_{self._current_group:04d}.png"
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
            desc = f"Import BTCHR tile sheet + palette {group:04d}"
        else:
            desc = f"Import BTCHR tile sheet {group:04d}"

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

        The PNG is sliced into the same uniform grid the cell exporter
        produced (slot size = max bbox over all cells), and for each cell
        every OAM's pixel rectangle is decoded straight into its target
        tile range using the inverse of render_cell_rgba's flip + tile
        formulas.

        Tiles not referenced by any OAM are preserved from the live tile
        bytes so editing one cell doesn't accidentally wipe storage that
        only other (possibly never-visualised) sub-bank tiles use. Tiles
        referenced by multiple OAMs are last-write-wins — matching the
        engine's behaviour where shared tiles render identically wherever
        they're sampled.
        """
        layout = self._cell_layout()
        if layout is None:
            QMessageBox.critical(
                self, "Import failed",
                "Current digimon has no cells — nothing to import.",
            )
            return
        rects, max_w, max_h = layout
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
        rows = (n_cells + columns - 1) // columns
        expected_w = max_w * columns
        expected_h = max_h * rows
        if img.width() != expected_w or img.height() != expected_h:
            QMessageBox.critical(
                self, "Bad image size",
                f"Cells PNG should be {expected_w}×{expected_h} for "
                f"{n_cells} cells in {columns} columns; got "
                f"{img.width()}×{img.height()}.",
            )
            return

        use_indexed = img.format() == QImage.Format_Indexed8
        if not use_indexed:
            img = img.convertToFormat(QImage.Format_RGBA8888)

        # Same palette-source decision as the raw-tiles importer — see
        # the comment block there for the full rationale.
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

        # Start from the existing tile bytes so cells with overlapping
        # tiles and any tiles outside every OAM keep their old contents.
        new_tiles = bytearray(d.tile_bytes)
        n_tiles = d.n_tiles
        tile_mult = d.ncer.boundary_bytes // btchr.BYTES_PER_TILE_8BPP

        for ci, cell in enumerate(d.ncer.cells):
            col = ci % columns
            row = ci // columns
            slot_x = col * max_w
            slot_y = row * max_h
            xmin, ymin, _, _ = rects[ci]
            for o in cell.oams:
                first_tile = o.tile * tile_mult
                ox = o.x - xmin
                oy = o.y - ymin
                ntw = o.w // 8
                nth = o.h // 8
                for ty in range(nth):
                    for tx in range(ntw):
                        tile_idx = first_tile + ty * ntw + tx
                        if tile_idx >= n_tiles:
                            continue
                        tile_off = tile_idx * btchr.BYTES_PER_TILE_8BPP
                        for r in range(8):
                            sr = (7 - r) if o.vflip else r
                            dst_y = slot_y + oy + ty * 8 + r
                            if not (0 <= dst_y < img.height()):
                                continue
                            for c in range(8):
                                sc = (7 - c) if o.hflip else c
                                dst_x = slot_x + ox + tx * 8 + c
                                if not (0 <= dst_x < img.width()):
                                    continue
                                if use_indexed:
                                    idx = img.pixelIndex(dst_x, dst_y)
                                else:
                                    color = img.pixelColor(dst_x, dst_y)
                                    if color.alpha() < 128:
                                        idx = 0
                                    else:
                                        idx = nearest_idx_opaque(
                                            color.red(), color.green(),
                                            color.blue(), new_palette,
                                        )
                                new_tiles[tile_off + sr * 8 + sc] = idx & 0xFF

        group = self._current_group
        orig_ncgr_raw = sprite.decompress_rle30(
            self._pak.entries[self._ncgr_entry_idx(group)]
        )
        new_ncgr = sprite.build_ncgr_from_template(
            bytes(new_tiles), orig_ncgr_raw,
        )
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
            desc = f"Import BTCHR cells + palette {group:04d}"
        else:
            desc = f"Import BTCHR cells {group:04d}"

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

    # ---- palette PNG ---------------------------------------------------

    def _on_export_palette_png(self) -> None:
        if self._current_decoded is None or self._current_group is None:
            return
        palette = self._current_decoded.palette
        suggested = f"btchr_pal_{self._current_group:04d}.png"
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
            description=f"Import BTCHR palette {group:04d}",
            on_change=self._refresh_after_pak_change,
        )
        if self._undo_stack is not None:
            self._undo_stack.push(cmd)
        else:
            cmd.redo()

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
            description=f"Set transparent color for BTCHR {group:04d}",
            on_change=self._refresh_after_pak_change,
        )
        if self._undo_stack is not None:
            self._undo_stack.push(cmd)
        else:
            cmd.redo()

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
        self._refresh_preview()
        if self._tabs.currentIndex() == 1:
            self._refresh_sheet_preview()
            self._sheet_dirty = False
        self._picker.set_current_color(self._current_decoded.palette[0])

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


def _format_track(track) -> str:
    """One-line readable summary of an animation track."""
    if not track:
        return "—"
    total = sum(s.duration for s in track)
    seq = " → ".join(f"c{s.cell}×{s.duration}" for s in track)
    return f"{total}f: {seq}"
