"""Round-trip tests for wild-encounter slot add/remove (FAT resize + .romproj
``wild_encounter_area_edits`` channel).

Adding a slot grows the area's ``DAT/EC/E0XX.BIN`` past its vanilla FAT slot,
so it can't ride the equal-length byte diff. ``serialize_all`` splices it via
``_apply_wild_encounter_splice`` on ROM export; project save skips that splice
and routes the full area bytes through the v11 channel, replayed on load.

Asserts:
1. An added slot survives ``serialize_all`` — re-parsing the exported ROM
   shows the grown area, and a downstream EC file is byte-intact.
2. The engine cap (16) is enforced.
3. An added slot survives a project save -> load round-trip (reloaded ROM
   bytes equal the original session's).
4. Unmodified areas stay out of the channel.
"""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PKG_PARENT = os.path.abspath(os.path.join(HERE, "..", ".."))
if PKG_PARENT not in sys.path:
    sys.path.insert(0, PKG_PARENT)

from digimon_core import fnt, loaders, model  # noqa: E402
from editor import project_file  # noqa: E402
from editor.session import RomSession  # noqa: E402


ROM_PATHS = {
    "DUSK_US": r"C:\Workspace\digimon_stuffs\rom_files\1420 - Digimon World - Dusk (US).nds",
    "DAWN_US": r"C:\Workspace\digimon_stuffs\rom_files\1421 - Digimon World - Dawn (USA).nds",
}


class _WildSlotBase:
    VERSION: str = ""

    @classmethod
    def setUpClass(cls):
        path = ROM_PATHS[cls.VERSION]
        if not os.path.exists(path):
            raise unittest.SkipTest(f"ROM not found: {path}")
        cls.path = path

    def _session(self) -> RomSession:
        return RomSession.from_file(self.path)

    def _pick_area(self, sess: RomSession):
        # first area with headroom under the cap
        for ix, area in enumerate(sess.wild_encounter_areas):
            if area.can_add_encounter():
                return ix, area
        self.skipTest("no area with encounter headroom")

    def test_add_slot_survives_serialize(self):
        sess = self._session()
        ix, area = self._pick_area(sess)
        before = len(area.encounters)
        enc = area.add_encounter()
        enc.digimon_id = 0x0123
        enc.spawn_chance = 45
        self.assertTrue(area.is_resized)

        out = bytes(sess.serialize_all())
        # ROM stayed 0x200-aligned; downstream files must still resolve.
        ft = fnt.FileTable.from_rom(out)
        reparsed = loaders.loadWildEncounterAreas(self.VERSION, bytearray(out), file_table=ft)
        self.assertEqual(len(reparsed[ix].encounters), before + 1)
        self.assertEqual(reparsed[ix].num_encounters, before + 1)
        self.assertEqual(reparsed[ix].encounters[-1].digimon_id, 0x0123)
        self.assertEqual(reparsed[ix].encounters[-1].spawn_chance, 45)
        # a different, unedited area is byte-identical to vanilla
        other = next(j for j in range(len(sess.wild_encounter_areas)) if j != ix)
        self.assertEqual(
            reparsed[other].getByteArray(),
            sess.wild_encounter_areas[other].getByteArray(),
        )

    def test_engine_cap_enforced(self):
        sess = self._session()
        _ix, area = self._pick_area(sess)
        while area.can_add_encounter():
            area.add_encounter()
        self.assertEqual(len(area.encounters), model.WildEncounterArea.MAX_ENCOUNTERS)
        with self.assertRaises(ValueError):
            area.add_encounter()

    def test_add_remove_restores_vanilla_bytes(self):
        sess = self._session()
        _ix, area = self._pick_area(sess)
        vanilla = bytes(area.getByteArray())
        enc = area.add_encounter()
        area.remove_encounter(area.encounters.index(enc))
        self.assertFalse(area.is_resized)
        self.assertEqual(bytes(area.getByteArray()), vanilla)

    def test_unmodified_areas_skip_channel(self):
        sess = self._session()
        self.assertEqual(sess.wild_encounter_area_edits(), [])

    def test_add_slot_project_round_trips(self):
        sess = self._session()
        ix, area = self._pick_area(sess)
        enc = area.add_encounter()
        enc.digimon_id = 0x0155
        enc.spawn_chance = 60
        enc.reward_slot = 7

        edits = sess.wild_encounter_area_edits()
        self.assertTrue(any(a_ix == ix for a_ix, _ in edits))

        edited = bytes(sess.serialize_all(
            skip_sprite_splice=True, skip_btmap_splice=True, skip_map_splice=True,
            skip_overlay5_splice=True, skip_sound_splice=True,
            skip_wild_encounter_splice=True,
        ))
        # project byte diff requires an equal-length ROM
        self.assertEqual(len(edited), len(sess.original_rom_data))

        with tempfile.TemporaryDirectory() as td:
            proj_path = os.path.join(td, "wild.romproj")
            project_file.save_project(
                proj_path,
                rom_version=self.VERSION,
                vanilla_rom_data=bytes(sess.original_rom_data),
                edited_rom_data=edited,
                qol=sess.qol,
                wild_encounter_area_edits=edits,
            )
            loaded = project_file.load_project(proj_path)

        self.assertEqual(loaded["format_version"], project_file.FORMAT_VERSION)
        self.assertTrue(any(a_ix == ix for a_ix, _ in loaded["wild_encounter_area_edits"]))

        patched = bytearray(sess.original_rom_data)
        project_file.apply_byte_diff(patched, loaded["diffs"])
        sess2 = RomSession.from_project(
            project_path=proj_path,
            vanilla_path=self.path,
            vanilla_data=bytes(sess.original_rom_data),
            version=self.VERSION,
            patched_data=bytes(patched),
            qol_settings=loaded["qol"],
        )
        sess2.apply_wild_encounter_area_edits(loaded["wild_encounter_area_edits"])

        # reloaded model carries the added slot
        self.assertEqual(
            len(sess2.wild_encounter_areas[ix].encounters),
            len(sess.wild_encounter_areas[ix].encounters),
        )
        self.assertEqual(sess2.wild_encounter_areas[ix].encounters[-1].digimon_id, 0x0155)

        # final invariant: reloaded ROM bytes == original session's ROM bytes
        self.assertEqual(
            bytes(sess2.serialize_all_with_qol()),
            bytes(sess.serialize_all_with_qol()),
        )


class DuskUsWildSlots(_WildSlotBase, unittest.TestCase):
    VERSION = "DUSK_US"


class DawnUsWildSlots(_WildSlotBase, unittest.TestCase):
    VERSION = "DAWN_US"


if __name__ == "__main__":
    unittest.main(verbosity=2)
