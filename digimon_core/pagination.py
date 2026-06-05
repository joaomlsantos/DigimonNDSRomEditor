"""Internal-format parsers for FAT-listed DWDD data files.

Covers the formats documented in PLAN.md §13.3:

  - **Page-offset header** (``DAT/dm/``, ``DAT/en/``, ``DAT/eq/``,
    ``DAT/sk/``) — ``parse_page_header`` / ``iter_pages`` / ``page_bytes``.
    File starts with a self-describing ``u32`` LE offset array; header byte
    length = value of entry 0, so ``page_count = first / 4``.

  - **Raw fixed-stride array** (``DAT/ec/I0XX.BIN``) — ``iter_fixed_records``.
    No header; the whole file is a flat array of fixed-size records (e.g.
    0x20-byte ``EncounterRewardTable`` entries in I0XX.BIN). The legacy
    ROM-absolute loader walked these as 0x400-aligned chunks terminated by
    a ``0xFFFFFFFF`` sentinel; the FNT-listed files already drop sentinel
    padding so a flat stride walk gives the same result.

The ``E0XX.BIN`` per-area record format and ``ENCTBL.BIN`` (§13.3.a/c) are
not parsed by this module — see PLAN.md §13.3 for status.
"""
import struct
from typing import Iterator, List, Tuple


def parse_page_header(file_bytes: bytes) -> List[int]:
    """Return the list of file-relative page offsets.

    Raises ``ValueError`` if the file is too short for a header or the
    first entry isn't a plausible header length (must be ≥ 4, a multiple
    of 4, and not run past EOF).
    """
    if len(file_bytes) < 4:
        raise ValueError("file too short for a page-offset header")
    first = struct.unpack_from("<I", file_bytes, 0)[0]
    if first < 4 or first % 4 != 0:
        raise ValueError(f"bad page-header first offset: 0x{first:X}")
    if first > len(file_bytes):
        raise ValueError(
            f"page-header runs past EOF: header=0x{first:X}, file_size=0x{len(file_bytes):X}"
        )
    count = first // 4
    return [struct.unpack_from("<I", file_bytes, i * 4)[0] for i in range(count)]


def iter_pages(file_bytes: bytes) -> Iterator[Tuple[int, int, int]]:
    """Yield ``(page_index, start_offset, length)`` for every page.

    Caller handles sentinel filtering — pages whose first record bytes
    decode to id ``0xFFFF`` represent unused slots in the legacy
    fixed-stride loader; the page parser surfaces them so the writer
    side can keep them in place on save.
    """
    offsets = parse_page_header(file_bytes)
    for i, off in enumerate(offsets):
        end = offsets[i + 1] if i + 1 < len(offsets) else len(file_bytes)
        yield i, off, end - off


def page_bytes(file_bytes: bytes, page_index: int) -> bytes:
    """Slice out a single page as bytes."""
    offsets = parse_page_header(file_bytes)
    if not 0 <= page_index < len(offsets):
        raise IndexError(f"page {page_index} out of range (count={len(offsets)})")
    start = offsets[page_index]
    end = offsets[page_index + 1] if page_index + 1 < len(offsets) else len(file_bytes)
    return bytes(file_bytes[start:end])


def iter_fixed_records(file_bytes: bytes, record_size: int) -> Iterator[Tuple[int, int]]:
    """Yield ``(record_index, file_offset)`` for every record in a flat
    fixed-stride file (§13.3.b). ``record_size`` must divide ``len(file_bytes)``
    evenly — otherwise the file has trailing partial data the caller should
    inspect explicitly rather than silently truncate."""
    if record_size <= 0:
        raise ValueError(f"record_size must be positive, got {record_size}")
    if len(file_bytes) % record_size != 0:
        raise ValueError(
            f"file size 0x{len(file_bytes):X} not a multiple of record size 0x{record_size:X}"
        )
    for i in range(len(file_bytes) // record_size):
        yield i, i * record_size
