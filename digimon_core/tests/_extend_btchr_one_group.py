"""One-shot ROM patcher: extend BTCHR by one group, repoint one enemy.

This is a verification probe, not a production feature. It hand-builds a
DWDD Dusk (US) ROM with one extra battle-sprite slot (group 415) carrying
a duplicate of group 2's data (Koromon, id=0x66), then repoints one
enemy's ``sprite_map.main_sprite`` to that new slot. Load the resulting
``.nds`` in DeSmuME, enter a battle against the patched enemy, and observe:

* If the enemy renders as Koromon -> engine accepts group 415; we can
  build a proper UI for inserting sprites.
* If it renders as Chicchimon (the vanilla group-0x65 default), or
  crashes, or shows garbage -> the engine has a hardcoded bound
  somewhere (Ghidra needed before we invest in the UI).

Run with::

    python -m digimon_core.tests._extend_btchr_one_group [target_enemy_id_hex]

``target_enemy_id_hex`` defaults to ``0x1F5`` — the first fixed-enemy
slot. Pick any sprite-map slot you can encounter quickly in-game.

Outputs ``<rom_dir>/dusk_us_btchr_ext.nds`` next to the source ROM.
"""
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PKG_PARENT = os.path.abspath(os.path.join(HERE, "..", ".."))
if PKG_PARENT not in sys.path:
    sys.path.insert(0, PKG_PARENT)

from digimon_core import btchr, constants, fat, fnt, pak, rom  # noqa: E402


ROM_DIR = r"C:\Workspace\digimon_stuffs\rom_files"
SRC_ROM = os.path.join(ROM_DIR, "1420 - Digimon World - Dusk (US).nds")
OUT_ROM = os.path.join(ROM_DIR, "dusk_us_btchr_ext.nds")

BTCHR_PAK = "DAT/BTCHR.PAK"
CHRSIZE = "DAT/BTCHR/CHRSIZE.BIN"
BTCHRSIZE = "DAT/BTCHR/BTCHRSIZE.BIN"

SOURCE_GROUP = 2  # Koromon (id=0x66) — distinct silhouette, easy to recognize
NEW_GROUP_ID = 415  # next index after vanilla 0..414


def _splice_file(out: bytearray, path: str, new_bytes: bytes) -> None:
    """FAT-aware in-place replacement of an entire FAT-listed file.

    Resolves ``path`` against the current FAT in ``out``, splices the
    new payload, and patches the FAT/header so downstream files shift
    by the aligned delta. Re-resolve any other path you need after this.
    """
    ft = fnt.FileTable.from_rom(bytes(out))
    start, end = ft.resolve(path)
    idx, _cs, ce = fat.find_container(out, start, end)
    content_delta = len(new_bytes) - (end - start)
    aligned_shift = fat.splice_range(out, start, end, ce, new_bytes)
    fat.resize_fat_entry(out, idx, ce, content_delta, aligned_shift)


def main() -> None:
    target_enemy_id = int(sys.argv[1], 16) if len(sys.argv) > 1 else 0x1F5

    print(f"Source ROM : {SRC_ROM}")
    print(f"Output ROM : {OUT_ROM}")
    print(f"Target slot: 0x{target_enemy_id:03x} (sprite_map list index)")
    print()

    rom_bytes = bytearray(rom.loadRom(SRC_ROM))

    # ---- snapshot vanilla file ranges (all from the same un-mutated ROM)
    ft = fnt.FileTable.from_rom(bytes(rom_bytes))
    btchr_start, btchr_end = ft.resolve(BTCHR_PAK)
    chr_start, chr_end = ft.resolve(CHRSIZE)
    bsz_start, bsz_end = ft.resolve(BTCHRSIZE)

    # ---- build extended BTCHR.PAK (415 groups -> 416 groups)
    pak_obj = pak.PakFile(bytes(rom_bytes[btchr_start:btchr_end]))
    n_groups = btchr.parse_pak_groups(pak_obj)
    assert n_groups == 415, f"expected vanilla 415 groups, got {n_groups}"
    src_base = SOURCE_GROUP * btchr.GROUP_SIZE
    for k in range(btchr.GROUP_SIZE):
        pak_obj.entries.append(bytes(pak_obj.entries[src_base + k]))
        pak_obj.flags.append(pak_obj.flags[src_base + k])
    pak_obj.count += btchr.GROUP_SIZE
    new_pak_bytes = pak_obj.to_bytes()
    print(f"BTCHR.PAK  : {btchr_end - btchr_start} -> {len(new_pak_bytes)} B")

    # ---- build extended chrsize.bin: vanilla bytes + source group's u32
    chr_raw = bytes(rom_bytes[chr_start:chr_end])
    src_word = struct.unpack_from("<I", chr_raw, SOURCE_GROUP * 4)[0]
    new_chr = chr_raw + struct.pack("<I", src_word)
    print(f"chrsize    : {len(chr_raw)} -> {len(new_chr)} B  "
          f"(new word=0x{src_word:08x}, id={src_word & 0xFFFF}, "
          f"tpf={(src_word >> 16) & 0xFFFF})")

    # ---- build extended btchrsize.bin
    bsz_raw = bytes(rom_bytes[bsz_start:bsz_end])
    src_bsz = struct.unpack_from("<I", bsz_raw, SOURCE_GROUP * 4)[0]
    new_bsz = bsz_raw + struct.pack("<I", src_bsz)
    print(f"btchrsize  : {len(bsz_raw)} -> {len(new_bsz)} B  "
          f"(new value={src_bsz} B uncompressed sum)")

    # ---- splice in descending-offset order so unspliced paths stay valid
    splices = [
        (btchr_start, BTCHR_PAK, new_pak_bytes),
        (chr_start, CHRSIZE, new_chr),
        (bsz_start, BTCHRSIZE, new_bsz),
    ]
    for _start, path, payload in sorted(splices, key=lambda x: -x[0]):
        _splice_file(rom_bytes, path, payload)
    print()

    # ---- patch the target enemy's sprite_map.main_sprite -> 415
    sm_start, _sm_end = constants.SPRITE_MAPPING_TABLE_OFFSET["DUSK_US"]
    entry_off = sm_start + target_enemy_id * 16
    main_sprite_off = entry_off + 8
    old_main = struct.unpack_from("<I", rom_bytes, main_sprite_off)[0]
    struct.pack_into("<I", rom_bytes, main_sprite_off, NEW_GROUP_ID)
    print(f"sprite_map[0x{target_enemy_id:03x}].main_sprite: "
          f"{old_main} -> {NEW_GROUP_ID}")
    print()

    rom.writeRom(rom_bytes, OUT_ROM)
    print(f"Wrote {len(rom_bytes)} B -> {OUT_ROM}")
    print()
    print("Verification steps:")
    print(f"  1. Open {os.path.basename(OUT_ROM)} in DeSmuME.")
    print(f"  2. Trigger a battle against enemy id 0x{target_enemy_id:03x}.")
    print( "  3. If the sprite renders as Koromon (group 2 duplicate) ->")
    print( "     engine accepts BTCHR group 415; we can build the UI.")
    print( "  4. Chicchimon (vanilla 0x65 default) / crash / blank ->")
    print( "     engine has a hardcoded bound; Ghidra the BTCHR loader.")


if __name__ == "__main__":
    main()
