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

import os
from typing import List, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPixmap, QUndoStack
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from digimon_core import bg, btmap
from digimon_core import sprite as sprite_mod

from .form_helpers import build_editor_footer, io_button_column
from .record_list_panel import RecordListPanel
from .tilemap_paint import PaintContext, TilemapPaintTab
from .transparent_picker import TransparentColorPicker


class _BgPaintProvider:
    """Feeds :class:`TilemapPaintTab` the selected UI background's Nitro trio
    and builds ``ReplaceBgFileCommand`` for NSCR edits."""

    def __init__(self, browser: "BgBrowser"):
        self._b = browser

    def paint_context(self) -> Optional[PaintContext]:
        b = self._b
        if b._current is None:
            return None
        ncgr_path = b._selected_ncgr_path()
        nclr_path = b._selected_nclr_path()
        if not ncgr_path or not nclr_path:
            return None
        nscr_path = b._current.nscr
        try:
            return PaintContext(
                ncgr=b._session.bg_file_bytes(ncgr_path),
                nscr=b._session.bg_file_bytes(nscr_path),
                nclr=b._session.bg_file_bytes(nclr_path),
                nscr_path=nscr_path,
                key=(nscr_path, ncgr_path, nclr_path),
                name=b._current.display_name,
            )
        except (ValueError, KeyError):
            return None

    def make_nscr_command(self, nscr_path, new_bytes, label, on_change):
        from editor.commands import ReplaceBgFileCommand
        return ReplaceBgFileCommand(
            self._b._session, nscr_path, new_bytes, label, on_change=on_change)

    def on_external_change(self) -> None:
        self._b._refresh_preview()


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
        # Dusk BGs default to the ``_M`` palette variant where a hint/family
        # match has one (see bg._BG_PALETTE_HINTS).
        prefer_m = "DUSK" in str(getattr(session, "version", "")).upper()
        self._records: List[bg.BgRecord] = bg.discover_bg_records(
            self._file_table, prefer_m=prefer_m)
        self._current: Optional[bg.BgRecord] = None
        self._pinned_rgba: bytes = b""
        # Cached native-size QImage of the preview, so the transparent-colour
        # eyedropper can sample the rendered background on each click.
        self._preview_qimage: Optional[QImage] = None

        self._build_ui()
        remembered = self._session.recall_selection(self._CURSOR_KEY)
        target = 0
        if remembered is not None and 0 <= int(remembered) < len(self._records):
            target = int(remembered)
        if not self._list.select_index(target):
            self._list.select_first()
        self._restore_tab()

    def _restore_tab(self) -> None:
        """Re-open the tab the user last had active — the session outlives the
        editor widget, so switching away and back returns to Paint/Preview."""
        saved = self._session.recall_selection(self._CURSOR_KEY + "_tab")
        if saved is not None and 0 <= int(saved) < self._tabs.count():
            self._tabs.setCurrentIndex(int(saved))

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
        right_layout.addWidget(self._build_footer(), 0)

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

        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(scroll, 1)
        return page

    def _build_footer(self) -> QWidget:
        """Shared bottom strip (below the tabs) — the Import/Export dropdowns
        (PNG + native) and a labelled Details block, matching the battle-BG,
        field-map, and sprite editors via the common ``build_editor_footer``."""
        self._import_btn = QPushButton("Import…")
        self._import_btn.setMenu(self._build_import_menu())
        self._import_btn.setEnabled(self._undo_stack is not None)
        self._export_btn = QPushButton("Export…")
        self._export_btn.setMenu(self._build_export_menu())
        io_panel = io_button_column(self._import_btn, self._export_btn)

        # Transparent-colour picker — edits palette bank 0 slot 0 (the engine's
        # transparent / backdrop index), same widget the sprite + battle-BG
        # editors use. Eyedrops the Preview render.
        self._transparent_picker = TransparentColorPicker(
            on_color_picked=self._apply_transparent_color)
        self._transparent_picker.bind_preview(self._preview, self._preview_source)
        picker_panel = QWidget()
        ppl = QVBoxLayout(picker_panel)
        ppl.setContentsMargins(0, 0, 0, 0)
        ppl.addWidget(self._transparent_picker)
        ppl.addStretch(1)

        self._meta_size = QLabel("—")
        self._meta_grid = QLabel("—")
        self._meta_tiles = QLabel("—")
        self._meta_bpp = QLabel("—")
        self._meta_palette = QLabel("—")
        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.addRow("Size", self._meta_size)
        form.addRow("Tilemap", self._meta_grid)
        form.addRow("Tiles", self._meta_tiles)
        form.addRow("Bit depth", self._meta_bpp)
        form.addRow("Palette banks", self._meta_palette)
        details_panel = QWidget()
        details_panel.setLayout(form)

        return build_editor_footer([io_panel, picker_panel, details_panel])

    def _build_export_menu(self) -> QMenu:
        menu = QMenu(self)
        menu.addAction("PNG (rendered background)…").triggered.connect(
            self._on_export_clicked)
        menu.addAction("Native files (NCGR / NSCR / NCLR)…").triggered.connect(
            self._on_export_native)
        return menu

    def _build_import_menu(self) -> QMenu:
        menu = QMenu(self)
        self._act_import_png = menu.addAction("PNG → background…")
        self._act_import_png.triggered.connect(self._on_import_clicked)
        menu.addAction("Native file (NCGR / NSCR / NCLR)…").triggered.connect(
            self._on_import_native)
        return menu

    def _on_export_native(self) -> None:
        if self._current is None:
            return
        paths = [
            self._selected_ncgr_path(),
            self._current.nscr,
            self._selected_nclr_path(),
        ]
        folder = QFileDialog.getExistingDirectory(
            self, "Export native files to folder")
        if not folder:
            return
        wrote = []
        for path in paths:
            if not path:
                continue
            data = self._session.bg_file_bytes(path)
            name = path.rsplit("/", 1)[-1]
            with open(os.path.join(folder, name), "wb") as fh:
                fh.write(data)
            wrote.append(name)
        QMessageBox.information(
            self, "Export complete",
            "Wrote:\n" + "\n".join(wrote) if wrote else "Nothing to write.")

    def _on_import_native(self) -> None:
        if self._current is None or self._undo_stack is None:
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
        if magic == b"RGCN":
            target, kind = self._selected_ncgr_path(), "NCGR"
        elif magic == b"RCSN":
            target, kind = self._current.nscr, "NSCR"
        elif magic == b"RLCN":
            target, kind = self._selected_nclr_path(), "NCLR"
        else:
            QMessageBox.warning(
                self, "Unrecognised file",
                "Expected an NCGR (RGCN), NSCR (RCSN) or NCLR (RLCN) file.")
            return
        if not target:
            QMessageBox.warning(
                self, "No such component",
                f"No {kind} is selected for this background.")
            return
        from editor.commands import ReplaceBgFileCommand
        self._undo_stack.push(ReplaceBgFileCommand(
            self._session, target, raw,
            f"Import {kind} → {target.rsplit('/', 1)[-1]}",
            on_change=self._on_bg_file_replaced))

    def _build_paint_tab(self) -> QWidget:
        # Porymap-style NSCR painter shared with the battle-background editor.
        self._paint_tab = TilemapPaintTab(
            self._undo_stack, _BgPaintProvider(self))
        return self._paint_tab

    def _on_index_selected(self, ix: int) -> None:
        if not (0 <= ix < len(self._records)):
            return
        self._current = self._records[ix]
        self._session.remember_selection(self._CURSOR_KEY, ix)
        self._populate_source_combos(self._current)
        self._refresh_preview()
        self._paint_tab.invalidate()
        if self._tabs.currentIndex() == self._TAB_PAINT:
            self._paint_tab.refresh()

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
        self._paint_tab.invalidate()
        if self._tabs.currentIndex() == self._TAB_PAINT:
            self._paint_tab.refresh()

    def _on_tab_changed(self, ix: int) -> None:
        self._session.remember_selection(self._CURSOR_KEY + "_tab", ix)
        if ix == self._TAB_PAINT:
            self._paint_tab.refresh()

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
        # ``copy()`` detaches from _pinned_rgba so the eyedropper keeps a valid
        # source even after the next render swaps the buffer.
        self._preview_qimage = image.copy()
        self._preview.setText("")
        self._preview.setPixmap(QPixmap.fromImage(image))
        self._preview.adjustSize()
        self._update_metadata(preview)
        self._sync_transparent_swatch()

    def _sync_transparent_swatch(self) -> None:
        """Show palette bank 0 slot 0 (the transparent index) in the picker."""
        picker = getattr(self, "_transparent_picker", None)
        if picker is None:
            return
        nclr_path = self._selected_nclr_path()
        if self._current is None or not nclr_path:
            picker.set_current_color(None)
            return
        try:
            palettes, _ = btmap.parse_nclr(self._session.bg_file_bytes(nclr_path))
        except (ValueError, KeyError):
            picker.set_current_color(None)
            return
        if palettes and palettes[0]:
            picker.set_current_color(palettes[0][0])
        else:
            picker.set_current_color(None)

    def _preview_source(self):
        """Source provider for the transparent-colour eyedropper: the live
        native-size QImage + the scaled label pixmap size, so a click on the
        preview maps back to a source pixel."""
        qimg = self._preview_qimage
        if qimg is None:
            return None
        pixmap = self._preview.pixmap()
        if pixmap is None or pixmap.isNull():
            return None
        return (
            qimg,
            (qimg.width(), qimg.height()),
            (pixmap.width(), pixmap.height()),
        )

    def _apply_transparent_color(self, rgb) -> None:
        """Write ``rgb`` into palette bank 0 slot 0 (the engine's transparent /
        backdrop index) as an undoable NCLR edit."""
        if self._current is None:
            return
        nclr_path = self._selected_nclr_path()
        if not nclr_path:
            return
        if self._undo_stack is None:
            QMessageBox.information(
                self, "Read-only", "Open a project to edit the transparent colour.")
            return
        try:
            nclr_raw = self._session.bg_file_bytes(nclr_path)
            palettes, _ = btmap.parse_nclr(nclr_raw)
        except (ValueError, KeyError) as exc:
            QMessageBox.warning(self, "Edit failed", str(exc))
            return
        if not palettes or not palettes[0]:
            return
        new_palette = list(palettes[0])
        if tuple(new_palette[0]) == tuple(rgb):
            return
        new_palette[0] = rgb
        new_nclr = sprite_mod.build_nclr_from_template(nclr_raw, {0: new_palette})
        from editor.commands import ReplaceBgFileCommand
        self._undo_stack.push(ReplaceBgFileCommand(
            self._session, nclr_path, new_nclr,
            f"Transparent colour — {self._current.display_name}",
            on_change=self._on_bg_file_replaced,
        ))

    def _update_metadata(self, preview) -> None:
        if preview is None:
            for lbl in (self._meta_size, self._meta_grid, self._meta_tiles,
                        self._meta_bpp, self._meta_palette):
                lbl.setText("—")
            return
        self._meta_size.setText(f"{preview.width}×{preview.height}")
        self._meta_grid.setText(f"{preview.width // 8}×{preview.height // 8} tiles")
        self._meta_palette.setText(str(preview.palette_bank_count))
        try:
            tiles, bd = btmap._ncgr_tiles_as_indices(
                self._session.bg_file_bytes(self._selected_ncgr_path())
            )
            self._meta_tiles.setText(str(len(tiles)))
            # bd is the raw NCGR bpp code (3 = 4bpp/16-colour, 4 = 8bpp/256).
            self._meta_bpp.setText({3: "4bpp", 4: "8bpp"}.get(bd, f"code {bd}"))
        except (ValueError, KeyError):
            self._meta_tiles.setText("—")
            self._meta_bpp.setText("—")

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

    def _on_bg_file_replaced(self) -> None:
        """Re-render after a native/PNG replace command's redo/undo flip:
        the Preview always, and the shared Paint tab reloaded from the new
        bytes so undo/redo updates the canvas."""
        self._refresh_preview()
        self._paint_tab.invalidate()
        if self._tabs.currentIndex() == self._TAB_PAINT:
            self._paint_tab.refresh()
