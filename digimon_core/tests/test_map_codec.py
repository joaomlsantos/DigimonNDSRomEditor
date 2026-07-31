"""Field-map codec — PLAN.md §14.5 Phase A acceptance.

Four guarantees from :mod:`digimon_core.map`:

1. **Discovery**: ``discover_map_ids`` returns 265 ids on vanilla Dusk,
   sorted numerically (``"0"`` through ``"264"``).

2. **MapFiles paths**: ids resolve to the documented eight FAT paths.

3. **Round-trip per suffix**: for every map id and every present
   suffix, ``build_X(parse_X(x)) == x`` byte-for-byte on the
   *decompressed* payload. This is the codec invariant the save path
   relies on — a no-edit save through ``decompress → parse → build``
   must reproduce the original bytes.

4. **Render parity**: ``render_map_from_file_table`` matches the
   standalone ``map_render.py``'s PNG output exactly. The renderer
   doesn't go through BGR555 quantization (palette parse returns the
   bit-replicated 8-bit RGB the standalone script also emits), so the
   comparison is exact byte-equality, not ±1.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PKG_PARENT = os.path.abspath(os.path.join(HERE, "..", ".."))
if PKG_PARENT not in sys.path:
    sys.path.insert(0, PKG_PARENT)

from digimon_core import fnt, rom  # noqa: E402
from digimon_core import map as mapmod  # noqa: E402
from digimon_core.sprite import maybe_decompress  # noqa: E402


ROM_DIR = r"C:\Workspace\digimon_stuffs\rom_files"
DUSK_US = os.path.join(ROM_DIR, "1420 - Digimon World - Dusk (US).nds")
DAWN_US = os.path.join(ROM_DIR, "1421 - Digimon World - Dawn (USA).nds")

PREVIEWS_DIR = r"C:\Workspace\digimon_stuffs\research_docs\claude_notes\_map_previews"


def _load_ft_rom(path: str):
    raw = bytes(rom.loadRom(path))
    return fnt.FileTable.from_rom(raw), raw


class DiscoveryTests(unittest.TestCase):
    def test_discover_dusk_us(self):
        if not os.path.exists(DUSK_US):
            self.skipTest("Dusk ROM missing")
        ft, _ = _load_ft_rom(DUSK_US)
        ids = mapmod.discover_map_ids(ft)
        self.assertEqual(len(ids), 265)
        self.assertEqual(ids[0], "0")
        self.assertEqual(ids[-1], "264")
        nums = [int(i) for i in ids]
        self.assertEqual(nums, sorted(nums))


class MapFilesTests(unittest.TestCase):
    def test_paths_for_id(self):
        f = mapmod.MapFiles("42")
        self.assertEqual(f.layer_a_tiles, "DAT/map/42a.c")
        self.assertEqual(f.layer_a_palette, "DAT/map/42a.p")
        self.assertEqual(f.layer_a_screen, "DAT/map/42a.s")
        self.assertEqual(f.layer_b_tiles, "DAT/map/42b.c")
        self.assertEqual(f.layer_b_palette, "DAT/map/42b.p")
        self.assertEqual(f.layer_b_screen, "DAT/map/42b.s")
        self.assertEqual(f.descriptor, "DAT/map/42.d")
        self.assertEqual(f.walkability, "DAT/map/42.0t")
        self.assertEqual(f.attributes, "DAT/map/42.a")
        all_paths = f.all_paths()
        self.assertEqual(len(all_paths), 9)
        self.assertEqual(len(set(all_paths)), 9)


class TilesRoundtripTests(unittest.TestCase):
    def _check(self, path: str):
        if not os.path.exists(path):
            self.skipTest(f"ROM missing: {path}")
        ft, raw = _load_ft_rom(path)
        for mid in mapmod.discover_map_ids(ft):
            for suffix in ("a.c", "b.c"):
                fat_path = f"DAT/map/{mid}{suffix}"
                if fat_path not in ft:
                    continue
                with self.subTest(map_id=mid, suffix=suffix):
                    comp = ft.slice(raw, fat_path)
                    decompressed = maybe_decompress(comp)
                    tiles = mapmod.parse_tiles(comp)
                    rebuilt = mapmod.build_tiles(tiles)
                    self.assertEqual(rebuilt, decompressed)

    def test_dusk_us(self):
        self._check(DUSK_US)


class PaletteRoundtripTests(unittest.TestCase):
    def _check(self, path: str):
        if not os.path.exists(path):
            self.skipTest(f"ROM missing: {path}")
        ft, raw = _load_ft_rom(path)
        for mid in mapmod.discover_map_ids(ft):
            for suffix in ("a.p", "b.p"):
                fat_path = f"DAT/map/{mid}{suffix}"
                if fat_path not in ft:
                    continue
                with self.subTest(map_id=mid, suffix=suffix):
                    comp = ft.slice(raw, fat_path)
                    decompressed = maybe_decompress(comp)
                    banks, trailer = mapmod.parse_palette(comp)
                    rebuilt = mapmod.build_palette(banks, trailer)
                    # BGR555 packs cleanly because vanilla colors are
                    # already bit-replicated 5-bit values; encoder uses
                    # high 5 bits of each channel.
                    self.assertEqual(rebuilt, decompressed)

    def test_dusk_us(self):
        self._check(DUSK_US)


class ScreenRoundtripTests(unittest.TestCase):
    def _check(self, path: str):
        if not os.path.exists(path):
            self.skipTest(f"ROM missing: {path}")
        ft, raw = _load_ft_rom(path)
        for mid in mapmod.discover_map_ids(ft):
            for suffix in ("a.s", "b.s"):
                fat_path = f"DAT/map/{mid}{suffix}"
                if fat_path not in ft:
                    continue
                with self.subTest(map_id=mid, suffix=suffix):
                    comp = ft.slice(raw, fat_path)
                    decompressed = maybe_decompress(comp)
                    w, h, entries = mapmod.parse_screen(comp)
                    rebuilt = mapmod.build_screen(w, h, entries)
                    self.assertEqual(rebuilt, decompressed)

    def test_dusk_us(self):
        self._check(DUSK_US)


class WalkabilityRoundtripTests(unittest.TestCase):
    def _check(self, path: str):
        if not os.path.exists(path):
            self.skipTest(f"ROM missing: {path}")
        ft, raw = _load_ft_rom(path)
        for mid in mapmod.discover_map_ids(ft):
            fat_path = f"DAT/map/{mid}.0t"
            if fat_path not in ft:
                continue
            with self.subTest(map_id=mid):
                comp = ft.slice(raw, fat_path)
                decompressed = maybe_decompress(comp)
                w, h, bits = mapmod.parse_walkability(comp)
                rebuilt = mapmod.build_walkability(w, h, bits)
                self.assertEqual(rebuilt, decompressed)

    def test_dusk_us(self):
        self._check(DUSK_US)


class WalkabilityOverlayTests(unittest.TestCase):
    """The overlay must stride bits at the walkability's own width, not
    the composite's. Map 88 (vanilla Dusk) is the canary: 768-px walk
    against a 752-px composite — striding by 752 shears the overlay by
    16 px per row.
    """

    def test_dim_mismatch_strides_by_walk_width(self):
        # 16x4 composite, 24x4 walk: block a single column (x=5) in the walk.
        comp_w, comp_h = 16, 4
        walk_w, walk_h = 24, 4
        composite = mapmod.MapPreview(
            rgba=bytes([0] * comp_w * comp_h * 4),
            width=comp_w, height=comp_h,
        )
        n_bits = walk_w * walk_h
        bits = bytearray((n_bits + 7) // 8)
        for y in range(walk_h):
            ix = y * walk_w + 5
            bits[ix >> 3] |= 1 << (ix & 7)
        tint = (255, 0, 0)
        out = mapmod.apply_walkability_overlay(
            composite, bytes(bits), walk_w, walk_h,
            tint=tint, alpha=255,
        )
        # Every row, only x=5 should be tinted; other columns stay black.
        # Blocked pixels where (x+y)%4==0 render the darker diagonal-hatch
        # red (tint[0]-120) instead of the flat tint.
        hatch_r = max(0, tint[0] - 120)
        for y in range(comp_h):
            for x in range(comp_w):
                off = (y * comp_w + x) * 4
                if x == 5:
                    expected = hatch_r if (x + y) % 4 == 0 else tint[0]
                    self.assertEqual(out.rgba[off], expected, f"({x},{y}) R")
                else:
                    self.assertEqual(out.rgba[off], 0, f"({x},{y}) R")

    def test_dim_match_unchanged_behavior(self):
        # When walk dims == composite dims, every blocked bit tints its pixel.
        w, h = 8, 2
        composite = mapmod.MapPreview(
            rgba=bytes([0] * w * h * 4), width=w, height=h,
        )
        # row 0 = 0xFF → x=0..7 blocked; row 1 = 0x00 → none blocked.
        bits = bytes([0xFF, 0x00])
        tint = (255, 0, 0)
        out = mapmod.apply_walkability_overlay(
            composite, bits, w, h, tint=tint, alpha=255,
        )
        # Blocked pixels where (x+y)%4==0 get the darker diagonal hatch.
        hatch_r = max(0, tint[0] - 120)
        for x in range(w):
            expected = hatch_r if x % 4 == 0 else tint[0]
            self.assertEqual(out.rgba[(0 * w + x) * 4], expected)
            self.assertEqual(out.rgba[(1 * w + x) * 4], 0)


class AttributesRoundtripTests(unittest.TestCase):
    """Opaque-bytes round trip. Body is all-zero in vanilla, but the
    codec doesn't rely on that — it just preserves whatever bytes the
    decompressor produced."""

    def _check(self, path: str):
        if not os.path.exists(path):
            self.skipTest(f"ROM missing: {path}")
        ft, raw = _load_ft_rom(path)
        for mid in mapmod.discover_map_ids(ft):
            fat_path = f"DAT/map/{mid}.a"
            if fat_path not in ft:
                continue
            with self.subTest(map_id=mid):
                comp = ft.slice(raw, fat_path)
                decompressed = maybe_decompress(comp)
                payload = mapmod.parse_attributes(comp)
                rebuilt = mapmod.build_attributes(payload)
                self.assertEqual(rebuilt, decompressed)

    def test_dusk_us(self):
        self._check(DUSK_US)


class DescriptorRoundtripTests(unittest.TestCase):
    """138-byte fixed struct: header + 7 tuple slots + trailer."""

    def _check(self, path: str):
        if not os.path.exists(path):
            self.skipTest(f"ROM missing: {path}")
        ft, raw = _load_ft_rom(path)
        for mid in mapmod.discover_map_ids(ft):
            fat_path = f"DAT/map/{mid}.d"
            with self.subTest(map_id=mid):
                comp = ft.slice(raw, fat_path)
                decompressed = maybe_decompress(comp)
                desc = mapmod.parse_descriptor(comp)
                rebuilt = mapmod.build_descriptor(desc)
                self.assertEqual(rebuilt, decompressed)
                self.assertEqual(len(rebuilt), mapmod.DESCRIPTOR_SIZE)

    def test_dusk_us(self):
        self._check(DUSK_US)


class DescriptorContentTests(unittest.TestCase):
    """Constant-region sanity check on parsed descriptors."""

    def test_header_starts_with_known_prefix(self):
        if not os.path.exists(DUSK_US):
            self.skipTest("Dusk ROM missing")
        ft, raw = _load_ft_rom(DUSK_US)
        # Per recon doc the leading bytes are 01 00 64 00 64 00 ... =
        # (version=1, width=100, height=100). Spot-check on several maps.
        for mid in ("0", "100", "200", "264"):
            with self.subTest(map_id=mid):
                comp = ft.slice(raw, f"DAT/map/{mid}.d")
                desc = mapmod.parse_descriptor(comp)
                self.assertEqual(
                    desc.header_bytes[:6],
                    bytes([0x01, 0x00, 0x64, 0x00, 0x64, 0x00]),
                )

    def test_tuple_kind_in_valid_range(self):
        if not os.path.exists(DUSK_US):
            self.skipTest("Dusk ROM missing")
        ft, raw = _load_ft_rom(DUSK_US)
        # Per recon doc col 0 (kind) is 0..7 across all maps when the
        # tuple is non-empty. Validate that nothing exotic slipped in.
        for mid in mapmod.discover_map_ids(ft):
            comp = ft.slice(raw, f"DAT/map/{mid}.d")
            desc = mapmod.parse_descriptor(comp)
            for tx, tup in enumerate(desc.tuples):
                if tup == mapmod.EMPTY_TUPLE:
                    continue
                kind = tup[0]
                self.assertLessEqual(
                    kind, 7,
                    f"map {mid} tuple {tx}: kind {kind} > 7",
                )


class ProjectRoundtripTests(unittest.TestCase):
    """Phase G: field-map edits must survive a ``.romproj`` save/load.

    Channel mirrors ``btmap_edits``: project save writes per-path bytes
    into ``map_edits`` and asks ``serialize_all`` to skip the field-map
    FAT splice (a grown ``.0t`` or ``.s`` would otherwise shift every
    downstream entry into the byte diff).
    """

    def test_map_edit_survives_project_roundtrip(self):
        if not os.path.exists(DUSK_US):
            self.skipTest("Dusk ROM missing")
        import tempfile
        from editor.session import RomSession
        from editor import project_file

        session = RomSession.from_file(DUSK_US)
        # Walkability `.0t` is small and present on every map — flipping
        # a single byte exercises both the dirty cache and the splice
        # without depending on which layer set the chosen id ships.
        path = mapmod.MapFiles("1").walkability
        original = maybe_decompress(session.map_file_bytes(path))
        new_uncompressed = bytearray(original)
        new_uncompressed[20] ^= 0xFF
        session.replace_map_file_bytes(path, bytes(new_uncompressed))

        with tempfile.TemporaryDirectory() as td:
            proj_path = os.path.join(td, "test.romproj")
            edited = bytes(session.serialize_all(
                skip_sprite_splice=True,
                skip_btmap_splice=True,
                skip_map_splice=True,
            ))
            project_file.save_project(
                proj_path,
                rom_version=session.version,
                vanilla_rom_data=session.original_rom_data,
                edited_rom_data=edited,
                qol=session.qol,
                string_edits=session.msgpak_string_edits(),
                sprite_edits=session.sprite_pak_edits(),
                btchr_appended_sidecars=session.btchr_appended_sidecars(),
                btmap_edits=session.btmap_file_edits(),
                map_edits=session.map_file_edits(),
            )

            loaded = project_file.load_project(proj_path)
            self.assertEqual(
                len(loaded["map_edits"]), 1,
                "map_edits channel didn't carry the edit",
            )
            self.assertEqual(loaded["map_edits"][0][0], path)
            self.assertEqual(
                loaded["map_edits"][0][1], bytes(new_uncompressed),
                "bytes didn't survive base64 round-trip",
            )

            reopened = RomSession.from_file(DUSK_US)
            reopened.apply_map_file_edits(loaded["map_edits"])
            self.assertEqual(
                maybe_decompress(reopened.map_file_bytes(path)),
                bytes(new_uncompressed),
                "reopened session didn't expose edited bytes",
            )

            out = reopened.serialize_all()
            new_ft = fnt.FileTable.from_rom(bytes(out))
            self.assertEqual(
                maybe_decompress(new_ft.slice(bytes(out), path)),
                bytes(new_uncompressed),
                "reopened serialize_all dropped the edit",
            )

    def test_v5_project_loads_with_empty_map_edits(self):
        """Backwards compat: a v5 project (no ``map_edits`` field) still
        loads, exposing the channel as an empty list."""
        import json
        import tempfile
        from editor import project_file

        with tempfile.TemporaryDirectory() as td:
            proj_path = os.path.join(td, "v5.romproj")
            with open(proj_path, "w", encoding="utf-8") as f:
                json.dump({
                    "format_version": 5,
                    "editor_version": "0.0.1",
                    "rom_version": "dusk_us",
                    "vanilla_rom_sha256": "0" * 64,
                    "qol": {},
                    "diffs": [],
                    "string_edits": [],
                    "sprite_edits": [],
                    "btchr_appended_sidecars": [],
                    "btmap_edits": [],
                }, f)
            loaded = project_file.load_project(proj_path)
            self.assertEqual(loaded["map_edits"], [])


class RenderParityTests(unittest.TestCase):
    """Browser preview vs. standalone map_render.py PNG. Byte-exact."""

    def test_renders_exactly(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow not available")
        if not os.path.exists(DUSK_US):
            self.skipTest("Dusk ROM missing")
        if not os.path.isdir(PREVIEWS_DIR):
            self.skipTest("standalone PNG previews not generated")
        ft, raw = _load_ft_rom(DUSK_US)
        checked = 0
        for mid in mapmod.discover_map_ids(ft):
            png = os.path.join(PREVIEWS_DIR, f"{mid}.png")
            if not os.path.exists(png):
                continue
            ref = Image.open(png).convert("RGBA").tobytes()
            preview = mapmod.render_map_from_file_table(mid, ft, raw)
            self.assertEqual(
                len(ref), len(preview.rgba),
                f"size mismatch at map {mid}",
            )
            self.assertEqual(
                ref, preview.rgba,
                f"render mismatch at map {mid}",
            )
            checked += 1
        self.assertGreater(checked, 0, "no previews compared")


if __name__ == "__main__":
    unittest.main()
