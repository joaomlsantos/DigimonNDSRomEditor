"""Round-trip tests for the .romproj string_edits channel (§12 Phase F).

`serialize_all` skips over-budget MSG.PAK strings — their grown encoded
bytes can't be written in place without corrupting neighbours. That means
the equal-length byte diff in .romproj can't carry them. The
``string_edits`` channel (project_file v2) closes the gap: Save Project
snapshots them as ``(region, vanilla_offset, text)`` triples, Open
Project replays them onto the reparsed model. The next ROM save then
runs them through the §12 grow path.

These tests assert two things:
1. A v1 .romproj (no string_edits field) loads cleanly with an empty list.
2. An over-budget MSG.PAK edit survives the save → load round-trip:
   the reloaded session's ROM bytes equal the original session's ROM bytes,
   and the reloaded session's string text matches the user's input.
"""
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PKG_PARENT = os.path.abspath(os.path.join(HERE, "..", ".."))
if PKG_PARENT not in sys.path:
    sys.path.insert(0, PKG_PARENT)

from editor import project_file  # noqa: E402
from editor.session import RomSession  # noqa: E402


ROM_PATHS = {
    "DUSK_US": r"C:\Workspace\digimon_stuffs\rom_files\1420 - Digimon World - Dusk (US).nds",
    "DAWN_US": r"C:\Workspace\digimon_stuffs\rom_files\1421 - Digimon World - Dawn (USA).nds",
}


class _ProjectMsgpakBase:
    VERSION: str = ""

    @classmethod
    def setUpClass(cls):
        path = ROM_PATHS[cls.VERSION]
        if not os.path.exists(path):
            raise unittest.SkipTest(f"ROM not found: {path}")
        cls.path = path

    def _session(self) -> RomSession:
        return RomSession.from_file(self.path)

    def _pick_growable(self, sess: RomSession):
        ms = sess.string_regions["msgpak_all"]
        return next(
            s for s in ms
            if 5 < len(s.text) < 30 and "[" not in s.text and "]" not in s.text
        )

    def test_v1_project_loads_with_empty_string_edits(self):
        # Hand-craft a v1 payload (no string_edits field) and confirm load
        # tolerates it without crashing.
        sess = self._session()
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "v1.romproj")
            payload = {
                "format_version": 1,
                "editor_version": "0.1.0",
                "rom_version": self.VERSION,
                "vanilla_rom_sha256": project_file.vanilla_sha256(
                    bytes(sess.original_rom_data)
                ),
                "qol": {},
                "diffs": [],
            }
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f)
            loaded = project_file.load_project(path)
        self.assertEqual(loaded["string_edits"], [])
        self.assertEqual(loaded["diffs"], [])

    def test_over_budget_msgpak_string_round_trips(self):
        sess = self._session()
        victim = self._pick_growable(sess)
        original_offset = victim.offset
        new_text = victim.text * 3
        victim.text = new_text
        self.assertFalse(victim.fits())

        # The snapshot the .romproj path will serialise.
        edits = sess.msgpak_string_edits()
        self.assertIn(
            ("msgpak_all", original_offset, new_text), edits,
            "over-budget edit missing from msgpak_string_edits()",
        )

        with tempfile.TemporaryDirectory() as td:
            proj_path = os.path.join(td, "edit.romproj")
            project_file.save_project(
                proj_path,
                rom_version=self.VERSION,
                vanilla_rom_data=bytes(sess.original_rom_data),
                edited_rom_data=bytes(sess.serialize_all()),
                qol=sess.qol,
                string_edits=edits,
            )
            loaded = project_file.load_project(proj_path)

        self.assertEqual(loaded["format_version"], 2)
        # string_edits must survive the JSON round-trip verbatim.
        self.assertIn(
            ("msgpak_all", original_offset, new_text), loaded["string_edits"],
        )

        # Rebuild a session from the project payload and reapply the channel.
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
        sess2.apply_string_edits(loaded["string_edits"])

        # Reloaded model carries the grown text at the same vanilla offset.
        match = next(
            (s for s in sess2.string_regions["msgpak_all"] if s.offset == original_offset),
            None,
        )
        self.assertIsNotNone(match, "string at original offset missing after reload")
        self.assertEqual(match.text, new_text)

        # Final invariant: ROM bytes produced by the reloaded session match
        # the original session's bytes byte-for-byte — proves the channel
        # carries all state the byte diff couldn't.
        out_original = bytes(sess.serialize_all_with_qol())
        out_reloaded = bytes(sess2.serialize_all_with_qol())
        self.assertEqual(out_reloaded, out_original)

    def test_unmodified_msgpak_strings_skip_string_edits_channel(self):
        # Reassigning the same value to a MSG.PAK string is treated as
        # unmodified (text == _initial_text), so it stays out of the
        # string_edits channel — otherwise every untouched session save
        # would write the entire MSG.PAK table into the channel.
        sess = self._session()
        ms = sess.string_regions["msgpak_all"]
        victim = next(s for s in ms if 5 < len(s.text) < 30 and "[" not in s.text)
        victim.text = victim.text  # no-op
        self.assertTrue(victim.fits())
        self.assertEqual(sess.msgpak_string_edits(), [])

    def test_in_budget_msgpak_edit_rides_string_edits_channel(self):
        # All MSG.PAK edits (grow, shrink, or same-length rename) go
        # through the string_edits channel now that ``serialize_all``
        # skips MSG.PAK in-place writes — the pad-and-write path leaked
        # `\x00` padding into the next string in the group.
        sess = self._session()
        ms = sess.string_regions["msgpak_all"]
        victim = next(s for s in ms if 8 < len(s.text) < 30 and "[" not in s.text)
        original_offset = victim.offset
        # Shrink by 2 chars — stays well in-budget.
        victim.text = victim.text[:-2]
        self.assertTrue(victim.fits())
        edits = sess.msgpak_string_edits()
        self.assertIn(("msgpak_all", original_offset, victim.text), edits)

    def test_apply_string_edits_rejects_unknown_offset(self):
        sess = self._session()
        with self.assertRaises(KeyError):
            sess.apply_string_edits([("msgpak_all", 0xDEADBEEF, "x")])

    def test_apply_string_edits_rejects_unknown_region(self):
        sess = self._session()
        with self.assertRaises(KeyError):
            sess.apply_string_edits([("not_a_region", 0, "x")])


class DuskUsProjectMsgpak(_ProjectMsgpakBase, unittest.TestCase):
    VERSION = "DUSK_US"


class DawnUsProjectMsgpak(_ProjectMsgpakBase, unittest.TestCase):
    VERSION = "DAWN_US"


if __name__ == "__main__":
    unittest.main(verbosity=2)
