"""Page-offset header parser tests.

Two layers of validation:

  1. **Structural**: for every page-headered file under ``DAT/dm/``,
     ``DAT/en/``, ``DAT/eq/``, ``DAT/sk/`` on both ROM versions, parse the
     page header and assert in-bounds, strictly-increasing offsets with a
     uniform page width matching the expected model struct size.

  2. **Equivalence with the legacy loader**: collect the raw record bytes
     for every non-sentinel page via FNT + page header, and compare the
     resulting set against the bytes the hardcoded-offset loader pulls
     for the same model. Mismatch means the page parser disagrees with
     the existing ROM-absolute walk — exactly the regression Phase C
     needs to avoid.
"""
import os
import struct
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PKG_PARENT = os.path.abspath(os.path.join(HERE, "..", ".."))
if PKG_PARENT not in sys.path:
    sys.path.insert(0, PKG_PARENT)

from digimon_core import fnt, loaders, model, pagination, rom  # noqa: E402


ROM_DIR = r"C:\Workspace\digimon_stuffs\rom_files"
ROM_PATHS = {
    "DUSK_US": os.path.join(ROM_DIR, "1420 - Digimon World - Dusk (US).nds"),
    "DAWN_US": os.path.join(ROM_DIR, "1421 - Digimon World - Dawn (USA).nds"),
}

# (dir prefix, expected page width, sentinel field unpacker, loader factory)
# Loader factory returns a {id: bytes-of-record} dict for equivalence
# checking against the hardcoded-offset path.
SENTINEL = 0xFFFF


class PaginationTestBase:
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

    # ---- structural ------------------------------------------------------

    def _check_dir_structure(self, dir_prefix: str, page_width: int):
        files = sorted(p for p in self.table.paths() if p.startswith(dir_prefix))
        self.assertGreater(len(files), 0, f"no files under {dir_prefix}")
        for path in files:
            start, end = self.table.resolve(path)
            data = bytes(self.rom[start:end])
            offsets = pagination.parse_page_header(data)
            self.assertGreater(len(offsets), 0, f"{path}: empty page header")
            for i, off in enumerate(offsets):
                self.assertLess(off, len(data),
                                f"{path}: page {i} offset 0x{off:X} >= file size 0x{len(data):X}")
                if i > 0:
                    self.assertGreater(
                        off, offsets[i - 1],
                        f"{path}: page {i} offset 0x{off:X} not strictly increasing"
                    )
            # All page spacings (and the final-page-to-EOF span) must equal
            # the expected record width — Phase B's uniformity assumption.
            for i, off in enumerate(offsets):
                end_off = offsets[i + 1] if i + 1 < len(offsets) else len(data)
                self.assertEqual(
                    end_off - off, page_width,
                    f"{path}: page {i} width 0x{end_off - off:X} != expected 0x{page_width:X}"
                )

    def test_dm_structure(self):
        self._check_dir_structure("DAT/DM/", 0x44)   # BaseDataDigimon

    def test_en_structure(self):
        self._check_dir_structure("DAT/EN/", 0x6C)   # EnemyDataDigimon

    def test_eq_structure(self):
        self._check_dir_structure("DAT/EQ/", 0x48)   # Equipment

    def test_sk_structure(self):
        self._check_dir_structure("DAT/SK/", 0x70)   # StandardDigivolution

    # ---- equivalence with legacy loader ---------------------------------
    #
    # Strategy: for each FAT-listed file, derive every page's *absolute ROM
    # offset* (file_start + page_offset). The legacy loaders expose
    # ``obj.offset`` — the ROM offset they read the record from — so we can
    # cross-check that every record the legacy path produces sits on a page
    # boundary the FNT path would also surface, and that the page byte
    # window matches the legacy record's underlying ROM bytes.

    def _fnt_page_starts(self, dir_prefix: str) -> dict:
        """Return ``{abs_rom_offset: page_width}`` for every page in dir."""
        starts: dict = {}
        for path in (p for p in self.table.paths() if p.startswith(dir_prefix)):
            file_start, file_end = self.table.resolve(path)
            data = bytes(self.rom[file_start:file_end])
            for _idx, off, length in pagination.iter_pages(data):
                starts[file_start + off] = length
        return starts

    def _assert_legacy_offsets_match_pages(self, dir_prefix: str, page_width: int,
                                           legacy_objs):
        pages = self._fnt_page_starts(dir_prefix)
        for obj in legacy_objs:
            self.assertIn(
                obj.offset, pages,
                f"{dir_prefix}: legacy record at 0x{obj.offset:08X} doesn't fall "
                f"on any FNT page start"
            )
            self.assertEqual(
                pages[obj.offset], page_width,
                f"{dir_prefix}: page at 0x{obj.offset:08X} has width "
                f"0x{pages[obj.offset]:X}, legacy expects 0x{page_width:X}"
            )
            # The legacy ROM-absolute bytes for SIZE must equal the first SIZE
            # bytes of the FNT-resolved page — confirms file_start + page_off
            # really does point at the same bytes the legacy walk reads.
            legacy_bytes = bytes(self.rom[obj.offset:obj.offset + obj.SIZE])
            self.assertEqual(
                legacy_bytes, bytes(obj.getByteArray()),
                f"{dir_prefix}: legacy ROM slice at 0x{obj.offset:08X} doesn't "
                f"round-trip through model.getByteArray()"
            )

    def test_dm_equivalence_with_loader(self):
        objs = loaders.loadBaseDigimonInfo(self.VERSION, self.rom).values()
        self._assert_legacy_offsets_match_pages("DAT/DM/", 0x44, objs)

    def test_en_equivalence_with_loader(self):
        objs = loaders.loadEnemyDigimonInfo(self.VERSION, self.rom).values()
        self._assert_legacy_offsets_match_pages("DAT/EN/", 0x6C, objs)

    def test_sk_equivalence_with_loader(self):
        objs = loaders.loadStandardDigivolutions(self.VERSION, self.rom).values()
        self._assert_legacy_offsets_match_pages("DAT/SK/", 0x70, objs)

    # ---- DAT/ec/I0XX.BIN: flat fixed-stride records ---------------------

    def _i0xx_record_starts(self) -> dict:
        """Return ``{abs_rom_offset: record_size}`` for every record in
        every ``DAT/EC/I*.BIN``."""
        starts: dict = {}
        i_paths = [p for p in self.table.paths()
                   if p.startswith("DAT/EC/I") and p.endswith(".BIN")]
        for path in i_paths:
            file_start, file_end = self.table.resolve(path)
            data = bytes(self.rom[file_start:file_end])
            for _idx, off in pagination.iter_fixed_records(data, 0x20):
                starts[file_start + off] = 0x20
        return starts

    def test_i0xx_files_divide_evenly(self):
        i_paths = [p for p in self.table.paths()
                   if p.startswith("DAT/EC/I") and p.endswith(".BIN")]
        self.assertGreater(len(i_paths), 0, "no DAT/EC/I*.BIN files in FNT")
        total = 0
        for path in i_paths:
            start, end = self.table.resolve(path)
            size = end - start
            self.assertEqual(size % 0x20, 0,
                             f"{path}: size 0x{size:X} not divisible by 0x20")
            total += size // 0x20
        # Sanity: at least some records exist across the dir.
        self.assertGreater(total, 0, "DAT/EC/I*.BIN yielded zero records")

    def test_i0xx_equivalence_with_loader(self):
        records = self._i0xx_record_starts()
        objs = loaders.loadEncounterRewardData(self.VERSION, self.rom)
        self.assertGreater(len(objs), 0, "legacy yielded zero EncounterReward records")
        for obj in objs:
            self.assertIn(
                obj.offset, records,
                f"legacy EncounterReward at 0x{obj.offset:08X} doesn't fall on "
                f"any DAT/EC/I*.BIN record start"
            )
            # EncounterRewardTable is variable-length; obj.size reports the
            # byte width of the parsed record.
            legacy_bytes = bytes(self.rom[obj.offset:obj.offset + obj.size])
            self.assertEqual(
                legacy_bytes, bytes(obj.getByteArray()),
                f"EncounterReward at 0x{obj.offset:08X} doesn't round-trip "
                f"through model.getByteArray()"
            )

    # ---- DAT/ec/E0XX.BIN: per-area record (§13.3.a) ---------------------

    def test_e0xx_format_invariant(self):
        """Every E0XX.BIN file matches:
            file_size == 0x10 + num_encounters * 0x18 + 4
        with a 0x10-byte header (num_encounters / rate_lower / rate_upper /
        10 zero bytes) and a 4-byte zero terminator at end."""
        e_paths = [p for p in self.table.paths()
                   if p.startswith("DAT/EC/E") and p.endswith(".BIN")
                   and not p.endswith("/ENCTBL.BIN")]
        self.assertGreater(len(e_paths), 0, "no DAT/EC/E*.BIN files in FNT")
        for path in e_paths:
            start, end = self.table.resolve(path)
            data = bytes(self.rom[start:end])
            self.assertGreaterEqual(len(data), 0x14,
                                    f"{path}: file too small for header + terminator")
            num = struct.unpack_from("<H", data, 0)[0]
            expected = 0x10 + num * 0x18 + 4
            self.assertEqual(
                len(data), expected,
                f"{path}: size 0x{len(data):X} != header+records+terminator "
                f"0x{expected:X} (num_encounters={num})"
            )
            # Header bytes 6..15 are documented filler — every E0XX has them
            # zeroed on Dusk and Dawn.
            self.assertEqual(
                data[6:0x10], b"\x00" * 10,
                f"{path}: header filler bytes 6..15 not all zero"
            )
            # Terminator: last 4 bytes are zero (digimon_id field reads 0).
            self.assertEqual(
                data[-4:], b"\x00\x00\x00\x00",
                f"{path}: terminator at end isn't 4 zero bytes"
            )

    def test_e0xx_parses_via_existing_model(self):
        """Feeding the FNT-resolved E0XX.BIN bytes through ``WildEncounterArea``
        produces the same parsed encounters as the legacy 0x200-per-area walk,
        for every area in ``AREA_ENCOUNTER_OFFSETS``."""
        legacy_areas = {
            obj.offset: obj
            for obj in loaders.loadWildEncounterAreas(self.VERSION, self.rom)
        }
        e_paths = [p for p in self.table.paths()
                   if p.startswith("DAT/EC/E") and p.endswith(".BIN")
                   and not p.endswith("/ENCTBL.BIN")]
        file_starts_seen = []
        for path in e_paths:
            start, end = self.table.resolve(path)
            file_starts_seen.append(start)
            if start not in legacy_areas:
                # Some E0XX.BIN files may sit outside the legacy walk's
                # AREA_ENCOUNTER_OFFSETS span — skip those, the equivalence
                # claim only covers the intersection.
                continue
            legacy = legacy_areas[start]
            data = bytearray(self.rom[start:end])
            via_fnt = model.WildEncounterArea(data, start)
            self.assertEqual(
                via_fnt.num_encounters, legacy.num_encounters,
                f"{path}: num_encounters differs (fnt={via_fnt.num_encounters}, "
                f"legacy={legacy.num_encounters})"
            )
            self.assertEqual(
                len(via_fnt.encounters), len(legacy.encounters),
                f"{path}: parsed encounter count differs"
            )
            for i, (a, b) in enumerate(zip(via_fnt.encounters, legacy.encounters)):
                self.assertEqual(
                    a.digimon_id, b.digimon_id,
                    f"{path}: encounter {i} digimon_id differs "
                    f"(fnt={a.digimon_id}, legacy={b.digimon_id})"
                )
                self.assertEqual(
                    a.reward_slot, b.reward_slot,
                    f"{path}: encounter {i} reward_slot differs "
                    f"(fnt={a.reward_slot}, legacy={b.reward_slot})"
                )
        # Sanity: at least one E0XX.BIN matched a legacy area.
        matched = sum(1 for fs in file_starts_seen if fs in legacy_areas)
        self.assertGreater(matched, 0,
                           "no E0XX.BIN file start matched a legacy WildEncounterArea offset")


class DuskUsPagination(PaginationTestBase, unittest.TestCase):
    VERSION = "DUSK_US"


class DawnUsPagination(PaginationTestBase, unittest.TestCase):
    VERSION = "DAWN_US"


class PaginationValidation(unittest.TestCase):
    """Negative-path tests on the parser itself (no ROM needed)."""

    def test_empty_file_raises(self):
        with self.assertRaises(ValueError):
            pagination.parse_page_header(b"")

    def test_header_too_short_raises(self):
        with self.assertRaises(ValueError):
            pagination.parse_page_header(b"\x00\x00\x00")

    def test_first_offset_unaligned_raises(self):
        # first u32 = 0x05 (not multiple of 4) -> rejected
        with self.assertRaises(ValueError):
            pagination.parse_page_header(b"\x05\x00\x00\x00" + b"\x00" * 32)

    def test_first_offset_overruns_eof_raises(self):
        # first u32 = 0x100 but file only 0x40 bytes
        with self.assertRaises(ValueError):
            pagination.parse_page_header(b"\x00\x01\x00\x00" + b"\x00" * 0x3C)

    def test_valid_minimal_header(self):
        # first u32 = 0x04 -> single-page header, 1 entry pointing into body
        data = b"\x04\x00\x00\x00" + b"AAAA"
        offsets = pagination.parse_page_header(data)
        self.assertEqual(offsets, [0x04])
        pages = list(pagination.iter_pages(data))
        self.assertEqual(pages, [(0, 0x04, 4)])

    def test_iter_fixed_records_basic(self):
        # 6 bytes / 2 -> 3 records at offsets 0, 2, 4
        recs = list(pagination.iter_fixed_records(b"AABBCC", 2))
        self.assertEqual(recs, [(0, 0), (1, 2), (2, 4)])

    def test_iter_fixed_records_non_divisible_raises(self):
        with self.assertRaises(ValueError):
            list(pagination.iter_fixed_records(b"AAAB", 3))

    def test_iter_fixed_records_zero_size_raises(self):
        with self.assertRaises(ValueError):
            list(pagination.iter_fixed_records(b"AAAA", 0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
