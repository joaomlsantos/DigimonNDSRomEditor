"""Verify the tailpad expansion model: vanilla cells + tail tiles, with
extra OAMs pointing into the tail range.

If this renders koromon vanilla PLUS 5 visible pink squares (one per
cell, positioned next to the original sprite), we've confirmed:
  - vanilla cell layout + tail padding is the engine-acceptable shape
  - new OAMs that reference tail tiles render correctly
  - this is THE expansion model

Tile layout (scale=2, old_tpf=64, new_tpf=128, 640 tiles total):
  [0..64)    cell 0 vanilla
  [64..128)  cell 1 vanilla
  [128..192) cell 2 vanilla
  [192..256) cell 3 vanilla
  [256..320) cell 4 vanilla
  [320..384) cell 0 growth (4 paint tiles + 60 unused)
  [384..448) cell 1 growth
  [448..512) cell 2 growth
  [512..576) cell 3 growth
  [576..640) cell 4 growth
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


def _inject_tail_paint_oams(
    ncer_raw: bytes,
    n_cells: int,
    cell_xmax_per_cell: List[int],
    paint_first_tile_per_cell: List[int],
    boundary_bytes: int = 128,
    bpt: int = 64,
) -> bytes:
    """Add one 16x16 OAM per cell pointing at paint_first_tile (in NCGR
    tile units). Same as the original paint helper but doesn't shift
    existing OAMs (tailpad uses vanilla OAMs)."""
    cebk = sprite.find_block(ncer_raw, b"KBEC")
    bank_attr = struct.unpack_from("<H", ncer_raw, cebk + 10)[0]
    cell_data_off = struct.unpack_from("<I", ncer_raw, cebk + 12)[0]
    kbec_size = struct.unpack_from("<I", ncer_raw, cebk + 4)[0]
    cell_size = 16 if (bank_attr & 1) else 8
    cells_base = cebk + 8 + cell_data_off
    oam_base = cells_base + n_cells * cell_size
    kbec_end = cebk + kbec_size
    post_kbec = bytes(ncer_raw[kbec_end:])

    tile_mult = boundary_bytes // bpt  # 128/64 = 2

    new_oam_block = bytearray()
    cell_records = []
    for ci in range(n_cells):
        cell_off = cells_base + ci * cell_size
        n_oam = struct.unpack_from("<H", ncer_raw, cell_off)[0]
        oam_off_old = struct.unpack_from("<I", ncer_raw, cell_off + 4)[0]
        old_oams = bytes(
            ncer_raw[oam_base + oam_off_old:oam_base + oam_off_old + n_oam * 6]
        )

        x = cell_xmax_per_cell[ci] + 4
        y = 0
        is8bpp = True
        slot = paint_first_tile_per_cell[ci] // tile_mult
        if slot > 0x3FF:
            raise ValueError(f"cell {ci}: slot {slot} > 1023")
        a0 = (y & 0xFF) | (0x2000 if is8bpp else 0)
        a1 = (x & 0x1FF) | (1 << 14)   # size=1 -> 16x16 with shape=0
        a2 = slot & 0x3FF

        new_oam_off = len(new_oam_block)
        new_oam_block += old_oams
        new_oam_block += struct.pack("<HHH", a0, a1, a2)
        cell_records.append((n_oam + 1, new_oam_off))

    while len(new_oam_block) % 4 != 0:
        new_oam_block.append(0)

    out = bytearray(ncer_raw[:cells_base])
    for ci, (new_n, new_off) in enumerate(cell_records):
        rec = bytearray(
            ncer_raw[cells_base + ci * cell_size:cells_base + (ci + 1) * cell_size]
        )
        struct.pack_into("<H", rec, 0, new_n)
        struct.pack_into("<I", rec, 4, new_off)
        out += rec
    out += new_oam_block
    new_kbec_size = len(out) - cebk
    struct.pack_into("<I", out, cebk + 4, new_kbec_size)
    out += post_kbec
    struct.pack_into("<I", out, 8, len(out))
    return bytes(out)


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
    bpt = btchr.BYTES_PER_TILE_8BPP
    base = args.group * btchr.GROUP_SIZE

    print(f"group {args.group}: tpf {old_tpf} -> {new_tpf}")

    # NCGR: vanilla cells [0..5*old_tpf) + tail [5*old_tpf..5*new_tpf).
    # Paint 4 tiles per cell-growth-region (16x16 visible square).
    d = btchr.decode_digimon(pak_obj, args.group, digimon_id=digimon_id)
    new_tiles = bytearray(5 * new_tpf * bpt)
    new_tiles[:5 * old_tpf * bpt] = d.tile_bytes
    growth_per_cell = new_tpf - old_tpf  # tiles per cell in tail
    paint_first_tile_per_cell = []
    for k in range(5):
        # K's growth tile range: [5*old_tpf + k*growth_per_cell ..
        #                         5*old_tpf + (k+1)*growth_per_cell)
        paint_first = 5 * old_tpf + k * growth_per_cell
        paint_first_tile_per_cell.append(paint_first)
        for ti in range(4):  # 4 tiles = 16x16
            t_off = (paint_first + ti) * bpt
            for b in range(bpt):
                new_tiles[t_off + b] = 1  # palette index 1 = pink

    orig_ncgr_raw = sprite.decompress_rle30(pak_obj.entries[base + 1])
    orig_ncer_raw = sprite.decompress_rle30(pak_obj.entries[base + 3])
    new_ncgr = sprite.build_ncgr_from_template(bytes(new_tiles), orig_ncgr_raw)

    # Add a paint OAM to each cell pointing at its tail growth region.
    cell_xmax = [btchr.cell_bbox(c)[2] for c in d.ncer.cells]
    new_ncer = _inject_tail_paint_oams(
        orig_ncer_raw, 5, cell_xmax, paint_first_tile_per_cell,
    )

    pak_obj.replace_entry(base + 1, sprite.compress_rle30(new_ncgr))
    pak_obj.replace_entry(base + 3, sprite.compress_rle30(new_ncer))
    new_pak_bytes = pak_obj.to_bytes()

    # chrsize.tpf bump
    new_word = (digimon_id & 0xFFFF) | ((new_tpf & 0xFFFF) << 16)
    struct.pack_into("<I", chrsize_raw, args.group * 4, new_word)

    # btchrsize bump
    def _uc(e: bytes) -> int:
        return len(sprite.decompress_rle30(e))
    sizes = [_uc(pak_obj.entries[base + i]) for i in (1, 2, 3, 4)]
    new_sum = sum(sizes)
    old_sum = struct.unpack_from("<I", btchrsize_raw, args.group * 4)[0]
    struct.pack_into("<I", btchrsize_raw, args.group * 4, new_sum)
    print(f"  btchrsize: {old_sum} -> {new_sum}")
    print(f"  paint tile starts: {paint_first_tile_per_cell}")
    print(f"  paint OAM slots: {[t // 2 for t in paint_first_tile_per_cell]}")

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
