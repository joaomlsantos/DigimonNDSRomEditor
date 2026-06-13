"""DWDD sprite codec — port of ``rom_files/ncgr_to_png.py``'s pure functions.

PIL is intentionally NOT a dependency here. ``render_rgba`` returns flat
RGBA bytes + dimensions; the Qt layer wraps that in a ``QImage`` and
``QImage.save`` handles PNG output.

Sprite PAK entries (``SPR_*.PAK``) store NDS BIOS RLE-0x30-compressed
payloads. Each compressed payload decompresses into a standard Nintendo
NCGR / NCLR / NCER / NANR file. The pak-directory layer
(:mod:`digimon_core.pak`) is compression-agnostic; sprite callers wrap
``replace_entry`` with :func:`compress_rle30`.

Module surface:

- :func:`decompress_rle30` / :func:`maybe_decompress` / :func:`compress_rle30`
- :func:`find_block` — generic NCGR/NCLR block locator
- :func:`parse_ncgr` / :func:`parse_nclr`
- :func:`unpack_pixels` / :func:`render_rgba`
- :func:`png_indices_to_ncgr_tiles` — encode tile bytes from raster indices
- :func:`build_ncgr_from_template` — preserves RAHC+0x12 (project memory)
- :func:`build_nclr` / :func:`build_nclr_from_template`
- :func:`quantize_palette` — median-cut color reduction for PNG imports
"""
from __future__ import annotations

import struct
from typing import Dict, List, Optional, Tuple

import numpy as np


# ---- RLE-0x30 -------------------------------------------------------------

def decompress_rle30(data: bytes) -> bytes:
    """NDS BIOS RLE (type 0x30): header 0x30 + 24-bit out size, then
    flag-prefixed runs (MSB=1: ``(flag & 0x7F) + 3`` repeats of next byte)
    or literals (MSB=0: ``(flag & 0x7F) + 1`` raw bytes).
    """
    if not data or data[0] != 0x30:
        raise ValueError(f"not RLE-30 (first byte 0x{data[0] if data else -1:02X})")
    out_size = data[1] | (data[2] << 8) | (data[3] << 16)
    out = bytearray()
    i = 4
    while len(out) < out_size:
        flag = data[i]
        i += 1
        if flag & 0x80:
            length = (flag & 0x7F) + 3
            out += bytes([data[i]]) * length
            i += 1
        else:
            length = (flag & 0x7F) + 1
            out += data[i:i + length]
            i += length
    return bytes(out[:out_size])


def maybe_decompress(data: bytes) -> bytes:
    """Decompress if RLE-30, otherwise return ``data`` unchanged."""
    return decompress_rle30(data) if data and data[0] == 0x30 else data


def compress_rle30(data: bytes) -> bytes:
    """RLE-0x30 encoder. Greedy: emit a run when 3+ identical bytes occur,
    otherwise accumulate literals up to 128 bytes, breaking early when a
    run becomes available.
    """
    n = len(data)
    out = bytearray([0x30, n & 0xFF, (n >> 8) & 0xFF, (n >> 16) & 0xFF])
    i = 0
    while i < n:
        run = 1
        while i + run < n and data[i + run] == data[i] and run < 130:
            run += 1
        if run >= 3:
            out.append(0x80 | (run - 3))
            out.append(data[i])
            i += run
        else:
            j = i
            while j < n:
                if j + 2 < n and data[j] == data[j + 1] == data[j + 2]:
                    break
                j += 1
                if j - i >= 128:
                    break
            length = j - i
            out.append(length - 1)
            out += data[i:j]
            i = j
    return bytes(out)


# ---- NCGR / NCLR block walker --------------------------------------------

def find_block(data: bytes, magic: bytes, header_end: int = 0x10) -> int:
    """Find a named NDS-format block (RAHC, TTLP, ...). Returns the
    block's start offset (where its 4-byte magic begins).
    """
    i = header_end
    while i < len(data) - 8:
        if data[i:i + 4] == magic:
            return i
        size = struct.unpack_from("<I", data, i + 4)[0]
        if size < 8 or i + size > len(data):
            break
        i += size
    raise ValueError(f"block {magic!r} not found")


# ---- NCGR (tile data) -----------------------------------------------------

NcgrInfo = Tuple[bytes, int, int, int, bool]  # (tile_bytes, bit_depth, hint_w, hint_h, is_bitmap)


def parse_ncgr(raw: bytes) -> NcgrInfo:
    """Parse an NCGR (compressed or raw) and return its tile payload + metadata.

    ``bit_depth`` is the raw RAHC field: 3 == 4bpp, 4 == 8bpp.
    ``hint_w`` / ``hint_h`` are 0 when the RAHC stores 0xFFFF (untagged).
    ``is_bitmap`` reflects the RAHC mapping field's bit 7 (NCGR-internal
    bitmap mode — unrelated to the NCER's 1D/2D OBJ mapping).
    """
    raw = maybe_decompress(raw)
    if raw[:4] != b"RGCN":
        raise ValueError(f"not NCGR: {raw[:4]!r}")
    rahc = find_block(raw, b"RAHC")
    th = struct.unpack_from("<H", raw, rahc + 8)[0]
    tw = struct.unpack_from("<H", raw, rahc + 10)[0]
    bit_depth = struct.unpack_from("<I", raw, rahc + 12)[0]
    mapping = struct.unpack_from("<I", raw, rahc + 20)[0]
    data_size = struct.unpack_from("<I", raw, rahc + 24)[0]
    data_off = struct.unpack_from("<I", raw, rahc + 28)[0]
    is_bitmap = (mapping & 0x80) != 0
    payload = raw[rahc + 8 + data_off:rahc + 8 + data_off + data_size]
    hint_w = tw if tw != 0xFFFF else 0
    hint_h = th if th != 0xFFFF else 0
    return payload, bit_depth, hint_w, hint_h, is_bitmap


def ncgr_tile_count(raw: bytes) -> int:
    """Number of 8x8 tiles in an NCGR — derived from RAHC's data size and bpp."""
    tile_bytes, bit_depth, *_ = parse_ncgr(raw)
    bytes_per_tile = 32 if bit_depth == 3 else 64
    return len(tile_bytes) // bytes_per_tile


# ---- NCLR (palette) -------------------------------------------------------

Palette = List[Tuple[int, int, int]]


def parse_nclr(raw: bytes) -> Tuple[List[Palette], int]:
    """Returns ``(palettes, bit_depth)`` where each palette is a list of
    ``(r, g, b)`` triples in 0..255 range.
    """
    raw = maybe_decompress(raw)
    if raw[:4] != b"RLCN":
        raise ValueError(f"not NCLR: {raw[:4]!r}")
    pltt = find_block(raw, b"TTLP")
    block_size = struct.unpack_from("<I", raw, pltt + 4)[0]
    bit_depth = struct.unpack_from("<I", raw, pltt + 8)[0]
    data_off = struct.unpack_from("<I", raw, pltt + 0x14)[0]
    payload_start = pltt + 8 + data_off
    payload_end = pltt + block_size
    payload = raw[payload_start:payload_end]
    colors_per_palette = 16 if bit_depth == 3 else 256
    bytes_per_palette = colors_per_palette * 2
    n_palettes = max(1, len(payload) // bytes_per_palette)
    palettes: List[Palette] = []
    for p in range(n_palettes):
        pal: Palette = []
        base = p * bytes_per_palette
        for c in range(colors_per_palette):
            off = base + c * 2
            if off + 2 > len(payload):
                pal.append((0, 0, 0))
                continue
            v = payload[off] | (payload[off + 1] << 8)
            r = (v & 0x1F) * 255 // 31
            g = ((v >> 5) & 0x1F) * 255 // 31
            b = ((v >> 10) & 0x1F) * 255 // 31
            pal.append((r, g, b))
        palettes.append(pal)
    return palettes, bit_depth


# ---- Pixel-level decode ---------------------------------------------------

def unpack_pixels(tile_bytes: bytes, bit_depth: int) -> List[int]:
    """Tile bytes → flat list of palette indices, one per pixel.

    4bpp: low nibble is left pixel. 8bpp: one byte per pixel.
    """
    if bit_depth == 3:
        px: List[int] = []
        for b in tile_bytes:
            px.append(b & 0x0F)
            px.append((b >> 4) & 0x0F)
        return px
    if bit_depth == 4:
        return list(tile_bytes)
    raise ValueError(f"unsupported bit_depth={bit_depth}")


def render_rgba(
    tile_bytes: bytes,
    bit_depth: int,
    palette: Palette,
    width_tiles: int,
    is_bitmap: bool,
    transparent_index_0: bool = True,
) -> Tuple[bytes, int, int]:
    """Decode tile/bitmap bytes into a flat RGBA buffer.

    Returns ``(rgba_bytes, width_px, height_px)``. RGBA is row-major,
    8 bits per channel. The Qt layer wraps it as
    ``QImage(buf, w, h, w*4, QImage.Format_RGBA8888)``.
    """
    pixels = unpack_pixels(tile_bytes, bit_depth)
    pal_len = len(palette)
    # Vanilla DWDD occasionally pairs an 8bpp NCGR with 4bpp palette
    # banks the engine concatenates at runtime; callers should flatten
    # before calling, but if an index still lands past the end we treat
    # it as transparent rather than crashing on the lookup.
    if is_bitmap:
        # Linear bitmap: row-major across the whole image.
        w = width_tiles * 8
        h = (len(pixels) + w - 1) // w
        out = bytearray(w * h * 4)
        for i, idx in enumerate(pixels):
            if i // w >= h:
                break
            if idx >= pal_len:
                r, g, b, a = 0, 0, 0, 0
            else:
                r, g, b = palette[idx]
                a = 0 if (transparent_index_0 and idx == 0) else 255
            j = i * 4
            out[j:j + 4] = bytes((r, g, b, a))
        return bytes(out), w, h
    # Tile mode: 8x8 tiles wrap every width_tiles.
    n_tiles = len(pixels) // 64
    rows = (n_tiles + width_tiles - 1) // width_tiles
    w = width_tiles * 8
    h = rows * 8
    out = bytearray(w * h * 4)
    for t in range(n_tiles):
        tx = (t % width_tiles) * 8
        ty = (t // width_tiles) * 8
        base = t * 64
        for py in range(8):
            row = ty + py
            for pxn in range(8):
                idx = pixels[base + py * 8 + pxn]
                if idx >= pal_len:
                    r, g, b, a = 0, 0, 0, 0
                else:
                    r, g, b = palette[idx]
                    a = 0 if (transparent_index_0 and idx == 0) else 255
                j = (row * w + tx + pxn) * 4
                out[j:j + 4] = bytes((r, g, b, a))
    return bytes(out), w, h


# ---- Encode tiles ---------------------------------------------------------

def encode_tiles(indices: List[int], w: int, h: int, bpp4: bool) -> bytes:
    """Walk a raster pixel grid in tile-major (8x8) order and pack into
    NCGR tile bytes. ``indices`` is a flat row-major list of palette
    indices, length ``w * h``.
    """
    tw, th = w // 8, h // 8
    out = bytearray()
    for ty in range(th):
        for tx in range(tw):
            x0, y0 = tx * 8, ty * 8
            if bpp4:
                for py in range(8):
                    row = y0 + py
                    for px in range(0, 8, 2):
                        lo = indices[row * w + x0 + px] & 0x0F
                        hi = indices[row * w + x0 + px + 1] & 0x0F
                        out.append((hi << 4) | lo)
            else:
                for py in range(8):
                    row = y0 + py
                    for px in range(8):
                        out.append(indices[row * w + x0 + px] & 0xFF)
    return bytes(out)


# ---- Build NCGR / NCLR ----------------------------------------------------

def build_ncgr_from_template(tile_bytes: bytes, template_raw: bytes) -> bytes:
    """Reuse a template NCGR's full header (NCGR + RAHC up to the tile data),
    swapping only the tile data and patching the block/file sizes.

    Preserves every byte of the original RAHC — notably RAHC+0x12 which is
    load-bearing per-sprite (see project memory
    ``project_ncgr_rahc_header_preserve``). Zeroing it causes tile-row
    shifts in-game without affecting colors.
    """
    raw = maybe_decompress(template_raw)
    if raw[:4] != b"RGCN":
        raise ValueError(f"template not NCGR: {raw[:4]!r}")
    rahc = find_block(raw, b"RAHC")
    data_off = struct.unpack_from("<I", raw, rahc + 28)[0]
    header_end = rahc + 8 + data_off
    out = bytearray(raw[:header_end])
    out += tile_bytes
    struct.pack_into("<I", out, 8, len(out))               # NCGR file size
    struct.pack_into("<I", out, rahc + 4, len(out) - rahc) # RAHC block size
    struct.pack_into("<I", out, rahc + 24, len(tile_bytes))# RAHC tile_data_size
    return bytes(out)


def build_nclr(palette: Palette, bpp4: bool, include_pcmp: bool = True) -> bytes:
    """Build an uncompressed NCLR with one palette bank. Optionally append
    an identity PCMP block (vanilla DWDD NCLRs ship one).
    """
    bit_depth = 3 if bpp4 else 4
    slots = 16 if bpp4 else 256
    pal = list(palette)
    while len(pal) < slots:
        pal.append((0, 0, 0))
    pal = pal[:slots]
    pal_bytes = bytearray()
    for r, g, b in pal:
        r5 = (r * 31 + 127) // 255
        g5 = (g * 31 + 127) // 255
        b5 = (b * 31 + 127) // 255
        pal_bytes += struct.pack("<H", r5 | (g5 << 5) | (b5 << 10))
    pltt = bytearray()
    pltt += b"TTLP"
    pltt += b"\x00\x00\x00\x00"                     # block_size placeholder
    pltt += struct.pack("<I", bit_depth)
    pltt += b"\x00\x00\x00\x00"                     # padding
    pltt += struct.pack("<I", len(pal_bytes))
    pltt += struct.pack("<I", 0x10)
    pltt += pal_bytes
    struct.pack_into("<I", pltt, 4, len(pltt))
    blocks = bytearray(pltt)
    n_blocks = 1
    if include_pcmp:
        pcmp = bytearray()
        pcmp += b"PMCP"
        pcmp += b"\x00\x00\x00\x00"
        pcmp += struct.pack("<H", 1)
        pcmp += struct.pack("<H", 0xBEEF)
        pcmp += struct.pack("<I", 8)
        pcmp += struct.pack("<H", 0)
        struct.pack_into("<I", pcmp, 4, len(pcmp))
        blocks += pcmp
        n_blocks = 2
    header = bytearray()
    header += b"RLCN"
    header += struct.pack("<HH", 0xFEFF, 0x0100)
    header += struct.pack("<I", 0x10 + len(blocks))
    header += struct.pack("<HH", 0x10, n_blocks)
    return bytes(header + blocks)


def build_nclr_from_template(
    template_raw: bytes, bank_replacements: Dict[int, Palette],
) -> bytes:
    """Overwrite specific palette banks in a template NCLR, preserving
    everything else (other banks, PCMP, headers, padding).

    Used by the editor's "Replace from PNG (new palette)" flow when the
    sprite is 4bpp — only the currently-displayed bank gets rebuilt;
    sibling banks used by other animations stay byte-identical so they
    keep rendering as before.

    ``bank_replacements`` maps bank index → new colors (length up to
    16 or 256 depending on bit depth; short banks are zero-padded).
    """
    raw = maybe_decompress(template_raw)
    if raw[:4] != b"RLCN":
        raise ValueError(f"template not NCLR: {raw[:4]!r}")
    pltt = find_block(raw, b"TTLP")
    bit_depth = struct.unpack_from("<I", raw, pltt + 8)[0]
    block_size = struct.unpack_from("<I", raw, pltt + 4)[0]
    data_off = struct.unpack_from("<I", raw, pltt + 0x14)[0]
    payload_start = pltt + 8 + data_off
    payload_end = pltt + block_size
    colors_per_bank = 16 if bit_depth == 3 else 256
    bytes_per_bank = colors_per_bank * 2
    n_banks = max(1, (payload_end - payload_start) // bytes_per_bank)

    out = bytearray(raw)
    for bank_idx, colors in bank_replacements.items():
        if not 0 <= bank_idx < n_banks:
            raise ValueError(
                f"bank_idx={bank_idx} out of range (template has {n_banks} banks)"
            )
        pal = list(colors)[:colors_per_bank]
        while len(pal) < colors_per_bank:
            pal.append((0, 0, 0))
        bank_off = payload_start + bank_idx * bytes_per_bank
        for c, (r, g, b) in enumerate(pal):
            r5 = (r * 31 + 127) // 255
            g5 = (g * 31 + 127) // 255
            b5 = (b * 31 + 127) // 255
            struct.pack_into(
                "<H", out, bank_off + c * 2, r5 | (g5 << 5) | (b5 << 10)
            )
    return bytes(out)


def quantize_palette(
    colors, k: int,
) -> List[Tuple[int, int, int]]:
    """Median-cut reduction of ``colors`` to at most ``k`` representatives.

    If the input has ≤ ``k`` distinct colors, returns them sorted for
    determinism. Otherwise repeatedly splits the box with the longest
    single-axis range, dividing it at the median along that axis; each
    final box collapses to its component-wise mean.

    Caller is expected to handle the reserved transparent slot separately
    — this function only sees opaque colors and only emits ``k`` slots.

    ``colors`` may be a list of ``(r, g, b)`` tuples or a numpy ndarray
    of shape ``(N, 3)``. The ndarray path skips the Python-tuple round-trip
    for the hot multi-bank import callers.
    """
    if k <= 0:
        return []
    if isinstance(colors, np.ndarray):
        if colors.size == 0:
            return []
        arr = colors.astype(np.int32, copy=False)
    else:
        if not colors:
            return []
        arr = np.asarray(colors, dtype=np.int32)
    if arr.ndim != 2 or arr.shape[1] != 3:
        raise ValueError(
            f"quantize_palette expected (N, 3) colors, got shape {arr.shape}"
        )

    unique_rows = np.unique(arr, axis=0)
    if len(unique_rows) <= k:
        return [(int(r), int(g), int(b)) for r, g, b in unique_rows]

    # Each box is an int32 ndarray of shape (M, 3). Track min/max/range so
    # we can pick the next box to split in O(B) instead of rescanning every
    # box's pixels every iteration.
    boxes: List[np.ndarray] = [arr]
    box_mins = [arr.min(axis=0)]
    box_maxs = [arr.max(axis=0)]
    box_max_ranges = [int((box_maxs[0] - box_mins[0]).max())]

    while len(boxes) < k:
        # Original Python version picks the (box, axis) pair with the
        # largest single-axis range, iterating outer→inner and tiebreaking
        # by strict ``>`` (i.e. first encountered wins). The equivalent on
        # the cached per-box max-range list is the first argmax.
        best_i = -1
        best_range = -1
        for i, r in enumerate(box_max_ranges):
            if len(boxes[i]) < 2:
                continue
            if r > best_range:
                best_range = r
                best_i = i
        if best_i < 0 or best_range <= 0:
            break

        box = boxes[best_i]
        ranges = box_maxs[best_i] - box_mins[best_i]
        axis = int(ranges.argmax())
        # Stable sort matches Python ``list.sort``'s tiebreaking semantics
        # so the split is bit-identical to the original on tied pivots.
        order = np.argsort(box[:, axis], kind="stable")
        sorted_box = box[order]
        mid = len(box) // 2
        lower = sorted_box[:mid]
        upper = sorted_box[mid:]

        boxes[best_i] = lower
        box_mins[best_i] = lower.min(axis=0)
        box_maxs[best_i] = lower.max(axis=0)
        box_max_ranges[best_i] = int((box_maxs[best_i] - box_mins[best_i]).max())

        boxes.insert(best_i + 1, upper)
        upper_min = upper.min(axis=0)
        upper_max = upper.max(axis=0)
        box_mins.insert(best_i + 1, upper_min)
        box_maxs.insert(best_i + 1, upper_max)
        box_max_ranges.insert(best_i + 1, int((upper_max - upper_min).max()))

    out: List[Tuple[int, int, int]] = []
    for box in boxes:
        if len(box) == 0:
            continue
        n = len(box)
        s = box.sum(axis=0)
        # Match the original integer-divided per-channel mean.
        out.append((int(s[0]) // n, int(s[1]) // n, int(s[2]) // n))
    return out
