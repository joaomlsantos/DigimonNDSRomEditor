"""FAT/header arithmetic for in-place ROM resizing (PLAN.md §12).

Pure byte-level helpers for growing or shrinking a sub-range inside a
FAT-listed file. Layout-agnostic — these primitives don't know the difference
between MSG.PAK, SPR_*.PAK, or an arbitrary file slot. The caller (e.g.
``RomSession.replace_pak_entry``) handles container-format updates first
(pak directories, etc.) and then asks ``fat`` to splice the new bytes in
and patch the FAT + NDS header to match.

NDS layout this module touches:

* ``header[0x48]`` (u32) FAT offset, ``header[0x4C]`` (u32) FAT size.
* Each FAT entry = 8 bytes: ``u32 start, u32 end`` (end exclusive).
* Header fields that store absolute ROM offsets and need shifting when data
  past a resized container moves: ``0x20`` ARM9, ``0x30`` ARM7, ``0x40``
  FNT, ``0x48`` FAT, ``0x50`` ARM9 overlay table, ``0x58`` ARM7 overlay
  table, ``0x68`` icon/banner. Verified by `rom_files/compact_test.py` to
  be exhaustive on DWDD; flag if any other hardcoded offset shows up.
* ``header[0x80]`` (u32) total used ROM size — always equals the max FAT
  entry end on vanilla Dusk + Dawn (verified empirically), so we recompute
  it from the post-shift FAT rather than tracking it via a delta.

NDS files are 0x200-aligned in the ROM image. Growth pads with 0xFF up to
the next 0x200 boundary so downstream files stay aligned; shrinkage only
reclaims whole 0x200 steps so we never break alignment of downstream files.
"""
import struct
from typing import List, Tuple


ALIGNMENT = 0x200

# (offset, label) for every NDS header field that stores an absolute ROM
# position past which a resize must propagate. Iterated by
# ``resize_fat_entry`` to shift each value if it sits at or beyond the
# resized container's old end. ``0x80`` is *not* listed here — it tracks
# total used size and is recomputed from the post-shift FAT.
_HEADER_OFFSET_FIELDS: Tuple[Tuple[int, str], ...] = (
    (0x20, "arm9_rom_off"),
    (0x30, "arm7_rom_off"),
    (0x40, "fnt_off"),
    (0x48, "fat_off"),
    (0x50, "arm9_ovl_off"),
    (0x58, "arm7_ovl_off"),
    (0x68, "icon_banner_off"),
)


def signed_align(delta: int, alignment: int = ALIGNMENT) -> int:
    """Round ``delta`` to a signed multiple of ``alignment``.

    Growth (``delta > 0``) rounds magnitude **up** to the next aligned step
    so the downstream FAT entries land on an aligned boundary after the
    splice. Shrinkage (``delta < 0``) rounds magnitude **down** (i.e. shrinks
    by *less*) so we never close more space than we can give back as whole
    0x200 blocks — keeping every downstream file aligned. ``delta == 0`` is
    a no-op.

    The choice of rounding direction is asymmetric on purpose: we err on
    the side of "leave more 0xFF gap" rather than risk misaligning a file
    the NDS loader expects to find at a 0x200 boundary.
    """
    if delta == 0 or alignment <= 0:
        return 0
    if delta > 0:
        return -(-delta // alignment) * alignment  # ceil
    return -((-delta) // alignment) * alignment    # -floor(|delta|)


def _read_fat_header(rom: bytes) -> Tuple[int, int]:
    fat_off = struct.unpack_from("<I", rom, 0x48)[0]
    fat_size = struct.unpack_from("<I", rom, 0x4C)[0]
    return fat_off, fat_size


def _iter_fat(rom: bytes) -> List[Tuple[int, int]]:
    fat_off, fat_size = _read_fat_header(rom)
    return [
        struct.unpack_from("<II", rom, fat_off + i * 8)
        for i in range(fat_size // 8)
    ]


def find_container(rom: bytes, start: int, end: int) -> Tuple[int, int, int]:
    """Find the FAT entry whose range contains ``[start, end)``.

    Returns ``(file_idx, container_start, container_end)``. Raises
    ``ValueError`` if no single FAT entry fully covers the range — useful
    for catching callers that accidentally hand us a byte range straddling
    two files (e.g. a stale offset against a grown ROM).
    """
    if end < start:
        raise ValueError(f"end 0x{end:X} precedes start 0x{start:X}")
    for idx, (cs, ce) in enumerate(_iter_fat(rom)):
        if cs <= start and end <= ce:
            return idx, cs, ce
    raise ValueError(
        f"no FAT entry contains [0x{start:08X}, 0x{end:08X})"
    )


def splice_range(
    rom: bytearray,
    target_start: int,
    target_end: int,
    container_end: int,
    new_content: bytes,
) -> int:
    """Replace ``rom[target_start:target_end]`` with ``new_content`` in place.

    The container's tail (``rom[target_end:container_end]``) is preserved
    immediately after ``new_content``, and 0xFF padding is appended so the
    downstream bytes start at an ``ALIGNMENT``-multiple offset relative to
    where they were. Returns the signed ``aligned_shift`` — the number of
    bytes the downstream region (``rom[container_end:]``) moved by. Pass
    that same value into ``resize_fat_entry`` so the FAT/header updates
    match the byte layout.

    Mutates ``rom`` in place and may change its length.
    """
    if not (target_start <= target_end <= container_end):
        raise ValueError(
            f"target_start 0x{target_start:X} target_end 0x{target_end:X} "
            f"container_end 0x{container_end:X} out of order"
        )
    if target_end > len(rom) or container_end > len(rom):
        raise ValueError("splice range extends past end of ROM")
    content_delta = len(new_content) - (target_end - target_start)
    aligned_shift = signed_align(content_delta, ALIGNMENT)
    # pad_count is always >= 0: for growth, fills new_content..next-aligned;
    # for shrink, fills the gap left by reclaiming the original target bytes.
    pad_count = aligned_shift - content_delta
    # Single splice over [target_start, container_end) keeps rom[:target_start]
    # and rom[container_end:] untouched (the downstream slice slides by the
    # natural Python list-assignment delta, which equals aligned_shift).
    preserved_tail = bytes(rom[target_end:container_end])
    rom[target_start:container_end] = (
        bytes(new_content) + preserved_tail + b"\xFF" * pad_count
    )
    return aligned_shift


def resize_fat_entry(
    rom: bytearray,
    container_idx: int,
    container_end_old: int,
    content_delta: int,
    aligned_shift: int,
) -> None:
    """Patch FAT entries + NDS header offsets to reflect a resize.

    ``container_idx`` is the FAT entry whose content changed by
    ``content_delta`` bytes (signed). ``container_end_old`` is that file's
    pre-splice end — the threshold for "downstream" entries that move by
    ``aligned_shift`` bytes (signed, what ``splice_range`` returned).

    Updates in place:
    * Container's own end: ``end += content_delta`` (start unchanged).
    * Every other FAT entry whose ``start >= container_end_old``: both
      start and end shift by ``aligned_shift``.
    * Each header offset field in ``_HEADER_OFFSET_FIELDS`` whose value is
      ``>= container_end_old``: shifted by ``aligned_shift``.
    * ``header[0x80]`` (total used size): recomputed as the max FAT end.

    Does **not** touch the ROM bytes — call ``splice_range`` first so the
    byte layout matches what the new FAT entries describe.
    """
    fat_off, fat_size = _read_fat_header(rom)
    n_entries = fat_size // 8

    cs, ce = struct.unpack_from("<II", rom, fat_off + container_idx * 8)
    struct.pack_into("<II", rom, fat_off + container_idx * 8, cs, ce + content_delta)

    if aligned_shift != 0:
        for i in range(n_entries):
            if i == container_idx:
                continue
            entry_off = fat_off + i * 8
            es, ee = struct.unpack_from("<II", rom, entry_off)
            if es >= container_end_old:
                struct.pack_into("<II", rom, entry_off, es + aligned_shift, ee + aligned_shift)

        for hoff, _label in _HEADER_OFFSET_FIELDS:
            val = struct.unpack_from("<I", rom, hoff)[0]
            if val >= container_end_old:
                struct.pack_into("<I", rom, hoff, val + aligned_shift)

    # Read FAT again — the container_idx's end has changed and downstream
    # entries may have shifted. header[0x80] tracks the post-shift max end.
    # On vanilla Dusk + Dawn header[0x80] == max(FAT.end) exactly; preserving
    # that invariant keeps the §12.3 trailing-FF trim well-defined (anything
    # past header[0x80] is reclaimable padding).
    new_used = 0
    new_fat_off, _ = _read_fat_header(rom)
    for i in range(n_entries):
        _, ee = struct.unpack_from("<II", rom, new_fat_off + i * 8)
        if ee > new_used:
            new_used = ee
    struct.pack_into("<I", rom, 0x80, new_used)
