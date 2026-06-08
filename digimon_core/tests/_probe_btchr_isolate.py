"""Variants of the BTCHR expansion probe — isolates which change
upsets the engine.

Modes:
  --mode chrsize : only bump chrsize.tpf + btchrsize (no NCGR/NCER changes)
  --mode oamonly : keep NCGR vanilla, shift OAMs, bump chrsize+btchrsize
  --mode ncgronly: expand NCGR (with zero pad), DON'T shift OAMs, bump chrsize+btchrsize

The full expansion (NCGR + OAM shift + chrsize + btchrsize) lives in
_probe_btchr_expand.py.
"""
from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path
from typing import List

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from digimon_core import btchr, fat, fnt, pak, sprite
from digimon_core.tests._probe_btchr_expand import _shift_ncer_oam_tiles


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rom", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--group", type=int, required=True)
    ap.add_argument("--scale", type=int, default=2)
    ap.add_argument(
        "--mode", required=True,
        choices=("chrsize", "oamonly", "ncgronly", "fullnobsz", "tailpad"),
    )
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
    print(f"mode={args.mode} group={args.group} digimon_id={digimon_id} "
          f"tpf {old_tpf} -> {new_tpf}")

    bpt = btchr.BYTES_PER_TILE_8BPP
    base = args.group * btchr.GROUP_SIZE

    # In all modes: bump chrsize.tpf
    new_word = (digimon_id & 0xFFFF) | ((new_tpf & 0xFFFF) << 16)
    struct.pack_into("<I", chrsize_raw, args.group * 4, new_word)

    new_pak_bytes = None  # only set if we touch the PAK

    if args.mode == "chrsize":
        # Just bump btchrsize by the hypothetical NCGR growth. No PAK
        # changes whatsoever. Tests whether the engine tolerates a
        # bigger allocation budget on its own.
        old_sum = struct.unpack_from("<I", btchrsize_raw, args.group * 4)[0]
        new_sum = old_sum + 5 * (new_tpf - old_tpf) * bpt
        struct.pack_into("<I", btchrsize_raw, args.group * 4, new_sum)
        print(f"  btchrsize: {old_sum} -> {new_sum} (no NCGR/NCER change)")

    elif args.mode == "oamonly":
        # Keep NCGR vanilla. Shift OAMs so cell K points at slot
        # K*new_tpf/2. If the engine still uploads tpf=128 tiles per
        # cell using a stride that lands in vanilla NCGR garbage, we'll
        # see corruption. If it uses chrsize purely as an allocation
        # hint, cells should mostly render (with possible misreads).
        orig_ncer_raw = sprite.decompress_rle30(pak_obj.entries[base + 3])
        delta = new_tpf - old_tpf
        if delta % 2:
            ap.error(f"odd tpf delta {delta}")
        slot_shifts = [k * (delta // 2) for k in range(5)]
        new_ncer = _shift_ncer_oam_tiles(orig_ncer_raw, slot_shifts)
        pak_obj.replace_entry(base + 3, sprite.compress_rle30(new_ncer))
        new_pak_bytes = pak_obj.to_bytes()

        # Bump btchrsize by the NCER delta only (NCGR/NCLR/NANR unchanged)
        old_ncer_uc = len(orig_ncer_raw)
        new_ncer_uc = len(new_ncer)
        ncer_delta = new_ncer_uc - old_ncer_uc
        old_sum = struct.unpack_from("<I", btchrsize_raw, args.group * 4)[0]
        new_sum = old_sum + ncer_delta
        struct.pack_into("<I", btchrsize_raw, args.group * 4, new_sum)
        print(f"  shifted OAMs by {slot_shifts}; NCER {old_ncer_uc}B -> {new_ncer_uc}B")
        print(f"  btchrsize: {old_sum} -> {new_sum}")

    elif args.mode == "ncgronly":
        # Expand NCGR (real tiles at cell K start, zero pad after), but
        # keep OAMs vanilla. OAM slots will read PARTIAL real data
        # plus PARTIAL zero data because the OAM still spans the old
        # contiguous layout.
        d = btchr.decode_digimon(pak_obj, args.group, digimon_id=digimon_id)
        new_tiles = bytearray(5 * new_tpf * bpt)
        for k in range(5):
            src = k * old_tpf * bpt
            dst = k * new_tpf * bpt
            new_tiles[dst:dst + old_tpf * bpt] = d.tile_bytes[src:src + old_tpf * bpt]
        orig_ncgr_raw = sprite.decompress_rle30(pak_obj.entries[base + 1])
        new_ncgr = sprite.build_ncgr_from_template(bytes(new_tiles), orig_ncgr_raw)
        pak_obj.replace_entry(base + 1, sprite.compress_rle30(new_ncgr))
        new_pak_bytes = pak_obj.to_bytes()

        def _uc(entry: bytes) -> int:
            return len(sprite.decompress_rle30(entry))
        sizes = [_uc(pak_obj.entries[base + i]) for i in (1, 2, 3, 4)]
        new_sum = sum(sizes)
        old_sum = struct.unpack_from("<I", btchrsize_raw, args.group * 4)[0]
        struct.pack_into("<I", btchrsize_raw, args.group * 4, new_sum)
        print(f"  expanded NCGR to {5 * new_tpf} tiles; OAMs unchanged")
        print(f"  btchrsize: {old_sum} -> {new_sum}")

    elif args.mode == "fullnobsz":
        # Full expansion (NCGR expanded with per-cell pad + OAMs shifted +
        # chrsize bumped) but DON'T bump btchrsize. Tests whether the
        # bumped btchrsize is what destabilises cell 1 rendering.
        d = btchr.decode_digimon(pak_obj, args.group, digimon_id=digimon_id)
        new_tiles = bytearray(5 * new_tpf * bpt)
        for k in range(5):
            src = k * old_tpf * bpt
            dst = k * new_tpf * bpt
            new_tiles[dst:dst + old_tpf * bpt] = d.tile_bytes[src:src + old_tpf * bpt]
        orig_ncgr_raw = sprite.decompress_rle30(pak_obj.entries[base + 1])
        orig_ncer_raw = sprite.decompress_rle30(pak_obj.entries[base + 3])
        new_ncgr = sprite.build_ncgr_from_template(bytes(new_tiles), orig_ncgr_raw)
        delta = new_tpf - old_tpf
        slot_shifts = [k * (delta // 2) for k in range(5)]
        new_ncer = _shift_ncer_oam_tiles(orig_ncer_raw, slot_shifts)
        pak_obj.replace_entry(base + 1, sprite.compress_rle30(new_ncgr))
        pak_obj.replace_entry(base + 3, sprite.compress_rle30(new_ncer))
        new_pak_bytes = pak_obj.to_bytes()
        old_sum = struct.unpack_from("<I", btchrsize_raw, args.group * 4)[0]
        print(f"  expanded NCGR + shifted OAMs; btchrsize UNCHANGED at {old_sum}")

    elif args.mode == "tailpad":
        # Keep vanilla NCGR layout (cells back-to-back at original tiles),
        # append zero padding AT THE END to reach 5*new_tpf total. Vanilla
        # OAMs. Tests whether the engine cares about per-cell padding
        # location, or just the total tile count.
        d = btchr.decode_digimon(pak_obj, args.group, digimon_id=digimon_id)
        vanilla_tile_data = d.tile_bytes  # 5 * old_tpf * bpt bytes
        new_tiles = bytearray(5 * new_tpf * bpt)
        new_tiles[:len(vanilla_tile_data)] = vanilla_tile_data
        # tiles [5*old_tpf .. 5*new_tpf) remain zero
        orig_ncgr_raw = sprite.decompress_rle30(pak_obj.entries[base + 1])
        new_ncgr = sprite.build_ncgr_from_template(bytes(new_tiles), orig_ncgr_raw)
        pak_obj.replace_entry(base + 1, sprite.compress_rle30(new_ncgr))
        new_pak_bytes = pak_obj.to_bytes()

        def _uc(entry: bytes) -> int:
            return len(sprite.decompress_rle30(entry))
        sizes = [_uc(pak_obj.entries[base + i]) for i in (1, 2, 3, 4)]
        new_sum = sum(sizes)
        old_sum = struct.unpack_from("<I", btchrsize_raw, args.group * 4)[0]
        struct.pack_into("<I", btchrsize_raw, args.group * 4, new_sum)
        print(f"  NCGR: vanilla 5 cells + {5*(new_tpf-old_tpf)} pad tiles at end")
        print(f"  btchrsize: {old_sum} -> {new_sum}")

    # Splice in descending offset order so earlier offsets stay valid.
    splices = []
    if new_pak_bytes is not None:
        splices.append((pak_start, pak_end, new_pak_bytes, "BTCHR.PAK"))
    splices.append((chr_start, chr_end, bytes(chrsize_raw), "CHRSIZE.BIN"))
    splices.append((bsz_start, bsz_end, bytes(btchrsize_raw), "BTCHRSIZE.BIN"))
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
