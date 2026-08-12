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

The per-sprite palette index defaults to the sprite's canonical palette,
resolved from the ARM9 overworld-sprite table
(``session.mchr_pal_for_chr`` / :func:`loaders.loadMchrChrToPalMap`). CHR→PAL
is 1:1 for sprites 0..662, then drifts past 0x0297 because 13 sprites carry
2-3 recolor palettes (e.g. CHR 0x2ed → PAL 0x2fb). The spinner still lets
users pin an alternate palette (e.g. a multi-palette sprite's other recolor).

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

import os
import re
from typing import List, Optional, Tuple

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import (
    QColor,
    QImage,
    QPainter,
    QPen,
    QPixmap,
    QTransform,
    QUndoStack,
    qRgba,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QMessageBox,
    QFrame,
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

from digimon_core import mchr, mchr_anm, pak, sprite

from ..commands import ReplaceSpriteCommand, SetMchrOwPalCommand
from ._png_palette import (
    build_palette_from_png,
    intensity_matched_palette,
    nearest_idx_opaque,
)
from .palette_batch_adjuster import PaletteBatchAdjuster
from .palette_editor import PaletteEditor
from .palette_grid import PaletteGrid
from .palette_toolkit import (
    DEFAULT_COLS_MIN,
    DEFAULT_SCROLL_MAX_H,
    DEFAULT_SWATCH,
    PaletteToolkit,
)
from .flow_layout import FlowLayout, make_height_for_width
from .form_helpers import ReflowHeightSync
from .record_list_panel import RecordListPanel
from .transparent_picker import TransparentColorPicker


# MCHR palettes are 16 colors, slot 0 reserved transparent.
PALETTE_SLOTS = 16


MCHR_CHR = "DAT/MCHR_CHR.PAK"
MCHR_PAL = "DAT/MCHR_PAL.PAK"
MCHR_ANM = "DAT/MCHR_ANM.PAK"


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


def _format_mchr_label(prefix: str, role_token: str, metadata: str) -> str:
    """Compose an MCHR_CHR list label. Format with role: ``"{prefix}  [OW]
    {name} - {metadata}"``. Format without role: ``"{prefix}  {metadata}"``."""
    if role_token:
        return f"{prefix}  {role_token} - {metadata}"
    return f"{prefix}  {metadata}"


def compute_mchr_labels(session) -> List[str]:
    """Public helper: MCHR_CHR label list (one per entry). Mirrors the
    browser's `_labels` so other widgets can populate pickers without
    instantiating a viewer."""
    chr_pak = session.sprite_pak(MCHR_CHR)
    # Frozen at session load — see RomSession.sprite_attribution. Means
    # reassigning a digimon's overworld sprite later doesn't relabel
    # the original sprite.
    overworld_to_base = session.sprite_attribution()["unknown_0x4"]

    out: List[str] = []
    for ix in range(chr_pak.count):
        prefix = f"0x{ix:04x}"
        base_id = overworld_to_base.get(ix)
        role = ""
        if base_id is not None:
            # `digimon_display_name` covers both digimon (DIGIMON_ID_TO_STR)
            # and NPC slots (battle-string fallback) — NPCs occupy
            # sprite_map 0x30e..0x363, where the slot's `unknown_0x4`
            # points back at the MCHR index, so this single lookup
            # handles Glare/Kogure/etc without a special path.
            role = f"[OW] {session.digimon_display_name(base_id)}"
        entry = _decoded_entry(chr_pak, ix)
        if entry is None:
            out.append(_format_mchr_label(prefix, role, "(parse error)"))
            continue
        wt, ht = mchr.pick_tile_grid(entry.tiles_per_frame)
        size_token = f"{wt * 8}×{ht * 8}"
        out.append(_format_mchr_label(prefix, role, f"{entry.frame_count}f {size_token}"))
    return out


class MchrBrowser(QWidget):
    """Read-only browser for the MCHR_CHR + MCHR_PAL overworld-sprite pair."""

    _CURSOR_KEY = "mchr_browser"

    def __init__(self, session, undo_stack: Optional[QUndoStack] = None, parent=None):
        super().__init__(parent)
        self._session = session
        self._undo_stack = undo_stack  # reserved for the import phase
        self._chr_pak: pak.PakFile = session.sprite_pak(MCHR_CHR)
        self._pal_pak: pak.PakFile = session.sprite_pak(MCHR_PAL)
        # MCHR_ANM is the parallel animation pak (one entry per sprite). Read
        # for the Animation tab; not part of the frame preview pipeline.
        self._anm_pak: pak.PakFile = session.sprite_pak(MCHR_ANM)
        self._count = self._chr_pak.count

        self._current_idx: Optional[int] = None
        self._current_frame: int = 0
        self._current_palette_idx: int = 0
        self._current_ow_id: int = 0  # canonical ow-id whose palette the spinner assigns
        # Non-None while the batch adjuster drags: the current palette with the
        # selected slots recoloured, for a live preview without committing.
        self._preview_palette: Optional[mchr.Palette] = None

        # Animation playback state (MCHR_ANM). ``_anim_flat`` is one frame
        # per output tick; the timer advances ``_anim_pos`` and paints the
        # named MCHR_CHR frame into the Animation tab's preview. 60 fps to
        # match the tick basis the durations are counted in.
        self._anim: Optional[mchr_anm.MchrAnm] = None
        self._anim_idx: int = 0
        self._anim_flat: List[mchr_anm.MchrAnimFrame] = []
        self._anim_pos: int = 0
        self._anim_fps: int = 60
        self._anim_editing_frame: int = -1
        self._anim_table_loading: bool = False
        self._anim_params_loading: bool = False
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

        # Reverse lookup: MCHR_CHR index -> first sprite-map slot that
        # points at it. SpriteMapEntry.unknown_0x4 is the party-follower
        # overworld sprite for that digimon (fixed NPC encounters and
        # scripted map events use their own sources, but the field IS
        # read for the digimon trailing the player). The slot's list
        # position is the digimon id, so this turns "0x0042" into
        # "Koromon". Recolors share sprites; setdefault keeps the first.
        self._overworld_to_base: dict[int, int] = {}
        for base_id, entry in enumerate(getattr(session, "sprite_map", [])):
            self._overworld_to_base.setdefault(entry.unknown_0x4, base_id)

        # Precompute decorated labels once (parse_mchr_chr_entry × 890 is a
        # few hundred ms — cheap enough at open, lets the filter box match
        # tokens like "32×64" or "8f" from the start).
        self._labels: List[str] = self._build_index_labels()

        self._build_ui()
        remembered = self._session.recall_selection(self._CURSOR_KEY)
        if remembered is None or not self._list.select_index(int(remembered)):
            self._list.select_first()

    # ---- labels ---------------------------------------------------------

    def _build_index_labels(self) -> List[str]:
        out: List[str] = []
        for ix in range(self._count):
            out.append(self._compute_index_label(ix))
        return out

    def _compute_index_label(self, ix: int) -> str:
        prefix = f"0x{ix:04x}"
        role_token = self._role_tag(ix)
        entry = _decoded_entry(self._chr_pak, ix)
        if entry is None:
            return _format_mchr_label(prefix, role_token, "(parse error)")
        tc = entry.tiles_per_frame
        wt, ht = mchr.pick_tile_grid(tc)
        size_token = f"{wt * 8}×{ht * 8}"
        return _format_mchr_label(prefix, role_token, f"{entry.frame_count}f {size_token}")

    def _role_tag(self, ix: int) -> str:
        """`[OW] <name>` for MCHR indices referenced by any sprite_map
        slot. `digimon_display_name` covers both digimon and NPC slots
        (NPCs live in sprite_map 0x30e..0x363 with their `unknown_0x4`
        pointing at the MCHR index), so a single lookup labels Glare,
        Kogure, Veemon, etc. uniformly. Empty for unreferenced utility
        / scene sprites."""
        base_id = self._overworld_to_base.get(ix)
        if base_id is None:
            return ""
        return f"[OW] {self._session.digimon_display_name(base_id)}"

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
        self._scroll.setWidgetResizable(False)
        self._scroll.setAlignment(Qt.AlignCenter)

        # Frame navigation: single-frame view by default (spinner picks the
        # frame), or a horizontal strip of every frame (cheap "play whole
        # animation" affordance without a timer).
        self._frame_spin = QSpinBox()
        self._frame_spin.setRange(0, 0)
        self._frame_spin.valueChanged.connect(self._on_frame_changed)

        self._all_frames_check = QCheckBox("Show all frames (strip)")
        self._all_frames_check.toggled.connect(self._on_all_frames_toggled)

        # In-app "add a frame": duplicate the current frame, growing the CHR
        # entry. Lets a single-frame sprite gain frames to animate between
        # without a PNG round-trip. (A wider frames-sheet import also adds
        # frames — this is the quick path.) Hidden in read-only mode.
        self._dup_frame_btn = QPushButton("+ Duplicate frame")
        self._dup_frame_btn.setToolTip(
            "Append a copy of the current frame, growing this sprite's frame "
            "count. Edit it via the Frames PNG tools, then reference it from "
            "the Animation tab."
        )
        self._dup_frame_btn.clicked.connect(self._on_duplicate_frame)
        if self._undo_stack is None:
            self._dup_frame_btn.setVisible(False)

        # Palette index — which MCHR_PAL the sprite is viewed/edited under
        # (canonical by default). To give it another palette, use the Borrow-
        # palette section (preview + copy the colours in).
        self._palette_spin = QSpinBox()
        self._palette_spin.setRange(0, max(0, self._pal_pak.count - 1))
        self._palette_spin.setToolTip(
            "Which palette to render this sprite under and edit. Copy another "
            "palette in via the Borrow-palette section below."
        )
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
        controls.addRow("", self._dup_frame_btn)
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
        self._export_btn = QPushButton("Export frames sheet PNG…")
        self._export_btn.clicked.connect(self._on_export_png)
        self._import_btn = QPushButton("Import frames sheet PNG…")
        self._import_btn.clicked.connect(self._on_import_png)
        self._export_pal_btn = QPushButton("Export palette PNG…")
        self._export_pal_btn.clicked.connect(self._on_export_palette_png)
        self._import_pal_btn = QPushButton("Import palette PNG…")
        self._import_pal_btn.clicked.connect(self._on_import_palette_png)
        # Per-frame IO: one PNG per frame instead of a horizontal strip.
        # Same codec as the strip path; the on-disk files are sized to the
        # current frame dimensions so they're swappable between sprites
        # with matching shapes.
        self._export_per_frame_btn = QPushButton("Export per-frame PNGs…")
        self._export_per_frame_btn.clicked.connect(self._on_export_per_frame_pngs)
        self._import_per_frame_btn = QPushButton("Import per-frame PNGs…")
        self._import_per_frame_btn.clicked.connect(self._on_import_per_frame_pngs)
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

        # "Export…" / "Import…" dropdowns front every mode, mirroring Battle
        # Sprites / Icons. The individual buttons stay in code (nothing deleted)
        # — they just back the menu rows and aren't laid out.
        self._export_menu = QMenu(self)
        self._export_menu.addAction(
            "Sprite sheet (all frames, one PNG)…", self._on_export_png)
        self._export_menu.addAction(
            "Individual frames (one PNG each)…", self._on_export_per_frame_pngs)
        self._export_menu.addSeparator()
        self._export_menu.addAction("Palette PNG…", self._on_export_palette_png)
        self._export_as_btn = QPushButton("Export…")
        self._export_as_btn.setMenu(self._export_menu)

        self._import_menu = QMenu(self)
        self._import_menu.addAction(
            "Sprite sheet (all frames, one PNG)…", self._on_import_png)
        self._import_menu.addAction(
            "Individual frames (one PNG each)…", self._on_import_per_frame_pngs)
        self._import_menu.addSeparator()
        self._import_menu.addAction("Palette PNG…", self._on_import_palette_png)
        self._import_as_btn = QPushButton("Import…")
        self._import_as_btn.setMenu(self._import_menu)

        for _superseded in (
            self._export_btn, self._import_btn,
            self._export_per_frame_btn, self._import_per_frame_btn,
            self._export_pal_btn, self._import_pal_btn,
        ):
            _superseded.setParent(self)
            _superseded.setVisible(False)

        io_btns = (self._import_as_btn, self._export_as_btn)
        max_btn_w = max(
            w.sizeHint().width()
            for w in (*io_btns, self._import_pal_with_sheet_cb)
        )
        for b in io_btns:
            b.setFixedWidth(max_btn_w)
        io_col = QVBoxLayout()
        io_col.setSpacing(4)
        io_col.addWidget(self._import_as_btn)
        io_col.addWidget(self._export_as_btn)
        io_col.addWidget(self._import_pal_with_sheet_cb)
        io_col.addStretch(1)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(8, 8, 8, 8)

        # Frame preview lives in a "Frames" tab; the animation player + its
        # editor live in an "Animation" tab (disabled for sprites with no
        # real animation). Mirrors the SPR_* browser's tab split.
        frames_tab = QWidget()
        frames_tab_layout = QVBoxLayout(frames_tab)
        frames_tab_layout.setContentsMargins(0, 0, 0, 0)
        frames_tab_layout.addWidget(self._scroll, 1)

        self._preview_tabs = QTabWidget()
        self._preview_tabs.addTab(frames_tab, "Frames")
        self._preview_tabs.addTab(self._build_anim_tab(), "Animation")
        self._anim_tab_index = self._preview_tabs.count() - 1
        self._preview_tabs.setTabEnabled(self._anim_tab_index, False)
        self._preview_tabs.currentChanged.connect(self._on_preview_tab_changed)
        # Added to a vertical splitter with the controls below (end of this
        # block) so the preview keeps the space and the controls scroll.

        # Single row under the preview: nav controls, then the two
        # button columns, then the metadata block, then stretch. Keeps
        # everything that fits on one line and pushes the metadata to
        # the right edge of the pane.
        # Flow the control groups so they wrap onto a second row when the pane
        # narrows, instead of the panels side by side pinning a wide floor.
        def _panel(inner_layout) -> QWidget:
            holder = QWidget()
            holder.setLayout(inner_layout)
            return holder

        controls_row_w = QWidget()
        controls_flow = FlowLayout(controls_row_w, margin=0, h_spacing=16, v_spacing=8)
        for inner in (controls, io_col, meta_form):
            controls_flow.addWidget(_panel(inner))
        make_height_for_width(controls_row_w)

        # Transparent-colour picker. The apply step is MCHR-specific
        # (4bpp nibble remap + MCHR_PAL write-back) so we pass our own
        # `_apply_transparent_color` callback. The widget itself just
        # provides UI + click capture on the bound preview.
        self._picker = TransparentColorPicker(
            on_color_picked=self._apply_transparent_color,
            on_slot_picked=lambda rgb: self._palette_toolkit.pick_slot_from_rgb(rgb),
        )
        self._picker.bind_preview(self._image_label, self._preview_source)

        # Controls + picker go in a scroll under the preview, split vertically
        # so the pane is compressible top-to-bottom (controls scroll instead of
        # the window growing tall as the panels wrap).
        controls_container = QWidget()
        cc_layout = QVBoxLayout(controls_container)
        cc_layout.setContentsMargins(0, 0, 0, 0)
        cc_layout.addWidget(controls_row_w)
        cc_layout.addWidget(self._picker)
        controls_scroll = QScrollArea()
        controls_scroll.setWidgetResizable(True)
        controls_scroll.setFrameShape(QFrame.NoFrame)
        controls_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        controls_scroll.setWidget(controls_container)
        ReflowHeightSync(controls_container)

        mid_split = QSplitter(Qt.Vertical)
        mid_split.addWidget(self._preview_tabs)
        mid_split.addWidget(controls_scroll)
        mid_split.setStretchFactor(0, 1)
        mid_split.setStretchFactor(1, 0)
        mid_split.setCollapsible(0, False)
        mid_split.setSizes([440, 280])
        right_layout.addWidget(mid_split)

        palette_col = self._build_palette_sidebar()
        # Palette tab: the frame preview (for reference) on the left + the
        # palette editor on the right, so recolouring keeps the image on
        # screen. The preview mirrors the Frames render (_refresh_preview_only).
        # The editor side scrolls so its tall panel doesn't pin the tab widget's
        # minimum height.
        self._palette_preview_label = QLabel("Select a sprite to preview.")
        self._palette_preview_label.setAlignment(Qt.AlignCenter)
        pal_prev_scroll = QScrollArea()
        pal_prev_scroll.setWidgetResizable(False)
        pal_prev_scroll.setAlignment(Qt.AlignCenter)
        pal_prev_scroll.setWidget(self._palette_preview_label)
        pal_edit_scroll = QScrollArea()
        pal_edit_scroll.setWidgetResizable(True)
        pal_edit_scroll.setFrameShape(QFrame.NoFrame)
        pal_edit_scroll.setWidget(palette_col)
        palette_tab = QSplitter(Qt.Horizontal)
        palette_tab.addWidget(pal_prev_scroll)
        palette_tab.addWidget(pal_edit_scroll)
        palette_tab.setStretchFactor(0, 1)
        palette_tab.setStretchFactor(1, 0)
        palette_tab.setSizes([380, 300])
        self._palette_tab_index = self._preview_tabs.addTab(palette_tab, "Palette")

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._list)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setChildrenCollapsible(False)
        splitter.setSizes([220, 900])

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(splitter)

    # ---- selection / refresh -------------------------------------------

    def _on_index_selected(self, ix: int) -> None:
        self._current_idx = ix
        self._session.remember_selection(self._CURSOR_KEY, ix)
        self._current_frame = 0
        # Palette to view/edit — the sprite's canonical MCHR_PAL. The spinner
        # lets you preview other palettes; the Borrow-palette section copies one
        # in (persisted as palette content, not an ARM9 reassignment).
        default_pal = self._session.mchr_pal_for_chr(ix)
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
        self._palette_toolkit.sync()
        self._reset_borrow()
        self._stop_anim_playback()
        self._refresh_anim_panel()

    def _on_frame_changed(self, value: int) -> None:
        self._current_frame = value
        self._refresh_preview_only()

    def _on_duplicate_frame(self) -> None:
        """Append a copy of the *selected* frame to the END of the sheet.

        Appending (never inserting) is deliberate: existing animations
        reference frames by index, so inserting in the middle would shift
        every later frame and desync every sequence. The new frame lands at
        index ``frame_count`` and nothing else moves."""
        if self._current_idx is None or self._undo_stack is None:
            return
        ix = self._current_idx
        entry = _decoded_entry(self._chr_pak, ix)
        if entry is None or entry.frame_count == 0:
            QMessageBox.warning(self, "Cannot duplicate", "No frame to copy.")
            return
        frames = list(entry.frames)
        src = min(self._current_frame, len(frames) - 1)
        frames.append(frames[src])
        try:
            new_raw = mchr.encode_mchr_chr_entry(frames)
        except ValueError as exc:
            QMessageBox.critical(self, "Encode failed", str(exc))
            return
        cmd = ReplaceSpriteCommand(
            self._session,
            [(MCHR_CHR, ix, sprite.compress_rle30(new_raw))],
            description=(
                f"Duplicate frame {src} → {len(frames) - 1} of MCHR 0x{ix:04x}"
            ),
            on_change=self._on_chr_entry_replaced,
        )
        self._undo_stack.push(cmd)

    def _on_palette_changed(self, value: int) -> None:
        # Preview/edit target only — pick which MCHR_PAL the sprite renders
        # under and which the editor targets. Persisting a different palette is
        # done by copying it in via the Borrow-palette section (palette content,
        # saved normally) — not an ARM9 reassignment.
        self._current_palette_idx = value
        self._refresh_preview_only()
        self._palette_toolkit.sync()

    # Dormant: the ARM9 ow-table reassignment path (superseded by Borrow-palette
    # per the user's preference for direct preview+confirm). Kept per
    # hide-never-delete; not wired to any live control.
    def _on_reset_ow_pal(self) -> None:
        """Revert this sprite's overworld palette to vanilla — the table is
        identity by design, so vanilla == the ow-id itself. No-op (no undo
        entry) when it's already vanilla."""
        if self._current_idx is None or self._undo_stack is None:
            return
        ow_id = self._current_ow_id
        if self._session.mchr_effective_ow_pal(ow_id) == ow_id:
            return
        self._undo_stack.push(SetMchrOwPalCommand(
            self._session, ow_id, ow_id,
            description=(
                f"Revert overworld palette for 0x{self._current_idx:04x} "
                "to vanilla"
            ),
            on_change=self._resync_ow_palette,
            mergeable=False,
        ))

    def _resync_ow_palette(self) -> None:
        """Reflect the current ow-id's effective (possibly undone/redone)
        palette in the spinner + preview, without re-pushing a command."""
        if self._current_idx is None:
            return
        eff = self._session.mchr_effective_ow_pal(self._current_ow_id)
        if eff == self._current_palette_idx:
            return
        self._current_palette_idx = eff
        self._palette_spin.blockSignals(True)
        self._palette_spin.setValue(eff)
        self._palette_spin.blockSignals(False)
        self._refresh_preview_only()
        self._palette_toolkit.sync()

    def _on_width_changed(self, value: int) -> None:
        self._width_tiles_override = value if value > 0 else None
        if self._current_idx is not None:
            self._width_overrides[self._current_idx] = self._width_tiles_override
        self._refresh_preview_only()

    def _on_all_frames_toggled(self, checked: bool) -> None:
        self._show_all_frames = checked
        # The Frame spinner stays live even in strip mode: it picks which
        # frame is highlighted in the strip and which one "Duplicate frame"
        # copies.
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
        self._frame_spin.blockSignals(False)

        self._refresh_preview_only()

    def _mirror_palette_preview(self, *, pixmap=None, text=None) -> None:
        """Keep the Palette-tab reference image in step with the Frames render
        so recolouring on the Palette tab shows the sprite live."""
        lbl = getattr(self, "_palette_preview_label", None)
        if lbl is None:
            return
        if pixmap is not None:
            lbl.setPixmap(pixmap)
            lbl.adjustSize()
        else:
            lbl.setText(text)

    def _refresh_preview_only(self) -> None:
        if self._current_idx is None:
            self._preview_src_qimage = None
            self._picker.set_current_color(None)
            self._mirror_palette_preview(text="Select a sprite to preview.")
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
            self._mirror_palette_preview(text="(palette decode failed)")
            return

        # While the batch adjuster drags, render the previewed colours instead
        # of the committed ones (no write until Apply).
        render_palette = (
            self._preview_palette if self._preview_palette is not None else palette
        )
        wt_override = self._effective_width_tiles()
        if self._show_all_frames:
            pixmap = self._render_frame_strip(entry, render_palette, wt_override)
        else:
            frame_i = min(self._current_frame, entry.frame_count - 1)
            pixmap = self._render_single_frame(
                entry.frames[frame_i], render_palette, wt_override
            )
        # Render at 4× nominal to make 16×32 sprites comfortably visible
        # in a 256+ minimum preview pane; QImage's nearest-neighbor scaling
        # keeps pixel edges sharp (no smoothing).
        scaled = pixmap.scaled(
            pixmap.width() * 4, pixmap.height() * 4,
            Qt.KeepAspectRatio, Qt.FastTransformation,
        )
        # In strip mode, outline the picked frame (the one "Duplicate frame"
        # copies). Frames sit side by side with a 1px gutter, so frame i
        # starts at x = i*(fw+1) in source pixels → ×4 in the scaled pixmap.
        if self._show_all_frames and entry.frame_count > 0:
            fw, _fh = self._frame_dims(entry)
            cur = min(self._current_frame, entry.frame_count - 1)
            painter = QPainter(scaled)
            painter.setPen(QPen(QColor(0x2E, 0x9A, 0xFF), 3))
            painter.drawRect(cur * (fw + 1) * 4, 0, fw * 4 - 1, scaled.height() - 1)
            painter.end()
        self._image_label.setPixmap(scaled)
        self._mirror_palette_preview(pixmap=scaled)
        # Force the QScrollArea to honor the pixmap's size so a wide
        # "show all frames" strip gets a horizontal scroll bar instead
        # of being silently clipped.
        self._image_label.adjustSize()

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

    # ---- palette editor (grid + inline editor + batch recolour) --------

    def _build_palette_sidebar(self) -> QWidget:
        """Build the right-hand palette pane and its :class:`PaletteToolkit`.
        MCHR palettes are a flat 16-colour bank (slot 0 transparent), selected
        by ``_current_palette_idx`` — no per-file banks, so slots map straight
        to palette indices."""
        self._palette_grid = PaletteGrid(cols=DEFAULT_COLS_MIN, swatch=DEFAULT_SWATCH)
        self._palette_grid.setToolTip(
            "This sprite's 16-colour palette. Click a swatch to edit it, or "
            "shift-click / drag a box to select several and shift them together "
            "with the H/S/L sliders. Slot 0 is transparent."
        )
        self._palette_editor = PaletteEditor()
        self._palette_adjuster = PaletteBatchAdjuster()
        self._eyedropper_btn = QPushButton("Eyedropper")
        self._eyedropper_btn.setCheckable(True)
        self._eyedropper_btn.setToolTip(
            "Then click a pixel on the sprite preview to select its palette slot."
        )

        col = QWidget()
        cl = QVBoxLayout(col)
        cl.setContentsMargins(4, 4, 4, 4)
        cl.setSpacing(6)
        header = QHBoxLayout()
        title = QLabel("Palette")
        title.setStyleSheet("font-weight: bold;")
        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(self._eyedropper_btn)
        cl.addLayout(header)
        self._pal_scroll = QScrollArea()
        self._pal_scroll.setWidget(self._palette_grid)
        self._pal_scroll.setWidgetResizable(False)
        self._pal_scroll.setAlignment(Qt.AlignTop | Qt.AlignHCenter)
        self._pal_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._pal_scroll.setMinimumWidth(self._palette_grid.width() + 20)
        self._pal_scroll.setFixedHeight(
            min(self._palette_grid.height() + 2, DEFAULT_SCROLL_MAX_H)
        )
        cl.addWidget(self._pal_scroll)
        cl.addWidget(self._palette_editor)
        cl.addWidget(self._palette_adjuster)
        cl.addLayout(self._build_borrow_palette_section())
        cl.addStretch(1)
        self._pal_min_w = self._palette_grid.width() + 30
        col.setMinimumWidth(self._pal_min_w)

        self._palette_toolkit = PaletteToolkit(
            self, self._palette_grid, self._palette_editor, self._palette_adjuster,
            get_palette=self._palette_get,
            write_palette=self._palette_write,
            set_preview_palette=self._palette_set_preview,
            refresh_preview=self._refresh_preview_only,
            picker=self._picker,
            eyedropper_btn=self._eyedropper_btn,
            scroll=self._pal_scroll,
        )
        return col

    def _palette_get(self) -> List[Tuple[int, int, int]]:
        pal = _decoded_palette(self._pal_pak, self._current_palette_idx)
        return [tuple(c) for c in pal] if pal else []

    def _palette_set_preview(self, pal) -> None:
        self._preview_palette = pal

    def _palette_write(self, mapping) -> None:
        """Commit N palette slots into the current MCHR_PAL entry, one undo
        step. Slots are flat 0..15 — no bank translation."""
        if self._current_idx is None:
            return
        palette = _decoded_palette(self._pal_pak, self._current_palette_idx)
        if palette is None:
            return
        new_palette = list(palette)
        changed = 0
        for slot, rgb in mapping.items():
            new = (int(rgb[0]), int(rgb[1]), int(rgb[2]))
            if 0 <= slot < len(new_palette) and \
                    self._snap5(new_palette[slot]) != self._snap5(new):
                new_palette[slot] = new
                changed += 1
        if changed == 0:
            return
        encoded = mchr.encode_palette_bgr555(new_palette)
        noun = "colour" if changed == 1 else "colours"
        cmd = ReplaceSpriteCommand(
            self._session,
            [(MCHR_PAL, self._current_palette_idx, sprite.compress_rle30(encoded))],
            description=(
                f"Edit MCHR palette 0x{self._current_palette_idx:04x} "
                f"({changed} {noun})"
            ),
            on_change=self._after_palette_write,
        )
        if self._undo_stack is not None:
            self._undo_stack.push(cmd)
        else:
            cmd.redo()

    def _after_palette_write(self) -> None:
        self._refresh_preview_only()
        self._palette_toolkit.sync()

    # ---- borrow palette (scroll another palette, preview, then copy in) ------

    def _build_borrow_palette_section(self) -> QVBoxLayout:
        box = QVBoxLayout()
        box.setSpacing(3)
        title = QLabel("Borrow palette")
        title.setStyleSheet("font-weight: bold;")
        box.addWidget(title)
        hint = QLabel("Scroll another palette onto this sprite, then apply.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #888; font-size: 10px;")
        box.addWidget(hint)
        row = QHBoxLayout()
        row.setSpacing(4)
        row.addWidget(QLabel("From #"))
        self._borrow_spin = QSpinBox()
        self._borrow_spin.setRange(0, max(0, self._pal_pak.count - 1))
        self._borrow_spin.setToolTip(
            "Preview this sprite under another MCHR_PAL entry (non-destructive). "
            "\"Use this palette\" copies it into this sprite's palette."
        )
        self._borrow_spin.valueChanged.connect(self._on_borrow_changed)
        row.addWidget(self._borrow_spin, 1)
        box.addLayout(row)
        # Off by default — overworld sprites carry foreign palettes well.
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
        self._borrow_apply_btn = QPushButton("Use this palette")
        self._borrow_apply_btn.setEnabled(False)
        self._borrow_apply_btn.clicked.connect(self._on_borrow_apply)
        box.addWidget(self._borrow_apply_btn)
        return box

    def _borrowed_palette(self, idx: int) -> Optional[List[Tuple[int, int, int]]]:
        """MCHR_PAL[idx] as it would land on this sprite — brightness-matched to
        the current palette when the toggle is on, else a raw copy."""
        src = _decoded_palette(self._pal_pak, idx)
        if src is None:
            return None
        if self._borrow_match_cb.isChecked():
            own = self._palette_get()
            if own:
                return intensity_matched_palette(own, [tuple(c) for c in src])
        return [tuple(c) for c in src]

    def _reset_borrow(self) -> None:
        if self._current_idx is None:
            return
        self._borrow_spin.blockSignals(True)
        self._borrow_spin.setValue(self._current_palette_idx)
        self._borrow_spin.blockSignals(False)
        self._borrow_apply_btn.setEnabled(False)

    def _on_borrow_changed(self, idx: int) -> None:
        if self._current_idx is None:
            return
        if idx == self._current_palette_idx:
            self._preview_palette = None
            self._borrow_apply_btn.setEnabled(False)
        else:
            pal = self._borrowed_palette(idx)
            if pal is None:
                return
            self._preview_palette = pal
            self._borrow_apply_btn.setEnabled(True)
        self._refresh_preview_only()

    def _on_borrow_apply(self) -> None:
        if self._current_idx is None:
            return
        idx = self._borrow_spin.value()
        if idx == self._current_palette_idx:
            return
        pal = self._borrowed_palette(idx)
        if pal is None:
            return
        self._palette_write({i: c for i, c in enumerate(pal)})
        self._reset_borrow()

    # ---- rendering ------------------------------------------------------

    @staticmethod
    def _render_single_frame(
        frame_bytes: bytes,
        palette: mchr.Palette,
        width_tiles_override: Optional[int],
        hflip: bool = False,
    ) -> QPixmap:
        rgba, w, h = mchr.render_frame_rgba(
            frame_bytes, palette, width_tiles=width_tiles_override,
        )
        img = QImage(rgba, w, h, w * 4, QImage.Format_RGBA8888).copy()
        pm = QPixmap.fromImage(img)
        if hflip:
            pm = pm.transformed(QTransform().scale(-1, 1))
        return pm

    def _effective_width_tiles(self) -> Optional[int]:
        """Width (in tiles) to render at: the user's explicit override, else
        the sprite's authoritative OAM width from MCHR_ANM (fixes the
        genuinely-wide sprites), else ``None`` → ``pick_tile_grid``."""
        if self._width_tiles_override is not None:
            return self._width_tiles_override
        if self._current_idx is None:
            return None
        return self._session.mchr_width_tiles_for_chr(self._current_idx)

    def _frame_dims(self, entry: mchr.MchrEntry) -> Tuple[int, int]:
        """Return ``(frame_w_px, frame_h_px)`` for the effective width."""
        wt_eff = self._effective_width_tiles()
        if wt_eff is not None:
            wt = wt_eff
            ht = (entry.tiles_per_frame + wt - 1) // wt
        else:
            wt, ht = mchr.pick_tile_grid(entry.tiles_per_frame)
        return wt * 8, ht * 8

    # ---- animation (MCHR_ANM) -------------------------------------------

    def _build_anim_tab(self) -> QWidget:
        """Assemble the Animation preview tab: a play surface on the left,
        the animation picker + editable frame list on the right.

        Each frame record carries a frame index + duration (both editable),
        an ``hflip`` facing-mirror bit, and four still-unidentified params —
        all five are per-animation (constant within an animation in vanilla),
        so they're surfaced as a per-animation control group, not per-frame."""
        self._anim_timer = QTimer(self)
        self._anim_timer.setInterval(max(1, 1000 // self._anim_fps))
        self._anim_timer.timeout.connect(self._on_anim_tick)

        self._anim_label = QLabel("Select an animated sprite.")
        self._anim_label.setAlignment(Qt.AlignCenter)
        self._anim_label.setMinimumSize(256, 256)
        self._anim_scroll = QScrollArea()
        self._anim_scroll.setWidget(self._anim_label)
        self._anim_scroll.setWidgetResizable(False)
        self._anim_scroll.setAlignment(Qt.AlignCenter)

        self._anim_combo = QComboBox()
        self._anim_combo.currentIndexChanged.connect(self._on_anim_combo_changed)
        self._anim_play_btn = QPushButton("▶ Play")
        self._anim_play_btn.setCheckable(True)
        self._anim_play_btn.toggled.connect(self._on_anim_play_toggled)
        self._anim_fps_spin = QSpinBox()
        self._anim_fps_spin.setRange(1, 120)
        self._anim_fps_spin.setValue(self._anim_fps)
        self._anim_fps_spin.setSuffix(" fps")
        self._anim_fps_spin.valueChanged.connect(self._on_anim_fps_changed)

        editable = self._undo_stack is not None
        self._anim_table = QTableWidget(0, 2)
        self._anim_table.setHorizontalHeaderLabels(["Frame", "Duration"])
        self._anim_table.verticalHeader().setVisible(False)
        self._anim_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.Stretch
        )
        self._anim_table.setEditTriggers(
            (QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
            if editable else QAbstractItemView.NoEditTriggers
        )
        self._anim_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._anim_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._anim_table.setMinimumHeight(220)
        self._anim_table.itemSelectionChanged.connect(self._on_anim_row_selected)
        self._anim_table.itemChanged.connect(self._on_anim_step_edited)

        self._anim_add_btn = QPushButton("+ Add frame")
        self._anim_add_btn.clicked.connect(self._on_anim_add_step)
        self._anim_remove_btn = QPushButton("- Remove frame")
        self._anim_remove_btn.clicked.connect(self._on_anim_remove_step)
        self._anim_remove_btn.setEnabled(False)

        note = QLabel(
            "Frame = MCHR_CHR frame index · Duration = ticks."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #888; font-size: 11px;")

        combo_row = QHBoxLayout()
        combo_row.addWidget(QLabel("Animation:"))
        combo_row.addWidget(self._anim_combo, 1)
        play_row = QHBoxLayout()
        play_row.addWidget(self._anim_play_btn)
        play_row.addWidget(self._anim_fps_spin)
        play_row.addStretch(1)
        step_row = QHBoxLayout()
        step_row.addWidget(self._anim_add_btn)
        step_row.addWidget(self._anim_remove_btn)
        step_row.addStretch(1)

        editor_panel = QWidget()
        editor_panel.setMinimumWidth(320)
        ep_layout = QVBoxLayout(editor_panel)
        ep_layout.setContentsMargins(0, 0, 0, 0)
        ep_layout.addLayout(combo_row)
        ep_layout.addLayout(play_row)
        ep_layout.addWidget(self._anim_table, 1)
        if self._undo_stack is not None:
            ep_layout.addLayout(step_row)
        ep_layout.addWidget(self._build_anim_params_group())
        ep_layout.addWidget(note)

        anim_split = QSplitter(Qt.Horizontal)
        anim_split.addWidget(self._anim_scroll)
        anim_split.addWidget(editor_panel)
        anim_split.setStretchFactor(0, 1)
        anim_split.setStretchFactor(1, 0)
        anim_split.setSizes([460, 360])

        tab = QWidget()
        tab_layout = QVBoxLayout(tab)
        tab_layout.setContentsMargins(8, 8, 8, 8)
        tab_layout.addWidget(anim_split, 1)
        return tab

    def _build_anim_params_group(self) -> QGroupBox:
        """Per-animation record fields. All five (hflip + p0..p3) are constant
        within an animation in vanilla, so they're edited once for the whole
        animation — a change writes that field onto every frame of the current
        animation (one undo step). ``hflip`` is the confirmed facing-mirror;
        p0..p3 are still unidentified, exposed as raw u16s for experimentation."""
        box = QGroupBox("This animation")
        form = QFormLayout(box)
        form.setContentsMargins(8, 6, 8, 6)
        form.setVerticalSpacing(4)

        editable = self._undo_stack is not None
        self._anim_hflip_cb = QCheckBox("Mirror horizontally (facing)")
        self._anim_hflip_cb.setToolTip(
            "Draw this animation horizontally flipped — the overworld "
            "left/right facing bit (record field 4)."
        )
        self._anim_hflip_cb.setEnabled(editable)
        self._anim_hflip_cb.toggled.connect(self._on_anim_hflip_toggled)
        form.addRow(self._anim_hflip_cb)

        self._anim_param_spins: List[QSpinBox] = []
        for k in range(4):
            sp = QSpinBox()
            sp.setRange(0, 0xFFFF)
            sp.setEnabled(editable)
            sp.setToolTip(
                "Raw record field — semantics not yet identified (likely draw "
                "offset / OAM attribute). Preserved verbatim unless changed."
            )
            sp.valueChanged.connect(
                lambda v, k=k: self._on_anim_param_changed(k, v)
            )
            form.addRow(f"Param {k}", sp)
            self._anim_param_spins.append(sp)

        self._anim_param_varies_lbl = QLabel(
            "⚠ these differ between this animation's frames — editing a field "
            "sets it on all of them."
        )
        self._anim_param_varies_lbl.setWordWrap(True)
        self._anim_param_varies_lbl.setStyleSheet("color: #c80; font-size: 10px;")
        self._anim_param_varies_lbl.setVisible(False)
        form.addRow(self._anim_param_varies_lbl)
        return box

    def _refresh_anim_params(self) -> None:
        """Load the current animation's field values into the param controls,
        flagging any field that isn't uniform across the animation's frames."""
        anim = self._current_animation()
        has = anim is not None and bool(anim.frames)
        editable = self._undo_stack is not None and has
        widgets = [self._anim_hflip_cb, *self._anim_param_spins]
        self._anim_params_loading = True
        for w in widgets:
            w.blockSignals(True)
        varies = False
        if has:
            self._anim_hflip_cb.setChecked(bool(anim.frames[0].hflip))
            if len({1 if f.hflip else 0 for f in anim.frames}) > 1:
                varies = True
            for k, sp in enumerate(self._anim_param_spins):
                sp.setValue(anim.frames[0].params[k])
                if len({f.params[k] for f in anim.frames}) > 1:
                    varies = True
        else:
            self._anim_hflip_cb.setChecked(False)
            for sp in self._anim_param_spins:
                sp.setValue(0)
        for w in widgets:
            w.setEnabled(editable)
            w.blockSignals(False)
        self._anim_param_varies_lbl.setVisible(varies)
        self._anim_params_loading = False

    def _on_anim_hflip_toggled(self, checked: bool) -> None:
        self._apply_field_to_anim(4, 1 if checked else 0, "mirror")

    def _on_anim_param_changed(self, k: int, value: int) -> None:
        self._apply_field_to_anim(k, value, f"param {k}")

    def _apply_field_to_anim(self, k: int, value: int, label: str) -> None:
        """Write record field ``k`` = ``value`` onto every frame of the current
        animation, then commit as one undoable change."""
        if self._anim_params_loading or self._undo_stack is None:
            return
        if self._current_idx is None:
            return
        anim = self._current_animation()
        if anim is None or not anim.frames:
            return
        value &= 0xFFFF
        if all(fr.params[k] == value for fr in anim.frames):
            return
        for fr in anim.frames:
            p = list(fr.params)
            p[k] = value
            fr.params = (p[0], p[1], p[2], p[3], p[4])
        self._push_anim_change(
            f"Set MCHR 0x{self._current_idx:04x} anim {self._anim_idx} {label}"
        )
        self._update_anim_combo_label(self._anim_idx)

    def _update_anim_combo_label(self, ai: int) -> None:
        """Refresh one animation's picker label (the ◀ mirrored tag tracks the
        hflip field)."""
        if self._anim is None or not (0 <= ai < len(self._anim.animations)):
            return
        idx = self._anim_combo.findData(ai)
        if idx < 0:
            return
        self._anim_combo.blockSignals(True)
        self._anim_combo.setItemText(
            idx, self._anim_label_for(ai, self._anim.animations[ai])
        )
        self._anim_combo.blockSignals(False)

    def _refresh_anim_panel(self) -> None:
        """Parse the current sprite's MCHR_ANM entry, repopulate the picker
        + frame list, and enable/disable the Animation tab.

        The tab is enabled when the sprite can actually show motion: it
        already animates, or it has ≥2 CHR frames to cycle between — which
        lets a modder build a new animation on a currently-static sprite by
        adding frames to any of its (single-frame) animation slots."""
        self._anim = None
        if self._current_idx is not None and self._current_idx < self._anm_pak.count:
            try:
                self._anim = mchr_anm.parse_mchr_anm(
                    self._anm_pak.entries[self._current_idx]
                )
            except (ValueError, IndexError):
                self._anim = None
        animations = self._anim.animations if self._anim else []

        self._anim_combo.blockSignals(True)
        self._anim_combo.clear()
        for ai, anim in enumerate(animations):
            self._anim_combo.addItem(self._anim_label_for(ai, anim), ai)
        self._anim_combo.blockSignals(False)

        entry = (
            _decoded_entry(self._chr_pak, self._current_idx)
            if self._current_idx is not None else None
        )
        frame_count = entry.frame_count if entry is not None else 0
        can_animate = bool(
            animations and (self._anim.has_animation or frame_count >= 2)
        )
        was_on_anim = self._preview_tabs.currentIndex() == self._anim_tab_index
        if not can_animate and was_on_anim:
            self._preview_tabs.setCurrentIndex(0)  # fall back to Frames
        self._preview_tabs.setTabEnabled(self._anim_tab_index, can_animate)

        self._anim_idx = 0
        self._anim_pos = 0
        self._anim_editing_frame = -1
        if can_animate:
            self._anim_combo.setCurrentIndex(0)
        self._refresh_anim_table()
        if not can_animate:
            self._anim_label.setText(
                "This sprite has a single frame — import more frames in the "
                "Frames tab to build an animation."
                if frame_count < 2 else "This sprite has no animation."
            )
        elif self._preview_tabs.currentIndex() == self._anim_tab_index:
            self._show_current_anim_frame_static()

    @staticmethod
    def _anim_label_for(ai: int, anim: "mchr_anm.MchrAnimation") -> str:
        n = len(anim.frames)
        mirror = " ◀ mirrored" if anim.frames and anim.frames[0].hflip else ""
        return f"Anim {ai} — {n} frame{'s' if n != 1 else ''}{mirror}"

    def _current_animation(self) -> Optional["mchr_anm.MchrAnimation"]:
        if self._anim is None:
            return None
        if not (0 <= self._anim_idx < len(self._anim.animations)):
            return None
        return self._anim.animations[self._anim_idx]

    def _refresh_anim_table(self) -> None:
        anim = self._current_animation()
        frames = anim.frames if anim else []
        self._anim_table_loading = True
        self._anim_table.blockSignals(True)
        self._anim_table.setRowCount(len(frames))
        for r, fr in enumerate(frames):
            self._anim_table.setItem(r, 0, QTableWidgetItem(str(fr.frame)))
            self._anim_table.setItem(r, 1, QTableWidgetItem(str(fr.duration)))
        self._anim_table.blockSignals(False)
        self._anim_table_loading = False
        self._refresh_anim_params()
        self._recompute_anim_flat()
        self._update_anim_step_buttons()

    def _recompute_anim_flat(self) -> None:
        anim = self._current_animation()
        self._anim_flat = mchr_anm.flatten_animation(anim) if anim else []
        if self._anim_pos >= len(self._anim_flat):
            self._anim_pos = 0

    def _on_anim_combo_changed(self, _idx: int) -> None:
        data = self._anim_combo.currentData()
        self._anim_idx = int(data) if data is not None else 0
        self._anim_pos = 0
        self._anim_editing_frame = -1
        self._refresh_anim_table()
        if self._anim_timer.isActive() and self._anim_flat:
            self._show_anim_frame(self._anim_flat[0])
        elif self._preview_tabs.currentIndex() == self._anim_tab_index:
            self._show_current_anim_frame_static()

    def _on_anim_fps_changed(self, fps: int) -> None:
        self._anim_fps = fps
        self._anim_timer.setInterval(max(1, 1000 // fps))

    def _on_preview_tab_changed(self, idx: int) -> None:
        if idx == self._anim_tab_index:
            self._show_current_anim_frame_static()
        elif self._anim_timer.isActive():
            self._stop_anim_playback()
        if idx == getattr(self, "_palette_tab_index", -1):
            # Render the current sprite into the tab's reference preview + reflow.
            self._refresh_preview_only()
            self._palette_toolkit.reflow()

    def _on_anim_play_toggled(self, checked: bool) -> None:
        if not checked:
            self._stop_anim_playback()
            return
        if not self._anim_flat:
            self._recompute_anim_flat()
        if not self._anim_flat:
            self._anim_play_btn.setChecked(False)
            return
        self._anim_play_btn.setText("■ Stop")
        self._anim_pos = 0
        self._show_anim_frame(self._anim_flat[0])
        self._anim_timer.start()

    def _stop_anim_playback(self) -> None:
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
        if self._anim_timer.isActive():
            return
        anim = self._current_animation()
        if anim is None or not anim.frames:
            return
        idx = self._anim_editing_frame if self._anim_editing_frame >= 0 else 0
        idx = max(0, min(idx, len(anim.frames) - 1))
        self._show_anim_frame(anim.frames[idx])

    def _show_anim_frame(self, fr: "mchr_anm.MchrAnimFrame") -> None:
        """Render the MCHR_CHR frame the record names into the Animation
        preview, using the current palette + width override."""
        if self._current_idx is None:
            return
        entry = _decoded_entry(self._chr_pak, self._current_idx)
        palette = _decoded_palette(self._pal_pak, self._current_palette_idx)
        if entry is None or palette is None:
            return
        if not (0 <= fr.frame < entry.frame_count):
            self._anim_label.setText(
                f"(frame {fr.frame} out of range — sprite has "
                f"{entry.frame_count})"
            )
            return
        pm = self._render_single_frame(
            entry.frames[fr.frame], palette, self._effective_width_tiles(),
            hflip=fr.hflip,
        )
        scaled = pm.scaled(
            pm.width() * 4, pm.height() * 4,
            Qt.KeepAspectRatio, Qt.FastTransformation,
        )
        self._anim_label.setPixmap(scaled)
        self._anim_label.adjustSize()

    def _on_anim_row_selected(self) -> None:
        rows = self._anim_table.selectionModel().selectedRows()
        self._anim_editing_frame = rows[0].row() if rows else -1
        self._update_anim_step_buttons()
        if not self._anim_timer.isActive():
            anim = self._current_animation()
            if anim and 0 <= self._anim_editing_frame < len(anim.frames):
                self._show_anim_frame(anim.frames[self._anim_editing_frame])

    def _update_anim_step_buttons(self) -> None:
        anim = self._current_animation()
        editable = self._undo_stack is not None and anim is not None
        self._anim_add_btn.setEnabled(editable)
        self._anim_remove_btn.setEnabled(
            editable
            and len(anim.frames) > 1
            and 0 <= self._anim_editing_frame < len(anim.frames)
        )

    def _on_anim_step_edited(self, item: QTableWidgetItem) -> None:
        """Apply an in-cell Frame or Duration edit; invalid input reverts."""
        if self._anim_table_loading or self._undo_stack is None:
            return
        if self._current_idx is None:
            return
        anim = self._current_animation()
        if anim is None:
            return
        row, col = item.row(), item.column()
        if not (0 <= row < len(anim.frames)) or col not in (0, 1):
            return
        fr = anim.frames[row]
        try:
            value = int(item.text())
        except ValueError:
            self._refresh_anim_table()
            return
        if value < 0:
            self._refresh_anim_table()
            return
        if col == 0:
            entry = _decoded_entry(self._chr_pak, self._current_idx)
            n_frames = entry.frame_count if entry is not None else None
            if n_frames and value >= n_frames:
                value = n_frames - 1
            if value == fr.frame:
                return
            fr.frame = value
        else:
            if value > 0xFFFF:
                value = 0xFFFF
            if value == fr.duration:
                return
            fr.duration = value
        self._push_anim_change(
            f"Edit MCHR 0x{self._current_idx:04x} anim {self._anim_idx} "
            f"frame {row} {'index' if col == 0 else 'duration'}"
        )

    def _on_anim_add_step(self) -> None:
        if self._undo_stack is None or self._current_idx is None:
            return
        anim = self._current_animation()
        if anim is None:
            return
        row = self._anim_editing_frame
        insert_at = row + 1 if 0 <= row < len(anim.frames) else len(anim.frames)
        src = (
            anim.frames[row] if 0 <= row < len(anim.frames)
            else (anim.frames[-1] if anim.frames else None)
        )
        new = mchr_anm.MchrAnimFrame(
            frame=src.frame if src else 0,
            duration=src.duration if src else 4,
            params=src.params if src else (0, 0, 0, 0, 0),
        )
        anim.frames.insert(insert_at, new)
        self._anim_editing_frame = insert_at
        self._push_anim_change(
            f"Add MCHR 0x{self._current_idx:04x} anim {self._anim_idx} frame"
        )

    def _on_anim_remove_step(self) -> None:
        if self._undo_stack is None or self._current_idx is None:
            return
        anim = self._current_animation()
        if anim is None:
            return
        row = self._anim_editing_frame
        if not (0 <= row < len(anim.frames)) or len(anim.frames) <= 1:
            return
        del anim.frames[row]
        self._anim_editing_frame = min(row, len(anim.frames) - 1)
        self._push_anim_change(
            f"Remove MCHR 0x{self._current_idx:04x} anim {self._anim_idx} "
            f"frame {row}"
        )

    def _push_anim_change(self, description: str) -> None:
        if self._current_idx is None or self._undo_stack is None or self._anim is None:
            return
        try:
            new_raw = mchr_anm.serialize_mchr_anm(self._anim)
        except ValueError as exc:
            QMessageBox.critical(self, "Build failed", f"MCHR_ANM rebuild: {exc}")
            return
        cmd = ReplaceSpriteCommand(
            self._session,
            [(MCHR_ANM, self._current_idx, sprite.compress_rle30(new_raw))],
            description=description,
            on_change=self._reload_anim_after_edit,
        )
        self._undo_stack.push(cmd)

    def _reload_anim_after_edit(self) -> None:
        """on_change after a frame edit (and its undo/redo). Re-parse and
        refresh the table for the current animation, preserving which
        animation is shown and the selected frame."""
        if self._current_idx is None:
            return
        try:
            self._anim = mchr_anm.parse_mchr_anm(
                self._anm_pak.entries[self._current_idx]
            )
        except (ValueError, IndexError):
            self._anim = None
        if self._anim is None or not (
            0 <= self._anim_idx < len(self._anim.animations)
        ):
            self._refresh_anim_panel()
            return
        anim = self._anim.animations[self._anim_idx]
        self._update_anim_combo_label(self._anim_idx)
        if self._anim_table.rowCount() == len(anim.frames):
            self._anim_table_loading = True
            self._anim_table.blockSignals(True)
            for r, fr in enumerate(anim.frames):
                for col, text in ((0, str(fr.frame)), (1, str(fr.duration))):
                    it = self._anim_table.item(r, col)
                    if it is not None:
                        it.setText(text)
            self._anim_table.blockSignals(False)
            self._anim_table_loading = False
            self._refresh_anim_params()
            self._recompute_anim_flat()
            self._update_anim_step_buttons()
            self._show_current_anim_frame_static()
        else:
            keep = self._anim_editing_frame
            self._refresh_anim_table()
            if anim.frames:
                row = keep if 0 <= keep < len(anim.frames) else len(anim.frames) - 1
                self._anim_table.selectRow(row)
            self._show_current_anim_frame_static()

    def aboutToTeardown(self) -> None:
        """Stop the playback timer before the widget is deleted."""
        if self._anim_timer.isActive():
            self._anim_timer.stop()

    # ---- PNG export / import -------------------------------------------

    def _on_export_png(self) -> None:
        if self._current_idx is None:
            return
        entry = _decoded_entry(self._chr_pak, self._current_idx)
        palette = _decoded_palette(self._pal_pak, self._current_palette_idx)
        if entry is None or palette is None:
            QMessageBox.critical(self, "Export failed", "Could not decode sprite/palette.")
            return
        suggested = f"mchr_chr_0x{self._current_idx:04x}.png"
        path, _ = QFileDialog.getSaveFileName(
            self, "Export frames sheet PNG", suggested, "PNG (*.png)"
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
                f"Import MCHR sprite 0x{self._current_idx:04x} + palette "
                f"0x{self._current_palette_idx:04x}"
            )
        else:
            desc = f"Import MCHR sprite 0x{self._current_idx:04x}"

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

    # ---- per-frame PNG IO ----------------------------------------------

    def _on_export_per_frame_pngs(self) -> None:
        """Write each frame to its own PNG ``<base>_frame_<K>.png``.

        Each file is sized to the current frame dimensions (driven by the
        Width-tiles override). Embeds ``mchr_mode=per_frame`` and
        ``mchr_frame=K`` so import can validate the set.
        """
        if self._current_idx is None:
            return
        entry = _decoded_entry(self._chr_pak, self._current_idx)
        palette = _decoded_palette(self._pal_pak, self._current_palette_idx)
        if entry is None or palette is None:
            QMessageBox.critical(
                self, "Export failed", "Could not decode sprite/palette.",
            )
            return
        suggested = f"mchr_chr_0x{self._current_idx:04x}.png"
        path, _ = QFileDialog.getSaveFileName(
            self, "Export per-frame PNGs (pick base name)",
            suggested, "PNG (*.png)",
        )
        if not path:
            return
        base = path[:-4] if path.lower().endswith(".png") else path
        m = re.match(r"^(.*)_frame_\d+$", base)
        if m:
            base = m.group(1)
        # Build the full strip once and slice columns out — keeps the
        # codec path identical to the composite exporter (decode_frame_to_
        # indices + colour table), so per-frame and composite outputs
        # agree pixel-for-pixel.
        strip = self._build_indexed_strip(entry, palette)
        fw, fh = self._frame_dims(entry)
        for fi in range(entry.frame_count):
            sub = strip.copy(fi * fw, 0, fw, fh)
            sub.setText("mchr_mode", "per_frame")
            sub.setText("mchr_frame", str(fi))
            sub.setText("mchr_n_frames", str(entry.frame_count))
            out_path = f"{base}_frame_{fi}.png"
            if not sub.save(out_path, "PNG"):
                QMessageBox.critical(
                    self, "Export failed",
                    f"Could not write {out_path}.",
                )
                return

    def _on_import_per_frame_pngs(self) -> None:
        """Read ``<base>_frame_<K>.png`` siblings and rebuild the MCHR entry.

        Frame count is auto-detected from the on-disk set: starting at
        ``_frame_0.png``, count consecutive indices until the next file
        is missing. Lets users add or drop frames externally without a
        separate count control. All files must share dimensions and PNG
        format (Indexed8 vs RGB) — a mismatch can't round-trip into one
        4bpp tile stream + one MCHR_PAL.
        """
        if self._current_idx is None:
            return
        entry = _decoded_entry(self._chr_pak, self._current_idx)
        palette = _decoded_palette(self._pal_pak, self._current_palette_idx)
        if entry is None or palette is None:
            QMessageBox.critical(
                self, "Import failed",
                "Current sprite/palette won't decode.",
            )
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Import per-frame PNGs (pick any frame)",
            "", "PNG (*.png)",
        )
        if not path:
            return
        m = re.match(r"^(.*)_frame_(\d+)\.png$", path, re.IGNORECASE)
        if not m:
            QMessageBox.critical(
                self, "Bad filename",
                "Per-frame PNGs must follow the pattern "
                "'<name>_frame_<N>.png'.\n"
                f"Got: {os.path.basename(path)}",
            )
            return
        base = m.group(1)
        # Walk consecutive indices from 0 until a gap. Bounded by a sane
        # max (256) so a stray glob accident can't run away.
        pngs: List[QImage] = []
        fi = 0
        while fi < 256:
            sibling = f"{base}_frame_{fi}.png"
            if not os.path.exists(sibling):
                break
            cell_img = QImage(sibling)
            if cell_img.isNull():
                QMessageBox.critical(
                    self, "Import failed",
                    f"Could not read {sibling}.",
                )
                return
            pngs.append(cell_img)
            fi += 1
        if not pngs:
            QMessageBox.critical(
                self, "Missing frame PNG",
                f"Expected file not found:\n{base}_frame_0.png",
            )
            return

        fw, fh = self._frame_dims(entry)
        for idx, img in enumerate(pngs):
            if img.width() != fw or img.height() != fh:
                QMessageBox.critical(
                    self, "Bad image size",
                    f"{os.path.basename(f'{base}_frame_{idx}.png')} is "
                    f"{img.width()}×{img.height()}; expected {fw}×{fh}. "
                    "Adjust the Width (tiles) spinner if the sprite "
                    "shape is wrong.",
                )
                return

        use_indexed = pngs[0].format() == QImage.Format_Indexed8
        for idx, img in enumerate(pngs):
            if (img.format() == QImage.Format_Indexed8) != use_indexed:
                QMessageBox.critical(
                    self, "Mixed PNG formats",
                    f"Frame {idx} PNG format differs from frame 0. "
                    "Convert all frames to the same format before importing.",
                )
                return
        if not use_indexed:
            pngs = [p.convertToFormat(QImage.Format_RGBA8888) for p in pngs]

        # Palette source — same shape as the composite importer, but RGB
        # quantisation runs across all frames concatenated so a colour
        # that only appears in one frame still makes it into the palette.
        checkbox_on = self._import_pal_with_sheet_cb.isChecked()
        pal_from_plte = (
            use_indexed and checkbox_on and len(pngs[0].colorTable()) >= 2
        )
        pal_from_quant = (not use_indexed) and checkbox_on
        rebuild_palette = pal_from_plte or pal_from_quant
        if rebuild_palette:
            if pal_from_plte:
                built = build_palette_from_png(pngs[0], total_slots=PALETTE_SLOTS)
            else:
                strip_w = fw * len(pngs)
                strip = QImage(strip_w, fh, QImage.Format_RGBA8888)
                strip.fill(0)
                from PySide6.QtGui import QPainter
                painter = QPainter(strip)
                for ci, p in enumerate(pngs):
                    painter.drawImage(ci * fw, 0, p)
                painter.end()
                built = build_palette_from_png(strip, total_slots=PALETTE_SLOTS)
            if built is None:
                QMessageBox.critical(
                    self, "PNG is fully transparent",
                    "Cannot rebuild a palette from PNGs with no opaque "
                    "pixels.",
                )
                return
            working_palette: mchr.Palette = list(built)
        else:
            working_palette = list(palette)

        new_frames: List[bytes] = []
        for img in pngs:
            if use_indexed:
                indices = [
                    img.pixelIndex(x, y)
                    for y in range(fh) for x in range(fw)
                ]
            elif pal_from_quant:
                indices = []
                for y in range(fh):
                    for x in range(fw):
                        c = img.pixelColor(x, y)
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
                        c = img.pixelColor(x, y)
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
                f"Import MCHR per-frame 0x{self._current_idx:04x} + palette "
                f"0x{self._current_palette_idx:04x}"
            )
        else:
            desc = f"Import MCHR per-frame 0x{self._current_idx:04x}"

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
        suggested = f"mchr_pal_0x{self._current_palette_idx:04x}.png"
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
            description=f"Import MCHR palette 0x{self._current_palette_idx:04x}",
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
            f"0x{self._current_palette_idx:04x}"
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
        # Frame count may have changed (wider sheet = more frames), which can
        # flip a static sprite into an animatable one — re-evaluate the tab.
        self._stop_anim_playback()
        self._refresh_anim_panel()

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
        wt_eff = self._effective_width_tiles()
        for fi, fb in enumerate(entry.frames):
            indices, w, h = mchr.decode_frame_to_indices(
                fb, wt_eff
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
