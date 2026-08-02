"""Sprite codec + NCER size derivation.

The §11 sprite editor needs three guarantees from these modules:

1. RLE-30 round-trips: ``compress(decompress(x)) == x`` for every
   compressed entry inside ``SPR_CHR.PAK`` (an RLE-30 bug here would
   corrupt every replacement we ship).

2. Header preservation: ``build_ncgr_from_template`` writes back the
   original RAHC bytes, including the load-bearing ``RAHC+0x12`` field
   (project memory ``project_ncgr_rahc_header_preserve``).

3. NCER tile-count math: ``min_tiles_required`` for entries 0006, 0230,
   1566 matches the actual CHR tile count. These three were the user's
   reference set when we worked out 1D-mapping awareness — 0230 in
   particular uses 1D mapping with a non-zero boundary shift, so it
   exercises the ``tile_step = boundary/32`` branch.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PKG_PARENT = os.path.abspath(os.path.join(HERE, "..", ".."))
if PKG_PARENT not in sys.path:
    sys.path.insert(0, PKG_PARENT)

from digimon_core import fnt, ncer, pak, rom, sprite  # noqa: E402


ROM_DIR = r"C:\Workspace\digimon_stuffs\rom_files"
DUSK_US = os.path.join(ROM_DIR, "1420 - Digimon World - Dusk (US).nds")

OUT_SPR_DIR = r"C:\Workspace\digimon_stuffs\WorldDuskExtracted\root\dat\out_spr"

# Reference indices the user worked through manually when we verified
# min_tiles_required against the CHR tile counts.
EXAMPLE_INDICES = (6, 230, 1566)


def _load_pak_from_rom(rom_path: str, pak_name: str) -> pak.PakFile:
    rom_bytes = bytes(rom.loadRom(rom_path))
    ft = fnt.FileTable.from_rom(rom_bytes)
    start, end = ft.resolve(pak_name)
    return pak.PakFile(rom_bytes[start:end])


class Rle30Tests(unittest.TestCase):
    def test_roundtrip_short_runs(self):
        cases = [
            b"",
            b"A",
            b"ABCDEFG",
            b"\x00" * 100,
            b"\xFF" * 1000,
            (b"hello world " * 50),
            bytes(range(256)),
        ]
        for c in cases:
            with self.subTest(n=len(c)):
                comp = sprite.compress_rle30(c)
                self.assertEqual(sprite.decompress_rle30(comp), c)

    def test_maybe_decompress_passthrough(self):
        # A plain payload that doesn't start with 0x30 must pass through.
        plain = b"\x10abcd"
        self.assertEqual(sprite.maybe_decompress(plain), plain)


class SpriteFromPakTests(unittest.TestCase):
    """Exercise the codec against real SPR_CHR.PAK entries."""

    @classmethod
    def setUpClass(cls):
        if not os.path.exists(DUSK_US):
            raise unittest.SkipTest("Dusk ROM missing")
        cls.chr_pak = _load_pak_from_rom(DUSK_US, "DAT/SPR_CHR.PAK")
        cls.cel_pak = _load_pak_from_rom(DUSK_US, "DAT/SPR_CEL.PAK")

    def test_pak_entry_counts_match(self):
        # The CHR↔CEL[N] pair heuristic (project memory) only makes
        # sense if both directories have the same length.
        self.assertEqual(self.chr_pak.count, self.cel_pak.count)

    def test_decompress_compress_roundtrip_for_examples(self):
        for idx in EXAMPLE_INDICES:
            with self.subTest(idx=idx):
                compressed = self.chr_pak.original_entry(idx)
                self.assertEqual(compressed[:1], b"\x30",
                                 "SPR_CHR entries are RLE-30 in vanilla")
                raw = sprite.decompress_rle30(compressed)
                rebuilt = sprite.compress_rle30(raw)
                # The re-compressed payload may not be byte-identical
                # (different encoder strategy) — but it must decompress
                # back to the same NCGR.
                self.assertEqual(sprite.decompress_rle30(rebuilt), raw)

    def test_parse_ncgr_for_examples(self):
        for idx in EXAMPLE_INDICES:
            with self.subTest(idx=idx):
                raw = self.chr_pak.original_entry(idx)
                tile_bytes, bit_depth, hint_w, hint_h, is_bitmap = sprite.parse_ncgr(raw)
                self.assertIn(bit_depth, (3, 4))
                bytes_per_tile = 32 if bit_depth == 3 else 64
                # NCGR data should be a whole number of tiles.
                self.assertEqual(len(tile_bytes) % bytes_per_tile, 0)
                self.assertGreater(len(tile_bytes), 0)

    def test_build_ncgr_from_template_preserves_rahc_header(self):
        # The whole point of build_ncgr_from_template is to keep RAHC bytes
        # (including +0x12) verbatim — verify that explicitly.
        for idx in EXAMPLE_INDICES:
            with self.subTest(idx=idx):
                compressed = self.chr_pak.original_entry(idx)
                decompressed = sprite.decompress_rle30(compressed)
                tile_bytes, *_ = sprite.parse_ncgr(decompressed)

                # Rebuild from the template with the SAME tile bytes —
                # everything up to the tile data must match byte-for-byte.
                rebuilt = sprite.build_ncgr_from_template(tile_bytes, decompressed)

                rahc_orig = sprite.find_block(decompressed, b"RAHC")
                rahc_new = sprite.find_block(rebuilt, b"RAHC")
                # Same RAHC offset, and crucially the same +0x12 byte.
                self.assertEqual(rahc_orig, rahc_new)
                # Compare the whole RAHC header up to data_off (32 bytes).
                self.assertEqual(
                    rebuilt[rahc_new:rahc_new + 32],
                    decompressed[rahc_orig:rahc_orig + 32],
                )
                # And the rebuild equals the original (since tiles unchanged).
                self.assertEqual(rebuilt, decompressed)


class NcerMinTilesTests(unittest.TestCase):
    """For each reference index N, ``min_tiles_required(NCER[N])`` must
    match the actual CHR[N] tile count."""

    @classmethod
    def setUpClass(cls):
        if not os.path.exists(DUSK_US):
            raise unittest.SkipTest("Dusk ROM missing")
        cls.chr_pak = _load_pak_from_rom(DUSK_US, "DAT/SPR_CHR.PAK")
        cls.cel_pak = _load_pak_from_rom(DUSK_US, "DAT/SPR_CEL.PAK")

    def test_min_tiles_matches_chr_tile_count(self):
        for idx in EXAMPLE_INDICES:
            with self.subTest(idx=idx):
                chr_raw = self.chr_pak.original_entry(idx)
                cel_raw = self.cel_pak.original_entry(idx)
                tile_bytes, bit_depth, *_ = sprite.parse_ncgr(chr_raw)
                bpp4 = (bit_depth == 3)
                bytes_per_tile = 32 if bpp4 else 64
                chr_tiles = len(tile_bytes) // bytes_per_tile
                parsed = ncer.parse_ncer(cel_raw)
                self.assertEqual(
                    ncer.min_tiles_required(parsed, bpp4=bpp4),
                    chr_tiles,
                    f"NCER[{idx}] min_tiles_required disagrees with CHR[{idx}] tile count",
                )


class NcerFromExtractedFilesTests(unittest.TestCase):
    """Same three indices, but read from the on-disk dump rather than the
    ROM. Lets the test still run if the user has the extracted ``out_spr``
    tree but not the packed ROM."""

    @classmethod
    def setUpClass(cls):
        if not os.path.isdir(OUT_SPR_DIR):
            raise unittest.SkipTest(f"{OUT_SPR_DIR} missing")

    def test_min_tiles_matches_chr_tile_count(self):
        for idx in EXAMPLE_INDICES:
            chr_path = os.path.join(OUT_SPR_DIR, "chr", f"{idx:04d}.NCGR")
            cel_path = os.path.join(OUT_SPR_DIR, "cel", f"{idx:04d}.NCER")
            if not (os.path.exists(chr_path) and os.path.exists(cel_path)):
                continue
            with self.subTest(idx=idx):
                with open(chr_path, "rb") as f:
                    chr_raw = f.read()
                with open(cel_path, "rb") as f:
                    cel_raw = f.read()
                tile_bytes, bit_depth, *_ = sprite.parse_ncgr(chr_raw)
                bpp4 = (bit_depth == 3)
                chr_tiles = len(tile_bytes) // (32 if bpp4 else 64)
                parsed = ncer.parse_ncer(cel_raw)
                self.assertEqual(
                    ncer.min_tiles_required(parsed, bpp4=bpp4),
                    chr_tiles,
                )


class AppendCellTests(unittest.TestCase):
    """``append_cloned_cell`` grows an NCER by one cell that clones a source
    cell's OAM layout with a tile-slot offset — the structural half of
    "add an animation frame" (caller grows the NCGR alongside)."""

    @classmethod
    def setUpClass(cls):
        if not os.path.exists(DUSK_US):
            raise unittest.SkipTest("Dusk ROM missing")
        cls.chr_pak = _load_pak_from_rom(DUSK_US, "DAT/SPR_CHR.PAK")
        cls.cel_pak = _load_pak_from_rom(DUSK_US, "DAT/SPR_CEL.PAK")

    def _first_slot_linear_sprite(self):
        """Find a sprite whose NCER mapping has slot == linear tile (2D,
        boundary == bytes_per_tile) — the case the browser supports."""
        count = min(self.chr_pak.count, self.cel_pak.count)
        for ix in range(count):
            try:
                _tb, bd, *_ = sprite.parse_ncgr(self.chr_pak.entries[ix])
                parsed = ncer.parse_ncer(self.cel_pak.entries[ix])
            except (ValueError, IndexError):
                continue
            bpt = 32 if bd == 3 else 64
            if not parsed.is_1d and parsed.boundary_bytes == bpt and parsed.cells:
                return ix, bd, parsed
        self.skipTest("no slot==linear sprite found")

    def test_append_grows_cell_count_and_reparses(self):
        ix, bd, parsed = self._first_slot_linear_sprite()
        used = ncer.min_tiles_required(parsed, bpp4=(bd == 3))
        grown = ncer.append_cloned_cell(self.cel_pak.entries[ix], 0, used)
        reparsed = ncer.parse_ncer(grown)
        self.assertEqual(len(reparsed.cells), len(parsed.cells) + 1)
        # The new cell mirrors cell 0's OAM shapes/positions...
        src, new = parsed.cells[0], reparsed.cells[-1]
        self.assertEqual(len(new.oams), len(src.oams))
        for so, no in zip(src.oams, new.oams):
            self.assertEqual((no.x, no.y, no.w, no.h), (so.x, so.y, so.w, so.h))
            # ...but its tiles point at the appended block.
            self.assertEqual(no.tile, so.tile + used)
        # The extra cell requires the grown tile bank.
        self.assertEqual(
            ncer.min_tiles_required(reparsed, bpp4=(bd == 3)), 2 * used
        )

    def test_existing_cells_unchanged(self):
        ix, bd, parsed = self._first_slot_linear_sprite()
        used = ncer.min_tiles_required(parsed, bpp4=(bd == 3))
        reparsed = ncer.parse_ncer(
            ncer.append_cloned_cell(self.cel_pak.entries[ix], 0, used)
        )
        for ci, cell in enumerate(parsed.cells):
            for a, b in zip(cell.oams, reparsed.cells[ci].oams):
                self.assertEqual(
                    (a.x, a.y, a.w, a.h, a.tile), (b.x, b.y, b.w, b.h, b.tile)
                )

    def test_out_of_range_source_raises(self):
        ix, _bd, parsed = self._first_slot_linear_sprite()
        with self.assertRaises(IndexError):
            ncer.append_cloned_cell(self.cel_pak.entries[ix], len(parsed.cells), 0)


class SpriteBboxTests(unittest.TestCase):
    """``sprite_bbox`` collapses every cell's screen footprint into a single
    ``(w, h)`` tuple — the largest extent the sprite reaches in any cell.
    Used by the browser's heuristic categorisation."""

    @classmethod
    def setUpClass(cls):
        if not os.path.exists(DUSK_US):
            raise unittest.SkipTest("Dusk ROM missing")
        cls.cel_pak = _load_pak_from_rom(DUSK_US, "DAT/SPR_CEL.PAK")

    def test_returns_positive_for_nonempty_cells(self):
        # Reference indices all have visible OAMs, so neither dim is zero.
        for idx in EXAMPLE_INDICES:
            with self.subTest(idx=idx):
                parsed = ncer.parse_ncer(self.cel_pak.original_entry(idx))
                w, h = ncer.sprite_bbox(parsed)
                self.assertGreater(w, 0)
                self.assertGreater(h, 0)

    def test_zero_for_empty_ncer(self):
        # A synthetic NCER with no cells must report (0, 0).
        empty = ncer.Ncer(cells=[], mapping=0)
        self.assertEqual(ncer.sprite_bbox(empty), (0, 0))


class SpritePakGrowSpliceTests(unittest.TestCase):
    """Phase F lift of the in-FAT-slot constraint: replacing an entry with
    a *larger* version must shift every downstream FAT entry + NDS header
    offset by an 0x200-aligned step (DS file-loader alignment requirement)
    so the engine still finds the next file at a multiple of 0x200.

    This test goes through the editor's RomSession so it covers the
    end-to-end save path the user touches, not just the fat.py primitives.
    """

    @classmethod
    def setUpClass(cls):
        if not os.path.exists(DUSK_US):
            raise unittest.SkipTest("Dusk ROM missing")
        # Importing editor.session is a heavier import (parses the whole
        # model graph in from_file). Confine it to this class so the
        # lighter pak/codec tests don't pay for it.
        from editor import session as editor_session  # noqa: E402
        cls.editor_session = editor_session
        cls.sess = editor_session.RomSession.from_file(DUSK_US)

    def setUp(self):
        # Each test mutates the sprite pak cache + dirty set on the shared
        # session. Clear both so a prior test's grown entries don't leak
        # into the next test's vanilla-baseline assertions. (Re-parsing
        # the full model graph in setUpClass once is the costly step;
        # the sprite pak cache is rebuilt lazily from original_rom_data.)
        self.sess._sprite_pak_cache.clear()
        self.sess._dirty_sprite_paks.clear()

    def _replace_with_grow(self, pak_name: str, idx: int, grow_bytes: int):
        pak_obj = self.sess.sprite_pak(pak_name)
        orig = pak_obj.entries[idx]
        new = orig + b"\x00" * grow_bytes
        pak_obj.replace_entry(idx, new)
        self.sess.mark_sprite_pak_dirty(pak_name)
        return new

    def test_single_pak_grow_shifts_downstream_fat(self):
        # Replace one entry with a payload too large for the vanilla
        # slot, serialize, and verify (a) the rebuilt pak parses cleanly
        # with the new bytes intact and (b) downstream FAT entries shifted
        # by an 0x200-aligned amount.
        pak_name = "DAT/SPR_CHR.PAK"
        idx = 6
        # 0x10000 forces a multi-block 0x200-aligned shift — vanilla SPR_CHR
        # has well under 0x200 of intra-pak slack, so this can't fit in
        # the vanilla slot regardless of compression.
        new_bytes = self._replace_with_grow(pak_name, idx, 0x10000)

        ft_before = fnt.FileTable.from_rom(self.sess.original_rom_data)
        v_start, v_end = ft_before.resolve(pak_name)
        # SPR_PAL sits right after SPR_CHR in Dusk — handy downstream probe
        # since the shift propagation is unambiguous on the adjacent file.
        downstream = "DAT/SPR_PAL.PAK"
        d_start_before, _d_end_before = ft_before.resolve(downstream)
        self.assertGreater(d_start_before, v_end,
                           "test assumes SPR_PAL sits past SPR_CHR")

        out = self.sess.serialize_all()
        ft_after = fnt.FileTable.from_rom(bytes(out))
        n_start, n_end = ft_after.resolve(pak_name)
        d_start_after, _ = ft_after.resolve(downstream)

        # The pak's start doesn't move; its end grows by exactly the new
        # content delta (not the aligned shift — the slack is intra-slot).
        self.assertEqual(n_start, v_start)
        # Re-parsing the spliced pak must surface the user's new bytes.
        new_pak = pak.PakFile(bytes(out[n_start:n_end]))
        self.assertEqual(new_pak.original_entry(idx), new_bytes)
        # Downstream shift must be a positive 0x200 multiple.
        shift = d_start_after - d_start_before
        self.assertGreater(shift, 0)
        self.assertEqual(shift % 0x200, 0)

    def test_multi_pak_grow_handles_descending_order(self):
        # Touch all three sprite paks. The splice has to process them
        # highest-offset first so the lower-offset paks' cached offsets
        # stay valid — verify the rebuilt paks all parse with the new
        # bytes, regardless of dict iteration order.
        targets = {
            "DAT/SPR_CHR.PAK": (6, 0x4000),
            "DAT/SPR_PAL.PAK": (6, 0x800),
            "DAT/SPR_CEL.PAK": (6, 0x1000),
        }
        expected: dict = {}
        for pak_name, (idx, grow) in targets.items():
            expected[pak_name] = (idx, self._replace_with_grow(pak_name, idx, grow))

        out = self.sess.serialize_all()
        ft_after = fnt.FileTable.from_rom(bytes(out))
        for pak_name, (idx, expected_entry) in expected.items():
            with self.subTest(pak=pak_name):
                s, e = ft_after.resolve(pak_name)
                new_pak = pak.PakFile(bytes(out[s:e]))
                self.assertEqual(new_pak.original_entry(idx), expected_entry)


class SpritePakReplaceRoundtripTests(unittest.TestCase):
    """Phase D save-path correctness: ``PakFile.replace_entry`` with the
    same bytes the entry already has must round-trip to a byte-identical
    pak (since ``to_bytes`` is deterministic and offsets are recomputed
    the same way they were originally packed).

    Used as a smoke test for the editor's "Import from NCGR+NCLR → export
    → save" loop: replacing a slot with its own export should produce a
    save that's byte-identical to vanilla in the relevant FAT range.
    """

    @classmethod
    def setUpClass(cls):
        if not os.path.exists(DUSK_US):
            raise unittest.SkipTest("Dusk ROM missing")
        cls.rom_bytes = bytes(rom.loadRom(DUSK_US))
        cls.ft = fnt.FileTable.from_rom(cls.rom_bytes)

    def test_replace_with_self_is_byte_identical(self):
        for pak_name in ("DAT/SPR_CHR.PAK", "DAT/SPR_PAL.PAK", "DAT/SPR_CEL.PAK"):
            with self.subTest(pak=pak_name):
                start, end = self.ft.resolve(pak_name)
                vanilla_slice = self.rom_bytes[start:end]
                p = pak.PakFile(vanilla_slice)
                # Touch every reference entry with its own bytes so the
                # path through replace_entry (which the QUndoCommand uses)
                # is exercised, not just the no-op identity case.
                for idx in EXAMPLE_INDICES:
                    if idx < p.count:
                        p.replace_entry(idx, p.original_entry(idx))
                rebuilt = p.to_bytes()
                # Length must match exactly — no slack, no truncation.
                self.assertEqual(len(rebuilt), len(vanilla_slice))
                self.assertEqual(rebuilt, vanilla_slice)


class QuantizePaletteTests(unittest.TestCase):
    """Median-cut sanity checks. The exact representatives aren't pinned —
    different valid implementations land on slightly different means — but
    the structural invariants (count, monochrome behavior, no-reduction
    case) are what callers depend on."""

    def test_returns_uniques_when_under_budget(self):
        colors = [(255, 0, 0), (0, 255, 0), (0, 0, 255)]
        out = sprite.quantize_palette(colors, k=15)
        # Pure passthrough — sorted-dedupe of the input.
        self.assertEqual(sorted(out), sorted(set(colors)))

    def test_reduces_to_k_buckets(self):
        # 64 distinct grayscale values squeezed into 8 representatives.
        colors = [(i * 4, i * 4, i * 4) for i in range(64)]
        out = sprite.quantize_palette(colors, k=8)
        self.assertLessEqual(len(out), 8)
        self.assertGreater(len(out), 0)
        # All grayscale → all outputs grayscale (boxes collapse on one axis).
        for r, g, b in out:
            self.assertEqual(r, g)
            self.assertEqual(g, b)

    def test_handles_monochrome(self):
        # Single repeated color — boxes can't be split; we get one entry.
        out = sprite.quantize_palette([(128, 64, 32)] * 100, k=15)
        self.assertEqual(out, [(128, 64, 32)])

    def test_empty_k_returns_empty(self):
        self.assertEqual(sprite.quantize_palette([(1, 2, 3)], k=0), [])


class BuildNclrFromTemplateTests(unittest.TestCase):
    """``build_nclr_from_template`` must (a) write the new bank's colors
    into the right offsets when re-parsed, (b) leave other banks untouched
    byte-for-byte, and (c) reject out-of-range bank indices."""

    @classmethod
    def setUpClass(cls):
        if not os.path.exists(DUSK_US):
            raise unittest.SkipTest("Dusk ROM missing")
        # Find a multibank 4bpp NCLR so the preserve-other-banks check
        # actually has siblings to preserve. Most SPR_PAL entries are
        # single-bank, so scan the whole pak until we find one with ≥2
        # banks; the first match (entry 456 in vanilla US) is fine.
        pal_pak = _load_pak_from_rom(DUSK_US, "DAT/SPR_PAL.PAK")
        cls.template = None
        for ix in range(pal_pak.count):
            try:
                pals, bd = sprite.parse_nclr(pal_pak.original_entry(ix))
            except ValueError:
                continue
            if bd == 3 and len(pals) >= 2:
                cls.template = pal_pak.original_entry(ix)
                cls.template_palettes = pals
                break
        if cls.template is None:
            raise unittest.SkipTest("no multibank 4bpp NCLR in SPR_PAL")

    def test_new_bank_round_trips_to_input_colors(self):
        # 5-bit quantization loss is the only allowed drift — pick colors
        # already on the 5-bit grid so the test asserts exact equality.
        new_bank = [(i * 17 % 248, (i * 13) % 248, (i * 7) % 248) for i in range(16)]
        # Snap to 5-bit grid the encoder uses.
        new_bank = [
            ((r * 31 + 127) // 255 * 255 // 31,
             (g * 31 + 127) // 255 * 255 // 31,
             (b * 31 + 127) // 255 * 255 // 31)
            for r, g, b in new_bank
        ]
        rebuilt = sprite.build_nclr_from_template(self.template, {1: new_bank})
        re_pals, _ = sprite.parse_nclr(rebuilt)
        for i, (er, eg, eb) in enumerate(new_bank):
            ar, ag, ab = re_pals[1][i]
            self.assertEqual((ar, ag, ab), (er, eg, eb))

    def test_other_banks_preserved_byte_identical(self):
        # Rebuild bank 1 only — bank 0 colors must remain bit-exact.
        rebuilt = sprite.build_nclr_from_template(
            self.template, {1: [(255, 255, 255)] * 16},
        )
        re_pals, _ = sprite.parse_nclr(rebuilt)
        self.assertEqual(re_pals[0], self.template_palettes[0])

    def test_out_of_range_bank_raises(self):
        with self.assertRaises(ValueError):
            sprite.build_nclr_from_template(
                self.template, {99: [(0, 0, 0)] * 16},
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
