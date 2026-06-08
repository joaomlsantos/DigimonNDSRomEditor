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
from typing import List, Tuple

from . import btchr, pak, sprite


MAGIC = b"BSPR"
FORMAT_VERSION = 1
HEADER_SIZE = 16
ENTRY_COUNT = btchr.GROUP_SIZE  # 5
# Warn threshold: 5 * max_non_boss_tpf (432) = 2160 tiles. Above this the
# port is likely a boss-class sprite and may break in multi-sprite screens.
TILE_COUNT_WARN_THRESHOLD = 2160


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
