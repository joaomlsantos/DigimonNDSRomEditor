"""Bump entry-0 tpf (bytes 5-6) for one digimon, keep NCGR/NCER vanilla.
Also bump chrsize.tpf + btchrsize so VRAM budget allows the larger stride
(isolating the test to entry-0.tpf alone).

If the engine reads stride from entry-0.tpf: cells 1-4 will read garbage
past vanilla cell 0 -> sprite breaks.

If the sprite still looks normal: entry-0.tpf is not the stride field
either, and the dimension/stride source is elsewhere (probably code).
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
    base = args.group * btchr.GROUP_SIZE

    # Patch entry-0 tpf field. Two encodings observed:
    #   small (tpf<=255): byte 5 = tpf, byte 6 = flag (top bit set, e.g. 0x82)
    #   large (tpf>=256): bytes 5-6 = u16 LE tpf (byte 6 top bit clear)
    # If new_tpf still fits in a byte, keep byte 6 flag intact.
    entry0 = bytearray(pak_obj.entries[base + 0])
    old_b5 = entry0[5]
    old_b6 = entry0[6]
    if new_tpf <= 255 and (old_b6 & 0x80):
        entry0[5] = new_tpf
        # leave byte 6 untouched
    else:
        struct.pack_into("<H", entry0, 5, new_tpf)
    pak_obj.replace_entry(base + 0, bytes(entry0))
    new_pak_bytes = pak_obj.to_bytes()

    print(f"group {args.group}: entry0 b5b6 {old_b5:02x} {old_b6:02x} "
          f"-> {entry0[5]:02x} {entry0[6]:02x} (chrsize.tpf {old_tpf} -> {new_tpf}, NCGR vanilla)")

    # Bump chrsize.tpf
    new_word = (digimon_id & 0xFFFF) | ((new_tpf & 0xFFFF) << 16)
    struct.pack_into("<I", chrsize_raw, args.group * 4, new_word)

    # Bump btchrsize to account for hypothetical NCGR growth (give engine
    # enough VRAM budget for the new stride).
    bpt = btchr.BYTES_PER_TILE_8BPP
    old_sum = struct.unpack_from("<I", btchrsize_raw, args.group * 4)[0]
    new_sum = old_sum + 5 * (new_tpf - old_tpf) * bpt
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
