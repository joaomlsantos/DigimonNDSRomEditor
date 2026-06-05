"""NDS FNT + FAT path resolver.

Parses a raw NDS ROM byte buffer into a single ``path -> (start, end)`` map
covering every FAT-listed file (``MSG.PAK``, ``/dm/``, ``/en/``, ``/ec/``,
``/eq/``, ``/sk/``, ``SPR_*.PAK``, ...).

This is the foundation for §13 in PLAN.md: loaders that currently use
hardcoded ROM offsets switch to ``FileTable.resolve(path)``, so the editor
stays correct against ROMs whose FAT-listed files have been grown by either
its own save path (§12) or by a third-party patch (notably fan translations
that extend ``MSG.PAK``).

NDS header reference:
  0x40  u32   FNT offset
  0x44  u32   FNT size
  0x48  u32   FAT offset
  0x4C  u32   FAT size

FAT layout: 8 bytes per file = (u32 start, u32 end). File ID = entry index.

FNT layout:
  - Directory table at FNT base. One 8-byte entry per directory:
        u32   subtable offset (relative to FNT base)
        u16   first file ID in this directory
        u16   parent directory ID. Root entry stores *total directory count*
              in this slot instead of a parent ID.
  - Subtable per directory: stream of variable-length entries, terminated
    by a 0x00 type byte.
        type/len byte: high bit = subdir flag; low 7 bits = name length.
        directory entry -> name[N] + u16 subdir id (0xF000 | dir_index)
        file entry      -> name[N]; file id auto-increments from the
                           directory's ``first_file_id``.
"""
import struct
from typing import Dict, Iterator, Tuple


class FileTable:
    """Maps ROM-internal paths to ``(start, end)`` byte ranges.

    Lookups are case-insensitive (NDS filesystems are case-preserving but
    DWDD mixes ``MSG.PAK`` with ``/dm/``-style lowercase content, and
    callers shouldn't need to remember which is which). Stored keys are
    upper-cased with forward slashes and no leading slash.
    """

    def __init__(self, entries: Dict[str, Tuple[int, int]]):
        self._entries = entries

    @classmethod
    def from_rom(cls, rom: bytes) -> "FileTable":
        fnt_off  = struct.unpack_from("<I", rom, 0x40)[0]
        fat_off  = struct.unpack_from("<I", rom, 0x48)[0]
        fat_size = struct.unpack_from("<I", rom, 0x4C)[0]

        fat = _parse_fat(rom, fat_off, fat_size)
        entries = _walk_fnt(rom, fnt_off, fat)
        return cls(entries)

    def resolve(self, path: str) -> Tuple[int, int]:
        """Return ``(start, end)`` byte range for ``path``. Raises
        ``KeyError`` if no FAT entry matches."""
        return self._entries[_norm(path)]

    def slice(self, rom: bytes, path: str) -> bytes:
        start, end = self.resolve(path)
        return bytes(rom[start:end])

    def paths(self) -> Iterator[str]:
        return iter(self._entries)

    def __contains__(self, path: object) -> bool:
        if not isinstance(path, str):
            return False
        return _norm(path) in self._entries

    def __len__(self) -> int:
        return len(self._entries)


def _norm(path: str) -> str:
    return path.replace("\\", "/").lstrip("/").upper()


def _parse_fat(rom: bytes, off: int, size: int) -> Dict[int, Tuple[int, int]]:
    out: Dict[int, Tuple[int, int]] = {}
    for i in range(size // 8):
        start, end = struct.unpack_from("<II", rom, off + i * 8)
        out[i] = (start, end)
    return out


def _walk_fnt(rom: bytes, fnt_off: int, fat: Dict[int, Tuple[int, int]]) -> Dict[str, Tuple[int, int]]:
    # Root entry's "parent" slot holds the total directory count — useful as
    # an upper bound when walking, but we recurse from sub-ids directly.
    _root_sub, _root_first, total_dirs = struct.unpack_from("<IHH", rom, fnt_off)

    entries: Dict[str, Tuple[int, int]] = {}

    def walk(dir_id: int, prefix: str) -> None:
        idx = dir_id & 0x0FFF
        if idx >= total_dirs:
            raise ValueError(f"FNT subdir id 0x{dir_id:04X} out of range (total_dirs={total_dirs})")
        sub_off, first_file_id, _parent = struct.unpack_from("<IHH", rom, fnt_off + idx * 8)
        pos = fnt_off + sub_off
        next_file_id = first_file_id
        while True:
            tb = rom[pos]; pos += 1
            if tb == 0x00:
                break
            length = tb & 0x7F
            is_dir = (tb & 0x80) != 0
            name = rom[pos:pos + length].decode("ascii", errors="replace")
            pos += length
            if is_dir:
                sub_id = struct.unpack_from("<H", rom, pos)[0]
                pos += 2
                walk(sub_id, prefix + name + "/")
            else:
                rng = fat.get(next_file_id)
                if rng is None:
                    raise ValueError(f"FNT references file id {next_file_id} not present in FAT")
                entries[_norm(prefix + name)] = rng
                next_file_id += 1

    walk(0xF000, "")
    return entries
