"""True per-cell expansion: ncgronly layout (cells at new stride) +
entry-0 stride bump. If this renders all 5 cells correctly, we've
confirmed the expansion model: entry-0 byte 5 = stride, NCGR cells at
that stride, no NCER change needed.

Optional --paint: paint the second half of each cell's tile region pink
so we can visually confirm the expanded region IS being read.
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rom", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--group", type=int, required=True)
    ap.add_argument("--scale", type=int, default=2)
    ap.add_argument("--paint", action="store_true",
                    help="paint the expanded portion of each cell pink")
    args = ap.parse_args()

    rom = bytearray(Path(args.rom).read_bytes())
    ft = fnt.FileTable.from_rom(rom)
    pak_start, pak_end = ft.resolve("DAT/BTCHR.PAK")
    chr_start, chr_end = ft.resolve("DAT/BTCHR/CHRSIZE.BIN")
    bsz_start, bsz_end = ft.resolve("DAT/BTCHR/BTCHRSIZE.BIN")

    pak_obj = pak.PakFile(bytes(rom[pak_start:pak_end]))
    chrsize_raw = bytearray(rom[chr_start:chr_end])
    btchrsize_raw = bytearray(rom[bsz_start:bsz_end])

    digimon_id, old_tpf = btchr.parse_chrsize(bytes(chrsize_raw))[args.group]
    new_tpf = old_tpf * args.scale
    bpt = btchr.BYTES_PER_TILE_8BPP
    base = args.group * btchr.GROUP_SIZE
    print(f"group {args.group}: tpf {old_tpf} -> {new_tpf}")

    # Build NCGR: cell K vanilla data at tile k*new_tpf, growth region after.
    d = btchr.decode_digimon(pak_obj, args.group, digimon_id=digimon_id)
    new_tiles = bytearray(5 * new_tpf * bpt)
    for k in range(5):
        src = k * old_tpf * bpt
        dst = k * new_tpf * bpt
        new_tiles[dst:dst + old_tpf * bpt] = d.tile_bytes[src:src + old_tpf * bpt]
        if args.paint:
            # Paint the growth region (tiles [k*new_tpf+old_tpf .. (k+1)*new_tpf))
            # palette index 1 so the expansion is visible IF the engine reads it.
            paint_start = dst + old_tpf * bpt
            paint_end = dst + new_tpf * bpt
            for off in range(paint_start, paint_end):
                new_tiles[off] = 1

    orig_ncgr_raw = sprite.decompress_rle30(pak_obj.entries[base + 1])
    new_ncgr = sprite.build_ncgr_from_template(bytes(new_tiles), orig_ncgr_raw)
    pak_obj.replace_entry(base + 1, sprite.compress_rle30(new_ncgr))

    # Patch entry-0 byte 5 = new stride (preserve byte 6 flag for small fmt).
    entry0 = bytearray(pak_obj.entries[base + 0])
    old_b5, old_b6 = entry0[5], entry0[6]
    if new_tpf <= 255 and (old_b6 & 0x80):
        entry0[5] = new_tpf
    else:
        struct.pack_into("<H", entry0, 5, new_tpf)
    pak_obj.replace_entry(base + 0, bytes(entry0))
    print(f"  entry0 b5b6 {old_b5:02x} {old_b6:02x} -> {entry0[5]:02x} {entry0[6]:02x}")

    new_pak_bytes = pak_obj.to_bytes()

    # Bump chrsize.tpf
    new_word = (digimon_id & 0xFFFF) | ((new_tpf & 0xFFFF) << 16)
    struct.pack_into("<I", chrsize_raw, args.group * 4, new_word)

    # Bump btchrsize to new uncompressed total
    def _uc(e: bytes) -> int:
        return len(sprite.decompress_rle30(e))
    sizes = [_uc(pak_obj.entries[base + i]) for i in (1, 2, 3, 4)]
    new_sum = sum(sizes)
    old_sum = struct.unpack_from("<I", btchrsize_raw, args.group * 4)[0]
    struct.pack_into("<I", btchrsize_raw, args.group * 4, new_sum)
    print(f"  btchrsize: {old_sum} -> {new_sum}")

    splices = [
        (pak_start, pak_end, new_pak_bytes, "BTCHR.PAK"),
        (chr_start, chr_end, bytes(chrsize_raw), "CHRSIZE.BIN"),
        (bsz_start, bsz_end, bytes(btchrsize_raw), "BTCHRSIZE.BIN"),
    ]
    splices.sort(key=lambda x: x[0], reverse=True)
    for start, end, new_bytes, label in splices:
        idx, _cs, ce = fat.find_container(bytes(rom), start, end)
        content_delta = len(new_bytes) - (end - start)
        aligned_shift = fat.splice_range(rom, start, end, ce, new_bytes)
        fat.resize_fat_entry(rom, idx, ce, content_delta, aligned_shift)
        print(f"  spliced {label}: delta={content_delta:+d}B aligned_shift={aligned_shift:+d}B")

    Path(args.out).write_bytes(bytes(rom))
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
