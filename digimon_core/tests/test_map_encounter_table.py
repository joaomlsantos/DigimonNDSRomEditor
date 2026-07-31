"""Locks the `DAT/ec/ENCTBL.BIN` decode (per-field-map encounter table).

Full reverse-engineering write-up:
``research_docs/claude_notes/map_encounter_table.md``. These tests pin the
structural invariants that the decode rests on so a future loader/model
change can't silently regress them:

  * 265 entries, entry ``i`` == field map ``i``; entry 0 is the unused dev
    map (area/bg both ``FFFF``) — i.e. the leading ``FFFFFFFF`` is entry 0,
    not a table header.
  * field domains (area_index / battle_bg / wild_battle_bgm / the +4
    category);
  * the hard correlation ``battle_bg == 49`` (boss backdrop) ⟺ ``+4 == 3``;
  * Dusk and Dawn decode identically;
  * ``getByteArray`` round-trips.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PKG_PARENT = os.path.abspath(os.path.join(HERE, "..", ".."))
if PKG_PARENT not in sys.path:
    sys.path.insert(0, PKG_PARENT)

from digimon_core import fnt, loaders

ROM_DIR = r"C:\Workspace\digimon_stuffs\rom_files"
ROM_PATHS = {
    "DUSK_US": os.path.join(ROM_DIR, "1420 - Digimon World - Dusk (US).nds"),
    "DAWN_US": os.path.join(ROM_DIR, "1421 - Digimon World - Dawn (USA).nds"),
}
NONE = 0xFFFF


def _load(version):
    path = ROM_PATHS[version]
    if not os.path.exists(path):
        raise unittest.SkipTest(f"ROM not found: {path}")
    with open(path, "rb") as fh:
        data = bytearray(fh.read())
    ft = fnt.FileTable.from_rom(bytes(data))
    return loaders.loadMapEncounterTable(version, data, file_table=ft)


class MapEncounterDecodeTests(unittest.TestCase):
    VERSION = "DUSK_US"

    @classmethod
    def setUpClass(cls):
        cls.ent = _load(cls.VERSION)

    def test_entry_count_and_map_id_alignment(self):
        self.assertEqual(len(self.ent), 265)
        for i, e in enumerate(self.ent):
            self.assertEqual(e.map_id, i)

    def test_entry0_is_dev_map(self):
        # The file opens with FF FF FF FF — that's map 0's empty area+bg,
        # NOT a leading u32 header. (Regression guard for the +4-shift misread.)
        e0 = self.ent[0]
        self.assertEqual(e0.area_index, NONE)
        self.assertEqual(e0.battle_bg, NONE)

    def test_field_domains(self):
        for e in self.ent:
            if e.area_index != NONE:
                self.assertLessEqual(e.area_index, 73)
            if e.battle_bg != NONE:
                self.assertLessEqual(e.battle_bg, 75)   # 76 btmaps, ids 0..75
            self.assertIn(e.unknown_0x4, {1, 2, 3, 4, 5, 6, 7, 8, 10})
            self.assertIn(e.wild_battle_bgm, {0x10, 0x11, 0x12})

    def test_boss_backdrop_implies_category_3(self):
        # Every map drawing the shared boss backdrop (btmap 49) has +4 == 3,
        # and nothing else uses backdrop 49.
        boss = [e for e in self.ent if e.battle_bg == 49]
        self.assertTrue(boss)
        self.assertTrue(all(e.unknown_0x4 == 3 for e in boss))

    def test_bytearray_round_trips(self):
        for e in self.ent:
            reparsed = type(e)(e.getByteArray(), e.offset, e.map_id)
            self.assertEqual(reparsed.area_index, e.area_index)
            self.assertEqual(reparsed.battle_bg, e.battle_bg)
            self.assertEqual(reparsed.unknown_0x4, e.unknown_0x4)
            self.assertEqual(reparsed.wild_battle_bgm, e.wild_battle_bgm)


class MapEncounterDecodeTestsDawn(MapEncounterDecodeTests):
    VERSION = "DAWN_US"


class MapEncounterRegionParityTest(unittest.TestCase):
    def test_dusk_and_dawn_decode_identically(self):
        dusk = _load("DUSK_US")   # skips if either ROM is absent
        dawn = _load("DAWN_US")
        self.assertEqual(len(dusk), len(dawn))
        for a, b in zip(dusk, dawn):
            self.assertEqual(
                (a.area_index, a.battle_bg, a.unknown_0x4, a.wild_battle_bgm),
                (b.area_index, b.battle_bg, b.unknown_0x4, b.wild_battle_bgm),
                f"map {a.map_id} differs between regions",
            )


if __name__ == "__main__":
    unittest.main()
