"""Menu/UI-background browser + per-tile editor for ``DAT/bg/``.

Lists every UI background in the ROM — the pack-select / window-frame /
status / title BGs, keyed by their NSCR tilemap — with two tabs:

- **Preview** — the rendered background, an Export/Import PNG round-trip
  (whole-layer, re-quantizes the *tiles*), and metadata.
- **Paint** — a Porymap/NitroPaint-style per-tile tilemap editor. Tools:
  Paint (stamp the selected tile), Select (marquee a block and drag it to
  move — Ctrl-drag copies; vacated cells fill with the background tile),
  and Eyedropper (grab an existing cell's tile/bank/flips). A definable
  **background tile** is the fill used when clearing/moving. Edits the
  **NSCR only** (rearranging existing tiles) — lossless, no palette or
  tile-graphics change, and per-screen, so it can't disturb another screen
  that shares this tile bank. One undo step per stroke / move / fill.

Two dropdowns above the tabs pick which ``.NCGR`` (tiles) and ``.NCLR``
(palette) both tabs render against (:func:`digimon_core.bg.discover_bg_records`).
Edits ride the ``bg_edits`` FAT channel; UI-background files are stored
uncompressed on the ROM, so the save path writes them verbatim
(``RomSession._apply_bg_splice``).
"""
from __future__ import annotations

from collections import Counter
from typing import List, Optional, Tuple

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPixmap, QUndoStack
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from digimon_core import bg, btmap
from digimon_core import sprite as sprite_mod

from .paint_canvas import PaintCanvas
from .record_list_panel import RecordListPanel

_ZOOM_MIN = 1
_ZOOM_MAX = 8
_PICKER_PER_ROW = 16
_PICKER_SCALE = 3
_SEL_COLOR = QColor(255, 230, 0)


class BgBrowser(QWidget):
    """Viewer/editor for ``DAT/bg/`` UI backgrounds."""

    _CURSOR_KEY = "bg_browser"
    _TAB_PREVIEW = 0
    _TAB_PAINT = 1

    def __init__(self, session, undo_stack: Optional[QUndoStack] = None, parent=None):
        super().__init__(parent)
        self._session = session
        self._undo_stack = undo_stack
        self._file_table = session.vanilla_file_table()
        self._records: List[bg.BgRecord] = bg.discover_bg_records(self._file_table)
        self._current: Optional[bg.BgRecord] = None
        self._pinned_rgba: bytes = b""

        # ---- Paint-tab state (rebuilt on record/source change) ----------
        self._paint_key: Optional[Tuple[str, str, str]] = None
        self._paint_entries: List[int] = []
        self._paint_tiles: List[bytes] = []
        self._paint_palettes: List[List[Tuple[int, int, int]]] = []
        self._paint_w = 0
        self._paint_h = 0
        self._paint_base: bytearray = bytearray()
        self._paint_scale = 3
        self._paint_sel_tile = 0
        self._paint_bank = 0
        self._paint_hflip = False
        self._paint_vflip = False
        self._paint_pick_mode = False
        self._paint_hover: Optional[Tuple[int, int]] = None
        self._paint_stroke_snapshot: Optional[List[int]] = None
        self._suppress_reload = False

        # Tool + background-tile + selection state.
        self._tool = "paint"                       # "paint" | "select"
        self._bg_entry = 0                          # the definable background/fill tile entry
        self._sel_rect: Optional[Tuple[int, int, int, int]] = None  # (cx0,cy0,cx1,cy1) inclusive
        self._sel_dragging = False
        self._sel_kind: Optional[str] = None        # "define" | "move"
        self._sel_anchor: Tuple[int, int] = (0, 0)
        self._sel_move_start: Tuple[int, int] = (0, 0)
        self._sel_move_delta: Tuple[int, int] = (0, 0)
        self._sel_float: Optional[List[int]] = None  # captured entries over the rect at move start
        self._sel_copy = False

        self._build_ui()
        remembered = self._session.recall_selection(self._CURSOR_KEY)
        target = 0
        if remembered is not None and 0 <= int(remembered) < len(self._records):
            target = int(remembered)
        if not self._list.select_index(target):
            self._list.select_first()

    # ---- UI construction -------------------------------------------------

    def _build_ui(self) -> None:
        self._list = RecordListPanel(
            records=list(self._records),
            label_for=lambda _ix, rec: rec.display_name,
        )
        self._list.indexSelected.connect(self._on_index_selected)

        self._ncgr_combo = QComboBox()
        self._ncgr_combo.setToolTip(
            "Tile bank (.NCGR) this background renders from. Defaults to the"
            " same-stem file; retarget it for a background that reuses another's"
            " tiles."
        )
        self._ncgr_combo.currentIndexChanged.connect(self._on_source_changed)
        self._nclr_combo = QComboBox()
        self._nclr_combo.setToolTip(
            "Palette (.NCLR). Defaults to the same-stem palette, then the _m"
            " main-screen variant when present; a shared palette from elsewhere"
            " can be selected for backgrounds that ship none of their own."
        )
        self._nclr_combo.currentIndexChanged.connect(self._on_source_changed)
        sources_row = QHBoxLayout()
        sources_row.setContentsMargins(0, 0, 0, 0)
        sources_row.addWidget(QLabel("Tiles"))
        sources_row.addWidget(self._ncgr_combo, 1)
        sources_row.addSpacing(8)
        sources_row.addWidget(QLabel("Palette"))
        sources_row.addWidget(self._nclr_combo, 1)

        self._tabs = QTabWidget()
        self._tabs.addTab(self._build_preview_tab(), "Preview")
        self._tabs.addTab(self._build_paint_tab(), "Paint")
        self._tabs.currentChanged.connect(self._on_tab_changed)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addLayout(sources_row)
        right_layout.addWidget(self._tabs, 1)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._list)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([220, 820])

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(splitter)

    def _build_preview_tab(self) -> QWidget:
        self._preview = QLabel("Select a UI background to preview.")
        self._preview.setAlignment(Qt.AlignCenter)
        self._preview.setMinimumSize(256, 256)
        scroll = QScrollArea()
        scroll.setWidget(self._preview)
        scroll.setWidgetResizable(True)
        scroll.setAlignment(Qt.AlignCenter)

        self._export_btn = QToolButton()
        self._export_btn.setText("Export PNG…")
        self._export_btn.setToolTip("Save the rendered background as a PNG (RGBA, native size).")
        self._export_btn.clicked.connect(self._on_export_clicked)
        self._import_btn = QToolButton()
        self._import_btn.setText("Import PNG…")
        self._import_btn.setToolTip(
            "Replace this background from a flat PNG at the native size. Rebuilds"
            " tiles/tilemap/palette (re-quantizes the tile ART — this affects any"
            " OTHER screen sharing this tile bank). For rearranging existing tiles,"
            " use the Paint tab. Writes to the selected .NCGR/.NCLR."
        )
        self._import_btn.setEnabled(self._undo_stack is not None)
        self._import_btn.clicked.connect(self._on_import_clicked)

        self._meta_size = QLabel("—")
        self._meta_tiles = QLabel("—")
        self._meta_palette = QLabel("—")
        meta_form = QFormLayout()
        meta_form.setContentsMargins(0, 0, 0, 0)
        meta_form.addRow("Size", self._meta_size)
        meta_form.addRow("Tiles", self._meta_tiles)
        meta_form.addRow("Palette banks", self._meta_palette)

        actions_row = QHBoxLayout()
        actions_row.setContentsMargins(0, 0, 0, 0)
        actions_row.addWidget(self._export_btn)
        actions_row.addWidget(self._import_btn)
        actions_row.addStretch(1)
        actions_row.addLayout(meta_form)

        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(scroll, 1)
        layout.addLayout(actions_row)
        return page

    def _build_paint_tab(self) -> QWidget:
        self._paint_canvas = PaintCanvas()
        self._paint_canvas.setText("Select a UI background.")
        self._paint_canvas.setAlignment(Qt.AlignCenter)
        self._paint_canvas.setMinimumSize(256, 256)
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

        # Selected-tile preview + flips.
        self._sel_preview = QLabel()
        self._sel_preview.setFixedSize(48, 48)
        self._sel_preview.setAlignment(Qt.AlignCenter)
        self._sel_preview.setStyleSheet("background: #1d1d1d; border: 1px solid #555;")
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

        self._bank_spin = QSpinBox()
        self._bank_spin.setMinimum(0)
        self._bank_spin.setToolTip("Palette bank stamped onto painted tiles.")
        self._bank_spin.valueChanged.connect(self._on_bank_changed)
        bank_row = QHBoxLayout()
        bank_row.setContentsMargins(0, 0, 0, 0)
        bank_row.addWidget(QLabel("Bank"))
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
            " (one-shot)."
        )
        self._pick_btn.toggled.connect(self._on_pick_toggled)
        tool_row = QHBoxLayout()
        tool_row.setContentsMargins(0, 0, 0, 0)
        tool_row.addWidget(self._select_btn)
        tool_row.addWidget(self._pick_btn)
        tool_row.addStretch(1)

        # Background/fill tile — the tile written into cleared / vacated cells.
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
        self._picker_scroll = QScrollArea()
        self._picker_scroll.setWidget(self._picker_canvas)
        self._picker_scroll.setWidgetResizable(False)
        self._picker_scroll.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self._picker_scale = _PICKER_SCALE

        right_col = QWidget()
        right_col.setMinimumWidth(220)
        rc = QVBoxLayout(right_col)
        rc.setContentsMargins(0, 0, 0, 0)
        rc.addLayout(sel_row)
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

        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(splitter)
        return page

    @staticmethod
    def _hline() -> QWidget:
        line = QWidget()
        line.setFixedHeight(1)
        line.setStyleSheet("background: #444;")
        return line

    # ---- Selection / sources --------------------------------------------

    def _on_index_selected(self, ix: int) -> None:
        if not (0 <= ix < len(self._records)):
            return
        self._current = self._records[ix]
        self._session.remember_selection(self._CURSOR_KEY, ix)
        self._populate_source_combos(self._current)
        self._refresh_preview()
        self._paint_key = None
        self._clear_selection()
        if self._tabs.currentIndex() == self._TAB_PAINT:
            self._ensure_paint_state()

    def _populate_source_combos(self, rec: bg.BgRecord) -> None:
        for combo, paths, own in (
            (self._ncgr_combo, rec.ncgrs, rec.own_ncgr),
            (self._nclr_combo, rec.nclrs, rec.own_nclr),
        ):
            combo.blockSignals(True)
            combo.clear()
            for i, p in enumerate(paths):
                if i == own and 0 < own < len(paths):
                    combo.insertSeparator(combo.count())
                combo.addItem(p.rsplit("/", 1)[-1], p)
            if combo.count():
                combo.setCurrentIndex(0)
            combo.setEnabled(combo.count() > 1)
            combo.blockSignals(False)

    def _on_source_changed(self, _ix: int) -> None:
        self._refresh_preview()
        self._paint_key = None
        if self._tabs.currentIndex() == self._TAB_PAINT:
            self._ensure_paint_state()

    def _on_tab_changed(self, ix: int) -> None:
        if ix == self._TAB_PAINT:
            self._ensure_paint_state()

    def _selected_ncgr_path(self) -> Optional[str]:
        return self._ncgr_combo.currentData()

    def _selected_nclr_path(self) -> Optional[str]:
        return self._nclr_combo.currentData()

    # ---- Preview tab -----------------------------------------------------

    def _refresh_preview(self) -> None:
        if self._current is None:
            return
        ncgr_path = self._selected_ncgr_path()
        nclr_path = self._selected_nclr_path()
        if not ncgr_path or not nclr_path:
            self._preview.setPixmap(QPixmap())
            self._preview.setText("(missing tile bank or palette)")
            self._update_metadata(None)
            return
        try:
            preview = btmap.render_single_layer(
                self._session.bg_file_bytes(ncgr_path),
                self._session.bg_file_bytes(self._current.nscr),
                self._session.bg_file_bytes(nclr_path),
                backdrop_opaque=True,
            )
        except (ValueError, KeyError) as e:
            self._preview.setPixmap(QPixmap())
            self._preview.setText(f"Render failed: {e}")
            self._update_metadata(None)
            return
        if preview.width == 0 or preview.height == 0:
            self._preview.setPixmap(QPixmap())
            self._preview.setText("(empty)")
            self._update_metadata(None)
            return
        self._pinned_rgba = preview.rgba
        image = QImage(
            self._pinned_rgba, preview.width, preview.height,
            preview.width * 4, QImage.Format_RGBA8888,
        )
        self._preview.setText("")
        self._preview.setPixmap(QPixmap.fromImage(image))
        self._preview.adjustSize()
        self._update_metadata(preview)

    def _update_metadata(self, preview) -> None:
        if preview is None:
            self._meta_size.setText("—")
            self._meta_tiles.setText("—")
            self._meta_palette.setText("—")
            return
        self._meta_size.setText(f"{preview.width}×{preview.height}")
        self._meta_palette.setText(str(preview.palette_bank_count))
        try:
            tiles, _bd = btmap._ncgr_tiles_as_indices(
                self._session.bg_file_bytes(self._selected_ncgr_path())
            )
            self._meta_tiles.setText(str(len(tiles)))
        except (ValueError, KeyError):
            self._meta_tiles.setText("—")

    def _on_export_clicked(self) -> None:
        if self._current is None:
            return
        ncgr_path = self._selected_ncgr_path()
        nclr_path = self._selected_nclr_path()
        if not ncgr_path or not nclr_path:
            return
        try:
            preview = btmap.render_single_layer(
                self._session.bg_file_bytes(ncgr_path),
                self._session.bg_file_bytes(self._current.nscr),
                self._session.bg_file_bytes(nclr_path),
                backdrop_opaque=True,
            )
        except (ValueError, KeyError) as e:
            QMessageBox.warning(self, "Export failed", str(e))
            return
        png_path, _ = QFileDialog.getSaveFileName(
            self, f"Export {self._current.display_name}",
            f"{self._current.display_name}.png",
            "PNG images (*.png);;All files (*)",
        )
        if not png_path:
            return
        try:
            from PIL import Image
        except ImportError:
            QMessageBox.warning(
                self, "Export failed",
                "Pillow is required to export PNGs. Install it (pip install pillow) and try again.",
            )
            return
        try:
            Image.frombytes("RGBA", (preview.width, preview.height), preview.rgba).save(png_path)
        except OSError as e:
            QMessageBox.warning(self, "Export failed", str(e))
            return
        QMessageBox.information(self, "Export complete", f"Wrote:\n{png_path}")

    def _on_import_clicked(self) -> None:
        if self._current is None or self._undo_stack is None:
            return
        ncgr_path = self._selected_ncgr_path()
        nclr_path = self._selected_nclr_path()
        nscr_path = self._current.nscr
        if not ncgr_path or not nclr_path:
            return
        original_ncgr = self._session.bg_file_bytes(ncgr_path)
        original_nscr = self._session.bg_file_bytes(nscr_path)
        original_nclr = self._session.bg_file_bytes(nclr_path)
        target_w, target_h, _ = btmap.parse_nscr(original_nscr)

        png_path, _ = QFileDialog.getOpenFileName(
            self, f"Import {self._current.display_name}",
            "", "PNG images (*.png);;All files (*)",
        )
        if not png_path:
            return
        try:
            from digimon_core import btmap_import
            result = btmap_import.import_layer_from_png(
                png_path,
                target_width_px=target_w,
                target_height_px=target_h,
                original_ncgr=original_ncgr,
                original_nscr=original_nscr,
                original_nclr=original_nclr,
                palette_bank=0,
                is_transparent_layer=False,
                max_tiles=1024,
                use_multi_bank=True,
                available_banks=None,
            )
        except ImportError:
            QMessageBox.warning(
                self, "Import failed",
                "Pillow + NumPy are required for background import."
                " Install them (pip install pillow numpy) and try again.",
            )
            return
        except (ValueError, KeyError, OSError) as e:
            QMessageBox.warning(self, "Import failed", str(e))
            return

        original_preview = btmap.render_single_layer(
            original_ncgr, original_nscr, original_nclr, backdrop_opaque=True,
        )
        new_preview = btmap.render_single_layer(
            result.new_ncgr, result.new_nscr, result.new_nclr, backdrop_opaque=True,
        )
        from .btmap_browser import _LayerImportPreviewDialog
        dialog = _LayerImportPreviewDialog(
            self, label_word="Background", map_id=self._current.display_name,
            original_preview=original_preview, new_preview=new_preview,
            stats=result.stats,
        )
        if dialog.exec() != dialog.DialogCode.Accepted:
            return

        from editor.commands import ReplaceBgFileCommand
        macro_label = f"Import background {self._current.display_name}"
        self._undo_stack.beginMacro(macro_label)
        try:
            for path, data, kind in (
                (ncgr_path, result.new_ncgr, "NCGR"),
                (nscr_path, result.new_nscr, "NSCR"),
                (nclr_path, result.new_nclr, "NCLR"),
            ):
                self._undo_stack.push(ReplaceBgFileCommand(
                    self._session, path, data, f"{macro_label} ({kind})",
                    on_change=self._on_bg_file_replaced,
                ))
        finally:
            self._undo_stack.endMacro()

    # ---- Paint tab: state build -----------------------------------------

    def _ensure_paint_state(self) -> None:
        if self._current is None:
            return
        ncgr_path = self._selected_ncgr_path()
        nclr_path = self._selected_nclr_path()
        if not ncgr_path or not nclr_path:
            self._paint_canvas.setPixmap(QPixmap())
            self._paint_canvas.setText("(missing tile bank or palette)")
            return
        key = (self._current.nscr, ncgr_path, nclr_path)
        if key == self._paint_key:
            return
        try:
            nscr = self._session.bg_file_bytes(self._current.nscr)
            self._paint_w, self._paint_h, self._paint_entries = btmap.parse_nscr(nscr)
            self._paint_tiles, _bd = btmap._ncgr_tiles_as_indices(
                self._session.bg_file_bytes(ncgr_path)
            )
            self._paint_palettes, _pbd = sprite_mod.parse_nclr(
                self._session.bg_file_bytes(nclr_path)
            )
        except (ValueError, KeyError) as e:
            self._paint_canvas.setPixmap(QPixmap())
            self._paint_canvas.setText(f"Cannot edit: {e}")
            self._paint_key = None
            return
        self._paint_key = key
        self._bank_spin.blockSignals(True)
        self._bank_spin.setMaximum(max(0, len(self._paint_palettes) - 1))
        if self._paint_bank > self._bank_spin.maximum():
            self._paint_bank = 0
        self._bank_spin.setValue(self._paint_bank)
        self._bank_spin.blockSignals(False)
        if self._paint_sel_tile >= len(self._paint_tiles):
            self._paint_sel_tile = 0
        # Default the background/fill tile to the most common entry — almost
        # always the blank backdrop tile.
        self._bg_entry = Counter(self._paint_entries).most_common(1)[0][0] if self._paint_entries else 0
        self._clear_selection()
        self._paint_base = self._compose_full(self._paint_entries)
        self._render_canvas()
        self._render_picker()
        self._render_sel_preview()
        self._render_bg_preview()

    # ---- Paint tab: RGBA composition ------------------------------------

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

    def _render_canvas(self) -> None:
        if not self._paint_base:
            return
        w, h, scale = self._paint_w, self._paint_h, self._paint_scale
        # During a move drag, render a preview buffer (source vacated, block
        # pasted at the destination) so the move is visible before commit.
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
            painter.setPen(QPen(QColor(255, 230, 0, 220), 1))
            painter.drawRect(hx * 8 * scale, hy * 8 * scale, 8 * scale - 1, 8 * scale - 1)
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
        image = QImage(bytes(buf), 8, 8, 8 * 4, QImage.Format_RGBA8888)
        return QPixmap.fromImage(image).scaled(size, size, Qt.KeepAspectRatio, Qt.FastTransformation)

    def _render_sel_preview(self) -> None:
        self._sel_preview.setPixmap(self._entry_swatch(self._sel_entry(), 48))
        flags = []
        if self._paint_hflip:
            flags.append("H")
        if self._paint_vflip:
            flags.append("V")
        suffix = (" · " + "".join(flags) + "-flip") if flags else ""
        self._sel_label.setText(f"Tile {self._paint_sel_tile} · bank {self._paint_bank}{suffix}")

    def _render_bg_preview(self) -> None:
        self._bg_preview.setPixmap(self._entry_swatch(self._bg_entry, 32))
        tile = self._bg_entry & 0x3FF
        bank = (self._bg_entry >> 12) & 0xF
        self._bg_label.setText(f"Background: tile {tile} · bank {bank}")

    def _render_picker(self) -> None:
        n = len(self._paint_tiles)
        if n == 0:
            self._picker_canvas.setText("(no tiles)")
            return
        per_row = _PICKER_PER_ROW
        rows = (n + per_row - 1) // per_row
        w = per_row * 8
        h = rows * 8
        buf = bytearray(w * 4 * h)
        pals = self._paint_palettes
        pal = pals[self._paint_bank] if self._paint_bank < len(pals) else (pals[0] if pals else [(0, 0, 0)])
        for ix, tile in enumerate(self._paint_tiles):
            cx, cy = (ix % per_row) * 8, (ix // per_row) * 8
            for py in range(8):
                for px in range(8):
                    idx = tile[py * 8 + px]
                    r, g, b = pal[idx] if idx < len(pal) else (0, 0, 0)
                    o = ((cy + py) * w + (cx + px)) * 4
                    buf[o] = r
                    buf[o + 1] = g
                    buf[o + 2] = b
                    buf[o + 3] = 255
        image = QImage(bytes(buf), w, h, w * 4, QImage.Format_RGBA8888)
        pm = QPixmap.fromImage(image)
        sc = self._picker_scale
        if sc != 1:
            pm = pm.scaled(w * sc, h * sc, Qt.KeepAspectRatio, Qt.FastTransformation)
        sel = self._paint_sel_tile
        if 0 <= sel < n:
            painter = QPainter(pm)
            painter.setPen(QPen(_SEL_COLOR, 2))
            painter.drawRect((sel % per_row) * 8 * sc, (sel // per_row) * 8 * sc, 8 * sc - 1, 8 * sc - 1)
            painter.end()
        self._picker_canvas.setText("")
        self._picker_canvas.setImageScale(sc)
        self._picker_canvas.setPixmap(pm)
        self._picker_canvas.adjustSize()

    # ---- Paint tab: helpers ---------------------------------------------

    def _sel_entry(self) -> int:
        e = self._paint_sel_tile & 0x3FF
        if self._paint_hflip:
            e |= 0x400
        if self._paint_vflip:
            e |= 0x800
        e |= (self._paint_bank & 0xF) << 12
        return e

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
        if self._undo_stack is None or self._current is None:
            return False
        try:
            template = self._session.bg_file_bytes(self._current.nscr)
            new_nscr = btmap.build_nscr_from_template(
                new_entries, self._paint_w, self._paint_h, template,
            )
        except (ValueError, KeyError) as e:
            QMessageBox.warning(self, "Edit failed", str(e))
            return False
        self._paint_entries = new_entries
        self._paint_base = self._compose_full(new_entries)
        from editor.commands import ReplaceBgFileCommand
        self._suppress_reload = True
        try:
            self._undo_stack.push(ReplaceBgFileCommand(
                self._session, self._current.nscr, new_nscr,
                f"{label} — {self._current.display_name}",
                on_change=self._on_bg_file_replaced,
            ))
        finally:
            self._suppress_reload = False
        return True

    # ---- Paint tab: canvas interaction ----------------------------------

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
        # Paint.
        idx = self._cell_index(x, y)
        if idx is None or self._undo_stack is None:
            return
        if self._paint_stroke_snapshot is None:
            self._paint_stroke_snapshot = list(self._paint_entries)
        new_entry = self._sel_entry()
        if self._paint_entries[idx] == new_entry:
            return
        self._paint_entries[idx] = new_entry
        tw = self._paint_w // 8
        self._blit_cell(self._paint_base, idx % tw, idx // tw, new_entry)
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
                self._render_canvas()  # finalize marquee / cancel a zero-delta move
            return
        # Paint stroke commit.
        snap = self._paint_stroke_snapshot
        self._paint_stroke_snapshot = None
        if snap is None or snap == self._paint_entries:
            return
        entries = self._paint_entries
        if not self._apply_entries(list(entries), "Paint background"):
            # Build failed — revert the in-memory edit to match the ROM.
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
        self._bank_spin.setValue(self._paint_bank)
        self._bank_spin.blockSignals(False)
        self._render_sel_preview()
        self._render_picker()
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
        per_row = _PICKER_PER_ROW
        tile = (y // 8) * per_row + (x // 8)
        if 0 <= tile < len(self._paint_tiles):
            self._paint_sel_tile = tile
            self._render_sel_preview()
            self._render_picker()

    def _on_picker_zoom(self, steps: int) -> None:
        new = max(1, min(8, self._picker_scale + steps))
        if new != self._picker_scale:
            self._picker_scale = new
            self._render_picker()

    # ---- Paint tab: toolbar handlers ------------------------------------

    def _on_flip(self, axis: str, value: bool) -> None:
        if axis == "h":
            self._paint_hflip = value
        else:
            self._paint_vflip = value
        self._render_sel_preview()

    def _on_bank_changed(self, value: int) -> None:
        self._paint_bank = value
        self._render_sel_preview()
        self._render_picker()

    def _on_pick_toggled(self, checked: bool) -> None:
        self._paint_pick_mode = checked
        if checked and self._select_btn.isChecked():
            self._select_btn.setChecked(False)  # eyedropper takes precedence

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

    # ---- Shared: replace-command callback -------------------------------

    def _on_bg_file_replaced(self) -> None:
        """Re-render after a replace command's redo/undo flip (preview always;
        paint state reloaded from the new bytes so undo/redo updates the canvas)."""
        self._refresh_preview()
        if self._suppress_reload:
            return
        self._paint_key = None
        if self._tabs.currentIndex() == self._TAB_PAINT:
            self._ensure_paint_state()
