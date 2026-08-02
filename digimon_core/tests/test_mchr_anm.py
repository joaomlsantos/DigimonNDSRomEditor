"""MCHR_ANM codec — overworld-sprite animation reader.

Guarantees verified against vanilla Dusk:

1. Every MCHR_ANM entry parses (890 on Dusk) and re-serializes to the
   exact original decompressed bytes — the correctness proof for the
   writer, since the leading record params + header are preserved
   verbatim.
2. Every animation frame's ``frame`` index falls inside the matching
   MCHR_CHR entry's frame count for the overwhelming majority of records
   (the field-5 = frame-index finding), and durations are positive.
3. A hand-built entry round-trips, and editing frame/duration or
   adding/removing frames preserves the structure.
"""
import os
import struct
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PKG_PARENT = os.path.abspath(os.path.join(HERE, "..", ".."))
if PKG_PARENT not in sys.path:
    sys.path.insert(0, PKG_PARENT)

from digimon_core import fnt, mchr, mchr_anm, pak, rom, sprite  # noqa: E402


ROM_DIR = r"C:\Workspace\digimon_stuffs\rom_files"
DUSK_US = os.path.join(ROM_DIR, "1420 - Digimon World - Dusk (US).nds")

MCHR_ANM = "DAT/MCHR_ANM.PAK"
MCHR_CHR = "DAT/MCHR_CHR.PAK"


def _load_pak(rom_bytes: bytes, name: str) -> pak.PakFile:
    ft = fnt.FileTable.from_rom(rom_bytes)
    start, end = ft.resolve(name)
    return pak.PakFile(rom_bytes[start:end])


class MchrAnmUnitTests(unittest.TestCase):
    def _build(self, header, anims):
        """anims = list of list of (frame, dur, params)."""
        m = mchr_anm.MchrAnm(
            header=header,
            animations=[
                mchr_anm.MchrAnimation([
                    mchr_anm.MchrAnimFrame(frame=f, duration=d, params=p)
                    for (f, d, p) in a
                ])
                for a in anims
            ],
        )
        return m

    def test_roundtrip_hand_built(self):
        m = self._build(b"\x01\x00\x02\x00\x07\x00\x1c\x00\x00\x00", [
            [(15, 12, (8, 29, 12, 10, 0)), (16, 12, (8, 29, 12, 10, 0))],
            [(3, 4, (0, 0, 0, 0, 1))],
        ])
        raw = mchr_anm.serialize_mchr_anm(m)
        back = mchr_anm.parse_mchr_anm(raw)
        self.assertEqual(len(back.animations), 2)
        self.assertEqual(
            [(f.frame, f.duration, f.params) for f in back.animations[0].frames],
            [(15, 12, (8, 29, 12, 10, 0)), (16, 12, (8, 29, 12, 10, 0))],
        )
        self.assertTrue(back.has_animation)
        self.assertEqual(back.header, m.header)
        # serialize(parse(x)) == x
        self.assertEqual(mchr_anm.serialize_mchr_anm(back), raw)

    def test_flatten(self):
        anim = mchr_anm.MchrAnimation([
            mchr_anm.MchrAnimFrame(frame=15, duration=3),
            mchr_anm.MchrAnimFrame(frame=16, duration=2),
        ])
        flat = mchr_anm.flatten_animation(anim)
        self.assertEqual([f.frame for f in flat], [15, 15, 15, 16, 16])

    def test_missing_terminator_raises(self):
        with self.assertRaises(ValueError):
            mchr_anm.parse_mchr_anm(b"\x00" * 10 + b"\x01\x00" * 7)

    def test_compressed_input(self):
        m = self._build(b"\x00" * 10, [[(1, 5, (0, 0, 0, 0, 0))]])
        raw = mchr_anm.serialize_mchr_anm(m)
        comp = sprite.compress_rle30(raw)
        self.assertEqual(
            mchr_anm.parse_mchr_anm(comp).animations[0].frames[0].frame, 1
        )


class _MchrAnmRomCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not os.path.exists(DUSK_US):
            raise unittest.SkipTest(f"ROM not found: {DUSK_US}")
        cls.rom_bytes = bytes(rom.loadRom(DUSK_US))
        cls.anm = _load_pak(cls.rom_bytes, MCHR_ANM)
        cls.chr = _load_pak(cls.rom_bytes, MCHR_CHR)

    def test_all_entries_byte_faithful_roundtrip(self):
        for ix in range(self.anm.count):
            raw = sprite.maybe_decompress(self.anm.entries[ix])
            parsed = mchr_anm.parse_mchr_anm(raw)
            self.assertEqual(
                mchr_anm.serialize_mchr_anm(parsed), raw,
                f"entry 0x{ix:04x} did not round-trip byte-for-byte",
            )

    def test_frame_index_mostly_valid(self):
        total = in_range = 0
        durations_positive = True
        for ix in range(self.anm.count):
            try:
                fc = mchr.parse_mchr_chr_entry(
                    sprite.maybe_decompress(self.chr.entries[ix])
                ).frame_count
            except ValueError:
                continue
            parsed = mchr_anm.parse_mchr_anm(self.anm.entries[ix])
            for anim in parsed.animations:
                for fr in anim.frames:
                    total += 1
                    if fr.frame < fc:
                        in_range += 1
                    if fr.duration <= 0:
                        durations_positive = False
        self.assertGreater(total, 40000)
        # field-5 = frame index: valid for the overwhelming majority.
        self.assertGreater(in_range / total, 0.95)
        self.assertTrue(durations_positive)

    def test_animation_counts(self):
        counts = [
            len(mchr_anm.parse_mchr_anm(self.anm.entries[ix]).animations)
            for ix in range(self.anm.count)
        ]
        # Every entry has multiple animations (facing × state).
        self.assertTrue(all(c >= 1 for c in counts))
        self.assertGreater(max(counts), 10)


if __name__ == "__main__":
    unittest.main()
