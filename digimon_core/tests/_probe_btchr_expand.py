"""One-off probe: expand a single BTCHR digimon's tile budget by N×.

Purpose: validate the empirical unknowns from the expansion audit
(VRAM allocation, OAM cap, engine tpf respect) *before* baking
assumptions into the importer UI.

**Mode A (default, no --paint):** the on-screen sprite should look
identical to vanilla. We only:
  - grow NCGR to ``5 × new_tpf`` tiles (extras = transparent index 0)
  - shift each OAM's tile field so cells still address their data
  - bump CHRSIZE.BIN's tpf field for this group
  - bump BTCHRSIZE.BIN's allocation sum for this group

If the sprite renders unchanged in DeSmuME, we've confirmed:
  - engine honours the new tpf
  - btchrsize allocation grew correctly
  - OAM slot arithmetic still works at the larger stride

**Mode B (``--paint``):** also inject one extra 16×16 OAM per cell
pointing at the newly-allocated tile range, filled with a solid
palette-index-1 block. If the engine renders these extra OAMs next to
the original sprite, we've confirmed it'll accept *new* OAMs (not just
a bigger tile bank). This tests the actual "grow the visible sprite"
path.

Usage::

    python probe_btchr_expand.py --rom in.nds --out probed.nds \\
        --group 2 --scale 2 [--paint]

Throwaway — not wired into the editor, not exercised by tests. Delete
once the audit lands in the importer.
"""
from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path
from typing import List, Tuple

# Allow running directly from inside the repo without installing.
_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from digimon_core import btchr, fat, fnt, pak, sprite
from digimon_core import ncer as ncer_mod


BTCHR_PAK = "DAT/BTCHR.PAK"
CHRSIZE_PATH = "DAT/BTCHR/CHRSIZE.BIN"
BTCHRSIZE_PATH = "DAT/BTCHR/BTCHRSIZE.BIN"


def _shift_ncer_oam_tiles(
    ncer_raw: bytes, slot_shift_per_cell: List[int],
) -> bytes:
    """Return a copy of ``ncer_raw`` with every OAM's tile field shifted.

    ``slot_shift_per_cell[k]`` is added to every OAM in cell k. Mutates
    only the low 10 bits of OAM word a2 — every other byte of the NCER
    (header, cell records, bboxes, mapping) is byte-identical.
    """
    out = bytearray(ncer_raw)
    cebk = sprite.find_block(ncer_raw, b"KBEC")
    n_cells = struct.unpack_from("<H", ncer_raw, cebk + 8)[0]
    bank_attr = struct.unpack_from("<H", ncer_raw, cebk + 10)[0]
    cell_data_off = struct.unpack_from("<I", ncer_raw, cebk + 12)[0]
    cell_size = 16 if (bank_attr & 1) else 8
    cells_base = cebk + 8 + cell_data_off
    oam_base = cells_base + n_cells * cell_size

    if len(slot_shift_per_cell) != n_cells:
        raise ValueError(
            f"shift list len {len(slot_shift_per_cell)} != n_cells {n_cells}"
        )

    for ci in range(n_cells):
        cell_off = cells_base + ci * cell_size
        n_oam = struct.unpack_from("<H", ncer_raw, cell_off)[0]
        oam_off = struct.unpack_from("<I", ncer_raw, cell_off + 4)[0]
        shift = slot_shift_per_cell[ci]
        if shift == 0:
            continue
        for oi in range(n_oam):
            attr_off = oam_base + oam_off + oi * 6 + 4  # a2 word
            a2 = struct.unpack_from("<H", out, attr_off)[0]
            old_tile = a2 & 0x3FF
            new_tile = old_tile + shift
            if new_tile > 0x3FF:
                raise ValueError(
                    f"cell {ci} OAM {oi} new slot {new_tile} > 1023 "
                    "(NCER tile field is 10 bits) — pick a smaller scale "
                    "or a smaller group to probe"
                )
            a2 = (a2 & ~0x3FF) | (new_tile & 0x3FF)
            struct.pack_into("<H", out, attr_off, a2)
    return bytes(out)


def _inject_paint_oams(
    ncer_raw: bytes,
    n_cells: int,
    cell_xmax_per_cell: List[int],
    paint_first_tile_per_cell: List[int],
) -> bytes:
    """Append one 16×16 OAM per cell pointing at ``paint_first_tile``.

    Each new OAM sits at ``(xmax + 4, 0)`` so it draws *next to* the
    original sprite — a visible square confirms the engine rendered an
    OAM that didn't exist in vanilla.

    Restructures the NCER OAM block: original OAMs stay, but each cell's
    run is followed immediately by its one new OAM, so cell K's
    ``oam_off`` shifts by ``K × 6`` (K extra OAMs precede it). Updates
    KBEC block size + RECN file_size, and preserves any post-KBEC blocks
    (LABL/UEXT — BTCHR ships 3 blocks per NCER).

    16×16 = shape=0 (square), size=1 → encoding (s, sz) = (0, 1).
    Slot field = ``paint_first_tile / tile_mult``; tile_mult=2 for BTCHR
    1D mapping, so the slot is half the tile index.
    """
    cebk = sprite.find_block(ncer_raw, b"KBEC")
    bank_attr = struct.unpack_from("<H", ncer_raw, cebk + 10)[0]
    cell_data_off = struct.unpack_from("<I", ncer_raw, cebk + 12)[0]
    kbec_size = struct.unpack_from("<I", ncer_raw, cebk + 4)[0]
    cell_size = 16 if (bank_attr & 1) else 8
    cells_base = cebk + 8 + cell_data_off
    oam_base = cells_base + n_cells * cell_size
    kbec_end = cebk + kbec_size
    post_kbec = bytes(ncer_raw[kbec_end:])

    # Build new OAM block per cell: original OAMs + 1 new OAM.
    new_oam_block = bytearray()
    cell_records: List[Tuple[int, int]] = []  # (new_n_oam, new_oam_off)
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
        slot = paint_first_tile_per_cell[ci] // 2
        if slot > 0x3FF:
            raise ValueError(
                f"cell {ci}: paint slot {slot} exceeds 10-bit OAM tile field"
            )
        # a0: y(8) | shape=0 in bits 14-15, is8bpp at bit 13
        a0 = (y & 0xFF) | (0x2000 if is8bpp else 0)
        # a1: x(9) | size=1 in bits 14-15
        a1 = (x & 0x1FF) | (1 << 14)
        # a2: slot(10) | pal(0) | prio(0)
        a2 = slot & 0x3FF

        new_oam_off = len(new_oam_block)
        new_oam_block += old_oams
        new_oam_block += struct.pack("<HHH", a0, a1, a2)
        cell_records.append((n_oam + 1, new_oam_off))

    # 4-byte align the OAM block so downstream LABL/UEXT magics land aligned.
    while len(new_oam_block) % 4 != 0:
        new_oam_block.append(0)

    # Rebuild: [0..cells_base) verbatim, cell records (n_oam + oam_off
    # patched, rest preserved), new OAM block, then post-KBEC blocks.
    out = bytearray(ncer_raw[:cells_base])
    for ci, (new_n_oam, new_oam_off) in enumerate(cell_records):
        cell_off_src = cells_base + ci * cell_size
        rec = bytearray(
            ncer_raw[cell_off_src:cell_off_src + cell_size]
        )
        struct.pack_into("<H", rec, 0, new_n_oam)
        struct.pack_into("<I", rec, 4, new_oam_off)
        out += rec
    out += new_oam_block
    new_kbec_size = len(out) - cebk
    struct.pack_into("<I", out, cebk + 4, new_kbec_size)
    out += post_kbec
    struct.pack_into("<I", out, 8, len(out))
    return bytes(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rom", required=True, help="input vanilla ROM")
    ap.add_argument("--out", required=True, help="output ROM")
    ap.add_argument("--group", type=int, required=True, help="BTCHR group idx")
    ap.add_argument("--scale", type=int, default=2, help="tpf multiplier")
    ap.add_argument(
        "--paint", action="store_true",
        help="add one visible 16×16 OAM per cell in the new tile area",
    )
    args = ap.parse_args()

    if args.group in btchr.SENTINEL_GROUPS:
        ap.error(f"group {args.group} is a sentinel — pick a real digimon")

    rom = bytearray(Path(args.rom).read_bytes())
    ft = fnt.FileTable.from_rom(rom)
    pak_start, pak_end = ft.resolve(BTCHR_PAK)
    chr_start, chr_end = ft.resolve(CHRSIZE_PATH)
    bsz_start, bsz_end = ft.resolve(BTCHRSIZE_PATH)

    pak_obj = pak.PakFile(bytes(rom[pak_start:pak_end]))
    chrsize_raw = bytes(rom[chr_start:chr_end])
    btchrsize_raw = bytes(rom[bsz_start:bsz_end])

    chrsize_rows = btchr.parse_chrsize(chrsize_raw)
    digimon_id, old_tpf = chrsize_rows[args.group]
    new_tpf = old_tpf * args.scale
    print(
        f"group {args.group} (digimon_id={digimon_id}): "
        f"tpf {old_tpf} -> {new_tpf} (x{args.scale})"
    )
    if new_tpf <= 0 or old_tpf <= 0:
        ap.error(f"bad tpf: old={old_tpf} new={new_tpf}")

    # Decode current state — we need OAM count + per-cell bbox even in
    # no-paint mode (the audit prints help correlate behaviour).
    d = btchr.decode_digimon(pak_obj, args.group, digimon_id=digimon_id)
    if d.n_tiles != 5 * old_tpf:
        print(
            f"WARN: ncgr tile count {d.n_tiles} != 5 × tpf "
            f"({5 * old_tpf}) — engine invariant violated, probe may "
            "behave oddly",
            file=sys.stderr,
        )
    print(
        f"  current OAMs: {sum(len(c.oams) for c in d.ncer.cells)} "
        f"({[len(c.oams) for c in d.ncer.cells]})"
    )

    # ---- new NCGR tile bytes ---------------------------------------------
    bpt = btchr.BYTES_PER_TILE_8BPP
    new_tiles = bytearray(5 * new_tpf * bpt)
    for k in range(5):
        src_off = k * old_tpf * bpt
        dst_off = k * new_tpf * bpt
        new_tiles[dst_off:dst_off + old_tpf * bpt] = (
            d.tile_bytes[src_off:src_off + old_tpf * bpt]
        )
    paint_first_tile_per_cell: List[int] = []
    if args.paint:
        # Fill 4 tiles (16×16 worth) per cell, immediately after the
        # original tiles, with solid palette index 1.
        for k in range(5):
            paint_first = k * new_tpf + old_tpf
            paint_first_tile_per_cell.append(paint_first)
            for ti in range(4):
                t_off = (paint_first + ti) * bpt
                for b in range(bpt):
                    new_tiles[t_off + b] = 1

    # ---- new NCER --------------------------------------------------------
    orig_ncgr_raw = sprite.decompress_rle30(
        pak_obj.entries[args.group * btchr.GROUP_SIZE + 1]
    )
    orig_ncer_raw = sprite.decompress_rle30(
        pak_obj.entries[args.group * btchr.GROUP_SIZE + 3]
    )

    # Slot shift per cell. Tile index shift = k * (new_tpf - old_tpf).
    # Slot stride is 2 tiles, so slot shift = k * (new_tpf - old_tpf) // 2.
    delta = new_tpf - old_tpf
    if delta % 2 != 0:
        ap.error(
            f"tpf delta {delta} is odd — slot shift wouldn't be integer. "
            f"Pick an even scale or an even-tpf group."
        )
    slot_shifts = [k * (delta // 2) for k in range(5)]
    new_ncer_raw = _shift_ncer_oam_tiles(orig_ncer_raw, slot_shifts)

    if args.paint:
        cell_xmax = []
        for cell in d.ncer.cells:
            _, _, xmax, _ = btchr.cell_bbox(cell)
            cell_xmax.append(xmax)
        new_ncer_raw = _inject_paint_oams(
            new_ncer_raw, 5, cell_xmax, paint_first_tile_per_cell,
        )

    # ---- rebuild NCGR ----------------------------------------------------
    new_ncgr = sprite.build_ncgr_from_template(bytes(new_tiles), orig_ncgr_raw)

    # ---- swap PAK entries (NCGR=base+1, NCER=base+3) ---------------------
    base = args.group * btchr.GROUP_SIZE
    pak_obj.replace_entry(base + 1, sprite.compress_rle30(new_ncgr))
    pak_obj.replace_entry(base + 3, sprite.compress_rle30(new_ncer_raw))

    new_pak_bytes = pak_obj.to_bytes()

    # ---- new chrsize.bin entry ------------------------------------------
    new_chrsize = bytearray(chrsize_raw)
    new_word = (digimon_id & 0xFFFF) | ((new_tpf & 0xFFFF) << 16)
    struct.pack_into("<I", new_chrsize, args.group * 4, new_word)

    # ---- new btchrsize.bin entry ----------------------------------------
    # Field is the sum of uncompressed sizes of entries 1..4 (mini-header
    # excluded — that's entry 0). The engine uses it for load-time VRAM
    # allocation, per project_btchr_format.
    def _uncompressed_size(entry: bytes) -> int:
        return len(sprite.decompress_rle30(entry))
    sizes = [_uncompressed_size(pak_obj.entries[base + i]) for i in (1, 2, 3, 4)]
    new_sum = sum(sizes)
    new_btchrsize = bytearray(btchrsize_raw)
    old_sum = struct.unpack_from("<I", new_btchrsize, args.group * 4)[0]
    struct.pack_into("<I", new_btchrsize, args.group * 4, new_sum)
    print(
        f"  btchrsize[{args.group}]: {old_sum} -> {new_sum} "
        f"(NCGR {sizes[0]}B, NCLR {sizes[1]}B, NCER {sizes[2]}B, NANR {sizes[3]}B)"
    )

    # ---- FAT splice: do highest-offset first so earlier offsets stay valid
    splices: List[Tuple[int, int, bytes, str]] = [
        (pak_start, pak_end, new_pak_bytes, BTCHR_PAK),
        (chr_start, chr_end, bytes(new_chrsize), CHRSIZE_PATH),
        (bsz_start, bsz_end, bytes(new_btchrsize), BTCHRSIZE_PATH),
    ]
    splices.sort(key=lambda x: x[0], reverse=True)
    for start, end, new_bytes, label in splices:
        idx, _cs, ce = fat.find_container(bytes(rom), start, end)
        content_delta = len(new_bytes) - (end - start)
        aligned_shift = fat.splice_range(rom, start, end, ce, new_bytes)
        fat.resize_fat_entry(rom, idx, ce, content_delta, aligned_shift)
        print(
            f"  spliced {label}: delta={content_delta:+d}B aligned_shift={aligned_shift:+d}B"
        )

    Path(args.out).write_bytes(bytes(rom))
    print(f"wrote {args.out} ({len(rom)}B)")
    if args.paint:
        print(
            "  --paint: expect 5 extra 16×16 squares next to the original "
            "sprite (one per cell). Missing squares = engine ignored new OAMs."
        )
    else:
        print(
            "  no --paint: sprite should look identical to vanilla. Any "
            "visual change indicates btchrsize/tpf/OAM-shift mismatch."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
