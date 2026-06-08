"""Port one digimon's full sprite assets (entry 0-4) into another's
slot. Bumps target's chrsize.tpf + btchrsize to match source. Keeps
target's digimon_id by default (test whether the slot's other-system
identity matters).

If target slot now renders the source digimon's sprite correctly, the
port-via-asset-copy model works as a userspace operation.
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
    ap.add_argument("--target", type=int, required=True,
                    help="target group (will be overwritten)")
    ap.add_argument("--source", type=int, required=True,
                    help="source group (sprite to port in)")
    ap.add_argument("--copy-digimon-id", action="store_true",
                    help="overwrite target's digimon_id with source's "
                         "(default: keep target's id)")
    args = ap.parse_args()

    rom = bytearray(Path(args.rom).read_bytes())
    ft = fnt.FileTable.from_rom(rom)
    pak_start, pak_end = ft.resolve("DAT/BTCHR.PAK")
    chr_start, chr_end = ft.resolve("DAT/BTCHR/CHRSIZE.BIN")
    bsz_start, bsz_end = ft.resolve("DAT/BTCHR/BTCHRSIZE.BIN")

    pak_obj = pak.PakFile(bytes(rom[pak_start:pak_end]))
    chrsize_raw = bytearray(rom[chr_start:chr_end])
    btchrsize_raw = bytearray(rom[bsz_start:bsz_end])

    chr_entries = btchr.parse_chrsize(bytes(chrsize_raw))
    tgt_id, tgt_tpf = chr_entries[args.target]
    src_id, src_tpf = chr_entries[args.source]
    src_bsz = struct.unpack_from("<I", btchrsize_raw, args.source * 4)[0]
    tgt_bsz = struct.unpack_from("<I", btchrsize_raw, args.target * 4)[0]
    print(f"target g{args.target}: digimon_id={tgt_id} tpf={tgt_tpf} btchrsize={tgt_bsz}")
    print(f"source g{args.source}: digimon_id={src_id} tpf={src_tpf} btchrsize={src_bsz}")

    src_base = args.source * btchr.GROUP_SIZE
    tgt_base = args.target * btchr.GROUP_SIZE
    for i in range(btchr.GROUP_SIZE):
        src_entry = bytes(pak_obj.entries[src_base + i])
        pak_obj.replace_entry(tgt_base + i, src_entry)
        print(f"  copied entry {i}: {len(src_entry)}B")
    new_pak_bytes = pak_obj.to_bytes()

    # chrsize: tpf from source. digimon_id from source iff --copy-digimon-id.
    new_id = src_id if args.copy_digimon_id else tgt_id
    new_word = (new_id & 0xFFFF) | ((src_tpf & 0xFFFF) << 16)
    struct.pack_into("<I", chrsize_raw, args.target * 4, new_word)
    print(f"  chrsize[g{args.target}]: id {tgt_id}->{new_id}, tpf {tgt_tpf}->{src_tpf}")

    # btchrsize: copy source's value
    struct.pack_into("<I", btchrsize_raw, args.target * 4, src_bsz)
    print(f"  btchrsize[g{args.target}]: {tgt_bsz} -> {src_bsz}")

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
