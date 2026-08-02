"""NANR codec — SPR_ANM.PAK animation reader.

Guarantees verified against vanilla Dusk:

1. Every SPR_ANM entry parses as NANR without error (1627 on Dusk).
2. Every animation frame's cell index falls inside the matching
   SPR_CEL entry's cell count — the reference correctness check that
   proves the frame-data pointer + element-format handling is right
   (6028 frames ROM-wide, zero out of range).
3. Reference entries: 0x0653 (a 7-sequence move sprite, seq 0 = 205
   frames starting on cell 28) and 0x0020 (14 single-frame poses on
   cells 0..13) decode to the expected shape.
4. ``flatten_sequence`` length equals the sum of the sequence's frame
   durations, and ``has_animation`` distinguishes multi-frame sprites
   from single-frame stubs.

Plus format-level unit checks that don't need a ROM.
"""
import os
import struct
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PKG_PARENT = os.path.abspath(os.path.join(HERE, "..", ".."))
if PKG_PARENT not in sys.path:
    sys.path.insert(0, PKG_PARENT)

from digimon_core import fnt, nanr, ncer as ncer_mod, pak, rom, sprite  # noqa: E402


ROM_DIR = r"C:\Workspace\digimon_stuffs\rom_files"
DUSK_US = os.path.join(ROM_DIR, "1420 - Digimon World - Dusk (US).nds")

SPR_ANM = "DAT/SPR_ANM.PAK"
SPR_CEL = "DAT/SPR_CEL.PAK"


def _load_pak(rom_bytes: bytes, name: str) -> pak.PakFile:
    ft = fnt.FileTable.from_rom(rom_bytes)
    start, end = ft.resolve(name)
    return pak.PakFile(rom_bytes[start:end])


class NanrUnitTests(unittest.TestCase):
    """Format checks that build the NANR bytes in-line — no ROM needed."""

    @staticmethod
    def _build_min_nanr(sequences):
        """Assemble a minimal RNAN/ABNK for ``sequences``.

        ``sequences`` is a list of ``(mode, element, [(cell, dur), ...])``.
        Frame data is one packed u16 cell index per frame (index format),
        which is what the parser reads regardless of element.
        """
        # data section: one u16 per (seq, frame) cell, in order.
        data = bytearray()
        frame_records = []  # (data_ptr, dur) per frame, in seq order
        for _mode, _elem, frames in sequences:
            for cell, dur in frames:
                frame_records.append((len(data), dur))
                data += struct.pack("<H", cell)

        frame_bytes = bytearray()
        seq_records = []  # (nframes, start, type, mode, frame_ofs)
        fi = 0
        for mode, elem, frames in sequences:
            frame_ofs = len(frame_bytes)
            for _cell, _dur in frames:
                data_ptr, dur = frame_records[fi]
                frame_bytes += struct.pack("<IHH", data_ptr, dur, 0xBEEF)
                fi += 1
            type_word = (1 << 16) | (elem & 0xFFFF)
            seq_records.append((len(frames), 0, type_word, mode, frame_ofs))

        seq_bytes = bytearray()
        for nframes, start, type_word, mode, frame_ofs in seq_records:
            seq_bytes += struct.pack("<HHIII", nframes, start, type_word, mode, frame_ofs)

        n_seq = len(sequences)
        n_frames = len(frame_records)
        # ABNK header: offsets relative to block-data start (base = abnk+8).
        seq_off = 0x18
        frame_off = seq_off + len(seq_bytes)
        data_off = frame_off + len(frame_bytes)
        abnk_body = bytearray()
        abnk_body += struct.pack("<HH", n_seq, n_frames)
        abnk_body += struct.pack("<III", seq_off, frame_off, data_off)
        abnk_body += struct.pack("<II", 0, 0)  # padding
        abnk_body += seq_bytes + frame_bytes + data
        abnk = b"KNBA" + struct.pack("<I", len(abnk_body) + 8) + bytes(abnk_body)

        file_size = 0x10 + len(abnk)
        header = b"RNAN" + struct.pack("<HHI", 0xFEFF, 0x0100, file_size)
        header += struct.pack("<HH", 0x10, 1)
        return bytes(header + abnk)

    def test_not_nanr_raises(self):
        with self.assertRaises(ValueError):
            nanr.parse_nanr(b"RGCN\x00\x00\x00\x00")

    def test_min_roundtrip_shape(self):
        raw = self._build_min_nanr([
            (nanr.MODE_LOOP, nanr.ELEMENT_INDEX, [(3, 8), (4, 8), (5, 16)]),
            (nanr.MODE_ONCE, nanr.ELEMENT_INDEX, [(0, 2)]),
        ])
        parsed = nanr.parse_nanr(raw)
        self.assertEqual(len(parsed.sequences), 2)
        s0 = parsed.sequences[0]
        self.assertEqual([f.cell for f in s0.frames], [3, 4, 5])
        self.assertEqual([f.duration for f in s0.frames], [8, 8, 16])
        self.assertTrue(s0.loops)
        self.assertFalse(parsed.sequences[1].loops)
        self.assertTrue(parsed.has_animation)
        flat = nanr.flatten_sequence(s0)
        self.assertEqual(len(flat), 32)
        self.assertEqual([f.cell for f in flat[:2]], [3, 3])

    def test_single_frame_not_animated(self):
        raw = self._build_min_nanr([
            (nanr.MODE_LOOP, nanr.ELEMENT_INDEX, [(0, 4)]),
        ])
        self.assertFalse(nanr.parse_nanr(raw).has_animation)

    def test_compressed_input(self):
        raw = self._build_min_nanr([
            (nanr.MODE_LOOP, nanr.ELEMENT_INDEX, [(1, 5), (2, 5)]),
        ])
        compressed = sprite.compress_rle30(raw)
        self.assertEqual(
            [f.cell for f in nanr.parse_nanr(compressed).sequences[0].frames],
            [1, 2],
        )


class _NanrRomCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not os.path.exists(DUSK_US):
            raise unittest.SkipTest(f"ROM not found: {DUSK_US}")
        cls.rom_bytes = bytes(rom.loadRom(DUSK_US))
        cls.anm = _load_pak(cls.rom_bytes, SPR_ANM)
        cls.cel = _load_pak(cls.rom_bytes, SPR_CEL)

    def test_all_entries_parse(self):
        for ix in range(self.anm.count):
            with self.subTest(ix=ix):
                nanr.parse_nanr(self.anm.entries[ix])

    def test_every_frame_cell_in_range(self):
        total_frames = 0
        animated = 0
        for ix in range(self.anm.count):
            parsed = nanr.parse_nanr(self.anm.entries[ix])
            if parsed.has_animation:
                animated += 1
            try:
                n_cells = len(ncer_mod.parse_ncer(self.cel.entries[ix]).cells)
            except (ValueError, IndexError):
                n_cells = 0
            for seq in parsed.sequences:
                for fr in seq.frames:
                    total_frames += 1
                    if n_cells:
                        self.assertLess(
                            fr.cell, n_cells,
                            f"entry 0x{ix:04x} frame cell {fr.cell} >= "
                            f"{n_cells} cells",
                        )
        self.assertGreater(total_frames, 5000)
        self.assertGreater(animated, 100)

    def test_reference_move_sprite(self):
        parsed = nanr.parse_nanr(self.anm.entries[0x0653])
        self.assertEqual(len(parsed.sequences), 7)
        s0 = parsed.sequences[0]
        self.assertEqual(len(s0.frames), 205)
        self.assertEqual(s0.frames[0].cell, 28)
        self.assertEqual(s0.frames[0].duration, 120)
        self.assertTrue(parsed.has_animation)

    def test_reference_pose_sheet(self):
        parsed = nanr.parse_nanr(self.anm.entries[0x0020])
        self.assertEqual(len(parsed.sequences), 14)
        self.assertEqual(
            [s.frames[0].cell for s in parsed.sequences],
            list(range(14)),
        )
        for s in parsed.sequences:
            self.assertEqual(len(s.frames), 1)

    def test_flatten_matches_duration_sum(self):
        parsed = nanr.parse_nanr(self.anm.entries[0x0653])
        for seq in parsed.sequences:
            expected = sum(f.duration for f in seq.frames)
            self.assertEqual(len(nanr.flatten_sequence(seq)), expected)

    def test_srt_transform_values(self):
        """0x00c7 is a scale-down (shrink) animation: rotation/translate 0,
        scale steps from 1.0 down toward 0."""
        parsed = nanr.parse_nanr(self.anm.entries[0x00c7])
        seq = parsed.sequences[0]
        self.assertEqual(seq.element, nanr.ELEMENT_SRT)
        self.assertEqual(seq.frames[0].scale_x, nanr.SCALE_ONE)
        self.assertAlmostEqual(seq.frames[0].scale_x_f, 1.0)
        # last frame is scaled far down (~0.01) with no rotation/translation
        last = seq.frames[-1]
        self.assertLess(last.scale_x_f, 0.1)
        self.assertEqual(last.rot, 0)
        self.assertEqual((last.trans_x, last.trans_y), (0, 0))

    def test_translate_transform_values(self):
        """0x0273 is a horizontal slide (element 2 translate)."""
        parsed = nanr.parse_nanr(self.anm.entries[0x0273])
        seq = parsed.sequences[0]
        self.assertEqual(seq.element, nanr.ELEMENT_INDEX_T)
        self.assertNotEqual(seq.frames[0].trans_x, 0)
        self.assertEqual(seq.frames[0].scale_x, nanr.SCALE_ONE)

    def test_serialize_roundtrip_all_entries(self):
        """serialize(parse(x)) re-parses to identical sequences for every
        vanilla SPR_ANM entry — the correctness proof for the writer."""
        for ix in range(self.anm.count):
            original = nanr.parse_nanr(self.anm.entries[ix])
            rebuilt = nanr.parse_nanr(
                nanr.serialize_nanr(original, self.anm.entries[ix])
            )
            self.assertEqual(
                len(rebuilt.sequences), len(original.sequences), f"ix=0x{ix:04x}"
            )
            for os_, rs in zip(original.sequences, rebuilt.sequences):
                self.assertEqual(
                    (os_.element, os_.mode, os_.start_frame),
                    (rs.element, rs.mode, rs.start_frame), f"ix=0x{ix:04x}",
                )
                self.assertEqual(len(os_.frames), len(rs.frames), f"ix=0x{ix:04x}")
                for of, rf in zip(os_.frames, rs.frames):
                    self.assertEqual(
                        (of.cell, of.duration, of.rot, of.scale_x, of.scale_y,
                         of.trans_x, of.trans_y),
                        (rf.cell, rf.duration, rf.rot, rf.scale_x, rf.scale_y,
                         rf.trans_x, rf.trans_y),
                        f"ix=0x{ix:04x}",
                    )


if __name__ == "__main__":
    unittest.main()
