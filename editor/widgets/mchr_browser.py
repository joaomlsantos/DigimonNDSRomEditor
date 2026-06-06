"""Overworld-sprite browser + PNG import/export for MCHR_CHR/MCHR_PAL.PAK.

MCHR is a custom multi-frame 4bpp tile format (see :mod:`digimon_core.mchr`),
not NCGR/NCLR — so this widget doesn't share much code with
:class:`sprite_browser.SpriteBrowser` beyond the surrounding
:class:`record_list_panel.RecordListPanel` and QSplitter layout.

Layout:
* left: filterable list of 890 entries (``"0006  3f 16×32"`` style — frame
  count + frame dimensions derived from the NDS-OAM tile-count table)
* right: preview pane + frame navigation + per-sprite palette index spinner
  + width-tiles override + Export/Import PNG buttons

The palette spinner is the load-bearing control here. CHR→PAL mapping is
1:1 for sprites 0..662 but irregular past that (sprite 663 → PAL 664,
sprites 754+ → PAL sprite+16, others still unresolved). The spinner lets
users pin the correct PAL index per sprite until the mapping table is
worked out.

PNG round-trip format:
* Frames PNG — indexed-8 strip, width = ``n_frames × frame_w``, alpha=0 on
  index 0 so transparency survives an external editor. Round-trips
  bit-exact when the editor keeps the file in indexed mode; an RGB(A) edit
  goes through nearest-palette quantization on import.
* Palette PNG — 16×1 RGB888 (one pixel per slot). Importable as 16×1 or as
  a wider canvas with 16 equal swatches (the first pixel of each swatch is
  sampled).
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage, QPixmap, QUndoStack, qRgba
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from digimon_core import mchr, pak, sprite

from ..commands import ReplaceSpriteCommand
from ._png_palette import build_palette_from_png, nearest_idx_opaque
from .record_list_panel import RecordListPanel
from .transparent_picker import TransparentColorPicker


# MCHR palettes are 16 colors, slot 0 reserved transparent.
PALETTE_SLOTS = 16


MCHR_CHR = "DAT/MCHR_CHR.PAK"
MCHR_PAL = "DAT/MCHR_PAL.PAK"


def _decoded_palette(pal_pak: pak.PakFile, pal_idx: int) -> Optional[mchr.Palette]:
    """Decompress + decode PAL[pal_idx] into RGB triples; ``None`` on failure."""
    if not (0 <= pal_idx < pal_pak.count):
        return None
    try:
        raw = sprite.decompress_rle30(pal_pak.entries[pal_idx])
        return mchr.parse_palette_bgr555(raw)
    except (ValueError, IndexError):
        return None


def _decoded_entry(chr_pak: pak.PakFile, idx: int) -> Optional[mchr.MchrEntry]:
    """Decompress + parse CHR[idx]; ``None`` on failure."""
    if not (0 <= idx < chr_pak.count):
        return None
    try:
        raw = sprite.decompress_rle30(chr_pak.entries[idx])
        return mchr.parse_mchr_chr_entry(raw)
    except (ValueError, IndexError):
        return None


class MchrBrowser(QWidget):
    """Read-only browser for the MCHR_CHR + MCHR_PAL overworld-sprite pair."""

    def __init__(self, session, undo_stack: Optional[QUndoStack] = None, parent=None):
        super().__init__(parent)
        self._session = session
        self._undo_stack = undo_stack  # reserved for the import phase
        self._chr_pak: pak.PakFile = session.sprite_pak(MCHR_CHR)
        self._pal_pak: pak.PakFile = session.sprite_pak(MCHR_PAL)
        self._count = self._chr_pak.count

        self._current_idx: Optional[int] = None
        self._current_frame: int = 0
        self._current_palette_idx: int = 0
        # ``None`` lets render_frame_rgba use the NDS-OAM heuristic; we only
        # set this when the user moves the override spinner off zero.
        self._width_tiles_override: Optional[int] = None
        # Per-sprite width-override memory. Keys we've never seen fall
        # through to "auto" (None) — same default as the initial state.
        # Stored as the int the user picked (1..16) or None.
        self._width_overrides: dict[int, Optional[int]] = {}
        self._show_all_frames: bool = False

        # Live preview cache for the transparent-colour picker. The picker
        # reads these via the bound source-provider callback on click.
        self._preview_src_qimage: Optional[QImage] = None
        self._preview_src_size: Tuple[int, int] = (0, 0)
        self._preview_pix_size: Tuple[int, int] = (0, 0)

        # Precompute decorated labels once (parse_mchr_chr_entry × 890 is a
        # few hundred ms — cheap enough at open, lets the filter box match
        # tokens like "32×64" or "8f" from the start).
        self._labels: List[str] = self._build_index_labels()

        self._build_ui()
        self._list.select_first()

    # ---- labels ---------------------------------------------------------

    def _build_index_labels(self) -> List[str]:
        out: List[str] = []
        for ix in range(self._count):
            out.append(self._compute_index_label(ix))
        return out

    def _compute_index_label(self, ix: int) -> str:
        prefix = f"{ix:04d}"
        entry = _decoded_entry(self._chr_pak, ix)
        if entry is None:
            return f"{prefix}  (parse error)"
        tc = entry.tiles_per_frame
        wt, ht = mchr.pick_tile_grid(tc)
        size_token = f"{wt * 8}×{ht * 8}"
        return f"{prefix}  {entry.frame_count}f {size_token}"

    # ---- UI -------------------------------------------------------------

    def _build_ui(self) -> None:
        self._list = RecordListPanel(
            records=list(range(self._count)),
            label_for=lambda ix, _rec: self._labels[ix],
        )
        self._list.indexSelected.connect(self._on_index_selected)

        self._image_label = QLabel("Select a sprite to preview.")
        self._image_label.setAlignment(Qt.AlignCenter)
        self._image_label.setMinimumSize(256, 256)

        self._scroll = QScrollArea()
        self._scroll.setWidget(self._image_label)
        self._scroll.setWidgetResizable(True)
        self._scroll.setAlignment(Qt.AlignCenter)

        # Frame navigation: single-frame view by default (spinner picks the
        # frame), or a horizontal strip of every frame (cheap "play whole
        # animation" affordance without a timer).
        self._frame_spin = QSpinBox()
        self._frame_spin.setRange(0, 0)
        self._frame_spin.valueChanged.connect(self._on_frame_changed)

        self._all_frames_check = QCheckBox("Show all frames (strip)")
        self._all_frames_check.toggled.connect(self._on_all_frames_toggled)

        # Palette index — the key control. Default tracks sprite index for
        # 0..662 (vanilla 1:1 mapping); past that the user pins it manually
        # until we figure out the real mapping table.
        self._palette_spin = QSpinBox()
        self._palette_spin.setRange(0, max(0, self._pal_pak.count - 1))
        self._palette_spin.valueChanged.connect(self._on_palette_changed)

        # Width-tiles override: 0 means "use NDS-OAM heuristic". 1..16 forces
        # a specific tile width for entries whose ambiguous shape (e.g. 4
        # tiles could be 16×16, 32×8, or 8×32) read wrong with the default.
        self._width_spin = QSpinBox()
        self._width_spin.setRange(0, 16)
        self._width_spin.setSpecialValueText("auto")
        self._width_spin.setValue(0)
        self._width_spin.valueChanged.connect(self._on_width_changed)

        controls = QFormLayout()
        controls.addRow("Frame", self._frame_spin)
        controls.addRow("", self._all_frames_check)
        controls.addRow("Palette index", self._palette_spin)
        controls.addRow("Width (tiles)", self._width_spin)

        # Metadata block, fixed width so switching sprites doesn't reflow.
        self._meta_frames = QLabel("—")
        self._meta_dims = QLabel("—")
        self._meta_tiles = QLabel("—")
        self._meta_chr_size = QLabel("—")
        fm = self._meta_chr_size.fontMetrics()
        worst_width = fm.horizontalAdvance("999999B compressed / 9999999B raw")
        for lbl in (
            self._meta_frames, self._meta_dims, self._meta_tiles, self._meta_chr_size,
        ):
            lbl.setMinimumWidth(worst_width)

        meta_form = QFormLayout()
        meta_form.addRow("Frames", self._meta_frames)
        meta_form.addRow("Frame size", self._meta_dims)
        meta_form.addRow("Tiles/frame", self._meta_tiles)
        meta_form.addRow("CHR entry bytes", self._meta_chr_size)

        # PNG round-trip: frames sheet (one PNG per sprite, all frames in a
        # horizontal strip) and a companion palette PNG (16×1 RGB888). Two
        # separate buttons rather than one combined export — users editing
        # only the palette shouldn't have to round-trip the whole sheet.
        self._export_btn = QPushButton("Export PNG…")
        self._export_btn.clicked.connect(self._on_export_png)
        self._import_btn = QPushButton("Import PNG…")
        self._import_btn.clicked.connect(self._on_import_png)
        self._export_pal_btn = QPushButton("Export palette PNG…")
        self._export_pal_btn.clicked.connect(self._on_export_palette_png)
        self._import_pal_btn = QPushButton("Import palette PNG…")
        self._import_pal_btn.clicked.connect(self._on_import_palette_png)
        # Same checkbox as BTCHR: treat the imported PNG as the source of
        # truth for colours. Indexed → use PLTE; RGB/RGBA → median-cut a
        # fresh 16-colour palette from opaque pixels. Off → posterize
        # against the existing MCHR_PAL.
        self._import_pal_with_sheet_cb = QCheckBox("Also import palette from PNG")
        self._import_pal_with_sheet_cb.setChecked(True)
        self._import_pal_with_sheet_cb.setToolTip(
            "Treat the PNG as the source of truth for colours.\n"
            "  Indexed-8 PNG: rebuild MCHR_PAL from its embedded palette.\n"
            "  RGB/RGBA PNG: median-cut a fresh 16-colour palette from "
            "the PNG's opaque pixels and re-index.\n"
            "Off: index against the existing MCHR_PAL (colours may posterize)."
        )

        # Two columns: frames sheet (left) and palette PNG (right). All
        # four buttons pinned to the widest label so Export/Import line
        # up across columns.
        sheet_btns = (self._export_btn, self._import_btn)
        pal_btns = (self._export_pal_btn, self._import_pal_btn)
        max_btn_w = max(
            b.sizeHint().width() for b in sheet_btns + pal_btns
        )
        for b in sheet_btns + pal_btns:
            b.setMinimumWidth(max_btn_w)
        sheet_col = QVBoxLayout()
        sheet_col.setSpacing(4)
        sheet_col.addWidget(self._export_btn)
        sheet_col.addWidget(self._import_btn)
        sheet_col.addWidget(self._import_pal_with_sheet_cb)
        sheet_col.addStretch(1)
        pal_col = QVBoxLayout()
        pal_col.setSpacing(4)
        pal_col.addWidget(self._export_pal_btn)
        pal_col.addWidget(self._import_pal_btn)
        pal_col.addStretch(1)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(8, 8, 8, 8)
        right_layout.addWidget(self._scroll, 1)

        # Single row under the preview: nav controls, then the two
        # button columns, then the metadata block, then stretch. Keeps
        # everything that fits on one line and pushes the metadata to
        # the right edge of the pane.
        controls_row = QHBoxLayout()
        controls_row.addLayout(controls)
        controls_row.addSpacing(16)
        controls_row.addLayout(sheet_col)
        controls_row.addSpacing(16)
        controls_row.addLayout(pal_col)
        controls_row.addSpacing(16)
        controls_row.addLayout(meta_form)
        controls_row.addStretch(1)
        right_layout.addLayout(controls_row)

        # Transparent-colour picker. The apply step is MCHR-specific
        # (4bpp nibble remap + MCHR_PAL write-back) so we pass our own
        # `_apply_transparent_color` callback. The widget itself just
        # provides UI + click capture on the bound preview.
        self._picker = TransparentColorPicker(
            on_color_picked=self._apply_transparent_color
        )
        self._picker.bind_preview(self._image_label, self._preview_source)
        right_layout.addWidget(self._picker)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._list)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([220, 800])

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(splitter)

    # ---- selection / refresh -------------------------------------------

    def _on_index_selected(self, ix: int) -> None:
        self._current_idx = ix
        self._current_frame = 0
        # Default palette tracks sprite index — correct for 0..662, an
        # informed guess past that until the user picks a better one.
        default_pal = min(ix, self._pal_pak.count - 1)
        self._current_palette_idx = default_pal
        # Block signals during programmatic set so we only refresh once.
        self._palette_spin.blockSignals(True)
        self._palette_spin.setValue(default_pal)
        self._palette_spin.blockSignals(False)
        # Width-tiles override is per-sprite — different sprites have
        # different ambiguous shapes, so a value picked for sprite N
        # would mis-render sprite N+1. Restore the user's last pick for
        # this sprite, or "auto" (None) if they haven't set one.
        remembered = self._width_overrides.get(ix)
        self._width_tiles_override = remembered
        self._width_spin.blockSignals(True)
        self._width_spin.setValue(remembered if remembered is not None else 0)
        self._width_spin.blockSignals(False)
        self._refresh_meta_and_preview()

    def _on_frame_changed(self, value: int) -> None:
        self._current_frame = value
        self._refresh_preview_only()

    def _on_palette_changed(self, value: int) -> None:
        self._current_palette_idx = value
        self._refresh_preview_only()

    def _on_width_changed(self, value: int) -> None:
        self._width_tiles_override = value if value > 0 else None
        if self._current_idx is not None:
            self._width_overrides[self._current_idx] = self._width_tiles_override
        self._refresh_preview_only()

    def _on_all_frames_toggled(self, checked: bool) -> None:
        self._show_all_frames = checked
        # Spinner is meaningless in strip mode; gray it out for clarity.
        self._frame_spin.setEnabled(not checked)
        self._refresh_preview_only()

    def _refresh_meta_and_preview(self) -> None:
        if self._current_idx is None:
            return
        entry = _decoded_entry(self._chr_pak, self._current_idx)
        if entry is None:
            self._image_label.setText("(parse error)")
            self._meta_frames.setText("—")
            self._meta_dims.setText("—")
            self._meta_tiles.setText("—")
            self._meta_chr_size.setText("—")
            return
        wt, ht = mchr.pick_tile_grid(entry.tiles_per_frame)
        self._meta_frames.setText(str(entry.frame_count))
        self._meta_dims.setText(f"{wt * 8}×{ht * 8}")
        self._meta_tiles.setText(str(entry.tiles_per_frame))
        compressed_n = len(self._chr_pak.entries[self._current_idx])
        raw_n = 8 + entry.frame_count * entry.bytes_per_frame
        self._meta_chr_size.setText(
            f"{compressed_n}B compressed / {raw_n}B raw"
        )

        self._frame_spin.blockSignals(True)
        self._frame_spin.setRange(0, max(0, entry.frame_count - 1))
        self._frame_spin.setValue(0)
        self._frame_spin.setEnabled(not self._show_all_frames)
        self._frame_spin.blockSignals(False)

        self._refresh_preview_only()

    def _refresh_preview_only(self) -> None:
        if self._current_idx is None:
            self._preview_src_qimage = None
            self._picker.set_current_color(None)
            return
        entry = _decoded_entry(self._chr_pak, self._current_idx)
        if entry is None:
            self._preview_src_qimage = None
            return
        palette = _decoded_palette(self._pal_pak, self._current_palette_idx)
        if palette is None:
            self._image_label.setText("(palette decode failed)")
            self._preview_src_qimage = None
            self._picker.set_current_color(None)
            return

        wt_override = self._width_tiles_override
        if self._show_all_frames:
            pixmap = self._render_frame_strip(entry, palette, wt_override)
        else:
            frame_i = min(self._current_frame, entry.frame_count - 1)
            pixmap = self._render_single_frame(
                entry.frames[frame_i], palette, wt_override
            )
        # Render at 4× nominal to make 16×32 sprites comfortably visible
        # in a 256+ minimum preview pane; QImage's nearest-neighbor scaling
        # keeps pixel edges sharp (no smoothing).
        scaled = pixmap.scaled(
            pixmap.width() * 4, pixmap.height() * 4,
            Qt.KeepAspectRatio, Qt.FastTransformation,
        )
        self._image_label.setPixmap(scaled)
        # Force the QScrollArea to honor the pixmap's size so a wide
        # "show all frames" strip gets a horizontal scroll bar instead
        # of being silently clipped.
        self._image_label.setMinimumSize(scaled.size())

        # Cache source for the eyedropper.
        self._preview_src_qimage = pixmap.toImage()
        self._preview_src_size = (pixmap.width(), pixmap.height())
        self._preview_pix_size = (scaled.width(), scaled.height())
        self._picker.set_current_color(palette[0])

    def _preview_source(self):
        if self._preview_src_qimage is None:
            return None
        return (
            self._preview_src_qimage,
            self._preview_src_size,
            self._preview_pix_size,
        )

    # ---- rendering ------------------------------------------------------

    @staticmethod
    def _render_single_frame(
        frame_bytes: bytes,
        palette: mchr.Palette,
        width_tiles_override: Optional[int],
    ) -> QPixmap:
        rgba, w, h = mchr.render_frame_rgba(
            frame_bytes, palette, width_tiles=width_tiles_override,
        )
        img = QImage(rgba, w, h, w * 4, QImage.Format_RGBA8888).copy()
        return QPixmap.fromImage(img)

    def _frame_dims(self, entry: mchr.MchrEntry) -> Tuple[int, int]:
        """Return ``(frame_w_px, frame_h_px)`` for the current width override."""
        if self._width_tiles_override is not None:
            wt = self._width_tiles_override
            ht = (entry.tiles_per_frame + wt - 1) // wt
        else:
            wt, ht = mchr.pick_tile_grid(entry.tiles_per_frame)
        return wt * 8, ht * 8

    # ---- PNG export / import -------------------------------------------

    def _on_export_png(self) -> None:
        if self._current_idx is None:
            return
        entry = _decoded_entry(self._chr_pak, self._current_idx)
        palette = _decoded_palette(self._pal_pak, self._current_palette_idx)
        if entry is None or palette is None:
            QMessageBox.critical(self, "Export failed", "Could not decode sprite/palette.")
            return
        suggested = f"mchr_chr_{self._current_idx:04d}.png"
        path, _ = QFileDialog.getSaveFileName(
            self, "Export sprite PNG", suggested, "PNG (*.png)"
        )
        if not path:
            return
        img = self._build_indexed_strip(entry, palette)
        if not img.save(path, "PNG"):
            QMessageBox.critical(self, "Export failed", f"Could not write {path}.")

    def _on_import_png(self) -> None:
        if self._current_idx is None:
            return
        entry = _decoded_entry(self._chr_pak, self._current_idx)
        palette = _decoded_palette(self._pal_pak, self._current_palette_idx)
        if entry is None or palette is None:
            QMessageBox.critical(self, "Import failed", "Current sprite/palette won't decode.")
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Import sprite PNG", "", "PNG (*.png)"
        )
        if not path:
            return
        img = QImage(path)
        if img.isNull():
            QMessageBox.critical(self, "Import failed", f"Could not read {path}.")
            return

        fw, fh = self._frame_dims(entry)
        # Strict: height must match exactly, width must divide cleanly into
        # frames of the current shape. Frame count is free — users can add
        # or drop frames by widening / narrowing the sheet.
        if img.height() != fh:
            QMessageBox.critical(
                self, "Bad image size",
                f"Expected height {fh} px (got {img.height()}). "
                f"Adjust the Width (tiles) spinner if the sprite shape is wrong.",
            )
            return
        if img.width() <= 0 or img.width() % fw != 0:
            QMessageBox.critical(
                self, "Bad image size",
                f"Width must be a positive multiple of {fw} px "
                f"(one frame). Got {img.width()}.",
            )
            return
        n_frames = img.width() // fw

        # Indexed-8 PNG → use stored indices straight (round-trips
        # bit-exact through an Aseprite/GIMP edit that stayed in indexed
        # mode). Any other format → convert to RGBA and quantize.
        use_indexed = img.format() == QImage.Format_Indexed8
        if not use_indexed:
            img = img.convertToFormat(QImage.Format_RGBA8888)

        # Decide where colours come from. See the BTCHR importer for the
        # mirrored decision tree — same shape, smaller palette (16 vs 256).
        checkbox_on = self._import_pal_with_sheet_cb.isChecked()
        pal_from_plte = (
            use_indexed and checkbox_on and len(img.colorTable()) >= 2
        )
        pal_from_quant = (not use_indexed) and checkbox_on
        rebuild_palette = pal_from_plte or pal_from_quant

        if rebuild_palette:
            built = build_palette_from_png(img, total_slots=PALETTE_SLOTS)
            if built is None:
                QMessageBox.critical(
                    self, "PNG is fully transparent",
                    "Cannot rebuild a palette from a PNG with no opaque pixels.",
                )
                return
            working_palette: mchr.Palette = list(built)
        else:
            working_palette = list(palette)

        new_frames: List[bytes] = []
        for fi in range(n_frames):
            x0 = fi * fw
            if use_indexed:
                indices = [
                    img.pixelIndex(x0 + x, y)
                    for y in range(fh) for x in range(fw)
                ]
            elif pal_from_quant:
                # Inline opaque-only nearest-match against [1:] so slot 0
                # never absorbs an opaque pixel that happens to be black.
                indices = []
                for y in range(fh):
                    for x in range(fw):
                        c = img.pixelColor(x0 + x, y)
                        if c.alpha() < 128:
                            indices.append(0)
                        else:
                            indices.append(nearest_idx_opaque(
                                c.red(), c.green(), c.blue(), working_palette,
                            ))
            else:
                rgba_buf = bytearray(fw * fh * 4)
                for y in range(fh):
                    for x in range(fw):
                        c = img.pixelColor(x0 + x, y)
                        off = (y * fw + x) * 4
                        rgba_buf[off] = c.red()
                        rgba_buf[off + 1] = c.green()
                        rgba_buf[off + 2] = c.blue()
                        rgba_buf[off + 3] = c.alpha()
                indices = mchr.quantize_rgba_to_indices(bytes(rgba_buf), palette)
            new_frames.append(mchr.encode_frame_from_indices(indices, fw, fh))

        try:
            new_raw = mchr.encode_mchr_chr_entry(new_frames)
        except ValueError as exc:
            QMessageBox.critical(self, "Encode failed", str(exc))
            return

        replacements = [
            (MCHR_CHR, self._current_idx, sprite.compress_rle30(new_raw)),
        ]
        if rebuild_palette:
            encoded_pal = mchr.encode_palette_bgr555(working_palette)
            replacements.append((
                MCHR_PAL,
                self._current_palette_idx,
                sprite.compress_rle30(encoded_pal),
            ))
            desc = (
                f"Import MCHR sprite {self._current_idx:04d} + palette "
                f"{self._current_palette_idx:04d}"
            )
        else:
            desc = f"Import MCHR sprite {self._current_idx:04d}"

        cmd = ReplaceSpriteCommand(
            self._session,
            replacements,
            description=desc,
            on_change=self._on_chr_entry_replaced,
        )
        if self._undo_stack is not None:
            self._undo_stack.push(cmd)
        else:
            cmd.redo()

    def _on_export_palette_png(self) -> None:
        palette = _decoded_palette(self._pal_pak, self._current_palette_idx)
        if palette is None:
            QMessageBox.critical(self, "Export failed", "Could not decode palette.")
            return
        suggested = f"mchr_pal_{self._current_palette_idx:04d}.png"
        path, _ = QFileDialog.getSaveFileName(
            self, "Export palette PNG", suggested, "PNG (*.png)"
        )
        if not path:
            return
        # 16×1 RGB888 — one pixel per palette slot. Trivial to edit in any
        # image editor (zoom in and recolour the strip).
        img = QImage(len(palette), 1, QImage.Format_RGB888)
        for i, (r, g, b) in enumerate(palette):
            img.setPixelColor(i, 0, QColor(r, g, b))
        if not img.save(path, "PNG"):
            QMessageBox.critical(self, "Export failed", f"Could not write {path}.")

    def _on_import_palette_png(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Import palette PNG", "", "PNG (*.png)"
        )
        if not path:
            return
        img = QImage(path)
        if img.isNull():
            QMessageBox.critical(self, "Import failed", f"Could not read {path}.")
            return
        if img.width() < 16 or img.height() < 1:
            QMessageBox.critical(
                self, "Bad image size",
                f"Palette PNG must be at least 16×1 (got {img.width()}×{img.height()}).",
            )
            return
        img = img.convertToFormat(QImage.Format_RGBA8888)
        # Sample the top row's first 16 pixels — supports both a 16×1 strip
        # and a wider/taller editor canvas (e.g. 256×16 with 16-pixel-wide
        # swatches: column 0 of each swatch is its colour).
        cell_w = img.width() // 16
        colours: mchr.Palette = []
        for i in range(16):
            c = img.pixelColor(i * cell_w, 0)
            colours.append((c.red(), c.green(), c.blue()))
        encoded = mchr.encode_palette_bgr555(colours)
        compressed = sprite.compress_rle30(encoded)
        cmd = ReplaceSpriteCommand(
            self._session,
            [(MCHR_PAL, self._current_palette_idx, compressed)],
            description=f"Import MCHR palette {self._current_palette_idx:04d}",
            on_change=self._refresh_preview_only,
        )
        if self._undo_stack is not None:
            self._undo_stack.push(cmd)
        else:
            cmd.redo()

    # ---- transparent-colour apply --------------------------------------

    @staticmethod
    def _snap5(rgb: Tuple[int, int, int]) -> Tuple[int, int, int]:
        """Project an 8-bit RGB triple onto MCHR_PAL's 5-bit grid.
        Two RGBs that snap to the same triple encode to identical
        BGR555 bytes — used for equality checks against on-disk
        palette colours."""
        def s(v: int) -> int:
            return (v * 31 + 127) // 255 * 255 // 31
        return (s(rgb[0]), s(rgb[1]), s(rgb[2]))

    @staticmethod
    def _remap_nibbles(frame_bytes: bytes, src: int, dst: int) -> bytes:
        """Rewrite every 4bpp nibble equal to ``src`` as ``dst``.
        MCHR packs two pixels per byte (low nibble = left, high =
        right), so naive byte-equality won't catch boundary cases."""
        src &= 0x0F
        dst &= 0x0F
        out = bytearray(len(frame_bytes))
        for i, b in enumerate(frame_bytes):
            lo = b & 0x0F
            hi = (b >> 4) & 0x0F
            if lo == src:
                lo = dst
            if hi == src:
                hi = dst
            out[i] = (hi << 4) | lo
        return bytes(out)

    def _apply_transparent_color(self, rgb: Tuple[int, int, int]) -> None:
        """Make ``rgb`` the current palette's transparent (slot 0) colour.

        Mirror of BTCHR's two-part edit, adapted for MCHR's 4bpp +
        per-palette layout:

        1. MCHR_PAL[pal_idx] slot 0 ← ``rgb``.
        2. If ``rgb`` already lives at slot K (1..15), every 4bpp nibble
           in the current MCHR_CHR entry pointing at K is remapped to 0,
           so the engine actually renders those pixels transparent.

        No-op when slot 0 already matches AND no remap candidate exists.
        """
        if self._current_idx is None:
            return
        palette = _decoded_palette(self._pal_pak, self._current_palette_idx)
        entry = _decoded_entry(self._chr_pak, self._current_idx)
        if palette is None or entry is None:
            return
        target_snap = self._snap5(rgb)

        source_idx: Optional[int] = None
        for si in range(1, len(palette)):
            if self._snap5(palette[si]) == target_snap:
                source_idx = si
                break

        if self._snap5(palette[0]) == target_snap and source_idx is None:
            return

        new_palette: mchr.Palette = list(palette)
        new_palette[0] = rgb
        encoded_pal = mchr.encode_palette_bgr555(new_palette)

        replacements = [(
            MCHR_PAL,
            self._current_palette_idx,
            sprite.compress_rle30(encoded_pal),
        )]

        if source_idx is not None:
            new_frames = [
                self._remap_nibbles(f, source_idx, 0) for f in entry.frames
            ]
            try:
                new_raw = mchr.encode_mchr_chr_entry(new_frames)
            except ValueError as exc:
                QMessageBox.critical(self, "Encode failed", str(exc))
                return
            replacements.append((
                MCHR_CHR,
                self._current_idx,
                sprite.compress_rle30(new_raw),
            ))

        desc = (
            f"Set transparent color for MCHR palette "
            f"{self._current_palette_idx:04d}"
        )
        cmd = ReplaceSpriteCommand(
            self._session,
            replacements,
            description=desc,
            on_change=self._on_chr_entry_replaced,
        )
        if self._undo_stack is not None:
            self._undo_stack.push(cmd)
        else:
            cmd.redo()

    def _on_chr_entry_replaced(self) -> None:
        """Post-replace hook: rebuild the touched label + refresh preview.

        Frame count can change on import (wider sheet = more frames), so the
        list label ``"NNNN  Nf W×H"`` may be stale. Recompute just the
        affected row and the metadata block.
        """
        if self._current_idx is not None:
            self._labels[self._current_idx] = self._compute_index_label(
                self._current_idx
            )
            self._list.refresh_label(self._current_idx)
        self._refresh_meta_and_preview()

    def _build_indexed_strip(
        self, entry: mchr.MchrEntry, palette: mchr.Palette
    ) -> QImage:
        """Pack every frame into a single ``Format_Indexed8`` strip QImage.

        Index 0 gets alpha=0 in the colour table so the saved PNG carries a
        tRNS chunk — a render → edit → re-import loop preserves
        transparent regions without going through the quantizer.
        """
        fw, fh = self._frame_dims(entry)
        strip_w = fw * entry.frame_count
        img = QImage(strip_w, fh, QImage.Format_Indexed8)
        ctable = [
            qRgba(r, g, b, 0 if i == 0 else 255)
            for i, (r, g, b) in enumerate(palette)
        ]
        img.setColorTable(ctable)
        for fi, fb in enumerate(entry.frames):
            indices, w, h = mchr.decode_frame_to_indices(
                fb, self._width_tiles_override
            )
            x0 = fi * fw
            for y in range(h):
                for x in range(w):
                    img.setPixel(x0 + x, y, indices[y * w + x])
        return img

    @staticmethod
    def _render_frame_strip(
        entry: mchr.MchrEntry,
        palette: mchr.Palette,
        width_tiles_override: Optional[int],
    ) -> QPixmap:
        """Paint every frame side-by-side into a single QPixmap.

        Render each frame independently then blit into the strip — keeps
        the per-frame tile-grid logic untouched and lets us add a 1px gutter
        for visual frame separation without affecting the codec path.
        """
        frame_pixmaps: List[QPixmap] = []
        max_h = 0
        for f in entry.frames:
            rgba, w, h = mchr.render_frame_rgba(
                f, palette, width_tiles=width_tiles_override,
            )
            img = QImage(rgba, w, h, w * 4, QImage.Format_RGBA8888).copy()
            frame_pixmaps.append(QPixmap.fromImage(img))
            max_h = max(max_h, h)
        gutter = 1
        total_w = sum(p.width() for p in frame_pixmaps) + gutter * max(
            0, len(frame_pixmaps) - 1
        )
        strip = QImage(total_w, max_h, QImage.Format_RGBA8888)
        strip.fill(0)
        from PySide6.QtGui import QPainter
        painter = QPainter(strip)
        x = 0
        for pm in frame_pixmaps:
            painter.drawPixmap(x, 0, pm)
            x += pm.width() + gutter
        painter.end()
        return QPixmap.fromImage(strip)
