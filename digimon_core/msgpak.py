"""MSG.PAK per-entry sub-format helpers.

Layered on top of :mod:`digimon_core.pak` (the generic DWDD pak directory).
Each MSG.PAK entry is:

    u32 sub_offsets[M]   (entry-relative offsets to FF-FF-terminated groups)
    M × <packed strings ending in FF FF>

M is derived from ``sub_offsets[0]`` (always == ``M*4``, the sub-header
size). Within a group, strings may be split by FE FF ([END]) markers; the
group itself ends at the first FF FF terminator.

This sub-format is unique to MSG.PAK — sprite PAKs (SPR_*.PAK) store their
own NCGR/NCLR/NCER/NANR file types inside the same outer directory but
without this sub-header. Keeping it in its own module so the sprite-side
code never has to import MSG.PAK-specific helpers.
"""
import struct
from typing import List, Tuple


def parse_entry_groups(entry: bytes) -> List[Tuple[int, int]]:
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


def rebuild_entry(group_payloads: List[bytes]) -> bytes:
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
