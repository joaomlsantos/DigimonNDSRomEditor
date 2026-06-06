"""End-to-end save test for BTCHR.PAK edits.

Exercises the path a real editor save takes:

1. Open a vanilla ROM as :class:`RomSession`.
2. Replace one (or more) entries inside BTCHR.PAK via the same
   ``sprite_pak() / replace_entry() / mark_sprite_pak_dirty()`` calls
   the browser's :class:`ReplaceSpriteCommand` makes.
3. ``serialize_all()`` — runs ``_apply_sprite_pak_splice`` which rebuilds
   the dirty pak, splices it into the ROM bytes, and patches the FAT.
4. Write the result to disk, reopen with ``RomSession.from_file``.
5. Assert the reloaded BTCHR.PAK carries the edited bytes, that the
   payloads still decode through the BTCHR codec, and that an unrelated
   file (BTCHRSIZE.BIN sitting in the same directory) is still readable
   — this catches FAT-shift corruption when the pak grew or shrank.
"""
import os
import struct
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PKG_PARENT = os.path.abspath(os.path.join(HERE, "..", ".."))
if PKG_PARENT not in sys.path:
    sys.path.insert(0, PKG_PARENT)

from digimon_core import btchr, fnt, pak, sprite  # noqa: E402
from editor.session import RomSession  # noqa: E402


ROM_PATHS = {
    "DUSK_US": r"C:\Workspace\digimon_stuffs\rom_files\1420 - Digimon World - Dusk (US).nds",
    "DAWN_US": r"C:\Workspace\digimon_stuffs\rom_files\1421 - Digimon World - Dawn (USA).nds",
}

BTCHR_PAK = "DAT/BTCHR.PAK"
BTCHRSIZE = "DAT/BTCHR/BTCHRSIZE.BIN"
CHRSIZE = "DAT/BTCHR/CHRSIZE.BIN"
TARGET_GROUP = 1  # Koromon — 5 cells, predictable layout
NCGR_OFFSET = 1
NCLR_OFFSET = 2


def _load_pak_via_fnt(rom_bytes: bytes, fat_path: str) -> bytes:
    ft = fnt.FileTable.from_rom(rom_bytes)
    start, end = ft.resolve(fat_path)
    return rom_bytes[start:end]


def _save_and_reload(sess: RomSession) -> RomSession:
    """Serialize + write to a temp .nds + reopen — full splice path."""
    out = bytes(sess.serialize_all())
    with tempfile.NamedTemporaryFile(suffix=".nds", delete=False) as f:
        f.write(out)
        tmp = f.name
    try:
        return RomSession.from_file(tmp)
    finally:
        os.unlink(tmp)


class _BtchrSaveRoundTripBase:
    VERSION: str = ""

    @classmethod
    def setUpClass(cls):
        path = ROM_PATHS[cls.VERSION]
        if not os.path.exists(path):
            raise unittest.SkipTest(f"ROM not found: {path}")
        cls.path = path

    def _session(self) -> RomSession:
        return RomSession.from_file(self.path)

    # ---- palette swap ---------------------------------------------------

    def test_palette_swap_survives_save(self):
        sess = self._session()
        pak_obj = sess.sprite_pak(BTCHR_PAK)
        nclr_entry_idx = TARGET_GROUP * btchr.GROUP_SIZE + NCLR_OFFSET

        # Build a new palette: all white, then overwrite slot 1 with a
        # signature colour the test can spot after reload.
        sentinel = (213, 47, 128)
        new_colours = [(255, 255, 255)] * 256
        new_colours[0] = (0, 0, 0)  # keep slot 0 as the transparent slot
        new_colours[1] = sentinel
        orig_nclr_raw = sprite.decompress_rle30(pak_obj.entries[nclr_entry_idx])
        new_nclr = sprite.build_nclr_from_template(orig_nclr_raw, {0: new_colours})
        compressed = sprite.compress_rle30(new_nclr)

        pak_obj.replace_entry(nclr_entry_idx, compressed)
        sess.mark_sprite_pak_dirty(BTCHR_PAK)

        sess2 = _save_and_reload(sess)
        pak2 = sess2.sprite_pak(BTCHR_PAK)

        # The pak's raw bytes survived the splice exactly as written.
        self.assertEqual(bytes(pak2.entries[nclr_entry_idx]), compressed)

        # And re-decoded through the codec, the palette still carries the
        # sentinel colour — small rounding from BGR555 is expected, so
        # tolerate a ±9 (1 step in 5-bit space ≈ 8).
        roundtrip = sprite.parse_nclr(
            sprite.decompress_rle30(pak2.entries[nclr_entry_idx])
        )[0][0]
        r, g, b = roundtrip[1]
        sr, sg, sb = sentinel
        self.assertLessEqual(abs(r - sr), 9, f"red drifted too far: {r} vs {sr}")
        self.assertLessEqual(abs(g - sg), 9, f"green drifted too far: {g} vs {sg}")
        self.assertLessEqual(abs(b - sb), 9, f"blue drifted too far: {b} vs {sb}")

    # ---- tile sheet swap ------------------------------------------------

    def test_tile_sheet_swap_survives_save(self):
        sess = self._session()
        pak_obj = sess.sprite_pak(BTCHR_PAK)
        ncgr_entry_idx = TARGET_GROUP * btchr.GROUP_SIZE + NCGR_OFFSET

        orig_ncgr_raw = sprite.decompress_rle30(pak_obj.entries[ncgr_entry_idx])
        orig_tiles, _bpp, _hw, _hh, _bm = sprite.parse_ncgr(orig_ncgr_raw)
        # Flip the high bit of every tile-data byte so the swap is non-trivial
        # *and* keeps every byte a valid palette index (256-entry palette).
        new_tiles = bytes((b ^ 0x80) for b in orig_tiles)
        new_ncgr = sprite.build_ncgr_from_template(new_tiles, orig_ncgr_raw)
        compressed = sprite.compress_rle30(new_ncgr)

        pak_obj.replace_entry(ncgr_entry_idx, compressed)
        sess.mark_sprite_pak_dirty(BTCHR_PAK)

        sess2 = _save_and_reload(sess)
        pak2 = sess2.sprite_pak(BTCHR_PAK)
        self.assertEqual(bytes(pak2.entries[ncgr_entry_idx]), compressed)

        # Through the codec, the swapped tile bytes come back identical.
        reparsed_tiles, *_ = sprite.parse_ncgr(
            sprite.decompress_rle30(pak2.entries[ncgr_entry_idx])
        )
        self.assertEqual(reparsed_tiles, new_tiles)

    # ---- combined edit + sibling-file invariant ------------------------

    def test_combined_edit_survives_save_and_sibling_files_intact(self):
        """Palette + tile-sheet edit together. After save, verify:

        - both edited entries are present in the reloaded pak,
        - the *whole* digimon still decodes through ``decode_digimon``,
        - sibling files in DAT/BTCHR/ (BTCHRSIZE.BIN, CHRSIZE.BIN) still
          resolve via the FAT and contain plausible content. A FAT-shift
          bug from splicing would land us on garbage bytes here.
        """
        sess = self._session()
        pak_obj = sess.sprite_pak(BTCHR_PAK)
        nclr_idx = TARGET_GROUP * btchr.GROUP_SIZE + NCLR_OFFSET
        ncgr_idx = TARGET_GROUP * btchr.GROUP_SIZE + NCGR_OFFSET

        # Palette: zero out slot 1 — easy fingerprint to spot post-reload.
        orig_nclr_raw = sprite.decompress_rle30(pak_obj.entries[nclr_idx])
        new_colours = sprite.parse_nclr(orig_nclr_raw)[0][0]
        new_colours[1] = (0, 0, 0)
        new_nclr = sprite.build_nclr_from_template(orig_nclr_raw, {0: new_colours})

        # Tile sheet: XOR pass — non-trivial but in-range.
        orig_ncgr_raw = sprite.decompress_rle30(pak_obj.entries[ncgr_idx])
        orig_tiles, *_ = sprite.parse_ncgr(orig_ncgr_raw)
        new_tiles = bytes((b ^ 0x55) for b in orig_tiles)
        new_ncgr = sprite.build_ncgr_from_template(new_tiles, orig_ncgr_raw)

        pak_obj.replace_entry(nclr_idx, sprite.compress_rle30(new_nclr))
        pak_obj.replace_entry(ncgr_idx, sprite.compress_rle30(new_ncgr))
        sess.mark_sprite_pak_dirty(BTCHR_PAK)

        sess2 = _save_and_reload(sess)
        pak2 = sess2.sprite_pak(BTCHR_PAK)

        # Both entries hold the edited bytes after the splice round-trip.
        self.assertEqual(
            bytes(pak2.entries[nclr_idx]), sprite.compress_rle30(new_nclr),
        )
        self.assertEqual(
            bytes(pak2.entries[ncgr_idx]), sprite.compress_rle30(new_ncgr),
        )

        # End-to-end: decode_digimon walks all 5 entries; this fails fast
        # if the splice corrupted any of them.
        chrsize_raw = _load_pak_via_fnt(bytes(sess2.original_rom_data), CHRSIZE)
        rows = btchr.parse_chrsize(chrsize_raw)
        d = btchr.decode_digimon(pak2, TARGET_GROUP, digimon_id=rows[TARGET_GROUP][0])
        self.assertEqual(d.palette[1], (0, 0, 0))  # our edit landed
        self.assertEqual(d.tile_bytes, new_tiles)  # ditto
        self.assertGreater(len(d.ncer.cells), 0)  # NCER didn't get clobbered

        # Sibling file invariant: BTCHRSIZE.BIN sits in the same directory
        # as BTCHR.PAK. A FAT-shift miscalculation would slide its offset
        # off the file boundary; resolve + length-sanity-check catches that.
        sibling_raw = _load_pak_via_fnt(bytes(sess2.original_rom_data), BTCHRSIZE)
        self.assertEqual(len(sibling_raw) % 4, 0)
        sidecar = struct.unpack(f"<{len(sibling_raw) // 4}I", sibling_raw)
        n_groups = btchr.parse_pak_groups(pak2)
        self.assertEqual(len(sidecar), n_groups)


class BtchrSaveRoundTripDusk(_BtchrSaveRoundTripBase, unittest.TestCase):
    VERSION = "DUSK_US"


class BtchrSaveRoundTripDawn(_BtchrSaveRoundTripBase, unittest.TestCase):
    VERSION = "DAWN_US"


if __name__ == "__main__":
    unittest.main(verbosity=2)
