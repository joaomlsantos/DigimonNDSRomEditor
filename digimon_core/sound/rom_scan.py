"""Find SDAT sound archives embedded in an NDS ROM.

The sound editor imports donor BGMs from an SDAT. Games don't agree on where
they keep it — DWDD uses ``DAT/snd/sound_data.sdat``, PMD BRT uses
``sound.sbin`` — so this scans the ROM's FAT-listed files by the ``b"SDAT"``
magic at file offset 0 rather than by extension. That subsumes the ``.sbin``
case and any other odd extension for free.

FNT names are resolved best-effort for display only; the magic scan itself
depends only on the FAT (header 0x48), so a ROM with an unusual name table
still yields usable donor candidates.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Dict, List, Optional

SDAT_MAGIC = b"SDAT"


@dataclass(frozen=True)
class SdatFile:
    """One SDAT archive located inside an NDS ROM."""
    file_id: int
    path: Optional[str]   # FNT path if resolvable, else None
    start: int
    end: int

    @property
    def size(self) -> int:
        return self.end - self.start

    @property
    def label(self) -> str:
        return self.path if self.path else f"file #{self.file_id}"


def find_sdats(rom: bytes) -> List[SdatFile]:
    """Return every FAT-listed file that begins with the SDAT magic.

    Raises ``ValueError`` if ``rom`` doesn't look like an NDS ROM (no valid
    FAT in the header). An empty list means the ROM parsed but holds no SDAT
    (e.g. a game whose audio is streamed PCM/ADPCM rather than sequenced).
    """
    if len(rom) < 0x50:
        raise ValueError("Not an NDS ROM (too small for a header)")
    fat_off, fat_size = struct.unpack_from("<II", rom, 0x48)
    if fat_off == 0 or fat_size == 0 or fat_off + fat_size > len(rom):
        raise ValueError("ROM header has no valid FAT")

    names = _try_fnt_names(rom)
    out: List[SdatFile] = []
    for fid in range(fat_size // 8):
        start, end = struct.unpack_from("<II", rom, fat_off + fid * 8)
        if end <= start or end > len(rom) or end - start < 4:
            continue
        if rom[start:start + 4] == SDAT_MAGIC:
            out.append(SdatFile(fid, names.get(fid), start, end))
    return out


def _try_fnt_names(rom: bytes) -> Dict[int, str]:
    """Best-effort ``file_id -> path`` from the FNT.

    Returns ``{}`` on any malformation — names are cosmetic here, so a broken
    name table must not sink the magic scan. Guards against cyclic / repeated
    directory ids so a corrupt FNT can't spin forever.
    """
    try:
        fnt_off = struct.unpack_from("<I", rom, 0x40)[0]
        _root_sub, _root_first, total_dirs = struct.unpack_from("<IHH", rom, fnt_off)
        names: Dict[int, str] = {}
        seen: set = set()

        def walk(dir_id: int, prefix: str) -> None:
            idx = dir_id & 0x0FFF
            if idx in seen or idx >= total_dirs:
                return
            seen.add(idx)
            sub_off, first_file_id, _parent = struct.unpack_from(
                "<IHH", rom, fnt_off + idx * 8
            )
            pos = fnt_off + sub_off
            fid = first_file_id
            while True:
                tb = rom[pos]
                pos += 1
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
                    names[fid] = prefix + name
                    fid += 1

        walk(0xF000, "")
        return names
    except Exception:
        return {}
