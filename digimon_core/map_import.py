"""PNG round-trip for field-map layers.

Wraps the btmap_import pipeline (multi-bank palette quantize → 8x8
dedup with flips → cluster to <= max_tiles → 4bpp pack) and re-headers
the result as the field map's .c / .p / .s files. Re-tiling is lossy:
palette quantization shifts colors and near-identical tiles may merge,
so PNG -> re-tile -> PNG is not bit-exact.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from . import btmap_import as btimport
from . import map as mapmod


@dataclass
class FieldImportStats:
    cells_total: int
    unique_tiles_raw: int
    unique_after_flip_dedup: int
    unique_after_merge: int
    max_tiles: int
    banks_used: int
    was_reduced: bool


@dataclass
class FieldImportResult:
    new_c_bytes: bytes
    new_p_bytes: bytes
    new_s_bytes: bytes
    stats: FieldImportStats


def export_rgba_to_png_bytes(
    rgba: bytes, width_px: int, height_px: int,
) -> bytes:
    """Encode a flat RGBA buffer to PNG bytes via Qt.

    Kept here (vs ``map.py``) so the codec layer stays Qt-free.
    """
    from PySide6.QtCore import QBuffer, QIODevice
    from PySide6.QtGui import QImage

    img = QImage(
        bytes(rgba), width_px, height_px, width_px * 4, QImage.Format_RGBA8888,
    )
    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    img.save(buf, "PNG")
    return bytes(buf.data())


def import_layer_from_png(
    png_path_or_bytes,
    *,
    target_width_px: int,
    target_height_px: int,
    n_palette_banks: int,
    palette_trailer: bytes = b"",
    max_tiles: int = 1024,
    is_transparent_layer: bool = False,
) -> FieldImportResult:
    """Quantize ``png`` into ``.c / .p / .s`` bytes for one field-map layer.

    ``n_palette_banks`` should match the original layer's bank count
    (Dusk vanilla: 7 or 8) so the rebuilt ``.p`` file is the same size
    as the one it replaces. ``palette_trailer`` is passed through to
    :func:`map.build_palette` for the (rare) maps that ship trailing
    zero bytes after their bank table.

    ``is_transparent_layer=True`` reserves slot 0 of each bank as the
    transparency sentinel — set this for Layer B so alpha<thresh pixels
    in the PNG end up rendered as transparent in-game.
    """
    rgba, w, h = btimport.load_png_rgba(png_path_or_bytes)
    if (w, h) != (target_width_px, target_height_px):
        raise ValueError(
            f"PNG is {w}x{h}, expected {target_width_px}x{target_height_px}"
        )

    n_cells = (w // 8) * (h // 8)
    available_banks = tuple(range(max(1, min(16, n_palette_banks))))
    banks_by_index, cell_bank_per_cell, indices = btimport.quantize_image_multi_bank(
        rgba, w, h,
        available_banks=available_banks,
        colors_per_bank=16,
        transparent_index_0=is_transparent_layer,
    )

    raw_tiles = btimport.chop_into_tiles(indices, w, h)
    unique_raw = len({bytes(t) for t in raw_tiles})
    unique, assignments = btimport.dedupe_tiles_with_flips(raw_tiles)
    unique_after_flip = len(unique)

    tile_rgb_expanded = None
    if len(unique) > max_tiles:
        # Dominant-bank RGB expansion for the k-medoids distance metric.
        import numpy as np

        tile_bank_counts: Dict[Tuple[int, int], int] = {}
        for cell_ix, (tile_ix, _) in enumerate(assignments):
            key = (tile_ix, cell_bank_per_cell[cell_ix])
            tile_bank_counts[key] = tile_bank_counts.get(key, 0) + 1
        primary_bank: Dict[int, int] = {}
        primary_count: Dict[int, int] = {}
        for (tile_ix, bank), count in tile_bank_counts.items():
            if count > primary_count.get(tile_ix, -1):
                primary_count[tile_ix] = count
                primary_bank[tile_ix] = bank
        pal_arr_by_bank = {
            b: np.array(banks_by_index[b], dtype=np.float32)
            for b in banks_by_index
        }
        tile_idx_arr = (
            np.frombuffer(b"".join(unique), dtype=np.uint8)
            .reshape(len(unique), 64)
        )
        tile_rgb_expanded = np.zeros((len(unique), 192), dtype=np.float32)
        fallback_bank = available_banks[0]
        for ix in range(len(unique)):
            bank = primary_bank.get(ix, fallback_bank)
            tile_rgb_expanded[ix] = (
                pal_arr_by_bank[bank][tile_idx_arr[ix]].reshape(192)
            )

    cluster_palette = (
        next(iter(banks_by_index.values())) if banks_by_index
        else [(0, 0, 0)] * 16
    )
    reduced_unique, reduced_assignments = btimport.cluster_tiles_to_max(
        unique, assignments, cluster_palette,
        max_tiles=max_tiles,
        tile_rgb_expanded=tile_rgb_expanded,
    )

    packed = btimport.pack_tiles_4bpp(reduced_unique)
    tile_chunks = [packed[i:i + 32] for i in range(0, len(packed), 32)]
    new_c_bytes = mapmod.build_tiles(tile_chunks)

    banks_list: List[List[Tuple[int, int, int]]] = []
    for bix in range(len(available_banks)):
        banks_list.append(
            list(banks_by_index[bix]) if bix in banks_by_index
            else [(0, 0, 0)] * 16
        )
    new_p_bytes = mapmod.build_palette(banks_list, trailer=palette_trailer)

    entries: List[int] = []
    for cell_ix, (tile_ix, flip) in enumerate(reduced_assignments):
        cell_bank = cell_bank_per_cell[cell_ix]
        hflip = 1 if (flip & btimport._FLIP_H) else 0
        vflip = 1 if (flip & btimport._FLIP_V) else 0
        entry = (
            (tile_ix & 0x3FF)
            | (hflip << 10)
            | (vflip << 11)
            | ((cell_bank & 0xF) << 12)
        )
        entries.append(entry)
    new_s_bytes = mapmod.build_screen(target_width_px, target_height_px, entries)

    banks_used = len({cell_bank_per_cell[ix] for ix in range(n_cells)})
    stats = FieldImportStats(
        cells_total=len(assignments),
        unique_tiles_raw=unique_raw,
        unique_after_flip_dedup=unique_after_flip,
        unique_after_merge=len(reduced_unique),
        max_tiles=max_tiles,
        banks_used=banks_used,
        was_reduced=(len(reduced_unique) < unique_after_flip),
    )
    return FieldImportResult(
        new_c_bytes=new_c_bytes,
        new_p_bytes=new_p_bytes,
        new_s_bytes=new_s_bytes,
        stats=stats,
    )
