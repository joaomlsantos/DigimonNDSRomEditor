"""Read-only battle-background browser (PLAN.md §14.4 Phase B).

Lists every btmap id present in the ROM and previews the static
two-layer composite plus each separately-editable surface in its own
tab. Mirrors the battle-sprite browser's tab structure (``Cells`` /
``Tile sheet``) so the graphics editors share one visual idiom.

Tabs:

- **Composite** — A + B cropped to A's footprint (matches in-game).
- **Layer A** — A's NCGR+NSCR standalone, palette index 0 painted
  opaque so the tilemap is fully legible.
- **Layer B** — B standalone, full native extent (often 512×512 vs
  A's 512×256). Disabled when the map ships no B.
- **Animations** — animated composite. Frame/sub-frame controls
  splice NaXc tiles into Layer A's bank and re-render the composite
  in place. Maps whose NaXn schema isn't decoded (small-overlay /
  off-by-N variants — see :func:`btmap.parse_naxn`) fall back to
  the raw NaXc tile-sheet view. Disabled when no animation frames
  exist for the map.

Under the tabs sits a single actions row carrying nav controls and a
compact metadata form (mirrors the mchr/btchr layout). Components
(per-FAT-file kind + size) are surfaced via tooltip on the metadata
block to keep the main view dominated by the preview, not by file
listings.
"""
from __future__ import annotations

import os
from typing import List, Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QImage, QPixmap, QUndoStack
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSplitter,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from digimon_core import btmap

from .flow_layout import FlowLayout, make_height_for_width
from .form_helpers import build_editor_footer, io_button_column
from .record_list_panel import RecordListPanel
from .tilemap_paint import PaintContext, TilemapPaintTab
from .transparent_picker import TransparentColorPicker


class _BtmapPaintProvider:
    """Feeds :class:`TilemapPaintTab` the selected battle-background layer's
    Nitro trio and builds ``ReplaceBtmapFileCommand`` for NSCR edits. Layer B
    shares Layer A's ``ap`` palette (per-bank NCLR)."""

    def __init__(self, browser: "BtmapBrowser"):
        self._b = browser

    def _paths(self):
        b = self._b
        mid = b._current_id
        if mid is None:
            return None
        layer = b._paint_layer
        if layer == "b" and f"DAT/btmap/{mid}bs" not in b._file_table:
            layer = "a"
        if layer == "a":
            return (f"DAT/btmap/{mid}ac", f"DAT/btmap/{mid}as",
                    f"DAT/btmap/{mid}ap", "A")
        return (f"DAT/btmap/{mid}bc", f"DAT/btmap/{mid}bs",
                f"DAT/btmap/{mid}ap", "B")

    def paint_context(self):
        b = self._b
        paths = self._paths()
        if paths is None:
            return None
        ncgr_p, nscr_p, nclr_p, layer = paths
        try:
            return PaintContext(
                ncgr=b._session.btmap_file_bytes(ncgr_p),
                nscr=b._session.btmap_file_bytes(nscr_p),
                nclr=b._session.btmap_file_bytes(nclr_p),
                nscr_path=nscr_p,
                key=(nscr_p, ncgr_p, nclr_p),
                name=f"{b._current_id} layer {layer}",
            )
        except (ValueError, KeyError):
            return None

    def make_nscr_command(self, nscr_path, new_bytes, label, on_change):
        from editor.commands import ReplaceBtmapFileCommand
        return ReplaceBtmapFileCommand(
            self._b._session, nscr_path, new_bytes, label, on_change=on_change)

    def on_external_change(self):
        self._b._on_paint_external_change()


class BtmapBrowser(QWidget):
    """Viewer for ``DAT/btmap/`` battle backgrounds.

    Phase B: read-only preview tabs + per-file metadata. Selection
    state is persisted on the session under the ``btmap_browser``
    cursor key (same convention as the sprite browser).
    """

    _CURSOR_KEY = "btmap_browser"

    # Layer A / Layer B / Composite are no longer separate tabs — they're one
    # "Background" visualizer whose Layers panel toggles each layer's
    # visibility + picks the active edit layer. Animations stays its own tab.
    _TAB_BG = 0
    _TAB_ANIM = 1
    _TAB_PAINT = 2

    _ZOOM_LEVELS = (1, 2, 3, 4)

    def __init__(self, session, undo_stack: Optional[QUndoStack] = None, parent=None):
        super().__init__(parent)
        self._session = session
        self._undo_stack = undo_stack
        self._file_table = session.vanilla_file_table()
        self._map_ids: List[str] = btmap.discover_map_ids(self._file_table)
        self._current_id: Optional[str] = None
        # Pinned RGBA buffers — one per tab. QImage views the bytes
        # directly; if Python frees the buffer the QPixmap pointer is
        # left dangling. Keying by tab so switching tabs while a render
        # is pending doesn't cross-pollute.
        self._pinned_rgba: dict[int, bytes] = {}

        # Animation tab state. Populated when a map is selected; cleared
        # on map switch so the play timer never references stale bytes
        # from the previous map's NaXc/NaXn files.
        self._anim_entries: list[dict] = []  # one per decodable outer frame
        self._anim_frame_ix = 0
        self._anim_sub_ix = 0
        self._anim_timer = QTimer(self)
        self._anim_timer.setSingleShot(True)
        self._anim_timer.timeout.connect(self._on_anim_tick)

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
        self._restore_tab()

    def _restore_tab(self) -> None:
        """Re-open the tab the user last had active (Background/Animations/
        Paint) — the session outlives the editor widget across nav switches."""
        saved = self._session.recall_selection(self._CURSOR_KEY + "_tab")
        if (saved is not None and 0 <= int(saved) < self._tabs.count()
                and self._tabs.isTabEnabled(int(saved))):
            self._tabs.setCurrentIndex(int(saved))

    # ---- UI construction -------------------------------------------------

    def _build_ui(self) -> None:
        self._list = RecordListPanel(
            records=list(self._map_ids),
            label_for=lambda _ix, mid: f"{int(mid):04d}",
        )
        self._list.indexSelected.connect(self._on_index_selected)

        self._tabs = QTabWidget()
        self._tabs.addTab(self._build_bg_tab(), "Background")
        self._tabs.addTab(self._build_anim_tab(), "Animations")
        self._tabs.addTab(self._build_paint_tab(), "Paint")
        self._tabs.currentChanged.connect(self._on_tab_changed)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)
        right_layout.addWidget(self._tabs, 1)
        right_layout.addWidget(self._build_footer(), 0)
        # Sync the PNG-import action's enabled state to the default view now
        # that the footer (which owns the Import menu) exists.
        self._on_view_changed(self._view_mode)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._list)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([220, 820])

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(splitter)

    def _build_bg_tab(self):
        """Unified background visualiser: one canvas showing the selected view
        (Layer A / Layer B / Composite), a controls sidebar, and the per-bank
        backdrop editor along the bottom. Replaces the three static tabs."""
        self._bg_label = QLabel("Select a battle background to preview.")
        self._bg_label.setAlignment(Qt.AlignCenter)
        self._bg_label.setMinimumSize(160, 90)
        self._bg_scroll = QScrollArea()
        self._bg_scroll.setWidget(self._bg_label)
        self._bg_scroll.setWidgetResizable(False)
        self._bg_scroll.setAlignment(Qt.AlignCenter)
        self._layer_a_label = self._bg_label  # backdrop eyedropper alias

        split = QSplitter(Qt.Horizontal)
        split.addWidget(self._bg_scroll)
        split.addWidget(self._build_bg_panel())
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 0)
        split.setCollapsible(0, False)
        split.setSizes([680, 220])

        # The per-bank backdrop swatch editor is hidden for now — it'll be
        # folded into the future palette work. We keep it built + wired
        # (dormant, not deleted) and surface ONLY its transparent-colour
        # picker, which lives in the shared footer. The picker eyedrops the
        # preview, so create it here where _bg_label exists.
        self._layer_a_preview_qimage = None
        self._backdrop_picker = TransparentColorPicker(
            on_color_picked=self._apply_backdrop_color,
        )
        self._backdrop_picker.bind_preview(
            self._bg_label, self._layer_a_preview_source,
        )
        self._backdrop_section = self._build_layer_a_backdrop_section(self._bg_label)
        self._backdrop_section.setVisible(False)

        page = QWidget()
        pl = QVBoxLayout(page)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.setSpacing(0)
        pl.addWidget(split, 1)
        pl.addWidget(self._backdrop_section)  # dormant / hidden
        return page

    def _build_bg_panel(self):
        """Sidebar: just the View selector (which layer / composite you see +
        act on) and Zoom. Details + Import/Export live in the shared footer
        below the tabs, matching the other editors. Scroll-wrapped +
        width-capped to stay small-screen friendly."""
        panel = QWidget()
        pl = QVBoxLayout(panel)
        pl.setContentsMargins(6, 6, 6, 6)
        pl.setSpacing(8)

        view_box = QGroupBox("View")
        vb = QVBoxLayout(view_box)
        vb.setContentsMargins(8, 6, 8, 6)
        vb.setSpacing(2)
        self._view_group = QButtonGroup(self)
        self._view_a = QRadioButton("Layer A")
        self._view_b = QRadioButton("Layer B")
        self._view_comp = QRadioButton("Composite")
        for rb, key in ((self._view_a, "a"), (self._view_b, "b"),
                        (self._view_comp, "composite")):
            self._view_group.addButton(rb)
            rb.toggled.connect(
                lambda checked, k=key: self._on_view_changed(k) if checked else None)
            vb.addWidget(rb)
        self._view_mode = "a"
        self._view_a.setChecked(True)
        pl.addWidget(view_box)

        zoom_row = QHBoxLayout()
        zoom_row.setContentsMargins(0, 0, 0, 0)
        zoom_row.addWidget(QLabel("Zoom"))
        self._zoom_combo = QComboBox()
        for z in self._ZOOM_LEVELS:
            self._zoom_combo.addItem(f"{z}×", z)
        self._zoom_combo.currentIndexChanged.connect(self._on_zoom_changed)
        zoom_row.addWidget(self._zoom_combo)
        zoom_row.addStretch(1)
        pl.addLayout(zoom_row)

        pl.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidget(panel)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setMaximumWidth(300)
        scroll.setMinimumWidth(120)
        return scroll

    def _build_footer(self):
        """Shared bottom strip (below the tabs): the map's Details on the
        left, Import/Export dropdowns on the right. Placing metadata + I/O
        in a footer — not the sidebar — keeps this editor consistent with
        the data + map editors. Scroll-wrapped so its labels never pin the
        window's minimum width."""
        # Import/Export dropdowns — uniform (larger) width matching the sprite
        # editors, stacked in the shared io column.
        self._import_btn = QPushButton("Import…")
        self._import_btn.setMenu(self._build_import_menu())
        self._import_btn.setEnabled(self._undo_stack is not None)
        self._export_btn = QPushButton("Export…")
        self._export_btn.setMenu(self._build_export_menu())
        io_panel = io_button_column(self._import_btn, self._export_btn)

        # Transparent-colour picker (relocated out of the hidden backdrop
        # editor) — same widget the sprite editors use.
        picker_panel = QWidget()
        ppl = QVBoxLayout(picker_panel)
        ppl.setContentsMargins(0, 0, 0, 0)
        ppl.addWidget(self._backdrop_picker)
        ppl.addStretch(1)

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        self._meta_size = QLabel("—")
        self._meta_tiles = QLabel("—")
        self._meta_banks = QLabel("—")
        self._meta_layer_b = QLabel("—")
        self._meta_anim = QLabel("—")
        form.addRow("Size", self._meta_size)
        form.addRow("Tiles", self._meta_tiles)
        form.addRow("Palette banks", self._meta_banks)
        form.addRow("Layer B", self._meta_layer_b)
        form.addRow("Anim frames", self._meta_anim)
        details_panel = QWidget()
        details_panel.setLayout(form)

        scroll = build_editor_footer([io_panel, picker_panel, details_panel])
        self._footer_scroll = scroll
        return scroll

    # ---- View selector / zoom / import-export ----------------------------

    def _active_layer_key_full(self):
        return "layer_b" if self._view_mode == "b" else "layer_a"

    def _zoom_factor(self):
        data = self._zoom_combo.currentData()
        return int(data) if data else 1

    def _on_view_changed(self, key):
        self._view_mode = key
        is_layer = key in ("a", "b")
        if hasattr(self, "_act_import_png"):
            self._act_import_png.setEnabled(is_layer and self._undo_stack is not None)
        self._refresh_active_tab()

    def _on_zoom_changed(self, _ix):
        self._refresh_active_tab()

    def _build_export_menu(self):
        menu = QMenu(self)
        menu.addAction("PNG (current view)…").triggered.connect(
            self._on_bg_export_clicked)
        menu.addAction("Native files (NCGR / NSCR / NCLR)…").triggered.connect(
            self._on_export_native)
        return menu

    def _build_import_menu(self):
        menu = QMenu(self)
        self._act_import_png = menu.addAction("PNG → selected layer…")
        self._act_import_png.triggered.connect(
            lambda: self._on_static_import_clicked(self._active_layer_key_full()))
        menu.addAction("Native file (NCGR / NSCR / NCLR)…").triggered.connect(
            self._on_import_native)
        return menu

    def _update_details(self, map_id):
        a_nscr = self._session.btmap_file_bytes(f"DAT/btmap/{map_id}as")
        aw, ah, _ = btmap.parse_nscr(a_nscr)
        self._meta_size.setText(f"{aw}×{ah}")
        try:
            tiles, _ = btmap._ncgr_tiles_as_indices(
                self._session.btmap_file_bytes(f"DAT/btmap/{map_id}ac"))
            self._meta_tiles.setText(str(len(tiles)))
        except Exception:
            self._meta_tiles.setText("—")
        palettes, _ = btmap.parse_nclr(
            self._session.btmap_file_bytes(f"DAT/btmap/{map_id}ap"))
        self._meta_banks.setText(str(len(palettes)))
        b_path = f"DAT/btmap/{map_id}bs"
        if b_path in self._file_table:
            bw, bh, _ = btmap.parse_nscr(self._session.btmap_file_bytes(b_path))
            self._meta_layer_b.setText(f"{bw}×{bh}")
        else:
            self._meta_layer_b.setText("—")
        anim_count = sum(
            1 for frame in btmap.ANIM_FRAMES
            if f"DAT/btmap/{map_id}a{frame}c" in self._file_table)
        self._meta_anim.setText(str(anim_count) if anim_count else "—")
        tooltip = self._build_components_tooltip(map_id)
        for w in (self._meta_size, self._meta_tiles, self._meta_banks,
                  self._meta_layer_b, self._meta_anim):
            w.setToolTip(tooltip)
        # Keep the footer's transparent-colour picker showing the current
        # backdrop colour (bank 0 slot 0) even though the swatch grid is
        # hidden; this also refreshes the dormant swatches harmlessly.
        self._refresh_backdrop_swatches()

    def _on_export_native(self):
        if self._current_id is None:
            return
        map_id = self._current_id
        folder = QFileDialog.getExistingDirectory(
            self, "Export native files to folder")
        if not folder:
            return
        wrote = []

        def _dump(path, name):
            if path in self._file_table:
                data = self._session.btmap_file_bytes(path)
                with open(os.path.join(folder, name), "wb") as fh:
                    fh.write(data)
                wrote.append(name)

        if self._view_mode in ("a", "composite"):
            _dump(f"DAT/btmap/{map_id}ac", f"{map_id}ac.ncgr")
            _dump(f"DAT/btmap/{map_id}as", f"{map_id}as.nscr")
        if self._view_mode in ("b", "composite"):
            _dump(f"DAT/btmap/{map_id}bc", f"{map_id}bc.ncgr")
            _dump(f"DAT/btmap/{map_id}bs", f"{map_id}bs.nscr")
        _dump(f"DAT/btmap/{map_id}ap", f"{map_id}ap.nclr")
        QMessageBox.information(
            self, "Export complete",
            "Wrote:\n" + "\n".join(wrote) if wrote else "Nothing to write.")

    def _on_import_native(self):
        if self._current_id is None or self._undo_stack is None:
            QMessageBox.information(
                self, "Read-only", "Open a project to import files.")
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Import native file", "",
            "Nitro files (*.ncgr *.nscr *.nclr *.bin);;All files (*)")
        if not path:
            return
        with open(path, "rb") as fh:
            raw = fh.read()
        magic = raw[:4]
        map_id = self._current_id
        layer = "b" if self._view_mode == "b" else "a"
        if magic == b"RGCN":
            target, kind = f"DAT/btmap/{map_id}{layer}c", "NCGR"
        elif magic == b"RCSN":
            target, kind = f"DAT/btmap/{map_id}{layer}s", "NSCR"
        elif magic == b"RLCN":
            target, kind = f"DAT/btmap/{map_id}ap", "NCLR"
        else:
            QMessageBox.warning(
                self, "Unrecognised file",
                "Expected an NCGR (RGCN), NSCR (RCSN) or NCLR (RLCN) file.")
            return
        if target not in self._file_table:
            QMessageBox.warning(
                self, "No such component",
                f"This map has no {kind} for the {layer.upper()} layer.")
            return
        from editor.commands import ReplaceBtmapFileCommand
        self._undo_stack.push(ReplaceBtmapFileCommand(
            self._session, target, raw,
            f"Import {kind} → {target.rsplit('/', 1)[-1]}",
            on_change=self._on_btmap_file_replaced))

    def _on_bg_export_clicked(self) -> None:
        if self._current_id is None:
            return
        preview = self._render_for_tab(self._TAB_BG)
        if preview is None or preview.width == 0:
            QMessageBox.information(
                self, "Nothing to export", "No layers are visible to export.")
            return
        try:
            from PIL import Image
        except ImportError:
            QMessageBox.warning(
                self, "Export failed",
                "Pillow is required to export PNGs (pip install pillow).")
            return
        default_name = f"map{self._current_id}_bg.png"
        png_path, _ = QFileDialog.getSaveFileName(
            self, f"Export background for map {self._current_id}",
            default_name, "PNG images (*.png);;All files (*)")
        if not png_path:
            return
        try:
            Image.frombytes(
                "RGBA", (preview.width, preview.height), preview.rgba).save(png_path)
        except OSError as e:
            QMessageBox.warning(self, "Export failed", str(e))
            return
        QMessageBox.information(self, "Export complete", f"Wrote:\n{png_path}")

    # ---- Layer A backdrop (per-bank slot 0) -----------------------------

    def _build_layer_a_backdrop_section(self, preview_label: QLabel) -> QWidget:
        """Collapsible row of 16 swatches showing each NCLR bank's slot 0
        — the colour the engine paints for any cell using palette index 0
        in that bank. NDS BG layers don't render index 0 as transparent
        on the bottom layer (see ``_render_layer`` in digimon_core/btmap),
        so this slot doubles as the per-bank backdrop / off-camera filler
        colour, and the camera's right-edge limit follows wherever
        non-filler tiles run out.

        Click a swatch to mark that bank active, then type a hex or use
        the eyedropper on the Layer A preview to set its slot 0. Apply
        goes through the same ``ReplaceBtmapFileCommand`` macro the
        import path uses, so a single Ctrl+Z reverts.
        """
        self._backdrop_active_bank = 0
        self._backdrop_swatches: List[QPushButton] = []

        toggle = QToolButton()
        toggle.setText("\u25b6 Backdrop colours (palette slot 0 per bank)")
        toggle.setCheckable(True)
        toggle.setToolButtonStyle(Qt.ToolButtonTextOnly)
        toggle.setStyleSheet(
            "QToolButton { border: none; text-align: left; padding: 4px 0; }"
        )
        self._backdrop_toggle = toggle

        content = QWidget()
        content.setVisible(False)
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(12, 4, 0, 4)
        self._backdrop_content = content

        # ``self._backdrop_picker`` is created + bound in ``_build_bg_tab``
        # (it lives in the footer now). This section only holds the per-bank
        # swatch grid + active-bank caption, kept dormant/hidden for now.
        self._backdrop_active_label = QLabel("Active: Bank 0")
        self._backdrop_active_label.setStyleSheet("color: #888;")
        content_layout.addWidget(self._backdrop_active_label)

        # 4x4 grid of swatches. Each shows the bank's slot-0 colour and
        # selects that bank when clicked.
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(4)
        for b in range(16):
            cell = QWidget()
            cell_layout = QVBoxLayout(cell)
            cell_layout.setContentsMargins(0, 0, 0, 0)
            cell_layout.setSpacing(1)
            caption = QLabel(f"B{b}")
            caption.setAlignment(Qt.AlignCenter)
            caption.setStyleSheet("color: #888; font-size: 10px;")
            swatch = QPushButton()
            swatch.setFixedSize(48, 24)
            swatch.setStyleSheet(
                "background-color: black; border: 1px solid #555;"
            )
            swatch.clicked.connect(
                lambda _checked=False, bank=b: self._on_bank_swatch_clicked(bank)
            )
            cell_layout.addWidget(caption)
            cell_layout.addWidget(swatch)
            grid.addWidget(cell, b // 4, b % 4)
            self._backdrop_swatches.append(swatch)
        content_layout.addLayout(grid)

        toggle.toggled.connect(self._on_backdrop_toggled)

        wrapper = QWidget()
        wrapper_layout = QVBoxLayout(wrapper)
        wrapper_layout.setContentsMargins(0, 0, 0, 0)
        wrapper_layout.setSpacing(0)
        wrapper_layout.addWidget(toggle)
        wrapper_layout.addWidget(content)
        return wrapper

    def _layer_a_preview_source(self):
        """Source provider for TransparentColorPicker.

        Returns ``(QImage, (src_w, src_h), (pix_w, pix_h))`` so the picker
        can map a click in label coords back to a source-image pixel.
        ``None`` means there's nothing rendered yet — the picker silently
        no-ops in that case.
        """
        qimg = self._layer_a_preview_qimage
        if qimg is None:
            return None
        label = self._layer_a_label
        pixmap = label.pixmap()
        if pixmap is None or pixmap.isNull():
            return None
        return (
            qimg,
            (qimg.width(), qimg.height()),
            (pixmap.width(), pixmap.height()),
        )

    def _on_backdrop_toggled(self, checked: bool) -> None:
        self._backdrop_content.setVisible(checked)
        arrow = "\u25bc" if checked else "\u25b6"
        self._backdrop_toggle.setText(
            f"{arrow} Backdrop colours (palette slot 0 per bank)"
        )
        if checked:
            self._refresh_backdrop_swatches()

    def _on_bank_swatch_clicked(self, bank: int) -> None:
        self._backdrop_active_bank = bank
        self._backdrop_active_label.setText(f"Active: Bank {bank}")
        self._refresh_backdrop_swatches()

    def _refresh_backdrop_swatches(self) -> None:
        if self._current_id is None:
            for swatch in self._backdrop_swatches:
                swatch.setStyleSheet(
                    "background-color: transparent; border: 1px dashed #999;"
                )
            self._backdrop_picker.set_current_color(None)
            return
        nclr = self._session.btmap_file_bytes(
            f"DAT/btmap/{self._current_id}ap"
        )
        palettes, _ = btmap.parse_nclr(nclr)
        for b, swatch in enumerate(self._backdrop_swatches):
            if b < len(palettes) and palettes[b]:
                r, g, bl = palettes[b][0]
                hex_str = f"#{r:02X}{g:02X}{bl:02X}"
                border = (
                    "2px solid #FFD700"
                    if b == self._backdrop_active_bank
                    else "1px solid #555"
                )
                swatch.setStyleSheet(
                    f"background-color: {hex_str}; border: {border};"
                )
                swatch.setToolTip(f"Bank {b} slot 0: {hex_str}")
            else:
                swatch.setStyleSheet(
                    "background-color: transparent; border: 1px dashed #999;"
                )
                swatch.setToolTip(f"Bank {b}: not present in NCLR")
        if self._backdrop_active_bank < len(palettes):
            self._backdrop_picker.set_current_color(
                palettes[self._backdrop_active_bank][0]
            )
        else:
            self._backdrop_picker.set_current_color(None)

    def _apply_backdrop_color(self, rgb) -> None:
        if self._current_id is None:
            return
        if self._undo_stack is None:
            QMessageBox.information(
                self, "Read-only",
                "Open a project to edit backdrop colours.",
            )
            return
        map_id = self._current_id
        nclr_path = f"DAT/btmap/{map_id}ap"
        nclr_raw = self._session.btmap_file_bytes(nclr_path)
        palettes, _ = btmap.parse_nclr(nclr_raw)
        bank = self._backdrop_active_bank
        if bank >= len(palettes):
            QMessageBox.warning(
                self, "Bank missing",
                f"Bank {bank} isn't present in this NCLR ({len(palettes)} banks).",
            )
            return
        new_palette = list(palettes[bank])
        if not new_palette:
            return
        if tuple(new_palette[0]) == tuple(rgb):
            return
        new_palette[0] = rgb

        from digimon_core import sprite
        from editor.commands import ReplaceBtmapFileCommand
        new_nclr = sprite.build_nclr_from_template(nclr_raw, {bank: new_palette})
        self._undo_stack.push(ReplaceBtmapFileCommand(
            self._session, nclr_path, new_nclr,
            f"Backdrop colour bank {bank} (map {map_id})",
            on_change=self._on_btmap_file_replaced,
        ))

    def _build_anim_tab(self) -> QWidget:
        """Animations tab: preview surface + frame/sub-frame controls.

        Two render modes share the same QLabel:

        - **Animated composite** when at least one outer frame has
          ``schema_ok=True``. Frame combo selects the outer frame,
          sub-frame slider scrubs through its tile-blit cycle, Play
          loops at the per-sub-frame tick rate.
        - **Tile sheet fallback** when no frame decodes. Controls are
          hidden; the tab shows the same stacked NaXc bitmap as before.
        """
        label = QLabel("Select a battle background to preview.")
        label.setAlignment(Qt.AlignCenter)
        label.setMinimumSize(160, 90)
        scroll = QScrollArea()
        scroll.setWidget(label)
        scroll.setWidgetResizable(True)
        scroll.setAlignment(Qt.AlignCenter)
        self._anim_label = label

        # Controls row — frame combo, sub-frame slider, play toggle,
        # plus a status label showing the current frame's tick rate.
        self._anim_frame_combo = QComboBox()
        self._anim_frame_combo.currentIndexChanged.connect(
            self._on_anim_frame_changed,
        )
        self._anim_sub_slider = QSlider(Qt.Horizontal)
        self._anim_sub_slider.setMinimum(0)
        self._anim_sub_slider.setTracking(True)
        self._anim_sub_slider.valueChanged.connect(self._on_anim_sub_changed)
        self._anim_play_btn = QToolButton()
        self._anim_play_btn.setText("\u25b6 Play")
        self._anim_play_btn.setCheckable(True)
        self._anim_play_btn.toggled.connect(self._on_anim_play_toggled)
        self._anim_export_btn = QToolButton()
        self._anim_export_btn.setText("Export PNG\u2026")
        self._anim_export_btn.setToolTip(
            "Save the current animation frame as editable sparse PNGs"
            " (one per sub-frame) plus a reference composite."
        )
        self._anim_export_btn.clicked.connect(self._on_anim_export_clicked)
        self._anim_import_btn = QToolButton()
        self._anim_import_btn.setText("Import PNG\u2026")
        self._anim_import_btn.setToolTip(
            "Re-encode the current animation frame from an exported"
            " folder. Requires the original .meta.json next to the PNGs."
        )
        self._anim_import_btn.clicked.connect(self._on_anim_import_clicked)
        # Import only makes sense when we have an undo stack to record
        # the swap on — disable in viewer-only contexts.
        self._anim_import_btn.setEnabled(self._undo_stack is not None)
        self._anim_status = QLabel("\u2014")
        # Word-wrap + a max width so the long "sub 1/4 \u00b7 \u2026 \u00b7 dominant-bank
        # splice" text wraps instead of pinning a wide minimum.
        self._anim_status.setWordWrap(True)
        self._anim_status.setMaximumWidth(300)

        # FlowLayout so the controls wrap onto a second row when the tab is
        # narrow instead of forcing the whole window wide.
        def _labeled(text: str, widget: QWidget) -> QWidget:
            g = QWidget()
            h = QHBoxLayout(g)
            h.setContentsMargins(0, 0, 0, 0)
            h.setSpacing(4)
            h.addWidget(QLabel(text))
            h.addWidget(widget)
            return g

        self._anim_sub_slider.setMinimumWidth(120)
        self._anim_controls = QWidget()
        cflow = FlowLayout(self._anim_controls, margin=0, h_spacing=10, v_spacing=4)
        cflow.addWidget(_labeled("Frame", self._anim_frame_combo))
        cflow.addWidget(_labeled("Sub-frame", self._anim_sub_slider))
        cflow.addWidget(self._anim_play_btn)
        cflow.addWidget(self._anim_export_btn)
        cflow.addWidget(self._anim_import_btn)
        cflow.addWidget(self._anim_status)
        make_height_for_width(self._anim_controls)

        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.addWidget(scroll, 1)
        page_layout.addWidget(self._anim_controls)
        return page

    # ---- Selection / tab change -----------------------------------------

    # ---- Paint tab -------------------------------------------------------

    def _build_paint_tab(self):
        """Porymap-style NSCR painter for the selected layer, shared verbatim
        with the UI-background editor (same NCGR/NSCR/NCLR format). A Layer A/B
        selector sits in the paint sidebar (via the shared tab's header slot)."""
        self._paint_layer = "a"
        # Layer A/B selector — lives in the controls sidebar, not a top row.
        layer_box = QGroupBox("Layer")
        lb = QHBoxLayout(layer_box)
        lb.setContentsMargins(8, 4, 8, 4)
        self._paint_layer_group = QButtonGroup(self)
        self._paint_layer_a = QRadioButton("A")
        self._paint_layer_b = QRadioButton("B")
        for rb, key in ((self._paint_layer_a, "a"), (self._paint_layer_b, "b")):
            self._paint_layer_group.addButton(rb)
            rb.toggled.connect(
                lambda checked, k=key: self._on_paint_layer_changed(k) if checked else None)
            lb.addWidget(rb)
        lb.addStretch(1)

        self._paint_tab = TilemapPaintTab(
            self._undo_stack, _BtmapPaintProvider(self), header_widget=layer_box)
        self._paint_layer_a.setChecked(True)
        return self._paint_tab

    def _on_paint_layer_changed(self, key: str) -> None:
        self._paint_layer = key
        self._paint_tab.refresh()

    def _on_paint_external_change(self) -> None:
        """After a paint NSCR edit (or its undo/redo), keep the Background
        preview + Details in sync with the new bytes."""
        if self._current_id is not None:
            self._update_details(self._current_id)
        if self._tabs.currentIndex() != self._TAB_PAINT:
            self._refresh_active_tab()

    def _on_index_selected(self, ix: int) -> None:
        if not (0 <= ix < len(self._map_ids)):
            return
        map_id = self._map_ids[ix]
        self._current_id = map_id
        self._session.remember_selection(self._CURSOR_KEY, int(map_id))
        self._rebuild_anim_entries(map_id)
        self._paint_tab.invalidate()
        self._update_tab_availability(map_id)
        self._update_details(map_id)
        self._refresh_active_tab()

    def _on_tab_changed(self, ix: int) -> None:
        self._session.remember_selection(self._CURSOR_KEY + "_tab", ix)
        # Pause the animation timer when the user navigates away from the
        # Anim tab — otherwise it keeps repainting an invisible label.
        if ix != self._TAB_ANIM:
            self._stop_anim_playback()
        self._refresh_active_tab()

    def _update_tab_availability(self, map_id: str) -> None:
        """Disable tabs whose underlying data isn't shipped for this map.

        Layer B and animation tabs are sparse — disabling rather than
        hiding keeps the tab strip's layout stable as the user moves
        between maps, so the active tab doesn't shuffle indices.
        """
        has_b = f"DAT/btmap/{map_id}bs" in self._file_table
        has_anim = any(
            f"DAT/btmap/{map_id}a{frame}c" in self._file_table
            for frame in btmap.ANIM_FRAMES
        )
        self._tabs.setTabEnabled(self._TAB_ANIM, has_anim)
        if not self._tabs.isTabEnabled(self._tabs.currentIndex()):
            self._tabs.blockSignals(True)
            self._tabs.setCurrentIndex(self._TAB_BG)
            self._tabs.blockSignals(False)
        # Layer B has no data for this map — grey out its View radio and fall
        # the selected view back to a valid one.
        self._view_b.setEnabled(has_b)
        if not has_b and self._view_b.isChecked():
            self._view_comp.setChecked(True)  # fires _on_view_changed
        # Same gating for the Paint tab's layer toggle.
        self._paint_layer_b.setEnabled(has_b)
        if not has_b and self._paint_layer_b.isChecked():
            self._paint_layer_a.setChecked(True)  # fires _on_paint_layer_changed

    # ---- Render dispatch -------------------------------------------------

    def _refresh_active_tab(self) -> None:
        if self._current_id is None:
            return
        ix = self._tabs.currentIndex()
        if ix == self._TAB_PAINT:
            self._paint_tab.refresh()  # manages its own canvas
            return
        try:
            preview = self._render_for_tab(ix)
        except (ValueError, KeyError) as e:
            self._set_label_text(ix, f"Render failed: {e}")
            return
        if preview is None or preview.width == 0 or preview.height == 0:
            self._set_label_text(ix, "(empty)")
            return
        self._set_pixmap_on_tab(ix, preview)

    def _render_for_tab(self, ix: int) -> Optional["btmap.BtmapPreview"]:
        map_id = self._current_id
        nclr = self._session.btmap_file_bytes(f"DAT/btmap/{map_id}ap")
        if ix == self._TAB_ANIM:
            return self._render_anim_tab(map_id, nclr)
        # Background: render the selected view (Layer A / Layer B / Composite).
        has_b = f"DAT/btmap/{map_id}bs" in self._file_table
        if self._view_mode == "a":
            return btmap.render_single_layer(
                self._session.btmap_file_bytes(f"DAT/btmap/{map_id}ac"),
                self._session.btmap_file_bytes(f"DAT/btmap/{map_id}as"),
                nclr, backdrop_opaque=True,
            )
        if self._view_mode == "b" and has_b:
            return btmap.render_single_layer(
                self._session.btmap_file_bytes(f"DAT/btmap/{map_id}bc"),
                self._session.btmap_file_bytes(f"DAT/btmap/{map_id}bs"),
                nclr, backdrop_opaque=True,
            )
        # Composite (or B unavailable).
        return btmap.render_btmap(
            map_id,
            layer_a_ncgr=self._session.btmap_file_bytes(f"DAT/btmap/{map_id}ac"),
            layer_a_nclr=nclr,
            layer_a_nscr=self._session.btmap_file_bytes(f"DAT/btmap/{map_id}as"),
            layer_b_ncgr=self._optional_bytes(f"DAT/btmap/{map_id}bc"),
            layer_b_nscr=self._optional_bytes(f"DAT/btmap/{map_id}bs"),
        )

    # ---- Animation tab ---------------------------------------------------

    def _rebuild_anim_entries(self, map_id: str) -> None:
        """Snapshot every animation frame for this map and parse it.

        Each entry stores the NaXc bytes, the parsed :class:`AnimFrame`,
        the original ``frame`` index, and the splice schema classification
        (``"all"`` / ``"dominant_bank"`` / ``"none"``). Stops the play
        timer so a previous map's cycle can't keep ticking into the new
        selection.
        """
        self._stop_anim_playback()
        self._anim_entries = []
        layer_a_ncgr_bytes: Optional[bytes] = None
        layer_a_nscr_bytes: Optional[bytes] = None
        for frame_ix in btmap.ANIM_FRAMES:
            ncgr_path = f"DAT/btmap/{map_id}a{frame_ix}c"
            naxn_path = f"DAT/btmap/{map_id}a{frame_ix}n"
            if ncgr_path not in self._file_table or naxn_path not in self._file_table:
                continue
            ncgr_bytes = self._session.btmap_file_bytes(ncgr_path)
            naxn_bytes = self._session.btmap_file_bytes(naxn_path)
            try:
                parsed = btmap.parse_naxn(naxn_bytes)
            except ValueError:
                parsed = None
            schema = None
            if parsed is not None and parsed.schema_ok:
                if layer_a_ncgr_bytes is None:
                    layer_a_ncgr_bytes = self._session.btmap_file_bytes(f"DAT/btmap/{map_id}ac")
                    layer_a_nscr_bytes = self._session.btmap_file_bytes(f"DAT/btmap/{map_id}as")
                try:
                    schema = btmap.classify_anim_frame_schema(
                        parsed,
                        layer_a_ncgr=layer_a_ncgr_bytes,
                        layer_a_nscr=layer_a_nscr_bytes,
                    )
                except Exception:
                    schema = None
            self._anim_entries.append({
                "frame_ix": frame_ix,
                "ncgr": ncgr_bytes,
                "naxn": naxn_bytes,
                "parsed": parsed,
                "schema": schema,
            })

        # Repopulate combo. Block signals so the initial setCurrentIndex
        # doesn't trigger a render before the surrounding state is ready.
        self._anim_frame_combo.blockSignals(True)
        self._anim_frame_combo.clear()
        decodable = [e for e in self._anim_entries if e["parsed"] is not None and e["parsed"].schema_ok]
        for e in decodable:
            self._anim_frame_combo.addItem(f"Frame {e['frame_ix']}", e)
        self._anim_frame_combo.blockSignals(False)

        has_decodable = bool(decodable)
        self._anim_controls.setVisible(has_decodable)
        if has_decodable:
            self._anim_frame_ix = 0
            self._anim_sub_ix = 0
            self._anim_frame_combo.setCurrentIndex(0)
            self._configure_sub_slider_for_current_frame()

    def _render_anim_tab(self, map_id: str, nclr: bytes):
        """Resolve which animation render the tab should show right now.

        Picks the in-place composite when at least one outer frame is
        decodable; otherwise falls back to the legacy NaXc tile sheet
        so the tab still has *something* to look at.
        """
        if not self._anim_entries:
            return None
        decodable = [e for e in self._anim_entries if e["parsed"] is not None and e["parsed"].schema_ok]
        if not decodable:
            # Fallback: stacked NaXc preview.
            frames = [e["ncgr"] for e in self._anim_entries]
            return btmap.render_anim_sheet(frames, nclr)
        if not (0 <= self._anim_frame_ix < len(decodable)):
            self._anim_frame_ix = 0
        entry = decodable[self._anim_frame_ix]
        frame: btmap.AnimFrame = entry["parsed"]
        if not (0 <= self._anim_sub_ix < len(frame.sub_frames)):
            self._anim_sub_ix = 0
        layer_b_ncgr = self._optional_bytes(f"DAT/btmap/{map_id}bc")
        layer_b_nscr = self._optional_bytes(f"DAT/btmap/{map_id}bs")
        preview, _schema = btmap.render_anim_state_routed(
            layer_a_ncgr=self._session.btmap_file_bytes(f"DAT/btmap/{map_id}ac"),
            layer_a_nclr=nclr,
            layer_a_nscr=self._session.btmap_file_bytes(f"DAT/btmap/{map_id}as"),
            anim_ncgr=entry["ncgr"],
            anim_naxn=entry["naxn"],
            sub_ix=self._anim_sub_ix,
            layer_b_ncgr=layer_b_ncgr,
            layer_b_nscr=layer_b_nscr,
        )
        return preview

    def _current_anim_frame(self) -> Optional["btmap.AnimFrame"]:
        decodable = [e for e in self._anim_entries if e["parsed"] is not None and e["parsed"].schema_ok]
        if not decodable or not (0 <= self._anim_frame_ix < len(decodable)):
            return None
        return decodable[self._anim_frame_ix]["parsed"]

    def _configure_sub_slider_for_current_frame(self) -> None:
        frame = self._current_anim_frame()
        if frame is None or not frame.sub_frames:
            self._anim_sub_slider.setEnabled(False)
            self._anim_status.setText("\u2014")
            return
        self._anim_sub_slider.blockSignals(True)
        self._anim_sub_slider.setMaximum(max(0, len(frame.sub_frames) - 1))
        self._anim_sub_slider.setValue(self._anim_sub_ix)
        self._anim_sub_slider.blockSignals(False)
        self._anim_sub_slider.setEnabled(len(frame.sub_frames) > 1)
        self._update_anim_status_label(frame)

    def _update_anim_status_label(self, frame: "btmap.AnimFrame") -> None:
        sub = frame.sub_frames[self._anim_sub_ix]
        # ticks are NDS vblanks (~60 Hz); show ms for human-readable cadence.
        ms = int(round(sub.ticks * 1000 / 60))
        decodable = [e for e in self._anim_entries if e["parsed"] is not None and e["parsed"].schema_ok]
        schema = None
        if 0 <= self._anim_frame_ix < len(decodable):
            schema = decodable[self._anim_frame_ix].get("schema")
        # "all" is the textbook case — leave the label clean. Surface the
        # mitigated/skipped cases so the user knows when the preview
        # diverges from a literal NCGR splice (see classify_anim_frame_schema).
        schema_suffix = ""
        if schema == "dominant_bank":
            schema_suffix = " \u00b7 dominant-bank splice (mixed banks)"
        elif schema == "none":
            schema_suffix = " \u00b7 static base (overlay schema undecoded)"
        self._anim_status.setText(
            f"sub {self._anim_sub_ix + 1}/{len(frame.sub_frames)} \u00b7 {sub.ticks}t ({ms} ms){schema_suffix}"
        )

    def _on_anim_frame_changed(self, ix: int) -> None:
        if ix < 0:
            return
        self._anim_frame_ix = ix
        self._anim_sub_ix = 0
        self._configure_sub_slider_for_current_frame()
        if self._anim_play_btn.isChecked():
            self._schedule_next_anim_tick()
        self._refresh_active_tab()

    def _on_anim_sub_changed(self, value: int) -> None:
        self._anim_sub_ix = value
        frame = self._current_anim_frame()
        if frame is not None:
            self._update_anim_status_label(frame)
        self._refresh_active_tab()

    def _on_anim_play_toggled(self, checked: bool) -> None:
        if checked:
            self._anim_play_btn.setText("\u23f8 Pause")
            self._schedule_next_anim_tick()
        else:
            self._anim_play_btn.setText("\u25b6 Play")
            self._anim_timer.stop()

    def _schedule_next_anim_tick(self) -> None:
        frame = self._current_anim_frame()
        if frame is None or not frame.sub_frames:
            return
        sub = frame.sub_frames[self._anim_sub_ix]
        # Clamp to a sane minimum so a malformed ticks=0 frame can't busy-loop.
        ms = max(16, int(round(sub.ticks * 1000 / 60)))
        self._anim_timer.start(ms)

    def _on_anim_tick(self) -> None:
        frame = self._current_anim_frame()
        if frame is None or not frame.sub_frames:
            return
        next_ix = (self._anim_sub_ix + 1) % len(frame.sub_frames)
        # Drive the slider — its valueChanged handler updates ix + repaints.
        self._anim_sub_slider.setValue(next_ix)
        self._schedule_next_anim_tick()

    def _stop_anim_playback(self) -> None:
        if self._anim_timer.isActive():
            self._anim_timer.stop()
        if self._anim_play_btn.isChecked():
            self._anim_play_btn.blockSignals(True)
            self._anim_play_btn.setChecked(False)
            self._anim_play_btn.setText("\u25b6 Play")
            self._anim_play_btn.blockSignals(False)

    def _on_anim_export_clicked(self) -> None:
        """Prompt for an output folder and dump the sub-frame PNG pack.

        Uses the current frame combo selection — the user picks which
        outer frame to export by selecting it before clicking. Stops
        playback first so the export doesn't race with the timer.
        """
        if self._current_id is None:
            return
        decodable = [e for e in self._anim_entries if e["parsed"] is not None and e["parsed"].schema_ok]
        if not (0 <= self._anim_frame_ix < len(decodable)):
            return
        entry = decodable[self._anim_frame_ix]
        self._stop_anim_playback()
        out_dir = QFileDialog.getExistingDirectory(
            self,
            f"Export map {self._current_id} frame {entry['frame_ix']}",
        )
        if not out_dir:
            return
        map_id = self._current_id
        try:
            written = btmap.export_anim_frame_pack(
                out_dir=out_dir,
                map_id=map_id,
                frame_ix=entry["frame_ix"],
                layer_a_ncgr=self._session.btmap_file_bytes(f"DAT/btmap/{map_id}ac"),
                layer_a_nclr=self._session.btmap_file_bytes(f"DAT/btmap/{map_id}ap"),
                layer_a_nscr=self._session.btmap_file_bytes(f"DAT/btmap/{map_id}as"),
                anim_ncgr=entry["ncgr"],
                anim_naxn=entry["naxn"],
                layer_b_ncgr=self._optional_bytes(f"DAT/btmap/{map_id}bc"),
                layer_b_nscr=self._optional_bytes(f"DAT/btmap/{map_id}bs"),
            )
        except ImportError:
            QMessageBox.warning(
                self,
                "Export failed",
                "Pillow is required to export PNGs. Install it (pip install pillow) and try again.",
            )
            return
        except (ValueError, KeyError) as e:
            QMessageBox.warning(self, "Export failed", str(e))
            return
        QMessageBox.information(
            self,
            "Export complete",
            f"Wrote {len(written)} files to:\n{out_dir}",
        )

    def _on_anim_import_clicked(self) -> None:
        """Prompt for a folder containing an export pack and apply the
        edits as an undoable command on the session.

        Validates that ``map_id`` and ``frame_ix`` in the meta JSON match
        the currently-selected frame before touching anything — a stray
        folder selection shouldn't silently rewrite a different map.
        """
        if self._current_id is None or self._undo_stack is None:
            return
        decodable = [e for e in self._anim_entries if e["parsed"] is not None and e["parsed"].schema_ok]
        if not (0 <= self._anim_frame_ix < len(decodable)):
            return
        entry = decodable[self._anim_frame_ix]
        self._stop_anim_playback()
        in_dir = QFileDialog.getExistingDirectory(
            self, f"Import map {self._current_id} frame {entry['frame_ix']}",
        )
        if not in_dir:
            return
        map_id = self._current_id
        ncgr_path = f"DAT/btmap/{map_id}a{entry['frame_ix']}c"
        try:
            result = btmap.import_anim_frame_pack(
                folder=in_dir,
                layer_a_nscr=self._session.btmap_file_bytes(f"DAT/btmap/{map_id}as"),
                layer_a_nclr=self._session.btmap_file_bytes(f"DAT/btmap/{map_id}ap"),
                anim_ncgr=entry["ncgr"],
            )
        except ImportError:
            QMessageBox.warning(
                self,
                "Import failed",
                "Pillow is required to import PNGs. Install it (pip install pillow) and try again.",
            )
            return
        except (ValueError, KeyError, OSError) as e:
            QMessageBox.warning(self, "Import failed", str(e))
            return

        if result.map_id != map_id or result.frame_ix != entry["frame_ix"]:
            QMessageBox.warning(
                self,
                "Import mismatch",
                f"This pack targets map {result.map_id} frame {result.frame_ix},"
                f" but the current selection is map {map_id} frame {entry['frame_ix']}."
                "\n\nSwitch the browser to the matching map/frame, or pick a different folder.",
            )
            return

        # Local import — avoids a circular top-level import.
        from editor.commands import ReplaceBtmapFileCommand
        cmd = ReplaceBtmapFileCommand(
            self._session,
            ncgr_path,
            result.new_ncgr,
            f"Import btmap {map_id} frame {entry['frame_ix']}",
            on_change=self._on_btmap_file_replaced,
        )
        self._undo_stack.push(cmd)
        QMessageBox.information(
            self,
            "Import complete",
            f"Rewrote {sum(result.tiles_touched)} tile slot(s) across"
            f" {len(result.tiles_touched)} sub-frame(s).",
        )

    def _on_static_export_clicked(self, tab_key: str) -> None:
        """Save the current static preview as a flat RGBA PNG.

        ``tab_key`` is one of ``"composite"``, ``"layer_a"``, ``"layer_b"``.
        Composite re-uses the same render :func:`render_btmap` produces
        for the Composite tab; the layer-only tabs use
        :func:`render_single_layer` so the saved image matches the on-screen
        view exactly (including the opaque backdrop on Layer A and the
        transparent backdrop on Layer B).
        """
        if self._current_id is None:
            return
        map_id = self._current_id
        paths = btmap.BtmapFiles(map_id)
        nclr = self._session.btmap_file_bytes(paths.layer_a_nclr)

        if tab_key == "composite":
            preview = btmap.render_btmap(
                map_id,
                layer_a_ncgr=self._session.btmap_file_bytes(paths.layer_a_ncgr),
                layer_a_nclr=nclr,
                layer_a_nscr=self._session.btmap_file_bytes(paths.layer_a_nscr),
                layer_b_ncgr=self._optional_bytes(paths.layer_b_ncgr),
                layer_b_nscr=self._optional_bytes(paths.layer_b_nscr),
            )
            default_name = f"map{map_id}_composite.png"
        elif tab_key == "layer_a":
            preview = btmap.render_single_layer(
                self._session.btmap_file_bytes(paths.layer_a_ncgr),
                self._session.btmap_file_bytes(paths.layer_a_nscr),
                nclr,
                backdrop_opaque=True,
            )
            default_name = f"map{map_id}_layerA.png"
        elif tab_key == "layer_b":
            if paths.layer_b_ncgr not in self._file_table:
                QMessageBox.information(
                    self, "Nothing to export",
                    f"Map {map_id} doesn't ship a Layer B file.",
                )
                return
            preview = btmap.render_single_layer(
                self._session.btmap_file_bytes(paths.layer_b_ncgr),
                self._session.btmap_file_bytes(paths.layer_b_nscr),
                nclr,
                backdrop_opaque=True,
            )
            default_name = f"map{map_id}_layerB.png"
        else:
            return

        png_path, _ = QFileDialog.getSaveFileName(
            self, f"Export {tab_key} for map {map_id}",
            default_name, "PNG images (*.png);;All files (*)",
        )
        if not png_path:
            return
        try:
            from PIL import Image
        except ImportError:
            QMessageBox.warning(
                self, "Export failed",
                "Pillow is required to export PNGs."
                " Install it (pip install pillow) and try again.",
            )
            return
        try:
            Image.frombytes(
                "RGBA", (preview.width, preview.height), preview.rgba,
            ).save(png_path)
        except OSError as e:
            QMessageBox.warning(self, "Export failed", str(e))
            return
        QMessageBox.information(
            self, "Export complete", f"Wrote:\n{png_path}",
        )

    def _on_static_import_clicked(self, layer_key: str) -> None:
        """Run a flat-PNG import for Layer A or Layer B and, on user
        approval, splice the new NCGR/NSCR/NCLR triple into the session.

        Layers share an NCLR; this importer writes Layer A's palette to
        bank 0 and Layer B's to bank 1 by default so a sequential
        import-A-then-import-B doesn't clobber A's colors. Other vanilla
        banks survive untouched (the NCLR template is preserved).
        """
        if self._current_id is None or self._undo_stack is None:
            return
        map_id = self._current_id
        paths = btmap.BtmapFiles(map_id)

        # The A/B bank partition is per-map, not a fixed convention.
        # Vanilla maps put Layer A on banks 0..N and Layer B on one high
        # bank (typically 5 or 6, varies per map). Hardcoding "Layer B
        # lives at bank 1" is wrong and was causing Layer A imports to
        # clobber whatever bank Layer B actually used (see issue: rocks
        # on map 69 leaking after a Layer A repaint). Detect each layer's
        # banks from its NSCR and exclude the other layer's banks from
        # this import's writable set.
        def _content_banks(nscr_raw: bytes) -> set[int]:
            _, _, entries = btmap.parse_nscr(nscr_raw)
            return {(e >> 12) & 0xF for e in entries}

        a_banks: set[int] = set()
        b_banks: set[int] = set()
        try:
            a_banks = _content_banks(
                self._session.btmap_file_bytes(paths.layer_a_nscr)
            )
        except (KeyError, ValueError):
            pass
        try:
            b_banks = _content_banks(
                self._session.btmap_file_bytes(paths.layer_b_nscr)
            )
        except (KeyError, ValueError):
            pass

        if layer_key == "layer_a":
            ncgr_path = paths.layer_a_ncgr
            nscr_path = paths.layer_a_nscr
            nclr_path = paths.layer_a_nclr
            # Reserve slot 0 of every bank as the per-bank filler colour.
            # Vanilla Layer A uses bank 0 slot 0 as the off-camera filler
            # tile; quantizer-derived imports were previously polluting it
            # with content (any pixel could nearest-match slot 0), so
            # editing the new backdrop-colour picker leaked across the
            # whole map. Mirroring Layer B's transparent reservation keeps
            # slot 0 strictly for cells the user painted with alpha=0 in
            # the source PNG — those become the strip + camera bound.
            is_transparent = True
            palette_bank = 0
            label_word = "Layer A"
            use_multi_bank = True
            # Exclude every bank Layer B references *except* bank 0. Bank
            # 0 is the shared filler bank: Layer B only ever reads slot 0
            # of it (the transparent/filler sentinel), and Layer A needs
            # slots 1-15 of bank 0 for content. Both can coexist there
            # because the importer's transparent_index_0 logic pins
            # slot 0 to the filler colour.
            reserved = b_banks - {0}
            available_banks: Optional[List[int]] = [
                b for b in range(16) if b not in reserved
            ]
        else:
            ncgr_path = paths.layer_b_ncgr
            nscr_path = paths.layer_b_nscr
            # Layer B shares Layer A's NCLR.
            nclr_path = paths.layer_a_nclr
            is_transparent = True
            label_word = "Layer B"
            use_multi_bank = False
            available_banks = None
            # Route Layer B's content to the bank it already used (so a
            # re-import doesn't shift which bank Layer A must avoid).
            # Fall back to the first bank Layer A doesn't reference if
            # Layer B currently has no non-shared bank.
            b_content = sorted(b_banks - {0})
            if b_content:
                palette_bank = b_content[0]
            else:
                free = [b for b in range(1, 16) if b not in a_banks]
                palette_bank = free[0] if free else 1

        png_path, _ = QFileDialog.getOpenFileName(
            self, f"Import {label_word} for map {map_id}",
            "", "PNG images (*.png);;All files (*)",
        )
        if not png_path:
            return

        original_ncgr = self._session.btmap_file_bytes(ncgr_path)
        original_nscr = self._session.btmap_file_bytes(nscr_path)
        original_nclr = self._session.btmap_file_bytes(nclr_path)

        # Determine the layer's native pixel dimensions from the original
        # NSCR so the importer can validate the imported PNG matches.
        target_w, target_h, _ = btmap.parse_nscr(original_nscr)

        # NaXn animation slot reservation only applies to Layer A: Layer B
        # is never written to by the animation overlay system. Collect any
        # (dst_lo, dst_hi) ranges from the map's NaXn frames so we don't
        # cluster-out tiles the animation will overwrite at runtime.
        naxn_ranges: list[tuple[int, int]] = []
        if layer_key == "layer_a":
            for frame in btmap.ANIM_FRAMES:
                naxn_path = paths.anim_cells(frame)
                if naxn_path not in self._file_table:
                    continue
                try:
                    parsed = btmap.parse_naxn(
                        self._session.btmap_file_bytes(naxn_path)
                    )
                except (ValueError, KeyError, IndexError):
                    continue
                if not parsed.schema_ok:
                    continue
                naxn_ranges.append((parsed.dst_lo, parsed.dst_hi))

        try:
            from digimon_core import btmap_import
            result = btmap_import.import_layer_from_png(
                png_path,
                target_width_px=target_w,
                target_height_px=target_h,
                original_ncgr=original_ncgr,
                original_nscr=original_nscr,
                original_nclr=original_nclr,
                palette_bank=palette_bank,
                is_transparent_layer=is_transparent,
                max_tiles=1024,
                naxn_dst_ranges=naxn_ranges or None,
                use_multi_bank=use_multi_bank,
                available_banks=available_banks,
            )
        except ImportError:
            QMessageBox.warning(
                self, "Import failed",
                "Pillow + NumPy are required for layer import."
                " Install them (pip install pillow numpy) and try again.",
            )
            return
        except (ValueError, KeyError, OSError) as e:
            QMessageBox.warning(self, "Import failed", str(e))
            return

        # Modal preview dialog: original vs imported re-render + stats.
        # Cancel here = nothing changes; Accept commits the splice.
        original_preview = btmap.render_single_layer(
            original_ncgr, original_nscr, original_nclr,
            backdrop_opaque=not is_transparent,
        )
        new_preview = btmap.render_single_layer(
            result.new_ncgr, result.new_nscr, result.new_nclr,
            backdrop_opaque=not is_transparent,
        )
        dialog = _LayerImportPreviewDialog(
            self,
            label_word=label_word,
            map_id=map_id,
            original_preview=original_preview,
            new_preview=new_preview,
            stats=result.stats,
        )
        if dialog.exec() != dialog.DialogCode.Accepted:
            return

        # Three files change atomically — wrap in a macro so a single
        # Ctrl+Z undoes the whole layer import.
        from editor.commands import ReplaceBtmapFileCommand
        macro_label = f"Import {label_word} for map {map_id}"
        self._undo_stack.beginMacro(macro_label)
        try:
            self._undo_stack.push(ReplaceBtmapFileCommand(
                self._session, ncgr_path, result.new_ncgr,
                f"{macro_label} (NCGR)",
                on_change=self._on_btmap_file_replaced,
            ))
            self._undo_stack.push(ReplaceBtmapFileCommand(
                self._session, nscr_path, result.new_nscr,
                f"{macro_label} (NSCR)",
                on_change=self._on_btmap_file_replaced,
            ))
            self._undo_stack.push(ReplaceBtmapFileCommand(
                self._session, nclr_path, result.new_nclr,
                f"{macro_label} (NCLR)",
                on_change=self._on_btmap_file_replaced,
            ))
        finally:
            self._undo_stack.endMacro()

    def _on_btmap_file_replaced(self) -> None:
        """Called by the replace command on redo/undo so the cached NaXn
        entries pick up the new bytes and the preview repaints.
        """
        if self._current_id is not None:
            self._rebuild_anim_entries(self._current_id)
            self._update_details(self._current_id)
            self._refresh_active_tab()

    def _label_for_tab(self, ix: int) -> QLabel:
        if ix == self._TAB_ANIM:
            return self._anim_label
        return self._bg_label

    def _set_label_text(self, ix: int, text: str) -> None:
        label = self._label_for_tab(ix)
        label.setPixmap(QPixmap())
        label.setText(text)

    def _set_pixmap_on_tab(self, ix: int, preview: "btmap.BtmapPreview") -> None:
        # Pin the rgba per-tab so a stale buffer from another tab can't
        # outlive its QImage view.
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
        pixmap = QPixmap.fromImage(image)
        if ix == self._TAB_BG:
            z = self._zoom_factor()
            if z > 1:
                pixmap = pixmap.scaled(
                    preview.width * z, preview.height * z,
                    Qt.KeepAspectRatio, Qt.FastTransformation,
                )
            # Cache the (unscaled) QImage for the backdrop picker's eyedropper.
            # ``copy()`` detaches from the rgba buffer so a later tab change
            # can swap _pinned_rgba without invalidating samples.
            self._layer_a_preview_qimage = image.copy()
        label.setPixmap(pixmap)
        label.adjustSize()
        if ix == self._TAB_BG and self._backdrop_content.isVisible():
            self._refresh_backdrop_swatches()

    # ---- Metadata --------------------------------------------------------

    def _optional_bytes(self, path: str) -> Optional[bytes]:
        if path not in self._file_table:
            return None
        return self._session.btmap_file_bytes(path)

    def _build_components_tooltip(self, map_id: str) -> str:
        layer_paths = [
            f"DAT/btmap/{map_id}ac",
            f"DAT/btmap/{map_id}ap",
            f"DAT/btmap/{map_id}as",
            f"DAT/btmap/{map_id}bc",
            f"DAT/btmap/{map_id}bs",
        ]
        anim_paths = [
            f"DAT/btmap/{map_id}a{frame}{suffix}"
            for frame in btmap.ANIM_FRAMES
            for suffix in ("c", "n")
        ]
        lines: list[str] = []
        for path in layer_paths + anim_paths:
            if path not in self._file_table:
                continue
            info = btmap.describe_component(path, self._session.btmap_file_bytes(path))
            name = path.rsplit("/", 1)[-1]
            lines.append(
                f"{name}  {info.kind}  {info.compressed_size}\u2192{info.uncompressed_size} B"
                f"  ({info.detail})"
            )
        return "\n".join(lines)


class _LayerImportPreviewDialog(QDialog):
    """Modal before/after preview for the flat-PNG layer import.

    Shows the original layer render and the post-import re-render
    side-by-side, plus the import stats so the user sees how lossy the
    tile reduction was before committing. Cancel = no splice; Accept =
    push the three-file undo macro.
    """

    def __init__(
        self,
        parent,
        *,
        label_word: str,
        map_id: str,
        original_preview,
        new_preview,
        stats,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Preview {label_word} import (map {map_id})")
        self.setModal(True)
        # Pin RGBA buffers; QImage views them directly.
        self._orig_rgba = original_preview.rgba
        self._new_rgba = new_preview.rgba

        orig_img = QImage(
            self._orig_rgba,
            original_preview.width, original_preview.height,
            original_preview.width * 4, QImage.Format_RGBA8888,
        )
        new_img = QImage(
            self._new_rgba,
            new_preview.width, new_preview.height,
            new_preview.width * 4, QImage.Format_RGBA8888,
        )

        orig_label = QLabel()
        orig_label.setPixmap(QPixmap.fromImage(orig_img))
        new_label = QLabel()
        new_label.setPixmap(QPixmap.fromImage(new_img))

        row = QHBoxLayout()
        row.addWidget(self._captioned(orig_label, "Original"))
        row.addWidget(self._captioned(new_label, "Imported"))

        reduction_line = (
            f"{stats.unique_tiles_raw} raw \u2192 "
            f"{stats.unique_after_flip_dedup} after flip-dedup"
        )
        if stats.was_reduced:
            reduction_line += (
                f" \u2192 {stats.unique_after_merge} after clustering "
                f"(cap {stats.max_tiles})"
            )
        else:
            reduction_line += " (no clustering needed)"
        banks_used = getattr(stats, "banks_used", 1)
        if banks_used > 1:
            palette_line = (
                f"Palette: {stats.palette_size} colors \u00d7 {banks_used} banks "
                f"({stats.palette_size * banks_used} effective)"
            )
        else:
            palette_line = f"Palette: {stats.palette_size} colors (single bank)"
        info = QLabel(
            f"<b>{label_word} \u00b7 {original_preview.width}\u00d7{original_preview.height}</b><br>"
            f"Cells: {stats.cells_total}<br>"
            f"Tiles: {reduction_line}<br>"
            f"{palette_line}"
        )
        info.setTextFormat(Qt.RichText)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        buttons.button(QDialogButtonBox.Ok).setText("Apply import")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(row)
        layout.addWidget(info)
        layout.addWidget(buttons)

    @staticmethod
    def _captioned(image_label: QLabel, caption: str) -> QWidget:
        wrap = QWidget()
        col = QVBoxLayout(wrap)
        col.setContentsMargins(0, 0, 0, 0)
        col.addWidget(image_label, 0, Qt.AlignCenter)
        cap = QLabel(caption)
        cap.setAlignment(Qt.AlignCenter)
        col.addWidget(cap)
        return wrap
