"""DWDD field-map codec — ``DAT/map/<id>{a,b}.{c,p,s}`` and ``<id>.{d,a,0t}``.

PIL-free. Returns flat RGBA bytes + dimensions; the Qt layer wraps the
result in a ``QImage``. Mirrors :mod:`digimon_core.btmap`'s shape.

Unlike battle backgrounds (Nitro NCGR/NCLR/NSCR), field maps are *raw*
NDS BG payloads with tiny custom headers, each individually RLE-30
wrapped. Each map id ships up to 8 FAT entries:

- ``<id>a.c`` / ``<id>a.p`` / ``<id>a.s`` — layer A tiles / palette / tilemap
- ``<id>b.c`` / ``<id>b.p`` / ``<id>b.s`` — layer B (same shape, often sparse)
- ``<id>.d``  — 138-byte map descriptor (header + 6-u16 tuple table + trailer)
- ``<id>.0t`` — 1-bpp pixel walkability mask
- ``<id>.a``  — per-tile attribute grid (ships all-zero across vanilla;
  treated as opaque bytes here — see ``project_btmap_map_recon`` note)

Tilemap entries use the standard NDS BG layout (bits 0-9 tile, 10 hflip,
11 vflip, 12-15 palette bank); there is no multi-SCB rearrangement
because none of the vanilla field maps exceed 32 tiles in either
dimension along an SCB boundary in a way that matters — the engine
treats these as one-SCB BGs with a custom (w, h) header rather than
relying on the NDS hardware BG-size selector.

For each suffix, ``parse_X`` / ``build_X`` are symmetric: a no-edit
round-trip through ``decompress → parse → build`` reproduces the
decompressed bytes verbatim. The RLE-30 wrapper itself is lossy on
re-compress (the encoder is greedy, not optimal) — round-trip tests
compare *decompressed* bytes, not the on-disk RLE stream.
"""
from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

from .sprite import maybe_decompress


MAP_DIR = "DAT/map/"

# Each .d file is exactly 138 B uncompressed across 265/265 vanilla maps.
DESCRIPTOR_SIZE = 138
DESCRIPTOR_HEADER_SIZE = 42      # 0x00..0x29: shared header / layer block
DESCRIPTOR_TUPLE_REGION_OFF = 42
DESCRIPTOR_TUPLE_REGION_SIZE = 84  # 0x2A..0x7D: 7 × 6 u16 tuples (12 B each)
DESCRIPTOR_TRAILER_OFF = 126     # 0x7E..0x89: 12 B trailer
DESCRIPTOR_TRAILER_SIZE = 12
DESCRIPTOR_MAX_TUPLES = 7
DESCRIPTOR_TUPLE_COLS = 6        # kind, param_a, param_b, flag, reserved, weight

# Convention: a 6-u16 tuple of all 0xFFFF is an empty slot. The vanilla
# .d files pad unused tuple positions with 0xFF bytes, so this round-
# trips byte-identically without needing a separate "tuple count" field.
EMPTY_TUPLE: Tuple[int, int, int, int, int, int] = (0xFFFF,) * 6


# ---- File-name helpers ----------------------------------------------------


@dataclass(frozen=True)
class MapFiles:
    """The up-to-8 FAT paths a map id can occupy in ``DAT/map/``.

    Layer A and B each ship three files (``c/p/s``); the descriptor,
    walkability mask, and attribute grid are per-map (no a/b split).
    Some vanilla maps omit layer B entirely — callers should
    ``in file_table`` test before resolving each path.
    """
    map_id: str

    @property
    def layer_a_tiles(self) -> str:
        return f"{MAP_DIR}{self.map_id}a.c"

    @property
    def layer_a_palette(self) -> str:
        return f"{MAP_DIR}{self.map_id}a.p"

    @property
    def layer_a_screen(self) -> str:
        return f"{MAP_DIR}{self.map_id}a.s"

    @property
    def layer_b_tiles(self) -> str:
        return f"{MAP_DIR}{self.map_id}b.c"

    @property
    def layer_b_palette(self) -> str:
        return f"{MAP_DIR}{self.map_id}b.p"

    @property
    def layer_b_screen(self) -> str:
        return f"{MAP_DIR}{self.map_id}b.s"

    @property
    def descriptor(self) -> str:
        return f"{MAP_DIR}{self.map_id}.d"

    @property
    def walkability(self) -> str:
        return f"{MAP_DIR}{self.map_id}.0t"

    @property
    def attributes(self) -> str:
        return f"{MAP_DIR}{self.map_id}.a"

    def all_paths(self) -> List[str]:
        return [
            self.layer_a_tiles, self.layer_a_palette, self.layer_a_screen,
            self.layer_b_tiles, self.layer_b_palette, self.layer_b_screen,
            self.descriptor, self.walkability, self.attributes,
        ]


_D_RE = re.compile(rf"^{re.escape(MAP_DIR.upper())}(\d+)\.D$")


def discover_map_ids(file_table) -> List[str]:
    """Numeric-sorted list of map ids present in the ROM.

    Discovered from ``*.d`` files (one per map id, every map ships one).
    """
    ids: List[str] = []
    for path in file_table.paths():
        m = _D_RE.match(path)
        if not m:
            continue
        ids.append(m.group(1))
    return sorted(set(ids), key=int)


# ---- .c — tile graphics --------------------------------------------------


def parse_tiles(raw: bytes) -> List[bytes]:
    """Return ``[tile_bytes, ...]`` for a ``.c`` payload.

    ``raw`` may be RLE-30 compressed; decompression is automatic.

    Header: ``u32 payload_size``. Body: ``payload_size / 32`` tiles, each
    32 bytes (4bpp, low nibble = left pixel, row-major within the tile).

    Tiles are returned *packed* (32 B each) — the rendering path is
    responsible for nibble-unpacking. This keeps the codec symmetric:
    :func:`build_tiles` consumes the same 32-byte chunks.
    """
    raw = maybe_decompress(raw)
    payload_size = struct.unpack_from("<I", raw, 0)[0]
    data = raw[4:4 + payload_size]
    if len(data) % 32 != 0:
        raise ValueError(
            f"tile payload {len(data)} B is not a multiple of 32 — "
            "file is truncated or header is wrong"
        )
    return [data[i:i + 32] for i in range(0, len(data), 32)]


def build_tiles(tiles: Sequence[bytes]) -> bytes:
    """Pack tile chunks into a ``.c`` payload (uncompressed).

    Each entry must be exactly 32 bytes. Caller is responsible for
    RLE-30 wrapping before splicing back into the FAT.
    """
    body = bytearray()
    for ix, tile in enumerate(tiles):
        if len(tile) != 32:
            raise ValueError(
                f"tile {ix} is {len(tile)} B, expected 32"
            )
        body += tile
    out = bytearray(struct.pack("<I", len(body)))
    out += body
    return bytes(out)


# ---- .p — palette --------------------------------------------------------

PALETTE_BANK_COLORS = 16
PALETTE_BANK_BYTES = PALETTE_BANK_COLORS * 2  # 32

# Vanilla survey (Dusk, 265 maps):
# - Layer A: 264 maps × 8 banks, 1 map (226) × 7 banks
# - Layer B: 263 maps × 8 banks, 2 maps (8, 9) × 8 banks + 4-byte zero trailer
# Codec therefore reads the bank count from payload_size rather than
# hardcoding 8, and preserves any trailing bytes verbatim.


def parse_palette(
    raw: bytes,
) -> Tuple[List[List[Tuple[int, int, int]]], bytes]:
    """Return ``(banks, trailer)`` for a ``.p`` payload.

    Colors are decoded from NDS BGR555 to 8-bit RGB (bit-replicated, so
    a no-edit round-trip is byte-identical). Index 0 of each bank is
    returned as-is; the renderer decides transparency per layer.

    Header: ``u32 payload_size``. Body: ``payload_size`` bytes. The
    leading ``n_banks × 32`` bytes are 16-color BGR555 banks; anything
    after that is preserved as ``trailer`` so the build path can
    reproduce it exactly. Two vanilla maps (8b.p and 9b.p) ship 4 bytes
    of zero trailer past 8 banks.
    """
    raw = maybe_decompress(raw)
    payload_size = struct.unpack_from("<I", raw, 0)[0]
    data = raw[4:4 + payload_size]
    n_banks = len(data) // PALETTE_BANK_BYTES
    bank_bytes_end = n_banks * PALETTE_BANK_BYTES
    trailer = bytes(data[bank_bytes_end:])
    banks: List[List[Tuple[int, int, int]]] = []
    for bix in range(n_banks):
        bank: List[Tuple[int, int, int]] = []
        for cix in range(PALETTE_BANK_COLORS):
            off = bix * PALETTE_BANK_BYTES + cix * 2
            word = data[off] | (data[off + 1] << 8)
            r5 = word & 0x1F
            g5 = (word >> 5) & 0x1F
            b5 = (word >> 10) & 0x1F
            r = (r5 << 3) | (r5 >> 2)
            g = (g5 << 3) | (g5 >> 2)
            b = (b5 << 3) | (b5 >> 2)
            bank.append((r, g, b))
        banks.append(bank)
    return banks, trailer


def build_palette(
    banks: Sequence[Sequence[Tuple[int, int, int]]],
    trailer: bytes = b"",
) -> bytes:
    """Pack ``banks`` (+ optional trailer) into a ``.p`` payload.

    Round-trips byte-identically with :func:`parse_palette` because
    vanilla BGR555 colors are already bit-replicated 5-bit values; for
    arbitrary 8-bit RGB input the encoder uses the high 5 bits.
    """
    body = bytearray()
    for bix, bank in enumerate(banks):
        if len(bank) != PALETTE_BANK_COLORS:
            raise ValueError(
                f"bank {bix} has {len(bank)} colors, expected "
                f"{PALETTE_BANK_COLORS}"
            )
        for r, g, b in bank:
            r5 = (r >> 3) & 0x1F
            g5 = (g >> 3) & 0x1F
            b5 = (b >> 3) & 0x1F
            word = r5 | (g5 << 5) | (b5 << 10)
            body += struct.pack("<H", word)
    body += trailer
    out = bytearray(struct.pack("<I", len(body)))
    out += body
    return bytes(out)


# ---- .s — BG tilemap -----------------------------------------------------


def parse_screen(raw: bytes) -> Tuple[int, int, List[int]]:
    """Return ``(width_px, height_px, entries)`` for a ``.s`` payload.

    ``raw`` may be RLE-30 compressed; decompression is automatic.

    Header: ``u16 width_px, u16 height_px``. Body:
    ``(w/8) * (h/8)`` u16 tilemap entries (bits 0-9 tile, 10 hflip,
    11 vflip, 12-15 palette bank). Row-major.
    """
    raw = maybe_decompress(raw)
    width_px, height_px = struct.unpack_from("<HH", raw, 0)
    tw = width_px // 8
    th = height_px // 8
    entries: List[int] = []
    for ix in range(tw * th):
        off = 4 + ix * 2
        if off + 2 > len(raw):
            entries.append(0)
            continue
        entries.append(raw[off] | (raw[off + 1] << 8))
    return width_px, height_px, entries


def build_screen(width_px: int, height_px: int, entries: Sequence[int]) -> bytes:
    """Pack a ``.s`` payload (uncompressed)."""
    tw = width_px // 8
    th = height_px // 8
    if len(entries) != tw * th:
        raise ValueError(
            f"entries length {len(entries)} != "
            f"{tw}*{th} = {tw * th}"
        )
    out = bytearray(struct.pack("<HH", width_px, height_px))
    for e in entries:
        out += struct.pack("<H", e & 0xFFFF)
    return bytes(out)


# ---- .0t — pixel walkability mask ---------------------------------------


def parse_walkability(raw: bytes) -> Tuple[int, int, bytearray]:
    """Return ``(width_px, height_px, bits)`` for a ``.0t`` payload.

    Header: ``u16 width_px, u16 0, u16 height_px, u16 0`` (8 bytes; the
    two zero halves look like the high u16 of a u32 width/height pair
    the engine reads). Body: ``width_px * height_px`` bits packed
    LSB-first, ``bit=1 → blocked``, ``bit=0 → walkable``.

    Returned ``bits`` is the raw bit-packed byte buffer (one byte per
    8 pixels); callers can index a bit at ``(x, y)`` via
    ``(bits[(y * width_px + x) // 8] >> ((y * width_px + x) % 8)) & 1``.
    """
    raw = maybe_decompress(raw)
    w_lo, w_hi, h_lo, h_hi = struct.unpack_from("<HHHH", raw, 0)
    if w_hi != 0 or h_hi != 0:
        # Recon doc says these are always zero across 265 vanilla maps.
        # Raise rather than silently truncate so a real format change
        # surfaces in the tests instead of corrupting rendering.
        raise ValueError(
            f"walkability header high words nonzero "
            f"(w_hi={w_hi}, h_hi={h_hi}) — unexpected format"
        )
    width_px, height_px = w_lo, h_lo
    bits_needed = width_px * height_px
    bytes_needed = (bits_needed + 7) // 8
    body = bytearray(raw[8:8 + bytes_needed])
    if len(body) != bytes_needed:
        raise ValueError(
            f"walkability body {len(body)} B, expected {bytes_needed}"
        )
    return width_px, height_px, body


def build_walkability(width_px: int, height_px: int, bits: bytes) -> bytes:
    """Pack a ``.0t`` payload (uncompressed)."""
    bits_needed = width_px * height_px
    bytes_needed = (bits_needed + 7) // 8
    if len(bits) != bytes_needed:
        raise ValueError(
            f"bits length {len(bits)} != expected {bytes_needed} "
            f"for {width_px}x{height_px}"
        )
    out = bytearray(struct.pack("<HHHH", width_px, 0, height_px, 0))
    out += bits
    return bytes(out)


# ---- .a — per-tile attribute grid (opaque) ------------------------------


def parse_attributes(raw: bytes) -> bytes:
    """Return the decompressed ``.a`` payload verbatim.

    Across all 265 vanilla maps the body is fully zero; the recon doc
    confirms no engine code populates it. Until an engine read-site is
    found, the editor treats the file as opaque bytes — header + body
    are kept as a single blob so round-trip is trivially exact and any
    future format pivot doesn't need a parser rewrite here.
    """
    return maybe_decompress(raw)


def build_attributes(payload: bytes) -> bytes:
    """Return the ``.a`` payload as-is (uncompressed)."""
    return bytes(payload)


# ---- .d — map descriptor -------------------------------------------------


@dataclass
class MapDescriptor:
    """Parsed 138-byte ``.d`` file.

    - ``header_bytes`` (42 B, 0x00..0x29): shared header / layer block.
      Mostly constant across maps; carries two ``(version=1, width=100,
      height=100)`` allocation blocks. Kept opaque here.
    - ``tuples`` (7 × 6 u16): the variable region (0x2A..0x7D), one
      tuple per encounter/event/warp/etc. slot. Unused slots are
      ``EMPTY_TUPLE`` (all 0xFFFF) — the vanilla files pad with 0xFF
      bytes, so this round-trips byte-identically.
    - ``trailer_bytes`` (12 B, 0x7E..0x89): trailing constant block.
      Mostly fixed across maps; kept opaque.

    Tuple column semantics (best-guess from per-area distribution, see
    ``research_docs/claude_notes/btmap_map_recon.md`` §`.d`):

    - col 0 — ``kind`` (0..7 enum, category-dependent)
    - col 1 — ``param_a``
    - col 2 — ``param_b``
    - col 3 — ``flag`` (almost always 1)
    - col 4 — ``reserved`` (always 0)
    - col 5 — ``weight`` (multiples of 4)

    The editor exposes raw integers per ``feedback_no_fabricated_game_
    mechanics`` — labels are guidance, not pinned semantics.
    """
    header_bytes: bytes
    tuples: List[Tuple[int, int, int, int, int, int]]
    trailer_bytes: bytes

    def used_tuples(self) -> List[Tuple[int, int, int, int, int, int]]:
        """Return only the non-empty tuple slots."""
        return [t for t in self.tuples if t != EMPTY_TUPLE]


def parse_descriptor(raw: bytes) -> MapDescriptor:
    """Parse a ``.d`` file into a :class:`MapDescriptor`."""
    raw = maybe_decompress(raw)
    if len(raw) != DESCRIPTOR_SIZE:
        raise ValueError(
            f"descriptor size {len(raw)} B, expected {DESCRIPTOR_SIZE}"
        )
    header = raw[:DESCRIPTOR_HEADER_SIZE]
    trailer = raw[DESCRIPTOR_TRAILER_OFF:DESCRIPTOR_TRAILER_OFF + DESCRIPTOR_TRAILER_SIZE]
    tuples: List[Tuple[int, int, int, int, int, int]] = []
    for ix in range(DESCRIPTOR_MAX_TUPLES):
        off = DESCRIPTOR_TUPLE_REGION_OFF + ix * DESCRIPTOR_TUPLE_COLS * 2
        cols = struct.unpack_from("<HHHHHH", raw, off)
        tuples.append(cols)
    return MapDescriptor(
        header_bytes=bytes(header),
        tuples=tuples,
        trailer_bytes=bytes(trailer),
    )


def build_descriptor(desc: MapDescriptor) -> bytes:
    """Pack a :class:`MapDescriptor` back into the 138-byte ``.d`` layout."""
    if len(desc.header_bytes) != DESCRIPTOR_HEADER_SIZE:
        raise ValueError(
            f"header {len(desc.header_bytes)} B, expected {DESCRIPTOR_HEADER_SIZE}"
        )
    if len(desc.trailer_bytes) != DESCRIPTOR_TRAILER_SIZE:
        raise ValueError(
            f"trailer {len(desc.trailer_bytes)} B, expected {DESCRIPTOR_TRAILER_SIZE}"
        )
    if len(desc.tuples) != DESCRIPTOR_MAX_TUPLES:
        raise ValueError(
            f"tuples must be exactly {DESCRIPTOR_MAX_TUPLES} slots "
            f"(use EMPTY_TUPLE for unused), got {len(desc.tuples)}"
        )
    out = bytearray(DESCRIPTOR_SIZE)
    out[:DESCRIPTOR_HEADER_SIZE] = desc.header_bytes
    for ix, cols in enumerate(desc.tuples):
        if len(cols) != DESCRIPTOR_TUPLE_COLS:
            raise ValueError(
                f"tuple {ix} has {len(cols)} cols, "
                f"expected {DESCRIPTOR_TUPLE_COLS}"
            )
        off = DESCRIPTOR_TUPLE_REGION_OFF + ix * DESCRIPTOR_TUPLE_COLS * 2
        struct.pack_into("<HHHHHH", out, off, *(c & 0xFFFF for c in cols))
    out[DESCRIPTOR_TRAILER_OFF:DESCRIPTOR_TRAILER_OFF + DESCRIPTOR_TRAILER_SIZE] = \
        desc.trailer_bytes
    return bytes(out)


# ---- Rendering -----------------------------------------------------------


@dataclass
class MapPreview:
    """Composited two-layer field-map render."""
    rgba: bytes
    width: int
    height: int


def _unpack_tile(tile: bytes) -> bytes:
    """Expand a 32-byte 4bpp tile into 64 palette indices (low nibble = left)."""
    out = bytearray(64)
    for bix, b in enumerate(tile):
        out[bix * 2] = b & 0x0F
        out[bix * 2 + 1] = (b >> 4) & 0x0F
    return bytes(out)


def _render_layer(
    width_px: int,
    height_px: int,
    entries: Sequence[int],
    tiles: Sequence[bytes],
    palettes: Sequence[Sequence[Tuple[int, int, int]]],
    *,
    backdrop_opaque: bool,
) -> bytearray:
    """Render one BG layer to a flat RGBA bytearray.

    ``backdrop_opaque=True`` for the bottom layer — palette index 0
    paints with full alpha (the map's background color). ``False`` for
    the top layer — palette index 0 stays transparent so the bottom
    layer shows through.
    """
    rgba = bytearray(width_px * height_px * 4)
    tw = width_px // 8
    n_tiles = len(tiles)
    n_banks = len(palettes)
    unpacked_cache: dict[int, bytes] = {}
    for ty in range(height_px // 8):
        for tx in range(tw):
            entry = entries[ty * tw + tx]
            tile_ix = entry & 0x3FF
            hflip = bool(entry & 0x400)
            vflip = bool(entry & 0x800)
            pal_ix = (entry >> 12) & 0xF
            if tile_ix >= n_tiles:
                continue
            tile = unpacked_cache.get(tile_ix)
            if tile is None:
                tile = _unpack_tile(tiles[tile_ix])
                unpacked_cache[tile_ix] = tile
            palette = palettes[pal_ix] if pal_ix < n_banks else palettes[0]
            base_x = tx * 8
            base_y = ty * 8
            for py in range(8):
                row = py if not vflip else (7 - py)
                row_off = row * 8
                dst_y = base_y + py
                for pxn in range(8):
                    col = pxn if not hflip else (7 - pxn)
                    idx = tile[row_off + col]
                    if idx == 0 and not backdrop_opaque:
                        continue
                    r, g, b = palette[idx]
                    dst_off = (dst_y * width_px + base_x + pxn) * 4
                    rgba[dst_off] = r
                    rgba[dst_off + 1] = g
                    rgba[dst_off + 2] = b
                    rgba[dst_off + 3] = 255
    return rgba


def _composite_over(base: bytearray, top: bytes, width_px: int, height_px: int) -> None:
    """Alpha-over compositing of ``top`` onto ``base`` in place.

    Both buffers are flat RGBA; both must share ``(width_px, height_px)``.
    The top layer uses index-0-transparent semantics: any pixel with
    ``alpha=0`` is skipped; opaque pixels overwrite.
    """
    n = width_px * height_px
    for i in range(n):
        off = i * 4
        if top[off + 3] == 0:
            continue
        base[off] = top[off]
        base[off + 1] = top[off + 1]
        base[off + 2] = top[off + 2]
        base[off + 3] = 255


def render_map_from_file_table(map_id: str, file_table, rom: bytes) -> MapPreview:
    """Render a full two-layer field map.

    Layer A is drawn opaque first; layer B is composited on top with
    palette index 0 treated as transparent. Mirrors ``map_render.py``
    in ``research_docs/claude_notes/`` — the reference implementation
    that was verified visually on every vanilla map.
    """
    files = MapFiles(map_id)
    if files.layer_a_screen not in file_table:
        raise KeyError(f"map {map_id} missing layer A")
    tiles_a = parse_tiles(file_table.slice(rom, files.layer_a_tiles))
    palettes_a, _ = parse_palette(file_table.slice(rom, files.layer_a_palette))
    width_px, height_px, entries_a = parse_screen(
        file_table.slice(rom, files.layer_a_screen)
    )
    rgba = _render_layer(
        width_px, height_px, entries_a, tiles_a, palettes_a,
        backdrop_opaque=True,
    )
    if (
        files.layer_b_screen in file_table
        and files.layer_b_tiles in file_table
        and files.layer_b_palette in file_table
    ):
        tiles_b = parse_tiles(file_table.slice(rom, files.layer_b_tiles))
        palettes_b, _ = parse_palette(file_table.slice(rom, files.layer_b_palette))
        bw, bh, entries_b = parse_screen(file_table.slice(rom, files.layer_b_screen))
        if (bw, bh) == (width_px, height_px) and tiles_b:
            top = _render_layer(
                bw, bh, entries_b, tiles_b, palettes_b,
                backdrop_opaque=False,
            )
            _composite_over(rgba, top, width_px, height_px)
    return MapPreview(rgba=bytes(rgba), width=width_px, height=height_px)


def apply_walkability_overlay(
    preview: MapPreview,
    bits: bytes,
    walk_width: int,
    walk_height: int,
    *,
    tint: Tuple[int, int, int] = (255, 0, 0),
    alpha: int = 110,
) -> MapPreview:
    """Return a new MapPreview with ``bit=1`` (blocked) pixels tinted.

    ``bits`` is the LSB-first bit buffer from :func:`parse_walkability`,
    laid out at *walkability* stride (``walk_width`` px per row), which
    may differ from the composite's dimensions — in vanilla Dusk, map
    88 ships a 768×384 ``.0t`` against a 752×384 composite. Reading
    bits at the composite stride would shear the overlay by 16 px per
    row; here we walk the overlap region at the walkability's own
    stride.

    The overlay alpha-blends the tint over the source, then overlays a
    diagonal hatch every 4 px so blocked tiles read as textured rather than
    merely red-tinted — disambiguating from red-floored maps.
    """
    bytes_needed = (walk_width * walk_height + 7) // 8
    if len(bits) < bytes_needed:
        raise ValueError(
            f"walkability bits ({len(bits)} B) too short for "
            f"{walk_width}x{walk_height} ({bytes_needed} B needed)"
        )
    # Vectorised with numpy: the walk tab re-tints the whole composite on
    # every paint-move sample, and the old per-pixel Python loop cost ~100 ms
    # per call on a 768×384 map — the dominant source of paint lag. numpy is
    # imported lazily so headless map-codec paths don't pull it in.
    import numpy as np

    tr, tg, tb = tint
    inv = 255 - alpha
    # Hatch line color: darker than the tint, fully opaque, so it
    # contrasts against both tinted blocked tiles AND red-floored maps.
    hr = max(0, tr - 120)
    hg = max(0, tg - 30) if tg else 0
    hb = max(0, tb - 30) if tb else 0
    pw = preview.width
    ph = preview.height
    overlap_w = min(walk_width, pw)
    overlap_h = min(walk_height, ph)

    src = np.frombuffer(bytes(preview.rgba), dtype=np.uint8).reshape(ph, pw, 4)
    out = src.copy()
    if overlap_w > 0 and overlap_h > 0:
        bit_arr = np.unpackbits(
            np.frombuffer(bytes(bits), dtype=np.uint8), bitorder="little",
        )
        ys = np.arange(overlap_h)
        xs = np.arange(overlap_w)
        # bit index = y*walk_width + x, read at the walkability's own stride.
        flat_ix = (ys[:, None] * walk_width) + xs[None, :]
        blocked = bit_arr[flat_ix].astype(bool)
        hatch = blocked & (((ys[:, None] + xs[None, :]) & 3) == 0)
        tinted = blocked & ~hatch

        region = out[:overlap_h, :overlap_w, :]
        src_rgb = src[:overlap_h, :overlap_w, :3].astype(np.uint16)
        blended = (
            (src_rgb * inv) + np.array([tr, tg, tb], dtype=np.uint16) * alpha
        ) // 255
        for ch, hval in enumerate((hr, hg, hb)):
            chan = region[:, :, ch]
            chan[tinted] = blended[:, :, ch][tinted].astype(np.uint8)
            chan[hatch] = hval
        region[:, :, 3][blocked] = 255
    return MapPreview(rgba=out.tobytes(), width=pw, height=ph)


def render_single_layer(
    tiles_raw: bytes,
    palette_raw: bytes,
    screen_raw: bytes,
    *,
    backdrop_opaque: bool = True,
) -> MapPreview:
    """Render one layer to a MapPreview from its raw FAT bytes.

    Used by the read-only browser's Layer A / Layer B preview tabs.
    """
    tiles = parse_tiles(tiles_raw)
    palettes, _ = parse_palette(palette_raw)
    width_px, height_px, entries = parse_screen(screen_raw)
    rgba = _render_layer(
        width_px, height_px, entries, tiles, palettes,
        backdrop_opaque=backdrop_opaque,
    )
    return MapPreview(rgba=bytes(rgba), width=width_px, height=height_px)


def paint_tile_into_rgba(
    rgba: bytearray,
    canvas_w: int,
    tx: int,
    ty: int,
    entry: int,
    tiles: Sequence[bytes],
    palettes: Sequence[Sequence[Tuple[int, int, int]]],
    *,
    backdrop_opaque: bool,
) -> None:
    """Re-paint a single 8x8 cell of an already-rendered layer in place.

    Used by Phase D's tilemap painter — when the user clicks a cell to
    overwrite its tilemap entry, only those 64 pixels need to update; the
    full layer render is expensive and runs once per map selection.

    Mirrors :func:`_render_layer`'s inner loop semantics:
    backdrop_opaque=True paints palette index 0 with full alpha;
    False leaves it as alpha=0 so a composited bottom layer would show.
    """
    tile_ix = entry & 0x3FF
    hflip = bool(entry & 0x400)
    vflip = bool(entry & 0x800)
    pal_ix = (entry >> 12) & 0xF
    n_tiles = len(tiles)
    n_banks = len(palettes)
    palette = palettes[pal_ix] if pal_ix < n_banks else palettes[0]
    base_x = tx * 8
    base_y = ty * 8
    if tile_ix >= n_tiles:
        # Out-of-range tile — emulate _render_layer's "skip" behavior by
        # leaving the cell at whatever was already in the buffer. The
        # caller is overwriting an existing entry so they expect a change,
        # but raising here would block legitimate "draw beyond loaded set"
        # workflows once tileset replacement lands.
        return
    tile = _unpack_tile(tiles[tile_ix])
    for py in range(8):
        row = py if not vflip else (7 - py)
        row_off = row * 8
        dst_y = base_y + py
        for pxn in range(8):
            col = pxn if not hflip else (7 - pxn)
            idx = tile[row_off + col]
            dst_off = (dst_y * canvas_w + base_x + pxn) * 4
            if idx == 0 and not backdrop_opaque:
                rgba[dst_off] = 0
                rgba[dst_off + 1] = 0
                rgba[dst_off + 2] = 0
                rgba[dst_off + 3] = 0
                continue
            r, g, b = palette[idx]
            rgba[dst_off] = r
            rgba[dst_off + 1] = g
            rgba[dst_off + 2] = b
            rgba[dst_off + 3] = 255


def render_tile_picker(
    tiles: Sequence[bytes],
    palette: Sequence[Tuple[int, int, int]],
    *,
    tiles_per_row: int = 16,
    scale: int = 2,
) -> MapPreview:
    """Render every tile in ``tiles`` as a grid using ``palette``.

    The grid is the Phase D tile picker: click cell ``(tx, ty)`` to
    select tile index ``ty * tiles_per_row + tx``. Scale is integer
    nearest-neighbor so the tile boundaries stay aligned to pixels and
    the click-to-tile math is just ``ix // scale // 8``.

    Index 0 is rendered opaque here regardless of layer transparency —
    the picker wants to *show* the tile, not preview compositing.
    """
    n = len(tiles)
    if n == 0:
        return MapPreview(rgba=b"", width=0, height=0)
    rows = (n + tiles_per_row - 1) // tiles_per_row
    cell_px = 8 * scale
    w = tiles_per_row * cell_px
    h = rows * cell_px
    rgba = bytearray(w * h * 4)
    for ix, tile in enumerate(tiles):
        gx = ix % tiles_per_row
        gy = ix // tiles_per_row
        unpacked = _unpack_tile(tile)
        base_x = gx * cell_px
        base_y = gy * cell_px
        for py in range(8):
            for pxn in range(8):
                idx = unpacked[py * 8 + pxn]
                r, g, b = palette[idx]
                for sy in range(scale):
                    dy = base_y + py * scale + sy
                    row_off = dy * w * 4
                    for sx in range(scale):
                        dx = base_x + pxn * scale + sx
                        off = row_off + dx * 4
                        rgba[off] = r
                        rgba[off + 1] = g
                        rgba[off + 2] = b
                        rgba[off + 3] = 255
    return MapPreview(rgba=bytes(rgba), width=w, height=h)
