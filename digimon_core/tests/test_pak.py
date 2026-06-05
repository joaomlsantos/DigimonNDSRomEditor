"""PakFile parse / rebuild tests + MSG.PAK entry sub-format tests.

The invariant the §12 grow path leans on is that ``PakFile(raw).to_bytes()``
returns ``raw`` when no entries have been replaced — that's what keeps a
no-edit save byte-identical. We also exercise the replace-and-grow path
on a real MSG.PAK entry to make sure ``rebuild_msgpak_entry`` reproduces
the sub-header layout the engine expects.
"""
import os
import struct
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PKG_PARENT = os.path.abspath(os.path.join(HERE, "..", ".."))
if PKG_PARENT not in sys.path:
    sys.path.insert(0, PKG_PARENT)

from digimon_core import fnt, pak, rom  # noqa: E402


ROM_DIR = r"C:\Workspace\digimon_stuffs\rom_files"
ROM_PATHS = {
    "DUSK_US": os.path.join(ROM_DIR, "1420 - Digimon World - Dusk (US).nds"),
    "DAWN_US": os.path.join(ROM_DIR, "1421 - Digimon World - Dawn (USA).nds"),
}


def _load_msgpak(path: str) -> bytes:
    rom_bytes = bytes(rom.loadRom(path))
    ft = fnt.FileTable.from_rom(rom_bytes)
    start, end = ft.resolve("DAT/MSG.PAK")
    return rom_bytes[start:end]


class PakFileTests(unittest.TestCase):
    def test_parse_msgpak_dusk(self):
        if not os.path.exists(ROM_PATHS["DUSK_US"]):
            self.skipTest("Dusk ROM missing")
        raw = _load_msgpak(ROM_PATHS["DUSK_US"])
        p = pak.PakFile(raw)
        self.assertEqual(p.count, 138)
        # Every entry's stored size + offset must lie inside the pak.
        for i in range(p.count):
            o, e = p.original_entry_range(i)
            self.assertGreaterEqual(o, p.header_size)
            self.assertLessEqual(e, len(raw))
            self.assertEqual(len(p.entries[i]), e - o)

    def test_roundtrip_no_changes(self):
        for version, path in ROM_PATHS.items():
            if not os.path.exists(path):
                continue
            raw = _load_msgpak(path)
            with self.subTest(version=version):
                self.assertEqual(pak.PakFile(raw).to_bytes(), raw)

    def test_replace_entry_byte_identical_when_same_bytes(self):
        if not os.path.exists(ROM_PATHS["DUSK_US"]):
            self.skipTest("Dusk ROM missing")
        raw = _load_msgpak(ROM_PATHS["DUSK_US"])
        p = pak.PakFile(raw)
        # Replace every entry with its own bytes — flag preserved, offsets
        # recomputed but identical because nothing changed size.
        for i in range(p.count):
            p.replace_entry(i, p.original_entry(i))
        self.assertEqual(p.to_bytes(), raw)

    def test_grow_entry_shifts_downstream_offsets(self):
        if not os.path.exists(ROM_PATHS["DUSK_US"]):
            self.skipTest("Dusk ROM missing")
        raw = _load_msgpak(ROM_PATHS["DUSK_US"])
        p = pak.PakFile(raw)
        original_e3_start, original_e3_end = p.original_entry_range(3)
        original_e4_start, _ = p.original_entry_range(4)
        # Grow entry 3 by 16 bytes.
        new_e3 = p.original_entry(3) + b"\xAA" * 16
        p.replace_entry(3, new_e3)
        rebuilt = p.to_bytes()
        # Parse the rebuild fresh and check the directory.
        new_p = pak.PakFile(rebuilt)
        new_e3_start, new_e3_end = new_p.original_entry_range(3)
        new_e4_start, _ = new_p.original_entry_range(4)
        # entry 0..2 untouched.
        for i in range(3):
            self.assertEqual(
                new_p.original_entry_range(i),
                p.original_entry_range(i),
                f"entry {i} shifted unexpectedly",
            )
        # entry 3 starts where it did, ends 16 bytes later.
        self.assertEqual(new_e3_start, original_e3_start)
        self.assertEqual(new_e3_end, original_e3_end + 16)
        # entry 4 starts 16 bytes later.
        self.assertEqual(new_e4_start, original_e4_start + 16)
        # And the entry 3 bytes round-trip.
        self.assertEqual(new_p.original_entry(3), new_e3)

    def test_entry_index_at(self):
        if not os.path.exists(ROM_PATHS["DUSK_US"]):
            self.skipTest("Dusk ROM missing")
        raw = _load_msgpak(ROM_PATHS["DUSK_US"])
        p = pak.PakFile(raw)
        # Probed in the design walkthrough: entry 0 lives at file_offset 0x454.
        self.assertEqual(p.entry_index_at(0x454), 0)
        self.assertEqual(p.entry_index_at(0x500), 0)  # inside entry 0
        self.assertEqual(p.entry_index_at(0xD10), 1)  # entry 1 start
        with self.assertRaises(ValueError):
            p.entry_index_at(0)  # in the pak header, not any entry


class MsgpakEntryFormatTests(unittest.TestCase):
    def setUp(self):
        if not os.path.exists(ROM_PATHS["DUSK_US"]):
            self.skipTest("Dusk ROM missing")
        raw = _load_msgpak(ROM_PATHS["DUSK_US"])
        self.pak = pak.PakFile(raw)

    def test_parse_entry_0_groups(self):
        # Entry 0: sub-header at offset 0, first u32 = 0x11C => 71 groups.
        groups = pak.parse_msgpak_entry_groups(self.pak.original_entry(0))
        self.assertEqual(len(groups), 71)
        # First group starts at the sub-header end (0x11C).
        self.assertEqual(groups[0][0], 0x11C)
        # Sequential, no overlaps, last ends at entry size.
        for (s, e), (ns, _) in zip(groups, groups[1:]):
            self.assertEqual(e, ns)
            self.assertGreater(e, s)
        self.assertEqual(groups[-1][1], len(self.pak.original_entry(0)))

    def test_groups_end_in_ff_ff(self):
        # Each group's last 2 bytes should be FF FF.
        groups = pak.parse_msgpak_entry_groups(self.pak.original_entry(0))
        e = self.pak.original_entry(0)
        for s, end in groups:
            self.assertEqual(e[end - 2:end], b"\xFF\xFF",
                             f"group [0x{s:x}..0x{end:x}) not FF FF terminated")

    def test_rebuild_entry_byte_identical_when_groups_unchanged(self):
        # Take entry 0's groups verbatim, rebuild, expect byte-identical.
        e = self.pak.original_entry(0)
        groups = pak.parse_msgpak_entry_groups(e)
        payloads = [e[s:end] for s, end in groups]
        rebuilt = pak.rebuild_msgpak_entry(payloads)
        self.assertEqual(rebuilt, e)

    def test_rebuild_entry_grows_one_group(self):
        e = self.pak.original_entry(0)
        groups = pak.parse_msgpak_entry_groups(e)
        payloads = [e[s:end] for s, end in groups]
        # Grow group 2 by 8 bytes (just append FF FF padding before the
        # original terminator and replace its terminator — we don't actually
        # need a valid string, just a longer payload).
        grown = b"\xFF\xFF" * 4 + payloads[2]
        payloads[2] = grown
        rebuilt = pak.rebuild_msgpak_entry(payloads)
        new_groups = pak.parse_msgpak_entry_groups(rebuilt)
        # Same group count, group 2 is 8 bytes longer, downstream offsets shifted.
        self.assertEqual(len(new_groups), len(groups))
        old_s2, old_e2 = groups[2]
        new_s2, new_e2 = new_groups[2]
        self.assertEqual(new_s2, old_s2)  # group 2 start unchanged
        self.assertEqual(new_e2 - new_s2, (old_e2 - old_s2) + 8)
        # Group 3 (and later) shifted by +8.
        self.assertEqual(new_groups[3][0], groups[3][0] + 8)
        self.assertEqual(len(rebuilt), len(e) + 8)


if __name__ == "__main__":
    unittest.main(verbosity=2)
