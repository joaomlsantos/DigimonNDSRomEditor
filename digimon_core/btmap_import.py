"""Flat-PNG → NCGR + NSCR + NCLR importer for battle background layers.

The user paints a 512×256 (Layer A) or 512×512 (Layer B) PNG; this module
chops it into 8×8 tiles, extracts a 16-color palette, dedupes tiles under
4-way flip, and — if the tile count exceeds the 1024 NSCR-addressable cap
— runs agglomerative greedy merging to fit. Output is a triple
(NCGR, NSCR, NCLR) the caller splices back over the originals.

Why merging matters: NSCR tile-index field is 10 bits (max 1024 unique
tiles per layer). Vanilla 512×256 backgrounds already use 999/1024 on
average — a hand-drawn import that exceeds the cap must be reduced
intelligently, not rejected. Flip-dedup is automatic (free 2-4×
multiplier); past that, pairwise RGB-distance merging absorbs the
remainder with controlled visual drift.
"""
from __future__ import annotations

import io
import struct
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from digimon_core import btmap as btmap_module
from digimon_core.sprite import (
    build_nclr_from_template,
    build_ncgr_from_template,
    parse_nclr,
    quantize_palette,
)


# 4-way flip variants encoded as NSCR flip bits (bit 10 = hflip, bit 11 = vflip)
_FLIP_NONE = 0
_FLIP_H = 1
_FLIP_V = 2
_FLIP_HV = 3


@dataclass(frozen=True)
class LayerImportStats:
    """Telemetry for the import pipeline; surfaced to the user in a preview
    dialog so they can decide whether to accept the reduced result."""
    cells_total: int
    unique_tiles_raw: int
    unique_after_flip_dedup: int
    unique_after_merge: int
    max_tiles: int
    palette_size: int
    was_reduced: bool
    # Number of NCLR sub-palette banks the importer actually populated.
    # 1 = single-bank (Layer B path or legacy single-bank Layer A); 2..16 =
    # multi-bank k-means partition. Surfaced in the preview dialog as
    # "16 colors × N banks" so the user knows how much palette headroom
    # they bought.
    banks_used: int = 1


@dataclass(frozen=True)
class LayerImportResult:
    new_ncgr: bytes
    new_nscr: bytes
    new_nclr: bytes
    stats: LayerImportStats


# ---- Step 1: PNG → palette + per-pixel index buffer ---------------------


def load_png_rgba(png_path_or_bytes) -> Tuple[bytes, int, int]:
    """Return ``(rgba, width_px, height_px)`` for a PNG file path or bytes.

    Centralized so the importer doesn't sprout multiple PIL entry points;
    the rest of the pipeline only needs the RGBA buffer.
    """
    from PIL import Image
    if isinstance(png_path_or_bytes, (bytes, bytearray)):
        img = Image.open(io.BytesIO(png_path_or_bytes))
    else:
        img = Image.open(png_path_or_bytes)
    img = img.convert("RGBA")
    w, h = img.size
    return img.tobytes(), w, h


def quantize_image_to_palette(
    rgba: bytes,
    width_px: int,
    height_px: int,
    *,
    palette_size: int = 16,
    transparent_index_0: bool = False,
    alpha_threshold: int = 128,
) -> Tuple[List[Tuple[int, int, int]], List[int]]:
    """Pick a ``palette_size``-color palette via median-cut and map every
    pixel to its nearest index.

    When ``transparent_index_0`` is set, fully-transparent pixels (alpha
    below ``alpha_threshold``) get index 0 and the palette uses slots
    1..N-1 for opaque colors. This matches Layer B's overlay semantics
    (palette idx 0 = transparent in :func:`_render_layer`).

    Returns ``(palette_rgb, indices)`` where ``indices`` is row-major
    length ``width_px * height_px``.
    """
    if len(rgba) != width_px * height_px * 4:
        raise ValueError(
            f"rgba length {len(rgba)} != {width_px}*{height_px}*4"
        )
    n_px = width_px * height_px
    opaque_colors: List[Tuple[int, int, int]] = []
    is_transparent = [False] * n_px
    for i in range(n_px):
        a = rgba[i * 4 + 3]
        if transparent_index_0 and a < alpha_threshold:
            is_transparent[i] = True
            continue
        opaque_colors.append(
            (rgba[i * 4], rgba[i * 4 + 1], rgba[i * 4 + 2])
        )
    reserved = 1 if transparent_index_0 else 0
    color_slots = palette_size - reserved
    if not opaque_colors:
        # All pixels transparent (Layer B is largely empty for many maps).
        palette = [(0, 0, 0)] * palette_size
        return palette, [0] * n_px
    palette_opaque = quantize_palette(opaque_colors, color_slots)
    # Pad palette to full size so callers can rely on len(palette) == N.
    palette: List[Tuple[int, int, int]] = []
    if transparent_index_0:
        palette.append((0, 0, 0))
    palette.extend(palette_opaque)
    while len(palette) < palette_size:
        palette.append((0, 0, 0))

    # Now map every pixel. Use NumPy for the nearest-neighbor scan.
    pal_arr = np.array(palette[reserved:], dtype=np.int32)  # (K, 3)
    indices = np.zeros(n_px, dtype=np.uint8)
    opaque_mask = np.array([not t for t in is_transparent], dtype=bool)
    rgba_arr = np.frombuffer(rgba, dtype=np.uint8).reshape(n_px, 4)
    rgb_opaque = rgba_arr[opaque_mask, :3].astype(np.int32)
    # Distance from each opaque pixel to each palette entry.
    # Shape (M, K): expand and subtract.
    diff = rgb_opaque[:, None, :] - pal_arr[None, :, :]
    dist = np.sum(diff * diff, axis=2)  # (M, K)
    nearest = np.argmin(dist, axis=1).astype(np.uint8) + reserved
    indices[opaque_mask] = nearest
    # Transparent pixels keep index 0 (already zeroed).
    return palette, indices.tolist()


def quantize_image_multi_bank(
    rgba: bytes,
    width_px: int,
    height_px: int,
    *,
    available_banks: Sequence[int] = tuple(range(16)),
    colors_per_bank: int = 16,
    transparent_index_0: bool = False,
    alpha_threshold: int = 128,
    n_iter: int = 4,
) -> Tuple[Dict[int, List[Tuple[int, int, int]]], List[int], List[int]]:
    """K-means partition of 8×8 cells across multiple NCLR sub-palettes.

    Each NSCR cell entry picks one of 16 sub-palettes (bits 12-15), so a
    multi-bank assignment yields up to 16×16=256 effective colors versus
    the 16 a single-bank import is limited to. This is the difference
    between a passable and a faithful Layer A import for vanilla maps,
    which already lean on banks 0..N to keep RMSE single-digit.

    ``available_banks`` is the set of NCLR bank slots the importer may
    write. The A/B bank partition is per-map — vanilla maps put Layer A
    on banks 0..N and Layer B on one high bank (typically 5 or 6, varies
    per map). Callers (e.g. the editor's import flow) should detect the
    other layer's banks from its NSCR and exclude them here so a Layer A
    repaint doesn't clobber Layer B's content bank, and vice versa.

    Returns ``(banks_by_index, cell_bank_assignments, per_pixel_indices)``:
    - ``banks_by_index``: NCLR slot → 16-color RGB palette (only the slots
      actually used; pass straight to ``build_nclr_from_template``).
    - ``cell_bank_assignments``: NCLR slot per cell (row-major over the
      8×8 cell grid). Threaded into NSCR entry bits 12-15.
    - ``per_pixel_indices``: row-major palette index per pixel, length
      ``width_px*height_px``. Each index is local to the cell's bank.

    Algorithm (Lloyd):
    1. Chop into 8×8 cells; compute per-cell opaque-pixel mean.
    2. Seed K bank centroids via median-cut on cell means (deterministic,
       no random init → reproducible imports).
    3. For up to ``n_iter`` iterations:
       - Each bank: union of pixels in assigned cells → median-cut(16) →
         sub-palette.
       - Each cell: quantization error against every bank, take argmin.
       - Stop early if assignments stabilize.
    4. Final pass: per-pixel nearest neighbor against the cell's bank.
    """
    if len(rgba) != width_px * height_px * 4:
        raise ValueError(
            f"rgba length {len(rgba)} != {width_px}*{height_px}*4"
        )
    if width_px % 8 != 0 or height_px % 8 != 0:
        raise ValueError(
            f"image size {width_px}×{height_px} not tile-aligned"
        )
    n_banks = len(available_banks)
    if n_banks <= 0:
        raise ValueError("available_banks must be non-empty")

    tw = width_px // 8
    th = height_px // 8
    n_cells = tw * th

    rgba_arr = np.frombuffer(rgba, dtype=np.uint8).reshape(height_px, width_px, 4)
    rgb_full = rgba_arr[:, :, :3].astype(np.int32)
    alpha_full = rgba_arr[:, :, 3]

    # Reshape into cell-major view via fancy slicing: (th, tw, 8, 8, C).
    cell_rgb_4d = rgb_full.reshape(th, 8, tw, 8, 3).transpose(0, 2, 1, 3, 4)
    cell_rgb = cell_rgb_4d.reshape(n_cells, 64, 3).copy()
    cell_alpha_4d = alpha_full.reshape(th, 8, tw, 8).transpose(0, 2, 1, 3)
    cell_alpha = cell_alpha_4d.reshape(n_cells, 64).copy()

    if transparent_index_0:
        cell_opaque_mask = cell_alpha >= alpha_threshold
        opaque_slots = colors_per_bank - 1
    else:
        cell_opaque_mask = np.ones((n_cells, 64), dtype=bool)
        opaque_slots = colors_per_bank

    # Cell mean (over opaque pixels). All-transparent cells fall back to
    # (0,0,0) — they'll cluster into whichever bank claims the corner of
    # RGB space, but their per-pixel error is zero anyway.
    cell_means = np.zeros((n_cells, 3), dtype=np.int32)
    cell_opaque_count = cell_opaque_mask.sum(axis=1)
    nonzero = cell_opaque_count > 0
    cell_sum = np.where(cell_opaque_mask[:, :, None], cell_rgb, 0).sum(axis=1)
    cell_means[nonzero] = (
        cell_sum[nonzero] // cell_opaque_count[nonzero, None]
    ).astype(np.int32)

    # Initial centroids: median-cut on cell means. quantize_palette is
    # deterministic on a sorted set, so the seeding is reproducible.
    seed = quantize_palette(cell_means, n_banks)
    while len(seed) < n_banks:
        seed.append((0, 0, 0))
    centroids = np.array(seed[:n_banks], dtype=np.int32)
    diff = cell_means[:, None, :] - centroids[None, :, :]
    dist = np.sum(diff * diff, axis=2)
    cell_bank_local = np.argmin(dist, axis=1).astype(np.int32)

    banks_local: List[List[Tuple[int, int, int]]] = [
        [(0, 0, 0)] * colors_per_bank for _ in range(n_banks)
    ]

    def _rebuild_bank(b: int) -> List[Tuple[int, int, int]]:
        assigned = np.where(cell_bank_local == b)[0]
        if len(assigned) == 0:
            return [(0, 0, 0)] * colors_per_bank
        opaque_rows = cell_opaque_mask[assigned]
        if not opaque_rows.any():
            return [(0, 0, 0)] * colors_per_bank
        pixels_arr = cell_rgb[assigned][opaque_rows]  # (M, 3)
        sub = quantize_palette(pixels_arr, opaque_slots)
        if transparent_index_0:
            bank_pal: List[Tuple[int, int, int]] = [(0, 0, 0)] + list(sub)
        else:
            bank_pal = list(sub)
        while len(bank_pal) < colors_per_bank:
            bank_pal.append((0, 0, 0))
        return bank_pal

    for _ in range(n_iter):
        banks_local = [_rebuild_bank(b) for b in range(n_banks)]

        # Reassign each cell to the bank minimizing total quantization error.
        # Compute bank-by-bank to keep the (n_cells, 64, P) tensor small.
        cell_error = np.full((n_cells, n_banks), np.inf, dtype=np.float64)
        for b in range(n_banks):
            pal = np.array(banks_local[b], dtype=np.int32)
            pal_match = pal[1:] if transparent_index_0 else pal
            if len(pal_match) == 0:
                continue
            d = cell_rgb[:, :, None, :] - pal_match[None, None, :, :]
            d = (d * d).sum(axis=3)  # (n_cells, 64, P)
            per_pixel_min = d.min(axis=2)  # (n_cells, 64)
            per_pixel_min = np.where(cell_opaque_mask, per_pixel_min, 0)
            cell_error[:, b] = per_pixel_min.sum(axis=1)
        new_cell_bank = np.argmin(cell_error, axis=1).astype(np.int32)
        if np.array_equal(new_cell_bank, cell_bank_local):
            break
        cell_bank_local = new_cell_bank

    # One last rebuild so banks reflect the final assignment, then per-pixel
    # quantization against each cell's chosen bank.
    banks_local = [_rebuild_bank(b) for b in range(n_banks)]
    out_indices = np.zeros((n_cells, 64), dtype=np.uint8)
    for b in range(n_banks):
        in_bank = np.where(cell_bank_local == b)[0]
        if len(in_bank) == 0:
            continue
        pal = np.array(banks_local[b], dtype=np.int32)
        pal_match = pal[1:] if transparent_index_0 else pal
        offset = 1 if transparent_index_0 else 0
        if len(pal_match) == 0:
            continue
        block_rgb = cell_rgb[in_bank]  # (K, 64, 3)
        d = block_rgb[:, :, None, :] - pal_match[None, None, :, :]
        d = (d * d).sum(axis=3)
        nearest = np.argmin(d, axis=2).astype(np.uint8) + offset
        if transparent_index_0:
            mask = cell_opaque_mask[in_bank]
            nearest = np.where(mask, nearest, 0)
        out_indices[in_bank] = nearest

    # Splat cell-major (n_cells, 64) back to pixel-major (h, w).
    out_2d = (
        out_indices.reshape(th, tw, 8, 8)
        .transpose(0, 2, 1, 3)
        .reshape(height_px, width_px)
    )

    banks_by_index: Dict[int, List[Tuple[int, int, int]]] = {}
    for local_ix in range(n_banks):
        slot = int(available_banks[local_ix])
        banks_by_index[slot] = banks_local[local_ix]
    cell_bank_assignments = [
        int(available_banks[int(b)]) for b in cell_bank_local.tolist()
    ]
    return banks_by_index, cell_bank_assignments, out_2d.flatten().tolist()


# ---- Step 2: indices → 8×8 tiles + canonical-under-flip + dedup ---------


def chop_into_tiles(
    indices: Sequence[int], width_px: int, height_px: int,
) -> List[bytes]:
    """Row-major list of 64-byte (8×8) palette-index tiles.

    Indices are expected in the same row-major layout
    :func:`quantize_image_to_palette` returns.
    """
    if width_px % 8 != 0 or height_px % 8 != 0:
        raise ValueError(
            f"image size {width_px}×{height_px} not tile-aligned"
        )
    tw = width_px // 8
    th = height_px // 8
    indices_arr = np.array(indices, dtype=np.uint8).reshape(height_px, width_px)
    tiles: List[bytes] = []
    for ty in range(th):
        for tx in range(tw):
            block = indices_arr[ty * 8:ty * 8 + 8, tx * 8:tx * 8 + 8]
            tiles.append(block.tobytes())
    return tiles


def _flip_variants(tile: bytes) -> List[bytes]:
    """Return the 4 flip variants ``[none, hflip, vflip, hflip+vflip]``.

    Order matches the ``_FLIP_*`` constants so the index into the returned
    list is the flip-flag value to store in NSCR.
    """
    arr = np.frombuffer(tile, dtype=np.uint8).reshape(8, 8)
    return [
        arr.tobytes(),
        arr[:, ::-1].tobytes(),
        arr[::-1, :].tobytes(),
        arr[::-1, ::-1].tobytes(),
    ]


def canonicalize_tile(tile: bytes) -> Tuple[bytes, int]:
    """Return ``(canonical_bytes, flip_flags)`` where ``canonical_bytes``
    is the lex-smallest of the 4 flip variants and ``flip_flags`` is the
    transform that maps the canonical form back to ``tile``.

    Lex-smallest is an arbitrary but deterministic canonical choice — any
    consistent canonical works for dedup; this one is the cheapest.
    """
    variants = _flip_variants(tile)
    best_flip = 0
    best_bytes = variants[0]
    for f in range(1, 4):
        if variants[f] < best_bytes:
            best_bytes = variants[f]
            best_flip = f
    # `flip_flags` records the transform that, applied to canonical,
    # reproduces the original. Since the 4 flips form a self-inverse
    # group (each flip is its own inverse), the flag is just `best_flip`.
    return best_bytes, best_flip


def dedupe_tiles_with_flips(
    tiles: Sequence[bytes],
) -> Tuple[List[bytes], List[Tuple[int, int]]]:
    """Collapse identical-up-to-flip tiles into a single canonical entry.

    Returns ``(unique_canonical_tiles, cell_assignments)`` where
    ``cell_assignments[i] = (tile_ix, flip_flags)`` for input cell ``i``.

    First-seen wins the slot order so the result is deterministic and
    independent of dict hash randomization.
    """
    seen: dict = {}
    unique: List[bytes] = []
    assignments: List[Tuple[int, int]] = []
    for tile in tiles:
        canonical, flip = canonicalize_tile(tile)
        ix = seen.get(canonical)
        if ix is None:
            ix = len(unique)
            seen[canonical] = ix
            unique.append(canonical)
        assignments.append((ix, flip))
    return unique, assignments


# ---- Step 3: clustering down to max_tiles -------------------------------


def cluster_tiles_to_max(
    unique_tiles: Sequence[bytes],
    cell_assignments: Sequence[Tuple[int, int]],
    palette: Sequence[Tuple[int, int, int]],
    *,
    max_tiles: int,
    locked_tile_indices: Optional[Sequence[int]] = None,
    tile_rgb_expanded: Optional[np.ndarray] = None,
    refine_iters: int = 3,
) -> Tuple[List[bytes], List[Tuple[int, int]]]:
    """Ref-count-weighted k-means++-seeded k-medoids reduction to ``max_tiles``.

    Distance = sum of squared per-pixel RGB differences (palette indices
    expanded to RGB before comparison so the distance is perceptual,
    not just integer-label distance).

    Algorithm:
    1. **Init medoids (k-means++ seeded)**: locked tiles are fixed. For each
       remaining slot, greedily pick the unchosen tile maximising
       ``ref_count[t] · min_dist²(t, already_chosen)``. The first non-locked
       seed bootstraps from the highest-ref-count tile (distance term is
       uniform with no seeds yet, so popularity wins). Once the seed set
       grows, near-duplicates of an already-chosen tile score near zero on
       the distance term and get suppressed — freeing slots for distinct
       tiles. Popular-and-distinct tiles dominate; ref_count=1 outliers
       can't run the table because their popularity term is tiny.
    2. **Assign**: every tile (medoid or not) maps to its nearest medoid via
       a single BLAS GEMM (``‖a−b‖² = ‖a‖² + ‖b‖² − 2·a·b``). The matrix is
       ``(n, k)`` floats — for n≈1940, k=1024 that's 8 MB and one matmul.
    3. **Refine** (up to ``refine_iters`` Lloyd passes): within each cluster,
       pick the member that minimises ref-count-weighted intra-cluster sq
       distance. Locked medoids can't move. Reassign and repeat until stable.

    Init is O(n·k·d) — k matrix-vector products to track the running
    min_dist² — which adds ~0.5s at n≈2000, k=1024, d=192. Bought back
    many times over in quality: pure top-by-ref-count seeding lets
    near-duplicates of a popular tile (e.g. 50 brown wood patches) fill
    slots that would otherwise go to distinct features (carved balustrade,
    architectural detail, floor patterning). k-means++ collapses those
    duplicates by scoring them near zero on the distance term.

    ``locked_tile_indices`` lists tiles that must survive — typically the
    slots NaXn animations overwrite at runtime, which the rest of Layer A
    references by slot index and cannot be remapped.

    Caller is responsible for re-resolving any flip flags: this function
    only swaps tile indices, not orientations, so a cell that originally
    pointed at a non-medoid tile keeps its original flip (which may now
    produce a different-looking tile, but its medoid is the closest
    survivor in RGB-space).
    """
    n = len(unique_tiles)
    if n <= max_tiles:
        return list(unique_tiles), list(cell_assignments)

    # Expand each tile (64 palette indices) to (192,) RGB float vector.
    # Caller may supply ``tile_rgb_expanded`` directly when tiles span
    # multiple sub-palettes (multi-bank import) — there's no single
    # ``palette`` that meaningfully expands them, so expansion happens
    # upstream using each tile's primary bank.
    if tile_rgb_expanded is not None:
        if tile_rgb_expanded.shape != (n, 64 * 3):
            raise ValueError(
                f"tile_rgb_expanded must be shape ({n}, 192), "
                f"got {tile_rgb_expanded.shape}"
            )
        tile_rgb = tile_rgb_expanded.astype(np.float32, copy=False)
    else:
        pal_arr = np.array(palette, dtype=np.float32)  # (P, 3)
        tile_idx_arr = np.frombuffer(
            b"".join(unique_tiles), dtype=np.uint8,
        ).reshape(n, 64)
        tile_rgb = pal_arr[tile_idx_arr].reshape(n, 64 * 3)  # (n, 192)

    # Reference counts double as Lloyd weights: a high-ref tile both
    # pulls the medoid toward itself during refinement and biases the
    # initial top-K selection.
    ref_count = np.zeros(n, dtype=np.float64)
    for t, _ in cell_assignments:
        if 0 <= t < n:
            ref_count[t] += 1.0

    locked = np.zeros(n, dtype=bool)
    if locked_tile_indices is not None:
        for ix in locked_tile_indices:
            if 0 <= ix < n:
                locked[ix] = True

    target = max(max_tiles, int(locked.sum()))
    if target >= n:
        return list(unique_tiles), list(cell_assignments)

    # ---- Init: k-means++ seeded, ref_count-weighted ---------------------
    # Maintain ``min_dist_sq[t]`` = squared RGB distance from tile t to
    # its nearest already-chosen seed. Each iteration picks the unchosen
    # tile maximising ``ref_count[t] * min_dist_sq[t]`` and folds the new
    # seed into the running minimum.
    tile_norms = np.einsum("ij,ij->i", tile_rgb, tile_rgb).astype(np.float32)
    locked_ix_list = np.where(locked)[0].tolist()
    chosen_list: List[int] = list(locked_ix_list)
    remaining = np.ones(n, dtype=bool)
    if chosen_list:
        remaining[chosen_list] = False
        seeds_rgb = tile_rgb[chosen_list]
        seeds_norms = np.einsum("ij,ij->i", seeds_rgb, seeds_rgb)
        all_d = (
            tile_norms[:, None]
            + seeds_norms[None, :]
            - 2.0 * (tile_rgb @ seeds_rgb.T)
        )
        min_dist_sq = np.clip(all_d.min(axis=1), 0.0, None).astype(np.float64)
    else:
        # Bootstrap with the highest-ref-count tile; np.argmax breaks ties
        # toward the lowest index for determinism.
        bootstrap = int(np.argmax(ref_count))
        chosen_list.append(bootstrap)
        remaining[bootstrap] = False
        boot_rgb = tile_rgb[bootstrap]
        min_dist_sq = (
            tile_norms.astype(np.float64)
            + float(tile_norms[bootstrap])
            - 2.0 * (tile_rgb @ boot_rgb).astype(np.float64)
        )
        np.clip(min_dist_sq, 0.0, None, out=min_dist_sq)

    while len(chosen_list) < target:
        score = ref_count * min_dist_sq
        score[~remaining] = -np.inf
        next_ix = int(np.argmax(score))
        if not np.isfinite(score[next_ix]) or score[next_ix] <= 0.0:
            # Remaining tiles are RGB-duplicates of already-chosen seeds.
            # Fall back to plain ref_count for the rest of the slots.
            rc_score = np.where(remaining, ref_count, -np.inf)
            next_ix = int(np.argmax(rc_score))
            if not np.isfinite(rc_score[next_ix]):
                break
        chosen_list.append(next_ix)
        remaining[next_ix] = False
        new_rgb = tile_rgb[next_ix]
        new_dist = (
            tile_norms.astype(np.float64)
            + float(tile_norms[next_ix])
            - 2.0 * (tile_rgb @ new_rgb).astype(np.float64)
        )
        np.minimum(min_dist_sq, new_dist, out=min_dist_sq)

    medoid_arr = np.sort(np.array(chosen_list, dtype=np.int32))

    # ---- Assign + refine ------------------------------------------------

    def assign_all(medoids: np.ndarray) -> np.ndarray:
        m_rgb = tile_rgb[medoids]                     # (k, 192)
        m_norms = np.einsum("ij,ij->i", m_rgb, m_rgb)  # (k,)
        dots = tile_rgb @ m_rgb.T                      # (n, k) via BLAS
        dists = tile_norms[:, None] + m_norms[None, :] - 2.0 * dots
        # np.argmin breaks ties by first index → deterministic.
        return dists.argmin(axis=1).astype(np.int32)

    assign = assign_all(medoid_arr)

    # Lloyd: each cluster's medoid moves to the member minimising
    # Σ_i ref_count[i] · ‖tile_rgb[i] - tile_rgb[candidate]‖².
    # Locked medoids are pinned and skipped.
    for _ in range(max(0, refine_iters)):
        new_medoid_arr = medoid_arr.copy()
        changed = False
        for cluster_ix in range(len(medoid_arr)):
            current = int(medoid_arr[cluster_ix])
            if locked[current]:
                continue
            members = np.where(assign == cluster_ix)[0]
            if len(members) <= 1:
                continue
            member_rgb = tile_rgb[members]                       # (M, 192)
            member_norms = tile_norms[members]                   # (M,)
            mem_dots = member_rgb @ member_rgb.T                  # (M, M)
            pairwise = (
                member_norms[:, None] + member_norms[None, :] - 2.0 * mem_dots
            )
            weights = ref_count[members]
            # cost[j] = Σ_i weights[i] · pairwise[i, j].
            cost = weights @ pairwise                             # (M,)
            best_local = int(cost.argmin())
            best_global = int(members[best_local])
            if best_global != current:
                new_medoid_arr[cluster_ix] = best_global
                changed = True
        if not changed:
            break
        medoid_arr = new_medoid_arr
        assign = assign_all(medoid_arr)

    # ---- Materialise output ---------------------------------------------
    # Each input tile_ix t routes to medoid_arr[assign[t]]. Survivors are
    # exactly the set ``set(medoid_arr)``. Keep them in sorted order so
    # the new tile-index space is deterministic.
    survivors = np.unique(medoid_arr)
    old_to_new = {int(old): new for new, old in enumerate(survivors.tolist())}
    new_tiles = [unique_tiles[ix] for ix in survivors.tolist()]

    # tile_to_medoid[t] = the survivor index t collapses into.
    tile_to_medoid = medoid_arr[assign]  # (n,)

    new_assignments: List[Tuple[int, int]] = []
    for t, flip in cell_assignments:
        new_assignments.append((old_to_new[int(tile_to_medoid[t])], flip))

    return new_tiles, new_assignments


# ---- Step 4: pack tiles into 4bpp NCGR bytes ----------------------------


def pack_tiles_4bpp(tile_indices: Sequence[bytes]) -> bytes:
    """Pack a list of 64-byte tile buffers into 4bpp NCGR tile-data bytes.

    Two pixels per byte, low nibble = left pixel (matches
    :func:`digimon_core.sprite.unpack_pixels` for bit_depth=3).
    """
    out = bytearray()
    for tile in tile_indices:
        if len(tile) != 64:
            raise ValueError(f"tile must be 64 bytes, got {len(tile)}")
        for py in range(8):
            base = py * 8
            for col in range(0, 8, 2):
                lo = tile[base + col] & 0x0F
                hi = tile[base + col + 1] & 0x0F
                out.append((hi << 4) | lo)
    return bytes(out)


# ---- Step 5: end-to-end orchestration -----------------------------------


def _collect_locked_slots_from_naxn(
    nscr_entries: Sequence[int],
    naxn_dst_ranges: Optional[Sequence[Tuple[int, int]]],
) -> List[int]:
    """Translate NaXn dst-tile ranges into the unique-tile-index space
    *before* clustering — but the indexing happens AFTER dedup runs, so
    this is just the raw tile_ix values from the original NSCR that fall
    inside any animation's dst range. The orchestrator passes these
    along to ``cluster_tiles_to_max`` only AFTER mapping through the
    dedup result.

    Stub kept here so callers don't have to know the format; the actual
    mapping lives in :func:`import_layer_from_png`.
    """
    if not naxn_dst_ranges:
        return []
    locked: List[int] = []
    for entry in nscr_entries:
        tile_ix = entry & 0x3FF
        for lo, hi in naxn_dst_ranges:
            if lo <= tile_ix <= hi:
                locked.append(tile_ix)
                break
    return locked


def import_layer_from_png(
    png_path_or_bytes,
    *,
    target_width_px: int,
    target_height_px: int,
    original_ncgr: bytes,
    original_nscr: bytes,
    original_nclr: bytes,
    palette_bank: int = 0,
    is_transparent_layer: bool = False,
    max_tiles: int = 1024,
    naxn_dst_ranges: Optional[Sequence[Tuple[int, int]]] = None,
    use_multi_bank: bool = False,
    available_banks: Optional[Sequence[int]] = None,
) -> LayerImportResult:
    """Convert a flat PNG into (NCGR, NSCR, NCLR) for one BG layer.

    ``is_transparent_layer=True`` reserves slot 0 of every NCLR bank as a
    sentinel: pixels with alpha < ``alpha_threshold`` quantize to that
    slot; opaque pixels never do. The renderer interprets the sentinel
    differently per layer — Layer B shows it as transparent (``backdrop_opaque
    =False``); Layer A paints it with the actual slot 0 colour so it
    works as the off-camera filler / camera-bound strip. Both layer
    imports should set this flag; the editor enables it for Layer A so
    bank 0 slot 0 can be recoloured without leaking into content cells.

    ``use_multi_bank=True`` partitions cells across the NCLR sub-palettes
    listed in ``available_banks`` (default all 16) via k-means. This
    pushes effective color depth from 16 to up to 256 — the difference
    between a passable Layer A import and a faithful one. ``palette_bank``
    is ignored in this mode; the NSCR's per-cell bank bits come from the
    k-means assignment. Each cell's NCGR tile holds palette-local indices
    [0,16) interpreted against whichever NCLR bank the NSCR entry picks.

    When ``use_multi_bank=False`` (legacy / Layer B path), all cells use
    ``palette_bank`` and the NCLR template only has that one bank
    rewritten — so e.g. importing Layer B doesn't disturb Layer A's
    palette.

    ``naxn_dst_ranges`` is the list of (dst_lo, dst_hi) tile-index ranges
    NaXn animations overwrite at runtime for this layer. The original
    tiles in those slots are preserved as-is (animation writes into them
    each tick anyway), and the slots are exempted from merge-as-loser so
    later NSCR cells keep pointing at the right runtime-driven content.

    Output triple uses the originals as templates so all NDS-format
    headers (NCGR RAHC trailing fields, NSCR NRCS flags, NCLR PCMP)
    survive unchanged. The caller splices the result over the FAT slots
    via the existing btmap dirty-cache + serialize_all pipeline.
    """
    rgba, w, h = load_png_rgba(png_path_or_bytes)
    if (w, h) != (target_width_px, target_height_px):
        raise ValueError(
            f"PNG is {w}×{h}, expected {target_width_px}×{target_height_px}"
        )

    n_cells = (w // 8) * (h // 8)
    if use_multi_bank:
        # Cap the requested bank set to slots that actually exist in the
        # template NCLR — many vanilla btmap NCLRs ship with only 4-6
        # banks and ``build_nclr_from_template`` raises on out-of-range
        # slot writes. The user can grow this later if we ever extend
        # NCLRs, but for now treat the template count as authoritative.
        nclr_banks, _ = parse_nclr(original_nclr)
        nclr_bank_count = len(nclr_banks)
        requested = (
            tuple(available_banks) if available_banks is not None
            else tuple(range(16))
        )
        effective = tuple(b for b in requested if 0 <= b < nclr_bank_count)
        if not effective:
            effective = (
                palette_bank if 0 <= palette_bank < nclr_bank_count else 0,
            )
        banks_by_index, cell_bank_per_cell, indices = quantize_image_multi_bank(
            rgba, w, h,
            available_banks=effective,
            colors_per_bank=16,
            transparent_index_0=is_transparent_layer,
        )
    else:
        palette, indices = quantize_image_to_palette(
            rgba, w, h,
            palette_size=16,
            transparent_index_0=is_transparent_layer,
        )
        banks_by_index = {palette_bank: palette}
        cell_bank_per_cell = [palette_bank] * n_cells

    raw_tiles = chop_into_tiles(indices, w, h)
    unique, assignments = dedupe_tiles_with_flips(raw_tiles)
    unique_after_flip = len(unique)

    # NaXn locking: identify which post-dedup tile indices correspond to
    # tile slots NaXn writes into. We can't lock by *new* index until we
    # know the assignment, so we walk the assignments and lock any
    # canonical tile that ends up at a runtime-overwritten slot in the
    # original NSCR. For static cells (no animation overlap), this is a
    # no-op since locked_tile_indices stays empty.
    locked_new_indices: List[int] = []
    if naxn_dst_ranges:
        _, _, orig_entries = btmap_module.parse_nscr(original_nscr)
        for cell_ix, orig_entry in enumerate(orig_entries):
            orig_tile = orig_entry & 0x3FF
            for lo, hi in naxn_dst_ranges:
                if lo <= orig_tile <= hi:
                    if cell_ix < len(assignments):
                        locked_new_indices.append(assignments[cell_ix][0])
                    break

    # Pre-expand each unique tile to RGB using its most-referenced bank.
    # Multi-bank tiles may legitimately appear under several banks (same
    # canonical pattern, different color scheme); clustering only needs a
    # representative appearance — picking the dominant bank keeps the
    # distance metric stable for cells that won't move.
    tile_rgb_expanded: Optional[np.ndarray] = None
    if use_multi_bank and len(unique) > max_tiles:
        # Count (tile, bank) co-occurrences, then resolve each tile to its
        # primary bank for RGB expansion.
        tile_bank_counts: Dict[Tuple[int, int], int] = {}
        for cell_ix, (tile_ix, _) in enumerate(assignments):
            bank = cell_bank_per_cell[cell_ix]
            key = (tile_ix, bank)
            tile_bank_counts[key] = tile_bank_counts.get(key, 0) + 1
        primary_bank: Dict[int, int] = {}
        primary_count: Dict[int, int] = {}
        for (tile_ix, bank), count in tile_bank_counts.items():
            if count > primary_count.get(tile_ix, -1):
                primary_count[tile_ix] = count
                primary_bank[tile_ix] = bank
        pal_arr_by_bank: Dict[int, np.ndarray] = {
            b: np.array(banks_by_index[b], dtype=np.float32)
            for b in banks_by_index
        }
        tile_idx_arr = (
            np.frombuffer(b"".join(unique), dtype=np.uint8).reshape(len(unique), 64)
        )
        tile_rgb_expanded = np.zeros((len(unique), 192), dtype=np.float32)
        for ix in range(len(unique)):
            bank = primary_bank.get(ix, palette_bank)
            tile_rgb_expanded[ix] = (
                pal_arr_by_bank[bank][tile_idx_arr[ix]].reshape(192)
            )

    cluster_palette = (
        next(iter(banks_by_index.values())) if banks_by_index
        else [(0, 0, 0)] * 16
    )
    reduced_unique, reduced_assignments = cluster_tiles_to_max(
        unique, assignments, cluster_palette,
        max_tiles=max_tiles,
        locked_tile_indices=locked_new_indices or None,
        tile_rgb_expanded=tile_rgb_expanded,
    )

    # NCGR: pack canonical tiles 4bpp.
    tile_bytes = pack_tiles_4bpp(reduced_unique)
    new_ncgr = build_ncgr_from_template(tile_bytes, original_ncgr)

    # NSCR: per-cell bank from the multi-bank assignment (or the constant
    # ``palette_bank`` in single-bank mode).
    new_entries: List[int] = []
    for cell_ix, (tile_ix, flip) in enumerate(reduced_assignments):
        cell_bank = cell_bank_per_cell[cell_ix]
        hflip = 1 if (flip & _FLIP_H) else 0
        vflip = 1 if (flip & _FLIP_V) else 0
        entry = (
            (tile_ix & 0x3FF)
            | (hflip << 10)
            | (vflip << 11)
            | ((cell_bank & 0xF) << 12)
        )
        new_entries.append(entry)
    new_nscr = btmap_module.build_nscr_from_template(
        new_entries, target_width_px, target_height_px, original_nscr,
    )

    # NCLR: write every populated bank. In single-bank mode that's one
    # bank only; sibling banks stay byte-identical so a later import of
    # the other layer can use them.
    new_nclr = build_nclr_from_template(original_nclr, banks_by_index)

    banks_used = len({cell_bank_per_cell[ix] for ix in range(n_cells)})
    stats = LayerImportStats(
        cells_total=len(assignments),
        unique_tiles_raw=len({bytes(t) for t in raw_tiles}),
        unique_after_flip_dedup=unique_after_flip,
        unique_after_merge=len(reduced_unique),
        max_tiles=max_tiles,
        palette_size=16,
        was_reduced=(len(reduced_unique) < unique_after_flip),
        banks_used=banks_used,
    )
    return LayerImportResult(
        new_ncgr=new_ncgr,
        new_nscr=new_nscr,
        new_nclr=new_nclr,
        stats=stats,
    )
