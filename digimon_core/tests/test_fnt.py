"""FileTable parse + resolve tests.

For each supported ROM, parse the FNT/FAT and confirm:
  - ``DAT/MSG.PAK`` resolves to the same byte offset hardcoded in
    ``constants.STRING_REGIONS`` (msgpak_all region start, minus the
    0x570-byte MSG.PAK file header).
  - The data subdirectories the editor needs to address (``DAT/dm/``,
    ``DAT/en/``, ``DAT/ec/``, ``DAT/eq/``, ``DAT/sk/``) each contain at
    least one file.
  - At least one ``DAT/SPR_*.PAK`` exists.
  - Path lookups are case-insensitive (callers shouldn't have to memorise
    DWDD's exact filesystem casing).

DWDD lays content under a single ``DAT/`` root subdirectory; bare paths
like ``MSG.PAK`` don't exist in the FNT. The §13 plan's path references
(``/dm/``, ``/en/``, etc.) are shorthand for the actual ``DAT/...`` paths.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PKG_PARENT = os.path.abspath(os.path.join(HERE, "..", ".."))
if PKG_PARENT not in sys.path:
    sys.path.insert(0, PKG_PARENT)

from digimon_core import fnt, rom  # noqa: E402


ROM_DIR = r"C:\Workspace\digimon_stuffs\rom_files"
ROM_PATHS = {
    "DUSK_US": os.path.join(ROM_DIR, "1420 - Digimon World - Dusk (US).nds"),
    "DAWN_US": os.path.join(ROM_DIR, "1421 - Digimon World - Dawn (USA).nds"),
}

# Expected MSG.PAK file start = msgpak_all region start - 0x570 (file header).
MSGPAK_FILE_START = {
    "DUSK_US": 0x0117E400,
    "DAWN_US": 0x0117E200,
}


class FntTestBase:
    VERSION: str = ""

    @classmethod
    def setUpClass(cls):
        path = ROM_PATHS[cls.VERSION]
        if not os.path.exists(path):
            raise unittest.SkipTest(f"ROM not found: {path}")
        cls.rom = rom.loadRom(path)
        detected = rom.detectVersion(cls.rom, path)
        if detected != cls.VERSION:
            raise unittest.SkipTest(
                f"ROM at {path} has version {detected}, expected {cls.VERSION}"
            )
        cls.table = fnt.FileTable.from_rom(cls.rom)

    def test_msgpak_resolves_to_expected_start(self):
        self.assertIn("DAT/MSG.PAK", self.table, "MSG.PAK missing from FNT")
        start, end = self.table.resolve("DAT/MSG.PAK")
        self.assertEqual(
            start, MSGPAK_FILE_START[self.VERSION],
            f"MSG.PAK start mismatch: FNT says 0x{start:08X}, "
            f"hardcoded says 0x{MSGPAK_FILE_START[self.VERSION]:08X}"
        )
        self.assertGreater(end, start)
        # MSG.PAK header is the first 0x570 bytes; sanity-check it's present.
        self.assertGreaterEqual(end - start, 0x570)

    def test_data_subdirectories_populated(self):
        for sub in ("DAT/DM/", "DAT/EN/", "DAT/EC/", "DAT/EQ/", "DAT/SK/"):
            matches = [p for p in self.table.paths() if p.startswith(sub)]
            self.assertGreater(
                len(matches), 0,
                f"no files found under {sub} in {self.VERSION} FNT"
            )

    def test_sprite_paks_present(self):
        spr = [p for p in self.table.paths()
               if p.startswith("DAT/SPR_") and p.endswith(".PAK")]
        self.assertGreater(len(spr), 0, "no DAT/SPR_*.PAK entries in FNT")

    def test_lookup_is_case_insensitive(self):
        upper = self.table.resolve("DAT/MSG.PAK")
        lower = self.table.resolve("dat/msg.pak")
        mixed = self.table.resolve("Dat/Msg.Pak")
        leading_slash = self.table.resolve("/DAT/MSG.PAK")
        self.assertEqual(upper, lower)
        self.assertEqual(upper, mixed)
        self.assertEqual(upper, leading_slash)

    def test_slice_returns_file_bytes(self):
        start, end = self.table.resolve("DAT/MSG.PAK")
        sliced = self.table.slice(self.rom, "DAT/MSG.PAK")
        self.assertEqual(len(sliced), end - start)
        self.assertEqual(sliced[:16], bytes(self.rom[start:start + 16]))


class DuskUsFnt(FntTestBase, unittest.TestCase):
    VERSION = "DUSK_US"


class DawnUsFnt(FntTestBase, unittest.TestCase):
    VERSION = "DAWN_US"


if __name__ == "__main__":
    unittest.main(verbosity=2)
