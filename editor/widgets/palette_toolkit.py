"""Reusable palette-editing controller.

Wires the four palette widgets — :class:`PaletteGrid` (multi-select swatches),
:class:`PaletteEditor` (inline single-slot RGB/hex), :class:`PaletteBatchAdjuster`
(H/S/L delta over a selection), and the slot eyedropper on a shared
:class:`TransparentColorPicker` — into one working recolour surface, and
delegates the format-specific parts to the host through four hooks:

* ``get_palette() -> list[(r,g,b)]`` — the current (bank's) committed colours.
* ``write_palette(mapping: dict[int, (r,g,b)])`` — commit N slot changes as one
  undo step (the host does any bank split + NCLR rebuild).
* ``set_preview_palette(pal | None)`` — stash a display-only palette override
  the host's render path consults (``None`` clears it).
* ``refresh_preview()`` — re-render the sprite preview (called throttled while
  a slider drags).

The single-slot editor and the batch adjuster both commit through the same
``write_palette`` hook (one slot vs many). BTCHR wires the same behaviour
inline today; SPR / MCHR use this controller so the logic lives once.
"""
from __future__ import annotations

from typing import Callable, Dict, List, Optional, Tuple

from PySide6.QtCore import QObject, QTimer

Rgb = Tuple[int, int, int]

# Defaults mirror the BTCHR palette sidebar so all three surfaces match.
DEFAULT_SCROLL_MAX_H = 360
DEFAULT_COLS_MIN = 8
DEFAULT_SWATCH = 20


class PaletteToolkit(QObject):
    def __init__(
        self,
        owner: QObject,
        grid,
        editor,
        adjuster,
        *,
        get_palette: Callable[[], List[Rgb]],
        write_palette: Callable[[Dict[int, Rgb]], None],
        set_preview_palette: Callable[[Optional[List[Rgb]]], None],
        refresh_preview: Callable[[], None],
        picker=None,
        eyedropper_btn=None,
        scroll=None,
        exclude_transparent_slot: bool = True,
        cols_min: int = DEFAULT_COLS_MIN,
        swatch: int = DEFAULT_SWATCH,
        scroll_max_h: int = DEFAULT_SCROLL_MAX_H,
        preview_interval_ms: int = 33,
    ) -> None:
        super().__init__(owner)
        self._grid = grid
        self._editor = editor
        self._adjuster = adjuster
        self._get_palette = get_palette
        self._write_palette = write_palette
        self._set_preview_palette = set_preview_palette
        self._refresh_preview = refresh_preview
        self._picker = picker
        self._eyedropper_btn = eyedropper_btn
        self._scroll = scroll
        self._exclude0 = exclude_transparent_slot
        self._cols_min = max(1, cols_min)
        self._swatch = max(1, swatch)
        self._scroll_max_h = scroll_max_h

        grid.set_select_mode(True)
        grid.set_multi_select(True)
        grid.colorEdited.connect(self._on_single_edit)
        editor.colorEdited.connect(self._on_single_edit)
        grid.selectedChanged.connect(self._on_anchor_changed)
        grid.selectionChanged.connect(self._on_selection_changed)
        adjuster.previewChanged.connect(self._on_preview)
        adjuster.committed.connect(self._write_palette)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(preview_interval_ms)
        self._timer.timeout.connect(refresh_preview)

        if picker is not None and eyedropper_btn is not None:
            eyedropper_btn.toggled.connect(picker.set_slot_pick_mode)
            picker.slotPickModeChanged.connect(eyedropper_btn.setChecked)

    # ---- host-facing --------------------------------------------------

    def sync(self) -> None:
        """Feed the grid + editor + adjuster the current committed palette.
        Call after every (re)decode so edits / undo reflect live."""
        self._set_preview_palette(None)
        pal = list(self._get_palette())
        self._grid.set_palette(pal)
        self._recap_scroll()
        sel = self._grid.selected()
        self._editor.set_slot(
            sel, self._grid.color_at(sel) if sel >= 0 else (0, 0, 0)
        )
        self._feed_adjuster()

    def reflow(self) -> None:
        """Fit as many swatches per row as the pane width allows (≥ minimum)."""
        if self._scroll is None:
            return
        avail = self._scroll.viewport().width()
        self._grid.set_cols(max(self._cols_min, avail // self._swatch))
        self._recap_scroll()

    def pick_slot_from_rgb(self, rgb: Rgb) -> None:
        """Eyedropper callback: select the palette slot matching a clicked
        sprite pixel (exact match expected; nearest as a fallback)."""
        pal = self._get_palette()
        if not pal:
            return
        target = (int(rgb[0]), int(rgb[1]), int(rgb[2]))
        match = next((i for i, c in enumerate(pal) if tuple(c) == target), None)
        if match is None:
            match = min(
                range(len(pal)),
                key=lambda i: sum((a - b) ** 2 for a, b in zip(pal[i], target)),
            )
        self._grid.select_slot(match)
        if self._scroll is not None:
            x, y = self._grid.slot_top_left(match)
            self._scroll.ensureVisible(x, y, 0, 40)

    # ---- internals ----------------------------------------------------

    def _recap_scroll(self) -> None:
        if self._scroll is not None:
            self._scroll.setFixedHeight(
                min(self._grid.height() + 2, self._scroll_max_h)
            )

    def _feed_adjuster(self) -> None:
        self._grid.set_preview(None)  # drop any stale transform tint
        slots = [
            s for s in self._grid.selected_slots()
            if not (self._exclude0 and s == 0)
        ]
        colors = [self._grid.color_at(s) for s in slots]
        self._adjuster.set_selection(slots, colors)

    def _on_single_edit(self, slot: int, rgb: Rgb) -> None:
        self._write_palette({int(slot): (int(rgb[0]), int(rgb[1]), int(rgb[2]))})

    def _on_anchor_changed(self, slot: int) -> None:
        self._editor.set_slot(
            slot, self._grid.color_at(slot) if slot >= 0 else (0, 0, 0)
        )

    def _on_selection_changed(self) -> None:
        self._feed_adjuster()

    def _on_preview(self, overrides: Dict[int, Rgb]) -> None:
        self._grid.set_preview(overrides)
        if overrides:
            pal = list(self._get_palette())
            for slot, c in overrides.items():
                if 0 <= slot < len(pal):
                    pal[slot] = (int(c[0]), int(c[1]), int(c[2]))
            self._set_preview_palette(pal)
        else:
            self._set_preview_palette(None)
        if not self._timer.isActive():
            self._timer.start()
