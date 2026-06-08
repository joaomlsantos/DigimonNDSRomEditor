"""Dump raw OAM tile slot for each cell of group N (any ROM)."""
from __future__ import annotations

import struct
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from digimon_core import btchr, fnt, pak, sprite


def main(rom_path: str, group: int = 2) -> int:
    rom = Path(rom_path).read_bytes()
    ft = fnt.FileTable.from_rom(rom)
    s, e = ft.resolve("DAT/BTCHR.PAK")
    pak_obj = pak.PakFile(rom[s:e])

    # Also pull chrsize
    cs, ce = ft.resolve("DAT/BTCHR/CHRSIZE.BIN")
    digimon_id, tpf = btchr.parse_chrsize(rom[cs:ce])[group]
    print(f"chrsize: digimon_id={digimon_id} tpf={tpf}")

    # Pull btchrsize
    bs, be = ft.resolve("DAT/BTCHR/BTCHRSIZE.BIN")
    bsize = struct.unpack_from("<I", rom[bs:be], group * 4)[0]
    print(f"btchrsize[{group}] = {bsize}")

    ncgr_raw = sprite.decompress_rle30(
        pak_obj.entries[group * btchr.GROUP_SIZE + 1]
    )
    rahc = sprite.find_block(ncgr_raw, b"RAHC")
    n_tiles = struct.unpack_from("<I", ncgr_raw, rahc + 24)[0] // 64
    print(f"NCGR n_tiles (8bpp): {n_tiles}")

    ncer_raw = sprite.decompress_rle30(
        pak_obj.entries[group * btchr.GROUP_SIZE + 3]
    )
    print(f"NCER size: {len(ncer_raw)}B")

    cebk = sprite.find_block(ncer_raw, b"KBEC")
    n_cells = struct.unpack_from("<H", ncer_raw, cebk + 8)[0]
    bank_attr = struct.unpack_from("<H", ncer_raw, cebk + 10)[0]
    cell_data_off = struct.unpack_from("<I", ncer_raw, cebk + 12)[0]
    mapping_type = struct.unpack_from("<I", ncer_raw, cebk + 16)[0]
    cell_size = 16 if (bank_attr & 1) else 8
    cells_base = cebk + 8 + cell_data_off
    oam_base = cells_base + n_cells * cell_size

    print(f"  n_cells={n_cells} bank_attr=0x{bank_attr:04x} "
          f"cell_size={cell_size} mapping_type=0x{mapping_type:08x}")

    for ci in range(n_cells):
        cell_off = cells_base + ci * cell_size
        n_oam = struct.unpack_from("<H", ncer_raw, cell_off)[0]
        oam_off = struct.unpack_from("<I", ncer_raw, cell_off + 4)[0]
        print(f"\ncell {ci}: n_oam={n_oam} oam_off={oam_off}")
        for oi in range(n_oam):
            base = oam_base + oam_off + oi * 6
            a0 = struct.unpack_from("<H", ncer_raw, base)[0]
            a1 = struct.unpack_from("<H", ncer_raw, base + 2)[0]
            a2 = struct.unpack_from("<H", ncer_raw, base + 4)[0]
            y = a0 & 0xFF
            x_raw = a1 & 0x1FF
            x = x_raw if x_raw < 0x100 else x_raw - 0x200
            shape = (a0 >> 14) & 0x3
            size = (a1 >> 14) & 0x3
            is8 = (a0 >> 13) & 1
            slot = a2 & 0x3FF
            print(f"  OAM {oi}: a0=0x{a0:04x} a1=0x{a1:04x} a2=0x{a2:04x} "
                  f"| pos=({x},{y}) shape={shape} size={size} 8bpp={is8} "
                  f"slot={slot} -> first_tile={slot*2}")
    return 0


if __name__ == "__main__":
    rom = sys.argv[1] if len(sys.argv) > 1 else r"C:\Workspace\digimon_stuffs\rom_files\1420 - Digimon World - Dusk (US).nds"
    grp = int(sys.argv[2]) if len(sys.argv) > 2 else 2
    raise SystemExit(main(rom, grp))
