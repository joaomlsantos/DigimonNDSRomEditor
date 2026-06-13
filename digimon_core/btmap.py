"""DWDD battle-background codec — ``DAT/btmap/<id>{ac,ap,as,bc,bs,a{0..3}{c,n}}``.

PIL-free. Returns flat RGBA bytes + dimensions; the Qt layer wraps the
result in a ``QImage``. Mirrors :mod:`digimon_core.sprite`'s shape.

Each btmap id ships up to 13 FAT entries (every file individually
RLE-30 wrapped, no internal pak directory):

- ``<id>ac``  NCGR  layer A tile graphics (4bpp)
- ``<id>ap``  NCLR  shared palette (also used by layer B)
- ``<id>as``  NSCR  layer A tilemap
- ``<id>bc``  NCGR  layer B tile graphics (often sparse)
- ``<id>bs``  NSCR  layer B tilemap
- ``<id>aNc`` / ``<id>aNn`` for ``N`` in 0..3 — animation overlay frames
  (NaXc decoded as Nitro NCGR; NaXn partially decoded). Deferred per
  PLAN.md §14.4 G.

The NSCR parser applies SCB-aware rearrangement: NDS hardware stores
any BG wider or taller than 32 tiles as multiple 32×32 screen-base
blocks concatenated in memory. Reading the stream as a flat
``(w/8)×(h/8)`` grid interleaves rows from different SCBs and produces
the "intercalated quadrants" artifact we hit in ``btmap_render.py``
(see PLAN.md §14.6). The build path must apply the inverse rearrangement
so a saved tilemap renders correctly in-game.
"""
from __future__ import annotations

import re
import struct
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from .sprite import (
    build_ncgr_from_template,
    decompress_rle30,
    find_block,
    maybe_decompress,
    parse_ncgr,
    parse_nclr,
    unpack_pixels,
)


BTMAP_DIR = "DAT/btmap/"

SCB_TILES = 32  # NDS text-BG screen-base block is always 32x32 tiles

# Animation overlay frame range. Up to 5 frames (0..4) ship per map id;
# not every map uses all of them (e.g. map 0 ships frames 0..3, map 12
# ships frames 0..4). Browser iterates the full range and skips paths
# that don't resolve through the FAT. Authoring deferred per PLAN.md
# §14.4 G — recon notes flag the NaXn cell-placement schema as the
# blocker (research_docs/claude_notes/btmap_map_recon.md open question
# #3).
ANIM_FRAMES = range(5)


# ---- File-name helpers ----------------------------------------------------


@dataclass(frozen=True)
class BtmapFiles:
    """The up-to-15 FAT paths a map id can occupy in ``DAT/btmap/``.

    Three layer-A files (``ac/ap/as``) plus two layer-B (``bc/bs``,
    shares A's palette) plus up to 5 animation frames each with an NCGR
    (``aNc``) and a cell-layout file (``aNn``). Not every map ships every
    frame — callers should ``in file_table`` test before resolving each
    path.
    """
    map_id: str

    @property
    def layer_a_ncgr(self) -> str:
        return f"{BTMAP_DIR}{self.map_id}ac"

    @property
    def layer_a_nclr(self) -> str:
        return f"{BTMAP_DIR}{self.map_id}ap"

    @property
    def layer_a_nscr(self) -> str:
        return f"{BTMAP_DIR}{self.map_id}as"

    @property
    def layer_b_ncgr(self) -> str:
        return f"{BTMAP_DIR}{self.map_id}bc"

    @property
    def layer_b_nscr(self) -> str:
        return f"{BTMAP_DIR}{self.map_id}bs"

    def anim_ncgr(self, frame: int) -> str:
        return f"{BTMAP_DIR}{self.map_id}a{frame}c"

    def anim_cells(self, frame: int) -> str:
        return f"{BTMAP_DIR}{self.map_id}a{frame}n"

    def all_paths(self) -> List[str]:
        out = [
            self.layer_a_ncgr, self.layer_a_nclr, self.layer_a_nscr,
            self.layer_b_ncgr, self.layer_b_nscr,
        ]
        for frame in ANIM_FRAMES:
            out.append(self.anim_ncgr(frame))
            out.append(self.anim_cells(frame))
        return out


_AC_RE = re.compile(rf"^{re.escape(BTMAP_DIR.upper())}(\d+)AC$")


def discover_map_ids(file_table) -> List[str]:
    """Numeric-sorted list of btmap ids present in the ROM.

    Discovered from ``*ac`` files (one per map id). The frame ``*a{N}c``
    entries match the pattern too; we filter them out by requiring the
    stem to be all digits — animation frames have an ``a`` between the
    map id and the ``c`` suffix.
    """
    ids: List[str] = []
    for path in file_table.paths():
        m = _AC_RE.match(path)
        if not m:
            continue
        ids.append(m.group(1))
    return sorted(set(ids), key=int)


# ---- NSCR (BG tilemap) ----------------------------------------------------


def parse_nscr(raw: bytes) -> Tuple[int, int, List[int]]:
    """Return ``(width_px, height_px, entries)`` for an NSCR payload.

    ``raw`` may be RLE-30 compressed; decompression is automatic.

    Output is row-major across the full ``(width_tiles, height_tiles)``
    grid, with NDS hardware's multi-SCB layout already unfolded — see
    :func:`_rearrange_scbs`.

    Entry bits: 0-9 tile index, 10 hflip, 11 vflip, 12-15 palette bank.
    """
    raw = maybe_decompress(raw)
    if raw[:4] != b"RCSN":
        raise ValueError(f"not NSCR: {raw[:4]!r}")
    nrcs = find_block(raw, b"NRCS")
    width_px = struct.unpack_from("<H", raw, nrcs + 8)[0]
    height_px = struct.unpack_from("<H", raw, nrcs + 0x0A)[0]
    data_size = struct.unpack_from("<I", raw, nrcs + 0x10)[0]
    tilemap = raw[nrcs + 0x14:nrcs + 0x14 + data_size]
    stream = [
        tilemap[i * 2] | (tilemap[i * 2 + 1] << 8)
        for i in range(data_size // 2)
    ]
    entries = _rearrange_scbs(
        stream, width_px // 8, height_px // 8, to_row_major=True,
    )
    return width_px, height_px, entries


def build_nscr_from_template(
    entries: List[int], width_px: int, height_px: int, template_raw: bytes,
) -> bytes:
    """Reuse a template NSCR's header (NSCR + NRCS up to the tilemap data),
    swapping only the tilemap bytes and patching block/file sizes.

    ``entries`` is the row-major tilemap (the format :func:`parse_nscr`
    returns); this function applies the inverse SCB rearrangement before
    packing.

    The new width/height may differ from the template's; downstream save
    callers already handle the FAT splice for grown files.
    """
    raw = maybe_decompress(template_raw)
    if raw[:4] != b"RCSN":
        raise ValueError(f"template not NSCR: {raw[:4]!r}")
    nrcs = find_block(raw, b"NRCS")
    width_tiles = width_px // 8
    height_tiles = height_px // 8
    if len(entries) != width_tiles * height_tiles:
        raise ValueError(
            f"entries length {len(entries)} != "
            f"{width_tiles}*{height_tiles} = {width_tiles * height_tiles}"
        )
    stream = _rearrange_scbs(
        entries, width_tiles, height_tiles, to_row_major=False,
    )
    tilemap_bytes = bytearray()
    for entry in stream:
        tilemap_bytes += struct.pack("<H", entry & 0xFFFF)

    # NRCS layout: u16 width_px, u16 height_px, u32 flags, u32 data_size,
    # then the tilemap. We copy the original header bytes (preserving
    # NRCS flags + any padding), only rewriting width/height/data_size.
    nrcs_header_end = nrcs + 0x14
    out = bytearray(raw[:nrcs_header_end])
    struct.pack_into("<H", out, nrcs + 8, width_px)
    struct.pack_into("<H", out, nrcs + 0x0A, height_px)
    struct.pack_into("<I", out, nrcs + 0x10, len(tilemap_bytes))
    out += tilemap_bytes
    struct.pack_into("<I", out, 8, len(out))                # NSCR file size
    struct.pack_into("<I", out, nrcs + 4, len(out) - nrcs)  # NRCS block size
    return bytes(out)


def _rearrange_scbs(
    stream: List[int], width_tiles: int, height_tiles: int, *, to_row_major: bool,
) -> List[int]:
    """Unfold/refold NDS multi-SCB tilemap layout.

    For ``width_tiles <= 32 and height_tiles <= 32`` the layout is a
    single 32×32 SCB and stream order *is* row-major (truncated to the
    actual grid). Larger BGs concatenate multiple 32×32 SCBs:

    - 512×256 (BG size 1): SCB0 = left, SCB1 = right
    - 256×512 (BG size 2): SCB0 = top, SCB1 = bottom
    - 512×512 (BG size 3): SCB0 = TL, SCB1 = TR, SCB2 = BL, SCB3 = BR

    Parse direction (``to_row_major=True``): SCB-concatenated stream →
    row-major (height_tiles × width_tiles) grid.
    Build direction (``to_row_major=False``): row-major grid →
    SCB-concatenated stream.
    """
    if width_tiles <= SCB_TILES and height_tiles <= SCB_TILES:
        n = width_tiles * height_tiles
        if to_row_major:
            return stream[:n] + [0] * max(0, n - len(stream))
        return list(stream) + [0] * max(0, n - len(stream))
    scbs_x = max(1, (width_tiles + SCB_TILES - 1) // SCB_TILES)
    scb_entries = SCB_TILES * SCB_TILES
    total = max(width_tiles * height_tiles, scb_entries)
    out = [0] * total
    for ty in range(height_tiles):
        scb_row = ty // SCB_TILES
        row_in_scb = ty % SCB_TILES
        for tx in range(width_tiles):
            scb_col = tx // SCB_TILES
            col_in_scb = tx % SCB_TILES
            scb_index = scb_row * scbs_x + scb_col
            scb_off = scb_index * scb_entries + row_in_scb * SCB_TILES + col_in_scb
            row_off = ty * width_tiles + tx
            if to_row_major:
                src, dst = scb_off, row_off
            else:
                src, dst = row_off, scb_off
            if src < len(stream):
                out[dst] = stream[src]
    # Trim padding from the row-major output to the actual grid size; the
    # SCB-concatenated stream stays full so the engine's BG fetcher reads
    # zero-padded off-screen tiles instead of garbage.
    if to_row_major:
        return out[:width_tiles * height_tiles]
    return out


# ---- Tile decode (NCGR → 64-byte-per-tile palette-index buffer) -----------


def _ncgr_tiles_as_indices(ncgr_raw: bytes) -> Tuple[List[bytes], int]:
    """Return ``(tiles, bit_depth)`` where each tile is a 64-byte palette
    index buffer (one byte per pixel) regardless of source bpp.

    Centralized so :func:`render_btmap` doesn't have to branch on bpp.
    """
    tile_bytes, bit_depth, _hw, _hh, _is_bitmap = parse_ncgr(ncgr_raw)
    if bit_depth == 3:  # 4bpp
        tile_size_src = 32
    elif bit_depth == 4:  # 8bpp
        tile_size_src = 64
    else:
        raise ValueError(f"unsupported NCGR bit_depth {bit_depth}")
    n_tiles = len(tile_bytes) // tile_size_src
    out: List[bytes] = []
    for ix in range(n_tiles):
        packed = tile_bytes[ix * tile_size_src:(ix + 1) * tile_size_src]
        out.append(bytes(unpack_pixels(packed, bit_depth)))
    return out, bit_depth


# ---- Layer render ---------------------------------------------------------


def _render_layer(
    width_px: int,
    height_px: int,
    entries: List[int],
    tiles: List[bytes],
    palettes: List[List[Tuple[int, int, int]]],
    *,
    backdrop_opaque: bool,
) -> bytearray:
    """RGBA bytes for one BG layer. ``palettes`` is the list returned by
    :func:`digimon_core.sprite.parse_nclr` (RGB triples).

    ``backdrop_opaque=True`` for the bottom layer — palette index 0 is
    painted with full alpha so the map has a solid background. ``False``
    for the top layer — palette index 0 is left transparent so the
    bottom layer shows through.
    """
    out = bytearray(width_px * height_px * 4)
    tw = width_px // 8
    th = height_px // 8
    n_tiles = len(tiles)
    n_banks = len(palettes)
    for ty in range(th):
        for tx in range(tw):
            entry = entries[ty * tw + tx]
            tile_ix = entry & 0x3FF
            hflip = bool(entry & 0x400)
            vflip = bool(entry & 0x800)
            pal_ix = (entry >> 12) & 0xF
            if tile_ix >= n_tiles:
                continue
            tile = tiles[tile_ix]
            palette = palettes[pal_ix] if pal_ix < n_banks else palettes[0]
            for py in range(8):
                row = py if not vflip else (7 - py)
                for pxn in range(8):
                    col = pxn if not hflip else (7 - pxn)
                    idx = tile[row * 8 + col]
                    if idx == 0 and not backdrop_opaque:
                        continue
                    if idx >= len(palette):
                        r, g, b, a = 0, 0, 0, 0
                    else:
                        r, g, b = palette[idx]
                        a = 255  # backdrop already forced opaque below
                        if idx == 0 and not backdrop_opaque:
                            a = 0
                    j = ((ty * 8 + py) * width_px + tx * 8 + pxn) * 4
                    out[j:j + 4] = bytes((r, g, b, a))
    return out


def _alpha_over(bottom: bytearray, top: bytearray) -> bytearray:
    """Simple porter-duff source-over composite. Inputs and output are
    width-px-aligned RGBA buffers of identical length.

    Avoids a PIL dependency; the btmap layers are small enough (<= 512²)
    that a Python loop is fine compared to a Qt/PIL round-trip.
    """
    if len(bottom) != len(top):
        raise ValueError("composite buffers must be the same length")
    out = bytearray(len(bottom))
    for i in range(0, len(bottom), 4):
        a_top = top[i + 3]
        if a_top == 0:
            out[i:i + 4] = bottom[i:i + 4]
            continue
        if a_top == 255:
            out[i:i + 4] = top[i:i + 4]
            continue
        inv = 255 - a_top
        out[i + 0] = (top[i + 0] * a_top + bottom[i + 0] * inv) // 255
        out[i + 1] = (top[i + 1] * a_top + bottom[i + 1] * inv) // 255
        out[i + 2] = (top[i + 2] * a_top + bottom[i + 2] * inv) // 255
        out[i + 3] = a_top + (bottom[i + 3] * inv) // 255
    return out


def _crop_rgba(
    buf: bytearray, src_w: int, src_h: int, dst_w: int, dst_h: int,
) -> bytearray:
    """Crop a width-px-major RGBA buffer to its top-left ``dst_w × dst_h``."""
    if (src_w, src_h) == (dst_w, dst_h):
        return buf
    out = bytearray(dst_w * dst_h * 4)
    row_bytes = dst_w * 4
    for y in range(dst_h):
        src_off = y * src_w * 4
        dst_off = y * row_bytes
        out[dst_off:dst_off + row_bytes] = buf[src_off:src_off + row_bytes]
    return out


# ---- Public API -----------------------------------------------------------


@dataclass(frozen=True)
class BtmapPreview:
    """Result of :func:`render_btmap` — RGBA buffer + dimensions + metadata.

    ``rgba`` is row-major, 8 bits per channel, length ``w * h * 4``.
    ``layer_b_full_size`` is the native ``(w, h)`` of layer B before the
    crop-to-A step; ``None`` if layer B is absent. The UI surfaces this
    so users know when the saved data extends beyond the composited
    preview (the engine uses the off-screen area for scroll/animation —
    see PLAN.md §14.6).
    """
    rgba: bytes
    width: int
    height: int
    layer_b_full_size: Optional[Tuple[int, int]]
    palette_bank_count: int


def render_btmap(
    map_id: str,
    *,
    layer_a_ncgr: bytes,
    layer_a_nclr: bytes,
    layer_a_nscr: bytes,
    layer_b_ncgr: Optional[bytes] = None,
    layer_b_nscr: Optional[bytes] = None,
) -> BtmapPreview:
    """Render the static two-layer composite for a btmap id.

    Inputs are raw FAT bytes (RLE-30 wrapped is fine; parsers handle it).
    Returns RGBA + dimensions sized to layer A's footprint. Layer B,
    when present, is cropped to A's ``(w, h)`` before alpha-blending
    (matches the standalone renderer's behavior).
    """
    palettes, _bd = parse_nclr(layer_a_nclr)
    tiles_a, _ = _ncgr_tiles_as_indices(layer_a_ncgr)
    w, h, entries_a = parse_nscr(layer_a_nscr)
    rgba = _render_layer(w, h, entries_a, tiles_a, palettes, backdrop_opaque=True)

    layer_b_full: Optional[Tuple[int, int]] = None
    if layer_b_ncgr is not None and layer_b_nscr is not None:
        tiles_b, _ = _ncgr_tiles_as_indices(layer_b_ncgr)
        bw, bh, entries_b = parse_nscr(layer_b_nscr)
        layer_b_full = (bw, bh)
        if tiles_b and bw >= w and bh >= h:
            layer_b = _render_layer(
                bw, bh, entries_b, tiles_b, palettes, backdrop_opaque=False,
            )
            layer_b = _crop_rgba(layer_b, bw, bh, w, h)
            rgba = _alpha_over(rgba, layer_b)

    return BtmapPreview(
        rgba=bytes(rgba),
        width=w,
        height=h,
        layer_b_full_size=layer_b_full,
        palette_bank_count=len(palettes),
    )


def render_single_layer(
    layer_ncgr: bytes,
    layer_nscr: bytes,
    palette_nclr: bytes,
    *,
    backdrop_opaque: bool = True,
) -> BtmapPreview:
    """Render one BG layer in isolation, using the supplied palette.

    Used by the browser's "Layer A only" / "Layer B only" view modes —
    seeing each plane on its own is a load-bearing edit precondition
    since A and B are entirely separate NCGR+NSCR pairs that just share
    the NCLR (PLAN.md §14.1, project memory note in the recon doc on BG2
    and BG3 sharing palette banks). ``backdrop_opaque`` defaults to True
    so layer B's content is visible standalone — when composited on top
    of A, the same data is rendered with ``backdrop_opaque=False`` (see
    :func:`render_btmap`).

    The returned ``BtmapPreview`` keeps ``layer_b_full_size=None`` since
    this is a single-layer view, not a composite — UI code should
    surface dimensions from ``width``/``height`` directly.
    """
    palettes, _bd = parse_nclr(palette_nclr)
    tiles, _ = _ncgr_tiles_as_indices(layer_ncgr)
    w, h, entries = parse_nscr(layer_nscr)
    rgba = _render_layer(
        w, h, entries, tiles, palettes, backdrop_opaque=backdrop_opaque,
    )
    return BtmapPreview(
        rgba=bytes(rgba),
        width=w,
        height=h,
        layer_b_full_size=None,
        palette_bank_count=len(palettes),
    )


def render_anim_frame(
    anim_ncgr: bytes, palette_nclr: bytes, *, columns: int = 16,
) -> BtmapPreview:
    """Render one animation-overlay frame's NCGR as a tile sheet.

    The frame's NaXn cell-placement schema is partially decoded — the
    leading u16 pairs are on-screen (x, y) coordinates but the trailing
    per-frame metadata is undecoded (recon doc open question #3). Until
    that lands, we can only show the raw tile graphics; the editor
    surfaces this as "tile sheet preview, exact on-screen layout TBD".

    ``columns`` is the width in tiles; users may want to override this
    if a frame's natural width differs from the default 16.
    """
    palettes, _bd = parse_nclr(palette_nclr)
    tiles, bit_depth = _ncgr_tiles_as_indices(anim_ncgr)
    if not tiles:
        return BtmapPreview(
            rgba=bytes(), width=0, height=0,
            layer_b_full_size=None, palette_bank_count=len(palettes),
        )
    palette = palettes[0] if palettes else [(0, 0, 0)] * 16
    columns = max(1, min(columns, len(tiles)))
    rows = (len(tiles) + columns - 1) // columns
    w = columns * 8
    h = rows * 8
    out = bytearray(w * h * 4)
    for ix, tile in enumerate(tiles):
        tx = (ix % columns) * 8
        ty = (ix // columns) * 8
        for py in range(8):
            for px in range(8):
                idx = tile[py * 8 + px]
                if idx >= len(palette):
                    r, g, b, a = 0, 0, 0, 0
                else:
                    r, g, b = palette[idx]
                    a = 0 if idx == 0 else 255
                j = ((ty + py) * w + tx + px) * 4
                out[j:j + 4] = bytes((r, g, b, a))
    return BtmapPreview(
        rgba=bytes(out),
        width=w,
        height=h,
        layer_b_full_size=None,
        palette_bank_count=len(palettes),
    )


def render_anim_sheet(
    anim_ncgrs: Sequence[bytes],
    palette_nclr: bytes,
    *,
    columns: int = 16,
    gap: int = 8,
) -> BtmapPreview:
    """Stack every animation frame into a single tile sheet.

    Each NaXc renders as its own ``columns``-wide tile grid; sheets are
    stacked vertically with ``gap`` transparent pixels between them so
    frame boundaries stay visible at a glance. Matches the
    btchr-browser idiom (one preview, all data visible) rather than a
    per-frame combo selector.
    """
    palettes, _bd = parse_nclr(palette_nclr)
    palette = palettes[0] if palettes else [(0, 0, 0)] * 16
    frames: list[tuple[int, int, list[list[int]]]] = []
    for ncgr in anim_ncgrs:
        tiles, _ = _ncgr_tiles_as_indices(ncgr)
        if not tiles:
            continue
        cols = max(1, min(columns, len(tiles)))
        rows = (len(tiles) + cols - 1) // cols
        frames.append((cols, rows, tiles))
    if not frames:
        return BtmapPreview(
            rgba=bytes(), width=0, height=0,
            layer_b_full_size=None, palette_bank_count=len(palettes),
        )
    w = max(cols for cols, _, _ in frames) * 8
    h = sum(rows * 8 for _, rows, _ in frames) + gap * (len(frames) - 1)
    out = bytearray(w * h * 4)
    cursor_y = 0
    for cols, rows, tiles in frames:
        for ix, tile in enumerate(tiles):
            tx = (ix % cols) * 8
            ty = cursor_y + (ix // cols) * 8
            for py in range(8):
                for px in range(8):
                    idx = tile[py * 8 + px]
                    if idx >= len(palette):
                        r, g, b, a = 0, 0, 0, 0
                    else:
                        r, g, b = palette[idx]
                        a = 0 if idx == 0 else 255
                    j = ((ty + py) * w + tx + px) * 4
                    out[j:j + 4] = bytes((r, g, b, a))
        cursor_y += rows * 8 + gap
    return BtmapPreview(
        rgba=bytes(out),
        width=w,
        height=h,
        layer_b_full_size=None,
        palette_bank_count=len(palettes),
    )


# ---- Animation overlay (NaXn) --------------------------------------------


@dataclass(frozen=True)
class AnimSubFrame:
    """One sub-frame in an animation cycle.

    Each sub-frame blits NaXc tiles ``[src_lo..src_hi]`` (inclusive)
    into Layer A's tile bank at ``[dst_lo..dst_hi]`` for ``ticks``
    vsync ticks. ``src_hi - src_lo`` always equals ``dst_hi - dst_lo``
    — that span invariant is what lets the cycle re-use one fixed
    destination region across sub-frames.
    """
    ticks: int
    src_lo: int
    src_hi: int


@dataclass(frozen=True)
class AnimFrame:
    """Parsed NaXn outer-frame record.

    ``cells`` is the 5-entry on-screen position list (some entries may
    be ``(0, 0)`` for inactive). ``dst_lo``/``dst_hi`` is the Layer A
    tile range overwritten by this frame's animation. ``sub_frames``
    is the cycle the engine steps through.

    ``schema_ok`` is ``True`` when every sub-frame satisfies the span
    invariant ``src_hi - src_lo == dst_hi - dst_lo``. ~90% of vanilla
    NaXn files satisfy it; the rest cluster into "small overlay"
    (``ticks=128``, tiny NaXc) and a few off-by-one / off-by-eight
    cases that don't yet have a known interpretation. Renderers should
    fall back to the raw tile-sheet view when ``schema_ok=False``.

    The 5 cell positions are likely BG scroll waypoints; they don't
    participate in the tile-blit itself (kept here for round-trip).
    """
    cells: Tuple[Tuple[int, int], ...]
    dst_lo: int
    dst_hi: int
    sub_frames: Tuple[AnimSubFrame, ...]
    schema_ok: bool


def parse_naxn(raw: bytes) -> AnimFrame:
    """Parse an NaXn into :class:`AnimFrame`. Input may be RLE-30 compressed.

    Layout (little-endian u16):

    - 5 ``(x, y)`` cell pairs (10 words)
    - ``(dst_lo, dst_hi)`` Layer A tile range (2 words)
    - N sub-frame triples ``(ticks, src_lo, src_hi)`` (3N words)
    - ``0xFFFF 0xFFFF 0xFFFF`` terminator (3 words)

    Sub-frames are read until the terminator. ``schema_ok`` reports
    whether the span invariant holds; the parser is lenient so callers
    can decide how to handle the irregular minority (small overlays,
    off-by-one cases — see :class:`AnimFrame` docstring).
    """
    import struct
    raw = maybe_decompress(raw)
    n_words = len(raw) // 2
    words = struct.unpack(f"<{n_words}H", raw[:n_words * 2])
    if n_words < 15:
        raise ValueError(f"NaXn too short: {n_words} words")
    cells = tuple((words[i], words[i + 1]) for i in range(0, 10, 2))
    dst_lo, dst_hi = words[10], words[11]
    dst_span = dst_hi - dst_lo
    sub_frames: List[AnimSubFrame] = []
    schema_ok = True
    i = 12
    while i + 2 < n_words:
        if words[i] == 0xFFFF and words[i + 1] == 0xFFFF and words[i + 2] == 0xFFFF:
            break
        ticks, src_lo, src_hi = words[i], words[i + 1], words[i + 2]
        if src_hi - src_lo != dst_span:
            schema_ok = False
        sub_frames.append(AnimSubFrame(ticks, src_lo, src_hi))
        i += 3
    return AnimFrame(
        cells=cells,
        dst_lo=dst_lo,
        dst_hi=dst_hi,
        sub_frames=tuple(sub_frames),
        schema_ok=schema_ok,
    )


def classify_anim_frame_schema(
    frame: AnimFrame,
    *,
    layer_a_ncgr: bytes,
    layer_a_nscr: bytes,
) -> str:
    """Decide how an NaXn frame should be rendered: ``"all"``, ``"dominant_bank"``,
    or ``"none"``.

    Two animation regimes exist in vanilla btmaps:

    - **Schema A (``"all"``)** — small ambient overlays. Cells referencing
      the dst tile range share a single palette bank, so a global splice
      into NCGR slots renders correctly (NDS BG draws each cell with its
      stored bank, which here all agree).

    - **Schema B (``"dominant_bank"``)** — larger overlays where NSCR cells
      in the dst range straddle 2-5 different palette banks. A global
      splice would write the same indexed-color bytes into all those cells,
      but the bank mismatch makes most of them render with wrong colors
      (the user-observed "right shape, wrong colors" glitch). Mitigation:
      only splice cells whose bank equals the dominant bank in the dst
      range; other cells keep their vanilla tile bytes. Cells that are
      "supposed to" animate (authored against the dominant bank) move
      correctly; the rest stay static — strictly better than the glitch.

    - **``"none"``** — splice has no target: either ``dst_hi`` exceeds the
      Layer A NCGR tile count (impossible to write into) or no NSCR cell
      references the dst range (write would be invisible). Caller renders
      the static base; the engine presumably draws this animation via a
      mechanism we haven't decoded yet (likely OBJ overlay at the NaXn
      ``cells`` (x, y) coords).
    """
    tiles_a, _ = _ncgr_tiles_as_indices(layer_a_ncgr)
    n_tiles_a = len(tiles_a)
    if frame.dst_hi >= n_tiles_a:
        return "none"
    _w, _h, entries = parse_nscr(layer_a_nscr)
    bank_counts: Dict[int, int] = {}
    for entry in entries:
        tile_ix = entry & 0x3FF
        if frame.dst_lo <= tile_ix <= frame.dst_hi:
            bank = (entry >> 12) & 0xF
            bank_counts[bank] = bank_counts.get(bank, 0) + 1
    if not bank_counts:
        return "none"
    if len(bank_counts) == 1:
        return "all"
    return "dominant_bank"


def render_anim_state(
    *,
    layer_a_ncgr: bytes,
    layer_a_nclr: bytes,
    layer_a_nscr: bytes,
    anim_ncgr: bytes,
    anim_naxn: bytes,
    sub_ix: int,
    layer_b_ncgr: Optional[bytes] = None,
    layer_b_nscr: Optional[bytes] = None,
    splice_mode: str = "all",
) -> BtmapPreview:
    """Render the full composite with one animation sub-frame applied.

    Splices NaXc tiles ``[sub.src_lo..sub.src_hi]`` over Layer A's
    tile bank at ``[dst_lo..dst_hi]``, then runs the standard two-layer
    composite. Layer B (if present) is composited on top unchanged.

    ``splice_mode`` controls which Layer A cells receive the splice:

    - ``"all"`` — every cell in the dst range gets the new tile bytes
      (textbook NDS BG splice). Correct when the cells share a palette
      bank; otherwise produces the "right shape, wrong colors" glitch in
      off-bank cells.
    - ``"dominant_bank"`` — only cells whose palette bank equals the most
      common bank in the dst range are spliced; the rest keep their
      vanilla tile bytes. Mitigates the glitch on heterogeneous-bank
      frames at the cost of fewer cells actually animating.
    - ``"none"`` — no splice; renders the static base composite.
      Equivalent to :func:`render_btmap`.

    Raises ``IndexError`` if ``sub_ix`` is out of range.
    """
    frame = parse_naxn(anim_naxn)
    if not frame.sub_frames:
        raise ValueError("NaXn has no sub-frames")
    if not frame.schema_ok:
        raise ValueError(
            "NaXn span invariant violated — animation schema unknown for "
            "this frame; caller should fall back to tile-sheet view"
        )
    if not 0 <= sub_ix < len(frame.sub_frames):
        raise IndexError(
            f"sub_ix {sub_ix} out of range 0..{len(frame.sub_frames) - 1}"
        )
    if splice_mode not in {"all", "dominant_bank", "none"}:
        raise ValueError(f"unknown splice_mode {splice_mode!r}")
    sub = frame.sub_frames[sub_ix]

    palettes, _ = parse_nclr(layer_a_nclr)
    tiles_a, _ = _ncgr_tiles_as_indices(layer_a_ncgr)
    tiles_anim, _ = _ncgr_tiles_as_indices(anim_ncgr)
    w, h, entries_a = parse_nscr(layer_a_nscr)

    if splice_mode == "none":
        rgba = _render_layer(w, h, entries_a, tiles_a, palettes, backdrop_opaque=True)
    elif splice_mode == "all":
        span = frame.dst_hi - frame.dst_lo + 1
        spliced = list(tiles_a)
        for i in range(span):
            dst_ix = frame.dst_lo + i
            src_ix = sub.src_lo + i
            if dst_ix < len(spliced) and src_ix < len(tiles_anim):
                spliced[dst_ix] = tiles_anim[src_ix]
        rgba = _render_layer(w, h, entries_a, spliced, palettes, backdrop_opaque=True)
    else:  # "dominant_bank"
        bank_counts: Dict[int, int] = {}
        for entry in entries_a:
            tile_ix = entry & 0x3FF
            if frame.dst_lo <= tile_ix <= frame.dst_hi:
                bank = (entry >> 12) & 0xF
                bank_counts[bank] = bank_counts.get(bank, 0) + 1
        dominant_bank = max(bank_counts, key=bank_counts.get) if bank_counts else None
        # Per-cell tile lookup: cells whose bank matches dominant get spliced
        # tile bytes; everyone else uses the vanilla NCGR tile. Implemented
        # as a custom entries list with a "shadow" tile pool that has both
        # the vanilla and spliced versions; each cell points at whichever
        # version is appropriate for its bank.
        # Simpler implementation: render with a per-cell-aware splice by
        # building a parallel tile list per-cell. We just rewrite cells
        # in-place over a base render.
        # Approach: render the base (no splice), then overdraw the
        # dominant-bank cells in the dst range with the spliced tile.
        rgba = _render_layer(w, h, entries_a, tiles_a, palettes, backdrop_opaque=True)
        tw = w // 8
        th = h // 8
        span = frame.dst_hi - frame.dst_lo + 1
        # Build spliced tile lookup once.
        spliced_tiles: Dict[int, bytes] = {}
        for i in range(span):
            dst_ix = frame.dst_lo + i
            src_ix = sub.src_lo + i
            if src_ix < len(tiles_anim):
                spliced_tiles[dst_ix] = tiles_anim[src_ix]
        n_banks = len(palettes)
        for ty in range(th):
            for tx in range(tw):
                entry = entries_a[ty * tw + tx]
                tile_ix = entry & 0x3FF
                if tile_ix not in spliced_tiles:
                    continue
                bank = (entry >> 12) & 0xF
                if bank != dominant_bank:
                    continue
                hflip = bool(entry & 0x400)
                vflip = bool(entry & 0x800)
                palette = palettes[bank] if bank < n_banks else palettes[0]
                tile = spliced_tiles[tile_ix]
                for py in range(8):
                    row = py if not vflip else (7 - py)
                    for pxn in range(8):
                        col = pxn if not hflip else (7 - pxn)
                        idx = tile[row * 8 + col]
                        if idx >= len(palette):
                            r, g, b, a = 0, 0, 0, 255
                        else:
                            r, g, b = palette[idx]
                            a = 255
                        j = ((ty * 8 + py) * w + tx * 8 + pxn) * 4
                        rgba[j:j + 4] = bytes((r, g, b, a))

    layer_b_full: Optional[Tuple[int, int]] = None
    if layer_b_ncgr is not None and layer_b_nscr is not None:
        tiles_b, _ = _ncgr_tiles_as_indices(layer_b_ncgr)
        bw, bh, entries_b = parse_nscr(layer_b_nscr)
        layer_b_full = (bw, bh)
        if tiles_b and bw >= w and bh >= h:
            layer_b = _render_layer(
                bw, bh, entries_b, tiles_b, palettes, backdrop_opaque=False,
            )
            layer_b = _crop_rgba(layer_b, bw, bh, w, h)
            rgba = _alpha_over(rgba, layer_b)

    return BtmapPreview(
        rgba=bytes(rgba),
        width=w,
        height=h,
        layer_b_full_size=layer_b_full,
        palette_bank_count=len(palettes),
    )


def render_anim_state_routed(
    *,
    layer_a_ncgr: bytes,
    layer_a_nclr: bytes,
    layer_a_nscr: bytes,
    anim_ncgr: bytes,
    anim_naxn: bytes,
    sub_ix: int,
    layer_b_ncgr: Optional[bytes] = None,
    layer_b_nscr: Optional[bytes] = None,
) -> Tuple[BtmapPreview, str]:
    """Auto-classify and render. Returns ``(preview, schema_label)``.

    Picks ``splice_mode`` via :func:`classify_anim_frame_schema` and
    forwards to :func:`render_anim_state`. ``schema_label`` mirrors the
    chosen mode so the UI can surface what the renderer decided.
    """
    frame = parse_naxn(anim_naxn)
    if not frame.schema_ok:
        raise ValueError(
            "NaXn span invariant violated — animation schema unknown for "
            "this frame; caller should fall back to tile-sheet view"
        )
    mode = classify_anim_frame_schema(
        frame, layer_a_ncgr=layer_a_ncgr, layer_a_nscr=layer_a_nscr,
    )
    preview = render_anim_state(
        layer_a_ncgr=layer_a_ncgr,
        layer_a_nclr=layer_a_nclr,
        layer_a_nscr=layer_a_nscr,
        anim_ncgr=anim_ncgr,
        anim_naxn=anim_naxn,
        sub_ix=sub_ix,
        layer_b_ncgr=layer_b_ncgr,
        layer_b_nscr=layer_b_nscr,
        splice_mode=mode,
    )
    return preview, mode


def render_anim_sub_frame_sparse(
    *,
    layer_a_nscr: bytes,
    layer_a_nclr: bytes,
    anim_ncgr: bytes,
    anim_naxn: bytes,
    sub_ix: int,
) -> BtmapPreview:
    """Layer-A-sized RGBA buffer with only the animated tiles drawn.

    Every pixel that doesn't belong to an animated tile cell is fully
    transparent. Cells whose NSCR tile index falls in
    ``[dst_lo, dst_hi]`` are rendered using the NaXc tile at
    ``src_lo + (tile_ix - dst_lo)`` from the requested sub-frame, with
    the cell's NSCR palette/flip flags applied.

    This is the export surface for Option A editing: the user paints
    over the visible tiles, and on import the editor diffs against this
    same shape to recover updated NaXc tile bytes.

    Palette index 0 stays transparent inside the visible cells too so
    "no tile here" and "transparent pixel inside an animated tile" look
    the same in the export — which matches what the engine displays.
    """
    frame = parse_naxn(anim_naxn)
    if not frame.schema_ok:
        raise ValueError(
            "NaXn schema not decoded — sparse export unavailable for this frame"
        )
    if not 0 <= sub_ix < len(frame.sub_frames):
        raise IndexError(
            f"sub_ix {sub_ix} out of range 0..{len(frame.sub_frames) - 1}"
        )
    sub = frame.sub_frames[sub_ix]
    palettes, _ = parse_nclr(layer_a_nclr)
    tiles_anim, _ = _ncgr_tiles_as_indices(anim_ncgr)
    w, h, entries = parse_nscr(layer_a_nscr)

    out = bytearray(w * h * 4)  # zero-filled = fully transparent
    tw = w // 8
    th = h // 8
    n_anim = len(tiles_anim)
    n_banks = len(palettes)
    for ty in range(th):
        for tx in range(tw):
            entry = entries[ty * tw + tx]
            tile_ix = entry & 0x3FF
            if not (frame.dst_lo <= tile_ix <= frame.dst_hi):
                continue
            rel = tile_ix - frame.dst_lo
            src_ix = sub.src_lo + rel
            if src_ix >= n_anim:
                continue
            hflip = bool(entry & 0x400)
            vflip = bool(entry & 0x800)
            pal_ix = (entry >> 12) & 0xF
            palette = palettes[pal_ix] if pal_ix < n_banks else palettes[0]
            tile = tiles_anim[src_ix]
            for py in range(8):
                row = py if not vflip else (7 - py)
                for pxn in range(8):
                    col = pxn if not hflip else (7 - pxn)
                    idx = tile[row * 8 + col]
                    if idx == 0 or idx >= len(palette):
                        # Leave transparent — engine treats palette 0
                        # as see-through on BG layers.
                        continue
                    r, g, b = palette[idx]
                    j = ((ty * 8 + py) * w + tx * 8 + pxn) * 4
                    out[j:j + 4] = bytes((r, g, b, 255))
    return BtmapPreview(
        rgba=bytes(out),
        width=w,
        height=h,
        layer_b_full_size=None,
        palette_bank_count=len(palettes),
    )


def export_anim_frame_pack(
    *,
    out_dir: str,
    map_id: str,
    frame_ix: int,
    layer_a_ncgr: bytes,
    layer_a_nclr: bytes,
    layer_a_nscr: bytes,
    anim_ncgr: bytes,
    anim_naxn: bytes,
    layer_b_ncgr: Optional[bytes] = None,
    layer_b_nscr: Optional[bytes] = None,
) -> List[str]:
    """Write the editable PNG pack for one animation outer frame.

    Produces:

    - ``mapID_reference.png`` — base composite (A + B cropped to A),
      the visual canvas the user aligns sub-frame edits against.
    - ``mapID_fFRAME_sN.png`` — one sparse layer-A-sized PNG per
      sub-frame (only the animated tile cells are opaque).
    - ``mapID_fFRAME.meta.json`` — dst range + sub-frame metadata so
      the import path can stitch the edits back without re-parsing
      the NaXn from scratch.

    Returns the list of written paths in write order. Raises
    :class:`ImportError` if Pillow isn't installed; the browser button
    surfaces that to the user.
    """
    try:
        from PIL import Image
    except ImportError as e:
        raise ImportError("Pillow is required to export btmap PNGs") from e
    import json
    import os

    frame = parse_naxn(anim_naxn)
    if not frame.schema_ok:
        raise ValueError(
            "Cannot export: NaXn schema not decoded for this frame"
        )

    os.makedirs(out_dir, exist_ok=True)
    written: List[str] = []

    ref = render_btmap(
        map_id,
        layer_a_ncgr=layer_a_ncgr,
        layer_a_nclr=layer_a_nclr,
        layer_a_nscr=layer_a_nscr,
        layer_b_ncgr=layer_b_ncgr,
        layer_b_nscr=layer_b_nscr,
    )
    ref_path = os.path.join(out_dir, f"map{map_id}_reference.png")
    Image.frombytes("RGBA", (ref.width, ref.height), ref.rgba).save(ref_path)
    written.append(ref_path)

    for s_ix in range(len(frame.sub_frames)):
        sparse = render_anim_sub_frame_sparse(
            layer_a_nscr=layer_a_nscr,
            layer_a_nclr=layer_a_nclr,
            anim_ncgr=anim_ncgr,
            anim_naxn=anim_naxn,
            sub_ix=s_ix,
        )
        sub_path = os.path.join(
            out_dir, f"map{map_id}_f{frame_ix}_s{s_ix}.png",
        )
        Image.frombytes(
            "RGBA", (sparse.width, sparse.height), sparse.rgba,
        ).save(sub_path)
        written.append(sub_path)

    meta = {
        "map_id": map_id,
        "frame_ix": frame_ix,
        "dst_lo": frame.dst_lo,
        "dst_hi": frame.dst_hi,
        "sub_frames": [
            {"ticks": s.ticks, "src_lo": s.src_lo, "src_hi": s.src_hi}
            for s in frame.sub_frames
        ],
    }
    meta_path = os.path.join(out_dir, f"map{map_id}_f{frame_ix}.meta.json")
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
    written.append(meta_path)
    return written


def _quantize_to_palette_nonzero(
    rgb: Tuple[int, int, int],
    palette: List[Tuple[int, int, int]],
) -> int:
    """Closest-match in the palette, skipping index 0.

    BG palette 0 is the transparent slot — using it for an opaque pixel
    on import would render that pixel invisible. The export reserves
    index 0 for alpha-zero source pixels and we honor the same contract
    here.
    """
    r, g, b = rgb
    best = 1
    best_dist = 1 << 30
    for i in range(1, len(palette)):
        pr, pg, pb = palette[i]
        d = (pr - r) * (pr - r) + (pg - g) * (pg - g) + (pb - b) * (pb - b)
        if d < best_dist:
            best_dist = d
            best = i
            if d == 0:
                return i
    return best


@dataclass(frozen=True)
class AnimImportResult:
    """Result of :func:`import_anim_frame_pack`.

    ``new_ncgr`` is the bytes to replace the existing NaXc FAT file with.
    ``tiles_touched`` is a per-sub-frame count of NaXc tiles that were
    actually re-encoded — useful for diagnostic UI ("Rewrote 17 tiles
    across 4 sub-frames").
    """
    new_ncgr: bytes
    tiles_touched: Tuple[int, ...]
    map_id: str
    frame_ix: int


def import_anim_frame_pack(
    *,
    folder: str,
    layer_a_nscr: bytes,
    layer_a_nclr: bytes,
    anim_ncgr: bytes,
) -> AnimImportResult:
    """Read an edited sparse-PNG pack and produce new NaXc NCGR bytes.

    Expects ``folder`` to contain a ``mapID_fFRAME.meta.json`` written
    by :func:`export_anim_frame_pack` plus the matching sub-frame PNGs.
    Each PNG must be the same dimensions as Layer A; opaque pixels
    inside the animated cells are quantized to the NSCR cell's palette
    bank and packed into NaXc tile bytes.

    Pixel rules per cell:

    - ``alpha == 0`` → palette index 0 (transparent in BG).
    - ``alpha > 0`` → closest non-zero entry in the NSCR cell's bank.

    Ambiguity rules:

    - If two NSCR cells reference the same dst tile (vanilla data is
      1:1) the first cell wins.
    - Pixels outside animated cells are ignored.

    Returns the rebuilt NCGR (header preserved via
    :func:`build_ncgr_from_template`) plus a per-sub-frame tile-touch
    count for the caller's status surface.
    """
    try:
        from PIL import Image
    except ImportError as e:
        raise ImportError("Pillow is required to import btmap PNGs") from e
    import json
    import os

    meta_files = [p for p in os.listdir(folder) if p.endswith(".meta.json")]
    if len(meta_files) != 1:
        raise ValueError(
            f"expected exactly 1 .meta.json in {folder}, found {len(meta_files)}"
        )
    with open(os.path.join(folder, meta_files[0]), encoding="utf-8") as f:
        meta = json.load(f)
    map_id = str(meta["map_id"])
    frame_ix = int(meta["frame_ix"])
    dst_lo = int(meta["dst_lo"])
    dst_hi = int(meta["dst_hi"])
    sub_meta = meta["sub_frames"]

    w, h, entries = parse_nscr(layer_a_nscr)
    palettes, _ = parse_nclr(layer_a_nclr)
    tiles_anim, bpp = _ncgr_tiles_as_indices(anim_ncgr)
    if bpp != 3:
        raise ValueError(
            f"anim NCGR is not 4bpp (bit_depth={bpp}); only 4bpp btmap"
            " animations are supported"
        )
    new_tiles = [bytearray(t) for t in tiles_anim]

    tw = w // 8
    th = h // 8
    # Build the dst-tile → (tx, ty, NSCR entry) map. First cell wins.
    dst_to_cell: Dict[int, Tuple[int, int, int]] = {}
    for ty in range(th):
        for tx in range(tw):
            entry = entries[ty * tw + tx]
            tile_ix = entry & 0x3FF
            if dst_lo <= tile_ix <= dst_hi and tile_ix not in dst_to_cell:
                dst_to_cell[tile_ix] = (tx, ty, entry)

    touched: List[int] = []
    for s_ix, sub in enumerate(sub_meta):
        png_path = os.path.join(folder, f"map{map_id}_f{frame_ix}_s{s_ix}.png")
        if not os.path.exists(png_path):
            touched.append(0)
            continue
        img = Image.open(png_path).convert("RGBA")
        if img.size != (w, h):
            raise ValueError(
                f"sub-frame {s_ix} PNG size {img.size} doesn't match"
                f" Layer A ({w}x{h})"
            )
        px = img.load()
        count = 0
        src_lo = int(sub["src_lo"])
        for dst_tile_ix, (tx, ty, entry) in dst_to_cell.items():
            rel = dst_tile_ix - dst_lo
            src_tile_ix = src_lo + rel
            if src_tile_ix >= len(new_tiles):
                continue
            hflip = bool(entry & 0x400)
            vflip = bool(entry & 0x800)
            pal_ix = (entry >> 12) & 0xF
            palette = palettes[pal_ix] if pal_ix < len(palettes) else palettes[0]
            tile_buf = new_tiles[src_tile_ix]
            # Reverse the flips applied at render time so the tile bank
            # stores the canonical un-flipped pixels.
            for py in range(8):
                row_in_png = ty * 8 + ((7 - py) if vflip else py)
                for col in range(8):
                    col_in_png = tx * 8 + ((7 - col) if hflip else col)
                    r, g, bl, a = px[col_in_png, row_in_png]
                    if a == 0:
                        idx = 0
                    else:
                        idx = _quantize_to_palette_nonzero((r, g, bl), palette)
                    tile_buf[py * 8 + col] = idx
            count += 1
        touched.append(count)

    # Pack tiles back into 4bpp NCGR body.
    packed = bytearray()
    for t in new_tiles:
        for py in range(8):
            base = py * 8
            for col in range(0, 8, 2):
                lo = t[base + col] & 0x0F
                hi = t[base + col + 1] & 0x0F
                packed.append((hi << 4) | lo)
    new_ncgr = build_ncgr_from_template(bytes(packed), anim_ncgr)
    return AnimImportResult(
        new_ncgr=new_ncgr,
        tiles_touched=tuple(touched),
        map_id=map_id,
        frame_ix=frame_ix,
    )


def render_btmap_from_file_table(
    map_id: str, file_table, rom: bytes,
) -> BtmapPreview:
    """Convenience: resolve every btmap file via ``file_table`` and render.

    Layer A files are required; layer B is optional. Raises ``KeyError``
    if layer A is missing — callers should
    ``map_id in discover_map_ids(file_table)`` first.
    """
    paths = BtmapFiles(map_id)

    def _slice(path: str) -> Optional[bytes]:
        if path not in file_table:
            return None
        return file_table.slice(rom, path)

    layer_a_ncgr = file_table.slice(rom, paths.layer_a_ncgr)
    layer_a_nclr = file_table.slice(rom, paths.layer_a_nclr)
    layer_a_nscr = file_table.slice(rom, paths.layer_a_nscr)
    return render_btmap(
        map_id,
        layer_a_ncgr=layer_a_ncgr,
        layer_a_nclr=layer_a_nclr,
        layer_a_nscr=layer_a_nscr,
        layer_b_ncgr=_slice(paths.layer_b_ncgr),
        layer_b_nscr=_slice(paths.layer_b_nscr),
    )


# ---- Component metadata (browser sidebar) ---------------------------------


@dataclass(frozen=True)
class BtmapComponentInfo:
    """One row in the browser's components table.

    Reflects the on-disk state of a single FAT entry: compressed size
    (as it sits in ROM), uncompressed size, and the parsed kind tag.
    ``kind`` is one of ``"NCGR"``, ``"NCLR"``, ``"NSCR"``, or ``"???"``
    when the magic doesn't match a known Nitro container.
    """
    path: str
    compressed_size: int
    uncompressed_size: int
    kind: str
    detail: str  # human-readable: "4bpp · 256 tiles", "6 banks", "512×256", ...


_KIND_BY_MAGIC = {
    b"RGCN": "NCGR",
    b"RLCN": "NCLR",
    b"RCSN": "NSCR",
}


def describe_component(path: str, raw: bytes) -> BtmapComponentInfo:
    """Parse a FAT-loaded btmap file enough to populate one browser row."""
    compressed = len(raw)
    try:
        decompressed = decompress_rle30(raw)
    except ValueError:
        decompressed = raw
    kind = _KIND_BY_MAGIC.get(decompressed[:4], "???")
    detail = ""
    try:
        if kind == "NCGR":
            tiles, bd = _ncgr_tiles_as_indices(decompressed)
            detail = f"{4 if bd == 3 else 8}bpp · {len(tiles)} tiles"
        elif kind == "NCLR":
            palettes, _bd = parse_nclr(decompressed)
            detail = f"{len(palettes)} banks"
        elif kind == "NSCR":
            w, h, _entries = parse_nscr(decompressed)
            detail = f"{w}\u00d7{h}"
    except (ValueError, struct.error) as e:
        detail = f"parse error: {e}"
    return BtmapComponentInfo(
        path=path,
        compressed_size=compressed,
        uncompressed_size=len(decompressed),
        kind=kind,
        detail=detail,
    )
