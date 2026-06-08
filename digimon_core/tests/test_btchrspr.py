"""btchrspr — portable single-digimon BTCHR sprite kit.

Verifies against vanilla Dusk:

1. serialize() → parse() round-trips header fields + every entry's bytes.
2. apply() mutates the target's 5 PAK entries, chrsize.tpf (preserving
   id), and btchrsize value.
3. ncgr_tile_count property matches the RAHC data_size for the carried
   NCGR.
4. Header is rejected with ValueError on bad magic, bad version, and on
   truncated payloads.
"""
import os
import struct
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PKG_PARENT = os.path.abspath(os.path.join(HERE, "..", ".."))
if PKG_PARENT not in sys.path:
    sys.path.insert(0, PKG_PARENT)

from digimon_core import btchr, btchrspr, fnt, pak, rom, sprite  # noqa: E402


ROM_DIR = r"C:\Workspace\digimon_stuffs\rom_files"
DUSK_PATH = os.path.join(ROM_DIR, "1420 - Digimon World - Dusk (US).nds")
BTCHR_PAK = "DAT/BTCHR.PAK"
CHRSIZE = "DAT/BTCHR/CHRSIZE.BIN"
BTCHRSIZE = "DAT/BTCHR/BTCHRSIZE.BIN"

# Two non-sentinel groups with clearly different sizes so apply() flips
# meaningful bytes. g1 = Koromon (small), g24 = a larger sprite proven
# to port cleanly in _probe_btchr_port runs.
SRC_GROUP = 24
TGT_GROUP = 1


def _slice(rom_bytes: bytes, name: str):
    ft = fnt.FileTable.from_rom(rom_bytes)
    s, e = ft.resolve(name)
    return rom_bytes[s:e]


class BtchrSprRoundTripTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not os.path.exists(DUSK_PATH):
            raise unittest.SkipTest(f"ROM not found: {DUSK_PATH}")
        cls.rom_bytes = bytes(rom.loadRom(DUSK_PATH))
        cls.pak = pak.PakFile(_slice(cls.rom_bytes, BTCHR_PAK))
        cls.chrsize_raw = _slice(cls.rom_bytes, CHRSIZE)
        cls.btchrsize_raw = _slice(cls.rom_bytes, BTCHRSIZE)
        rows = btchr.parse_chrsize(cls.chrsize_raw)
        cls.src_id, cls.src_tpf = rows[SRC_GROUP]
        cls.tgt_id, cls.tgt_tpf = rows[TGT_GROUP]
        cls.src_bsz = struct.unpack_from("<I", cls.btchrsize_raw, SRC_GROUP * 4)[0]
        cls.tgt_bsz = struct.unpack_from("<I", cls.btchrsize_raw, TGT_GROUP * 4)[0]

    def _build_payload(self) -> bytes:
        return btchrspr.serialize(
            self.pak, SRC_GROUP,
            source_digimon_id=self.src_id,
            source_tpf=self.src_tpf,
            btchrsize_value=self.src_bsz,
        )

    def test_serialize_parse_roundtrip(self):
        spr = btchrspr.parse(self._build_payload())
        self.assertEqual(spr.source_digimon_id, self.src_id)
        self.assertEqual(spr.source_tpf, self.src_tpf)
        self.assertEqual(spr.btchrsize_value, self.src_bsz)
        self.assertEqual(len(spr.entries), btchrspr.ENTRY_COUNT)
        base = SRC_GROUP * btchr.GROUP_SIZE
        for i in range(btchrspr.ENTRY_COUNT):
            self.assertEqual(
                spr.entries[i],
                bytes(self.pak.entries[base + i]),
                f"entry {i} bytes mismatch",
            )

    def test_ncgr_tile_count_matches_rahc(self):
        spr = btchrspr.parse(self._build_payload())
        # RAHC.data_size / 64 (8bpp) must equal chrsize.hi * 5 for non-
        # sentinel groups — same invariant test_btchr exercises directly.
        self.assertEqual(spr.ncgr_tile_count, self.src_tpf * 5)

    def test_apply_replaces_entries_and_sidecar_slots(self):
        spr = btchrspr.parse(self._build_payload())
        # Fresh mutable copies so apply()'s in-place writes don't leak
        # across tests in the class.
        pak_copy = pak.PakFile(_slice(self.rom_bytes, BTCHR_PAK))
        chrsize_buf = bytearray(self.chrsize_raw)
        btchrsize_buf = bytearray(self.btchrsize_raw)

        btchrspr.apply(pak_copy, chrsize_buf, btchrsize_buf, TGT_GROUP, spr)

        # Entries replaced.
        tgt_base = TGT_GROUP * btchr.GROUP_SIZE
        for i in range(btchrspr.ENTRY_COUNT):
            self.assertEqual(pak_copy.entries[tgt_base + i], spr.entries[i])

        # chrsize: id preserved, tpf bumped.
        new_word = struct.unpack_from("<I", chrsize_buf, TGT_GROUP * 4)[0]
        self.assertEqual(new_word & 0xFFFF, self.tgt_id)
        self.assertEqual((new_word >> 16) & 0xFFFF, self.src_tpf)

        # btchrsize: source's value written verbatim.
        new_bsz = struct.unpack_from("<I", btchrsize_buf, TGT_GROUP * 4)[0]
        self.assertEqual(new_bsz, self.src_bsz)

        # And — for paranoia — a non-target slot must be untouched.
        other = (TGT_GROUP + 7) % (len(self.chrsize_raw) // 4)
        self.assertEqual(
            chrsize_buf[other * 4: other * 4 + 4],
            self.chrsize_raw[other * 4: other * 4 + 4],
        )

    def test_parse_rejects_bad_magic(self):
        bad = bytearray(self._build_payload())
        bad[:4] = b"XXXX"
        with self.assertRaises(ValueError):
            btchrspr.parse(bytes(bad))

    def test_parse_rejects_bad_version(self):
        bad = bytearray(self._build_payload())
        struct.pack_into("<H", bad, 4, btchrspr.FORMAT_VERSION + 99)
        with self.assertRaises(ValueError):
            btchrspr.parse(bytes(bad))

    def test_parse_rejects_truncated_payload(self):
        payload = self._build_payload()
        # Lose the last byte — the final entry's declared length now
        # overshoots the buffer.
        with self.assertRaises(ValueError):
            btchrspr.parse(payload[:-1])
        # And a header-too-short.
        with self.assertRaises(ValueError):
            btchrspr.parse(payload[:btchrspr.HEADER_SIZE - 1])


if __name__ == "__main__":
    unittest.main()
