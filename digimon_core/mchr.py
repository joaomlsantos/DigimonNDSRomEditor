"""DWDD overworld sprite codec — port of ``mchr_chr_unpack.py``'s pure functions.

PIL-free: :func:`render_frame_rgba` returns a flat RGBA buffer + dimensions
the Qt layer wraps as ``QImage``. Mirrors :mod:`digimon_core.sprite`'s policy.

MCHR is *not* NCGR. Each ``MCHR_CHR.PAK`` entry decompresses (NDS BIOS
RLE-0x30) to::

    u32 frame_count
    u32 bytes_per_frame        # always a multiple of 32 (one 8x8 4bpp tile)
    u8  frame_data[frame_count][bytes_per_frame]   # raw 4bpp tile bytes

There's no RAHC wrapper. ``MCHR_PAL.PAK`` entries similarly decompress to
raw BGR555 palette banks (no TTLP/NCLR), 16 colors per bank.

Width inference for the tile-grid layout: DWDD overworld sprites all come
in NDS OAM legal shapes (1/2/4/8/16/32/64 tiles). Where a tile count is
ambiguous (tall vs. wide), we default to *tall* — overworld content is
virtually all standing humanoids/digimon. Validated end-to-end against all
890 sprites in MCHR_CHR.PAK 2026-06-05.

Sprite→palette index mapping is 1:1 for sprites 0..662; past that the
mapping is irregular (sprite 663 → pal 664, sprite 754+ → pal sprite+16,
others still unresolved). The codec is mapping-agnostic — callers pass the
palette bytes they want. The editor exposes a per-sprite palette-index
spinner so users can pin the right palette until we work out the table.

Module surface:

- :class:`MchrEntry` — parsed CHR entry (frames + dimensions)
- :func:`parse_mchr_chr_entry` / :func:`encode_mchr_chr_entry`
- :func:`parse_palette_bgr555` / :func:`encode_palette_bgr555`
- :func:`pick_tile_grid` — NDS OAM tile-count → (width_tiles, height_tiles)
- :func:`render_frame_rgba` — one frame → RGBA bytes
- :func:`decode_frame_to_indices` — one frame → raster palette indices
- :func:`encode_frame_from_indices` — raster indices → 4bpp tile bytes
- :func:`quantize_rgba_to_indices` — RGBA buffer → nearest-palette indices
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import List, Optional, Tuple

from digimon_core.sprite import Palette


# ---- MCHR_CHR entry -------------------------------------------------------

@dataclass(frozen=True)
class MchrEntry:
    """One decoded MCHR_CHR entry: N frames of identical tile-byte length."""

    frame_count: int
    bytes_per_frame: int
    frames: Tuple[bytes, ...]

    @property
    def tiles_per_frame(self) -> int:
        return self.bytes_per_frame // 32  # 4bpp: 8x8 tile = 32 bytes


def parse_mchr_chr_entry(decompressed: bytes) -> MchrEntry:
    """Decompose decompressed MCHR_CHR entry bytes into per-frame tile blobs.

    Raises ``ValueError`` if the header math doesn't account for every byte
    or the bytes-per-frame isn't a tile multiple — both would silently
    misalign the frame strip if accepted.
    """
    if len(decompressed) < 8:
        raise ValueError(f"too short for header ({len(decompressed)}B)")
    frame_count, bytes_per_frame = struct.unpack_from("<II", decompressed, 0)
    expected = 8 + frame_count * bytes_per_frame
    if len(decompressed) != expected:
        raise ValueError(
            f"header says {frame_count} x {bytes_per_frame}B "
            f"= {expected} bytes; got {len(decompressed)} bytes"
        )
    if bytes_per_frame % 32 != 0:
        raise ValueError(
            f"bytes_per_frame={bytes_per_frame} not a tile multiple (32)"
        )
    frames: List[bytes] = []
    off = 8
    for _ in range(frame_count):
        frames.append(decompressed[off:off + bytes_per_frame])
        off += bytes_per_frame
    return MchrEntry(frame_count, bytes_per_frame, tuple(frames))


def encode_mchr_chr_entry(frames: List[bytes]) -> bytes:
    """Pack frames into the MCHR_CHR entry bytes a fresh entry expects.

    Every frame must be the same length and a multiple of 32 bytes (one
    8x8 4bpp tile). The output is the *uncompressed* entry; callers wrap
    in :func:`digimon_core.sprite.compress_rle30` before writing into a
    PakFile.
    """
    if not frames:
        raise ValueError("need at least one frame")
    bytes_per_frame = len(frames[0])
    if bytes_per_frame == 0 or bytes_per_frame % 32 != 0:
        raise ValueError(
            f"bytes_per_frame={bytes_per_frame} not a tile multiple (32)"
        )
    for i, f in enumerate(frames):
        if len(f) != bytes_per_frame:
            raise ValueError(
                f"frame {i} length {len(f)} != frame 0 length {bytes_per_frame}"
            )
    out = bytearray(struct.pack("<II", len(frames), bytes_per_frame))
    for f in frames:
        out += f
    return bytes(out)


# ---- palette (raw BGR555) -------------------------------------------------

def parse_palette_bgr555(decompressed: bytes) -> Palette:
    """16-bit BGR555 little-endian → ``(R, G, B)`` triples in 0..255.

    Matches :func:`digimon_core.sprite.parse_nclr`'s output type so the
    Qt rendering path is identical for SPR and MCHR sprites. Alpha for
    index 0 is the renderer's concern (see :func:`render_frame_rgba`).
    """
    out: List[Tuple[int, int, int]] = []
    for i in range(0, len(decompressed) - 1, 2):
        word = decompressed[i] | (decompressed[i + 1] << 8)
        r5 = word & 0x1F
        g5 = (word >> 5) & 0x1F
        b5 = (word >> 10) & 0x1F
        # 5-bit -> 8-bit (replicate top bits into bottom for full-range output).
        r8 = (r5 << 3) | (r5 >> 2)
        g8 = (g5 << 3) | (g5 >> 2)
        b8 = (b5 << 3) | (b5 >> 2)
        out.append((r8, g8, b8))
    return out


def encode_palette_bgr555(palette: Palette) -> bytes:
    """Pack ``(R, G, B)`` triples (0..255 channels) into BGR555 little-endian.

    The DWDD engine reads 32 bytes (16 colors) per bank; callers wanting
    a bank should ensure ``len(palette) == 16``. We don't pad here so
    round-trip tests can detect length drift.
    """
    out = bytearray()
    for r, g, b in palette:
        r5 = (r >> 3) & 0x1F
        g5 = (g >> 3) & 0x1F
        b5 = (b >> 3) & 0x1F
        word = (b5 << 10) | (g5 << 5) | r5
        out += struct.pack("<H", word)
    return bytes(out)


# ---- NDS OAM tile-grid heuristic ------------------------------------------

# NDS OAM sprite shapes, keyed by tile count → (width_tiles, height_tiles).
# Where two shapes share a tile count (tall vs. wide), we keep the *tall*
# one (DWDD overworld is standing humanoids/digimon). The 4-tile slot has
# three legal candidates (16x16 square, 32x8 wide, 8x32 tall); square is
# the most common small-prop shape, so we pick that.
_NDS_OAM_TILE_GRID = {
    1:  (1, 1),   # 8x8
    2:  (1, 2),   # 8x16 tall   (alt: 16x8 wide)
    4:  (2, 2),   # 16x16 square (alts: 32x8 wide, 8x32 tall)
    8:  (2, 4),   # 16x32 tall  (alt: 32x16 wide) — main char male
    16: (4, 4),   # 32x32 square (alt: 64x16 wide)
    32: (4, 8),   # 32x64 tall  (alt: 64x32 wide) — large NPCs
    64: (8, 8),   # 64x64 square
}


def pick_tile_grid(tile_count: int) -> Tuple[int, int]:
    """Choose ``(width_tiles, height_tiles)`` for a given tile count.

    Looks up :data:`_NDS_OAM_TILE_GRID` first (covers every entry in
    vanilla MCHR_CHR.PAK). Falls back to a near-square decomposition for
    exotic counts that might appear in modded content, still preferring
    tall over wide.
    """
    if tile_count <= 0:
        return (1, 1)
    if tile_count in _NDS_OAM_TILE_GRID:
        return _NDS_OAM_TILE_GRID[tile_count]
    target = int(tile_count ** 0.5)
    for w in range(target, 0, -1):
        if tile_count % w == 0:
            h = tile_count // w
            return (w, h) if h >= w else (h, w)
    return (1, tile_count)


# ---- frame → RGBA ---------------------------------------------------------

def render_frame_rgba(
    frame_bytes: bytes,
    palette: Palette,
    width_tiles: Optional[int] = None,
    transparent_index_0: bool = True,
) -> Tuple[bytes, int, int]:
    """Decode one 4bpp tile-stream frame into a flat RGBA buffer.

    Returns ``(rgba_bytes, width_px, height_px)`` — row-major, 8 bits per
    channel. The Qt layer wraps as
    ``QImage(buf, w, h, w*4, QImage.Format_RGBA8888)``.

    ``width_tiles`` overrides the NDS-OAM heuristic for callers who know
    the sprite shape independently (e.g. from a future palette-mapping
    table or an explicit user override).
    """
    tile_count = len(frame_bytes) // 32
    if width_tiles is None:
        wt, ht = pick_tile_grid(tile_count)
    else:
        wt = width_tiles
        ht = (tile_count + wt - 1) // wt
    w = wt * 8
    h = ht * 8
    pal_len = len(palette)
    out = bytearray(w * h * 4)
    for tile_i in range(tile_count):
        tx = (tile_i % wt) * 8
        ty = (tile_i // wt) * 8
        for row in range(8):
            row_off = tile_i * 32 + row * 4
            for col_pair in range(4):
                b = frame_bytes[row_off + col_pair]
                lo = b & 0x0F
                hi = (b >> 4) & 0x0F
                for sub, idx in ((0, lo), (1, hi)):
                    if idx >= pal_len:
                        r, g, bb, a = 0, 0, 0, 0
                    else:
                        r, g, bb = palette[idx]
                        a = 0 if (transparent_index_0 and idx == 0) else 255
                    px_x = tx + col_pair * 2 + sub
                    px_y = ty + row
                    j = (px_y * w + px_x) * 4
                    out[j:j + 4] = bytes((r, g, bb, a))
    return bytes(out), w, h


# ---- frame ↔ raster indices ----------------------------------------------

def decode_frame_to_indices(
    frame_bytes: bytes, width_tiles: Optional[int] = None
) -> Tuple[List[int], int, int]:
    """Decode one 4bpp tile-stream frame into a row-major index buffer.

    Returns ``(indices, w_px, h_px)`` — the palette index per pixel. Inverse
    of :func:`encode_frame_from_indices`. Used by the PNG-sheet exporter so
    we can emit an indexed-color PNG (preserving exact indices) without
    going through RGBA and quantizing back.
    """
    tile_count = len(frame_bytes) // 32
    if width_tiles is None:
        wt, ht = pick_tile_grid(tile_count)
    else:
        wt = width_tiles
        ht = (tile_count + wt - 1) // wt
    w_px = wt * 8
    h_px = ht * 8
    indices = [0] * (w_px * h_px)
    for tile_i in range(tile_count):
        tx = (tile_i % wt) * 8
        ty = (tile_i // wt) * 8
        for row in range(8):
            row_off = tile_i * 32 + row * 4
            for col_pair in range(4):
                b = frame_bytes[row_off + col_pair]
                lo = b & 0x0F
                hi = (b >> 4) & 0x0F
                base = (ty + row) * w_px + tx + col_pair * 2
                indices[base] = lo
                indices[base + 1] = hi
    return indices, w_px, h_px


# ---- encode (raster indices → tile bytes) ---------------------------------

def encode_frame_from_indices(
    indices: List[int], w_px: int, h_px: int
) -> bytes:
    """Pack a row-major 4bpp pixel grid into MCHR tile bytes.

    Walks the raster in tile-major order (8x8 tiles, scanning left-to-right
    then top-to-bottom), packing two 4-bit indices per byte (low nibble =
    left pixel, high nibble = right pixel). Inverse of
    :func:`render_frame_rgba`'s tile→pixel mapping.

    Raises ``ValueError`` if ``w_px`` / ``h_px`` aren't multiples of 8 or
    the indices buffer length doesn't match ``w_px * h_px``.
    """
    if w_px % 8 or h_px % 8:
        raise ValueError(f"dimensions must be 8-aligned ({w_px} x {h_px})")
    if len(indices) != w_px * h_px:
        raise ValueError(
            f"indices length {len(indices)} != {w_px} * {h_px} = {w_px * h_px}"
        )
    tw, th = w_px // 8, h_px // 8
    out = bytearray()
    for ty in range(th):
        for tx in range(tw):
            x0, y0 = tx * 8, ty * 8
            for py in range(8):
                row = y0 + py
                for px in range(0, 8, 2):
                    lo = indices[row * w_px + x0 + px] & 0x0F
                    hi = indices[row * w_px + x0 + px + 1] & 0x0F
                    out.append((hi << 4) | lo)
    return bytes(out)


# ---- RGBA → palette index quantization -----------------------------------

def quantize_rgba_to_indices(
    rgba: bytes,
    palette: Palette,
    *,
    transparent_index_0: bool = True,
    alpha_threshold: int = 128,
) -> List[int]:
    """Nearest-neighbour-in-RGB quantization of an RGBA buffer to indices.

    Used on the PNG-import path when the user's image isn't already
    paletted (or is paletted with a colour table we can't trust). Pixels
    below ``alpha_threshold`` map to index 0 when ``transparent_index_0``
    is set — matching :func:`render_frame_rgba`'s rendering convention so
    a render → edit RGB → import round-trip preserves the transparent
    background.

    Distance is squared Euclidean in 8-bit RGB. For a 16-colour palette
    this is fast enough not to need a cache.
    """
    if len(rgba) % 4 != 0:
        raise ValueError(f"rgba length {len(rgba)} not a multiple of 4")
    n_px = len(rgba) // 4
    pal_n = len(palette)
    if pal_n == 0:
        raise ValueError("palette is empty")
    out: List[int] = [0] * n_px
    for i in range(n_px):
        a = rgba[i * 4 + 3]
        if transparent_index_0 and a < alpha_threshold:
            out[i] = 0
            continue
        r = rgba[i * 4]
        g = rgba[i * 4 + 1]
        b = rgba[i * 4 + 2]
        best_idx = 0
        best_d = 1 << 30
        for k in range(pal_n):
            pr, pg, pb = palette[k]
            dr = r - pr
            dg = g - pg
            db = b - pb
            d = dr * dr + dg * dg + db * db
            if d < best_d:
                best_d = d
                best_idx = k
                if d == 0:
                    break
        out[i] = best_idx
    return out
