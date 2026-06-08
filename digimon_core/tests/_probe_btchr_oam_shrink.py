"""Shrink each cell's primary OAM from 64x64 to 32x32 in NCER, leave
everything else vanilla. Discriminator for whether the engine reads OAM
shape/size from NCER.

Encoding (8bpp OBJ): a1 high 2 bits = size, a0 high 2 bits = shape.
  64x64 -> shape=0 size=3 -> a0 bits 14-15 = 0, a1 bits 14-15 = 3
  32x32 -> shape=0 size=2 -> a0 bits 14-15 = 0, a1 bits 14-15 = 2

Outcomes:
  - koromon visibly shrinks (32x32) -> engine reads OAM size; expansion
    path is multi-OAM cells
  - koromon unchanged at 64x64 -> OAM size also ignored; upload-count
    source is elsewhere (entry-0? code?)
  - crash -> NCER mutation broke something else
"""
from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from digimon_core import btchr, fat, fnt, pak, sprite


def _shrink_first_oam_size(ncer_raw: bytes, n_cells: int) -> bytes:
    cebk = sprite.find_block(ncer_raw, b"KBEC")
    bank_attr = struct.unpack_from("<H", ncer_raw, cebk + 10)[0]
    cell_data_off = struct.unpack_from("<I", ncer_raw, cebk + 12)[0]
    cell_size = 16 if (bank_attr & 1) else 8
    cells_base = cebk + 8 + cell_data_off
    oam_base = cells_base + n_cells * cell_size

    out = bytearray(ncer_raw)
    for ci in range(n_cells):
        cell_off = cells_base + ci * cell_size
        oam_off = struct.unpack_from("<I", ncer_raw, cell_off + 4)[0]
        a1_addr = oam_base + oam_off + 2
        a1 = struct.unpack_from("<H", out, a1_addr)[0]
        # clear top 2 bits, set to size=2 (32x32 with shape=0)
        a1_new = (a1 & 0x3FFF) | (2 << 14)
        struct.pack_into("<H", out, a1_addr, a1_new)
        print(f"  cell {ci}: OAM0 a1 0x{a1:04x} -> 0x{a1_new:04x}")
    return bytes(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rom", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--group", type=int, required=True)
    args = ap.parse_args()

    rom = bytearray(Path(args.rom).read_bytes())
    ft = fnt.FileTable.from_rom(rom)
    pak_start, pak_end = ft.resolve("DAT/BTCHR.PAK")

    pak_obj = pak.PakFile(bytes(rom[pak_start:pak_end]))
    base = args.group * btchr.GROUP_SIZE

    orig_ncer_raw = sprite.decompress_rle30(pak_obj.entries[base + 3])
    # determine n_cells
    cebk = sprite.find_block(orig_ncer_raw, b"KBEC")
    n_cells = struct.unpack_from("<H", orig_ncer_raw, cebk + 8)[0]
    print(f"group {args.group}: n_cells={n_cells}, shrinking each cell's OAM0 to 32x32")

    new_ncer = _shrink_first_oam_size(orig_ncer_raw, n_cells)
    pak_obj.replace_entry(base + 3, sprite.compress_rle30(new_ncer))
    new_pak_bytes = pak_obj.to_bytes()

    idx, _cs, ce = fat.find_container(bytes(rom), pak_start, pak_end)
    content_delta = len(new_pak_bytes) - (pak_end - pak_start)
    aligned_shift = fat.splice_range(rom, pak_start, pak_end, ce, new_pak_bytes)
    fat.resize_fat_entry(rom, idx, ce, content_delta, aligned_shift)
    print(f"  spliced BTCHR.PAK: delta={content_delta:+d}B aligned_shift={aligned_shift:+d}B")

    Path(args.out).write_bytes(bytes(rom))
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
