"""MCHR codec — port of the standalone ``mchr_chr_unpack.py`` decoder.

The §13 overworld-sprite editor (MCHR_CHR.PAK / MCHR_PAL.PAK) needs four
guarantees from this module before any UI work touches it:

1. **PAK directories parse**: both paks present the expected entry counts
   (CHR=890, PAL=906 for vanilla Dusk + Dawn) and every entry's payload
   round-trips through RLE-30.
2. **Header decode**: every CHR entry decompresses to ``(frame_count,
   bytes_per_frame, raw_tiles)`` with ``bytes_per_frame`` a multiple of
   32 (one 8x8 4bpp tile). The NDS-OAM heuristic resolves every observed
   tile count without hitting the fallback.
3. **Frame round-trip**: ``encode_mchr_chr_entry(parse_mchr_chr_entry(x))
   == x``. Catches drift in either function before it corrupts a save.
4. **Palette round-trip**: BGR555 decode → encode is bit-exact (5-bit
   channels mean encode must throw away the bottom 3 bits and bit-replicate
   on decode — both sides must agree).
"""
import os
import sys
import unittest
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
PKG_PARENT = os.path.abspath(os.path.join(HERE, "..", ".."))
if PKG_PARENT not in sys.path:
    sys.path.insert(0, PKG_PARENT)

from digimon_core import fnt, loaders, mchr, mchr_anm, pak, rom, sprite  # noqa: E402


ROM_DIR = r"C:\Workspace\digimon_stuffs\rom_files"
ROM_PATHS = {
    "DUSK_US": os.path.join(ROM_DIR, "1420 - Digimon World - Dusk (US).nds"),
    "DAWN_US": os.path.join(ROM_DIR, "1421 - Digimon World - Dawn (USA).nds"),
}

# Tile counts that show up in vanilla MCHR_CHR.PAK (Dusk survey 2026-06-05).
# Anything outside this set means a mod added an exotic shape — the codec
# still handles it via pick_tile_grid's fallback, but vanilla shouldn't hit
# the fallback.
VANILLA_TILE_COUNTS = {4, 8, 16, 32, 64}


def _load_pak_from_rom(rom_path: str, pak_name: str) -> pak.PakFile:
    rom_bytes = bytes(rom.loadRom(rom_path))
    ft = fnt.FileTable.from_rom(rom_bytes)
    start, end = ft.resolve(pak_name)
    return pak.PakFile(rom_bytes[start:end])


class TileGridTests(unittest.TestCase):
    """NDS-OAM table covers every vanilla tile count, prefers tall."""

    def test_oam_legal_counts(self):
        # All seven NDS-OAM-legal tile counts must hit the lookup table.
        self.assertEqual(mchr.pick_tile_grid(1),  (1, 1))
        self.assertEqual(mchr.pick_tile_grid(2),  (1, 2))   # tall
        self.assertEqual(mchr.pick_tile_grid(4),  (2, 2))   # square
        self.assertEqual(mchr.pick_tile_grid(8),  (2, 4))   # tall — main char
        self.assertEqual(mchr.pick_tile_grid(16), (4, 4))   # square
        self.assertEqual(mchr.pick_tile_grid(32), (4, 8))   # tall — large NPC
        self.assertEqual(mchr.pick_tile_grid(64), (8, 8))   # square

    def test_fallback_prefers_tall(self):
        # A non-OAM-legal count goes through the near-square fallback; tall
        # is preferred over wide so a 6-tile sprite reads as 2x3 not 3x2.
        wt, ht = mchr.pick_tile_grid(6)
        self.assertLessEqual(wt, ht)
        self.assertEqual(wt * ht, 6)

    def test_zero_or_negative_safe(self):
        self.assertEqual(mchr.pick_tile_grid(0),  (1, 1))
        self.assertEqual(mchr.pick_tile_grid(-1), (1, 1))


class PaletteRoundtripTests(unittest.TestCase):
    """BGR555 encode/decode is bit-exact (with the 5-bit truncation)."""

    def test_known_colors(self):
        # White, black, pure channels — all preserve through the 5-bit
        # truncation because their channels are 0 or 0xFF.
        cases = [(0, 0, 0), (255, 255, 255), (255, 0, 0), (0, 255, 0),
                 (0, 0, 255)]
        encoded = mchr.encode_palette_bgr555(cases)
        decoded = mchr.parse_palette_bgr555(encoded)
        self.assertEqual(decoded, cases)

    def test_truncation_is_idempotent(self):
        # Any 8-bit color reduces to 5+3 bits on encode; the same encoded
        # word must round-trip to itself.
        pal = [(i, 255 - i, (i * 7) & 0xFF) for i in range(0, 256, 8)]
        once = mchr.parse_palette_bgr555(mchr.encode_palette_bgr555(pal))
        twice = mchr.parse_palette_bgr555(mchr.encode_palette_bgr555(once))
        self.assertEqual(once, twice)


class EncodeEntryTests(unittest.TestCase):
    """Synthetic encode_mchr_chr_entry sanity (no ROM needed)."""

    def test_basic_pack(self):
        f0 = bytes(range(32))            # one 8x8 tile of varying bytes
        f1 = bytes([0xAA] * 32)
        out = mchr.encode_mchr_chr_entry([f0, f1])
        entry = mchr.parse_mchr_chr_entry(out)
        self.assertEqual(entry.frame_count, 2)
        self.assertEqual(entry.bytes_per_frame, 32)
        self.assertEqual(entry.frames, (f0, f1))

    def test_rejects_mismatched_frame_lengths(self):
        with self.assertRaises(ValueError):
            mchr.encode_mchr_chr_entry([bytes(32), bytes(64)])

    def test_rejects_non_tile_multiple(self):
        with self.assertRaises(ValueError):
            mchr.encode_mchr_chr_entry([bytes(30)])

    def test_rejects_empty(self):
        with self.assertRaises(ValueError):
            mchr.encode_mchr_chr_entry([])


class EncodeFrameTests(unittest.TestCase):
    """encode_frame_from_indices ↔ render_frame_rgba round-trip."""

    def test_indices_to_tiles_roundtrip_via_decoded_frame(self):
        # Build a known frame, decode it, harvest indices, re-encode,
        # confirm we recover the original tile bytes.
        original = bytes(range(32)) + bytes(range(31, -1, -1))  # 2 tiles
        # Palette big enough that every nibble (0..15) maps to a distinct
        # opaque color — so we can recover indices by RGB equality.
        palette = [(i * 16, 255 - i * 16, i * 8) for i in range(16)]

        rgba, w, h = mchr.render_frame_rgba(
            original, palette, transparent_index_0=False
        )
        # Recover indices via reverse RGB lookup.
        rgb_to_idx = {(r, g, b): i for i, (r, g, b) in enumerate(palette)}
        indices = []
        for y in range(h):
            for x in range(w):
                off = (y * w + x) * 4
                indices.append(rgb_to_idx[
                    (rgba[off], rgba[off + 1], rgba[off + 2])
                ])
        rebuilt = mchr.encode_frame_from_indices(indices, w, h)
        self.assertEqual(rebuilt, original)

    def test_encode_rejects_unaligned_dimensions(self):
        with self.assertRaises(ValueError):
            mchr.encode_frame_from_indices([0] * (15 * 8), 15, 8)

    def test_encode_rejects_wrong_buffer_size(self):
        with self.assertRaises(ValueError):
            mchr.encode_frame_from_indices([0] * 63, 8, 8)


class DecodeFrameIndicesTests(unittest.TestCase):
    """decode_frame_to_indices ↔ encode_frame_from_indices are inverses."""

    def test_roundtrip_known_frame(self):
        original = bytes(range(32)) + bytes(range(31, -1, -1))  # 2 tiles
        indices, w, h = mchr.decode_frame_to_indices(original)
        self.assertEqual(len(indices), w * h)
        # Every index must be a 4-bit value — anything bigger means we
        # accidentally widened to a byte without masking.
        self.assertTrue(all(0 <= i <= 15 for i in indices))
        rebuilt = mchr.encode_frame_from_indices(indices, w, h)
        self.assertEqual(rebuilt, original)

    def test_width_override_respected(self):
        # 4-tile frame: default is 2×2 (16×16). Forcing width_tiles=4 should
        # give 4×1 → 32×8.
        frame = bytes(range(32)) * 4
        indices, w, h = mchr.decode_frame_to_indices(frame, width_tiles=4)
        self.assertEqual((w, h), (32, 8))
        self.assertEqual(len(indices), 32 * 8)


class QuantizeRgbaTests(unittest.TestCase):
    """RGBA→palette index quantization handles exact, near, transparent."""

    def test_exact_palette_color_returns_index(self):
        pal = [(0, 0, 0), (255, 0, 0), (0, 255, 0), (0, 0, 255)]
        rgba = bytes([
            255, 0, 0, 255,
            0, 255, 0, 255,
            0, 0, 255, 255,
            0, 0, 0, 255,
        ])
        self.assertEqual(
            mchr.quantize_rgba_to_indices(rgba, pal), [1, 2, 3, 0]
        )

    def test_alpha_zero_maps_to_index_0_when_transparent(self):
        pal = [(0, 0, 0), (255, 255, 255)]
        # A fully transparent white pixel goes to 0 (transparent slot) even
        # though its RGB matches index 1 — alpha wins.
        rgba = bytes([255, 255, 255, 0])
        self.assertEqual(mchr.quantize_rgba_to_indices(rgba, pal), [0])

    def test_alpha_ignored_when_transparent_disabled(self):
        pal = [(0, 0, 0), (255, 255, 255)]
        rgba = bytes([255, 255, 255, 0])
        self.assertEqual(
            mchr.quantize_rgba_to_indices(
                rgba, pal, transparent_index_0=False
            ),
            [1],
        )

    def test_picks_nearest_in_rgb_space(self):
        pal = [(0, 0, 0), (100, 100, 100), (200, 200, 200)]
        # 90 → closest to 100 (idx 1), 180 → closest to 200 (idx 2).
        rgba = bytes([90, 90, 90, 255, 180, 180, 180, 255])
        self.assertEqual(mchr.quantize_rgba_to_indices(rgba, pal), [1, 2])

    def test_rejects_misaligned_buffer(self):
        with self.assertRaises(ValueError):
            mchr.quantize_rgba_to_indices(b"\x00" * 7, [(0, 0, 0)])


class _McharRomBase:
    """Per-version ROM tests. Subclassed by Dusk + Dawn fixtures."""

    VERSION: str = ""

    @classmethod
    def setUpClass(cls):
        path = ROM_PATHS[cls.VERSION]
        if not os.path.exists(path):
            raise unittest.SkipTest(f"ROM missing: {path}")
        cls.chr_pak = _load_pak_from_rom(path, "DAT/MCHR_CHR.PAK")
        cls.pal_pak = _load_pak_from_rom(path, "DAT/MCHR_PAL.PAK")
        cls.anm_pak = _load_pak_from_rom(path, "DAT/MCHR_ANM.PAK")
        cls.rom_data = bytes(rom.loadRom(path))

    def test_pak_entry_counts(self):
        # Both regions ship 890 CHR + 906 PAL — 16 extra palettes whose
        # mapping to CHR indices is still being worked out (see codec
        # docstring). The codec doesn't depend on these counts being equal.
        self.assertEqual(self.chr_pak.count, 890)
        self.assertEqual(self.pal_pak.count, 906)

    def test_every_chr_entry_decompresses_and_parses(self):
        # End-to-end check: 890 entries × RLE-30 decompress × header parse.
        # Failures here usually mean a corrupted entry or an off-by-one in
        # the standard pak directory reader.
        for i in range(self.chr_pak.count):
            with self.subTest(chr_idx=i):
                raw = self.chr_pak.original_entry(i)
                self.assertEqual(raw[:1], b"\x30",
                                 "vanilla MCHR_CHR entries are RLE-30")
                decompressed = sprite.decompress_rle30(raw)
                entry = mchr.parse_mchr_chr_entry(decompressed)
                self.assertGreater(entry.frame_count, 0)
                self.assertEqual(entry.bytes_per_frame % 32, 0)

    def test_every_chr_entry_round_trips(self):
        # parse(encode(parse(x))) is the load-bearing chain: a sprite
        # written back unchanged must decode to the same frame tuple.
        for i in range(self.chr_pak.count):
            with self.subTest(chr_idx=i):
                decompressed = sprite.decompress_rle30(
                    self.chr_pak.original_entry(i)
                )
                entry = mchr.parse_mchr_chr_entry(decompressed)
                rebuilt = mchr.encode_mchr_chr_entry(list(entry.frames))
                self.assertEqual(rebuilt, decompressed)

    def test_tile_counts_are_oam_legal(self):
        # Every vanilla entry's tile count belongs to NDS-OAM-legal sizes
        # (1/2/4/8/16/32/64). If a new tile count shows up here, the
        # _NDS_OAM_TILE_GRID table should be extended rather than relying
        # on the fallback.
        counts = Counter()
        for i in range(self.chr_pak.count):
            entry = mchr.parse_mchr_chr_entry(
                sprite.decompress_rle30(self.chr_pak.original_entry(i))
            )
            counts[entry.tiles_per_frame] += 1
        unexpected = set(counts) - VANILLA_TILE_COUNTS
        self.assertFalse(
            unexpected,
            f"unexpected tile counts in vanilla: {sorted(unexpected)}",
        )

    def test_palette_decode_size(self):
        # Most banks decompress to 32 bytes (16 colors). Anything else is
        # surprising enough to surface — we want a heads-up before the
        # editor's palette spinner shows wrong-sized palettes.
        odd = []
        for i in range(self.pal_pak.count):
            decompressed = sprite.decompress_rle30(self.pal_pak.original_entry(i))
            if len(decompressed) != 32:
                odd.append((i, len(decompressed)))
        # The vanilla survey on Dusk found zero outliers — surface any.
        self.assertEqual(odd, [], f"non-32B palette banks: {odd[:10]}")

    def test_chr_to_pal_map_resolves_drift(self):
        # The ARM9 overworld-sprite table gives every CHR its canonical PAL.
        m = loaders.loadMchrChrToPalMap(self.VERSION, self.rom_data)
        self.assertTrue(m, "table failed to validate — offset likely wrong")
        # Every CHR entry is covered, none maps outside MCHR_PAL.
        self.assertEqual(sorted(m), list(range(self.chr_pak.count)))
        self.assertTrue(all(0 <= p < self.pal_pak.count for p in m.values()))
        # Identity holds through the 1:1 region.
        for c in (0, 1, 100, 500, 0x296):
            self.assertEqual(m[c], c)
        # First divergence at 0x297, and the chest at 0x2ed -> 0x2fb (the
        # bug this map fixes: naive chr==pal gave the wrong-coloured 0x2ed).
        self.assertEqual(m[0x297], 0x298)
        self.assertEqual(m[0x2ed], 0x2fb)
        # Last CHR carries the full +16 accumulated palette offset.
        self.assertEqual(m[self.chr_pak.count - 1], self.pal_pak.count - 1)
        # Monotonic non-decreasing, matching the engine's ordering.
        pals = [m[c] for c in range(self.chr_pak.count)]
        self.assertEqual(pals, sorted(pals))

    def test_ow_to_chr_map_resolves_graphic(self):
        # Field-map / cutscene objects key by ow-id (906-space); resolve each
        # to its CHR graphic (the table's f0).
        m = loaders.loadMchrOwToChrMap(self.VERSION, self.rom_data)
        self.assertTrue(m, "table failed to validate — offset likely wrong")
        self.assertEqual(sorted(m), list(range(self.pal_pak.count)))  # 906 ow-ids
        self.assertTrue(all(0 <= c < self.chr_pak.count for c in m.values()))
        # Identity through the 1:1 region.
        for i in (0, 1, 100, 0x296):
            self.assertEqual(m[i], i)
        # Chest object: ow-id 0x2fb -> CHR 0x2ed (the graphic this map fixes,
        # vs. rendering the unrelated CHR-0x2fb pyramid). Its recolor sibling
        # 0x2fc shares the same graphic.
        self.assertEqual(m[0x2fb], 0x2ed)
        self.assertEqual(m[0x2fc], 0x2ed)

    def test_anm_oam_grid_fixes_sprite_width(self):
        # The MCHR_ANM OAM shape+size gives each sprite's true tile grid,
        # resolving the wide/tall ambiguity pick_tile_grid only guesses. It
        # must agree with the CHR tile count for effectively every sprite,
        # and it surfaces the genuinely-wide sprites pick_tile_grid misses.
        matched = wide = 0
        grid_2ca = None
        for i in range(self.chr_pak.count):
            tc = mchr.parse_mchr_chr_entry(
                sprite.decompress_rle30(self.chr_pak.original_entry(i))
            ).tiles_per_frame
            grid = mchr_anm.oam_grid_from_header(self.anm_pak.original_entry(i))
            if grid is None:
                continue
            if grid[0] * grid[1] == tc:
                matched += 1
                if grid[0] > grid[1]:
                    wide += 1
            if i == 0x2ca:
                grid_2ca = grid
        # 889/890 agree on vanilla Dusk+Dawn (one outlier ANM header).
        self.assertGreaterEqual(matched, self.chr_pak.count - 2)
        self.assertGreater(wide, 0)  # wide sprites exist and are detected
        # The wide field object from the report: 8x4 tiles = 64x32, which
        # pick_tile_grid(32) would wrongly render as 4x8 (tall/squished).
        self.assertEqual(grid_2ca, (8, 4))
        self.assertEqual(mchr.pick_tile_grid(32), (4, 8))

    def test_unknown_version_returns_empty_maps(self):
        # Callers treat empty as "fall back to identity" — never raise.
        self.assertEqual(loaders.loadMchrChrToPalMap("NOPE", self.rom_data), {})
        self.assertEqual(loaders.loadMchrOwToChrMap("NOPE", self.rom_data), {})

    def test_palette_round_trips_for_sample_indices(self):
        # Bit-exact round-trip on a handful of real palettes (running the
        # full 906 is overkill — each one is the same arithmetic).
        for pal_idx in (0, 100, 500, self.pal_pak.count - 1):
            with self.subTest(pal_idx=pal_idx):
                decompressed = sprite.decompress_rle30(
                    self.pal_pak.original_entry(pal_idx)
                )
                colors = mchr.parse_palette_bgr555(decompressed)
                rebuilt = mchr.encode_palette_bgr555(colors)
                self.assertEqual(rebuilt, decompressed)


class DuskUsMchr(_McharRomBase, unittest.TestCase):
    VERSION = "DUSK_US"


class DawnUsMchr(_McharRomBase, unittest.TestCase):
    VERSION = "DAWN_US"


if __name__ == "__main__":
    unittest.main(verbosity=2)
