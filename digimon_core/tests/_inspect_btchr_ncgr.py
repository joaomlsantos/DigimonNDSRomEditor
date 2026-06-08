"""Dump RAHC fields for vanilla group 2 NCGR."""
from __future__ import annotations

import struct
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from digimon_core import btchr, fnt, pak, sprite

ROM_PATH = r"C:\Workspace\digimon_stuffs\rom_files\1420 - Digimon World - Dusk (US).nds"


def main(group: int = 2) -> int:
    rom = Path(ROM_PATH).read_bytes()
    ft = fnt.FileTable.from_rom(rom)
    s, e = ft.resolve("DAT/BTCHR.PAK")
    pak_obj = pak.PakFile(rom[s:e])

    ncgr_raw = sprite.decompress_rle30(
        pak_obj.entries[group * btchr.GROUP_SIZE + 1]
    )
    print(f"NCGR size: {len(ncgr_raw)}B")
    print(f"  magic: {ncgr_raw[:4]}")
    print(f"  file_size@8: {struct.unpack_from('<I', ncgr_raw, 8)[0]}")
    print(f"  n_blocks@0xE: {struct.unpack_from('<H', ncgr_raw, 0xE)[0]}")

    rahc = sprite.find_block(ncgr_raw, b"RAHC")
    print(f"\nRAHC at 0x{rahc:x}:")
    block_size = struct.unpack_from("<I", ncgr_raw, rahc + 4)[0]
    th = struct.unpack_from("<H", ncgr_raw, rahc + 8)[0]
    tw = struct.unpack_from("<H", ncgr_raw, rahc + 10)[0]
    bit_depth = struct.unpack_from("<I", ncgr_raw, rahc + 12)[0]
    f0x10 = struct.unpack_from("<I", ncgr_raw, rahc + 16)[0]
    f0x14 = struct.unpack_from("<I", ncgr_raw, rahc + 20)[0]
    data_size = struct.unpack_from("<I", ncgr_raw, rahc + 24)[0]
    data_off = struct.unpack_from("<I", ncgr_raw, rahc + 28)[0]

    print(f"  block_size: {block_size}")
    print(f"  tile_h (rahc+8): 0x{th:04x} ({th})")
    print(f"  tile_w (rahc+10): 0x{tw:04x} ({tw})")
    print(f"  bit_depth (rahc+12): {bit_depth} ({'8bpp' if bit_depth==4 else '4bpp' if bit_depth==3 else '?'})")
    print(f"  +0x10: 0x{f0x10:08x}")
    print(f"  mapping (rahc+0x14): 0x{f0x14:08x}")
    print(f"  data_size (rahc+0x18): {data_size}")
    print(f"  data_off (rahc+0x1C): 0x{data_off:x}")
    print(f"  header_end: 0x{rahc + 8 + data_off:x}")
    print(f"  computed n_tiles (8bpp): {data_size // 64}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(int(sys.argv[1]) if len(sys.argv) > 1 else 2))
