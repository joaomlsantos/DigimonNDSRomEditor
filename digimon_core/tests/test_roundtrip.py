"""Byte-exact round-trip tests.

For each model in the package: parse all instances from a vanilla ROM, write
them back at their original offsets onto a fresh copy of the ROM, and assert
the resulting bytes match the source exactly.

If a serializer is wrong, this test will pinpoint which model and which byte.
"""
import os
import sys
import unittest

# allow running with `python -m unittest` from the digimon_core/ dir,
# or directly via `python tests/test_roundtrip.py`
HERE = os.path.dirname(os.path.abspath(__file__))
PKG_PARENT = os.path.abspath(os.path.join(HERE, "..", ".."))
if PKG_PARENT not in sys.path:
    sys.path.insert(0, PKG_PARENT)

from digimon_core import constants, loaders, rom  # noqa: E402


ROM_DIR = r"C:\Workspace\digimon_stuffs\rom_files"
ROM_PATHS = {
    "DUSK_US": os.path.join(ROM_DIR, "1420 - Digimon World - Dusk (US).nds"),
    "DAWN_US": os.path.join(ROM_DIR, "1421 - Digimon World - Dawn (USA).nds"),
}


def _first_diff(a: bytearray, b: bytearray) -> str:
    """Find the first differing byte and report it with surrounding context."""
    for i in range(min(len(a), len(b))):
        if a[i] != b[i]:
            ctx_start = max(0, i - 4)
            ctx_end = min(len(a), i + 4)
            return (
                f"first diff at offset 0x{i:08x}: "
                f"vanilla={a[i]:#04x} roundtrip={b[i]:#04x} "
                f"(vanilla={a[ctx_start:ctx_end].hex()} "
                f"roundtrip={b[ctx_start:ctx_end].hex()})"
            )
    if len(a) != len(b):
        return f"length mismatch: vanilla={len(a)} roundtrip={len(b)}"
    return "<no diff>"


class RoundtripTestBase:
    """Mixin: one TestCase per ROM version, sharing the same suite of checks."""

    VERSION: str = ""  # filled in by subclass

    @classmethod
    def setUpClass(cls):
        path = ROM_PATHS[cls.VERSION]
        if not os.path.exists(path):
            raise unittest.SkipTest(f"ROM not found: {path}")
        cls.vanilla = rom.loadRom(path)
        detected = rom.detectVersion(cls.vanilla, path)
        if detected != cls.VERSION:
            raise unittest.SkipTest(
                f"ROM at {path} has version {detected}, expected {cls.VERSION}"
            )

    # helpers -----------------------------------------------------------

    def _assert_region_equal(self, target: bytearray, label: str):
        if target != self.vanilla:
            self.fail(f"{label} ({self.VERSION}): {_first_diff(self.vanilla, target)}")

    # tests -------------------------------------------------------------

    def test_base_digimon(self):
        target = bytearray(self.vanilla)
        objs = loaders.loadBaseDigimonInfo(self.VERSION, self.vanilla)
        self.assertGreater(len(objs), 0, "no base digimon parsed")
        for obj in objs.values():
            obj.writeToRom(target)
        self._assert_region_equal(target, "BaseDataDigimon")

    def test_enemy_digimon(self):
        target = bytearray(self.vanilla)
        objs = loaders.loadEnemyDigimonInfo(self.VERSION, self.vanilla)
        self.assertGreater(len(objs), 0, "no enemy digimon parsed")
        for obj in objs.values():
            obj.writeToRom(target)
        self._assert_region_equal(target, "EnemyDataDigimon")

    def test_moves(self):
        target = bytearray(self.vanilla)
        for obj in loaders.loadMoveData(self.VERSION, self.vanilla):
            obj.writeToRom(target)
        self._assert_region_equal(target, "MoveData")

    def test_quests(self):
        target = bytearray(self.vanilla)
        for obj in loaders.loadQuestData(self.VERSION, self.vanilla):
            obj.writeToRom(target)
        self._assert_region_equal(target, "QuestData")

    def test_encounter_rewards(self):
        target = bytearray(self.vanilla)
        for obj in loaders.loadEncounterRewardData(self.VERSION, self.vanilla):
            obj.writeToRom(target)
        self._assert_region_equal(target, "EncounterRewardTable")

    def test_standard_digivolutions(self):
        target = bytearray(self.vanilla)
        for obj in loaders.loadStandardDigivolutions(self.VERSION, self.vanilla).values():
            obj.writeToRom(target)
        self._assert_region_equal(target, "StandardDigivolution")

    def test_armor_digivolutions(self):
        target = bytearray(self.vanilla)
        for obj in loaders.loadArmorDigivolutions(self.VERSION, self.vanilla):
            obj.writeToRom(target)
        self._assert_region_equal(target, "ArmorDigivolution")

    def test_dna_digivolutions(self):
        target = bytearray(self.vanilla)
        objs, _ = loaders.loadDnaDigivolutions(self.VERSION, self.vanilla)
        for obj in objs:
            obj.writeToRom(target)
        self._assert_region_equal(target, "DNADigivolution")

    def test_sprite_map(self):
        target = bytearray(self.vanilla)
        for obj in loaders.loadSpriteMapTable(self.VERSION, self.vanilla):
            obj.writeToRom(target)
        self._assert_region_equal(target, "SpriteMapEntry")

    def test_battle_strings(self):
        target = bytearray(self.vanilla)
        for obj in loaders.loadBattleStringTable(self.VERSION, self.vanilla):
            obj.writeToRom(target)
        self._assert_region_equal(target, "BattleStringEntry")

    def test_habitats_worldmap(self):
        target = bytearray(self.vanilla)
        for obj in loaders.loadHabitatsWorldmap(self.VERSION, self.vanilla):
            obj.writeToRom(target)
        self._assert_region_equal(target, "HabitatWorldmap")

    def test_farm_terrains(self):
        target = bytearray(self.vanilla)
        for obj in loaders.loadFarmTerrains(self.VERSION, self.vanilla):
            obj.writeToRom(target)
        self._assert_region_equal(target, "FarmTerrain")

    def test_equipment(self):
        target = bytearray(self.vanilla)
        objs = loaders.loadEquipment(self.VERSION, self.vanilla)
        self.assertGreater(len(objs), 0, "no equipment parsed")
        for obj in objs.values():
            obj.writeToRom(target)
        self._assert_region_equal(target, "Equipment")

    def test_wild_encounter_areas(self):
        target = bytearray(self.vanilla)
        objs = loaders.loadWildEncounterAreas(self.VERSION, self.vanilla)
        self.assertGreater(len(objs), 0, "no wild encounter areas parsed")
        for obj in objs:
            obj.writeToRom(target)
        self._assert_region_equal(target, "WildEncounterArea")

    def test_strings(self):
        regions = constants.STRING_REGIONS.get(self.VERSION, [])
        if not regions:
            self.skipTest(f"no string regions defined for {self.VERSION}")
        target = bytearray(self.vanilla)
        total = 0
        for region_id, _start, _end, _label in regions:
            objs = loaders.loadStringRegion(self.VERSION, self.vanilla, region_id)
            total += len(objs)
            for obj in objs:
                obj.writeToRom(target)
        self.assertGreater(total, 0, "no strings parsed across any region")
        self._assert_region_equal(target, "GameString")


class DuskUsRoundtrip(RoundtripTestBase, unittest.TestCase):
    VERSION = "DUSK_US"


class DawnUsRoundtrip(RoundtripTestBase, unittest.TestCase):
    VERSION = "DAWN_US"


if __name__ == "__main__":
    unittest.main(verbosity=2)
