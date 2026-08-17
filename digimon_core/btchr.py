"""DWDD battle sprite codec for ``BTCHR.PAK``.

PIL-free, mirrors :mod:`digimon_core.mchr`'s shape. Where MCHR is a custom
4bpp tile container, BTCHR is standard Nintendo NCGR/NCLR/NCER/NANR — but
at **8bpp** (RAHC ``bpp_mode==4``). Decoding glue lives here so the editor
layer doesn't have to juggle the bit-depth swap.

PAK layout: 415 digimon × 5 entries in fixed order::

    [mini_header, NCGR, NCLR, NCER, NANR]

All entries are RLE-30 compressed. The mini-header is a small DWDD-custom
record that encodes the digimon's three animation tracks (idle, attack,
defend) plus a bbox/pivot block — see :func:`parse_mini_header`.

Sidecars in ``DAT/BTCHR/``:

- ``CHRSIZE.BIN`` (1660B = 415 × u32): packed ``(secondary_id_u16,
  tile_count/5_u16)`` per digimon. ``lo`` is the in-game digimon id.
- ``BTCHRSIZE.BIN`` (1660B = 415 × u32): sum of uncompressed sizes of
  entries 1–4 per digimon. Used by the engine for load-time allocation.

Module surface (visualizer scope):

- :class:`BtchrDigimon` — per-digimon parsed sprite kit
- :func:`parse_pak_groups` — split flat pak entries into 5-tuples
- :func:`parse_mini_header` — animation tracks + bbox
- :func:`parse_chrsize` — sidecar parse
- :func:`render_cell_rgba` — composite an NCER cell into a QImage-ready
  RGBA buffer, given an 8bpp NCGR tile array + 256-color palette
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import List, Optional, Tuple

from . import ncer as ncer_mod
from .sprite import decompress_rle30, parse_ncgr, parse_nclr


BYTES_PER_TILE_8BPP = 64
GROUP_SIZE = 5  # entries per digimon in BTCHR.PAK
NCER_BOUNDARY_BYTES_DEFAULT = 128  # 1D mapping shift=2 across all of BTCHR

# Sentinel/placeholder slots at the tail of vanilla BTCHR.PAK — single
# 4-tile NCGR, single cell, all-transparent. Behave differently from
# real digimon: chrsize.hi reports the *reserved* tile budget (20)
# rather than the actual 4 tiles present. UI treats them as empty.
SENTINEL_GROUPS = frozenset({400, 401})


# ---- mini-header ----------------------------------------------------------


@dataclass
class AnimStep:
    """One step in an animation track: show ``cell`` for ``duration`` frames."""
    cell: int
    duration: int


@dataclass
class MiniHeader:
    """Parsed BTCHR entry-0 mini-header.

    Three animation tracks (idle/attack/defend) and a 14-byte fixed prefix.
    Field semantics confirmed empirically by editing Lunamon's values and
    observing gameplay + scan-target UI.
    """
    footprint_scale: int   # u16 at 0x00 — tile-per-frame VRAM budget;
                           # setting above actual tpf spills across cell
                           # boundaries and garbles intermediate frames
    flag: int              # u16 at 0x02
    pad_04: int            # i16 at 0x04 — usually 0
    y_pivot_a: int         # i16 at 0x06 — perceived Y for scan-circle
                           # overlay; does NOT move sprite on battlefield
    x_pivot: int           # i16 at 0x08 — no observable effect in vanilla
    y_pivot_b: int         # i16 at 0x0A — no observable effect in vanilla
    pad_0c: int            # u16 at 0x0C — usually 0
    idle: List[AnimStep]   # track A — starts implicit on cell 0
    attack: List[AnimStep] # track B
    defend: List[AnimStep] # track C
    raw_size: int          # original byte length (36/48/52/60) for round-trip


def parse_mini_header(raw: bytes) -> MiniHeader:
    """Decode a decompressed entry-0 mini-header.

    The format is three variable-length animation tracks separated by
    ``u32 0xFFFFFFFF`` terminators, preceded by a 14-byte fixed prefix.
    Track A always has an *odd* u16 count: first u16 is the implicit
    "cell 0 for N frames", then ``(u16 cell, u16 dur)`` pairs.
    """
    if len(raw) < 14 + 12:  # header + 3 minimum terminators
        raise ValueError(f"mini-header too short: {len(raw)}B")
    fixed = struct.unpack_from("<HHhhhhH", raw, 0)
    terms = [
        i * 4 for i in range(len(raw) // 4)
        if struct.unpack_from("<I", raw, i * 4)[0] == 0xFFFFFFFF
    ]
    if len(terms) != 3:
        raise ValueError(f"expected 3 u32 terminators, got {len(terms)}")

    # Track A: 0x0E .. terms[0]
    sec_a = raw[0x0E:terms[0]]
    if len(sec_a) % 2 != 0:
        raise ValueError("track A not u16-aligned")
    a_u16s = struct.unpack(f"<{len(sec_a) // 2}H", sec_a)
    idle: List[AnimStep] = []
    if a_u16s:
        # First u16 is duration for implicit cell 0.
        idle.append(AnimStep(cell=0, duration=a_u16s[0]))
        # Remaining u16s are (cell, dur) pairs.
        rest = a_u16s[1:]
        if len(rest) % 2 != 0:
            raise ValueError("track A: expected odd total u16 count")
        for i in range(0, len(rest), 2):
            idle.append(AnimStep(cell=rest[i], duration=rest[i + 1]))

    # Tracks B and C: u32 (cell, dur) pairs.
    def _decode_pairs(off_start: int, off_end: int) -> List[AnimStep]:
        chunk = raw[off_start:off_end]
        if len(chunk) % 4 != 0:
            raise ValueError(f"track not u32-aligned: {len(chunk)}B")
        out = []
        for i in range(0, len(chunk), 4):
            cell, dur = struct.unpack_from("<HH", chunk, i)
            out.append(AnimStep(cell=cell, duration=dur))
        return out

    attack = _decode_pairs(terms[0] + 4, terms[1])
    defend = _decode_pairs(terms[1] + 4, terms[2])

    return MiniHeader(
        footprint_scale=fixed[0],
        flag=fixed[1],
        pad_04=fixed[2],
        y_pivot_a=fixed[3],
        x_pivot=fixed[4],
        y_pivot_b=fixed[5],
        pad_0c=fixed[6],
        idle=idle,
        attack=attack,
        defend=defend,
        raw_size=len(raw),
    )


def serialize_mini_header(h: MiniHeader) -> bytes:
    """Round-trip writer for :func:`parse_mini_header`.

    Layout::

        0x00 H footprint_scale
        0x02 H flag
        0x04 h pad_04
        0x06 h y_pivot_a
        0x08 h x_pivot
        0x0A h y_pivot_b
        0x0C H pad_0c
        0x0E ... track A (u16 implicit-cell-0 dur, then (u16 cell, u16 dur)*)
             u32 0xFFFFFFFF terminator
             ...  track B ((u16 cell, u16 dur)*)
             u32 0xFFFFFFFF terminator
             ...  track C
             u32 0xFFFFFFFF terminator

    Track A always has its implicit-cell-0 first duration; if the input
    ``idle`` list is empty we still emit a zero duration to keep the
    structure valid (engine reads at least one u16 before scanning for
    the terminator).
    """
    out = bytearray()
    out += struct.pack(
        "<HHhhhhH",
        h.footprint_scale & 0xFFFF, h.flag & 0xFFFF,
        h.pad_04, h.y_pivot_a, h.x_pivot, h.y_pivot_b,
        h.pad_0c & 0xFFFF,
    )
    # Track A: first u16 is the implicit cell-0 duration; remaining
    # steps are explicit (cell, dur) pairs.
    if h.idle:
        out += struct.pack("<H", h.idle[0].duration & 0xFFFF)
        for step in h.idle[1:]:
            out += struct.pack(
                "<HH", step.cell & 0xFFFF, step.duration & 0xFFFF,
            )
    else:
        out += struct.pack("<H", 0)
    out += struct.pack("<I", 0xFFFFFFFF)
    for step in h.attack:
        out += struct.pack(
            "<HH", step.cell & 0xFFFF, step.duration & 0xFFFF,
        )
    out += struct.pack("<I", 0xFFFFFFFF)
    for step in h.defend:
        out += struct.pack(
            "<HH", step.cell & 0xFFFF, step.duration & 0xFFFF,
        )
    out += struct.pack("<I", 0xFFFFFFFF)
    return bytes(out)


# ---- per-digimon sprite kit -----------------------------------------------


@dataclass
class BtchrDigimon:
    """Decoded sprite kit for one digimon (one PAK group)."""
    group_idx: int                 # 0..414
    digimon_id: int                # chrsize.lo for this group
    header: MiniHeader
    tile_bytes: bytes              # raw 8bpp NCGR tile stream
    palette: List[Tuple[int, int, int]]  # 256-color RGB triples
    ncer: ncer_mod.Ncer

    @property
    def n_tiles(self) -> int:
        return len(self.tile_bytes) // BYTES_PER_TILE_8BPP


def parse_pak_groups(pak_file) -> int:
    """Return the number of 5-tuple digimon groups in a BTCHR.PAK.

    ``pak_file`` is a :class:`digimon_core.pak.PakFile`. Just validates
    that count is a multiple of 5 and returns ``count // 5``.
    """
    if pak_file.count % GROUP_SIZE != 0:
        raise ValueError(
            f"BTCHR.PAK count {pak_file.count} is not a multiple of {GROUP_SIZE}"
        )
    return pak_file.count // GROUP_SIZE


def decode_digimon(pak_file, group_idx: int, digimon_id: int = -1) -> BtchrDigimon:
    """Decompress + parse the 5 PAK entries for one digimon.

    ``digimon_id`` is the chrsize.lo value; pass ``-1`` if the caller hasn't
    loaded the sidecar yet (the field is informational, not load-bearing).
    """
    base = group_idx * GROUP_SIZE
    header_raw = decompress_rle30(pak_file.entries[base + 0])
    ncgr_raw = decompress_rle30(pak_file.entries[base + 1])
    nclr_raw = decompress_rle30(pak_file.entries[base + 2])
    ncer_raw = decompress_rle30(pak_file.entries[base + 3])
    # NANR (entry 4) untouched in v1 — animation timing lives in the
    # mini-header, not in NANR, for BTCHR.

    header = parse_mini_header(header_raw)
    tile_bytes, *_ = parse_ncgr(ncgr_raw)
    palettes, _ = parse_nclr(nclr_raw)
    palette = palettes[0]  # NCLR is one 256-color bank when bit_depth==4
    ncer = ncer_mod.parse_ncer(ncer_raw)

    return BtchrDigimon(
        group_idx=group_idx,
        digimon_id=digimon_id,
        header=header,
        tile_bytes=tile_bytes,
        palette=palette,
        ncer=ncer,
    )


# ---- sidecar --------------------------------------------------------------


def derived_footprint_scale(n_tiles: int, n_cells: int) -> int:
    """Compute the canonical mini-header footprint_scale from tile geometry.

    Verified across all 415 vanilla BTCHR groups: ``footprint_scale`` in
    entry-0's mini-header exactly matches ``chrsize.tpf``, and equals
    ``n_tiles // n_cells`` — the per-cell tile budget the engine uses to
    slice the NCGR into cell-sized chunks (cell *i* reads
    ``[i*fs : (i+1)*fs]``). The two single-cell sentinel groups fall out
    naturally: ``n_tiles // 1 == n_tiles``.

    Any code path that rebuilds the tile bank should recompute fs from
    here and rewrite mini-header + chrsize + btchrsize together — the
    three sources must stay consistent or the engine indexes garbled
    tiles for cells > 0.
    """
    if n_cells > 0:
        return n_tiles // n_cells
    return n_tiles


def parse_chrsize(raw: bytes) -> List[Tuple[int, int]]:
    """Return ``[(digimon_id, tile_count_div5), ...]`` per group.

    chrsize.bin is 1660 bytes for vanilla DWDD = 415 × u32, where each
    u32 packs ``(digimon_id u16, tile_count/5 u16)``.
    """
    if len(raw) % 4 != 0:
        raise ValueError(f"chrsize.bin length not u32-aligned: {len(raw)}")
    n = len(raw) // 4
    vals = struct.unpack(f"<{n}I", raw)
    return [(v & 0xFFFF, (v >> 16) & 0xFFFF) for v in vals]


# ---- cell rendering -------------------------------------------------------


def cell_bbox(cell: ncer_mod.Cell) -> Tuple[int, int, int, int]:
    """Return ``(xmin, ymin, xmax, ymax)`` for a cell's OAM union.

    Returns ``(0, 0, 8, 8)`` for empty cells so callers always get a
    valid render target.
    """
    if not cell.oams:
        return (0, 0, 8, 8)
    xs = [o.x for o in cell.oams]
    ys = [o.y for o in cell.oams]
    xes = [o.x + o.w for o in cell.oams]
    yes = [o.y + o.h for o in cell.oams]
    return (min(xs), min(ys), max(xes), max(yes))


def oam_coverage_mask(
    cell: ncer_mod.Cell, w: int, h: int, x_origin: int, y_origin: int,
) -> bytes:
    """Return a ``w*h`` byte array marking which pixels the cell's OAMs
    cover (``1`` = at least one OAM covers this pixel, ``0`` = gap).

    Origin ``(x_origin, y_origin)`` is the OAM-coord location of pixel
    ``(0, 0)`` in the output — i.e. the bbox min for a straight
    ``render_cell_rgba`` call, or ``(xmin - slot_x, ymin - slot_y)`` when
    the caller is aligning with a PNG slot.

    Motivated by BTCHR sprites like group 161 (Baihumon, 0x00a1) whose
    OAM union leaves an interior gap on the left flank — any content
    painted into that gap by a PNG import has no OAM referencing it and
    never renders in-game.
    """
    if w <= 0 or h <= 0:
        return b""
    mask = bytearray(w * h)
    for o in cell.oams:
        x0 = o.x - x_origin
        y0 = o.y - y_origin
        for py in range(o.h):
            dy = y0 + py
            if not (0 <= dy < h):
                continue
            row = dy * w
            xs = max(0, x0)
            xe = min(w, x0 + o.w)
            if xe > xs:
                for dx in range(xs, xe):
                    mask[row + dx] = 1
    return bytes(mask)


def referenced_tile_indices(
    cells: List[ncer_mod.Cell], boundary_bytes: int, n_tiles: int,
) -> set:
    """Set of linear NCGR tile indices any OAM (across every cell) references.

    Same slot→linear mapping as :func:`render_cell_rgba`
    (``first = slot*boundary // bytes_per_tile``, then ``w/8 * h/8`` tiles).
    Flips don't change which tiles are read, only their orientation."""
    refs = set()
    for cell in cells:
        for o in cell.oams:
            first = (o.tile * boundary_bytes) // BYTES_PER_TILE_8BPP
            for i in range((o.w // 8) * (o.h // 8)):
                idx = first + i
                if 0 <= idx < n_tiles:
                    refs.add(idx)
    return refs


def count_orphaned_opaque_pixels(
    tile_bytes: bytes, cells: List[ncer_mod.Cell], boundary_bytes: int,
) -> int:
    """Non-transparent pixels living in NCGR tiles no OAM references.

    Such tiles never render — they're art stranded in the bank when an
    OAM layout stops pointing at them (typically left over from OAM
    re-cover experiments). A clean re-cover (Compress OAM) drops them, so
    a non-zero count here is exactly the "content not covered by the OAM"
    condition the browser flags. Zero for all but one vanilla sprite (the
    slack tiles inside each cell's fs budget are zero-filled)."""
    n_tiles = len(tile_bytes) // BYTES_PER_TILE_8BPP
    refs = referenced_tile_indices(cells, boundary_bytes, n_tiles)
    total = 0
    for idx in range(n_tiles):
        if idx in refs:
            continue
        off = idx * BYTES_PER_TILE_8BPP
        total += sum(1 for b in tile_bytes[off:off + BYTES_PER_TILE_8BPP] if b)
    return total


def render_cell_rgba(
    cell: ncer_mod.Cell,
    tile_bytes: bytes,
    palette: List[Tuple[int, int, int]],
    boundary_bytes: int = NCER_BOUNDARY_BYTES_DEFAULT,
    pad: int = 0,
) -> Tuple[bytes, int, int]:
    """Composite one cell into a flat RGBA buffer + ``(width, height)``.

    Tile addressing is 8bpp (all 415 vanilla BTCHR digimon). OAM ``tile``
    is a slot index whose unit is ``boundary_bytes`` — the linear NCGR
    tile index is ``(slot * boundary_bytes) // bytes_per_tile``. Handles
    both 1D-mapped BTCHR (boundary=128 → 2 tiles per slot) and
    2D-mapped BTCHR (boundary=32 → half a tile per slot) with the same
    formula.

    Index 0 is transparent (alpha=0). ``pad`` adds transparent border on
    every side — convenient for previews so OAM-edge artifacts don't
    clip against the widget border.
    """
    xmin, ymin, xmax, ymax = cell_bbox(cell)
    w = (xmax - xmin) + pad * 2
    h = (ymax - ymin) + pad * 2
    if w <= 0 or h <= 0:
        return b"\x00" * (8 * 8 * 4), 8, 8
    buf = bytearray(w * h * 4)  # zero-init → fully transparent

    n_tiles = len(tile_bytes) // BYTES_PER_TILE_8BPP

    for o in ncer_mod.oams_back_to_front(cell):
        first_tile = (o.tile * boundary_bytes) // BYTES_PER_TILE_8BPP
        ox = o.x - xmin + pad
        oy = o.y - ymin + pad
        ntw = o.w // 8
        nth = o.h // 8
        for ty in range(nth):
            for tx in range(ntw):
                idx = first_tile + ty * ntw + tx
                if idx >= n_tiles:
                    continue
                tile_off = idx * BYTES_PER_TILE_8BPP
                for r in range(8):
                    sr = (7 - r) if o.vflip else r
                    src_row = tile_off + sr * 8
                    dst_y = oy + ty * 8 + r
                    if not (0 <= dst_y < h):
                        continue
                    dst_row_off = dst_y * w * 4
                    for c in range(8):
                        sc = (7 - c) if o.hflip else c
                        pi = tile_bytes[src_row + sc]
                        if pi == 0:
                            continue
                        dst_x = ox + tx * 8 + c
                        if not (0 <= dst_x < w):
                            continue
                        rr, gg, bb = palette[pi]
                        po = dst_row_off + dst_x * 4
                        buf[po] = rr
                        buf[po + 1] = gg
                        buf[po + 2] = bb
                        buf[po + 3] = 255
    return bytes(buf), w, h


def cells_union_canvas(cells: List["ncer_mod.Cell"]) -> Tuple[int, int, int, int]:
    """Return ``(x_origin, y_origin, width, height)`` of an 8-aligned canvas
    covering the OAM union of every cell, so all cells can be rendered onto
    one aligned grid (required by the shared-layout re-cover). Returns
    ``(0, 0, 8, 8)`` if no cell has OAMs."""
    boxes = [cell_bbox(c) for c in cells if c.oams]
    if not boxes:
        return (0, 0, 8, 8)
    xo = (min(b[0] for b in boxes) // 8) * 8
    yo = (min(b[1] for b in boxes) // 8) * 8
    w = ((max(b[2] for b in boxes) - xo + 7) // 8) * 8
    h = ((max(b[3] for b in boxes) - yo + 7) // 8) * 8
    return (xo, yo, max(8, w), max(8, h))


def render_cell_indexed(
    cell: ncer_mod.Cell,
    tile_bytes: bytes,
    w: int,
    h: int,
    x_origin: int,
    y_origin: int,
    boundary_bytes: int = NCER_BOUNDARY_BYTES_DEFAULT,
) -> bytes:
    """Indexed twin of :func:`render_cell_rgba`: composite one cell into a
    ``w*h`` one-byte-per-pixel buffer where each byte is the palette index
    (0 = transparent, never written). Output pixel ``(0,0)`` is OAM coord
    ``(x_origin, y_origin)`` — pass a **shared** canvas (see
    :func:`cells_union_canvas`) across all cells so their pixels align.

    Feeds a sprite's own pixels back through the occupied-only re-cover
    (:func:`ncer.generate_masked_multicell`) so its OAM/tile footprint
    shrinks to just the character — no art change.
    """
    buf = bytearray(w * h)
    if w <= 0 or h <= 0:
        return bytes(buf)
    n_tiles = len(tile_bytes) // BYTES_PER_TILE_8BPP
    for o in ncer_mod.oams_back_to_front(cell):
        first_tile = (o.tile * boundary_bytes) // BYTES_PER_TILE_8BPP
        ox = o.x - x_origin
        oy = o.y - y_origin
        ntw = o.w // 8
        nth = o.h // 8
        for ty in range(nth):
            for tx in range(ntw):
                idx = first_tile + ty * ntw + tx
                if idx >= n_tiles:
                    continue
                toff = idx * BYTES_PER_TILE_8BPP
                for r in range(8):
                    sr = (7 - r) if o.vflip else r
                    srow = toff + sr * 8
                    dy = oy + ty * 8 + r
                    if not (0 <= dy < h):
                        continue
                    drow = dy * w
                    for c in range(8):
                        sc = (7 - c) if o.hflip else c
                        pi = tile_bytes[srow + sc]
                        if pi == 0:
                            continue
                        dx = ox + tx * 8 + c
                        if 0 <= dx < w:
                            buf[drow + dx] = pi
    return bytes(buf)


@dataclass(frozen=True)
class OamBox:
    """One OBJ in a cell's OAM cover: its position/size plus footprint cost and
    how much real (opaque) art it holds — the inputs for an OAM-map view."""
    x: int
    y: int
    w: int
    h: int
    tile_col: int          # grid column of the OBJ's top-left on the shared canvas
    tile_row: int
    slots: int             # tile slots it consumes (ceil(area_tiles / slot_tiles))
    opaque_tiles: int      # tiles inside it that some frame actually draws
    fill: float            # opaque_tiles / area_tiles (1.0 = solid, 0 = empty)


@dataclass(frozen=True)
class OamCoverAnalysis:
    """Footprint breakdown of a sprite's editable OAM cover, for the OAM map."""
    origin: Tuple[int, int]     # (x, y) of the shared canvas top-left in OAM coords
    size: Tuple[int, int]       # (w, h) px of the shared canvas
    boxes: List["OamBox"]       # a shared, tile-aligned cover of the union
    fs: int                     # tiles/cell of THIS cover (what Apply produces)
    total_slots: int
    n_oams: int
    slot_tiles: int             # tiles per slot (boundary_bytes / 64)
    stored_fs: int              # the sprite's current on-disk tiles/cell


# Cover thresholds tried by the OAM map, keeping the smallest footprint (mirrors
# btchrspr._COVER_THRESHOLDS; kept local to avoid a btchr→btchrspr import cycle).
_MAP_COVER_THRESHOLDS = (0.5, 0.6, 0.7, 0.75, 0.8)


def _stored_cover_editable(cells, xo, yo, gc, gr, union, fs_slots) -> bool:
    """True when the sprite's stored OAMs are already a directly-editable cover:
    every cell shares cell 0's structure (shapes + tile-order), cell 0 sits on
    the 8px grid, and it covers the union. Then the map shows the real layout and
    re-opening the editor preserves it; otherwise (per-frame positions / off-grid
    vanilla art) the map synthesises a fresh shared cover instead."""
    c0 = cells[0].oams
    if not c0:
        return False
    if any((o.x - xo) % 8 or (o.y - yo) % 8 for o in c0):
        return False
    ref = [(o.w, o.h, o.tile) for o in c0]
    for i, c in enumerate(cells):
        if [(o.w, o.h, o.tile - i * fs_slots) for o in c.oams] != ref:
            return False
    covered = set()
    for o in c0:
        tc, tr = (o.x - xo) // 8, (o.y - yo) // 8
        for j in range(o.h // 8):
            for k in range(o.w // 8):
                covered.add((tc + k, tr + j))
    return all(
        (tx, ty) in covered
        for ty in range(gr) for tx in range(gc) if union[ty][tx]
    )


def analyze_oam_cover(ncer, tile_bytes: bytes) -> "OamCoverAnalysis":
    """Build a **shared, tile-aligned cover of the union of every frame** and
    break it into per-OBJ footprint + art-fill, for the OAM map / manual editor.

    Crucially this is NOT cell 0's raw OAMs: vanilla / freshly-imported sprites
    use per-frame OAM positions and off-grid placements, so cell 0's layout
    neither covers the union nor sits on the 8px grid — which made the read-only
    map and the tile-based editor disagree. Instead this covers the union the
    same way the compressor / manual rebuild do (:func:`ncer.cover_occupied_tiles`
    over :func:`ncer.union_tile_mask`, smallest fs across boundary × threshold),
    so the map, the editor, and Apply all show the exact same thing. ``fs`` is
    this cover's tiles/cell (== what Apply produces); ``stored_fs`` is the
    sprite's current on-disk footprint (they differ when a per-frame sprite is
    re-laid as one shared layout). See project memory ``project_oam_compression``."""
    cells = ncer.cells
    default_st = max(1, ncer.boundary_bytes // BYTES_PER_TILE_8BPP)
    stored_fs = derived_footprint_scale(
        len(tile_bytes) // BYTES_PER_TILE_8BPP, len(cells)
    )
    if not cells:
        return OamCoverAnalysis((0, 0), (8, 8), [], 0, 0, 0, default_st, stored_fs)
    xo, yo, w, h = cells_union_canvas(cells)
    gc, gr = w // 8, h // 8
    cell_indexed = [
        render_cell_indexed(c, tile_bytes, w, h, xo, yo, ncer.boundary_bytes)
        for c in cells
    ]
    union, *_ = ncer_mod.union_tile_mask(cell_indexed, [(w, h)] * len(cells), 1)

    # Fast path: if the stored OAMs are already a shared, aligned, covering layout
    # (compressed / hand-tuned / previously-applied sprites), show THEM — so the
    # map reflects the real layout and re-opening the editor keeps the user's work.
    fs_slots = stored_fs // default_st if default_st else 0
    if _stored_cover_editable(cells, xo, yo, gc, gr, union, fs_slots):
        boxes: List[OamBox] = []
        total_slots = 0
        for o in cells[0].oams:
            tw, th = o.w // 8, o.h // 8
            area = tw * th
            slots = (area + default_st - 1) // default_st
            total_slots += slots
            tc, tr = (o.x - xo) // 8, (o.y - yo) // 8
            opq = sum(
                1 for j in range(th) for k in range(tw)
                if 0 <= tc + k < gc and 0 <= tr + j < gr and union[tr + j][tc + k]
            )
            boxes.append(OamBox(o.x, o.y, o.w, o.h, tc, tr, slots, opq,
                                opq / area if area else 0.0))
        return OamCoverAnalysis((xo, yo), (w, h), boxes, stored_fs, total_slots,
                                len(boxes), default_st, stored_fs)

    max_anchor_col = max(0, min(gc - 1, (255 - xo) // 8))
    max_anchor_row = max(0, min(gr - 1, (127 - yo) // 8))
    best = None  # (fs, st, rects) — smallest footprint that fits the tile field
    for boundary in (NCER_BOUNDARY_BYTES_DEFAULT, 256):
        st_ = max(1, boundary // BYTES_PER_TILE_8BPP)
        for thr in _MAP_COVER_THRESHOLDS:
            try:
                rects = ncer_mod.cover_occupied_tiles(
                    union, gc, gr, thr,
                    max_anchor_col=max_anchor_col, max_anchor_row=max_anchor_row,
                )
            except ValueError:
                continue  # content past the OAM range at this cover — try another
            fs_slots = ncer_mod.rects_fs_slots(rects, st_)
            if len(cells) * fs_slots - 1 > 0x3FF or len(rects) > 128:
                continue
            fs_ = fs_slots * st_
            if best is None or fs_ < best[0]:
                best = (fs_, st_, rects)
        if best is not None:
            break
    if best is None:
        # Uncoverable within limits (giant boss) — fall back to the sprite's own
        # boundary and coarsest cover so the map still shows something.
        rects = ncer_mod.cover_occupied_tiles(
            union, gc, gr, 0.5,
            max_anchor_col=max_anchor_col, max_anchor_row=max_anchor_row,
        )
        best = (ncer_mod.rects_fs_slots(rects, default_st) * default_st,
                default_st, rects)
    fs, st, rects = best

    boxes: List[OamBox] = []
    total_slots = 0
    for tx, ty, ow, oh in rects:
        area = ow * oh
        slots = (area + st - 1) // st
        total_slots += slots
        opq = sum(
            1 for j in range(oh) for k in range(ow)
            if 0 <= tx + k < gc and 0 <= ty + j < gr and union[ty + j][tx + k]
        )
        boxes.append(OamBox(xo + tx * 8, yo + ty * 8, ow * 8, oh * 8, tx, ty,
                            slots, opq, opq / area if area else 0.0))
    return OamCoverAnalysis((xo, yo), (w, h), boxes, fs, total_slots,
                            len(rects), st, stored_fs)


def flatten_anim_track(track: List[AnimStep]) -> List[Tuple[int, int]]:
    """Expand an animation track into ``[(cell, frame_in_step), ...]`` for
    QTimer-driven playback.

    Returns one entry per *output frame* — the player advances one entry
    per tick, the ``cell`` field tells it which cell to draw. Looping is
    the caller's responsibility (wrap modulo ``len(out)``).
    """
    out: List[Tuple[int, int]] = []
    for step in track:
        for f in range(step.duration):
            out.append((step.cell, f))
    return out
