"""NCER (cell / OAM) parser used to size a replacement sprite sheet.

Sprite editing in §11 needs one number from the NCER: the minimum tile
count the new NCGR must contain so every OAM the engine renders still
points at valid tile data. That number is what
:func:`min_tiles_required` returns.

OBJ mapping mode (NCER's KBEC header, ``mapping`` field) sets the OAM
``tile`` field's *slot size in bytes*:

- 2D mapping (``mapping & 0xFF == 0``): slot stride is always **32B**,
  regardless of bit depth. A 4bpp 8x8 tile (32B) consumes one slot per
  tile; an **8bpp 8x8 tile (64B) consumes two slots**, so the linear
  index into the NCGR's tile array is ``tile / 2``.

- 1D mapping (``mapping & 0xFF != 0``): slot stride is
  ``32 << (mapping & 0xFF)`` bytes per slot. The linear tile index is
  ``tile * slot_bytes / bytes_per_tile``.

We work in bytes throughout so the same formula covers both cases plus
the 8bpp-in-2D quirk:

    end_byte = oam.tile * slot_bytes + oam.n_tiles * bytes_per_tile
    min_tiles = ceil(end_byte / bytes_per_tile)

Verified empirically: this matches the CHR tile count across all 1627
vanilla Dusk SPR_CHR entries, including the 477 8bpp+2D ones the
naive linear formula over-counted.

This module reuses :func:`digimon_core.sprite.maybe_decompress` and
:func:`digimon_core.sprite.find_block` so callers can hand it the raw
PAK entry bytes (RLE-30 compressed) or the decompressed NCER.
"""
from __future__ import annotations

import struct
from typing import List, Tuple

from .sprite import find_block, maybe_decompress


SHAPE_SIZE = {
    (0, 0): ( 8,  8), (0, 1): (16, 16), (0, 2): (32, 32), (0, 3): (64, 64),
    (1, 0): (16,  8), (1, 1): (32,  8), (1, 2): (32, 16), (1, 3): (64, 32),
    (2, 0): ( 8, 16), (2, 1): ( 8, 32), (2, 2): (16, 32), (2, 3): (32, 64),
}


def _s8(v: int) -> int:
    return v - 256 if v & 0x80 else v


def _s9(v: int) -> int:
    return v - 512 if v & 0x100 else v


class Oam:
    __slots__ = ("x", "y", "w", "h", "tile", "is8bpp", "hflip", "vflip", "pal", "prio")

    def __init__(self, x, y, w, h, tile, is8bpp, hflip, vflip, pal, prio):
        self.x = x
        self.y = y
        self.w = w
        self.h = h
        self.tile = tile
        self.is8bpp = is8bpp
        self.hflip = hflip
        self.vflip = vflip
        self.pal = pal
        self.prio = prio

    @property
    def n_tiles(self) -> int:
        return (self.w // 8) * (self.h // 8)


class Cell:
    __slots__ = ("oams", "bbox")

    def __init__(self, oams: List[Oam], bbox=None):
        self.oams = oams
        self.bbox = bbox  # (xmin, ymin, xmax, ymax) or None


class Ncer:
    __slots__ = ("cells", "mapping", "boundary_bytes", "is_1d")

    def __init__(self, cells: List[Cell], mapping: int):
        self.cells = cells
        self.mapping = mapping
        shift = mapping & 0xFF
        self.is_1d = shift != 0
        self.boundary_bytes = (32 << shift) if self.is_1d else 32


def _parse_oam(raw: bytes, off: int) -> Oam:
    a0, a1, a2 = struct.unpack_from("<HHH", raw, off)
    shape = (a0 >> 14) & 0x3
    size = (a1 >> 14) & 0x3
    w, h = SHAPE_SIZE.get((shape, size), (0, 0))
    return Oam(
        x=_s9(a1 & 0x1FF),
        y=_s8(a0 & 0xFF),
        w=w,
        h=h,
        tile=a2 & 0x3FF,
        is8bpp=bool(a0 & 0x2000),
        hflip=bool(a1 & 0x1000),
        vflip=bool(a1 & 0x2000),
        pal=(a2 >> 12) & 0xF,
        prio=(a2 >> 10) & 0x3,
    )


def parse_ncer(raw: bytes) -> Ncer:
    """Parse an NCER (compressed or raw)."""
    raw = maybe_decompress(raw)
    if raw[:4] != b"RECN":
        raise ValueError(f"not NCER: {raw[:4]!r}")
    cebk = find_block(raw, b"KBEC")
    n_cells = struct.unpack_from("<H", raw, cebk + 8)[0]
    bank_attr = struct.unpack_from("<H", raw, cebk + 10)[0]
    cell_data_off = struct.unpack_from("<I", raw, cebk + 12)[0]
    mapping = struct.unpack_from("<I", raw, cebk + 16)[0]
    cell_size = 16 if (bank_attr & 1) else 8
    cells_base = cebk + 8 + cell_data_off
    oam_base = cells_base + n_cells * cell_size

    cells: List[Cell] = []
    for ci in range(n_cells):
        off = cells_base + ci * cell_size
        n_oam = struct.unpack_from("<H", raw, off)[0]
        oam_off = struct.unpack_from("<I", raw, off + 4)[0]
        bbox = None
        if cell_size == 16:
            bbox = struct.unpack_from("<hhhh", raw, off + 8)
        oams = [_parse_oam(raw, oam_base + oam_off + oi * 6) for oi in range(n_oam)]
        cells.append(Cell(oams, bbox))

    return Ncer(cells, mapping)


def shift_cell_oams(raw: bytes, cell_idx: int, dx: int, dy: int) -> bytes:
    """Return a copy of ``raw`` NCER with every OAM in ``cell_idx`` moved by
    ``(dx, dy)`` on screen.

    Byte-patches only each OAM's x (a1 low 9 bits, signed) and y (a0 low 8
    bits, signed) fields — every other attribute, the OAM offsets, and the
    block sizes are untouched, so no structural re-encode is needed (same
    minimal-diff philosophy as ``sprite.build_ncgr_from_template``). When
    the bank uses the 16-byte cell layout, the cell's stored bbox is
    translated too so it stays consistent with the moved OAMs.

    Raises ``ValueError`` if any resulting coordinate leaves the hardware
    field range (x ∈ [-256, 255], y ∈ [-128, 127]); the caller should clamp
    its spinbox ranges so this never fires in normal use.
    """
    raw = bytearray(maybe_decompress(raw))
    if raw[:4] != b"RECN":
        raise ValueError(f"not NCER: {raw[:4]!r}")
    cebk = find_block(raw, b"KBEC")
    n_cells = struct.unpack_from("<H", raw, cebk + 8)[0]
    if not (0 <= cell_idx < n_cells):
        raise IndexError(f"cell {cell_idx} out of range (n_cells={n_cells})")
    if dx == 0 and dy == 0:
        return bytes(raw)
    bank_attr = struct.unpack_from("<H", raw, cebk + 10)[0]
    cell_data_off = struct.unpack_from("<I", raw, cebk + 12)[0]
    cell_size = 16 if (bank_attr & 1) else 8
    cells_base = cebk + 8 + cell_data_off
    oam_base = cells_base + n_cells * cell_size

    off = cells_base + cell_idx * cell_size
    n_oam = struct.unpack_from("<H", raw, off)[0]
    oam_off = struct.unpack_from("<I", raw, off + 4)[0]
    for oi in range(n_oam):
        o = oam_base + oam_off + oi * 6
        a0, a1 = struct.unpack_from("<HH", raw, o)
        y = _s8(a0 & 0xFF) + dy
        x = _s9(a1 & 0x1FF) + dx
        if not (-128 <= y <= 127):
            raise ValueError(f"OAM y {y} out of range [-128, 127]")
        if not (-256 <= x <= 255):
            raise ValueError(f"OAM x {x} out of range [-256, 255]")
        a0 = (a0 & 0xFF00) | (y & 0xFF)
        a1 = (a1 & 0xFE00) | (x & 0x1FF)
        struct.pack_into("<HH", raw, o, a0, a1)

    if cell_size == 16:
        xmin, ymin, xmax, ymax = struct.unpack_from("<hhhh", raw, off + 8)
        struct.pack_into(
            "<hhhh", raw, off + 8,
            xmin + dx, ymin + dy, xmax + dx, ymax + dy,
        )
    return bytes(raw)


def min_tiles_required(ncer: Ncer, bpp4: bool = True) -> int:
    """Smallest NCGR tile count that satisfies every OAM in every cell.

    ``bpp4`` is the CHR's bit depth (the OAM ``is8bpp`` flag is ignored
    — DWDD's vanilla data has OAMs whose flag mismatches the NCGR's
    bit_depth, and the engine reads from the linear NCGR using the CHR's
    actual tile size).

    Worked in bytes so the same expression handles 1D (slot_bytes =
    NCER boundary), 4bpp+2D (slot_bytes=32 == tile_bytes), and the
    8bpp+2D quirk (slot_bytes=32, tile_bytes=64 → slot stride is half
    a tile).
    """
    bytes_per_tile = 32 if bpp4 else 64
    slot_bytes = ncer.boundary_bytes if ncer.is_1d else 32
    best_bytes = 0
    for cell in ncer.cells:
        for oam in cell.oams:
            end = oam.tile * slot_bytes + oam.n_tiles * bytes_per_tile
            if end > best_bytes:
                best_bytes = end
    # Round up — an OAM that ends mid-tile still requires that tile to
    # be present in the CHR.
    return (best_bytes + bytes_per_tile - 1) // bytes_per_tile


def sprite_bbox(ncer: Ncer) -> Tuple[int, int]:
    """Largest ``(width, height)`` over every cell's screen footprint.

    Prefers the NCER-stored per-cell bbox when present (cell_size==16 in
    KBEC); falls back to a hull of the cell's OAM rects when only the
    8-byte cell layout is in use. Returns ``(0, 0)`` for empty cells.

    Used by the sprite browser's heuristic categorisation (PLAN §11 G):
    the on-screen footprint is the most stable shape signal across
    portraits / minis / icons in DWDD's vanilla data.
    """
    max_w = max_h = 0
    for cell in ncer.cells:
        if cell.bbox is not None:
            xmin, ymin, xmax, ymax = cell.bbox
            w = xmax - xmin
            h = ymax - ymin
        else:
            if not cell.oams:
                continue
            xs0 = [oam.x for oam in cell.oams]
            ys0 = [oam.y for oam in cell.oams]
            xs1 = [oam.x + oam.w for oam in cell.oams]
            ys1 = [oam.y + oam.h for oam in cell.oams]
            w = max(xs1) - min(xs0)
            h = max(ys1) - min(ys0)
        if w > max_w:
            max_w = w
        if h > max_h:
            max_h = h
    return max_w, max_h


def cell_tile_ranges(ncer: Ncer, bpp4: bool = True) -> List[List[Tuple[int, int]]]:
    """Per-cell ``[(tile_start, tile_end), ...]`` ranges each OAM
    occupies in the linear NCGR. Useful for highlighting which tiles a
    given cell actually draws from.
    """
    bytes_per_tile = 32 if bpp4 else 64
    slot_bytes = ncer.boundary_bytes if ncer.is_1d else 32
    out: List[List[Tuple[int, int]]] = []
    for cell in ncer.cells:
        ranges = []
        for oam in cell.oams:
            start_b = oam.tile * slot_bytes
            end_b = start_b + oam.n_tiles * bytes_per_tile
            start = start_b // bytes_per_tile
            end = (end_b + bytes_per_tile - 1) // bytes_per_tile
            ranges.append((start, end))
        out.append(ranges)
    return out
