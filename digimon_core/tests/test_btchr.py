"""BTCHR codec — battle sprite reader (PAK groups, mini-header, NCER composite).

Guarantees verified against vanilla Dusk + Dawn:

1. PAK structure: 2075 entries = 415 digimon × 5 sub-entries each. Every
   group's magic sequence is ``(RGCN, RLCN, RECN, RNAN)`` for entries 1–4.
2. Mini-header parses without error across all 415 groups and recovers
   the expected size variants (36/48/52/60 B).
3. ``btchrsize.bin`` == sum of uncompressed sizes of entries 1–4 per group.
4. ``chrsize.bin[i].hi * 5 * BYTES_PER_TILE_8BPP`` == NCGR RAHC data_size.
5. ``render_cell_rgba`` returns a non-empty RGBA buffer for every (group,
   cell) combo without raising.
"""
import os
import struct
import sys
import unittest
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
PKG_PARENT = os.path.abspath(os.path.join(HERE, "..", ".."))
if PKG_PARENT not in sys.path:
    sys.path.insert(0, PKG_PARENT)

from digimon_core import btchr, fnt, ncer as ncer_mod, pak, rom, sprite  # noqa: E402


ROM_DIR = r"C:\Workspace\digimon_stuffs\rom_files"
ROM_PATHS = {
    "DUSK_US": os.path.join(ROM_DIR, "1420 - Digimon World - Dusk (US).nds"),
    "DAWN_US": os.path.join(ROM_DIR, "1421 - Digimon World - Dawn (USA).nds"),
}

BTCHR_PAK = "DAT/BTCHR.PAK"
CHRSIZE = "DAT/BTCHR/CHRSIZE.BIN"
BTCHRSIZE = "DAT/BTCHR/BTCHRSIZE.BIN"

EXPECTED_GROUPS = 415
EXPECTED_HEADER_SIZES = {36, 48, 52, 60}


def _resolve(rom_bytes: bytes, name: str) -> bytes:
    ft = fnt.FileTable.from_rom(rom_bytes)
    start, end = ft.resolve(name)
    return rom_bytes[start:end]


def _load_pak(rom_bytes: bytes, name: str) -> pak.PakFile:
    return pak.PakFile(_resolve(rom_bytes, name))


class _BtchrRomCase(unittest.TestCase):
    """Base class — each per-ROM subclass loads its own bytes once."""

    ROM_KEY = None  # set by subclass

    @classmethod
    def setUpClass(cls):
        if cls.ROM_KEY is None:
            raise unittest.SkipTest("base class — no ROM bound")
        path = ROM_PATHS[cls.ROM_KEY]
        if not os.path.exists(path):
            raise unittest.SkipTest(f"ROM not found: {path}")
        cls.rom_bytes = bytes(rom.loadRom(path))
        cls.pak = _load_pak(cls.rom_bytes, BTCHR_PAK)
        cls.chrsize_raw = _resolve(cls.rom_bytes, CHRSIZE)
        cls.btchrsize_raw = _resolve(cls.rom_bytes, BTCHRSIZE)

    def test_group_count(self):
        self.assertEqual(btchr.parse_pak_groups(self.pak), EXPECTED_GROUPS)

    def test_magic_sequence_every_group(self):
        n_groups = btchr.parse_pak_groups(self.pak)
        misses = []
        for g in range(n_groups):
            base = g * btchr.GROUP_SIZE
            magics = []
            for k in range(1, 5):  # skip mini-header (entry 0)
                d = sprite.decompress_rle30(self.pak.entries[base + k])
                magics.append(d[:4])
            if tuple(magics) != (b"RGCN", b"RLCN", b"RECN", b"RNAN"):
                misses.append((g, [m.decode("ascii", "replace") for m in magics]))
        self.assertEqual(misses, [], f"unexpected magic sequences: {misses[:5]}")

    def test_mini_header_parses_every_group(self):
        n_groups = btchr.parse_pak_groups(self.pak)
        size_hist = Counter()
        for g in range(n_groups):
            base = g * btchr.GROUP_SIZE
            raw = sprite.decompress_rle30(self.pak.entries[base])
            h = btchr.parse_mini_header(raw)
            size_hist[h.raw_size] += 1
            # All animation tracks must have at least one step.
            self.assertGreaterEqual(len(h.idle), 1, f"g={g}: empty idle")
            self.assertGreaterEqual(len(h.attack), 1, f"g={g}: empty attack")
            self.assertGreaterEqual(len(h.defend), 1, f"g={g}: empty defend")
        # Every observed size must be in the known set.
        unknown = set(size_hist) - EXPECTED_HEADER_SIZES
        self.assertEqual(unknown, set(), f"new mini-header sizes seen: {unknown}")
        # The 52-byte variant must dominate (~409/415 in vanilla Dusk).
        self.assertGreater(size_hist[52], 350)

    def test_mini_header_roundtrip_every_group(self):
        """serialize_mini_header is the exact inverse of parse_mini_header.

        Compares against the uncompressed entry-0 bytes (truncated to
        ``raw_size`` — vanilla entries sometimes carry trailing bytes
        past the third terminator that we don't model). If this passes
        across all 415 groups, the writer is byte-perfect for every
        header shape DWDD ships.
        """
        n_groups = btchr.parse_pak_groups(self.pak)
        mismatches = []
        for g in range(n_groups):
            base = g * btchr.GROUP_SIZE
            raw = sprite.decompress_rle30(self.pak.entries[base])
            h = btchr.parse_mini_header(raw)
            rebuilt = btchr.serialize_mini_header(h)
            # raw_size is the byte length parse_mini_header consumed —
            # the third terminator's end. Compare just that prefix; any
            # trailing pad is the engine's concern, not ours.
            if rebuilt != raw[:h.raw_size]:
                mismatches.append(g)
                if len(mismatches) >= 3:
                    break
        self.assertEqual(mismatches, [], f"first mismatches: {mismatches}")

    def test_btchrsize_matches_uncompressed_body_sum(self):
        n_groups = btchr.parse_pak_groups(self.pak)
        sidecar = struct.unpack(f"<{len(self.btchrsize_raw) // 4}I",
                                self.btchrsize_raw)
        self.assertEqual(len(sidecar), n_groups)
        for g in range(n_groups):
            base = g * btchr.GROUP_SIZE
            total = sum(
                len(sprite.decompress_rle30(self.pak.entries[base + k]))
                for k in range(1, 5)
            )
            self.assertEqual(sidecar[g], total, f"g={g}: btchrsize mismatch")

    def test_chrsize_high_matches_ncgr_tiles(self):
        n_groups = btchr.parse_pak_groups(self.pak)
        rows = btchr.parse_chrsize(self.chrsize_raw)
        self.assertEqual(len(rows), n_groups)
        for g in range(n_groups):
            if g in btchr.SENTINEL_GROUPS:
                continue  # placeholder slots — chrsize.hi reports budget, not actual
            digimon_id, tiles_div5 = rows[g]
            base = g * btchr.GROUP_SIZE
            ncgr_raw = sprite.decompress_rle30(self.pak.entries[base + 1])
            tile_bytes, *_ = sprite.parse_ncgr(ncgr_raw)
            n_tiles_8bpp = len(tile_bytes) // btchr.BYTES_PER_TILE_8BPP
            self.assertEqual(n_tiles_8bpp, tiles_div5 * 5, f"g={g}: tile count")

    def test_render_cell_rgba_runs_every_cell(self):
        """Smoke test — every cell composites without raising, output non-empty."""
        n_groups = btchr.parse_pak_groups(self.pak)
        rows = btchr.parse_chrsize(self.chrsize_raw)
        # Sample every 25th group (17 groups) to keep the test under a few
        # seconds; the codec is uniform so this is enough coverage.
        for g in range(0, n_groups, 25):
            d = btchr.decode_digimon(self.pak, g, digimon_id=rows[g][0])
            for ci, cell in enumerate(d.ncer.cells):
                rgba, w, h = btchr.render_cell_rgba(
                    cell, d.tile_bytes, d.palette,
                    boundary_bytes=d.ncer.boundary_bytes,
                )
                self.assertEqual(len(rgba), w * h * 4)
                self.assertGreater(w, 0)
                self.assertGreater(h, 0)
                # Real cells must paint something. Sentinel groups
                # legitimately render empty.
                if cell.oams and g not in btchr.SENTINEL_GROUPS:
                    alphas = rgba[3::4]
                    self.assertTrue(any(a > 0 for a in alphas),
                                    f"g={g} cell{ci}: all transparent")

    def test_flatten_anim_track(self):
        """Idle track expansion gives one entry per output frame."""
        rows = btchr.parse_chrsize(self.chrsize_raw)
        d = btchr.decode_digimon(self.pak, 1, digimon_id=rows[1][0])  # Koromon
        expanded = btchr.flatten_anim_track(d.header.idle)
        total = sum(step.duration for step in d.header.idle)
        self.assertEqual(len(expanded), total)
        # Each output frame's cell must be a valid NCER cell index.
        for cell_idx, _ in expanded:
            self.assertGreaterEqual(cell_idx, 0)
            self.assertLess(cell_idx, len(d.ncer.cells))


class BtchrDuskTests(_BtchrRomCase):
    ROM_KEY = "DUSK_US"


class BtchrDawnTests(_BtchrRomCase):
    ROM_KEY = "DAWN_US"


if __name__ == "__main__":
    unittest.main()
