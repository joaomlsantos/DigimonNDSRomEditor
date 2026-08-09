"""Portable BTCHR sprite file (.btchrspr) — one digimon's sprite assets.

Carries everything needed to put a digimon's sprite into another slot:
the 5 BTCHR.PAK entries (mini-header, NCGR, NCLR, NCER, NANR), the
target tpf for chrsize.bin, and the uncompressed btchrsize.bin value.
Source ``digimon_id`` is recorded for informational use only — import
keeps the destination slot's identity.

Format (little-endian):
  offset  size  field
  0       4     magic = b"BSPR"
  4       2     format_version = 1
  6       2     source_digimon_id (informational)
  8       2     source_tpf
  10      2     reserved (0)
  12      4     btchrsize_value
  16      5     u32 length for each of the 5 entries
  36      ...   entry payloads concatenated, in order 0..4

Apply is a pure function over a parsed PakFile and the two raw size
buffers (chrsize.bin, btchrsize.bin) — leaves splicing to the caller.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import List, Optional, Tuple

from . import btchr, pak, sprite


MAGIC = b"BSPR"
FORMAT_VERSION = 1
HEADER_SIZE = 16
ENTRY_COUNT = btchr.GROUP_SIZE  # 5
# Warn threshold: 5 * max_non_boss_tpf (432) = 2160 tiles. Above this the
# port is likely a boss-class sprite and may break in multi-sprite screens.
TILE_COUNT_WARN_THRESHOLD = 2160

# Per-cell (tiles-per-frame) cap of the party-viewer / single-sprite VRAM
# pool. Sprites whose fs exceeds this garble in the party viewer and gallery
# (see project memory partyviewer_bigsprite_cap); it's also the target
# compress_existing_fit shrinks toward.
PARTY_VIEWER_TPF_CAP = 512


@dataclass(frozen=True)
class BtchrSprite:
    """In-memory representation of a .btchrspr payload."""

    source_digimon_id: int
    source_tpf: int
    btchrsize_value: int
    entries: Tuple[bytes, ...]  # length ENTRY_COUNT

    @property
    def ncgr_tile_count(self) -> int:
        """Uncompressed 8bpp tile count of the carried NCGR (entry 1).

        Mirrors the inline RAHC walk used elsewhere in the codebase so
        the caller can decide whether to warn before applying.
        """
        ncgr_raw = sprite.decompress_rle30(self.entries[1])
        rahc = sprite.find_block(ncgr_raw, b"RAHC")
        # RAHC+24 holds the data byte count; div by 64 for 8bpp tiles.
        data_size = struct.unpack_from("<I", ncgr_raw, rahc + 24)[0]
        return data_size // btchr.BYTES_PER_TILE_8BPP


def serialize(
    pak_obj: pak.PakFile,
    group: int,
    *,
    source_digimon_id: int,
    source_tpf: int,
    btchrsize_value: int,
) -> bytes:
    """Build a .btchrspr payload from ``group``'s 5 PAK entries plus the
    accompanying chrsize/btchrsize values."""
    base = group * btchr.GROUP_SIZE
    entries: List[bytes] = [
        bytes(pak_obj.entries[base + i]) for i in range(ENTRY_COUNT)
    ]
    out = bytearray()
    out += MAGIC
    out += struct.pack("<H", FORMAT_VERSION)
    out += struct.pack("<H", source_digimon_id & 0xFFFF)
    out += struct.pack("<H", source_tpf & 0xFFFF)
    out += struct.pack("<H", 0)  # reserved
    out += struct.pack("<I", btchrsize_value & 0xFFFFFFFF)
    for e in entries:
        out += struct.pack("<I", len(e))
    for e in entries:
        out += e
    return bytes(out)


def parse(data: bytes) -> BtchrSprite:
    """Decode a .btchrspr payload. Raises ValueError on bad format."""
    if len(data) < HEADER_SIZE + 4 * ENTRY_COUNT:
        raise ValueError("truncated .btchrspr (header missing)")
    if data[:4] != MAGIC:
        raise ValueError(
            f"bad magic: expected {MAGIC!r}, got {data[:4]!r}"
        )
    fmt = struct.unpack_from("<H", data, 4)[0]
    if fmt != FORMAT_VERSION:
        raise ValueError(
            f"unsupported .btchrspr format_version {fmt} "
            f"(this editor reads version {FORMAT_VERSION})"
        )
    source_digimon_id = struct.unpack_from("<H", data, 6)[0]
    source_tpf = struct.unpack_from("<H", data, 8)[0]
    btchrsize_value = struct.unpack_from("<I", data, 12)[0]
    lengths = [
        struct.unpack_from("<I", data, HEADER_SIZE + i * 4)[0]
        for i in range(ENTRY_COUNT)
    ]
    cursor = HEADER_SIZE + 4 * ENTRY_COUNT
    entries: List[bytes] = []
    for i, n in enumerate(lengths):
        if cursor + n > len(data):
            raise ValueError(
                f"truncated .btchrspr: entry {i} wants {n}B but only "
                f"{len(data) - cursor}B left"
            )
        entries.append(bytes(data[cursor:cursor + n]))
        cursor += n
    return BtchrSprite(
        source_digimon_id=source_digimon_id,
        source_tpf=source_tpf,
        btchrsize_value=btchrsize_value,
        entries=tuple(entries),
    )


# Cover thresholds tried per sprite, keeping the smallest footprint. The
# threshold is the min occupancy a legal OBJ needs to be placed: low grabs big
# OBJs (few OAMs, but they swallow transparent tiles), high traces the
# silhouette tighter (less wasted tile area, but more OAMs whose slot-rounding
# padding can eat the gain). The fs-minimising point differs per sprite (0.8
# wins most, but the biggest bosses want 0.6–0.75), and every threshold renders
# identical pixels — so searching is free VRAM. Verified across BTCHR.PAK:
# −9.5% aggregate vs a fixed 0.5.
_COVER_THRESHOLDS = (0.5, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8)


def _assemble_from_layout(
    boundary_bytes, per_cell_oams, per_cell_plan, total_tiles, fs,
    dims, cell_indexed, tpl, template_entries, palette, source_digimon_id,
):
    """Turn a computed OAM layout (per-cell OAMs + tile plan + fs + boundary)
    into a finished :class:`BtchrSprite`. Shared by the union and joint covers —
    only the layout differs; the NCGR/NCER/mini/sidecar assembly is identical."""
    from . import ncer as ncer_mod

    _tpl_mini, tpl_ncgr, tpl_nclr, tpl_ncer, tpl_nanr = tpl
    # Lay every cell's pixels into the uniformly-chunked tile bank (each cell's
    # plan maps its OAM tiles to their source positions).
    tiles = bytearray(total_tiles * btchr.BYTES_PER_TILE_8BPP)
    for plan, (w, _h), indexed in zip(per_cell_plan, dims, cell_indexed):
        part = ncer_mod.encode_indexed_tiles(indexed, w, plan, total_tiles, is8bpp=True)
        for d, *_ in plan:
            tiles[d * 64:d * 64 + 64] = part[d * 64:d * 64 + 64]

    # The NCGR and NCER must agree on the OAM tile boundary — the engine reads
    # it per-sprite, and a mismatch fetches tiles at the wrong stride.
    new_ncgr = sprite.build_ncgr_from_template(bytes(tiles), tpl_ncgr)
    new_ncgr = sprite.set_ncgr_boundary(new_ncgr, boundary_bytes)
    new_ncer = ncer_mod.set_ncer_boundary(tpl_ncer, boundary_bytes)
    for k, oams in enumerate(per_cell_oams):
        new_ncer = ncer_mod.set_cell_oams(new_ncer, k, oams)
    new_nclr = sprite.build_nclr_from_template(tpl_nclr, {0: list(palette)})
    new_mini = bytearray(_tpl_mini)
    struct.pack_into("<H", new_mini, 0, fs & 0xFFFF)  # footprint_scale (tiles/cell)
    # flag@0x02 is the sprite's VRAM/size tier — the engine sizes its per-cell
    # tile buffer from it, so a big sprite carrying a small template's flag
    # under-allocates and renders garbled. Verified band maxima across vanilla:
    # fs≤256 → 0, ≤356 → 1, else 2 (every boundary-256 sprite is flag 2).
    size_flag = 0 if fs <= 256 else (1 if fs <= 356 else 2)
    struct.pack_into("<H", new_mini, 2, size_flag)

    # btchrsize = sum of *uncompressed* sizes of entries 1–4 (NCGR/NCLR/NCER/NANR).
    btchrsize_value = len(new_ncgr) + len(new_nclr) + len(new_ncer) + len(tpl_nanr)
    entries = (
        sprite.compress_rle30(bytes(new_mini)),
        sprite.compress_rle30(new_ncgr),
        sprite.compress_rle30(new_nclr),
        sprite.compress_rle30(new_ncer),
        bytes(template_entries[4]),  # NANR reused as-is (already compressed)
    )
    return BtchrSprite(
        source_digimon_id=source_digimon_id,
        source_tpf=fs,
        btchrsize_value=btchrsize_value,
        entries=entries,
    )


def build_from_cells(
    cell_indexed: List[bytes],
    dims: List[Tuple[int, int]],
    palette: List[Tuple[int, int, int]],
    template_entries: List[bytes],
    *,
    source_digimon_id: int = 0,
    oam_origin: Optional[Tuple[int, int]] = None,
    min_opaque: int = 1,
) -> BtchrSprite:
    """Assemble a fresh sprite kit from per-cell 8bpp images + a 256-color
    palette, reusing a destination group's 5 entries as templates.

    ``min_opaque > 1`` drops 8×8 tiles carrying fewer than that many opaque
    pixels (a lossy edge trim) — used to shave the last tiles off a sprite that
    just overflows a VRAM cap; ``1`` (default) is lossless.

    ``cell_indexed[k]`` is one-byte-per-pixel indexed data (row-major) of
    size ``dims[k] = (w, h)`` (both multiples of 8). One image per NCER cell;
    the count must match the template's cell count so the mini-header
    animation tracks stay valid. New OAMs are generated to cover each cell
    (custom layout), the NCGR is rebuilt as the uniformly-chunked shared tile
    bank, and mini-header/chrsize/btchrsize are recomputed to match. The NANR
    (entry 4) is reused verbatim. Returns a :class:`BtchrSprite` ready for
    :func:`apply` (or the editor's ``PortBtchrSpriteCommand``).
    """
    from . import ncer as ncer_mod

    if len(cell_indexed) != len(dims):
        raise ValueError("cell_indexed and dims length mismatch")
    tpl = [sprite.decompress_rle30(template_entries[i]) for i in range(ENTRY_COUNT)]
    tpl_mini, tpl_ncgr, tpl_nclr, tpl_ncer, tpl_nanr = tpl
    tpl_parsed = ncer_mod.parse_ncer(tpl_ncer)
    if len(dims) != len(tpl_parsed.cells):
        raise ValueError(
            f"import provides {len(dims)} cell image(s) but the sprite has "
            f"{len(tpl_parsed.cells)} cells — supply exactly one image per cell"
        )
    for w, h in dims:
        if w <= 0 or h <= 0 or w % 8 or h % 8:
            raise ValueError(
                f"cell {w}×{h}px: dimensions must be positive multiples of 8"
            )
        # For a fresh import we centre OAMs on the origin, so the signed
        # position fields (x −256..255, y −128..127) cap the sprite at
        # 512×256 px. When ``oam_origin`` is supplied (re-covering an existing
        # sprite at its own coords) this centred cap doesn't apply — the real
        # OAM range is validated after generation instead.
        if oam_origin is None and (w > 512 or h > 256):
            raise ValueError(
                f"cell {w}×{h}px is too big — a battle sprite fits within "
                "512×256 px (the OAM position range). Shrink the artwork."
            )
    # Cover only the non-transparent tiles (index != 0), sharing one
    # uniformly-chunked NCGR — so a sprite on a transparent field costs tiles
    # and OAMs for the character, not the empty background. Pick the OAM
    # tile-slot stride: BTCHR ships 128 (2 tiles/slot), but the 10-bit OAM
    # tile field only reaches 2046 tiles there — big sprites need 256 (4/slot),
    # what vanilla bosses use. Smallest stride that fits wins (less padding).
    #
    # ALL 5 CELLS MUST SHARE ONE OAM LAYOUT. The engine renders every animation
    # frame with cell 0's OAM structure (frames differ only by a uniform per-
    # cell x/y translation — project_btchr_vertical_pivots), so a per-cell
    # layout draws frames 1-4's tiles at cell 0's positions and garbles them in
    # the gallery / party viewer (confirmed in-game: ChaosGallantmon, Ophanimon).
    # So cover the UNION of all cells' occupied tiles and give every cell the
    # SAME cover. Build the union once, then search cover thresholds over it —
    # every threshold renders identical pixels, so keep the smallest footprint
    # that fits the hardware (10-bit tile field, ≤128 OAMs/frame).
    union, gcols, grows, gw, gh = ncer_mod.union_tile_mask(cell_indexed, dims, min_opaque)
    chosen = None
    fewest_oams = None
    for boundary in (btchr.NCER_BOUNDARY_BYTES_DEFAULT, 256):
        st = max(1, boundary // btchr.BYTES_PER_TILE_8BPP)
        for threshold in _COVER_THRESHOLDS:
            oams, plans, total, fs_, moams = ncer_mod.masked_multicell_from_union(
                union, gcols, grows, gw, gh, dims,
                slot_tiles=st, is8bpp=True, pal=0,
                threshold=threshold, origin=oam_origin,
            )
            if fewest_oams is None or moams < fewest_oams:
                fewest_oams = moams
            max_slot = max((o.tile for cell in oams for o in cell), default=0)
            if max_slot > 0x3FF or moams > 128:  # 10-bit tile field / OAM limit
                continue
            if chosen is None or fs_ < chosen[5]:
                chosen = (boundary, st, oams, plans, total, fs_, moams)
        if chosen is not None:
            break  # a valid cover at this (smaller) boundary always wins
    if chosen is None:
        if fewest_oams is not None and fewest_oams > 128:
            raise ValueError(
                f"A frame needs {fewest_oams} OAM sprites even at the coarsest "
                "cover, over the 128-per-frame hardware limit. Simplify the "
                "silhouette (fewer isolated bits) or shrink the sprite."
            )
        max_tiles_cell = (0x400 // len(dims)) * 4  # slots/cell × 4 tiles/slot
        raise ValueError(
            "Sprite too large: even after skipping transparent background the "
            f"{len(dims)} cells overflow the 1024-slot OAM tile field. Keep each "
            f"cell's *opaque* content under ~{max_tiles_cell} tiles (about "
            f"{int(max_tiles_cell ** 0.5) * 8}px square) and re-import."
        )
    boundary_bytes, _st, per_cell_oams, per_cell_plan, total_tiles, fs, _worst_oams = chosen
    if oam_origin is not None:
        # Re-covered at the source sprite's own coords: confirm every OAM
        # position still fits the signed OAM fields (x −256..255, y −128..127).
        # A tall sprite whose lowest opaque tiles are covered by small OBJs can
        # push an OAM top past y=127 — bail with a clear message rather than
        # silently wrap the sprite.
        bad = next(
            (o for cell in per_cell_oams for o in cell
             if not (-256 <= o.x <= 255 and -128 <= o.y <= 127)),
            None,
        )
        if bad is not None:
            raise ValueError(
                "can't re-cover within the OAM position range (an OAM landed "
                f"at x={bad.x}, y={bad.y}; y must be −128..127). This sprite's "
                "content reaches too far for an occupied-only re-lay — its "
                "original layout is fine, leave it as-is."
            )
    return _assemble_from_layout(
        boundary_bytes, per_cell_oams, per_cell_plan, total_tiles, fs,
        dims, cell_indexed, tpl, template_entries, palette, source_digimon_id,
    )


def build_from_cells_joint(
    cell_indexed: List[bytes],
    dims: List[Tuple[int, int]],
    palette: List[Tuple[int, int, int]],
    template_entries: List[bytes],
    *,
    source_digimon_id: int = 0,
    oam_origin: Optional[Tuple[int, int]] = None,
) -> BtchrSprite:
    """Like :func:`build_from_cells`, but the cells SHARE one shape-sequence
    while each POSITIONS the blocks itself (`ncer.masked_multicell_joint`).
    Engine-safe — this is the Tsumemon structure (same OAM count/shapes/tile-
    order, per-cell positions) — and ``fs`` approaches the biggest single frame
    instead of the union of all frames. Raises if the sprite can't be jointly
    covered within the OAM tile field / 128-OAM / signed-position limits."""
    from . import ncer as ncer_mod

    if len(cell_indexed) != len(dims):
        raise ValueError("cell_indexed and dims length mismatch")
    tpl = [sprite.decompress_rle30(template_entries[i]) for i in range(ENTRY_COUNT)]
    tpl_parsed = ncer_mod.parse_ncer(tpl[3])
    if len(dims) != len(tpl_parsed.cells):
        raise ValueError(
            f"import provides {len(dims)} cell image(s) but the sprite has "
            f"{len(tpl_parsed.cells)} cells — supply exactly one image per cell"
        )
    for w, h in dims:
        if w <= 0 or h <= 0 or w % 8 or h % 8:
            raise ValueError(
                f"cell {w}×{h}px: dimensions must be positive multiples of 8"
            )

    chosen = None
    for boundary in (btchr.NCER_BOUNDARY_BYTES_DEFAULT, 256):
        st = max(1, boundary // btchr.BYTES_PER_TILE_8BPP)
        res = ncer_mod.masked_multicell_joint(
            cell_indexed, dims, slot_tiles=st, is8bpp=True, pal=0, origin=oam_origin,
        )
        if res is None:
            continue
        oams, plans, total, fs_, moams = res
        max_slot = max((o.tile for cell in oams for o in cell), default=0)
        if max_slot > 0x3FF or moams > 128:
            continue
        chosen = (boundary, st, oams, plans, total, fs_, moams)
        break
    if chosen is None:
        raise ValueError(
            "joint cover: this sprite can't be re-laid as a shared shape-"
            "sequence within the OAM limits (tile field / 128 OAMs / position "
            "range). Its original layout is fine, leave it as-is."
        )
    boundary_bytes, _st, per_cell_oams, per_cell_plan, total_tiles, fs, _moams = chosen
    bad = next(
        (o for cell in per_cell_oams for o in cell
         if not (-256 <= o.x <= 255 and -128 <= o.y <= 127)),
        None,
    )
    if bad is not None:
        raise ValueError(
            f"joint cover: an OAM landed at x={bad.x}, y={bad.y} (outside the "
            "signed OAM position range) — leave this sprite as-is."
        )
    return _assemble_from_layout(
        boundary_bytes, per_cell_oams, per_cell_plan, total_tiles, fs,
        dims, cell_indexed, tpl, template_entries, palette, source_digimon_id,
    )


def _compress_with(group_entries, builder):
    """Decode a group's 5 cells and re-cover them with ``builder``
    (:func:`build_from_cells` = union, or :func:`build_from_cells_joint`).
    Returns ``(rebuilt, old_fs, new_fs)``; raises on empty/uncoverable."""
    from . import ncer as ncer_mod

    if len(group_entries) != ENTRY_COUNT:
        raise ValueError(f"expected {ENTRY_COUNT} entries, got {len(group_entries)}")
    tile_bytes, *_ = sprite.parse_ncgr(sprite.decompress_rle30(group_entries[1]))
    palettes, _ = sprite.parse_nclr(sprite.decompress_rle30(group_entries[2]))
    palette = palettes[0]
    parsed = ncer_mod.parse_ncer(sprite.decompress_rle30(group_entries[3]))
    cells = parsed.cells
    n_cells = len(cells)
    n_tiles = len(tile_bytes) // btchr.BYTES_PER_TILE_8BPP
    if n_cells == 0 or n_tiles == 0:
        raise ValueError("sprite has no cells/tiles to compress")
    old_fs = btchr.derived_footprint_scale(n_tiles, n_cells)

    # Decode with the source's OWN tile-slot boundary (128 for most BTCHR,
    # 256 for the ~19 boss sprites) — a wrong stride reads garbage tiles.
    src_boundary = parsed.boundary_bytes
    xo, yo, w, h = btchr.cells_union_canvas(cells)
    cell_indexed = [
        btchr.render_cell_indexed(c, tile_bytes, w, h, xo, yo, src_boundary)
        for c in cells
    ]
    dims = [(w, h)] * n_cells
    # Pin the re-covered OAMs to the sprite's OWN origin (not centred) so the
    # content keeps its authored position.
    rebuilt = builder(
        cell_indexed, dims, palette, list(group_entries), oam_origin=(xo, yo)
    )
    if rebuilt.source_tpf == 0:
        # An all-transparent group (e.g. an unused/placeholder slot) re-covers
        # to zero tiles — a 0-footprint sprite is degenerate, not a saving.
        raise ValueError(
            "this sprite is empty (all-transparent) — there's nothing to compress."
        )
    return rebuilt, old_fs, rebuilt.source_tpf


def rebuild_from_entries(
    current_entries: List[bytes],
    source_digimon_id: int,
    replacements: Optional[dict] = None,
) -> "BtchrSprite":
    """Re-derive a consistent :class:`BtchrSprite` from the 5 CURRENT (compressed)
    PAK entries, optionally swapping in decompressed component files.

    ``replacements`` maps an entry index (0 mini / 1 NCGR / 2 NCLR / 3 NCER /
    4 NANR) to its new *decompressed* bytes — the standard Nitro file an
    external tool (NitroPaint) reads/writes. Whatever's swapped, the sidecars
    are recomputed so the sprite stays loadable: the mini-header's
    footprint_scale + size-tier flag are re-derived from the resulting
    NCGR-tiles ÷ NCER-cells, the NCGR is forced to the NCER's tile boundary
    (the engine reads mapping mode from the NCER — they must agree), and
    ``btchrsize`` is the uncompressed sum of entries 1–4. Raises ValueError if
    the NCER has no cells."""
    from . import ncer as ncer_mod

    replacements = replacements or {}
    raw = [
        bytes(replacements[i]) if i in replacements
        else sprite.decompress_rle30(current_entries[i])
        for i in range(ENTRY_COUNT)
    ]
    mini, ncgr, nclr, ncer, nanr = raw

    parsed = ncer_mod.parse_ncer(ncer)
    n_cells = len(parsed.cells)
    if n_cells == 0:
        raise ValueError("NCER has no cells — can't rebuild the sprite.")
    boundary = parsed.boundary_bytes
    rahc = sprite.find_block(ncgr, b"RAHC")
    n_tiles = struct.unpack_from("<I", ncgr, rahc + 24)[0] // btchr.BYTES_PER_TILE_8BPP
    fs = btchr.derived_footprint_scale(n_tiles, n_cells)

    ncgr = sprite.set_ncgr_boundary(ncgr, boundary)  # NCGR must match NCER stride
    mini = bytearray(mini)
    struct.pack_into("<H", mini, 0, fs & 0xFFFF)
    struct.pack_into("<H", mini, 2, 0 if fs <= 256 else (1 if fs <= 356 else 2))

    btchrsize_value = len(ncgr) + len(nclr) + len(ncer) + len(nanr)
    entries = (
        sprite.compress_rle30(bytes(mini)),
        sprite.compress_rle30(ncgr),
        sprite.compress_rle30(nclr),
        sprite.compress_rle30(ncer),
        # Reuse the original compressed NANR when it wasn't swapped (avoids a
        # needless recompress); recompress only a freshly imported one.
        sprite.compress_rle30(nanr) if 4 in replacements
        else bytes(current_entries[4]),
    )
    return BtchrSprite(
        source_digimon_id=source_digimon_id,
        source_tpf=fs,
        btchrsize_value=btchrsize_value,
        entries=entries,
    )


def compress_existing(
    group_entries: List[bytes], min_opaque: int = 1
) -> Tuple[BtchrSprite, int, int]:
    """Re-cover an existing group's sprite with occupied-only **union** coverage
    — one shared layout for all cells (the safe default). ``min_opaque=1`` is
    lossless; higher trims faint edge tiles (see :func:`compress_existing_fit`).
    Returns ``(rebuilt, old_fs, new_fs)``; raises on empty/uncoverable."""
    from functools import partial
    return _compress_with(group_entries, partial(build_from_cells, min_opaque=min_opaque))


def _visible_pixel_count(entries: List[bytes]) -> int:
    """Opaque pixels across all cells — used to price a trim (drop vs. keep)."""
    from . import ncer as ncer_mod
    tile_bytes, *_ = sprite.parse_ncgr(sprite.decompress_rle30(entries[1]))
    palettes, _ = sprite.parse_nclr(sprite.decompress_rle30(entries[2]))
    parsed = ncer_mod.parse_ncer(sprite.decompress_rle30(entries[3]))
    total = 0
    for c in parsed.cells:
        buf, w, h = btchr.render_cell_rgba(
            c, tile_bytes, palettes[0], boundary_bytes=parsed.boundary_bytes
        )
        total += sum(1 for i in range(w * h) if buf[i * 4 + 3])
    return total


def compress_existing_fit(group_entries: List[bytes], target: int = 512):
    """Union-compress, raising the faint-edge trim threshold just enough to land
    ``fs <= target``. Lossless first (``min_opaque=1``); only escalates if that
    misses. Returns ``(spr, old_fs, new_fs, min_opaque, pixels_dropped)``, or
    ``None`` if even an aggressive trim can't reach ``target``. ``min_opaque==1``
    and ``pixels_dropped==0`` means it fit losslessly."""
    spr, old_fs, new_fs = compress_existing(group_entries)  # lossless
    if new_fs <= target:
        return spr, old_fs, new_fs, 1, 0
    base_px = _visible_pixel_count(group_entries)
    for mo in range(2, 33):
        try:
            cand, _o, nf = compress_existing(group_entries, min_opaque=mo)
        except ValueError:
            return None  # trimmed to empty
        if nf <= target:
            dropped = base_px - _visible_pixel_count(list(cand.entries))
            return cand, old_fs, nf, mo, dropped
    return None


def compress_existing_joint(group_entries: List[bytes]) -> Tuple[BtchrSprite, int, int]:
    """SHELVED (not wired to UI — garbles the gallery, see ncer.py). Joint
    shared-shape cover; kept for tests/reference. Returns
    ``(rebuilt, old_fs, new_fs)``."""
    return _compress_with(group_entries, build_from_cells_joint)


def apply(
    pak_obj: pak.PakFile,
    chrsize_buf: bytearray,
    btchrsize_buf: bytearray,
    target_group: int,
    spr: BtchrSprite,
) -> None:
    """Apply a parsed .btchrspr onto a target group in-place.

    - Replaces the 5 BTCHR.PAK entries at ``target_group``'s base.
    - Updates chrsize.bin's tpf field for ``target_group`` (preserves
      the slot's existing digimon_id — the slot keeps its identity).
    - Overwrites btchrsize.bin's u32 for ``target_group``.

    Caller is responsible for splicing ``pak_obj`` and writing the two
    size buffers back to the ROM.
    """
    base = target_group * btchr.GROUP_SIZE
    for i in range(ENTRY_COUNT):
        pak_obj.replace_entry(base + i, spr.entries[i])

    # Preserve the slot's vanilla digimon_id; only bump tpf.
    cur_id, _ = btchr.parse_chrsize(bytes(chrsize_buf))[target_group]
    new_word = (cur_id & 0xFFFF) | ((spr.source_tpf & 0xFFFF) << 16)
    struct.pack_into("<I", chrsize_buf, target_group * 4, new_word)

    struct.pack_into(
        "<I", btchrsize_buf, target_group * 4, spr.btchrsize_value
    )


def serialize_from_session_state(
    pak_obj: pak.PakFile,
    chrsize_buf: bytes,
    btchrsize_buf: bytes,
    group: int,
) -> bytes:
    """Convenience: read the three live sources and pack them into a
    .btchrspr. Used by the editor's Export action."""
    digimon_id, tpf = btchr.parse_chrsize(chrsize_buf)[group]
    btchrsize_value = struct.unpack_from("<I", btchrsize_buf, group * 4)[0]
    return serialize(
        pak_obj, group,
        source_digimon_id=digimon_id,
        source_tpf=tpf,
        btchrsize_value=btchrsize_value,
    )
