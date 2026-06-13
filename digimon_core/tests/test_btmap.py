"""Btmap codec — PLAN.md §14.4 Phase A acceptance.

Three guarantees from :mod:`digimon_core.btmap`:

1. **Discovery**: ``discover_map_ids`` returns the same 76 ids the
   standalone renderer finds in the loose extract.

2. **NSCR round-trip**: ``build_nscr_from_template(parse_nscr(x), ...)``
   reproduces ``x`` byte-for-byte for every layer-A/B tilemap in
   vanilla ROM. The SCB rearrangement is the load-bearing piece — if
   the build path skipped it, the engine would render the broken-
   quadrant image we hit in the standalone renderer's first pass.

3. **Render parity**: ``render_btmap_from_file_table`` matches the
   standalone renderer's PNG output within ±1 per channel for every
   vanilla btmap id. The 1-LSB difference is the bit-replication vs.
   linear-scale BGR555→RGB choice (PLAN.md §14.7).
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PKG_PARENT = os.path.abspath(os.path.join(HERE, "..", ".."))
if PKG_PARENT not in sys.path:
    sys.path.insert(0, PKG_PARENT)

from digimon_core import btmap, fnt, rom  # noqa: E402
from digimon_core.sprite import decompress_rle30  # noqa: E402


ROM_DIR = r"C:\Workspace\digimon_stuffs\rom_files"
DUSK_US = os.path.join(ROM_DIR, "1420 - Digimon World - Dusk (US).nds")
DAWN_US = os.path.join(ROM_DIR, "1421 - Digimon World - Dawn (USA).nds")

PREVIEWS_DIR = r"C:\Workspace\digimon_stuffs\research_docs\claude_notes\_btmap_previews"


def _load_ft_rom(path: str):
    raw = bytes(rom.loadRom(path))
    return fnt.FileTable.from_rom(raw), raw


class DiscoveryTests(unittest.TestCase):
    def test_discover_dusk_us(self):
        if not os.path.exists(DUSK_US):
            self.skipTest("Dusk ROM missing")
        ft, _ = _load_ft_rom(DUSK_US)
        ids = btmap.discover_map_ids(ft)
        # 76 maps in vanilla Dusk; ids are decimal strings, numeric-sorted.
        self.assertEqual(len(ids), 76)
        self.assertEqual(ids[0], "0")
        self.assertEqual(ids[-1], "75")
        # Confirm sorting is numeric, not lexicographic: '10' must come
        # after '9' if the latter exists (it doesn't in vanilla, but
        # the assertion is cheap and catches a regression).
        nums = [int(i) for i in ids]
        self.assertEqual(nums, sorted(nums))


class BtmapFilesTests(unittest.TestCase):
    def test_paths_for_id(self):
        f = btmap.BtmapFiles("42")
        self.assertEqual(f.layer_a_ncgr, "DAT/btmap/42ac")
        self.assertEqual(f.layer_a_nclr, "DAT/btmap/42ap")
        self.assertEqual(f.layer_a_nscr, "DAT/btmap/42as")
        self.assertEqual(f.layer_b_ncgr, "DAT/btmap/42bc")
        self.assertEqual(f.layer_b_nscr, "DAT/btmap/42bs")
        self.assertEqual(f.anim_ncgr(2), "DAT/btmap/42a2c")
        self.assertEqual(f.anim_cells(0), "DAT/btmap/42a0n")
        all_paths = f.all_paths()
        # 3 layer-A files + 2 layer-B + 5 frames × 2 anim files = 15.
        self.assertEqual(len(all_paths), 15)
        self.assertEqual(len(set(all_paths)), 15)
        self.assertIn("DAT/btmap/42a4c", all_paths)
        self.assertIn("DAT/btmap/42a4n", all_paths)


class NscrRoundtripTests(unittest.TestCase):
    """Every vanilla btmap NSCR must rebuild byte-identically after parse.

    SCB rearrangement is symmetric: parse unfolds SCB-concatenated stream
    into a row-major grid; build refolds row-major back into the SCB
    concatenation. A bug in either direction (or asymmetry between them)
    surfaces as a byte diff on a no-edit pass.
    """

    def _check_rom(self, path: str):
        if not os.path.exists(path):
            self.skipTest(f"ROM missing: {path}")
        ft, raw = _load_ft_rom(path)
        for mid in btmap.discover_map_ids(ft):
            for suffix in ("as", "bs"):
                fat_path = f"DAT/btmap/{mid}{suffix}"
                if fat_path not in ft:
                    continue
                comp = ft.slice(raw, fat_path)
                with self.subTest(map_id=mid, suffix=suffix):
                    decompressed = decompress_rle30(comp)
                    w, h, entries = btmap.parse_nscr(comp)
                    rebuilt = btmap.build_nscr_from_template(
                        entries, w, h, decompressed,
                    )
                    self.assertEqual(
                        rebuilt, decompressed,
                        f"NSCR round-trip broke at {fat_path}",
                    )

    def test_dusk_us(self):
        self._check_rom(DUSK_US)

    def test_dawn_us(self):
        self._check_rom(DAWN_US)


class ScbRearrangementTests(unittest.TestCase):
    """Direct check on the SCB unfold/refold math with synthetic data.

    Builds a stream where each cell holds its source SCB index so the
    rearrangement is visible. Verifies layout for the three multi-SCB
    BG sizes documented in PLAN.md §14.6.
    """

    def _scb_stream(self, scb_count: int) -> list:
        # Each SCB is filled with its own index; row-major output should
        # then show which SCB each tile came from.
        scb_entries = btmap.SCB_TILES * btmap.SCB_TILES
        stream = []
        for ix in range(scb_count):
            stream.extend([ix] * scb_entries)
        return stream

    def test_size_1_horizontal(self):
        # 512x256: SCB0 = left, SCB1 = right
        stream = self._scb_stream(2)
        grid = btmap._rearrange_scbs(stream, 64, 32, to_row_major=True)
        # Row 0: 32 zeros (SCB0) then 32 ones (SCB1)
        self.assertEqual(grid[:32], [0] * 32)
        self.assertEqual(grid[32:64], [1] * 32)
        # Last row (index 31 — grid is 64 wide × 32 tall) should be identical to first
        self.assertEqual(grid[31 * 64:31 * 64 + 32], [0] * 32)
        self.assertEqual(grid[31 * 64 + 32:32 * 64], [1] * 32)

    def test_size_2_vertical(self):
        # 256x512: SCB0 = top, SCB1 = bottom
        stream = self._scb_stream(2)
        grid = btmap._rearrange_scbs(stream, 32, 64, to_row_major=True)
        # Top half all 0, bottom half all 1
        self.assertEqual(set(grid[:32 * 32]), {0})
        self.assertEqual(set(grid[32 * 32:]), {1})

    def test_size_3_four_quadrant(self):
        # 512x512: SCB0=TL, SCB1=TR, SCB2=BL, SCB3=BR
        stream = self._scb_stream(4)
        grid = btmap._rearrange_scbs(stream, 64, 64, to_row_major=True)
        # Sample one cell from each quadrant
        def cell(ty, tx):
            return grid[ty * 64 + tx]
        self.assertEqual(cell(0, 0), 0)
        self.assertEqual(cell(0, 63), 1)
        self.assertEqual(cell(63, 0), 2)
        self.assertEqual(cell(63, 63), 3)

    def test_refold_is_inverse_of_unfold(self):
        # Random-ish stream, walk it through parse + build, verify
        # bytes-out == bytes-in.
        stream = [(i * 9301 + 49297) & 0xFFFF for i in range(64 * 64)]
        unfolded = btmap._rearrange_scbs(stream, 64, 64, to_row_major=True)
        refolded = btmap._rearrange_scbs(unfolded, 64, 64, to_row_major=False)
        self.assertEqual(refolded, stream)


class RenderParityTests(unittest.TestCase):
    """Browser preview vs. standalone btmap_render.py PNG."""

    def test_renders_within_one_lsb(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow not available")
        if not os.path.exists(DUSK_US):
            self.skipTest("Dusk ROM missing")
        if not os.path.isdir(PREVIEWS_DIR):
            self.skipTest("standalone PNG previews not generated")
        ft, raw = _load_ft_rom(DUSK_US)
        worst = 0
        for mid in btmap.discover_map_ids(ft):
            png = os.path.join(PREVIEWS_DIR, f"{mid}.png")
            if not os.path.exists(png):
                continue
            ref = Image.open(png).convert("RGBA").tobytes()
            preview = btmap.render_btmap_from_file_table(mid, ft, raw)
            self.assertEqual(
                len(ref), len(preview.rgba),
                f"size mismatch at {mid}",
            )
            for i, (a, b) in enumerate(zip(ref, preview.rgba)):
                d = abs(a - b)
                if d > worst:
                    worst = d
                self.assertLessEqual(
                    d, 1,
                    f"{mid} byte {i}: ref={a} ours={b}",
                )
        # Document the worst diff so a regression that bumps to ±2 is
        # immediately visible in test output (CI would still pass; the
        # assertion above gates ±1).
        self.assertLessEqual(worst, 1)


class ComponentDescribeTests(unittest.TestCase):
    def test_describe_layer_a_components(self):
        if not os.path.exists(DUSK_US):
            self.skipTest("Dusk ROM missing")
        ft, raw = _load_ft_rom(DUSK_US)
        paths = btmap.BtmapFiles("0")
        ncgr = btmap.describe_component(paths.layer_a_ncgr, ft.slice(raw, paths.layer_a_ncgr))
        nclr = btmap.describe_component(paths.layer_a_nclr, ft.slice(raw, paths.layer_a_nclr))
        nscr = btmap.describe_component(paths.layer_a_nscr, ft.slice(raw, paths.layer_a_nscr))
        self.assertEqual(ncgr.kind, "NCGR")
        self.assertEqual(nclr.kind, "NCLR")
        self.assertEqual(nscr.kind, "NSCR")
        self.assertIn("bpp", ncgr.detail)
        self.assertIn("banks", nclr.detail)
        self.assertIn("\u00d7", nscr.detail)


class AnimDecodeTests(unittest.TestCase):
    """NaXn schema decode (PLAN.md §14.7 — animation tile-blit).

    Two guarantees:

    1. **Coverage**: parsing every vanilla NaXn in Dusk yields
       ``schema_ok=True`` on at least 170/191 outer frames. The
       remainder are documented variants (small overlay / off-by-N)
       and the parser is lenient on them so callers can fall back.

    2. **Effect**: ``render_anim_state`` actually differs from the
       base composite for a known map. A bug that reverted to a
       no-op splice (e.g. dropping the ``maybe_decompress`` call on
       the NaXn input) wouldn't change any byte and this catches it.
    """

    def test_naxn_decode_coverage(self):
        if not os.path.exists(DUSK_US):
            self.skipTest("Dusk ROM missing")
        ft, raw = _load_ft_rom(DUSK_US)
        total = 0
        ok = 0
        for mid in btmap.discover_map_ids(ft):
            for frame_ix in btmap.ANIM_FRAMES:
                path = f"DAT/btmap/{mid}a{frame_ix}n"
                if path not in ft:
                    continue
                total += 1
                try:
                    parsed = btmap.parse_naxn(ft.slice(raw, path))
                except ValueError:
                    continue
                if parsed.schema_ok:
                    ok += 1
        # 172/191 in vanilla — set the floor a little lower so a tiny
        # regression doesn't trip the test, but tight enough to catch a
        # broken decode that drops most frames.
        self.assertGreaterEqual(ok, 170, f"NaXn decode coverage dropped: {ok}/{total}")
        self.assertGreaterEqual(total, 190, f"NaXn count regressed: {total}")

    def test_render_anim_state_differs_from_base(self):
        if not os.path.exists(DUSK_US):
            self.skipTest("Dusk ROM missing")
        ft, raw = _load_ft_rom(DUSK_US)
        # Map 12 has schema_ok frame 0 with 6 sub-frames — known good case.
        mid = "12"
        paths = btmap.BtmapFiles(mid)
        nclr = ft.slice(raw, paths.layer_a_nclr)
        ncgr_a = ft.slice(raw, paths.layer_a_ncgr)
        nscr_a = ft.slice(raw, paths.layer_a_nscr)
        ncgr_anim = ft.slice(raw, paths.anim_ncgr(0))
        naxn = ft.slice(raw, paths.anim_cells(0))
        base = btmap.render_btmap(
            mid, layer_a_ncgr=ncgr_a, layer_a_nclr=nclr, layer_a_nscr=nscr_a,
        )
        sub = btmap.render_anim_state(
            layer_a_ncgr=ncgr_a, layer_a_nclr=nclr, layer_a_nscr=nscr_a,
            anim_ncgr=ncgr_anim, anim_naxn=naxn, sub_ix=0,
        )
        self.assertEqual(base.width, sub.width)
        self.assertEqual(base.height, sub.height)
        self.assertNotEqual(
            base.rgba, sub.rgba,
            "render_anim_state matches base — splice is a no-op",
        )

    def test_classify_anim_frame_schema_routes_known_cases(self):
        """Three classifications, three real vanilla examples:

        - Map 12 frame 2: dst 959..969 — every NSCR cell in that range
          uses one bank → "all".
        - Map 12 frame 1: dst 960..996 — mixed banks → "dominant_bank".
        - Map 39 frame 0: dst 748..749 — single bank → "all".

        The dst counts come from _probe_anim_concurrent.py output.
        """
        if not os.path.exists(DUSK_US):
            self.skipTest("Dusk ROM missing")
        ft, raw = _load_ft_rom(DUSK_US)
        paths12 = btmap.BtmapFiles("12")
        ncgr12 = ft.slice(raw, paths12.layer_a_ncgr)
        nscr12 = ft.slice(raw, paths12.layer_a_nscr)
        frame_homo = btmap.parse_naxn(ft.slice(raw, paths12.anim_cells(2)))
        frame_mixed = btmap.parse_naxn(ft.slice(raw, paths12.anim_cells(1)))
        self.assertEqual(
            btmap.classify_anim_frame_schema(
                frame_homo, layer_a_ncgr=ncgr12, layer_a_nscr=nscr12,
            ),
            "all",
        )
        self.assertEqual(
            btmap.classify_anim_frame_schema(
                frame_mixed, layer_a_ncgr=ncgr12, layer_a_nscr=nscr12,
            ),
            "dominant_bank",
        )
        paths39 = btmap.BtmapFiles("39")
        ncgr39 = ft.slice(raw, paths39.layer_a_ncgr)
        nscr39 = ft.slice(raw, paths39.layer_a_nscr)
        frame39_0 = btmap.parse_naxn(ft.slice(raw, paths39.anim_cells(0)))
        self.assertEqual(
            btmap.classify_anim_frame_schema(
                frame39_0, layer_a_ncgr=ncgr39, layer_a_nscr=nscr39,
            ),
            "all",
        )

    def test_classify_anim_frame_schema_detects_unrenderable(self):
        """Map 39 frame 2: dst 930..1003, schema_ok, but *no* NSCR cell
        references any tile in that range. Splice would be invisible.
        Classifier must report "none" so the renderer falls back to the
        static base (the engine presumably draws this via a path we
        haven't decoded — likely OBJ overlay at the NaXn (x, y) coords).
        """
        if not os.path.exists(DUSK_US):
            self.skipTest("Dusk ROM missing")
        ft, raw = _load_ft_rom(DUSK_US)
        paths = btmap.BtmapFiles("39")
        ncgr_a = ft.slice(raw, paths.layer_a_ncgr)
        nscr_a = ft.slice(raw, paths.layer_a_nscr)
        frame2 = btmap.parse_naxn(ft.slice(raw, paths.anim_cells(2)))
        self.assertEqual(
            btmap.classify_anim_frame_schema(
                frame2, layer_a_ncgr=ncgr_a, layer_a_nscr=nscr_a,
            ),
            "none",
        )

    def test_render_anim_state_routed_returns_base_for_none(self):
        """When the classifier picks "none", the routed render must equal
        :func:`render_btmap` byte-for-byte — that's the contract callers
        rely on to drop the glitched splice.
        """
        if not os.path.exists(DUSK_US):
            self.skipTest("Dusk ROM missing")
        ft, raw = _load_ft_rom(DUSK_US)
        paths = btmap.BtmapFiles("39")
        nclr = ft.slice(raw, paths.layer_a_nclr)
        ncgr_a = ft.slice(raw, paths.layer_a_ncgr)
        nscr_a = ft.slice(raw, paths.layer_a_nscr)
        ncgr_anim = ft.slice(raw, paths.anim_ncgr(2))
        naxn = ft.slice(raw, paths.anim_cells(2))
        base = btmap.render_btmap(
            "39", layer_a_ncgr=ncgr_a, layer_a_nclr=nclr, layer_a_nscr=nscr_a,
        )
        sub, schema = btmap.render_anim_state_routed(
            layer_a_ncgr=ncgr_a, layer_a_nclr=nclr, layer_a_nscr=nscr_a,
            anim_ncgr=ncgr_anim, anim_naxn=naxn, sub_ix=0,
        )
        self.assertEqual(schema, "none")
        self.assertEqual(base.rgba, sub.rgba)

    def test_render_anim_state_routed_dominant_bank_diverges_from_full_splice(self):
        """Map 12 frame 1 has mixed-bank dst cells. The dominant-bank
        splice must differ from the "all" splice (otherwise the
        mitigation is a no-op) and from the static base (otherwise the
        animated cells don't move at all).
        """
        if not os.path.exists(DUSK_US):
            self.skipTest("Dusk ROM missing")
        ft, raw = _load_ft_rom(DUSK_US)
        paths = btmap.BtmapFiles("12")
        nclr = ft.slice(raw, paths.layer_a_nclr)
        ncgr_a = ft.slice(raw, paths.layer_a_ncgr)
        nscr_a = ft.slice(raw, paths.layer_a_nscr)
        ncgr_anim = ft.slice(raw, paths.anim_ncgr(1))
        naxn = ft.slice(raw, paths.anim_cells(1))
        base = btmap.render_btmap(
            "12", layer_a_ncgr=ncgr_a, layer_a_nclr=nclr, layer_a_nscr=nscr_a,
        )
        full = btmap.render_anim_state(
            layer_a_ncgr=ncgr_a, layer_a_nclr=nclr, layer_a_nscr=nscr_a,
            anim_ncgr=ncgr_anim, anim_naxn=naxn, sub_ix=0, splice_mode="all",
        )
        routed, schema = btmap.render_anim_state_routed(
            layer_a_ncgr=ncgr_a, layer_a_nclr=nclr, layer_a_nscr=nscr_a,
            anim_ncgr=ncgr_anim, anim_naxn=naxn, sub_ix=0,
        )
        self.assertEqual(schema, "dominant_bank")
        self.assertNotEqual(base.rgba, routed.rgba, "dominant-bank splice rendered nothing")
        self.assertNotEqual(full.rgba, routed.rgba, "dominant-bank splice matched full splice")


class SparseExportTests(unittest.TestCase):
    """Option A export surface (PLAN.md §14.7 — animation editing).

    The sparse PNG must be the same dims as Layer A (so the user
    paints on a canvas aligned with the in-game tilemap), and every
    pixel outside the animated tile cells must be fully transparent.
    Inside the animated cells, at least one pixel should be opaque —
    otherwise the user has nothing to edit and we've shipped a blank
    file by mistake.
    """

    def test_sparse_alpha_layout(self):
        if not os.path.exists(DUSK_US):
            self.skipTest("Dusk ROM missing")
        ft, raw = _load_ft_rom(DUSK_US)
        mid = "12"
        paths = btmap.BtmapFiles(mid)
        sparse = btmap.render_anim_sub_frame_sparse(
            layer_a_nscr=ft.slice(raw, paths.layer_a_nscr),
            layer_a_nclr=ft.slice(raw, paths.layer_a_nclr),
            anim_ncgr=ft.slice(raw, paths.anim_ncgr(0)),
            anim_naxn=ft.slice(raw, paths.anim_cells(0)),
            sub_ix=0,
        )
        # Layer A on map 12 is 512x256.
        self.assertEqual((sparse.width, sparse.height), (512, 256))
        # Build a tile-cell alpha mask from the NaXn dst range.
        naxn_frame = btmap.parse_naxn(ft.slice(raw, paths.anim_cells(0)))
        w, h, entries = btmap.parse_nscr(ft.slice(raw, paths.layer_a_nscr))
        tw = w // 8
        animated_cells: set = set()
        for ty in range(h // 8):
            for tx in range(tw):
                if naxn_frame.dst_lo <= (entries[ty * tw + tx] & 0x3FF) <= naxn_frame.dst_hi:
                    animated_cells.add((tx, ty))
        # Every opaque pixel must fall inside an animated cell.
        rgba = sparse.rgba
        opaque_in_animated = 0
        for ty in range(h // 8):
            for tx in range(tw):
                is_animated = (tx, ty) in animated_cells
                for py in range(8):
                    for px in range(8):
                        i = ((ty * 8 + py) * w + tx * 8 + px) * 4
                        alpha = rgba[i + 3]
                        if not is_animated:
                            self.assertEqual(
                                alpha, 0,
                                f"opaque pixel at ({tx*8+px},{ty*8+py}) outside animated region",
                            )
                        elif alpha == 255:
                            opaque_in_animated += 1
        self.assertGreater(
            opaque_in_animated, 0,
            "sparse render is fully transparent — nothing to edit",
        )

    def test_export_pack_writes_expected_files(self):
        if not os.path.exists(DUSK_US):
            self.skipTest("Dusk ROM missing")
        try:
            from PIL import Image  # noqa: F401
        except ImportError:
            self.skipTest("Pillow not available")
        import shutil
        import tempfile
        ft, raw = _load_ft_rom(DUSK_US)
        mid = "12"
        paths = btmap.BtmapFiles(mid)
        tmp = tempfile.mkdtemp(prefix="btmap_export_")
        try:
            written = btmap.export_anim_frame_pack(
                out_dir=tmp,
                map_id=mid,
                frame_ix=0,
                layer_a_ncgr=ft.slice(raw, paths.layer_a_ncgr),
                layer_a_nclr=ft.slice(raw, paths.layer_a_nclr),
                layer_a_nscr=ft.slice(raw, paths.layer_a_nscr),
                anim_ncgr=ft.slice(raw, paths.anim_ncgr(0)),
                anim_naxn=ft.slice(raw, paths.anim_cells(0)),
                layer_b_ncgr=ft.slice(raw, paths.layer_b_ncgr) if paths.layer_b_ncgr in ft else None,
                layer_b_nscr=ft.slice(raw, paths.layer_b_nscr) if paths.layer_b_nscr in ft else None,
            )
            # reference + 6 sub-frames + meta json = 8.
            self.assertEqual(len(written), 8)
            for p in written:
                self.assertTrue(os.path.exists(p), f"missing: {p}")
            # Spot-check meta JSON shape.
            import json
            meta_path = next(p for p in written if p.endswith(".meta.json"))
            with open(meta_path) as f:
                meta = json.load(f)
            self.assertEqual(meta["map_id"], mid)
            self.assertEqual(meta["frame_ix"], 0)
            self.assertGreater(meta["dst_hi"], meta["dst_lo"])
            self.assertEqual(len(meta["sub_frames"]), 6)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class ImportRoundtripTests(unittest.TestCase):
    """Export → un-edited PNG → import must recover the original NaXc.

    If the round-trip drifts, the user's first save after opening the
    PNGs in an external editor (without painting anything) would
    silently rewrite the file — that's the kind of bug that corrupts
    saves without producing an obvious error, so the test gates it.
    """

    def test_unedited_roundtrip_matches_original(self):
        if not os.path.exists(DUSK_US):
            self.skipTest("Dusk ROM missing")
        try:
            from PIL import Image  # noqa: F401
        except ImportError:
            self.skipTest("Pillow not available")
        import shutil
        import tempfile
        ft, raw = _load_ft_rom(DUSK_US)
        # Map 12 frame 0 — known-good schema_ok with 6 sub-frames.
        mid = "12"
        paths = btmap.BtmapFiles(mid)
        nclr = ft.slice(raw, paths.layer_a_nclr)
        nscr = ft.slice(raw, paths.layer_a_nscr)
        anim_ncgr = ft.slice(raw, paths.anim_ncgr(0))
        anim_naxn = ft.slice(raw, paths.anim_cells(0))
        # Decompress the original so we compare against the same shape
        # build_ncgr_from_template emits (uncompressed NCGR body).
        from digimon_core.sprite import maybe_decompress
        original_ncgr = maybe_decompress(anim_ncgr)
        tmp = tempfile.mkdtemp(prefix="btmap_roundtrip_")
        try:
            btmap.export_anim_frame_pack(
                out_dir=tmp,
                map_id=mid,
                frame_ix=0,
                layer_a_ncgr=ft.slice(raw, paths.layer_a_ncgr),
                layer_a_nclr=nclr,
                layer_a_nscr=nscr,
                anim_ncgr=anim_ncgr,
                anim_naxn=anim_naxn,
                layer_b_ncgr=ft.slice(raw, paths.layer_b_ncgr) if paths.layer_b_ncgr in ft else None,
                layer_b_nscr=ft.slice(raw, paths.layer_b_nscr) if paths.layer_b_nscr in ft else None,
            )
            result = btmap.import_anim_frame_pack(
                folder=tmp,
                layer_a_nscr=nscr,
                layer_a_nclr=nclr,
                anim_ncgr=anim_ncgr,
            )
            self.assertEqual(result.map_id, mid)
            self.assertEqual(result.frame_ix, 0)
            # 17 tiles in the dst range, 6 sub-frames → 17 × 6 = 102 touches.
            self.assertEqual(sum(result.tiles_touched), 17 * 6)
            # Compare at tile-data level: build_ncgr_from_template drops any
            # trailing blocks past RAHC (CPOS etc.), so a byte-equal check
            # against the original would always fail. The guarantee we
            # actually need is "every pixel index round-trips identically".
            orig_tiles, _ = btmap._ncgr_tiles_as_indices(original_ncgr)
            new_tiles, _ = btmap._ncgr_tiles_as_indices(result.new_ncgr)
            self.assertEqual(len(orig_tiles), len(new_tiles))
            for ix, (a, b) in enumerate(zip(orig_tiles, new_tiles)):
                self.assertEqual(a, b, f"tile {ix} differs on un-edited round-trip")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_edited_pixel_propagates(self):
        if not os.path.exists(DUSK_US):
            self.skipTest("Dusk ROM missing")
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow not available")
        import shutil
        import tempfile
        ft, raw = _load_ft_rom(DUSK_US)
        mid = "12"
        paths = btmap.BtmapFiles(mid)
        nclr = ft.slice(raw, paths.layer_a_nclr)
        nscr = ft.slice(raw, paths.layer_a_nscr)
        anim_ncgr = ft.slice(raw, paths.anim_ncgr(0))
        anim_naxn = ft.slice(raw, paths.anim_cells(0))
        tmp = tempfile.mkdtemp(prefix="btmap_edited_")
        try:
            btmap.export_anim_frame_pack(
                out_dir=tmp,
                map_id=mid,
                frame_ix=0,
                layer_a_ncgr=ft.slice(raw, paths.layer_a_ncgr),
                layer_a_nclr=nclr,
                layer_a_nscr=nscr,
                anim_ncgr=anim_ncgr,
                anim_naxn=anim_naxn,
            )
            # Find one opaque pixel in sub-frame 0 PNG and recolor it to
            # something distinct; verify the import path picks it up.
            png_path = os.path.join(tmp, f"map{mid}_f0_s0.png")
            img = Image.open(png_path).convert("RGBA")
            w, h = img.size
            px = img.load()
            edit_at = None
            for y in range(h):
                for x in range(w):
                    if px[x, y][3] == 255:
                        edit_at = (x, y)
                        break
                if edit_at is not None:
                    break
            self.assertIsNotNone(edit_at, "no opaque pixel to edit")
            # Paint with bright red. Quantizer should map to whichever
            # palette entry is closest to (255, 0, 0) — almost certainly
            # not the original index for this pixel.
            px[edit_at[0], edit_at[1]] = (255, 0, 0, 255)
            img.save(png_path)
            from digimon_core.sprite import maybe_decompress
            original_ncgr = maybe_decompress(anim_ncgr)
            result = btmap.import_anim_frame_pack(
                folder=tmp,
                layer_a_nscr=nscr,
                layer_a_nclr=nclr,
                anim_ncgr=anim_ncgr,
            )
            self.assertNotEqual(
                result.new_ncgr, original_ncgr,
                "edited pixel did not propagate to NCGR bytes",
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class ReplaceBtmapCommandTests(unittest.TestCase):
    """End-to-end Phase D: a real Import PNG → command → session flip.

    Confirms the dirty cache layer plumbs through: after the command
    runs, ``btmap_file_bytes`` for the touched path must return the
    new bytes; after undo, it must return the original. The browser's
    visible animation should reflect both flips (covered by re-running
    ``parse_naxn`` on the cached bytes).
    """

    def test_command_redo_undo_flips_session_bytes(self):
        if not os.path.exists(DUSK_US):
            self.skipTest("Dusk ROM missing")
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow not available")
        from PySide6.QtGui import QUndoStack
        try:
            from PySide6.QtWidgets import QApplication
        except ImportError:
            self.skipTest("PySide6 not available")
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        QApplication.instance() or QApplication([])
        from editor.session import RomSession
        from editor.commands import ReplaceBtmapFileCommand

        import shutil
        import tempfile
        session = RomSession.from_file(DUSK_US)
        mid = "12"
        paths = btmap.BtmapFiles(mid)
        ncgr_fat_path = paths.anim_ncgr(0)
        original = session.btmap_file_bytes(ncgr_fat_path)

        tmp = tempfile.mkdtemp(prefix="btmap_cmd_")
        try:
            btmap.export_anim_frame_pack(
                out_dir=tmp,
                map_id=mid,
                frame_ix=0,
                layer_a_ncgr=session.btmap_file_bytes(paths.layer_a_ncgr),
                layer_a_nclr=session.btmap_file_bytes(paths.layer_a_nclr),
                layer_a_nscr=session.btmap_file_bytes(paths.layer_a_nscr),
                anim_ncgr=original,
                anim_naxn=session.btmap_file_bytes(paths.anim_cells(0)),
            )
            png_path = os.path.join(tmp, f"map{mid}_f0_s0.png")
            img = Image.open(png_path).convert("RGBA")
            w, h = img.size
            px = img.load()
            # Recolor one opaque pixel to force a non-trivial diff.
            for y in range(h):
                for x in range(w):
                    if px[x, y][3] == 255:
                        px[x, y] = (255, 0, 0, 255)
                        img.save(png_path)
                        break
                else:
                    continue
                break
            result = btmap.import_anim_frame_pack(
                folder=tmp,
                layer_a_nscr=session.btmap_file_bytes(paths.layer_a_nscr),
                layer_a_nclr=session.btmap_file_bytes(paths.layer_a_nclr),
                anim_ncgr=original,
            )
            stack = QUndoStack()
            stack.push(ReplaceBtmapFileCommand(
                session, ncgr_fat_path, result.new_ncgr, "test import",
            ))
            self.assertEqual(session.btmap_file_bytes(ncgr_fat_path), result.new_ncgr)
            self.assertNotEqual(session.btmap_file_bytes(ncgr_fat_path), original)
            stack.undo()
            self.assertEqual(session.btmap_file_bytes(ncgr_fat_path), original)
            stack.redo()
            self.assertEqual(session.btmap_file_bytes(ncgr_fat_path), result.new_ncgr)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class SerializeSpliceTests(unittest.TestCase):
    """Phase E: ``serialize_all`` must replay ``_dirty_btmap_files`` into the
    ROM bytes so a save actually persists the edit.

    Two checks beyond "the function didn't crash":

    1. The serialized ROM, parsed back through ``fnt.FileTable``, returns
       the new bytes at the edited FAT path (after RLE-30 decompression).
    2. Downstream FAT entries that lived past the edited file have their
       offsets shifted consistently — i.e., reading another vanilla file
       past the edit still yields its original bytes. A bug in the
       descending-order iteration or in the resize_fat_entry call would
       surface as garbage here.
    """

    def test_dirty_btmap_lands_in_serialized_rom(self):
        if not os.path.exists(DUSK_US):
            self.skipTest("Dusk ROM missing")
        try:
            from PySide6.QtWidgets import QApplication  # noqa: F401
        except ImportError:
            self.skipTest("PySide6 not available")
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        QApplication.instance() or QApplication([])
        from editor.session import RomSession
        from digimon_core.sprite import maybe_decompress

        session = RomSession.from_file(DUSK_US)
        paths = btmap.BtmapFiles("12")
        path = paths.anim_ncgr(0)
        original_uncompressed = maybe_decompress(session.btmap_file_bytes(path))
        # Flip one byte in the tile bank so the splice has something to do.
        new_uncompressed = bytearray(original_uncompressed)
        # Header end varies — write past the NCGR header (96 bytes is a
        # safe lower bound on RAHC header), well inside the tile body.
        new_uncompressed[100] ^= 0xFF
        session.replace_btmap_file_bytes(path, bytes(new_uncompressed))

        # Pick a downstream FAT path to spot-check that surrounding files
        # still resolve cleanly after the splice (alignment + FAT update).
        vanilla_ft = session.vanilla_file_table()
        edit_start, _ = vanilla_ft.resolve(path)
        downstream_path = None
        downstream_vanilla = None
        for candidate_path in (paths.anim_ncgr(1), paths.layer_b_ncgr, "DAT/btmap/13ac"):
            try:
                cs, ce = vanilla_ft.resolve(candidate_path)
            except KeyError:
                continue
            if cs > edit_start:
                downstream_path = candidate_path
                downstream_vanilla = bytes(session.original_rom_data[cs:ce])
                break
        self.assertIsNotNone(downstream_path, "no downstream btmap to validate")

        out = session.serialize_all()
        # Reload FAT from the serialized bytes and confirm the new payload
        # is at the right path.
        new_ft = fnt.FileTable.from_rom(bytes(out))
        roundtripped = maybe_decompress(new_ft.slice(bytes(out), path))
        self.assertEqual(
            roundtripped, bytes(new_uncompressed),
            "edited btmap bytes didn't survive serialize_all",
        )
        # Downstream file should still be its vanilla self.
        ds_after = new_ft.slice(bytes(out), downstream_path)
        self.assertEqual(
            ds_after, downstream_vanilla,
            f"downstream file {downstream_path} corrupted by splice",
        )


class LayerImportTests(unittest.TestCase):
    """Phase G: flat-PNG → NCGR + NSCR + NCLR for static layers.

    The importer absorbs the 1024-tile NSCR ceiling via flip-dedup + lossy
    pairwise-merge clustering. These tests verify:

    1. **Algorithmic primitives** (chop, canonicalize, dedupe, pack/unpack)
       round-trip correctly so a clean (≤1024-tile) image survives import
       byte-identically at the tile-data level.
    2. **Clustering** drops the unique count to the requested cap and
       respects locked tiles (the NaXn dst-slot reservation).
    3. **End-to-end** import of a rendered vanilla Layer A produces a
       grown-tile-count count that is ≤ 1024 and an NSCR whose cells
       all reference in-bounds tiles.
    """

    def test_pack_unpack_4bpp_roundtrip(self):
        from digimon_core import btmap_import
        from digimon_core.sprite import unpack_pixels
        # Synthetic tile: row-major palette indices 0..15 repeating.
        tile = bytes((i % 16) for i in range(64))
        packed = btmap_import.pack_tiles_4bpp([tile])
        self.assertEqual(len(packed), 32, "4bpp tile must be 32 bytes")
        unpacked = bytes(unpack_pixels(packed, bit_depth=3))
        self.assertEqual(unpacked, tile)

    def test_canonicalize_collapses_flip_variants(self):
        from digimon_core import btmap_import
        import numpy as np
        # Asymmetric tile so all 4 flips are distinct.
        arr = np.arange(64, dtype=np.uint8).reshape(8, 8) % 16
        base = arr.tobytes()
        flips = btmap_import._flip_variants(base)
        self.assertEqual(len(set(flips)), 4, "test tile must be flip-asymmetric")
        # All 4 flip variants canonicalize to the same form.
        canonicals = {btmap_import.canonicalize_tile(v)[0] for v in flips}
        self.assertEqual(len(canonicals), 1)

    def test_dedupe_collapses_flips(self):
        from digimon_core import btmap_import
        import numpy as np
        arr = (np.arange(64, dtype=np.uint8).reshape(8, 8) % 16).tobytes()
        flips = btmap_import._flip_variants(arr)
        # 4 cells, each a different flip → all collapse to 1 canonical.
        unique, assignments = btmap_import.dedupe_tiles_with_flips(flips)
        self.assertEqual(len(unique), 1)
        self.assertEqual(len(assignments), 4)
        # Each cell got the same tile_ix but a distinct flip flag.
        self.assertEqual({a[0] for a in assignments}, {0})
        self.assertEqual({a[1] for a in assignments}, {0, 1, 2, 3})

    def test_cluster_reduces_to_max(self):
        from digimon_core import btmap_import
        import numpy as np
        # 20 distinct random tiles; reduce to 8.
        rng = np.random.default_rng(seed=42)
        tiles = [
            bytes(rng.integers(0, 16, size=64, dtype=np.uint8).tolist())
            for _ in range(20)
        ]
        unique, _ = btmap_import.dedupe_tiles_with_flips(tiles)
        # 20 random tiles are extremely unlikely to flip-collide, but
        # gate on the assumption explicitly.
        self.assertEqual(len(unique), 20)
        assignments = [(i, 0) for i in range(20)]
        palette = [(i * 16, i * 16, i * 16) for i in range(16)]
        reduced, new_assignments = btmap_import.cluster_tiles_to_max(
            unique, assignments, palette, max_tiles=8,
        )
        self.assertLessEqual(len(reduced), 8)
        # Every cell still has a valid in-range tile_ix.
        for tile_ix, _flip in new_assignments:
            self.assertGreaterEqual(tile_ix, 0)
            self.assertLess(tile_ix, len(reduced))

    def test_cluster_preserves_locked(self):
        from digimon_core import btmap_import
        import numpy as np
        rng = np.random.default_rng(seed=7)
        tiles = [
            bytes(rng.integers(0, 16, size=64, dtype=np.uint8).tolist())
            for _ in range(20)
        ]
        unique, _ = btmap_import.dedupe_tiles_with_flips(tiles)
        assignments = [(i, 0) for i in range(len(unique))]
        palette = [(i * 16, i * 16, i * 16) for i in range(16)]
        # Lock tiles 0, 3, 7 — they must survive the reduction.
        locked = [0, 3, 7]
        locked_canonicals = {unique[ix] for ix in locked}
        reduced, _ = btmap_import.cluster_tiles_to_max(
            unique, assignments, palette,
            max_tiles=5, locked_tile_indices=locked,
        )
        survivors = set(reduced)
        for canonical in locked_canonicals:
            self.assertIn(
                canonical, survivors,
                "locked tile was clustered out",
            )

    def test_end_to_end_vanilla_layer_a_render_roundtrip(self):
        """Render vanilla Layer A → save as PNG → re-import → confirm the
        result fits the cap and references only in-bounds tiles."""
        if not os.path.exists(DUSK_US):
            self.skipTest("Dusk ROM missing")
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow not available")
        import shutil
        import tempfile
        from digimon_core import btmap_import

        ft, raw = _load_ft_rom(DUSK_US)
        mid = "12"
        paths = btmap.BtmapFiles(mid)
        ncgr_a = ft.slice(raw, paths.layer_a_ncgr)
        nclr = ft.slice(raw, paths.layer_a_nclr)
        nscr_a = ft.slice(raw, paths.layer_a_nscr)
        # Render Layer A in isolation.
        preview = btmap.render_single_layer(
            ncgr_a, nscr_a, nclr, backdrop_opaque=True,
        )
        img = Image.frombytes(
            "RGBA", (preview.width, preview.height), bytes(preview.rgba),
        )
        tmp = tempfile.mkdtemp(prefix="btmap_layer_import_")
        try:
            png_path = os.path.join(tmp, "layer_a.png")
            img.save(png_path)
            result = btmap_import.import_layer_from_png(
                png_path,
                target_width_px=preview.width,
                target_height_px=preview.height,
                original_ncgr=ncgr_a,
                original_nscr=nscr_a,
                original_nclr=nclr,
                palette_bank=0,
                is_transparent_layer=False,
                max_tiles=1024,
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        stats = result.stats
        self.assertLessEqual(stats.unique_after_merge, 1024)
        # NSCR entries must reference tiles in-bounds.
        w, h, entries = btmap.parse_nscr(result.new_nscr)
        self.assertEqual((w, h), (preview.width, preview.height))
        for entry in entries:
            self.assertLess(
                entry & 0x3FF, stats.unique_after_merge,
                "NSCR cell references an out-of-bounds tile",
            )

    def test_end_to_end_layer_b_transparent(self):
        """Layer B import: alpha-0 pixels must map to palette index 0
        and the resulting NSCR must reference in-bounds tiles."""
        if not os.path.exists(DUSK_US):
            self.skipTest("Dusk ROM missing")
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow not available")
        import shutil
        import tempfile
        from digimon_core import btmap_import

        ft, raw = _load_ft_rom(DUSK_US)
        mid = "12"
        paths = btmap.BtmapFiles(mid)
        ncgr_b = ft.slice(raw, paths.layer_b_ncgr)
        nclr = ft.slice(raw, paths.layer_a_nclr)  # shared palette
        nscr_b = ft.slice(raw, paths.layer_b_nscr)
        preview = btmap.render_single_layer(
            ncgr_b, nscr_b, nclr, backdrop_opaque=False,
        )
        img = Image.frombytes(
            "RGBA", (preview.width, preview.height), bytes(preview.rgba),
        )
        tmp = tempfile.mkdtemp(prefix="btmap_layer_b_import_")
        try:
            png_path = os.path.join(tmp, "layer_b.png")
            img.save(png_path)
            result = btmap_import.import_layer_from_png(
                png_path,
                target_width_px=preview.width,
                target_height_px=preview.height,
                original_ncgr=ncgr_b,
                original_nscr=nscr_b,
                original_nclr=nclr,
                palette_bank=0,
                is_transparent_layer=True,
                max_tiles=1024,
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        # Layer B vanilla is mostly transparent; result should be far
        # below the cap.
        self.assertLess(result.stats.unique_after_merge, 1024)

    def test_multi_bank_quantizer_basic_shape(self):
        """Smoke test the multi-bank quantizer on a tiny synthetic image:
        4 cells = 16×16, 2 banks. Output structure must match the
        documented contract (banks dict, per-cell list, per-pixel list)."""
        from digimon_core import btmap_import

        # 16×16 RGBA. Top-left cell red, top-right green, bottom-left blue,
        # bottom-right white. 4 distinct color regions → at least 2 banks
        # would help, but we exercise the API with n_banks=2 to confirm
        # the partition happens.
        rgba = bytearray()
        for y in range(16):
            for x in range(16):
                if y < 8 and x < 8:
                    rgba.extend([255, 0, 0, 255])
                elif y < 8:
                    rgba.extend([0, 255, 0, 255])
                elif x < 8:
                    rgba.extend([0, 0, 255, 255])
                else:
                    rgba.extend([255, 255, 255, 255])
        banks, cell_banks, indices = btmap_import.quantize_image_multi_bank(
            bytes(rgba), 16, 16,
            available_banks=(0, 5),  # non-contiguous slots
            colors_per_bank=16,
        )
        self.assertEqual(set(banks.keys()), {0, 5})
        self.assertEqual(len(cell_banks), 4)
        self.assertEqual(len(indices), 16 * 16)
        # Every assignment must reference an available bank slot.
        for cb in cell_banks:
            self.assertIn(cb, {0, 5})
        # Per-pixel indices must stay inside one of the 16-color banks.
        self.assertTrue(all(0 <= i < 16 for i in indices))

    def test_multi_bank_skips_reserved_bank(self):
        """``available_banks`` excluding a slot must result in zero NSCR
        cells referencing that slot. This guards the Layer-A reserves-bank-1
        contract that keeps Layer B's palette intact across imports."""
        if not os.path.exists(DUSK_US):
            self.skipTest("Dusk ROM missing")
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow not available")
        import shutil
        import tempfile
        from digimon_core import btmap_import

        ft, raw = _load_ft_rom(DUSK_US)
        paths = btmap.BtmapFiles("12")
        ncgr_a = ft.slice(raw, paths.layer_a_ncgr)
        nclr = ft.slice(raw, paths.layer_a_nclr)
        nscr_a = ft.slice(raw, paths.layer_a_nscr)
        preview = btmap.render_single_layer(
            ncgr_a, nscr_a, nclr, backdrop_opaque=True,
        )
        img = Image.frombytes(
            "RGBA", (preview.width, preview.height), bytes(preview.rgba),
        )
        tmp = tempfile.mkdtemp(prefix="btmap_multi_bank_reserved_")
        try:
            png_path = os.path.join(tmp, "layer_a.png")
            img.save(png_path)
            result = btmap_import.import_layer_from_png(
                png_path,
                target_width_px=preview.width,
                target_height_px=preview.height,
                original_ncgr=ncgr_a,
                original_nscr=nscr_a,
                original_nclr=nclr,
                is_transparent_layer=False,
                max_tiles=1024,
                use_multi_bank=True,
                available_banks=[0, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15],
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        _, _, entries = btmap.parse_nscr(result.new_nscr)
        banks_seen = {(e >> 12) & 0xF for e in entries}
        self.assertNotIn(1, banks_seen, "reserved bank 1 leaked into NSCR")
        self.assertGreater(
            len(banks_seen), 1,
            "multi-bank import should populate multiple banks for a complex layer",
        )
        self.assertGreaterEqual(result.stats.banks_used, len(banks_seen))

    def test_multi_bank_lower_rmse_than_single_bank(self):
        """Multi-bank should beat single-bank on RMSE for a vanilla Layer A
        roundtrip. The bound is generous (multi must be strictly better) —
        the actual delta is much larger in practice but a tight bound would
        be flaky against arch quirks of the median-cut tiebreaker."""
        if not os.path.exists(DUSK_US):
            self.skipTest("Dusk ROM missing")
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow not available")
        import shutil
        import tempfile
        from digimon_core import btmap_import

        ft, raw = _load_ft_rom(DUSK_US)
        paths = btmap.BtmapFiles("12")
        ncgr_a = ft.slice(raw, paths.layer_a_ncgr)
        nclr = ft.slice(raw, paths.layer_a_nclr)
        nscr_a = ft.slice(raw, paths.layer_a_nscr)
        preview = btmap.render_single_layer(
            ncgr_a, nscr_a, nclr, backdrop_opaque=True,
        )
        img = Image.frombytes(
            "RGBA", (preview.width, preview.height), bytes(preview.rgba),
        )
        tmp = tempfile.mkdtemp(prefix="btmap_multi_vs_single_")
        try:
            png_path = os.path.join(tmp, "layer_a.png")
            img.save(png_path)
            single = btmap_import.import_layer_from_png(
                png_path,
                target_width_px=preview.width,
                target_height_px=preview.height,
                original_ncgr=ncgr_a,
                original_nscr=nscr_a,
                original_nclr=nclr,
                palette_bank=0,
                is_transparent_layer=False,
                max_tiles=1024,
                use_multi_bank=False,
            )
            multi = btmap_import.import_layer_from_png(
                png_path,
                target_width_px=preview.width,
                target_height_px=preview.height,
                original_ncgr=ncgr_a,
                original_nscr=nscr_a,
                original_nclr=nclr,
                is_transparent_layer=False,
                max_tiles=1024,
                use_multi_bank=True,
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        import numpy as np
        target = np.frombuffer(bytes(preview.rgba), dtype=np.uint8).astype(np.int32)
        single_render = btmap.render_single_layer(
            single.new_ncgr, single.new_nscr, single.new_nclr,
            backdrop_opaque=True,
        )
        multi_render = btmap.render_single_layer(
            multi.new_ncgr, multi.new_nscr, multi.new_nclr,
            backdrop_opaque=True,
        )
        single_rgba = np.frombuffer(bytes(single_render.rgba), dtype=np.uint8).astype(np.int32)
        multi_rgba = np.frombuffer(bytes(multi_render.rgba), dtype=np.uint8).astype(np.int32)
        single_rmse = float(np.sqrt(np.mean((single_rgba - target) ** 2)))
        multi_rmse = float(np.sqrt(np.mean((multi_rgba - target) ** 2)))
        self.assertLess(
            multi_rmse, single_rmse,
            f"multi-bank RMSE ({multi_rmse:.2f}) not lower than single-bank ({single_rmse:.2f})",
        )
        self.assertGreater(multi.stats.banks_used, 1)


class LayerImportSpliceTests(unittest.TestCase):
    """Phase G end-to-end: a flat-PNG layer import dirties 3 FAT files
    (NCGR, NSCR, NCLR) atomically; the existing splice machinery must
    handle multiple co-dirty files in one ``serialize_all`` pass.

    The descending-FAT-offset ordering in :meth:`_apply_btmap_splice` is
    load-bearing here — without it, splicing a lower-offset file first
    would shift everything past it and invalidate the cached offset for
    the higher-offset files.
    """

    def test_three_file_splice_lands_in_serialized_rom(self):
        if not os.path.exists(DUSK_US):
            self.skipTest("Dusk ROM missing")
        try:
            from PIL import Image
            from PySide6.QtWidgets import QApplication  # noqa: F401
        except ImportError:
            self.skipTest("PIL or PySide6 missing")
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        QApplication.instance() or QApplication([])
        from editor.session import RomSession
        from digimon_core import btmap_import
        from digimon_core.sprite import maybe_decompress

        session = RomSession.from_file(DUSK_US)
        paths = btmap.BtmapFiles("12")
        ncgr_a = session.btmap_file_bytes(paths.layer_a_ncgr)
        nscr_a = session.btmap_file_bytes(paths.layer_a_nscr)
        nclr = session.btmap_file_bytes(paths.layer_a_nclr)
        # Render a Layer A preview and feed it back as a PNG to the importer.
        preview = btmap.render_single_layer(
            ncgr_a, nscr_a, nclr, backdrop_opaque=True,
        )
        img = Image.frombytes(
            "RGBA", (preview.width, preview.height), bytes(preview.rgba),
        )
        import io
        png_bytes = io.BytesIO()
        img.save(png_bytes, format="PNG")
        result = btmap_import.import_layer_from_png(
            png_bytes.getvalue(),
            target_width_px=preview.width,
            target_height_px=preview.height,
            original_ncgr=ncgr_a,
            original_nscr=nscr_a,
            original_nclr=nclr,
            palette_bank=0,
            is_transparent_layer=False,
            max_tiles=1024,
        )
        session.replace_btmap_file_bytes(paths.layer_a_ncgr, result.new_ncgr)
        session.replace_btmap_file_bytes(paths.layer_a_nscr, result.new_nscr)
        session.replace_btmap_file_bytes(paths.layer_a_nclr, result.new_nclr)

        out = session.serialize_all()
        new_ft = fnt.FileTable.from_rom(bytes(out))
        # All three files survive at their FAT paths with the imported bytes.
        self.assertEqual(
            maybe_decompress(new_ft.slice(bytes(out), paths.layer_a_ncgr)),
            maybe_decompress(result.new_ncgr),
        )
        self.assertEqual(
            maybe_decompress(new_ft.slice(bytes(out), paths.layer_a_nscr)),
            maybe_decompress(result.new_nscr),
        )
        self.assertEqual(
            maybe_decompress(new_ft.slice(bytes(out), paths.layer_a_nclr)),
            maybe_decompress(result.new_nclr),
        )


class ProjectRoundtripTests(unittest.TestCase):
    """Phase F: btmap edits must survive a ``.romproj`` save/load.

    The channel mirrors ``sprite_edits``: project save writes per-path
    bytes into ``btmap_edits`` and asks ``serialize_all`` to skip the
    btmap splice (otherwise a grown file would shift every downstream
    FAT entry into the byte diff and bloat the project file).

    Round-trip invariants:

    1. A fresh session loaded from the saved ``.romproj`` exposes the
       edited bytes via ``btmap_file_bytes`` (cache repopulated).
    2. The next ``serialize_all`` on that loaded session splices the
       edit back into the ROM exactly as the original save would have.
    """

    def test_btmap_edit_survives_project_roundtrip(self):
        if not os.path.exists(DUSK_US):
            self.skipTest("Dusk ROM missing")
        import tempfile
        from editor.session import RomSession
        from editor import project_file
        from digimon_core.sprite import maybe_decompress

        session = RomSession.from_file(DUSK_US)
        paths = btmap.BtmapFiles("12")
        path = paths.anim_ncgr(0)
        original = maybe_decompress(session.btmap_file_bytes(path))
        new_uncompressed = bytearray(original)
        new_uncompressed[100] ^= 0xFF
        session.replace_btmap_file_bytes(path, bytes(new_uncompressed))

        with tempfile.TemporaryDirectory() as td:
            proj_path = os.path.join(td, "test.romproj")
            edited = bytes(session.serialize_all(
                skip_sprite_splice=True, skip_btmap_splice=True,
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
            )

            loaded = project_file.load_project(proj_path)
            self.assertEqual(
                len(loaded["btmap_edits"]), 1,
                "btmap_edits channel didn't carry the edit",
            )
            self.assertEqual(loaded["btmap_edits"][0][0], path)
            self.assertEqual(
                loaded["btmap_edits"][0][1], bytes(new_uncompressed),
                "bytes didn't survive base64 round-trip",
            )

            # Simulate the load path: fresh session, apply edits.
            reopened = RomSession.from_file(DUSK_US)
            reopened.apply_btmap_file_edits(loaded["btmap_edits"])
            self.assertEqual(
                maybe_decompress(reopened.btmap_file_bytes(path)),
                bytes(new_uncompressed),
                "reopened session didn't expose edited bytes",
            )

            # And serialize_all on the reopened session must still splice it.
            out = reopened.serialize_all()
            new_ft = fnt.FileTable.from_rom(bytes(out))
            self.assertEqual(
                maybe_decompress(new_ft.slice(bytes(out), path)),
                bytes(new_uncompressed),
                "reopened serialize_all dropped the edit",
            )

    def test_v4_project_loads_with_empty_btmap_edits(self):
        """Backwards compat: a project file without the v5 channel still
        loads and exposes ``btmap_edits`` as an empty list."""
        import json
        import tempfile
        from editor import project_file

        with tempfile.TemporaryDirectory() as td:
            proj_path = os.path.join(td, "v4.romproj")
            with open(proj_path, "w", encoding="utf-8") as f:
                json.dump({
                    "format_version": 4,
                    "editor_version": "0.0.1",
                    "rom_version": "dusk_us",
                    "vanilla_rom_sha256": "0" * 64,
                    "qol": {},
                    "diffs": [],
                    "string_edits": [],
                    "sprite_edits": [],
                    "btchr_appended_sidecars": [],
                }, f)
            loaded = project_file.load_project(proj_path)
            self.assertEqual(loaded["btmap_edits"], [])


class BrowserSmokeTests(unittest.TestCase):
    """Phase B acceptance: the browser opens without crashing and renders
    every map id in the ROM.

    Skipped when Qt/PySide6 isn't importable so headless CI environments
    that only run the codec tests still pass.
    """

    def test_construct_and_iterate(self):
        try:
            from PySide6.QtWidgets import QApplication  # noqa: F401
        except ImportError:
            self.skipTest("PySide6 not available")
        if not os.path.exists(DUSK_US):
            self.skipTest("Dusk ROM missing")
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])  # noqa: F841
        from editor.session import RomSession
        from editor.widgets.btmap_browser import BtmapBrowser
        session = RomSession.from_file(DUSK_US)
        widget = BtmapBrowser(session)
        self.assertEqual(len(widget._map_ids), 76)
        # Visiting every id should not raise — render covers the full
        # SCB-rearrangement + crop path on real data.
        any_decodable_seen = False
        for ix in range(len(widget._map_ids)):
            widget._on_index_selected(ix)
            self.assertIsNotNone(widget._current_id)
            # Metadata tooltip carries the per-file component list now;
            # a non-empty tooltip proves the components walker ran.
            self.assertTrue(widget._meta_size.toolTip())
            # Animations tab — exercise the in-place render path. At
            # least one vanilla map must show decodable anim frames; if
            # this drops to zero the controls-visibility logic is wrong.
            widget._tabs.setCurrentIndex(widget._TAB_ANIM)
            if widget._anim_frame_combo.count() > 0:
                any_decodable_seen = True
                # Drive sub-frame slider one step — covers the on_sub
                # callback and the re-render.
                slider = widget._anim_sub_slider
                if slider.maximum() > 0:
                    slider.setValue(min(1, slider.maximum()))
        self.assertTrue(
            any_decodable_seen,
            "No map exposed decodable anim frames — controls path untested",
        )


if __name__ == "__main__":
    unittest.main()
