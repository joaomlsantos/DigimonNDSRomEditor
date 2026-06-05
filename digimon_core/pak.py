"""DWDD pak format reader/writer (PLAN.md §11.2, §12.2).

A "pak" is the per-file container that DWDD uses inside the NDS FAT for
grouped resources (MSG.PAK strings, SPR_*.PAK sprite sheets, etc.). The
format is:

    u32 entry_count
    entry_count × (u32 file_offset, u32 size_with_flag)
    entry_count × <opaque entry bytes>

`file_offset` is relative to the pak file's start. The top bit of the
size word is a per-entry flag the engine reads (compression / type? —
never set, always set to 1 in observed paks); we preserve it byte-for-byte
on rebuild rather than trying to interpret it.

Entries are 4-byte aligned within the pak: the engine reads each entry's
`size` field literally (so the size value is the entry's real payload
size, possibly odd), but the next entry's `offset` is rounded up to the
next multiple of 4. Inter-entry padding is `0x00`. `to_bytes` reproduces
that alignment so a no-edit save is byte-identical to vanilla.

This module is layout-agnostic about what's *inside* an entry; MSG.PAK's
internal sub-header is parsed by :func:`parse_msgpak_entry_groups`, kept
separate so a future sprite/pak format only needs to drop in its own
sub-parser.
"""
import struct
from typing import List, Tuple


_SIZE_FLAG_MASK = 0x80000000
_SIZE_VALUE_MASK = 0x7FFFFFFF


class PakFile:
    """Parses a DWDD pak and lets the caller swap entries before rewriting.

    ``replace_entry(idx, new_bytes)`` is in-place — the original ``raw``
    snapshot stays untouched so callers can still read the original bytes
    of unmodified entries via :attr:`original_entry`.
    """

    def __init__(self, raw: bytes):
        self.raw = bytes(raw)
        self.count: int = struct.unpack_from("<I", self.raw, 0)[0]
        self._orig_offsets: List[int] = []
        self.flags: List[int] = []  # high bit of each size word, preserved
        self.entries: List[bytes] = []
        for i in range(self.count):
            o, sf = struct.unpack_from("<II", self.raw, 4 + i * 8)
            self._orig_offsets.append(o)
            self.flags.append(sf & _SIZE_FLAG_MASK)
            sz = sf & _SIZE_VALUE_MASK
            self.entries.append(self.raw[o:o + sz])

    @property
    def header_size(self) -> int:
        return 4 + self.count * 8

    def original_entry(self, idx: int) -> bytes:
        """Return entry ``idx``'s bytes as they were in the source pak —
        ignoring any prior ``replace_entry`` calls."""
        o = self._orig_offsets[idx]
        sf = struct.unpack_from("<II", self.raw, 4 + idx * 8)[1]
        return self.raw[o:o + (sf & _SIZE_VALUE_MASK)]

    def original_entry_range(self, idx: int) -> Tuple[int, int]:
        """Return (start, end) of entry ``idx`` within the source pak."""
        o = self._orig_offsets[idx]
        sf = struct.unpack_from("<II", self.raw, 4 + idx * 8)[1]
        return o, o + (sf & _SIZE_VALUE_MASK)

    def entry_index_at(self, file_offset: int) -> int:
        """Return the entry idx whose original range contains ``file_offset``.

        Uses the original directory (pre-``replace_entry``) so callers can
        translate ROM/source offsets into entry indices before mutating.
        Raises ``ValueError`` if ``file_offset`` falls outside every entry.
        """
        for i in range(self.count):
            o = self._orig_offsets[i]
            sf = struct.unpack_from("<II", self.raw, 4 + i * 8)[1]
            sz = sf & _SIZE_VALUE_MASK
            if o <= file_offset < o + sz:
                return i
        raise ValueError(f"file offset 0x{file_offset:x} not in any pak entry")

    def replace_entry(self, idx: int, new_bytes: bytes) -> None:
        self.entries[idx] = bytes(new_bytes)

    def to_bytes(self) -> bytes:
        """Serialise the pak with current entries.

        Offsets are recomputed (entries are packed back-to-back starting at
        ``header_size``); flags are preserved per entry. If no entries have
        been replaced, the result is byte-identical to ``raw``.
        """
        out = bytearray(self.header_size)
        struct.pack_into("<I", out, 0, self.count)
        cur = self.header_size
        for i, entry in enumerate(self.entries):
            struct.pack_into("<II", out, 4 + i * 8, cur, len(entry) | self.flags[i])
            out += entry
            cur += len(entry)
            # Pad to 4-byte alignment so the next entry's offset stays
            # 4-aligned. Last entry's trailing pad is skipped (no successor
            # to align for).
            if i != self.count - 1 and cur % 4 != 0:
                pad = 4 - (cur % 4)
                out += b"\x00" * pad
                cur += pad
        return bytes(out)


# --- MSG.PAK per-entry sub-format ------------------------------------------
#
# Each entry is:
#   u32 sub_offsets[M]   (entry-relative offsets to FF-FF-terminated groups)
#   M × <packed strings ending in FF FF>
#
# M is derived from sub_offsets[0] (always == M*4, the sub-header size).
# Within a group, strings may be split by FE FF ([END]) markers; the group
# itself ends at the first FF FF terminator.


def parse_msgpak_entry_groups(entry: bytes) -> List[Tuple[int, int]]:
    """Return [(group_start, group_end)] entry-relative ranges for each
    FF-FF-terminated group inside a MSG.PAK entry.

    The group count is read from the entry's sub-header (first u32 / 4).
    ``group_end`` is exclusive and equals the next group's start, with the
    last group's end equal to ``len(entry)``.
    """
    if len(entry) < 4:
        return []
    first = struct.unpack_from("<I", entry, 0)[0]
    if first % 4 != 0 or first == 0 or first > len(entry):
        raise ValueError(
            f"MSG.PAK entry sub-header malformed: first u32 = 0x{first:x}, "
            f"entry size = 0x{len(entry):x}"
        )
    m = first // 4
    starts = [struct.unpack_from("<I", entry, i * 4)[0] for i in range(m)]
    ends = starts[1:] + [len(entry)]
    return list(zip(starts, ends))


def rebuild_msgpak_entry(group_payloads: List[bytes]) -> bytes:
    """Pack ``group_payloads`` into a fresh MSG.PAK entry with sub-header.

    Each item is the raw byte content of one FF-FF-terminated group (i.e.
    the bytes between sub-header offsets, including any FE FF dividers and
    the terminating FF FF). The sub-header is rebuilt with the new offsets.

    Group count must match the source entry's group count; resizing the
    sub-header itself isn't supported here because no caller in §12 needs
    it — every supported edit changes payload bytes, not group counts.
    """
    m = len(group_payloads)
    header_size = m * 4
    out = bytearray(header_size)
    cur = header_size
    for i, payload in enumerate(group_payloads):
        struct.pack_into("<I", out, i * 4, cur)
        out += payload
        cur += len(payload)
    return bytes(out)
