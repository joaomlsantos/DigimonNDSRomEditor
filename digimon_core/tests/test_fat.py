"""FAT/header arithmetic tests (PLAN.md §12 Phase A).

Three layers:

  1. **Unit**: ``signed_align`` on representative deltas; ``find_container``
     on synthetic and vanilla ROMs; ``splice_range`` on synthetic buffers
     for growth, shrink, and same-size.

  2. **Oracle equivalence**: mimic ``rom_files/inject_test.py``'s exact
     output against vanilla Dusk, with a same-size replacement, a growth
     past the next alignment step, and a shrink. The fat.py helpers must
     produce byte-identical ROMs.

  3. **Round-trip**: same-size injection of MSG.PAK's own bytes via the
     helpers leaves the ROM unchanged — a sanity check that the
     splice + FAT + header pipeline composes correctly.
"""
import os
import struct
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PKG_PARENT = os.path.abspath(os.path.join(HERE, "..", ".."))
if PKG_PARENT not in sys.path:
    sys.path.insert(0, PKG_PARENT)

from digimon_core import fat, fnt, rom  # noqa: E402


ROM_DIR = r"C:\Workspace\digimon_stuffs\rom_files"
ROM_PATHS = {
    "DUSK_US": os.path.join(ROM_DIR, "1420 - Digimon World - Dusk (US).nds"),
    "DAWN_US": os.path.join(ROM_DIR, "1421 - Digimon World - Dawn (USA).nds"),
}

ALIGNMENT = 0x200


# --- oracle: a direct port of rom_files/inject_test.py's transform -----------
# Kept here (not imported) because the script is a CLI in rom_files/ that
# isn't on sys.path. If this drifts from the real script the test will fail
# vs. fat.py; that's intentional — both are independent statements of the
# same arithmetic.
def _inject_oracle(rom_bytes: bytes, target_start: int, target_end: int,
                   new_content: bytes) -> bytearray:
    out = bytearray(rom_bytes)
    old_inner = target_end - target_start
    delta = len(new_content) - old_inner
    if delta > 0:
        shift = ((delta + ALIGNMENT - 1) // ALIGNMENT) * ALIGNMENT
    elif delta < 0:
        shift = -((-delta) // ALIGNMENT) * ALIGNMENT
    else:
        shift = 0
    pad_len = shift - delta

    fat_off = struct.unpack_from("<I", out, 0x48)[0]
    fat_size = struct.unpack_from("<I", out, 0x4C)[0]
    n = fat_size // 8

    container_idx = container_start = container_end = None
    for i in range(n):
        s, e = struct.unpack_from("<II", out, fat_off + i * 8)
        if s <= target_start and target_end <= e:
            container_idx, container_start, container_end = i, s, e
            break
    assert container_idx is not None

    for i in range(n):
        eo = fat_off + i * 8
        s, e = struct.unpack_from("<II", out, eo)
        if i == container_idx:
            struct.pack_into("<II", out, eo, container_start, container_end + delta)
        elif s >= container_end:
            struct.pack_into("<II", out, eo, s + shift, e + shift)

    for hoff, _ in fat._HEADER_OFFSET_FIELDS:
        val = struct.unpack_from("<I", out, hoff)[0]
        if val >= container_end:
            struct.pack_into("<I", out, hoff, val + shift)

    tail = bytes(out[target_end:container_end])
    out[target_start:container_end] = bytes(new_content) + tail + b"\xFF" * pad_len
    # NB: inject_test.py writes len(rom) here, which inflates header[0x80] by
    # the trailing cart-FF padding amount. fat.py deliberately writes
    # max(FAT.end) instead (see resize_fat_entry docstring) so the §12.3 trim
    # has a well-defined boundary. The oracle adopts fat.py's semantics so
    # the helpers and the oracle can be compared byte-for-byte.
    max_end = 0
    for i in range(n):
        _, e = struct.unpack_from("<II", out, fat_off + i * 8)
        if e > max_end:
            max_end = e
    struct.pack_into("<I", out, 0x80, max_end)
    return out


def _apply_via_helpers(rom_bytes: bytes, target_start: int, target_end: int,
                       new_content: bytes) -> bytearray:
    out = bytearray(rom_bytes)
    idx, _cs, ce = fat.find_container(out, target_start, target_end)
    content_delta = len(new_content) - (target_end - target_start)
    aligned_shift = fat.splice_range(out, target_start, target_end, ce, new_content)
    fat.resize_fat_entry(out, idx, ce, content_delta, aligned_shift)
    return out


# --- unit tests --------------------------------------------------------------

class SignedAlignTests(unittest.TestCase):
    def test_zero(self):
        self.assertEqual(fat.signed_align(0), 0)

    def test_positive_round_up(self):
        # 1 byte of growth still needs a full 0x200 step downstream.
        self.assertEqual(fat.signed_align(1), 0x200)
        self.assertEqual(fat.signed_align(0x200), 0x200)
        self.assertEqual(fat.signed_align(0x201), 0x400)

    def test_negative_round_magnitude_down(self):
        # 1 byte of shrink cannot reclaim a 0x200 block — shift is 0.
        self.assertEqual(fat.signed_align(-1), 0)
        self.assertEqual(fat.signed_align(-0x1FF), 0)
        self.assertEqual(fat.signed_align(-0x200), -0x200)
        self.assertEqual(fat.signed_align(-0x201), -0x200)
        self.assertEqual(fat.signed_align(-0x400), -0x400)

    def test_pad_count_always_nonneg(self):
        # pad_count = aligned_shift - content_delta must be >= 0 for any
        # signed delta — this is the invariant splice_range depends on.
        for delta in range(-0x500, 0x500):
            shift = fat.signed_align(delta)
            self.assertGreaterEqual(shift - delta, 0,
                                    f"pad_count<0 for delta={delta}")


class SpliceRangeUnitTests(unittest.TestCase):
    """Synthetic-buffer tests so failures point at splice arithmetic, not FAT."""

    def test_same_size_no_shift(self):
        buf = bytearray(b"AAAAXXXXBBBB")  # container = [0, 12)
        shift = fat.splice_range(buf, 4, 8, 12, b"YYYY")
        self.assertEqual(shift, 0)
        self.assertEqual(bytes(buf), b"AAAAYYYYBBBB")

    def test_growth_pads_to_alignment(self):
        # 4-byte target, 5-byte new content => +1 byte. Aligned shift = 0x200.
        # pad = 0x1FF zeros... no, 0xFF bytes, between preserved tail and the
        # downstream region.
        buf = bytearray(b"A" * 4 + b"X" * 4 + b"B" * 4)
        shift = fat.splice_range(buf, 4, 8, 12, b"Y" * 5)
        self.assertEqual(shift, 0x200)
        # rom[:4] untouched
        self.assertEqual(bytes(buf[:4]), b"AAAA")
        # then new_content
        self.assertEqual(bytes(buf[4:9]), b"YYYYY")
        # then preserved tail
        self.assertEqual(bytes(buf[9:13]), b"BBBB")
        # then 0xFF pad up to original_container_end + shift = 12 + 0x200
        self.assertEqual(bytes(buf[13:12 + 0x200]), b"\xFF" * (0x200 - 1))
        self.assertEqual(len(buf), 12 + 0x200)

    def test_shrink_below_alignment_step_no_shift(self):
        # Shrink by 1 byte: too small to reclaim a 0x200 block, so the
        # downstream slice should not move; the gap is 0xFF-padded.
        buf = bytearray(b"A" * 4 + b"X" * 4 + b"B" * 4)
        original_len = len(buf)
        shift = fat.splice_range(buf, 4, 8, 12, b"Y" * 3)
        self.assertEqual(shift, 0)
        self.assertEqual(bytes(buf[:4]), b"AAAA")
        self.assertEqual(bytes(buf[4:7]), b"YYY")
        self.assertEqual(bytes(buf[7:11]), b"BBBB")
        self.assertEqual(buf[11], 0xFF)  # the reclaimed pad byte
        self.assertEqual(len(buf), original_len)

    def test_shrink_full_alignment_step(self):
        # Shrink by exactly 0x200: reclaim a whole block, downstream moves.
        buf = bytearray(b"A" * 4 + b"X" * (0x200 + 4) + b"B" * 4)
        shift = fat.splice_range(buf, 4, 4 + 0x200 + 4, 4 + 0x200 + 4, b"Y" * 4)
        # No downstream region (container_end == buf end), so length shrinks
        # by exactly 0x200.
        self.assertEqual(shift, -0x200)
        self.assertEqual(bytes(buf[:4]), b"AAAA")
        self.assertEqual(bytes(buf[4:8]), b"YYYY")
        self.assertEqual(bytes(buf[8:12]), b"BBBB")
        self.assertEqual(len(buf), 12)

    def test_invalid_range_raises(self):
        buf = bytearray(b"\x00" * 32)
        with self.assertRaises(ValueError):
            fat.splice_range(buf, 8, 4, 16, b"x")  # target_end < target_start


# --- vanilla-ROM tests -------------------------------------------------------

class _VanillaBase:
    VERSION: str = ""

    @classmethod
    def setUpClass(cls):
        path = ROM_PATHS[cls.VERSION]
        if not os.path.exists(path):
            raise unittest.SkipTest(f"ROM not found: {path}")
        cls.rom = bytes(rom.loadRom(path))
        ft = fnt.FileTable.from_rom(cls.rom)
        cls.msgpak_start, cls.msgpak_end = ft.resolve("DAT/MSG.PAK")
        cls.msgpak_idx, _, _ = fat.find_container(
            cls.rom, cls.msgpak_start, cls.msgpak_end
        )

    # -- find_container ---------------------------------------------------

    def test_find_msgpak(self):
        idx, cs, ce = fat.find_container(self.rom, self.msgpak_start, self.msgpak_end)
        self.assertEqual(idx, self.msgpak_idx)
        self.assertEqual((cs, ce), (self.msgpak_start, self.msgpak_end))

    def test_find_inner_slice(self):
        # An arbitrary inner byte range still resolves to MSG.PAK's entry.
        idx, cs, ce = fat.find_container(
            self.rom, self.msgpak_start + 0x100, self.msgpak_start + 0x200
        )
        self.assertEqual(idx, self.msgpak_idx)
        self.assertEqual(ce, self.msgpak_end)

    def test_find_cross_file_raises(self):
        # A range that starts in MSG.PAK and ends past it must not resolve.
        with self.assertRaises(ValueError):
            fat.find_container(
                self.rom, self.msgpak_end - 0x10, self.msgpak_end + 0x10
            )

    # -- vanilla header[0x80] == max(FAT.end) ------------------------------

    def test_vanilla_header_0x80_matches_max_fat_end(self):
        fat_off = struct.unpack_from("<I", self.rom, 0x48)[0]
        fat_size = struct.unpack_from("<I", self.rom, 0x4C)[0]
        max_end = 0
        for i in range(fat_size // 8):
            _, e = struct.unpack_from("<II", self.rom, fat_off + i * 8)
            if e > max_end:
                max_end = e
        used = struct.unpack_from("<I", self.rom, 0x80)[0]
        self.assertEqual(used, max_end,
                         "header[0x80] != max(FAT.end) — resize_fat_entry's "
                         "assumption that 0x80 can be recomputed is broken")

    # -- end-to-end equivalence with inject_test.py oracle ----------------

    def _inject_inside_msgpak(self, new_size: int) -> None:
        # Replace a slice in the middle of MSG.PAK with `new_size` bytes.
        target_start = self.msgpak_start + 0x1000
        target_end = self.msgpak_start + 0x1100  # 0x100-byte target window
        new_content = bytes((i & 0xFF for i in range(new_size)))

        expected = _inject_oracle(self.rom, target_start, target_end, new_content)
        actual = _apply_via_helpers(self.rom, target_start, target_end, new_content)
        self.assertEqual(
            bytes(actual), bytes(expected),
            f"helpers diverge from inject_test oracle for new_size={new_size}"
        )

    def test_inject_same_size(self):
        self._inject_inside_msgpak(0x100)  # delta = 0

    def test_inject_growth(self):
        # +0x100: needs a full +0x200 downstream shift.
        self._inject_inside_msgpak(0x200)

    def test_inject_growth_crosses_two_steps(self):
        # +0x300: needs +0x400 downstream shift.
        self._inject_inside_msgpak(0x400)

    def test_inject_shrink_reclaims_block(self):
        # -0x100 of a 0x100 target = empty content => -0x200 shift? No: delta
        # is -0x100, abs(delta) < 0x200, so shift = 0. Test the reclaiming case
        # with a larger target.
        target_start = self.msgpak_start + 0x1000
        target_end = self.msgpak_start + 0x1400  # 0x400-byte target
        new_content = b""  # delta = -0x400, shift = -0x400

        expected = _inject_oracle(self.rom, target_start, target_end, new_content)
        actual = _apply_via_helpers(self.rom, target_start, target_end, new_content)
        self.assertEqual(bytes(actual), bytes(expected),
                         "shrink path diverges from oracle")

    def test_noop_identity(self):
        # Re-inject the exact bytes that are already there: ROM must be
        # bit-identical to the input.
        target_start = self.msgpak_start + 0x800
        target_end = self.msgpak_start + 0xC00
        same_bytes = self.rom[target_start:target_end]
        actual = _apply_via_helpers(self.rom, target_start, target_end, same_bytes)
        self.assertEqual(bytes(actual), self.rom,
                         "same-bytes inject changed the ROM")


class DuskUsFat(_VanillaBase, unittest.TestCase):
    VERSION = "DUSK_US"


class DawnUsFat(_VanillaBase, unittest.TestCase):
    VERSION = "DAWN_US"


if __name__ == "__main__":
    unittest.main(verbosity=2)
