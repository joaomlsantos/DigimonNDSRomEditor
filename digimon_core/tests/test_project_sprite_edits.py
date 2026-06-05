"""Round-trip tests for the .romproj sprite_edits channel (v3).

Sprite PAK replacements live in their own channel rather than the byte diff:
a grown entry triggers a fat-shift across every later file, which would
otherwise bloat the diff to span the whole rest of the ROM. ``save_project``
passes ``skip_sprite_splice=True`` to ``serialize_all`` so the byte diff stays
small, and snapshots each modified entry into ``sprite_edits``. ``Open
Project`` replays them via ``apply_sprite_pak_edits``.

These tests assert:
1. A v2 .romproj (no sprite_edits field) loads cleanly with an empty list.
2. A replaced SPR_CHR entry survives save → load:
   - the channel carries the new bytes verbatim,
   - the reloaded session's serialized ROM equals the original's byte-for-byte.
3. apply_sprite_pak_edits rejects out-of-range entry indices.
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

SPR_CHR = "DAT/SPR_CHR.PAK"


class _ProjectSpriteEditsBase:
    VERSION: str = ""

    @classmethod
    def setUpClass(cls):
        path = ROM_PATHS[cls.VERSION]
        if not os.path.exists(path):
            raise unittest.SkipTest(f"ROM not found: {path}")
        cls.path = path

    def _session(self) -> RomSession:
        return RomSession.from_file(self.path)

    def _mutate_chr_entry(self, sess: RomSession, idx: int) -> bytes:
        """Flip a byte in entry ``idx`` of SPR_CHR and return the new bytes.

        Same-length swap keeps the splice on the cheap in-budget path while
        still proving the channel actually carries the user's edit.
        """
        chr_pak = sess.sprite_pak(SPR_CHR)
        original = bytes(chr_pak.entries[idx])
        # Pick a byte well past the RAHC header so the NCGR still parses.
        mutated = bytearray(original)
        mutated[-1] ^= 0xFF
        new_bytes = bytes(mutated)
        chr_pak.replace_entry(idx, new_bytes)
        sess.mark_sprite_pak_dirty(SPR_CHR)
        return new_bytes

    def test_v2_project_loads_with_empty_sprite_edits(self):
        # Hand-craft a v2 payload (no sprite_edits field) and confirm load
        # tolerates it without crashing.
        sess = self._session()
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "v2.romproj")
            payload = {
                "format_version": 2,
                "editor_version": "0.1.0",
                "rom_version": self.VERSION,
                "vanilla_rom_sha256": project_file.vanilla_sha256(
                    bytes(sess.original_rom_data)
                ),
                "qol": {},
                "diffs": [],
                "string_edits": [],
            }
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f)
            loaded = project_file.load_project(path)
        self.assertEqual(loaded["sprite_edits"], [])

    def test_chr_entry_replacement_round_trips(self):
        sess = self._session()
        new_bytes = self._mutate_chr_entry(sess, idx=0)

        edits = sess.sprite_pak_edits()
        self.assertIn((SPR_CHR, 0, new_bytes), edits)

        with tempfile.TemporaryDirectory() as td:
            proj_path = os.path.join(td, "edit.romproj")
            project_file.save_project(
                proj_path,
                rom_version=self.VERSION,
                vanilla_rom_data=bytes(sess.original_rom_data),
                edited_rom_data=bytes(
                    sess.serialize_all(skip_sprite_splice=True)
                ),
                qol=sess.qol,
                sprite_edits=edits,
            )
            loaded = project_file.load_project(proj_path)

        self.assertEqual(loaded["format_version"], 3)
        self.assertIn((SPR_CHR, 0, new_bytes), loaded["sprite_edits"])

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
        sess2.apply_sprite_pak_edits(loaded["sprite_edits"])

        # Reloaded pak carries the new bytes at the same entry index.
        self.assertEqual(
            bytes(sess2.sprite_pak(SPR_CHR).entries[0]), new_bytes,
        )

        # Final invariant: ROM bytes produced by the reloaded session match
        # the original session's bytes byte-for-byte.
        out_original = bytes(sess.serialize_all_with_qol())
        out_reloaded = bytes(sess2.serialize_all_with_qol())
        self.assertEqual(out_reloaded, out_original)

    def test_unmodified_sprite_paks_skip_channel(self):
        # Touching ``sprite_pak`` but not editing any entry must not produce
        # spurious channel entries — otherwise opening a sprite for preview
        # would dirty the pak.
        sess = self._session()
        sess.sprite_pak(SPR_CHR)  # load into cache, no mutation
        self.assertEqual(sess.sprite_pak_edits(), [])

    def test_apply_sprite_pak_edits_rejects_out_of_range_idx(self):
        sess = self._session()
        with self.assertRaises(IndexError):
            sess.apply_sprite_pak_edits([(SPR_CHR, 10_000_000, b"\x00")])


class DuskUsProjectSpriteEdits(_ProjectSpriteEditsBase, unittest.TestCase):
    VERSION = "DUSK_US"


class DawnUsProjectSpriteEdits(_ProjectSpriteEditsBase, unittest.TestCase):
    VERSION = "DAWN_US"


if __name__ == "__main__":
    unittest.main(verbosity=2)
