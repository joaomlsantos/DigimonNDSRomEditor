"""Diagnostic probe: ONLY bump chrsize.tpf and btchrsize. Don't touch
NCGR or NCER OAMs. Goal: see if the engine's per-cell upload uses
tpf from chrsize or has hardcoded behaviour.

If the sprite still looks vanilla in DeSmuME, the engine doesn't care
about chrsize.tpf for upload offsets — meaning my full probe's bug is
elsewhere.

If the sprite *breaks* in some way, the engine is sensitive to chrsize
in ways we need to understand.
"""
from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from digimon_core import btchr, fat, fnt, pak  # noqa


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rom", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--group", type=int, required=True)
    ap.add_argument("--scale", type=int, default=2)
    args = ap.parse_args()

    rom = bytearray(Path(args.rom).read_bytes())
    ft = fnt.FileTable.from_rom(rom)
    chr_start, chr_end = ft.resolve("DAT/BTCHR/CHRSIZE.BIN")
    bsz_start, bsz_end = ft.resolve("DAT/BTCHR/BTCHRSIZE.BIN")

    chrsize_raw = bytearray(rom[chr_start:chr_end])
    btchrsize_raw = bytearray(rom[bsz_start:bsz_end])

    digimon_id, old_tpf = btchr.parse_chrsize(bytes(chrsize_raw))[args.group]
    new_tpf = old_tpf * args.scale
    print(f"group {args.group}: tpf {old_tpf} -> {new_tpf}")

    new_word = (digimon_id & 0xFFFF) | ((new_tpf & 0xFFFF) << 16)
    struct.pack_into("<I", chrsize_raw, args.group * 4, new_word)

    # Bump btchrsize by the extra room for new tiles (assuming we'd
    # expand NCGR too, even though we're not). This is to give the
    # engine enough VRAM, in case it pre-allocates based on this.
    old_sum = struct.unpack_from("<I", btchrsize_raw, args.group * 4)[0]
    new_sum = old_sum + 5 * (new_tpf - old_tpf) * 64  # account for hypothetical NCGR growth
    struct.pack_into("<I", btchrsize_raw, args.group * 4, new_sum)
    print(f"btchrsize[{args.group}]: {old_sum} -> {new_sum}")

    splices = [
        (chr_start, chr_end, bytes(chrsize_raw), "CHRSIZE"),
        (bsz_start, bsz_end, bytes(btchrsize_raw), "BTCHRSIZE"),
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
