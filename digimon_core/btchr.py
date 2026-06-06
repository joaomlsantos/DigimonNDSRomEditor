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

    Three animation tracks (idle/attack/defend) and a 14-byte fixed prefix
    whose signed fields scale with sprite footprint — exact meaning is
    pivot/bbox, exposed verbatim so the UI can show them.
    """
    footprint_scale: int   # u16 at 0x00 — scales with sprite size
    flag: int              # u16 at 0x02
    pad_04: int            # i16 at 0x04 — usually 0
    y_pivot_a: int         # i16 at 0x06 — always <= 0 in vanilla
    x_pivot: int           # i16 at 0x08
    y_pivot_b: int         # i16 at 0x0A — always <= 0 in vanilla
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


def render_cell_rgba(
    cell: ncer_mod.Cell,
    tile_bytes: bytes,
    palette: List[Tuple[int, int, int]],
    boundary_bytes: int = NCER_BOUNDARY_BYTES_DEFAULT,
    pad: int = 0,
) -> Tuple[bytes, int, int]:
    """Composite one cell into a flat RGBA buffer + ``(width, height)``.

    Tile addressing assumes 8bpp 1D-mapped (DWDD vanilla — all 415
    digimon). The OAM tile field is a slot index; ``slot_bytes /
    bytes_per_tile`` gives the linear NCGR tile index multiplier.

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
    tile_mult = boundary_bytes // BYTES_PER_TILE_8BPP

    for o in cell.oams:
        first_tile = o.tile * tile_mult
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
