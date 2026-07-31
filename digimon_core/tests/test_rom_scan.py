"""Tests for the donor-ROM SDAT scanner (digimon_core/sound/rom_scan.py).

Vanilla Dusk/Dawn each ship exactly one SDAT (``DAT/snd/sound_data.sdat``);
the scan must find it by magic and its byte range must match the FAT's own
resolution of that path. Synthetic buffers cover the not-an-NDS and
no-SDAT-present paths.
"""
import os
import struct
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PKG_PARENT = os.path.abspath(os.path.join(HERE, "..", ".."))
if PKG_PARENT not in sys.path:
    sys.path.insert(0, PKG_PARENT)

from digimon_core import fnt
from digimon_core.sound import rom_scan

ROM_DIR = r"C:\Workspace\digimon_stuffs\rom_files"
ROM_PATHS = {
    "DUSK_US": os.path.join(ROM_DIR, "1420 - Digimon World - Dusk (US).nds"),
    "DAWN_US": os.path.join(ROM_DIR, "1421 - Digimon World - Dawn (USA).nds"),
}


class VanillaRomScanTests(unittest.TestCase):
    VERSION = "DUSK_US"

    @classmethod
    def setUpClass(cls):
        path = ROM_PATHS[cls.VERSION]
        if not os.path.exists(path):
            raise unittest.SkipTest(f"ROM not found: {path}")
        with open(path, "rb") as fh:
            cls.rom = fh.read()

    def test_finds_the_sound_archive(self):
        found = rom_scan.find_sdats(self.rom)
        self.assertGreaterEqual(len(found), 1)
        # Every hit really starts with the magic.
        for s in found:
            self.assertEqual(self.rom[s.start:s.start + 4], b"SDAT")
            self.assertEqual(s.size, s.end - s.start)

    def test_range_matches_fat_resolution(self):
        ft = fnt.FileTable.from_rom(self.rom)
        start, end = ft.resolve("DAT/snd/sound_data.sdat")
        matches = [s for s in rom_scan.find_sdats(self.rom)
                   if s.start == start and s.end == end]
        self.assertEqual(len(matches), 1)
        self.assertIsNotNone(matches[0].path)
        self.assertTrue(matches[0].path.upper().endswith("SOUND_DATA.SDAT"))


class DawnRomScanTests(VanillaRomScanTests):
    VERSION = "DAWN_US"


class SyntheticRomScanTests(unittest.TestCase):
    def _fake_rom(self, files):
        """Build a minimal NDS-ish buffer: a header with a FAT plus the given
        file payloads. FNT is left empty (offset 0) so name resolution no-ops
        and the magic scan is exercised on its own."""
        header = bytearray(0x200)
        payload = bytearray()
        fat = bytearray()
        base = 0x400
        for content in files:
            start = base + len(payload)
            payload += content
            end = start + len(content)
            fat += struct.pack("<II", start, end)
        fat_off = 0x400 + len(payload)
        struct.pack_into("<I", header, 0x40, 0)          # FNT offset (unused)
        struct.pack_into("<II", header, 0x48, fat_off, len(fat))
        rom = bytearray(0x400)
        rom[0:len(header)] = header
        rom[0x400:0x400 + len(payload)] = payload
        rom += fat
        return bytes(rom)

    def test_finds_sdat_by_magic_without_names(self):
        rom = self._fake_rom([b"SDAT" + b"\x00" * 60, b"NOPE" + b"\x00" * 12])
        found = rom_scan.find_sdats(rom)
        self.assertEqual(len(found), 1)
        self.assertIsNone(found[0].path)
        self.assertEqual(found[0].size, 64)

    def test_no_sdat_present_returns_empty(self):
        rom = self._fake_rom([b"NARC" + b"\x00" * 12])
        self.assertEqual(rom_scan.find_sdats(rom), [])

    def test_too_small_raises(self):
        with self.assertRaises(ValueError):
            rom_scan.find_sdats(b"\x00" * 16)

    def test_no_fat_raises(self):
        with self.assertRaises(ValueError):
            rom_scan.find_sdats(b"\x00" * 0x200)


if __name__ == "__main__":
    unittest.main()
