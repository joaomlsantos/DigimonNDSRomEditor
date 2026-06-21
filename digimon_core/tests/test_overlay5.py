"""Overlay5 codec — PLAN.md §14.9 Phase A acceptance.

Three guarantees:

1. **Index over Dusk US** — ``Overlay5Index.from_file_table`` parses
   the in-ROM overlay 5 file and returns the expected ~505 entries.

2. **Entry 0264 = Dark Market events** — iterating overworld sprites
   in entry 0264 yields the 7 placements (ToyAgumon..Tentomon) with
   the exact (x, y) verified in
   ``research_docs/claude_notes/_overlay5_annotated/0264_sprite_overlay.py``.

3. **replace_sprite_xy round-trip** — flipping one placement's x/y
   updates only that 4-byte window inside the entry; every other byte
   is identical.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PKG_PARENT = os.path.abspath(os.path.join(HERE, "..", ".."))
if PKG_PARENT not in sys.path:
    sys.path.insert(0, PKG_PARENT)

from digimon_core import fnt, overlay5, rom  # noqa: E402


ROM_DIR = r"C:\Workspace\digimon_stuffs\rom_files"
DUSK_US = os.path.join(ROM_DIR, "1420 - Digimon World - Dusk (US).nds")


# (overworld_sprite_id, x, y) for entry 0264 (map 29 = Dark Market). The list
# order matches script execution order and was verified visually in
# 0264_sprite_overlay.png — every disc lands on a shop counter / NPC.
# (overworld_sprite_id == MCHR idx, x, y, name) per Dark Market render.
# The MCHR idx maps to a base species via sprite_map reverse-lookup —
# verified end-to-end against vanilla Dusk: every id resolves to its
# expected name. See research_docs/overworld_list.txt for the canonical
# MCHR idx -> overworld-sprite name table.
DARK_MARKET_SPRITES = [
    (0x00a2, 352, 148),  # ToyAgumon
    (0x00a1, 264, 221),  # DemiDevimon
    (0x00a3, 312, 197),  # Hagurumon
    (0x00a5, 433, 137),  # Wormmon
    (0x00b0, 482, 116),  # Kumamon
    (0x003c, 473, 237),  # Braun
    (0x009c, 520, 212),  # Tentomon
]


class Overlay5IndexTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not os.path.exists(DUSK_US):
            raise unittest.SkipTest(f"ROM not at {DUSK_US}")
        cls.rom = bytes(rom.loadRom(DUSK_US))
        cls.ft = fnt.FileTable.from_rom(cls.rom)
        cls.idx = overlay5.Overlay5Index.from_file_table(cls.ft, cls.rom)

    def test_entry_count(self):
        # The vanilla Dusk overlay5 ships 505 script entries.
        self.assertEqual(self.idx.entry_count(), 505)

    def test_map_id_mapping(self):
        # Entry 0235 -> map 0, 0264 -> map 29, 0498 -> map 263,
        # 0499 -> map 264. Map 264's entry overshoots into shared
        # cutscene data past 0x00C8 — see :func:`script_prologue_bytes`.
        self.assertEqual(overlay5.map_id_for(235), 0)
        self.assertEqual(overlay5.map_id_for(264), 29)
        self.assertEqual(overlay5.map_id_for(498), 263)
        self.assertEqual(overlay5.map_id_for(499), 264)
        self.assertIsNone(overlay5.map_id_for(234))
        self.assertIsNone(overlay5.map_id_for(500))

        self.assertEqual(overlay5.entry_ix_for_map(0), 235)
        self.assertEqual(overlay5.entry_ix_for_map(29), 264)
        self.assertEqual(overlay5.entry_ix_for_map(263), 498)
        self.assertEqual(overlay5.entry_ix_for_map(264), 499)
        self.assertIsNone(overlay5.entry_ix_for_map(265))

    def test_entry_0264_dark_market(self):
        entry = self.idx.read_entry(264)
        placements = overlay5.iter_overworld_sprites(entry)
        self.assertEqual(len(placements), len(DARK_MARKET_SPRITES))
        for p, (did, x, y) in zip(placements, DARK_MARKET_SPRITES):
            self.assertEqual(p.overworld_sprite_id, did)  # noqa: E501
            self.assertEqual(p.x, x)
            self.assertEqual(p.y, y)
            # Vanilla observation: facing always 0x0100, radius 0x0064,
            # reserved 0. If any of these drift, the codec's payload
            # layout assumption is wrong and the test should fail.
            self.assertEqual(p.facing, 0x0100)
            self.assertEqual(p.radius, 0x0064)
            self.assertEqual(p.reserved, 0)
            self.assertEqual(p.slot, p.slot_again)
            self.assertEqual(p.slot, p.slot_again2)

    def test_first_per_sprite_dedups(self):
        # No vanilla duplicates inside entry 0264 — this is the
        # baseline; the helper is exercised more thoroughly when other
        # entries have repeats.
        entry = self.idx.read_entry(264)
        placements = overlay5.iter_overworld_sprites(entry)
        firsts = overlay5.first_per_sprite_id(placements)
        self.assertEqual(len(firsts), len(placements))

    def test_replace_sprite_xy_byte_diff(self):
        entry = self.idx.read_entry(264)
        placements = overlay5.iter_overworld_sprites(entry)
        # Move ToyAgumon (first sprite) to a deliberate new position.
        target = placements[0]
        edited = overlay5.replace_sprite_xy(entry, target.block_offset, 100, 200)
        self.assertEqual(len(edited), len(entry))
        # Re-parse and check the field flipped.
        parsed = overlay5.OverworldSpritePlacement.from_bytes(
            edited, target.block_offset,
        )
        self.assertEqual(parsed.x, 100)
        self.assertEqual(parsed.y, 200)
        self.assertEqual(parsed.overworld_sprite_id, target.overworld_sprite_id)
        # Bytes outside the 4-byte x/y window must be identical to
        # vanilla, otherwise the splice path would clobber adjacent
        # script bytes.
        xy_start = target.block_offset + 4
        xy_end = xy_start + 4
        self.assertEqual(entry[:xy_start], edited[:xy_start])
        self.assertEqual(entry[xy_end:], edited[xy_end:])

    def test_replace_sprite_xy_rejects_non_opcode(self):
        entry = self.idx.read_entry(264)
        # Offset 0 of entry 0264 is raw map-descriptor bytes (not an
        # opcode); replace_sprite_xy must refuse rather than silently
        # corrupt the entry.
        with self.assertRaises(ValueError):
            overlay5.replace_sprite_xy(entry, 0, 0, 0)

    def test_replace_entry_length_invariant(self):
        # Same-length replacements only — any other length is rejected
        # so the surrounding pointer table stays valid.
        entry = self.idx.read_entry(264)
        with self.assertRaises(ValueError):
            self.idx.replace_entry(264, entry + b"\x00")
        # Same length: must succeed and produce a payload of unchanged
        # total length.
        unchanged = self.idx.replace_entry(264, entry)
        self.assertEqual(len(unchanged), len(self.idx.payload))
        self.assertEqual(unchanged, self.idx.payload)

    def test_iter_map_entries_skips_non_field(self):
        # Field maps span 235..499 = 265 entries (maps 0..264 inclusive).
        rows = self.idx.iter_map_entries()
        self.assertEqual(len(rows), overlay5.MAX_MAP_ID + 1)
        map_ids = [mid for mid, _, _ in rows]
        self.assertEqual(map_ids, list(range(overlay5.MAX_MAP_ID + 1)))

    def test_overlay5_payload_starts_with_pointer_table(self):
        # Sanity check: u32 hdr_size at offset 0, then hdr_size-4 bytes
        # of u32 pointers. Pointers shouldn't run past the file.
        import struct
        hdr_size = struct.unpack_from("<I", self.idx.payload, 0)[0]
        self.assertGreater(hdr_size, 4)
        self.assertLess(hdr_size, len(self.idx.payload))
        for ptr in self.idx.pointers:
            self.assertLess(ptr, len(self.idx.payload))


if __name__ == "__main__":
    unittest.main()
