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
from typing import List, Optional, Tuple

from .sprite import find_block, maybe_decompress


SHAPE_SIZE = {
    (0, 0): ( 8,  8), (0, 1): (16, 16), (0, 2): (32, 32), (0, 3): (64, 64),
    (1, 0): (16,  8), (1, 1): (32,  8), (1, 2): (32, 16), (1, 3): (64, 32),
    (2, 0): ( 8, 16), (2, 1): ( 8, 32), (2, 2): (16, 32), (2, 3): (32, 64),
}


_SIZE_TO_SHAPE = {wh: sh for sh, wh in SHAPE_SIZE.items()}


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


def _encode_oam(o: Oam) -> bytes:
    """Inverse of :func:`_parse_oam` — pack one OAM back to its 6 bytes.

    Only the attributes the parser reads are encoded; rotation/scaling,
    mosaic and OBJ-mode bits are written as 0 (static, normal sprites —
    what every DWDD cell uses). Raises ``KeyError`` for a (w, h) that isn't
    a legal hardware OBJ size."""
    shape, size = _SIZE_TO_SHAPE[(o.w, o.h)]
    a0 = (o.y & 0xFF) | (0x2000 if o.is8bpp else 0) | (shape << 14)
    a1 = (
        (o.x & 0x1FF)
        | (0x1000 if o.hflip else 0)
        | (0x2000 if o.vflip else 0)
        | (size << 14)
    )
    a2 = (o.tile & 0x3FF) | ((o.prio & 0x3) << 10) | ((o.pal & 0xF) << 12)
    return struct.pack("<HHH", a0, a1, a2)


def oams_bbox(oams: List[Oam]) -> Tuple[int, int, int, int]:
    """``(xmin, ymin, xmax, ymax)`` OAM-screen bounds of a list, or all-zero
    when empty."""
    if not oams:
        return (0, 0, 0, 0)
    xs = [o.x for o in oams]
    ys = [o.y for o in oams]
    return (
        min(xs), min(ys),
        max(o.x + o.w for o in oams), max(o.y + o.h for o in oams),
    )


def oams_back_to_front(cell: "Cell") -> List[Oam]:
    """Cell OAMs in back-to-front paint order for a painter's-algorithm composite.

    NDS OBJ priority orders overlapping sprites two ways: the 2-bit ``prio``
    field is the coarse key (0 = front, 3 = back), and *within* one priority
    the **lower OAM index draws in front**. Painting backmost-first — highest
    ``prio`` first, and OAM 0 last of its priority — reproduces the hardware.

    Straight list order (``cell.oams``) is the exact opposite and mis-composites
    any cell whose front content precedes its background: e.g. SPR 0x0256's
    button labels are OAMs 0-1 but the body-fill OAMs 2-5 follow them, so a
    forward paint buries the text under the fill.
    """
    return [
        cell.oams[i]
        for i in sorted(
            range(len(cell.oams)),
            key=lambda i: (cell.oams[i].prio, i),
            reverse=True,
        )
    ]


def set_cell_oams(raw: bytes, cell_idx: int, new_oams: List[Oam]) -> bytes:
    """Return a copy of ``raw`` NCER with ``cell_idx``'s OAM list replaced by
    ``new_oams`` (an arbitrary, freshly-built layout).

    Unlike :func:`shift_cell_oams` (which byte-patches in place) this rebuilds
    the whole OAM-data region: replacing one cell's OAM *count* shifts every
    later cell's offset, so all cells are re-emitted contiguously and their
    ``oam_off`` recomputed. Untouched cells keep their exact original OAM
    bytes; only ``cell_idx`` is re-encoded from ``new_oams``. The 16-byte
    cell layout's stored bbox is recomputed for the edited cell.

    This is the primitive behind custom-OAM authoring: a caller that has laid
    out a fresh sprite (e.g. an imported PNG) hands the OAM rectangles that
    cover it here. Header, mapping and trailing LABL/UEXT blocks are
    preserved; ``n_cells`` is unchanged.
    """
    raw = maybe_decompress(raw)
    if raw[:4] != b"RECN":
        raise ValueError(f"not NCER: {raw[:4]!r}")
    cebk = find_block(raw, b"KBEC")
    block_size = struct.unpack_from("<I", raw, cebk + 4)[0]
    n_cells = struct.unpack_from("<H", raw, cebk + 8)[0]
    if not (0 <= cell_idx < n_cells):
        raise IndexError(f"cell {cell_idx} out of range (n_cells={n_cells})")
    bank_attr = struct.unpack_from("<H", raw, cebk + 10)[0]
    cell_data_off = struct.unpack_from("<I", raw, cebk + 12)[0]
    cell_size = 16 if (bank_attr & 1) else 8
    cells_base = cebk + 8 + cell_data_off
    oam_base = cells_base + n_cells * cell_size

    # Existing OAM bytes per cell (kept verbatim for the untouched ones).
    per_cell: List[bytes] = []
    for ci in range(n_cells):
        off = cells_base + ci * cell_size
        n_oam = struct.unpack_from("<H", raw, off)[0]
        oam_off = struct.unpack_from("<I", raw, off + 4)[0]
        per_cell.append(bytes(raw[oam_base + oam_off:oam_base + oam_off + n_oam * 6]))
    per_cell[cell_idx] = b"".join(_encode_oam(o) for o in new_oams)

    new_cells = bytearray()
    new_oam_data = bytearray()
    for ci in range(n_cells):
        entry = bytearray(raw[cells_base + ci * cell_size:cells_base + (ci + 1) * cell_size])
        struct.pack_into("<H", entry, 0, len(per_cell[ci]) // 6)
        struct.pack_into("<I", entry, 4, len(new_oam_data))
        if cell_size == 16 and ci == cell_idx:
            struct.pack_into("<hhhh", entry, 8, *oams_bbox(new_oams))
        new_cells += entry
        new_oam_data += per_cell[ci]

    header = raw[cebk:cells_base]                  # KBEC header + cell-array offset
    content = bytearray(header + bytes(new_cells) + bytes(new_oam_data))
    while len(content) % 4:                        # NDS blocks are 4-aligned
        content.append(0)
    struct.pack_into("<I", content, 4, len(content))

    trailer = raw[cebk + block_size:]              # LABL / UEXT, position-independent
    out = bytearray(raw[:cebk]) + content + trailer
    struct.pack_into("<I", out, 8, len(out))       # NCER file size
    return bytes(out)


def encode_indexed_tiles(
    indexed: bytes,
    src_width: int,
    plan: List[Tuple[int, int, int, int]],
    total_tiles: int,
    *,
    is8bpp: bool = True,
) -> bytes:
    """Lay out ``indexed`` (one byte per pixel) into NCGR tile bytes per a
    :func:`oam_grid_tile_plan` ``plan``.

    Each plan entry ``(dst_tile, src_col, src_row, _)`` copies the source
    8×8 cell at ``(src_col, src_row)`` into linear NCGR tile ``dst_tile``.
    Slot-padding gaps stay zero (transparent). ``is8bpp`` picks 64-byte
    tiles (1 byte/pixel) or 32-byte 4bpp tiles (two pixels per byte, low
    nibble = even column). This is the pixel half of a fresh-layout import —
    the OAM side is :func:`generate_oam_grid`.
    """
    bpt = 64 if is8bpp else 32
    out = bytearray(total_tiles * bpt)
    for (dst, sc, sr, _) in plan:
        base = dst * bpt
        for r in range(8):
            srow = (sr * 8 + r) * src_width + sc * 8
            if is8bpp:
                trow = base + r * 8
                out[trow:trow + 8] = bytes(indexed[srow:srow + 8])
            else:
                trow = base + r * 4
                for c in range(0, 8, 2):
                    lo = indexed[srow + c] & 0xF
                    hi = indexed[srow + c + 1] & 0xF
                    out[trow + c // 2] = (hi << 4) | lo
    return bytes(out)


# All legal OBJ (w, h) sizes, largest-area first — used to cover a region
# with as few OAMs as possible (squares alone explode the count on
# non-64-aligned edges: a 264×8 strip is 9 wide-OBJs vs 33 8×8 squares).
_OBJ_SIZES_BY_AREA = sorted(SHAPE_SIZE.values(), key=lambda wh: wh[0] * wh[1], reverse=True)


def _largest_obj(w: int, h: int) -> Tuple[int, int]:
    """Largest-area legal OBJ ``(ow, oh)`` fitting within ``(w, h)``."""
    for ow, oh in _OBJ_SIZES_BY_AREA:
        if ow <= w and oh <= h:
            return ow, oh
    return 8, 8


def generate_oam_grid(
    width_px: int,
    height_px: int,
    *,
    origin_x: int = 0,
    origin_y: int = 0,
    is8bpp: bool = True,
    pal: int = 0,
    slot_tiles: int = 1,
    tile_start: int = 0,
) -> List[Oam]:
    """Tile a ``width_px × height_px`` region with hardware-legal OAMs.

    Covers the region exactly (no gaps, no overlap) using square OBJs only —
    squares (8/16/32/64) are the subset of legal OBJ sizes that compose any
    8-aligned rectangle, so no illegal aspect ratio (e.g. 64×8) is ever
    emitted. A recursive split (biggest square in the corner, then the strip
    to its right and the band below) keeps the OAM count low: a 64×64 sprite
    is one OAM, not 64.

    ``slot_tiles`` is the OAM addressing stride in 8×8 tiles
    (``boundary_bytes / bytes_per_tile`` — ``1`` for 4bpp/32-byte-boundary
    sprites, ``2`` for BTCHR's 8bpp/128-byte slots). Each OAM's ``tile`` is
    returned in *slot* units and its tile block is padded up to a slot
    boundary, so a caller laying the NCGR out via :func:`oam_grid_tile_plan`
    gets every OAM referencing a valid, correctly-strided run.

    ``tile_start`` (in slot units) offsets every OAM's ``tile`` — cells of a
    multi-cell sprite share one concatenated NCGR, so cell K starts where
    cell K-1's tiles ended (see :func:`generate_multicell_oam_grid`). This
    matches how vanilla BTCHR's 5 cells index a shared tile bank.

    Raises ``ValueError`` if the dimensions aren't positive multiples of 8.
    """
    if width_px <= 0 or height_px <= 0 or width_px % 8 or height_px % 8:
        raise ValueError(
            f"dimensions must be positive multiples of 8, got {width_px}x{height_px}"
        )
    st = max(1, int(slot_tiles))
    oams: List[Oam] = []
    cursor = 0  # in 8×8 tiles

    def emit(x: int, y: int, w: int, h: int) -> None:
        nonlocal cursor
        if w <= 0 or h <= 0:
            return
        if cursor % st:                       # align this OAM to a slot start
            cursor += st - (cursor % st)
        ow, oh = _largest_obj(w, h)
        oams.append(Oam(
            x=origin_x + x, y=origin_y + y, w=ow, h=oh, tile=tile_start + cursor // st,
            is8bpp=is8bpp, hflip=False, vflip=False, pal=pal, prio=0,
        ))
        cursor += (ow // 8) * (oh // 8)
        emit(x + ow, y, w - ow, oh)   # strip right of the OBJ (height oh)
        emit(x, y + oh, w, h - oh)    # band below (full width)

    emit(0, 0, width_px, height_px)
    return oams


# Legal OBJ sizes in *tiles* (8×8 units), largest-area first — for covering
# an occupancy mask with as few OAMs as possible.
_OBJ_TILE_SIZES_BY_AREA = sorted(
    {(w // 8, h // 8) for (w, h) in SHAPE_SIZE.values()},
    key=lambda t: t[0] * t[1], reverse=True,
)
# Same set, smallest-area first — the mop-up pass wants the *tightest* legal
# OBJ that still reaches an out-of-range tile, to waste as few tiles as it can.
_OBJ_TILE_SIZES_BY_AREA_ASC = list(reversed(_OBJ_TILE_SIZES_BY_AREA))


def occupied_tile_mask(indexed: bytes, width_px: int, height_px: int, min_opaque: int = 1):
    """``(occ, cols, rows)`` where ``occ[ty][tx]`` is True if the 8×8 tile at
    ``(tx, ty)`` holds at least ``min_opaque`` non-transparent pixels (index
    != 0). ``min_opaque=1`` (default) = any pixel = lossless. Higher values drop
    tiles that carry only a few faint edge pixels — a *lossy* trim used to shave
    the last tiles off a sprite that just misses a VRAM cap."""
    cols, rows = width_px // 8, height_px // 8
    occ = [[False] * cols for _ in range(rows)]
    for ty in range(rows):
        for tx in range(cols):
            if min_opaque <= 1:
                hit = False
                for r in range(8):
                    row = (ty * 8 + r) * width_px + tx * 8
                    if any(indexed[row:row + 8]):
                        hit = True
                        break
                occ[ty][tx] = hit
            else:
                cnt = 0
                for r in range(8):
                    row = (ty * 8 + r) * width_px + tx * 8
                    cnt += sum(1 for b in indexed[row:row + 8] if b)
                occ[ty][tx] = cnt >= min_opaque
    return occ, cols, rows


def cover_occupied_tiles(
    occ,
    cols: int,
    rows: int,
    threshold: float = 0.5,
    *,
    max_anchor_col: Optional[int] = None,
    max_anchor_row: Optional[int] = None,
):
    """Cover the occupied tiles with legal OBJ rectangles, ``[(tx, ty, w, h)]``
    in tiles. Greedy top-left scan placing the largest legal OBJ that is fully
    uncovered and at least ``threshold`` occupied — a transparent-tile budget
    that trades a little wasted tile space for far fewer OAMs (an 8×8 always
    qualifies, so every occupied tile is covered).

    ``max_anchor_col`` / ``max_anchor_row`` cap where an OBJ's *top-left* (its
    OAM position) may sit. Content past the cap — a tall sprite laid out
    un-centred, whose lowest opaque tiles would need an OAM-y beyond 127 — is
    still covered, but by an OBJ anchored *within* the cap and grown to reach
    it (how vanilla covers a low body with a tall OBJ, rather than a small OBJ
    at an unencodable position). Raises ``ValueError`` when content sits more
    than one max OBJ (64 px) past the cap, where no in-range anchor reaches."""
    if max_anchor_col is None:
        max_anchor_col = cols - 1
    if max_anchor_row is None:
        max_anchor_row = rows - 1
    covered = [[False] * cols for _ in range(rows)]
    rects = []

    def place(tx, ty, ow, oh):
        rects.append((tx, ty, ow, oh))
        for j in range(oh):
            for i in range(ow):
                covered[ty + j][tx + i] = True

    # Phase 1: greedy cover, but never *anchor* past the caps. An OBJ may still
    # extend past them (only its top-left is the OAM position), which pulls
    # low/right content up into an in-range anchor for free.
    for ty in range(min(rows, max_anchor_row + 1)):
        for tx in range(min(cols, max_anchor_col + 1)):
            if not occ[ty][tx] or covered[ty][tx]:
                continue
            for ow, oh in _OBJ_TILE_SIZES_BY_AREA:
                if tx + ow > cols or ty + oh > rows:
                    continue
                blocked = False
                nocc = 0
                for j in range(oh):
                    for i in range(ow):
                        if covered[ty + j][tx + i]:
                            blocked = True
                            break
                        if occ[ty + j][tx + i]:
                            nocc += 1
                    if blocked:
                        break
                if blocked or nocc < threshold * ow * oh:
                    continue
                place(tx, ty, ow, oh)
                break

    # Phase 2: mop up occupied tiles the caps left uncovered (content past the
    # OAM-position range). Cover each with the smallest legal OBJ anchored
    # within the caps that still reaches it — a few wasted tiles buys a legal
    # OAM instead of an unencodable one.
    for ty in range(rows):
        for tx in range(cols):
            if not occ[ty][tx] or covered[ty][tx]:
                continue
            placed = False
            for ow, oh in _OBJ_TILE_SIZES_BY_AREA_ASC:
                if ow > cols or oh > rows:
                    continue
                atx = min(tx, max_anchor_col, cols - ow)
                aty = min(ty, max_anchor_row, rows - oh)
                if atx < 0 or aty < 0:
                    continue
                # must anchor in range AND still reach the tile
                if atx > max_anchor_col or aty > max_anchor_row:
                    continue
                if not (atx <= tx <= atx + ow - 1 and aty <= ty <= aty + oh - 1):
                    continue
                place(atx, aty, ow, oh)
                placed = True
                break
            if not placed:
                raise ValueError(
                    f"tile ({tx},{ty}) sits past the OAM position range and no "
                    "legal OBJ anchored in range reaches it — the content "
                    "extends too far below its top for an occupied-only re-lay. "
                    "Its original layout is fine, leave it as-is."
                )
    return rects


def union_tile_mask(cell_indexed: List[bytes], dims: List[Tuple[int, int]],
                    min_opaque: int = 1):
    """The union occupancy of every cell's opaque tiles on the shared grid:
    ``(union, gcols, grows, gw, gh)``. A tile is occupied if *any* cell has at
    least ``min_opaque`` non-transparent pixels there — the cells share one OAM
    layout, so the cover works off this union. ``min_opaque>1`` trims faint
    edge tiles (lossy). Pulled out of :func:`generate_masked_multicell` so a
    caller trying several cover thresholds pays this (per-pixel) pass once."""
    gw = max((w for w, _ in dims), default=8)
    gh = max((h for _, h in dims), default=8)
    gcols, grows = gw // 8, gh // 8
    union = [[False] * gcols for _ in range(grows)]
    for indexed, (w, h) in zip(cell_indexed, dims):
        occ, cols, rows = occupied_tile_mask(indexed, w, h, min_opaque)
        for ty in range(min(grows, rows)):
            for tx in range(min(gcols, cols)):
                if occ[ty][tx]:
                    union[ty][tx] = True
    return union, gcols, grows, gw, gh


def masked_multicell_from_union(
    union,
    gcols: int,
    grows: int,
    gw: int,
    gh: int,
    dims: List[Tuple[int, int]],
    *,
    slot_tiles: int = 1,
    is8bpp: bool = True,
    pal: int = 0,
    threshold: float = 0.5,
    origin: Optional[Tuple[int, int]] = None,
):
    """Cover a prebuilt occupancy ``union`` and lay it out across cells.
    Returns the same 5-tuple as :func:`generate_masked_multicell`. Split from
    it so the (cheap) cover + layout can be re-run at several thresholds over
    one (expensive) :func:`union_tile_mask`."""
    st = max(1, int(slot_tiles))
    # Anchor: centre on the grid by default (fresh import — keeps content
    # centred within the signed OAM range). ``origin`` lets a caller pin the
    # OAMs to an existing sprite's own coords instead — needed when
    # re-covering art that the artist laid out un-centred (its content can
    # span >256px because tall OBJs extend below in-range positions).
    ox, oy = origin if origin is not None else (-(gw // 2), -(gh // 2))
    # An OBJ's top-left is its OAM position (x −256..255, y −128..127). Cap
    # where the cover may *anchor* so every emitted OAM is encodable; content
    # past the cap is covered by a taller OBJ anchored within it.
    max_anchor_col = max(0, min(gcols - 1, (255 - ox) // 8))
    max_anchor_row = max(0, min(grows - 1, (127 - oy) // 8))
    rects = cover_occupied_tiles(  # shared layout across cells
        union, gcols, grows, threshold,
        max_anchor_col=max_anchor_col, max_anchor_row=max_anchor_row,
    )
    return layout_from_rects(
        rects, ox, oy, dims, slot_tiles=st, is8bpp=is8bpp, pal=pal,
    )


def rects_fs_slots(rects, slot_tiles: int) -> int:
    """Tile slots a rect list occupies once packed: each OBJ starts on a slot
    boundary (OAM.tile addresses in slot units), so its area rounds up to a
    whole slot. ``fs = rects_fs_slots(...) * slot_tiles``. The cheap live-cost
    computation behind the OAM map / manual editor."""
    st = max(1, int(slot_tiles))
    cur = 0
    for _, _, ow, oh in rects:
        if cur % st:
            cur += st - (cur % st)
        cur += ow * oh
    return (cur + st - 1) // st


def layout_from_rects(
    rects,
    ox: int,
    oy: int,
    dims: List[Tuple[int, int]],
    *,
    slot_tiles: int = 1,
    is8bpp: bool = True,
    pal: int = 0,
):
    """Lay a fixed list of OBJ rectangles ``[(tx, ty, ow, oh), ...]`` (tiles, on
    a shared canvas anchored at OAM coord ``(ox, oy)``) into the same per-cell
    OAMs + tile plan the greedy cover produces — so a hand-drawn cover (manual
    OAM editor) and the automatic one feed the identical assembly. All cells get
    the SAME layout (shared structure); each fills the tiles from its own pixels.
    Returns ``(per_cell_oams, per_cell_plan, total_tiles, fs_tiles, n_oams)``."""
    st = max(1, int(slot_tiles))
    fs_slots = rects_fs_slots(rects, st)

    per_cell_oams: List[List[Oam]] = []
    per_cell_plan: List[List[Tuple[int, int, int, int]]] = []
    for i, (w, h) in enumerate(dims):
        cw, ch = w // 8, h // 8
        oams: List[Oam] = []
        plan: List[Tuple[int, int, int, int]] = []
        cursor = 0
        for tx, ty, ow, oh in rects:
            if cursor % st:
                cursor += st - (cursor % st)
            base_slot = i * fs_slots + cursor // st
            oams.append(Oam(
                x=ox + tx * 8, y=oy + ty * 8, w=ow * 8, h=oh * 8, tile=base_slot,
                is8bpp=is8bpp, hflip=False, vflip=False, pal=pal, prio=0,
            ))
            base_tile = base_slot * st
            for j in range(oh):
                for k in range(ow):
                    sc, sr = tx + k, ty + j
                    if sc < cw and sr < ch:   # in this cell's image
                        plan.append((base_tile + j * ow + k, sc, sr, 0))
            cursor += ow * oh
        per_cell_oams.append(oams)
        per_cell_plan.append(plan)

    fs_tiles = fs_slots * st
    return per_cell_oams, per_cell_plan, fs_tiles * len(dims), fs_tiles, len(rects)


def manual_layout_stats(rects, n_cells: int, ox: int, oy: int) -> dict:
    """Live footprint + validity of a hand-drawn ``rects`` cover, without
    building anything — for the manual OAM editor's readout. Picks the smallest
    boundary stride that fits the 10-bit tile field (like the real build), and
    reports fs, OAM count, and whether every OBJ is inside the signed position
    range. ``fits`` is True when the layout is buildable (tile-field + 128-OAM +
    positions); coverage of the art is checked separately."""
    n_oams = len(rects)
    in_range = all(
        -256 <= ox + tx * 8 <= 255 and -128 <= oy + ty * 8 <= 127
        for tx, ty, _ow, _oh in rects
    )
    chosen = None
    for st in (2, 4):  # boundary 128 then 256
        fs_slots = rects_fs_slots(rects, st)
        if n_cells * fs_slots - 1 <= 0x3FF:  # max OAM.tile index fits 10 bits
            chosen = (fs_slots * st, st)
            break
    if chosen is None:
        fs_slots = rects_fs_slots(rects, 4)
        chosen = (fs_slots * 4, 4)
    fs, st = chosen
    return {
        "fs": fs,
        "slot_tiles": st,
        "n_oams": n_oams,
        "in_range": in_range,
        "fits": (n_cells * rects_fs_slots(rects, st) - 1 <= 0x3FF
                 and n_oams <= 128 and in_range),
    }


def generate_masked_multicell(
    cell_indexed: List[bytes],
    dims: List[Tuple[int, int]],
    *,
    slot_tiles: int = 1,
    is8bpp: bool = True,
    pal: int = 0,
    threshold: float = 0.5,
    origin: Optional[Tuple[int, int]] = None,
):
    """Transparency-aware multi-cell layout: cover only the non-transparent
    tiles (index != 0), sharing one uniformly-chunked NCGR.

    Every cell gets the **same OAM layout** — the cover of the *union* of all
    cells' occupied tiles — like vanilla, where each animation frame has an
    identical OAM structure (same count/positions, only the pixels differ).
    This matters in-game: the engine loads each frame's tiles into a fixed
    per-cell VRAM slot sized from footprint_scale, so a *varying* per-cell
    footprint garbles the later frames. Skipping background tiles keeps the
    cost to the character (not the empty field), and the shared centre anchor
    holds the character's authored position across frames.

    Returns ``(per_cell_oams, per_cell_plan, total_tiles, fs_tiles,
    max_oams)`` — ``per_cell_plan[k]`` maps each destination tile to its
    source ``(col, row)`` in cell k (entries outside a smaller cell's image
    are dropped, leaving those tiles blank).
    """
    union, gcols, grows, gw, gh = union_tile_mask(cell_indexed, dims)
    return masked_multicell_from_union(
        union, gcols, grows, gw, gh, dims,
        slot_tiles=slot_tiles, is8bpp=is8bpp, pal=pal,
        threshold=threshold, origin=origin,
    )


# DEAD END (dormant, not wired to UI — kept only so the tests document the
# finding): the JOINT cover shares one shape-sequence but POSITIONS the blocks
# per cell, so fs approaches the biggest single frame instead of the union. The
# gallery/party render CAN handle per-cell positions (vanilla Matadormon g262
# moves a 64×64 block 136px and renders fine; max per-OAM divergence anywhere in
# the PAK is 344px), so the limit is movement MAGNITUDE. But measured on the
# right metric — per-cell divergence, not per-block movement — gallery-safety
# (≤344px) and sub-512 fs are MUTUALLY EXCLUSIVE for the pose-changing bosses:
# staying ≤344 leaves fs ≥ union, and beating union needs divergence >344 that
# garbles. The fs win IS the divergence the gallery rejects. So the union cover
# (above) + the fit-≤512 edge trim are the only viable paths. See
# research_docs/claude_notes/oam_compression.md.
_JOINT_OAM_BUDGET = 30
_JOINT_ALPHAS = (1.0, 0.7, 0.6, 0.55, 0.5, 0.45, 0.4, 0.35, 0.3, 0.25, 0.2, 0.0)


def _joint_greedy_pass(masks, gcols, grows, st, mac, mar, alpha):
    """One shared-shape cover, positioning each block per cell. Each step adds
    the block whose (total-new-coverage-across-cells) / (slot-cost ** alpha) is
    highest — high alpha packs tight (small blocks), low alpha uses big blocks
    (fewer OAMs). Returns ``[(ow, oh, [(tx, ty) per cell])]`` or ``None`` if the
    content can't be covered in range / within 128 blocks. Integral images make
    each block's best position O(1) per anchor."""
    import numpy as np

    uncov = [m.copy() for m in masks]
    ncells = len(masks)
    boxes = []

    def best_pos(ii, ow, oh):
        max_ty = min(grows - oh, mar)
        max_tx = min(gcols - ow, mac)
        if max_ty < 0 or max_tx < 0:
            return (0, 0, 0)
        sub = (ii[oh:oh + max_ty + 1, ow:ow + max_tx + 1]
               - ii[0:max_ty + 1, ow:ow + max_tx + 1]
               - ii[oh:oh + max_ty + 1, 0:max_tx + 1]
               + ii[0:max_ty + 1, 0:max_tx + 1])
        ty, tx = np.unravel_index(int(np.argmax(sub)), sub.shape)
        return (int(sub[ty, tx]), int(tx), int(ty))

    guard = 0
    while any(u.sum() > 0 for u in uncov):
        guard += 1
        if guard > 4096:
            return None
        integrals = [
            np.pad(np.cumsum(np.cumsum(u, 0), 1), ((1, 0), (1, 0))) for u in uncov
        ]
        pick = None  # (efficiency, (ow, oh), positions)
        for ow, oh in _OBJ_TILE_SIZES_BY_AREA:
            if ow > gcols or oh > grows:
                continue
            positions = []
            total = 0
            for i in range(ncells):
                c, tx, ty = best_pos(integrals[i], ow, oh)
                positions.append((tx, ty))
                total += c
            if total == 0:
                continue
            cost = ((ow * oh + st - 1) // st) * st
            eff = total / (cost ** alpha) if alpha > 0 else total
            if pick is None or eff > pick[0]:
                pick = (eff, (ow, oh), positions)
        if pick is None:
            return None  # remaining content unreachable within the OAM range
        _, (ow, oh), positions = pick
        for i, (tx, ty) in enumerate(positions):
            uncov[i][ty:ty + oh, tx:tx + ow] = 0
        boxes.append((ow, oh, positions))
        if len(boxes) > 128:
            return None
    return boxes


def _joint_greedy_boxes(masks, gcols, grows, st, mac, mar,
                        oam_budget=_JOINT_OAM_BUDGET):
    """Search the block-size bias for the smallest-footprint joint cover whose
    OAM count fits ``oam_budget``. Falls back to the fewest-OAM cover if none
    fits; the caller still validates the 128-OAM hard limit. (Dead end — the
    output is gallery-unsafe for pose-changing sprites; see the note above.)"""
    def fs_slots(boxes):
        cur = 0
        for ow, oh, _ in boxes:
            if cur % st:
                cur += st - (cur % st)
            cur += ow * oh
        return (cur + st - 1) // st

    best = None     # (fs_slots, boxes) with len ≤ budget
    fewest = None   # (n_oams, boxes) overall — fallback
    for alpha in _JOINT_ALPHAS:
        boxes = _joint_greedy_pass(masks, gcols, grows, st, mac, mar, alpha)
        if boxes is None:
            continue
        n = len(boxes)
        if fewest is None or n < fewest[0]:
            fewest = (n, boxes)
        if n <= oam_budget:
            f = fs_slots(boxes)
            if best is None or f < best[0]:
                best = (f, boxes)
    if best is not None:
        return best[1]
    return fewest[1] if fewest is not None else None


def masked_multicell_joint(
    cell_indexed: List[bytes],
    dims: List[Tuple[int, int]],
    *,
    slot_tiles: int = 1,
    is8bpp: bool = True,
    pal: int = 0,
    origin: Optional[Tuple[int, int]] = None,
):
    """Joint shared-shape layout: one shape-sequence, per-cell block positions.
    Same return shape as :func:`masked_multicell_from_union` — ``(per_cell_oams,
    per_cell_plan, total_tiles, fs_tiles, max_oams)`` — so it drops into the same
    assembly. ``fs`` is the biggest single frame's cover, not the union.

    Returns ``None`` when the sprite can't be jointly covered (unreachable
    content or > 128 blocks); the caller falls back / raises."""
    import numpy as np

    st = max(1, int(slot_tiles))
    gw = max((w for w, _ in dims), default=8)
    gh = max((h for _, h in dims), default=8)
    gcols, grows = gw // 8, gh // 8
    masks = []
    for indexed, (w, h) in zip(cell_indexed, dims):
        occ, cols, rows = occupied_tile_mask(indexed, w, h)
        m = np.zeros((grows, gcols), dtype=np.int32)
        for ty in range(min(grows, rows)):
            row = occ[ty]
            for tx in range(min(gcols, cols)):
                if row[tx]:
                    m[ty, tx] = 1
        masks.append(m)

    ox, oy = origin if origin is not None else (-(gw // 2), -(gh // 2))
    max_anchor_col = max(0, min(gcols - 1, (255 - ox) // 8))
    max_anchor_row = max(0, min(grows - 1, (127 - oy) // 8))
    boxes = _joint_greedy_boxes(masks, gcols, grows, st, max_anchor_col, max_anchor_row)
    if boxes is None:
        return None

    offsets = []
    cur = 0
    for ow, oh, _ in boxes:
        if cur % st:
            cur += st - (cur % st)
        offsets.append(cur // st)
        cur += ow * oh
    fs_slots = (cur + st - 1) // st
    fs_tiles = fs_slots * st

    per_cell_oams: List[List[Oam]] = []
    per_cell_plan: List[List[Tuple[int, int, int, int]]] = []
    max_oams = 0
    for i, (w, h) in enumerate(dims):
        cw, ch = w // 8, h // 8
        oams: List[Oam] = []
        plan: List[Tuple[int, int, int, int]] = []
        for k, (ow, oh, positions) in enumerate(boxes):
            tx, ty = positions[i]
            base_slot = i * fs_slots + offsets[k]
            oams.append(Oam(
                x=ox + tx * 8, y=oy + ty * 8, w=ow * 8, h=oh * 8, tile=base_slot,
                is8bpp=is8bpp, hflip=False, vflip=False, pal=pal, prio=0,
            ))
            base_tile = base_slot * st
            for j in range(oh):
                for kk in range(ow):
                    sc, sr = tx + kk, ty + j
                    if sc < cw and sr < ch:
                        plan.append((base_tile + j * ow + kk, sc, sr, 0))
        per_cell_oams.append(oams)
        per_cell_plan.append(plan)
        max_oams = max(max_oams, len(oams))

    return per_cell_oams, per_cell_plan, fs_tiles * len(dims), fs_tiles, max_oams


def generate_multicell_oam_grid(
    dims: List[Tuple[int, int]],
    *,
    is8bpp: bool = True,
    pal: int = 0,
    slot_tiles: int = 1,
) -> Tuple[List[List[Oam]], int, int]:
    """Lay out several cells' OAMs into one shared, **uniformly-chunked** NCGR.

    ``dims`` is ``[(w, h), ...]`` per cell. Verified against vanilla BTCHR:
    the engine slices the NCGR into equal per-cell chunks of ``fs =
    n_tiles // n_cells`` tiles and cell *i*'s OAMs index the absolute range
    ``[i*fs, (i+1)*fs)`` (``btchr.derived_footprint_scale``). So every cell
    gets the *same* budget — ``fs`` is the largest single cell's need
    (slot-rounded) — and shorter cells are padded with blank tiles.

    Returns ``(per_cell_oams, total_tiles, fs_tiles)``: feed each cell's OAMs
    to :func:`set_cell_oams`, lay the NCGR out per each cell's
    :func:`oam_grid_tile_plan` into a ``total_tiles``-tile NCGR, and set the
    mini-header/chrsize footprint_scale to ``fs_tiles``.
    """
    st = max(1, int(slot_tiles))
    # Pass 1: slots each cell needs on its own (tile_start = 0).
    cell_slots: List[int] = []
    for (w, h) in dims:
        probe = generate_oam_grid(w, h, slot_tiles=st)
        _, need = oam_grid_tile_plan(probe, slot_tiles=st)
        cell_slots.append((need + st - 1) // st)
    fs_slots = max(cell_slots) if cell_slots else 0      # uniform per-cell budget
    # Pass 2: place cell i at its fixed chunk i*fs_slots. Centre each cell on
    # the origin so OAM y stays in the signed 8-bit field (−128..127) — a
    # sprite ≥128px tall placed from y=0 would wrap. Matches vanilla, which
    # centres sprites on their battlefield anchor.
    per_cell: List[List[Oam]] = [
        generate_oam_grid(
            w, h, is8bpp=is8bpp, pal=pal, slot_tiles=st, tile_start=i * fs_slots,
            origin_x=-(w // 2), origin_y=-(h // 2),
        )
        for i, (w, h) in enumerate(dims)
    ]
    fs_tiles = fs_slots * st
    return per_cell, fs_tiles * len(dims), fs_tiles


def oam_grid_tile_plan(
    oams: List[Oam], slot_tiles: int = 1,
) -> Tuple[List[Tuple[int, int, int, int]], int]:
    """Map each OAM (from :func:`generate_oam_grid`) to the source 8×8 tiles
    it covers, in NCGR-linear order.

    Returns ``(plan, total_tiles)`` where ``plan`` is one entry per
    destination tile — ``(dst_tile, src_col, src_row, _)`` — telling a tile
    encoder which source cell goes at which linear NCGR position (gaps left
    by slot padding stay unwritten / blank). ``src_col``/``src_row`` are in
    8×8-tile units relative to the sprite's top-left, derived from the OAM
    bounding-box min — so it's correct regardless of where the OAMs are
    positioned on screen (e.g. centred for the signed OAM y range).
    ``slot_tiles`` must match the ``generate_oam_grid`` call.
    """
    st = max(1, int(slot_tiles))
    plan: List[Tuple[int, int, int, int]] = []
    total = 0
    xmin = min((o.x for o in oams), default=0)
    ymin = min((o.y for o in oams), default=0)
    for o in oams:
        base_tile = o.tile * st
        col0 = (o.x - xmin) // 8
        row0 = (o.y - ymin) // 8
        ntw = o.w // 8
        nth = o.h // 8
        for ty in range(nth):
            for tx in range(ntw):
                dst = base_tile + ty * ntw + tx
                plan.append((dst, col0 + tx, row0 + ty, 0))
                total = max(total, dst + 1)
    return plan, total


def set_ncer_boundary(raw: bytes, boundary_bytes: int) -> bytes:
    """Patch the NCER mapping so OAM ``tile`` slots stride ``boundary_bytes``.

    ``boundary = 32 << (mapping & 0xFF)`` — BTCHR ships 128 (2 tiles/slot at
    8bpp); large sprites (>2046 tiles) need 256 (4 tiles/slot) so the 10-bit
    OAM tile field can still reach every tile. Only the low byte of the
    mapping u32 changes."""
    raw = bytearray(maybe_decompress(raw))
    if raw[:4] != b"RECN":
        raise ValueError(f"not NCER: {raw[:4]!r}")
    cebk = find_block(raw, b"KBEC")
    shift = (boundary_bytes // 32).bit_length() - 1  # 128→2, 256→3
    mapping = struct.unpack_from("<I", raw, cebk + 16)[0]
    struct.pack_into("<I", raw, cebk + 16, (mapping & ~0xFF) | (shift & 0xFF))
    return bytes(raw)


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


def append_cloned_cell(raw: bytes, src_cell_idx: int, tile_slot_delta: int) -> bytes:
    """Return a copy of ``raw`` NCER with a new cell appended that clones
    ``src_cell_idx``'s OAM layout, shifting every OAM's ``tile`` slot by
    ``tile_slot_delta``.

    This is the structural half of "add an animation frame": the caller
    grows the NCGR by a duplicated tile block, then adds a cell whose OAMs
    point at that block (``tile_slot_delta`` = where the copy landed, in
    OAM slot units). The new cell is initially a pixel-perfect duplicate of
    the source — editable independently afterwards because it references
    its own tiles.

    Only the cell array + OAM data are rebuilt (n_cells + 1, one new cell
    entry, the source's OAMs re-appended with patched tile fields); the
    NCER header, mapping, and any trailing LABL/UEXT blocks are preserved.
    Raises ``ValueError`` on a non-NCER payload, and ``IndexError`` for an
    out-of-range source cell.
    """
    raw = maybe_decompress(raw)
    if raw[:4] != b"RECN":
        raise ValueError(f"not NCER: {raw[:4]!r}")
    cebk = find_block(raw, b"KBEC")
    block_size = struct.unpack_from("<I", raw, cebk + 4)[0]
    n_cells = struct.unpack_from("<H", raw, cebk + 8)[0]
    bank_attr = struct.unpack_from("<H", raw, cebk + 10)[0]
    cell_data_off = struct.unpack_from("<I", raw, cebk + 12)[0]
    if not (0 <= src_cell_idx < n_cells):
        raise IndexError(f"src cell {src_cell_idx} out of range (n_cells={n_cells})")
    cell_size = 16 if (bank_attr & 1) else 8
    cells_base = cebk + 8 + cell_data_off
    oam_base = cells_base + n_cells * cell_size

    # OAM data used length = furthest (oam_off + n_oam*6) over every cell.
    used_oam_bytes = 0
    for ci in range(n_cells):
        off = cells_base + ci * cell_size
        n_oam = struct.unpack_from("<H", raw, off)[0]
        oam_off = struct.unpack_from("<I", raw, off + 4)[0]
        used_oam_bytes = max(used_oam_bytes, oam_off + n_oam * 6)

    src_off = cells_base + src_cell_idx * cell_size
    src_n_oam = struct.unpack_from("<H", raw, src_off)[0]
    src_oam_off = struct.unpack_from("<I", raw, src_off + 4)[0]

    # New cell entry clones the source (incl. its bbox when cell_size==16)
    # but points its OAMs at the freshly-appended block.
    new_cell = bytearray(raw[src_off:src_off + cell_size])
    struct.pack_into("<I", new_cell, 4, used_oam_bytes)

    # New OAMs clone the source's, offsetting each tile slot.
    new_oams = bytearray(raw[oam_base + src_oam_off:oam_base + src_oam_off + src_n_oam * 6])
    for oi in range(src_n_oam):
        a2 = struct.unpack_from("<H", new_oams, oi * 6 + 4)[0]
        tile = ((a2 & 0x3FF) + tile_slot_delta) & 0x3FF
        struct.pack_into("<H", new_oams, oi * 6 + 4, (a2 & ~0x3FF) | tile)

    header = raw[cebk:cells_base]                 # KBEC header up to cell array
    old_cells = raw[cells_base:oam_base]
    old_oams = raw[oam_base:oam_base + used_oam_bytes]
    content = bytearray(header + old_cells + bytes(new_cell) + old_oams + bytes(new_oams))
    while len(content) % 4:                        # NDS blocks are 4-aligned
        content.append(0)
    struct.pack_into("<H", content, 8, n_cells + 1)
    struct.pack_into("<I", content, 4, len(content))

    trailer = raw[cebk + block_size:]              # LABL / UEXT, position-independent
    out = bytearray(raw[:cebk]) + content + trailer
    struct.pack_into("<I", out, 8, len(out))       # NCER file size
    return bytes(out)
